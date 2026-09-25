#!/usr/bin/env python3
"""Refuse to publish anything that carries this household's data.

    ./deploy/publish_check.py                     the commits not on origin/main yet
    ./deploy/publish_check.py <base>..<head>      a range, e.g. before a push
    ./deploy/publish_check.py --staged            what is staged right now

A public repository is published from a checkout that is not the one the house
runs on, and this is the gate in between (see "Publishing" in CLAUDE.md). It
reads the household's real values *on this machine* and fails if any of them
is in what would leave it:

  1. every credential in the live env file and the checkout's seed one;
  2. every login id, email and phone number in the portal's user store;
  3. the sanitizer's identifiers -- `deploy/sanitize.py --check` over the tree,
     with `deploy/sanitize-rules.local.py` if it is present (it names the
     household; without it this check is much weaker, and says so);
  4. anything shaped like an API key or a private key, in the added lines;
  5. a commit whose author or committer email is not the allowed one
     (`git config publish.email`, else the repository's `user.email`).

It prints what kind of value it found and where, never the value itself: the
output of a check like this ends up in terminals and logs.

Exit status 0 means publishable, 1 means it found something, 2 means it could
not run a check it needed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _repo() -> Path:
    """The repository being published: the one this is run from, which is not
    always the one this file is in -- a publishing clone whose main predates
    the gate runs the working checkout's copy against itself."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, check=True).stdout.strip()
        return Path(top) if top else ROOT
    except (OSError, subprocess.CalledProcessError):
        return ROOT


REPO = _repo()

# Shapes that are a credential whoever it belongs to. Test fixtures that only
# carry the header (`-----BEGIN OPENSSH PRIVATE KEY-----\nx\n`) do not match
# the private-key rule, which wants a body after it.
KEY_SHAPES = [
    (r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}", "an OpenAI/Anthropic-style API key"),
    (r"ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}", "a GitHub token"),
    (r"AKIA[0-9A-Z]{16}", "an AWS access key id"),
    (r"AIza[0-9A-Za-z_-]{35}", "a Google API key"),
    (r"xox[abpr]-[A-Za-z0-9-]{10,}", "a Slack token"),
    (r"hf_[A-Za-z0-9]{30,}", "a Hugging Face token"),
    (r"gsk_[A-Za-z0-9]{30,}|tvly-[A-Za-z0-9]{20,}", "a provider API key"),
    (r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}", "a JWT"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*[A-Za-z0-9+/=]{40,}", "a private key"),
]
# Values in an env file that are not secrets: addresses, flags, numbers, names.
_NOT_SECRET = re.compile(r"(?:https?|wss?|mqtts?)://\S*|\d+|true|false|yes|no", re.I)
_HOSTNAME = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?", re.I)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True,
                          check=True).stdout


def live_config() -> tuple[dict, Path | None]:
    sys.path.insert(0, str(ROOT / "deploy"))
    try:
        import yaml
        import deploy  # resolves the live config the same way every command does
        path = Path(deploy.CONFIG)
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}), path
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not read the live config: {exc})")
        return {}, None


def env_values(paths: list[Path]) -> dict[str, str]:
    """value -> the key it came from, for everything that looks like a secret."""
    out: dict[str, str] = {}
    for p in paths:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if len(value) < 10 or _NOT_SECRET.fullmatch(value):
                continue
            if _HOSTNAME.fullmatch(value) and not re.search(r"[A-Za-z0-9]{20,}", value):
                continue
            out.setdefault(value, f"credential {key.strip()}")
    return out


def user_values(state: str) -> dict[str, str]:
    out: dict[str, str] = {}
    path = Path(state) / "home-core" / "users.json" if state else None
    try:
        doc = json.loads(path.read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return out
    users = doc.values() if isinstance(doc, dict) else doc
    for u in users:
        if isinstance(u, dict):
            for field in ("username", "email", "phone"):
                v = str(u.get(field) or "").strip()
                if len(v) >= 5:
                    out.setdefault(v, f"a user store {field}")
    return out


def added_lines(diff: str) -> list[tuple[str, str]]:
    """(file, line) for every added line of a unified diff."""
    # By hunk rather than by prefix: an added line whose own text starts with
    # "++" (`++i;`) reads as "+++" and was taken for a file header and skipped.
    out, current, in_hunk = [], "", False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
        elif not in_hunk and line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            out.append((current, line[1:]))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="publish_check")
    ap.add_argument("range", nargs="?", default="origin/main..HEAD")
    ap.add_argument("--staged", action="store_true")
    args = ap.parse_args(argv)

    if args.staged:
        diff = git("diff", "--cached", "--no-color", "--text")
        commits: list[str] = []
        what = "the staged changes"
    else:
        diff = git("diff", "--no-color", "--text", args.range.replace("..", "...", 1)
                   if "..." not in args.range else args.range)
        commits = [c for c in git("rev-list", args.range).split() if c]
        what = f"{args.range} ({len(commits)} commit(s))"
    lines = added_lines(diff)
    print(f"publish check: {REPO.name} {what}, {len(lines)} added line(s)")
    problems: list[str] = []

    cfg, cfg_path = live_config()
    paths = cfg.get("paths") or {}
    envs = [REPO / "secrets" / "smart-home-bot.env", ROOT / "secrets" / "smart-home-bot.env"]
    if paths.get("config"):
        envs.append(Path(paths["config"]) / "smart-home-bot.env")
    needles = {**env_values(envs), **user_values(str(paths.get("state") or ""))}
    if not needles:
        print("  cannot run: found no credentials or user store to check against "
              "(is the live config reachable from here?)")
        return 2
    print(f"  checking against {len(needles)} real value(s) from this machine")

    # 1-2. real values, anywhere in what is added
    for value, label in needles.items():
        hits = sorted({f for f, line in lines if value in line})
        if hits:
            problems.append(f"{label} in {', '.join(hits[:5])}")

    # 4. key shapes in added lines
    for pattern, label in KEY_SHAPES:
        rx = re.compile(pattern)
        hits = sorted({f for f, line in lines if rx.search(line)})
        if hits:
            problems.append(f"{label} in {', '.join(hits[:5])}")

    # 5. commit identity
    allowed = (subprocess.run(["git", "config", "publish.email"], cwd=REPO, capture_output=True,
                              text=True).stdout.strip()
               or subprocess.run(["git", "config", "user.email"], cwd=REPO, capture_output=True,
                                 text=True).stdout.strip())
    for c in commits:
        author, committer = git("show", "-s", "--format=%ae%n%ce", c).split("\n")[:2]
        for role, email in (("author", author), ("committer", committer)):
            if email != allowed:
                problems.append(f"commit {c[:8]} {role} email is not the allowed one "
                                f"({'set git config publish.email' if not allowed else allowed})")

    # 3. the sanitizer over the tree, with the household's own rules
    # The sanitizer checks the tree it sits in, so it is the published repo's own.
    local = REPO / "deploy" / "sanitize-rules.local.py"
    if not local.exists():
        print("  warning: deploy/sanitize-rules.local.py is missing, so the sanitizer "
              "checks shapes only and cannot see this household's names")
    r = subprocess.run([sys.executable, str(REPO / "deploy" / "sanitize.py"), "--check"],
                       cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        # The sanitizer quotes the matching line; only where it is goes out.
        tail = [re.sub(r"^(\s*\S+:\d+):.*$", r"\1", ln)
                for ln in r.stdout.splitlines() if ln.strip()][-8:]
        problems.append("sanitize.py --check found household identifiers:\n      "
                        + "\n      ".join(tail))

    if problems:
        print("NOT publishable:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("publishable: no household data found")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""What a commit must not carry out of the house, checked before it is made.

Every commit the assistants make to a project goes through the broker -- the
Programmer's `commit_changes`, Alfred's `commit` -- and the self-improvement
pipeline's `improve commit` makes the same check on the host
(deploy/improve/guard.py). This is the broker's half.

Two kinds of finding:

* **This house's real values**: its credentials, and the logins, e-mails and
  phones in the portal's user store. The broker is not given them. The deployer
  writes salted SHA-256 hashes of each (`deploy.py`, `write_leak_guard`) to a
  directory mounted here read-only; a commit's added lines are cut into words,
  each word hashed, and a match is a refusal naming the *kind* of value and the
  file -- never the value, which this process does not have.
* **Key shapes** anybody's keys have -- an API key, a token, a private key --
  the publish gate's list (deploy/publish_check.py, kept equal by a test).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from loguru import logger

# Kept equal to deploy/publish_check.py's KEY_SHAPES by
# tests/code_broker/test_leaks.py: the two gates must not disagree on what a
# key looks like.
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
_KEYS = [(re.compile(p), label) for p, label in KEY_SHAPES]
# The words a value can hide in: anything a credential, a login or an address is
# made of, cut at everything else (quotes, `=`, spaces, brackets).
_WORD = re.compile(r"[A-Za-z0-9_\-+/.@:]{5,}")
_PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
GUARD_FILE = os.environ.get("LEAK_GUARD_FILE", "/leak-guard/hashes.json")


def digest(salt: str, value: str) -> str:
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()[:32]


def load(path: str | None = None) -> dict | None:
    """The hashes the deployer wrote, or None when there are none -- then only
    key shapes are checked, and the log says so."""
    path = path or GUARD_FILE
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), dict):
        return None
    return doc


def added_lines(diff: str) -> list[tuple[str, str]]:
    """(file, line) for every added line of a unified diff, by hunk -- an added
    line that itself starts with "++" is not a file header."""
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


def findings(diff: str, guard: dict | None) -> list[str]:
    """Which kinds of value *diff* adds, and in which files."""
    lines = added_lines(diff)
    where: dict[str, set[str]] = {}
    if guard:
        salt, items = str(guard.get("salt") or ""), guard["items"]
        for f, line in lines:
            words = set(_WORD.findall(line))
            words |= {w.rstrip("=") for w in words} | {w.strip(".:") for w in words}
            words |= {re.sub(r"\D", "", m) for m in _PHONE.findall(line)}
            for w in words:
                label = items.get(digest(salt, w))
                if label:
                    where.setdefault(label, set()).add(f)
    for rx, label in _KEYS:
        for f, line in lines:
            if rx.search(line):
                where.setdefault(label, set()).add(f)
    return [f"{label} in {', '.join(sorted(files)[:5])}" for label, files in where.items()]


def warn_if_blind(guard: dict | None) -> None:
    if guard is None:
        logger.warning("code-broker: no leak guard at {}; commits are checked for key "
                       "shapes only, not for this house's own values", GUARD_FILE)

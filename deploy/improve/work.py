"""Where a fix is made, and how it is committed: code, not the agent's shell.

The Programmer may not run git that changes anything -- no branch, no
worktree, no commit (`build_opencode_config` in deploy.py, and the reasons
there). Those go through something with rules in it. For its own projects that
is the code broker; for the self-improvement pipeline it is this:

    improve start <id> <repo>     a git worktree of <repo> on branch improve/<id>,
                                  under {paths.state}/improve/work/, and its path
    improve commit <id> <repo> -m "<message>"
                                  commit what changed there, after the checks below
    improve status <id>           the worktrees of that request and what is on them

The worktree is the only place the agent edits: never the checkout that
deploys (a deploy ships the working tree, uncommitted edits included), never a
plugin's own checkout. The commit refuses, for this stack's repository, the
paths a fix must not touch -- the deployer, the admin page, the manifest, the
benchmark, this pipeline, the credentials -- and anything the sanitizer calls
household data. Nothing here merges, pushes or deploys.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

# A fix to the stack may not change how it is deployed, judged or guarded.
STACK_PROTECTED = ("deploy/", "admin/", "secrets/", "CLAUDE.md", "home-stack",
                   "services/nanobot/bench/", ".github/")
_ID = re.compile(r"^[0-9]{1,9}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class WorkError(Exception):
    pass


def _git(path: Path | str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                       timeout=120)
    if check and r.returncode != 0:
        raise WorkError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def _repo(repos: list[dict], name: str) -> dict:
    if not _NAME.match(name or ""):
        raise WorkError(f"not a repository name: {name!r}")
    for r in repos:
        if r["name"] == name:
            return r
    raise WorkError(f"{name!r} is not in repos.json; the places a fix may go are: "
                    + ", ".join(r["name"] for r in repos))


def worktree(d: Path, rid: str, name: str) -> Path:
    if not _ID.match(str(rid)):
        raise WorkError(f"not a request number: {rid!r}")
    return d / "work" / f"{rid}-{name}"


def start(d: Path, repos: list[dict], rid: str, name: str) -> Path:
    """The worktree for request *rid* in repository *name*, made if needed."""
    repo = _repo(repos, name)
    path = worktree(d, rid, name)
    branch = f"improve/{rid}"
    if path.exists():
        if _git(path, "rev-parse", "--abbrev-ref", "HEAD") != branch:
            raise WorkError(f"{path} exists and is not on {branch}")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    # From the branch the repository is on: for the stack that is the trunk
    # the working checkout deploys from, for a plugin whatever it tracks.
    base = repo.get("branch") or "HEAD"
    exists = _git(repo["path"], "branch", "--list", branch)
    if exists:
        _git(repo["path"], "worktree", "add", str(path), branch)
    else:
        _git(repo["path"], "worktree", "add", "-b", branch, str(path), base)
    if repo["kind"] == "stack":
        # The sanitizer's household rules are gitignored, so a fresh worktree
        # would check shapes only; linked, never copied, never committed.
        local = Path(repo["path"]) / "deploy" / "sanitize-rules.local.py"
        if local.exists() and not (path / "deploy" / "sanitize-rules.local.py").exists():
            (path / "deploy" / "sanitize-rules.local.py").symlink_to(local)
    return path


def _changed(path: Path) -> list[tuple[str, str]]:
    out = []
    for line in _git(path, "status", "--porcelain", "--untracked-files=all").splitlines():
        status, name = line[:2], line[3:].strip().strip('"')
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        out.append((status, name))
    return out


def refusals(kind: str, changed: list[tuple[str, str]]) -> list[str]:
    """Why a commit of *changed* is refused, or []."""
    why = []
    for status, name in changed:
        if kind == "stack" and any(name == p.rstrip("/") or name.startswith(p)
                                   for p in STACK_PROTECTED):
            why.append(f"{name}: a fix may not change how the stack is deployed, judged "
                       "or guarded")
        if "D" in status and re.search(r"(^|/)test[^/]*\.py$|(^|/)tests?/", name):
            why.append(f"{name}: a test is deleted")
    return why


def commit(d: Path, repos: list[dict], rid: str, name: str, message: str) -> str:
    repo = _repo(repos, name)
    path = worktree(d, rid, name)
    if not path.exists():
        raise WorkError(f"no worktree for #{rid} in {name}: run `improve start {rid} {name}`")
    if _git(path, "rev-parse", "--abbrev-ref", "HEAD") != f"improve/{rid}":
        raise WorkError(f"{path} is not on improve/{rid}")
    message = (message or "").strip()
    if len(message) < 10:
        raise WorkError("say what the commit does: -m with a real message")
    changed = _changed(path)
    if not changed:
        raise WorkError("nothing changed in the worktree")
    why = refusals(repo["kind"], changed)
    if repo["kind"] == "stack":
        r = subprocess.run([sys.executable, str(path / "deploy" / "sanitize.py"), "--check"],
                           cwd=path, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            # Where, never what: the matching line is household data.
            where = sorted({m.group(1) for m in re.finditer(r"^\s*(\S+?):\d+", r.stdout, re.M)})
            why.append("the sanitizer found household data in: " + (", ".join(where[:8]) or "?"))
    if why:
        raise WorkError("refused, nothing committed:\n  " + "\n  ".join(why))
    # The owner's identity, as the repository already commits with it: for
    # the stack the no-reply address the publish gate requires (CLAUDE.md).
    email = (_git(repo["path"], "config", "publish.email", check=False)
             or _git(repo["path"], "config", "user.email", check=False))
    user = _git(repo["path"], "config", "user.name", check=False) or "Alfred"
    if not email:
        raise WorkError(f"{name} has no user.email to commit as")
    _git(path, "add", "-A")
    _git(path, "-c", f"user.name={user}", "-c", f"user.email={email}", "commit", "-q",
         "-m", f"{message}\n\nFix request #{rid}, made in the Programmer.")
    return _git(path, "log", "-1", "--format=%h %s") + "\n" + _git(path, "show", "--stat",
                                                                    "--format=", "HEAD")


def status(d: Path, rid: str) -> str:
    if not _ID.match(str(rid)):
        raise WorkError(f"not a request number: {rid!r}")
    out = []
    for path in sorted((d / "work").glob(f"{rid}-*")):
        log = _git(path, "log", "--format=  %h %s", "HEAD", "--not", "--remotes", "-n", "10",
                   check=False)
        dirty = len(_changed(path))
        out.append(f"{path}  on {_git(path, 'rev-parse', '--abbrev-ref', 'HEAD', check=False)}"
                   f"{f', {dirty} uncommitted' if dirty else ''}\n{log}")
    return "\n".join(out) or f"no worktree for #{rid}"


def write_shim(d: Path) -> Path:
    """`{improve}/bin/improve`: how the Programmer calls this. Inside the one
    folder its config lets it reach, so running it asks nobody for anything."""
    b = d / "bin"
    b.mkdir(exist_ok=True)
    shim = b / "improve"
    shim.write_text(f"#!/bin/sh\nexec {ROOT}/.venv/bin/python {HERE}/cli.py \"$@\"\n",
                    encoding="utf-8")
    os.chmod(shim, 0o700)
    return shim

"""Publishing and deploying a fix: the last two steps, and the person's to ask for.

    improve publish <id> <repo>   the fix onto the repository's own checkout -- a
                                  fast-forward, never a merge -- and, for a plugin,
                                  pushed to its remote
    improve deploy <id> <repo>    the services that repository provides, deployed

The Programmer runs these only when the person has said to, in the request's
own conversation (the request's prompt says so). The household decided on
2026-09-29 that its Programmer may, after a fix request that it asked for and
answered yes to. What the code holds, whoever calls it:

* a checkout with uncommitted work is somebody's, and is not touched;
* a checkout that moved on since the fix branched is not merged into: the fix
  is started again from it;
* this stack's working checkout never pushes (CLAUDE.md: publishing it is a
  pull request from the publishing clone), so publishing it is the
  fast-forward alone;
* a deploy deploys only what is published -- the deployer reads checkouts,
  never a worktree -- refuses to start beside another deploy or a benchmark,
  refuses a stack checkout with uncommitted work (a deploy ships it), and
  deploys admin last when the stack's own code changed, so the admin page's
  copy is current (CLAUDE.md, "One deploy at a time").
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import work as W

ROOT = W.ROOT


def _is_ancestor(path: str, older: str, newer: str) -> bool:
    return subprocess.run(["git", "-C", path, "merge-base", "--is-ancestor", older, newer],
                          capture_output=True).returncode == 0


def _new_commits(path: str, rid: str) -> list[str]:
    out = W._git(path, "rev-list", f"HEAD..improve/{rid}", check=False)
    return [c for c in out.split() if c]


def publish(d: Path, repos: list[dict], rid: str, name: str, state: Path | None = None) -> str:
    """Fast-forward *name*'s own checkout to the fix, and push a plugin's."""
    repo = W._repo(repos, name)
    wt = W.worktree(d, rid, name)
    if not wt.exists():
        raise W.WorkError(f"no worktree for #{rid} in {name}")
    if state is not None:
        import guard  # noqa: PLC0415
        guard.require_approval(state, rid, wt, "publish")
    if W._changed(wt):
        raise W.WorkError(f"the worktree has uncommitted changes: commit them first "
                          f"(improve commit {rid} {name} -m ...)")
    live = repo["path"]
    head = W._git(live, "rev-parse", "--abbrev-ref", "HEAD")
    if head != repo.get("branch"):
        raise W.WorkError(f"{live} is on {head}, not {repo.get('branch')}")
    dirty = W._changed(Path(live))
    if dirty:
        raise W.WorkError(f"{live} has uncommitted work ({len(dirty)} file(s)); it is "
                          "somebody's, and a fix is not published over it")
    if not _new_commits(live, rid):
        raise W.WorkError(f"improve/{rid} has nothing {name} does not already have")
    if not _is_ancestor(live, "HEAD", f"improve/{rid}"):
        # The checkout moved on while the fix was being made. Put the fix on top
        # of where it is now -- in the worktree, never the checkout -- and stop:
        # what would be published is code nobody has tested together. A
        # conflict is not resolved here; the worktree is left as it was.
        r = subprocess.run(["git", "-C", str(wt), "rebase", "-q", head], capture_output=True,
                           text=True, timeout=300)
        if r.returncode != 0:
            subprocess.run(["git", "-C", str(wt), "rebase", "--abort"], capture_output=True,
                           timeout=60)
            raise W.WorkError(f"{name} moved on since the fix branched, and the fix does not "
                              f"apply on top of it cleanly; nothing was changed. Start the fix "
                              f"again from the current code.")
        raise W.WorkError(f"{name} moved on since the fix branched: the fix is now rebased on "
                          f"top of it, in the worktree. That combination is untested -- run "
                          f"improve test {rid} {name} (and improve bench if it changes how the "
                          f"assistant behaves), then publish again.")
    import checks  # noqa: PLC0415 -- imports this module's sibling, not a cycle
    tested = checks._load(d, rid, name, "tests") or {}
    if tested.get("head") and tested["head"] != W._git(wt, "rev-parse", "HEAD") and \
            tested.get("head") != W._git(wt, "rev-parse", "HEAD~"):
        raise W.WorkError(f"the last test run was on other code than what would be published: "
                          f"improve test {rid} {name}, then publish")
    if tested.get("head") == W._git(wt, "rev-parse", "HEAD") and not tested.get("ok"):
        raise W.WorkError(f"the tests of what would be published fail: improve test {rid} {name}")
    W._git(live, "merge", "--ff-only", "-q", f"improve/{rid}")
    out = [f"{name}: {head} is now at {W._git(live, 'log', '-1', '--format=%h %s')}"]
    if repo["kind"] == "stack":
        out.append("not pushed: this checkout never pushes -- publishing it is a pull request "
                   "from the publishing clone (CLAUDE.md)")
    elif repo.get("remote"):
        r = subprocess.run(["git", "-C", live, "push", "origin", head], capture_output=True,
                           text=True, timeout=180)
        out.append(f"pushed to origin/{head}" if r.returncode == 0 else
                   f"NOT pushed: {(r.stderr or r.stdout).strip()[-200:]} -- the checkout has it")
    else:
        out.append("no remote to push to")
    return "\n".join(out)


def services_for(repo: dict, rid: str) -> list[str]:
    """What deploying the fix means: a plugin's own services, or the stack's
    services whose units hold the files the fix changed."""
    if repo["kind"] != "stack":
        return list((repo.get("provides") or {}).get("services") or [])
    import yaml  # noqa: PLC0415
    # The fix's own commits, by the line `improve commit` writes into each.
    commits = W._git(repo["path"], "log", "--format=%H", "--fixed-strings",
                     f"--grep=Fix request #{rid}, made in the Programmer.", f"improve/{rid}",
                     check=False).split()
    changed = sorted({f for c in commits for f in W._git(
        repo["path"], "diff-tree", "--no-commit-id", "--name-only", "-r", c,
        check=False).splitlines() if f})
    manifest = yaml.safe_load((Path(repo["path"]) / "deploy" / "manifest.yml")
                              .read_text(encoding="utf-8")) or {}
    out = []
    for name, svc in (manifest.get("services") or {}).items():
        dirs = {str(u.get("dir")).rstrip("/") + "/" for u in (svc.get("units") or [])
                if u.get("dir")}
        if any(f.startswith(dr) for f in changed for dr in dirs):
            out.append(name)
    return out


def deploy_running() -> bool:
    """A deploy from the admin page (anchored, CLAUDE.md) or from a shell."""
    for pattern in (r"^/usr/local/bin/python /app/admin/deploy/deploy\.py",
                    r"deploy/deploy\.py "):
        if subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0:
            return True
    return False


def bench_running(state: Path) -> bool:
    try:
        return bool(json.loads((state / "admin" / "bench" / "job.json").read_text()).get("running"))
    except (OSError, ValueError):
        return False


def deploy(d: Path, repos: list[dict], rid: str, name: str, state: Path, run=subprocess.run,
           busy=deploy_running) -> str:
    """Deploy what *name*'s published fix changed."""
    repo = W._repo(repos, name)
    live = repo["path"]
    if not _is_ancestor(live, f"improve/{rid}", "HEAD"):
        raise W.WorkError(f"improve/{rid} is not published in {name} yet: "
                          f"improve publish {rid} {name}")
    wt = W.worktree(d, rid, name)
    if wt.exists():
        import guard  # noqa: PLC0415
        guard.require_approval(state, rid, wt, "deploy")
    services = services_for(repo, rid)
    if not services:
        raise W.WorkError(f"nothing in {name} to deploy for #{rid}")
    if repo["kind"] == "stack" and W._changed(Path(live)):
        raise W.WorkError(f"{live} has uncommitted work, and a deploy ships it: not deploying")
    if busy():
        raise W.WorkError("another deploy is running; try again when it has finished")
    if bench_running(state):
        raise W.WorkError("a benchmark is running, and a deploy would cut it off; try later")
    order = [s for s in services if s != "admin"]
    if repo["kind"] == "stack":
        order.append("admin")
    lines = []
    for svc in order:
        r = run([str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "deploy" / "deploy.py"),
                 svc, "--no-color"], capture_output=True, text=True, timeout=1800, cwd=str(ROOT))
        tail = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()][-2:]
        lines.append(f"{svc}: {'ok' if r.returncode == 0 else 'FAILED'} -- {' / '.join(tail)[-300:]}")
        if r.returncode != 0:
            lines.append("stopped here: the services after it were not deployed")
            break
    return "\n".join(lines)

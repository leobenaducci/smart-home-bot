"""What a fix has to pass before it may be committed: its tests, and -- for a
change to how the assistant behaves -- the benchmark, before and after.

    improve test <id> <repo>      the test suites of what the worktree changed
    improve bench <id> <repo>     the benchmark's roles on the current code and on the
                                  worktree's, in the member's own assistant container

The commit refuses a change whose tests fail, and a change to the assistant's
behaviour -- its skills, its agent code, its prompts -- with no benchmark run
for exactly that diff, or one where a role got worse. That is the part a
model cannot talk its way past: on 2026-09-29 the Programmer proposed a
runner change that would have broken every skill call, and later edited the
record of a measurement to fit its plan. Tests and the benchmark do not read
the plan's reasoning; they run the code.

The benchmark's cases come from this checkout, never the worktree -- they are
the answer key, kept out of the fixer's reach -- and only role totals and the
ids of failing cases come back, never a case's prompt or its checks.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import work as W

ROOT = W.ROOT
PY = str(ROOT / ".venv" / "bin" / "python")
# What in this stack changes how the assistant behaves, and so needs the
# benchmark as well as the tests.
BEHAVIOUR = ("services/nanobot/nanobot/", "services/nanobot/config/")
# The benchmark's roles for a behaviour change, unless asked for others: the
# everyday turns and the tool calls are where a skill or runner change shows.
DEFAULT_ROLES = "everyday,tools"
# A role may lose this many cases between runs and still count as unchanged:
# the models sample, and the same case passes and fails from run to run.
NOISE = 1


def changed_files(path: Path, branch: str = "") -> list[str]:
    """Files the worktree changes against the branch it started from, committed
    or not."""
    base = W._git(path, "merge-base", "HEAD", branch, check=False) if branch else ""
    committed = []
    if base:
        committed = W._git(path, "diff", "--name-only", base, "HEAD", check=False).splitlines()
    return sorted(set(committed) | {name for _, name in W._changed(path)})


def diff_digest(path: Path) -> str:
    """What exactly is being judged: the worktree's uncommitted diff plus its
    untracked files, hashed. A benchmark run is for this digest and no other."""
    h = hashlib.sha256()
    h.update(W._git(path, "diff", "HEAD", check=False).encode())
    for status, name in W._changed(path):
        if status.strip() == "??" and (path / name).is_file():
            h.update(name.encode())
            h.update((path / name).read_bytes())
    return h.hexdigest()[:16]


def _suites(kind: str, path: Path, files: list[str]) -> list[tuple[str, list[str], Path]]:
    """(label, argv, cwd) for the test suites *files* call for."""
    out: list[tuple[str, list[str], Path]] = []
    if kind == "stack":
        if any(f.startswith("services/nanobot/") for f in files):
            out.append(("nanobot", [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                                    "tests"], path / "services" / "nanobot"))
        if any(f.startswith("services/home-core/") for f in files):
            for t in sorted((path / "services" / "home-core" / "local").glob("test_*.py")):
                out.append((f"home-core {t.name}", [PY, t.name], t.parent))
        if any(f.startswith(("deploy/", "config/")) for f in files):
            for t in sorted((path / "deploy").glob("test_*.py")):
                out.append((f"deploy {t.name}", [PY, str(t)], path))
        if any(f.startswith("admin/") for f in files):
            for t in sorted((path / "admin").glob("test_*.py")):
                out.append((f"admin {t.name}", [PY, str(t)], path))
        if any(f.startswith("i18n/") for f in files):
            out.append(("i18n", [PY, "i18n/check.py"], path))
        return out
    # A plugin says how it is tested (`tests:` in plugin.yml, a command run from
    # its root); otherwise the test scripts beside what changed.
    try:
        import yaml  # noqa: PLC0415
        doc = yaml.safe_load((path / "plugin.yml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        doc = {}
    if isinstance(doc.get("tests"), str) and doc["tests"].strip():
        return [("plugin tests", ["sh", "-c", doc["tests"]], path)]
    dirs = sorted({(path / f).parent for f in files})
    for d in dirs:
        for t in sorted(d.glob("test_*.py")):
            out.append((f"{t.relative_to(path)}", [PY, t.name], d))
    return out


def run_tests(d: Path, repos: list[dict], rid: str, name: str, run=subprocess.run) -> dict:
    repo = W._repo(repos, name)
    path = W.worktree(d, rid, name)
    if not path.exists():
        raise W.WorkError(f"no worktree for #{rid} in {name}")
    files = changed_files(path, repo.get("branch") or "")
    suites = _suites(repo["kind"], path, files)
    results = []
    for label, argv, cwd in suites:
        # A scratch state (suites default to the live one, and one has deleted
        # real accounts), and the worktree's own package first on the path.
        env = dict(os.environ, HOME_STACK_STATE_DIR=tempfile.mkdtemp(prefix="improve-test-"),
                   PYTHONPATH=str(path / "services" / "nanobot"))
        t0 = time.time()
        r = run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=1800, env=env)
        tail = [ln for ln in (r.stdout or "").splitlines() + (r.stderr or "").splitlines()
                if ln.strip()][-3:]
        results.append({"suite": label, "ok": r.returncode == 0,
                        "seconds": round(time.time() - t0), "tail": tail})
    record = {"digest": diff_digest(path), "files": files, "suites": results,
              "ok": all(x["ok"] for x in results), "at": int(time.time())}
    _save(d, rid, name, "tests", record)
    return record


def touches_behaviour(kind: str, files: list[str]) -> bool:
    return kind == "stack" and any(f.startswith(BEHAVIOUR) for f in files)


def _record_path(d: Path, rid: str, name: str, what: str) -> Path:
    return d / "work" / f"{rid}-{name}.{what}.json"


def _save(d: Path, rid: str, name: str, what: str, record: dict) -> None:
    p = _record_path(d, rid, name, what)
    p.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _load(d: Path, rid: str, name: str, what: str) -> dict | None:
    try:
        return json.loads(_record_path(d, rid, name, what).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _bench_once(container: str, model: str, roles: str, pkg: str | None, run) -> dict:
    """One benchmark run in *container*, on its own code or -- with *pkg* -- on
    the nanobot package copied in there. Returns the run's summary."""
    run_id = f"improve-{os.getpid()}-{int(time.time() * 1000)}"
    work = f"/tmp/{run_id}-bench"
    # The cases and the benchmark from this checkout: the answer key never
    # comes from the worktree, and the benchmark deletes its copy once read.
    run(["docker", "cp", str(ROOT / "services" / "nanobot" / "bench") + "/.",
         f"{container}:{work}"], capture_output=True, text=True, timeout=120, check=True)
    out = f"/tmp/{run_id}.json"
    argv = ["docker", "exec"]
    if pkg:
        argv += ["-e", f"PYTHONPATH={pkg}"]
    argv += [container, "python3", f"{work}/model_bench.py", "--model", model,
             "--roles", roles, "--out", out]
    r = run(argv, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise W.WorkError(f"the benchmark failed: {(r.stderr or r.stdout).strip()[-300:]}")
    got = run(["docker", "exec", container, "cat", out], capture_output=True, text=True,
              timeout=60)
    run(["docker", "exec", container, "rm", "-rf", work, out], capture_output=True, timeout=60)
    doc = json.loads(got.stdout)
    failing = sorted({c.get("id", "?") for c in doc.get("cases") or [] if not c.get("passed")})
    return {"summary": doc.get("summary") or {}, "failing": failing}


def run_bench(d: Path, repos: list[dict], rid: str, name: str, container: str, model: str,
              roles: str = DEFAULT_ROLES, repeat: int = 1, run=subprocess.run,
              busy=None) -> dict:
    """The benchmark's *roles*, *repeat* times, on the running code and on the
    worktree's. Only this stack's assistant has a benchmark."""
    repo = W._repo(repos, name)
    if repo["kind"] != "stack":
        raise W.WorkError(f"{name} has no benchmark; its tests are what judge it")
    path = W.worktree(d, rid, name)
    if not path.exists():
        raise W.WorkError(f"no worktree for #{rid} in {name}")
    if busy and busy():
        raise W.WorkError("a benchmark or a deploy is running; try again when it has finished")
    if not re.fullmatch(r"[a-z0-9,]+", roles):
        raise W.WorkError(f"not a list of roles: {roles!r}")
    digest = diff_digest(path)
    # The worktree's package, copied in beside the benchmark: the container's
    # own is left alone, and PYTHONPATH puts this one first for this run only.
    pkg_dir = f"/tmp/improve-{rid}-{digest}"
    run(["docker", "exec", container, "rm", "-rf", pkg_dir], capture_output=True, timeout=60)
    run(["docker", "exec", container, "mkdir", "-p", pkg_dir], capture_output=True, timeout=60)
    run(["docker", "cp", str(path / "services" / "nanobot" / "nanobot"), f"{container}:{pkg_dir}/"],
        capture_output=True, text=True, timeout=300, check=True)
    runs = {"before": [], "after": []}
    try:
        for _ in range(max(1, repeat)):
            runs["before"].append(_bench_once(container, model, roles, None, run))
            runs["after"].append(_bench_once(container, model, roles, pkg_dir, run))
    finally:
        run(["docker", "exec", container, "rm", "-rf", pkg_dir], capture_output=True, timeout=60)
    verdict = compare(runs, roles)
    record = {"digest": digest, "roles": roles, "repeat": repeat, "model": model,
              "container": container, "runs": runs, **verdict, "at": int(time.time())}
    _save(d, rid, name, "bench", record)
    return record


def compare(runs: dict, roles: str) -> dict:
    """Per role, the best of each side's runs, and whether *after* fell more
    than the noise below *before*."""
    table, worse = {}, []
    for role in roles.split(","):
        b = [r["summary"].get(role, {}).get("passed", 0) for r in runs["before"]]
        a = [r["summary"].get(role, {}).get("passed", 0) for r in runs["after"]]
        total = max([r["summary"].get(role, {}).get("total", 0)
                     for r in runs["before"] + runs["after"]] or [0])
        table[role] = {"before": max(b or [0]), "after": max(a or [0]), "total": total}
        if max(a or [0]) < max(b or [0]) - NOISE:
            worse.append(role)
    return {"table": table, "worse": worse, "ok": not worse}


def gate(d: Path, repo: dict, rid: str, path: Path) -> list[str]:
    """Why a commit of the worktree as it stands is refused, or []."""
    why = []
    digest = diff_digest(path)
    files = changed_files(path, repo.get("branch") or "")
    tests = _load(d, rid, repo["name"], "tests")
    if not tests or tests.get("digest") != digest:
        why.append(f"no test run for this exact change: improve test {rid} {repo['name']}")
    elif not tests.get("ok"):
        bad = ", ".join(s["suite"] for s in tests["suites"] if not s["ok"])
        why.append(f"tests fail: {bad}")
    if touches_behaviour(repo["kind"], files):
        bench = _load(d, rid, repo["name"], "bench")
        if not bench or bench.get("digest") != digest:
            why.append(f"it changes how the assistant behaves, and there is no benchmark run "
                       f"for this exact change: improve bench {rid} {repo['name']}")
        elif not bench.get("ok"):
            why.append("the benchmark got worse: " + ", ".join(
                f"{r} {bench['table'][r]['before']}->{bench['table'][r]['after']}"
                f"/{bench['table'][r]['total']}" for r in bench["worse"]))
    return why

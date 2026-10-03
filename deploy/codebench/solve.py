#!/usr/bin/env python3
"""One attempt at one coding problem, inside the benchmark's sandbox container.

    docker run -i ... home-stack-codebench < job.json

stdin is the job, read whole before anything else runs:

    {"problem": "cpp-lru", "task": "...", "check": "make -s test",
     "hidden": "<command>", "hidden_files": {"tests/x.cpp": "..."},
     "protected": ["tests/", "Makefile"], "base_url": "http://server:8080/v1",
     "model": "bench", "context": 32768, "timeout": 900}

The problem's starting files are in the image (/bench/problems/<id>/start);
the hidden tests are only ever in this process's memory until grading, after
opencode has exited -- the model can read the whole filesystem.

stdout is one JSON object per line, for admin/codebench.py to relay:

    {"ev": "step", ...}     as the model works: a tool call, its text, a turn
    {"ev": "result", ...}   once, at the end: the grade and everything measured

Grading: the protected paths (the tests, the Makefile) are put back as they
were, the build directory is removed, then the visible check runs, then the
hidden tests are added and their command runs. A pass is both green with the
protected paths untouched. Editing the tests is reported, and graded against
the real ones rather than the model's.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PROBLEMS = Path(os.environ.get("CODEBENCH_PROBLEMS", "/bench/problems"))
WORK = Path(os.environ.get("CODEBENCH_WORK", "/home/bench/work"))
OPENCODE = os.environ.get("CODEBENCH_OPENCODE", "opencode")
CHECK_TIMEOUT = 300
MAX_STEPS = 400
CLIP = 600
DIFF_CLIP = 40_000


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def clip(text, n: int = CLIP) -> str:
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return text if len(text) <= n else text[: n - 1] + "…"


def git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=check).stdout


def make_repo(start: Path, repo: Path) -> None:
    shutil.copytree(start, repo)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=bench@example.org", "-c", "user.name=bench", "commit", "-qm", "start")


def opencode_config(base_url: str, model: str, context: int) -> dict:
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"bench": {"npm": "@ai-sdk/openai-compatible", "name": "bench",
                               "options": {"baseURL": base_url},
                               "models": {model: {"name": model, "tool_call": True,
                                                  "limit": {"context": context, "output": 8192}}}}},
        # Nothing outside the repository, and nothing from the web: the
        # answer has to come from the model.
        "permission": {"external_directory": {"*": "deny"}, "webfetch": "deny"},
        "autoupdate": False,
        "share": "disabled",
        "mcp": {},
    }


# -- reading opencode's events -------------------------------------------------
class Events:
    """opencode's `--format json` stream, folded into steps and totals.

    Read defensively: an event this does not know is counted, not fatal, so a
    newer opencode degrades the report rather than the run."""

    def __init__(self, t0: float):
        self.t0 = t0
        self.steps: list[dict] = []
        self.turns = 0
        self.tools = 0
        self.tool_errors = 0
        self.tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
        self.errors: list[str] = []
        self.unknown: dict[str, int] = {}
        self.final_text = ""
        self.stopped = False

    def feed(self, line: str) -> dict | None:
        """One line of output; the step it made, if any."""
        try:
            ev = json.loads(line)
        except ValueError:
            return None
        if not isinstance(ev, dict):
            return None
        kind = str(ev.get("type") or "")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        at = round(time.time() - self.t0, 1)
        step = None
        if kind == "step_start":
            self.turns += 1
        elif kind == "step_finish":
            tok = part.get("tokens") or {}
            for k in ("input", "output", "reasoning"):
                self.tokens[k] += int(tok.get(k) or 0)
            self.tokens["cache_read"] += int((tok.get("cache") or {}).get("read") or 0)
        elif kind == "tool_use" or part.get("type") == "tool":
            state = part.get("state") or {}
            status = str(state.get("status") or "")
            self.tools += 1
            if status == "error":
                self.tool_errors += 1
            timing = state.get("time") or {}
            took = None
            if timing.get("start") and timing.get("end"):
                took = round((timing["end"] - timing["start"]) / 1000, 1)
            step = {"at": at, "kind": "tool", "tool": str(part.get("tool") or "?"),
                    "title": clip(state.get("title") or "", 200), "status": status,
                    "input": clip(state.get("input") or ""),
                    "output": clip(state.get("output") or state.get("error") or ""), "seconds": took}
        elif kind == "text":
            text = str(part.get("text") or "").strip()
            if text:
                self.final_text = text
                step = {"at": at, "kind": "text", "text": clip(text, 1500)}
        elif kind == "reasoning":
            text = str(part.get("text") or "").strip()
            if text:
                step = {"at": at, "kind": "thinking", "text": clip(text, 800)}
        elif kind == "error":
            err = ev.get("error") or part
            msg = clip((err.get("data") or {}).get("message") or err.get("name") or err
                       if isinstance(err, dict) else err, 400)
            self.errors.append(msg)
            step = {"at": at, "kind": "error", "text": msg}
        else:
            self.unknown[kind] = self.unknown.get(kind, 0) + 1
        if step and len(self.steps) < MAX_STEPS:
            self.steps.append(step)
        return step


# -- grading -------------------------------------------------------------------
def run_check(repo: Path, command: str) -> dict:
    t0 = time.time()
    try:
        r = subprocess.run(["bash", "-c", command], cwd=repo, capture_output=True, text=True,
                           timeout=CHECK_TIMEOUT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        ok, out = r.returncode == 0, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired as exc:
        ok, out = False, f"timed out after {CHECK_TIMEOUT}s\n" + str(exc.stdout or "")[-2000:]
    return {"ok": ok, "seconds": round(time.time() - t0, 1), "output": out[-3000:]}


def under(path: str, protected: list[str]) -> bool:
    return any(path == p.rstrip("/") or (p.endswith("/") and path.startswith(p)) for p in protected)


def grade(repo: Path, job: dict) -> dict:
    protected = list(job.get("protected") or [])
    git(repo, "add", "-A")
    changed = [p for p in git(repo, "diff", "--cached", "--name-only", "HEAD").splitlines() if p]
    stat = git(repo, "diff", "--cached", "--stat", "HEAD")
    diff = git(repo, "diff", "--cached", "HEAD")
    touched = [p for p in changed if under(p, protected)]
    # The real tests, whatever the model did to them.
    if protected:
        git(repo, "reset", "-q", "HEAD", "--", *protected, check=False)
        git(repo, "checkout", "-q", "HEAD", "--", *protected, check=False)
        git(repo, "clean", "-qfd", "--", *protected, check=False)
    shutil.rmtree(repo / "build", ignore_errors=True)
    visible = run_check(repo, job["check"])
    for rel, text in (job.get("hidden_files") or {}).items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    hidden = run_check(repo, job["hidden"]) if job.get("hidden") else {"ok": True, "seconds": 0, "output": ""}
    return {"pass": visible["ok"] and hidden["ok"] and not touched,
            "visible": visible, "hidden": hidden, "tests_untouched": not touched, "touched": touched,
            "changed": changed, "diff_stat": stat.strip()[-2000:], "diff": diff[:DIFF_CLIP],
            "diff_clipped": len(diff) > DIFF_CLIP}


# -- the attempt ---------------------------------------------------------------
def attempt(job: dict) -> dict:
    pid = job["problem"]
    start = PROBLEMS / pid / "start"
    repo = WORK / pid
    shutil.rmtree(repo, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    make_repo(start, repo)
    cfg = Path.home() / ".config" / "opencode-bench.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(opencode_config(job["base_url"], job["model"], int(job["context"]))))
    env = {**os.environ, "OPENCODE_CONFIG": str(cfg), "OPENCODE_DISABLE_AUTOUPDATE": "1",
           "OPENCODE_DISABLE_LSP_DOWNLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    log = WORK / "opencode.log"
    t0 = time.time()
    events = Events(t0)
    emit({"ev": "started", "problem": pid})
    timed_out = False
    with open(log, "w") as err:
        proc = subprocess.Popen(
            [OPENCODE, "run", "--pure", "--thinking", "--dir", str(repo), "-m", f"bench/{job['model']}",
             "--format", "json", "--print-logs", "--log-level", "WARN", "--auto", job["task"]],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, text=True,
            env=env, start_new_session=True)
        deadline = t0 + int(job.get("timeout") or 900)

        def kill(*_):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        # A stop from the admin page is a SIGTERM to this process (docker
        # stop): end opencode and still report what happened so far.
        def stop(*_):
            events.stopped = True
            kill()

        signal.signal(signal.SIGTERM, stop)
        import selectors
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        while True:
            if time.time() > deadline:
                timed_out = True
                kill()
                break
            if not sel.select(timeout=1):
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            step = events.feed(line)
            if step:
                emit({"ev": "step", **step})
        proc.wait()
    seconds = round(time.time() - t0, 1)
    try:
        log_tail = log.read_text(errors="replace")[-3000:]
    except OSError:
        log_tail = ""
    graded = grade(repo, job)
    return {"ev": "result", "problem": pid, **graded, "seconds": seconds, "timed_out": timed_out,
            "stopped": events.stopped, "exit_code": proc.returncode,
            "turns": events.turns, "tool_calls": events.tools, "tool_errors": events.tool_errors,
            "tokens": events.tokens, "errors": events.errors[:5], "unknown_events": events.unknown,
            "final_text": clip(events.final_text, 3000), "steps": events.steps,
            "log_tail": log_tail if not graded["pass"] else ""}


def main() -> int:
    job = json.loads(sys.stdin.read())
    try:
        emit(attempt(job))
    except Exception as exc:                                   # noqa: BLE001 -- reported, not raised
        emit({"ev": "result", "problem": job.get("problem"), "pass": False,
              "error": f"{type(exc).__name__}: {exc}"[:500]})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

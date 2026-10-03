#!/usr/bin/env python3
"""The coding benchmark's problems can tell a right answer from a wrong one.

For every problem: as given, it fails; with its reference solution, the
visible tests *and* the hidden ones pass; a half answer that only satisfies
the visible tests is caught by the hidden ones where the problem says it
should be; and an answer that edits the tests is graded against the real ones
and reported. Also the reading of opencode's event stream.

    ./.venv/bin/python deploy/codebench/test_codebench.py

Needs g++, make and pytest on the PATH -- the sandbox image has them; on the
host, run it with the checkout's venv. Touches nothing outside a temp dir.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]

import solve  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail=None) -> None:
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}    {label}" + ("" if ok or detail is None else f"  <- {detail}"))
    failures += 0 if ok else 1


def job_for(pid: str) -> dict:
    d = HERE / "problems" / pid
    p = json.loads((d / "problem.json").read_text())
    hidden = {str(f.relative_to(d / "hidden")): f.read_text() for f in (d / "hidden").rglob("*") if f.is_file()}
    return {"problem": pid, "check": p["check"], "hidden": p["hidden"], "hidden_files": hidden,
            "protected": p["protected"]}


def repo_with(pid: str, tmp: Path, overlay: dict[str, str] | None = None, solution: bool = False) -> Path:
    repo = tmp / pid
    shutil.rmtree(repo, ignore_errors=True)
    solve.make_repo(HERE / "problems" / pid / "start", repo)
    if solution:
        sol = HERE / "problems" / pid / "solution"
        for f in sol.rglob("*"):
            if f.is_file():
                dest = repo / f.relative_to(sol)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(f.read_bytes())
    for rel, text in (overlay or {}).items():
        (repo / rel).write_text(text)
    return repo


# A half answer per problem: what passes the visible tests and misses a rule
# the specification states. The hidden tests exist for these.
HALF = {
    # get() refreshes; put() of an existing key still duplicates it.
    "cpp-lru": ("lru.hpp", lambda s: s.replace(
        "        return it->second->second;\n    }",
        "        items_.splice(items_.begin(), items_, it->second);\n        return it->second->second;\n    }")
        .replace("    void put(const K& key, V value) {\n",
                 "    void put(const K& key, V value) {\n        auto old = index_.find(key);\n"
                 "        if (old != index_.end()) { old->second->second = std::move(value); return; }\n")),
    # Slots: the docstring's strict two-digit parse is the rule left out.
    "py-slots": ("slots/times.py", lambda s: s.replace(
        '_HHMM = re.compile(r"([01]\\d|2[0-3]):([0-5]\\d)")', '_HHMM = re.compile(r"(\\d{1,2}):(\\d{2})")')
        .replace("    return int(m.group(1)) * 60", "    if int(m.group(1)) > 23 or int(m.group(2)) > 59:\n"
                 "        raise ValueError(text)\n    return int(m.group(1)) * 60")),
}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="codebench-test-"))
    try:
        problems = sorted(p.name for p in (HERE / "problems").iterdir() if (p / "problem.json").is_file())
        print("the problems")
        check("five problems, at least two in C++", len(problems) == 5 and sum(
            json.loads((HERE / "problems" / p / "problem.json").read_text())["language"] == "C++"
            for p in problems) >= 2, problems)
        for pid in problems:
            print(f"\n{pid}")
            meta = json.loads((HERE / "problems" / pid / "problem.json").read_text())
            check("says what it is and what to do",
                  all(meta.get(k) for k in ("title", "language", "kind", "about", "task", "check", "hidden"))
                  and meta["kind"] in ("fix", "implement"), meta)
            job = job_for(pid)
            g = solve.grade(repo_with(pid, tmp), job)
            check("as given, the visible tests fail", not g["visible"]["ok"] and not g["pass"],
                  g["visible"]["output"][-300:])
            check("and so do the hidden ones", not g["hidden"]["ok"], g["hidden"]["output"][-300:])
            g = solve.grade(repo_with(pid, tmp, solution=True), job)
            check("the reference solution passes both", g["pass"] and g["tests_untouched"],
                  (g["visible"]["output"][-400:], g["hidden"]["output"][-400:]))
            check("and its change is in the diff", bool(g["changed"]) and "build/" not in "".join(g["changed"]),
                  g["changed"])
            if pid in HALF:
                rel, edit = HALF[pid]
                src = (HERE / "problems" / pid / "solution" / rel).read_text()
                if pid == "cpp-lru":
                    src = (HERE / "problems" / pid / "start" / rel).read_text()
                g = solve.grade(repo_with(pid, tmp, {rel: edit(src)}, solution=pid != "cpp-lru"), job)
                check("a half answer passes the visible tests and the hidden ones catch it",
                      g["visible"]["ok"] and not g["hidden"]["ok"] and not g["pass"],
                      (g["visible"]["output"][-300:], g["hidden"]["output"][-300:]))
            # Emptying a visible test is reported, and graded against the real one.
            test_file = next(f for f in sorted((HERE / "problems" / pid / "start" / "tests").iterdir())
                             if f.name.startswith("test_"))
            rel = f"tests/{test_file.name}"
            blank = "int main() { return 0; }\n" if rel.endswith(".cpp") else "def test_nothing():\n    pass\n"
            g = solve.grade(repo_with(pid, tmp, {rel: blank}), job)
            check("an answer that guts the tests is caught, and graded on the real ones",
                  not g["pass"] and g["touched"] == [rel] and not g["visible"]["ok"], (g["touched"], g["pass"]))

        print("\nopencode's events")
        ev = solve.Events(0)
        lines = [
            {"type": "step_start", "part": {}},
            {"type": "tool_use", "part": {"type": "tool", "tool": "bash", "state": {
                "status": "completed", "input": {"command": "make test"}, "output": "1 check(s) failed",
                "title": "make test", "time": {"start": 1000, "end": 3500}}}},
            {"type": "step_finish", "part": {"tokens": {"input": 9000, "output": 120, "reasoning": 40,
                                                        "cache": {"read": 8000, "write": 0}}}},
            {"type": "step_start", "part": {}},
            {"type": "tool_use", "part": {"type": "tool", "tool": "edit", "state": {
                "status": "error", "input": {"filePath": "lru.hpp"}, "error": "oldString not found"}}},
            {"type": "text", "part": {"text": "Fixed the cache."}},
            {"type": "step_finish", "part": {"tokens": {"input": 9500, "output": 80}}},
            {"type": "something_new", "part": {}},
        ]
        steps = [ev.feed(json.dumps(x)) for x in lines] + [ev.feed("not json"), ev.feed("[1]")]
        check("each tool call is a step, with what it ran and how long it took",
              steps[1]["tool"] == "bash" and steps[1]["seconds"] == 2.5 and "make test" in steps[1]["input"], steps[1])
        check("a failed tool call is counted and keeps its error",
              ev.tool_errors == 1 and "oldString" in steps[4]["output"], steps[4])
        check("turns and tokens add up", ev.turns == 2 and ev.tokens == {
            "input": 18500, "output": 200, "reasoning": 40, "cache_read": 8000}, (ev.turns, ev.tokens))
        check("the last thing it said is kept", ev.final_text == "Fixed the cache.")
        cut = solve.Events(0).feed(json.dumps({"type": "step_finish", "part": {"reason": "length", "tokens": {"output": 16384}}}))
        check("a reply cut off at the output limit is a step that says so",
              cut and cut["kind"] == "error" and "output limit" in cut["text"], cut)
        check("an unknown event or a stray line is counted, not fatal",
              ev.unknown == {"something_new": 1} and steps[-2:] == [None, None], ev.unknown)
        print("\nwhat the sandbox is given")
        cfg = solve.opencode_config("http://codebench-server:8080/v1", "bench", 32768)
        check("a reply may be as long as the Programmer allows its local model",
              cfg["provider"]["bench"]["models"]["bench"]["limit"]["output"] == 16384)
        check("opencode may not fetch the web or leave the repository",
              cfg["permission"]["webfetch"] == "deny" and cfg["permission"]["external_directory"] == {"*": "deny"})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nall checks passed" if not failures else f"\n{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

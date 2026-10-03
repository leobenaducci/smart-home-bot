"""The coding benchmark's admin side, without docker or a card.

Run: ./.venv/bin/python admin/test_codebench.py

What a queue entry may hold (every value ends up on a docker command line),
what the sandbox image is built from (the hidden tests and the solutions must
never be in it: the model can read the whole filesystem), how the server's log
is read, and the queue's own bookkeeping. Touches a temp directory only.
"""
import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import codebench as C  # noqa: E402

ROOT = HERE.parent / "deploy" / "codebench"
failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  <- {detail}" if not cond and detail != "" else ""))
    if not cond:
        failures.append(label)


print("the problems")
probs = C.problems(ROOT)
known = {p["id"] for p in probs}
check("five, each with a title and a task, none with its hidden tests",
      len(probs) == 5 and all(p["title"] and p["task"] for p in probs)
      and not any("hidden" in p for p in probs), probs)
job = C.job_for(ROOT, "cpp-lru", "http://codebench-server:8080/v1", "bench", 32768)
check("a job carries the hidden tests, for stdin only",
      "tests/test_lru_hidden.cpp" in job["hidden_files"] and job["protected"] == ["tests/", "Makefile"])

print("\nthe sandbox image")
with tempfile.TemporaryDirectory() as tmp:
    tag = C.stage_image(ROOT, pathlib.Path(tmp))
    files = sorted(str(f.relative_to(tmp)) for f in pathlib.Path(tmp).rglob("*") if f.is_file())
    check("holds the Dockerfile, solve.py and each problem's starting files",
          "Dockerfile" in files and "solve.py" in files and "problems/cpp-lru/start/lru.hpp" in files)
    check("and never a hidden test, a solution or the problem's grading command",
          not [f for f in files if "/hidden/" in f or "/solution/" in f or f.endswith("problem.json")],
          [f for f in files if "/hidden/" in f or "/solution/" in f])
    with tempfile.TemporaryDirectory() as tmp2:
        check("its tag is a hash of what is in it: the same files, the same tag",
              C.stage_image(ROOT, pathlib.Path(tmp2)) == tag and tag.startswith(C.IMAGE_REPO + ":"))

print("\nwhat a run may ask for")
ok = C.check_entry({"model": "qwen3.5:9b", "engine": "llamacpp", "context": "32768",
                    "problems": ["py-slots", "cpp-lru"]}, known)
check("a model, an engine, a window and problems, in the problems' own order",
      ok == {"model": "qwen3.5:9b", "engine": "llamacpp", "context": 32768,
             "problems": ["cpp-lru", "py-slots"], "interrupt": False}, ok)
check("a 256k window may be asked for, as the Programmer's own runs at",
      C.check_entry({"model": "qwen3.5:9b", "engine": "llamacpp", "context": "262144",
                     "problems": ["cpp-lru"]}, known)["context"] == 262144)
for bad, why in [
    ({"model": "qwen; rm -rf /", "engine": "ollama", "context": 32768, "problems": ["cpp-lru"]}, "a shell in the name"),
    ({"model": "/etc/../x.gguf", "engine": "llamacpp", "context": 32768, "problems": ["cpp-lru"]}, "a path out"),
    ({"model": "qwen3.5:9b", "engine": "vllm", "context": 32768, "problems": ["cpp-lru"]}, "an engine it has not"),
    ({"model": "qwen3.5:9b", "engine": "ollama", "context": 1000, "problems": ["cpp-lru"]}, "a window not offered"),
    ({"model": "qwen3.5:9b", "engine": "ollama", "context": 32768, "problems": []}, "no problem"),
    ({"model": "qwen3.5:9b", "engine": "ollama", "context": 32768, "problems": ["nope"]}, "a problem that is not"),
    ({"model": "hf:a/b/c.gguf", "engine": "ollama", "context": 32768, "problems": ["cpp-lru"]}, "a GGUF on Ollama"),
]:
    try:
        C.check_entry(bad, known)
        check(f"refused: {why}", False)
    except C.BenchError:
        check(f"refused: {why}", True)

print("\nthe server's log")
log = """load_tensors: offloaded 33/33 layers to GPU
prompt eval time =    1200.00 ms /  9000 tokens (    0.13 ms per token,  7500.00 tokens per second)
       eval time =    2000.00 ms /    80 tokens (   25.00 ms per token,    40.00 tokens per second)
prompt eval time =    300.00 ms /  1000 tokens (    0.30 ms per token,  3333.33 tokens per second)
       eval time =    1000.00 ms /    44 tokens (   22.73 ms per token,    44.00 tokens per second)
prompt eval time =    300.00 ms /  1000 tokens (    0.30 ms per token,  3000.00 tokens per second)
       eval time =    1000.00 ms /    44 tokens (   22.73 ms per token,    42.00 tokens per second)
"""
f = C.server_facts(log)
check("layers on the card, and median speeds", f == {"layers": "33/33", "all_on_gpu": True,
                                                     "prompt_tok_s": 3333.3, "gen_tok_s": 42.0}, f)
f = C.server_facts(
    "0.27.058.863 I slot print_timing: id  0 | task 57 | prompt eval time =     4551.30 ms /  7110 tokens "
    "(    0.64 ms per token,  1562.19 tokens per second)\n"
    "0.27.058.876 I slot print_timing: id  0 | task 57 |        eval time =    1222.46 ms /    62 tokens "
    "(   20.04 ms per token,    49.90 tokens per second)\n"
    "0.24.789.557 I slot print_timing: id  0 | task 57 | prompt processing, n_tokens =   6144, progress = 0.86, "
    "t =   3.50 s / 1753.28 tokens per second\n")
check("this build's lines, behind their slot prefix; progress lines are not totals",
      f["prompt_tok_s"] == 1562.2 and f["gen_tok_s"] == 49.9, f)
f = C.server_facts("load_tensors: offloaded 20/41 layers to GPU\n")
check("a model that did not fit says so", f["all_on_gpu"] is False and f["gen_tok_s"] is None, f)

_crash = """llama_kv_cache: size = 9216.00 MiB (131072 cells, 48 layers)
ggml_backend_cuda_buffer_type_alloc_buffer: allocating 9216.00 MiB on device 0: cudaMalloc failed: out of memory
llama_init_from_model: failed to initialize the context
/llama/bin/libllama.so.0(llama_decode+0xf)[0x7aa615f10a3f]
/llama/bin/libllama-common.so.0(_Z23common_init_from_paramsR13common_paramsb+0x43b)[0x7aa6165f68ab]
/lib/x86_64-linux-gnu/libc.so.6(__libc_start_main+0x8b)[0x7aa61699a28b]
"""
_why = C.load_failure(_crash)
check("a failed load says why, not only the backtrace after it",
      "cudaMalloc failed: out of memory" in _why and "failed to initialize" in _why
      and _why.count("libllama") <= 2, _why)

print("\nthe queue and the results")
with tempfile.TemporaryDirectory() as tmp:
    r = C.Runner(ROOT, pathlib.Path(tmp), gpu=lambda: 1, studio=lambda: ("", ""),
                 ollama_url=lambda: "", build_dir=lambda f: pathlib.Path("/x"), models_dir="/m",
                 server_image="img", docker="/bin/false")
    r.kick = lambda: None                      # no thread, no docker
    a = r.add(dict(ok), "999000111")
    b = r.add(dict(ok, model="ornith-1.5:9b"), "999000111")
    check("entries queue in order, each with an id", [e["id"] for e in r.queue()] == [a["id"], b["id"]]
          and C.ID_RE.fullmatch(a["id"]), r.queue())
    check("one can be taken out", r.remove(a["id"]) and [e["id"] for e in r.queue()] == [b["id"]])
    check("an idle status, with no thread", r.status().get("state") in (None, "idle"))
    run = {"id": "20261001-120000-abcdef", "model": "qwen3.5:9b", "engine": "llamacpp", "context": 32768,
           "state": "done", "load_seconds": 12.5, "problems": {
               "cpp-lru": {"pass": True, "seconds": 60, "turns": 6, "tool_calls": 9, "tool_errors": 0,
                           "visible": {"ok": True}, "hidden": {"ok": True}, "tokens": {"output": 900},
                           "steps": [{"kind": "tool"}], "diff": "x"},
               "py-slots": {"pass": False, "seconds": 300, "visible": {"ok": True}, "hidden": {"ok": False}}}}
    (pathlib.Path(tmp) / "results" / f"{run['id']}.json").write_text(json.dumps(run))
    rows = r.runs()
    check("a run's row: score, and per problem the verdict without the bulk",
          rows and rows[0]["passed"] == 1 and rows[0]["graded"] == 2
          and rows[0]["problems"]["py-slots"]["visible"] is True and rows[0]["problems"]["py-slots"]["hidden"] is False
          and "steps" not in rows[0]["problems"]["cpp-lru"], rows)
    check("the full run is there for the detail view", r.run_file(run["id"])["problems"]["cpp-lru"]["steps"])
    check("a run id is checked before it names a file", r.run_file("../../etc/passwd") is None
          and not r.delete_run("../x"))
    check("and a run can be deleted", r.delete_run(run["id"]) and r.run_file(run["id"]) is None)

print("\nafter a restart")
with tempfile.TemporaryDirectory() as tmp:
    calls = []
    r = C.Runner(ROOT, pathlib.Path(tmp), gpu=lambda: 1, studio=lambda: ("", ""),
                 ollama_url=lambda: "", build_dir=lambda f: pathlib.Path("/x"), models_dir="/m",
                 server_image="img")
    r._docker = lambda *a, **k: (calls.append(a), C.subprocess.CompletedProcess(a, 0, "codebench-server\n", ""))[1]
    r._docker_out = lambda *a: "codebench-server\ncodebench-run-x-cpp-lru\n"
    r.kick = lambda: None
    r.recover()
    check("a runner whose own state is idle removes nobody's containers",
          not [c for c in calls if c[:1] == ("rm",)], calls)
    r._status("running", run="x", studio_paused_by_us=False)
    r.recover()
    check("one whose own run was cut short removes what it left",
          ("rm", "-f", "codebench-server") in calls and ("rm", "-f", "codebench-run-x-cpp-lru") in calls, calls)
    check("and says so", r._read("status.json", {}).get("state") == "idle")

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all checks passed")

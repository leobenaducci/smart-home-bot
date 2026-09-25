#!/usr/bin/env python3
"""The benchmark card's pure logic: what a typed model becomes, and what may go.

Run: python3 test_bench.py   (from admin/). No Flask, no network: Hugging Face
is a stub that answers with a file list.
"""
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench as B  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        failures.append(label)


def repo(*names, sizes=None):
    files = [{"path": n, "size": (sizes or {}).get(n, 5_000_000_000)} for n in names]
    return lambda path: files


GGUF = repo("README.md", "LFM2.5-8B-Q4_K_M.gguf", "LFM2.5-8B-Q8_0.gguf",
            "LFM2.5-8B-IQ4_XS.gguf", "mmproj-LFM2.5-8B-f16.gguf")

print("a model the pickers would write passes through untouched")
for text in ("ollama:ornith-1.5:9b", "together:Qwen/Qwen3.8-Flash", "deepseek-v4-flash",
             "ollama-vision:qwen3-vl:4b"):
    check(f"  {text}", B.resolve_model(text, GGUF) == (text, ""), B.resolve_model(text, GGUF))

print("\na Hugging Face link, however it was pasted, becomes the name Ollama pulls")
for text in ("https://huggingface.co/LiquidAI/LFM2.5-8B-GGUF",
             "huggingface.co/LiquidAI/LFM2.5-8B-GGUF",
             "hf.co/LiquidAI/LFM2.5-8B-GGUF",
             "ollama:hf.co/LiquidAI/LFM2.5-8B-GGUF",
             "https://huggingface.co/LiquidAI/LFM2.5-8B-GGUF/tree/main"):
    model, _ = B.resolve_model(text, GGUF)
    check(f"  {text}", model == "ollama:hf.co/LiquidAI/LFM2.5-8B-GGUF:Q4_K_M", model)

model, note = B.resolve_model("hf.co/LiquidAI/LFM2.5-8B-GGUF:q8_0", GGUF)
check("a named quant is kept (and case does not matter)",
      model == "ollama:hf.co/LiquidAI/LFM2.5-8B-GGUF:Q8_0", model)
check("the vision projector is not mistaken for a quant", "F16" not in note, note)
check("with none named, the note lists what else the repo offers",
      "IQ4_XS" in B.resolve_model("hf.co/x/y", GGUF)[1])

print("\nwhat cannot run on Ollama is refused before a pull, with the reason")
for label, fetch, want in (
    ("a safetensors / NVFP4 repo", repo("model-00001-of-00005.safetensors", "config.json"), "no GGUF"),
    ("a quant the repo does not have", GGUF, "has no Q2_K"),
):
    try:
        B.resolve_model("hf.co/nvidia/Qwen3.8-27B-NVFP4" + (":Q2_K" if "quant" in label else ""), fetch)
        check(f"  {label}", False, "no error")
    except B.ResolveError as exc:
        check(f"  {label}", want in str(exc), exc)


def gated(path):
    raise urllib.error.HTTPError(path, 401, "unauthorized", {}, None)


try:
    B.resolve_model("hf.co/meta-llama/Secret-GGUF", gated)
    check("  a gated repo", False, "no error")
except B.ResolveError as exc:
    check("  a gated repo says so", "gated" in str(exc), exc)
try:
    B.resolve_model("rm -rf /", GGUF)
    check("  something that is not a model name", False, "no error")
except B.ResolveError:
    check("  something that is not a model name is refused", True)

print("\nsplit files are one quant, summed")
split = repo("M-Q4_K_M-00001-of-00002.gguf", "M-Q4_K_M-00002-of-00002.gguf",
             sizes={"M-Q4_K_M-00001-of-00002.gguf": 3_000_000_000,
                    "M-Q4_K_M-00002-of-00002.gguf": 2_000_000_000})
check("  two halves read as one Q4_K_M of 5 GB", B.gguf_quants("x/y", split) == {"Q4_K_M": 5_000_000_000},
      B.gguf_quants("x/y", split))

print("\ntwo different files at one quant are not one file")
variants = repo("Q-9B-Q4_K_M.gguf", "Q-9B-MTP-Q4_K_M.gguf", "Q-9B-MTP-Q8_0.gguf",
                sizes={"Q-9B-Q4_K_M.gguf": 5_630_000_000, "Q-9B-MTP-Q4_K_M.gguf": 5_890_000_000,
                       "Q-9B-MTP-Q8_0.gguf": 9_790_000_000})
check("  Qwythos' plain + MTP builds: the plain one's size, not the sum",
      B.gguf_quants("x/y", variants)["Q4_K_M"] == 5_630_000_000, B.gguf_quants("x/y", variants))

print("\nonly a model nothing uses may be removed")
cfg = {"assistant": {"models": {"everyday": "ollama:ornith-1.5:9b",
                                "fallback": ["ollama:ornith-1.5:9b", "together:Q"],
                                "documents": "ollama-vision:minicpm-v:8b"}}}
check("  a benchmark-only pull is removable", B.removable(cfg, "ollama:lfm2.5:8b"))
check("  the everyday model is not", not B.removable(cfg, "ollama:ornith-1.5:9b"))
check("  nor one named only inside a fallback chain",
      not B.removable({"assistant": {"models": {"fallback": ["ollama:x:1"]}}}, "ollama:x:1"))
check("  nor Paperless's model on the vision instance", not B.removable(cfg, "ollama-vision:minicpm-v:8b"))
check("  nor anything hosted", not B.removable(cfg, "together:Qwen/X"))

print("\nthe card is ordered by score, best first")
_v = [{"pct": 60, "passed": 15, "started": "2026-09-10 21:45", "file": "a"},
      {"pct": 84, "passed": 21, "started": "2026-09-10 21:13", "file": "b"},
      {"pct": 84, "passed": 21, "started": "2026-09-10 22:00", "file": "c"},
      {"pct": 76, "passed": 19, "started": "2026-09-10 20:33", "file": "d"}]
check("  highest score first, newest among equals",
      [v["file"] for v in B.sort_runs(_v)] == ["c", "b", "d", "a"], [v["file"] for v in B.sort_runs(_v)])

print("\neach kind has its own best, and a kind nobody measured is not a zero")
def _kv(file, roles, secs=1.0):
    return {"file": file, "pct": 0, "passed": sum(p for p, _ in roles.values()),
            "started": "", "roles": [{"role": r, "passed": p, "total": t} for r, (p, t) in roles.items()],
            "groups": [{"role": r, "cases": [{"seconds": secs}] * t} for r, (_, t) in roles.items()]}
_k = [_kv("fast", {"everyday": (4, 5), "notifications": (3, 3), "events": (4, 4)}, secs=1.0),
      _kv("slow", {"everyday": (5, 5), "notifications": (3, 3), "events": (4, 4)}, secs=9.0),
      _kv("long", {"longtask": (9, 10)})]
for v in _k:
    v["pct"] = 0
B.rank_kinds(_k)
_by = {v["file"]: v for v in _k}
check("  everyday: the one that passed more", "everyday" in _by["slow"]["best_for"], _by["slow"]["best_for"])
check("  background: a tie goes to the faster", "background" in _by["fast"]["best_for"], _by["fast"]["best_for"])
check("  background combines notifications, events and heartbeats",
      _by["fast"]["kinds"]["background"]["total"] == 7, _by["fast"]["kinds"]["background"])
check("  a model never run on long tasks has no long-task score",
      "longtask" not in _by["fast"]["kinds"] and "longtask" not in _by["fast"]["rank"])
check("  alone in a kind is not 'best' of it", "longtask" not in _by["long"]["best_for"])
check("  every role belongs to exactly one kind besides overall",
      sorted(B.KIND_OF_ROLE) == sorted(B.ROLES))

print("\nruns read back newest first, and a broken file is skipped")
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    (d / "20260910-100000-a.json").write_text(json.dumps({"model": "a"}))
    (d / "20260910-120000-b.json").write_text(json.dumps({"model": "b"}))
    (d / "20260910-110000-c.json").write_text("{not json")
    runs = B.load_results(d)
    check("  newest first", [r["model"] for r in runs] == ["b", "a"], [r.get("model") for r in runs])
    check("  each carries its file name for the buttons", runs[0]["file"] == "20260910-120000-b.json")

print("\none result per model: the last run replaces the earlier ones")
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    (d / "20260910-100000-a.json").write_text(json.dumps({"model": "a", "n": 1}))
    (d / "20260910-110000-a.json").write_text(json.dumps({"model": "a", "n": 2}))
    (d / "20260910-120000-b.json").write_text(json.dumps({"model": "b"}))
    (d / "20260910-130000-a.json").write_text(json.dumps({"model": "a", "n": 3}))
    runs = B.load_results(d)
    check("  the page shows the newest run of each model, even before any pruning",
          [(r["model"], r.get("n")) for r in runs] == [("a", 3), ("b", None)],
          [(r["model"], r.get("n")) for r in runs])
    check("  every file a model has, newest first",
          [p.name for p in B.results_for(d, "a")]
          == ["20260910-130000-a.json", "20260910-110000-a.json", "20260910-100000-a.json"])
    dropped = B.supersede(d, "a", "20260910-130000-a.json")
    check("  superseding drops the model's earlier files and names them",
          sorted(dropped) == ["20260910-100000-a.json", "20260910-110000-a.json"], dropped)
    check("  the kept run and the other model are untouched",
          sorted(q.name for q in d.glob("*.json")) == ["20260910-120000-b.json", "20260910-130000-a.json"],
          sorted(q.name for q in d.glob("*.json")))
    check("  superseding with nothing to drop is a no-op", B.supersede(d, "b", "20260910-120000-b.json") == [])

print("\nthe results card's view of a run")
run = {"model": "ollama:hf.co/empero-ai/Qwythos-9B-GGUF:Q4_K_M", "started": "2026-09-10T18:49:03",
       "seconds": 420, "repeat": 1,
       "summary": {"everyday": {"passed": 6, "total": 6}, "tools": {"passed": 3, "total": 7},
                   "all": {"passed": 9, "total": 13}},
       "cases": [{"role": "everyday", "id": "greet", "passed": True, "seconds": 2.0, "first_s": 1.0,
                  "llm_calls": 1, "calls": []},
                 {"role": "tools", "id": "light_on", "passed": False, "seconds": 9.0, "llm_calls": 3,
                  "calls": [{"label": "HassTurnOn", "ran": False}], "failures": ["x"]}]}
v = B.run_view(run)
check("  a Hugging Face model shows as owner/repo:quant, marked hf",
      (v["shown"], v["source"]) == ("empero-ai/Qwythos-9B-GGUF:Q4_K_M", "hf"), (v["shown"], v["source"]))
check("  roles it did not run are left out, grades follow the score",
      [(r["role"], r["grade"]) for r in v["roles"]] == [("everyday", "good"), ("tools", "bad")], v["roles"])
check("  total and percentage", (v["passed"], v["total"], v["pct"], v["grade"]) == (9, 13, 69, "part"))
check("  cases are grouped by role with readable labels",
      [(g["role"], g["cases"][0]["label"]) for g in v["groups"]] == [("everyday", "Greet"), ("tools", "Light on")])
check("  hosted and local names", B.display_name("together:Qwen/X") == ("Qwen/X", "hosted")
      and B.display_name("ollama:lfm2.5:8b") == ("lfm2.5:8b", "local")
      and B.display_name("deepseek-v4-flash") == ("deepseek-v4-flash", "hosted"))
check("  an empty run does not crash", B.run_view({})["total"] == 0)
run2 = dict(run, speed={"decode_tok_s": 51.1, "prefill_tok_s": 1473.8},
            memory={"vram_gb": 4.1, "size_gb": 5.9, "on_gpu_pct": 69, "context": 36864})
v2 = B.run_view(run2)
check("  speed and memory come from the probe when there is one",
      (v2["decode_tok_s"], v2["decode_measured"], v2["prefill_tok_s"], v2["vram_gb"], v2["on_gpu_pct"])
      == (51.1, True, 1473.8, 4.1, 69), v2)
v3 = B.run_view(dict(run, summary={**run["summary"], "all": {"passed": 9, "total": 13, "tok_s_median": 88.0}}))
check("  a hosted model falls back to the calls' own rate, marked as such",
      (v3["decode_tok_s"], v3["decode_measured"], v3["vram_gb"]) == (88.0, False, None), v3)
check("  a long name splits into owner, name and tag",
      B.name_parts("empero-ai/Qwythos-9B-GGUF:Q4_K_M") == {"owner": "empero-ai", "name": "Qwythos-9B-GGUF", "tag": "Q4_K_M"}
      and B.name_parts("lfm2.5:8b") == {"owner": "", "name": "lfm2.5", "tag": "8b"}
      and B.name_parts("Qwen/Qwen3.8-Flash") == {"owner": "Qwen", "name": "Qwen3.8-Flash", "tag": ""},
      B.name_parts("lfm2.5:8b"))

print("\na run made with a setup that has changed since is marked, and only then")
stamped = B.run_view(dict(run, setup_digest="aaaaaaaaaaaa", container_name="nanobot-user1"))
check("  the digest and the container come through the view",
      (stamped["setup_digest"], stamped["container_name"]) == ("aaaaaaaaaaaa", "nanobot-user1"), stamped)
check("  same setup deployed now: not stale", not B.setup_is_stale(stamped, "aaaaaaaaaaaa"))
check("  a different setup deployed now: stale", B.setup_is_stale(stamped, "bbbbbbbbbbbb"))
check("  not known yet (still being worked out, or the container is gone): not stale",
      not B.setup_is_stale(stamped, None) and not B.setup_is_stale(stamped, ""))
unstamped = B.run_view(dict(run))
check("  a run from before the stamp: stale, whatever is deployed",
      unstamped["setup_digest"] == "" and B.setup_is_stale(unstamped, None)
      and B.setup_is_stale(unstamped, "aaaaaaaaaaaa"), unstamped)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")

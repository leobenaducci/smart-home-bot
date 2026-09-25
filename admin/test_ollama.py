#!/usr/bin/env python3
"""The Models page's Ollama servers: "runs on", the instances card, the estimate.

Run: python admin/test_ollama.py

A local model is listed once and the server that runs it is picked beside it;
saving writes the server back into the model's prefix, which every consumer
downstream already reads. What would go wrong quietly, and so is checked here:
a role silently moved to another server on an unrelated save, a server removed
while a role still runs on it, and an estimate that says "fits" on a guess.
"""
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
tmp = pathlib.Path(tempfile.mkdtemp(prefix="ollama-admin-"))
os.environ["HOME_STACK_CONFIG"] = str(tmp / "home-stack.yml")
os.environ["HOME_STACK_STATE_DIR"] = str(tmp)
os.environ["HOME_STACK_CACHE"] = str(tmp)
(tmp / "home-stack.yml").write_text("{}\n")

import app as A  # noqa: E402
import ollama_vram as V  # noqa: E402
from werkzeug.datastructures import MultiDict  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


print("\na local model is shown once, with the server it runs on")
check("  vision's model", A.local_model("ollama-vision:qwen3-vl:4b") == ("ollama:qwen3-vl:4b", "vision"))
check("  the main server's", A.local_model("ollama:gemma4:e4b") == ("ollama:gemma4:e4b", "main"))
check("  a further one", A.local_model("ollama-gpu0:m:1") == ("ollama:m:1", "gpu0"))
check("  ollama.com is not local", A.local_model("ollama-cloud:x:1") == ("ollama-cloud:x:1", ""))
check("  a hosted model is left alone", A.local_model("deepseek-v4-flash") == ("deepseek-v4-flash", ""))

CFG = {"hosts": {"hub": {"address": "127.0.0.1"}},
       "assistant": {"models": {"vision": "ollama-vision:qwen3-vl:4b", "everyday": "ollama:gemma4:e4b"}},
       "cloud": {"ollama": {"instances": [
           {"id": "main", "host": "hub", "port": 11434, "gpus": [1], "context": 40960, "managed": True},
           {"id": "vision", "host": "hub", "port": 11435, "gpus": [1], "context": 8192, "managed": True},
           {"id": "spare", "host": "hub", "port": 11440, "gpus": [0], "context": 8192}]}}}


def row(n, iid, **kw):
    base = {"port": "", "context": "8192", "parallel": "1", "max_models": "1", "kv_cache": "q4_0"}
    base.update(kw)
    return [(f"oi-{n}-id", iid)] + [(f"oi-{n}-{k}", str(v)) for k, v in base.items()]


print("\nthe instances card")
form = MultiDict([("oi-count", "3")] + row(0, "main", port=11434, context=40960, gpu="1", managed="on")
                 + row(1, "vision", port=11435, gpu="0", managed="on")
                 + row(2, "spare", port=11440) + [("oi-2-remove", "on")]
                 + row(3, "tasks", port=11437, context=131072, parallel=2, kv_cache="q8_0", gpu="0",
                       managed="on", label="Background"))
entries, errors = A._form_instances(form, CFG)
ids = [e["id"] for e in entries]
check("  saved with no complaint", not errors, errors)
check("  an unused server is removed, a new one added", ids == ["main", "vision", "tasks"], ids)
check("  vision moved to GPU0", next(e for e in entries if e["id"] == "vision")["gpus"] == [0])
check("  the new one as typed", next(e for e in entries if e["id"] == "tasks") | {} == {
    **next(e for e in entries if e["id"] == "tasks"), "context": 131072, "parallel": 2,
    "kv_cache": "q8_0", "gpus": [0], "managed": True, "unit": "ollama-tasks", "label": "Background"})
_, errors = A._form_instances(MultiDict([("oi-count", "1")] + row(0, "vision", port=11435)
                                        + [("oi-0-remove", "on")]), CFG)
check("  a server a role still runs on is not removed", errors and "vision" in errors[0], errors)
_, errors = A._form_instances(MultiDict([("oi-count", "1")] + row(0, "main", port=11434)
                                        + row(1, "other", port=11434)), CFG)
check("  two servers on one port are refused", any("both use" in e for e in errors), errors)

print("\na model setup: the model, its window, cache and slots, placed automatically")
form = MultiDict([("oi-count", "1")] + row(0, "main", port=11434, context=40960, gpu="1", managed="on")
                 + [("oi-1-model", "gemma4:e4b"), ("oi-1-context", "49152"), ("oi-1-kv_cache", "q4_0"),
                    ("oi-1-parallel", "2"), ("oi-1-gpu", "auto"), ("oi-1-pinned", "on"),
                    ("oi-1-managed", "on")])
entries, errors = A._form_instances(form, CFG)
setup = entries[-1] if entries else {}
check("  only a model is needed: the id comes from it, the port is the next free one",
      not errors and setup.get("id") == "gemma4e4b" and setup.get("port") == 11437, (errors, setup))
check("  kept loaded, on Auto, one model to the server",
      setup.get("pinned") and setup.get("gpu_mode") == "auto" and setup.get("max_models") == 1
      and setup.get("context") == 49152 and setup.get("parallel") == 2, setup)
import ollama_instances as OI  # noqa: E402
_inst = OI.normalize(setup)
check("  its unit keeps it loaded for good", OI.desired_env(_inst).get("OLLAMA_KEEP_ALIVE") == "-1")
check("  the picker names it by model and cache",
      OI.display(_inst).startswith("gemma4:e4b · q4_0"), OI.display(_inst))

print("\nthe setups rows")
SCFG = {"hosts": {"hub": {"address": "127.0.0.1"}},
        "assistant": {"models": {"everyday": "ollama-chat:gemma4:e4b"}},
        "cloud": {"ollama": {"instances": CFG["cloud"]["ollama"]["instances"],
                             "setups": [{"id": "chat", "name": "Chat", "model": "gemma4:e4b",
                                         "context": 49152, "parallel": 2, "port": 11437, "gpus": [1]},
                                        {"id": "old", "model": "gemma4:e2b"}]}}}
form = MultiDict([("su-count", "2"),
                  ("su-0-id", "chat"), ("su-0-name", "Chat"), ("su-0-model", "gemma4:e4b"),
                  ("su-0-context", "65536"), ("su-0-kv_cache", "q4_0"), ("su-0-parallel", "2"), ("su-0-gpu", "auto"),
                  ("su-1-id", "old"), ("su-1-model", "gemma4:e2b"), ("su-1-remove", "on"),
                  ("su-2-name", "Short Text"), ("su-2-model", "gemma4:e4b"), ("su-2-context", "8192"),
                  ("su-2-kv_cache", "q8_0"), ("su-2-parallel", "4"), ("su-2-gpu", "cpu")])
got, errs = A._form_setups(form, SCFG)
ids = [g["id"] for g in got]
check("  an unused setup is removed; a new one takes its id from its name",
      not errs and ids == ["chat", "shorttext"], (errs, ids))
check("  an existing one keeps its port and placement while its window changes",
      got[0]["port"] == 11437 and got[0]["gpus"] == [1] and got[0]["context"] == 65536, got[0])
check("  CPU is a choice", got[1]["gpu"] == "cpu" and got[1]["gpus"] == [])
_, errs = A._form_setups(MultiDict([("su-count", "1"), ("su-0-id", "chat"), ("su-0-model", "gemma4:e4b"),
                                    ("su-0-remove", "on")]), SCFG)
check("  a setup a task uses is not removed", errs and "Chat" in errs[0], errs)
got, errs = A._form_setups(MultiDict([("su-count", "1"), ("su-0-id", "chat"), ("su-0-model", "__other__"),
                                   ("su-0-model-other", "hf:owner/repo/file.gguf"),
                                   ("su-0-engine", "llamacpp")]), SCFG)
check("  the picker's Other takes the name typed beside it",
      got and got[0]["model"] == "hf:owner/repo/file.gguf", (got, errs))

print("\nplacement")
G = V.GIB
cards = [{"index": 0, "total_mib": 12288, "tenants": [{"owner": "container audio-cpp", "mib": 600}]},
         {"index": 1, "total_mib": 12288, "tenants": [{"owner": "container cams", "mib": 930}]}]
insts = [{"id": "gemma", "gpu_mode": "auto", "gpus": []}, {"id": "vl", "gpu_mode": "auto", "gpus": []},
         {"id": "ornith", "gpu_mode": "auto", "gpus": []}, {"id": "bench", "gpu_mode": "fixed", "gpus": [0]}]
got = V.place(cards, insts, {"gemma": 6 * G, "vl": 4.4 * G, "ornith": 7.5 * G, "bench": 0})
check("  largest first, each where there is most room: nothing left over",
      not got["unplaced"] and got["gpus"]["ornith"] != got["gpus"]["gemma"], got)
check("  what fits no card alone is split when both together hold it",
      V.place(cards, [{"id": "big", "gpu_mode": "auto", "gpus": []}], {"big": 16 * G})["gpus"]["big"] == [0, 1])
check("  and reported when nothing holds it",
      V.place(cards, [{"id": "huge", "gpu_mode": "auto", "gpus": []}], {"huge": 40 * G})["unplaced"] == ["huge"])
check("  a server still fitting where it is stays there (no restart for nothing)",
      V.place(cards, [{"id": "a", "gpu_mode": "auto", "gpus": [0]}], {"a": 2 * G})["gpus"]["a"] == [0])
check("  a CPU server takes no card's room",
      V.place(cards, [{"id": "c", "cpu": True, "gpu_mode": "fixed", "gpus": []},
                      {"id": "a", "gpu_mode": "auto", "gpus": []}], {"c": 30 * G, "a": 3 * G})["unplaced"] == [])
check("  fixed servers are counted where they are",
      V.place(cards, [{"id": "f", "gpu_mode": "fixed", "gpus": [1]}, {"id": "a", "gpu_mode": "auto", "gpus": []}],
              {"f": 10 * G, "a": 3 * G})["gpus"]["a"] == [0])

print("\naudio.cpp and the cameras, counted on the card chosen for them")
import json as _json  # noqa: E402
(tmp / "gpus.json").write_text(_json.dumps({"gpus": [
    {"index": 0, "total_mib": 12288, "tenants": [{"owner": "container audio-cpp", "mib": 570}]},
    {"index": 1, "total_mib": 12288, "tenants": [{"owner": "container home-cameras-web-homecameras-1", "mib": 930},
                                                 {"owner": "unit ollama", "mib": 4500}]}]}))
_cfg = {"services": {"audio-cpp": {"gpu": True, "gpu_device": "0"},
                     "home-cameras": {"gpu": False, "gpu_device": "0"}}}
_g = {g["index"]: [t["owner"] for t in g["tenants"]] for g in A._gpus_with_tenants(_cfg)}
check("  the cameras, moved to GPU0 in the config, are counted there",
      "container home-cameras-web-homecameras-1" in _g[0] and _g[1] == ["unit ollama"], _g)
_cfg["services"]["audio-cpp"]["gpu"] = False
_g = {g["index"]: [t["owner"] for t in g["tenants"]] for g in A._gpus_with_tenants(_cfg)}
check("  audio on the CPU is on no card", not any("audio" in o for v in _g.values() for o in v), _g)
check("  the choice reads back", A._tenant_choice(_cfg, "audio-cpp") == "cpu"
      and A._tenant_choice(_cfg, "home-cameras") == "0")
_w = {"device": "cuda", "gpu_device": "all"}
A._set_tenant(_w, "faster-whisper", "cpu")
check("  speech to text on the CPU: float32, not int8", _w["device"] == "cpu" and _w["compute_type"] == "float32", _w)
A._set_tenant(_w, "faster-whisper", "0")
check("  and back on a card: cuda, float16, that card",
      _w == {"device": "cuda", "compute_type": "float16", "gpu_device": "0"}, _w)
check("  a service on any card stays where the host saw it",
      "container audio-cpp" in [t["owner"] for g in A._gpus_with_tenants(
          {"services": {"audio-cpp": {"gpu": True, "gpu_device": "all"}}}) for t in g["tenants"]])
_g = A._gpus_with_tenants({"services": {"faster-whisper": {"device": "cuda", "gpu_device": "1"}}})
check("  a service never seen on a card is counted at its typical size there",
      any(t["owner"] == "container faster-whisper" and t.get("estimated") for t in _g[1]["tenants"]), _g)
check("  the cameras cannot be put on the CPU",
      [c for s_, _o, c in A.GPU_TENANTS if s_ == "home-cameras"] == [False])

print("\nTest on a sub-agent row, with pi on")
import subprocess as _sp  # noqa: E402
A.bench_containers = lambda: ["nanobot-user1"]
_doc = {"ok": True, "model": "gemma4:e4b", "base": "http://h:11437/v1", "rounds": 1, "turns": 7,
        "tools": [{"name": "web", "error": False}, {"name": "read", "error": True}],
        "links": ["download:bench/x.pdf"], "error": "", "reply": "Listo"}
_calls = []
A.subprocess.run = lambda argv, **kw: (_calls.append(argv), type("R", (), {
    "stdout": "log line\nPROBE_JSON " + _json.dumps(_doc), "stderr": "", "returncode": 0})())[1]
_r = A._probe_on_pi("subagent", "ollama-text:gemma4:e4b")
check("  runs the probe in an assistant, through pi",
      _calls[-1][:3] == ["docker", "exec", "nanobot-user1"] and "nanobot.harness.probe" in _calls[-1])
check("  delivered, with the file and the tools", _r["ok"] is True and any(
    "x.pdf" in st.get("detail", "") for st in _r["steps"]) and any("read ✗" in st.get("detail", "") for st in _r["steps"]), _r)
check("  the saved model is the one tested: no note",
      not any("deploy nanobot" in st["step"] for st in _r["steps"]))
_r = A._probe_on_pi("subagent", "ollama-text:qwen3.5:9b")
check("  a model saved since the deploy: said", any("deploy nanobot" in st["step"] or "desplegá" in st["step"]
                                                  for st in _r["steps"]), _r["steps"])
_doc.update(model="deepseek-v4-pro", base="https://opencode.ai/zen/v1")
_r = A._probe_on_pi("subagent_powerful", "opencode-go/deepseek-v4-pro")
check("  Go picked but Zen still running: said, though the names match",
      any("deploy nanobot" in st["step"] or "desplegá" in st["step"] for st in _r["steps"]), _r["steps"])
A.subprocess.run = _sp.run

print("\nsaving: roles and the fallback chain follow their setup")
import copy as _copy  # noqa: E402
_base = {"hosts": {"hub": {"address": "127.0.0.1"}},
         "assistant": {"models": {"everyday": "ollama-text:qwen3.5:9b",
                                  "notifications": "ollama-text:gemma4:e4b",
                                  "fallback": ["ollama-text:gemma4:e4b", "zen-a"]}},
         "cloud": {"ollama": {"instances": CFG["cloud"]["ollama"]["instances"],
                              "setups": [{"id": "text", "name": "Text", "model": "gemma4:e4b",
                                          "context": 65536, "port": 11437, "gpus": [0]}]}}}
_saved = []
A.load_config = lambda: _copy.deepcopy(_base)
A.save_config = lambda c: _saved.append(c)
A.note_pending = lambda *a, **k: None
A._ollama_needs = lambda cfg, insts, *a, **k: {i["id"]: {"bytes": 0, "how": "idle"} for i in insts}
_form = {"su-count": "1", "su-0-id": "text", "su-0-name": "Text", "su-0-model": "qwen3.5:9b",
         "su-0-context": "65536", "su-0-kv_cache": "q8_0", "su-0-parallel": "2", "su-0-gpu": "0"}
with A.app.test_request_context("/models/ollama", method="POST", data=_form):
    A.ollama_save()
    _flashes = [m for _c, m in A.session.get("_flashes", [])]
_m = _saved[-1]["assistant"]["models"]
check("  a role on the setup follows its new model", _m["notifications"] == "ollama-text:qwen3.5:9b", _m)
check("  so does the fallback chain's entry", _m["fallback"][0] == "ollama-text:qwen3.5:9b", _m["fallback"])
check("  and a chain that now names the everyday model is said out loud",
      any("qwen3.5:9b" in f and ("everyday" in f or "día a día" in f) for f in _flashes), _flashes)

print("\nthe estimate")
facts = {"file": 3 * V.GIB, "window": 512,
         "layers": [("full", 2, 512, 512)] * 4 + [("swa", 2, 256, 256)] * 20}
check("  gemma4's KV: 4,608 bytes a token at q4_0 (docs/local-ollama.md)",
      round(V.kv_bytes(facts, 2001, 1, "q4_0") - V.kv_bytes(facts, 2000, 1, "q4_0")) == 4608)
inst = {"id": "main", "context": 40960, "parallel": 1, "kv_cache": "q4_0", "max_models": 1}
need = V.instance_need(inst, ["a"], {"a": facts}, {})
check("  a guess says it is one", need["how"] == "estimated")
point = {"models": {"a": {"vram": int(3.2 * V.GIB), "context": 40960, "parallel": 1, "kv_cache": "q4_0"}},
         "overhead": int(1.3 * V.GIB)}
need = V.instance_need(inst, ["a"], {"a": facts}, point)
check("  a matching measurement is 'measured', with the host's overhead",
      need["how"] == "measured" and abs(need["bytes"] - 4.5 * V.GIB) < 0.01 * V.GIB, need)
need2 = V.instance_need(dict(inst, parallel=4), ["a"], {"a": facts}, point)
check("  other slots from a measurement: estimated, and larger",
      need2["how"] == "estimated" and need2["bytes"] > need["bytes"])
view = V.gpu_view([{"index": 0, "total_mib": 12288, "tenants": [{"owner": "container audio-cpp", "mib": 600},
                                                                {"owner": "unit ollama", "mib": 4500}]}],
                  [dict(inst, gpus=[0])], {"main": {"bytes": 13 * V.GIB, "how": "estimated"}})
check("  a card it does not fit says so, and counts the others but not Ollama twice",
      not view[0]["fits"] and [o["owner"] for o in view[0]["others"]] == ["container audio-cpp"], view)
_units = [{"index": 0, "total_mib": 12288, "tenants": [
    {"owner": "unit llamacpp-chat", "mib": 8000}, {"owner": "container faster-whisper", "mib": 2000}]}]
_chat = {"id": "chat", "unit": "llamacpp-chat", "gpus": [0], "setup": True, "model": "b.gguf"}
_main = {"id": "main", "unit": "ollama", "gpus": [0]}
_v = V.gpu_view(_units, [_chat, _main], {"chat": {"bytes": 9 * V.GIB, "how": "estimated"},
                                        "main": {"bytes": 1 * V.GIB, "how": "unknown"}})[0]
check("  a llama.cpp server is the setup's, not another service",
      [o["owner"] for o in _v["others"]] == ["container faster-whisper"], _v["others"])
check("  and is drawn at what the host saw it hold",
      [(r["id"], r["how"]) for r in _v["instances"]] == [("chat", "measured"), ("main", "idle")]
      and abs(_v["instances"][0]["bytes"] - 8000 * 1024 * 1024) < 1, _v["instances"])
_p = V.gpu_view(_units, [_chat], {"chat": {"bytes": 9 * V.GIB, "how": "estimated"}}, measured=False)[0]
check("  the preview draws the estimate: what it shows is not running yet",
      _p["instances"][0]["how"] == "estimated")

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all checks passed")

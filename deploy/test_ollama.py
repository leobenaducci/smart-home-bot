#!/usr/bin/env python3
"""cloud.ollama.instances: the household's own Ollama servers.

Run: python deploy/test_ollama.py

What is asserted, most important first:

  * a config written before the list deploys exactly as it did -- the old
    `local` / `vision` entries are read as the list they mean, and the URLs,
    windows and model routing that come out are the ones that always did;
  * a new instance is a model prefix (`ollama-tasks:`), a provider nanobot can
    build (a block in its config) and a unit the host can apply -- all three;
  * the root side stays narrow: only `ollama*` units, and every value that
    reaches a unit file is checked by shape;
  * the plan says what an apply would change and nothing else: a running unit
    that matches the list is "nothing to do", whatever its drop-ins are called.
"""
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("deployer", HERE / "deploy.py")
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)
import ollama_instances as OI  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except OI.InstanceError as exc:
        return str(exc)
    return ""


HOSTS = {"hub": {"address": "127.0.0.1", "from_container": "host.docker.internal"}}
LEGACY = {"hosts": HOSTS, "cloud": {"ollama": {
    "local": {"enabled": True, "host": "hub", "port": 11434, "context": 40960},
    "vision": {"enabled": True, "url": "http://host.docker.internal:11435", "context": 8192}}}}

print("\na config from before the list deploys as it did")
ep = D.ollama_endpoints(LEGACY, {})
check("  main and vision, at the addresses they always had",
      ep.get("ollama") == ("http://host.docker.internal:11434", "ollama")
      and ep.get("ollama_vision") == ("http://host.docker.internal:11435", "ollama")
      and ep.get("ollama_host") == ("http://127.0.0.1:11434", "ollama"), ep)
check("  their windows", D.ollama_context(LEGACY, "ollama") == "40960"
      and D.ollama_context(LEGACY, "ollama_vision") == "8192")
check("  a legacy entry with no window says nothing rather than inventing one",
      D.ollama_context({"hosts": HOSTS, "cloud": {"ollama": {"local": {"host": "hub"}}}},
                       "ollama") == "")
check("  the old prefixes still split the same way",
      D.split_model("ollama:gemma4:e4b") == ("gemma4:e4b", "ollama")
      and D.split_model("ollama-vision:qwen3-vl:4b") == ("qwen3-vl:4b", "ollama_vision")
      and D.split_model("ollama-cloud:x:1b") == ("x:1b", "ollama_cloud"))

print("\nthe same servers as a list give the same answers")
LISTED = {"hosts": HOSTS, "cloud": {"ollama": {"instances": [
    {"id": "main", "host": "hub", "port": 11434, "gpus": [1], "context": 40960,
     "max_models": 2, "managed": True},
    {"id": "vision", "host": "hub", "port": 11435, "gpus": [1], "context": 8192, "managed": True},
    {"id": "bench", "host": "hub", "port": 11436, "gpus": [0], "context": 40960,
     "purpose": "bench", "keep_alive": "5m", "managed": True}]}}}
ep2 = D.ollama_endpoints(LISTED, {})
check("  endpoints", ep2 == ep, ep2)
check("  windows", D.ollama_context(LISTED, "ollama") == "40960"
      and D.ollama_context(LISTED, "ollama_vision") == "8192")
check("  the benchmark's server is not a role's",
      "ollama_bench" not in ep2 and OI.bench_instance(LISTED)["port"] == 11436)

print("\na new instance is a prefix, a provider block and a unit")
NEW = json.loads(json.dumps(LISTED))
NEW["cloud"]["ollama"]["instances"].append(
    {"id": "tasks", "host": "hub", "port": 11437, "gpus": [0], "context": 131072,
     "parallel": 2, "kv_cache": "q8_0", "managed": True})
NEW["assistant"] = {"models": {"subagent": "ollama-tasks:ornith-1.5:9b"}}
check("  its prefix splits into its provider",
      D.split_model("ollama-tasks:ornith-1.5:9b") == ("ornith-1.5:9b", "ollama_tasks"))
check("  an id ending in a digit too", D.split_model("ollama-gpu0:m:1") == ("m:1", "ollama_gpu0"))
check("  the model is reached on its port",
      D.model_endpoint(NEW, {}, "ollama-tasks:ornith-1.5:9b")
      == ("ornith-1.5:9b", "http://host.docker.internal:11437/v1", "ollama"))
out = json.loads(D.apply_model_choices(
    json.dumps({"providers": {"ollama": {}, "ollama_vision": {}}, "agents": {"defaults": {}}}), NEW))
check("  nanobot's config gets a block for it",
      out["providers"].get("ollama_tasks") == {"apiKey": "ollama",
                                               "apiBase": "http://host.docker.internal:11437/v1"},
      out["providers"])
check("  and the role names it",
      out["agents"]["defaults"].get("subagentProvider") == "ollama_tasks")
check("  the shipped blocks are left to the shipped files",
      out["providers"]["ollama"] == {} and out["providers"]["ollama_vision"] == {})
check("  a removed instance's model fails as not configured, not as a Zen model",
      D.split_model("ollama-gone:m")[1] == "ollama_gone"
      and D.model_endpoint(LISTED, {}, "ollama-gone:m")[1] == "")

print("\nwhat the list refuses")
check("  a duplicate id", "twice" in raises(OI.instances, {"cloud": {"ollama": {"instances": [
    {"id": "a", "port": 1}, {"id": "a", "port": 2}]}}}))
check("  two servers on one port", "both use" in raises(OI.instances, {"cloud": {"ollama": {"instances": [
    {"id": "a", "port": 11434}, {"id": "b", "port": 11434}]}}}))
check("  `cloud` as an id (it is ollama.com)", raises(OI.normalize, {"id": "cloud", "port": 1}))
check("  a unit that is not Ollama's", "ollama or ollama-" in raises(
    OI.normalize, {"id": "x", "port": 1, "unit": "sshd"}))
check("  a keep-alive that is not a duration", raises(
    OI.normalize, {"id": "x", "port": 1, "keep_alive": "5m\nExecStart=/bin/sh"}))
check("  a KV cache type Ollama does not have", raises(OI.normalize, {"id": "x", "port": 1, "kv_cache": "q2"}))
check("  a label cannot break a unit file's line",
      "\n" not in OI.normalize({"id": "x", "port": 1, "label": "a\nb"})["label"])

print("\nthe unit")
inst = OI.by_id(NEW)["tasks"]
dropin = OI.render_dropin(inst, {0: 0, 1: 1})
check("  the settings", all(l in dropin for l in (
    "Environment=OLLAMA_HOST=0.0.0.0:11437", "Environment=OLLAMA_NUM_PARALLEL=2",
    "Environment=OLLAMA_CONTEXT_LENGTH=131072", "Environment=OLLAMA_KV_CACHE_TYPE=q8_0")), dropin)
check("  pinned by device node, with earlier pins reset first",
      dropin.index("DeviceAllow=\n") < dropin.index("DeviceAllow=/dev/nvidia0 rw")
      and "/dev/nvidia1" not in dropin and "DevicePolicy=closed" in dropin)
check("  an index maps to its device minor",
      "/dev/nvidia7 rw" in OI.render_dropin(inst, {0: 7}))

SHOWN = OI.parse_show(
    "DevicePolicy=closed\nDeviceAllow=/dev/nvidia-modeset rw\nDeviceAllow=/dev/nvidia-uvm-tools rw\n"
    "DeviceAllow=/dev/nvidia-uvm rw\nDeviceAllow=/dev/nvidiactl rw\nDeviceAllow=/dev/nvidia1 rw\n"
    "Environment=PATH=/usr/bin OLLAMA_HOST=0.0.0.0:11434 OLLAMA_FLASH_ATTENTION=1 "
    "OLLAMA_KV_CACHE_TYPE=q4_0 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=2 "
    "OLLAMA_CONTEXT_LENGTH=40960\nLoadState=loaded\nActiveState=active\n")
main = OI.by_id(LISTED)["main"]
check("  a running unit that matches is nothing to do", OI.diff(main, SHOWN) == [], OI.diff(main, SHOWN))
moved = dict(main, gpus=[0], parallel=4)
d = OI.diff(moved, SHOWN)
check("  a change says what, from what", any("OLLAMA_NUM_PARALLEL: 1 -> 4" in x for x in d)
      and any("/dev/nvidia1 -> /dev/nvidia0" in x for x in d), d)
check("  a unit that does not exist is created",
      OI.diff(inst, OI.parse_show("LoadState=not-found\n")) == ["create ollama-tasks.service"])
imported = OI.normalize(OI.instance_from_unit("main", "ollama", SHOWN, {1: 1}))
check("  --import describes a unit exactly", OI.diff(imported, SHOWN) == []
      and imported["gpus"] == [1] and imported["max_models"] == 2, imported)

print("\na server taken off the list is stopped by the next apply")
import tempfile  # noqa: E402
import ollama_host as H  # noqa: E402
_sd = pathlib.Path(tempfile.mkdtemp(prefix="systemd-"))
for _u in ("ollama", "ollama-vision", "ollama-old", "ollama-handmade"):
    (_sd / f"{_u}.service.d").mkdir()
for _u in ("ollama", "ollama-vision", "ollama-old"):
    (_sd / f"{_u}.service.d" / OI.DROPIN_NAME).write_text("x")
H.SYSTEMD = _sd
H.show = lambda unit: SHOWN
_rows = H.plan(LISTED, {"gpus": []})
_orph = [r for r in _rows if r.get("orphan")]
check("  a stack-written unit no longer listed is to be removed",
      [r["unit"] for r in _orph] == ["ollama-old"], _orph)
check("  never the main unit, never one the stack never wrote",
      not any(r["unit"] in ("ollama", "ollama-handmade") for r in _orph))
_calls = []
H._sudo = lambda argv, data=None: _calls.append(argv)
(_sd / "ollama-old.service").write_text("[Unit]\nDescription=Ollama (old) - managed by home-stack\n")
H.do_apply(LISTED, {"gpus": []}, _orph, yes=True)
check("  it is stopped, its drop-in and its unit removed",
      ["systemctl", "disable", "--now", "ollama-old.service"] in _calls
      and ["rm", "-f", str(_sd / "ollama-old.service.d" / OI.DROPIN_NAME)] in _calls
      and ["rm", "-f", str(_sd / "ollama-old.service")] in _calls, _calls)

print("\nmodel setups: only the used ones run")
SET = json.loads(json.dumps(LISTED))
SET["cloud"]["ollama"]["setups"] = [
    {"id": "chat", "name": "Chat", "model": "gemma4:e4b", "context": 49152, "kv_cache": "q4_0", "parallel": 2},
    {"id": "short", "name": "Short text", "model": "gemma4:e4b", "context": 8192, "kv_cache": "q8_0",
     "parallel": 4, "gpu": 1, "port": 11440},
    {"id": "spare", "model": "gemma4:e2b", "context": 4096},
    {"id": "slow", "model": "qwen3:1.7b", "gpu": "cpu"}]
SET["assistant"] = {"models": {"everyday": "ollama-chat:gemma4:e4b",
                               "notifications": "ollama-short:gemma4:e4b",
                               "fallback": ["ollama-slow:qwen3:1.7b", "zen-x"]}}
_by = {i["id"]: i for i in OI.instances(SET)}
check("  used setups are servers, unused ones are not",
      {"chat", "short", "slow"} <= set(_by) and "spare" not in _by, sorted(_by))
check("  a setup keeps its given port; one without gets the next free one",
      _by["short"]["port"] == 11440 and _by["chat"]["port"] == 11437, (_by["chat"]["port"], _by["short"]["port"]))
check("  kept loaded, one model, the window, cache and slots given",
      _by["chat"]["pinned"] and _by["chat"]["max_models"] == 1 and _by["chat"]["context"] == 49152
      and OI.desired_env(_by["chat"])["OLLAMA_KEEP_ALIVE"] == "-1")
check("  a card given is fixed; none is Auto",
      _by["short"]["gpus"] == [1] and _by["short"]["gpu_mode"] == "fixed" and _by["chat"]["gpu_mode"] == "auto")
check("  three versions of one model are three setups",
      D.model_endpoint(SET, {}, "ollama-short:gemma4:e4b")[1].endswith(":11440/v1"))
_cpu = OI.render_dropin(_by["slow"])
check("  a CPU setup may open no card at all",
      "DevicePolicy=closed" in _cpu and "/dev/nvidia" not in _cpu and _by["slow"]["gpus"] == [], _cpu)
check("  and the plan says so of a unit that has one",
      any("CPU only" in c for c in OI.diff(_by["slow"], SHOWN)))
check("  a setup id may not be a server's", "also a server" in raises(OI.instances, {**SET, "cloud": {"ollama": {
    "instances": SET["cloud"]["ollama"]["instances"],
    "setups": [{"id": "vision", "model": "x:1"}]}}}))

check("  the vision URL follows the vision role onto its setup",
      D._vision_url({**SET, "assistant": {"models": {"vision": "ollama-short:gemma4:e4b"}}},
                    D.ollama_endpoints({**SET, "assistant": {"models": {"vision": "ollama-short:gemma4:e4b"}}}, {}),
                    "http://main").endswith(":11440"))
check("  and stays on the vision server otherwise",
      D._vision_url(LISTED, D.ollama_endpoints(LISTED, {}), "http://main").endswith(":11435"))

print("\nwhat the picker shows")
check("  a name, its cards, its window and slots",
      OI.display(OI.normalize({"id": "tasks", "label": "Background", "port": 1, "gpus": [0],
                               "context": 131072, "parallel": 2}))
      == "Background · GPU 0 · 128k × 2")

print("\na llama.cpp setup")
LC_SET = {"cloud": {"ollama": {"instances": [{"id": "main", "port": 11434}], "setups": [
    {"id": "steps", "model": "hf:prism-ml/Bonsai-2-27B-gguf/Ternary-Bonsai-2-27B-PQ2_0.gguf",
     "engine": "prism", "context": 98304, "parallel": 2, "kv_cache": "q4_0", "gpu": 0,
     "thinking": False},
    {"id": "moe", "model": "qwen3.8:27b-iq3-gpu", "engine": "llamacpp", "gpu": "cpu"}]}},
    "assistant": {"models": {"plan_steps": "ollama-steps:hf:prism-ml/Bonsai-2-27B-gguf/Ternary-Bonsai-2-27B-PQ2_0.gguf",
                             "subagent": "ollama-moe:qwen3.8:27b-iq3-gpu"}}}
_lc = {i["id"]: i for i in OI.instances(LC_SET)}
check("  runs as its own llamacpp unit", _lc["steps"]["unit"] == "llamacpp-steps"
      and _lc["steps"]["engine"] == "prism", _lc["steps"])
check("  and is named for its engine", "PrismML" in OI.display(_lc["steps"]), OI.display(_lc["steps"]))
_args = OI.llamacpp_args(_lc["steps"], "/m.gguf")
check("  a window per slot, as the page means it: -c is the whole cache",
      _args[_args.index("-c") + 1] == str(98304 * 2) and _args[_args.index("-np") + 1] == "2", _args)
check("  thinking off at the server", "--reasoning-budget" in _args)
check("  tool calls need --jinja", "--jinja" in _args)
_unit = OI.render_llamacpp_unit(_lc["steps"], "/opt/home-stack/llamacpp/prism-x/bin", "/m.gguf", {0: 0})
check("  pinned to its card like an Ollama server", "DeviceAllow=/dev/nvidia0 rw" in _unit
      and "DevicePolicy=closed" in _unit and "User=ollama" in _unit, _unit)
_cpu_args = OI.llamacpp_args(_lc["moe"], "/m.gguf")
check("  a CPU setup offloads nothing", _cpu_args[_cpu_args.index("-ngl") + 1] == "0")
check("  and opens no card", "/dev/nvidia" not in OI.render_llamacpp_unit(_lc["moe"], "/b", "/m.gguf"))
check("  the signature changes with the engine",
      OI.signature(_lc["steps"]) != OI.signature({**_lc["steps"], "engine": "llamacpp"}))
for bad in ("/etc/shadow", "/var/lib/home-stack/models/../../../etc/x.gguf", "hf:a/b/../c.txt",
            "x; rm -rf /", "hf:owner/repo/file.bin"):
    check(f"  refuses the model {bad!r}", "llama.cpp" in raises(OI.instances, {**LC_SET, "cloud": {"ollama": {
        "instances": [{"id": "main", "port": 11434}],
        "setups": [{"id": "steps", "model": bad, "engine": "llamacpp"}]}}}) or
        "model" in raises(OI.instances, {**LC_SET, "cloud": {"ollama": {
        "instances": [{"id": "main", "port": 11434}],
        "setups": [{"id": "steps", "model": bad, "engine": "llamacpp"}]}}}))
check("  a unit name cannot be anything but ollama* or llamacpp-*",
      not OI.UNIT_RE.fullmatch("sshd") and not OI.UNIT_RE.fullmatch("llamacpp-x;y")
      and OI.UNIT_RE.fullmatch("llamacpp-steps"))
check("  an unknown engine is refused", "engine" in raises(OI.instances, {**LC_SET, "cloud": {"ollama": {
    "instances": [{"id": "main", "port": 11434}],
    "setups": [{"id": "steps", "model": "x:1", "engine": "vllm"}]}}}))

import ollama_host as OH  # noqa: E402
_store = pathlib.Path(tempfile.mkdtemp())
(_store / "manifests/registry.ollama.ai/library/qwen3.5").mkdir(parents=True)
(_store / "manifests/hf.co/owner/repo").mkdir(parents=True)
_dig = "a" * 64
for _m in ("registry.ollama.ai/library/qwen3.5/9b", "hf.co/owner/repo/Q4_K_M"):
    (_store / "manifests" / _m).write_text(json.dumps({"layers": [
        {"mediaType": "application/vnd.ollama.image.template", "digest": "sha256:" + "b" * 64},
        {"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:" + _dig}]}))
OH.OLLAMA_MODELS = _store
check("  an Ollama name reads its GGUF from Ollama's store",
      OH.ollama_blob("qwen3.5:9b") == _store / "blobs" / f"sha256-{_dig}")
check("  a name with a registry host keeps its path",
      OH.ollama_blob("hf.co/owner/repo:Q4_K_M") == _store / "blobs" / f"sha256-{_dig}")
check("  and a model Ollama does not have is said so", OH.ollama_blob("nope:1") is None
      and "ollama pull" in OH.model_path({"model": "nope:1"})[1])
check("  a hf: model lands in the stack's models directory",
      str(OH.model_path(_lc["steps"])[0]).startswith(OI.MODELS_DIR + "/hf/prism-ml/"))

print("\nthe llama.cpp builds")
import llamacpp as LC  # noqa: E402
_script = LC.build_script("prism", "86", 1000, 1000)
check("  pinned: the clone is the tag, nothing newer", "--branch prism-b10709-9a9394a" in _script)
check("  for this CPU and these cards", "-DGGML_NATIVE=ON" in _script and "CMAKE_CUDA_ARCHITECTURES='86'" in _script)
check("  no NCCL: the host has none and a build linked to it does not start",
      "-DGGML_CUDA_NCCL=OFF" in _script)
check("  CUDA's runtime goes with the build", "libcublasLt.so.12" in _script)
check("  no card, no CUDA", "-DGGML_CUDA=OFF" in LC.build_script("vanilla", "", 1000, 1000))
_root = pathlib.Path(tempfile.mkdtemp())
check("  an unbuilt flavour is not offered", LC.built("prism", _root) is None)
(_root / LC.build_name("prism") / "bin").mkdir(parents=True)
(_root / LC.build_name("prism") / "bin" / "llama-server").write_text("")
(_root / LC.build_name("prism") / "build.json").write_text('{"built_at": 1}')
_bf = _root / "builds.json"
LC.record_builds(_root, _bf)
check("  and a built one is recorded for the page", set(json.loads(_bf.read_text())) == {"prism"})

print("\nthe model library")
import model_library as ML  # noqa: E402
for bad, why in (({"op": "rm -rf", "model": "x:1"}, "unknown"),
                 ({"op": "download", "model": "qwen3.5:9b"}, "hf:"),
                 ({"op": "download", "model": "hf:a/b/../../etc/passwd.gguf"}, "hf:"),
                 ({"op": "pull", "model": "hf:a/b/c.gguf"}, "Ollama model name"),
                 ({"op": "delete", "model": "/etc/shadow"}, "library model")):
    try:
        ML.check_job(bad)
        check(f"  refuses {bad}", False)
    except ML.LibraryError as exc:
        check(f"  refuses {bad['op']} {bad['model']!r}", why in str(exc), str(exc))
check("  keeps only engines that exist",
      ML.check_job({"op": "test", "model": "x:1", "engines": ["prism", "vllm"]})["engines"] == ["prism"])
_md = pathlib.Path(tempfile.mkdtemp())
(_md / "hf/prism-ml/Bonsai-gguf").mkdir(parents=True)
(_md / "hf/prism-ml/Bonsai-gguf/Bonsai-PQ2_0.gguf").write_bytes(b"x")
(_md / "hf/prism-ml/Bonsai-gguf/mmproj-F16.gguf").write_bytes(b"x")
_files = ML.file_models(_md)
check("  a downloaded GGUF is listed by the name a setup uses, projectors left out",
      [f["id"] for f in _files] == ["hf:prism-ml/Bonsai-gguf/Bonsai-PQ2_0.gguf"], _files)
check("  the reason a load failed is llama.cpp's, not the CPU test's CUDA notice",
      ML.load_error("0.01 E ggml_cuda_init: failed to initialize CUDA: no CUDA-capable device\n"
                    "0.02 E llama_model_load: error loading model: done_getting_tensors: wrong number "
                    "of tensors; expected 2131, got 720\n") ==
      "done_getting_tensors: wrong number of tensors; expected 2131, got 720")
for evil in ("hf:a/b/../../../../etc/cron.d/x.gguf", "hf:a/b/..gguf", "hf:../b/c.gguf", "hf:a/b//x.gguf"):
    check(f"  no path escape in {evil!r}", not OI.HF_RE.fullmatch(evil))
check("  a real file name still passes",
      OI.HF_RE.fullmatch("hf:prism-ml/Ternary-Bonsai-2-27B-gguf/Ternary-Bonsai-2-27B-PQ2_0.gguf")
      and OI.HF_RE.fullmatch("hf:unsloth/Qwen3.8-27B-GGUF/UD/Qwen3.8-27B-UD-IQ3_XXS.gguf"))
check("  a model a setup uses is not deleted",
      "used by a setup" in ML.delete("gemma4:e4b", {"gemma4:e4b"}))

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all checks passed")

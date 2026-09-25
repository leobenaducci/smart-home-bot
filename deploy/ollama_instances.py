"""The household's own Ollama servers: any number, each with its own card(s),
port, window and slots.

Why more than one: `OLLAMA_NUM_PARALLEL` and `OLLAMA_CONTEXT_LENGTH` are
server-wide. One server gives every model it loads the same slots and the same
window, so "vision with a small window beside notifications with a big one" is
two servers, and "background tasks with a huge window and several slots on the
other card" is a third. `docs/local-ollama.md` has the measurements.

`cloud.ollama.instances` is the list. Each one is reached by its own model
prefix, which is what lets a role pick an instance by picking a model:

    id `main`    ->  `ollama:<model>`        provider `ollama`
    id `vision`  ->  `ollama-vision:<model>` provider `ollama_vision`
    id `gpu0`    ->  `ollama-gpu0:<model>`   provider `ollama_gpu0`

`main` and `vision` are the two names this stack had before there was a list,
so every model already written in `assistant.models` keeps meaning what it
meant. A config written before the list (`cloud.ollama.local`, `.vision`,
`.bench`) is read as the equivalent list; nothing has to be migrated by hand.

Pure: no I/O, no subprocess. `ollama_host.py` is what talks to systemd and
nvidia-smi; `deploy.py` and the admin page read this.
"""
from __future__ import annotations

import re

# `ollama-cloud:` is ollama.com, not an instance.
RESERVED_IDS = {"cloud"}
ID_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
KV_TYPES = ("q4_0", "q8_0", "f16")
PURPOSES = ("", "bench")
# The root-owned copy the page's Apply button runs (ollama_host.py
# --install-trigger). Bump when either file changes in a way the copy must
# follow, and the page will ask for the trigger to be installed again.
HELPER_VERSION = 7
UNIT_RE = re.compile(r"ollama(-[a-z][a-z0-9]{0,15})?|llamacpp-[a-z][a-z0-9]{0,15}")
# What serves a setup. Ollama applies window, slots and cache per server;
# llama.cpp takes them per server too but runs one model per process, reads
# any GGUF (Ollama's store included), and in PrismML's fork is the only
# runtime for Bonsai's ternary packings. See deploy/llamacpp.py.
ENGINES = ("ollama", "llamacpp", "prism")
ENGINE_LABELS = {"ollama": "Ollama", "llamacpp": "llama.cpp", "prism": "llama.cpp (PrismML)"}
# A llama.cpp setup's model: an Ollama model name (its GGUF is read from
# Ollama's store), a Hugging Face file `hf:owner/repo/file.gguf` (downloaded
# once into MODELS_DIR), or a .gguf path inside MODELS_DIR. Checked by shape:
# a root service writes it into a unit.
MODELS_DIR = "/var/lib/home-stack/models"
# No ".." anywhere: the root helper downloads to and runs from the path this
# names, and a "../" would reach outside MODELS_DIR.
HF_RE = re.compile(r"hf:(?!.*\.\.)([A-Za-z0-9][\w.\-]{0,95})/([A-Za-z0-9][\w.\-]{0,95})/"
                   r"([\w.\-]+(?:/[\w.\-]+){0,4}\.gguf)")
GGUF_PATH_RE = re.compile(re.escape(MODELS_DIR) + r"/[\w.\-/]{1,300}\.gguf")
KEEP_ALIVE_RE = re.compile(r"-1|0|\d{1,5}(ms|s|m|h)|(\d{1,4}h)?(\d{1,4}m)?(\d{1,4}s)?")

# What an instance is when the config does not say. The same values the
# household's units ran before this list existed.
DEFAULTS = {
    "label": "",
    "host": "compute",
    "port": 11434,
    "url": "",
    "gpus": [],
    "context": 8192,
    "parallel": 1,
    "max_models": 1,
    "kv_cache": "q4_0",
    "flash_attention": True,
    "keep_alive": "",
    "unit": "",
    "managed": False,
    "purpose": "",
    "enabled": True,
    # A *model setup*: one server that exists to run one model at its own
    # window, cache and slots. Blank is a general server that loads whatever
    # it is asked for (the main one, the benchmark's).
    "model": "",
    # Kept loaded for good (keep-alive -1, loaded after every apply) rather
    # than on first use and dropped when idle.
    "pinned": False,
    # "auto": the page places it on a card when the list is saved; "fixed":
    # the cards ticked are the cards used.
    "gpu_mode": "fixed",
    # No card at all: the unit may open no GPU device, so Ollama runs the
    # model on the CPU -- slow, but out of every card's way.
    "cpu": False,
    "engine": "ollama",
    # llama.cpp only: thinking off at the server (--reasoning-budget 0). A
    # caller asks per request as well, but not every template honours it.
    "thinking": True,
}
MODEL_RE = re.compile(r"[A-Za-z0-9][\w.:/@+\-]{0,199}")


class InstanceError(ValueError):
    """A `cloud.ollama.instances` entry that cannot be used as written."""


def prefix_of(instance_id: str) -> str:
    return "ollama" if instance_id == "main" else f"ollama-{instance_id}"


def provider_of(instance_id: str) -> str:
    return "ollama" if instance_id == "main" else f"ollama_{instance_id}"


def id_of_provider(provider: str) -> str:
    """`ollama_gpu0` -> `gpu0`, `ollama` -> `main`; "" for anything else."""
    if provider == "ollama":
        return "main"
    m = re.fullmatch(r"ollama_([a-z][a-z0-9]{0,15})", provider or "")
    return m.group(1) if m and m.group(1) not in RESERVED_IDS else ""


def provider_of_prefix(prefix: str) -> str:
    """The provider a model prefix names, when it names a local instance.

    By shape rather than by looking the id up: `split_model` has no config in
    hand, and a model written for an instance that was since removed must still
    split into the right provider -- so the failure is "no such instance",
    said by whoever resolves the endpoint, and not a model silently sent to
    OpenCode Zen as an unknown name.
    """
    if prefix == "ollama":
        return "ollama"
    m = re.fullmatch(r"ollama-([a-z][a-z0-9]{0,15})", prefix or "")
    return f"ollama_{m.group(1)}" if m and m.group(1) not in RESERVED_IDS else ""


def is_local_provider(provider: str) -> bool:
    return bool(id_of_provider(provider))


def default_unit(instance_id: str) -> str:
    return "ollama" if instance_id == "main" else f"ollama-{instance_id}"


def _int(value, default: int, lo: int, hi: int, what: str) -> int:
    if value in (None, ""):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise InstanceError(f"{what} is {value!r}, not a number") from None
    if not lo <= n <= hi:
        raise InstanceError(f"{what} is {n}; it has to be between {lo} and {hi}")
    return n


def _gpus(value, what: str) -> list[int]:
    if value in (None, "", "all"):
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for v in items:
        n = _int(v, 0, 0, 63, what)
        if n not in out:
            out.append(n)
    return sorted(out)


def check_llamacpp_model(model: str, what: str) -> str:
    """A llama.cpp setup's model, by shape. See MODELS_DIR above."""
    if HF_RE.fullmatch(model) or (GGUF_PATH_RE.fullmatch(model) and ".." not in model):
        return model
    if MODEL_RE.fullmatch(model) and not model.startswith(("hf:", "/")):
        return model                              # an Ollama model name
    raise InstanceError(f"{what}: {model!r} -- an Ollama model name, hf:owner/repo/file.gguf, "
                        f"or a .gguf file in {MODELS_DIR}")


def normalize(entry: dict, where: str = "cloud.ollama.instances") -> dict:
    """One entry, with every field present and checked."""
    if not isinstance(entry, dict):
        raise InstanceError(f"{where}: an entry is not a mapping")
    iid = str(entry.get("id") or "").strip()
    if not ID_RE.fullmatch(iid) or iid in RESERVED_IDS:
        raise InstanceError(
            f"{where}: id {iid!r} -- lowercase letters and digits, starting with a "
            f"letter, at most 16, and not {', '.join(sorted(RESERVED_IDS))}")
    what = f"{where}[{iid}]"
    out = dict(DEFAULTS)
    out["id"] = iid
    out["label"] = str(entry.get("label") or "").strip()
    out["host"] = str(entry.get("host") or DEFAULTS["host"]).strip()
    out["port"] = _int(entry.get("port"), 0, 1, 65535, f"{what}.port") if entry.get("port") else 0
    out["url"] = str(entry.get("url") or "").strip().rstrip("/")
    if not out["port"] and not out["url"]:
        raise InstanceError(f"{what}: needs a port (or a url, for a server on another machine)")
    out["gpus"] = _gpus(entry.get("gpus", entry.get("gpu")), f"{what}.gpus")
    out["context"] = _int(entry.get("context"), DEFAULTS["context"], 512, 1 << 21, f"{what}.context")
    out["parallel"] = _int(entry.get("parallel"), DEFAULTS["parallel"], 1, 64, f"{what}.parallel")
    out["max_models"] = _int(entry.get("max_models"), DEFAULTS["max_models"], 1, 16, f"{what}.max_models")
    kv = str(entry.get("kv_cache") or DEFAULTS["kv_cache"]).strip().lower()
    if kv not in KV_TYPES:
        raise InstanceError(f"{what}.kv_cache is {kv!r}; one of {', '.join(KV_TYPES)}")
    out["kv_cache"] = kv
    out["flash_attention"] = bool(entry.get("flash_attention", True))
    out["keep_alive"] = str(entry.get("keep_alive") or "").strip()
    # Written into a unit by a root service (ollama_host.py --trigger), so
    # every value that reaches a unit file is checked here, by shape.
    if out["keep_alive"] and not KEEP_ALIVE_RE.fullmatch(out["keep_alive"]):
        raise InstanceError(f"{what}.keep_alive {out['keep_alive']!r}: a duration like 5m, 1h or -1")
    out["label"] = re.sub(r"[\x00-\x1f]", " ", out["label"])[:40]
    engine = str(entry.get("engine") or "ollama").strip()
    if engine not in ENGINES:
        raise InstanceError(f"{what}.engine is {engine!r}; one of {', '.join(ENGINES)}")
    out["engine"] = engine
    out["thinking"] = bool(entry.get("thinking", True))
    out["unit"] = str(entry.get("unit") or (f"llamacpp-{iid}" if engine != "ollama"
                                            else default_unit(iid))).strip()
    # Only Ollama's own units. The page writes this list and a root service
    # applies it; a unit name here must never be able to reach sshd.
    if not UNIT_RE.fullmatch(out["unit"]):
        raise InstanceError(f"{what}.unit {out['unit']!r}: must be ollama or ollama-<name>")
    out["managed"] = bool(entry.get("managed", False)) and not out["url"]
    purpose = str(entry.get("purpose") or "").strip()
    if purpose not in PURPOSES:
        raise InstanceError(f"{what}.purpose is {purpose!r}; blank or 'bench'")
    out["purpose"] = purpose
    out["enabled"] = bool(entry.get("enabled", True))
    model = str(entry.get("model") or "").strip()
    if engine != "ollama":
        if not model:
            raise InstanceError(f"{what}: a llama.cpp server runs one model; name it")
        check_llamacpp_model(model, f"{what}.model")
        if out["unit"] != f"llamacpp-{iid}":
            raise InstanceError(f"{what}.unit: a llama.cpp server's unit is llamacpp-{iid}")
    elif model and not MODEL_RE.fullmatch(model):
        raise InstanceError(f"{what}.model {model!r} is not an Ollama model name")
    elif out["unit"].startswith("llamacpp-"):
        raise InstanceError(f"{what}.unit: an Ollama server's unit is ollama or ollama-<name>")
    out["model"] = model
    out["pinned"] = bool(entry.get("pinned")) and bool(model)
    if model:
        # One model is the point of a setup: a second one loaded beside it
        # would share its window and evict it under pressure.
        out["max_models"] = 1
    mode = str(entry.get("gpu_mode") or "fixed").strip()
    if mode not in ("auto", "fixed"):
        raise InstanceError(f"{what}.gpu_mode is {mode!r}; auto or fixed")
    out["gpu_mode"] = mode
    out["cpu"] = bool(entry.get("cpu"))
    if out["cpu"]:
        out["gpus"], out["gpu_mode"] = [], "fixed"
    return out


def _port_of(url: str, default: int) -> int:
    m = re.search(r":(\d+)(?:/|$)", url or "")
    return int(m.group(1)) if m else default


def legacy_instances(conf: dict) -> list[dict]:
    """The list a config written before `instances` existed means.

    `local` is `main`, `vision` is `vision`, `bench` is the benchmark's. Their
    slots and max models were never in the config -- the stack did not run
    the servers -- so they come back as the defaults and unmanaged: nothing
    is written to systemd from a guess. `./home-stack ollama --import` reads
    the real values from the units and writes the list.
    """
    out = []
    if "mode" in conf and "local" not in conf and "cloud" not in conf:
        conf = {"local": {"enabled": str(conf.get("mode", "local")).lower() == "local",
                          "host": conf.get("host", "compute"), "port": conf.get("port", 11434)}}
    local = conf.get("local") or {}
    if local.get("enabled", True):
        out.append(normalize({"id": "main", "label": "", "host": local.get("host") or "compute",
                              "port": local.get("port", 11434),
                              "context": local.get("context") or None}))
    vision = conf.get("vision") or {}
    if vision.get("enabled") and str(vision.get("url") or "").strip():
        url = str(vision["url"]).strip()
        # The old entry carried a URL because it could be anywhere. On the
        # same host as `local` it is simply another port there.
        out.append(normalize({"id": "vision", "host": local.get("host") or "compute",
                              "port": _port_of(url, 11435),
                              "context": vision.get("context") or None}))
        out[-1]["legacy_url"] = url
    bench = conf.get("bench") or {}
    if bench.get("url"):
        out.append(normalize({"id": "bench", "host": local.get("host") or "compute",
                              "port": _port_of(str(bench["url"]), 11436), "purpose": "bench"}))
    return out


def instances(cfg: dict, *, enabled_only: bool = True) -> list[dict]:
    """Every local Ollama instance the config names, normalized.

    Raises InstanceError on an entry that cannot work -- a duplicate id or
    port, a bad value -- because a deploy that quietly dropped one would send
    a role's turns to a server that is not there.
    """
    conf = ((cfg.get("cloud") or {}).get("ollama") or {})
    raw = conf.get("instances")
    if raw is None:
        found = legacy_instances(conf)
    else:
        if not isinstance(raw, list):
            raise InstanceError("cloud.ollama.instances is not a list")
        found = [normalize(e) for e in raw]
    found += setup_instances(cfg, found)
    ids, ports = set(), {}
    for inst in found:
        if inst["id"] in ids:
            raise InstanceError(f"cloud.ollama.instances: id {inst['id']!r} appears twice")
        ids.add(inst["id"])
        if inst["port"] and not inst["url"]:
            key = (inst["host"], inst["port"])
            if key in ports:
                raise InstanceError(
                    f"cloud.ollama.instances: {inst['id']!r} and {ports[key]!r} both use "
                    f"port {inst['port']} on {inst['host']}")
            ports[key] = inst["id"]
    benches = [i["id"] for i in found if i["purpose"] == "bench"]
    if len(benches) > 1:
        raise InstanceError(f"cloud.ollama.instances: more than one benchmark instance ({', '.join(benches)})")
    return [i for i in found if i["enabled"]] if enabled_only else found


# ---------------------------------------------------------------------------
# Model setups (cloud.ollama.setups)
# ---------------------------------------------------------------------------
# What the household configures: a model at a window, a KV cache type and a
# number of slots, with a name and optionally a card. Everything else -- the
# server, its port, its unit, which card it lands on -- follows, and only for
# the setups a task actually uses: an unused setup costs nothing and runs
# nowhere. Each used setup is one server running that one model, kept loaded,
# because Ollama applies window, cache and slots per server.
SETUP_PORT_BASE = 11437


def normalize_setup(entry: dict) -> dict:
    where = "cloud.ollama.setups"
    if not isinstance(entry, dict):
        raise InstanceError(f"{where}: an entry is not a mapping")
    sid = str(entry.get("id") or "").strip()
    if not ID_RE.fullmatch(sid) or sid in RESERVED_IDS:
        raise InstanceError(f"{where}: id {sid!r} -- lowercase letters and digits, from a letter")
    what = f"{where}[{sid}]"
    model = str(entry.get("model") or "").strip()
    engine = str(entry.get("engine") or "ollama").strip()
    if engine not in ENGINES:
        raise InstanceError(f"{what}.engine is {engine!r}; one of {', '.join(ENGINES)}")
    if engine == "ollama" and not model:
        raise InstanceError(f"{what}: needs a model")
    # hf:… and a .gguf path are this stack's own spellings for llama.cpp; Ollama
    # would be handed a name it cannot pull, and fail only when the server starts.
    if engine == "ollama" and model.startswith(("hf:", "/")):
        raise InstanceError(f"{what}: {model!r} is a GGUF file, which only llama.cpp runs -- "
                            f"pick llama.cpp as its engine")
    if engine == "ollama" and not MODEL_RE.fullmatch(model):
        raise InstanceError(f"{what}.model {model!r} is not an Ollama model name")
    if engine != "ollama":
        check_llamacpp_model(model, f"{what}.model")
    kv = str(entry.get("kv_cache") or "q4_0").strip().lower()
    if kv not in KV_TYPES:
        raise InstanceError(f"{what}.kv_cache is {kv!r}; one of {', '.join(KV_TYPES)}")
    gpu = entry.get("gpu", "auto")
    cpu = gpu == "cpu"
    fixed = [] if gpu in (None, "", "auto", "cpu") else _gpus(gpu, f"{what}.gpu")
    return {
        "id": sid, "name": re.sub(r"[\x00-\x1f]", " ", str(entry.get("name") or "").strip())[:40],
        "model": model, "kv_cache": kv, "engine": engine,
        "thinking": bool(entry.get("thinking", True)),
        "context": _int(entry.get("context"), 8192, 512, 1 << 21, f"{what}.context"),
        "parallel": _int(entry.get("parallel"), 1, 1, 64, f"{what}.parallel"),
        "gpu": "cpu" if cpu else (fixed or "auto"),
        # Where the page placed it last, and its port once given: both kept so
        # a save does not move or renumber a server that is fine where it is.
        "gpus": [] if cpu else (fixed or _gpus(entry.get("gpus") or [], f"{what}.gpus")),
        "port": _int(entry.get("port"), 0, 0, 65535, f"{what}.port"),
    }


def setups(cfg: dict) -> list[dict]:
    raw = ((cfg.get("cloud") or {}).get("ollama") or {}).get("setups") or []
    if not isinstance(raw, list):
        raise InstanceError("cloud.ollama.setups is not a list")
    out = [normalize_setup(e) for e in raw]
    ids = [s["id"] for s in out]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise InstanceError(f"cloud.ollama.setups: {', '.join(sorted(dup))} appears twice")
    return out


def used_setup_ids(cfg: dict, ids: set[str] | None = None) -> set[str]:
    """The setups some task points at: a model setting written `ollama-<id>:`."""
    ids = ids if ids is not None else {s["id"] for s in setups(cfg)}
    values = []
    for v in ((cfg.get("assistant") or {}).get("models") or {}).values():
        values += v if isinstance(v, list) else [v]
    values.append(((cfg.get("services") or {}).get("home-paperless") or {}).get("embeddings"))
    out = set()
    for v in values:
        prefix = str(v or "").partition(":")[0]
        provider = provider_of_prefix(prefix)
        iid = id_of_provider(provider) if provider else ""
        if iid in ids:
            out.add(iid)
    return out


def setup_instances(cfg: dict, general: list[dict] | None = None) -> list[dict]:
    """The servers the used setups run on, as instances the rest of the stack
    already knows how to deploy, apply and route to."""
    try:
        all_setups = setups(cfg)
    except InstanceError:
        raise
    if not all_setups:
        return []
    general = general or []
    clash = {s["id"] for s in all_setups} & {i["id"] for i in general}
    if clash:
        raise InstanceError(f"cloud.ollama.setups: {', '.join(sorted(clash))} is also a server's id")
    used = used_setup_ids(cfg, {s["id"] for s in all_setups})
    host = next((i["host"] for i in general if i["id"] == "main"), None) or \
        ((((cfg.get("cloud") or {}).get("ollama") or {}).get("local") or {}).get("host") or "hub")
    taken = {i["port"] for i in general} | {s["port"] for s in all_setups if s["port"]}
    out = []
    for s in all_setups:
        if s["id"] not in used:
            continue
        port = s["port"]
        if not port:                        # a hand-written setup: the next free
            port = SETUP_PORT_BASE
            while port in taken:
                port += 1
            taken.add(port)
        inst = normalize({
            "id": s["id"], "label": s["name"], "host": host, "port": port,
            "gpus": s["gpus"], "gpu_mode": "fixed" if s["gpu"] != "auto" else "auto",
            "cpu": s["gpu"] == "cpu",
            "context": s["context"], "parallel": s["parallel"], "kv_cache": s["kv_cache"],
            "model": s["model"], "pinned": True, "managed": True, "max_models": 1,
            "engine": s["engine"], "thinking": s["thinking"]})
        inst["setup"] = True
        out.append(inst)
    return out


def by_id(cfg: dict) -> dict[str, dict]:
    return {i["id"]: i for i in instances(cfg)}


def serving(cfg: dict) -> list[dict]:
    """The instances a role may use: every enabled one but the benchmark's."""
    return [i for i in instances(cfg) if i["purpose"] != "bench"]


def bench_instance(cfg: dict) -> dict | None:
    return next((i for i in instances(cfg) if i["purpose"] == "bench"), None)


def short_model(model: str) -> str:
    """A model as a person reads it: a GGUF file (hf:owner/repo/file.gguf, or a
    path in MODELS_DIR) by its file name; an Ollama name as it is."""
    m = HF_RE.fullmatch(model)
    if m:
        return m.group(3).rsplit("/", 1)[-1]
    return model.rsplit("/", 1)[-1] if model.startswith("/") else model


def display(inst: dict) -> str:
    """"Notifications · GPU1 · 64k × 2" -- what the picker and the plan call it.
    A model setup is named by its model and its cache type."""
    name = inst["label"] or inst["id"]
    if inst.get("model"):
        name = f"{short_model(inst['model'])} · {inst['kv_cache']}"
        if inst.get("engine", "ollama") != "ollama":
            name += f" · {ENGINE_LABELS[inst['engine']]}"
    where = ("CPU" if inst.get("cpu") else
             ("GPU " + "+".join(str(g) for g in inst["gpus"])) if inst["gpus"] else "any GPU")
    ctx = inst["context"]
    ctx_s = f"{ctx // 1024}k" if ctx % 1024 == 0 else str(ctx)
    return f"{name} · {where} · {ctx_s} × {inst['parallel']}"


# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------
# One drop-in per managed unit, named to sort last, so it wins over whatever
# the household's own drop-ins say without anybody's file being deleted. The
# device list is reset first: `DeviceAllow=` accumulates across drop-ins, and
# an earlier pin to another card would otherwise stay allowed.
DROPIN_NAME = "zz-home-stack.conf"
NVIDIA_SHARED = ("/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools", "/dev/nvidia-modeset")


def desired_env(inst: dict) -> dict[str, str]:
    env = {
        "OLLAMA_HOST": f"0.0.0.0:{inst['port']}",
        "OLLAMA_FLASH_ATTENTION": "1" if inst["flash_attention"] else "0",
        "OLLAMA_KV_CACHE_TYPE": inst["kv_cache"],
        "OLLAMA_NUM_PARALLEL": str(inst["parallel"]),
        "OLLAMA_MAX_LOADED_MODELS": str(inst["max_models"]),
        "OLLAMA_CONTEXT_LENGTH": str(inst["context"]),
    }
    if inst.get("pinned"):
        env["OLLAMA_KEEP_ALIVE"] = "-1"
    elif inst["keep_alive"]:
        env["OLLAMA_KEEP_ALIVE"] = inst["keep_alive"]
    return env


def signature(inst: dict) -> str:
    """What an apply would make of *inst*, as one string: the host records it
    with each plan, and the page compares it with the list as saved now, so a
    save shows as "to apply" at once rather than after the host's next look."""
    return "|".join([*(f"{k}={v}" for k, v in sorted(desired_env(inst).items())),
                     "gpus=" + ",".join(map(str, inst["gpus"])),
                     f"enabled={inst['enabled']}", f"managed={inst['managed']}",
                     # Only when set: a general server's signature stays what
                     # an older helper computed, so an upgrade does not show
                     # every server as "to apply" until the host looks again.
                     *([f"model={inst['model']}"] if inst.get("model") else []),
                     *(["pinned=True"] if inst.get("pinned") else []),
                     *(["cpu=True"] if inst.get("cpu") else []),
                     *([f"engine={inst['engine']}"] if inst.get("engine", "ollama") != "ollama" else []),
                     *(["thinking=False"] if not inst.get("thinking", True) else [])])


def desired_devices(inst: dict, minors: dict[int, int] | None = None) -> list[str]:
    """Device nodes the unit may open; [] means not pinned.

    *minors* maps an nvidia-smi index to its device node's minor number --
    they are usually equal, and not guaranteed to be (docs/local-ollama.md).
    """
    if not inst["gpus"]:
        return []
    minors = minors or {}
    return sorted([f"/dev/nvidia{minors.get(g, g)}" for g in inst["gpus"]] + list(NVIDIA_SHARED))


def render_dropin(inst: dict, minors: dict[int, int] | None = None) -> str:
    lines = ["# Written by ./home-stack ollama from cloud.ollama.instances "
             f"(id: {inst['id']}).",
             "# Edit the instance on the admin page's Models tab, not this file:",
             "# the next apply rewrites it.",
             "[Service]"]
    lines += [f"Environment={k}={v}" for k, v in desired_env(inst).items()]
    devices = desired_devices(inst, minors)
    if inst.get("cpu"):
        # Closed with nothing allowed: the standard pseudo-devices only, no
        # card. Ollama finds no GPU and runs on the CPU.
        lines += ["DevicePolicy=closed", "DeviceAllow="]
    elif devices:
        # Ollama finds cards through NVML, which ignores CUDA_VISIBLE_DEVICES,
        # so a pin is a device-node denial.
        lines += ["DevicePolicy=closed", "DeviceAllow="]
        lines += [f"DeviceAllow={d} rw" for d in devices]
    else:
        lines += ["DevicePolicy=auto", "DeviceAllow="]
    return "\n".join(lines) + "\n"


def render_unit(inst: dict, ollama_bin: str = "/usr/local/bin/ollama") -> str:
    """A whole unit, for an instance whose unit does not exist yet.

    Never written over an existing unit file: the main one is the Ollama
    installer's, and the household may have edited the others by hand.
    Everything that varies lives in the drop-in.
    """
    return (
        "[Unit]\n"
        f"Description=Ollama ({inst['label'] or inst['id']}) - managed by home-stack\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={ollama_bin} serve\n"
        "User=ollama\nGroup=ollama\nRestart=always\nRestartSec=3\n"
        'Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin"\n\n'
        "[Install]\nWantedBy=multi-user.target\n")


def parse_show(text: str) -> dict:
    """`systemctl show -p Environment -p DevicePolicy -p DeviceAllow ...` output."""
    out: dict = {"env": {}, "devices": [], "policy": "", "props": {}}
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "Environment":
            for item in _split_env(value):
                k, _, v = item.partition("=")
                out["env"][k] = v
        elif key == "DeviceAllow":
            node = value.split()[0] if value.split() else ""
            if node:
                out["devices"].append(node)
        elif key == "DevicePolicy":
            out["policy"] = value
        else:
            out["props"][key] = value
    out["devices"].sort()
    return out


def _split_env(value: str) -> list[str]:
    items, cur, quote = [], "", ""
    for ch in value:
        if quote:
            if ch == quote:
                quote = ""
            else:
                cur += ch
        elif ch in "\"'":
            quote = ch
        elif ch == " ":
            if cur:
                items.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        items.append(cur)
    return items


def diff(inst: dict, shown: dict, minors: dict[int, int] | None = None) -> list[str]:
    """What applying would change on a running unit, one line each. [] = nothing."""
    changes = []
    if shown["props"].get("LoadState") not in ("loaded",):
        return [f"create {inst['unit']}.service"]
    for k, v in desired_env(inst).items():
        have = shown["env"].get(k)
        if have != v:
            changes.append(f"{k}: {have if have is not None else '(unset)'} -> {v}")
    if not inst["keep_alive"] and not inst.get("pinned") and "OLLAMA_KEEP_ALIVE" in shown["env"]:
        # Not written means "Ollama's default"; an old drop-in's value would
        # survive ours, so it has to be said.
        changes.append(f"OLLAMA_KEEP_ALIVE: {shown['env']['OLLAMA_KEEP_ALIVE']} -> (default)")
    want_dev = desired_devices(inst, minors)
    if inst.get("cpu"):
        if shown["policy"] != "closed" or any(d.startswith("/dev/nvidia") for d in shown["devices"]):
            have = [d for d in shown["devices"] if d not in NVIDIA_SHARED] or ["any"]
            changes.append(f"cards: {', '.join(have)} -> CPU only")
    elif want_dev:
        if shown["policy"] != "closed" or shown["devices"] != want_dev:
            have = [d for d in shown["devices"] if d not in NVIDIA_SHARED] or ["any"]
            want = [d for d in want_dev if d not in NVIDIA_SHARED]
            changes.append(f"cards: {', '.join(have)} -> {', '.join(want)}")
    elif shown["policy"] == "closed":
        have = [d for d in shown["devices"] if d not in NVIDIA_SHARED]
        changes.append(f"cards: {', '.join(have) or 'none'} -> any")
    return changes


def instance_from_unit(iid: str, unit: str, shown: dict, index_of_minor: dict[int, int] | None = None,
                       **extra) -> dict:
    """An instance entry that describes a running unit exactly -- for --import."""
    env = shown["env"]
    host_port = env.get("OLLAMA_HOST", "")
    port = _port_of(host_port if host_port.startswith("http") else f"x://{host_port}/", 11434)
    minors = [int(m.group(1)) for d in shown["devices"]
              for m in [re.fullmatch(r"/dev/nvidia(\d+)", d)] if m]
    index_of_minor = index_of_minor or {}
    gpus = sorted(index_of_minor.get(m, m) for m in minors) if shown["policy"] == "closed" else []
    entry = {
        "id": iid, "unit": unit, "port": port, "gpus": gpus,
        "context": int(env.get("OLLAMA_CONTEXT_LENGTH") or 2048),
        "parallel": int(env.get("OLLAMA_NUM_PARALLEL") or 1),
        "max_models": int(env.get("OLLAMA_MAX_LOADED_MODELS") or 1),
        "kv_cache": (env.get("OLLAMA_KV_CACHE_TYPE") or "f16").lower(),
        "flash_attention": env.get("OLLAMA_FLASH_ATTENTION", "0") in ("1", "true"),
        "keep_alive": env.get("OLLAMA_KEEP_ALIVE", ""),
        "managed": True,
    }
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# llama.cpp servers
# ---------------------------------------------------------------------------
# A llama.cpp setup's unit is the stack's whole: it is rendered every apply,
# compared as text, and replaced when it differs. Every value in it has been
# through normalize() and the model path through the host's resolver, which
# only returns paths inside Ollama's store or MODELS_DIR.
def llamacpp_args(inst: dict, model_path: str) -> list[str]:
    args = ["-m", model_path, "--alias", inst["model"],
            "--host", "0.0.0.0", "--port", str(inst["port"]),
            # llama.cpp's -c is the whole cache, shared by the slots; the page
            # speaks of a window *per slot*, as Ollama does.
            "-c", str(inst["context"] * inst["parallel"]), "-np", str(inst["parallel"]),
            "-ngl", "0" if inst.get("cpu") else "999",
            "-fa", "on" if inst["flash_attention"] else "off",
            "-ctk", inst["kv_cache"], "-ctv", inst["kv_cache"], "--jinja"]
    if not inst.get("thinking", True):
        args += ["--reasoning-budget", "0"]
    return args


def render_llamacpp_unit(inst: dict, bin_dir: str, model_path: str,
                         minors: dict[int, int] | None = None) -> str:
    import shlex
    exec_start = " ".join(shlex.quote(a) for a in [f"{bin_dir}/llama-server",
                                                    *llamacpp_args(inst, model_path)])
    lines = ["[Unit]",
             f"Description=llama.cpp ({inst['label'] or inst['id']}) - managed by home-stack",
             "After=network-online.target",
             # A model llama.cpp cannot load fails in two seconds, every time:
             # three tries, then the unit stays failed and says why, instead
             # of a restart loop nobody sees (gemma4:e4b from Ollama's
             # library, 2026-09-24).
             "StartLimitIntervalSec=300", "StartLimitBurst=3", "",
             "[Service]",
             f"ExecStart={exec_start}",
             f"Environment=LD_LIBRARY_PATH={bin_dir}",
             # The ollama user: it can read Ollama's store, where most of the
             # household's GGUFs already are.
             "User=ollama", "Group=ollama", "Restart=on-failure", "RestartSec=5"]
    devices = desired_devices(inst, minors)
    if inst.get("cpu"):
        lines += ["DevicePolicy=closed", "DeviceAllow="]
    elif devices:
        lines += ["DevicePolicy=closed", "DeviceAllow="]
        lines += [f"DeviceAllow={d} rw" for d in devices]
    lines += ["", "[Install]", "WantedBy=multi-user.target", ""]
    return "\n".join(lines)

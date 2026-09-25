"""How much card memory each local Ollama instance needs, and whether it fits.

Two answers, and the page says which one it is showing:

* **measured** -- the instance was asked to load its models (`/api/generate`
  with no prompt) and `/api/ps` said what each takes. Ollama's own figure
  leaves out the CUDA context and compute buffers -- gemma4:e4b at 40k x 1 is
  3.14 GiB in `/api/ps` and 4.47 GiB on the card -- so the host's inventory
  (`./home-stack ollama`, gpus.json) supplies the unit's real total when it has
  one, and the difference is kept as that instance's overhead.
* **estimated** -- from the model's own metadata (`/api/show`): the weights
  from the file size, and the KV cache from its layers, heads and window,
  times the instance's context x slots at its cache type. A measurement of the
  same model is preferred as the anchor, and only the KV difference is added.

The KV arithmetic follows the model: gemma4's 42 layers are 18 that reuse
another layer's cache and 24 that keep one, of which 20 slide over 512 tokens
-- which is how 4,608 bytes per token (docs/local-ollama.md, measured) comes
out of the metadata rather than being typed in.
"""
from __future__ import annotations

import json
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path

GIB = 1024 ** 3
# Bytes per element of the KV cache, by OLLAMA_KV_CACHE_TYPE. The quantized
# ones carry a scale per 32 values.
KV_BYTES = {"f16": 2.0, "q8_0": 34 / 32, "q4_0": 18 / 32}
# CUDA context and compute buffers, per instance per card, when nothing
# measured says otherwise. 1.3 GiB was measured for gemma4 at 40k x 1.
DEFAULT_OVERHEAD = 1.0 * GIB
# llama.cpp's compute buffers, beyond weights and cache: 1.24 GiB for Bonsai 2
# 27B at 2 x 96k with the default batch (2026-09-24; 2 x 128k failed on it).
LLAMACPP_OVERHEAD = 1.3 * GIB


def _post(url: str, path: str, body: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _get(url: str, path: str, timeout: int = 8) -> dict:
    with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as r:
        return json.load(r)


# ---------------------------------------------------------------------------
# What a model is
# ---------------------------------------------------------------------------
def model_facts(url: str, name: str, tags: dict | None = None) -> dict:
    """{"file": bytes, "layers": [(tokens_kind, n_kv, k, v)...], "window": n} or {}."""
    try:
        show = _post(url, "/api/show", {"model": name}, timeout=15)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return {}
    return facts_from_info(show.get("model_info") or {}, int((tags or {}).get(name) or 0))


def facts_from_info(info: dict, size: int) -> dict:
    """The facts an estimate needs, from a GGUF's metadata (Ollama's
    `model_info` is the same keys) and the file's size."""
    arch = info.get("general.architecture") or ""
    g = lambda k, d=None: info.get(f"{arch}.{k}", d)          # noqa: E731
    n_layers = int(g("block_count") or 0)
    heads = g("attention.head_count") or 1
    kv_heads = g("attention.head_count_kv", heads)
    emb = int(g("embedding_length") or 0)
    head_dim = emb // (heads if isinstance(heads, int) else max(heads)) if emb else 128
    k = int(g("attention.key_length") or head_dim)
    v = int(g("attention.value_length") or k)
    k_swa = int(g("attention.key_length_swa") or k)
    v_swa = int(g("attention.value_length_swa") or v)
    window = int(g("attention.sliding_window") or 0)
    pattern = g("attention.sliding_window_pattern")
    shared = int(g("attention.shared_kv_layers") or 0)
    # A hybrid model (Qwen3.5/3.6/3.8) keeps a cache only on its attention
    # layers. Ollama's metadata lists heads per layer with zeros elsewhere;
    # a GGUF from llama.cpp's converter says "every Nth layer" instead.
    interval = int(g("full_attention_interval") or 0)
    layers = []
    for i in range(max(0, n_layers - shared)):
        nkv = kv_heads[i] if isinstance(kv_heads, list) else kv_heads
        if not nkv or (interval and not isinstance(kv_heads, list) and (i + 1) % interval):
            continue                       # a layer with no attention (hybrid models)
        swa = bool(window) and isinstance(pattern, list) and i < len(pattern) and bool(pattern[i])
        layers.append(("swa" if swa else "full", int(nkv), k_swa if swa else k, v_swa if swa else v))
    # Per-layer embeddings (gemma4's "e" models) are a table Ollama keeps in
    # host RAM: 5.4 GB of gemma4:e4b's 9.6 GB file never reaches the card.
    # Stored at 16 bits; vocabulary as the model states it, else Gemma's.
    ple_dim = int(g("embedding_length_per_layer_input") or 0)
    if ple_dim and size:
        vocab = int(g("vocab_size") or 262144)
        size = max(size // 4, size - vocab * n_layers * ple_dim * 2)
    return {"file": size, "layers": layers, "window": window,
            "arch": arch, "context_max": int(g("context_length") or 0)}


# ---------------------------------------------------------------------------
# GGUF metadata, for a model Ollama does not have (a llama.cpp setup's hf: file)
# ---------------------------------------------------------------------------
# The metadata sits at the front of the file, before the tensors: a few MB,
# most of it the tokenizer's vocabulary, which is skipped rather than kept.
# Read from a local file, or from a URL by range requests -- so a 7 GB model
# can be sized before anybody downloads it.
_GGUF_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?",
                 10: "<Q", 11: "<q", 12: "<d"}


class _Source:
    def __init__(self, where: str):
        self.where, self.buf, self.size = where, b"", 0
        self.remote = where.startswith(("http://", "https://"))
        if not self.remote:
            self.size = Path(where).stat().st_size

    def need(self, end: int) -> None:
        if end <= len(self.buf):
            return
        want = max(end, len(self.buf) * 2, 1 << 20)
        if want > (96 << 20):
            raise ValueError("GGUF metadata larger than 96 MB")
        if self.remote:
            # Only the bytes not already here: doubling from zero each time
            # fetched a vocabulary-sized header two times over.
            start = len(self.buf)
            req = urllib.request.Request(self.where, headers={"Range": f"bytes={start}-{want - 1}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status == 206:
                    self.buf += r.read(want - start)
                else:                      # no ranges: the whole file, of which the front
                    self.buf = r.read(want)
                total = (r.headers.get("Content-Range") or "").rpartition("/")[2]
                self.size = int(total) if total.isdigit() else self.size
        else:
            with open(self.where, "rb") as fh:
                self.buf = fh.read(want)
        if end > len(self.buf):
            raise ValueError("GGUF file ends inside its metadata")


def gguf_info(where: str) -> tuple[dict, int]:
    """(metadata, file size) of a GGUF file or URL. Arrays are kept only when
    short -- the per-layer ones a hybrid model's estimate reads."""
    src, pos = _Source(where), 0

    def take(n: int) -> bytes:
        nonlocal pos
        src.need(pos + n)
        out = src.buf[pos:pos + n]
        pos += n
        return out

    def scalar(t: int):
        fmt = _GGUF_SCALARS[t]
        return struct.unpack(fmt, take(struct.calcsize(fmt)))[0]

    def string() -> str:
        (n,) = struct.unpack("<Q", take(8))
        return take(n).decode("utf-8", "replace")

    def value(t: int):
        if t == 8:
            return string()
        if t == 9:
            (et,) = struct.unpack("<I", take(4))
            (n,) = struct.unpack("<Q", take(8))
            if n > 4096:                         # a vocabulary: skip it
                for _ in range(n):
                    value(et)
                return None
            return [value(et) for _ in range(n)]
        return scalar(t)

    if take(4) != b"GGUF":
        raise ValueError("not a GGUF file")
    struct.unpack("<I", take(4))
    _tensors, n_kv = struct.unpack("<QQ", take(16))
    info = {}
    for _ in range(n_kv):
        key = string()
        (t,) = struct.unpack("<I", take(4))
        info[key] = value(t)
    return info, src.size


def gguf_facts(where: str) -> dict:
    try:
        info, size = gguf_info(where)
    except (OSError, ValueError, KeyError, struct.error, urllib.error.URLError) as exc:
        return {"error": str(exc)[:200]}
    return facts_from_info(info, size)


def kv_bytes(facts: dict, context: int, parallel: int, kv_cache: str) -> float:
    per = KV_BYTES.get(kv_cache, 2.0)
    total = 0.0
    for kind, nkv, k, v in facts.get("layers") or []:
        tokens = min(context, facts.get("window") or context) if kind == "swa" else context
        total += tokens * nkv * (k + v) * per
    return total * parallel


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------
def load_measured(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_measured(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    tmp.replace(path)


def measure(url: str, models: list[str], inst: dict) -> tuple[list[dict], str]:
    """Load *models* on the instance and read what each takes. Never sends a
    window of its own: that would reload the model at another size."""
    for name in models:
        try:
            _post(url, "/api/generate", {"model": name, "prompt": "", "keep_alive": "10m"}, timeout=300)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            return [], f"{name}: {exc}"
    try:
        loaded = _get(url, "/api/ps").get("models") or []
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return [], str(exc)
    out = []
    for m in loaded:
        out.append({"model": m.get("name") or m.get("model"), "vram": int(m.get("size_vram") or 0),
                    "size": int(m.get("size") or 0), "context": int(m.get("context_length") or 0),
                    "parallel": inst["parallel"], "kv_cache": inst["kv_cache"], "at": int(time.time())})
    return out, ""


# ---------------------------------------------------------------------------
# The estimate
# ---------------------------------------------------------------------------
def model_need(name: str, facts: dict, inst: dict, measured: dict) -> tuple[float, str]:
    """(bytes on the card, "measured"|"estimated"|"unknown") for one model on *inst*."""
    point = (measured.get("models") or {}).get(name)
    want_kv = kv_bytes(facts, inst["context"], inst["parallel"], inst["kv_cache"]) if facts else 0.0
    if point:
        had_kv = kv_bytes(facts, point["context"], point["parallel"], point["kv_cache"]) if facts else 0.0
        same = (point["context"] == inst["context"] and point["parallel"] == inst["parallel"]
                and point["kv_cache"] == inst["kv_cache"])
        return max(0.0, point["vram"] - had_kv + want_kv), "measured" if same else "estimated"
    if not facts or not facts.get("file"):
        return 0.0, "unknown"
    return facts["file"] + want_kv, "estimated"


def instance_need(inst: dict, models: list[str], facts: dict[str, dict], measured: dict) -> dict:
    """What one instance needs with the models its roles use loaded at once
    (up to its max_models, largest first), plus its overhead."""
    rows = []
    for name in models:
        need, how = model_need(name, facts.get(name) or {}, inst, measured)
        rows.append({"model": name, "bytes": need, "how": how})
    rows.sort(key=lambda r: -r["bytes"])
    resident = rows[:inst["max_models"]]
    # *measured* is this instance's own record (admin/app.py ollama_measure):
    # its models, and the overhead the host saw beyond what /api/ps reports.
    overhead = measured.get("overhead") or (LLAMACPP_OVERHEAD if inst.get("engine", "ollama") != "ollama"
                                            else DEFAULT_OVERHEAD)
    total = sum(r["bytes"] for r in resident) + (overhead if resident else 0)
    hows = {r["how"] for r in resident}
    how = ("unknown" if "unknown" in hows else "estimated" if "estimated" in hows
           else "measured" if hows else "idle")
    return {"id": inst["id"], "models": rows, "resident": [r["model"] for r in resident],
            "overhead": overhead if resident else 0, "bytes": total, "how": how}


def gpu_view(gpus: list[dict], instances: list[dict], needs: dict[str, dict],
             measured: bool = True) -> list[dict]:
    """Per card: its size, what else is on it, each instance's share, and what is left.

    An instance on several cards is counted as an even split -- how Ollama
    places a model that does not fit one card is its own business, and the
    page says "split" rather than pretending to know the layout.

    The house's own servers (Ollama's and llama.cpp's units) are never "other
    services": a llama.cpp server was, and GPU0 showed the Chat setup twice --
    its estimate and its process. With *measured*, a server the host saw on
    this card is drawn at what it actually holds; the Preview passes False,
    because what it draws is not running yet.
    """
    own = ("unit ollama", "unit llamacpp-")
    out = []
    for g in gpus:
        idx = g["index"]
        tenants = g.get("tenants") or []
        others = [t for t in tenants if not str(t.get("owner", "")).startswith(own)]
        seen = {str(t.get("owner", ""))[len("unit "):]: t for t in tenants
                if str(t.get("owner", "")).startswith(own)}
        rows = []
        for inst in instances:
            cards = inst["gpus"]
            if cards and idx not in cards:
                continue
            if not cards:
                continue                                      # unpinned: shown apart
            share, how = needs[inst["id"]]["bytes"] / len(cards), needs[inst["id"]]["how"]
            live = seen.get(inst.get("unit") or "") if measured else None
            if live and live.get("mib"):
                share, how = live["mib"] * 1024 * 1024, "measured"
            elif measured and inst.get("unit") and not inst.get("setup") and not inst.get("model"):
                # A general server the host saw nothing of on this card holds
                # nothing there now -- its main one was drawn at a 1 GiB
                # guess while it held no model at all. A setup not seen yet
                # keeps its estimate: that is what it will take once applied.
                share, how = 0.0, "idle"
            rows.append({"id": inst["id"], "bytes": share, "how": how,
                         "split": len(cards) > 1})
        total = g["total_mib"] * 1024 * 1024
        used = sum(t["mib"] for t in others) * 1024 * 1024 + sum(r["bytes"] for r in rows)
        out.append({"index": idx, "name": g.get("name", ""), "total": total,
                    "others": [{"owner": t["owner"], "bytes": t["mib"] * 1024 * 1024} for t in others],
                    "instances": rows, "free": total - used, "fits": used <= total * 0.97})
    return out


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------
# Kept free on every card for what does not show up in any estimate: a
# transient synthesis on the voice card was what failed a transcription at
# 11.2 of 12 GB (docs/local-ollama.md).
MARGIN = 0.6 * GIB


def place(gpus: list[dict], instances: list[dict], needs: dict[str, float]) -> dict:
    """Cards for every instance whose gpu_mode is "auto".

    `needs` is bytes per instance id. Fixed instances are counted where they
    are; the automatic ones go largest first, each onto the card with the most
    room left that holds it -- which spreads the load rather than filling one
    card and leaving the other idle. One that fits no card alone is split over
    all of them when each holds an even share, and otherwise put where
    there is most room and reported: the page then says it does not fit.

    Returns {"gpus": {id: [indices]}, "unplaced": [ids that did not fit]}.
    """
    free = {}
    for g in gpus:
        others = sum(t["mib"] for t in g.get("tenants") or []
                     if not str(t.get("owner", "")).startswith("unit ollama"))
        free[g["index"]] = g["total_mib"] * 1024 * 1024 - others * 1024 * 1024 - MARGIN
    out: dict[str, list[int]] = {}
    unplaced = []
    # A disabled server holds no memory and is not placed: counting it would
    # push a running one onto the crowded card for room nothing uses.
    instances = [i for i in instances if i.get("enabled", True)]
    for inst in instances:
        if inst.get("gpu_mode") != "auto" and inst["gpus"]:
            share = needs.get(inst["id"], 0.0) / len(inst["gpus"])
            for gi in inst["gpus"]:
                if gi in free:
                    free[gi] -= share
    instances = [i for i in instances if not i.get("cpu") and i.get("enabled", True)]
    auto = sorted((i for i in instances if i.get("gpu_mode") == "auto"),
                  key=lambda i: -needs.get(i["id"], 0.0))
    # Stable first: an automatic server already on a card that still holds it
    # stays there. Moving it is a restart and an unload, for nothing.
    rest = []
    for inst in auto:
        need, cur = needs.get(inst["id"], 0.0), [g for g in inst["gpus"] if g in free]
        if cur and all(free[g] >= need / len(cur) for g in cur):
            out[inst["id"]] = cur
            for g in cur:
                free[g] -= need / len(cur)
        else:
            rest.append(inst)
    for inst in rest:
        need = needs.get(inst["id"], 0.0)
        if not free:
            out[inst["id"]] = []
            continue
        best = max(free, key=lambda k: free[k])
        if free[best] >= need:
            out[inst["id"]] = [best]
            free[best] -= need
        elif len(free) > 1 and all(v >= need / len(free) for v in free.values()):
            # An even share on each card, which is how gpu_view counts a
            # split -- so each card has to hold its share, not just the sum.
            cards = sorted(free)
            out[inst["id"]] = cards
            for k in cards:
                free[k] -= need / len(cards)
        else:
            out[inst["id"]] = [best]
            free[best] -= need
            unplaced.append(inst["id"])
    return {"gpus": out, "unplaced": unplaced}

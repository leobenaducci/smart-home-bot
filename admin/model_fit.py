"""Fit a model to one card: which of a Hugging Face repo's GGUF quants fits,
at what context, and which one to take.

The household names a repository and a card; this reads the repository's file
list and one file's metadata (by range requests -- nothing is downloaded), and
for every quant works out what it needs on the card at the context, cache type
and slot count asked for, using the same estimate the Models page draws its
cards with (ollama_vram). The largest quant that fits the context is the
recommendation: at one base model, a bigger file is the better one.

What it knows it cannot see: a vision projector (a separate mmproj file), and
a hybrid model's recurrent state -- ~0.2 GiB on the Qwen3.5 family, inside the
margin.
"""
from __future__ import annotations

import json
import re
import urllib.request

import ollama_vram as V

GIB = V.GIB
# Kept free on the card after the estimate: the estimate is not a measurement.
MARGIN = 0.5 * GIB
CONTEXTS = (4096, 8192, 16384, 32768, 49152, 65536, 98304, 131072, 196608, 262144)
REPO_RE = re.compile(r"[A-Za-z0-9][\w.\-]{0,95}/[A-Za-z0-9][\w.\-]{0,95}")
# The quant is the file name's last part: ...-Q4_K_M.gguf, ...-PQ2_0.gguf, ..._IQ3_XXS.gguf
QUANT_RE = re.compile(r"[-_.]((?:UD-)?(?:IQ|Q|TQ|PQ|PTQ)\d[A-Z0-9_]*|BF16|F16|F32)\.gguf$", re.I)
SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$")
# PrismML's ternary packings: stock llama.cpp and Ollama load them as garbage.
PRISM_ONLY = re.compile(r"^(PQ|PTQ)\d", re.I)
# Qwen's own sampling advice for the family, which Ollama's library carries and
# a bare GGUF import does not.
QWEN35_PARAMS = {"temperature": 1, "top_k": 20, "top_p": 0.95, "presence_penalty": 1.5}


class FitError(ValueError):
    pass


def parse_repo(text: str) -> str:
    """owner/name from what a person pastes: the id, a huggingface.co or hf.co
    link, with or without a file path after it."""
    t = (text or "").strip()
    t = re.sub(r"^(https?://)?(www\.)?(huggingface\.co|hf\.co)/", "", t)
    t = re.sub(r"^hf:", "", t)
    parts = [p for p in t.split("/") if p][:2]
    repo = "/".join(parts)
    if not REPO_RE.fullmatch(repo):
        raise FitError("a repository is owner/name, as on huggingface.co")
    return repo


def repo_files(repo: str) -> list[dict]:
    with urllib.request.urlopen(
            f"https://huggingface.co/api/models/{repo}/tree/main?recursive=true", timeout=20) as r:
        tree = json.load(r)
    return [{"path": f["path"], "size": int((f.get("lfs") or {}).get("size") or f.get("size") or 0)}
            for f in tree if isinstance(f, dict) and f.get("type") == "file"]


def quants(files: list[dict]) -> tuple[list[dict], list[str]]:
    """The single-file GGUF quants, and the names of the ones skipped (split
    across files, which the library does not download)."""
    out, split = [], set()
    for f in files:
        path = f["path"]
        # Only model files: not a vision projector, and not the importance
        # matrix bartowski ships beside the quants (`...-imatrix.gguf`), which
        # is the smallest .gguf in the repo and has no architecture in it.
        low = path.lower()
        if not low.endswith(".gguf") or "mmproj" in low or "imatrix" in low:
            continue
        if SPLIT_RE.search(path):
            m = QUANT_RE.search(SPLIT_RE.sub(".gguf", path))
            split.add(m.group(1).upper() if m else path)
            continue
        m = QUANT_RE.search(path)
        if m:
            out.append({"path": path, "size": f["size"], "quant": m.group(1).upper()})
    return sorted(out, key=lambda q: -q["size"]), sorted(split)


def inspect(url: str) -> dict:
    """What one file's metadata says: the arch, and whether its chat template
    refuses a conversation that ends in tool results -- NeoHorse's and
    Ornith's did ("No user query found in messages"), and every tool-using
    turn after the first call failed with a 500."""
    info, _size = V.gguf_info(url)
    template = str(info.get("tokenizer.chat_template") or "")
    return {"info": info, "arch": info.get("general.architecture") or "",
            "template_raises": "No user query found" in template}


def need(info: dict, size: int, context: int, parallel: int, kv_cache: str, engine: str) -> float:
    facts = V.facts_from_info(info, size)
    overhead = V.LLAMACPP_OVERHEAD if engine in ("llamacpp", "prism") else V.DEFAULT_OVERHEAD
    return facts["file"] + V.kv_bytes(facts, context, parallel, kv_cache) + overhead


def max_context(info: dict, size: int, budget: float, parallel: int, kv_cache: str,
                engine: str) -> int:
    top = int(V.facts_from_info(info, size).get("context_max") or CONTEXTS[-1])
    best = 0
    for c in CONTEXTS:
        if c > top:
            break
        if need(info, size, c, parallel, kv_cache, engine) + MARGIN <= budget:
            best = c
    return best


def plan(repo: str, files: list[dict], info: dict, budget: float, context: int, parallel: int,
         kv_cache: str, engine: str) -> dict:
    rows, split = quants(files)
    for q in rows:
        eng = "prism" if PRISM_ONLY.match(q["quant"]) else engine
        n = need(info, q["size"], context, parallel, kv_cache, eng)
        q.update({"model": f"hf:{repo}/{q['path']}", "engine": eng,
                  "gib": round(q["size"] / GIB, 2), "need_gib": round(n / GIB, 2),
                  "fits": n + MARGIN <= budget,
                  "max_context": max_context(info, q["size"], budget, parallel, kv_cache, eng)})
    fitting = [q for q in rows if q["fits"]]
    best = fitting[0] if fitting else None
    return {"repo": repo, "rows": rows, "split": split,
            "recommended": best["path"] if best else "",
            "budget_gib": round(budget / GIB, 2), "context": context, "parallel": parallel,
            "kv_cache": kv_cache, "engine": engine,
            "context_max": int(V.facts_from_info(info, 0).get("context_max") or 0)}


def card_budget(gpu: dict, whole: bool) -> float:
    """Bytes a model may use on *gpu*: the whole card less what is not a model
    server (speech, cameras), or what is free right now."""
    total = int(gpu.get("total_mib") or 0) * 1024 * 1024
    if not whole:
        return max(0.0, total - int(gpu.get("used_mib") or 0) * 1024 * 1024)
    others = sum(int(t.get("mib") or 0) for t in gpu.get("tenants") or []
                 if not str(t.get("owner", "")).startswith(("unit ollama", "unit llamacpp")))
    return max(0.0, total - others * 1024 * 1024)


def ollama_name(repo: str, quant: str) -> str:
    """The name an imported file gets in Ollama: the repo's model name and the
    quant, lower case -- mimo-v2.6-distill-qwen-9b:q4_k_m."""
    base = re.sub(r"[-_.]?gguf$", "", repo.split("/", 1)[1], flags=re.I).lower()
    base = re.sub(r"[^a-z0-9._-]+", "-", base).strip("-.") or "model"
    tag = re.sub(r"[^a-z0-9._-]+", "-", quant.lower()).strip("-.") or "latest"
    return f"{base[:60]}:{tag[:30]}"

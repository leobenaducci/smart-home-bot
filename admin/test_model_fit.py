#!/usr/bin/env python3
"""model_fit.py: which quant of a repo fits a card, and at what window.

Plain script, like the other admin suites. No network: the file lists are
invented and the metadata is a small GGUF header written here.
"""
from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
import model_fit as F  # noqa: E402

failed = []
GIB = F.GIB


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  <- {detail}"))
    if not ok:
        failed.append(name)


def gguf(path: Path, kv: dict) -> None:
    """A GGUF header with these metadata keys and no tensors."""
    def s(x: str) -> bytes:
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    out = b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", 0, len(kv))
    for k, v in kv.items():
        out += s(k)
        if isinstance(v, str):
            out += struct.pack("<I", 8) + s(v)
        else:
            out += struct.pack("<I", 4) + struct.pack("<I", v)
    path.write_bytes(out)


print("repositories")
for text in ("bartowski/MiMo-GGUF", "https://huggingface.co/bartowski/MiMo-GGUF",
             "hf.co/bartowski/MiMo-GGUF/blob/main/x.gguf", "hf:bartowski/MiMo-GGUF/x.gguf"):
    check(f"  {text!r} is bartowski/MiMo-GGUF", F.parse_repo(text) == "bartowski/MiMo-GGUF",
          F.parse_repo(text))
try:
    F.parse_repo("../../etc")
    check("a path is not a repository", False)
except F.FitError:
    check("a path is not a repository", True)

print("\nquants")
files = [{"path": "M-Q4_K_M.gguf", "size": 5 * GIB}, {"path": "M-Q8_0.gguf", "size": 9 * GIB},
         {"path": "M-imatrix.gguf", "size": 5_000_000}, {"path": "mmproj-M-f16.gguf", "size": GIB},
         {"path": "M-BF16-00001-of-00002.gguf", "size": 9 * GIB},
         {"path": "M-PQ2_0.gguf", "size": 6 * GIB}, {"path": "README.md", "size": 10}]
rows, split = F.quants(files)
check("model files only, largest first",
      [r["quant"] for r in rows] == ["Q8_0", "PQ2_0", "Q4_K_M"], rows)
check("  the importance matrix and the vision projector are not quants",
      all("imatrix" not in r["path"] and "mmproj" not in r["path"] for r in rows))
check("  a split quant is named, not offered", split == ["BF16"], split)

print("\nsizing")
with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "h.gguf"
    # A Qwen3.5-shaped hybrid: 32 layers, attention on every 4th, 4 KV heads of 256.
    gguf(f, {"general.architecture": "qwen35", "qwen35.block_count": 32,
             "qwen35.attention.head_count": 16, "qwen35.attention.head_count_kv": 4,
             "qwen35.embedding_length": 4096, "qwen35.attention.key_length": 256,
             "qwen35.attention.value_length": 256, "qwen35.full_attention_interval": 4,
             "qwen35.context_length": 262144,
             "tokenizer.chat_template": "{{ raise_exception('No user query found in messages.') }}"})
    x = F.inspect(str(f))
check("the arch and a template that breaks on tool results are read",
      x["arch"] == "qwen35" and x["template_raises"], x["arch"])
info = x["info"]
kv128 = F.need(info, 0, 131072, 1, "q4_0", "ollama") - F.need(info, 0, 0, 1, "q4_0", "ollama")
# 8 attention layers x 4 heads x (256+256) x 18/32 bytes x 131072 tokens
check("only the attention layers hold a cache: 128k at q4_0 is ~1.1 GiB",
      abs(kv128 - 8 * 4 * 512 * 18 / 32 * 131072) < 1, kv128 / GIB)
check("  llama.cpp's own overhead is counted",
      F.need(info, GIB, 4096, 1, "q4_0", "llamacpp") > F.need(info, GIB, 4096, 1, "q4_0", "ollama"))
budget = 9 * GIB
plan = F.plan("o/M-GGUF", files, info, budget, 131072, 1, "q4_0", "ollama")
by = {r["quant"]: r for r in plan["rows"]}
check("the largest quant that fits is the one recommended",
      plan["recommended"] == "M-PQ2_0.gguf" and not by["Q8_0"]["fits"], plan["recommended"])
check("  a ternary PrismML packing is sized for PrismML's llama.cpp",
      by["PQ2_0"]["engine"] == "prism" and by["Q4_K_M"]["engine"] == "ollama")
check("  and each quant says the largest window it can hold",
      by["Q4_K_M"]["max_context"] >= 131072 and by["Q8_0"]["max_context"] == 0, by["Q8_0"])
none = F.plan("o/M-GGUF", files, info, 2 * GIB, 131072, 1, "q4_0", "ollama")
check("nothing that does not fit is recommended", none["recommended"] == "")

print("\ncards")
gpu = {"total_mib": 12288, "used_mib": 10659, "tenants": [
    {"owner": "container faster-whisper", "mib": 2056}, {"owner": "unit ollama-chat", "mib": 7562},
    {"owner": "unit llamacpp-chat", "mib": 100}, {"owner": "container home-cameras-web", "mib": 924}]}
check("the whole card leaves out speech and cameras, not the model servers",
      F.card_budget(gpu, True) == (12288 - 2056 - 924) * 1024 * 1024)
check("  'free now' is what the card reports free",
      F.card_budget(gpu, False) == (12288 - 10659) * 1024 * 1024)

print("\nnames")
check("an imported file gets a plain Ollama name",
      F.ollama_name("bartowski/MiMo-V2.6-Distill-Qwen-9B-GGUF", "Q4_K_M")
      == "mimo-v2.6-distill-qwen-9b:q4_k_m", F.ollama_name("bartowski/MiMo-V2.6-Distill-Qwen-9B-GGUF", "Q4_K_M"))

print()
if failed:
    print(f"{len(failed)} FAILED: {', '.join(failed)}")
    sys.exit(1)
print("all checks passed")

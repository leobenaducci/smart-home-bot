"""The house's local model library: what is on disk, where, and what runs it.

Two stores hold the models a setup can name:

  Ollama's own store      `qwen3.5:9b`, `hf.co/owner/repo:TAG` -- pulled by Ollama
  the stack's GGUF folder `hf:owner/repo/file.gguf` -- a Hugging Face file,
                          downloaded into ollama_instances.MODELS_DIR

A model being on disk says nothing about whether an engine can run it. On
2026-09-24 two setups were applied on llama.cpp with models from Ollama's
library -- qwen3.5:9b ("rope.dimension_sections has wrong array length") and
gemma4:e4b ("wrong number of tensors; expected 2131, got 720") -- and the roles
on them went down. So every model is *tested* on each engine that could run
it, right after it arrives, and the page offers a setup only the models that
passed on its engine.

A test loads the model on the CPU (no card's memory is touched, nothing
running is evicted) with a small window and asks for one token. Results go to
`model-library.json` next to the config, which the admin page reads.

The page queues work in `model-library-queue.json`; the host helper's root
trigger runs it (ollama_host.py). Everything in a queued job is checked here
by shape before anything acts on it: names by the same patterns the setups
use, files only inside MODELS_DIR, deletes only of what no setup uses.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import llamacpp as LC
import ollama_instances as OI

LIBRARY = "model-library.json"
QUEUE = "model-library-queue.json"
OLLAMA_URL = os.environ.get("HOME_STACK_OLLAMA_URL", "http://127.0.0.1:11434")
TEST_PORT = 18931
TEST_CTX = 2048
LOAD_TIMEOUT_S = 300
OPS = ("pull", "download", "delete", "test", "import")
# What an import may put in front of a GGUF instead of the template it carries.
# `qwen3.5` is Ollama's own renderer for the family: some Hugging Face files'
# templates refuse a conversation ending in tool results ("No user query found
# in messages"), and every tool call after the first failed with a 500
# (NeoHorse, Ornith, 2026-09-25). The admin page reads the file and decides.
RENDERERS = ("", "qwen3.5")
QWEN35_PARAMS = (("temperature", "1"), ("top_k", "20"), ("top_p", "0.95"),
                 ("presence_penalty", "1.5"))
# The engine a flavour's builds serve (ollama_instances.ENGINES).
FLAVOR_OF = {"llamacpp": "vanilla", "prism": "prism"}


class LibraryError(ValueError):
    """A queued job that cannot be run as written."""


# ---------------------------------------------------------------------------
# What is on disk
# ---------------------------------------------------------------------------
def _get(path: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(OLLAMA_URL + path, timeout=timeout) as r:
        return json.load(r)


def ollama_models() -> list[dict]:
    try:
        rows = _get("/api/tags").get("models") or []
    except (OSError, ValueError):
        return []
    out = []
    for m in rows:
        d = m.get("details") or {}
        out.append({"id": m.get("name"), "store": "ollama", "bytes": int(m.get("size") or 0),
                    "family": d.get("family") or "", "quant": d.get("quantization_level") or "",
                    "params": d.get("parameter_size") or ""})
    return [m for m in out if m["id"]]


def file_models(models_dir: Path | None = None) -> list[dict]:
    root = Path(models_dir or OI.MODELS_DIR)
    out = []
    for f in sorted((root / "hf").glob("*/*/**/*.gguf")) if (root / "hf").is_dir() else []:
        rel = f.relative_to(root / "hf").parts
        if len(rel) < 3 or "mmproj" in f.name.lower():
            continue
        ref = f"hf:{rel[0]}/{rel[1]}/{'/'.join(rel[2:])}"
        if not OI.HF_RE.fullmatch(ref):
            continue
        quant = re.search(r"(I?Q\d[\w]*|P?T?Q\d_\d|BF16|F16|F32)", f.stem, re.I)
        out.append({"id": ref, "store": "file", "bytes": f.stat().st_size, "family": "",
                    "quant": quant.group(1) if quant else "", "params": "", "path": str(f)})
    return out


def engines_for(entry: dict) -> list[str]:
    """The engines a model could run on: Ollama for what is in its store,
    llama.cpp for any GGUF, in each flavour that is built."""
    engines = ["ollama"] if entry["store"] == "ollama" else []
    engines += [e for e, flavor in FLAVOR_OF.items() if LC.built(flavor)]
    return engines


def load_library(config_dir: Path) -> dict:
    try:
        doc = json.loads((config_dir / LIBRARY).read_text())
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("tests", {})
    return doc


def save_library(config_dir: Path, doc: dict) -> None:
    path = config_dir / LIBRARY
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    tmp.replace(path)


def refresh(config_dir: Path) -> dict:
    """The inventory, with each model's recorded tests, written for the page."""
    doc = load_library(config_dir)
    models = ollama_models() + file_models()
    for m in models:
        m["engines"] = engines_for(m)
    doc["models"] = models
    doc["at"] = int(time.time())
    try:
        usage = shutil.disk_usage(OI.MODELS_DIR if Path(OI.MODELS_DIR).exists() else "/")
        doc["disk_free"] = usage.free
    except OSError:
        pass
    # Tests of models no longer on disk are dropped: a name pulled again is a
    # new file and is tested again.
    have = {m["id"] for m in models}
    doc["tests"] = {k: v for k, v in doc["tests"].items() if k in have}
    save_library(config_dir, doc)
    return doc


# ---------------------------------------------------------------------------
# Checking a queued job
# ---------------------------------------------------------------------------
def check_job(job: dict) -> dict:
    if not isinstance(job, dict):
        raise LibraryError("a job is not a mapping")
    op = str(job.get("op") or "")
    if op not in OPS:
        raise LibraryError(f"unknown operation {op!r}")
    model = str(job.get("model") or "").strip()
    if op == "download":
        if not OI.HF_RE.fullmatch(model):
            raise LibraryError(f"{model!r}: a download is hf:owner/repo/file.gguf")
    elif op == "pull":
        if not OI.MODEL_RE.fullmatch(model) or model.startswith(("hf:", "/")):
            raise LibraryError(f"{model!r} is not an Ollama model name")
    elif not (OI.HF_RE.fullmatch(model) or (OI.MODEL_RE.fullmatch(model) and not model.startswith("/"))):
        raise LibraryError(f"{model!r} is not a library model")
    engines = [e for e in (job.get("engines") or []) if e in OI.ENGINES]
    if op == "import":
        # A downloaded file, into Ollama under a plain name.
        name = str(job.get("name") or "").strip()
        renderer = str(job.get("renderer") or "")
        if not OI.HF_RE.fullmatch(model):
            raise LibraryError(f"{model!r}: an import is of hf:owner/repo/file.gguf")
        if (not OI.MODEL_RE.fullmatch(name) or name.startswith(("hf:", "hf.co/", "/"))
                or ":" not in name):
            raise LibraryError(f"{name!r} is not a name for an Ollama model (name:tag)")
        if renderer not in RENDERERS:
            raise LibraryError(f"unknown renderer {renderer!r}")
        return {"op": op, "model": model, "engines": engines, "name": name, "renderer": renderer}
    return {"op": op, "model": model, "engines": engines}


def used_models(cfg: dict) -> set[str]:
    try:
        return {s["model"] for s in OI.setups(cfg)}
    except OI.InstanceError:
        return set()


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------
def pull(model: str) -> str:
    """Ollama pulls it; progress as lines. "" or why not."""
    print(f"  pulling {model} into Ollama ...")
    body = json.dumps({"model": model, "stream": True}).encode()
    last = ""
    try:
        req = urllib.request.Request(OLLAMA_URL + "/api/pull", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3600) as r:
            for line in r:
                d = json.loads(line or b"{}")
                if d.get("error"):
                    return d["error"]
                status = d.get("status", "")
                if d.get("total") and d.get("completed"):
                    pct = int(d["completed"] * 100 / d["total"])
                    status = f"{status} {pct}%"
                if status != last and (not status[-1:] == "%" or status.endswith("0%")):
                    print(f"    {status}")
                    last = status
    except (OSError, ValueError) as exc:
        return str(exc)
    return ""


def download(model: str) -> str:
    """A Hugging Face GGUF into MODELS_DIR; progress as lines. "" or why not."""
    m = OI.HF_RE.fullmatch(model)
    dest = Path(OI.MODELS_DIR) / "hf" / m.group(1) / m.group(2) / m.group(3)
    if dest.exists():
        print(f"  {model} is already here")
        return ""
    url = f"https://huggingface.co/{m.group(1)}/{m.group(2)}/resolve/main/{m.group(3)}"
    part = dest.with_name(dest.name + ".part")
    print(f"  downloading {url} ...")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as r, part.open("wb") as fh:
            total = int(r.headers.get("Content-Length") or 0)
            done, step = 0, 0
            while True:
                chunk = r.read(1 << 22)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total and done * 10 // total > step:
                    step = done * 10 // total
                    print(f"    {step * 10}% of {total / 1e9:.1f} GB")
        part.rename(dest)
        for p in (dest, *dest.parents):
            if p == Path(OI.MODELS_DIR).parent:
                break
            p.chmod(0o755 if p.is_dir() else 0o644)
        return ""
    except (OSError, ValueError) as exc:
        part.unlink(missing_ok=True)
        return f"download failed: {exc}"


def import_to_ollama(model: str, name: str, renderer: str) -> str:
    """Create *name* in Ollama from the downloaded file, downloading it first
    if it is not here. "" or why not."""
    m = OI.HF_RE.fullmatch(model)
    path = Path(OI.MODELS_DIR) / "hf" / m.group(1) / m.group(2) / m.group(3)
    if not path.is_file():
        why = download(model)
        if why:
            return why
    lines = [f"FROM {path}"]
    if renderer:
        lines += ["TEMPLATE {{ .Prompt }}", f"RENDERER {renderer}", f"PARSER {renderer}"]
        lines += [f"PARAMETER {k} {v}" for k, v in QWEN35_PARAMS]
    print(f"  importing {model} into Ollama as {name}"
          + (f" (renderer {renderer})" if renderer else "") + " ...")
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".Modelfile", delete=False) as fh:
        fh.write("\n".join(lines) + "\n")
        modelfile = fh.name
    try:
        host = OLLAMA_URL.split("://", 1)[-1]
        r = subprocess.run(["ollama", "create", name, "-f", modelfile], capture_output=True,
                           text=True, timeout=3600, env={**os.environ, "OLLAMA_HOST": host})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    finally:
        Path(modelfile).unlink(missing_ok=True)
    if r.returncode != 0:
        said = (r.stderr or r.stdout or "").strip()
        return said.splitlines()[-1][:300] if said else f"ollama create exited {r.returncode}"
    return ""


def delete(model: str, used: set[str]) -> str:
    if model in used:
        return f"{model} is used by a setup; pick another model for it first"
    m = OI.HF_RE.fullmatch(model)
    if m:
        f = Path(OI.MODELS_DIR) / "hf" / m.group(1) / m.group(2) / m.group(3)
        try:
            f.unlink()
        except OSError as exc:
            return str(exc)
        print(f"  deleted {f}")
        return ""
    try:
        req = urllib.request.Request(OLLAMA_URL + "/api/delete", method="DELETE",
                                     data=json.dumps({"model": model}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=60).close()
    except (OSError, ValueError) as exc:
        return str(exc)
    print(f"  removed {model} from Ollama")
    return ""


def test_ollama(model: str) -> dict:
    """One token from Ollama, on the CPU (num_gpu 0), unloaded afterwards."""
    started = time.time()
    body = json.dumps({"model": model, "prompt": "Hi", "stream": False, "keep_alive": 0,
                       "options": {"num_predict": 1, "num_ctx": TEST_CTX, "num_gpu": 0}}).encode()
    try:
        req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=LOAD_TIMEOUT_S) as r:
            json.load(r)
        return {"ok": True, "seconds": round(time.time() - started, 1)}
    except urllib.error.HTTPError as exc:
        try:
            why = json.loads(exc.read()).get("error") or f"HTTP {exc.code}"
        except (OSError, ValueError):
            why = f"HTTP {exc.code}"
        return {"ok": False, "error": str(why)[:300]}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:300]}


def load_error(stderr: str) -> str:
    """The line that says why llama.cpp did not load a model. The CUDA notice
    a CPU test always prints is not it."""
    lines = [ln.split(" E ", 1)[-1].strip() for ln in stderr.splitlines()
             if " E " in ln and "ggml_cuda_init" not in ln]
    best = next((ln for ln in lines if "error loading model" in ln), lines[0] if lines else "")
    return (best.split("error loading model:", 1)[-1].strip() or best or "llama-server exited")[:300]


def test_llamacpp(model: str, engine: str, path: str) -> dict:
    """Load *path* in llama-server on the CPU, ask for one token, stop it.

    Runs the root-owned build as the ollama user (it reads Ollama's store),
    on a port nothing else uses, with no card visible."""
    flavor = FLAVOR_OF[engine]
    bin_dir = LC.INSTALLED / LC.build_name(flavor) / "bin"
    if not (bin_dir / "llama-server").is_file():
        bin_dir = LC.build_dir(flavor) / "bin"
    if not (bin_dir / "llama-server").is_file():
        return {"ok": False, "error": f"llama.cpp {flavor} is not built"}
    argv = [str(bin_dir / "llama-server"), "-m", path, "--host", "127.0.0.1",
            "--port", str(TEST_PORT), "-c", str(TEST_CTX), "-np", "1", "-ngl", "0", "--jinja"]
    if os.geteuid() == 0:
        argv = ["runuser", "-u", "ollama", "--", *argv]
    env = {"PATH": "/usr/bin:/bin", "LD_LIBRARY_PATH": str(bin_dir), "CUDA_VISIBLE_DEVICES": ""}
    started = time.time()
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        while time.time() - started < LOAD_TIMEOUT_S:
            if proc.poll() is not None:
                return {"ok": False, "error": load_error(proc.stderr.read() or "")}
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{TEST_PORT}/health", timeout=3) as r:
                    if json.load(r).get("status") == "ok":
                        break
            except (OSError, ValueError):
                pass
            time.sleep(2)
        else:
            return {"ok": False, "error": f"did not load within {LOAD_TIMEOUT_S} s"}
        body = json.dumps({"messages": [{"role": "user", "content": "Hi"}], "max_tokens": 1}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{TEST_PORT}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            json.load(r)
        return {"ok": True, "seconds": round(time.time() - started, 1)}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        # Gone already when the load failed; stopped, and killed if it will not stop, when not.
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass


def model_file(model: str, ollama_blob) -> str:
    """The GGUF llama.cpp would read for *model*, or ""."""
    m = OI.HF_RE.fullmatch(model)
    if m:
        f = Path(OI.MODELS_DIR) / "hf" / m.group(1) / m.group(2) / m.group(3)
        return str(f) if f.is_file() else ""
    blob = ollama_blob(model)
    return str(blob) if blob and blob.is_file() else ""


def test(model: str, engines: list[str], ollama_blob) -> dict[str, dict]:
    out = {}
    for engine in engines:
        print(f"  testing {model} on {OI.ENGINE_LABELS[engine]} ...")
        if engine == "ollama":
            res = test_ollama(model)
        else:
            path = model_file(model, ollama_blob)
            res = test_llamacpp(model, engine, path) if path else {"ok": False, "error": "no file"}
        res["at"] = int(time.time())
        print(f"    {'works' if res['ok'] else 'does not load: ' + res.get('error', '')}"
              + (f" ({res['seconds']} s)" if res.get("seconds") else ""))
        out[engine] = res
    return out


def run_queue(config_dir: Path, cfg: dict, ollama_blob) -> int:
    """Run and empty the page's queue. Returns the number of failed jobs."""
    qpath = config_dir / QUEUE
    try:
        jobs = json.loads(qpath.read_text()) or []
    except (OSError, ValueError):
        jobs = []
    qpath.unlink(missing_ok=True)
    failed = 0
    doc = refresh(config_dir)
    for raw in jobs if isinstance(jobs, list) else []:
        try:
            job = check_job(raw)
        except LibraryError as exc:
            print(f"  refused: {exc}")
            failed += 1
            continue
        op, model = job["op"], job["model"]
        why = ""
        if op == "pull":
            why = pull(model)
        elif op == "download":
            why = download(model)
        elif op == "delete":
            why = delete(model, used_models(cfg))
        elif op == "import":
            why = import_to_ollama(model, job["name"], job["renderer"])
            model = job["name"]             # what gets tested is the Ollama model
        if why:
            print(f"  {op} {model}: {why}")
            failed += 1
            continue
        doc = refresh(config_dir)
        if op in ("pull", "download", "test", "import"):
            entry = next((m for m in doc["models"] if m["id"] == model), None)
            if entry is None:
                print(f"  {model} is not in the library")
                failed += 1
                continue
            engines = [e for e in (job["engines"] or entry["engines"]) if e in entry["engines"]]
            doc["tests"].setdefault(model, {}).update(test(model, engines, ollama_blob))
            save_library(config_dir, doc)
    return failed

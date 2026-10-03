"""The coding benchmark: local models solving the same five problems, on the Studio's card.

A model is served on the card the Studio owns -- the one the Programmer's local
model borrows -- by the engine picked for it (Ollama, llama.cpp or PrismML's
fork), and opencode drives it through each problem exactly as the Programmer
would: read, edit, run the tests, repeat. Every attempt is graded by the
problem's visible tests and by hidden ones the model never saw, and what it did
on the way is kept -- each tool call, its text, its turns, its tokens, its time.

Three kinds of container, all started here through the docker socket:

  codebench-server        the model, on the card: llama-server from the house's
                          own build (deploy/llamacpp.py), or the host's Ollama
                          binary over its store, read-only
  codebench-run-<run>-<p> one attempt at one problem: opencode and a toolchain,
                          nothing of the house's mounted (deploy/codebench/)
  the network `codebench` between them; the server is also published on the
                          host's loopback, which is how this page watches it

The card is borrowed the way the Programmer's local model borrows it: the
Studio is paused -- after its running job, or at once with that job sent back
to the queue -- and given back when the queue is empty. Anything else holding
the card (the Programmer's local model) is waited for, never evicted.

Runs one queue entry at a time: an entry is one model, one engine, one window
and the problems to give it -- all five, or one, so a problem can be re-run on
its own. Results are files in the state directory, one per entry.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

# llama.cpp first: it is what the Programmer runs its local model on.
ENGINES = ("llamacpp", "ollama", "prism")
ENGINE_LABELS = {"ollama": "Ollama", "llamacpp": "llama.cpp", "prism": "llama.cpp (PrismML)"}
CONTEXTS = (16384, 32768, 65536, 131072, 262144)
DEFAULT_CONTEXT = 32768
NETWORK = "codebench"
SERVER = "codebench-server"
RUN_PREFIX = "codebench-run-"
CACHE_VOLUME = "codebench-opencode-cache"
IMAGE_REPO = "home-stack-codebench"
HOST_PORT = 11483                       # the server, on the host's loopback
# The host's Ollama, mounted read-only into the server container: the binary,
# its runners and the store. An Ollama model's GGUF path comes from the host's
# own Ollama (/api/show), so the store is where that says it is.
OLLAMA_BIN = "/usr/local/bin/ollama"
OLLAMA_LIB = "/usr/local/lib/ollama"
# What somebody else may hold on the card before a run waits for it. The
# Studio's own idle worker is gone by then; a desktop takes a few hundred MB.
FOREIGN_MB = 1500
LOAD_TIMEOUT_S = 600
MODEL_RE = re.compile(r"^(hf:[\w.\-]+/[\w.\-]+/[\w.\-/]+\.gguf|/[\w.\-/]+\.gguf|[\w.\-/]+(:[\w.\-]+)?)$")
ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")


class BenchError(ValueError):
    """What to tell the person."""


# -- the problems --------------------------------------------------------------
def problems(root: Path) -> list[dict]:
    """Each problem's card: id, title, language, kind, what it tests. Not the
    hidden tests, not the solution."""
    out = []
    for d in sorted((root / "problems").iterdir()):
        try:
            meta = json.loads((d / "problem.json").read_text())
        except (OSError, ValueError):
            continue
        out.append({"id": d.name, **{k: meta.get(k, "") for k in ("title", "language", "kind", "about", "task")},
                    "timeout": int(meta.get("timeout") or 900)})
    return out


def job_for(root: Path, pid: str, base_url: str, model: str, context: int) -> dict:
    """What solve.py reads on stdin: the task, the checks and the hidden tests."""
    d = root / "problems" / pid
    meta = json.loads((d / "problem.json").read_text())
    hidden = {str(f.relative_to(d / "hidden")): f.read_text() for f in sorted((d / "hidden").rglob("*"))
              if f.is_file()}
    return {"problem": pid, "task": meta["task"], "check": meta["check"], "hidden": meta["hidden"],
            "hidden_files": hidden, "protected": meta.get("protected") or [], "base_url": base_url,
            "model": model, "context": context, "timeout": int(meta.get("timeout") or 900)}


def stage_image(root: Path, into: Path) -> str:
    """The sandbox's build context -- the Dockerfile, solve.py and each
    problem's starting files, nothing else -- and its tag, a hash of it."""
    digest = hashlib.sha256()
    shutil.copy2(root / "Dockerfile", into / "Dockerfile")
    shutil.copy2(root / "solve.py", into / "solve.py")
    for f in sorted([root / "Dockerfile", root / "solve.py"]):
        digest.update(f.name.encode() + b"\0" + f.read_bytes())
    for d in sorted((root / "problems").iterdir()):
        start = d / "start"
        if not start.is_dir():
            continue
        shutil.copytree(start, into / "problems" / d.name / "start")
        for f in sorted(start.rglob("*")):
            if f.is_file():
                digest.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes())
    return f"{IMAGE_REPO}:{digest.hexdigest()[:12]}"


# -- what a run asks for ----------------------------------------------------------
def check_entry(raw: dict, known: set[str]) -> dict:
    """A queue entry from the page, checked by shape: every value ends up on a
    docker command line."""
    model = str(raw.get("model") or "").strip()
    if not model or not MODEL_RE.fullmatch(model) or ".." in model:
        raise BenchError("Pick a local model: an Ollama name, hf:owner/repo/file.gguf or a .gguf path.")
    engine = str(raw.get("engine") or "")
    if engine not in ENGINES:
        raise BenchError(f"The engine is one of {', '.join(ENGINES)}.")
    if engine == "ollama" and (model.startswith("hf:") or model.endswith(".gguf")):
        raise BenchError("Ollama runs models from its own store; a .gguf file runs on llama.cpp.")
    try:
        context = int(raw.get("context") or DEFAULT_CONTEXT)
    except (TypeError, ValueError):
        context = 0
    if context not in CONTEXTS:
        raise BenchError(f"The window is one of {', '.join(str(c) for c in CONTEXTS)}.")
    picked = [str(p) for p in raw.get("problems") or []]
    if not picked or any(p not in known for p in picked):
        raise BenchError("Pick at least one problem.")
    return {"model": model, "engine": engine, "context": context,
            "problems": [p for p in sorted(known) if p in picked],
            "interrupt": bool(raw.get("interrupt"))}


# -- reading the server's log -----------------------------------------------------
_LAYERS = re.compile(r"offloaded (\d+)/(\d+) layers to GPU")
# What says why a load failed, rather than the backtrace after it: the tail of
# a crash is twenty lines of C++ symbols (Bonsai on Prism, 2026-10-02).
_WHY = re.compile(r"out of memory|cudaMalloc|failed to allocate|CUDA error|GGML_ASSERT|error:|"
                  r"failed to load|unable to|not enough|exceeds|terminate called|abort", re.I)


def load_failure(log: str) -> str:
    """The lines of a server's log that say why it did not load, then its
    last few: what goes in a failed run's error."""
    lines = [ln.strip() for ln in log.splitlines() if ln.strip()]
    why = [ln for ln in lines if _WHY.search(ln) and not ln.startswith(("/", "#"))]
    keep = list(dict.fromkeys(why[:8]))
    tail = [ln for ln in lines[-4:] if ln not in keep]
    return "\n".join(keep + (["…"] if keep and tail else []) + tail)[-1500:]
# Bare in older builds; behind "slot print_timing: id 0 | task 57 |" in newer ones.
_SPEED = re.compile(r"(?:^|\|)[ \t]*(prompt eval|eval) time =.*?([\d.]+) tokens per second", re.M)


def server_facts(log: str) -> dict:
    """What llama-server (or Ollama's runner, which is llama-server too) says
    about a load and its requests: layers on the card, and median speeds."""
    facts: dict = {}
    m = None
    for m in _LAYERS.finditer(log):
        pass
    if m:
        facts["layers"] = f"{m.group(1)}/{m.group(2)}"
        facts["all_on_gpu"] = m.group(1) == m.group(2)
    speeds: dict[str, list[float]] = {"prompt eval": [], "eval": []}
    for kind, value in _SPEED.findall(log):
        with contextlib.suppress(ValueError):
            speeds[kind].append(float(value))

    def median(xs):
        xs = sorted(xs)
        return round(xs[len(xs) // 2], 1) if xs else None
    facts["prompt_tok_s"] = median(speeds["prompt eval"])
    facts["gen_tok_s"] = median(speeds["eval"])
    return facts


def summary(run: dict) -> dict:
    """One results-table row from a run file."""
    res = run.get("problems") or {}
    done = [r for r in res.values() if "pass" in r]
    return {"id": run.get("id"), "model": run.get("model"), "engine": run.get("engine"),
            "context": run.get("context"), "state": run.get("state"), "error": run.get("error", ""),
            "started": run.get("started"), "finished": run.get("finished"),
            "load_seconds": run.get("load_seconds"), "server": run.get("server") or {},
            "vram_mb": run.get("vram_mb"),
            "passed": sum(1 for r in done if r.get("pass")), "graded": len(done),
            "problems": {pid: {k: r.get(k) for k in ("pass", "seconds", "turns", "tool_calls", "tool_errors",
                                                      "timed_out", "stopped", "tests_untouched", "error")}
                         | {"visible": (r.get("visible") or {}).get("ok"), "hidden": (r.get("hidden") or {}).get("ok"),
                            "output_tokens": (r.get("tokens") or {}).get("output")}
                         for pid, r in res.items()}}


# -- the runner ----------------------------------------------------------------------
class Runner:
    """The queue and the one thread that works it."""

    def __init__(self, root: Path, state: Path, *, gpu: Callable[[], int | None],
                 studio: Callable[[], tuple[str, str]], ollama_url: Callable[[], str],
                 build_dir: Callable[[str], Path], models_dir: str, server_image: str,
                 docker: str = "docker"):
        self.root, self.state = root, state
        self.gpu, self.studio, self.ollama_url = gpu, studio, ollama_url
        self.build_dir, self.models_dir, self.server_image = build_dir, models_dir, server_image
        self.docker = docker
        self.results = state / "results"
        self.results.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._live: dict = {}

    # -- files --
    def _read(self, name: str, default):
        try:
            return json.loads((self.state / name).read_text())
        except (OSError, ValueError):
            return default

    def _write(self, name: str, value) -> None:
        tmp = self.state / f".{name}.tmp"
        tmp.write_text(json.dumps(value, ensure_ascii=False))
        tmp.replace(self.state / name)

    def queue(self) -> list[dict]:
        return self._read("queue.json", [])

    def status(self) -> dict:
        st = self._read("status.json", {})
        if st.get("state") not in (None, "idle") and not self.running():
            st = {"state": "idle", "note": "interrupted: the admin page restarted during a run"}
        return {**st, **({"live": self._live} if self._live else {})}

    def _status(self, state: str, **fields) -> None:
        self._write("status.json", {"state": state, "at": time.time(), **fields})

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def runs(self, limit: int = 60) -> list[dict]:
        out = []
        for f in sorted(self.results.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            with contextlib.suppress(OSError, ValueError):
                out.append(summary(json.loads(f.read_text())))
        return out

    def run_file(self, run_id: str) -> dict | None:
        if not ID_RE.fullmatch(run_id or ""):
            return None
        try:
            return json.loads((self.results / f"{run_id}.json").read_text())
        except (OSError, ValueError):
            return None

    def delete_run(self, run_id: str) -> bool:
        if not ID_RE.fullmatch(run_id or "") or (self._live.get("run") == run_id):
            return False
        try:
            (self.results / f"{run_id}.json").unlink()
            (self.results / f"{run_id}.server.log").unlink(missing_ok=True)
            return True
        except OSError:
            return False

    # -- the page's verbs --
    def add(self, entry: dict, by: str) -> dict:
        with self._lock:
            q = self.queue()
            entry = {**entry, "id": time.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(
                f"{time.time()}{entry}".encode()).hexdigest()[:6], "by": by, "queued_at": time.time()}
            q.append(entry)
            self._write("queue.json", q)
        self.kick()
        return entry

    def remove(self, entry_id: str) -> bool:
        with self._lock:
            q = self.queue()
            kept = [e for e in q if e.get("id") != entry_id]
            self._write("queue.json", kept)
            return len(kept) != len(q)

    def stop(self) -> None:
        """Stop the attempt under way and empty the queue; the card goes back."""
        with self._lock:
            self._write("queue.json", [])
        self._stop.set()
        live = self._live.get("container")
        if live:
            self._docker("stop", "-t", "20", live, timeout=40)

    def kick(self) -> None:
        with self._lock:
            if self.running() or not self.queue():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._work, name="codebench", daemon=True)
            self._thread.start()

    def recover(self) -> None:
        """After a restart of this page: what *its own* interrupted run left
        behind goes -- the containers, and a Studio it paused is given back.

        Only when this state directory says a run was under way. The
        containers' names are the same for every runner on the machine, and a
        runner built over a scratch directory (a test rendering the page)
        removed a live run's attempts twice before this (2026-10-02)."""
        st = self._read("status.json", {})
        if st.get("state") not in (None, "idle"):
            for name in self._docker_out("ps", "-a", "--format", "{{.Names}}").split():
                if name == SERVER or name.startswith(RUN_PREFIX):
                    self._docker("rm", "-f", name)
            if st.get("studio_paused_by_us"):
                self._studio("resume")
            self._status("idle", note="interrupted: the admin page restarted during a run")
        self.kick()

    # -- docker --
    def _docker(self, *args: str, timeout: int = 120, **kw) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self.docker, *args], capture_output=True, text=True, timeout=timeout, **kw)
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(exc.cmd, 124, "", f"timed out: {' '.join(args[:2])}")

    def _docker_out(self, *args: str) -> str:
        r = self._docker(*args)
        return r.stdout if r.returncode == 0 else ""

    def card_used_mb(self, gpu: int) -> int | None:
        """Memory in use on the card, everything on it counted."""
        # Past the image's entrypoint: the CUDA images print a licence banner
        # on stdout before running anything.
        r = self._docker("run", "--rm", "--gpus", f"device={gpu}", "-e", "NVIDIA_DRIVER_CAPABILITIES=utility",
                         "--entrypoint", "nvidia-smi", self.server_image, "--query-gpu=memory.used",
                         "--format=csv,noheader,nounits", timeout=60)
        try:
            return int(r.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            return None

    # -- the Studio --
    def _studio(self, action: str, body: dict | None = None, method: str = "POST") -> dict | None:
        url, secret = self.studio()
        if not url or not secret:
            return None
        path = "/api/queue" if action == "queue" else f"/api/admin/{action}"
        req = urllib.request.Request(
            url.rstrip("/") + path, method="GET" if action == "queue" else method,
            data=None if action == "queue" else json.dumps(body or {}).encode(),
            headers={"X-Studio-Secret": secret, "X-Studio-User": "codebench", "X-Studio-Name": "Coding benchmark",
                     "X-Studio-Admin": "1", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except (OSError, ValueError, urllib.error.URLError):
            return None

    # -- the work --
    def _wait(self, seconds: float) -> bool:
        """Sleep; False when a stop came."""
        return not self._stop.wait(seconds)

    def _borrow_card(self, interrupt: bool) -> tuple[int | None, bool]:
        """(the card, whether the Studio was paused here). None when stopped."""
        gpu = self.gpu()
        if gpu is None:
            raise BenchError("There is no card for the benchmark: the Studio's card is not configured.")
        q = self._studio("queue")
        ours = False
        if q is not None:
            st = q.get("status") or {}
            if not st.get("paused") or (interrupt and q.get("running")):
                self._studio("pause", {"reason": "bench", "now": interrupt})
                ours = not st.get("paused")
            self._status("waiting", gpu=gpu, studio_paused_by_us=ours, why="studio")
            while True:
                q = self._studio("queue") or {}
                st = q.get("status") or {}
                if not (st.get("worker") or q.get("running")):
                    break
                running = q.get("running") or {}
                self._status("waiting", gpu=gpu, studio_paused_by_us=ours, why="studio",
                             what=running.get("what", ""), progress=running.get("progress"))
                if not self._wait(10):
                    return None, ours
        while True:
            used = self.card_used_mb(gpu)
            if used is not None and used <= FOREIGN_MB:
                return gpu, ours
            self._status("waiting", gpu=gpu, studio_paused_by_us=ours, why="card", used_mb=used)
            if not self._wait(15):
                return None, ours

    def _work(self) -> None:
        gpu, ours, note = None, False, ""
        try:
            while not self._stop.is_set():
                with self._lock:
                    q = self.queue()
                if not q:
                    break
                entry = q[0]
                try:
                    if gpu is None:
                        gpu, ours = self._borrow_card(entry.get("interrupt", False))
                        if gpu is None:
                            break
                    self._run_entry(entry, gpu, ours)
                finally:
                    self.remove(entry["id"])
        except Exception as exc:                                  # noqa: BLE001 -- the page says it
            note = f"stopped by an error: {exc}"[:300]
        finally:
            self._docker("rm", "-f", SERVER)
            self._live = {}
            if ours:
                self._studio("resume")
            self._status("idle", note=note or ("stopped" if self._stop.is_set() else ""))
            # Something queued while the last entry finished.
            if not self._stop.is_set() and self.queue():
                threading.Timer(1, self.kick).start()

    def _model_file(self, model: str) -> str:
        """The host path of the GGUF llama.cpp loads."""
        if model.startswith("hf:"):
            return f"{self.models_dir}/hf/{model[3:]}"
        if model.startswith("/"):
            if not model.startswith(self.models_dir + "/"):
                raise BenchError(f"A .gguf file has to be under {self.models_dir}.")
            return model
        req = urllib.request.Request(self.ollama_url().rstrip("/") + "/api/show", method="POST",
                                     data=json.dumps({"model": model}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                modelfile = json.load(r).get("modelfile") or ""
        except (OSError, ValueError) as exc:
            raise BenchError(f"Ollama does not know {model}: {exc}") from exc
        m = re.search(r"^FROM (/\S+)$", modelfile, re.M)
        if not m:
            raise BenchError(f"Ollama does not say where {model}'s file is.")
        return m.group(1)

    def _ollama_store(self, model: str) -> str:
        path = self._model_file(model)
        if "/blobs/" not in path:
            raise BenchError(f"{model} is not in Ollama's store.")
        return path.split("/blobs/")[0]

    def _start_server(self, entry: dict, gpu: int) -> tuple[str, str]:
        """Start the model on the card; (the model name opencode asks for, the base URL)."""
        self._docker("rm", "-f", SERVER)
        if self._docker("network", "inspect", NETWORK).returncode != 0:
            self._docker("network", "create", NETWORK)
        common = ["run", "-d", "--name", SERVER, "--network", NETWORK, "--gpus", f"device={gpu}",
                  "-p", f"127.0.0.1:{HOST_PORT}:8080"]
        ctx = str(entry["context"])
        if entry["engine"] == "ollama":
            store = self._ollama_store(entry["model"])
            args = common + [
                "-v", f"{OLLAMA_BIN}:/usr/local/bin/ollama:ro", "-v", f"{OLLAMA_LIB}:/usr/local/lib/ollama:ro",
                "-v", f"{store}:/models:ro", "-e", "OLLAMA_MODELS=/models", "-e", "OLLAMA_HOST=0.0.0.0:8080",
                "-e", "OLLAMA_NOPRUNE=1", "-e", f"OLLAMA_CONTEXT_LENGTH={ctx}", "-e", "OLLAMA_FLASH_ATTENTION=1",
                "-e", "OLLAMA_KV_CACHE_TYPE=q8_0", "-e", "OLLAMA_NUM_PARALLEL=1",
                "-e", "OLLAMA_MAX_LOADED_MODELS=1", "-e", "OLLAMA_KEEP_ALIVE=-1", "-e", "OLLAMA_VULKAN=0",
                self.server_image, "/usr/local/bin/ollama", "serve"]
            name = entry["model"]
        else:
            build = self.build_dir("vanilla" if entry["engine"] == "llamacpp" else "prism")
            path = self._model_file(entry["model"])
            args = common + [
                "-v", f"{build}:/llama:ro", "-v", f"{path}:/model.gguf:ro",
                "-e", "LD_LIBRARY_PATH=/llama/bin:/llama/lib",
                self.server_image, "/llama/bin/llama-server", "-m", "/model.gguf", "--alias", "bench",
                "--host", "0.0.0.0", "--port", "8080", "-c", ctx, "-np", "1", "-ngl", "999",
                "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--jinja"]
            name = "bench"
        r = self._docker(*args)
        if r.returncode != 0:
            raise BenchError(f"the model server did not start: {(r.stderr or r.stdout).strip()[-400:]}")
        return name, f"http://{SERVER}:8080/v1"

    def _wait_loaded(self, entry: dict, name: str) -> None:
        base = f"http://127.0.0.1:{HOST_PORT}"
        deadline = time.time() + LOAD_TIMEOUT_S
        while time.time() < deadline:
            if self._stop.is_set():
                raise BenchError("stopped")
            state = self._docker_out("inspect", "-f", "{{.State.Running}}", SERVER).strip()
            if state != "true":
                logs = self._docker("logs", SERVER)
                raise BenchError("the model server exited while loading:\n"
                                 + load_failure((logs.stdout or "") + (logs.stderr or "")))
            try:
                if entry["engine"] == "ollama":
                    req = urllib.request.Request(base + "/api/generate", method="POST",
                                                 data=json.dumps({"model": name, "prompt": "",
                                                                  "keep_alive": -1}).encode(),
                                                 headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=LOAD_TIMEOUT_S) as r:
                        r.read()
                    return
                with urllib.request.urlopen(base + "/health", timeout=5) as r:
                    if r.status == 200:
                        return
            except urllib.error.HTTPError as exc:
                if entry["engine"] == "ollama" and exc.code != 503:
                    raise BenchError(f"Ollama could not load {name}: "
                                     f"{exc.read().decode(errors='replace')[:300]}") from exc
            except (OSError, ValueError):
                pass
            time.sleep(2)
        raise BenchError(f"the model did not load within {LOAD_TIMEOUT_S}s")

    def _ensure_image(self) -> str:
        with tempfile.TemporaryDirectory(prefix="codebench-") as tmp:
            tag = stage_image(self.root, Path(tmp))
            if self._docker("image", "inspect", tag).returncode == 0:
                return tag
            self._status("building", note="building the sandbox image (once per change to it)")
            r = self._docker("build", "-t", tag, tmp, timeout=1800)
            if r.returncode != 0:
                raise BenchError("the sandbox image did not build: " + (r.stderr or r.stdout)[-600:])
        return tag

    def _server_facts(self) -> dict:
        r = self._docker("logs", SERVER)
        return server_facts((r.stdout or "") + (r.stderr or ""))

    def _save(self, run: dict) -> None:
        tmp = self.results / f".{run['id']}.tmp"
        tmp.write_text(json.dumps(run, ensure_ascii=False))
        tmp.replace(self.results / f"{run['id']}.json")

    def _run_entry(self, entry: dict, gpu: int, ours: bool) -> None:
        run = {"id": entry["id"], "model": entry["model"], "engine": entry["engine"],
               "context": entry["context"], "gpu": gpu, "by": entry.get("by", ""), "started": time.time(),
               "state": "running", "problems": {}}
        self._save(run)
        self._live = {"run": run["id"], "model": entry["model"], "engine": entry["engine"], "steps": []}
        try:
            image = self._ensure_image()
            self._status("loading", gpu=gpu, studio_paused_by_us=ours, run=run["id"], model=entry["model"])
            t0 = time.time()
            name, base_url = self._start_server(entry, gpu)
            self._wait_loaded(entry, name)
            run["load_seconds"] = round(time.time() - t0, 1)
            run["vram_mb"] = self.card_used_mb(gpu)
            run["server"] = self._server_facts()
            self._save(run)
            for pid in entry["problems"]:
                if self._stop.is_set():
                    break
                self._status("running", gpu=gpu, studio_paused_by_us=ours, run=run["id"],
                             model=entry["model"], problem=pid)
                run["problems"][pid] = self._attempt(image, run["id"], pid, name, base_url, entry["context"])
                run["server"] = self._server_facts()
                self._save(run)
            run["state"] = "stopped" if self._stop.is_set() else "done"
        except BenchError as exc:
            run["state"], run["error"] = ("stopped", "") if self._stop.is_set() else ("failed", str(exc)[:1600])
        finally:
            run["finished"] = time.time()
            self._save(run)
            # The server's whole log beside the run, for what the page does not show.
            logs = self._docker("logs", SERVER)
            if logs.returncode == 0:
                with contextlib.suppress(OSError):
                    (self.results / f"{run['id']}.server.log").write_text(
                        ((logs.stdout or "") + (logs.stderr or ""))[-400_000:])
            self._docker("rm", "-f", SERVER)
            self._live = {}

    def _attempt(self, image: str, run_id: str, pid: str, name: str, base_url: str, context: int) -> dict:
        job = job_for(self.root, pid, base_url, name, context)
        container = f"{RUN_PREFIX}{run_id}-{pid}"[:120]
        self._live.update(problem=pid, container=container, steps=[], started=time.time())
        cmd = [self.docker, "run", "--rm", "-i", "--name", container, "--network", NETWORK,
               "--memory", "6g", "--pids-limit", "1024", "--cpus", str(min(6, os.cpu_count() or 2)),
               "-v", f"{CACHE_VOLUME}:/home/bench/.cache", image]
        result: dict = {}
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        self._proc = proc
        assert proc.stdin and proc.stdout
        proc.stdin.write(json.dumps(job))
        proc.stdin.close()
        hard = time.time() + job["timeout"] + 900
        timer = threading.Timer(job["timeout"] + 900, lambda: self._docker("rm", "-f", container))
        timer.start()
        try:
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("ev") == "step":
                    self._live["steps"] = (self._live.get("steps") or [])[-39:] + [ev]
                elif ev.get("ev") == "result":
                    result = ev
                if time.time() > hard:
                    break
        finally:
            timer.cancel()
            proc.wait()
            self._proc = None
        if not result:
            result = {"pass": False, "error": "the sandbox ended without a result: "
                                              + (proc.stderr.read() if proc.stderr else "")[-600:]}
        result.pop("ev", None)
        return result

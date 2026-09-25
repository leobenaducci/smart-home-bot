#!/usr/bin/env python3
"""./home-stack ollama -- the household's Ollama servers, on this machine.

    ./home-stack ollama              the cards, the instances, and what an apply
                                     would change. Changes nothing.
    ./home-stack ollama --import     write cloud.ollama.instances from the units
                                     running now, so the list starts out
                                     describing exactly what is there
    ./home-stack ollama --apply      write the drop-ins, restart what changed
                                     (asks for sudo, and confirms first)
    sudo ./home-stack ollama --install-trigger
                                     once: lets the admin page's Apply button
                                     do what --apply does, with no password

The admin page edits the list. It runs in a container and cannot reach
systemd, so a saved change is made real by an apply -- the same "saving is not
deploying" the rest of the page follows. With the trigger installed, the page's
Apply button writes `ollama-apply.request` next to the config; a root `.path`
unit on the host sees it and runs this file's --from-trigger, which applies the
*saved list* (never anything in the request) and writes the result back for the
page. The root side runs a root-owned copy of this file and
ollama_instances.py, can only write `ollama*.service` units, and takes every
value through ollama_instances.normalize. Each run also records
the cards and what already sits on them (`gpus.json` next to the config), which
is how the page knows how much memory each card has for its estimate.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import llamacpp as LC  # noqa: E402
import model_library as ML  # noqa: E402
import ollama_instances as OI  # noqa: E402

SYSTEMD = Path("/etc/systemd/system")
# The trigger: a root-owned copy of this file and its one import, run by a
# .path unit when the page asks. Re-run --install-trigger after this changes;
# the page compares HELPER_VERSION with the installed one and says so.
HELPER_DIR = Path("/usr/local/lib/home-stack-ollama")
TRIGGER = "home-stack-ollama"
REQUEST = "ollama-apply.request"
APPLY_LOG = "ollama-apply.log"
MARKER = "ollama-trigger.json"


def _run(argv: list[str], timeout: int = 20) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def config_path() -> Path:
    # The trigger service names the file outright: it runs a copy of this
    # file with no deploy.py beside it.
    if os.environ.get("HOME_STACK_CONFIG"):
        return Path(os.environ["HOME_STACK_CONFIG"])
    import deploy  # the same resolution every other command uses
    return deploy.CONFIG


def load_cfg(path: Path) -> dict:
    import yaml
    return yaml.safe_load(path.read_text()) or {}


# ---------------------------------------------------------------------------
# What is on the cards
# ---------------------------------------------------------------------------
def _container_names() -> dict[str, str]:
    out = {}
    for line in _run(["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"]).splitlines():
        cid, _, name = line.partition(" ")
        out[cid] = name
    return out


def _owner(pid: int, containers: dict[str, str]) -> str:
    """The systemd unit or container a process belongs to."""
    try:
        cg = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return f"pid {pid}"
    m = re.search(r"docker-([0-9a-f]{64})\.scope", cg) or re.search(r"/docker/([0-9a-f]{64})", cg)
    if m:
        return "container " + containers.get(m.group(1), m.group(1)[:12])
    m = re.search(r"/([^/]+)\.service", cg)
    return f"unit {m.group(1)}" if m else f"pid {pid}"


def inventory() -> dict:
    """The cards, their memory, and who is using how much of each."""
    if not shutil.which("nvidia-smi"):
        return {"gpus": [], "error": "nvidia-smi is not installed on this machine"}
    gpus = []
    for line in _run(["nvidia-smi", "--query-gpu=index,pci.bus_id,name,memory.total,memory.used",
                      "--format=csv,noheader,nounits"]).splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        bus = parts[1].lower()[-12:]          # 00000000:03:00.0 -> 0000:03:00.0
        minor = None
        info = Path(f"/proc/driver/nvidia/gpus/{bus}/information")
        if info.is_file():
            m = re.search(r"Device Minor:\s*(\d+)", info.read_text())
            minor = int(m.group(1)) if m else None
        gpus.append({"index": int(parts[0]), "bus": bus, "name": parts[2],
                     "total_mib": int(parts[3]), "used_mib": int(parts[4]),
                     "minor": minor if minor is not None else int(parts[0]), "tenants": []})
    by_bus = {g["bus"]: g for g in gpus}
    containers = _container_names()
    for line in _run(["nvidia-smi", "--query-compute-apps=pid,used_memory,gpu_bus_id",
                      "--format=csv,noheader,nounits"]).splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        g = by_bus.get(parts[2].lower()[-12:])
        if g is not None:
            g["tenants"].append({"owner": _owner(int(parts[0]), containers),
                                 "mib": int(parts[1] or 0)})
    return {"gpus": gpus, "at": int(time.time())}


def minors_of(inv: dict) -> dict[int, int]:
    return {g["index"]: g["minor"] for g in inv.get("gpus") or []}


def show(unit: str) -> dict:
    return OI.parse_show(_run(["systemctl", "show", unit, "-p", "Environment", "-p", "DevicePolicy",
                               "-p", "DeviceAllow", "-p", "LoadState", "-p", "ActiveState",
                               "-p", "FragmentPath"]))


# ---------------------------------------------------------------------------
# llama.cpp servers (engine llamacpp / prism)
# ---------------------------------------------------------------------------
OLLAMA_MODELS = Path(os.environ.get("OLLAMA_MODELS", "/usr/share/ollama/.ollama/models"))


def is_llamacpp(inst: dict) -> bool:
    return inst.get("engine", "ollama") != "ollama"


def ollama_blob(name: str) -> Path | None:
    """The GGUF an Ollama model name stands for, in Ollama's own store.

    `qwen3.5:9b` is registry.ollama.ai/library/qwen3.5/9b; a name with a host
    in it (`hf.co/owner/repo:Q4_K_M`) keeps its path. The model layer is the
    GGUF itself -- but only a pulled hf.co file is the original: Ollama's own
    library conversions write some keys its way, and stock llama.cpp refuses
    them (qwen3.5:2b: "rope.dimension_sections has wrong array length;
    expected 4, got 3", b11171, 2026-09-24).
    """
    base, _, tag = name.partition(":")
    parts = base.split("/")
    # A host only leads a path: `qwen3.5` has a dot and is a library model.
    if not (len(parts) > 1 and "." in parts[0]):
        parts = ["registry.ollama.ai", *(["library"] if len(parts) == 1 else []), *parts]
    manifest = OLLAMA_MODELS / "manifests" / Path(*parts) / (tag or "latest")
    try:
        layers = json.loads(manifest.read_text()).get("layers") or []
    except (OSError, ValueError):
        return None
    for layer in layers:
        if layer.get("mediaType") == "application/vnd.ollama.image.model":
            blob = OLLAMA_MODELS / "blobs" / str(layer.get("digest", "")).replace(":", "-")
            return blob if re.fullmatch(r"sha256-[0-9a-f]{64}", blob.name) else None
    return None


def model_path(inst: dict) -> tuple[Path | None, str]:
    """(path, "") where a llama.cpp setup's GGUF is or will be; (None, why) if nowhere."""
    model = inst["model"]
    m = OI.HF_RE.fullmatch(model)
    if m:
        return Path(OI.MODELS_DIR) / "hf" / m.group(1) / m.group(2) / m.group(3), ""
    if model.startswith("/"):
        return Path(model), ""
    blob = ollama_blob(model)
    return (blob, "") if blob else (None, f"Ollama has no model {model!r} (ollama pull it first)")


def fetch_hf(inst: dict, dest: Path) -> str:
    """Download a hf: model once. "" or why not."""
    m = OI.HF_RE.fullmatch(inst["model"])
    url = f"https://huggingface.co/{m.group(1)}/{m.group(2)}/resolve/main/{m.group(3)}"
    part = dest.with_name(dest.name + ".part")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"  downloading {url} ...")
        # Hugging Face answers with a redirect to its CDN on another host,
        # which urllib follows (Ollama's puller refused it, 2026-09-24).
        with urllib.request.urlopen(url, timeout=60) as r, part.open("wb") as fh:
            shutil.copyfileobj(r, fh, 1 << 22)
        part.rename(dest)
        for p in (dest, *dest.parents):
            if p == Path(OI.MODELS_DIR).parent:
                break
            p.chmod(0o755 if p.is_dir() else 0o644)
        return ""
    except (OSError, ValueError) as exc:
        part.unlink(missing_ok=True)
        return f"download failed: {exc}"


def build_for(inst: dict) -> tuple[str, str]:
    """(installed bin dir, "") for the setup's flavour; ("", why) if it is not built."""
    flavor = "vanilla" if inst["engine"] == "llamacpp" else "prism"
    meta = LC.built(flavor)
    if not meta:
        return "", f"llama.cpp {flavor} is not built (./home-stack llamacpp build {flavor})"
    return str(LC.INSTALLED / LC.build_name(flavor) / "bin"), ""


def install_build(inst: dict) -> str:
    """Copy the setup's build into the root-owned place its unit runs from."""
    flavor = "vanilla" if inst["engine"] == "llamacpp" else "prism"
    src, dst = LC.build_dir(flavor) / "bin", LC.INSTALLED / LC.build_name(flavor) / "bin"
    stamp_src, stamp_dst = LC.build_dir(flavor) / "build.json", dst.parent / "build.json"
    try:
        if stamp_dst.is_file() and stamp_dst.read_text() == stamp_src.read_text():
            return ""
    except OSError:
        pass
    _sudo(["mkdir", "-p", str(dst.parent)])
    _sudo(["rm", "-rf", str(dst)])
    _sudo(["cp", "-a", "--no-preserve=ownership", str(src), str(dst)])
    _sudo(["cp", str(stamp_src), str(stamp_dst)])
    _sudo(["chown", "-R", "root:root", str(dst.parent)])
    _sudo(["chmod", "-R", "go-w", str(dst.parent)])
    print(f"  installed {LC.build_name(flavor)} in {dst.parent}")
    return ""


def desired_llamacpp_unit(inst: dict, minors: dict[int, int]) -> tuple[str, str]:
    """(unit text, "") or ("", why it cannot run)."""
    bin_dir, why = build_for(inst)
    if why:
        return "", why
    path, why = model_path(inst)
    if why:
        return "", why
    return OI.render_llamacpp_unit(inst, bin_dir, str(path), minors), ""


def llamacpp_changes(inst: dict, minors: dict[int, int]) -> tuple[list[str], str]:
    text, why = desired_llamacpp_unit(inst, minors)
    if why:
        return [], why
    unit_file = SYSTEMD / f"{inst['unit']}.service"
    try:
        have = unit_file.read_text()
    except OSError:
        return [f"create {inst['unit']}.service"], ""
    changes = [] if have == text else [f"rewrite {inst['unit']}.service"]
    if not changes and show(inst["unit"])["props"].get("ActiveState") != "active":
        changes = [f"start {inst['unit']}.service"]
    path, _ = model_path(inst)
    if path is not None and not path.exists():
        changes.insert(0, f"download {inst['model']}")
    return changes, ""


def llamacpp_failure(unit: str) -> str:
    """Why a llama.cpp unit is not running, when it has failed: llama.cpp's own
    error line from the journal. "" while it is up or still starting."""
    props = show(unit)["props"]
    if props.get("ActiveState") not in ("failed",) and props.get("SubState") not in ("auto-restart", "failed"):
        return ""
    lines = _run(["journalctl", "-u", f"{unit}.service", "-n", "80", "--no-pager", "-o", "cat"]).splitlines()
    why = [ln for ln in lines if "error loading model" in ln or " E " in ln]
    return (why[0].split(" E ", 1)[-1].strip() if why else "it exits as soon as it starts")[:300]


def llamacpp_ready(inst: dict, wait_s: int = 0) -> str:
    """"" once the server answers /health ok (the model is loaded); else why.
    Stops waiting the moment the unit fails -- a model llama.cpp cannot load
    used to hold the apply for the whole wait while the unit looped."""
    deadline = time.time() + wait_s
    while True:
        failed = llamacpp_failure(inst["unit"])
        if failed:
            return f"does not start: {failed}"
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{inst['port']}/health", timeout=5) as r:
                if json.load(r).get("status") == "ok":
                    return ""
        except urllib.error.HTTPError as exc:
            if exc.code != 503:                    # 503 is "loading"
                return f"{exc.code} from /health"
        except (OSError, ValueError):
            pass
        if time.time() >= deadline:
            return "not answering /health yet"
        time.sleep(3)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def plan(cfg: dict, inv: dict) -> list[dict]:
    minors = minors_of(inv)
    rows = []
    for inst in OI.instances(cfg, enabled_only=False):
        row = {"id": inst["id"], "unit": inst["unit"], "managed": inst["managed"],
               "display": OI.display(inst), "changes": [], "state": "",
               "signature": OI.signature(inst)}
        if inst["url"]:
            row["state"] = "elsewhere"          # another machine: not ours to run
        elif not inst["managed"]:
            row["state"] = "unmanaged"
        elif is_llamacpp(inst):
            row["engine"] = inst["engine"]
            row["changes"], why = llamacpp_changes(inst, minors) if inst["enabled"] else ([], "")
            if why:
                row["error"] = why
            row["state"] = "blocked" if why else ("pending" if row["changes"] else "applied")
        else:
            shown = show(inst["unit"])
            row["active"] = shown["props"].get("ActiveState", "")
            if not inst["enabled"]:
                row["changes"] = ["stop and disable"] if row["active"] == "active" else []
            else:
                row["changes"] = OI.diff(inst, shown, minors)
                # A pinned model that is not loaded: after a reboot, or when
                # the setup's model changed and the unit did not. Loading it
                # needs no restart.
                if inst.get("pinned") and not row["changes"] and not loaded(inst):
                    row["changes"] = [f"load {inst['model']}"]
                    row["load_only"] = True
            row["state"] = "pending" if row["changes"] else "applied"
        rows.append(row)
    # A server taken off the list on the page: its unit still runs, holding
    # memory on a card the page now counts as free. Anything carrying this
    # stack's drop-in and no longer listed is stopped and removed -- never the
    # main unit, which is the Ollama installer's.
    listed = {r["unit"] for r in rows}
    for unit in stack_units():
        if unit in listed or unit == "ollama":
            continue
        rows.append({"id": unit.split("-", 1)[1], "unit": unit, "managed": True, "orphan": True,
                     "display": "removed from the list", "state": "pending",
                     "changes": ["stop, disable and remove"]})
    return rows


def _local_url(inst: dict) -> str:
    return inst["url"] or f"http://127.0.0.1:{inst['port']}"


def loaded(inst: dict) -> bool:
    try:
        with urllib.request.urlopen(_local_url(inst) + "/api/ps", timeout=4) as r:
            names = {m.get("name") or m.get("model") for m in (json.load(r).get("models") or [])}
    except (OSError, ValueError):
        return False
    want = inst["model"]
    return want in names or (":" not in want and f"{want}:latest" in names)


def warm(inst: dict) -> str:
    """Load a pinned setup's model, for good. "" or why not."""
    body = json.dumps({"model": inst["model"], "prompt": "", "keep_alive": -1}).encode()
    for _ in range(30):                        # the server may still be starting
        try:
            req = urllib.request.Request(_local_url(inst) + "/api/generate", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600):
                return ""
        except urllib.error.HTTPError as exc:
            return f"{exc.code} {exc.read().decode(errors='replace')[:200]}"
        except TimeoutError:
            # The server answered and is loading: asking again would only
            # queue another ten-minute wait behind this one.
            return "the load did not finish within 600 s"
        except (OSError, ValueError):
            time.sleep(2)
    return "the server did not answer"


def stack_units() -> list[str]:
    """Units this stack has written a drop-in for (managed at some point)."""
    ours = [d.parent.name[:-len(".service.d")]
            for d in SYSTEMD.glob(f"ollama*.service.d/{OI.DROPIN_NAME}")]
    for f in SYSTEMD.glob("llamacpp-*.service"):
        try:
            if "managed by home-stack" in f.read_text():
                ours.append(f.name[:-len(".service")])
        except OSError:
            pass
    return sorted(u for u in ours if OI.UNIT_RE.fullmatch(u))


def write_state(config_dir: Path, inv: dict, rows: list[dict]) -> None:
    """What the admin page reads: the cards, and whether the list is applied."""
    for name, doc in (("gpus.json", inv), ("ollama-plan.json", {"at": int(time.time()), "instances": rows})):
        path = config_dir / name
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, indent=1))
            tmp.replace(path)
        except OSError as exc:
            print(f"  (could not write {path}: {exc})")


def print_report(inv: dict, rows: list[dict], cfg: dict) -> None:
    print("\n  Cards")
    if not inv.get("gpus"):
        print(f"    none found ({inv.get('error', 'nvidia-smi answered nothing')})")
    for g in inv.get("gpus") or []:
        print(f"    GPU{g['index']}  {g['name']}  {g['used_mib'] / 1024:.1f} of {g['total_mib'] / 1024:.1f} GiB in use")
        for t in g["tenants"]:
            print(f"        {t['mib'] / 1024:5.2f} GiB  {t['owner']}")
    print("\n  Instances (cloud.ollama.instances)")
    if (((cfg.get("cloud") or {}).get("ollama") or {}).get("instances")) is None:
        print("    not listed yet -- read from the old cloud.ollama.local/vision/bench entries.\n"
              "    ./home-stack ollama --import writes the list from the units running now.")
    for r in rows:
        mark = {"applied": "ok", "pending": "->", "unmanaged": "  ", "elsewhere": "  ",
                "blocked": "!!"}[r["state"]]
        print(f"    {mark:2}  {r['id']:<10} {r['display']:<42} {r['unit']}.service"
              + ("" if r["managed"] else "  (not managed here)"))
        for c in r["changes"]:
            print(f"          {c}")
        if r.get("error"):
            print(f"          cannot run: {r['error']}")
    pending = [r for r in rows if r["changes"]]
    print()
    if pending:
        print(f"  {len(pending)} instance(s) differ from the list. ./home-stack ollama --apply "
              "restarts them, unloading whatever they hold.")
    else:
        print("  Every managed instance matches the list.")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
LABELS = {"main": "Text", "vision": "Vision", "bench": "Benchmark"}


def discover_units() -> list[str]:
    out = []
    for line in _run(["systemctl", "list-units", "--all", "--type=service", "--no-legend",
                      "--plain", "ollama*"]).splitlines():
        name = line.split()[0] if line.split() else ""
        if re.fullmatch(r"ollama(-[a-z][a-z0-9]{0,15})?\.service", name):
            out.append(name[:-len(".service")])
    return sorted(out, key=lambda u: (u != "ollama", u))


def do_import(path: Path, inv: dict, force: bool) -> int:
    cfg = load_cfg(path)
    conf = (cfg.get("cloud") or {}).get("ollama") or {}
    if conf.get("instances") is not None and not force:
        print("  cloud.ollama.instances is already written. --import --force replaces it.")
        return 1
    index_of_minor = {g["minor"]: g["index"] for g in inv.get("gpus") or []}
    legacy = {i["id"]: i for i in OI.legacy_instances(conf)}
    entries = []
    for unit in discover_units():
        iid = "main" if unit == "ollama" else unit[len("ollama-"):]
        shown = show(unit)
        if shown["props"].get("LoadState") != "loaded":
            continue
        extra = {"label": LABELS.get(iid, iid.capitalize()),
                 "host": (legacy.get("main") or {}).get("host", "compute")}
        if iid == "bench":
            extra["purpose"] = "bench"
        entry = OI.instance_from_unit(iid, unit, shown, index_of_minor, **extra)
        OI.normalize(entry)                                # refuse now, not at deploy
        entries.append(entry)
    if not entries:
        print("  no ollama*.service units found; nothing imported.")
        return 1
    _write_instances(path, entries)
    print(f"  wrote {len(entries)} instance(s) to cloud.ollama.instances in {path}:")
    for e in entries:
        print(f"    {e['id']:<8} {OI.display(OI.normalize(e))}  ({e['unit']}.service)")
    return 0


def _write_instances(path: Path, entries: list[dict]) -> None:
    """Round-trip, so the file's comments survive; same inode, so the admin
    container's single-file mount still sees it."""
    from ruamel.yaml import YAML
    import deploy
    y = YAML()
    y.preserve_quotes = True
    y.width = 100          # admin/app.py's setting: nothing else in the file re-wraps
    doc = y.load(path.read_text())
    cloud = doc.setdefault("cloud", {})
    oll = cloud.setdefault("ollama", {})
    oll["instances"] = entries
    tmp = path.with_suffix(".import.tmp")
    with tmp.open("w") as fh:
        y.dump(doc, fh)
    deploy.write_in_place(tmp, path)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def _sudo(argv: list[str], data: str | None = None) -> None:
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    out = subprocess.DEVNULL if argv[0] == "tee" else None
    subprocess.run([*prefix, *argv], input=data, text=True, check=True, stdout=out)


def do_apply(cfg: dict, inv: dict, rows: list[dict], yes: bool) -> int:
    todo = [r for r in rows if r["changes"]]
    if not todo:
        print("  nothing to apply.")
        return 0
    print("  This restarts, unloading whatever each holds (a turn in flight on it fails):")
    for r in todo:
        print(f"    {r['unit']}.service  ({r['id']})")
    if not yes:
        if not sys.stdin.isatty():
            print("  not a terminal; pass --yes to apply without asking.")
            return 1
        if input("  apply? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  nothing changed.")
            return 1
    minors = minors_of(inv)
    by = {i["id"]: i for i in OI.instances(cfg, enabled_only=False)}
    ollama_bin = shutil.which("ollama") or "/usr/local/bin/ollama"
    for r in [r for r in todo if r.get("orphan")]:
        unit = r["unit"]
        if not OI.UNIT_RE.fullmatch(unit) or unit == "ollama":
            continue
        _sudo(["systemctl", "disable", "--now", f"{unit}.service"])
        _sudo(["rm", "-f", str(SYSTEMD / f"{unit}.service.d" / OI.DROPIN_NAME)])
        unit_file = SYSTEMD / f"{unit}.service"
        # Only a unit this stack wrote (render_unit's Description); one the
        # household wrote by hand is stopped and left on disk.
        try:
            ours = "managed by home-stack" in unit_file.read_text()
        except OSError:
            ours = False
        if ours:
            _sudo(["rm", "-f", str(unit_file)])
        print(f"  stopped {unit}.service (no longer on the list)")
    todo = [r for r in todo if not r.get("orphan")]
    loads = [r for r in todo if r.get("load_only")]
    todo = [r for r in todo if not r.get("load_only")]
    for r in todo:
        inst = by[r["id"]]
        unit = inst["unit"]
        if not inst["enabled"]:
            _sudo(["systemctl", "disable", "--now", f"{unit}.service"])
            continue
        if is_llamacpp(inst):
            path, _ = model_path(inst)
            if path is not None and not path.exists() and OI.HF_RE.fullmatch(inst["model"]):
                why = fetch_hf(inst, path)
                if why:
                    print(f"  {unit}: {why}")
                    r["skip"] = True
                    continue
            install_build(inst)
            text, why = desired_llamacpp_unit(inst, minors)
            if why:
                print(f"  {unit}: {why}")
                r["skip"] = True
                continue
            _sudo(["tee", str(SYSTEMD / f"{unit}.service")], text)
            continue
        unit_file = SYSTEMD / f"{unit}.service"
        if not unit_file.exists() and not show(unit)["props"].get("FragmentPath"):
            _sudo(["tee", str(unit_file)], OI.render_unit(inst, ollama_bin))
        _sudo(["mkdir", "-p", str(SYSTEMD / f"{unit}.service.d")])
        _sudo(["tee", str(SYSTEMD / f"{unit}.service.d" / OI.DROPIN_NAME)], OI.render_dropin(inst, minors))
    _sudo(["systemctl", "daemon-reload"])
    for r in todo:
        inst = by[r["id"]]
        if r.get("skip"):
            continue
        if inst["enabled"] and not r.get("orphan"):
            # A restart, not a reload: reload does not move a running process
            # between cgroups, so a new card pin would not take.
            _sudo(["systemctl", "enable", f"{inst['unit']}.service"])
            _sudo(["systemctl", "restart", f"{inst['unit']}.service"])
            print(f"  restarted {inst['unit']}.service")
            if is_llamacpp(inst):
                why = llamacpp_ready(inst, wait_s=600)
                print(f"  {inst['unit']}: loaded {inst['model']}" if not why
                      else f"  {inst['unit']}: {why}")
            elif inst.get("pinned"):
                loads.append(r)
    for r in loads:
        inst = by[r["id"]]
        why = warm(inst)
        print(f"  loaded {inst['model']} on {inst['unit']}" if not why
              else f"  could not load {inst['model']} on {inst['unit']}: {why}")
    return 0


# ---------------------------------------------------------------------------
# The page's Apply button
# ---------------------------------------------------------------------------
def install_trigger(path: Path) -> int:
    config_dir = path.parent
    _sudo(["mkdir", "-p", str(HELPER_DIR)])
    for name in ("ollama_host.py", "ollama_instances.py", "llamacpp.py", "model_library.py"):
        _sudo(["install", "-m", "0644", "-o", "root", "-g", "root", str(HERE / name), str(HELPER_DIR / name)])
    py = "/usr/bin/python3"
    _sudo(["tee", str(SYSTEMD / f"{TRIGGER}.path")],
          "[Unit]\nDescription=Apply cloud.ollama.instances when the admin page asks\n\n"
          f"[Path]\nPathExists={config_dir / REQUEST}\nUnit={TRIGGER}.service\n\n"
          "[Install]\nWantedBy=multi-user.target\n")
    _sudo(["tee", str(SYSTEMD / f"{TRIGGER}.service")],
          "[Unit]\nDescription=Apply cloud.ollama.instances (home-stack)\n\n"
          "[Service]\nType=oneshot\n"
          f"Environment=HOME_STACK_CONFIG={path}\n"
          f"ExecStart={py} {HELPER_DIR / 'ollama_host.py'} --from-trigger\n")
    _sudo(["systemctl", "daemon-reload"])
    _sudo(["systemctl", "enable", "--now", f"{TRIGGER}.path"])
    marker = config_dir / MARKER
    marker.write_text(json.dumps({"version": OI.HELPER_VERSION, "at": int(time.time())}))
    with contextlib.suppress(OSError):
        owner = config_dir.stat()
        os.chown(marker, owner.st_uid, owner.st_gid)
    print(f"  installed. The Models page's Apply button now applies the saved list;\n"
          f"  its log is {config_dir / APPLY_LOG}.")
    return 0


def from_trigger(path: Path) -> int:
    """Run by the root service. Applies the saved list; the request only says 'now'."""
    config_dir = path.parent
    request = config_dir / REQUEST
    if not request.exists():
        return 0
    try:
        what = str((json.loads(request.read_text() or "{}") or {}).get("what") or "apply")
    except (OSError, ValueError):
        what = "apply"
    request.unlink(missing_ok=True)
    owner = config_dir.stat()
    log_path = config_dir / APPLY_LOG
    # Line-buffered: the page follows this file while an apply runs, and an
    # apply that downloads a model or waits for one to load takes minutes.
    with log_path.open("w", buffering=1) as log:
        sys.stdout = log
        try:
            cfg = load_cfg(path)
            if what == "library":
                # The model library's queue: pulls, downloads, deletes, and a
                # test of each model on every engine that could run it.
                print(time.strftime("%Y-%m-%d %H:%M:%S"), "model library work from the admin page")
                rc = 1 if ML.run_queue(config_dir, cfg, ollama_blob) else 0
                print("done." if rc == 0 else "failed.")
                raise SystemExit(rc)
            print(time.strftime("%Y-%m-%d %H:%M:%S"), "apply requested from the admin page")
            inv = inventory()
            rows = plan(cfg, inv)
            rc = do_apply(cfg, inv, rows, yes=True)
            ML.refresh(config_dir)
            inv = inventory()
            rows = plan(cfg, inv)
            write_state(config_dir, inv, rows)
            print_report(inv, rows, cfg)
            print("done." if rc == 0 else "failed.")
        except SystemExit as exc:
            rc = int(exc.code or 0)
        except Exception as exc:                          # noqa: BLE001
            print(f"failed: {exc}")
            rc = 1
        finally:
            sys.stdout = sys.__stdout__
    for name in (APPLY_LOG, "gpus.json", "ollama-plan.json", ML.LIBRARY):
        try:
            os.chown(config_dir / name, owner.st_uid, owner.st_gid)
        except OSError:
            pass
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="home-stack ollama", description=__doc__.split("\n\n")[0])
    ap.add_argument("--import", dest="do_import", action="store_true",
                    help="write cloud.ollama.instances from the running units")
    ap.add_argument("--force", action="store_true", help="with --import: replace an existing list")
    ap.add_argument("--apply", action="store_true", help="make the units match the list")
    ap.add_argument("--yes", action="store_true", help="with --apply: do not ask")
    ap.add_argument("--json", action="store_true", help="print the plan as JSON")
    ap.add_argument("--install-trigger", action="store_true",
                    help="once, with sudo: let the admin page apply the list")
    ap.add_argument("--from-trigger", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--library", action="store_true",
                    help="the local model library: what is on disk and what each engine runs")
    ap.add_argument("--library-run", action="store_true",
                    help="run the page's queued library work here (pulls, downloads, tests)")
    ap.add_argument("--library-test", metavar="MODEL",
                    help="test one library model on every engine that could run it")
    args = ap.parse_args(argv)
    path = config_path()
    if args.from_trigger:
        return from_trigger(path)
    if args.install_trigger:
        return install_trigger(path)
    if args.library or args.library_run or args.library_test:
        cfg = load_cfg(path)
        if args.library_test:
            queue = path.parent / ML.QUEUE
            jobs = json.loads(queue.read_text()) if queue.exists() else []
            queue.write_text(json.dumps([*jobs, {"op": "test", "model": args.library_test}]))
        rc = 1 if (args.library_run or args.library_test) and ML.run_queue(path.parent, cfg, ollama_blob) else 0
        doc = ML.refresh(path.parent)
        for m in doc["models"]:
            tests = doc["tests"].get(m["id"]) or {}
            marks = "  ".join(f"{OI.ENGINE_LABELS[e]}: " + ("ok" if (tests.get(e) or {}).get("ok")
                              else "FAILS" if e in tests else "untested") for e in m["engines"])
            print(f"  {m['id']:<60} {m['bytes'] / 2**30:5.1f} GiB  {marks}")
        return rc
    inv = inventory()
    if args.do_import:
        rc = do_import(path, inv, args.force)
        if rc:
            return rc
    cfg = load_cfg(path)
    try:
        rows = plan(cfg, inv)
    except OI.InstanceError as exc:
        print(f"  {exc}")
        return 2
    if args.apply:
        rc = do_apply(cfg, inv, rows, args.yes)
        inv = inventory()
        rows = plan(cfg, inv)
        write_state(path.parent, inv, rows)
        print_report(inv, rows, cfg)
        return rc
    write_state(path.parent, inv, rows)
    if args.json:
        print(json.dumps({"gpus": inv, "instances": rows}, indent=1))
    else:
        print_report(inv, rows, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())

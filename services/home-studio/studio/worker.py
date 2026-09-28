"""The process that holds WanGP, and so the card.

    python -m studio.worker <report fd>

The manager starts it when there is work and stops it when there has been
none for a while: WanGP has no call that gives the card and the RAM back
(its session keeps the last model loaded, which is what makes the next job
of the same kind fast), so ending the process is how they are released. A
model that crashes takes this process with it, not the queue or the page.

Commands arrive on stdin, one JSON object per line:
    {"run": "<job id>", "settings": {...}, "output_dir": "..."}
    {"cancel": "<job id>"}
Reports go to the fd given on the command line -- never stdout, which WanGP
writes its console to:
    {"kind": "ready"}
    {"kind": "progress", "id", "phase", "progress", "step", "steps"}
    {"kind": "done", "id", "success", "files", "errors", "seconds"}
    {"kind": "preview", "id", "file"}      the latest in-progress picture, a JPEG
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

# How often an in-progress picture is written: a step is tens of seconds on
# this card, so this mostly means "every step" without ever meaning more.
PREVIEW_EVERY_S = 3.0
WANGP = Path(os.environ.get("WANGP_ROOT", "/opt/wangp"))
# SDPA: SageAttention breaks H3's text encoder (WanGP issue #2182). Profile 5
# is the low-VRAM, RAM-heavy one the 12 GB card needs for H3.
CLI_ARGS = ["--attention", "sdpa", "--profile", os.environ.get("WANGP_PROFILE", "5"),
            "--perc-reserved-mem-max", os.environ.get("WANGP_RESERVED_MEM", "0.1")]


def main() -> int:
    report = os.fdopen(int(sys.argv[1]), "w", buffering=1)
    lock = threading.Lock()

    def send(**msg) -> None:
        with lock:
            report.write(json.dumps(msg, ensure_ascii=False) + "\n")
            report.flush()

    sys.path.insert(0, str(WANGP))
    os.chdir(WANGP)
    from shared.api import init  # noqa: PLC0415 -- WanGP is only importable from its root

    scratch = Path(os.environ.get("STUDIO_SCRATCH", "/tmp/studio-scratch"))
    scratch.mkdir(parents=True, exist_ok=True)
    session = init(root=WANGP, output_dir=scratch, cli_args=CLI_ARGS, console_output=True)
    send(kind="ready")

    jobs: queue.Queue = queue.Queue()
    current: dict = {"id": None, "job": None}

    def commands() -> None:
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("run"):
                jobs.put(msg)
            elif msg.get("cancel") and msg["cancel"] == current["id"] and current["job"] is not None:
                current["job"].cancel()
        jobs.put(None)                                     # stdin closed: the manager is gone

    threading.Thread(target=commands, daemon=True).start()
    while True:
        msg = jobs.get()
        if msg is None:
            return 0
        job_id, started = msg["run"], time.time()
        try:
            settings = {**session.get_default_settings(msg["settings"]["model_type"]), **msg["settings"]}
            job = session.submit_task(settings)
        except Exception as exc:                           # noqa: BLE001 -- reported, not raised
            send(kind="done", id=job_id, success=False, files=[], errors=[str(exc)[:500]], seconds=0)
            continue
        current.update(id=job_id, job=job)
        last, last_preview = None, 0.0
        preview_file = scratch / "previews" / f"{job_id}.jpg"
        for ev in job.events.iter(timeout=1.0):
            if ev.kind == "preview" and time.time() - last_preview >= PREVIEW_EVERY_S:
                # What the denoiser has so far, decoded from the latents on the
                # CPU by WanGP itself. Written whole and renamed, so a reader
                # never gets half a JPEG.
                image = getattr(ev.data, "image", None)
                if image is not None:
                    try:
                        preview_file.parent.mkdir(parents=True, exist_ok=True)
                        tmp = preview_file.with_suffix(".tmp")
                        image.convert("RGB").save(tmp, "JPEG", quality=80)
                        tmp.replace(preview_file)
                        send(kind="preview", id=job_id, file=str(preview_file))
                        last_preview = time.time()
                    except Exception:                      # noqa: BLE001 -- a preview is never worth a job
                        pass
            elif ev.kind == "progress":
                p = ev.data
                now = (p.phase, round(float(p.progress or 0), 3), p.current_step)
                if now != last:
                    send(kind="progress", id=job_id, phase=str(p.phase or ""),
                         progress=float(p.progress or 0), step=p.current_step or 0, steps=p.total_steps or 0)
                    last = now
        res = job.result()
        current.update(id=None, job=None)
        send(kind="done", id=job_id, success=bool(res.success),
             files=[str(f) for f in res.generated_files],
             errors=[str(e.message)[:500] for e in res.errors], seconds=round(time.time() - started, 1))


if __name__ == "__main__":
    sys.exit(main())

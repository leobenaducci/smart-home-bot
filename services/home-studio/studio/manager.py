"""The manager: one queue, one card, one worker at a time.

Runs in the API process as a thread. Each round: the next job in the fair
order (store.Store.order), resolved against its project *now* -- a shot that
continues the one before it starts from that shot's chosen take as it is at
this moment, not as it was when the job was queued -- then sent to the
worker, followed, and filed into its project.

The worker is started on demand and stopped after `idle_s` with nothing to
do, which gives the card and the RAM back (worker.py says why that is the
only way). A worker that dies fails the job it was running and is started
fresh for the next one.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import analysis, media, recipes
from .projects import ProjectError, Projects
from .store import Store

log = logging.getLogger("studio.manager")


# Where each WanGP phase sits in one bar from 0 to 1. WanGP's own `progress`
# is per phase and on its own scale (the first real job read "100%" while the
# model was still loading), so the steps decide where the step is known.
PHASE_SPAN = {"loading": (0.0, 0.05), "loading_model": (0.05, 0.15), "encoding_text": (0.15, 0.2),
              "inference": (0.2, 0.95), "denoising": (0.2, 0.95), "decoding": (0.95, 1.0)}


def overall_progress(msg: dict, time_share: float | None = None) -> float:
    """One bar from WanGP's phases. Steps decide where they are known; where
    they are not, `time_share` -- how much of this kind of job's usual time
    has gone -- does, and WanGP's own figure only when neither exists.

    Its own figure is not a measure of anything in a stage with no steps:
    ACE-Step reports 100% as its lyrics-to-music stage *starts*, and a song
    then sat at 95% for five minutes and read as stuck (2026-09-28)."""
    phase = str(msg.get("phase") or "").lower()
    lo, hi = next((span for key, span in PHASE_SPAN.items() if key in phase), (0.2, 0.95))
    steps, step = msg.get("steps") or 0, msg.get("step") or 0
    if steps:
        within = step / steps
    elif time_share is not None:
        # Never quite the end of the stage: a job running long should look
        # slow, not finished.
        within = min(0.9, max(0.0, time_share))
    else:
        raw = float(msg.get("progress") or 0)
        within = raw / 100 if raw > 1 else raw
    return round(lo + (hi - lo) * max(0.0, min(1.0, within)), 3)


class Worker:
    """The child process and its report pipe."""

    def __init__(self, scratch: Path, log_path: Path):
        read_fd, write_fd = os.pipe()
        env = {**os.environ, "STUDIO_SCRATCH": str(scratch)}
        self.log = open(log_path, "ab", buffering=0)
        self.proc = subprocess.Popen([sys.executable, "-m", "studio.worker", str(write_fd)],
                                     stdin=subprocess.PIPE, stdout=self.log, stderr=subprocess.STDOUT,
                                     pass_fds=(write_fd,), env=env, cwd=str(Path(__file__).parent.parent),
                                     text=True, bufsize=1)
        os.close(write_fd)
        self.reports = os.fdopen(read_fd, "r", buffering=1)
        self.model = ""
        self.last_used = time.time()

    def send(self, **msg) -> None:
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def read(self) -> dict | None:
        line = self.reports.readline()
        return json.loads(line) if line else None

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
        self.log.close()


class Manager:
    def __init__(self, store: Store, projects: Projects, scratch: Path, logs: Path,
                 idle_s: float = 600, notify: Callable[[dict], None] | None = None,
                 worker_factory: Callable[[], Worker] | None = None,
                 audio: "analysis.AudioServer | None" = None, characters=None):
        self.store, self.projects = store, projects
        self.audio, self.characters = audio, characters
        self.scratch, self.logs = Path(scratch), Path(logs)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.idle_s, self.notify = idle_s, notify
        self._factory = worker_factory or (lambda: Worker(self.scratch, self.logs / "worker.log"))
        self.worker: Worker | None = None
        # The latest in-progress picture of the running job, by job id: shown
        # to its owner while the card works, removed when it finishes.
        self.previews: dict[str, Path] = {}
        self.paused = False
        # Why the card is paused, for the page to say: "update" when the
        # deployer holds it to restart the studio, "" when a parent did.
        self.pause_reason = ""
        self._cancelling: set[str] = set()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="studio-manager", daemon=True)

    # -- control ------------------------------------------------------------
    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.worker:
            self.worker.stop()

    def wake(self) -> None:
        self._wake.set()

    def cancel(self, job_id: str) -> str | None:
        """Cancel queued or running. The state it was in, or None."""
        job = self.store.get(job_id)
        if not job:
            return None
        if job["state"] == "running" and self.worker and self.worker.alive():
            # Remembered here, not only in the job's phase: the worker keeps
            # reporting progress until WanGP stops, and each report rewrote the
            # phase -- so a cancelled job ended "failed" and its owner was told
            # it could not be made.
            self._cancelling.add(job_id)
            self.worker.send(cancel=job_id)
            self.store.update(job_id, phase="cancelling")
            return "running"
        return self.store.cancel(job_id)

    def status(self) -> dict:
        return {"worker": bool(self.worker and self.worker.alive()),
                "model": self.worker.model if self.worker else "",
                "paused": self.paused, "pause_reason": self.pause_reason if self.paused else ""}

    # -- the loop -----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._fail_blocked()
                job = None if self.paused else self.store.next_job(self.worker.model if self.worker else "")
                if job is None:
                    if self.worker and time.time() - self.worker.last_used > self.idle_s:
                        log.info("idle for %ss: stopping the worker, the card is free", int(self.idle_s))
                        self.worker.stop()
                        self.worker = None
                    self._wake.wait(timeout=5)
                    self._wake.clear()
                    continue
                self._run(job)
            except Exception:                                  # noqa: BLE001 -- the loop must not die
                log.exception("manager round failed")
                time.sleep(5)

    def _fail_blocked(self) -> None:
        for job in self.store.blocked_forever():
            self.store.update(job["id"], state="failed", finished=time.time(),
                              error="the shot it continues from was not made")

    def _ensure_worker(self) -> Worker:
        if self.worker and not self.worker.alive():
            self.worker = None
        if self.worker is None:
            self.worker = self._factory()
            msg = self.worker.read()
            if not msg or msg.get("kind") != "ready":
                self.worker.stop()
                self.worker = None
                raise RuntimeError("the worker did not start; see worker.log")
        return self.worker

    def _run(self, job: dict) -> None:
        if job["kind"] == "analyze":
            return self._analyze(job)
        if job["kind"] == "repaint":
            return self._repaint(job)
        try:
            params = self._resolve(job)
            settings = recipes.settings_for(job["kind"], params)
        except (recipes.RecipeError, ProjectError, media.MediaError) as exc:
            self.store.update(job["id"], state="failed", finished=time.time(), error=str(exc))
            self._notify(job, ok=False)
            return
        self.store.update(job["id"], state="running", started=time.time(), progress=0, phase="loading")
        try:
            worker = self._ensure_worker()
        except RuntimeError as exc:
            self.store.update(job["id"], state="failed", finished=time.time(), error=str(exc))
            self._notify(job, ok=False)
            return
        out_dir = self.scratch / job["id"]
        worker.send(run=job["id"], settings=settings, output_dir=str(out_dir))
        worker.model = settings["model_type"]
        result, began, expected = None, time.time(), max(30.0, self.store.seconds_for_job(job))
        while True:
            msg = worker.read()
            if msg is None:
                break                                          # the worker died
            if msg.get("id") != job["id"]:
                continue
            if msg["kind"] == "progress":
                share = (time.time() - began) / expected
                self.store.update(job["id"], progress=overall_progress(msg, share),
                                  **({} if job["id"] in self._cancelling else {"phase": msg.get("phase", "")[:60]}))
            elif msg["kind"] == "preview" and msg.get("file"):
                self.previews[job["id"]] = Path(msg["file"])
            elif msg["kind"] == "done":
                result = msg
                break
        worker.last_used = time.time()
        stale = self.previews.pop(job["id"], None)
        if stale:
            stale.unlink(missing_ok=True)
        if result is None:
            self._cancelling.discard(job["id"])
            self.worker = None
            self.store.update(job["id"], state="failed", finished=time.time(),
                              error="the generator stopped unexpectedly (see worker.log)")
            self._notify(job, ok=False)
            return
        current = self.store.get(job["id"])
        was_cancelled = job["id"] in self._cancelling
        self._cancelling.discard(job["id"])
        if not result["success"]:
            cancelled = was_cancelled or bool(current and current["phase"] == "cancelling")
            self.store.update(job["id"], state="cancelled" if cancelled else "failed", finished=time.time(),
                              error="" if cancelled else "; ".join(result.get("errors") or [])[:1000])
            if not cancelled:
                self._notify(job, ok=False)
            return
        try:
            files = self._file(job, [Path(f) for f in result.get("files") or []])
        except (ProjectError, media.MediaError, OSError) as exc:
            self.store.update(job["id"], state="failed", finished=time.time(), error=f"could not file the result: {exc}")
            self._notify(job, ok=False)
            return
        self.store.update(job["id"], state="done", finished=time.time(), progress=1.0, phase="", files=files)
        self._notify(job, ok=True)

    def _analyze(self, job: dict) -> None:
        """A song listened to (studio/analysis.py), for a music video.

        In the queue like everything else on the card, and the card given back
        before the next job starts: the audio server unloads its model a few
        seconds after using it, and this waits for that. The result is a file
        beside the take and a summary on it; nobody is notified -- the page
        that asked is waiting for it."""
        self.store.update(job["id"], state="running", started=time.time(), progress=0.05, phase="listening")
        work = self.scratch / job["id"]
        try:
            owner, pid = job["owner"], job["project"]
            doc = self.projects.load(owner, pid)
            found = Projects.find(doc, job["target"])
            if not found:
                raise ProjectError("the song is gone")
            item = found[2]
            take = next((t for t in item.get("takes") or [] if t.get("id") == job["params"].get("take")), None)
            if not take:
                raise ProjectError("that version of the song is gone")
            base = self.projects.dir(owner, pid)
            words = item.get("lyrics") if (item.get("kind") or "song") == "song" else item.get("text")
            language = str(item.get("language") or (doc.get("settings") or {}).get("language") or "es")[:5]
            result = analysis.analyze(
                base / take["file"], str(words or ""), language, self.audio, work,
                progress=lambda phase, share: self.store.update(job["id"], progress=share, phase=phase))
            rel = f"takes/{item['id']}/{take['id']}-analysis.json"
            (base / rel).write_text(json.dumps(result, ensure_ascii=False))
            self.projects.set_take_field(owner, pid, item["id"], take["id"], "analysis", {
                "file": rel, "tempo": result["tempo"], "aligned": result["aligned"],
                "sections": len(result["sections"]), "duration": result["duration"],
                "error": result["error"]})
            self.store.update(job["id"], state="done", finished=time.time(), progress=1.0, phase="", files=[rel])
        except Exception as exc:                               # noqa: BLE001 -- reported on the job
            log.exception("analysis %s failed", job["id"])
            self.store.update(job["id"], state="failed", finished=time.time(), error=str(exc)[:500])
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if self.audio and self.audio.url:
                self.audio.wait_idle()

    def _repaint(self, job: dict) -> None:
        """Only a stretch of a song made again, on the audio unit's ACE-Step.

        ACE-Step there is ~6 GB, so the video generator is let go first if it
        is resident -- the next video job loads it again, which is the price of
        sharing one card -- and the audio server unloads when it is done."""
        self.store.update(job["id"], state="running", started=time.time(), progress=0.05, phase="loading_model")
        work = self.scratch / job["id"]
        try:
            if not (self.audio and self.audio.url):
                raise RuntimeError("the Studio's audio unit is not configured")
            p = self._resolve(job)
            if self.worker:
                self.worker.stop()
                self.worker = None
            self.store.update(job["id"], progress=0.2, phase="inference")
            made = self.audio.repaint(Path(p["source_file"]), work, start=float(p["start"]), end=float(p["end"]),
                                      lyrics=str(p.get("lyrics") or ""), style=str(p.get("style") or ""),
                                      language=str(p.get("language") or "es"), seed=int(time.time()) % 1_000_000)
            files = self._file(job, [made])
            self.store.update(job["id"], state="done", finished=time.time(), progress=1.0, phase="", files=files)
            self._notify(job, ok=True)
        except Exception as exc:                               # noqa: BLE001 -- reported on the job
            log.exception("repaint %s failed", job["id"])
            self.store.update(job["id"], state="failed", finished=time.time(), error=str(exc)[:500])
            self._notify(job, ok=False)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if self.audio and self.audio.url:
                self.audio.wait_idle()

    # -- before and after ---------------------------------------------------
    def _resolve(self, job: dict) -> dict:
        """The job's params with every reference turned into a file on disk,
        read from the project as it is now."""
        p = dict(job["params"])
        if not job["project"]:
            return p
        owner, pid = job["owner"], job["project"]
        doc = self.projects.load(owner, pid)
        base = self.projects.dir(owner, pid)

        def take_frame(item_id: str, key: str) -> str:
            found = Projects.find(doc, item_id)
            take = Projects.chosen_take(found[2]) if found else None
            if not take or not take.get(key):
                raise ProjectError("the shot it has to match has no take yet")
            return str(base / take[key])

        if p.get("continue_from"):
            p["start_image"] = take_frame(p["continue_from"], "last")
        if p.get("end_at"):
            p["end_image"] = take_frame(p["end_at"], "first")
        if p.get("start_upload"):
            p["start_image"] = str(self.projects.file(owner, pid, p["start_upload"]))
        if p.get("voice_upload"):
            p["voice_file"] = str(self.projects.file(owner, pid, p["voice_upload"]))
        if p.get("voice_char") and self.characters:
            # A character speaking: its own voice sample is what is cloned.
            ch = self.characters.get(p["voice_char"], owner, pid)
            if not ch.get("voice"):
                raise ProjectError("this character has no voice sample yet")
            p["voice_file"] = str(self.characters.file(ch["id"], owner, pid, ch["voice"]))
        if p.get("start_board"):
            # The approved storyboard frame: what the shot starts from.
            p["start_image"] = str(self.projects.file(owner, pid, p["start_board"]))
        if p.get("from_take"):
            # The version a retouch starts from, read when it runs: deleted in
            # the meantime is a clear failure, not a retouch of another one.
            found = Projects.find(doc, job["target"])
            src = next((t for t in (found[2].get("takes") or []) if t.get("id") == p["from_take"]), None) if found else None
            if not src:
                raise ProjectError("the version to retouch is gone")
            p["source_file"] = str(base / src["file"])
        if job["kind"] == "edit":
            found = Projects.find(doc, job["target"])
            take = Projects.chosen_take(found[2]) if found else None
            if not take:
                raise ProjectError("nothing to edit yet")
            p["source_video"] = str(media.for_generator(
                base / take["file"], self.scratch / job["id"] / "source.mp4"))
            p.setdefault("start_image", str(base / take["first"]))
            p.setdefault("end_image", str(base / take["last"]))
        return p

    def _file(self, job: dict, produced: list[Path]) -> list[str]:
        """Move what the card made into the project (or the person's loose
        results) and record the take. Paths returned relative to the project."""
        produced = [f for f in produced if f.is_file()]
        if not produced:
            raise media.MediaError("the generator reported success but wrote nothing")
        if job["kind"] == "portrait" and self.characters:
            # A picture of a character goes to the character, not a project item.
            self.characters.add_picture_file(job["target"], job["owner"], job["project"], produced[0],
                                             admin=bool(job["params"].get("admin")))
            shutil.rmtree(self.scratch / job["id"], ignore_errors=True)
            return []
        owner, pid = job["owner"], job["project"] or "loose"
        if not job["project"]:
            dest_root = self.projects.root / owner / ".loose"
        else:
            dest_root = self.projects.dir(owner, pid)
        take_dir = dest_root / "takes" / (job["target"] or job["id"])
        take_dir.mkdir(parents=True, exist_ok=True)
        rel_files, take = [], {"job": job["id"], "kind": job["kind"]}
        # A song's version remembers the words it was sung with: a retouch can
        # change them, and the version kept should not say otherwise.
        if job["kind"] in ("song", "repaint") and job["params"].get("lyrics") is not None:
            take["lyrics"] = str(job["params"]["lyrics"])[:4000]
        if job["params"].get("from_take"):
            take["from"] = job["params"]["from_take"]
        for i, src in enumerate(produced):
            dst = take_dir / f"{job['id']}-{i}{src.suffix.lower()}"
            shutil.move(str(src), dst)
            if dst.suffix == ".mp4":
                try:
                    media.compress_video(dst)
                except (media.MediaError, OSError, subprocess.SubprocessError) as exc:
                    log.warning("could not compress %s: %s", dst.name, exc)
            if dst.suffix in media.LOSSLESS_AUDIO:
                try:
                    dst = media.compress_audio(dst)
                except (media.MediaError, OSError, subprocess.SubprocessError) as exc:
                    # Kept as it came: a big file beats a lost take.
                    log.warning("could not compress %s: %s", dst.name, exc)
            rel = str(dst.relative_to(dest_root))
            rel_files.append(rel)
            if i == 0:
                take["file"] = rel
                if dst.suffix in (".mp4", ".mov", ".webm", ".mkv"):
                    info = media.probe(dst)
                    take["seconds"] = round(info["seconds"], 2)
                    take["first"] = str(media.frame(dst, take_dir / f"{job['id']}-first.png", "first").relative_to(dest_root))
                    take["last"] = str(media.frame(dst, take_dir / f"{job['id']}-last.png", "last").relative_to(dest_root))
                elif dst.suffix in (".wav", ".mp3", ".flac", ".ogg"):
                    take["seconds"] = round(media.probe(dst)["seconds"], 2)
        shutil.rmtree(self.scratch / job["id"], ignore_errors=True)
        if job["params"].get("voice_char") and self.characters:
            # A voice test lives with the character, one per person who tried it.
            self.characters.store_voice_test(job["params"]["voice_char"], owner, pid, dest_root / rel_files[0])
            return []
        elif job["project"] and job["target"]:
            if job["kind"] == "board":
                self.projects.add_board(owner, pid, job["target"], {"file": take["file"], "job": job["id"],
                                                                     "prompt": job["params"].get("shot_prompt", "")})
            else:
                # The frame a video started from, so the storyboard can tell
                # a video made before its frame was drawn -- one to make again.
                if job["params"].get("start_board"):
                    take["board"] = job["params"]["start_board"]
                self.projects.add_take(owner, pid, job["target"], take)
        return rel_files

    def _notify(self, job: dict, ok: bool) -> None:
        if self.notify:
            try:
                self.notify({**(self.store.get(job["id"]) or job), "ok": ok})
            except Exception:                                  # noqa: BLE001
                log.exception("notify failed")

"""home-studio's API: the portal's Studio page and Alfred's skill call this.

Never reached from outside the house. The portal (home-core) proxies
`/studio/api/*` to it with the shared secret and the signed-in person's login
-- the same server-to-server vouching the skills use -- so a request here
always says who it is for, and projects key on that login.

    X-Studio-Secret   the shared secret (derived by the deployer)
    X-Studio-User     the login id the portal session carries
    X-Studio-Name     how the queue names that person to everyone else
    X-Studio-Admin    "1" for a parent: sees and manages the whole queue

What anyone sees of another person's job is whose it is, what kind, where it
stands and how long it will take -- never its prompt, its project or its
result.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import analysis, handwriting, media, recipes, story
from .manager import Manager
from .characters import Characters
from .history import LABELS, History
from .projects import (ProjectError, Projects, clean_callouts, clean_card, clean_eyes, clean_layout,
                       clean_mix, clean_sections, clean_write)
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("studio")

DATA = Path(os.environ.get("STUDIO_DATA", "/data"))
SECRET = os.environ.get("STUDIO_SECRET", "")
NOTIFY_URL = os.environ.get("STUDIO_NOTIFY_URL", "")
IDLE_S = float(os.environ.get("STUDIO_IDLE_S", "600"))
MAX_UPLOAD = 200 * 1024 * 1024

store = Store(DATA / "queue" / "jobs.db")
projects = Projects(DATA / "projects")
characters = Characters(DATA / "projects")
renders = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-render")
# Each project's words under version control (studio/history.py). A save's
# revision is written beside the request, one at a time, so typing is never
# held up by git.
history = History(projects, characters)
histories = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-history")
# Taking a snapshot and queueing it happen as one step: two saves at once
# (the page and the person's assistant) are then written in the order they
# were taken, and the newest revision is the project as it is.
_remember_order = threading.Lock()
# Transcripts wait on the house's speech recogniser -- up to half an hour for
# a long recording -- so they have a pool of their own: sharing the renders'
# one worker held every person's films and encodes behind one transcript.
speech = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-speech")
render_state: dict[str, dict] = {}


def _notify(job: dict) -> None:
    """Tell the person their job finished, through the portal (ntfy)."""
    try:
        _auto_step(job)
    except Exception:                                      # noqa: BLE001 -- never the queue's problem
        log.exception("explainer auto step failed")
    if not NOTIFY_URL:
        return
    what = recipes.estimate_note(job["kind"], job["params"])
    text = (f"Listo: {what}" + (f" — {job['title']}" if job["title"] else "")) if job.get("ok") else \
        f"No se pudo generar: {what}" + (f" ({job['error'][:120]})" if job.get("error") else "")
    try:
        requests.post(NOTIFY_URL, json={"login": job["owner"], "text": text, "project": job["project"],
                                        "ok": bool(job.get("ok"))},
                      headers={"X-Studio-Secret": SECRET}, timeout=10, verify=False)
    except requests.RequestException as exc:
        log.warning("notify failed: %s", exc)
    # A frame drawn inside a refine loop goes back to the portal to be looked
    # at: the reviewer is the person's assistant, which the Studio cannot reach.
    refine = (job.get("params") or {}).get("refine")
    if job.get("ok") and job.get("kind") == "board" and refine and NOTIFY_URL.endswith("/notify"):
        try:
            requests.post(NOTIFY_URL[:-len("notify")] + "frame-review",
                          json={"login": job["owner"], "project": job["project"], "shot": job["target"],
                                "job": job["id"], "refine": refine},
                          headers={"X-Studio-Secret": SECRET}, timeout=10, verify=False)
        except requests.RequestException as exc:
            log.warning("frame review hook failed: %s", exc)


manager = Manager(store, projects, DATA / "scratch", DATA / "logs", idle_s=IDLE_S, notify=_notify,
                  audio=analysis.AudioServer(os.environ.get("STUDIO_AUDIO_URL", "")), characters=characters)
app = FastAPI(title="home-studio", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _start() -> None:
    # Work that was running beside the queue when the studio stopped is gone
    # with the process; left "running", its button never came back.
    n = projects.interrupt_running()
    if n:
        log.info("marked %d interrupted transcript/trim/encode(s) as failed", n)
    manager.start()
    threading.Thread(target=_housekeeping, daemon=True).start()


def _housekeeping() -> None:
    try:
        n = history.begin_all()
        if n:
            log.info("began the history of %d project(s)", n)
    except Exception:                                          # noqa: BLE001
        log.exception("beginning project histories failed")
    try:
        n = projects.compress_audio_takes(media.compress_audio)
        if n:
            log.info("compressed %d audio take(s) to mp3", n)
    except Exception:                                          # noqa: BLE001
        log.exception("compressing old audio takes failed")
    while True:
        try:
            projects.purge_trash()
        except Exception:                                      # noqa: BLE001
            log.exception("trash purge failed")
        time.sleep(6 * 3600)


class Who:
    def __init__(self, login: str, name: str, admin: bool, via: str = ""):
        self.login, self.name, self.admin, self.via = login, name, admin, via


def who(x_studio_secret: str = Header(""), x_studio_user: str = Header(""),
        x_studio_name: str = Header(""), x_studio_admin: str = Header(""),
        x_studio_via: str = Header("")) -> Who:
    """The person asking. `X-Studio-Via` names their assistant when it is
    the one asking on their behalf -- the history says so."""
    if not SECRET or not hmac.compare_digest(x_studio_secret, SECRET):
        raise HTTPException(401, "not authorised")
    if not x_studio_user:
        raise HTTPException(401, "no user")
    return Who(x_studio_user, x_studio_name or x_studio_user, x_studio_admin == "1",
               " ".join(x_studio_via.split())[:40])


def _remember(me: Who, pid: str, message: str = "", kind: str = "edit") -> None:
    """A revision of the project's words: taken now, written in the
    background."""
    def work(captured):
        try:
            history.record(me.login, pid, me.login, me.name, me.via, kind=kind, message=message, captured=captured)
        except Exception:                                      # noqa: BLE001 -- history never fails a save
            log.exception("history of %s/%s", me.login, pid)
    with _remember_order:
        histories.submit(work, history.capture(me.login, pid))


def _history_settled(needed: bool) -> None:
    """Wait for the saves before this request to be in the history. For a
    list, a history a moment behind is still an answer; for undoing or going
    back, it is not -- they must start from the latest revision."""
    try:
        histories.submit(lambda: None).result(timeout=30)
    except TimeoutError:
        if needed:
            raise HTTPException(503, "the history is still writing the latest saves; try again in a moment")


def _bad(exc: Exception, code: int = 400):
    raise HTTPException(code, str(exc))


# -- health and the queue -------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True, **manager.status(), "queued": len(store.active())}


def _public(job: dict, me: Who) -> dict:
    mine = job["owner"] == me.login
    out = {"id": job["id"], "kind": job["kind"], "state": job["state"], "owner_name": job["owner_name"],
           "mine": mine, "what": recipes.estimate_note(job["kind"], job["params"]),
           "progress": round(job["progress"], 3), "phase": job["phase"],
           "position": job.get("position"), "starts_in": job.get("starts_in"), "takes": job.get("takes"),
           "created": job["created"], "started": job["started"], "finished": job["finished"]}
    if mine or me.admin:
        out.update(title=job["title"], project=job["project"] if mine else "", target=job["target"] if mine else "",
                   error=job["error"])
    if mine:
        out["files"] = job["files"]
        out["preview"] = job["id"] in manager.previews
    return out


@app.get("/api/queue")
def queue(me: Who = Depends(who)):
    sched = store.schedule(manager.worker.model if manager.worker else "")
    running = store.running()
    return {"running": _public(running, me) if running else None,
            "queued": [_public(j, me) for j in sched],
            "status": manager.status(), "is_admin": me.admin,
            # Card seconds per second of video, for the page to say how long
            # a music video will take before it is queued.
            "video_rate": round(store.rate("video_shot"))}


@app.get("/api/jobs")
def my_jobs(me: Who = Depends(who)):
    return {"recent": [_public(j, me) for j in store.recent(owner=me.login, limit=40)]}


def _enqueue(me: Who, kind: str, params: dict, project: str = "", target: str = "",
             title: str = "", after: str = "") -> dict:
    if kind not in recipes.KINDS:
        _bad(ValueError(f"unknown kind {kind}"))
    if project:
        projects.load(me.login, project)                      # it exists and it is theirs
    job = store.add(owner=me.login, owner_name=me.name, kind=kind, model=recipes.model_for(kind, params),
                    params=params, title=title, project=project, target=target, after=after)
    manager.wake()
    return job


# What the generic job door takes. The other kinds -- a storyboard frame, a
# character's portrait, a song's analysis or repaint -- have routes of their
# own that check who may ask; through here they skipped those checks.
OPEN_KINDS = ("image", "song", "instrumental", "voice", "video_shot")
# Params the manager fills in from what a project holds, as paths on disk or
# references into somebody's files. Sent from outside they would name any
# file the studio can read -- another person's -- so they never come in here.
RESOLVED_PARAMS = ("start_image", "end_image", "voice_file", "source_video", "source_file",
                   "start_board", "voice_char", "from_take", "image_refs", "ref_chars", "ref_files", "ref_shot",
                   "with_refs")


def _drawing_refs(doc: dict, cast: list[str], me: Who, pid: str, own: list[str] | None = None) -> dict:
    """What a picture is drawn from besides its words: the cast's pictures (a
    character with none is drawn from its look alone), the shot's own
    reference pictures (*own*: its `refs`) and the project's style pictures.
    Empty when there are none, and the picture is Z-Image's from text, as
    before. The files are found when the job runs (manager._refs)."""
    chars = []
    for cid in cast or []:
        try:
            if characters.get(str(cid), me.login, pid).get("pictures"):
                chars.append(str(cid))
        except ProjectError:
            continue
    files = Projects.style_refs(doc)[:recipes.MAX_STYLE_REFS]
    chars = chars[:recipes.MAX_CAST_REFS]
    shot = [str(r) for r in (own or []) if str(r).startswith("uploads/")][:recipes.MAX_SHOT_REFS]
    return ({"with_refs": True, "ref_chars": chars, "ref_files": files, "ref_shot": shot}
            if chars or files or shot else {})


@app.post("/api/jobs")
def add_job(body: dict, me: Who = Depends(who)):
    kind = str(body.get("kind"))
    if kind not in OPEN_KINDS:
        _bad(ValueError(f"{kind} jobs are asked for through their own route"))
    params = {k: v for k, v in dict(body.get("params") or {}).items() if k not in RESOLVED_PARAMS}
    try:
        job = _enqueue(me, kind, params,
                       str(body.get("project") or ""), str(body.get("target") or ""),
                       str(body.get("title") or ""))
    except ProjectError as exc:
        _bad(exc, 404)
    sched = {j["id"]: j for j in store.schedule()}
    return _public(sched.get(job["id"], job), me)


@app.get("/api/jobs/{job_id}/preview")
def job_preview(job_id: str, me: Who = Depends(who)):
    """What the running job looks like so far. Its owner's only: the queue
    tells everybody what is running, never what it shows."""
    job = store.get(job_id)
    path = manager.previews.get(job_id)
    if not job or job["owner"] != me.login or not path or not path.is_file():
        raise HTTPException(404, "no preview")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str, me: Who = Depends(who)):
    job = store.get(job_id)
    if not job or (job["owner"] != me.login and not me.admin):
        raise HTTPException(404, "no such job")
    was = manager.cancel(job_id)
    return {"ok": True, "was": was}


@app.post("/api/admin/{action}")
def admin(action: str, body: dict | None = None, me: Who = Depends(who)):
    if not me.admin:
        raise HTTPException(403, "only a parent")
    if action == "pause":
        # Why, for the page to say: a Studio update, the card lent to the
        # Programmer's local model (deploy/host/programmer-local.sh) or to the
        # coding benchmark (deploy/codebench), or -- with no reason -- a
        # parent's decision. `now` stops the job on the card as well; it goes
        # back to the queue.
        reason = (body or {}).get("reason")
        stopped = manager.pause(reason if reason in ("update", "programmer", "bench") else "",
                                now=bool((body or {}).get("now")))
        return {"ok": True, "stopped": stopped, **manager.status()}
    elif action == "resume":
        manager.paused = False
        manager.wake()
    elif action == "priority":
        store.update(str((body or {}).get("id")), priority=int((body or {}).get("priority") or 0))
        manager.wake()
    else:
        raise HTTPException(404, "unknown action")
    return {"ok": True, **manager.status()}


# -- projects -------------------------------------------------------------------
@app.get("/api/projects")
def list_projects(me: Who = Depends(who)):
    listed = projects.list(me.login)
    count = Counter(c for p in listed for c in p["collections"])
    return {"projects": listed,
            "collections": [{**c, "count": count[c["id"]]} for c in projects.collections(me.login)["collections"]]}


@app.post("/api/projects")
def new_project(body: dict, me: Who = Depends(who)):
    try:
        doc = projects.create(me.login, str(body.get("name") or ""), str(body.get("kind") or "free"),
                              str(body.get("collection") or ""))
    except ProjectError as exc:
        _bad(exc, 404)
    _remember(me, doc["id"])
    return doc


# -- collections: a person's projects gathered under a name ----------------------
def _collections_call(fn):
    try:
        return fn()
    except ProjectError as exc:
        _bad(exc, 404 if "no such" in str(exc) else 400)


@app.post("/api/collections")
def new_collection(body: dict, me: Who = Depends(who)):
    return _collections_call(lambda: projects.create_collection(me.login, str(body.get("name") or "")))


@app.put("/api/collections/{cid}")
def rename_collection(cid: str, body: dict, me: Who = Depends(who)):
    return _collections_call(lambda: projects.rename_collection(me.login, cid, str(body.get("name") or "")))


@app.delete("/api/collections/{cid}")
def delete_collection(cid: str, me: Who = Depends(who)):
    _collections_call(lambda: projects.delete_collection(me.login, cid))
    return {"ok": True}


@app.post("/api/collections/{cid}/projects")
def add_to_collection(cid: str, body: dict, me: Who = Depends(who)):
    _collections_call(lambda: projects.add_to_collection(me.login, cid, str(body.get("project") or "")))
    return {"ok": True}


@app.delete("/api/collections/{cid}/projects/{pid}")
def remove_from_collection(cid: str, pid: str, me: Who = Depends(who)):
    _collections_call(lambda: projects.remove_from_collection(me.login, cid, pid))
    return {"ok": True}


# -- characters ----------------------------------------------------------------
def _project(me: Who, pid: str) -> dict:
    try:
        return projects.load(me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)


def _char_public(doc: dict, me: Who) -> dict:
    return {**doc, "editable": Characters.may_edit(doc, me.login, me.admin)}


@app.get("/api/projects/{pid}/characters")
def list_characters(pid: str, me: Who = Depends(who)):
    """Everyone who can be cast here: the project's, the person's, the family's."""
    _project(me, pid)
    return {"characters": [_char_public(c, me) for c in characters.list(me.login, pid)]}


@app.post("/api/projects/{pid}/characters")
def new_character(pid: str, body: dict, me: Who = Depends(who)):
    _project(me, pid)
    try:
        made = _char_public(characters.create(me.login, pid, body), me)
        _remember(me, pid)
        return made
    except ProjectError as exc:
        _bad(exc)


@app.put("/api/projects/{pid}/characters/{cid}")
def edit_character(pid: str, cid: str, body: dict, me: Who = Depends(who)):
    _project(me, pid)
    try:
        changed = _char_public(characters.update(cid, me.login, pid, body, me.admin), me)
        _remember(me, pid)
        return changed
    except ProjectError as exc:
        _bad(exc, 403)


@app.post("/api/projects/{pid}/characters/{cid}/widen")
def widen_character(pid: str, cid: str, me: Who = Depends(who)):
    """One scope wider: this project -> all of mine -> the family's."""
    _project(me, pid)
    try:
        wider = _char_public(characters.widen(cid, me.login, pid, me.admin), me)
        _remember(me, pid)
        return wider
    except ProjectError as exc:
        _bad(exc, 403)


@app.delete("/api/projects/{pid}/characters/{cid}")
def delete_character(pid: str, cid: str, me: Who = Depends(who)):
    _project(me, pid)
    try:
        characters.delete(cid, me.login, pid, me.admin)
        _remember(me, pid)
    except ProjectError as exc:
        _bad(exc, 403)
    return {"ok": True}


@app.post("/api/projects/{pid}/characters/{cid}/upload")
def character_upload(pid: str, cid: str, file: UploadFile = File(...), kind: str = Form("picture"),
                     me: Who = Depends(who)):
    # A plain def: a voice sample is converted with ffmpeg, which must not
    # hold up every other request on the event loop while it runs.
    _project(me, pid)
    data = file.file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "too large")
    try:
        return _char_public(characters.add_file(cid, me.login, pid, file.filename or "file", data,
                                                "voice" if kind == "voice" else "picture", me.admin), me)
    except ProjectError as exc:
        _bad(exc, 403)


@app.get("/api/projects/{pid}/characters/{cid}/file/{rel:path}")
def character_file(pid: str, cid: str, rel: str, me: Who = Depends(who)):
    _project(me, pid)
    try:
        path = characters.file(cid, me.login, pid, rel)
    except ProjectError as exc:
        _bad(exc, 404)
    # A character is seen by the whole family once shared, so its files are
    # served as what they are allowed to be -- a picture or a sound -- never
    # as whatever the name suggests (an .html or .svg would run as the viewer).
    kinds = {".png": "image/png", ".jpg": "image/jpeg", ".wav": "audio/wav", ".mp3": "audio/mpeg"}
    media_type = kinds.get(path.suffix.lower())
    if not media_type:
        _bad(ProjectError("no such file"), 404)
    return FileResponse(path, media_type=media_type)


@app.post("/api/projects/{pid}/characters/{cid}/portrait")
def character_portrait(pid: str, cid: str, me: Who = Depends(who)):
    """A picture of the character drawn from its look, filed on it."""
    doc = _project(me, pid)
    try:
        ch = characters.get(cid, me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    if not Characters.may_edit(ch, me.login, me.admin):
        _bad(ProjectError("only whoever made this character can change its pictures"), 403)
    if not ch.get("look") and not ch.get("pictures"):
        _bad(ValueError("describe how the character looks first, or give it a picture"))
    look = str(doc["settings"].get("look") or "").strip()
    prompt = (f"{look}. " if look else "") + f"Character portrait of {ch['name']}: {ch['look']}. " \
             "Full figure, facing the camera, plain background, clear light."
    # From its own picture when it has one -- a photo of the person, drawn
    # again in the film's look -- and the film's style pictures.
    job = _enqueue(me, "portrait", {"prompt": prompt[:1500], "size": "832x1216", "admin": me.admin,
                                    **_drawing_refs(doc, [cid], me, pid)},
                   pid, cid, title=ch["name"])
    return {"queued": [_public(store.get(job["id"]) or job, me)]}


@app.post("/api/projects/{pid}/characters/{cid}/speak")
def character_speak(pid: str, cid: str, body: dict | None = None, me: Who = Depends(who)):
    """A line in the character's cloned voice, to hear it before casting it."""
    _project(me, pid)
    try:
        ch = characters.get(cid, me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    if not ch.get("voice"):
        _bad(ValueError("give the character a voice sample first"))
    text = str((body or {}).get("text") or "").strip()[:600]
    if not text:
        _bad(ValueError("nothing to say"))
    job = _enqueue(me, "voice", {"text": text, "voice_char": cid, "voice_text": ch.get("voice_text") or "",
                                 "seconds": 30, "language": "es"}, pid, cid, title=ch["name"])
    return {"queued": [_public(store.get(job["id"]) or job, me)]}


# -- recordings (the Recording kind) --------------------------------------------
RECORD_CHUNK_MAX = 32 * 1024 * 1024


def _rec_dir(me: Who, pid: str, rid: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]{6,32}", rid or ""):
        _bad(ProjectError("no such recording"), 404)
    return projects.dir(me.login, pid) / "recordings" / rid


@app.post("/api/projects/{pid}/recordings")
def start_recording(pid: str, body: dict | None = None, me: Who = Depends(who)):
    """A new clip being recorded in the page. Its pieces arrive while it is
    recorded, so closing the tab or losing the network loses seconds, not the
    recording."""
    _project(me, pid)
    item = projects.add_recording(me.login, pid, str((body or {}).get("title") or ""))
    _rec_dir(me, pid, item["id"]).mkdir(parents=True, exist_ok=True)
    return item


@app.post("/api/projects/{pid}/recordings/{rid}/chunk")
async def recording_chunk(pid: str, rid: str, n: int, request: Request, track: str = "main",
                          me: Who = Depends(who)):
    """One piece of the recording, numbered in the order it was made. `track`
    is `main` (the screen, or the camera alone, with the microphone), `cam`
    (the camera recorded beside the screen as a recording of its own, never
    drawn into it, so whether it shows in a corner is decided afterwards) or
    `pc` (the computer's sound, apart from the microphone, so each can be
    turned up or down afterwards)."""
    if track not in ("main", "cam", "pc"):
        _bad(ValueError("bad track"))
    d = _rec_dir(me, pid, rid)
    if not d.is_dir():
        _bad(ProjectError("no such recording"), 404)
    data = await request.body()
    if len(data) > RECORD_CHUNK_MAX:
        raise HTTPException(413, "too large")
    if not 0 <= n < 100000:
        _bad(ValueError("bad piece number"))
    (d / f"{ {'main': 'part', 'cam': 'cam', 'pc': 'pc'}[track]}-{n:06d}.webm").write_bytes(data)
    return {"ok": True, "n": n, "track": track}


@app.post("/api/projects/{pid}/recordings/{rid}/finish")
def finish_recording(pid: str, rid: str, body: dict | None = None, me: Who = Depends(who)):
    """The pieces joined in order -- a browser recording's pieces are one
    stream cut up, so joined they are the file -- and encoded into a kept
    clip on the CPU, beside the card's queue. The clip becomes its take.

    A camera recorded beside the screen (`cam-*` pieces) is encoded as a clip
    of its own, lined up with the screen's by `cam_offset_ms` (how much later
    it started), and kept on the take as `cam`. `cam_layout` is where the page
    showed it while recording -- off, or a corner and a size -- and becomes the
    clip's, changeable any time after. The computer's sound (`pc-*` pieces) is
    kept the same way as `pc`, lined up by `pc_offset_ms`, with the clip's
    volumes in `mix`."""
    body = body or {}
    d = _rec_dir(me, pid, rid)
    parts = sorted(d.glob("part-*.webm"))
    cam_parts = sorted(d.glob("cam-*.webm"))
    pc_parts = sorted(d.glob("pc-*.webm"))

    def offset(key: str) -> float:
        try:
            return max(-10.0, min(10.0, float(body.get(key) or 0) / 1000))
        except (TypeError, ValueError):
            return 0.0
    cam_offset, pc_offset = offset("cam_offset_ms"), offset("pc_offset_ms")
    if not parts:
        _bad(ValueError("nothing was recorded"))
    base = projects.dir(me.login, pid)
    projects.set_item_field(me.login, pid, rid, "recording", {"state": "processing", "parts": len(parts)})
    if cam_parts and isinstance(body.get("cam_layout"), dict):
        projects.set_item_field(me.login, pid, rid, "cam_layout", clean_layout(body["cam_layout"]))
    if pc_parts:
        projects.set_item_field(me.login, pid, rid, "mix", clean_mix(body.get("mix")))

    def work():
        raw = d / "recording.webm"
        try:
            with open(raw, "wb") as out:
                for part in parts:
                    out.write(part.read_bytes())
            take_dir = base / "takes" / rid
            clip = media.encode_recording(raw, take_dir / f"{rid}-rec.mp4")
            seconds = round(media.probe(clip)["seconds"], 2)
            first = media.frame(clip, take_dir / f"{rid}-first.png", "first")
            last = media.frame(clip, take_dir / f"{rid}-last.png", "last")
            take = {"file": str(clip.relative_to(base)), "kind": "recording",
                    "seconds": seconds, "first": str(first.relative_to(base)),
                    "last": str(last.relative_to(base))}
            if cam_parts:
                # The camera, apart. Its failing costs the camera, not the
                # recording: the screen and its sound are the take either way.
                raw_cam = d / "camera.webm"
                try:
                    with open(raw_cam, "wb") as out:
                        for part in cam_parts:
                            out.write(part.read_bytes())
                    cam = media.encode_camera(raw_cam, take_dir / f"{rid}-cam.mp4", cam_offset, seconds)
                    take["cam"] = str(cam.relative_to(base))
                except Exception as exc:                       # noqa: BLE001
                    log.warning("recording %s: the camera track failed: %s", rid, exc)
                    take["cam_error"] = str(exc)[:200]
            if pc_parts:
                raw_pc = d / "computer.webm"
                try:
                    with open(raw_pc, "wb") as out:
                        for part in pc_parts:
                            out.write(part.read_bytes())
                    pc = media.encode_sound(raw_pc, take_dir / f"{rid}-pc.m4a", pc_offset, seconds)
                    take["pc"] = str(pc.relative_to(base))
                except Exception as exc:                       # noqa: BLE001
                    log.warning("recording %s: the computer sound failed: %s", rid, exc)
                    take["pc_error"] = str(exc)[:200]
            projects.add_take(me.login, pid, rid, take)
            projects.set_item_field(me.login, pid, rid, "seconds", seconds)
            projects.set_item_field(me.login, pid, rid, "recording", {"state": "done"})
            shutil.rmtree(d, ignore_errors=True)
        except Exception as exc:                               # noqa: BLE001
            log.warning("recording %s failed: %s", rid, exc)
            # The pieces are kept: a recording that failed to encode is not lost.
            projects.set_item_field(me.login, pid, rid, "recording", {"state": "failed", "error": str(exc)[:300]})

    renders.submit(work)
    return {"ok": True, "state": "processing"}


WHISPER_URL = os.environ.get("WHISPER_URL", "")
TRANSCRIBE_PIECE_S = 60.0


@app.post("/api/projects/{pid}/items/{item_id}/trim")
def trim_silences(pid: str, item_id: str, body: dict | None = None, me: Who = Depends(who)):
    """A new version of a clip with its long silences taken out -- the pauses
    in a recording where nothing is said. The original version is kept. On
    the CPU pool beside the queue; the item says where it is."""
    body = body or {}
    _doc, item, take = _item_take(me, pid, item_id, str(body.get("take") or ""))
    if not item.get("recorded"):
        _bad(ValueError("silences are taken out of recordings"))
    base = projects.dir(me.login, pid)
    try:
        min_s = max(0.6, min(10.0, float(body.get("min_seconds") or 1.2)))
    except (TypeError, ValueError) as exc:
        _bad(exc)
    projects.set_item_field(me.login, pid, item_id, "trim", {"state": "running"})

    def work():
        try:
            stamp = uuid.uuid4().hex[:8]
            spans, total = media.speaking_spans(base / take["file"], min_s=min_s)
            clip = media.cut_spans(base / take["file"], base / "takes" / item_id / f"{stamp}-trim.mp4", spans)
            removed = round(total - sum(b - a for a, b in spans), 2)
            # The camera, cut at the same places, so it stays with the screen.
            cam = None
            if take.get("cam") and (base / take["cam"]).is_file():
                cam = media.cut_spans(base / take["cam"], base / "takes" / item_id / f"{stamp}-trim-cam.mp4",
                                      spans, sound=False)
            # The computer sound too. Silences are found in the microphone
            # alone -- the screen's track -- so music under a pause is not
            # taken for speech.
            pc = None
            if take.get("pc") and (base / take["pc"]).is_file():
                pc = media.cut_spans(base / take["pc"], base / "takes" / item_id / f"{stamp}-trim-pc.m4a",
                                     spans, picture=False)
            first = media.frame(clip, clip.with_name(f"{stamp}-trim-first.png"), "first")
            last = media.frame(clip, clip.with_name(f"{stamp}-trim-last.png"), "last")
            seconds = round(media.probe(clip)["seconds"], 2)
            projects.add_take(me.login, pid, item_id, {"file": str(clip.relative_to(base)), "kind": "trimmed",
                                                       "from": take["id"], "seconds": seconds,
                                                       "first": str(first.relative_to(base)),
                                                       "last": str(last.relative_to(base)),
                                                       **({"cam": str(cam.relative_to(base))} if cam else {}),
                                                       **({"pc": str(pc.relative_to(base))} if pc else {})})
            projects.set_item_field(me.login, pid, item_id, "seconds", seconds)
            projects.set_item_field(me.login, pid, item_id, "trim", {"state": "done", "removed": removed})
        except Exception as exc:                               # noqa: BLE001
            log.warning("trim %s failed: %s", item_id, exc)
            projects.set_item_field(me.login, pid, item_id, "trim", {"state": "failed", "error": str(exc)[:300]})

    renders.submit(work)
    return {"ok": True, "state": "running"}


def _share_file(base: Path, rel: str, dest: Path) -> str:
    """Another name for a file a new version keeps as it is (the camera, the
    computer sound, a frame): a hard link, so it costs no space and deleting
    either version leaves the other's file in place. A copy where a link is
    not possible."""
    src = base / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)
    return str(dest.relative_to(base))


@app.post("/api/projects/{pid}/items/{item_id}/clean")
def clean_voice(pid: str, item_id: str, body: dict | None = None, me: Who = Depends(who)):
    """A new version of a recording with its microphone cleaned -- background
    noise taken out, loudness evened out -- and everything else as it was: the
    picture copied, the camera and the computer sound kept. Its timing is the
    original's, so sections drawn over that version move to this one. On the
    CPU pool beside the queue; the item says where it is."""
    body = body or {}
    _doc, item, take = _item_take(me, pid, item_id, str(body.get("take") or ""))
    if not item.get("recorded"):
        _bad(ValueError("the microphone is cleaned on recordings"))
    denoise, level = body.get("denoise", True) is not False, body.get("level", True) is not False
    if not (denoise or level):
        _bad(ValueError("nothing to clean"))
    base = projects.dir(me.login, pid)
    projects.set_item_field(me.login, pid, item_id, "clean", {"state": "running"})

    def work():
        try:
            stamp = uuid.uuid4().hex[:8]
            folder = base / "takes" / item_id
            clip = media.clean_voice(base / take["file"], folder / f"{stamp}-clean.mp4", denoise=denoise, level=level)
            new = {"file": str(clip.relative_to(base)), "kind": "cleaned", "from": take["id"],
                   "seconds": take.get("seconds") or round(media.probe(clip)["seconds"], 2),
                   "cleaned": {"denoise": denoise, "level": level}}
            for key, suffix in (("first", "-clean-first.png"), ("last", "-clean-last.png"),
                                ("cam", "-clean-cam.mp4"), ("pc", "-clean-pc.m4a")):
                if take.get(key) and (base / take[key]).is_file():
                    new[key] = _share_file(base, take[key], folder / f"{stamp}{suffix}")
            made = projects.add_take(me.login, pid, item_id, new)
            current = Projects.find(projects.load(me.login, pid), item_id)
            for field in ("sections_take", "callouts_take", "eyes_take"):
                if current and current[2].get(field) == take["id"]:
                    projects.set_item_field(me.login, pid, item_id, field, made["id"])
            projects.set_item_field(me.login, pid, item_id, "clean", {"state": "done", **new["cleaned"]})
        except Exception as exc:                               # noqa: BLE001
            log.warning("clean %s failed: %s", item_id, exc)
            projects.set_item_field(me.login, pid, item_id, "clean", {"state": "failed", "error": str(exc)[:300]})

    renders.submit(work)
    return {"ok": True, "state": "running"}


@app.post("/api/projects/{pid}/items/{item_id}/eyes")
def eye_contact(pid: str, item_id: str, body: dict | None = None, me: Who = Depends(who)):
    """Queue eye contact on a recording's camera: the eyes moved to the lens
    over the stretches marked on that version (the item's `eyes`, or the
    whole clip when `whole`), as a new version. It is the card's work and
    waits its turn with the rest (manager._eyes)."""
    body = body or {}
    _doc, item, take = _item_take(me, pid, item_id, str(body.get("take") or ""))
    if not item.get("recorded") or not take.get("cam"):
        _bad(ValueError("eye contact is for a recording with its camera"))
    for job in store.active():
        if job["kind"] == "eyes" and job["target"] == item_id:
            return {"job": _public(job, me)}
    length = float(take.get("seconds") or 0) or media.probe(projects.dir(me.login, pid) / take["cam"])["seconds"]
    if body.get("whole"):
        spans = []
    else:
        marked = item.get("eyes") if item.get("eyes_take") == take["id"] else []
        spans = [[s["start"], min(s["end"], length)] for s in clean_eyes(marked) if s["start"] < length]
        if not spans:
            _bad(ValueError("mark the stretches to correct first, or ask for the whole clip"))
    try:
        strength = max(0.3, min(1.0, float(body.get("strength") or 1.0)))
    except (TypeError, ValueError):
        strength = 1.0
    seconds = sum(b - a for a, b in spans) if spans else length
    job = _enqueue(me, "eyes", {"take": take["id"], "spans": spans, "strength": strength,
                                "seconds": round(max(1.0, seconds), 2)}, pid, item_id,
                   title=item.get("title") or "")
    projects.set_item_field(me.login, pid, item_id, "eyes_fix", {"state": "queued"})
    return {"job": _public(store.get(job["id"]) or job, me)}


@app.post("/api/projects/{pid}/items/{item_id}/transcribe")
def transcribe(pid: str, item_id: str, body: dict | None = None, me: Who = Depends(who)):
    """What is said in a clip, with its times: the house's speech recogniser
    on the clip's sound (on its own card, not this one), kept on the version
    as JSON and as SubRip subtitles, for the film to burn in or to download.
    On the CPU pool beside the queue; the version says where it is."""
    if not WHISPER_URL:
        _bad(ValueError("the house has no speech recogniser configured"))
    doc, item, take = _item_take(me, pid, item_id, str((body or {}).get("take") or ""))
    if not item.get("recorded"):
        _bad(ValueError("subtitles are made for recordings"))
    base = projects.dir(me.login, pid)
    language = str((body or {}).get("language") or doc["settings"].get("language") or "es")[:5]
    # The earlier transcript's file names are kept while this one runs (it
    # writes the same names): a record that forgot them left the files behind
    # when the version was deleted.
    projects.set_take_field(me.login, pid, item_id, take["id"], "transcript",
                            {**(take.get("transcript") or {}), "state": "running"})

    def work():
        stem = base / "takes" / item_id / f"{take['id']}-transcript"
        wav = stem.with_suffix(".wav")
        piece = stem.with_suffix(".piece.wav")
        try:
            media.to_wav(base / take["file"], wav, rate=16000, channels=1)
            total = media.probe(wav)["seconds"]
            # A minute at a time: the recogniser is the house's, and a whole
            # recording in one request held it -- and every room's voice
            # request behind it -- for as long as the recording lasted.
            segments, at = [], 0.0
            while at < total - 0.05:
                media.cut_audio(wav, piece, at, TRANSCRIBE_PIECE_S)
                with open(piece, "rb") as fh:
                    r = requests.post(WHISPER_URL, files={"file": ("speech.wav", fh, "audio/wav")},
                                      data={"language": language}, timeout=600)
                r.raise_for_status()
                segments += [{"start": round(at + float(s["start"]), 2), "end": round(at + float(s["end"]), 2),
                              "text": str(s.get("text") or "").strip()} for s in r.json().get("segments") or []]
                at += TRANSCRIBE_PIECE_S
            stem.with_suffix(".json").write_text(json.dumps(segments, ensure_ascii=False))
            stem.with_suffix(".srt").write_text(media.srt(segments), encoding="utf-8")
            projects.set_take_field(me.login, pid, item_id, take["id"], "transcript", {
                "state": "done", "file": str(stem.with_suffix(".json").relative_to(base)),
                "srt": str(stem.with_suffix(".srt").relative_to(base)),
                "text": " ".join(s["text"] for s in segments)[:4000]})
        except Exception as exc:                               # noqa: BLE001
            log.warning("transcript %s failed: %s", item_id, exc)
            projects.set_take_field(me.login, pid, item_id, take["id"], "transcript",
                                    {**{k: v for k, v in (take.get("transcript") or {}).items() if k in ("file", "srt")},
                                     "state": "failed", "error": str(exc)[:300]})
        finally:
            wav.unlink(missing_ok=True)
            piece.unlink(missing_ok=True)

    speech.submit(work)
    return {"ok": True, "state": "running"}


@app.post("/api/projects/{pid}/storyboard")
def storyboard(pid: str, body: dict | None = None, me: Who = Depends(who)):
    """A still for each shot, before any video: a minute a frame where a shot
    is half an hour, so the whole film can be looked at -- and redrawn frame
    by frame -- before the card is spent on it. `items` names the shots (a
    redraw); none, every shot that has no frame yet. Each is drawn with the
    project's look ahead of its description, so the frames read as one film."""
    body = body or {}
    try:
        doc = projects.load(me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    want = [str(i) for i in body.get("items") or []]
    # `prompts`: what to draw instead of a shot's description -- a reviewer's
    # improved prompt -- which leaves the description the person wrote as it
    # is. `refine`: rounds of review left after this drawing (the portal
    # reviews each frame drawn with some and redraws it while it scores low).
    overrides = {str(k): str(v).strip()[:1200] for k, v in (body.get("prompts") or {}).items() if str(v).strip()}
    # Every frame is looked at when it lands -- a first drawing, a redraw,
    # the planner's, the assistant's -- so a redraw is never a guess nobody
    # checked: a budget of no further rounds unless one is asked for, and
    # none at all only when asked (`review: false`).
    refine = None if body.get("review") is False else {"rounds": 0, "threshold": 7, "round": 0}
    if isinstance(body.get("refine"), dict):
        try:
            refine = {"rounds": max(0, min(3, int(body["refine"].get("rounds", 0)))),
                      "threshold": max(1, min(10, int(body["refine"].get("threshold", 7)))),
                      "round": max(0, min(9, int(body["refine"].get("round", 0))))}
        except (TypeError, ValueError):
            pass
    look = str(doc["settings"].get("look") or "").strip()
    size = recipes.BOARD_SIZE.get(doc["settings"].get("resolution", "832x480"), "1344x768")
    busy = {j["target"] for j in store.active() if j["owner"] == me.login and j["kind"] == "board"}
    queued = []
    for idx, shot in enumerate(doc.get("shots") or []):
        if want and shot["id"] not in want:
            continue
        if not want and (shot.get("boards") or shot["id"] in busy):
            continue
        what = str(shot.get("prompt") or "").strip()
        if not what:
            continue
        drawn = overrides.get(shot["id"], what)
        cast = characters.describe(shot.get("cast") or [], me.login, pid)
        prompt = (f"{look}. " if look else "") + f"Film still: {drawn}" + (f" Characters: {cast}." if cast else "")
        # `shot_prompt`: the description as it was when the frame was asked
        # for, kept on the frame so the page can tell a frame whose shot has
        # been described differently since -- one to draw again.
        params = {"prompt": prompt[:1500], "size": size, "shot_prompt": what[:1200],
                  **_drawing_refs(doc, shot.get("cast") or [], me, pid, shot.get("refs") or [])}
        if drawn != what:
            params["drawn_from"] = drawn
        if refine:
            params["refine"] = refine
        queued.append(_enqueue(me, "board", params,
                               pid, shot["id"],
                               shot.get("title") or f"{doc['name']} {idx + 1}")["id"])
    if not queued:
        _bad(ValueError("every shot already has a frame"))
    sched = {j["id"]: j for j in store.schedule()}
    return {"queued": [_public(sched.get(i) or store.get(i), me) for i in queued]}


@app.get("/api/projects/{pid}")
def get_project(pid: str, me: Who = Depends(who)):
    # The render's state before the project, never after: a film is filed in
    # the project (add_render) and only then marked done, so "done" read
    # first means the film is in what is loaded next. Read the other way
    # round, a render finishing between the two answered "done" with the
    # film list from before it -- the page stops polling on "done" and kept
    # that list, and a test read the previous film (2026-10-06).
    render = render_state.get(f"{me.login}/{pid}")
    try:
        doc = projects.load(me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    active = [j for j in store.active() if j["owner"] == me.login and j["project"] == pid]
    # The collections it is filed in, by name, for the page's header; not
    # part of the project, so never saved back into it.
    cols = projects.collections(me.login)
    names = {c["id"]: c["name"] for c in cols["collections"]}
    return {**doc, "jobs": [_public(j, me) for j in active], "render": render,
            "in_collections": [{"id": c, "name": names[c]} for c in cols["of"].get(pid, [])]}


@app.put("/api/projects/{pid}")
def save_project(pid: str, body: dict, me: Who = Depends(who)):
    try:
        saved = projects.save(me.login, pid, body)
        _remember(me, pid)
        return saved
    except ProjectError as exc:
        _bad(exc, 404)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str, me: Who = Depends(who)):
    for j in store.active():
        if j["owner"] == me.login and j["project"] == pid:
            manager.cancel(j["id"])
    try:
        projects.delete(me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    return {"ok": True}


# -- a project's history -----------------------------------------------------------
def _history_call(fn):
    try:
        return fn()
    except ProjectError as exc:
        _bad(exc, 404 if "no such" in str(exc) else 400)


@app.get("/api/projects/{pid}/history")
def project_history(pid: str, me: Who = Depends(who)):
    """Every revision of the project's words, newest first, and its tags."""
    _project(me, pid)
    _history_settled(needed=False)
    return _history_call(lambda: history.log(me.login, pid))


@app.get("/api/projects/{pid}/history/{rev}")
def project_revision(pid: str, rev: str, me: Who = Depends(who)):
    _project(me, pid)
    return _history_call(lambda: history.show(me.login, pid, rev))


@app.post("/api/projects/{pid}/history/{rev}/revert")
def revert_revision(pid: str, rev: str, me: Who = Depends(who)):
    """Undo what one revision changed, where nothing changed it since."""
    _project(me, pid)
    _history_settled(needed=True)
    return _history_call(lambda: history.revert(me.login, pid, rev, me.login, me.name, me.via))


@app.post("/api/projects/{pid}/history/{rev}/restore")
def restore_revision(pid: str, rev: str, me: Who = Depends(who)):
    """The project as it read at that revision."""
    _project(me, pid)
    _history_settled(needed=True)
    return _history_call(lambda: history.restore(me.login, pid, rev, me.login, me.name, me.via))


@app.post("/api/projects/{pid}/history/{rev}/tag")
def tag_revision(pid: str, rev: str, body: dict, me: Who = Depends(who)):
    _project(me, pid)
    return _history_call(lambda: history.tag(me.login, pid, rev, str(body.get("name") or ""), me.login, me.name))


@app.delete("/api/projects/{pid}/tags/{ref}")
def untag(pid: str, ref: str, me: Who = Depends(who)):
    _project(me, pid)
    _history_call(lambda: history.untag(me.login, pid, ref))
    return {"ok": True}


@app.post("/api/projects/{pid}/duplicate")
def duplicate_project(pid: str, body: dict | None = None, me: Who = Depends(who)):
    try:
        copy = projects.duplicate(me.login, pid, str((body or {}).get("name") or ""))
        lang = "en" if str(copy["settings"].get("language") or "es").startswith("en") else "es"
        _remember(me, copy["id"], message=LABELS[lang]["copy"].format(s=projects.load(me.login, pid)["name"]))
        return copy
    except ProjectError as exc:
        _bad(exc, 404)


@app.post("/api/projects/{pid}/upload")
async def upload(pid: str, file: UploadFile = File(...), kind: str = Form("reference"),
                 style: str = Form(""), me: Who = Depends(who)):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "too large")
    try:
        entry = projects.add_upload(me.login, pid, file.filename or "file", data,
                                    kind if kind in ("reference", "voice", "audio") else "reference")
        if style and entry["kind"] == "reference":
            # A style picture brought in: flagged as it lands. Over the limit
            # it is still kept, as a plain reference, and the answer says so.
            try:
                entry = projects.set_upload_style(me.login, pid, entry["file"], True, recipes.MAX_STYLE_REFS)
            except ProjectError as exc:
                entry = {**entry, "style_error": str(exc)}
        return entry
    except ProjectError as exc:
        _bad(exc, 404)


def _item_take(me: Who, pid: str, item_id: str, take_id: str = "") -> tuple[dict, dict, dict]:
    """The project, one item of it, and one of its versions (the one asked
    for, else the chosen one)."""
    try:
        doc = projects.load(me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    found = Projects.find(doc, item_id)
    if not found:
        _bad(ProjectError("no such item"), 404)
    item = found[2]
    take = next((t for t in item.get("takes") or [] if t.get("id") == take_id), None) if take_id \
        else Projects.chosen_take(item)
    if not take:
        _bad(ProjectError("no such version"), 404)
    return doc, item, take


@app.post("/api/projects/{pid}/items/{item_id}/analyze")
def analyze_song(pid: str, item_id: str, body: dict | None = None, me: Who = Depends(who)):
    """A song's timing, for a music video: served from the version once it has
    been listened to, otherwise queued -- once -- and the job returned, for
    the page to wait on. Listening takes about a minute on the card."""
    _doc, item, take = _item_take(me, pid, item_id, str((body or {}).get("take") or ""))
    if (take.get("analysis") or {}).get("file"):
        # With each sung line's time, for a retouch that redoes only a line.
        lines, an = [], {}
        try:
            an = json.loads(projects.file(me.login, pid, take["analysis"]["file"]).read_text())
            lines = [{k: l.get(k) for k in ("text", "section", "start", "end")} for l in an.get("lines") or []]
        except (ProjectError, OSError, ValueError):
            pass
        # Its words could not be followed by an older analysis -- every song
        # over the aligner's limit, before analysis.VERSION 2 -- so it is
        # listened to again, once: a failure now is the current one, served.
        stale = an.get("error") and not an.get("aligned") and int(an.get("version") or 1) < analysis.VERSION
        if not stale:
            return {"take": take["id"], "analysis": take["analysis"], "lines": lines}
    for job in store.active():
        if job["owner"] == me.login and job["kind"] == "analyze" and job["target"] == item_id \
                and job["params"].get("take") == take["id"]:
            return {"take": take["id"], "job": _public(job, me)}
    # A listen that just failed is reported, not queued again: the page asks
    # every few seconds, and each ask would otherwise start another one.
    if not (body or {}).get("retry"):
        for job in store.recent(owner=me.login, limit=40):
            if job["kind"] == "analyze" and job["target"] == item_id and job["state"] == "failed" \
                    and job["params"].get("take") == take["id"] and time.time() - (job["finished"] or 0) < 600:
                return {"take": take["id"], "failed": job["error"] or "failed"}
    job = _enqueue(me, "analyze", {"take": take["id"]}, pid, item_id,
                   title=item.get("title") or "")
    return {"take": take["id"], "job": _public(store.get(job["id"]) or job, me)}


@app.post("/api/projects/{pid}/items/{item_id}/score")
def song_score(pid: str, item_id: str, body: dict | None = None, me: Who = Depends(who)):
    """A song version's parts -- guitar as notes and tab, piano -- and the
    stems to practise with: served once written, otherwise queued, once, and
    the job returned for the page to wait on. `retry` asks again after a
    failure."""
    body = body or {}
    _doc, item, take = _item_take(me, pid, item_id, str(body.get("take") or ""))
    sc = take.get("score") or {}
    if sc.get("state") == "done":
        return {"take": take["id"], "score": sc}
    for job in store.active():
        if job["owner"] == me.login and job["kind"] == "score" and job["target"] == item_id \
                and job["params"].get("take") == take["id"]:
            return {"take": take["id"], "job": _public(job, me)}
    if sc.get("state") == "failed" and not body.get("retry"):
        return {"take": take["id"], "failed": sc.get("error") or "failed"}
    if not body.get("start"):
        return {"take": take["id"], "none": True}
    job = _enqueue(me, "score", {"take": take["id"]}, pid, item_id, title=item.get("title") or "")
    return {"take": take["id"], "job": _public(store.get(job["id"]) or job, me)}


def _listened(me: Who, pid: str, take: dict) -> dict:
    """A song version's analysis, on the steady beat grid: one listened to
    before the grid existed gets it the first time it is asked for (a few
    seconds on the CPU), and keeps it."""
    meta = take.get("analysis") or {}
    if not meta.get("file"):
        _bad(ProjectError("this version has not been listened to yet"), 409)
    try:
        path = projects.file(me.login, pid, meta["file"])
        an = json.loads(path.read_text())
        if an.get("grid") != 2:
            an = analysis.regrid(an, projects.file(me.login, pid, take["file"]))
            path.write_text(json.dumps(an, ensure_ascii=False))
        return an
    except (ProjectError, OSError, ValueError, KeyError) as exc:
        _bad(exc, 400)


@app.post("/api/projects/{pid}/retime")
def retime_shots(pid: str, body: dict | None = None, me: Who = Depends(who)):
    """The project's shots, as they are, refitted to its song: each keeps its
    place and its words, and gets a length that starts and ends on the music
    (the song's bars, its section changes). A shot whose video is now shorter
    than its length is named, to be made again."""
    body = body or {}
    doc = _project(me, pid)
    song_id = str(body.get("song") or doc["settings"].get("soundtrack") or "")
    if not song_id:
        song = next((a for a in doc.get("audio") or [] if a.get("takes")), None)
        song_id = song["id"] if song else ""
    if not song_id:
        _bad(ProjectError("this project has no song to fit the shots to"), 409)
    _doc, _item, take = _item_take(me, pid, song_id, str(body.get("take") or ""))
    an = _listened(me, pid, take)
    shots = [s for s in doc.get("shots") or [] if not s.get("recorded")]
    if not shots:
        _bad(ProjectError("no shots to fit"))
    try:
        cuts = analysis.cuts_for(an, len(shots))
    except ValueError as exc:
        _bad(exc)
    plan = {s["id"]: c for s, c in zip(shots, cuts)}
    projects.retime(me.login, pid, {sid: (c["start"], c["seconds"]) for sid, c in plan.items()}, song_id)
    _remember(me, pid)
    short = [i + 1 for i, s in enumerate(doc.get("shots") or [])
             if s["id"] in plan and Projects.chosen_take(s) and float(Projects.chosen_take(s).get("seconds") or 0)
             < plan[s["id"]]["seconds"] - 0.05]
    return {"shots": len(cuts), "tempo": an["tempo"], "grid": an.get("grid") == 2, "short": short,
            "cuts": cuts}


@app.post("/api/projects/{pid}/items/{item_id}/cuts")
def music_video_cuts(pid: str, item_id: str, body: dict | None = None, me: Who = Depends(who)):
    """Where a music video's cuts fall on this song, for shots of about
    `shot_seconds` -- each with its time, its section and the words sung in
    it. Arithmetic on the analysis; nothing is queued."""
    body = body or {}
    _doc, _item, take = _item_take(me, pid, item_id, str(body.get("take") or ""))
    an = _listened(me, pid, take)
    try:
        shot = float(body.get("shot_seconds") or 8)
    except (ValueError, TypeError) as exc:
        _bad(exc, 400)
    return {"take": take["id"], "duration": an["duration"], "tempo": an["tempo"],
            "aligned": an["aligned"], "error": an.get("error", ""),
            "sections": [{k: s[k] for k in ("name", "start", "end", "sung")} for s in an["sections"]],
            "cuts": analysis.plan_cuts(an, shot)}


@app.post("/api/projects/{pid}/items/{item_id}/rework")
def rework_song(pid: str, item_id: str, body: dict | None = None, me: Who = Depends(who)):
    """A new version of a song made from one it already has.

    With `start`/`end`: a **repaint** -- only those seconds are made again
    (a changed line, a bar that went wrong) and the rest is the original,
    on the audio unit's ACE-Step. Without: a **cover** of the whole song, held
    to the original by `strength` and keeping its singer's timbre if
    `keep_voice`. Either way the lyrics and style sent become the song's.
    """
    body = body or {}
    doc, item, take = _item_take(me, pid, item_id, str(body.get("take") or ""))
    if (item.get("kind") or "song") != "song":
        _bad(ValueError("only a song can be retouched this way"))
    lyrics = str(body.get("lyrics") if body.get("lyrics") is not None else item.get("lyrics") or "")[:4000]
    style = str(body.get("style") if body.get("style") is not None else item.get("style") or "")[:400]
    projects.save(me.login, pid, {"audio": [
        {**a, "lyrics": lyrics, "style": style} if a["id"] == item_id else a for a in doc.get("audio") or []]})
    params = {"lyrics": lyrics, "style": style, "from_take": take["id"],
              "language": item.get("language") or (doc.get("settings") or {}).get("language") or "es",
              "seconds": float(take.get("seconds") or item.get("seconds") or 60)}
    if body.get("start") is not None and body.get("end") is not None:
        try:
            start, end = float(body["start"]), float(body["end"])
        except (TypeError, ValueError) as exc:
            _bad(exc)
        if not 0 <= start < end <= params["seconds"] + 0.5:
            _bad(ValueError("that stretch is not inside the song"))
        job = _enqueue(me, "repaint", {**params, "start": round(start, 3), "end": round(end, 3)}, pid, item_id,
                       title=item.get("title") or "")
    else:
        try:
            strength = max(0.1, min(1.0, float(body.get("strength") or 0.8)))
        except (TypeError, ValueError) as exc:
            _bad(exc)
        job = _enqueue(me, "song", {**params, "strength": strength, "keep_voice": bool(body.get("keep_voice")),
                                    "bpm": item.get("bpm")}, pid, item_id, title=item.get("title") or "")
    return {"queued": [_public(store.get(job["id"]) or job, me)]}


@app.delete("/api/projects/{pid}/renders/{name}")
def delete_render(pid: str, name: str, me: Who = Depends(who)):
    try:
        projects.delete_render(me.login, pid, f"renders/{name}")
    except ProjectError as exc:
        _bad(exc, 404)
    return {"ok": True}


@app.post("/api/projects/{pid}/items/{item_id}/board")
def choose_board(pid: str, item_id: str, body: dict | None = None, me: Who = Depends(who)):
    """Which storyboard frame of a shot is the chosen one. Its own call, not a
    field of the page's save: a page holding an older copy would otherwise
    choose the old frame again over one just drawn."""
    try:
        return projects.choose_board(me.login, pid, item_id, int((body or {}).get("index", -1)))
    except (ProjectError, TypeError, ValueError) as exc:
        _bad(exc, 404)


@app.post("/api/projects/{pid}/items/{item_id}/board_from")
def board_from(pid: str, item_id: str, body: dict, me: Who = Depends(who)):
    """A picture already in the project as this shot's frame."""
    try:
        return projects.board_from(me.login, pid, item_id, str(body.get("file") or ""))
    except ProjectError as exc:
        _bad(exc, 404 if "no such" in str(exc) else 400)


@app.post("/api/projects/{pid}/items/{item_id}/prompt")
def set_shot_prompt(pid: str, item_id: str, body: dict, me: Who = Depends(who)):
    """One shot's description, set on its own -- a reviewer's improved prompt
    taken up -- without sending the whole project (which a page holding an
    older copy would). In the history under whoever asked."""
    text = str(body.get("prompt") or "").strip()
    if not text:
        _bad(ValueError("a description cannot be empty"))
    try:
        found = Projects.find(projects.load(me.login, pid), item_id)
        if not found or found[0] != "shots":
            raise ProjectError("no such shot")
        projects.set_item_field(me.login, pid, item_id, "prompt", text[:1200])
    except ProjectError as exc:
        _bad(exc, 404)
    _remember(me, pid)
    return {"ok": True}


@app.post("/api/projects/{pid}/items/{item_id}/boards/{board_id}/review")
def board_review(pid: str, item_id: str, board_id: str, body: dict, me: Who = Depends(who)):
    """What the person's assistant saw in a frame, kept on it."""
    try:
        return projects.set_board_review(me.login, pid, item_id, board_id, dict(body.get("review") or {}))
    except ProjectError as exc:
        _bad(exc, 404)


@app.post("/api/projects/{pid}/items/{item_id}/favorite")
def favorite(pid: str, item_id: str, body: dict | None = None, me: Who = Depends(who)):
    """Mark one version the favourite (or none, with no `take`): it is the one
    used, and a new version no longer takes its place."""
    try:
        return projects.set_favorite(me.login, pid, item_id, str((body or {}).get("take") or ""))
    except ProjectError as exc:
        _bad(exc, 404)


@app.delete("/api/projects/{pid}/items/{item_id}/takes/{take_id}")
def delete_take(pid: str, item_id: str, take_id: str, me: Who = Depends(who)):
    try:
        return projects.delete_take(me.login, pid, item_id, take_id)
    except ProjectError as exc:
        _bad(exc, 404)


@app.post("/api/projects/{pid}/references")
def add_reference(pid: str, body: dict, me: Who = Depends(who)):
    """A picture made in this project, kept as a reference picture -- and,
    with `style`, flagged as the film's style at once."""
    try:
        entry = projects.reference_from(me.login, pid, str(body.get("file") or ""), str(body.get("name") or ""))
        if body.get("style"):
            entry = projects.set_upload_style(me.login, pid, entry["file"], True, recipes.MAX_STYLE_REFS)
        return entry
    except ProjectError as exc:
        _bad(exc, 404 if "no such" in str(exc) else 400)


@app.post("/api/projects/{pid}/uploads/{name}/description")
def upload_description(pid: str, name: str, body: dict | None = None, me: Who = Depends(who)):
    """What a reference picture shows, in words, kept on it: written by the
    house's vision model so the Designer -- which never sees the picture --
    can write shots from it."""
    try:
        return projects.set_upload_field(me.login, pid, f"uploads/{name}", "description",
                                         str((body or {}).get("description") or "").strip()[:800])
    except ProjectError as exc:
        _bad(exc, 404)


@app.post("/api/projects/{pid}/uploads/{name}/style")
def upload_style(pid: str, name: str, body: dict | None = None, me: Who = Depends(who)):
    """🎨 A reference picture flagged as the film's style, or not: every frame
    and portrait drawn after it is drawn to match it (recipes.REF_IMAGE_MODEL)."""
    try:
        return projects.set_upload_style(me.login, pid, f"uploads/{name}", bool((body or {}).get("on", True)),
                                         recipes.MAX_STYLE_REFS)
    except ProjectError as exc:
        _bad(exc, 404 if "no such" in str(exc) else 400)


@app.delete("/api/projects/{pid}/uploads/{name}")
def delete_upload(pid: str, name: str, me: Who = Depends(who)):
    try:
        projects.delete_upload(me.login, pid, f"uploads/{name}")
    except ProjectError as exc:
        _bad(exc, 404)
    return {"ok": True}


@app.get("/api/projects/{pid}/file/{rel:path}")
def project_file(pid: str, rel: str, me: Who = Depends(who)):
    try:
        return FileResponse(projects.file(me.login, pid, rel))
    except ProjectError as exc:
        _bad(exc, 404)


# -- generating a project's parts -------------------------------------------------
@app.post("/api/projects/{pid}/generate")
def generate(pid: str, body: dict, me: Who = Depends(who)):
    """Queue work for items of a project.

    Shots are the smart part. Asked for several, they are queued in timeline
    order, each waiting for the one before it when it continues from it, so
    a long scene comes out as one continuous take cut into pieces. Asked for
    one shot in the middle (a retake), it starts from the shot before and,
    when the shot after already exists, must end on that shot's first frame
    -- so the joins on both sides still match and nothing else is redone.
    """
    try:
        doc = projects.load(me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    want = [str(i) for i in body.get("items") or []]
    lang = {"es": "Spanish", "en": "English"}.get(doc["settings"].get("language", "es"), "Spanish")
    size = doc["settings"].get("resolution", "832x480")
    queued = []
    shots = doc.get("shots") or []
    shot_ids = [s["id"] for s in shots]
    chain_prev_job = ""
    for idx, shot in enumerate(shots):
        if shot["id"] not in want:
            chain_prev_job = ""
            continue
        params = {k: shot.get(k) for k in ("prompt", "soundscape", "music", "dialogue", "seconds", "exact")}
        params["characters"] = characters.describe(shot.get("cast") or [], me.login, pid)
        # The project's look, as every frame and portrait already had it: a
        # shot that continues from the one before starts from no frame, and
        # without the words it drifts from the film's style over its length.
        params["look"] = str(doc["settings"].get("look") or "").strip()
        params.update(language_name=lang, size=size, index=idx + 1)
        after = ""
        if idx > 0 and shot.get("continuity", True):
            prev = shots[idx - 1]
            params["continue_from"] = prev["id"]
            if prev["id"] in want:
                after = chain_prev_job                     # made in this same batch: wait for it
            elif not Projects.chosen_take(prev):
                _bad(ValueError(f"shot {idx} has to exist before shot {idx + 1} can continue it"))
        nxt = shots[idx + 1] if idx + 1 < len(shots) else None
        if nxt and nxt["id"] not in want and nxt.get("continuity", True) and Projects.chosen_take(nxt):
            params["end_at"] = nxt["id"]
        starts_fresh = idx == 0 or not shot.get("continuity", True)
        if shot.get("refs"):
            params["start_upload"] = shot["refs"][0] if starts_fresh else None
        # The approved storyboard frame is where a shot that does not carry
        # on from the one before begins: what was looked at is what is made.
        board = Projects.chosen_board(shot)
        use_boards = body.get("use_boards", True) and doc["settings"].get("use_storyboard", True) is not False
        if board and starts_fresh and use_boards:
            params["start_board"] = board["file"]
            params.pop("start_upload", None)
        try:
            recipes.h3_prompt(params)
        except recipes.RecipeError as exc:
            _bad(ValueError(f"shot {idx + 1}: {exc}"))
        job = _enqueue(me, "video_shot", params, pid, shot["id"], shot.get("title") or doc["name"], after)
        queued.append(job["id"])
        chain_prev_job = job["id"]
    for section, kind_of in (("audio", lambda it: it.get("kind", "song")), ("images", lambda it: "image")):
        for item in doc.get(section) or []:
            if item["id"] not in want:
                continue
            kind = kind_of(item)
            params = {k: item.get(k) for k in ("prompt", "lyrics", "style", "seconds", "bpm", "text", "size")}
            params["language"] = item.get("language") or doc["settings"].get("language", "es")
            if kind == "voice" and item.get("speaker"):
                # Said by one of the cast: its own sample is cloned (the
                # manager reads it when the job runs) and its words for it.
                try:
                    ch = characters.get(item["speaker"], me.login, pid)
                except ProjectError:
                    _bad(ValueError(f"{item.get('title') or kind}: that character is gone"))
                if not ch.get("voice"):
                    _bad(ValueError(f"{item.get('title') or kind}: {ch.get('name')} has no voice sample yet"))
                params.update(voice_char=ch["id"], voice_text=ch.get("voice_text") or "")
            elif kind == "voice":
                params["voice_upload"] = item.get("voice")
            try:
                recipes.settings_for(kind, {**params, "voice_file": "x" if kind == "voice" and (
                    item.get("voice") or params.get("voice_char")) else None})
            except recipes.RecipeError as exc:
                _bad(ValueError(f"{item.get('title') or kind}: {exc}"))
            queued.append(_enqueue(me, kind, params, pid, item["id"], item.get("title") or doc["name"])["id"])
    if not queued:
        _bad(ValueError("nothing to generate"))
    sched = {j["id"]: j for j in store.schedule()}
    return {"queued": [_public(sched.get(i) or store.get(i), me) for i in queued]}


@app.get("/api/default-project")
def default_project(name: str = "Alfred", me: Who = Depends(who)):
    """The person's default project -- loose requests land here."""
    return projects.default(me.login, name)


@app.post("/api/projects/{pid}/items")
def add_items(pid: str, body: dict, me: Who = Depends(who)):
    """Add items to a project without touching the rest of it, and queue
    them when asked (`generate`). For requests that come from somewhere other
    than the page -- the assistant -- which must never replace what is there."""
    section = str(body.get("section") or "")
    try:
        added = projects.append(me.login, pid, section, list(body.get("items") or []))
        _remember(me, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    out = {"items": [a["id"] for a in added]}
    if body.get("generate") and added:
        out["queued"] = generate(pid, {"items": out["items"]}, me)["queued"]
    return out


# ---------------------------------------------------------------------------
# The drawing skill's door: Together's /v1/images/generations dialect, so the
# assistant's `images` skill draws here by pointing IMAGE_API_URL at it. Every
# picture it asks for goes into the person's default project, through the one
# queue, like anything else on this card.
#
# The skill waits for a picture, and the card may be busy with somebody's
# film for many minutes. So this waits a while and then answers "queued"
# instead of an image; the skill says so, and the person is notified when it
# is ready, like any other studio job.
IMAGE_WAIT_S = float(os.environ.get("STUDIO_IMAGE_WAIT_S", "170"))


def _nearest_size(width: int, height: int) -> str:
    want = (width or 1024) / (height or 1024)
    return min(recipes.IMAGE_SIZES, key=lambda s: abs(int(s.split("x")[0]) / int(s.split("x")[1]) - want))


@app.post("/v1/images/generations")
def images_generations(body: dict, me: Who = Depends(who)):
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    doc = projects.default(me.login)
    item = projects.append(me.login, doc["id"], "images", [{
        "prompt": prompt, "title": prompt[:60],
        "size": _nearest_size(int(body.get("width") or 1024), int(body.get("height") or 1024))}])[0]
    job_id = generate(doc["id"], {"items": [item["id"]]}, me)["queued"][0]["id"]
    deadline = time.time() + IMAGE_WAIT_S
    while time.time() < deadline:
        job = store.get(job_id)
        if job["state"] == "done":
            path = projects.dir(me.login, doc["id"]) / job["files"][0]
            import base64
            return {"created": int(time.time()), "model": recipes.IMAGE_MODEL, "studio_project": doc["id"],
                    "data": [{"b64_json": base64.b64encode(path.read_bytes()).decode()}]}
        if job["state"] in ("failed", "cancelled"):
            raise HTTPException(502, job["error"] or f"the picture was {job['state']}")
        time.sleep(1.5)
    sched = {j["id"]: j for j in store.schedule()}
    j = sched.get(job_id) or store.get(job_id)
    return JSONResponse({"queued": True, "id": job_id, "studio_project": doc["id"],
                         "position": j.get("position"), "starts_in": j.get("starts_in"),
                         "message": "The house's card is busy: the picture is queued in the Studio "
                                    "(the person's default project) and they will be notified when it is ready."},
                        status_code=202)


@app.post("/api/projects/{pid}/edit")
def edit_shot(pid: str, body: dict, me: Who = Depends(who)):
    """Change part of a shot: re-generate it from its own video, anchored to
    its own first and last frames, with a new description."""
    try:
        doc = projects.load(me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    found = Projects.find(doc, str(body.get("item")))
    if not found or found[0] != "shots" or not Projects.chosen_take(found[2]):
        _bad(ValueError("that shot has nothing to edit yet"))
    shot = found[2]
    params = {"prompt": str(body.get("prompt") or shot.get("prompt") or ""), "seconds": shot.get("seconds"),
              "soundscape": shot.get("soundscape"), "music": shot.get("music"), "dialogue": shot.get("dialogue"),
              "strength": body.get("strength", 0.6), "size": doc["settings"].get("resolution"),
              "index": found[1] + 1}
    job = _enqueue(me, "edit", params, pid, shot["id"], shot.get("title") or doc["name"])
    return _public(job, me)


@app.post("/api/projects/{pid}/render")
def render(pid: str, body: dict | None = None, me: Who = Depends(who)):
    """Stitch the chosen takes into one film, with any audio items laid
    under it. On the CPU, beside the card's queue, not in it.

    A song under it follows the shots: each shot made gets the stretch of the
    song from where that shot sits in the whole video, so a film of the shots
    made so far -- `preview`, the preview's download -- keeps every one in
    time with its words across the gaps."""
    body = body or {}
    try:
        doc = projects.load(me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    base = projects.dir(me.login, pid)
    made = [(s, Projects.chosen_take(s)) for s in doc.get("shots") or []]
    preview = bool(body.get("preview"))
    # What the file is for: "h265" (kept, small) or "h264" (to publish), at
    # the clips' own size or "1080"/"720".
    codec = body.get("format") if body.get("format") in media.CODECS else "h265"
    film_size = str(body.get("size") or "") if str(body.get("size") or "") in media.SIZES else ""
    # Subtitles burnt in, for the clips whose version has them.
    with_subs = bool(body.get("subtitles"))

    def subs_of(t):
        rel = ((t or {}).get("transcript") or {}).get("srt") if with_subs else None
        return base / rel if rel and (base / rel).is_file() else None
    def sections_of(s: dict, t: dict | None) -> list[tuple[float, float, dict | None]] | None:
        """A recording's kept sections over its chosen version -- (start, end,
        its own camera or None) -- or None to use the clip whole. Sections
        drawn over another version are not this one's, and are ignored."""
        secs = s.get("sections") or []
        if not (t and s.get("recorded") and secs and s.get("sections_take") == t.get("id")):
            return None
        total = float(t.get("seconds") or 0) or 86400.0
        return [(sec["start"], min(sec["end"], total), sec["cam"]) for sec in clean_sections(secs)
                if sec["keep"] and min(sec["end"], total) - sec["start"] >= 0.1]
    clips = [base / t["file"] for s, t in made if t]
    # The preview's download is the whole video: a shot not made yet is a
    # placeholder card for its length (in the page's words), so the song runs
    # under it unbroken, the way the page's preview plays it.
    labels = body.get("labels") if isinstance(body.get("labels"), dict) else {}
    # Where each made shot sits in the whole video, the missing ones counted
    # at the length they will have.
    segments, pos = [], 0.0
    for s, t in made:
        secs = sections_of(s, t)
        card = clean_card(s.get("card")) if not t else None
        if card:
            length = card["seconds"]
        elif secs is not None:
            length = sum(b - a for a, b, _cam in secs)
        elif s.get("exact") and s.get("seconds"):
            length = float(s["seconds"])
        elif t and t.get("seconds"):
            length = float(t["seconds"])
        else:
            length = recipes.h3_frames(float(s.get("seconds") or 5)) / recipes.FPS
        if t or card or preview:
            segments.append((pos, length))
        pos += length
    # A preview of frames alone is an animatic, and fine; a film needs shots.
    if not clips and not any(clean_card(s.get("card")) for s, _t in made) \
            and not (preview and any(Projects.chosen_board(s) or s.get("prompt") for s, _t in made)):
        _bad(ValueError("no shot has a take yet"))
    # One render of a project at a time: the page follows the project's one
    # render state, and a second would hand it the first one's file.
    if (render_state.get(f"{me.login}/{pid}") or {}).get("state") == "running":
        _bad(ValueError("a film of this project is already being put together"), 409)
    tracks = []
    for m in body.get("tracks") or []:
        found = Projects.find(doc, str(m.get("item")))
        take = Projects.chosen_take(found[2]) if found else None
        if take:
            tracks.append({"file": base / take["file"], "start": m.get("start", 0), "volume": m.get("volume", 0.8)})
    key = f"{me.login}/{pid}"
    render_state[key] = {"state": "running", "started": time.time()}

    def work():
        stamp = (time.strftime("%Y%m%d-%H%M%S") + ("-preview" if preview else "")
                 + ("" if preview or codec == "h265" else f"-{codec}")
                 + (f"-{film_size}p" if film_size and not preview else ""))
        out = base / "renders" / f"{stamp}.mp4"
        followed = []

        def layers(s: dict, t: dict | None) -> tuple[dict | None, dict | None]:
            """A recording's camera in its corner (when shown) and its sound
            at its volumes, the computer's under the microphone's."""
            if not (t and s.get("recorded")):
                return None, None
            pip = None
            if t.get("cam") and (base / t["cam"]).is_file():
                lay = clean_layout(s.get("cam_layout"))
                pip = {"file": base / t["cam"], **lay} if lay["show"] else None
            mix = clean_mix(s.get("mix"))
            snd = {"own": mix["mic"]}
            if t.get("pc") and (base / t["pc"]).is_file():
                snd.update(file=base / t["pc"], volume=mix["pc"])
            return pip, snd

        # The frame a title card or a callout is drawn at: the first clip's,
        # or the project's own size when there is none yet.
        frame = None
        for _s, _t in made:
            if _t and (base / _t["file"]).is_file():
                _first = media.probe(base / _t["file"])
                frame = (_first["width"] or 832, _first["height"] or 480)
                break
        if frame is None:
            _w, _h = (doc["settings"].get("resolution") or "832x480").split("x")
            frame = (int(_w), int(_h))

        def callouts_of(s: dict, t: dict, n: int, a: float, b: float) -> list[dict]:
            """A recording's callouts that fall in [a, b] of its clip, as
            overlays timed from a: drawn once each, kept for the cleanup."""
            if not (s.get("recorded") and s.get("callouts") and s.get("callouts_take") == t.get("id")):
                return []
            out_overlays = []
            for k, c in enumerate(clean_callouts(s["callouts"])):
                if c["end"] <= a or c["start"] >= b or not c["text"].strip():
                    continue
                png = media.callout(c["text"], c["spot"], frame,
                                    base / "renders" / f"{stamp}-callout{n}-{k}-{int(a * 1000)}.png")
                followed.append(png)
                out_overlays.append({"png": png, "start": max(0.0, c["start"] - a), "end": min(b, c["end"]) - a})
            return out_overlays

        def pieces(s: dict, t: dict | None, n: int) -> list[dict]:
            """What a made item puts in the film: the clip whole, or -- a
            recording cut into sections -- each kept section, from where it
            starts for as long as it lasts, with its own camera (or the
            clip's) and its stretch of the subtitles. A title card is its
            own clip, drawn now."""
            card = clean_card(s.get("card")) if not t else None
            if card:
                clip = media.title_card(card["title"], card["subtitle"], card["seconds"], frame,
                                        base / "renders" / f"{stamp}-title{n}.mp4", card["theme"])
                followed.append(clip)
                return [{"file": clip, "start": None, "length": None, "pip": None, "sound": None, "subs": None,
                         "overlays": None}]
            pip, snd = layers(s, t)
            sub = subs_of(t)
            secs = sections_of(s, t)
            if secs is None:
                whole = float(t.get("seconds") or 0) or 86400.0
                return [{"file": base / t["file"], "start": None, "pip": pip, "sound": snd, "subs": sub,
                         "length": float(s["seconds"]) if s.get("exact") and s.get("seconds") else None,
                         "overlays": callouts_of(s, t, n, 0.0, whole) or None}]
            out_pieces = []
            for j, (a, b, cam) in enumerate(secs):
                own = pip
                if cam is not None and t.get("cam") and (base / t["cam"]).is_file():
                    own = {"file": base / t["cam"], **cam} if cam["show"] else None
                part_sub = None
                if sub:
                    part_sub = media.shift_srt(sub, base / "renders" / f"{stamp}-sub{n}-{j}.srt", a, b)
                    followed.append(part_sub)
                out_pieces.append({"file": base / t["file"], "start": a or None, "length": b - a,
                                   "pip": own, "sound": snd, "subs": part_sub,
                                   "overlays": callouts_of(s, t, n, a, b) or None})
            return out_pieces

        try:
            all_pieces = [pc for n, (s, t) in enumerate(made, 1) if t or clean_card(s.get("card"))
                          for pc in pieces(s, t, n)]
            if not all_pieces and not preview:
                raise media.MediaError("every section of every clip is cut out")
            use_clips = [pc["file"] for pc in all_pieces]
            use_lengths = [pc["length"] for pc in all_pieces]
            use_starts = [pc["start"] for pc in all_pieces]
            use_subs = [pc["subs"] for pc in all_pieces]
            use_pips = [pc["pip"] for pc in all_pieces]
            use_sounds = [pc["sound"] for pc in all_pieces]
            use_overlays = [pc.get("overlays") for pc in all_pieces]
            use_marks = None
            if preview:
                if clips:
                    first = media.probe(clips[0])
                    size = (first["width"] or 832, first["height"] or 480)
                else:
                    w, h = (doc["settings"].get("resolution") or "832x480").split("x")
                    size = (int(w), int(h))
                use_clips, use_lengths, use_marks, use_subs, use_pips, use_sounds = [], [], [], [], [], []
                use_starts, use_overlays = [], []
                badge = str(labels.get("preview") or "PREVIEW")[:30].upper()

                def clock(t: float) -> str:
                    return f"{int(t // 60)}:{t % 60:04.1f}"
                for n, ((s, t), (pos, length)) in enumerate(zip(made, segments), 1):
                    info = (f"{str(labels.get('shot') or 'Shot')[:40]} {n}/{len(made)} · "
                            f"{clock(pos)}-{clock(pos + length)}")
                    if t:
                        takes = s.get("takes") or []
                        k = next((i for i, x in enumerate(takes) if x is t), len(takes) - 1) + 1
                        info += f" · {str(labels.get('version') or 'version')[:30]} {k}/{len(takes)}"
                    mark = media.watermark(badge, info, size, base / "renders" / f"{stamp}-mark{n}.png")
                    followed.append(mark)
                    if t or clean_card(s.get("card")):
                        # Its pieces -- the clip, a recording's kept sections,
                        # or a title card -- each under the same watermark.
                        for pc in pieces(s, t, n):
                            use_overlays.append(pc.get("overlays"))
                            use_marks.append(mark)
                            use_subs.append(pc["subs"])
                            use_pips.append(pc["pip"])
                            use_sounds.append(pc["sound"])
                            use_clips.append(pc["file"])
                            use_lengths.append(pc["length"])
                            use_starts.append(pc["start"])
                        continue
                    use_marks.append(mark)
                    use_subs.append(None)
                    use_pips.append(None)
                    use_sounds.append(None)
                    use_starts.append(None)
                    use_overlays.append(None)
                    if Projects.chosen_board(s):
                        # Not made yet but drawn: the storyboard frame, held for
                        # the shot's length -- an animatic of what is coming.
                        card = media.still(base / Projects.chosen_board(s)["file"], length, size,
                                           base / "renders" / f"{stamp}-card{n}.mp4")
                    else:
                        card = media.placeholder(f"{str(labels.get('shot') or 'Shot')[:40]} {n} · "
                                                 f"{str(labels.get('missing') or 'not made yet')[:60]}",
                                                 str(s.get("prompt") or "")[:600], length, size,
                                                 base / "renders" / f"{stamp}-card{n}.mp4")
                    followed.append(card)
                    use_clips.append(card)
                    use_lengths.append(None)
            film = media.stitch(use_clips, out if not tracks else base / "renders" / f"{stamp}-video.mp4",
                                crossfade=float(body.get("crossfade") or 0), lengths=use_lengths,
                                marks=use_marks, fast=preview, subs=use_subs, pips=use_pips, sounds=use_sounds,
                                starts=use_starts, codec=codec, size=None if preview else (film_size or None),
                                overlays=use_overlays)
            if tracks:
                laid = []
                for k, tr in enumerate(tracks):
                    cut = media.follow(tr["file"], [sg for sg in segments if sg[1] > 0],
                                       base / "renders" / f"{stamp}-song{k}.wav",
                                       delay=float(tr.get("start") or 0))
                    followed.append(cut)
                    laid.append({**tr, "file": cut, "start": 0})
                media.mix(film, laid, out, keep_own=body.get("own_sound", True) is not False)
                film.unlink(missing_ok=True)
            projects.add_render(me.login, pid, {"file": str(out.relative_to(base)), "preview": preview,
                                                "format": "h264" if preview else codec, "size": film_size,
                                                "seconds": round(media.probe(out)["seconds"], 1)})
            render_state[key] = {"state": "done", "file": str(out.relative_to(base))}
        except Exception as exc:                               # noqa: BLE001
            # Anything, not only ffmpeg's own errors: a render that times out
            # (subprocess.TimeoutExpired is not an OSError) or reads a bad
            # probe left the state "running" and the page polling forever.
            log.warning("render %s failed: %s", key, exc)
            render_state[key] = {"state": "failed", "error": str(exc) or exc.__class__.__name__}
        finally:
            for f in followed:
                f.unlink(missing_ok=True)

    renders.submit(work)
    return {"ok": True, "render": render_state[key]}


@app.post("/api/projects/{pid}/story")
def put_story_together(pid: str, body: dict | None = None, me: Who = Depends(who)):
    """An audio story as one file: its voices and songs in order, its
    instrumentals under the voices that follow (story.plan). `format` "audio"
    is an M4A with the cover as its artwork; "video" the cover held over the
    sound, for sites that take video only. On the CPU, beside the queue."""
    body = body or {}
    try:
        doc = projects.load(me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    base = projects.dir(me.login, pid)
    fmt_ = "video" if body.get("format") == "video" else "audio"
    items = []
    for a in doc.get("audio") or []:
        take = Projects.chosen_take(a)
        if take and take.get("file") and (base / take["file"]).is_file():
            items.append((a.get("kind") or "song", base / take["file"], bool(a.get("alone"))))
    if not items:
        _bad(ValueError("no voice or music has a version yet"))
    cover = None
    named = doc["settings"].get("cover")
    for im in doc.get("images") or []:
        take = Projects.chosen_take(im) if im.get("id") == named else None
        if take and take.get("file") and (base / take["file"]).is_file():
            cover = base / take["file"]
    if fmt_ == "video" and not cover:
        _bad(ValueError("a video of the story needs its cover: draw it first"))
    key = f"{me.login}/{pid}"
    if (render_state.get(key) or {}).get("state") == "running":
        _bad(ValueError("this project is already being put together"), 409)
    render_state[key] = {"state": "running", "started": time.time()}

    def work():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        wav = base / "renders" / f"{stamp}-story.wav"
        out = base / "renders" / f"{stamp}-story.{'mp4' if fmt_ == 'video' else 'm4a'}"
        try:
            placements, _total = story.plan([(k, str(f), media.probe(f)["seconds"], al) for k, f, al in items])
            media.story_mix(placements, wav)
            if fmt_ == "video":
                media.story_video(wav, cover, out)
            else:
                media.story_audio(wav, out, cover, doc.get("name") or "")
            projects.add_render(me.login, pid, {"file": str(out.relative_to(base)), "story": True,
                                                "format": "mp4" if fmt_ == "video" else "m4a",
                                                "seconds": round(media.probe(out)["seconds"], 1)})
            render_state[key] = {"state": "done", "file": str(out.relative_to(base))}
        except Exception as exc:                               # noqa: BLE001 -- as the film's render
            log.warning("story %s failed: %s", key, exc)
            render_state[key] = {"state": "failed", "error": str(exc) or exc.__class__.__name__}
        finally:
            wav.unlink(missing_ok=True)

    renders.submit(work)
    return {"ok": True, "render": render_state[key]}


# An explainer: each point's picture or clip on screen while its narration is
# said, with a little air either side; a point with no narration yet holds
# for its own length. Never shorter than MIN_POINT.
POINT_LEAD, POINT_TAIL, MIN_POINT = 0.4, 0.8, 3.0
EXPLAINER_BED = 0.15


def _explainer_plan(login: str, pid: str) -> list:
    """Each point of an explainer with what shows it -- a title card, its
    page written by hand, its clip, its picture -- and its narration's file.
    Raises ValueError naming the first point with nothing to show."""
    doc = projects.load(login, pid)
    base = projects.dir(login, pid)
    points = [s for s in doc.get("shots") or [] if not s.get("recorded")]
    if not points:
        raise ValueError("the explainer has no points yet")
    narration = {}
    for a in doc.get("audio") or []:
        take = Projects.chosen_take(a)
        if a.get("point") and take and take.get("file") and (base / take["file"]).is_file():
            narration[a["point"]] = base / take["file"]
    plan = []
    for n, s in enumerate(points, 1):
        card, write = clean_card(s.get("card")), clean_write(s.get("write"))
        take, board = Projects.chosen_take(s), Projects.chosen_board(s)
        video = base / take["file"] if take and take.get("file") and (base / take["file"]).is_file() else None
        picture = base / board["file"] if board and board.get("file") and (base / board["file"]).is_file() else None
        if not card and not write and not video and not picture:
            raise ValueError(f"point {n} has no picture yet")
        plan.append((s, card, write, video, picture, narration.get(s["id"])))
    return plan


def _assemble_explainer(login: str, pid: str, codec: str, size: str, on_done=None) -> dict:
    """Start putting an explainer together beside the queue; the render's
    state. `on_done(state)` is told how it ended."""
    try:
        plan = _explainer_plan(login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    except ValueError as exc:
        _bad(exc)
    base = projects.dir(login, pid)
    doc = projects.load(login, pid)
    bed = next((base / Projects.chosen_take(a)["file"] for a in doc.get("audio") or []
                if a.get("kind") == "instrumental" and not a.get("point") and Projects.chosen_take(a)
                and (base / Projects.chosen_take(a)["file"]).is_file()), None)
    key = f"{login}/{pid}"
    if (render_state.get(key) or {}).get("state") == "running":
        _bad(ValueError("this project is already being put together"), 409)
    render_state[key] = {"state": "running", "started": time.time()}

    def work():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        tmp = base / "renders" / f".explainer-{stamp}"
        out = base / "renders" / f"{stamp}-explainer-{codec}{f'-{size}p' if size else ''}.mp4"
        try:
            tmp.mkdir(parents=True, exist_ok=True)
            first = next((p for *_rest, p, _n in plan if p), None)
            if first:
                from PIL import Image  # noqa: PLC0415
                with Image.open(first) as im:
                    frame = (im.width // 2 * 2, im.height // 2 * 2)
            else:
                # Nothing drawn to take a shape from (every point written by
                # hand): pages at the size asked for, not scaled up to it.
                frame = media.fit_size(1280, 720, size)
            clips, lengths, placements, pos = [], [], [], 0.0
            # What the sheet in front of the writer already holds: a written
            # point carries on the page of the written point before it -- one
            # exercise worked through on one sheet, a step a point -- and
            # starts a clean one when it says so (`continuity` off: a new
            # exercise), after anything that is not writing, or when full.
            sheet: list[str] = []
            room = handwriting.capacity(frame)
            for i, (s, card, write, video, picture, voice) in enumerate(plan):
                if not write or s.get("continuity") is False or len(sheet) + len(write) > room:
                    sheet = []
                said = media.probe(voice)["seconds"] if voice else 0.0
                length = (card["seconds"] if card and not voice
                          else max(MIN_POINT, POINT_LEAD + said + POINT_TAIL) if voice
                          else max(MIN_POINT, float(s.get("seconds") or 4)))
                if card:
                    clips.append(media.title_card(card["title"], card["subtitle"], length, frame,
                                                  tmp / f"{i}-card.mp4", card["theme"]))
                    lengths.append(None)
                elif write:
                    clips.append(handwriting.page(write, length, frame, tmp / f"{i}-write.mp4", lead=POINT_LEAD,
                                                  already=sheet))
                    lengths.append(None)
                    sheet = sheet + write
                elif video:
                    have = media.probe(video)["seconds"]
                    clips.append(video)
                    lengths.append(min(have, length))
                    if have < length - 0.05:
                        last = media.frame(video, tmp / f"{i}-last.png", "last")
                        clips.append(media.still(last, length - have, frame, tmp / f"{i}-hold.mp4"))
                        lengths.append(None)
                else:
                    clips.append(media.still(picture, length, frame, tmp / f"{i}-still.mp4"))
                    lengths.append(None)
                if voice:
                    placements.append({"file": str(voice), "start": round(pos + POINT_LEAD, 3),
                                       "length": round(said, 3), "volume": 1.0, "loop": False,
                                       "fade_in": 0.0, "fade_out": 0.0})
                pos += length
            film = media.stitch(clips, tmp / "film.mp4", lengths=lengths, codec=codec, size=size)
            if bed:
                placements.append({"file": str(bed), "start": 0.0, "length": round(pos, 3), "volume": EXPLAINER_BED,
                                   "loop": True, "fade_in": 1.0, "fade_out": 2.0})
            if placements:
                sound = media.story_mix(placements, tmp / "sound.wav")
                media.mix(film, [{"file": sound, "start": 0, "volume": 1.0}], out, keep_own=False)
            else:
                film.replace(out)
            projects.add_render(login, pid, {"file": str(out.relative_to(base)), "explainer": True,
                                             "format": codec, "size": size,
                                             "seconds": round(media.probe(out)["seconds"], 1)})
            render_state[key] = {"state": "done", "file": str(out.relative_to(base))}
        except Exception as exc:                               # noqa: BLE001 -- as the film's render
            log.warning("explainer %s failed: %s", key, exc)
            render_state[key] = {"state": "failed", "error": str(exc) or exc.__class__.__name__}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            if on_done:
                on_done(render_state[key])

    renders.submit(work)
    return render_state[key]


def _render_args(body: dict) -> tuple[str, str]:
    codec = body.get("format") if body.get("format") in media.CODECS else "h264"
    size = str(body.get("size") or "1080") if str(body.get("size") or "1080") in media.SIZES else ""
    return codec, size


@app.post("/api/projects/{pid}/explainer")
def put_explainer_together(pid: str, body: dict | None = None, me: Who = Depends(who)):
    """An explainer as one film: its points in order -- a title card drawn, a
    point written by hand on a page, a point's clip when it has one, else its
    picture held -- each on screen for as long as its narration (the voice
    card whose `point` is that shot) takes to say. The narrations, and an
    instrumental under them all when there is one, are one levelled
    soundtrack; the clips' own sound is left out. A clip shorter than its
    narration ends on its last frame, held. H.264 by default (an explainer is
    made to be shown), at the size asked for. On the CPU, beside the queue,
    like the film."""
    codec, size = _render_args(body or {})
    return {"ok": True, "render": _assemble_explainer(me.login, pid, codec, size)}


# An explainer made start to finish without anyone pressing the next button:
# its pictures drawn, its narrations said, and when the last of them lands, the
# film put together. Kept on disk, so a restart in the middle still finishes
# the film once the queue -- which survives restarts -- has made the rest.
AUTO_FILE = DATA / "explainer-auto.json"
_auto_lock = threading.Lock()


def _auto_load() -> dict:
    try:
        return json.loads(AUTO_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _auto_save(runs: dict) -> None:
    AUTO_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTO_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(runs))
    tmp.replace(AUTO_FILE)


def _tell(login: str, pid: str, text: str, ok: bool) -> None:
    if not NOTIFY_URL:
        return
    try:
        requests.post(NOTIFY_URL, json={"login": login, "text": text, "project": pid, "ok": ok},
                      headers={"X-Studio-Secret": SECRET}, timeout=10, verify=False)
    except requests.RequestException as exc:
        log.warning("notify failed: %s", exc)


def _auto_step(job: dict) -> None:
    """After a job of a project being made by itself: stop on a failure, and
    put the film together once nothing of it is left in the queue."""
    key = f"{job.get('owner')}/{job.get('project')}"
    with _auto_lock:
        runs = _auto_load()
        run = runs.get(key)
        if not run:
            return
        login, pid = job["owner"], job["project"]
        if not job.get("ok"):
            runs.pop(key)
            _auto_save(runs)
            what = recipes.estimate_note(job["kind"], job["params"])
            _tell(login, pid, f"El video explicativo se detuvo: no se pudo generar {what}"
                  + (f" ({job['error'][:120]})" if job.get("error") else ""), False)
            return
        if any(j["owner"] == login and j["project"] == pid for j in store.active()):
            return
        runs.pop(key)
        _auto_save(runs)
    try:
        name = projects.load(login, pid).get("name") or ""

        def finished(state: dict) -> None:
            if state.get("state") == "done":
                _tell(login, pid, "Listo: video explicativo" + (f" — {name}" if name else ""), True)
            else:
                _tell(login, pid, f"No se pudo armar el video explicativo: {state.get('error', '')[:160]}", False)
        _assemble_explainer(login, pid, run.get("format", "h264"), run.get("size", "1080"), finished)
    except HTTPException as exc:
        _tell(login, pid, f"No se pudo armar el video explicativo: {exc.detail}", False)


@app.post("/api/projects/{pid}/explainer/auto")
def make_explainer(pid: str, body: dict | None = None, me: Who = Depends(who)):
    """Make what an explainer is missing and then put it together, without
    waiting for anyone: a frame for every point with a picture to draw and
    none yet, every narration not said yet, and the film once the last of
    those is done. Points written by hand need nothing from the card. Nothing
    missing, the film starts now. The frames are not sent to the person's
    assistant for review -- a run that finishes by itself does not wait on
    one."""
    body = body or {}
    codec, size = _render_args(body)
    try:
        doc = projects.load(me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    key = f"{me.login}/{pid}"
    busy = {j["target"] for j in store.active() if j["owner"] == me.login and j["project"] == pid}
    # Remembered before anything is queued: a job finishing before the run
    # was written down would leave the last step to nobody.
    with _auto_lock:
        runs = _auto_load()
        runs[key] = {"format": codec, "size": size, "since": time.time()}
        _auto_save(runs)
    queued = []
    to_draw = [s["id"] for s in doc.get("shots") or [] if not s.get("recorded") and not clean_card(s.get("card"))
               and not clean_write(s.get("write")) and not Projects.chosen_take(s) and not s.get("boards")
               and str(s.get("prompt") or "").strip() and s["id"] not in busy]
    to_say = [a["id"] for a in doc.get("audio") or [] if a.get("point") and str(a.get("text") or "").strip()
              and not (a.get("takes") or []) and a["id"] not in busy]
    try:
        if to_draw:
            queued += [j["id"] for j in storyboard(pid, {"items": to_draw, "review": False}, me)["queued"]]
        if to_say:
            queued += [j["id"] for j in generate(pid, {"items": to_say}, me)["queued"]]
    except HTTPException:
        with _auto_lock:
            runs = _auto_load()
            runs.pop(key, None)
            _auto_save(runs)
        raise
    if not queued and not busy:
        with _auto_lock:
            runs = _auto_load()
            runs.pop(key, None)
            _auto_save(runs)
        return {"ok": True, "queued": [], "render": _assemble_explainer(me.login, pid, codec, size)}
    return {"ok": True, "queued": queued, "waiting": len(busy)}


@app.exception_handler(ProjectError)
def _project_error(_req, exc: ProjectError):
    return JSONResponse({"detail": str(exc)}, status_code=404)

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import analysis, media, recipes
from .manager import Manager
from .characters import Characters
from .projects import ProjectError, Projects
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
# Transcripts wait on the house's speech recogniser -- up to half an hour for
# a long recording -- so they have a pool of their own: sharing the renders'
# one worker held every person's films and encodes behind one transcript.
speech = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-speech")
render_state: dict[str, dict] = {}


def _notify(job: dict) -> None:
    """Tell the person their job finished, through the portal (ntfy)."""
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
    def __init__(self, login: str, name: str, admin: bool):
        self.login, self.name, self.admin = login, name, admin


def who(x_studio_secret: str = Header(""), x_studio_user: str = Header(""),
        x_studio_name: str = Header(""), x_studio_admin: str = Header("")) -> Who:
    if not SECRET or not hmac.compare_digest(x_studio_secret, SECRET):
        raise HTTPException(401, "not authorised")
    if not x_studio_user:
        raise HTTPException(401, "no user")
    return Who(x_studio_user, x_studio_name or x_studio_user, x_studio_admin == "1")


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
    job = store.add(owner=me.login, owner_name=me.name, kind=kind, model=recipes.MODEL_OF[kind],
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
                   "start_board", "voice_char", "from_take")


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
        manager.paused = True
        manager.pause_reason = "update" if (body or {}).get("reason") == "update" else ""
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
    return {"projects": projects.list(me.login)}


@app.post("/api/projects")
def new_project(body: dict, me: Who = Depends(who)):
    return projects.create(me.login, str(body.get("name") or ""), str(body.get("kind") or "free"))


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
        return _char_public(characters.create(me.login, pid, body), me)
    except ProjectError as exc:
        _bad(exc)


@app.put("/api/projects/{pid}/characters/{cid}")
def edit_character(pid: str, cid: str, body: dict, me: Who = Depends(who)):
    _project(me, pid)
    try:
        return _char_public(characters.update(cid, me.login, pid, body, me.admin), me)
    except ProjectError as exc:
        _bad(exc, 403)


@app.post("/api/projects/{pid}/characters/{cid}/widen")
def widen_character(pid: str, cid: str, me: Who = Depends(who)):
    """One scope wider: this project -> all of mine -> the family's."""
    _project(me, pid)
    try:
        return _char_public(characters.widen(cid, me.login, pid, me.admin), me)
    except ProjectError as exc:
        _bad(exc, 403)


@app.delete("/api/projects/{pid}/characters/{cid}")
def delete_character(pid: str, cid: str, me: Who = Depends(who)):
    _project(me, pid)
    try:
        characters.delete(cid, me.login, pid, me.admin)
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
    if not ch.get("look"):
        _bad(ValueError("describe how the character looks first"))
    look = str(doc["settings"].get("look") or "").strip()
    prompt = (f"{look}. " if look else "") + f"Character portrait of {ch['name']}: {ch['look']}. " \
             "Full figure, facing the camera, plain background, clear light."
    job = _enqueue(me, "portrait", {"prompt": prompt[:1500], "size": "832x1216", "admin": me.admin},
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
async def recording_chunk(pid: str, rid: str, n: int, request: Request, me: Who = Depends(who)):
    """One piece of the recording, numbered in the order it was made."""
    d = _rec_dir(me, pid, rid)
    if not d.is_dir():
        _bad(ProjectError("no such recording"), 404)
    data = await request.body()
    if len(data) > RECORD_CHUNK_MAX:
        raise HTTPException(413, "too large")
    if not 0 <= n < 100000:
        _bad(ValueError("bad piece number"))
    (d / f"part-{n:06d}.webm").write_bytes(data)
    return {"ok": True, "n": n}


@app.post("/api/projects/{pid}/recordings/{rid}/finish")
def finish_recording(pid: str, rid: str, me: Who = Depends(who)):
    """The pieces joined in order -- a browser recording's pieces are one
    stream cut up, so joined they are the file -- and encoded into a kept
    clip on the CPU, beside the card's queue. The clip becomes its take."""
    d = _rec_dir(me, pid, rid)
    parts = sorted(d.glob("part-*.webm"))
    if not parts:
        _bad(ValueError("nothing was recorded"))
    base = projects.dir(me.login, pid)
    projects.set_item_field(me.login, pid, rid, "recording", {"state": "processing", "parts": len(parts)})

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
            projects.add_take(me.login, pid, rid, {"file": str(clip.relative_to(base)), "kind": "recording",
                                                   "seconds": seconds, "first": str(first.relative_to(base)),
                                                   "last": str(last.relative_to(base))})
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
            clip, removed = media.cut_silences(base / take["file"], base / "takes" / item_id / f"{stamp}-trim.mp4",
                                               min_s=min_s)
            first = media.frame(clip, clip.with_name(f"{stamp}-trim-first.png"), "first")
            last = media.frame(clip, clip.with_name(f"{stamp}-trim-last.png"), "last")
            seconds = round(media.probe(clip)["seconds"], 2)
            projects.add_take(me.login, pid, item_id, {"file": str(clip.relative_to(base)), "kind": "trimmed",
                                                       "from": take["id"], "seconds": seconds,
                                                       "first": str(first.relative_to(base)),
                                                       "last": str(last.relative_to(base))})
            projects.set_item_field(me.login, pid, item_id, "seconds", seconds)
            projects.set_item_field(me.login, pid, item_id, "trim", {"state": "done", "removed": removed})
        except Exception as exc:                               # noqa: BLE001
            log.warning("trim %s failed: %s", item_id, exc)
            projects.set_item_field(me.login, pid, item_id, "trim", {"state": "failed", "error": str(exc)[:300]})

    renders.submit(work)
    return {"ok": True, "state": "running"}


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
        cast = characters.describe(shot.get("cast") or [], me.login, pid)
        prompt = (f"{look}. " if look else "") + f"Film still: {what}" + (f" Characters: {cast}." if cast else "")
        # `shot_prompt`: the description as it was when the frame was asked
        # for, kept on the frame so the page can tell a frame whose shot has
        # been described differently since -- one to draw again.
        queued.append(_enqueue(me, "board", {"prompt": prompt[:1500], "size": size, "shot_prompt": what[:1200]},
                               pid, shot["id"],
                               shot.get("title") or f"{doc['name']} {idx + 1}")["id"])
    if not queued:
        _bad(ValueError("every shot already has a frame"))
    sched = {j["id"]: j for j in store.schedule()}
    return {"queued": [_public(sched.get(i) or store.get(i), me) for i in queued]}


@app.get("/api/projects/{pid}")
def get_project(pid: str, me: Who = Depends(who)):
    try:
        doc = projects.load(me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    active = [j for j in store.active() if j["owner"] == me.login and j["project"] == pid]
    return {**doc, "jobs": [_public(j, me) for j in active], "render": render_state.get(f"{me.login}/{pid}")}


@app.put("/api/projects/{pid}")
def save_project(pid: str, body: dict, me: Who = Depends(who)):
    try:
        return projects.save(me.login, pid, body)
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


@app.post("/api/projects/{pid}/duplicate")
def duplicate_project(pid: str, body: dict | None = None, me: Who = Depends(who)):
    try:
        return projects.duplicate(me.login, pid, str((body or {}).get("name") or ""))
    except ProjectError as exc:
        _bad(exc, 404)


@app.post("/api/projects/{pid}/upload")
async def upload(pid: str, file: UploadFile = File(...), kind: str = Form("reference"),
                 me: Who = Depends(who)):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "too large")
    try:
        return projects.add_upload(me.login, pid, file.filename or "file", data,
                                   kind if kind in ("reference", "voice", "audio") else "reference")
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
        lines = []
        try:
            an = json.loads(projects.file(me.login, pid, take["analysis"]["file"]).read_text())
            lines = [{k: l.get(k) for k in ("text", "section", "start", "end")} for l in an.get("lines") or []]
        except (ProjectError, OSError, ValueError):
            pass
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


@app.post("/api/projects/{pid}/items/{item_id}/cuts")
def music_video_cuts(pid: str, item_id: str, body: dict | None = None, me: Who = Depends(who)):
    """Where a music video's cuts fall on this song, for shots of about
    `shot_seconds` -- each with its time, its section and the words sung in
    it. Arithmetic on the analysis; nothing is queued."""
    body = body or {}
    _doc, _item, take = _item_take(me, pid, item_id, str(body.get("take") or ""))
    meta = take.get("analysis") or {}
    if not meta.get("file"):
        _bad(ProjectError("this version has not been listened to yet"), 409)
    try:
        an = json.loads(projects.file(me.login, pid, meta["file"]).read_text())
        shot = float(body.get("shot_seconds") or 8)
    except (ProjectError, OSError, ValueError, TypeError) as exc:
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
    """A picture made in this project, kept as a reference picture."""
    try:
        return projects.reference_from(me.login, pid, str(body.get("file") or ""), str(body.get("name") or ""))
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
            if kind == "voice":
                params["voice_upload"] = item.get("voice")
            try:
                recipes.settings_for(kind, {**params, "voice_file": "x" if kind == "voice" and item.get("voice") else None})
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
    # Subtitles burnt in, for the clips whose version has them.
    with_subs = bool(body.get("subtitles"))

    def subs_of(t):
        rel = ((t or {}).get("transcript") or {}).get("srt") if with_subs else None
        return base / rel if rel and (base / rel).is_file() else None
    clips = [base / t["file"] for s, t in made if t]
    # A shot cut to the music is read only up to its cut.
    lengths = [float(s["seconds"]) if s.get("exact") and s.get("seconds") else None for s, t in made if t]
    # The preview's download is the whole video: a shot not made yet is a
    # placeholder card for its length (in the page's words), so the song runs
    # under it unbroken, the way the page's preview plays it.
    labels = body.get("labels") if isinstance(body.get("labels"), dict) else {}
    # Where each made shot sits in the whole video, the missing ones counted
    # at the length they will have.
    segments, pos = [], 0.0
    for s, t in made:
        if s.get("exact") and s.get("seconds"):
            length = float(s["seconds"])
        elif t and t.get("seconds"):
            length = float(t["seconds"])
        else:
            length = recipes.h3_frames(float(s.get("seconds") or 5)) / recipes.FPS
        if t or preview:
            segments.append((pos, length))
        pos += length
    # A preview of frames alone is an animatic, and fine; a film needs shots.
    if not clips and not (preview and any(Projects.chosen_board(s) or s.get("prompt") for s, _t in made)):
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
        stamp = time.strftime("%Y%m%d-%H%M%S") + ("-preview" if preview else "")
        out = base / "renders" / f"{stamp}.mp4"
        followed = []
        try:
            use_clips, use_lengths, use_marks = clips, lengths, None
            use_subs = [subs_of(t) for s, t in made if t]
            if preview:
                if clips:
                    first = media.probe(clips[0])
                    size = (first["width"] or 832, first["height"] or 480)
                else:
                    w, h = (doc["settings"].get("resolution") or "832x480").split("x")
                    size = (int(w), int(h))
                use_clips, use_lengths, use_marks, use_subs = [], [], [], []
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
                    use_marks.append(media.watermark(badge, info, size, base / "renders" / f"{stamp}-mark{n}.png"))
                    followed.append(use_marks[-1])
                    use_subs.append(subs_of(t))
                    if t:
                        use_clips.append(base / t["file"])
                        use_lengths.append(length if s.get("exact") else None)
                    elif Projects.chosen_board(s):
                        # Not made yet but drawn: the storyboard frame, held for
                        # the shot's length -- an animatic of what is coming.
                        card = media.still(base / Projects.chosen_board(s)["file"], length, size,
                                           base / "renders" / f"{stamp}-card{n}.mp4")
                    else:
                        card = media.placeholder(f"{str(labels.get('shot') or 'Shot')[:40]} {n} · "
                                                 f"{str(labels.get('missing') or 'not made yet')[:60]}",
                                                 str(s.get("prompt") or "")[:600], length, size,
                                                 base / "renders" / f"{stamp}-card{n}.mp4")
                    if not t:
                        followed.append(card)
                        use_clips.append(card)
                        use_lengths.append(None)
            film = media.stitch(use_clips, out if not tracks else base / "renders" / f"{stamp}-video.mp4",
                                crossfade=float(body.get("crossfade") or 0), lengths=use_lengths,
                                marks=use_marks, fast=preview, subs=use_subs)
            if tracks:
                laid = []
                for k, tr in enumerate(tracks):
                    cut = media.follow(tr["file"], segments, base / "renders" / f"{stamp}-song{k}.wav",
                                       delay=float(tr.get("start") or 0))
                    followed.append(cut)
                    laid.append({**tr, "file": cut, "start": 0})
                media.mix(film, laid, out, keep_own=body.get("own_sound", True) is not False)
                film.unlink(missing_ok=True)
            projects.add_render(me.login, pid, {"file": str(out.relative_to(base)), "preview": preview,
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


@app.exception_handler(ProjectError)
def _project_error(_req, exc: ProjectError):
    return JSONResponse({"detail": str(exc)}, status_code=404)

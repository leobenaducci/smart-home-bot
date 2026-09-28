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
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import media, recipes
from .manager import Manager
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
renders = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-render")
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


manager = Manager(store, projects, DATA / "scratch", DATA / "logs", idle_s=IDLE_S, notify=_notify)
app = FastAPI(title="home-studio", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _start() -> None:
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


@app.post("/api/jobs")
def add_job(body: dict, me: Who = Depends(who)):
    try:
        job = _enqueue(me, str(body.get("kind")), dict(body.get("params") or {}),
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
    return projects.create(me.login, str(body.get("name") or ""))


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


@app.delete("/api/projects/{pid}/items/{item_id}/takes/{take_id}")
def delete_take(pid: str, item_id: str, take_id: str, me: Who = Depends(who)):
    try:
        return projects.delete_take(me.login, pid, item_id, take_id)
    except ProjectError as exc:
        _bad(exc, 404)


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
        params = {k: shot.get(k) for k in ("prompt", "soundscape", "music", "dialogue", "seconds")}
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
        if shot.get("refs"):
            params["start_upload"] = shot["refs"][0] if idx == 0 or not shot.get("continuity", True) else None
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
    under it. On the CPU, beside the card's queue, not in it."""
    body = body or {}
    try:
        doc = projects.load(me.login, pid)
    except ProjectError as exc:
        _bad(exc, 404)
    base = projects.dir(me.login, pid)
    clips = [base / t["file"] for t in (Projects.chosen_take(s) for s in doc.get("shots") or []) if t]
    if not clips:
        _bad(ValueError("no shot has a take yet"))
    tracks = []
    for m in body.get("tracks") or []:
        found = Projects.find(doc, str(m.get("item")))
        take = Projects.chosen_take(found[2]) if found else None
        if take:
            tracks.append({"file": base / take["file"], "start": m.get("start", 0), "volume": m.get("volume", 0.8)})
    key = f"{me.login}/{pid}"
    render_state[key] = {"state": "running", "started": time.time()}

    def work():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = base / "renders" / f"{stamp}.mp4"
        try:
            film = media.stitch(clips, out if not tracks else base / "renders" / f"{stamp}-video.mp4",
                                crossfade=float(body.get("crossfade") or 0))
            if tracks:
                media.mix(film, tracks, out, keep_own=body.get("own_sound", True) is not False)
                film.unlink(missing_ok=True)
            projects.add_render(me.login, pid, {"file": str(out.relative_to(base)),
                                                "seconds": round(media.probe(out)["seconds"], 1)})
            render_state[key] = {"state": "done", "file": str(out.relative_to(base))}
        except (media.MediaError, OSError) as exc:
            render_state[key] = {"state": "failed", "error": str(exc)}

    renders.submit(work)
    return {"ok": True, "render": render_state[key]}


@app.exception_handler(ProjectError)
def _project_error(_req, exc: ProjectError):
    return JSONResponse({"detail": str(exc)}, status_code=404)

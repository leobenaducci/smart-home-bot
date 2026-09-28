"""home-studio without the card: the queue, projects, the manager, the API.

Run: python services/home-studio/test/test_studio.py   (needs fastapi and ffmpeg)

The worker is a stand-in that "generates" with ffmpeg in a moment, so the
manager, the continuity logic and the filing are exercised end to end
without WanGP or a GPU. What the real models do is measured by the prototype
(docs/home-studio.md), not asserted here.

People are the invented household: Tomi and Mora (parents), Juana.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
tmp = Path(tempfile.mkdtemp(prefix="studio-test-"))
os.environ.update(STUDIO_DATA=str(tmp), STUDIO_SECRET="s" * 32, STUDIO_IDLE_S="1")

from studio import recipes  # noqa: E402
from studio.projects import Projects, ProjectError  # noqa: E402
from studio.store import Store  # noqa: E402

TOMI, MORA, JUANA = "999000111", "999000222", "999000333"
failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + repr(detail)}")
    if not cond:
        failures.append(label)


print("recipes")
check("  5 s is 124 frames, the H3 shape (17n+5)", recipes.h3_frames(5) == 124, recipes.h3_frames(5))
check("  every length lands on 17n+5, within one window",
      all((recipes.h3_frames(s) - 5) % 17 == 0 and 107 <= recipes.h3_frames(s) <= 481 for s in (1, 5, 9.3, 15, 60)))
s = recipes.settings_for("video_shot", {"prompt": "a cat walks", "seconds": 5, "start_image": "/a.png"})
check("  a continued shot starts from a frame (S) with the small text encoder",
      s["image_prompt_type"] == "S" and s["image_start"] == "/a.png" and s["config"] == "gguf_q2_k", s)
s = recipes.settings_for("video_shot", {"prompt": "x", "dialogue": "Hola, ¿qué tal?", "start_image": "/a", "end_image": "/b"})
check("  dialogue goes in H3's <d>[Spanish] ...</d>, both ends anchored (SE)",
      "<d>[Spanish] Hola, ¿qué tal?</d>" in s["prompt"] and s["image_prompt_type"] == "SE", s["prompt"])
s = recipes.settings_for("song", {"lyrics": "[Verse]\nla la", "style": "pop", "seconds": 60})
check("  a song: lyrics are the prompt, style the caption, Spanish by default",
      s["prompt"].startswith("[Verse]") and s["alt_prompt"] == "pop" and s["custom_settings"]["language"] == "es", s)
try:
    recipes.settings_for("voice", {"text": "hola"})
    check("  a voice without a sample is refused", False)
except recipes.RecipeError:
    check("  a voice without a sample is refused", True)

from studio.manager import overall_progress  # noqa: E402
check("  loading the model is early in the bar, whatever WanGP's own number says",
      overall_progress({"phase": "loading_model", "progress": 100}) <= 0.15)
check("  generation is measured by its steps",
      overall_progress({"phase": "inference", "step": 10, "steps": 20}) == 0.575)

print("\nthe queue is fair")
store = Store(tmp / "q1.db")
for i in range(4):
    store.add(owner=TOMI, owner_name="Tomi", kind="video_shot", model="h3", params={}, title=f"shot {i}")
store.add(owner=JUANA, owner_name="Juana", kind="image", model="z", params={}, title="pic")
order = [j["title"] for j in store.order()]
check("  Juana's picture waits for one of Tomi's shots, not four", order.index("pic") <= 1, order)
a = store.add(owner=MORA, owner_name="Mora", kind="video_shot", model="h3", params={}, title="m1")
b = store.add(owner=MORA, owner_name="Mora", kind="video_shot", model="h3", params={}, title="m2", after=a["id"])
order = [j["title"] for j in store.order()]
check("  a shot never runs before the one it continues", order.index("m1") < order.index("m2"), order)
sched = store.schedule()
check("  every queued job has a position and a start estimate",
      all(j["position"] and j["starts_in"] is not None for j in sched) and sched[0]["starts_in"] == 0)
store.cancel(a["id"])
check("  cancelling a shot cancels the ones waiting on it", store.get(b["id"])["state"] == "cancelled")
running = store.add(owner=JUANA, owner_name="Juana", kind="image", model="z", params={})
store.update(running["id"], state="running", started=time.time())
Store(tmp / "q1.db")
check("  a job running when the process died is queued again",
      Store(tmp / "q1.db").get(running["id"])["state"] == "queued")

print("\nprojects are per person, and the page cannot point at files")
projects = Projects(tmp / "projects")
p = projects.create(JUANA, "Mi peli")
doc = projects.save(JUANA, p["id"], {"shots": [{"prompt": "uno", "seconds": 5}, {"prompt": "dos", "seconds": 7}]})
s1, s2 = doc["shots"]
check("  shots get ids and keep their order", [s["prompt"] for s in doc["shots"]] == ["uno", "dos"])
doc = projects.save(JUANA, p["id"], {"shots": [{**s2, "takes": [{"file": "../../999000111/x/secret.mp4"}]}, s1]})
check("  the page reorders, but cannot inject a take", [s["prompt"] for s in doc["shots"]] == ["dos", "uno"]
      and doc["shots"][0]["takes"] == [], doc["shots"][0])
try:
    projects.load(TOMI, p["id"])
    check("  Tomi cannot open Juana's project", False)
except ProjectError:
    check("  Tomi cannot open Juana's project", True)
try:
    projects.file(JUANA, p["id"], "../../../etc/passwd")
    check("  no path out of a project", False)
except ProjectError:
    check("  no path out of a project", True)
check("  a project list is only the person's own", [x["id"] for x in projects.list(JUANA)] == [p["id"]]
      and projects.list(TOMI) == [])

print("\nthe manager, end to end with a stand-in generator")


class FakeWorker:
    """Makes a short video (or a PNG, or a WAV) with ffmpeg, like WanGP would."""

    def __init__(self, scratch):
        self.scratch, self.model, self.last_used = Path(scratch), "", time.time()
        self._out = []
        self.seen = []
        self._lock = threading.Condition()

    def read(self):
        with self._lock:
            while not self._out:
                self._lock.wait()
            return self._out.pop(0)

    def send(self, **msg):
        if "run" not in msg:
            return
        self.seen.append(msg["settings"])
        out = Path(msg["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        mt = msg["settings"]["model_type"]
        if mt == recipes.VIDEO_MODEL:
            f = out / "shot.mp4"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x96:rate=24",
                            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-shortest", "-pix_fmt", "yuv420p", str(f)],
                           check=True)
        elif mt == recipes.IMAGE_MODEL:
            f = out / "pic.png"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64",
                            "-frames:v", "1", str(f)], check=True)
        else:
            f = out / "song.wav"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330",
                            "-t", "1", str(f)], check=True)
        with self._lock:
            self._out += [{"kind": "progress", "id": msg["run"], "progress": 0.5, "phase": "Denoising"},
                          {"kind": "done", "id": msg["run"], "success": True, "files": [str(f)], "errors": []}]
            self._lock.notify_all()

    def alive(self):
        return True

    def stop(self):
        pass


if not shutil.which("ffmpeg"):
    print("  SKIP: no ffmpeg here")
else:
    from studio.manager import Manager
    store2 = Store(tmp / "q2.db")
    fake = FakeWorker(tmp / "scratch")
    fake._out.append({"kind": "ready"})
    notified = []
    mgr = Manager(store2, projects, tmp / "scratch", tmp / "logs", idle_s=3600,
                  notify=notified.append, worker_factory=lambda: fake)
    doc = projects.load(JUANA, p["id"])
    first, second = doc["shots"]
    j1 = store2.add(owner=JUANA, owner_name="Juana", kind="video_shot", model=recipes.VIDEO_MODEL,
                    params={"prompt": "dos", "seconds": 5}, project=p["id"], target=first["id"])
    j2 = store2.add(owner=JUANA, owner_name="Juana", kind="video_shot", model=recipes.VIDEO_MODEL,
                    params={"prompt": "uno", "seconds": 5, "continue_from": first["id"]},
                    project=p["id"], target=second["id"], after=j1["id"])
    mgr.start()
    deadline = time.time() + 60
    while time.time() < deadline and store2.get(j2["id"])["state"] not in ("done", "failed"):
        time.sleep(0.2)
    doc = projects.load(JUANA, p["id"])
    t1, t2 = Projects.chosen_take(doc["shots"][0]), Projects.chosen_take(doc["shots"][1])
    check("  both shots made, filed as takes with their first and last frames",
          t1 and t2 and all((projects.dir(JUANA, p["id"]) / t[k]).is_file() for t in (t1, t2) for k in ("file", "first", "last")),
          (store2.get(j2["id"]), t1, t2))
    check("  the second started from the first's last frame, read when it ran",
          fake.seen[1].get("image_start", "").endswith(t1["last"]), fake.seen[1])
    check("  each person is told when their job is done", len(notified) == 2 and all(n["ok"] for n in notified))
    # A retake of the first shot, now that the second exists: it must end on the second's first frame.
    j3 = store2.add(owner=JUANA, owner_name="Juana", kind="video_shot", model=recipes.VIDEO_MODEL,
                    params={"prompt": "dos otra vez", "seconds": 5, "end_at": second["id"]},
                    project=p["id"], target=first["id"])
    mgr.wake()
    while time.time() < deadline and store2.get(j3["id"])["state"] not in ("done", "failed"):
        time.sleep(0.2)
    doc = projects.load(JUANA, p["id"])
    check("  a retake keeps the old take and ends on the next shot's first frame",
          len(doc["shots"][0]["takes"]) == 2 and fake.seen[2].get("image_end", "").endswith(t2["first"])
          and fake.seen[2]["image_prompt_type"] == "E", (doc["shots"][0]["takes"], fake.seen[2]))

    # A song comes out of the generator as WAV and is kept as MP3: the disk
    # keeps every take, and WAV is ~10 MB a minute.
    song = projects.append(JUANA, p["id"], "audio", [{"kind": "song", "lyrics": "[Verse]\nla"}])[0]
    j4 = store2.add(owner=JUANA, owner_name="Juana", kind="song", model=recipes.SONG_MODEL,
                    params={"lyrics": "[Verse]\nla", "seconds": 10}, project=p["id"], target=song["id"])
    mgr.wake()
    while time.time() < deadline and store2.get(j4["id"])["state"] not in ("done", "failed"):
        time.sleep(0.2)
    doc = projects.load(JUANA, p["id"])
    st = Projects.chosen_take(doc["audio"][-1]) or {}
    song_dir = projects.dir(JUANA, p["id"])
    check("  a song is filed as MP3, with its length",
          st.get("file", "").endswith(".mp3") and (song_dir / st["file"]).is_file() and st.get("seconds", 0) > 0.5,
          (store2.get(j4["id"]), st))
    check("  and no WAV is left beside it", not list((song_dir / "takes").rglob("*.wav")))

    from studio import media
    # One made before that: a WAV take on disk, which the start-up pass converts
    # and re-points, and a second pass leaves alone.
    old = song_dir / "takes" / song["id"] / "old-0.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220",
                    "-t", "1", str(old)], check=True)
    projects.add_take(JUANA, p["id"], song["id"], {"file": str(old.relative_to(song_dir)), "kind": "song"})
    check("  an old WAV take is converted once", projects.compress_audio_takes(media.compress_audio) == 1
          and projects.compress_audio_takes(media.compress_audio) == 0)
    st = Projects.chosen_take(projects.load(JUANA, p["id"])["audio"][-1])
    check("  and the project points at the MP3", st["file"].endswith("old-0.mp3") and not old.exists()
          and (song_dir / st["file"]).is_file(), st)

    shot_file = projects.dir(JUANA, p["id"]) / Projects.chosen_take(doc["shots"][0])["file"]
    check("  a shot is kept as H.265, under the name the generator's file had",
          media.probe(shot_file)["codec"] == "hevc" and media.probe(shot_file)["has_audio"], media.probe(shot_file))
    check("  and the generator is handed H.264 when it has to read one",
          media.probe(media.for_generator(shot_file, tmp / "gen" / "source.mp4"))["codec"] == "h264")

    film = media.stitch([projects.dir(JUANA, p["id"]) / Projects.chosen_take(s)["file"] for s in doc["shots"]],
                        tmp / "film.mp4", crossfade=0.2)
    check("  the shots stitch into one film with sound", media.probe(film)["has_audio"]
          and 1.5 < media.probe(film)["seconds"] < 2.1, media.probe(film))
    check("  as H.265", media.probe(film)["codec"] == "hevc", media.probe(film))
    mixed = media.mix(film, [{"file": song_dir / st["file"], "volume": 0.8}], tmp / "mixed.mp4")
    check("  and a song laid under it keeps the picture as it was",
          media.probe(mixed)["codec"] == "hevc" and media.probe(mixed)["has_audio"], media.probe(mixed))
    mgr.stop()

print("\nthe API: who sees what")
os.environ["STUDIO_DATA"] = str(tmp / "api")
import importlib  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import studio.app as A  # noqa: E402
importlib.reload(A)
A.manager.start = lambda: None                                   # no worker in this test
c = TestClient(A.app)


def h(login, name, admin=False):
    return {"X-Studio-Secret": "s" * 32, "X-Studio-User": login, "X-Studio-Name": name,
            "X-Studio-Admin": "1" if admin else ""}


check("  no secret, no entry", c.get("/api/queue", headers={"X-Studio-User": JUANA}).status_code == 401)
pj = c.post("/api/projects", json={"name": "Gatos"}, headers=h(JUANA, "Juana")).json()
c.put(f"/api/projects/{pj['id']}", json={"shots": [{"prompt": "un gato", "seconds": 5}, {"prompt": "otro gato", "seconds": 5}],
                                        "images": [{"prompt": "un gato rojo"}]}, headers=h(JUANA, "Juana"))
doc = c.get(f"/api/projects/{pj['id']}", headers=h(JUANA, "Juana")).json()
r = c.post(f"/api/projects/{pj['id']}/generate", json={"items": [s["id"] for s in doc["shots"]] + [doc["images"][0]["id"]]},
           headers=h(JUANA, "Juana")).json()
check("  a storyboard queues its shots chained, plus the picture", len(r["queued"]) == 3, r)
q_tomi = c.get("/api/queue", headers=h(TOMI, "Tomi")).json()["queued"]
check("  Tomi sees Juana's jobs, by name and kind, and nothing of their content",
      q_tomi and all(j["owner_name"] == "Juana" and "title" not in j and "project" not in j and "files" not in j
                     for j in q_tomi), q_tomi[:1])
check("  and when each should start", all(j["position"] and j["starts_in"] is not None for j in q_tomi))
jid = q_tomi[0]["id"]
check("  Tomi cannot cancel Juana's job", c.delete(f"/api/jobs/{jid}", headers=h(TOMI, "Tomi")).status_code == 404)
check("  a parent can", c.delete(f"/api/jobs/{jid}", headers=h(MORA, "Mora", admin=True)).status_code == 200)
check("  and a child cannot pause the card", c.post("/api/admin/pause", json={}, headers=h(JUANA, "Juana")).status_code == 403)
check("  Juana cannot open Tomi's projects",
      c.get(f"/api/projects/{pj['id']}", headers=h(TOMI, "Tomi")).status_code == 404)

print("\nthe default project, for what the assistant is asked")
d1 = c.get("/api/default-project", headers=h(JUANA, "Juana")).json()
d2 = c.get("/api/default-project", headers=h(JUANA, "Juana")).json()
check("  made once, then the same one", d1["id"] == d2["id"] and d1.get("default") is True, (d1["id"], d2["id"]))
lst = c.get("/api/projects", headers=h(JUANA, "Juana")).json()["projects"]
check("  and listed first", lst[0]["id"] == d1["id"] and lst[0]["default"], [x["name"] for x in lst])
r1 = c.post(f"/api/projects/{d1['id']}/items", json={"section": "images", "items": [{"prompt": "un gato"}]},
            headers=h(JUANA, "Juana")).json()
r2 = c.post(f"/api/projects/{d1['id']}/items", json={"section": "images", "items": [{"prompt": "un perro"}], "generate": True},
            headers=h(JUANA, "Juana")).json()
imgs = c.get(f"/api/projects/{d1['id']}", headers=h(JUANA, "Juana")).json()["images"]
check("  two requests add two items; neither replaces the other",
      [i["prompt"] for i in imgs] == ["un gato", "un perro"] and len(r2.get("queued") or []) == 1, imgs)
A.IMAGE_WAIT_S = 0.5
r = c.post("/v1/images/generations", json={"prompt": "a red boat", "width": 1344, "height": 768},
           headers=h(JUANA, "Juana"))
check("  the drawing door queues in the default project and says so when the card is busy",
      r.status_code == 202 and r.json()["queued"] and r.json()["studio_project"] == d1["id"], (r.status_code, r.text[:200]))
imgs = c.get(f"/api/projects/{d1['id']}", headers=h(JUANA, "Juana")).json()["images"]
check("  at the size nearest the one asked for", imgs[-1]["prompt"] == "a red boat" and imgs[-1]["size"] == "1344x768",
      imgs[-1])
check("  and nobody else's default project is involved",
      c.get("/api/default-project", headers=h(TOMI, "Tomi")).json()["id"] != d1["id"])

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)

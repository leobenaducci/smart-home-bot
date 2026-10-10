"""home-studio without the card: the queue, projects, the manager, the API.

Run: python services/home-studio/test/test_studio.py   (needs fastapi and ffmpeg)

The worker is a stand-in that "generates" with ffmpeg in a moment, so the
manager, the continuity logic and the filing are exercised end to end
without WanGP or a GPU. What the real models do is measured by the prototype
(docs/home-studio.md), not asserted here.

People are the invented household: Tomi and Mora (parents), Juana.
"""
import os
import re
import shutil
from collections import Counter
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
two = recipes.h3_prompt({"prompt": "two kids at a lighthouse", "characters": "Tomi: a boy of ten; Mora: a girl of nine",
                         "dialogue": "Tomi: ¿Ves la luz?\nMora: Sí, gira.\nTomi: ¡Qué lindo!\nLook: the sea"})
check("  a script's lines go to their speakers -- S1, S2 by who speaks first -- without the name spoken",
      "(S1) <d>[Spanish] ¿Ves la luz?</d> (S2) <d>[Spanish] Sí, gira.</d> (S1) <d>[Spanish] ¡Qué lindo!</d>" in two
      and "The characters speak" in two and "Tomi:" not in two.split("synchronization:")[1], two)
check("  and a colon in a line that names no one in the shot is just part of the line",
      "(S1) <d>[Spanish] Look: the sea</d>" in two, two)
s = recipes.settings_for("video_shot", {"prompt": "a cat walks", "look": "pastel watercolour."})
check("  a shot's video carries the film's look, the same words as its frame",
      s["prompt"].count("Visual style: pastel watercolour.") == 1
      and "Visual style" not in recipes.settings_for("video_shot", {"prompt": "a cat walks"})["prompt"], s["prompt"])
s = recipes.settings_for("board", {"prompt": "x", "size": "1344x768", "with_refs": True, "image_refs": ["/a.png", "/b.png"]})
check("  a picture drawn from reference pictures is FLUX.2 klein's, the pictures as people/objects (I)",
      s["model_type"] == recipes.REF_IMAGE_MODEL == "flux2_klein_4b" and s["video_prompt_type"] == "I"
      and s["image_refs"] == ["/a.png", "/b.png"], s)
s = recipes.settings_for("board", {"prompt": "x", "with_refs": True, "image_refs": []})
check("  with its pictures gone since, the same model from the words alone",
      s["model_type"] == recipes.REF_IMAGE_MODEL and "image_refs" not in s and s["video_prompt_type"] == "", s)
check("  and the job is filed under the model it will run on",
      recipes.model_for("board", {"with_refs": True}) == recipes.REF_IMAGE_MODEL
      and recipes.model_for("board", {}) == recipes.IMAGE_MODEL and recipes.model_for("video_shot", {"with_refs": True})
      == recipes.VIDEO_MODEL)
s = recipes.settings_for("song", {"lyrics": "[Verse]\nla la", "style": "pop", "seconds": 60})
check("  a song: lyrics are the prompt, style the caption, Spanish by default",
      s["prompt"].startswith("[Verse]") and s["alt_prompt"] == "pop" and s["custom_settings"]["language"] == "es", s)
check("  audio asks for the audio profile, whole on the card; a video keeps the session's",
      s["override_profile"] == recipes.AUDIO_PROFILE
      and recipes.settings_for("instrumental", {"style": "rock"})["override_profile"] == recipes.AUDIO_PROFILE
      and recipes.settings_for("voice", {"text": "hola", "voice_file": "/v.wav"})["override_profile"] == recipes.AUDIO_PROFILE
      and "override_profile" not in recipes.settings_for("video_shot", {"prompt": "x"})
      and "override_profile" not in recipes.settings_for("image", {"prompt": "x"}), s)
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
rr = Store(tmp / "rr.db")
for i in range(6):
    rr.add(owner=TOMI, owner_name="Tomi", kind="board", model="flux", params={}, title=f"T{i}")
prev = None
for i in range(3):
    prev = rr.add(owner=MORA, owner_name="Mora", kind="video_shot", model="h3", params={}, title=f"M{i}",
                  after=prev["id"] if prev else "")
for i in range(2):
    rr.add(owner=JUANA, owner_name="Juana", kind="song", model="ace", params={}, title=f"J{i}")
check("  several people at once take turns, a job each, whatever the model or the queue's length",
      [j["title"] for j in rr.order()] == ["T0", "M0", "J0", "T1", "M1", "J1", "T2", "M2", "T3", "T4", "T5"],
      [j["title"] for j in rr.order()])
rr.update(rr.order()[0]["id"], state="running", started=time.time())
check("  and whoever is on the card goes to the back of the turn",
      [j["title"] for j in rr.order()][:3] == ["M0", "J0", "T1"], [j["title"] for j in rr.order()])
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

print("\nthe bar, where WanGP gives no steps")
from studio.manager import overall_progress  # noqa: E402
check("  a stage with no steps follows the clock, not WanGP's own 100%",
      overall_progress({"phase": "Inference", "progress": 100}, 0.3) < 0.5,
      overall_progress({"phase": "Inference", "progress": 100}, 0.3))
check("  and a job running long looks slow, never finished",
      overall_progress({"phase": "Inference", "progress": 100}, 5.0) < 0.95)
check("  steps still win where they exist",
      overall_progress({"phase": "Denoising", "step": 5, "steps": 10}, 0.9) == round(0.2 + 0.75 * 0.5, 3))

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
        self.previews = getattr(self, "previews", [])
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
        pv = out / "preview.jpg"
        pv.write_bytes(b"\xff\xd8 not really a jpeg")
        self.previews.append(pv)
        with self._lock:
            self._out += [{"kind": "progress", "id": msg["run"], "progress": 0.5, "phase": "Denoising"},
                          {"kind": "preview", "id": msg["run"], "file": str(pv)},
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
    # Not the real card: this machine's Studio may be on it while the suite runs.
    mgr.card_used = lambda: 0
    doc = projects.load(JUANA, p["id"])
    first, second = doc["shots"]
    fr = projects.dir(JUANA, p["id"]) / "takes" / "fr.png"
    fr.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=160x96", "-frames:v", "1",
                    str(fr)], check=True)
    j1 = store2.add(owner=JUANA, owner_name="Juana", kind="video_shot", model=recipes.VIDEO_MODEL,
                    params={"prompt": "dos", "seconds": 5, "start_board": "takes/fr.png"}, project=p["id"], target=first["id"])
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
    check("  a video remembers the frame it started from, and one that continued has none",
          t1.get("board") == "takes/fr.png" and "board" not in t2, (t1, t2))
    check("  the second started from the first's last frame, read when it ran",
          fake.seen[1].get("image_start", "").endswith(t1["last"]), fake.seen[1])
    check("  each person is told when their job is done", len(notified) == 2 and all(n["ok"] for n in notified))
    check("  an in-progress picture is kept while a job runs, and removed when it ends",
          fake.previews and not mgr.previews and not any(pv.exists() for pv in fake.previews), (mgr.previews, fake.previews))
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

    def loudest(path):
        out = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                             capture_output=True, text=True).stderr
        line = next((l for l in out.splitlines() if "max_volume" in l), "max_volume: -inf dB")
        return float(line.split("max_volume:")[1].split("dB")[0].strip().replace("inf", "1e9").replace("-1e9", "-1e9"))
    silent = media.mix(film, [{"file": song_dir / st["file"], "volume": 0.0}], tmp / "mute.mp4", keep_own=False)
    check("  a music video can drop the shots' own sound: with the song at zero it is silent",
          loudest(silent) < -80 and loudest(mixed) > -40, (loudest(silent), loudest(mixed)))

    print("\ndeleting a version, and a file somebody brought")
    base = projects.dir(JUANA, p["id"])
    doc = projects.load(JUANA, p["id"])
    first_id = doc["shots"][0]["id"]
    takes = doc["shots"][0]["takes"]
    gone, kept = takes[0], takes[1]
    projects.save(JUANA, p["id"], {"shots": [{**sh, "chosen": 1 if sh["id"] == first_id else sh.get("chosen", -1)}
                                             for sh in doc["shots"]]})
    item = projects.delete_take(JUANA, p["id"], first_id, gone["id"])
    check("  the version leaves the project", [t["id"] for t in item["takes"]] == [kept["id"]], item["takes"])
    check("  and the disk: its clip and both frames",
          not any((base / gone[k]).exists() for k in ("file", "first", "last")), gone)
    check("  the other version is untouched", (base / kept["file"]).is_file())
    check("  and is still the one chosen, at its new place", item["chosen"] == 0, item["chosen"])
    try:
        projects.delete_take(JUANA, p["id"], first_id, gone["id"])
        check("  a version cannot be deleted twice", False)
    except ProjectError:
        check("  a version cannot be deleted twice", True)
    try:
        projects.delete_take(TOMI, p["id"], first_id, kept["id"])
        check("  nor by somebody else", False)
    except ProjectError:
        check("  nor by somebody else", (base / kept["file"]).is_file())

    up = projects.add_upload(JUANA, p["id"], "cara.png", b"png", "reference")
    doc = projects.load(JUANA, p["id"])
    projects.save(JUANA, p["id"], {"shots": [{**sh, "refs": [up["file"]]} if i == 0 else sh
                                             for i, sh in enumerate(doc["shots"])]})
    projects.delete_upload(JUANA, p["id"], up["file"])
    doc = projects.load(JUANA, p["id"])
    check("  an upload is deleted from the disk and the list",
          not (base / up["file"]).exists() and not any(u["file"] == up["file"] for u in doc["uploads"]))
    check("  and the shot that started from it no longer points at it", doc["shots"][0].get("refs") == [], doc["shots"][0])
    for bad in ("takes/x.mp4", "uploads/../project.json", "../other/uploads/a.png"):
        try:
            projects.delete_upload(JUANA, p["id"], bad)
            check(f"  {bad!r} is not an upload", False)
        except ProjectError:
            check(f"  {bad!r} is not an upload", (base / "project.json").is_file())

    print("\na favourite version stays the one used")
    fav_doc = projects.load(JUANA, p["id"])
    sid = fav_doc["audio"][-1]["id"]
    first_take = fav_doc["audio"][-1]["takes"][0]["id"]
    item = projects.set_favorite(JUANA, p["id"], sid, first_take)
    check("  marking one makes it the chosen one", item["chosen"] == 0 and item["takes"][0].get("favorite"), item["chosen"])
    projects.add_take(JUANA, p["id"], sid, {"file": "takes/x.mp3", "kind": "song"})
    item = Projects.find(projects.load(JUANA, p["id"]), sid)[2]
    check("  a new version does not take its place", item["chosen"] == 0 and len(item["takes"]) >= 2, item["chosen"])
    item = projects.set_favorite(JUANA, p["id"], sid, "")
    projects.add_take(JUANA, p["id"], sid, {"file": "takes/y.mp3", "kind": "song"})
    item = Projects.find(projects.load(JUANA, p["id"]), sid)[2]
    check("  with none marked, the newest is chosen again", item["chosen"] == len(item["takes"]) - 1, item["chosen"])

    print("\na song listened to, for a music video")
    from studio import analysis
    song_file = tmp / "tune.wav"
    # Twenty seconds of clicks at 120 bpm, so there are beats to find.
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "aevalsrc='if(lt(mod(t,0.5),0.03),sin(2*PI*880*t),0)':s=22050:d=20", str(song_file)], check=True)
    lyr = ("[Verso]\nNació con alas para volar al mar\npero soñaba con la tierra\n\n"
           "[Coro]\nDale pelícano sin descansar\ncontra el barro y la grava")
    lines = analysis.lyric_lines(lyr)
    check("  the lyrics' tags name the sections", [l["section"] for l in lines] == ["Verso", "Verso", "Coro", "Coro"], lines)

    class FakeAudio(analysis.AudioServer):
        def __init__(self):
            super().__init__("http://audio.invalid")

        def separate_vocals(self, song, work):
            return song

        def align_mono(self, mono, text, language):
            # Two words a second from 3 s, a three-second break after the verse
            # (12 words), so the song has an intro, a break and an outro.
            out, t = [], 3.0
            for i, w in enumerate(text.split()):
                if i == 12:
                    t += 3.0
                out.append({"word": w.rstrip(","), "start": t, "end": t + 0.4})
                t += 0.5
            return out

        def wait_idle(self, timeout=60.0):
            pass
    an = analysis.analyze(song_file, lyr, "es", FakeAudio(), tmp / "an")
    check("  each line gets its time from the aligned words",
          an["aligned"] and an["lines"][0]["start"] == 3.0 and an["lines"][2]["start"] > an["lines"][1]["end"] + 2.9,
          an["lines"])
    names = [(s["name"], s["sung"]) for s in an["sections"]]
    check("  sections come out in time, with the unsung stretches marked",
          names[0] == ("Instrumental", False) and ("Verso", True) in names and ("Coro", True) in names
          and names[-1] == ("Instrumental", False), names)

    # A long song, followed a window at a time: the aligner here refuses more
    # than 90 s, places what is really sung in the window, and squeezes any
    # lines it was given beyond that into the window's last two seconds --
    # what too much text does to a forced aligner.
    truth, t = [], 6.0
    for n in range(44):
        if n == 28:
            t += 25.0                                   # a guitar solo
        truth.append((t, t + 3.2))
        t += 4.6
    long_lines = [{"text": f"linea {n} palabra{n}", "section": "Verse", "section_index": 0} for n in range(44)]
    calls = []

    def fake_window(start, end, some):
        assert end - start <= 90.0, ("refused", start, end)
        calls.append((round(start, 1), round(end, 1), len(some)))
        out, crowd = [], end - 2.0
        for line in some:
            n = int(line["text"].split()[1])
            a, b = truth[n]
            if a >= start and b <= end:
                out += [{"word": "linea", "start": a, "end": a + 1.5}, {"word": str(n), "start": a + 1.5, "end": a + 2.0},
                        {"word": f"palabra{n}", "start": a + 2.0, "end": b}]
            else:
                out += [{"word": w, "start": crowd, "end": crowd + 0.05} for w in ("linea", str(n), f"palabra{n}")]
                crowd += 0.05
        return out
    song_len = truth[-1][1] + 8.0
    gaps = [(truth[n][1], truth[n + 1][0]) for n in range(43)]
    got = analysis.align_windows(fake_window, long_lines, song_len, gaps)
    check(f"  a {song_len:.0f} s song is followed in windows the aligner takes",
          len(calls) >= 3 and all(e - s_ <= 90 for s_, e, _k in calls), calls)
    wrong = [n for n, l in enumerate(got) if abs(l.get("start", -99) - truth[n][0]) > 0.01]
    check("  and every line lands where it is sung, the solo included", not wrong,
          [(n, got[n].get("start"), truth[n][0]) for n in wrong][:6])
    check("  each window ends in a pause, so no word is cut in two",
          all(any(abs(e - (a + b) / 2) < 0.01 for a, b in gaps) or e == round(song_len, 1) for _s, e, _k in calls), calls)
    held = analysis.end_at_pauses([{"text": "y su caballo", "start": 59.25, "end": 67.73,
                                    "words": [{"word": "y", "start": 59.25, "end": 59.5},
                                              {"word": "caballo", "start": 61.0, "end": 67.73}]},
                                   {"text": "otra", "start": 67.81, "end": 70.6}],
                                  [(63.9, 67.8), (70.7, 70.9)])
    check("  a line held on through a pause ends where the pause begins; a short pause changes nothing",
          held[0]["end"] == 63.9 and held[1]["end"] == 70.6, held)
    nothing = analysis.align_windows(lambda a, b, s_: [], long_lines[:3], 200.0)
    check("  an aligner that finds nothing leaves the lines unplaced, not the analysis broken",
          len(nothing) == 3 and not any("start" in l for l in nothing), nothing)
    check("  the beats are found (120 bpm clicks)", 100 < an["tempo"] < 140 and len(an["beats"]) >= 12, (an["tempo"], len(an["beats"])))
    gaps = [b - a for a, b in zip(an["beats"], an["beats"][1:])]
    check("  on a steady grid: one tempo, every beat the same distance apart",
          an.get("grid") == 2 and abs(an["tempo"] - 120) < 1.0 and max(gaps) - min(gaps) < 0.005, (an["tempo"], min(gaps), max(gaps)))
    fake_an = {"duration": 60.0, "tempo": 120.0, "beats": [0.25 + 0.5 * i for i in range(119)],
               "bars": [0.25 + 2.0 * i for i in range(30)], "lines": [],
               "sections": [{"name": "Verso", "start": 0.0, "end": 22.1, "sung": True},
                            {"name": "Coro", "start": 22.1, "end": 60.0, "sung": True}]}
    fitted = analysis.cuts_for(fake_an, 6)
    ends = [c["end"] for c in fitted]
    check("  refitting: exactly the shots asked for, covering the song end to end",
          len(fitted) == 6 and fitted[0]["start"] == 0 and ends[-1] == 60.0
          and all(abs(a["end"] - b["start"]) < 1e-6 for a, b in zip(fitted, fitted[1:])), ends)
    check("  each cut on a bar line, and a section change sung just off its bar goes to the bar",
          all(any(abs(e - b) <= 1 / 48 for b in fake_an["bars"]) for e in ends[:-1]) and any(abs(e - 22.25) < 1 / 48 for e in ends), ends)
    try:
        analysis.cuts_for(fake_an, 2)
        check("  too few shots for the song is said, not stretched", False)
    except ValueError:
        check("  too few shots for the song is said, not stretched", True)
    cuts = analysis.plan_cuts(an, 4)
    check("  the cuts cover the song exactly, end to end",
          cuts[0]["start"] == 0 and cuts[-1]["end"] == an["duration"]
          and all(abs(a["end"] - b["start"]) < 1e-6 for a, b in zip(cuts, cuts[1:])), cuts)
    check("  every cut sits on a frame", all(abs(c["end"] * 24 - round(c["end"] * 24)) < 0.01 or c["end"] == an["duration"]
                                            for c in cuts), [c["end"] for c in cuts])
    coro = next(s["start"] for s in an["sections"] if s["name"] == "Coro")
    check("  and one falls where the chorus starts", any(abs(c["start"] - coro) < 1.0 / 24 + 1e-6 for c in cuts),
          (coro, [c["start"] for c in cuts]))
    check("  each shot knows the words sung in it, and the outro that nobody sings",
          any(c["words"].startswith("Dale") for c in cuts) and not cuts[-1]["sung"], [(c["start"], c["words"]) for c in cuts])
    offline = analysis.analyze(song_file, lyr, "es", None, tmp / "an2")
    check("  without the audio server a song still gets its beats, not its words",
          not offline["aligned"] and offline["beats"] and analysis.plan_cuts(offline, 4)[-1]["end"] == offline["duration"])
    mgr.audio = FakeAudio()
    song_item = Projects.find(projects.load(JUANA, p["id"]), sid)[2]
    real_take = next(t for t in song_item["takes"] if t["file"].endswith(".mp3") and (projects.dir(JUANA, p["id"]) / t["file"]).is_file())
    ja = store2.add(owner=JUANA, owner_name="Juana", kind="analyze", model="audio.cpp",
                    params={"take": real_take["id"]}, project=p["id"], target=sid)
    mgr.wake()
    deadline = time.time() + 120
    while time.time() < deadline and store2.get(ja["id"])["state"] not in ("done", "failed"):
        time.sleep(0.2)
    got = next(t for t in Projects.find(projects.load(JUANA, p["id"]), sid)[2]["takes"] if t["id"] == real_take["id"])
    check("  a listening job runs in the queue and leaves its result on the version",
          store2.get(ja["id"])["state"] == "done" and (projects.dir(JUANA, p["id"]) / got["analysis"]["file"]).is_file(),
          (store2.get(ja["id"]), got.get("analysis")))
    check("  without starting the generator for it", fake.seen[-1].get("model_type") != "audio.cpp")

    print("\na song written out: guitar as notes and tab, piano on two staves")
    from studio import score
    import xml.etree.ElementTree as ET

    def strum(t0, pitches, inst, length=0.24, idx=[0]):
        out = []
        for pch in pitches:
            idx[0] += 1
            out += [{"type": "start", "pitch": pch, "start_time": round(t0, 2), "index": idx[0], "instrument": inst},
                    {"type": "end", "end_time": round(t0 + length, 2), "start_event_index": idx[0]}]
        return out
    # 118 bpm, the first downbeat at 0.31 s: C, G, Am, F a bar each, strummed
    # in eighths, and a picked line on the electric over the last two bars.
    beat_s, t0 = 60 / 118, 0.31
    shapes = [[48, 52, 55, 60, 64], [43, 47, 50, 55, 59, 67], [45, 52, 57, 60, 64], [41, 48, 53, 57, 60, 65]]
    evs = []
    for bar in range(8):
        for k in range(8):
            evs += strum(t0 + (bar * 4 + k / 2) * beat_s, shapes[bar % 4], "acoustic_guitar")
    for k, pch in enumerate([64, 67, 69, 72, 71, 69, 67, 64] * 2):
        evs += strum(t0 + (24 + k / 2) * beat_s, [pch], "clean_electric_guitar", length=0.2)
    for k in range(16):
        evs += strum(t0 + k * 2 * beat_s, [36 + (k % 4) * 2, 72 + k % 3], "acoustic_piano", length=0.9)
    evs += strum(5.0, [30], "acoustic_guitar")                   # below the low E: out of the tab
    evs += strum(7.0, [60], "distorted_electric_guitar")         # heard once: noise, not a part
    notes = [n for n in score.notes_from_events(evs) if n["instrument"] in score.INSTRUMENTS]
    b, first = score.fit_grid(notes, 123.0)
    check("  the tempo comes from the notes, not the beat tracker's guess",
          abs(60 / b - 118) < 0.2, 60 / b)
    check("  and the bar starts where the chords change",
          abs(((first - t0) / (4 * b) + 0.5) % 1 - 0.5) * 4 * b < 0.03, (first, t0))
    check("  chords are named from their notes, the bass first",
          score.chord_name(shapes[0])[1:] == ("major", "") and score.chord_name(shapes[2])[1:] == ("minor", "m")
          and score.chord_name([43, 47, 50, 55, 59, 67])[0] == 7 and score.chord_name([60]) is None
          and score.chord_name([52, 59, 64]) == (4, "power", "5"))
    xml, meta = score.build(evs, 123.0, "Prueba <1>")
    check("  one part per instrument heard, and a stray note is not a part",
          [t["id"] for t in meta["tracks"]] == ["acoustic_guitar", "clean_electric_guitar", "acoustic_piano"], meta["tracks"])
    root = ET.fromstring(xml.split("\n", 2)[2])
    sums = []
    for part in root.findall("part"):
        for m in part.findall("measure"):
            pos = {}
            for el in m.findall("note"):
                if el.find("chord") is None:
                    st = el.findtext("staff") or "1"
                    pos[st] = pos.get(st, 0) + int(el.findtext("duration"))
            sums += list(pos.values())
    check("  every bar of every staff adds up to a bar", sums and set(sums) == {16}, Counter(sums))
    check("  the title is escaped", root.findtext("work/work-title") == "Prueba <1>")
    frets_ok = True
    for n in root.iter("note"):
        tech = n.find("notations/technical")
        if tech is None:
            continue
        stp, alt, octv = n.findtext("pitch/step"), int(n.findtext("pitch/alter") or 0), int(n.findtext("pitch/octave"))
        midi = (octv + 1) * 12 + "C D EF G A B".index(stp) + alt
        string, fret = int(tech.findtext("string")), int(tech.findtext("fret"))
        frets_ok &= score.TUNING[6 - string] + fret == midi and 0 <= fret <= score.MAX_FRET
    check("  every tab number is the note it stands for, on a real string", frets_ok)
    first_c = [n for n in root.find("part").iter("note") if n.find("notations/technical") is not None][:5]
    check("  an open chord is fingered open, not up the neck",
          max(int(n.findtext("notations/technical/fret")) for n in first_c) <= 3,
          [n.findtext("notations/technical/fret") for n in first_c])
    check("  and the harmony is written above it", [h.findtext("root/root-step") for h in root.find("part").iter("harmony")][:4]
          == ["C", "G", "A", "F"], [h.findtext("root/root-step") for h in root.find("part").iter("harmony")][:6])
    check("  the page gets a whole-number tempo and the exact one to follow the song by",
          meta["score_tempo"] == 118 and abs(meta["tempo"] - 118) < 0.2 and meta["start"] <= t0 + 0.03)

    class FakeScoreAudio:
        url = "http://audio"

        def __init__(self):
            self.asked = None

        def separate_stems(self, song, work):
            out = {}
            for k in ("vocals", "drums", "bass", "other"):
                out[k] = work / f"stem-{k}.wav"
                subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=2",
                                str(out[k])], check=True)
            return out

        def notes(self, song, work, instruments):
            self.asked = instruments
            return evs, b"MThd fake"

        def wait_idle(self, timeout=60.0):
            pass
    mgr.audio = FakeScoreAudio()
    js = store2.add(owner=JUANA, owner_name="Juana", kind="score", model="audio.cpp",
                    params={"take": real_take["id"]}, project=p["id"], target=sid)
    mgr.wake()
    deadline = time.time() + 120
    while time.time() < deadline and store2.get(js["id"])["state"] not in ("done", "failed"):
        time.sleep(0.2)
    got = next(t for t in Projects.find(projects.load(JUANA, p["id"]), sid)[2]["takes"] if t["id"] == real_take["id"])
    sc = got.get("score") or {}
    base_dir = projects.dir(JUANA, p["id"])
    check("  a score job files the score, the notes and two play-along tracks on the version",
          store2.get(js["id"])["state"] == "done" and sc.get("state") == "done"
          and all((base_dir / sc[k]).is_file() for k in ("file", "midi", "minus", "part"))
          and sc["dir"].startswith(f"takes/{sid}/"), (store2.get(js["id"]), sc))
    check("  asking the notes of guitars and piano only", set(mgr.audio.asked) == set(score.INSTRUMENTS), mgr.audio.asked)
    mgr.audio = None
    jf = store2.add(owner=JUANA, owner_name="Juana", kind="score", model="audio.cpp",
                    params={"take": real_take["id"]}, project=p["id"], target=sid)
    mgr.wake()
    while time.time() < deadline and store2.get(jf["id"])["state"] not in ("done", "failed"):
        time.sleep(0.2)
    got = next(t for t in Projects.find(projects.load(JUANA, p["id"]), sid)[2]["takes"] if t["id"] == real_take["id"])
    check("  without the audio unit it fails, and says so on the version",
          store2.get(jf["id"])["state"] == "failed" and got["score"]["state"] == "failed", got.get("score"))
    projects.set_take_field(JUANA, p["id"], sid, real_take["id"], "score", sc)
    extra = projects.append(JUANA, p["id"], "audio", [{"kind": "song", "title": "otra"}])[0]
    xt = projects.add_take(JUANA, p["id"], extra["id"], {"file": sc["minus"], "kind": "song"})
    xdir = f"takes/{extra['id']}/{xt['id']}-score"
    (base_dir / xdir).mkdir(parents=True)
    (base_dir / xdir / "score.musicxml").write_text("x")
    projects.set_take_field(JUANA, p["id"], extra["id"], xt["id"], "score", {"state": "done", "dir": xdir})
    projects.delete_take(JUANA, p["id"], extra["id"], xt["id"])
    check("  deleting a version takes its scores and stems with it", not (base_dir / xdir).exists())
    xt2 = projects.add_take(JUANA, p["id"], extra["id"], {"file": sc["minus"], "kind": "song"})
    projects.set_take_field(JUANA, p["id"], extra["id"], xt2["id"], "score", {"state": "done", "dir": "takes"})
    projects.delete_take(JUANA, p["id"], extra["id"], xt2["id"])
    check("  and never a folder that is not one version's", (base_dir / sc["file"]).is_file())
    print("\nretouching a song")
    cover = recipes.settings_for("song", {"lyrics": "[Coro]\nla", "style": "rock", "seconds": 20,
                                          "source_file": "/data/x.mp3", "strength": 0.85, "keep_voice": True})
    check("  the whole song again is ACE-Step's cover of the version, held to it",
          cover["audio_prompt_type"] == "AB" and cover["audio_guide"] == cover["audio_guide2"] == "/data/x.mp3"
          and cover["audio_scale"] == 0.85, cover)
    check("  and without keeping the voice, only the cover",
          recipes.settings_for("song", {"lyrics": "x", "source_file": "/d/x.mp3"})["audio_prompt_type"] == "A")

    class FakeRepaint(FakeAudio):
        def repaint(self, song, work, *, start, end, lyrics, style, language, seed):
            work.mkdir(parents=True, exist_ok=True)
            out = work / "repainted.wav"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(song), str(out)], check=True)
            self.asked = {"start": start, "end": end, "lyrics": lyrics}
            return out
    mgr.audio = FakeRepaint()
    before = len(Projects.find(projects.load(JUANA, p["id"]), sid)[2]["takes"])
    jr = store2.add(owner=JUANA, owner_name="Juana", kind="repaint", model="audio.cpp",
                    params={"from_take": real_take["id"], "start": 0.2, "end": 0.6, "lyrics": "[Coro]\nnueva línea",
                            "style": "rock", "language": "es", "seconds": 1}, project=p["id"], target=sid)
    mgr.wake()
    deadline = time.time() + 120
    while time.time() < deadline and store2.get(jr["id"])["state"] not in ("done", "failed"):
        time.sleep(0.2)
    after = Projects.find(projects.load(JUANA, p["id"]), sid)[2]["takes"]
    new = after[-1] if len(after) > before else {}
    check("  a repaint of a stretch lands as a new version, from the one asked for",
          store2.get(jr["id"])["state"] == "done" and new.get("from") == real_take["id"]
          and mgr.audio.asked["start"] == 0.2 and mgr.audio.asked["end"] == 0.6, (store2.get(jr["id"]), new))
    check("  kept as MP3, remembering the words it was sung with",
          new.get("file", "").endswith(".mp3") and new.get("lyrics") == "[Coro]\nnueva línea", new)
    check("  a shot cut to the music is made at least as long as its cut",
          recipes.h3_frames_at_least(7.9) / 24 >= 7.9 and recipes.h3_frames_at_least(7.9) - 17 < 7.9 * 24
          and recipes.settings_for("video_shot", {"prompt": "x", "seconds": 7.9, "exact": True})["video_length"]
          == recipes.h3_frames_at_least(7.9))
    trimmed = media.stitch([projects.dir(JUANA, p["id"]) / Projects.chosen_take(s)["file"] for s in doc["shots"]],
                           tmp / "trimmed.mp4", lengths=[0.5, None])
    frames = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                            "stream=nb_read_frames", "-of", "csv=p=0", str(trimmed)], capture_output=True, text=True).stdout.strip()
    check("  and the film reads it only up to its cut, to the frame (12 + 24)", frames == "36", frames)

    print("\na preview as a file: the song in place, cards for missing shots, a watermark")

    def loud(path, start, length):
        out = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(length), "-i", str(path),
                              "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
        line = next((l for l in out.splitlines() if "max_volume" in l), "max_volume: -91 dB")
        return float(line.split("max_volume:")[1].split("dB")[0].replace("-inf", "-91"))
    beep = tmp / "beep.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "aevalsrc='if(between(t,2,3),sin(2*PI*440*t),0)':s=48000:d=4", str(beep)], check=True)
    cut = media.follow(beep, [(0.0, 1.0), (2.0, 1.0)], tmp / "followed.wav")
    check("  each shot gets the song from where it sits: silence for 0-1 s, then the 2-3 s tone",
          abs(media.probe(cut)["seconds"] - 2.0) < 0.05 and loud(cut, 0.1, 0.8) < -60 and loud(cut, 1.1, 0.8) > -20,
          (media.probe(cut)["seconds"], loud(cut, 0.1, 0.8), loud(cut, 1.1, 0.8)))
    late = media.follow(beep, [(2.0, 1.0)], tmp / "late.wav", delay=1.0)
    check("  and a song starting later is followed from its own start", loud(late, 0.1, 0.8) < -60, loud(late, 0.1, 0.8))
    card = media.placeholder("Toma 2 · Todavía no se generó", "Un pelícano en una moto, al atardecer",
                             1.5, (160, 96), tmp / "card.mp4")
    check("  a missing shot is a still card for its length, with sound to stitch",
          abs(media.probe(card)["seconds"] - 1.5) < 0.1 and media.probe(card)["has_audio"], media.probe(card))
    mark = media.watermark("VISTA PREVIA", "Toma 1/2 · 0:00.0-0:01.0 · Versión 1/1", (160, 96), tmp / "mark.png")
    marked = media.stitch([projects.dir(JUANA, p["id"]) / Projects.chosen_take(doc["shots"][0])["file"], card],
                          tmp / "marked.mp4", marks=[mark, None])
    frame = tmp / "frame.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(marked), "-frames:v", "1", str(frame)], check=True)
    from PIL import Image
    px = Image.open(frame).convert("RGB").getpixel((8, 8))
    check("  the watermark is on the frames (the badge's colour in the corner)",
          px[0] > 150 and 90 < px[1] < 180 and px[2] < 110, px)
    check("  and the film is the shot then the card", 2.3 < media.probe(marked)["seconds"] < 2.7, media.probe(marked))

    print("\na storyboard: a frame per shot before any video")
    sb = projects.create(JUANA, "Corto", "music_video")
    check("  a project has a kind", sb["kind"] == "music_video"
          and projects.create(JUANA, "x", "nonsense")["kind"] == "free")
    projects.save(JUANA, sb["id"], {"shots": [{"prompt": "un pelícano en una moto", "seconds": 5},
                                              {"prompt": "la moto salta", "seconds": 5, "continuity": False}],
                                    "settings": {"look": "película de los 80, neón"}})
    sdoc = projects.load(JUANA, sb["id"])
    jb = store2.add(owner=JUANA, owner_name="Juana", kind="board", model=recipes.IMAGE_MODEL,
                    params={"prompt": "película de los 80. Film still: un pelícano", "size": "1344x768",
                            "shot_prompt": "la moto salta"},
                    project=sb["id"], target=sdoc["shots"][1]["id"])
    # The repaint above let the generator go; a new one says it is ready.
    with fake._lock:
        fake._out.append({"kind": "ready"})
        fake._lock.notify_all()
    mgr.wake()
    deadline = time.time() + 60
    while time.time() < deadline and store2.get(jb["id"])["state"] not in ("done", "failed"):
        time.sleep(0.2)
    shot2 = projects.load(JUANA, sb["id"])["shots"][1]
    check("  a frame lands on its shot as a storyboard frame, not as a take",
          len(shot2.get("boards") or []) == 1 and not shot2.get("takes")
          and shot2["boards"][0].get("prompt") == "la moto salta"
          and (projects.dir(JUANA, sb["id"]) / Projects.chosen_board(shot2)["file"]).is_file(), (store2.get(jb["id"]), shot2))
    check("  the image recipe draws it at the shot's shape",
          recipes.settings_for("board", {"prompt": "x", "size": recipes.BOARD_SIZE["480x832"]})["resolution"] == "768x1344")

    print("\ncharacters: a portrait and a voice, filed on the character")
    from studio.characters import Characters
    chars = Characters(tmp / "projects")
    mgr.characters = chars
    pel = chars.create(JUANA, sb["id"], {"name": "Pelícano", "look": "a brown pelican in a red helmet"})
    with fake._lock:
        fake._out.append({"kind": "ready"})
        fake._lock.notify_all()
    jp = store2.add(owner=JUANA, owner_name="Juana", kind="portrait", model=recipes.IMAGE_MODEL,
                    params={"prompt": "portrait", "size": "832x1216"}, project=sb["id"], target=pel["id"])
    mgr.wake()
    deadline = time.time() + 60
    while time.time() < deadline and store2.get(jp["id"])["state"] not in ("done", "failed"):
        time.sleep(0.2)
    pel = chars.get(pel["id"], JUANA, sb["id"])
    check("  a portrait lands on the character, and becomes its picture",
          store2.get(jp["id"])["state"] == "done" and len(pel["pictures"]) == 1 and pel["portrait"] == 0, (store2.get(jp["id"]), pel))
    webm = tmp / "voz.webm"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(tmp / "tune.wav"), "-c:a", "libopus", str(webm)], check=True)
    pel = chars.add_file(pel["id"], JUANA, sb["id"], "voz.webm", webm.read_bytes(), "voice")
    check("  a voice recorded in the page (WebM) is kept as WAV", pel["voice"].endswith(".wav")
          and media.probe(chars.file(pel["id"], JUANA, sb["id"], pel["voice"]))["seconds"] > 5, pel["voice"])
    jv = store2.add(owner=JUANA, owner_name="Juana", kind="voice", model=recipes.VOICE_MODEL,
                    params={"text": "hola", "voice_char": pel["id"], "seconds": 5}, project=sb["id"], target=pel["id"])
    mgr.wake()
    while time.time() < deadline and store2.get(jv["id"])["state"] not in ("done", "failed"):
        time.sleep(0.2)
    pel = chars.get(pel["id"], JUANA, sb["id"])
    vt = (pel.get("voice_tests") or {}).get(JUANA, "")
    check("  a voice test speaks with its own sample, kept in the character's folder, per person",
          store2.get(jv["id"])["state"] == "done" and vt.endswith(".mp3")
          and chars.file(pel["id"], JUANA, sb["id"], vt).is_file()
          and fake.seen[-1].get("audio_guide", "").endswith(pel["voice"]), (store2.get(jv["id"]), vt,
                                                                             fake.seen[-1].get("audio_guide")))
    line = projects.save(JUANA, sb["id"], {"audio": [{"kind": "voice", "title": "1", "text": "hola", "speaker": pel["id"]}]})
    line_id = next(a["id"] for a in line["audio"] if a.get("speaker") == pel["id"])
    jl = store2.add(owner=JUANA, owner_name="Juana", kind="voice", model=recipes.VOICE_MODEL,
                    params={"text": "hola", "voice_char": pel["id"], "seconds": 5}, project=sb["id"], target=line_id)
    mgr.wake()
    deadline = time.time() + 60
    while time.time() < deadline and store2.get(jl["id"])["state"] not in ("done", "failed"):
        time.sleep(0.2)
    said = Projects.find(projects.load(JUANA, sb["id"]), line_id)[2]
    check("  a line said in a character's voice is filed on the line, not taken for a voice test",
          store2.get(jl["id"])["state"] == "done" and Projects.chosen_take(said)
          and (chars.get(pel["id"], JUANA, sb["id"]).get("voice_tests") or {}).get(JUANA) == vt,
          (store2.get(jl["id"]), said.get("takes")))
    try:
        chars.add_file(pel["id"], JUANA, sb["id"], "x.html", b"<script>alert(1)</script>", "picture")
        check("  a 'picture' that is not one is refused", False)
    except ProjectError:
        check("  a 'picture' that is not one is refused", True)
    png = tmp / "p.jpg"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=32x32", "-frames:v", "1", str(png)], check=True)
    pel = chars.add_file(pel["id"], JUANA, sb["id"], "p.jpg", png.read_bytes(), "picture")
    check("  and a real one is kept as PNG, whatever it came as", pel["pictures"][-1].endswith(".png"), pel["pictures"])

    print("\nhow long the queue says a shot takes grows with the shot")
    five = store2.seconds_for_job({"kind": "video_shot", "params": {"seconds": 5}})
    twenty = store2.seconds_for_job({"kind": "video_shot", "params": {"seconds": 20}})
    check("  a 20-second shot is four 5-second ones", abs(twenty - 4 * five) < 1e-6 and five > 0, (five, twenty))
    check("  measured from what this card did", store2.rate("video_shot") < 60, store2.rate("video_shot"))
    check("  and a default before it has done any", Store(tmp / "empty.db").rate("video_shot") == 360)
    print("\na paused Studio gives the card back at once")
    check("  a worker is up, idle, an hour from its idle stop", mgr.worker is not None, mgr.status())
    mgr.paused, mgr.pause_reason = True, "programmer"
    mgr.wake()
    deadline = time.time() + 15
    while time.time() < deadline and mgr.worker is not None:
        time.sleep(0.2)
    check("  paused, it is stopped now -- the Programmer's local model waits for that card",
          mgr.worker is None and not mgr.status()["worker"], mgr.status())
    mgr.paused = False

    print("\nsomebody else on the card: a job waits instead of failing")
    mgr.card_used = lambda: 10692
    jw = store2.add(owner=JUANA, owner_name="Juana", kind="image", model=recipes.IMAGE_MODEL,
                    params={"prompt": "un faro", "size": "1024x1024"})
    mgr.wake()
    time.sleep(2)
    check("  with 10.7 GB held by something else and no worker of its own, the job is not started",
          store2.get(jw["id"])["state"] == "queued" and mgr.status()["card_busy_mb"] == 10692,
          (store2.get(jw["id"])["state"], mgr.status()))
    with fake._lock:
        fake._out.append({"kind": "ready"})
        fake._lock.notify_all()
    mgr.card_used = lambda: 220
    mgr.wake()
    deadline = time.time() + 30
    while time.time() < deadline and store2.get(jw["id"])["state"] not in ("done", "failed"):
        time.sleep(0.2)
    check("  and once the card is free it runs, and the page stops saying so",
          store2.get(jw["id"])["state"] == "done" and mgr.status()["card_busy_mb"] == 0,
          (store2.get(jw["id"]), mgr.status()))
    mgr.stop()

    print("\na pause now stops the job on the card and puts it back in the queue")

    class SlowWorker(FakeWorker):
        """Works until it is told to stop, the way a long video shot does."""

        def send(self, **msg):
            with self._lock:
                if "run" in msg:
                    self.running = msg["run"]
                    self._out.append({"kind": "progress", "id": msg["run"], "progress": 0.3, "phase": "Denoising"})
                elif msg.get("cancel") == getattr(self, "running", None):
                    self._out.append({"kind": "done", "id": msg["cancel"], "success": False, "files": [],
                                      "errors": ["cancelled"]})
                self._lock.notify_all()

    slow = SlowWorker(tmp / "scratch")
    slow._out.append({"kind": "ready"})
    mgr3 = Manager(store2, projects, tmp / "scratch", tmp / "logs", idle_s=3600,
                   notify=notified.append, worker_factory=lambda: slow)
    mgr3.card_used = lambda: 0
    mgr3.start()
    jl = store2.add(owner=JUANA, owner_name="Juana", kind="image", model=recipes.IMAGE_MODEL,
                    params={"prompt": "una tormenta", "size": "1024x1024"})
    mgr3.wake()
    deadline = time.time() + 15
    while time.time() < deadline and store2.get(jl["id"])["state"] != "running":
        time.sleep(0.1)
    heard = len(notified)
    stopped = mgr3.pause(now=True)
    deadline = time.time() + 15
    while time.time() < deadline and (store2.get(jl["id"])["state"] != "queued" or mgr3.worker is not None):
        time.sleep(0.1)
    back = store2.get(jl["id"])
    check("  the running job is stopped and queued again, from the start, and the card is let go",
          stopped == jl["id"] and back["state"] == "queued" and back["progress"] == 0 and not back["started"]
          and mgr3.worker is None, (stopped, back, mgr3.status()))
    check("  nobody is told it failed", len(notified) == heard, notified[heard:])
    time.sleep(1)
    check("  and it waits while paused", store2.get(jl["id"])["state"] == "queued")
    check("  a pause that is not now leaves the job alone", mgr3.pause() is None)
    mgr3.stop()
    store2.cancel(jl["id"])

print("\na project's words under version control")
import json  # noqa: E402
from studio.history import History
hp = Projects(tmp / "hist")
hc = Characters(tmp / "hist")
hist = History(hp, hc)
hdoc = hp.create(JUANA, "Historia", "music_video")
hpid = hdoc["id"]
r1 = hist.record(JUANA, hpid, JUANA, "Juana")
check("  a new project's first revision", r1 and hist.log(JUANA, hpid)["revisions"][0]["subject"] == "Proyecto creado",
      hist.log(JUANA, hpid))
hp.save(JUANA, hpid, {"shots": [{"prompt": "uno", "seconds": 5}]})
r2 = hist.record(JUANA, hpid, JUANA, "Juana")
s1 = hp.load(JUANA, hpid)["shots"][0]["id"]
hp.save(JUANA, hpid, {"shots": [{"id": s1, "prompt": "uno bis"}]})
r2b = hist.record(JUANA, hpid, JUANA, "Juana")
revs = hist.log(JUANA, hpid)["revisions"]
check("  saves close together by the same person are one revision",
      len(revs) == 2 and revs[0]["subject"].startswith("Toma 1") and r2b != r2, revs)
hdir = hp.dir(JUANA, hpid) / ".history"
item_file = (hdir / "items" / f"{s1}.json").read_text()
check("  a plain git repository, one readable file per item, words only",
      (hdir / ".git").is_dir() and '"uno bis"' in item_file and "takes" not in item_file
      and subprocess.run(["git", "-C", str(hdir), "log", "--oneline"], capture_output=True).returncode == 0)
hp.save(JUANA, hpid, {"shots": [{"id": s1, "prompt": "uno bis y algo"}]})
hist.record(JUANA, hpid, JUANA, "Juana")
hp.save(JUANA, hpid, {"shots": [{"id": s1, "prompt": "uno bis"}]})
hist.record(JUANA, hpid, JUANA, "Juana")
now_revs = hist.log(JUANA, hpid)["revisions"]
check("  typed and undone within a revision leaves it as it was",
      [(r["subject"], r["kind"]) for r in now_revs] == [(r["subject"], r["kind"]) for r in revs]
      and json.loads((hdir / "items" / f"{s1}.json").read_text())["prompt"] == "uno bis", now_revs)
hp.save(JUANA, hpid, {"shots": [{"id": s1, "prompt": "uno"}]})
hist.record(JUANA, hpid, JUANA, "Juana")
hp.save(JUANA, hpid, {"shots": [{"id": s1, "prompt": "uno bis"}]})
hist.record(JUANA, hpid, JUANA, "Juana")
revs = hist.log(JUANA, hpid)["revisions"]
tagged = revs[0]["rev"]
hist.tag(JUANA, hpid, tagged, "primera versión", JUANA, "Juana")
hp.save(JUANA, hpid, {"shots": [{"id": s1, "prompt": "tres"}]})
r3 = hist.record(JUANA, hpid, JUANA, "Juana", via="Alfred")
revs = hist.log(JUANA, hpid)["revisions"]
check("  a change by the person's assistant is its own revision, and says so",
      len(revs) == 3 and revs[0]["author"] == "Juana (Alfred)", revs[:1])
hp.add_take(JUANA, hpid, s1, {"file": "takes/x.mp4", "seconds": 5})
check("  a version made by the card is not a revision", hist.record(JUANA, hpid, JUANA, "Juana") is None)
hp.save(JUANA, hpid, {"shots": [{"id": s1, "prompt": "cuatro"}, {"prompt": "dos", "seconds": 5}]})
r4 = hist.record(JUANA, hpid, JUANA, "Juana")
s2 = hp.load(JUANA, hpid)["shots"][1]["id"]
shown = hist.show(JUANA, hpid, r4)["changes"]
check("  a revision shows what changed, before and after, by name",
      {(c["label"], c.get("field_label"), c.get("before"), c.get("after")) for c in shown if c["on"] == "item" and c.get("field")}
      == {("Toma 1", "descripción", "tres", "cuatro")} and any(c.get("change") == "added" and c["label"] == "Toma 2" for c in shown), shown)
out = hist.revert(JUANA, hpid, r3, JUANA, "Juana")
check("  reverting a change made over since is reported, not forced",
      out["conflicts"] and hp.load(JUANA, hpid)["shots"][0]["prompt"] == "cuatro", out)
out = hist.revert(JUANA, hpid, r4, JUANA, "Juana")
now = hp.load(JUANA, hpid)
check("  reverting a revision undoes exactly it: the words back, the added shot out",
      not out["conflicts"] and [x["prompt"] for x in now["shots"]] == ["tres"] and now["shots"][0]["takes"], (out, now["shots"]))
check("  and is a revision of its own", hist.log(JUANA, hpid)["revisions"][0]["kind"] == "revert")
hp.add_take(JUANA, hpid, s1, {"file": "takes/y.mp4", "seconds": 5})
back = hist.restore(JUANA, hpid, r2b, JUANA, "Juana")
now = hp.load(JUANA, hpid)
check("  going back to a revision: its words, its shots -- and the versions made since stay",
      [x["prompt"] for x in now["shots"]] == ["uno bis"] and len(now["shots"][0]["takes"]) == 2, now["shots"])
(hp.dir(JUANA, hpid) / "takes").mkdir(exist_ok=True)
(hp.dir(JUANA, hpid) / "takes" / "z.mp4").write_bytes(b"x")
hp.save(JUANA, hpid, {"shots": [{"id": s1, "prompt": "uno bis"}, {"prompt": "con versión", "seconds": 5}]})
s3 = hp.load(JUANA, hpid)["shots"][1]["id"]
hp.add_take(JUANA, hpid, s3, {"file": "takes/z.mp4", "seconds": 5})
with_s3 = hist.record(JUANA, hpid, JUANA, "Juana", kind="checkpoint")
hp.save(JUANA, hpid, {"shots": [{"id": s1, "prompt": "uno bis"}]})
hist.record(JUANA, hpid, JUANA, "Juana", kind="checkpoint")
hist.restore(JUANA, hpid, with_s3, JUANA, "Juana")
back_s3 = next((x for x in hp.load(JUANA, hpid)["shots"] if x["id"] == s3), None)
check("  a shot deleted on the page comes back with its versions", back_s3 and back_s3["takes"]
      and back_s3["takes"][0]["file"] == "takes/z.mp4", back_s3)
tags = hist.log(JUANA, hpid)["tags"]
check("  tags keep a name, accents and all, on a revision", [t["name"] for t in tags] == ["primera versión"]
      and tags[0]["rev"] == tagged, tags)
hist.untag(JUANA, hpid, tags[0]["ref"])
check("  and can be taken off", hist.log(JUANA, hpid)["tags"] == [])
try:
    hist.revert(JUANA, hpid, hist.log(JUANA, hpid)["revisions"][-1]["rev"], JUANA, "Juana")
    check("  the first revision cannot be undone (it would take everything out)", False)
except ProjectError:
    check("  the first revision cannot be undone (it would take everything out)", True)
try:
    hist.show(JUANA, hpid, "HEAD; rm -rf /")
    check("  a revision is a hash, nothing else", False)
except ProjectError:
    check("  a revision is a hash, nothing else", True)
old = hp.create(JUANA, "De antes", "free")
hp.save(JUANA, old["id"], {"images": [{"prompt": "un faro"}]})
check("  a project from before the history gets its first revision, once",
      hist.begin_all() >= 1 and hist.begin_all() == 0
      and [r["subject"] for r in hist.log(JUANA, old["id"])["revisions"]] == ["Historial iniciado"])
cp = hp.duplicate(JUANA, hpid)
check("  a copy keeps the history it was copied from",
      len(hist.log(JUANA, cp["id"])["revisions"]) == len(hist.log(JUANA, hpid)["revisions"]))
hch = hc.create(JUANA, hpid, {"name": "Bruma", "look": "un pelícano"})
hist.record(JUANA, hpid, JUANA, "Juana", kind="checkpoint")
hc.update(hch["id"], JUANA, hpid, {"look": "un pelícano con casco"})
rc = hist.record(JUANA, hpid, JUANA, "Juana", kind="checkpoint")
hist.revert(JUANA, hpid, rc, JUANA, "Juana")
check("  a character's words are kept and reverted too",
      hc.get(hch["id"], JUANA, hpid)["look"] == "un pelícano", hc.get(hch["id"], JUANA, hpid))

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
c.post("/api/admin/pause", json={"reason": "update"}, headers=h(MORA, "Mora", admin=True))
st_q = c.get("/api/queue", headers=h(JUANA, "Juana")).json()["status"]
check("  a pause says why: an update is not a parent's decision", st_q["paused"] and st_q["pause_reason"] == "update", st_q)
c.post("/api/admin/pause", json={"reason": "programmer"}, headers=h(MORA, "Mora", admin=True))
check("  and a card lent to the Programmer says so",
      c.get("/api/queue", headers=h(JUANA, "Juana")).json()["status"]["pause_reason"] == "programmer")
c.post("/api/admin/pause", json={}, headers=h(MORA, "Mora", admin=True))
check("  and a parent's pause has no reason to show",
      c.get("/api/queue", headers=h(JUANA, "Juana")).json()["status"]["pause_reason"] == "")
c.post("/api/admin/resume", json={}, headers=h(MORA, "Mora", admin=True))
check("  and a child cannot pause the card", c.post("/api/admin/pause", json={}, headers=h(JUANA, "Juana")).status_code == 403)
check("  Juana cannot open Tomi's projects",
      c.get(f"/api/projects/{pj['id']}", headers=h(TOMI, "Tomi")).status_code == 404)

song_id = doc["shots"][0]["id"]
c.put(f"/api/projects/{pj['id']}", json={"settings": {"soundtrack": song_id}}, headers=h(JUANA, "Juana"))
check("  a project remembers the song its video is for",
      c.get(f"/api/projects/{pj['id']}", headers=h(JUANA, "Juana")).json()["settings"].get("soundtrack") == song_id)
c.put(f"/api/projects/{pj['id']}", json={"settings": {"soundtrack": "../x"}}, headers=h(JUANA, "Juana"))
check("  and only as an item id",
      c.get(f"/api/projects/{pj['id']}", headers=h(JUANA, "Juana")).json()["settings"].get("soundtrack") == "")

live = [j for j in c.get("/api/queue", headers=h(JUANA, "Juana")).json()["queued"] if j["mine"]][0]["id"]
pv = tmp / "live.jpg"
pv.write_bytes(b"\xff\xd8 frame")
A.manager.previews[live] = pv
mine = [j for j in c.get("/api/queue", headers=h(JUANA, "Juana")).json()["queued"] if j["id"] == live][0]
check("  the owner is told a job has a picture so far", mine.get("preview") is True, mine)
r = c.get(f"/api/jobs/{live}/preview", headers=h(JUANA, "Juana"))
check("  and can see it, never cached", r.status_code == 200 and r.content == pv.read_bytes()
      and r.headers.get("cache-control") == "no-store", (r.status_code, r.headers.get("cache-control")))
check("  nobody else can -- not even a parent",
      c.get(f"/api/jobs/{live}/preview", headers=h(TOMI, "Tomi")).status_code == 404
      and c.get(f"/api/jobs/{live}/preview", headers=h(MORA, "Mora", admin=True)).status_code == 404)
theirs = [j for j in c.get("/api/queue", headers=h(TOMI, "Tomi")).json()["queued"] if j["id"] == live][0]
check("  nor learns that one exists", "preview" not in theirs, theirs)
A.manager.previews.clear()

im_rel = "takes/pic1.png"
(A.projects.dir(JUANA, pj["id"]) / "takes").mkdir(exist_ok=True)
(A.projects.dir(JUANA, pj["id"]) / im_rel).write_bytes(b"\x89PNG picture")
r1 = c.post(f"/api/projects/{pj['id']}/references", json={"file": im_rel, "name": "gato"}, headers=h(JUANA, "Juana")).json()
r2 = c.post(f"/api/projects/{pj['id']}/references", json={"file": im_rel}, headers=h(JUANA, "Juana")).json()
ups = c.get(f"/api/projects/{pj['id']}", headers=h(JUANA, "Juana")).json()["uploads"]
check("  a picture made here becomes a reference, once however often it is asked",
      r1.get("kind") == "reference" and r1["file"] == r2["file"] and r1["file"].startswith("uploads/")
      and [u["source"] for u in ups if u.get("source")] == [im_rel], (r1, r2, ups))
check("  as a copy: the reference goes, the picture stays",
      c.delete(f"/api/projects/{pj['id']}/{r1['file'].replace('uploads/', 'uploads/', 1)}", headers=h(JUANA, "Juana")).status_code == 200
      and (A.projects.dir(JUANA, pj["id"]) / im_rel).is_file())
check("  and only a picture made here",
      c.post(f"/api/projects/{pj['id']}/references", json={"file": "project.json"}, headers=h(JUANA, "Juana")).status_code == 400
      and c.post(f"/api/projects/{pj['id']}/references", json={"file": "takes/../../x.png"}, headers=h(JUANA, "Juana")).status_code == 400
      and c.post(f"/api/projects/{pj['id']}/references", json={"file": "takes/none.png"}, headers=h(JUANA, "Juana")).status_code == 404)
check("  and nobody else's",
      c.post(f"/api/projects/{pj['id']}/references", json={"file": im_rel}, headers=h(TOMI, "Tomi")).status_code == 404)

sng = c.put(f"/api/projects/{pj['id']}", json={"audio": [{"kind": "song", "lyrics": "[Coro]\nla la"}]},
            headers=h(JUANA, "Juana")).json()["audio"][0]
check("  a song with no version cannot be listened to",
      c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/analyze", json={}, headers=h(JUANA, "Juana")).status_code == 404)
A.projects.add_take(JUANA, pj["id"], sng["id"], {"file": "takes/s.mp3", "kind": "song"})
r1 = c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/analyze", json={}, headers=h(JUANA, "Juana")).json()
r2 = c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/analyze", json={}, headers=h(JUANA, "Juana")).json()
check("  asking to listen queues one job, however often it is asked",
      r1.get("job") and r2.get("job") and r1["job"]["id"] == r2["job"]["id"], (r1, r2))
check("  and the plan waits for it",
      c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/cuts", json={"shot_seconds": 8},
             headers=h(JUANA, "Juana")).status_code == 409)
sc0 = c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/score", json={}, headers=h(JUANA, "Juana")).json()
sc1 = c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/score", json={"start": True}, headers=h(JUANA, "Juana")).json()
sc2 = c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/score", json={"start": True}, headers=h(JUANA, "Juana")).json()
check("  a song's scores are only made when asked, and asked twice they are one job",
      sc0.get("none") and sc1.get("job") and sc1["job"]["id"] == sc2["job"]["id"] and sc1["job"]["kind"] == "score", (sc0, sc1, sc2))
check("  nobody else can ask for them",
      c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/score", json={"start": True}, headers=h(TOMI, "Tomi")).status_code == 404)
A.manager.cancel(sc1["job"]["id"])
stake = sc1["take"]
sdir = f"takes/{sng['id']}/{stake}-score"
(A.projects.dir(JUANA, pj["id"]) / sdir).mkdir(parents=True, exist_ok=True)
(A.projects.dir(JUANA, pj["id"]) / sdir / "score.musicxml").write_text("<score-partwise/>")
A.projects.set_take_field(JUANA, pj["id"], sng["id"], stake, "score",
                          {"state": "done", "dir": sdir, "file": f"{sdir}/score.musicxml", "tempo": 120.0})
sc3 = c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/score", json={"start": True}, headers=h(JUANA, "Juana")).json()
check("  once written they are served, not made again", sc3.get("score", {}).get("file") == f"{sdir}/score.musicxml", sc3)
A.projects.set_take_field(JUANA, pj["id"], sng["id"], stake, "score", {"state": "failed", "error": "boom"})
check("  a failure is reported, not retried on its own",
      c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/score", json={"start": True}, headers=h(JUANA, "Juana")).json().get("failed") == "boom")
A.projects.set_take_field(JUANA, pj["id"], sng["id"], stake, "score",
                          {"state": "done", "dir": sdir, "file": f"{sdir}/score.musicxml"})
r = c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/rework",
           json={"lyrics": "[Coro]\nle le", "start": 1, "end": 3}, headers=h(JUANA, "Juana")).json()
check("  a stretch to redo queues a repaint", r["queued"][0]["kind"] == "repaint", r)
r = c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/rework",
           json={"lyrics": "[Coro]\nlo lo", "strength": 0.7, "keep_voice": True}, headers=h(JUANA, "Juana")).json()
check("  and no stretch, a cover of the whole song", r["queued"][0]["kind"] == "song", r)
sng_now = next(a for a in c.get(f"/api/projects/{pj['id']}", headers=h(JUANA, "Juana")).json()["audio"] if a["id"] == sng["id"])
check("  the lyrics sent become the song's", sng_now["lyrics"] == "[Coro]\nlo lo", sng_now.get("lyrics"))
check("  a stretch outside the song is refused",
      c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/rework", json={"start": 50, "end": 900},
             headers=h(JUANA, "Juana")).status_code == 400)
vp = c.post("/api/projects", json={"name": "Vista"}, headers=h(JUANA, "Juana")).json()
c.put(f"/api/projects/{vp['id']}", json={"shots": [{"prompt": "uno", "seconds": 5}, {"prompt": "dos", "seconds": 5}]},
      headers=h(JUANA, "Juana"))
vdoc = c.get(f"/api/projects/{vp['id']}", headers=h(JUANA, "Juana")).json()
clip = A.projects.dir(JUANA, vp["id"]) / "takes" / "one.mp4"
clip.parent.mkdir(parents=True, exist_ok=True)
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x96:rate=24", "-f", "lavfi",
                "-i", "sine=frequency=440", "-t", "2", "-shortest", "-pix_fmt", "yuv420p", str(clip)], check=True)
A.projects.add_take(JUANA, vp["id"], vdoc["shots"][0]["id"], {"file": "takes/one.mp4", "seconds": 2.0})
r = c.post(f"/api/projects/{vp['id']}/render", json={"preview": True, "labels": {"preview": "Vista previa", "shot": "Toma",
                                                                                    "missing": "Todavía no", "version": "Versión"}},
           headers=h(JUANA, "Juana")).json()
deadline = time.time() + 180
while time.time() < deadline:
    st = c.get(f"/api/projects/{vp['id']}", headers=h(JUANA, "Juana")).json()
    if (st.get("render") or {}).get("state") != "running":
        break
    time.sleep(0.5)
rend = (st.get("renders") or [{}])[-1]
check("  a preview render is the whole video: the made shot and a card for the missing one",
      st["render"]["state"] == "done" and rend.get("preview") is True
      and abs(rend.get("seconds", 0) - (2.0 + 124 / 24)) < 0.3, (st.get("render"), rend))
check("  encoded as a draft: H.264, fast, playable everywhere",
      media.probe(A.projects.dir(JUANA, vp["id"]) / rend["file"])["codec"] == "h264")
check("  and it leaves nothing of its own behind but the film",
      sorted(x.name for x in (A.projects.dir(JUANA, vp["id"]) / "renders").iterdir()) == [rend["file"].split("/")[-1]],
      list((A.projects.dir(JUANA, vp["id"]) / "renders").iterdir()))
check("  a film or preview can be deleted, from the list and the disk",
      c.delete(f"/api/projects/{vp['id']}/{rend['file']}", headers=h(JUANA, "Juana")).status_code == 200
      and not (A.projects.dir(JUANA, vp["id"]) / rend["file"]).exists()
      and not c.get(f"/api/projects/{vp['id']}", headers=h(JUANA, "Juana")).json().get("renders"))
check("  but only a film", c.delete(f"/api/projects/{vp['id']}/renders/..%2Fproject.json", headers=h(JUANA, "Juana")).status_code == 404)
print("\n  the storyboard, through the API")
sbp = c.post("/api/projects", json={"name": "Clip", "kind": "music_video"}, headers=h(JUANA, "Juana")).json()
check("  a project is made with its kind, and listed with it",
      sbp["kind"] == "music_video" and any(x["id"] == sbp["id"] and x["kind"] == "music_video"
                                           for x in c.get("/api/projects", headers=h(JUANA, "Juana")).json()["projects"]))
c.put(f"/api/projects/{sbp['id']}", json={"settings": {"look": "neón, noche"},
                                          "shots": [{"prompt": "uno", "seconds": 5}, {"prompt": "dos", "seconds": 5, "continuity": False},
                                                    {"prompt": "", "seconds": 5}]}, headers=h(JUANA, "Juana"))
r = c.post(f"/api/projects/{sbp['id']}/storyboard", json={}, headers=h(JUANA, "Juana")).json()
check("  asking for the storyboard queues a frame per shot with a description",
      len(r.get("queued") or []) == 2 and all(q["kind"] == "board" for q in r["queued"]), r)
job = A.store.get(r["queued"][0]["id"])
check("  and remembers the description it was drawn from", job["params"].get("shot_prompt") == "uno", job["params"])
check("  drawn with the project's look first, at the video's shape",
      job["params"]["prompt"].startswith("neón, noche. Film still: uno") and job["params"]["size"] == "1344x768", job["params"])
check("  asked again while they are being drawn, nothing is queued twice",
      c.post(f"/api/projects/{sbp['id']}/storyboard", json={}, headers=h(JUANA, "Juana")).status_code == 400)
sdoc = c.get(f"/api/projects/{sbp['id']}", headers=h(JUANA, "Juana")).json()
frame = A.projects.dir(JUANA, sbp["id"]) / "takes" / "f.png"
frame.parent.mkdir(parents=True, exist_ok=True)
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x96", "-frames:v", "1", str(frame)], check=True)
A.projects.add_board(JUANA, sbp["id"], sdoc["shots"][1]["id"], {"file": "takes/f.png"})
for jid in (q["id"] for q in r["queued"]):
    A.manager.cancel(jid)
g = c.post(f"/api/projects/{sbp['id']}/generate", json={"items": [sdoc["shots"][1]["id"]]}, headers=h(JUANA, "Juana")).json()
gp = A.store.get(g["queued"][0]["id"])["params"]
check("  a shot that starts fresh starts from its approved frame", gp.get("start_board") == "takes/f.png", gp)
check("  and its video is told the film's look, as its frame was",
      gp.get("look") == "neón, noche" and "Visual style: neón, noche." in recipes.h3_prompt(gp), gp)
A.manager.cancel(g["queued"][0]["id"])
c.put(f"/api/projects/{sbp['id']}", json={"settings": {"use_storyboard": False}}, headers=h(JUANA, "Juana"))
g = c.post(f"/api/projects/{sbp['id']}/generate", json={"items": [sdoc["shots"][1]["id"]]}, headers=h(JUANA, "Juana")).json()
check("  with the storyboard switched off for the project, it does not",
      "start_board" not in A.store.get(g["queued"][0]["id"])["params"], A.store.get(g["queued"][0]["id"])["params"])
A.manager.cancel(g["queued"][0]["id"])
c.put(f"/api/projects/{sbp['id']}", json={"settings": {"use_storyboard": True}}, headers=h(JUANA, "Juana"))
g = c.post(f"/api/projects/{sbp['id']}/generate", json={"items": [sdoc["shots"][1]["id"]]}, headers=h(JUANA, "Juana")).json()
A.projects.add_board(JUANA, sbp["id"], sdoc["shots"][1]["id"], {"file": "takes/f.png"})
c.put(f"/api/projects/{sbp['id']}", json={"shots": [dict(x, board=0) for x in sdoc["shots"]]}, headers=h(JUANA, "Juana"))
s1 = c.get(f"/api/projects/{sbp['id']}", headers=h(JUANA, "Juana")).json()["shots"][1]
check("  a page's save cannot move the chosen frame back (a newer one stays chosen)", s1["board"] == 1, s1.get("board"))
c.post(f"/api/projects/{sbp['id']}/items/{s1['id']}/board", json={"index": 0}, headers=h(JUANA, "Juana"))
check("  choosing one is its own call",
      c.get(f"/api/projects/{sbp['id']}", headers=h(JUANA, "Juana")).json()["shots"][1]["board"] == 0)
s1id = sdoc["shots"][1]["id"]
for q in [j for j in A.store.active() if j["kind"] == "board"]:
    A.manager.cancel(q["id"])
rq = c.post(f"/api/projects/{sbp['id']}/storyboard", json={"items": [s1id], "prompts": {s1id: "a brighter lighthouse at dawn"},
                                                          "refine": {"rounds": 1, "threshold": 8}}, headers=h(JUANA, "Juana")).json()
bp_ = A.store.get(rq["queued"][0]["id"])["params"]
check("  a frame can be drawn from a reviewer's prompt, the person's description left as it is",
      "Film still: a brighter lighthouse at dawn" in bp_["prompt"] and bp_["drawn_from"] == "a brighter lighthouse at dawn"
      and bp_["shot_prompt"] == sdoc["shots"][1]["prompt"].strip()
      and bp_["refine"] == {"rounds": 1, "threshold": 8, "round": 0}, bp_)
A.manager.cancel(rq["queued"][0]["id"])
rq2 = c.post(f"/api/projects/{sbp['id']}/storyboard", json={"items": [s1id]}, headers=h(JUANA, "Juana")).json()
check("  a plain redraw is still looked at when it lands", A.store.get(rq2["queued"][0]["id"])["params"].get("refine")
      == {"rounds": 0, "threshold": 7, "round": 0})
A.manager.cancel(rq2["queued"][0]["id"])
rq3 = c.post(f"/api/projects/{sbp['id']}/storyboard", json={"items": [s1id], "review": False}, headers=h(JUANA, "Juana")).json()
check("  unless asked not to be", "refine" not in A.store.get(rq3["queued"][0]["id"])["params"])
A.manager.cancel(rq3["queued"][0]["id"])
hooked = []
saved_post, saved_url = A.requests.post, A.NOTIFY_URL
A.requests.post = lambda url, json=None, **kw: hooked.append((url, json))
A.NOTIFY_URL = "https://portal.invalid/studio/api/notify"
A._notify({"id": "jb1", "kind": "board", "owner": JUANA, "project": sbp["id"], "target": s1id, "title": "",
           "params": {"refine": {"rounds": 1, "threshold": 8, "round": 0}}, "ok": True})
A._notify({"id": "jb2", "kind": "board", "owner": JUANA, "project": sbp["id"], "target": s1id, "title": "",
           "params": {}, "ok": True})
A.requests.post, A.NOTIFY_URL = saved_post, saved_url
check("  a frame drawn inside a refine loop goes back to the portal to be looked at -- only that one",
      [u for u, _ in hooked if u.endswith("/frame-review")] == ["https://portal.invalid/studio/api/frame-review"]
      and next(j for u, j in hooked if u.endswith("/frame-review"))["job"] == "jb1", hooked)
check("  one shot's description can be set on its own",
      c.post(f"/api/projects/{sbp['id']}/items/{s1id}/prompt", json={"prompt": "un faro al amanecer"},
             headers=h(JUANA, "Juana")).status_code == 200
      and next(x for x in c.get(f"/api/projects/{sbp['id']}", headers=h(JUANA, "Juana")).json()["shots"] if x["id"] == s1id)["prompt"]
      == "un faro al amanecer")
check("  never emptied, and never someone else's",
      c.post(f"/api/projects/{sbp['id']}/items/{s1id}/prompt", json={"prompt": " "}, headers=h(JUANA, "Juana")).status_code == 400
      and c.post(f"/api/projects/{sbp['id']}/items/{s1id}/prompt", json={"prompt": "x"}, headers=h(TOMI, "Tomi")).status_code == 404)
bd = Projects.chosen_board(next(x for x in c.get(f"/api/projects/{sbp['id']}", headers=h(JUANA, "Juana")).json()["shots"] if x["id"] == s1id))
rv = c.post(f"/api/projects/{sbp['id']}/items/{s1id}/boards/{bd['id']}/review",
            json={"review": {"score": 12, "ok": ["luz"], "problems": ["<b>mano</b>"] * 20, "prompt": "p", "round": 1}},
            headers=h(JUANA, "Juana")).json()
check("  a review is kept on its frame, shaped: a score out of ten, a few findings",
      rv["review"]["score"] == 10 and len(rv["review"]["problems"]) == 8 and rv["review"]["state"] == "done", rv.get("review"))
rv = c.post(f"/api/projects/{sbp['id']}/items/{s1id}/boards/{bd['id']}/review",
            json={"review": {"score": 4, "style": "no"}}, headers=h(JUANA, "Juana")).json()
check("  and whether the frame kept to the film's style", rv["review"]["style"] == "no", rv.get("review"))
rv = c.post(f"/api/projects/{sbp['id']}/items/{s1id}/boards/{bd['id']}/review",
            json={"review": {"score": 4, "style": "<b>"}}, headers=h(JUANA, "Juana")).json()
check("  as one of three words, or nothing", rv["review"]["style"] == "", rv.get("review"))
rv = c.post(f"/api/projects/{sbp['id']}/items/{s1id}/boards/{bd['id']}/review",
            json={"review": {"score": 9, "style": "yes", "own_style": "black-and-white pencil sketch " * 20}},
            headers=h(JUANA, "Juana")).json()
check("  and the style its shot asks for itself, when it asks for one",
      rv["review"]["own_style"].startswith("black-and-white pencil sketch") and len(rv["review"]["own_style"]) <= 200,
      rv.get("review"))
check("  a review without a score is refused",
      c.post(f"/api/projects/{sbp['id']}/items/{s1id}/boards/{bd['id']}/review", json={"review": {"ok": []}},
             headers=h(JUANA, "Juana")).status_code == 404)
check("  and nobody else can write one",
      c.post(f"/api/projects/{sbp['id']}/items/{s1id}/boards/{bd['id']}/review", json={"review": {"score": 5}},
             headers=h(TOMI, "Tomi")).status_code == 404)
s0 = sdoc["shots"][0]["id"]
bf = c.post(f"/api/projects/{sbp['id']}/items/{s0}/board_from", json={"file": "takes/f.png"}, headers=h(JUANA, "Juana")).json()
s0doc = next(x for x in c.get(f"/api/projects/{sbp['id']}", headers=h(JUANA, "Juana")).json()["shots"] if x["id"] == s0)
check("  a picture in the project becomes a shot's chosen frame, as a copy, with the shot's description",
      Projects.chosen_board(s0doc)["file"] == bf["file"] and bf["file"].startswith(f"takes/{s0}/frame-")
      and bf["source"] == "takes/f.png" and bf["prompt"] == s0doc["prompt"].strip()
      and (A.projects.dir(JUANA, sbp["id"]) / bf["file"]).is_file(), bf)
check("  only a picture of this project, and only on a shot",
      c.post(f"/api/projects/{sbp['id']}/items/{s0}/board_from", json={"file": "project.json"}, headers=h(JUANA, "Juana")).status_code == 400
      and c.post(f"/api/projects/{sbp['id']}/items/{s0}/board_from", json={"file": "takes/../../x.png"}, headers=h(JUANA, "Juana")).status_code == 400
      and c.post(f"/api/projects/{sbp['id']}/items/nope/board_from", json={"file": "takes/f.png"}, headers=h(JUANA, "Juana")).status_code == 404
      and c.post(f"/api/projects/{sbp['id']}/items/{s0}/board_from", json={"file": "takes/f.png"}, headers=h(TOMI, "Tomi")).status_code == 404)
clip2 = A.projects.dir(JUANA, sbp["id"]) / "takes" / "one.mp4"
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x96:rate=24", "-f", "lavfi",
                "-i", "sine=frequency=440", "-t", "2", "-shortest", "-pix_fmt", "yuv420p", str(clip2)], check=True)
A.projects.add_take(JUANA, sbp["id"], sdoc["shots"][0]["id"], {"file": "takes/one.mp4", "seconds": 2.0})
A.manager.cancel(g["queued"][0]["id"])
bo = c.post("/api/projects", json={"name": "Solo cuadros"}, headers=h(JUANA, "Juana")).json()
c.put(f"/api/projects/{bo['id']}", json={"shots": [{"prompt": "uno", "seconds": 5}]}, headers=h(JUANA, "Juana"))
bo_shot = c.get(f"/api/projects/{bo['id']}", headers=h(JUANA, "Juana")).json()["shots"][0]
(A.projects.dir(JUANA, bo["id"]) / "takes").mkdir(parents=True, exist_ok=True)
shutil.copy(frame, A.projects.dir(JUANA, bo["id"]) / "takes" / "f.png")
A.projects.add_board(JUANA, bo["id"], bo_shot["id"], {"file": "takes/f.png"})
check("  a preview of frames alone (no video yet) can be made -- an animatic",
      c.post(f"/api/projects/{bo['id']}/render", json={"preview": True}, headers=h(JUANA, "Juana")).status_code == 200)
A.render_state[f"{JUANA}/{sbp['id']}"] = {"state": "running"}
check("  one film of a project at a time",
      c.post(f"/api/projects/{sbp['id']}/render", json={"preview": True}, headers=h(JUANA, "Juana")).status_code == 409)
A.render_state.pop(f"{JUANA}/{sbp['id']}")
c.post(f"/api/projects/{sbp['id']}/render", json={"preview": True}, headers=h(JUANA, "Juana"))
deadline = time.time() + 180
while time.time() < deadline:
    st = c.get(f"/api/projects/{sbp['id']}", headers=h(JUANA, "Juana")).json()
    if (st.get("render") or {}).get("state") != "running":
        break
    time.sleep(0.5)
rend = (st.get("renders") or [{}])[-1]
check("  and the preview download holds the frame where the shot is not made yet",
      st["render"]["state"] == "done" and abs(rend.get("seconds", 0) - (2.0 + 124 / 24 + 124 / 24)) < 0.4, (st["render"], rend))
print("\n  characters, their scopes and who may do what")
cp = c.post("/api/projects", json={"name": "Serie", "kind": "short_film"}, headers=h(JUANA, "Juana")).json()
cp2 = c.post("/api/projects", json={"name": "Otra"}, headers=h(JUANA, "Juana")).json()
ch = c.post(f"/api/projects/{cp['id']}/characters", json={"name": "Pelícano", "look": "a brown pelican, red helmet",
                                                         "personality": "brave, a bit clumsy"}, headers=h(JUANA, "Juana")).json()
def names(pid, who):
    return [x["name"] for x in c.get(f"/api/projects/{pid}/characters", headers=who).json()["characters"]]
check("  a character starts in its project, and only there",
      ch["scope"] == "project" and names(cp["id"], h(JUANA, "Juana")) == ["Pelícano"] and names(cp2["id"], h(JUANA, "Juana")) == [])
check("  a character needs a name", c.post(f"/api/projects/{cp['id']}/characters", json={"look": "x"},
                                           headers=h(JUANA, "Juana")).status_code == 400)
ch = c.post(f"/api/projects/{cp['id']}/characters/{ch['id']}/widen", headers=h(JUANA, "Juana")).json()
check("  widened, it is all of Juana's projects', same id",
      ch["scope"] == "person" and names(cp2["id"], h(JUANA, "Juana")) == ["Pelícano"])
tp = c.post("/api/projects", json={"name": "De Tomi"}, headers=h(TOMI, "Tomi")).json()
check("  and still nobody else's", names(tp["id"], h(TOMI, "Tomi")) == [])
ch = c.post(f"/api/projects/{cp2['id']}/characters/{ch['id']}/widen", headers=h(JUANA, "Juana")).json()
check("  widened again, the family's: Tomi can cast it", ch["scope"] == "family" and names(tp["id"], h(TOMI, "Tomi")) == ["Pelícano"])
check("  but not change it",
      c.put(f"/api/projects/{tp['id']}/characters/{ch['id']}", json={"look": "a gull"}, headers=h(TOMI, "Tomi")).status_code == 403)
mp = c.post("/api/projects", json={"name": "De Mora"}, headers=h(MORA, "Mora", admin=True)).json()
check("  a parent can", c.put(f"/api/projects/{mp['id']}/characters/{ch['id']}", json={"look": "a brown pelican, red helmet, goggles"},
                             headers=h(MORA, "Mora", admin=True)).status_code == 200)
check("  and nothing narrows it again",
      c.post(f"/api/projects/{cp['id']}/characters/{ch['id']}/widen", headers=h(JUANA, "Juana")).status_code == 403)
c.put(f"/api/projects/{cp['id']}", json={"shots": [{"prompt": "el pelícano salta", "seconds": 5, "cast": [ch["id"]]}]},
      headers=h(JUANA, "Juana"))
r = c.post(f"/api/projects/{cp['id']}/storyboard", json={}, headers=h(JUANA, "Juana")).json()
bp = A.store.get(r["queued"][0]["id"])["params"]["prompt"]
check("  a shot's cast puts their look in its frame, as edited",
      "Characters: Pelícano: a brown pelican, red helmet, goggles" in bp, bp)
A.manager.cancel(r["queued"][0]["id"])

print("\n  drawn from pictures: the cast's own, and the film's style")
rfp = c.post("/api/projects", json={"name": "Carrera"}, headers=h(JUANA, "Juana")).json()
bru = c.post(f"/api/projects/{rfp['id']}/characters", json={"name": "Bruma", "look": "a toddler"},
             headers=h(JUANA, "Juana")).json()
gav = c.post(f"/api/projects/{rfp['id']}/characters", json={"name": "Gaviota", "look": "a grey gull"},
             headers=h(JUANA, "Juana")).json()
pic = tmp / "bruma.jpg"
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=32x32", "-frames:v", "1",
                str(pic)], check=True)
c.post(f"/api/projects/{rfp['id']}/characters/{bru['id']}/upload", files={"file": ("bruma.jpg", pic.read_bytes())},
       headers=h(JUANA, "Juana"))
ups = [c.post(f"/api/projects/{rfp['id']}/upload", files={"file": (f"s{i}.png", pic.read_bytes())},
              data={"style": "1"} if i < 2 else {}, headers=h(JUANA, "Juana")).json() for i in range(3)]
check("  a picture uploaded as the film's style is flagged as it lands", ups[0].get("style") and ups[1].get("style")
      and not ups[2].get("style"), ups)
third = c.post(f"/api/projects/{rfp['id']}/uploads/{ups[2]['file'].split('/', 1)[1]}/style", json={"on": True},
               headers=h(JUANA, "Juana"))
check("  at most two: a third is refused, to choose, not quietly left out", third.status_code == 400, third.text)
c.post(f"/api/projects/{rfp['id']}/uploads/{ups[0]['file'].split('/', 1)[1]}/style", json={"on": False},
       headers=h(JUANA, "Juana"))
ok3 = c.post(f"/api/projects/{rfp['id']}/uploads/{ups[2]['file'].split('/', 1)[1]}/style", json={"on": True},
             headers=h(JUANA, "Juana"))
check("  with one taken off, it goes on", ok3.status_code == 200 and ok3.json().get("style"), ok3.text)
rdoc = A.projects.load(JUANA, rfp["id"])
check("  and the style pictures are kept in the order they were flagged",
      Projects.style_refs(rdoc) == [ups[1]["file"], ups[2]["file"]], Projects.style_refs(rdoc))
c.put(f"/api/projects/{rfp['id']}", json={"settings": {"look": "3D cartoon"}, "shots": [
    {"prompt": "Bruma drives a red car", "seconds": 5, "cast": [bru["id"], gav["id"]]}]}, headers=h(JUANA, "Juana"))
r = c.post(f"/api/projects/{rfp['id']}/storyboard", json={}, headers=h(JUANA, "Juana")).json()
fj = A.store.get(r["queued"][0]["id"])
check("  a frame with a cast member who has a picture, or style pictures, is drawn from them, on klein",
      fj["model"] == recipes.REF_IMAGE_MODEL and fj["params"]["ref_chars"] == [bru["id"]]
      and fj["params"]["ref_files"] == [ups[1]["file"], ups[2]["file"]], fj)
rp_ = A.manager._resolve(fj)
check("  when it runs: the character's picture first, then the style ones, as files",
      len(rp_["image_refs"]) == 3 and rp_["image_refs"][0].endswith(".png") and "/characters/" in rp_["image_refs"][0]
      and all(Path(x).is_file() for x in rp_["image_refs"]), rp_["image_refs"])
check("  and the prompt says which is which: who, and the style -- not the content",
      "image 1 is Bruma" in rp_["prompt"] and "images 2 and 3 show the film's style" in rp_["prompt"]
      and "not their content" in rp_["prompt"] and "Gaviota" not in rp_["prompt"].split("Reference images:")[1], rp_["prompt"])
A.manager.cancel(fj["id"])
c.delete(f"/api/projects/{rfp['id']}/uploads/{ups[1]['file'].split('/', 1)[1]}", headers=h(JUANA, "Juana"))
rp_ = A.manager._resolve(fj)
check("  a style picture deleted since is left out, and the numbering follows what is sent",
      len(rp_["image_refs"]) == 2 and "image 2 show" in rp_["prompt"] and "images 2 and" not in rp_["prompt"], rp_["prompt"])
c.put(f"/api/projects/{rfp['id']}", json={"shots": [{**A.projects.load(JUANA, rfp["id"])["shots"][0],
                                                     "refs": [ups[0]["file"]]}]}, headers=h(JUANA, "Juana"))
r = c.post(f"/api/projects/{rfp['id']}/storyboard", json={"items": [A.projects.load(JUANA, rfp["id"])["shots"][0]["id"]]},
           headers=h(JUANA, "Juana")).json()
oj = A.store.get(r["queued"][0]["id"])
ro = A.manager._resolve(oj)
check("  a shot's own reference picture is drawn from too: after the cast, before the style",
      oj["params"]["ref_shot"] == [ups[0]["file"]] and len(ro["image_refs"]) == 3
      and "image 2 are this shot's own reference" in ro["prompt"] and "image 3 show the film's style" in ro["prompt"],
      ro["prompt"])
A.manager.cancel(oj["id"])
dsc = c.post(f"/api/projects/{rfp['id']}/uploads/{ups[0]['file'].split('/', 1)[1]}/description",
             json={"description": "a sunny racetrack"}, headers=h(JUANA, "Juana")).json()
check("  and a reference picture keeps what it shows, in words, for the Designer",
      dsc.get("description") == "a sunny racetrack"
      and next(u for u in A.projects.load(JUANA, rfp["id"])["uploads"] if u["file"] == ups[0]["file"])["description"]
      == "a sunny racetrack")
pic2 = tmp / "bruma2.jpg"
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=green:s=32x32", "-frames:v", "1",
                str(pic2)], check=True)
ch2 = c.post(f"/api/projects/{rfp['id']}/characters/{bru['id']}/upload", files={"file": ("b2.jpg", pic2.read_bytes())},
             headers=h(JUANA, "Juana")).json()
check("  a picture given to a character becomes its chosen one -- a changed photo changes what is drawn",
      len(ch2["pictures"]) == 2 and ch2["portrait"] == 1, ch2)
c.put(f"/api/projects/{rfp['id']}/characters/{bru['id']}", json={"look": "a toddler", "look_from": ch2["pictures"][1]},
      headers=h(JUANA, "Juana"))
same = c.put(f"/api/projects/{rfp['id']}/characters/{bru['id']}", json={"look": "a toddler", "name": "Bruma"},
             headers=h(JUANA, "Juana")).json()
mine = c.put(f"/api/projects/{rfp['id']}/characters/{bru['id']}", json={"look": "my own words"},
             headers=h(JUANA, "Juana")).json()
check("  a look keeps the picture it was written from until somebody changes the words",
      same.get("look_from") == ch2["pictures"][1] and "look_from" not in mine, (same, mine))
pj_ = c.post(f"/api/projects/{rfp['id']}/characters/{bru['id']}/portrait", headers=h(JUANA, "Juana")).json()
pjob = A.store.get(pj_["queued"][0]["id"])
check("  a portrait is drawn from the character's own picture, in the film's style",
      pjob["model"] == recipes.REF_IMAGE_MODEL and pjob["params"]["ref_chars"] == [bru["id"]], pjob)
A.manager.cancel(pjob["id"])
noface = c.post(f"/api/projects/{rfp['id']}/characters", json={"name": "Nadie"}, headers=h(JUANA, "Juana")).json()
c.post(f"/api/projects/{rfp['id']}/characters/{noface['id']}/upload", files={"file": ("n.jpg", pic.read_bytes())},
       headers=h(JUANA, "Juana"))
pn = c.post(f"/api/projects/{rfp['id']}/characters/{noface['id']}/portrait", headers=h(JUANA, "Juana"))
check("  a character with a picture and no words yet can still have its portrait drawn", pn.status_code == 200, pn.text)
for q in (pn.json().get("queued") or []):
    A.manager.cancel(q["id"])
check("  and nobody can send pictures in through the job door",
      not ({"image_refs", "ref_files", "ref_chars", "with_refs"} & set(A.store.get(c.post("/api/jobs", json={
          "kind": "image", "params": {"prompt": "x", "with_refs": True, "image_refs": ["/etc/passwd"],
                                      "ref_files": ["uploads/x.png"]}}, headers=h(TOMI, "Tomi")).json()["id"])["params"])))
check("  only whoever may edit a character can have its portrait drawn",
      c.post(f"/api/projects/{tp['id']}/characters/{ch['id']}/portrait", headers=h(TOMI, "Tomi")).status_code == 403)
check("  and the generic job door takes none of the kinds with their own routes",
      c.post("/api/jobs", json={"kind": "portrait", "target": ch["id"], "params": {"prompt": "x"}},
             headers=h(TOMI, "Tomi")).status_code == 400)
jj = c.post("/api/jobs", json={"kind": "image", "params": {"prompt": "x", "source_file": "/data/projects/999000333/a.mp3",
                                                        "start_image": "/etc/passwd", "voice_char": ch["id"]}},
            headers=h(TOMI, "Tomi")).json()
jp = A.store.get(jj["id"])["params"]
check("  nor a path or a reference into somebody's files", not ({"source_file", "start_image", "voice_char"} & set(jp)), jp)
A.manager.cancel(jj["id"])
dup = c.post(f"/api/projects/{cp['id']}/duplicate", json={}, headers=h(JUANA, "Juana")).json()
own = c.post(f"/api/projects/{cp['id']}/characters", json={"name": "Gaviota", "look": "a grey gull"}, headers=h(JUANA, "Juana")).json()
dup2 = c.post(f"/api/projects/{cp['id']}/duplicate", json={}, headers=h(JUANA, "Juana")).json()
check("  a copy of a project keeps its own characters, same ids",
      own["id"] in [x["id"] for x in c.get(f"/api/projects/{dup2['id']}/characters", headers=h(JUANA, "Juana")).json()["characters"]])
check("  a portrait needs a look, a voice test a voice sample",
      c.post(f"/api/projects/{cp['id']}/characters/{ch['id']}/speak", json={"text": "hola"}, headers=h(JUANA, "Juana")).status_code == 400)
print("\n  a recording made in the page, arriving in pieces")
rp = c.post("/api/projects", json={"name": "Tutorial", "kind": "recording"}, headers=h(JUANA, "Juana")).json()
rec = c.post(f"/api/projects/{rp['id']}/recordings", json={"title": "Toma 1"}, headers=h(JUANA, "Juana")).json()
check("  a recording starts as a clip of the project, recording", rec["recorded"] and rec["recording"]["state"] == "recording")
webm = tmp / "screen.webm"
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25", "-f", "lavfi",
                "-i", "sine=frequency=330", "-t", "3", "-shortest", "-c:v", "libvpx", "-b:v", "300k", "-c:a", "libopus",
                str(webm)], check=True)
blob = webm.read_bytes()
cuts = [0, len(blob) // 3, 2 * len(blob) // 3, len(blob)]
for n in (2, 0, 1):   # out of order on purpose: the number, not the arrival, decides
    ok = c.post(f"/api/projects/{rp['id']}/recordings/{rec['id']}/chunk?n={n}", content=blob[cuts[n]:cuts[n + 1]],
                headers={**h(JUANA, "Juana"), "Content-Type": "application/octet-stream"})
check("  its pieces are taken in", ok.status_code == 200, ok.text)
check("  and nobody else can add to it",
      c.post(f"/api/projects/{rp['id']}/recordings/{rec['id']}/chunk?n=3", content=b"x",
             headers={**h(TOMI, "Tomi"), "Content-Type": "application/octet-stream"}).status_code == 404)
c.post(f"/api/projects/{rp['id']}/recordings/{rec['id']}/finish", headers=h(JUANA, "Juana"))
deadline = time.time() + 180
while time.time() < deadline:
    clipd = next(x for x in c.get(f"/api/projects/{rp['id']}", headers=h(JUANA, "Juana")).json()["shots"] if x["id"] == rec["id"])
    if clipd["recording"]["state"] != "processing":
        break
    time.sleep(0.5)
kept = A.projects.dir(JUANA, rp["id"]) / (Projects.chosen_take(clipd) or {}).get("file", "none")
check("  finished, the pieces are one clip, kept as H.265 with its sound and length",
      clipd["recording"]["state"] == "done" and kept.is_file() and media.probe(kept)["codec"] == "hevc"
      and media.probe(kept)["has_audio"] and 2.8 < clipd["seconds"] < 3.2, (clipd, kept))
check("  and the pieces are gone", not (A.projects.dir(JUANA, rp["id"]) / "recordings" / rec["id"]).exists())
print("\n  a recording's transcript, subtitles and silences")
check("  subtitles in SubRip form", media.srt([{"start": 1.5, "end": 3.25, "text": " Hola "}, {"start": 3.5, "end": 4, "text": ""}])
      == "1\n00:00:01,500 --> 00:00:03,250\nHola\n", media.srt([{"start": 1.5, "end": 3.25, "text": " Hola "}]))


class _Heard:
    def raise_for_status(self):
        pass

    def json(self):
        return {"segments": [{"start": 0.2, "end": 1.4, "text": " Hola, esto es una prueba."},
                             {"start": 1.6, "end": 2.8, "text": "Y esto también."}]}


heard = []
A.WHISPER_URL = "http://whisper.invalid/transcribe"
_post = A.requests.post
A.requests.post = lambda url, **kw: heard.append((url, kw.get("data"))) or _Heard()
A.TRANSCRIBE_PIECE_S = 1.0   # the 3-second recording goes as three pieces
try:
    c.post(f"/api/projects/{rp['id']}/items/{rec['id']}/transcribe", json={}, headers=h(JUANA, "Juana"))
    deadline = time.time() + 60
    while time.time() < deadline:
        tk = Projects.chosen_take(next(x for x in c.get(f"/api/projects/{rp['id']}", headers=h(JUANA, "Juana")).json()["shots"]
                                       if x["id"] == rec["id"]))
        if (tk.get("transcript") or {}).get("state") != "running":
            break
        time.sleep(0.3)
finally:
    A.requests.post = _post
tr = tk.get("transcript") or {}
check("  the recording's speech goes to the recogniser, in the project's language",
      heard and heard[0][0] == A.WHISPER_URL and heard[0][1]["language"] == "es", heard)
check("  a recording goes to the recogniser a piece at a time", len(heard) == 3, len(heard))
check("  each piece's times moved to where it sits in the recording",
      tr.get("state") == "done" and "00:00:02,200 --> 00:00:03,400" in (A.projects.dir(JUANA, rp["id"]) / tr["srt"]).read_text(), tr)
check("  and comes back as a transcript and subtitles on the version",
      tr.get("state") == "done" and "Hola, esto es una prueba." in tr.get("text", "")
      and "00:00:00,200 --> 00:00:01,400" in (A.projects.dir(JUANA, rp["id"]) / tr["srt"]).read_text(), tr)
c.post(f"/api/projects/{rp['id']}/render", json={"subtitles": True}, headers=h(JUANA, "Juana"))
deadline = time.time() + 180
while time.time() < deadline:
    st = c.get(f"/api/projects/{rp['id']}", headers=h(JUANA, "Juana")).json()
    if (st.get("render") or {}).get("state") != "running":
        break
    time.sleep(0.5)
check("  a film can burn them in", st["render"]["state"] == "done", st.get("render"))
talk = tmp / "talk.mp4"
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x96:rate=24", "-f", "lavfi",
                "-i", "aevalsrc='if(lt(t,2)+gt(t,5),sin(2*PI*300*t),0)':s=48000:d=7", "-t", "7", "-shortest",
                "-pix_fmt", "yuv420p", str(talk)], check=True)
trimmed, removed = media.cut_silences(talk, tmp / "talk-trim.mp4")
check("  a long silence is cut, keeping a little air either side",
      2.2 < removed < 2.6 and 4.4 < media.probe(trimmed)["seconds"] < 4.8, (removed, media.probe(trimmed)["seconds"]))
try:
    media.cut_silences(trimmed, tmp / "again.mp4", min_s=2.0)
    check("  and a clip with none left says so", False)
except media.MediaError:
    check("  and a clip with none left says so", True)
c.post(f"/api/projects/{rp['id']}/items/{rec['id']}/trim", json={}, headers=h(JUANA, "Juana"))
deadline = time.time() + 60
while time.time() < deadline:
    tr_state = next(x for x in c.get(f"/api/projects/{rp['id']}", headers=h(JUANA, "Juana")).json()["shots"]
                    if x["id"] == rec["id"]).get("trim") or {}
    if tr_state.get("state") != "running":
        break
    time.sleep(0.3)
check("  trimming a clip with no long silence says so, and keeps the clip",
      tr_state.get("state") == "failed" and "no silence" in tr_state.get("error", ""), tr_state)
rdoc = c.get(f"/api/projects/{rp['id']}", headers=h(JUANA, "Juana")).json()
c.put(f"/api/projects/{rp['id']}", json={"shots": [dict(x, description="Paso a paso.",
                                                        chapters=[{"start": 95, "title": "Aflojar"}, {"start": "x", "title": "?"},
                                                                  {"start": 0, "title": "Intro"}, {"start": 30, "title": ""}])
                                                   for x in rdoc["shots"]]}, headers=h(JUANA, "Juana"))
kept_ch = c.get(f"/api/projects/{rp['id']}", headers=h(JUANA, "Juana")).json()["shots"][0]
check("  a recording keeps its description and chapters, in order, the unreadable ones dropped",
      kept_ch["description"] == "Paso a paso." and kept_ch["chapters"] == [{"start": 0.0, "title": "Intro"}, {"start": 95.0, "title": "Aflojar"}],
      kept_ch.get("chapters"))
check("  subtitles and trimming are for recordings",
      c.post(f"/api/projects/{sbp['id']}/items/{sdoc['shots'][0]['id']}/transcribe", json={}, headers=h(JUANA, "Juana")).status_code == 400
      and c.post(f"/api/projects/{sbp['id']}/items/{sdoc['shots'][0]['id']}/trim", json={}, headers=h(JUANA, "Juana")).status_code == 400)
# Sent as a request would be crafted by hand: a literal 1e999 in the JSON,
# which Python reads as infinity (the page's own parser cannot send it).
import json as _json
raw_body = _json.dumps({"shots": [dict(x, chapters=[{"start": "__INF__", "title": "nunca"}, {"start": 5, "title": "ok"}])
                                  for x in c.get(f"/api/projects/{rp['id']}", headers=h(JUANA, "Juana")).json()["shots"]]})
c.put(f"/api/projects/{rp['id']}", content=raw_body.replace('"__INF__"', "1e999"),
      headers={**h(JUANA, "Juana"), "Content-Type": "application/json"})
gi = c.get(f"/api/projects/{rp['id']}", headers=h(JUANA, "Juana"))
check("  a chapter time that is not a real one is dropped, and the project still opens",
      gi.status_code == 200 and gi.json()["shots"][0]["chapters"] == [{"start": 5.0, "title": "ok"}], gi.status_code)
A.projects.set_item_field(JUANA, rp["id"], rec["id"], "trim", {"state": "running"})
A.projects.set_take_field(JUANA, rp["id"], rec["id"], Projects.chosen_take(clipd)["id"], "transcript",
                          {**tr, "state": "running"})
check("  work cut off by a restart is marked interrupted, not left running", A.projects.interrupt_running() >= 1)
after = next(x for x in c.get(f"/api/projects/{rp['id']}", headers=h(JUANA, "Juana")).json()["shots"] if x["id"] == rec["id"])
check("  so the page offers it again",
      after["trim"]["state"] == "failed" and Projects.chosen_take(after)["transcript"]["state"] == "failed", after.get("trim"))
srt_left = A.projects.dir(JUANA, rp["id"]) / tr["srt"]
A.projects.delete_take(JUANA, rp["id"], rec["id"], Projects.chosen_take(after)["id"])
check("  deleting a version takes its transcript with it", not srt_left.exists())
empty = c.post(f"/api/projects/{rp['id']}/recordings", json={}, headers=h(JUANA, "Juana")).json()
check("  a recording with nothing in it cannot be finished",
      c.post(f"/api/projects/{rp['id']}/recordings/{empty['id']}/finish", headers=h(JUANA, "Juana")).status_code == 400)
print("\n  a tutorial: the screen, the camera and the computer's sound, each recorded apart")
tp = c.post("/api/projects", json={"name": "Tutorial aparte", "kind": "recording"}, headers=h(JUANA, "Juana")).json()
trec = c.post(f"/api/projects/{tp['id']}/recordings", json={"title": "Toma"}, headers=h(JUANA, "Juana")).json()
scr, camw, pcw = tmp / "t-screen.webm", tmp / "t-cam.webm", tmp / "t-pc.webm"
# The screen with the microphone: speech, a three-second pause, speech.
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25", "-f", "lavfi",
                "-i", "aevalsrc='if(lt(t,2)+gt(t,5),sin(2*PI*300*t),0)':s=48000:d=7", "-t", "7", "-shortest",
                "-c:v", "libvpx", "-b:v", "300k", "-c:a", "libopus", str(scr)], check=True)
# The camera: green, no sound. The computer: a tone all through, no picture.
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x00ff00:s=160x120:rate=25:d=7",
                "-c:v", "libvpx", "-b:v", "200k", str(camw)], check=True)
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=880:d=7", "-c:a", "libopus",
                str(pcw)], check=True)
for track, f in (("main", scr), ("cam", camw), ("pc", pcw)):
    r = c.post(f"/api/projects/{tp['id']}/recordings/{trec['id']}/chunk?n=0&track={track}", content=f.read_bytes(),
               headers={**h(JUANA, "Juana"), "Content-Type": "application/octet-stream"})
check("  three tracks taken in, each by its name", r.status_code == 200 and r.json()["track"] == "pc", r.text)
check("  and no fourth",
      c.post(f"/api/projects/{tp['id']}/recordings/{trec['id']}/chunk?n=0&track=other", content=b"x",
             headers={**h(JUANA, "Juana"), "Content-Type": "application/octet-stream"}).status_code == 400)
c.post(f"/api/projects/{tp['id']}/recordings/{trec['id']}/finish",
       json={"cam_offset_ms": 200, "pc_offset_ms": -100, "cam_layout": {"show": True, "corner": "tl", "size": 0.3},
             "mix": {"mic": 1, "pc": 0.5}}, headers=h(JUANA, "Juana"))


def tclip():
    return next(x for x in c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()["shots"]
                if x["id"] == trec["id"])


deadline = time.time() + 180
while time.time() < deadline and tclip()["recording"]["state"] == "processing":
    time.sleep(0.5)
tc = tclip()
tt = Projects.chosen_take(tc) or {}
tbase = A.projects.dir(JUANA, tp["id"])
check("  the camera and the computer sound are kept beside the screen, not in it",
      tc["recording"]["state"] == "done" and tt.get("cam") and tt.get("pc")
      and not media.probe(tbase / tt["cam"])["has_audio"] and media.probe(tbase / tt["file"])["has_audio"], (tc, tt))
check("  each lined up with the screen, as long as it",
      all(abs(media.probe(tbase / tt[k])["seconds"] - tc["seconds"]) < 0.25 for k in ("cam", "pc")),
      [media.probe(tbase / tt[k])["seconds"] for k in ("cam", "pc")] + [tc["seconds"]])
check("  where the camera was shown while recording, and the volumes, become the clip's",
      tc.get("cam_layout") == {"show": True, "corner": "tl", "size": 0.3} and tc.get("mix") == {"mic": 1.0, "pc": 0.5},
      (tc.get("cam_layout"), tc.get("mix")))


def tfilm():
    c.post(f"/api/projects/{tp['id']}/render", json={}, headers=h(JUANA, "Juana"))
    end = time.time() + 180
    while time.time() < end:
        st = c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()
        if (st.get("render") or {}).get("state") != "running":
            break
        time.sleep(0.5)
    assert st["render"]["state"] == "done", st.get("render")
    return tbase / st["render"]["file"]


def pixel(video, x, y, at=1.0):
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", str(at), "-i", str(video), "-frames:v", "1",
                          "-vf", f"crop=2:2:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True).stdout
    return tuple(raw[:3])


def loudness(video, at, seconds=1.0):
    err = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(at), "-t", str(seconds), "-i", str(video),
                          "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    m = re.search(r"mean_volume: (-?[\d.]+|-inf) dB", err)
    return float(m.group(1)) if m and m.group(1) != "-inf" else -200.0


def green(px):
    return len(px) == 3 and px[1] > 160 and px[0] < 90 and px[2] < 90


film1 = tfilm()
check("  the film draws the camera in its corner, and only there",
      green(pixel(film1, 40, 30)) and not green(pixel(film1, 300, 170)), (pixel(film1, 40, 30), pixel(film1, 300, 170)))
check("  and the computer sound under the microphone, through the microphone's pause",
      loudness(film1, 3.0) > -45, loudness(film1, 3.0))
shots = c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()["shots"]
c.put(f"/api/projects/{tp['id']}", json={"shots": [dict(x, cam_layout={"show": False, "corner": "tl", "size": 0.3},
                                                         mix={"mic": 1, "pc": 0}) for x in shots]}, headers=h(JUANA, "Juana"))
film2 = tfilm()
check("  turned off afterwards, the camera is not in the film -- it was never in the recording",
      not green(pixel(film2, 40, 30)), pixel(film2, 40, 30))
check("  and the computer sound turned down to nothing is gone from the pause", loudness(film2, 3.0) < -60,
      loudness(film2, 3.0))
c.put(f"/api/projects/{tp['id']}", json={"shots": [dict(x, cam_layout={"show": "yes", "corner": "middle", "size": 9},
                                                         mix={"mic": -3, "pc": "x"}) for x in shots]},
      headers=h(JUANA, "Juana"))
tc = tclip()
check("  a layout and volumes are kept in bounds",
      tc["cam_layout"] == {"show": True, "corner": "br", "size": 0.5} and tc["mix"] == {"mic": 0.0, "pc": 1.0},
      (tc["cam_layout"], tc["mix"]))
print("\n  a film to publish: H.264 at a size of its own")
check("  a size keeps the film's shape: wide to 1920 across, tall to 1920 down",
      media.fit_size(832, 480, "1080") == (1920, 1108) and media.fit_size(480, 832, "1080") == (1108, 1920)
      and media.fit_size(320, 180, "720") == (1280, 720) and media.fit_size(320, 180, None) == (320, 180))
c.post(f"/api/projects/{tp['id']}/render", json={"format": "h264", "size": "720"}, headers=h(JUANA, "Juana"))
deadline = time.time() + 240
while time.time() < deadline:
    st = c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()
    if (st.get("render") or {}).get("state") != "running":
        break
    time.sleep(0.5)
pub = tbase / (st.get("render") or {}).get("file", "none")
pi = media.probe(pub) if pub.is_file() else {}
check("  the film is H.264 at 720p, its name says so, and its record too",
      pi.get("codec") == "h264" and (pi.get("width"), pi.get("height")) == (1280, 720) and pub.name.endswith("-h264-720p.mp4")
      and st["renders"][-1].get("format") == "h264" and st["renders"][-1].get("size") == "720", (st.get("render"), pi))
c.post(f"/api/projects/{tp['id']}/render", json={"format": "vp9", "size": "4k"}, headers=h(JUANA, "Juana"))
deadline = time.time() + 240
while time.time() < deadline:
    st = c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()
    if (st.get("render") or {}).get("state") != "running":
        break
    time.sleep(0.5)
kept = media.probe(tbase / st["render"]["file"])
check("  anything else asked for is the kept film: H.265 at the clips' size",
      kept["codec"] == "hevc" and (kept["width"], kept["height"]) == (320, 180), kept)
key_ = f"{JUANA}/{tp['id']}"
A.render_state[key_] = {"state": "running", "started": time.time()}
_load = A.projects.load


def _load_while_it_finishes(owner, pid):
    # The project read as it was, and the render finishing right after the
    # read: the film filed and marked done between the two halves of a GET.
    doc = _load(owner, pid)
    A.render_state[key_] = {"state": "done", "file": "renders/new.mp4"}
    return doc


A.projects.load = _load_while_it_finishes
seen_ = c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()
A.projects.load = _load
check("  a render finishing while the project is read is still running in that answer, never done with the old films",
      (seen_.get("render") or {}).get("state") == "running", seen_.get("render"))
A.render_state.pop(key_, None)
print("\n  a title card, and text over a recording")
tt_now = Projects.chosen_take(tclip())["id"]
shots = c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()["shots"]
c.put(f"/api/projects/{tp['id']}", json={"shots": [
    {"card": {"title": "Cómo cambiar una rueda", "subtitle": "en cinco minutos", "seconds": 2, "theme": "olive"}, "prompt": ""}]
    + [dict(x, callouts_take=tt_now, callouts=[{"start": 3, "end": 5, "text": "Aflojá las tuercas primero", "spot": "bottom"},
                                              {"start": "x", "end": 2, "text": "?"}]) for x in shots]}, headers=h(JUANA, "Juana"))
tdoc = c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()
check("  a title card is a clip of the project with its words, and a recording keeps its texts",
      tdoc["shots"][0]["card"] == {"title": "Cómo cambiar una rueda", "subtitle": "en cinco minutos", "seconds": 2.0, "theme": "olive"}
      and [x["text"] for x in tdoc["shots"][1]["callouts"]] == ["Aflojá las tuercas primero"], tdoc["shots"][:2])
film5 = tfilm()
check("  the film opens with the card, for as long as it lasts",
      abs(media.probe(film5)["seconds"] - (2 + tc["seconds"])) < 0.5
      and pixel(film5, 20, 20, 1.0)[:3] and abs(pixel(film5, 20, 20, 1.0)[0] - 60) < 12 and abs(pixel(film5, 20, 20, 1.0)[1] - 70) < 12,
      (media.probe(film5)["seconds"], pixel(film5, 20, 20, 1.0)))


def dark(px):
    return len(px) == 3 and max(px) < 80


# Left of the camera, which still shows large in the bottom-right corner.
_in, _out = pixel(film5, 100, 160, 2 + 4.0), pixel(film5, 100, 160, 2 + 1.0)
check("  and the text shows over the recording in its stretch, and only there",
      len(_in) == 3 and sum(abs(a - b) for a, b in zip(_in, _out)) > 90, (_in, _out))
shots = c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()["shots"]
c.put(f"/api/projects/{tp['id']}", json={"shots": [dict(x, callouts_take="zzzzzz222222") for x in shots]},
      headers=h(JUANA, "Juana"))
film6 = tfilm()
check("  texts placed on another version are not drawn on this one",
      sum(abs(a - b) for a, b in zip(pixel(film6, 100, 160, 2 + 4.0), _in)) > 90, pixel(film6, 100, 160, 6.0))
shots = c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()["shots"]
c.put(f"/api/projects/{tp['id']}", json={"shots": [dict(x, callouts=[], callouts_take="") for x in shots if not x.get("card")]},
      headers=h(JUANA, "Juana"))
print("\n  a recording cut into sections by hand, nothing cut from its files")
tt_id = (Projects.chosen_take(tclip()) or {})["id"]
shots = c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()["shots"]
c.put(f"/api/projects/{tp['id']}", json={"shots": [dict(x, sections_take=tt_id, sections=[
    {"start": 0, "end": 2, "keep": True, "cam": {"show": True, "corner": "tl", "size": 0.3}},
    {"start": 1, "end": 3, "keep": True},                       # overlaps the first: dropped
    {"start": 2, "end": 5, "keep": False},
    {"start": 5, "end": 7, "keep": True, "cam": {"show": False, "corner": "br", "size": 0.3}},
    {"start": "x", "end": 9}]) for x in shots]}, headers=h(JUANA, "Juana"))
tc = tclip()
check("  its sections are kept in order, the overlapping and unreadable ones dropped",
      [(x["start"], x["end"], x["keep"]) for x in tc["sections"]] == [(0, 2, True), (2, 5, False), (5, 7, True)]
      and tc["sections"][1]["cam"] is None and tc["sections_take"] == tt_id, tc.get("sections"))
film3 = tfilm()
check("  the film keeps only the kept sections", 3.6 < media.probe(film3)["seconds"] < 4.4, media.probe(film3)["seconds"])
check("  each with its own camera: in its corner in the first, hidden in the last",
      green(pixel(film3, 40, 30, 1.0)) and not green(pixel(film3, 40, 30, 3.0))
      and not green(pixel(film3, 300, 170, 3.0)), (pixel(film3, 40, 30, 1.0), pixel(film3, 40, 30, 3.0)))
check("  and the files are untouched", abs(media.probe(tbase / tt["file"])["seconds"] - tc["seconds"]) < 0.2)
c.put(f"/api/projects/{tp['id']}", json={"shots": [dict(x, sections_take="zzzzzz111111") for x in
                                                     c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()["shots"]]},
      headers=h(JUANA, "Juana"))
film4 = tfilm()
check("  sections drawn over another version are not this one's: the clip goes in whole",
      media.probe(film4)["seconds"] > 6.5, media.probe(film4)["seconds"])
c.put(f"/api/projects/{tp['id']}", json={"shots": [dict(x, sections=[], sections_take="") for x in
                                                     c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()["shots"]]},
      headers=h(JUANA, "Juana"))
srt_in = tmp / "parts.srt"
srt_in.write_text(media.srt([{"start": 0.5, "end": 1.5, "text": "uno"}, {"start": 2.5, "end": 4.5, "text": "dos"},
                             {"start": 6.0, "end": 6.8, "text": "tres"}]), encoding="utf-8")
part = media.shift_srt(srt_in, tmp / "part.srt", 2.0, 5.0).read_text()
check("  a section's subtitles are its own lines, timed from where it starts",
      "dos" in part and "uno" not in part and "tres" not in part and "00:00:00,500 --> 00:00:02,500" in part, part)
print("\n  a recording's microphone cleaned: noise out, loudness evened")
noisy = tmp / "noisy.mp4"
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x96:rate=24", "-f", "lavfi",
                "-i", "anoisesrc=d=4:c=white:a=0.02", "-f", "lavfi", "-i", "sine=frequency=300:d=4", "-filter_complex",
                "[2:a]volume=0.05[v];[1:a][v]amix=inputs=2:normalize=0[a]", "-map", "0:v", "-map", "[a]", "-t", "4",
                "-pix_fmt", "yuv420p", str(noisy)], check=True)
cleaned = media.clean_voice(noisy, tmp / "noisy-clean.mp4")
check("  the picture is copied as it is, the sound evened out to video level",
      media.probe(cleaned)["codec"] == media.probe(noisy)["codec"] and -20 < loudness(cleaned, 0.5, 3) < -12,
      (media.probe(cleaned)["codec"], loudness(noisy, 0.5, 3), loudness(cleaned, 0.5, 3)))
try:
    media.clean_voice(noisy, tmp / "none.mp4", denoise=False, level=False)
    check("  asking for nothing says so", False)
except media.MediaError:
    check("  asking for nothing says so", True)
before = Projects.chosen_take(tclip())
shots = c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()["shots"]
c.put(f"/api/projects/{tp['id']}", json={"shots": [dict(x, sections_take=before["id"], sections=[
    {"start": 0, "end": 3, "keep": True}, {"start": 3, "end": 7, "keep": False}]) for x in shots]}, headers=h(JUANA, "Juana"))
c.post(f"/api/projects/{tp['id']}/items/{trec['id']}/clean", json={}, headers=h(JUANA, "Juana"))
deadline = time.time() + 120
while time.time() < deadline and (tclip().get("clean") or {}).get("state") == "running":
    time.sleep(0.3)
tc = tclip()
cl = Projects.chosen_take(tc)
check("  a new version, its camera and computer sound kept beside it",
      (tc.get("clean") or {}).get("state") == "done" and cl["id"] != before["id"] and cl["kind"] == "cleaned"
      and cl.get("cam") and cl.get("pc") and (tbase / cl["cam"]).is_file(), (tc.get("clean"), cl))
check("  sharing their files, not copies of them",
      os.stat(tbase / cl["cam"]).st_ino == os.stat(tbase / before["cam"]).st_ino)
check("  and the sections drawn over the old version move to it", tc["sections_take"] == cl["id"], tc["sections_take"])
A.projects.delete_take(JUANA, tp["id"], trec["id"], cl["id"])
check("  deleting the cleaned version leaves the original's camera in place", (tbase / before["cam"]).is_file())
shots = c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()["shots"]
c.put(f"/api/projects/{tp['id']}", json={"shots": [dict(x, sections=[], sections_take="",
                                                          chosen=next(i for i, k in enumerate(x["takes"]) if k["id"] == before["id"]))
                                                     for x in shots]}, headers=h(JUANA, "Juana"))
c.post(f"/api/projects/{tp['id']}/items/{trec['id']}/trim", json={}, headers=h(JUANA, "Juana"))
deadline = time.time() + 120
while time.time() < deadline and (tclip().get("trim") or {}).get("state") == "running":
    time.sleep(0.3)
tc = tclip()
tt2 = Projects.chosen_take(tc) or {}
check("  the microphone's pause is cut from all three, so they stay together",
      (tc.get("trim") or {}).get("state") == "done" and tt2.get("cam") and tt2.get("pc")
      and all(abs(media.probe(tbase / tt2[k])["seconds"] - media.probe(tbase / tt2["file"])["seconds"]) < 0.3
              for k in ("cam", "pc")) and media.probe(tbase / tt2["file"])["seconds"] < tc["seconds"] + 0.1
      and media.probe(tbase / tt2["file"])["seconds"] < 5.2, (tc.get("trim"), tt2))
left = [tbase / tt2[k] for k in ("file", "cam", "pc")]
A.projects.delete_take(JUANA, tp["id"], trec["id"], tt2["id"])
check("  deleting a version takes its camera and computer sound with it", not any(f.exists() for f in left))
check("  nobody else can ask for someone's song",
      c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/analyze", json={}, headers=h(TOMI, "Tomi")).status_code == 404)

print("\n  eye contact on a recording's camera, over the stretches marked")
import numpy as np  # noqa: E402
from studio import eyes as EY  # noqa: E402
check("  outside every stretch nothing applies; inside, all of it; around one it eases",
      EY.ease_weight(1.0, [(3, 6)]) == 0 and EY.ease_weight(4.0, [(3, 6)]) == 1
      and 0 < EY.ease_weight(2.9, [(3, 6)]) < 1 and EY.ease_weight(9.0, []) == 1)
check("  closed or wide-open eyes are left alone, usual ones corrected",
      EY.open_weight(0.4, 0.4) == 1 and EY.open_weight(0.18, 0.4) == 0 and EY.open_weight(0.7, 0.4) == 0
      and 0 < EY.open_weight(0.28, 0.4) < 1)
sm = np.array([[0.0, -0.08], [-0.07, -0.08], [0.0, -0.02], [0.3, 0.3]])
sh = EY.shifts_for(sm.copy(), np.array([14.0, 8.0]))
check("  a gaze already on the lens gets no shift; one beside or below it is moved back, never past the cap",
      (sh[0] == 0).all() and sh[1, 0] > 0 and sh[1, 1] == 0 and sh[2, 1] < 0
      and (np.abs(sh[3]) <= EY.CAP + 1e-9).all(), sh.tolist())
check("  a direction the face barely answers in is left alone",
      (EY.shifts_for(sm.copy(), np.array([14.0, 2.0]))[:, 1] == 0).all())
g = np.zeros((90, 2)); g[:, 0] = -0.07; g[45, 0] = 0.4
good = np.ones(90, bool); good[45] = False
check("  a blink's stray pupil is not part of the smoothed gaze", abs(EY.smooth_gaze(g, good, 30)[45, 0] + 0.07) < 1e-6)
check("  stretches: in order, inside the clip, the too-short dropped",
      EY.clean_spans([[5, 9], [1, 2], [3, 3.1], ["x", 1]], 8) == [(1.0, 2.0), (5.0, 8.0)])
from studio.projects import clean_eyes  # noqa: E402
check("  the item's stretches are kept in order, overlaps joined",
      clean_eyes([{"start": 4, "end": 6}, {"start": 1, "end": 3}, {"start": 5, "end": 8}, {"start": 2, "end": "x"}])
      == [{"start": 1.0, "end": 3.0}, {"start": 4.0, "end": 8.0}])
fake_eyes = tmp / "fake_eyes.py"
fake_eyes.write_text(
    "import json, shutil, sys, time\n"
    "job = json.load(open(sys.argv[1]))\n"
    "print('LivePortrait chatter', file=sys.stderr)\n"
    "print(json.dumps({'progress': 0.5, 'phase': 'rendering'}), flush=True)\n"
    "if job['spans'] and job['spans'][0][0] >= 3: time.sleep(30)\n"
    "shutil.copy(job['src'], job['dst'])\n"
    "print(json.dumps({'done': {'frames': 10, 'changed': 4}}), flush=True)\n")
A.manager.eyes_cmd = [sys.executable, str(fake_eyes)]
base_t = Projects.chosen_take(tclip())
r = c.post(f"/api/projects/{tp['id']}/items/{trec['id']}/eyes", json={}, headers=h(JUANA, "Juana"))
check("  nothing marked and not the whole clip: it says what to do", r.status_code == 400, r.status_code)
shots = c.get(f"/api/projects/{tp['id']}", headers=h(JUANA, "Juana")).json()["shots"]
c.put(f"/api/projects/{tp['id']}", json={"shots": [dict(x, eyes=[{"start": 1, "end": 2.5}], eyes_take=base_t["id"],
                                                          callouts=[{"start": 0, "end": 1, "text": "hola"}],
                                                          callouts_take=base_t["id"]) for x in shots]},
      headers=h(JUANA, "Juana"))
r = c.post(f"/api/projects/{tp['id']}/items/{trec['id']}/eyes", json={"strength": 0.8}, headers=h(JUANA, "Juana")).json()
ej = A.store.get(r["job"]["id"])
check("  queued on the card with its stretches, its strength and the seconds it covers",
      ej["kind"] == "eyes" and ej["params"]["spans"] == [[1.0, 2.5]] and ej["params"]["strength"] == 0.8
      and ej["params"]["seconds"] == 1.5 and ej["model"] == "liveportrait", ej["params"])
check("  asking again while it waits returns the same job",
      c.post(f"/api/projects/{tp['id']}/items/{trec['id']}/eyes", json={}, headers=h(JUANA, "Juana")).json()["job"]["id"] == ej["id"])
A.manager._run(ej)
tc = tclip()
ne = Projects.chosen_take(tc)
check("  done: a new version whose camera is new and whose screen and sounds are the same files",
      A.store.get(ej["id"])["state"] == "done" and ne["id"] != base_t["id"] and ne["kind"] == "eyes"
      and ne["cam"] != base_t["cam"] and (tbase / ne["cam"]).is_file()
      and os.stat(tbase / ne["file"]).st_ino == os.stat(tbase / base_t["file"]).st_ino
      and (not base_t.get("pc") or os.stat(tbase / ne["pc"]).st_ino == os.stat(tbase / base_t["pc"]).st_ino),
      (A.store.get(ej["id"]), ne))
check("  the stretches and the texts move to it, and the item says it is done",
      tc["eyes_take"] == ne["id"] and tc["callouts_take"] == ne["id"] and tc["eyes_fix"]["state"] == "done",
      (tc.get("eyes_take"), tc.get("eyes_fix")))
check("  the job's own chatter stays out of its reports", "LivePortrait chatter" in (tmp / "api" / "logs" / "eyes.log").read_text()
      if (tmp / "api" / "logs" / "eyes.log").exists() else True)
A.projects.delete_take(JUANA, tp["id"], trec["id"], ne["id"])
check("  deleting it leaves the original's files", (tbase / base_t["cam"]).is_file() and (tbase / base_t["file"]).is_file())
r = c.post(f"/api/projects/{tp['id']}/items/{trec['id']}/eyes", json={"whole": True, "take": base_t["id"]},
           headers=h(JUANA, "Juana")).json()
check("  the whole clip, when asked for: no stretches", A.store.get(r["job"]["id"])["params"]["spans"] == [])
A.manager.cancel(r["job"]["id"])
slow = A.store.add(owner=JUANA, owner_name="Juana", kind="eyes", model="liveportrait",
                   params={"take": base_t["id"], "spans": [[3, 4]], "strength": 1.0, "seconds": 1},
                   project=tp["id"], target=trec["id"])
th = threading.Thread(target=A.manager._run, args=(slow,)); th.start()
deadline = time.time() + 20
while time.time() < deadline and A.store.get(slow["id"])["progress"] < 0.5:
    time.sleep(0.1)
A.manager.pause(now=True)
th.join(20)
check("  a pause now stops it on the card and puts it back in the queue",
      not th.is_alive() and A.store.get(slow["id"])["state"] == "queued", A.store.get(slow["id"])["state"])
A.manager.paused = False
th = threading.Thread(target=A.manager._run, args=(A.store.get(slow["id"]),)); th.start()
deadline = time.time() + 20
while time.time() < deadline and A.store.get(slow["id"])["progress"] < 0.5:
    time.sleep(0.1)
A.manager.cancel(slow["id"])
th.join(20)
check("  and cancelling it ends it cancelled, with no new version",
      not th.is_alive() and A.store.get(slow["id"])["state"] == "cancelled"
      and Projects.chosen_take(tclip())["id"] == base_t["id"], A.store.get(slow["id"])["state"])

print("\n  the shots refitted to the song")
import json  # noqa: E402
rt = c.post("/api/projects", json={"name": "A la canción"}, headers=h(JUANA, "Juana")).json()
rsong = c.put(f"/api/projects/{rt['id']}", json={"audio": [{"kind": "song", "lyrics": "[Coro]\nla"}],
                                                "shots": [{"prompt": f"toma {i}", "seconds": 5} for i in range(6)]},
              headers=h(JUANA, "Juana")).json()
rdir = A.projects.dir(JUANA, rt["id"])
(rdir / "takes").mkdir(exist_ok=True)
shutil.copy(song_file, rdir / "takes" / "song.wav") if "song_file" in globals() else None
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=60", str(rdir / "takes" / "s.wav")], check=True)
A.projects.add_take(JUANA, rt["id"], rsong["audio"][0]["id"], {"file": "takes/s.wav", "kind": "song"})
stake = c.get(f"/api/projects/{rt['id']}", headers=h(JUANA, "Juana")).json()["audio"][0]["takes"][0]
old_an = {"duration": 60.0, "tempo": 120.0, "beats": [0.25 + 0.5 * i for i in range(119)],
          "bars": [0.25 + 2.0 * i for i in range(30)], "lines": [], "sections": [], "aligned": False}
(rdir / "takes" / "an.json").write_text(json.dumps(old_an))
A.projects.set_take_field(JUANA, rt["id"], rsong["audio"][0]["id"], stake["id"], "analysis", {"file": "takes/an.json", "tempo": 120})
A.analysis.regrid = lambda an, song: {**an, "grid": 2}          # the CPU part, measured above
first_shot = c.get(f"/api/projects/{rt['id']}", headers=h(JUANA, "Juana")).json()["shots"][0]["id"]
A.projects.add_take(JUANA, rt["id"], first_shot, {"file": "takes/v.mp4", "seconds": 5.0})
out = c.post(f"/api/projects/{rt['id']}/retime", json={}, headers=h(JUANA, "Juana")).json()
after = c.get(f"/api/projects/{rt['id']}", headers=h(JUANA, "Juana")).json()
check("  every shot keeps its place and its words, and gets its cut on the song",
      out.get("shots") == 6 and [s["prompt"] for s in after["shots"]] == [f"toma {i}" for i in range(6)]
      and all(s.get("exact") for s in after["shots"]) and abs(sum(s["seconds"] for s in after["shots"]) - 60.0) < 0.01
      and after["settings"]["soundtrack"] == rsong["audio"][0]["id"], (out, [(s["start"], s["seconds"]) for s in after["shots"]]))
check("  a video now shorter than its shot is named, to make again", out.get("short") == [1], out.get("short"))
check("  an analysis from before the grid is given one, and keeps it",
      json.loads((rdir / "takes" / "an.json").read_text()).get("grid") == 2)
check("  nobody else's", c.post(f"/api/projects/{rt['id']}/retime", json={}, headers=h(TOMI, "Tomi")).status_code == 404)

print("\n  a project's history, through the API")
hq = c.post("/api/projects", json={"name": "Con historia"}, headers=h(JUANA, "Juana")).json()
c.put(f"/api/projects/{hq['id']}", json={"shots": [{"prompt": "hola", "seconds": 5}]}, headers=h(JUANA, "Juana"))
via = {**h(JUANA, "Juana"), "X-Studio-Via": "Alfred"}
c.post(f"/api/projects/{hq['id']}/items", json={"section": "images", "items": [{"prompt": "un gato"}]}, headers=via)
lg = c.get(f"/api/projects/{hq['id']}/history", headers=h(JUANA, "Juana")).json()
check("  every save is in the history by the time it is asked for, the assistant's marked as its",
      [r["author"] for r in lg["revisions"]] == ["Juana (Alfred)", "Juana", "Juana"]
      and lg["revisions"][-1]["kind"] == "start", lg["revisions"])
check("  nobody else can read it", c.get(f"/api/projects/{hq['id']}/history", headers=h(TOMI, "Tomi")).status_code == 404)
check("  a revision that is not one is a 404",
      c.get(f"/api/projects/{hq['id']}/history/zzzz", headers=h(JUANA, "Juana")).status_code == 404)
rv = c.post(f"/api/projects/{hq['id']}/history/{lg['revisions'][0]['rev']}/revert", headers=h(JUANA, "Juana")).json()
check("  and reverting the assistant's change takes its picture out",
      not rv["conflicts"] and c.get(f"/api/projects/{hq['id']}", headers=h(JUANA, "Juana")).json()["images"] == [], rv)
tg = c.post(f"/api/projects/{hq['id']}/history/{lg['revisions'][1]['rev']}/tag", json={"name": "para Mora"},
            headers=h(JUANA, "Juana")).json()
check("  a tag is put on a revision and listed",
      [t["name"] for t in c.get(f"/api/projects/{hq['id']}/history", headers=h(JUANA, "Juana")).json()["tags"]] == ["para Mora"])
check("  and taken off", c.delete(f"/api/projects/{hq['id']}/tags/{tg['ref']}", headers=h(JUANA, "Juana")).status_code == 200
      and c.get(f"/api/projects/{hq['id']}/history", headers=h(JUANA, "Juana")).json()["tags"] == [])

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

print("\ncollections: a person's projects gathered under a name")
HJ, HT = h(JUANA, "Juana"), h(TOMI, "Tomi")
check("  a collection needs a name", c.post("/api/collections", json={"name": "  "}, headers=HJ).status_code == 400)
nico = c.post("/api/collections", json={"name": "Nico"}, headers=HJ).json()
cumple = c.post("/api/collections", json={"name": "Cumples"}, headers=HJ).json()
inside = c.post("/api/projects", json={"name": "Pirata", "kind": "free", "collection": nico["id"]}, headers=HJ).json()
check("  a project made inside a collection is in it",
      next(x for x in c.get("/api/projects", headers=HJ).json()["projects"] if x["id"] == inside["id"])["collections"]
      == [nico["id"]])
check("  an unknown collection makes no project",
      c.post("/api/projects", json={"name": "x", "collection": "zzzzzzzz"}, headers=HJ).status_code == 404
      and not any(x["name"] == "x" for x in c.get("/api/projects", headers=HJ).json()["projects"]))
c.post(f"/api/collections/{cumple['id']}/projects", json={"project": inside["id"]}, headers=HJ)
c.post(f"/api/collections/{cumple['id']}/projects", json={"project": inside["id"]}, headers=HJ)
c.post(f"/api/collections/{nico['id']}/projects", json={"project": d1["id"]}, headers=HJ)
lst = c.get("/api/projects", headers=HJ).json()
mine = {x["id"]: x["collections"] for x in lst["projects"]}
check("  added to a second collection, once however often it is asked",
      mine[inside["id"]] == [nico["id"], cumple["id"]], mine)
check("  each collection counts its projects",
      {x["name"]: x["count"] for x in lst["collections"]} == {"Nico": 2, "Cumples": 1}, lst["collections"])
check("  Tomi sees none of Juana's collections, and cannot add to them",
      c.get("/api/projects", headers=HT).json()["collections"] == []
      and c.post(f"/api/collections/{nico['id']}/projects", json={"project": inside["id"]}, headers=HT).status_code == 404)
tomis = c.post("/api/projects", json={"name": "De Tomi"}, headers=HT).json()
check("  nor can Juana file Tomi's project in hers",
      c.post(f"/api/collections/{nico['id']}/projects", json={"project": tomis["id"]}, headers=HJ).status_code == 404)
c.delete(f"/api/collections/{cumple['id']}/projects/{inside['id']}", headers=HJ)
check("  removed from one collection, it stays in the other and the project stays",
      next(x for x in c.get("/api/projects", headers=HJ).json()["projects"] if x["id"] == inside["id"])["collections"]
      == [nico["id"]])
c.put(f"/api/collections/{nico['id']}", json={"name": "Nico pirata"}, headers=HJ)
copy = c.post(f"/api/projects/{inside['id']}/duplicate", json={}, headers=HJ).json()
lst = c.get("/api/projects", headers=HJ).json()
check("  renamed; and a copy sits where its original does",
      [x["name"] for x in lst["collections"] if x["id"] == nico["id"]] == ["Nico pirata"]
      and next(x for x in lst["projects"] if x["id"] == copy["id"])["collections"] == [nico["id"]])
hist = c.get(f"/api/projects/{inside['id']}/history", headers=HJ).json()
check("  filing a project is not a change to it: its history does not grow",
      not any("ollection" in json.dumps(r) for r in (hist.get("revisions") or hist.get("log") or [])), hist)
c.delete(f"/api/projects/{copy['id']}", headers=HJ)
c.delete(f"/api/collections/{nico['id']}", headers=HJ)
lst = c.get("/api/projects", headers=HJ).json()
check("  a deleted collection takes no project with it",
      [x["name"] for x in lst["collections"]] == ["Cumples"]
      and all(x["collections"] == [] for x in lst["projects"]) and any(x["id"] == inside["id"] for x in lst["projects"]),
      lst)

print("\nan audio story: voices in order, music under them, a cover")
from studio import story  # noqa: E402
pl, total = story.plan([("instrumental", "m", 20), ("voice", "a", 5), ("voice", "b", 4), ("song", "s", 30),
                        ("voice", "c", 3), ("instrumental", "x", 8)])
by = {p["file"]: p for p in pl}
check("  the music before the voices plays alone first, then under them, quietly and looped",
      by["a"]["start"] == story.LEAD and by["m"]["start"] == 0 and by["m"]["volume"] == story.BED and by["m"]["loop"]
      and by["b"]["start"] == round(story.LEAD + 5 + story.GAP, 3), pl)
check("  a song ends the music under it and plays whole",
      by["m"]["start"] + by["m"]["length"] <= by["s"]["start"] + 1.0 and by["s"]["volume"] == 1.0
      and by["c"]["start"] == round(by["s"]["start"] + 30 + story.GAP, 3), pl)
check("  music with nothing to go under is an interlude, whole and at its own level",
      by["x"]["volume"] == story.MUSIC and not by["x"]["loop"] and by["x"]["length"] == 8
      and total == round(by["x"]["start"] + 8, 3), (pl, total))
pl2, total2 = story.plan([("voice", "a", 5), ("instrumental", "m", 3), ("voice", "b", 4)])
check("  music under the last voices lasts past them and fades",
      {p["file"]: p for p in pl2}["m"]["length"] == round(story.LEAD + 4 + story.TAIL, 3)
      and total2 == round(5 + story.GAP + story.LEAD + 4 + story.TAIL, 3), (pl2, total2))
check("  nothing made yet is nothing to place", story.plan([("voice", "a", 0)]) == ([], 0.0))
pj, _tj = story.plan([("instrumental", "intro", 6, True), ("voice", "a", 5), ("voice", "b", 4),
                      ("instrumental", "outro", 5, True)])
byj = {p["file"]: p for p in pj}
check("  a jingle marked to play on its own is not laid under the voices: it plays whole, then they start",
      byj["intro"]["volume"] == story.MUSIC and not byj["intro"]["loop"] and byj["intro"]["length"] == 6
      and byj["a"]["start"] == round(6 + story.GAP, 3) and byj["outro"]["volume"] == story.MUSIC, pj)

sp = c.post("/api/projects", json={"name": "El faro", "kind": "audio_story"}, headers=HJ).json()
check("  a project can be an audio story", sp["kind"] == "audio_story")
spdir = A.projects.dir(JUANA, sp["id"])
(spdir / "takes").mkdir(exist_ok=True)


def tone(name, seconds, freq):
    out = spdir / "takes" / name
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}", "-ac", "2", str(out)], check=True)
    return out.name


saved = c.put(f"/api/projects/{sp['id']}", json={"audio": [
    {"kind": "instrumental", "title": "Intro", "style": "soft piano", "alone": "yes please"},
    {"kind": "voice", "title": "Narrador", "text": "Había una vez un faro."},
    {"kind": "voice", "title": "Bruma", "text": "¡Hola!"}],
    "images": [{"prompt": "a lighthouse at dusk", "size": "1024x1024"}]}, headers=HJ).json()
intro, line1, line2 = (a["id"] for a in saved["audio"])
check("  `alone` is kept as a yes or no", saved["audio"][0]["alone"] is True, saved["audio"][0])
c.put(f"/api/projects/{sp['id']}", json={"audio": [{**saved["audio"][0], "alone": False}] + saved["audio"][1:]},
      headers=HJ)
cover_id = saved["images"][0]["id"]
for iid, (name, secs, f) in zip((intro, line1, line2), (("intro.wav", 4, 220), ("n.wav", 3, 440), ("b.wav", 2, 660))):
    A.projects.add_take(JUANA, sp["id"], iid, {"file": "takes/" + tone(name, secs, f)})
check("  the story wants a version of something before it can be put together",
      c.post(f"/api/projects/{c.post('/api/projects', json={'name': 'vacía', 'kind': 'audio_story'}, headers=HJ).json()['id']}/story",
             json={}, headers=HJ).status_code == 400)
check("  and a video of it wants its cover",
      c.post(f"/api/projects/{sp['id']}/story", json={"format": "video"}, headers=HJ).status_code == 400)
subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=navy:s=512x512",
                "-frames:v", "1", str(spdir / "takes" / "cover.jpg")], check=True)
A.projects.add_take(JUANA, sp["id"], cover_id, {"file": "takes/cover.jpg"})
c.put(f"/api/projects/{sp['id']}", json={"settings": {"cover": cover_id}}, headers=HJ)
check("  its cover is the picture named, on its card",
      next(x for x in c.get("/api/projects", headers=HJ).json()["projects"] if x["id"] == sp["id"])["cover"]
      == "takes/cover.jpg")
c.put(f"/api/projects/{sp['id']}", json={"settings": {"cover": "../../x"}}, headers=HJ)
check("  and a cover is an item id, nothing else",
      c.get(f"/api/projects/{sp['id']}", headers=HJ).json()["settings"]["cover"] == "")
c.put(f"/api/projects/{sp['id']}", json={"settings": {"cover": cover_id}}, headers=HJ)


def story_done(fmt):
    c.post(f"/api/projects/{sp['id']}/story", json={"format": fmt}, headers=HJ)
    deadline = time.time() + 240
    while time.time() < deadline:
        st = c.get(f"/api/projects/{sp['id']}", headers=HJ).json()
        if (st.get("render") or {}).get("state") != "running":
            return st
        time.sleep(0.3)
    return st


st = story_done("audio")
m4a = spdir / (st.get("render") or {}).get("file", "none")
info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                                  str(m4a)], capture_output=True, text=True).stdout or "{}") if m4a.is_file() else {}
kinds = sorted(s_["codec_type"] for s_ in info.get("streams", []))
expected = story.plan([("instrumental", "", 4), ("voice", "", 3), ("voice", "", 2)])[1]
check("  put together: an M4A as long as the plan, with the cover as its artwork",
      m4a.suffix == ".m4a" and kinds == ["audio", "video"]
      and abs(float(info["format"]["duration"]) - expected) < 0.5
      and st["renders"][-1].get("story") and st["renders"][-1]["format"] == "m4a", (st.get("render"), kinds, info.get("format")))
st = story_done("video")
mp4 = spdir / (st.get("render") or {}).get("file", "none")
vi = media.probe(mp4) if mp4.is_file() else {}
check("  and as a video of the cover, square, for sites that take video only",
      mp4.suffix == ".mp4" and (vi.get("width"), vi.get("height")) == (1080, 1080) and vi.get("has_audio")
      and abs(vi.get("seconds", 0) - expected) < 0.7, (st.get("render"), vi))
check("  the mix was not left behind",
      not list((spdir / "renders").glob("*.wav")))

bru = c.post(f"/api/projects/{sp['id']}/characters", json={"name": "Bruma", "look": "a small fox"}, headers=HJ).json()
doc = c.get(f"/api/projects/{sp['id']}", headers=HJ).json()
for a in doc["audio"]:
    if a["id"] == line2:
        a["speaker"] = bru["id"]
c.put(f"/api/projects/{sp['id']}", json={"audio": doc["audio"]}, headers=HJ)
r = c.post(f"/api/projects/{sp['id']}/generate", json={"items": [line2]}, headers=HJ)
check("  a line said by a character without a voice sample is refused, by name",
      r.status_code == 400 and "Bruma" in r.text, (r.status_code, r.text[:200]))
c.post(f"/api/projects/{sp['id']}/characters/{bru['id']}/upload", headers=HJ,
       files={"file": ("bruma.wav", (spdir / "takes" / "b.wav").read_bytes(), "audio/wav")}, data={"kind": "voice"})
r = c.post(f"/api/projects/{sp['id']}/generate", json={"items": [line2]}, headers=HJ)
job = A.store.get(r.json()["queued"][0]["id"]) if r.status_code == 200 else {}
check("  with one, it is said in that character's voice",
      r.status_code == 200 and job.get("kind") == "voice" and job["params"].get("voice_char") == bru["id"]
      and not job["params"].get("voice_upload"), (r.status_code, r.text[:200], job.get("params")))
A.manager.cancel(job["id"]) if job else None

print("\nan explainer: a picture or a clip for each point, on screen while it is said")
ep = c.post("/api/projects", json={"name": "Cómo funciona un faro", "kind": "explainer"}, headers=HJ).json()
epdir = A.projects.dir(JUANA, ep["id"])
(epdir / "takes").mkdir(exist_ok=True)


def media_file(name, args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, str(epdir / "takes" / name)], check=True)
    return "takes/" + name


saved_e = c.put(f"/api/projects/{ep['id']}", json={"shots": [
    {"card": {"title": "Cómo funciona un faro", "subtitle": "en tres pasos", "seconds": 2}},
    {"prompt": "a lighthouse lamp", "seconds": 4},
    {"prompt": "a lens turning", "seconds": 4},
    {"prompt": "ships at sea", "seconds": 3}]}, headers=HJ).json()
_card, pA, pB, pC = (x["id"] for x in saved_e["shots"])
check("  a point without a picture cannot be put together, and says which",
      c.post(f"/api/projects/{ep['id']}/explainer", json={}, headers=HJ).status_code == 400)
for sid, name in ((pA, "a.png"), (pB, "b.png"), (pC, "c.png")):
    A.projects.add_board(JUANA, ep["id"], sid, {"file": media_file(name, ["-f", "lavfi", "-i", "color=c=teal:s=640x360",
                                                                          "-frames:v", "1"])})
A.projects.add_take(JUANA, ep["id"], pB, {"file": media_file("b.mp4", ["-f", "lavfi", "-i", "color=c=red:s=640x360:d=1",
                                                                       "-r", "24", "-pix_fmt", "yuv420p"])})
saved_e = c.put(f"/api/projects/{ep['id']}", json={"audio": [
    {"kind": "voice", "title": "A", "text": "La lámpara.", "point": pA},
    {"kind": "voice", "title": "B", "text": "La lente gira.", "point": pB},
    {"kind": "instrumental", "title": "Fondo", "style": "calm"},
    {"kind": "voice", "title": "x", "text": "y", "point": "../../nope"}], "settings": {"narrator": "abcdef123456"}},
    headers=HJ).json()
check("  a narration names its point by id, nothing else; the project keeps its narrator",
      saved_e["audio"][3]["point"] == "" and saved_e["audio"][0]["point"] == pA
      and saved_e["settings"]["narrator"] == "abcdef123456", saved_e["audio"])
for aid, (name, secs, f) in zip((saved_e["audio"][0]["id"], saved_e["audio"][1]["id"], saved_e["audio"][2]["id"]),
                                (("na.wav", 3, 330), ("nb.wav", 4, 440), ("bed.wav", 2, 220))):
    A.projects.add_take(JUANA, ep["id"], aid, {"file": media_file(name, ["-f", "lavfi", "-i",
                                                                         f"sine=frequency={f}:duration={secs}", "-ac", "2"])})
c.post(f"/api/projects/{ep['id']}/explainer", json={"format": "h264", "size": "1080"}, headers=HJ)
deadline = time.time() + 300
while time.time() < deadline:
    st = c.get(f"/api/projects/{ep['id']}", headers=HJ).json()
    if (st.get("render") or {}).get("state") != "running":
        break
    time.sleep(0.3)
efilm = epdir / (st.get("render") or {}).get("file", "none")
ei = media.probe(efilm) if efilm.is_file() else {}
want = 2 + (0.4 + 3 + 0.8) + (0.4 + 4 + 0.8) + 3
check("  put together: each point on screen while it is said, the title card first, a short clip held on its last frame",
      abs(ei.get("seconds", 0) - want) < 0.35 and ei.get("codec") == "h264" and ei.get("width") == 1920
      and ei.get("has_audio") and st["renders"][-1].get("explainer"), (st.get("render"), ei, want))
level = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", "11.6", "-t", "1", "-i", str(efilm), "-af",
                        "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
check("  and the music under it all, quieter than the voices, where no one speaks",
      "mean_volume" in level and -60 < float(level.split("mean_volume:")[1].split()[0]) < -20, level[-300:])
check("  nothing of the work left behind", not [x for x in (epdir / "renders").iterdir() if x.name.startswith(".")])

print("\n  a point written by hand on its page, and an explainer that makes itself")
wp = c.post("/api/projects", json={"name": "Función inversa", "kind": "explainer"}, headers=HJ).json()
wdir = A.projects.dir(JUANA, wp["id"])
(wdir / "takes").mkdir(exist_ok=True)


def w_file(name, args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, str(wdir / "takes" / name)], check=True)
    return "takes/" + name


saved_w = c.put(f"/api/projects/{wp['id']}", json={"shots": [
    {"write": ["  Función   inversa ", "", "$f^{-1}(x) = \\frac{7x}{2}$", "x" * 300] + [f"l{i}" for i in range(10)],
     "seconds": 4},
    {"prompt": "a pencil resting on an open notebook", "seconds": 4}]}, headers=HJ).json()
w1, w2 = (x["id"] for x in saved_w["shots"])
from studio.handwriting import _mathtext  # noqa: E402
check("  what mathtext cannot read is said its way, and notation left among the words is written as a formula",
      _mathtext("Dom f^{-1} = $\\text{Rec} f$") == "Dom $f^{-1}$ = $\\mathrm{Rec} f$"
      and _mathtext("an odd $ sign") == "an odd  sign" and _mathtext("plain 2x + 3") == "plain 2x + 3",
      _mathtext("Dom f^{-1} = $\\text{Rec} f$"))
wl = saved_w["shots"][0]["write"]
check("  its lines are kept trimmed, empty ones dropped, eight at most",
      wl[0] == "Función inversa" and len(wl) == 8 and len(wl[2]) == 140 and wl[1].startswith("$f^"), wl)
c.put(f"/api/projects/{wp['id']}", json={"shots": [dict(saved_w["shots"][0], write=wl[:2]), saved_w["shots"][1]],
                                         "audio": [{"kind": "voice", "title": "1", "voice": "muestra.wav",
                                                    "text": "La inversa deshace lo que hace f.", "point": w1}]},
      headers=HJ)
notes = []
A._tell = lambda login, pid, text, ok: notes.append((text, ok))
r = c.post(f"/api/projects/{wp['id']}/explainer/auto", json={"size": "720"}, headers=HJ)
jobs = [A.store.get(i) for i in r.json().get("queued", [])]
check("  made by itself: the picture to draw and the narration to say are queued, nothing for the written page",
      r.status_code == 200 and sorted(j["kind"] for j in jobs) == ["board", "voice"]
      and not any(j["kind"] == "board" and j["target"] == w1 for j in jobs), (r.status_code, r.text[:300]))
board_job = next((j for j in jobs if j["kind"] == "board"), {})
voice_job = next((j for j in jobs if j["kind"] == "voice"), {})
check("  its frame is not held for the assistant's review", "refine" not in (board_job.get("params") or {}),
      board_job.get("params"))
wkey = f"{JUANA}/{wp['id']}"
check("  and the run is remembered on disk, for a restart in the middle", wkey in A._auto_load())
A.projects.add_take(JUANA, wp["id"], voice_job["target"],
                    {"file": w_file("n1.wav", ["-f", "lavfi", "-i", "sine=frequency=300:duration=2", "-ac", "2"])})
A.store.update(voice_job["id"], state="done", finished=time.time())
A._auto_step({**A.store.get(voice_job["id"]), "ok": True})
check("  one of two done: nothing is put together yet",
      wkey in A._auto_load() and (A.render_state.get(wkey) or {}).get("state") is None)
A.projects.add_board(JUANA, wp["id"], w2, {"file": w_file("w2.png", ["-f", "lavfi", "-i", "color=c=teal:s=640x360",
                                                                       "-frames:v", "1"])})
A.store.update(board_job["id"], state="done", finished=time.time())
A._auto_step({**A.store.get(board_job["id"]), "ok": True})
deadline = time.time() + 300
while time.time() < deadline and (A.render_state.get(wkey) or {}).get("state") == "running":
    time.sleep(0.3)
wst = A.render_state.get(wkey) or {}
wfilm = wdir / wst.get("file", "none")
wi = media.probe(wfilm) if wfilm.is_file() else {}
check("  the last one in: the film is put together by itself, the written page as long as its narration",
      wst.get("state") == "done" and abs(wi.get("seconds", 0) - ((0.4 + 2 + 0.8) + 4)) < 0.35
      and wi.get("width") == 1280, (wst, wi))
check("  and the person is told it is ready", any(ok and "video explicativo" in t for t, ok in notes), notes)
check("  the run is over", wkey not in A._auto_load())
page_png = tmp / "page.png"
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", "2.8", "-i", str(wfilm), "-frames:v", "1", str(page_png)],
               check=True)
from PIL import Image  # noqa: E402
with Image.open(page_png) as im:
    inked = sum(1 for px in im.convert("RGB").getdata() if px[2] > px[0] + 40 and px[0] < 90)
check("  and the page has its writing on it, in ink", inked > 500, inked)

with A._auto_lock:
    A._auto_save({wkey: {"format": "h264", "size": "720", "since": time.time()}})
A._auto_step({**A.store.get(board_job["id"]), "ok": False, "error": "out of memory"})
check("  a piece that cannot be made stops the run, and says which",
      wkey not in A._auto_load() and any(not ok and "se detuvo" in t and "out of memory" in t for t, ok in notes),
      notes[-1:])

print("\n  a song whose words could not be followed before is listened to again, once")
an_take = Projects.chosen_take(Projects.find(A.projects.load(JUANA, sp["id"]), intro)[2])
(spdir / "takes" / "old-analysis.json").write_text(json.dumps(
    {"aligned": False, "error": "could not follow the words: 500 Server Error", "lines": []}), encoding="utf-8")
A.projects.set_take_field(JUANA, sp["id"], intro, an_take["id"], "analysis",
                          {"file": "takes/old-analysis.json", "aligned": False})
r = c.post(f"/api/projects/{sp['id']}/items/{intro}/analyze", json={}, headers=HJ).json()
check("  an analysis from before the windows, without words, is queued again", bool(r.get("job")), r)
A.manager.cancel(r["job"]["id"]) if r.get("job") else None
(spdir / "takes" / "old-analysis.json").write_text(json.dumps(
    {"aligned": False, "error": "could not follow the words: 500", "lines": [], "version": analysis.VERSION}),
    encoding="utf-8")
r = c.post(f"/api/projects/{sp['id']}/items/{intro}/analyze", json={}, headers=HJ).json()
check("  but a current one is served as it is, so asking again does not loop",
      not r.get("job") and r.get("analysis"), r)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)

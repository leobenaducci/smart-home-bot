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

        def align(self, vocals, text, language, work):
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
    mgr.stop()

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
bd = Projects.chosen_board(next(x for x in c.get(f"/api/projects/{sbp['id']}", headers=h(JUANA, "Juana")).json()["shots"] if x["id"] == s1id))
rv = c.post(f"/api/projects/{sbp['id']}/items/{s1id}/boards/{bd['id']}/review",
            json={"review": {"score": 12, "ok": ["luz"], "problems": ["<b>mano</b>"] * 20, "prompt": "p", "round": 1}},
            headers=h(JUANA, "Juana")).json()
check("  a review is kept on its frame, shaped: a score out of ten, a few findings",
      rv["review"]["score"] == 10 and len(rv["review"]["problems"]) == 8 and rv["review"]["state"] == "done", rv.get("review"))
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
check("  nobody else can ask for someone's song",
      c.post(f"/api/projects/{pj['id']}/items/{sng['id']}/analyze", json={}, headers=h(TOMI, "Tomi")).status_code == 404)

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

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)

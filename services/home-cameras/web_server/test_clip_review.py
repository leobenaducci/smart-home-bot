"""Deciding whether a motion clip was worth recording.

Run: python web_server/test_clip_review.py

Most of what these cameras record is a cloud crossing the sun or a branch
moving. The motion detector cannot tell those from a person, so the recordings
folder fills with clips nobody will watch and the ones that matter are buried.

A finished clip now lands in `pending/`, one worker asks the vision model what
is in it, and the answer decides where it goes. What these pin down is
everything that could lose a recording, because that is the only failure that
cannot be undone:

- **A clip is never deleted for being boring.** "Probably nothing" is a
  judgement a model made about a dark frame; it goes to `to_review/` for a
  person, and only age empties that tray.
- **Except where the camera is only noticing itself, and that is held to its
  edges.** A scene that never changed is binned unwatched, because about a
  third of what these cameras record is that — and so are the four other ways
  the scene changes without anything happening: the burned-in clock ticking, a
  light switching, weather, an insect on the lens. What can never be binned is
  fixed in `BINNABLE_KINDS` and no setting reaches past it: person, animal,
  vehicle, object, and `other` — the model saying it saw something it could
  not name. Only the model's own words for those five — not "a cloud
  went over" — and not when the recorder's detector named a person or an animal
  in the frame it fired on. Every way of widening that is checked here, because
  each one costs recordings and none of them announces itself.
- **Anything the model cannot answer is kept.** A model that is down, timing
  out or talking nonsense must not file the house's recordings as shadows.
- **Only `to_review/` ages out.** A retention rule that can reach the month
  folders is unrecoverable, and this one is built so it cannot.
- **A broken clip is caught before it is filed**, because ffmpeg dying
  mid-encode leaves a plausible-looking file no player will open.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# The reviewer waits for a quiet GPU before it asks the vision model anything,
# and everything below this line is about *filing* -- which folder a verdict
# sends a clip to -- not about scheduling. Left on, these tests would sit on
# the real card of whatever machine runs them and file nothing until it went
# idle, which on a box with the detectors running is a suite that looks hung.
# The gate has its own tests at the bottom of this file, with the GPU faked.
#
# Assigned, not setdefault'd. The deployment sets CLIP_REVIEW_GPU_MAX_WAIT=900,
# so inside the container -- the one place these can run, since they need
# ffmpeg and the module's deps -- setdefault left the wait switched *on* and
# the suite became a coin toss: the filing checks time out waiting on a card
# the live detectors are using, and a different handful fails each run. Which
# is the exact failure the paragraph above says this line exists to prevent.
os.environ['CLIP_REVIEW_GPU_MAX_WAIT'] = '0'

import clip_review  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def have_ffmpeg():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=10)
        subprocess.run(['ffprobe', '-version'], capture_output=True, timeout=10)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


HAVE_FFMPEG = have_ffmpeg()
root = tempfile.mkdtemp(prefix="cams-review-")
clip_review.ensure_dirs(root)


def make_clip(name, seconds=1, broken=False):
    """A real playable mp4, or a file that only looks like one."""
    path = os.path.join(clip_review.pending_dir(root), name)
    if broken:
        with open(path, 'wb') as fh:
            fh.write(b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 4000)
        return path
    subprocess.run(
        ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi',
         '-i', f'testsrc=duration={seconds}:size=320x240:rate=5',
         '-pix_fmt', 'yuv420p', path], capture_output=True, timeout=60)
    return path


def month_path(filename):
    dest = os.path.join(root, "2026-08", filename)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    return dest


# The queue from the last run_queue(), for the checks that are about a clip
# that no longer exists — a deletion calls no callback and leaves no file, so
# its counter is the only thing left to look at.
last_queue = []


def run_queue(answer, name="2026-08-08_10-00-00_patio.mp4", broken=False, ask=True):
    """One clip through the worker, with the model stubbed to `answer`.

    `ask=False` leaves the stub alone, for the case where the queue must not
    consult a model at all.
    """
    make_clip(name, broken=broken)
    filed = []
    if ask:
        # Takes the filename too: the prompt is built from the tags in it,
        # so a stub of the old shape would TypeError on every clip.
        clip_review._ask_model = lambda frames, filename='': dict(answer)
    q = clip_review.ClipReviewQueue(root, month_path,
                                    on_filed=lambda n, d, v: filed.append((n, d, v)))
    del last_queue[:]
    last_queue.append(q)
    q.submit(name)
    for _ in range(400):                       # the worker is a real thread
        if filed or not os.path.exists(os.path.join(clip_review.pending_dir(root), name)):
            break
        time.sleep(0.02)
    time.sleep(0.05)
    return filed


def whereabouts(name):
    """Where that clip is now: 'shelf', 'tray', 'pending' or 'gone'."""
    if os.path.exists(month_path(name)):
        return 'shelf'
    if os.path.exists(os.path.join(clip_review.review_dir(root), name)):
        return 'tray'
    if os.path.exists(os.path.join(clip_review.pending_dir(root), name)):
        return 'pending'
    return 'gone'


KEEP = {'keep': True, 'what': 'a person in the patio', 'certainty': 'high', 'error': None}
SKIP = {'keep': False, 'what': 'only the light changes', 'certainty': 'high', 'error': None}

if not HAVE_FFMPEG:
    print("SKIP: ffmpeg/ffprobe are not installed here — this needs real video.")
    raise SystemExit(0)

# --- where a clip ends up -----------------------------------------------------
print("something happened: it goes on the shelf")
filed = run_queue(KEEP, "2026-08-08_10-00-00_patio.mp4")
check("it was filed", bool(filed), filed)
check("into the month folder", filed and "2026-08" in filed[0][1], filed)
check("and not left pending",
      not os.path.exists(os.path.join(clip_review.pending_dir(root),
                                      "2026-08-08_10-00-00_patio.mp4")))

print("\nnothing happened: it goes to the tray, never to the bin")
filed = run_queue(SKIP, "2026-08-08_11-00-00_patio.mp4")
check("into to_review", filed and clip_review.REVIEW_DIRNAME in filed[0][1], filed)
check("the file still exists", filed and os.path.isfile(filed[0][1]), filed)

print("\nand it says why, so the review page is not forty unexplained clips")
verdict = clip_review.read_verdict(filed[0][1])
check("there is a sidecar", verdict is not None)
check("with the model's words", verdict and verdict.get('what') == 'only the light changes', verdict)
check("and which model said it", verdict and verdict.get('model'), verdict)

# --- when the model cannot answer ---------------------------------------------
# A clip nobody could look at goes to the tray, not the shelf. It used to be
# kept — right that "the model is down" must never file a recording as boring,
# wrong about where that leaves it: kept means *judged interesting*, so it
# vanished into the month folder among clips a person chose, and the tray's
# "no se pudo revisar" line could never appear because nothing reached it.
# Nothing in the tray is deleted for being dull, only by age.
print("\nanything the model cannot answer goes to the tray, never quietly filed")
for label, answer in (
        ("ollama is down", {'keep': True, 'what': '', 'certainty': 'low',
                            'error': 'no pude preguntarle al modelo: refused'}),
        ("it answered nonsense", {'keep': True, 'what': 'blah', 'certainty': 'low',
                                  'error': 'el modelo no respondió en JSON'})):
    filed = run_queue(answer, f"2026-08-08_12-{len(failures):02d}-00_patio.mp4")
    check(f"  {label}: to the tray", filed and clip_review.REVIEW_DIRNAME in filed[0][1], filed)
    check(f"  {label}: and never deleted", filed and os.path.exists(filed[0][1]), filed)

# Review switched off is a *state*, not a failure. Reading it as one sent
# every recording in the house to the tray, where the fifteen-day purge would
# have deleted them all — armed and not yet firing, because nothing sets the
# variable today.
print("\nwith review switched off, everything is kept — nothing goes to the tray")
_was = clip_review.REVIEW_ENABLED
clip_review.REVIEW_ENABLED = False
try:
    filed = run_queue(None, "2026-08-08_12-80-00_patio.mp4", ask=False)
    check("  filed by month", filed and "2026-08" in filed[0][1]
          and clip_review.REVIEW_DIRNAME not in filed[0][1], filed)
    check("  and not counted as unreviewable",
          filed and filed[0][2].get("outcome") == "keep", filed and filed[0][2])
finally:
    clip_review.REVIEW_ENABLED = _was

print("\nand a real 'nothing happened' still goes to the tray as before")
filed = run_queue({'keep': False, 'what': 'leaves in the wind', 'certainty': 'high',
                   'error': None}, "2026-08-08_12-90-00_patio.mp4")
check("  shelved with its reason", filed and clip_review.REVIEW_DIRNAME in filed[0][1], filed)

print("\nwhile something worth keeping is still kept")
filed = run_queue({'keep': True, 'what': 'a person', 'certainty': 'high',
                   'error': None}, "2026-08-08_12-91-00_patio.mp4")
check("  filed by month", filed and "2026-08" in filed[0][1]
      and clip_review.REVIEW_DIRNAME not in filed[0][1], filed)

# --- the one thing that is binned unwatched -----------------------------------
# Everything above is about never losing a recording, and this is the single
# exception the house asked for: a clip where the scene is the same at the end
# as it was at the start. About a third of what these cameras record is that.
# It is deliberately narrow — only the model's own word for "nothing moved",
# never "a cloud went over" — and it is vetoed by the recorder's detector,
# which looked at full-resolution live frames rather than three stills.
STATIC = {'keep': False, 'what': 'nada nuevo o movido en la escena',
          'certainty': 'high', 'kind': 'static', 'error': None}

print("\na scene that never changed is binned, not added to the tray")
run_queue(STATIC, "2026-08-08_16-00-00_patio.mp4")
check("  it is gone", whereabouts("2026-08-08_16-00-00_patio.mp4") == 'gone',
      whereabouts("2026-08-08_16-00-00_patio.mp4"))
check("  and counted where somebody can see it",
      last_queue[0].deleted == 1, last_queue[0].deleted)
check("  without filling the tray",
      last_queue[0].to_review == 0, last_queue[0].to_review)

print("\nbut not when the recorder's own detector saw somebody")
# The two models are wrong about different things, and in this house's tray 28
# of 693 clips carry one of these tags. Those are precisely the clips where
# they disagree.
for name in ("2026-08-08_16-10-00_patio_person.mp4",
             "2026-08-08_16-11-00_patio_dog_car.mp4",
             "2026-08-08_16-12-00_patio_car_cat.mp4"):
    run_queue(STATIC, name)
    check(f"  {name.split('_patio_')[1][:-4]}: kept for a person",
          whereabouts(name) == 'tray', whereabouts(name))
# ...and a parked car is not somebody. It tags a quarter of the tray and would
# turn the veto into "never delete anything".
run_queue(STATIC, "2026-08-08_16-13-00_patio_car_car_car.mp4")
check("  but a parked car does not save it",
      whereabouts("2026-08-08_16-13-00_patio_car_car_car.mp4") == 'gone',
      whereabouts("2026-08-08_16-13-00_patio_car_car_car.mp4"))

print("\nand for the other ways a camera notices its own scene")
# These used to go to the tray and age out fifteen days later. A light
# switching, weather and an insect on the lens are the camera noticing itself,
# exactly as a burned-in clock is, and on this house's cameras the tray was
# filling at about seven hundred a week with them. Binned unwatched now --
# still only on an unhedged answer, still vetoed by the detector.
for i, (label, kind) in enumerate((
        ("a cloud", 'light'), ("branches", 'weather'), ("a moth", 'bug'))):
    # Numbered by the loop, not by the failure count: two iterations that
    # happened to share a name would find the *previous* clip sitting in the
    # tray and pass without having proved anything.
    name = f"2026-08-08_17-{i:02d}-00_patio.mp4"
    run_queue(dict(STATIC, kind=kind, what=label), name)
    check(f"  {label}: binned", whereabouts(name) == 'gone', whereabouts(name))

print("\nbut never when it could not say what it saw")
# `other` is the model reporting that it saw something and could not name it,
# which on a security camera is the most interesting answer there is -- the
# opposite of the five around it. A verdict with no `kind` at all is the same
# case wearing older clothes. Neither may ever be binned, and `BINNABLE_KINDS`
# is what makes that true regardless of configuration.
for i, (label, kind) in enumerate((
        ("something it could not name", 'other'),
        ("a verdict written before there were words", None))):
    name = f"2026-08-08_19-{i:02d}-00_patio.mp4"
    answer = dict(STATIC, kind=kind, what=label)
    if kind is None:
        del answer['kind']
    run_queue(answer, name)
    check(f"  {label}: to the tray", whereabouts(name) == 'tray', whereabouts(name))

print("\nnor when the model contradicts itself or never answered")
name = "2026-08-08_18-00-00_patio.mp4"
run_queue(dict(STATIC, keep=True), name)
check("  'nothing moved' but keep it: kept (see `clock` below for the "
      "claim that is trusted on its own)", whereabouts(name) == 'shelf',
      whereabouts(name))
name = "2026-08-08_18-01-00_patio.mp4"
run_queue(dict(STATIC, error='could not ask the model: refused'), name)
check("  'nothing moved' from a failed call: to the tray",
      whereabouts(name) == 'tray', whereabouts(name))

print("\nand the whole rule can be switched off")
_was_delete = clip_review.DELETE_STATIC
clip_review.DELETE_STATIC = False
try:
    name = "2026-08-08_19-00-00_patio.mp4"
    run_queue(STATIC, name)
    check("  the same clip goes to the tray instead", whereabouts(name) == 'tray',
          whereabouts(name))
finally:
    clip_review.DELETE_STATIC = _was_delete

# --- the clock in the corner --------------------------------------------------
# Reported from the page: clips reading "The clock in the corner changed from
# 20:30:38 to 20:30:53" were sitting on the shelf with a low-certainty flag.
# The model had seen exactly what it was — only the burned-in timestamp moved —
# and then hedged `keep: true`, because the prompt tells it to hedge toward
# keeping whenever it is unsure. The hedge swallowed the answer.
CLOCK = {'keep': True, 'what': 'The clock in the corner changed from 20:30:38 to 20:30:53',
         'certainty': 'low', 'kind': 'clock', 'error': None}

print("\na clip where only the clock changed is binned, hedge and all")
run_queue(CLOCK, "2026-08-08_20-30-38_patio.mp4")
check("  it is gone", whereabouts("2026-08-08_20-30-38_patio.mp4") == 'gone',
      whereabouts("2026-08-08_20-30-38_patio.mp4"))
# The distinction being made: "nothing moved" is a judgement about a whole
# scene and `keep` is a useful second opinion on it. "Only the clock
# changed" is a specific claim that cannot coexist with an event.
print("\nwhile 'nothing moved' still defers to the keep flag")
run_queue(dict(CLOCK, kind='static'), "2026-08-08_20-31-00_patio.mp4")
check("  a contradicted 'static' is kept", 
      whereabouts("2026-08-08_20-31-00_patio.mp4") == 'shelf',
      whereabouts("2026-08-08_20-31-00_patio.mp4"))

print("\nand the clock never overrides the things that protect a recording")
run_queue(dict(CLOCK, error='ollama answered 500'), "2026-08-08_20-32-00_patio.mp4")
check("  a failed review still goes to the tray",
      whereabouts("2026-08-08_20-32-00_patio.mp4") == 'tray',
      whereabouts("2026-08-08_20-32-00_patio.mp4"))
run_queue(CLOCK, "2026-08-08_20-33-00_patio_person.mp4")
# Not deleted is the property; where it lands afterwards is the ordinary
# keep/discard question, and this verdict says keep.
check("  and the detector's veto still wins",
      whereabouts("2026-08-08_20-33-00_patio_person.mp4") != 'gone',
      whereabouts("2026-08-08_20-33-00_patio_person.mp4"))
_was_del = clip_review.DELETE_STATIC
clip_review.DELETE_STATIC = False
try:
    run_queue(CLOCK, "2026-08-08_20-34-00_patio.mp4")
    check("  and switching the rule off still switches it off",
          whereabouts("2026-08-08_20-34-00_patio.mp4") != 'gone',
          whereabouts("2026-08-08_20-34-00_patio.mp4"))
finally:
    clip_review.DELETE_STATIC = _was_del

print("\nreading the tags out of a name")
check("  a plain clip has none", clip_review.living_tags_in(
    "2026-08-08_10-00-00_patio.mp4") == set())
check("  a person is found", clip_review.living_tags_in(
    "2026-08-08_10-00-00_patio_person.mp4") == {'person'})
check("  among three", clip_review.living_tags_in(
    "2026-08-08_10-00-00_patio_car_dog_car.mp4") == {'dog'})
# The camera part cannot contain an underscore — _safe_camera_id turns anything
# that is not a letter or a digit into a dash — so a camera called "Dog House"
# is "Dog-House" and cannot be read as a tag.
check("  and a camera named after one is not a tag", clip_review.living_tags_in(
    "2026-08-08_10-00-00_Dog-House.mp4") == set())
check("  a full path is fine too", clip_review.living_tags_in(
    "/a/b/2026-08-08_10-00-00_patio_cat.mp4") == {'cat'})

# --- a file that is not a video -----------------------------------------------
print("\na clip ffmpeg died halfway through is caught before it is filed")
name = "2026-08-08_13-00-00_patio.mp4"
filed = run_queue(KEEP, name, broken=True)
check("it is not on the shelf", not os.path.exists(month_path(name)))
check("nor in the tray",
      not os.path.exists(os.path.join(clip_review.review_dir(root), name)))
check("nor still pending",
      not os.path.exists(os.path.join(clip_review.pending_dir(root), name)))

print("\nand validate_clip names what is wrong with one")
tiny = os.path.join(clip_review.pending_dir(root), "tiny.mp4")
with open(tiny, "wb") as fh:
    fh.write(b"x" * 100)
check("an all-but-empty file", clip_review.validate_clip(tiny) is not None,
      clip_review.validate_clip(tiny))
os.remove(tiny)

plausible = make_clip("plausible.mp4", broken=True)   # right header, no video
check("a file that only looks like an mp4",
      clip_review.validate_clip(plausible) is not None,
      clip_review.validate_clip(plausible))
os.remove(plausible)

good = make_clip("good.mp4")
check("a real clip passes", clip_review.validate_clip(good) is None,
      clip_review.validate_clip(good))
os.remove(good)

check("and pending is empty again, so the next check counts what it makes",
      os.listdir(clip_review.pending_dir(root)) == [],
      os.listdir(clip_review.pending_dir(root)))

# --- the 15-day rule ----------------------------------------------------------
print("\nthe tray empties itself after the cutoff")
# Emptied first so the count below is exactly what this section makes. The
# clips the sections above filed here carry 2026-08-08 in their names, and the
# tray now ages by that name rather than by mtime -- so they are correctly
# past a fifteen day cutoff and would be swept in with these two.
for _leftover in os.listdir(clip_review.review_dir(root)):
    os.remove(os.path.join(clip_review.review_dir(root), _leftover))

old = os.path.join(clip_review.review_dir(root), "viejo.mp4")
new = os.path.join(clip_review.review_dir(root), "nuevo.mp4")
for path in (old, new):
    with open(path, 'wb') as fh:
        fh.write(b'x' * 2048)
long_ago = time.time() - 16 * 86400
os.utime(old, (long_ago, long_ago))
removed = clip_review.purge_old_reviews(root, max_age_days=15)
# Neither name carries a timestamp, so both fall back to mtime -- which is the
# fallback path, exercised on purpose.
check("the old one goes", not os.path.exists(old))
check("the recent one stays", os.path.exists(new))
check("and it says how many", removed == 1, removed)

# The name beats the mtime, which is the whole point: `shutil.move` within a
# filesystem is a rename, so a clip's mtime is when recording finished and
# survives every move. A copy or a restore rewrites it, and then a purge
# reading mtime keeps a stale tray for another fifteen days or empties it at
# once. What the clip says it is beats what the filesystem says it is.
stamped = os.path.join(clip_review.review_dir(root),
                       "2026-07-01_12-00-00_patio.mp4")
with open(stamped, 'wb') as fh:
    fh.write(b'x' * 2048)
with open(stamped + '.json', 'w') as fh:
    fh.write('{}')
os.utime(stamped, (time.time(), time.time()))       # "just written"
check("a clip old by its name goes despite a fresh mtime",
      clip_review.purge_old_reviews(root, max_age_days=15) == 1
      and not os.path.exists(stamped))
# The sidecar is written at filing time, so it is newer than its clip. Aged
# separately it outlives the video and the page renders a verdict for a
# recording that is not there.
check("and its verdict went with it", not os.path.exists(stamped + '.json'))

print("\nit cannot reach the month folders, whatever its cutoff")
shelf = month_path("2026-08-08_10-00-00_patio.mp4")
if not os.path.exists(shelf):
    with open(shelf, 'wb') as fh:
        fh.write(b'x' * 2048)
os.utime(shelf, (long_ago, long_ago))
clip_review.purge_old_reviews(root, max_age_days=1)
check("a month-old recording is untouched", os.path.exists(shelf))

print("\na cutoff of zero is 'never delete', not 'delete everything'")
with open(new, 'wb') as fh:
    fh.write(b'x' * 2048)
os.utime(new, (long_ago, long_ago))
check("nothing removed", clip_review.purge_old_reviews(root, max_age_days=0) == 0)
check("and it is still there", os.path.exists(new))

# --- restarts -----------------------------------------------------------------
print("\nclips left pending by a restart are picked up again")
make_clip("2026-08-08_14-00-00_patio.mp4")
make_clip("2026-08-08_15-00-00_patio.mp4")
q = clip_review.ClipReviewQueue(root, month_path)
check("both found", q.resume() == 2)

shutil.rmtree(root, ignore_errors=True)

# --- reading the model's reply ------------------------------------------------
# Found in production: qwen3-vl reasons before it answers, and Ollama returns
# that reasoning in its own field — for this model the answer lands there and
# `response` comes back empty. Reading only `response` meant every reply failed
# to parse, every clip fell through to "keep", and the queue quietly filed
# everything as interesting for weeks. It failed in the safe direction, which
# is exactly why nobody noticed.
print("\nthe answer is read wherever the model puts it")
import importlib
import json as _json
import urllib.request as _urlreq

# The sections above replace `_ask_model` with a stub, so without this these
# checks would quietly exercise the stub and pass no matter what the real one
# does — which is precisely the accident that let the production bug through.
clip_review = importlib.reload(clip_review)


class _Reply:
    def __init__(self, payload):
        self._raw = _json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _answering(payload):
    _urlreq.urlopen = lambda req, timeout=None: _Reply(payload)


GOOD = '{"keep": true, "what": "a person at the door", "certainty": "high"}'

_answering({"response": GOOD, "thinking": ""})
out = clip_review._ask_model([b"frame"])
check("plain `response` is read", out["keep"] and not out["error"], out)

_answering({"response": "", "thinking": GOOD})
out = clip_review._ask_model([b"frame"])
check("and so is a reasoning model's `thinking`", out["keep"] and not out["error"], out)
check("with what it saw", "person" in out["what"], out)

# A model that thinks out loud does not always stop at the closing brace.
_answering({"response": "", "thinking": "Let's see the image...\n" + GOOD + "\nThat is all."}) 
out = clip_review._ask_model([b"frame"])
check("even with talking around it", out["keep"] and not out["error"], out)

# The safety net still has to work: unparseable is "I could not tell", which
# keeps the clip. It must never read as a confident "boring".
_answering({"response": "", "thinking": "I have no idea at all"})
out = clip_review._ask_model([b"frame"])
check("and nonsense is still kept, not filed as boring", out["keep"], out)
check("and says it could not be read", out["error"], out)

_answering({"response": "", "thinking": ""})
out = clip_review._ask_model([b"frame"])
check("an empty reply is kept too", out["keep"] and out["error"], out)

# --- the word the page filters on, and the word that deletes -------------------
print("\nthe model names what it saw, in one word off a fixed list")
_answering({"response": '{"keep": true, "what": "somebody at the door",'
                        ' "certainty": "high", "kind": "person"}'})
out = clip_review._ask_model([b"frame"])
check("a word off the list is kept as written", out["kind"] == "person", out)

_answering({"response": '{"keep": false, "what": "all the same",'
                        ' "certainty": "high", "kind": "STATIC "}'})
out = clip_review._ask_model([b"frame"])
check("case and spacing do not change the answer",
      out["kind"] == clip_review.STATIC_KIND, out)

# Anything unrecognised becomes "other", and the only reason that matters is
# that "other" cannot delete a recording. The static case asks for the exact
# word rather than ruling the others out one at a time, so a vocabulary that
# grows later cannot quietly widen what gets binned.
for made_up in ('"a dog"', '"estatico"', '"Static!"', 'null', '42'):
    _answering({"response": '{"keep": false, "what": "x",'
                            ' "certainty": "high", "kind": %s}' % made_up})
    out = clip_review._ask_model([b"frame"])
    check(f"  kind {made_up} is not a category", out["kind"] == "other", out)
    check(f"  and {made_up} cannot bin a clip",
          not clip_review.is_just_a_static_scene(out, "2026-08-08_10-00-00_p.mp4"), out)

_answering({"response": '{"keep": false, "what": "x", "certainty": "high"}'})
out = clip_review._ask_model([b"frame"])
check("a reply with no kind at all is 'other' too", out["kind"] == "other", out)

# Every path out of _ask_model has to carry the field, or a reader that trusts
# it gets None from one branch and a word from the next.
_answering({"response": "", "thinking": "I have no idea at all"})
out = clip_review._ask_model([b"frame"])
check("and an unreadable reply names nothing rather than guessing",
      out["kind"] == "", out)
check("which is not the word that deletes",
      not clip_review.is_just_a_static_scene(out, "2026-08-08_10-00-00_p.mp4"), out)


# --- how much picture the model is sent ----------------------------------------
# Found in production: the cameras record 1920x1080, and three frames that size
# are ~4400 tokens against a 4096-token context. Every real clip came back
# "request exceeds the available context size", every one was therefore counted
# as unreviewable, and every one was kept — the filtering could not work and
# said nothing. The synthetic frames these tests used were small enough to fit,
# which is exactly why it survived them.
print("\nframes are cut down to something the model can hold")
import io as _io
import shutil as _shutil
import subprocess as _subprocess

if not _shutil.which("ffmpeg") or not _shutil.which("ffprobe"):
    print("  SKIP  no ffmpeg here")
else:
    try:
        from PIL import Image as _Image
    except ImportError:
        print("  SKIP  no Pillow here")
        _Image = None
    if _Image is not None:
        big = os.path.join(tempfile.mkdtemp(), "big.mp4")
        _subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
             "testsrc=size=1920x1080:rate=10:duration=3", "-pix_fmt", "yuv420p", big],
            check=True)
        frames = clip_review._extract_frames(big, 3)
        check("it still gets its frames", len(frames) == 3, len(frames))
        sizes = [_Image.open(_io.BytesIO(f)).size for f in frames]
        cap = clip_review.REVIEW_FRAME_PX
        check(f"none is wider or taller than {cap}",
              all(w <= cap and h <= cap for w, h in sizes), sizes)
        check("and the shape is kept, not squashed",
              all(abs((w / h) - (1920 / 1080)) < 0.02 for w, h in sizes), sizes)
        # The whole point: what goes on the wire is small enough to fit.
        check("the three together are well under a megabyte",
              sum(len(f) for f in frames) < 400_000,
              sum(len(f) for f in frames))


print("\na clip is shown one frame a second, between a floor and a cap")
# A fixed three put 7.5 s between frames on a 30 s clip, and somebody walking
# through is in view for about three: the clip read as empty, which is the kind
# that gets deleted.
for secs, want in ((0.5, 3), (2, 3), (3, 3), (12, 12), (15.9, 15),
                   (30, 30), (31, 30), (600, 30)):
    check(f"  {secs} s -> {want} frames", clip_review.frames_for(secs) == want,
          clip_review.frames_for(secs))
# Thirty frames at the frame size must still fit the window they are sent in,
# with room left to think and answer -- 32x32 px per token, as measured.
_per_frame = -(-clip_review.REVIEW_FRAME_PX // 32) * -(-(clip_review.REVIEW_FRAME_PX * 9 // 16) // 32)
check("  thirty frames leave half the window free",
      clip_review.REVIEW_MAX_FRAMES * _per_frame < clip_review.REVIEW_NUM_CTX // 2,
      (clip_review.REVIEW_MAX_FRAMES * _per_frame, clip_review.REVIEW_NUM_CTX))
if _Image is not None:
    twelve = os.path.join(tempfile.mkdtemp(), "twelve.mp4")
    _subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         "testsrc=size=640x360:rate=5:duration=12", "-pix_fmt", "yuv420p", twelve],
        check=True)
    check("  a 12 s clip comes out as 12 frames",
          len(clip_review._extract_frames(twelve)) == 12)


print("\nan unreadable `keep` must not be able to delete a recording")
# `bool(answer.get('keep', True))` said yes to anything truthy and no to
# anything else -- so a model answering `keep: ""` or `keep: 0` produced
# keep=False, and a `static` verdict with keep=False is exactly what
# is_just_a_static_scene deletes on. An answer nobody can read must not be able
# to remove a file.
for bad in ("", 0, "no", "false", None, [], {}):
    check(f"keep={bad!r} does not read as a decision to bin",
          clip_review.keep_from({"keep": bad}) is True,
          clip_review.keep_from({"keep": bad}))
# A real boolean is still honoured, both ways.
check("keep=True is kept", clip_review.keep_from({"keep": True}) is True)
check("keep=False is still a real answer",
      clip_review.keep_from({"keep": False}) is False)
check("a missing keep defaults to keeping", clip_review.keep_from({}) is True)

print("\nthe outcome branch, now that it is one function")
check("a real keep is still the shelf",
      clip_review.outcome_for(
          {"keep": True, "kind": "person", "what": "somebody", "certainty": "high",
           "error": None}, "cam_20260820_120000.mp4") == "keep")
check("an error is still unknown",
      clip_review.outcome_for(
          {"keep": True, "kind": "", "what": "", "certainty": "low",
           "error": "boom"}, "cam_20260820_120000.mp4") == "unknown")

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")


# --- what a security camera is for, and what it is not ------------------------
#
# `DELETE_KINDS` was `{static, clock}` and everything else the reviewer judged
# dull went to the tray to age out fifteen days later. On this house's cameras
# that tray was filling at about seven hundred a week, and the ones that are
# the camera noticing its own scene -- a light switching, weather, an insect on
# the lens -- are as binnable as a burned-in clock.
#
# The floor is the point of these checks. `person`, `animal`, `vehicle`,
# `object` and `other` can never be binned, whatever the environment says, and
# `other` matters most: it is the model saying it saw something it could not
# name, which on a security camera is the most interesting verdict there is.

check("the reviewer bins the scene-noticing kinds",
      clip_review.DELETE_KINDS
      == {'static', 'clock', 'light', 'weather', 'bug'},
      sorted(clip_review.DELETE_KINDS))
check("and never the ones a camera exists for",
      not (clip_review.BINNABLE_KINDS
           & {'person', 'animal', 'vehicle', 'object', 'other'}),
      sorted(clip_review.BINNABLE_KINDS))

# The hedge. The prompt tells the model to lean toward keeping when unsure, so
# `keep: true` arrives on plenty of clips whose `kind` is unambiguous. A
# specific claim -- "the only thing that changed is the clock" -- cannot be
# true at the same time as an event, so it is trusted hedge and all. "Nothing
# moved" is a judgement about the whole scene, and there `keep` is a real
# second opinion; the three new kinds stay hedged for want of measurement here.
_name = '2026-08-31_12-00-00_Front.mp4'
check("a hedged clock is still binned",
      clip_review.is_just_a_static_scene(
          {'kind': 'clock', 'keep': True, 'error': None}, _name))
check("a hedged static is not",
      not clip_review.is_just_a_static_scene(
          {'kind': 'static', 'keep': True, 'error': None}, _name))
check("an unhedged light is binned",
      clip_review.is_just_a_static_scene(
          {'kind': 'light', 'keep': False, 'error': None}, _name))
check("a hedged light is not",
      not clip_review.is_just_a_static_scene(
          {'kind': 'light', 'keep': True, 'error': None}, _name))
check("and `other` is never binned, hedge or no hedge",
      not clip_review.is_just_a_static_scene(
          {'kind': 'other', 'keep': False, 'error': None}, _name))
# Unchanged and worth restating beside the new kinds: the recorder's own
# detector vetoes the reviewer. Two models wrong about different things.
check("the detector still vetoes a bin",
      not clip_review.is_just_a_static_scene(
          {'kind': 'light', 'keep': False, 'error': None},
          '2026-08-31_12-00-00_Front_person.mp4'))


# --- the reviewer waits for a quiet card -------------------------------------
#
# Reviewing a clip is the lowest-priority inference in the house: the clip is
# already recorded and a verdict ten minutes late files it to the same place.
# Everything else on this GPU has somebody in front of it. What these pin is
# that waiting stays a *priority* and never becomes a veto -- on a box with no
# card to measure, and on an afternoon where the card never goes quiet, the
# clip still gets reviewed. A gate that can strand a recording is worse than
# no gate, because `to_review/` is purged on REVIEW_MAX_AGE_DAYS.
print("\nthe clip reviewer yields the GPU to whatever else is inferring")

# Switched back on for this block only -- the import above turned it off so the
# filing tests would not sit on a real card. Every GPU reading below is faked.
clip_review.REVIEW_GPU_MAX_WAIT_S = 900

_slept = []


def _fake_sleep(seconds):
    _slept.append(seconds)


def _reads(*values):
    """A GPU that reports each of these in turn, then repeats the last."""
    seen = list(values)

    def read():
        return seen.pop(0) if len(seen) > 1 else seen[0]
    return read


# 'idle' and 'quiet' are separate answers on purpose: the counter the queue
# keeps is "how often did this yield to something else", and folding a card
# that was never busy into it makes that number equal to the review count.
check("an idle card is not waited on",
      clip_review.wait_for_quiet_gpu(_reads(0), _fake_sleep) == 'idle')

_slept.clear()
check("a busy card is waited on until it frees up",
      clip_review.wait_for_quiet_gpu(_reads(100, 100, 0, 0, 0), _fake_sleep) == 'quiet')
check("  and it actually slept between samples", len(_slept) > 0, _slept)

# The measured reason this needs consecutive samples: the card is bimodal and
# bursty. The host read 100% and the container 0% four seconds apart, so one
# low sample in the middle of a chat turn is a coin flip, not an idle GPU.
_slept.clear()
check("one dip between two bursts is not a quiet card",
      clip_review.wait_for_quiet_gpu(
          _reads(100, 0, 100, 0, 0, 0), _fake_sleep) == 'quiet'
      and len(_slept) >= 3, _slept)


# The two ways this must never strand a clip.
check("a box that cannot measure its GPU reviews normally",
      clip_review.wait_for_quiet_gpu(_reads(None), _fake_sleep) == 'unknown')

_prev = clip_review.REVIEW_GPU_MAX_WAIT_S
try:
    clip_review.REVIEW_GPU_MAX_WAIT_S = 0
    check("and the wait can be switched off outright",
          clip_review.wait_for_quiet_gpu(_reads(100), _fake_sleep) == 'unknown')
finally:
    clip_review.REVIEW_GPU_MAX_WAIT_S = _prev

# A card the detectors keep busy all afternoon. The backstop has to fire, or
# every clip sits in pending/ until the tray purge takes it.
_slept.clear()
_clock = [0.0]


def _creeping_clock():
    _clock[0] += 60.0
    return _clock[0]


_real_monotonic = clip_review.time.monotonic
try:
    clip_review.time.monotonic = _creeping_clock
    check("a card that never goes quiet is reviewed anyway",
          clip_review.wait_for_quiet_gpu(_reads(100), _fake_sleep) == 'timeout')
finally:
    clip_review.time.monotonic = _real_monotonic

check("nvidia-smi returning nothing usable is 'cannot know', not 'busy'",
      clip_review.wait_for_quiet_gpu(lambda: None, _fake_sleep) == 'unknown')

# And the reading itself, against the shape nvidia-smi actually returns. Two
# cards where one is saturated is not a half-idle machine: this reviewer does
# not choose which card it lands on, ollama does.
_real_run = clip_review.subprocess.run


class _Done:
    def __init__(self, out, code=0):
        self.stdout, self.returncode = out, code


try:
    clip_review.subprocess.run = lambda *a, **k: _Done("0\n100\n")
    check("the busiest card is the one that counts",
          clip_review.gpu_utilisation() == 100)
    clip_review.subprocess.run = lambda *a, **k: _Done("", 9)
    check("a failed nvidia-smi is None, not zero",
          clip_review.gpu_utilisation() is None)
    clip_review.subprocess.run = lambda *a, **k: _Done("N/A\n")
    check("and so is a card that will not say",
          clip_review.gpu_utilisation() is None)

    def _boom(*a, **k):
        raise FileNotFoundError('nvidia-smi')
    clip_review.subprocess.run = _boom
    check("a box with no nvidia-smi at all is None",
          clip_review.gpu_utilisation() is None)
finally:
    clip_review.subprocess.run = _real_run

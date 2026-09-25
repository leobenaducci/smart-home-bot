"""Decide whether a motion clip was worth recording, before it is filed.

Most of what these cameras record is nothing. A cloud crosses the sun and the
whole patio changes brightness; a branch moves; a moth finds the IR lamp. The
motion detector cannot tell those from a person — it is looking at pixels
changing, and pixels changed — so the recordings folder fills with clips nobody
will ever watch, and the ones that matter are buried among them.

So a finished clip does not go straight to the shelf. It lands in `pending/`,
a single worker asks the vision model at `OLLAMA_URL` what is in it, and the
answer decides where it ends up:

    pending/  ──→  YYYY-MM/     something happened
              ├─→  to_review/   probably nothing, but a person should say
              └─→  (borrado)    nothing moved at all

One thing is deleted on the spot: a clip where *nothing moved at all*. Not "a
cloud went over" — those still go to the tray, because a shadow crossing a
garden and a person crossing it are the same argument. Only the scene that is
the same at the end as it was at the start, which is about a third of
everything these cameras record. Everything else keeps the old promise:
"probably nothing" is a judgement made by a model about a dark frame, and the
cost of it being wrong is the one clip somebody actually needed. `to_review/`
is the tray for that; what ages out of it is a separate, visible rule
(`REVIEW_MAX_AGE_DAYS`).

**One worker, on purpose.** The vision model runs on `compute`, which is also
this box and also the GPU the per-camera YOLO detectors use continuously and
the house's assistant uses for its own image turns. Two clips at once would
make both slower and take frames away from the detectors, which are the part
that must not fall behind.
"""

import base64
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional

import local_usage

logger = logging.getLogger(__name__)

# Supplied by the deployer from `cloud.ollama` — a role's address in local
# mode, ollama.com in cloud mode. The fallback is loopback rather than the
# `ollama.home` it used to be: an internal URL that needs DNS is the
# failure this stack has already had, where a container retrying `getaddrinfo
# ENOTFOUND` forever looks exactly like the service being down.
OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
# Ignored by a local Ollama, required by ollama.com.
OLLAMA_API_KEY = os.environ.get('OLLAMA_API_KEY', 'ollama')
REVIEW_MODEL = os.environ.get('CLIP_REVIEW_MODEL', 'qwen3-vl:8b')
# Off by default so a deployment that cannot reach Ollama keeps its recordings
# where they have always been rather than silently filling a review tray.
REVIEW_ENABLED = os.environ.get('CLIP_REVIEW_ENABLED', '1') not in ('0', 'false', '')
REVIEW_TIMEOUT_S = int(os.environ.get('CLIP_REVIEW_TIMEOUT', '180'))
REVIEW_MAX_AGE_DAYS = int(os.environ.get('CLIP_REVIEW_MAX_AGE_DAYS', '15'))
# How many frames of the clip the model sees: one a second, between a floor and
# a cap.
#
# It was a fixed three -- start, middle, end -- which tells a person from a
# shadow while the person stays in view, and misses one who crosses between
# samples. On a 30 s clip three frames are 7.5 s apart, somebody walking through
# is in view for about three, and a clip judged empty is the kind this deletes.
# At one a second nothing that lasts a second falls between frames, and one
# frame always means one second, whatever the clip's length.
#
# The cap is what the window holds (see REVIEW_FRAME_PX): past 30 s the thirty
# frames spread out rather than overrun it. The floor keeps a two-second clip at
# start, middle and end, which is what every clip got before.
REVIEW_FPS = float(os.environ.get('CLIP_REVIEW_FPS', '1'))
REVIEW_MIN_FRAMES = int(os.environ.get('CLIP_REVIEW_MIN_FRAMES', '3'))
REVIEW_MAX_FRAMES = int(os.environ.get('CLIP_REVIEW_MAX_FRAMES', '30'))
# Longest side of each frame handed to the model: 720p, either way up.
#
# Sized so thirty frames fit -- thirty seconds of clip at one frame a second.
# Qwen3-VL charges one token per 32x32-pixel block, which measured almost
# exactly on 2026-09-11 (1920x1080 ~2,040, 1296x2304 ~2,950, 1440x1920 ~2,700).
# A 1280x720 frame is ~900 after Ollama snaps it to that grid, so thirty come to
# ~27k: inside the 64k window below with room for the prompt, the reasoning and
# the verdict. At 1080p the same thirty would be ~61k and would not fit.
#
# It was 768 when the window was 4096, and too small a window is the failure
# that matters here: three 1080p frames overran it, *every* clip came back
# "request exceeds the available context size", was counted as unreviewable,
# and was therefore kept. The filtering could not work and the reason was
# invisible until the verdicts reached the page. Raise frames or pixels, and
# check the sum against REVIEW_NUM_CTX.
#
# Not rounded to 32 here: ffmpeg's force_divisible_by would squash 720 to 704,
# and Ollama rounds to its own grid anyway.
REVIEW_FRAME_PX = int(os.environ.get('CLIP_REVIEW_FRAME_PX', '1280'))
# Context to ask ollama for. The vision instance serves 65536 by default, and
# asking for the same thing matters as much as fitting: Ollama reloads a model
# whose requested context differs from the one it is loaded with, so a reviewer
# asking for 8192 beside photo turns at 65536 reloads the model on every switch.
REVIEW_NUM_CTX = int(os.environ.get('CLIP_REVIEW_NUM_CTX', '65536'))
# Bin a clip where nothing moved, instead of putting it in the tray. Set to 0
# and every discard goes to the tray as it always did.
DELETE_STATIC = os.environ.get('CLIP_REVIEW_DELETE_STATIC', '1') not in ('0', 'false', '')

# --- yielding the card to whatever else is inferring on it -------------------
#
# Reviewing a clip is the lowest-priority inference in the house. Nobody is
# waiting on it: the clip is already recorded, already on disk, and a verdict
# ten minutes late files it to exactly the same place. Everything else that
# touches this GPU has somebody in front of it -- a person mid-sentence with
# the assistant, the per-camera detectors that must not fall behind -- so the
# reviewer waits for a quiet card rather than competing for one.
#
# Measured on this box, 60 samples over 30 seconds: the card is bimodal. 40% of
# samples sit at 0-10% and 45% at 90-100%, with almost nothing in between. So a
# threshold separates the two modes cleanly -- and a *single* sample is a coin
# flip, which is why several consecutive ones are required. The host read 100%
# and the container read 0% four seconds apart while this was being written.
REVIEW_GPU_BUSY_PCT = int(os.environ.get('CLIP_REVIEW_GPU_BUSY_PCT', '25'))
# Consecutive quiet samples before the card counts as free. One dip between two
# bursts of the same chat turn is not an idle GPU.
REVIEW_GPU_SAMPLES = int(os.environ.get('CLIP_REVIEW_GPU_SAMPLES', '3'))
REVIEW_GPU_GAP_S = float(os.environ.get('CLIP_REVIEW_GPU_GAP', '2'))
# ...and the backstop. Waiting is a priority, not a veto: the detectors use this
# card continuously, so "quiet" is a window that may never open on a busy
# afternoon. After this long the clip is reviewed anyway, because the tray it
# would otherwise sit in is purged on REVIEW_MAX_AGE_DAYS and a clip nobody
# ever looked at is a worse outcome than a slow one. 0 disables the wait.
REVIEW_GPU_MAX_WAIT_S = int(os.environ.get('CLIP_REVIEW_GPU_MAX_WAIT', '900'))


def gpu_utilisation() -> Optional[int]:
    """Busiest GPU on this box, 0-100, or None if that cannot be known.

    None is the important return. A household with no card, a container
    without the GPU overlay, an nvidia-smi that is missing or times out -- all
    of them have to mean "review normally", never "wait forever". The whole
    point of this gate is to be polite to other work; a gate that stops the
    reviewer on a machine it cannot measure is not politeness, it is an outage
    in the shape of a feature.
    """
    try:
        done = subprocess.run(
            ['nvidia-smi', '--query-gpu=utilization.gpu',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    seen = [int(line.strip()) for line in done.stdout.splitlines()
            if line.strip().isdigit()]
    # The busiest card, not the average: two GPUs where one is saturated is not
    # a half-idle machine, and this reviewer does not choose which one it lands
    # on -- ollama does.
    return max(seen) if seen else None


def wait_for_quiet_gpu(is_busy=gpu_utilisation, pause=time.sleep) -> str:
    """Hold until the card is free, and say what happened.

    Four answers, and the first two are deliberately not one:

      'idle'     no sample was ever above the threshold, so nothing was
                 yielded to and nothing was waited for.
      'quiet'    the card *was* busy and then went quiet -- a real wait.
      'timeout'  it never went quiet and the backstop fired.
      'unknown'  nothing to measure, so proceed.

    Telling 'idle' from 'quiet' is the whole value of the counter the caller
    keeps: folded together they make every review on a working card look like
    a wait, and the number the settings page would then show is just the
    review count under another name.

    The return is for the log and the counters, not for the caller to branch
    on: every one of them ends in the clip being reviewed.
    """
    if REVIEW_GPU_MAX_WAIT_S <= 0:
        return 'unknown'
    deadline = time.monotonic() + REVIEW_GPU_MAX_WAIT_S
    quiet, was_busy = 0, False
    while quiet < REVIEW_GPU_SAMPLES:
        used = is_busy()
        if used is None:
            return 'unknown'
        if used <= REVIEW_GPU_BUSY_PCT:
            quiet += 1
        else:
            quiet, was_busy = 0, True
        if quiet >= REVIEW_GPU_SAMPLES:
            break
        if time.monotonic() >= deadline:
            logger.info(
                "clip review: the GPU stayed above %d%% for %ds, reviewing "
                "anyway rather than letting the clip age out",
                REVIEW_GPU_BUSY_PCT, REVIEW_GPU_MAX_WAIT_S)
            return 'timeout'
        pause(REVIEW_GPU_GAP_S)
    return 'quiet' if was_busy else 'idle'


PENDING_DIRNAME = 'pending'
REVIEW_DIRNAME = 'to_review'

# What the clip is *of*, in one word, so the page can be filtered by it.
#
# The verdict already carried a sentence, and a sentence cannot be filtered:
# "a new vehicle behind the wall", "black vehicle parked in the garden" and
# "a vehicle in the garage and another in the street" are three ways of
# writing the same word, and somebody looking for the clip where a person came
# to the door has to read all of them. The first four are reasons to keep; the
# rest are reasons not to.
KINDS = frozenset(('person', 'animal', 'vehicle', 'object',
                   'static', 'clock', 'light', 'weather', 'bug', 'other'))
# The vocabulary used to be Spanish, and it is written into every clip's
# metadata on disk rather than recomputed, so a tray filled before the rename
# still holds these. Applied when a stored verdict is read, never when one is
# written: the model is asked for the English words now.
KIND_ALIASES = {
    'persona': 'person', 'vehiculo': 'vehicle', 'objeto': 'object',
    'estatico': 'static', 'reloj': 'clock', 'luz': 'light',
    'clima': 'weather', 'bicho': 'bug', 'otro': 'other',
}
CERTAINTY_ALIASES = {'alta': 'high', 'media': 'medium', 'baja': 'low'}


def normalise_verdict(verdict: dict) -> dict:
    """A stored verdict in today's vocabulary.

    Deliberately not in-place and deliberately tolerant: an unknown word is
    left alone rather than mapped to anything, because the one thing this must
    never do is turn a word it does not recognise into `static` and hand a
    recording to the deleter.
    """
    if not isinstance(verdict, dict):
        return verdict
    out = dict(verdict)
    kind = str(out.get('kind') or '')
    certainty = str(out.get('certainty') or '')
    out['kind'] = KIND_ALIASES.get(kind, kind)
    out['certainty'] = CERTAINTY_ALIASES.get(certainty, certainty)
    return out
# The two that are binned rather than trayed. Named here, because these are the
# only values in the vocabulary that cost a recording if the model is wrong
# about them, and a bare string literal three functions away is how that gets
# widened by accident.
#
# `clock` is its own word rather than a flavour of `static` because it is a
# different kind of claim. "Nothing moved" is a judgement about a whole scene
# and deserves the `keep` flag as a second opinion. "The only thing that
# changed is the clock burned into the corner" is a specific, checkable
# statement that cannot be true at the same time as somebody walking past — so
# it is trusted on its own, which is what makes it survive the hedge below.
STATIC_KIND = 'static'
CLOCK_KIND = 'clock'

# What a security camera is for, and what it is not. `KINDS` above splits into
# four things worth a person's attention -- person, animal, vehicle, object --
# and six that are the camera noticing its own scene: nothing moved, the
# burned-in clock ticked, a light switched, weather, an insect on the lens, and
# `other`.
#
# `other` is deliberately NOT here and must not be added. It is the catch-all
# the model reaches for when it saw something it could not name, and on a
# security camera an unnameable something is the most interesting verdict there
# is -- the opposite of the ones around it. The rule this file already states
# for unknown words applies to the known word that *means* unknown.
# The floor, and it is not configurable. `person`, `animal`, `vehicle` and
# `object` are the reasons this camera exists; a setting able to bin them is a
# setting somebody eventually gets wrong, and the clip is the only copy. So the
# env var may narrow this set and can never widen it past here.
BINNABLE_KINDS = frozenset((STATIC_KIND, CLOCK_KIND, 'light', 'weather', 'bug'))

DELETE_KINDS = frozenset(
    k.strip().lower()
    for k in os.environ.get(
        'CLIP_REVIEW_DELETE_KINDS',
        ','.join(sorted(BINNABLE_KINDS))).split(',')
    if k.strip()) & BINNABLE_KINDS

# Kinds trusted over the model's own hedge. The prompt tells it to lean toward
# keeping whenever unsure, so `keep: true` arrives on plenty of clips whose
# `kind` is an unambiguous statement of what changed -- and for a *specific*
# claim the hedge is not a second opinion, it is noise. "The only thing that
# changed is the clock in the corner" cannot be true at the same time as
# somebody walking past.
#
# Only `clock` by default, which is where this started. `static` stays hedged
# because "nothing moved" is a judgement about a whole scene and `keep` is a
# real second opinion on it, and the three new kinds stay hedged because they
# have no measured record here yet. Widen it deliberately, per household,
# after looking at what the tray actually filled with.
HEDGE_TRUSTED_KINDS = frozenset(
    k.strip().lower()
    for k in os.environ.get('CLIP_REVIEW_TRUST_OVER_HEDGE', CLOCK_KIND).split(',')
    if k.strip()) & DELETE_KINDS

# COCO classes that are somebody rather than something. The recorder writes
# whatever the per-camera detector saw at the trigger into the filename, so
# these are a second opinion from a different model looking at a different
# thing — full-resolution live frames, rather than three downscaled stills.
#
# Only ever used to *veto* a deletion. In this house's tray, 28 of 693 clips
# carry one of these tags: exactly the clips where the two judges disagree,
# which are the last ones that should go without a person seeing them.
# Vehicles are deliberately absent — a parked car is in frame all day and tags
# a quarter of the tray.
LIVING_TAGS = frozenset(('person', 'dog', 'cat', 'bird', 'horse', 'sheep',
                         'cow', 'elephant', 'bear', 'zebra', 'giraffe'))

# Everything the recorder's detector can name, vehicles included. This answers a
# different question from LIVING_TAGS -- that one decides what may veto a
# deletion, and leaves vehicles out because a parked car is in frame all day.
# This one is only ever *told* to the model, so a car belongs in it: knowing the
# detector found a car is exactly what lets the model rule on whether the car
# did anything.
DETECTOR_TAGS = LIVING_TAGS | frozenset(
    ('car', 'truck', 'bus', 'motorcycle', 'bicycle'))


def detector_tags_in(filename: str) -> list:
    """Everything the recorder's detector named in this clip, in order, once each.

    Same filename grammar `living_tags_in` reads --
    `YYYY-MM-DD_HH-MM-SS_<camera>[_tag...]`, camera never containing an
    underscore -- so everything from the fourth field on is a class name.
    """
    parts = os.path.splitext(os.path.basename(filename))[0].split('_')
    named = [p.lower() for p in parts[3:] if p.lower() in DETECTOR_TAGS]
    return list(dict.fromkeys(named))

# What the model is asked to look for, and what it must not be fooled by. The
# second list is the whole reason this exists, and it is written in the words
# the house used to describe the problem.
_PROMPT = """These are images from a house security camera, in order.
{detector}
The question is what CHANGED between one image and the next, not what is in
them. A parked car that is still parked is not something that happened, however
big it looks.

Is something happening that is worth keeping?

It IS worth it if: a person, an animal (dog, cat, bird) or a vehicle arrives,
passes or moves; a package or a bundle appears that was not there before; a
door or a gate opens or closes; or something moves by itself.

It is NOT worth it if: everything visible was already there and stayed the same
— a parked vehicle that did not move, a chair, a plant —; or if the only thing
that changes is the light (a cloud, the sun, a lamp switching on), moving
shadows, leaves or branches in the wind, rain or drops on the lens, insects
near the camera, image noise or grain at night, or the clock changing in the
corner.

Answer ONLY with this JSON, nothing else:
{"keep": true|false, "what": "<what you see, in under 12 words>", "certainty": "high"|"medium"|"low", "kind": "<one word from the list>"}

"kind" is ONE single word from this list, whichever best describes the clip:

  person   — somebody arrives, passes or is there
  animal   — a dog, a cat, a bird moves
  vehicle  — a car arrives, leaves or moves; NOT one parked that stays parked
  object   — a new bundle or package, a door or a gate that moved
  static   — absolutely nothing moved; the scene is the same at the end as at
             the beginning, even if there are cars or things in the frame
  clock    — the ONLY thing that changed was the time or the corner clock
  light    — the only thing that changed was the light or the shadows
  weather  — rain, drops on the lens, wind, leaves or branches
  bug      — an insect near the lens
  other    — anything else

Clips marked "static" and "clock" are deleted automatically, so those words
have their own rules:

- If the ONLY thing that changed is the time or the corner clock, that is
  "clock", not "light" and not "static": the clock is not light, and saying
  "clock" is more precise.
- Never use "static" or "clock" if a person or an animal is visible, even if
  they are still. In that case the kind is "person" or "animal".
- A car that "moved a little" or "slightly" and is still in the same place did
  NOT move: that is image noise. That is "static". A car moved when it
  arrived, left, or is somewhere else in the frame at the end.
- If the light changed it is "light", if the leaves moved it is "weather", and
  if you are torn between "static" and another, choose the other.

If in doubt, answer "keep": true with certainty "low" — somebody taking a look
is far cheaper than losing something that mattered."""


# What the detector found, written into the prompt so the model is answering a
# question about *something*, rather than staring at three stills and guessing.
#
# Nothing reaches the reviewer now without a person, vehicle or animal tracked
# across three frames, so this is nearly always a real list -- and telling the
# model what was found is what stops the two most common junk answers: naming
# scenery ("a small object moved on the ground") and narrating light ("shadow of
# tree moved across patio"). Neither is an event and neither was ever the
# question.
_FOUND = """
The camera's own object detector has already been through these frames and
followed each of these across several frames, so they really are in the clip:

    {tags}

LOOK AT THOSE, and answer one question about them: did they DO something, or
were they merely present? A person who walks in is an event; a parked car that
is still parked is not, however big it looks.

IGNORE EVERYTHING ELSE. The detector names people, vehicles and animals and
nothing else, so anything it did not name is scenery no matter what it looks
like: plants, branches and leaves; the date and time burned into the corner or
down the edge of the picture; chairs, tables, pots, washing; shadows and
changing light; rain or drops on the lens; insects near the lens; grain at
night. None of those is something happening. Do not report them as what you
saw, and never keep a clip for them.
"""

# The other half. The gate means this is now rare and usually an old clip, so it
# says so plainly rather than implying the frames are empty -- a detector that
# found nothing is evidence, but it is not proof, and the cost of being wrong
# here is the one recording somebody needed.
_FOUND_NOTHING = """
The camera's own object detector went through these frames and found no person,
vehicle or animal in them.

That is not the last word -- it can miss somebody in the dark, or at the far end
of a garden -- so look for one yourself. But it does mean the burden runs the
other way: say a person or an animal is here only if you can actually see one.

IGNORE THE SCENERY, which is what these clips are usually full of: plants,
branches and leaves; the date and time burned into the corner or down the edge
of the picture; chairs, tables, pots, washing; shadows and changing light; rain
or drops on the lens; insects near the lens; grain at night. None of those is
something happening, and none of them is a reason to keep a clip.
"""


def prompt_for(filename: str) -> str:
    """The review prompt, with what the detector found written into it."""
    tags = detector_tags_in(filename)
    # str.replace, not str.format: the prompt below carries a literal JSON
    # example, and every brace in it would be a format field. `.format()` here
    # raises KeyError('"keep"') on the answer template -- which is the one
    # part of the prompt that must never change.
    detector = (_FOUND.replace('{tags}', ', '.join(tags)) if tags
                else _FOUND_NOTHING)
    return _PROMPT.replace('{detector}', detector)


def pending_dir(recordings_dir: str) -> str:
    return os.path.join(recordings_dir, PENDING_DIRNAME)


def review_dir(recordings_dir: str) -> str:
    return os.path.join(recordings_dir, REVIEW_DIRNAME)


def ensure_dirs(recordings_dir: str) -> None:
    for path in (pending_dir(recordings_dir), review_dir(recordings_dir)):
        os.makedirs(path, exist_ok=True)


# --- is this even a video -----------------------------------------------------

def validate_clip(path: str) -> Optional[str]:
    """None if the file is a playable video, otherwise why it is not.

    Checked because a clip is written by an ffmpeg that can die mid-encode — the
    camera drops, the process is killed, the disk fills — and what it leaves
    behind is a file with a plausible name and a plausible size that no player
    will open. Those used to sit in the month folder forever, indistinguishable
    from real recordings until somebody clicked one.
    """
    try:
        if os.path.getsize(path) < 1024:
            return "the file is empty or nearly so"
    except OSError as e:
        return f"no pude leerlo: {e}"
    try:
        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_name,nb_read_packets',
             '-count_packets', '-of', 'json', path],
            capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        # No ffprobe in this image: the size check above is all we get, and a
        # missing tool must not condemn every recording.
        logger.warning("ffprobe not found; clips are only size-checked")
        return None
    except subprocess.TimeoutExpired:
        return "ffprobe did not finish reading it"
    if probe.returncode != 0:
        return f"no es un video legible: {probe.stderr.strip()[:120]}"
    try:
        streams = json.loads(probe.stdout or '{}').get('streams') or []
    except ValueError:
        return "ffprobe returned something unreadable"
    if not streams:
        return "no tiene pista de video"
    if int(streams[0].get('nb_read_packets') or 0) < 1:
        return "no tiene ni un cuadro"
    return None


# --- asking the model ---------------------------------------------------------

def frames_for(duration: float) -> int:
    """How many frames a clip of `duration` seconds is shown as."""
    return max(REVIEW_MIN_FRAMES, min(REVIEW_MAX_FRAMES, int(duration * REVIEW_FPS)))


def _extract_frames(path: str, count: Optional[int] = None) -> List[bytes]:
    """JPEGs spread across the clip, as bytes: `count` of them, or by default
    as many as `frames_for` gives the clip's length.

    Spread rather than the first N: a clip starts with the pre-motion buffer,
    so its first second is by definition the scene *before* anything happened.
    """
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', path],
            capture_output=True, text=True, timeout=30)
        duration = float((out.stdout or '0').strip() or 0)
    except Exception:
        duration = 0.0
    if duration <= 0:
        duration = 10.0
    if count is None:
        count = frames_for(duration)
    frames: List[bytes] = []
    for i in range(count):
        # Evenly spaced, avoiding the very first and last instant — the last
        # frame of an encode is the one most likely to be half-written.
        at = duration * (i + 1) / (count + 1)
        try:
            shot = subprocess.run(
                ['ffmpeg', '-v', 'error', '-ss', f'{at:.2f}', '-i', path,
                 '-frames:v', '1', '-q:v', '4',
                 # Both sides capped, aspect kept. `decrease` never enlarges,
                 # so a camera already smaller than this is left alone, and a
                 # portrait one is capped on its long side rather than blown
                 # up sideways.
                 '-vf', f'scale=w={REVIEW_FRAME_PX}:h={REVIEW_FRAME_PX}:'
                        'force_original_aspect_ratio=decrease',
                 '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'],
                capture_output=True, timeout=60)
        except Exception as e:
            logger.warning("could not pull a frame at %.1fs from %s: %s", at, path, e)
            continue
        if shot.returncode == 0 and shot.stdout:
            frames.append(shot.stdout)
    return frames


def _plain_error(code: int, detail: str) -> str:
    """A sentence a person can act on, not a pasted JSON body.

    These end up under a recording on the review page, where "ollama 400:
    {\"error\":{\"code\":400,\"message\":\"request (4365 tokens)..." tells
    the reader nothing they can do anything about.
    """
    lower = detail.lower()
    if 'context' in lower:
        return 'the images do not fit the model'
    if code == 404:
        return f'the model {REVIEW_MODEL} is not installed in ollama'
    if code >= 500:
        return 'ollama failed while looking at the clip'
    return f'ollama answered {code}'


def _json_block(raw: str):
    """The JSON object in `raw`, or None.

    A reasoning model does not always stop at the closing brace, so this takes
    the first balanced object rather than insisting the whole reply is JSON.
    Being strict here is what turns a good answer into "no lo pude revisar".
    """
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        found = json.loads(raw)
        return found if isinstance(found, dict) else None
    except ValueError:
        pass
    start = raw.find('{')
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(raw[start:], start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                try:
                    found = json.loads(raw[start:i + 1])
                except ValueError:
                    return None
                return found if isinstance(found, dict) else None
    return None


def _unsure(what: str, error: str) -> dict:
    """The answer when there is no answer.

    `kind` is empty rather than any word from the vocabulary: nothing looked at
    this clip, so naming what is in it would be an invention, and one of those
    words gets clips deleted.
    """
    return {'keep': True, 'what': what, 'certainty': 'low', 'kind': '',
            'error': error}


def _ask_model(frames: List[bytes], filename: str = '') -> dict:
    """{'keep': bool, 'what': str, 'certainty': str, 'kind': str, 'error': str|None}.

    An error is *not* a "no". Anything this cannot decide is kept — the model
    being down is a reason to look at a clip yourself, never a reason to file it
    as boring.
    """
    import urllib.error
    import urllib.request

    body = json.dumps({
        'model': REVIEW_MODEL,
        'prompt': prompt_for(filename),
        'images': [base64.b64encode(f).decode() for f in frames],
        'stream': False,
        'format': 'json',
        'options': {'temperature': 0, 'num_ctx': REVIEW_NUM_CTX},
    }).encode()
    headers = {'Content-Type': 'application/json'}
    # A local Ollama takes no auth and ignores the header; ollama.com refuses
    # without it. Sent whenever the key is not the local placeholder, so the
    # same code path serves both modes and neither has to know which is on.
    if OLLAMA_API_KEY and OLLAMA_API_KEY != 'ollama':
        headers['Authorization'] = f'Bearer {OLLAMA_API_KEY}'
    req = urllib.request.Request(f'{OLLAMA_URL}/api/generate', data=body,
                                 headers=headers)
    # What the card actually did, for the usage page. `units` is the frame
    # count, because that is what a household changes when a review gets slow
    # (CLIP_REVIEW_FPS, CLIP_REVIEW_MAX_FRAMES) and a per-clip time alone does
    # not say whether it was three frames or thirty.
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=REVIEW_TIMEOUT_S) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        # Too much picture for the model to hold. Frames are capped so this
        # should not happen — but it is the failure that silently disabled the
        # whole feature once, so it gets one go with a single frame rather than
        # being reported as "could not review" for every clip in the house.
        if e.code == 400 and 'context' in detail and len(frames) > 1:
            logger.warning("clip review: %d frames did not fit; retrying with one",
                           len(frames))
            # One clip is one review. The retry records its own outcome, so
            # the failed first attempt must not also be recorded: a review
            # that succeeded on the second try would otherwise appear as a
            # failure *and* a success, inflating both the call count and the
            # failure rate for the exact condition this retry exists to
            # absorb. The same rule the voice smoke test follows -- retry
            # noise you have measured, never a failure.
            return _ask_model(frames[len(frames) // 2:][:1], filename)
        local_usage.record('vision', 'ollama', REVIEW_MODEL,
                           (time.monotonic() - started) * 1000,
                           len(frames), ok=False, route='clip-review')
        return _unsure('', _plain_error(e.code, detail))
    except Exception as e:
        local_usage.record('vision', 'ollama', REVIEW_MODEL,
                           (time.monotonic() - started) * 1000,
                           len(frames), ok=False, route='clip-review')
        return _unsure('', f'could not ask the model: {e}')
    # Ollama's own timings, in nanoseconds, when it reports them. Preferred
    # over the wall clock because it excludes the queue: two clips arriving
    # together would otherwise show the second one taking twice as long as the
    # model spent on it, which reads as the model getting slower under load
    # when what happened is that it was busy.
    total_ns = payload.get('total_duration')
    ms = (total_ns / 1e6 if isinstance(total_ns, (int, float)) and total_ns > 0
          else (time.monotonic() - started) * 1000)
    local_usage.record('vision', 'ollama', REVIEW_MODEL, ms, len(frames),
                       route='clip-review')
    # `response` *or* `thinking`. qwen3-vl reasons before it answers, and this
    # Ollama hands the reasoning back in its own field — for this model that
    # field is where the answer actually lands, with `response` left empty.
    # Reading only `response` meant every clip failed to parse and fell through
    # to "keep", so the queue ran for weeks filing everything as interesting:
    # no wrong clips deleted, and no clips filtered either.
    raw = (payload.get('response') or '').strip()
    if not raw:
        raw = (payload.get('thinking') or '').strip()
    answer = _json_block(raw)
    if answer is None:
        return _unsure(raw[:80], 'the model did not answer in JSON')
    # A word off the list or nothing at all becomes "other", never "static":
    # an unrecognised answer must not be able to delete a recording, and the
    # only way to be sure of that is for the static case to require the exact
    # word rather than for the others to be excluded one at a time.
    kind = str(answer.get('kind') or '').strip().lower()
    return {
        'keep': keep_from(answer),
        'what': str(answer.get('what') or '')[:120],
        'certainty': str(answer.get('certainty') or 'low')[:10],
        'kind': kind if kind in KINDS else 'other',
        'error': None,
    }


def review_clip(path: str) -> dict:
    """Look at one clip and say whether it is worth keeping."""
    frames = _extract_frames(path)
    if not frames:
        return _unsure('', 'no pude sacarle ni un cuadro')
    return _ask_model(frames, os.path.basename(path))


def living_tags_in(filename: str) -> set:
    """The living things the recorder's own detector saw when it hit record.

    `YYYY-MM-DD_HH-MM-SS_<camera>[_tag[_tag[_tag]]].mp4`, and the camera part
    cannot contain an underscore — `_safe_camera_id` turns anything that is not
    a letter or a digit into a dash — so everything from the fourth field on is
    a class name.
    """
    parts = os.path.splitext(os.path.basename(filename))[0].split('_')
    return LIVING_TAGS.intersection(p.lower() for p in parts[3:])


def keep_from(answer: dict) -> bool:
    """The model's `keep`, and only if it actually answered the question.

    This was `bool(answer.get('keep', True))`, which says yes to anything
    truthy and *no* to anything else -- so `keep: ""`, `keep: 0` and a missing
    JSON value all came out False. False on a `static` verdict is exactly what
    is_just_a_static_scene deletes on, so an answer nobody could read was able
    to remove a recording.

    Same rule the kind vocabulary already follows, pointing the same way: an
    unrecognised answer must not be able to delete a clip. A word that is not
    a boolean is not an answer, and not answering means keep.
    """
    keep = answer.get('keep', True)
    return keep if isinstance(keep, bool) else True


def outcome_for(verdict: dict, filename: str) -> str:
    """Where a reviewed clip goes: 'unknown', 'delete', 'keep' or 'discard'.

    Asked in this order on purpose. The bin used to be reachable only from
    `discard`, so a verdict that named the clock and then hedged `keep: true` --
    which the prompt asks it to do whenever unsure -- never reached the question
    at all.

    `is_just_a_static_scene` is the one place that decides on the bin, and it
    already refuses on an error and on a hedge.

    A hedged `static` then defers to the keep flag and goes to the shelf, and
    that is deliberate rather than an oversight: "nothing moved" is a judgement
    about a whole scene, and `keep` is a useful second opinion on it. "Only the
    clock changed" is a specific claim that cannot coexist with an event, which
    is why `clock` is binned hedge and all. The two are not the same question.

    (PORT_PLAN.md §4b fix 1 sends a hedged delete-word to the tray instead.
    That is a change to this decision, not a repair of it -- the case it would
    move is exactly the `static` one above. Left alone until somebody decides
    it on purpose.)
    """
    if verdict.get('error'):
        return 'unknown'
    if is_just_a_static_scene(verdict, filename):
        return 'delete'
    return 'keep' if verdict.get('keep') else 'discard'


def is_just_a_static_scene(verdict: dict, filename: str) -> bool:
    """Nothing moved, and nothing else saw anybody. Safe to bin unwatched.

    Three conditions, all of them positive statements rather than the absence
    of a problem:

    - the model answered (an error is not a verdict, and never was);
    - it said not to keep it, *and* named the one category that means the scene
      never changed;
    - the detector that triggered the recording did not name a person or an
      animal in the frame it fired on.

    The last is worth having because the two models are wrong about different
    things. It is deliberately not a fourth *pixel* test: measured over 611 of
    this house's own clips, the quietest one the reviewer kept — a black cat
    crossing a dark garden — moves fewer pixels between frames than a quarter
    of the clips it sent to the tray, where a burned-in clock ticking over is
    the whole of the change. There is no frame-difference threshold that
    separates them, at any resolution, and one picked by eye would eventually
    delete the cat.
    """
    if not DELETE_STATIC:
        return False
    if verdict.get('error'):
        return False                    # no answer is not an answer
    kind = verdict.get('kind')
    if kind not in DELETE_KINDS:
        return False
    # `guardar` is a second opinion on "nothing moved", which is a judgement
    # about a whole scene and worth cross-checking. It is *not* a second
    # opinion on "the only thing that changed was the clock in the corner":
    # that is a specific claim which cannot be true at the same time as
    # something happening, and the prompt tells the model to hedge toward
    # keeping whenever it is unsure — so the hedge was swallowing the answer.
    # Clips reading "The clock in the corner changed from 20:30:38 to 20:30:53" were
    # landing on the shelf marked "poco seguro", which is the case this exists
    # to remove.
    if kind not in HEDGE_TRUSTED_KINDS and verdict.get('keep'):
        return False
    living = living_tags_in(filename)
    if living:
        logger.info("clip review: %s says nothing happened, but the detector "
                    "saw %s — keeping it for a person", filename,
                    ', '.join(sorted(living)))
        return False
    return True


# --- the queue ----------------------------------------------------------------

class ClipReviewQueue:
    """One worker, walking `pending/` and filing what it finds.

    The queue is in memory but the work is not: the queue *is* the pending
    folder, so a restart mid-review loses nothing — `resume()` picks up whatever
    is still sitting there. Clips are never lost by this process crashing;
    the worst case is one reviewed twice.
    """

    def __init__(self, recordings_dir: str, month_path, on_filed=None):
        self._dir = recordings_dir
        self._month_path = month_path      # (datetime, filename) -> destination
        self._on_filed = on_filed
        self._q: "queue.Queue[str]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._seen: set = set()
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None
        # How the GPU gate went, so a backlog can be told apart from a broken
        # reviewer on the status page. A check nobody can see the result of is
        # the decorative kind.
        self.gpu_waits = 0
        self.gpu_timeouts = 0
        self.reviewed = 0
        self.kept = 0
        self.to_review = 0
        self.deleted = 0

    def submit(self, filename: str) -> None:
        with self._lock:
            if filename in self._seen:
                return
            self._seen.add(filename)
        self._q.put(filename)
        self._ensure_worker()

    def resume(self) -> int:
        """Whatever was left in `pending/` when the process last stopped."""
        ensure_dirs(self._dir)
        found = 0
        try:
            for name in sorted(os.listdir(pending_dir(self._dir))):
                if name.endswith('.mp4'):
                    self.submit(name)
                    found += 1
        except OSError as e:
            logger.warning("could not read the pending folder: %s", e)
        if found:
            logger.info("clip review: %d clip(s) still pending from last run", found)
        return found

    def depth(self) -> int:
        return self._q.qsize()

    def _ensure_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name='clip-review',
                                        daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while True:
            try:
                name = self._q.get(timeout=300)
            except queue.Empty:
                return                      # nothing to do; a submit restarts us
            try:
                self._handle(name)
            except Exception as e:          # a clip must never kill the worker
                logger.exception("clip review failed for %s", name)
                self.last_error = f'{type(e).__name__}: {e}'
            finally:
                with self._lock:
                    self._seen.discard(name)

    def _handle(self, name: str) -> None:
        src = os.path.join(pending_dir(self._dir), name)
        if not os.path.isfile(src):
            return                          # already filed, or deleted by hand

        broken = validate_clip(src)
        if broken:
            logger.warning("discarding %s: %s", name, broken)
            try:
                os.remove(src)
            except OSError:
                pass
            return

        # Four outcomes, decided here and written down, rather than every
        # reader working them out again from the truthiness of an error string.
        # That inference had one case badly wrong: review switched off produces
        # a *state* ("review disabled"), not a failure, and reading it as
        # one sent every recording in the house to the tray — where the fifteen
        # day purge would have deleted the lot. Off means keep, as it always
        # has.
        if REVIEW_ENABLED:
            # Lowest-priority inference in the house: let anything else on the
            # card go first. Nothing here branches on the outcome -- the clip
            # is reviewed either way -- it is recorded so the page can say
            # "waiting for the GPU" rather than "stuck".
            waited = wait_for_quiet_gpu()
            # Only the reviews that actually gave way to something else. An
            # idle card is not a wait, and counting it as one would make this
            # equal to `reviewed` on every box with a working nvidia-smi.
            if waited in ('quiet', 'timeout'):
                self.gpu_waits += 1
            if waited == 'timeout':
                self.gpu_timeouts += 1
            verdict = review_clip(src)
            outcome = outcome_for(verdict, name)
        else:
            verdict = {'keep': True, 'what': '', 'certainty': 'high', 'kind': '',
                       'error': None, 'skipped': 'review disabled'}
            outcome = 'keep'
        verdict['outcome'] = outcome
        self.reviewed += 1
        if verdict.get('error'):
            self.last_error = verdict['error']

        # Gone, with no sidecar to write and no tray to fill: the tray is for
        # clips somebody might want, and a garden that is identical at both
        # ends of ten seconds is not one of those. Two thirds of what these
        # cameras record reaches the tray and better than half of that is this,
        # which is why it is worth a branch: the tray was filling at about
        # seven hundred a week and the fifteen day rule was deleting them
        # anyway, just later and with more to scroll past first.
        if outcome == 'delete':
            try:
                os.remove(src)
            except OSError as e:
                logger.error("could not delete %s: %s", name, e)
                return
            self.deleted += 1
            logger.info("clip review: %s → borrado (%s)", name,
                        verdict.get('what') or 'nothing moved')
            return

        # A clip nobody could look at goes to the tray, not the shelf.
        #
        # It used to be kept, on the reasoning that "the model is down" must
        # never file a recording as boring — which is right about what it must
        # not do and wrong about where it belongs. Kept means *judged
        # interesting*: it disappears into the month folder among the clips a
        # person chose to keep, and the tray's "no se pudo revisar" line could
        # never appear because nothing ever reached it. The tray is exactly the
        # right place: it is the pile a person looks through, and nothing there
        # is deleted for being dull — only by age, after fifteen days, which is
        # a decision somebody made once out loud.
        #
        # `keep` is still honoured when it is an answer. This is only for when
        # there was no answer at all.
        if outcome == 'keep':
            dest = self._month_path(name)
            self.kept += 1
        else:
            dest = os.path.join(review_dir(self._dir), name)
            self.to_review += 1
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            shutil.move(src, dest)
        except OSError as e:
            logger.error("could not file %s: %s", name, e)
            return
        _write_verdict(dest, verdict)
        # Where it actually went, not where `keep` alone would suggest: a clip
        # nobody could look at is kept=True and sitting in the tray, and an
        # operator grepping this line to find a recording was being told the
        # opposite of the truth.
        logger.info("clip review: %s → %s (%s)", name,
                    'guardado' if outcome == 'keep' else 'para revisar',
                    verdict.get('what') or verdict.get('error')
                    or verdict.get('skipped') or '')
        if self._on_filed:
            try:
                self._on_filed(name, dest, verdict)
            except Exception:
                logger.exception("clip review callback failed for %s", name)


def _write_verdict(clip_path: str, verdict: dict) -> None:
    """A sidecar saying why this clip is where it is.

    The review page shows it, which is the difference between "here are forty
    clips" and "here are forty clips the model thought were shadows". Best
    effort: a missing sidecar costs an explanation, not a recording.
    """
    try:
        with open(clip_path + '.json', 'w', encoding='utf-8') as fh:
            json.dump({'reviewed_at': datetime.now().isoformat(timespec='seconds'),
                       'model': REVIEW_MODEL, **verdict}, fh, ensure_ascii=False)
    except OSError:
        pass


def read_verdict(clip_path: str) -> Optional[dict]:
    # Normalised here rather than at each caller: this is the only reader, and
    # a tray filled before the vocabulary rename would otherwise show every old
    # clip as an unknown kind on a page whose whole point is filtering by it.
    try:
        with open(clip_path + '.json', encoding='utf-8') as fh:
            return normalise_verdict(json.load(fh))
    except (OSError, ValueError):
        return None


# --- what ages out of the tray ------------------------------------------------

# How long a kept recording stays on this disk. Zero is never delete, and zero
# is the shipped default -- the month folders are the household's archive and
# nothing in this package has ever expired them.
#
# It is off by default for a reason that is not caution in general. These clips
# are marked `backup: bulk` in the manifest, which `home-stack backup` skips
# from every archive. So whatever off-box mirror
# exists is the ONLY other copy, and switching this on before that mirror is
# known to be working deletes the sole copy of everything older than the
# cutoff. Turn it on once, deliberately, after checking the mirror.
RECORDING_MAX_AGE_DAYS = int(os.environ.get('CLIP_RECORDING_MAX_AGE_DAYS', '0'))

# Month folders are `YYYY-MM`. Anchored, because this decides what a deleter
# is allowed to walk into and a loose match would let it into `pending`,
# `to_review`, or anything else somebody puts here later.
_MONTH_DIR = re.compile(r'^\d{4}-\d{2}$')


def purge_old_recordings(recordings_dir: str, max_age_days: int = None) -> int:
    """Delete kept recordings older than the cutoff. Returns how many went.

    The counterpart to `purge_old_reviews`, and deliberately a separate
    function with a separate setting, because it is a categorically different
    act: that one bins clips nobody came back for within fifteen days, this one
    deletes recordings the reviewer judged worth keeping. Sharing one switch
    would let a household enable the harmless one and get this.

    Three things bound it, all of them positive:

    * a cutoff of zero deletes nothing, and is the default;
    * it walks only directories matching `YYYY-MM` -- never `pending`, never
      `to_review`, never a directory somebody adds later;
    * it deletes a clip's `.json` sidecar with it, so a verdict cannot outlive
      the recording it describes and leave the page listing a clip that is not
      there.

    Age comes from the filename's own timestamp where there is one, and only
    falls back to mtime when there is not. A copy or a restore rewrites every
    mtime -- which is exactly what happened to this stack's own archive on
    2026-08-29 -- and a retention rule reading mtime would then delete nothing
    for a month, or, with the dates the other way round, everything at once.
    """
    days = RECORDING_MAX_AGE_DAYS if max_age_days is None else max_age_days
    if days <= 0 or not os.path.isdir(recordings_dir):
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for month in sorted(os.listdir(recordings_dir)):
        folder = os.path.join(recordings_dir, month)
        if not _MONTH_DIR.match(month) or not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.endswith('.json'):
                continue                # goes with its clip, never on its own
            path = os.path.join(folder, name)
            try:
                if not os.path.isfile(path):
                    continue
                stamp = _stamp_from_name(name)
                age_source = stamp if stamp is not None else os.path.getmtime(path)
                if age_source >= cutoff:
                    continue
                os.remove(path)
                removed += 1
                sidecar = path + '.json'
                if os.path.isfile(sidecar):
                    os.remove(sidecar)
            except OSError as e:
                logger.warning("could not delete %s: %s", name, e)
    if removed:
        logger.info("recordings: %d clip(s) past the %d day retention", removed, days)
    return removed


def _stamp_from_name(name: str) -> Optional[float]:
    """The epoch time in `YYYY-MM-DD_HH-MM-SS_...`, or None if it is not there."""
    try:
        return datetime.strptime(name[:19], '%Y-%m-%d_%H-%M-%S').timestamp()
    except (ValueError, TypeError):
        return None


def purge_old_reviews(recordings_dir: str, max_age_days: int = None) -> int:
    """Delete `to_review/` clips older than the cutoff. Returns how many went.

    Only this folder. The month folders are the house's recordings and nothing
    here is allowed to touch them — a bug in a retention rule that reaches the
    real archive is unrecoverable, and this one is deliberately incapable of it.

    Ages by the filename's own timestamp, like `purge_old_recordings`, and for
    the same reason: `shutil.move` within a filesystem is a rename, so a clip's
    mtime is when *recording finished* and survives every move it ever makes.
    That is close enough to the truth here to have hidden the problem — but it
    is rewritten by a copy or a restore, and then this either keeps a tray full
    of old clips for another fifteen days or empties it at once. Two deleters
    in this file measuring age two different ways is also just a thing waiting
    to be found out the hard way.

    The sidecar goes with its clip rather than ageing on its own. It is written
    at filing time, so it is *newer* than the video it describes: aged
    separately, the clip goes first and leaves an orphan `.json` in the tray,
    which the page then renders as a verdict for a recording that is not there.
    """
    days = REVIEW_MAX_AGE_DAYS if max_age_days is None else max_age_days
    folder = review_dir(recordings_dir)
    if not os.path.isdir(folder) or days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for name in sorted(os.listdir(folder)):
        if name.endswith('.json'):
            continue                    # goes with its clip, never on its own
        path = os.path.join(folder, name)
        try:
            if not os.path.isfile(path):
                continue
            stamp = _stamp_from_name(name)
            if (stamp if stamp is not None else os.path.getmtime(path)) >= cutoff:
                continue
            os.remove(path)
            removed += 1
            sidecar = path + '.json'
            if os.path.isfile(sidecar):
                os.remove(sidecar)
        except OSError as e:
            logger.warning("could not delete %s: %s", name, e)
    if removed:
        logger.info("clip review: %d clip(s) aged out of to_review", removed)
    return removed

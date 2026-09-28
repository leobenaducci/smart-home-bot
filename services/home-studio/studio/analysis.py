"""A song, listened to: when each line is sung, the beats, the sections --
and from those, where a music video's cuts go.

Three sources, each doing what it is good at:

  * **The lyrics' own tags** ([Verse], [Chorus]...) say what the sections are.
    The person or Alfred wrote them, and the singing model followed them.
  * **The Studio's audio.cpp** says *when*: Mel-Band RoFormer separates the
    vocals from the music, and the Qwen3 forced aligner places each known word
    on them. Forced alignment, not transcription: the words are already known,
    and a speech recogniser hearing singing over guitars heard one wrong line
    for a whole 75-second song (2026-09-28). The aligner, on the separated
    vocals, put every line boundary inside a measured vocal pause.
  * **librosa** finds the beats, on the CPU, beside the queue.

Measured on a 90-second song on the Studio's card: separation 23 s including
the model load, alignment 4 s, beats a few seconds. Without the audio server --
switched off, or a song with no words -- a song still gets its beats and a
plan that cuts on them; it only loses the words.

The aligner is given 16 kHz mono: given the separator's 44.1 kHz stereo it
reported its word positions in seconds computed at the wrong rate.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import re
import time
import unicodedata
from pathlib import Path

import requests

from . import media

log = logging.getLogger("studio.analysis")

SEP_MODEL = "mel_band_roformer_q8_0"
# For the scores (studio/score.py): HTDemucs splits a song into vocals, drums,
# bass and "other" -- guitars and keys land in the last -- and MuScriptor
# hears the notes, constrained to the instruments asked for.
STEMS_MODEL = "htdemucs_q8_0"
NOTES_MODEL = "muscriptor_small_f32"
MUSIC_MODEL = "ace_step_turbo_q8_0"
ALIGN_MODEL = "qwen3_forced_aligner_0_6b_q8_0"
TAG_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
# Shot lengths, in seconds. H3 makes up to 20 s; under ~2.5 s a shot is a
# flash, and cutting that often is a decision the person should make, not us.
MIN_SHOT, MAX_SHOT = 2.5, 20.0
FPS = 24


def lyric_lines(lyrics: str) -> list[dict]:
    """The sung lines, in order, each with the section it sits under. A tag
    with no lines after it (an [Instrumental] break) is kept as a section with
    no words."""
    lines, section, index = [], "", -1
    for raw in (lyrics or "").splitlines():
        m = TAG_RE.match(raw)
        if m:
            section, index = m.group(1).strip(), index + 1
            continue
        text = raw.strip()
        if text:
            lines.append({"text": text, "section": section or "Verse", "section_index": max(index, 0)})
    return lines


def _norm(word: str) -> str:
    word = unicodedata.normalize("NFKD", word.lower())
    return "".join(c for c in word if c.isalnum())


def place_lines(lines: list[dict], words: list[dict]) -> list[dict]:
    """Each line's start and end, from the aligner's words.

    The aligner returns one entry per word of the text it was given, in order;
    they are matched back to the lines by walking both, comparing letters only
    (punctuation and accents are the aligner's business). A line whose words
    were not found keeps no times rather than borrowing a neighbour's.
    """
    out, k = [], 0
    for line in lines:
        mine = [w for w in (_norm(t) for t in line["text"].split()) if w]
        found = []
        for token in mine:
            # Look a few entries ahead: an aligner may split or merge a token.
            for j in range(k, min(k + 4, len(words))):
                if _norm(str(words[j].get("word", ""))) == token:
                    found.append(words[j])
                    k = j + 1
                    break
        entry = dict(line)
        if found:
            entry["start"] = round(float(found[0]["start"]), 3)
            entry["end"] = round(float(found[-1]["end"]), 3)
            entry["words"] = [{"word": w["word"], "start": round(float(w["start"]), 3),
                               "end": round(float(w["end"]), 3)} for w in found]
        out.append(entry)
    return out


def sections_from(lines: list[dict], duration: float) -> list[dict]:
    """The song's parts in time: each tagged section from its first sung word
    to its last, and the stretches with nobody singing -- an intro, a break,
    the outro -- as instrumental ones."""
    timed = [l for l in lines if "start" in l]
    groups: dict[int, dict] = {}
    for line in timed:
        g = groups.setdefault(line["section_index"], {"name": line["section"], "start": line["start"],
                                                      "end": line["end"], "sung": True})
        g["start"], g["end"] = min(g["start"], line["start"]), max(g["end"], line["end"])
    ordered = sorted(groups.values(), key=lambda g: g["start"])
    out, t = [], 0.0
    for g in ordered:
        if g["start"] - t > 1.5:
            out.append({"name": "Instrumental", "start": round(t, 3), "end": g["start"], "sung": False})
        out.append(g)
        t = g["end"]
    if duration - t > 1.5:
        out.append({"name": "Instrumental", "start": round(t, 3), "end": round(duration, 3), "sung": False})
    return out


def beats_of(song: Path) -> tuple[float, list[float]]:
    """The song's tempo and beats: a steady grid when the song keeps one
    tempo, which a generated song does, else the beat tracker's own beats.

    The tracker's beats wander. On the first song checked, a steady 122 bpm
    (0.492 s a beat), its beats came 0.395-0.557 s apart and it called the
    tempo 123 -- a second and a half of drift by the end of the song, and a
    tenth of its beats more than 150 ms off the music. A cut placed on one
    lands visibly off the beat."""
    import librosa  # noqa: PLC0415 -- heavy, and only this needs it
    import numpy as np  # noqa: PLC0415
    y, sr = librosa.load(str(song), sr=22050, mono=True)
    env = librosa.onset.onset_strength(y=y, sr=sr)
    tempo, beats = librosa.beat.beat_track(onset_envelope=env, sr=sr, units="time")
    tempo = float(np.atleast_1d(tempo)[0])
    # The onset envelope rises one analysis hop after the sound does: on
    # clicks at known times both the tracker and the grid put every beat
    # 22-23 ms late (512 samples at 22050 Hz). Taken back here.
    late = 512 / sr
    times = librosa.times_like(env, sr=sr) - late
    beats = [max(0.0, float(b) - late) for b in beats]
    grid = steady_grid(env, times, len(y) / sr, tempo, beats)
    if grid:
        return grid
    return tempo, [round(b, 3) for b in beats]


def steady_grid(env, times, duration: float, tempo: float, beats: list[float]) -> tuple[float, list[float]] | None:
    """One tempo and one phase for the whole song: those whose beats sit on
    the most onset strength, searched within 3 % of the tracker's tempo. Used
    only when the tracker's own beats mostly agree with it -- a song that
    really changes tempo keeps the tracker's."""
    import numpy as np  # noqa: PLC0415
    if len(beats) < 16 or tempo <= 0:
        return None
    env = np.asarray(env, dtype=float)
    env = env / (env.max() or 1.0)
    p0, best = 60.0 / tempo, (-1.0, 0.0, 0.0)
    for p in p0 * (1 + np.linspace(-0.03, 0.03, 241)):
        k = np.arange(0, int(duration / p) + 1)
        ph = np.arange(0, p, 0.002)
        g = ph[:, None] + k[None, :] * p
        m = np.nanmean(np.where(g <= duration, np.interp(g, times, env), np.nan), axis=1)
        i = int(np.argmax(m))
        if m[i] > best[0]:
            best = (float(m[i]), float(p), float(ph[i]))
    _, p, ph = best
    grid = ph + np.arange(0, int((duration - ph) / p) + 1) * p
    near = float(np.mean([np.min(np.abs(grid - b)) < 0.06 for b in beats]))
    if near < 0.6:
        return None
    return 60.0 / p, [round(float(g), 3) for g in grid]


def bars_from(beats: list[float], anchors: list[float]) -> list[float]:
    """Every fourth beat, starting on whichever of the first four lines up best
    with where the sections start -- a bar's first beat is where a section
    begins, and the beat tracker does not say which beat is the first."""
    if not beats:
        return []
    if not anchors:
        return beats[::4]

    def cost(phase: int) -> float:
        bars = beats[phase::4]
        return sum(min(abs(a - b) for b in bars) for a in anchors) if bars else math.inf
    return beats[min(range(4), key=cost)::4]


class AudioServer:
    """The Studio's audio.cpp, over HTTP."""

    def __init__(self, url: str):
        self.url = (url or "").rstrip("/")

    def separate_vocals(self, song: Path, work: Path) -> Path:
        wav = media.to_wav(song, work / "song.wav", rate=44100, channels=2)
        r = requests.post(f"{self.url}/v1/tasks/run", timeout=900,
                          json={"model": SEP_MODEL, "task": "sep", "audio": str(wav)})
        r.raise_for_status()
        stems = {o.get("id"): o.get("audio") for o in r.json().get("named_audio_outputs") or []}
        if not stems.get("vocals"):
            raise RuntimeError("the separator returned no vocals")
        out = work / "vocals.wav"
        out.write_bytes(base64.b64decode(stems["vocals"]))
        return out

    def separate_stems(self, song: Path, work: Path) -> dict[str, Path]:
        """The song as HTDemucs's four stems: vocals, drums, bass, other."""
        wav = media.to_wav(song, work / "song.wav", rate=44100, channels=2)
        r = requests.post(f"{self.url}/v1/tasks/run", timeout=900,
                          json={"model": STEMS_MODEL, "task": "sep", "audio": str(wav)})
        r.raise_for_status()
        out = {}
        for o in r.json().get("named_audio_outputs") or []:
            if o.get("id") and o.get("audio"):
                out[o["id"]] = work / f"stem-{o['id']}.wav"
                out[o["id"]].write_bytes(base64.b64decode(o["audio"]))
        if "other" not in out:
            raise RuntimeError("the separator returned no accompaniment stem")
        return out

    def notes(self, song: Path, work: Path, instruments: list[str]) -> tuple[list[dict], bytes]:
        """The notes MuScriptor hears in the song -- the whole mix, which
        tells a strummed acoustic from a picked electric better than the
        separated stem does -- as note events and as MIDI. 16 kHz mono in."""
        mono = media.to_wav(song, work / "song16.wav", rate=16000, channels=1)
        r = requests.post(f"{self.url}/v1/tasks/run", timeout=1800, json={
            "model": NOTES_MODEL, "task": "midi", "audio": str(mono),
            "options": {"instruments": ",".join(instruments)}})
        r.raise_for_status()
        data = r.json()
        midi = next((a.get("payload") for a in data.get("artifacts") or [] if a.get("kind") == "midi"), "")
        return json.loads(data.get("text") or "[]"), base64.b64decode(midi) if midi else b""

    def align(self, vocals: Path, text: str, language: str, work: Path) -> list[dict]:
        mono = media.to_wav(vocals, work / "vocals16.wav", rate=16000, channels=1)
        with open(mono, "rb") as fh:
            r = requests.post(f"{self.url}/v1/audio/alignments", timeout=900,
                              files={"file": ("vocals.wav", fh, "audio/wav")},
                              data={"model": ALIGN_MODEL, "text": text, "language": language})
        r.raise_for_status()
        return r.json().get("words") or []

    def repaint(self, song: Path, work: Path, *, start: float, end: float, lyrics: str,
                style: str, language: str, seed: int) -> Path:
        """The song again with only `start`..`end` made anew -- a changed line
        sung in place, the rest the original (ACE-Step's repaint route). The
        model takes 48 kHz stereo."""
        wav = media.to_wav(song, work / "source.wav", rate=48000, channels=2)
        r = requests.post(f"{self.url}/v1/tasks/run", timeout=1800, json={
            "model": MUSIC_MODEL, "task": "gen", "task_route": "repaint", "audio": str(wav),
            "text": style or "song", "lyrics": lyrics, "language": language, "seed": seed,
            "repaint_start": round(start, 3), "repaint_end": round(end, 3)})
        r.raise_for_status()
        # One track comes back whole, as `audio` (measured; a separation's
        # stems come as `named_audio_outputs` instead).
        data = r.json()
        audio = data.get("audio") or next((o.get("audio") for o in data.get("named_audio_outputs") or []), None)
        if not audio:
            raise RuntimeError("the repaint returned no audio")
        out = work / "repainted.wav"
        out.write_bytes(base64.b64decode(audio))
        return out

    def wait_idle(self, timeout: float = 60.0) -> None:
        """Until nothing is loaded on the card, so the next render has it
        whole. The server unloads on its own a few seconds after a request."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                models = requests.get(f"{self.url}/v1/models", timeout=5).json().get("data") or []
                if not any(m.get("loaded") for m in models):
                    return
            except (requests.RequestException, ValueError):
                return
            time.sleep(1)


def analyze(song: Path, lyrics: str, language: str, server: AudioServer | None,
            work: Path, progress=lambda phase, share: None) -> dict:
    """Everything a music video needs to know about one recording."""
    work.mkdir(parents=True, exist_ok=True)
    duration = media.probe(song)["seconds"]
    lines = lyric_lines(lyrics)
    error, aligned = "", False
    if server and server.url and lines:
        try:
            progress("separating", 0.2)
            vocals = server.separate_vocals(song, work)
            progress("aligning", 0.6)
            words = server.align(vocals, "\n".join(l["text"] for l in lines), language, work)
            lines = place_lines(lines, words)
            aligned = any("start" in l for l in lines)
        except (requests.RequestException, RuntimeError, media.MediaError, ValueError) as exc:
            error = f"could not follow the words: {exc}"
            log.warning("analysis of %s: %s", song.name, error)
    progress("beats", 0.85)
    tempo, beats = beats_of(song)
    sections = sections_from(lines, duration) if aligned else []
    return {"duration": round(duration, 3), "tempo": round(tempo, 2), "beats": beats, "grid": 2,
            "bars": bars_from(beats, [s["start"] for s in sections if s["start"] > 0]),
            "lines": lines, "sections": sections, "aligned": aligned, "error": error,
            "language": language}


def regrid(an: dict, song: Path) -> dict:
    """An analysis from before the steady grid, given one: the beats and bars
    again, the rest as it was. A few seconds on the CPU."""
    tempo, beats = beats_of(song)
    sections = an.get("sections") or []
    return {**an, "tempo": round(tempo, 2), "beats": beats, "grid": 2,
            "bars": bars_from(beats, [s["start"] for s in sections if s["start"] > 0])}


def _frame(t: float) -> float:
    return round(round(t * FPS) / FPS, 4)


def _snap_to_music(an: dict, points: list[float]) -> list[float]:
    """A section's start is a sung line's, and a line is often sung a beat
    before or after the bar it belongs to: each goes to the nearest bar line
    within half a beat, else the nearest beat within half a beat."""
    beat = 60.0 / float(an.get("tempo") or 120)
    bars, beats = an.get("bars") or [], an.get("beats") or []
    out = []
    for t in points:
        near_bar = min(bars, key=lambda b: abs(b - t)) if bars else None
        near_beat = min(beats, key=lambda b: abs(b - t)) if beats else None
        if near_bar is not None and abs(near_bar - t) <= beat / 2:
            out.append(near_bar)
        elif near_beat is not None and abs(near_beat - t) <= beat / 2:
            out.append(near_beat)
        else:
            out.append(t)
    return out


def cuts_for(an: dict, n: int) -> list[dict]:
    """Exactly `n` shots over the song, each cut on the music: the shots a
    project already has, refitted to it. A cut aims at an even share of the
    song and takes a section change within reach, else the nearest bar, else
    the nearest beat."""
    duration = float(an["duration"])
    n = max(1, int(n))
    if duration / n > MAX_SHOT:
        raise ValueError(f"{n} shots cannot cover {round(duration)} s: a shot is at most {int(MAX_SHOT)} s")
    share = duration / n
    starts = sorted({s["start"] for s in an.get("sections") or []} | {s["end"] for s in an.get("sections") or []})
    starts = _snap_to_music(an, [b for b in starts if MIN_SHOT <= b <= duration - MIN_SHOT])
    bars, beats = an.get("bars") or [], an.get("beats") or []
    bounds = [0.0]
    for i in range(1, n):
        target = i * share
        lo = max(bounds[-1] + MIN_SHOT, target - share / 2)
        hi = min(duration - (n - i) * MIN_SHOT, bounds[-1] + MAX_SHOT, target + share / 2)

        def nearest(points, reach):
            inside = [p for p in points if lo <= p <= hi and abs(p - target) <= reach]
            return min(inside, key=lambda p: abs(p - target)) if inside else None
        end = nearest(starts, share * 0.35) or nearest(bars, share * 0.5) or nearest(beats, share * 0.5) \
            or min(max(target, lo), hi)
        bounds.append(_frame(end))
    bounds.append(round(duration, 4))
    return [_describe(an, a, b) for a, b in zip(bounds, bounds[1:])]


def plan_cuts(an: dict, shot_seconds: float) -> list[dict]:
    """Where a music video's cuts go, each shot ending on the music.

    From the start, one shot at a time, aiming at `shot_seconds`: a section
    change within reach wins (a new part of the song is the cut people
    expect), then the nearest bar, then the nearest beat, then the aim itself.
    Every cut sits on a frame, the last one on the song's end, so the shots
    add up to the song exactly -- each is generated a little long and trimmed
    to its cut when the film is made.
    """
    duration = float(an["duration"])
    length = max(MIN_SHOT, min(MAX_SHOT, float(shot_seconds)))
    starts = sorted({s["start"] for s in an.get("sections") or []} | {s["end"] for s in an.get("sections") or []})
    starts = _snap_to_music(an, [b for b in starts if MIN_SHOT <= b <= duration - MIN_SHOT])
    bars, beats = an.get("bars") or [], an.get("beats") or []
    cuts, t = [], 0.0
    while duration - t > 1.0 / FPS:
        remaining = duration - t
        if remaining <= min(MAX_SHOT, length * 1.5):
            end = duration
        else:
            target = t + length
            lo, hi = t + max(MIN_SHOT, 0.6 * length), min(t + min(MAX_SHOT, 1.6 * length), duration - MIN_SHOT)

            def nearest(points, a, b):
                inside = [p for p in points if a <= p <= b]
                return min(inside, key=lambda p: abs(p - target)) if inside else None
            end = (nearest(starts, lo, hi) or nearest(bars, t + 0.7 * length, min(t + 1.3 * length, hi))
                   or nearest(beats, lo, hi) or target)
        end = duration if end >= duration - 1.0 / FPS else _frame(end)
        cuts.append((t, end))
        t = end
    # A last sliver joins the shot before it when that still fits in one.
    if len(cuts) > 1 and cuts[-1][1] - cuts[-1][0] < MIN_SHOT and cuts[-1][1] - cuts[-2][0] <= MAX_SHOT:
        cuts[-2:] = [(cuts[-2][0], cuts[-1][1])]
    return [_describe(an, a, b) for a, b in cuts]


def _describe(an: dict, start: float, end: float) -> dict:
    words = [w["word"] for line in an.get("lines") or [] for w in line.get("words") or []
             if start <= w["start"] < end]
    mid = (start + end) / 2
    section = next((s for s in an.get("sections") or [] if s["start"] <= mid < s["end"]), None)
    # Four decimals: a cut is a frame (1/24 s), and three decimals moved some
    # of them onto the next one -- a frame of drift per shot.
    return {"start": round(start, 4), "end": round(end, 4), "seconds": round(end - start, 4),
            "words": " ".join(words), "sung": bool(words),
            "section": section["name"] if section else ""}

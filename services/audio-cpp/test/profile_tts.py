#!/usr/bin/env python3
"""Which of audio.cpp's TTS models can speak a short Spanish line, and how fast?

Run: python3 services/audio-cpp/test/profile_tts.py --token <gateway token>
     ... --vram        against the CUDA image, to see what each one costs the card

`bench.py` next door asked whether *one* candidate beat the pair the house
runs today, and the answer for the speaking half was no: qwen3_tts 0.6B took
17-19 s to say a sentence Piper says in 0.6 s. But audio.cpp ships **33 TTS
families**, and "the first one tried is slow" is not the same finding as "the
runtime is slow". This walks the ones that claim Spanish and times them all
against Piper on the same lines.

What it does NOT do is pick a winner on speed alone. Every clip goes back
through the house's own whisper and two words have to survive: one from the
**middle** of the sentence, which is what `services/home-voice/test/smoke.py`
already asserts, and the **first** one, which this profile added after
measuring why the fastest model was fastest. PocketTTS returns audio a third
shorter than piper's for the same line and drops the opening verb --
"Enciende la luz de la cocina" comes back as "de la luz de la cocina". It
passes a middle-word rule and it is not a candidate: a command with no verb
is not a fast command. Both columns have to read 3/3.

Nothing here touches the gateway. `TTS_ENGINE` stays `piper` and the house
keeps working while this runs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import threading
import unicodedata
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# The sentences, the WAV reader, the whisper and piper calls all come from the
# head-to-head bench: two files disagreeing about what "the same job" means is
# how two benches end up not comparable.
from bench import CASES, _post, hear_whisper, speak_piper, wav_seconds  # noqa: E402

# What each family needs in the request body, in the order to try it.
#
# This is a property of the *model*, not of the bench: a Base package with no
# built-in voice cannot speak without a reference clip, and says so -- "Qwen3
# base TTS requires voice clone reference audio". A model with packaged voices
# wants a name instead. There is no /openapi.json to read, and the served
# schema does not list request fields, so an unknown family gets the ladder
# below tried in order and the shape that worked is printed in its row.
#
# The fragments each rung sends are filled in from the command line: shapes().
LADDER = {
    # Measured against the running server, not guessed. PocketTTS answers
    # `plain` with "PocketTTS session prepare() requires a session voice via
    # --voice-id or --voice-ref" and Qwen3 Base with "requires voice clone
    # reference audio": for these the reference clip is not a nicety, it is
    # the only way the model has of knowing what to sound like.
    "pocket_tts":  ["clone"],
    "qwen3_tts":   ["clone"],
    "audio8_tts":  ["clone"],
    "omnivoice":   ["clone", "plain"],
    # These have a voice of their own and take the text alone.
    "supertonic":  ["plain", "voice_es", "clone"],
    "moss_tts_nano": ["plain", "clone"],
    "moss_tts_local": ["plain", "clone"],
    # Magpie has five packaged voices and an explicit language, and defaults
    # to Aria speaking English -- which for a Spanish household is the wrong
    # voice reading the right words. Ask for Spanish first.
    "magpie_tts":  ["voice_es", "plain"],
}
DEFAULT_LADDER = ["plain", "clone", "voice_es"]


class VramWatch:
    """Peak GPU memory while one model is being profiled, in MiB.

    Sampled from `nvidia-smi` on the **host**, which reports the whole card --
    so what a row is worth depends on what else was on it. The number reported
    is peak-minus-baseline, taken just before the model's first call, which is
    the model's own footprint as long as the other tenants hold still. They do
    not always: Ollama loads a vision model when somebody asks Alfred about a
    photo, and the camera detectors are always there. `total` is printed too
    for exactly that reason -- a delta of 900 MiB on a card that was already at
    11 GB is a different fact from the same delta at 3 GB.

    Silently inert without nvidia-smi, so the CPU profile is unchanged.
    """

    def __init__(self, interval: float = 0.3):
        self.interval = interval
        self.baseline = None
        self.peak = None
        self._stop = threading.Event()
        self._thread = None

    @staticmethod
    def _used() -> int | None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        line = out.stdout.strip().splitlines()
        return int(line[0].strip()) if out.returncode == 0 and line else None

    def start(self):
        self.baseline = self._used()
        if self.baseline is None:
            return self
        self.peak = self.baseline
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self.interval):
            now = self._used()
            if now is not None and now > self.peak:
                self.peak = now

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.baseline is None:
            return {}
        return {"vram_mib": self.peak - self.baseline,
                "vram_total_mib": self.peak,
                "vram_baseline_mib": self.baseline}


def _norm(text: str) -> str:
    """Casefolded and stripped of accents, for comparing a word to a transcript.

    whisper returns "que hay para la cena" for "¿Qué hay...": the word is
    there and the acute accent is not. Comparing the two literally scored
    **piper** as dropping its own first word, which is a bug in the check and
    would have been read as a defect in the thing being measured. Accents
    still matter to this household -- they are what `mañana` coming back as
    `Mana` costs -- but that is a judgement about the audio, and it belongs in
    the transcripts printed below, not in a word-survived-the-round-trip test.
    """
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold())
                   if not unicodedata.combining(c))


def first_word(text: str) -> str:
    """The word the sentence opens with, punctuation and inverted marks off."""
    return re.sub(r"^[¿¡\W_]+", "", text).split()[0]


def shapes(args) -> dict:
    """The request-body fragments each rung of the ladder adds."""
    return {
        "plain": {},
        "clone": {"voice_ref": args.voice_ref, "reference_text": args.ref_text},
        # Magpie ships five packaged voices and takes the language explicitly;
        # Sofia is the Spanish-sounding one and `es` is what the household
        # speaks. A model that ignores both fields is unaffected by sending
        # them -- one that needs them fails without.
        "voice_es": {"voice_id": args.voice_id, "language": "es"},
    }


def speak(base: str, model: str, text: str, extra: dict, timeout: float):
    body = {"model": model, "input": text, "response_format": "wav"}
    body.update({k: v for k, v in extra.items() if v})
    t0 = time.time()
    blob, _ = _post(f"{base}/v1/audio/speech", json.dumps(body).encode(),
                    {"Content-Type": "application/json"}, timeout)
    return blob, time.time() - t0


def find_shape(base: str, model: str, family: str, args):
    """The first rung of the ladder the server accepts, and its first clip.

    Returns (shape_name, extra, blob, seconds) or (None, None, None, error).
    Its time is this model's cold call and is reported separately rather than
    thrown away: `lazy_load` means the first request pays for loading the
    weights, and a house that speaks a handful of times a day pays it often.
    """
    text = CASES[0][0]
    table = shapes(args)
    errors = []
    for name in LADDER.get(family, DEFAULT_LADDER):
        try:
            blob, secs = speak(base, model, text, table[name], args.timeout)
            return name, table[name], blob, secs
        except urllib.error.HTTPError as exc:                     # noqa: PERF203
            detail = exc.read().decode(errors="replace")[:160].replace("\n", " ")
            errors.append(f"{name}: HTTP {exc.code} {detail}")
        except Exception as exc:                                  # noqa: BLE001
            errors.append(f"{name}: {str(exc)[:160]}")
    return None, None, None, " | ".join(errors)


def profile_model(base: str, model: str, family: str, args) -> dict:
    row = {"model": model, "family": family}
    watch = VramWatch().start() if args.vram else None
    shape, extra, blob, cold = find_shape(base, model, family, args)
    if shape is None:
        row["error"] = cold
        if watch:
            row.update(watch.stop())
        return row
    row["shape"] = shape
    row["cold_s"] = round(cold, 2)

    # Warm only, and a median of repeats rather than one sample. whisper's own
    # first call was 9.6 s against a 3.6 s median: a bench that times one cold
    # call publishes a number nobody will ever see again.
    #
    # Repeats shrink in proportion for a slow model rather than the model
    # being dropped: at 150 s a line, four repeats of three sentences is half
    # an hour to refine a number whose first digit already settled it. Down to
    # one call per sentence, which is still three warm samples -- the cold one
    # was spent finding the shape.
    repeats = args.repeats
    if cold >= args.slow_after:
        repeats = max(1, min(args.repeats, round(args.repeats * args.slow_after / cold)))
    times, audio, heard, hits, onsets = [], [], [], 0, 0
    for text, expect in CASES:
        for i in range(repeats):
            try:
                blob, secs = speak(base, model, text, extra, args.timeout)
            except Exception as exc:                              # noqa: BLE001
                row.setdefault("partial", []).append(str(exc)[:100])
                continue
            times.append(secs)
            audio.append(wav_seconds(blob))
            if i == 0 and args.save_dir:
                # Kept so a person can listen. The round trip below scores
                # whether the words survived, which is not the same question
                # as whether the household wants this voice reading to them at
                # seven in the morning -- and that one only ears can answer.
                os.makedirs(args.save_dir, exist_ok=True)
                name = f"{model}--{expect}.wav"
                with open(os.path.join(args.save_dir, name), "wb") as fh:
                    fh.write(blob)
            if i == 0:      # transcribe one clip per sentence, not every repeat
                try:
                    w, _ = hear_whisper(args.whisper, blob, args.timeout)
                except Exception as exc:                          # noqa: BLE001
                    w = f"<whisper failed: {str(exc)[:60]}>"
                heard.append(w.strip())
                hits += int(_norm(expect) in _norm(w))
                # And the FIRST word, which is the failure this profile found
                # and the middle-word rule cannot see: a model that clips its
                # own onset scores 3/3 on `expect`, returns audio a third
                # shorter than piper's, and drops the verb -- "Enciende la luz
                # de la cocina" heard back as "de la luz de la cocina". A
                # command with its verb missing is not a fast command.
                onsets += int(_norm(first_word(text)) in _norm(w))
    if watch:
        row.update(watch.stop())
    if not times:
        row["error"] = "every warm call failed"
        return row
    row["n"] = len(times)
    row["median_s"] = round(statistics.median(times), 2)
    row["min_s"] = round(min(times), 2)
    row["max_s"] = round(max(times), 2)
    row["audio_s"] = round(statistics.median(audio), 2) if audio else 0.0
    row["rtf"] = round(row["median_s"] / row["audio_s"], 2) if row["audio_s"] else None
    row["hits"] = f"{hits}/{len(CASES)}"
    row["onset"] = f"{onsets}/{len(CASES)}"
    row["ok"] = hits == len(CASES) and onsets == len(CASES)
    row["heard"] = heard
    return row


def piper_row(args) -> dict:
    """What the family hears today, measured the same way in the same run."""
    row = {"model": "piper es_ES-davefx-medium", "family": "piper (today)"}
    times, audio, hits, onsets, heard = [], [], 0, 0, []
    for text, expect in CASES:
        for i in range(args.repeats):
            try:
                blob, secs = speak_piper(args.gateway, args.token, text,
                                         args.timeout)
            except Exception as exc:                              # noqa: BLE001
                row["error"] = str(exc)[:120]
                return row
            times.append(secs)
            audio.append(wav_seconds(blob))
            if i == 0 and args.save_dir:
                os.makedirs(args.save_dir, exist_ok=True)
                with open(os.path.join(args.save_dir,
                                       f"piper--{expect}.wav"), "wb") as fh:
                    fh.write(blob)
            if i == 0:
                w, _ = hear_whisper(args.whisper, blob, args.timeout)
                heard.append(w.strip())
                hits += int(_norm(expect) in _norm(w))
                onsets += int(_norm(first_word(text)) in _norm(w))
    row.update(n=len(times), median_s=round(statistics.median(times), 2),
               min_s=round(min(times), 2), max_s=round(max(times), 2),
               audio_s=round(statistics.median(audio), 2),
               hits=f"{hits}/{len(CASES)}", onset=f"{onsets}/{len(CASES)}",
               ok=hits == len(CASES) and onsets == len(CASES), heard=heard,
               shape="-", cold_s=None)
    row["rtf"] = round(row["median_s"] / row["audio_s"], 2) if row["audio_s"] else None
    return row


def get_models(base: str, timeout: float = 30.0):
    with urllib.request.urlopen(f"{base}/v1/models", timeout=timeout) as r:
        return json.loads(r.read().decode())["data"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--audiocpp", default="http://127.0.0.1:21014")
    ap.add_argument("--whisper", default="http://127.0.0.1:21010/transcribe")
    ap.add_argument("--gateway", default="http://127.0.0.1:21011")
    ap.add_argument("--token", default="", help="a gateway device token, for "
                    "piper's row -- without it there is nothing to rank against")
    ap.add_argument("--models", default="", help="comma-separated model ids to "
                    "profile; default is every tts model the server registers")
    ap.add_argument("--repeats", type=int, default=4,
                    help="warm calls per sentence (default 4)")
    ap.add_argument("--slow-after", type=float, default=10.0,
                    help="a model whose first call takes longer than this gets "
                         "half the repeats (default 10s)")
    ap.add_argument("--voice-ref", default="/voices/reference-es.wav",
                    help="a Spanish reference clip *inside the container*")
    ap.add_argument("--ref-text", default=(
        "Hola, soy la voz de la casa. Esto es una muestra de referencia en "
        "espa\u00f1ol para clonar."))
    ap.add_argument("--voice-id", default="Sofia",
                    help="packaged voice name for families that have them")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--json", default="", help="also write the rows here")
    ap.add_argument("--save-dir", default="", help="write one clip per model "
                    "per sentence here, to listen to")
    ap.add_argument("--vram", action="store_true", help="sample nvidia-smi "
                    "while each model runs and report its peak, for the GPU "
                    "build -- meaningless against the CPU one")
    args = ap.parse_args()

    try:
        served = get_models(args.audiocpp)
    except Exception as exc:                                      # noqa: BLE001
        print(f"cannot reach {args.audiocpp}: {exc}")
        print("start it with: docker compose --profile bench up -d")
        return 2
    tts = [m for m in served if m.get("task") == "tts"]
    if args.models:
        want = [s.strip() for s in args.models.split(",") if s.strip()]
        tts = [m for m in tts if m["id"] in want]
    if not tts:
        print("the server registers no tts models. AUDIOCPP_TTS_PACKAGES in "
              "docker-compose.yml is the list it reads.")
        return 2

    print(f"{len(tts)} TTS model(s) registered, {args.repeats} warm calls per "
          f"sentence, {len(CASES)} sentences.")
    rows = []
    for m in tts:
        print(f"  ... {m['id']} ({m['family']})", flush=True)
        rows.append(profile_model(args.audiocpp, m["id"], m["family"], args))
    if args.token:
        print("  ... piper, the same lines through the gateway", flush=True)
        rows.append(piper_row(args))

    good = [r for r in rows if "error" not in r]
    good.sort(key=lambda r: r["median_s"])
    print()
    vram_col = f"{'vram':>9}" if args.vram else ""
    print(f"{'model':<30}{'median':>9}{'range':>15}{'audio':>8}{'rtf':>7}"
          f"{'cold':>8}{vram_col}{'mid':>6}{'onset':>7}   shape")
    print("-" * (100 + (9 if args.vram else 0)))
    for r in good:
        cold = f"{r['cold_s']:.1f}s" if r.get("cold_s") else "-"
        rtf = f"{r['rtf']:.2f}" if r.get("rtf") else "-"
        rng = f"{r['min_s']:.2f}-{r['max_s']:.2f}"
        vram = ""
        if args.vram:
            v = r.get("vram_mib")
            vram = f"{f'{v}M':>9}" if v is not None else f"{'-':>9}"
        print(f"{r['model'][:29]:<30}{r['median_s']:>8.2f}s{rng:>15}"
              f"{r['audio_s']:>7.2f}s{rtf:>7}{cold:>8}{vram}{r['hits']:>6}"
              f"{r['onset']:>7}   {r['shape']}")
    for r in rows:
        if "error" in r:
            print(f"{r['model'][:29]:<30}  FAILED  {r['error'][:120]}")

    # A transcript per model, because "3/3" is the assertion and the accents
    # are the thing a Spanish-speaking household actually hears go wrong.
    print("\nwhat the house's whisper heard back:")
    for r in good:
        for said, (text, expect) in zip(r.get("heard", []), CASES):
            mid = _norm(expect) in _norm(said)
            onset = _norm(first_word(text)) in _norm(said)
            mark = "ok  " if mid and onset else ("MID " if not mid else "ONSET")
            print(f"  {mark} {r['model'][:26]:<28} {said[:60]!r}")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        print(f"\nrows written to {args.json}")

    if not args.token:
        print("\n(no --token, so piper's row was skipped: without it this "
              "ranks the candidates against each other and against nothing "
              "the house actually runs)")
    print("\nThe row that matters is a median warm time at or under piper's "
          "with 3/3 in BOTH heard columns. Fast and unintelligible is not "
          "second place, and neither is fast because it stopped early.")
    return 0 if any(r.get("ok") for r in good) else 1


if __name__ == "__main__":
    sys.exit(main())

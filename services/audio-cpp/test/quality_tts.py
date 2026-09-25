#!/usr/bin/env python3
"""Not which model is fastest. Which one this household would want to listen to.

Run: python3 services/audio-cpp/test/quality_tts.py --token <gateway token> \
         --save-dir /tmp/voice-quality

`profile_tts.py` answers "how long does it take and does the word survive".
Both of its word rules are floors -- a model can pass them and still be a voice
nobody wants reading to them, and on the card the speed question stopped
deciding anything: eight models answer in under 1.5 s and the slowest of those
is still faster than a person notices. So the axis moves.

Three things are measured here and the fourth is admitted rather than faked:

  1. **WER on hard Spanish.** Not one word from the middle -- every word,
     against sentences chosen for what actually breaks: `ñ`, the accented
     vowels, inverted marks, numbers a model has to say rather than read,
     proper nouns, and the English brand names a household says out loud
     several times a day. Scored through the house's own whisper, which is the
     same judge the voice panels use.

  2. **Accents, specifically.** WER is computed accent-folded, because whisper
     writes "que" for "Qué" and that is a transcriber artefact rather than a
     voice defect. So the accented words are then checked *unfolded*, on their
     own: `mañana` coming back `Mana` is the single most expensive failure for
     this house and it is worth exactly one number.

  3. **Stability.** Every line is spoken more than once. A model that is
     excellent on average and occasionally says something else is worse for a
     voice assistant than one that is mediocre and predictable -- PocketTTS
     answered "¿Qué hay para la cena de mañana?" with "¡Adiós!" on one run of
     the speed profile, which no average would have shown.

  4. **Timbre is not measured, and cannot be.** Whether the voice is pleasant,
     whether it sounds Chilean or Iberian or like a call centre -- that is a
     listening test. `--save-dir` writes every clip; the numbers below only
     narrow the field down to the ones worth listening to.

The house's whisper judges, and audio.cpp's own ASR deliberately does not:
asking a runtime to grade its own speech is a closed loop, and the question is
whether the transcriber the family already runs can hear it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import unicodedata
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bench import _post, hear_whisper, speak_piper, wav_seconds  # noqa: E402
from profile_tts import (LADDER, DEFAULT_LADDER, _norm, get_models,  # noqa: E402
                         shapes, speak)

# Six lines a household actually says, each carrying something that breaks a
# TTS model. The third column is the words whose accents have to survive
# intact -- checked unfolded, unlike the WER above them.
CASES = [
    ("Mañana a las siete y media hay reunión en el colegio de los niños.",
     ["mañana", "reunión", "niños"]),
    ("¿Quieres que apague las luces del salón y encienda la del pasillo?",
     ["salón"]),
    ("He añadido veintitrés huevos y dos kilos de azúcar a la lista de la compra.",
     ["añadido", "azúcar"]),
    ("La película empieza en Netflix a las nueve y el wifi del salón va lento.",
     ["película", "salón"]),
    ("Recuérdale a Sofía que el cumpleaños de su abuela es el próximo miércoles.",
     ["recuérdale", "sofía", "cumpleaños", "próximo", "miércoles"]),
    ("Cierra la puerta del garaje, enciende la calefacción del cuarto de los "
     "niños y avísame cuando el lavavajillas termine.",
     ["calefacción", "niños", "avísame"]),
]


# What whisper writes where the sentence says something else, and it is the
# transcriber's convention rather than the voice's mistake: digits for spoken
# numbers, and a hyphen inside a brand name. Left uncorrected these charged
# every model -- piper included -- two word errors for saying "nueve" and
# "wifi" correctly, which made the Netflix line the worst line for five of the
# eight models for a reason that had nothing to do with any of them.
#
# Only the forms this corpus actually provokes are listed. A general
# number-to-words expander would be a second thing to get wrong.
_CANON = {
    "9": "nueve", "7": "siete", "23": "veintitres", "2": "dos",
    "30": "treinta", "730": "sietetreinta",
}


def words(text: str) -> list:
    """Comparable words: folded, punctuation gone, transcriber forms canonical."""
    folded = _norm(text)
    # Join what a hyphen or a full stop splits inside one spoken token, so
    # "Wi-Fi" is one word and not two, and "7.30" is one and not two.
    for a, b in (("wi-fi", "wifi"), ("wi fi", "wifi")):
        folded = folded.replace(a, b)
    folded = re.sub(r"(\d)[.,:](\d)", r"\1\2", folded)
    raw = "".join(c if c.isalnum() or c.isspace() else " "
                  for c in folded).split()
    return [_CANON.get(w, w) for w in raw if w]


def wer(said: str, heard: str) -> float:
    """Levenshtein over words, divided by the reference length.

    Word error rate rather than a substring test, because "did one word
    survive" stopped being a useful question once every model passed it. This
    counts what a listener would have to correct.
    """
    ref, hyp = words(said), words(heard)
    if not ref:
        return 0.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / len(ref)


def accents_kept(targets: list, heard: str) -> tuple:
    """(kept, total) for the words whose diacritics are the point.

    Compared with the accents ON, and only for words the transcript got right
    when folded -- a word the model never said is a WER failure and counting it
    here too would charge the same mistake twice.
    """
    low = heard.casefold()
    folded = _norm(heard)
    kept = total = 0
    for t in targets:
        if _norm(t) not in folded:
            continue                      # not said at all; WER's problem
        total += 1
        kept += int(t.casefold() in low)
    return kept, total


def find_shape(base, model, family, args):
    table = shapes(args)
    errors = []
    for name in LADDER.get(family, DEFAULT_LADDER):
        try:
            speak(base, model, "Hola.", table[name], args.timeout)
            return name, table[name], None
        except urllib.error.HTTPError as exc:                     # noqa: PERF203
            errors.append(f"{name}: {exc.read().decode(errors='replace')[:100]}")
        except Exception as exc:                                  # noqa: BLE001
            errors.append(f"{name}: {str(exc)[:100]}")
    return None, None, " | ".join(errors)


def judge(clips, args) -> dict:
    """Score a list of (case, heard) pairs."""
    wers, kept, total, worst = [], 0, 0, None
    for (said, targets), heard in clips:
        w = wer(said, heard)
        wers.append(w)
        k, t = accents_kept(targets, heard)
        kept += k
        total += t
        if worst is None or w > worst[0]:
            worst = (w, said, heard)
    return {"wer_mean": round(statistics.mean(wers), 3),
            "wer_worst": round(max(wers), 3),
            "wer_spread": round(max(wers) - min(wers), 3),
            "perfect": sum(1 for w in wers if w == 0),
            "lines": len(wers),
            "accents": f"{kept}/{total}",
            "accent_rate": round(kept / total, 3) if total else None,
            "worst_line": {"wer": round(worst[0], 3), "said": worst[1],
                           "heard": worst[2]} if worst else None}


def run_model(base, model, family, args) -> dict:
    row = {"model": model, "family": family}
    shape, extra, err = find_shape(base, model, family, args)
    if shape is None:
        row["error"] = err
        return row
    row["shape"] = shape
    clips, secs, audio = [], [], []
    for idx, (said, targets) in enumerate(CASES):
        for rep in range(args.repeats):
            try:
                blob, t = speak(base, model, said, extra, args.timeout)
            except Exception as exc:                              # noqa: BLE001
                row.setdefault("partial", []).append(str(exc)[:100])
                continue
            secs.append(t)
            audio.append(wav_seconds(blob))
            heard, _ = hear_whisper(args.whisper, blob, args.timeout)
            clips.append(((said, targets), heard.strip()))
            if args.save_dir and rep == 0:
                os.makedirs(args.save_dir, exist_ok=True)
                with open(os.path.join(args.save_dir,
                                       f"{model}--{idx + 1}.wav"), "wb") as fh:
                    fh.write(blob)
    if not clips:
        row["error"] = "every line failed"
        return row
    row.update(judge(clips, args))
    row["median_s"] = round(statistics.median(secs), 2)
    row["audio_s"] = round(statistics.median(audio), 2)
    row["heard"] = [h for _, h in clips]
    return row


def piper(args) -> dict:
    row = {"model": "piper es_ES-davefx-medium", "family": "piper (today)",
           "shape": "-"}
    clips, secs, audio = [], [], []
    for idx, (said, targets) in enumerate(CASES):
        for rep in range(args.repeats):
            blob, t = speak_piper(args.gateway, args.token, said, args.timeout)
            secs.append(t)
            audio.append(wav_seconds(blob))
            heard, _ = hear_whisper(args.whisper, blob, args.timeout)
            clips.append(((said, targets), heard.strip()))
            if args.save_dir and rep == 0:
                os.makedirs(args.save_dir, exist_ok=True)
                with open(os.path.join(args.save_dir, f"piper--{idx + 1}.wav"),
                          "wb") as fh:
                    fh.write(blob)
    row.update(judge(clips, args))
    row["median_s"] = round(statistics.median(secs), 2)
    row["audio_s"] = round(statistics.median(audio), 2)
    row["heard"] = [h for _, h in clips]
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--audiocpp", default="http://127.0.0.1:21014")
    ap.add_argument("--whisper", default="http://127.0.0.1:21010/transcribe")
    ap.add_argument("--gateway", default="http://127.0.0.1:21011")
    ap.add_argument("--token", default="")
    ap.add_argument("--models", default="")
    ap.add_argument("--repeats", type=int, default=2,
                    help="times each line is spoken (default 2). More than one "
                         "on purpose: a model that is usually right and "
                         "sometimes says something else is the failure an "
                         "average hides.")
    ap.add_argument("--voice-ref", default="/voices/reference-es.wav")
    ap.add_argument("--ref-text", default=(
        "Hola, soy la voz de la casa. Esto es una muestra de referencia en "
        "español para clonar."))
    ap.add_argument("--voice-id", default="Sofia")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--no-piper", action="store_true")
    ap.add_argument("--save-dir", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    rows = []
    try:
        served = [m for m in get_models(args.audiocpp) if m.get("task") == "tts"]
    except Exception as exc:                                      # noqa: BLE001
        print(f"cannot reach {args.audiocpp}: {exc}")
        return 2
    if args.models:
        want = [s.strip() for s in args.models.split(",") if s.strip()]
        served = [m for m in served if m["id"] in want]
    for m in served:
        print(f"  ... {m['id']}", flush=True)
        rows.append(run_model(args.audiocpp, m["id"], m["family"], args))
    if args.token and not args.no_piper:
        print("  ... piper", flush=True)
        rows.append(piper(args))

    good = [r for r in rows if "error" not in r]
    good.sort(key=lambda r: (r["wer_mean"], -(r["accent_rate"] or 0)))
    print(f"\n{'model':<30}{'WER':>7}{'worst':>8}{'clean':>7}{'accents':>9}"
          f"{'speak':>8}   shape")
    print("-" * 78)
    for r in good:
        clean = f"{r['perfect']}/{r['lines']}"
        print(f"{r['model'][:29]:<30}{r['wer_mean']:>7.3f}{r['wer_worst']:>8.3f}"
              f"{clean:>7}{r['accents']:>9}{r['median_s']:>7.2f}s   {r['shape']}")
    for r in rows:
        if "error" in r:
            print(f"{r['model'][:29]:<30}  FAILED  {r['error'][:110]}")
    print("\nthe line each model did worst on:")
    for r in good:
        w = r["worst_line"]
        print(f"  {r['model'][:26]:<28} WER {w['wer']:.2f}  {w['heard'][:64]!r}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        print(f"\nrows written to {args.json}")
    print("\nWER and accents narrow the field. Which of the survivors the "
          "household wants reading to them at seven in the morning is a "
          "listening test, and --save-dir is what it is for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

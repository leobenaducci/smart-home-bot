#!/usr/bin/env python3
"""Is one CPU runtime as good as the two things the house uses today?

Run: python3 services/audio-cpp/test/bench.py

The question is not "does audio.cpp work". It is whether it beats the pair it
would replace **on this machine, at the sizes the house actually speaks in** --
and the honest way to ask that is to make all three do the same job on the same
sentence and time them.

    audio.cpp TTS  ->  a WAV  ->  audio.cpp ASR   (the candidate, both halves)
    piper          ->  a WAV  ->  whisper         (what the family hears today)

Both round trips are scored the same way: a word from the middle of the
sentence has to survive. That is the assertion `services/home-voice/test/
smoke.py` already makes, for the reason its docstring gives -- a gateway whose
whisper is down still answers, with an empty transcript, and reaches the
family as a panel that hears nothing and never says why.

**Short sentences on purpose.** The published numbers for this class of model
are long-form real-time factors, and a voice assistant's replies are mostly a
handful of words. A model that is 40x real time on a paragraph and slow to
start is worse here than one half as fast that begins immediately, because
what a person waits for is the *first* audio, not the last.

Nothing here touches the gateway. `TTS_ENGINE` stays `piper` and the house
keeps working while this runs.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave

# Short, Spanish, and each with a word that must survive the round trip. The
# middle word rather than the first or last: a transcriber that catches only
# the start is a failure this would otherwise score as a pass.
CASES = [
    ("Enciende la luz de la cocina, por favor", "cocina"),
    ("¿Qué hay para la cena de mañana?", "cena"),
    ("Recuérdame sacar la basura el martes", "basura"),
]


def _post(url: str, data: bytes, headers: dict, timeout: float):
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), dict(r.headers)


def _multipart(field: str, filename: str, blob: bytes, extra: dict) -> tuple:
    """A multipart body without pulling in `requests` for four lines of it."""
    boundary = f"----bench{uuid.uuid4().hex}"
    parts = []
    for key, value in extra.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{field}"; filename="{filename}"\r\n'
                 f"Content-Type: audio/wav\r\n\r\n".encode())
    parts.append(blob)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def wav_seconds(blob: bytes) -> float:
    try:
        with wave.open(io.BytesIO(blob)) as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:                                             # noqa: BLE001
        return 0.0


def speak_audiocpp(base: str, text: str, voice_ref: str, ref_text: str,
                   timeout: float):
    # `voice_ref` and `reference_text`, both of them, and neither is optional
    # for a Base package: it has no built-in voices at all and refuses with
    # "Qwen3 base TTS requires voice clone reference audio" without the clip,
    # then "requires reference text" without the transcript. The field names
    # are not in any served schema -- there is no /openapi.json -- and the
    # README documents the CLI's `--voice-ref`, so these were found by trying
    # candidates against the running server.
    body = {"model": "tts", "input": text, "response_format": "wav"}
    if voice_ref:
        body["voice_ref"] = voice_ref
        body["reference_text"] = ref_text
    t0 = time.time()
    blob, _ = _post(f"{base}/v1/audio/speech", json.dumps(body).encode(),
                    {"Content-Type": "application/json"}, timeout)
    return blob, time.time() - t0


def hear_audiocpp(base: str, blob: bytes, timeout: float):
    data, ctype = _multipart("file", "clip.wav", blob,
                             {"model": "asr", "language": "es"})
    t0 = time.time()
    out, _ = _post(f"{base}/v1/audio/transcriptions", data,
                   {"Content-Type": ctype}, timeout)
    try:
        text = json.loads(out.decode()).get("text", "")
    except ValueError:
        text = out.decode(errors="replace")
    return text, time.time() - t0


def speak_piper(gateway: str, token: str, text: str, timeout: float):
    body = json.dumps({"text": text}).encode()
    t0 = time.time()
    blob, _ = _post(f"{gateway}/v1/tts", body,
                    {"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"}, timeout)
    return blob, time.time() - t0


def hear_whisper(url: str, blob: bytes, timeout: float):
    data, ctype = _multipart("file", "clip.wav", blob, {"language": "es"})
    t0 = time.time()
    out, _ = _post(url, data, {"Content-Type": ctype}, timeout)
    try:
        payload = json.loads(out.decode())
        text = payload.get("text") or payload.get("transcript") or ""
    except ValueError:
        text = out.decode(errors="replace")
    return text, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--audiocpp", default="http://127.0.0.1:21014")
    ap.add_argument("--whisper", default="http://127.0.0.1:21010/transcribe")
    ap.add_argument("--gateway", default="http://127.0.0.1:21011",
                    help="the voice gateway, for piper's side of the comparison")
    ap.add_argument("--token", default="", help="a gateway device token")
    ap.add_argument("--voice-ref", default="/voices/reference-es.wav",
                    help="a Spanish reference clip *inside the container*. The "
                         "Base package has no built-in voice and cannot speak "
                         "without one.")
    ap.add_argument("--ref-text", default=(
        "Hola, soy la voz de la casa. Esto es una muestra de referencia en "
        "español para clonar."),
        help="the transcript of --voice-ref, word for word. The clone is "
             "in-context: it is shown the audio and what the audio says.")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    rows = []
    for text, expect in CASES:
        row = {"text": text, "expect": expect}
        # --- the candidate -------------------------------------------------
        try:
            blob, secs = speak_audiocpp(args.audiocpp, text, args.voice_ref,
                                        args.ref_text, args.timeout)
            row["ac_tts_s"] = round(secs, 2)
            row["ac_audio_s"] = round(wav_seconds(blob), 2)
            heard, hsecs = hear_audiocpp(args.audiocpp, blob, args.timeout)
            row["ac_asr_s"] = round(hsecs, 2)
            row["ac_heard"] = heard.strip()
            row["ac_ok"] = expect.lower() in heard.lower()
            # The same clip through the house's whisper, which isolates which
            # half of the candidate is responsible for a miss.
            w, wsecs = hear_whisper(args.whisper, blob, args.timeout)
            row["ac_via_whisper"] = w.strip()
            row["ac_via_whisper_ok"] = expect.lower() in w.lower()
            row["whisper_s"] = round(wsecs, 2)
        except Exception as exc:                                  # noqa: BLE001
            row["ac_error"] = str(exc)[:120]

        # --- what the house does today --------------------------------------
        if args.token:
            try:
                blob, secs = speak_piper(args.gateway, args.token, text,
                                         args.timeout)
                row["piper_s"] = round(secs, 2)
                row["piper_audio_s"] = round(wav_seconds(blob), 2)
                w, wsecs = hear_whisper(args.whisper, blob, args.timeout)
                row["piper_via_whisper"] = w.strip()
                row["piper_ok"] = expect.lower() in w.lower()
                row["piper_whisper_s"] = round(wsecs, 2)
            except Exception as exc:                              # noqa: BLE001
                row["piper_error"] = str(exc)[:120]
        rows.append(row)

    for r in rows:
        print(f"\n  {r['text']!r}  (must hear {r['expect']!r})")
        if "ac_error" in r:
            print(f"    audio.cpp  FAILED  {r['ac_error']}")
        else:
            print(f"    audio.cpp  tts {r['ac_tts_s']}s for {r['ac_audio_s']}s "
                  f"of audio · asr {r['ac_asr_s']}s · "
                  f"{'ok' if r['ac_ok'] else 'MISS'} {r['ac_heard'][:44]!r}")
            print(f"      same clip through whisper: "
                  f"{'ok' if r['ac_via_whisper_ok'] else 'MISS'} "
                  f"{r['ac_via_whisper'][:44]!r} ({r['whisper_s']}s)")
        if "piper_error" in r:
            print(f"    piper      FAILED  {r['piper_error']}")
        elif "piper_s" in r:
            print(f"    piper      tts {r['piper_s']}s for "
                  f"{r['piper_audio_s']}s of audio · whisper "
                  f"{r['piper_whisper_s']}s · "
                  f"{'ok' if r['piper_ok'] else 'MISS'} "
                  f"{r['piper_via_whisper'][:44]!r}")

    good = [r for r in rows if "ac_error" not in r]
    if good:
        tts = sum(r["ac_tts_s"] for r in good) / len(good)
        hits = sum(1 for r in good if r["ac_ok"])
        print(f"\naudio.cpp: {hits}/{len(good)} round trips, "
              f"{tts:.2f}s average to speak a short line")
        if any("piper_s" in r for r in rows):
            pt = [r["piper_s"] for r in rows if "piper_s" in r]
            print(f"piper:     {sum(pt) / len(pt):.2f}s average for the same lines")
        print("\nThe number that decides it is the time to speak a SHORT line "
              "with the assistants running, not a real-time factor on a "
              "paragraph. Compare the two averages above.")
    if not args.token:
        print("\n(no --token, so piper's side was skipped: pass a gateway "
              "device token for the comparison this exists to make)")
    return 0 if good and all(r.get("ac_ok") for r in good) else 1


if __name__ == "__main__":
    sys.exit(main())

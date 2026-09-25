"""Drive the gateway the way a panel does, and check something real came back.

Speaks a sentence with the gateway's own piper, feeds that audio back in as if a
panel had recorded it, and asserts the transcript contains a word from the
middle. That exercises TTS, STT and the whole /v1/turn path in one pass, with no
recorded WAV to commit and nothing to keep in sync with the voice model.

The point is that it can fail. A gateway whose whisper is down still answers
/v1/turn — with an empty transcript and "I did not hear you" — which reaches the
family as a panel that hears nothing and never says why.

By hand:
    python smoke.py --base http://compute.home:8083 --token <a real device token>
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.request
import wave


# The probe sentence, per language, with the word the transcript must contain.
#
# This used to be one English sentence and `--expect kitchen`, whatever the
# house spoke. The gateway runs whisper with `WHISPER_LANGUAGE={locale.default}`
# -- `es` here -- so piper said an English sentence in a Spanish voice and
# whisper, told to expect Spanish, transcribed it as Spanish: 'tú no te quichen
# li, le hace' and 'y tú no te quiches ni te leas' on two consecutive runs. The
# word `kitchen` could not appear in any of them, so the check failed five
# times and reported the flakiness it was written to tolerate. The stack was
# healthy throughout.
#
# The expected word is chosen to be one whisper gets right rather than one that
# reads well: short, common, and not a homophone of anything in the sentence.
PROBES = {
    "en": ("Turn on the kitchen light, please.", "kitchen"),
    "es": ("Enciende la luz de la cocina, por favor.", "cocina"),
    "de": ("Schalte bitte das Licht in der Küche ein.", "küche"),
    "fr": ("Allume la lumière de la cuisine, s'il te plaît.", "cuisine"),
    "it": ("Accendi la luce della cucina, per favore.", "cucina"),
    "pt": ("Liga a luz da cozinha, por favor.", "cozinha"),
}

def _post(url: str, data: bytes, headers: dict) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def _get(url: str) -> tuple[int, bytes, dict]:
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def _round_trip(args) -> tuple[bool, str]:
    """One synthesize -> transcribe -> answer pass. Returns (ok, why not)."""
    status, body, _ = _post(
        f"{args.base}/v1/announce",
        json.dumps({"room": args.room, "text": args.text}).encode(),
        {"Authorization": f"Bearer {args.gateway_token}", "Content-Type": "application/json"},
    )
    if status != 200:
        return False, f"/v1/announce returned {status}: {body[:300]!r}"
    ann = json.loads(body)
    print(f"synthesized {ann['audio']['bytes']} bytes at {ann['audio']['sample_rate']} Hz")

    status, pcm, _ = _get(f"{args.base}{ann['audio']['url']}")
    if status != 200 or not pcm:
        return False, f"could not fetch the clip ({status})"
    # The clip we feed back must be the clip it said it made. A short read here
    # would reach whisper as a half sentence and read exactly like a bad model.
    if len(pcm) != ann["audio"]["bytes"]:
        return False, (f"clip truncated: announced {ann['audio']['bytes']} bytes, "
                       f"fetched {len(pcm)}")

    # 4. feed it back in as if a panel had recorded it
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(ann["audio"]["sample_rate"])
        wav.writeframes(pcm)

    status, body, _ = _post(
        f"{args.base}/v1/turn", buf.getvalue(), {"X-Device-Token": args.token}
    )
    if status != 200:
        return False, f"/v1/turn returned {status}: {body[:300]!r}"
    turn = json.loads(body)
    print(f"room       : {turn['room']}")
    print(f"transcript : {turn['transcript']!r}")
    print(f"reply      : {turn['reply']!r}")

    if not turn["transcript"].strip():
        return False, "empty transcript — is faster-whisper up on :8000?"
    if args.expect.lower() not in turn["transcript"].lower():
        return False, f"expected {args.expect!r} in the transcript"
    if not turn["reply"].strip() or turn["reply"].startswith("I could not reach"):
        return False, "Alfred House did not answer — check nanobot-house on hub"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8083")
    ap.add_argument("--token", default="", help="a device token from devices.json")
    ap.add_argument("--gateway-token", default=os.environ.get("VOICE_GATEWAY_TOKEN", ""),
                    help="enables the /v1/announce check; defaults to $VOICE_GATEWAY_TOKEN")
    ap.add_argument("--devices", default="",
                    help="devices.json to read --token and --room from; "
                         "defaults to $VOICE_CONFIG_DIR/devices.json")
    ap.add_argument("--room", default="", help="room the --token device is in")
    # The language the gateway was deployed with, which decides the probe. The
    # manifest passes `{locale.default}`, the same value it gives the gateway as
    # WHISPER_LANGUAGE -- so the sentence, the voice and the transcriber always
    # agree. A language with no probe sentence refuses to run rather than
    # falling back to English: an English sentence on a non-English voice can
    # never come back containing the English word, so the fallback is the very
    # failure this probe exists to catch, redder than the original.
    ap.add_argument("--lang", default=os.environ.get("WHISPER_LANGUAGE", "en"),
                    help="probe language; defaults to $WHISPER_LANGUAGE")
    ap.add_argument("--text", default="", help="override the probe sentence")
    # Piper is a VITS model: it samples, so the same sentence is a slightly
    # different waveform every time, and whisper on CPU mishears some of those.
    # Measured on the test box (whisper `small`, float32, CPU), one run in three
    # came back as "you don't take it to the place" -- pipeline entirely healthy.
    # Three re-rolls still lost one run in four, so the failures are correlated
    # rather than independent; five is where a healthy pipeline stopped reddening
    # deploys. A healthy run breaks on the first attempt, so the extra ones are
    # only ever paid by a run that is already going wrong.
    #
    # So this retries, and the retry is only honest because every *deterministic*
    # failure still fails all of them: a gateway posting to the wrong URL, a
    # whisper that is down, an assistant that cannot be reached, and a truncated
    # clip each fail identically on every attempt. Only the dice are re-rolled.
    ap.add_argument("--attempts", type=int, default=5,
                    help="re-roll the synthesis this many times before failing")
    ap.add_argument("--expect", default="", help="override the expected word")
    args = ap.parse_args()

    lang = (args.lang or "en").split("-")[0].lower()
    if lang not in PROBES:
        # Falling back to English is the bug this file's history is about:
        # piper speaks the sentence in the house's voice and whisper is told to
        # expect the house's language, so the English word can never appear in
        # the transcript -- five attempts, five confusing failures, a healthy
        # stack. Refuse and say what to do, so a locale nobody has probed yet
        # reddens the deploy with an answer instead of a riddle.
        print(f"FAIL: no probe sentence for {lang!r} in PROBES", file=sys.stderr)
        print("    add one to test/smoke.py, or deploy a locale PROBES already "
              "knows", file=sys.stderr)
        return 1
    default_text, default_expect = PROBES[lang]
    args.text = args.text or default_text
    args.expect = args.expect or default_expect

    # Nothing can hardcode a device token: they are per-install secrets, and the
    # deploy that runs this has no way to know one. Read the registry the
    # gateway itself reads. Without this the check needed an argument the
    # manifest could not supply, so it failed every deploy on a usage error --
    # after the new containers were already live.
    if not args.token:
        path = args.devices or os.path.join(
            os.environ.get("VOICE_CONFIG_DIR", "/config"), "devices.json")
        try:
            with open(path) as fh:
                devices = json.load(fh).get("devices") or []
        except OSError as exc:
            print(f"FAIL: no --token and could not read {path}: {exc}", file=sys.stderr)
            return 1
        if not devices:
            print(f"FAIL: {path} lists no devices", file=sys.stderr)
            return 1
        args.token = devices[0]["token"]
        args.room = args.room or devices[0].get("room", "")
        print(f"using the {args.room!r} device from {path}")

    # 1. health
    status, body, _ = _get(f"{args.base}/health")
    if status != 200:
        print(f"FAIL: /health returned {status}", file=sys.stderr)
        return 1
    health = json.loads(body)
    print(f"health: {health}")
    if not health.get("devices"):
        print("FAIL: the gateway knows about no devices — check devices.json", file=sys.stderr)
        return 1

    # 2. an unknown token must be refused, or the LAN can talk to Alfred
    status, _, _ = _post(f"{args.base}/v1/turn", b"RIFFfake", {"X-Device-Token": "definitely-not-a-token"})
    if status != 403:
        print(f"FAIL: an unknown device token got {status}, expected 403", file=sys.stderr)
        return 1
    print("unknown token refused: OK")

    # 3. synthesize the probe sentence with the gateway's own voice, via announce
    #    if we can (it returns audio for arbitrary text), else skip to a turn.
    if not args.gateway_token:
        print("no --gateway-token given; skipping the audio round trip")
        return 0

    for attempt in range(1, args.attempts + 1):
        ok, why = _round_trip(args)
        if ok:
            break
        print(f"  attempt {attempt}/{args.attempts}: {why}", file=sys.stderr)
    else:
        print(f"FAIL: {why}", file=sys.stderr)
        return 1

    print("OK — piper, whisper and Alfred House are all working")
    return 0


if __name__ == "__main__":
    sys.exit(main())

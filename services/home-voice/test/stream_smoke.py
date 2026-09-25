"""Drive the /v1/stream socket the way a puck does, and check what came back.

Synthesizes a sentence with the gateway's own piper, streams it up the socket as
if a microphone had heard it, and asserts the transcript and the reply. That
exercises the wake gate, the server-side VAD, whisper, Alfred and piper in one
pass, with no microphone and no recorded audio to keep in sync.

The wake word cannot be synthesized convincingly by a Spanish TTS voice, so by
default this runs against a gateway started with `WAKE_THRESHOLD=0`, which makes
the first audio chunk wake it. That deliberately does not test the *model* — it
tests everything the model hands off to. Measuring the model itself is a day in
a kitchen; see ../wakeword/README.md.

By hand:
    python3 stream_smoke.py --base http://compute.home:8083 --token <device token>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request

try:
    import websockets
except ImportError:
    print("needs `websockets` — run this inside the gateway image", file=sys.stderr)
    raise


def synthesize(base: str, gateway_token: str, room: str, text: str) -> tuple[bytes, int]:
    """Borrow /v1/announce as a text-to-PCM endpoint."""
    req = urllib.request.Request(
        f"{base}/v1/announce",
        data=json.dumps({"room": room, "text": text}).encode(),
        headers={"Authorization": f"Bearer {gateway_token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        ann = json.loads(r.read())
    with urllib.request.urlopen(f"{base}{ann['audio']['url']}", timeout=60) as r:
        return r.read(), ann["audio"]["sample_rate"]


def resample_to_16k(pcm: bytes, rate: int) -> bytes:
    """Crude nearest-neighbour downsample, 22050 -> 16000.

    Good enough to feed whisper a recognisable sentence, which is all this is
    for. A device records at 16 kHz natively and never does this.
    """
    import array

    src = array.array("h")
    src.frombytes(pcm)
    ratio = rate / 16000
    out = array.array("h", (src[min(int(i * ratio), len(src) - 1)] for i in range(int(len(src) / ratio))))
    return out.tobytes()


async def run(args) -> int:
    print(f"synthesizing {args.text!r}")
    pcm, rate = synthesize(args.base, args.gateway_token, args.room, args.text)
    pcm16 = resample_to_16k(pcm, rate)
    print(f"{len(pcm16) / 32000:.1f}s of 16 kHz audio to stream")

    ws_base = args.base.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{ws_base}/v1/stream?token={args.token}"

    async with websockets.connect(url, max_size=None) as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        print(f"connected: {first}")
        if first.get("state") != "idle":
            print(f"FAIL: expected idle, got {first}", file=sys.stderr)
            return 1

        # 40 ms frames, the shape a device sends. Sent as fast as the socket
        # takes them: the server times its VAD in audio seconds, not wall clock,
        # precisely so this works.
        frame = 16000 * 2 * 40 // 1000
        for i in range(0, len(pcm16), frame):
            await ws.send(pcm16[i : i + frame])

        # A tail of silence, because the end of a sentence is what tells the VAD
        # the person stopped talking.
        await ws.send(b"\x00" * (16000 * 2 * 2))

        transcript = reply = None
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
                print(f"  <- {msg}")
                if msg.get("type") == "speak":
                    transcript = msg.get("transcript", "")
                    reply = msg.get("text", "")
                    break
                if msg.get("type") == "error":
                    print(f"FAIL: gateway error {msg}", file=sys.stderr)
                    return 1
        except asyncio.TimeoutError:
            print("FAIL: no reply — did it wake? run the gateway with WAKE_THRESHOLD=0",
                  file=sys.stderr)
            return 1

        await ws.send(json.dumps({"type": "resume"}))

    print(f"transcript : {transcript!r}")
    print(f"reply      : {reply!r}")

    if not transcript or args.expect.lower() not in transcript.lower():
        print(f"FAIL: expected {args.expect!r} in the transcript", file=sys.stderr)
        return 1
    if not reply or reply.startswith("I could not reach"):
        print("FAIL: Alfred did not answer — check nanobot-house on hub", file=sys.stderr)
        return 1

    print("OK — wake gate, VAD, whisper, Alfred and piper all working")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8083")
    ap.add_argument("--token", required=True, help="a device token from devices.json")
    ap.add_argument("--gateway-token", default="", help="for the synthesis step")
    ap.add_argument("--room", default="cocina")
    ap.add_argument("--text", default="Enciende la luz de la cocina, por favor.")
    ap.add_argument("--expect", default="cocina")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

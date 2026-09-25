"""The one service an ESP32 voice device talks to.

The device streams its microphone and gets audio back. It does not know that
whisper, nanobot or piper exist, does not parse a reply out of a chat API, and
never holds a credential beyond its own device token:

    WS   /v1/stream    mic up, control down  <- the wake word lives HERE
    GET  /v1/audio/ID  raw PCM out, streamed
    POST /v1/turn      one-shot audio in     (testing, and any device that
                                              would rather record than stream)
    POST /v1/announce  text in  -> speak it in a room, unprompted
    GET  /v1/whoami    which room am I?
    POST /v1/ir/send   fire an infrared code out of a room's blaster
    POST /v1/ir/learn  capture the next code the room sees, and name it
    GET  /v1/ir/codes  what this room knows how to send
    POST /v1/ir/codes  remember a code that worked
    GET  /v1/sensors   how warm/bright/occupied each room is
    GET  /v1/tts/voices which engines and voices can speak here
    POST /v1/tts       text in -> a WAV back, for choosing a voice by ear
    GET  /health

**The wake word runs here, not on the device.** There is no stock "Alfred" in
any on-device engine — WakeNet ships 29 words and Alfred is not one, and
Espressif's custom route wants 20,000 corpus entries or a purchase order. So the
device streams continuously and openWakeWord listens on this side, which means
the model is entirely ours, retuning it never requires reflashing anything, and
the firmware loses its wake engine and its VAD instead of gaining them.

What that costs: the microphone streams to this server whenever the device is
powered. It never leaves the LAN, but it does leave the room. What it does NOT
cost is availability — an on-device wake word would buy nothing there, because a
device that heard you with no gateway to ask has nothing to say.

Audio comes back as a separate GET rather than down the socket, on purpose: it
lets the firmware stream PCM straight into the I2S DMA buffer a chunk at a time,
which is the shape an ESP32 is actually good at, and it is the same path an
announcement uses.

The PCM is served raw, at the model's own sample rate, mono 16-bit little
endian: no WAV header to skip, no MP3 to decode, nothing to resample. The
`audio` block in the JSON says what to configure I2S to.

Runs on compute — beside the whisper it calls (so the upload never crosses the
LAN twice) and on the 56 cores piper synthesizes with. Only the text hop
crosses to hub.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from typing import Any

import aiohttp

import local_usage
import numpy as np
import paho.mqtt.publish as mqtt_publish
from fastapi import (Body, Depends, FastAPI, Header, HTTPException, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from openwakeword.model import Model as WakeModel

LOG = logging.getLogger("voice-gateway")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

WHISPER_URL = os.environ.get("WHISPER_URL", "http://127.0.0.1:21010/transcribe")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "es")
NANOBOT_URL = os.environ.get("NANOBOT_URL", "http://assistant.home:8906/v1/chat/completions")
NANOBOT_SECRET = os.environ.get("NANOBOT_API_SECRET_HOUSE", "")
GATEWAY_TOKEN = os.environ.get("VOICE_GATEWAY_TOKEN", "")
DEVICES_FILE = os.environ.get("DEVICES_FILE", "/config/devices.json")
PIPER_MODEL = os.environ.get("PIPER_MODEL", "/models/voice.onnx")

# How Alfred speaks, as opposed to what he sounds like.
#
# The voice is baked into the image; this is the delivery, and it turned out to
# matter about as much. Piper was being run with no synthesis flags at all —
# every default — which is a neutral reading-aloud voice. A butler is slower,
# steadier and leaves a beat between sentences, and these four numbers are the
# whole difference: the same sentence goes from 12.4 s to 14.8 s.
#
#   length_scale      phoneme duration. Above 1.0 is slower, and most of the
#                     effect lives here.
#   noise_scale       generator noise. Lower is a steadier pitch — composed
#                     rather than expressive.
#   noise_w           phoneme-width noise. Lower is less variation between
#                     phonemes, which reads as measured.
#   sentence_silence  seconds between sentences. Piper's default 0.2 runs
#                     sentences together at a butler's pace.
#
# Environment rather than constants because tuning this is iterative and every
# turn of the loop should be a restart, not a rebuild — the same reason the
# wake model lives in /config. Set any of them to the piper defaults
# (1.0 / 0.667 / 0.8 / 0.2) to get the plain reading voice back.
PIPER_LENGTH_SCALE = os.environ.get("PIPER_LENGTH_SCALE", "1.18")
PIPER_NOISE_SCALE = os.environ.get("PIPER_NOISE_SCALE", "0.55")
PIPER_NOISE_W = os.environ.get("PIPER_NOISE_W", "0.65")
PIPER_SENTENCE_SILENCE = os.environ.get("PIPER_SENTENCE_SILENCE", "0.45")

# Built once. These are the flag spellings of piper 2023.11.14-2 — underscores,
# not hyphens, and `piper --help` is the authority if that ever moves.
PIPER_VOICE_ARGS = [
    "--length_scale", PIPER_LENGTH_SCALE,
    "--noise_scale", PIPER_NOISE_SCALE,
    "--noise_w", PIPER_NOISE_W,
    "--sentence_silence", PIPER_SENTENCE_SILENCE,
]

# Extra piper voices, dropped in by hand beside the wake model rather than baked
# into the image — see _piper_voices(). The baked one is still the default and
# still works with this directory empty or absent.
PIPER_VOICES_DIR = os.environ.get("PIPER_VOICES_DIR", "/config/voices")

# The Qwen3-TTS sidecar, if one is running. Empty means "no such engine", which
# is a normal state: it is a comparison rig, not a dependency.
QWEN_TTS_URL = os.environ.get("QWEN_TTS_URL", "")
QWEN_TIMEOUT_S = float(os.environ.get("QWEN_TIMEOUT_S", "60"))

# Which engine answers when nobody names one. Everything the house actually
# runs on — a room, an announcement, a reply — goes through here, so this stays
# piper until something has been listened to and chosen.
DEFAULT_TTS_ENGINE = os.environ.get("DEFAULT_TTS_ENGINE", "piper")
# Which voice of that engine, when nobody names one. Empty means the engine's
# own default -- piper's baked house voice, audio.cpp's AUDIOCPP_TTS_MODEL.
#
# It belongs beside the engine because the two are chosen together and are
# meaningless apart: `es_MX-ald-medium` is a piper voice and
# `supertonic_3_q8_0` is an audio.cpp model, and handing either to the other
# is a 404 from whichever was asked. `_synthesize` only applies this to the
# engine it was set for, which is also why the fallback drops it.
DEFAULT_TTS_VOICE = os.environ.get("DEFAULT_TTS_VOICE", "")
# Every engine that can be named — by a caller, or by DEFAULT_TTS_ENGINE. An
# engine that is not on this list is a 400 that says so rather than a silent
# fallback to piper: a name nobody offers is a typo, and answering a typo with
# the house voice made the listening bench compare piper against piper and
# label one of them Qwen.
TTS_ENGINES = ("piper", "qwen", "audiocpp")

# services/audio-cpp, when it is deployed. One GGUF runtime for both halves of
# the voice path; measured faster and more intelligible than piper on an
# NVIDIA card and slower on the CPU — services/audio-cpp/README.md has both
# tables. Absent is a normal state: the service ships off.
AUDIOCPP_TTS_URL = os.environ.get("AUDIOCPP_TTS_URL", "")
# **The model is named here, by the client, and not on the admin page.** The
# server offers a menu (`tts_packages`) and every request picks from it, so
# this is the gateway's choice for the house voice rather than a household
# setting somebody has to keep in step with what is installed.
AUDIOCPP_TTS_MODEL = os.environ.get("AUDIOCPP_TTS_MODEL", "supertonic_3_q8_0")
AUDIOCPP_TIMEOUT_S = float(os.environ.get("AUDIOCPP_TIMEOUT_S", "60"))

class NotConfigured(HTTPException):
    """Asked for an engine this gateway was never given an address for.

    A 503 like any other to the caller, and *not* a fallback: "the sidecar is
    restarting" and "there is no sidecar" look identical from outside and are
    opposite problems. Falling back on the second would leave a household
    thinking they had switched voices while piper answered every request
    forever, with a 200 each time and nothing to notice.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=503, detail=detail)


# What speaks when the chosen engine cannot. Empty disables it.
#
# This is deliberately *not* the silent fallback the paragraph above rejects.
# The difference is which question is being answered: an engine name nobody
# offers is a mistake in the request and stays a 400, and so does a voice that
# does not exist. This catches the other case — a named engine that is real
# and is *down* — because the family asking for the kitchen light should not
# get silence when a sidecar is restarting. It only ever downgrades, never
# upgrades, and every response says what actually spoke.
TTS_FALLBACK_ENGINE = os.environ.get("TTS_FALLBACK_ENGINE", "piper")
# A bench, not a narrator: one of Alfred's replies, not a pasted document.
TTS_MAX_CHARS = int(os.environ.get("TTS_MAX_CHARS", "1200"))
# MQTT_BROKER/MQTT_PORT are the stack-wide names — see
# docs/mqtt-conventions.md, which the whole house follows.
# MQTT_HOST is kept as a fallback because this gateway shipped with it.
MQTT_BROKER = os.environ.get("MQTT_BROKER") or os.environ.get("MQTT_HOST", "mqtt.home")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
TURN_TIMEOUT = float(os.environ.get("TURN_TIMEOUT", "90"))

# The trained wake-word model, living in /config beside devices.json rather than
# baked into the image: tuning a wake word is iterative, and swapping the file is
# a restart instead of a rebuild-and-redeploy.
WAKE_MODEL = os.environ.get("WAKE_MODEL", "/config/wake.onnx")
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))
# openWakeWord wants 80 ms of 16 kHz mono at a time.
WAKE_CHUNK_SAMPLES = 1280

# End-of-speech, server side now. Same numbers that were tuned against this
# family's voices in the Android assistant (AssistActivity.kt) — mean absolute
# amplitude per 20 ms window.
VAD_ONSET_MIN = 2600.0
VAD_END_MIN = 1500.0
VAD_SILENCE_S = 0.9
VAD_NO_SPEECH_S = 6.0
VAD_MAX_S = 20.0

# Clips are held only long enough for the panel to come back for them. A device
# that asks and then loses WiFi must not pin memory forever, and a reply nobody
# fetched in two minutes is a reply nobody is still standing there waiting for.
AUDIO_TTL_S = 120
MAX_CLIPS = 32
# 30 s of 16 kHz mono 16-bit. Longer than anyone speaks to a wall panel, and a
# bound on what a device with a stuck button can push into this process.
MAX_UPLOAD_BYTES = 16000 * 2 * 30

app = FastAPI(title="home-voice gateway")


@dataclass
class Clip:
    pcm: bytes
    rate: int
    created: float = field(default_factory=time.monotonic)


_clips: dict[str, Clip] = {}


def _reap() -> None:
    now = time.monotonic()
    for cid in [c for c, clip in _clips.items() if now - clip.created > AUDIO_TTL_S]:
        _clips.pop(cid, None)
    while len(_clips) > MAX_CLIPS:
        _clips.pop(min(_clips, key=lambda c: _clips[c].created), None)


def _devices() -> dict[str, str]:
    """token -> room.

    Read per request, not cached: adding a panel is then editing a JSON file on
    the host, with no deploy and no restart. The file lives outside the Jenkins
    workspace on purpose — four projects in this house have wiped live config by
    keeping it next to the code.
    """
    try:
        with open(DEVICES_FILE) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        LOG.error("no devices file at %s — every device will be refused", DEVICES_FILE)
        return {}
    except json.JSONDecodeError:
        LOG.exception("devices file is not valid JSON — refusing every device")
        return {}
    return {str(d["token"]): str(d["room"]) for d in raw.get("devices", []) if d.get("token")}


def _room_for(token: str | None) -> str:
    if not token:
        raise HTTPException(status_code=401, detail="missing X-Device-Token")
    room = _devices().get(token)
    if not room:
        # Deliberately does not say whether the token is unknown or the file is
        # missing: this endpoint faces the LAN.
        raise HTTPException(status_code=403, detail="unknown device")
    return room


def _known_room(room: str, rooms: set[str] | None = None) -> str:
    """A room is a room because a device says it lives there.

    devices.json is the only place a room name is written down, so this is the
    single answer to "does that room exist" for every endpoint that takes one —
    announce, the IR routes and sensors alike. Naming the rooms that do exist is
    load-bearing: the caller is an LLM, and "unknown room" on its own invites it
    to guess again with another wrong name.
    """
    rooms = set(_devices().values()) if rooms is None else rooms
    if room not in rooms:
        raise HTTPException(status_code=404, detail=f"no device in '{room}'; rooms: {sorted(rooms)}")
    return room


def _gateway_auth(authorization: str | None = Header(default=None)) -> None:
    """The credential Alfred and the bench present, as one dependency.

    It used to be nine pasted copies of the same two lines. The policy they
    encode is fail-OPEN — an unset VOICE_GATEWAY_TOKEN leaves these routes
    ungated — so the copy that gets forgotten on the tenth endpoint does not
    fail loudly, it silently opens a route to the LAN. One home for it means a
    new route inherits the decision instead of having to remember it, and
    tightening it later is one edit rather than nine.
    """
    if GATEWAY_TOKEN and authorization != f"Bearer {GATEWAY_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


def _to_wav(pcm: bytes, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buf.getvalue()


async def _transcribe(audio: bytes, is_wav: bool, rate: int) -> str:
    payload = audio if is_wav else _to_wav(audio, rate)
    form = aiohttp.FormData()
    form.add_field("file", payload, filename="audio.wav", content_type="audio/wav")
    # faster-whisper answers 400 on a region subtag, and the house speaks es.
    form.add_field("language", WHISPER_LANGUAGE.split("-")[0])

    # Milliseconds of audio, which is what makes a transcription time
    # readable: 400ms to hear a two-second sentence is the number a household
    # can judge, and 400ms on its own is not.
    audio_ms = int(len(payload) / max(1, rate * 2) * 1000) if is_wav else \
        int(len(audio) / max(1, rate * 2) * 1000)
    heard = local_usage.timed()
    try:
        with heard:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=60)) as s:
                async with s.post(WHISPER_URL, data=form) as r:
                    r.raise_for_status()
                    text = ((await r.json()).get("text") or "").strip()
    except Exception:
        local_usage.record("asr", "faster-whisper", "faster-whisper",
                           heard.ms, audio_ms, ok=False, route="voice-asr")
        raise
    local_usage.record("asr", "faster-whisper", "faster-whisper",
                       heard.ms, audio_ms, route="voice-asr")
    return text


async def _ask_alfred(room: str, text: str) -> str:
    body = {
        "channel": "voice",
        # The room IS the session. nanobot keys sessions on channel:chat_id with
        # a lock per key, so one container serves every room concurrently and the
        # kitchen never sees the living room's history.
        "chat_id": room,
        "messages": [{"role": "user", "content": text}],
    }
    headers = {"Authorization": f"Bearer {NANOBOT_SECRET}"} if NANOBOT_SECRET else {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TURN_TIMEOUT)) as s:
        async with s.post(NANOBOT_URL, json=body, headers=headers) as r:
            r.raise_for_status()
            data = await r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


def _piper_voices() -> dict[str, str]:
    """Every piper voice this gateway can reach: id -> path to the .onnx.

    The baked one is always present and always called `default` as well as by
    its own name, so a caller that names nothing gets the house voice and a
    caller comparing voices can still name it explicitly.

    The rest are dropped into /config/voices by hand — same arrangement as the
    wake model, and for the same reason: choosing a voice is iterative, and a
    turn of that loop should be copying a file rather than rebuilding an image.
    A voice needs both `<name>.onnx` and `<name>.onnx.json`; one without the
    other is skipped rather than half-loaded.
    """
    out: dict[str, str] = {}
    try:
        for name in sorted(os.listdir(PIPER_VOICES_DIR)):
            if not name.endswith(".onnx"):
                continue
            path = os.path.join(PIPER_VOICES_DIR, name)
            if not os.path.isfile(path + ".json"):
                LOG.warning("piper voice %s has no .onnx.json beside it; skipped", name)
                continue
            out[name[: -len(".onnx")]] = path
    except FileNotFoundError:
        pass
    except OSError:
        # A `voices` that is a plain file, or one this process cannot read.
        # Optional extras must never take the house voice down with them, and
        # every path the family uses — a room, an announcement, a reply — runs
        # through here.
        LOG.warning("could not list %s; only the baked voice is available", PIPER_VOICES_DIR)

    # Last, and unconditionally: `default` is the baked house voice and is not
    # a name a dropped-in file gets to claim. Otherwise copying a candidate in
    # as `default.onnx` silently repoints every room, every announcement and
    # every reply — which is exactly what the README promises the bench cannot
    # do to the house.
    if os.path.isfile(PIPER_MODEL):
        if "default" in out:
            LOG.warning("ignoring %s/default.onnx: `default` is the baked house voice",
                        PIPER_VOICES_DIR)
        out["default"] = PIPER_MODEL
    return out


def _piper_rate(model_path: str) -> int:
    try:
        with open(model_path + ".json") as fh:
            return int(json.load(fh)["audio"]["sample_rate"])
    except Exception:
        LOG.warning("could not read sample rate from %s; assuming 22050", model_path)
        return 22050


async def _synthesize_piper(text: str, voice: str | None = None) -> tuple[bytes, int]:
    """Raw int16 mono PCM from piper, plus its sample rate.

    Shells out to the `piper` binary with --output_raw rather than importing the
    python API: the CLI contract has been stable across versions while the
    module's has not, and this is the one thing in the chain a person hears.
    """
    voices = _piper_voices()
    model = voices.get(voice or "default")
    if model is None:
        raise HTTPException(
            status_code=404,
            detail=f"no piper voice '{voice}'; have: {sorted(voices)}",
        )

    proc = await asyncio.create_subprocess_exec(
        "piper", "--model", model, "--output_raw",
        *PIPER_VOICE_ARGS,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    pcm, err = await proc.communicate(text.encode("utf-8"))
    if proc.returncode != 0 or not pcm:
        # An HTTPException rather than a RuntimeError so both engines fail the
        # same shape: `/v1/tts` deliberately does not catch, and a bare
        # RuntimeError reached the bench as a bodyless 500 that HomeCore could
        # only render as "gateway HTTP 500".
        raise HTTPException(
            status_code=502,
            detail=f"piper failed ({proc.returncode}): {err.decode()[:300]}",
        )

    return pcm, _piper_rate(model)


async def _detail_of(response: aiohttp.ClientResponse) -> str:
    """The `detail` out of a FastAPI error body, if that is what came back."""
    try:
        body = await response.json()
    except Exception:
        return ""
    return str(body.get("detail") or "") if isinstance(body, dict) else ""


async def _synthesize_qwen(text: str, voice: str | None = None) -> tuple[bytes, int]:
    """Raw PCM from the Qwen3-TTS sidecar, which owns the GPU half of this.

    A separate service on purpose, exactly as faster-whisper is: it wants torch
    and CUDA, which would take this image from ~1 GB to several, and it wants
    VRAM, which is a resource the box shares with Ollama's vision model. Absent
    is a normal state here — the sidecar is a comparison rig, and this gateway
    has to keep talking without it.
    """
    if not QWEN_TTS_URL:
        raise NotConfigured("no Qwen TTS sidecar configured")

    body = {"text": text, "voice": voice or "default"}
    try:
        timeout = aiohttp.ClientTimeout(total=QWEN_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{QWEN_TTS_URL.rstrip('/')}/synthesize", json=body) as r:
                if r.status == 404:
                    # The sidecar's own 404 names the voices it does have, the
                    # way piper's does; passing it through beats replacing it
                    # with a message that says `None` on every house path.
                    detail = await _detail_of(r) or f"the Qwen sidecar has no voice '{voice}'"
                    raise HTTPException(status_code=404, detail=detail)
                if r.status == 503:
                    # "Still loading the weights", not "down". Passed through so
                    # the bench can say come back rather than reporting a
                    # sidecar that is working as unreachable.
                    raise HTTPException(
                        status_code=503,
                        detail=await _detail_of(r) or "the Qwen sidecar is not ready yet",
                        headers={"Retry-After": r.headers.get("Retry-After", "20")},
                    )
                r.raise_for_status()
                rate = int(r.headers.get("X-Sample-Rate", "24000"))
                pcm = await r.read()
                if not pcm:
                    # The same guarantee piper's `not pcm` check makes. Without
                    # it a zero-byte clip is stored, announced over MQTT and
                    # played as silence, with a 200 everywhere and nothing in
                    # the log to say a word was ever lost.
                    raise HTTPException(
                        status_code=502, detail="the Qwen sidecar returned no audio")
                return pcm, rate
    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("qwen tts failed")
        raise HTTPException(status_code=502, detail=f"Qwen TTS sidecar: {exc}") from exc


def _from_wav(blob: bytes) -> tuple[bytes, int]:
    """Raw frames and sample rate out of a WAV, for engines that return one.

    Refused rather than resampled if it is not 16-bit mono: everything
    downstream — the puck's DMA buffer, the clip store, `_to_wav` — assumes
    `pcm_s16le`, and quietly handing it stereo would play as noise at double
    speed rather than fail.
    """
    with wave.open(io.BytesIO(blob)) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise HTTPException(
                status_code=502,
                detail=(f"expected 16-bit mono, got {w.getsampwidth() * 8}-bit "
                        f"x{w.getnchannels()}"))
        return w.readframes(w.getnframes()), w.getframerate()


async def _synthesize_audiocpp(text: str, voice: str | None = None,
                               model: str | None = None) -> tuple[bytes, int]:
    """A WAV from services/audio-cpp, which serves several models at once.

    Two different things, and conflating them was the first version of this.
    The **model** is the package -- `supertonic_3_q8_0` -- and it is a setting,
    `AUDIOCPP_TTS_MODEL`. The **voice** is a style *inside* that package, and
    supertonic ships ten: M1-M5 and F1-F5, with M1 the default.

    `voice` used to be sent as the model, which made every style unreachable
    through this gateway -- asking for `M3` tried to load a package by that
    name. It was easy to miss because the server answers 200 to a voice it does
    not know and returns the default, so a wrong field name and a working one
    look identical unless you compare the bytes; and comparing them using M1
    compares the default against the default.

    `audiocpp_cli --inspect <model>` lists a package's styles as
    `voice_style_*` configs. Not every family has any.
    """
    if not AUDIOCPP_TTS_URL:
        raise NotConfigured("no audio.cpp configured — AUDIOCPP_TTS_URL is empty")

    # `model` names a package other than the house one. Only the admin page's
    # Listen button passes it, and only so a package can be auditioned before
    # anybody commits to it -- previewing through the gateway is what makes the
    # preview honest about the fallback, and it would otherwise only ever be
    # able to test the package already chosen.
    body = {"model": model or AUDIOCPP_TTS_MODEL, "input": text,
            "response_format": "wav"}
    if voice:
        body["voice"] = voice
    try:
        timeout = aiohttp.ClientTimeout(total=AUDIOCPP_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{AUDIOCPP_TTS_URL.rstrip('/')}/v1/audio/speech",
                              json=body) as r:
                if r.status in (400, 404):
                    # It names what it does not have, which is more useful
                    # than "no such voice" — and it is a mistake in the
                    # request, so it must not reach the fallback.
                    raise HTTPException(
                        status_code=404,
                        detail=await _detail_of(r)
                        or f"audio.cpp has no model '{body['model']}'")
                r.raise_for_status()
                blob = await r.read()
                if not blob:
                    # The same guarantee piper's and the sidecar's checks make:
                    # a zero-byte clip is stored, announced and played as
                    # silence, with a 200 the whole way and nothing said.
                    raise HTTPException(
                        status_code=502, detail="audio.cpp returned no audio")
                return _from_wav(blob)
    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("audio.cpp tts failed")
        raise HTTPException(status_code=502, detail=f"audio.cpp: {exc}") from exc


async def _synthesize(text: str, engine: str | None = None,
                      voice: str | None = None,
                      model: str | None = None,
                      route: str = "voice-tts") -> tuple[bytes, int, str]:
    """Speak this text, and say which engine actually did it.

    Returns `(pcm, rate, engine)` — three values, and the third is the point.
    `/v1/tts` used to stamp the *asked for* engine into `X-Engine`, so a typo
    made the listening bench compare piper against piper and label one of them
    Qwen. Now that a real engine can fail over to piper mid-request, reporting
    the request instead of the result would put that mistake back with a
    louder version of the same symptom.

    An engine nobody offers is still refused rather than quietly answered by
    piper. See TTS_FALLBACK_ENGINE for where the line is: a bad name is a 400,
    a broken engine is a downgrade.
    """
    name = engine or DEFAULT_TTS_ENGINE
    if name not in TTS_ENGINES:
        raise HTTPException(
            status_code=400,
            detail=f"no TTS engine '{name}'; have: {list(TTS_ENGINES)}",
        )
    # The house voice, but only for the house engine. A caller that named an
    # engine and no voice wants *that engine's* default, not a voice belonging
    # to a different one -- asking piper for `supertonic_3_q8_0` is a 404, and
    # it would be a 404 caused by a setting the caller never mentioned.
    if voice is None and name == DEFAULT_TTS_ENGINE:
        voice = DEFAULT_TTS_VOICE or None
    # What the household's card actually did, for the usage page. Timed around
    # the engine call and not around the request, so the number is the model's
    # and not the network's -- and recorded on the failure path too, because
    # an engine that is down and slow about it is the case worth seeing.
    spoken = local_usage.timed()
    try:
        with spoken:
            if name == "audiocpp":
                out = (*await _synthesize_audiocpp(text, voice, model), name)
            else:
                out = (*await _engine(name)(text, voice), name)
    except HTTPException as exc:
        local_usage.record("tts", name, _tts_model_name(name, model),
                           spoken.ms, len(text), ok=False, route=route)
        # 5xx only, and only downgrading to something else. A 400 or a 404 is
        # a mistake in the request -- an engine nobody offers, a model that is
        # not installed -- and answering it with the house voice is how a
        # typo gets served instead of reported. A 5xx is the engine being
        # down, which the family should not hear as silence.
        fb = TTS_FALLBACK_ENGINE
        if (isinstance(exc, NotConfigured) or exc.status_code < 500
                or not fb or fb == name or fb not in TTS_ENGINES):
            raise
        LOG.warning("[tts] %s failed (%s); falling back to %s",
                    name, exc.status_code, fb)
        # `voice` and `model` are both dropped: they belonged to the engine
        # that failed, and piper's voices are neither audio.cpp's packages nor
        # its styles. Asking piper for `supertonic_3_q8_0` would turn a
        # recoverable outage into a 404 from the thing meant to rescue it.
        with spoken:
            out = (*await _engine(fb)(text, None), fb)
        local_usage.record("tts", fb, _tts_model_name(fb, None),
                           spoken.ms, len(text), route=route)
        return out
    local_usage.record("tts", name, _tts_model_name(name, model),
                       spoken.ms, len(text), route=route)
    return out


def _tts_model_name(engine: str, model: str | None) -> str:
    """Which model spoke, as the page should group it.

    Only audio.cpp has a model *inside* the engine -- piper's voice is baked
    into its image and qwen's is the server's. For those the engine name is
    the whole answer, and inventing a model string for them would put two
    names in the table for one thing.
    """
    return (model or AUDIOCPP_TTS_MODEL) if engine == "audiocpp" else engine


def _engine(name: str):
    return {"qwen": _synthesize_qwen,
            "audiocpp": _synthesize_audiocpp}.get(name, _synthesize_piper)


def _store(pcm: bytes, rate: int) -> str:
    _reap()
    cid = uuid.uuid4().hex[:16]
    _clips[cid] = Clip(pcm=pcm, rate=rate)
    return cid


def _audio_block(cid: str, pcm: bytes, rate: int) -> dict:
    return {
        "audio_id": cid,
        "audio": {
            "url": f"/v1/audio/{cid}",
            "encoding": "pcm_s16le",
            "sample_rate": rate,
            "channels": 1,
            "bytes": len(pcm),
        },
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "clips": len(_clips), "devices": len(_devices())}


@app.get("/v1/whoami")
async def whoami(x_device_token: str | None = Header(default=None)) -> dict:
    """Which room does this device belong to?

    A panel needs its own room name for exactly one thing: subscribing to
    `home-voice/<room>/announce`. Without this it would have to be configured
    with the room a second time, in the device's own NVS, and the two copies
    would drift the first time a panel moved between rooms — devices.json stays
    the single place a room is written down.
    """
    room = _room_for(x_device_token)
    return {"room": room, "mqtt_topic": f"home-voice/{room}/announce"}


@app.post("/v1/turn")
async def turn(request: Request, x_device_token: str | None = Header(default=None)) -> JSONResponse:
    """One utterance, start to finish."""
    room = _room_for(x_device_token)

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="audio too long")

    is_wav = body[:4] == b"RIFF"
    rate = int(request.headers.get("X-Sample-Rate", "16000"))
    started = time.monotonic()

    try:
        transcript = await _transcribe(body, is_wav, rate)
    except Exception:
        LOG.exception("[%s] whisper failed", room)
        raise HTTPException(status_code=502, detail="stt failed")

    if not transcript:
        # Silence, or whisper declining to invent something. Say so out loud
        # rather than returning a turn with nothing in it: the person is
        # standing there with no screen feedback until audio comes back.
        try:
            pcm, srate, _ = await _synthesize("I didn't hear you.")
        except Exception:
            # Same shape as the reply path below. Unwrapped, a broken voice
            # made the commonest branch here — somebody walked past the panel —
            # answer with a bodyless 500.
            LOG.exception("[%s] tts failed", room)
            raise HTTPException(status_code=502, detail="tts failed")
        cid = _store(pcm, srate)
        return JSONResponse({"room": room, "transcript": "", "reply": "I didn't hear you.",
                             **_audio_block(cid, pcm, srate)})

    LOG.info("[%s] heard: %r", room, transcript)

    try:
        reply = await _ask_alfred(room, transcript)
    except Exception:
        LOG.exception("[%s] alfred failed", room)
        reply = "I could not reach Alfred."

    if not reply:
        reply = "I have no answer for that."

    try:
        pcm, srate, _ = await _synthesize(reply)
    except Exception:
        # "tts", not "piper": with an engine knob, the log line that names the
        # wrong engine is the one that sends somebody to read piper's stderr
        # while qwen is the thing that is down.
        LOG.exception("[%s] tts failed", room)
        raise HTTPException(status_code=502, detail="tts failed")

    cid = _store(pcm, srate)
    LOG.info("[%s] replied in %.1fs: %r", room, time.monotonic() - started, reply[:120])
    return JSONResponse({"room": room, "transcript": transcript, "reply": reply,
                         **_audio_block(cid, pcm, srate)})


@app.get("/v1/audio/{clip_id}")
async def audio(clip_id: str) -> StreamingResponse:
    clip = _clips.get(clip_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="expired")

    def chunks():
        # 1 KB at a time: small enough to hand straight to an I2S DMA buffer
        # without the firmware needing its own staging allocation.
        for i in range(0, len(clip.pcm), 1024):
            yield clip.pcm[i : i + 1024]

    return StreamingResponse(
        chunks(),
        media_type="audio/L16",
        headers={
            "Content-Length": str(len(clip.pcm)),
            "X-Sample-Rate": str(clip.rate),
        },
    )


@app.post("/v1/announce", dependencies=[Depends(_gateway_auth)])
async def announce(
    payload: dict = Body(...)
) -> dict:
    """Speak into a room nobody addressed — timers, finished background work.

    Called by Alfred House's `announce` skill, not by a device, so it is gated on
    the gateway token rather than a device token.
    """
    room = str(payload.get("room") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not room or not text:
        raise HTTPException(status_code=400, detail="room and text are required")

    _known_room(room)

    try:
        pcm, rate, _ = await _synthesize(text, route="announce")
    except HTTPException as exc:
        # Never pass a synthesis failure through as its own status code. The
        # announce skill documents 404 as "I got the name wrong" and retries
        # with a different room, so a missing voice model used to send Alfred
        # round every room in the house instead of saying the voice is broken.
        LOG.error("[%s] tts failed: %s", room, exc.detail)
        raise HTTPException(status_code=502, detail=f"tts failed: {exc.detail}") from exc
    except Exception:
        LOG.exception("[%s] tts failed", room)
        raise HTTPException(status_code=502, detail="tts failed")
    cid = _store(pcm, rate)

    # Push, not poll: the panel holds an MQTT subscription and plays what it is
    # told. The broker is already the house's device bus, with its conventions
    # written down in docs/mqtt-conventions.md.
    try:
        mqtt_publish.single(
            f"home-voice/{room}/announce",
            json.dumps({"audio_id": cid, "text": text, "sample_rate": rate}),
            qos=1,
            hostname=MQTT_BROKER,
            port=MQTT_PORT,
        )
    except Exception:
        LOG.exception("mqtt publish failed for room %s", room)
        raise HTTPException(status_code=502, detail="could not reach the broker")

    LOG.info("[%s] announced: %r", room, text[:120])
    return {"room": room, "text": text, **_audio_block(cid, pcm, rate)}


# ---------------------------------------------------------------------------
# The listening bench
# ---------------------------------------------------------------------------
#
# Two endpoints that exist so a voice can be chosen by ear instead of by
# argument. There is no puck on any wall yet, so the only place Alfred can be
# heard is a browser, and the only way to compare two engines is to hear the
# same sentence out of both.
#
# Deliberately not gated behind a flag on this side: the gateway token already
# guards it, and the thing that should be switchable per-person is whether the
# *chat* speaks, which is a HomeCore preference and lives there.


@app.get("/v1/tts/voices", dependencies=[Depends(_gateway_auth)])
async def tts_voices() -> dict:
    """What can speak here, and what is merely configured.

    `available` is the load-bearing field. A sidecar that is configured but not
    running has to look different from one that was never set up, or the
    dropdown offers a choice that fails when taken.
    """
    piper = sorted(_piper_voices())
    engines = [{
        "id": "piper",
        "label": "Piper",
        "available": bool(piper),
        "default_voice": "default",
        "voices": [{"id": v, "label": ("the house voice" if v == "default" else v)}
                   for v in piper],
    }]

    qwen: dict[str, Any] = {"id": "qwen", "label": "Qwen3-TTS", "available": False,
                            "default_voice": "default", "voices": []}
    if not QWEN_TTS_URL:
        qwen["detail"] = "not configured — QWEN_TTS_URL is empty"
    else:
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(f"{QWEN_TTS_URL.rstrip('/')}/voices") as r:
                    r.raise_for_status()
                    data = await r.json()
            qwen["voices"] = data.get("voices", [])
            qwen["available"] = bool(qwen["voices"])
        except Exception as exc:
            # Reported, not raised: piper still works, and the dropdown should
            # say why the other one is greyed out rather than fail to load.
            qwen["detail"] = f"not responding: {exc}"
            LOG.warning("qwen sidecar unreachable at %s: %s", QWEN_TTS_URL, exc)

    engines.append(qwen)

    # audio.cpp. Its "voices" are model packages -- see _synthesize_audiocpp --
    # so the list comes from /v1/models and is filtered to the ones that speak.
    ac: dict[str, Any] = {"id": "audiocpp", "label": "audio.cpp",
                          "available": False,
                          "default_voice": AUDIOCPP_TTS_MODEL, "voices": []}
    if not AUDIOCPP_TTS_URL:
        ac["detail"] = "not configured — AUDIOCPP_TTS_URL is empty"
    else:
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(f"{AUDIOCPP_TTS_URL.rstrip('/')}/v1/models") as r:
                    r.raise_for_status()
                    data = await r.json()
            ac["voices"] = [{"id": m["id"], "label": m["id"]}
                            for m in (data.get("data") or [])
                            if m.get("task") == "tts"]
            ac["available"] = bool(ac["voices"])
        except Exception as exc:
            # Reported, not raised, for the reason above: piper still works and
            # this list is what a dropdown greys out from.
            ac["detail"] = f"not responding: {exc}"
            LOG.warning("audio.cpp unreachable at %s: %s", AUDIOCPP_TTS_URL, exc)
    engines.append(ac)

    return {"engines": engines, "default_engine": DEFAULT_TTS_ENGINE,
            # What the house speaks with when nobody asks for anything.
            # Empty means the engine's own default.
            "default_voice": DEFAULT_TTS_VOICE,
            # What catches a 5xx from whichever engine is chosen. Named so a
            # person reading the list can tell "piper is the default" from
            # "piper is what happens when the other one breaks".
            "fallback_engine": TTS_FALLBACK_ENGINE}


@app.post("/v1/tts", dependencies=[Depends(_gateway_auth)])
async def tts(
    payload: dict = Body(...)
) -> Response:
    """Speak this text and hand the audio straight back.

    A WAV, unlike everything else here, because the consumer is an `<audio>`
    element rather than an I2S peripheral: the puck wants raw PCM it can push
    into a DMA buffer with no header to skip, and a browser wants a container
    it recognises. Same synthesis either way.
    """
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > TTS_MAX_CHARS:
        # A bench, not a narrator. Alfred's replies are a paragraph; a wall of
        # text is a mistake or a paste, and either way it is minutes of audio.
        text = text[:TTS_MAX_CHARS]

    engine = str(payload.get("engine") or DEFAULT_TTS_ENGINE)
    voice = payload.get("voice") or None
    # Which package, for engines that have them. Ignored by piper, which has
    # voice files and no packages -- said here rather than refused, because a
    # caller sending it to piper has made a harmless mistake and refusing
    # would break the one caller that sends it to both.
    model = payload.get("model") or None

    started = time.monotonic()
    pcm, rate, spoke = await _synthesize(text, engine=engine, voice=voice,
                                        model=model)
    took = time.monotonic() - started
    seconds = len(pcm) / 2 / rate if rate else 0.0

    LOG.info("[tts] %s/%s: %.1fs of audio in %.1fs (%.2fx realtime), %d chars%s",
             spoke, voice or "default", seconds, took,
             (seconds / took) if took else 0.0, len(text),
             f" (asked for {engine})" if spoke != engine else "")

    # The numbers go back in headers so the bench can show them without a
    # second request. Which engine is fast enough is half the comparison.
    return Response(
        content=_to_wav(pcm, rate),
        media_type="audio/wav",
        headers={
            "X-Sample-Rate": str(rate),
            # What spoke, not what was asked for. A bench that reads this
            # header is the reason the difference matters.
            "X-Engine": spoke,
            "X-Engine-Requested": engine,
            "X-Fallback": "1" if spoke != engine else "0",
            "X-Voice": str(voice or "default"),
            # Which package actually spoke, so a preview can say what it
            # tested rather than what it asked for.
            #
            # Gated on `spoke`, not on `model`. `model` is what the *caller*
            # asked for and survives a fallback: an audio.cpp 5xx that
            # downgrades to piper was still sending
            # `X-Model: supertonic_3_q8_0` beside `X-Engine: piper` and
            # `X-Fallback: 1`. That is precisely the mistake X-Engine exists
            # to prevent -- reporting the request instead of the result --
            # reappearing one header down.
            "X-Model": str((model or AUDIOCPP_TTS_MODEL)
                           if spoke == "audiocpp" else ""),
            "X-Synth-Seconds": f"{took:.2f}",
            "X-Audio-Seconds": f"{seconds:.2f}",
        },
    )


# ---------------------------------------------------------------------------
# Infrared: the puck as a blaster
# ---------------------------------------------------------------------------
#
# An air conditioner and a television have no network and never will. The puck
# is already in the room, mains-powered and pointed at both, so it carries an
# IR LED and a receiver, and this is the half that decides *what* to send.
#
# Codes live in a file this process writes, not in devices.json: they arrive at
# runtime — learned off a remote, or looked up and confirmed by Alfred — and a
# thing that is written while running cannot live in a read-only mount. Same
# rule as devices.json otherwise, and for the same reason: outside any Jenkins
# workspace, because this house has wiped live state with a checkout four times.
#
# The round trip is deliberately synchronous. Alfred is mid-sentence with
# somebody standing in a room, and "did the television turn on" is the only
# interesting part of the answer; an accepted-and-queued 202 would make him say
# "listo" before anything had happened.

IR_CODES_FILE = os.environ.get("IR_CODES_FILE", "/state/ir-codes.json")
# A learn is somebody walking to a drawer to find the remote. A send is a frame.
IR_SEND_TIMEOUT_S = float(os.environ.get("IR_SEND_TIMEOUT_S", "10"))
IR_LEARN_TIMEOUT_S = float(os.environ.get("IR_LEARN_TIMEOUT_S", "30"))
# A raw capture is a few hundred timings; anything far past that is a receiver
# picking up a fluorescent tube, and storing it would poison the file.
IR_MAX_RAW_MARKS = 1024


def _ir_store() -> dict:
    try:
        with open(IR_CODES_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"version": 1, "rooms": {}}
    except Exception:
        LOG.exception("could not read %s", IR_CODES_FILE)
        raise HTTPException(status_code=500, detail="the IR code store is unreadable")
    data.setdefault("version", 1)
    data.setdefault("rooms", {})
    return data


def _ir_store_save(data: dict) -> None:
    tmp = f"{IR_CODES_FILE}.tmp"
    try:
        os.makedirs(os.path.dirname(IR_CODES_FILE) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        # Replaced rather than written in place: a power cut halfway through
        # would otherwise leave the house with no codes and a syntax error.
        os.replace(tmp, IR_CODES_FILE)
    except Exception:
        LOG.exception("could not write %s", IR_CODES_FILE)
        raise HTTPException(status_code=500, detail="the IR code store is not writable")


HEX_HINT = "Hex needs its 0x, as in 0x20DF10EF"


def _ir_number(value, field: str, default: int = 0, hint: str = HEX_HINT) -> int:
    """A caller-supplied integer, or a 400 that says which field was wrong.

    Every one of these used to be a bare `int()`, so a model writing `"32 bits"`
    or the bare hex `20DF10EF` that half the code databases on the web use got
    a bodyless 500 — the one answer an LLM cannot act on. The whole rest of this
    surface answers 400/404 with what to try instead.
    """
    if value in (None, ""):
        return default
    try:
        return int(str(value), 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        detail = f"'{field}' must be a number; got {value!r}"
        raise HTTPException(status_code=400, detail=f"{detail}. {hint}" if hint else detail) from None


def _ir_protocol(value) -> str:
    """A protocol name the puck's own library will recognise back.

    `typeToString(type, repeat)` on the firmware side answers "NEC (Repeat)"
    when the capture caught an auto-repeat frame, and `strToDecodeType` cannot
    parse that back — so a learn taken with a finger still on the button would
    store a confirmed code that never sends. The suffix is dropped here, at the
    one place every code shape passes through.
    """
    return str(value or "").strip().removesuffix("(Repeat)").strip().upper()


def _ir_code_from(payload: dict) -> dict:
    """The three shapes a code can have, normalised and bounded.

    Kept as one function so the wire format, the stored format and what goes to
    the puck cannot drift apart — they are the same dict all the way through.
    That includes what a *learn* captured: the puck's reply comes through here
    too, so nothing but a normalised code can ever reach the file.
    """
    raw = payload.get("raw")
    if raw:
        if not isinstance(raw, list) or len(raw) > IR_MAX_RAW_MARKS:
            raise HTTPException(status_code=400, detail=f"raw must be a list of at most {IR_MAX_RAW_MARKS} timings")
        return {"raw": [_ir_number(v, "raw") for v in raw],
                "khz": _ir_number(payload.get("khz"), "khz", 38)}

    protocol = _ir_protocol(payload.get("protocol"))
    if not protocol:
        raise HTTPException(status_code=400, detail="need protocol+code+bits, or state, or raw")

    state = str(payload.get("state") or "").strip()
    if state:
        return {"protocol": protocol, "state": state,
                "bits": _ir_number(payload.get("bits"), "bits", 0)}

    code = payload.get("code")
    if code is None:
        raise HTTPException(status_code=400, detail="need a code with the protocol")
    # Accepts "0x20DF10EF" as well as an integer: Alfred looks these up on the
    # web, where they are always written in hex.
    return {"protocol": protocol, "code": _ir_number(code, "code"),
            "bits": _ir_number(payload.get("bits"), "bits", 32),
            "repeats": _ir_number(payload.get("repeats"), "repeats", 0)}


def _ir_devices(store: dict, room: str) -> dict:
    return store["rooms"].get(room, {}).get("devices", {})


def _ir_summary(code: dict) -> dict:
    """What a code looks like in a listing.

    Raw timings are hundreds of numbers and every one of them would land in
    Alfred's context on a listing he only asked for the names in.
    """
    if code.get("raw"):
        return {"shape": "raw", "marks": len(code["raw"]), "khz": code.get("khz", 38)}
    if code.get("state"):
        return {"shape": "state", "protocol": code.get("protocol")}
    if code.get("protocol") and code.get("code") is not None:
        return {"shape": "code", "protocol": code.get("protocol"),
                "code": hex(_ir_number(code.get("code"), "code")), "bits": code.get("bits")}
    # Not one of the three shapes. Saying so beats the old fallthrough, which
    # invented `protocol: null, code: "0x0"` for a truncated or hand-edited
    # entry — a listing Alfred reads as a perfectly good code, and a send that
    # publishes a command with no code in it and reports the puck's 200 back.
    return {"shape": "unknown"}


def _ir_roundtrip(room: str, payload: dict, timeout: float) -> dict:
    """Publish a command and wait for that puck to answer it.

    Subscribes *before* publishing. The other order loses the race on anything
    the firmware answers immediately — which is every failure, including the
    "this puck has no blaster" that a room without the parts replies with.
    """
    import paho.mqtt.client as mqtt

    request_id = uuid.uuid4().hex[:12]
    payload = {**payload, "request_id": request_id}
    answer: dict = {}
    arrived = threading.Event()

    def on_message(_client, _userdata, message) -> None:
        try:
            data = json.loads(message.payload)
        except Exception:
            return
        if data.get("request_id") != request_id:
            return
        answer.update(data)
        arrived.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = on_message
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.subscribe(f"home-voice/{room}/ir/event", qos=1)
        client.loop_start()
        client.publish(f"home-voice/{room}/ir/set", json.dumps(payload), qos=1)
        got = arrived.wait(timeout)
    except Exception:
        LOG.exception("mqtt round trip failed for room %s", room)
        raise HTTPException(status_code=502, detail="could not reach the broker")
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    if not got:
        # Distinguishable from a refusal on purpose: this is an unplugged puck,
        # a puck with no broker, or one wedged mid-turn, and the fix is not to
        # try a different code.
        raise HTTPException(status_code=504, detail=f"the puck in '{room}' did not answer")
    return answer


@app.post("/v1/ir/send", dependencies=[Depends(_gateway_auth)])
async def ir_send(
    payload: dict = Body(...)
) -> dict:
    """Fire a code. Either one this room already knows, or one being tried out.

    The second form is what makes a code database usable: Alfred looks up what
    an LG television wants, sends it without saving it, and asks whether the
    thing turned on. Nothing is written until somebody says it worked.
    """
    room = _known_room(str(payload.get("room") or "").strip())
    device = str(payload.get("device") or "").strip()
    command = str(payload.get("command") or "").strip()

    if payload.get("ac"):
        # A described state rather than a stored frame: this is how "subilo a
        # 23" works without anyone ever having captured 23. The protocol comes
        # from the device entry so Alfred does not have to remember the brand.
        if not isinstance(payload["ac"], dict):
            # The sibling field on this endpoint IS a bare string (`command`),
            # so `"ac": "frio 23"` is the natural confusion — and it used to be
            # the only malformed body here that was a 500 rather than a 400.
            raise HTTPException(
                status_code=400,
                detail='ac must be an object, as in {"power": true, "mode": "cool", "degrees": 23}',
            )
        devices = _ir_devices(await asyncio.to_thread(_ir_store), room)
        ac = dict(payload["ac"])
        protocol = _ir_protocol(ac.get("protocol") or devices.get(device, {}).get("ac_protocol"))
        if not protocol:
            raise HTTPException(
                status_code=400,
                detail=f"no AC protocol for '{device}' in {room}; set one with POST /v1/ir/codes",
            )
        ac["protocol"] = protocol
        result = await asyncio.to_thread(
            _ir_roundtrip, room, {"action": "send", "ac": ac}, IR_SEND_TIMEOUT_S
        )
        LOG.info("[%s] ir ac %s: %s", room, protocol, result.get("ok"))
        return {"room": room, "sent": {"ac": ac}, **result}

    if command:
        # Read only where a stored code is actually wanted. The ad-hoc path
        # below — the one the docstring calls the interesting one, and the one
        # Alfred runs many times per confirmed code — used to parse the whole
        # store and throw it away, and to 500 on an unreadable file it never
        # needed to open.
        devices = _ir_devices(await asyncio.to_thread(_ir_store), room)
        entry = devices.get(device, {}).get("commands", {}).get(command)
        if entry is None:
            known = sorted(devices.get(device, {}).get("commands", {}))
            raise HTTPException(
                status_code=404,
                detail=f"'{device}' in {room} has no '{command}'; it knows: {known}",
            )
        code = {k: v for k, v in entry.items() if k not in ("source", "confirmed", "updated")}
    else:
        code = _ir_code_from(payload)

    result = await asyncio.to_thread(
        _ir_roundtrip, room, {"action": "send", **code}, IR_SEND_TIMEOUT_S
    )
    LOG.info("[%s] ir send %s/%s: %s", room, device or "-", command or "adhoc", result.get("ok"))
    return {"room": room, "sent": _ir_summary(code), **result}


@app.post("/v1/ir/learn", dependencies=[Depends(_gateway_auth)])
async def ir_learn(
    payload: dict = Body(...)
) -> dict:
    """Capture the next code the room sees and file it under a name.

    Somebody points the original remote at the puck and presses the button
    once. Whatever comes out is stored, whether or not the library recognised
    it — an unrecognised remote still has timings, and timings replay.
    """
    room = _known_room(str(payload.get("room") or "").strip())
    device = str(payload.get("device") or "").strip()
    command = str(payload.get("command") or "").strip()
    if not device or not command:
        raise HTTPException(status_code=400, detail="device and command are required")

    # Bounded, like every other number on this surface. Unbounded it was the
    # one field that could park a thread-pool worker and a broker subscription
    # for as long as the caller asked — and a model that reads "30 s" as
    # milliseconds sends 30000, which is eight hours with the room's receiver
    # held open and every other learn in it refused as "superseded".
    timeout = _ir_number(payload.get("timeout_s"), "timeout_s", int(IR_LEARN_TIMEOUT_S), hint="")
    if not 1 <= timeout <= 120:
        raise HTTPException(
            status_code=400,
            detail=f"timeout_s must be between 1 and 120 seconds; got {timeout}",
        )
    result = await asyncio.to_thread(
        _ir_roundtrip,
        room,
        {"action": "learn", "timeout_s": int(timeout)},
        # The puck answers when it gives up, so this waits slightly longer than
        # the puck does — otherwise both time out and the person is told the
        # puck is unreachable when it is merely disappointed.
        timeout + 5,
    )

    if not result.get("ok"):
        return {"room": room, "learned": False, "detail": result.get("detail") or "nothing arrived"}

    captured = {k: v for k, v in result.items()
                if k in ("protocol", "code", "bits", "state", "raw", "khz") and v not in (None, "", [])}
    if not captured:
        return {"room": room, "learned": False, "detail": "the capture was empty"}
    if len(captured.get("raw") or []) > IR_MAX_RAW_MARKS:
        return {"room": room, "learned": False, "detail": "the capture was too long to be a remote"}

    # Through the same normaliser as every other write. Storing the puck's
    # reply verbatim was the one hole in "the same dict all the way through":
    # an unnormalised protocol ("NEC (Repeat)") stored a confirmed code that
    # could never be sent back, and a non-numeric `code` was written to the
    # file *before* the summary that then raised on it — poisoning the store
    # so that every later listing for the whole house answered 500.
    try:
        code = _ir_code_from(captured)
    except HTTPException as exc:
        LOG.error("[%s] the puck answered a learn with something unusable: %s", room, captured)
        return {"room": room, "learned": False,
                "detail": f"the puck's capture was not a usable code: {exc.detail}"}

    store = await asyncio.to_thread(_ir_store)
    entry = store["rooms"].setdefault(room, {}).setdefault("devices", {}).setdefault(device, {})
    if payload.get("brand"):
        entry["brand"] = str(payload["brand"])
    entry.setdefault("commands", {})[command] = {
        **code,
        "source": "learned",
        "confirmed": True,        # it came off the real remote; nothing to doubt
        "updated": int(time.time()),
    }
    await asyncio.to_thread(_ir_store_save, store)

    LOG.info("[%s] ir learned %s/%s: %s", room, device, command, _ir_summary(code))
    return {"room": room, "learned": True, "device": device, "command": command, **_ir_summary(code)}


@app.get("/v1/ir/codes", dependencies=[Depends(_gateway_auth)])
async def ir_codes(
    room: str = "",
) -> dict:
    """What a room knows how to send."""
    if room:
        # The listing is the ir skill's documented first step, so it is the
        # first place a wrong room name shows up. Answering 200 with an empty
        # entry told Alfred "that room has no appliances" and invited him to
        # offer to learn one for a room that cannot exist — the guess-again
        # loop every sibling endpoint's 404 was written to prevent.
        _known_room(room)

    store = await asyncio.to_thread(_ir_store)
    rooms = {room: store["rooms"].get(room, {})} if room else store["rooms"]
    out: dict = {}
    for name, entry in rooms.items():
        out[name] = {
            dev: {
                "brand": d.get("brand"),
                "ac_protocol": d.get("ac_protocol"),
                # `confirmed` travels with the summary. Without it the listing
                # is the only thing Alfred can read stored codes from, and he
                # states a code nobody has ever watched work as fact — which is
                # precisely the distinction the flag was added to carry.
                "commands": {
                    c: {**_ir_summary(code),
                        "confirmed": bool(code.get("confirmed", False)),
                        "source": code.get("source"),
                        "updated": code.get("updated")}
                    for c, code in (d.get("commands") or {}).items()
                },
            }
            for dev, d in (entry.get("devices") or {}).items()
        }
    return {"rooms": out}


@app.post("/v1/ir/codes", dependencies=[Depends(_gateway_auth)])
async def ir_save_code(
    payload: dict = Body(...)
) -> dict:
    """Remember a code that worked, or register an appliance.

    The second half of test-and-confirm: a code Alfred found somewhere is sent
    with /v1/ir/send, somebody says the television turned on, and only then
    does it get a name. A body with no code at all just records the appliance —
    which is how an air conditioner gets its protocol.
    """
    room = _known_room(str(payload.get("room") or "").strip())
    device = str(payload.get("device") or "").strip()
    if not device:
        raise HTTPException(status_code=400, detail="device is required")

    store = await asyncio.to_thread(_ir_store)
    entry = store["rooms"].setdefault(room, {}).setdefault("devices", {}).setdefault(device, {})
    if payload.get("brand"):
        entry["brand"] = str(payload["brand"])
    if payload.get("ac_protocol"):
        entry["ac_protocol"] = str(payload["ac_protocol"]).strip().upper()
    entry.setdefault("commands", {})

    command = str(payload.get("command") or "").strip()
    saved = None
    if command:
        code = _ir_code_from(payload)
        entry["commands"][command] = {
            **code,
            "source": str(payload.get("source") or "lookup"),
            # Whoever calls this says whether a person actually saw it work.
            # An unconfirmed code is still sendable and still listed; it just
            # carries the fact that nobody has watched it do anything.
            "confirmed": bool(payload.get("confirmed", False)),
            "updated": int(time.time()),
        }
        saved = _ir_summary(code)

    await asyncio.to_thread(_ir_store_save, store)
    LOG.info("[%s] ir saved %s/%s", room, device, command or "(device only)")
    return {"room": room, "device": device, "command": command or None, "saved": saved}


@app.delete("/v1/ir/codes", dependencies=[Depends(_gateway_auth)])
async def ir_forget_code(
    payload: dict = Body(...)
) -> dict:
    """Forget a command, or a whole appliance."""
    room = str(payload.get("room") or "").strip()
    device = str(payload.get("device") or "").strip()
    command = str(payload.get("command") or "").strip()
    if not room or not device:
        raise HTTPException(status_code=400, detail="room and device are required")
    # The one destructive route, and the only one that used to skip this —
    # so a misspelled room got "'tele' is not a thing in livng", which the
    # ir skill reads as a wrong *device* name and answers by guessing devices.
    _known_room(room)

    store = await asyncio.to_thread(_ir_store)
    devices = _ir_devices(store, room)
    if device not in devices:
        raise HTTPException(status_code=404, detail=f"'{device}' is not a thing in {room}")

    if command:
        if command not in (devices[device].get("commands") or {}):
            raise HTTPException(status_code=404, detail=f"'{device}' in {room} has no '{command}'")
        del devices[device]["commands"][command]
    else:
        del devices[device]

    await asyncio.to_thread(_ir_store_save, store)
    LOG.info("[%s] ir forgot %s/%s", room, device, command or "(all)")
    return {"room": room, "device": device, "command": command or None, "forgotten": True}


# ---------------------------------------------------------------------------
# Sensors: what the room is actually like
# ---------------------------------------------------------------------------
#
# The puck publishes temperature, humidity, light and presence to a retained
# topic. Retained is the whole design: this endpoint subscribes, collects
# whatever the broker already holds, and returns — it never asks a device
# anything and never waits for a reading to be taken. A room whose puck has
# been unplugged for an hour still answers, with a timestamp that says so.
#
# Presence is a room's property and never a person's. A PIR says "something
# moved" and nothing else, which is exactly as much as a device in a shared
# room should know.

SENSOR_COLLECT_S = float(os.environ.get("SENSOR_COLLECT_S", "2"))
# How long after the last retained message to keep listening. The burst arrives
# in one go right after SUBACK, so a quiet gap this long means it is over.
SENSOR_IDLE_S = float(os.environ.get("SENSOR_IDLE_S", "0.25"))


def _sensor_snapshot(rooms: set[str]) -> dict:
    """Everything retained under home-voice/+/sensors/state, right now."""
    import paho.mqtt.client as mqtt

    seen: dict[str, dict] = {}
    done = threading.Event()
    arrived = threading.Event()

    def on_message(_client, _userdata, message) -> None:
        try:
            data = json.loads(message.payload)
        except Exception:
            return
        parts = message.topic.split("/")
        room = parts[1] if len(parts) > 2 else data.get("room")
        if not room:
            return
        seen[room] = data
        arrived.set()
        # Retained messages all arrive in one burst on subscribe, so once every
        # room with a puck has answered there is nothing left to wait for.
        if rooms and rooms.issubset(seen.keys()):
            done.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = on_message
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.subscribe("home-voice/+/sensors/state", qos=1)
        client.loop_start()
        # Wait for the burst to go quiet rather than for every room to answer.
        # A room whose puck has never published — which is every room until one
        # is on a wall — meant `done` could not be set, so each call sat out the
        # full SENSOR_COLLECT_S with Alfred mid-sentence and somebody standing
        # in the room. SENSOR_COLLECT_S stays the ceiling.
        deadline = time.monotonic() + SENSOR_COLLECT_S
        while not done.is_set() and time.monotonic() < deadline:
            arrived.clear()
            if not arrived.wait(min(SENSOR_IDLE_S, deadline - time.monotonic())):
                break        # a quiet gap: the retained burst is over
    except Exception:
        LOG.exception("mqtt sensor snapshot failed")
        raise HTTPException(status_code=502, detail="could not reach the broker")
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    return seen


@app.get("/v1/sensors", dependencies=[Depends(_gateway_auth)])
async def sensors(
    room: str = "",
) -> dict:
    """Temperature, humidity, light and presence, per room.

    Rooms with a puck but no sensors fitted come back present and empty, which
    is a different answer from a room that does not exist — Alfred should say
    "the kitchen has no sensors", not invent a temperature.
    """
    rooms = set(_devices().values())
    if room:
        # The set is already in hand; _known_room re-reading devices.json was
        # the only place in this file that opened it twice per request.
        _known_room(room, rooms)
        rooms = {room}

    snapshot = await asyncio.to_thread(_sensor_snapshot, rooms)

    readings = ("celsius", "humidity", "lux", "motion", "seconds_since_motion")
    out: dict = {}
    for name in sorted(rooms):
        data = snapshot.get(name) or {}
        entry = {key: data[key] for key in readings if key in data}
        # `sensors` follows the readings, not the message. A puck with no parts
        # fitted still publishes a retained `{"room": ...}`, and answering
        # `sensors: true` with nothing in it is the exact inverse of what this
        # endpoint promises — Alfred would say the room has sensors and then
        # have no number to give.
        out[name] = {"sensors": bool(entry), **entry}
    return {"rooms": out}


# ---------------------------------------------------------------------------
# The audio stream: where the wake word lives
# ---------------------------------------------------------------------------

def _load_wake_model() -> WakeModel | None:
    """One model instance per connection — openWakeWord is stateful."""
    if not os.path.exists(WAKE_MODEL):
        LOG.error(
            "no wake model at %s — every stream will connect and never wake. "
            "Train one (see home-voice/wakeword/README.md) and drop it there.",
            WAKE_MODEL,
        )
        return None
    try:
        return WakeModel(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
    except Exception:
        LOG.exception("could not load the wake model at %s", WAKE_MODEL)
        return None


class Session:
    """One device's stream, as a small state machine.

    idle      -> feeding audio to the wake model, discarding it
    capturing -> the wake word fired; buffering the utterance, watching VAD
    busy      -> transcribing/thinking/speaking; audio arriving is ignored
    """

    def __init__(self, ws: WebSocket, room: str) -> None:
        self.ws = ws
        self.room = room
        self.model = _load_wake_model()
        self.state = "idle"
        self.buf = bytearray()
        self.pending = bytearray()      # partial 80 ms chunk between frames
        self.speech_started = False
        self.last_loud_s = 0.0          # AUDIO seconds, not wall clock
        self.capture_wall = 0.0         # wall clock, only as a stuck-stream guard

    async def send(self, payload: dict) -> None:
        try:
            await self.ws.send_text(json.dumps(payload))
        except Exception:
            pass

    async def feed(self, pcm: bytes) -> None:
        if self.state == "busy":
            return                       # our own speaker, or a slow client
        if self.state == "capturing":
            await self._capture(pcm)
            return

        # idle: hand 80 ms at a time to the wake model.
        if self.model is None:
            return
        self.pending.extend(pcm)
        need = WAKE_CHUNK_SAMPLES * 2
        while len(self.pending) >= need:
            chunk = bytes(self.pending[:need])
            del self.pending[:need]
            samples = np.frombuffer(chunk, dtype=np.int16)
            scores = self.model.predict(samples)
            if max(scores.values(), default=0.0) >= WAKE_THRESHOLD:
                LOG.info("[%s] wake (%.2f)", self.room, max(scores.values()))
                await self._begin_capture()
                return

    async def _begin_capture(self) -> None:
        self.state = "capturing"
        self.buf = bytearray()
        self.pending = bytearray()
        self.speech_started = False
        self.last_loud_s = 0.0
        self.capture_wall = time.monotonic()
        # Tell the device first: the ring going blue is the only cue the person
        # standing there gets that it is now listening to them.
        await self.send({"type": "state", "state": "listening"})

    async def _capture(self, pcm: bytes) -> None:
        self.buf.extend(pcm)

        # Timed in AUDIO seconds — bytes captured — rather than wall clock. A
        # device whose WiFi stutters for half a second would otherwise have its
        # sentence cut off mid-word by a silence timer that measured the network
        # instead of the room. It also makes this testable at faster than real
        # time, which is the only way to test it without a microphone.
        elapsed_s = len(self.buf) / (16000 * 2)

        samples = np.frombuffer(pcm, dtype=np.int16)
        level = float(np.abs(samples).mean()) if samples.size else 0.0

        if not self.speech_started:
            if level >= VAD_ONSET_MIN:
                self.speech_started = True
                self.last_loud_s = elapsed_s
            elif elapsed_s > VAD_NO_SPEECH_S:
                # Woken by something that was not a person talking to us. Say
                # nothing at all — a device that apologises every time the
                # television trips the wake word gets unplugged.
                LOG.info("[%s] woke, nobody spoke", self.room)
                await self._reset()
                return
        else:
            if level >= VAD_END_MIN:
                self.last_loud_s = elapsed_s
            elif elapsed_s - self.last_loud_s >= VAD_SILENCE_S:
                await self._finish()
                return

        if elapsed_s >= VAD_MAX_S:
            await self._finish()
            return

        # The one thing audio time cannot catch: a stream that simply stops.
        # Nothing arrives, so nothing advances, and the session would sit in
        # `capturing` forever holding the device's ring blue.
        if time.monotonic() - self.capture_wall > VAD_MAX_S * 3:
            LOG.warning("[%s] capture stalled, giving up", self.room)
            await self._reset()

    async def _reset(self) -> None:
        self.state = "idle"
        self.buf = bytearray()
        self.pending = bytearray()
        if self.model is not None:
            # Without this the scores that triggered the wake are still in the
            # model's buffer and it fires again immediately.
            self.model.reset()
        await self.send({"type": "state", "state": "idle"})

    async def _finish(self) -> None:
        self.state = "busy"
        audio = bytes(self.buf)
        self.buf = bytearray()
        await self.send({"type": "state", "state": "thinking"})

        seconds = len(audio) / (16000 * 2)
        LOG.info("[%s] captured %.1fs", self.room, seconds)

        try:
            transcript = await _transcribe(audio, is_wav=False, rate=16000)
        except Exception:
            LOG.exception("[%s] whisper failed", self.room)
            await self.send({"type": "error", "message": "stt"})
            await self._reset()
            return

        if not transcript:
            LOG.info("[%s] empty transcript", self.room)
            await self._reset()
            return

        LOG.info("[%s] heard: %r", self.room, transcript)

        try:
            reply = await _ask_alfred(self.room, transcript)
        except Exception:
            LOG.exception("[%s] alfred failed", self.room)
            reply = "I could not reach Alfred."

        if not reply:
            reply = "I have no answer for that."

        try:
            pcm, rate, _ = await _synthesize(reply)
        except Exception:
            LOG.exception("[%s] tts failed", self.room)
            await self.send({"type": "error", "message": "tts"})
            await self._reset()
            return

        cid = _store(pcm, rate)
        LOG.info("[%s] replied: %r", self.room, reply[:120])
        await self.send({
            "type": "speak",
            "text": reply,
            "transcript": transcript,
            **_audio_block(cid, pcm, rate),
        })
        # Stays "busy" until the device says it has finished playing. The device
        # stops streaming while the speaker is on, which is what keeps Alfred
        # from waking himself — there is no echo canceller anywhere in this.

    async def resumed(self) -> None:
        await self._reset()


@app.websocket("/v1/stream")
async def stream(ws: WebSocket) -> None:
    # The token is a query parameter rather than a header: not every ESP32
    # WebSocket client can set headers on the upgrade, and this matches how
    # nanobot's own websocket is gated.
    token = ws.query_params.get("token")
    devices = _devices()
    room = devices.get(token or "")
    if not room:
        await ws.close(code=4403)
        LOG.warning("stream refused: unknown device token")
        return

    await ws.accept()
    session = Session(ws, room)
    LOG.info("[%s] stream open%s", room, "" if session.model else " (NO WAKE MODEL)")
    await session.send({"type": "state", "state": "idle"})
    if session.model is None:
        await session.send({"type": "error", "message": "no wake model"})

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if (data := msg.get("bytes")) is not None:
                await session.feed(data)
            elif (text := msg.get("text")) is not None:
                try:
                    payload = json.loads(text)
                except Exception:
                    continue
                if payload.get("type") == "resume":
                    await session.resumed()
    except WebSocketDisconnect:
        pass
    except Exception:
        LOG.exception("[%s] stream error", room)
    finally:
        LOG.info("[%s] stream closed", room)

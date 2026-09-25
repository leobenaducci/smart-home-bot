"""Qwen3-TTS as a sidecar, so the voice gateway never has to hold a GPU.

Why this is a separate service and not two more imports in gateway.py:

- It wants torch and CUDA, which takes an image from ~1 GB to several.
- It wants VRAM, and the card is shared. `nvidia-smi` on compute shows a 12 GB
  RTX 3060 with ~8.4 GB free at idle, and Ollama loads the vision model on
  demand for Alfred's vision turns — `qwen3-vl:4b`, 3.3 GB, since the house
  moved down from the 8b (6.1 GB). That leaves real room now, which it did not
  before: a small TTS model and a vision turn fit together. It is still not a
  reason to pin one for the rest of the day. See MODEL and IDLE_UNLOAD_S below —
  this process is built to let go, and the camera detectors want the card too.
- The gateway must keep talking when this is down. It is a listening bench: a
  thing for choosing a voice, not a thing the house runs on.

Same shape as faster-whisper next door — a small HTTP service that owns the
card for one job.

    GET  /health      is a model loaded, and how much VRAM is it holding
    GET  /voices      what can speak
    POST /synthesize  {text, voice} -> raw int16 mono PCM, X-Sample-Rate header

PCM rather than WAV because the gateway is the only caller and it wraps or
forwards as its own callers need — the puck wants headerless PCM, the browser
wants a container, and putting a header on here would mean stripping it there.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import numpy as np
import torch
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import Response

LOG = logging.getLogger("qwen-tts")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

# 0.6B by default, not 1.7B. The bigger one is better and the card cannot
# reliably hold it *and* let Ollama load an 8 GB vision model when somebody
# points a camera at Alfred. Override to compare; watch nvidia-smi when you do.
MODEL = os.environ.get("QWEN_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-Base")
DEVICE = os.environ.get("QWEN_TTS_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
DTYPE = torch.bfloat16 if DEVICE == "cuda" else torch.float32

# Give the card back after a quiet spell. Somebody comparing voices does it in
# bursts of a minute and then goes away; holding 1.5 GB of VRAM for the rest of
# the day so that Ollama cannot load is the wrong trade for a bench.
# 0 disables unloading, which is what you want if this ever becomes the house
# engine rather than a comparison.
IDLE_UNLOAD_S = float(os.environ.get("QWEN_TTS_IDLE_UNLOAD_S", "300"))

# Reference clips for cloned voices, mounted read-only. A file called
# mayordomo.wav becomes the voice `mayordomo`. Cross-lingual cloning carries
# the accent of the *reference*, so a Chilean butler needs a Chilean recording:
# a Spanish reference read in Spanish is the only thing that gets the vowels
# right, and an English reference speaking Spanish will not.
VOICES_DIR = os.environ.get("QWEN_TTS_VOICES_DIR", "/voices")

app = FastAPI(title="qwen-tts sidecar")

_model = None
_processor = None
_last_used = 0.0
_lock = asyncio.Lock()          # one generation at a time — the card is shared
_load_lock = asyncio.Lock()     # …and exactly one load is ever in flight
_loading: asyncio.Task | None = None


REFERENCE_EXTS = (".wav", ".flac", ".mp3")


def _preset_voices() -> dict[str, str | None]:
    """Voice id -> the reference clip behind it, or None for the model's own.

    One directory listing, one extension list. Building the ids here and then
    re-probing the same three extensions at synthesis time meant adding a
    format in one place and not the other would offer a voice in the dropdown
    that then silently spoke in the model's default instead.
    """
    out: dict[str, str | None] = {"default": None}
    try:
        for name in sorted(os.listdir(VOICES_DIR)):
            stem, ext = os.path.splitext(name)
            if ext.lower() in REFERENCE_EXTS and stem != "default":
                out[stem] = os.path.join(VOICES_DIR, name)
    except FileNotFoundError:
        pass
    except OSError:
        LOG.warning("could not list %s; only the model's own voice is available", VOICES_DIR)
    return out


def _voice_labels() -> list[dict]:
    return [{"id": v, "label": "la del modelo" if v is None or v == "default" else f"clonada: {v}"}
            for v in _preset_voices()]


async def _ensure_model():
    """Load on first use, not at boot.

    Booting straight into VRAM would mean this container competes for the card
    from the moment compose brings it up, including on a box that was only
    restarted. Nobody is listening at that point.

    The load runs in the background and callers are told to come back, rather
    than being held on an open socket for the duration. The weights are not
    baked (see the Dockerfile), so the very first load is a multi-GB download —
    far longer than the gateway's QWEN_TIMEOUT_S, which meant the documented
    first click on the bench timed out, reported the sidecar as unreachable,
    and left every retry queued behind the same lock while the thing was in
    fact working.
    """
    global _last_used
    _last_used = time.monotonic()
    if _model is not None:
        return _model, _processor

    async with _load_lock:
        if _model is None and _loading is None:
            _start_load()
    raise HTTPException(
        status_code=503,
        detail=f"loading {MODEL} — this is the first use and the weights are not baked. Try again shortly.",
        headers={"Retry-After": "20"},
    )


def _start_load() -> None:
    global _loading

    async def _load() -> None:
        global _model, _processor, _loading
        from transformers import AutoModelForCausalLM, AutoProcessor

        LOG.info("loading %s onto %s (%s)", MODEL, DEVICE, DTYPE)
        started = time.monotonic()
        try:
            processor = await asyncio.to_thread(AutoProcessor.from_pretrained, MODEL)
            model = await asyncio.to_thread(
                lambda: AutoModelForCausalLM.from_pretrained(
                    MODEL, torch_dtype=DTYPE, device_map=DEVICE, trust_remote_code=True,
                ).eval()
            )
        except Exception:
            LOG.exception("could not load %s", MODEL)
            _loading = None
            return
        # Published together: nothing may see a model without its processor.
        _processor, _model = processor, model
        _loading = None
        LOG.info("loaded in %.1fs", time.monotonic() - started)

    _loading = asyncio.create_task(_load())


def _unload() -> None:
    global _model, _processor
    if _model is None:
        return
    LOG.info("idle for %.0fs — giving the card back", time.monotonic() - _last_used)
    _model = None
    _processor = None
    if DEVICE == "cuda":
        torch.cuda.empty_cache()


async def _idle_watch() -> None:
    while True:
        await asyncio.sleep(30)
        if _model is not None and time.monotonic() - _last_used > IDLE_UNLOAD_S:
            async with _lock:
                # Checked again inside the lock. The first check is a cheap
                # filter taken while a request may be arriving; acting on it
                # blind meant a generation that started in that gap either
                # waited out an unload and paid a full cold reload, or finished
                # and was unloaded seconds after being used.
                if _model is not None and time.monotonic() - _last_used > IDLE_UNLOAD_S:
                    _unload()


@app.on_event("startup")
async def _startup() -> None:
    if IDLE_UNLOAD_S:
        asyncio.create_task(_idle_watch())


@app.get("/health")
async def health() -> dict:
    vram = {}
    # Only once a model is actually resident. Both of these calls run torch's
    # CUDA lazy init, which claims a primary context — a few hundred MB that
    # `_unload()` cannot give back, since empty_cache() frees cached blocks and
    # not the context. A health check every 30 s would therefore have taken the
    # card hostage on behalf of a service holding nothing, which is the one
    # thing this whole file is arranged to avoid.
    if DEVICE == "cuda" and _model is not None and torch.cuda.is_available():
        free, total = torch.cuda.mem_get_info()
        vram = {"free_mb": free // 2**20, "total_mb": total // 2**20,
                "held_mb": torch.cuda.memory_allocated() // 2**20}
    return {"ok": True, "model": MODEL, "device": DEVICE,
            "loaded": _model is not None, **vram}


@app.get("/voices")
async def voices() -> dict:
    return {"voices": _voice_labels(), "model": MODEL}


@app.post("/synthesize")
async def synthesize(payload: dict = Body(...)) -> Response:
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    voice = str(payload.get("voice") or "default")

    known = _preset_voices()
    if voice not in known:
        raise HTTPException(status_code=404, detail=f"no voice '{voice}'; have {sorted(known)}")
    reference = known[voice]

    # Kick the load off (and answer 503) before taking the generation lock, so
    # a cold start does not park every other caller behind it.
    if _model is None:
        await _ensure_model()

    # One at a time. Two concurrent generations on a card that is also expected
    # to hand 6 GB to Ollama is how this ends up being the thing that broke the
    # cameras.
    async with _lock:
        model, processor = await _ensure_model()
        started = time.monotonic()
        try:
            audio, rate = await asyncio.to_thread(
                _generate, model, processor, text, reference)
        except Exception as exc:
            LOG.exception("generation failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        took = time.monotonic() - started

    seconds = len(audio) / rate if rate else 0.0
    LOG.info("%.1fs of audio in %.1fs (%.2fx realtime) — %r",
             seconds, took, (seconds / took) if took else 0.0, text[:60])

    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    return Response(content=pcm, media_type="application/octet-stream",
                    headers={"X-Sample-Rate": str(rate),
                             "X-Synth-Seconds": f"{took:.2f}"})


def _generate(model, processor, text: str, reference: str | None):
    """The one part that is specific to Qwen3-TTS's interface.

    Kept alone and small on purpose: it is the only thing here that has to
    change if the checkpoint's API moves, and everything around it — loading,
    unloading, the lock, the wire format — is ours.
    """
    kwargs: dict = {"text": text, "return_tensors": "pt"}
    if reference:
        kwargs["reference_audio"] = reference
    inputs = processor(**kwargs)
    inputs = {k: (v.to(DEVICE) if hasattr(v, "to") else v) for k, v in inputs.items()}

    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=int(os.environ.get("QWEN_MAX_TOKENS", "4096")))

    audio = processor.batch_decode(out, return_audio=True)[0]
    if hasattr(audio, "cpu"):
        audio = audio.cpu().float().numpy()
    # The caller's int16 conversion clips to [-1, 1], so it assumes a float
    # waveform and one channel. The `hasattr` above already concedes this may
    # not be a tensor, and an int16 array through that conversion is not a
    # quiet degradation — it is a full-scale square wave delivered as a
    # perfectly well-formed PCM buffer, with the right length and rate and no
    # error anywhere.
    audio = np.asarray(audio)
    if audio.ndim > 1:
        # Take one channel rather than letting reshape(-1) interleave two into
        # a "mono" buffer that plays at double speed.
        audio = audio[..., 0] if audio.shape[-1] <= 2 else audio[0]
    was_integer = np.issubdtype(audio.dtype, np.integer)
    audio = audio.astype(np.float32).reshape(-1)
    if was_integer:
        audio /= 32768.0
    rate = int(getattr(processor, "sampling_rate", 24000) or 24000)
    return audio, rate

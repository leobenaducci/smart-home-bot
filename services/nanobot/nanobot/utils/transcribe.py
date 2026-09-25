"""Faster-Whisper transcription server client (http://whisper.home:8000)."""
from __future__ import annotations

import base64
import re

import httpx
from loguru import logger


_DATA_URL_RE = re.compile(r"^data:([^;]+);base64,(.+)$", re.DOTALL)

AUDIO_MIME_ALLOWED: frozenset[str] = frozenset({
    "audio/webm",
    "audio/ogg",
    "audio/mp4",
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
    "audio/flac",
})

_AUDIO_EXTENSIONS: dict[str, str] = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
}


def is_audio_data_url(data_url: str) -> bool:
    if not isinstance(data_url, str):
        return False
    m = _DATA_URL_RE.match(data_url)
    if not m:
        return False
    return m.group(1).strip().lower() in AUDIO_MIME_ALLOWED


async def transcribe_audio(
    data_url: str,
    *,
    api_base: str = "http://whisper.home:8000",
) -> str:
    """Transcribe audio from a base64 data URL via the Faster-Whisper server.

    POSTs to ``{api_base}/transcribe`` and returns the transcribed text.
    """
    m = _DATA_URL_RE.match(data_url)
    if not m:
        raise ValueError("invalid data URL")
    mime = m.group(1).strip().lower()
    if mime not in AUDIO_MIME_ALLOWED:
        raise ValueError(f"unsupported audio MIME type: {mime}")

    audio_bytes = base64.b64decode(m.group(2))
    ext = _AUDIO_EXTENSIONS.get(mime, ".webm")
    filename = f"recording{ext}"

    url = api_base.rstrip("/") + "/transcribe"
    logger.debug("transcribe: POST {} size={} bytes", url, len(audio_bytes))

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            url,
            files={"file": (filename, audio_bytes, mime)},
        )
        resp.raise_for_status()
        data = resp.json()

    text: str = data.get("text", "")
    return text.strip()

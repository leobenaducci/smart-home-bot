"""household A10: the fork's voice-note settings keep working on upstream's transcription.

The fork transcribed channel voice notes with Groq or OpenAI Whisper, set by
``channels.transcriptionProvider`` and ``channels.transcriptionLanguage``.
Its Faster-Whisper client (``utils/transcribe.py``) was never called. Upstream
moved the settings to a top-level ``transcription`` block and still reads the
fork's channel keys, so nothing is ported; this pins that.
"""

from __future__ import annotations

from nanobot.audio.transcription import resolve_transcription_config
from nanobot.config.schema import Config


def test_the_forks_channel_keys_still_choose_provider_and_language():
    cfg = Config.model_validate({
        "providers": {"openai": {"apiKey": "k"}},
        "channels": {"transcriptionProvider": "openai", "transcriptionLanguage": "es"},
    })
    eff = resolve_transcription_config(cfg)
    assert eff.provider == "openai" and eff.language == "es" and eff.enabled


def test_the_top_level_block_wins_over_the_channel_keys():
    cfg = Config.model_validate({
        "channels": {"transcriptionProvider": "openai", "transcriptionLanguage": "es"},
        "transcription": {"provider": "groq", "language": "en"},
    })
    eff = resolve_transcription_config(cfg)
    assert eff.provider == "groq" and eff.language == "en"

"""The prewarm is an optimisation for the household's own engine, and nothing else.

2026-09-10: with `everyday` on deepseek-v4-flash, the startup prewarm sent a
16k-char system-only prompt to OpenCode Zen, timed out three times, and the
retry ladder marked the model DOWN for an hour on all six assistants -- which
answered on the local fallback until it expired. It also held each assistant's
API dark until the retries were done.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent import prewarm as P


@pytest.mark.parametrize("base, local", [
    ("http://host.docker.internal:11434/v1", True),
    ("http://127.0.0.1:11434/v1", True),
    ("http://192.168.88.35:11436/v1", True),
    ("http://assistant.home:1919/v1", True),
    ("https://opencode.ai/zen/v1", False),
    ("https://api.together.xyz/v1", False),
    ("https://ollama.com", False),
    ("", False), (None, False),
])
def test_only_the_house_engine_is_local(base, local):
    assert P.is_local_engine(base) is local


def _ctx():
    ctx = MagicMock()
    ctx.shared_prompt_prefix.return_value = "x" * 5000
    return ctx


@pytest.mark.asyncio
async def test_a_hosted_provider_is_never_prewarmed():
    provider = MagicMock(api_base="https://opencode.ai/zen/v1")
    provider.chat = AsyncMock(); provider.chat_with_retry = AsyncMock()
    assert await P.prewarm_shared_prefix(provider, _ctx(), model="deepseek-v4-flash") is False
    provider.chat.assert_not_called()
    provider.chat_with_retry.assert_not_called()


@pytest.mark.asyncio
async def test_a_local_engine_gets_one_plain_request():
    """`chat`, not `chat_with_retry`: no retries, no fallback, no outage marking."""
    provider = MagicMock(api_base="http://host.docker.internal:11434/v1")
    provider.chat = AsyncMock(return_value=MagicMock()); provider.chat_with_retry = AsyncMock()
    assert await P.prewarm_shared_prefix(provider, _ctx(), model="ornith-1.5:9b") is True
    provider.chat.assert_awaited_once()
    provider.chat_with_retry.assert_not_called()
    assert provider.chat.await_args.kwargs["max_tokens"] == 1


@pytest.mark.asyncio
async def test_a_stuck_engine_cannot_hold_the_start(monkeypatch):
    monkeypatch.setattr(P, "PREWARM_TIMEOUT_S", 0.05)

    async def never(**_kw):
        await asyncio.sleep(5)
    provider = MagicMock(api_base="http://127.0.0.1:11434/v1", chat=never)
    assert await P.prewarm_shared_prefix(provider, _ctx()) is False

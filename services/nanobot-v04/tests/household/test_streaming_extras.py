"""household A14: echo hold while streaming, StepFun through a gateway, trim_to on stream_end."""

from __future__ import annotations

import pytest

from nanobot.providers.base import LLMResponse
from nanobot.providers.openai_compat_provider import OpenAICompatProvider

REASONING = "The person asked about the lights in Oficina Tomi. I should check the state first."


class _Streaming(OpenAICompatProvider):
    def __init__(self, thinking, content, model="deepseek-v4"):
        super().__init__(api_key="k", api_base="http://gw.test/v1", default_model=model)
        self.thinking, self.content = thinking, content

    async def _chat_stream_unsettled(self, messages, tools=None, model=None, max_tokens=4096,
                                     temperature=0.7, reasoning_effort=None, tool_choice=None,
                                     on_content_delta=None, on_thinking_delta=None,
                                     on_tool_call_delta=None, provider_context=None):
        for piece in self.thinking:
            await on_thinking_delta(piece)
        for piece in self.content:
            await on_content_delta(piece)
        return LLMResponse(content="".join(self.content) or None, finish_reason="stop",
                           reasoning_content="".join(self.thinking) or None)


async def _stream(provider):
    painted, thought = [], []

    async def on_content(d):
        painted.append(d)

    async def on_thinking(d):
        thought.append(d)

    response = await provider.chat_stream([{"role": "user", "content": "hola"}],
                                          on_content_delta=on_content, on_thinking_delta=on_thinking)
    return response, "".join(painted), "".join(thought)


@pytest.mark.asyncio
async def test_a_streamed_echo_of_the_reasoning_is_never_painted():
    halves = [REASONING[:30], REASONING[30:]]
    response, painted, thought = await _stream(_Streaming(halves, halves))
    assert response.content is None and painted == "" and thought == REASONING


@pytest.mark.asyncio
async def test_a_real_reply_is_released_as_soon_as_it_is_not_the_reasoning():
    response, painted, _ = await _stream(_Streaming([REASONING], ["The ", "light is on."]))
    assert painted == "The light is on." and response.content == "The light is on."


@pytest.mark.asyncio
async def test_a_reply_without_reasoning_is_not_held():
    response, painted, _ = await _stream(_Streaming([], ["Done."]))
    assert painted == "Done."


@pytest.mark.asyncio
async def test_stepfun_through_a_gateway_answers_in_its_reasoning():
    response, painted, _ = await _stream(_Streaming(["The light is on."], [], model="stepfun/step-3"))
    assert response.content == "The light is on."


def test_trim_to_is_the_kept_text(monkeypatch):
    from types import SimpleNamespace

    from nanobot.household import stream_trim

    monkeypatch.setattr("nanobot.household.skill_roots.household_enabled", lambda: True)
    block = 'Checking.\n```json\n{"skill": "lights", "action": "state", "name": "Luz Paula"}\n```'
    kept = stream_trim.trim_to(SimpleNamespace(content=block))
    assert kept is not None and "skill" not in kept and kept.startswith("Checking.")
    assert stream_trim.trim_to(SimpleNamespace(content="")) == ""
    assert stream_trim.trim_to(SimpleNamespace(content=None)) is None
    monkeypatch.setattr("nanobot.household.skill_roots.household_enabled", lambda: False)
    assert stream_trim.trim_to(SimpleNamespace(content=block)) is None


@pytest.mark.asyncio
async def test_the_stream_end_frame_carries_trim_to_to_the_page(tmp_path):
    import json
    from unittest.mock import AsyncMock, MagicMock

    from nanobot.channels.websocket.runtime import WebSocketChannel
    from nanobot.channels.websocket.tests.test_websocket_channel import _basic_handler

    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"], "streaming": True}, bus,
                               gateway=_basic_handler(bus, workspace_path=tmp_path))
    ws = AsyncMock()
    channel._attach(ws, "chat-1")
    await channel.send_delta("chat-1", "Checking.\n```json", {"_trim_to": "Checking."},
                             stream_id="sid", stream_end=True)
    final = json.loads(ws.send.await_args.args[0])
    assert final["event"] == "stream_end" and final["trim_to"] == "Checking."

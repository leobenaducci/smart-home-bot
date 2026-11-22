"""household: a file the reply announced and left out still reaches the chat.

Fork ``_undelivered_download_links``: camera snapshots were captured and
served fifteen times running without one reaching the chat, because the model
wrote "here it is" and dropped the link. Driven through process_direct.
"""

from __future__ import annotations

import pytest
from agent.conftest import make_provider

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import tool_parameters_schema
from nanobot.bus.queue import MessageBus
from nanobot.household.loop import HouseholdAgentLoop
from nanobot.providers.base import LLMResponse, ToolCallRequest

LINK = "[Front](download:media/cam_front_1795339800.jpg)"
KEY = "websocket:homeweb:999000111:2026-11-22:1"


@tool_parameters(tool_parameters_schema(required=[]))
class _Camera(Tool):
    @property
    def name(self) -> str:
        return "camera_snapshot"

    @property
    def description(self) -> str:
        return "Snapshot of a camera."

    async def execute(self, **kwargs):
        return f'{{"camera": "Front", "file": "{LINK}"}}'


def _loop(tmp_path, final: str):
    provider = make_provider(default_model="cheap")
    provider.provider_name = "custom"
    replies = [LLMResponse(content="", tool_calls=[ToolCallRequest(id="c1", name="camera_snapshot",
                                                                   arguments={})])]
    replies.append(LLMResponse(content=final))
    it = iter(replies)

    async def chat(**_kw):
        return next(it)

    provider.chat_with_retry = provider.chat_stream_with_retry = provider.chat = chat
    bus = MessageBus()
    loop = HouseholdAgentLoop(bus=bus, provider=provider, workspace=tmp_path,
                              model="cheap", context_window_tokens=128_000)
    loop.tools.register(_Camera())
    return loop, bus


def _sent(bus) -> list[str]:
    out = []
    while not bus.outbound.empty():
        out.append(bus.outbound.get_nowait().content)
    return out


@pytest.mark.asyncio
async def test_a_link_the_reply_left_out_is_sent_on_its_own(tmp_path):
    loop, bus = _loop(tmp_path, "Acá está la cámara de adelante 👇")
    await loop.process_direct("mostrame la cámara de adelante", session_key=KEY,
                              channel="websocket", chat_id=KEY.split(":", 1)[1])
    assert LINK in _sent(bus)


@pytest.mark.asyncio
async def test_a_reply_that_carries_the_link_is_left_alone(tmp_path):
    loop, bus = _loop(tmp_path, f"Acá está: {LINK}")
    await loop.process_direct("mostrame la cámara de adelante", session_key=KEY,
                              channel="websocket", chat_id=KEY.split(":", 1)[1])
    assert LINK not in _sent(bus)


@pytest.mark.asyncio
async def test_history_never_counts_as_undelivered(tmp_path):
    loop, bus = _loop(tmp_path, f"Acá está: {LINK}")
    await loop.process_direct("mostrame la cámara", session_key=KEY,
                              channel="websocket", chat_id=KEY.split(":", 1)[1])
    _sent(bus)
    provider = loop.provider

    async def chat(**_kw):
        return LLMResponse(content="De nada.")

    provider.chat_with_retry = provider.chat_stream_with_retry = provider.chat = chat
    await loop.process_direct("gracias", session_key=KEY, channel="websocket",
                              chat_id=KEY.split(":", 1)[1])
    assert LINK not in _sent(bus)

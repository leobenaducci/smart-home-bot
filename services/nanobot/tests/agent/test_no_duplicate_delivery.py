"""A MessageTool reply must reach the reader exactly once.

Production wires ``MessageTool(send_callback=self.bus.publish_outbound)``, so
anything the tool sends is already on its way to the channel. `_process_message`
*also* returns that content — deliberately, because `process_direct` callers
(the OpenAI-compatible API) never read the bus and would otherwise get nothing
back. The run loop then published that return value to the same bus, and the
family saw the message twice: "Patio, ahora mismo:" arrived twice for one
camera request.

`_already_sent` separates the two audiences: the API caller still gets its copy,
the bus does not get a second one.
"""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import mock_provider

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest


def _loop_with_real_bus() -> tuple[AgentLoop, MessageBus]:
    """Wired the way production wires it — MessageTool publishes to the bus."""
    bus = MessageBus()
    provider = mock_provider("test-model")
    loop = AgentLoop(
        bus=bus, provider=provider,
        workspace=Path(tempfile.mkdtemp()), model="test-model",
    )
    return loop, bus


async def _drain(bus: MessageBus, seconds: float = 0.2) -> list[str]:
    collected: list[str] = []

    async def collect():
        while True:
            collected.append((await bus.outbound.get()).content)

    task = asyncio.create_task(collect())
    await asyncio.sleep(seconds)
    task.cancel()
    return collected


@pytest.mark.asyncio
async def test_message_tool_content_is_delivered_once() -> None:
    loop, bus = _loop_with_real_bus()
    tool_call = ToolCallRequest(
        id="c1", name="message",
        arguments={"content": "Patio, ahora mismo:",
                   "channel": "websocket", "chat_id": "chat1"},
    )
    calls = iter([
        LLMResponse(content="", tool_calls=[tool_call]),
        LLMResponse(content="Done", tool_calls=[]),
    ])
    loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
    loop.tools.get_definitions = MagicMock(return_value=[])

    msg = InboundMessage(channel="websocket", sender_id="u1",
                         chat_id="chat1", content="mándame el patio")
    result = await loop._process_message(msg)

    # Exactly what nanobot/agent/loop.py does with the return value.
    if result is not None and not (result.metadata or {}).get("_already_sent"):
        await bus.publish_outbound(result)

    delivered = [c for c in await _drain(bus) if c]
    assert delivered.count("Patio, ahora mismo:") == 1, (
        f"reader saw it {delivered.count('Patio, ahora mismo:')} times: {delivered}"
    )


@pytest.mark.asyncio
async def test_direct_callers_still_receive_the_content() -> None:
    """The reason the return value exists at all. Suppressing it outright would
    make the OpenAI-compatible API answer camera requests with nothing."""
    loop, _ = _loop_with_real_bus()
    tool_call = ToolCallRequest(
        id="c1", name="message",
        arguments={"content": "Patio, ahora mismo:",
                   "channel": "cli", "chat_id": "direct"},
    )
    calls = iter([
        LLMResponse(content="", tool_calls=[tool_call]),
        LLMResponse(content="Done", tool_calls=[]),
    ])
    loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
    loop.tools.get_definitions = MagicMock(return_value=[])

    result = await loop.process_direct("mándame el patio", channel="cli", chat_id="direct")

    assert result is not None
    assert result.content == "Patio, ahora mismo:"

"""Tests for structured tool-event progress metadata emitted by AgentLoop."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import mock_provider

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = mock_provider("test-model")
    return AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")


class TestToolEventProgress:
    """_run_agent_loop emits structured tool_events via on_progress."""

    @pytest.mark.asyncio
    async def test_start_and_finish_events_emitted(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        tool_call = ToolCallRequest(id="call1", name="custom_tool", arguments={"path": "foo.txt"})
        calls = iter([
            LLMResponse(content="Visible", tool_calls=[tool_call]),
            LLMResponse(content="Done", tool_calls=[]),
        ])
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.prepare_call = MagicMock(return_value=(None, {"path": "foo.txt"}, None))
        loop.tools.execute = AsyncMock(return_value="ok")

        progress: list[tuple[str, bool, list[dict] | None]] = []

        async def on_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[dict] | None = None,
        ) -> None:
            progress.append((content, tool_hint, tool_events))

        final_content, _, _, _, _ = await loop._run_agent_loop([], on_progress=on_progress)

        assert final_content == "Done"
        assert progress == [
            ("Visible", False, None),
            (
                'custom_tool("foo.txt")',
                True,
                [{
                    "version": 1,
                    "phase": "start",
                    "call_id": "call1",
                    "name": "custom_tool",
                    "arguments": {"path": "foo.txt"},
                    "result": None,
                    "error": None,
                    "files": [],
                    "embeds": [],
                }],
            ),
            (
                "",
                False,
                [{
                    "version": 1,
                    "phase": "end",
                    "call_id": "call1",
                    "name": "custom_tool",
                    "arguments": {"path": "foo.txt"},
                    "result": "ok",
                    "error": None,
                    "files": [],
                    "embeds": [],
                }],
            ),
        ]

    @pytest.mark.asyncio
    async def test_bus_progress_forwards_tool_events_to_outbound_metadata(self, tmp_path: Path) -> None:
        """When run() handles a bus message, _tool_events lands in OutboundMessage metadata."""
        bus = MessageBus()
        provider = mock_provider("test-model")
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

        tool_call = ToolCallRequest(id="tc1", name="exec", arguments={"command": "ls"})
        calls = iter([
            LLMResponse(content="", tool_calls=[tool_call]),
            LLMResponse(content="Done", tool_calls=[]),
        ])
        loop.provider.chat_with_retry = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        loop.tools.get_definitions = MagicMock(return_value=[])
        loop.tools.prepare_call = MagicMock(return_value=(None, {"command": "ls"}, None))
        loop.tools.execute = AsyncMock(return_value="file.txt")

        msg = InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="chat1",
            content="run ls",
        )
        await loop._dispatch(msg)

        # Drain all outbound messages and find the one carrying _tool_events
        outbound = []
        while bus.outbound_size > 0:
            outbound.append(await bus.consume_outbound())

        tool_event_msgs = [m for m in outbound if m.metadata and m.metadata.get("_tool_events")]
        assert tool_event_msgs, "expected at least one outbound message with _tool_events"

        start_msgs = [m for m in tool_event_msgs if m.metadata["_tool_events"][0]["phase"] == "start"]
        finish_msgs = [m for m in tool_event_msgs if m.metadata["_tool_events"][0]["phase"] in ("end", "error")]
        assert start_msgs, "expected a start-phase tool event"
        assert finish_msgs, "expected a finish-phase tool event"

        start = start_msgs[0].metadata["_tool_events"][0]
        assert start["name"] == "exec"
        assert start["call_id"] == "tc1"
        assert start["result"] is None

        finish = finish_msgs[0].metadata["_tool_events"][0]
        assert finish["phase"] == "end"
        assert finish["result"] == "file.txt"


class TestReasoningLiveness:
    """Reasoning deltas become a sparse "still working" tick, and nothing else.

    A turn that thinks for a minute before its first visible token was being
    read as a hang and pushed to a sub-agent mid-conversation. The reasoning is
    already on the wire; this is what makes it count as a sign of life.
    """

    def _hook(self, tmp_path: Path, on_progress):
        from nanobot.agent.loop import _LoopHook

        return _LoopHook(_make_loop(tmp_path), on_progress=on_progress)

    @pytest.mark.asyncio
    async def test_first_delta_reports_the_turn_is_alive(self, tmp_path: Path) -> None:
        seen: list[dict] = []

        async def on_progress(content: str, *, tool_hint: bool = False,
                              alive: bool = False, **_kw) -> None:
            seen.append({"content": content, "alive": alive})

        hook = self._hook(tmp_path, on_progress)
        await hook.on_reasoning(MagicMock(), "a ver, son las")

        assert seen == [{"content": "", "alive": True}]

    @pytest.mark.asyncio
    async def test_the_thinking_itself_is_never_forwarded(self, tmp_path: Path) -> None:
        """Chain-of-thought is not something the family reads mid-answer."""
        seen: list[str] = []

        async def on_progress(content: str, *, tool_hint: bool = False,
                              alive: bool = False, **_kw) -> None:
            seen.append(content)

        hook = self._hook(tmp_path, on_progress)
        await hook.on_reasoning(MagicMock(), "el usuario vive en Santiago")

        assert "Santiago" not in "".join(seen)

    @pytest.mark.asyncio
    async def test_a_flood_of_deltas_collapses_to_one_tick(self, tmp_path: Path) -> None:
        """Hundreds a second in, one every few seconds out."""
        seen: list[bool] = []

        async def on_progress(content: str, *, tool_hint: bool = False,
                              alive: bool = False, **_kw) -> None:
            seen.append(alive)

        hook = self._hook(tmp_path, on_progress)
        for _ in range(500):
            await hook.on_reasoning(MagicMock(), "…")

        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_a_callback_that_cannot_be_told_is_not_called(self, tmp_path: Path) -> None:
        """The outbound-bus progress handler takes no keywords at all, and an
        empty progress message would reach a real reader as an empty line."""
        seen: list[str] = []

        async def bus_style_progress(content: str, *, tool_hint: bool = False) -> None:
            seen.append(content)

        hook = self._hook(tmp_path, bus_style_progress)
        await hook.on_reasoning(MagicMock(), "pensando")

        assert seen == []

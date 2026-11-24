"""household C3: the websocket as HomeCore's chat relay uses it (recorded contract).

HomeCore's ``/chat/events`` (services/home-core/local/app.py) opens the
assistant's websocket, reads one frame (``ready``), sends
``{"type": "attach", "chat_id"}`` and then forwards ``message``, ``delta``,
``stream_end`` and ``subagent_status`` frames to the page. ``chat.html`` reads
``text``, ``stream_id``, ``trim_to``, and ``subagent_event``/``task_id``/
``label``/``kind``/``text`` off them. This drives a real trial server with a
plain client doing exactly that, so a frame or field the page depends on
cannot disappear unnoticed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import websockets

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import StreamDeltaEvent, StreamEndEvent
from nanobot.channels.manager import ChannelManager
from nanobot.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanobot.household import task_panel
from nanobot.webui.gateway_services import build_gateway_services

PORT = 29977
CHAT = "homeweb:999000111:2026-11-21:s-1:3"     # a space conversation, as HomeCore names it


def _channel(bus: Any) -> WebSocketChannel:
    cfg: dict[str, Any] = {"enabled": True, "allowFrom": ["*"], "host": "127.0.0.1", "port": PORT,
                           "path": "/", "websocketRequiresToken": False, "streaming": True}
    gateway = build_gateway_services(
        config=WebSocketConfig.model_validate(cfg), bus=bus, session_manager=None,
        static_dist_path=None, workspace_path=Path.cwd(), default_restrict_to_workspace=False,
        runtime_model_name=None, runtime_surface="browser", runtime_capabilities_overrides=None)
    return WebSocketChannel(cfg, bus, gateway=gateway)


async def _frames(conn: Any, want: int) -> list[dict]:
    out: list[dict] = []
    while len(out) < want:
        data = json.loads(await asyncio.wait_for(conn.recv(), timeout=5))
        if data.get("event") in ("message", "delta", "stream_end", "subagent_status"):
            out.append(data)                                # what the relay forwards
    return out


@pytest.mark.asyncio
async def test_a_streamed_reply_and_a_task_reach_homecore_in_the_shape_its_page_reads(monkeypatch):
    monkeypatch.setattr(task_panel, "relay", lambda: MagicMock())
    monkeypatch.setattr("nanobot.channels.homeweb_relay.HomeCoreRelay.post", lambda self, record: None)
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    channel = _channel(bus)
    server = asyncio.create_task(channel.start())
    try:
        for _ in range(50):
            try:
                conn = await websockets.connect(f"ws://127.0.0.1:{PORT}/")
                break
            except OSError:
                await asyncio.sleep(0.05)
        async with conn:
            await conn.recv()                                       # ready, consumed unread
            await conn.send(json.dumps({"type": "attach", "chat_id": CHAT}))
            await asyncio.sleep(0.1)

            send = ChannelManager._send_stream_event
            await send(channel, OutboundMessage(channel="websocket", chat_id=CHAT, content="Checking ",
                                                event=StreamDeltaEvent(stream_id="s1")),
                       StreamDeltaEvent(stream_id="s1"))
            end = StreamEndEvent(stream_id="s1", trim_to="Checking the lights.")
            await send(channel, OutboundMessage(channel="websocket", chat_id=CHAT, content="",
                                                metadata={"_trim_to": "Checking the lights."},
                                                event=end), end)
            task_panel.post(CHAT, "start", "t1", "lights")
            task_panel.post(CHAT, "progress", "t1", "lights", kind="tool", text="HassTurnOn", seq=1)
            await channel.send(OutboundMessage(channel="websocket", chat_id=CHAT,
                                               content="Luz Paula is on."))
            frames = await _frames(conn, 5)
    finally:
        await channel.stop()
        await server

    by = {}
    for f in frames:
        by.setdefault(f["event"], []).append(f)
    delta, stream_end = by["delta"][0], by["stream_end"][0]
    assert delta["text"] == "Checking " and delta["stream_id"] == "s1"
    assert stream_end["stream_id"] == "s1" and stream_end["trim_to"] == "Checking the lights."
    start, progress = by["subagent_status"]
    assert (start["subagent_event"], start["task_id"], start["label"]) == ("start", "t1", "lights")
    assert progress["kind"] == "tool" and progress["text"] == "HassTurnOn"
    assert by["message"][0]["text"] == "Luz Paula is on."
    assert all(f["chat_id"] == CHAT for f in frames)

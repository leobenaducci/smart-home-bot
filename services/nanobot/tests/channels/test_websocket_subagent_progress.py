"""Relaying a subagent's commentary out of the WebSocket channel.

`progress` rides the same `_subagent_event` envelope as start/done, and like
them it is posted to HomeCore whether or not anyone is attached — these tasks
run for hours with the phone locked, which is exactly when the trace matters.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.channels.websocket import WebSocketChannel


def _channel(homeweb_posts: list) -> WebSocketChannel:
    ch = WebSocketChannel({"enabled": True, "host": "127.0.0.1", "port": 29901,
                           "path": "/ws", "websocketRequiresToken": False},
                          MagicMock())
    ch._homeweb = MagicMock()
    ch._homeweb.owns = lambda cid: isinstance(cid, str) and cid.startswith("homeweb:")
    ch._homeweb.post = homeweb_posts.append
    return ch


def _progress(chat_id="homeweb:Alex:2026-08-03:dev", **meta: Any) -> OutboundMessage:
    base = {"_subagent_event": "progress", "task_id": "abc123", "label": "Investigar",
            "kind": "tool", "text": "web_search", "detail": '{"q": "x"}', "seq": 4}
    base.update(meta)
    return OutboundMessage(channel="websocket", chat_id=chat_id, content="", metadata=base)


@pytest.mark.asyncio
async def test_progress_reaches_homeweb_with_nobody_attached():
    posts: list = []
    ch = _channel(posts)
    await ch.send(_progress())

    assert len(posts) == 1
    assert posts[0] == {
        "type": "task", "chat_id": "homeweb:Alex:2026-08-03:dev", "event": "progress",
        "task_id": "abc123", "label": "Investigar", "status": "",
        "kind": "tool", "text": "web_search", "detail": '{"q": "x"}', "seq": 4,
    }


@pytest.mark.asyncio
async def test_progress_also_goes_to_attached_subscribers():
    posts: list = []
    ch = _channel(posts)
    conn = MagicMock()
    conn.send = AsyncMock()
    ch._subs["homeweb:Alex:2026-08-03:dev"] = {conn}

    await ch.send(_progress())

    conn.send.assert_awaited_once()
    payload = json.loads(conn.send.await_args.args[0])
    assert payload["event"] == "subagent_status"
    assert payload["subagent_event"] == "progress"
    assert payload["kind"] == "tool"
    assert payload["text"] == "web_search"
    assert payload["seq"] == 4


@pytest.mark.asyncio
async def test_start_and_done_are_unchanged_by_the_new_field():
    """The extra keys belong to `progress` alone — a lifecycle event must keep
    the exact shape HomeCore's /chat/agent-event already accepts."""
    posts: list = []
    ch = _channel(posts)
    await ch.send(OutboundMessage(
        channel="websocket", chat_id="homeweb:Alex:2026-08-03", content="",
        metadata={"_subagent_event": "start", "task_id": "abc123", "label": "Investigar"}))
    await ch.send(OutboundMessage(
        channel="websocket", chat_id="homeweb:Alex:2026-08-03", content="",
        metadata={"_subagent_event": "done", "task_id": "abc123", "status": "ok"}))

    assert [p["event"] for p in posts] == ["start", "done"]
    for p in posts:
        assert set(p) == {"type", "chat_id", "event", "task_id", "label", "status"}


@pytest.mark.asyncio
async def test_a_non_homeweb_chat_is_left_alone():
    posts: list = []
    ch = _channel(posts)
    await ch.send(_progress(chat_id="telegram:123"))
    assert posts == []

"""A HomeCore message is kept by HomeCore, not by whoever happens to be attached.

15 Aug 2026, 22:23. A background turn finished marking two prize redemptions as
delivered and answered "Hecho, señor Alex". The relay was only consulted when
``conns`` was empty, a connection was still registered for that chat, and the
frame went out over it — to an app that was already gone. The message was never
written to the day's history, so the conversation ended on "Procesando en
segundo plano, te aviso cuando esté listo" with the work four minutes done, and
nothing in the log said otherwise.

A subscriber is not a reader. HomeCore owns the history and the push, and it
dedupes against what the page already stored, so the fix is to always hand it
the message and treat the socket as the fast path it is.

Trace — tool hints and progress lines — is exempt on purpose: it is not
conversation, and it is not worth a row in the history or a lit screen.
"""

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger as _loguru

from nanobot.bus.events import OutboundMessage
from nanobot.channels.websocket import WebSocketChannel

CHAT = "homeweb:user1:2026-08-15:1786840580988"
ANSWER = "Hecho, señor Alex — los dos canjes de Kai quedaron entregados."


@contextmanager
def _captured_warnings():
    lines: list[str] = []
    sink = _loguru.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        yield lines
    finally:
        _loguru.remove(sink)


def _channel(posts: list) -> WebSocketChannel:
    ch = WebSocketChannel({"enabled": True, "host": "127.0.0.1", "port": 29902,
                           "path": "/ws", "websocketRequiresToken": False},
                          MagicMock())
    ch._homeweb = MagicMock()
    ch._homeweb.owns = lambda cid: isinstance(cid, str) and cid.startswith("homeweb:")
    ch._homeweb.post = posts.append
    return ch


def _attach(ch: WebSocketChannel, chat_id: str = CHAT):
    conn = MagicMock()
    conn.send = AsyncMock()
    ch._subs[chat_id] = {conn}
    return conn


def _message(chat_id: str = CHAT, content: str = ANSWER, **meta) -> OutboundMessage:
    return OutboundMessage(channel="websocket", chat_id=chat_id,
                           content=content, metadata=meta)


@pytest.mark.asyncio
async def test_a_message_is_relayed_even_with_someone_attached():
    """The reported failure: delivered to a socket, kept nowhere."""
    posts: list = []
    ch = _channel(posts)
    conn = _attach(ch)

    await ch.send(_message())

    assert posts == [{"type": "message", "chat_id": CHAT, "text": ANSWER}], (
        "the answer was pushed and never stored — this is the bug")
    conn.send.assert_awaited_once(), "and it must still arrive live"
    assert json.loads(conn.send.await_args.args[0])["text"] == ANSWER


@pytest.mark.asyncio
async def test_a_message_with_nobody_attached_is_still_relayed():
    posts: list = []
    ch = _channel(posts)

    with _captured_warnings() as log:
        await ch.send(_message())

    assert posts and posts[0]["text"] == ANSWER
    assert "no active subscribers" not in "".join(log), (
        "a message that was handed to HomeCore did not go undelivered")


@pytest.mark.asyncio
async def test_a_fired_reminder_keeps_its_buttons():
    posts: list = []
    ch = _channel(posts)
    _attach(ch)

    await ch.send(_message(content="La pizza está lista",
                           reminder_job="job-7", reminder_name="pizza",
                           reminder_recurring=False))

    assert posts[0]["reminder"] == {"job": "job-7", "name": "pizza", "recurring": False}


@pytest.mark.parametrize("trace", ["_tool_hint", "_progress"])
@pytest.mark.asyncio
async def test_trace_is_never_kept(trace):
    """Breadcrumbs belong to the moment they happen in."""
    posts: list = []
    ch = _channel(posts)
    conn = _attach(ch)

    await ch.send(_message(content="exec: ls", **{trace: True}))

    assert posts == [], "a tool hint is not conversation"
    conn.send.assert_awaited_once(), "but a watcher still sees it"


@pytest.mark.asyncio
async def test_a_chat_that_is_not_homewebs_is_unchanged():
    posts: list = []
    ch = _channel(posts)

    with _captured_warnings() as log:
        await ch.send(_message(chat_id="cli-session-1"))

    assert posts == []
    assert "no active subscribers" in "".join(log), (
        "nothing else has a store of record — undeliverable has to be said")


@pytest.mark.asyncio
async def test_an_empty_message_is_not_relayed():
    posts: list = []
    ch = _channel(posts)

    await ch.send(_message(content="   "))

    assert posts == []

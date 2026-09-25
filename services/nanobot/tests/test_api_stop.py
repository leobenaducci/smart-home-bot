"""Stopping a turn that is already running (`POST /v1/stop`).

Closing the HTTP response cancels a run too, but only when this process next
tries to write to it — and the turns worth stopping are exactly the ones that
are not writing anything: two minutes inside a tool call, a model that has not
produced a first token. HomeCore's «Detener» has to be immediate or it is not a
stop, so it says so out of band and this is what answers.

What the tests below pin down:

- the run is actually cancelled, not merely detached;
- whatever it had already streamed still reaches the caller, because the
  partial answer is kept and shown as interrupted;
- the key is the same one the completion registered under, since a stop aimed
  at a session nobody is running in is silence with a 200 on it;
- and the registry does not keep tasks that have ended.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nanobot.api.server import create_app

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:  # pragma: no cover - mirrors test_api_stream.py
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

CHAT_ID = "homeweb:user1:2026-08-07:1754500000000"
SESSION_KEY = f"websocket:{CHAT_ID}"


def _make_blocking_agent(started: asyncio.Event, cancelled: asyncio.Event) -> MagicMock:
    """An agent that says one word and then goes quiet for a long time.

    The shape that matters: it is not writing to the response when the stop
    arrives, which is precisely the case a closed connection cannot catch.
    """
    agent = MagicMock()
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    async def fake_process_direct(*, on_stream=None, on_stream_end=None, **_kw):
        if on_stream:
            await on_stream("Voy a re")
        started.set()
        try:
            await asyncio.sleep(30)          # a long tool call
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never finished"             # pragma: no cover - the point is it doesn't

    agent.process_direct = fake_process_direct
    return agent


@pytest_asyncio.fixture
async def aiohttp_client():
    clients: list[TestClient] = []

    async def _make_client(app):
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    try:
        yield _make_client
    finally:
        for client in clients:
            await client.close()


def _completion_body(**extra):
    return {
        "messages": [{"role": "user", "content": "resume el mes"}],
        "stream": True,
        "channel": "websocket",
        "chat_id": CHAT_ID,
        # Otherwise the escalation timer, not the stop, is what ends this.
        "no_escalate": True,
        **extra,
    }


def _streamed_text(body: str) -> str:
    """The text a client ends up with, reassembled from the SSE chunks."""
    out = []
    for line in body.split("\n"):
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        for choice in chunk.get("choices", []):
            out.append(choice.get("delta", {}).get("content") or "")
    return "".join(out)


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stop_cancels_the_running_turn(aiohttp_client) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()
    app = create_app(_make_blocking_agent(started, cancelled), model_name="m")
    client = await aiohttp_client(app)

    async def _ask():
        resp = await client.post("/v1/chat/completions", json=_completion_body())
        return await resp.text()

    asking = asyncio.create_task(_ask())
    await asyncio.wait_for(started.wait(), timeout=5)

    stop = await client.post("/v1/stop", json={"channel": "websocket", "chat_id": CHAT_ID})
    assert stop.status == 200
    assert (await stop.json())["stopped"] == 1

    body = await asyncio.wait_for(asking, timeout=5)
    assert cancelled.is_set(), "the run was detached, not cancelled"
    # The half-sentence it managed to say still arrives: HomeCore keeps it and
    # marks it interrupted, which is only possible if it was delivered.
    # Read across the chunks rather than searching the raw body: the response
    # is paced, so where the text is cut is up to the pacer — "Voy a " then
    # "re" is the same delivery as "Voy a re" and only one of them is a bug.
    assert "Voy a re" in _streamed_text(body)


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stop_only_reaches_the_session_it_names(aiohttp_client) -> None:
    """A stop is aimed at one conversation. Somebody else's turn — another
    Profesión, another member — keeps running."""
    started, cancelled = asyncio.Event(), asyncio.Event()
    app = create_app(_make_blocking_agent(started, cancelled), model_name="m")
    client = await aiohttp_client(app)

    asking = asyncio.create_task(
        client.post("/v1/chat/completions", json=_completion_body()))
    await asyncio.wait_for(started.wait(), timeout=5)

    other = await client.post("/v1/stop", json={"channel": "websocket",
                                                "chat_id": "homeweb:user2:2026-08-07:1"})
    assert (await other.json())["stopped"] == 0
    assert not cancelled.is_set()

    await client.post("/v1/stop", json={"channel": "websocket", "chat_id": CHAT_ID})
    resp = await asyncio.wait_for(asking, timeout=5)
    await resp.text()
    assert cancelled.is_set()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stopping_nothing_is_not_an_error(aiohttp_client) -> None:
    """Pressing «Detener» a moment after the answer landed is ordinary, not a
    failure — the page cannot know the turn ended between paint and tap."""
    agent = MagicMock()
    agent._connect_mcp = AsyncMock()
    agent.process_direct = AsyncMock(return_value="listo")
    app = create_app(agent, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post("/v1/stop", json={"channel": "websocket", "chat_id": CHAT_ID})
    assert resp.status == 200
    assert (await resp.json()) == {"stopped": 0, "session_key": SESSION_KEY}


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_a_finished_turn_leaves_nothing_behind(aiohttp_client) -> None:
    """The registry is not a log. A session that keeps its entry forever is a
    slow leak in a process that runs for weeks."""
    agent = MagicMock()
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    async def fake(*, on_stream=None, on_stream_end=None, **_kw):
        if on_stream:
            await on_stream("listo")
        if on_stream_end:
            await on_stream_end()
        return "listo"

    agent.process_direct = fake
    app = create_app(agent, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post("/v1/chat/completions", json=_completion_body())
    await resp.text()
    assert app["active_runs"] == {}


# --- what an escalated turn knows -------------------------------------------
# A subagent starts with a fresh context and cannot see the conversation. The
# `spawn` *tool* tells the model to pass `context` and write a self-contained
# task; an escalation has no model in the loop to do that — it takes the user's
# message verbatim, at the moment the turn timed out. So «¿cuál es el precio
# por imagen?» reached a subagent that had never heard of an image, and it
# answered, correctly and uselessly, that it had no idea what we were looking
# at.

from nanobot.api.server import _escalation_context  # noqa: E402


class FakeSessions:
    def __init__(self, messages):
        self._s = type("S", (), {"messages": messages})()

    def get_or_create(self, key):
        return self._s


def _loop_with(messages):
    loop = MagicMock()
    loop.sessions = FakeSessions(messages)
    return loop


def test_an_escalated_task_carries_what_was_being_talked_about() -> None:
    ctx = _escalation_context(_loop_with([
        {"role": "user", "content": "muéstrame los modelos de imagen de Together"},
        {"role": "assistant", "content": "Qwen-Image-2.0 y FLUX.1"},
        {"role": "user", "content": "¿cuál es el precio por imagen?"},
    ]), "websocket:x")
    assert ctx and "Qwen-Image-2.0" in ctx
    assert "muéstrame los modelos" in ctx
    # Framed as history, so it is not read as a fresh instruction.
    assert "not new instructions" in ctx or "history" in ctx


def test_the_oldest_turns_are_what_gets_dropped() -> None:
    """A question hangs off the end of a conversation, not the start of it."""
    msgs = [{"role": "user", "content": f"mensaje {i}"} for i in range(40)]
    ctx = _escalation_context(_loop_with(msgs), "websocket:x")
    assert "mensaje 39" in ctx and "mensaje 0" not in ctx


def test_tool_calls_and_images_are_not_quoted() -> None:
    """What makes the question answerable is what was said."""
    ctx = _escalation_context(_loop_with([
        {"role": "user", "content": "hola"},
        {"role": "tool", "content": "{'rows': 400}"},
        {"role": "assistant", "content": None},
        {"role": "assistant", "content": "buenas"},
    ]), "websocket:x")
    assert "rows" not in ctx
    assert "hola" in ctx and "buenas" in ctx


def test_an_empty_session_carries_nothing_rather_than_an_empty_heading() -> None:
    assert _escalation_context(_loop_with([]), "websocket:x") is None


def test_a_session_that_cannot_be_read_is_not_fatal() -> None:
    """The escalation is the fallback path already; it must not be the thing
    that raises."""
    loop = MagicMock()
    loop.sessions.get_or_create.side_effect = RuntimeError("no session store")
    assert _escalation_context(loop, "websocket:x") is None

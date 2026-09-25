"""Tests for SSE streaming support in /v1/chat/completions."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nanobot.api.server import (
    _sse_chunk,
    _SSE_DONE,
    create_app,
)

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)


# ---------------------------------------------------------------------------
# Unit tests for SSE helpers
# ---------------------------------------------------------------------------


def test_sse_chunk_with_delta() -> None:
    raw = _sse_chunk("hello", "test-model", "chatcmpl-abc123")
    line = raw.decode()
    assert line.startswith("data: ")
    payload = json.loads(line[len("data: "):])
    assert payload["id"] == "chatcmpl-abc123"
    assert payload["object"] == "chat.completion.chunk"
    assert payload["model"] == "test-model"
    assert payload["choices"][0]["delta"]["content"] == "hello"
    assert payload["choices"][0]["finish_reason"] is None


def test_sse_chunk_finish_reason() -> None:
    raw = _sse_chunk("", "m", "id1", finish_reason="stop")
    payload = json.loads(raw.decode().split("data: ", 1)[1])
    assert payload["choices"][0]["delta"] == {}
    assert payload["choices"][0]["finish_reason"] == "stop"


def test_sse_done_format() -> None:
    assert _SSE_DONE == b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Integration tests with aiohttp TestClient
# ---------------------------------------------------------------------------


def _make_streaming_agent(tokens: list[str]) -> MagicMock:
    """Create a mock agent that streams tokens via on_stream callback."""
    agent = MagicMock()
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    async def fake_process_direct(*, content="", media=None, session_key="",
                                  channel="", chat_id="", on_stream=None,
                                  on_stream_end=None, **kwargs):
        if on_stream:
            for token in tokens:
                await on_stream(token)
        if on_stream_end:
            await on_stream_end()
        return " ".join(tokens)

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


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_true_returns_sse(aiohttp_client) -> None:
    """stream=true should return text/event-stream with SSE chunks."""
    agent = _make_streaming_agent(["Hello", " world"])
    app = create_app(agent, model_name="test-model")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status == 200
    assert resp.content_type == "text/event-stream"

    body = await resp.text()
    lines = [l for l in body.split("\n") if l.startswith("data: ")]

    data_lines = [l[len("data: "):] for l in lines]
    assert data_lines[-1] == "[DONE]"

    chunks = [json.loads(l) for l in data_lines[:-1]]
    # What the model said, not how it was cut up. The response is paced — text
    # is buffered and released at a steady rate so an answer reads the same
    # whether the provider delivered it in two bursts or forty — so chunk
    # boundaries are the pacer's business and no client should depend on them.
    # The concatenation is the contract.
    streamed = "".join(
        c["choices"][0]["delta"].get("content", "") for c in chunks
    )
    assert streamed == "Hello world"
    # Last chunk before [DONE] should have finish_reason=stop
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["choices"][0]["delta"] == {}


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_false_returns_json(aiohttp_client) -> None:
    """stream=false should still return regular JSON response."""
    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="normal reply")
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    app = create_app(agent, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "normal reply"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_default_is_false(aiohttp_client) -> None:
    """Omitting stream should behave like stream=false."""
    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="default reply")
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    app = create_app(agent, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["object"] == "chat.completion"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_sse_chunk_ids_are_consistent(aiohttp_client) -> None:
    """All SSE chunks in a single stream should share the same id."""
    agent = _make_streaming_agent(["A", "B", "C"])
    app = create_app(agent, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "go"}], "stream": True},
    )
    body = await resp.text()
    data_lines = [l[len("data: "):] for l in body.split("\n") if l.startswith("data: ") and l != "data: [DONE]"]
    chunks = [json.loads(l) for l in data_lines]

    chunk_ids = {c["id"] for c in chunks}
    assert len(chunk_ids) == 1, f"Expected single chunk id, got {chunk_ids}"
    assert chunk_ids.pop().startswith("chatcmpl-")


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_passes_on_stream_callbacks(aiohttp_client) -> None:
    """process_direct should be called with on_stream and on_stream_end when streaming."""
    captured_kwargs: dict = {}

    async def fake_process_direct(**kwargs):
        captured_kwargs.update(kwargs)
        if kwargs.get("on_stream_end"):
            await kwargs["on_stream_end"]()
        return "done"

    agent = MagicMock()
    agent.process_direct = fake_process_direct
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    app = create_app(agent, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status == 200
    assert captured_kwargs.get("on_stream") is not None
    assert captured_kwargs.get("on_stream_end") is not None


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_stream_with_session_id(aiohttp_client) -> None:
    """Streaming should respect session_id for session key routing."""
    captured_key: str = ""

    async def fake_process_direct(*, session_key="", on_stream=None, on_stream_end=None, **kwargs):
        nonlocal captured_key
        captured_key = session_key
        if on_stream:
            await on_stream("ok")
        if on_stream_end:
            await on_stream_end()
        return "ok"

    agent = MagicMock()
    agent.process_direct = fake_process_direct
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    app = create_app(agent, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "session_id": "my-session",
        },
    )
    assert resp.status == 200
    assert captured_key == "api:my-session"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_streaming_backend_failure_does_not_emit_success_terminator(aiohttp_client) -> None:
    """Backend exceptions should not surface as a normal stop+[DONE] stream."""
    agent = MagicMock()

    async def boom(**kwargs):
        raise RuntimeError("backend blew up")

    agent.process_direct = boom
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    app = create_app(agent, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )

    assert resp.status == 200
    body = await resp.text()
    assert '"finish_reason": "stop"' not in body
    assert "[DONE]" not in body


# ---------------------------------------------------------------------------
# Liveness and pacing
#
# Both exist because of the same afternoon in the logs: a thinking model that
# streams nothing but reasoning for a minute was being declared dead and
# pushed to a sub-agent mid-conversation, and the text that did arrive came in
# whatever bursts the network happened to deliver.
# ---------------------------------------------------------------------------


def _make_thinking_agent(quiet_s: float, answer: str = "listo") -> MagicMock:
    """An agent that thinks out loud for *quiet_s* and shows nothing until the end.

    Exactly the turn that used to be mistaken for a hang: the only thing on the
    wire is reasoning, which the reader never sees.
    """
    agent = MagicMock()
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    async def fake_process_direct(*, on_stream=None, on_stream_end=None,
                                  on_progress=None, **_kw):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + quiet_s
        while loop.time() < deadline:
            if on_progress:
                await on_progress("", alive=True)
            await asyncio.sleep(0.02)
        if on_stream:
            await on_stream(answer)
        if on_stream_end:
            await on_stream_end()
        return answer

    agent.process_direct = fake_process_direct
    return agent


def _streamed_text(body: str) -> str:
    out = []
    for line in body.split("\n"):
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        for choice in json.loads(line[len("data: "):]).get("choices", []):
            out.append(choice.get("delta", {}).get("content") or "")
    return "".join(out)


def _content_chunks(body: str) -> list[str]:
    return [c for c in (
        json.loads(l[len("data: "):])["choices"][0].get("delta", {}).get("content") or ""
        for l in body.split("\n")
        if l.startswith("data: ") and l != "data: [DONE]"
    ) if c]


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_thinking_alone_keeps_a_turn_from_being_escalated(
    aiohttp_client, monkeypatch
) -> None:
    """A model that is only thinking is working, not hung.

    The turn stays silent for twice the escalation budget and still answers in
    the chat it was asked in — no sub-agent, no "te aviso cuando esté listo".
    """
    monkeypatch.setattr("nanobot.api.server._FIRST_TOKEN_BUDGET_S", 0.25)
    agent = _make_thinking_agent(quiet_s=0.5, answer="son las 20:41")
    agent.subagents = MagicMock()
    agent.subagents.spawn = AsyncMock()

    app = create_app(agent, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "que hora es"}],
        "stream": True, "channel": "websocket", "chat_id": "c1",
    })
    body = await resp.text()

    assert _streamed_text(body) == "son las 20:41"
    agent.subagents.spawn.assert_not_awaited()
    assert "segundo plano" not in body


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_a_turn_with_no_sign_of_life_still_escalates(
    aiohttp_client, monkeypatch
) -> None:
    """The counterpart: silence with nothing behind it is still a hang."""
    monkeypatch.setattr("nanobot.api.server._FIRST_TOKEN_BUDGET_S", 0.25)
    agent = MagicMock()
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    async def fake_process_direct(**_kw):
        await asyncio.sleep(30)

    agent.process_direct = fake_process_direct
    agent.subagents = MagicMock()
    agent.subagents.spawn = AsyncMock()

    app = create_app(agent, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "que hora es"}],
        "stream": True, "channel": "websocket", "chat_id": "c1",
    })
    body = await resp.text()

    agent.subagents.spawn.assert_awaited()
    assert "in the background" in _streamed_text(body)


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_a_burst_is_released_at_a_steady_pace(aiohttp_client) -> None:
    """One 600-character burst reaches the reader as many small chunks.

    Written straight through it would land as a single paragraph appearing at
    once, which is not what the rest of the conversation looks like.
    """
    answer = "x" * 600
    app = create_app(_make_streaming_agent([answer]), model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True,
    })
    body = await resp.text()

    chunks = _content_chunks(body)
    assert "".join(chunks) == answer
    assert len(chunks) > 5, f"burst was not paced, went out in {len(chunks)} chunk(s)"
    # Evenly, not in one lump with a dribble after it.
    assert max(len(c) for c in chunks) <= len(answer) // 2


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_a_trickle_arrives_whole(aiohttp_client) -> None:
    """Sixty small tokens over half a second, none of them lost.

    The reader now wakes on the pacer as well as on the queue, so it goes
    round its loop tens of times per answer instead of once per token. Every
    token still has to come out the other side, in order.
    """
    tokens = [f"[{i:03d}]" for i in range(60)]
    agent = MagicMock()
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    async def fake_process_direct(*, on_stream=None, on_stream_end=None, **_kw):
        for token in tokens:
            await on_stream(token)
            await asyncio.sleep(0.01)
        if on_stream_end:
            await on_stream_end()
        return "".join(tokens)

    agent.process_direct = fake_process_direct

    app = create_app(agent, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True,
    })
    body = await resp.text()

    assert _streamed_text(body) == "".join(tokens)


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_a_long_answer_does_not_arrive_faster_than_a_short_one(aiohttp_client) -> None:
    """The invariant the first pacer violated, and the reason it was rewritten.

    That version had one rule — never add more than 1.5 s of latency — expressed
    as `rate = max(base, backlog / 1.5)`. It has no ceiling, so every answer
    drained inside a second and a half however long it was: 600 characters left
    at 67 words a second and 1500 at 167. Reading is about four. The answers
    that most need pacing were the ones guaranteed not to get it.

    So: rate is bounded, and a longer answer therefore takes longer. That is the
    whole feature, and it is one assertion.
    """
    import time as _time

    async def elapsed_for(n_chars: int) -> float:
        agent = _make_streaming_agent([" ".join(["palabra"] * (n_chars // 8))])
        client = await aiohttp_client(create_app(agent, model_name="m"))
        started = _time.monotonic()
        resp = await client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hi"}], "stream": True,
        })
        await resp.text()
        return _time.monotonic() - started

    short = await elapsed_for(200)
    long_ = await elapsed_for(1600)

    assert long_ > short * 2, (
        f"1600 chars took {long_:.2f}s and 200 took {short:.2f}s — the long one "
        "is being dumped rather than paced"
    )


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_words_arrive_whole(aiohttp_client) -> None:
    """No chunk splits a word, so it reads like talking rather than typing."""
    answer = " ".join(f"palabra{i:03d}" for i in range(60))
    app = create_app(_make_streaming_agent([answer]), model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True,
    })
    chunks = _content_chunks(await resp.text())

    assert "".join(chunks) == answer
    # Every chunk but the last ends on a space; none starts mid-word.
    for c in chunks[:-1]:
        assert c.endswith(" "), f"chunk split a word: {c!r}"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_text_with_no_spaces_is_still_paced(aiohttp_client) -> None:
    """A URL, a base64 blob or a table row has no word boundary to wait for.

    Preferring one without a fallback would hold the whole thing until the end
    and then paint it — the exact behaviour being removed.
    """
    answer = "x" * 900
    app = create_app(_make_streaming_agent([answer]), model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True,
    })
    chunks = _content_chunks(await resp.text())

    assert "".join(chunks) == answer
    assert len(chunks) > 5, f"went out in {len(chunks)} chunk(s)"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_a_model_slower_than_the_pacer_loses_nothing(aiohttp_client) -> None:
    """Every token survives a model that answers slower than the pacer ticks.

    This is the case the other pacing tests do not reach. They feed tokens
    faster than `_PACE_TICK_S`, so the buffer always has something in it, the
    reader drains it and blocks on the queue — and the timeout path is never
    taken. A model slower than the tick takes it on every single token.

    The bug that made this necessary: the reader awaited
    `wait_for(shield(queue.get()), ...)`. `wait_for` cancels what it is handed,
    and it is handed the *shield* — so the `queue.get()` behind it survived,
    still waiting in the queue's line, while a fresh one was created next time
    round. The pile of live getters nobody awaited each ate a token. Measured
    on a real answer: 1411 characters reached the chat as 656, in alternating
    strips cut mid-word.
    """
    tokens = [f"[{i:02d}]" for i in range(12)]
    agent = MagicMock()
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    async def fake_process_direct(*, on_stream=None, on_stream_end=None, **_kw):
        for token in tokens:
            await on_stream(token)
            # Slower than _PACE_TICK_S (0.05), so the reader times out waiting
            # for each one and goes round the loop again.
            await asyncio.sleep(0.08)
        if on_stream_end:
            await on_stream_end()
        return "".join(tokens)

    agent.process_direct = fake_process_direct

    app = create_app(agent, model_name="m")
    client = await aiohttp_client(app)

    resp = await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True,
    })
    body = await resp.text()

    assert _streamed_text(body) == "".join(tokens), (
        "tokens were swallowed by getters the reader abandoned"
    )


# ---------------------------------------------------------------------------
# The end of a turn: its performance line, and when the stream is released
# ---------------------------------------------------------------------------


def _steps(body: str) -> list[dict]:
    out = []
    for line in body.split("\n"):
        if line.startswith("data: ") and line != "data: [DONE]":
            chunk = json.loads(line[len("data: "):])
            if "step" in chunk:
                out.append(chunk["step"])
    return out


def _agent(fake) -> MagicMock:
    agent = MagicMock()
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()
    agent.process_direct = fake
    return agent


_USAGE = {"prompt_tokens": 1200, "completion_tokens": 40,
          "model": "ornith-1.5:9b", "effort": "low"}


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_a_plain_answer_carries_its_performance_line(aiohttp_client) -> None:
    """The runner closes the stream, *then* reports the iteration's usage.

    Terminating on the stream's end lost that report on every turn with no
    tool call -- which is most of ordinary chat -- so "Ver rendimiento"
    showed nothing for exactly the turns people look at it for.
    """
    async def fake(*, on_stream=None, on_stream_end=None, on_progress=None, **_kw):
        await on_stream("Hola")
        await on_stream_end()
        await on_progress("", tool_hint=False, usage=dict(_USAGE))
        return "Hola"

    client = await aiohttp_client(create_app(_agent(fake), model_name="m"))
    body = await (await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True})).text()

    usage = [s for s in _steps(body) if s["type"] == "usage"]
    assert len(usage) == 1, body
    assert (usage[0]["in"], usage[0]["out"]) == (1200, 40)
    assert usage[0]["model"] == "ornith-1.5:9b" and usage[0]["effort"] == "low"
    assert body.rstrip().endswith("data: [DONE]")


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_the_answer_is_not_held_for_work_after_it(aiohttp_client) -> None:
    """Memory consolidation runs after the reply; the reader must not wait for it."""
    async def fake(*, on_stream=None, on_stream_end=None, on_progress=None, **_kw):
        await on_stream("Hola")
        await on_stream_end()
        await on_progress("", tool_hint=False, usage=dict(_USAGE))
        await asyncio.sleep(3)              # post-turn work
        return "Hola"

    client = await aiohttp_client(create_app(_agent(fake), model_name="m"))
    started = asyncio.get_running_loop().time()
    body = await (await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True})).text()

    assert asyncio.get_running_loop().time() - started < 2
    assert any(s["type"] == "usage" for s in _steps(body))


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_a_provider_that_reports_no_usage_cannot_hold_the_end(
        aiohttp_client, monkeypatch) -> None:
    import nanobot.api.server as server
    monkeypatch.setattr(server, "_USAGE_GRACE_S", 0.2)

    async def fake(*, on_stream=None, on_stream_end=None, **_kw):
        await on_stream("Hola")
        await on_stream_end()
        await asyncio.sleep(3)
        return "Hola"

    client = await aiohttp_client(create_app(_agent(fake), model_name="m"))
    started = asyncio.get_running_loop().time()
    body = await (await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True})).text()

    assert asyncio.get_running_loop().time() - started < 2
    assert _streamed_text(body) == "Hola"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_a_tool_turn_reports_its_last_model_call(aiohttp_client) -> None:
    """The line describes the call that wrote the answer, not the first one."""
    async def fake(*, on_stream=None, on_stream_end=None, on_progress=None, **_kw):
        await on_progress("", tool_hint=True, tool_events=[{"name": "exec", "arguments": {}}])
        await on_progress("", tool_hint=False, usage={"prompt_tokens": 100, "completion_tokens": 5})
        await on_stream("Listo")
        await on_stream_end()
        await on_progress("", tool_hint=False, usage={"prompt_tokens": 180, "completion_tokens": 12})
        return "Listo"

    client = await aiohttp_client(create_app(_agent(fake), model_name="m"))
    body = await (await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True})).text()

    usage = [s for s in _steps(body) if s["type"] == "usage"]
    assert [(u["in"], u["out"]) for u in usage] == [(180, 12)], usage


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_the_stream_is_never_compressed(aiohttp_client) -> None:
    """zlib holds small writes until the stream closes, so a deflate client
    got the whole turn -- steps, text, [DONE] -- in one burst at the end."""
    agent = _make_streaming_agent(["Hola"])
    client = await aiohttp_client(create_app(agent, model_name="m"))
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers={"Accept-Encoding": "gzip, deflate"},
    )
    assert "Content-Encoding" not in resp.headers
    assert (await resp.text()).rstrip().endswith("data: [DONE]")


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_the_performance_line_carries_latency(aiohttp_client) -> None:
    """First word, whole turn and model calls, measured from the request."""
    async def fake(*, on_stream=None, on_stream_end=None, on_progress=None, **_kw):
        await asyncio.sleep(0.3)                          # thinking before a word
        await on_progress("", tool_hint=True, tool_events=[{"name": "exec", "arguments": {}}])
        await on_progress("", tool_hint=False, usage={"prompt_tokens": 100, "completion_tokens": 5})
        await on_stream("Listo")
        await asyncio.sleep(0.2)
        await on_stream_end()
        await on_progress("", tool_hint=False, usage={"prompt_tokens": 180, "completion_tokens": 12})
        return "Listo"

    client = await aiohttp_client(create_app(_agent(fake), model_name="m"))
    body = await (await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True})).text()

    (usage,) = [s for s in _steps(body) if s["type"] == "usage"]
    assert 250 <= usage["first_ms"] < 450, usage
    assert usage["total_ms"] >= usage["first_ms"] + 150, usage
    assert usage["calls"] == 2, usage


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_a_turn_with_no_usage_sends_no_step(aiohttp_client, monkeypatch) -> None:
    """No usage, no step: a plain stream stays `choices` chunks only, which is
    what a strict OpenAI-style client expects of every chunk."""
    import nanobot.api.server as server
    monkeypatch.setattr(server, "_USAGE_GRACE_S", 0.1)

    async def fake(*, on_stream=None, on_stream_end=None, **_kw):
        await on_stream("Hola")
        await on_stream_end()
        return "Hola"

    client = await aiohttp_client(create_app(_agent(fake), model_name="m"))
    body = await (await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "stream": True})).text()

    assert _steps(body) == []
    assert _streamed_text(body) == "Hola"

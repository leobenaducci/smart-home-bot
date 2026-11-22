"""household D2: standing context, Recent History, contextBlockLimit (through process_direct)."""

from __future__ import annotations

import pytest
from agent.conftest import make_provider

from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config
from nanobot.household import standing_context
from nanobot.household.context_extras import STANDING_KEY, limit_history
from nanobot.household.loop import HouseholdAgentLoop
from nanobot.household.runtime import RUNTIME
from nanobot.providers.base import LLMResponse

KEY = "websocket:homeweb:999000111:space-garden:2026-11-17"


def _loop(tmp_path):
    provider = make_provider(default_model="cheap")
    provider.provider_name = "custom"
    sent: list[list[dict]] = []

    async def chat(**kw):
        sent.append(kw["messages"])
        return LLMResponse(content="ok")

    provider.chat_with_retry = provider.chat_stream_with_retry = provider.chat = chat
    loop = HouseholdAgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                              model="cheap", context_window_tokens=128_000)
    return loop, sent


def _text(message):
    content = message["content"]
    return content if isinstance(content, str) else " ".join(b.get("text", "") for b in content)


PERSONA = "You are the garden helper for Mora. Answer like a gardener."


@pytest.mark.asyncio
async def test_standing_context_reaches_every_turn_but_is_never_stored(tmp_path):
    loop, sent = _loop(tmp_path)
    for word in ("roses?", "tulips?"):
        await loop.process_direct(standing_context.wrap(PERSONA) + "\nWhat about " + word,
                                  session_key=KEY, channel="websocket", chat_id=KEY.split(":", 1)[1])
    for messages in sent:
        assert PERSONA in messages[0]["content"]  # system prompt, every turn
        assert all(PERSONA not in _text(m) for m in messages[1:])
    session = await loop.sessions.state.read(KEY)
    stored = [m for m in session.messages if m["role"] == "user"]
    assert len(stored) == 2 and all(PERSONA not in _text(m) and "[[[" not in _text(m) for m in stored)
    assert "What about tulips?" in _text(stored[-1])
    assert PERSONA in session.metadata[STANDING_KEY]


@pytest.mark.asyncio
async def test_a_turn_without_one_reuses_the_remembered_copy(tmp_path):
    loop, sent = _loop(tmp_path)
    await loop.process_direct(standing_context.wrap(PERSONA) + "\nhello", session_key=KEY,
                              channel="websocket", chat_id=KEY.split(":", 1)[1])
    await loop.process_direct("and now?", session_key=KEY, channel="websocket", chat_id=KEY.split(":", 1)[1])
    assert PERSONA in sent[-1][0]["content"]


@pytest.mark.asyncio
async def test_recent_history_is_this_sessions_own_and_not_persisted(tmp_path):
    loop, sent = _loop(tmp_path)
    memory = loop.context.memory
    memory.append_history("Tomi asked to water the ferns on Fridays.", session_key=KEY)
    memory.append_history("Paula booked the dentist.", session_key="whatsapp:5491100000000")
    await loop.process_direct("remind me what we said", session_key=KEY,
                              channel="websocket", chat_id=KEY.split(":", 1)[1])
    current = _text(sent[-1][-1])
    assert "# Recent History" in current and "ferns" in current and "dentist" not in current
    assert sent[-1][0]["content"].count("ferns") == 0  # not in the system prompt (cache prefix)
    session = await loop.sessions.state.read(KEY)
    assert all("Recent History" not in _text(m) for m in session.messages)


def test_context_block_limit_keeps_the_newest_history():
    history = []
    for i in range(40):
        history += [{"role": "user", "content": f"question {i} " + "x" * 400},
                    {"role": "assistant", "content": f"answer {i} " + "y" * 400}]
    kept = limit_history(history, 1000)
    assert kept and kept[-1] is history[-1] and kept[0]["role"] == "user" and len(kept) < 20
    assert limit_history(history, None) is history


@pytest.mark.asyncio
async def test_context_block_limit_applies_to_the_turn(tmp_path):
    loop, sent = _loop(tmp_path)
    RUNTIME.config = Config.model_validate({"agents": {"defaults": {
        "model": "cheap", "provider": "custom", "contextBlockLimit": 600}}})
    for i in range(8):
        await loop.process_direct(f"message {i} " + "z" * 600, session_key="email:tomi@example.org",
                                  channel="email", chat_id="tomi@example.org")
    assert len(sent[-1]) < len(sent[-2]) + 2 and len(sent[-1]) <= 4


@pytest.mark.asyncio
async def test_every_turn_tells_the_model_the_time_and_who_is_answering(tmp_path):
    import datetime

    loop, sent = _loop(tmp_path)
    await loop.process_direct("what do I have today?", session_key=KEY, channel="websocket",
                              chat_id=KEY.split(":", 1)[1])
    user = _text(sent[-1][-1])
    assert "Current Time: " + datetime.date.today().isoformat() in user
    assert "Model: cheap" in user
    # Upstream keeps each turn's runtime context with that message and replays
    # it unchanged (prompt-cache stability), so an earlier message keeps the
    # time it was sent at; the newest message carries the current one.
    assert user.count("Current Time: ") == 1


def test_the_time_line_is_the_forks():
    import datetime as dt
    from zoneinfo import ZoneInfo

    from nanobot.household.turn_facts import current_time_str

    now = dt.datetime(2026, 11, 22, 10, 5, tzinfo=ZoneInfo("America/Santiago"))
    assert current_time_str("America/Santiago", now) == \
        "2026-11-22 10:05 (Sunday) (America/Santiago, UTC-03:00)"

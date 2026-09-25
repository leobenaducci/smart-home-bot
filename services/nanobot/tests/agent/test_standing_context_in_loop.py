"""The loop's half of the standing-context contract.

The unit tests cover the two functions. This covers the wiring: that the block
reaches the model on the turn it arrives, and that the session never grows a
second copy of it — which is the whole reason the split exists.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from conftest import mock_provider

from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunResult
from nanobot.bus.events import InboundMessage
from nanobot.utils.standing_context import wrap

PERSONA = "[Context: Programmer]\nYou are the programmer Alfred. Read before writing."


def _loop(tmp_path: Path, monkeypatch) -> AgentLoop:
    provider = mock_provider("fast-model")
    loop = AgentLoop(bus=MagicMock(), provider=provider, workspace=tmp_path,
                     model="fast-model")
    monkeypatch.setattr(loop, "_connect_mcp", _anoop)
    # Consolidation reads token budgets off the mocked provider and is not what
    # these tests are about — the duplication they prevent is precisely what
    # used to drive it.
    monkeypatch.setattr(loop.consolidator, "maybe_consolidate_by_tokens", _anoop)
    return loop


def _turn(question: str) -> InboundMessage:
    return InboundMessage(channel="websocket", sender_id="user",
                          chat_id="homeweb:Alex:2026-08-03:dev",
                          content=f"{wrap(PERSONA)}\n\n{question}")


@pytest.mark.asyncio
async def test_model_sees_the_persona_every_turn_history_keeps_one_copy(tmp_path, monkeypatch):
    loop = _loop(tmp_path, monkeypatch)
    prompts: list[str] = []

    async def fake_run(spec):
        prompts.append("\n".join(
            m.get("content") if isinstance(m.get("content"), str) else ""
            for m in spec.initial_messages))
        return AgentRunResult(final_content="listo", messages=[])

    monkeypatch.setattr(loop.runner, "run", fake_run)

    for i in range(4):
        await loop._process_message(_turn(f"pregunta {i}"), session_key="websocket:dev")

    # Seen on every turn, in full — that is what re-sending is for.
    assert len(prompts) == 4
    for p in prompts:
        assert "programmer Alfred" in p
        assert "[[[standing-context]]]" not in p          # markers never reach the model

    # ...and exactly once in the prompt, not once per past turn.
    assert prompts[-1].count("Read before writing") == 1

    stored = loop.sessions.get_or_create("websocket:dev").messages
    user_texts = [m["content"] for m in stored if m.get("role") == "user"]
    assert user_texts == [f"pregunta {i}" for i in range(4)]
    assert not any("programmer Alfred" in t for t in user_texts)


@pytest.mark.asyncio
async def test_a_message_with_no_standing_context_is_stored_verbatim(tmp_path, monkeypatch):
    """Every other caller must be unaffected."""
    loop = _loop(tmp_path, monkeypatch)

    async def fake_run(spec):
        return AgentRunResult(final_content="ok", messages=[])

    monkeypatch.setattr(loop.runner, "run", fake_run)

    await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct",
                       content="¿qué tiempo hace?"),
        session_key="cli:direct")

    stored = loop.sessions.get_or_create("cli:direct").messages
    assert [m["content"] for m in stored if m.get("role") == "user"] == ["¿qué tiempo hace?"]


async def _anoop(*_a, **_kw):
    return None


# --- Turns nobody prepended a block to ---------------------------------------
# A background task started inside a Profesión announces its result as a system
# message. It carries no standing block, and since the block is no longer stored
# it can no longer inherit one from history — so without a remembered copy the
# answer came back phrased by the ordinary Alfred.

@pytest.mark.asyncio
async def test_a_subagent_result_is_phrased_in_the_profession_s_voice(tmp_path, monkeypatch):
    loop = _loop(tmp_path, monkeypatch)
    prompts: list[list[dict]] = []

    async def fake_run(spec):
        prompts.append(spec.initial_messages)
        return AgentRunResult(final_content="listo", messages=[])

    monkeypatch.setattr(loop.runner, "run", fake_run)

    # An ordinary turn in the space teaches the session its voice.
    await loop._process_message(_turn("investiga los arriendos"),
                                session_key="websocket:homeweb:Alex:2026-08-03:dev")

    # Hours later the subagent announces its result — no block of its own.
    await loop._process_message(
        InboundMessage(channel="system", sender_id="subagent",
                       chat_id="websocket:homeweb:Alex:2026-08-03:dev",
                       content="Encontré tres arriendos."),
        session_key="websocket:homeweb:Alex:2026-08-03:dev")

    system_prompt = prompts[-1][0]["content"]
    assert prompts[-1][0]["role"] == "system"
    assert "programmer Alfred" in system_prompt, "the profession's voice was lost"
    assert "[[[standing-context]]]" not in system_prompt, "markers reached the model"


@pytest.mark.asyncio
async def test_an_ordinary_turn_does_not_get_the_block_twice(tmp_path, monkeypatch):
    """It carries its own copy; re-attaching would duplicate it — the exact
    thing this whole mechanism exists to stop."""
    loop = _loop(tmp_path, monkeypatch)
    prompts: list[str] = []

    async def fake_run(spec):
        prompts.append("\n".join(
            m.get("content") if isinstance(m.get("content"), str) else ""
            for m in spec.initial_messages))
        return AgentRunResult(final_content="listo", messages=[])

    monkeypatch.setattr(loop.runner, "run", fake_run)

    for i in range(3):
        await loop._process_message(_turn(f"pregunta {i}"), session_key="websocket:dev")

    for p in prompts:
        assert p.count("Read before writing") == 1, "the persona was attached twice"


@pytest.mark.asyncio
async def test_a_session_that_never_saw_a_block_gets_none(tmp_path, monkeypatch):
    """The normal chat, the CLI, every other channel: unchanged."""
    loop = _loop(tmp_path, monkeypatch)
    prompts: list[str] = []

    async def fake_run(spec):
        prompts.append(spec.initial_messages[0]["content"])
        return AgentRunResult(final_content="ok", messages=[])

    monkeypatch.setattr(loop.runner, "run", fake_run)
    await loop._process_message(
        InboundMessage(channel="system", sender_id="subagent", chat_id="cli:direct",
                       content="terminé"),
        session_key="cli:direct")
    assert "standing" not in prompts[-1].lower()

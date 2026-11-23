"""household: routing, the classifier and escalation at the loop level (B3).

Driven through upstream's AgentLoop.process_direct on a WhatsApp-style
channel, so the routing is not an API-only path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agent.conftest import make_provider

from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config
from nanobot.household.loop import HouseholdAgentLoop
from nanobot.household.runtime import RUNTIME
from nanobot.household import turn_router
from nanobot.providers.base import LLMResponse


def _loop(tmp_path, replies_by_model):
    provider = make_provider(default_model="cheap")
    provider.provider_name = "custom"
    calls: list[str] = []

    async def chat(**kw):
        model = kw.get("model")
        calls.append(model)
        reply = replies_by_model[model]
        return reply() if callable(reply) else reply

    provider.chat_with_retry = chat
    provider.chat_stream_with_retry = chat
    provider.chat = chat
    loop = HouseholdAgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                              model="cheap", context_window_tokens=128_000)
    return loop, calls


def _config(mode="active", classifier=True):
    turn_router._CLASSIFIERS.clear()
    d = {"model": "cheap", "provider": "custom", "modelPowerful": "strong",
         "routing": {"mode": mode, "stickyTurns": 2}}
    if classifier:
        d["classifierModel"] = "tiny"
    return Config.model_validate({"agents": {"defaults": d}})


@pytest.mark.asyncio
async def test_a_complex_label_runs_on_the_powerful_model(tmp_path):
    loop, calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "complex", "reason": "advice"}'),
        "strong": LLMResponse(content="Pagalo al contado."),
    })
    RUNTIME.config = _config()
    out = await loop.process_direct("¿cuotas o contado con este interés?",
                                    session_key="whatsapp-own:56999999999",
                                    channel="whatsapp", chat_id="56999999999")
    assert out.content == "Pagalo al contado."
    assert calls == ["tiny", "strong"]


@pytest.mark.asyncio
async def test_shadow_mode_records_but_runs_cheap(tmp_path):
    loop, calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "complex", "reason": "x"}'),
        "cheap": LLMResponse(content="ok"),
    })
    RUNTIME.config = _config(mode="shadow")
    await loop.process_direct("hola", session_key="email:tomi@example.org",
                              channel="email", chat_id="tomi@example.org")
    assert calls == ["tiny", "cheap"]


@pytest.mark.asyncio
async def test_a_cheap_attempt_that_ends_empty_is_continued_on_the_strong_model(tmp_path):
    loop, calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "action", "reason": "x"}'),
        "cheap": LLMResponse(content=""),
        "strong": LLMResponse(content="Listo, apagué la Luz Paula."),
    })
    RUNTIME.config = _config()
    # Voice: nobody reads "later" aloud, so the strong model continues it here
    # (in a person's own chat it goes to a sub-agent, test_dead_end_* below).
    out = await loop.process_direct("apagá la luz", session_key="voice:cocina",
                                    channel="voice", chat_id="cocina")
    assert out.content == "Listo, apagué la Luz Paula."
    assert calls[0] == "tiny" and "cheap" in calls and calls[-1] == "strong"
    session = loop.sessions.get_existing("voice:cocina")
    assert session.metadata.get(turn_router.STICKY_KEY) == 2


@pytest.mark.asyncio
async def test_off_mode_asks_no_classifier(tmp_path):
    loop, calls = _loop(tmp_path, {"cheap": LLMResponse(content="ok")})
    RUNTIME.config = _config(mode="off")
    await loop.process_direct("hola", session_key="cli:x")
    assert calls == ["cheap"]


def _spawned(monkeypatch):
    from nanobot.household import api_adapter

    calls: list[dict] = []

    async def spawn(self, task, **kw):
        calls.append({"task": task, **kw})
        return "started"
    monkeypatch.setattr(api_adapter._Subagents, "spawn", spawn)
    return calls


@pytest.mark.asyncio
async def test_a_dead_end_in_a_persons_own_chat_goes_to_a_sub_agent_with_what_it_did(tmp_path, monkeypatch):
    """A4d (fork test_a_dead_end_is_handed_off_with_what_it_did)."""
    from nanobot.household import delegate
    from nanobot.providers.base import ToolCallRequest

    spawned = _spawned(monkeypatch)
    rows: list[dict] = []
    monkeypatch.setattr("nanobot.household.usage_report.report_usage",
                        lambda *a, **kw: rows.append({"model": a[1], **kw}))
    loop, calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "action", "reason": "x"}'),
        "cheap": lambda: LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id=f"c{len(calls)}", name="read_file", arguments={"path": "lights.txt"})]),
        "strong": LLMResponse(content="never"),
    })
    cfg = _config()
    cfg.agents.defaults.routing.everyday_iterations = 2
    RUNTIME.config = cfg
    out = await loop.process_direct("prendé Oficina Tomi", session_key="whatsapp-own:56999999999",
                                    channel="whatsapp", chat_id="56999999999")
    assert out.content == delegate.ack("dead_end") and "strong" not in calls
    assert spawned and spawned[0]["task"] == "prendé Oficina Tomi"
    assert "lights.txt" in spawned[0]["context"] and "Do not repeat" in spawned[0]["context"]
    saved = loop.sessions.get_existing("whatsapp-own:56999999999").messages
    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert saved[-1]["content"] == delegate.ack("dead_end")
    assert len(rows) == 1 and rows[0]["model"] == "cheap" and rows[0]["stop_reason"] == "max_iterations"


@pytest.mark.asyncio
async def test_a_dead_end_needs_no_powerful_model_to_go_to_a_sub_agent(tmp_path, monkeypatch):
    spawned = _spawned(monkeypatch)
    loop, calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "action", "reason": "x"}'),
        "cheap": LLMResponse(content=""),
    })
    cfg = _config()
    cfg.agents.defaults.model_powerful = None
    RUNTIME.config = cfg
    await loop.process_direct("apagá la luz", session_key="whatsapp-own:1",
                              channel="whatsapp", chat_id="1")
    assert spawned and spawned[0]["harness"] is True


@pytest.mark.asyncio
async def test_background_work_saves_the_acknowledgement_the_person_read(tmp_path, monkeypatch):
    from nanobot.household import delegate

    _spawned(monkeypatch)
    loop, _calls = _loop(tmp_path, {"tiny": LLMResponse(content='{"label": "background", "reason": "x"}')})
    RUNTIME.config = _config()
    out = await loop.process_direct("armame un informe", session_key="whatsapp-own:1",
                                    channel="whatsapp", chat_id="1")
    saved = loop.sessions.get_existing("whatsapp-own:1").messages
    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert saved[-1]["content"] == out.content == delegate.ack("long")


@pytest.mark.asyncio
async def test_an_api_turn_is_routed_like_a_chat_turn_when_a_default_effort_is_set(tmp_path):
    """HomeCore's chat goes through the API adapter. With `reasoningEffortDefault`
    set (every member's config sets "low"), the adapter used to hand the loop a
    preset runtime for every turn, which skips the classifier: no escalation,
    no plan, no everyday budget."""
    from nanobot.household.api_adapter import LoopAdapter

    loop, calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "complex", "reason": "advice"}'),
        "strong": LLMResponse(content="Pagalo al contado."),
    })
    cfg = _config()
    cfg.agents.defaults.reasoning_effort_default = "low"
    RUNTIME.config = cfg
    out = await LoopAdapter(loop).process_direct(
        "¿cuotas o contado con este interés?",
        session_key="api:homeweb:999000111:2026-11-22:s-1:3", channel="api", chat_id="default")
    assert out.content == "Pagalo al contado."
    assert calls == ["tiny", "strong"]


@pytest.mark.asyncio
async def test_a_blank_answer_keeps_the_stream_open_for_the_escalation(tmp_path, monkeypatch):
    """Fork `ends_unanswered`: ending the stream on the cheap model's blank
    answer sent the API's terminator first, and the strong model's answer to
    the same turn never reached the page that was waiting for it."""
    loop, calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "chat", "reason": "smalltalk"}'),
        "cheap": LLMResponse(content=""),
        "strong": LLMResponse(content="Las persianas están cerradas."),
    })
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")  # the image sets it
    RUNTIME.config = _config()
    ends: list[bool] = []

    async def on_stream(_delta):
        pass

    async def on_stream_end(*_a, resuming=False, **_kw):
        ends.append(resuming)

    out = await loop.process_direct("¿cerraste las persianas?", session_key="voice:cocina",
                                    channel="voice", chat_id="cocina",
                                    on_stream=on_stream, on_stream_end=on_stream_end)
    assert out.content == "Las persianas están cerradas."
    assert calls[-1] == "strong"
    # One terminator, at the very end: the blank cheap answer does not close it.
    assert ends[-1] is False and ends.count(False) == 1, ends


@pytest.mark.asyncio
async def test_the_classifier_is_warmed_once_at_start(tmp_path):
    """Fork loop.warm_up: the cold first call to the local classifier crossed
    its timeout in the dress rehearsal, routing the first real turn blind."""
    import asyncio

    from nanobot.household import _WARMING, bind_household_loop

    loop, calls = _loop(tmp_path, {"tiny": LLMResponse(content='{"label": "chat", "reason": "x"}')})
    bind_household_loop(loop, _config())
    await asyncio.gather(*list(_WARMING))
    assert calls == ["tiny"]

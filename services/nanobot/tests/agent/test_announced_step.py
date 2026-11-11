"""A reply that only announces a step, with no tool call, gets one nudge.

On 2026-11-09 and 11-10 the morning greeting was delivered as "Let me get a
forecast for low/high and rain." and "Let me get the weather.": the model said
what it would do, called nothing, and the sentence ended the turn.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.runner import AgentRunSpec, AgentRunner
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.runtime import ANNOUNCED_STEP_PROMPT, announces_next_step

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


@pytest.mark.parametrize("text", [
    "Let me get the weather.",
    "Let me get a forecast for low/high and rain.",
    "Only current weather. I need low/high and rain. Let me get forecast.",
    "Let me get the forecast for low/high.",
    "I'll check the calendar now.",
    "Okay, let me look that up",
    "Voy a buscar el pronóstico.",
    "Déjame revisar el clima.",
    "Hmm, that was executed via a tool call? Interesting — it returned weather. "
    "Let me get the forecast for rain info and low/high.",
    "Hmm, let me get the forecast.",
    "**Let me get the weather.**",
    "Let me check if it will rain.",
])
def test_announcements_are_caught(text):
    assert announces_next_step(text)


@pytest.mark.parametrize("text", [
    "",
    "Buenos días, Mora. Hoy es martes 10 de noviembre.",
    "Let me know if you need anything else.",
    "Avísame si querés que lo revise. ¿Te ayudo con algo más?",
    "Should I check the calendar too?",
    "Let's see how tomorrow goes!",
    "Listo. Voy a estar acá si me necesitás.",
    "It's 18°C and sunny; I'll remind you about the meeting at 9.",
    "Here is the summary: " + "x" * 300 + ". Let me check more later.",
    # promises about later, or waiting on the person (review of the first cut)
    "I set a reminder for 9am. I'll send you a message then.",
    "Ok, I'll turn off the lights at 11pm.",
    "Done! I turned off the kitchen lights. I'll check again tonight.",
    "I can't reach the camera right now. I'll try again in a few minutes.",
    "Cron created for 7:00. I'll send the forecast every morning.",
    "Programé la tarea. Voy a revisar el clima cada mañana a las 7.",
    "Buenos días! Hoy 14-27°C. Voy a ver si llueve más tarde.",
    "I'll turn on the kitchen lights once they confirm.",
    "I'll set the thermostat to 22 when you say yes.",
])
def test_answers_are_left_alone(text):
    assert not announces_next_step(text)


def _runner_with(replies, executed):
    provider = MagicMock()
    calls = {"n": 0, "seen": []}

    async def chat_with_retry(*, messages, **kwargs):
        calls["seen"].append([dict(m) for m in messages])
        reply = replies[min(calls["n"], len(replies) - 1)]
        calls["n"] += 1
        return reply

    provider.chat_with_retry = chat_with_retry

    async def fake_execute(name, params):
        executed.append(name)
        return "Santiago: 14-27°C, clear"

    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(side_effect=fake_execute)
    return AgentRunner(provider), tools, calls


async def _run(runner, tools, **kw):
    spec = dict(initial_messages=[{"role": "user", "content": "# Morning greeting"}],
                tools=tools, model="test-model", max_iterations=6,
                max_tool_result_chars=_MAX_TOOL_RESULT_CHARS)
    spec.update(kw)
    return await runner.run(AgentRunSpec(**spec))


@pytest.mark.asyncio
async def test_announcement_is_nudged_and_the_model_acts():
    executed: list[str] = []
    runner, tools, calls = _runner_with([
        LLMResponse(content="Let me get the weather.", tool_calls=[], usage={}),
        LLMResponse(content="", tool_calls=[
            ToolCallRequest(id="w1", name="exec", arguments={"command": "weather"})], usage={}),
        LLMResponse(content="Buenos días. 14-27°C, despejado.", tool_calls=[], usage={}),
    ], executed)

    result = await _run(runner, tools)

    assert result.final_content == "Buenos días. 14-27°C, despejado."
    assert executed == ["exec"]
    # The second request carried the announcement and the nudge, in that order.
    second = calls["seen"][1]
    assert second[-2]["role"] == "assistant" and "Let me get the weather" in str(second[-2]["content"])
    assert second[-1] == {"role": "user", "content": ANNOUNCED_STEP_PROMPT}


@pytest.mark.asyncio
async def test_a_second_announcement_ends_the_turn_as_before():
    executed: list[str] = []
    runner, tools, calls = _runner_with([
        LLMResponse(content="Let me get the weather.", tool_calls=[], usage={}),
        LLMResponse(content="Let me get the forecast.", tool_calls=[], usage={}),
        LLMResponse(content="never asked for", tool_calls=[], usage={}),
    ], executed)

    result = await _run(runner, tools)

    assert result.final_content == "Let me get the forecast."
    assert calls["n"] == 2
    assert executed == []


@pytest.mark.asyncio
async def test_an_ordinary_answer_costs_no_extra_call():
    executed: list[str] = []
    runner, tools, calls = _runner_with([
        LLMResponse(content="Buenos días, Tomi. Hoy es martes.", tool_calls=[], usage={}),
    ], executed)

    result = await _run(runner, tools)

    assert result.final_content == "Buenos días, Tomi. Hoy es martes."
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_read_only_plan_steps_are_not_nudged():
    executed: list[str] = []
    runner, tools, calls = _runner_with([
        LLMResponse(content="Let me get the weather.", tool_calls=[], usage={}),
    ], executed)

    result = await _run(runner, tools, read_only=True)

    assert result.final_content == "Let me get the weather."
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_no_nudge_on_the_last_allowed_pass():
    executed: list[str] = []
    runner, tools, calls = _runner_with([
        LLMResponse(content="Let me get the weather.", tool_calls=[], usage={}),
    ], executed)

    result = await _run(runner, tools, max_iterations=1)

    assert result.final_content == "Let me get the weather."
    assert result.stop_reason != "max_iterations"


def test_the_nudge_is_not_saved_as_something_the_person_said():
    from nanobot.agent.loop import AgentLoop
    from nanobot.session.manager import Session

    session = Session(key="websocket:test")
    messages = [
        {"role": "user", "content": "# Morning greeting"},
        {"role": "assistant", "content": "Let me get the weather."},
        {"role": "user", "content": ANNOUNCED_STEP_PROMPT},
        {"role": "assistant", "content": "Buenos días."},
    ]
    AgentLoop._save_turn(MagicMock(spec=AgentLoop), session, messages, 0)

    saved = [(m["role"], m["content"]) for m in session.messages]
    assert ("user", ANNOUNCED_STEP_PROMPT) not in saved
    assert saved[-1] == ("assistant", "Buenos días.")

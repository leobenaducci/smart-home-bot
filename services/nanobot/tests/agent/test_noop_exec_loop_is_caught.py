"""A model that keeps sending different no-op execs is still looping.

deepseek-v4-pro on 2026-09-21, done with a lights lookup and unable to write
the answer, sent `echo x`, `echo y`, `echo z` ... `echo done52`: 83 calls in
five minutes. The repetition guard counts calls by exact arguments, so a fresh
argument every time never repeated. A call that does nothing has no argument
worth distinguishing; every no-op exec counts as the same call.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.runner import _REPEATED_TOOL_CALL_LIMIT, AgentRunSpec, AgentRunner
from nanobot.providers.base import LLMResponse, ToolCallRequest


async def _run_with(commands):
    provider = MagicMock()
    n = {"i": 0}

    async def chat_with_retry(*, messages, **kwargs):
        i = n["i"]; n["i"] += 1
        if kwargs.get("tools") is None:            # the finalisation call: tools off
            return LLMResponse(content="done", tool_calls=[], usage={})
        if i < len(commands):
            return LLMResponse(content="working", tool_calls=[
                ToolCallRequest(id=f"c{i}", name="exec", arguments={"command": commands[i]})])
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.tool_names = ["exec"]
    tools.has = lambda name: name == "exec"
    tools.execute = AsyncMock(return_value="ok")
    return await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hacé algo"}],
        tools=tools, model="test-model", max_iterations=60, max_tool_result_chars=16_000,
    )), n["i"]


@pytest.mark.asyncio
async def test_distinct_noop_echoes_count_as_one_repeated_call_and_the_answer_is_asked_for():
    """After the guard fires, one call with the tools off asks for the answer
    from what the turn already has; the fake answers "done", so the turn ends
    completed with it rather than with the stuck sentence."""
    result, calls = await _run_with([f"echo done{i}" for i in range(40)])
    assert result.stop_reason == "completed"
    assert result.final_content == "done"
    assert calls == _REPEATED_TOOL_CALL_LIMIT + 1


@pytest.mark.asyncio
async def test_when_the_answer_does_not_come_the_honest_sentence_stands():
    provider = MagicMock()

    async def chat_with_retry(*, messages, **kwargs):
        if kwargs.get("tools") is None:            # the finalisation call: tools off
            return LLMResponse(content="   ", tool_calls=[], usage={})
        n = len([m for m in messages if m.get("role") == "assistant"])
        return LLMResponse(content="", tool_calls=[
            ToolCallRequest(id=f"c{n}", name="exec", arguments={"command": f"echo {n}"})])

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.tool_names = ["exec"]
    tools.has = lambda name: name == "exec"
    tools.execute = AsyncMock(return_value="ok")
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hacé algo"}],
        tools=tools, model="test-model", max_iterations=60, max_tool_result_chars=16_000,
    ))
    assert result.stop_reason == "repeated_tool_calls"
    assert "stopped instead of looping" in (result.final_content or "")


@pytest.mark.asyncio
async def test_distinct_real_commands_are_not_a_loop():
    result, calls = await _run_with([f"ls /tmp/dir{i}" for i in range(12)])
    assert result.stop_reason == "completed"
    assert result.final_content == "done"
    assert calls == 13

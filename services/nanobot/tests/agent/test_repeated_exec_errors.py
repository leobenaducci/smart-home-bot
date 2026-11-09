"""Circuit breaker for consecutive exec failures in one turn (#12)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.runner import AgentRunSpec, AgentRunner
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _exec_call(i: int) -> ToolCallRequest:
    return ToolCallRequest(id=f"exec_{i}", name="exec", arguments={"command": f"probe-{i}"})


@pytest.mark.asyncio
async def test_consecutive_exec_failures_refuse_further_exec_but_keep_turn_alive():
    """After 3 consecutive exec failures, refuse more exec but let other tools run.

    The turn must not end: the model gets the refusal, can use other tools, and
    can answer with what it already knows (adjustment #1).
    """
    provider = MagicMock()
    call_count = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(content="", tool_calls=[_exec_call(1)], usage={})
        if call_count["n"] == 2:
            return LLMResponse(content="", tool_calls=[_exec_call(2)], usage={})
        if call_count["n"] == 3:
            return LLMResponse(content="", tool_calls=[_exec_call(3)], usage={})
        if call_count["n"] == 4:
            # Tries exec again (must be refused) AND uses another tool.
            return LLMResponse(content="", tool_calls=[
                _exec_call(4),
                ToolCallRequest(id="rf_1", name="read_file", arguments={"path": "x"}),
            ], usage={})
        return LLMResponse(content="answered from what I know", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry

    exec_calls = {"n": 0}

    async def fake_execute(name, params):
        if name == "exec":
            exec_calls["n"] += 1
            return "Error: connection refused"
        if name == "read_file":
            return "file contents"
        return "ok"

    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(side_effect=fake_execute)

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "do the thing"}],
        tools=tools,
        model="test-model",
        max_iterations=8,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    # 3 failures executed; the 4th exec call is refused before execution.
    assert exec_calls["n"] == 3
    # Other tools still work and the model answers (turn not ended).
    assert result.final_content == "answered from what I know"
    assert "read_file" in result.tools_used

    # The model saw the blocking message for the refused exec call.
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    blocked = [m for m in tool_messages if "exec failed" in str(m.get("content", "")).lower()
               or "consecutive" in str(m.get("content", "")).lower()]
    assert blocked, f"expected a consecutive-exec block message, got: {tool_messages}"


@pytest.mark.asyncio
async def test_exec_nonzero_exit_not_counted_as_failure():
    """Exit code N is output, not a runner failure; it must not trip the breaker.

    Adjustment #2: runner treats as failure only exceptions or results starting
    with "Error". A non-zero exit comes back as output plus `Exit code: N`.
    """
    provider = MagicMock()
    call_count = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 4:
            return LLMResponse(content="", tool_calls=[_exec_call(call_count["n"])], usage={})
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry

    exec_calls = {"n": 0}

    async def fake_execute(name, params):
        if name == "exec":
            exec_calls["n"] += 1
            return "some output\nExit code: 1"
        return "ok"

    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(side_effect=fake_execute)

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "run checks"}],
        tools=tools,
        model="test-model",
        max_iterations=8,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    # All 4 exec calls executed; none refused by a consecutive-failure breaker.
    assert exec_calls["n"] == 4
    assert result.final_content == "done"
    blocked = [m for m in result.messages if m.get("role") == "tool"
               and "consecutive" in str(m.get("content", "")).lower()
               and "exec" in str(m.get("content", "")).lower()]
    assert not blocked, f"non-zero exits must not trigger breaker, got: {blocked}"

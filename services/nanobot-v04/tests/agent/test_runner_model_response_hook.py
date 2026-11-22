"""household: the one permanent runner patch, AgentHook.on_model_response."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from agent.runner_helpers import make_run_spec

from nanobot.agent.hook import AgentHook, AgentHookContext, CompositeHook, ModelResponseAction
from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _Echo(Tool):
    name = "echo"
    description = "Echo the text."
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs["text"])
        return f"echo:{kwargs['text']}"


def _provider(replies: list[LLMResponse]) -> tuple[MagicMock, list[list[dict]]]:
    provider = MagicMock(spec=LLMProvider)
    seen: list[list[dict]] = []
    it = iter(replies)

    async def chat(*, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        return next(it)

    provider.chat_with_retry = chat
    provider.chat_stream_with_retry = chat
    return provider, seen


async def _run(provider, hook, tools):
    return await AgentRunner().run(make_run_spec(
        provider, initial_messages=[{"role": "user", "content": "hi"}], tools=tools,
        model="m", max_iterations=5, max_tool_result_chars=10_000, hook=hook,
    ))


@pytest.mark.asyncio
async def test_rewritten_response_executes_synthesised_call_and_clears_provider_state():
    echo = _Echo()
    tools = ToolRegistry()
    tools.register(echo)
    first = LLMResponse(content='CALL echo {"text": "Luz Paula"}', tool_calls=[])
    first.provider_state = MagicMock()  # opaque state that knows nothing of the call
    provider, _ = _provider([first, LLMResponse(content="done", tool_calls=[])])
    rewritten: list[LLMResponse] = []

    class TextCalls(AgentHook):
        async def on_model_response(self, context: AgentHookContext):
            r = context.response
            if r and r.content and r.content.startswith("CALL echo"):
                new = LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="t1", name="echo", arguments={"text": "Luz Paula"})],
                    finish_reason="tool_calls",
                    provider_state=r.provider_state,
                )
                rewritten.append(new)
                return ModelResponseAction(response=new)
            return None

    result = await _run(provider, TextCalls(), tools)
    assert echo.calls == ["Luz Paula"]
    assert result.final_content == "done"
    assert rewritten[0].provider_state is None


@pytest.mark.asyncio
async def test_refused_call_is_answered_not_executed():
    echo = _Echo()
    tools = ToolRegistry()
    tools.register(echo)
    call = ToolCallRequest(id="t1", name="echo", arguments={"text": "Oficina Tomi"})
    provider, seen = _provider([
        LLMResponse(content="", tool_calls=[call], finish_reason="tool_calls"),
        LLMResponse(content="asked first", tool_calls=[]),
    ])

    class Gate(AgentHook):
        async def on_model_response(self, context):
            if context.response.tool_calls:
                return ModelResponseAction(refuse={"t1": "Refused: ask for a go-ahead first."})
            return None

    result = await _run(provider, Gate(), tools)
    assert echo.calls == []
    tool_msgs = [m for m in seen[1] if m.get("role") == "tool"]
    assert tool_msgs and "go-ahead" in str(tool_msgs[0]["content"])
    assert result.final_content == "asked first"
    assert result.tool_events[0]["detail"] == "refused"


@pytest.mark.asyncio
async def test_inject_keeps_the_turn_going_instead_of_finishing():
    tools = ToolRegistry()
    provider, seen = _provider([
        LLMResponse(content="I will check the lights now.", tool_calls=[]),
        LLMResponse(content="All lights are off.", tool_calls=[]),
    ])
    nudged: list[int] = []

    class Nudge(AgentHook):
        async def on_model_response(self, context):
            if not nudged and "will check" in (context.response.content or ""):
                nudged.append(context.iteration)
                return ModelResponseAction(inject=[{"role": "user", "content": "Do it now."}])
            return None

    result = await _run(provider, CompositeHook([AgentHook(), Nudge()]), tools)
    assert result.final_content == "All lights are off."
    assert [m["content"] for m in seen[1][-2:]] == ["I will check the lights now.", "Do it now."]


@pytest.mark.asyncio
async def test_composite_pipeline_isolates_a_failing_hook():
    tools = ToolRegistry()
    provider, _ = _provider([LLMResponse(content="ok", tool_calls=[])])

    class Broken(AgentHook):
        async def on_model_response(self, context):
            raise RuntimeError("boom")

    result = await _run(provider, CompositeHook([Broken()]), tools)
    assert result.final_content == "ok"

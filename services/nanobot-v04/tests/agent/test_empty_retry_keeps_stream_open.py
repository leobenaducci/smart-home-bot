"""Trial port (fork 408c897): retrying an empty reply keeps the stream open."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from agent.runner_helpers import make_run_spec

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.runner import AgentRunner
from nanobot.providers.base import LLMProvider, LLMResponse


@pytest.mark.asyncio
async def test_empty_then_answer_only_the_real_end_closes_the_stream():
    provider = MagicMock(spec=LLMProvider)
    replies = iter([LLMResponse(content="", tool_calls=[], usage=None),
                    LLMResponse(content="hello", tool_calls=[], usage=None)])

    async def chat_stream_with_retry(*, on_content_delta, **kwargs):
        r = next(replies)
        if r.content:
            await on_content_delta(r.content)
        return r

    provider.chat_stream_with_retry = chat_stream_with_retry
    provider.chat_with_retry = AsyncMock()
    tools = MagicMock()
    tools.get_definitions.return_value = []
    endings: list[bool] = []

    class StreamingHook(AgentHook):
        def wants_streaming(self) -> bool:
            return True

        async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
            endings.append(resuming)

    result = await AgentRunner().run(make_run_spec(
        provider, initial_messages=[], tools=tools, model="m", max_iterations=3,
        max_tool_result_chars=10_000, hook=StreamingHook(),
    ))

    assert result.final_content == "hello"
    assert endings == [True, False]

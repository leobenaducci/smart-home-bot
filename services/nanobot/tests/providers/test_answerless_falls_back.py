"""An answer with nothing in it is a failure, and the fallback answers instead.

The household's requirement is blunt: a notification must not be lost. The way
it was being lost is not an error path at all -- a reasoning model asked to do
triage spends its whole token budget thinking, stops at the cap, and returns
`content: ""` with `finish_reason: "length"`. The request succeeded, so every
guard downstream passed it through, and the family got a turn with nothing in
it.

Measured on Qwen3.6-35B-A3B: both the default and "low" produce exactly that.
`reasoning_effort: "none"` fixes that model. These pin the floor under every
model, because the cause does not matter to somebody who did not get told the
chore was due.
"""
import asyncio

import pytest

from nanobot.providers import base as provider_base
from nanobot.providers.base import (
    GenerationSettings,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
)


class _Provider(LLMProvider):
    """Answers empty for everything except the model named as the rescue."""

    def __init__(self, fallback_model=None, rescue="rescuer"):
        super().__init__()
        self.models: list[str | None] = []
        self._rescue = rescue
        self.generation = GenerationSettings(fallback_model=fallback_model)

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.models.append(kwargs.get("model"))
        if kwargs.get("model") == self._rescue:
            return LLMResponse(content="the bins go out tonight",
                               finish_reason="stop")
        # The exact shape the local model returns: budget spent thinking, cap
        # reached, nothing said.
        return LLMResponse(content="", finish_reason="length",
                           reasoning_content="…" * 40,
                           usage={"completion_tokens": 4096})

    def get_default_model(self) -> str:
        return "qwen3.6-35b-a3b"


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    _real_sleep = asyncio.sleep

    async def _fake_sleep(delay, result=None):
        await _real_sleep(0)
        return result

    monkeypatch.setattr(provider_base.asyncio, "sleep", _fake_sleep)


def test_an_empty_answer_is_recognised_as_model_specific():
    """It takes the fallback directly rather than walking the retry ladder:
    re-asking the same model the same question returns the same nothing."""
    marked = LLMResponse(content=None, finish_reason="error",
                         error_kind="answerless")
    assert LLMProvider._is_model_specific_error(marked)


def test_the_fallback_answers_in_its_place():
    """The whole point of the file: an empty completion reaches the configured
    rescue instead of being handed back as a success with nothing in it."""
    provider = _Provider(fallback_model="rescuer")
    result = asyncio.run(provider.chat_with_retry(
        [{"role": "user", "content": "is the bin day today?"}],
        model="qwen3.6-35b-a3b"))
    assert result.content == "the bins go out tonight"
    assert "rescuer" in provider.models


def test_with_no_fallback_the_family_is_not_read_a_diagnosis():
    """`_ANSWERLESS` is a label for the log and the profiler, never a reply.

    `runner.py` renders an error turn as `clean or spec.error_message`, so a
    response carrying that sentence would be spoken in Alfred's voice -- and
    HomeCore pushes the reply verbatim as the notification body, which is how
    "the model returned no answer and called no tool" would arrive as a chore
    reminder.
    """
    provider = _Provider(fallback_model=None)
    result = asyncio.run(provider.chat_with_retry(
        [{"role": "user", "content": "is the bin day today?"}],
        model="qwen3.6-35b-a3b"))
    assert result.finish_reason == "error"
    assert result.error_kind == "answerless"
    assert not (result.content or "").strip()


def test_the_reasoning_that_ate_the_budget_survives_the_relabelling():
    """The diagnosis is that thinking spent the completion. Rebuilding the
    response without `reasoning_content` reports zero reasoning characters to
    the profiler for exactly the turn the field exists to explain."""
    provider = _Provider(fallback_model=None)
    result = asyncio.run(provider.chat_with_retry(
        [{"role": "user", "content": "is the bin day today?"}],
        model="qwen3.6-35b-a3b"))
    assert result.reasoning_content
    # And the model's own verdict is kept beside our label.
    assert result.model_finish_reason == "length"


def test_a_tool_call_with_no_prose_is_not_a_failure():
    """The common shape this must not break: the model calls a tool and says
    nothing. Empty content alone would fail every tool-using turn."""

    class _ToolProvider(_Provider):
        async def chat(self, *args, **kwargs) -> LLMResponse:
            self.models.append(kwargs.get("model"))
            return LLMResponse(
                content="", finish_reason="tool_calls",
                tool_calls=[ToolCallRequest(id="1", name="tasks", arguments={})])

    provider = _ToolProvider(fallback_model="rescuer")
    result = asyncio.run(provider.chat_with_retry(
        [{"role": "user", "content": "what is due?"}],
        model="qwen3.6-35b-a3b"))
    assert result.finish_reason == "tool_calls"
    assert provider.models == ["qwen3.6-35b-a3b"]


def test_whitespace_only_counts_as_empty():
    """Blank is blank -- but only `length` is escalated here. See below."""
    class _BlankProvider(_Provider):
        async def chat(self, *args, **kwargs) -> LLMResponse:
            self.models.append(kwargs.get("model"))
            if kwargs.get("model") == "rescuer":
                return LLMResponse(content="ok", finish_reason="stop")
            return LLMResponse(content="   \n\t ", finish_reason="length")

    provider = _BlankProvider(fallback_model="rescuer")
    result = asyncio.run(provider.chat_with_retry(
        [{"role": "user", "content": "hello"}], model="qwen3.6-35b-a3b"))
    assert result.content == "ok"


def test_a_blank_that_merely_stopped_is_left_to_the_runner():
    """The boundary, and the reason this guard is narrow.

    `runner.py` already recovers a blank completion: two retries, then a
    finalization prompt, both gated on `finish_reason != "error"`. Escalating
    every blank here would step in front of a floor that already works -- and
    on a household with no `modelFallback` it would turn a model that stumbled
    once into a failed turn.

    `length` is the shape that recovery cannot help, because the budget is gone
    and the same question returns the same nothing. `stop` is not, so it must
    reach the runner untouched.
    """
    class _StoppedBlank(_Provider):
        async def chat(self, *args, **kwargs) -> LLMResponse:
            self.models.append(kwargs.get("model"))
            return LLMResponse(content="", finish_reason="stop")

    provider = _StoppedBlank(fallback_model="rescuer")
    result = asyncio.run(provider.chat_with_retry(
        [{"role": "user", "content": "hello"}], model="qwen3.6-35b-a3b"))
    assert result.finish_reason == "stop", "the runner's own recovery must still get it"
    assert "rescuer" not in provider.models, "no fallback should have been spent"

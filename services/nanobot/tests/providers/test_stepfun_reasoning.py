"""The model's reasoning, and when (if ever) it is the reply.

The parsers report what came back: `content` as content, `reasoning` /
`reasoning_content` as reasoning. One step, `_settle_reply`, reconciles them
for every path:

- StepFun's Plan API answers in `reasoning` and leaves `content` empty, so for
  StepFun -- by provider, base URL or model name -- an empty reply takes it.
- Everywhere else `reasoning` is the model's thinking. NanoGPT streaming
  DeepSeek V4 Flash, on a turn where the model gave no answer, carried that
  thinking again as `content`, and it was published as the assistant's reply:
  "He says he already fed her... let me do both calls", with no call made. A
  reply that is the reasoning again -- whole, whitespace aside, or a long
  enough piece of it -- is no reply.

The stream holds content back while it could still be such a copy, so the
notes are never painted live either.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nanobot.providers.base import LLMResponse
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import find_by_name


def _provider(**kw) -> OpenAICompatProvider:
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        return OpenAICompatProvider(**kw)


def _stepfun() -> OpenAICompatProvider:
    return _provider(spec=find_by_name("stepfun"))


def _gateway() -> OpenAICompatProvider:
    return _provider(spec=find_by_name("nanogpt"), api_base="https://nano-gpt.com/api/v1")


def _settled(provider, response, model=None):
    return provider._settle_reply(provider._parse(response), model)


def _sdk_message(content, reasoning=None, reasoning_content=None):
    msg = SimpleNamespace(content=content, tool_calls=None)
    if reasoning is not None:
        msg.reasoning = reasoning
    if reasoning_content is not None:
        msg.reasoning_content = reasoning_content
    return msg


def _sdk_chunk(reasoning_content=None, reasoning=None, content=None, finish=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning_content,
                            reasoning=reasoning, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, delta=delta)],
                           usage=None)


# ── The parsers report, they do not decide ──────────────────────────────────


def test_parse_leaves_reasoning_as_reasoning() -> None:
    result = _gateway()._parse({"choices": [{"message": {
        "content": None, "reasoning": "thinking"}, "finish_reason": "stop"}]})

    assert not result.content
    assert result.reasoning_content == "thinking"


def test_parse_reasoning_content_takes_priority() -> None:
    result = _gateway()._parse({"choices": [{"message": {
        "content": None, "reasoning": "informal", "reasoning_content": "formal"},
        "finish_reason": "stop"}]})

    assert result.reasoning_content == "formal"


def test_parse_sdk_reasoning_content_takes_priority() -> None:
    msg = _sdk_message(content=None, reasoning="informal", reasoning_content="formal")
    result = _gateway()._parse(SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=msg)], usage=None))

    assert result.reasoning_content == "formal"


def test_parse_chunks_reasoning_and_precedence() -> None:
    result = OpenAICompatProvider._parse_chunks([
        _sdk_chunk(reasoning_content="formal: ", reasoning="informal: "),
        _sdk_chunk(reasoning="step 2."),
        _sdk_chunk(content="final answer", finish="stop"),
    ])

    assert result.content == "final answer"
    assert result.reasoning_content == "formal: step 2."


def test_parse_dict_chunks_reasoning() -> None:
    result = OpenAICompatProvider._parse_chunks([
        {"choices": [{"finish_reason": None, "delta": {"content": None, "reasoning": "a "}}]},
        {"choices": [{"finish_reason": "stop", "delta": {"content": "answer"}}]},
    ])

    assert result.content == "answer"
    assert result.reasoning_content == "a "


def test_standard_reply_untouched() -> None:
    result = _settled(_gateway(), {"choices": [{"message": {"content": "Hello!"},
                                                "finish_reason": "stop"}]})

    assert result.content == "Hello!"
    assert result.reasoning_content is None


def test_reply_with_its_own_reasoning_untouched() -> None:
    result = _settled(_gateway(), {"choices": [{"message": {
        "content": "The answer is 42.", "reasoning_content": "Let me think..."},
        "finish_reason": "stop"}]})

    assert result.content == "The answer is 42."


# ── StepFun: the reasoning field is the answer ──────────────────────────────


def test_stepfun_empty_reply_takes_the_reasoning() -> None:
    result = _settled(_stepfun(), {"choices": [{"message": {
        "content": None, "reasoning": "The answer is 42."}, "finish_reason": "stop"}]})

    assert result.content == "The answer is 42."
    assert result.reasoning_content == "The answer is 42."


def test_stepfun_sdk_empty_reply_takes_the_reasoning() -> None:
    msg = _sdk_message(content=None, reasoning="After analysis: 4.")
    result = _stepfun()._settle_reply(_stepfun()._parse(SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=msg)], usage=None)))

    assert result.content == "After analysis: 4."


def test_stepfun_reached_through_a_custom_base_url() -> None:
    provider = _provider(api_base="https://api.stepfun.com/v1")
    result = _settled(provider, {"choices": [{"message": {
        "content": None, "reasoning": "42."}, "finish_reason": "stop"}]})

    assert result.content == "42."


def test_stepfun_model_reached_through_a_gateway() -> None:
    result = _settled(_gateway(), {"choices": [{"message": {
        "content": None, "reasoning": "42."}, "finish_reason": "stop"}]},
        model="stepfun-ai/step-3")

    assert result.content == "42."


def test_stepfun_answer_echoed_in_both_fields_is_kept() -> None:
    result = _settled(_stepfun(), {"choices": [{"message": {
        "content": "The answer is 42.", "reasoning": "The answer is 42."},
        "finish_reason": "stop"}]})

    assert result.content == "The answer is 42."


# ── Everyone else: the reasoning is never the reply ─────────────────────────


def test_gateway_reasoning_beside_a_tool_call_is_not_the_reply() -> None:
    result = _settled(_gateway(), {"choices": [{"message": {
        "content": None, "reasoning": "He says he fed her. Let me complete 1106.",
        "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "complete_chore", "arguments": "{\"task_id\": 1106}"}}],
    }, "finish_reason": "tool_calls"}]})

    assert not result.content
    assert result.reasoning_content == "He says he fed her. Let me complete 1106."
    assert [t.name for t in result.tool_calls] == ["complete_chore"]


def test_gateway_reasoning_alone_stays_empty() -> None:
    result = _settled(_gateway(), {"choices": [{"message": {
        "content": "", "reasoning": "I said I would but I haven't made the calls yet."},
        "finish_reason": "stop"}]})

    assert not result.content
    assert result.reasoning_content


def test_echo_whole_is_dropped() -> None:
    notes = "Let me get the forecast."
    result = _settled(_gateway(), {"choices": [{"message": {
        "content": notes, "reasoning": notes}, "finish_reason": "stop"}]})

    assert not result.content


def test_echo_differing_only_in_whitespace_is_dropped() -> None:
    notes = "He says he did it.\n\nLet me complete 1111."
    result = _settled(_gateway(), {"choices": [{"message": {
        "content": "\n\n" + notes.replace("\n\n", " ") + "\n", "reasoning": notes},
        "finish_reason": "stop"}]})

    assert not result.content


def test_echo_cut_short_is_dropped() -> None:
    notes = ("He says he fed her in the morning and will give dinner later. "
             "Let me check the list first, then complete 1106.")
    result = _settled(_gateway(), {"choices": [{"message": {
        "content": notes[:70], "reasoning": notes}, "finish_reason": "length"}]})

    assert not result.content


def test_short_answer_also_in_the_reasoning_is_kept() -> None:
    result = _settled(_gateway(), {"choices": [{"message": {
        "content": "Listo.", "reasoning": "He did it. I'll reply: Listo. Then stop."},
        "finish_reason": "stop"}]})

    assert result.content == "Listo."


def test_streamed_echo_settles_to_no_reply() -> None:
    notes = 'He says "Ya lo hice". Let me check the list first.'
    provider = _gateway()
    result = provider._settle_reply(OpenAICompatProvider._parse_chunks([
        _sdk_chunk(reasoning=notes), _sdk_chunk(content=notes, finish="stop")]))

    assert not result.content
    assert result.reasoning_content == notes


def test_error_replies_are_left_alone() -> None:
    err = LLMResponse(content="boom", finish_reason="error", reasoning_content="boom")

    assert _gateway()._settle_reply(err).content == "boom"


# ── The live stream ─────────────────────────────────────────────────────────


def _stream_of(*chunks):
    class _S:
        def __aiter__(self):
            self._it = iter(chunks)
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration
    return _S()


def _streamed(chunks, provider=None, model="deepseek/deepseek-v4.1-flash"):
    provider = provider or _gateway()
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=_stream_of(*chunks)))))
    shown: list[str] = []

    async def on_content(d: str) -> None:
        shown.append(d)

    result = asyncio.run(provider.chat_stream(
        messages=[{"role": "user", "content": "Ya lo hice"}],
        model=model, on_content_delta=on_content))
    return result, "".join(shown)


def test_stream_echo_is_never_shown_live() -> None:
    notes = "He says he did it. Let me complete 1111."
    result, shown = _streamed([
        _sdk_chunk(reasoning=notes[:20]), _sdk_chunk(reasoning=notes[20:]),
        _sdk_chunk(content=notes[:25]), _sdk_chunk(content=notes[25:], finish="stop"),
    ])

    assert shown == ""
    assert not result.content


def test_stream_echo_after_a_blank_line_is_never_shown() -> None:
    notes = "He says he did it. Let me complete 1111."
    result, shown = _streamed([
        _sdk_chunk(reasoning=notes),
        _sdk_chunk(content="\n\n"), _sdk_chunk(content=notes, finish="stop"),
    ])

    assert shown == ""
    assert not result.content


def test_stream_real_reply_is_shown() -> None:
    result, shown = _streamed([
        _sdk_chunk(reasoning="He fed the cat."),
        _sdk_chunk(content="¡Listo, "), _sdk_chunk(content="la marqué!", finish="stop"),
    ])

    assert shown == "¡Listo, la marqué!"
    assert result.content == "¡Listo, la marqué!"


def test_stream_stops_holding_once_the_reply_diverged() -> None:
    # "Ok." is the whole reasoning, and also how the reply ends: once the
    # reply has said something else it is never held again.
    result, shown = _streamed([
        _sdk_chunk(reasoning="Ok."),
        _sdk_chunk(content="Hecho. "), _sdk_chunk(content="Ok.", finish="stop"),
    ])

    assert shown == "Hecho. Ok."
    assert result.content == "Hecho. Ok."


def test_stream_short_answer_also_in_the_reasoning_is_shown() -> None:
    result, shown = _streamed([
        _sdk_chunk(reasoning="Listo, he did it. Mark 1111."),
        _sdk_chunk(content="Listo", finish="stop"),
    ])

    assert shown == "Listo"
    assert result.content == "Listo"


def test_stream_stepfun_answer_is_shown() -> None:
    result, shown = _streamed([
        _sdk_chunk(reasoning="The answer is 42."),
        _sdk_chunk(content="The answer is 42.", finish="stop"),
    ], provider=_stepfun(), model="step-3")

    assert shown == "The answer is 42."
    assert result.content == "The answer is 42."

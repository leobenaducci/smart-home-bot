"""Tests for StepFun Plan API reasoning field fallback in OpenAICompatProvider.

StepFun Plan API returns response content in the 'reasoning' field when
the model is in thinking mode and 'content' is empty. This test module
verifies the fallback logic for all code paths.
"""

from types import SimpleNamespace
from unittest.mock import patch

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import find_by_name


def _stepfun() -> OpenAICompatProvider:
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        return OpenAICompatProvider(spec=find_by_name("stepfun"))


# ── _parse: dict branch ─────────────────────────────────────────────────────


def test_parse_dict_stepfun_reasoning_fallback() -> None:
    """When content is None and reasoning exists, content falls back to reasoning."""
    provider = _stepfun()

    response = {
        "choices": [{
            "message": {
                "content": None,
                "reasoning": "Let me think... The answer is 42.",
            },
            "finish_reason": "stop",
        }],
    }

    result = provider._parse(response)

    assert result.content == "Let me think... The answer is 42."
    # reasoning_content should also be populated from reasoning
    assert result.reasoning_content == "Let me think... The answer is 42."


def test_parse_dict_stepfun_reasoning_priority() -> None:
    """reasoning_content field takes priority over reasoning when both present."""
    provider = _stepfun()

    response = {
        "choices": [{
            "message": {
                "content": None,
                "reasoning": "informal thinking",
                "reasoning_content": "formal reasoning content",
            },
            "finish_reason": "stop",
        }],
    }

    result = provider._parse(response)

    assert result.content == "informal thinking"
    # reasoning_content uses the dedicated field, not reasoning
    assert result.reasoning_content == "formal reasoning content"


# ── _parse: SDK object branch ───────────────────────────────────────────────


def _make_sdk_message(content, reasoning=None, reasoning_content=None):
    """Create a mock SDK message object."""
    msg = SimpleNamespace(content=content, tool_calls=None)
    if reasoning is not None:
        msg.reasoning = reasoning
    if reasoning_content is not None:
        msg.reasoning_content = reasoning_content
    return msg


def test_parse_sdk_stepfun_reasoning_fallback() -> None:
    """SDK branch: content falls back to msg.reasoning when content is None."""
    provider = _stepfun()

    msg = _make_sdk_message(content=None, reasoning="After analysis: result is 4.")
    choice = SimpleNamespace(finish_reason="stop", message=msg)
    response = SimpleNamespace(choices=[choice], usage=None)

    result = provider._parse(response)

    assert result.content == "After analysis: result is 4."
    assert result.reasoning_content == "After analysis: result is 4."


def test_parse_sdk_stepfun_reasoning_priority() -> None:
    """reasoning_content field takes priority over reasoning in SDK branch."""
    provider = _stepfun()

    msg = _make_sdk_message(
        content=None,
        reasoning="thinking process",
        reasoning_content="formal reasoning"
    )
    choice = SimpleNamespace(finish_reason="stop", message=msg)
    response = SimpleNamespace(choices=[choice], usage=None)

    result = provider._parse(response)

    assert result.content == "thinking process"
    assert result.reasoning_content == "formal reasoning"


# ── _parse_chunks: streaming dict branch ────────────────────────────────────


def test_parse_chunks_dict_stepfun_reasoning_fallback() -> None:
    """Streaming dict: reasoning field used when reasoning_content is absent."""
    chunks = [
        {
            "choices": [{
                "finish_reason": None,
                "delta": {"content": None, "reasoning": "Thinking step 1... "},
            }],
        },
        {
            "choices": [{
                "finish_reason": None,
                "delta": {"content": None, "reasoning": "step 2."},
            }],
        },
        {
            "choices": [{
                "finish_reason": "stop",
                "delta": {"content": "final answer"},
            }],
        },
    ]

    result = OpenAICompatProvider._parse_chunks(chunks)

    assert result.content == "final answer"
    assert result.reasoning_content == "Thinking step 1... step 2."


# ── Regression: normal models unaffected ────────────────────────────────────


def test_parse_dict_normal_model_with_reasoning_content_unaffected() -> None:
    """Models that use reasoning_content (e.g. DeepSeek-R1) are not affected."""
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider()

    response = {
        "choices": [{
            "message": {
                "content": "The answer is 42.",
                "reasoning_content": "Let me think step by step...",
            },
            "finish_reason": "stop",
        }],
    }

    result = provider._parse(response)

    assert result.content == "The answer is 42."
    assert result.reasoning_content == "Let me think step by step..."


def test_parse_dict_standard_model_no_reasoning_unaffected() -> None:
    """Standard models (no reasoning fields at all) work exactly as before."""
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider()

    response = {
        "choices": [{
            "message": {"content": "Hello!"},
            "finish_reason": "stop",
        }],
    }

    result = provider._parse(response)

    assert result.content == "Hello!"
    assert result.reasoning_content is None


def test_parse_chunks_dict_reasoning_precedence() -> None:
    """reasoning_content takes precedence over reasoning in dict chunks."""
    chunks = [
        {
            "choices": [{
                "finish_reason": None,
                "delta": {
                    "content": None,
                    "reasoning_content": "formal: ",
                    "reasoning": "informal: ",
                },
            }],
        },
        {
            "choices": [{
                "finish_reason": "stop",
                "delta": {"content": "result"},
            }],
        },
    ]

    result = OpenAICompatProvider._parse_chunks(chunks)

    assert result.reasoning_content == "formal: "


# ── _parse_chunks: streaming SDK-object branch ─────────────────────────────


def _make_sdk_chunk(reasoning_content=None, reasoning=None, content=None, finish=None):
    """Create a mock SDK chunk object."""
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        reasoning=reasoning,
        tool_calls=None,
    )
    choice = SimpleNamespace(finish_reason=finish, delta=delta)
    return SimpleNamespace(choices=[choice], usage=None)


def test_parse_chunks_sdk_stepfun_reasoning_fallback() -> None:
    """SDK streaming: reasoning field used when reasoning_content is None."""
    chunks = [
        _make_sdk_chunk(reasoning="Thinking... ", content=None, finish=None),
        _make_sdk_chunk(reasoning=None, content="answer", finish="stop"),
    ]

    result = OpenAICompatProvider._parse_chunks(chunks)

    assert result.content == "answer"
    assert result.reasoning_content == "Thinking... "


def test_parse_chunks_sdk_reasoning_precedence() -> None:
    """reasoning_content takes precedence over reasoning in SDK chunks."""
    chunks = [
        _make_sdk_chunk(reasoning_content="formal: ", reasoning="informal: ", content=None),
        _make_sdk_chunk(reasoning_content=None, reasoning=None, content="result", finish="stop"),
    ]

    result = OpenAICompatProvider._parse_chunks(chunks)

    assert result.reasoning_content == "formal: "


# ── Every other gateway: `reasoning` is thinking, never the reply ───────────
#
# NanoGPT and OpenRouter return the model's reasoning in `reasoning` and leave
# `content` empty beside a tool call. Promoting it sent the assistant's private
# planning ("He says he already fed her... let me do both calls") to the
# household as its reply, while the calls it described were never made.


def _gateway() -> OpenAICompatProvider:
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        return OpenAICompatProvider()


def test_parse_dict_gateway_reasoning_is_not_the_reply() -> None:
    response = {"choices": [{"message": {
        "content": None, "reasoning": "He says he fed her. Let me complete 1106.",
        "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "complete_chore", "arguments": "{\"task_id\": 1106}"}}],
    }, "finish_reason": "tool_calls"}]}

    result = _gateway()._parse(response)

    assert not result.content
    assert result.reasoning_content == "He says he fed her. Let me complete 1106."
    assert [t.name for t in result.tool_calls] == ["complete_chore"]


def test_parse_dict_gateway_reasoning_without_content_stays_empty() -> None:
    response = {"choices": [{"message": {
        "content": "", "reasoning": "I said I would but I haven't made the calls yet.",
    }, "finish_reason": "stop"}]}

    result = _gateway()._parse(response)

    assert not result.content
    assert result.reasoning_content


def test_parse_sdk_gateway_reasoning_is_not_the_reply() -> None:
    msg = _make_sdk_message(content=None, reasoning="Let me get the forecast.")
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=msg)],
                               usage=None)

    result = _gateway()._parse(response)

    assert not result.content
    assert result.reasoning_content == "Let me get the forecast."


# ── Reasoning echoed back as the reply ──────────────────────────────────────
#
# NanoGPT streaming DeepSeek V4 Flash, on a turn where the model gave no
# answer, carried the reasoning again as `content`, word for word. A reply is
# never identical to the thinking behind it, so that is no reply at all.


def test_parse_chunks_reasoning_echo_is_no_reply() -> None:
    notes = 'He says "Ya lo hice". Let me check the list first.'
    chunks = [
        _make_sdk_chunk(reasoning=notes),
        _make_sdk_chunk(content=notes, finish="stop"),
    ]

    result = OpenAICompatProvider._parse_chunks(chunks)

    assert not result.content
    assert result.reasoning_content == notes


def test_parse_chunks_reply_that_differs_is_kept() -> None:
    chunks = [
        _make_sdk_chunk(reasoning="He fed the cat."),
        _make_sdk_chunk(content="¡Listo, la marqué!", finish="stop"),
    ]

    assert OpenAICompatProvider._parse_chunks(chunks).content == "¡Listo, la marqué!"


def test_parse_dict_gateway_reasoning_echo_is_no_reply() -> None:
    response = {"choices": [{"message": {
        "content": "Let me get the forecast.", "reasoning": "Let me get the forecast.",
    }, "finish_reason": "stop"}]}

    assert not _gateway()._parse(response).content


def test_parse_dict_stepfun_answer_in_reasoning_still_kept() -> None:
    response = {"choices": [{"message": {
        "content": None, "reasoning": "The answer is 42.",
    }, "finish_reason": "stop"}]}

    assert _stepfun()._parse(response).content == "The answer is 42."


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


def _streamed(chunks):
    import asyncio
    from unittest.mock import AsyncMock

    provider = _gateway()
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=_stream_of(*chunks)))))
    shown: list[str] = []

    async def on_content(d: str) -> None:
        shown.append(d)

    result = asyncio.run(provider.chat_stream(
        messages=[{"role": "user", "content": "Ya lo hice"}],
        model="deepseek/deepseek-v4.1-flash", on_content_delta=on_content))
    return result, "".join(shown)


def test_stream_echo_is_never_shown_live() -> None:
    notes = "He says he did it. Let me complete 1111."
    result, shown = _streamed([
        _make_sdk_chunk(reasoning=notes[:20]),
        _make_sdk_chunk(reasoning=notes[20:]),
        _make_sdk_chunk(content=notes[:25]),
        _make_sdk_chunk(content=notes[25:], finish="stop"),
    ])

    assert shown == ""
    assert not result.content


def test_stream_real_reply_is_shown() -> None:
    result, shown = _streamed([
        _make_sdk_chunk(reasoning="He fed the cat."),
        _make_sdk_chunk(content="¡Listo, "),
        _make_sdk_chunk(content="la marqué!", finish="stop"),
    ])

    assert shown == "¡Listo, la marqué!"
    assert result.content == "¡Listo, la marqué!"


def test_stream_reply_opening_like_the_reasoning_is_still_shown() -> None:
    result, shown = _streamed([
        _make_sdk_chunk(reasoning="Listo, he did it. Mark 1111."),
        _make_sdk_chunk(content="Listo", finish="stop"),
    ])

    assert shown == "Listo"
    assert result.content == "Listo"

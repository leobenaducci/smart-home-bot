"""Trial port: a reply that only repeats the reasoning is no reply."""

import pytest

from nanobot.providers.base import LLMResponse
from nanobot.providers.openai_compat_provider import OpenAICompatProvider, _is_reasoning_echo
from nanobot.providers.registry import find_by_name

THOUGHT = "The user wants the lights off. I should call the lights skill with action off for the office."


def _provider(name="custom"):
    return OpenAICompatProvider(api_key="k", api_base="http://gw.invalid/v1",
                                default_model="m", spec=find_by_name(name))


@pytest.mark.parametrize("content", [THOUGHT, "  " + THOUGHT.replace(" ", "\n", 3), THOUGHT[:60]])
def test_echo_is_dropped(content):
    r = _provider()._settle_reply(LLMResponse(content=content, reasoning_content=THOUGHT, finish_reason="stop"))
    assert r.content is None


def test_short_answer_also_in_reasoning_is_kept():
    r = _provider()._settle_reply(LLMResponse(content="Done.", reasoning_content="Done. " + THOUGHT,
                                              finish_reason="stop"))
    assert r.content == "Done."


def test_errors_and_real_replies_untouched():
    p = _provider()
    assert p._settle_reply(LLMResponse(content=THOUGHT, reasoning_content=THOUGHT,
                                       finish_reason="error")).content == THOUGHT
    assert p._settle_reply(LLMResponse(content="Lights off.", reasoning_content=THOUGHT,
                                       finish_reason="stop")).content == "Lights off."


def test_reasoning_as_content_spec_is_exempt():
    spec = find_by_name("stepfun")
    assert spec is not None and spec.reasoning_as_content
    r = _provider("stepfun")._settle_reply(LLMResponse(content=THOUGHT, reasoning_content=THOUGHT,
                                                       finish_reason="stop"))
    assert r.content == THOUGHT


def test_helper_shapes():
    assert not _is_reasoning_echo(None, THOUGHT)
    assert not _is_reasoning_echo("", THOUGHT)

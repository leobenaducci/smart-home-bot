"""A prompt too long for this model must reach the fallback, not the family.

Measured 2026-09-02. FreeToken served notification triage at a 16k ceiling
while this household's event prompts run 25.8k on average and 32.4k at the
largest, so every chore and geofence turn answered

    prompt is too long: 27852 tokens > 16384 maximum

and the family was shown that sentence as Alfred's reply. Raising the ceiling
fixed this house. The classification is what stops the next one being a silent
outage: a context that fits today stops fitting as a session grows, and the
remedy is never a retry -- the request is identical the second time -- but a
model with more room, which is exactly what the outage fallback is.
"""
import pytest

from nanobot.providers.base import LLMProvider, LLMResponse


def _err(text, status=None):
    return LLMResponse(content=text, finish_reason="error",
                       error_status_code=status)


@pytest.mark.parametrize("body", [
    # The literal FreeToken/vLLM shape this household was sent.
    "prompt is too long: 27852 tokens > 16384 maximum (prompt + generation)",
    # OpenAI's wording for the same thing.
    "This model's maximum context length is 8192 tokens. Please reduce the "
    "length of the messages.",
    "Error: context length exceeded",
])
def test_a_prompt_that_does_not_fit_is_model_specific(body):
    assert LLMProvider._is_model_specific_error(_err(body, 400)) is True


def test_it_does_not_need_a_status_code():
    # The status is not always carried through; the text identifies it.
    assert LLMProvider._is_model_specific_error(
        _err("prompt is too long: 30000 tokens > 16384 maximum")) is True


def test_an_ordinary_bad_request_is_not_swept_up():
    assert LLMProvider._is_model_specific_error(_err("Bad Request", 400)) is False


def test_a_revoked_key_is_still_not_model_specific():
    # The boundary this classifier already had: a bad key says the same thing
    # to every model, so swapping models buys nothing.
    assert LLMProvider._is_model_specific_error(
        _err("Incorrect API key provided", 401)) is False

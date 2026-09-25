"""A parameter the model refuses must reach the fallback, not the family.

Found in production on 2026-09-02. `reasoning_effort: "none"` is mandatory for
qwen3.6-35b-a3b and rejected by openai/gpt-oss-20b; the roles moved from one to
the other and the setting stayed. Together answered 400 "Input validation
error", nothing classed it as model-specific, so no fallback ran and the raw
error dict was delivered to the household as the assistant's reply.
"""
from nanobot.providers.base import LLMProvider, LLMResponse


def _err(text: str, status: int | None = None) -> LLMResponse:
    return LLMResponse(content=text, finish_reason="error",
                       error_status_code=status)


def test_a_rejected_parameter_is_model_specific():
    # The literal shape Together returns, dict repr and all.
    body = ("Error: {'message': 'Input validation error', "
            "'type': 'invalid_request_error', 'param': None, 'code': None}")
    assert LLMProvider._is_model_specific_error(_err(body, 400)) is True


def test_it_is_still_model_specific_without_a_status():
    # The status is not always carried through; the text is what identifies it.
    assert LLMProvider._is_model_specific_error(
        _err("Input validation error")) is True


def test_an_ordinary_bad_request_is_not_swept_up():
    # Only the measured shape. A 400 that says nothing recognisable stays
    # non-model-specific, because spending a fallback call on every malformed
    # request costs a request per turn and fixes nothing.
    assert LLMProvider._is_model_specific_error(
        _err("Bad Request", 400)) is False


def test_a_permanent_key_error_is_still_not_model_specific():
    # The boundary this class already had: a revoked key says the same thing
    # to every model, so swapping models buys nothing.
    assert LLMProvider._is_model_specific_error(
        _err("Incorrect API key provided", 401)) is False

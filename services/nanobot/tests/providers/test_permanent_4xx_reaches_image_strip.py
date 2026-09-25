"""A 400 is permanent, even when the gateway calls it "Upstream request failed".

This is the bug behind an error the family actually saw:

    Error from provider (Console Go): Upstream request failed:
    [invalid_request_error] Failed to deserialize the JSON body into the target
    type: messages[49]: unknown variant `image_url`, expected `text`

An image sent in an earlier turn stays in the session as a multimodal content
block. A later turn goes to the text model, which cannot parse it, and the whole
request is refused. `_run_with_retry` already has the fix — strip the images to
placeholders and try once more — but it only runs that path for errors it
considers *non*-transient.

And every failure from the opencode gateway, permanent ones included, is
prefixed "Upstream request failed", which is one of `_TRANSIENT_ERROR_MARKERS`.
So the gateway's own prose decided the question: a 400 was retried ten times
over 42 seconds and then handed to somebody's chat as a raw error dict, while
the one fallback that would have fixed it could never run.

A 4xx that is not 408/409/429 will say the same thing forever, so the status
code has to win over the text.
"""
from unittest.mock import patch

import pytest

from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.openai_compat_provider import OpenAICompatProvider

GATEWAY_400 = (
    "Error from provider (Console Go): Upstream request failed: "
    "[invalid_request_error] Failed to deserialize the JSON body into the target "
    "type: messages[49]: unknown variant `image_url`, expected `text` "
    "at line 1 column 149331"
)


def _err(content, status=None, kind=None):
    return LLMResponse(content=content, finish_reason="error",
                       error_status_code=status, error_kind=kind)


# --- the classifier ---------------------------------------------------------

def test_a_wrapped_400_is_permanent_despite_the_transient_wording():
    assert LLMProvider._is_transient_response(_err(GATEWAY_400, 400)) is False


def test_the_reasoning_content_400_too():
    """Same gateway, same wrapper, same wrong answer before this."""
    msg = ("Upstream request failed: [invalid_request_error] The `reasoning_content` "
           "in the thinking mode must be passed back to the API.")
    assert LLMProvider._is_transient_response(_err(msg, 400)) is False


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504])
def test_the_genuinely_retryable_ones_still_retry(status):
    assert LLMProvider._is_transient_response(_err("Upstream request failed", status)) is True


def test_a_transient_error_with_no_status_still_reads_as_transient():
    """Providers that report no status fall back to the text markers, as before."""
    assert LLMProvider._is_transient_response(_err("connection reset by peer")) is True


def test_an_explicit_should_retry_still_wins():
    r = _err(GATEWAY_400, 400)
    r.error_should_retry = True
    assert LLMProvider._is_transient_response(r) is True


# --- and therefore the fallback becomes reachable ----------------------------

@pytest.mark.asyncio
async def test_the_400_now_reaches_the_image_strip_and_the_turn_survives():
    """The point of the whole change: the retry-without-images path runs.

    Before, this exact message was classified transient, so `_run_with_retry`
    looped on it and never tried the one thing that works.
    """
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider()

    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "¿qué es esto?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"},
             "_meta": {"path": "media/foto.png"}},
        ]},
    ]

    seen = []

    async def fake_chat(**kw):
        seen.append(kw["messages"])
        has_image = any(
            isinstance(b, dict) and b.get("type") == "image_url"
            for m in kw["messages"] if isinstance(m.get("content"), list)
            for b in m["content"]
        )
        if has_image:
            return _err(GATEWAY_400, 400)
        return LLMResponse(content="Es una tira LED.", finish_reason="stop")

    provider.chat = fake_chat
    result = await provider.chat_with_retry(messages=messages, retry_mode="persistent")

    assert result.finish_reason == "stop"
    assert result.content == "Es una tira LED."
    # Exactly two calls: the one that failed, and the one with the image
    # replaced by a placeholder. Not ten identical ones.
    assert len(seen) == 2, f"expected one retry, got {len(seen)} calls"
    assert not any(
        isinstance(b, dict) and b.get("type") == "image_url"
        for m in seen[1] if isinstance(m.get("content"), list)
        for b in m["content"]
    )

"""The Responses API reports its cache under a different name, and it was dropped.

`/chat/completions` puts the count at `prompt_tokens_details.cached_tokens`;
`/responses` puts it at `input_tokens_details.cached_tokens`. This parser read
neither, so every model answering on /responses reported zero cached tokens --
indistinguishable from a cache that is switched off, and read that way for a
fortnight while three models carried 9.1M prompt tokens.
"""
from nanobot.providers.openai_responses.parsing import parse_response_output


def test_cached_tokens_come_out_of_input_tokens_details():
    resp = {
        "status": "completed",
        "output": [],
        "usage": {"input_tokens": 24598, "output_tokens": 120,
                  "total_tokens": 24718,
                  "input_tokens_details": {"cached_tokens": 21504}},
    }
    out = parse_response_output(resp)
    assert out.usage["prompt_tokens"] == 24598
    assert out.usage["cached_tokens"] == 21504


def test_a_response_with_no_cache_says_nothing_rather_than_zero():
    # Absent, not 0: a zero is a measurement and this is the lack of one.
    resp = {"status": "completed", "output": [],
            "usage": {"input_tokens": 900, "output_tokens": 10,
                      "total_tokens": 910}}
    assert "cached_tokens" not in parse_response_output(resp).usage

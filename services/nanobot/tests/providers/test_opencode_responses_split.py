"""OpenCode's gateway splits by model, and the two shapes are not swappable.

Measured across all 64 Zen models on 2026-09-03: six answer 200 on
`/v1/responses` and 500 on `/v1/chat/completions` -- gpt-5.6-luna, sol, terra,
gpt-5.4-mini, nano and muse-spark. `deepseek-v4-flash`, on the same key and the
same base, is the other way round.

nanobot posted `/chat/completions` to all of them, so six working models read as
broken and the household moved six roles off them. What is pinned here is that
the choice follows the *model*, and in particular that a reasoning effort cannot
drag deepseek onto an endpoint it does not serve.
"""

import pytest

from nanobot.providers.openai_compat_provider import OpenAICompatProvider

ZEN = "https://opencode.ai/zen/v1"


def wants_responses(model, base=ZEN, effort=None):
    p = OpenAICompatProvider(api_key="k", api_base=base, default_model=model)
    return p._should_use_responses_api(model, effort)


class TestOpenCodeSplitsByModel:
    @pytest.mark.parametrize("model", [
        "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
        "gpt-5.4-mini", "gpt-5.4-nano",
    ])
    def test_the_gpt5_family_goes_to_responses(self, model):
        assert wants_responses(model) is True

    @pytest.mark.parametrize("model", [
        "deepseek-v4-flash", "deepseek-v4-pro", "kimi-k2.7-code",
        "claude-sonnet-5", "gemini-3.8-flash",
    ])
    def test_everything_else_stays_on_chat_completions(self, model):
        assert wants_responses(model) is False

    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_a_reasoning_effort_does_not_move_deepseek(self, effort):
        """The regression this branch exists to avoid.

        Elsewhere an effort is reason enough to prefer /responses. Here it would
        route deepseek to an endpoint it does not serve, the moment a role asked
        it to think -- turning a working model into a broken one through a
        setting that has nothing to do with the endpoint.
        """
        assert wants_responses("deepseek-v4-flash", effort=effort) is False

    def test_and_still_moves_the_family(self):
        assert wants_responses("gpt-5.6-luna", effort="high") is True

    def test_none_effort_is_not_an_effort(self):
        assert wants_responses("deepseek-v4-flash", effort="none") is False


class TestNobodyElseChanged:
    def test_together_is_untouched(self):
        assert wants_responses("gpt-5-turbo", base="https://api.together.xyz/v1") is False

    def test_a_local_ollama_is_untouched(self):
        assert wants_responses("gpt-5-whatever", base="http://127.0.0.1:11434/v1") is False

    def test_direct_openai_still_uses_it(self):
        assert wants_responses("gpt-5.1", base="https://api.openai.com/v1") is True

    def test_and_still_honours_an_effort_there(self):
        """Off OpenCode the old rule stands: an effort is reason enough."""
        assert wants_responses("some-model", base="https://api.openai.com/v1",
                               effort="high") is True


class TestTheCircuitBreakerStillGuards:
    def test_repeated_failures_stop_it_trying(self):
        p = OpenAICompatProvider(api_key="k", api_base=ZEN,
                                 default_model="gpt-5.6-luna")
        assert p._should_use_responses_api("gpt-5.6-luna", None) is True
        for _ in range(5):
            p._record_responses_failure("gpt-5.6-luna", None)
        assert p._should_use_responses_api("gpt-5.6-luna", None) is False

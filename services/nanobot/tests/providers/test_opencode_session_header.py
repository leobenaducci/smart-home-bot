"""OpenCode requires x-opencode-session, and only OpenCode gets it.

OpenCode's mail of 2026-09-03: requests missing this header "may error" from
09/06. One of the callers it named by user-agent was the Python OpenAI client,
which is this provider.

Two things are pinned. That the header goes out on OpenCode's gateway -- an
assistant that stops answering because a header is missing looks exactly like
an outage, and this house has already spent a session chasing one of those. And
that it does *not* go out anywhere else: a session id is meaningless to
Together or Ollama, and quietly tagging every provider with an OpenCode header
is the kind of thing that is read as a bug years later.
"""

import pytest

from nanobot.providers.openai_compat_provider import (
    OpenAICompatProvider,
    _uses_opencode,
)

ZEN = "https://opencode.ai/zen/v1"


def headers_for(api_base):
    return OpenAICompatProvider(
        api_key="k", api_base=api_base, default_model="m",
    )._client.default_headers


def test_opencode_gets_the_session_header():
    sent = headers_for(ZEN)
    assert "x-opencode-session" in sent


def test_the_session_matches_the_affinity_id():
    """One ongoing thing, one id, whatever the gateway chooses to call it."""
    sent = headers_for(ZEN)
    assert sent["x-opencode-session"] == sent["x-session-affinity"]


def test_the_id_is_stable_for_the_life_of_the_client():
    provider = OpenAICompatProvider(api_key="k", api_base=ZEN, default_model="m")
    first = provider._client.default_headers["x-opencode-session"]
    assert provider._client.default_headers["x-opencode-session"] == first


def test_two_assistants_do_not_share_an_id():
    assert (headers_for(ZEN)["x-opencode-session"]
            != headers_for(ZEN)["x-opencode-session"])


@pytest.mark.parametrize("base", [
    "https://api.together.xyz/v1",
    "http://127.0.0.1:11434/v1",
    "https://openrouter.ai/api/v1",
    "https://api.openai.com/v1",
])
def test_nobody_else_is_tagged_with_it(base):
    assert "x-opencode-session" not in headers_for(base)


def test_everyone_still_gets_the_affinity_header():
    """The header that was already there must not have been displaced."""
    assert "x-session-affinity" in headers_for("https://api.together.xyz/v1")


@pytest.mark.parametrize("base,expected", [
    (ZEN, True),
    ("https://opencode.ai/zen/go/v1", True),   # not ours to send to, still theirs
    ("https://OpenCode.ai/zen/v1", True),      # host match is case-insensitive
    ("https://api.together.xyz/v1", False),
    (None, False),
    ("", False),
])
def test_which_bases_count_as_opencode(base, expected):
    assert _uses_opencode(None, base) is expected


def test_a_named_provider_counts_even_without_a_base():
    class Spec:
        name = "opencode"
        env_key = None
        default_api_base = None
    assert _uses_opencode(Spec(), None) is True


def test_the_responses_body_carries_a_prompt_cache_key():
    """Caching is on by default and still misses without it.

    A gateway spreads requests across machines, and only the ones that land on
    the same machine find a warm prefix. OpenAI's guide says the key is what
    makes matching reliable; the same symptom -- zero cache hits over a
    32-hour session -- is filed against OpenCode itself.
    """
    provider = OpenAICompatProvider(
        api_key="k", api_base=ZEN, default_model="gpt-5.6-luna")
    body = provider._build_responses_body(
        [{"role": "user", "content": "hola"}], None, "gpt-5.6-luna", 256, 0.7,
        None, None)
    assert body["prompt_cache_key"] == provider._session_id
    # The same value the headers carry: one conversation groups together, and
    # two households never share a key.
    assert body["prompt_cache_key"] == \
        provider._client.default_headers["x-opencode-session"]


def test_the_session_id_survives_a_restart(monkeypatch):
    """It is the prompt_cache_key, so a new one is a cold cache for everybody.

    It was `uuid4()`, which is stable for the life of a provider instance and
    therefore new on every deploy -- so deploying the assistants cost a
    full-price first turn on every conversation, on top of whatever the deploy
    was actually for.
    """
    monkeypatch.setenv("NANOBOT_INSTANCE", "user1")
    first = OpenAICompatProvider(
        api_key="k", api_base=ZEN, default_model="m")._session_id
    second = OpenAICompatProvider(
        api_key="k", api_base=ZEN, default_model="m")._session_id
    assert first == second


def test_two_members_never_share_a_cache_key(monkeypatch):
    # A shared key would put two households' prompts in one cache namespace.
    monkeypatch.setenv("NANOBOT_INSTANCE", "user1")
    a = OpenAICompatProvider(api_key="k", api_base=ZEN, default_model="m")._session_id
    monkeypatch.setenv("NANOBOT_INSTANCE", "user2")
    b = OpenAICompatProvider(api_key="k", api_base=ZEN, default_model="m")._session_id
    assert a != b


def test_two_endpoints_never_share_a_cache_key(monkeypatch):
    # A key that spans providers claims two unrelated prefixes are the same.
    monkeypatch.setenv("NANOBOT_INSTANCE", "user1")
    a = OpenAICompatProvider(api_key="k", api_base=ZEN, default_model="m")._session_id
    b = OpenAICompatProvider(api_key="k", api_base="https://api.together.xyz/v1",
                             default_model="m")._session_id
    assert a != b


def test_the_member_id_does_not_travel_in_the_clear(monkeypatch):
    # It reaches a third party on every request, and a member id is one of the
    # three ids CLAUDE.md says not to leak where it does not belong.
    monkeypatch.setenv("NANOBOT_INSTANCE", "user1")
    sent = OpenAICompatProvider(
        api_key="k", api_base=ZEN, default_model="m")._session_id
    assert "user1" not in sent


def test_no_instance_name_falls_back_to_a_random_id(monkeypatch):
    # Unique is merely useless; shared by accident is wrong.
    monkeypatch.delenv("NANOBOT_INSTANCE", raising=False)
    a = OpenAICompatProvider(api_key="k", api_base=ZEN, default_model="m")._session_id
    b = OpenAICompatProvider(api_key="k", api_base=ZEN, default_model="m")._session_id
    assert a != b


def test_the_cache_key_does_not_go_to_endpoints_that_never_asked_for_it():
    """`prompt_cache_key` is OpenAI's field, not a universal one.

    The session *header* was always gated on the host, with a rule in
    CLAUDE.md about it. The body field was not, and it is the riskier of the
    two: a self-hosted vLLM or SGLang Responses endpoint may refuse an
    unknown body field outright, so a caching hint that does nothing for
    those providers could refuse the request instead.
    """
    provider = OpenAICompatProvider(
        api_key="k", api_base="https://api.together.xyz/v1", default_model="m")
    body = provider._build_responses_body(
        [{"role": "user", "content": "hola"}], None, "m", 256, 0.7, None, None)
    assert "prompt_cache_key" not in body


def test_but_it_still_goes_to_the_two_that_do():
    for base in (ZEN, "https://api.openai.com/v1"):
        provider = OpenAICompatProvider(
            api_key="k", api_base=base, default_model="gpt-5.6-luna")
        body = provider._build_responses_body(
            [{"role": "user", "content": "hola"}], None, "gpt-5.6-luna",
            256, 0.7, None, None)
        assert body.get("prompt_cache_key"), base


def test_a_local_responses_server_is_not_openai():
    provider = OpenAICompatProvider(
        api_key="k", api_base="http://127.0.0.1:8000/v1", default_model="m")
    body = provider._build_responses_body(
        [{"role": "user", "content": "hola"}], None, "m", 256, 0.7, None, None)
    assert "prompt_cache_key" not in body

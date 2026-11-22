"""household A12: a provider session id and prompt_cache_key that survive a restart."""

from __future__ import annotations

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import find_by_name


def _provider(base=None, model="gpt-5", spec="openai"):
    return OpenAICompatProvider(api_key="k", api_base=base, default_model=model, spec=find_by_name(spec))


def _body(provider):
    return provider._build_responses_body(
        messages=[{"role": "user", "content": "hola"}], tools=None, model=None, max_tokens=100,
        temperature=0.1, reasoning_effort=None, tool_choice=None)


def test_the_id_is_the_same_after_a_restart_and_differs_per_member(monkeypatch):
    monkeypatch.setenv("NANOBOT_INSTANCE", "tomi")
    first, again = _provider(), _provider()
    assert first._session_id == again._session_id
    assert first._default_headers["x-session-affinity"] == first._session_id
    assert "tomi" not in first._session_id                         # hashed, not in the clear
    monkeypatch.setenv("NANOBOT_INSTANCE", "mora")
    assert _provider()._session_id != first._session_id
    assert _provider(model="gpt-5-mini")._session_id != _provider()._session_id


def test_without_an_instance_name_it_is_random(monkeypatch):
    monkeypatch.delenv("NANOBOT_INSTANCE", raising=False)
    assert _provider()._session_id != _provider()._session_id


def test_openai_gets_the_cache_key_and_a_local_server_does_not(monkeypatch):
    monkeypatch.setenv("NANOBOT_INSTANCE", "tomi")
    openai = _provider()
    assert _body(openai)["prompt_cache_key"] == openai._session_id
    local = _provider(base="http://gpu.test:8000/v1", model="qwen3.5:9b", spec="vllm")
    assert "prompt_cache_key" not in _body(local)


def test_opencode_gets_it_on_the_session_header_too(monkeypatch):
    monkeypatch.setenv("NANOBOT_INSTANCE", "tomi")
    zen = _provider(base="https://opencode.ai/zen/v1", model="kimi-k3", spec="opencode")
    assert zen._default_headers["x-opencode-session"] == zen._session_id
    assert _body(zen)["prompt_cache_key"] == zen._session_id

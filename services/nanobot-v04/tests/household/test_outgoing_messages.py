"""household: the fork's rules for assistant messages a chat request carries.

The household's everyday and plan routes are DeepSeek and GLM-5.3 on NanoGPT:
a spec with no thinking style and no reasoning effort set, which is exactly
where upstream adds neither rule.
"""

from __future__ import annotations

import pytest

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import find_by_name

CALL = {"id": "call_1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}


def _sent(messages):
    provider = OpenAICompatProvider(api_key="k", api_base="https://nano-gpt.com/api/v1",
                                    default_model="deepseek/deepseek-v4.1-flash",
                                    spec=find_by_name("nanogpt"))
    kwargs = provider._build_kwargs(messages=messages, tools=None, model=None, max_tokens=64,
                                    temperature=0.1, reasoning_effort=None, tool_choice=None)
    return [m for m in kwargs["messages"] if m["role"] == "assistant"]


HISTORY = [
    {"role": "user", "content": "Is Luz Paula on?"},
    {"role": "assistant", "content": None, "tool_calls": [CALL], "reasoning_content": "check it"},
    {"role": "tool", "tool_call_id": "call_1", "content": "on"},
    {"role": "assistant", "content": "Luz Paula is on."},
    {"role": "user", "content": "Turn it off."},
]


def test_tool_calls_go_with_empty_content_and_thinking_mode_is_kept(monkeypatch):
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    first, second = _sent([dict(m) for m in HISTORY])
    assert first["content"] == "" and first["tool_calls"]
    assert second["reasoning_content"] == ""
    assert HISTORY[1]["content"] is None and "reasoning_content" not in HISTORY[3]  # caller's copy untouched


def test_without_the_household_upstream_is_unchanged(monkeypatch):
    monkeypatch.delenv("NANOBOT_HOUSEHOLD", raising=False)
    monkeypatch.setattr("nanobot.household.runtime.RUNTIME.loop", None)
    first, second = _sent([dict(m) for m in HISTORY])
    assert first["content"] is None
    assert "reasoning_content" not in second


def test_earlier_turns_reasoning_is_emptied_and_the_current_turn_keeps_its(monkeypatch):
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    current = [{"role": "assistant", "content": None, "reasoning_content": "which bulb?",
                "tool_calls": [dict(CALL, id="call_2")]},
               {"role": "tool", "tool_call_id": "call_2", "content": "off"}]
    sent = _sent([dict(m) for m in HISTORY] + current)
    earlier, _, now = sent
    assert earlier["reasoning_content"] == ""             # key kept, text gone
    assert now["reasoning_content"] == "which bulb?"


@pytest.mark.parametrize("spec_name,sent", [("ollama_text", "none"), ("ollama_cloud", "none"), ("nanogpt", None)])
def test_effort_none_reaches_ollama_and_only_ollama(monkeypatch, spec_name, sent):
    """The classifier, heartbeat and event profiles run gemma on Ollama with
    effort "none"; dropped, the model thinks through its whole 80-token budget
    and the classifier reads an empty answer."""
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    from types import SimpleNamespace

    from nanobot.household.provider_specs import household_dynamic_spec

    cfg = SimpleNamespace(api_base="http://host.docker.internal:11437/v1")
    spec = find_by_name(spec_name) or household_dynamic_spec(spec_name, cfg)
    provider = OpenAICompatProvider(api_key="k", api_base=cfg.api_base,
                                    default_model="gemma4:e4b", spec=spec)
    kwargs = provider._build_kwargs(messages=[{"role": "user", "content": "hi"}], tools=None, model=None,
                                    max_tokens=80, temperature=0.0, reasoning_effort="none", tool_choice=None)
    assert kwargs.get("reasoning_effort") == sent

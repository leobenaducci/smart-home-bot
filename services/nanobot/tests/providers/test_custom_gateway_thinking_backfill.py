"""A thinking model reached through a gateway the registry does not know.

`provider: "custom"` is nothing but a base URL, so its ProviderSpec carries no
`thinking_style` and nanobot has no way to know from configuration that the
model behind it thinks. This house runs `deepseek-v4-flash` through opencode's
zen endpoint exactly like that — and DeepSeek enforces the rule regardless:

    400 invalid_request_error — "The `reasoning_content` in the thinking mode
    must be passed back to the API."

So the *history* is what says the mode is on. If any assistant message came
back carrying the field, every assistant message in that conversation needs it
— including the ones this process wrote itself (a subagent result, a crash
placeholder), which never had any thinking behind them.

Observed on 2026-08-13: a subagent finished in Alex's finanzas chat, its result
was injected as an assistant message with no `reasoning_content`, and the turn
that was supposed to relay it died with the raw provider error.
"""

from __future__ import annotations

from unittest.mock import patch

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import find_by_name


def _kwargs_for(messages: list[dict]) -> dict:
    """Build request kwargs the way the house is configured: the `custom`
    spec, no reasoning_effort, a model name the registry knows nothing about."""
    spec = find_by_name("custom")
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider(
            api_key="k", default_model="deepseek-v4-flash", spec=spec
        )
    return provider._build_kwargs(
        messages=messages,
        tools=None,
        model="deepseek-v4-flash",
        max_tokens=1024,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    )


def _assistants(kwargs: dict) -> list[dict]:
    return [m for m in kwargs["messages"] if m.get("role") == "assistant"]


def test_injected_assistant_message_is_backfilled() -> None:
    """The reported failure: one real turn, then a message we wrote ourselves."""
    kwargs = _kwargs_for([
        {"role": "user", "content": "renombra la cartola"},
        {"role": "assistant", "content": "", "reasoning_content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "task", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        # The subagent's result, injected by agent/loop — no thinking behind it.
        {"role": "assistant", "content": "Listo: 2026-08_Bci_1813_Nac.pdf"},
    ])

    assert all("reasoning_content" in m for m in _assistants(kwargs))


def test_presence_not_truthiness_is_what_proves_the_mode() -> None:
    """The field is legitimately "" on turns where nothing was thought, and
    those are exactly the ones that show thinking mode is on. Reading it as a
    boolean would miss every one of them — which is the whole reported case."""
    kwargs = _kwargs_for([
        {"role": "assistant", "content": "hola", "reasoning_content": ""},
        {"role": "assistant", "content": "resultado del subagente"},
    ])

    assert all("reasoning_content" in m for m in _assistants(kwargs))


def test_a_conversation_that_never_thought_is_left_alone() -> None:
    """No evidence of thinking mode, so nothing is invented: a provider that
    does not expect the field must not start receiving it."""
    kwargs = _kwargs_for([
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "hola, ¿en qué te ayudo?"},
    ])

    assert all("reasoning_content" not in m for m in _assistants(kwargs))


def test_real_thinking_is_never_overwritten() -> None:
    """Backfilling fills holes; it does not flatten what the model actually
    thought. (The shapes here are what survives sanitising: consecutive
    assistant messages are merged and trailing ones are popped, so the pair has
    to be separated by a tool result and followed by a user message.)"""
    kwargs = _kwargs_for([
        {"role": "user", "content": "renombra la cartola"},
        {"role": "assistant", "content": "a", "reasoning_content": "pensé esto",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "exec", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "resultado del subagente"},
        {"role": "user", "content": "gracias"},
    ])

    assert _assistants(kwargs)[0]["reasoning_content"] == "pensé esto"
    assert _assistants(kwargs)[1]["reasoning_content"] == ""

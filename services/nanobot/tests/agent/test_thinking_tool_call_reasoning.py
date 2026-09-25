"""A tool call from a thinking model must carry `reasoning_content`.

DeepSeek-style thinking models (this house runs `deepseek-v4-flash` through the
opencode gateway) reject the *next* request outright when the assistant
message that asked for a tool arrives without a `reasoning_content` field:

    400 invalid_request_error — "The `reasoning_content` in the thinking mode
    must be passed back to the API."

Verified against the live gateway on 2026-08-12: absent → 400, ``None`` → 400,
``""`` → 200, prose → 200. Only the last assistant message is checked.

I concluded from that last sentence that *dropping* the field from older turns
was therefore safe, and `ContextBuilder._strip_stale_reasoning` deleted it. It
is not safe, and on 2026-08-13 it cost a turn: which message ends up last is
not fixed. A subagent follow-up is injected as an assistant message and the
provider pops trailing assistant messages before sending, so an older tool
call — just stripped — became the last one. `_strip_stale_reasoning` now empties
the value and keeps the key, which serves the same purpose (the deliberation is
what costs tokens, not the field) without depending on message order at all.
See `test_subagent_followup_reasoning.py`.

The failure needs a step that produces tool calls and no thinking to reach it:
a short follow-up call, or one the text parser lifted out of plain prose, which
carries no reasoning by construction. The turn then dies mid-tool-loop, and
with `providerRetryMode: persistent` the identical request is retried ten times
before the raw provider error is handed to whoever was waiting for an answer.
"""

from __future__ import annotations

from nanobot.utils.helpers import build_assistant_message


TOOL_CALLS = [
    {
        "id": "c1",
        "type": "function",
        "function": {"name": "exec", "arguments": '{"command": "ls"}'},
    }
]


def test_tool_call_without_thinking_still_carries_the_field() -> None:
    msg = build_assistant_message("", tool_calls=TOOL_CALLS)

    assert msg["reasoning_content"] == ""


def test_explicit_thinking_is_kept_verbatim() -> None:
    msg = build_assistant_message("", tool_calls=TOOL_CALLS, reasoning_content="pensando")

    assert msg["reasoning_content"] == "pensando"


def test_a_message_with_no_tool_calls_is_left_alone() -> None:
    """Plain prose has nothing to round-trip, and the API asks for nothing."""
    msg = build_assistant_message("Son las 20:41.")

    assert "reasoning_content" not in msg


def test_the_field_is_not_confused_with_content() -> None:
    """Empty thinking must not swallow the model's actual words."""
    msg = build_assistant_message("Voy a mirar.", tool_calls=TOOL_CALLS)

    assert msg["content"] == "Voy a mirar."
    assert msg["reasoning_content"] == ""
    assert msg["tool_calls"] == TOOL_CALLS

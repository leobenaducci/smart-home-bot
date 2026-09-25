"""A subagent's result must not cost the turn that relays it.

Reported on 2026-08-13 from Alex's finanzas chat: a subagent finished renaming a
statement, its result was injected into the session, and the turn that was
supposed to phrase it died with the raw provider error in the chat —

    400 invalid_request_error — "The `reasoning_content` in the thinking mode
    must be passed back to the API."

Three pieces had to line up, which is why nothing caught it before:

1. The subagent's result is injected as an **assistant** message (this process
   wrote it, so nothing thought about it).
2. `_strip_stale_reasoning` removed `reasoning_content` from every message in
   history, the previous turn's tool call included.
3. The provider **pops trailing assistant messages** before sending, so the
   injected message goes and the previous turn's tool call — just stripped —
   becomes the last assistant message in the request.

Each step is reasonable alone. Together they hand a thinking model exactly the
message shape it refuses. The test walks the real path rather than any one of
those functions, because every individual piece looked correct.
"""

from __future__ import annotations

from unittest.mock import patch

from nanobot.agent.context import ContextBuilder
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import find_by_name

# The session as it was on disk when this failed: a question, the assistant
# calling the subagent, the tool result, and the injected answer.
HISTORY = [
    {"role": "user", "content": "renombra la cartola y guárdala"},
    {"role": "assistant", "content": "",
     "reasoning_content": "el usuario quiere renombrar…",
     "tool_calls": [{"id": "c1", "type": "function",
                     "function": {"name": "task", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "task", "content": "lanzado"},
    {"role": "assistant", "content": "Listo: 2026-08_Bci_1813_Nac.pdf",
     "injected_event": "subagent_result", "subagent_task_id": "sub-1"},
]


def _request_messages(history: list[dict], workspace) -> list[dict]:
    """History → the message list the provider would actually send."""
    builder = ContextBuilder(workspace=workspace, timezone="Etc/UTC")
    messages = builder.build_messages(
        history=history,
        current_message="Listo: 2026-08_Bci_1813_Nac.pdf",
        # A subagent follow-up arrives with no user message at all — this is
        # the "assistant" that puts a stripped older message last.
        current_role="assistant",
        channel="websocket",
        chat_id="fin",
    )
    spec = find_by_name("custom")
    with patch("nanobot.providers.openai_compat_provider.AsyncOpenAI"):
        provider = OpenAICompatProvider(
            api_key="k", default_model="deepseek-v4-flash", spec=spec
        )
    return provider._build_kwargs(
        messages=messages, tools=None, model="deepseek-v4-flash",
        max_tokens=1024, temperature=0.7, reasoning_effort=None, tool_choice=None,
    )["messages"]


def test_last_assistant_message_still_carries_the_field(tmp_path) -> None:
    """The one the API actually checks."""
    assistants = [m for m in _request_messages(HISTORY, tmp_path) if m.get("role") == "assistant"]

    assert assistants, "the request has no assistant message to check at all"
    assert "reasoning_content" in assistants[-1]


def test_every_assistant_message_carries_the_field(tmp_path) -> None:
    """Stronger than the API's rule on purpose: which message ends up last
    depends on trailing-assistant popping and on merging, so 'the last one is
    fine' is a property that holds by luck. Every one of them holding it is a
    property that survives the next change to either."""
    assistants = [m for m in _request_messages(HISTORY, tmp_path) if m.get("role") == "assistant"]

    assert all("reasoning_content" in m for m in assistants)


def test_the_old_deliberation_is_not_re_sent() -> None:
    """The field stays, its content does not — that is the whole point of
    stripping it. A previous turn's thinking is scratch work, and re-sending it
    is billed as input on every later message."""
    stripped = ContextBuilder._strip_stale_reasoning(HISTORY)

    assert stripped[1]["reasoning_content"] == ""
    assert "el usuario quiere renombrar" not in str(stripped)


def test_history_is_not_mutated_in_place() -> None:
    """The list belongs to the live Session; emptying its thinking for one
    request must not empty it in the session that gets saved back."""
    ContextBuilder._strip_stale_reasoning(HISTORY)

    assert HISTORY[1]["reasoning_content"] == "el usuario quiere renombrar…"

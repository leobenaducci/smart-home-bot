"""An assistant message carrying tool_calls sends "" for content, never null.

Found in production 2026-09-02. Household event turns send a message on nearly
every turn, so nearly every one of them made a tool call -- and it was the
*follow-up* request, the one carrying the tool result, that Together refused
with 400 "Input validation error". The family was shown the raw error dict.

Measured across every model this house runs (deepseek-v4-flash and -pro on both
Zen and Together, zai-org/GLM-5.3, openai/gpt-oss-20b): "" is accepted by all
six, None by four. gpt-oss-20b answers 400 for a null and GLM-5.3 does not
answer with valid JSON at all.

The original reason for emptying it stands -- some gateways reject an assistant
message that mixes *non-empty* content with tool_calls -- and "" satisfies that
just as well as None did.
"""
from nanobot.providers.openai_compat_provider import OpenAICompatProvider

TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "message", "arguments": '{"content": "hi"}'},
}


def _sanitize(messages):
    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    return provider._sanitize_messages(messages)


def test_content_is_emptied_to_a_string_not_none():
    out = _sanitize([
        {"role": "user", "content": "say hi"},
        {"role": "assistant", "content": "I will send it", "tool_calls": [TOOL_CALL]},
        {"role": "tool", "tool_call_id": "call_1", "content": "sent"},
    ])
    assistant = [m for m in out if m.get("role") == "assistant"][0]
    assert assistant["content"] == ""
    assert assistant["content"] is not None
    # The point of emptying it at all: never *non-empty* beside tool_calls.
    assert not assistant["content"]
    assert assistant["tool_calls"]


def test_a_null_from_upstream_is_also_normalised():
    out = _sanitize([
        {"role": "user", "content": "say hi"},
        {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]},
        {"role": "tool", "tool_call_id": "call_1", "content": "sent"},
    ])
    assistant = [m for m in out if m.get("role") == "assistant"][0]
    assert assistant["content"] == ""


def test_an_assistant_message_without_tool_calls_keeps_its_words():
    # A user turn after it, because `_enforce_role_alternation` drops a
    # trailing assistant message -- the request has to end on something the
    # model is answering.
    out = _sanitize([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hello back"},
        {"role": "user", "content": "and again"},
    ])
    assistant = [m for m in out if m.get("role") == "assistant"][0]
    assert assistant["content"] == "hello back"

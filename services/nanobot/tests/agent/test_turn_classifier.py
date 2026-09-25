"""The turn classifier: a hint before the turn, never a blocker.

A small local model names a request chat / action / complex. Everything in
here is about the ways that can go wrong without costing the household a
turn: an unparseable answer, a slow one, a dead model -- each lands on
`action` (the cheap tier with escalation armed) and says so in `source`.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.classify import (
    TurnClass,
    TurnClassifier,
    continuation_messages,
    parse_label,
    previous_assistant_text,
)
from nanobot.providers.base import LLMResponse


def _classifier(answer: str | None = None, *, delay: float = 0.0, raise_: bool = False,
                timeout_s: float = 1.5) -> TurnClassifier:
    provider = MagicMock()

    async def chat(**kwargs):
        if delay:
            await asyncio.sleep(delay)
        if raise_:
            raise RuntimeError("ollama is down")
        return LLMResponse(content=answer)

    provider.chat = chat
    return TurnClassifier(provider, "tiny-model", timeout_s=timeout_s, long_message_chars=100)


# --- the answer, in the shapes a small model actually writes ------------------

@pytest.mark.parametrize("text, want", [
    ('{"label": "complex", "reason": "3 systems"}', ("complex", "3 systems")),
    ('Sure!\n```json\n{"label":"action","reason":"one light"}\n```', ("action", "one light")),
    ('{"label": "CHAT"}', ("chat", "")),
    ("complex", ("complex", "")),
    ('"action"', ("action", "")),
    ('{"label": "banana"}', None),
    ("I think this is hard", None),
    ("", None),
    (None, None),
])
def test_parse_label_is_tolerant_but_not_credulous(text, want):
    assert parse_label(text) == want


# --- fast paths: no model asked ---------------------------------------------

@pytest.mark.asyncio
async def test_an_attachment_is_complex_without_asking():
    c = _classifier('{"label": "chat"}')
    r = await c.classify("mirá esto", attachments=1)
    assert (r.label, r.tier, r.source) == ("complex", "powerful", "fast_path")


@pytest.mark.asyncio
async def test_a_long_message_is_long_work_without_asking():
    c = _classifier('{"label": "chat"}')
    r = await c.classify("x" * 101)
    # A long message is usually work handed over, not a question to answer
    # fast: it goes to a sub-agent (2026-09-24).
    assert (r.label, r.tier, r.source) == ("long", "everyday", "fast_path")


@pytest.mark.asyncio
async def test_an_empty_message_is_chat():
    c = _classifier('{"label": "complex"}')
    r = await c.classify("   ")
    assert (r.label, r.tier, r.source) == ("chat", "everyday", "fast_path")


# --- the model's word ---------------------------------------------------------

@pytest.mark.asyncio
async def test_the_label_picks_the_tier():
    assert (await _classifier('{"label": "complex", "reason": "verify across wiz and HA"}')
            .classify("buscá las luces azules y verificá el nombre en wiz y HA")).tier == "powerful"
    assert (await _classifier('{"label": "action"}').classify("prendé la luz")).tier == "everyday"
    assert (await _classifier('{"label": "chat"}').classify("gracias")).tier == "everyday"
    r = await _classifier('{"label": "complex"}').classify("investigá")
    assert r.source == "model" and r.ms >= 0


@pytest.mark.asyncio
async def test_the_previous_turn_reaches_the_prompt():
    seen = {}
    provider = MagicMock()

    async def chat(**kwargs):
        seen["prompt"] = kwargs["messages"][0]["content"]
        return LLMResponse(content='{"label": "complex"}')

    provider.chat = chat
    c = TurnClassifier(provider, "tiny-model")
    await c.classify("sí, dale", previous="Puedo revisar las tres cámaras y compararlas, ¿querés?")
    assert "compararlas" in seen["prompt"]
    assert "sí, dale" in seen["prompt"]


# --- and the ways it fails, all of which cost nothing -------------------------

@pytest.mark.asyncio
async def test_a_slow_classifier_defaults_to_action():
    c = _classifier('{"label": "complex"}', delay=0.5, timeout_s=0.2)
    r = await c.classify("hola")
    assert (r.label, r.tier, r.source) == ("action", "everyday", "default")
    assert "timeout" in r.reason


@pytest.mark.asyncio
async def test_a_dead_classifier_defaults_to_action():
    r = await _classifier(raise_=True).classify("hola")
    assert (r.label, r.tier, r.source) == ("action", "everyday", "default")


@pytest.mark.asyncio
async def test_an_unparseable_answer_defaults_to_action():
    r = await _classifier("hmm, not sure").classify("hola")
    assert (r.label, r.tier, r.source) == ("action", "everyday", "default")


def test_the_record_is_what_the_usage_report_carries():
    r = TurnClass("action", "everyday", "one light", "model", 12.34, escalated_from="bad_invocation")
    rec = r.as_record()
    assert rec == {"tier": "everyday", "label": "action", "source": "model",
                   "classifier_ms": 12.3, "escalated": True, "escalated_from": "bad_invocation"}


# --- handing a failed attempt to the strong model -----------------------------

def test_previous_assistant_text_reads_plain_and_block_content():
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": [{"type": "text", "text": "first"}]},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": "second"},
    ]
    assert previous_assistant_text(msgs) == "second"
    assert previous_assistant_text(msgs[:3]) == "first"
    assert previous_assistant_text([]) == ""


def test_nothing_ran_means_a_clean_retry():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "prendé la luz"},
            {"role": "assistant", "content": "[Escribí una invocación que no se pudo interpretar]"}]
    out = continuation_messages(msgs, "bad_invocation", tools_ran=False)
    assert out == msgs[:2]


def test_tools_ran_means_continue_with_a_note_and_no_replay():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "agregá leche"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "added"},
            {"role": "assistant", "content": "[loop cut]"}]
    out = continuation_messages(msgs, "repeated_tool_calls", tools_ran=True)
    assert out[:4] == msgs[:4]                      # the tool result stays: no second `add`
    assert out[-1]["role"] == "user" and "repeated_tool_calls" in out[-1]["content"]
    assert "Do not repeat" in out[-1]["content"]

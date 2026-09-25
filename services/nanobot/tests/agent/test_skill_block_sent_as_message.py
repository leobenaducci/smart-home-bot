"""A skill block sent to the chat instead of written as the reply.

11 Sep 2026, model benchmark, Qwen3.6-35B-A3B at IQ2_XXS, "¿qué tareas tengo
pendientes para hoy?". It wrote ``{"skill": "tasks", "action": "list_chores"}``
-- exactly the block the skill asks for -- as the text of a ``message`` call,
three times, and ran out of turns. The Q3 build of the same model did the same
for the weather, twice. The sibling of the ``exec("echo ...")`` case in
test_skill_block_echoed_through_exec.py: the block was right, the envelope
was wrong.
"""

from __future__ import annotations

import pytest

from nanobot.agent.runner import _lift_echoed_skill_blocks, _messaged_skill_block
from nanobot.providers.base import LLMResponse, ToolCallRequest

from tests.agent.test_noop_call_beside_skill_block import _parse


def _message(content: str, id_: str = "c1", **extra) -> ToolCallRequest:
    return ToolCallRequest(id=id_, name="message", arguments={"content": content, **extra})


CAPTURED = '{"skill": "tasks", "action": "list_chores"}'


def test_the_captured_turn_runs_the_skill():
    out, _ = _parse(LLMResponse(content="", tool_calls=[_message(CAPTURED)],
                                finish_reason="tool_calls"))
    assert out.has_tool_calls, "the block was lost again"
    assert not any(tc.name == "message" for tc in out.tool_calls), \
        "the block still goes to the chat"
    # `__skill_translate` runs the skill; `read_file` reads its SKILL.md first.
    assert out.tool_calls[0].name in ("__skill_translate", "read_file"), out.tool_calls


@pytest.mark.parametrize("content", [
    CAPTURED,
    '{"skill": "tasks", "action": "list_chores", "user": "Tomi"}',
    '  {"skill":"weather","action":"get_forecast","days":2}\n',
    '```json\n{"skill": "tasks", "action": "list_chores"}\n```',
    '```\n{"skill": "tasks", "action": "list_chores"}\n```',
])
def test_every_way_of_sending_the_bare_block_is_recognised(content):
    block = _messaged_skill_block(_message(content))
    assert block is not None, content
    assert '"skill"' in block


@pytest.mark.parametrize("content", [
    'Ya te las busco: {"skill": "tasks", "action": "list_chores"}',  # a sentence around it
    '{"name": "Tomi"}',                                                # JSON, not a skill block
    "Hola, ¿cómo estás?",
    "",
    '{"skill": "tasks", "action": "list_chores"}\n{"skill": "grocery", "action": "list"}',
])
def test_a_message_somebody_meant_to_send_is_left_to_go(content):
    assert _messaged_skill_block(_message(content)) is None, content


def test_a_real_message_beside_it_is_kept():
    real = _message("Ahora te digo.", id_="c2")
    out = _lift_echoed_skill_blocks(
        LLMResponse(content="", tool_calls=[_message(CAPTURED), real],
                    finish_reason="tool_calls"), "s")
    assert [tc.id for tc in out.tool_calls] == ["c2"]
    assert "list_chores" in out.content
    assert out.finish_reason == "tool_calls"


def test_the_block_is_never_shown_to_the_user():
    out, _ = _parse(LLMResponse(content="Ya te las busco.",
                                tool_calls=[_message(CAPTURED)],
                                finish_reason="tool_calls"))
    assert "Ya te las busco." in (out.content or "")
    assert '"skill"' not in (out.content or "")


def test_other_tools_are_never_touched():
    call = ToolCallRequest(id="c1", name="write_file",
                           arguments={"content": CAPTURED, "path": "/tmp/x.json"})
    assert _messaged_skill_block(call) is None

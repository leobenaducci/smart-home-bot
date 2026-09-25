"""A skill block sent through the shell instead of written as the reply.

10 Sep 2026, ornith-1.5:9b, "¿qué tareas tengo pendientes para hoy?". The
model wrote exactly the block the tasks skill asks for and then wrapped it in
``exec("echo ...")``, because a tool call is what it was trained to end a turn
with. The shell printed it back, nothing ran, and the model decided the skill
was broken: it invented an API on a host that does not exist, sent it a bearer
token, and grepped the environment for credentials -- twenty-odd round trips
until the turn timed out. The block was right; only the envelope was wrong.
"""

from __future__ import annotations

import pytest

from nanobot.agent.runner import _echoed_skill_block, _lift_echoed_skill_blocks
from nanobot.providers.base import LLMResponse, ToolCallRequest

# Reuse the neighbouring file's fixtures: same runner entry point, same spec.
from tests.agent.test_noop_call_beside_skill_block import _parse


def _exec(command: str, id_: str = "c1") -> ToolCallRequest:
    return ToolCallRequest(id=id_, name="exec", arguments={"command": command})


CAPTURED = 'echo {"skill":"tasks","action":"list_chores"}'


def test_the_captured_turn_runs_the_skill():
    """The exact call from the capture resolves to the skill, not the shell."""
    out, _ = _parse(LLMResponse(content="", tool_calls=[_exec(CAPTURED)],
                                finish_reason="tool_calls"))
    assert out.has_tool_calls, "the block was lost again"
    assert not any(tc.name == "exec" for tc in out.tool_calls), "the echo still runs"
    # `__skill_translate` runs the skill; `read_file` reads its SKILL.md first.
    # Which one depends on whether the runner already knows the skill -- both
    # are the skill, and neither is the shell.
    assert out.tool_calls[0].name in ("__skill_translate", "read_file"), out.tool_calls


@pytest.mark.parametrize("command", [
    CAPTURED,
    "echo '{\"skill\": \"tasks\", \"action\": \"list_chores\"}'",
    'echo "{\\"skill\\": \\"tasks\\", \\"action\\": \\"list_chores\\"}"',
    'echo -n {"skill":"tasks","action":"list_chores"}',
    'printf \'{"skill":"tasks","action":"list_chores"}\'',
    '{"skill":"tasks","action":"list_chores"}',
    'echo {"skill":"tasks","action":"list_chores"};',
])
def test_every_way_of_printing_the_block_is_recognised(command):
    block = _echoed_skill_block(_exec(command))
    assert block is not None, command
    assert '"skill"' in block and "list_chores" in block


@pytest.mark.parametrize("command", [
    'echo {"skill":"tasks"} > /tmp/x',          # a redirect is a real command
    'echo {"skill":"tasks"} | python3 run.py',  # so is a pipe
    'echo {"skill":"tasks"}; rm -rf /tmp/y',    # and a second command
    'echo {"name": "Tomi"}',                     # JSON, but not a skill block
    "echo hola",
    "ls -la",
])
def test_a_command_that_does_something_is_left_to_run(command):
    assert _echoed_skill_block(_exec(command)) is None, command


def test_a_real_call_beside_it_is_kept():
    """Only the echo is lifted; the call that does something still runs."""
    real = ToolCallRequest(id="c2", name="read_file", arguments={"path": "/x"})
    out = _lift_echoed_skill_blocks(
        LLMResponse(content="", tool_calls=[_exec(CAPTURED), real],
                    finish_reason="tool_calls"), "s")
    assert [tc.name for tc in out.tool_calls] == ["read_file"]
    assert "list_chores" in out.content
    assert out.finish_reason == "tool_calls"


def test_the_prose_is_kept_and_the_block_is_not_shown():
    out, _ = _parse(LLMResponse(content="Ya te las busco.",
                                tool_calls=[_exec(CAPTURED)],
                                finish_reason="tool_calls"))
    assert "Ya te las busco." in (out.content or "")
    assert '"skill"' not in (out.content or "")


def test_other_tools_are_never_touched():
    call = ToolCallRequest(id="c1", name="write_file",
                           arguments={"command": CAPTURED})
    assert _echoed_skill_block(call) is None

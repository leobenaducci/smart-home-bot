"""A turn that runs `true` and calls it work.

15 Aug 2026. Asked to mark two prize redemptions as delivered, Alfred answered
with the block the tasks skill asks for —
``{"skill": "tasks", "action": "fulfill_redemption", "redemption_id": 19}`` —
and put ``exec("true")`` beside it, the way a model does when it wants a turn
to feel like an action. The runner saw a structured tool call, stripped the
block as user-facing JSON, and ran the no-op. What came back was an empty
assistant message and ``Exit code: 0``: no error, nothing executed, nothing to
learn from. He tried the identical shape 132 times in five minutes — one LLM
round trip each, ~27 a minute — until the websocket timed out and the failure
escalated to a sub-agent, which is what finally marked the second redemption.
Six minutes and two API calls' worth of work.

Two guards, one for each half of that:

- the parser resolves the block when everything beside it does nothing, and
  never strips one in silence when something beside it does (this file, below)
- ``exec`` refuses a bare no-op with the sentence that ends the loop, instead of
  returning the empty success that reads as progress

The stripping itself is not in question: a block that reaches the chat as raw
JSON is its own old bug (the camera stream URLs). What changed is that it is no
longer *only* stripped.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from loguru import logger as _loguru

from nanobot.agent.tools.shell import ExecTool
from nanobot.providers.base import LLMResponse, ToolCallRequest


@contextmanager
def _captured(level: str = "WARNING"):
    """nanobot logs through loguru, which never reaches pytest's `caplog`."""
    lines: list[str] = []
    sink = _loguru.add(lambda m: lines.append(str(m)), level=level)
    try:
        yield lines
    finally:
        _loguru.remove(sink)


SKILL_PATHS = (
    "## Skills\n"
    "- **tasks** — tareas y premios de la familia. `/app/nanobot/skills/tasks/SKILL.md`\n"
)

BLOCK = '{"skill": "tasks", "action": "fulfill_redemption", "redemption_id": 19}'


def _spec(tool_names=("read_file", "exec")):
    from nanobot.agent.runner import AgentRunSpec
    from nanobot.config.schema import AgentDefaults

    tools = MagicMock()
    tools.tool_names = list(tool_names)
    tools.get_definitions.return_value = []
    return AgentRunSpec(
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=4,
        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
        session_key="homeweb:user1:2026-08-15:1786840580988",
    )


def _messages():
    return [
        {"role": "system", "content": SKILL_PATHS},
        {"role": "user", "content": "Entregados los 2"},
    ]


def _noop(command="true"):
    return ToolCallRequest(id="c1", name="exec", arguments={"command": command})


def _parse(response, spec=None, messages=None, level="WARNING"):
    from nanobot.agent.runner import AgentRunner

    with _captured(level) as lines:
        out = AgentRunner._apply_text_tool_call_parsing(
            response, spec or _spec(), messages or _messages())
    return out, "".join(lines)


# --- the block wins over the placeholder ---------------------------------
#
# "Resolves to the skill" is one of two calls. A block with an action, for a
# skill that ships a SKILL_PYTHON.md, becomes `__skill_translate` (run it); one
# without becomes `read_file` (read SKILL.md first). Which one depends on
# whether the guide is on disk -- in the image it is, at /app/nanobot/skills --
# so these accept either. Neither is `exec`, which is the bug.
_SKILL_CALLS = ("__skill_translate", "read_file")


def test_a_block_beside_a_noop_is_executed():
    """The reported turn. The block is the intent; `true` is punctuation."""
    out, _ = _parse(LLMResponse(
        content=f"Los marco como entregados.\n{BLOCK}",
        tool_calls=[_noop()], finish_reason="tool_calls"))

    assert out.has_tool_calls, "the block was dropped again — this is the bug"
    assert out.tool_calls[0].name in _SKILL_CALLS, "it must resolve to the skill"
    assert not any(tc.name == "exec" for tc in out.tool_calls), "the no-op survived"


@pytest.mark.parametrize("command", ["true", " true ", "true;", ":", "/bin/true", "exit 0"])
def test_every_shape_of_doing_nothing_gives_way(command):
    out, _ = _parse(LLMResponse(
        content=BLOCK, tool_calls=[_noop(command)], finish_reason="tool_calls"))
    assert out.has_tool_calls and out.tool_calls[0].name in _SKILL_CALLS


def test_the_prose_survives_and_the_json_does_not():
    """Whatever else happens, the block is not what the family reads."""
    out, _ = _parse(LLMResponse(
        content=f"Los marco como entregados.\n{BLOCK}",
        tool_calls=[_noop()], finish_reason="tool_calls"))

    assert '"skill"' not in (out.content or "")
    assert "Los marco" in (out.content or "")


def test_dropping_the_placeholder_is_logged():
    """132 identical turns produced no log line anyone could have read."""
    _out, log = _parse(
        LLMResponse(content=BLOCK, tool_calls=[_noop()], finish_reason="tool_calls"),
        level="INFO")

    assert "no-op" in log
    assert "homeweb:user1:2026-08-15:1786840580988" in log, "name the conversation"


# --- a real call beside a block still wins, but not in silence -----------


def test_a_block_beside_a_real_call_is_reported():
    out, log = _parse(LLMResponse(
        content=f"Ya lo hice.\n{BLOCK}",
        tool_calls=[ToolCallRequest(id="c1", name="exec", arguments={"command": "ls"})],
        finish_reason="tool_calls"))

    assert out.tool_calls[0].name == "exec", "the real call still runs"
    assert '"skill"' not in (out.content or ""), "the block still never reaches the chat"
    assert "beside" in log, f"the block vanished silently; log was: {log!r}"


def test_a_block_beside_a_real_call_runs_as_well():
    """Telling the model its block was dropped did not change what it wrote:
    deepseek-v4-flash read that note twenty times in one turn on 2026-09-22 and
    wrote block + exec every time. So the block now runs beside the call."""
    out, log = _parse(LLMResponse(
        content=f"Primero miro.\n{BLOCK}",
        tool_calls=[ToolCallRequest(id="c1", name="exec", arguments={"command": "ls"})],
        finish_reason="tool_calls"))

    names = [tc.name for tc in out.tool_calls]
    assert names[0] == "exec" and len(names) == 2 and names[1] in _SKILL_CALLS, names
    assert '"skill"' not in (out.content or "")
    assert not getattr(out, "_stripped_skill_block", False)


UNKNOWN_BLOCK = '{"skill": "no-such-skill", "action": "frobnicate"}'


def test_an_unresolvable_block_is_flagged_for_the_model():
    out, log = _parse(LLMResponse(
        content=f"Ya lo hice.\n{UNKNOWN_BLOCK}",
        tool_calls=[ToolCallRequest(id="c1", name="exec", arguments={"command": "ls"})],
        finish_reason="tool_calls"))
    assert [tc.name for tc in out.tool_calls] == ["exec"]
    assert getattr(out, "_stripped_skill_block", False)
    assert "no-such-skill" in log, "what it wrote has to be in the log"

    plain, _ = _parse(LLMResponse(
        content="Ahora lo reviso.",
        tool_calls=[ToolCallRequest(id="c1", name="exec", arguments={"command": "ls"})],
        finish_reason="tool_calls"))
    assert not getattr(plain, "_stripped_skill_block", False)


@pytest.mark.asyncio
async def test_the_model_is_told_when_its_block_could_not_run():
    from unittest.mock import AsyncMock
    from nanobot.agent.runner import AgentRunner, _STRIPPED_BLOCK_NOTE

    seen: list[list[dict]] = []

    async def chat_with_retry(*, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            return LLMResponse(
                content=f"Primero miro.\n{UNKNOWN_BLOCK}",
                tool_calls=[ToolCallRequest(id="c1", name="exec", arguments={"command": "ls"})],
                finish_reason="tool_calls")
        return LLMResponse(content="listo", tool_calls=[], finish_reason="stop")

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    spec = _spec()
    spec.initial_messages = _messages()
    spec.tools.execute = AsyncMock(return_value="file-a\nfile-b")
    await AgentRunner(provider).run(spec)

    tool_msgs = [m for m in seen[1] if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[-1]["content"].startswith("file-a\nfile-b")
    assert _STRIPPED_BLOCK_NOTE in tool_msgs[-1]["content"], tool_msgs[-1]["content"]


def test_an_ordinary_call_with_no_block_stays_quiet():
    """If every tool call logged a warning the warning would be worthless."""
    out, log = _parse(LLMResponse(
        content="Ahora lo reviso.",
        tool_calls=[ToolCallRequest(id="c1", name="exec", arguments={"command": "ls"})],
        finish_reason="tool_calls"))

    assert out.tool_calls[0].name == "exec"
    assert log.strip() == "", f"an ordinary call logged: {log!r}"


def test_a_bare_noop_with_no_block_is_left_to_exec():
    """The parser has nothing to resolve here — the guard below is what speaks."""
    out, _ = _parse(LLMResponse(
        content="", tool_calls=[_noop()], finish_reason="tool_calls"))

    assert out.has_tool_calls and out.tool_calls[0].arguments["command"] == "true"


# --- and the no-op itself, when it runs alone ----------------------------


@pytest.mark.parametrize("command", ["true", ":", "exit 0", "/bin/true", " true ; "])
def test_exec_refuses_a_command_that_does_nothing(command):
    error = ExecTool._noop_command_error(command)
    assert error and error.startswith("Error:")


def test_the_refusal_says_what_to_do_instead():
    """An error the model cannot act on costs the same turn twice — and this
    one was paid 132 times."""
    error = ExecTool._noop_command_error("true")
    assert "invocation block" in error
    assert "does not need a call" in error


@pytest.mark.parametrize("command", [
    "ls -la",
    "echo true",
    "true && echo hola",          # `true` as part of real work
    "if true; then echo x; fi",
    "python3 -c 'print(True)'",
])
def test_ordinary_commands_still_run(command):
    assert ExecTool._noop_command_error(command) is None


# --- blank text is not a block ---------------------------------------------
#
# 24 Sep 2026, deepseek-v4-pro, asked to test every light and cross-reference
# them with Home Assistant. It answered with no text -- "\n\n" -- and one exec
# call that read as a no-op. The stripper trims, so "\n\n" came back as "" and
# counted as a skill block: the only call was dropped as its placeholder, the
# "block" resolved to nothing, and the reply was "nothing was executed". A
# blank reply beside a call is a model calling a tool, and the call runs.


@pytest.mark.parametrize("content", ["\n\n", "   ", "\n", ""])
def test_blank_text_beside_a_call_is_not_a_block(content):
    out, log = _parse(LLMResponse(content=content, tool_calls=[_noop()], finish_reason="tool_calls"))
    assert out.has_tool_calls and out.tool_calls[0].name == "exec", "the only call was dropped"
    assert "resolved to no call" not in log


def test_a_block_with_blank_lines_around_it_still_counts():
    out, _ = _parse(LLMResponse(content=f"\n\n{BLOCK}\n\n", tool_calls=[_noop()],
                                finish_reason="tool_calls"))
    assert out.has_tool_calls and out.tool_calls[0].name in _SKILL_CALLS


def test_the_dropped_call_is_in_the_log():
    """The reported turn could not say what the model had called."""
    _out, log = _parse(LLMResponse(content=BLOCK, tool_calls=[_noop("echo ok")],
                                   finish_reason="tool_calls"), level="INFO")
    assert "echo ok" in log

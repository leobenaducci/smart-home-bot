"""A turn that ends without running anything has to say so in the log.

Reported from real use: the Profesor answered "Voy a revisar la estructura
exacta del skill document para generar ambos archivos correctamente." and then
nothing happened — no tool ran, no background task started, the turn simply
ended on the promise.

Two different paths through `_apply_text_tool_call_parsing` produce exactly
that, and by the time anyone looks at the chat they are indistinguishable: the
page strips skill-invocation blocks before rendering, so both leave the same
sentence and no output.

    1. the model re-emitted a call it already made in this turn, and dedup
       dropped every one of them
    2. the model wrote a block no extractor could resolve

Both used to return in silence, which made the difference undiagnosable from
the logs as well. These tests pin that each one is reported, and — just as
important — that the ordinary text answer stays quiet, or the warning is noise
and nobody will read it when it matters.

The fix for the stall itself depends on which path it is, so these tests
deliberately assert on the *reporting*, not on new behaviour.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

from loguru import logger as _loguru

from nanobot.providers.base import LLMResponse, ToolCallRequest


@contextmanager
def _captured_warnings():
    """nanobot logs through loguru, which never reaches pytest's `caplog` —
    the stdlib fixture stayed empty while the warning was plainly on stderr."""
    lines: list[str] = []
    sink = _loguru.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        yield lines
    finally:
        _loguru.remove(sink)


# The exact shape the system message uses (see `_SKILL_PATH_RE`): a bullet,
# the name in bold, and the path in backticks.
SKILL_PATHS = (
    "## Skills\n"
    "- **document** — crea archivos descargables. `/app/nanobot/skills/document/SKILL.md`\n"
)


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
        session_key="homeweb:user1:2026-08-04:edu:1785900000000",
    )


def _messages_with_prior_read(path="/app/nanobot/skills/document/SKILL.md"):
    """A turn in which the model has already read the skill file once."""
    return [
        {"role": "system", "content": SKILL_PATHS},
        {"role": "user", "content": "hazme la prueba y la pauta"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "%s"}' % path},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "# Document Generation ..."},
    ]


def _parse(response, spec, messages):
    from nanobot.agent.runner import AgentRunner

    with _captured_warnings() as lines:
        out = AgentRunner._apply_text_tool_call_parsing(response, spec, messages)
    return out, "".join(lines)


ANNOUNCEMENT = (
    "Voy a revisar la estructura exacta del skill document para generar "
    "ambos archivos correctamente.\n"
    '{"skill": "document"}'
)


def test_a_dedup_stall_is_reported():
    """The exact reported shape: a re-read of a file already read this turn."""
    spec, messages = _spec(), _messages_with_prior_read()
    response = LLMResponse(content=ANNOUNCEMENT, tool_calls=[], finish_reason="stop")

    out, log = _parse(response, spec, messages)

    assert not out.has_tool_calls, "nothing should be executed — that is the bug"
    assert "already-run" in log, f"the stall was silent; log was: {log!r}"
    assert "read_file" in log, "the log has to name what was dropped"
    assert spec.session_key in log, "and which conversation stalled"


def test_the_block_never_reaches_the_reader():
    """Whatever else happens, the invocation syntax does not go to the chat —
    which is also why the person reporting this could only say 'no hizo nada'."""
    spec, messages = _spec(), _messages_with_prior_read()
    out, _ = _parse(
        LLMResponse(content=ANNOUNCEMENT, tool_calls=[], finish_reason="stop"),
        spec, messages)
    assert '"skill"' not in (out.content or "")
    assert "Voy a revisar" in (out.content or ""), "the prose itself must survive"


def test_an_unresolvable_block_is_reported():
    """The other path: a block naming a skill that is not in the system message.
    Nothing runs, and until now nothing was logged either."""
    spec = _spec()
    messages = [
        {"role": "system", "content": SKILL_PATHS},
        {"role": "user", "content": "hazme la prueba"},
    ]
    response = LLMResponse(
        content='Ahora lo preparo.\n{"skill": "documentos", "action": "create"}',
        tool_calls=[], finish_reason="stop")

    out, log = _parse(response, spec, messages)

    assert not out.has_tool_calls
    assert "resolved to no call" in log, f"the miss was silent; log was: {log!r}"


def test_an_ordinary_answer_stays_quiet():
    """If every turn logged a warning the warning would be worthless."""
    spec, messages = _spec(), _messages_with_prior_read()
    response = LLMResponse(
        content="Listo, aquí está la guía: [descargar](download:media/guia.pdf)",
        tool_calls=[], finish_reason="stop")

    _out, log = _parse(response, spec, messages)

    assert log.strip() == "", f"an ordinary answer logged: {log!r}"


def test_a_real_call_still_runs_and_is_not_reported_as_a_stall():
    """The guard must not fire on the path that works — a call the model has
    not made yet is parsed out of the text and executed."""
    spec = _spec()
    messages = [
        {"role": "system", "content": SKILL_PATHS},
        {"role": "user", "content": "hazme la prueba"},
    ]
    response = LLMResponse(
        content='Voy a leerlo.\n{"skill": "document"}',
        tool_calls=[], finish_reason="stop")

    out, log = _parse(response, spec, messages)

    assert out.has_tool_calls, "a fresh call must still be parsed and run"
    assert out.tool_calls[0].name == "read_file"
    assert "already-run" not in log and "resolved to no call" not in log


def test_a_structured_call_is_untouched():
    """Responses that already carry real tool calls skip the parser entirely."""
    spec, messages = _spec(), _messages_with_prior_read()
    response = LLMResponse(
        content="Ahora genero el archivo.",
        tool_calls=[ToolCallRequest(id="c1", name="exec", arguments={"command": "ls"})],
        finish_reason="tool_calls")

    out, log = _parse(response, spec, messages)

    assert out.has_tool_calls and out.tool_calls[0].name == "exec"
    assert log.strip() == ""

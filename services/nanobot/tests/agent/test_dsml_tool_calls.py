"""DeepSeek's DSML tool-call markup, written as text, is run -- never published.

deepseek-v4-flash ended the morning greeting of 2026-09-26 with its own markup
instead of a structured call, and it went out verbatim in two chats:
<｜DSML｜tool_calls><｜DSML｜invoke name="weather">... It is rewritten into the
Qwen3.5 format, whose extractors already handle a tool, a skill by name, and
stripping what resolves to nothing.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from nanobot.providers.base import LLMResponse

WEATHER = (
    "<｜DSML｜tool_calls>\n"
    '<｜DSML｜invoke name="weather">\n'
    '<｜DSML｜parameter name="action" string="true">get_forecast</｜DSML｜parameter>\n'
    '<｜DSML｜parameter name="days" string="false">1</｜DSML｜parameter>\n'
    "</｜DSML｜invoke>\n"
    "</｜DSML｜tool_calls>"
)
SKILLS = "## Skills\n- **weather** — the forecast. `/app/nanobot/skills/weather/SKILL.md`\n"


def _spec(tool_names=("read_file", "exec")):
    from nanobot.agent.runner import AgentRunSpec
    from nanobot.config.schema import AgentDefaults
    tools = MagicMock()
    tools.tool_names = list(tool_names)
    tools.get_definitions.return_value = []
    return AgentRunSpec(initial_messages=[], tools=tools, model="m", max_iterations=4,
                        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
                        session_key="homeweb:999000111:2026-09-26:1")


def _parse(content, messages):
    from nanobot.agent.runner import AgentRunner
    return AgentRunner._apply_text_tool_call_parsing(
        LLMResponse(content=content, tool_calls=[], finish_reason="stop"), _spec(), messages)


def test_the_rewrite_is_the_qwen35_format():
    from nanobot.agent.runner import _dsml_as_qwen35
    assert _dsml_as_qwen35(WEATHER).split() == [
        "<function=weather>", "<parameter=action>get_forecast</parameter>",
        "<parameter=days>1</parameter>", "</function>"]
    assert _dsml_as_qwen35("plain text") == "plain text"


def test_a_skill_named_in_dsml_runs_as_that_skill():
    out = _parse(WEATHER, [{"role": "system", "content": SKILLS},
                           {"role": "user", "content": "buenos días"}])
    assert out.tool_calls, "the call was not run"
    assert "DSML" not in (out.content or "")


def test_a_tool_named_in_dsml_runs_as_that_tool():
    text = ('<｜DSML｜tool_calls><｜DSML｜invoke name="exec">'
            '<｜DSML｜parameter name="command" string="true">date</｜DSML｜parameter>'
            "</｜DSML｜invoke></｜DSML｜tool_calls>")
    out = _parse(text, [{"role": "user", "content": "qué hora es"}])
    assert [tc.name for tc in out.tool_calls] == ["exec"]
    assert out.tool_calls[0].arguments == {"command": "date"}


def test_markup_nothing_resolves_is_not_published():
    text = ("Buenos días.\n" + WEATHER.replace('"weather"', '"nonexistent_thing"'))
    out = _parse(text, [{"role": "user", "content": "hola"}])
    assert "DSML" not in (out.content or "") and "<function" not in (out.content or "")

"""A skill called the way a tool is called.

10 Sep 2026, the model benchmark. gemma4:e4b was asked to add milk to the
shopping list and called `grocery(...)` four times; gemma4:12b reached for
`weather:get_forecast`, `home_lights` and `home_assistant:todo_get_items`. Every
one of those failed as "Tool not found", spent the turn, and lost the case --
while naming exactly the right skill. Across every run in the table, all fifteen
malformed names resolved by lowercasing and swapping `_` for `-`, so the rescue
is a lookup against the catalogue already in the prompt, never a guess.
"""

from __future__ import annotations

import pytest

from nanobot.agent.runner import _lift_skill_named_tool_calls
from nanobot.providers.base import LLMResponse, ToolCallRequest

from tests.agent.test_noop_call_beside_skill_block import _captured, _spec

# The catalogue as the prompt carries it: every skill a model actually reached
# for in the benchmark, with the hyphenated names the loader really uses.
_CATALOGUE = "## Skills\n" + "".join(
    f"- **{n}** - x. `/app/nanobot/skills/{n}/SKILL.md`\n"
    for n in ("tasks", "grocery", "notifications", "weather",
              "family-message", "lights", "home-assistant"))


def _messages():
    return [{"role": "system", "content": _CATALOGUE},
            {"role": "user", "content": "agrega leche a la lista"}]


def _parse(response, level="WARNING"):
    from nanobot.agent.runner import AgentRunner
    with _captured(level) as lines:
        out = AgentRunner._apply_text_tool_call_parsing(response, _spec(), _messages())
    return out, "".join(lines)

# A block with an action and a SKILL_PYTHON.md runs (`__skill_translate`); one
# without reads the SKILL.md first. Both are the skill. Neither is a dead end.
_SKILL_CALLS = ("__skill_translate", "read_file")
_TOOLS = frozenset({"read_file", "exec", "message"})


def _call(name, args=None, id_="c1"):
    return ToolCallRequest(id=id_, name=name, arguments=args or {})


def _lift(name, args=None):
    return _lift_skill_named_tool_calls(
        LLMResponse(content="", tool_calls=[_call(name, args)],
                    finish_reason="tool_calls"),
        _messages(), _TOOLS, "test")


@pytest.mark.parametrize("name", [
    "grocery",                       # gemma4:e4b, four times
    "notifications",                 # gemma4:e4b and :12b
    "weather",                       # gemma4:e4b
    "tasks",                         # gemma4:e4b
    "family-message",                # gemma4:e4b
    "home_lights",                    # gemma4:12b -- underscore for hyphen, and
                                      # the name `lights` had until 2026-09-12
    "weather:get_forecast",          # gemma4:12b -- namespaced action
    "home_assistant:todo_get_items",  # gemma4:12b -- both at once
])
def test_every_captured_name_reaches_its_skill(name):
    out = _lift(name)
    assert out.has_tool_calls, f"{name} was dropped"
    assert out.tool_calls[0].name in _SKILL_CALLS, out.tool_calls[0].name


def test_an_action_in_the_name_is_carried_through():
    out = _lift("weather:get_forecast")
    tc = out.tool_calls[0]
    if tc.name == "__skill_translate":
        assert tc.arguments["invocation"]["action"] == "get_forecast"
        assert tc.arguments["invocation"]["skill"] == "weather"


def test_an_action_argument_is_used_when_the_name_has_none():
    out = _lift("grocery", {"action": "add_grocery", "name": "leche"})
    tc = out.tool_calls[0]
    if tc.name == "__skill_translate":
        assert tc.arguments["invocation"]["action"] == "add_grocery"
        assert tc.arguments["invocation"]["name"] == "leche"


def test_a_real_tool_is_left_alone():
    """The rescue must never touch a call that would have worked."""
    out = _lift_skill_named_tool_calls(
        LLMResponse(content="", tool_calls=[_call("read_file", {"path": "/x"})],
                    finish_reason="tool_calls"),
        _messages(), _TOOLS, "test")
    assert out.tool_calls[0].name == "read_file"
    assert out.tool_calls[0].arguments == {"path": "/x"}


def test_a_name_that_is_no_skill_still_fails():
    """Not a catch-all: an invented name has to keep failing as before."""
    out = _lift("definitely_not_a_skill")
    assert out.tool_calls[0].name == "definitely_not_a_skill"


def test_the_rescue_is_logged():
    """A model that needed rescuing got the call wrong; that stays visible."""
    _, log = _parse(LLMResponse(content="", tool_calls=[_call("grocery")],
                                finish_reason="tool_calls"), level="INFO")
    assert "rescued" in log.lower(), log

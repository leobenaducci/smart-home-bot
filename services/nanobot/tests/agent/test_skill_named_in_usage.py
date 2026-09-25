"""A skill is recorded by its name, not as the `exec` it becomes.

Every skill runs through `exec` — that is how the whole mechanism works — so the
record of a turn said "exec" as many times as skills were invoked and nothing
about which ones. That is fine until somebody asks the question the usage page
exists for: *which* of these is spending the money. «Comandos» at 412 calls is
not an answer.

So the translator remembers which call it turned into which skill, and the turn
is recorded with `skill:<name>` in that call's place. Two properties matter and
they pull against each other:

- the name has to survive into the record, or the page is blind again;
- it must **replace** the `exec`, never be added beside it, or every skill
  invocation counts twice and the totals stop matching the count nanobot sends.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from nanobot.agent.runner import AgentRunner
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.providers.base import LLMResponse, ToolCallRequest

SKILLS = Path(BUILTIN_SKILLS_DIR)


def _translate(*invocations):
    """Run the real translator over a response carrying `n` skill calls.

    Called unbound, like the vouching test next door: every invocation here
    translates *statically*, so nothing on the instance is touched. Pick actions
    that do — one that falls through to the LLM fallback reaches for `self` and
    dies on None, which is a fair warning that the fixture drifted.
    """
    calls = []
    for i, (skill, invocation) in enumerate(invocations):
        calls.append(ToolCallRequest(
            id=f"call_{i}",
            name="__skill_translate",
            arguments={"path": str(SKILLS / skill / "SKILL.md"),
                       "invocation": invocation},
        ))
    response = LLMResponse(content=None, tool_calls=calls)
    spec = SimpleNamespace(tools=SimpleNamespace(tool_names=["exec"]), model="test")
    return asyncio.run(AgentRunner._maybe_translate_skill_calls(None, response, spec, []))


def _recorded(response):
    """What the run loop would file for this response — the same substitution."""
    skills = getattr(response, "_skills_by_index", None) or {}
    return [f"skill:{skills[i]}" if i in skills else tc.name
            for i, tc in enumerate(response.tool_calls)]


def test_the_skill_name_reaches_the_record():
    out = _translate(("grocery", {"skill": "grocery", "action": "list_groceries"}))
    assert [tc.name for tc in out.tool_calls] == ["exec"], "it still runs as exec"
    assert _recorded(out) == ["skill:grocery"], "but it is filed by name"


def test_it_replaces_the_exec_rather_than_joining_it():
    """Counted twice, a skill would inflate every total on the page and stop
    agreeing with the plain count nanobot sends alongside the names."""
    out = _translate(("grocery", {"skill": "grocery", "action": "list_groceries"}))
    assert len(_recorded(out)) == len(out.tool_calls) == 1


def test_two_skills_in_one_turn_keep_their_own_names():
    out = _translate(
        ("grocery", {"skill": "grocery", "action": "list_groceries"}),
        ("menu", {"skill": "menu", "action": "list_menu"}),
    )
    assert _recorded(out) == ["skill:grocery", "skill:menu"]


def test_a_real_exec_is_still_an_exec():
    """Only translated calls are renamed. A shell command Alfred wrote himself
    is a different thing from a skill and belongs in a different row."""
    response = LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id="c1", name="exec", arguments={"command": "ls"}),
    ])
    spec = SimpleNamespace(tools=SimpleNamespace(tool_names=["exec"]), model="test")
    out = asyncio.run(AgentRunner._maybe_translate_skill_calls(None, response, spec, []))
    assert _recorded(out) == ["exec"]


def test_a_response_with_nothing_translated_carries_no_note():
    """The attribute is read with getattr and a default everywhere, and this is
    why: most responses never have it."""
    response = LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id="c1", name="read_file", arguments={"path": "/x"}),
    ])
    spec = SimpleNamespace(tools=SimpleNamespace(tool_names=["read_file"]), model="test")
    out = asyncio.run(AgentRunner._maybe_translate_skill_calls(None, response, spec, []))
    assert getattr(out, "_skills_by_index", None) is None
    assert _recorded(out) == ["read_file"]

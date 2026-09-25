"""What the exec guard accepts has to be what the translator actually emits.

The guard lets a skill's own generated code past. Deciding that by the *shape*
of the command was the bug — `python -c "import base64; exec(base64.b64decode(`
is visible to the model in its own tool-call history, so the model wrote it
back, and a hand-rolled payload wearing the wrapper skipped the check that
exists to stop exactly that.

So the translator now vouches for each command as it emits one, and the guard
asks. The two live in different modules and the string is built in only one of
them: these tests run the real translator and hand its output to the real
guard, so a change to the command on either side fails here rather than in the
house — silently opening the hole again if the guard drifts permissive, or
taking out every JSON-invocable skill if it drifts strict.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.runner import AgentRunner
from nanobot.agent.skill_invocation import (
    ECHO_LIKE,
    REROUTABLE_ECHO_LIKE,
    echoed_skill_invocation,
)
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.tools.shell import ExecTool
from nanobot.providers.base import LLMResponse, ToolCallRequest

SKILLS = Path(BUILTIN_SKILLS_DIR)


def _translate(invocation: dict, skill: str) -> str:
    """Run the real translator over *invocation*, return the exec command.

    Called unbound: the LLM fallback hangs off `self`, and every invocation
    here translates statically, so nothing on the instance is touched.
    """
    response = LLMResponse(content=None, tool_calls=[ToolCallRequest(
        id="call_1",
        name="__skill_translate",
        arguments={"path": str(SKILLS / skill / "SKILL.md"), "invocation": invocation},
    )])
    spec = SimpleNamespace(tools=SimpleNamespace(tool_names=["exec"]), model="test")
    translated = asyncio.run(
        AgentRunner._maybe_translate_skill_calls(None, response, spec, [])
    )
    call = translated.tool_calls[0]
    assert call.name == "exec", f"{skill} did not translate to exec: {call.name}"
    return call.arguments["command"]


# The house's own skills, one invocation each — the ones whose services are on
# the owned-signal list, so a guard that stopped trusting the translator would
# refuse them.
@pytest.mark.parametrize("invocation,skill", [
    ({"skill": "grocery", "action": "add_grocery", "name": "yerba"}, "grocery"),
    ({"skill": "grocery", "action": "list_groceries"}, "grocery"),
    ({"skill": "chores", "action": "list_chores", "scope": "today"}, "chores"),
    ({"skill": "menu", "action": "list_menu"}, "menu"),
])
def test_the_translators_own_command_is_let_through(invocation, skill):
    command = _translate(invocation, skill)
    assert ExecTool._skill_reimplementation_error(command) is None


def test_an_edited_payload_is_refused():
    """The vouching is what does the work, not the wrapper it happens to wear.

    This is the live failure in miniature: the model took a translated command,
    kept the wrapper, and changed the code inside — which is how it ended up
    calling the tasks skill's private `_curl` with a path it invented. Editing
    has to be done through the base64; a first draft of this test did a plain
    `.replace` on the command, changed nothing, and passed the *original*
    command to a guard that rightly allowed it.
    """
    import base64

    command = _translate(
        {"skill": "chores", "action": "list_chores", "scope": "today"}, "chores"
    )
    payload = ExecTool._python_payloads(command)[0]
    edited = payload.replace(
        "list_chores(scope='today')", "_curl('GET', 'list?scope=all')"
    )
    assert edited != payload, "the call line moved; this test is no longer editing it"
    tampered = command.replace(
        base64.b64encode(payload.encode()).decode(),
        base64.b64encode(edited.encode()).decode(),
    )
    assert tampered != command
    error = ExecTool._skill_reimplementation_error(tampered)
    assert error and "`tasks`" in error


@pytest.mark.parametrize("skill,action", [
    ("grocery", "list_groceries"),
    ("menu", "list_menu"),
    ("geo", "list_places"),
])
def test_a_refusal_names_the_skill_the_request_is_actually_about(skill, action):
    """grocery, menu, geo and family-message reach HomeCore by rewriting the
    tasks URL, so their own code carries TASKS_API_URL and /tasks/api. Told to
    "use the `tasks` skill" for a shopping-list request, the model cannot
    comply, and on 2026-08-12 it spent fifteen rounds failing to.

    Built from the real skill code rather than a handwritten payload: if a
    skill changes how it reaches HomeCore, this follows it.
    """
    import base64

    command = _translate({"skill": skill, "action": action}, skill)
    source = ExecTool._python_payloads(command)[0]
    assert "TASKS_API_URL" in source, (
        f"{skill} no longer derives its base from the tasks URL — this test is "
        f"guarding a collision that no longer exists"
    )
    # the same code, unvouched: what the model produces when it writes its own
    handwritten = command.replace(
        base64.b64encode(source.encode()).decode(),
        base64.b64encode((source + "\n# hand-written\n").encode()).decode(),
    )
    error = ExecTool._skill_reimplementation_error(handwritten)
    assert error, f"{skill} payload was not refused at all"
    assert f"`{skill}`" in error, f"refusal misnames the skill: {error}"


SKILL_LINES = (
    f"- **grocery** — lista de compras  `{SKILLS / 'grocery' / 'SKILL.md'}`\n"
    f"- **tasks** — tareas  `{SKILLS / 'tasks' / 'SKILL.md'}`\n"
)


def _run(response):
    spec = SimpleNamespace(tools=SimpleNamespace(tool_names=["exec"]), model="m")
    messages = [{"role": "system", "content": SKILL_LINES}]
    return asyncio.run(
        AgentRunner._maybe_translate_skill_calls(None, response, spec, messages)
    )


def _exec_call(command):
    return LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id="c1", name="exec", arguments={"command": command})
    ])


def test_an_echoed_block_becomes_the_call_it_was_trying_to_make():
    """`echo '{"skill":...}'` was refused, so the model spent a round trip
    learning what it already knew. It opened nearly every skill call this way.
    """
    out = _run(_exec_call(
        """echo '{"skill":"grocery","action":"add_grocery","name":"cafe"}'"""
    ))
    call = out.tool_calls[0]
    assert call.name == "exec"
    source = ExecTool._python_payloads(call.arguments["command"])[0]
    assert "add_grocery(name='cafe')" in source.strip().splitlines()[-1]
    # and it is vouched for, so the reimplementation guard lets it run
    assert ExecTool._skill_reimplementation_error(call.arguments["command"]) is None


@pytest.mark.parametrize("command", [
    # a skill that does not exist — nothing to route it to
    """echo '{"skill":"groceries","action":"add_grocery","name":"cafe"}'""",
    # not the whole command: the block is being written somewhere
    """echo '{"skill":"grocery","action":"list_groceries"}' > /tmp/x.json""",
    # genuinely printing data that happens to have a skill key
    """echo 'the skill JSON is {"skill":"grocery"} roughly'""",
])
def test_what_cannot_be_rerouted_is_left_for_the_guard(command):
    """The reroute is narrower than the refusal on purpose. Anything it does
    not claim must still reach ExecTool rather than silently running."""
    out = _run(_exec_call(command))
    assert out.tool_calls[0].arguments.get("command") == command


def test_cat_is_refusable_but_never_reroutable():
    """`cat` does not print its argument, it opens it as a filename. Refusing
    `cat '{...}'` costs a round trip; rerouting it performs the call. This was
    live: one shared set drove both, so
    `cat '{"skill":"tasks","action":"delete_chore","task_id":9,"confirm":true}'`
    deleted chore 9, its confirmation satisfied by a literal meant for display.
    """
    block = '{"skill":"chores","action":"delete_chore","task_id":9,"confirm":true}'
    assert echoed_skill_invocation(f"cat '{block}'") is None
    assert ExecTool._skill_invocation_error(f"cat '{block}'") is not None
    # echo and printf really do print, so those stay reroutable
    for cmd in ("echo", "printf"):
        assert echoed_skill_invocation(f"{cmd} '{block}'") is not None


def test_the_reroute_set_is_a_subset_of_the_refusal_set():
    """Anything the reroute claims must also be something the guard would have
    refused. If these diverge the other way, narrowing the refusal silently
    un-refuses whatever the reroute declines."""
    assert REROUTABLE_ECHO_LIKE < ECHO_LIKE


@pytest.mark.parametrize("arguments", [
    {"command": '''echo '{"skill": null, "action": "x"}\''''},
    {"command": '''echo '{"skill": 3, "action": "x"}\''''},
    {"command": '''echo '{"skill": {"n": 1}, "action": "x"}\''''},
    {"command": None},
    {},
])
def test_malformed_arguments_do_not_kill_the_turn(arguments):
    """A tool call the model got wrong must come back as a recoverable error,
    not an exception out of the translator. Before the guards, `{"skill": null}`
    raised AttributeError and `command: null` raised TypeError, and neither is
    caught between here and AgentRunner.run — the turn died and the family got
    nothing."""
    response = LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id="c1", name="exec", arguments=arguments)])
    out = _run(response)                     # must not raise
    assert out.tool_calls[0].name == "exec"


def test_a_real_shell_command_is_untouched():
    out = _run(_exec_call("ls -la /tmp"))
    assert out.tool_calls[0].arguments["command"] == "ls -la /tmp"


def test_tasks_itself_is_still_named_for_its_own_service():
    """Reordering the signals must not stop tasks from claiming its own."""
    command = """python3 -c "import os; print(os.environ['TASKS_API_URL'])" """
    error = ExecTool._skill_reimplementation_error(command)
    assert error and "`tasks`" in error


def test_a_translated_command_still_carries_an_owned_signal():
    """Guarding against the test above passing for the wrong reason: if the
    generated code stopped mentioning TASKS_API_URL, the refusal would prove
    nothing about vouching."""
    command = _translate(
        {"skill": "chores", "action": "list_chores", "scope": "today"}, "chores"
    )
    payloads = ExecTool._python_payloads(command)
    assert any("TASKS_API_URL" in p or "/tasks/api" in p for p in payloads)

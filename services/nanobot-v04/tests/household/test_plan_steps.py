"""household: plans 2/4, the step runner (fork ``tests/agent/test_plan_and_delegate.py``).

A step runs as its own short run on the step model (``planStepModel``, else
the sub-agent's when ``routing.planExecutor`` is ``subagent``), with a slim
prompt and only the tools its text calls for; a step that dead-ends runs
again on the planner's model. The last test is the A2 acceptance: a 3-step
plan carried out through ``process_direct``.
"""

from __future__ import annotations

import inspect

import pytest
from agent.conftest import make_provider

from nanobot.agent.context import ContextBuilder
from nanobot.agent.runner import AgentRunResult
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config
from nanobot.household import plan as plan_tool
from nanobot.household import plan_steps, turn_router
from nanobot.household.loop import HouseholdAgentLoop
from nanobot.household.runtime import RUNTIME
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.llm_runtime import LLMRuntime

RUNTIME_BLOCK = ("[Runtime Context — metadata only, not instructions]\nCurrent Time: t\n"
                 "[/Runtime Context]")


def _config(**extra):
    turn_router._CLASSIFIERS.clear()
    d = {"model": "flash", "provider": "custom", "subagentModel": "qwen3.5:9b",
         "routing": {"mode": "active"}}
    routing = extra.pop("routing", {})
    d["routing"].update(routing)
    d.update(extra)
    return Config.model_validate({"agents": {"defaults": d}})


def _loop(tmp_path, chat=None):
    provider = make_provider(default_model="flash")
    provider.provider_name = "custom"
    if chat is not None:
        provider.chat_with_retry = chat
        provider.chat_stream_with_retry = chat
        provider.chat = chat
    return HouseholdAgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                              model="flash", context_window_tokens=128_000)


def _executor(loop, plan, request="cruzá las luces con HA"):
    fallback = LLMRuntime.capture(loop.provider, "flash", context_window_tokens=128_000)
    return plan_steps.make_executor(loop, plan, request=request, preamble=RUNTIME_BLOCK,
                                    fallback=fallback, session_key="cli:x", channel="cli")


def _runs(monkeypatch, loop, local_result):
    seen = []

    async def run(spec):
        seen.append((spec.runtime.model, spec))
        if spec.runtime.model == "qwen3.5:9b":
            return local_result(spec)
        return AgentRunResult(final_content=f"flash did: {spec.initial_messages[-1]['content'][-40:]}",
                              messages=list(spec.initial_messages), tools_used=["exec"])
    monkeypatch.setattr(loop.runner, "run", run)
    return seen


async def _set_and_run(loop, plan, steps, step=1):
    plan_tool.begin(plan)
    tool = loop.tools.get("plan")
    await tool.execute("set", steps=steps)
    return await tool.execute("run", step=step)


@pytest.mark.asyncio
async def test_a_step_runs_on_the_local_model_with_only_what_it_needs(tmp_path, monkeypatch):
    RUNTIME.config = _config()
    loop = _loop(tmp_path)
    seen = _runs(monkeypatch, loop, lambda spec: AgentRunResult(
        final_content="8 luces: Oficina Tomi, Luz Paula...", messages=list(spec.initial_messages),
        tools_used=["skill"]))
    plan = plan_tool.TurnPlan(spec=None)
    plan.executor = _executor(loop, plan)
    out = await _set_and_run(loop, plan, ["listar luces", "listar HA", "cruzar"])
    model, spec = seen[-1]
    assert model == "qwen3.5:9b" and "8 luces" in out and "Next: step 2" in out
    system = spec.initial_messages[0]["content"]
    assert "## Workspace" in system or "workspace" in system.lower()
    prompt = spec.initial_messages[-1]["content"]
    assert "cruzá las luces con HA" in prompt and "Do step 1 now" in prompt and "Current Time" in prompt
    assert "This step may only READ" in prompt
    names = spec.tools.tool_names
    assert "plan" not in names and "spawn" not in names and "cron" not in names
    assert "exec" in names and "exec" not in {d["function"]["name"] for d in spec.tools.get_definitions()}
    assert spec.max_iterations == RUNTIME.config.agents.defaults.routing.plan_step_calls
    assert spec.runtime.generation.reasoning_effort == "none"
    assert plan.by[1] == "qwen3.5:9b" and plan.tools[1] == ["skill"]


@pytest.mark.asyncio
async def test_a_step_that_dead_ends_locally_is_done_again_on_the_everyday_model(tmp_path, monkeypatch):
    RUNTIME.config = _config()
    loop = _loop(tmp_path)
    seen = _runs(monkeypatch, loop, lambda spec: AgentRunResult(
        final_content="", stop_reason="repeated_tool_calls", messages=list(spec.initial_messages) + [
            {"role": "assistant", "tool_calls": [{"function": {"name": "exec",
                                                               "arguments": '{"command":"echo x"}'}}]}]))
    plan = plan_tool.TurnPlan(spec=None)
    plan.executor = _executor(loop, plan, request="x")
    await _set_and_run(loop, plan, ["a"])
    assert [m for m, _ in seen] == ["qwen3.5:9b", "flash"]
    assert "echo x" in seen[-1][1].initial_messages[-1]["content"]     # what already ran
    assert plan.by[1].startswith("flash (after qwen3.5:9b)")
    # The planner's model gets every tool (but the plan itself), not the step's few.
    assert "write_file" in seen[-1][1].tools.tool_names or "read_file" in seen[-1][1].tools.tool_names


@pytest.mark.asyncio
async def test_a_local_step_that_raises_is_done_again_on_the_everyday_model(tmp_path, monkeypatch):
    RUNTIME.config = _config()
    loop = _loop(tmp_path)

    def boom(spec):
        raise RuntimeError("local model down")
    seen = _runs(monkeypatch, loop, boom)
    plan = plan_tool.TurnPlan(spec=None)
    plan.executor = _executor(loop, plan, request="x")
    out = await _set_and_run(loop, plan, ["a"])
    assert [m for m, _ in seen] == ["qwen3.5:9b", "flash"] and "flash did" in out


@pytest.mark.asyncio
async def test_everyday_can_be_told_to_do_the_steps_itself(tmp_path, monkeypatch):
    RUNTIME.config = _config(routing={"planExecutor": "everyday"})
    loop = _loop(tmp_path)
    seen = _runs(monkeypatch, loop, lambda spec: AgentRunResult(final_content="no", messages=[]))
    plan = plan_tool.TurnPlan(spec=None)
    plan.executor = _executor(loop, plan, request="x")
    await _set_and_run(loop, plan, ["a"])
    assert [m for m, _ in seen] == ["flash"]


@pytest.mark.asyncio
async def test_with_no_step_model_the_planner_does_the_steps(tmp_path, monkeypatch):
    RUNTIME.config = _config(subagentModel=None)
    loop = _loop(tmp_path)
    seen = _runs(monkeypatch, loop, lambda spec: AgentRunResult(final_content="no", messages=[]))
    plan = plan_tool.TurnPlan(spec=None)
    plan.executor = _executor(loop, plan, request="x")
    await _set_and_run(loop, plan, ["a"])
    assert [m for m, _ in seen] == ["flash"]


@pytest.mark.asyncio
async def test_the_plan_steps_model_wins_over_the_sub_agents(tmp_path, monkeypatch):
    RUNTIME.config = _config(planStepModel="gemma4:e4b")
    loop = _loop(tmp_path)
    seen = _runs(monkeypatch, loop, lambda spec: AgentRunResult(final_content="x", messages=[]))
    plan = plan_tool.TurnPlan(spec=None)
    plan.executor = _executor(loop, plan, request="x")
    await _set_and_run(loop, plan, ["a"])
    assert [m for m, _ in seen] == ["gemma4:e4b"] and plan.by[1] == "gemma4:e4b"


@pytest.mark.asyncio
async def test_the_checklist_shows_the_step_running_and_who_ran_it(tmp_path, monkeypatch):
    RUNTIME.config = _config()
    loop = _loop(tmp_path)
    _runs(monkeypatch, loop, lambda spec: AgentRunResult(final_content="ok", messages=[],
                                                         tools_used=["skill"]))
    said = []

    async def progress(state):
        said.append(state)
    plan = plan_tool.TurnPlan(spec=None, progress=progress)
    plan.executor = _executor(loop, plan, request="x")
    await _set_and_run(loop, plan, ["a", "b"])
    events = [(s["event"], s["running"]) for s in said]
    assert events == [("set", None), ("run", 1), ("done", None)]
    assert said[-1]["by"] == {"1": "qwen3.5:9b"} and said[-1]["tools"] == {"1": ["skill"]}


@pytest.mark.asyncio
async def test_a_step_the_planner_named_in_acts_may_act(tmp_path, monkeypatch):
    RUNTIME.config = _config()
    loop = _loop(tmp_path)
    seen = _runs(monkeypatch, loop, lambda spec: AgentRunResult(final_content="ok", messages=[]))
    plan = plan_tool.TurnPlan(spec=None)
    plan.executor = _executor(loop, plan, request="x")
    plan_tool.begin(plan)
    tool = loop.tools.get("plan")
    await tool.execute("set", steps=["listar con read_file", "guardar notas con write_file"], acts=[2])
    await tool.execute("run", step=1)
    await tool.execute("run", step=2)
    first, second = seen[0][1], seen[1][1]
    assert "write_file" not in first.tools.tool_names and "write_file" in second.tools.tool_names
    assert "This step may only READ" not in second.initial_messages[-1]["content"]


@pytest.mark.asyncio
async def test_a_step_does_not_flag_the_turn(tmp_path, monkeypatch):
    """A step's bad invocation is the step's: the turn must not escalate for it."""
    from nanobot.household.runtime import TURN_FLAGS, raise_turn_flag

    RUNTIME.config = _config()
    loop = _loop(tmp_path)

    def bad(spec):
        raise_turn_flag("bad_invocation")
        return AgentRunResult(final_content="ok", messages=[])
    _runs(monkeypatch, loop, bad)
    plan = plan_tool.TurnPlan(spec=None)
    plan.executor = _executor(loop, plan, request="x")
    token = TURN_FLAGS.set({})
    try:
        await _set_and_run(loop, plan, ["a"])
        assert not (TURN_FLAGS.get() or {}).get("bad_invocation")
    finally:
        TURN_FLAGS.reset(token)


def test_a_step_is_told_not_to_guess_not_to_ask_and_to_batch():
    src = inspect.getsource(plan_steps._step_prompt)
    assert "Never fill a gap with a likely guess" in src
    assert "do not act and do not ask" in src
    assert "make all those calls" in src


def test_the_step_prompt_is_small_and_names_what_the_plan_names(tmp_path):
    (tmp_path / "SOUL.md").write_text("zzsoulzz " * 4000)
    (tmp_path / "AGENTS.md").write_text("zzrulezz " * 3000)
    (tmp_path / "TOOLS.md").write_text("tool notes")
    cb = ContextBuilder(tmp_path)
    full, slim = cb.build_system_prompt(), plan_steps.step_system_prompt(cb, "list the lights")
    assert "zzsoulzz" in full and "zzsoulzz" not in slim and "zzrulezz" not in slim
    assert "tool notes" in slim and len(slim) < len(full) / 2


def test_a_named_always_skill_keeps_its_path_line_in_the_step_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    cb = ContextBuilder(tmp_path)
    always = cb.skills.get_always_skills()
    if not always:
        pytest.skip("no always-loaded skill in the bundled set")
    name = always[0]
    slim = plan_steps.step_system_prompt(cb, f"call skill {name}")
    assert "# Active Skill Paths" in slim
    others = {s["name"] for s in cb.skills.list_skills(filter_unavailable=True)} - {name}
    assert cb.skills.build_skills_summary(exclude=others).strip() in slim


def test_a_skill_a_step_names_is_in_its_prompt_whole(tmp_path):
    skill = tmp_path / "skills" / "lamps"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: lamps\ndescription: the lamps\n---\n\nzzlampguidezz\n")
    cb = ContextBuilder(tmp_path)
    assert "zzlampguidezz" in plan_steps.step_system_prompt(cb, "Call skill lamps, action list_lamps.")
    assert "zzlampguidezz" in plan_steps.step_system_prompt(cb, "Flash each with the lamps skill.")
    # A word that happens to be a skill's name is not a step calling it.
    assert "zzlampguidezz" not in plan_steps.step_system_prompt(cb, "turn the lamps off")


def test_a_step_is_offered_only_the_tools_its_text_calls_for():
    names = ["read_file", "write_file", "edit_file", "grep", "exec", "message", "cron", "plan",
             "web_search", "web_fetch", "mcp_brightdata_scrape_as_markdown",
             "mcp_homeassistant_GetLiveContext", "mcp_homeassistant_HassTurnOn",
             "mcp_homeassistant_homeassistant__GetDateTime"]
    t = plan_steps.tools_for_step
    assert t("Flash each light with the lights skill (flash_light)", names) == ["read_file"]
    assert t("List the chores for today with the chores skill", names) == ["read_file"]
    ha = t("Listar las luces de Home Assistant (entity, área)", names)
    assert "mcp_homeassistant_GetLiveContext" in ha and "web_search" not in ha
    assert "mcp_homeassistant_GetLiveContext" in t("Call GetLiveContext for lights", names)
    web = t("Buscar en la web el pronóstico de mañana", names)
    assert {"web_search", "web_fetch"} <= set(web) and "mcp_brightdata_scrape_as_markdown" not in web
    assert "cron" in t("Agendar reintento del draw en 5 minutos", names)
    named = t("scrape_as_markdown the page, then write_file the notes", names)
    assert "mcp_brightdata_scrape_as_markdown" in named and "write_file" in named
    assert "exec" not in t("exec the script", names)


def test_cron_files_and_home_assistant_intents_change_things():
    c = plan_steps.changes_things
    assert c("cron") and c("write_file") and c("message") and c("mcp_homeassistant_HassTurnOn")
    assert not c("read_file") and not c("mcp_homeassistant_HassGetState")
    assert not c("mcp_homeassistant_GetLiveContext")


# --- A2 acceptance: a 3-step plan through process_direct ------------------------


@pytest.mark.asyncio
async def test_a_three_step_plan_completes_through_process_direct(tmp_path):
    RUNTIME.config = _config(classifierModel="tiny")
    planner = iter([
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="p1", name="plan", arguments={"action": "set", "steps": [
                "listar las luces", "listar las áreas", "cruzar luces con áreas"]})]),
        *[LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id=f"r{k}", name="plan", arguments={"action": "run", "step": k})]) for k in (1, 2, 3)],
        LLMResponse(content="Oficina Tomi está en Oficina; Luz Paula en Living."),
    ])
    step_answers = iter(["Oficina Tomi, Luz Paula", "Oficina, Living",
                         "Oficina Tomi → Oficina; Luz Paula → Living"])
    calls: list[str] = []
    step_prompts: list[str] = []

    async def chat(**kw):
        model = kw.get("model")
        calls.append(model)
        if model == "tiny":
            return LLMResponse(content='{"label": "long", "reason": "several steps"}')
        if model == "qwen3.5:9b":
            step_prompts.append(kw["messages"][-1]["content"])
            return LLMResponse(content=next(step_answers))
        return next(planner)

    loop = _loop(tmp_path, chat)
    out = await loop.process_direct("cruzá las luces con las áreas",
                                    session_key="whatsapp-own:56999999999",
                                    channel="whatsapp", chat_id="56999999999")
    assert out.content == "Oficina Tomi está en Oficina; Luz Paula en Living."
    assert calls == ["tiny", "flash", "flash", "qwen3.5:9b", "flash", "qwen3.5:9b",
                     "flash", "qwen3.5:9b", "flash"]
    assert "Do step 3 now" in step_prompts[2]
    assert "Step 1 (listar las luces) found:\nOficina Tomi, Luz Paula" in step_prompts[2]
    results = [str(m["content"]) for m in loop.sessions.get_or_create(
        "whatsapp-own:56999999999").messages if m.get("role") == "tool"]
    assert any("Step 3 result" in r and "Every step is done" in r for r in results)


# --- A3: read-only steps and no shell in a step ---------------------------------


def _exec_registry(tmp_path):
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.shell import ExecTool

    tools = ToolRegistry()
    tools.register(ExecTool(working_dir=str(tmp_path)))
    return plan_steps.step_registry(tools, [], read_only=True)


@pytest.mark.asyncio
async def test_a_plan_step_has_no_shell_of_its_own(tmp_path):
    from nanobot.household.skill_invocation import register_skill_translation

    reg = _exec_registry(tmp_path)
    assert all(d["function"]["name"] != "exec" for d in reg.get_definitions())
    said = await reg.execute("exec", {"command": "echo hi"})
    assert "no shell in a plan step" in str(said)
    register_skill_translation("echo from-a-skill")
    assert "no shell" not in str(await reg.execute("exec", {"command": "echo from-a-skill"}))


@pytest.mark.asyncio
async def test_the_runtimes_own_notes_pass_the_step_shell_guard(tmp_path):
    from nanobot.household.text_calls import _runtime_printf

    said = await _exec_registry(tmp_path).execute(
        "exec", {"command": _runtime_printf("This step only reads.")})
    assert "This step only reads." in str(said) and "no shell" not in str(said)


@pytest.mark.asyncio
async def test_a_hand_written_shell_call_in_a_step_is_refused_by_the_runner(tmp_path):
    """Through upstream's executor (prepare_call), not only the registry's execute."""
    RUNTIME.config = _config()
    marker = tmp_path / "ran.txt"
    step_replies = iter([
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="x1", name="exec", arguments={"command": f"touch {marker}"})]),
        LLMResponse(content="Could not check: no shell."),
    ])
    seen_tool_results: list[str] = []

    async def chat(**kw):
        seen_tool_results.extend(str(m.get("content")) for m in kw["messages"] if m.get("role") == "tool")
        return next(step_replies)

    loop = _loop(tmp_path, chat)
    from nanobot.agent.tools.shell import ExecTool
    if not loop.tools.has("exec"):
        loop.tools.register(ExecTool(working_dir=str(tmp_path)))
    plan = plan_tool.TurnPlan(spec=None)
    plan.executor = _executor(loop, plan, request="x")
    out = await _set_and_run(loop, plan, ["check the disk"])
    assert not marker.exists()
    assert any("no shell in a plan step" in r for r in seen_tool_results)
    assert "Could not check" in out


def test_only_hass_intents_that_act_are_left_out_of_a_reading_step():
    c = plan_steps.changes_things
    for name in ("mcp_homeassistant_intent__HassTurnOn", "mcp_homeassistant_light__HassLightSet",
                 "mcp_homeassistant_media_player__HassSetVolume",
                 "mcp_homeassistant_assist_satellite__HassBroadcast"):
        assert c(name), name
    for name in ("mcp_homeassistant_homeassistant__GetLiveContext",
                 "mcp_homeassistant_intent__HassGetState", "exec", "read_file",
                 "mcp_brightdata_search_engine", "mcp_homeassistant_todo_get_items"):
        assert not c(name), name


def test_a_reading_step_may_run_reads_and_nothing_with_a_go_ahead():
    from nanobot.household.text_calls import is_read_action
    for inv in ({"action": "list_lights"}, {"action": "get_state"}, {"action": "find_in_ha", "device": "x"},
                {"action": "room_from_ha"}, {"action": "list_ha_lights"}):
        assert is_read_action(inv), inv
    for inv in ({"action": "turn_on", "device": "x"}, {"action": "flash_light"}, {"action": "toggle(x)"},
                {"action": "find_in_ha", "device": "x", "confirmed": True},
                {"action": "room_from_ha", "apply": True}, {"action": "add_grocery"}, {}):
        assert not is_read_action(inv), inv


def _step_hook(spec):
    from nanobot.household.hooks import HouseholdResponseHook

    return next(h for h in spec.hook._hooks if isinstance(h, HouseholdResponseHook))


@pytest.mark.asyncio
async def test_the_planner_names_the_steps_that_act(tmp_path, monkeypatch):
    RUNTIME.config = _config()
    loop = _loop(tmp_path)
    seen = _runs(monkeypatch, loop, lambda spec: AgentRunResult(
        final_content="ok", messages=list(spec.initial_messages), tools_used=["x"]))
    plan = plan_tool.TurnPlan(spec=None)
    plan.executor = _executor(loop, plan, request="flash each light")
    plan_tool.begin(plan)
    tool = loop.tools.get("plan")
    said = await tool.execute("set", steps=["list", "flash each"], acts=[2, 9])
    assert plan.acts == {2} and "only reads" not in said
    await tool.execute("run", step=1)
    assert _step_hook(seen[-1][1]).read_only is True
    await tool.execute("run", step=2)
    assert _step_hook(seen[-1][1]).read_only is False
    said = await tool.execute("set", steps=["list"])
    assert plan.acts == set() and "only reads" in said


@pytest.mark.asyncio
async def test_a_step_stopped_from_acting_tells_the_planner(tmp_path, monkeypatch):
    RUNTIME.config = _config()
    loop = _loop(tmp_path)

    def local(spec):
        _step_hook(spec).refused.append("lights.turn_on")
        return AgentRunResult(final_content="Turned the lamp on.", messages=list(spec.initial_messages),
                              tools_used=["skill"])
    seen = _runs(monkeypatch, loop, local)
    plan = plan_tool.TurnPlan(spec=None)
    plan.executor = _executor(loop, plan, request="the lamp")
    out = await _set_and_run(loop, plan, ["look at the lamp with the lights skill"])
    assert "Not done" in out and "lights.turn_on" in out and "step 1 in acts" in out
    names = seen[-1][1].tools.tool_names
    assert "cron" not in names and "write_file" not in names and "web_search" not in names


@pytest.mark.asyncio
async def test_a_reading_step_is_told_so_before_it_starts(tmp_path, monkeypatch):
    RUNTIME.config = _config()
    loop = _loop(tmp_path)
    seen = _runs(monkeypatch, loop, lambda spec: AgentRunResult(
        final_content="ok", messages=list(spec.initial_messages), tools_used=["x"]))
    plan = plan_tool.TurnPlan(spec=None)
    plan.executor = _executor(loop, plan, request="the lamp")
    plan_tool.begin(plan)
    tool = loop.tools.get("plan")
    await tool.execute("set", steps=["Call skill lights, action turn_on", "flash it"], acts=[2])
    await tool.execute("run", step=1)
    assert "may only READ" in seen[-1][1].initial_messages[-1]["content"]
    await tool.execute("run", step=2)
    assert "may only READ" not in seen[-1][1].initial_messages[-1]["content"]


@pytest.mark.asyncio
async def test_a_reading_step_that_sends_an_acting_skill_block_is_stopped(tmp_path):
    """End to end: the step model writes a skill block that would switch a
    lamp; the household skill hook refuses it and the planner is told."""
    skill = tmp_path / "skills" / "lamps"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: lamps\ndescription: the lamps\n---\n\nUse actions.\n")
    (skill / "SKILL_PYTHON.md").write_text(
        "```python\ndef turn_on(device):\n    print('on', device)\n\n"
        "def list_lamps():\n    print('Oficina Tomi, Luz Paula')\n```\n")
    RUNTIME.config = _config()
    block = '```json\n{"skill": "lamps", "action": "turn_on", "device": "Luz Paula"}\n```'
    replies = iter([LLMResponse(content=block), LLMResponse(content="Luz Paula needs a go-ahead.")])
    tool_results: list[str] = []

    async def chat(**kw):
        tool_results.extend(str(m.get("content")) for m in kw["messages"] if m.get("role") == "tool")
        return next(replies)

    loop = _loop(tmp_path, chat)
    plan = plan_tool.TurnPlan(spec=None)
    plan.executor = _executor(loop, plan, request="prendé Luz Paula")
    out = await _set_and_run(loop, plan, ["Call skill lamps, action turn_on for Luz Paula"])
    assert any("This step only reads" in r for r in tool_results), tool_results
    assert "Not done" in out and "lamps.turn_on" in out

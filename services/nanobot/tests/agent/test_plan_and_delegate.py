"""Long work in the chat with a plan; hours of work, and dead ends, to a sub-agent.

2026-09-24. Asked to cross-reference the lights with Home Assistant, a turn on
the powerful model spent its calls on shell placeholders and stopped
"repeating the same action without progress". The house now routes:

* ``complex`` -- a hard question somebody waits on: the powerful model;
* ``long`` -- several steps to do now: the everyday model, in the chat, with a
  plan it ticks off (tools/plan.py), which buys the turn more calls per step;
* ``background`` -- hours of work, or asked for in the background: a sub-agent;
* a cheap turn that dead-ends: a sub-agent, with what it already did;
* past the time limit mid-plan: the steps left, to a sub-agent.

Only where a later answer is an answer (delegate.may_delegate): a person's own
chat or own WhatsApp; never voice, a HomeCore system turn, a space, or someone
else's message.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import mock_provider

from nanobot.agent import delegate
from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunResult, AgentRunSpec
from nanobot.agent.tools import plan as plan_tool
from nanobot.config.schema import RoutingConfig
from nanobot.providers.base import LLMResponse
from nanobot.session.manager import Session

CHAT = "homeweb:999000111:2026-09-24:1790250767247"
RUNTIME = ("[Runtime Context — metadata only, not instructions]\nCurrent Time: t\n"
           "[/Runtime Context]\n\n")


# --- who may hand off ----------------------------------------------------------

@pytest.mark.parametrize("channel,chat_id,key,ok", [
    ("websocket", CHAT, None, True),                                  # a person's own chat
    ("websocket", "homeweb:999000111:2026-09-24:ev-notif", None, False),  # HomeCore's triage
    ("websocket", "homeweb:999000111:2026-09-24:ev-ask-999", None, False),  # someone else asking
    ("websocket", "homeweb:999000111:2026-09-24:teacher:1790", None, False),  # a profession's space
    ("voice", "cocina", None, False),                                  # nobody reads "later" aloud
    ("api", "default", None, False),
    ("cron", "job", None, False),
    ("whatsapp", "56911111111", "whatsapp-own:56911111111", True),     # the owner, from his phone
    ("whatsapp", "56911111111", "whatsapp:56911111111", False),        # anybody else
])
def test_who_may_hand_off(channel, chat_id, key, ok):
    assert delegate.may_delegate(channel, chat_id, key) is ok


def test_the_task_is_what_the_person_wrote():
    assert delegate.task_text("recent history...\n" + RUNTIME + "cruzá las luces con HA") \
        == "cruzá las luces con HA"
    assert delegate.task_text([{"type": "text", "text": RUNTIME + "hola"}]) == "hola"


def test_what_was_already_done_travels_with_it():
    msgs = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "skill", "arguments": '{"skill": "grocery", "action": "add", "item": "leche"}'}}]}]
    note = delegate.already_done(msgs)
    assert "grocery" in note and "leche" in note and "Do not repeat" in note
    assert delegate.already_done([{"role": "user", "content": "x"}]) is None


def test_the_acknowledgement_is_in_the_house_language(monkeypatch):
    monkeypatch.setenv("SEARCH_LANGUAGE", "es")
    assert delegate.ack("long").startswith("Lo hago en segundo plano")
    monkeypatch.setenv("SEARCH_LANGUAGE", "de")
    assert "background" in delegate.ack("long")


# --- the plan tool -------------------------------------------------------------

def _spec(budget=40):
    return AgentRunSpec(initial_messages=[], tools=MagicMock(), model="m",
                        max_iterations=budget, max_tool_result_chars=1000)


@pytest.mark.asyncio
async def test_a_plan_buys_its_steps_calls_and_says_so():
    said = []

    async def progress(text):
        said.append(text)
    plan = plan_tool.TurnPlan(per_step=10, max_budget=200, spec=_spec(40), progress=progress)
    plan_tool.begin(plan)
    tool = plan_tool.PlanTool()
    out = await tool.execute("set", steps=["listar luces", "listar HA", "cruzar"])
    assert "3 steps" in out and plan.spec.max_iterations == 70
    out = await tool.execute("done", step=1, note="8 luces")
    assert "Next: step 2" in out
    assert said[0]["event"] == "set" and said[0]["steps"][0] == "listar luces"
    assert said[-1]["event"] == "done" and said[-1]["done"] == {"1": "8 luces"}


@pytest.mark.asyncio
async def test_the_budget_never_passes_its_ceiling():
    plan = plan_tool.TurnPlan(per_step=50, max_budget=90, spec=_spec(40))
    plan_tool.begin(plan)
    await plan_tool.PlanTool().execute("set", steps=[str(i) for i in range(10)])
    assert plan.spec.max_iterations == 90


@pytest.mark.asyncio
async def test_past_the_time_limit_the_rest_is_handed_off():
    plan = plan_tool.TurnPlan(limit_s=0.0, spec=_spec())
    plan_tool.begin(plan)
    tool = plan_tool.PlanTool()
    await tool.execute("set", steps=["a", "b", "c"])
    out = await tool.execute("done", step=1)
    assert plan.handoff and "continue in the background" in out
    assert [s for _i, s in plan.remaining()] == ["b", "c"]


@pytest.mark.asyncio
async def test_a_finished_plan_asks_for_the_answer():
    plan = plan_tool.TurnPlan(limit_s=0.0, spec=_spec())
    plan_tool.begin(plan)
    tool = plan_tool.PlanTool()
    await tool.execute("set", steps=["a"])
    out = await tool.execute("done", step=1)
    assert not plan.handoff and "Write the answer" in out


# --- the loop ------------------------------------------------------------------

def _classifier(label):
    p = mock_provider("tiny")

    async def chat(**kwargs):
        return LLMResponse(content='{"label": "%s", "reason": "t"}' % label)
    p.chat = chat
    return p


def _loop(tmp_path: Path, label: str) -> AgentLoop:
    loop = AgentLoop(bus=MagicMock(), provider=mock_provider("fast"), workspace=tmp_path, model="fast",
                     powerful_provider=mock_provider("pro"), powerful_model="pro",
                     classifier_provider=_classifier(label), classifier_model="tiny",
                     routing=RoutingConfig(mode="active"))
    loop.subagents.spawn = AsyncMock(return_value="started")
    return loop


def _msgs(text="cruzá las luces con Home Assistant"):
    return [{"role": "user", "content": RUNTIME + text}]


@pytest.mark.asyncio
async def test_background_work_is_handed_off_from_a_persons_chat(tmp_path, monkeypatch):
    loop = _loop(tmp_path, "background")
    run = AsyncMock()
    monkeypatch.setattr(loop.runner, "run", run)
    reply, _tools, msgs, stop, _inj = await loop._run_agent_loop(
        _msgs("en segundo plano, armame un informe"), session=Session(key=f"websocket:{CHAT}"),
        channel="websocket", chat_id=CHAT)
    assert stop == "delegated" and not run.called
    kw = loop.subagents.spawn.call_args.kwargs
    assert kw["task"] == "en segundo plano, armame un informe" and kw["session_key"] == f"websocket:{CHAT}"
    assert msgs[-1] == {"role": "assistant", "content": reply}


@pytest.mark.asyncio
async def test_background_work_on_voice_runs_here(tmp_path, monkeypatch):
    loop = _loop(tmp_path, "background")

    async def run(spec):
        return AgentRunResult(final_content="ok", messages=list(spec.initial_messages))
    monkeypatch.setattr(loop.runner, "run", run)
    _r, _t, _m, stop, _i = await loop._run_agent_loop(_msgs(), channel="voice", chat_id="cocina")
    assert stop != "delegated" and not loop.subagents.spawn.called


@pytest.mark.asyncio
async def test_long_work_stays_in_the_chat_with_a_plan(tmp_path, monkeypatch):
    loop = _loop(tmp_path, "long")
    seen = {}

    async def run(spec):
        seen["spec"] = spec
        await loop.tools.get("plan").execute("set", steps=["a", "b"])
        return AgentRunResult(final_content="listo", messages=list(spec.initial_messages))
    monkeypatch.setattr(loop.runner, "run", run)
    reply, *_ = await loop._run_agent_loop(_msgs(), session=Session(key=f"websocket:{CHAT}"),
                                           channel="websocket", chat_id=CHAT)
    spec = seen["spec"]
    assert reply == "listo" and not loop.subagents.spawn.called
    assert "Mode: this request takes several steps" in spec.initial_messages[-1]["content"]
    assert spec.max_iterations == loop._routing.everyday_iterations + 2 * loop._routing.plan_step_calls


@pytest.mark.asyncio
async def test_past_the_limit_the_steps_left_go_on_in_the_background(tmp_path, monkeypatch):
    loop = _loop(tmp_path, "long")
    loop._routing.plan_chat_seconds = 30

    async def run(spec):
        tool = loop.tools.get("plan")
        await tool.execute("set", steps=["listar luces", "listar HA", "cruzar"])
        plan_tool._current.get().started -= 3600          # an hour in
        await tool.execute("done", step=1, note="8 luces")
        return AgentRunResult(final_content="Listé las 8 luces.", messages=list(spec.initial_messages))
    monkeypatch.setattr(loop.runner, "run", run)
    reply, _t, _m, stop, _i = await loop._run_agent_loop(
        _msgs(), session=Session(key=f"websocket:{CHAT}"), channel="websocket", chat_id=CHAT)
    assert stop == "delegated" and reply.startswith("Listé las 8 luces.")
    ctx = loop.subagents.spawn.call_args.kwargs["context"]
    assert "✓ 1. listar luces -- 8 luces" in ctx and "· 2. listar HA" in ctx


@pytest.mark.asyncio
async def test_a_dead_end_is_handed_off_with_what_it_did(tmp_path, monkeypatch):
    loop = _loop(tmp_path, "action")

    async def run(spec):
        return AgentRunResult(final_content="x", stop_reason="repeated_tool_calls", messages=list(
            spec.initial_messages) + [{"role": "assistant", "tool_calls": [
                {"function": {"name": "exec", "arguments": '{"command": "echo hi"}'}}]}])
    monkeypatch.setattr(loop.runner, "run", run)
    pro = AsyncMock()
    monkeypatch.setattr(loop._powerful_runner, "run", pro)
    _r, _t, _m, stop, _i = await loop._run_agent_loop(
        _msgs("prendé la luz"), session=Session(key=f"websocket:{CHAT}"), channel="websocket", chat_id=CHAT)
    assert stop == "delegated" and not pro.called
    assert "echo hi" in loop.subagents.spawn.call_args.kwargs["context"]


@pytest.mark.asyncio
async def test_the_same_request_is_not_started_twice(tmp_path, monkeypatch):
    loop = _loop(tmp_path, "background")
    monkeypatch.setattr(loop.subagents, "is_running_task", lambda key, task: True)
    reply, *_ = await loop._run_agent_loop(_msgs("armame un informe"), session=Session(key=f"websocket:{CHAT}"),
                                           channel="websocket", chat_id=CHAT)
    assert not loop.subagents.spawn.called and reply == delegate.ack("already")


# --- two at a time -------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_more_than_two_sub_agents_run_at_once(tmp_path, monkeypatch):
    from nanobot.agent import subagent as S
    monkeypatch.setattr(S, "MAX_CONCURRENT", 2)
    m = S.SubagentManager(provider=MagicMock(), workspace=tmp_path, bus=MagicMock(),
                          max_tool_result_chars=1000, model="m")
    running, peak = 0, 0

    async def work():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
    statuses = [MagicMock(phase="") for _ in range(4)]
    await asyncio.gather(*(m._gated(st, work()) for st in statuses))
    assert peak == 2
    assert any(st.phase == "queued" for st in statuses)


# --- the everyday model plans, the local one carries out the steps ---------------

def _loop_with_local(tmp_path: Path, executor="subagent") -> AgentLoop:
    loop = AgentLoop(bus=MagicMock(), provider=mock_provider("flash"), workspace=tmp_path, model="flash",
                     subagent_provider=mock_provider("qwen"), subagent_model="qwen3.5:9b",
                     classifier_provider=_classifier("long"), classifier_model="tiny",
                     routing=RoutingConfig(mode="active", plan_executor=executor))
    loop.subagents.spawn = AsyncMock(return_value="started")
    return loop


def _runs(monkeypatch, local_result):
    """AgentRunner.run, answering by which provider it runs on."""
    from nanobot.agent import runner as R
    seen = []
    real = R.AgentRunner.run

    async def run(self, spec):
        seen.append((spec.model, spec))
        if spec.model == "qwen3.5:9b":
            return local_result(spec)
        return AgentRunResult(final_content=f"flash did: {spec.initial_messages[-1]['content'][-40:]}",
                              messages=list(spec.initial_messages), tools_used=["exec"])
    monkeypatch.setattr(R.AgentRunner, "run", run)
    return seen


@pytest.mark.asyncio
async def test_a_step_runs_on_the_local_model_with_only_what_it_needs(tmp_path, monkeypatch):
    loop = _loop_with_local(tmp_path)
    seen = _runs(monkeypatch, lambda spec: AgentRunResult(
        final_content="8 luces: Oficina Tomi, Luz Paula...", messages=list(spec.initial_messages),
        tools_used=["skill"]))
    plan = plan_tool.TurnPlan(spec=_spec())
    plan.executor = loop._step_runner(plan, [{"role": "system", "content": "SYS"},
                                             {"role": "user", "content": RUNTIME + "cruzá las luces con HA"}], None)
    plan_tool.begin(plan)
    tool = loop.tools.get("plan")
    await tool.execute("set", steps=["listar luces", "listar HA", "cruzar"])
    out = await tool.execute("run", step=1)
    model, spec = seen[-1]
    assert model == "qwen3.5:9b" and "8 luces" in out and "Next: step 2" in out
    # The slim step prompt, not the turn's own system message.
    system = spec.initial_messages[0]["content"]
    assert system != "SYS" and "## Workspace" in system and "SOUL" not in system
    assert spec.read_only is True
    prompt = spec.initial_messages[-1]["content"]
    assert "cruzá las luces con HA" in prompt and "Do step 1 now" in prompt and "Current Time" in prompt
    assert "plan" not in spec.tools.tool_names and "spawn" not in spec.tools.tool_names
    assert plan.by[1] == "qwen3.5:9b" and plan.tools[1] == ["skill"]


@pytest.mark.asyncio
async def test_a_step_that_dead_ends_locally_is_done_again_on_the_everyday_model(tmp_path, monkeypatch):
    loop = _loop_with_local(tmp_path)
    seen = _runs(monkeypatch, lambda spec: AgentRunResult(
        final_content="", stop_reason="repeated_tool_calls", messages=list(spec.initial_messages) + [
            {"role": "assistant", "tool_calls": [{"function": {"name": "exec", "arguments": '{"command":"echo x"}'}}]}]))
    plan = plan_tool.TurnPlan(spec=_spec())
    plan.executor = loop._step_runner(plan, [{"role": "user", "content": RUNTIME + "x"}], None)
    plan_tool.begin(plan)
    tool = loop.tools.get("plan")
    await tool.execute("set", steps=["a"])
    await tool.execute("run", step=1)
    assert [m for m, _ in seen] == ["qwen3.5:9b", "flash"]
    assert "echo x" in seen[-1][1].initial_messages[-1]["content"]     # what already ran
    assert plan.by[1].startswith("flash (after qwen3.5:9b)")


@pytest.mark.asyncio
async def test_everyday_can_be_told_to_do_the_steps_itself(tmp_path, monkeypatch):
    loop = _loop_with_local(tmp_path, executor="everyday")
    seen = _runs(monkeypatch, lambda spec: AgentRunResult(final_content="no", messages=[]))
    plan = plan_tool.TurnPlan(spec=_spec())
    plan.executor = loop._step_runner(plan, [{"role": "user", "content": RUNTIME + "x"}], None)
    plan_tool.begin(plan)
    tool = loop.tools.get("plan")
    await tool.execute("set", steps=["a"])
    await tool.execute("run", step=1)
    assert [m for m, _ in seen] == ["flash"]


@pytest.mark.asyncio
async def test_the_checklist_shows_the_step_running_and_who_ran_it(tmp_path, monkeypatch):
    loop = _loop_with_local(tmp_path)
    _runs(monkeypatch, lambda spec: AgentRunResult(final_content="ok", messages=[], tools_used=["skill"]))
    said = []

    async def progress(state):
        said.append(state)
    plan = plan_tool.TurnPlan(spec=_spec(), progress=progress)
    plan.executor = loop._step_runner(plan, [{"role": "user", "content": RUNTIME + "x"}], None)
    plan_tool.begin(plan)
    tool = loop.tools.get("plan")
    await tool.execute("set", steps=["a", "b"])
    await tool.execute("run", step=1)
    events = [(s["event"], s["running"]) for s in said]
    assert events == [("set", None), ("run", 1), ("done", None)]
    assert said[-1]["by"] == {"1": "qwen3.5:9b"} and said[-1]["tools"] == {"1": ["skill"]}


@pytest.mark.asyncio
async def test_the_planner_and_the_steps_can_each_be_chosen(tmp_path, monkeypatch):
    """Models page: "Planner" and "Plan steps" -- the turn plans on one, its
    steps run on the other, and a step that dead-ends falls back to the planner."""
    loop = AgentLoop(bus=MagicMock(), provider=mock_provider("flash"), workspace=tmp_path, model="flash",
                     subagent_provider=mock_provider("qwen"), subagent_model="qwen3.5:9b",
                     plan_provider=mock_provider("pro"), plan_model="planner-model",
                     plan_step_provider=mock_provider("gemma"), plan_step_model="gemma4:e4b",
                     classifier_provider=_classifier("long"), classifier_model="tiny",
                     routing=RoutingConfig(mode="active"))
    from nanobot.agent import runner as R
    seen = []

    async def run(self, spec):
        seen.append(spec.model)
        if spec.model == "planner-model" and len(seen) == 1:
            tool = loop.tools.get("plan")
            await tool.execute("set", steps=["a"])
            await tool.execute("run", step=1)
        return AgentRunResult(final_content="ok", messages=list(spec.initial_messages))
    monkeypatch.setattr(R.AgentRunner, "run", run)
    await loop._run_agent_loop(_msgs(), session=Session(key=f"websocket:{CHAT}"),
                               channel="websocket", chat_id=CHAT)
    assert seen == ["planner-model", "gemma4:e4b"], seen


@pytest.mark.asyncio
async def test_a_long_step_keeps_the_turn_alive(monkeypatch):
    """A step on a local model took 70 s in silence and the API server handed the
    turn to a sub-agent mid-plan (2026-09-24). The checklist is re-sent while it runs."""
    monkeypatch.setattr(plan_tool, "HEARTBEAT_S", 0.01)
    said = []

    async def progress(state):
        said.append(state["event"])

    async def slow(k):
        await asyncio.sleep(0.06)
        return {"text": "ok", "by": "qwen", "tools": []}
    plan = plan_tool.TurnPlan(spec=_spec(), progress=progress, executor=slow)
    plan_tool.begin(plan)
    tool = plan_tool.PlanTool()
    await tool.execute("set", steps=["a"])
    await tool.execute("run", step=1)
    assert said.count("run") >= 3 and said[-1] == "done", said


@pytest.mark.asyncio
async def test_a_step_that_called_nothing_is_marked_unchecked():
    # 2026-09-24: step 3 matched the lights to HA without a tool call -- four
    # WiZ bulbs "were Zigbee repeaters" -- and the answer called them tested.
    results = {1: {"text": "8 lights", "by": "qwen", "tools": ["exec"]},
               2: {"text": "they are repeaters", "by": "qwen", "tools": []}}

    async def run(k):
        return results[k]
    plan = plan_tool.TurnPlan(spec=_spec(), executor=run)
    plan_tool.begin(plan)
    tool = plan_tool.PlanTool()
    await tool.execute("set", steps=["list", "match"])
    first = await tool.execute("run", step=1)
    second = await tool.execute("run", step=2)
    assert "called no tools" not in first
    assert "called no tools" in second and "not checked data" in second
    assert "nothing was tested" in second


def test_a_step_is_told_not_to_guess_and_not_to_ask(tmp_path):
    import inspect
    src = inspect.getsource(AgentLoop._step_runner)
    assert "Never fill a gap with a likely guess" in src
    assert "do not act and do not ask" in src


def test_the_step_prompt_is_small_and_names_what_the_plan_names(tmp_path):
    from nanobot.agent.context import ContextBuilder
    (tmp_path / "SOUL.md").write_text("zzsoulzz " * 4000)
    (tmp_path / "AGENTS.md").write_text("zzrulezz " * 3000)
    (tmp_path / "TOOLS.md").write_text("tool notes")
    cb = ContextBuilder(tmp_path)
    full, slim = cb.build_system_prompt(), cb.build_step_system_prompt("list the lights")
    assert "zzsoulzz" in full and "zzsoulzz" not in slim and "zzrulezz" not in slim
    assert "tool notes" in slim and len(slim) < len(full) / 2


def test_only_hass_intents_that_act_are_left_out_of_a_reading_step():
    from nanobot.agent.loop import _changes_things
    for name in ("mcp_homeassistant_intent__HassTurnOn", "mcp_homeassistant_light__HassLightSet",
                 "mcp_homeassistant_media_player__HassSetVolume",
                 "mcp_homeassistant_assist_satellite__HassBroadcast"):
        assert _changes_things(name), name
    for name in ("mcp_homeassistant_homeassistant__GetLiveContext",
                 "mcp_homeassistant_intent__HassGetState", "exec", "read_file",
                 "mcp_brightdata_search_engine"):
        assert not _changes_things(name), name


def test_a_reading_step_may_run_reads_and_nothing_with_a_go_ahead():
    from nanobot.agent.runner import is_read_action
    for inv in ({"action": "list_lights"}, {"action": "get_state"}, {"action": "find_in_ha", "device": "x"},
                {"action": "room_from_ha"}, {"action": "list_ha_lights"}):
        assert is_read_action(inv), inv
    for inv in ({"action": "turn_on", "device": "x"}, {"action": "flash_light"}, {"action": "toggle(x)"},
                {"action": "find_in_ha", "device": "x", "confirmed": True},
                {"action": "room_from_ha", "apply": True}, {"action": "add_grocery"}, {}):
        assert not is_read_action(inv), inv


@pytest.mark.asyncio
async def test_the_planner_names_the_steps_that_act(tmp_path, monkeypatch):
    loop = _loop_with_local(tmp_path)
    seen = _runs(monkeypatch, lambda spec: AgentRunResult(final_content="ok",
                                                          messages=list(spec.initial_messages),
                                                          tools_used=["x"]))
    plan = plan_tool.TurnPlan(spec=_spec())
    plan.executor = loop._step_runner(plan, [{"role": "user", "content": RUNTIME + "flash each light"}], None)
    plan_tool.begin(plan)
    tool = loop.tools.get("plan")
    said = await tool.execute("set", steps=["list", "flash each"], acts=[2, 9])
    assert plan.acts == {2} and "only reads" not in said
    await tool.execute("run", step=1)
    assert seen[-1][1].read_only is True
    await tool.execute("run", step=2)
    assert seen[-1][1].read_only is False
    said = await tool.execute("set", steps=["list"])
    assert plan.acts == set() and "only reads" in said


def test_a_go_ahead_counts_only_after_the_person_answered_a_pending():
    # deepseek sent find_in_ha confirmed:true for eight bulbs in the same turn,
    # reasoning "you asked to test" (2026-09-24).
    from nanobot.agent.runner import _person_answered_pending
    asked = [{"role": "user", "content": "probá las luces"}]
    assert not _person_answered_pending(asked)
    pending = asked + [{"role": "tool", "content": '{\n  "pending": true,\n  "would_switch": ["Entrada"]\n}'}]
    assert not _person_answered_pending(pending)
    pending += [{"role": "assistant", "content": "¿Las cambio?"}, {"role": "user", "content": "sí"}]
    assert _person_answered_pending(pending)


@pytest.mark.asyncio
async def test_a_plan_that_outgrows_the_chat_turn_finishes_in_the_background(tmp_path, monkeypatch):
    loop = _loop_with_local(tmp_path)
    loop._routing.plan_detach_seconds = 0.05
    sent = []

    async def publish(msg):
        sent.append(msg)
    monkeypatch.setattr(loop.bus, "publish_outbound", publish)
    release = asyncio.Event()

    async def slow_result():
        await release.wait()
        return AgentRunResult(final_content="| luz | HA |\n| Oficina | Luz Oficina |", messages=[])
    task = asyncio.ensure_future(slow_result())
    plan = plan_tool.TurnPlan(spec=_spec())
    plan.steps = ["list", "match"]
    from nanobot.agent.classify import TurnClass
    reply, _, _, stop, _ = await loop._detach_plan(
        task, plan, [{"role": "user", "content": "x"}], None, "websocket",
        "homeweb:1:2026-09-24:1", TurnClass("long", "everyday", "", "model"))
    assert stop == "delegated" and "background" in reply or "segundo plano" in reply
    assert plan.limit_s == float("inf") and not sent
    release.set()
    for _ in range(50):
        if sent:
            break
        await asyncio.sleep(0.01)
    assert sent and "Luz Oficina" in sent[-1].content
    saved = loop.sessions.get_or_create("websocket:homeweb:1:2026-09-24:1").messages
    assert saved and "Luz Oficina" in saved[-1]["content"]


def test_a_step_asks_for_its_calls_together_and_has_room_for_them():
    import inspect
    from nanobot.config.schema import RoutingConfig
    assert RoutingConfig().plan_step_calls >= 24
    assert "make all those calls" in inspect.getsource(AgentLoop._step_runner)


def test_a_named_always_skill_keeps_its_path_line_in_the_step_prompt(tmp_path):
    # Without it the interceptor could not map {"skill": "chores"} to its
    # SKILL.md, and every chores step ended "bad_invocation" (2026-09-25).
    from nanobot.agent.context import ContextBuilder
    cb = ContextBuilder(tmp_path)
    always = cb.skills.get_always_skills()
    if not always:
        pytest.skip("no always-loaded skill in the bundled set")
    name = always[0]
    slim = cb.build_step_system_prompt(f"call skill {name}")
    assert "# Active Skill Paths" in slim
    assert cb.skills.build_skills_summary(only={name}).strip() in slim


@pytest.mark.asyncio
async def test_a_plan_step_has_no_shell_of_its_own():
    # Bonsai 2 spent 7-12 hand-written shell calls per step (2026-09-25). A
    # step's registry offers no exec and runs only the runtime's skill calls.
    from nanobot.agent.skill_invocation import register_skill_translation
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.shell import ExecTool
    reg = ToolRegistry()
    reg.register(ExecTool())
    reg.shell_for_skills_only = True
    assert all(d["function"]["name"] != "exec" for d in reg.get_definitions())
    said = await reg.execute("exec", {"command": "echo hi"})
    assert "no shell in a plan step" in said
    register_skill_translation("echo from-a-skill")
    assert "no shell" not in str(await reg.execute("exec", {"command": "echo from-a-skill"}))


def test_a_step_is_offered_only_the_tools_its_text_calls_for():
    # With all of Alfred's tools, Bonsai 2 answered "flash each light" by
    # scraping example.com and a chores step by writing files (2026-09-25).
    from nanobot.agent.loop import tools_for_step
    names = ["read_file", "write_file", "edit_file", "grep", "exec", "message", "cron", "plan",
             "web_search", "web_fetch", "mcp_brightdata_scrape_as_markdown",
             "mcp_homeassistant_GetLiveContext", "mcp_homeassistant_HassTurnOn",
             "mcp_homeassistant_homeassistant__GetDateTime"]
    assert tools_for_step("Flash each light with the lights skill (flash_light)", names) == ["read_file"]
    assert tools_for_step("List the chores for today with the chores skill", names) == ["read_file"]
    ha = tools_for_step("Listar las luces de Home Assistant (entity, área)", names)
    assert "mcp_homeassistant_GetLiveContext" in ha and "web_search" not in ha
    assert "mcp_homeassistant_GetLiveContext" in tools_for_step("Call GetLiveContext for lights", names)
    web = tools_for_step("Buscar en la web el pronóstico de mañana", names)
    assert {"web_search", "web_fetch"} <= set(web) and "mcp_brightdata_scrape_as_markdown" not in web
    assert "cron" in tools_for_step("Agendar reintento del draw en 5 minutos", names)
    # Named outright, anything but the plan itself and the shell.
    named = tools_for_step("scrape_as_markdown the page, then write_file the notes", names)
    assert "mcp_brightdata_scrape_as_markdown" in named and "write_file" in named
    assert "exec" not in tools_for_step("exec the script", names)


def test_cron_and_files_change_things():
    from nanobot.agent.loop import _changes_things
    for name in ("cron", "message", "write_file", "edit_file", "mcp_homeassistant_HassTurnOn"):
        assert _changes_things(name), name
    assert not _changes_things("mcp_homeassistant_todo_get_items")


@pytest.mark.asyncio
async def test_a_step_stopped_from_acting_tells_the_planner(tmp_path, monkeypatch):
    # A read-only step that scheduled a retry reported "scheduled", and the
    # planner scheduled another (2026-09-24). What the runtime stopped is said
    # by the runtime.
    loop = _loop_with_local(tmp_path)

    def local(spec):
        spec.refused.append("lights.turn_on")
        return AgentRunResult(final_content="Turned the lamp on.", messages=list(spec.initial_messages),
                              tools_used=["skill"])
    seen = _runs(monkeypatch, local)
    plan = plan_tool.TurnPlan(spec=_spec())
    plan.executor = loop._step_runner(plan, [{"role": "user", "content": RUNTIME + "the lamp"}], None)
    plan_tool.begin(plan)
    tool = loop.tools.get("plan")
    await tool.execute("set", steps=["look at the lamp with the lights skill"])
    out = await tool.execute("run", step=1)
    assert "Not done" in out and "lights.turn_on" in out and "step 1 in acts" in out
    # The step model saw only what the step called for; no acting tool either way.
    names = seen[-1][1].tools.tool_names
    assert "cron" not in names and "write_file" not in names and "web_search" not in names


def test_the_runtimes_own_notes_pass_the_step_shell_guard():
    import asyncio as _a
    from nanobot.agent.runner import _runtime_printf
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.shell import ExecTool
    reg = ToolRegistry()
    reg.register(ExecTool())
    reg.shell_for_skills_only = True
    said = _a.run(reg.execute("exec", {"command": _runtime_printf("This step only reads.")}))
    assert "This step only reads." in str(said) and "no shell" not in str(said)


def test_a_skill_a_step_names_is_in_its_prompt_whole(tmp_path):
    # Bonsai 2 read the lights SKILL.md seven times in one step and never
    # called it (2026-09-25): a skill the planner named comes loaded.
    from nanobot.agent.context import ContextBuilder
    skill = tmp_path / "skills" / "lamps"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: lamps\ndescription: the lamps\n---\n\nzzlampguidezz\n")
    cb = ContextBuilder(tmp_path)
    assert "zzlampguidezz" in cb.build_step_system_prompt("Call skill lamps, action list_lamps.")
    assert "zzlampguidezz" in cb.build_step_system_prompt("Flash each with the lamps skill.")
    # A word that happens to be a skill's name is not a step calling it.
    assert "zzlampguidezz" not in cb.build_step_system_prompt("turn the lamps off")


@pytest.mark.asyncio
async def test_a_reading_step_is_told_so_before_it_starts(tmp_path, monkeypatch):
    loop = _loop_with_local(tmp_path)
    seen = _runs(monkeypatch, lambda spec: AgentRunResult(final_content="ok",
                                                          messages=list(spec.initial_messages),
                                                          tools_used=["x"]))
    plan = plan_tool.TurnPlan(spec=_spec())
    plan.executor = loop._step_runner(plan, [{"role": "user", "content": RUNTIME + "the lamp"}], None)
    plan_tool.begin(plan)
    tool = loop.tools.get("plan")
    await tool.execute("set", steps=["Call skill lights, action turn_on", "flash it"], acts=[2])
    await tool.execute("run", step=1)
    assert "may only READ" in seen[-1][1].initial_messages[-1]["content"]
    await tool.execute("run", step=2)
    assert "may only READ" not in seen[-1][1].initial_messages[-1]["content"]

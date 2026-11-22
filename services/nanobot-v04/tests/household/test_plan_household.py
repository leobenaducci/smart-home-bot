"""household: plans 1/4, the planner (fork ``tests/agent/test_plan_and_delegate.py``).

The plan tool's own tests are the fork's, against ``household/plan.py``. The
loop tests drive upstream's ``process_direct``: a ``long`` label runs on
``planModel``, the plan tool grows the run's call budget, and a plan never
outlives its turn. The step runner is tested in test_plan_steps.py.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from agent.conftest import make_provider

from nanobot.agent import runner as R
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config
from nanobot.household import plan as plan_tool
from nanobot.household import turn_router
from nanobot.household.loop import HouseholdAgentLoop
from nanobot.household.runtime import RUNTIME
from nanobot.providers.base import LLMResponse, ToolCallRequest

# --- the plan tool (the fork's tests) -----------------------------------------


def _spec(budget=40):
    return SimpleNamespace(max_iterations=budget)


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


@pytest.mark.asyncio
async def test_a_long_step_keeps_the_turn_alive(monkeypatch):
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


@pytest.mark.asyncio
async def test_with_no_spec_the_plan_grows_the_run_in_progress():
    run_spec = _spec(40)
    token = R.CURRENT_RUN_SPEC.set(run_spec)
    try:
        plan = plan_tool.TurnPlan(per_step=12, max_budget=160)
        plan_tool.begin(plan)
        await plan_tool.PlanTool().execute("set", steps=["a", "b"])
    finally:
        R.CURRENT_RUN_SPEC.reset(token)
    assert run_spec.max_iterations == 64 and plan.spec is None


@pytest.mark.asyncio
async def test_outside_a_plan_the_tool_says_to_just_do_it():
    token = plan_tool.begin(None)
    try:
        out = await plan_tool.PlanTool().execute("set", steps=["a"])
    finally:
        plan_tool.end(token)
    assert "no plan in this turn" in out


# --- the loop -----------------------------------------------------------------


def _loop(tmp_path, replies_by_model):
    provider = make_provider(default_model="cheap")
    provider.provider_name = "custom"
    calls: list[str] = []

    async def chat(**kw):
        model = kw.get("model")
        calls.append(model)
        reply = replies_by_model[model]
        return reply() if callable(reply) else reply

    provider.chat_with_retry = chat
    provider.chat_stream_with_retry = chat
    provider.chat = chat
    loop = HouseholdAgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                              model="cheap", context_window_tokens=128_000)
    return loop, calls


def _config(plan_model="planner"):
    turn_router._CLASSIFIERS.clear()
    d = {"model": "cheap", "provider": "custom", "modelPowerful": "strong",
         "classifierModel": "tiny",
         "routing": {"mode": "active", "planStepCalls": 7, "complexIterations": 300}}
    if plan_model:
        d["planModel"] = plan_model
    return Config.model_validate({"agents": {"defaults": d}})


def _planner_replies(seen_budgets):
    """The planner sets two steps, does them itself, then answers."""
    script = iter([
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="c1", name="plan", arguments={"action": "set", "steps": ["listar luces", "cruzar"]})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="c2", name="plan", arguments={"action": "done", "step": 1, "note": "8 luces"})]),
        LLMResponse(content="Listo: 8 luces, todas en HA."),
    ])

    def reply():
        spec = R.CURRENT_RUN_SPEC.get()
        seen_budgets.append(spec.max_iterations if spec else None)
        return next(script)
    return reply


@pytest.mark.asyncio
async def test_a_long_label_is_planned_on_the_planner_model(tmp_path):
    budgets: list[int | None] = []
    loop, calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "long", "reason": "several systems"}'),
        "planner": _planner_replies(budgets),
    })
    RUNTIME.config = _config()
    out = await loop.process_direct("cruzá las luces de Oficina Tomi con HA",
                                    session_key="whatsapp-own:56999999999",
                                    channel="whatsapp", chat_id="56999999999")
    assert out.content == "Listo: 8 luces, todas en HA."
    assert calls == ["tiny", "planner", "planner", "planner"]
    base = RUNTIME.config.agents.defaults.routing.everyday_iterations   # A4: the cheap budget
    assert budgets[0] == base and budgets[1:] == [base + 14, base + 14]
    tool_results = [m["content"] for m in loop.sessions.get_or_create(
        "whatsapp-own:56999999999").messages if m.get("role") == "tool"]
    assert any("Plan saved, 2 steps" in str(c) for c in tool_results)


@pytest.mark.asyncio
async def test_without_a_planner_a_long_label_stays_on_the_everyday_model(tmp_path):
    loop, calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "long", "reason": "x"}'),
        "cheap": LLMResponse(content="ok"),
    })
    RUNTIME.config = _config(plan_model=None)
    await loop.process_direct("hacé varias cosas", session_key="email:tomi@example.org",
                              channel="email", chat_id="tomi@example.org")
    assert calls == ["tiny", "cheap"]


@pytest.mark.asyncio
async def test_other_labels_never_reach_the_planner(tmp_path):
    loop, calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "action", "reason": "x"}'),
        "cheap": LLMResponse(content="Prendida."),
    })
    RUNTIME.config = _config()
    await loop.process_direct("prendé Luz Paula", session_key="cli:x")
    assert calls == ["tiny", "cheap"]


@pytest.mark.asyncio
async def test_a_plan_never_outlives_its_turn(tmp_path):
    seen: list[plan_tool.TurnPlan | None] = []

    def reply():
        seen.append(plan_tool._current.get())
        return LLMResponse(content="ok")
    loop, _calls = _loop(tmp_path, {"tiny": LLMResponse(content='{"label": "chat"}'),
                                    "cheap": reply})
    RUNTIME.config = _config()
    await loop.process_direct("hola", session_key="cli:x")
    await loop.process_direct("chau", session_key="cli:x")
    assert all(p is not None for p in seen) and seen[0] is not seen[1]
    assert plan_tool._current.get() is None


def test_the_plan_tool_is_the_main_loops_only(tmp_path):
    loop, _calls = _loop(tmp_path, {})
    assert loop.tools.has("plan")
    assert not loop.subagents._build_tools().has("plan")


def test_a_voice_turn_has_no_time_limit(tmp_path):
    loop, _calls = _loop(tmp_path, {})
    RUNTIME.config = _config()

    def ctx(channel, chat_id, key):
        return SimpleNamespace(msg=SimpleNamespace(channel=channel, chat_id=chat_id), session_key=key,
                               attributes={}, runtime=None)
    chat = loop._turn_plan(ctx("whatsapp", "56999999999", "whatsapp-own:56999999999"))
    voice = loop._turn_plan(ctx("voice", "cocina", "voice:cocina"))
    assert chat.limit_s == 240.0 and voice.limit_s == float("inf")   # A4b: min(chat 300, detach 240)
    assert chat.per_step == 7 and chat.max_budget == max(loop.max_iterations, 300)


# --- A4: a `long` turn is told to plan, the cheap budget, handoff, the greeting --


def _spawned(monkeypatch):
    from nanobot.household import api_adapter

    calls: list[dict] = []

    async def spawn(self, task, **kw):
        calls.append({"task": task, **kw})
        return "started"
    monkeypatch.setattr(api_adapter._Subagents, "spawn", spawn)
    return calls


@pytest.mark.asyncio
async def test_a_long_turn_is_told_to_plan_and_history_keeps_only_the_words(tmp_path):
    seen: list[str] = []

    def reply(**_kw):
        return LLMResponse(content="ok")
    loop, _calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "long", "reason": "x"}'), "planner": reply})
    RUNTIME.config = _config()
    real = loop.provider.chat_with_retry

    async def chat(**kw):
        seen.append(str(kw["messages"][-1]["content"]))
        return await real(**kw)
    loop.provider.chat_with_retry = loop.provider.chat_stream_with_retry = loop.provider.chat = chat
    await loop.process_direct("cruzá las luces con HA", session_key="cli:x")
    assert "Mode: this request takes several steps" in seen[-1]
    users = [m["content"] for m in loop.sessions.get_or_create("cli:x").messages if m["role"] == "user"]
    assert users and all("Mode:" not in str(u) for u in users)


@pytest.mark.asyncio
async def test_a_cheap_turn_gets_the_cheap_budget_and_a_plan_buys_more(tmp_path):
    budgets: list[int | None] = []
    loop, _calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "long", "reason": "x"}'),
        "planner": _planner_replies(budgets),
    })
    cfg = _config()
    cfg.agents.defaults.routing.everyday_iterations = 40
    RUNTIME.config = cfg
    await loop.process_direct("cruzá las luces", session_key="cli:x")
    assert budgets == [40, 54, 54]          # 40, then two steps at planStepCalls 7


@pytest.mark.asyncio
async def test_a_powerful_turn_keeps_the_loops_budget(tmp_path):
    budgets: list[int | None] = []

    def reply(**_kw):
        spec = R.CURRENT_RUN_SPEC.get()
        budgets.append(spec.max_iterations if spec else None)
        return LLMResponse(content="ok")
    loop, _calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "complex", "reason": "x"}'), "strong": reply})
    RUNTIME.config = _config()
    await loop.process_direct("¿cuotas o contado?", session_key="cli:x")
    assert budgets == [loop.max_iterations]


@pytest.mark.asyncio
async def test_past_the_limit_the_steps_left_go_on_in_the_background(tmp_path, monkeypatch):
    spawned = _spawned(monkeypatch)
    script = iter([
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="c1", name="plan", arguments={"action": "set", "steps": ["listar luces", "listar HA", "cruzar"]})]),
        "an hour in",
        LLMResponse(content="Listé las 8 luces."),
    ])

    def planner():
        nxt = next(script)
        if nxt == "an hour in":
            plan_tool._current.get().started -= 3600
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id="c2", name="plan", arguments={"action": "done", "step": 1, "note": "8 luces"})])
        return nxt
    loop, _calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "long", "reason": "x"}'), "planner": planner})
    RUNTIME.config = _config()
    out = await loop.process_direct("cruzá las luces con HA", session_key="whatsapp-own:56999999999",
                                    channel="whatsapp", chat_id="56999999999")
    assert out.content.startswith("Listé las 8 luces.")
    assert "background" in out.content or "segundo plano" in out.content
    ctx = spawned[0]["context"]
    assert "✓ 1. listar luces -- 8 luces" in ctx and "· 2. listar HA" in ctx
    assert spawned[0]["task"] == "cruzá las luces con HA"
    saved = loop.sessions.get_or_create("whatsapp-own:56999999999").messages
    assert saved[-1]["role"] == "assistant" and saved[-1]["content"] == out.content


@pytest.mark.asyncio
async def test_on_voice_a_late_plan_runs_to_its_end(tmp_path, monkeypatch):
    spawned = _spawned(monkeypatch)
    script = iter([
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="c1", name="plan", arguments={"action": "set", "steps": ["a", "b"]})]),
        "an hour in",
        LLMResponse(content="Listo."),
    ])

    def planner():
        nxt = next(script)
        if nxt == "an hour in":
            plan_tool._current.get().started -= 3600
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id="c2", name="plan", arguments={"action": "done", "step": 1})])
        return nxt
    loop, _calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "long", "reason": "x"}'), "planner": planner})
    RUNTIME.config = _config()
    out = await loop.process_direct("hacé a y b", session_key="voice:cocina",
                                    channel="voice", chat_id="cocina")
    assert out.content == "Listo." and not spawned


@pytest.mark.asyncio
async def test_an_inline_turn_is_neither_routed_nor_planned(tmp_path, monkeypatch):
    """The morning greeting (fork inline=True): the classifier would call it
    background work; it is answered here, on the everyday model."""
    from nanobot.household.loop import INLINE_ATTR

    spawned = _spawned(monkeypatch)
    seen: list[str] = []
    loop, calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "background", "reason": "x"}'),
        "cheap": LLMResponse(content="Buen día, Tomi."),
    })
    real = loop.provider.chat_with_retry

    async def chat(**kw):
        seen.append(str(kw["messages"][-1]["content"]))
        return await real(**kw)
    loop.provider.chat_with_retry = loop.provider.chat_stream_with_retry = loop.provider.chat = chat
    RUNTIME.config = _config()
    out = await loop.process_direct("Escribí el saludo de la mañana con el clima y las tareas",
                                    session_key="websocket:homeweb:999000111:2026-11-21:g",
                                    channel="websocket", chat_id="homeweb:999000111:2026-11-21:g",
                                    attributes={INLINE_ATTR: True})
    assert out.content == "Buen día, Tomi." and calls == ["cheap"] and not spawned
    assert "Mode:" not in seen[-1]


# --- A4b: a step may not outlive the time limit ---------------------------------


@pytest.mark.asyncio
async def test_a_step_still_running_at_the_limit_is_stopped():
    cancelled = asyncio.Event()

    async def slow(k):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"text": "late", "by": "qwen", "tools": []}
    plan = plan_tool.TurnPlan(limit_s=0.05, spec=_spec(), executor=slow)
    plan_tool.begin(plan)
    tool = plan_tool.PlanTool()
    await tool.execute("set", steps=["a", "b"])
    out = await tool.execute("run", step=1)
    assert out == plan_tool.TIME_UP and plan.handoff and plan.interrupted == 1
    assert cancelled.is_set() and plan.running is None and 1 not in plan.done


@pytest.mark.asyncio
async def test_a_step_interrupted_at_the_limit_is_handed_off_with_what_was_found(tmp_path, monkeypatch):
    spawned = _spawned(monkeypatch)
    planner = iter([
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="c1", name="plan", arguments={"action": "set", "steps": ["listar luces", "listar HA", "cruzar"]})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="c2", name="plan", arguments={"action": "run", "step": 1})]),
        "tighten",
        LLMResponse(content="Listé las luces; sigo con HA."),
    ])
    step_calls = 0

    async def chat(**kw):
        nonlocal step_calls
        model = kw.get("model")
        if model == "tiny":
            return LLMResponse(content='{"label": "long", "reason": "x"}')
        if model == "qwen3.5:9b":
            step_calls += 1
            if step_calls > 1:
                await asyncio.sleep(5)            # step 2 outlives the limit
            return LLMResponse(content="8 luces: Oficina Tomi, Luz Paula")
        nxt = next(planner)
        if nxt == "tighten":
            plan_tool._current.get().limit_s = (time.monotonic() - plan_tool._current.get().started) + 0.1
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                id="c3", name="plan", arguments={"action": "run", "step": 2})])
        return nxt

    provider = make_provider(default_model="cheap")
    provider.provider_name = "custom"
    provider.chat_with_retry = provider.chat_stream_with_retry = provider.chat = chat
    loop = HouseholdAgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                              model="cheap", context_window_tokens=128_000)
    cfg = _config(plan_model=None)
    cfg.agents.defaults.subagent_model = "qwen3.5:9b"
    RUNTIME.config = cfg
    out = await loop.process_direct("cruzá las luces con HA", session_key="whatsapp-own:56999999999",
                                    channel="whatsapp", chat_id="56999999999")
    assert out.content.startswith("Listé las luces; sigo con HA.")
    sent = spawned[0]
    assert sent["harness"] is False
    assert "Step 1 (listar luces) found:\n8 luces: Oficina Tomi, Luz Paula" in sent["context"]
    assert "Step 2 was stopped part-way" in sent["context"]
    assert "· 2. listar HA" in sent["context"] and "· 3. cruzar" in sent["context"]


# --- A4c: the checklist reaches the page ----------------------------------------


def _two_step_planner():
    return iter([
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="c1", name="plan", arguments={"action": "set", "steps": ["listar luces", "cruzar"]})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="c2", name="plan", arguments={"action": "run", "step": 1})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="c3", name="plan", arguments={"action": "run", "step": 2})]),
        LLMResponse(content="Listo."),
    ])


@pytest.mark.asyncio
async def test_the_checklist_goes_to_a_caller_that_takes_it(tmp_path):
    script = _two_step_planner()
    loop, _calls = _loop(tmp_path, {
        "tiny": LLMResponse(content='{"label": "long", "reason": "x"}'),
        "planner": lambda: next(script),
        "qwen": LLMResponse(content="8 luces"),
    })
    RUNTIME.config = _config()
    RUNTIME.config.agents.defaults.subagent_model = "qwen"
    states: list[dict] = []

    async def on_progress(content, *, plan=None, **_kw):
        if plan is not None:
            states.append(plan)
    out = await loop.process_direct("cruzá las luces", session_key="cli:x", on_progress=on_progress)
    assert out.content == "Listo."
    assert [(s["event"], s["running"]) for s in states] == [
        ("set", None), ("run", 1), ("done", None), ("run", 2), ("done", None)]
    assert states[-1]["steps"] == ["listar luces", "cruzar"] and set(states[-1]["done"]) == {"1", "2"}


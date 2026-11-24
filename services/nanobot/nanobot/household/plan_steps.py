"""household: what carries out one step of a plan (plans 2/4).

Ported from the fork's ``AgentLoop._step_runner``, ``tools_for_step``,
``_changes_things`` and ``ContextBuilder.build_step_system_prompt``.

The planner (the turn's own model) writes the plan and the answer; each step
runs as its own short ``AgentRunner.run`` on:

* ``planStepModel``/``planStepProvider`` when set, else
* the sub-agent's model when ``routing.planExecutor`` is ``subagent``
  (the default), else
* the planner's own model.

A step on another model than the planner's gets a slim system prompt (the
identity, TOOLS.md, the skills the request and plan name) and only the tools
its text calls for; if it dead-ends (an ``escalateOn`` stop, an error, or an
empty answer) it runs again on the planner's model, with every tool and what
it already did, so one weak step does not sink the plan.

A step only reads unless the planner named it in ``acts``: acting tools are
left out of its registry and the household skill hook refuses acting skill
calls (``HouseholdResponseHook.read_only``), with a note to the planner. The
shell runs only the runtime's own skill calls (``StepTools``, A3).
"""

from __future__ import annotations

import dataclasses
import re
import time
from functools import partial
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.household import delegate
from nanobot.household.runtime import RUNTIME

# Tools that change something whatever their arguments. A read-only step that
# had `cron` scheduled a retry the person had asked the planner for, and the
# planner, reading "scheduled", scheduled a second one (2026-09-24).
_ACTING_TOOLS = {"cron", "message", "write_file", "edit_file", "spawn", "plan"}


def changes_things(tool_name: str) -> bool:
    """A tool that acts rather than reads: the ones above, and Home Assistant's
    intents, which are `Hass<Verb>` -- TurnOn, LightSet, SetVolume, Broadcast --
    where only the `HassGet...` ones read; GetLiveContext and GetDateTime carry
    no prefix."""
    if tool_name in _ACTING_TOOLS:
        return True
    if not tool_name.startswith("mcp_homeassistant"):
        return False
    verb = tool_name.rsplit("__", 1)[-1].rsplit("_", 1)[-1]
    return verb.startswith("Hass") and not verb.startswith("HassGet")


# What a step's words have to say for a family of tools to be offered.
_STEP_TOOL_WORDS: tuple[tuple[re.Pattern, tuple[str, ...]], ...] = (
    (re.compile(r"home ?assistant|\bha\b|\bhass", re.I), ("mcp_homeassistant",)),
    (re.compile(r"\bweb\b|internet|online|search|busca|http|url|p[aá]gina|sitio|site\b", re.I),
     ("web_search", "web_fetch", "mcp_crawl4ai")),
    (re.compile(r"cron|schedule|remind|agend|recordatorio|in \d+ min|en \d+ min", re.I), ("cron",)),
    (re.compile(r"image|imagen|photo|foto|picture", re.I), ("describe_image",)),
)
# Always there: a skill's SKILL.md is read before it is called.
_STEP_BASE_TOOLS = ("read_file",)


def tools_for_step(step: str, names: list[str]) -> list[str]:
    """The tools one plan step is offered: the ones its text names, the
    families its words call for, and reading a skill's instructions.

    A step on a small local model reaches for whatever is in its list. With
    every one of the house's ~60 tools there, a local model answered "flash
    each light" by scraping a web page nine times, and a chores step wrote
    seven files instead of calling the chores skill (2026-09-25). A skill is
    not a tool here -- it is a `{"skill": ...}` block the runtime runs -- so a
    step that only calls skills is offered almost nothing, and that is the
    point. The planner writes the step, so the planner decides: naming a tool
    in a step is how it hands one over.
    """
    text = step.lower()
    words = set(re.findall(r"[a-z0-9_]+", text))
    chosen = [n for n in names if n in _STEP_BASE_TOOLS]
    for n in names:
        if n in chosen or n in ("plan", "spawn", "exec"):
            continue
        short = n.rsplit("__", 1)[-1] if "__" in n else (
            n.split("_", 2)[-1] if n.startswith("mcp_") else n)
        if n.lower() in words or short.lower() in words:
            chosen.append(n)
            continue
        if any(rx.search(text) and n.startswith(prefixes) for rx, prefixes in _STEP_TOOL_WORDS):
            chosen.append(n)
    return chosen


NO_SHELL = ("Error: there is no shell in a plan step. Call the skill or tool the "
            "step names, then report what it returned.")


class StepTools(ToolRegistry):
    """A step's registry (fork ``ToolRegistry.shell_for_skills_only``).

    ``exec`` is registered, so the runtime's skill calls (``{"skill": ...}``
    blocks, which run through it) and its own notes (``_runtime_printf``)
    resolve, but it is not offered to the model, and any other command is
    refused: a local model spent 7-12 hand-written shell calls per step
    checking on its own work (2026-09-25). A step names the skill or tool to
    use. Checked in ``prepare_call``, the path upstream's executor takes.
    """

    def get_definitions(self) -> list[dict[str, Any]]:
        return [d for d in super().get_definitions() if self._schema_name(d) != "exec"]

    def prepare_call(self, name: str, params: Any) -> tuple[Any, Any, str | None]:
        tool, cast_params, error = super().prepare_call(name, params)
        if error is None and name == "exec":
            from nanobot.agent.tools.base import ToolResult
            from nanobot.household.skill_invocation import is_registered_skill_translation

            command = str((cast_params or {}).get("command") or "") if isinstance(cast_params, dict) else ""
            if not is_registered_skill_translation(command):
                return tool, cast_params, ToolResult.error(NO_SHELL)
        return tool, cast_params, error


def step_registry(tools: ToolRegistry, names: list[str], read_only: bool) -> StepTools:
    reg = StepTools()
    for name in list(names) + ["exec"]:
        if name in ("plan", "spawn") or (read_only and changes_things(name)):
            continue
        if (tool := tools.get(name)) is not None and not reg.has(name):
            reg.register(tool)
    return reg


def step_system_prompt(context: Any, relevant: str, channel: str | None = None) -> str:
    """The system prompt for one step (fork ``build_step_system_prompt``).

    The full prompt is persona, household rules, memory and every always-skill
    loaded whole, and a step needs almost none of it: the planner holds the
    conversation and writes the answer. On a local model that prompt is the
    step's cost (two minutes of prompt reading before the first word of every
    step, 2026-09-24), and memory is where a step found "facts" nobody had
    checked. So a step gets the identity, the tool notes, every skill's line in
    the catalogue, and in full only the always-skills *relevant* (the request
    and the plan) names, plus any skill a step calls by name.
    """
    from nanobot.utils.prompt_templates import render_template

    skills = context.skills
    parts = [context._get_identity(channel=channel)]
    tools_md = context.workspace / "TOOLS.md"
    if tools_md.is_file():
        parts.append(f"## TOOLS.md\n\n{tools_md.read_text(encoding='utf-8')}")
    low = (relevant or "").lower()
    words = set(re.findall(r"[a-z0-9]+", low))
    with skills.snapshot():
        named = [s for s in skills.get_always_skills()
                 if s.lower() in words or set(s.lower().split("-")) <= words]
        everything = [e["name"] for e in skills.list_skills(filter_unavailable=True)]
        for name in everything:
            if name not in named and re.search(
                    rf"\bskill:? {re.escape(name.lower())}\b|\b{re.escape(name.lower())} skill\b", low):
                named.append(name)
        if named:
            content = skills.load_skills_for_context(named)
            if content:
                parts.append(f"# Active Skills\n\n{content}")
                # The path lines too: the invocation interceptor maps
                # {"skill": ...} to SKILL.md through them (2026-09-25: without
                # one every chores step ended "bad_invocation").
                paths = skills.build_skills_summary(exclude=set(everything) - set(named))
                if paths:
                    parts.append("# Active Skill Paths\n\n(Reference only: these skills are "
                                 "already loaded above; do not re-read them.)\n\n" + paths)
        summary = skills.build_skills_summary(exclude=set(named))
    if summary:
        parts.append(render_template("agent/skills_section.md", skills_summary=summary))
    return "\n\n---\n\n".join(parts)


def step_runtime(loop: Any, fallback: Any, config: Any = None) -> tuple[Any, bool]:
    """(the runtime a step runs on, whether it is not the planner's own)."""
    from nanobot.household.routing import resolve_runtime
    from nanobot.household.subagents import subagent_runtime

    cfg = config or RUNTIME.config
    if cfg is None or fallback is None:
        return fallback, False
    d = cfg.agents.defaults
    if d.plan_step_model:
        runtime = resolve_runtime(loop, plan_step=True, config=cfg)
    elif d.routing.plan_executor == "subagent":
        runtime = subagent_runtime(fallback, complex=False, config=cfg)
    else:
        return fallback, False
    if runtime is None or (runtime.model == fallback.model
                           and runtime.provider is fallback.provider):
        return fallback, False
    effort = d.routing.plan_step_effort
    if effort:
        runtime = dataclasses.replace(
            runtime, generation=dataclasses.replace(runtime.generation, reasoning_effort=effort))
    return runtime, True


def _step_prompt(preamble: str, request: str, plan: Any, k: int) -> str:
    earlier = "\n\n".join(f"Step {i} ({plan.steps[i - 1]}) found:\n{plan.results[i]}"
                           for i in sorted(plan.results) if i != k)
    prompt = ((f"{preamble}\n\n" if preamble else "")
              + f"You are carrying out ONE step of a plan for this request:\n"
              f"{request}\n\nThe plan:\n{plan.summary()}\n\n"
              + (f"What earlier steps found:\n{earlier}\n\n" if earlier else "")
              + f"Do step {k} now, and only step {k}: {plan.steps[k - 1]}\n"
              "Use the tools and skills you need. When it is done, answer with what "
              "you found or did -- facts and names, briefly. Do not do other steps.\n"
              # One call per reply made a step over eight lights cost nine
              # rounds of the whole prompt (2026-09-24).
              "When the same action applies to several items, make all those calls "
              "in one reply rather than one reply each.\n"
              # A step with no tool call once matched the lights to Home
              # Assistant by guessing, and the answer called that tested
              # (2026-09-24). A skill that wants the person's yes first
              # cannot get it here.
              "Every fact in your answer must come from a tool result, in this step "
              "or an earlier one. Never fill a gap with a likely guess: say what could "
              "not be found. If a skill asks for the person's confirmation before "
              "acting, do not act and do not ask: end the step saying what needs "
              "their yes and what it would do.")
    if k not in plan.acts:
        # Said before the step starts, not only when a call is refused
        # (steps bench, 2026-09-25: every local model called the action,
        # was stopped, and reported the lamp on).
        prompt += ("\nThis step may only READ. Do not call any action that changes "
                   "something (turn on or off, set, add, send, schedule, rename, delete), "
                   "even if the step names one: it would be refused. Instead, end the step "
                   "saying which change it asks for and that it needs the person's go-ahead.")
    return prompt


def make_executor(loop: Any, plan: Any, *, request: str, preamble: str, fallback: Any,
                  session_key: str | None, channel: str | None,
                  config: Any = None) -> Callable[[int], Awaitable[dict]]:
    """``TurnPlan.executor``: carry out step *k*, return {"text", "by", "tools"}."""
    from nanobot.agent.hook import CompositeHook
    from nanobot.agent.runner import AgentRunSpec
    from nanobot.household import telemetry
    from nanobot.household.hooks import HouseholdResponseHook, ToolTimingHook
    from nanobot.household.runtime import TURN_FLAGS

    cfg = config or RUNTIME.config
    routing = cfg.agents.defaults.routing
    escalate_on = set(routing.escalate_on)

    async def attempt(runtime: Any, messages: list[dict], tools: ToolRegistry,
                      read_only: bool, refused: list[str]) -> Any:
        hook = HouseholdResponseHook(tools, workspace=loop.workspace, session_key=session_key,
                                     max_iterations=routing.plan_step_calls)
        hook.read_only, hook.refused = read_only, refused
        # A step's bad invocation is the step's, not the turn's: the planner
        # reads the result and the turn must not escalate for it.
        flags = TURN_FLAGS.set({})
        # Compaction as upstream's sub-agents have it: summarise in place,
        # never written to the session (a step keeps no history of its own).
        definitions = tools.get_definitions()
        compact = dict(runtime=runtime, session_key=session_key, tools=definitions, persist=False)
        try:
            return await loop.runner.run(AgentRunSpec(
                initial_messages=messages, tools=tools, runtime=runtime,
                max_iterations=routing.plan_step_calls,
                max_tool_result_chars=loop.max_tool_result_chars,
                hook=CompositeHook([hook, ToolTimingHook()]), concurrent_tools=True,
                workspace=loop.workspace, session_key=session_key,
                provider_retry_mode=loop.provider_retry_mode,
                consolidate_history=partial(loop.consolidator.summarize_transcript, **compact),
                consolidate_provider_compaction=partial(
                    loop.consolidator.summarize_provider_compaction, **compact),
            ))
        finally:
            TURN_FLAGS.reset(flags)

    async def run_step(k: int) -> dict:
        runtime, local = step_runtime(loop, fallback, cfg)
        read_only = k not in plan.acts
        prompt = _step_prompt(preamble, request, plan, k)
        system = [{"role": "system", "content": step_system_prompt(
            loop.context, request + "\n" + "\n".join(plan.steps), channel=channel)}]
        msgs = system + [{"role": "user", "content": prompt}]
        everything = list(loop.tools.tool_names)
        refused: list[str] = []
        t0 = time.perf_counter()
        by, res = fallback.model, None
        if local:
            by = runtime.model
            narrow = step_registry(loop.tools, tools_for_step(plan.steps[k - 1], everything),
                                   read_only)
            try:
                res = await attempt(runtime, msgs, narrow, read_only, refused)
            except Exception as exc:  # noqa: BLE001 -- run again on the planner's model
                logger.warning("Plan step {} on {} failed: {}", k, runtime.model, exc)
                res = None
            if (res is None or res.stop_reason in escalate_on
                    or not (res.final_content or "").strip()):
                done = delegate.already_done(res.messages) if res is not None else None
                logger.info("Plan step {} ended with {} on {}; again on {}", k,
                            res.stop_reason if res else "an error", runtime.model, fallback.model)
                if res is not None:
                    _report(telemetry, session_key, runtime, res, k, escalated=False, t0=t0)
                msgs = msgs + ([{"role": "user", "content": done}] if done else [])
                by, res = f"{fallback.model} (after {runtime.model})", None
        if res is None:
            wide = step_registry(loop.tools, everything, read_only)
            res = await attempt(fallback, msgs, wide, read_only, refused)
            _report(telemetry, session_key, fallback, res, k, escalated=local, t0=t0)
        else:
            _report(telemetry, session_key, runtime, res, k, escalated=False, t0=t0)
        logger.debug("Plan step {} by {} in {} ms", k, by, round((time.perf_counter() - t0) * 1000))
        text = res.final_content or ""
        if refused:
            # Said to the planner, not left to the step's own words: a step
            # that was stopped still tends to report the change as done.
            text += (f"\n\n(Not done: this step only reads, and it tried to change "
                     f"something -- {', '.join(dict.fromkeys(refused))}. If the person "
                     f"asked for that, set the plan again with step {k} in acts; if not, "
                     f"ask them.)")
        return {"text": text, "by": by, "tools": list(res.tools_used or [])}

    return run_step


def _report(telemetry: Any, session_key: str | None, runtime: Any, res: Any, k: int,
            *, escalated: bool, t0: float) -> None:
    """One usage row per step attempt, as the fork filed them. Its own latency,
    so the turn's profiler block stays for the turn's own row."""
    telemetry.report(
        session_key, runtime.model, res.usage, tools=list(res.tools_used or []),
        route={"tier": "plan-step", "label": f"step {k}", "source": "plan",
               "classifier_ms": 0, "escalated": escalated, "escalated_from": ""},
        provider=getattr(runtime.provider, "provider_name", None),
        stop_reason=res.stop_reason, latency_ms=round((time.perf_counter() - t0) * 1000))

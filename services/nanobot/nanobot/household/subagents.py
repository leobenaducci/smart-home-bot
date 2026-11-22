"""household: sub-agents the way the household runs them (fork B1).

On top of upstream's SubagentManager (runtime per task, inbox, cancel,
persisted status):

- the sub-agent's own model: ``subagentModel``/``subagentProvider``, and
  ``subagentModelPowerful`` for a task marked ``complex`` (or classified so
  when ``routing.mode`` is ``active``, ``routing.subagents`` is on and a
  classifier is configured);
- escalation: a task on the cheaper model that stops on one of
  ``routing.escalateOn`` is continued on ``subagentModelPowerful`` from the
  child transcript (tool results kept, side effects not re-run), once;
- media hand-off: the turn's files are named in the task so the sub-agent
  can read them;
- dedup: the same task for the same session while one is still running is
  answered "already working on it" instead of starting twice;
- a result whose session no longer exists is delivered to its origin chat
  instead of being dropped (upstream drops it);
- the pi harness (B2, ``harness.enabled``): a task from the member's own
  conversation with no attachments runs on pi (household/harness) instead of
  upstream's runner; anything else, or pi unavailable, stays on the loop;
- budget (A7): a task on the powerful sub-agent model gets
  ``routing.complexIterations`` calls, one on the cheaper model
  ``routing.everydayIterations`` (fork: 40), instead of the loop's
  ``maxToolIterations``; an escalated run takes the powerful budget;
- time limit (A8): each attempt is stopped after
  ``NANOBOT_SUBAGENT_MAX_RUNTIME_S`` (fork: 1200 s) and the task reports what
  it managed to do, as an ``incomplete`` result with ``stop_reason`` timeout;
- ``describe_image`` (A9): offered to a sub-agent when the loop has it, so a
  task handed an attached picture can look at it;
- the raw result (A9): kept per task until the loop summarises it, so a
  summary that stalls past 60 s delivers the result itself (household/loop.py);
- ``spawn``: the fork's tool name and arguments, which the household's prompts
  and skills use, on top of upstream's ``subagent`` tool.

OpenCode Go stays refused for a sub-agent role by upstream routing
(household area 7).
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import re
from typing import Any

from loguru import logger

from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema
from nanobot.household.runtime import RUNTIME


def _norm(task: str) -> str:
    return " ".join((task or "").lower().split())[:500]


def subagent_runtime(parent: Any, *, complex: bool = False, config: Any = None) -> Any:  # noqa: A002
    """The runtime a household sub-agent runs on, or *parent* when none is set."""
    from nanobot.household.routing import _provider_for
    from nanobot.utils.llm_runtime import LLMRuntime

    cfg = config or RUNTIME.config
    if cfg is None or parent is None:
        return parent
    d = cfg.agents.defaults
    model = (d.subagent_model_powerful if complex else None) or d.subagent_model
    provider_name = (d.subagent_provider_powerful if complex and d.subagent_model_powerful
                     else d.subagent_provider)
    if not model:
        return parent
    try:
        provider = (_provider_for(cfg, provider_name, model, None)
                    if provider_name and provider_name != d.provider else parent.provider)
    except Exception as exc:
        logger.warning("sub-agent model {}/{} unavailable ({}); using the parent's", provider_name, model, exc)
        return parent
    return LLMRuntime.capture(provider, model, context_window_tokens=parent.context_window_tokens)


# Set around ``spawn`` so the task's own asyncio context (copied when upstream
# creates it) knows whether pi may take it: not from a third party, no files.
_HARNESS_OK: contextvars.ContextVar[bool] = contextvars.ContextVar("household_harness_ok", default=False)


def harness_endpoint(runtime: Any, config: Any = None) -> Any:
    """pi's endpoint for a task on *runtime*, or None when pi may not run it."""
    cfg = config or RUNTIME.config
    if cfg is None:
        return None
    h = cfg.agents.defaults.harness
    if not (h.enabled and h.engine == "pi"):
        return None
    from nanobot.household.harness import pi_runner

    if not pi_runner.available():
        logger.warning("harness is on but pi is not installed at {}", pi_runner.PI_HOME)
        return None
    if h.base_url and h.model:
        ep = pi_runner.Endpoint(base_url=h.base_url, model=h.model, api_key=h.api_key or "local",
                                context_window=h.context_window, reasoning=h.reasoning)
    elif runtime is None:
        return None
    else:
        ep = pi_runner.endpoint_for_provider(runtime.provider, runtime.model, context_window=h.context_window)
    if ep is None:
        logger.info("harness: pi cannot reach {}; the loop runs it", getattr(runtime, "model", "?"))
        return None
    if why := ep.check():  # refuses OpenCode Go, among others
        logger.warning("harness endpoint refused: {}", why)
        return None
    return ep


def harness_takes_long_tasks(loop: Any, config: Any = None) -> bool:
    cfg = config or RUNTIME.config
    if cfg is None or not cfg.agents.defaults.harness.long_tasks:
        return False
    from nanobot.utils.llm_runtime import LLMRuntime

    parent = LLMRuntime.capture(loop.provider, loop.model, context_window_tokens=loop.context_window_tokens)
    return harness_endpoint(subagent_runtime(parent, config=cfg), cfg) is not None


def _person_line(workspace: Any) -> str:
    """Whose assistant this is, from USER.md's ``**Name**:`` line (fork)."""
    try:
        text = (workspace / "USER.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^\s*[-*]\s*\*\*Name\*\*\s*:\s*(.+?)\s*$", text, re.M)
    name = m.group(1).strip() if m else ""
    if not name or name.startswith("(") or len(name) > 60:
        return ""
    return (f"You are working for {name}: this assistant is {name}'s. \"Your person\", "
            f"\"me\" and \"I\" in the task mean {name}. Address {name}, and do not read or "
            f"use another family member's profile unless the task asks about that person.")


def _powerful_model(config: Any = None) -> str | None:
    cfg = config or RUNTIME.config
    return cfg.agents.defaults.subagent_model_powerful if cfg is not None else None


async def classify_complex(loop: Any, task: str, config: Any = None) -> bool:
    """The fork's ``_classify_task``: the chat classifier, applied to a task."""
    cfg = config or RUNTIME.config
    if cfg is None:
        return False
    r = cfg.agents.defaults.routing
    if r.mode != "active" or not r.subagents or not _powerful_model(cfg):
        return False
    from nanobot.household.turn_router import _classifier

    classifier = _classifier(loop, cfg) if loop is not None else None
    if classifier is None:
        return False
    try:
        route = await classifier.classify(task)
    except Exception:
        logger.exception("sub-agent classification failed; running on the sub-agent model")
        return False
    logger.info("sub-agent task classified {} ({})", route.tier, route.source)
    return route.tier == "powerful"


def should_escalate(stop_reason: str | None, model: str | None, config: Any = None) -> bool:
    cfg = config or RUNTIME.config
    if cfg is None or not stop_reason:
        return False
    r = cfg.agents.defaults.routing
    strong = _powerful_model(cfg)
    return (r.mode == "active" and r.max_escalations_per_turn > 0 and bool(strong)
            and model != strong and stop_reason in set(r.escalate_on))


def max_runtime_s() -> float:
    """Wall-clock cap on one sub-agent attempt (fork ``SUBAGENT_MAX_RUNTIME_S``)."""
    try:
        return float(os.environ.get("NANOBOT_SUBAGENT_MAX_RUNTIME_S", "1200"))
    except ValueError:
        return 1200.0


def partial_from_status(status: Any) -> str:
    """The fork's ``_format_partial_from_status``: what a stopped task managed."""
    done = [e for e in (getattr(status, "tool_events", None) or []) if e.get("status") == "ok"]
    lines = ["The task ran longer than allowed and was stopped before finishing."]
    if done:
        lines.append("What it managed to do:")
        lines.extend(f"- {e.get('name')}: {e.get('detail')}" for e in done[-5:])
    else:
        lines.append("It did not complete any steps in time.")
    lines.append("Ask me to continue if you want me to keep going.")
    return "\n".join(lines)


async def _capped(record: Any, attempt: Any) -> Any:
    """Run one attempt under the time limit; on timeout, the partial-progress outcome."""
    limit = max_runtime_s()
    work = asyncio.ensure_future(attempt)
    try:
        done, _ = await asyncio.wait({work}, timeout=limit if limit > 0 else None)
    except asyncio.CancelledError:
        work.cancel()
        raise
    if work in done:
        return work.result()
    work.cancel()
    try:
        await work
    except BaseException:  # noqa: BLE001 -- the attempt's own cancellation
        pass
    from nanobot.agent.subagent import _SubagentOutcome

    status = record.status
    logger.warning("sub-agent {} exceeded {}s; stopping with partial progress",
                   status.task_id, limit)
    text = partial_from_status(status)
    status.result = text  # upstream's finish() reports status.result for an unfinished task
    return _SubagentOutcome("incomplete", text, "timeout")


def iteration_budget(model: str | None, config: Any = None) -> int | None:
    """A sub-agent run's call budget (fork ``_run_subagent``), None to keep the loop's."""
    cfg = config or RUNTIME.config
    if cfg is None:
        return None
    r = cfg.agents.defaults.routing
    strong = _powerful_model(cfg)
    return r.complex_iterations if strong and model == strong else r.everyday_iterations


class HouseholdSubagentManager(SubagentManager):
    """Installed on the household loop's ``subagents`` (see household/loop.py)."""

    # Set by ``install``; the loop the classifier reuses (provider, config).
    household_loop: Any = None

    def _running_duplicate(self, task: str, session_key: str | None) -> str | None:
        key = _norm(task)
        for task_id, record in getattr(self, "_tasks", {}).items():
            status = record.status
            if (status.finished_at is None and status.owner == (session_key or "")
                    and _norm(status.task_description) == key):
                return task_id
        return None

    async def spawn(self, task: str, *args: Any, runtime: Any = None, media: list[str] | None = None,
                    complex: bool = False, harness: bool = True, **kwargs: Any) -> str:  # noqa: A002
        session_key = kwargs.get("session_key") if "session_key" in kwargs else (args[3] if len(args) > 3 else None)
        if (dup := self._running_duplicate(task, session_key)) is not None:
            from nanobot.household.delegate import ack

            return f"{ack('already')} (id: {dup})"
        import os

        from nanobot.utils.homeweb_chat_id import is_third_party_session

        media = [p for p in (media or []) if isinstance(p, str) and os.path.isfile(p)]
        origin = f"{kwargs.get('origin_channel') or ''}:{kwargs.get('origin_chat_id') or ''}"
        third_party = is_third_party_session(session_key) or is_third_party_session(origin)
        if media:
            task = (task + "\n\nThe person attached " + ("this file" if len(media) == 1 else "these files")
                    + " (read as needed):\n" + "\n".join(f"- {path}" for path in media))
        if not complex:
            complex = await classify_complex(self.household_loop, task)  # noqa: A001
        runtime = subagent_runtime(runtime, complex=complex)
        token = _HARNESS_OK.set(harness and not third_party and not media)
        try:
            return await super().spawn(task, *args, runtime=runtime, **kwargs)
        finally:
            _HARNESS_OK.reset(token)

    async def run_inline(self, task: str, *args: Any, runtime: Any = None, **kwargs: Any) -> Any:
        return await super().run_inline(task, *args, runtime=subagent_runtime(runtime), **kwargs)

    def _narrator(self, record: Any) -> Any:
        from nanobot.household.task_panel import Narrator

        return Narrator(record.origin.get("chat_id"), record.status.task_id, record.status.label)

    def _build_subagent_prompt(self, workspace: Any = None) -> str:
        """Upstream's prompt (the household template, via the loader), with the
        current time the fork's sub-agents had (``{{ time_ctx }}``): a task
        about "today" or "next week" otherwise runs without knowing when."""
        from nanobot.household.turn_facts import current_time_str

        prompt = super()._build_subagent_prompt(workspace)
        timezone = getattr(RUNTIME.config.agents.defaults, "timezone", None) if RUNTIME.config else None
        return prompt.replace("# Subagent\n", f"# Subagent\n\nCurrent Time: {current_time_str(timezone)}\n", 1)

    def _extra_turn_hooks(self, record: Any, tools: Any = None, workspace: Any = None) -> list[Any]:
        from nanobot.household.hooks import HouseholdNudgeHook, HouseholdResponseHook, ToolTimingHook
        from nanobot.household.task_panel import TaskPanelHook

        model = getattr(getattr(record, "runtime", None), "model", None)
        budget = iteration_budget(model)
        hooks = [*super()._extra_turn_hooks(record, tools, workspace)]
        if tools is not None:
            # The fork's sub-agents ran the same runner as a chat turn: skill
            # blocks executed, written-out tool calls parsed, the repeated-call
            # stop and the exec-failure cap. Upstream's sub-agent runner has
            # none of it, and the sub-agent prompt still lists the skills.
            limit = budget or self.max_iterations
            session_key = getattr(getattr(record, "session", None), "key", None)
            hooks += [HouseholdResponseHook(tools, workspace=workspace or self.workspace,
                                            session_key=session_key, max_iterations=limit),
                      HouseholdNudgeHook(max_iterations=limit)]
        hooks += [TaskPanelHook(self._narrator(record)), ToolTimingHook()]
        if budget is not None:
            from nanobot.household.plan import TurnBudgetHook

            hooks.append(TurnBudgetHook(budget, exact=True))
        return hooks

    async def _run_on_harness(self, record: Any, endpoint: Any) -> Any:
        """The task on pi; the outcome upstream's lifecycle settles and announces."""
        from nanobot.agent.subagent import _SubagentOutcome
        from nanobot.household.harness import pi_runner

        cfg = RUNTIME.config.agents.defaults.harness
        status = record.status
        context = _person_line(self.workspace) or None
        root = self.workspace / "harness"
        workdir = pi_runner.new_workdir(root)
        status.phase = "harness"
        await self._save_status(status)
        narrate = self._narrator(record)

        async def on_note(text: str) -> None:
            if text:
                narrate("thought", text)

        async def on_start(call: Any) -> None:
            import json

            narrate("tool", call.name, detail=json.dumps(call.args, ensure_ascii=False))

        async def on_tool(call: Any) -> None:
            narrate("tool_result", call.name, status="error" if call.is_error else "ok",
                    detail=call.result[:300] if call.is_error else "")

        async def on_nudge(n: int, of: int, short: str) -> None:
            narrate("phase", f"asked to continue ({n}/{of}): {short}")

        res = await pi_runner.run(
            status.task_description, endpoint, workdir, context=context,
            timeout=float(cfg.timeout_s), thinking=cfg.thinking, max_nudges=cfg.max_nudges,
            on_tool=on_tool, on_note=on_note, on_start=on_start, on_nudge=on_nudge)
        pi_runner.prune_workdirs(root, keep=cfg.keep_workdirs)
        from nanobot.household.profiling import current_span

        if (span := current_span()) is not None:
            span.note(harness="pi", provider="pi")
            for key, value in (res.usage or {}).items():  # pi calls its model itself
                span.tokens[key] = span.tokens.get(key, 0) + int(value)
            for call in res.tools:
                span.tool_names[call.name] = span.tool_names.get(call.name, 0) + 1
        logger.info("sub-agent {} on pi: {} round(s), {} turn(s), {}s, delivered={}", status.task_id,
                    res.rounds, res.turns, res.seconds, res.delivered)
        if res.text:
            return _SubagentOutcome("done", res.text, "completed")
        steps = ", ".join(c.name for c in res.tools if not c.is_error) or "none"
        why = res.error or "no reply"
        return _SubagentOutcome("error", f"The background task did not produce an answer ({why}). "
                                f"Steps that ran: {steps}.", "error", why)

    async def _run_admitted_subagent(self, record: Any) -> Any:
        from nanobot.household import telemetry
        from nanobot.household.profiling import PROFILER

        origin = record.origin
        with PROFILER.span("task", origin.get("session_key") or f"{origin.get('channel')}:{origin.get('chat_id')}",
                           label=record.status.label or "subagent", channel=origin.get("channel") or "",
                           chat_id=origin.get("chat_id") or "",
                           model=getattr(record.runtime, "model", "") or "") as span:
            try:
                outcome = await self._household_run_admitted(record)
            except asyncio.CancelledError:
                telemetry.report_cancelled(
                    f"sub:{origin.get('chat_id')}", getattr(record.runtime, "model", None), span,
                    route={"tier": "subagent", "label": record.status.label or "",
                           "source": "subagent"})
                raise
            usage = getattr(record.status, "usage", None) or span.unbilled()[0]
            if usage:  # B5: one row per task, filed against the chat that asked for it
                telemetry.report(f"sub:{origin.get('chat_id')}", getattr(record.runtime, "model", None),
                                 usage, tools=span.unbilled()[1], stop_reason=outcome.stop_reason,
                                 route={"tier": "subagent", "label": record.status.label or "",
                                        "source": "harness" if span.outcome.get("harness") else "subagent"},
                                 span=span)
            return outcome

    async def _household_run_admitted(self, record: Any) -> Any:
        from nanobot.household import task_panel

        task_panel.post(record.origin.get("chat_id"), "start", record.status.task_id,
                        record.status.label, powerful=getattr(record.runtime, "model", None)
                        == _powerful_model())
        if _HARNESS_OK.get() and record.announce:
            endpoint = harness_endpoint(record.runtime)
            if endpoint is not None:
                try:
                    return await _capped(record, self._run_on_harness(record, endpoint))
                except Exception as exc:  # noqa: BLE001 -- the fork announced the error
                    from nanobot.agent.subagent import _SubagentOutcome

                    logger.error("sub-agent {} failed on the harness: {}", record.status.task_id, exc)
                    return _SubagentOutcome("error", f"Error: {exc}", "error", str(exc))
        outcome = await _capped(record, super()._run_admitted_subagent(record))
        model = getattr(record.runtime, "model", None)
        if not should_escalate(outcome.stop_reason, model):
            return outcome
        strong = subagent_runtime(record.runtime, complex=True)
        if strong is None or strong.model == model:
            return outcome
        from nanobot.household.classify import continuation_messages

        history = list(record.session.messages)
        tools_ran = any(m.get("role") == "tool" for m in history)
        record.session.messages = continuation_messages(history, outcome.stop_reason, tools_ran=tools_ran)
        logger.warning("sub-agent {} ended {} on {}; continuing on {}", record.status.task_id,
                       outcome.stop_reason, model, strong.model)
        record.runtime = strong
        return await _capped(record, super()._run_admitted_subagent(record))

    def _build_tools(self, *args: Any, **kwargs: Any) -> Any:
        registry = super()._build_tools(*args, **kwargs)
        loop = self.household_loop
        if loop is not None and loop._ensure_describe_image():
            registry.register(loop.tools.get("describe_image"))
        return registry

    def take_raw_result(self, task_id: str | None) -> str | None:
        """The result a summary turn is about, once (A9)."""
        raw = getattr(self, "_raw_results", None)
        return raw.pop(task_id, None) if raw is not None and task_id else None

    async def _announce_result(self, task_id: str, label: str, task: str, result: str,
                               origin: Any, status: str, *args: Any, **kwargs: Any) -> None:
        from nanobot.household import task_panel

        raw = getattr(self, "_raw_results", None)
        if raw is None:
            raw = self._raw_results = {}
        raw[task_id] = result or ""
        while len(raw) > 64:  # results nobody summarised (a gone session, a crash)
            raw.pop(next(iter(raw)))

        task_panel.post(origin.get("chat_id"), "done", task_id, label, status=status)
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        if self.sessions is not None and not self.sessions.exists(override):
            # Upstream drops it; the fork delivered it to the chat it came from.
            from nanobot.bus.events import OutboundMessage

            logger.warning("sub-agent {} finished for a session that no longer exists; "
                           "delivering to {}:{}", task_id, origin["channel"], origin["chat_id"])
            await self.bus.publish_outbound(OutboundMessage(
                channel=origin["channel"], chat_id=origin["chat_id"], content=result or "",
                metadata={"subagent_task_id": task_id, "subagent_state": status},
            ))
            return
        await super()._announce_result(task_id, label, task, result, origin, status, *args, **kwargs)


@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("The task for the subagent to complete"),
        context=StringSchema("What the subagent needs from this conversation: prior decisions, "
                             "filenames, and any details it would otherwise be missing."),
        label=StringSchema("Optional short label for the task (for display)"),
        complex=BooleanSchema(description="True for a hard task that deserves the stronger model."),
        required=["task"],
    )
)
class SpawnTool(Tool):
    """The fork's ``spawn``: background work, result posted to this chat."""

    def __init__(self, manager: SubagentManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return ("Start a sub-agent on a task in the background; its result is posted to this "
                "conversation when it is done.")

    async def execute(self, task: str, context: str | None = None, label: str | None = None,
                      complex: bool = False, **_: Any) -> str:  # noqa: A002
        from nanobot.agent.tools.context import current_request_context

        ctx = current_request_context()
        if ctx is None or ctx.runtime is None:
            return "Error: spawn needs an active conversation"
        text = f"{context}\n\n---\n\n{task}" if context else task
        session_key = ctx.session_key or f"{ctx.channel}:{ctx.chat_id}"
        return await self._manager.spawn(
            text, label=label, origin_channel=ctx.channel, origin_chat_id=ctx.chat_id,
            session_key=session_key, origin_message_id=ctx.message_id,
            runtime=ctx.runtime, complex=complex,
        )


def install(loop: Any) -> None:
    """Make the loop's manager household-aware and register ``spawn``."""
    manager = loop.subagents
    if isinstance(manager, SubagentManager) and not isinstance(manager, HouseholdSubagentManager):
        manager.__class__ = HouseholdSubagentManager  # same state, household methods
    if isinstance(manager, HouseholdSubagentManager):
        manager.household_loop = loop
    tools = getattr(loop, "tools", None)
    if tools is not None and not tools.has("spawn"):
        tools.register(SpawnTool(manager))


__all__ = ["HouseholdSubagentManager", "SpawnTool", "classify_complex", "harness_endpoint",
           "iteration_budget", "max_runtime_s", "partial_from_status",
           "harness_takes_long_tasks", "install",
           "should_escalate", "subagent_runtime"]

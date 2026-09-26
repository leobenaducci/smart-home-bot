"""Subagent manager for background task execution."""

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.utils.helpers import strip_think
from nanobot.utils.prompt_templates import render_template
from nanobot.agent.classify import TurnClass, TurnClassifier, continuation_messages
from nanobot.agent.runner import AgentRunResult, AgentRunSpec, AgentRunner
from nanobot.utils.profiling import PROFILER
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.search import GlobTool, GrepTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ExecToolConfig, HarnessConfig, RoutingConfig, WebToolsConfig
from nanobot.utils.homeweb_chat_id import is_third_party_session
from nanobot.providers.base import LLMProvider


# Hard cap on a single subagent's total runtime. A long research task can
# legitimately take a while, but it must not run forever; on timeout we
# announce whatever partial progress we have rather than dying silently.
# OpenCode Go: models written `opencode-go/<name>`, the flat plan's endpoint.
GO_PREFIX = "opencode-go/"
GO_BASE_URL = "https://opencode.ai/zen/go/v1"


def is_go_model(model: str | None) -> bool:
    return str(model or "").strip().lower().startswith(GO_PREFIX)


SUBAGENT_MAX_RUNTIME_S = float(os.environ.get("NANOBOT_SUBAGENT_MAX_RUNTIME_S", "1200"))

# What the runner puts in final_content when it stops without the model ever
# writing an answer. Named because the result path has to recognise it: it
# occupies final_content, so "did we get an answer?" cannot be a truthiness
# check, and treating this as an answer is how a task whose every tool failed
# came to announce that it had completed.
NO_ANSWER_PLACEHOLDER = "Task completed but no final response was generated."


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running subagent."""

    task_id: str
    label: str
    task_description: str
    started_at: float          # time.monotonic()
    phase: str = "initializing"  # initializing | awaiting_tools | tools_completed | final_response | done | error
    iteration: int = 0
    tool_events: list = field(default_factory=list)   # [{name, status, detail}, ...]
    usage: dict = field(default_factory=dict)          # token usage
    stop_reason: str | None = None
    error: str | None = None
    powerful: bool = False  # whether this subagent uses the powerful model


# Ceilings on what one subagent may narrate. A four-hour research task runs
# dozens of iterations with several tool calls each; without these, a single
# task could push tens of thousands of rows at whoever is storing the trace.
# Both are per-task, and hitting either only stops the *commentary* — the task
# itself runs to completion exactly as before.
PROGRESS_MAX_EVENTS = int(os.environ.get("NANOBOT_SUBAGENT_PROGRESS_MAX", "600"))
# How many sub-agents run at once in one assistant; the rest queue (_gated).
MAX_CONCURRENT = max(1, int(os.environ.get("NANOBOT_SUBAGENT_CONCURRENCY", "2")))
# One line, before it reaches the wire. The consumer truncates again; this keeps
# a tool argument holding a whole file from being serialised in the first place.
PROGRESS_MAX_CHARS = 1000


def _clip(text: str, limit: int = PROGRESS_MAX_CHARS) -> str:
    # Cut before normalising, not after: `detail` is a json.dumps of the tool
    # arguments, which for a write_file is the whole file. Splitting a 2 MB
    # string into half a million words and rejoining it to keep 1000 characters
    # is ~170 ms of synchronous work on the event loop, per tool call. The
    # 8x headroom leaves room for whitespace runs to collapse.
    text = (text or "")[: limit * 8]
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _SubagentHook(AgentHook):
    """Hook for subagent execution — logs tool calls, updates status, and
    narrates.

    The narration is what HomeCore's "En segundo plano" panel draws as a live
    timeline. Everything it publishes is something the runner already produced
    and threw away: the model's own text in the turn *before* a tool call, and
    the tool calls themselves with their outcome.

    Deliberately not the raw chain-of-thought (`reasoning_content`): it is far
    larger, arrives in fragments that read as nonsense out of context, and the
    pre-tool-call text is the part that actually explains what the task is
    doing. The panel is meant to be readable by whoever asked for the task.
    """

    def __init__(
        self,
        task_id: str,
        status: SubagentStatus | None = None,
        emit: Any | None = None,
    ) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status
        self._emit = emit
        self._seq = 0
        self._emitted = 0

    async def _publish(self, kind: str, text: str = "", detail: str = "", status: str = "") -> None:
        """Best-effort: narration must never be able to kill the task.

        A publish that raises here would propagate out of the hook and end a
        task that may have hours of work behind it, for the sake of a line in a
        panel. Nothing about the work depends on anyone hearing about it.
        """
        if self._emit is None or self._emitted >= PROGRESS_MAX_EVENTS:
            return
        self._emitted += 1
        self._seq += 1
        try:
            await self._emit({
                "kind": kind,
                "text": _clip(text),
                "detail": _clip(detail),
                "status": status,
                "seq": self._seq,
            })
        except Exception as e:  # noqa: BLE001 — see docstring
            logger.debug("Subagent [{}] progress publish failed: {}", self._task_id, e)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if self._emit is not None:
            # Strip the model's own thinking wrapper if the provider left it in
            # the content. `strip_think` rather than a local regex because it
            # also handles the unclosed opener a pre-tool-call turn routinely
            # carries — matching only `<think>…</think>` would publish the raw
            # reasoning dump this hook exists not to publish.
            thought = strip_think((context.response.content if context.response else "") or "")
            if thought.strip():
                await self._publish("thought", thought)
        for tool_call in context.tool_calls:
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.debug(
                "Subagent [{}] executing: {} with arguments: {}",
                self._task_id, tool_call.name, args_str,
            )
            await self._publish("tool", tool_call.name, detail=args_str)

    async def after_iteration(self, context: AgentHookContext) -> None:
        # Outcomes, paired with the calls announced above. `tool_events` is
        # positional against `tool_calls`, which is how the runner builds both.
        for idx, event in enumerate(context.tool_events or []):
            if not isinstance(event, dict):
                continue
            name = event.get("name") or ""
            if not name and idx < len(context.tool_calls or []):
                name = getattr(context.tool_calls[idx], "name", "")
            status = event.get("status") or ""
            await self._publish(
                "tool_result", name,
                detail=str(event.get("detail") or "") if status == "error" else "",
                status=status,
            )
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_events)
        self._status.usage = dict(context.usage)
        if context.error:
            self._status.error = str(context.error)


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        max_tool_result_chars: int,
        model: str | None = None,
        web_config: "WebToolsConfig | None" = None,
        exec_config: "ExecToolConfig | None" = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        powerful_model: str | None = None,
        powerful_provider: LLMProvider | None = None,
        classifier: "TurnClassifier | None" = None,
        routing: "RoutingConfig | None" = None,
        harness: "HarnessConfig | None" = None,
        main_model: str | None = None,
        main_provider: LLMProvider | None = None,
    ):
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.powerful_model = powerful_model
        self.powerful_provider = powerful_provider
        # The same classifier and routing rules the main loop uses -- a
        # sub-agent task is a request like any other, and before 2026-09-21 it
        # ran on the cheap model unless the spawner remembered to say
        # `complex=true`, which it mostly did not.
        self.classifier = classifier
        self.routing = routing or RoutingConfig()
        self.web_config = web_config or WebToolsConfig()
        self.max_tool_result_chars = max_tool_result_chars
        self.exec_config = exec_config or ExecToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        self.disabled_skills = set(disabled_skills or [])
        self.harness = harness or HarnessConfig()
        # The everyday model: what a task runs on when its sub-agent model is
        # an OpenCode Go one but it cannot go to pi. nanobot's own loop never
        # calls Go (CLAUDE.md).
        self._main_model = main_model
        self._main_provider = main_provider
        self.runner = AgentRunner(provider)
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._task_routes: dict[str, "TurnClass | None"] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
        self._intentional_cancels: set[str] = set()  # task_ids cancelled on purpose (/stop)
        self._slots: asyncio.Semaphore | None = None     # see _gated

    async def _classify_task(self, task: str, context: str | None) -> tuple[bool, "TurnClass | None"]:
        """Whether an unmarked task gets the strong model, and the label that said so."""
        if self.classifier is None or self.routing.mode == "off" or not self.routing.subagents:
            return False, None
        text = task if not context else f"{context.strip()}\n\n{task}"
        route = await self.classifier.classify(text)
        if self.routing.mode != "active" or not self.powerful_model:
            return False, route
        return route.tier == "powerful", route

    def _should_escalate(self, result: "AgentRunResult", powerful: bool) -> bool:
        return (
            not powerful
            and self.routing.mode == "active"
            and bool(self.powerful_model)
            and self.routing.max_escalations_per_turn > 0
            and result.stop_reason in set(self.routing.escalate_on)
        )

    def harness_takes_long_tasks(self) -> bool:
        """The household sends `long` turns to pi, and pi can run one now."""
        return bool(self.harness.long_tasks) and self._harness_endpoint() is not None

    def _harness_endpoint(self, powerful: bool = False):
        """pi's endpoint for this task, or None when it may not run there.

        The model the household picked: the sub-agent's, or the powerful
        sub-agent's for a complex task -- reached the way nanobot reaches it,
        unless `harness.base_url`/`model` override it. An OpenCode Go model is
        reached on Go's own endpoint and only with `harness.allow_go`. Whether
        the task came from the member's own session is the caller's check.
        """
        h = self.harness
        if not (h.enabled and h.engine == "pi"):
            return None
        from nanobot.harness import pi_runner
        if not pi_runner.available():
            logger.warning("harness is on but pi is not installed at {}", pi_runner.PI_HOME)
            return None
        use_powerful = bool(powerful and self.powerful_model)
        model = self.powerful_model if use_powerful else self.model
        provider = (self.powerful_provider or self.provider) if use_powerful else self.provider
        if h.base_url and h.model:
            ep = pi_runner.Endpoint(base_url=h.base_url, model=h.model, api_key=h.api_key or "local",
                                    context_window=h.context_window, reasoning=h.reasoning,
                                    allow_go=h.allow_go)
        elif is_go_model(model):
            ep = pi_runner.Endpoint(
                base_url=GO_BASE_URL, model=model.split("/", 1)[1],
                api_key=os.environ.get("OPENCODE_API_KEY") or os.environ.get("OPENCODE_GO_API_KEY", ""),
                context_window=max(h.context_window, 131072), reasoning=False, allow_go=h.allow_go)
        else:
            ep = pi_runner.endpoint_for_provider(provider, model, context_window=h.context_window,
                                                 allow_go=h.allow_go)
        if ep is None:
            logger.info("harness: pi cannot reach {} -- nanobot's loop runs it", model)
            return None
        why = ep.check()
        if why:
            logger.warning("harness endpoint refused: {}", why)
            return None
        return ep

    async def _run_on_harness(self, task_id: str, task: str, label: str, origin: dict[str, str],
                              status: "SubagentStatus", context: str | None,
                              hook: "_SubagentHook", endpoint) -> tuple[str, str]:
        """The task on pi. (what to announce, "ok" or "error"); the caller announces."""
        from nanobot.harness import pi_runner
        origin_channel = origin.get("channel") or "cli"
        origin_chat_id = origin.get("chat_id") or "direct"

        async def on_note(text: str) -> None:
            if text:
                await hook._publish("thought", text)

        async def on_start(call) -> None:
            await hook._publish("tool", call.name, detail=json.dumps(call.args, ensure_ascii=False))

        async def on_tool(call) -> None:
            await hook._publish("tool_result", call.name, status="error" if call.is_error else "ok",
                                detail=call.result[:300] if call.is_error else "")

        async def on_nudge(n: int, of: int, short: str) -> None:
            await hook._publish("phase", f"asked to continue ({n}/{of}): {short}")

        root = self.workspace / "harness"
        workdir = pi_runner.new_workdir(root)
        with PROFILER.span("task", f"{origin_channel}:{origin_chat_id}", label=label or "subagent",
                           channel=origin_channel, chat_id=origin_chat_id,
                           model=f"pi:{endpoint.model}") as span:
            res = await pi_runner.run(
                task, endpoint, workdir, context=context,
                timeout=min(float(self.harness.timeout_s), SUBAGENT_MAX_RUNTIME_S),
                thinking=self.harness.thinking, max_nudges=self.harness.max_nudges,
                on_tool=on_tool, on_note=on_note, on_start=on_start, on_nudge=on_nudge)
            span.note(task_id=task_id, harness="pi", rounds=res.rounds,
                      delivered=res.delivered, error=res.error or None)
        pi_runner.prune_workdirs(root, keep=self.harness.keep_workdirs)
        logger.info("Subagent [{}] on pi: {} round(s), {} turn(s), {}s, delivered={}", task_id,
                    res.rounds, res.turns, res.seconds, res.delivered)
        if res.text:
            status.phase = "done"
            return res.text, "ok"
        status.phase = "error"
        status.error = res.error or "no reply"
        steps = ", ".join(c.name for c in res.tools if not c.is_error) or "none"
        return (f"The background task did not produce an answer ({res.error or 'no reply'}). "
                f"Steps that ran: {steps}.", "error")

    async def spawn(
        self,
        task: str,
        context: str | None = None,
        label: str | None = None,
        complex: bool | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background.

        ``complex`` is the spawner's word and wins when given. Left unset, the
        task text is classified the way a chat turn is; in ``shadow`` mode the
        label is recorded and the fast model runs, in ``active`` mode a
        `complex` label picks the strong one.
        """
        task_id = str(uuid.uuid4())[:8]
        route = None
        if complex is None:
            complex, route = await self._classify_task(task, context)
            logger.info("Subagent [{}] classified as {} ({}) -> powerful={}",
                        task_id, route.label if route else "?",
                        route.source if route else "no classifier", complex)
        self._task_routes[task_id] = route
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": session_key}

        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
            powerful=complex,
        )
        self._task_statuses[task_id] = status

        bg_task = asyncio.create_task(
            self._gated(status, self._run_subagent(task_id, task, display_label, origin,
                                                   status, complex, context))
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(t: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]
            intentional = task_id in self._intentional_cancels
            self._intentional_cancels.discard(task_id)
            # _run_subagent announces its own result on every normal path. If the
            # task instead ended cancelled or with an unretrieved exception, that
            # path was skipped and the user would get nothing. Surface it: log,
            # and (unless the user cancelled on purpose) best-effort notify them.
            try:
                if t.cancelled():
                    logger.warning("Subagent [{}] cancelled before finishing", task_id)
                    if intentional:
                        return
                    reason = ("The background task was interrupted before it could "
                              "finish (the assistant may have restarted). Ask me to "
                              "run it again if you still need it.")
                else:
                    exc = t.exception()
                    if exc is None:
                        return
                    logger.error("Subagent [{}] died with unhandled error: {!r}", task_id, exc)
                    reason = f"The background task stopped unexpectedly: {exc}"
            except asyncio.CancelledError:
                return
            except Exception:
                return
            try:
                asyncio.get_running_loop().create_task(
                    self._announce_result(task_id, display_label, task, reason, origin, "error")
                )
            except RuntimeError:
                pass  # event loop gone (hard shutdown) — nothing we can do

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)

        # Notify the origin channel that a sub-agent started
        await self.bus.publish_outbound(OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content="",
            metadata={"_subagent_event": "start", "task_id": task_id, "label": display_label, "powerful": complex},
        ))

        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        powerful: bool = False,
        context: str | None = None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)

        origin_channel = origin.get("channel") or "cli"
        origin_chat_id = origin.get("chat_id") or "direct"

        async def _emit_progress(payload: dict) -> None:
            """One line of commentary, out to the origin channel.

            Same envelope as the start/done events (`_subagent_event`), so the
            channels that already route those need only learn one more value —
            and the ones that do not care keep ignoring the whole family.
            """
            await self.bus.publish_outbound(OutboundMessage(
                channel=origin_channel,
                chat_id=origin_chat_id,
                content="",
                metadata={
                    "_subagent_event": "progress",
                    "task_id": task_id,
                    "label": label,
                    **payload,
                },
            ))

        hook = _SubagentHook(task_id, status, emit=_emit_progress)

        # Both the session key and the channel's own address: with
        # unified_session on, a WhatsApp message's session key is
        # `unified:default` and says nothing about who wrote it.
        third_party = (is_third_party_session(origin.get("session_key"))
                       or is_third_party_session(f"{origin_channel}:{origin_chat_id}"))
        endpoint = None if third_party else self._harness_endpoint(powerful)
        if endpoint is not None:
            try:
                answer, outcome = await self._run_on_harness(
                    task_id, task, label, origin, status, context, hook, endpoint)
            except Exception as e:                      # noqa: BLE001
                status.phase = "error"
                status.error = str(e)
                logger.error("Subagent [{}] failed on the harness: {}", task_id, e)
                answer, outcome = f"Error: {e}", "error"
            await self._announce_result(task_id, label, task, answer, origin, outcome)
            return

        async def _on_checkpoint(payload: dict) -> None:
            phase = payload.get("phase", status.phase)
            if phase != status.phase:
                # Through the hook, not straight to _emit_progress: that is
                # where the try/except, the PROGRESS_MAX_EVENTS cap and the
                # monotonic seq live, and `AgentRunner._emit_checkpoint` awaits
                # this callback unguarded — a raise here ends a task that may
                # have hours of work behind it.
                await hook._publish("phase", str(phase))
            status.phase = phase
            status.iteration = payload.get("iteration", status.iteration)

        # Use powerful model/provider if requested and configured
        model = self.model
        runner = self.runner
        if powerful and self.powerful_model:
            model = self.powerful_model
            if self.powerful_provider:
                runner = AgentRunner(self.powerful_provider)
            logger.info("Subagent [{}] using powerful model: {}", task_id, model)
        # An OpenCode Go model is for pi alone. Here, on nanobot's own loop --
        # the task could not go to pi -- it runs on the everyday model instead.
        if is_go_model(model):
            logger.info("Subagent [{}]: {} is an OpenCode Go model and this task is not on pi; "
                        "running it on {}", task_id, model, self._main_model)
            model = self._main_model or self.provider.get_default_model()
            runner = AgentRunner(self._main_provider or self.provider)

        try:
            # Build subagent tools (no message tool, no spawn tool)
            tools = ToolRegistry()
            allowed_dir = self.workspace if (self.restrict_to_workspace or self.exec_config.sandbox) else None
            extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
            tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_read))
            tools.register(WriteFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(EditFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(ListDirTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(GlobTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(GrepTool(workspace=self.workspace, allowed_dir=allowed_dir))
            if self.exec_config.enable:
                tools.register(ExecTool(
                    working_dir=str(self.workspace),
                    timeout=self.exec_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    sandbox=self.exec_config.sandbox,
                    path_append=self.exec_config.path_append,
                    allowed_env_keys=self.exec_config.allowed_env_keys,
                ))
            if self.web_config.enable:
                tools.register(WebSearchTool(config=self.web_config.search, proxy=self.web_config.proxy))
                tools.register(WebFetchTool(proxy=self.web_config.proxy))
            system_prompt = self._build_subagent_prompt()
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
            ]
            if context and context.strip():
                messages.append({
                    "role": "user",
                    "content": f"Background context for this task:\n{context.strip()}",
                })
            messages.append({"role": "user", "content": task})

            # Long/open-ended background work (house/product searches, multi-site
            # research) needs room; complex tasks get a much bigger tool budget.
            max_iters = self.routing.complex_iterations if powerful else 40
            try:
                # Filed against the chat that asked for it, so a session's cost
                # includes the hours of background work it set off and not only
                # the turns somebody waited through. The span closes on the way
                # out whichever way this leaves — answer, timeout or cancel.
                with PROFILER.span(
                    "task",
                    f"{origin_channel}:{origin_chat_id}",
                    label=label or "subagent",
                    channel=origin_channel,
                    chat_id=origin_chat_id,
                    model=model or "",
                ) as span:
                    result = await asyncio.wait_for(
                        runner.run(AgentRunSpec(
                            initial_messages=messages,
                            tools=tools,
                            model=model,
                            max_iterations=max_iters,
                            max_tool_result_chars=self.max_tool_result_chars,
                            hook=hook,
                            max_iterations_message=NO_ANSWER_PLACEHOLDER,
                            error_message=None,
                            fail_on_tool_error=False,
                            checkpoint_callback=_on_checkpoint,
                        )),
                        timeout=SUBAGENT_MAX_RUNTIME_S,
                    )
                    span.note(
                        stop_reason=result.stop_reason,
                        served_by=result.served_by_model,
                        task_id=task_id,
                        powerful=powerful,
                    )
                    # The cheap attempt did not finish: the strong model takes
                    # the same task over, from the tool results already in
                    # hand. Same rule as the main loop -- see agent/classify.py.
                    if self._should_escalate(result, powerful):
                        reason = result.stop_reason
                        route = self._task_routes.get(task_id)
                        if route is not None:
                            route.escalated_from = reason
                        logger.info("Subagent [{}] ended with {} on the fast model; "
                                    "continuing on {}", task_id, reason, self.powerful_model)
                        span.note(escalated_from=reason)
                        result = await asyncio.wait_for(
                            AgentRunner(self.powerful_provider or self.provider).run(AgentRunSpec(
                                initial_messages=continuation_messages(
                                    result.messages, reason, tools_ran=bool(result.tools_used)),
                                tools=tools,
                                model=self.powerful_model,
                                max_iterations=self.routing.complex_iterations,
                                max_tool_result_chars=self.max_tool_result_chars,
                                hook=hook,
                                max_iterations_message=NO_ANSWER_PLACEHOLDER,
                                error_message=None,
                                fail_on_tool_error=False,
                                checkpoint_callback=_on_checkpoint,
                            )),
                            timeout=SUBAGENT_MAX_RUNTIME_S,
                        )
                        model = self.powerful_model or model
            except asyncio.TimeoutError:
                logger.warning(
                    "Subagent [{}] exceeded {}s — stopping with partial progress",
                    task_id, SUBAGENT_MAX_RUNTIME_S,
                )
                status.phase = "timeout"
                await self._announce_result(
                    task_id, label, task,
                    self._format_partial_from_status(status),
                    origin, "error",
                )
                return
            status.phase = "done"
            status.stop_reason = result.stop_reason

            # A subagent runs with fail_on_tool_error=False on purpose: one bad
            # tool call should not end a task that may have hours of work behind
            # it. But that also means stop_reason is rarely "tool_error", so it
            # cannot be the only thing that triggers a partial-progress report —
            # relying on it alone made a task whose tools all failed announce
            # "Task completed", which is both wrong and the opposite of useful.
            # What decides is whether there is an answer to give.
            tool_errors = [e for e in result.tool_events if e["status"] == "error"]
            answer = (result.final_content or "").strip()
            no_answer = not answer or answer == NO_ANSWER_PLACEHOLDER

            if result.stop_reason == "tool_error" or (no_answer and tool_errors):
                status.tool_events = list(result.tool_events)
                await self._announce_result(
                    task_id, label, task,
                    self._format_partial_progress(result),
                    origin, "error",
                )
            elif result.stop_reason == "error":
                await self._announce_result(
                    task_id, label, task,
                    result.error or "Error: subagent execution failed.",
                    origin, "error",
                )
            else:
                final_result = result.final_content or NO_ANSWER_PLACEHOLDER
                logger.info("Subagent [{}] completed successfully", task_id)
                await self._announce_result(task_id, label, task, final_result, origin, "ok")

        except Exception as e:
            status.phase = "error"
            status.error = str(e)
            logger.error("Subagent [{}] failed: {}", task_id, e)
            await self._announce_result(task_id, label, task, f"Error: {e}", origin, "error")

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = render_template(
            "agent/subagent_announce.md",
            label=label,
            status_text=status_text,
            task=task,
            result=result,
        )

        # Inject as a system message so _process_message parses the delivery
        # channel and chat_id from msg.chat_id (format: "{channel}:{chat_id}").
        # session_key_override routes this to the correct session/pending queue.
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=override,
            metadata={
                "injected_event": "subagent_result",
                "subagent_task_id": task_id,
                "subagent_raw_result": result,  # fallback if LLM summarization stalls
            },
        )

        # Notify the origin channel that the sub-agent finished
        await self.bus.publish_outbound(OutboundMessage(
            channel=origin.get("channel", "websocket"),
            chat_id=origin["chat_id"],
            content="",
            metadata={"_subagent_event": "done", "task_id": task_id, "label": label, "status": status},
        ))

        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    @staticmethod
    def _format_partial_progress(result) -> str:
        completed = [e for e in result.tool_events if e["status"] == "ok"]
        failure = next((e for e in reversed(result.tool_events) if e["status"] == "error"), None)
        lines: list[str] = []
        if completed:
            lines.append("Completed steps:")
            for event in completed[-3:]:
                lines.append(f"- {event['name']}: {event['detail']}")
        if failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {failure['name']}: {failure['detail']}")
        if result.error and not failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {result.error}")
        return "\n".join(lines) or (result.error or "Error: subagent execution failed.")

    @staticmethod
    def _format_partial_from_status(status) -> str:
        """Partial-progress summary built from a running subagent's status,
        used when the overall runtime cap trips before a result exists."""
        events = status.tool_events or []
        done = [e for e in events if e.get("status") == "ok"]
        lines = ["The task ran longer than allowed and was stopped before finishing."]
        if done:
            lines.append("What it managed to do:")
            for e in done[-5:]:
                lines.append(f"- {e.get('name')}: {e.get('detail')}")
        else:
            lines.append("It did not complete any steps in time.")
        lines.append("Ask me to continue if you want me to keep going.")
        return "\n".join(lines)

    def _build_subagent_prompt(self) -> str:
        """Build a focused system prompt for the subagent."""
        from nanobot.agent.context import ContextBuilder
        from nanobot.agent.skills import SkillsLoader

        time_ctx = ContextBuilder._build_runtime_context(None, None)
        skills_summary = SkillsLoader(
            self.workspace,
            disabled_skills=self.disabled_skills,
        ).build_skills_summary()
        return render_template(
            "agent/subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(self.workspace),
            skills_summary=skills_summary or "",
        )

    async def cancel(self, task_id: str) -> bool:
        """Stop one subagent. True if it was running and is not any more.

        `cancel_by_session` stops everything a session started, which is the
        right shape for /stop — the person is cancelling the turn they are in.
        It is the wrong shape for the «En segundo plano» panel, where each row
        is a separate piece of work with its own hours behind it: the family
        cancels *that* one, and taking its neighbours down with it is a bug that
        looks like a feature until somebody loses four hours of research.

        Marked as intentional first, or the cancellation is reported as a crash
        — same bookkeeping the session-wide one does, and for the same reason.
        """
        task = self._running_tasks.get(task_id)
        if task is None or task.done():
            return False
        self._intentional_cancels.add(task_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tids = [tid for tid in self._session_tasks.get(session_key, [])
                if tid in self._running_tasks and not self._running_tasks[tid].done()]
        self._intentional_cancels.update(tids)
        tasks = [self._running_tasks[tid] for tid in tids]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    async def _gated(self, status: "SubagentStatus", work) -> None:
        """Run *work* when one of MAX_CONCURRENT places is free.

        Since chat turns that take minutes are handed here (delegate.py), a
        household can start several at once -- and they share the GPU server
        that also answers notifications and routes turns. Two at a time keeps
        one of its slots for those; the rest wait, marked queued.
        """
        if self._slots is None:
            self._slots = asyncio.Semaphore(MAX_CONCURRENT)
        if self._slots.locked():
            status.phase = "queued"
        async with self._slots:
            await work

    def is_running_task(self, session_key: str, task: str) -> bool:
        """Whether this conversation already has this task running."""
        for tid in self._session_tasks.get(session_key, set()):
            t, st = self._running_tasks.get(tid), self._task_statuses.get(tid)
            if t is not None and not t.done() and st is not None \
                    and (st.task_description or "").strip() == task.strip():
                return True
        return False

    def get_running_count_by_session(self, session_key: str) -> int:
        """Return the number of currently running subagents for a session."""
        tids = self._session_tasks.get(session_key, set())
        return sum(
            1 for tid in tids
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        )

"""household: the turn hook that carries the fork's runner behaviour.

Everything here reaches the runner through ``AgentHook.on_model_response``,
the household's one runner patch. A factory builds one hook per turn, so
per-turn state (a stripped skill block, a bad invocation) lives on the hook.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobot.agent.hook import (
    AgentHook,
    AgentHookContext,
    AgentTurnHookContext,
    ModelResponseAction,
)
from nanobot.household import nudges, text_calls
from nanobot.household.runtime import RUNTIME
from nanobot.household.skill_translation import maybe_translate_skill_calls
from nanobot.session.history_visibility import HIDDEN_HISTORY_META
from nanobot.utils.runtime import is_blank_text


class HouseholdResponseHook(AgentHook):
    """Text tool calls (A1) and skill invocations (A2) become real calls."""

    def __init__(
        self,
        tools: Any,
        *,
        workspace: Path | None = None,
        session_key: str | None = None,
        max_iterations: int = 200,
    ) -> None:
        super().__init__()
        self._tools = tools
        self._workspace = workspace
        self._session_key = session_key
        self._max_iterations = max_iterations
        self.bad_invocation = False
        self._stripped_block_pending = False
        self._skill_calls: dict[str, tuple[str, dict]] = {}
        self.read_only = False
        self.refused: list[str] | None = None

    def _view(self, context: AgentHookContext) -> text_calls.TurnView:
        return text_calls.TurnView(
            tools=self._tools,
            workspace=self._workspace,
            session_key=context.session_key or self._session_key,
            max_iterations=self._max_iterations,
        )

    def parse(self, response: Any, context: AgentHookContext) -> Any:
        token = text_calls.SKILL_ROOT_WORKSPACE.set(self._workspace)
        try:
            return text_calls.apply_text_tool_call_parsing(
                response, self._view(context), context.messages,
            )
        finally:
            text_calls.SKILL_ROOT_WORKSPACE.reset(token)

    async def on_model_response(self, context: AgentHookContext) -> ModelResponseAction | None:
        response = context.response
        if response is None:
            return None
        before = (response.content, list(response.tool_calls))
        parsed = self.parse(response, context)
        if getattr(parsed, "bad_invocation", False):
            self.bad_invocation = True
            from nanobot.household.runtime import raise_turn_flag

            raise_turn_flag("bad_invocation")
        if getattr(parsed, "_stripped_skill_block", False):
            self._stripped_block_pending = True
        refusals: dict[str, str] = {}
        if parsed.has_tool_calls and context.messages:
            parsed = await self._translate(parsed, context, refusals)
        if (
            parsed is response
            and not refusals
            and (response.content, list(response.tool_calls)) == before
        ):
            return None
        return ModelResponseAction(
            response=parsed if parsed is not response else None, refuse=refusals,
        )

    async def _translate(self, response: Any, context: AgentHookContext, refusals: dict) -> Any:
        view = self._view(context)
        view_with_policy = _PolicyView(view, self.read_only, self.refused)
        token = text_calls.SKILL_ROOT_WORKSPACE.set(self._workspace)
        try:
            out = await maybe_translate_skill_calls(
                response, view_with_policy, context.messages,
                provider=RUNTIME.provider, model=RUNTIME.model or "",
                refusals=refusals,
            )
        finally:
            text_calls.SKILL_ROOT_WORKSPACE.reset(token)
        skills = getattr(out, "_skills_by_index", None) or {}
        invocations = getattr(out, "_skill_invocations", None) or {}
        for i, name in skills.items():
            self._skill_calls[out.tool_calls[i].id] = (name, invocations.get(i, {}))
        return out

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        # The history keeps what was asked, not the base64 program that runs
        # it (one lights call added ~16k tokens to every later request).
        if not self._skill_calls:
            return
        for message in reversed(context.messages):
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                continue
            for call in message["tool_calls"]:
                entry = self._skill_calls.get(call.get("id"))
                if entry is not None:
                    name, invocation = entry
                    call["function"] = {
                        "name": name,
                        "arguments": json.dumps(invocation, ensure_ascii=False),
                    }
            break

    async def after_iteration(self, context: AgentHookContext) -> None:
        # The model only learns its block did not run if the tool result says
        # so; the fork appended the note to the last tool result of the reply.
        if not self._stripped_block_pending:
            return
        self._stripped_block_pending = False
        for message in reversed(context.messages):
            if message.get("role") != "tool":
                continue
            content = message.get("content")
            note = text_calls._STRIPPED_BLOCK_NOTE
            if isinstance(content, list):
                message["content"] = [*content, {"type": "text", "text": note}]
            else:
                message["content"] = f"{content or ''}\n\n{note}".lstrip()
            return


_STUCK_MESSAGE = (
    "I kept making the same call over and over without getting anywhere, so I "
    "stopped instead of looping. Tell me what to try instead, or ask for a "
    "smaller piece of it."
)
_NUDGE_META = {HIDDEN_HISTORY_META: {"kind": "household_nudge"}}


class HouseholdNudgeHook(AgentHook):
    """Finalization nudges and per-turn tool limits (A3).

    - a reply that only announces a step is asked, once, to act or answer;
    - a reply that thought but said nothing is told so, once;
    - after three consecutive exec failures, exec is refused for the turn;
    - a list item nobody in the conversation named is refused;
    - the same call (every no-op exec counts as one) made
      ``_REPEATED_TOOL_CALL_LIMIT`` times is refused with an instruction to
      answer, and a further call ends the turn on an honest sentence.
    """

    def __init__(self, *, max_iterations: int = 200, read_only: bool = False) -> None:
        super().__init__()
        self._max_iterations = max_iterations
        self.read_only = read_only
        self._announced_nudged = False
        self._empty_nudged = False
        self._exec_counts: dict[str, int] = {}
        self._call_counts: dict[tuple[str, str], int] = {}
        self._stuck = False
        self.stop_reason: str | None = None

    async def on_model_response(self, context: AgentHookContext) -> ModelResponseAction | None:
        r = context.response
        if r is None:
            return None
        if r.has_tool_calls:
            return self._limit_calls(r, context)
        if r.finish_reason in ("error", "length"):
            return None
        if (
            is_blank_text(r.content)
            and r.reasoning_content
            and not self._empty_nudged
        ):
            self._empty_nudged = True
            return ModelResponseAction(inject=[{**nudges.build_empty_reply_message(), **_NUDGE_META}], keep_content=False)
        if (
            not self._announced_nudged
            and not self.read_only
            and context.iteration + 1 < self._max_iterations
            and nudges.announces_next_step(r.content)
        ):
            self._announced_nudged = True
            return ModelResponseAction(inject=[{**nudges.build_announced_step_message(), **_NUDGE_META}])
        return None

    def _limit_calls(self, r: Any, context: AgentHookContext) -> ModelResponseAction | None:
        refuse: dict[str, str] = {}
        for tc in r.tool_calls:
            sig = (
                ("exec", "<no-op>") if text_calls._is_noop_exec(tc)
                else (tc.name, json.dumps(tc.arguments or {}, sort_keys=True, default=str))
            )
            self._call_counts[sig] = self._call_counts.get(sig, 0) + 1
            if self._call_counts[sig] >= text_calls._REPEATED_TOOL_CALL_LIMIT:
                self._stuck = True
            error = nudges.repeated_exec_error(tc.name, self._exec_counts) or (
                nudges.unrequested_list_item_error(tc.name, tc.arguments or {}, context.messages)
            )
            if error:
                refuse[tc.id] = error
        if self._stuck:
            # Not looping to max_iterations: ask for the answer from what the
            # turn has; blank means it really is stuck, and says so.
            return ModelResponseAction(
                finalize=True,
                finalize_fallback=_STUCK_MESSAGE,
                finalize_stop_reason="repeated_tool_calls",
            )
        return ModelResponseAction(refuse=refuse) if refuse else None

    async def after_iteration(self, context: AgentHookContext) -> None:
        for event in context.tool_events:
            if event.get("name") != "exec" or event.get("detail") == "refused":
                continue
            nudges.record_exec_outcome("exec", event.get("status") == "error", self._exec_counts)


@dataclass
class _PolicyView:
    """TurnView plus the plan step's read-only policy the translator reads."""

    view: text_calls.TurnView
    read_only: bool = False
    refused: list[str] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.view, name)


class ToolTimingHook(AgentHook):
    """A13: each tool call's time into the profiler (fork runner ``_profile``).

    Timed per call rather than around the batch: tools in one iteration run
    together, so the batch's wall time says nothing about which of them the
    turn waited for. Upstream's tool hooks fire around the execution itself,
    so a call refused before it reached a tool (bad arguments, an unknown
    name) is not timed, unlike the fork.
    """

    def __init__(self) -> None:
        super().__init__()
        self._started: dict[str, float] = {}

    async def before_execute_tool(self, context: Any, tool_call: Any, tool: Any, params: Any) -> None:
        import time

        self._started[tool_call.id] = time.perf_counter()

    def _record(self, tool_call: Any, status: str, detail: str = "", result: Any = None) -> None:
        import time

        from nanobot.household.profiling import PROFILER

        started = self._started.pop(tool_call.id, None)
        if started is None:
            return
        PROFILER.record_tool(name=tool_call.name, duration_s=time.perf_counter() - started,
                             status=status, started_at=started, detail=detail,
                             result_chars=len(result) if isinstance(result, str) else 0)

    async def after_execute_tool(self, context: Any, tool_call: Any, tool: Any, params: Any,
                                 result: Any) -> None:
        self._record(tool_call, "ok", result=result)

    async def on_execute_tool_error(self, context: Any, tool_call: Any, tool: Any, params: Any,
                                    error: Any) -> None:
        detail = error.replace("\n", " ").strip() if isinstance(error, str) else str(error)
        self._record(tool_call, "error", detail, error if isinstance(error, str) else None)


def tool_timing_hook_factory() -> Callable[[AgentTurnHookContext], AgentHook | None]:
    return lambda ctx: ToolTimingHook()


def response_hook_factory(
    tools: Any,
    *,
    max_iterations: int = 200,
) -> Callable[[AgentTurnHookContext], AgentHook | None]:
    """Build the per-turn factory AgentLoop takes in ``hook_factories``."""

    def factory(ctx: AgentTurnHookContext) -> AgentHook | None:
        return HouseholdResponseHook(
            tools,
            workspace=ctx.workspace,
            session_key=ctx.session_key,
            max_iterations=max_iterations,
        )

    return factory


def nudge_hook_factory(*, max_iterations: int = 200) -> Callable[[AgentTurnHookContext], AgentHook | None]:
    """Per-turn A3 hook; runs after the response hook so parsed calls count."""

    def factory(ctx: AgentTurnHookContext) -> AgentHook | None:
        return HouseholdNudgeHook(max_iterations=max_iterations)

    return factory

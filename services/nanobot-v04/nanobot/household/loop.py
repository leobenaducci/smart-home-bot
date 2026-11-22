"""household: the thin AgentLoop subclass the assessors called for (fork B3).

Overrides around upstream's own stages:

- ``_build_turn``: before upstream picks the session's runtime, route the
  turn (``turn_router.route_turn``) -- every channel, not just the API; a
  ``long`` turn is told to plan. An inline turn (the morning greeting) is
  not routed.
- ``_run_turn``: a ``background`` turn that may be delegated goes to a
  sub-agent with an acknowledgement. Every turn has a plan (household/plan.py)
  and a cheap one the everyday budget. After upstream's run: a plan past its
  time limit hands its steps left to a sub-agent; a cheap attempt that ended
  on one of ``routing.escalate_on`` goes to a sub-agent where a later answer
  is an answer, else is continued on the powerful model from where it
  stopped (tool results kept, side effects not re-run), and the session
  starts its next ``routing.sticky_turns`` turns strong.
- ``process_direct``: keeps a caller's progress callback that takes the
  plan's checklist.

Everything else is upstream's AgentLoop.
"""

from __future__ import annotations

import asyncio

import contextvars
from typing import Any

from loguru import logger

from nanobot.agent.context import TranscriptInput
from nanobot.agent.loop import AgentLoop, TurnContext, TurnKind
from nanobot.household import delegate, turn_router
from nanobot.household import plan as household_plan
from nanobot.household.classify import continuation_messages
from nanobot.household.runtime import RUNTIME
from nanobot.session.history_visibility import HIDDEN_HISTORY_META

ROUTE_ATTR = "household_route"
PRESET_ATTR = "household_preset_runtime"
# Set by a caller (the morning greeting) for a turn that answers here and now:
# no routing, no delegation, no plan steps handed out (fork `inline=True`).
INLINE_ATTR = "household_inline"
NOTE_ATTR = "household_turn_note"
BILL_ATTR = "household_bill_delegated"
# A4c: the direct caller's progress callback (the household API's), when it
# takes `plan=`; upstream folds on_progress into the turn's event sink, where
# the plan's checklist cannot reach it.
TURN_PROGRESS: contextvars.ContextVar[Any] = contextvars.ContextVar("household_turn_progress", default=None)


class HouseholdAgentLoop(AgentLoop):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        from nanobot.household import subagents

        subagents.install(self)  # B1: sub-agent model, dedup, media, gone sessions, `spawn`
        from nanobot.household import context_extras

        context_extras.install(self)  # D2: standing context, Recent History
        from nanobot.household.plan import PlanTool

        self.tools.register(PlanTool())  # A1: the checklist for work of several steps

    async def process_direct(self, *args: Any, **kwargs: Any) -> Any:
        from nanobot.utils.progress_events import _on_progress_accepts

        on_progress = kwargs.get("on_progress")
        token = TURN_PROGRESS.set(
            on_progress if on_progress is not None and _on_progress_accepts(on_progress, "plan")
            else None)
        try:
            return await super().process_direct(*args, **kwargs)
        finally:
            TURN_PROGRESS.reset(token)

    async def _build_turn(self, ctx: TurnContext) -> None:
        from nanobot.household import context_extras

        if ctx.session is not None:
            session = ctx.session
            content, standing = context_extras.take_standing(session, ctx.msg.content)
            if content is not ctx.msg.content:
                ctx.msg.content = content  # the session keeps no copy of the region
            ctx.attributes[context_extras.STANDING_KEY] = standing
            ctx.attributes["household_recent"] = (
                context_extras.recent_history(getattr(self.context, "memory", None), ctx.session_key)
                if ctx.kind is TurnKind.USER else "")
        standing_token = context_extras.TURN_STANDING.set(ctx.attributes.get(context_extras.STANDING_KEY, ""))
        recent_token = context_extras.TURN_RECENT.set(ctx.attributes.get("household_recent", ""))
        try:
            await self._household_build_turn(ctx)
        finally:
            context_extras.TURN_STANDING.reset(standing_token)
            context_extras.TURN_RECENT.reset(recent_token)
        limit = RUNTIME.config.agents.defaults.context_block_limit if RUNTIME.config is not None else None
        if limit and ctx.transcript_input is not None:
            import dataclasses

            ctx.transcript_input = dataclasses.replace(
                ctx.transcript_input,
                history=context_extras.limit_history(list(ctx.transcript_input.history), limit))

    def _ensure_describe_image(self) -> bool:
        """D6: register ``describe_image`` once a vision model is configured."""
        if self.tools.has("describe_image"):
            return True
        if RUNTIME.config is None or not RUNTIME.config.agents.defaults.vision_model:
            return False
        from nanobot.household.routing import resolve_runtime
        from nanobot.household.vision import DescribeImageTool

        runtime = resolve_runtime(self, vision=True)
        if runtime is None:
            return False
        self.tools.register(DescribeImageTool(runtime.provider, runtime.model, workspace=self.workspace))
        return True

    def _images_to_describe_image(self, ctx: TurnContext) -> None:
        """D6: an image turn stays on the main model; the picture goes through
        ``describe_image`` (the vision model is small and local and fails on
        the whole prompt and tool list, while one focused question works)."""
        from nanobot.household.turn_router import _IMAGE_SUFFIXES
        from nanobot.utils.helpers import image_placeholder_text

        media = list(ctx.msg.media or [])
        images = [m for m in media if str(m).lower().endswith(_IMAGE_SUFFIXES)]
        if not images or not self._ensure_describe_image():
            return
        said = ctx.msg.content if isinstance(ctx.msg.content, str) else ""
        ctx.msg.content = " ".join([*(image_placeholder_text(p) for p in images), said]).strip()
        ctx.msg.media = [m for m in media if m not in images]

    async def _household_build_turn(self, ctx: TurnContext) -> None:
        # A2: a runtime the caller chose (the API's profile/powerful, the
        # heartbeat) is not a plain turn, so its plan steps are not handed out.
        inline = bool(ctx.attributes.get(INLINE_ATTR))
        ctx.attributes[PRESET_ATTR] = ctx.runtime is not None or inline
        if ctx.kind is TurnKind.USER and RUNTIME.config is not None:
            self._images_to_describe_image(ctx)
        if (ctx.runtime is None and not inline and ctx.kind is TurnKind.USER
                and RUNTIME.config is not None):
            session = ctx.require_session()
            try:
                route, runtime = await turn_router.route_turn(
                    self,
                    text=delegate.task_text(ctx.msg.content),
                    media=list(ctx.msg.media or []),
                    chat_id=ctx.msg.chat_id,
                    session_metadata=session.metadata,
                    history=list(session.messages[-6:]),
                )
            except Exception:
                logger.exception("routing failed; the turn runs on the default model")
                route, runtime = None, None
            if route is not None:
                ctx.attributes[ROUTE_ATTR] = route
                logger.info("route {} tier={} source={} ({})", ctx.session_key,
                            route.tier, route.source, route.reason)
            if runtime is not None:
                ctx.runtime = runtime
            if (route is not None and route.label == "long" and self._plain_turn(ctx)
                    and RUNTIME.config.agents.defaults.routing.mode == "active"):
                ctx.attributes[NOTE_ATTR] = household_plan.MODE_NOTE  # A4: plan it
        await super()._build_turn(ctx)

    async def _run_turn(self, ctx: TurnContext) -> None:
        from nanobot.household import context_extras

        from nanobot.household import telemetry
        from nanobot.household.profiling import PROFILER

        standing_token = context_extras.TURN_STANDING.set(ctx.attributes.get(context_extras.STANDING_KEY, ""))
        recent_token = context_extras.TURN_RECENT.set(ctx.attributes.get("household_recent", ""))
        note_token = context_extras.TURN_NOTE.set(ctx.attributes.get(NOTE_ATTR, ""))
        try:
            with PROFILER.span("turn", ctx.session_key, channel=ctx.msg.channel or "",
                               chat_id=ctx.msg.chat_id or "",
                               model=ctx.runtime.model if ctx.runtime else "") as span:
                try:
                    if _is_subagent_result(ctx):
                        await self._summarise_capped(ctx)
                    else:
                        await self._household_run_turn(ctx)
                except asyncio.CancelledError:
                    telemetry.report_cancelled(
                        ctx.session_key, ctx.runtime.model if ctx.runtime else None, span,
                        route=telemetry.route_dict(ctx.attributes.get(ROUTE_ATTR)))
                    raise
                # B5: the sub-agent bills its own run; a plan handed off past
                # its time limit (A4) bills what the chat turn spent first.
                if ctx.stop_reason != "delegated" or ctx.attributes.get(BILL_ATTR):
                    telemetry.report(
                        ctx.session_key, ctx.runtime.model if ctx.runtime else None, ctx.usage,
                        tools=telemetry.tool_names(ctx.all_messages),
                        route=telemetry.route_dict(ctx.attributes.get(ROUTE_ATTR)),
                        provider=span.outcome.get("provider")
                        or getattr(getattr(ctx.runtime, "provider", None), "provider_name", None),
                        stop_reason=ctx.stop_reason, span=span)
        finally:
            context_extras.TURN_STANDING.reset(standing_token)
            context_extras.TURN_RECENT.reset(recent_token)
            context_extras.TURN_NOTE.reset(note_token)

    async def _summarise_capped(self, ctx: TurnContext) -> None:
        """A sub-agent result's summary, capped (A9, fork ``_dispatch``).

        A stalled provider call must not lose the result: past
        ``SUMMARY_TIMEOUT_S`` the summary is stopped and the result itself is
        the reply. 60 s, not 20: in the fork a summariser that was working
        (calling tools) was cut off at 20 s and a sub-agent's reasoning went
        to the chat instead of an answer.
        """
        raw = self.subagents.take_raw_result((ctx.msg.metadata or {}).get("subagent_task_id")) \
            if hasattr(self.subagents, "take_raw_result") else None
        work = asyncio.ensure_future(self._household_run_turn(ctx))
        try:
            done, _ = await asyncio.wait({work}, timeout=SUMMARY_TIMEOUT_S if raw else None)
        except asyncio.CancelledError:
            work.cancel()
            raise
        if work in done:
            work.result()
            return
        work.cancel()
        try:
            await work
        except BaseException:  # noqa: BLE001 -- the summary's own cancellation
            pass
        logger.warning("sub-agent result summary for {} timed out after {}s; "
                       "delivering the result itself", ctx.session_key, SUMMARY_TIMEOUT_S)
        ctx.final_content = raw
        ctx.all_messages = _reply_only(ctx, raw)
        ctx.stop_reason = "summary_timeout"
        ctx.streamed_content = False

    async def _household_run_turn(self, ctx: TurnContext) -> None:
        route = ctx.attributes.get(ROUTE_ATTR)
        if (
            route is not None
            and route.label == "background"
            and route.tier == "subagent"
            and delegate.may_delegate(ctx.msg.channel, ctx.msg.chat_id, ctx.session_key)
        ):
            await self._delegate(ctx)
            return
        if (
            route is not None
            and route.label == "long"
            and route.source not in ("forced", "sticky")
            and RUNTIME.config is not None
            and RUNTIME.config.agents.defaults.routing.mode == "active"
            and delegate.may_delegate(ctx.msg.channel, ctx.msg.chat_id, ctx.session_key)
        ):
            from nanobot.household.subagents import harness_takes_long_tasks

            if harness_takes_long_tasks(self):  # B2: `long` work goes to pi
                await self._delegate(ctx)
                return
        from nanobot.household.runtime import TURN_FLAGS

        token = TURN_FLAGS.set({})
        plan = self._turn_plan(ctx)
        plan_token = household_plan.begin(plan)
        budget = self._everyday_budget(ctx)
        if budget is not None:
            ctx.hooks.append(household_plan.TurnBudgetHook(budget))
        try:
            await super()._run_turn(ctx)
            flags = TURN_FLAGS.get() or {}
        finally:
            TURN_FLAGS.reset(token)
            household_plan.end(plan_token)
        if plan.handoff and plan.remaining() and plan.limit_s != float("inf"):
            await self._hand_off(ctx, plan, "continue")
            return
        if flags.get("bad_invocation") and ctx.stop_reason == "completed":
            ctx.stop_reason = "bad_invocation"
        if (turn_router.dead_end(route, ctx.stop_reason) and self._plain_turn(ctx)
                and delegate.may_delegate(ctx.msg.channel, ctx.msg.chat_id, ctx.session_key)):
            await self._hand_off(ctx, plan, "dead_end")  # A4d
            return
        if not turn_router.should_escalate(route, ctx.stop_reason):
            return
        strong = turn_router.escalation_runtime(self)
        if strong is None or ctx.transcript_input is None:
            return
        tools_ran = any(m.get("role") == "tool" for m in ctx.all_messages)
        carried = continuation_messages(
            list(ctx.all_messages[1:]), ctx.stop_reason, tools_ran=tools_ran,
        )
        if carried and carried[-1].get("role") == "user" and tools_ran:
            carried[-1] = {**carried[-1], HIDDEN_HISTORY_META: {"kind": "household_escalation"}}
        logger.warning("routing: {} ended {} on {}; continuing on {}", ctx.session_key,
                       ctx.stop_reason, ctx.runtime.model if ctx.runtime else "?", strong.model)
        original = ctx.transcript_input
        ctx.transcript_input = TranscriptInput(
            history=carried,
            current_message=None,
            session_summary=original.session_summary,
            runtime_context_blocks=original.runtime_context_blocks,
        )
        route.escalated_from = route.tier
        route.tier = "powerful"
        ctx.runtime = strong
        try:
            await super()._run_turn(ctx)
        finally:
            ctx.transcript_input = original
        sticky = RUNTIME.config.agents.defaults.routing.sticky_turns
        if sticky and ctx.session is not None:
            ctx.session.metadata[turn_router.STICKY_KEY] = sticky

    def _plain_turn(self, ctx: TurnContext) -> bool:
        """The everyday or planner model, routed here: not a runtime the caller
        chose, not an inline turn, not the powerful or vision tier."""
        route = ctx.attributes.get(ROUTE_ATTR)
        return not ctx.attributes.get(PRESET_ATTR) and (route is None or route.tier == "everyday")

    def _everyday_budget(self, ctx: TurnContext) -> int | None:
        """A4: a turn the classifier sent to the cheap tier gets the cheap
        tier's budget (``routing.everydayIterations``, fork: 40); running out
        of it is ``max_iterations``, which escalates. A plan buys more per
        step. Forced, sticky, shadow and caller-chosen turns keep the loop's."""
        route = ctx.attributes.get(ROUTE_ATTR)
        if RUNTIME.config is None or route is None or not self._plain_turn(ctx):
            return None
        routing = RUNTIME.config.agents.defaults.routing
        if routing.mode != "active" or route.source not in ("model", "default", "fast_path"):
            return None
        return min(self.max_iterations, routing.everyday_iterations)

    async def _hand_off(self, ctx: TurnContext, plan: household_plan.TurnPlan, kind: str) -> None:
        """Hand the rest of this turn to a sub-agent (fork ``_delegate_turn``).

        ``continue`` (A4/A4b): past the chat's time limit mid-plan the model
        has said what is done; the steps left go on with the checklist, what
        each step found and the calls already made. The person reads that
        line plus "the rest continues in the background", the turn's trail
        stays in the session and the chat turn is billed here. Not on pi: the
        steps left are this turn's plan, and pi on the local model was the
        runner that came back with nothing (fork, 2026-09-24).

        ``dead_end`` (A4d): a cheap attempt that ended on ``escalateOn`` in a
        conversation where a later answer is an answer goes to a sub-agent
        with what it already did, rather than to the powerful model. The
        session keeps the request and the acknowledgement, not the stuck
        trail, and the attempt is billed before the trail is dropped.
        """
        from nanobot.household import telemetry
        from nanobot.household.api_adapter import _Subagents

        parts = [delegate.conversation_context(ctx.session) if ctx.session else None]
        if plan.steps:
            parts.append(f"The plan so far (✓ done, · still to do):\n{plan.summary()}")
            parts.append("\n\n".join(f"Step {i} ({plan.steps[i - 1]}) found:\n{plan.results[i][:800]}"
                                      for i in sorted(plan.results)))
        if plan.interrupted:
            parts.append(f"Step {plan.interrupted} was stopped part-way: check what it already "
                         f"changed before doing it again.")
        parts.append(delegate.already_done(ctx.all_messages))
        await _Subagents(self).spawn(
            delegate.task_text(ctx.msg.content), context="\n\n".join(p for p in parts if p),
            origin_channel=ctx.msg.channel, origin_chat_id=ctx.msg.chat_id,
            session_key=ctx.session_key, harness=kind != "continue",
        )
        if kind == "continue":
            await household_plan.say(plan, "handoff")
            lead = (ctx.final_content or "").strip()
            reply = f"{lead}\n\n{delegate.ack('continue')}" if lead else delegate.ack("continue")
            for message in reversed(ctx.all_messages):
                if message.get("role") == "assistant" and not message.get("tool_calls"):
                    message["content"] = reply
                    break
            ctx.attributes[BILL_ATTR] = True
        else:
            route = ctx.attributes.get(ROUTE_ATTR)
            telemetry.report(
                ctx.session_key, ctx.runtime.model if ctx.runtime else None, ctx.usage,
                tools=telemetry.tool_names(ctx.all_messages), route=telemetry.route_dict(route),
                provider=getattr(getattr(ctx.runtime, "provider", None), "provider_name", None),
                stop_reason=ctx.stop_reason)
            reply = delegate.ack("dead_end")
            ctx.all_messages = _reply_only(ctx, reply)
        logger.info("Turn {} handed to a sub-agent ({}; plan {}/{} steps)", ctx.session_key,
                    kind, len(plan.done), len(plan.steps))
        ctx.final_content = reply
        ctx.stop_reason = "delegated"

    def _turn_plan(self, ctx: TurnContext) -> household_plan.TurnPlan:
        """A1: every turn gets a fresh plan, so a plan never leaks into the next.

        A2: on a plain turn (the everyday or planner model, routed here) the
        plan gets an executor, so `run` carries a step out on the step model.

        The time limit applies only where the steps left could go on in the
        background (fork ``_run_agent_loop``); elsewhere (voice) a plan runs to
        its end. Each step buys ``routing.planStepCalls`` more model calls, up
        to the larger of the loop's budget and ``routing.complexIterations``.
        """
        routing = RUNTIME.config.agents.defaults.routing if RUNTIME.config is not None else None
        can_hand_off = delegate.may_delegate(ctx.msg.channel, ctx.msg.chat_id, ctx.session_key)
        progress = TURN_PROGRESS.get()
        if routing is None:
            return household_plan.TurnPlan(limit_s=float("inf"), max_budget=self.max_iterations)
        # A4b: the earlier of planChatSeconds and planDetachSeconds, so the
        # steps left are handed off before the API's request timeout (the
        # house runs 300 s) cancels the turn and loses them.
        limit = float(min(routing.plan_chat_seconds, routing.plan_detach_seconds))
        plan = household_plan.TurnPlan(
            limit_s=limit if can_hand_off else float("inf"),
            # A4c: the checklist goes to the page (set/run/done/handoff, and
            # every few seconds while a step runs, which keeps the API's
            # stall timer from taking a slow step for a dead turn).
            progress=(lambda state: progress("", plan=state)) if progress is not None else None,
            per_step=routing.plan_step_calls,
            max_budget=max(self.max_iterations, routing.complex_iterations),
        )
        if self._plain_turn(ctx) and ctx.runtime is not None:
            # A2: steps run as their own short runs (plan_steps.py); one that
            # dead-ends runs again on this turn's model.
            from nanobot.household import plan_steps

            blocks = getattr(ctx, "runtime_context_blocks", None) or []
            plan.executor = plan_steps.make_executor(
                self, plan, request=delegate.task_text(ctx.msg.content),
                preamble="\n\n".join(b.content for b in blocks),
                fallback=ctx.runtime, session_key=ctx.session_key, channel=ctx.msg.channel)
        return plan

    async def _delegate(self, ctx: TurnContext) -> None:
        from nanobot.household.api_adapter import _Subagents

        context = delegate.conversation_context(ctx.session) if ctx.session else None
        await _Subagents(self).spawn(
            delegate.task_text(ctx.msg.content), context=context,
            origin_channel=ctx.msg.channel, origin_chat_id=ctx.msg.chat_id,
            session_key=ctx.session_key, media=list(ctx.msg.media or []),
        )
        ctx.final_content = delegate.ack("long")
        ctx.all_messages = _reply_only(ctx, ctx.final_content)
        ctx.stop_reason = "delegated"


SUMMARY_TIMEOUT_S = 60.0


def _is_subagent_result(ctx: TurnContext) -> bool:
    return (ctx.msg.channel == "system" and ctx.msg.sender_id == "subagent"
            and (ctx.msg.metadata or {}).get("injected_event") == "subagent_result")


def _reply_only(ctx: TurnContext, reply: str) -> list[dict[str, Any]]:
    """The turn's messages as upstream saves them, with *reply* as the only new one.

    Upstream persists ``all_messages`` past the turn's initial messages
    (``save_skip``), so a delegated turn that set it to ``[]`` saved the
    request and not the acknowledgement the person read; the fork saved both.
    The initial messages are the run's own when there was a run, else rebuilt
    from the transcript input (system, history, current message), which is
    exactly what ``message_count`` counts.
    """
    ti = ctx.transcript_input
    initial = ti.message_count if ti is not None else 0
    if len(ctx.all_messages) >= initial:
        prefix = list(ctx.all_messages[:initial])
    elif ti is not None:
        prefix = [{"role": "system", "content": ""}, *ti.history]
        if ti.current_message is not None:
            prefix.append({"role": ti.current_role, "content": ti.current_message})
    else:
        prefix = []
    return [*prefix, {"role": "assistant", "content": reply}]

"""A checklist for work that takes several steps, done in the chat.

Since 2026-09-24 a request the router labels ``long`` -- several steps, several
systems, checking one thing against another -- runs on the everyday model in
the chat rather than on the powerful one or in the background. What made that
fail before was structure, not the model: asked to cross-reference the lights
with Home Assistant, the turn spent its call budget on shell placeholders and
stopped "repeating the same action without progress". So the model first
writes a short plan, then works through it one step at a time:

* ``set`` -- the steps (1 to 10). Each step buys the turn ``per_step`` more
  model calls, so a plan of six steps is not held to the budget of a one-line
  question.
* ``done`` -- a step finished, with a note. The person sees "✓ 2/6 …" as it
  happens.
* ``show`` -- the checklist so far.

Past ``limit_s`` in the chat, ``done`` tells the model to stop and write one
line; the loop hands the steps left to a sub-agent, with what is done, and the
answer arrives later in the same conversation (AgentLoop._run_agent_loop).
"""
from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (ArraySchema, IntegerSchema, StringSchema,
                                        tool_parameters_schema)

MAX_STEPS = 10
HEARTBEAT_S = 10.0


@dataclass
class TurnPlan:
    """One turn's plan. The loop creates it; the tool fills it in."""
    limit_s: float = 300.0
    per_step: int = 12
    max_budget: int = 160
    spec: Any = None                     # the AgentRunSpec, whose budget grows
    progress: Callable[[dict], Awaitable[None]] | None = None
    started: float = field(default_factory=time.monotonic)
    steps: list[str] = field(default_factory=list)
    done: dict[int, str] = field(default_factory=dict)
    handoff: bool = False
    # Carries out one step and returns {"text", "by", "tools"} (AgentLoop):
    # the planner stays the everyday model; the steps can run on another.
    executor: Callable[[int], Awaitable[dict]] | None = None
    results: dict[int, str] = field(default_factory=dict)
    by: dict[int, str] = field(default_factory=dict)
    tools: dict[int, list[str]] = field(default_factory=dict)
    running: int | None = None
    # The steps that may change something -- switch a lamp, add to a list,
    # send. The planner names them, and only when the person asked for that
    # change; every other step reads and nothing else (2026-09-24: a step
    # asked to cross-reference the lights turned every lamp in the house on).
    acts: set[int] = field(default_factory=set)

    def remaining(self) -> list[tuple[int, str]]:
        return [(i, s) for i, s in enumerate(self.steps, 1) if i not in self.done]

    def state(self, event: str) -> dict:
        """The checklist as the chat page draws it: the steps, which are done
        and with what note, and how long the turn has been at it."""
        return {"event": event, "steps": list(self.steps),
                "done": {str(i): n for i, n in self.done.items()},
                "running": self.running,
                "by": {str(i): b for i, b in self.by.items()},
                "tools": {str(i): t for i, t in self.tools.items()},
                "elapsed_s": int(time.monotonic() - self.started),
                "handoff": self.handoff}

    def summary(self) -> str:
        lines = []
        for i, s in enumerate(self.steps, 1):
            mark = "✓" if i in self.done else "·"
            note = f" -- {self.done[i]}" if self.done.get(i) else ""
            lines.append(f"{mark} {i}. {s}{note}")
        return "\n".join(lines)


_current: ContextVar[TurnPlan | None] = ContextVar("turn_plan", default=None)


async def say(plan: TurnPlan, event: str) -> None:
    """Send the checklist to whoever is watching the turn. Narration only: a
    page that has gone away never costs the turn anything."""
    if plan.progress is not None:
        try:
            await plan.progress(plan.state(event))
        except Exception:                                  # noqa: BLE001
            pass


def begin(plan: TurnPlan) -> Any:
    """Make *plan* this turn's; returns the token to hand to ``end``."""
    return _current.set(plan)


def end(token: Any) -> None:
    _current.reset(token)


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema("set: write the steps. run: carry out one step and get its result. "
                            "done: mark a step you did yourself. show: the checklist.",
                            enum=["set", "run", "done", "show"]),
        steps=ArraySchema(StringSchema("one step, as an exact instruction: the tool or skill "
                                       "action to call, its arguments, and what to report"),
                          description="For set: the steps, in order (1 to 10).", nullable=True),
        step=IntegerSchema(description="For run and done: the step's number.", nullable=True),
        note=StringSchema("For done: what came of it, in a few words.", nullable=True),
        acts=ArraySchema(IntegerSchema(description="a step number"),
                         description="For set: the steps that change something (switch, add, "
                                     "send, rename, delete) -- only ones the person asked for. "
                                     "Every other step can only read.", nullable=True),
        required=["action"],
    )
)
class PlanTool(Tool):
    """Plan a request that takes several steps, then tick the steps off."""

    @property
    def name(self) -> str:
        return "plan"

    @property
    def description(self) -> str:
        return ("For a request that takes several steps: set the steps first, then run them "
                "one at a time and read each result. Not for a one-step request.")

    async def _say(self, plan: TurnPlan, event: str) -> None:
        await say(plan, event)

    def _grant(self, plan: TurnPlan, calls: int) -> None:
        spec = plan.spec
        if spec is not None and calls > 0:
            spec.max_iterations = min(plan.max_budget, spec.max_iterations + calls)

    async def execute(self, action: str, steps: list[str] | None = None, step: int | None = None,
                      note: str | None = None, acts: list[int] | None = None,
                      **kwargs: Any) -> str:
        plan = _current.get()
        if plan is None:
            return "There is no plan in this turn: just do the request."
        if action == "set":
            clean = [str(s).strip()[:200] for s in (steps or []) if str(s).strip()][:MAX_STEPS]
            if not clean:
                return "Give the steps: a list of 1 to 10 short strings."
            granted_before = len(plan.steps)
            plan.steps, plan.done = clean, {}
            plan.acts = {int(a) for a in (acts or [])
                         if isinstance(a, int) and 1 <= a <= len(clean)}
            self._grant(plan, plan.per_step * max(0, len(clean) - granted_before))
            await self._say(plan, "set")
            if plan.executor is not None:
                reads = ("" if plan.acts else
                         " Every step only reads: to change something the person asked for, "
                         "set the plan again with that step in acts.")
                return (f"Plan saved, {len(clean)} steps.{reads} Call plan with action=run and step=1. "
                        f"Read each result before the next: change the plan (set again) if a "
                        f"result calls for it, and ask the person if a step needs their decision.")
            return (f"Plan saved, {len(clean)} steps. Do step 1 now ({clean[0]}), then call plan "
                    f"with action=done, step=1 and a short note. If a step needs the person's "
                    f"decision, stop and ask them.")
        if action == "done":
            if not plan.steps:
                return "Set the steps first (action=set)."
            if not isinstance(step, int) or not 1 <= step <= len(plan.steps):
                return f"step must be a number from 1 to {len(plan.steps)}."
            plan.done[step] = (note or "").strip()[:200]
            await self._say(plan, "done")
            left = plan.remaining()
            if not left:
                return "Every step is done. Write the answer for the person now."
            if time.monotonic() - plan.started > plan.limit_s:
                plan.handoff = True
                return ("Time is up for doing this in the chat. Do not call any more tools: "
                        "write one short line saying what is done so far. The steps left "
                        "continue in the background on their own, and the answer arrives here.")
            nxt, text = left[0]
            return f"Step {step} done. Next: step {nxt}, {text}."
        if action == "run":
            if not plan.steps:
                return "Set the steps first (action=set)."
            if not isinstance(step, int) or not 1 <= step <= len(plan.steps):
                return f"step must be a number from 1 to {len(plan.steps)}."
            if plan.executor is None:
                return f"Do step {step} yourself, then call plan with action=done."
            if plan.handoff or (plan.done and time.monotonic() - plan.started > plan.limit_s):
                plan.handoff = True
                return ("Time is up for doing this in the chat. Do not call any more tools: "
                        "write one short line saying what is done so far. The steps left "
                        "continue in the background on their own, and the answer arrives here.")
            plan.running = step
            await self._say(plan, "run")
            # A step on a local model can take minutes and says nothing while
            # it works. The checklist goes out again every few seconds: the
            # card's clock moves, and the API server's stall timer -- which
            # hands a silent turn to a sub-agent -- sees the turn is alive
            # (measured 2026-09-24: a 70 s step was taken for a dead turn).
            async def _heartbeat() -> None:
                while True:
                    await asyncio.sleep(HEARTBEAT_S)
                    await self._say(plan, "run")
            beat = asyncio.ensure_future(_heartbeat())
            try:
                res = await plan.executor(step)
            finally:
                beat.cancel()
                plan.running = None
            text = str(res.get("text") or "").strip() or "(the step returned nothing)"
            plan.results[step] = text[:3000]
            plan.by[step] = str(res.get("by") or "")
            plan.tools[step] = list(res.get("tools") or [])[:20]
            plan.done[step] = text.splitlines()[0][:160]
            await self._say(plan, "done")
            left = plan.remaining()
            nxt = (f"Next: step {left[0][0]}, {left[0][1]}." if left
                   else "Every step is done: write the answer for the person now. Say only "
                        "what the steps actually did -- nothing was tested or checked "
                        "unless a step's tools did it.")
            # A step that called nothing produced reasoning, not data: the
            # planner must know before it passes it on as fact.
            unchecked = ("" if plan.tools.get(step) else
                         "\n(This step called no tools: its result is the step model's own "
                         "reasoning over earlier results, not checked data. Verify it with a "
                         "tool, or present it as unconfirmed.)")
            return f"Step {step} result:\n{text[:3000]}{unchecked}\n\n{nxt}"
        if action == "show":
            return plan.summary() or "No steps yet."
        return "action must be set, done or show."

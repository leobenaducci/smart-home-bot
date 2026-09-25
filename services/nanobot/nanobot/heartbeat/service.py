"""Heartbeat service - periodic agent wake-up to check for tasks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
from nanobot.utils.instance_phase import phase_fraction
from nanobot.agent.usage_report import report_usage
from nanobot.utils.profiling import PROFILER

_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat",
            "description": "Report heartbeat decision after reviewing tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],
                        "description": "skip = nothing to do, run = has active tasks",
                    },
                    "tasks": {
                        "type": "string",
                        "description": "Natural-language summary of active tasks (required for run)",
                    },
                },
                "required": ["action"],
            },
        },
    }
]


# The shortest first wait a staggered instance may get. `start()` is awaited
# before `channels.start_all()`, so a tick seconds after boot can want a
# channel that has not connected yet.
_MIN_FIRST_WAIT_S = 60.0


class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    Phase 1 (decision): reads HEARTBEAT.md and asks the LLM — via a virtual
    tool call — whether there are active tasks.  This avoids free-text parsing
    and the unreliable HEARTBEAT_OK token.

    Phase 2 (execution): only triggered when Phase 1 returns ``run``.  The
    ``on_execute`` callback runs the task through the full agent loop and
    returns the result to deliver.
    """

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        on_execute: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
        timezone: str | None = None,
        reasoning_effort: str | None = None,
    ):
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.on_execute = on_execute
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self.timezone = timezone
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def heartbeat_file(self) -> Path:
        return self.workspace / "HEARTBEAT.md"

    def _read_heartbeat_file(self) -> str | None:
        if self.heartbeat_file.is_file():
            try:
                return self.heartbeat_file.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    async def _decide(self, content: str) -> tuple[str, str]:
        """Phase 1: ask LLM to decide skip/run via virtual tool call.

        Returns (action, tasks) where action is 'skip' or 'run'.
        """
        from nanobot.utils.helpers import current_time_str

        with PROFILER.span("job", label="heartbeat", model=self.model or ""):
            response = await self.provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": (
                        "You are a heartbeat agent. Call the heartbeat tool "
                        "to report your decision."
                    )},
                    {"role": "user", "content": (
                        f"Current Time: {current_time_str(self.timezone)}\n\n"
                        "Review the following HEARTBEAT.md and decide whether "
                        "there are active tasks.\n\n"
                        f"{content}"
                    )},
                ],
                tools=_HEARTBEAT_TOOL,
                model=self.model,
                # Passed only when set: `chat_with_retry` distinguishes "no
                # opinion" from an explicit value with a sentinel, and sending
                # None would be an opinion.
                **({"reasoning_effort": self.reasoning_effort}
                   if self.reasoning_effort else {}),
            )

        # This turn never reached the usage page. `report_usage` is called from
        # the agent loop, and the decision phase deliberately does not go
        # through it -- it is one provider call with a virtual tool -- so the
        # heartbeat was spending real money in a place whose entire job is
        # saying where the money goes. Six containers, forty-eight times a
        # day, invisible.
        #
        # `ev-heartbeat` rather than a bare key, so HomeCore's `_usage_scope`
        # files it as its own row instead of reading a missing key as
        # ordinary chat -- which would have hidden it a second way.
        #
        # Phase 2 is not reported here: when the decision says `run`, the work
        # goes through `on_execute` and the agent loop, which reports it
        # already. Reporting both would count one heartbeat twice.
        report_usage("ev-heartbeat", self.model,
                     getattr(response, "usage", None), 0)

        if not response.should_execute_tools:
            if response.has_tool_calls:
                logger.warning(
                    "Ignoring heartbeat tool calls under finish_reason='{}'",
                    response.finish_reason,
                )
            return "skip", ""

        args = response.tool_calls[0].arguments
        return args.get("action", "skip"), args.get("tasks", "")

    async def start(self) -> None:
        """Start the heartbeat service."""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return
        if self._running:
            logger.warning("Heartbeat already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Heartbeat started (every {}s)", self.interval_s)

    def stop(self) -> None:
        """Stop the heartbeat service."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    def _stagger_s(self) -> float:
        """How far into the interval this instance sits.

        The walk itself lives in `utils.instance_phase` because the heartbeat
        is not the only thing that herds: Dream and the morning greeting are
        registered identically on all five instances too. One walk, one place,
        so everything periodic lands in the same spread rather than each
        service inventing its own — and two generators that nothing keeps apart
        is exactly the collision this design already rejected once.
        """
        return self.interval_s * phase_fraction()

    async def _run_loop(self) -> None:
        """Main heartbeat loop."""
        # The offset moves this instance's phase; it must not lengthen the
        # blind window. Sleeping it *before* the loop put the first tick at
        # stagger + interval — 55.6 minutes for `user3` — so a redeploy, which
        # restarts every container, left HEARTBEAT.md unread for nearly an hour
        # on the instances furthest into the cycle. Taking the offset out of
        # the first wait instead spreads them exactly as well and keeps the
        # first check inside the interval the service advertises.
        stagger = self._stagger_s()
        wait = self.interval_s - stagger
        if wait < _MIN_FIRST_WAIT_S:
            # An offset near the top of the interval would otherwise put the
            # first tick seconds after start — and `start()` is awaited before
            # `channels.start_all()`, so that tick can want to deliver down a
            # channel that has not connected. Phase is modulo the interval, so
            # adding one back keeps this instance's slot exactly and costs it
            # one cycle. `user21` (frac .978) is the case: 39s without this.
            wait += self.interval_s
        logger.info(
            "Heartbeat phase offset {}s; first check in {}s", int(stagger), int(wait),
        )
        while self._running:
            try:
                await asyncio.sleep(wait)
                wait = self.interval_s
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat error: {}", e)

    async def _tick(self) -> None:
        """Execute a single heartbeat tick."""
        from nanobot.utils.evaluator import evaluate_response

        content = self._read_heartbeat_file()
        if not content:
            logger.debug("Heartbeat: HEARTBEAT.md missing or empty")
            return

        logger.info("Heartbeat: checking for tasks...")

        try:
            action, tasks = await self._decide(content)

            if action != "run":
                logger.info("Heartbeat: OK (nothing to report)")
                return

            logger.info("Heartbeat: tasks found, executing...")
            if self.on_execute:
                response = await self.on_execute(tasks)

                if response:
                    should_notify = await evaluate_response(
                        response, tasks, self.provider, self.model,
                    )
                    if should_notify and self.on_notify:
                        logger.info("Heartbeat: completed, delivering response")
                        await self.on_notify(response)
                    else:
                        logger.info("Heartbeat: silenced by post-run evaluation")
        except Exception:
            logger.exception("Heartbeat execution failed")

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat."""
        content = self._read_heartbeat_file()
        if not content:
            return None
        action, tasks = await self._decide(content)
        if action != "run" or not self.on_execute:
            return None
        return await self.on_execute(tasks)

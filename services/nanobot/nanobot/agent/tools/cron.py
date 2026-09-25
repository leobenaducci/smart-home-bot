"""Cron tool for scheduling reminders and tasks."""

import re
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from nanobot.agent.skill_invocation import (
    INVOCATION_DEBRIS_RE,
    SKILL_BLOCK_RE,
)
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronJobState, CronSchedule

# A skill-invocation block, fenced or bare. Meaningful only inside the turn that
# emits it, so scheduling one guarantees a job that does nothing.
# Shared with the message and exec tools so the three cannot drift: the same
# block that must not be echoed through a shell must not become a job body.
_SKILL_BLOCK_RE = SKILL_BLOCK_RE
_INVOCATION_DEBRIS_RE = INVOCATION_DEBRIS_RE

_CRON_PARAMETERS = tool_parameters_schema(
    action=StringSchema("Action to perform", enum=["add", "list", "remove"]),
    name=StringSchema(
        "Optional short human-readable label for the job "
        "(e.g., 'weather-monitor', 'daily-standup'). Defaults to first 30 chars of message."
    ),
    message=StringSchema(
        "REQUIRED when action='add'. Instruction for the agent to execute when the job triggers "
        "(e.g., 'Send a reminder to WeChat: xxx' or 'Check system status and report'). "
        "Not used for action='list' or action='remove'."
    ),
    every_seconds=IntegerSchema(0, description="Interval in seconds (for recurring tasks)"),
    delay_seconds=IntegerSchema(0, description="One-shot delay: run once after this many seconds from now (e.g. 10 for 'in 10 seconds'). Simpler than computing an absolute 'at' time."),
    cron_expr=StringSchema("Cron expression like '0 9 * * *' (for scheduled tasks)"),
    tz=StringSchema(
        "Optional IANA timezone for cron expressions (e.g. 'America/Vancouver'). "
        "When omitted with cron_expr, the tool's default timezone applies."
    ),
    at=StringSchema(
        "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00'). "
        "Naive values use the tool's default timezone."
    ),
    deliver=BooleanSchema(
        description="Whether to deliver the execution result to the user channel (default true)",
        default=True,
    ),
    job_id=StringSchema("REQUIRED when action='remove'. Job ID to remove (obtain via action='list')."),
    required=["action"],
    description=(
        "Action-specific parameters: add requires a non-empty message plus one schedule "
        "(delay_seconds, every_seconds, cron_expr, or at); remove requires job_id; list only needs action. "
        "Use delay_seconds for simple one-shot reminders like 'in 30 seconds' or 'in 5 minutes' without "
        "needing to know the current time. "
        "Per-action requirements are enforced at runtime (see field descriptions) so the "
        "top-level schema stays compatible with providers (e.g. OpenAI Codex/Responses) that "
        "reject oneOf/anyOf/allOf/enum/not at the root of function parameters."
    ),
)


@tool_parameters(_CRON_PARAMETERS)
class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(self, cron_service: CronService, default_timezone: str = "UTC"):
        self._cron = cron_service
        self._default_timezone = default_timezone
        self._channel: ContextVar[str] = ContextVar("cron_channel", default="")
        self._chat_id: ContextVar[str] = ContextVar("cron_chat_id", default="")
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel.set(channel)
        self._chat_id.set(chat_id)

    def set_cron_context(self, active: bool):
        """Mark whether the tool is executing inside a cron job callback."""
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token) -> None:
        """Restore previous cron context."""
        self._in_cron_context.reset(token)

    @staticmethod
    def _validate_timezone(tz: str) -> str | None:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(tz)
        except (KeyError, Exception):
            return f"Error: unknown timezone '{tz}'"
        return None

    def _display_timezone(self, schedule: CronSchedule) -> str:
        """Pick the most human-meaningful timezone for display."""
        return schedule.tz or self._default_timezone

    @staticmethod
    def _format_timestamp(ms: int, tz_name: str) -> str:
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz_name))
        return f"{dt.isoformat()} ({tz_name})"

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule reminders and recurring tasks. Actions: add, list, remove. "
            "Use delay_seconds for one-shot reminders relative to now (e.g. delay_seconds=30 for 'in 30 seconds'). "
            f"If tz is omitted, cron expressions and naive ISO times default to {self._default_timezone}."
        )

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        action = params.get("action")
        if action == "add" and not str(params.get("message") or "").strip():
            errors.append("message is required when action='add'")
        if action == "remove" and not str(params.get("job_id") or "").strip():
            errors.append("job_id is required when action='remove'")
        return errors

    async def execute(
        self,
        action: str,
        name: str | None = None,
        message: str = "",
        every_seconds: int | None = None,
        delay_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        deliver: bool = True,
        **kwargs: Any,
    ) -> str:
        if action == "add":
            if self._in_cron_context.get():
                return "Error: cannot schedule new jobs from within a cron job execution"
            return self._add_job(
                name,
                message,
                every_seconds=every_seconds,
                delay_seconds=delay_seconds,
                cron_expr=cron_expr,
                tz=tz,
                at=at,
                deliver=deliver,
            )
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    @staticmethod
    def _validate_message(message: str) -> str | None:
        """Reject a job body that will not mean anything when it fires.

        A cron job's message is executed or delivered days later, with nobody
        watching, so a malformed one fails silently and long after the mistake.
        One reached production reading exactly

            Recordatorio skill JSON invocation

        — a fragment of a skill-invocation block captured as the body during a
        confused turn. It sat queued for the next morning and would have run
        that string as a task.

        Only shapes that are certainly wrong are refused; anything a person
        might plausibly want scheduled is left alone.
        """
        text = message.strip()

        if _SKILL_BLOCK_RE.search(text):
            return (
                "Error: the cron message contains a skill-invocation block. Those "
                "are instructions to the runtime for the current turn, and mean "
                "nothing when the job fires later. Write what should happen in "
                "words instead — e.g. message=\"Saca una foto del patio y "
                "send it to me\" — and the agent will invoke the skill at that time."
            )

        if _INVOCATION_DEBRIS_RE.fullmatch(text):
            return (
                f"Error: {message!r} is not something that can run later — it "
                "reads as leftover plumbing rather than an instruction. Give the "
                "job the actual reminder text or task description."
            )

        if len(text) < 3:
            return (
                f"Error: the cron message {message!r} is too short to mean "
                "anything when it fires. Describe the reminder or task."
            )

        return None

    def _add_job(
        self,
        name: str | None,
        message: str,
        *,
        # Keyword-only past this point. These are six interchangeable
        # str|int|None slots, so inserting one shifts the meaning of every
        # positional call without any type error to catch it: adding
        # `delay_seconds` here once silently redirected a caller's cron
        # expression into it, and the schedule it produced was not the one
        # asked for.
        every_seconds: int | None = None,
        delay_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        deliver: bool = True,
    ) -> str:
        if not message:
            return (
                "Error: cron action='add' requires a non-empty 'message' parameter "
                "describing what to do when the job triggers "
                "(e.g. the reminder text). Retry including message=\"...\"."
            )
        if err := self._validate_message(message):
            return err
        channel = self._channel.get()
        chat_id = self._chat_id.get()
        if not channel or not chat_id:
            return "Error: no session context (channel/chat_id)"
        if tz and not cron_expr:
            return "Error: tz can only be used with cron_expr"
        if tz:
            if err := self._validate_timezone(tz):
                return err

        # Build schedule
        delete_after = False
        if delay_seconds:
            import time as _time
            at_ms = int(_time.time() * 1000) + delay_seconds * 1000
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        elif every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            effective_tz = tz or self._default_timezone
            if err := self._validate_timezone(effective_tz):
                return err
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=effective_tz)
        elif at:
            from zoneinfo import ZoneInfo

            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return f"Error: invalid ISO datetime format '{at}'. Expected format: YYYY-MM-DDTHH:MM:SS"
            if dt.tzinfo is None:
                if err := self._validate_timezone(self._default_timezone):
                    return err
                dt = dt.replace(tzinfo=ZoneInfo(self._default_timezone))
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return "Error: either delay_seconds, every_seconds, cron_expr, or at is required"

        job = self._cron.add_job(
            name=name or message[:30],
            schedule=schedule,
            message=message,
            deliver=deliver,
            channel=channel,
            to=chat_id,
            delete_after_run=delete_after,
        )
        return f"Created job '{job.name}' (id: {job.id})"

    def _format_timing(self, schedule: CronSchedule) -> str:
        """Format schedule as a human-readable timing string."""
        if schedule.kind == "cron":
            tz = f" ({schedule.tz})" if schedule.tz else ""
            return f"cron: {schedule.expr}{tz}"
        if schedule.kind == "every" and schedule.every_ms:
            ms = schedule.every_ms
            if ms % 3_600_000 == 0:
                return f"every {ms // 3_600_000}h"
            if ms % 60_000 == 0:
                return f"every {ms // 60_000}m"
            if ms % 1000 == 0:
                return f"every {ms // 1000}s"
            return f"every {ms}ms"
        if schedule.kind == "at" and schedule.at_ms:
            return f"at {self._format_timestamp(schedule.at_ms, self._display_timezone(schedule))}"
        return schedule.kind

    def _format_state(self, state: CronJobState, schedule: CronSchedule) -> list[str]:
        """Format job run state as display lines."""
        lines: list[str] = []
        display_tz = self._display_timezone(schedule)
        if state.last_run_at_ms:
            info = (
                f"  Last run: {self._format_timestamp(state.last_run_at_ms, display_tz)}"
                f" — {state.last_status or 'unknown'}"
            )
            if state.last_error:
                info += f" ({state.last_error})"
            lines.append(info)
        if state.next_run_at_ms:
            lines.append(f"  Next run: {self._format_timestamp(state.next_run_at_ms, display_tz)}")
        return lines

    @staticmethod
    def _system_job_purpose(job: CronJob) -> str:
        if job.name == "dream":
            return "Dream memory consolidation for long-term memory."
        return "System-managed internal job."

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = []
        for j in jobs:
            timing = self._format_timing(j.schedule)
            parts = [f"- {j.name} (id: {j.id}, {timing})"]
            if j.payload.kind == "system_event":
                parts.append(f"  Purpose: {self._system_job_purpose(j)}")
                parts.append("  Protected: visible for inspection, but cannot be removed.")
            parts.extend(self._format_state(j.state, j.schedule))
            lines.append("\n".join(parts))
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        result = self._cron.remove_job(job_id)
        if result == "removed":
            return f"Removed job {job_id}"
        if result == "protected":
            job = self._cron.get_job(job_id)
            if job and job.name == "dream":
                return (
                    "Cannot remove job `dream`.\n"
                    "This is a system-managed Dream memory consolidation job for long-term memory.\n"
                    "It remains visible so you can inspect it, but it cannot be removed."
                )
            return (
                f"Cannot remove job `{job_id}`.\n"
                "This is a protected system-managed cron job."
            )
        return f"Job {job_id} not found"

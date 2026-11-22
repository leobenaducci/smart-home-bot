"""household: cron additions (fork D3).

- ``validate_cron_message``: refuse a job body that cannot mean anything when
  it fires days later (a skill-invocation block, plumbing debris, a fragment).
- ``delay_to_at``: the cron tool's ``delay_seconds`` ("in 10 minutes"), as the
  ISO ``at`` upstream already understands.
- ``retarget_bound_job``: a session-bound job created in a HomeCore chat
  delivers into *today's* conversation, not the one it was created in. v0.4.1
  binds a job to its original session, so a daily reminder created in July
  keeps writing into July's chat; the fork rewrote the chat id at fire time
  and this keeps doing so, on a copy of the job.
"""

from __future__ import annotations

from typing import Any

import dataclasses
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from nanobot.cron.types import CronJob
from nanobot.household.skill_invocation import INVOCATION_DEBRIS_RE, SKILL_BLOCK_RE
from nanobot.utils.homeweb_chat_id import HOMEWEB_PREFIX, retarget_for_delivery


def validate_cron_message(message: str) -> str | None:
    text = message.strip()
    if SKILL_BLOCK_RE.search(text):
        return (
            "Error: the cron message contains a skill-invocation block. Those "
            "are instructions to the runtime for the current turn, and mean "
            "nothing when the job fires later. Write what should happen in "
            "words instead, and the skill will be invoked at that time."
        )
    if INVOCATION_DEBRIS_RE.fullmatch(text):
        return (
            f"Error: {message!r} is not something that can run later; it "
            "reads as leftover plumbing rather than an instruction. Give the "
            "job the actual reminder text or task description."
        )
    if len(text) < 3:
        return (
            f"Error: the cron message {message!r} is too short to mean "
            "anything when it fires. Describe the reminder or task."
        )
    return None


def delay_to_at(delay_seconds: int, timezone: str, now: float | None = None) -> str:
    when = (now if now is not None else time.time()) + int(delay_seconds)
    return datetime.fromtimestamp(when, ZoneInfo(timezone)).replace(microsecond=0).isoformat()


def retarget_bound_job(job: CronJob, timezone: str | None, now_ms: int | None = None) -> CronJob:
    """A copy of *job* whose HomeCore target is today's conversation."""
    payload = job.payload
    chat_id = payload.origin_chat_id or ""
    if payload.origin_channel != "websocket" or not chat_id.startswith(HOMEWEB_PREFIX):
        return job
    new_chat_id = retarget_for_delivery(chat_id, timezone, now_ms)
    if not new_chat_id or new_chat_id == chat_id:
        return job
    session_key = payload.session_key
    if session_key and session_key.endswith(chat_id):
        session_key = session_key[: -len(chat_id)] + new_chat_id
    new_payload = dataclasses.replace(payload, origin_chat_id=new_chat_id, session_key=session_key)
    return dataclasses.replace(job, payload=new_payload)


def heartbeat_runtime(loop: Any, config: Any) -> Any:
    """The fork's ``heartbeatModel``/``heartbeatProvider``/``heartbeatReasoningEffort``:
    the runtime the heartbeat's agent turn runs on, or None for the loop's own."""
    d = config.agents.defaults
    if not d.heartbeat_model:
        return None
    from nanobot.household.routing import _provider_for
    from nanobot.utils.llm_runtime import LLMRuntime

    try:
        provider_name = d.heartbeat_provider or d.provider
        provider = (_provider_for(config, provider_name, d.heartbeat_model, d.heartbeat_reasoning_effort)
                    if provider_name != d.provider or d.heartbeat_reasoning_effort else loop.provider)
    except Exception as exc:  # the heartbeat must still run
        from loguru import logger

        logger.warning("heartbeat model {} unavailable ({}); using the default", d.heartbeat_model, exc)
        return None
    return LLMRuntime.capture(provider, d.heartbeat_model, context_window_tokens=loop.context_window_tokens)

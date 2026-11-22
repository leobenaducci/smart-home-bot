"""Session retention (household A6; the fork's ``SessionManager.prune``).

Upstream never deletes a session. That is fine for a handful of long-lived
chats and not for anything that keys a session per day or per event: a house
relaying about 270 phone notifications a day reaches six figures of sessions
in a year, on a Raspberry Pi's SD card. The fork pruned at every start; here a
daily system cron job does it through ``SessionManager.state``, the async owner
of session state, so a running turn and the prune never race on the store.

Rules (the fork's, plus what upstream's store adds):

* a session untouched for longer than ``NANOBOT_SESSION_MAX_AGE_DAYS`` (60 by
  default; 0 or less turns pruning off) is deleted. "Touched" is the newest of
  the header's ``updated_at`` and the last history write;
* never a session held in memory (it may hold an unflushed turn);
* never a session with a running sub-agent (its result is announced there);
* upstream deletes a session's children with it, so a session whose
  descendants are not all old is kept too.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from nanobot.session.types import PARENT_SESSION_KEY

JOB_ID = "household-session-retention"
SCHEDULE = "30 4 * * *"   # daily, 04:30 local: quiet hours
DEFAULT_MAX_AGE_DAYS = 60


def max_age_days() -> int:
    try:
        return int(os.environ.get("NANOBOT_SESSION_MAX_AGE_DAYS", str(DEFAULT_MAX_AGE_DAYS)))
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS


def _last_touched(row: dict[str, Any]) -> float:
    """Epoch seconds of the newest write to the session, 0 when unreadable."""
    newest = 0.0
    try:
        updated = datetime.fromisoformat(str(row.get("updated_at")))
        if updated.tzinfo is None:
            updated = updated.astimezone()        # stored as local wall time
        newest = updated.timestamp()
    except (TypeError, ValueError):
        pass
    history_ms = row.get("history_updated_at_ms") or 0
    try:
        newest = max(newest, float(history_ms) / 1000)
    except (TypeError, ValueError):
        pass
    return newest


def _busy_owners(subagents: Any) -> set[str]:
    """Session keys with a sub-agent still running."""
    owners: set[str] = set()
    for record in list(getattr(subagents, "_tasks", {}).values()):
        status = getattr(record, "status", None)
        if status is not None and status.finished_at is None and status.owner:
            owners.add(status.owner)
    return owners


async def prune(sessions: Any, *, subagents: Any = None, days: int | None = None,
                now: datetime | None = None) -> int:
    """Delete the sessions the rules above allow; return how many."""
    days = max_age_days() if days is None else days
    if days <= 0:
        return 0
    state = sessions.state
    rows = await state.list_metadata()
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).timestamp()
    by_key = {row["key"]: row for row in rows}
    children: dict[str, list[str]] = {}
    for row in rows:
        parent = (row.get("metadata") or {}).get(PARENT_SESSION_KEY)
        if isinstance(parent, str):
            children.setdefault(parent, []).append(row["key"])
    busy = _busy_owners(subagents)

    def keep(key: str, seen: set[str]) -> bool:
        if key in seen:
            return False
        seen.add(key)
        row = by_key.get(key)
        if row is None:
            return False
        if key in busy or state.peek_metadata(key) is not None:
            return True
        if _last_touched(row) >= cutoff:
            return True
        return any(keep(child, seen) for child in children.get(key, ()))

    removed = 0
    for key in by_key:
        parent = (by_key[key].get("metadata") or {}).get(PARENT_SESSION_KEY)
        if isinstance(parent, str) and parent in by_key:
            continue                      # goes with its parent, or stays with it
        if keep(key, set()):
            continue
        try:
            if await state.delete(key):
                removed += 1
        except Exception as exc:          # a conflicting writer: next night
            logger.debug("session retention skipped {}: {}", key, exc)
    if removed:
        logger.info("Pruned {} session(s) untouched for over {}d", removed, days)
    return removed


__all__ = ["DEFAULT_MAX_AGE_DAYS", "JOB_ID", "SCHEDULE", "max_age_days", "prune"]

"""household A6: old sessions are pruned nightly on a real SQLite store."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from nanobot.household import session_retention
from nanobot.session.manager import Session, SessionManager
from nanobot.session.types import PARENT_SESSION_KEY


def _store(tmp_path, ages: dict[str, int], parents: dict[str, str] | None = None) -> SessionManager:
    manager = SessionManager(tmp_path)
    now = datetime.now()
    for key, days in ages.items():
        when = now - timedelta(days=days)
        meta = {PARENT_SESSION_KEY: parents[key]} if parents and key in parents else {}
        manager.save(Session(key=key, created_at=when, updated_at=when, metadata=meta,
                             messages=[{"role": "user", "content": "hola"}]))
    return manager


def _keys(manager: SessionManager) -> set[str]:
    return {row["key"] for row in manager.list_session_metadata()}


@pytest.mark.asyncio
async def test_sessions_untouched_past_the_limit_go_and_recent_ones_stay(tmp_path):
    manager = _store(tmp_path, {"phone:event-1": 90, "phone:event-2": 61,
                                "homeweb:999000111:main": 3, "phone:event-3": 59})
    removed = await session_retention.prune(manager, days=60)
    assert removed == 2
    assert _keys(manager) == {"homeweb:999000111:main", "phone:event-3"}


@pytest.mark.asyncio
async def test_a_session_with_a_running_subagent_is_kept(tmp_path):
    manager = _store(tmp_path, {"homeweb:999000111:old": 120, "phone:event-1": 120})
    running = SimpleNamespace(status=SimpleNamespace(finished_at=None, owner="homeweb:999000111:old"))
    done = SimpleNamespace(status=SimpleNamespace(finished_at=1.0, owner="phone:event-1"))
    subagents = SimpleNamespace(_tasks={"t1": running, "t2": done})
    assert await session_retention.prune(manager, subagents=subagents, days=60) == 1
    assert _keys(manager) == {"homeweb:999000111:old"}


@pytest.mark.asyncio
async def test_a_session_held_in_memory_is_kept(tmp_path):
    manager = _store(tmp_path, {"homeweb:999000111:tomi": 120})
    await manager.state.read("homeweb:999000111:tomi")
    if manager.state.peek_metadata("homeweb:999000111:tomi") is None:
        pytest.skip("read() does not cache on this store")
    assert await session_retention.prune(manager, days=60) == 0


@pytest.mark.asyncio
async def test_an_old_parent_with_a_recent_child_is_kept_with_it(tmp_path):
    manager = _store(
        tmp_path,
        {"homeweb:999000111:mora": 120, "subagent:a1": 2, "homeweb:999000111:pili": 120,
         "subagent:b1": 100},
        parents={"subagent:a1": "homeweb:999000111:mora", "subagent:b1": "homeweb:999000111:pili"},
    )
    assert await session_retention.prune(manager, days=60) == 1
    assert _keys(manager) == {"homeweb:999000111:mora", "subagent:a1"}


@pytest.mark.asyncio
async def test_zero_or_less_turns_pruning_off(tmp_path, monkeypatch):
    manager = _store(tmp_path, {"phone:event-1": 400})
    monkeypatch.setenv("NANOBOT_SESSION_MAX_AGE_DAYS", "0")
    assert await session_retention.prune(manager) == 0
    monkeypatch.setenv("NANOBOT_SESSION_MAX_AGE_DAYS", "junk")
    assert session_retention.max_age_days() == 60
    assert _keys(manager) == {"phone:event-1"}

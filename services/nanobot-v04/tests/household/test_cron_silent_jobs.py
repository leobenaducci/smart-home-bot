"""household: the fork's silent agent jobs keep running after the upgrade.

A fork agent job with ``deliver: false`` and no channel or target (a daily check
that alerts through ``ntfy-send`` itself and otherwise replies SILENCE) ran as a
private turn in ``cron:<job id>``. v0.4.1 disables every agent job that is not
bound to a chat session, which would have stopped such a check without a word.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.cron.service import CronService
from nanobot.household.cron_extras import is_silent_job, run_silent_job


def _store(tmp_path, payload: dict):
    path = tmp_path / "cron" / "jobs.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "jobs": [{
        "id": "watch1", "name": "backup-watch", "enabled": True,
        "schedule": {"kind": "cron", "expr": "30 6 * * *", "tz": "America/Santiago"},
        "payload": {"kind": "agent_turn", "message": "Check the backup page; reply SILENCE if green.",
                    **payload},
        "state": {"nextRunAtMs": 4_000_000_000_000, "lastStatus": "ok"},
        "createdAtMs": 1, "updatedAtMs": 1}]}), encoding="utf-8")
    return path


SILENT = {"deliver": False, "channel": None, "to": None}


def test_a_silent_fork_job_stays_enabled_and_scheduled(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    service = CronService(_store(tmp_path, SILENT))
    job = service.get_job("watch1")
    assert job.enabled is True
    assert job.state.last_status == "ok" and not job.state.last_error
    assert is_silent_job(job)


def test_without_the_household_upstream_still_disables_it(tmp_path, monkeypatch):
    monkeypatch.delenv("NANOBOT_HOUSEHOLD", raising=False)
    monkeypatch.setattr("nanobot.household.runtime.RUNTIME.loop", None, raising=False)
    job = CronService(_store(tmp_path, SILENT)).get_job("watch1")
    assert job.enabled is False and "recreate it from a chat session" in (job.state.last_error or "")


def test_a_job_that_meant_to_deliver_but_lost_its_target_is_still_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    job = CronService(_store(tmp_path, {"deliver": True})).get_job("watch1")
    assert job.enabled is False and "missing channel/to" in (job.state.last_error or "")


@pytest.mark.asyncio
async def test_it_runs_as_a_private_turn_in_its_own_session(tmp_path, monkeypatch):
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    job = CronService(_store(tmp_path, SILENT)).get_job("watch1")
    agent = SimpleNamespace(process_direct=AsyncMock(return_value=SimpleNamespace(content="SILENCE")))
    assert await run_silent_job(job, agent) == "SILENCE"
    args, kwargs = agent.process_direct.call_args
    assert kwargs["session_key"] == "cron:watch1"
    assert (kwargs["channel"], kwargs["chat_id"]) == ("cli", "direct")
    assert "Check the backup page" in args[0]


def test_the_gateway_runs_silent_jobs_before_refusing_unbound_ones():
    import inspect

    from nanobot.cli import gateway_runtime

    src = inspect.getsource(gateway_runtime)
    assert src.index("is_silent_job(job)") < src.index('reason = "unbound agent cron job')

"""Job-store edits must not cancel the timer task executing an agent turn.

Ported from upstream HKUDS/nanobot#5686, adapted to this fork's add_job.
"""

import asyncio
from pathlib import Path

import pytest

from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronSchedule


def _add(service: CronService, name: str) -> CronJob:
    return service.add_job(name=name, schedule=CronSchedule(kind="every", every_ms=3_600_000),
                           message=name)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["add", "update", "remove", "disable"])
@pytest.mark.parametrize("edit_from_callback", [True, False])
async def test_edit_during_timer_execution_preserves_turn(
    tmp_path: Path, action: str, edit_from_callback: bool,
) -> None:
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    outcomes: list[str] = []

    def edit_store() -> None:
        if action == "add":
            _add(service, "new-job")
        elif action == "update":
            service.update_job(other.id, message="updated")
        elif action == "remove":
            service.remove_job(other.id)
        else:
            service.enable_job(other.id, False)

    async def on_job(job: CronJob) -> None:
        entered.set()
        try:
            if edit_from_callback:
                edit_store()
            await release.wait()
            outcomes.append("completed")
        except asyncio.CancelledError:
            outcomes.append("cancelled")
            raise
        finally:
            settled.set()

    service = CronService(tmp_path / "cron" / "jobs.json", on_job=on_job)
    await service.start()
    try:
        other = _add(service, "other")
        active = _add(service, "active")
        active.state.next_run_at_ms = 1
        service._save_store()
        service._arm_timer()
        async with asyncio.timeout(2):
            await entered.wait()
            if not edit_from_callback:
                edit_store()
            release.set()
            await settled.wait()
        assert outcomes == ["completed"]

        async with asyncio.timeout(2):
            while service._timer_active:
                await asyncio.sleep(0)
        restored = CronService(service.store_path).get_job(active.id)
        assert restored is not None
        assert restored.state.last_status == "ok"
        assert len(restored.state.run_history) == 1
        assert restored.state.next_run_at_ms > restored.state.last_run_at_ms
        assert service._timer_task is not None and not service._timer_task.done()
    finally:
        timer = service._timer_task
        service.stop()
        if timer is not None:
            await asyncio.gather(timer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["remove", "disable", "reschedule"])
async def test_due_job_edited_while_waiting_is_skipped(tmp_path: Path, action: str) -> None:
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def on_job(job: CronJob) -> None:
        calls.append(job.name)
        if job.name == "first":
            entered.set()
            await release.wait()
        elif job.name == "last":
            finished.set()

    service = CronService(tmp_path / "cron" / "jobs.json", on_job=on_job)
    await service.start()
    try:
        for name in ("first", "waiting", "last"):
            _add(service, name)
        jobs = service.list_jobs()
        jobs.sort(key=lambda j: ("first", "waiting", "last").index(j.name))
        waiting = jobs[1]
        for job in jobs:
            job.state.next_run_at_ms = 1
        service._save_store()
        service._arm_timer()
        async with asyncio.timeout(2):
            await entered.wait()
            if action == "remove":
                service.remove_job(waiting.id)
            elif action == "disable":
                service.enable_job(waiting.id, False)
            else:
                service.update_job(waiting.id, schedule=CronSchedule(kind="every", every_ms=7_200_000))
            release.set()
            await finished.wait()
        assert calls == ["first", "last"]
    finally:
        timer = service._timer_task
        service.stop()
        if timer is not None:
            await asyncio.gather(timer, return_exceptions=True)

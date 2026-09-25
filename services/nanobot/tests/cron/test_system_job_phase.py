"""Dream and the morning greeting herd the same way the heartbeat did.

The heartbeat was fixed on 2026-08-24 after the profiler caught all five family
instances firing in the same second — 10:01:45, 10:31:46, 11:01:56 — and the
gateway answering the burst with errors that cost each of them 7.0s of retry
waiting. Two more periodic jobs have exactly the same shape and were not
touched by that fix:

- **Dream** is `kind="every"`, and `register_system_job` recomputes its anchor
  on every restart. One `docker compose up` starts five containers, so all five
  Dreams line up — and every deploy re-aligns whatever had drifted apart.
- **The morning greeting** is `0 7 * * *`, which is 07:00:00 on all five
  whatever anyone does.

So the phase moves up a level, into the registration both go through. Two
things it must not do: shift a reminder somebody typed, and turn `0 7 * * *`
into a greeting that arrives at half past.
"""
import time

from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.utils.instance_phase import phase_fraction

HOUR_MS = 3_600_000


def _service(tmp_path):
    return CronService(tmp_path / "cron" / "jobs.json")


def _every(job_id="dream", hours=2):
    return CronJob(
        id=job_id, name=job_id,
        schedule=CronSchedule(kind="every", every_ms=hours * HOUR_MS),
        payload=CronPayload(kind="system_event"),
    )


def _cron(expr="0 7 * * *", job_id="morning-greeting"):
    return CronJob(
        id=job_id, name=job_id,
        schedule=CronSchedule(kind="cron", expr=expr, tz="Etc/UTC"),
        payload=CronPayload(kind="system_event"),
    )


def _next_run(tmp_path, job, instance, monkeypatch):
    monkeypatch.setenv("NANOBOT_INSTANCE", instance)
    return _service(tmp_path).register_system_job(job).state.next_run_at_ms


# --- the interval case ------------------------------------------------------

def test_five_instances_do_not_dream_at_the_same_moment(tmp_path, monkeypatch):
    runs = sorted(_next_run(tmp_path / f"u{i}", _every(), f"user{i}", monkeypatch)
                  for i in range(1, 6))
    gaps = [b - a for a, b in zip(runs, runs[1:])]

    assert min(gaps) > 5 * 60_000, [round(g / 60_000, 1) for g in gaps]


def test_the_shift_never_pushes_the_first_run_past_one_interval(tmp_path, monkeypatch):
    """Subtracted, not added — the correction the heartbeat needed for the same
    reason. Adding would delay Dream's first run by up to two hours after every
    deploy, which is the blind window this exists to avoid."""
    now = int(time.time() * 1000)
    for i in range(1, 6):
        run = _next_run(tmp_path / f"u{i}", _every(), f"user{i}", monkeypatch)
        assert run - now <= 2 * HOUR_MS + 1000, f"user{i} waits longer than its interval"


def test_an_instance_near_the_top_of_the_cycle_still_waits_a_minute(tmp_path, monkeypatch):
    """`start()` runs before the channels connect, so a run seconds after boot
    wants a channel that is not there. user21's fraction is .978, which without
    a floor lands 39 seconds out."""
    now = int(time.time() * 1000)
    run = _next_run(tmp_path, _every(), "user21", monkeypatch)

    assert run - now >= 60_000, (run - now) / 1000
    assert phase_fraction("user21") > 0.9, "the case this is about"


def test_the_phase_survives_a_restart(tmp_path, monkeypatch):
    """Re-rolled per boot, a restart would be a fresh chance to collide — and
    these five restart together."""
    first = _next_run(tmp_path / "a", _every(), "user3", monkeypatch)
    second = _next_run(tmp_path / "b", _every(), "user3", monkeypatch)

    assert abs(first - second) < 2000, (first, second)


# --- the wall-clock case ----------------------------------------------------

def test_seven_am_stays_seven_am(tmp_path, monkeypatch):
    """A spread would be wrong here. `0 7 * * *` means seven, and a
    good-morning message at 07:26 is a different message — so this is a jitter
    big enough to miss the gateway burst and small enough that nobody could
    tell you it happened."""
    runs = [_next_run(tmp_path / f"u{i}", _cron(), f"user{i}", monkeypatch)
            for i in range(1, 6)]

    assert max(runs) - min(runs) <= 120_000, (max(runs) - min(runs)) / 1000
    assert len(set(runs)) == 5, "and still not all at the same instant"


# --- what must not move -----------------------------------------------------

def test_a_reminder_somebody_typed_is_not_shifted(tmp_path, monkeypatch):
    """The whole boundary. A system job is registered identically on five
    instances and has to be spread; a reminder is one person's, on one
    instance, and fires when they said it fires."""
    monkeypatch.setenv("NANOBOT_INSTANCE", "user3")
    service = _service(tmp_path)
    at = int(time.time() * 1000) + HOUR_MS

    job = service.add_job(
        name="sacar la basura",
        schedule=CronSchedule(kind="at", at_ms=at),
        message="sacar la basura",
    )

    assert job.state.next_run_at_ms == at


def test_a_deployment_of_one_is_not_delayed_at_all(tmp_path, monkeypatch):
    monkeypatch.delenv("NANOBOT_INSTANCE", raising=False)
    now = int(time.time() * 1000)
    run = _service(tmp_path).register_system_job(_every()).state.next_run_at_ms

    assert abs(run - (now + 2 * HOUR_MS)) < 2000

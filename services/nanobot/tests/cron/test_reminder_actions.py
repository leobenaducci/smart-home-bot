"""Answering a fired reminder from the notification shade.

Three buttons, three different meanings — and the reason they are three rather
than one "dismiss" is entirely in what they do to the job behind the reminder:

- **done** is about the occurrence, not the job. Having taken today's pills is
  not a reason to stop being reminded tomorrow, so a recurring job survives it.
- **discard** is about the job. That one stops.
- **snooze** asks again shortly, without moving the original — snoozing today's
  8 AM must not move tomorrow's.

A reminder that already deleted itself (the ordinary case for a one-time job,
which is gone by the time anyone reads the notification) is not an error: a
button that worked must not report "not found".
"""

import time

import pytest

from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


@pytest.fixture()
def cron(tmp_path):
    return CronService(store_path=tmp_path / "cron.json")


def _daily(cron, name="pastillas"):
    return cron.add_job(name=name, schedule=CronSchedule(kind="cron", expr="0 8 * * *"),
                        message="tomar las pastillas", deliver=True,
                        channel="websocket", to="homeweb:user1:2026-08-08")


def _one_off(cron, name="sacar la basura"):
    return cron.add_job(name=name,
                        schedule=CronSchedule(kind="at", at_ms=int(time.time() * 1000) + 60_000),
                        message="sacar la basura", deliver=True,
                        channel="websocket", to="homeweb:user1:2026-08-08",
                        delete_after_run=True)


def test_done_leaves_a_recurring_reminder_alone(cron):
    job = _daily(cron)
    # "done" says the thing was done, not that the reminder was wrong.
    assert cron.get_job(job.id) is not None
    assert cron.get_job(job.id).enabled


def test_discard_stops_a_recurring_reminder(cron):
    job = _daily(cron)
    assert cron.remove_job(job.id) == "removed"
    assert cron.get_job(job.id) is None


def test_discard_of_something_already_gone_is_not_an_error(cron):
    # The one-time case: it deleted itself when it fired, and the person is
    # answering a notification about a job that no longer exists.
    assert cron.remove_job("nunca-existio") == "not_found"


def test_snooze_adds_a_new_one_off_and_leaves_the_original(cron):
    job = _daily(cron)
    later = int(time.time() * 1000) + 15 * 60 * 1000
    snoozed = cron.add_job(name=f"{job.name} (pospuesto)",
                           schedule=CronSchedule(kind="at", at_ms=later),
                           message=job.payload.message, deliver=job.payload.deliver,
                           channel=job.payload.channel, to=job.payload.to,
                           delete_after_run=True)

    assert snoozed.id != job.id
    assert snoozed.schedule.kind == "at"
    assert snoozed.delete_after_run, "a snooze that outlives its firing is a second reminder forever"
    # Tomorrow's 8 AM is untouched.
    original = cron.get_job(job.id)
    assert original is not None and original.schedule.expr == "0 8 * * *"


def test_a_snooze_says_the_same_thing_as_the_reminder_it_came_from(cron):
    job = _one_off(cron)
    snoozed = cron.add_job(name=f"{job.name} (pospuesto)",
                           schedule=CronSchedule(kind="at",
                                                 at_ms=int(time.time() * 1000) + 900_000),
                           message=job.payload.message, deliver=job.payload.deliver,
                           channel=job.payload.channel, to=job.payload.to,
                           delete_after_run=True)
    # Same words and the same delivery target, or it arrives as a reminder
    # about nothing, in the wrong conversation.
    assert snoozed.payload.message == job.payload.message
    assert snoozed.payload.to == job.payload.to
    assert snoozed.payload.channel == job.payload.channel
    assert snoozed.payload.deliver is True

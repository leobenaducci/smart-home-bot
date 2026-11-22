"""household: cron delay_seconds, message validation, fire-time retargeting."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.household.cron_extras import delay_to_at, retarget_bound_job, validate_cron_message

TZ = "America/Argentina/Buenos_Aires"


def _job(chat_id: str, channel: str = "websocket") -> CronJob:
    return CronJob(
        id="j1", name="riego", schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz=TZ),
        payload=CronPayload(
            kind="agent_turn", message="Recordale a Tomi regar las plantas",
            session_key=f"{channel}:{chat_id}", origin_channel=channel, origin_chat_id=chat_id,
        ),
    )


def test_delay_seconds_becomes_a_future_at():
    at = delay_to_at(600, TZ, now=1_800_000_000)
    parsed = datetime.fromisoformat(at)
    assert parsed.tzinfo is not None
    assert parsed.timestamp() == 1_800_000_600


def test_skill_debris_is_refused():
    assert validate_cron_message('{"skill": "lights", "action": "on"}')
    assert validate_cron_message("ok")
    assert validate_cron_message("Recordale a Mora sacar la basura") is None


def test_a_july_homeweb_job_fires_into_todays_conversation():
    job = _job("homeweb:999000111:2026-07-01:1751370000000")
    now_ms = int(datetime(2026, 11, 17, 9, 0, tzinfo=ZoneInfo(TZ)).timestamp() * 1000)
    out = retarget_bound_job(job, TZ, now_ms)
    assert out is not job
    assert out.payload.origin_chat_id == f"homeweb:999000111:2026-11-17:{now_ms}"
    assert out.payload.session_key == f"websocket:homeweb:999000111:2026-11-17:{now_ms}"
    assert job.payload.origin_chat_id.endswith("1751370000000")  # the stored job is untouched


def test_a_space_scope_is_kept():
    job = _job("homeweb:999000111:2026-07-01:edu:1751370000000")
    out = retarget_bound_job(job, TZ, int(datetime(2026, 11, 17, tzinfo=ZoneInfo(TZ)).timestamp() * 1000))
    assert out.payload.origin_chat_id == "homeweb:999000111:2026-11-17:edu"


def test_other_channels_are_left_alone():
    job = _job("56999999999@s.whatsapp.net", channel="whatsapp")
    assert retarget_bound_job(job, TZ) is job


def test_cron_tool_accepts_delay_seconds(tmp_path):
    import asyncio

    from nanobot.agent.tools.context import RequestContext, request_context
    from nanobot.agent.tools.cron import CronTool
    from nanobot.cron.service import CronService

    service = CronService(tmp_path / "jobs.json")
    tool = CronTool(service, default_timezone=TZ)
    ctx = RequestContext(channel="websocket", chat_id="homeweb:999000111:2026-11-17:1",
                         session_key="websocket:homeweb:999000111:2026-11-17:1")
    with request_context(ctx):
        out = asyncio.run(tool.execute(action="add", message="Recordale a Juana la reunión",
                                       delay_seconds=600))
        bad = asyncio.run(tool.execute(action="add", message='{"skill":"menu","action":"x"}',
                                       delay_seconds=60))
    assert "Created job" in out, out
    assert "skill-invocation block" in bad
    jobs = service.list_jobs()
    assert jobs[0].schedule.kind == "at" and jobs[0].delete_after_run


def test_heartbeat_runs_on_its_own_model_when_set():
    from types import SimpleNamespace

    from nanobot.config.schema import Config
    from nanobot.household.cron_extras import heartbeat_runtime

    loop = SimpleNamespace(provider=object(), context_window_tokens=1000)
    assert heartbeat_runtime(loop, Config()) is None
    cfg = Config.model_validate({"agents": {"defaults": {"model": "m", "heartbeatModel": "small"}}})
    runtime = heartbeat_runtime(loop, cfg)
    assert runtime.model == "small" and runtime.provider is loop.provider

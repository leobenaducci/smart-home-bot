import asyncio

import pytest

from nanobot.heartbeat.service import HeartbeatService
from nanobot.utils.instance_phase import phase_fraction
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class DummyProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="", tool_calls=[])

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_start_is_idempotent(tmp_path) -> None:
    provider = DummyProvider([])

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        interval_s=9999,
        enabled=True,
    )

    await service.start()
    first_task = service._task
    await service.start()

    assert service._task is first_task

    service.stop()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_decide_returns_skip_when_no_tool_call(tmp_path) -> None:
    provider = DummyProvider([LLMResponse(content="no tool call", tool_calls=[])])
    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
    )

    action, tasks = await service._decide("heartbeat content")
    assert action == "skip"
    assert tasks == ""


@pytest.mark.asyncio
async def test_trigger_now_executes_when_decision_is_run(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] do thing", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check open tasks"},
                )
            ],
        )
    ])

    called_with: list[str] = []

    async def _on_execute(tasks: str) -> str:
        called_with.append(tasks)
        return "done"

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
    )

    result = await service.trigger_now()
    assert result == "done"
    assert called_with == ["check open tasks"]


@pytest.mark.asyncio
async def test_trigger_now_returns_none_when_decision_is_skip(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] do thing", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "skip"},
                )
            ],
        )
    ])

    async def _on_execute(tasks: str) -> str:
        return tasks

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
    )

    assert await service.trigger_now() is None


@pytest.mark.asyncio
async def test_tick_notifies_when_evaluator_says_yes(tmp_path, monkeypatch) -> None:
    """Phase 1 run -> Phase 2 execute -> Phase 3 evaluate=notify -> on_notify called."""
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] check deployments", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check deployments"},
                )
            ],
        ),
    ])

    executed: list[str] = []
    notified: list[str] = []

    async def _on_execute(tasks: str) -> str:
        executed.append(tasks)
        return "deployment failed on staging"

    async def _on_notify(response: str) -> None:
        notified.append(response)

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
        on_notify=_on_notify,
    )

    async def _eval_notify(*a, **kw):
        return True

    monkeypatch.setattr("nanobot.utils.evaluator.evaluate_response", _eval_notify)

    await service._tick()
    assert executed == ["check deployments"]
    assert notified == ["deployment failed on staging"]


@pytest.mark.asyncio
async def test_tick_suppresses_when_evaluator_says_no(tmp_path, monkeypatch) -> None:
    """Phase 1 run -> Phase 2 execute -> Phase 3 evaluate=silent -> on_notify NOT called."""
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] check status", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check status"},
                )
            ],
        ),
    ])

    executed: list[str] = []
    notified: list[str] = []

    async def _on_execute(tasks: str) -> str:
        executed.append(tasks)
        return "everything is fine, no issues"

    async def _on_notify(response: str) -> None:
        notified.append(response)

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
        on_notify=_on_notify,
    )

    async def _eval_silent(*a, **kw):
        return False

    monkeypatch.setattr("nanobot.utils.evaluator.evaluate_response", _eval_silent)

    await service._tick()
    assert executed == ["check status"]
    assert notified == []


@pytest.mark.asyncio
async def test_decide_retries_transient_error_then_succeeds(tmp_path, monkeypatch) -> None:
    provider = DummyProvider([
        LLMResponse(content="429 rate limit", finish_reason="error"),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check open tasks"},
                )
            ],
        ),
    ])

    delays: list[int] = []

    async def _fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
    )

    action, tasks = await service._decide("heartbeat content")

    assert action == "run"
    assert tasks == "check open tasks"
    assert provider.calls == 2
    assert delays == [1]


@pytest.mark.asyncio
async def test_decide_prompt_includes_current_time(tmp_path) -> None:
    """Phase 1 user prompt must contain current time so the LLM can judge task urgency."""

    captured_messages: list[dict] = []

    class CapturingProvider(LLMProvider):
        async def chat(self, *, messages=None, **kwargs) -> LLMResponse:
            if messages:
                captured_messages.extend(messages)
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="hb_1", name="heartbeat",
                        arguments={"action": "skip"},
                    )
                ],
            )

        def get_default_model(self) -> str:
            return "test-model"

    service = HeartbeatService(
        workspace=tmp_path,
        provider=CapturingProvider(),
        model="test-model",
    )

    await service._decide("- [ ] check servers at 10:00 UTC")

    user_msg = captured_messages[1]
    assert user_msg["role"] == "user"
    assert "Current Time:" in user_msg["content"]



# --- five instances must not knock on the same door at the same instant ------
#
# Measured on the live box, 2026-08-24: user1..user5 fired at 10:01:45,
# 10:31:46 and 11:01:56 — the same second every time, because one
# `docker compose up` starts them all and each then sleeps a fixed interval.
# The gateway answered the burst with errors and every instance spent 7.0s in
# retry waits for a 540-token heartbeat. It also defeats the shared outage
# window, which can only help an instance that fails after somebody else has
# already written the file.


def _stagger(tmp_path, instance, monkeypatch, interval_s=1800):
    if instance is None:
        monkeypatch.delenv("NANOBOT_INSTANCE", raising=False)
    else:
        monkeypatch.setenv("NANOBOT_INSTANCE", instance)
    return HeartbeatService(
        workspace=tmp_path, provider=DummyProvider([]), model="m",
        interval_s=interval_s,
    )._stagger_s()


def _min_circular_gap(offsets, interval_s=1800):
    """Phase is modulo the interval, so the seam counts.

    Linear `b - a` over a sorted list ignores the wrap: two instances at 5s and
    1795s pass it while firing ten seconds apart, every cycle, which is the
    herd this file exists to prevent.
    """
    ordered = sorted(offsets)
    return min((ordered[(i + 1) % len(ordered)] - o) % interval_s
               for i, o in enumerate(ordered))



def _apart(a, b, interval_s=1800):
    """How far two phases are, going either way round the interval."""
    d = abs(a - b) % interval_s
    return min(d, interval_s - d)


def test_the_family_instances_land_minutes_apart(tmp_path, monkeypatch):
    offsets = [_stagger(tmp_path, f"user{i}", monkeypatch) for i in range(1, 6)]

    assert all(0 <= o < 1800 for o in offsets), offsets
    assert _min_circular_gap(offsets) > 120, \
        f"still a herd: {[round(o / 60, 1) for o in sorted(offsets)]}"


def test_a_named_unnumbered_instance_does_not_land_on_a_member(tmp_path, monkeypatch):
    """The case the old test could not see.

    It asked about `casa`, which sets no NANOBOT_INSTANCE in this repo and so
    took the `return 0.0` path -- while a deployment that *names* its unnumbered
    instance took the sha256 branch instead. `NANOBOT_INSTANCE=house` landed
    26.5s from `user3`: two unrelated generators with nothing keeping them
    apart, and permanent, because both are deterministic.
    """
    live = [_stagger(tmp_path, n, monkeypatch)
            for n in ("user1", "user2", "user3", "user4", "user5", "house")]

    assert _min_circular_gap(live) > 120, \
        f"herd: {[round(o / 60, 1) for o in sorted(live)]}"


def test_the_offset_survives_a_restart(tmp_path, monkeypatch):
    """Re-rolled on every boot, a restart is a fresh chance to collide — and
    these five restart together, which is exactly when it would.

    Pinned to the value rather than compared with itself: calling a pure
    function twice in one process proves purity, not cross-process stability.
    The classic way to lose that is `hash(name)`, which PYTHONHASHSEED
    randomises per process and which would pass a self-comparison every time.
    """
    assert _stagger(tmp_path, "user3", monkeypatch) == pytest.approx(1537.38, abs=0.01)


def test_a_sixth_instance_lands_in_a_gap_rather_than_on_somebody(tmp_path, monkeypatch):
    """What the golden-ratio walk buys over a hash: it does not need to know
    how many instances there are to keep filling the largest hole."""
    existing = [_stagger(tmp_path, f"user{i}", monkeypatch) for i in range(1, 6)]
    sixth = _stagger(tmp_path, "user6", monkeypatch)

    assert min(abs(sixth - o) for o in existing) > 120


def test_an_unnumbered_instance_is_the_base_the_others_spread_around(tmp_path, monkeypatch):
    """Index 0, which is what the unset case already returned -- one walk, not
    a second generator that can land anywhere."""
    for name in ("casa", "house"):
        assert _stagger(tmp_path, name, monkeypatch) == 0.0


def test_the_deployer_s_rank_beats_the_digits_in_the_name(tmp_path, monkeypatch):
    """The id is not the rank once a household has seen departures.

    Ids are monotonic and never reused, so `user2` can be running beside
    `user15`. The walk is low-discrepancy over a consecutive run and says
    nothing about a sparse one. Only the deployer holds the live list.
    """
    monkeypatch.setenv("NANOBOT_INSTANCE", "user15")
    monkeypatch.setenv("NANOBOT_STAGGER_INDEX", "2")
    ranked = phase_fraction()

    monkeypatch.delenv("NANOBOT_STAGGER_INDEX")
    by_id = phase_fraction()

    assert ranked != by_id
    assert ranked == pytest.approx(phase_fraction("user2"), abs=1e-9)


def test_a_sparse_household_is_spread_by_rank_and_herds_by_id(monkeypatch):
    """Three members left after departures: ranked they are minutes apart,
    numbered two of them are seconds apart -- and nothing prints differently."""
    live = ["user2", "user15", "user49"]
    interval = 1800

    monkeypatch.delenv("NANOBOT_STAGGER_INDEX", raising=False)
    by_id = [phase_fraction(n) * interval for n in live]
    assert min(_apart(a, b) for i, a in enumerate(by_id)
               for b in by_id[i + 1:]) < 120, by_id

    ranked = []
    for rank, name in enumerate(live, start=1):
        monkeypatch.setenv("NANOBOT_INSTANCE", name)
        monkeypatch.setenv("NANOBOT_STAGGER_INDEX", str(rank))
        ranked.append(phase_fraction() * interval)
    assert min(_apart(a, b) for i, a in enumerate(ranked)
               for b in ranked[i + 1:]) > 120, ranked


def test_a_junk_rank_falls_back_rather_than_crashing_the_service(monkeypatch):
    """It arrives through compose interpolation; an unset variable with a `:-`
    default is the empty string, not an absent one."""
    monkeypatch.setenv("NANOBOT_INSTANCE", "user3")
    for junk in ("", "   ", "two", "3.5"):
        monkeypatch.setenv("NANOBOT_STAGGER_INDEX", junk)
        assert phase_fraction() == pytest.approx(phase_fraction("user3"), abs=1e-9)


def test_a_deployment_of_one_waits_for_nobody(tmp_path, monkeypatch):
    """No NANOBOT_INSTANCE is the CLI and the single-instance install: there is
    nothing to collide with, and a first heartbeat delayed by a quarter of an
    hour for no reason is its own small bug."""
    assert _stagger(tmp_path, None, monkeypatch) == 0.0


@pytest.mark.asyncio
async def test_the_stagger_is_waited_once_and_not_per_tick(tmp_path, monkeypatch):
    """It shifts the phase; it does not slow the rhythm down."""
    monkeypatch.setenv("NANOBOT_INSTANCE", "user2")
    service = HeartbeatService(
        workspace=tmp_path, provider=DummyProvider([]), model="m", interval_s=1800,
    )
    slept: list[float] = []

    async def fake_sleep(delay):
        slept.append(delay)
        if len(slept) >= 3:
            service._running = False

    async def no_tick():
        return None            # not asyncio.sleep(0): the fake above counts it

    monkeypatch.setattr("nanobot.heartbeat.service.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(service, "_tick", no_tick)
    service._running = True
    await service._run_loop()

    # The offset comes *out of* the first wait. Sleeping it before the loop put
    # the first tick at stagger + interval -- 55.6 minutes for `user3` -- so a
    # redeploy left HEARTBEAT.md unread for nearly an hour.
    assert slept[0] == pytest.approx(1800 - service._stagger_s())
    assert slept[1:] == [1800, 1800]


@pytest.mark.asyncio
async def test_the_first_check_is_never_seconds_after_boot(tmp_path, monkeypatch):
    """`start()` is awaited before `channels.start_all()`.

    An offset near the top of the interval leaves `interval - stagger` tiny:
    `user21` (frac .978) would tick 39s after boot, wanting a channel that has
    not connected. One interval is added back, which keeps the phase exactly.
    """
    monkeypatch.setenv("NANOBOT_INSTANCE", "user21")
    service = HeartbeatService(
        workspace=tmp_path, provider=DummyProvider([]), model="m", interval_s=1800,
    )
    slept: list[float] = []

    async def fake_sleep(delay):
        slept.append(delay)
        service._running = False

    monkeypatch.setattr("nanobot.heartbeat.service.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(service, "_tick", lambda: None)
    service._running = True
    await service._run_loop()

    assert slept[0] >= 60, slept
    assert slept[0] % 1800 == pytest.approx((1800 - service._stagger_s()) % 1800)

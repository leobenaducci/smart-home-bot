"""A turn has to be able to say where its time and tokens went.

Two numbers used to describe a turn: how long it took, and what it cost. Both
hide the thing you need when one of them is wrong. "Nine seconds" is time in
the model, time asleep between retries the model caused, time in tools, and our
own overhead — and only the last is ours to fix. "Sixty thousand prompt tokens"
is a bill that turns almost entirely on how much of it the gateway served from
cache, which is invisible unless somebody counts `cached_tokens`.

These pin the properties the panel is read for: that a call's ladder is
measured and not guessed, that the four parts of a turn add back up to the
turn, that a fallback is visible as a fallback, and that the reply nobody
received — the empty one that finished `length` after spending its whole
budget reasoning — is counted as the failure it is rather than as a success.
"""
import asyncio
import time
from unittest.mock import MagicMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunResult
from nanobot.providers import base as provider_base
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse
from nanobot.utils.profiling import PROFILER, Profiler, _merge_spans


@pytest.fixture(autouse=True)
def _clean_profiler():
    PROFILER.clear()
    PROFILER.enabled = True
    yield
    PROFILER.clear()


class _Provider(LLMProvider):
    """Answers on a schedule, so the ladder under test is the real one."""

    def __init__(self, responses, *, fallback_model=None, api_base="https://gw.test/v1",
                 delay=0.0, default="main-model"):
        super().__init__(api_base=api_base)
        self._responses = list(responses)
        self._default = default
        self.delay = delay
        self.generation = GenerationSettings(fallback_model=fallback_model)

    async def chat(self, *args, **kwargs) -> LLMResponse:
        # A blocking sleep on purpose: the fixture below fakes asyncio.sleep on
        # the module itself, so an awaited one would take no time and the test
        # measuring where a turn's time went would have none to find.
        if self.delay:
            time.sleep(self.delay)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    def get_default_model(self) -> str:
        return self._default


def _ok(content="listo", **kw):
    return LLMResponse(content=content, usage={
        "prompt_tokens": 1000, "completion_tokens": 40, "cached_tokens": 900,
    }, **kw)


def _err():
    return LLMResponse(content="500 internal", finish_reason="error", error_status_code=500)


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """The ladder's waits are real seconds. Skipped, but still awaited, so the
    ordering the ladder depends on is unchanged."""
    real = asyncio.sleep

    async def fake(delay, result=None):
        await real(0)
        return result

    monkeypatch.setattr(provider_base.asyncio, "sleep", fake)


# --- one call ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_call_is_recorded_with_its_tokens_and_its_cache_hit():
    provider = _Provider([_ok()])

    await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    call = PROFILER.snapshot()["recent_calls"][0]
    assert call["tokens"]["prompt_tokens"] == 1000
    assert call["tokens"]["cached_tokens"] == 900
    assert call["cache_hit_pct"] == 90.0, "the lever, precomputed"
    assert call["attempts"] == 1
    # Not `== 0`: `wait_s` is `duration_s - request_s`, two independent
    # perf_counter spans, so a single attempt leaves the bookkeeping between
    # them -- usually rounding to 0.0 and occasionally to 0.1. The claim is
    # that nothing *waited*, and the ladder's shortest delay is 1s, so any
    # real sleep is three orders of magnitude above this bound.
    assert call["retry_wait_ms"] < 5, call["retry_wait_ms"]
    assert call["gateway"] == "gw.test"


@pytest.mark.asyncio
async def test_the_ladder_is_counted_not_guessed():
    """Three failures and an answer is four requests, and the panel has to say
    four — a turn that looks slow because it was retried three times is a
    different problem from a turn on a slow model."""
    provider = _Provider([_err(), _err(), _err(), _ok()])

    await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    call = PROFILER.snapshot()["recent_calls"][0]
    assert call["attempts"] == 4, "three failures and the answer, not one call"
    assert call["finish_reason"] == "stop"
    assert PROFILER.snapshot()["totals"]["retries"] == 3


@pytest.mark.asyncio
async def test_a_fallback_is_visible_as_one():
    provider = _Provider([_err(), _err(), _err(), _err(), _ok()],
                         fallback_model="backup-model")

    await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    call = PROFILER.snapshot()["recent_calls"][0]
    assert call["model"] == "main-model"
    assert call["served_by"] == "backup-model"
    assert PROFILER.snapshot()["totals"]["fallbacks"] == 1


@pytest.mark.asyncio
async def test_the_reply_nobody_received_is_counted_as_a_failure():
    """finish_reason `length` with an empty message: the model spent its whole
    completion budget reasoning. It reaches the family as Alfred not answering
    and reaches the log as a success — this house has been bitten by it, and
    the panel is where it should be countable."""
    provider = _Provider([LLMResponse(content="", finish_reason="length", usage={
        "prompt_tokens": 500, "completion_tokens": 6000, "reasoning_tokens": 6000,
    })])

    await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    call = PROFILER.snapshot()["recent_calls"][0]
    assert call["empty"] is True
    assert call["truncated"] is True
    assert PROFILER.snapshot()["totals"]["empty_replies"] == 1


@pytest.mark.asyncio
async def test_a_provider_that_throws_is_recorded_as_the_error_it_became():
    """`_safe_chat` turns a thrown socket into an error response, so this is
    what the ladder actually sees and what the panel should show."""

    class _Boom(_Provider):
        async def chat(self, *args, **kwargs):
            raise RuntimeError("socket closed")

    provider = _Boom([_ok()])
    await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    call = PROFILER.snapshot()["recent_calls"][0]
    assert call["finish_reason"] == "error"
    assert "socket closed" in (call.get("error") or "")


@pytest.mark.asyncio
async def test_an_exception_escaping_the_ladder_is_still_recorded(monkeypatch):
    """Otherwise the profile is quietly a record of the calls that worked."""
    provider = _Provider([_ok()])

    async def boom(*args, **kwargs):
        raise RuntimeError("cancelled mid-flight")

    monkeypatch.setattr(provider, "_run_with_retry_inner", boom)
    with pytest.raises(RuntimeError):
        await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    call = PROFILER.snapshot()["recent_calls"][0]
    assert call["finish_reason"] == "exception"
    assert "cancelled mid-flight" in call["error"]


# --- one turn ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_turn_collects_the_calls_made_inside_it(tmp_path, monkeypatch):
    provider = _Provider([_ok()])
    loop = AgentLoop(bus=MagicMock(), provider=provider, workspace=tmp_path,
                     model="main-model")

    async def fake_run(spec):
        await provider.chat_with_retry(messages=spec.initial_messages)
        await provider.chat_with_retry(messages=spec.initial_messages)
        return AgentRunResult(final_content="ok", messages=[])

    monkeypatch.setattr(loop.runner, "run", fake_run)
    messages = ContextBuilder(tmp_path).build_messages(history=[], current_message="hola")
    await loop._run_agent_loop(messages, channel="websocket", chat_id="homeweb:1")

    span = PROFILER.snapshot()["recent_spans"][0]
    assert span["kind"] == "turn"
    assert span["calls"] == 2
    assert span["tokens"]["prompt_tokens"] == 2000
    assert span["cache_hit_pct"] == 90.0
    assert span["session_key"] == "websocket:homeweb:1"
    assert span["model"] == "main-model"
    assert PROFILER.snapshot()["recent_calls"][0]["scope"] == "turn"


@pytest.mark.asyncio
async def test_the_parts_of_a_turn_add_back_up_to_the_turn(tmp_path, monkeypatch):
    """llm + wait + tools + other = the turn. If they did not, the breakdown
    would send somebody optimising a part that is not there."""
    provider = _Provider([_ok()], delay=0.02)
    loop = AgentLoop(bus=MagicMock(), provider=provider, workspace=tmp_path,
                     model="main-model")

    async def fake_run(spec):
        await provider.chat_with_retry(messages=spec.initial_messages)
        time.sleep(0.01)                   # stands in for our own overhead
        return AgentRunResult(final_content="ok", messages=[])

    monkeypatch.setattr(loop.runner, "run", fake_run)
    await loop._run_agent_loop([{"role": "user", "content": "hola"}])

    s = PROFILER.snapshot()["recent_spans"][0]
    parts = s["llm_ms"] + s["retry_wait_ms"] + s["tools_wall_ms"] + s["other_ms"]
    assert abs(parts - s["duration_ms"]) < 1.0, s
    assert s["llm_ms"] >= 15, "the model's own time is in there"
    assert s["other_ms"] >= 5, "and so is ours"


@pytest.mark.asyncio
async def test_a_turn_that_falls_back_says_so_on_the_span(tmp_path, monkeypatch):
    provider = _Provider([_ok()], fallback_model="backup-model")
    loop = AgentLoop(bus=MagicMock(), provider=provider, workspace=tmp_path,
                     model="main-model")

    async def fake_run(spec):
        return AgentRunResult(final_content="ok", messages=[],
                              served_by_model="backup-model")

    monkeypatch.setattr(loop.runner, "run", fake_run)
    await loop._run_agent_loop([{"role": "user", "content": "hola"}])

    assert PROFILER.snapshot()["recent_spans"][0]["served_by"] == "backup-model"


@pytest.mark.asyncio
async def test_a_job_inside_a_turn_is_attributed_to_itself(tmp_path, monkeypatch):
    """Consolidation runs inside somebody's turn and spends a whole prompt of
    its own. Unlabelled it reads as that turn being mysteriously slow."""
    provider = _Provider([_ok()])
    loop = AgentLoop(bus=MagicMock(), provider=provider, workspace=tmp_path,
                     model="main-model")

    async def fake_run(spec):
        with PROFILER.span("job", "websocket:homeweb:1", label="consolidate"):
            await provider.chat_with_retry(messages=[])
        return AgentRunResult(final_content="ok", messages=[])

    monkeypatch.setattr(loop.runner, "run", fake_run)
    await loop._run_agent_loop([{"role": "user", "content": "hola"}],
                               channel="websocket", chat_id="homeweb:1")

    snap = PROFILER.snapshot()
    assert snap["recent_calls"][0]["scope"] == "job:consolidate"
    kinds = {s["kind"] for s in snap["recent_spans"]}
    assert kinds == {"turn", "job"}
    turn = [s for s in snap["recent_spans"] if s["kind"] == "turn"][0]
    assert turn["calls"] == 0, "the job's call is the job's, not the turn's"


# --- what the prompt is made of ---------------------------------------------

def test_a_turn_records_what_its_prompt_is_made_of():
    """The token count is the bill; this is the reason for it. Measured on the
    live box: a notification turn sent 30,303 prompt tokens and the session
    behind it held five messages and 3.5 KB — so the history everyone assumed
    was the problem was about 3% of the prompt, and nothing could say what the
    other 99% was."""
    from nanobot.agent.loop import _prompt_shape

    class _Tools:
        def get_definitions(self):
            return [{"name": "exec", "description": "run a command"},
                    {"name": "web_search", "description": "search"}]

    shape = _prompt_shape([
        {"role": "system", "content": "x" * 40_000},
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "buenas"},
    ], _Tools())

    assert shape["system_chars"] == 40_000
    assert shape["history_msgs"] == 2
    assert shape["history_chars"] == len("hola") + len("buenas")
    assert shape["tool_count"] == 2
    assert shape["tool_chars"] > 50


def test_a_multimodal_turn_is_measured_by_its_text():
    """An image is a data: URI worth tens of thousands of characters and no
    tokens like the ones being counted here — including it would drown the
    number it is there to explain."""
    from nanobot.agent.loop import _prompt_shape

    class _Tools:
        def get_definitions(self):
            return []

    shape = _prompt_shape([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [
            {"type": "text", "text": "¿qué es esto?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 50_000}},
        ]},
    ], _Tools())

    assert shape["history_chars"] == len("¿qué es esto?")


def test_a_tool_call_is_counted_where_the_model_is_billed_for_it():
    """An assistant message that ran a tool carries an empty `content` and
    kilobytes of argument JSON under `tool_calls`, and the provider sends all
    of it (`_ALLOWED_MSG_KEYS`). Counting `content` alone reported ~0 chars for
    the histories that are actually large — the exact wrong answer the column
    was added to prevent."""
    from nanobot.agent.loop import _prompt_shape

    class _Tools:
        def get_definitions(self):
            return []

    shape = _prompt_shape([
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "write_file", "arguments": '{"content": "' + "x" * 5_000 + '"}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "written"},
    ], _Tools())

    assert shape["history_msgs"] == 2
    assert shape["history_chars"] > 5_000


def test_a_registry_that_cannot_describe_itself_does_not_fail_the_turn():
    from nanobot.agent.loop import _prompt_shape

    class _Broken:
        def get_definitions(self):
            raise RuntimeError("no")

    shape = _prompt_shape([{"role": "system", "content": "sys"}], _Broken())

    assert shape["tool_count"] == 0
    assert shape["system_chars"] == 3


# --- aggregation ------------------------------------------------------------

def test_overlapping_tools_are_unioned_not_summed():
    """Three 2s tools gathered together cost the turn 2 seconds. Summed, the
    breakdown would exceed the turn it is breaking down."""
    assert _merge_spans([(0, 2), (0.5, 2.5), (1, 2)]) == pytest.approx(2.5)
    assert _merge_spans([(0, 1), (2, 3)]) == pytest.approx(2.0)
    assert _merge_spans([]) == 0.0


@pytest.mark.asyncio
async def test_a_chat_shows_its_prompt_growing():
    """The measured problem in this house is not the model, it is a session
    that accumulates: the same question costs more in a chat that never
    forgets. prompt-per-call is where that shows up."""
    prof = Profiler()
    for prompt in (1000, 5000, 20000):
        with prof.span("turn", "websocket:ev-notif"):
            prof.record_call(
                model="m", served_by="m", api_base="https://gw.test/v1",
                duration_s=1.0, request_s=1.0, attempts=1, finish_reason="stop",
                usage={"prompt_tokens": prompt, "completion_tokens": 20},
                stream=False,
            )

    chat = prof.snapshot()["by_chat"]["websocket:ev-notif"]
    assert chat["spans"] == 3
    assert chat["tokens"]["prompt_tokens"] == 26000
    assert chat["prompt_per_call"] == pytest.approx(8666.7, rel=1e-3)


@pytest.mark.asyncio
async def test_the_snapshot_can_be_narrowed_to_one_session():
    prof = Profiler()
    for key in ("websocket:a", "websocket:b"):
        with prof.span("turn", key):
            prof.record_call(model="m", served_by="m", api_base=None, duration_s=0.1,
                             request_s=0.1, attempts=1, finish_reason="stop",
                             usage={"prompt_tokens": 10}, stream=False)

    snap = prof.snapshot(session_key="websocket:a")
    assert [s["session_key"] for s in snap["recent_spans"]] == ["websocket:a"]
    assert len(snap["recent_calls"]) == 1


def test_disabled_costs_nothing_and_breaks_nothing():
    """The `with` still has to yield a span nobody has to check for None, or
    every call site grows an `if`."""
    prof = Profiler()
    prof.enabled = False
    with prof.span("turn", "websocket:a") as span:
        span.note(stop_reason="completed")
        prof.record_call(model="m", served_by="m", api_base=None, duration_s=1.0,
                         request_s=1.0, attempts=1, finish_reason="stop",
                         usage={}, stream=False)
    snap = prof.snapshot()
    assert snap["counts"] == {"spans": 0, "running": 0, "calls": 0, "tools": 0}
    assert snap["enabled"] is False


def test_a_turn_in_flight_is_in_the_table_not_only_in_a_footnote():
    """A turn that has not returned is exactly the one somebody is asking
    about, and it is not in the ring buffer yet. A panel that shows nothing
    until it finishes looks broken at the one moment it is being read — which
    is how this was first reported."""
    prof = Profiler()
    with prof.span("task", "websocket:a", label="investigar el 502") as span:
        prof.record_call(model="m", served_by="m", api_base=None, duration_s=0.4,
                         request_s=0.4, attempts=1, finish_reason="stop",
                         usage={"prompt_tokens": 100}, stream=False)
        snap = prof.snapshot()
        assert snap["counts"] == {"spans": 0, "running": 1, "calls": 1, "tools": 0}
        row = snap["recent_spans"][0]
        assert row["running"] is True, "and marked, so it is not read as a fast turn"
        assert row["label"] == "investigar el 502"
        assert row["duration_ms"] >= 0
        assert row["calls"] == 1, "with what it has spent so far"
        assert snap["by_chat"]["websocket:a"]["spans"] == 1
        assert snap["by_kind"]["task"]["count"] == 1
        span.note(stop_reason="completed")
    done = prof.snapshot()
    assert done["live"] == []
    assert done["counts"]["spans"] == 1
    assert "running" not in done["recent_spans"][0]


def test_the_buffers_are_bounded():
    prof = Profiler()
    for _ in range(1200):
        prof.record_call(model="m", served_by="m", api_base=None, duration_s=0.1,
                         request_s=0.1, attempts=1, finish_reason="stop",
                         usage={}, stream=False)
    assert prof.snapshot()["counts"]["calls"] == 1000


def test_percentiles_do_not_need_numpy():
    prof = Profiler()
    for ms in (10, 20, 30, 40, 1000):
        with prof.span("turn", "s"):
            pass
        prof._spans[-1]["duration_ms"] = ms
    totals = prof.snapshot()["totals"]
    assert totals["span_ms"]["p50"] == 30
    assert totals["span_ms"]["max"] == 1000

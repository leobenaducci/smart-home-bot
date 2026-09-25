"""One outage, discovered once, for every container on the box.

The in-process window shares what a route learns with the other routes in the
same process. There are six processes here — five family instances and casa —
and each was rediscovering the same failure on somebody else's server.
Measured on 2026-08-24 while `gpt-5.6-luna` was 500ing at opencode: eleven
requests and 31.0s of waiting, per container, for the same fact.

The file under /shared-state is that fact written down. These tests pin what
it must do (a second container reads it and skips the ladder), what it must
survive (no mount, a read-only mount, a truncated file, a stale cache), and
what it must never do — hold a model down past its hour, or leak between two
gateways that happen to serve models with the same name.
"""
import asyncio
import json
import time

import pytest

from nanobot.providers import base as provider_base
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse
from nanobot.utils.outage_store import SHARED_OUTAGES, OutageStore

ZEN = "https://opencode.ai/zen/v1"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Point the process-wide store at a temp dir, and clear the in-process
    windows — otherwise a test could pass by remembering rather than reading."""
    was = SHARED_OUTAGES.path.parent
    SHARED_OUTAGES.use_dir(tmp_path / "shared-state")
    LLMProvider._OUTAGE_WINDOWS.clear()
    yield
    LLMProvider._OUTAGE_WINDOWS.clear()
    # What it was, not the literal `/shared-state`: restoring the deployed
    # default would re-point the process-wide singleton at the live mount for
    # every test collected after this file.
    SHARED_OUTAGES.use_dir(was)


class _Provider(LLMProvider):
    def __init__(self, responses, *, fallback_model=None, api_base=ZEN):
        super().__init__(api_base=api_base)
        self._responses = list(responses)
        self.requests = 0
        self.models: list[str | None] = []
        self.generation = GenerationSettings(fallback_model=fallback_model)

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.requests += 1
        self.models.append(kwargs.get("model"))
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    def get_default_model(self) -> str:
        return "gpt-5.6-luna"


def _err():
    return LLMResponse(content="{'type': 'error', 'message': 'Internal server error'}",
                       finish_reason="error", error_status_code=500)


def _ok():
    return LLMResponse(content="Buenas, don Alex.")


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    real = asyncio.sleep

    async def fake(delay, result=None):
        await real(0)
        return result

    # Patched on the module object rather than through the dotted string:
    # `nanobot.providers` has a lazy __getattr__ that raises for anything not
    # in its provider table, so resolving "nanobot.providers.base.…" by name
    # depends on the submodule attribute happening to be bound — which another
    # test in this directory can undo, and did.
    monkeypatch.setattr(provider_base.asyncio, "sleep", fake)


def _another_container() -> _Provider:
    """A provider with no memory of this process — which is what a second
    container is, from the file's point of view."""
    LLMProvider._OUTAGE_WINDOWS.clear()
    return _Provider([_ok()], fallback_model="deepseek-v4-flash")


# --- the point of the whole thing -------------------------------------------

@pytest.mark.asyncio
async def test_the_second_container_does_not_pay_the_ladder_again():
    first = _Provider([_err()] * 6 + [_ok()], fallback_model="deepseek-v4-flash")
    await first.chat_with_retry(messages=[{"role": "user", "content": "hola"}])
    assert first.requests > 1, "the first one discovers it the hard way"

    second = _another_container()
    await second.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert second.requests == 1, "and the second goes straight to the fallback"
    assert second.serving_model("gpt-5.6-luna") == ("deepseek-v4-flash", "gpt-5.6-luna")


def test_a_reader_adopts_the_window_and_stops_reading_the_file():
    """Read once, then answer from memory: this is on the path of every call,
    and a stat per call is fine while a parse per call is not."""
    SHARED_OUTAGES.mark(ZEN, "gpt-5.6-luna", time.time() + 3600)
    reader = _another_container()

    assert reader._model_is_down("gpt-5.6-luna") is True
    SHARED_OUTAGES.path.unlink()
    assert reader._model_is_down("gpt-5.6-luna") is True, "already adopted"


def test_the_window_the_reader_adopts_is_the_time_that_is_left():
    """Not a fresh hour. A container joining forty minutes in has twenty."""
    SHARED_OUTAGES.mark(ZEN, "gpt-5.6-luna", time.time() + 1200)
    reader = _another_container()

    assert reader._model_is_down("gpt-5.6-luna") is True
    left = reader._model_down_until["gpt-5.6-luna"] - time.monotonic()
    assert 1100 < left < 1250, left


def test_an_expired_shared_window_is_not_adopted():
    SHARED_OUTAGES.mark(ZEN, "gpt-5.6-luna", time.time() - 1)
    assert _another_container()._model_is_down("gpt-5.6-luna") is False


def test_another_gateway_is_told_nothing():
    SHARED_OUTAGES.mark(ZEN, "gpt-5.6-luna", time.time() + 3600)
    ollama = _Provider([_ok()], fallback_model="x", api_base="http://ollama.home:11434/v1")

    assert ollama._model_is_down("gpt-5.6-luna") is False


def test_a_provider_with_no_gateway_neither_reads_nor_writes():
    """"Gateway unknown" is not "the same gateway" — and it is what keeps every
    test double in this repo out of a file the deployment reads."""
    nowhere = _Provider([_ok()], fallback_model="deepseek-v4-flash", api_base=None)
    nowhere._mark_model_down("gpt-5.6-luna")

    assert not SHARED_OUTAGES.path.exists()
    SHARED_OUTAGES.mark(ZEN, "gpt-5.6-luna", time.time() + 3600)
    assert nowhere._shared_outage_remaining("gpt-5.6-luna") == 0.0


# --- the file itself --------------------------------------------------------

def test_a_write_is_visible_to_a_reader_that_had_already_looked(tmp_path):
    """The reader stats before it parses, so a writer has to invalidate that —
    a cache that never notices the write is the same as no file at all."""
    a = OutageStore(tmp_path)
    b = OutageStore(tmp_path)
    assert b.remaining(ZEN, "luna") == 0.0          # b has now cached "nothing"

    a.mark(ZEN, "luna", time.time() + 3600)

    assert b.remaining(ZEN, "luna") > 3000


def test_two_writers_do_not_lose_each_other(tmp_path):
    """Each re-reads before merging, so the second write keeps the first."""
    a, b = OutageStore(tmp_path), OutageStore(tmp_path)
    a.mark(ZEN, "luna", time.time() + 3600)
    b.mark(ZEN, "kimi-k3", time.time() + 3600)

    held = OutageStore(tmp_path).deadlines(ZEN)
    assert set(held) == {"luna", "kimi-k3"}


def test_expired_entries_are_dropped_on_the_way_past(tmp_path):
    store = OutageStore(tmp_path)
    store.mark(ZEN, "old", time.time() - 10)
    store.mark(ZEN, "new", time.time() + 3600)

    assert set(store.deadlines(ZEN)) == {"new"}, "no reaper, no growth"


def test_a_gateway_with_nothing_left_is_removed_entirely(tmp_path):
    store = OutageStore(tmp_path)
    store.mark(ZEN, "old", time.time() - 10)
    store.mark("http://other/v1", "new", time.time() + 3600)

    assert json.loads(store.path.read_text()) == {
        "http://other/v1": {"new": pytest.approx(time.time() + 3600, abs=5)}
    }


def test_a_truncated_file_is_survived(tmp_path):
    store = OutageStore(tmp_path)
    store.mark(ZEN, "luna", time.time() + 3600)
    store.path.write_text('{"https://opencode', encoding="utf-8")

    reader = OutageStore(tmp_path)
    assert reader.remaining(ZEN, "luna") == 0.0, "unreadable is not down"
    store.mark(ZEN, "luna", time.time() + 3600)     # and it repairs on the next write
    assert OutageStore(tmp_path).remaining(ZEN, "luna") > 3000


def test_nonsense_shapes_are_ignored_rather_than_crashing(tmp_path):
    store = OutageStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({
        ZEN: {"luna": "soon", "kimi": None, "flash": time.time() + 3600},
        "bad": ["not", "a", "dict"],
    }), encoding="utf-8")

    assert store.remaining(ZEN, "luna") == 0.0
    assert store.remaining(ZEN, "flash") > 3000
    assert store.deadlines("bad") == {}


def test_no_mount_at_all_is_simply_no_knowledge(tmp_path):
    store = OutageStore(tmp_path / "does" / "not" / "exist")
    assert store.remaining(ZEN, "luna") == 0.0


def test_an_unwritable_mount_costs_the_sharing_and_not_the_turn(tmp_path):
    """A read-only /shared-state must degrade to the behaviour this house had
    yesterday, not take the assistant down with it."""
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    store = OutageStore(ro)
    try:
        store.mark(ZEN, "luna", time.time() + 3600)     # must not raise
        assert store.remaining(ZEN, "luna") == 0.0
    finally:
        ro.chmod(0o700)


# --- the faster ladder ------------------------------------------------------

@pytest.mark.asyncio
async def test_the_fallback_is_tried_after_three_requests_not_ten():
    """Measured on the live box: ten meant eleven requests and 31s before the
    fallback was reached, on the one turn per hour that pays for discovery."""
    provider = _Provider([_err()] * 3 + [_ok()], fallback_model="deepseek-v4-flash")

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hola"}], retry_mode="persistent",
    )

    assert response.finish_reason == "stop"
    assert provider.requests == 4, "three failures, then the fallback"
    assert provider.models == [None, None, None, "deepseek-v4-flash"], provider.models


@pytest.mark.asyncio
async def test_a_blip_that_answers_on_a_retry_never_reaches_the_fallback():
    """The other side of the trade: three is still a ladder, not a hair
    trigger, and a model that comes back on attempt two is not moved off."""
    provider = _Provider([_err(), _ok()], fallback_model="deepseek-v4-flash")

    await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hola"}], retry_mode="persistent",
    )

    assert provider.requests == 2
    assert not SHARED_OUTAGES.path.exists(), "and nothing was published about it"

def test_a_deadline_beyond_one_window_is_clamped_and_rewritten(tmp_path):
    """A clock that was wrong when the file was written must not be forever.

    A container that starts before NTP steps it writes `time.time() + 3600`
    against a date months out. Once the clock corrects, every process on the
    box reads that as "remaining" and routes the model to the fallback for
    months -- `_prune` drops only deadlines already past, the local lazy expiry
    is cancelled by the next re-adoption from the file, and nothing anywhere
    clears it short of deleting the file by hand.
    """
    store = OutageStore(str(tmp_path))
    store.mark("gw", "luna", time.time() + 90 * 24 * 3600)

    left = store.remaining("gw", "luna")

    assert left <= store.max_window_s, left
    # And rewritten, so the next reader does not have to clamp it again.
    fresh = OutageStore(str(tmp_path))
    assert fresh.remaining("gw", "luna") <= fresh.max_window_s


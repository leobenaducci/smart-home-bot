"""One dead model must not take the house down.

On 2026-08-23 `gpt-5.6-luna` returned a hard 500 on every call at the OpenCode
Zen gateway while `kimi-k3` and `deepseek-v4-flash` answered normally on the
same key and the same base URL. The model was broken, not busy, so the retry
ladder was the wrong instrument entirely: it spent its delays and handed the
family back the same error dict, and because that model was also the house
instance's voice model and three modelProfiles, every assistant in the house
went down together.

The fallback fires only after the ladder has established that waiting does not
help, and only once — these tests pin both halves of that, because a fallback
that fires too eagerly silently moves the house onto the wrong model and a
fallback that never fires is a config key nobody can see is dead.
"""
import asyncio

import pytest

from nanobot.providers import base as provider_base
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse


class RecordingProvider(LLMProvider):
    """Records the model each call asked for, so the swap is observable."""

    def __init__(self, responses, fallback_model=None):
        super().__init__()
        self._responses = list(responses)
        self.models: list[str | None] = []
        self.generation = GenerationSettings(fallback_model=fallback_model)

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.models.append(kwargs.get("model"))
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]

    def get_default_model(self) -> str:
        return "gpt-5.6-luna"


def _outage(n: int) -> list[LLMResponse]:
    """n copies of the exact response the gateway returned for luna."""
    return [
        LLMResponse(
            content="{'type': 'error', 'message': 'Internal server error'}",
            finish_reason="error",
            error_status_code=500,
        )
        for _ in range(n)
    ]


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    _real_sleep = asyncio.sleep

    async def _fake_sleep(delay, result=None):
        # Still yield to the event loop: a fake that never awaits turns the
        # whole ladder into a straight line and nothing else gets scheduled.
        await _real_sleep(0)
        return result

    # Patched on the module object rather than through the dotted string:
    # `nanobot.providers` has a lazy __getattr__ that raises for anything not
    # in its provider table, so resolving "nanobot.providers.base.…" by name
    # depends on the submodule attribute happening to be bound, which is not
    # this test's to rely on.
    monkeypatch.setattr(provider_base.asyncio, "sleep", _fake_sleep)


@pytest.mark.asyncio
async def test_a_model_that_is_down_falls_back_and_the_turn_is_answered():
    provider = RecordingProvider(
        _outage(4) + [LLMResponse(content="Buenas, don Alex.")],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert response.finish_reason == "stop"
    assert response.content == "Buenas, don Alex."
    # The ladder runs first and only then the swap: waiting is tried before
    # quietly moving the house onto a different model.
    assert provider.models[-1] == "deepseek-v4-flash"
    assert provider.models[:-1] == [None] * 4, \
        "the fallback fired before the retries were spent"


@pytest.mark.asyncio
async def test_without_a_fallback_configured_nothing_changes():
    """The feature is off unless asked for — this is the pre-2026-08-23 path."""
    provider = RecordingProvider(_outage(4), fallback_model=None)

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert response.finish_reason == "error"
    assert provider.models == [None] * 4, "a model was swapped with no fallback configured"


@pytest.mark.asyncio
async def test_a_fallback_equal_to_the_failing_model_is_not_retried():
    """Otherwise the fallback is a fifth attempt at the thing that just failed.

    The comparison has to resolve `model=None` to the provider default, which
    is what an ordinary chat turn actually sends.
    """
    provider = RecordingProvider(_outage(4), fallback_model="gpt-5.6-luna")

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert response.finish_reason == "error"
    assert len(provider.models) == 4, "the fallback spent a call on the dead model"


@pytest.mark.asyncio
async def test_a_prefixed_fallback_naming_the_same_model_is_not_retried():
    """Six registry specs strip a `vendor/` prefix before the name reaches the
    wire, so `custom/gpt-5.6-luna` and `gpt-5.6-luna` are one model. Comparing
    the configured spellings would spend a request re-asking the dead one."""
    provider = RecordingProvider(_outage(4), fallback_model="custom/gpt-5.6-luna")

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert response.finish_reason == "error"
    assert len(provider.models) == 4, "the fallback spent a call on the dead model"


@pytest.mark.asyncio
async def test_when_the_fallback_fails_too_the_original_error_is_returned():
    """The family should see what actually broke, not a second error about a
    model they never asked for.

    The two errors have to be *distinguishable* or this test cannot fail:
    handing back the fallback's error looks like an improvement (it is the more
    recent one), and with identical bodies nothing would notice the change.
    """
    provider = RecordingProvider(
        _outage(4)
        + [
            LLMResponse(
                content="model deepseek-v4-flash not found",
                finish_reason="error",
                error_status_code=404,
            )
        ],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert response.finish_reason == "error"
    assert "Internal server error" in (response.content or ""), \
        "the fallback's own error was handed back instead of the one that started this"
    assert "deepseek-v4-flash not found" not in (response.content or "")
    assert provider.models[-1] == "deepseek-v4-flash", "the fallback was never tried"


@pytest.mark.asyncio
async def test_a_permanent_error_does_not_spend_a_call_on_the_fallback():
    """A 401 says the same thing to every model on the key. Falling back there
    would buy nothing and cost a request."""
    provider = RecordingProvider(
        [LLMResponse(content="401 unauthorized", finish_reason="error",
                     error_status_code=401)],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert response.finish_reason == "error"
    assert provider.models == [None]


@pytest.mark.asyncio
async def test_persistent_retry_mode_also_falls_back():
    """`providerRetryMode` is `persistent` in this house, and that is the mode
    that made the outage hurt: it kept asking a broken model politely instead of
    ever giving up. It must reach the fallback too, or the deployed config is
    the one configuration the fix does not cover.
    """
    provider = RecordingProvider(
        _outage(LLMProvider._PERSISTENT_FALLBACK_ATTEMPTS)
        + [LLMResponse(content="Listo.")],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hola"}], retry_mode="persistent"
    )

    assert response.content == "Listo."
    assert provider.models[-1] == "deepseek-v4-flash"
    # Counted from the constant rather than written out, because the number is
    # a measurement and has moved once already: it was ten, which cost eleven
    # requests and 31s on the one turn per hour that discovers an outage.
    assert len(provider.models) == LLMProvider._PERSISTENT_FALLBACK_ATTEMPTS + 1


@pytest.mark.asyncio
async def test_an_explicitly_requested_model_falls_back_too():
    """Profesión turns name a model (`modelProfiles`); they are not the default,
    and they were just as dead."""
    provider = RecordingProvider(
        _outage(4) + [LLMResponse(content="Hecho.")],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hola"}], model="gpt-5.6-luna"
    )

    assert response.content == "Hecho."
    assert provider.models[:-1] == ["gpt-5.6-luna"] * 4
    assert provider.models[-1] == "deepseek-v4-flash"


def test_the_outage_window_is_at_least_an_hour():
    """The window is the whole point: one turn pays the ladder, the rest of the
    outage does not. An hour is the floor, not a tuning knob to shave."""
    assert LLMProvider._MODEL_OUTAGE_COOLDOWN_S >= 3600


@pytest.mark.asyncio
async def test_the_fallback_sticks_so_the_next_turn_skips_the_dead_model():
    """Without this the outage is re-discovered on every single call.

    A vendor-side outage lasts hours. Re-proving the model dead each time costs
    the whole ladder again — in the deployed `persistent` mode that is ten more
    requests and ~31s of waiting, per LLM call, and an agent turn makes one call
    per tool round trip.
    """
    provider = RecordingProvider(
        _outage(4) + [LLMResponse(content="Buenas, don Alex.")],
        fallback_model="deepseek-v4-flash",
    )

    first = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])
    assert first.content == "Buenas, don Alex."
    assert provider.models == [None] * 4 + ["deepseek-v4-flash"]

    provider.models.clear()
    second = await provider.chat_with_retry(messages=[{"role": "user", "content": "y esto?"}])

    assert second.content == "Buenas, don Alex."
    assert provider.models == ["deepseek-v4-flash"], \
        "the second turn re-ran the ladder against a model already known to be dead"
    assert second.served_by_model == "deepseek-v4-flash", \
        "a turn served by the fallback must say so however it got routed there"


@pytest.mark.asyncio
async def test_the_outage_window_expires_and_the_main_model_gets_another_chance(monkeypatch):
    """The house must come back on its own when the vendor recovers, with no
    restart and no reaper task — expiry is a comparison at the next call."""
    monkeypatch.setattr(LLMProvider, "_MODEL_OUTAGE_COOLDOWN_S", 0.0)
    provider = RecordingProvider(
        _outage(4) + [LLMResponse(content="Listo.")],
        fallback_model="deepseek-v4-flash",
    )

    await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])
    provider.models.clear()
    await provider.chat_with_retry(messages=[{"role": "user", "content": "de nuevo"}])

    assert provider.models == [None], "the expired window kept routing to the fallback"


@pytest.mark.asyncio
async def test_a_fallback_that_failed_too_does_not_pin_the_house_to_it():
    """A gateway-wide outage teaches nothing about *which* model is broken, so
    it must not route the next hour onto a fallback that is also down."""
    provider = RecordingProvider(_outage(5), fallback_model="deepseek-v4-flash")

    await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert provider._model_down_until == {}


@pytest.mark.asyncio
async def test_a_persistent_outage_that_stamps_its_errors_still_reaches_the_fallback():
    """`persistent` used to leave the ladder only through the identical-error
    counter, and that counter resets on any byte of difference. A gateway that
    puts a request id in its 500s — most of them do — meant the loop never
    ended and the fallback never fired, on the one mode this house deploys.
    """
    stamped = [
        LLMResponse(
            content=f"Error: {{'message': 'Internal server error', 'request_id': 'req_{i}'}}",
            finish_reason="error",
            error_status_code=500,
        )
        for i in range(LLMProvider._PERSISTENT_FALLBACK_ATTEMPTS)
    ]
    provider = RecordingProvider(
        stamped + [LLMResponse(content="Listo.")], fallback_model="deepseek-v4-flash"
    )

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hola"}], retry_mode="persistent"
    )

    assert response.content == "Listo."
    assert provider.models[-1] == "deepseek-v4-flash"
    assert len(provider.models) == LLMProvider._PERSISTENT_FALLBACK_ATTEMPTS + 1


@pytest.mark.asyncio
async def test_a_retired_model_falls_back_without_spending_the_ladder():
    """Waiting cannot un-retire a model, but another model on the same key
    answers fine — the most literal form of "one dead model"."""
    provider = RecordingProvider(
        [
            LLMResponse(
                content="model gpt-5.6-luna has been decommissioned",
                finish_reason="error",
                error_status_code=404,
            ),
            LLMResponse(content="Hecho."),
        ],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert response.content == "Hecho."
    assert provider.models == [None, "deepseek-v4-flash"]


@pytest.mark.asyncio
async def test_the_family_is_not_told_we_gave_up_when_the_fallback_answered():
    """`on_retry_wait` is not a log line — AgentLoop publishes it to the
    channel. Announcing "giving up" and then handing over a working answer is a
    notice the next second contradicts."""
    notices: list[str] = []

    async def _on_retry_wait(message: str) -> None:
        notices.append(message)

    provider = RecordingProvider(
        _outage(4) + [LLMResponse(content="Listo.")],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hola"}], on_retry_wait=_on_retry_wait
    )

    assert response.content == "Listo."
    assert not [n for n in notices if "giving up" in n], notices


@pytest.mark.asyncio
async def test_an_empty_reply_from_the_fallback_is_not_counted_as_a_rescue():
    """The fallback spends reasoning tokens where the main model spends none,
    so under a ceiling picked for a non-reasoning model it can burn the budget
    thinking and return `length` with nothing in it. That reaches the family as
    Alfred not answering — and it must not open the hour-long window either."""
    provider = RecordingProvider(
        _outage(4) + [LLMResponse(content="", finish_reason="length")],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert response.finish_reason == "error", "an empty reply was passed off as an answer"
    assert provider._model_down_until == {}


@pytest.mark.asyncio
async def test_a_fallback_turn_says_which_model_actually_answered():
    """Otherwise the swap exists only in the log, and every consumer downstream
    — usage accounting included — believes the main model answered."""
    provider = RecordingProvider(
        _outage(4) + [LLMResponse(content="Listo.")],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert response.served_by_model == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_an_ordinary_answer_is_not_labelled_as_a_fallback():
    provider = RecordingProvider(
        [LLMResponse(content="Buenas.")], fallback_model="deepseek-v4-flash"
    )

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert response.served_by_model is None


class StreamingProvider(LLMProvider):
    """Paints deltas the way a real streaming provider does, then answers.

    Overrides ``chat_stream`` rather than ``chat``: the base implementation
    falls back to a non-streaming call and paints the whole body as one delta,
    error bodies included, which no real streaming provider does.
    """

    def __init__(self, script, fallback_model=None):
        super().__init__()
        self._script = list(script)
        self.generation = GenerationSettings(fallback_model=fallback_model)

    async def chat_stream(self, *args, **kwargs) -> LLMResponse:
        chunks, response = (
            self._script.pop(0) if len(self._script) > 1 else self._script[0]
        )
        delta = kwargs.get("on_content_delta")
        for chunk in chunks:
            if delta is not None:
                await delta(chunk)
        return response

    async def chat(self, *args, **kwargs) -> LLMResponse:  # pragma: no cover - unused
        raise AssertionError("these tests drive the streaming path")

    def get_default_model(self) -> str:
        return "gpt-5.6-luna"


@pytest.mark.asyncio
async def test_a_half_streamed_answer_is_not_spliced_onto_the_fallbacks():
    """The dying model painted into the reader's message before it failed.

    Re-driving the same callback would leave two answers stitched together on
    screen. What the reader ends up with is corrected by `trim_to` at stream
    end, but not before they have watched the splice appear mid-turn.
    """
    painted: list[str] = []

    async def _paint(delta: str) -> None:
        painted.append(delta)

    provider = StreamingProvider(
        [
            (["Claro, ", "el problema es"], _outage(1)[0]),
            (["Buenas, ", "don Alex."], LLMResponse(content="Buenas, don Alex.")),
        ],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_stream_with_retry(
        messages=[{"role": "user", "content": "hola"}],
        on_content_delta=_paint,
    )

    assert response.content == "Buenas, don Alex."
    assert "".join(painted) == "Claro, el problema es", (
        "the fallback re-painted over a message the dying model had already "
        f"written into: {''.join(painted)!r}"
    )


@pytest.mark.asyncio
async def test_a_turn_that_never_painted_still_streams_normally():
    """The silencing must be a consequence of having painted, not of retrying —
    otherwise every recovered turn loses streaming for no reason."""
    painted: list[str] = []

    async def _paint(delta: str) -> None:
        painted.append(delta)

    provider = StreamingProvider(
        [
            ([], _outage(1)[0]),  # died before emitting anything
            (["Listo", "."], LLMResponse(content="Listo.")),
        ],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_stream_with_retry(
        messages=[{"role": "user", "content": "hola"}],
        on_content_delta=_paint,
    )

    assert response.content == "Listo."
    assert "".join(painted) == "Listo.", "a turn that painted nothing was silenced anyway"


@pytest.mark.asyncio
async def test_a_busy_fallback_is_retried_rather_than_discarded():
    """A 429 on the fallback is the expected consequence of this working.

    Every instance and every route swings onto the same fallback on the same
    key at once. Giving up there hands back the main model's 500 because the
    rescue was busy for a second.
    """
    provider = RecordingProvider(
        _outage(4)
        + [
            LLMResponse(content="429 rate limit", finish_reason="error",
                        error_status_code=429),
            LLMResponse(content="Buenas, don Alex."),
        ],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert response.content == "Buenas, don Alex."
    assert provider.models[-2:] == ["deepseek-v4-flash"] * 2, \
        "the fallback was not given a second chance after a transient error"
    assert response.served_by_model == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_a_permanently_broken_fallback_is_not_hammered():
    """Bounded on purpose: this is already the last resort, and the caller is
    better served by the original error than by never being answered."""
    provider = RecordingProvider(
        _outage(4)
        + [LLMResponse(content="401 unauthorized", finish_reason="error",
                       error_status_code=401)],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert response.finish_reason == "error"
    assert "Internal server error" in (response.content or ""), \
        "the caller got the fallback's error instead of the one that explains the outage"
    assert provider.models.count("deepseek-v4-flash") == 1, \
        "a permanent error on the fallback was retried"


@pytest.mark.asyncio
async def test_the_fallback_ladder_is_bounded():
    """A fallback that is transiently broken for the whole window must still
    terminate — `persistent` here would hang the turn forever."""
    provider = RecordingProvider(
        _outage(4)
        + [LLMResponse(content="429 rate limit", finish_reason="error",
                       error_status_code=429)],
        fallback_model="deepseek-v4-flash",
    )

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hola"}])

    assert response.finish_reason == "error"
    tries = provider.models.count("deepseek-v4-flash")
    assert tries == 1 + len(LLMProvider._CHAT_RETRY_DELAYS), \
        f"the fallback ladder is not the bounded one it claims to be: {tries} tries"

@pytest.mark.asyncio
async def test_the_fallback_ladder_runs_once_per_call_and_not_twice(_no_sleeping):
    """`fallback_tried` has to guard both arms that reach the fallback.

    It guarded only the attempt-count one. That was invisible while
    `_PERSISTENT_FALLBACK_ATTEMPTS` and `_PERSISTENT_IDENTICAL_ERROR_LIMIT`
    were both 10, because the two conditions came true on the same attempt and
    the rescue ran once -- which is what `_try_fallback_model`'s own docstring
    promises. Lowering the first to 3 pulled them apart: the attempt arm fires
    at 3, and `ladder_spent` fires again at 10 and runs the fallback's whole
    ladder a second time.

    So the case where nothing is rescued became *more* expensive than before,
    and during a vendor outage every route in every container is already on
    that one fallback -- which is the moment this doubles the load on it.
    """
    provider = RecordingProvider(_outage(40), fallback_model="deepseek-v4-flash")

    await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hola"}],
        model="gpt-5.6-luna",
        retry_mode="persistent",
    )

    asked_fallback = [m for m in provider.models if m == "deepseek-v4-flash"]
    # One entry into the rescue. It may spend its own attempts inside that one
    # entry; what must not happen is a second entry after the first gave up.
    runs = 0
    previous = None
    for model in provider.models:
        if model == "deepseek-v4-flash" and previous != "deepseek-v4-flash":
            runs += 1
        previous = model
    assert runs == 1, (
        f"the fallback was entered {runs} times "
        f"({len(asked_fallback)} requests); sequence: {provider.models}"
    )


# --- a chain, because one rescue shares its fate ----------------------------
# 2026-08-30: the everyday model answered 500 on every attempt and the single
# configured fallback answered "Model is disabled" in the same minute. One
# provider-side change took out the rescue along with the thing it rescued, and
# every Alfred in the house said "Internal server error" to whoever was typing.

def _dead(n: int) -> list[LLMResponse]:
    """A model the gateway knows and will not route -- the 401 shape."""
    return [
        LLMResponse(content="{'message': 'Model is disabled'}",
                    finish_reason="error", error_status_code=401)
        for _ in range(n)
    ]


class ScriptedProvider(LLMProvider):
    """Answers per model name, so a chain can be walked and observed."""

    def __init__(self, per_model, fallback_model=None):
        super().__init__()
        self._per_model = per_model
        self.models: list[str | None] = []
        self.generation = GenerationSettings(fallback_model=fallback_model)

    async def chat(self, *args, **kwargs) -> LLMResponse:
        model = kwargs.get("model") or self.get_default_model()
        self.models.append(model)
        answer = self._per_model.get(model)
        if callable(answer):
            return answer()
        return answer

    def get_default_model(self) -> str:
        return "gpt-5.6-luna"


@pytest.mark.asyncio
async def test_a_second_candidate_answers_when_the_first_is_also_dead():
    """The outage that prompted this. luna is 500, the configured fallback is
    401, and the turn is still answered -- by a model from another family."""
    provider = ScriptedProvider(
        {
            "gpt-5.6-luna": _outage(1)[0],
            "deepseek-v4-flash": _dead(1)[0],
            "qwen3.8-flash": LLMResponse(content="listo", finish_reason="stop"),
        },
        fallback_model=["deepseek-v4-flash", "qwen3.8-flash"],
    )
    result = await provider.chat_with_retry(messages=[])
    assert result.finish_reason == "stop"
    assert result.content == "listo"
    assert result.served_by_model == "qwen3.8-flash"
    # And it tried them in the order given, rather than picking one.
    tried = [m for m in provider.models if m != "gpt-5.6-luna"]
    assert tried[0] == "deepseek-v4-flash"
    assert "qwen3.8-flash" in tried


@pytest.mark.asyncio
async def test_the_chain_stops_at_the_first_model_that_answers():
    """A rescue is not a survey. Once one answers, the rest are not asked --
    every extra call is a paid request and a second or two of somebody waiting.
    """
    provider = ScriptedProvider(
        {
            "gpt-5.6-luna": _outage(1)[0],
            "deepseek-v4-flash": LLMResponse(content="listo", finish_reason="stop"),
            "qwen3.8-flash": LLMResponse(content="no debería llegar aquí",
                                         finish_reason="stop"),
        },
        fallback_model=["deepseek-v4-flash", "qwen3.8-flash"],
    )
    result = await provider.chat_with_retry(messages=[])
    assert result.served_by_model == "deepseek-v4-flash"
    assert "qwen3.8-flash" not in provider.models


@pytest.mark.asyncio
async def test_every_candidate_dead_returns_the_original_error():
    """Not the last candidate's error. The caller asked for luna, and "Model is
    disabled" about some model they never named explains less than the 500 they
    would otherwise have got."""
    provider = ScriptedProvider(
        {
            "gpt-5.6-luna": _outage(1)[0],
            "deepseek-v4-flash": _dead(1)[0],
            "qwen3.8-flash": _dead(1)[0],
        },
        fallback_model=["deepseek-v4-flash", "qwen3.8-flash"],
    )
    result = await provider.chat_with_retry(messages=[])
    assert result.finish_reason == "error"
    assert "Internal server error" in (result.content or "")


@pytest.mark.asyncio
async def test_a_candidate_equal_to_the_failing_model_is_skipped():
    """Anywhere in the chain, not just first: a rescue that is the thing being
    rescued is the retry ladder again, wearing a hat."""
    provider = ScriptedProvider(
        {
            "gpt-5.6-luna": _outage(1)[0],
            "qwen3.8-flash": LLMResponse(content="listo", finish_reason="stop"),
        },
        fallback_model=["gpt-5.6-luna", "qwen3.8-flash"],
    )
    result = await provider.chat_with_retry(messages=[])
    assert result.served_by_model == "qwen3.8-flash"
    # luna appears while the retry ladder is running -- that is the ladder's
    # job, and counting those would be measuring the wrong thing. What must be
    # true is that it is never asked again *once the rescue has started*.
    first_rescue = provider.models.index("qwen3.8-flash")
    assert "gpt-5.6-luna" not in provider.models[first_rescue:]


@pytest.mark.asyncio
async def test_a_repeated_candidate_is_only_tried_once():
    provider = ScriptedProvider(
        {
            "gpt-5.6-luna": _outage(1)[0],
            "deepseek-v4-flash": _dead(1)[0],
        },
        fallback_model=["deepseek-v4-flash", "deepseek-v4-flash"],
    )
    await provider.chat_with_retry(messages=[])
    assert provider.models.count("deepseek-v4-flash") == 1


@pytest.mark.asyncio
async def test_a_plain_string_still_works():
    """Every household that has one today wrote a name, and none of them should
    have to learn a list to keep what they had."""
    provider = ScriptedProvider(
        {
            "gpt-5.6-luna": _outage(1)[0],
            "deepseek-v4-flash": LLMResponse(content="listo", finish_reason="stop"),
        },
        fallback_model="deepseek-v4-flash",
    )
    result = await provider.chat_with_retry(messages=[])
    assert result.served_by_model == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_a_turn_routed_onto_a_dead_first_candidate_still_reaches_the_chain():
    """The turn that *discovers* the outage walked the chain; the ones after it
    did not, and those are the whole hour.

    Once luna is inside its window every call is sent straight at the first
    candidate. That candidate answering `Model is disabled` is a permanent
    error, so the loop returned it -- the family got "Internal server error"
    from the rescue instead of from the model, with a healthy third candidate
    sitting unused. Which is the exact outage this chain was added for, one
    turn later than it was tested at.
    """
    provider = ScriptedProvider(
        {
            "gpt-5.6-luna": _outage(1)[0],
            "deepseek-v4-flash": _dead(1)[0],
            "qwen3.8-flash": LLMResponse(content="listo", finish_reason="stop"),
        },
        fallback_model=["deepseek-v4-flash", "qwen3.8-flash"],
    )
    await provider.chat_with_retry(messages=[])   # the turn that learns it

    provider.models.clear()
    result = await provider.chat_with_retry(messages=[])
    assert result.finish_reason == "stop"
    assert result.content == "listo"
    assert provider.models == ["deepseek-v4-flash", "qwen3.8-flash"]


@pytest.mark.asyncio
async def test_the_routing_walks_past_a_candidate_that_is_also_down():
    """Routing around a dead model is about not paying for it again, so the
    routing has to walk the chain as well as the rescue does.

    Two turns is what it takes to learn both: the first marks luna down, the
    second is sent at deepseek, is rescued by qwen and marks deepseek too. From
    there `serving_model` still read only the *first* candidate, so every
    remaining call in the hour-long window was dialled at deepseek again -- one
    wasted request per turn, and the whole retry ladder of one when the first
    candidate fails transiently rather than with a 401.
    """
    provider = ScriptedProvider(
        {
            "gpt-5.6-luna": _outage(1)[0],
            "deepseek-v4-flash": _dead(1)[0],
            "qwen3.8-flash": LLMResponse(content="listo", finish_reason="stop"),
        },
        fallback_model=["deepseek-v4-flash", "qwen3.8-flash"],
    )
    await provider.chat_with_retry(messages=[])   # luna is down
    await provider.chat_with_retry(messages=[])   # and now deepseek is too

    # The model this provider would reach right now, asked rather than guessed.
    assert provider.serving_model() == ("qwen3.8-flash", "gpt-5.6-luna")

    provider.models.clear()
    result = await provider.chat_with_retry(messages=[])
    assert result.content == "listo"
    assert provider.models == ["qwen3.8-flash"], (
        f"{provider.models}: the dead candidate was dialled again")


# --- every builder can route a cross-provider fallback -----------------------
#
# There are four places that construct a provider: the facade, and the CLI's
# main, subagent and vision builders. The first version of cross-provider
# fallback wired one of them. That looked correct -- calling the factory by
# hand built the right provider -- while the process actually serving the house
# used a different builder, found no factory, and logged
#
#     Fallback provider together_ai is not configured in this process
#
# during a real OpenCode Go outage, answering the room with an error instead of
# a rescue. Nothing failed; the feature was simply absent on the path that
# mattered. This is what says so.

def test_every_provider_builder_can_reach_another_provider():
    import inspect

    from nanobot.cli import commands
    from nanobot import nanobot as facade

    builders = [
        ("facade", [facade._make_provider]),
        ("cli main", [commands._make_provider]),
        # Two thin builders delegate to one that does the work; the wiring may
        # live in either, so both count.
        ("cli subagent", [commands._make_subagent_provider,
                          commands._resolve_alt_provider]),
        ("cli powerful", [commands._make_main_powerful_provider,
                          commands._resolve_alt_provider]),
        ("cli vision", [commands._make_vision_provider]),
    ]
    for name, fns in builders:
        src = "\n".join(inspect.getsource(fn) for fn in fns)
        assert "sibling_for" in src, (
            f"{name} builds a provider that cannot reach another one; a "
            f"cross-provider fallback would be skipped on every turn it serves"
        )


def test_the_factory_returns_none_for_an_undeclared_provider():
    """Walked past, never raised. A misconfigured rescue must not be the thing
    that turns an outage into an exception."""
    from nanobot.providers.sibling import sibling_factory

    class _Cfg:
        providers = None

    assert sibling_factory(_Cfg())("together_ai", "m") is None


def test_the_profiler_wrapper_does_not_disable_the_cross_provider_rescue():
    """`_run_with_retry` wraps the call to time it, and the rescue finds the
    sibling's equivalent method by name.

    Unnamed, that wrapper made the lookup ask every provider for a method
    called `counted`. None has one, so the rescue reported the provider as *not
    configured* and skipped -- while the provider was configured, declared, and
    reachable. It cost this household its fallback through a real OpenCode Go
    outage, and every by-hand test passed throughout, because those call the
    method directly and never go through the wrapper. The profiler is on in the
    deployed containers, so this was the normal path, not an edge case.
    """
    calls: list[str] = []

    class _Sib:
        async def _safe_chat(self, **kw):
            calls.append(kw["model"])
            return LLMResponse(content="rescued")

    provider = RecordingProvider(
        _outage(40), fallback_model=[{"model": "m2", "provider": "other"}])
    provider.sibling_for = lambda name, model: _Sib()

    was = provider_base.PROFILER.enabled
    provider_base.PROFILER.enabled = True
    try:
        asyncio.run(provider._run_with_retry(
            provider._safe_chat, {"model": "gpt-5.6-luna"}, [],
            retry_mode="fast", on_retry_wait=None))
    finally:
        provider_base.PROFILER.enabled = was

    assert calls == ["m2"], (
        "the rescue never reached the other provider; the timing wrapper hid "
        "the method name it is looked up by"
    )

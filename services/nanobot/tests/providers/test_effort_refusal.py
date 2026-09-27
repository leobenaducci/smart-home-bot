"""A model that refuses a thinking level is asked again at one it accepts.

On 2026-09-26 the planner moved to `xiaomi/mimo-v2.6-flash` on NanoGPT. A
planner turn carries the everyday level, "low", and NanoGPT answered every one

    Invalid value for reasoning_effort on model "xiaomi/mimo-v2.6-flash": "low".
    Supported values are: none, high.

so every multi-step request in the house failed on a setting nobody chose for
that model. The refusal names what it accepts; the provider learns it once per
model and sends the nearest level from then on -- the higher one on a tie, so a
role that asked for some thinking does not quietly get none.
"""

import pytest

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import find_by_name

MIMO = "xiaomi/mimo-v2.6-flash"
REFUSAL = ('Error code: 400 - {"error": {"message": "Invalid value for reasoning_effort on '
           'model \\"xiaomi/mimo-v2.6-flash\\": \\"low\\". Supported values are: none, high.", '
           '"type": "invalid_request_error", "param": "reasoning_effort"}}')


class _Answer:
    def __init__(self):
        self.choices = [type("C", (), {
            "message": type("M", (), {"content": "listo", "tool_calls": None})(),
            "finish_reason": "stop",
        })()]
        self.usage = None
        self.model = MIMO


class _Stream:
    """An empty stream: enough for chat_stream to finish without content."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _NanoGPT:
    """Refuses any level but none/high, as NanoGPT does for this model."""

    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("reasoning_effort") not in (None, "none", "high"):
            raise ValueError(REFUSAL)
        return _Stream() if kwargs.get("stream") else _Answer()


@pytest.fixture(autouse=True)
def _forget_learned_models():
    before = dict(OpenAICompatProvider._EFFORTS_ACCEPTED)
    OpenAICompatProvider._EFFORTS_ACCEPTED.clear()
    yield
    OpenAICompatProvider._EFFORTS_ACCEPTED.clear()
    OpenAICompatProvider._EFFORTS_ACCEPTED.update(before)


def _provider():
    p = OpenAICompatProvider(api_key="k", api_base="https://nano-gpt.com/api/v1",
                             default_model=MIMO, spec=find_by_name("nanogpt"))
    api = _NanoGPT()
    p._client = type("Client", (), {"chat": type("Chat", (), {"completions": api})()})()
    return p, api


def test_the_refusal_is_read_and_nothing_else_is():
    assert OpenAICompatProvider._accepted_efforts(ValueError(REFUSAL)) == ("none", "high")
    assert OpenAICompatProvider._accepted_efforts(ValueError("context length exceeded")) is None
    # A refusal that names no values teaches nothing, so it keeps failing fast.
    assert OpenAICompatProvider._accepted_efforts(
        ValueError("reasoning_effort is not supported")) is None


def test_the_nearest_level_wins_and_a_tie_goes_up():
    near = OpenAICompatProvider._nearest_effort
    assert near("low", ("none", "high")) == "high"          # equidistant: keep thinking
    assert near("low", ("none", "low", "high")) == "low"    # accepted as asked
    assert near("medium", ("none", "high", "max")) == "high"
    assert near("none", ("low", "high")) == "low"           # off is not on offer: least on


@pytest.mark.asyncio
async def test_a_planner_turn_survives_and_the_model_is_remembered():
    p, api = _provider()
    answer = await p.chat(messages=[{"role": "user", "content": "hola"}], model=MIMO,
                          reasoning_effort="low")
    assert answer.content == "listo" and answer.finish_reason != "error"
    assert [c.get("reasoning_effort") for c in api.calls] == ["low", "high"]
    # The next turn goes straight to the accepted level: one request, not two.
    await p.chat(messages=[{"role": "user", "content": "otra"}], model=MIMO, reasoning_effort="low")
    assert [c.get("reasoning_effort") for c in api.calls][2:] == ["high"]


@pytest.mark.asyncio
async def test_the_streaming_path_too():
    p, api = _provider()
    answer = await p.chat_stream(messages=[{"role": "user", "content": "hola"}], model=MIMO,
                                 reasoning_effort="low")
    assert answer.finish_reason != "error", answer.content
    assert [c.get("reasoning_effort") for c in api.calls] == ["low", "high"]
    assert api.calls[-1].get("stream") is True

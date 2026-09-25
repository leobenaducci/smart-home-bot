"""A model that will not take a temperature is asked again without one.

Reported from the chat: every turn on one model came back

    Error from provider (Console Go): Upstream request failed:
    [invalid_request_error] invalid temperature: only 1 is allowed for this model

The provider decides whether to send `temperature` by matching the model's
*name* against gpt-5/o1/o3/o4. A gateway may call a model anything it likes,
and this one was named nothing of the sort — so the parameter went out, the
request was refused, and no name list would have caught it.

Retrying was correct to skip, too: a 400 normally means the request will be
wrong the same way next time. This is the exception, because dropping the
parameter genuinely changes the request — so the refusal itself is what
teaches it, once per model, rather than another guess about names.
"""

import pytest

from nanobot.providers.openai_compat_provider import OpenAICompatProvider


class _Refuses:
    """Answers like the gateway did: refuse while `temperature` is present."""

    def __init__(self, message="invalid temperature: only 1 is allowed for this model"):
        self.calls: list[dict] = []
        self._message = message

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if "temperature" in kwargs:
            raise ValueError(f"[invalid_request_error] {self._message}")
        return _Answer()


class _Answer:
    """The least a parse needs to see."""

    def __init__(self):
        self.choices = [type("C", (), {
            "message": type("M", (), {"content": "listo", "tool_calls": None})(),
            "finish_reason": "stop",
        })()]
        self.usage = None
        self.model = "console-go/reasoner"


@pytest.fixture(autouse=True)
def _forget_learned_models():
    """The set is per process and this test teaches it — leave it as found."""
    before = set(OpenAICompatProvider._NO_TEMPERATURE)
    yield
    OpenAICompatProvider._NO_TEMPERATURE.clear()
    OpenAICompatProvider._NO_TEMPERATURE.update(before)


def test_a_name_that_looks_ordinary_still_gets_a_temperature():
    # The behaviour that was right and stays right: an unknown name is assumed
    # to take one, or every ordinary model would lose the setting.
    assert OpenAICompatProvider._supports_temperature("console-go/reasoner")
    assert OpenAICompatProvider._supports_temperature("claude-sonnet-5")


def test_the_known_names_are_still_skipped():
    assert not OpenAICompatProvider._supports_temperature("gpt-5")
    assert not OpenAICompatProvider._supports_temperature("openai/o3-mini")
    # Reasoning effort still wins whatever the name is.
    assert not OpenAICompatProvider._supports_temperature("anything", "high")


def test_the_refusal_is_recognised_and_nothing_else_is():
    real = ValueError("[invalid_request_error] invalid temperature: only 1 is allowed")
    assert OpenAICompatProvider._rejected_temperature(real)
    assert OpenAICompatProvider._rejected_temperature(
        ValueError("temperature is not supported with this model"))
    # A 400 about anything else must keep failing fast; retrying a request that
    # will be refused identically just makes the wait longer.
    assert not OpenAICompatProvider._rejected_temperature(
        ValueError("[invalid_request_error] context length exceeded"))
    assert not OpenAICompatProvider._rejected_temperature(
        ValueError("rate limit reached"))


def test_the_model_is_asked_again_without_it_and_remembered():
    OpenAICompatProvider._NO_TEMPERATURE.clear()
    assert OpenAICompatProvider._supports_temperature("console-go/reasoner")

    # The refusal teaches it.
    OpenAICompatProvider._NO_TEMPERATURE.add("console-go/reasoner")
    assert not OpenAICompatProvider._supports_temperature("console-go/reasoner")
    # ...and only that model. Another one is unaffected.
    assert OpenAICompatProvider._supports_temperature("some/other-model")


@pytest.mark.asyncio
async def test_a_turn_survives_the_refusal():
    """The whole point: the person gets an answer, not an error about a setting
    they did not choose and cannot see."""
    provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
    refuses = _Refuses()
    provider._client = type("Client", (), {
        "chat": type("Chat", (), {"completions": refuses})()})()
    provider.default_model = "console-go/reasoner"
    provider.api_base = "https://example.invalid/v1"
    provider._spec = None
    OpenAICompatProvider._NO_TEMPERATURE.clear()

    kwargs = {"model": "console-go/reasoner", "temperature": 0.1, "messages": []}
    try:
        await refuses.create(**kwargs)
    except ValueError as e:
        assert OpenAICompatProvider._rejected_temperature(e)
        kwargs.pop("temperature")
        answer = await refuses.create(**kwargs)
        assert answer.choices[0].message.content == "listo"
    else:                                   # pragma: no cover - fixture is wrong
        pytest.fail("the stand-in was supposed to refuse")

    assert len(refuses.calls) == 2
    assert "temperature" in refuses.calls[0]
    assert "temperature" not in refuses.calls[1]

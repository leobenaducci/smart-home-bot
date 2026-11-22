"""household D1: provider specs, compat retries, shared outage windows, OpenCode session."""

from __future__ import annotations

import json

import pytest

from nanobot.config.schema import Config, ModelPresetConfig
from nanobot.household import compat_retries as cr
from nanobot.household import outages
from nanobot.household.outage_store import SHARED_OUTAGES
from nanobot.household.provider_specs import household_dynamic_spec
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.factory import make_provider
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import create_dynamic_spec


# --- specs -----------------------------------------------------------------

def test_household_specs_keep_the_fork_defaults():
    cfg = Config.model_validate({"providers": {"nanogpt": {"apiKey": "k"}}})
    spec = household_dynamic_spec("nanogpt", cfg.providers.model_extra["nanogpt"])
    assert spec.default_api_base == "https://nano-gpt.com/api/v1" and spec.is_gateway
    local = household_dynamic_spec("ollama_gpu0", Config.model_validate(
        {"providers": {"ollama_gpu0": {"apiBase": "http://h:11437/v1"}}}).providers.model_extra["ollama_gpu0"])
    assert local.is_local
    assert household_dynamic_spec("ollama_cloud", cfg.providers.model_extra["nanogpt"]).is_local is False
    assert household_dynamic_spec("company_proxy", cfg.providers.model_extra["nanogpt"]) is None


def test_a_nanogpt_block_without_api_base_reaches_nanogpt():
    cfg = Config.model_validate({
        "providers": {"nanogpt": {"apiKey": "k"}},
        "agents": {"defaults": {"model": "deepseek/deepseek-v4", "provider": "nanogpt"}},
    })
    provider = make_provider(cfg)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider._effective_base == "https://nano-gpt.com/api/v1"


def test_every_opencode_request_carries_a_session_header():
    p = OpenAICompatProvider(api_key="k", api_base="https://opencode.ai/zen/v1", default_model="m",
                             spec=create_dynamic_spec("zen_custom"))
    assert "x-opencode-session" in p._default_headers  # calls with no conversation too


# --- compat retries --------------------------------------------------------

class _Fake(OpenAICompatProvider):
    def __init__(self, errors):
        super().__init__(api_key="k", api_base="http://gw/v1", default_model="m1")
        self.errors = list(errors)
        self.calls = []

    async def _chat_unsettled(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7,
                              reasoning_effort=None, tool_choice=None, provider_context=None):
        self.calls.append(("chat", reasoning_effort, self._supports_temperature(model or "m1")))
        if self.errors:
            return LLMResponse(content=self.errors.pop(0), finish_reason="error", error_status_code=400)
        return LLMResponse(content="ok", finish_reason="stop")

    async def _chat_stream_unsettled(self, messages, tools=None, model=None, max_tokens=4096,
                                     temperature=0.7, reasoning_effort=None, tool_choice=None,
                                     on_content_delta=None, on_thinking_delta=None,
                                     on_tool_call_delta=None, provider_context=None):
        self.calls.append(("stream", reasoning_effort, None))
        return LLMResponse(content="streamed", finish_reason="stop")


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    cr.NO_TEMPERATURE.clear()
    cr.EFFORTS_ACCEPTED.clear()
    outages._LOCAL.clear()
    outages._FAILURES.clear()
    SHARED_OUTAGES.use_dir(tmp_path)
    yield
    cr.NO_TEMPERATURE.clear()
    cr.EFFORTS_ACCEPTED.clear()
    outages._LOCAL.clear()
    outages._FAILURES.clear()


@pytest.mark.asyncio
async def test_effort_refusal_sends_the_nearest_accepted_level_and_remembers():
    p = _Fake(["Invalid reasoning_effort 'low'. Supported values are: none, high"])
    r = await p.chat([{"role": "user", "content": "hi"}], model="m1", reasoning_effort="low")
    assert r.content == "ok" and [c[1] for c in p.calls] == ["low", "high"]
    await p.chat([{"role": "user", "content": "hi"}], model="m1", reasoning_effort="low")
    assert p.calls[-1][1] == "high"  # not asked with the refused level again


@pytest.mark.asyncio
async def test_temperature_refusal_retries_without_it():
    p = _Fake(["temperature: only 1 is allowed for this model"])
    r = await p.chat([{"role": "user", "content": "hi"}], model="m1")
    assert r.content == "ok" and [c[2] for c in p.calls] == [True, False]


@pytest.mark.asyncio
async def test_streaming_only_model_is_asked_on_the_streaming_path():
    p = _Fake(['This model only supports streaming. Set "stream": true.'])
    r = await p.chat([{"role": "user", "content": "hi"}], model="m1")
    assert r.content == "streamed" and [c[0] for c in p.calls] == ["chat", "stream"]


@pytest.mark.asyncio
async def test_an_ordinary_400_is_not_retried():
    p = _Fake(["messages: field required"])
    r = await p.chat([{"role": "user", "content": "hi"}], model="m1")
    assert r.finish_reason == "error" and len(p.calls) == 1


# --- shared outage windows ------------------------------------------------

class _Down(LLMProvider):
    def __init__(self, name, base, *, fail):
        super().__init__(provider_name=name)
        self.api_base, self.fail, self.calls = base, fail, 0

    def get_default_model(self):
        return "main-model"

    async def chat(self, **kwargs):
        self.calls += 1
        if self.fail:
            return LLMResponse(content="upstream 503", finish_reason="error", error_status_code=503)
        return LLMResponse(content="rescued", finish_reason="stop")

    async def chat_stream(self, **kwargs):
        return await self.chat(**kwargs)


@pytest.mark.asyncio
async def test_a_dead_model_opens_a_shared_window_and_is_skipped(tmp_path):
    primary = _Down("opencode", "https://opencode.ai/zen/v1", fail=True)
    backup = _Down("together_ai", "https://api.together.xyz/v1", fail=False)
    wrapper = outages.HouseholdFallbackProvider(primary, [ModelPresetConfig(model="qwen")], lambda _: backup)
    for _ in range(outages.FAILURE_THRESHOLD):
        assert (await wrapper.chat(messages=[])).content == "rescued"
    assert outages.remaining("https://opencode.ai/zen/v1", "main-model") > 3000
    data = json.loads((tmp_path / "model-outages.json").read_text())
    assert "main-model" in data["https://opencode.ai/zen/v1"]
    # another process (a fresh wrapper, local memory cleared) skips it without a call
    outages._LOCAL.clear()
    primary2 = _Down("opencode", "https://opencode.ai/zen/v1", fail=True)
    wrapper2 = outages.HouseholdFallbackProvider(primary2, [ModelPresetConfig(model="qwen")], lambda _: backup)
    assert (await wrapper2.chat(messages=[])).content == "rescued" and primary2.calls == 0


@pytest.mark.asyncio
async def test_when_every_candidate_is_down_they_are_tried_anyway():
    primary = _Down("opencode", "https://opencode.ai/zen/v1", fail=False)
    backup = _Down("together_ai", "https://api.together.xyz/v1", fail=True)
    outages.mark_down("https://opencode.ai/zen/v1", "main-model")
    outages.mark_down("https://api.together.xyz/v1", "qwen")
    wrapper = outages.HouseholdFallbackProvider(primary, [ModelPresetConfig(model="qwen")], lambda _: backup)
    assert (await wrapper.chat(messages=[])).content == "rescued" and primary.calls == 1


@pytest.mark.asyncio
async def test_an_auth_failure_opens_no_window():
    class _Auth(_Down):
        async def chat(self, **kwargs):
            return LLMResponse(content="bad key", finish_reason="error", error_status_code=401,
                               error_kind="authentication")

    primary = _Auth("opencode", "https://opencode.ai/zen/v1", fail=True)
    wrapper = outages.HouseholdFallbackProvider(primary, [ModelPresetConfig(model="qwen")],
                                                lambda _: _Down("x", "http://x", fail=False))
    for _ in range(5):
        await wrapper.chat(messages=[])
    assert outages.remaining("https://opencode.ai/zen/v1", "main-model") == 0


# --- B5: one outage file for a mixed fleet ---------------------------------

def test_the_trial_reads_a_window_the_fork_wrote(tmp_path):
    # Exactly the fork's write: SHARED_OUTAGES.mark(api_base.rstrip("/"),
    # _model_identity(model), deadline) -- base as configured, model bare and lower-case.
    import time as _time

    (tmp_path / "model-outages.json").write_text(json.dumps(
        {"https://nano-gpt.com/api/v1": {"deepseek-v4-flash": _time.time() + 600}}))
    SHARED_OUTAGES.use_dir(tmp_path)
    p = OpenAICompatProvider(api_key="k", api_base="https://nano-gpt.com/api/v1/",
                             default_model="deepseek/DeepSeek-V4-Flash")
    assert outages.remaining(outages.gateway_of(p), "deepseek/DeepSeek-V4-Flash") > 500


def test_the_trial_writes_the_forks_keys(tmp_path):
    SHARED_OUTAGES.use_dir(tmp_path)
    p = OpenAICompatProvider(api_key="k", api_base="https://Gw.Example/v1/", default_model="m")
    outages.mark_down(outages.gateway_of(p), "moonshot/Kimi-K3")
    doc = json.loads((tmp_path / "model-outages.json").read_text())
    assert "kimi-k3" in json.dumps(doc) and "https://Gw.Example/v1" in json.dumps(doc)


def test_a_provider_without_a_base_url_shares_nothing(tmp_path):
    SHARED_OUTAGES.use_dir(tmp_path)

    class _NoBase(LLMProvider):
        def get_default_model(self):
            return "m"

        async def chat(self, **kwargs):
            raise AssertionError

        async def chat_stream(self, **kwargs):
            raise AssertionError

    gw = outages.gateway_of(_NoBase(provider_name="ollama_local"))
    outages.mark_down(gw, "m")
    assert outages.remaining(gw, "m") > 0 and not (tmp_path / "model-outages.json").exists()


@pytest.mark.asyncio
async def test_one_rescue_opens_the_window_for_the_whole_house(tmp_path):
    # C6 found the trial needed three slow turns (twelve requests to the dead
    # model) before the window opened; the fork opened it on the first rescue.
    SHARED_OUTAGES.use_dir(tmp_path)

    class _Gw(LLMProvider):
        def __init__(self):
            super().__init__(provider_name="custom")
            self.api_base, self.asked = "http://gw.test/v1", []

        def get_default_model(self):
            return "cheap"

        async def chat(self, **kwargs):
            self.asked.append(kwargs.get("model"))
            if kwargs.get("model") == "cheap":
                return LLMResponse(content="upstream 503", finish_reason="error", error_status_code=503)
            return LLMResponse(content="flash answered", finish_reason="stop")

        async def chat_stream(self, **kwargs):
            return await self.chat(**kwargs)

    gw = _Gw()
    wrapper = outages.HouseholdFallbackProvider(gw, [ModelPresetConfig(model="flash")], lambda p: gw)
    assert (await wrapper.chat(messages=[], model="cheap")).content == "flash answered"
    assert outages.remaining("http://gw.test/v1", "cheap") > 3000
    gw.asked.clear()
    await wrapper.chat(messages=[], model="cheap")
    assert gw.asked == ["flash"]                                # skipped from the next call on


@pytest.mark.asyncio
@pytest.mark.parametrize("status,text", [
    (400, "Input validation error"),
    (404, "The model `gemma-old` does not exist"),
    (400, "prompt is too long: 27852 tokens > 16384 maximum"),
])
async def test_a_refusal_about_the_model_takes_the_fallback_without_a_window(status, text):
    """Fork _MODEL_SPECIFIC_ERROR_*: another model answers the same request;
    upstream handed the person the raw error. Not an outage, so no window."""
    class _Refuses(_Down):
        async def chat(self, **kwargs):
            self.calls += 1
            return LLMResponse(content=text, finish_reason="error", error_status_code=status,
                               error_should_retry=False)

    primary = _Refuses("nanogpt", "https://nano-gpt.com/api/v1", fail=True)
    wrapper = outages.HouseholdFallbackProvider(primary, [ModelPresetConfig(model="qwen")],
                                                lambda _: _Down("x", "http://x", fail=False))
    for _ in range(outages.FAILURE_THRESHOLD + 1):
        assert (await wrapper.chat(messages=[])).content == "rescued"
    assert outages.remaining("https://nano-gpt.com/api/v1", "main-model") == 0


@pytest.mark.asyncio
async def test_an_ordinary_bad_request_is_still_the_callers():
    class _Bad(_Down):
        async def chat(self, **kwargs):
            return LLMResponse(content="messages[3].role must be one of ...", finish_reason="error",
                               error_status_code=400, error_should_retry=False)

    wrapper = outages.HouseholdFallbackProvider(_Bad("nanogpt", "https://nano-gpt.com/api/v1", fail=True),
                                                [ModelPresetConfig(model="qwen")],
                                                lambda _: _Down("x", "http://x", fail=False))
    assert (await wrapper.chat(messages=[])).finish_reason == "error"


@pytest.mark.asyncio
async def test_when_nothing_rescues_no_window_opens():
    """Fork rule: if every candidate errors, the gateway is down, which says
    nothing about which model is broken; marking one down would also reach the
    fork's members through the shared file."""
    primary = _Down("ollama_text", "http://host.docker.internal:11437/v1", fail=True)
    backup = _Down("ollama_cloud", "https://ollama.com/v1", fail=True)
    wrapper = outages.HouseholdFallbackProvider(primary, [ModelPresetConfig(model="qwen", provider="ollama_cloud")],
                                                lambda _: backup)
    for _ in range(5):
        assert (await wrapper.chat(messages=[])).finish_reason == "error"
    assert outages.remaining("http://host.docker.internal:11437/v1", "main-model") == 0
    assert outages.remaining("https://ollama.com/v1", "qwen") == 0

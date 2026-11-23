"""household A5: which role runtimes fall back during an outage (fork ``_outage_fallback_for``).

A role on the default provider reuses the loop's fallback wrapper, which takes
the role's model as its primary: it falls back like the everyday model. A role
on another provider falls back to the main provider's chain, and vision never
takes one. The wrapper's own
60 s circuit is off, so one role's failures never skip another role's model.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.config.schema import Config, ModelPresetConfig
from nanobot.household import outages, routing
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.factory import make_provider
from nanobot.providers.fallback_provider import FallbackProvider


@pytest.fixture(autouse=True)
def _windows(tmp_path):
    from nanobot.household.outage_store import SHARED_OUTAGES

    outages._LOCAL.clear()
    outages._FAILURES.clear()
    SHARED_OUTAGES.use_dir(tmp_path)
    yield
    outages._LOCAL.clear()
    outages._FAILURES.clear()


class _Gateway(LLMProvider):
    """One gateway; the models in *dead* answer 503."""

    def __init__(self, base, dead=()):
        super().__init__(provider_name="custom")
        self.api_base, self.dead, self.asked = base, set(dead), []

    def get_default_model(self):
        return "cheap"

    async def chat(self, **kwargs):
        model = kwargs.get("model") or "cheap"
        self.asked.append(model)
        if model in self.dead:
            return LLMResponse(content="upstream 503", finish_reason="error", error_status_code=503)
        return LLMResponse(content=f"{model} answered", finish_reason="stop")

    async def chat_stream(self, **kwargs):
        return await self.chat(**kwargs)


def _config(**defaults):
    d = {"model": "cheap", "provider": "custom", "modelFallback": "flash",
         "modelPowerful": "strong", **defaults}
    return Config.model_validate({
        "providers": {"custom": {"apiKey": "k", "apiBase": "http://gw.test/v1"},
                      "together_ai": {"apiKey": "k"}},
        "agents": {"defaults": d},
    })


def _wrapped_loop(primary, backup):
    wrapper = outages.HouseholdFallbackProvider(primary, [ModelPresetConfig(model="flash")],
                                                lambda _preset: backup)
    return SimpleNamespace(provider=wrapper, context_window_tokens=128_000)


@pytest.mark.asyncio
async def test_a_role_on_the_default_provider_falls_back_like_the_everyday_model():
    primary = _Gateway("http://gw.test/v1", dead={"strong"})
    backup = _Gateway("http://gw.test/v1")
    loop = _wrapped_loop(primary, backup)
    runtime = routing.resolve_runtime(loop, powerful=True, config=_config())
    assert runtime.provider is loop.provider and runtime.model == "strong"
    out = await runtime.provider.chat(messages=[], model=runtime.model)
    assert out.content == "flash answered" and primary.asked == ["strong"]


@pytest.mark.asyncio
async def test_one_roles_failures_never_skip_another_roles_model():
    primary = _Gateway("http://gw.test/v1", dead={"strong"})
    loop = _wrapped_loop(primary, _Gateway("http://gw.test/v1"))
    for _ in range(4):                  # past upstream's 3-failure circuit
        await loop.provider.chat(messages=[], model="strong")
    out = await loop.provider.chat(messages=[], model="cheap")
    assert out.content == "cheap answered" and primary.asked[-1] == "cheap"


def test_a_role_on_another_provider_falls_back_to_the_main_providers_models():
    cfg = _config(providerPowerful="together_ai")
    routing._PROVIDERS.clear()
    loop = SimpleNamespace(provider=make_provider(cfg), context_window_tokens=128_000)
    runtime = routing.resolve_runtime(loop, powerful=True, config=cfg)
    assert runtime.model == "strong" and runtime.provider is not loop.provider
    assert isinstance(runtime.provider, FallbackProvider)
    assert [p.model for p in runtime.provider._fallback_presets] == ["flash"]
    assert runtime.provider._fallback_presets[0].provider == "custom"   # the main provider


def test_vision_on_another_provider_takes_no_chain():
    cfg = _config(visionModel="qwen3-vl:8b", visionProvider="together_ai")
    routing._PROVIDERS.clear()
    loop = SimpleNamespace(provider=make_provider(cfg), context_window_tokens=128_000)
    runtime = routing.resolve_runtime(loop, vision=True, config=cfg)
    assert runtime.model == "qwen3-vl:8b" and not isinstance(runtime.provider, FallbackProvider)


def test_vision_never_falls_back_to_a_text_model():
    cfg = _config(visionModel="qwen3-vl:8b")
    loop = SimpleNamespace(provider=make_provider(cfg), context_window_tokens=128_000)
    assert isinstance(loop.provider, FallbackProvider)
    runtime = routing.resolve_runtime(loop, vision=True, config=cfg)
    assert runtime.model == "qwen3-vl:8b" and not isinstance(runtime.provider, FallbackProvider)

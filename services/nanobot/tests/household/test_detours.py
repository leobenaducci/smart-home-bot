"""household A11: a server lent to a benchmark sends its calls where the detour says."""

from __future__ import annotations

import json
import time

import pytest

from nanobot.config.schema import ModelPresetConfig
from nanobot.household import outages
from nanobot.household.outage_store import SHARED_DETOURS, SHARED_OUTAGES
from nanobot.providers.base import LLMProvider, LLMResponse

LOCAL = "http://gpu.test:11437/v1"


class _Gateway(LLMProvider):
    def __init__(self, base, name):
        super().__init__(provider_name=name)
        self.api_base, self.asked = base, []

    def get_default_model(self):
        return "qwen3.5:9b"

    async def chat(self, **kwargs):
        self.asked.append(kwargs.get("model"))
        return LLMResponse(content=f"{kwargs.get('model')} answered", finish_reason="stop")

    async def chat_stream(self, **kwargs):
        return await self.chat(**kwargs)


@pytest.fixture(autouse=True)
def _dirs(tmp_path):
    outages._LOCAL.clear()
    outages._FAILURES.clear()
    SHARED_OUTAGES.use_dir(tmp_path)
    SHARED_DETOURS.use_dir(tmp_path)
    yield
    SHARED_DETOURS.use_dir(tmp_path / "none")


def _lend(tmp_path, until, model="deepseek-v4-flash", provider="nanogpt"):
    (tmp_path / "model-detours.json").write_text(json.dumps({
        "gateways": {LOCAL + "/": {"until": until, "model": model, "provider": provider}},
        "reason": "benchmark of qwen3.5:2b"}))


def _wrapper(primary, cloud, built):
    def factory(preset: ModelPresetConfig):
        built.append((preset.provider, preset.model))
        return cloud
    return outages.HouseholdFallbackProvider(primary, [ModelPresetConfig(model="qwen3.5:4b")], factory)


@pytest.mark.asyncio
async def test_a_lent_server_sends_its_calls_to_the_detour_target(tmp_path):
    primary, cloud, built = _Gateway(LOCAL, "ollama_gpu0"), _Gateway("https://cloud.test/v1", "nanogpt"), []
    _lend(tmp_path, time.time() + 600)
    out = await _wrapper(primary, cloud, built).chat(messages=[], model="qwen3.5:9b")
    assert out.content == "deepseek-v4-flash answered"
    assert primary.asked == [] and built == [("nanogpt", "deepseek-v4-flash")]
    assert SHARED_OUTAGES.remaining(LOCAL, "qwen3.5:9b") == 0      # lent, not down


@pytest.mark.asyncio
async def test_an_expired_detour_is_ignored(tmp_path):
    primary, cloud, built = _Gateway(LOCAL, "ollama_gpu0"), _Gateway("https://cloud.test/v1", "nanogpt"), []
    _lend(tmp_path, time.time() - 1)
    out = await _wrapper(primary, cloud, built).chat(messages=[], model="qwen3.5:9b")
    assert out.content == "qwen3.5:9b answered" and built == []


@pytest.mark.asyncio
async def test_a_detour_on_the_same_provider_swaps_only_the_model(tmp_path):
    primary, cloud, built = _Gateway(LOCAL, "ollama_gpu0"), _Gateway("https://cloud.test/v1", "nanogpt"), []
    _lend(tmp_path, time.time() + 600, model="qwen3.5:2b", provider=None)
    out = await _wrapper(primary, cloud, built).chat(messages=[], model="qwen3.5:9b")
    assert out.content == "qwen3.5:2b answered" and primary.asked == ["qwen3.5:2b"] and built == []

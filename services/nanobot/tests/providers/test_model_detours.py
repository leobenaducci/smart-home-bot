"""A server lent to a benchmark: its calls go to the named model until the deadline.

The admin page writes /shared-state/model-detours.json while a benchmark
borrows a card and the house's text servers on it are unloaded. What must
hold: the detoured server's calls go to the target, with its provider; other
servers are untouched; and the detour ends at its deadline on its own, so a
benchmark that dies holding it does not leave the house on the cloud.
"""
import json
import time

import pytest

from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse
from nanobot.utils.outage_store import SHARED_DETOURS

TEXT = "http://host.docker.internal:11437/v1"


@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    was = SHARED_DETOURS.path.parent
    SHARED_DETOURS.use_dir(tmp_path)
    yield tmp_path
    SHARED_DETOURS.use_dir(was)


class _Provider(LLMProvider):
    def __init__(self, api_base):
        super().__init__(api_base=api_base)
        self.generation = GenerationSettings()

    async def chat(self, *args, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "qwen3.5:4b"


def _write(tmp_path, until, gateway=TEXT):
    (tmp_path / "model-detours.json").write_text(json.dumps({"gateways": {
        gateway: {"until": until, "model": "deepseek-v4-flash", "provider": "custom"}}}))


def test_a_detoured_server_goes_to_the_target(_isolated):
    _write(_isolated, time.time() + 600)
    p = _Provider(TEXT)
    assert p._serving_route("qwen3.5:4b") == ("deepseek-v4-flash", "custom", "qwen3.5:4b")
    assert p.serving_model("qwen3.5:4b") == ("deepseek-v4-flash", "qwen3.5:4b")


def test_other_servers_are_untouched(_isolated):
    _write(_isolated, time.time() + 600)
    assert _Provider("http://host.docker.internal:11438/v1").serving_model("qwen3-vl:4b") \
        == ("qwen3-vl:4b", None)


def test_the_detour_ends_at_its_deadline(_isolated):
    _write(_isolated, time.time() - 1)
    assert _Provider(TEXT).serving_model("qwen3.5:4b") == ("qwen3.5:4b", None)


def test_no_file_or_a_broken_one_is_no_detour(_isolated):
    assert _Provider(TEXT).serving_model("qwen3.5:4b") == ("qwen3.5:4b", None)
    (_isolated / "model-detours.json").write_text("{not json")
    assert _Provider(TEXT).serving_model("qwen3.5:4b") == ("qwen3.5:4b", None)


def test_a_direct_caller_follows_the_detour_too(_isolated):
    # The classifier calls chat() itself; routed() is how it follows.
    _write(_isolated, time.time() + 600)
    p = _Provider(TEXT)
    cloud = _Provider("https://opencode.ai/zen/v1")
    p.sibling_for = lambda name, model=None: cloud
    prov, model = p.routed("qwen3.5:4b")
    assert prov is cloud and model == "deepseek-v4-flash"
    _write(_isolated, time.time() - 1)
    prov, model = p.routed("qwen3.5:4b")
    assert prov is p and model == "qwen3.5:4b"

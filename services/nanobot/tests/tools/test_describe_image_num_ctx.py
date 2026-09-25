"""describe_image must ask Ollama for a window big enough to answer in.

An image is ~3188 prompt tokens. Against Ollama's default 4096 that leaves ~900
to answer in, and qwen3-vl spends most of them thinking. Measured against the
live model, a real call returned the eleven characters "En el patio" with
done_reason ``length`` — which Alfred relayed as the description of the patio.
The window can only be raised on the native endpoint; ``/v1`` drops ``options``.
"""
import base64
from unittest.mock import patch

import pytest

from nanobot.agent.tools.vision import (
    _NUM_CTX,
    DescribeImageTool,
    _ollama_native_chat_url,
)
from nanobot.providers.base import LLMResponse

JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 64


class FakeProvider:
    def __init__(self, api_base=None, response=None):
        self.api_base = api_base
        self.response = response or LLMResponse(content="via provider")
        self.calls = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeAsyncClient:
    """Stands in for httpx.AsyncClient, recording the request body."""

    def __init__(self, payload=None, *, raises=None, **kwargs):
        self.payload = payload
        self.raises = raises
        self.posted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.posted.append((url, json))
        if self.raises:
            raise self.raises
        return FakeResponse(self.payload)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _tool(tmp_path, provider):
    return DescribeImageTool(
        provider=provider, model="qwen3-vl:8b",
        workspace=tmp_path, allowed_dir=tmp_path,
    )


def _write_image(tmp_path, name="media/cam_patio_1785285395.jpg"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(JPEG_BYTES)
    return name


# --- which providers get the native treatment ----------------------------


@pytest.mark.parametrize("api_base,expected", [
    ("http://ollama.home:11434/v1", "http://ollama.home:11434/api/chat"),
    ("http://ollama.home:11434/v1/", "http://ollama.home:11434/api/chat"),
    ("http://localhost:11434/v1", "http://localhost:11434/api/chat"),
    # already native, or a bare root
    ("http://ollama.home:11434", "http://ollama.home:11434/api/chat"),
])
def test_derives_the_native_url(api_base, expected):
    assert _ollama_native_chat_url(api_base) == expected


@pytest.mark.parametrize("api_base", [
    None,
    "",
    "https://api.openai.com/v1",
    "https://opencode.ai/zen/v1",
    # a local vLLM is OpenAI-compatible but is NOT Ollama; options would 400
    "http://localhost:8000/v1",
])
def test_leaves_every_other_provider_alone(api_base):
    """Only Ollama understands `options`; nothing else may be sent them."""
    assert _ollama_native_chat_url(api_base) is None


# --- the actual point ----------------------------------------------------


@pytest.mark.asyncio
async def test_asks_ollama_for_a_bigger_window(tmp_path):
    provider = FakeProvider(api_base="http://ollama.home:11434/v1")
    rel = _write_image(tmp_path)
    client = FakeAsyncClient({"message": {"content": "No hay nadie en el patio."},
                              "done_reason": "stop"})

    with patch("nanobot.agent.tools.vision.httpx.AsyncClient", return_value=client):
        result = await _tool(tmp_path, provider).execute(
            path=rel, question="¿qué hay en el patio?")

    assert result == "No hay nadie en el patio."
    (url, body), = client.posted
    assert url == "http://ollama.home:11434/api/chat"
    assert body["options"]["num_ctx"] == _NUM_CTX
    assert _NUM_CTX > 4096, "a window no larger than the default fixes nothing"
    # native schema: images ride alongside plain text, not as OpenAI content parts
    user = body["messages"][-1]
    assert user["content"] == "¿qué hay en el patio?"
    assert user["images"] == [base64.b64encode(JPEG_BYTES).decode()]
    assert not provider.calls, "should not also call the provider endpoint"


@pytest.mark.asyncio
async def test_reasoning_never_reaches_the_main_model(tmp_path):
    """The native endpoint splits thinking into its own field; only content is
    the description. Leaking scratch work would have Alfred read it aloud."""
    provider = FakeProvider(api_base="http://ollama.home:11434/v1")
    rel = _write_image(tmp_path)
    client = FakeAsyncClient({
        "message": {"content": "Se ve un auto oscuro.",
                    "thinking": "Let me look at the image. The user asks..."},
        "done_reason": "stop"})

    with patch("nanobot.agent.tools.vision.httpx.AsyncClient", return_value=client):
        result = await _tool(tmp_path, provider).execute(path=rel)

    assert result == "Se ve un auto oscuro."


@pytest.mark.asyncio
async def test_a_broken_native_call_still_produces_a_description(tmp_path):
    """Ollama moved, or the response shape changed. Losing the description is a
    worse outcome than answering in the smaller window."""
    provider = FakeProvider(api_base="http://ollama.home:11434/v1",
                            response=LLMResponse(content="via provider"))
    rel = _write_image(tmp_path)
    client = FakeAsyncClient(None, raises=RuntimeError("connection refused"))

    with patch("nanobot.agent.tools.vision.httpx.AsyncClient", return_value=client):
        result = await _tool(tmp_path, provider).execute(path=rel)

    assert result == "via provider"
    assert provider.calls, "should have fallen back to the provider endpoint"


@pytest.mark.asyncio
async def test_an_empty_answer_does_not_retry_in_the_smaller_window(tmp_path):
    """The model answered — it just had nothing to say. Retrying on /v1 would
    re-run the exact conditions that produce empty answers, for another 15s."""
    provider = FakeProvider(api_base="http://ollama.home:11434/v1")
    rel = _write_image(tmp_path)
    client = FakeAsyncClient({"message": {"content": "", "thinking": "hmm " * 500},
                              "done_reason": "length"})

    with patch("nanobot.agent.tools.vision.httpx.AsyncClient", return_value=client):
        result = await _tool(tmp_path, provider).execute(path=rel)

    assert result.startswith("Error:")
    assert not provider.calls


@pytest.mark.asyncio
async def test_non_ollama_provider_uses_the_ordinary_path(tmp_path):
    provider = FakeProvider(api_base="https://api.openai.com/v1",
                            response=LLMResponse(content="via provider"))
    rel = _write_image(tmp_path)

    result = await _tool(tmp_path, provider).execute(path=rel)

    assert result == "via provider"
    assert provider.calls


# --- the window is the server's when the deployer says what it serves ----------

@pytest.mark.asyncio
async def test_asks_for_the_servers_own_window_when_told(tmp_path, monkeypatch):
    """Against the house's text instance at 65536, asking for 8192 would reload
    the model on every image. Naming the window it already holds never does."""
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "65536")
    provider = FakeProvider(api_base="http://ollama.home:11434/v1")
    rel = _write_image(tmp_path)
    client = FakeAsyncClient({"message": {"content": "ok"}, "done_reason": "stop"})
    with patch("nanobot.agent.tools.vision.httpx.AsyncClient", return_value=client):
        await _tool(tmp_path, provider).execute(path=rel, question="¿qué hay?")
    (_, body), = client.posted
    assert body["options"]["num_ctx"] == 65536


def test_a_smaller_or_missing_server_window_keeps_the_floor(monkeypatch):
    from nanobot.agent.tools.vision import _native_num_ctx
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    assert _native_num_ctx() == _NUM_CTX
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "4096")
    assert _native_num_ctx() == _NUM_CTX        # never ask for less than what answers fit in
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "banana")
    assert _native_num_ctx() == _NUM_CTX

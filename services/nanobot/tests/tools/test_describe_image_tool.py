import base64

import pytest

from nanobot.agent.tools.vision import DescribeImageTool
from nanobot.providers.base import LLMResponse

# Smallest thing detect_image_mime() accepts as a JPEG.
JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 64


class FakeProvider:
    """Records the request instead of calling out."""

    def __init__(self, response: LLMResponse | None = None) -> None:
        self.response = response or LLMResponse(content="Se ve el patio vacío.")
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _tool(tmp_path, provider, *, restrict: bool = True) -> DescribeImageTool:
    return DescribeImageTool(
        provider=provider,
        model="vision-model",
        workspace=tmp_path,
        allowed_dir=tmp_path if restrict else None,
    )


def _write_image(tmp_path, name: str = "media/cam_patio_1.jpg") -> str:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(JPEG_BYTES)
    return name


@pytest.mark.asyncio
async def test_sends_image_and_question_to_vision_model(tmp_path) -> None:
    provider = FakeProvider()
    rel = _write_image(tmp_path)

    result = await _tool(tmp_path, provider).execute(
        path=rel, question="¿hay alguien en el patio?"
    )

    assert result == "Se ve el patio vacío."
    (call,) = provider.calls
    assert call["model"] == "vision-model"
    user = call["messages"][-1]
    text, image = user["content"]
    assert text["text"] == "¿hay alguien en el patio?"
    expected = base64.b64encode(JPEG_BYTES).decode()
    assert image["image_url"]["url"] == f"data:image/jpeg;base64,{expected}"


@pytest.mark.asyncio
async def test_relative_path_resolves_against_workspace(tmp_path) -> None:
    """The camera skill returns 'media/<file>', not an absolute path."""
    provider = FakeProvider()
    rel = _write_image(tmp_path)

    assert await _tool(tmp_path, provider).execute(path=rel) == "Se ve el patio vacío."


@pytest.mark.asyncio
async def test_refuses_paths_outside_the_workspace(tmp_path) -> None:
    """A path is model-supplied and the bytes leave the machine, so the sandbox
    boundary has to hold here as much as in read_file."""
    provider = FakeProvider()
    outside = tmp_path.parent / "secret.jpg"
    outside.write_bytes(JPEG_BYTES)

    result = await _tool(tmp_path, provider).execute(path=str(outside))

    assert result.startswith("Error:")
    assert provider.calls == []


@pytest.mark.asyncio
async def test_missing_file_errors_without_calling_the_model(tmp_path) -> None:
    provider = FakeProvider()

    result = await _tool(tmp_path, provider).execute(path="media/nope.jpg")

    assert result == "Error: no such image: media/nope.jpg"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_non_image_is_rejected(tmp_path) -> None:
    provider = FakeProvider()
    (tmp_path / "notes.txt").write_bytes(b"just text")

    result = await _tool(tmp_path, provider).execute(path="notes.txt")

    assert result == "Error: notes.txt is not an image"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_provider_error_surfaces_instead_of_an_empty_description(tmp_path) -> None:
    """An error must read as an error: a blank return would let the caller
    describe the frame from imagination, which is the bug this tool exists for."""
    provider = FakeProvider(LLMResponse(content="down", finish_reason="error"))
    rel = _write_image(tmp_path)

    result = await _tool(tmp_path, provider).execute(path=rel)

    assert result.startswith("Error: vision model error")


@pytest.mark.asyncio
async def test_reasoning_block_is_stripped(tmp_path) -> None:
    """qwen3-vl thinks out loud and Ollama ignores the think:false override, so
    the block arrives in content and would otherwise land in the tool result."""
    provider = FakeProvider(
        LLMResponse(content="<think>Dark. Maybe a roof?</think>Se ve un techo.")
    )
    rel = _write_image(tmp_path)

    assert await _tool(tmp_path, provider).execute(path=rel) == "Se ve un techo."


@pytest.mark.asyncio
async def test_asks_for_enough_tokens_to_survive_the_reasoning(tmp_path) -> None:
    """Measured runs spend 200-520 tokens thinking; at the provider default the
    description itself was truncated mid-sentence."""
    provider = FakeProvider()
    rel = _write_image(tmp_path)

    await _tool(tmp_path, provider).execute(path=rel)

    assert provider.calls[0]["max_tokens"] >= 2048


@pytest.mark.asyncio
async def test_blank_response_is_an_error(tmp_path) -> None:
    provider = FakeProvider(LLMResponse(content="   "))
    rel = _write_image(tmp_path)

    result = await _tool(tmp_path, provider).execute(path=rel)

    assert result == "Error: the vision model returned nothing"


@pytest.mark.asyncio
async def test_oversized_image_is_rejected(tmp_path) -> None:
    provider = FakeProvider()
    big = tmp_path / "big.jpg"
    big.write_bytes(b"\xff\xd8\xff" + b"\x00" * (9 * 1024 * 1024))

    result = await _tool(tmp_path, provider).execute(path="big.jpg")

    assert "over the" in result
    assert provider.calls == []

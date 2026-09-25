"""Vision tool: ask a vision model what an image actually shows.

The main model is text-only, and per-turn vision routing (see
``AgentLoop._vision_runner``) is decided from the messages that *start* a turn.
An image produced mid-turn — a camera snapshot, a downloaded photo — arrives too
late for that switch, so the only thing the main model ever sees is the tool
result's file path. Left there it will happily narrate the frame it never saw.

This tool closes that gap with a single stateless call to the vision provider,
the same shape as ``AgentRunner._translate_skill_invocation``: the image goes
out, a description comes back as text, and the main model answers from it.
"""

from __future__ import annotations

import asyncio
import base64
import os
import mimetypes
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from nanobot.agent.tools.base import tool_parameters
# Subclassed for its path resolution, not because this is a filesystem tool: it
# confines reads to the workspace and media dir. Without it the model could name
# any path on the host and this would ship those bytes off the machine — to the
# LAN today, and to whatever visionProvider is configured tomorrow.
from nanobot.agent.tools.filesystem import _FsTool
from nanobot.providers.base import LLMProvider
from nanobot.utils.helpers import detect_image_mime, strip_think

# Generous because the vision model is local: a cold qwen3-vl pays a model load
# into VRAM before the first token, which a cloud endpoint never did. Waiting is
# better than reporting a timeout on a call that was about to answer.
_TIMEOUT_SECONDS = 120.0
_MAX_IMAGE_BYTES = 8 * 1024 * 1024

# An image is not a small prompt. Measured on the house's camera frames, one
# describe_image call is ~3188 prompt tokens — the picture is nearly all of it.
# Ollama's default window is 4096, so only ~900 tokens were left to answer in,
# and qwen3-vl spends most of that thinking before it writes a word. The failure
# is silent and reaches the family: a real call came back with the eleven
# characters "En el patio" and finish_reason ``length``, which Alfred then
# relayed as the description of the patio.
#
# 8192 leaves roughly 5000 tokens of headroom after the image. Verified against
# the live model rather than assumed — an earlier attempt to bake a larger
# window into a custom Ollama variant (qwen3-vl-32k:8b) made it stop answering
# entirely, so this is deliberately a per-request option, not a new model.
_NUM_CTX = 8192


def _native_num_ctx() -> int:
    """The window to ask the native endpoint for.

    Ollama reloads a model whose requested context differs from the one it
    holds. Against a server left at its 4096 default, 8192 is the fix above.
    Against the house's text instance, which serves 65536, 8192 would reload
    gemma on every image and evict the assistants' resident slots -- so when
    the deployer says what the server serves (`OLLAMA_CONTEXT_LENGTH`, the
    server's own variable name), that is the number asked for: naming the
    window a model already holds never reloads it.
    """
    raw = (os.environ.get("OLLAMA_CONTEXT_LENGTH") or "").strip()
    if raw.isdigit() and int(raw) >= _NUM_CTX:
        return int(raw)
    return _NUM_CTX

# Bounds the answer inside that window; unlike _NUM_CTX this never had teeth on
# its own, because a cap on output cannot recover space the prompt already took.
_MAX_TOKENS = 4096

_DEFAULT_QUESTION = "What is in this image?"

# Two failure modes pull against each other here: narrating a frame the model
# never resolved, and dismissing a usable one. An earlier draft spelled out the
# refusal phrase; the model simply copied it back on a night shot where the roof,
# a table and a ladder were all plainly visible. So the escape hatch is described,
# never quoted, and partial detail is asked for first.
_SYSTEM_PROMPT = (
    "You are looking at a photo for someone who cannot see it. "
    "Answer their question using only what is actually visible in the image.\n"
    "- Say what you can make out, concretely and briefly (2-3 sentences). Night "
    "and infrared camera frames are dark and grainy but usually still show "
    "shapes, furniture, and whether anyone is present — describe those.\n"
    "- Frames arrive correctly oriented, but the cameras are mounted high and "
    "look steeply down, so perspective can be unusual. Read the scene anyway; "
    "do not comment on the camera angle.\n"
    "- Report that you cannot tell only when you genuinely cannot resolve the "
    "part being asked about, and say which part that is rather than dismissing "
    "the whole frame. Never assert something you did not see in order to give a "
    "fuller answer.\n"
    "- Do not infer names, identities, or intentions of people. Describe them "
    "as what is visible ('una persona con chaqueta oscura').\n"
    "- Do not speculate about what happened before or after the photo.\n"
    "- Reply in the same language as the question."
)


def _ollama_native_chat_url(api_base: str | None) -> str | None:
    """``http://ollama.home:11434/v1`` -> ``http://ollama.home:11434/api/chat``.

    Ollama's OpenAI-compatible ``/v1`` silently drops ``options``, so num_ctx
    cannot be raised there — the registry's ``{"options": {"think": False}}``
    override for qwen3 has been inert for exactly this reason. The native
    ``/api/chat`` honours it. Returns None for any other provider, which keeps
    every non-Ollama vision backend on the ordinary provider path.

    Matched on the port, the same signal the registry's ``detect_by_base_keyword``
    uses, so a host renamed from ``localhost`` to ``ollama.home`` still matches.
    """
    if not api_base or "11434" not in api_base:
        return None
    root = api_base.strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return f"{root}/api/chat"


@tool_parameters({
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Path to the image, as returned by the skill that produced it "
                "(e.g. 'media/cam_patio_1753630000.jpg'). Relative paths resolve "
                "against the workspace."
            ),
        },
        "question": {
            "type": "string",
            "description": (
                "What you need to know about the image, in the user's own terms "
                "(e.g. 'is anybody in the patio?'). Defaults to a general "
                "description."
            ),
        },
    },
    "required": ["path"],
})
class DescribeImageTool(_FsTool):
    """Send an image to the vision model and return what it reports seeing."""

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
        extra_allowed_dirs: list[Path] | None = None,
    ) -> None:
        super().__init__(
            workspace=workspace,
            allowed_dir=allowed_dir,
            extra_allowed_dirs=extra_allowed_dirs,
        )
        self._provider = provider
        self._model = model

    @property
    def name(self) -> str:
        return "describe_image"

    @property
    def description(self) -> str:
        return (
            "Look at an image file and answer a question about what it shows. "
            "You cannot see images yourself — this is the only way to know what "
            "is in one. Use it whenever the answer depends on the picture's "
            "content (a camera snapshot, a photo, a scanned page): take or "
            "locate the image first, then pass its path here. Never describe an "
            "image you have not passed through this tool."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def _describe_via_ollama(
        self, url: str, b64: str, prompt: str, path: str,
    ) -> str | None:
        """Ask Ollama directly so ``num_ctx`` is actually applied.

        Returns the description, or an ``Error:`` string when the model answered
        but said nothing usable. Returns None only when the call itself did not
        complete, which is the caller's signal to retry on the provider path —
        an empty answer must NOT fall back, since the fallback runs in the
        smaller window that caused the empty answer in the first place.
        """
        body = {
            "model": self._model,
            "stream": False,
            "options": {"num_ctx": _native_num_ctx(), "num_predict": _MAX_TOKENS},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                # The native schema takes images alongside plain text content,
                # not as OpenAI content parts.
                {"role": "user", "content": prompt, "images": [b64]},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=body)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning("describe_image: native call to {} failed: {}", url, exc)
            return None

        message = data.get("message") or {}
        # The native endpoint splits deliberation into its own field, so content
        # arrives clean. strip_think stays for the malformed inline tags Ollama's
        # renderer still emits on some models.
        described = strip_think(message.get("content") or "").strip()
        if described:
            if data.get("done_reason") == "length":
                logger.warning(
                    "describe_image: {} truncated at num_ctx={} "
                    "(prompt {} + answer {} tokens)",
                    path, _native_num_ctx(),
                    data.get("prompt_eval_count"), data.get("eval_count"),
                )
            return described

        logger.warning(
            "describe_image: {} produced no description (done_reason={}, "
            "prompt {} + answer {} tokens, {} chars of thinking)",
            path, data.get("done_reason"), data.get("prompt_eval_count"),
            data.get("eval_count"), len(message.get("thinking") or ""),
        )
        return "Error: the vision model returned nothing"

    async def execute(
        self,
        path: str | None = None,
        question: str | None = None,
        **kwargs: Any,
    ) -> Any:
        if not path:
            return "Error: path is required"

        try:
            resolved = self._resolve(path)
        except PermissionError as exc:
            return f"Error: {exc}"

        try:
            raw = resolved.read_bytes()
        except FileNotFoundError:
            return f"Error: no such image: {path}"
        except OSError as exc:
            return f"Error: could not read {path}: {exc}"

        if not raw:
            return f"Error: {path} is empty"
        if len(raw) > _MAX_IMAGE_BYTES:
            return (
                f"Error: {path} is {len(raw) // 1024}KB, over the "
                f"{_MAX_IMAGE_BYTES // (1024 * 1024)}MB limit for a vision call"
            )

        mime = detect_image_mime(raw) or mimetypes.guess_type(str(resolved))[0]
        if not mime or not mime.startswith("image/"):
            return f"Error: {path} is not an image"

        b64 = base64.b64encode(raw).decode()
        prompt = (question or "").strip() or _DEFAULT_QUESTION

        native_url = _ollama_native_chat_url(getattr(self._provider, "api_base", None))
        if native_url:
            described = await self._describe_via_ollama(native_url, b64, prompt, path)
            if described is not None:
                logger.info(
                    "describe_image: {} ({} bytes) via {} (native, num_ctx={})",
                    path, len(raw), self._model, _native_num_ctx(),
                )
                return described
            # Fall through: a native call that failed for any reason is not worth
            # losing the description over, and the provider path still works at
            # the default window.
            logger.warning(
                "describe_image: native Ollama call failed for {}, "
                "falling back to the provider endpoint", path,
            )

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                ],
            },
        ]

        try:
            response = await asyncio.wait_for(
                self._provider.chat_with_retry(
                    messages=messages,
                    tools=None,
                    model=self._model,
                    max_tokens=_MAX_TOKENS,
                    retry_mode="standard",
                ),
                timeout=_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("describe_image timed out for {}", path)
            return "Error: the vision model did not answer in time"
        except Exception as exc:
            logger.warning("describe_image failed for {}: {}", path, exc)
            return f"Error: vision call failed: {exc}"

        if response.finish_reason == "error":
            return f"Error: vision model error: {response.content or 'unknown'}"

        # The reasoning block is the model's scratch work, and it reaches the main
        # model verbatim as a tool result otherwise. strip_think also covers the
        # unclosed and malformed tags Ollama's renderer emits.
        described = strip_think(response.content or "").strip()
        if not described:
            return "Error: the vision model returned nothing"

        logger.info("describe_image: {} ({} bytes) via {}", path, len(raw), self._model)
        return described

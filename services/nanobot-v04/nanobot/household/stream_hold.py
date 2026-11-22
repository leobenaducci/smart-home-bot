"""Streamed text held while it could still be the reasoning again (household A14).

``_settle_reply`` drops a reply that only repeats the reasoning, but by then a
streamed echo has been painted live. The fork held content deltas while the
text so far could still be found inside the reasoning, and released them for
good the moment it could not; at the end anything still held is sent only if
the settled reply kept it. A provider whose reasoning *is* the answer
(StepFun) holds nothing.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

Delta = Callable[[str], Awaitable[None]]


def _squash(text: str) -> str:
    return " ".join(text.split())


def reasoning_is_answer(spec: Any, api_base: str | None, model: str | None) -> bool:
    """StepFun answers in ``reasoning``: by its spec, its base URL or, through a gateway, its model name."""
    if spec is not None and getattr(spec, "reasoning_as_content", False):
        return True
    if "stepfun" in str(api_base or "").lower():
        return True
    name = (model or "").lower()
    return "stepfun" in name or name.split("/")[-1].startswith("step-")


class EchoHold:
    """Wraps a call's ``on_content_delta``/``on_thinking_delta``."""

    def __init__(self, on_content: Delta, on_thinking: Delta | None) -> None:
        self._content, self._thinking = on_content, on_thinking
        self._reasoning: list[str] = []
        self._held = ""
        self.released = False

    async def thinking(self, delta: str) -> None:
        self._reasoning.append(delta)
        if self._thinking is not None:
            await self._thinking(delta)

    async def content(self, delta: str) -> None:
        if self.released:
            await self._content(delta)
            return
        self._held += delta
        squashed = _squash(self._held)
        reasoning = _squash("".join(self._reasoning))
        if squashed and not (reasoning and squashed in reasoning):
            self.released = True
            held, self._held = self._held, ""
            await self._content(held)

    async def settle(self, kept: str | None) -> None:
        """End of the call: the held text goes out only if the reply kept it."""
        if self._held and kept:
            held, self._held = self._held, ""
            self.released = True
            await self._content(held)
        self._held = ""


__all__ = ["EchoHold", "reasoning_is_answer"]

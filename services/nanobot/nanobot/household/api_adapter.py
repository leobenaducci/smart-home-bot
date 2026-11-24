"""household: the fork's view of the agent loop, on upstream's AgentLoop.

``api_server`` was written against the fork's loop: ``process_direct`` took
``powerful`` and ``profile``, sub-agents took ``context``/``complex``/
``media`` and were cancelled by id alone, and sessions were read
synchronously. This adapter answers those names with upstream's structure so
the server itself stays nearly verbatim.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext


class _UsageHook(AgentHook):
    """Reports each model call's usage the way the fork's progress did."""

    def __init__(self, on_progress: Any, model: str | None) -> None:
        super().__init__()
        self._on_progress = on_progress
        self._model = model

    async def after_iteration(self, context: AgentHookContext) -> None:
        usage = context.usage
        if usage is None or self._on_progress is None:
            return
        # v0.4.1's LLMUsage says input/output; the fork's dict said
        # prompt/completion, which is what the API's usage step reads.
        payload = {
            "prompt_tokens": (getattr(usage, "input_tokens", None)
                              or getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": (getattr(usage, "output_tokens", None)
                                  or getattr(usage, "completion_tokens", 0) or 0),
            "model": self._model,
        }
        try:
            await self._on_progress("", usage=payload)
        except Exception:
            logger.debug("api usage step failed", exc_info=True)


def _household_manager(loop: Any) -> bool:
    from nanobot.household.subagents import HouseholdSubagentManager

    return isinstance(getattr(loop, "subagents", None), HouseholdSubagentManager)


class _Subagents:
    def __init__(self, loop: Any) -> None:
        self._loop = loop

    async def spawn(
        self,
        task: str,
        *,
        context: str | None = None,
        complex: bool = False,  # noqa: A002 - the fork's name
        origin_channel: str = "api",
        origin_chat_id: str = "default",
        session_key: str | None = None,
        media: list[str] | None = None,
        harness: bool = True,
        **_ignored: Any,
    ) -> str:
        text = f"{context}\n\n---\n\n{task}" if context else task
        from nanobot.utils.llm_runtime import LLMRuntime

        runtime = LLMRuntime.capture(
            self._loop.provider, self._loop.model,
            context_window_tokens=self._loop.context_window_tokens,
        )
        kwargs = ({"media": media, "complex": complex, "harness": harness}
                  if _household_manager(self._loop) else {})
        return await self._loop.subagents.spawn(
            text,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            session_key=session_key,
            runtime=runtime,
            **kwargs,
        )

    async def cancel(self, task_id: str) -> bool:
        manager = self._loop.subagents
        record = getattr(manager, "_tasks", {}).get(task_id)
        if record is None or record.status.finished_at is not None:
            return False
        await manager.cancel(task_id, record.status.owner)
        return True


class _Sessions:
    def __init__(self, loop: Any) -> None:
        self._loop = loop

    def get_or_create(self, key: str) -> Any:
        from nanobot.session.manager import Session

        return self._loop.sessions.get_existing(key) or Session(key=key)


class LoopAdapter:
    def __init__(self, loop: Any) -> None:
        self._loop = loop
        self.subagents = _Subagents(loop)
        self.sessions = _Sessions(loop)

    @property
    def workspace(self) -> Any:
        return self._loop.workspace

    @property
    def cron_service(self) -> Any:
        return getattr(self._loop, "cron_service", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loop, name)

    async def process_direct(
        self,
        content: str,
        *,
        powerful: bool = False,
        profile: str | None = None,
        on_progress: Any = None,
        **kwargs: Any,
    ) -> Any:
        from nanobot.household.routing import resolve_runtime

        # Only a role the caller asked for is preset. A plain turn is left to
        # the loop, which classifies it like a chat turn and applies the
        # default effort itself (turn_router); presetting it here whenever
        # `reasoningEffortDefault` is set skipped routing for every API turn.
        runtime = (resolve_runtime(self._loop, powerful=powerful, profile=profile)
                   if powerful or profile else None)
        model = runtime.model if runtime is not None else getattr(self._loop, "model", None)
        extra: dict[str, Any] = {}
        if runtime is not None:
            extra["runtime"] = runtime
        if on_progress is not None:
            extra["on_progress"] = on_progress
            extra["hooks"] = [_UsageHook(on_progress, model)]
        return await self._loop.process_direct(content=content, **kwargs, **extra)

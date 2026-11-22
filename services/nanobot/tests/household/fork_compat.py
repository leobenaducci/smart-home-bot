"""household test support: the fork's runner entry points on v0.4.1.

The fork's tests call ``AgentRunner._apply_text_tool_call_parsing(response,
spec, messages)`` and ``AgentRunner(provider).run(AgentRunSpec(...))``. This
module answers those names with the trial's structure -- the parser module
and upstream's runner with the household hook -- so the fork's tests run
unchanged apart from their import line.
"""

from __future__ import annotations

from typing import Any

from agent.runner_helpers import make_run_spec

from nanobot.agent.runner import AgentRunner as _UpstreamRunner
from nanobot.household import text_calls
from nanobot.agent.hook import CompositeHook
from nanobot.household.hooks import HouseholdNudgeHook, HouseholdResponseHook
from nanobot.household.nudges import *  # noqa: F401,F403
from nanobot.household.nudges import ANNOUNCED_STEP_PROMPT  # noqa: F401
from nanobot.household import skill_translation

# The fork's tests import its runner's module-level helpers, private ones too.
globals().update({k: v for k, v in vars(text_calls).items() if not k.startswith("__")})
globals().update({k: v for k, v in vars(skill_translation).items() if not k.startswith("__")})


class AgentRunSpec(dict):  # the fork's name; attribute and key access
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        kw.setdefault("session_key", None)
        kw.setdefault("workspace", None)
        kw.setdefault("initial_messages", [])
        kw.setdefault("read_only", False)
        kw.setdefault("refused", None)
        self.__dict__.update(kw)


def _view(spec: dict[str, Any]) -> text_calls.TurnView:
    return text_calls.TurnView(
        tools=spec.get("tools"),
        workspace=spec.get("workspace"),
        session_key=spec.get("session_key"),
        max_iterations=spec.get("max_iterations", 200),
    )


def _upstream_usage(response: Any) -> Any:
    # The fork's providers returned usage as a dict; upstream's an LLMUsage.
    if isinstance(getattr(response, "usage", None), dict):
        response.usage = None
    return response


class AgentRunner:
    def __init__(self, provider: Any = None) -> None:
        self.provider = provider

    @staticmethod
    def _apply_text_tool_call_parsing(response, spec, messages=None):
        view = spec if isinstance(spec, text_calls.TurnView) else _view(spec)
        token = text_calls.SKILL_ROOT_WORKSPACE.set(view.workspace)
        try:
            return text_calls.apply_text_tool_call_parsing(response, view, messages)
        finally:
            text_calls.SKILL_ROOT_WORKSPACE.reset(token)

    async def _maybe_translate_skill_calls(self, response, spec, messages):
        # Called unbound in the fork's tests (self is None). No veto channel
        # here, so refusals take the fork's exec-printf shape.
        token = text_calls.SKILL_ROOT_WORKSPACE.set(getattr(spec, "workspace", None))
        try:
            return await skill_translation.maybe_translate_skill_calls(
                response, spec, messages,
                provider=getattr(self, "provider", None),
                model=getattr(spec, "model", "") or "",
                refusals=None,
            )
        finally:
            text_calls.SKILL_ROOT_WORKSPACE.reset(token)

    async def run(self, spec: dict[str, Any]):
        spec = dict(spec)
        provider = self.provider
        fork_chat = provider.chat_with_retry

        async def chat(**kw: Any) -> Any:
            kw.pop("on_content_delta", None)
            kw.pop("on_reasoning_delta", None)
            kw.pop("on_thinking_delta", None)
            return _upstream_usage(await fork_chat(**kw))

        provider.chat_with_retry = chat
        provider.chat_stream_with_retry = chat
        max_iterations = spec.get("max_iterations", 200)
        nudge = HouseholdNudgeHook(max_iterations=max_iterations,
                                   read_only=bool(spec.get("read_only")))
        hook = CompositeHook([
            HouseholdResponseHook(
                spec["tools"], workspace=spec.get("workspace"),
                session_key=spec.get("session_key"), max_iterations=max_iterations,
            ),
            nudge,
        ])
        for key in ("hook", "read_only", "refused", "model_profile"):
            spec.pop(key, None)
        result = await _UpstreamRunner().run(make_run_spec(provider, hook=hook, **spec))
        if nudge.stop_reason:
            result.stop_reason = nudge.stop_reason
        return result

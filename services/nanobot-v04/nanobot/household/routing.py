"""household: which model a turn runs on, by role (fork B3, first part).

HomeCore names a *role* -- a Profession (``profile``) or ``powerful`` -- and
the roster lives in this process's config (``modelProfiles``,
``providerProfiles``, ``reasoningEffortProfiles``, ``modelPowerful``...), so a
client never has to know which model serves it. Upstream has model presets
and a per-session resolver but no roles; this turns a role into an
``LLMRuntime`` for ``process_direct(runtime=...)``.

A role on the default provider reuses the loop's provider; one on another
provider gets its own client from upstream's ``make_provider`` (cached).
OpenCode Go is refused by upstream routing (household area 7), whatever the
role says. ``plan=True`` is the planner (``planModel``/``planProvider``) that
a ``long`` turn runs on; unset, the turn keeps the everyday model.
``plan_step=True`` is ``planStepModel``/``planStepProvider`` (plan_steps.py).

Fallback (A5, the fork's ``_outage_fallback_for``): a role on the default
provider reuses the loop's provider, whose ``fallbackModels`` chain (the
household's ``modelFallback``) takes the role's own model as its primary, so
the role falls back like the everyday model does. A role on another provider
falls back to the same chain (household decision, 2026-11-21; the fork gave it none), and
vision never takes one.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from loguru import logger

from nanobot.household.runtime import RUNTIME

_PROVIDERS: dict[tuple[str, str, bool], Any] = {}


def role_route(defaults: Any, *, powerful: bool = False, profile: str | None = None,
               vision: bool = False, plan: bool = False,
               plan_step: bool = False) -> tuple[str, str | None, str | None] | None:
    """(model, provider, reasoning effort) for a role, or None for the default turn."""
    if vision and defaults.vision_model:
        return defaults.vision_model, defaults.vision_provider, None
    if plan_step and defaults.plan_step_model:  # effort: routing.planStepEffort (plan_steps)
        return defaults.plan_step_model, defaults.plan_step_provider, None
    if plan and defaults.plan_model:
        return defaults.plan_model, defaults.plan_provider, defaults.reasoning_effort_default
    if profile and profile in defaults.model_profiles:
        return (
            defaults.model_profiles[profile],
            defaults.provider_profiles.get(profile),
            defaults.reasoning_effort_profiles.get(profile),
        )
    if powerful and defaults.model_powerful:
        return defaults.model_powerful, defaults.provider_powerful, defaults.reasoning_effort_powerful
    if defaults.reasoning_effort_default:
        return defaults.model, None, defaults.reasoning_effort_default
    return None


def _provider_for(config: Any, provider_name: str, model: str, effort: str | None,
                  *, chain: bool = True) -> Any:
    key = (provider_name, model, chain)
    if key not in _PROVIDERS:
        from nanobot.config.schema import ModelPresetConfig
        from nanobot.providers.factory import _make_provider_core, make_provider

        d = config.agents.defaults
        preset = ModelPresetConfig(
            model=model, provider=provider_name, max_tokens=d.max_tokens,
            context_window_tokens=d.context_window_tokens, temperature=d.temperature,
            reasoning_effort=effort,
        )
        # Household decision, 2026-11-21: a role on another provider falls back to the main
        # provider's `fallbackModels` (the household's `modelFallback`, whose
        # entries name the default provider). The fork gave it no rescue.
        # Vision never takes a chain: a text model cannot stand in for it.
        build = make_provider if chain else _make_provider_core
        _PROVIDERS[key] = build(config, preset=preset)
    return _PROVIDERS[key]


def resolve_runtime(loop: Any, *, powerful: bool = False, profile: str | None = None,
                    vision: bool = False, plan: bool = False, plan_step: bool = False,
                    config: Any = None) -> Any:
    """An LLMRuntime for the role, or None to let the loop pick its own."""
    from nanobot.utils.llm_runtime import LLMRuntime

    cfg = config or RUNTIME.config
    if cfg is None:
        return None
    d = cfg.agents.defaults
    route = role_route(d, powerful=powerful, profile=profile, vision=vision, plan=plan,
                       plan_step=plan_step)
    if route is None:
        return None
    model, provider_name, effort = route
    try:
        if provider_name and provider_name != d.provider:
            provider = _provider_for(cfg, provider_name, model, effort, chain=not vision)
        else:
            provider = loop.provider
            if vision:
                # Never a text model in place of the vision one (fork: "not
                # applied to visionModel: different modality").
                from nanobot.providers.fallback_provider import FallbackProvider

                if isinstance(provider, FallbackProvider):
                    provider = provider._primary
    except Exception as exc:  # a role must not take the turn down
        logger.warning("role route {}/{} unavailable ({}); using the default model",
                       provider_name, model, exc)
        return None
    runtime = LLMRuntime.capture(
        provider, model, context_window_tokens=getattr(loop, "context_window_tokens", None)
        or d.context_window_tokens,
    )
    if effort:
        runtime = dataclasses.replace(
            runtime, generation=dataclasses.replace(runtime.generation, reasoning_effort=effort),
        )
    return runtime

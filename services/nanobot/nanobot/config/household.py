"""household: configuration blocks this stack adds to upstream's schema.

Upstream's ``Base`` ignores unknown keys, so every key here used to be parsed
away in silence by v0.4.1 -- a household setting that never reached the code
that reads it. The models live in one module so the delta against upstream's
``schema.py`` stays a handful of field declarations.

The consumers are the household modules (routing, plans, sub-agents, the pi
harness, n8n, profiling); this module only makes sure the values arrive.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

from nanobot.config_base import Base


class RoutingConfig(Base):
    """Which tier a turn starts on, and when a failed cheap attempt moves up.

    ``mode``: ``off`` routes nothing; ``shadow`` classifies and records every
    turn but changes nothing; ``active`` routes on the label and escalates on
    failure. ``escalate_on`` are runner stop reasons that mean the cheap attempt
    did not finish; the strong model then continues the same turn, at most
    ``max_escalations_per_turn`` times.
    """

    mode: Literal["off", "shadow", "active"] = "shadow"
    timeout_s: float = Field(default=3.0, ge=0.2)
    long_message_chars: int = Field(default=600, ge=50)
    sticky_turns: int = Field(default=3, ge=0)
    max_escalations_per_turn: int = Field(default=1, ge=0)
    escalate_on: list[str] = Field(default_factory=lambda: [
        "bad_invocation", "error", "empty_final_response",
        "repeated_tool_calls", "max_iterations",
    ])
    subagents: bool = True
    complex_iterations: int = Field(default=80, ge=1)
    plan_step_calls: int = Field(default=24, ge=1)
    plan_step_effort: str | None = "none"
    plan_detach_seconds: int = Field(default=240, ge=30)
    plan_chat_seconds: int = Field(default=300, ge=30)
    plan_executor: Literal["subagent", "everyday"] = "subagent"
    everyday_iterations: int = Field(default=40, ge=1)


class HarnessConfig(Base):
    """Long background tasks on the pi harness. Off by default."""

    enabled: bool = False
    engine: Literal["pi"] = "pi"
    long_tasks: bool = False
    base_url: str = ""
    model: str = ""
    api_key: str = Field(default="", repr=False)
    context_window: int = 40960
    reasoning: bool = True
    thinking: str = "off"
    timeout_s: int = 900
    max_nudges: int = 3
    keep_workdirs: int = 10


class N8nConfig(Base):
    """n8n workflow automation, read by the n8n skill."""

    api_key: str | None = Field(default=None, repr=False)
    base_url: str = "http://localhost:5678"
    workflows_endpoint: str = "/api/v1/workflows/"

    model_config = ConfigDict(extra="allow")


class ProfilingConfig(Base):
    """Where turns spend time and tokens (/v1/debug/profile). On by default."""

    enabled: bool = True


class HouseholdAgentFields(Base):
    """Fields mixed into ``AgentDefaults``. See ``AgentDefaults`` for the MRO."""

    context_block_limit: int | None = None
    harness: HarnessConfig = Field(default_factory=HarnessConfig)
    subagent_model: str | None = None
    subagent_provider: str | None = None
    subagent_model_powerful: str | None = None
    subagent_provider_powerful: str | None = None
    model_powerful: str | None = None
    provider_powerful: str | None = None
    model_profiles: dict[str, str] = Field(default_factory=dict)
    provider_profiles: dict[str, str] = Field(default_factory=dict)
    reasoning_effort_profiles: dict[str, str] = Field(default_factory=dict)
    reasoning_effort_powerful: str | None = None
    reasoning_effort_default: str | None = None
    vision_model: str | None = None
    vision_provider: str | None = None
    heartbeat_model: str | None = None
    heartbeat_provider: str | None = None
    heartbeat_reasoning_effort: str | None = None
    classifier_model: str | None = None
    classifier_provider: str | None = None
    classifier_reasoning_effort: str | None = "none"
    plan_model: str | None = None
    plan_provider: str | None = None
    plan_step_model: str | None = None
    plan_step_provider: str | None = None
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    # Kept verbatim as well as mapped onto fallback_models: the household's
    # outage fallback is per role (each role falls back on its own provider),
    # which upstream's single fallback chain cannot say.
    model_fallback: str | list[str | dict[str, Any]] | dict[str, Any] | None = None


def map_model_fallback(data: dict[str, Any]) -> dict[str, Any]:
    """Translate the household's ``modelFallback`` into upstream ``fallbackModels``.

    Upstream insists a *string* fallback names a model preset. Ours is a bare
    model name meaning "the same provider as the role", so each entry becomes
    an inline ``{model, provider}`` candidate on the default provider. An entry
    that already names a provider keeps it. An explicit ``fallbackModels`` wins.
    """
    raw = data.get("modelFallback", data.get("model_fallback"))
    if raw is None or "fallbackModels" in data or "fallback_models" in data:
        return data
    default_provider = data.get("provider", "auto")
    items = raw if isinstance(raw, list) else [raw]
    mapped: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            mapped.append({"model": item.strip(), "provider": default_provider})
        elif isinstance(item, dict) and item.get("model"):
            entry = dict(item)
            entry.setdefault("provider", default_provider)
            mapped.append(entry)
    return {**data, "fallbackModels": mapped}


__all__ = [
    "HarnessConfig",
    "HouseholdAgentFields",
    "N8nConfig",
    "ProfilingConfig",
    "RoutingConfig",
    "map_model_fallback",
]

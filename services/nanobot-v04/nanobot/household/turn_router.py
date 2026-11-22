"""household: route every turn by role and label, and escalate a failed cheap one (fork B3).

``HouseholdAgentLoop`` (household/loop.py) calls ``route_turn`` from
``_build_turn`` for every user turn, whatever the channel -- WhatsApp, email,
cron, voice, HomeCore -- and ``escalation_runtime`` after ``_run_turn``.

Order, as in the fork's ``AgentLoop._route_turn``: a turn that already
carries a runtime (the API's ``profile``/``powerful``) is forced; an image
turn goes to the vision model; a HomeCore space session is forced powerful;
a session that escalated recently starts strong for ``routing.sticky_turns``;
otherwise the classifier's label decides in ``active`` mode, is only recorded
in ``shadow`` mode, and nothing is asked in ``off`` mode.

A ``long`` label runs on the planner (``planModel``) when one is set, with the
plan tool (household/plan.py); otherwise on the everyday model.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from nanobot.household.classify import TurnClass, TurnClassifier, previous_assistant_text
from nanobot.household.runtime import RUNTIME

STICKY_KEY = "sticky_powerful"
_CLASSIFIERS: dict[tuple[str, str], TurnClassifier] = {}
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic")


def _classifier(loop: Any, config: Any) -> TurnClassifier | None:
    d = config.agents.defaults
    if not d.classifier_model:
        return None
    key = (d.classifier_provider or "", d.classifier_model)
    if key not in _CLASSIFIERS:
        from nanobot.household.routing import _provider_for

        provider = (
            _provider_for(config, d.classifier_provider, d.classifier_model, d.classifier_reasoning_effort)
            if d.classifier_provider and d.classifier_provider != d.provider
            else loop.provider
        )
        _CLASSIFIERS[key] = TurnClassifier(
            provider, d.classifier_model,
            timeout_s=d.routing.timeout_s,
            long_message_chars=d.routing.long_message_chars,
            reasoning_effort=d.classifier_reasoning_effort,
        )
    return _CLASSIFIERS[key]


def _has_image(media: list[str] | None) -> bool:
    return any(str(m).lower().endswith(_IMAGE_SUFFIXES) for m in media or [])


async def route_turn(loop: Any, *, text: str, media: list[str] | None, chat_id: str,
                     session_metadata: dict[str, Any], history: list[dict[str, Any]],
                     config: Any = None) -> tuple[TurnClass, Any]:
    """(decision, runtime or None for the loop's default)."""
    from nanobot.household.routing import resolve_runtime
    from nanobot.utils.homeweb_chat_id import is_space_session

    cfg = config or RUNTIME.config
    d = cfg.agents.defaults
    if _has_image(media) and d.vision_model:
        return TurnClass("forced", "vision", "image", "forced"), resolve_runtime(loop, vision=True, config=cfg)
    if is_space_session(chat_id):
        return TurnClass("forced", "powerful", "space", "forced"), resolve_runtime(loop, powerful=True, config=cfg)
    mode = d.routing.mode
    classifier = _classifier(loop, cfg) if mode != "off" else None
    if classifier is None:
        return TurnClass("action", "everyday", "", "off"), resolve_runtime(loop, config=cfg)
    if mode == "active":
        left = int(session_metadata.get(STICKY_KEY) or 0)
        if left > 0:
            session_metadata[STICKY_KEY] = left - 1
            return (TurnClass("sticky", "powerful", f"{left} sticky turns left", "sticky"),
                    resolve_runtime(loop, powerful=True, config=cfg))
    route = await classifier.classify(
        text, previous=previous_assistant_text(history),
        attachments=len(media or []),
    )
    if mode != "active":
        return (TurnClass(route.label, "everyday", route.reason, f"shadow:{route.source}", route.ms),
                resolve_runtime(loop, config=cfg))
    if route.tier == "powerful":
        return route, resolve_runtime(loop, powerful=True, config=cfg)
    if route.label == "long" and d.plan_model:
        return route, resolve_runtime(loop, plan=True, config=cfg)
    return route, resolve_runtime(loop, config=cfg)


def dead_end(route: TurnClass | None, stop_reason: str, config: Any = None) -> bool:
    """A cheap attempt that ended badly, in active mode, once -- whatever there
    is to escalate to (fork ``_should_escalate_any``: a sub-agent needs no
    stronger model)."""
    cfg = config or RUNTIME.config
    if cfg is None or route is None:
        return False
    r = cfg.agents.defaults.routing
    return (
        r.mode == "active"
        and r.max_escalations_per_turn > 0
        and route.tier == "everyday"
        and route.escalated_from is None
        and stop_reason in set(r.escalate_on)
    )


def should_escalate(route: TurnClass | None, stop_reason: str, config: Any = None) -> bool:
    """A dead end the powerful model can continue (where it is not handed to a
    sub-agent: voice, system turns, someone else's message)."""
    cfg = config or RUNTIME.config
    return dead_end(route, stop_reason, cfg) and bool(cfg.agents.defaults.model_powerful)


def escalation_runtime(loop: Any, config: Any = None) -> Any:
    from nanobot.household.routing import resolve_runtime

    runtime = resolve_runtime(loop, powerful=True, config=config)
    if runtime is None:
        logger.warning("routing: escalation wanted but no powerful model resolves")
    return runtime

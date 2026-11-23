"""household: everything this stack adds to upstream nanobot.

Kept in one package so the delta against upstream stays visible: upstream
files carry only short ``# household:`` seams that call into here.
"""

from __future__ import annotations

from typing import Any


def household_hook_factories(config: Any, tools: Any) -> list[Any]:
    """Per-turn hook factories the gateway adds to AgentLoop."""
    from nanobot.household.hooks import (
        nudge_hook_factory,
        response_hook_factory,
        tool_timing_hook_factory,
    )

    max_iterations = config.agents.defaults.max_tool_iterations
    return [
        response_hook_factory(tools, max_iterations=max_iterations),
        nudge_hook_factory(max_iterations=max_iterations),
        tool_timing_hook_factory(),
    ]


def bind_household_loop(loop: Any, config: Any) -> None:
    """Called by the gateway once its AgentLoop exists."""
    from nanobot.household.runtime import RUNTIME

    RUNTIME.bind(loop, config)
    _warm_classifier(loop, config)
    # describe_image from the first turn, as the fork had it (D6).
    ensure = getattr(loop, "_ensure_describe_image", None)
    if callable(ensure):
        try:
            ensure()
        except Exception:  # noqa: BLE001 -- a vision misconfiguration must not stop the gateway
            from loguru import logger

            logger.exception("describe_image could not be registered at start")
    from nanobot.household.profiling import PROFILER

    profiling = getattr(config, "profiling", None)
    PROFILER.enabled = bool(getattr(profiling, "enabled", True))
    # D6: skills a house service serves, kept on disk while it answers
    # (off with NANOBOT_REMOTE_SKILLS=0 or when no service is discovered).
    from nanobot.household.remote_skills import RemoteSkillSync
    from nanobot.household.skill_roots import HOUSEHOLD_SKILLS_DIR

    try:
        RemoteSkillSync.ensure_started(loop.workspace, HOUSEHOLD_SKILLS_DIR)
    except Exception:  # a sync that cannot start must not stop the gateway
        from loguru import logger

        logger.exception("remote skill sync did not start")


def health_ok_while_starting(config: Any) -> bool:
    """Gateway ``/health`` answers 200 while channels start (``gateway.healthOkWhileStarting``).

    The fork's gateway always answered 200; upstream answers 503 until the
    websocket channel runs, which a compose healthcheck reads as unhealthy
    during a slow boot. The readiness is still in the payload (``ready``).
    """
    from nanobot.household.skill_roots import household_enabled

    gateway = getattr(config, "gateway", None)
    return household_enabled() and bool(getattr(gateway, "health_ok_while_starting", False))


_WARMING: set = set()


def _warm_classifier(loop: Any, config: Any) -> None:
    """Ask the turn classifier once in the background at start (fork loop.warm_up).

    The first call to the local classifier after a restart is the cold one and
    crossed its 3 s timeout in the dress rehearsal, so the first real turn was
    routed on the default label. A warm-up that fails or is slow costs nobody
    a turn.
    """
    import asyncio

    try:
        from nanobot.household import turn_router

        if config.agents.defaults.routing.mode == "off":
            return
        classifier = turn_router._classifier(loop, config)
        if classifier is None:
            return
        task = asyncio.get_running_loop().create_task(classifier.classify("hola"))
    except Exception:  # noqa: BLE001 -- no running loop (tests) or no classifier
        return
    _WARMING.add(task)
    task.add_done_callback(_WARMING.discard)

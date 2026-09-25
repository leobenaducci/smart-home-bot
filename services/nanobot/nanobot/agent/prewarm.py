"""Prewarm the serving engine's prefix cache with the house-shared prompt head.

FreeToken's prefix cache will append to a sequence it has already seen ending
at a given point, but it will not re-enter one from the middle. Measured on
2026-09-10 against qwen3.6-35b-a3b: two requests sharing a 12.6k-token head but
differing after it cost 18.7s and 16.4s to first token -- no reuse at all. Send
that head as a request of its own first, so a boundary exists there, and the
same two requests cost 4.3s.

So this is not tuning an existing benefit; it is what creates the benefit. One
call per engine lifetime is enough, and it costs one full prefill (~15s) that
the first real turn would otherwise have paid anyway.

Deliberately best-effort: a house whose prewarm fails is a slower house, not a
broken one, so every failure is logged and swallowed.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from loguru import logger

# How long one prewarm may take. Long enough for a cold local engine to load
# the model and prefill ~10k tokens; short enough that a stuck engine cannot
# hold the assistant's startup.
PREWARM_TIMEOUT_S = 120.0

# Hosts whose engine keeps a prefix cache this can seed: the household's own.
# A hosted gateway gains nothing from a system-only request, and on 2026-09-10
# it lost a lot: with `everyday` on deepseek-v4-flash, the prewarm's 16k-char
# system prompt timed out three times at Zen, the retry ladder marked the model
# DOWN for an hour on all six assistants, and the house answered on the local
# fallback until it expired. Local, or nothing.
_LOCAL_HOST_MARKERS = ("host.docker.internal", "localhost", ".home", ".local", ".lan")


def is_local_engine(api_base: str | None) -> bool:
    host = (urlsplit(api_base or "").hostname or "").lower()
    if not host:
        return False
    if host.startswith(("127.", "10.", "192.168.")) or host == "::1":
        return True
    if host.startswith("172.") and host.split(".")[1].isdigit() and 16 <= int(host.split(".")[1]) <= 31:
        return True
    return any(host == m.lstrip(".") or host.endswith(m) for m in _LOCAL_HOST_MARKERS)


async def prewarm_shared_prefix(
    provider: Any,
    context_builder: Any,
    model: str | None = None,
    channel: str = "api",
) -> bool:
    """Send the shared prompt head so later turns can append to it.

    Returns True when a prewarm was actually sent. One plain request -- no
    retries, no fallback, no outage marking: this is an optimisation, and an
    optimisation must never decide that a model is down.
    """
    if not is_local_engine(getattr(provider, "api_base", None)):
        logger.debug("prewarm: {} is not a local engine; nothing to warm",
                     getattr(provider, "api_base", None))
        return False
    try:
        prefix = context_builder.shared_prompt_prefix(channel=channel)
    except Exception as exc:  # a workspace mid-write, a missing file
        logger.warning("prewarm: could not build shared prefix: {}", exc)
        return False

    if not prefix:
        logger.debug("prewarm: no shared bootstrap files; nothing to warm")
        return False

    try:
        # `max_tokens=1` because nothing reads the answer -- the point is the
        # prefill, and the boundary it leaves in the engine's cache.
        await asyncio.wait_for(
            provider.chat(
                messages=[{"role": "system", "content": prefix}],
                model=model,
                max_tokens=1,
            ),
            timeout=PREWARM_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warning("prewarm: request failed ({}); turns will prefill normally", exc)
        return False

    logger.info("prewarm: shared prefix warmed ({} chars)", len(prefix))
    return True

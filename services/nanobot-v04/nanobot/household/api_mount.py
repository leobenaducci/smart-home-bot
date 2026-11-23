"""household: serve the HomeCore API inside the gateway process (fork B4).

``nanobot gateway --api-port N`` (the household entrypoint) sets
``NANOBOT_HOUSEHOLD_API_PORT``; the gateway then runs ``api_server`` on the
same event loop and AgentLoop as its channels, so a sub-agent's or a cron
job's result reaches the HomeCore streams. Upstream's ``nanobot serve`` is a
separate process with its own loop and cannot do that.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from loguru import logger


def household_api_port() -> int | None:
    raw = os.environ.get("NANOBOT_HOUSEHOLD_API_PORT", "").strip()
    return int(raw) if raw else None


async def serve_household_api(agent: Any, config: Any, port: int,
                              shutdown: asyncio.Event | None = None) -> None:
    from aiohttp import web

    from nanobot.household.api_server import create_app

    host = os.environ.get("NANOBOT_HOUSEHOLD_API_HOST", "0.0.0.0")
    app = create_app(agent, model_name=getattr(agent, "model", "nanobot") or "nanobot",
                     request_timeout=float(config.api.timeout))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("household API: http://{}:{}/v1/chat/completions", host, port)
    try:
        if shutdown is not None:
            await shutdown.wait()
        else:
            await asyncio.Event().wait()
    finally:
        await runner.cleanup()

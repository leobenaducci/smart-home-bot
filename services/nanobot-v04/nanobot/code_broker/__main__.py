"""`python -m nanobot.code_broker` — the broker as its own process.

Same image as the agent, different entrypoint and a different environment: this
container holds the project credentials and none of the family's, and the agent
containers hold the family's and none of the projects'. That split is the whole
design, and it is enforced by which variables each service is given rather than
by anything in this file.
"""

import os

from aiohttp import web
from loguru import logger

from nanobot.code_broker.service import build_from_env


def main() -> None:
    port = int(os.environ.get("CODE_BROKER_PORT", "8910"))
    logger.info("code-broker starting on :{}", port)
    web.run_app(build_from_env(), host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()

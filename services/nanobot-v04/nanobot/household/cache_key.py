"""A provider session id that survives a restart (household A12, fork ``_stable_session_id``).

Upstream's ``OpenAICompatProvider`` names itself with ``uuid4()`` on every
start: the ``x-session-affinity`` header, and ``x-opencode-session`` on a call
with no conversation. The fork made that id stable and also sent it as
``prompt_cache_key`` on Responses requests to OpenAI and OpenCode, because a
new key is a cold prefix cache for every conversation: each deploy cost every
assistant a full-price first turn.

The id is a hash of ``NANOBOT_INSTANCE`` (the member or house the entrypoint
runs), the endpoint and the model. So it is the same across restarts, never
shared by two members or two endpoints, and does not send the member id in
the clear. With no instance name it is random again: a unique key is merely
useless, while a key shared by accident is wrong.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from urllib.parse import urlparse


def stable_session_id(api_base: str | None, model: str | None) -> str:
    instance = os.environ.get("NANOBOT_INSTANCE", "").strip()
    if not instance:
        return uuid.uuid4().hex
    seed = f"{instance}|{api_base or ''}|{model or ''}"
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def uses_openai_caching(opencode: bool, api_base: str | None) -> bool:
    """Whether ``prompt_cache_key`` means anything to this endpoint.

    OpenAI defines the field and OpenCode Zen proxies to models that honour
    it. A local vLLM, SGLang or Ollama Responses endpoint may refuse an
    unknown body field, which would turn a caching hint into an outage.
    """
    if opencode:
        return True
    host = (urlparse(api_base or "https://api.openai.com/v1").hostname or "").lower()
    return host == "api.openai.com"


__all__ = ["stable_session_id", "uses_openai_caching"]

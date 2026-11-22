"""household: MCP servers the operator configured may live on private addresses.

v0.4.1 runs every MCP URL through the SSRF guard (connect check, a per-request
hook, and a DNS-pinning transport), so a server on the LAN, the Docker network
or ``host.docker.internal`` is refused. The household's are all there: Home
Assistant and home-devices through ``host.docker.internal``, crawl4ai and craft
on the compose network. The fork never checked MCP URLs. This trusts exactly
the hosts named in the configuration, and only for MCP traffic: a redirect to
any other private host is still refused, and web fetches and exec keep the
guard.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

_trusted: set[str] = set()


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _enabled() -> bool:
    from nanobot.household.skill_roots import household_enabled

    return household_enabled()


def check_configured(url: str, validate: Any) -> tuple[bool, str]:
    """The connect-time check for a configured server's URL."""
    if _enabled() and (host := _host(url)):
        _trusted.add(host)
        return True, ""
    return validate(url)


def check_request(url: str, validate: Any) -> tuple[bool, str]:
    """The per-request check: a configured host passes, anything else is validated."""
    if _enabled() and _host(url) in _trusted:
        return True, ""
    return validate(url)


def transport_for(url: str | None) -> Any:
    """A plain transport for a configured host, or None for upstream's pinned one."""
    if url and _enabled() and _host(url) in _trusted:
        import httpx

        return httpx.AsyncHTTPTransport()
    return None

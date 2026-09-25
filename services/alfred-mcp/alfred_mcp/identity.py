"""Who is calling, and the one rule this service exists to keep.

The agent on the other end of these tools is an LLM reading a prompt, and a
prompt is something anyone who can get text in front of it can write. So:

    identity comes from the HTTP header, never from a tool argument.

Not one tool here takes a `user`, a `member` or a `login`. There is nothing to
take: this process holds exactly one person's credentials (see config.py), and
the header only decides whether the call is allowed at all -- it cannot select
*which* person the call acts as, because there is only ever one.

That is the same reasoning the code broker's own docstring gives for existing,
and it is why this server sits in front of the broker rather than beside it.
"""

from __future__ import annotations

import hmac

from mcp.server.fastmcp import Context

USER_HEADER = "x-alfred-user"
TOKEN_HEADER = "x-alfred-token"


class Denied(Exception):
    """The caller did not prove it is the member this container serves."""


def _headers(ctx: Context) -> dict:
    """The request's headers, or {} when there is no HTTP request.

    FastMCP hands the tool a request context; over the streamable-HTTP
    transport it carries the Starlette request, and over stdio it does not.
    Missing is treated as unauthenticated rather than as a crash: this server
    is only ever run over HTTP, so no request means something is calling it in
    a way it was not built for.
    """
    request = getattr(ctx.request_context, "request", None)
    if request is None:
        return {}
    return {k.lower(): v for k, v in request.headers.items()}


def authenticate(ctx: Context, member: str, token: str) -> str:
    """Return the member this call may act as, or raise Denied.

    `compare_digest` for the same reason the broker uses it: the check is cheap
    and remote, and a timing signal is worth denying even where exploiting it
    would be a stretch.

    The member is compared too, not just the token. A correct token presented
    under another member's name is a configuration error worth refusing loudly
    -- one container, one person, and a call that disagrees about which person
    is not one this process should serve.
    """
    headers = _headers(ctx)
    said_user = (headers.get(USER_HEADER) or "").strip()
    said_token = (headers.get(TOKEN_HEADER) or "").strip()

    if not said_user or not said_token:
        raise Denied(
            "This call carried no identity. opencode must send "
            f"{USER_HEADER} and {TOKEN_HEADER}; see the `mcp` block that "
            "./home-stack deploy writes into opencode.json.")

    ok_token = hmac.compare_digest(said_token, token)
    ok_user = hmac.compare_digest(said_user, member)
    # Both compared before either is reported on, so the answer does not say
    # which half was wrong -- a caller probing this should learn nothing about
    # whether it has the right name or the right token.
    if not (ok_token and ok_user):
        raise Denied("Not authorised to act for this household member.")
    return member

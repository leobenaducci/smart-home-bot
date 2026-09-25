"""Identity comes from the header, and there is nothing else it can come from.

This is the file to read first if you are changing anything here. The agent on
the other end of these tools is a model reading a prompt, and a prompt is
something anybody who can get text in front of it can write -- a page it
fetched, a file in a repo, a forwarded notification. So a tool that took a
`user` argument would be one sentence away from acting as somebody else.
"""

import pytest

from alfred_mcp.identity import Denied, authenticate

MEMBER = "user1"
TOKEN = "the-expected-token"


class _Req:
    def __init__(self, headers):
        self.headers = headers


class _Ctx:
    """Enough of a FastMCP Context for authenticate() to read."""

    def __init__(self, headers=None, with_request=True):
        self.request_context = type(
            "RC", (), {"request": _Req(headers or {}) if with_request else None})()


def test_the_right_pair_is_accepted():
    ctx = _Ctx({"X-Alfred-User": MEMBER, "X-Alfred-Token": TOKEN})
    assert authenticate(ctx, MEMBER, TOKEN) == MEMBER


def test_headers_are_matched_case_insensitively():
    # HTTP header names are case-insensitive and different clients send
    # different casings; opencode's own config block writes them one way and
    # nothing guarantees that is the way they arrive.
    ctx = _Ctx({"x-alfred-user": MEMBER, "X-ALFRED-TOKEN": TOKEN})
    assert authenticate(ctx, MEMBER, TOKEN) == MEMBER


@pytest.mark.parametrize("headers", [
    {},
    {"X-Alfred-User": MEMBER},
    {"X-Alfred-Token": TOKEN},
    {"X-Alfred-User": "", "X-Alfred-Token": ""},
])
def test_a_call_with_no_identity_is_refused(headers):
    with pytest.raises(Denied):
        authenticate(_Ctx(headers), MEMBER, TOKEN)


def test_a_wrong_token_is_refused():
    ctx = _Ctx({"X-Alfred-User": MEMBER, "X-Alfred-Token": "guess"})
    with pytest.raises(Denied):
        authenticate(ctx, MEMBER, TOKEN)


def test_a_right_token_under_another_name_is_refused():
    """One container serves one person.

    A correct token presented for a different member is a deployment that has
    gone wrong, and serving it would mean this process acting for somebody
    whose credentials it does not hold.
    """
    ctx = _Ctx({"X-Alfred-User": "user2", "X-Alfred-Token": TOKEN})
    with pytest.raises(Denied):
        authenticate(ctx, MEMBER, TOKEN)


def test_the_refusal_does_not_say_which_half_was_wrong():
    """A caller probing this learns nothing about what it got right."""
    wrong_token = _Ctx({"X-Alfred-User": MEMBER, "X-Alfred-Token": "guess"})
    wrong_user = _Ctx({"X-Alfred-User": "user2", "X-Alfred-Token": TOKEN})
    with pytest.raises(Denied) as a:
        authenticate(wrong_token, MEMBER, TOKEN)
    with pytest.raises(Denied) as b:
        authenticate(wrong_user, MEMBER, TOKEN)
    assert str(a.value) == str(b.value)


def test_no_http_request_is_not_authenticated():
    """Over stdio there is no request. This server is only ever run over HTTP,
    so no request means something is calling it in a way it was not built for.
    """
    with pytest.raises(Denied):
        authenticate(_Ctx(with_request=False), MEMBER, TOKEN)


def test_no_tool_takes_an_identity_argument():
    """The rule, asserted rather than only written down.

    Adding `user` to a tool signature is exactly how this stops being true, and
    it would look perfectly reasonable in review.
    """
    import asyncio

    from alfred_mcp.server import mcp

    forbidden = {"user", "username", "member", "login", "as_user", "on_behalf_of",
                 "folder", "token"}
    offenders = {}
    for spec in asyncio.run(mcp.list_tools()):
        args = set((spec.inputSchema or {}).get("properties") or {})
        if args & forbidden:
            offenders[spec.name] = sorted(args & forbidden)
    assert not offenders, (
        f"these tools let the caller name who it is: {offenders}. "
        f"Identity is the header; see alfred_mcp/identity.py.")

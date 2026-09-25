"""The debug log holds the agent's commands and their output — the most
sensitive thing this process has, and it was readable by anyone who could reach
the port.

That is not hypothetical. A Paperless API token reached /v1/debug/shell-log on
a live instance and every token in the house had to be rotated.

Two layers here, because either alone is insufficient. Redaction keeps the
secret out of the buffer whichever way it arrives — inlined into a command, or
echoed back by an API that was asked for it. The gate keeps the buffer from
being read by whoever asks. Redaction without a gate still exposes what the
family's assistant has been doing; a gate without redaction means one
misconfiguration re-opens the original hole.
"""
import os
from unittest.mock import patch

import pytest

from nanobot.utils import shell_log


@pytest.fixture(autouse=True)
def _clean_buffer():
    shell_log.clear_execs()
    yield
    shell_log.clear_execs()


# --- redaction -----------------------------------------------------------


TOKEN = "810313570b59b96c797347ec076fc33de6a3d464"


def test_a_token_inlined_into_a_command_is_redacted():
    with patch.dict(os.environ, {"PAPERLESS_API_TOKEN": TOKEN}):
        shell_log.log_exec(
            f'curl -H "Authorization: Token {TOKEN}" http://paperless.home:8000/api/',
            0, "{}", "",
        )
    entry = shell_log.get_execs()[0]
    assert TOKEN not in entry["command"]
    assert "[redacted:PAPERLESS_API_TOKEN]" in entry["command"]


def test_a_token_echoed_back_in_output_is_redacted():
    """The likelier path: the command is clean but the response carries the
    secret — /api/profile/ returns auth_token, and `env` prints everything."""
    with patch.dict(os.environ, {"PAPERLESS_API_TOKEN": TOKEN}):
        shell_log.log_exec(
            "curl -s http://paperless.home:8000/api/profile/",
            0, f'{{"auth_token": "{TOKEN}"}}', "",
        )
    entry = shell_log.get_execs()[0]
    assert TOKEN not in entry["output"]


def test_a_secret_on_stderr_is_redacted():
    with patch.dict(os.environ, {"NTFY_CREDENTIALS": "user1:hunter2hunter2"}):
        shell_log.log_exec("curl ...", 1, "", "failed for user1:hunter2hunter2")
    assert "hunter2hunter2" not in shell_log.get_execs()[0]["output"]


@pytest.mark.parametrize("name", [
    "PAPERLESS_API_TOKEN", "OPENCODE_API_KEY", "HOMECORE_PROXY_TOKEN",
    "NTFY_CREDENTIALS", "SMB_PASSWORD", "BRIGHTDATA_API_TOKEN",
    "NANOBOT_DEBUG_SECRET",
])
def test_every_credential_shaped_name_is_covered(name):
    """Matched on the variable name so a credential added later is protected
    without anyone remembering to update this module."""
    secret = "s3cret-value-long-enough"
    with patch.dict(os.environ, {name: secret}):
        shell_log.log_exec(f"echo {secret}", 0, secret, "")
    entry = shell_log.get_execs()[0]
    assert secret not in entry["command"] and secret not in entry["output"]


def test_ordinary_output_survives():
    """Redaction that eats real output makes the log useless and people turn it
    off, which is worse than the problem."""
    with patch.dict(os.environ, {"PAPERLESS_API_TOKEN": TOKEN}):
        shell_log.log_exec("ls -la /media", 0, "total 8\ncam_patio_1785.jpg", "")
    entry = shell_log.get_execs()[0]
    assert entry["command"] == "ls -la /media"
    assert "cam_patio_1785.jpg" in entry["output"]


def test_short_env_values_are_not_scrubbed():
    """A PORT of "8000" is not a secret, and replacing every 8000 in the output
    would corrupt it."""
    with patch.dict(os.environ, {"SOME_KEY": "8000"}):
        shell_log.log_exec("curl http://host:8000/api", 0, "listening on 8000", "")
    entry = shell_log.get_execs()[0]
    assert "8000" in entry["command"] and "8000" in entry["output"]


def test_longest_secret_is_replaced_first():
    """A token that contains a shorter one as a prefix must not be half-scrubbed
    into something still recognisable."""
    short, long = "abcdefghijkl", "abcdefghijklMNOPQRSTUV"
    with patch.dict(os.environ, {"A_TOKEN": short, "B_TOKEN": long}):
        shell_log.log_exec(f"echo {long}", 0, "", "")
    command = shell_log.get_execs()[0]["command"]
    assert long not in command and "MNOPQRSTUV" not in command


# --- the gate ------------------------------------------------------------


def _request(headers=None):
    class _Req:
        def __init__(self, h):
            self.headers = h or {}
            self.remote = "192.168.1.99"   # logged on rejection
    return _Req(headers)


def _denied(headers=None):
    from nanobot.api.server import _debug_auth_error
    return _debug_auth_error(_request(headers))


def test_disabled_when_no_secret_is_configured():
    """Closed by default. The previous default was open, and that is what
    leaked the token."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NANOBOT_DEBUG_SECRET", None)
        denied = _denied()
    assert denied is not None and denied.status == 403


def test_rejects_a_wrong_secret():
    with patch.dict(os.environ, {"NANOBOT_DEBUG_SECRET": "right"}):
        denied = _denied({"X-Debug-Secret": "wrong"})
    assert denied is not None and denied.status == 401


def test_rejects_no_header_when_enabled():
    with patch.dict(os.environ, {"NANOBOT_DEBUG_SECRET": "right"}):
        denied = _denied()
    assert denied is not None and denied.status == 401


@pytest.mark.parametrize("headers", [
    {"X-Debug-Secret": "right"},
    {"Authorization": "Bearer right"},
    {"Authorization": "bearer right"},
])
def test_accepts_the_secret_either_way(headers):
    with patch.dict(os.environ, {"NANOBOT_DEBUG_SECRET": "right"}):
        assert _denied(headers) is None


# --- the chat API gate ---------------------------------------------------
#
# /v1/chat/completions drives a specific family member's assistant: it can send
# messages as them, run their skills and read their history. It was reachable
# by anything on the LAN.


def _api_denied(headers=None):
    from nanobot.api.server import _api_auth_error
    return _api_auth_error(_request(headers))


def test_chat_api_stays_open_when_no_secret_is_set():
    """Opposite default to the debug gate, deliberately. An unset debug secret
    costs an operator a diagnostic; an unset secret here would take the family's
    assistant away with no warning and no way for them to fix it."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NANOBOT_API_SECRET", None)
        assert _api_denied() is None


def test_chat_api_rejects_a_missing_credential_once_configured():
    with patch.dict(os.environ, {"NANOBOT_API_SECRET": "right"}):
        denied = _api_denied()
    assert denied is not None and denied.status == 401


def test_chat_api_rejects_a_wrong_credential():
    with patch.dict(os.environ, {"NANOBOT_API_SECRET": "right"}):
        denied = _api_denied({"Authorization": "Bearer wrong"})
    assert denied is not None and denied.status == 401


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer right"},
    {"X-Nanobot-Auth": "right"},
])
def test_chat_api_accepts_the_credential(headers):
    """Bearer is what HomeCore sends; X-Nanobot-Auth matches the header the
    websocket channel already documents, so operators have one convention."""
    with patch.dict(os.environ, {"NANOBOT_API_SECRET": "right"}):
        assert _api_denied(headers) is None


def test_the_two_gates_use_different_secrets():
    """A debug secret must not unlock the chat API or vice versa — they protect
    different things and are rotated by different people."""
    with patch.dict(os.environ, {"NANOBOT_API_SECRET": "api-secret",
                                 "NANOBOT_DEBUG_SECRET": "debug-secret"}):
        assert _api_denied({"Authorization": "Bearer debug-secret"}) is not None
        assert _denied({"X-Debug-Secret": "api-secret"}) is not None


# --- the workspace gate --------------------------------------------------
#
# /v1/workspace/* stayed open while the two doors either side of it were shut.
# `list` shows media, but `files/{path}` served any path under the workspace —
# cron/jobs.json, memory/, and sessions/*.jsonl, which hold whole conversations
# including the agent's reasoning. One unauthenticated GET from anywhere on the
# LAN. Found on a live instance on 2026-08-03.


def _ws_denied(headers=None):
    from nanobot.api.server import _workspace_auth_error
    return _workspace_auth_error(_request(headers))


def test_workspace_is_open_only_when_no_credential_exists_at_all():
    """Same degradation as the chat API: a deployment that has configured
    nothing keeps its photos rather than losing them with no way to fix it."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NANOBOT_API_SECRET", None)
        os.environ.pop("NANOBOT_DEBUG_SECRET", None)
        assert _ws_denied() is None


def test_workspace_rejects_a_missing_credential_once_configured():
    with patch.dict(os.environ, {"NANOBOT_API_SECRET": "api-secret"}):
        os.environ.pop("NANOBOT_DEBUG_SECRET", None)
        denied = _ws_denied()
    assert denied is not None and denied.status == 401


def test_workspace_closes_when_only_the_debug_secret_is_set():
    """Either secret existing means somebody has thought about credentials, so
    an unauthenticated read is no longer the configured intent."""
    with patch.dict(os.environ, {"NANOBOT_DEBUG_SECRET": "debug-secret"}):
        os.environ.pop("NANOBOT_API_SECRET", None)
        denied = _ws_denied()
    assert denied is not None and denied.status == 401


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer api-secret"},     # what HomeCore sends
    {"X-Nanobot-Auth": "api-secret"},
    {"X-Debug-Secret": "debug-secret"},         # an operator's diagnostic
    {"Authorization": "Bearer debug-secret"},
])
def test_workspace_accepts_either_credential(headers):
    """Two callers, two credentials. The debug secret already unlocks strictly
    more than this, so refusing it here would only make people pass the API
    secret around."""
    with patch.dict(os.environ, {"NANOBOT_API_SECRET": "api-secret",
                                 "NANOBOT_DEBUG_SECRET": "debug-secret"}):
        assert _ws_denied(headers) is None


def test_workspace_rejects_a_wrong_credential():
    with patch.dict(os.environ, {"NANOBOT_API_SECRET": "api-secret",
                                 "NANOBOT_DEBUG_SECRET": "debug-secret"}):
        denied = _ws_denied({"Authorization": "Bearer neither-of-them"})
    assert denied is not None and denied.status == 401


# --- and the path it resolves --------------------------------------------


class _Loop:
    def __init__(self, workspace):
        self.workspace = workspace


def _resolved(workspace, path):
    from nanobot.api.server import _workspace_path
    return _workspace_path(_Loop(workspace), path)


def test_an_ordinary_workspace_file_resolves(tmp_path):
    (tmp_path / "media").mkdir()
    f = tmp_path / "media" / "cam_patio.jpg"
    f.write_bytes(b"jpeg")
    assert _resolved(tmp_path, "media/cam_patio.jpg") == f.resolve()


def test_an_absolute_path_does_not_escape(tmp_path):
    """The old guard was `".." in file_path`, which reads like a traversal
    check and is not one: joining an absolute path REPLACES the base, so
    GET /v1/workspace/files//etc/hostname walked out of the workspace and
    returned the file. Confirmed against a live instance."""
    assert _resolved(tmp_path, "/etc/hostname") is None
    assert _resolved(tmp_path, "/proc/self/environ") is None


def test_dot_dot_does_not_escape(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    assert _resolved(tmp_path / "ws", "media/../../outside.txt") is None


def test_a_symlink_pointing_out_of_the_workspace_does_not_escape(tmp_path):
    ws = tmp_path / "ws"
    (ws / "media").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = ws / "media" / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this platform")
    assert _resolved(ws, "media/link.txt") is None


def test_the_workspace_root_itself_is_not_a_file(tmp_path):
    assert _resolved(tmp_path, "") is None
    assert _resolved(tmp_path, ".") is None


# --- the profile endpoint ------------------------------------------------
#
# It reports what the family asked, how long it took and what it cost — less
# dangerous than the shell log and still nobody's business but the operator's.
# It goes behind the same gate, and its panel is deliberately *not* behind it,
# because the panel contains nothing: the secret rides the URL fragment, which
# browsers never send to a server, and the page uses it as a header on its own
# fetch. That distinction is the thing worth a test — an operator who reads
# "the panel is open" and assumes the data is too would be wrong.


def _query(**params):
    class _Req:
        def __init__(self):
            self.headers = {}
            self.remote = "192.168.1.99"
            self.query = params
    return _Req()


@pytest.mark.asyncio
async def test_the_profile_is_behind_the_same_gate_as_the_shell_log():
    from nanobot.api.server import handle_debug_profile

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NANOBOT_DEBUG_SECRET", None)
        denied = await handle_debug_profile(_query())
    assert denied.status == 403

    with patch.dict(os.environ, {"NANOBOT_DEBUG_SECRET": "right"}):
        denied = await handle_debug_profile(_query())
    assert denied.status == 401


@pytest.mark.asyncio
async def test_the_panel_itself_carries_no_data_and_no_secret():
    from nanobot.api.server import handle_debug_profile_panel

    with patch.dict(os.environ, {"NANOBOT_DEBUG_SECRET": "s3cr3t"}):
        page = await handle_debug_profile_panel(_query())

    assert page.status == 200
    assert page.content_type == "text/html"
    body = page.text
    assert "s3cr3t" not in body, "the page must never carry the secret"
    assert "location.hash" in body, "it reads it from the fragment instead"
    assert "X-Debug-Secret" in body, "and sends it as a header on its own fetch"


@pytest.mark.asyncio
async def test_the_profile_answers_with_the_secret():
    from nanobot.api.server import handle_debug_profile
    from nanobot.utils.profiling import PROFILER

    PROFILER.clear()
    with PROFILER.span("turn", "websocket:test"):
        pass
    with patch.dict(os.environ, {"NANOBOT_DEBUG_SECRET": "right"}):
        req = _query()
        req.headers = {"X-Debug-Secret": "right"}
        resp = await handle_debug_profile(req)

    assert resp.status == 200
    import json
    body = json.loads(resp.text)
    assert body["counts"]["spans"] == 1
    assert body["recent_spans"][0]["session_key"] == "websocket:test"
    PROFILER.clear()

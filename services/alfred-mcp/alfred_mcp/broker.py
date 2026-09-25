"""A client for the code broker, and nothing more than a client.

Every decision worth making about a project -- may this person see it, where is
it checked out, which paths are protected, whose name goes on the commit, which
credential pushes it -- is the broker's, and none of it is re-implemented here.
This translates an MCP tool call into one HTTP request and its answer back into
something a model can read.

That is deliberate to the point of being the design. opencode has `bash`, and
`bash` has `git`; if the agent commits through its own shell then the access
list, the protected paths and the co-author line are all gone and *nothing
fails* -- the commit lands, with the wrong identity, possibly in a repo the
person was never entitled to. These verbs exist so the agent never needs to.
"""

from __future__ import annotations

import httpx

_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

# Key names whose value must never reach the model. Everything the broker
# answers is copied verbatim into a conversation, and a conversation is stored,
# summarised, and shown to a person -- so a credential that appears in one is
# not "logged", it is published.
#
# Nothing the broker returns today carries one: `auth_secret` is read from the
# registry and handed straight to `Credential`, and the `project` object echoed
# back is its sibling rather than its parent. This is insurance against that
# staying true. The registry gains a field, or the broker widens what it echoes,
# and the leak arrives here with nothing in between having visibly changed.
_NEVER_RETURN = ("secret", "token", "password", "passphrase", "private_key",
                 "api_key")


def scrub(value):
    """The broker's answer with anything credential-shaped removed.

    Matched on the key's *name* and recursively, never on the value: a string
    that looks like a hash is not necessarily a secret, and a secret does not
    necessarily look like one. The key is replaced rather than dropped, so the
    shape of the answer is unchanged and the removal is visible instead of
    looking like a field the broker did not send.
    """
    if isinstance(value, dict):
        return {k: ("(removed)" if any(n in k.lower() for n in _NEVER_RETURN)
                    else scrub(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def retarget(value, frm: str, to: str):
    """Rewrite the broker's paths into the ones the agent can actually open.

    Only the values under a key named `path`, and only when the prefix matches:
    a slug, a branch name or a commit message that happens to contain the root
    is not a path and must survive untouched.

    The trailing separator matters. Without it `/workspace` also matches
    `/workspace-backup`, which would be rewritten into a directory that is not
    the one it names.
    """
    if not frm or not to or frm == to:
        return value
    frm, to = frm.rstrip("/"), to.rstrip("/")
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "path" and isinstance(v, str) and (
                    v == frm or v.startswith(frm + "/")):
                out[k] = to + v[len(frm):]
            else:
                out[k] = retarget(v, frm, to)
        return out
    if isinstance(value, list):
        return [retarget(v, frm, to) for v in value]
    return value


class BrokerDown(Exception):
    """The broker could not be reached or answered something unusable."""


class Broker:
    def __init__(self, base_url: str, member: str, token: str,
                 workspace_broker: str = "", workspace_agent: str = "") -> None:
        self._base = base_url.rstrip("/")
        self._member = member
        self._headers = {"X-Code-User": member, "X-Code-Token": token}
        # See config.Settings: the broker names the checkout from inside a
        # container, the agent opens it from the host.
        self._ws_from = workspace_broker
        self._ws_to = workspace_agent

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self._base}{path}"
        try:
            with httpx.Client(timeout=_TIMEOUT) as http:
                resp = http.request(method, url, headers=self._headers, json=body)
        except Exception as exc:                                  # noqa: BLE001
            # Unreachable is not the same as refused, and the difference is
            # what the agent should do next: one is worth saying out loud and
            # waiting on, the other never is. The broker draws the same line
            # between its registry being down and a project not existing.
            raise BrokerDown(
                f"the code broker did not answer ({type(exc).__name__}). "
                f"It runs beside the assistants; if it is down, every git verb "
                f"here will refuse and the work should stop rather than be "
                f"done another way.") from exc

        if resp.status_code == 404:
            # The broker answers 404 both for a project that does not exist and
            # for one this person may not see, on purpose, so a leaked token
            # cannot enumerate somebody else's projects a slug at a time. The
            # message repeats that ambiguity rather than resolving it.
            raise BrokerDown(
                "no such project, or not one this account may open. "
                "`list_projects` shows what is available.")
        if resp.status_code == 401:
            raise BrokerDown(
                "the code broker refused this container's token. That is a "
                "deployment problem, not something to work around.")
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = (resp.json() or {}).get("error") or ""
            except Exception:                                     # noqa: BLE001
                detail = (resp.text or "")[:300]
            raise BrokerDown(f"the code broker said {resp.status_code}: {detail}")

        try:
            return retarget(scrub(resp.json() or {}),
                            self._ws_from, self._ws_to)
        except Exception as exc:                                  # noqa: BLE001
            raise BrokerDown("the code broker's answer was not JSON") from exc

    # --- the verbs ----------------------------------------------------------

    def list_projects(self) -> dict:
        return self._call("GET", "/v1/projects")

    def checkout(self, slug: str) -> dict:
        return self._call("POST", f"/v1/projects/{slug}/checkout")

    def status(self, slug: str) -> dict:
        return self._call("GET", f"/v1/projects/{slug}/status")

    def branch(self, slug: str, name: str) -> dict:
        return self._call("POST", f"/v1/projects/{slug}/branch", {"name": name})

    def commit(self, slug: str, message: str) -> dict:
        return self._call("POST", f"/v1/projects/{slug}/commit",
                          {"message": message})

    def push(self, slug: str) -> dict:
        return self._call("POST", f"/v1/projects/{slug}/push")

    def merge_branch(self, slug: str) -> dict:
        return self._call("POST", f"/v1/projects/{slug}/merge")

    def pull(self, slug: str) -> dict:
        return self._call("POST", f"/v1/projects/{slug}/pull")

    def deploy_script(self, slug: str) -> dict:
        return self._call("GET", f"/v1/projects/{slug}/deploy-script")

    def deploy(self, slug: str) -> dict:
        return self._call("POST", f"/v1/projects/{slug}/deploy")

    def health(self) -> dict:
        return self._call("GET", "/health")

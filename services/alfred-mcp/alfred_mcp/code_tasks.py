"""Handing a coding job to opencode, and asking later how it went.

The Programmer answers questions itself. *Doing* the work -- reading a repo,
editing it, running the tests, committing -- is opencode's, and this is how the
one reaches the other.

It hands off rather than waiting. A refactor takes minutes and a person is
sitting in a chat, so a tool call that blocked until the work finished would
look exactly like the assistant having hung. The portal registers the job as a
background task -- the same table, panel and "done" notification the assistant's
own subagents use -- so it can be watched while it runs and is announced in the
chat when it lands.

Not a direct call to opencode, and that is not indirection for its own sake:
opencode listens on the host's loopback and this container is on a bridge
network that cannot reach it. The portal is host-networked, so it is the only
part of the stack that can dial opencode at all.
"""

from __future__ import annotations

import httpx

# Starting a job is a registration, not the work: the portal answers as soon as
# the task is recorded and the thread is running, so this is a short wait. If it
# ever becomes a long one, something is wrong at the other end and saying so
# beats hanging the conversation.
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class CodeTaskError(Exception):
    """The job was refused, or the portal could not be reached."""


class CodeTasks:
    def __init__(self, base_url: str, login: str, token: str,
                 verify_tls: bool = False) -> None:
        self._base = base_url.rstrip("/")
        self._verify = verify_tls
        # The login, not the member id: this is the header the portal resolves a
        # session from, and the opencode server it then reaches is the one
        # configured for that person. Which is the whole access story here --
        # that server holds one member's checkout and one member's projects, so
        # a tool call must not be able to name a different person.
        self._headers = {"X-Proxy-User": login, "X-Proxy-Secret": token}

    def _call(self, method: str, path: str, **kw) -> dict:
        try:
            with httpx.Client(timeout=_TIMEOUT, verify=self._verify) as http:
                resp = http.request(method, f"{self._base}{path}",
                                    headers=self._headers, **kw)
        except Exception as exc:                                  # noqa: BLE001
            raise CodeTaskError(
                f"the portal did not answer ({type(exc).__name__})") from exc
        if resp.status_code == 409:
            # This member has no opencode server -- the Programmer profession is
            # switched off for them. A refusal the model can act on: answer the
            # question yourself rather than reporting a broken tool.
            raise CodeTaskError(
                "there is no coding server for this account, so this job cannot "
                "be handed off -- answer from what you can read instead.")
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = (resp.json() or {}).get("error") or ""
            except Exception:                                     # noqa: BLE001
                detail = (resp.text or "")[:300]
            raise CodeTaskError(f"the portal said {resp.status_code}: {detail}")
        try:
            return resp.json() or {}
        except Exception as exc:                                  # noqa: BLE001
            raise CodeTaskError("the portal answered with something that is "
                                "not JSON") from exc

    def start(self, task: str, chat_id: str = "", label: str = "") -> dict:
        return self._call("POST", "/projects/api/code/run",
                          json={"task": task, "chat_id": chat_id,
                                "label": label})

    def set_deploy_script(self, slug: str, script: str) -> dict:
        return self._call("POST", f"/projects/api/code/deploy-script/{slug}",
                          json={"deploy_script": script})

    def status(self, task_id: str) -> dict:
        return self._call("GET", f"/projects/api/code/status/{task_id}")

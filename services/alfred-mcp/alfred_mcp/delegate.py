"""Handing a visual piece to the Designer.

The Programmer does not make diagrams, charts, posters or pages, even when it
knows the HTML. The Designer has different rules, different judgement and a
different model, and a piece by one sitting next to a piece by the other shows.
So the Programmer writes the commission and somebody else makes it.

The brief is all the Designer sees. They do not get this conversation, they do
not know who asked or what for, and there is no back and forth -- so a thin
brief comes back as a thin piece. That is worth saying in the tool description
rather than only here, because the model reads one and not the other.
"""

from __future__ import annotations

import httpx

# The portal answers inline when the delegate finishes quickly -- the common
# case for a chart -- and otherwise keeps going and delivers into the caller's
# conversation on its own. So this waits a while but not forever, and says
# which of the two happened.
_TIMEOUT = httpx.Timeout(240.0, connect=10.0)


class DelegateError(Exception):
    """The delegation was refused, or the portal could not be reached."""


class Delegate:
    def __init__(self, base_url: str, login: str, token: str,
                 verify_tls: bool = False) -> None:
        self._base = base_url.rstrip("/")
        self._verify = verify_tls
        self._headers = {"X-Proxy-User": login, "X-Proxy-Secret": token}

    def to(self, profession: str, brief: str) -> dict:
        try:
            with httpx.Client(timeout=_TIMEOUT, verify=self._verify) as http:
                resp = http.post(f"{self._base}/chat/api/delegate",
                                 headers=self._headers,
                                 json={"to": profession, "brief": brief,
                                       "from": "programmer"})
        except Exception as exc:                                  # noqa: BLE001
            raise DelegateError(
                f"the portal did not answer ({type(exc).__name__})") from exc

        if resp.status_code == 409:
            # One delegation at a time per person. This is what stops a
            # delegate delegating back, and it is also what keeps one question
            # from fanning out into several turns on a small box.
            raise DelegateError(
                "there is already a delegation running for this account -- "
                "answer with what you have rather than asking again.")
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = (resp.json() or {}).get("error") or ""
            except Exception:                                     # noqa: BLE001
                detail = (resp.text or "")[:300]
            raise DelegateError(f"the portal said {resp.status_code}: {detail}")
        try:
            return resp.json() or {}
        except Exception as exc:                                  # noqa: BLE001
            raise DelegateError("the portal's answer was not JSON") from exc

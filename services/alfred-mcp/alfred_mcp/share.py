"""Putting a file where the person can actually open it.

A file inside opencode's own container -- /tmp/report.md, a path in a checkout
-- is a file nobody else can reach. The share is where something handed over
lives: in the person's own folder, under `alfred/`, in the "My files" panel,
backed up, and there when nothing is running.

The link matters as much as the upload. A `download:` link does not fail when
it is written; it fails when somebody clicks it, and from the agent's side that
looks exactly like success. So the link is *returned by the call that made the
file* and never assembled from memory -- the same rule the file-share skill
states for the assistant.
"""

from __future__ import annotations

import posixpath
import re

import httpx

_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

# Where what Alfred makes for somebody is filed inside their own folder.
# HomeCore's chat lists exactly this as "My files".
OWN_SUBFOLDER = "alfred"


class ShareError(Exception):
    """The share refused, or could not be reached."""


def _safe_name(name: str) -> str:
    """One path segment, no traversal, no separators."""
    name = posixpath.basename((name or "").replace("\\", "/")).strip()
    if not name or name in (".", ".."):
        raise ShareError("that is not a usable file name")
    return name


def download_link(rel: str) -> str:
    """The markdown link that goes in the chat, for a share-relative path.

    Angle brackets only when the name needs them: a name carrying brackets ends
    the reader's markdown URL at the first ")", so "Factura (1).pdf" linked to
    ".../Factura (1" and died on click with a stray ".pdf)" printed after it.
    Most names have none and the plain form is what everything already reads.
    """
    name = rel.split("/")[-1]
    target = f"download:{rel}"
    if re.search(r"[()\[\]<>]", rel):
        return f"[{name}](<{target}>)"
    return f"[{name}]({target})"


class Share:
    def __init__(self, base_url: str, login: str, token: str, folder: str,
                 verify_tls: bool = False) -> None:
        self._base = base_url.rstrip("/")
        self._folder = folder
        self._verify = verify_tls
        # The login, not the member id and not the folder. This is the header
        # HomeCore resolves a session from, and it is the one a request is
        # keyed on -- a table keyed on the wrong one of the three does not
        # fail, it answers nothing.
        self._headers = {"X-Proxy-User": login, "X-Proxy-Secret": token}

    def _post(self, path: str, **kw) -> dict:
        url = f"{self._base}{path}"
        try:
            with httpx.Client(timeout=_TIMEOUT, verify=self._verify) as http:
                resp = http.post(url, headers=self._headers, **kw)
        except Exception as exc:                                  # noqa: BLE001
            raise ShareError(
                f"the file share did not answer ({type(exc).__name__})") from exc
        if resp.status_code == 403:
            raise ShareError("the share refused that path for this account")
        if resp.status_code >= 400:
            raise ShareError(f"the share said {resp.status_code}")
        try:
            return resp.json() or {}
        except Exception as exc:                                  # noqa: BLE001
            raise ShareError("the share's answer was not JSON") from exc

    def save_bytes(self, name: str, data: bytes, subdir: str = "") -> str:
        """Write bytes into the person's `alfred/` folder; return the link.

        `subdir` is optional and relative to `alfred/`. Everything is anchored
        under the caller's own folder here rather than taken as a free path:
        HomeCore checks access again on its side, but a tool that accepted an
        arbitrary share path would be one prompt away from writing into the
        family folder or somebody else's.
        """
        name = _safe_name(name)
        parts = [self._folder, OWN_SUBFOLDER]
        for seg in (subdir or "").replace("\\", "/").split("/"):
            seg = seg.strip()
            if not seg or seg in (".", ".."):
                continue
            parts.append(seg)
        rel_dir = "/".join(parts)

        body = self._post("/files/api/upload",
                          data={"path": rel_dir},
                          files={"files": (name, data)})
        if not body.get("ok") or name not in (body.get("saved") or []):
            raise ShareError(
                f"the share did not save {name!r} "
                f"(saved={body.get('saved')}, errors={body.get('errors')})")
        return download_link(f"{rel_dir}/{name}")

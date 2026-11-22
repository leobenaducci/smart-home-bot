"""HomeCore keeps a homeweb conversation; the socket is only the fast path.

Port of the fork's ``WebSocketChannel.send`` durability branch onto v0.4.1's
event-typed outbound path (``webui/outbound_delivery.py``). Upstream fans a
message out to whoever is attached and stores a WebUI transcript; it has no
notion of an external owner of the history. For a ``homeweb:`` chat the
household portal is that owner, so every *answer* is posted to it whether or
not a socket is attached (HomeCore dedupes what the page already stored).

What counts as an answer here, re-expressed in upstream's terms:

- ``event is None`` (a plain reply) or ``StreamedResponseEvent`` (the final
  text of a reply that was streamed; upstream sends it whole at the end);
- never a ``ProgressEvent`` (tool hints, reasoning, progress): trace, not
  conversation -- the fork's ``_tool_hint``/``_progress`` metadata flags no
  longer exist, the event type carries that now;
- a ``SubagentTaskChanged`` is relayed as a ``task`` record with its id only.
  The fork relayed per-step progress text (``subagent_status`` frames);
  upstream's task projection has no such stream, so the panel loses detail.
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import ProgressEvent, StreamedResponseEvent
from nanobot.bus.runtime_events import SubagentTaskChanged
from nanobot.channels.homeweb_relay import HOMEWEB_CHAT_PREFIX

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def homeweb_media_links(paths: list[str] | None, workspace: Path) -> str:
    """Markdown for files sent to a HomeCore chat (copied into ``media/``)."""
    ws = workspace.resolve()
    out = []
    for raw in paths or []:
        src = Path(str(raw))
        if not src.is_file():
            continue
        try:
            rel: Path | None = src.resolve().relative_to(ws)
        except ValueError:
            rel = None
        if rel is None or not rel.parts or rel.parts[0] != "media":
            media = ws / "media"
            media.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", src.name)[:80] or "file"
            dest = media / f"{uuid.uuid4().hex[:8]}-{safe}"
            shutil.copy2(src, dest)
            rel = dest.relative_to(ws)
        path = rel.as_posix()
        label = src.stem.replace("_", " ")[:60]
        out.append(f"![{label}]({path})" if src.suffix.lower() in _IMAGE_EXT
                   else f"[{src.name}](download:{path})")
    return "\n\n".join(out)


def homeweb_record(msg: OutboundMessage, workspace: Path | None) -> dict[str, Any] | None:
    """The record HomeCore should keep for *msg*, or None."""
    if not isinstance(msg.chat_id, str) or not msg.chat_id.startswith(HOMEWEB_CHAT_PREFIX):
        return None
    event = msg.event
    if isinstance(event, SubagentTaskChanged):
        return {"type": "task", "chat_id": msg.chat_id, "event": "changed",
                "task_id": event.task_id}
    if isinstance(event, ProgressEvent):
        return None
    if event is not None and not isinstance(event, StreamedResponseEvent):
        return None
    content = (msg.content or "").strip()
    if msg.media and workspace is not None:
        links = homeweb_media_links(msg.media, workspace)
        if links:
            content = f"{content}\n\n{links}" if content else links
    if not content:
        return None
    record: dict[str, Any] = {"type": "message", "chat_id": msg.chat_id, "text": content}
    if msg.metadata.get("reminder_job"):
        record["reminder"] = {
            "job": msg.metadata["reminder_job"],
            "name": msg.metadata.get("reminder_name") or "",
            "recurring": bool(msg.metadata.get("reminder_recurring")),
        }
    return record

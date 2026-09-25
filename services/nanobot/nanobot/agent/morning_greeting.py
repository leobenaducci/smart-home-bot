"""The daily good-morning message an instance sends to its own person.

Not a cron job somebody typed into a chat. This house tried that: the job
stored the chat_id that was current the day it was created, so it kept posting
into that day forever; its instruction ended with a receipt ("saludos enviados
a todos") that the cron delivery gate then suppressed as routine; and it lived
on one instance, which could only ever write into one person's history. Every
one of those is a property of *where the thing was defined*, so it is defined
here instead — registered by the gateway on every instance at startup, exactly
like Dream.

What that buys, point by point:

- **The target is built at fire time**, from ``HOMECORE_USER_ID`` (the same
  variable the HomeCore relay authenticates with) and today's date in the
  house's timezone. There is no stored chat_id, so there is nothing to go
  stale.
- **Each firing names a new conversation** (``homeweb:<user>:<day>:<now ms>``),
  and the turn runs in *that conversation's* session — ``websocket:<chat_id>``,
  the very key HomeCore uses when the person types a reply. So "thanks, what
  tengo hoy?" continues the same conversation instead of talking to an agent
  that has never heard of the greeting.
- **The response is the message.** It is published as-is, with no notification
  gate in front of it: a job whose entire purpose is to say good morning has
  nothing to evaluate. The gate exists for jobs that may have nothing to
  report, and it is what made the previous attempt invisible.
- **The wording lives in ``MORNING.md``**, symlinked into the workspace from
  the read-only shared config. Rewording the greeting is an edit to that file
  and applies on the next firing — no rebuild, no restart. A missing or empty
  file means no greeting at all, and says so in the log; inventing a default
  would put words in Alfred's mouth that nobody wrote.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from loguru import logger

from nanobot.utils.homeweb_chat_id import new_conversation_chat_id

JOB_ID = "morning-greeting"
PROMPT_FILE = "MORNING.md"
# The conversation belongs to the chat page, so it is delivered the way every
# other HomeCore message is: the websocket channel, which hands anything with no
# live subscriber to HomeCore's /chat/agent-event (see channels/homeweb_relay).
CHANNEL = "websocket"


class MorningGreeting:
    """Compose the day's greeting and deliver it into the person's own chat."""

    def __init__(
        self,
        workspace: Path,
        timezone: str,
        user_id: str,
        run_turn: Callable[..., Awaitable[Any]],
        publish: Callable[[str, str], Awaitable[None]],
    ) -> None:
        self.workspace = Path(workspace)
        self.timezone = timezone
        self.user_id = (user_id or "").strip()
        self._run_turn = run_turn
        self._publish = publish

    @property
    def addressable(self) -> bool:
        """Whether this instance knows whose chat to write into."""
        return bool(self.user_id)

    def prompt(self) -> str:
        try:
            return (self.workspace / PROMPT_FILE).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
        except OSError as e:
            logger.warning("morning greeting: cannot read {}: {}", PROMPT_FILE, e)
            return ""

    def _today(self) -> str:
        try:
            now = datetime.now(ZoneInfo(self.timezone)) if self.timezone else datetime.now()
        except Exception:
            # A missing tzdata entry must not stop the greeting; the schedule
            # itself is what decides the hour, and this only names the day.
            now = datetime.now()
        return now.strftime("%Y-%m-%d")

    async def run(self, now_ms: int | None = None) -> str | None:
        """Run one firing. Returns the delivered text, or None if nothing was."""
        if not self.addressable:
            logger.warning(
                "morning greeting: no HOMECORE_USER_ID, so there is no chat to "
                "write into — skipping"
            )
            return None
        prompt = self.prompt()
        if not prompt:
            logger.warning(
                "morning greeting: {} is missing or empty, skipping (the wording "
                "lives there on purpose)", PROMPT_FILE,
            )
            return None

        chat_id = new_conversation_chat_id(self.user_id, self.timezone, now_ms=now_ms)
        # The reply the person types lands on websocket:<chat_id> (HomeCore sends
        # channel "websocket" and session_id = chat_id), so the greeting has to
        # run there too or the conversation starts with amnesia.
        session_key = f"{CHANNEL}:{chat_id}"
        response = await self._run_turn(
            prompt, session_key=session_key, channel=CHANNEL, chat_id=chat_id,
        )
        text = ((getattr(response, "content", "") or "") if response else "").strip()
        if not text:
            logger.warning("morning greeting: the turn produced nothing to send")
            return None
        await self._publish(chat_id, text)
        logger.info("morning greeting: delivered to {}", chat_id)
        return text

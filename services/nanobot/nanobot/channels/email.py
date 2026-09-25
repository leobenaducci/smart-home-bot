"""Email channel implementation using IMAP polling + SMTP replies."""

import asyncio
import html
import imaplib
import re
import smtplib
import ssl
from datetime import date
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.email_rules import (
    describe as describe_verdict,
    evaluate as evaluate_rules,
    outbound_permission,
)
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base
from nanobot.utils.helpers import safe_filename


class EmailConfig(Base):
    """Email channel configuration (IMAP inbound + SMTP outbound)."""

    enabled: bool = False
    consent_granted: bool = False

    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    imap_use_ssl: bool = True

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    from_address: str = ""

    auto_reply_enabled: bool = True
    poll_interval_seconds: int = 30
    mark_seen: bool = True
    max_body_chars: int = 12000
    subject_prefix: str = "Re: "
    # Was declared and never read: every message that passed SPF and DKIM
    # became a turn, from anybody. Kept as the coarse allowlist it always
    # looked like — a sender not on it is dropped before any rule runs — and
    # empty still means «no opinion», because that is what every deployment
    # carrying this field already assumes.
    allow_from: list[str] = Field(default_factory=list)

    # Alfred's own addresses. A mailbox is not an address: his is usually an
    # alias on somebody's real account, and a catch-all takes mail for every
    # name at the domain, so «it arrived in the inbox we poll» says nothing
    # about who it was for. Empty keeps the old behaviour — the whole mailbox
    # is his — which is what a single-purpose account wants.
    alfred_addresses: list[str] = Field(default_factory=list)

    # What he may do, per sender. Least privilege: see email_rules. No rule
    # matching is no permission, so a deployment that sets none and leaves
    # `rules_enforced` on gets a channel that reads nothing.
    rules: list[dict] = Field(default_factory=list)
    rules_enforced: bool = False

    # Email authentication verification (anti-spoofing)
    verify_dkim: bool = True   # Require Authentication-Results with dkim=pass
    verify_spf: bool = True    # Require Authentication-Results with spf=pass

    # Attachment handling — set allowed types to enable (e.g. ["application/pdf", "image/*"], or ["*"] for all)
    allowed_attachment_types: list[str] = Field(default_factory=list)
    max_attachment_size: int = 2_000_000  # 2MB per attachment
    max_attachments_per_email: int = 5


class EmailChannel(BaseChannel):
    """
    Email channel.

    Inbound:
    - Poll IMAP mailbox for unread messages.
    - Convert each message into an inbound event.

    Outbound:
    - Send responses via SMTP back to the sender address.
    """

    name = "email"
    display_name = "Email"
    _IMAP_MONTHS = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )
    _IMAP_RECONNECT_MARKERS = (
        "disconnected for inactivity",
        "eof occurred in violation of protocol",
        "socket error",
        "connection reset",
        "broken pipe",
        "bye",
    )
    _IMAP_MISSING_MAILBOX_MARKERS = (
        "mailbox doesn't exist",
        "select failed",
        "no such mailbox",
        "can't open mailbox",
        "does not exist",
    )

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return EmailConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = EmailConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: EmailConfig = config
        self._self_addresses = self._collect_self_addresses()
        self._last_subject_by_chat: dict[str, str] = {}
        self._last_message_id_by_chat: dict[str, str] = {}
        self._processed_uids: set[str] = set()  # Capped to prevent unbounded growth
        # What the rules said about the last message from each sender. Kept so
        # `send` can refuse without re-reading the mailbox, and so a reply is
        # judged by the rules that let the message in rather than by whatever
        # the config says by the time the turn ends.
        self._verdict_by_chat: dict[str, object] = {}
        self._MAX_PROCESSED_UIDS = 100000

    async def start(self) -> None:
        """Start polling IMAP for inbound emails."""
        if not self.config.consent_granted:
            logger.warning(
                "Email channel disabled: consent_granted is false. "
                "Set channels.email.consentGranted=true after explicit user permission."
            )
            return

        if not self._validate_config():
            return

        self._running = True
        if not self.config.verify_dkim and not self.config.verify_spf:
            logger.warning(
                "Email channel: DKIM and SPF verification are both DISABLED. "
                "Emails with spoofed From headers will be accepted. "
                "Set verify_dkim=true and verify_spf=true for anti-spoofing protection."
            )
        logger.info("Starting Email channel (IMAP polling mode)...")

        poll_seconds = max(5, int(self.config.poll_interval_seconds))
        while self._running:
            try:
                inbound_items = await asyncio.to_thread(self._fetch_new_messages)
                for item in inbound_items:
                    sender = item["sender"]
                    subject = item.get("subject", "")
                    message_id = item.get("message_id", "")

                    if subject:
                        self._last_subject_by_chat[sender] = subject
                    if message_id:
                        self._last_message_id_by_chat[sender] = message_id

                    await self._handle_message(
                        sender_id=sender,
                        chat_id=sender,
                        content=item["content"],
                        media=item.get("media") or None,
                        metadata=item.get("metadata", {}),
                    )
            except Exception as e:
                logger.error("Email polling error: {}", e)

            await asyncio.sleep(poll_seconds)

    async def stop(self) -> None:
        """Stop polling loop."""
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        """Send email via SMTP."""
        if not self.config.consent_granted:
            logger.warning("Skip email send: consent_granted is false")
            return

        if not self.config.smtp_host:
            logger.warning("Email channel SMTP host not configured")
            return

        to_addr = msg.chat_id.strip()
        if not to_addr:
            logger.warning("Email channel missing recipient address")
            return

        # Determine if this is a reply (recipient has sent us an email before)
        is_reply = to_addr in self._last_subject_by_chat
        force_send = bool((msg.metadata or {}).get("force_send"))

        # The one place `respond` and `resend` differ, and the only place the
        # difference is decidable: back to whoever wrote is a reply, anywhere
        # else is passing their mail on to a third party — which is a
        # different thing to be allowed to do, and the one worth being strict
        # about. Judged against the verdict that let the message in, so
        # editing the rules mid-turn cannot widen what this reply may do.
        #
        # `force_send` does not bypass it. It exists so a proactive send is
        # not mistaken for an auto-reply, and a permission somebody withheld
        # is not an inconvenience for the agent to route around.
        if self.config.rules_enforced:
            origin = (msg.metadata or {}).get("in_reply_to_sender") or ""
            if not origin:
                origin = next((s for s in self._verdict_by_chat
                               if s == to_addr), "")
            verdict = self._verdict_by_chat.get(origin or to_addr)
            needed = outbound_permission(to_addr, origin or to_addr)
            if verdict is None:
                logger.warning(
                    "Refusing to email {}: no rule ever granted anything for "
                    "this address", to_addr)
                return
            meta = msg.metadata or {}
            if needed == "respond":
                # «Only what the family asks». An email arriving is not a
                # request — a person saying «contéstale» is — and only the send
                # path knows which of the two produced this message. An
                # auto-reply is by definition the first kind, so it is the one
                # `on_request` exists to stop.
                asked_for = bool(meta.get("asked_by") or meta.get("force_send"))
                if not verdict.may_reply(asked_for=asked_for):
                    logger.warning(
                        "Refusing to reply to {}: replies are «{}» and nobody "
                        "asked for this one", to_addr, verdict.replies)
                    return
            elif not verdict.allows(needed):
                logger.warning(
                    "Refusing to {} to {}: {}", needed, to_addr, verdict.reason)
                return
            # Approval is checked last, because it is about the text rather
            # than the permission: everything above decides whether this may be
            # sent at all, and this decides whether *this wording* was seen.
            # The token is the id of the message a person approved, so an
            # approval cannot be reused for a different draft.
            if verdict.confirm and not meta.get("approved_draft"):
                logger.warning(
                    "Refusing to email {}: the rule requires somebody to "
                    "approve the text and nothing did", to_addr)
                return

        # autoReplyEnabled only controls automatic replies, not proactive sends
        if is_reply and not self.config.auto_reply_enabled and not force_send:
            logger.info("Skip automatic email reply to {}: auto_reply_enabled is false", to_addr)
            return

        base_subject = self._last_subject_by_chat.get(to_addr, "nanobot reply")
        subject = self._reply_subject(base_subject)
        if msg.metadata and isinstance(msg.metadata.get("subject"), str):
            override = msg.metadata["subject"].strip()
            if override:
                subject = override

        email_msg = EmailMessage()
        email_msg["From"] = self.config.from_address or self.config.smtp_username or self.config.imap_username
        email_msg["To"] = to_addr
        email_msg["Subject"] = subject
        email_msg.set_content(msg.content or "")

        in_reply_to = self._last_message_id_by_chat.get(to_addr)
        if in_reply_to:
            email_msg["In-Reply-To"] = in_reply_to
            email_msg["References"] = in_reply_to

        try:
            await asyncio.to_thread(self._smtp_send, email_msg)
        except Exception as e:
            logger.error("Error sending email to {}: {}", to_addr, e)
            raise

    def _validate_config(self) -> bool:
        missing = []
        if not self.config.imap_host:
            missing.append("imap_host")
        if not self.config.imap_username:
            missing.append("imap_username")
        if not self.config.imap_password:
            missing.append("imap_password")
        if not self.config.smtp_host:
            missing.append("smtp_host")
        if not self.config.smtp_username:
            missing.append("smtp_username")
        if not self.config.smtp_password:
            missing.append("smtp_password")

        if missing:
            logger.error("Email channel not configured, missing: {}", ', '.join(missing))
            return False
        return True

    def _smtp_send(self, msg: EmailMessage) -> None:
        timeout = 30
        if self.config.smtp_use_ssl:
            with smtplib.SMTP_SSL(
                self.config.smtp_host,
                self.config.smtp_port,
                timeout=timeout,
            ) as smtp:
                smtp.login(self.config.smtp_username, self.config.smtp_password)
                smtp.send_message(msg)
            return

        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=timeout) as smtp:
            if self.config.smtp_use_tls:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.config.smtp_username, self.config.smtp_password)
            smtp.send_message(msg)

    def _fetch_new_messages(self) -> list[dict[str, Any]]:
        """Poll IMAP and return parsed unread messages."""
        return self._fetch_messages(
            search_criteria=("UNSEEN",),
            mark_seen=self.config.mark_seen,
            dedupe=True,
            limit=0,
        )

    def fetch_messages_between_dates(
        self,
        start_date: date,
        end_date: date,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Fetch messages in [start_date, end_date) by IMAP date search.

        This is used for historical summarization tasks (e.g. "yesterday").
        """
        if end_date <= start_date:
            return []

        return self._fetch_messages(
            search_criteria=(
                "SINCE",
                self._format_imap_date(start_date),
                "BEFORE",
                self._format_imap_date(end_date),
            ),
            mark_seen=False,
            dedupe=False,
            limit=max(1, int(limit)),
        )

    def _fetch_messages(
        self,
        search_criteria: tuple[str, ...],
        mark_seen: bool,
        dedupe: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        cycle_uids: set[str] = set()

        for attempt in range(2):
            try:
                self._fetch_messages_once(
                    search_criteria,
                    mark_seen,
                    dedupe,
                    limit,
                    messages,
                    cycle_uids,
                )
                return messages
            except Exception as exc:
                if attempt == 1 or not self._is_stale_imap_error(exc):
                    raise
                logger.warning("Email IMAP connection went stale, retrying once: {}", exc)

        return messages

    def _fetch_messages_once(
        self,
        search_criteria: tuple[str, ...],
        mark_seen: bool,
        dedupe: bool,
        limit: int,
        messages: list[dict[str, Any]],
        cycle_uids: set[str],
    ) -> None:
        """Fetch messages by arbitrary IMAP search criteria."""
        mailbox = self.config.imap_mailbox or "INBOX"

        if self.config.imap_use_ssl:
            client = imaplib.IMAP4_SSL(self.config.imap_host, self.config.imap_port)
        else:
            client = imaplib.IMAP4(self.config.imap_host, self.config.imap_port)

        try:
            client.login(self.config.imap_username, self.config.imap_password)
            try:
                status, _ = client.select(mailbox)
            except Exception as exc:
                if self._is_missing_mailbox_error(exc):
                    logger.warning("Email mailbox unavailable, skipping poll for {}: {}", mailbox, exc)
                    return messages
                raise
            if status != "OK":
                logger.warning("Email mailbox select returned {}, skipping poll for {}", status, mailbox)
                return messages

            status, data = client.search(None, *search_criteria)
            if status != "OK" or not data:
                return messages

            ids = data[0].split()
            if limit > 0 and len(ids) > limit:
                ids = ids[-limit:]
            for imap_id in ids:
                status, fetched = client.fetch(imap_id, "(BODY.PEEK[] UID)")
                if status != "OK" or not fetched:
                    continue

                raw_bytes = self._extract_message_bytes(fetched)
                if raw_bytes is None:
                    continue

                uid = self._extract_uid(fetched)
                if uid and uid in cycle_uids:
                    continue
                if dedupe and uid and uid in self._processed_uids:
                    continue

                parsed = BytesParser(policy=policy.default).parsebytes(raw_bytes)
                sender = parseaddr(parsed.get("From", ""))[1].strip().lower()
                if not sender:
                    continue
                if self._is_self_address(sender):
                    logger.info("Email from {} ignored: matches bot-owned address", sender)
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    if mark_seen:
                        client.store(imap_id, "+FLAGS", "\\Seen")
                    continue

                # --- Anti-spoofing: verify Authentication-Results ---
                spf_pass, dkim_pass = self._check_authentication_results(parsed)
                if self.config.verify_spf and not spf_pass:
                    logger.warning(
                        "Email from {} rejected: SPF verification failed "
                        "(no 'spf=pass' in Authentication-Results header)",
                        sender,
                    )
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    continue
                if self.config.verify_dkim and not dkim_pass:
                    logger.warning(
                        "Email from {} rejected: DKIM verification failed "
                        "(no 'dkim=pass' in Authentication-Results header)",
                        sender,
                    )
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    continue

                subject = self._decode_header_value(parsed.get("Subject", ""))

                # `allow_from` finally does the one thing its name promised.
                # Coarse and first: a sender nobody listed never reaches the
                # rules, so a deployment using only this field behaves the way
                # its config always claimed it did.
                if self.config.allow_from and not any(
                        fnmatch(sender, pat.strip().lower())
                        for pat in self.config.allow_from if pat):
                    logger.info("Email from {} ignored: not in allow_from", sender)
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    if mark_seen:
                        client.store(imap_id, "+FLAGS", "\\Seen")
                    continue

                # Was it addressed to him, and what may he do with it? Both
                # answers come from email_rules, which is where they can be
                # tested against crafted headers without an IMAP server.
                verdict = evaluate_rules(
                    self.config.rules, sender=sender, headers=parsed,
                    subject=subject, alfred_addresses=self.config.alfred_addresses)
                if self.config.rules_enforced and not verdict.read:
                    logger.info("Email from {} not read: {}", sender, verdict.reason)
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    if mark_seen:
                        client.store(imap_id, "+FLAGS", "\\Seen")
                    continue
                self._verdict_by_chat[sender] = verdict

                date_value = parsed.get("Date", "")
                message_id = parsed.get("Message-ID", "").strip()
                body = self._extract_text_body(parsed)

                if not body:
                    body = "(empty email body)"

                body = body[: self.config.max_body_chars]
                permissions = describe_verdict(verdict) if self.config.rules_enforced else ""
                content = (
                    f"[EMAIL-CONTEXT] Email received.\n"
                    f"From: {sender}\n"
                    f"Subject: {subject}\n"
                    f"Date: {date_value}\n"
                    + (f"{permissions}\n" if permissions else "")
                    + f"\n{body}"
                )

                # --- Attachment extraction ---
                attachment_paths: list[str] = []
                if self.config.allowed_attachment_types:
                    saved = self._extract_attachments(
                        parsed,
                        uid or "noid",
                        allowed_types=self.config.allowed_attachment_types,
                        max_size=self.config.max_attachment_size,
                        max_count=self.config.max_attachments_per_email,
                    )
                    for p in saved:
                        attachment_paths.append(str(p))
                        content += f"\n[attachment: {p.name} — saved to {p}]"

                metadata = {
                    "message_id": message_id,
                    "subject": subject,
                    "date": date_value,
                    "sender_email": sender,
                    "uid": uid,
                }
                messages.append(
                    {
                        "sender": sender,
                        "subject": subject,
                        "message_id": message_id,
                        "content": content,
                        "metadata": metadata,
                        "media": attachment_paths,
                    }
                )

                self._remember_processed_uid(uid, dedupe, cycle_uids)

                if mark_seen:
                    client.store(imap_id, "+FLAGS", "\\Seen")
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def _collect_self_addresses(self) -> set[str]:
        """Return normalized email addresses owned by this channel instance."""
        candidates = (
            self.config.from_address,
            self.config.smtp_username,
            self.config.imap_username,
        )
        normalized = {
            addr
            for candidate in candidates
            if (addr := self._normalize_address(candidate))
        }
        return normalized

    @staticmethod
    def _normalize_address(value: str) -> str:
        """Normalize an address or mailbox-like identifier for comparisons."""
        raw = (value or "").strip()
        if not raw:
            return ""
        parsed = parseaddr(raw)[1].strip().lower()
        if parsed:
            return parsed
        if "@" in raw:
            return raw.lower()
        return ""

    def _is_self_address(self, sender: str) -> bool:
        """Return True when an inbound sender belongs to the bot itself."""
        normalized_sender = self._normalize_address(sender)
        return bool(normalized_sender) and normalized_sender in self._self_addresses

    def _remember_processed_uid(self, uid: str, dedupe: bool, cycle_uids: set[str]) -> None:
        """Track a fetched UID so skipped messages are not reprocessed forever."""
        if not uid:
            return
        cycle_uids.add(uid)
        if dedupe:
            self._processed_uids.add(uid)
            # mark_seen is the primary dedup; this set is a safety net
            if len(self._processed_uids) > self._MAX_PROCESSED_UIDS:
                # Evict a random half to cap memory; mark_seen is the primary dedup
                self._processed_uids = set(list(self._processed_uids)[len(self._processed_uids) // 2:])

    @classmethod
    def _is_stale_imap_error(cls, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in cls._IMAP_RECONNECT_MARKERS)

    @classmethod
    def _is_missing_mailbox_error(cls, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in cls._IMAP_MISSING_MAILBOX_MARKERS)

    @classmethod
    def _format_imap_date(cls, value: date) -> str:
        """Format date for IMAP search (always English month abbreviations)."""
        month = cls._IMAP_MONTHS[value.month - 1]
        return f"{value.day:02d}-{month}-{value.year}"

    @staticmethod
    def _extract_message_bytes(fetched: list[Any]) -> bytes | None:
        for item in fetched:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        return None

    @staticmethod
    def _extract_uid(fetched: list[Any]) -> str:
        for item in fetched:
            if isinstance(item, tuple) and item and isinstance(item[0], (bytes, bytearray)):
                head = bytes(item[0]).decode("utf-8", errors="ignore")
                m = re.search(r"UID\s+(\d+)", head)
                if m:
                    return m.group(1)
        return ""

    @staticmethod
    def _decode_header_value(value: str) -> str:
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    @classmethod
    def _extract_text_body(cls, msg: Any) -> str:
        """Best-effort extraction of readable body text."""
        if msg.is_multipart():
            plain_parts: list[str] = []
            html_parts: list[str] = []
            for part in msg.walk():
                if part.get_content_disposition() == "attachment":
                    continue
                content_type = part.get_content_type()
                try:
                    payload = part.get_content()
                except Exception:
                    payload_bytes = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    payload = payload_bytes.decode(charset, errors="replace")
                if not isinstance(payload, str):
                    continue
                if content_type == "text/plain":
                    plain_parts.append(payload)
                elif content_type == "text/html":
                    html_parts.append(payload)
            if plain_parts:
                return "\n\n".join(plain_parts).strip()
            if html_parts:
                return cls._html_to_text("\n\n".join(html_parts)).strip()
            return ""

        try:
            payload = msg.get_content()
        except Exception:
            payload_bytes = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            payload = payload_bytes.decode(charset, errors="replace")
        if not isinstance(payload, str):
            return ""
        if msg.get_content_type() == "text/html":
            return cls._html_to_text(payload).strip()
        return payload.strip()

    @staticmethod
    def _check_authentication_results(parsed_msg: Any) -> tuple[bool, bool]:
        """Parse Authentication-Results headers for SPF and DKIM verdicts.

        Returns:
            A tuple of (spf_pass, dkim_pass) booleans.
        """
        spf_pass = False
        dkim_pass = False
        for ar_header in parsed_msg.get_all("Authentication-Results") or []:
            ar_lower = ar_header.lower()
            if re.search(r"\bspf\s*=\s*pass\b", ar_lower):
                spf_pass = True
            if re.search(r"\bdkim\s*=\s*pass\b", ar_lower):
                dkim_pass = True
        return spf_pass, dkim_pass

    @classmethod
    def _extract_attachments(
        cls,
        msg: Any,
        uid: str,
        *,
        allowed_types: list[str],
        max_size: int,
        max_count: int,
    ) -> list[Path]:
        """Extract and save email attachments to the media directory.

        Returns list of saved file paths.
        """
        if not msg.is_multipart():
            return []

        saved: list[Path] = []
        media_dir = get_media_dir("email")

        for part in msg.walk():
            if len(saved) >= max_count:
                break
            if part.get_content_disposition() != "attachment":
                continue

            content_type = part.get_content_type()
            if not any(fnmatch(content_type, pat) for pat in allowed_types):
                logger.debug("Email attachment skipped (type {}): not in allowed list", content_type)
                continue

            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            if len(payload) > max_size:
                logger.warning(
                    "Email attachment skipped: size {} exceeds limit {}",
                    len(payload),
                    max_size,
                )
                continue

            raw_name = part.get_filename() or "attachment"
            sanitized = safe_filename(raw_name) or "attachment"
            dest = media_dir / f"{uid}_{sanitized}"

            try:
                dest.write_bytes(payload)
                saved.append(dest)
                logger.info("Email attachment saved: {}", dest)
            except Exception as exc:
                logger.warning("Failed to save email attachment {}: {}", dest, exc)

        return saved

    @staticmethod
    def _html_to_text(raw_html: str) -> str:
        text = re.sub(r"<\s*br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
        text = re.sub(r"<\s*/\s*p\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return html.unescape(text)

    def _reply_subject(self, base_subject: str) -> str:
        subject = (base_subject or "").strip() or "nanobot reply"
        prefix = self.config.subject_prefix or "Re: "
        if subject.lower().startswith("re:"):
            return subject
        return f"{prefix}{subject}"

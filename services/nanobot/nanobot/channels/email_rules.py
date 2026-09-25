"""Who Alfred may answer, and what he may do about it.

The email channel arrived with an `allow_from` list that nothing read: every
message that passed SPF and DKIM became a turn, from anybody. This module is
the part that decides, and it is separate from `email.py` on purpose — the
decision needs testing against a hundred crafted headers, and the channel it
serves needs an IMAP server to say anything at all.

Three things it settles, in the order a message meets them:

* **Was this even addressed to him?** A mailbox is not an address. Alfred's
  address is regularly an alias on somebody's real account, and a catch-all
  receives mail for every name at the domain — so «it arrived in the inbox we
  poll» says nothing about who it was for. `recipients()` reads every header
  that can carry a delivery address and `addressed_to_alfred()` requires one of
  his to be among them.

* **What may he do with it?** Rules grant `read`, `respond` and `resend`. The
  default is none of them: a message nobody wrote a rule for is one he never
  sees, which is the same fail-closed default the rest of this house runs on.

* **What if several rules match?** Least privilege, and literally — a
  permission survives only if *every* matching rule grants it. That is the
  surprising direction, and it is deliberate: it means a broad rule can only
  ever narrow, so `*@empresa.com: read` sitting beside `jefe@empresa.com:
  respond` yields read. Adding a rule can take permissions away and never hand
  them out by accident, which is the property worth having when the thing being
  granted is «may reply to strangers as me».

Instructions are not a permission and do not intersect: every matching rule's
prose comes along, in order, because two rules that both apply both have
something to say.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import getaddresses
from fnmatch import fnmatch

# The headers that can carry the address a message was delivered *to*. To and
# Cc are what the sender wrote; Delivered-To and X-Original-To are what the
# receiving server recorded, and they are the ones that survive an alias — mail
# to `alfred@example.com` forwarded into `user1@mail.example` keeps `alfred@example.com`
# only in the second pair. Bcc is deliberately absent: it does not arrive.
RECIPIENT_HEADERS = ("To", "Cc", "Delivered-To", "X-Original-To",
                     "X-Forwarded-To", "Envelope-To", "Resent-To")

PERMISSIONS = ("read", "respond", "resend")

# How far «may reply» goes. Ordered narrowest first, because that ordering is
# the rule when several match — see `evaluate`.
#
#   no          — never
#   on_request  — only when somebody in the house asked for this reply. An
#                 email arriving is not a request; a person saying «contéstale»
#                 is. This is the setting for a mailbox Alfred watches but does
#                 not speak for.
#   always      — may answer on his own, which is what an auto-reply is.
RESPOND_MODES = ("no", "on_request", "always")

MATCH_KINDS = ("from", "to", "subject", "any")


@dataclass(frozen=True)
class EmailRule:
    """One line of «when this, he may that»."""

    match: str = "from"
    pattern: str = "*"
    read: bool = False
    respond: bool = False
    resend: bool = False
    instructions: str = ""
    respond_mode: str = ""     # "" means: read it off `respond`
    confirm: bool = False      # nothing leaves without somebody approving it

    @property
    def replies(self) -> str:
        """The respond mode, however this rule spelled it.

        A rule written before modes existed says `respond: true`, which meant
        «may answer, including on his own» — so it maps to `always` and not to
        the safer setting. Quietly tightening a stored rule would be a change
        nobody made; the page is where somebody chooses the new one.
        """
        if self.respond_mode in RESPOND_MODES:
            return self.respond_mode
        return "always" if self.respond else "no"

    @classmethod
    def from_dict(cls, raw: dict) -> "EmailRule":
        match = str(raw.get("match") or raw.get("match_kind") or "from").lower()
        if match not in MATCH_KINDS:
            match = "from"
        mode = str(raw.get("respond_mode") or raw.get("respondMode") or "").lower()
        return cls(
            match=match,
            pattern=str(raw.get("pattern") or "*"),
            read=bool(raw.get("read", raw.get("can_read", False))),
            respond=bool(raw.get("respond", raw.get("can_respond", False))),
            resend=bool(raw.get("resend", raw.get("can_resend", False))),
            instructions=str(raw.get("instructions") or ""),
            respond_mode=mode if mode in RESPOND_MODES else "",
            confirm=bool(raw.get("confirm", raw.get("can_confirm",
                         raw.get("confirm_before_send", False)))),
        )


@dataclass(frozen=True)
class Verdict:
    """What Alfred may do with one message, and why."""

    read: bool = False
    respond: bool = False
    resend: bool = False
    instructions: tuple[str, ...] = field(default_factory=tuple)
    matched: int = 0
    reason: str = ""
    replies: str = "no"        # narrowest respond mode any matching rule allows
    confirm: bool = False      # somebody has to approve the text before it goes

    def allows(self, permission: str) -> bool:
        return bool(getattr(self, permission, False))

    def may_reply(self, *, asked_for: bool) -> bool:
        """Whether a reply may go out at all, given who wanted it.

        `asked_for` is «somebody in the house asked for this one» — a person
        saying «contéstale», not an email arriving. That distinction is the
        whole of `on_request`, and it is decided by the caller because only the
        send path knows which of the two produced the message.
        """
        if self.replies == "always":
            return True
        if self.replies == "on_request":
            return bool(asked_for)
        return False


def normalize(address: str) -> str:
    """One address, lowercased and bare. Empty when there is not one."""
    if not address:
        return ""
    return str(address).strip().strip("<>").strip().lower()


def recipients(headers) -> set[str]:
    """Every address this message was delivered to, from every header that says.

    `headers` is anything with `.get_all(name)` — an `email.message.Message` —
    or a plain mapping, which is what the tests use.
    """
    raw: list[str] = []
    for name in RECIPIENT_HEADERS:
        if hasattr(headers, "get_all"):
            values = headers.get_all(name) or []
        else:
            value = headers.get(name) if hasattr(headers, "get") else None
            values = [value] if value else []
        raw.extend(v for v in values if v)
    return {normalize(addr) for _, addr in getaddresses(raw) if normalize(addr)}


def addressed_to_alfred(headers, alfred_addresses) -> bool:
    """Was one of his addresses actually on this message?

    With none configured this is True: a channel nobody told which address is
    his is one where every message in the mailbox is his, which is the
    behaviour that existed before this module and the one a single-purpose
    mailbox still wants.
    """
    mine = {normalize(a) for a in (alfred_addresses or []) if normalize(a)}
    if not mine:
        return True
    return bool(mine & recipients(headers))


def _matches(rule: EmailRule, sender: str, to: set[str], subject: str) -> bool:
    pattern = (rule.pattern or "*").strip().lower()
    if rule.match == "any":
        return True
    if rule.match == "from":
        return fnmatch(normalize(sender), pattern)
    if rule.match == "to":
        return any(fnmatch(addr, pattern) for addr in to)
    if rule.match == "subject":
        # Subjects are prose, so a bare word should match it. A pattern with no
        # wildcard of its own is read as «contains», which is what somebody
        # typing «factura» into a form means.
        text = (subject or "").lower()
        if not any(c in pattern for c in "*?["):
            return pattern in text
        return fnmatch(text, pattern)
    return False


def evaluate(rules, sender: str, headers=None, subject: str = "",
             alfred_addresses=None) -> Verdict:
    """What this message earns, given the rules.

    Returns a Verdict with nothing granted when no rule matches, when the
    message was not addressed to him, or when there are no rules at all. There
    is no path through this function that grants a permission by default.
    """
    to = recipients(headers) if headers is not None else set()

    if not addressed_to_alfred(headers or {}, alfred_addresses):
        return Verdict(reason="not addressed to any of Alfred's addresses")

    parsed = [r if isinstance(r, EmailRule) else EmailRule.from_dict(r)
              for r in (rules or [])]
    if not parsed:
        return Verdict(reason="no rules configured, so nothing is permitted")

    matched = [r for r in parsed if _matches(r, sender, to, subject)]
    if not matched:
        return Verdict(reason=f"no rule matches {normalize(sender) or 'this sender'}")

    granted = {p: all(getattr(r, p) for r in matched) for p in PERMISSIONS}
    notes = tuple(r.instructions.strip() for r in matched if r.instructions.strip())

    # A *permission* survives only if every matching rule grants it, and a
    # *restriction* applies if any of them asks for it. Those are the same
    # principle pointing opposite ways — both make the outcome the safest one
    # consistent with the rules — and getting the second one to intersect like
    # the first would mean a broad «always confirm» rule could be switched off
    # by adding a narrow rule that forgot to mention it.
    replies = min((r.replies for r in matched), key=RESPOND_MODES.index)
    confirm = any(r.confirm for r in matched)

    narrowed = [p for p in PERMISSIONS
                if not granted[p] and any(getattr(r, p) for r in matched)]
    reason = f"{len(matched)} rule(s) matched"
    if narrowed:
        # Said out loud because this is the direction that surprises people:
        # a rule they just added granting `respond` can come out as no.
        reason += f"; {', '.join(narrowed)} withheld by a narrower rule"
    if replies != "always" and any(r.replies == "always" for r in matched):
        reason += f"; replies limited to «{replies}» by a narrower rule"
    if confirm:
        reason += "; nothing sends without approval"
    return Verdict(read=granted["read"],
                   # Kept for callers that ask the coarse question. «May he
                   # reply at all» is not «may he reply unprompted»: see
                   # may_reply, which is what the send path uses.
                   respond=replies != "no",
                   resend=granted["resend"], instructions=notes,
                   matched=len(matched), reason=reason,
                   replies=replies, confirm=confirm)


def outbound_permission(to_addr: str, original_sender: str) -> str:
    """Whether sending this is answering or passing it on.

    The distinction is the whole difference between the two permissions, and it
    is decidable from the addresses alone: back to whoever wrote is a reply,
    anywhere else is a resend. Treating an unknown thread as a resend is the
    conservative way round — `resend` is the rarer grant, so an unrecognised
    destination needs the stronger permission rather than the weaker one.
    """
    if original_sender and normalize(to_addr) == normalize(original_sender):
        return "respond"
    return "resend"


def describe(verdict: Verdict) -> str:
    """The line that goes to the agent above the message.

    He is told what he may do rather than left to infer it from what happens
    when he tries: a refusal at send time is a wasted turn and reads to him
    like a fault.
    """
    allowed = [p for p in PERMISSIONS if verdict.allows(p)]
    line = ("Permisos para este correo: "
            + (", ".join(allowed) if allowed else "ninguno"))
    # The two conditions, spelled out. He is told what he may do rather than
    # left to infer it from what happens when he tries: a refusal at send time
    # is a wasted turn and reads to him like a fault.
    if verdict.replies == "on_request":
        line += ("\nSólo contestas si alguien de la casa te lo pide. Que llegue un "
                 "correo no es un pedido; que alguien te diga «contéstale», sí.")
    elif verdict.replies == "no":
        line += "\nNo contestas este correo, ni aunque te lo pidan."
    if verdict.confirm:
        line += ("\nAntes de enviar cualquier cosa, muestra el texto y espera "
                 "que lo aprueben en la app. Escribe el borrador en un bloque "
                 ":::ask con «Enviar» y «Cambiar algo» como respuestas.")
    if verdict.instructions:
        line += "\nInstrucciones para este remitente:\n" + "\n".join(
            f"- {i}" for i in verdict.instructions)
    return line

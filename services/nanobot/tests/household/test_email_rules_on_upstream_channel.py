"""household: email rules enforced on upstream's EmailChannel (C5).

v0.4.1's EmailConfig ignored ``rules``, so mail from anybody on allowFrom was
answered. With rules enforced, a sender no rule lets read is not delivered,
and a reply nobody asked for to a read-only sender is refused at send.
"""

from __future__ import annotations

import asyncio

import pytest

from nanobot.bus.delivery import DeliveryRejectedError
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.email.runtime import EmailChannel
from nanobot.channels.email.tests.test_email_channel import (
    _make_config,
    _make_fake_imap,
    _make_raw_email,
)

RULES = [
    {"match": "from", "pattern": "tomi@example.org", "read": True, "respond": True},
    {"match": "from", "pattern": "*@bank.example", "read": True},
]


def _fetch(monkeypatch, sender: str, **overrides):
    fake = _make_fake_imap(_make_raw_email(from_addr=sender, subject="Hola"), uid=b"7")
    monkeypatch.setattr("nanobot.channels.email.runtime.imaplib.IMAP4_SSL", lambda _h, _p: fake)
    channel = EmailChannel(_make_config(rules=RULES, rules_enforced=True, **overrides), MessageBus())
    items, skipped = channel._fetch_new_messages()
    return channel, items, skipped


def test_a_sender_no_rule_lets_read_is_not_delivered(monkeypatch):
    _, items, skipped = _fetch(monkeypatch, "stranger@example.net")
    assert items == []
    assert skipped == {"7"}


def test_an_allowed_sender_arrives_with_its_permissions(monkeypatch):
    _, items, _ = _fetch(monkeypatch, "tomi@example.org")
    assert len(items) == 1
    assert "tomi@example.org" in items[0]["content"]


def test_rules_off_keeps_upstream_behaviour(monkeypatch):
    fake = _make_fake_imap(_make_raw_email(from_addr="stranger@example.net"), uid=b"8")
    monkeypatch.setattr("nanobot.channels.email.runtime.imaplib.IMAP4_SSL", lambda _h, _p: fake)
    channel = EmailChannel(_make_config(rules=RULES), MessageBus())
    items, _ = channel._fetch_new_messages()
    assert len(items) == 1


def test_an_unasked_reply_to_a_read_only_sender_is_refused(monkeypatch):
    channel, items, _ = _fetch(monkeypatch, "alerts@bank.example")
    assert len(items) == 1
    channel._last_subject_by_chat["alerts@bank.example"] = "Hola"
    with pytest.raises(DeliveryRejectedError):
        asyncio.run(channel.send(OutboundMessage(channel="email", chat_id="alerts@bank.example",
                                                 content="Gracias")))


def test_mail_to_an_address_no_rule_granted_is_refused():
    channel = EmailChannel(_make_config(rules=RULES, rules_enforced=True), MessageBus())
    with pytest.raises(DeliveryRejectedError):
        asyncio.run(channel.send(OutboundMessage(channel="email", chat_id="nobody@example.net",
                                                 content="x", metadata={"force_send": True})))


def test_allow_from_takes_the_forks_patterns_in_the_household(monkeypatch):
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.email.runtime import EmailChannel, EmailConfig

    cfg = EmailConfig.model_validate({"allowFrom": ["*@example.org", "Mora@Family.test"]})
    channel = EmailChannel(cfg, MessageBus())
    monkeypatch.delenv("NANOBOT_HOUSEHOLD", raising=False)
    assert not channel.is_allowed("tomi@example.org")  # upstream: exact tokens only
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    assert channel.is_allowed("tomi@example.org") and channel.is_allowed("TOMI@EXAMPLE.ORG")
    assert channel.is_allowed("mora@family.test")
    assert not channel.is_allowed("tomi@example.org.evil.test")

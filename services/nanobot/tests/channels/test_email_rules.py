"""The email gate: who Alfred may answer, and what he may do about it.

The channel shipped with an `allow_from` list that nothing read — every message
passing SPF and DKIM became a turn, from anybody — so most of this file is
about refusals, and about the two that are easy to get subtly wrong:

* **A mailbox is not an address.** Alfred's address is usually an alias on a
  real account, and a catch-all takes mail for every name at the domain.
  «It arrived in the inbox we poll» therefore says nothing about who it was
  for, and the headers that survive an alias are not the ones the sender wrote.

* **Least privilege means a broad rule narrows a specific one.** That is the
  direction people do not expect, so it is pinned here rather than left to be
  rediscovered the first time a reply does not go out.
"""

from email import policy
from email.parser import Parser

import pytest

from nanobot.channels.email_rules import (
    EmailRule,
    addressed_to_alfred,
    describe,
    evaluate,
    outbound_permission,
    recipients,
)

ALFRED = ["alfred@example.com"]


def headers(**kw):
    """A parsed message with the headers named, so the code under test sees the
    same object shape it sees in production."""
    raw = "".join(f"{k.replace('_', '-')}: {v}\n" for k, v in kw.items())
    return Parser(policy=policy.default).parsestr(raw + "\n")


def rule(**kw):
    return EmailRule.from_dict(kw)


# --- was it even addressed to him -------------------------------------------

def test_every_delivery_header_is_read():
    msg = headers(To="uno@x.cl", Cc="dos@x.cl", Delivered_To="tres@x.cl",
                  X_Original_To="cuatro@x.cl")
    assert recipients(msg) == {"uno@x.cl", "dos@x.cl", "tres@x.cl", "cuatro@x.cl"}


def test_a_display_name_does_not_hide_the_address():
    assert recipients(headers(To='"Alfred, el mayordomo" <alfred@example.com>')) \
        == {"alfred@example.com"}


def test_case_and_angle_brackets_do_not_make_a_different_address():
    assert addressed_to_alfred(headers(To="<ALFRED@Example.COM>"), ALFRED)


def test_mail_for_somebody_else_in_the_same_mailbox_is_not_his():
    """The catch-all case, and the reason this check exists: the message is in
    the inbox being polled and was never addressed to him."""
    assert not addressed_to_alfred(headers(To="user1@example.com"), ALFRED)


def test_an_alias_survives_because_the_server_recorded_it():
    """Mail to alfred@ forwarded into user1@: only Delivered-To still says who it
    was for, which is exactly why To alone is not enough."""
    msg = headers(To="user1@example.com", Delivered_To="alfred@example.com")
    assert addressed_to_alfred(msg, ALFRED)


def test_being_on_the_cc_line_counts():
    assert addressed_to_alfred(headers(To="user1@x.cl", Cc="alfred@example.com"), ALFRED)


def test_with_no_address_configured_the_whole_mailbox_is_his():
    """A single-purpose mailbox that nobody told about aliases keeps working."""
    assert addressed_to_alfred(headers(To="cualquiera@x.cl"), [])


def test_a_message_not_addressed_to_him_earns_nothing_at_all():
    v = evaluate([rule(match="any", read=True, respond=True)],
                 sender="jefe@x.cl", headers=headers(To="other@example.com"),
                 alfred_addresses=ALFRED)
    assert not v.read and not v.respond
    assert "not addressed" in v.reason


# --- the default is nothing --------------------------------------------------

def test_no_rules_is_no_permission():
    v = evaluate([], sender="quien@sea.cl", headers=headers(To=ALFRED[0]),
                 alfred_addresses=ALFRED)
    assert not any((v.read, v.respond, v.resend))
    assert "no rules" in v.reason


def test_a_sender_no_rule_mentions_earns_nothing():
    v = evaluate([rule(match="from", pattern="jefe@x.cl", read=True)],
                 sender="desconocido@spam.cl", headers=headers(To=ALFRED[0]),
                 alfred_addresses=ALFRED)
    assert not v.read
    assert "no rule matches" in v.reason


def test_a_rule_grants_only_what_it_names():
    v = evaluate([rule(match="from", pattern="*@x.cl", read=True)],
                 sender="jefe@x.cl", headers=headers(To=ALFRED[0]),
                 alfred_addresses=ALFRED)
    assert v.read and not v.respond and not v.resend


# --- least privilege ---------------------------------------------------------

def test_a_broad_rule_narrows_a_specific_one():
    """The surprising direction, on purpose: adding a rule can take a
    permission away and can never hand one out by accident."""
    v = evaluate(
        [rule(match="from", pattern="*@empresa.com", read=True),
         rule(match="from", pattern="jefe@empresa.com", read=True, respond=True)],
        sender="jefe@empresa.com", headers=headers(To=ALFRED[0]),
        alfred_addresses=ALFRED)
    assert v.read is True
    assert v.respond is False, "the broader rule did not grant respond"
    assert "respond withheld" in v.reason


def test_a_rule_that_does_not_match_does_not_narrow():
    v = evaluate(
        [rule(match="from", pattern="*@otra.com", read=True),
         rule(match="from", pattern="jefe@empresa.com", read=True, respond=True)],
        sender="jefe@empresa.com", headers=headers(To=ALFRED[0]),
        alfred_addresses=ALFRED)
    assert v.read and v.respond


def test_the_reason_says_which_permission_was_withheld_and_by_what():
    v = evaluate(
        [rule(match="any", read=True),
         rule(match="from", pattern="jefe@x.cl", read=True, respond=True, resend=True)],
        sender="jefe@x.cl", headers=headers(To=ALFRED[0]), alfred_addresses=ALFRED)
    assert "respond" in v.reason and "resend" in v.reason
    assert v.matched == 2


# --- what a rule can match on ------------------------------------------------

def test_matching_on_the_address_it_was_sent_to():
    v = evaluate([rule(match="to", pattern="alfred@example.com", read=True)],
                 sender="quien@sea.cl", headers=headers(To="alfred@example.com"),
                 alfred_addresses=ALFRED)
    assert v.read


def test_a_bare_word_matches_a_subject_containing_it():
    """Somebody typing «factura» into a form means «subjects about invoices»,
    not «a subject that is exactly the word factura»."""
    v = evaluate([rule(match="subject", pattern="factura", read=True)],
                 sender="a@b.cl", headers=headers(To=ALFRED[0]),
                 subject="Re: Factura 3312 vencida", alfred_addresses=ALFRED)
    assert v.read


def test_a_subject_pattern_with_a_wildcard_is_taken_literally():
    v = evaluate([rule(match="subject", pattern="factura*", read=True)],
                 sender="a@b.cl", headers=headers(To=ALFRED[0]),
                 subject="Re: Factura 3312", alfred_addresses=ALFRED)
    assert not v.read, "the subject does not start with «factura»"


# --- instructions ------------------------------------------------------------

def test_every_matching_rule_contributes_its_instructions():
    v = evaluate(
        [rule(match="any", read=True, instructions="Nunca prometas fechas."),
         rule(match="from", pattern="jefe@x.cl", read=True,
              instructions="Con el jefe, formal y breve.")],
        sender="jefe@x.cl", headers=headers(To=ALFRED[0]), alfred_addresses=ALFRED)
    assert len(v.instructions) == 2
    assert "Nunca prometas fechas." in v.instructions


def test_the_agent_is_told_what_he_may_do():
    v = evaluate([rule(match="any", read=True, respond=True,
                       instructions="Formal y breve.")],
                 sender="a@b.cl", headers=headers(To=ALFRED[0]), alfred_addresses=ALFRED)
    text = describe(v)
    assert "read" in text and "respond" in text and "resend" not in text
    assert "Formal y breve." in text


def test_no_permission_says_so_in_words():
    assert "ninguno" in describe(evaluate([], sender="a@b.cl", headers=headers()))


# --- replying versus passing it on -------------------------------------------

@pytest.mark.parametrize("to_addr,expected", [
    ("jefe@x.cl", "respond"),
    ("JEFE@X.CL", "respond"),
    ("otro@x.cl", "resend"),
    ("", "resend"),
])
def test_answering_the_writer_is_a_reply_and_anywhere_else_is_a_resend(to_addr, expected):
    assert outbound_permission(to_addr, "jefe@x.cl") == expected


def test_an_unknown_thread_needs_the_stronger_permission():
    """Conservative on purpose: `resend` is the rarer grant, so a destination
    this process cannot tie to an incoming message asks for that one."""
    assert outbound_permission("alguien@x.cl", "") == "resend"


# --- «lee todo, contesta sólo lo que le pidan, y confirma antes de mandar» ---
#
# Three separate things, and only the first is a plain permission. Replying is
# graded, because «may answer» and «may answer unprompted» are different powers
# over somebody's mailbox. Confirmation is not a permission at all — it is a
# condition on the text, so it travels the opposite way through least
# privilege: a permission survives only if every matching rule grants it, and a
# restriction applies if any matching rule asks for it.

def test_reading_everything_sent_to_one_address():
    v = evaluate([rule(match="to", pattern="alfred@example.com", read=True,
                       respond_mode="on_request")],
                 sender="cualquiera@fuera.cl",
                 headers=headers(To="alfred@example.com"), alfred_addresses=ALFRED)
    assert v.read
    # Mail to the same mailbox but a different name is still not his.
    v2 = evaluate([rule(match="to", pattern="alfred@example.com", read=True)],
                  sender="cualquiera@fuera.cl",
                  headers=headers(To="alex@example.com"), alfred_addresses=ALFRED)
    assert not v2.read


def test_on_request_answers_when_asked_and_not_otherwise():
    v = evaluate([rule(match="any", read=True, respond_mode="on_request")],
                 sender="quien@sea.cl", headers=headers(To=ALFRED[0]),
                 alfred_addresses=ALFRED)
    assert v.replies == "on_request"
    assert v.may_reply(asked_for=True) is True
    assert v.may_reply(asked_for=False) is False, "an email arriving is not a request"


@pytest.mark.parametrize("mode,asked,expected", [
    ("always", False, True),        # an auto-reply, which is what always means
    ("always", True, True),
    ("on_request", True, True),
    ("on_request", False, False),
    ("no", True, False),            # not even when asked
    ("no", False, False),
])
def test_every_combination_of_mode_and_who_asked(mode, asked, expected):
    v = evaluate([rule(match="any", read=True, respond_mode=mode)],
                 sender="a@b.cl", headers=headers(To=ALFRED[0]), alfred_addresses=ALFRED)
    assert v.may_reply(asked_for=asked) is expected


def test_a_rule_written_before_modes_existed_still_means_what_it_meant():
    """`respond: true` meant «may answer, including on his own». Reading it as
    the safer setting would silently change a rule nobody edited."""
    v = evaluate([rule(match="any", read=True, respond=True)],
                 sender="a@b.cl", headers=headers(To=ALFRED[0]), alfred_addresses=ALFRED)
    assert v.replies == "always"


def test_the_narrowest_reply_mode_wins():
    v = evaluate(
        [rule(match="any", read=True, respond_mode="on_request"),
         rule(match="from", pattern="jefe@x.cl", read=True, respond_mode="always")],
        sender="jefe@x.cl", headers=headers(To=ALFRED[0]), alfred_addresses=ALFRED)
    assert v.replies == "on_request", "the broad rule narrows the specific one"
    assert "limited to" in v.reason


def test_one_rule_asking_for_confirmation_is_enough():
    """The opposite direction from a permission, on purpose. If confirmation
    intersected like `read` does, adding a narrow rule that forgot to mention
    it would switch off a broad «always confirm»."""
    v = evaluate(
        [rule(match="any", read=True, respond_mode="always", confirm=True),
         rule(match="from", pattern="jefe@x.cl", read=True, respond_mode="always")],
        sender="jefe@x.cl", headers=headers(To=ALFRED[0]), alfred_addresses=ALFRED)
    assert v.confirm is True
    assert "without approval" in v.reason


def test_no_rule_asking_for_it_means_no_confirmation():
    v = evaluate([rule(match="any", read=True, respond_mode="always")],
                 sender="a@b.cl", headers=headers(To=ALFRED[0]), alfred_addresses=ALFRED)
    assert v.confirm is False


def test_the_agent_is_told_both_conditions_in_words():
    v = evaluate([rule(match="any", read=True, respond_mode="on_request", confirm=True)],
                 sender="a@b.cl", headers=headers(To=ALFRED[0]), alfred_addresses=ALFRED)
    text = describe(v)
    assert "sólo contestas si alguien de la casa te lo pide" in text.lower()
    assert ":::ask" in text, "he is told how to ask, not just that he must"

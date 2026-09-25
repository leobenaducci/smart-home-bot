"""Branching a conversation at one message, without paying for it twice.

"Take this somewhere else from here" needs the context up to that point and
nothing after it. Replaying the earlier turns would mean paying for each one
again and getting different answers on the way; quoting the single message would
hand the model a sentence and call it context. Sessions are files, so the honest
version is to copy the prefix — which is what `/v1/sessions/fork` does.

Two things here are easy to get wrong and expensive when wrong:

- **Where the cut lands.** Tapping a *question* means "ask this again,
  differently", so the branch has to end on the previous answer and leave the
  question to be re-asked. Tapping an *answer* means "carry on from here", so
  that answer stays. Same feature, opposite sides of the anchor.

- **Landing on a legal boundary.** A cut through the middle of a tool turn
  copies an assistant message that announced tool calls whose results were left
  behind. That is not a soft inconsistency — every provider answers it with a
  400 — so the fork has to walk back to a boundary and say how far it walked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.session.manager import SessionManager
from nanobot.utils.helpers import find_legal_message_end


@pytest.fixture
def manager(tmp_path: Path) -> SessionManager:
    (tmp_path / "sessions").mkdir()
    return SessionManager(workspace=tmp_path)


def conversation() -> list[dict]:
    """A day in the chat, with a tool turn in the middle of it."""
    return [
        {"role": "user", "content": "¿qué compro para la once?"},
        {"role": "assistant", "content": "pan, palta y té."},
        {"role": "user", "content": "y para el almuerzo?"},
        # The tool turn: two calls, both answered, then the reply.
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "exec"}},
            {"id": "c2", "function": {"name": "read_file"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "lista"},
        {"role": "tool", "tool_call_id": "c2", "content": "minuta"},
        {"role": "assistant", "content": "tallarines, hay salsa en la despensa."},
        {"role": "user", "content": "gracias"},
        {"role": "assistant", "content": "de nada."},
    ]


# --- Where the cut lands ------------------------------------------------------

def test_a_question_is_cut_before_so_it_can_be_asked_again():
    msgs = conversation()
    anchor = 2                     # "y para el almuerzo?"
    kept = find_legal_message_end(msgs, anchor)
    assert kept == 2
    assert [m["content"] for m in msgs[:kept]] == [
        "¿qué compro para la once?", "pan, palta y té."]


def test_an_answer_is_cut_after_so_it_can_be_continued():
    msgs = conversation()
    anchor = 6                     # "tallarines…"
    kept = find_legal_message_end(msgs, anchor + 1)
    assert kept == 7
    assert msgs[kept - 1]["content"].startswith("tallarines")
    assert "gracias" not in [m.get("content") for m in msgs[:kept]]


# --- Landing on a legal boundary ----------------------------------------------

def test_a_cut_inside_a_tool_turn_walks_back_out_of_it():
    """The case a provider answers with a 400 rather than a shrug."""
    msgs = conversation()
    # Right after the assistant announced two calls and before either answer.
    assert find_legal_message_end(msgs, 4) == 3
    # After one of the two results — still illegal, c2 is unanswered.
    assert find_legal_message_end(msgs, 5) == 3


def test_a_boundary_is_a_boundary():
    msgs = conversation()
    for legal in (0, 1, 2, 3, 7, 8, 9):
        assert find_legal_message_end(msgs, legal) == legal, legal


def test_a_completed_tool_turn_is_kept_whole():
    """The walk-back goes to the *earliest* orphan, not one message at a time,
    so a finished turn behind an unfinished one survives intact."""
    msgs = [
        {"role": "user", "content": "arréglalo"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "function": {}}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b", "function": {}}]},
    ]
    # `a` is answered inside the prefix, so the first three stay; only the
    # assistant still waiting on `b` is dropped.
    assert find_legal_message_end(msgs, 4) == 3


def test_two_unanswered_turns_are_walked_out_of_completely():
    """Dropping one offending assistant can leave another behind it, and the
    cut has to clear both in one pass rather than settling on the nearer one."""
    msgs = [
        {"role": "user", "content": "arréglalo"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "function": {}}]},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b", "function": {}}]},
        {"role": "tool", "tool_call_id": "b", "content": "ok"},
    ]
    # `b` is answered but `a` never is, and `a` comes first — so the only legal
    # end is before both of them.
    assert find_legal_message_end(msgs, 4) == 1


def test_it_never_returns_more_than_it_was_given():
    msgs = conversation()
    assert find_legal_message_end(msgs, 999) == len(msgs)
    assert find_legal_message_end(msgs, -5) == 0
    assert find_legal_message_end([], 3) == 0


# --- What the copy has to be ---------------------------------------------------

def test_the_branch_is_a_copy_and_not_a_view(manager: SessionManager):
    """Editing one must not reach the other — they are separate conversations
    from the moment they part, and a shared list would make the branch grow the
    parent's next answer."""
    import copy as _copy

    src = manager.get_or_create("websocket:homeweb:user1:2026-08-18:1")
    src.messages = conversation()
    dst = manager.get_or_create("websocket:homeweb:user1:2026-08-18:2")
    dst.messages = _copy.deepcopy(src.messages[:2])
    dst.last_consolidated = 0

    dst.messages[0]["content"] = "otra cosa"
    dst.messages.append({"role": "user", "content": "sigo por acá"})
    assert src.messages[0]["content"] == "¿qué compro para la once?"
    assert len(src.messages) == 9


def test_consolidation_does_not_come_along(manager: SessionManager):
    """`last_consolidated` counts messages already summarised into files that
    belong to the source. Inherited, it would make the branch hide its own
    opening messages behind a summary it does not have."""
    src = manager.get_or_create("websocket:homeweb:user1:2026-08-18:3")
    src.messages = conversation()
    src.last_consolidated = 6

    dst = manager.get_or_create("websocket:homeweb:user1:2026-08-18:4")
    dst.messages = list(src.messages[:2])
    dst.last_consolidated = 0
    manager.save(dst)

    reloaded = manager.get_or_create("websocket:homeweb:user1:2026-08-18:4")
    assert reloaded.last_consolidated == 0
    assert len(reloaded.messages) == 2

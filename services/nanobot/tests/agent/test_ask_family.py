"""Asking another member's Alfred, rather than widening the read.

Every HomeCore read a skill makes is per-user — `tasks_api_list` filters
`assignee = ?` on every scope — so an Alfred asked "who cleaned the cat
bathroom yesterday?" sees only its own chores. On 2026-08-11 that produced a
confident "ayer nadie" when Robin had done it and been approved: the skill is
described as the family's, the data is one person's, and the gap got filled
with an answer instead of an admission.

Widening the API was the other option and the worse one — the per-user derived
proxy token is what keeps a prompt-injected instance acting only as its own
user. Asking keeps every credential where it is: each Alfred answers from its
own data, under its own token.
"""

from pathlib import Path

import pytest

from nanobot.agent.runner import (
    _ACTION_TO_SKILL,
    _load_skill_python_guide,
    _static_skill_translation,
)
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.tools.shell import ExecTool

SKILLS = Path(BUILTIN_SKILLS_DIR)


def _guide(skill: str) -> str:
    guide = _load_skill_python_guide(str(SKILLS / skill / "SKILL.md"))
    assert guide, f"{skill} has no Python guide"
    return guide


def test_ask_family_translates_with_both_arguments():
    code = _static_skill_translation(
        {"skill": "family-message", "action": "ask_family",
         "to": "user3", "question": "¿Limpiaste el baño de los gatos ayer?"},
        _guide("family-message"),
    )
    assert code is not None
    call = code.strip().splitlines()[-1]
    assert "ask_family(" in call
    assert "to='user3'" in call
    assert "gatos" in call


def test_ask_family_routes_to_its_skill():
    """Without this the action is an unknown tool name and never translates."""
    assert _ACTION_TO_SKILL.get("ask_family") == "family-message"


def test_asking_reaches_its_own_endpoint_not_the_dm_one():
    """A question must not go out through /chat/dm — that one lands in the
    other person's chat and rings their phone."""
    code = _static_skill_translation(
        {"skill": "family-message", "action": "ask_family",
         "to": "user3", "question": "¿cuántos puntos tienes?"},
        _guide("family-message"),
    )
    assert "/chat/ask-family" in code


def test_a_hand_written_ask_is_refused_and_names_family_message():
    """The endpoint is a skill-owned service like every other."""
    command = """python3 -c "import os; print('https://x/chat/ask-family')" """
    error = ExecTool._skill_reimplementation_error(command)
    assert error and "`family-message`" in error


def test_the_chores_skill_documents_how_to_ask_about_someone_else():
    """The false answer came from a doc that called the data the family's while
    the call returned one person's. Both halves of the fix have to stay
    described, or the model has the widened API and no reason to reach for it.
    """
    text = (SKILLS / "chores" / "SKILL.md").read_text(encoding="utf-8")
    assert '"user": "all"' in text, "chores must show how to read the whole house"
    assert "list_chores([scope],[user])" in text, "the signature must advertise user"
    assert "nobody, yesterday" in text, "the failure it exists to prevent must stay written down"


def test_the_chores_guide_actually_sends_the_user_parameter():
    """The doc can promise `user` only if the generated code passes it."""
    guide = _guide("chores")
    assert "user=None" in guide and "&user=" in guide


@pytest.mark.parametrize("action", ["send_family_message", "ask_family"])
def test_both_directions_still_translate(action):
    """Adding the question path must not break the message path."""
    invocation = {"skill": "family-message", "action": action, "to": "user2"}
    invocation["question" if action == "ask_family" else "text"] = "hola"
    code = _static_skill_translation(invocation, _guide("family-message"))
    assert code is not None and f"{action}(" in code.strip().splitlines()[-1]

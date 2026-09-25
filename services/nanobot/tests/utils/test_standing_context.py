"""Standing context: seen by the model every turn, stored on none of them."""

import pytest

from nanobot.utils.standing_context import (
    CLOSE,
    OPEN,
    for_history,
    for_prompt,
    strip_markers,
    wrap,
)

PERSONA = "[Context: Programmer]\n\nHere you are the programmer Alfred.\nRead before writing."
QUESTION = "¿por qué falla el deploy?"


def _turn(persona=PERSONA, question=QUESTION):
    return f"{wrap(persona)}\n\n{question}"


def test_the_model_sees_the_block_and_the_question_without_markers():
    out = for_prompt(_turn())
    assert PERSONA in out
    assert QUESTION in out
    assert OPEN not in out and CLOSE not in out


def test_history_keeps_the_question_and_drops_the_block():
    out = for_history(_turn())
    assert out == QUESTION
    assert "programmer Alfred" not in out


def test_text_without_markers_is_untouched_by_both():
    plain = "hola, ¿qué tal?"
    assert for_prompt(plain) == plain
    assert for_history(plain) == plain


def test_a_turn_that_is_only_standing_context_stores_as_empty():
    """There was nothing the user said — persisting an empty user turn would be
    inventing one."""
    assert for_history(wrap(PERSONA)) == ""
    assert PERSONA in for_prompt(wrap(PERSONA))


def test_an_unbalanced_marker_never_swallows_the_question():
    """A truncated message, or one someone typed by hand. Losing a marker must
    not lose the text next to it."""
    orphan_open = f"{OPEN}\nalgo\n\n{QUESTION}"
    assert QUESTION in for_prompt(orphan_open)
    orphan_close = f"algo\n{CLOSE}\n{QUESTION}"
    assert QUESTION in for_prompt(orphan_close)
    assert QUESTION in for_history(orphan_close)
    assert CLOSE not in for_history(orphan_close)


def test_two_blocks_stay_two_blocks():
    text = f"{wrap('uno')}\n{wrap('dos')}\n{QUESTION}"
    assert for_history(text) == QUESTION
    prompt = for_prompt(text)
    assert "uno" in prompt and "dos" in prompt


def test_strip_markers_defends_against_a_user_typing_them():
    typed = f"mira esto {OPEN} secreto {CLOSE} y dime"
    cleaned = strip_markers(typed)
    assert OPEN not in cleaned and CLOSE not in cleaned
    assert "secreto" in cleaned
    # Once cleaned, wrapping the real block cannot hide the user's own words.
    assert "secreto" in for_history(f"{wrap(PERSONA)}\n\n{cleaned}")


@pytest.mark.parametrize("value", [None, ""])
def test_empty_input_is_safe(value):
    assert for_prompt(value) == ""
    assert for_history(value) == ""

"""Harmony channel markup must never reach the household.

Reported 2026-09-02, from the family's own notifications:

    finalJuana salió de casa.
    final<|message|>[Assistant reply unavailable due to model error.]

gpt-oss models speak harmony -- `<|channel|>analysis<|message|>...<|end|>`
then `<|channel|>final<|message|>...` -- and some gateways parse it before
returning. Together does not: openai/gpt-oss-20b hands back the whole thing in
`content`. `strip_think` removed the leading `<|channel|>` and left the channel
*name* welded to the text, which is how "final" ended up as the first word of a
notification.
"""
import pytest

from nanobot.utils.helpers import strip_harmony, strip_think


def test_a_final_channel_is_unwrapped():
    assert strip_think("<|channel|>final<|message|>Juana salió de casa.") == \
        "Juana salió de casa."


def test_the_channel_name_never_survives_as_text():
    # The exact reported symptom.
    assert not strip_think(
        "<|channel|>final<|message|>Juana salió de casa.").startswith("final")


def test_reasoning_is_dropped_and_only_the_answer_kept():
    raw = ("<|channel|>analysis<|message|>The user says X, I should Y<|end|>"
           "<|channel|>final<|message|>Pili llegó al colegio")
    assert strip_think(raw) == "Pili llegó al colegio"


def test_reasoning_with_no_final_channel_yields_nothing():
    # It never produced an answer, and "" is the honest report of that: the
    # answerless guard swaps in the fallback. Half a reasoning trace is worse
    # than nothing, because it reads like an answer.
    raw = "<|channel|>analysis<|message|>The user says: reasoning that ran out"
    assert strip_think(raw) == ""


def test_an_empty_final_channel_yields_nothing():
    assert strip_think("<|channel|>final<|message|>") == ""


@pytest.mark.parametrize("text", [
    "plain reply with no markers at all",
    "Talking about <|channel|> tokens in prose stays put",
    "The <|message|> token on its own is not harmony",
])
def test_text_that_is_not_harmony_is_left_alone(text):
    # Only the full `<|channel|>NAME<|message|>` sequence engages the parser,
    # so a message discussing these tokens is not silently rewritten.
    assert strip_harmony(text) == text


def test_the_older_think_blocks_still_work():
    assert strip_think("<think>hidden</think>visible") == "visible"

# The same leak with every `<|...|>` already eaten by the gateway. This is what
# the household was sent on 2026-09-02 at 10:37 -- a full reasoning trace,
# including the model talking itself into an answer, with "Hecho." at the very
# end. The marker-parsing above cannot see it: there are no markers left.
BARE = ('analysisWe need to interpret the request: "[HomeCore system] Mora '
        'arrived at trabajo mora." We already sent it. I will respond '
        '"Hecho."assistantfinalHecho.')


def test_a_marker_less_harmony_reply_keeps_only_the_answer():
    assert strip_think(BARE) == "Hecho."


def test_the_reasoning_does_not_reach_the_household():
    out = strip_think(BARE)
    assert "We need to interpret" not in out
    assert "assistant" not in out
    assert not out.startswith("analysis")


def test_a_bare_final_channel_opening_the_reply_is_removed():
    # The exact symptom in this file's own docstring, and the one shape the
    # first version of the marker-less rule could not see: the gateway ate the
    # markers *and* the reasoning, so the reply opens on `final` rather than on
    # `analysis`. `admin/models.py`'s detector flags this; the stripper has to
    # agree with it or the probe calls a model broken that nothing fixes.
    assert strip_think("finalJuana salió de casa.") == "Juana salió de casa."


def test_a_half_eaten_marker_after_the_channel_name_goes_too():
    # Reported alongside it: the opening `<|channel|>` gone, `<|message|>`
    # still there, so neither the full-marker parser nor the bare rule saw it.
    assert strip_think(
        "final<|message|>[Assistant reply unavailable due to model error.]"
    ) == "[Assistant reply unavailable due to model error.]"


def test_the_last_final_wins():
    # The reasoning quotes its own intended answer on the way past, so an
    # earlier `final` is inside the analysis rather than the start of a reply.
    raw = 'analysisI could say finalAnswer here.assistantfinalMora llegó.'
    assert strip_think(raw) == "Mora llegó."


def test_marker_less_reasoning_with_no_answer_yields_nothing():
    assert strip_think("analysisReasoned but never reached an answer") == ""


@pytest.mark.parametrize("text", [
    "Analysis of the bill is attached",     # a capital A, and a real sentence
    "analysis of the situation is simple",  # a space after it, so not a channel
    "The final Answer is 42",               # `final` mid-sentence
    "Hecho.",
])
def test_prose_that_merely_looks_like_it_is_left_alone(text):
    # The signature is a channel name run straight into the next word with no
    # space. Anything else is somebody's sentence.
    assert strip_think(text) == text

"""What routes a turn is what the person wrote, not what the portal put beside it.

A reminder of about 200 characters, arriving with the day's location alerts,
the person's location and a line about pending chores, was over the 600
characters the router's fast path reads as "work handed over" -- and was sent to
a background sub-agent, which took 77 seconds to set a reminder (2026-09-28).
"""
from nanobot.agent.classify import TurnClassifier
from nanobot.utils import standing_context as sc

WORDS = "Recuérdame mañana revisar el reflejo del lago"
NOTES = ("[Shown in this chat since the person last wrote: ...]\n" + "- 08:07 (location alert): Juana salió de casa.\n" * 20
         + "[User's current location: -33.00000,-70.00000 (±16m)]")
MSG = (f"{sc.OPEN}\nThe professions on offer...\n{sc.CLOSE}\n\n"
       f"{sc.TURN_OPEN}\n{NOTES}\n{sc.TURN_CLOSE}\n{WORDS}")


def test_routing_reads_only_the_persons_words():
    assert sc.for_routing(MSG) == WORDS


def test_the_model_and_history_still_have_the_notes():
    seen = sc.for_prompt(MSG)
    assert "location alert" in seen and WORDS in seen
    assert "[[[" not in seen
    stored = sc.for_history(MSG)
    assert "location alert" in stored and "professions" not in stored and "[[[" not in stored


def test_a_short_message_with_long_notes_is_not_work_handed_over():
    router = TurnClassifier(provider=None, model="x")
    assert len(MSG) > router.long_message_chars
    assert router.fast_path(sc.for_routing(MSG)) is None


def test_a_command_behind_the_location_is_still_a_command():
    msg = f"{sc.OPEN}\npersona\n{sc.CLOSE}\n\n{sc.TURN_OPEN}\n[User's current location: 0,0]\n{sc.TURN_CLOSE}\n/new"
    assert sc.for_routing(msg) == "/new"


def test_a_message_without_turn_context_routes_as_before():
    plain = f"{sc.OPEN}\npersona\n{sc.CLOSE}\n\nhola"
    assert sc.for_routing(plain) == sc.for_history(plain) == "hola"

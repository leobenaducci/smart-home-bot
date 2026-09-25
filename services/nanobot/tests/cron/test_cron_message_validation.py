"""A cron job's body has to still mean something when it fires.

Taken from production: a job sat queued for the next morning whose entire
message was "Recordatorio skill JSON invocation" — a fragment of a skill
invocation captured as the body during a confused turn. Nobody would have seen
it fail; it would simply have run that string as a task.
"""
import pytest

from nanobot.agent.tools.cron import CronTool

reject = CronTool._validate_message


# --- refused -------------------------------------------------------------


def test_rejects_the_observed_debris():
    err = reject("Recordatorio skill JSON invocation")
    assert err and "leftover plumbing" in err


def test_rejects_a_bare_invocation_block():
    err = reject('{"skill": "camera-feed", "action": "snapshot", "camera": "patio"}')
    assert err and "skill-invocation block" in err


def test_rejects_a_fenced_invocation_block():
    err = reject('```json\n{"skill": "menu", "action": "list_menu"}\n```')
    assert err and "skill-invocation block" in err


def test_rejects_a_block_buried_in_prose():
    """The model writes prose around the block; scheduling that still schedules
    a call that means nothing later."""
    err = reject('Mañana revisa esto:\n{"skill":"tasks","action":"list_chores"}')
    assert err and "skill-invocation block" in err


def test_rejects_something_too_short_to_act_on():
    assert reject("ok") is not None


# --- allowed -------------------------------------------------------------


@pytest.mark.parametrize("message", [
    "Sacar la basura",
    "Recuérdame comprar pan",
    "Saca una foto del patio y mándamela",
    "Revisa si llegó la boleta de la luz y avísame",
    "Tomar el medicamento de las 8",
    # mentions a skill by name, which is fine — it is an instruction, not a block
    "Usa la skill de cámaras para mirar el frente y contarme qué ve",
    # a reminder that merely contains braces
    "Llevar el {informe} impreso",
])
def test_allows_real_reminders(message):
    assert reject(message) is None, f"wrongly refused: {message!r}"


def test_advice_points_at_what_to_write_instead():
    """An error that only says no costs a turn; this one says what works."""
    err = reject('{"skill":"camera-feed","action":"snapshot"}')
    assert "en palabras" in err or "in words" in err or "Saca una foto" in err

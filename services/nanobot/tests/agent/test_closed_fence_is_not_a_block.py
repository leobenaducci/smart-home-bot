"""A reply that ends with a code block is an answer, not a broken invocation.

The stripper removes an opening fence left dangling at the end of a reply --
what remains when a fenced skill block is cut in half. Its pattern also matched
an ordinary *closing* fence, so every reply ending in a code block read as "a
skill-invocation block that resolved to no call": the turn was marked a bad
invocation, a note saying nothing was executed was appended, and it was handed
on. The Studio's music-video planner asks for a JSON array; Alfred answered with
eleven correct shots in a ```json block, and the portal never got them
(2026-09-28).
"""
from nanobot.agent.runner import _has_skill_invocation_text, _strip_skill_invocation_text

PLAN = ('```json\n[\n  {"prompt": "A pelican on a cliff at golden hour", "continues": false},\n'
        '  {"prompt": "The same pelican racing a dune", "continues": true}\n]\n```')


def test_a_closed_json_block_is_left_whole():
    assert _strip_skill_invocation_text(PLAN) == PLAN
    assert not _has_skill_invocation_text(PLAN)


def test_prose_then_a_closed_code_block():
    reply = "Here is the script:\n\n```python\nprint('hola')\n```"
    assert _strip_skill_invocation_text(reply) == reply
    assert not _has_skill_invocation_text(reply)


def test_a_dangling_opening_fence_is_still_removed():
    """The case the pattern exists for: a skill block cut off after its object,
    so only the opening fence is left at the end."""
    cut = 'Lo miro ahora.\n```json\n{"skill": "camera-feed", "action": "snapshot", "camera": "patio"}'
    assert _strip_skill_invocation_text(cut) == "Lo miro ahora."
    assert _has_skill_invocation_text(cut)


def test_a_fenced_skill_block_is_still_removed():
    block = 'Voy.\n```json\n{"skill": "menu", "action": "list_menu"}\n```'
    assert _strip_skill_invocation_text(block) == "Voy."

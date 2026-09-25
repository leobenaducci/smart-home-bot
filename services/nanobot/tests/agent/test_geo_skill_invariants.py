"""What `geo` must keep saying, whatever else is trimmed out of it.

`geo` is `always: true`, so its whole body sits in every prompt of every
conversation — it was the single largest item there, and it got shortened. This
pins the parts that are not prose. Each one is a rule whose loss is either
invisible or expensive:

- calling `locate` before `where_is` wakes somebody's phone and drains their
  battery, for a question `where_is` answers for free;
- offering to locate another person to a non-admin promises something the
  server answers with 403;
- confusing `share_location` with `track` points the wrong phone at the wrong
  person, and one of the two is admin-only;
- re-invoking the skill for a `[HomeCore system]` arrival re-asks a question
  whose answer is already in the message.

If a rule genuinely stops applying, delete its assertion in the same commit as
the wording — deliberately, not by discovering the test in the way.
"""

import re
from pathlib import Path

import pytest

from nanobot.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader

SKILL = Path(BUILTIN_SKILLS_DIR) / "geo" / "SKILL.md"
TEXT = SKILL.read_text(encoding="utf-8")
BODY = TEXT.split("---", 2)[2]
FRONTMATTER = TEXT.split("---", 2)[1]
# The file is hard-wrapped prose. Asserting on it verbatim would mean a test
# that fails when a paragraph is re-flowed — noise that teaches people to edit
# the assertions rather than read them. Every phrase check runs against this.
FLAT = " ".join(BODY.split())
FLAT_LOW = FLAT.lower()
# The description line only, without the YAML comment above it (which quotes the
# same convention it is asserting about).
DESCRIPTION = next(line for line in FRONTMATTER.splitlines()
                   if line.startswith("description:"))

ACTIONS = [
    "where_is", "list_places", "save_place", "delete_place",
    "list_reminders", "cancel_reminder", "add_reminder",
    "get_location", "locate", "track", "stop_track", "track_status",
    "share_location", "stop_sharing", "ring_phone", "stop_ring",
]


@pytest.mark.parametrize("action", ACTIONS)
def test_every_action_is_still_documented(action):
    assert re.search(rf"\b{action}\b", FLAT), f"{action} disappeared from geo"


@pytest.mark.parametrize("action", [
    "where_is", "save_place", "list_places", "delete_place",
    "add_reminder", "list_reminders", "cancel_reminder",
    "get_location", "locate", "track", "stop_track", "track_status",
    "share_location", "stop_sharing", "ring_phone", "stop_ring",
])
def test_every_action_has_a_copyable_json_example(action):
    """The model emits these blocks verbatim; a described-but-unshown action is
    one it has to guess the shape of."""
    assert re.search(rf'"action":\s*"{action}"', FLAT), f"no JSON example for {action}"


def test_where_is_is_the_default_and_locate_is_not():
    rule = FLAT_LOW.split("the rule that decides everything")[1][:400]
    assert "where_is" in rule, "the routing rule no longer leads with where_is"
    assert "never `locate`" in FLAT_LOW, "the ban on locate-as-first-answer is gone"


def test_the_in_left_unknown_reading_survives():
    for token in ('status:"in"', 'status:"left"', "known:false"):
        assert token in FLAT, f"{token} handling is gone — the age of the data is the answer"
    assert "don't offer `locate`" in FLAT_LOW, \
        "an old `in` is a good answer; without this it escalates for nothing"


def test_admin_only_and_403_are_stated():
    assert "403" in FLAT
    assert "admins only" in FLAT_LOW, "the admin-only rule is no longer stated"


def test_share_location_is_distinguished_from_track():
    assert "not admin-only" in FLAT_LOW, "share_location must say it is NOT admin-only"
    assert "never `track`" in FLAT_LOW, "sharing my location must not route to track"


def test_ring_phone_keeps_its_trigger_phrases():
    assert "i can't find my phone" in FLAT_LOW
    assert "on silent" in FLAT_LOW, "the point of ring_phone is that it works on silent"


def test_system_messages_must_not_re_invoke_the_skill():
    assert "[HomeCore system]" in FLAT
    assert "do NOT invoke this skill" in FLAT, \
        "a [HomeCore system] arrival would be re-queried"


def test_the_map_field_is_pasted_verbatim_and_where_is_has_none():
    assert "`map`" in FLAT
    assert "verbatim" in FLAT, "the map markdown must be pasted unmodified"
    assert "`where_is` never brings a map" in FLAT, \
        "without this, a saved place's centre is shown as somebody's position"


def test_coordinates_are_never_invented():
    assert "Don't invent coordinates" in FLAT
    assert "[User's current location:" in FLAT


def test_it_stays_always_loaded_and_invocable_by_json():
    assert '"always":true' in FRONTMATTER.replace(" ", "")
    assert DESCRIPTION.startswith('description: "Invoke with JSON'), \
        "the description must keep the prefix AGENTS.md tells the model to look for"


def test_the_interceptor_can_still_map_a_geo_block_to_its_path():
    """`_collect_skill_paths` reads name and path off the summary line. A
    description that grew a newline would break the regex and every
    {"skill":"geo"} block would stop routing."""
    from nanobot.agent.runner import _SKILL_PATH_RE
    import tempfile

    summary = SkillsLoader(Path(tempfile.mkdtemp())).build_skills_summary(only={"geo"})
    found = dict((m.group(1).strip().lower(), m.group(2)) for m in _SKILL_PATH_RE.finditer(summary))
    assert "geo" in found, f"geo no longer routable; summary was {summary[:200]!r}"
    assert found["geo"].endswith("geo/SKILL.md")


def test_it_stays_meaningfully_smaller_than_it_was():
    """It is in every prompt of every conversation; this is the budget that
    justified rewriting it. Well clear of the ~3950 it used to cost."""
    try:
        import tiktoken
    except ImportError:
        pytest.skip("tiktoken not installed")
    total = len(tiktoken.get_encoding("cl100k_base").encode(TEXT))
    assert total < 2800, f"geo is back up to {total} tokens per turn"

"""A recurring job must deliver into today's chat, as a conversation of its own.

HomeCore files an agent-event under the day inside the chat_id, and a cron job
stores the chat_id that was current when it was created. So the daily message
kept landing in the day the job was set up: the ntfy push arrived as normal,
which is exactly why it looked like the message never reached the chat at all.

The fourth segment names the conversation. Without one the message is stored
unstamped and inherits whatever conversation is on screen — the 6 AM greeting
tacked onto last night's chat — so each firing names a new one.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from nanobot.utils.homeweb_chat_id import retarget_for_delivery


# The moment these tests pretend it is. `now_ms` fixes the day as well as the
# conversation id, so every expectation below is derived from it rather than
# from the wall clock — which is what made two of these pass on the day they
# were written and fail at the next midnight.
FIRE = 1785751200000
LATER = 1785837600000


def today(tz="Etc/UTC"):
    return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")


def day_of(ms, tz="Etc/UTC"):
    return datetime.fromtimestamp(ms / 1000, ZoneInfo(tz)).strftime("%Y-%m-%d")


DAY = day_of(FIRE)


def test_a_stale_day_is_moved_to_today():
    out = retarget_for_delivery("homeweb:user1:2026-07-15",
                                "Etc/UTC", now_ms=FIRE)
    assert out == f"homeweb:user1:{DAY}:{FIRE}"


def test_the_stored_conversation_is_replaced_by_this_firing():
    """Not carried (it is weeks old) and not dropped either: an unstamped
    message inherits the conversation on screen, and a job running in its own
    session (cron:<id>) has no business joining one it never saw."""
    out = retarget_for_delivery("homeweb:user1:2026-07-15:1752610000000",
                                "Etc/UTC", now_ms=FIRE)
    assert out == f"homeweb:user1:{DAY}:{FIRE}"


def test_two_firings_land_in_two_conversations():
    a = retarget_for_delivery("homeweb:user1:2026-07-15",
                              "Etc/UTC", now_ms=FIRE)
    b = retarget_for_delivery("homeweb:user1:2026-07-15",
                              "Etc/UTC", now_ms=LATER)
    assert a != b


def test_the_conversation_defaults_to_now():
    out = retarget_for_delivery("homeweb:user1:2026-07-15", "Etc/UTC")
    conv = int(out.split(":")[3])
    assert abs(conv - int(datetime.now().timestamp() * 1000)) < 60_000


def test_a_chat_id_already_on_today_still_gets_a_fresh_conversation():
    """The day is right and the conversation still has to be new — this is the
    ordinary case for a daily job that fired yesterday too."""
    out = retarget_for_delivery(f"homeweb:user1:{DAY}",
                                "Etc/UTC", now_ms=FIRE)
    assert out == f"homeweb:user1:{DAY}:{FIRE}"


def test_the_user_segment_is_never_touched():
    """HomeCore re-checks this against the authenticated proxy user; nothing
    here may move a message into somebody else's history."""
    for user in ["user1", "user2", "user3"]:
        assert retarget_for_delivery(f"homeweb:{user}:2026-01-01",
                                     "Etc/UTC").split(":")[1] == user


def test_other_channels_are_left_alone():
    """Only HomeCore encodes a day in its address; every other channel owns its
    own and must not be rewritten."""
    for chat_id in ["direct", "cli", "telegram:12345", "wecom:abc", ""]:
        assert retarget_for_delivery(chat_id, "Etc/UTC") == chat_id
    assert retarget_for_delivery(None, "Etc/UTC") is None


def test_a_malformed_homeweb_id_is_left_alone():
    """Better an unchanged (wrong) target than an invented one — the caller
    still has something HomeCore will reject loudly."""
    for chat_id in ["homeweb:", "homeweb:onlyuser", "homeweb::2026-07-15"]:
        assert retarget_for_delivery(chat_id, "Etc/UTC") == chat_id


def test_an_unknown_timezone_still_delivers():
    """tzdata missing in a slim container must not stop a job delivering."""
    out = retarget_for_delivery("homeweb:user1:2026-07-15", "Not/AZone",
                                now_ms=FIRE)
    assert out.startswith("homeweb:user1:")
    assert len(out.split(":")[2]) == 10
    assert out.endswith(":{}".format(FIRE))


def test_no_timezone_falls_back_to_local_time():
    out = retarget_for_delivery("homeweb:user1:2026-07-15", None, now_ms=FIRE)
    local_day = datetime.fromtimestamp(FIRE / 1000).strftime("%Y-%m-%d")
    assert out == f"homeweb:user1:{local_day}:{FIRE}"


# --- Spaces (HomeCore's Profesiones) ------------------------------------------
# The fourth segment is the scope that routes a reply back into a profession.
# Replacing it with a conversation id delivers into the ordinary chat instead —
# so a reminder set while talking to the Profesor would come back answered by
# the normal Alfred, without the persona that promised to schedule it.
#
# A space holds conversations of its own (homeweb:<u>:<day>:<scope>:<start>),
# and that trailing id is dropped here on purpose: it is weeks old by the time a
# job fires. Landing without one, HomeCore stores the reply unstamped and it
# joins whatever conversation is open in that profession.

def test_a_space_scope_survives_the_rewrite():
    out = retarget_for_delivery("homeweb:user1:2026-07-15:edu",
                                "Etc/UTC", now_ms=FIRE)
    assert out == f"homeweb:user1:{DAY}:edu"


def test_every_space_scope_is_kept_and_the_day_still_moves():
    for scope in ("fin", "dev", "edu", "dsg"):
        out = retarget_for_delivery(f"homeweb:user1:2026-07-15:{scope}",
                                    "Etc/UTC", now_ms=FIRE)
        assert out == f"homeweb:user1:{DAY}:{scope}", scope


def test_two_firings_in_a_space_share_its_one_daily_context():
    """The opposite of the conversation case above, and deliberately so: both
    land unstamped in the same profession on the same day, so both join the
    conversation the person actually has open there."""
    a = retarget_for_delivery("homeweb:user1:2026-07-15:dev",
                              "Etc/UTC", now_ms=FIRE)
    b = retarget_for_delivery("homeweb:user1:2026-07-15:dev",
                              "Etc/UTC", now_ms=1785751260000)
    assert a == b


def test_a_conversation_inside_a_space_keeps_the_space_and_loses_the_id():
    """The 5th segment is a conversation from the day the job was created; a
    reply arriving weeks later must not reopen it. The profession still has to
    survive, or the answer surfaces in the ordinary chat."""
    for scope in ("fin", "dev", "edu", "dsg"):
        out = retarget_for_delivery(f"homeweb:user1:2026-07-15:{scope}:1752610000000",
                                    "Etc/UTC", now_ms=FIRE)
        assert out == f"homeweb:user1:{DAY}:{scope}", scope


def test_a_delegate_session_is_not_mistaken_for_a_conversation():
    """dlg-* is HomeCore's per-profession delegate. It has the shape of a space
    scope and is treated as one — which is right: what matters is that the id
    is not replaced by a bare conversation in the ordinary chat."""
    out = retarget_for_delivery("homeweb:user1:2026-07-15:dlg-dsg",
                                "Etc/UTC", now_ms=FIRE)
    assert out == f"homeweb:user1:{DAY}:dlg-dsg"


def test_a_machine_event_scope_is_still_replaced():
    """ev-* sessions are stored unstamped so they inherit the conversation on
    screen — exactly what a job must not do."""
    out = retarget_for_delivery("homeweb:user1:2026-07-15:ev-notif",
                                "Etc/UTC", now_ms=FIRE)
    assert out == f"homeweb:user1:{DAY}:{FIRE}"


# --- "a Profesión is never the fast model" ------------------------------------
# Interactive turns carry `profile` and `powerful` from HomeCore. Turns nanobot
# starts for itself — a cron delivery, a job reporting back — carry neither, so
# the loop reads the session id instead.

def test_a_space_session_is_recognised_with_and_without_a_conversation():
    from nanobot.utils.homeweb_chat_id import is_space_session
    for scope in ("fin", "dev", "edu", "dsg"):
        assert is_space_session(f"homeweb:user1:2026-08-04:{scope}"), scope
        assert is_space_session(f"homeweb:user1:2026-08-04:{scope}:1785900000000"), scope


def test_a_delegate_counts_as_one():
    """One profession working for another, presented as the caller's own work —
    the fast model would be least visible and most wrong there."""
    from nanobot.utils.homeweb_chat_id import is_space_session
    assert is_space_session("homeweb:user1:2026-08-04:dlg-dsg")


def test_the_ordinary_chat_is_not_a_space():
    from nanobot.utils.homeweb_chat_id import is_space_session
    for chat_id in ("homeweb:user1:2026-08-04",
                    "homeweb:user1:2026-08-04:1785900000000",
                    "homeweb:user1:2026-08-04:ev-notif",
                    "homeweb:user1:2026-08-04:ev-geo",
                    "telegram:12345", "cli", "direct", "", None):
        assert not is_space_session(chat_id), chat_id

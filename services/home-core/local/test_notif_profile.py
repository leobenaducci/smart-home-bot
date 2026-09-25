"""The notification turn names its own role, and that role has to exist.

Run: python local/test_notif_profile.py   (needs Flask; skips loudly without it)

Notification triage is the highest-volume turn in the house by an order of
magnitude — ~300 a day against a couple of dozen of everything else — and the
smallest: two thirds of them answer SILENCE in eight tokens. So it is the one
turn worth being able to point at a different model without touching the
geofence or the tasks turns, which are neither.

It names a **role**, never a model: `profile: "notificaciones"` goes in the body
and nanobot's `modelProfiles` decides what that means. The roster lives in one
file that way, and changing it is a nanobot restart rather than a HomeCore
redeploy.

The failure this pins is quiet, which is the only reason it needs pinning. An
unconfigured role is not an error anywhere — nanobot falls back to the default
model on purpose, so that naming a role can never take a turn down. Which means
a typo here, or a role removed from the roster over there, costs nothing visible
and simply stops routing. The two halves live in different repositories, so
nothing else would notice.
"""
import json
import os
import shutil
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-notifprofile-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)

USER1 = "user1"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}]' % USER1)

sys.path.insert(0, dst)
import app as A  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


class FakeResponse:
    ok = True

    @staticmethod
    def json():
        return {"choices": [{"message": {"content": "SILENCE"}}]}


sent = []


def fake_post(url, headers=None, json=None, timeout=None):
    sent.append(json or {})
    return FakeResponse()


A.requests.post = fake_post


def body_of(**kw):
    sent.clear()
    A._alfred_notify(USER1, "hola", persist=False, **kw)
    assert sent, "nothing was sent"
    return sent[-1]


print("what goes in the request body")
check("a turn with a role names it",
      body_of(scope="ev-notif", profile="notificaciones").get("profile") == "notificaciones")
# Absent rather than null: nanobot reads a role off the body, and an explicit
# null is a value somebody has to remember to handle.
check("a turn without one omits the key entirely",
      "profile" not in body_of(scope="ev-geo"),
      body_of(scope="ev-geo"))
check("the session is still the isolated one",
      body_of(scope="ev-notif", profile="notificaciones")["session_id"].endswith(":ev-notif"))

print("\nand the notification turn is the one that carries it")
check("NOTIF_PROFILE is set", bool(A.NOTIF_PROFILE), A.NOTIF_PROFILE)
src = open(os.path.join(SRC, "app.py"), encoding="utf-8").read()
check("the triage call passes it", "scope=scope, profile=NOTIF_PROFILE" in src,
      "the highest-volume turn would silently run on the default model")
# `scope=scope`, not the literal: triage rotates its session every
# NOTIF_SESSION_TURNS turns so it cannot re-send its whole day (see
# `_notif_scope`). The role it names is what this file is about and does not
# rotate with it.
check("  and the scope it passes is the rotating one",
      "scope = _notif_scope(username)" in src,
      "without rotation the session carries the whole day again")

# The geofence and tasks turns got their own role on 2026-09-02, which is the
# decision the previous version of this check was holding the door open for:
# they are bounded where triage is not -- 631 turns measured, largest 32,416
# tokens, none above 60k -- so they are the household job a small local model
# can hold, and sharing `everyday` with 972k-token chat was what stopped that.
# They must name EVENT_PROFILE and not borrow triage's.
# The scope goes through `_event_scope` rather than being written literally:
# an events session rotates every 6h so it cannot grow all day, the same reason
# triage has `_notif_scope`. What this test is really protecting is the
# *profile* -- that events name their own role and do not borrow triage's --
# so it checks the rotated form and still refuses the borrowed one.
for scope in ("ev-geo", "ev-task"):
    check(f"{scope} names the events role",
          f"scope=_event_scope('{scope}'), profile=EVENT_PROFILE" in src)
    check(f"  and {scope} does not borrow triage's",
          f"scope=_event_scope('{scope}'), profile=NOTIF_PROFILE" not in src
          and f"scope='{scope}', profile=NOTIF_PROFILE" not in src)
    check(f"  and {scope} rotates rather than growing all day",
          f"scope='{scope}'," not in src)

print("\nand both roles exist in the roster nanobot actually loads")
cfg = os.path.join(os.path.dirname(os.path.dirname(SRC)), "nanobot",
                   "config", "config.json")
if os.path.isfile(cfg):
    d = json.load(open(cfg, encoding="utf-8"))["agents"]["defaults"]
    profiles = d.get("modelProfiles") or {}
    providers = d.get("providerProfiles") or {}
    check(f"modelProfiles has {A.NOTIF_PROFILE}", A.NOTIF_PROFILE in profiles,
          sorted(profiles))
    # The same guard for the events role. A profile HomeCore names and the
    # config does not declare reaches no turn -- the deployer warns and drops
    # it -- so the setting would read as configured and change nothing.
    check(f"modelProfiles has {A.EVENT_PROFILE}", A.EVENT_PROFILE in profiles,
          sorted(profiles))
    check(f"providerProfiles has {A.EVENT_PROFILE}", A.EVENT_PROFILE in providers,
          sorted(providers))
    # nanobot's own test asserts these two keysets match; checked from this side
    # as well because this is the file that decides the role name.
    check(f"providerProfiles has {A.NOTIF_PROFILE}", A.NOTIF_PROFILE in providers,
          sorted(providers))
else:
    print("  (no nanobot checkout alongside — skipping the roster check)")
    print("      This half only means something from inside the home-stack")
    print("      superproject, where both submodules are present.")

print("\nand a chat-only block never reaches a push notification")
# The blocks are drawn by the chat and are noise anywhere else. A phone buzzing
# with ":::si-no" on the lock screen is the reason the strip exists server-side
# and not only in the page.
for raw, want in [
    (':::si-no\nq: ¿Le aviso a Sam?\n:::', '¿Le aviso a Sam?'),
    (':::si-no\n¿Lo agrego a la lista?\n:::', '¿Lo agrego a la lista?'),
    ('Listo.\n\n:::si-no\nq: ¿Algo más?\n:::', 'Listo.\n\n¿Algo más?'),
    (':::ask\nq: ¿Para qué curso?\n- 3°\n- 5°\n:::', '¿Para qué curso?'),
]:
    got = A._strip_ui_blocks(raw)
    check(f'{raw.splitlines()[0]} -> {want!r}', got == want, repr(got))
check("and the fence itself never survives",
      ':::' not in A._strip_ui_blocks(':::si-no\nq: ¿Sí?\n:::'))

print("\nthe word the prompt asks for is the word the filter hears")
# The prompt says answer EXACTLY NOTIF_SILENT_TOKEN; the filter only knew
# `silencio`, so 21 correct silent verdicts reached one member's chat as the
# message "SILENCE". Tied to the constant, so the two cannot drift apart again.
for _reply in (A.NOTIF_SILENT_TOKEN, A.NOTIF_SILENT_TOKEN + ".", A.NOTIF_SILENT_TOKEN.lower(),
               "silencio"):
    check(f"  {_reply!r} is silence", bool(A._NOTIF_SILENT_RE.match(_reply)))
for _reply in ("Mora te escribió: llega tarde", "Silence is golden, dijo tu jefe"):
    check(f"  {_reply!r} is spoken", not A._NOTIF_SILENT_RE.match(_reply))

print()
shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")

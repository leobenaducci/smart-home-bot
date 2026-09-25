"""One notification per person, and none at all for an out-and-back.

Run: python local/test_geo_collapse.py   (needs Flask; skips loudly without it)

On 2026-08-16 Alex's chat carried four movement alerts for one afternoon —
"Robin llegó a la casa de Sonia" at 12:00, again at 12:12, "salió" at 12:14,
"llegó" at 12:22 — plus Sam leaving home at 12:17 and arriving at 12:18. The
Sam pair is a minute apart: somebody walked to the car. That is not news, and
neither is the third telling of where Robin is.

So an alert about somebody ELSE's movement waits before it goes anywhere, and
while it waits:

- a newer crossing for the same person REPLACES it, because only the latest
  state is still true;
- the opposite crossing within five minutes CANCELS it and itself.

A reminder somebody set for themselves is not held. "Comprar pan cuando llegue
a casa" delivered five minutes late is five minutes after they left the shop —
that is content, not news, and the distinction is the whole design.

The windows are shrunk to fractions of a second here. What is being tested is
the state machine, not the clock.
"""
import os
import shutil
import sys
import tempfile
import time

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-geocollapse-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)

USER1 = "user1"
USER2 = "user2"
USER3 = "user3"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}, {"username": "%s", "nanobot_id": 3},'
            ' {"username": "%s", "nanobot_id": 1}]' % (USER1, USER2, USER3))

sys.path.insert(0, dst)
import app as A  # noqa: E402

# Fractions of a second, so the suite runs in about one.
A.GEO_COLLAPSE_S = 0.30
A.GEO_ROUNDTRIP_S = 0.30
A.GEO_MAX_HOLD_S = 0.90

sent = []
A._geo_deliver = lambda *a: sent.append(a)

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def move(target, direction, place, notify=USER1, text="avísame"):
    A._geo_hold_or_deliver(notify, target, text, place, direction)


def settle(t=0.55):
    time.sleep(t)


def reset():
    for e in list(A._geo_pending.values()):
        e['timer'].cancel()
    A._geo_pending.clear()
    sent.clear()


# --- The out-and-back ----------------------------------------------------------
print("\na minute out and back is not news")
reset()
move(USER2, 'exit', 'casa')
move(USER2, 'enter', 'casa')
settle()
check("Sam leaving and arriving cancels both", sent == [], sent)

reset()
move(USER3, 'enter', 'la casa de Sonia')
move(USER3, 'exit', 'la casa de Sonia')
settle()
check("and so does arriving then leaving", sent == [], sent)

reset()
move(USER2, 'exit', 'casa')
time.sleep(0.35)          # past the window: a real errand, not a wobble
move(USER2, 'enter', 'casa')
settle()
# Both, and that is right: the trip took longer than the window, so by the time
# she came back "Sam salió" had already been sent and was true when it went.
# This also pins the relationship between the two constants — the cancel can
# only ever fire while BOTH halves are still pending, so the effective
# round-trip window is min(GEO_ROUNDTRIP_S, GEO_COLLAPSE_S) and setting the
# former larger than the latter would buy nothing at all.
check("a trip longer than the window is told as two, having been true when sent",
      [s[4] for s in sent] == ['exit', 'enter'], sent)


# --- Only the latest -----------------------------------------------------------
print("\nonly the last one")
reset()
move(USER3, 'enter', 'la casa de Sonia')
move(USER3, 'enter', 'la casa de Sonia')
move(USER3, 'enter', 'el colegio')
settle()
check("three arrivals become one", len(sent) == 1, sent)
check("and it is the last place, not the first",
      sent and sent[0][3] == 'el colegio', sent)

reset()
move(USER3, 'enter', 'el colegio')
move(USER2, 'enter', 'casa')
settle()
check("two different people are two notifications", len(sent) == 2, sent)

reset()
move(USER3, 'enter', 'el colegio', notify=USER1)
move(USER3, 'enter', 'el colegio', notify=USER2)
settle()
check("the same movement told to both parents stays two",
      len(sent) == 2 and {s[0] for s in sent} == {USER1, USER2}, sent)


# --- Somebody who never stops --------------------------------------------------
print("\nsomebody crossing a fence all afternoon")
reset()
t0 = time.time()
while time.time() - t0 < 1.15:      # longer than GEO_MAX_HOLD_S
    move(USER3, 'enter', 'el mall')
    time.sleep(0.12)                # always sooner than the collapse window
settle()
check("does not defer their own alert forever", len(sent) >= 1, sent)


# --- A reminder is not an alert ------------------------------------------------
print("\nyour own reminder is content, not news")
reset()
A._geo_hold_or_deliver(USER1, USER1, 'comprar pan', 'casa', 'enter')
check("it goes immediately, with no wait at all", len(sent) == 1, sent)
A._geo_hold_or_deliver(USER1, USER1, 'comprar pan', 'casa', 'exit')
check("and a second one is not swallowed by the first", len(sent) == 2, sent)


# --- Still wired to the real path ----------------------------------------------
print("\nthe fan-out still goes through it")
reset()
A._geo_deliver_all([(USER1, USER3, 'avísame', 'el colegio', 'enter')])
check("nothing is sent yet", sent == [], sent)
settle()
check("and it arrives once the window closes", len(sent) == 1, sent)

reset()
A._geo_deliver_all([(USER1, USER3, 'x', 'casa', 'exit'),
                    (USER1, USER3, 'x', 'casa', 'enter')])
settle()
check("a pair fired together cancels too — this is the Sam case", sent == [], sent)

for e in list(A._geo_pending.values()):
    e['timer'].cancel()
shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)

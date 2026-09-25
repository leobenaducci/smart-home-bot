"""A day's history is that day's, and no older.

Run: python local/test_history_day_purity.py   (needs Flask; skips loudly without it)

Reported from the house on 2026-08-18: open Alfred on a new chat, say hello, go
to «Consumo de Alfred», press back — and the chat is showing a conversation from
weeks earlier. It survived two fixes aimed at the wrong thing (a stale cached
page, then a deep link re-firing) because it was never about how the page was
reached. The day's own history said it began in July.

`load_user_history` used to fall back to the pre-per-day single file when a day
file did not exist yet, so "history isn't visibly lost on the first day of the
new model". That day was in July. What it did afterwards, every day, was hand
the July archive to the first caller of the morning — and the first caller is
usually `append_user_history`, which appends one message to whatever it was
given and saves the lot as today.

Measured before the fix: `2026-08-18.json` held 245 messages, 197 of them dated
14–23 July, and the day before held the same 197. Each day copied them to the
next.

Two properties, and the second is the one that keeps the cure from being worse
than the disease: nothing older than the day survives the read, and a
conversation that runs past midnight still does — `_writing_day` decides which
day a late message belongs to, and this must not second-guess it.
"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import timedelta

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-histpure-")
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


TODAY = A._tasks_today()
DAY = TODAY.isoformat()
MID = A._day_floor_ms(DAY)
JULY = A._day_floor_ms((TODAY - timedelta(days=27)).isoformat())


def write_day(day, msgs):
    path = A._history_path(USER1, day, None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(msgs, f)


print("what a day file may hand back")
write_day(DAY, [
    {"role": "user", "text": "de julio", "ts": JULY + 1000},
    {"role": "bot", "text": "también de julio", "ts": JULY + 2000},
    {"role": "user", "text": "Hola", "ts": MID + 9 * 3600 * 1000},
    {"role": "bot", "text": "¡Hola, señor Alex!", "ts": MID + 9 * 3600 * 1000 + 500},
])
got = A.load_user_history(USER1, DAY)
check("July does not come along", [m["text"] for m in got] == ["Hola", "¡Hola, señor Alex!"], got)

# The cut is at midnight, not at "same calendar date": a conversation running
# past midnight files a message or two into the day it started in, and
# _writing_day is what decides that.
write_day(DAY, [
    {"role": "user", "text": "justo antes de medianoche", "ts": MID + 86399_000},
    {"role": "bot", "text": "y la respuesta, ya pasada", "ts": MID + 86401_000},
])
got = A.load_user_history(USER1, DAY)
check("a conversation running past midnight is kept whole", len(got) == 2, got)

write_day(DAY, [{"role": "user", "text": "justo en el filo", "ts": MID}])
check("midnight itself belongs to the day", len(A.load_user_history(USER1, DAY)) == 1)
write_day(DAY, [{"role": "user", "text": "un segundo antes", "ts": MID - 1}])
check("a millisecond before it does not", A.load_user_history(USER1, DAY) == [])

print("\nand the legacy file is never read again")
# This is the whole bug: the fallback handed July to append_user_history, which
# saved it as today, and every day copied it to the next.
legacy = A._legacy_history_path(USER1)
os.makedirs(os.path.dirname(legacy), exist_ok=True)
with open(legacy, "w", encoding="utf-8") as f:
    json.dump([{"role": "user", "text": "del archivo viejo", "ts": JULY}], f)
os.remove(A._history_path(USER1, DAY, None))
check("a day with no file is empty, not July", A.load_user_history(USER1, DAY) == [],
      A.load_user_history(USER1, DAY))

A.append_user_history(USER1, {"role": "user", "text": "Hola", "ts": MID + 3600_000}, DAY)
saved = json.load(open(A._history_path(USER1, DAY, None), encoding="utf-8"))
check("so the first message of the day writes a file with one message in it",
      [m["text"] for m in saved] == ["Hola"], saved)
check("and the old file is still on disk, undeleted", os.path.exists(legacy))

print("\nand a day that never had one is still empty")
check("no file at all", A.load_user_history(USER1, "2026-01-01") == [])

print("\nthe archive is filed under the days it happened on, not dropped")
# Removing the fallback alone would have hidden a real week: the messages it
# carried have no day files of their own. They exist only inside the blob — and
# inside every later day file that inherited a copy of it.
import glob
for f in glob.glob(os.path.join(os.path.dirname(A._history_path(USER1, DAY, None)), '*.json')):
    os.remove(f)
old_day = (TODAY - timedelta(days=27)).isoformat()
newer_day = (TODAY - timedelta(days=2)).isoformat()
write_day(newer_day, [{"role": "user", "text": "el archivo de ese día manda",
                       "ts": A._day_floor_ms(newer_day) + 1000}])
with open(A._legacy_history_path(USER1), "w", encoding="utf-8") as f:
    json.dump([
        {"role": "user", "text": "de julio", "ts": JULY + 1000},
        {"role": "bot", "text": "también", "ts": JULY + 2000},
        {"role": "user", "text": "otro día", "ts": A._day_floor_ms(old_day) + 86400_000 + 5000},
        {"role": "user", "text": "el mismo día que ya tiene archivo",
         "ts": A._day_floor_ms(newer_day) + 2000},
    ], f)

check("it reports what it filed", A.migrate_legacy_history() == 2)
check("July is browsable under July",
      [m["text"] for m in A.load_user_history(USER1, old_day)] == ["de julio", "también"],
      A.load_user_history(USER1, old_day))
# A day that already has a file was recorded properly, and its file is the
# truth — the archive must not be allowed to overwrite it.
check("a day that already had a file is untouched",
      [m["text"] for m in A.load_user_history(USER1, newer_day)] == ["el archivo de ese día manda"],
      A.load_user_history(USER1, newer_day))
check("nothing is deleted, only renamed aside",
      not os.path.exists(A._legacy_history_path(USER1))
      and os.path.exists(A._legacy_history_path(USER1) + ".migrated"))
check("and running it again does nothing", A.migrate_legacy_history() == 0)

print()
shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")

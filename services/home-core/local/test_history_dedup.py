"""One answer, filed twice, must land once.

Run: python local/test_history_dedup.py   (needs Flask; skips loudly without it)

Reported as «Alfred repite la respuesta», with the second copy carrying
`{"skill":"finance","action":"sync"}` in front of it.

Both halves of that are one bug. Two things file every reply — the page
persists what it received, the server persists what it kept — which is
deliberate, because either can end up being the only one that survives. The
dedup decided they were the same message by comparing the two strings, and the
server stripped the skill-invocation block while the page did not. Different
strings, so both were kept: the answer twice, once with the plumbing showing.

The page strips now (chat.html, both turn paths). These pin the other half:
the server no longer trusts the two sides to produce byte-identical text.
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

tmp = tempfile.mkdtemp(prefix="homecore-dedup-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)
USER = "user1"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}]' % USER)
sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_bgtask_db()
A.init_persona_db()

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


TODAY = A._tasks_today().isoformat()
ANSWER = ("Sincronización completa, señor Alex. Recorrió 13 archivos de cartolas.\n\n"
          "Quedan 15 movimientos sin categorizar.")
DIRTY = '{"skill":"finance","action":"sync"}' + ANSWER

day = [0]


def fresh_day():
    """A day of its own per case, so one case cannot dedup against another."""
    day[0] += 1
    return f"2026-01-{day[0]:02d}"


def texts(d):
    return [m["text"] for m in A.load_user_history(USER, d) if m["role"] == "bot"]


print("the reported bug: the same answer, one copy still carrying the block")
d = fresh_day()
ts = int(time.time() * 1000)
A.append_user_history(USER, {"role": "bot", "text": DIRTY, "ts": ts}, d)
A.append_user_history(USER, {"role": "bot", "text": ANSWER, "ts": ts + 400}, d)
check("it is filed once, not twice", len(texts(d)) == 1, texts(d))
check("and what is kept has no plumbing in it",
      '"skill"' not in (texts(d)[0] if texts(d) else "?"), texts(d))

print("\nand in the other order — whichever of the two writers gets there first")
d = fresh_day()
ts = int(time.time() * 1000)
A.append_user_history(USER, {"role": "bot", "text": ANSWER, "ts": ts}, d)
A.append_user_history(USER, {"role": "bot", "text": DIRTY, "ts": ts + 400}, d)
check("still once", len(texts(d)) == 1, texts(d))
check("still clean", '"skill"' not in (texts(d)[0] if texts(d) else "?"), texts(d))

print("\na trailing newline is not a different message either")
d = fresh_day()
ts = int(time.time() * 1000)
A.append_user_history(USER, {"role": "bot", "text": ANSWER, "ts": ts}, d)
A.append_user_history(USER, {"role": "bot", "text": ANSWER + "\n\n", "ts": ts + 400}, d)
check("collapsed to one", len(texts(d)) == 1, texts(d))

print("\nbut two answers that really are different both stay")
d = fresh_day()
ts = int(time.time() * 1000)
A.append_user_history(USER, {"role": "bot", "text": "Listo.", "ts": ts}, d)
A.append_user_history(USER, {"role": "bot", "text": "Listo, y algo más.", "ts": ts + 400}, d)
check("both kept", len(texts(d)) == 2, texts(d))

print("\nand the same answer an hour later is a new answer")
d = fresh_day()
ts = int(time.time() * 1000)
A.append_user_history(USER, {"role": "bot", "text": ANSWER, "ts": ts}, d)
A.append_user_history(USER, {"role": "bot", "text": ANSWER,
                             "ts": ts + A.HISTORY_DEDUP_WINDOW_MS + 1000}, d)
check("both kept", len(texts(d)) == 2, texts(d))

print("\na reply that is nothing but a skill block is not a reply")
d = fresh_day()
ts = int(time.time() * 1000)
A.append_user_history(USER, {"role": "bot", "text": "Hola.", "ts": ts}, d)
A.append_user_history(USER, {"role": "bot",
                             "text": '{"skill":"finance","action":"sync"}', "ts": ts + 10}, d)
# Nothing is left of it after the strip, so there is no reply to file — and
# the message that came before it is untouched.
check("it is not filed at all", texts(d) == ["Hola."], texts(d))

print("\nand a person quoting JSON at Alfred keeps their own words")
d = fresh_day()
mine = 'probá con {"skill":"finance","action":"sync"} a ver qué pasa'
A.append_user_history(USER, {"role": "user", "text": mine,
                             "ts": int(time.time() * 1000)}, d)
kept = [m["text"] for m in A.load_user_history(USER, d) if m["role"] == "user"]
check("stored verbatim", kept == [mine], kept)

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")

"""Tasks nobody decided about, and the Sunday report that says so.

Run: python local/test_tasks_auto.py   (needs Flask; skips loudly without it)

Two ways a task sits forever, and neither is a decision:

- a completed one waits in `review` for a parent to look at it, and while
  nobody does, the kid did the chore and is still waiting for points. After
  two days it approves itself.
- a `pending` one days past its due date is not going to be done — reminders
  stopped at the end of its day — and just accumulates as a small reproach.
  After two days it closes itself as `excused`.

The dangerous half is the first: it **grants points**, and a sweep that runs
every five minutes must never grant them twice. The ledger's partial unique
index on `(ref_type, ref_id)` is the real guard and this leans on it directly.

The Sunday report is the accountability half. Auto-approving quietly would mean
a parent never learning that a week of chores went through unlooked-at, so the
report counts those separately from the ones somebody actually reviewed — and
its numbers are computed in Python rather than left to a model to restate.
"""
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-tasksauto-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32,
                  # Who lives here and who is a parent. Both were literals in
                  # app.py -- `ADVANCED_USERS = {...}` -- and are supplied by
                  # the deployer now, so a suite that does not say leaves the
                  # house with no adults in it and every admin route answers
                  # 403 with "Solo administradores".
                  HOMECORE_MEMBERS="user1,user2,user3,user4",
                  HOMECORE_ADMIN_MEMBERS="user1,user2")

USER1 = "user1"      # admin
USER2 = "user2"     # admin
USER4 = "user4"
USER3 = "user3"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}, {"username": "%s", "nanobot_id": 3},'
            ' {"username": "%s", "nanobot_id": 4}, {"username": "%s", "nanobot_id": 1}]'
            % (USER1, USER2, USER4, USER3))

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_tasks_db()

pushed = []
alfred = []
A._notify_user = lambda u, msg, **kw: pushed.append((u, msg))
A._notify_tasks_admins = lambda *a, **kw: None
A._alfred_notify = lambda u, prompt, **kw: (alfred.append((u, prompt)), '')[1]
A._user_watching = lambda u: False

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


NOW = datetime.now(A.TASKS_TZ)
TODAY = NOW.date()


def add(assignee, title, status='pending', points=10, due=None, completed_days_ago=None,
        excuse_note=''):
    conn = A._tasks_conn()
    try:
        completed = (int(NOW.timestamp()) - completed_days_ago * 86400
                     if completed_days_ago is not None else None)
        cur = conn.execute(
            'INSERT INTO tasks (title, points, assignee, due_date, status, completed_at, '
            'excuse_note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)',
            (title, points, assignee, (due or TODAY).isoformat(), status, completed,
             excuse_note, USER1, int(NOW.timestamp())))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def task(task_id):
    conn = A._tasks_conn()
    try:
        return conn.execute(
            'SELECT status, reviewed_by, excuse_note FROM tasks WHERE id = ?',
            (task_id,)).fetchone()
    finally:
        conn.close()


def balance(user):
    conn = A._tasks_conn()
    try:
        return A._points_balance(conn, user)
    finally:
        conn.close()


def sweep(when=None):
    conn = A._tasks_conn()
    try:
        return A._tasks_auto_resolve(conn, when or NOW, (when or NOW).date())
    finally:
        conn.close()


# --- Review that nobody looked at ---------------------------------------------
print("\na chore nobody reviewed")
# Due inside the report window (7 days ending yesterday) so the Sunday report
# below can see them; the auto-excuse sweep only touches `pending`, so an old
# due date is inert for a task already in review.
IN_WEEK = TODAY - timedelta(days=3)
fresh = add(USER4, 'ordenar la pieza', status='review', completed_days_ago=1, due=IN_WEEK)
stale = add(USER4, 'sacar la basura', status='review', completed_days_ago=3, points=15,
            due=IN_WEEK)
sweep()
check("one day in review is left alone", task(fresh)[0] == 'review', task(fresh))
check("three days approves itself", task(stale)[0] == 'approved', task(stale))
check("and it is marked as the system's doing, not a parent's",
      task(stale)[1] == 'sistema', task(stale))
check("the points are granted", balance(USER4) == 15, balance(USER4))
check("and the kid is told, because they earned them",
      any(u == USER4 and 'approved' in m.lower() for u, m in pushed), pushed)

before = len(pushed)
sweep()
sweep()
check("running the sweep again grants nothing more", balance(USER4) == 15, balance(USER4))
check("and says nothing more", len(pushed) == before, pushed[before:])

print("\nexactly on the boundary")
edge = add(USER3, 'justo dos días', status='review', due=IN_WEEK,
           completed_days_ago=A.TASKS_AUTO_APPROVE_DAYS)
sweep()
check("two days is enough — the rule is 'after 2 days', inclusive",
      task(edge)[0] == 'approved', task(edge))

print("\na task worth nothing still closes")
free = add(USER3, 'tarea sin puntos', status='review', completed_days_ago=3, points=0)
b = balance(USER3)
sweep()
check("it is approved", task(free)[0] == 'approved', task(free))
check("and the ledger is untouched", balance(USER3) == b, balance(USER3))


# --- Overdue that nobody did ---------------------------------------------------
print("\na chore nobody did")
today_task = add(USER4, 'de hoy', due=TODAY)
yesterday = add(USER4, 'de ayer', due=TODAY - timedelta(days=1))
old = add(USER4, 'de hace tres días', due=TODAY - timedelta(days=3))
edge_due = add(USER4, 'de hace dos días', due=TODAY - timedelta(days=2))
sweep()
check("today's task is untouched", task(today_task)[0] == 'pending', task(today_task))
check("yesterday's is untouched", task(yesterday)[0] == 'pending', task(yesterday))
check("two days late closes itself", task(edge_due)[0] == 'excused', task(edge_due))
check("three days late too", task(old)[0] == 'excused', task(old))
check("with a note that says the system did it, not the kid",
      'itself' in task(old)[2], task(old))
# Was `== 15` until 2026-08-19: a task nobody did used to be worth exactly
# nothing, which made ignoring a chore and explaining yourself identical
# outcomes. It now costs half its points. Two of the four above closed as
# excused (`de hace tres días` and `de hace dos días`), 10 points each, so 10
# was charged. The rules themselves live in test_task_penalty.py; this line
# only keeps this file honest about the balance it is asserting on.
check("a task nobody did now costs half its points", balance(USER4) == 5, balance(USER4))

print("\nan excused task is not re-excused, and never grants points")
kid_excused = add(USER3, 'me enfermé', status='excused', excuse_note='estaba enferma')
sweep()
check("the kid's own excuse survives untouched",
      task(kid_excused) == ('excused', None, 'estaba enferma'), task(kid_excused))


# --- The Sunday report ---------------------------------------------------------
print("\nthe Sunday report")


def weekly(at):
    conn = A._tasks_conn()
    try:
        A._tasks_send_weekly_report(conn, at, at.date())
        conn.commit()
    finally:
        conn.close()


def next_weekday(d, weekday):
    return d + timedelta(days=(weekday - d.weekday()) % 7)


sunday = next_weekday(TODAY, A.TASKS_WEEKLY_REPORT_WEEKDAY)
sun_am = datetime.combine(sunday, datetime.min.time(),
                          tzinfo=A.TASKS_TZ).replace(hour=A.TASKS_NOTIFY_HOUR + 1)

# Every row this section counts is pinned to the report's own window — the
# seven days ending the Saturday before that Sunday — and not to today. Hung
# off today, these checks passed or failed depending on which weekday the suite
# happened to run on, which is the worst kind of green: right on the day it was
# written and quietly wrong afterwards.
in_week = sunday - timedelta(days=2)
auto_note = A.TASKS_AUTO_EXCUSE_NOTE % A.TASKS_AUTO_EXCUSE_DAYS
add(USER4, 'se cerró sola', status='excused', excuse_note=auto_note, due=in_week)
add(USER3, 'la aprobó el sistema', status='approved', due=in_week)
conn = A._tasks_conn()
try:
    conn.execute("UPDATE tasks SET reviewed_by = 'sistema' "
                 "WHERE title = 'la aprobó el sistema'")
    conn.commit()
finally:
    conn.close()

alfred.clear()
weekly(sun_am.replace(hour=A.TASKS_NOTIFY_HOUR - 1))
check("nothing goes out before the notify hour", alfred == [], alfred)

saturday = sun_am - timedelta(days=1)
weekly(saturday)
check("and nothing on a Saturday", alfred == [], alfred)

weekly(sun_am)
check("Sunday morning it goes to both adults",
      {u for u, _ in alfred} == {USER1, USER2}, [u for u, _ in alfred])
check("and to nobody else", len(alfred) == 2, alfred)

sent = alfred[0][1]
check("it names the week it covers", 'Resumen de la semana' in sent, sent[:80])
check("it tells the model not to touch the numbers",
      'do not change any number' in sent, sent[:200])
check("it counts what was approved without anybody looking",
      'aprobadas solas' in sent, sent)
check("and what simply ran out of time", 'vencidas' in sent, sent)

before = len(alfred)
weekly(sun_am)
weekly(sun_am.replace(hour=A.TASKS_NOTIFY_HOUR + 5))
check("it is sent once a day, not once a tick", len(alfred) == before, alfred[before:])

print("\nwhen a week had nothing in it")
conn = A._tasks_conn()
try:
    conn.execute('DELETE FROM tasks')
    conn.execute("DELETE FROM tasks_meta WHERE key = 'last_weekly_report'")
    conn.commit()
finally:
    conn.close()
alfred.clear()
weekly(sun_am)
check("no report rather than an empty one", alfred == [], alfred)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)

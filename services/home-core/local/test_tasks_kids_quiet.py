"""Children get no chore reminders after 22:00; parents keep each chore's window.

Run: python local/test_tasks_kids_quiet.py   (needs Flask; skips loudly without it)

A chore with no end time reminded until midnight. One child got 17 reminders
after 22:00 in a week, the last at 23:34. TASKS_KIDS_REMIND_UNTIL stops them
for anyone who is not a parent; a parent's own late chore still reminds.

Invented household (CLAUDE.md): member Tomi, parent Paula.
"""
import os
import shutil
import sys
import tempfile
from datetime import datetime

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-kidsquiet-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32,
                  HOMECORE_MEMBERS="user1,user2",
                  HOMECORE_ADMIN_MEMBERS="user1",
                  HOMECORE_MEMBER_FOLDERS="user1:paula,user2:tomi")
os.environ.pop("TASKS_KIDS_REMIND_UNTIL", None)

PARENT, TOMI = "user1", "user2"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "member": "user1", "nanobot_id": 1},'
            ' {"username": "%s", "member": "user2", "nanobot_id": 2}]' % (PARENT, TOMI))

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_tasks_db()
asked = []
A._alfred_notify = lambda user, prompt, **kw: asked.append(user) or "¡A dormir!"
A._notify_user = lambda *a, **kw: None
A._user_watching = lambda u: True

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


DAY = "2026-11-16"


def reminded_at(hhmm, assignee, start="21:00", end=None):
    conn = A._tasks_conn()
    try:
        conn.execute("DELETE FROM tasks")
        conn.execute("INSERT INTO tasks (title, points, time_start, time_end, assignee, due_date, "
                     "created_by, created_at) VALUES ('Acostarse', 5, ?, ?, ?, ?, ?, 0)",
                     (start, end, assignee, DAY, PARENT))
        conn.commit()
        asked.clear()
        h, m = map(int, hhmm.split(":"))
        A._tasks_send_reminders(conn, datetime(2026, 11, 16, h, m, tzinfo=A.TASKS_TZ),
                                datetime(2026, 11, 16).date())
        return asked == [assignee]
    finally:
        conn.close()


print("a child's chore with no end time")
check("reminds at 21:30", reminded_at("21:30", TOMI))
check("is quiet at 22:00", not reminded_at("22:00", TOMI))
check("is quiet at 23:34", not reminded_at("23:34", TOMI))

print("\na parent's late chore")
check("still reminds at 23:00", reminded_at("23:00", PARENT, start="22:30"))

print("\nthe chore's own end time still applies first")
check("a child's chore ending 21:15 is quiet at 21:30", not reminded_at("21:30", TOMI, end="21:15"))

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)

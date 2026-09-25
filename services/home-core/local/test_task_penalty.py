"""A task that ends unfinished costs points, unless an admin approves the excuse.

Run: python local/test_task_penalty.py   (needs Flask; skips loudly without it)

Asked for on 2026-08-19. Until then the only consequence of not doing a chore
was not being paid for it, which makes a 0-point outcome identical whether you
explained yourself or ignored it entirely.

The rules, and the reason each is here rather than the obvious alternative:

- **Half the points, floored.** A 5-point chore pays +5 and costs -2, so doing
  chores always pays much better than skipping them; the swing is 7, not 10.
  A 1-point chore costs nothing, because half of 1 floors to 0 and a token
  chore does not deserve a token punishment.

- **Only an accepted excuse clears it.** `accept-excuse` is the one endpoint
  that does, and it refunds a penalty already charged — accepting late has to
  mean the same as accepting on time, or the answer to "was this excusable?"
  would depend on how fast an adult reached the panel.

- **Silence costs points here, and pays them on the approve side.** An
  unreviewed *excuse* is charged after two days; an unreviewed *completed*
  task auto-approves and pays. That asymmetry is deliberate and was confirmed
  directly after being put to Alex as a contradiction — approval is the thing
  that clears a penalty and nothing may stand in for it. The check below pins
  it so nobody "fixes" it into symmetry by accident.

- **The floor is applied at charge time, not at display time.** Capping the
  *shown* balance at 0 was the obvious reading and is a trap: the ledger would
  keep summing below zero while the page showed 0, so the next chore earned
  would move the true sum from -7 to -2 and still display 0 — the kid does
  chores and watches nothing happen. Charging only what they have keeps the
  ledger a true running sum and makes every later point visible at once.

- **One task can cost you at most once**, whatever route it takes through the
  state machine. The ledger's partial unique index is the guard, the same one
  that makes double-granting impossible; this leans on it directly rather than
  on any bookkeeping in Python.
"""
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-penalty-")
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
                  HOMECORE_MEMBERS="user1,user3,user4",
                  HOMECORE_ADMIN_MEMBERS="user1")

USER1 = "user1"      # admin
USER4 = "user4"
USER3 = "user3"     # the broke one, kept apart from Kai on purpose: the
                       # zero-balance checks below wipe their ledger, and doing
                       # that to Kai would delete the very rows the weekly
                       # report is asserted on at the end.
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}, {"username": "%s", "nanobot_id": 4},'
            ' {"username": "%s", "nanobot_id": 1}]' % (USER1, USER4, USER3))

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_tasks_db()

pushed = []
A._notify_user = lambda u, msg, **kw: pushed.append((u, msg))
A._notify_tasks_admins = lambda *a, **kw: None
A._alfred_notify = lambda u, prompt, **kw: (A, '')[1]
A._user_watching = lambda u: False

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


NOW = datetime.now(A.TASKS_TZ)
TODAY = NOW.date()


def add(assignee, title, status='pending', points=10, due=None,
        excused_days_ago=None, excuse_note=''):
    conn = A._tasks_conn()
    try:
        excused = (int(NOW.timestamp()) - excused_days_ago * 86400
                   if excused_days_ago is not None else None)
        cur = conn.execute(
            'INSERT INTO tasks (title, points, assignee, due_date, status, '
            'excused_at, excuse_note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)',
            (title, points, assignee, (due or TODAY).isoformat(), status, excused,
             excuse_note, USER1, int(NOW.timestamp())))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def balance(user):
    conn = A._tasks_conn()
    try:
        return A._points_balance(conn, user)
    finally:
        conn.close()


def grant(user, pts):
    """Give somebody a starting balance, so a penalty has something to bite."""
    conn = A._tasks_conn()
    try:
        conn.execute('INSERT INTO points_ledger (user, delta, reason, ref_type, ref_id, '
                     'created_at) VALUES (?,?,?,?,?,?)',
                     (user, pts, 'saldo inicial', 'ajuste', None, int(NOW.timestamp())))
        conn.commit()
    finally:
        conn.close()


def sweep(when=None):
    conn = A._tasks_conn()
    try:
        w = when or NOW
        A._tasks_auto_resolve(conn, w, w.date())
        conn.commit()
    finally:
        conn.close()


def ledger(user, ref_type=None):
    conn = A._tasks_conn()
    try:
        q = 'SELECT delta, reason, ref_type FROM points_ledger WHERE user = ?'
        args = [user]
        if ref_type:
            q += ' AND ref_type = ?'
            args.append(ref_type)
        return conn.execute(q, args).fetchall()
    finally:
        conn.close()


print("what an unfinished task costs")
check("half the points, floored", A._task_penalty(5) == 2, A._task_penalty(5))
check("a 10-point chore costs 5", A._task_penalty(10) == 5, A._task_penalty(10))
# Not an edge case being tolerated — a deliberate answer. See the module docstring.
check("a 1-point chore costs nothing", A._task_penalty(1) == 0, A._task_penalty(1))
check("a 0-point chore costs nothing", A._task_penalty(0) == 0, A._task_penalty(0))
check("and nothing can make it negative", A._task_penalty(-5) == 0, A._task_penalty(-5))

print("\na task nobody did and nobody explained")
grant(USER4, 100)
old = (TODAY - timedelta(days=A.TASKS_AUTO_EXCUSE_DAYS + 1))
t1 = add(USER4, 'Sacar la basura', points=10, due=old)
before = balance(USER4)
sweep()
check("is charged", balance(USER4) == before - 5, f'{before} -> {balance(USER4)}')
check("and the ledger says which task",
      any(r[2] == 'penalty' and 'Sacar la basura' in r[1] for r in ledger(USER4)),
      ledger(USER4, 'penalty'))
check("and the kid is told, not just debited",
      any(u == USER4 and 'Sacar la basura' in m and '5' in m for u, m in pushed),
      pushed[-3:])

print("\nand the sweep runs every five minutes, so it must not charge twice")
after_one = balance(USER4)
sweep(); sweep()
check("three sweeps, one charge", balance(USER4) == after_one, balance(USER4))
check("one penalty row for that task",
      len([r for r in ledger(USER4, 'penalty') if 'Sacar la basura' in r[1]]) == 1)

print("\nan excuse an admin accepts costs nothing — this is what 'no pude' is for")
t2 = add(USER4, 'Tender la cama', points=10, status='excused',
         excuse_note='tenía prueba', excused_days_ago=0)
before = balance(USER4)
with A.app.test_request_context(json={'task_id': t2}):
    flask.session['user'] = USER1
    resp = A.tasks_api_accept_excuse()
body = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
check("accepted", body.get('ok') and body.get('accepted'), body)
check("and nothing was charged", balance(USER4) == before, f'{before} -> {balance(USER4)}')
sweep()
check("nor later, once it has been reviewed", balance(USER4) == before, balance(USER4))

print("\nan excuse nobody reviews IS charged — approval is the thing that clears it")
# Deliberately opposite to auto-approve, which pays out when nobody looks.
# Confirmed 2026-08-19 after the contradiction was raised explicitly.
stale = A.TASKS_PENALTY_UNREVIEWED_DAYS + 1
t3 = add(USER4, 'Lavar los platos', points=10, status='excused',
         excuse_note='se me olvidó', excused_days_ago=stale)
before = balance(USER4)
sweep()
check("charged", balance(USER4) == before - 5, f'{before} -> {balance(USER4)}')
conn = A._tasks_conn()
row = conn.execute('SELECT reviewed_by, status FROM tasks WHERE id = ?', (t3,)).fetchone()
conn.close()
check("and stamped decided, so it leaves the review panel", row[0] == 'sistema', row)
check("still 'excused' — the state machine gains no new state", row[1] == 'excused', row)
_src = open(os.path.join(SRC, 'app.py'), encoding='utf-8').read()
check("the asymmetry with auto-approve is deliberate and documented",
      'TASKS_AUTO_APPROVE_DAYS' in _src.split('TASKS_PENALTY_UNREVIEWED_DAYS')[0][-1400:],
      "if that comment goes, the next reader will 'fix' the asymmetry into symmetry")

print("\naccepting late gives it back — the answer cannot depend on an adult's inbox")
before = balance(USER4)
with A.app.test_request_context(json={'task_id': t3}):
    flask.session['user'] = USER1
    resp = A.tasks_api_accept_excuse()
body = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
check("refunded", body.get('refunded') == 5, body)
check("and the balance is whole again", balance(USER4) == before + 5, balance(USER4))
check("told, so the kid sees the reversal too",
      any(u == USER4 and 'devolvimos' in m for u, m in pushed), pushed[-2:])
# The refund is its own ref_type in the same unique index, so this is
# structural rather than a guard someone has to remember to write.
with A.app.test_request_context(json={'task_id': t3}):
    flask.session['user'] = USER1
    A.tasks_api_accept_excuse()
check("and cannot be claimed twice", balance(USER4) == before + 5, balance(USER4))

print("\ngranting the points anyway settles the penalty too")
t4 = add(USER4, 'Regar las plantas', points=10, due=old)
sweep()                                   # rots -> charged 5
mid = balance(USER4)
with A.app.test_request_context(json={'task_id': t4}):
    flask.session['user'] = USER1
    resp = A.tasks_api_approve()
body = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
check("approved", body.get('ok'), body)
check("+10 for the task and +5 back", balance(USER4) == mid + 15,
      f'{mid} -> {balance(USER4)}')

print("\nthe balance is floored at charge time, not at display time")
poor = USER3
conn = A._tasks_conn()
conn.execute('DELETE FROM points_ledger WHERE user = ?', (poor,))
conn.commit(); conn.close()
grant(poor, 2)                            # two points, about to miss a 10-pointer
t5 = add(poor, 'Ordenar la pieza', points=10, due=old)
sweep()
check("charged only what they had", balance(poor) == 0, balance(poor))
check("never below zero", balance(poor) >= 0, balance(poor))
check("and the ledger says the cap bit",
      any('tope' in r[1] for r in ledger(poor, 'penalty')), ledger(poor, 'penalty'))
# The point of charging rather than display-flooring: the next point earned is
# visible immediately instead of disappearing into an invisible debt.
grant(poor, 4)
check("so the next points earned show up at once", balance(poor) == 4, balance(poor))

print("\nnothing to take is not an error")
conn = A._tasks_conn()
conn.execute('DELETE FROM points_ledger WHERE user = ?', (poor,))
conn.commit(); conn.close()
t6 = add(poor, 'Guardar los juguetes', points=10, due=old)
sweep()
check("a zero balance is charged nothing", balance(poor) == 0, balance(poor))
check("and no ledger noise is written for it",
      not [r for r in ledger(poor, 'penalty') if 'juguetes' in r[1]],
      ledger(poor, 'penalty'))

print("\nthe API a person uses to discount points by hand")
before = balance(USER4)
with A.app.test_request_context(json={'user': 'user4', 'delta': -7, 'reason': 'Se portó mal'}):
    flask.session['user'] = USER1
    resp = A.tasks_api_adjust()
body = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
check("a negative adjust works", body.get('ok'), body)
check("and takes the points", balance(USER4) == before - 7, balance(USER4))
# The gap this closes: it used to write the ledger row and tell nobody.
check("and says so — a silent deduction is one nobody can account for later",
      any(u == USER4 and 'Se portó mal' in m for u, m in pushed), pushed[-2:])

print("\nand the weekly report counts what was actually taken")
lines, people = (lambda c: A._tasks_week_report_lines(
    c, TODAY - timedelta(days=30), TODAY))(A._tasks_conn())
user4 = people.get(USER4, {})
check("penalties are reported", user4.get('penalty', 0) > 0, user4)
# Exactly 5, and the arithmetic is the point: three tasks were charged 5 each
# (basura, platos, plantas) and two were given back (platos via accept-excuse,
# plantas via approve). A looser `>= 0` here would pass on any behaviour at all.
# The -7 hand adjustment is ref_type 'ajuste' and must not be counted as a
# penalty — that it isn't is half of what this line pins.
check("net of refunds — a penalty given back is not still a penalty",
      user4.get('penalty') == 5, user4)
check("and a line mentions them",
      any('por no hacer' in ln for ln in lines), lines)

print()
shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")

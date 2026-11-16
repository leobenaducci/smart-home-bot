"""Shared chores: several people do one chore together.

Run: python local/test_tasks_shared.py   (needs Flask; skips loudly without it)

A recurring chore can rotate (one person a day), give each assignee their own
copy, or -- this file -- be done together. Each person still has their own row,
so points, penalties, reminders and excuses stay per person; the rows are
linked, and the link decides one thing: a part marked done waits for the
partners who have not done theirs before a parent sees it.

What must hold:

- a together chore makes one linked row per person, every scheduled day;
- the first part done is held out of review and away from auto-approve, and
  the partner is nudged instead of the parents;
- the last part in releases them all, and the parents get one message;
- each person is approved separately and paid in full;
- a partner who never does it is auto-excused (and charged), which releases
  the part that was done -- and any excuse that releases a part tells the
  parents then, and starts that part's auto-approve clock then;
- a partner a parent sent back does not hold the other one hostage;
- rotate and together together is refused.

Invented household (CLAUDE.md): members Tomi and Mora, parent user1.
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

tmp = tempfile.mkdtemp(prefix="homecore-tasksshared-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
# The deployer stages the catalogues beside app.py; from a checkout, copy the
# repository's own so the pushes below are real strings and not key names.
_I18N = os.path.join(SRC, "..", "..", "..", "i18n")
if not os.path.isdir(os.path.join(dst, "i18n")) and os.path.isdir(_I18N):
    shutil.copytree(_I18N, os.path.join(dst, "i18n"),
                    ignore=shutil.ignore_patterns("__pycache__"))
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32,
                  HOMECORE_MEMBERS="user1,user2,user3",
                  HOMECORE_ADMIN_MEMBERS="user1",
                  HOMECORE_MEMBER_FOLDERS="user1:paula,user2:tomi,user3:mora")

PARENT, TOMI, MORA = "user1", "user2", "user3"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "member": "user1", "nanobot_id": 1},'
            ' {"username": "%s", "member": "user2", "nanobot_id": 2},'
            ' {"username": "%s", "member": "user3", "nanobot_id": 3}]' % (PARENT, TOMI, MORA))

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_tasks_db()

pushed, to_admins = [], []
pushed_titles = []


def _push(u, msg, **kw):
    pushed.append((u, msg))
    pushed_titles.append((u, kw.get('title')))


A._notify_user = _push
# A parent's push may be a function of that parent (it reads in their language).
A._notify_tasks_admins = lambda msg, **kw: to_admins.append(
    msg(PARENT) if callable(msg) else msg)

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


client = A.app.test_client()


def call(method, url, user, body=None):
    with client.session_transaction() as s:
        s['user'] = user
        s['csrf_token'] = 'tok'
    r = client.open(url, method=method, json=body, headers={'X-CSRF-Token': 'tok'})
    return r.status_code, (r.get_json(silent=True) or {})


NOW = datetime.now(A.TASKS_TZ)
TODAY = NOW.date()
ALL_DAYS = list(range(7))


def rows(title):
    conn = A._tasks_conn()
    try:
        return {a: (i, st) for i, a, st in conn.execute(
            'SELECT id, assignee, status FROM tasks WHERE title = ? AND due_date = ?',
            (title, TODAY.isoformat()))}
    finally:
        conn.close()


def queue():
    return {(t['title'], t['assignee']) for t in
            call('GET', '/tasks/api/review-queue', PARENT)[1].get('tasks', [])}


def balance(user):
    conn = A._tasks_conn()
    try:
        return A._points_balance(conn, user)
    finally:
        conn.close()


def sweep(when):
    conn = A._tasks_conn()
    try:
        return A._tasks_auto_resolve(conn, when, when.date())
    finally:
        conn.close()


# --- creating one ---------------------------------------------------------------
print("\nrotate and together at once")
code, body = call('POST', '/tasks/api/templates', PARENT, {
    'title': 'x', 'points': 10, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS,
    'rotate': True, 'shared': True})
check("is refused", code == 400, (code, body))
code, body = call('POST', '/tasks/api/templates', PARENT, {
    'title': 'x', 'points': 10, 'assignees': [TOMI], 'weekdays': ALL_DAYS, 'shared': True})
check("and so is together with one person", code == 400, (code, body))

print("\na chore done together")
code, body = call('POST', '/tasks/api/templates', PARENT, {
    'title': 'Lavar', 'points': 400, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS,
    'shared': True})
check("is created", code == 200 and body.get('ok'), (code, body))
tpl_id = body.get('id')
r = rows('Lavar')
check("one row each, today", set(r) == {TOMI, MORA}, r)
_, tpls = call('GET', '/tasks/api/templates', PARENT)
check("the template says shared", any(t['id'] == tpl_id and t['shared']
                                      for t in tpls.get('templates', [])), tpls)
_, mine = call('GET', '/tasks/api/list', TOMI)
lav = [t for t in mine['tasks'] if t['title'] == 'Lavar']
check("Tomi's list names Mora as the partner",
      lav and lav[0]['shared'] and [p['name'] for p in lav[0]['partners']] == ['Mora'], lav)

# --- the first part ---------------------------------------------------------------
print("\nTomi does his part first")
pushed.clear(); to_admins.clear()
code, _ = call('POST', '/tasks/api/complete', TOMI, {'task_id': r[TOMI][0]})
check("it is accepted", code == 200, code)
check("Mora is nudged", any(u == MORA and 'Tomi' in m for u, m in pushed), pushed)
check("the parents hear nothing yet", not to_admins, to_admins)
check("it is not in the review queue", ('Lavar', TOMI) not in queue(), queue())
_, mine = call('GET', '/tasks/api/list', TOMI)
lav = [t for t in mine['tasks'] if t['title'] == 'Lavar'][0]
check("Tomi's row says it waits for Mora",
      lav['status'] == 'review' and lav['partners'][0]['waiting'], lav)
# Backdated rather than swept in the future: the auto-excuse window is as long
# as the auto-approve one, so moving the clock would excuse Mora's row too.
_c = A._tasks_conn()
_c.execute('UPDATE tasks SET completed_at = ? WHERE id = ?',
           (int(NOW.timestamp()) - (A.TASKS_AUTO_APPROVE_DAYS + 1) * 86400, r[TOMI][0]))
_c.close()
approved, _ = sweep(NOW)
check("auto-approve leaves a held part alone", r[TOMI][0] not in [a[0] for a in approved],
      approved)

print("\nMora does hers")
code, _ = call('POST', '/tasks/api/complete', MORA, {'task_id': r[MORA][0]})
check("both parts reach the queue",
      {('Lavar', TOMI), ('Lavar', MORA)} <= queue(), queue())
check("the parents get one message naming both",
      len(to_admins) == 1 and 'Tomi' in to_admins[0] and 'Mora' in to_admins[0], to_admins)

print("\neach is approved, and paid in full")
before = balance(TOMI), balance(MORA)
call('POST', '/tasks/api/approve', PARENT, {'task_id': r[TOMI][0]})
call('POST', '/tasks/api/approve', PARENT, {'task_id': r[MORA][0]})
check("Tomi +400", balance(TOMI) - before[0] == 400, balance(TOMI) - before[0])
check("Mora +400", balance(MORA) - before[1] == 400, balance(MORA) - before[1])

# --- a partner who never does it --------------------------------------------------
print("\na partner who never does theirs")
code, body = call('POST', '/tasks/api/templates', PARENT, {
    'title': 'Recoger', 'points': 100, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS,
    'shared': True})
r2 = rows('Recoger')
call('POST', '/tasks/api/complete', MORA, {'task_id': r2[MORA][0]})
check("Mora's part is held", ('Recoger', MORA) not in queue(), queue())
# Days later Tomi's row is overdue past the auto-excuse window: he is excused
# and charged, and Mora's part is free to be approved.
far = NOW + timedelta(days=max(A.TASKS_AUTO_EXCUSE_DAYS, A.TASKS_AUTO_APPROVE_DAYS) + 1)
m_before, t_before = balance(MORA), balance(TOMI)
to_admins.clear()
sweep(far)          # excuses Tomi
st = rows('Recoger')
check("Tomi is excused", st[TOMI][1] == 'excused', st)
check("and charged", balance(TOMI) < t_before, (t_before, balance(TOMI)))
check("the parents are told Mora's part is there to review",
      any('Mora' in m and 'Tomi' in m and 'Recoger' in m for m in to_admins), to_admins)
check("it is in the review queue", ('Recoger', MORA) in queue(), queue())
# Done days ago, but released only now: the parents get their full window.
approved, _ = sweep(far)
check("auto-approve does not take it the moment it is released",
      rows('Recoger')[MORA][1] == 'review', rows('Recoger'))
approved, _ = sweep(far + timedelta(days=A.TASKS_AUTO_APPROVE_DAYS, minutes=5))
st = rows('Recoger')
check("it goes through once the window has passed", st[MORA][1] == 'approved', st)
check("and she is paid", balance(MORA) - m_before == 100, balance(MORA) - m_before)

print("\na partner who says they could not")
call('POST', '/tasks/api/templates', PARENT, {
    'title': 'Pasear', 'points': 60, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS,
    'shared': True})
r8 = rows('Pasear')
call('POST', '/tasks/api/complete', MORA, {'task_id': r8[MORA][0]})
_c = A._tasks_conn()
_c.execute('UPDATE tasks SET completed_at = ? WHERE id = ?',
           (int(NOW.timestamp()) - (A.TASKS_AUTO_APPROVE_DAYS + 1) * 86400, r8[MORA][0]))
_c.close()
to_admins.clear()
code, _ = call('POST', '/tasks/api/excuse', TOMI, {'task_id': r8[TOMI][0], 'note': 'enfermo'})
check("the excuse is accepted", code == 200, code)
check("the parents are told Mora's part is there to review",
      any('Mora' in m and 'Pasear' in m and 'Tomi' in m for m in to_admins), to_admins)
check("it is in the review queue", ('Pasear', MORA) in queue(), queue())
approved, _ = sweep(NOW)
check("an old completion is not auto-approved on release",
      r8[MORA][0] not in [a[0] for a in approved], approved)

print("\na partner who taps No pude on the push")
call('POST', '/tasks/api/templates', PARENT, {
    'title': 'Tender', 'points': 15, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS,
    'shared': True})
r10 = rows('Tender')
call('POST', '/tasks/api/complete', TOMI, {'task_id': r10[TOMI][0]})
to_admins.clear()
code, _ = call('POST', '/tasks/api/notify-action', MORA,
               {'task_id': r10[MORA][0], 'do': 'excuse', 'note': 'sin tiempo'})
check("the excuse is accepted", code == 200, code)
check("the parents are told Tomi's part is there to review",
      any('Tender' in m and 'Tomi' in m and 'Mora' in m and 'Motivo' not in m
          for m in to_admins), to_admins)

print("\na partner whose whole day is excused")
call('POST', '/tasks/api/templates', PARENT, {
    'title': 'Cocinar', 'points': 70, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS,
    'shared': True})
r9 = rows('Cocinar')
call('POST', '/tasks/api/complete', TOMI, {'task_id': r9[TOMI][0]})
to_admins.clear()
code, _ = call('POST', '/tasks/api/excuse-day', PARENT, {'user': MORA, 'note': 'viaje'})
check("the day is excused", code == 200, code)
check("the parents are told Tomi's part is there to review",
      sum('Cocinar' in m for m in to_admins) == 1, to_admins)

# --- a partner sent back -----------------------------------------------------------
print("\na partner a parent sent back")
call('POST', '/tasks/api/templates', PARENT, {
    'title': 'Barrer', 'points': 50, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS,
    'shared': True})
r3 = rows('Barrer')
call('POST', '/tasks/api/complete', TOMI, {'task_id': r3[TOMI][0]})
call('POST', '/tasks/api/complete', MORA, {'task_id': r3[MORA][0]})
call('POST', '/tasks/api/reject', PARENT, {'task_id': r3[TOMI][0], 'note': 'no ayudó'})
check("does not hold the other's part", ('Barrer', MORA) in queue(), queue())

# --- an ordinary chore is untouched -------------------------------------------------
print("\na chore that is not shared")
call('POST', '/tasks/api/templates', PARENT, {
    'title': 'Cama', 'points': 10, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS})
r4 = rows('Cama')
to_admins.clear()
call('POST', '/tasks/api/complete', TOMI, {'task_id': r4[TOMI][0]})
check("goes straight to review", ('Cama', TOMI) in queue(), queue())
check("and tells the parents", len(to_admins) == 1, to_admins)

# --- editing a template whose stored assignees include somebody who left ----------
print("\nediting a template that names somebody who left")
code, body = call('POST', '/tasks/api/templates', PARENT, {
    'title': 'Regar', 'points': 20, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS,
    'shared': True})
gone_tpl = body.get('id')
_c = A._tasks_conn()
_c.execute('UPDATE task_templates SET assignees = ? WHERE id = ?',
           ('["%s", "%s", "999000111"]' % (TOMI, MORA), gone_tpl))
_c.close()
code, body = call('PUT', f'/tasks/api/templates/{gone_tpl}', PARENT, {'title': 'Regar plantas'})
check("a title change still saves", code == 200 and body.get('ok'), (code, body))
code, body = call('PUT', f'/tasks/api/templates/{gone_tpl}', PARENT, {'active': False})
check("and so does switching it off", code == 200 and body.get('ok'), (code, body))
code, body = call('PUT', f'/tasks/api/templates/{gone_tpl}', PARENT, {'rotate': True})
check("rotate on a shared chore is still refused", code == 400, (code, body))
_c = A._tasks_conn()
_c.execute('UPDATE task_templates SET assignees = ? WHERE id = ?',
           ('["%s", "999000111"]' % TOMI, gone_tpl))
_c.close()
code, body = call('PUT', f'/tasks/api/templates/{gone_tpl}', PARENT, {'shared': True})
check("together with one person left who resolves is refused", code == 400, (code, body))
code, body = call('PUT', f'/tasks/api/templates/{gone_tpl}', PARENT,
                  {'assignees': [TOMI, MORA], 'shared': True})
check("naming two people who resolve is accepted", code == 200 and body.get('ok'), (code, body))

# --- the shared pushes are localised ----------------------------------------------
print("\nthe shared pushes read in the recipient's language")
check("the catalogue was loaded", A._translator is not None)
A._MEMBER_LOCALES[MORA] = 'en'
A._MEMBER_LOCALES[TOMI] = 'es'
call('POST', '/tasks/api/templates', PARENT, {
    'title': 'Ordenar', 'points': 30, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS,
    'shared': True})
r5 = rows('Ordenar')
pushed.clear(); pushed_titles.clear(); to_admins.clear()
call('POST', '/tasks/api/complete', TOMI, {'task_id': r5[TOMI][0]})
check("Mora's nudge is in English",
      (MORA, 'Tomi did their part of: Ordenar. Your turn!') in pushed, pushed)
check("under an English title", (MORA, 'Shared chore') in pushed_titles, pushed_titles)
pushed.clear(); pushed_titles.clear()
call('POST', '/tasks/api/complete', MORA, {'task_id': r5[MORA][0]})
call('POST', '/tasks/api/templates', PARENT, {
    'title': 'Ordenar2', 'points': 30, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS,
    'shared': True})
r6 = rows('Ordenar2')
pushed.clear()
call('POST', '/tasks/api/complete', MORA, {'task_id': r6[MORA][0]})
check("Tomi's nudge is in Spanish",
      (TOMI, 'Mora hizo su parte de: Ordenar2. ¡Te toca!') in pushed, pushed)
check("no push carries a key name",
      not any('tasks.' in (m or '') for _, m in pushed) and
      not any('tasks.' in (m or '') for m in to_admins), (pushed, to_admins))
_es = A._translator('tasks.push_shared_finished', locale='es', names='Mora, Tomi',
                    title='Ordenar', points=30)
check("the parents' message is in the house language",
      any(m == _es for m in to_admins), (to_admins, _es))

# --- both partners finish at the same moment --------------------------------------
print("\nboth partners finish at the same moment")
call('POST', '/tasks/api/templates', PARENT, {
    'title': 'Secar', 'points': 40, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS,
    'shared': True})
r7 = rows('Secar')
# Both requests have marked their own row done before either asks "am I the
# last?" -- the interleaving that told the parents twice.
_c = A._tasks_conn()
_c.execute("UPDATE tasks SET status = 'review', completed_at = ? WHERE id IN (?, ?)",
           (int(NOW.timestamp()), r7[TOMI][0], r7[MORA][0]))
_c.close()
to_admins.clear()
A._notify_shared_completion(r7[TOMI][0])
A._notify_shared_completion(r7[MORA][0])
check("the parents are told once", len(to_admins) == 1, to_admins)
check("and it names both", to_admins and 'Tomi' in to_admins[0] and 'Mora' in to_admins[0],
      to_admins)
print("\none part sent back and done again")
call('POST', '/tasks/api/reject', PARENT, {'task_id': r7[TOMI][0], 'note': 'otra vez'})
to_admins.clear()
call('POST', '/tasks/api/complete', TOMI, {'task_id': r7[TOMI][0]})
check("the parents hear about the redone part", len(to_admins) == 1, to_admins)

# --- switching "together" on and off moves today's rows with it ------------------
print("\nswitching together on carries today's rows")
code, body = call('POST', '/tasks/api/templates', PARENT, {
    'title': 'Ventanas', 'points': 25, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS})
win_tpl = body.get('id')
code, body = call('PUT', f'/tasks/api/templates/{win_tpl}', PARENT, {'shared': True})
check("the edit saves", code == 200, (code, body))
r11 = rows('Ventanas')
pushed.clear(); to_admins.clear()
call('POST', '/tasks/api/complete', TOMI, {'task_id': r11[TOMI][0]})
check("today's first part now waits for the partner",
      ('Ventanas', TOMI) not in queue() and not to_admins, (queue(), to_admins))
check("and the partner is nudged", any(u == MORA for u, _ in pushed), pushed)

print("\nswitching it off releases a held part")
code, body = call('POST', '/tasks/api/templates', PARENT, {
    'title': 'Puertas', 'points': 25, 'assignees': [TOMI, MORA], 'weekdays': ALL_DAYS,
    'shared': True})
door_tpl = body.get('id')
r12 = rows('Puertas')
call('POST', '/tasks/api/complete', TOMI, {'task_id': r12[TOMI][0]})
check("Tomi's part is held first", ('Puertas', TOMI) not in queue(), queue())
code, body = call('PUT', f'/tasks/api/templates/{door_tpl}', PARENT, {'shared': False})
check("the edit saves", code == 200, (code, body))
check("Tomi's part reaches the queue", ('Puertas', TOMI) in queue(), queue())
to_admins.clear()
call('POST', '/tasks/api/complete', MORA, {'task_id': r12[MORA][0]})
check("Mora's part is her own chore again: straight to review, one message",
      ('Puertas', MORA) in queue() and len(to_admins) == 1, (queue(), to_admins))

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    raise SystemExit(1)
print("all checks passed")

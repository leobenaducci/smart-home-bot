"""`/tasks/api/list` is readable by the whole house, and writing still is not.

Run inside a container built from the deployed image (app.py imports Flask and
the rest of it):

    docker run --rm --network none -e SECRET_KEY=x -e DEBUG_API_KEY=x \
      -v $PWD/local:/app-src:ro -w /app local-web:latest \
      sh -c 'cp -r /app-src/. /app/; python test_tasks_family_read.py'

The list used to filter `assignee = ?` on every scope, so an empty result meant
"not yours" but read as "nobody did it". Asked on 2026-08-11 who had cleaned the
cat bathroom the day before, Alfred saw nothing in Alex's list and answered "ayer
nadie". Robin had done it and been approved.

The rows below are that day, copied from the live table.
"""
import os
import sqlite3
import sys
import tempfile

os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('DEBUG_API_KEY', 'test')
# Who lives here and who is a parent. Both were literals in app.py and come
# from the deployer now; without them the house has no adults and every admin
# route answers 403.
os.environ.setdefault('HOMECORE_MEMBERS', 'user1,user3,user4')
os.environ.setdefault('HOMECORE_ADMIN_MEMBERS', 'user1')
# And what each of them is called on the share, which is the key the
# assignee lookup uses. Without it the folder table is empty and
# `user=user3` is refused by a message that lists user3 as valid.
os.environ.setdefault('HOMECORE_MEMBER_FOLDERS',
                      'user1:user1,user3:user3,user4:user4')

USER1, USER3, USER4 = 'user1', 'user3', 'user4'

ROWS = [
    # (title, assignee, due_date, status, completed_at, excuse_note, review_note)
    ('Limpiar baño gatos abajo (PM)', USER3, '2026-08-10', 'approved', 1786405436,
     'me dolía la guata otra vez', 'mentiste, no lo limpiaste'),
    ('Limpiar baño gatos arriba (PM)', USER4, '2026-08-10', 'pending', None, '', ''),
    ('Limpiar baño gatos arriba (PM)', USER1, '2026-08-11', 'pending', None,
     'excusa mía', 'nota mía'),
]


def _seed(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        'CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, '
        'description TEXT DEFAULT "", icon TEXT DEFAULT "", points INTEGER DEFAULT 0, '
        'assignee TEXT, due_date TEXT, time_start TEXT, time_end TEXT, '
        'remind_before INTEGER, remind_every INTEGER, status TEXT, '
        'completed_at INTEGER, review_note TEXT DEFAULT "", excuse_note TEXT DEFAULT "", '
        'snoozed_until INTEGER, template_id INTEGER)')
    conn.execute('CREATE TABLE points_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                 'user TEXT, delta INTEGER, reason TEXT, ts INTEGER)')
    conn.execute('INSERT INTO points_ledger (user, delta) VALUES (?, 6000)', (USER1,))
    conn.execute('INSERT INTO points_ledger (user, delta) VALUES (?, 1200)', (USER3,))
    for title, assignee, due, status, done, excuse, review in ROWS:
        conn.execute(
            'INSERT INTO tasks (title, assignee, due_date, status, completed_at, '
            'excuse_note, review_note, points) VALUES (?,?,?,?,?,?,?,500)',
            (title, assignee, due, status, done, excuse, review))
    conn.commit()
    conn.close()


def main():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, 'tasks.db')
    _seed(db)

    # The house, as the portal learns it: `HOMECORE_*` say who the members are
    # and what they are called, and the user store is the join from a login to
    # a member. `_refresh_people` builds every login-keyed table from that
    # store, so without one the folder table is empty and `user=user3` is
    # refused -- by a message that lists user3 among the valid names, because
    # the message is built from a different source than the lookup.
    import json
    # `USERS_FILE` is a bare 'users.json', resolved against the working
    # directory, so the store has to be beside us rather than pointed at.
    os.chdir(tmp)
    with open('users.json', 'w', encoding='utf-8') as fh:
        json.dump([{'username': u, 'member': u, 'nanobot_id': i}
                   for i, u in enumerate((USER1, USER3, USER4), start=1)], fh)

    import app
    app.TASKS_DB_PATH = db
    # The day is fixed so "today"/overdue windows are deterministic.
    import datetime
    app._tasks_today = lambda: datetime.date(2026, 8, 11)
    app._materialize_tasks = lambda day: None

    client = app.app.test_client()

    def get(user_arg=None, scope='all', as_user=USER1):
        with client.session_transaction() as s:
            s['user'] = as_user
        url = f'/tasks/api/list?scope={scope}'
        if user_arg:
            url += f'&user={user_arg}'
        r = client.get(url)
        assert r.status_code == 200, (r.status_code, r.data[:200])
        return r.get_json()

    failures = []

    def check(name, cond, detail=''):
        print(('  ok  ' if cond else '  FAIL') + '  ' + name + (f'  {detail}' if detail and not cond else ''))
        if not cond:
            failures.append(name)

    # --- the question that was answered wrong ---------------------------
    everyone = get(user_arg='all')
    titles = {(t['title'], t['assignee_name'], t['due_date'])
              for t in everyone['tasks']}
    check('user=all sees Robin\'s Aug-10 cat bathroom',
          any(t[2] == '2026-08-10' and 'abajo' in t[0] and t[1] == 'Robin'
              for t in titles), str(titles))
    check('user=all sees more than the caller\'s own',
          len(everyone['tasks']) == len(ROWS), f"{len(everyone['tasks'])} of {len(ROWS)}")
    check('user=all reports itself as all', everyone['user'] == 'all', everyone['user'])

    # --- the default has not moved --------------------------------------
    mine = get()
    check('no user= is still only mine',
          all(t['assignee'] == USER1 for t in mine['tasks']),
          str([t['assignee'] for t in mine['tasks']]))
    check('default reports the caller', mine['user'] == USER1, str(mine['user']))

    # --- one named person, asked by a non-admin --------------------------
    hers = get(user_arg='user3', as_user=USER4)
    check('a non-admin may read another member',
          hers['tasks'] and all(t['assignee_name'] == 'Robin' for t in hers['tasks']),
          str([t['assignee_name'] for t in hers['tasks']]))

    # --- the schedule is shared; the notes and the RUTs are not -----------
    # The widening was argued for "who has the dishes tonight". These rode
    # along because the whole row was reused: excuse_note is a child's reason
    # for not doing a chore, review_note is a parent's rejection wording meant
    # for that child, and `assignee` is a national ID number in this house.
    # As a child: Kai reading the house. Alex and Sam are admins and keep the
    # full row — they already read every note through the review queue, and the
    # Tareas dashboard needs the raw assignee to edit somebody's chore.
    others = [t for t in get(user_arg='all', as_user=USER4)['tasks']
              if t['assignee_name'] != 'Kai']
    check('another member\'s excuse note is not readable',
          all(t['excuse_note'] is None for t in others),
          str([t['excuse_note'] for t in others]))
    check('another member\'s review note is not readable',
          all(t['review_note'] is None for t in others),
          str([t['review_note'] for t in others]))
    check('another member\'s login id is not handed out',
          all(t['assignee'] is None for t in others),
          str([t['assignee'] for t in others]))
    check('but their name still is, which is what the docs promise',
          all(t['assignee_name'] for t in others),
          str([t['assignee_name'] for t in others]))
    own = [t for t in get(as_user=USER4)['tasks']]
    check('a child still sees their own notes and id',
          own and all(t['assignee'] == USER4 for t in own),
          str([(t['assignee'], t['excuse_note']) for t in own]))
    parent_view = get(user_arg='all')          # Alex, an admin
    check('a parent still gets the raw assignee the dashboard edits with',
          all(t['assignee'] for t in parent_view['tasks']),
          str([t['assignee'] for t in parent_view['tasks']]))
    check('and still reads the notes they already see in the review queue',
          any(t['excuse_note'] for t in parent_view['tasks']),
          str([t['excuse_note'] for t in parent_view['tasks']]))

    # --- points are not a scoreboard -------------------------------------
    # Against the seeded number, not against another live request: comparing
    # two responses to each other passes just as well if both return 0.
    check('own call reports the caller\'s seeded balance',
          get().get('balance') == 6000, str(get().get('balance')))
    everyone_payload = get(user_arg='all')
    check('a family-wide call does not carry a bare `balance`',
          'balance' not in everyone_payload, str(everyone_payload.get('balance')))
    check('it carries my_balance instead, and it is the caller\'s',
          everyone_payload.get('my_balance') == 6000,
          str(everyone_payload.get('my_balance')))
    hers_payload = get(user_arg='user3')
    check('asking about Robin never reports a number as hers',
          'balance' not in hers_payload and hers_payload.get('my_balance') == 6000,
          str(hers_payload))

    # --- an unknown name must not silently widen -------------------------
    with client.session_transaction() as sess:
        sess['user'] = USER1
    r = client.get('/tasks/api/list?scope=all&user=nobodyhere')
    check('an unknown name is an error, not the caller\'s rows relabelled',
          r.status_code == 400, f'got {r.status_code}: {r.data[:120]}')

    # --- a wrong parameter has to look wrong -----------------------------
    with client.session_transaction() as s:
        s['user'] = USER1
    r = client.get('/tasks/api/list?scope=robin')
    check('an unknown scope is an error, not a quiet fallback to today',
          r.status_code == 400, f'got {r.status_code}')
    check('and the error says where the name actually goes',
          b'user=' in r.data, r.data[:120].decode('utf-8', 'replace'))

    # --- every scope, not just the one that was tested -------------------
    # No escape hatch: with the seeded rows and the frozen date, Kai's
    # Aug-10 pending row reaches today's window through the overdue clause, so
    # every scope has a non-Alex row to find. The `or scope == 'today'` that
    # used to be here made that iteration `X or True` — the today branch has
    # the most complex WHERE and its own placeholder count, and was the one
    # never actually asserted on.
    for scope in ('today', 'week', 'all'):
        rows = get(user_arg='all', scope=scope)['tasks']
        others = [t for t in rows if t['assignee_name'] != 'Alex']
        check(f'scope={scope} honours user=all',
              bool(others),
              str([(t['title'], t['assignee_name']) for t in rows]))

    print()
    if failures:
        print(f'{len(failures)} FAILED: {failures}')
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())

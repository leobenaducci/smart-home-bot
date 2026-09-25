"""The second opinion: a geofence report checked against the GPS before it fires.

Run: python local/test_geo_confirm.py   (needs Flask; skips loudly without it)

The five checks in `_geo_event_rejection` judge a crossing by the fix that came
with it, and the app sends none — so four of them have never executed on a real
event. `_geo_confirm_transition` closes that by asking the phone for a fix of our
own (the same `location_request` push `locate` uses) and running the geometry on
the answer.

Two properties are worth pinning, and they pull in opposite directions:

- it **refutes** the report that made this necessary — "Kai llegó al colegio"
  arriving while the phone is two kilometres away at home;
- it **never invents silence**. A phone that does not answer, a fix too vague to
  mean anything, an exception on the way — every one of those has to leave the
  reminder firing exactly as it did before this existed. A notification that
  wrongly arrives gets reported; one that wrongly never arrives does not.

The third property is about *when* we write: nothing may touch `user_whereabouts`
or spend a one-shot reminder until the confirmation comes back. A refuted arrival
that had already recorded "está en el colegio" would answer "¿dónde está Kai?"
with the very thing we just refused to send.
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

tmp = tempfile.mkdtemp(prefix="homecore-geoconfirm-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)

USER1 = "user1"
USER2 = "user2"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2},'
            ' {"username": "%s", "nanobot_id": 3}]' % (USER1, USER2))

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_geo_db()
A.GEO_CONFIRM_WAIT_S = 1        # the "phone never answered" case must not take 25s

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


# --- The house, roughly Santiago ---------------------------------------------
# colegio is ~2 km north of casa: far enough that a fix at one cannot honestly
# claim a crossing at the other, close enough to be a real school run.
CASA = (-33.4489, -70.6693)
COLEGIO = (-33.4309, -70.6693)

conn = A._geo_conn()
now = int(time.time())
conn.execute('INSERT INTO places (id,name,name_key,lat,lng,radius,owner,created_at) '
             'VALUES (1,?,?,?,?,?,?,?)', ('casa', 'casa', CASA[0], CASA[1], 150, USER1, now))
conn.execute('INSERT INTO places (id,name,name_key,lat,lng,radius,owner,created_at) '
             'VALUES (2,?,?,?,?,?,?,?)',
             ('colegio', 'colegio', COLEGIO[0], COLEGIO[1], 150, USER1, now))
conn.close()

CASA_PLACE = ('casa', CASA[0], CASA[1], 150)
COLEGIO_PLACE = ('colegio', COLEGIO[0], COLEGIO[1], 150)

check("the two places really are ~2 km apart",
      1800 < A._haversine_m(*CASA, *COLEGIO) < 2200,
      int(A._haversine_m(*CASA, *COLEGIO)))


def add_reminder(target, place_id, direction="enter", one_shot=1, last_fired=None,
                 cooldown=300):
    conn = A._geo_conn()
    try:
        cur = conn.execute(
            'INSERT INTO geofence_reminders (creator,target_user,place_id,direction,'
            'text,notify_user,one_shot,active,cooldown_s,created_at,last_fired_at) '
            'VALUES (?,?,?,?,?,?,?,1,?,?,?)',
            (target, target, place_id, direction, 'comprar pan', target, one_shot,
             cooldown, int(time.time()), last_fired))
        return cur.lastrowid
    finally:
        conn.close()


def reminder_active(rid):
    conn = A._geo_conn()
    try:
        row = conn.execute('SELECT active FROM geofence_reminders WHERE id=?', (rid,)).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()


def whereabouts(user):
    conn = A._geo_conn()
    try:
        return conn.execute(
            'SELECT place_id,transition,place_name FROM user_whereabouts WHERE user=?',
            (user,)).fetchone()
    finally:
        conn.close()


def clear_state(user):
    conn = A._geo_conn()
    try:
        conn.execute('DELETE FROM user_whereabouts WHERE user=?', (user,))
        conn.execute('DELETE FROM geofence_reminders WHERE target_user=?', (user,))
    finally:
        conn.close()
    with A._user_location_lock:
        A._user_location.pop(user, None)


def set_last_fix(user, lat, lng, acc, age_s=0):
    with A._user_location_lock:
        A._user_location[user] = {'lat': lat, 'lon': lng, 'acc': acc,
                                  'ts': time.time() - age_s}


def phone_answers(lat, lng, acc):
    """Stand in for the handset: the location_request push arrives and the app
    POSTs a fix back, which is what wakes the long poll."""
    def push(user, tag, extra=None):
        pushes.append((user, tag))
        if tag == 'location_request':
            set_last_fix(user, lat, lng, acc)
            A._geo_report_signal(user)
    return push


def phone_silent():
    def push(user, tag, extra=None):
        pushes.append((user, tag))
    return push


pushes = []
A._geo_push_control = phone_silent()


# --- When is it worth waking the GPS at all? ---------------------------------
print("\nonly a report that is about to tell somebody something is confirmed")
clear_state(USER1)
conn = A._geo_conn()
try:
    check("no reminder waiting on it — nothing to protect, so no GPS",
          A._geo_event_worth_confirming(conn, USER1, 2, 'enter', None, None, now) is False)
    rid = add_reminder(USER1, 2, 'enter')
    check("a reminder is waiting — confirm it",
          A._geo_event_worth_confirming(conn, USER1, 2, 'enter', None, None, now) is True)
    check("the other direction fires nothing here",
          A._geo_event_worth_confirming(conn, USER1, 2, 'exit', None, None, now) is False)
    check("a repeat of what we already believe fires nothing",
          A._geo_event_worth_confirming(conn, USER1, 2, 'enter', (2, 'enter', 'colegio'),
                                        None, now) is False)
    check("an arrival somewhere else is still confirmed",
          A._geo_event_worth_confirming(conn, USER1, 2, 'enter', (1, 'enter', 'casa'),
                                        None, now) is True)
    check("a report that already carried a usable fix is not re-checked",
          A._geo_event_worth_confirming(conn, USER1, 2, 'enter', None,
                                        {'lat': COLEGIO[0], 'lng': COLEGIO[1], 'acc': 12},
                                        now) is False)
    check("a fix with no accuracy is not usable — confirm anyway",
          A._geo_event_worth_confirming(conn, USER1, 2, 'enter', None,
                                        {'lat': COLEGIO[0], 'lng': COLEGIO[1], 'acc': None},
                                        now) is True)
finally:
    conn.close()

clear_state(USER1)
add_reminder(USER1, 2, 'enter', last_fired=now - 30, cooldown=300)
conn = A._geo_conn()
try:
    check("a reminder still in cooldown fires nothing — no GPS for it",
          A._geo_event_worth_confirming(conn, USER1, 2, 'enter', None, None, now) is False)
finally:
    conn.close()
clear_state(USER1)
add_reminder(USER1, 2, 'enter', last_fired=now - 3600, cooldown=300)
conn = A._geo_conn()
try:
    check("one whose cooldown has expired does",
          A._geo_event_worth_confirming(conn, USER1, 2, 'enter', None, None, now) is True)
finally:
    conn.close()


# --- What the GPS says --------------------------------------------------------
print("\nthe fix we asked for refutes the report that was wrong")
clear_state(USER1)
set_last_fix(USER1, *CASA, 20, age_s=600)
A._geo_push_control = phone_answers(CASA[0], CASA[1], 20)
verdict = A._geo_confirm_transition(USER1, COLEGIO_PLACE, 2, 'enter', None)
check("'llegó al colegio' while the phone is at home is refused", bool(verdict), verdict)
check("and the reason names the distance", verdict and 'colegio' in verdict, verdict)

print("\nand leaves the report that was right alone")
clear_state(USER1)
set_last_fix(USER1, *CASA, 20, age_s=600)
A._geo_push_control = phone_answers(COLEGIO[0], COLEGIO[1], 15)
check("a real arrival, confirmed from inside the fence, fires",
      A._geo_confirm_transition(USER1, COLEGIO_PLACE, 2, 'enter', None) is None)

clear_state(USER1)
set_last_fix(USER1, *COLEGIO, 20, age_s=600)
A._geo_push_control = phone_answers(CASA[0], CASA[1], 20)
check("a late exit, already 2 km away, still fires when we saw them arrive",
      A._geo_confirm_transition(USER1, COLEGIO_PLACE, 2, 'exit',
                                (2, 'enter', 'colegio')) is None)

clear_state(USER1)
set_last_fix(USER1, *CASA, 20, age_s=600)
A._geo_push_control = phone_answers(CASA[0], CASA[1], 20)
check("the same exit with no record of them ever being inside is refused",
      A._geo_confirm_transition(USER1, COLEGIO_PLACE, 2, 'exit', None) is not None)

clear_state(USER1)
set_last_fix(USER1, *COLEGIO, 20, age_s=600)
A._geo_push_control = phone_answers(COLEGIO[0], COLEGIO[1], 15)
check("'salió del colegio' from the middle of the colegio is refused",
      A._geo_confirm_transition(USER1, COLEGIO_PLACE, 2, 'exit',
                                (2, 'enter', 'colegio')) is not None)


# --- Everything inconclusive believes the phone -------------------------------
print("\nnothing conclusive means the report stands — silence is never invented")
clear_state(USER1)
set_last_fix(USER1, *CASA, 20, age_s=600)
A._geo_push_control = phone_silent()
t0 = time.time()
check("a phone that never answers does not block the reminder",
      A._geo_confirm_transition(USER1, COLEGIO_PLACE, 2, 'enter', None) is None)
check("and does not hold the thread longer than the wait",
      time.time() - t0 < A.GEO_CONFIRM_WAIT_S + 2, round(time.time() - t0, 1))

clear_state(USER1)
set_last_fix(USER1, *CASA, 20, age_s=600)
A._geo_push_control = phone_answers(CASA[0], CASA[1], 900)
check("a fix too vague to mean anything is not a refutation",
      A._geo_confirm_transition(USER1, COLEGIO_PLACE, 2, 'enter', None) is None)

clear_state(USER1)
set_last_fix(USER1, *CASA, 20, age_s=600)
A._geo_push_control = phone_answers(CASA[0], CASA[1], None)
check("neither is one that reports no accuracy at all",
      A._geo_confirm_transition(USER1, COLEGIO_PLACE, 2, 'enter', None) is None)

clear_state(USER1)


def explode(user, tag, extra=None):
    raise RuntimeError("ntfy is down")


A._geo_push_control = explode
rid = add_reminder(USER1, 2, 'enter')
delivered = []
A._geo_deliver_all = lambda ds: delivered.extend(ds)
A._geo_event_confirm_and_fire(USER1, COLEGIO_PLACE, 2, 'colegio', 'enter', None, None)
check("a confirmation that raises still delivers", len(delivered) == 1, delivered)
check("and still records where they are", whereabouts(USER1) == (2, 'enter', 'colegio'),
      whereabouts(USER1))


# --- Nothing is written before the answer comes back --------------------------
print("\na refuted arrival leaves no trace at all")
clear_state(USER1)
set_last_fix(USER1, *CASA, 20, age_s=600)
A._geo_push_control = phone_answers(CASA[0], CASA[1], 20)
rid = add_reminder(USER1, 2, 'enter')
delivered = []
A._geo_deliver_all = lambda ds: delivered.extend(ds)
A._geo_event_confirm_and_fire(USER1, COLEGIO_PLACE, 2, 'colegio', 'enter', None, None)
check("nothing delivered", delivered == [], delivered)
check("the one-shot reminder is still armed for the real arrival", reminder_active(rid))
check("and '¿dónde está?' was not told he is at the colegio",
      whereabouts(USER1) is None, whereabouts(USER1))


# --- The route: answer the phone now, decide later ----------------------------
print("\nthe endpoint answers immediately and commits nothing until confirmed")
clear_state(USER1)
set_last_fix(USER1, *CASA, 20, age_s=600)
A._geo_push_control = phone_answers(COLEGIO[0], COLEGIO[1], 15)
rid = add_reminder(USER1, 2, 'enter')
delivered = []
A._geo_deliver_all = lambda ds: delivered.extend(ds)

A.app.config['TESTING'] = True
client = A.app.test_client()
with client.session_transaction() as s:
    s['user'] = USER1
t0 = time.time()
resp = client.post('/geo/api/event', json={'place_id': 2, 'transition': 'enter'})
elapsed = time.time() - t0
body = resp.get_json()
check("the phone gets its 200 back at once", resp.status_code == 200 and elapsed < 0.5,
      (resp.status_code, round(elapsed, 2)))
check("and is told the report is being confirmed", body.get('confirming') is True, body)
check("no reminder was spent on the way out", reminder_active(rid))

for _ in range(60):
    if delivered:
        break
    time.sleep(0.1)
check("the confirmed arrival fires", len(delivered) == 1, delivered)
check("the one-shot is spent once it did", not reminder_active(rid))
check("and the whereabouts row follows", whereabouts(USER1) == (2, 'enter', 'colegio'),
      whereabouts(USER1))
check("exactly one location_request went to the phone",
      [p for p in pushes if p == (USER1, 'location_request')][-1:] == [(USER1, 'location_request')],
      pushes[-3:])

print("\na crossing nobody is waiting on never wakes the GPS")
clear_state(USER1)
set_last_fix(USER1, *CASA, 20, age_s=600)
pushes.clear()
resp = client.post('/geo/api/event', json={'place_id': 1, 'transition': 'enter'})
check("it is committed inline", resp.get_json() == {'ok': True, 'fired': 0},
      resp.get_json())
check("no location_request was sent", pushes == [], pushes)
check("but where he is was still recorded", whereabouts(USER1) == (1, 'enter', 'casa'),
      whereabouts(USER1))

resp = client.post('/geo/api/event', json={'place_id': 1, 'transition': 'enter'})
check("a repeat is still suppressed", resp.get_json().get('ignored', '').startswith('already'),
      resp.get_json())

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)

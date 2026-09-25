"""Finding a message somebody sent, instead of hunting for it.

Run: python local/test_notif_search.py   (needs Flask; skips loudly without it)

On 2026-08-16 Alex asked "¿cuál es el total de la última cuenta que mandó Jana?".
The answer had been relayed from WhatsApp eight minutes earlier and was sitting
in `notif_log` as `com.whatsapp | "Jana 2" | "Jana 2: 17500 carne\\n21000 super"`
— the 5th most recent notification, with nothing arriving in between. A bare
`list_notifications()` would have returned it.

It took 40 tool calls and four and a half minutes: nine at the finance DB, five
at paperless, four greps of memory, five no-op `true`s, and finally two greps of
Alfred's own session transcripts, which is where he found it — reading the
record of having been told rather than the store.

Nothing was missing except a way to ask. `/chat/notifications/recent` answers
"what came in lately"; nobody could ask "what did Jana send me". This pins the
endpoint that closes that.

The sender check is the subtle one. The Android relay folds the sender into the
body as `"<sender>: <text>"` (NotificationRelayService.messageText), so there is
no sender column — searching for a person and searching for a word they wrote
have to be the same query, or looking up "jana" finds nothing.
"""
import json
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

tmp = tempfile.mkdtemp(prefix="homecore-notifsearch-")
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

A.init_notif_db()

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


NOW = int(time.time())


def log(username, package, label, title, body, age_s=0, can_reply=0):
    conn = A._notif_conn()
    try:
        conn.execute(
            'INSERT INTO notif_log (username, package, label, title, body, nkey, '
            'can_reply, ts) VALUES (?,?,?,?,?,?,?,?)',
            (username, package, label, title, body, f'k{title}{age_s}', can_reply,
             NOW - age_s))
        conn.commit()  # _notif_conn is not autocommit, unlike _geo_conn
    finally:
        conn.close()


# The real notification, as the phone actually relayed it, plus the noise that
# was around it that day.
log(USER1, 'com.whatsapp', 'WhatsApp', 'Jana 2', 'Jana 2: 17500 carne\n21000 super', 520)
log(USER1, 'com.samsung.android.calendar', 'Calendar', 'Pagar Jana 335', '12:00', 87000)
log(USER1, 'com.whatsapp', 'WhatsApp', 'Sam', 'Sam: ya voy saliendo', 300)
log(USER1, 'com.whatsapp', 'WhatsApp', 'Colegio 5°B', 'Ana: reunión el jueves', 200)
log(USER2, 'com.whatsapp', 'WhatsApp', 'Jana 2', 'Jana 2: mañana voy a las 9', 100)

client = A.app.test_client()
A.app.config['TESTING'] = True
with client.session_transaction() as s:
    s['user'] = USER1


def search(**params):
    qs = '&'.join(f'{k}={v}' for k, v in params.items())
    r = client.get(f'/chat/notifications/search?{qs}')
    return r, (r.get_json() or {})


# --- The question that started this --------------------------------------------
print("\nthe message Alex could not find")
r, body = search(q='jana')
check("searching a sender's name finds their message", r.status_code == 200
      and any('17500 carne' in (e['text'] or '') for e in body.get('entries', [])),
      body)
check("and the WhatsApp one comes before the old calendar entry",
      body['entries'][0]['text'].startswith('Jana 2: 17500'),
      [e['title'] for e in body['entries']])
check("the total is right there in the text",
      '21000 super' in body['entries'][0]['text'])

r, body = search(q='carne')
check("searching a word from the body finds it too",
      any('17500' in (e['text'] or '') for e in body['entries']), body)

r, body = search(q='colegio')
check("searching a group name finds the group",
      len(body['entries']) == 1 and 'reunión' in body['entries'][0]['text'], body)


# --- Whose messages ------------------------------------------------------------
print("\none person's phone is not another's")
check("Alex's search does not return Sam's Jana message",
      all('mañana voy a las 9' not in (e['text'] or '') for e in body.get('entries', []))
      and all('mañana voy a las 9' not in (e['text'] or '')
              for e in search(q='jana')[1]['entries']))
with client.session_transaction() as s:
    s['user'] = USER2
r, body = search(q='jana')
check("and Sam's returns only hers",
      len(body['entries']) == 1 and 'mañana' in body['entries'][0]['text'], body)
with client.session_transaction() as s:
    s['user'] = USER1


# --- The window and the shape --------------------------------------------------
print("\nbounds")
r, body = search(q='jana', days=1)
check("days narrows the window — the year-old calendar entry drops out",
      all(e['app'] == 'WhatsApp' for e in body['entries']),
      [(e['app'], e['title']) for e in body['entries']])
check("and says which window it used", body.get('days') == 1, body.get('days'))

r, body = search(q='jana', limit=1)
check("limit is honoured", len(body['entries']) == 1, len(body['entries']))

r, body = search(q='')
check("an empty query is a 400, not every message ever", r.status_code == 400, r.status_code)

r, body = search(q='pantufla')
check("no match is an empty list, not an error",
      r.status_code == 200 and body['entries'] == [], body)


# --- A name is not a pattern ---------------------------------------------------
print("\nthe query is text, not SQL")
log(USER1, 'com.whatsapp', 'WhatsApp', '100%', '100%: descuento', 50)
r, body = search(q='%')
check("a bare % matches the message containing '%', not everything",
      len(body['entries']) == 1 and '100%' in body['entries'][0]['title'],
      [(e['title']) for e in body['entries']])
r, body = search(q='_')
check("and _ is a literal underscore, not any-character",
      body['entries'] == [], [(e['title']) for e in body['entries']])


# --- Same shape as /recent, so the skill can render either ---------------------
print("\nthe entry shape matches /recent")
r, body = search(q='jana')
rec = client.get('/chat/notifications/recent?limit=1').get_json()
check("same keys", set(body['entries'][0]) == set(rec['entries'][0]),
      (sorted(body['entries'][0]), sorted(rec['entries'][0])))

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)

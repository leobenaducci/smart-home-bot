"""Most notifications never needed a model to know they were nothing.

Run: python local/test_notif_filter.py   (needs Flask; skips loudly without it)

Measured over four real days (2026-08-14 → 17, off the ev-notif session files):
**795 notifications became 795 Alfred turns and he spoke once** — Sam's clinic
confirming an appointment. The other 794 answered SILENCE at ~86k prompt tokens
apiece, roughly $48 a month to say nothing.

He could not have answered otherwise. The rules the user writes ARE the spec for
when he may speak, and Alex's are narrow: "avísame si user3, user4 o user2 necesitan
algo importante o urgente". A WhatsApp reaction emoji in a work group has one
legal answer, and paying a model to reach it is waste and not judgement.

So the rules are read as the filter they already are. Replayed through those
four days, the code below keeps 26 notifications of 795 and drops 769, and the
one Alfred spoke on is among the 26.

**Every check here is about the direction of failure.** Dropping a message that
mattered is the expensive mistake — "avísame si Kai necesita algo" is a promise
about a child — while sending one that did not costs a fraction of a cent. So
uncertainty resolves towards spending the call: no rules, a repliable app, a
missed call, or any word a rule mentions. The tests below are mostly about the
things that must survive the filter, not the things it removes.
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

tmp = tempfile.mkdtemp(prefix="homecore-notiffilter-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
PROXY_SECRET = "p" * 32
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET=PROXY_SECRET,
                  DEBUG_API_KEY="d" * 32)

USER1 = "user1"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}]' % USER1)

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_notif_db()

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


# Alex's live rules, copied from the box on 2026-08-17.
RULES = ('1. Solo leelas, no me informes ni muestres notificaciones a menos que una '
         'regla siguiente te pida por un app especifico\n'
         '2. Avisame con sonido si user3, user4 o user2 necesitan algo importante o urgente')


def worth(text, label="WhatsApp", title="", can_reply=False, rules=RULES):
    return A._notif_worth_a_turn(label, title, text, can_reply, rules)


print("the rules are the spec, so they are where the terms come from")
terms = A._notif_rule_terms(RULES)
for name in ("user3", "user4", "user2", "urgente", "importante"):
    check(f"{name} is a term", name in terms, sorted(terms))
# Every one of these is in Alex's rules and in half the messages he gets. Keeping
# them would send the day's chatter through and the filter would do nothing.
for noise in ("solo", "algo", "aviso", "avisame", "mensaje", "notificaciones", "regla"):
    check(f"{noise} is not", noise not in terms, sorted(terms))
_names = {a for al in A.TASKS_NAME_ALIASES.values() for a in al}
check("and nothing tiny gets in that is not a name",
      all(len(t) >= 4 for t in terms - _names), sorted(terms))
# A rule naming a person is about the person, not the spelling. The house
# already keeps this table for assigning chores to "Kai"; the filter would
# otherwise keep the promise only when the sender used the short form.
# "kai" and "sam" are three letters, below the generic term floor -- a name is
# exempt from it, or the promise holds only for the people with long names.
for alias in ("kai", "kaito", "sam", "sammy", "robin"):
    check(f"{alias} comes along with the nickname", alias in terms, sorted(terms))
check("but only for the people a rule mentions",
      not (terms & {"user5", "noito"}), sorted(terms))
check("and never a two-letter one", "ka" not in terms, sorted(terms))


print("\nwhat has to get through, whatever else changes")
must_pass = [
    ("Sam Roe: Hola Sam Patricia Roe", {}),          # the real one
    ("Robin: se me olvidó la mochila", {}),
    ("Kai necesita que la vayan a buscar", {}),
    ("Colegio: es urgente, llame al establecimiento", {}),
    ("Robin: llegué bien", {}),                                        # accented
    ("Kai necesita permiso firmado", {}),                          # inflection
    ("Kaito se quedó sin batería", {}),                            # nickname
    ("Sam Roe compartió su ubicación", {}),
]
for text, kw in must_pass:
    w, why = worth(text, **kw)
    check(f'"{text[:44]}"', w, why)

print("\nand the escalations nobody wrote a rule for")
w, why = worth("Nicolas Lamanna: 2 llamadas perdidas")
check("a missed call, even with no name in any rule", w, why)
check("  and it says so", why == 'llamada perdida', why)
w, why = worth("cualquier cosa", can_reply=True)
check("an app the user let Alfred reply to", w, why)
w, why = worth("cualquier cosa", rules="")
check("no rules at all — nothing to filter against", w, why)
w, why = worth("cualquier cosa", rules="1. ok\n2. si")
check("rules too short to identify anything", w, why)

print("\nwhat is not worth a model call")
for text in [
    "Nicolas Lamanna: Reacted 😂 to \"Ok, por privacidad lo deje solo habilitado\"",
    "GenAI (2 messages): Federico Oro Vojacek: Yo la pague nueva 1200",
    "AliExpress: ¡Tu pedido va en camino!",
    "Weather: Lluvia ligera esta tarde",
]:
    w, why = worth(text)
    check(f'"{text[:44]}"', not w, why)


print("\nthe archive is not what is being filtered")
# The row is written before the judgement, so everything is still stored and
# still searchable. `search_notifications` answers "¿cuánto era la cuenta que
# mandó Jana?" out of exactly the rows this decided not to wake him for.
delivered = []
A._notif_deliver = lambda *a, **kw: delivered.append(a[3])   # title

client = A.app.test_client()
HDRS = {"X-Proxy-Secret": PROXY_SECRET, "X-Proxy-User": USER1}
conn = A._notif_conn()
try:
    conn.execute('INSERT INTO notif_apps (username, package, label, can_read, can_reply, '
                 'seen_count, last_seen) VALUES (?,?,?,1,0,1,?)',
                 (USER1, 'com.whatsapp', 'WhatsApp', int(time.time())))
    for text in ('Solo leelas, no me informes ni muestres notificaciones a menos que una '
                 'regla siguiente te pida por un app especifico',
                 'Avisame con sonido si user3, user4 o user2 necesitan algo importante o urgente'):
        conn.execute('INSERT INTO notif_rule_items (username, text, enabled, created_at) '
                     'VALUES (?,?,1,?)', (USER1, text, int(time.time())))
    conn.commit()
finally:
    conn.close()


def post(title, text):
    return client.post('/chat/notification', headers=HDRS, json={
        'package': 'com.whatsapp', 'label': 'WhatsApp', 'title': title, 'text': text})


r = post('Nicolas', 'Reacted 😂 to "lo deje habilitado"')
check("the noisy one is accepted", r.status_code == 200, r.status_code)
check("  and marked filtered", r.get_json().get('filtered') is True, r.get_json())
noisy_id = r.get_json().get('id')
check("  and still has an id, so it was stored", bool(noisy_id), r.get_json())

r = post('Sam Roe', 'Hola Sam, su cita es a las 13:00')
check("the one about Sam goes through", not r.get_json().get('filtered'), r.get_json())

# The delivery thread is a real thread; give it a moment to start.
for _ in range(50):
    if delivered:
        break
    time.sleep(0.02)
check("exactly one turn was started", delivered == ['Sam Roe'], delivered)

conn = A._notif_conn()
try:
    rows = dict(conn.execute(
        'SELECT title, skipped FROM notif_log WHERE username = ?', (USER1,)).fetchall())
finally:
    conn.close()
check("both are in the archive", set(rows) == {'Nicolas', 'Sam Roe'}, rows)
check("the skipped one says why", rows.get('Nicolas') == 'no rule mentions it', rows)
check("the delivered one says nothing", rows.get('Sam Roe') == '', rows)

print("\nand the panel can show its workings")
# A filter nobody can see the workings of is a filter nobody can trust with
# "avísame si Kai necesita algo". The counts come off notif_log.skipped rather
# than a counter in memory, so they survive a deploy and still answer the
# question a week later.
cfg = client.get('/chat/notifications/config', headers=HDRS).get_json()
f = (cfg.get('filter') or {}).get('today') or {}
check("today's total counts everything that arrived", f.get('total') == 2, f)
check("and says how many he actually looked at", f.get('looked') == 1, f)
check("and how many he did not", f.get('skipped') == 1, f)
check("with the reason, not just a number",
      [r['why'] for r in f.get('reasons', [])] == ['no rule mentions it'], f)
week = (cfg.get('filter') or {}).get('week') or {}
check("the week is there too", week.get('total') == 2, week)

rec = client.get('/chat/notifications/recent', headers=HDRS).get_json()
by_title = {e['title']: e for e in rec['entries']}
check("the skipped entry says so", by_title['Nicolas']['skipped'], by_title.get('Nicolas'))
check("and the delivered one does not", by_title['Sam Roe']['skipped'] == '',
      by_title.get('Sam Roe'))
# The `notifications` skill reads this same list, so "¿por qué no me avisaste?"
# is answerable by Alfred and not only by the panel.
check("both are still listed", len(rec['entries']) == 2, rec['entries'])

print()
print("\nthe rules are handed over as an ordered list, not a flat set")

# People write rules that lean on each other. This house's first one ends
# "a menos que una regla siguiente te pida por un app especifico" -- an
# override written into the text, invisible if the list is presented as a
# closed set of peers. A blanket "don't tell me anything" followed by "but
# wake me if the family needs something urgent" then reads as two rules of
# equal weight, and the exception loses.
# From the file rather than `inspect`, which reads whatever copy the harness
# imported and can lag the tree under test.
_all = open(os.path.join(dst, "app.py"), encoding="utf-8").read()
_i = _all.index("def _notif_deliver")
_src = _all[_i:_all.index("\ndef ", _i + 10)]
check("  the preamble says they are ordered", "in order" in _src)
check("  and that a later rule can override an earlier one",
      "overrides an earlier" in _src)
# A fragment, not the whole sentence: the string is split across source lines,
# so the full wording exists only once concatenated at runtime.
check("  while still closing the set",
      "if something is not here" in _src)

shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")

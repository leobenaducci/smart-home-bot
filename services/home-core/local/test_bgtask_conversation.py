"""A background task remembers WHICH conversation asked for it.

Run: python local/test_bgtask_conversation.py   (needs Flask; skips loudly without it)

The panel's "Ver en el chat" opened the task's *day*, and a day is not a
conversation. Ask for something long in the morning chat, start two more
conversations before it lands, and the button painted the whole day — which
reads as whichever conversation the day ended on. The answer was in there, two
conversations down, and the button looked like it had gone to the wrong chat
because for any practical purpose it had.

The conversation was never missing: the chat_id nanobot posts back carries it
(`homeweb:<user>:<day>:<conv>`, and `:<scope>:<conv>` inside a Profesión). The
task path read `parts[2]` and dropped the rest, while the message path six
hundred lines below parsed all of it. Now both call `_chat_id_conv`, and what
it returns is stored on the row.
"""
import os
import shutil
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homeweb-bgconv-")
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

A.init_bgtask_db()

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


client = A.app.test_client()
H = {'X-Proxy-Secret': PROXY_SECRET, 'X-Proxy-User': USER1}
DAY = A._tasks_today().isoformat()


def start(task_id, chat_id, label='investigar el 502'):
    return client.post('/chat/agent-event', headers=H, json={
        'type': 'task', 'event': 'start', 'task_id': task_id,
        'label': label, 'chat_id': chat_id,
    })


def row(task_id):
    return A._bgtask_get(USER1, task_id)


print("reading the conversation out of a chat_id")
cases = [
    ("plain day",        ['homeweb', USER1, DAY],                    ('', None)),
    ("ordinary chat",    ['homeweb', USER1, DAY, '1787544867402'],   ('1787544867402', None)),
    ("machine event",    ['homeweb', USER1, DAY, 'ev-notif'],        ('', None)),
    ("delegate",         ['homeweb', USER1, DAY, 'dlg-fin'],         ('', None)),
    ("profession alone", ['homeweb', USER1, DAY, 'dsg'],             ('', 'designer')),
    ("profession + conv", ['homeweb', USER1, DAY, 'dsg', '17875448'], ('17875448', 'designer')),
]
for name, parts, expected in cases:
    check(f"  {name}", A._chat_id_conv(parts) == expected, A._chat_id_conv(parts))

print("\na task started in a conversation keeps it")
start('t-conv', f'homeweb:{USER1}:{DAY}:1787544867402')
t = row('t-conv')
check("the day is still there", t and t['day'] == DAY, t)
check("and so is the conversation", t and t['conv'] == '1787544867402', t)
check("the ordinary chat has no space", t and t['space'] == '', t)

print("\na task started inside a Profesión keeps both")
start('t-space', f'homeweb:{USER1}:{DAY}:dsg:17875448')
t = row('t-space')
check("the conversation", t and t['conv'] == '17875448', t)
check("and the space it lives in", t and t['space'] == 'designer', t)

print("\na task with no conversation to name is not invented one")
start('t-bare', f'homeweb:{USER1}:{DAY}')
t = row('t-bare')
check("conv stays empty", t and t['conv'] == '', t)
check("so the panel falls back to the day", t and t['day'] == DAY, t)

print("\nthe machine sessions name no conversation either")
start('t-ev', f'homeweb:{USER1}:{DAY}:ev-notif')
t = row('t-ev')
check("ev-* is a scope, not a conversation", t and t['conv'] == '', t)

print("\nthe panel hands the button what it needs")
listed = {x['id']: x for x in (client.get('/chat/background-tasks', headers=H)
                               .get_json() or {}).get('tasks', [])}
check("every task carries day, conv and space",
      all(set(('day', 'conv', 'space')) <= set(x) for x in listed.values()),
      list(listed.values())[:1])
check("the conversation survives the round trip",
      listed.get('t-conv', {}).get('conv') == '1787544867402', listed.get('t-conv'))

print("\na second report without a conversation does not erase the first")
# nanobot re-reports a running task after a restart, and that report can carry
# a chat_id with no conversation in it. Unconditional, the UPDATE wrote the
# empty string over a real one and the panel's button fell back to the whole
# day -- silently undoing the fix this file exists for.
start('t-keep', f'homeweb:{USER1}:{DAY}:1787544867402')
start('t-keep', f'homeweb:{USER1}:{DAY}')
t = row('t-keep')
check("the conversation survives a conversation-less re-report",
      t and t['conv'] == '1787544867402', t)

print("\na restart does not lose it (the same task_id reported twice)")
start('t-conv', f'homeweb:{USER1}:{DAY}:1787544867402')
check("still the same conversation", row('t-conv')['conv'] == '1787544867402', row('t-conv'))

print("\nan older row, filed before the column existed, still opens its day")
conn = A._bgtask_conn()
conn.execute("UPDATE bg_tasks SET conv = '', space = '' WHERE task_id = 't-conv'")
conn.commit()
conn.close()
t = row('t-conv')
check("no conversation, but a day", t['conv'] == '' and t['day'] == DAY, t)

print("\nthe message path still stamps the conversation it always did")
sent = client.post('/chat/agent-event', headers=H, json={
    'type': 'message', 'text': 'listo, aquí está lo que encontré',
    'chat_id': f'homeweb:{USER1}:{DAY}:1787544867402',
})
check("accepted", sent.status_code == 200, sent.get_json())
hist = A.load_user_history(USER1, DAY)
check("and filed under that conversation",
      any(m.get('conv') == 1787544867402 for m in hist),
      [m.get('conv') for m in hist])

sent = client.post('/chat/agent-event', headers=H, json={
    'type': 'message', 'text': 'la pieza quedó lista',
    'chat_id': f'homeweb:{USER1}:{DAY}:dsg:17875448',
})
check("a Profesión reply lands in the Profesión", sent.status_code == 200, sent.get_json())
hist = A.load_user_history(USER1, DAY, 'designer')
check("under its own conversation",
      any(m.get('conv') == 17875448 for m in hist),
      [m.get('conv') for m in hist])

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all good")

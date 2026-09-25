#!/usr/bin/env python3
"""A reminder you can answer from the notification, instead of only read.

Alfred's reminders arrived as a line of text: to say "done" you had to open
the app, find the conversation and tell him. Three buttons now ride the push —
Listo, Posponer, Descartar — and the work happens in that member's own nanobot,
which owns the cron job behind the reminder.

What this pins:

- the buttons appear on a reminder and *only* on a reminder. Every other thing
  Alfred says on his own time — a finished background task, a note — is not
  something you mark as done, and three buttons under it would be noise;
- pressing one reaches that member's own Alfred, and there is no way to name
  anybody else's;
- an unknown action is refused rather than passed along.

Run where app.py can import. Skips rather than fails when it cannot.
"""

import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-the-reminder-tests')
os.environ.setdefault('DEBUG_API_KEY', 'test-debug-key-for-the-reminder-tests')
os.environ['PROXY_SHARED_SECRET'] = 'p' * 32

SRC = os.path.dirname(os.path.abspath(__file__))
_tmp = tempfile.mkdtemp(prefix='homecore-reminder-')
shutil.copytree(SRC, os.path.join(_tmp, 'local'),
                ignore=shutil.ignore_patterns('backup_data', 'history', '__pycache__', 'certs'))
os.chdir(os.path.join(_tmp, 'local'))
USER = 'user1'
with open('users.json', 'w', encoding='utf-8') as f:
    f.write('[{"username": "%s", "nanobot_id": 2}]' % USER)
sys.path.insert(0, os.getcwd())
os.makedirs('backup_data', exist_ok=True)

try:
    import app as A  # noqa: E402
except ImportError as e:
    print('SKIP: %s — run this where app.py can import.' % e)
    raise SystemExit(0)

failures = []


def check(label, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


A.app.config['TESTING'] = True

# --- the buttons themselves ---------------------------------------------------
print('a fired reminder carries three answers')
actions = A._reminder_actions({'job': 'ab12cd', 'name': 'pastillas', 'recurring': True})
labels = [a['label'] for a in actions]
check('Listo, Posponer, Descartar', labels == ['Listo', 'Posponer', 'Descartar'], labels)
check('each names the job it answers',
      all(json.loads(a['body'])['job'] == 'ab12cd' for a in actions), actions)
check('and says which of the three it is',
      sorted(json.loads(a['body'])['do'] for a in actions) == ['discard', 'done', 'snooze'],
      actions)
# A notification still sitting there after you have answered it reads as a
# button that did nothing.
check('all three clear the notification', all(a['clear'] for a in actions), actions)
check('they post to this app, not to nanobot directly',
      all(a['url'].endswith('/chat/api/reminder-action') for a in actions), actions)

# --- only on reminders --------------------------------------------------------
print('\nand only a reminder gets them')
pushes = []
A.send_ntfy = lambda topic, message, **kw: pushes.append((message, kw)) or True
A._ntfy_topic = lambda username: 'topic-' + username
A._user_watching = lambda username: False           # nobody is looking
A._alfred_turn_active = lambda username, day: False
A.append_user_history = lambda username, entry, day, space=None: True
A._bgtask_attach_result = lambda username, text: None

client = A.app.test_client()
HEAD = {'X-Proxy-Secret': A._proxy_user_token(USER), 'X-Proxy-User': USER}


def relay(text, reminder=None):
    pushes.clear()
    body = {'type': 'message', 'chat_id': f'homeweb:{USER}:2026-08-08', 'text': text}
    if reminder:
        body['reminder'] = reminder
    return client.post('/chat/agent-event', headers=HEAD, json=body)


r = relay('Ya está la investigación que pediste.')
check('an ordinary message pushes', r.status_code == 200 and pushes, r.status_code)
check('with no buttons on it', pushes and pushes[0][1].get('actions') is None, pushes)

r = relay('Hora de tomar las pastillas.', {'job': 'ab12cd', 'name': 'pastillas'})
check('a reminder pushes too', r.status_code == 200 and pushes, r.status_code)
check('and this one has the three buttons',
      pushes and len(pushes[0][1].get('actions') or []) == 3, pushes)

r = relay('Hora de algo.', {'name': 'sin id'})
check('a reminder with no job id gets none either',
      pushes and pushes[0][1].get('actions') is None, pushes)

# --- pressing one -------------------------------------------------------------
print('\npressing one reaches this member\'s own Alfred')
calls = []


class _Resp:
    ok, status_code, content = True, 200, b'{"ok":true}'

    def json(self):
        return {'ok': True}


def _post(url, **kw):
    calls.append((url, kw.get('json')))
    return _Resp()


A.requests.post = _post
with client.session_transaction() as s:
    s['user'] = USER

r = client.post('/chat/api/reminder-action', json={'job': 'ab12cd', 'do': 'snooze'})
check('it is accepted', r.status_code == 200, r.get_json())
check('and forwarded to a nanobot cron action',
      calls and calls[-1][0].endswith('/cron/action'), calls)
check('naming the job and what to do',
      calls and calls[-1][1] == {'job': 'ab12cd', 'do': 'snooze'}, calls)

print('\nand nothing else is')
for bad in ({'job': 'ab12cd', 'do': 'explode'}, {'do': 'done'}, {'job': '', 'do': 'done'}):
    before = len(calls)
    r = client.post('/chat/api/reminder-action', json=bad)
    check(f'  {bad} is refused', r.status_code == 400 and len(calls) == before, r.status_code)

print()
if failures:
    print(f'{len(failures)} FAILED: ' + ', '.join(failures))
    raise SystemExit(1)
print('all checks passed')

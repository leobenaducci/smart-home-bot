#!/usr/bin/env python3
"""A turn that fails for a reason that will pass is simply tried again.

The person asking cannot tell "the provider is busy" from "Alfred is broken",
and should not have to — so the failures that go away on their own go away
quietly. What matters is everything this must *not* do, which is what most of
this file checks:

- retry a refusal that will refuse identically, making somebody wait three
  times as long for the same message;
- retry after text is already on the screen, which would write the second half
  of a different answer under the first half of this one;
- retry something the person cancelled;
- keep waiting out a retry pause after they press Detener.

Run where app.py can import. Skips rather than fails when it cannot.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-the-retry-tests')
os.environ.setdefault('DEBUG_API_KEY', 'test-debug-key-for-the-retry-tests')

_tmp = tempfile.mkdtemp(prefix='homecore-retry-')
shutil.copytree(os.path.dirname(os.path.abspath(__file__)),
                os.path.join(_tmp, 'local'),
                ignore=shutil.ignore_patterns('backup_data', 'history', '__pycache__'))
os.chdir(os.path.join(_tmp, 'local'))
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


# --- a nanobot that fails on demand -------------------------------------------
PLAN = []          # one entry per request: 'ok', 'http:<code>', 'stream-error',
calls = []         # or 'text-then-error'


class _Nanobot(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = len(calls)
        calls.append(self.path)
        mode = PLAN[n] if n < len(PLAN) else 'ok'
        length = int(self.headers.get('Content-Length', 0))
        self.rfile.read(length)

        if mode.startswith('http:'):
            code = int(mode.split(':')[1])
            body = json.dumps({'error': {'message': f'boom {code}'}}).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()

        def send(obj):
            self.wfile.write(b'data: ' + json.dumps(obj).encode() + b'\n\n')
            self.wfile.flush()

        if mode == 'stream-error':
            send({'error': 'el proveedor está saturado'})
            return
        if mode == 'text-then-error':
            send({'choices': [{'delta': {'content': 'La mitad de una '}}]})
            send({'error': 'se cayó a mitad de camino'})
            return
        send({'choices': [{'delta': {'content': 'listo'}}]})
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()


_srv = HTTPServer(('127.0.0.1', 0), _Nanobot)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
API = 'http://127.0.0.1:%d' % _srv.server_address[1]

# The pauses are the real ones; keep them short so the suite stays quick.
A.TURN_RETRY_BACKOFF = (0.05, 0.05)
A.nanobot_auth_headers = lambda nid: {}
A._turn_deliver = lambda turn: None          # no history to file in a test
A._queue_advance = lambda user, chat_id: None


def run(plan):
    """Run one turn against `plan` and report what came back."""
    global PLAN
    PLAN = list(plan)
    calls.clear()
    turn = A._turn_new('user1', 'chat-1', '2026-08-08', 'c1', None)
    A._turn_worker(turn, API, 1, 'hola', False, None)
    kinds = [k for e in turn['events'] for k in e]
    return turn, kinds


print('a failure that will pass is tried again, and nobody has to be told why')
turn, kinds = run(['http:503', 'ok'])
check('it succeeded in the end', turn['error'] is None, turn['error'])
check('it took two goes', len(calls) == 2, len(calls))
check('the answer is the whole answer', turn['text'] == 'listo', turn['text'])
check('no error was ever shown', 'error' not in kinds, kinds)
check('but the retry was said out loud', 'hint' in kinds, kinds)

print('\nand it gives up rather than trying forever')
turn, kinds = run(['http:503', 'http:503', 'http:503', 'http:503'])
check('three goes, not four', len(calls) == 3, len(calls))
check('the error is reported in the end', turn['error'], turn['error'])

print('\na refusal that will refuse identically is not tried again')
turn, kinds = run(['http:400', 'ok'])
check('one go only', len(calls) == 1, len(calls))
check('and it says so straight away', turn['error'], turn['error'])

print('\nan error the stream reports is worth another go')
turn, kinds = run(['stream-error', 'ok'])
check('it retried', len(calls) == 2, len(calls))
check('and got the answer', turn['text'] == 'listo', turn['text'])

print('\nbut never once anything has been said')
# Half an answer followed by the second half of a *different* answer is worse
# than an error, and no amount of retrying can unsay what was already streamed.
turn, kinds = run(['text-then-error', 'ok'])
check('one go only', len(calls) == 1, len(calls))
check('the half it managed is kept', turn['text'] == 'La mitad de una ', turn['text'])
check('and the failure is honest', turn['error'], turn['error'])

print('\na cancel is not a failure, so it is not retried')
PLAN = ['http:503', 'ok']
calls.clear()
turn = A._turn_new('user1', 'chat-2', '2026-08-08', 'c1', None)
turn['cancelled'] = True
A._turn_worker(turn, API, 1, 'hola', False, None)
check('nothing was sent', len(calls) == 0, len(calls))
check('and it is not filed as an error', turn['error'] is None, turn['error'])

print('\nand Detener during a retry pause is felt at once, not when it runs out')
A.TURN_RETRY_BACKOFF = (30, 30)              # a pause nobody would sit through
PLAN = ['http:503', 'ok']
calls.clear()
turn = A._turn_new('user1', 'chat-3', '2026-08-08', 'c1', None)
started = time.time()
threading.Timer(0.3, lambda: A._turn_cancel(turn)).start()
A._turn_worker(turn, API, 1, 'hola', False, None)
elapsed = time.time() - started
check('it stopped in well under the pause', elapsed < 5, f'{elapsed:.1f}s')
check('and did not send the retry', len(calls) == 1, len(calls))

_srv.shutdown()
print()
if failures:
    print(f'{len(failures)} FAILED: ' + ', '.join(failures))
    raise SystemExit(1)
print('all checks passed')

"""What you can do about a background task, not just watch it.

Run: python local/test_bgtask_actions.py   (needs Flask; skips loudly without it)

The «En segundo plano» panel offered exactly one action, in every state: open
the conversation. So a task that turned out to be the wrong question ran to its
cap regardless — up to four hours of a model working on something nobody wanted
any more — and an answer could only be read where it landed.

Stopping is the half with teeth, and three things about it are load-bearing:

- **One task, never a session's worth.** nanobot's `cancel_by_session` is the
  right shape for /stop, where somebody is cancelling the turn they are in. In
  this panel each row is separate work with its own hours behind it, and taking
  a row's neighbours down with it is a bug that reads as a feature until four
  hours of research go with it.
- **It belongs to whoever owns it.** A task_id is a nanobot uuid, not a
  capability: knowing one must not be enough to stop somebody else's work.
- **`cancelled`, not `lost`.** No `done` event is coming — the coroutine simply
  ceases — and left alone the row would age into `lost`, which means "we do not
  know what happened to this". We do know: somebody stopped it.
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

tmp = tempfile.mkdtemp(prefix="homecore-bgact-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
PROXY_SECRET = "p" * 32
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET=PROXY_SECRET,
                  DEBUG_API_KEY="d" * 32)

USER1, USER2 = "user1", "user2"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2},'
            ' {"username": "%s", "nanobot_id": 3}]' % (USER1, USER2))

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_bgtask_db() if hasattr(A, 'init_bgtask_db') else None

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


asked = []


class FakeResp:
    def __init__(self, ok=True, payload=None, status=200):
        self.ok, self._payload, self.status_code = ok, payload or {}, status

    def json(self):
        return self._payload


def fake_post(url, headers=None, json=None, timeout=None):
    asked.append({'url': url, 'body': json})
    if fake_post.fail:
        return FakeResp(False, {}, 502)
    return FakeResp(True, {'ok': True, 'cancelled': fake_post.cancelled})


fake_post.fail = False
fake_post.cancelled = True
A.requests.post = fake_post

client = A.app.test_client()
H_USER1 = {'X-Proxy-Secret': PROXY_SECRET, 'X-Proxy-User': USER1}
H_USER2 = {'X-Proxy-Secret': PROXY_SECRET, 'X-Proxy-User': USER2}
DAY = A._tasks_today().isoformat()


def new_task(user=USER1, tid='task-1'):
    A._bgtask_start(user, tid, 'investigar el 502', DAY)
    return tid


def cancel(tid, headers=H_USER1):
    asked.clear()
    r = client.post(f'/chat/background-tasks/{tid}/cancel', headers=headers)
    return r.status_code, (r.get_json() or {})


print("stopping a task that is running")
tid = new_task()
code, out = cancel(tid)
check("it works", code == 200, out)
check("nanobot was asked to stop exactly this one",
      asked[-1]['body'] == {'task_id': tid}, asked[-1] if asked else None)
check("by task, not by session",
      asked[-1]['url'].endswith('/subagents/cancel'), asked[-1]['url'])
task = A._bgtask_get(USER1, tid)
check("the row says «cancelled», not «lost»", task['status'] == 'cancelled', task)
check("and it has a finish time", bool(task.get('finished_at')), task)
events = A._bgtask_events(USER1, tid)
check("the trace ends with why it stopped",
      any('Detenida por ti' in (e.get('text') or '') for e in events), events[-2:])

print("\nand it belongs to whoever owns it")
tid2 = new_task(USER1, 'task-2')
code, out = cancel(tid2, headers=H_USER2)
check("somebody else's task is simply unknown", code == 404, (code, out))
check("nothing was sent to nanobot on their behalf", not asked, asked)
check("and it is still running", A._bgtask_get(USER1, tid2)['status'] == 'running')

print("\nstopping one that already finished is not an error")
tid3 = new_task(USER1, 'task-3')
A._bgtask_finish(USER1, tid3, 'done')
code, out = cancel(tid3)
check("200 — the outcome asked for is the outcome", code == 200, (code, out))
check("it says nothing was cancelled", out.get('cancelled') is False, out)
check("nanobot was not troubled", not asked, asked)
check("and the status is left alone", A._bgtask_get(USER1, tid3)['status'] == 'done')

print("\nwhen the task had already stopped working on its own")
tid4 = new_task(USER1, 'task-4')
fake_post.cancelled = False
code, out = cancel(tid4)
check("still recorded as stopped", A._bgtask_get(USER1, tid4)['status'] == 'cancelled', out)
check("and the trace says so honestly",
      any('already finished' in (e.get('text') or '')
          for e in A._bgtask_events(USER1, tid4)), A._bgtask_events(USER1, tid4)[-2:])
fake_post.cancelled = True

print("\nand a nanobot that refuses does not leave a lie on the row")
tid5 = new_task(USER1, 'task-5')
fake_post.fail = True
code, out = cancel(tid5)
check("502 to the caller", code == 502, (code, out))
check("the task is still running", A._bgtask_get(USER1, tid5)['status'] == 'running')
fake_post.fail = False

print("\nan unknown task is a 404")
code, out = cancel('nope')
check("404", code == 404, (code, out))

print("\nand the panel offers what each state can actually do")
page = open(os.path.join(SRC, "templates", "chat.html"), encoding="utf-8").read()
check("«Stop» only while it runs", "t.status === 'running'" in page
      and "'Stop'" in page)
check("«Copy result» only when there is one", "if (t.result) {" in page
      and 'Copy the result' in page)
# Every path that posts to /cancel must ask first — there are two of them (the
# row and the detail header) and losing hours of work to a mis-tap is exactly
# the mistake a confirm exists for. Checked per call site rather than "the word
# askConfirm appears somewhere in the file", which the tooltip alone satisfies.
import re as _re
sites = [m.start() for m in _re.finditer(r"background-tasks/'\s*\+\s*encodeURIComponent\([^)]*\)\s*\+\s*'/cancel", page)]
check("both stop paths exist", len(sites) == 2, len(sites))
check("and each one asks before it fires",
      all('askConfirm' in page[max(0, at - 900):at] for at in sites),
      [page[max(0, at - 200):at][-80:] for at in sites])
check("«Detenida» is its own state, distinct from «Interrumpida»",
      "cancelled: { icon:'🛑'" in page and "lost:      { icon:'⛔'" in page)
# navigator.clipboard needs a secure context and a gesture, and the Android
# WebView regularly has neither.
check("copying has a fallback for the WebView", "execCommand('copy')" in page)

print()
shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")

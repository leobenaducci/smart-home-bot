"""Branching from a message, not only from where the conversation got to.

Run: python local/test_fork_here.py   (needs Flask; skips loudly without it)

Branches already existed: `_fork_start` opens one for a queued command, the
sidebar draws the ‹1/2› switcher from `branch_of`/`branch_at`, and a branch
stores only its own messages because copying would double every message in the
day file. None of that changes here. What changes is where a branch may be
anchored — any message, not just the end — and what the model is given.

**The model's side is the point.** A branch used to carry the parent as text
(`_fork_seed`): one big message at the top of an empty session, paid for on
every turn afterwards, and a transcript pretending to be a memory. nanobot's
sessions are files, so this asks it to copy the real prefix — genuine context,
no replay, nothing paid twice.

Two directions, and they cut on opposite sides of the anchor:

- your own question is "ask that again, differently" — the branch ends on the
  answer *before* it, and the question comes back to be edited;
- Alfred's answer is "carry on from here" — that answer is the last thing the
  branch knows.

Nothing is written when you fork. A branch exists once something is said in it,
which is what keeps an abandoned fork out of the sidebar — so the test that
matters most here is that a fork writes nothing at all.
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

tmp = tempfile.mkdtemp(prefix="homecore-fork-")
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

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


# --- A stand-in for nanobot ----------------------------------------------------
forks = []


class FakeResp:
    def __init__(self, ok=True, payload=None, status=200):
        self.ok, self._payload, self.status_code = ok, payload or {}, status

    def json(self):
        return self._payload


def fake_post(url, headers=None, json=None, timeout=None):
    forks.append(json)
    if fake_post.refuse:
        return FakeResp(False, {'error': {'message': 'not in this conversation any more'}}, 404)
    return FakeResp(True, {'ok': True, 'kept': 4})


fake_post.refuse = False
A.requests.post = fake_post

DAY = A._tasks_today().isoformat()
# Anchored to the day it claims to be in, not a fixed number. A day's history is
# now that day's — anything older is dropped at the door — so a literal
# timestamp ages into "this conversation is empty" a few days after it is
# written, which is a test that fails for a reason nobody is looking for.
CONV = A._day_floor_ms(DAY) + 9 * 3600 * 1000
HIST = [
    {'role': 'user', 'text': '¿qué compro para la once?', 'ts': CONV, 'conv': CONV},
    {'role': 'bot', 'text': 'pan, palta y té.', 'ts': CONV + 1, 'conv': CONV},
    {'role': 'user', 'text': 'y para el almuerzo?', 'ts': CONV + 2, 'conv': CONV},
    {'role': 'bot', 'text': 'tallarines, hay salsa.', 'ts': CONV + 3, 'conv': CONV},
    {'role': 'user', 'text': 'gracias', 'ts': CONV + 4, 'conv': CONV},
]
A.save_user_history(USER1, list(HIST), DAY)

client = A.app.test_client()
# Proxy auth, like the app itself uses through home-chat — and CSRF-exempt for
# the same reason it is there: the proxy authenticated the request.
HDRS = {'X-Proxy-Secret': PROXY_SECRET, 'X-Proxy-User': USER1}


def fork(ts):
    forks.clear()
    r = client.post('/chat/fork', headers=HDRS, json={'ts': ts, 'date': DAY})
    return r.status_code, (r.get_json() or {})


print("forking from your own question means asking it again")
code, out = fork(CONV + 2)
check("it works", code == 200, out)
check("the branch knows its parent", out.get('branch_of') == CONV, out)
check("and where it split", out.get('branch_at') == CONV + 2, out)
check("the question comes back to be edited", out.get('reask') == 'y para el almuerzo?', out)
check("nanobot was asked to cut before a user message",
      forks[-1]['anchor_role'] == 'user', forks[-1])
check("with the text as the anchor, not an index",
      forks[-1]['anchor_text'] == 'y para el almuerzo?', forks[-1])
check("source and target are the two conversations",
      forks[-1]['source_chat_id'].endswith(f':{CONV}')
      and forks[-1]['target_chat_id'].endswith(':%d' % out['conv']), forks[-1])

print("\nand from one of Alfred's answers means carrying on from it")
code, out = fork(CONV + 3)
check("it works", code == 200, out)
check("nothing is handed back to retype", out.get('reask') == '', out)
check("nanobot was asked to cut after an assistant message",
      forks[-1]['anchor_role'] == 'assistant', forks[-1])

print("\na fork writes nothing until something is said in it")
# The branch is real when its first message carries branch_of/branch_at. Until
# then an abandoned fork must leave no trace, or the sidebar fills with empty
# conversations nobody opened on purpose.
check("the day's history is untouched",
      A.load_user_history(USER1, DAY) == HIST, A.load_user_history(USER1, DAY))
# The `.conv` sidecar is what a turn joins when it names no conversation. A
# branch must never become it: a reminder or a voice note arriving now belongs
# in the conversation being had, not in the thread set running beside it — the
# same reason `_fork_start` deliberately does not call `_conv_write`.
check("and the day's current conversation was not moved to it",
      A._conv_read(USER1, DAY) != out.get('conv'), A._conv_read(USER1, DAY))

print("\nand then the first message is what makes it a branch")
conv = out['conv']
client.post('/chat/history', headers=HDRS, json={
    'role': 'user', 'text': 'mejor algo sin carne', 'ts': conv, 'conv': conv,
    'date': DAY, 'branch_of': CONV, 'branch_at': CONV + 3})
saved = A.load_user_history(USER1, DAY)
first = next((m for m in saved if m.get('conv') == conv), None)
check("it is stored", bool(first), saved[-1] if saved else None)
check("carrying which conversation it came from", first.get('branch_of') == CONV, first)
check("and at which message", first.get('branch_at') == CONV + 3, first)
check("so the sidebar can see it as a branch",
      any(s.get('branch_of') == CONV for s in A._split_sessions(saved)),
      A._split_sessions(saved))
# `_derived_conv` skips branches on purpose, for the same reason: what follows
# belongs in the conversation being had, not in the side thread.
check("but the day's main conversation is still the parent",
      A._derived_conv(saved) == CONV, A._derived_conv(saved))
check("and the branch is not what an unnamed turn would join",
      A._conv_read(USER1, DAY) != conv, A._conv_read(USER1, DAY))

print("\nbranch fields are ignored unless they name a conversation")
client.post('/chat/history', headers=HDRS, json={
    'role': 'user', 'text': 'suelto', 'ts': CONV + 90, 'date': DAY,
    'branch_of': CONV, 'branch_at': CONV})
loose = next(m for m in A.load_user_history(USER1, DAY) if m.get('text') == 'suelto')
check("no conv, no branch", 'branch_of' not in loose, loose)

print("\nwhen Alfred no longer has that moment, the fork is refused")
fake_post.refuse = True
code, out = fork(CONV)
check("409, not 500 — it is a fact about this fork, not a fault", code == 409, (code, out))
check("and it says so in Spanish", 'memoria' in (out.get('error') or ''), out)
check("still nothing written", A.load_user_history(USER1, DAY)[:5] == HIST)
fake_post.refuse = False

print("\nand a message that is not there cannot be forked from")
code, out = fork(12345)
check("404", code == 404, (code, out))
code, out = fork(0)
check("no anchor at all is a 400", code == 400, (code, out))

print()
shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")

"""Commands queued behind the one Alfred is answering.

Run: python local/test_queue.py   (needs Flask; skips loudly without it)

Alfred answers one thing at a time per conversation — nanobot serialises every
turn sharing a chat_id, because they are one model session. The box used to
lock while he worked, so a thought you had mid-answer was yours to hold. It
queues now, and the queue is the server's: it has to survive the phone locking,
the app being backgrounded and a Profesión switch, all of which are page loads.

What these pin down, in the order they can hurt:

- **The queue drains without a browser.** The whole point of putting it on the
  server is that nobody has to be watching for the next command to run.
- **One turn at a time per conversation.** Two turns in one model session
  produce an answer to neither question.
- **Stopping keeps what was said.** A cancelled turn's half-answer is filed and
  marked, never dropped and never filed as an error.
- **Replacing does both halves.** Stop this, run that, in that order.
- **A branch is a conversation, not a copy.** Its own id, its own model session,
  the parent's messages read back rather than duplicated into the day file.
- **Nothing crosses a person.** Every id here arrives in a request body.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-queue-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)

USER = "user1"
OTHER = "user2"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}, {"username": "%s", "nanobot_id": 3}]'
            % (USER, OTHER))

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_bgtask_db()
A.init_persona_db()

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


TODAY = A._tasks_today().isoformat()


class FakeStream:
    """nanobot's side of a streaming completion, under this test's control.

    `hold` is the turn taking its time; `closed` is what a cancel does to it —
    closing the response from another thread is exactly how `_turn_cancel`
    unblocks a worker parked on `iter_lines`.
    """

    def __init__(self, chunks, hold=None, hold_after=0):
        self.ok, self.status_code = True, 200
        self._chunks, self._hold, self._after = chunks, hold, hold_after
        self.closed = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        self.closed.set()
        if self._hold is not None:
            self._hold.set()          # let the generator wake and notice

    def iter_lines(self):
        # `hold_after` lets a turn say something and *then* stall, which is the
        # only interesting case for a cancel: a stop with nothing streamed yet
        # cannot show that the partial answer is kept.
        for i, c in enumerate(self._chunks):
            if self._hold is not None and i >= self._after:
                self._hold.wait()
            if self.closed.is_set():
                raise OSError("connection closed")
            yield c
        yield b"data: [DONE]"


def delta(text):
    return b"data: " + json.dumps(
        {"choices": [{"delta": {"content": text}}]}).encode()


streams, stops, sent = [], [], []


def install(chunks, hold=None, hold_after=0):
    """Point A.requests.post at a fake nanobot. /v1/stop is answered too — it is
    the out-of-band half of a cancel and the tests must see it fired."""
    def _post(url, **kw):
        if url.endswith("/stop"):
            stops.append(kw.get("json") or {})
            for s in streams:
                if not s.closed.is_set():
                    s.close()
            return type("R", (), {"ok": True, "status_code": 200,
                                  "json": staticmethod(lambda: {"stopped": 1})})()
        sent.append(kw.get("json") or {})
        stream = FakeStream(chunks, hold, hold_after)
        streams.append(stream)
        return stream
    A.requests.post = _post


def wait_until(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


A.app.config["TESTING"] = True
client = A.app.test_client()
with client.session_transaction() as s:
    s["user"] = USER
    s["csrf_token"] = "tok"

H = {"X-CSRF-Token": "tok"}


def send(text, space=None, conv=None):
    """Ask, the way the page asks: it persists the user's own message itself
    (POST /chat/history) and then opens the turn. /chat/send deliberately does
    not write it — so a test that skips the first half is testing a
    conversation whose questions were never said out loud."""
    body = {"content": text, "date": TODAY}
    if space:
        body["space"] = space
    if conv:
        body["conv"] = conv
    client.post("/chat/history", headers=H,
                json={"role": "user", "text": text, "ts": int(time.time() * 1000),
                      "conv": conv, "date": TODAY, "space": space})
    return client.post("/chat/send", headers=H, json=body)


def queue(text, space=None, conv=None, **extra):
    body = {"content": text, "date": TODAY, **extra}
    if space:
        body["space"] = space
    if conv:
        body["conv"] = conv
    return client.post("/chat/queue", headers=H, json=body)


def history(space=None):
    return A.load_user_history(USER, TODAY, space)


def running(space=None):
    return [t for t in client.get("/chat/turns" + (f"?space={space}" if space else ""))
            .get_json()["turns"] if t["running"]]


# --- Queueing while Alfred is busy --------------------------------------------
print("a command typed mid-answer waits its turn")
gate = threading.Event()
install([delta("primero")], hold=gate)
r = send("pregunta uno", space="teacher")
first_turn = r.headers["X-Turn-Id"]
conv = A._turn_get(first_turn, USER)["conv"]

q = queue("pregunta dos", space="teacher", conv=conv).get_json()
check("it is accepted", bool(q.get("id")), q)
check("and not started, because one is running", q.get("started") is None, q)
check("the page can see what is waiting",
      [i["content"] for i in q["queued"]] == ["pregunta dos"], q["queued"])
check("only one turn is running", len(running("teacher")) == 1, running("teacher"))

print("\nand starts on its own when the first one ends — with nobody watching")
gate.set()
check("the queue empties", wait_until(lambda: not A._queue_list(
    A._conv_chat_id(USER, TODAY, conv, "teacher"))), "still queued")
check("the second turn ran", wait_until(lambda: len(sent) == 2), sent)
check("in the same conversation", sent[-1]["chat_id"] == sent[0]["chat_id"],
      (sent[0]["chat_id"], sent[-1]["chat_id"]))
check("and it is the command that was queued",
      "pregunta dos" in json.dumps(sent[-1]["messages"], ensure_ascii=False))

print("\nqueueing into an idle conversation just runs it")
install([delta("al tiro")])
q = queue("pregunta tres", space="teacher", conv=conv).get_json()
check("it starts immediately", bool(q.get("started")), q)
check("and nothing is left waiting", q["queued"] == [], q)
check("the turn belongs to the same conversation",
      A._turn_get(q["started"], USER)["conv"] == conv)

print("\na command can be taken back before it runs")
gate2 = threading.Event()
install([delta("ocupado")], hold=gate2)
send("pregunta cuatro", space="teacher", conv=conv)
qid = queue("me arrepentí", space="teacher", conv=conv).get_json()["id"]
out = client.post("/chat/queue/cancel", headers=H,
                  json={"id": qid, "date": TODAY, "conv": conv,
                        "space": "teacher"}).get_json()
check("it leaves the queue", out["ok"] and out["queued"] == [], out)
before = len(sent)
gate2.set()
check("the running turn still finishes", wait_until(lambda: not running("teacher")))
time.sleep(0.1)
check("and the cancelled command never reached Alfred", len(sent) == before, sent[-1:])


# --- Stopping the turn in progress --------------------------------------------
print("\nstopping keeps what Alfred had already said")
gate3 = threading.Event()
install([delta("Voy a re"), delta("visar el mes")], hold=gate3, hold_after=1)
r = send("algo largo", space="teacher", conv=conv)
turn_id = r.headers["X-Turn-Id"]
check("it is running", wait_until(lambda: len(streams) and not streams[-1].closed.is_set()))
check("a partial answer accumulated",
      wait_until(lambda: (A._turn_get(turn_id, USER)["text"] or "").strip()))
check("and it is still going", not A._turn_get(turn_id, USER)["done"])

stopped = client.post(f"/chat/turn/{turn_id}/cancel", headers=H).get_json()
check("the cancel is accepted", stopped["ok"], stopped)
check("nanobot was told out of band", bool(stops), stops)
check("aimed at this conversation only",
      stops[-1]["chat_id"] == A._conv_chat_id(USER, TODAY, conv, "teacher"), stops[-1])
check("the turn ends", wait_until(lambda: A._turn_get(turn_id, USER)["done"]))
check("and not as an error", A._turn_get(turn_id, USER)["error"] is None,
      A._turn_get(turn_id, USER)["error"])

kept = [m for m in history("teacher") if m.get("interrupted")]
check("the half answer is filed", bool(kept), history("teacher")[-2:])
check("marked interrupted, so nothing reads it as complete",
      kept and kept[-1]["text"].startswith("Voy a re"), kept[-1:] )

print("\nstopping a turn that already ended is not an error")
check("it simply reports nothing to stop",
      client.post(f"/chat/turn/{turn_id}/cancel", headers=H).get_json()["ok"] is False)


# --- Replacing ----------------------------------------------------------------
print("\nreplace stops what is running and starts the new one")
gate4 = threading.Event()
install([delta("lo viejo")], hold=gate4)
r = send("lo que ya no quiero", space="teacher", conv=conv)
old_turn = r.headers["X-Turn-Id"]
before = len(sent)
out = client.post("/chat/queue/replace", headers=H,
                  json={"content": "mejor esto", "date": TODAY, "conv": conv,
                        "space": "teacher"}).get_json()
check("it says which turn it stopped", out["stopped"] == old_turn, out)
check("the old turn is over", wait_until(lambda: A._turn_get(old_turn, USER)["done"]))
check("and the replacement ran without being asked again",
      wait_until(lambda: len(sent) > before), (before, len(sent)))
check("it is the replacement",
      "mejor esto" in json.dumps(sent[-1]["messages"], ensure_ascii=False))
check("nothing is left in the queue",
      A._queue_list(A._conv_chat_id(USER, TODAY, conv, "teacher")) == [])


# --- Branching ----------------------------------------------------------------
print("\nfork runs a queued command in a conversation of its own")
install([delta("respuesta de la rama")])
send("tema principal", space="teacher", conv=conv)
check("the parent turn finished", wait_until(lambda: not running("teacher")))
gate5 = threading.Event()
install([delta("ocupado otra vez")], hold=gate5)
send("sigo con el tema", space="teacher", conv=conv)
qid = queue("y de paso, otra cosa", space="teacher", conv=conv).get_json()["id"]
# A different answer for the branch, installed before the fork's own POST:
# `install` closes over its chunks and builds a stream per request, so without
# this the branch's turn returns the parent's line verbatim and "did not land in
# the branch" below cannot tell the two apart. It only ever passed because the
# branch had not finished writing yet — which is exactly the race that made this
# file flaky, roughly two runs in three.
install([delta("solo de la rama")])
forked = client.post("/chat/queue/fork", headers=H,
                     json={"id": qid, "date": TODAY, "conv": conv,
                           "space": "teacher"}).get_json()
check("the fork is accepted", forked.get("ok"), forked)
check("it opened a new conversation", forked["conv"] != conv, forked)
check("and started right away, without waiting for the parent",
      bool(forked.get("turn")), forked)
check("the parent is still running", not A._turn_get(
    [t["id"] for t in running("teacher") if t["id"] != forked["turn"]][0], USER)["done"])

branch = forked["conv"]
sessions = client.get("/chat/sessions?space=teacher").get_json()["sessions"]
entry = [s for s in sessions if s["start"] == branch]
check("the branch is its own entry in the sidebar", len(entry) == 1, sessions)
check("and says which conversation it came out of",
      entry and entry[0].get("branch_of") == conv, entry)

read = client.get(f"/chat/history?date={TODAY}&start={branch}&space=teacher").get_json()
check("opening it shows the parent's messages above the fork",
      any(m.get("from_parent") and m["text"] == "tema principal" for m in read), read[:3])
check("and its own message below them",
      read[-1]["text"] == "y de paso, otra cosa" or
      any(m["text"] == "y de paso, otra cosa" for m in read), read[-2:])
check("the inherited ones are marked, not copied into the day",
      sum(1 for m in history("teacher") if m["text"] == "tema principal") == 1,
      [m["text"] for m in history("teacher")])

check("the branch's turn ran in its own model session",
      sent[-1]["chat_id"] == A._conv_chat_id(USER, TODAY, branch, "teacher"),
      sent[-1]["chat_id"])
check("carrying the parent conversation as context",
      "tema principal" in json.dumps(sent[-1]["messages"], ensure_ascii=False))
check("framed as history rather than as instructions",
      "not new instructions" in json.dumps(sent[-1]["messages"], ensure_ascii=False))
gate5.set()
check("the parent finishes in its own conversation",
      wait_until(lambda: not running("teacher")))
branch_msgs = A._session_slice(history("teacher"), branch)
check("the branch kept its own answer",
      any("solo de la rama" in m.get("text", "") for m in branch_msgs),
      [(m.get("role"), (m.get("text") or "")[:30]) for m in branch_msgs])
check("and the parent's did not land in it",
      not any("ocupado otra vez" in m.get("text", "") for m in branch_msgs),
      [(m.get("role"), (m.get("text") or "")[:30]) for m in branch_msgs])

print("\na branch is not where the next reminder lands")
check("the day's current conversation is still the one being had",
      A._conv_peek(USER, TODAY, "teacher") == conv,
      (A._conv_peek(USER, TODAY, "teacher"), conv, branch))


# --- Whose queue is it --------------------------------------------------------
print("\nthe ids arrive in a request body, so they are checked against the caller")
gate6 = threading.Event()
install([delta("de user1")], hold=gate6)
mine = send("algo mío", space="teacher", conv=conv).headers["X-Turn-Id"]
other = A.app.test_client()
with other.session_transaction() as s2:
    s2["user"] = OTHER
    s2["csrf_token"] = "tok"
check("another member cannot stop my turn",
      other.post(f"/chat/turn/{mine}/cancel", headers=H).status_code == 404)
check("nor see my queue",
      other.get(f"/chat/queue?date={TODAY}&conv={conv}&space=teacher")
      .get_json()["queued"] == [])
gate6.set()
wait_until(lambda: A._turn_get(mine, USER)["done"])


# --- The cap ------------------------------------------------------------------
print("\nthe queue is bounded")
gate7 = threading.Event()
install([delta("ocupadísimo")], hold=gate7)
send("bloqueante", space="teacher", conv=conv)
codes = [queue(f"cola {i}", space="teacher", conv=conv).status_code
         for i in range(A.QUEUE_MAX_PER_CONV + 3)]
check("it fills up and then refuses, rather than growing without end",
      codes.count(200) == A.QUEUE_MAX_PER_CONV and codes[-1] == 409, codes)
A._queues.clear()
gate7.set()

# --- Nothing typed is thrown away ---------------------------------------------
print("\nan advance that fails puts the command back rather than eating it")

A._queues.clear()
chat = A._conv_chat_id(USER, TODAY, "c-requeue")
item = A._queue_item(USER, TODAY, "c-requeue", None, "no me pierdas", [], [])
A._queue_add(chat, item)

# The failure a real one looks like: no nanobot for this user. It used to log
# "dropping queued command" and return, having already popped.
_real_nanobot_for = A._nanobot_for
A._nanobot_for = lambda u: (None, None)
try:
    A._queue_advance(USER, chat)
finally:
    A._nanobot_for = _real_nanobot_for
check("a missing nanobot leaves it queued",
      [i["content"] for i in A._queues.get(chat, [])] == ["no me pierdas"],
      A._queues.get(chat))

# …and an exception anywhere after the take, which the caller's `except` used
# to swallow into a log line that did not even contain the message.
A._nanobot_for = lambda u: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    A._queue_advance(USER, chat)
except RuntimeError:
    pass
finally:
    A._nanobot_for = _real_nanobot_for
check("so does a raise",
      [i["content"] for i in A._queues.get(chat, [])] == ["no me pierdas"],
      A._queues.get(chat))

# Order survives it: a requeue goes to the front, because it was the front.
A._queue_add(chat, A._queue_item(USER, TODAY, "c-requeue", None, "vine después", [], []))
A._nanobot_for = lambda u: (None, None)
try:
    A._queue_advance(USER, chat)
finally:
    A._nanobot_for = _real_nanobot_for
check("and it goes back to the front, not behind what arrived since",
      [i["content"] for i in A._queues.get(chat, [])] == ["no me pierdas", "vine después"],
      A._queues.get(chat))
A._queues.clear()

# --- The conversation you walked away from ------------------------------------
print("\na queue in a conversation nobody returns to still drains")

# `_queue_advance` is only ever called with one chat_id — from a turn ending in
# that conversation, or somebody acting in it. Queue something, press ✚, and
# nothing ends a turn there again: the item used to sit in _queues forever,
# invisible, because the page polls /chat/queue for the conversation it is
# looking at. Starting a new chat, switching Profesión and crossing midnight
# all change the chat_id, so this is the normal case and not an edge one.
A._queues.clear()
streams.clear()
install([delta("respondido igual")])
abandoned = A._conv_chat_id(USER, TODAY, "c-abandonada")
A._queue_add(abandoned, A._queue_item(
    USER, TODAY, "c-abandonada", None, "quedé en la cola", [], []))

turn = A._queue_advance(USER, abandoned)      # what the sweeper does
check("the sweep starts it", turn is not None)
if turn:
    deadline = time.time() + 5
    while not turn["done"] and time.time() < deadline:
        time.sleep(0.05)
    check("and it is answered", turn["done"] and "respondido" in (turn.get("text") or ""),
          turn.get("text"))
check("leaving nothing behind", not A._queues.get(abandoned), A._queues.get(abandoned))
A._queues.clear()

# --- Which day a message belongs to -------------------------------------------
print("\na message is filed under the day it is sent, not the day the page loaded")

TOMORROW = (A._tasks_today() + __import__("datetime").timedelta(days=1)).isoformat()
YESTERDAY = (A._tasks_today() - __import__("datetime").timedelta(days=1)).isoformat()

# chat.html renders `TODAY` once at page load and had no rollover, so a tab open
# since yesterday evening kept sending yesterday's date after midnight. Two of
# Alex's conversations on 2026-08-13 are stored under 2026-08-12 for exactly that
# reason — not lost, but filed where the next morning's view never looks.
check("a stale day is corrected", A._writing_day(YESTERDAY) == TODAY, A._writing_day(YESTERDAY))
check("so is a day in the future", A._writing_day(TOMORROW) == TODAY)
check("and rubbish", A._writing_day("no-soy-una-fecha") == TODAY)
check("today is left alone", A._writing_day(TODAY) == TODAY)

# Reading is not writing: this is how the sidebar opens last Tuesday.
check("reading still takes the day it was asked for",
      A._valid_day(YESTERDAY) == YESTERDAY, A._valid_day(YESTERDAY))

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")

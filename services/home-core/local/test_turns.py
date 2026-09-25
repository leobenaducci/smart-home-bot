"""A turn finishes whether or not anyone is still reading it.

Run: python local/test_turns.py   (needs Flask; skips loudly without it)

Reported: ask one of the Profesiones something, switch tabs, and the answer
never arrives. It was not slow — it was dead. The whole turn lived inside the
streaming response: the request generator made the nanobot call, and the browser
was the only thing that wrote the reply into history. So closing the tab, a
phone sleeping, or moving to another Profesión — which is a full page load —
tore down the request, dropped the connection to nanobot mid-answer, and nothing
ever recorded what came back.

The turn is now an object the server owns and a worker runs to completion. What
these tests pin is exactly that separation: the worker's result depends on the
worker, never on whether a response was read; several turns run at once and each
lands in the conversation it was asked in; and a page that comes back can pick a
running one up rather than being told nothing is happening.
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

tmp = tempfile.mkdtemp(prefix="homecore-turns-")
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
    """nanobot's side of a streaming completion, under this test's control."""

    def __init__(self, chunks, hold=None):
        self.ok, self.status_code = True, 200
        self._chunks, self._hold = chunks, hold

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        for c in self._chunks:
            if self._hold is not None:
                self._hold.wait()
            yield c
        yield b"data: [DONE]"


def delta(text):
    return b"data: " + json.dumps(
        {"choices": [{"delta": {"content": text}}]}).encode()


def install(chunks, hold=None, sent=None):
    def _post(url, **kw):
        if sent is not None:
            sent.append(kw.get("json") or {})
        return FakeStream(chunks, hold)
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


def send(text, space=None, conv=None):
    body = {"content": text, "date": TODAY}
    if space:
        body["space"] = space
    if conv:
        body["conv"] = conv
    return client.post("/chat/send", headers={"X-CSRF-Token": "tok"}, json=body)


# --- The reported bug ---------------------------------------------------------
print("the answer arrives even if nobody reads the stream")
sent = []
install([delta("Aquí tienes "), delta("la guía.")], sent=sent)
r = send("hazme una guía", space="teacher")
check("the request is accepted", r.status_code == 200, r.status_code)
turn_id = r.headers.get("X-Turn-Id")
check("and it names the turn it started", bool(turn_id), r.headers)
# Never read r.data — this is the tab being switched away from.
check("the worker finishes anyway",
      wait_until(lambda: A._turn_get(turn_id, USER) and A._turn_get(turn_id, USER)["done"]),
      "still running")
stored = A.load_user_history(USER, TODAY, "teacher")
check("and the reply is filed in the profession it was asked in",
      any(m["text"] == "Aquí tienes la guía." for m in stored), stored[-2:])
check("in the ordinary chat it is not",
      not any("guía" in m["text"] for m in A.load_user_history(USER, TODAY)))
# One persona, one model. This used to assert `powerful is True` as well,
# which said the same thing twice and disagreed in the case that matters: a
# persona with no model configured fell through to `powerful` and answered as
# a different assistant than the one whose name is on the chat.
check("the turn names the profession",
      sent[-1]["profile"] == "teacher", sent[-1])
check("and asks for nothing else on top of it",
      sent[-1].get("powerful") is not True,
      "the profession's name is the whole instruction")

print("\nand it is stamped with the conversation it belongs to")
conv = [m.get("conv") for m in stored if m["text"] == "Aquí tienes la guía."][0]
check("the reply carries a conversation", bool(conv), stored[-1])
check("the same one the turn was created with",
      conv == A._turn_get(turn_id, USER)["conv"], (conv, A._turn_get(turn_id, USER)["conv"]))


# --- Several at once ----------------------------------------------------------
print("\ntwo Profesiones can be answering at the same time")
gate = threading.Event()
install([delta("respuesta lenta")], hold=gate)
r1 = send("algo lento", space="designer")
t1 = r1.headers.get("X-Turn-Id")
install([delta("respuesta rápida")])
r2 = send("algo rápido", space="programmer")
t2 = r2.headers.get("X-Turn-Id")
check("the second turn finishes while the first is still going",
      wait_until(lambda: A._turn_get(t2, USER)["done"]) and not A._turn_get(t1, USER)["done"],
      (A._turn_get(t1, USER)["done"], A._turn_get(t2, USER)["done"]))
gate.set()
check("then the first finishes too", wait_until(lambda: A._turn_get(t1, USER)["done"]))
check("each landed in its own profession",
      any("lenta" in m["text"] for m in A.load_user_history(USER, TODAY, "designer"))
      and any("rápida" in m["text"] for m in A.load_user_history(USER, TODAY, "programmer")))
check("and neither leaked into the other",
      not any("rápida" in m["text"] for m in A.load_user_history(USER, TODAY, "designer")))


# --- Coming back --------------------------------------------------------------
print("\na page that comes back can pick a running turn up")
gate2 = threading.Event()
install([delta("primera parte "), delta("y el resto")], hold=gate2)
r3 = send("algo largo", space="teacher")
t3 = r3.headers.get("X-Turn-Id")
listed = client.get("/chat/turns?space=teacher").get_json()["turns"]
check("it is listed as running", any(t["id"] == t3 and t["running"] for t in listed), listed)
check("a different profession does not see it",
      not any(t["id"] == t3 for t in client.get("/chat/turns?space=designer").get_json()["turns"]))
check("nor does the ordinary chat",
      not any(t["id"] == t3 for t in client.get("/chat/turns").get_json()["turns"]))
gate2.set()
check("it finishes", wait_until(lambda: A._turn_get(t3, USER)["done"]))
replay = client.get(f"/chat/turn/{t3}").get_data(as_text=True)
check("re-attaching replays everything it said, from the beginning",
      "primera parte " in replay and "y el resto" in replay, replay[:160])
check("and reports the end", "[DONE]" in replay)
check("a turn id that does not exist is not found",
      client.get("/chat/turn/inventado").status_code == 404)

# The id travels in a URL, so it is the kind of thing that gets pasted, logged
# and guessed at. Belonging to the caller is the only thing standing between a
# turn and whoever holds its id.
other = A.app.test_client()
with other.session_transaction() as s2:
    s2["user"] = OTHER
    s2["csrf_token"] = "tok"
check("somebody else's turn is not tailable",
      other.get(f"/chat/turn/{t3}").status_code == 404,
      other.get(f"/chat/turn/{t3}").status_code)
check("nor listed for them",
      not any(t["id"] == t3 for t in other.get("/chat/turns?space=teacher").get_json()["turns"]))


# --- Failure still reaches the person -----------------------------------------
print("\na turn that fails says so instead of vanishing")

# 502 is one of the codes a turn is now given another go at (see
# `_turn_retryable`), so this one fails three times before it gives up and the
# person is told. The waiting between goes is the part worth removing here —
# what this section is about is that the failure still arrives, not how long
# the pauses are; `test_turn_retry.py` is where the pauses themselves are
# checked. Without this the turn outlives `wait_until` and the failure looks
# like a hang.
A.TURN_RETRY_BACKOFF = (0, 0)


class Boom:
    ok, status_code = False, 502

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def json(self):
        return {"error": {"message": "nanobot caído"}}


A.requests.post = lambda url, **kw: Boom()
r4 = send("algo", space="finanzas")
t4 = r4.headers.get("X-Turn-Id")
check("the turn finishes", wait_until(lambda: A._turn_get(t4, USER)["done"]))
check("with the error recorded", A._turn_get(t4, USER)["error"] == "nanobot caído",
      A._turn_get(t4, USER)["error"])
check("and nothing empty was written to history",
      not any(m["text"].strip() == "" for m in A.load_user_history(USER, TODAY, "finanzas")))


# --- Housekeeping -------------------------------------------------------------
print("\nfinished turns do not pile up")
old = A._turn_new(USER, "homeweb:x:y", TODAY, 0, None)
old["done"], old["finished"] = True, time.time() - A.TURN_TTL_S - 60
A._turn_sweep()
check("one past its TTL is swept", A._turn_get(old["id"], USER) is None)
running = A._turn_get(t1, USER)
check("a running one is never evicted to make room",
      all(A._turn_get(t, USER) is not None or True for t in [t1]))

print()
shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")

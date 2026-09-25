"""The Programmer profession, routed to opencode instead of the assistant.

Run: python local/test_opencode_programmer.py   (needs Flask; skips loudly without it)

Every other space is a nanobot turn. This one cannot be: `opencode serve` is an
agent server, not a gateway -- no /v1/chat/completions, and its `tools:` field
switches its own tools on and off rather than accepting ours. So the seam is a
whole second way of running a turn, and these are the parts of it that fail
silently if they are wrong:

- **The delta shape.** `message.part.delta` carries increments;
  `message.part.updated` carries a snapshot, and the *first* snapshot of a turn
  is the user's own message echoed back. Streaming those would print the
  question into the answer and then repeat the reply with every chunk. Measured
  against opencode 1.18.25 before the code was written.
- **The session filter.** `/event` is a *global* stream: every session on that
  server shares it. Without filtering, two people in the Programmer at once
  each receive the other's answer.
- **Which id names the directory.** The broker's checkout directories are keyed
  on the member id (`user1`), not the login the session carries and not the
  folder the share uses. A directory built from the wrong one is not an error;
  it is a session working somewhere that does not exist.
- **Falling back.** A household that has not switched `cloud.opencode` on must
  get the ordinary nanobot Programmer, not a broken space.
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-opencode-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)

# Before `import app`: OPENCODE_URL and friends are read at module level, and
# a suite that sets them afterwards is testing the defaults while believing it
# is testing the feature. The packaging suites learned this the expensive way.
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32,
                  # One server per member, and user2 deliberately has none:
                  # a member without their own opencode must not be served by
                  # somebody else's.
                  OPENCODE_SERVERS="user1=http://127.0.0.1:4096",
                  OPENCODE_WORKSPACE_ROOT="/state/nanobot-code-workspace")

# The login is a number and the member id is not -- that is the whole point of
# the third check below, so the fixture has to make them differ.
LOGIN = "999000111"
MEMBER = "user1"
# A second household member, who the opencode server is *not* configured for.
OTHER_LOGIN = "999000222"
OTHER_MEMBER = "user2"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write(json.dumps([{"username": LOGIN, "member": MEMBER, "nanobot_id": 2},
                        {"username": OTHER_LOGIN, "member": OTHER_MEMBER,
                         "nanobot_id": 3}]))

sys.path.insert(0, dst)
import app as A  # noqa: E402

BASE = "http://127.0.0.1:4096"
failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


# --- fakes -------------------------------------------------------------------

class FakeResp:
    def __init__(self, ok=True, status=200, payload=None, lines=None):
        self.ok, self.status_code = ok, status
        self._payload, self._lines = payload or {}, lines or []
        self.closed = False

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_lines(self):
        for ln in self._lines:
            if self.closed:
                return
            yield ln

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Recorder:
    """Stands in for `requests`, remembering what was asked of it."""

    def __init__(self, get=None, post=None):
        self.gets, self.posts = [], []
        self._get, self._post = get, post

    def get(self, url, **kw):
        self.gets.append((url, kw))
        return self._get(url, kw) if callable(self._get) else FakeResp()

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return self._post(url, kw) if callable(self._post) else FakeResp()


def sse(events):
    return [b"data: " + json.dumps(e).encode() for e in events]


def a_turn(space="programmer"):
    return A._turn_new(LOGIN, "homeweb:999000111:2026-09-02:dev", "2026-09-02",
                       None, space)


# --- is this space on opencode at all ----------------------------------------

print("the switch is the URL, the space, and *who is asking*")
check("the Programmer is", A._opencode_on("programmer"))
check("no other profession is", not A._opencode_on("teacher")
      and not A._opencode_on("designer") and not A._opencode_on("doctor"))
check("nor the ordinary chat", not A._opencode_on(None)
      and not A._opencode_on(""))

_saved_servers = A.OPENCODE_SERVERS
A.OPENCODE_SERVERS = {}
check("and with no opencode configured, nothing is -- the space falls back "
      "to the assistant", not A._opencode_on("programmer"))
A.OPENCODE_SERVERS = _saved_servers

# The one that would not fail, and would not look like anything. opencode holds
# a single MCP token, the profession spaces are open to every member, and a
# second member's turn would run in their own checkout while `list_projects`
# answered with the first member's projects and `save_text` wrote into the first
# member's folder on the share.
check("the member opencode is configured for goes there",
      A._opencode_on("programmer", LOGIN))
check("and anybody else stays on the assistant",
      not A._opencode_on("programmer", OTHER_LOGIN),
      "one opencode server holds one member's credentials")
check("including somebody with no account at all",
      not A._opencode_on("programmer", "nobody"))

check("each member is answered by their own server and no other",
      A._opencode_url(LOGIN) == "http://127.0.0.1:4096"
      and A._opencode_url(OTHER_LOGIN) == "",
      "a server holding one member's token must never answer for another")

# Two members, two servers: the map is what makes that expressible.
A.OPENCODE_SERVERS = {"user1": "http://127.0.0.1:4096",
                      "user2": "http://127.0.0.1:4097"}
check("with a server each, both are routed",
      A._opencode_on("programmer", LOGIN)
      and A._opencode_on("programmer", OTHER_LOGIN))
check("and each to their own",
      A._opencode_url(LOGIN).endswith(":4096")
      and A._opencode_url(OTHER_LOGIN).endswith(":4097"),
      (A._opencode_url(LOGIN), A._opencode_url(OTHER_LOGIN)))
A.OPENCODE_SERVERS = _saved_servers

print("\nthe map is parsed from one environment variable")
check("  pairs become a mapping",
      A._opencode_servers.__doc__ is not None)
import os as _os
_os.environ["OPENCODE_SERVERS"] = "user1=http://a:1, user2=http://b:2 ,"
check("  whitespace and a trailing comma are tolerated",
      A._opencode_servers() == {"user1": "http://a:1", "user2": "http://b:2"},
      A._opencode_servers())
_os.environ["OPENCODE_SERVERS"] = ""
check("  and empty is nobody, not everybody", A._opencode_servers() == {})
_os.environ["OPENCODE_SERVERS"] = "user1=http://127.0.0.1:4096"


# --- which of the three ids names the checkout -------------------------------

print("\nthe working directory is named by the member id, not the login")
d = A._opencode_directory(LOGIN)
check("it ends in the member id", d.endswith("/" + MEMBER), d)
check("and not in the login the session carries", LOGIN not in d, d)
check("it is under the broker's workspace",
      d == "/state/nanobot-code-workspace/user1", d)
check("somebody with no account gets nothing rather than a guessed path",
      A._opencode_directory("nobody") == "")

_saved_root = A.OPENCODE_WORKSPACE_ROOT
A.OPENCODE_WORKSPACE_ROOT = ""
check("and with no workspace configured, the session names no directory",
      A._opencode_directory(LOGIN) == "")
A.OPENCODE_WORKSPACE_ROOT = _saved_root


# --- the session per conversation --------------------------------------------

print("\none opencode session per conversation, made once and reused")
rec = Recorder(get=lambda u, kw: FakeResp(ok=True, payload={"id": "ses_A"}),
               post=lambda u, kw: FakeResp(payload={"id": "ses_A"}))
A.requests = rec
first = A._opencode_session(BASE, "chat-1", "/state/nanobot-code-workspace/user1")
check("a new conversation gets a session", first == "ses_A", first)
check("created with the working directory",
      any("directory" in (kw.get("params") or {}) for _, kw in rec.posts),
      rec.posts)

posts_before = len(rec.posts)
again = A._opencode_session(BASE, "chat-1", "/state/nanobot-code-workspace/user1")
check("asking again reuses it", again == "ses_A")
check("and does not make a second one", len(rec.posts) == posts_before,
      f"{len(rec.posts)} posts, was {posts_before}")

# A session opencode has forgotten: the person is holding a chat that looks
# continuous, and a 404 from a server they have never heard of is not an answer.
rec2 = Recorder(get=lambda u, kw: FakeResp(ok=False, status=404),
                post=lambda u, kw: FakeResp(payload={"id": "ses_B"}))
A.requests = rec2
replaced = A._opencode_session(BASE, "chat-1",
                              "/state/nanobot-code-workspace/user1")
check("a session opencode has forgotten is replaced", replaced == "ses_B",
      replaced)

# The directory is asked for, not assumed. v1's stream is scoped to it, so
# listening on one directory for a session living in another is 310s of
# silence -- a hang that reads as a long think rather than as a mistake.
rec2b = Recorder(get=lambda u, kw: FakeResp(ok=True, payload={"id": "ses_B"}),
                 post=lambda u, kw: FakeResp(payload={"id": "ses_D"}))
A.requests = rec2b
moved = A._opencode_session(BASE, "chat-1", "/state/somewhere-else/user1")
check("a session made in another directory is not reused", moved == "ses_D",
      moved)
# Put the conversation back where the checks below expect it.
A.requests = Recorder(get=lambda u, kw: FakeResp(ok=False, status=404),
                      post=lambda u, kw: FakeResp(payload={"id": "ses_B"}))
A._opencode_session(BASE, "chat-1", "/state/nanobot-code-workspace/user1")

# Unreachable is not forgotten. Making a second session because the server
# blinked would split one conversation in two and lose the first half.
def _boom(u, kw):
    raise OSError("connection refused")


rec3 = Recorder(get=_boom, post=lambda u, kw: FakeResp(payload={"id": "ses_C"}))
A.requests = rec3
kept = A._opencode_session(BASE, "chat-1", "/state/nanobot-code-workspace/user1")
check("but an unreachable server does not start a second conversation",
      kept == "ses_B" and not rec3.posts, f"{kept}, posts={rec3.posts}")


# --- the stream ---------------------------------------------------------------

print("\nthe answer is streamed from deltas, and only from deltas")
SID = "ses_A"
events = [
    # The user's own message, echoed back as a snapshot. Streaming this is the
    # bug this check exists for.
    {"type": "message.part.updated", "properties": {
        "sessionID": SID, "part": {"id": "prt_echo", "type": "text",
                                   "text": "what is broken?"}}},
    # The model thinking. Announced as `reasoning`, and then its deltas carry
    # `field: "text"` like everything else -- which is exactly how "El usuario
    # quiere que agregue dark mode al proyecto" reached a household's chat.
    # Measured against a live server: the announcement comes first, the deltas
    # carry only a partID, and nothing in the delta says what kind of part it
    # belongs to.
    {"type": "message.part.updated", "properties": {
        "sessionID": SID, "part": {"id": "prt_think", "type": "reasoning"}}},
    {"type": "message.part.delta", "properties": {
        "sessionID": SID, "partID": "prt_think", "field": "text",
        "delta": "El usuario quiere que agregue dark mode al proyecto."}},
    # The answer itself.
    {"type": "message.part.updated", "properties": {
        "sessionID": SID, "part": {"id": "prt_say", "type": "text"}}},
    {"type": "message.part.delta", "properties": {
        "sessionID": SID, "partID": "prt_say", "field": "text", "delta": "The "}},
    {"type": "message.part.delta", "properties": {
        "sessionID": SID, "partID": "prt_say", "field": "text", "delta": "worker "}},
    # Another session on the same server: /event is global.
    {"type": "message.part.delta", "properties": {
        "sessionID": "ses_SOMEBODY_ELSE", "partID": "prt_say", "field": "text",
        "delta": "SOMEBODY ELSE'S ANSWER"}},
    # A non-text field on our own answer part.
    {"type": "message.part.delta", "properties": {
        "sessionID": SID, "partID": "prt_say", "field": "reasoning",
        "delta": "(thinking)"}},
    {"type": "message.part.delta", "properties": {
        "sessionID": SID, "partID": "prt_say", "field": "text", "delta": "died."}},
    # The full snapshot opencode sends at the end.
    {"type": "message.part.updated", "properties": {
        "sessionID": SID, "part": {"id": "prt_say", "type": "text",
                                   "text": "The worker died."}}},
    {"type": "session.idle", "properties": {"sessionID": SID}},
    # Never read: the loop stops at idle, and the stream stays open for others.
    {"type": "message.part.delta", "properties": {
        "sessionID": SID, "partID": "prt_say", "field": "text",
        "delta": " AFTER IDLE"}},
]
A.requests = Recorder(
    get=lambda u, kw: (FakeResp(ok=True, payload={"id": SID})
                       if "/session/" in u and "/event" not in u
                       else FakeResp(lines=sse(events))),
    post=lambda u, kw: FakeResp(payload={"id": SID}))

turn = a_turn()
err, retry = A._turn_attempt_opencode(turn, "what is broken?")
check("it runs without error", err is None, err)
check("the text is the deltas, in order", turn["text"] == "The worker died.",
      repr(turn["text"]))
check("the user's own message is not echoed into the answer",
      "what is broken?" not in turn["text"], repr(turn["text"]))
check("the final snapshot does not repeat the answer",
      turn["text"].count("The worker died.") == 1, repr(turn["text"]))
check("another session's answer is not delivered here",
      "SOMEBODY ELSE" not in turn["text"], repr(turn["text"]))
check("a non-text field is not streamed as text",
      "thinking" not in turn["text"], repr(turn["text"]))
check("nothing after session.idle is read",
      "AFTER IDLE" not in turn["text"], repr(turn["text"]))
# Not decoration: v1's /event holds an unscoped connection open and sends
# nothing at all, which reads as a model thinking rather than as a mistake.
_dirs = [kw.get("params", {}).get("directory")
         for u, kw in A.requests.gets if u.endswith("/event")]
check("the v1 stream is scoped to the session's directory",
      _dirs == [A._opencode_directory(LOGIN)], _dirs)

check("the page was given the same text it accumulated",
      "".join(e["text"] for e in turn["events"] if "text" in e)
      == turn["text"], turn["events"])


print("\nfailures are reported, not swallowed")
A.requests = Recorder(
    get=lambda u, kw: (FakeResp(ok=True, payload={"id": SID})
                       if "/session/" in u and "/event" not in u
                       else FakeResp(lines=sse([
                           {"type": "session.error", "properties": {
                               "sessionID": SID,
                               "error": {"message": "model is disabled"}}}]))),
    post=lambda u, kw: FakeResp(payload={"id": SID}))
turn = a_turn()
err, retry = A._turn_attempt_opencode(turn, "hello")
check("a session error becomes the turn's error",
      err and "model is disabled" in err, err)

A.requests = Recorder(
    get=lambda u, kw: (FakeResp(ok=True, payload={"id": SID})
                       if "/session/" in u and "/event" not in u
                       else FakeResp(ok=False, status=503)),
    post=lambda u, kw: FakeResp(payload={"id": SID}))
turn = a_turn()
err, retry = A._turn_attempt_opencode(turn, "hello")
check("an unreachable event stream is retryable", err and retry, (err, retry))

A.requests = Recorder(
    get=lambda u, kw: (FakeResp(ok=True, payload={"id": SID})
                       if "/session/" in u and "/event" not in u
                       else FakeResp(lines=sse([]))),
    post=lambda u, kw: FakeResp(ok=False, status=400))
turn = a_turn()
err, retry = A._turn_attempt_opencode(turn, "hello")
check("a refused prompt is not retried -- 400 is the same 400 next time",
      err and not retry, (err, retry))


print("\nthe space only moves once opencode can answer as Alfred")
# opencode accepts `agent: "alfred-programmer"` with a 204 even when it has no
# such agent, and answers as its own `build` agent instead. Nothing errors --
# the Programmer just quietly becomes somebody else, in another voice, with
# another prompt. So the switch is checked rather than assumed, which is also
# what lets the routing ship before the persona does.
A._OPENCODE_READY.clear()
A.requests = Recorder(get=lambda u, kw: FakeResp(
    payload=[{"name": "build"}, {"name": "plan"}]))
check("with no such agent installed, the space stays on the assistant",
      not A._opencode_ready("http://127.0.0.1:4096"))

A._OPENCODE_READY.clear()
A.requests = Recorder(get=lambda u, kw: FakeResp(
    payload=[{"name": "build"}, {"name": "alfred-programmer"}]))
check("once the agent is there, it moves",
      A._opencode_ready("http://127.0.0.1:4096"))

A._OPENCODE_READY.clear()
A.requests = Recorder(get=_boom)
check("and an unreachable opencode is a fallback, not an error",
      not A._opencode_ready("http://127.0.0.1:4096"))

# Cheap enough to sit on every turn: asked once, then cached.
A._OPENCODE_READY.clear()
rec_ready = Recorder(get=lambda u, kw: FakeResp(
    payload=[{"name": "alfred-programmer"}]))
A.requests = rec_ready
for _ in range(3):
    A._opencode_ready("http://127.0.0.1:4096")
check("the check is cached rather than paid for on every turn",
      len(rec_ready.gets) == 1, f"{len(rec_ready.gets)} calls")


print("\na cancel reaches opencode, not just the socket")
# Closing the stream only stops this side reading: /event is global, so
# opencode carries on, burning a turn on an answer nobody will see and holding
# the session busy against the next question.
conn = A._opencode_conn()
conn.execute("INSERT OR REPLACE INTO sessions (chat_id, session_id, directory,"
             " created) VALUES (?,?,?,?)", ("chat-cancel", "ses_Z", "", 0))
conn.commit()
conn.close()
rec4 = Recorder()
A.requests = rec4
A._opencode_abort(BASE, "chat-cancel")
check("abort is posted for the session",
      any(u.endswith("/session/ses_Z/abort") for u, _ in rec4.posts),
      rec4.posts)

# Which side is told to stop is recorded on the turn, not decided again. The
# readiness check is cached and can flip between a turn starting and somebody
# pressing Stop, and a cancel that asked again could abort an opencode session
# for a turn nanobot is running.
turn = a_turn()
check("a turn says who answered it, from the start",
      turn.get("backend") == "nanobot", turn.get("backend"))

rec5 = Recorder()
A.requests = rec5
A._opencode_abort(BASE, "a-chat-that-never-ran")
check("and nothing is posted for a chat with no session", not rec5.posts,
      rec5.posts)


# --- v2 ----------------------------------------------------------------------

print("\nv2 is a different surface, not a different spelling")
_saved_api = A.OPENCODE_API
A.OPENCODE_API = "v2"

# The same chat_id as above, which already has a session -- made on v1, so it
# carries no agent, because v1 names the agent on every prompt instead. Reusing
# it here would answer as opencode's own `build` persona and say so nowhere,
# which is the failure `_opencode_ready` exists to prevent.
V2SID = SID
v2_events = [
    {"type": "session.next.prompt.admitted", "data": {"sessionID": V2SID}},
    {"type": "session.next.step.started", "data": {"sessionID": V2SID}},
    {"type": "session.next.reasoning.ended",
     "data": {"sessionID": V2SID, "text": "thinking out loud"}},
    {"type": "session.next.text.ended",
     "data": {"sessionID": V2SID, "text": "The worker died."}},
    {"type": "session.next.step.ended", "data": {"sessionID": V2SID}},
    # Never read: the loop stops at step.ended.
    {"type": "session.next.text.ended",
     "data": {"sessionID": V2SID, "text": " AFTER THE END"}},
]

# Order is the whole point of this one. v2's per-session stream sends nothing
# -- not even response headers -- until that session has an event, so a stream
# opened before the prompt blocks the request that would produce one and the
# turn deadlocks until the read timeout. That shipped, and from the outside it
# looked exactly like a server that was down.
order = []
rec_v2 = Recorder(
    get=lambda u, kw: (order.append("get:" + ("event" if "/event" in u
                                              else "session")),
                       FakeResp(ok=True, payload={"data": {"id": V2SID}})
                       if "/event" not in u
                       else FakeResp(lines=sse(v2_events)))[1],
    post=lambda u, kw: (order.append("post:" + ("prompt"
                                                if u.endswith("/prompt")
                                                else "session")),
                        FakeResp(payload={"data": {"id": V2SID,
                                                   "admittedSeq": 7}}))[1])
A.requests = rec_v2
turn = a_turn()
err, retry = A._turn_attempt_opencode(turn, "what is broken?")
check("it runs without error", err is None, err)
check("the prompt is posted before the event stream is opened",
      order.index("post:prompt") < order.index("get:event"),
      "a stream opened first blocks on headers until the read timeout: " + str(order))
check("the answer is the text of session.next.text.ended",
      turn["text"] == "The worker died.", repr(turn["text"]))
check("reasoning is not delivered as the answer",
      "thinking out loud" not in turn["text"], repr(turn["text"]))
check("nothing after step.ended is read",
      "AFTER THE END" not in turn["text"], repr(turn["text"]))

# Listening late loses nothing only because the stream is asked to replay.
_after = [kw.get("params", {}).get("after")
          for u, kw in rec_v2.gets if "/event" in u]
check("the stream replays from just before the admitted sequence",
      _after == [6], _after)

_prompts = [(u, kw) for u, kw in rec_v2.posts if u.endswith("/prompt")]
check("v2 prompts on /api and carries no agent -- the session holds it",
      _prompts and all(u.endswith("/api/session/" + V2SID + "/prompt")
                       and "agent" not in (kw.get("json") or {})
                       for u, kw in _prompts), rec_v2.posts)

# Which is only true because the session it prompts is one *this* surface made.
_made = [(u, kw) for u, kw in rec_v2.posts if u.endswith("/api/session")]
check("a session made on v1 is not reused on v2", len(_made) == 1, rec_v2.posts)
check("and the new one carries the agent, since v2's prompt cannot",
      _made and _made[0][1].get("json", {}).get("agent") == A.OPENCODE_AGENT,
      _made)

print("\nreadiness reads the agent list on either surface")
for field, surface in (("name", "v1"), ("id", "v2")):
    A.OPENCODE_API = surface
    A._OPENCODE_READY.clear()
    # v1 answers with a bare list, v2 wraps it in {location, data}. Both
    # shapes are here because the unwrap is as easy to get wrong as the field.
    rows = [{field: A.OPENCODE_AGENT}, {field: "build"}]
    A.requests = Recorder(get=lambda u, kw, r=rows, s=surface: FakeResp(
        ok=True, payload=({"data": r} if s == "v2" else r)))
    check("  %s names the agent by `%s`" % (surface, field),
          A._opencode_ready(BASE),
          "a field name guessed wrong reads exactly like the agent missing")
A.OPENCODE_API = "v2"
A._OPENCODE_READY.clear()
A.requests = Recorder(get=lambda u, kw: FakeResp(
    ok=True, payload={"data": [{"id": "build"}, {"id": "plan"}]}))
check("  and an opencode without it is not ready", not A._opencode_ready(BASE))

A.OPENCODE_API = _saved_api


print("\nthe model's thinking is not the answer")

# What reached a household's chat, verbatim: "El usuario quiere que agregue
# dark mode al proyecto. Necesito continuar explorando el proyecto." That is a
# `reasoning` part, and a reasoning part has a `text` field like any other --
# so filtering on `field == "text"` alone streams the model thinking out loud
# as though it were the reply. No wording in the agent prompt can fix that; it
# is not the model's output being wrong, it is this end reading the wrong parts.
#
# The sequence below is the one measured against a live opencode server: a
# part is announced with its type, and its deltas follow carrying only a
# partID. So the type has to be remembered from the announcement.
_events = [
    ("message.part.updated", {"part": {"id": "p1", "type": "reasoning"}}),
    ("message.part.delta",   {"partID": "p1", "field": "text",
                              "delta": "El usuario quiere que agregue dark mode"}),
    ("message.part.updated", {"part": {"id": "p2", "type": "tool"}}),
    ("message.part.delta",   {"partID": "p2", "field": "text", "delta": "glob(**/*.css)"}),
    ("message.part.updated", {"part": {"id": "p3", "type": "text"}}),
    ("message.part.delta",   {"partID": "p3", "field": "text", "delta": "Listo: "}),
    ("message.part.delta",   {"partID": "p3", "field": "text", "delta": "agregué modo oscuro."}),
    # Never announced: dropped, because dropping a reply is loud and leaking
    # thinking is quiet.
    ("message.part.delta",   {"partID": "p9", "field": "text", "delta": "(unannounced)"}),
    # A different field of a text part is not the text.
    ("message.part.delta",   {"partID": "p3", "field": "reasoning", "delta": "(other field)"}),
]


def _stream(events):
    """The filter exactly as _turn_attempt_opencode applies it."""
    part_types, out = {}, []
    for kind, props in events:
        if kind == "message.part.updated":
            part = props.get("part") or {}
            if part.get("id"):
                part_types[part["id"]] = part.get("type") or ""
            continue
        if kind == "message.part.delta":
            if props.get("field") != "text":
                continue
            if part_types.get(props.get("partID")) != "text":
                continue
            out.append(props.get("delta") or "")
    return "".join(out)


def _activity(events):
    """The tool lines the person sees while it works, as the loop emits them."""
    seen, shown = set(), []
    for kind, props in events:
        if kind != "message.part.updated":
            continue
        part = props.get("part") or {}
        if part.get("type") == "tool" and part.get("id") not in seen:
            seen.add(part.get("id"))
            n = part.get("tool") or part.get("name") or ""
            n = n[len("alfred_"):] if n.startswith("alfred_") else n
            if n:
                shown.append(n)
    return shown


# Hiding the thinking is only half of it. This model answers with `reasoning`
# and `tool` parts and no text until the end, so with the thinking gone there
# was nothing at all: four tool calls of silence, which reads as a hang. The
# tools are what it is *doing*, and they go to the chat's existing step
# channel -- the same one nanobot's tools use.
_work = [
    ("message.part.updated", {"part": {"id": "t1", "type": "tool",
                                       "tool": "alfred_list_projects"}}),
    # The same part again as it runs and finishes: one line, not three.
    ("message.part.updated", {"part": {"id": "t1", "type": "tool",
                                       "tool": "alfred_list_projects"}}),
    ("message.part.updated", {"part": {"id": "t2", "type": "tool",
                                       "tool": "read"}}),
]
check("what it is doing reaches the chat",
      _activity(_work) == ["list_projects", "read"], _activity(_work))
check("  and a tool is announced once, not once per state change",
      _activity(_work).count("list_projects") == 1)
check("  with the bridge's prefix stripped",
      "alfred_" not in "".join(_activity(_work)))
check("  and app.py emits them as steps",
      "'step': {'type': 'tool'" in pathlib.Path(__file__).with_name("app.py")
      .read_text(encoding="utf-8"))

_got = _stream(_events)
check("only the answer is streamed", _got == "Listo: agregué modo oscuro.", _got)
check("  the thinking never reaches the chat",
      "El usuario quiere" not in _got, _got)
check("  nor does tool output", "glob(" not in _got, _got)

# And the guard the whole thing rests on: the source really does check the
# part type, not just the field. A future edit that drops it puts the thinking
# straight back into the household's chat.
_src = pathlib.Path(__file__).with_name("app.py").read_text(encoding="utf-8")
check("  and app.py filters on the part type, not only the field",
      "part_types.get(props.get('partID')) != 'text'" in _src)

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    raise SystemExit(1)
print("all checks passed")

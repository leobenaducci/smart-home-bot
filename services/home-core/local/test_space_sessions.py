"""A profession keeps conversations, exactly like the ordinary chat.

Run: python local/test_space_sessions.py   (needs Flask; skips loudly without it)

The professions started as one continuous context per day: no sidebar, no "+ Nuevo",
one nanobot session called `homeweb:<user>:<day>:fin`. That was fine for a
single space and wrong for four — you cannot start a fresh design without
losing the previous one, and there was no way back to yesterday's.

So a space now splits into conversations the same way the ordinary chat does.
What this file pins is the part that is easy to get subtly wrong: *whose*
conversations. Every piece of the mechanism — the day files, the `.conv`
sidecar, the split, the sidebar, the titles, the model session — has to be
per-space, and a single one of them reading the unscoped path puts the
Diseñador's chat in the Profesor's sidebar, or pins the ordinary chat's
conversation to a turn that runs in a profession.

Unlike test_sessions.py this imports app.py rather than lifting a function out
by AST: what is under test here is the wiring between a dozen of them, and a
copy of that wiring in the test would only ever prove the copy right.
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

# A throwaway copy: this writes history files, a .conv sidecar and two SQLite
# databases, and none of that belongs in the working tree.
tmp = tempfile.mkdtemp(prefix="homecore-spaces-")
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
    f.write('[{"username": "%s", "nanobot_id": 2},'
            ' {"username": "%s", "nanobot_id": 3}]' % (USER, OTHER))

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_chat_titles_db()
A.init_bgtask_db()

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


DAY = "2026-08-01"
OLD = "2026-07-20"
MIN = 60 * 1000
GAP = A.CHAT_SESSION_GAP_MS


def say(role, text, ts, day=DAY, space=None, conv=None, user=USER):
    msg = {"role": role, "text": text, "ts": ts}
    if conv:
        msg["conv"] = conv
    A.append_user_history(user, msg, day, space)


# --- Storage: a space's days are its own ------------------------------------
print("history lives in the profession's own folder")
T0 = 1785700000000
say("user", "cuánto gasté en junio", T0, space="programmer", conv=T0)
say("user", "diagrama de la red", T0, space="designer", conv=T0)
say("user", "qué hay de cena", T0)
say("user", "algo viejo del profe", T0 - 5 * 86400000, day=OLD, space="teacher")

check("the ordinary chat lists only its own days",
      A._history_day_stems(USER) == sorted({DAY, A._tasks_today().isoformat()}, reverse=True),
      A._history_day_stems(USER))
check("a space lists only its own days",
      OLD in A._history_day_stems(USER, space="teacher")
      and OLD not in A._history_day_stems(USER),
      A._history_day_stems(USER, space="teacher"))
check("one space cannot see another's",
      DAY not in A._history_day_stems(USER, space="teacher"),
      A._history_day_stems(USER, space="teacher"))
check("an unknown space falls back to the ordinary chat, it does not invent one",
      A._history_path(USER, DAY, "../etc") == A._history_path(USER, DAY),
      A._history_path(USER, DAY, "../etc"))
check("listing days does not create the folder it looked in",
      not os.path.isdir(os.path.join("history", OTHER, "designer"))
      and A._history_day_stems(OTHER, space="designer") == [A._tasks_today().isoformat()]
      and not os.path.isdir(os.path.join("history", OTHER, "designer")),
      os.listdir(os.path.join("history", OTHER)) if os.path.isdir(
          os.path.join("history", OTHER)) else "no history at all")
check("the messages themselves stay apart",
      [m["text"] for m in A.load_user_history(USER, DAY, "programmer")] == ["cuánto gasté en junio"]
      and [m["text"] for m in A.load_user_history(USER, DAY)] == ["qué hay de cena"])

# --- The conversation pointer is per space ----------------------------------
print("\nthe .conv sidecar is per space")
A._conv_write(USER, DAY, 111, "programmer")
A._conv_write(USER, DAY, 222, "designer")
A._conv_write(USER, DAY, 333)
check("three sidecars, three values",
      (A._conv_read(USER, DAY, "programmer"), A._conv_read(USER, DAY, "designer"),
       A._conv_read(USER, DAY)) == (111, 222, 333),
      (A._conv_read(USER, DAY, "programmer"), A._conv_read(USER, DAY)))
check("they are different files",
      len({A._conv_path(USER, DAY), A._conv_path(USER, DAY, "programmer"),
           A._conv_path(USER, DAY, "designer")}) == 3)

print("\nresolving a conversation reads the space's own history")
now = int(time.time() * 1000)
today = A._tasks_today().isoformat()
say("user", "hace rato", now - GAP - MIN, day=today, space="programmer")
conv_dev, fresh_dev = A._conv_resolve(USER, today, space="programmer")
check("silence in a space opens a new conversation there", fresh_dev and conv_dev >= now,
      (conv_dev, fresh_dev))
say("user", "recién", now - MIN, day=today)
conv_norm, fresh_norm = A._conv_resolve(USER, today)
check("and it did not move the ordinary chat's pointer",
      not fresh_norm and conv_norm != conv_dev, (conv_norm, fresh_norm, conv_dev))
check("nor the other way round: the space kept what it resolved",
      A._conv_read(USER, today, "programmer") == conv_dev)

# --- The model session names both the space and the conversation ------------
print("\nthe chat_id carries the scope and the conversation")
cid = A._conv_chat_id(USER, DAY, 1785700000000, "designer")
check("space + conversation", cid == f"homeweb:{USER}:{DAY}:dsg:1785700000000", cid)
check("no conversation yet — the old 4-segment form still",
      A._space_chat_id(USER, DAY, "designer") == f"homeweb:{USER}:{DAY}:dsg")
check("the ordinary chat is untouched",
      A._conv_chat_id(USER, DAY, 42) == f"homeweb:{USER}:{DAY}:42")
check("a space's session can never equal the ordinary chat's",
      A._conv_chat_id(USER, DAY, 42, "programmer") != A._conv_chat_id(USER, DAY, 42))

# --- Titles ------------------------------------------------------------------
print("\ntitles are keyed by space too")
check("the unscoped key is unchanged (rows already in the table keep matching)",
      A._title_key(DAY, 999) == f"{DAY}:999")
check("two conversations that share a start do not share a title",
      len({A._title_key(DAY, 999), A._title_key(DAY, 999, "programmer"),
           A._title_key(DAY, 999, "teacher")}) == 3)

# --- The sidebar -------------------------------------------------------------
print("\n/chat/sessions is scoped to the page that asks")
A.app.config["TESTING"] = True
client = A.app.test_client()
with client.session_transaction() as s:
    s["user"] = USER
    s["csrf_token"] = "tok"

# Two separate conversations in the Diseñador, three hours apart.
say("user", "un afiche de fracciones", T0, space="designer", conv=T0)
say("bot", "listo", T0 + MIN, space="designer", conv=T0)
LATER = T0 + GAP + MIN
say("user", "ahora una portada", LATER, space="designer", conv=LATER)

sessions = client.get("/chat/sessions?space=designer").get_json()["sessions"]
starts = sorted(s["start"] for s in sessions if s["date"] == DAY)
check("both conversations are listed", starts == [T0, LATER], starts)
texts = " ".join(s.get("preview", "") + " " + s.get("first_user", "") for s in sessions)
check("and only this profession's",
      "portada" in texts and "diagrama" in texts and "cena" not in texts, texts)

# A day this profession has and the ordinary chat does not: listing has to walk
# the space's own folder, not the user's and then read the space's files out of
# it — that reads correctly right up to the first day they disagree on.
prof = client.get("/chat/sessions?space=teacher").get_json()["sessions"]
check("a day only the profession has is still listed",
      [s["date"] for s in prof] == [OLD], [s["date"] for s in prof])

normal = client.get("/chat/sessions").get_json()["sessions"]
check("the ordinary chat's sidebar never shows a profession's",
      not any("afiche" in (s.get("first_user") or "") for s in normal))
check("an unknown space is answered as the ordinary chat, not as an error",
      client.get("/chat/sessions?space=../../etc").get_json()["space"] == "")

one = client.get(f"/chat/history?date={DAY}&start={LATER}&space=designer").get_json()
check("opening one conversation returns just that one",
      [m["text"] for m in one] == ["ahora una portada"], one)

# --- The page itself ---------------------------------------------------------
print("\nthe profession's page draws the panel rather than hiding it")
page = client.get("/chat/designer")
html = page.get_data(as_text=True)
check("it renders", page.status_code == 200, page.status_code)
check("the sidebar is not hidden by a stylesheet any more",
      "#sidebar,#sidebar-toggle,#new-chat-btn{display:none" not in html)
check("+ Nuevo is on the page", 'id="new-chat-btn"' in html)
check("and the header says whose history it is", "Designer" in html)

# --- Sending from a profession -----------------------------------------------
# The one write path that decides everything downstream: what session the turn
# runs in. nanobot is replaced by a recorder — what is under test is the address
# on the envelope, not what the model does with it.
print("\n/chat/send addresses the profession's own conversation")
sent = []


class _FakeResponse:
    ok = True
    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        return iter([b'data: {"choices":[{"delta":{"content":"listo"}}]}', b'data: [DONE]'])


def _fake_post(url, **kw):
    sent.append(kw.get("json") or {})
    return _FakeResponse()


A.requests.post = _fake_post
NEWCONV = int(time.time() * 1000)
r = client.post("/chat/send", headers={"X-CSRF-Token": "tok"},
                json={"content": "hazme una portada", "space": "designer",
                      "conv": NEWCONV, "date": today})
r.get_data()          # drain the stream so generate() actually runs
check("the turn is sent", r.status_code == 200 and len(sent) == 1, (r.status_code, sent))
body = sent[-1] if sent else {}
check("to the space's own conversation",
      body.get("chat_id") == f"homeweb:{USER}:{today}:dsg:{NEWCONV}", body.get("chat_id"))
check("on the profession's own model", body.get("profile") == "designer", body)
# `powerful` is background work the assistant starts for itself now, not a
# second opinion layered over a persona that already names its model.
check("and not asking for a different one as well",
      body.get("powerful") is not True, body)
check("and it left the ordinary chat's pointer alone",
      A._conv_read(USER, today) != NEWCONV
      and A._conv_read(USER, today, "designer") == NEWCONV,
      (A._conv_read(USER, today), A._conv_read(USER, today, "designer")))

sent.clear()
r = client.post("/chat/send", headers={"X-CSRF-Token": "tok"},
                json={"content": "qué tal", "conv": NEWCONV, "date": today})
r.get_data()
check("the ordinary chat still addresses itself",
      sent and sent[-1].get("chat_id") == f"homeweb:{USER}:{today}:{NEWCONV}",
      sent[-1].get("chat_id") if sent else None)


# --- Replies coming back the other way ---------------------------------------
print("\n/chat/agent-event files a reply where it was actually said")
hdrs = {"X-Proxy-Secret": A._proxy_user_token(USER), "X-Proxy-User": USER}


def relay(chat_id, text, user=USER):
    h = {"X-Proxy-Secret": A._proxy_user_token(user), "X-Proxy-User": user}
    return client.post("/chat/agent-event", headers=h,
                       json={"type": "message", "text": text, "chat_id": chat_id})


r = relay(f"homeweb:{USER}:{DAY}:dsg:{LATER}", "aquí está la portada")
stored = A.load_user_history(USER, DAY, "designer")
check("a 5-segment id lands in the space", r.status_code == 200
      and stored[-1]["text"] == "aquí está la portada", (r.status_code, stored[-1:]))
check("stamped with the conversation it belongs to", stored[-1].get("conv") == LATER,
      stored[-1])
check("and not in the ordinary chat",
      all("portada" not in m["text"] for m in A.load_user_history(USER, DAY)))

relay(f"homeweb:{USER}:{DAY}:dsg", "un recordatorio del diseñador")
tail = A.load_user_history(USER, DAY, "designer")[-1]
check("a 4-segment space id still works, unstamped so it joins what is on screen",
      tail["text"] == "un recordatorio del diseñador" and "conv" not in tail, tail)

relay(f"homeweb:{USER}:{DAY}:dlg-dsg", "ruido de un delegado")
check("a delegate's stray line is NOT filed into the profession",
      A.load_user_history(USER, DAY, "designer")[-1]["text"] != "ruido de un delegado")

r = relay(f"homeweb:{OTHER}:{DAY}:dsg:{LATER}", "ajeno", user=USER)
check("a chat_id naming somebody else is still refused", r.status_code == 403, r.status_code)
check("and wrote nothing",
      all("ajeno" not in m["text"] for m in A.load_user_history(OTHER, DAY, "designer")))

r = relay(f"homeweb:{USER}:{DAY}:dsg:{LATER}:extra", "demasiados segmentos")
check("an id with more segments than exist is refused", r.status_code == 403, r.status_code)

print()

# --- the two professions added 2026-08-24 -----------------------------------
#
# Salud and Legal are wired like the other four, and two of their properties
# are worth a test rather than a reading. The tab says "Salud" and not
# "Médico": a label is what people remember, and this one must not promise a
# doctor. And Legal's whole accuracy story rests on looking the law up instead
# of recalling it — a model citing an article from memory sounds exactly as
# certain when it is wrong, so the instruction to search has to actually be in
# the persona the model is handed.

print("\nthe new professions are registered like the rest")
for key, scope, url in (('doctor', 'sal', 'doctor'), ('legal', 'ley', 'legal')):
    meta = A.CHAT_SPACES.get(key) or {}
    check(f"  {key} exists", bool(meta), list(A.CHAT_SPACES))
    check(f"  {key} scope is {scope}", meta.get('scope') == scope, meta.get('scope'))
    check(f"  {key} reads back from its scope",
          A.CHAT_SCOPE_SPACES.get(scope) == key, A.CHAT_SCOPE_SPACES)
    check(f"  {key} has a persona file",
          os.path.isfile(os.path.join(SRC, 'personas', f'{key}.md')))

check("every scope is still unique",
      len({m['scope'] for m in A.CHAT_SPACES.values()}) == len(A.CHAT_SPACES),
      [m['scope'] for m in A.CHAT_SPACES.values()])

print("\nSalud does not call itself a doctor where people look first")
med = A.CHAT_SPACES['doctor']
check("the tab says Salud", med['title'] == 'Health', med['title'])
check("and the hint says it is not a consultation",
      'not a substitute' in med['hint'].lower(), med['hint'])

print("\nLegal is told to look the law up rather than recall it")
legal = open(os.path.join(SRC, 'personas', 'legal.md'), encoding='utf-8').read()
check("web_search is named", 'web_search' in legal)
check("Ley Chile / BCN is named as the source", 'bcn.cl' in legal.lower())
check("and inventing an article is called out",
      'invent' in legal.lower(), legal[:0])

print("\nSalud knows when to stop talking and say go")
doctor = open(os.path.join(SRC, 'personas', 'doctor.md'), encoding='utf-8').read()
check("the emergency number is there", '131' in doctor)
for red in ('dolor de pecho', 'convulsi'):
    check(f"  names {red}", red in doctor.lower())
check("and it refuses to hand out doses",
      'dosis' in doctor.lower() and 'no indicas' in doctor.lower(), doctor[:0])

if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    shutil.rmtree(tmp, ignore_errors=True)
    raise SystemExit(1)
print("all checks passed")
shutil.rmtree(tmp, ignore_errors=True)


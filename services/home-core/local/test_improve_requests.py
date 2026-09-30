"""Asking Alfred to fix himself: the request, the card, the Programmer.

Run: python local/test_improve_requests.py   (needs Flask; skips loudly without it)

"The lights skill points to the wrong server -- fix it." The `self-improve`
skill files that here, and Alfred answers with a card that opens the
Programmer with the fix already written out. What is pinned:

* only somebody whose Programmer runs on opencode can file one -- anybody
  else has nowhere for it to go, and is told so rather than handed a card
  that opens the assistant;
* the card carries the request's number and nothing else, and the page fetches
  the text -- the model never copies a long prompt;
* only the person who asked can read the prompt back: it is their words;
* the prompt says what the fix may and may not do, because it is what the
  person reads before sending, and nothing in the page sends it for them.
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

tmp = tempfile.mkdtemp(prefix="homecore-improve-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
# The catalogue, as the deployer stages it beside the service: the cards and
# the failure message are read in the asker's language.
_repo_i18n = os.path.join(SRC, "..", "..", "..", "i18n")
if not os.path.isdir(os.path.join(dst, "i18n")) and os.path.isdir(_repo_i18n):
    shutil.copytree(_repo_i18n, os.path.join(dst, "i18n"))
os.chdir(dst)
CODER, OTHER = "999000111", "999000222"
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32, DEBUG_API_KEY="d" * 32,
                  OPENCODE_SERVERS="user1=http://127.0.0.1:4096",
                  IMPROVE_DIR="/srv/state/improve")
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "member": "user1", "nanobot_id": 1},'
            ' {"username": "%s", "member": "user2", "nanobot_id": 2}]' % (CODER, OTHER))

sys.path.insert(0, dst)
import app as A  # noqa: E402
_REAL_APPEND = A.append_user_history  # a later section stubs it

A.app.config["TESTING"] = True
failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def client(login):
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["user"] = login
        s["csrf_token"] = "tok"
    return c


H = {"X-CSRF-Token": "tok"}
coder, other = client(CODER), client(OTHER)

print("\nfiling a request")
check("the Programmer is on opencode for one of them", bool(A._opencode_url(CODER))
      and not A._opencode_url(OTHER), A.OPENCODE_SERVERS)
r = coder.post("/improve/api/requests", headers=H, json={
    "problem": "La skill de luces apunta al servidor equivocado; arreglala.",
    "context": "skill lights, flash_light: connection refused."})
d = r.get_json()
check("it is filed", r.status_code == 200 and d.get("ok") and d.get("id") == 1, d)
check("and answered with a card for the Programmer, carrying only the number",
      d.get("card", "").startswith(":::goto\nspace: programmer\nlabel: ") and "request: 1\n" in d["card"]
      and all(ln.split(":")[0] in ("space", "label", "request", "date", "conv", "why")
              for ln in d["card"].splitlines()[1:-1])
      and "luces" not in d["card"], d.get("card"))
r = other.post("/improve/api/requests", headers=H, json={"problem": "arreglá algo"})
check("somebody without an opencode Programmer is told why, and gets no card",
      r.status_code == 409 and "card" not in r.get_json() and "Programmer" in r.get_json()["error"],
      r.get_json())
dup = coder.post("/improve/api/requests", headers=H, json={"problem": "lo mismo otra vez"}).get_json()
check("asking again within minutes, while the first is open, is the same request",
      dup.get("duplicate") is True and dup.get("id") == 1, dup)
A.IMPROVE_DEDUP_S = -60  # the rest of this file files one request after another
check("an empty problem is refused",
      coder.post("/improve/api/requests", headers=H, json={"problem": "  "}).status_code == 400)
long = coder.post("/improve/api/requests", headers=H, json={"problem": "x" * 5000}).get_json()
conn = A._improve_conn()
check("and a long one is kept to its limit", len(conn.execute(
    "SELECT problem FROM improve_requests WHERE id = ?", (long["id"],)).fetchone()[0]) == 2000)
conn.close()

print("\nthe investigation starts at once, in its own Programmer conversation")
launched = []
A._nanobot_for = lambda login: ("http://nanobot.invalid", 1)
A._turn_launch = lambda *a, **kw: launched.append(a) or {"id": "t"}
r = coder.post("/improve/api/requests", headers=H, json={"problem": "el clima contesta en inglés"})
d = r.get_json()
check("it is started", d.get("investigating") is True and len(launched) == 1, d)
login, day, conv, space, content = launched[0][:5]
check("as the person, in the Programmer, in a new conversation",
      login == CODER and space == "programmer" and conv > 10 ** 12, launched[0][:4])
check("and the card opens that conversation", f"date: {day}\nconv: {conv}\n" in d["card"], d["card"])
hist = [m for m in A.load_user_history(CODER, day, "programmer") if m.get("conv") == conv]
check("the request is the conversation's first message, as theirs",
      hist and hist[0]["role"] == "user"
      and hist[0]["text"].startswith(f"Fix request #{d['id']}"), hist[:1])
conn = A._improve_conn()
st = conn.execute("SELECT status, day, conv FROM improve_requests WHERE id = ?", (d["id"],)).fetchone()
conn.close()
check("and the request remembers it", st == ("investigating", day, conv), st)
A.PROJECT_SPACES = set(getattr(A, "PROJECT_SPACES", ())) | {"programmer"}
with A.app.test_request_context():
    A.session["user"] = CODER
    check("a fix request's conversation is on Alfred himself, whatever is sent",
          A._valid_project({"project": "fracciones", "conv": conv}, "programmer") == "alfred-self"
          and A._valid_project({"conv": conv}, "programmer") == "alfred-self")
    check("and kept in any other conversation",
          A._valid_project({"project": "fracciones", "conv": conv + 1}, "programmer") == "fracciones")
    A.session["user"] = OTHER
    check("and it is that person's request that counts, not anybody's",
          A._valid_project({"project": "fracciones", "conv": conv}, "programmer") == "fracciones")
A._turn_launch = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("opencode down"))
d = coder.post("/improve/api/requests", headers=H, json={"problem": "otra cosa"}).get_json()
check("when it cannot start, the request is still filed and the card fills the input instead",
      d.get("ok") and d.get("investigating") is False and "conv:" not in d["card"], d)

print("\none task, one Programmer conversation")
A.IMPROVE_DEDUP_S = 15 * 60
launched.clear(); queued_c = []
A._turn_launch = lambda *a, **kw: launched.append(a) or {"id": "t"}
A._queue_add = lambda chat_id, item, front=False: queued_c.append((chat_id, item)) or True
A._queue_advance = lambda login, chat_id: None
ORIG = "websocket:homeweb:999000111:2026-09-29:1790700000001"
first = coder.post("/improve/api/requests", headers=H, json={
    "problem": "la skill del clima contesta en inglés", "origin": ORIG}).get_json()
A.time.sleep(0.002)  # conv is the start time in ms; two starts in one ms would collide here only
second = coder.post("/improve/api/requests", headers=H, json={
    "problem": "el recordatorio de tareas llega dos veces", "origin": ORIG}).get_json()
conn = A._improve_conn()
rows = {r[0]: r[1:] for r in conn.execute("SELECT id, day, conv, origin FROM improve_requests "
                                          "WHERE id IN (?, ?)", (first["id"], second["id"]))}
conn.close()
check("the first request opens a Programmer conversation",
      rows[first["id"]][1] > 0 and first.get("investigating"), rows)
check("a second, different one from the same chat opens its own -- never continues the first",
      len(launched) == 2 and not queued_c and second.get("investigating")
      and rows[second["id"]][1] > 0 and rows[second["id"]][1] != rows[first["id"]][1]
      and "continued" not in second, (second, rows))
dup = coder.post("/improve/api/requests", headers=H, json={
    "problem": "la skill del clima sigue contestando en inglés", "origin": ORIG}).get_json()
check("the same problem again from it is still the same request",
      dup.get("duplicate") and dup["id"] == first["id"] and len(launched) == 2, dup)
A.IMPROVE_DEDUP_S = -60

print("\npublishing a fix, asked for in the chat")
queued = []
A._queue_add = lambda chat_id, item, front=False: queued.append((chat_id, item)) or True
A._queue_advance = lambda login, chat_id: None
r = coder.post("/improve/api/requests/publish", headers=H, json={"deploy": True})
d = r.get_json()
latest = d.get("id")
check("the person's latest open request with a conversation is the one published",
      r.status_code == 200 and latest and queued, d)
chat_id, item = queued[-1]
check("its conversation is told to publish and deploy, through the tool",
      item["project"] == "alfred-self" and f"Publish and deploy fix request #{latest}" in item["content"]
      and f"improve publish {latest} <repo>" in item["content"]
      and f"improve deploy {latest} <repo>" in item["content"], item)
conn = A._improve_conn()
day_, conv_ = conn.execute("SELECT day, conv FROM improve_requests WHERE id = ?", (latest,)).fetchone()
conn.close()
check("in that request's own conversation, as the person's message",
      item["conv"] == conv_ and any(m.get("conv") == conv_ and m["text"].startswith("Publish and deploy")
                                    for m in A.load_user_history(CODER, day_, "programmer")))
check("and the card opens it", f"conv: {conv_}" in d.get("card", ""), d)
d = coder.post("/improve/api/requests/publish", headers=H, json={"id": latest}).get_json()
check("without deploy it only publishes", "deploy" not in queued[-1][1]["content"].split("now")[0]
      and "improve deploy" not in queued[-1][1]["content"], queued[-1][1]["content"])
A.OPENCODE_SERVERS["user2"] = "http://127.0.0.1:4097"
check("nobody publishes another person's request, even with a Programmer of their own",
      other.post("/improve/api/requests/publish", headers=H, json={"id": latest}).status_code == 404)
del A.OPENCODE_SERVERS["user2"]
check("a request that does not exist is said so",
      coder.post("/improve/api/requests/publish", headers=H, json={"id": 999}).status_code == 404)

print("\nthe special project: Alfred himself")
A.PROJECT_SPACES = ("programmer",)
A._projects_visible_to = lambda login: [{"slug": "fracciones", "name": "Fracciones"}]
lst = coder.get("/chat/projects?space=programmer").get_json()["projects"]
check("it is first in the Programmer's selector", lst[0]["slug"] == "alfred-self"
      and lst[0].get("special") and lst[1]["slug"] == "fracciones", lst)
check("and only for somebody whose Programmer can reach the tool",
      [p["slug"] for p in other.get("/chat/projects?space=programmer").get_json()["projects"]]
      == ["fracciones"])
blk = A._project_block("alfred-self", CODER)
check("choosing it tells the Programmer how fixes to Alfred are made",
      "not a project of the code broker" in blk and f"/srv/state/improve/bin/improve list --login {CODER}" in blk
      and "improve/bin/improve publish <id> <repo>" in blk and "never by hand" in blk, blk)
check("and to read the map, the rules and the lessons first, and back every claim",
      "/srv/state/improve/docs/MAP.md" in blk and "/srv/state/improve/lessons.md" in blk
      and "/srv/state/improve/docs/RULES.md" in blk
      and "never a premise" in blk and "a question is not a yes" in blk
      and "improve/bin/improve test <id> <repo>" in blk and "improve/bin/improve bench <id> <repo>" in blk
      and "improve/bin/improve measure" in blk and "improve/bin/improve lesson" in blk, blk)
check("and any other project is still a checkout", "checkout(\"fracciones\")" in A._project_block("fracciones", CODER))

print("\nthe prompt the Programmer opens with")
r = coder.get("/improve/api/requests/1/prompt")
p = (r.get_json() or {}).get("prompt", "")
check("the person's words, verbatim", "> La skill de luces apunta al servidor equivocado; arreglala." in p, p)
check("and what Alfred knew", "> skill lights, flash_light: connection refused." in p, p)
check("where a fix may go", "/srv/state/improve/repos.json" in p and "/srv/state/improve/inbox/" in p, p)
check("code is read in a worktree, never the live checkouts",
      "start 1 <repo>` first and read the worktree" in p and "reaching for them is refused" in p, p)
check("where the assistant that was asked actually runs",
      "docker exec nanobot-user1 printenv <NAME>" in p and "not the assistant's" in p, p)
check("and settings come from the person, not the admin's files", "ask me for a value" in p, p)
check("it investigates only, and asks before changing anything",
      "investigate only -- change nothing" in p and "ask me whether to apply it" in p
      and p.index("Only after I say yes") < p.index("commit 1"), p)
check("what Alfred knew is a lead to check, not a fact", "leads to check, not facts" in p, p)
check("the fix goes through the tool, in a worktree, never the deploying checkout",
      "/srv/state/improve/bin/improve start 1 <repo>" in p and "improve commit 1" in p
      and "never the checkout that deploys" in p and "Git itself is refused" in p, p)
check("and nothing broken is an answer", "nothing is broken, say so plainly" in p, p)
check("a setting is not code", "Never write a household value into code" in p, p)
check("the fix request says to read the map first, and to test and bench before committing",
      "/srv/state/improve/docs/MAP.md" in p and "/srv/state/improve/docs/RULES.md" in p
      and "improve test 1 <repo>" in p
      and "improve bench 1 <repo>" in p, p)
check("publish and deploy only when told, only through the tool",
      "only when I say so" in p and "/srv/state/improve/bin/improve publish 1 <repo>" in p
      and "improve deploy 1 <repo>" in p and "never by hand" in p
      and "do not work around it" in p, p)
r = other.get("/improve/api/requests/1/prompt")
check("nobody else can read it", r.status_code == 404, r.status_code)
lst = coder.get("/improve/api/requests").get_json()["requests"]
check("the person's list, newest first", [x["id"] for x in lst][-1] == 1 and lst[0]["id"] > 1, lst)
check("and nobody else's", other.get("/improve/api/requests").get_json()["requests"] == [])

print("\nwhat the portal was not there to file")
def om(role, texts, done=None):
    return {"info": {"role": role, "time": {"completed": done}},
            "parts": [{"type": "text", "text": t} for t in texts] + [{"type": "tool"}]}
msgs = [om("user", ["Publicá y desplegá."]), om("assistant", ["Publicando…"], 1000),
        om("assistant", ["Desplegado: home-core y admin."], 5000)]
got = A._opencode_unfiled(msgs, 900)
check("an answer opencode finished after the last one filed is found, whole",
      got == ("Publicando…\n\nDesplegado: home-core y admin.", 5000), got)
check("and not when the conversation already has it", A._opencode_unfiled(msgs, 4000) is None)
check("nor while the answer is unfinished",
      A._opencode_unfiled(msgs[:-1] + [om("assistant", ["…"], None)], 900) is None)
check("nor when the last word is the person's", A._opencode_unfiled(msgs + [om("user", ["¿y?"])], 900) is None)

print("\na failed Programmer turn says so")
err = ('opencode: {"name": "APIError", "data": {"message": "Upstream request failed: Endpoint is '
       'unavailable.", "statusCode": 521, "isRetryable": true}}')
txt = A._turn_failure_text(CODER, err)
check("the reason, short, and what to do", "Endpoint is unavailable." in txt and "(521)" in txt
      and "{" not in txt, txt)
filed = []
A.append_user_history = lambda user, msg, day, space: filed.append((space, msg)) or True
A._turn_deliver({"user": CODER, "text": "", "space": "programmer", "day": "2026-09-29", "conv": 5,
                 "id": "x"}, err)
check("filed in the conversation when nothing else was said",
      filed and filed[0][0] == "programmer" and "(521)" in filed[0][1]["text"], filed)
filed.clear()
A._turn_deliver({"user": CODER, "text": "", "space": "", "day": "2026-09-29", "conv": 5, "id": "x"}, err)
check("the ordinary chat keeps its own behaviour", filed == [], filed)
check("no generic «Publicar en master» under a fix request's answer",
      A._offers_fallback({"backend": "opencode", "space": "programmer", "project": "alfred-self",
                          "text": "¿Aplico el cambio?"}) == "")

print("\na Programmer conversation made a fix request")
A.append_user_history = _REAL_APPEND
conv_q = []
A._queue_add = lambda chat_id, item, front=False: conv_q.append((chat_id, item)) or True
A._queue_advance = lambda login, chat_id: None
DAY = A._tasks_today().isoformat()
CONV = 1790800000001
A.append_user_history(CODER, {"role": "user", "text": "¿por qué tarda tanto la skill de luces?",
                              "ts": CONV, "conv": CONV}, DAY, A.OPENCODE_SPACE)
d = coder.post("/improve/api/requests/convert", headers=H,
               json={"problem": "la skill de luces tarda 20 s", "conv": CONV, "date": DAY}).get_json()
conn = A._improve_conn()
row = conn.execute("SELECT day, conv, status, origin, problem FROM improve_requests WHERE id = ?",
                   (d.get("id"),)).fetchone()
conn.close()
check("the conversation the person is in becomes the request's, in place",
      d.get("ok") and d.get("investigating") and row and row[1] == CONV and row[2] == "investigating",
      (d, row))
check("with an origin of its own, so dedupe and 'publish the last fix' never take it for another",
      row and row[3] == f"programmer:{CONV}", row)
opening = conv_q[-1][1] if conv_q else {}
check("its opening message is queued there, on Alfred himself, behind whatever is running",
      opening.get("conv") == CONV and opening.get("project") == A.IMPROVE_PROJECT
      and opening.get("content", "").startswith(f"Fix request #{d.get('id')}, made from this conversation:")
      and "Everything above in this conversation is its context" in opening.get("content", ""),
      opening)
check("and filed, so it reads in the conversation where it was asked",
      any(m.get("conv") == CONV and m.get("text", "").startswith(f"Fix request #{d.get('id')}")
          for m in A.load_user_history(CODER, DAY, A.OPENCODE_SPACE)))
check("from then on every turn there is on Alfred himself, whatever the page sends",
      A._improve_conversation(CODER, CONV))
again = coder.post("/improve/api/requests/convert", headers=H,
                   json={"problem": "otra vez", "conv": CONV, "date": DAY})
check("a conversation that already is one says which, and makes no second",
      again.status_code == 409 and again.get_json().get("id") == d.get("id"), again.get_json())
missing = coder.post("/improve/api/requests/convert", headers=H,
                     json={"problem": "algo", "conv": CONV + 5, "date": DAY})
check("a conversation that is not there is refused", missing.status_code == 404)
check("and so is one with no issue named",
      coder.post("/improve/api/requests/convert", headers=H,
                 json={"problem": " ", "conv": CONV, "date": DAY}).status_code == 400)
theirs = client(OTHER).post("/improve/api/requests/convert", headers=H,
                            json={"problem": "algo", "conv": CONV, "date": DAY})
check("nor is anybody else's: another member's history has no such conversation",
      theirs.status_code in (404, 409), theirs.status_code)

print("\nthe page")
page = open(os.path.join(SRC, "templates", "chat.html"), encoding="utf-8").read()
check("a goto card may carry a request number and a conversation", "(space|label|why|request|date|conv)" in page)
check("a fix request's conversation shows the special project, locked, and sends it",
      "return m.role === 'user' && /^Fix request #\\d+/.test(m.text || '');" in page
      and "if (fixConv) return IMPROVE_PROJECT;" in page and "markFixConversation(msgs);" in page
      and "markFixConversation([]);" in page and "var IMPROVE_PROJECT = 'alfred-self';" in page)
check("a conversation made one partway through counts too, and the button to do it hides",
      "fixConv = (msgs || []).some(" in page
      and "convertBtn.hidden = fixConv || !(msgs || []).length;" in page
      and "'/improve/api/requests/convert'" in page)
check("the page and the server name the special project the same", A.IMPROVE_PROJECT == "alfred-self")
check("which it opens when the investigation started", "a.href += '?date=' + fields.date + '&conv=' + fields.conv" in page)
check("which becomes ?improve= on the Programmer's link", "a.href += '?improve=' + fields.request" in page)
check("the Programmer fetches the text into the input",
      "'/improve/api/requests/' + fix + '/prompt'" in page and "SPACE === 'programmer'" in page)
fill = page[page.index("var fix = params.get('improve');"):]
fill = fill[:fill.index("}).catch")]
check("and never sends it", "sendMessage" not in fill and "send(" not in fill and ".click()" not in fill, fill)
check("the requests are a login-keyed table",
      ("improve.db", "improve_requests", "username") in A.LOGIN_KEYED_TABLES)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)

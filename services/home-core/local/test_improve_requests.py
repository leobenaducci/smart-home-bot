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
A._turn_launch = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("opencode down"))
d = coder.post("/improve/api/requests", headers=H, json={"problem": "otra cosa"}).get_json()
check("when it cannot start, the request is still filed and the card fills the input instead",
      d.get("ok") and d.get("investigating") is False and "conv:" not in d["card"], d)

print("\nthe prompt the Programmer opens with")
r = coder.get("/improve/api/requests/1/prompt")
p = (r.get_json() or {}).get("prompt", "")
check("the person's words, verbatim", "> La skill de luces apunta al servidor equivocado; arreglala." in p, p)
check("and what Alfred knew", "> skill lights, flash_light: connection refused." in p, p)
check("where a fix may go", "/srv/state/improve/repos.json" in p and "/srv/state/improve/inbox/" in p, p)
check("it investigates only, and asks before changing anything",
      "investigate only -- change nothing" in p and "ask me whether to apply it" in p
      and p.index("Only after I say yes") < p.index("improve/1"), p)
check("what Alfred knew is a lead to check, not a fact", "leads to check, not facts" in p, p)
check("a branch in a worktree, never main", "never on main" in p, p)
check("and nothing broken is an answer", "nothing is broken, say so plainly" in p, p)
check("a setting is not code", "Never write a household value into code" in p, p)
check("and no deploy, no push", "Do not deploy and do not push" in p, p)
r = other.get("/improve/api/requests/1/prompt")
check("nobody else can read it", r.status_code == 404, r.status_code)
lst = coder.get("/improve/api/requests").get_json()["requests"]
check("the person's list, newest first", [x["id"] for x in lst][-1] == 1 and lst[0]["id"] > 1, lst)
check("and nobody else's", other.get("/improve/api/requests").get_json()["requests"] == [])

print("\nthe page")
page = open(os.path.join(SRC, "templates", "chat.html"), encoding="utf-8").read()
check("a goto card may carry a request number and a conversation", "(space|label|why|request|date|conv)" in page)
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

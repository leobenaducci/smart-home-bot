"""A Programmer conversation remembers its project on the server.

Run: python local/test_conversation_project.py   (needs Flask; skips without it)

The project selector used to be one localStorage value per browser. Open the
same conversation from a laptop after starting it on a phone and the laptop's
last choice was selected -- the next message pointed the Programmer at a
different checkout from the one the conversation was about, and nothing said
so. The project is now filed with the conversation's messages, and the page
asks before a switch in the middle of one.

Both halves are pinned here: the server stamps (from the page's POST, from the
turn that files the reply, and onto a duplicate the page got to first), and the
page restores from the stamp and confirms a switch.
"""
import os
import re
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

tmp = tempfile.mkdtemp(prefix="homecore-convproject-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)

USER = "999000111"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}]' % USER)

sys.path.insert(0, dst)
import app as A  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


DAY = "2026-08-01"
T0 = 1785700000000
SPACE = "programmer"

A.app.config["TESTING"] = True
client = A.app.test_client()
with client.session_transaction() as s:
    s["user"] = USER
    s["csrf_token"] = "tok"


def post(**body):
    body.setdefault("date", DAY)
    return client.post("/chat/history", json=body, headers={"X-CSRF-Token": "tok"})


def stored():
    return A.load_user_history(USER, DAY, SPACE)


print("the page's message carries its project into history")
r = post(role="user", text="arreglá el login", ts=T0, conv=T0, space=SPACE, project="web-app")
check("accepted", r.status_code == 200, r.status_code)
check("filed with the project", stored()[-1].get("project") == "web-app", stored()[-1])

post(role="user", text="y el registro", ts=T0 + 1000, conv=T0, space=SPACE,
     project="../../etc")
check("a value that is not a slug is not filed", "project" not in stored()[-1], stored()[-1])

post(role="user", text="qué hay de cena", ts=T0, conv=T0, project="web-app")
check("outside a project space nothing is filed",
      "project" not in A.load_user_history(USER, DAY)[-1], A.load_user_history(USER, DAY))

print("\nthe turn files the reply with the project it ran on")
turn = A._turn_new(USER, "homeweb:x", DAY, T0, SPACE, project="web-app")
turn["text"] = "Listo, el login anda."
A._turn_deliver(turn)
check("the reply carries it", stored()[-1].get("project") == "web-app", stored()[-1])

# The page often files the same reply first, with no project; the server's
# copy is a duplicate then, and its project must not go with it.
# Now, not T0: the dedup window is about wall-clock time, and the turn files with time.time().
post(role="bot", text="Ahora el registro también.", ts=int(time.time() * 1000), conv=T0,
     space=SPACE)
turn = A._turn_new(USER, "homeweb:x", DAY, T0, SPACE, project="api")
turn["text"] = "Ahora el registro también."
A._turn_deliver(turn)
last = stored()[-1]
check("a duplicate's project lands on the page's copy",
      last.get("text") == "Ahora el registro también." and last.get("project") == "api"
      and last.get("conv") == T0, last)
check("and it was not filed twice",
      sum(m.get("text") == "Ahora el registro también." for m in stored()) == 1)

other = A._turn_new(USER, "homeweb:y", DAY, T0, "designer", project="web-app")
other["text"] = "un afiche"
A._turn_deliver(other)
check("a space with no selector files no project",
      "project" not in A.load_user_history(USER, DAY, "designer")[-1])

print("\nthe page restores it and asks before a switch")
src = open(os.path.join(SRC, "templates", "chat.html"), encoding="utf-8").read()
check("persistMsg sends the project",
      re.search(r"project: role === 'user' \? activeProject\(\) : null", src) is not None)
check("painting a conversation restores its project",
      re.search(r"function markFixConversation[\s\S]*?applyConversationProject\(msgs\);\n  \}",
                src) is not None)
check("the newest stamp wins",
      re.search(r"for \(var i = \(msgs \|\| \[\]\)\.length - 1; i >= 0; i--\)", src) is not None)
check("the loaded list prefers the conversation's project over localStorage",
      "if (convMatched) projectSel.value = convProject;" in src)
change = re.search(r"projectSel\.addEventListener\('change', function\(\) \{([\s\S]*?)\n    \}\);",
                   src)
check("a switch mid-conversation asks first",
      bool(change) and "window.confirm" in change.group(1)
      and "projectSel.value = convProject" in change.group(1),
      "a cancelled confirm has to put the conversation's project back")
check("a repaint does not undo a confirmed switch", "appliedProjectKey" in src)
# Staged into the build context in the image, at the repository root on a checkout.
CATALOGUE = next(p for p in (os.path.join(SRC, "i18n", "en.json"),
                             os.path.join(SRC, "..", "..", "..", "i18n", "en.json"))
                 if os.path.exists(p))
for key in ("chat.project_switch_confirm", "chat.project_none"):
    check(f"{key} is in en.json",
          f'"{key}"' in open(CATALOGUE, encoding="utf-8").read())

shutil.rmtree(tmp, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("all checks passed")

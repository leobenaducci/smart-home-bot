"""One profession, two jobs — and which one you get.

Run: python local/test_persona_modes.py   (needs Flask; skips loudly without it)

Profesor serves the child doing the homework and the teacher who assigns it, and
the rules are opposite: with a student you withhold the answer on purpose, with
the teacher withholding it is an obstacle. A mode picks which file loads.

This is worth pinning because the failure is silent and expensive. Nothing
crashes when the wrong file loads — the teacher just gets asked "¿hasta dónde
llegaste?" while trying to print an answer key, and it reads as Alfred being
obtuse rather than as a configuration that never took effect.

The content checks are deliberately about *rules*, not wording: they assert that
the Socratic rule is present in one mode and absent from the other, and that
what both modes share reaches both. A rewrite may rephrase anything; it may not
quietly drop the one rule that distinguishes the two.
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

tmp = tempfile.mkdtemp(prefix="homecore-personas-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)

TEACHER = "user1"
KID = "user2"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2},'
            ' {"username": "%s", "nanobot_id": 3}]' % (TEACHER, KID))

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_persona_db()

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


# The rule that separates the two modes, and the one that must not be in either
# file twice. Matched loosely on purpose — a rewrite may rephrase.
SOCRATIC = "Don't give the answer to an exercise straight away"
DIRECT = "No Socratic method with them"


# --- What each profession declares -------------------------------------------
print("only Profesor has modes")
check("teacher declares two", list(A._space_modes("teacher")) == ["tutor", "assistant"],
      list(A._space_modes("teacher")))
check("the others have none",
      all(not A._space_modes(s) for s in ("finanzas", "programmer", "designer")))
check("tutor is the default, so nobody moves without choosing",
      A._space_default_mode("teacher") == "tutor", A._space_default_mode("teacher"))
check("a profession with no modes has no default",
      A._space_default_mode("finanzas") is None)
check("every declared mode has a file on disk",
      all(os.path.isfile(os.path.join("personas", f"teacher.{m}.md"))
          for m in A._space_modes("teacher")),
      sorted(os.listdir("personas")))


# --- The files compose --------------------------------------------------------
print("\neach mode is the shared part plus its own")
base = open(os.path.join("personas", "teacher.md"), encoding="utf-8").read()
tutor = A._persona_default("teacher", "tutor")
asis = A._persona_default("teacher", "assistant")
check("the shared part reaches both",
      "profession" in tutor and "profession" in asis
      and "document" in tutor and "document" in asis)
check("and it is not duplicated into either file",
      tutor.count("action\": \"delegate") == 1 and asis.count("action\": \"delegate") == 1,
      (tutor.count("action\": \"delegate"), asis.count("action\": \"delegate")))
check("the shared part comes first, so a mode can override it",
      tutor.startswith(base[:80]) and asis.startswith(base[:80]))

print("\nthe rule that separates them")
check("Tutor withholds the answer", SOCRATIC in tutor)
check("the assistant mode does NOT", SOCRATIC not in asis)
check("the assistant mode says so explicitly", DIRECT in asis)
check("Tutor does not carry the teacher's rules", DIRECT not in tutor)
check("only the assistant mode knows about mark schemes and rubrics",
      "rubric" in asis.lower() and "rubric" not in tutor.lower())
check("only Tutor is told to look up who it is talking to",
      "FAMILY.md" in tutor and "FAMILY.md" not in asis)


# --- Per user, not per house --------------------------------------------------
print("\nthe choice is per person")
check("unset reads as the default", A._persona_mode(TEACHER, "teacher") == "tutor")
A._persona_mode_set(TEACHER, "teacher", "assistant")
check("the teacher moved", A._persona_mode(TEACHER, "teacher") == "assistant")
check("the kid did not", A._persona_mode(KID, "teacher") == "tutor")
check("a space with no modes has no mode", A._persona_mode(TEACHER, "finanzas") is None)
check("a mode that no longer exists falls back rather than half-loading",
      A._persona_mode_set(KID, "teacher", "socratico") == "tutor"
      and A._persona_mode(KID, "teacher") == "tutor")


# --- The block the model actually receives ------------------------------------
print("\nthe composed block")
blk_t = A._space_block("teacher", KID)
blk_a = A._space_block("teacher", TEACHER)
check("the header names the mode", blk_a.startswith("[Context: Teacher — Teaching assistant]")
      and blk_t.startswith("[Context: Teacher — Tutor]"), blk_a[:60])
check("a profession with no modes keeps its plain header",
      A._space_block("programmer", TEACHER).startswith("[Context: Programmer]"))
check("the teacher's block has no Socratic rule in it at all", SOCRATIC not in blk_a)
check("the kid's does", SOCRATIC in blk_t)

print("\nthe personal override still sits below everything")
A._persona_override_set(TEACHER, "teacher", "Trabajo en 7° y 8° básico.")
blk = A._space_block("teacher", TEACHER)
check("it is appended", "Trabajo en 7° y 8° básico." in blk)
check("after the mode's rules, not before",
      blk.index(DIRECT) < blk.index("Trabajo en 7°"))
check("and it did not disturb the mode", A._persona_mode(TEACHER, "teacher") == "assistant")


# --- The two settings are independent -----------------------------------------
print("\nchanging one setting does not clear the other")
A._persona_mode_set(TEACHER, "teacher", "tutor")
check("switching mode keeps what was written",
      A._persona_override(TEACHER, "teacher") == "Trabajo en 7° y 8° básico.")
A._persona_override_set(TEACHER, "teacher", "")
check("clearing the text keeps the mode",
      A._persona_mode(TEACHER, "teacher") == "tutor")
A._persona_mode_set(TEACHER, "teacher", "assistant")


# --- Over HTTP ----------------------------------------------------------------
print("\n/chat/persona carries both")
A.app.config["TESTING"] = True
client = A.app.test_client()
with client.session_transaction() as s:
    s["user"] = TEACHER
    s["csrf_token"] = "tok"

d = client.get("/chat/persona?space=teacher").get_json()
check("GET lists the modes with labels",
      [m["key"] for m in d["modes"]] == ["tutor", "assistant"]
      and all(m.get("label") and m.get("hint") for m in d["modes"]), d.get("modes"))
check("and says which one is on", d["mode"] == "assistant", d.get("mode"))
check("a space with no modes returns an empty list",
      client.get("/chat/persona?space=programmer").get_json()["modes"] == [])

r = client.put("/chat/persona?space=teacher", headers={"X-CSRF-Token": "tok"},
               json={"text": "Hago clases de matemática."})
check("PUT with only text leaves the mode alone",
      r.get_json()["mode"] == "assistant", r.get_json())
r = client.put("/chat/persona?space=teacher", headers={"X-CSRF-Token": "tok"},
               json={"mode": "tutor"})
check("PUT with only mode leaves the text alone",
      r.get_json()["text"] == "Hago clases de matemática."
      and r.get_json()["mode"] == "tutor", r.get_json())
r = client.put("/chat/persona?space=teacher", headers={"X-CSRF-Token": "tok"},
               json={"mode": "inventado"})
check("an unknown mode is refused into the default, not stored",
      r.get_json()["mode"] == "tutor", r.get_json())


# --- The page says which one is on --------------------------------------------
print("\nthe header names the mode, painted server-side")
A._persona_mode_set(TEACHER, "teacher", "assistant")
html = client.get("/chat/teacher").get_data(as_text=True)
# The label the app would render, not the English one written here. Mode
# labels go through the catalogue now, so a Spanish household gets the Spanish
# word -- which is the point -- and asserting the English string passed on a
# developer's machine and failed inside the built image, where a locale is
# actually configured. The deployer caught it; this suite had not.
# Inside a request context, because the label goes through `t()`, which asks
# `request.args` which language to answer in. Outside one that raises -- and
# only where a catalogue is actually loaded, since `t()` returns the key
# without touching `request` when there is none. That is exactly the gap
# between a developer's machine and the built image, and it is why this check
# passed here and failed there.
with A.app.test_request_context("/chat/teacher"):
    _want = A._space_mode_label("teacher", "assistant")
check("the mode has a label at all", bool(_want), _want)
check("the label is in the HTML, not fetched afterwards",
      _want in html.split("</header>")[0], _want)
check("it sits inside the title", 'id="space-mode"' in html
      and html.index('id="space-mode"') < html.index("</h1>"))
check("and it is not hidden when there is one",
      '<span id="space-mode" hidden>' not in html)

A._persona_mode_set(TEACHER, "teacher", "tutor")
html = client.get("/chat/teacher").get_data(as_text=True)
head = html.split("</header>")[0]
check("switching mode changes what the header says",
      "Tutor" in head and "Teaching assistant" not in head, head[:0])

other = client.get("/chat/programmer").get_data(as_text=True)
check("a profession with no modes shows no qualifier",
      '<span id="space-mode" hidden>' in other, other[:0])

with client.session_transaction() as s:
    s["user"] = KID
kid_head = client.get("/chat/teacher").get_data(as_text=True).split("</header>")[0]
check("and it is each person's own", "Tutor" in kid_head, kid_head[:0])
with client.session_transaction() as s:
    s["user"] = TEACHER
A._persona_mode_set(TEACHER, "teacher", "tutor")


# --- Editing a persona is picked up -------------------------------------------
print("\nthe cache follows both files")
mode_file = os.path.join("personas", "teacher.assistant.md")
before = A._persona_default("teacher", "assistant")
with open(mode_file, "a", encoding="utf-8") as f:
    f.write("\nUna regla nueva del modo.\n")
os.utime(mode_file, (0, 0))     # a different mtime, whichever direction
check("a change to the mode file is seen",
      "Una regla nueva del modo." in A._persona_default("teacher", "assistant"))
base_file = os.path.join("personas", "teacher.md")
with open(base_file, "a", encoding="utf-8") as f:
    f.write("\nUna regla nueva compartida.\n")
os.utime(base_file, (0, 0))
check("and so is a change to the shared one — in both modes",
      "Una regla nueva compartida." in A._persona_default("teacher", "assistant")
      and "Una regla nueva compartida." in A._persona_default("teacher", "tutor"),
      before[:0])


# --- The other two rewrites ---------------------------------------------------

print("\nprogrammer knows what a review and a design are")
dev = A._persona_default("programmer")
check("reviewing code is described", "Reviewing code" in dev
      and "severity" in dev and "defect" in dev)
check("so is proposing a design", "Proposing a design" in dev and "trade-offs" in dev)
check("the house is still there for when the topic is the house",
      "config/home-stack.yml" in dev and "A push is not a deploy" in dev)
check("but scoped, so it stays out of unrelated answers",
      "None of this applies when the question is about other code" in dev)
check("network security survived", all(
    w in dev.lower() for w in ("firewall", "segmentation", "hardening")))

print()
shutil.rmtree(tmp, ignore_errors=True)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")

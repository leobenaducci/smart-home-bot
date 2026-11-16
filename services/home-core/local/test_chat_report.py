"""A person can report a conversation that went wrong, and a copy is kept.

Run: python local/test_chat_report.py   (needs Flask; skips loudly without it)

The 👍/👎 bar rates one answer. Some problems are not one answer: a reply that
never came, a greeting that said "I couldn't finish it here", a run of turns
that went sideways. "Report problem" keeps the whole conversation that is open,
with whatever the person cares to say about it, for whoever investigates.

Pinned here: the server copies the conversation from its own history (the page
only names it), a note is optional, the copy does not change when the history
does, and only an admin can read the reports back.
"""
import json
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

tmp = tempfile.mkdtemp(prefix="homecore-report-")
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

A.init_problem_reports_db()
A.app.config["TESTING"] = True
client = A.app.test_client()
with client.session_transaction() as s:
    s["user"] = USER
    s["csrf_token"] = "tok"
H = {"X-CSRF-Token": "tok"}

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


DAY = "2026-09-29"
T0 = 1790700000000
T1 = T0 + 3 * 60 * 60 * 1000          # a second conversation, three hours later
for role, text, ts, conv in (("user", "prendé la luz de Oficina Tomi", T0, T0),
                             ("bot", "Listo, prendí Oficina Tomi.", T0 + 20_000, T0),
                             ("user", "¿qué tareas tiene Mora hoy?", T1, T1),
                             ("bot", "No pude terminarlo acá, así que sigo en segundo plano.", T1 + 9_000, T1)):
    A.append_user_history(USER, {"role": role, "text": text, "ts": ts, "conv": conv}, DAY)


def rows():
    import sqlite3
    c = sqlite3.connect(A.PROBLEM_REPORTS_DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT * FROM problem_reports ORDER BY id")]
    finally:
        c.close()


print("a report keeps the open conversation")
r = client.post("/chat/report", headers=H, json={"date": DAY, "conv": T1, "note": "  nunca respondió  "})
check("is accepted", r.status_code == 200 and r.get_json().get("ok"), r.get_json())
got = rows()
check("one row", len(got) == 1, got)
row = got[0] if got else {}
msgs = json.loads(row.get("messages") or "[]")
check("holds that conversation, not the other one",
      [m["text"] for m in msgs] == ["¿qué tareas tiene Mora hoy?",
                                    "No pude terminarlo acá, así que sigo en segundo plano."], msgs)
check("records who, which day and which conversation",
      (row.get("username"), row.get("day"), row.get("conv"), row.get("message_count")) == (USER, DAY, T1, 2), row)
check("keeps the note, trimmed", row.get("note") == "nunca respondió", row.get("note"))
check("starts open", row.get("status") == "open", row.get("status"))

print("\nthe note is optional")
r = client.post("/chat/report", headers=H, json={"date": DAY, "conv": T0})
check("a report without one is accepted", r.status_code == 200 and rows()[-1]["note"] == "", r.get_json())

print("\nthe page cannot supply the conversation")
r = client.post("/chat/report", headers=H, json={"date": DAY, "conv": T0,
                                                 "messages": [{"role": "bot", "text": "inventado"}]})
check("messages in the body are ignored",
      "inventado" not in rows()[-1]["messages"], rows()[-1]["messages"])

print("\nthe copy is a copy")
A.save_user_history(USER, [], DAY)
check("emptying the history leaves the report", json.loads(rows()[0]["messages"]), rows()[0])

print("\nnothing to report is refused")
r = client.post("/chat/report", headers=H, json={"date": "2026-09-01"})
check("an empty day without a note is a 400", r.status_code == 400, r.status_code)

print("\nreading reports back is for admins")
A._tasks_is_admin = lambda u: False
check("a member gets 403", client.get("/chat/reports").status_code == 403)
A._tasks_is_admin = lambda u: True
lst = client.get("/chat/reports").get_json()
check("an admin gets the list, newest first", [x["id"] for x in lst] == sorted([x["id"] for x in lst], reverse=True)
      and "messages" not in lst[0], lst)
one = client.get(f"/chat/reports?id={lst[-1]['id']}").get_json()
check("and one report with its messages", isinstance(one.get("messages"), list) and one["messages"], one)

print("\nthe page draws it")
page = open(os.path.join(SRC, "templates", "chat.html"), encoding="utf-8").read()
check("a header button", 'id="report-btn"' in page)
check("it names the open conversation", "conv: convStart()" in page and "'/chat/report'" in page)
cat = next(p for p in (os.path.join(SRC, "i18n", "en.json"),
                       os.path.join(SRC, "..", "..", "..", "i18n", "en.json"))
           if os.path.exists(p))
en = json.load(open(cat, encoding="utf-8"))
for k in ("button", "hint", "title", "explain", "placeholder", "cancel", "send", "sending", "thanks", "failed"):
    check(f"chat.report.{k} has English", bool(en.get(f"chat.report.{k}")))

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)

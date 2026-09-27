"""The family chat: who can reach whom, and that a message is never lost.

Run: python local/test_family_chat.py   (needs Flask; skips loudly without it)

Mostly for emergencies, so what is pinned here is delivery:

- an alert goes to every recipient and never to the sender;
- "delivered" (the phone has it) and "seen" (somebody opened it) are separate,
  and seen stops the alert on every one of that person's phones;
- a snooze is five minutes and then it alerts again -- it cannot make a
  message go away;
- a push no phone confirmed is pushed again, and after three minutes the
  *sender's* phone is asked to send it as an SMS, to recipients with a number;
- nobody can read or post in a conversation they are not in.

People are the invented household: Tomi and Mora (parents), Juana.
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

tmp = tempfile.mkdtemp(prefix="homecore-familychat-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
# The translation catalogue, where the image has it (the deployer stages it
# as an asset). Without it t() takes a shortcut, and the worker bug this file
# exists for -- translating outside a request -- only ever failed in the image.
_repo_i18n = os.path.join(SRC, "..", "..", "..", "i18n")
if not os.path.isdir(os.path.join(dst, "i18n")) and os.path.isdir(_repo_i18n):
    shutil.copytree(_repo_i18n, os.path.join(dst, "i18n"))
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32, DEBUG_API_KEY="d" * 32,
                  HOMECORE_MEMBERS="user1,user2,user3", HOMECORE_ADMIN_MEMBERS="user1,user2",
                  FAMILY_FILE=os.path.join(dst, "family.json"))

TOMI, MORA, JUANA = "999000111", "999000222", "999000333"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    json.dump([{"username": TOMI, "member": "user1", "nanobot_id": 1},
               {"username": MORA, "member": "user2", "nanobot_id": 2},
               {"username": JUANA, "member": "user3", "nanobot_id": 3}], f)
with open(os.path.join(dst, "family.json"), "w", encoding="utf-8") as f:
    json.dump({"people": [
        {"person": "tomi", "member": "user1", "display_name": "Tomi", "parents": True,
         "phone": "+15550100", "active": True},
        {"person": "mora", "member": "user2", "display_name": "Mora", "parents": True,
         "phone": "", "active": True},
        {"person": "juana", "member": "user3", "display_name": "Juana", "parents": False,
         "phone": "+15550103", "active": True}],
        "groups": [{"id": "family", "name": "Family", "preset": True,
                    "members": ["user1", "user2", "user3"]},
                   {"id": "parents", "name": "Parents", "preset": True,
                    "members": ["user1", "user2"]}]}, f)

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_family_chat_db()
A.app.config["TESTING"] = True
pushes = []
A._geo_push_control = lambda user, tag, extra=None: pushes.append((user, tag, extra or {}))
failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + repr(detail)}")
    if not cond:
        failures.append(label)


class as_:
    """A request as the app sends it: through the chat proxy, which vouches
    for the login server to server (and is why there is no CSRF token)."""

    def __init__(self, user):
        self.c, self.h = A.app.test_client(), {"X-Proxy-Secret": "p" * 32, "X-Proxy-User": user}

    def get(self, url):
        return self.c.get(url, headers=self.h)

    def post(self, url, json=None):
        return self.c.post(url, json=json, headers=self.h)


def tags(user=None):
    return [(u, t) for u, t, _e in pushes if user is None or u == user]


print("who is in which conversation")
d = as_(JUANA).get("/family-chat/api/directory").get_json()
check("a child sees Family but not Parents", [g["id"] for g in d["groups"]] == ["family"], d["groups"])
check("and the directory carries the numbers the SMS fallback dials",
      {p["login"]: p["phone"] for p in d["people"]}[TOMI] == "+15550100")
r = as_(JUANA).post("/family-chat/api/send", json={"thread": "g:parents", "text": "hola"})
check("she cannot post in Parents", r.status_code == 404, r.status_code)
r = as_(JUANA).get("/family-chat/api/messages?thread=g:parents")
check("or read it", r.status_code == 404, r.status_code)
r = as_(JUANA).post("/family-chat/api/send", json={"thread": "dm:999000111:999000222", "text": "x"})
check("or post in two other people's conversation", r.status_code == 404, r.status_code)

print("\nsending: everyone else is alerted, the sender is not")
pushes.clear()
r = as_(TOMI).post("/family-chat/api/send", json={"thread": "g:family", "text": "¿Dónde están?",
                                                  "urgent": True, "client_id": "c-1"}).get_json()
msg_id = r["id"]
check("two recipients", r["recipients"] == 2, r)
check("each gets a family_msg push", sorted(tags()) == sorted([(MORA, "family_msg"), (JUANA, "family_msg")]), tags())
check("the push says it is urgent and who sent it",
      all(e["urgent"] and e["from_name"] == "Tomi" for _u, _t, e in pushes), pushes)
check("and names the group in the recipient's language", pushes[0][2]["thread_name"] in ("Family", "Familia"))
r = as_(TOMI).post("/family-chat/api/send", json={"thread": "g:family", "text": "¿Dónde están?",
                                                  "client_id": "c-1"}).get_json()
check("the same client_id twice is one message (SMS first, synced later)",
      r.get("duplicate") and r["id"] == msg_id, r)

print("\ndelivered, then seen")
as_(MORA).post("/family-chat/api/delivered", json={"ids": [msg_id]})
m = as_(TOMI).get("/family-chat/api/messages?thread=g:family").get_json()["messages"][-1]
rc = {x["login"]: x for x in m["receipts"]}
check("the sender sees who has it", rc[MORA]["delivered"] and not rc[JUANA]["delivered"], rc)
check("and that nobody has seen it yet", not rc[MORA]["seen"], rc)
pushes.clear()
as_(MORA).post("/family-chat/api/seen", json={"thread": "g:family"})
check("seen stops the alert on her phones", tags() == [(MORA, "family_stop")], tags())
t = as_(MORA).get("/family-chat/api/threads").get_json()["threads"]
check("and it is no longer unread", {x["thread"]: x["unread"] for x in t}["g:family"] == 0, t)

print("\nsnooze is five minutes, then it alerts again")
as_(JUANA).post("/family-chat/api/delivered", json={"ids": [msg_id]})
pushes.clear()
r = as_(JUANA).post("/family-chat/api/snooze", json={"thread": "g:family"}).get_json()
check("snoozing stops it now", tags() == [(JUANA, "family_stop")], tags())
until = r["until"]
pushes.clear()
A._fc_tick(now=until - 10)
check("nothing during the snooze", tags(JUANA) == [], tags())
A._fc_tick(now=until + 1)
check("and it alerts again when the snooze ends", tags(JUANA) == [(JUANA, "family_msg")], tags())
pushes.clear()
A._fc_tick(now=until + 20)
check("once, not on every pass", tags(JUANA) == [], tags())

print("\nan undelivered message: pushed again, then texted by the sender's phone")
pushes.clear()
r = as_(MORA).post("/family-chat/api/send", json={"thread": "g:family", "text": "Llamame"}).get_json()
m2 = r["id"]
ts = A._fc_conn().execute("SELECT ts FROM fc_messages WHERE id=?", (m2,)).fetchone()[0]
as_(TOMI).post("/family-chat/api/delivered", json={"ids": [m2]})     # Tomi's phone has it; Juana's does not
pushes.clear()
A._fc_tick(now=ts + 30)
check("not pushed again before a minute", tags(JUANA) == [], tags())
A._fc_tick(now=ts + 65)
check("pushed again after a minute", (JUANA, "family_msg") in tags(), tags())
check("but not to the one whose phone confirmed it", (TOMI, "family_msg") not in tags(), tags())
pushes.clear()
A._fc_tick(now=ts + 3 * 60 + 1)
sms = [e for u, tg, e in pushes if tg == "family_sms"]
check("after three minutes the SENDER's phone is asked to text it",
      [(u, tg) for u, tg, _e in pushes if tg == "family_sms"] == [(MORA, "family_sms")], pushes)
check("to the recipient that never got it, with her number",
      sms and sms[0]["to"] == [{"login": JUANA, "name": "Juana", "phone": "+15550103"}], sms)
pushes.clear()
A._fc_tick(now=ts + 4 * 60)
check("and only once", not [1 for _u, tg, _e in pushes if tg == "family_sms"], pushes)

print("\nsomeone with no number gets no SMS request (there is nothing to dial)")
pushes.clear()
r = as_(JUANA).post("/family-chat/api/send", json={"thread": A._fc_dm(JUANA, MORA), "text": "hola"}).get_json()
ts3 = A._fc_conn().execute("SELECT ts FROM fc_messages WHERE id=?", (r["id"],)).fetchone()[0]
pushes.clear()
A._fc_tick(now=ts3 + 3 * 60 + 5)
check("Mora has no phone on file: no family_sms", not [1 for _u, tg, _e in pushes if tg == "family_sms"], pushes)

print("\na message the sender already texted (no data) is not texted again")
pushes.clear()
r = as_(TOMI).post("/family-chat/api/send", json={"thread": "g:family", "text": "sin datos",
                                                  "client_id": "a-offline-1", "sms_sent": True}).get_json()
check("the push carries the sender's id, so an SMS copy is the same message",
      all(e.get("client_id") == "a-offline-1" for _u, t_, e in pushes if t_ == "family_msg") and pushes, pushes)
ts4 = A._fc_conn().execute("SELECT ts FROM fc_messages WHERE id=?", (r["id"],)).fetchone()[0]
pushes.clear()
A._fc_tick(now=ts4 + 3 * 60 + 5)
check("and no SMS hand-off for it", not [1 for _u, tg, _e in pushes if tg == "family_sms"], pushes)

print("\nthe worker runs outside any request, and names groups in each person's language")
A._MEMBER_LOCALES.update({TOMI: "en", JUANA: "es"})
try:
    A._fc_tick()
    check("a worker pass does not need a request", True)
except RuntimeError as exc:
    check("a worker pass does not need a request", False, exc)
check("Family for Tomi, Familia for Juana",
      (A._fc_thread_name("g:family", TOMI), A._fc_thread_name("g:family", JUANA)) == ("Family", "Familia"),
      (A._fc_thread_name("g:family", TOMI), A._fc_thread_name("g:family", JUANA)))

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)

"""Mailboxes, the rules on them, and the file nanobot is handed.

Run: python local/test_email_rules_registry.py   (needs Flask + cryptography)

This side owns the words and nanobot owns the refusal, so what these pin down
is the contract between them: that a password never comes back out of the API,
that the export is the shape the channel reads, and that the two states which
must never arrive by accident cannot.

Those two are worth naming, because both are silent when wrong:

- **`rulesEnforced` is only true once a rule exists.** Enforcement with an
  empty rule list is a mailbox Alfred reads nothing from — correct, and not
  what an admin who filled in a mailbox and has not reached the rules yet
  asked for.
- **An instance can only fetch its own.** The export takes no path segment:
  it answers for whoever the proxy header resolved to, so a container cannot
  ask for another member's mailbox password by changing a URL.
"""
import json
import os
import shutil
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
    from cryptography.fernet import Fernet
except ImportError as exc:
    print(f"SKIP: {exc} — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homeweb-email-rules-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)

PROXY = "p" * 40
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET=PROXY,
                  DEBUG_API_KEY="d" * 32,
                  PROJECTS_KEY=Fernet.generate_key().decode(),
                  # Who lives here and who is a parent. Both used to be literals in app.py --
                  # `ADVANCED_USERS = {'user1', 'user2'}` -- and are now supplied by the
                  # deployer, so a suite that does not say leaves the house with no adults in
                  # it and every admin route below answers 403.
                  HOMECORE_MEMBERS="user1,user2,user3",
                  HOMECORE_ADMIN_MEMBERS="user1,user2")

MEMBER_A, MEMBER_B, MEMBER_C = "user1", "user2", "user3"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    json.dump([{"username": MEMBER_A, "nanobot_id": 2},
               {"username": MEMBER_B, "nanobot_id": 3},
               {"username": MEMBER_C, "nanobot_id": 1}], f)

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_projects_db()
A.init_profiles_db()
A.app.config["TESTING"] = True
client = A.app.test_client()

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label}" + (f"  <- {detail}" if detail else ""))


CSRF = "x" * 16
H = {"X-CSRF-Token": CSRF}


def as_user(user):
    with client.session_transaction() as sess:
        sess["user"] = user
        sess["csrf_token"] = CSRF


def call(path, method="GET", body=None, user=MEMBER_A):
    as_user(user)
    fn = getattr(client, method.lower())
    return fn(path, json=body, headers=H) if body is not None else fn(path, headers=H)


MAILBOX = {
    "address": "alfred@example.com", "label": "Alfred", "is_alfred": True,
    # `.invalid` is reserved and never resolves, so the save's connection test
    # fails immediately and locally. A real hostname here -- this said
    # `imap.fastmail.com` -- makes every run of this file open an outbound
    # connection to somebody else's IMAP server and wait on it: measured at 24
    # seconds wall for 0.9s of CPU, and dependent on their uptime. This file is
    # about rules; the probe has its own checks below with no socket at all.
    "imap_host": "imap.example.invalid", "imap_username": "alex@example.com",
    "imap_secret": "clave-imap",
    "smtp_host": "smtp.example.invalid", "smtp_username": "alex@example.com",
    "smtp_secret": "clave-smtp",
}


def profile(instance="user2"):
    for p in call("/profiles/api").get_json()["profiles"]:
        if p["instance"] == instance:
            return p
    return None


print("a profile exists for every instance that does")
check("seeded from users.json",
      {p["instance"] for p in call("/profiles/api").get_json()["profiles"]}
      == {"user1", "user2", "user3"},
      [p["instance"] for p in call("/profiles/api").get_json()["profiles"]])

print("\nand only an admin touches any of it")
check("the list is refused", call("/profiles/api", user=MEMBER_C).status_code == 403)
check("adding a mailbox is refused",
      call("/profiles/api/user2/accounts", "POST", MAILBOX, user=MEMBER_C).status_code == 403)

print("\na mailbox goes in, and the password does not come back out")
r = call("/profiles/api/user2/accounts", "POST", MAILBOX)
check("added", r.status_code == 201, r.get_json())
account_id = r.get_json()["account_id"]
acc = [a for a in profile()["accounts"] if a["id"] == account_id][0]
check("no secret in the response",
      "imap_secret" not in acc and "smtp_secret" not in acc, list(acc))
check("but the page can tell there is one",
      acc["has_imap_secret"] and acc["has_smtp_secret"], acc)
check("the same address twice is a conflict",
      call("/profiles/api/user2/accounts", "POST", MAILBOX).status_code == 409)
check("something that is not an address is refused",
      call("/profiles/api/user2/accounts", "POST",
           {**MAILBOX, "address": "no-arroba"}).status_code == 400)

print("\nan edit that does not mention the password keeps it")
# The page never receives it, so a blank field means «I did not change it».
r = call(f"/profiles/api/accounts/{account_id}", "PUT", {"label": "Alfred (trabajo)"})
check("the edit lands", r.status_code == 200, r.get_json())
acc = [a for a in profile()["accounts"] if a["id"] == account_id][0]
check("and the password survived", acc["has_imap_secret"], acc)
check("while the label changed", acc["label"] == "Alfred (trabajo)", acc)

print("\nleast privilege, as the page reports it")
call(f"/profiles/api/accounts/{account_id}/rules", "POST",
     {"match_kind": "from", "pattern": "*@empresa.com", "can_read": True})
call(f"/profiles/api/accounts/{account_id}/rules", "POST",
     {"match_kind": "from", "pattern": "jefe@empresa.com", "can_read": True,
      "can_respond": True, "instructions": "Con el jefe, formal y breve."})
eff = [a for a in profile()["accounts"] if a["id"] == account_id][0]["effective_if_all_match"]
check("read survives both rules", eff["read"] is True, eff)
check("respond does not, because the broader rule withholds it",
      eff["respond"] is False, eff)
check("and the instructions come along", eff["instructions"], eff)
check("an invented match kind is refused",
      call(f"/profiles/api/accounts/{account_id}/rules", "POST",
           {"match_kind": "telepatía"}).status_code == 400)

print("\nthe file nanobot is handed")
export = call("/profiles/api/export").get_json()
email = export["channels"]["email"]
check("it is the channel's own shape, camelCase",
      "imapHost" in email and "alfredAddresses" in email, list(email))
check("the login is the real account", email["imapUsername"] == "alex@example.com")
check("and the address he answers to is the alias",
      email["fromAddress"] == "alfred@example.com"
      and email["alfredAddresses"] == ["alfred@example.com"], email.get("alfredAddresses"))
check("the password is in there, decrypted, because that is what it is for",
      email["imapPassword"] == "clave-imap")
check("both rules travel", len(email["rules"]) == 2, email["rules"])
check("enforcement is on, because a rule exists", email["rulesEnforced"] is True)

print("\nand the states that must never arrive by accident")
r = call("/profiles/api/export", user=MEMBER_B)
check("another member gets their own export, not this one",
      r.get_json().get("channels", {}) == {}, r.get_json())
# user3 has no mailbox, so there is nothing to enforce and nothing to read.
call("/profiles/api/user3/accounts", "POST",
     {**MAILBOX, "address": "sam-alfred@example.com"}, user=MEMBER_A)
export3 = call("/profiles/api/export", user=MEMBER_B).get_json()
check("a mailbox with no rules does not turn enforcement on",
      export3["channels"]["email"]["rulesEnforced"] is False,
      export3["channels"]["email"].get("rulesEnforced"))
check("which is the difference between «no rules yet» and «read nothing»",
      export3["channels"]["email"]["rules"] == [])

print("\na disabled profile exports nothing rather than failing")
call("/profiles/api/user2", "PUT", {"display_name": "Alex", "enabled": False})
check("an empty overlay, so the entrypoint merges nothing",
      call("/profiles/api/export").get_json() == {"channels": {}},
      call("/profiles/api/export").get_json())
call("/profiles/api/user2", "PUT", {"display_name": "Alex", "enabled": True})

print("\nan address of Alfred's own needs no mailbox behind it")
# The ordinary case, not an incomplete one: his address is usually a name on
# somebody else's account. The export reads the mailbox from the first row with
# a host and the addresses he answers to from every row flagged his.
r = call("/profiles/api/user2/accounts", "POST",
         {"address": "alfred-alias@example.com", "is_alfred": True})
check("an alias is accepted with nothing else filled in", r.status_code == 201, r.get_json())
r = call("/profiles/api/user2/accounts", "POST", {"address": "huerfana@example.com"})
check("but a row that is neither a mailbox nor his is refused",
      r.status_code == 400, r.get_json())
check("and the refusal says which of the two it wanted",
      "alias" in r.get_json().get("error", ""), r.get_json())
export = call("/profiles/api/export").get_json()["channels"]["email"]
check("the alias reaches alfredAddresses",
      "alfred-alias@example.com" in export["alfredAddresses"], export["alfredAddresses"])
check("without becoming the mailbox he logs into",
      export["imapUsername"] == "alex@example.com", export["imapUsername"])

print("\nan address can be corrected without losing the mailbox")
# Somebody mistypes one, or a domain moves. A mailbox you can only delete and
# rebuild takes its rules with it.
r = call(f"/profiles/api/accounts/{account_id}", "PUT",
         {"address": "alfred-nuevo@example.com"})
check("the rename lands", r.status_code == 200, r.get_json())
acc = [a for a in profile()["accounts"] if a["id"] == account_id][0]
check("the new address is there", acc["address"] == "alfred-nuevo@example.com", acc["address"])
check("the password came along", acc["has_imap_secret"], acc)
check("and so did its rules", len(acc["rules"]) >= 1, acc["rules"])
r = call(f"/profiles/api/accounts/{account_id}", "PUT",
         {"address": "alfred-alias@example.com"})
check("renaming onto one that is taken is a conflict", r.status_code == 409, r.get_json())
r = call(f"/profiles/api/accounts/{account_id}", "PUT", {"address": "sin-arroba"})
check("and something that is not an address is refused", r.status_code == 400)

print("\nthe page is shown the config, not the passwords")
# «Is the password right» is answered by «Probar conexión», not by reading it.
# Redacted on the server: a value the page hides is still a value it was sent.
shown = call("/profiles/api/export?redact=1").get_json()["channels"]["email"]
check("the rail gets a stand-in", "clave-imap" not in json.dumps(shown), shown.get("imapPassword"))
check("that says a password exists", "guardada" in shown["imapPassword"], shown["imapPassword"])
check("everything else is the real thing",
      shown["imapHost"] == export["imapHost"] and shown["rules"] == export["rules"])
check("and the file nanobot fetches is untouched",
      call("/profiles/api/export").get_json()["channels"]["email"]["imapPassword"] == "clave-imap")

print("\nreads everything to one address, answers only when asked, confirms first")
# The three Alex asked for, which are three different kinds of thing: a plain
# permission, a graded one, and a condition on the text rather than on the act.
r = call(f"/profiles/api/accounts/{account_id}/rules", "POST",
         {"match_kind": "to", "pattern": "alfred@example.com",
          "can_read": True, "respond_mode": "on_request", "confirm": True})
check("the rule is accepted", r.status_code == 201, r.get_json())
made = [a for a in profile()["accounts"] if a["id"] == account_id][0]["rules"][-1]
check("it reads everything sent to that address",
      made["match_kind"] == "to" and made["can_read"], made)
check("it answers only when somebody asks",
      made["respond_mode"] == "on_request", made)
check("and nothing leaves without approval", made["confirm"] is True, made)
check("an invented mode is refused rather than stored",
      call(f"/profiles/api/accounts/{account_id}/rules", "POST",
           {"respond_mode": "telepatía"}).status_code == 400)

print("\nand nanobot is handed all three")
sent = [x for x in call("/profiles/api/export").get_json()["channels"]["email"]["rules"]
        if x["pattern"] == "alfred@example.com"][0]
check("the mode travels", sent["respondMode"] == "on_request", sent)
check("the approval requirement travels", sent["confirm"] is True, sent)
check("and `respond` stays true for a reader that only knows the old field",
      sent["respond"] is True, sent)

print("\na rule stored before modes existed keeps meaning what it meant")
# `can_respond` meant «may answer, including on his own». Reading it as the
# safer setting would be a change nobody made.
conn = A._profiles_conn()
conn.execute("INSERT INTO email_rules (account_id, match_kind, pattern, can_read, "
             "can_respond, respond_mode) VALUES (?,?,?,?,?,'')",
             (account_id, "any", "*", 1, 1))
conn.commit()
conn.close()
old = [a for a in profile()["accounts"] if a["id"] == account_id][0]["rules"][-1]
check("it reads as «always», not as the safer one",
      old["respond_mode"] == "always", old)

print("\nremoving things")
# By id, not by position: accounts come back with Alfred's own addresses first,
# so «the first one» stopped being «the one with the mailbox» the moment an
# alias existed.
mailbox_row = [a for a in profile()["accounts"] if a["id"] == account_id][0]
rule_id = mailbox_row["rules"][0]["id"]
check("a rule goes",
      call(f"/profiles/api/rules/{rule_id}", "DELETE").status_code == 200)
check("removing it twice is a 404",
      call(f"/profiles/api/rules/{rule_id}", "DELETE").status_code == 404)
check("and a mailbox takes its rules with it",
      call(f"/profiles/api/accounts/{account_id}", "DELETE").status_code == 200)
check("that account is gone",
      not [a for a in profile()["accounts"] if a["id"] == account_id],
      [a["address"] for a in profile()["accounts"]])
conn = A._profiles_conn()
left = conn.execute("SELECT COUNT(*) FROM email_rules WHERE account_id=?",
                    (account_id,)).fetchone()[0]
conn.close()
check("with no rules orphaned behind it", left == 0, left)

print("\na mailbox is tried when it is saved, not when Alfred needs it")
# You could save a mailbox with a wrong host or a stale app-password and find
# out weeks later, from Alfred quietly reading nothing. The save reports what
# happened instead.
r = call("/profiles/api/user1/accounts", "POST",
         {"address": "prueba@example.com", "is_alfred": True,
          "imap_host": "imap.no-existe.invalid", "imap_username": "user1",
          "imap_secret": "clave", "smtp_host": "smtp.no-existe.invalid",
          "smtp_username": "user1", "smtp_secret": "clave"})
check("it saves even so", r.status_code == 201, r.get_json())
probe = r.get_json()["test"]
check("and says the mailbox does not answer", probe["imap"][0] is False, probe)
check("in the server's own words", len(probe["imap"][1]) > 12, probe["imap"])
tested_id = r.get_json()["account_id"]

# An alias has nothing to connect to, so its check is neither a pass nor a
# failure — and saying so is different from a red cross against somebody who
# has filled in half the form.
r = call("/profiles/api/user1/accounts", "POST",
         {"address": "vacio@example.com", "is_alfred": True})
check("an alias saves", r.status_code == 201, r.get_json())
check("and its check is neither pass nor fail",
      r.get_json()["test"]["imap"][0] is None, r.get_json()["test"])

print("\nand not tried again when nothing about it changed")
# Renaming a label should not cost two network round trips, and an admin
# editing four mailboxes should not wait on eight of them.
r = call(f"/profiles/api/accounts/{tested_id}", "PUT", {"label": "Otro nombre"})
check("a label edit does not reach for the network",
      r.get_json()["test"] is None, r.get_json().get("test"))
r = call(f"/profiles/api/accounts/{tested_id}", "PUT", {"imap_host": "otro.invalid"})
check("changing the host does", r.get_json()["test"] is not None)

print("\nand can be asked again, because the answer goes stale")
r = call(f"/profiles/api/accounts/{tested_id}/test", "POST", {})
check("on demand", r.status_code == 200 and "test" in r.get_json(), r.get_json())
check("a mailbox that is not there is a 404",
      call("/profiles/api/accounts/999999/test", "POST", {}).status_code == 404)
check("and a non-admin cannot use it to reach arbitrary hosts",
      call(f"/profiles/api/accounts/{tested_id}/test", "POST", {}, user=MEMBER_C).status_code == 403)

print("\nthe password never comes back in the error")
# smtplib puts the whole failed command in SMTPAuthenticationError, base64 and
# all, and this text goes to a page and into the log.
import base64 as _b64, smtplib as _smtp
_pw = "clave-super-secreta"
_exc = _smtp.SMTPAuthenticationError(535, (
    "5.7.8 auth failed for AUTH PLAIN "
    + _b64.b64encode(b"\0" + _pw.encode()).decode()
    + " pass=" + _pw).encode())
_out = A._email_test_reason(_exc, _pw)
check("not in plaintext", _pw not in _out, _out)
check("nor base64 encoded",
      _b64.b64encode(b"\0" + _pw.encode()).decode() not in _out, _out)
check("and what is left still says what went wrong", "535" in _out, _out)

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all good")

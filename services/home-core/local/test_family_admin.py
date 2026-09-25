"""The family directory, as an admin edits it — and what nanobot ends up reading.

Run: python local/test_family_admin.py   (needs Flask; skips loudly without it)

A profile row is not a preference: `FAMILY.md` is loaded by every Alfred in the
house, so editing somebody's profile edits what five assistants believe about
them. That is why the page is admin-only and why most of this file is about the
refusals rather than the happy path.

Two rules worth stating out loud, because both are the kind that rot quietly:

- **A relationship is stored once and read from both ends.** «Alex es padre de
  Robin» and «Robin es hija de Alex» are one row, not two. Two rows can be edited
  apart, deleted apart, and end up saying different things about the same pair
  — and a directory that contradicts itself is worse than one that is thin.
- **The reverse side is rendered, so it cannot be deleted from the wrong end.**
  Robin's card shows «hija de Alex» with no way to remove it there; the edge
  belongs to the card that stored it.
"""
import json
import os
import shutil
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError as exc:
    print(f"SKIP: {exc} — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homeweb-family-admin-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32,
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

A.init_family_db()
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


def admin(path, method="GET", body=None, user=MEMBER_A):
    as_user(user)
    fn = getattr(client, method.lower())
    return fn(path, json=body, headers=H) if body is not None else fn(path, headers=H)


def profile(person):
    for p in admin("/family/api/admin").get_json()["people"]:
        if p["person"] == person:
            return p
    return None


print("the directory is an admin's, not a member's")
# Every Alfred loads this file. A member editing their own row is what the
# `family` skill is for; this page rewrites the whole house's memory.
as_user(MEMBER_C)
check("a non-admin is sent away from the page",
      client.get("/profiles").status_code == 302,
      client.get("/profiles").status_code)
check("and refused the API", admin("/family/api/admin", user=MEMBER_C).status_code == 403)
check("and cannot write a profile",
      admin("/family/api/admin/user1", "PUT", {"display_name": "X"}, user=MEMBER_C).status_code == 403)
check("nor a relationship",
      admin("/family/api/admin/user1/relations", "POST",
            {"kind": "parent", "other": "user3"}, user=MEMBER_C).status_code == 403)
as_user(MEMBER_A)
check("an admin gets the page", client.get("/profiles").status_code == 200)

print("\nthe whole profile saves in one write")
# `set-profile` takes one field at a time, which is right for a conversation
# and wrong for a form: six fields would be six writes, and three of them could
# land before one failed.
r = admin("/family/api/admin/user1", "PUT", {
    "display_name": "Alex", "full_name": "Alex Example",
    "relationship": "Father", "birthdate": "1984-03-22",
    "timezone": "Etc/UTC", "gender": "m"})
check("a full save is accepted", r.status_code == 200, r.get_json())
p = profile("user1")
check("and every field came back", p["full_name"] == "Alex Example"
      and p["birthdate"] == "1984-03-22" and p["gender"] == "m", p)
r = admin("/family/api/admin/user1", "PUT", {"display_name": "Alex", "gender": "sí"})
check("an invented gender is refused rather than stored", r.status_code == 400, r.get_json())
check("and the stored one survived it", profile("user1")["gender"] == "m")
r = admin("/family/api/admin/nadie", "PUT", {"display_name": "X"})
check("somebody who is not in the house is a 404", r.status_code == 404)

print("\na relationship is written once and read from both ends")
admin("/family/api/admin/user2", "PUT", {"display_name": "Sam", "gender": "f"})
admin("/family/api/admin/user3", "PUT", {"display_name": "Robin", "gender": "f"})
admin("/family/api/admin/noa", "PUT", {"display_name": "Noa", "gender": "f"})
check("adding one works",
      admin("/family/api/admin/user1/relations", "POST",
            {"kind": "parent", "other": "user3"}).status_code == 200)
check("the side that stored it says «padre de»",
      "padre de User3" in profile("user1")["parentesco"], profile("user1")["parentesco"])
check("and the other side says «hija de», with nobody having written that",
      "hija de User1" in profile("user3")["parentesco"], profile("user3")["parentesco"])
derived = [r for r in profile("user3")["relations"] if r["other"] == "user1"]
check("the reverse is marked as not stored here",
      derived and derived[0]["stored"] is False, derived)

print("\nand the wording follows the person, not the row")
admin("/family/api/admin/user2/relations", "POST", {"kind": "parent", "other": "user3"})
check("a mother is «madre de», not «padre de»",
      "madre de User3" in profile("user2")["parentesco"], profile("user2")["parentesco"])
admin("/family/api/admin/user4", "PUT", {"display_name": "Kai", "gender": ""})
admin("/family/api/admin/user4/relations", "POST", {"kind": "sibling", "other": "user3"})
check("nobody's gender is guessed — unset gets the neutral form",
      "hermane de" in profile("user4")["parentesco"], profile("user4")["parentesco"])

print("\nseveral of the same relationship read as one phrase")
admin("/family/api/admin/user1/relations", "POST", {"kind": "parent", "other": "noa"})
admin("/family/api/admin/user1/relations", "POST", {"kind": "parent", "other": "user4"})
check("«padre de Robin, Noa y Kai», not three bullets",
      "padre de" in profile("user1")["parentesco"]
      and profile("user1")["parentesco"].count("padre de") == 1
      and " y " in profile("user1")["parentesco"], profile("user1")["parentesco"])

print("\nthe refusals that keep the directory from contradicting itself")
r = admin("/family/api/admin/user1/relations", "POST", {"kind": "parent", "other": "alex"})
check("nobody is their own parent", r.status_code == 400, r.get_json())
r = admin("/family/api/admin/user1/relations", "POST", {"kind": "primo", "other": "user3"})
check("a parentesco that does not exist is refused", r.status_code == 400, r.get_json())
r = admin("/family/api/admin/user1/relations", "POST", {"kind": "parent", "other": "nadie"})
check("so is somebody who is not in the house", r.status_code == 404, r.get_json())
admin("/family/api/admin/user1/relations", "POST", {"kind": "spouse", "other": "user2"})
r = admin("/family/api/admin/user2/relations", "POST", {"kind": "spouse", "other": "alex"})
check("the same marriage entered from the other side is a conflict, not a second row",
      r.status_code == 409, r.get_json())

print("\nand removing one works from either end, because it is one row")
check("removed from the end that did not store it",
      admin("/family/api/admin/user3/relations", "DELETE",
            {"kind": "parent", "other": "alex"}).status_code == 200)
check("it is gone from both", "padre de Robin" not in profile("user1")["parentesco"]
      and "hija de User1" not in profile("user3")["parentesco"],
      (profile("user1")["parentesco"], profile("user3")["parentesco"]))
r = admin("/family/api/admin/user3/relations", "DELETE", {"kind": "parent", "other": "alex"})
check("removing it twice is a 404, not a second success", r.status_code == 404)

print("\nwhat nanobot actually reads")
# The page shows this file rather than a summary of it, so it has to be the
# same bytes the export serves.
md = admin("/family/api/admin/preview").get_json()["markdown"]
check("the preview is the real FAMILY.md", md == A._family_markdown())
check("and it carries the parentesco", "- Parentesco:" in md, md[:400])
check("a relationship reaches it in words",
      "madre de User3" in md, [l for l in md.splitlines() if "Parentesco" in l])
as_user(MEMBER_A)
check("the export route still serves the same thing",
      client.get("/family/api/markdown").get_data(as_text=True).strip() == md.strip())

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all good")

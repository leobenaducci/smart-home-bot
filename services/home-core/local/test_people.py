"""Who the portal thinks you are.

Run: python local/test_people.py   (needs Flask; skips loudly without it)

This app keys almost everything on the login id: which folder on the share is
yours, whether you may edit chores, whether /stats opens or bounces you back to
the wall. A login id is not a member id -- a household that already had
accounts goes on logging in with whatever it logged in with before -- and those
tables were five login ids written into app.py.

The consequence was silent and total. On the install this package was extracted
from, once its accounts were carried across: /files answered 403 to every
member, /stats and /credentials redirected the two adults back to the wall, and
nothing anywhere logged a word about it. Every page returned a valid response.

So this pins the join. The deploy says who the members are, which are admins
and what the house calls each one; users.json says which login belongs to which
member; this file says nothing. The cases below are the ones that were wrong:
a login that is not a member id, a folder that is not a member id, and an
account added after the process started.
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

tmp = tempfile.mkdtemp(prefix="homecore-people-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs", "i18n"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)

USERS = os.path.join(dst, "users.json")
# Deliberately none of these is a member id, and none of the folders is
# either: that is the whole shape the old tables could not express.
ACCOUNTS = [
    {"username": "10293847", "hash": "$2b$12$x", "nanobot_id": 1, "member": "user1"},
    {"username": "56473829", "hash": "$2b$12$x", "nanobot_id": 2, "member": "user2"},
    {"username": "11223344", "hash": "$2b$12$x", "nanobot_id": 3, "member": "user3"},
]


def write_users(accounts):
    with open(USERS, "w", encoding="utf-8") as fh:
        json.dump(accounts, fh)


write_users(ACCOUNTS)
os.environ.update(
    SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32, DEBUG_API_KEY="d" * 32,
    HOMECORE_MEMBERS="user1,user2,user3,user4",
    HOMECORE_ADMIN_MEMBERS="user1,user2",
    HOMECORE_MEMBER_FOLDERS="user1:ana,user2:bo,user3:cleo,user4:dani",
)

sys.path.insert(0, dst)
import app as A  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


print("a login id is joined to a member, and the member decides everything")
check("everybody who has an account has a folder",
      A.FILES_FOLDERS == {"10293847": "ana", "56473829": "bo", "11223344": "cleo"},
      A.FILES_FOLDERS)
check("the folder is what the house calls them, not their member id",
      "user1" not in A.FILES_FOLDERS.values(), A.FILES_FOLDERS)
check("the admins are the members the deploy called admins",
      A.ADVANCED_USERS == {"10293847", "56473829"}, A.ADVANCED_USERS)
check("and a child is not one", "11223344" not in A.ADVANCED_USERS)
# One object, two names, thirty call sites: rebinding either would leave half
# the app reading the other.
check("FILES_ADMINS and ADVANCED_USERS are the same set",
      A.FILES_ADMINS is A.ADVANCED_USERS)

print("\nthe share knows every member's room, not only the ones signed up")
check("a member with no account yet still has a folder on the share",
      "dani" in A.FILES_ALL_FOLDERS, A.FILES_ALL_FOLDERS)
check("the shared family folder is in there too",
      A.FAMILY_FOLDER in A.FILES_ALL_FOLDERS, A.FILES_ALL_FOLDERS)
check("and nothing is listed twice",
      len(A.FILES_ALL_FOLDERS) == len(set(A.FILES_ALL_FOLDERS)), A.FILES_ALL_FOLDERS)

print("\nwhat the pages actually do with it")
folder, is_admin = A._files_access("11223344")
check("a child gets their own folder", folder == "cleo", folder)
check("and is not an admin", is_admin is False)
folder, is_admin = A._files_access("10293847")
check("an adult gets theirs", folder == "ana", folder)
check("and may reach the whole share", is_admin is True)
check("somebody with no account gets nothing rather than everything",
      A._files_access("nobody") == (None, False), A._files_access("nobody"))
check("a child may not touch another child's folder",
      A._files_resolve("11223344", "ana/secrets") is None)
check("but may use their own", A._files_resolve("11223344", "cleo/homework"))
check("and the family folder", A._files_resolve("11223344", A.FAMILY_FOLDER))
check("an adult may reach anything", A._files_resolve("10293847", "cleo/homework"))
check("chore editing follows the same answer",
      A._tasks_is_admin("10293847") and not A._tasks_is_admin("11223344"))

print("\nan account added after this process started is picked up")
write_users(ACCOUNTS + [
    {"username": "99887766", "hash": "$2b$12$x", "nanobot_id": 4, "member": "user4"}])
check("find_user sees it", A.find_user("99887766") is not None)
check("and it has its folder", A.FILES_FOLDERS.get("99887766") == "dani",
      A.FILES_FOLDERS)
check("without becoming an admin", "99887766" not in A.ADVANCED_USERS)
# After a rebuild, not only before it: the tables are refilled in place, and a
# refresh that rebound the names instead would leave the files code holding the
# set this process started with -- which answers "not an admin" to everybody.
check("the two names are still one set after a rebuild",
      A.FILES_ADMINS is A.ADVANCED_USERS,
      "_refresh_people must clear/update, never rebind")

print("\nand a removed one stops having rights")
write_users([a for a in ACCOUNTS if a["username"] != "10293847"])
A.find_user("anyone")            # the read that refreshes
check("their login is no longer an admin", "10293847" not in A.ADVANCED_USERS,
      A.ADVANCED_USERS)
check("and no longer has a folder", "10293847" not in A.FILES_FOLDERS,
      A.FILES_FOLDERS)
write_users(ACCOUNTS)
A.find_user("anyone")

print("\na fresh install, where the login id is the member id")
# The fallback, and the reason it is not a guess: `install --create-user`
# makes the first account with the member id as its login, and such a record
# has no `member` field at all.
write_users([{"username": "user1", "hash": "$2b$12$x", "nanobot_id": 1}])
A.find_user("anyone")
check("the login stands in for the member", A.FILES_FOLDERS == {"user1": "ana"},
      A.FILES_FOLDERS)
check("and their rights come out right", A.ADVANCED_USERS == {"user1"},
      A.ADVANCED_USERS)

print("\nan install that says nothing gives nobody anybody else's things")
# HOMECORE_MEMBER_FOLDERS unset: the member id is the folder. What must not
# happen is two members sharing one, or somebody inheriting an admin's.
A.MEMBER_FOLDERS.clear()
A.MEMBER_ADMINS.clear()
write_users(ACCOUNTS)
A._refresh_people(force=True)
check("each folder is that member's own id",
      A.FILES_FOLDERS == {"10293847": "user1", "56473829": "user2",
                          "11223344": "user3"}, A.FILES_FOLDERS)
check("and nobody is an admin by default", A.ADVANCED_USERS == set(),
      A.ADVANCED_USERS)

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")

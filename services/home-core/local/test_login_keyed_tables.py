"""Which tables key on a login, and whether the migration actually moves them.

Run: python local/test_login_keyed_tables.py   (needs Flask; skips loudly without it)

`migrate_person_keys()` is the guard against what CLAUDE.md calls the most
expensive confusion in this codebase's history: three ids name a person and
anything a request reaches has to key on the login, because that is what the
session carries. Nothing tested it. Not the migration, not the list it walks --
so a table added to the request path without being declared here failed exactly
the way the rule warns about, which is silently.

That is not hypothetical. `projects.created_by` held member ids while
`_projects_visible_to` ended its WHERE with `OR p.created_by = ?` against the
login, so the clause never matched: a project you created was invisible to you
unless it was *also* shared with everyone. `projects_broker_one` refuses a
credential whose `created_by` is not the asking user, so a credential that was
not shared could not be used by anybody. Both read as working, because the one
project in this house happens to be shared and so is its credential.
"""
import os
import shutil
import sqlite3
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-loginkeys-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)

# Numeric logins on purpose. A fixture where the login and the member id are the
# same string cannot tell the two apart, which is how this went unnoticed.
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    # `member` is the field _member_of reads; without it the login *is* the
    # member as far as the migration is concerned, and it correctly skips every
    # row. The first version of this fixture left it out and read the skip as a
    # broken migration.
    f.write('[{"username": "900000111", "member": "user1"},'
            ' {"username": "900000222", "member": "user2"}]')

sys.path.insert(0, dst)
import app as A  # noqa: E402

FAILED = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + ("" if ok else f"  <- {detail}"))
    if not ok:
        FAILED.append(label)


def _db(name, table, col, rows):
    """A throwaway database with one person-keyed table in it."""
    path = os.path.join("backup_data", name)
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} "
                 f"({col} TEXT, thing TEXT, note TEXT, UNIQUE({col}, thing))")
    conn.executemany(f"INSERT OR IGNORE INTO {table} ({col}, thing, note) VALUES (?,?,?)",
                     rows)
    conn.commit()
    conn.close()
    return path


def _all(path, table):
    conn = sqlite3.connect(path)
    try:
        return sorted(conn.execute(f"SELECT * FROM {table}").fetchall())
    finally:
        conn.close()


print("the projects registry keys on the login, like everything a request reaches")
_declared = {(f, t, c) for f, t, c in A.LOGIN_KEYED_TABLES}
for entry, why in (
    (("projects.db", "projects", "created_by"),
     "_projects_visible_to compares it to the session's name"),
    (("projects.db", "project_credentials", "created_by"),
     "projects_broker_one refuses a credential whose created_by is not the asker"),
    (("projects.db", "project_access", "username"),
     "the visibility subquery matches it against the session's name"),
):
    check(f"  {entry[1]}.{entry[2]} is declared", entry in _declared, why)

print("\nevery declared table names a real column of a real file")
for filename, table, col in A.LOGIN_KEYED_TABLES:
    check(f"  {filename}:{table}.{col} is spelled plausibly",
          filename.endswith(".db") and table.isidentifier() and col.isidentifier(),
          f"{filename} {table} {col}")

print("\nthe migration moves a member-keyed row onto the login")
p = _db("mig-one.db", "widgets", "username",
        [("user1", "a", "kept"), ("900000222", "b", "already right")])
A.LOGIN_KEYED_TABLES = (("mig-one.db", "widgets", "username"),)
A.migrate_person_keys()
rows = _all(p, "widgets")
check("  the member id is gone", not any(r[0] == "user1" for r in rows), rows)
check("  and its row is now the login", ("900000111", "a", "kept") in rows, rows)
check("  a row already keyed right is untouched",
      ("900000222", "b", "already right") in rows, rows)

print("\na person keyed both ways is merged, not dropped")
# The first version of this dropped every member-keyed row in a table that had
# any collision at all -- seventy rows of one household's app history, to keep
# the eight written since. Collisions are per (person, thing).
p = _db("mig-merge.db", "widgets", "username",
        [("user1", "same", "old"), ("900000111", "same", "new"),
         ("user1", "only-old", "moves")])
A.LOGIN_KEYED_TABLES = (("mig-merge.db", "widgets", "username"),)
A.migrate_person_keys()
rows = _all(p, "widgets")
check("  the correctly-keyed row wins the collision",
      ("900000111", "same", "new") in rows, rows)
check("  and the one with no collision moves across",
      ("900000111", "only-old", "moves") in rows, rows)
check("  nothing is left under the member id",
      not any(r[0] == "user1" for r in rows), rows)

print("\nrunning it twice changes nothing the second time")
before = _all(p, "widgets")
A.migrate_person_keys()
check("  idempotent", _all(p, "widgets") == before, _all(p, "widgets"))

print("\na table that does not exist is skipped rather than raising")
A.LOGIN_KEYED_TABLES = (("nope.db", "missing", "username"),)
try:
    A.migrate_person_keys()
    check("  a missing file is survivable", True)
except Exception as exc:  # noqa: BLE001
    check("  a missing file is survivable", False, f"{type(exc).__name__}: {exc}")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all checks passed")

"""Where a `download:` link is allowed to reach.

Run: python local/test_alfred_files.py

Alfred's files live in `<su-carpeta>/alfred/` on the family share, so a link
resolves against the same per-person access the Files page enforces instead of
against his container. That widens what one URL can name from "one directory of
throwaway media" to "the whole family share", and the interesting cases are all
about what it must still refuse: another member's folder, a walk out of the
share, a path bent into a header.

The route itself needs Flask and a live SMB mount, so the decision it makes is
lifted out by AST and driven directly — the approach test_dirsize.py
established. Only the resolvers are real; `_shared_resolve` reads a sqlite
table and is stubbed with the grants each case is about.
"""
import ast
import sys
from pathlib import Path

APP = Path(__file__).with_name("app.py")
TREE = ast.parse(APP.read_text(encoding="utf-8"))

WANT_FUNCS = ("_norm_rel", "_files_access", "_alfred_share_dir", "_smb_root",
              "_files_resolve", "_share_download_path")
# ADVANCED_USERS is in here because `FILES_ADMINS = ADVANCED_USERS` -- it used
# to be a literal set of login ids and is now whatever the deployer supplies,
# so lifting the one without the other execs a name that does not exist yet.
# It is refreshed from HOMECORE_ADMIN_MEMBERS at request time; this slice only
# needs it to be *defined*, and the cases below fill it themselves.
WANT_NAMES = ("FILES_FOLDERS", "FILES_ADMINS", "ADVANCED_USERS", "FAMILY_FOLDER",
              "ALFRED_FOLDER", "FILES_HIDDEN", "APK_DEPLOY_FOLDER", "SMB_HOST",
              "SMB_SHARE")

# One namespace, used as exec's globals: with a separate locals dict the lifted
# functions close over the globals and cannot see each other.
ns = {"os": __import__("os")}
body = []
for node in TREE.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
        body.append(node)
    elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in WANT_NAMES for t in node.targets):
        # SMB_HOST/SMB_SHARE come from os.environ.get(...) — keep the default.
        body.append(node)
exec(compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"), ns)
missing = [n for n in WANT_FUNCS + WANT_NAMES if n not in ns]
assert not missing, f"app.py no longer defines {missing}"

USER1, USER2, USER3 = "user1", "user2", "user3"   # Alex+Sam are admins

# The house, stated here rather than lifted. `FILES_FOLDERS` and
# `FILES_ADMINS` used to be literals in app.py and this slice got them for
# free; they are supplied by the deployer now and filled in at request time,
# so an AST lift finds `{}` and every folder below resolves to nothing. The
# names are this file's own fixture -- which is the right way round: what a
# household is called is not something a test of path resolution should be
# reading out of the module it is testing.
ns["FILES_FOLDERS"].update({USER1: "user1", USER2: "user2", USER3: "user3"})
ns["FILES_ADMINS"].update({USER1, USER2})
ROOT = ns["_smb_root"]()

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def resolve(username, path, grants=()):
    """What /chat/download/<path> decides, with *grants* shared to this user."""
    ns["_shared_resolve"] = lambda user, rel: (
        ROOT + "\\" + "\\".join(rel.split("/"))
        if any(rel == g or rel.startswith(g + "/") for g in grants) else None
    )
    return ns["_share_download_path"](username, path)[1]


def denied(username, path, grants=()):
    return resolve(username, path, grants) is None


print("a person reaches what Alfred filed for them")
check("their own alfred folder",
      resolve(USER1, "user1/alfred/informe.md") == ROOT + r"\user1\alfred\informe.md")
check("and below it", resolve(USER3, "user3/alfred/documentos/tarea.docx") is not None)
check("the rest of their own folder too — links are not alfred-only",
      resolve(USER3, "user3/fotos/perro.jpg") is not None)
check("familia, which everyone writes to", resolve(USER3, "familia/lista.md") is not None)
check("names in Spanish survive intact",
      resolve(USER1, "user1/alfred/Informe anual — 2026.pdf")
      == ROOT + "\\user1\\alfred\\Informe anual — 2026.pdf")

print("\nand nothing of anybody else's")
for path in ["user2/alfred/diario.md", "user1/documentos/contrato.pdf", "user5/notas.txt"]:
    check(f"user3 cannot open {path}", denied(USER3, path), resolve(USER3, path))
check("not even by naming the share root", denied(USER3, ""))
check("a folder nobody owns is refused", denied(USER3, "otra/cosa.txt"))

print("\nunless it was actually shared with her")
check("the granted file opens",
      resolve(USER3, "user2/recetas/pastel.md", grants=["user2/recetas/pastel.md"]) is not None)
check("and things under a granted folder",
      resolve(USER3, "user2/recetas/postres/flan.md", grants=["user2/recetas"]) is not None)
check("but not its siblings",
      denied(USER3, "user2/diario.md", grants=["user2/recetas"]),
      "a grant on one folder must not open the parent")
check("and not a prefix that only looks like it",
      denied(USER3, "user2/recetas-privadas/x.md", grants=["user2/recetas"]))

print("\nan admin reaches the whole share, which is what the Files page already gives them")
check("user1 can open user2's", resolve(USER1, "user2/alfred/nota.md") is not None)
check("user2 can too", resolve(USER2, "user4/alfred/nota.md") is not None)

print("\nno link escapes the share")
for path in ["user1/../../etc/passwd", "user1/alfred/../../user2/diario.md",
             "../etc/passwd", "user1/./../../etc/shadow"]:
    check(repr(path), denied(USER1, path), resolve(USER1, path))
check("user3 cannot name an absolute path", denied(USER3, "/etc/passwd"))
# For an admin `/etc/passwd` is *not* denied — the leading slash is stripped and
# it becomes `etc/passwd` on the share, a folder that does not exist, which
# 404s. Denial is the wrong thing to assert; containment is the property, so
# assert that instead, over every input this file tries.
print("  ...and whatever does resolve stays under the share root")
for user in (USER1, USER2, USER3):
    for path in ["/etc/passwd", "user1/alfred/x.md", "familia/a/b/c.txt",
                 "user1/../user2/x.md", "//user1//alfred//x.md", "user1/alfred/",
                 "C:\\Windows\\win.ini", r"\\otro-host\share\x"]:
        unc = resolve(user, path)
        check(f"{user} {path!r}",
              unc is None or (unc == ROOT or unc.startswith(ROOT + "\\"))
              and ".." not in unc.split("\\"),
              unc)

print("\nnor smuggles anything into a response header")
# The filename goes into Content-Disposition. A CR or LF in it would split the
# response; a NUL would be handed to the SMB layer as a name it cannot mean.
for path in ["user1/alfred/x\r\nSet-Cookie: a=b.md", "user1/alfred/x\n.md",
             "user1/alfred/x\x00.md", "user1/alfred/\x7f.md"]:
    check(repr(path), denied(USER1, path), resolve(USER1, path))

print("\nwhere each person's files are")
check("user1", ns["_alfred_share_dir"](USER1)[0] == "user1/alfred")
check("user3", ns["_alfred_share_dir"](USER3)[0] == "user3/alfred")
check("and the UNC matches the resolver",
      ns["_alfred_share_dir"](USER1)[1] == ROOT + r"\user1\alfred")
check("an account with no folder has none", ns["_alfred_share_dir"]("999")[0] is None)

print("\nthe hidden deploy folder stays hidden here too")
# It is excluded from every Files listing, admin browse included. A download:
# link could only ever mean media/ before this leg existed, so naming it was
# not a question that came up.
for user in (USER1, USER2, USER3):
    for path in ["alfred-app/app-release.apk", "alfred-app/latest.json",
                 "alfred-app/sub/x"]:
        check(f"{user} cannot name {path}", denied(user, path), resolve(user, path))
check("and that is the folder the app is served from",
      ns["APK_DEPLOY_FOLDER"] in ns["FILES_HIDDEN"])

print("\n`media/` stays reserved for the workspace leg")
# chat_download splits on this prefix before anything here runs. It must not be
# reachable as a share folder, or the two legs would disagree about one path.
check("it is not a folder on the share", "media" not in ns["FILES_FOLDERS"].values())
check("nor the family folder", ns["FAMILY_FOLDER"] != "media")
check("and a non-admin cannot name it", denied(USER3, "media/foto.jpg"))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")

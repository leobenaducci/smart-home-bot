"""Exercise _dir_size against a fake share.  Run: python local/test_dirsize.py

smbclient is only in the deployed image, so the function is lifted out of
app.py by AST and given a stub — the same approach HomeCore's CLAUDE.md
recommends for path logic.
"""
import ast
import io
import sys
import time
import types
from pathlib import Path

# Beside this file. It was an absolute path into one Windows checkout
# (`F:\home-lab\...`), so the test could only ever run on that machine — and
# ran nowhere at all after the repo moved.
APP = Path(__file__).resolve().parent / "app.py"


class FakeEntry:
    def __init__(self, name, size=0, children=None):
        self.name = name
        self._size = size
        self.children = children

    def is_dir(self):
        return self.children is not None

    def stat(self):
        return types.SimpleNamespace(st_size=self._size)


class FakeSmb:
    """A tree keyed by UNC path, plus a set of paths that raise."""

    def __init__(self, tree, unreadable=(), delay=0.0):
        self.tree = tree
        self.unreadable = set(unreadable)
        self.delay = delay
        self.scans = 0

    def scandir(self, path):
        self.scans += 1
        if self.delay:
            time.sleep(self.delay)
        if path in self.unreadable:
            raise PermissionError(path)
        return self.tree[path]


def load_dir_size(smb, hidden=frozenset({"_apk"}), **overrides):
    """Compile _dir_size alone, with its module globals stubbed."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_dir_size")
    ns = {
        "smbclient": smb,
        "FILES_HIDDEN": hidden,
        "time": time,
        "threading": __import__("threading"),
        "_dirsize_cache": {},
        "_dirsize_lock": __import__("threading").Lock(),
        "_DIRSIZE_TTL_S": 300,
        "_DIRSIZE_MAX_ENTRIES": 50_000,
        "_DIRSIZE_MAX_SECONDS": 25.0,
    }
    ns.update(overrides)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "app.py", "exec"), ns)
    return ns["_dir_size"], ns


ROOT = r"\\compute.home\share\user1"
FLAT = {ROOT: [FakeEntry("a.pdf", 100), FakeEntry("b.jpg", 250)]}
NESTED = {
    ROOT: [FakeEntry("a.pdf", 100), FakeEntry("docs", children=True)],
    fr"{ROOT}\docs": [FakeEntry("c.docx", 400), FakeEntry("sub", children=True)],
    fr"{ROOT}\docs\sub": [FakeEntry("d.txt", 7)],
}

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + detail}")
    if not cond:
        failures.append(label)


print("flat folder")
f, _ = load_dir_size(FakeSmb(FLAT))
r = f(ROOT)
check("sums file sizes", r["bytes"] == 350, str(r))
check("counts files", r["files"] == 2, str(r))
check("not partial", r["partial"] is False, str(r))

print("\nnested folders")
f, _ = load_dir_size(FakeSmb(NESTED))
r = f(ROOT)
check("recurses to the bottom", r["bytes"] == 507, str(r))
check("counts subdirectories", r["dirs"] == 2, str(r))
check("counts every file", r["files"] == 3, str(r))

print("\nhidden folder is skipped")
tree = {ROOT: [FakeEntry("a.pdf", 100), FakeEntry("_apk", children=True)],
        fr"{ROOT}\_apk": [FakeEntry("huge.apk", 99_999)]}
f, _ = load_dir_size(FakeSmb(tree))
r = f(ROOT)
check("does not descend into FILES_HIDDEN", r["bytes"] == 100, str(r))

print("\nunreadable subfolder")
f, _ = load_dir_size(FakeSmb(NESTED, unreadable={fr"{ROOT}\docs\sub"}))
r = f(ROOT)
check("still returns what it could read", r["bytes"] == 500, str(r))
check("marks the result partial", r["partial"] is True, str(r))

print("\nentry budget")
wide = {ROOT: [FakeEntry(f"f{i}.bin", 1) for i in range(100)]}
f, _ = load_dir_size(FakeSmb(wide), _DIRSIZE_MAX_ENTRIES=10)
r = f(ROOT)
check("stops at the cap", r["bytes"] <= 100, str(r))
check("reports partial when capped", r["partial"] is True, str(r))

print("\ntime budget")
slow = {ROOT: [FakeEntry("d1", children=True)], fr"{ROOT}\d1": [FakeEntry("x", 5)]}
f, _ = load_dir_size(FakeSmb(slow, delay=0.05), _DIRSIZE_MAX_SECONDS=0.01)
r = f(ROOT)
check("gives up on a slow share", r["partial"] is True, str(r))

print("\ncaching")
smb = FakeSmb(NESTED)
f, _ = load_dir_size(smb)
f(ROOT); first = smb.scans
f(ROOT)
check("second call does not re-walk", smb.scans == first, f"{smb.scans} vs {first}")

print("\nempty folder")
f, _ = load_dir_size(FakeSmb({ROOT: []}))
r = f(ROOT)
check("zero, not partial", r == {"bytes": 0, "files": 0, "dirs": 0, "partial": False}, str(r))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")

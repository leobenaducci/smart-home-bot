#!/usr/bin/env python3
"""Every name the portal calls on startup exists.

Run: python local/test_starts.py   (needs no Flask, no certs, no port)

`init_backup_db()` was called on the first line of `if __name__ == '__main__':`
and defined nowhere. `python app.py` raised NameError before binding anything,
so the container crash-looped and the portal had never once started in this
package -- while every check here passed, because they all `import app`, and a
name inside that block is only resolved when the block runs.

The backup history it set up belongs to `home-backups`, a service upstream
ships and this package does not. Its definition and every reader of it came out
at extraction; the call did not.

So this reads the startup block without executing it: ast, no import, no
subprocess, no TLS. It cannot bind a port and cannot be slow, and it catches
the whole class -- a function that moved, was renamed, or left with a feature.
"""
import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


tree = ast.parse((HERE / "app.py").read_text(encoding="utf-8"))

# Everything the module defines or imports at the top level, plus the builtins.
defined = set(dir(__builtins__) if isinstance(__builtins__, dict)
              else dir(__builtins__))
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        defined.add(node.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    defined.add(n.id)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        for n in ast.walk(node.target):
            if isinstance(n, ast.Name):
                defined.add(n.id)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            defined.add((a.asname or a.name).split(".")[0])
    elif isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
        # Conditionally defined names still count: a try/except ImportError
        # around an import is how three of these arrive.
        for sub in ast.walk(node):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(sub.name)
            elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                for a in sub.names:
                    defined.add((a.asname or a.name).split(".")[0])
            elif isinstance(sub, ast.Assign):
                for t in sub.targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            defined.add(n.id)

main_blocks = [
    node for node in tree.body
    if isinstance(node, ast.If)
    and isinstance(node.test, ast.Compare)
    and isinstance(node.test.left, ast.Name)
    and node.test.left.id == "__name__"
]
# There is more than one -- the file guards two things this way -- so this
# scans all of them rather than assuming a count it would have to keep in step.
check("app.py has at least one startup block", bool(main_blocks), len(main_blocks))

called = []
for block in main_blocks:
    for node in ast.walk(block):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.append((node.func.id, node.lineno))
        # Names bound inside the block are available to later lines in it.
        if isinstance(node, ast.Assign):
            for t in node.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        defined.add(n.id)

check("it calls something", bool(called), called)
missing = [(name, line) for name, line in called if name not in defined]
check(f"every one of the {len(called)} names it calls is defined",
      not missing,
      "; ".join(f"{n}() at line {ln}" for n, ln in missing))

# The specific shape, named: these are the ones whose absence is a crash-loop
# rather than a 500 on one page.
inits = sorted({n for n, _ in called if n.startswith("init_")})
check(f"including all {len(inits)} schema initialisers",
      all(n in defined for n in inits),
      [n for n in inits if n not in defined])

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")

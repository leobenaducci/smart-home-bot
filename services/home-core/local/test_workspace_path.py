"""What /chat/download may ask a nanobot for.
Run: python local/test_workspace_path.py

The proxy passed its path straight through, and the workspace on the other side
holds more than the pictures Alfred links to: cron/jobs.json, memory/, and
sessions/*.jsonl — whole conversations, model reasoning included. So the
workspace leg allows `media/` and nothing else.

That leg is now only *transient* delivery — a camera snapshot, a thumbnail, an
image shown inline. Anything meant to last lives on the family share, and a
`download:` link to it takes the other leg entirely; see
test_alfred_files.py for what that one may reach.

Importing app.py needs Flask and the rest of the deployed image, so the
function is lifted out by AST — the approach test_dirsize.py established.
"""
import ast
import sys
from pathlib import Path
from urllib.parse import quote

APP = Path(__file__).with_name("app.py")


def load(name):
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "app.py", "exec"), ns)
    return ns[name]


_workspace_media_path = load("_workspace_media_path")

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + detail}")
    if not cond:
        failures.append(label)


def allowed(p):
    return _workspace_media_path(p) == p


print("what the chat actually links to")
for p in [
    "media/cam_patio_1785208081.jpg",          # camera-feed
    "media/report.docx",                       # document
    "media/doc_41_thumb.jpg",                  # paperless
    "media/perro.jpg",                         # file-share
    "media/subcarpeta/informe final.pdf",      # spaces survive; quote() handles them
    "media/año_2026.pdf",                      # and accents
]:
    check(p, allowed(p))

print("\nthe rest of the workspace is not downloadable")
for p in [
    "sessions/cron_955a0907.jsonl",            # a whole conversation
    "sessions/homeweb_user1_2026-08-03.jsonl",
    "cron/jobs.json",
    "memory/MEMORY.md",
    "memory/history.jsonl",
    "config.json",
]:
    check(p, _workspace_media_path(p) is None, repr(p))

print("\nand neither is anything reached by escaping it")
for p in [
    "media/../cron/jobs.json",
    "media/../../etc/passwd",
    "media/./../memory/MEMORY.md",
    "/etc/passwd",                             # absolute: joining one replaces the base
    "media//../cron/jobs.json",                # empty segment
    "media\\..\\..\\etc\\passwd",              # backslashes
    "media/x\x00.jpg",                         # NUL
    "media/x\n.jpg",                           # header-shaped
]:
    check(repr(p), _workspace_media_path(p) is None, repr(p))

print("\nnot a file at all")
for p in ["", None, "media", "media/", "media/.", "media/..", "mediaX/a.jpg", "amedia/a.jpg"]:
    check(repr(p), _workspace_media_path(p) is None, repr(p))

print("\nthe path is quoted before it becomes a URL")
check("a space cannot split the request line",
      quote("media/informe final.pdf") == "media/informe%20final.pdf")
check("a query character cannot start a query string",
      quote("media/a?b=c.jpg") == "media/a%3Fb%3Dc.jpg")
check("separators survive", quote("media/sub/a.jpg") == "media/sub/a.jpg")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")

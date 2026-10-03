"""Every push the portal sends opens the place it is about, in the Android app.

Run: python local/test_notification_links.py   (needs Flask; skips without it)

A notification's `click` link is where a tap lands. Without one the app falls
back to the bare chat, and for anything that is not a message in the chat --
a chore approved, points, a prize, a shared file, a Studio job -- that is a
screen with nothing about it (2026-10-02: fourteen of them did that). And a
link the app does not accept falls back the same way, silently: the app opens
only the paths in MainActivity's DEEP_LINK_PREFIXES, so a Studio link sent to
an app built before `/studio` was added opened the chat too.

So, read from the source rather than from a list kept by hand:

  * every call that sends a push names its link, except the control messages
    the app handles without showing anything (its tags are read from the app);
  * every link builder returns a path the app accepts, read from the app's
    own list -- the two sides cannot drift apart unnoticed;
  * and the links say where: the chores panel on the task, the review list for
    a parent, the files panel, the prizes tab, the Studio on its project.
"""
import ast
import os
import re
import shutil
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

SRC = os.path.dirname(os.path.abspath(__file__))
APP_KT = os.path.join(SRC, "..", "..", "proxy", "android", "app", "src", "main", "java", "com", "chat",
                      "app", "MainActivity.kt")
NTFY_KT = os.path.join(SRC, "..", "..", "proxy", "android", "app", "src", "main", "java", "com", "chat",
                       "app", "ntfy", "NtfyClientService.kt")

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  <- {detail}" if not cond and detail != "" else ""))
    if not cond:
        failures.append(label)


# The app's source is in the repository but not in the portal's image, where
# the deployer runs this suite. There the app's lists are the ones last read
# here, and the checks that compare the two sides are skipped, saying so.
HAVE_APP = os.path.exists(APP_KT) and os.path.exists(NTFY_KT)
if HAVE_APP:
    kt = open(APP_KT, encoding="utf-8").read()
    PREFIXES = re.findall(r'"(/[a-z]+)"', re.search(r"DEEP_LINK_PREFIXES\s*=\s*listOf\(([^)]*)\)", kt).group(1))
    SILENT_TAGS = set(re.findall(r'_TAG\s*=\s*"([a-z_]+)"', open(NTFY_KT, encoding="utf-8").read()))
else:
    PREFIXES = ["/chat", "/tasks", "/grocery", "/geo", "/menu", "/studio"]
    SILENT_TAGS = {"geofence_sync", "notif_sync", "notif_reply", "location_request", "location_track",
                   "location_track_stop", "ring_phone", "ring_phone_stop", "device_cmd", "family_msg",
                   "family_stop", "family_sms", "alfred_update", "alfred_update_beta"}


def app_opens(url):
    """What the app does with *url*: the path it opens, or None for the chat
    fallback (MainActivity.deepLinkUrl)."""
    path = urlparse(url).path
    return path if any(path == p or path.startswith(p + "/") for p in PREFIXES) else None


print("what the app accepts")
if HAVE_APP:
    check("the app's deep-link list is read", "/chat" in PREFIXES and "/tasks" in PREFIXES, PREFIXES)
    check("and the tags it handles without showing anything", {"geofence_sync", "notif_sync", "location_request",
                                                                 "family_msg", "ring_phone"} <= SILENT_TAGS,
          sorted(SILENT_TAGS))
else:
    print("  SKIP  the app's source is not here (the image holds only the portal): its lists as last read")

print("\nevery push names where it opens")
src = open(os.path.join(SRC, "app.py"), encoding="utf-8").read()
tree = ast.parse(src)
SENDERS = ("send_ntfy", "_notify_user", "_notify_tasks_admins")
# The two helpers pass their caller's link on; what they are given is checked
# at their callers.
PASS_THROUGH = {"_notify_user", "_notify_tasks_admins", "_geo_push_control"}


class Calls(ast.NodeVisitor):
    def __init__(self):
        self.stack, self.found = [], []

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node):
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in SENDERS:
            self.found.append((node, self.stack[-1] if self.stack else ""))
        self.generic_visit(node)


v = Calls()
v.visit(tree)
missing, silent = [], 0
for node, fn in v.found:
    if fn in PASS_THROUGH:
        continue
    click = next((k for k in node.keywords if k.arg == "click"), None)
    if click is not None:
        continue
    tags = next((k.value for k in node.keywords if k.arg == "tags"), None)
    tag = tags.value if isinstance(tags, ast.Constant) else None
    if tag in SILENT_TAGS:
        silent += 1
        continue
    missing.append(f"{fn} (line {node.lineno})")
check(f"all {len(v.found)} calls name their link, but the {silent} control messages the app never shows",
      not missing, missing)

print("\nthe links go where they say")
tmp = tempfile.mkdtemp(prefix="homecore-links-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns("__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32, DEBUG_API_KEY="d" * 32)
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "user1", "nanobot_id": 2}]')
sys.path.insert(0, dst)
import app as A  # noqa: E402


def q(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


links = {
    "a reply, on its day and space": A.chat_link(date="2026-10-02"),
    "a chore": A.tasks_link(42),
    "a chore to review": A.tasks_link(42, review=True),
    "the prizes to hand over": A.tasks_page_link("redeems"),
    "a shared file": A.files_link(),
    "the shopping list": A._grocery_chat_link(),
    "the week's menu": A._menu_chat_link(),
}
for what, url in links.items():
    check(f"{what}: the app opens it ({urlparse(url).path})", app_opens(url) is not None, (url, PREFIXES))
check("a chore opens the chores panel on that task",
      q(A.tasks_link(42)) == {"panel": "tasks", "task": "42"}, A.tasks_link(42))
check("one to review opens a parent's review list on it",
      q(A.tasks_link(7, review=True)) == {"panel": "tasks", "mode": "review", "task": "7"})
check("a prize request opens the chores page on its prizes tab",
      urlparse(A.tasks_page_link("redeems")).path == "/tasks" and q(A.tasks_page_link("redeems")) == {"tab": "redeems"})
check("a shared file opens the files panel", q(A.files_link()) == {"panel": "files"})

# The panels and the tab exist where the links point.
chat_html = open(os.path.join(SRC, "templates", "chat.html"), encoding="utf-8").read()
for panel in ("tasks", "files", "grocery", "menu", "family"):
    check(f"the chat opens the {panel} panel from ?panel=", f"params.get('panel') === '{panel}'" in chat_html)
check("and the review list from ?mode=review", "openTasks(params.get('mode')" in chat_html)
tasks_html = open(os.path.join(SRC, "templates", "tasks.html"), encoding="utf-8").read()
check("the chores page opens the tab its link names",
      "get('tab')" in tasks_html and 'data-tab="redeems"' in tasks_html)

# The Studio's own notification, built by the route the Studio calls.
sent = []
A._notify_user = lambda *a, **k: sent.append(k.get("click")) or True
A.STUDIO_URL, A.STUDIO_SECRET = "http://studio.invalid", "s" * 32
c = A.app.test_client()
r = c.post("/studio/api/notify", json={"login": "user1", "ok": True, "what": "a song", "project": "abc123"},
           headers={"X-Studio-Secret": "s" * 32})
check("a finished Studio job opens the Studio on its project",
      r.status_code == 200 and sent and app_opens(sent[-1]) == "/studio" and q(sent[-1]).get("project") == "abc123",
      (r.status_code, sent))

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all checks passed")

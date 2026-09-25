"""The WhatsApp panel in the chat page, which is how the phone configures it.

Run: python local/test_whatsapp_panel.py   (needs Flask; skips loudly without it)

The Alfred Android app is a WebView around this page plus native helpers — there
is no native settings screen. So "configure WhatsApp in the app" is a panel in
`chat.html`, exactly like `#notif-panel` beside it, and it ships with a HomeCore
deploy rather than an APK release.

What this pins is the wiring, because every part of it is silent when broken: a
menu entry whose `data-app` nothing dispatches just closes the menu, a panel
whose id the JS never looks up never opens, and a fetch to a path that does not
exist fails inside a `try` that paints an empty list. None of that throws where
anybody can see it.

The endpoints themselves are covered by test_whatsapp_store.py; this is only
about the three ends meeting.
"""
import os
import re
import shutil
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-wapanel-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32)

USER1 = "user1"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}]' % USER1)

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_wa_db()

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


html = open(os.path.join(dst, "templates", "chat.html"), encoding="utf-8").read()

# --- The three ends meet ------------------------------------------------------
print("\nthe menu entry, the dispatcher and the panel")
check("there is a WhatsApp entry in the apps menu",
      'data-app="wa"' in html)
check("and something dispatches it", "app === 'wa'" in html and 'openWa()' in html)
check("openWa exists and opens the panel by id",
      re.search(r"function openWa\(\)\s*\{[^}]*waPanel\.classList\.add\('open'\)", html) is not None)
check("the panel exists with that id", 'id="wa-panel"' in html)
check("its back button closes it",
      "wa-back-btn" in html and re.search(
          r"wa-back-btn'\)\.addEventListener\('click', function\(\) \{\s*waPanel\.classList\.remove\('open'\)",
          html) is not None)

print("\nthe containers the loader fills")
for el in ("wa-chats", "wa-recent"):
    check(f"#{el} exists in the markup and is looked up in JS",
          f'id="{el}"' in html and f"getElementById('{el}')" in html)

# --- Every path it calls is a real route ---------------------------------------
print("\nevery path it fetches actually exists")
paths = set(re.findall(r"waApi\('([a-z]+)", html))
check("it calls chats and recent", {'chats', 'recent'} <= paths, paths)
routes = {str(r) for r in A.app.url_map.iter_rules()}
for p in sorted(paths):
    check(f"/chat/whatsapp/{p} is a route", f'/chat/whatsapp/{p}' in routes,
          sorted(x for x in routes if 'whatsapp' in x))

print("\nwriting a setting needs the CSRF token, like every other panel")
check("waApi sends X-CSRF-Token on writes",
      re.search(r"async function waApi[\s\S]{0,400}X-CSRF-Token", html) is not None)

# --- The three reply modes are the ones the server accepts ---------------------
print("\nthe buttons offer exactly what the server takes")
modes = re.search(r"var WA_MODES = \[(.*?)\];", html, re.S)
check("WA_MODES is defined", modes is not None)
offered = set(re.findall(r"\['(\w+)',", modes.group(1))) if modes else set()
check("off / ask / auto, and nothing else", offered == {'off', 'ask', 'auto'}, offered)

client = A.app.test_client()
A.app.config['TESTING'] = True
h = {'X-Proxy-Secret': 'p' * 32, 'X-Proxy-User': USER1}
client.post('/chat/whatsapp/message', headers=h,
            json={'chat_id': 'c1', 'text': 'hola', 'name': 'Jana', 'message_id': 'm1'})
for mode in sorted(offered):
    r = client.post('/chat/whatsapp/chats', headers=h,
                    json={'chat_id': 'c1', 'reply_mode': mode})
    check(f"the server accepts '{mode}'", r.status_code == 200, r.get_json())
r = client.post('/chat/whatsapp/chats', headers=h,
                json={'chat_id': 'c1', 'reply_mode': 'siempre'})
check("and rejects a mode the panel never offers", r.status_code == 400)

# --- The panel does not claim more than the server does ------------------------
print("\nwhat the panel says matches what the server does")
panel = html[html.index('id="wa-panel"'):html.index('id="notif-panel"')]
check("it tells the user the trigger word", 'alfred' in panel.lower(), panel[:200])
check("it explains that answering is per conversation",
      'preguntar' in panel and 'siempre' in panel)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)

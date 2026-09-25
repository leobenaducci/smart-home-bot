"""The profiler panel, reachable from the app instead of from a URL bar.

Run: python local/test_profile_panel_proxy.py   (needs Flask; skips loudly without it)

nanobot serves the panel on its API port, authenticated by a secret in the URL
fragment. That works from a terminal and not from a phone: nobody pastes a
secret into a browser bar, and the API port is not reachable from outside the
house anyway. So there is a menu entry, and HomeCore stands in front:

- the page itself is *fetched from the instance*, not copied here, so there is
  one panel and not two that drift;
- its data call is proxied with the secret added on this side, so the browser
  never carries it;
- both are behind the same gate as «Alfred's usage», because this says what
  the family asked and how long each turn took.
"""
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

tmp = tempfile.mkdtemp(prefix="homeweb-profile-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
SECRET = "s" * 40
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32, NANOBOT_DEBUG_SECRET=SECRET,
                  # Who lives here and who is a parent. Both were literals in
                  # app.py -- `ADVANCED_USERS = {...}` -- and are supplied by
                  # the deployer now, so a suite that does not say leaves the
                  # house with no adults in it and every admin route answers
                  # 403 with "Solo administradores".
                  HOMECORE_MEMBERS="user1,user2,user3",
                  HOMECORE_ADMIN_MEMBERS="user1")

ADMIN, OTHER, KID = "user1", "user2", "user3"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2},'
            ' {"username": "%s", "nanobot_id": 3},'
            ' {"username": "%s", "nanobot_id": 4}]' % (ADMIN, OTHER, KID))

sys.path.insert(0, dst)
import app as A  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


asked = []


class FakeResp:
    def __init__(self, text="", status=200, ctype="application/json"):
        self.text = text
        self.content = text.encode()
        self.status_code = status
        self.headers = {"Content-Type": ctype}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def fake_get(url, headers=None, params=None, timeout=None, **kw):
    asked.append({"url": url, "headers": headers or {}, "params": params or {}})
    if url.endswith(".html"):
        return FakeResp("<html>the panel</html>", ctype="text/html")
    if fake_get.down:
        raise RuntimeError("connection refused")
    return FakeResp('{"counts": {"spans": 3}}')


fake_get.down = False
A.requests.get = fake_get

client = A.app.test_client()


def login(user):
    with client.session_transaction() as sess:
        sess['user'] = user


def get(path, user=ADMIN):
    login(user)
    asked.clear()
    return client.get(path)


print("the gate is the one «Alfred's usage» already uses")
r = get('/alfred/profile.html', user=KID)
check("a non-advanced user is redirected, not shown the panel",
      r.status_code == 302, r.status_code)
r = get('/alfred/profile', user=KID)
check("and cannot reach the data either", r.status_code == 403, r.status_code)

print("\nthe panel comes from the instance, not from a copy kept here")
r = get('/alfred/profile.html')
check("served", r.status_code == 200, r.status_code)
check("fetched from nanobot", asked[-1]['url'].endswith('/v1/debug/profile.html'),
      asked[-1]['url'])
# Derived from the app's own base rather than pinned. Pinned numbers are how
# the renumbering went green: this file asserted `:8902/` while the branch that
# added it moved the base to 21301, so the one test whose job is "which
# instance does the proxy dial" was evidence that the stale answer was right.
check("from the viewer's own instance (nanobot_id 2)",
      f':{A.NANOBOT_BASE_PORT + 1}/' in asked[-1]['url'], asked[-1]['url'])
check("and it is the instance's own markup", b'the panel' in r.data, r.data[:60])

print("\nthe secret is added here and never reaches the browser")
r = get('/alfred/profile?limit=25')
check("the data call works", r.status_code == 200, r.status_code)
check("the secret went out as a header",
      asked[-1]['headers'].get('X-Debug-Secret') == SECRET, asked[-1]['headers'])
check("the panel's own query survives", asked[-1]['params'].get('limit') == '25',
      asked[-1]['params'])
check("and nothing in the reply carries it", SECRET.encode() not in r.data, r.data[:80])

page = get('/alfred/profile.html').data
check("nor does the page", SECRET.encode() not in page, page[:80])

print("\nan admin can look at somebody else's instance, because that is where")
print("a slow turn reported by somebody else actually happened")
r = get('/alfred/profile?id=3')
check("the other member's instance is the next port up",
      f':{A.NANOBOT_BASE_PORT + 2}/' in asked[-1]['url'], asked[-1]['url'])
check("and `id` is not forwarded as a query param to nanobot",
      'id' not in asked[-1]['params'], asked[-1]['params'])

# The instance has to live in the path. The panel is nanobot's own page and it
# fetches its data with a *relative* URL, which keeps the directory and drops
# the query string — so `?id=3` served instance 3's markup and then filled it
# with the viewer's own numbers, and the wrong container got diagnosed.
r = get('/alfred/3/profile.html')
check("the panel can be addressed by path", r.status_code == 200, r.status_code)
check("and it is instance 3's markup", f':{A.NANOBOT_BASE_PORT + 2}/' in asked[-1]['url'], asked[-1]['url'])
r = get('/alfred/3/profile?limit=25')
check("so the panel's own relative data call keeps the instance",
      f':{A.NANOBOT_BASE_PORT + 2}/' in asked[-1]['url'], asked[-1]['url'])
check("with its query intact", asked[-1]['params'].get('limit') == '25',
      asked[-1]['params'])
r = get('/alfred/profile.html?id=3')
check("and `?id=` is answered with the URL that survives the fetch",
      r.status_code == 302 and r.headers['Location'].endswith('/alfred/3/profile.html'),
      (r.status_code, r.headers.get('Location')))

print("\nan instance that is not there costs an error, not a stack trace")
fake_get.down = True
r = get('/alfred/profile')
check("502, with a message", r.status_code == 502, r.status_code)
check("and it says which way it failed",
      (r.get_json() or {}).get('error') == 'instance unreachable', r.get_json())
fake_get.down = False

print("\nand without the secret configured it says so instead of guessing")
A.NANOBOT_DEBUG_SECRET = ''
r = get('/alfred/profile')
check("503", r.status_code == 503, r.status_code)
check("naming the missing variable",
      'NANOBOT_DEBUG_SECRET' in ((r.get_json() or {}).get('error') or ''), r.get_json())
A.NANOBOT_DEBUG_SECRET = SECRET

print("\nthe way in is a menu entry, next to the one it belongs beside")
html = open(os.path.join(dst, 'templates', 'chat.html'), encoding='utf-8').read()
check("the button exists", '/alfred/profile.html' in html)
i_stats, i_prof = html.index('href="/stats"'), html.index('/alfred/profile.html')
check("it sits with «Alfred's usage»", 0 < i_prof - i_stats < 800, i_prof - i_stats)
check("and inside the same admin block",
      html[:i_prof].rindex('{% if is_admin %}') > html[:i_prof].rindex('{% endif %}'))

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all good")

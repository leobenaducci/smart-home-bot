#!/usr/bin/env python3
"""Las cámaras, served through this app.

It used to be a link to another host with its own front door, behind one
shared admin password — so it could not say *which* member was looking, and
could not carry that member's theme. Proxied here it inherits this app's
login.

What can go quietly wrong, and is therefore what this checks:

- forwarding the member's identity, as a *derived* token rather than the
  master secret, so what travels can only ever claim to be that one member;
- forwarding this app's own session cookie, which would hand another container
  the thing that logs anybody in here;
- letting the far side's absolute redirects escape the mount, which would
  throw somebody logging in over there out to the portal instead;
- forwarding the mount, without which those apps cannot write a single link
  that comes back to them.

Run where app.py can import (needs flask, bcrypt, websocket-client,
smbprotocol). Skips rather than fails when it cannot.
"""

import hashlib
import http.server
import os
import shutil
import socketserver
import sys
import tempfile
import threading

os.environ.setdefault('SECRET_KEY', 'test-secret-key-for-the-proxy-tests')
os.environ.setdefault('DEBUG_API_KEY', 'test-debug-key-for-the-proxy-tests')
os.environ['PROXY_SHARED_SECRET'] = 'testsecret'

_seen = {}


class _Upstream(http.server.BaseHTTPRequestHandler):
    """Stands in for the camera app: records what reached it, and answers the
    one redirect that matters."""

    def log_message(self, *a):
        pass

    # DELETE as well as GET, or the "with the token it gets through" check
    # below is answered by BaseHTTPRequestHandler's own 501 and would keep
    # passing with the proxy removed entirely.
    def do_DELETE(self):
        self.do_GET()

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        _seen.clear()
        _seen.update(path=self.path,
                     user=self.headers.get('X-Proxy-User'),
                     secret=self.headers.get('X-Proxy-Secret'),
                     prefix=self.headers.get('X-Forwarded-Prefix'),
                     cookie=self.headers.get('Cookie'),
                     csrf=self.headers.get('X-CSRF-Token'),
                     sec_fetch=self.headers.get('Sec-Fetch-Site'))
        if self.path.startswith('/needs-login'):
            self.send_response(302)
            self.send_header('Location', '/login?next=/settings')
            self.end_headers()
            return
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_srv = socketserver.TCPServer(('127.0.0.1', 0), _Upstream)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
os.environ['CAMERAS_APP_URL'] = 'http://127.0.0.1:%d' % _srv.server_address[1]

_tmp = tempfile.mkdtemp(prefix='homecore-house-proxy-')
shutil.copytree(os.path.dirname(os.path.abspath(__file__)),
                os.path.join(_tmp, 'local'),
                ignore=shutil.ignore_patterns('backup_data', 'history', '__pycache__'))
os.chdir(os.path.join(_tmp, 'local'))
sys.path.insert(0, os.getcwd())
os.makedirs('backup_data', exist_ok=True)

try:
    import app as A  # noqa: E402
except ImportError as e:
    print('SKIP: %s — run this where app.py can import.' % e)
    raise SystemExit(0)

failures = []


def check(label, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


A.app.config['TESTING'] = True
client = A.app.test_client()
# Buffered, and this suite cannot work without it. The proxy view returns
# `Response(stream_with_context(...))`, and an unbuffered test client hands back
# the app iterator without consuming it -- so the request context that generator
# holds is still pushed when the *next* request pushes its own, and the pop
# order is wrong. It surfaces far from the cause, as "Popped wrong request
# context ... /camaras/_csrf instead of /camaras/" at whichever later line first
# reads a body.
_unbuffered_open = client.open
client.open = lambda *a, **kw: _unbuffered_open(*a, **{**kw, "buffered": True})

print('a stranger is sent to the login, not to the cameras')
r = client.get('/camaras/settings')
check('it redirects', r.status_code == 302, r.status_code)
check('and nothing reached the camera app', not _seen, _seen)

print('\na member is forwarded as themselves')
with client.session_transaction() as s:
    s['user'] = 'user1'
r = client.get('/camaras/settings')
check('the page comes back', r.status_code == 200, r.status_code)
check('the mount is stripped on the way', _seen.get('path') == '/settings', _seen.get('path'))
check('the member is named', _seen.get('user') == 'user1', _seen.get('user'))
check('with a token derived for that member alone',
      _seen.get('secret') == hashlib.sha256(b'testsecret:user1').hexdigest(),
      _seen.get('secret'))
check('never the master secret itself', _seen.get('secret') != 'testsecret')
# Forwarding it would hand another container the cookie that logs anyone in
# here, and anything over there that logs headers would capture it.
check('this app keeps its own cookie', _seen.get('cookie') is None, _seen.get('cookie'))
check('and the far side is told where it is mounted',
      _seen.get('prefix') == '/camaras', _seen.get('prefix'))

print('\nthe far side cannot redirect anyone out of the mount')
r = client.get('/camaras/needs-login')
check('the redirect keeps the prefix',
      r.headers.get('Location') == '/camaras/login?next=/settings',
      r.headers.get('Location'))

print('\nthe bare mount is the far side\'s root')
r = client.get('/camaras/')
check('forwarded as /', _seen.get('path') == '/', _seen.get('path'))


print("\nthose pages are given the token they need to change anything")
# Proxied, the page is same-origin with this app, so `login_required` refuses
# any POST/PUT/DELETE without a CSRF token — correctly, or any site could post
# to /camaras/api/* with the family's cookie. Until the page could *get* the
# token, every button that changed something answered 403 with an HTML error
# page, which the page then tried to read as JSON: deleting a clip, keeping
# one, saving a camera's settings, turning a light on.
_seen.clear()
r = client.get('/camaras/_csrf')
check('there is a token to fetch', r.status_code == 200, r.status_code)
# silent=True, not `or {}`: get_json() *raises* on a non-JSON body, so the very
# regression this block exists to catch — the route gone and Flask's HTML 404
# in its place — would abort the run instead of reporting a labelled failure.
token = (r.get_json(silent=True) or {}).get('csrf')
check('and it is a token', bool(token), r.get_json(silent=True))
# The body is a credential; nothing along the way should keep a copy of it.
check('handed out uncacheable',
      'no-store' in r.headers.get('Cache-Control', ''), r.headers.get('Cache-Control'))
# `token and` first: without it two broken mounts both yield None and this
# passes by comparing nothing to nothing.
# Answered here, not forwarded: the far side has never heard of a HomeCore CSRF
# token and would 404 the path.
check('it is not forwarded upstream', not _seen, _seen)

ghost = '/camaras/api/recordings/does-not-exist.mp4'
r = client.open(ghost, method='DELETE')
check('a change with no token is still refused', r.status_code == 403, r.status_code)
_seen.clear()
r = client.open(ghost, method='DELETE', headers={'X-CSRF-Token': token})
# `!= 403` alone would also be satisfied by a 502 from an upstream that never
# answered, so check the request actually arrived and arrived stripped of the
# mount — that is the behaviour the token is supposed to unlock.
check('and with the token it reaches the far side',
      _seen.get('path') == '/api/recordings/does-not-exist.mp4', _seen.get('path'))
# And it arrives without the token: it means nothing over there, and forwarding
# it would scatter half of this app's session credential across the LAN.
check('but the token stays here', _seen.get('csrf') is None, _seen.get('csrf'))

print("\nthe browser's own word for 'this came from your page' is enough")
# Sec-Fetch-Site is a forbidden header name: page script cannot set it, so
# "same-origin" is the browser asserting the request came from a page of ours.
# Without this every proxied app needs a token round trip before it can write
# anything, and until it has one the failure reads as a JSON parse error.
r = client.open(ghost, method='DELETE', headers={'Sec-Fetch-Site': 'same-origin'})
check('a same-origin write reaches the far side',
      _seen.get('path') == '/api/recordings/does-not-exist.mp4', _seen.get('path'))
check('and the header does not travel with it',
      _seen.get('sec_fetch') is None, _seen.get('sec_fetch'))
# The exemption is for the proxied mounts only. Left in the shared gate it
# would lift token enforcement from every session-backed route in this app.
# `/theme/api/reset` goes through `api_login_required`, which is the gate the
# exemption sits in. (Not a task route: those use the native-app decorator and
# are CSRF-exempt by design, so they prove nothing here.)
r = client.post('/theme/api/reset', headers={'Sec-Fetch-Site': 'same-origin'})
check("this app's own routes still want the token", r.status_code == 403, r.status_code)

for site in ('cross-site', 'same-site', 'none'):
    r = client.open(ghost, method='DELETE', headers={'Sec-Fetch-Site': site})
    check(f'  but {site} still needs the token', r.status_code == 403, r.status_code)

print('\na body too big is refused before it is read')
big = b'x' * (A.HOUSE_PROXY_MAX_BYTES + 1)
r = client.post('/camaras/api/import_config', data=big,
                headers={'X-CSRF-Token': token, 'Content-Type': 'application/octet-stream'})
check('it is a 413', r.status_code == 413, r.status_code)
check('and it never reached the far side', _seen.get('path') != '/api/import_config',
      _seen.get('path'))
r = client.post('/camaras/api/import_config', data=b'{}',
                headers={'X-CSRF-Token': token, 'Content-Type': 'application/json'})
check('while an ordinary one reaches the far side',
      _seen.get('path') == '/api/import_config', _seen.get('path'))

# Chunked declares no length, so the content_length test short-circuited and
# the whole stream went into RAM regardless — the exact thing the cap is for.
_seen.clear()
r = client.post('/camaras/api/import_config',
                data=(b'x' * (A.HOUSE_PROXY_MAX_BYTES + 1)),
                headers={'X-CSRF-Token': token,
                         'Content-Type': 'application/octet-stream'},
                environ_overrides={'CONTENT_LENGTH': None,
                                   'wsgi.input_terminated': True})
check('a body with no declared length is capped too', r.status_code == 413, r.status_code)
check('and never reached the far side', _seen.get('path') != '/api/import_config',
      _seen.get('path'))


print('\nwhat this app serves itself is offered as a path, not as a link out')
# `A.SERVICES` is gone; the menu's own list is `CHAT_APP_LINKS`. The rule it
# encoded still holds and is what this checks: the chat template badges anything
# `http://` as "casa" and opens it outside the WebView, so a page this app
# actually serves must be a path or that badge is a lie.
#
# Luces was named here beside Camaras and is deliberately no longer: it is not
# proxied and cannot be as it stands -- `web_server/index.html` asks for
# `/theme.css` and fetches `/theme/version` absolutely, so behind a `/luces/`
# prefix both 404. It reaches the menu as an extension with an absolute URL,
# earns the badge honestly, and is dropped altogether from away. That half is
# pinned in test_house_only.py.
for link in A.CHAT_APP_LINKS:
    check(f"{link['name']} is a local path",
          not link['url'].startswith('http'), link['url'])
check('and Camaras is one of them',
      any(l['url'].startswith('/camaras') for l in A.CHAT_APP_LINKS),
      [l['url'] for l in A.CHAT_APP_LINKS])

_srv.shutdown()
print()
if failures:
    print(f'{len(failures)} FAILED: ' + ', '.join(failures))
    raise SystemExit(1)
print('all checks passed')

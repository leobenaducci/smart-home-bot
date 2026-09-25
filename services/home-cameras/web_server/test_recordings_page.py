#!/usr/bin/env python3
"""The recordings page, in a real browser.

Run: python web_server/test_recordings_page.py

This page decides what somebody sees and what they delete, and both of those
now depend on JavaScript that no Python test can reach: the filter runs over
the list already in hand, "seleccionar las que se ven" reads whatever that
filter left, and the tray's button deletes several hundred files at once.

So the page is rendered with Jinja exactly as Flask renders it, served next to
a stand-in API, and driven through the DevTools protocol. Checks are written
against what is on the screen — the number of cards, the words in them, the
request that leaves — because the last round of this suite checked a harness
that re-implemented the thing it was testing and had drifted from it.

Skips rather than fails when there is no Chromium here.
"""

import glob
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(label, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def find_chrome():
    for pattern in ('~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome',
                    '~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux*/*'):
        for path in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):
            if os.access(path, os.X_OK) and os.path.isfile(path):
                return path
    return shutil.which('chromium') or shutil.which('google-chrome')


try:
    from jinja2 import Environment, FileSystemLoader
    import websocket                                   # noqa: F401
except ImportError as e:
    print(f'SKIP: {e} — needs jinja2 and websocket-client.')
    raise SystemExit(0)

CHROME = find_chrome()
if not CHROME:
    print('SKIP: no Chromium here.')
    raise SystemExit(0)

# --- the page, rendered the way Flask renders it ------------------------------
env = Environment(loader=FileSystemLoader(os.path.join(HERE, 'templates')))
PAGE = env.get_template('recordings.html').render(base='')

# --- what the API says --------------------------------------------------------
# Shaped like the real thing, including the two that carry no `kind`: clips
# recorded before the reviewer named what it saw are most of what is on this
# house's shelf, and they must not be swept into some category by accident.
VIDEOS = [
    {'filename': '2026-08-08_10-00-00_Patio_person.mp4', 'path': '2026-08/a.mp4',
     'camera_ip': 'Patio', 'size': 2_000_000, 'created': 1_770_000_000,
     'why': 'a person in the garden', 'certainty': 'high', 'kind': 'person'},
    {'filename': '2026-08-08_10-05-00_Patio_dog.mp4', 'path': '2026-08/b.mp4',
     'camera_ip': 'Patio', 'size': 1_000_000, 'created': 1_770_000_100,
     'why': 'a dog runs past', 'certainty': 'medium', 'kind': 'animal'},
    {'filename': '2026-08-08_10-10-00_Front_car.mp4', 'path': '2026-08/c.mp4',
     'camera_ip': 'Front', 'size': 3_000_000, 'created': 1_770_000_200,
     'why': 'a car pulls in', 'certainty': 'high', 'kind': 'vehicle'},
    {'filename': '2026-08-08_10-15-00_Front.mp4', 'path': '2026-08/d.mp4',
     'camera_ip': 'Front', 'size': 500_000, 'created': 1_770_000_300,
     'why': 'something moves', 'certainty': 'low', 'kind': 'other'},
    {'filename': '2026-08-08_10-20-00_Front.mp4', 'path': '2026-08/e.mp4',
     'camera_ip': 'Front', 'size': 500_000, 'created': 1_770_000_400,
     'why': '', 'certainty': '', 'kind': ''},
    {'filename': '2026-08-08_10-25-00_Patio.mp4', 'path': '2026-08/f.mp4',
     'camera_ip': 'Patio', 'size': 500_000, 'created': 1_770_000_500,
     'why': '', 'certainty': '', 'kind': '', 'review_error': 'ollama answered 500'},
]
TRAY = [
    {'filename': '2026-08-08_09-00-00_Patio.mp4', 'rel_path': 'to_review/x.mp4',
     'size': 400_000, 'modified': 1_769_000_000, 'why': 'nada nuevo en la escena',
     'certainty': 'high', 'kind': 'static', 'error': None, 'expires_in_days': 12},
    {'filename': '2026-08-08_09-30-00_Patio.mp4', 'rel_path': 'to_review/y.mp4',
     'size': 400_000, 'modified': 1_769_000_100, 'why': 'cambió la luz',
     'certainty': 'medium', 'kind': 'light', 'error': None, 'expires_in_days': 2},
]

seen = []          # every request that reached the stand-in, in order


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send(self, body, ctype='application/json'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        seen.append(('POST', self.path))
        if self.path.endswith('/review/purge'):
            del TRAY[:]
            return self._send({'success': True, 'deleted': 2})
        self._send({'success': True})

    def do_DELETE(self):
        seen.append(('DELETE', self.path))
        self._send({'success': True})

    def do_GET(self):
        seen.append(('GET', self.path))
        path = self.path.split('?')[0]
        if path in ('/', '/recordings'):
            return self._send(PAGE.encode(), 'text/html; charset=utf-8')
        if path == '/api/cameras':
            return self._send({'Patio': {'name': 'Patio', 'device_ip': '10.0.0.2'},
                               'Front': {'name': 'Front', 'device_ip': '10.0.0.3'}})
        if path == '/api/recordings':
            want = 'snapshot' if 'type=snapshot' in self.path else 'video'
            return self._send({'recordings': [] if want == 'snapshot' else VIDEOS})
        if path == '/api/recordings/review':
            return self._send({'recordings': list(TRAY), 'max_age_days': 15,
                               'review': {'pending': 0}})
        self.send_response(404)
        self.send_header('Content-Length', '0')
        self.end_headers()


srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]

# --- the browser --------------------------------------------------------------
profile = tempfile.mkdtemp(prefix='cams-page-')
chrome = subprocess.Popen(
    [CHROME, '--headless=new', '--no-sandbox', '--disable-gpu',
     '--remote-debugging-port=0', '--remote-allow-origins=*',
     f'--user-data-dir={profile}', 'about:blank'],
    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

# The port is printed on stderr as "DevTools listening on ws://...". Read it
# rather than picking a port ourselves: a fixed one collides with whatever else
# on this box is already debugging something.
ws_url = None
deadline = time.time() + 30
while time.time() < deadline:
    line = chrome.stderr.readline().decode(errors='replace')
    if 'ws://' in line:
        ws_url = line.strip().split('ws://')[1]
        ws_url = 'ws://' + ws_url
        break
if not ws_url:
    chrome.kill()
    print('SKIP: Chromium did not start.')
    raise SystemExit(0)

devtools_port = ws_url.split('/')[2].split(':')[1]
# The about:blank tab Chromium was started with, rather than /json/new — that
# endpoint wants a PUT in current builds and answers 405 to anything else.
targets = json.loads(urllib.request.urlopen(
    f'http://127.0.0.1:{devtools_port}/json/list').read())
pages = [t for t in targets if t.get('type') == 'page']
if not pages:
    chrome.kill()
    print('SKIP: Chromium opened no page to drive.')
    raise SystemExit(0)
ws = websocket.create_connection(pages[0]['webSocketDebuggerUrl'], timeout=30)
_msg_id = [0]


def cdp(method, **params):
    _msg_id[0] += 1
    ws.send(json.dumps({'id': _msg_id[0], 'method': method, 'params': params}))
    while True:
        got = json.loads(ws.recv())
        if got.get('id') == _msg_id[0]:
            if 'error' in got:
                raise RuntimeError(got['error'])
            return got.get('result', {})


def js(expression):
    """Evaluate in the page and hand back the value. Promises are awaited, so a
    check can wait for the page's own fetches instead of sleeping and hoping."""
    out = cdp('Runtime.evaluate', expression=expression, awaitPromise=True,
              returnByValue=True)
    if out.get('exceptionDetails'):
        raise RuntimeError(json.dumps(out['exceptionDetails'])[:400])
    return out['result'].get('value')


SETTLE = """
new Promise(r => {
  const t = setInterval(() => {
    if (document.querySelectorAll('#recordings-container .card').length ||
        document.querySelector('#recordings-container .empty-state h3')) {
      clearInterval(t); r(true);
    }
  }, 25);
  setTimeout(() => { clearInterval(t); r(false); }, 10000);
})
"""

try:
    cdp('Page.enable')
    cdp('Runtime.enable')
    cdp('Page.navigate', url=f'http://127.0.0.1:{PORT}/recordings')
    ready = js(SETTLE)
    check('the page loads its recordings', ready is True, ready)

    cards = 'document.querySelectorAll("#recordings-container .card").length'
    check('every recording is there to begin with', js(cards) == len(VIDEOS), js(cards))
    check('and the count says so, without a second number',
          js('document.getElementById("showing").textContent') == '6 recordings',
          js('document.getElementById("showing").textContent'))

    print('\nthe filter answers "only the people"')
    pick = """
    (k => { const s = document.getElementById('filter-kind'); s.value = k;
            s.dispatchEvent(new Event('change')); return s.value; })
    """
    check('the option exists', js(f'{pick}("person")') == 'person')
    check('  one card left', js(cards) == 1, js(cards))
    check('  and it is the right one',
          'person' in js('document.querySelector("#recordings-container .verdict").textContent'),
          js('document.querySelector("#recordings-container .verdict").textContent'))
    check('  the count says what it hid',
          js('document.getElementById("showing").textContent') == '1 of 6 recordings',
          js('document.getElementById("showing").textContent'))
    check('  and the chip carries the same word',
          js('document.querySelector("#recordings-container .kind").textContent') == 'person',
          js('document.querySelector("#recordings-container .kind").textContent'))

    # `_alive` is the one that is not a single word in the verdict, and the
    # reason the mapping exists at all.
    for kind, n in (('animal', 1), ('vehicle', 1), ('_alive', 2), ('object', 0)):
        js(f'{pick}("{kind}")')
        check(f'  {kind}: {n}', js(cards) == n, js(cards))
    # Nothing matched is not the same as nothing recorded, and a page that says
    # the wrong one of those sends somebody looking for a broken camera.
    check('  and an empty filter says why it is empty',
          'Nothing matches that filter' in js(
              'document.querySelector("#recordings-container .empty-state h3").textContent'),
          js('document.querySelector("#recordings-container .empty-state h3").textContent'))

    print('\nthe two states, which are not subjects')
    js(f'{pick}("_unsure")')
    # The low-certainty one *and* the one nobody could review: both are clips
    # worth a person's eye, which is the whole point of the option.
    check('  poco seguro finds both', js(cards) == 2, js(cards))
    js(f'{pick}("_unclassified")')
    check('  sin clasificar finds the two with no verdict', js(cards) == 2, js(cards))
    check('  and they are not given a chip they never earned',
          js('document.querySelectorAll("#recordings-container .kind").length') == 0,
          js('document.querySelectorAll("#recordings-container .kind").length'))

    print('\nselecting what is on the screen, which is how a filtered list is binned')
    js(f'{pick}("vehicle")')
    js('document.getElementById("select-shown").click()')
    check('  the bar appears',
          js('document.getElementById("selected-bar").classList.contains("show")'))
    check('  with only the filtered one in it',
          js('document.getElementById("selected-count").textContent') == '1',
          js('document.getElementById("selected-count").textContent'))
    # The bug this is really about: selecting "todo" while a filter is on used
    # to be the only route to a bulk delete, and it was hidden behind a bar
    # that only appears once something is selected.
    js(f'{pick}("")')
    js('clearSelection()')
    js('document.getElementById("select-shown").click()')
    check('  and all six with the filter off',
          js('document.getElementById("selected-count").textContent') == '6',
          js('document.getElementById("selected-count").textContent'))

    print('\nthe tray, and its one button')
    check('  it says how many it will take',
          js('document.getElementById("review-purge").textContent.trim()') == 'Delete all 2',
          js('document.getElementById("review-purge").textContent.trim()'))
    check('  the tray cards carry their word too',
          js('document.querySelector("#review-container .kind").textContent') == 'no change',
          js('document.querySelector("#review-container .kind").textContent'))
    check('  and still say when they expire',
          js('document.querySelector("#review-container .expiry-slot").textContent')
          == '12 days left',
          js('document.querySelector("#review-container .expiry-slot").textContent'))

    del seen[:]
    js('window.confirm = () => true; document.getElementById("review-purge").click()')
    js('new Promise(r => setTimeout(r, 700))')
    posts = [p for m, p in seen if m == 'POST']
    check('  pressing it asks the server to empty the tray',
          posts == ['/api/recordings/review/purge'], posts)
    check('  and the tray goes away when it is empty',
          js('document.getElementById("review-section").style.display') == 'none',
          js('document.getElementById("review-section").style.display'))
    check('  taking its button with it',
          js('document.getElementById("review-purge").style.display') == 'none',
          js('document.getElementById("review-purge").style.display'))

    print('\nan error page from an expired session is read as words, not as JSON')
    # `r.json()` on a login page throws a SyntaxError about a character
    # position, which is what this house saw for weeks whenever a delete failed.
    msg = js("""
    (async () => {
      const fake = new Response('<!doctype html><h1>Log in</h1>',
                                {status: 403, headers: {'Content-Type': 'text/html'}});
      try { await reviewResult(fake); return 'no error'; }
      catch (e) { return e.message; }
    })()
    """)
    check('  it says the server refused', '403' in str(msg), msg)
    check('  and not "unexpected token"', 'token' not in str(msg).lower(), msg)

finally:
    try:
        ws.close()
    except Exception:
        pass
    chrome.terminate()
    try:
        chrome.wait(timeout=10)
    except subprocess.TimeoutExpired:
        chrome.kill()
    srv.shutdown()
    shutil.rmtree(profile, ignore_errors=True)

print()
if failures:
    print(f'{len(failures)} FAILED: ' + ', '.join(failures))
    sys.exit(1)
print('all checks passed')

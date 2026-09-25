"""The 📥 button in the chat, and what it does when the probe is unsure.

Run: python3 test_download_button.py   (needs node, like test_templates_parse.py)

A document that existed on the share -- served 200 to the Files panel the same
minute -- showed «no está» in the chat and never reached the server at all: no
HEAD in the container's log, ever. The probe in `markDownloadBroken` stands in
front of the download, so anything that answers it wrongly takes a working file
with it, permanently, because the chip replaces the anchor in the DOM.

What is pinned here is that the check fails *open*. Only our own fresh 404 may
replace a link; every other answer -- a 500, a 403, a redirect, an opaque
response, a fetch that throws -- means "I could not tell", and the honest
response to that is to let the download happen.
"""

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'templates', 'chat.html')

if not subprocess.run(['which', 'node'], capture_output=True).returncode == 0:
    print("SKIP: node is not installed here — this checks browser behaviour.")
    raise SystemExit(0)

failures = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + ("" if cond else f"   <- {detail!r}"))
    if not cond:
        failures.append(label)


def grab(source, name):
    """One `function name(...) {...}` out of the template, brace-matched."""
    i = source.index('function %s(' % name)
    depth, k = 0, source.index('{', i)
    while True:
        if source[k] == '{':
            depth += 1
        elif source[k] == '}':
            depth -= 1
            if depth == 0:
                break
        k += 1
    return source[i:k + 1]


page = open(SRC, encoding='utf-8').read()

HARNESS = r'''
// A DOM small enough to answer the only questions asked here.
var replaced = null, clicks = 0, fetched = null;
function makeAnchor() {
  return {
    href: '/chat/download/tomi/alfred/documents/documento.html?dl=1',
    dataset: {}, _click: null,
    addEventListener: function (_, fn) { this._click = fn; },
    click: function () { clicks++; this._click({ preventDefault: function () {} }); },
    parentNode: { replaceChild: function (chip) { replaced = chip; } }
  };
}
document = { createElement: function () { return { style: {}, dataset: {} }; } };

function run(reply) {
  replaced = null; clicks = 0; fetched = null;
  fetch = function (url, opts) {
    fetched = { url: url, opts: opts };
    return reply();
  };
  var a = makeAnchor();
  markDownloadBroken(a, 'tomi/alfred/documents/documento.html');
  a.click();
  return new Promise(function (res) { setTimeout(function () {
    res({ replaced: replaced !== null, clicks: clicks, fetched: fetched });
  }, 0); });
}

var CASES = {
  ok:       function () { return Promise.resolve({ ok: true,  status: 200 }); },
  missing:  function () { return Promise.resolve({ ok: false, status: 404 }); },
  denied:   function () { return Promise.resolve({ ok: false, status: 403 }); },
  broken:   function () { return Promise.resolve({ ok: false, status: 500 }); },
  opaque:   function () { return Promise.resolve({ ok: false, status: 0   }); },
  threw:    function () { return Promise.reject(new Error('blocked')); }
};

(async function () {
  var out = {};
  for (var name in CASES) out[name] = await run(CASES[name]);
  console.log(JSON.stringify(out));
})();
'''

script = "\n".join([grab(page, 'markDownloadBroken'), HARNESS])
proc = subprocess.run(['node', '-e', script], capture_output=True, text=True)
if proc.returncode != 0:
    print("node failed:\n" + proc.stderr[:600])
    raise SystemExit(1)
r = json.loads(proc.stdout.strip().splitlines()[-1])

print("a file that is there downloads")
check("  the click goes through", r['ok']['clicks'] == 2, r['ok'])
check("  and nothing is replaced", not r['ok']['replaced'])

print("\nonly our own 404 may say «no está»")
check("  a 404 replaces the link", r['missing']['replaced'], r['missing'])
check("  and does not download", r['missing']['clicks'] == 1, r['missing'])

print("\neverything else fails open — 'I could not tell' is not 'it is gone'")
for name, label in (('denied', 'a 403'), ('broken', 'a 500'),
                    ('opaque', 'an opaque response'), ('threw', 'a fetch that throws')):
    check(f"  {label} still downloads", r[name]['clicks'] == 2, r[name])
    check(f"    and leaves the link alone", not r[name]['replaced'], r[name])

print("\nthe probe cannot be answered from cache")
check("  it asks for no-store",
      (r['ok']['fetched'] or {}).get('opts', {}).get('cache') == 'no-store',
      r['ok']['fetched'])
check("  by HEAD, with the session",
      (r['ok']['fetched'] or {}).get('opts', {}).get('method') == 'HEAD'
      and (r['ok']['fetched'] or {}).get('opts', {}).get('credentials') == 'same-origin')

print("\nand the button asks to save, like the one in Files")
check("  the probed url carries ?dl=1",
      '?dl=1' in (r['ok']['fetched'] or {}).get('url', ''),
      (r['ok']['fetched'] or {}).get('url'))

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all checks passed")

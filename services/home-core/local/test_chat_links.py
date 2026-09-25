"""An address the assistant writes has to be one tap.

The Programmer offered a local preview at `**http://192.168.88.35:8123/**` and
the person had to retype it into a browser: the chat only ever linkified
markdown `[text](url)`, and bold matched before anything else, so the one thing
on screen that needed tapping was the one thing left plain.

Two fixes, and both are easy to undo by accident:

* the bare-URL branch must stay **last** in the alternation, or it starts eating
  the insides of markdown links;
* bold must **recurse** into the renderer rather than setting `textContent`, or
  an address inside `**…**` goes back to being plain text -- and the recursion
  needs a regex per call, because a shared global one carries `lastIndex` into
  the nested call and the outer loop resumes at the wrong offset, silently
  dropping the rest of the message.

So this lifts the real function out of the template and runs it in node against
a small DOM stub, rather than asserting on the source. Run:
python local/test_chat_links.py   (needs node; skips loudly without)
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CHAT = Path(__file__).resolve().parent / "templates" / "chat.html"

if not shutil.which("node"):
    print("SKIP: no node here — this test needs a JavaScript engine.")
    raise SystemExit(0)

src = CHAT.read_text(encoding="utf-8")

rx = re.search(r"var INLINE_RE = (/.*?/g);", src, re.S)
fn = re.search(r"  function appendInlineMarkdown\(parent, text\) \{.*?\n  \}\n", src, re.S)
if not rx or not fn:
    print("FAIL  the inline renderer is not where this test looks for it; "
          "chat.html changed shape and these checks are blind")
    raise SystemExit(1)

body = fn.group(0)
for call in ("markImageBroken(imgEl, imgTarget);",
             "markImageBroken(img2, linkTarget.replace(/^download:/, ''));",
             "markDownloadBroken(a, linkTarget.replace(/^download:/, ''));",
             "imgEl.addEventListener('click', (function(s){return function(){"
             "openLightbox(s);};})(imgSrc));",
             "img2.addEventListener('click', (function(s){return function(){"
             "openLightbox(s);};})(href));"):
    body = body.replace(call, "")
body = body.replace("document.createTextNode(", "textNode(")
body = body.replace("document.createElement(", "El(")

CASES = [
    # text, must contain, must not contain
    ("Mira http://192.168.88.35:8123/ y me dices.",
     ["<a href=http://192.168.88.35:8123/>"], []),
    # The one that was actually broken.
    ("La vista previa: **http://a.b:8123/** — avísame.",
     ["<a href=http://a.b:8123/>"], []),
    # Markdown links keep working: the bare branch must not eat their insides.
    ("[Abrir](http://192.168.88.35:8123/) para verlo",
     ["<a href=http://192.168.88.35:8123/>Abrir</a>"], []),
    # A full stop ends the sentence, not the address.
    ("Ve a http://a.b/x. Luego vuelve",
     ["<a href=http://a.b/x>http://a.b/x</a>."], ["x.</a>"]),
    # Balanced brackets belong to the address.
    ("https://es.wikipedia.org/wiki/Fracción_(matemáticas) aquí",
     ["Fracción_(matemáticas)</a>"], []),
    # An unbalanced one does not.
    ("(ver http://a.b/x) y ya", ["<a href=http://a.b/x>http://a.b/x</a>)"], []),
    # Code spans are quoted text, not links.
    ("una `http://a.b/x` en código", ["`http://a.b/x`"], ["<a href"]),
    # Ordinary bold still renders, and the text after it is not lost -- which is
    # what a shared regex's lastIndex would do once bold recursed.
    ("**negrita** y el resto del mensaje",
     ["**negrita**", "y el resto del mensaje"], []),
    # Nothing to do.
    ("sin nada que enlazar", ["sin nada que enlazar"], ["<a href"]),
    # Not a scheme anybody should be sent to.
    ("javascript:alert(1) no es un enlace", [], ["<a href"]),
]

script = """
%s
function sanitizeHref(u){ return /^https?:\\/\\//.test(u) ? u : ''; }
function El(t){ return { t:t, kids:[], textContent:'', href:'',
                         appendChild(c){ this.kids.push(c); } }; }
function textNode(s){ return { t:'#text', textContent:s, kids:[] }; }
%s
function show(n){
  if (n.t === '#text') return n.textContent;
  const inner = n.kids.map(show).join('') || n.textContent;
  if (n.t === 'a') return '<a href=' + n.href + '>' + inner + '</a>';
  if (n.t === 'strong') return '**' + inner + '**';
  if (n.t === 'code') return '`' + inner + '`';
  return inner;
}
const out = [];
for (const [text] of %s) {
  const p = El('div');
  appendInlineMarkdown(p, text);
  out.push(p.kids.map(show).join(''));
}
console.log(JSON.stringify(out));
""" % (f"const INLINE_RE = {rx.group(1)};", body, json.dumps([[c[0]] for c in CASES]))

with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                 encoding="utf-8") as fh:
    fh.write(script)
    path = fh.name
try:
    run = subprocess.run(["node", path], capture_output=True, text=True)
finally:
    Path(path).unlink(missing_ok=True)

if run.returncode != 0:
    print("FAIL  the renderer threw:\n" + (run.stderr or "")[-1500:])
    raise SystemExit(1)

rendered = json.loads(run.stdout)
failures = []
for (text, wants, nots), got in zip(CASES, rendered):
    bad = ([w for w in wants if w not in got]
           + [f"(unwanted) {n}" for n in nots if n in got])
    if bad:
        failures.append((text, got, bad))
        print(f"  FAIL  {text!r}\n          got: {got}\n          missing: {bad}")
    else:
        print(f"  PASS  {text!r}")

print()
if failures:
    print(f"{len(failures)} case(s) failed")
    sys.exit(1)
print("all checks passed")

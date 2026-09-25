/* Fenced code blocks in the chat: the panel, the colours and the copy button.
   Run: node test_code_block.js   (from services/home-core/local/)

   The assistant hands out commands and files all day -- the programmer persona
   most, but the teacher with a script and the designer with their HTML too --
   and before this they arrived as prose with backticks in them: wrapped at the
   bubble's edge, indentation gone, impossible to paste anywhere.

   The sample code stays Spanish on purpose. It is what exercises the
   highlighter on non-ASCII -- an accented word in a comment, an "si" with an
   accent inside a string -- and translating the fixtures away would drop the
   only case in this file that is not plain ASCII.

   Two things matter here and they pull in opposite directions. The block has to
   be *exact*: what comes out of the panel, and out of the copy button, is what
   the model wrote, character for character, or it is not a command any more.
   And it is still untrusted text — a code block is where the least trustworthy
   text in the page lives, which is the whole reason it is a code block — so it
   goes in as text nodes and never as markup. Same lift-the-real-source approach
   as test_ask_block.js. */
'use strict';
const fs = require('fs');
const path = require('path');

const HTML = fs.readFileSync(path.join(__dirname, 'templates', 'chat.html'), 'utf8');

function lift(signature) {
  const at = HTML.indexOf(signature);
  if (at < 0) throw new Error(`not found in chat.html: ${signature}`);
  let i = HTML.indexOf('{', at), depth = 0;
  for (let j = i; j < HTML.length; j++) {
    if (HTML[j] === '{') depth++;
    else if (HTML[j] === '}' && --depth === 0) return HTML.slice(at, j + 1);
  }
  throw new Error(`unbalanced braces after ${signature}`);
}

function liftLine(re) {
  const m = re.exec(HTML);
  if (!m) throw new Error(`not found in chat.html: ${re}`);
  return m[0];
}

const SOURCE = [
  lift('var CODE_ALIASES = '),
  liftLine(/var CODE_HASH = .*/),
  liftLine(/var CODE_SLASH = .*/),
  liftLine(/var CODE_DASH = .*/),
  liftLine(/var CODE_SQ = .*/),
  liftLine(/var CODE_DQ = .*/),
  liftLine(/var CODE_BQ = .*/),
  liftLine(/var CODE_PY3 = .*/),
  lift('var CODE_GRAMMARS = '),
  liftLine(/var CODE_RES = .*/),
  lift('function codeGrammarFor(lang)'),
  lift('function codeTokenRe(name)'),
  lift('function highlightInto(parent, source, grammarName)'),
  lift('function findCodeBlocks(text)'),
  lift('function copyCode(source, btn)'),
  lift('function buildCodeEl(lang, source)'),
  liftLine(/var FENCED_UI_RE = .*/),
  lift('function unwrapFencedUiBlocks(text)'),
].join('\n');

let failures = 0;
function check(label, cond, detail) {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}` +
              (cond ? '' : `  <- ${JSON.stringify(detail)}`));
  if (!cond) failures++;
}

/* Just enough DOM for what buildCodeEl touches. Nothing here has innerHTML, on
   purpose: if the page ever reaches for it this harness stops working, which is
   the point. */
function makeDom() {
  const listeners = [];
  function el(tag) {
    const node = {
      tagName: tag, className: '', type: '', _text: '', children: [], classes: new Set(),
      style: { cssText: '' },
      set textContent(v) { this._text = String(v); this.children = []; },
      get textContent() { return this._text; },
      appendChild(c) { this.children.push(c); return c; },
      removeChild(c) { this.children = this.children.filter(x => x !== c); },
      setAttribute() {}, select() {}, setSelectionRange() {},
      addEventListener(ev, fn) { listeners.push({ node: this, ev, fn }); },
    };
    node.classList = {
      add: (c) => { node.classes.add(c); node.className += ' ' + c; },
      remove: (c) => { node.classes.delete(c); },
      contains: (c) => node.classes.has(c),
    };
    return node;
  }
  const body = el('body');
  return {
    listeners,
    document: {
      body,
      createElement: el,
      createTextNode: (t) => ({ tagName: '#text', _text: t, textContent: t, children: [] }),
      execCommand: () => true,
    },
  };
}

function run(expr, opts) {
  opts = opts || {};
  const dom = makeDom();
  const copied = [];
  const page = {
    document: dom.document,
    navigator: opts.noClipboard ? {} : {
      clipboard: { writeText: (t) => { copied.push(t); return Promise.resolve(); } },
    },
  };
  const names = ['document', 'navigator'];
  const fn = new Function(...names, `${SOURCE}\nreturn (${expr});`);
  return { dom, copied, value: fn(...names.map((n) => page[n])) };
}

/* Everything the panel shows, in order, whatever nesting it is under: the one
   invariant a highlighter must never break. */
function flatText(node) {
  if (!node) return '';
  if (node.tagName === '#text') return node._text;
  if (node.children && node.children.length) return node.children.map(flatText).join('');
  return node._text || '';
}
function findByClass(node, cls) {
  const out = [];
  (function walk(n) {
    (n.children || []).forEach((c) => {
      if (String(c.className).split(/\s+/).includes(cls)) out.push(c);
      walk(c);
    });
  })(node);
  return out;
}

const PY = [
  '```python',
  '# suma dos números',
  'def suma(a, b):',
  '    return a + b   # 42',
  'print(suma(1, 2))',
  '```',
].join('\n');

console.log('a fence is found, and the fence lines are not part of it');
let r = run(`findCodeBlocks(${JSON.stringify('Mira:\n\n' + PY + '\n\nEso.')})`);
let blocks = r.value;
check('one block', blocks.length === 1, blocks.length);
check('the language is read', blocks[0].lang === 'python', blocks[0].lang);
check('the body is the code alone',
      blocks[0].body === PY.split('\n').slice(1, -1).join('\n'), blocks[0].body);
check('what follows the block is left to the rest of the renderer',
      ('Mira:\n\n' + PY + '\n\nEso.').slice(blocks[0].end) === '\nEso.',
      ('Mira:\n\n' + PY + '\n\nEso.').slice(blocks[0].end));

console.log('\na fence nobody closed still draws — a stopped turn is kept and shown');
r = run(`findCodeBlocks(${JSON.stringify('Corre esto:\n```bash\ndocker ps\nsystemctl restart')})`);
check('it runs to the end of the message',
      r.value.length === 1 && r.value[0].body === 'docker ps\nsystemctl restart',
      r.value.map((b) => b.body));

console.log('\nthe panel says what it is and offers the copy');
r = run(`buildCodeEl('python', ${JSON.stringify('print(1)')})`);
let el = r.value;
check('a code-block', el.className.includes('code-block'), el.className);
check('labelled with its language',
      findByClass(el, 'code-lang')[0].textContent === 'python');
check('with a copy button', findByClass(el, 'code-copy')[0].textContent === 'Copy');
check('the code sits in a pre', findByClass(el, 'code-body').length === 1);
r = run(`buildCodeEl('', ${JSON.stringify('cualquier cosa')})`);
check('a fence with no language is still labelled',
      findByClass(r.value, 'code-lang')[0].textContent === 'code');

console.log('\nnothing is lost, added or turned into markup');
const NASTY = '<script>alert(1)</script>\n<b>&amp;</b>';
r = run(`buildCodeEl('html', ${JSON.stringify(NASTY)})`);
check('the source comes out character for character', flatText(r.value).includes(NASTY),
      flatText(r.value));
r = run(`buildCodeEl('python', ${JSON.stringify(PY.split('\n').slice(1, -1).join('\n'))})`);
check('including the highlighted one',
      flatText(findByClass(r.value, 'code-body')[0]) === PY.split('\n').slice(1, -1).join('\n'),
      flatText(findByClass(r.value, 'code-body')[0]));

console.log('\nkeywords, strings, comments and numbers are picked out');
function tokens(lang, src) {
  const out = run(`buildCodeEl(${JSON.stringify(lang)}, ${JSON.stringify(src)})`);
  const body = findByClass(out.value, 'code-body')[0];
  const pick = (cls) => findByClass(body, cls).map((n) => n.textContent);
  return { kw: pick('c-kw'), str: pick('c-str'), com: pick('c-com'), num: pick('c-num') };
}
let t = tokens('python', PY.split('\n').slice(1, -1).join('\n'));
check('the keyword', t.kw.includes('def') && t.kw.includes('return'), t.kw);
check('the comments', t.com.length === 2 && t.com[0] === '# suma dos números', t.com);
check('the numbers', t.num.includes('1') && t.num.includes('2'), t.num);
t = tokens('bash', 'echo "no # es un comentario"   # este sí');
check('a # inside a string stays string',
      t.str.length === 1 && t.str[0] === '"no # es un comentario"', t.str);
check('and the one outside is the comment', t.com.length === 1 && t.com[0] === '# este sí', t.com);
t = tokens('sql', 'SELECT nombre FROM socios WHERE id = 3;');
check('SQL is the same word in either case',
      t.kw.includes('SELECT') && t.kw.includes('FROM'), t.kw);
t = tokens('brainfuck', "algo 'entre comillas' # y un comentario");
check('an unknown language still gets its strings and comments',
      t.str.length === 1 && t.com.length === 1 && t.kw.length === 0, t);

/* The rest is a tick late on purpose: the clipboard API answers with a promise,
   so what the button says about it cannot be read in the same breath as the
   tap. */
(async function () {
  console.log('\nthe button copies the source, not what the screen shows');
  const CMD = 'sudo systemctl restart mosquitto   # tira las luces\n';
  let c = run(`buildCodeEl('bash', ${JSON.stringify(CMD)})`);
  const btn = findByClass(c.value, 'code-copy')[0];
  c.dom.listeners.find((l) => l.node === btn).fn();
  await Promise.resolve();
  check('exactly what was written, whitespace and all',
        c.copied.length === 1 && c.copied[0] === CMD, c.copied);
  check('and it says so', btn.textContent === '\u2713 Copied', btn.textContent);

  console.log('\nno clipboard API, no lost copy — the textarea fallback runs');
  c = run(`buildCodeEl('bash', ${JSON.stringify('ls -la')})`, { noClipboard: true });
  const btn2 = findByClass(c.value, 'code-copy')[0];
  c.dom.listeners.find((l) => l.node === btn2).fn();
  check('it still reports a copy', btn2.textContent === '\u2713 Copied', btn2.textContent);
  check('and it cleans up after itself', c.dom.document.body.children.length === 0,
        c.dom.document.body.children.length);

  console.log('\na :::goto in a bare fence is a button, not a listing');
  const FENCED_GOTO = 'Te contesto acá.\n\n```\n:::goto\nspace: programador\nlabel: Abrir\n:::\n```';
  const unwrapped = run(`unwrapFencedUiBlocks(${JSON.stringify(FENCED_GOTO)})`).value;
  check('the fence lines are gone', unwrapped.indexOf('```') === -1, unwrapped);
  check('the block itself is untouched', unwrapped.indexOf(':::goto\nspace: programador') !== -1,
        unwrapped);
  check('so nothing there reads as code',
        run(`findCodeBlocks(${JSON.stringify(unwrapped)})`).value.length === 0);
  check('a real fence is left alone',
        run(`unwrapFencedUiBlocks(${JSON.stringify(PY)})`).value === PY);

  console.log(failures ? `\n${failures} FAILED` : '\nall good');
  process.exit(failures ? 1 : 0);
})();

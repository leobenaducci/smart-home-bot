/* Alfred asking back with the answers already written (a `:::ask` block).
   Run: node local/test_ask_block.js

   A vague request has two bad endings: guess and produce the wrong thing, or ask
   and make someone type a paragraph on a phone. This is the third — the question
   arrives with its likely answers, and answering is one tap.

   What matters here is that the block is *data*, not behaviour. The model
   chooses the words; it must not be able to choose what a tap does. Everything
   goes in as text, the only effect of a tap is to send that text as an ordinary
   user message, and a malformed block draws nothing rather than a control that
   cannot be got past. Same lift-the-real-source approach as test_catchup.js. */
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

/* The regex and the cap are declarations, not functions — take them verbatim
   too, so a change to either is seen here. */
function liftLine(re) {
  const m = re.exec(HTML);
  if (!m) throw new Error(`not found in chat.html: ${re}`);
  return m[0];
}

const SOURCE = [
  liftLine(/var ASK_RE = .*/),
  liftLine(/var ASK_MAX_OPTIONS = .*/),
  lift('function parseAskBlock(body)'),
  lift('function buildAskEl(fields)'),
  liftLine(/var SINO_RE = .*/),
  lift('function parseYesNoBlock(body)'),
].join('\n');

let failures = 0;
function check(label, cond, detail) {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}` +
              (cond ? '' : `  <- ${JSON.stringify(detail)}`));
  if (!cond) failures++;
}

/* Just enough DOM for what buildAskEl touches. */
function makeDom() {
  const listeners = [];
  function el(tag) {
    const node = {
      tagName: tag, className: '', type: '', disabled: false,
      _text: '', children: [], classes: new Set(),
      set textContent(v) { this._text = String(v); },
      get textContent() { return this._text; },
      appendChild(c) { this.children.push(c); return c; },
      addEventListener(ev, fn) { listeners.push({ node: this, ev, fn }); },
      querySelectorAll(sel) {
        const want = sel.replace('.', '');
        const out = [];
        (function walk(n) {
          n.children.forEach(c => {
            if (String(c.className).split(/\s+/).includes(want)) out.push(c);
            walk(c);
          });
        })(this);
        return out;
      },
    };
    node.classList = {
      add: (c) => { node.classes.add(c); node.className += ' ' + c; },
      contains: (c) => node.classes.has(c),
    };
    return node;
  }
  return {
    listeners,
    document: { createElement: el, createTextNode: (t) => ({ tagName: '#text', _text: t, children: [] }) },
  };
}

function build(body, opts) {
  opts = opts || {};
  const dom = makeDom();
  const page = {
    document: dom.document,
    busy: !!opts.busy,
    inputEl: { value: '', focus() { page.focused = true; } },
    autoResize() {},
    sent: [],
    focused: false,
  };
  page.send = () => { page.sent.push(page.inputEl.value); };
  const names = ['document', 'busy', 'inputEl', 'autoResize', 'send'];
  const fn = new Function(...names, 'Array',
    `${SOURCE}\nreturn { parsed: parseAskBlock(${JSON.stringify(body)}),
                          el: buildAskEl(parseAskBlock(${JSON.stringify(body)})) };`);
  const out = fn(...names.map(n => page[n]), Array);
  return { page, dom, ...out };
}

function optionsOf(el) {
  const found = [];
  (function walk(n) {
    (n.children || []).forEach(c => {
      // Exact class, not substring: the container is `ask-opts`.
      if (String(c.className).split(/\s+/).includes('ask-opt')) found.push(c);
      walk(c);
    });
  })(el);
  return found;
}

const BODY = '  q: ¿Para qué curso es la guía?\n  - 3° básico\n  - 5° básico\n  - 7° básico';

console.log('the block is parsed into a question and its answers');
let r = build(BODY);
check('the question is read', r.parsed.q === '¿Para qué curso es la guía?', r.parsed.q);
check('so are the options', r.parsed.opts.length === 3 && r.parsed.opts[0] === '3° básico',
      r.parsed.opts);
check('bullets may be -, * or •',
      build('q: X\n* uno\n• dos\n- tres').parsed.opts.length === 3);
check('"pregunta:" works as well as "q:"',
      build('pregunta: ¿Cuál?\n- a').parsed.q === '¿Cuál?');

console.log('\nit is drawn as tappable options plus an escape');
r = build(BODY);
let opts = optionsOf(r.el);
check('one button per option, plus the escape hatch', opts.length === 4, opts.length);
check('the escape is last and is not an answer',
      String(opts[3].className).includes('ask-other') && /Something else/.test(opts[3].textContent),
      opts.map(o => o.textContent));
check('labels go in as text, never as markup',
      opts[0].textContent === '3° básico', opts[0].textContent);

console.log('\ntapping answers the question as if it had been typed');
r = build(BODY);
opts = optionsOf(r.el);
r.dom.listeners.find(l => l.node === opts[1]).fn();
check('the option is sent as an ordinary message',
      r.page.sent.length === 1 && r.page.sent[0] === '5° básico', r.page.sent);
check('the card is marked answered', r.el.classList.contains('spent'));
check('and the other options stop inviting a tap',
      opts[0].disabled && opts[2].disabled, opts.map(o => o.disabled));

console.log('\nthe escape hatch just opens the keyboard');
r = build(BODY);
opts = optionsOf(r.el);
r.dom.listeners.find(l => l.node === opts[3]).fn();
check('nothing is sent', r.page.sent.length === 0, r.page.sent);
check('the input takes focus', r.page.focused === true);

console.log('\nit cannot be tapped into a mess');
r = build(BODY, { busy: true });
opts = optionsOf(r.el);
r.dom.listeners.find(l => l.node === opts[0]).fn();
check('mid-turn a tap does nothing', r.page.sent.length === 0, r.page.sent);

r = build(BODY);
opts = optionsOf(r.el);
const first = r.dom.listeners.find(l => l.node === opts[0]).fn;
first(); first();
check('tapping twice sends one answer, not two', r.page.sent.length === 1, r.page.sent);

console.log('\na malformed block draws nothing rather than a dead control');
check('no options at all', build('q: ¿Y?').el.tagName === '#text');
check('an empty body', build('').el.tagName === '#text');
check('a question is optional when there are options',
      optionsOf(build('- sí\n- no').el).length === 3);

console.log('\nthe number of options is capped');
const many = 'q: X\n' + Array.from({ length: 12 }, (_, i) => `- op${i}`).join('\n');
check('at ASK_MAX_OPTIONS', build(many).parsed.opts.length === 6, build(many).parsed.opts.length);

/* The commonest ask is a plain yes or no, and writing it as a full :::ask means
   typing the two answers everybody already knows. `:::yes-no` is that shorthand
   and must be nothing more — same card, same spent-once rule, same escape. */
console.log('\nthe yes/no shorthand');
// Same lifted source and same fake page as build(), only entered through
// parseYesNoBlock — the point being that it ends up in buildAskEl either way.
function sino(body, opts = {}) {
  const dom = makeDom();
  const page = {
    document: dom.document,
    busy: !!opts.busy,
    inputEl: { value: '', focus() { page.focused = true; } },
    autoResize() {},
    sent: [],
    focused: false,
  };
  page.send = () => { page.sent.push(page.inputEl.value); };
  const names = ['document', 'busy', 'inputEl', 'autoResize', 'send'];
  const fn = new Function(...names, 'Array',
    `${SOURCE}\nreturn { parsed: parseYesNoBlock(${JSON.stringify(body)}),
                          el: buildAskEl(parseYesNoBlock(${JSON.stringify(body)})),
                          RE: SINO_RE };`);
  const out = fn(...names.map(n => page[n]), Array);
  return { page, dom, ...out };
}
check('the face is a check and a cross',
      sino('q: ¿Le aviso a Sam?').parsed.opts.join('|') === '✅|❌');
// What it *sends* is still a word: the transcript stays a conversation, and
// Alfred answers "Yes" rather than having to read an emoji.
check('but what it sends is still Yes / No',
      sino('q: Shall I tell Sam?').parsed.values.join('|') === 'Yes|No');
check('and tapping one sends the word, not the symbol',
      (function () {
        const t = sino('q: ¿Vamos?');
        const opts = optionsOf(t.el);
        t.dom.listeners.find(l => l.node === opts[0]).fn();
        t.dom.listeners.find(l => l.node === opts[1]).fn();   // spent: ignored
        return t.page.sent.join('|') === 'Yes';
      })(), 'the emoji would reach Alfred as the answer');
check('the cross sends No',
      (function () {
        const t = sino('q: ¿Vamos?');
        t.dom.listeners.find(l => l.node === optionsOf(t.el)[1]).fn();
        return t.page.sent.join('') === 'No';
      })());
check('and keeps the question', sino('q: ¿Le aviso a Sam?').parsed.q === '¿Le aviso a Sam?');
// Repeating the question in the prose and again inside the fence reads as a
// stutter, so a bare line counts as the question too.
check('a bare line is the question', sino('¿Lo agrego a la lista?').parsed.q === '¿Lo agrego a la lista?');
// An empty fence is the case where he asked in the prose just above. Buttons
// still have to appear — they are the whole point — with no question of their own.
check('an empty fence still draws the buttons',
      optionsOf(sino('').el).length === 3, optionsOf(sino('').el).length);
check('it builds the same card as :::ask', sino('q: ¿Sí?').el.className.includes('ask-card'));
check('so it gets the escape hatch too',
      optionsOf(sino('q: Shall I?').el).some(o => o._text.includes('Something else')));

console.log('\nand the block matches the way a model actually writes it');
function firstMatch(text) {
  const re = sino('').RE;
  re.lastIndex = 0;
  const m = re.exec(text);
  return m ? m[0] : null;
}
check('with a q: line', !!firstMatch(':::yes-no\nq: ¿Vamos?\n:::'));
check('with the hyphen spelled out', !!firstMatch(':::sino\nq: ¿Vamos?\n:::'));
check('empty', !!firstMatch(':::yes-no\n:::'));
check('and it does not swallow the rest of the message',
      firstMatch(':::yes-no\nq: ¿Vamos?\n:::\nY otra cosa.').indexOf('otra cosa') < 0);

console.log();
if (failures) { console.log(`${failures} FAILED`); process.exit(1); }
console.log('all checks passed');

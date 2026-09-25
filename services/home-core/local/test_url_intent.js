/* A URL that says what to do on arrival must say it exactly once.
   Run: node local/test_url_intent.js

   Reported from the house, 2026-08-18: open Alfred on a new chat, say hello,
   open «Consumo de Alfred», press back — and the chat is showing August 15th.

   Nothing was pinned and nothing was cached. The app was sitting on the history
   entry `/chat?date=2026-08-15`, from a notification tap days earlier. Starting
   a new chat happens in-page and does not change the URL, so pressing back
   restored that entry and the deep link ran again, exactly as it had the first
   time. It would have kept happening for as long as that entry stayed in the
   back stack.

   `?new=1` already knew this — it deletes itself, with a comment about a reload
   starting yet another conversation — but the reasoning was never applied to
   `date`, `welcome` or `prefill`. All four are instructions for an arrival, not
   properties of the page.

   `embed` is the exception and the reason this is a list rather than "drop the
   query string": it is what the app *is*, not something it asked for once. */
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

const SOURCE = lift('function forgetUrlIntent()');

let failures = 0;
function check(label, cond, detail) {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}` +
              (cond ? '' : `  <- ${JSON.stringify(detail)}`));
  if (!cond) failures++;
}

/* The page, reduced to the two things this touches. */
function forget(search, pathname = '/chat') {
  const calls = [];
  const page = {
    window: { location: { search, pathname } },
    history: { replaceState: (a, b, url) => calls.push(url) },
    URLSearchParams,
  };
  const names = Object.keys(page);
  // eslint-disable-next-line no-new-func
  const run = new Function(...names, `${SOURCE}\nforgetUrlIntent();`);
  run(...names.map(n => page[n]));
  return calls.length ? calls[calls.length - 1] : null;
}

console.log('an instruction for an arrival is used once and forgotten');
check('the deep link that caused this', forget('?date=2026-08-15') === '/chat');
check('a launch asking for a clean sheet', forget('?new=1') === '/chat');
check('a greeting from a notification', forget('?welcome=hola') === '/chat');
check('an input staged by a task', forget('?prefill=comprar%20pan') === '/chat');
check('the conversation inside that day', forget('?conv=1787544867402') === '/chat');
check('all of them at once',
      forget('?date=2026-08-15&conv=1787544867402&new=1&welcome=hola&prefill=x') === '/chat');

console.log('\nbut what the page IS survives');
// Dropping the whole query string would take this with it, and the page would
// come back with chrome the app does not want.
check('embed is kept', forget('?embed=1&date=2026-08-15') === '/chat?embed=1');
check('and kept alone', forget('?embed=1') === null, 'nothing to forget, no rewrite');
check('an unknown parameter is not ours to drop',
      forget('?embed=1&utm=x&new=1') === '/chat?embed=1&utm=x');

console.log('\nand it never rewrites a URL that has nothing to forget');
check('no query at all', forget('') === null);
check('nothing recognised', forget('?space=finanzas') === null);
check('the path is preserved', forget('?new=1', '/chat/finanzas') === '/chat/finanzas');

console.log('\nand it runs before anything acts on what it read');
// Read first, forget second, act third: the snapshot is a copy, so clearing the
// address bar cannot take the instructions with it. If `openDay` ever moves
// above the call, the deep link stops being consumed and this comes back.
const then = HTML.slice(HTML.indexOf('initHistory().then('));
const readAt = then.indexOf('new URLSearchParams');
const forgetAt = then.indexOf('forgetUrlIntent()');
const actAt = then.indexOf("params.get('date')");
check('the parameters are read first', readAt >= 0 && readAt < forgetAt, [readAt, forgetAt]);
check('forgotten before anything acts', forgetAt < actAt, [forgetAt, actAt]);
check('and the day link still returns after opening the day',
      /openDay\(d\); return;/.test(then));
/* `conv` names WHICH conversation of that day. Without it the deep link paints
   the whole day, which reads as whichever conversation the day ended on — the
   background-tasks panel's "Ver en el chat" landing somewhere the task was
   never asked. It has to be consumed the same way and act before the day. */
check('the conversation is read from the same snapshot',
      then.indexOf("params.get('conv')") > forgetAt
      && then.slice(0, actAt).split('new URLSearchParams').length === 2,
      [then.slice(0, actAt).split('new URLSearchParams').length - 1]);
/* Both present, then ordered. `indexOf` returns -1 when a string is absent
   and -1 is less than any real index, so the bare comparison passed when the
   deep-link branch was gone entirely -- mutation-tested: deleting it left this
   suite printing "all checks passed". The check above has the `>= 0` guard;
   this one had lost it. */
check('and wins over the day it belongs to',
      then.indexOf('openSessionEntry(') >= 0
      && then.indexOf('openDay(d)') >= 0
      && then.indexOf('openSessionEntry(') < then.indexOf('openDay(d)'));

/* A conversation start is a millisecond timestamp. Stripping non-digits
   instead of testing the shape turned a date pasted into the wrong parameter
   into '20260815', and openSessionEntry pins whatever it is handed: an empty
   conversation the sidebar never listed, held against the 3h rule, with the
   next message filed under it. */
check('a conv that is not a conversation start names no conversation',
      then.includes('/^\\d{10,16}$/.test(') && !then.includes('replace(/\\D/g'),
      then.slice(then.indexOf("params.get('conv')"), then.indexOf("params.get('conv')") + 120));

console.log();
if (failures) { console.log(`${failures} FAILED`); process.exit(1); }
console.log('all checks passed');

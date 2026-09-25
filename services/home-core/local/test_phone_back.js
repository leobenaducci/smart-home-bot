/* The phone's back button does one step of the right thing, decided by the page.
   Run: node local/test_phone_back.js

   Reported from the house on 2026-08-19: the phone's back button "still takes
   me to the same old chat". The server side was innocent by then — day files
   clean, launch fix deployed — because the stale thing was the WebView's own
   back stack. The app's Activity lives for days, warm launches never reload,
   and every notification deep link appends, so `webView.goBack()` from the chat
   walks into a /chat entry from another day. Meanwhile everything back should
   act on — an open panel, a pinned old conversation, the sidebar — is DOM
   state that never made a history entry at all.

   So the page owns the answer now: `window.__handleBack()` returns 'handled'
   (closed one thing), 'exit' (chat at its root — the app should leave), and
   the app only falls back to history on 'pass', i.e. on pages that are not the
   chat. These checks lift the real backTargets/handleBack out of chat.html and
   drive them against a fake DOM, so what is asserted is the shipped code and
   not a description of it. */
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

/* Both lifted, so a missing name is a loud ReferenceError instead of a test
   green on a function that does nothing — the trap test_catchup.js records. */
const SOURCE = [
  lift('function backTargets()'),
  lift('function handleBack()'),
].join('\n\n');

const TODAY = '2026-08-19';

let failures = [];
function check(label, cond, detail) {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}${cond ? '' : '  <- ' + detail}`);
  if (!cond) failures.push(label);
}

/* A fake DOM: elements exist, some carry .open, classList records removals. */
function makePage(state) {
  const removed = [];
  const els = {};
  (state.open || []).concat(state.exists || []).forEach(function (id) {
    if (els[id]) return;
    els[id] = {
      id: id,
      classList: {
        contains: function (c) { return c === 'open' && (state.open || []).includes(id); },
        remove: function (c) { if (c === 'open') removed.push(id); },
      },
    };
  });
  const calls = { closeApps: 0, closeBgDetail: 0, applySidebar: 0, backTo: [] };
  const page = {
    TODAY,
    curDay: state.curDay || TODAY,
    pinnedConv: !!state.pinnedConv,
    dayView: !!state.dayView,
    sidebarOpen: !!state.sidebarOpen,
    isMobile: function () { return !!state.mobile; },
    applySidebar: function () { calls.applySidebar++; },
    closeApps: function () { calls.closeApps++; },
    closeBgDetail: function () { calls.closeBgDetail++; },
    backToCurrentChat: function (force) { calls.backTo.push(force); },
    document: { getElementById: function (id) { return els[id] || null; } },
  };
  const fn = new Function(
    'TODAY', 'curDay', 'pinnedConv', 'dayView', 'sidebarOpen',
    'isMobile', 'applySidebar', 'closeApps', 'closeBgDetail',
    'backToCurrentChat', 'document',
    SOURCE + '\nreturn handleBack();'
  );
  const result = fn(
    page.TODAY, page.curDay, page.pinnedConv, page.dayView, page.sidebarOpen,
    page.isMobile, page.applySidebar, page.closeApps, page.closeBgDetail,
    page.backToCurrentChat, page.document
  );
  return { result, removed, calls };
}

console.log('one press closes one thing, topmost first');
let r = makePage({ open: ['notif-panel'] });
check("a panel open: 'handled' and the panel closes",
      r.result === 'handled' && r.removed.join() === 'notif-panel',
      r.result + ' / ' + r.removed.join());

r = makePage({ open: ['bg-detail', 'bg-panel'] });
check('the task trace closes before the task list — out the way you came in',
      r.result === 'handled' && r.calls.closeBgDetail === 1 && r.removed.length === 0,
      JSON.stringify([r.result, r.calls.closeBgDetail, r.removed]));

r = makePage({ open: ['lightbox', 'tasks-panel'] });
check('a lightbox over a panel closes first',
      r.result === 'handled' && r.removed.join() === 'lightbox',
      r.removed.join());

r = makePage({ open: ['apps-dropdown'] });
check('the apps dropdown counts, via its own close (aria state travels with it)',
      r.result === 'handled' && r.calls.closeApps === 1,
      JSON.stringify(r.calls));

console.log('\nthe pinned old chat is a back target — the reported bug');
r = makePage({ pinnedConv: true });
check("an old conversation pinned from the sidebar: 'handled'",
      r.result === 'handled', r.result);
check('and it goes back to the current chat, forced — typed text follows, not blocks',
      r.calls.backTo.length === 1 && r.calls.backTo[0] === true,
      JSON.stringify(r.calls.backTo));

r = makePage({ curDay: '2026-08-15' });
check('a parked old day (notification deep link) is one too',
      r.result === 'handled' && r.calls.backTo.length === 1, r.result);

r = makePage({ dayView: true });
check('and so is the whole-day view', r.result === 'handled', r.result);

r = makePage({ open: ['wa-panel'], pinnedConv: true });
check('panel over a pinned chat: the panel closes first, the pin waits its turn',
      r.result === 'handled' && r.removed.join() === 'wa-panel' && r.calls.backTo.length === 0,
      JSON.stringify([r.removed, r.calls.backTo]));

console.log('\nthe sidebar is mobile-only as a target');
r = makePage({ sidebarOpen: true, mobile: true });
check('open on a phone: back closes it',
      r.result === 'handled' && r.calls.applySidebar === 1,
      JSON.stringify([r.result, r.calls.applySidebar]));
r = makePage({ sidebarOpen: true, mobile: false });
check("open on a desktop: it is furniture, not a layer — 'exit'",
      r.result === 'exit' && r.calls.applySidebar === 0, r.result);

console.log("\nnothing to do means 'exit', never a dead press");
r = makePage({});
check("current chat, nothing open: 'exit' — the app reads this as leave",
      r.result === 'exit', r.result);
check('and nothing was touched on the way out',
      r.removed.length === 0 && r.calls.backTo.length === 0 &&
      r.calls.closeApps + r.calls.closeBgDetail + r.calls.applySidebar === 0,
      JSON.stringify(r.calls));

console.log('\nthe wiring, read off the page and the app');
check('the page exports the handler the app asks for',
      HTML.includes('window.__handleBack = handleBack'));
check('opening a panel arms the sentinel for un-updated apps and plain browsers',
      /!wasOpen && open\) armBackGuard\(\)/.test(HTML));
check('pinning a conversation arms it too',
      lift('async function openSessionEntry(s)').includes('armBackGuard()'));
check('and so does the ?date deep link',
      lift('async function openDay(date)').includes('armBackGuard()'));
const MAIN_PATH = path.join(__dirname, '..', '..', 'proxy', 'android',
  'app', 'src', 'main', 'java', 'com', 'chat', 'app', 'MainActivity.kt');
if (fs.existsSync(MAIN_PATH)) {
  const MAIN = fs.readFileSync(MAIN_PATH, 'utf8');
  check('the app asks the page before touching WebView history',
        MAIN.includes('window.__handleBack') &&
        MAIN.indexOf('window.__handleBack') < MAIN.indexOf('webView.goBack()'));
  check("and only 'pass' falls through to goBack — 'exit' means finish",
        /"exit"\s*->\s*finish\(\)/.test(MAIN));
} else {
  // The app is services/proxy/android/ in this repository. Missing means the
  // path is stale, which a skip would have reported as a pass.
  check('the app source is where this expects it: ' + MAIN_PATH, false);
}

console.log();
if (failures.length) { console.log(`${failures.length} FAILED: ${failures}`); process.exit(1); }
console.log('all checks passed');

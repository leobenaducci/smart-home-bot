/* Leaving a panel puts you back in the chat you are actually having.
   Run: node local/test_back_to_chat.js

   Opening an old conversation pins the view on purpose — `openSessionEntry`
   sets `pinnedConv` so the 3h rule and catchUp cannot yank somebody out of what
   they are reading, and `openDay` (a notification deep link, a task's «Ver en el
   chat») parks `curDay` on the day it came from. Both are right while you are
   looking at them.

   Neither accounted for leaving. Open Notificaciones, come back, and the chat
   was still sitting in March: catchUp returns early on both counts, so nothing
   moved the view forward again and the next thing typed went into a
   conversation from another day.

   Closing a panel is an explicit "back to the chat", so that is where the view
   is put back — today's last conversation, or a new one when the last is stale
   by the same SESSION_GAP the server uses to decide the very same thing.

   The checks that matter most are the ones where it must NOT move: mid-turn,
   and with text already typed. Both mean somebody is mid-thought, and moving
   the ground under them is worse than the bug this fixes. */
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

/* Everything backToCurrentChat reaches for. A missing name is a ReferenceError
   inside a function whose only catch returns quietly — the test would go green
   on a function that does nothing. Same trap test_catchup.js records. */
const SOURCE = [
  lift('function splitConversations(msgs)'),
  lift('function mainConversations(convs)'),
  lift('function startNewChat()'),
  lift('async function backToCurrentChat(force)'),
].join('\n\n');

const TODAY = '2026-08-18';
const SESSION_GAP = 3 * 60 * 60 * 1000;
const MIN = 60 * 1000;
const NOW = 1787000000000;
const T0 = NOW - 30 * MIN;

function runIn(dayMsgs, state) {
  const page = Object.assign({
    TODAY, SESSION_GAP, SPACE: '',
    curDay: TODAY, curSessionId: null, pinnedConv: false, dayView: false,
    freshChat: false, busy: false, allMsgs: [], viewEpoch: 0, lastMsgDay: null,
    messagesEl: { innerHTML: 'algo' },
    inputEl: { value: '' },
    painted: 0, reconnected: 0, sidebarRendered: 0, fetched: [], offline: false,
  }, state);
  page.spaceQ = () => '';
  page.paintMessages = () => { page.painted++; };
  page.renderSidebar = () => { page.sidebarRendered++; };
  page.connectEvents = () => { page.reconnected++; };
  page.fetch = async (url) => {
    page.fetched.push(url);
    if (page.offline) throw new Error('offline');
    return { ok: true, json: async () => dayMsgs };
  };
  const names = Object.keys(page);
  const body = `${SOURCE}\nreturn Promise.resolve(backToCurrentChat())` +
               `.then(() => ({${names.join(',')}}));`;
  // eslint-disable-next-line no-new-func
  const run = new Function(...names, 'encodeURIComponent', 'Date', 'Math', body);
  const FakeDate = { now: () => NOW };
  return run(...names.map(n => page[n]), encodeURIComponent, FakeDate, Math)
    .then(scope => Object.assign({}, scope, {
      painted: page.painted, reconnected: page.reconnected,
      fetched: page.fetched,
    }));
}

let failures = 0;
function check(label, cond, detail) {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}` +
              (cond ? '' : `  <- ${JSON.stringify(detail)}`));
  if (!cond) failures++;
}

/* A day with two conversations, the second still warm. */
const DAY = [
  { role: 'user', text: 'buenos días', ts: T0 - 5 * SESSION_GAP, conv: T0 - 5 * SESSION_GAP },
  { role: 'bot', text: 'buenos días', ts: T0 - 5 * SESSION_GAP + 1000, conv: T0 - 5 * SESSION_GAP },
  { role: 'user', text: '¿qué hay de almuerzo?', ts: T0, conv: T0 },
  { role: 'bot', text: 'tallarines', ts: T0 + 1000, conv: T0 },
];

(async () => {
  console.log('coming back from a panel lands on the current conversation');
  let out = await runIn(DAY, { curDay: '2026-03-04', dayView: true });
  check('a day from March is left behind', out.curDay === TODAY, out.curDay);
  check('and the view is the day\'s last conversation',
        out.curSessionId === TODAY + ':' + T0, out.curSessionId);
  check('it is no longer a whole-day view', out.dayView === false);
  check('nor pinned, so catchUp can follow the tail again', out.pinnedConv === false);
  check('the messages are that conversation\'s', out.allMsgs.length === 2, out.allMsgs);
  check('and it repainted and reconnected', out.painted === 1 && out.reconnected === 1, out);

  out = await runIn(DAY, { pinnedConv: true, curSessionId: TODAY + ':' + (T0 - 5 * SESSION_GAP) });
  check('a pinned old conversation is released too',
        out.curSessionId === TODAY + ':' + T0 && out.pinnedConv === false, out.curSessionId);

  console.log('\nand on nothing worth resuming, it opens a new one');
  out = await runIn([], { curDay: '2026-03-04', dayView: true });
  check('an empty day starts a fresh chat', out.freshChat === true, out);
  check('with today\'s date', out.curDay === TODAY, out.curDay);
  // Same rule the server applies when a turn names no conversation, so the page
  // and the model agree about what "the current chat" is.
  const STALE = [
    { role: 'user', text: 'hace rato', ts: NOW - SESSION_GAP - MIN, conv: NOW - SESSION_GAP - MIN },
  ];
  out = await runIn(STALE, { pinnedConv: true, curSessionId: TODAY + ':1' });
  check('a conversation older than SESSION_GAP is not resumed', out.freshChat === true, out);
  const WARM = [
    { role: 'user', text: 'recién', ts: NOW - SESSION_GAP + MIN, conv: NOW - SESSION_GAP + MIN },
  ];
  out = await runIn(WARM, { pinnedConv: true, curSessionId: TODAY + ':1' });
  check('one just inside it is', out.freshChat === false
        && out.curSessionId === TODAY + ':' + (NOW - SESSION_GAP + MIN), out.curSessionId);

  console.log('\nbut it never moves the ground under somebody mid-thought');
  out = await runIn(DAY, { busy: true, curDay: '2026-03-04', dayView: true });
  check('not while a turn is running', out.curDay === '2026-03-04', out.curDay);
  check('and it does not even ask the server', out.fetched.length === 0, out.fetched);
  out = await runIn(DAY, { inputEl: { value: 'estaba escribiendo esto' },
                           curDay: '2026-03-04', dayView: true });
  check('not with text already typed', out.curDay === '2026-03-04', out.curDay);
  out = await runIn(DAY, { inputEl: { value: '   ' }, curDay: '2026-03-04', dayView: true });
  check('whitespace is not text', out.curDay === TODAY, out.curDay);

  console.log('\nand it does nothing when there is nothing to do');
  out = await runIn(DAY, {});
  check('already current: no fetch, no repaint',
        out.fetched.length === 0 && out.painted === 0, out);
  // Offline it must leave the view alone rather than blanking it: a chat that
  // empties itself because the network blinked is worse than an old one.
  out = await runIn(DAY, { offline: true, curDay: '2026-03-04', dayView: true });
  check('offline leaves the view exactly as it was', out.curDay === '2026-03-04', out.curDay);
  check('and paints nothing', out.painted === 0, out.painted);

  /* Two doors lead back to the chat and both have to call it. The panel
     observer was the only one at first, and «Consumo de Alfred» is a link to
     /stats rather than a panel — so coming back from it is a page restore, no
     class is dropped, the observer never fires, and the old chat was still
     there. Read off the page rather than trusted: this is wiring, and wiring is
     what silently stops being connected. */
  console.log('\nboth ways back into the chat call it');
  check('closing the last panel does',
        /wasOpen && !open\) \{ backToCurrentChat\(\);/.test(HTML));
  const pageshow = HTML.slice(HTML.indexOf("addEventListener('pageshow'"));
  check('and a page restore does — that is the /stats route back',
        pageshow.slice(0, 400).includes('backToCurrentChat()'),
        pageshow.slice(0, 160));

  console.log();
  if (failures) { console.log(`${failures} FAILED`); process.exit(1); }
  console.log('all checks passed');
})();

/* What the chat repaints when it comes back from the background.
   Run: node local/test_catchup.js

   catchUp() is the only thing that draws a reply which arrived while the page
   was away — backgrounded WebView, laptop asleep, app closed entirely. It is
   also, twice now, where the bugs were: it used to compare a whole day against
   the one conversation on screen (so every glance at the phone repainted the
   morning's chats on top of the current one), and the fix for that could wipe a
   pinned conversation the moment the server split it behind us.

   Neither was visible in a unit test because there wasn't one. This lifts the
   real function out of chat.html — the source text, not a copy of the rule —
   and runs it against a stubbed page. Same trick as test_sessions.py, which
   lifts _iter_sessions out of app.py by AST.  */
'use strict';
const fs = require('fs');
const path = require('path');

const HTML = fs.readFileSync(path.join(__dirname, 'templates', 'chat.html'), 'utf8');

/* The function's own source, brace-matched from its declaration. Taking it by
   text is the point: a paraphrase here would pass forever while the page broke. */
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

/* Everything catchUp reaches for. A name it calls that is missing here is a
   ReferenceError inside its own `try`, which the page swallows by design — so
   the test goes quiet rather than red, and passes on a function that does
   nothing at all. `mainConversations` was added to the page with branches and
   not to this list, and five checks here spent that time silently green-lighting
   a catchUp that threw on every unpinned repaint. If a check below fails on a
   view that should have moved, look here first. */
const SOURCE = [
  lift('function splitConversations(msgs)'),
  lift('function mainConversations(convs)'),
  lift('function lastConversationEntry(msgs)'),
  lift('function persistMsg(role, text, conv, day)'),
  lift('function conversationActiveAt(entry)'),
  lift('function launchStaysWarm(entry)'),
  lift('function startNewChat()'),
  lift('async function catchUp()'),
].join('\n\n');

const TODAY = '2026-08-03';
const MIN = 60 * 1000;
const T0 = 1785700000000;

/* The page's own numbers, not copies of them: this test is about what the rules
   do, and a second literal here would keep passing after a rule moved.

   BOTH are lifted, and in the page's order, because the page may write one in
   terms of the other — `LAUNCH_WARM_MS` is `SESSION_GAP` now, the two windows
   having turned out to be the same rule. Lifting only the second was worse than
   not lifting it: `eval('SESSION_GAP')` resolved against a literal declared
   here, so the number came from this file while looking as though it came from
   the page, and moving the page's gap left every check green against a rule it
   no longer had. Declared after MIN and T0 so an expression naming one of those
   (`30 * MIN`) meets a value rather than its temporal dead zone. */
function liftConst(name) {
  const m = HTML.match(new RegExp('const ' + name + '\\s*=\\s*([^;]+);'));
  if (!m) throw new Error(`no \`const ${name}\` in chat.html — renamed?`);
  return eval(m[1]);
}
const SESSION_GAP = liftConst('SESSION_GAP');
const LAUNCH_WARM_MS = liftConst('LAUNCH_WARM_MS');

/* The page, reduced to what the lifted functions touch. `code` is the call
   under test, run inside that scope so it sees the same free variables the real
   page does. */
function runIn(dayMsgs, state, code) {
  const page = Object.assign({
    TODAY, SESSION_GAP, LAUNCH_WARM_MS, MAX_MSGS: 500, SPACE: '', CSRF_TOKEN: 'tok',
    curDay: TODAY, curSessionId: null, pinnedConv: false, dayView: false,
    freshChat: false, busy: false, catchingUp: false, allMsgs: [],
    viewEpoch: 0, lastMsgDay: null, messagesEl: { innerHTML: 'algo' },
    painted: 0, reconnected: 0, sidebarRendered: 0, fetched: [], posted: [],
    // Somewhere to hang "and while that request was in flight, this happened".
    // An object, because a stub reassigning a free variable would only move the
    // binding inside the lifted scope — see the note below.
    hooks: {},
  }, state);
  page.spaceQ = () => '';
  page.paintMessages = () => { page.painted++; };
  page.renderSidebar = () => { page.sidebarRendered++; };
  page.connectEvents = () => { page.reconnected++; };
  page.convStart = () => {
    if (!page.curSessionId) return null;
    const p = page.curSessionId.split(':');
    const v = parseInt(p[p.length - 1], 10);
    return isFinite(v) && v > 0 ? v : null;
  };
  page.fetch = async (url, opts) => {
    if (opts && opts.method === 'POST') {
      page.posted.push(JSON.parse(opts.body));
      return { ok: true, json: async () => ({}) };
    }
    page.fetched.push(url);
    if (page.hooks.inFlight) page.hooks.inFlight();
    return { ok: true, json: async () => dayMsgs };
  };
  // The lifted source reads and writes these as free variables, so it has to
  // run in a scope where they are exactly that.
  const names = Object.keys(page);
  const body = `${SOURCE}\nreturn Promise.resolve(${code}).then(() => ({${names.join(',')}}));`;
  // eslint-disable-next-line no-new-func
  const run = new Function(...names, 'encodeURIComponent', body);
  // What the function reassigned comes from that scope (`allMsgs`,
  // `curSessionId`, `freshChat`); what it did comes off the page, since a
  // counter bumped through a stub mutates the object and never the binding.
  return run(...names.map(n => page[n]), encodeURIComponent).then(scope =>
    Object.assign({}, scope, {
      painted: page.painted, reconnected: page.reconnected,
      sidebarRendered: page.sidebarRendered,
      fetched: page.fetched, posted: page.posted,
    }));
}

function makePage(dayMsgs, state) {
  return runIn(dayMsgs, state, 'catchUp()');
}

let failures = 0;
function check(label, cond, detail) {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}` +
              (cond ? '' : `  <- ${JSON.stringify(detail)}`));
  if (!cond) failures++;
}

const msg = (ts, text, conv, role) =>
  Object.assign({ ts, text, role: role || 'user' }, conv ? { conv } : {});

/* A day holding two conversations, three hours apart. */
const FIRST = [msg(T0, 'la primera', T0), msg(T0 + MIN, 'ya está', T0, 'bot')];
const SECOND_START = T0 + SESSION_GAP + MIN;
const SECOND = [msg(SECOND_START, 'la segunda', SECOND_START)];
const DAY = FIRST.concat(SECOND);

(async () => {
  console.log('a quiet return repaints nothing');
  let p = await makePage(DAY, {
    allMsgs: SECOND.slice(), curSessionId: `${TODAY}:${SECOND_START}`,
  });
  check('the view is left alone', p.painted === 0, p.painted);
  check('and it still holds one conversation, not the day',
        p.allMsgs.length === 1, p.allMsgs);

  console.log('\na reply that arrived while away is drawn');
  const withReply = DAY.concat([msg(SECOND_START + MIN, 'aquí tienes', SECOND_START, 'bot')]);
  p = await makePage(withReply, {
    allMsgs: SECOND.slice(), curSessionId: `${TODAY}:${SECOND_START}`,
  });
  check('it repaints once', p.painted === 1, p.painted);
  check('with this conversation only — not the whole day',
        p.allMsgs.length === 2 && p.allMsgs.every(m => (m.conv || m.ts) >= SECOND_START),
        p.allMsgs.map(m => m.text));

  console.log('\na reminder that opened a NEW conversation is followed');
  const NEW_START = SECOND_START + SESSION_GAP + MIN;
  const withReminder = DAY.concat([msg(NEW_START, 'no olvides la prueba', NEW_START, 'bot')]);
  p = await makePage(withReminder, {
    allMsgs: SECOND.slice(), curSessionId: `${TODAY}:${SECOND_START}`,
  });
  check('the view moves to it', p.painted === 1 && p.allMsgs.length === 1
        && p.allMsgs[0].text === 'no olvides la prueba', p.allMsgs);
  check('the conversation id follows, so the answer runs in the new context',
        p.curSessionId === `${TODAY}:${NEW_START}`, p.curSessionId);
  check('and the stream is re-attached to it', p.reconnected === 1, p.reconnected);

  console.log('\na pinned conversation the server split behind us is not wiped');
  /* Resumed from the sidebar after 3h: send() keeps the old stamp (pinnedConv),
     but the server splits on the gap first and files the new exchange under a
     start of its own. The lookup finds the ORIGINAL segment, now shorter than
     what is on screen. Repainting there would take away the exchange the person
     is reading. */
  const resumed = FIRST.concat([
    msg(T0 + SESSION_GAP + 2 * MIN, 'seguimos', T0),
    msg(T0 + SESSION_GAP + 3 * MIN, 'claro', T0, 'bot'),
  ]);
  p = await makePage(resumed, {
    allMsgs: FIRST.concat(resumed.slice(2)), pinnedConv: true,
    curSessionId: `${TODAY}:${T0}`,
  });
  check('nothing is repainted', p.painted === 0, p.painted);
  check('the exchange stays on screen', p.allMsgs.length === 4, p.allMsgs.length);
  check('and the pin is not moved', p.curSessionId === `${TODAY}:${T0}`, p.curSessionId);

  console.log('\na pinned conversation that is no longer in the day is left alone');
  /* The pin names a start the split no longer produces — history trimmed at
     MAX_HISTORY_MSGS, or the boundary moved. There is nothing to show, and
     showing nothing means blanking a chat the person is reading.

     This pins the behaviour, not the guard that produces it: deleting the
     explicit bail leaves the next line dereferencing a null entry, and
     catchUp's own try/catch swallows that to the same outcome. Keep the bail
     anyway — the two are only equivalent by accident. */
  p = await makePage(DAY, {
    allMsgs: SECOND.slice(), pinnedConv: true, curSessionId: `${TODAY}:12345`,
  });
  check('the view is not blanked', p.painted === 0 && p.allMsgs.length === 1,
        [p.painted, p.allMsgs.length]);

  console.log('\na pinned conversation still takes its own new messages');
  const pinnedGrew = FIRST.concat([msg(T0 + 2 * MIN, 'una cosa más', T0)]);
  p = await makePage(pinnedGrew, {
    allMsgs: FIRST.slice(), pinnedConv: true, curSessionId: `${TODAY}:${T0}`,
  });
  check('it repaints', p.painted === 1 && p.allMsgs.length === 3, p.allMsgs.length);
  check('without re-attaching the stream (the pin decides, not the tail)',
        p.reconnected === 0, p.reconnected);

  console.log('\na whole day opened on purpose stays a whole day');
  /* openDay() — the background-tasks panel's "Ver en el chat", and the ?date=
     deep link. Slicing it to its last conversation here would collapse the view
     seconds after it opened: connectEvents() reconnects and calls straight back
     into catchUp. */
  p = await makePage(DAY, { dayView: true, allMsgs: DAY.slice(0, 2) });
  check('the day is refreshed as a whole', p.allMsgs.length === DAY.length, p.allMsgs.length);
  const q = await makePage(DAY, { dayView: true, allMsgs: DAY.slice() });
  check('and an unchanged day is not repainted', q.painted === 0, q.painted);

  console.log('\n"+ Nuevo" is not undone behind your back');
  /* Reported: pressing Nuevo sometimes just continued the last chat. The view
     is empty and unpinned, so catchUp took the day's last conversation as the
     tail, repainted it and restored its id — every return to the foreground and
     every stream reconnect was another chance to do it. An empty new chat is
     empty on purpose. */
  p = await makePage(DAY, {
    freshChat: true, allMsgs: [], curSessionId: TODAY + ':' + (SECOND_START + 999),
  });
  check('nothing is painted into it', p.painted === 0, p.painted);
  check('it stays empty', p.allMsgs.length === 0, p.allMsgs);
  check('and it keeps its own id', p.curSessionId === TODAY + ':' + (SECOND_START + 999),
        p.curSessionId);
  check('it does not even ask the server', p.fetched.length === 0, p.fetched);

  p = await makePage(DAY, {
    freshChat: false, allMsgs: [], curSessionId: TODAY + ':' + (SECOND_START + 999),
  });
  check('but once it has been used, catching up works again',
        p.painted === 1 && p.allMsgs.length === 1, [p.painted, p.allMsgs.length]);

  console.log('\nand a catch-up already in flight cannot undo it either');
  /* 2026-08-15, in the house: the app launches on `/chat?new=1`, whose
     startNewChat() runs after initHistory resolves — while the catchUp that
     initHistory's connectEvents() started is still waiting on its own request.
     It landed second, repainted the conversation just abandoned and re-pinned
     its id, but left `freshChat` set, because only the thing that moved the
     view owns that flag. The page then showed one conversation and sent the
     next message into a third one: Alfred was asked to turn off a light he had
     no memory of, and the sidebar grew two entries for one exchange.

     The guard at the top of catchUp cannot see this — `freshChat` was false
     when it was read. Only the epoch can. */
  p = await runIn(withReply, {
    allMsgs: SECOND.slice(), curSessionId: `${TODAY}:${SECOND_START}`,
  }, '(function(){ hooks.inFlight = startNewChat; return catchUp(); })()');
  check('the abandoned conversation is not painted back over the new chat',
        p.painted === 0, p.painted);
  check('the new chat stays empty', p.allMsgs.length === 0, p.allMsgs);
  check('and keeps the id it was given, not the one it left',
        p.curSessionId !== `${TODAY}:${SECOND_START}`, p.curSessionId);
  check('and stays fresh, so the next message runs where the person is looking',
        p.freshChat === true, p.freshChat);

  console.log('\nwhat still counts as warm when the app launches');
  /* The app asks for a clean sheet on every launch. The page keeps the
     conversation instead while it is still being talked in — and "talked in"
     has to mean the people in it, not the house: geofence alerts and reminders
     are filed unstamped into whatever conversation is open, and with four
     phones coming and going they arrive every few minutes. Reading the last
     message would make every conversation warm until midnight. */
  const NOW = Date.now();
  const warmth = async (msgs) => (await runIn([], { hooks: { day: msgs } },
    '(hooks.result = launchStaysWarm(lastConversationEntry(hooks.day)))')).hooks.result;

  /* The line this file exists to hold down, and the one nothing was holding:
     every case below passes with the window at 30 minutes just as happily as at
     three hours, so the change that made the two windows one rule could be
     undone without a single check going red. */
  check('the launch window is the session gap', LAUNCH_WARM_MS === SESSION_GAP,
        { LAUNCH_WARM_MS, SESSION_GAP });

  const recentConv = NOW - 40 * MIN;
  let warm = await warmth([msg(recentConv, 'pon la luz de user5 al 10%', recentConv),
                           msg(NOW - 10 * MIN, 'listo, señor Alex', recentConv, 'bot')]);
  check('a conversation answered ten minutes ago is warm', warm === true, warm);

  /* Warmth is the last thing said in it, not the first. The case above stopped
     proving that when the window grew — both its stamps now sit inside three
     hours, so taking the *first* would pass it too. Here only the answer is
     inside the window, and the two are still one conversation because they are
     less than SESSION_GAP apart. */
  const openedLongBefore = NOW - LAUNCH_WARM_MS - 25 * MIN;
  warm = await warmth([msg(openedLongBefore, 'la pregunta larga', openedLongBefore),
                       msg(NOW - 30 * MIN, 'la respuesta', openedLongBefore, 'bot')]);
  check('and it is the last turn that counts, not the one that opened it',
        warm === true, warm);

  const staleConv = NOW - LAUNCH_WARM_MS - 20 * MIN;
  const stale = [msg(staleConv, 'algo de antes', staleConv),
                 msg(staleConv + MIN, 'listo', staleConv, 'bot')];
  warm = await warmth(stale);
  check('one left alone past the window is not', warm === false, warm);
  /* A conversation kept alive by alerts alone. They arrive unstamped and land
     in whatever is open, so a steady trickle stops the silence split from ever
     ending it — and the last thing a person said can be twice the gap back
     while the last *message* is a minute old. `conversationActiveAt` ignores
     them for exactly this, and it matters more now that the launch window is
     that same silence: without it, four phones coming and going would make
     every conversation warm until midnight.

     Written as a chain rather than one late alert because that is the shape it
     takes in the house, and because a single alert past the gap starts a
     conversation of its own — which is a different thing.

     Every hop comes off SESSION_GAP rather than being written in hours. The
     fixture only says anything while each hop is inside the gap and the human
     turn is outside the window, and spelling those in absolute time made a
     fixture that broke whenever the rule under it moved — which is how the one
     this replaced died. */
  const HOP = SESSION_GAP - 20 * MIN;   // a trickle: inside the gap, so it never splits
  const longAgo = NOW - 2 * SESSION_GAP;
  warm = await warmth([
    msg(longAgo, 'algo de antes', longAgo),
    msg(longAgo + MIN, 'listo', longAgo, 'bot'),
    msg(longAgo + MIN + HOP, 'Kai salió del colegio', null, 'bot'),
    msg(longAgo + MIN + 2 * HOP, 'Sam llegó al trabajo', null, 'bot'),
    msg(NOW - MIN, 'Sam llegó a casa', null, 'bot'),
  ]);
  check('and alerts trickling into it do not revive it',
        warm === false, warm);

  /* The same rule with nobody in it at all. A day whose entire history is
     crossings has no stamped message to measure, and the fallback meant for
     pre-`conv` history used to hand back the newest message anyway — so a
     launch opened on a "conversation" of the house talking to itself. Thirty
     minutes kept it rare; the gap would have made it most of the day. */
  warm = await warmth([
    msg(NOW - 2 * 60 * MIN, 'Kai salió del colegio', null, 'bot'),
    msg(NOW - 45 * MIN, 'Sam llegó a casa', null, 'bot'),
  ]);
  check('a day of nothing but alerts is not a conversation to open on',
        warm === false, warm);

  /* But that fallback still has its real job: pre-`conv` history carries no
     stamps either, and it is a conversation — the person is in it. */
  warm = await warmth([
    msg(NOW - 50 * MIN, 'algo de antes de que existiera conv', null),
    msg(NOW - 49 * MIN, 'listo', null, 'bot'),
  ]);
  check('while unstamped history with a person in it still is', warm === true, warm);

  /* Which conversation a launch opens, not merely whether one is warm. */
  const openedConv = async (msgs) => (await runIn([], { hooks: { day: msgs } },
    '(hooks.result = (lastConversationEntry(hooks.day) || {}).start || 0)')).hooks.result;

  /* A → B → back to A. Coming back to a conversation the day already holds
     appends to the entry `splitConversations` already made for it, so the array
     stays in creation order while the talking moves back up it: the entry made
     LAST is B, and the conversation actually being had is A. Taking the tail
     handed the launch B — abandoned, but warm enough under the wider window to
     be opened, with the person's real last exchange off screen. */
  const convA = NOW - 2 * SESSION_GAP;
  const convB = convA + MIN + SESSION_GAP + 10 * MIN;   // past the gap: its own entry
  const backToA = convB + MIN + 30 * MIN;               // inside the gap: resumes A
  const opened = await openedConv([
    msg(convA,        'conversación A', convA),
    msg(convA + MIN,  'listo',          convA, 'bot'),
    msg(convB,        'conversación B', convB),
    msg(convB + MIN,  'listo',          convB, 'bot'),
    msg(backToA,      'sigo con A',     convA),
    msg(backToA + MIN,'listo',          convA, 'bot'),
  ]);
  check('a day that went A → B → back to A opens A, not the entry made last',
        opened === convA, { opened, convA, convB });

  warm = await warmth([]);
  check('an empty day is not warm either', warm === false, warm);

  console.log('\na message files itself in the right conversation');
  /* persistMsg decides what the sidebar and the model session will agree on, so
     the two things that can go wrong here are worth stating: a first message
     has to end the "fresh" state, and a reply to a turn you have already walked
     away from has to go where it was asked. */
  p = await runIn(DAY, { freshChat: true, allMsgs: [], curSessionId: `${TODAY}:${T0}` },
                  "persistMsg('user', 'hola')");
  check('writing into a new chat ends its fresh state', p.freshChat === false, p.freshChat);
  check('and the message names that conversation',
        p.posted[0].conv === T0 && p.posted[0].date === TODAY, p.posted[0]);
  check('and it appears in the view', p.allMsgs.length === 1, p.allMsgs);

  /* "+ Nuevo" pressed while the previous answer was still streaming: the view
     has moved to a brand-new conversation by the time the reply lands. */
  p = await runIn(DAY, { allMsgs: [], curSessionId: `${TODAY}:${SECOND_START + 999}` },
                  `persistMsg('bot', 'la respuesta de antes', ${T0}, '${TODAY}')`);
  check('a reply to the turn you left is filed where it was asked',
        p.posted[0].conv === T0, p.posted[0]);
  check('and does NOT appear in the new chat', p.allMsgs.length === 0, p.allMsgs);
  check('which stays fresh — nothing was written into it',
        p.freshChat === false, p.freshChat);   // it was not fresh to begin with

  p = await runIn(DAY, { allMsgs: SECOND.slice(), curSessionId: `${TODAY}:${SECOND_START}` },
                  `persistMsg('bot', 'la respuesta', ${SECOND_START}, '${TODAY}')`);
  check('a reply to the conversation still on screen does appear',
        p.allMsgs.length === 2, p.allMsgs.length);

  p = await runIn(DAY, { allMsgs: [], curSessionId: `${TODAY}:${T0}`, curDay: TODAY },
                  `persistMsg('bot', 'de ayer', ${T0}, '2026-08-02')`);
  check('a reply from another day is stored against that day',
        p.posted[0].date === '2026-08-02' && p.allMsgs.length === 0, p.posted[0]);

  console.log('\nthe guards still hold');
  p = await makePage(DAY, { busy: true, allMsgs: [] });
  check('mid-turn it does not run', p.fetched.length === 0 && p.painted === 0, p.fetched);
  p = await makePage(DAY, { curDay: '2026-07-01', allMsgs: [] });
  check('viewing a past day it does not run', p.fetched.length === 0, p.fetched);
  p = await makePage(DAY, { catchingUp: true, allMsgs: [] });
  check('it does not overlap itself', p.fetched.length === 0, p.fetched);

  console.log();
  if (failures) {
    console.log(`${failures} FAILED`);
    process.exit(1);
  }
  console.log('all checks passed');
})();

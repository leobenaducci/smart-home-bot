"""What every turn cost, and what kind of turn it was.

Run: python local/test_usage_stats.py   (needs Flask; skips loudly without it)

Nothing recorded token usage anywhere. Choosing a model was therefore done by
sending synthetic prompts to candidates and reasoning about the answers — a
proxy for the house's traffic, not the traffic. The one number that actually
decided it on 2026-08-16 (98% of input arrives cached, so cache_read dominates
and the headline input price misleads by 5x) came from a single log line
somebody happened to paste into a config comment.

Two things are worth pinning here.

**The taxonomy.** The breakdown the family wants — notificaciones, tareas,
las profesiones, Alfred normal — is already encoded in nanobot's
session key, so `_usage_scope` is the whole feature. Get it wrong and the page
is confidently mislabelled, which is worse than empty: somebody would move a
model on it.

**The cost model.** Uncached input is charged at `input` and the rest at
`cache_read`, and those differ by ~30x on the same model. Bill the cached
tokens at the input rate and every conclusion this page exists to support
inverts.
"""
import os
import shutil
import sys
import tempfile
import time

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-usage-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32,
                  # Who lives here and who is a parent. Both were literals in
                  # app.py -- `ADVANCED_USERS = {...}` -- and are supplied by
                  # the deployer now, so a suite that does not say leaves the
                  # house with no adults in it and every admin route answers
                  # 403 with "Solo administradores".
                  HOMECORE_MEMBERS="user1,user2,user4",
                  HOMECORE_ADMIN_MEMBERS="user1,user2")

USER1 = "user1"        # ADVANCED_USERS
USER2 = "user2"       # ADVANCED_USERS
USER4 = "user4"       # not an admin
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "%s", "nanobot_id": 2}, {"username": "%s", "nanobot_id": 3},'
            ' {"username": "%s", "nanobot_id": 4}]' % (USER1, USER2, USER4))

sys.path.insert(0, dst)
import app as A  # noqa: E402

A.init_usage_db()
A.app.config['TESTING'] = True
client = A.app.test_client()

failures = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def _h(u=USER1):
    return {'X-Proxy-Secret': 'p' * 32, 'X-Proxy-User': u}


def send(session_key, model='deepseek-v4-flash', prompt=45571, cached=44544,
         completion=300, reasoning=0, tools=0, user=USER1, tool_names=None, route=None):
    body = {
        'session_key': session_key, 'model': model, 'tools': tools,
        'tool_names': tool_names or [],
        'usage': {'prompt_tokens': prompt, 'cached_tokens': cached,
                  'completion_tokens': completion,
                  'completion_tokens_details': {'reasoning_tokens': reasoning}},
    }
    if route:
        body['route'] = route
    return client.post('/chat/usage', headers=_h(user), json=body).get_json()


def summary(days=30, user=USER1):
    return client.get(f'/stats/api/summary?days={days}', headers=_h(user))


# --- The taxonomy -------------------------------------------------------------
print("\nwhat kind of turn was this")
DAY = '2026-08-16'
cases = [
    (f'websocket:homeweb:{USER1}:{DAY}:ev-notif', 'ev-notif', 'Notifications'),
    (f'websocket:homeweb:{USER1}:{DAY}:ev-geo', 'ev-geo', 'Location'),
    (f'websocket:homeweb:{USER1}:{DAY}:ev-task', 'ev-task', 'Chores'),
    (f'websocket:homeweb:{USER1}:{DAY}:ev-ask-{USER2}', 'ev-ask', 'Household questions'),
    (f'websocket:homeweb:{USER1}:{DAY}:dev', 'dev', 'Programmer'),
    (f'websocket:homeweb:{USER1}:{DAY}:dev', 'dev', 'Programmer'),
    (f'websocket:homeweb:{USER1}:{DAY}:edu', 'edu', 'Teacher'),
    (f'websocket:homeweb:{USER1}:{DAY}:dsg', 'dsg', 'Designer'),
    (f'websocket:homeweb:{USER1}:{DAY}:dlg-dsg', 'dlg', 'Delegations between professions'),
    # The heartbeat reports a bare scope: it is not a conversation and has no
    # homeweb key. Its own row, because an unrecognised key falls through to
    # `sub` and a missing one reads as ordinary chat -- and this turn was
    # invisible on the page for long enough already.
    ('ev-heartbeat', 'ev-heartbeat', 'Heartbeat'),
    ('whatsapp:56911111111', 'whatsapp', 'WhatsApp'),
    # A conversation start in milliseconds — an ordinary chat.
    (f'websocket:homeweb:{USER1}:{DAY}:1786897021264', '', 'Ordinary Alfred'),
    # The plain day session, no conversation segment at all.
    (f'websocket:homeweb:{USER1}:{DAY}', '', 'Ordinary Alfred'),
]
for key, scope, label in cases:
    got = A._usage_scope(key)
    check(f"{label:32} <- {key.split(':')[-1][:22]}", got == scope, f"got {got!r}")
    check(f"   and it is named '{label}'", A._usage_label(got)[0] == label,
          A._usage_label(got))

check("a subagent is not miscounted as ordinary chat",
      A._usage_scope('subagent:research-1786897021') == 'sub')
check("and an empty key does not crash", A._usage_scope(None) == '')

# The professions are read from CHAT_SPACES, not a second copy.
check("every profession scope resolves to its own name",
      all(A._usage_label(s)[0] == A.CHAT_SPACES[sp]['title']
          for s, sp in A.CHAT_SCOPE_SPACES.items()),
      {s: A._usage_label(s)[0] for s in A.CHAT_SCOPE_SPACES})


# --- The cost model -----------------------------------------------------------
print("\nwhat a turn costs")
# The house's own measured ordinary turn, on the incumbent model.
# Re-based on Zen's prices, 2026-09-01. The numbers moved because the provider
# did: `deepseek-v4-flash` was 0.22/0.66/0.007 on the flat Go plan and is
# 0.14/0.28/0.028 on Zen -- cheaper input, four times the cache read -- which
# is why an ordinary turn costs *more* here despite the cheaper headline.
c = A._usage_cost('deepseek-v4-flash', 45571, 44544, 300)
check("an ordinary flash turn is ~0.00148", abs(c - 0.001475) < 5e-6, c)
check("cached tokens are NOT billed at the input rate",
      c < A._usage_cost('deepseek-v4-flash', 45571, 0, 300) / 3,
      (c, A._usage_cost('deepseek-v4-flash', 45571, 0, 300)))
check("gpt-5-nano is about a quarter of flash",
      0.24 < A._usage_cost('gpt-5-nano', 45571, 44544, 300) / c < 0.30,
      A._usage_cost('gpt-5-nano', 45571, 44544, 300) / c)
check("luna is a wash against flash",
      0.95 < A._usage_cost('gpt-5.6-luna', 45571, 44544, 300) / c < 1.02)
# The whole reason this table carries three numbers and not one. gpt-oss-20b
# is a third of flash's input price and publishes no cached rate at all, so an
# ordinary turn -- which is mostly re-sent history -- costs half again as much.
# This is not hypothetical: it is why `notifications` came off that model.
check("gpt-oss-20b costs MORE than flash despite a cheaper input price — cache_read",
      A._usage_cost('openai/gpt-oss-20b', 45571, 44544, 300) > c * 1.5
      and A.USAGE_RATES['openai/gpt-oss-20b'][0]
          < A.USAGE_RATES['deepseek-v4-flash'][0])
check("an unknown model has no invented price", A._usage_cost('some-new-model', 100, 0, 10) is None)


# --- Ingest and roll-up --------------------------------------------------------
print("\nthe page's numbers")
for key, _, _ in cases:
    send(key)
send(f'websocket:homeweb:{USER2}:{DAY}:ev-notif', user=USER2, completion=100)
send(f'websocket:homeweb:{USER1}:{DAY}:1786897021264', model='gpt-5.6-luna',
     completion=40, tools=3)
send(f'websocket:homeweb:{USER1}:{DAY}:1786897021264', model='some-new-model')

r = summary()
check("the summary loads", r.status_code == 200, r.status_code)
d = r.get_json()
check("every record is counted", d['total']['turns'] == len(cases) + 3, d['total']['turns'])
check("tools are summed", d['total']['tools'] == 3, d['total']['tools'])
check("an unpriced model is flagged, not silently zero",
      d['total']['unpriced'] == 1, d['total']['unpriced'])
check("it still counts that turn's tokens",
      d['total']['prompt'] == 45571 * (len(cases) + 3), d['total']['prompt'])

scopes = {s['key']: s for s in d['by_scope']}
check("ordinary chat is its own line", scopes['']['turns'] == 4, scopes.get(''))
check("notifications from two people land together",
      scopes['ev-notif']['turns'] == 2, scopes.get('ev-notif'))
check("the scope rows carry their labels",
      scopes['dev']['label'] == 'Programmer' and scopes['whatsapp']['icon'] == '💬')

models = {m['key']: m for m in d['by_model']}
check("models are split out", 'gpt-5.6-luna' in models and 'deepseek-v4-flash' in models,
      list(models))
check("the unknown model appears with zero cost",
      models['some-new-model']['cost'] == 0 and models['some-new-model']['turns'] == 1)

users = {u['key']: u for u in d['by_user']}
check("per person, and named", users[USER2]['label'] and users[USER2]['turns'] == 1,
      users.get(USER2))
check("the split adds up to the total cost",
      abs(sum(d['split'].values()) - d['total']['cost']) < 1e-6,
      (sum(d['split'].values()), d['total']['cost']))
check("cached input is the biggest slice of the bill",
      d['split']['cached_in'] > d['split']['uncached_in'], d['split'])
# No budget set is the default, and the page has to tell that apart from a
# budget of zero: `cap` and `pct` are None, not 0, so the bar is not drawn
# rather than drawn full. These used to assert $60 and $12 -- the flat
# OpenCode Go plan's ceilings, which stopped describing anything when the
# stack moved to per-token billing.
check("with no budget set there is no ceiling to report",
      d['caps']['month']['cap'] is None and d['caps']['h5']['cap'] is None,
      d['caps'])
check("and no percentage either, which is not the same as zero",
      d['caps']['month']['pct'] is None, d['caps']['month'])
check("the spend itself is still reported",
      d['caps']['month']['spent'] > 0, d['caps']['month'])

# And with one set, it is measured against it.
_prev = (A.USAGE_CAP_MONTH, A.USAGE_CAP_5H)
A.USAGE_CAP_MONTH, A.USAGE_CAP_5H = 60.0, 12.0
try:
    _b = summary().get_json()['caps']
    check("a budget that is set is drawn against",
          _b['month']['cap'] == 60.0 and _b['h5']['cap'] == 12.0, _b)
    check("and the house is nowhere near it", _b['month']['pct'] < 1, _b['month'])
finally:
    A.USAGE_CAP_MONTH, A.USAGE_CAP_5H = _prev
check("there is a day series to draw", len(d['by_day']) == 1, d['by_day'])


# --- Who may look -------------------------------------------------------------
print("\nwhose numbers these are")
check("an admin may", summary(user=USER1).status_code == 200)
check("the other admin may", summary(user=USER2).status_code == 200)
check("a child may not — this is the whole house's traffic",
      summary(user=USER4).status_code == 403)
with client.session_transaction() as s:
    s['user'] = USER4
check("and the page redirects rather than rendering",
      client.get('/stats').status_code in (301, 302))
with client.session_transaction() as s:
    s['user'] = USER1
check("while an admin gets the page", client.get('/stats').status_code == 200)


# --- Degrading -----------------------------------------------------------------
print("\nwhen the report is odd")
check("a report with no usage is accepted and ignored",
      client.post('/chat/usage', headers=_h(), json={'session_key': 'x'}).status_code == 200)
before = summary().get_json()['total']['turns']
send('websocket:homeweb:%s:%s:ev-notif' % (USER1, DAY), prompt=0, cached=0, completion=0)
check("a zero-token turn still counts as a turn",
      summary().get_json()['total']['turns'] == before + 1)
# deepseek reports the cached count under a name of its own.
client.post('/chat/usage', headers=_h(), json={
    'session_key': f'websocket:homeweb:{USER1}:{DAY}:ev-geo', 'model': 'deepseek-v4-flash',
    'usage': {'prompt_tokens': 1000, 'prompt_cache_hit_tokens': 900, 'completion_tokens': 10}})
check("deepseek's prompt_cache_hit_tokens is read as cached",
      summary().get_json()['total']['cached'] >= 900)
# and the OpenAI-shaped ones nest it.
client.post('/chat/usage', headers=_h(), json={
    'session_key': f'websocket:homeweb:{USER1}:{DAY}:ev-geo', 'model': 'gpt-5.6-luna',
    'usage': {'prompt_tokens': 1000, 'prompt_tokens_details': {'cached_tokens': 800},
              'completion_tokens': 10}})
check("so is the nested prompt_tokens_details.cached_tokens",
      summary().get_json()['total']['cached'] >= 1700)

# And the Anthropic shape, which was missing. A name this list does not know
# does not read as a gap in the reader -- it reads as a model that does not
# cache, and eleven Designer turns at 300-900k prompt tokens each were counted
# that way before anybody looked.
client.post('/chat/usage', headers=_h(), json={
    'session_key': f'websocket:homeweb:{USER1}:{DAY}:dsg', 'model': 'gpt-6-astra',
    'usage': {'prompt_tokens': 1000, 'cache_read_input_tokens': 700,
              'completion_tokens': 10}})
check("and Anthropic's cache_read_input_tokens is read as cached",
      summary().get_json()['total']['cached'] >= 2400)

# --- Routing --------------------------------------------------------------------
# Which tier a turn ran on and who decided (docs/routing.md). An escalation is
# two records -- the cheap attempt that failed and the strong continuation --
# and only the continuation says `escalation`, so it is counted once.
print("\nrouting: the tier, the label, and escalations per label")
chat_key = f'websocket:homeweb:{USER1}:{DAY}:1700000000000'
send(chat_key, model='deepseek-v4-flash', prompt=30000, cached=29000,
     route={'tier': 'everyday', 'label': 'action', 'source': 'model',
            'escalated': True, 'escalated_from': 'bad_invocation'})
send(chat_key, model='deepseek-v4-pro', prompt=30000, cached=0,
     route={'tier': 'powerful', 'label': 'action', 'source': 'escalation',
            'escalated': True, 'escalated_from': 'bad_invocation'})
send(chat_key, model='deepseek-v4-pro', prompt=30000, cached=0,
     route={'tier': 'powerful', 'label': 'complex', 'source': 'model'})
send(chat_key, model='deepseek-v4-flash', prompt=30000, cached=29000)   # a record with no route at all
by_route = {r['key']: r for r in summary().get_json()['by_route']}
check("the cheap attempt is filed under its tier and label",
      by_route.get('everyday/action', {}).get('turns') == 1, by_route.keys())
check("the continuation is filed under powerful, and is the one escalation",
      by_route.get('powerful/action', {}).get('turns') == 1
      and by_route['powerful/action'].get('escalations') == 1, by_route.get('powerful/action'))
check("a turn that started strong on the classifier's word is not an escalation",
      by_route.get('powerful/complex', {}).get('escalations') == 0, by_route.get('powerful/complex'))
check("a record with no route is in the totals and in no route",
      'everyday/-' not in by_route and '/-' not in ''.join(by_route), by_route.keys())
check("the money follows the tier",
      by_route['powerful/complex']['cost'] > by_route['everyday/action']['cost'])

# --- Reachable from the phone ---------------------------------------------------
print("\nfinding it from the chat page")
html = open(os.path.join(dst, "templates", "chat.html"), encoding="utf-8").read()
debug = html[html.index('id="debug-group"'):]
debug = debug[:debug.index("</div>")]
check("it is in the Debug group, which is where somebody goes looking",
      "/stats" in debug, debug[:200])
check("as a link and not a panel — the page already exists",
      'href="/stats"' in debug)
check("and it opens in place, not a new tab (the app is a WebView)",
      "target=" not in debug.split('href="/stats"')[1][:120])
check("admin-only, matching the route that redirects everybody else",
      "{% if is_admin %}" in html[:html.index('href="/stats"')][-400:], "not gated")

with client.session_transaction() as s2:
    s2['user'] = USER1
page = client.get('/chat').get_data(as_text=True)
check("an admin sees the entry on the real page", 'href="/stats"' in page)
with client.session_transaction() as s2:
    s2['user'] = USER4
page = client.get('/chat').get_data(as_text=True)
check("a child does not", 'href="/stats"' not in page)

# --- What the turns actually did ----------------------------------------------
print("\nwhich tools were called")
before = summary().get_json()['total']['tools']
# The count alone was what this recorded at first: it answers "was that turn
# busy" and never "busy doing what", which is the question worth the storage.
send('websocket:homeweb:%s:2026-08-18:1787000000000' % USER1,
     tool_names=['exec', 'exec', 'read_file', 'web_search',
                 'mcp_brightdata_search_engine', 'skill:finanzas', 'skill:whatsapp'])
d = summary().get_json()
tools = {t['label']: t['calls'] for t in d.get('by_tool', [])}
check("every call is counted, repeats included", tools.get('Commands') == 2, tools)
# The point of the whole exercise: every skill runs through exec, so without
# this the row says «Comandos» and never which skill was doing the spending.
check("each skill is its own row", tools.get('Skill · finanzas') == 1
      and tools.get('Skill · whatsapp') == 1, tools)
check("and is not folded back into the commands row", tools.get('Commands') == 2, tools)
check("and the others too", tools.get('Reading files') == 1 and tools.get('Web search') == 1, tools)
check("an mcp tool is named by what it is", 'brightdata search engine' in tools, tools)
# Ranked by cost now, not by call count: the page exists to answer where the
# money goes, and a tool called constantly for free is not the answer.
by = {t['label']: t for t in d['by_tool']}
check("a tool used twice in one turn is one turn, two calls",
      by['Commands']['calls'] == 2 and by['Commands']['turns'] == 1, by)
check("and it carries the turn's prompt, not a share of it",
      by['Commands']['prompt'] == 45571, by['Commands'])
check("with the average spelled out",
      by['Commands']['avg_prompt'] == 45571, by['Commands'])
check("the cached part comes along", by['Commands']['cached'] == 44544, by)
check("and so does what the turn cost", by['Commands']['cost'] > 0, by)
# A label can fold several real tools, and the row says which rather than
# leaving the reader to guess what «Leer archivos» was.
check("a row names the real tools behind it", by['Commands']['names'] == ['exec'], by['Commands'])
check("and a skill row names itself",
      by['Skill · finanzas']['names'] == ['skill:finanzas'], by['Skill · finanzas'])
# Every tool in that turn is charged the whole turn — the only honest
# arithmetic available, since nothing says which tool the 45k prompt was for.
check("each tool in a turn carries the same turn cost",
      abs(by['Reading files']['cost'] - by['Commands']['cost']) < 1e-9, by)
# `tools` stays exactly what the caller sent. The names are extra, not a
# recount: rows written before names existed still have a truthful count, and a
# derived one would make those rows disagree with these.
check("the count is the caller's, not derived from the names",
      d['total']['tools'] == before, (before, d['total']['tools']))

# Rows written before this existed have no names, and must not break the fold
# or invent a tool called ''.
send('websocket:homeweb:%s:2026-08-18:1787000000001' % USER1, tools=3)
d = summary().get_json()
check("a row with no names adds nothing to the breakdown",
      sum(t['calls'] for t in d['by_tool']) == 7, d['by_tool'])
check("but still counts as calls in the total",
      d['total']['tools'] == before + 3, (before, d['total']['tools']))

# It arrives over HTTP and ends up rendered, so it is cleaned on the way in.
send('websocket:homeweb:%s:2026-08-18:1787000000002' % USER1,
     tool_names=['<script>alert(1)</script>', 'x' * 200, '', 'read_file'])
d = summary().get_json()
labels = ' '.join(t['label'] for t in d['by_tool'])
check("markup cannot survive the trip", '<' not in labels and '>' not in labels, labels)
check("nor can an unbounded name", max(len(t['label']) for t in d['by_tool']) <= 60, labels)
check("and an empty name is not a tool", '' not in [t['label'] for t in d['by_tool']])

print("\nand the overlap is stated rather than hidden")
page_src = open(os.path.join(SRC, "templates", "stats.html"), encoding="utf-8").read()
check("the note says why the shares exceed 100%",
      'more than 100%' in page_src, 'the reader would think it was a bug')
check("and that an even split was refused on purpose",
      'would be invented' in page_src)

print("\nand the page has somewhere to draw it")
page = client.get('/stats', headers=_h()).get_data(as_text=True)
check("the section exists", 'id="by-tool"' in page)
check("with its own renderer", 'toolTable(' in page)
for col in ('Calls', 'Turns', 'Mean prompt', 'Cached'):
    check(f"and a «{col}» column", col in page, col)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks failed" % len(failures) if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)

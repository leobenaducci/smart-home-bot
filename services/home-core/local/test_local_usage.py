"""The house's own models, the card they share, and what a turn really cost.

Run: python local/test_local_usage.py   (needs Flask; skips loudly without it)

Three things this pins, each of which was a wrong number on the page before it
existed.

**Local work is not free work.** Vision, speech and listening cost no money
and were therefore in every figure on /stats as a zero -- which on a house
with one 12 GB card is the wrong axis entirely. What they spend is the card.
So they are a separate store with no price column at all, and the tests below
refuse a dollar figure rather than checking one.

**Two cards are not one card.** The household is about to add a second,
identical 12 GB GPU. A graph that adds them says 14 GB of 24 GB used on a
machine where an 8 GB model fits on neither half. `gpu_sample` therefore keys
on (ts, gpu) -- a key on ts alone silently kept one card's sample and dropped
the other's, and the resulting graph looked entirely reasonable.

**A cost is a fact about a moment.** The page used to re-derive every
historical row against today's prices, so a rate edit re-valued last August
and a model retired from the roster made a year of turns free. What a turn
cost is now written down when it happens.
"""
import os
import shutil
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))

try:
    import flask  # noqa: F401
except ImportError:
    print("SKIP: Flask is not installed here — run this where app.py can import.")
    raise SystemExit(0)

tmp = tempfile.mkdtemp(prefix="homecore-local-usage-")
dst = os.path.join(tmp, "local")
shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(
    "__pycache__", "backup_data", "history", "certs"))
os.makedirs(os.path.join(dst, "backup_data"), exist_ok=True)
os.chdir(dst)
os.environ.update(SECRET_KEY="t" * 32, PROXY_SHARED_SECRET="p" * 32,
                  DEBUG_API_KEY="d" * 32,
                  HOMECORE_MEMBERS="user1,user2",
                  HOMECORE_ADMIN_MEMBERS="user1")
USER1, USER2 = "user1", "user2"
with open(os.path.join(dst, "users.json"), "w", encoding="utf-8") as f:
    f.write('[{"username": "user1", "nanobot_id": 2},'
            ' {"username": "user2", "nanobot_id": 3}]')

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


def svc(name='voice-gateway'):
    return {'X-Service-Token': A._service_token(name)}


def post(records, headers=None):
    return client.post('/stats/api/local',
                       headers=svc() if headers is None else headers,
                       json={'records': records})


def summary(days=30, user=USER1):
    r = client.get(f'/stats/api/summary?days={days}',
                   headers={'X-Proxy-Secret': 'p' * 32, 'X-Proxy-User': user})
    return r.get_json()


# ---------------------------------------------------------------------------
print("\na service can report, and nothing else can")
# The three callers are containers with nobody signed in. Lending them a
# member's identity would be the cheap way and would put a person's name on a
# row that belongs to a room.
check("a derived service token is accepted", post([
    {'kind': 'tts', 'engine': 'audiocpp', 'model': 'supertonic_3_q8_0',
     'route': 'voice-tts', 'ms': 170, 'units': 62}]).status_code == 200)
check("no token is refused", post([{'kind': 'tts'}], headers={}).status_code == 403)
check("a wrong token is refused",
      post([{'kind': 'tts'}], headers={'X-Service-Token': 'x' * 64}).status_code == 403)
# A service token must never satisfy a route that expects a person: the
# namespace is separate (`svc:`) precisely so find_user() cannot resolve it.
check("a service token is not a member token",
      A._service_token('voice-gateway') != A._proxy_user_token('voice-gateway'))
check("each service gets its own",
      A._service_token('voice-gateway') != A._service_token('home-cameras'))
# An unconfigured household must refuse these, not accept every one of them.
_saved, A.PROXY_SHARED_SECRET = A.PROXY_SHARED_SECRET, ''
check("with no master secret, nothing is a service",
      post([{'kind': 'tts'}], headers={'X-Service-Token': 'anything'}).status_code == 403)
A.PROXY_SHARED_SECRET = _saved

print("\nand the record has to look like one")
check("an unknown kind is dropped, not stored",
      post([{'kind': 'telepathy', 'ms': 5}]).get_json()['stored'] == 0)
check("a batch keeps only the records it understands",
      post([{'kind': 'telepathy'}, {'kind': 'asr', 'ms': 90, 'units': 2000}]
           ).get_json()['stored'] == 1)
big = post([{'kind': 'vision', 'model': 'x', 'ms': 10 ** 12, 'units': 10 ** 12}])
check("a nonsense duration is clamped rather than stored", big.status_code == 200)
rows = A._usage_conn().execute(
    'SELECT ms, units FROM local_usage ORDER BY id DESC LIMIT 1').fetchone()
check("  and clamped to something a page can average", rows[0] <= 3_600_000, rows)

print("\nthe page separates what is billed from what is not")
d = summary()
check("the local half is its own key", 'local' in d)
check("  with models", d['local']['by_model'])
check("  and kinds", d['local']['by_kind'])
check("  and the routes that used them", d['local']['by_route'])
# The whole point: no dollar figure anywhere in the local half. A `$0,00` in a
# column beside a real charge is what an unpriced model looked like the last
# time this page was wrong, and it under-reported a month.
flat = str(d['local'])
check("no cost is invented for local work",
      'cost' not in flat and 'usd' not in flat.lower())
tts = [r for r in d['local']['by_model'] if r['kind'] == 'tts'][0]
check("what is reported instead is real: calls", tts['calls'] >= 1)
check("  measured latency", tts['median_ms'] > 0)
check("  time the card was actually busy", tts['busy_ms'] > 0)
check("  and a throughput a household can judge", tts['rate_unit'] == 'char/s')
check("vision gets no invented throughput",
      all(r['rate'] is None for r in d['local']['by_model'] if r['kind'] == 'vision'))

print("\na failed call counts but does not set the pace")
for _ in range(9):
    post([{'kind': 'asr', 'engine': 'faster-whisper', 'model': 'faster-whisper',
           'route': 'voice-asr', 'ms': 100, 'units': 1000}])
post([{'kind': 'asr', 'engine': 'faster-whisper', 'model': 'faster-whisper',
       'route': 'voice-asr', 'ms': 3, 'units': 0, 'ok': False}])
asr = [r for r in summary()['local']['by_model'] if r['kind'] == 'asr'][0]
check("the failure is counted", asr['failed'] == 1)
# A model must not look faster the more often it breaks.
check("but it is not in the timings", asr['median_ms'] >= 90, asr['median_ms'])

print("\nwhich path used which model")
post([{'kind': 'vision', 'engine': 'ollama', 'model': 'qwen3-vl:4b',
       'route': 'clip-review', 'ms': 4200, 'units': 3}])
post([{'kind': 'vision', 'engine': 'ollama', 'model': 'qwen3-vl:4b',
       'route': 'describe-image', 'ms': 1900, 'units': 1}])
routes = {r['route']: r for r in summary()['local']['by_route']}
# The same model serving two jobs with different tolerances. A table keyed on
# the model alone cannot see this, which is how one gets chosen for both.
check("the camera wall is its own row", 'clip-review' in routes)
check("and a person showing Alfred a photo is another", 'describe-image' in routes)
check("each names the model it used",
      routes['clip-review']['model'] == 'qwen3-vl:4b')
check("and reads as something in the house",
      routes['clip-review']['label'] == 'Camera wall, reviewing clips')
check("an unknown route keeps its own name rather than becoming 'other'",
      (post([{'kind': 'tts', 'model': 'm', 'route': 'brand-new', 'ms': 5}])
       and 'brand-new' in {r['route'] for r in summary()['local']['by_route']}))

# ---------------------------------------------------------------------------
print("\ntwo cards are two cards")
# Near enough to now that every window on the page reaches it. A fixed
# epoch here is a test that passes today and silently stops exercising
# anything once it falls out of the longest range the page offers.
now = int(__import__('time').time()) - 120
# Identical hardware reports identical numbers. Keyed on ts alone, the second
# card's sample replaced the first and the graph lost half the machine.
A._gpu_store([{'gpu': 0, 'used_mib': 5940, 'total_mib': 12288, 'util': 40,
               'temp': 55, 'watts': 120},
              {'gpu': 1, 'used_mib': 5940, 'total_mib': 12288, 'util': 40,
               'temp': 55, 'watts': 120}], now=now)
kept = A._usage_conn().execute(
    'SELECT COUNT(*) FROM gpu_sample WHERE ts = ?', (now,)).fetchone()[0]
check("both cards survive one sample", kept == 2, kept)
series = A._gpu_series(hours=24 * 365)
check("and they are drawn separately", len(series['cards']) == 2)
check("each keeps its own size, never the sum",
      all(c['total_mib'] == 12288 for c in series['cards']),
      [c['total_mib'] for c in series['cards']])
check("the timestamps are not shifted to dodge a key collision",
      {r[0] for r in A._usage_conn().execute(
          'SELECT ts FROM gpu_sample').fetchall()} == {now})

print("\nand the graph says what is in them")
A._gpu_store([{'gpu': 0, 'used_mib': 9243, 'total_mib': 12288, 'util': 90,
               'temp': 60, 'watts': 140}],
             resident=[('ollama', -1, 'qwen3-vl:4b', 4616),
                       ('audiocpp', -1, 'supertonic_3_q8_0', 615)],
             now=now + 60)
bd = A._gpu_breakdown(hours=24 * 365)
names = {m['model']: m for m in bd['models']}
check("the vision model is named", 'qwen3-vl:4b' in names)
check("and the voice", 'supertonic_3_q8_0' in names)
# The gap is the camera detector, the CUDA contexts and everything Ollama
# does not count about its own weights -- measured at 1.7 GB for one model.
# Folding it into the models would overstate each by a number nothing reported.
check("the unattributed remainder is a row, not a rounding error",
      'other, unattributed' in names)
check("  and it is the measured gap", names['other, unattributed']['peak_mib'] > 0)
check("peak is what a model needs", names['qwen3-vl:4b']['peak_mib'] == 4616)
check("resident says how much of the window it was there",
      0 < names['qwen3-vl:4b']['resident_pct'] <= 100)

print("\nan exporter is read, not guessed at")
# Both exporters in the wild, and this house runs the DCGM one.
dcgm = ('# HELP DCGM_FI_DEV_FB_USED x\n'
        'DCGM_FI_DEV_FB_USED{gpu="0",UUID="GPU-a"} 5940\n'
        'DCGM_FI_DEV_FB_FREE{gpu="0",UUID="GPU-a"} 6348\n'
        'DCGM_FI_DEV_GPU_UTIL{gpu="0"} 40\n'
        'DCGM_FI_DEV_FB_USED{gpu="1",UUID="GPU-b"} 100\n'
        'DCGM_FI_DEV_FB_FREE{gpu="1",UUID="GPU-b"} 12188\n')
parsed = A._parse_prometheus(dcgm)
check("two cards come back as two", set(parsed) == {0, 1})
check("  with the right framebuffer", parsed[0]['DCGM_FI_DEV_FB_USED'] == 5940)
check("a comment line is not a metric", '# HELP' not in str(parsed))
smi = ('nvidia_smi_memory_used_bytes{index="0"} 6.2277025e+09\n'
       'nvidia_smi_memory_free_bytes{index="0"} 6.6572288e+09\n'
       'nvidia_smi_utilization_gpu_ratio{index="0"} 0.4\n')
check("the other exporter's names are understood too",
      set(A._parse_prometheus(smi)) == {0})
check("garbage in the middle does not stop the parse",
      A._parse_prometheus('junk\nDCGM_FI_DEV_FB_USED{gpu="0"} 12\n')[0][
          'DCGM_FI_DEV_FB_USED'] == 12)

print("\nnothing is drawn where there is no exporter")
_saved_url, A.GPU_EXPORTER_URL = A.GPU_EXPORTER_URL, ''
r = client.get('/stats/api/gpu', headers={'X-Proxy-Secret': 'p' * 32,
                                          'X-Proxy-User': USER1})
check("the page is told it is not configured", r.get_json()['configured'] is False)
check("and the sampler does not start", A.start_gpu_sampler() is False)
A.GPU_EXPORTER_URL = _saved_url

print("\nthe card is the household's business, not the household's")
r = client.get('/stats/api/gpu', headers={'X-Proxy-Secret': 'p' * 32,
                                          'X-Proxy-User': USER2})
check("a non-admin cannot see it", r.status_code == 403)

# ---------------------------------------------------------------------------
print("\nwhat a turn cost is written down when it happens")
sent = client.post('/chat/usage',
                   headers={'X-Proxy-Secret': 'p' * 32, 'X-Proxy-User': USER1},
                   json={'session_key': 'homeweb:user1:2026-09-09:1',
                         'model': 'deepseek-v4-flash',
                         'usage': {'prompt_tokens': 45571, 'cached_tokens': 44544,
                                   'completion_tokens': 300}})
check("the record posts", sent.status_code == 200)
stored = A._usage_conn().execute(
    'SELECT cost_usd FROM token_usage ORDER BY id DESC LIMIT 1').fetchone()[0]
check("with a price attached", stored is not None and stored > 0, stored)
before = summary()['total']['cost']
# Zen retires models under you, and prices move. Neither may re-value history.
_rates, A.USAGE_RATES = A.USAGE_RATES, dict(A.USAGE_RATES)
A.USAGE_RATES['deepseek-v4-flash'] = (99.0, 99.0, 99.0)
check("a price change does not re-value last month", summary()['total']['cost'] == before)
del A.USAGE_RATES['deepseek-v4-flash']
check("and a model leaving the roster does not make its history free",
      summary()['total']['cost'] == before)
A.USAGE_RATES = _rates
# A row written before `cost_usd` existed must not be left blank by the
# migration -- if it were, NULL would mean two things forever ("old" and
# "unpriced") and every historical turn would start reading as unpriced.
# A real pre-migration store: the table as it was, with no `cost_usd` at all.
import sqlite3 as _sq  # noqa: E402
_old_path, A.USAGE_DB_PATH = A.USAGE_DB_PATH, os.path.join(
    dst, 'backup_data', 'pre-migration.db')
_pre = _sq.connect(A.USAGE_DB_PATH)
_pre.executescript('''
    CREATE TABLE token_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL,
        ts INTEGER NOT NULL, day TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '', prompt_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        tools INTEGER NOT NULL DEFAULT 0);
    INSERT INTO token_usage (username, ts, day, model, prompt_tokens,
                             cached_tokens, completion_tokens)
        VALUES ('user1', 1, '2026-01-01', 'deepseek-v4-flash', 1000, 0, 100);
    INSERT INTO token_usage (username, ts, day, model, prompt_tokens,
                             cached_tokens, completion_tokens)
        VALUES ('user1', 1, '2026-01-01', 'a-model-nobody-prices', 1000, 0, 100);
''')
_pre.commit()
_pre.close()
A.init_usage_db()          # the migration, on a store that predates the column
_pre = _sq.connect(A.USAGE_DB_PATH)
_priced = _pre.execute(
    "SELECT cost_usd FROM token_usage WHERE model = 'deepseek-v4-flash'"
).fetchone()[0]
check("the migration backfills a row written before the column",
      _priced is not None and _priced > 0, _priced)
# ...and leaves NULL only where NULL is the right answer, so afterwards the
# value has exactly one meaning.
_unpriceable = _pre.execute(
    "SELECT cost_usd FROM token_usage WHERE model = 'a-model-nobody-prices'"
).fetchone()[0]
check("  and leaves a model it cannot price as unpriced", _unpriceable is None)
_pre.close()
A.USAGE_DB_PATH = _old_path
_c = A._usage_conn()
# And what NULL means afterwards is one thing only: nobody could price it.
# It must stay None rather than becoming a zero that joins a total silently.
_c.execute("INSERT INTO token_usage (username, ts, day, scope, model, "
           "prompt_tokens, cached_tokens, completion_tokens, reasoning_tokens, "
           "tools, tool_names, cost_usd) VALUES ('user1', ?, ?, '', "
           "'a-model-nobody-prices', 1000, 0, 100, 0, 0, '', NULL)",
           (int(__import__('time').time()), A._tasks_today().isoformat()))
_after = summary()
check("an unpriced turn is reported as unpriced", _after['total']['unpriced'] >= 1)
check("  and adds nothing to the cost", _after['total']['cost'] == before)

print("\nand the page can draw all of it")
page = open(os.path.join(dst, 'templates', 'stats.html'), encoding='utf-8').read()
for want, why in [
        ('local-totals', 'the local tiles'),
        ('local-models', 'the local model table'),
        ('local-routes', 'which path used which model'),
        ('gpu-charts', 'a chart per card'),
        ('gpu-breakdown', "what was in the card"),
        ('pill local', 'local work is marked as local'),
        ('--olive', 'and marked in its own colour')]:
    check(f"  {why}", want in page)
check("  the local sheet carries no money format",
      'money(' not in page.split('function localTable')[1].split('function routeTable')[0])
check("  and the note says two cards are not one",
      'not one 24 GB card' in page)

shutil.rmtree(tmp, ignore_errors=True)
print("\nall checks passed" if not failures else
      f"\n{len(failures)} FAILED: {failures}")
raise SystemExit(1 if failures else 0)

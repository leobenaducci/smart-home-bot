"""The markup for /v1/debug/profile.html.

Kept out of api/server.py because it is a page, not a route, and out of a
template file because this one is served by the API process rather than
rendered by the prompt-template loader — one import instead of a path that has
to exist inside the container.

It holds no data and no secret: the secret arrives in the URL fragment, which
browsers never send to a server, and the page uses it as the `X-Debug-Secret`
header on its own fetch. That is why the route itself is open — there is
nothing here to protect.

Ordered by the question being asked. Turns first, because a turn is what
somebody waited through, and then the LLM calls inside them, because that is
where a slow turn's time almost always is. Tools, sessions and models are
underneath for when the first two do not explain it.
"""

PANEL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alfred — profile</title>
<style>
  :root {
    --bg: #10131a; --panel: #171b24; --line: #262c39; --text: #e6e9ef;
    --dim: #8b93a7; --accent: #7aa2f7; --warn: #e0af68; --bad: #f7768e;
    --good: #9ece6a;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; }
  header { position: sticky; top: 0; z-index: 5; background: var(--bg);
           border-bottom: 1px solid var(--line); padding: 10px 16px;
           display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
  h1 { font-size: 14px; margin: 0; font-weight: 600; letter-spacing: .04em; }
  h2 { font-size: 12px; margin: 22px 0 8px; color: var(--dim);
       text-transform: uppercase; letter-spacing: .1em; font-weight: 600; }
  main { padding: 0 16px 48px; }
  .cards { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
          padding: 9px 12px; min-width: 116px; }
  .card b { display: block; font-size: 17px; font-weight: 600; }
  .card span { color: var(--dim); font-size: 11px; }
  .wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px;
          background: var(--panel); }
  table { border-collapse: collapse; width: 100%; white-space: nowrap; }
  th, td { padding: 5px 10px; text-align: right; border-bottom: 1px solid var(--line); }
  th { color: var(--dim); font-weight: 600; position: sticky; top: 0;
       background: var(--panel); font-size: 11px; }
  th:first-child, td:first-child, .l { text-align: left; }
  tr:last-child td { border-bottom: 0; }
  .dim { color: var(--dim); }
  .bad { color: var(--bad); } .warn { color: var(--warn); } .good { color: var(--good); }
  button, select { background: var(--panel); color: var(--text);
                   border: 1px solid var(--line); border-radius: 6px;
                   padding: 5px 10px; font: inherit; cursor: pointer; }
  #err { color: var(--bad); }
  .bar { display: inline-flex; height: 9px; width: 150px; border-radius: 3px;
         overflow: hidden; background: #0c0e13; vertical-align: middle; }
  .bar i { display: block; height: 100%; }
  .k { color: var(--accent); }
</style>
</head>
<body>
<header>
  <h1>ALFRED · PROFILE</h1>
  <span class="dim" id="range"></span>
  <label class="dim">rows <select id="limit">
    <option>25</option><option selected>50</option><option>100</option><option>250</option>
  </select></label>
  <label class="dim"><input type="checkbox" id="auto"> auto</label>
  <button id="reload">reload</button>
  <span id="err"></span>
</header>
<main id="out"><p class="dim">loading…</p></main>
<script>
(function () {
  var out = document.getElementById('out');
  var err = document.getElementById('err');
  var secret = decodeURIComponent((location.hash || '').replace(/^#/, ''));

  function n(v) { return (v === null || v === undefined) ? '—' : v; }
  function ms(v) {
    if (v === null || v === undefined) return '—';
    return v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + 's' : Math.round(v) + 'ms';
  }
  function num(v) {
    if (!v && v !== 0) return '—';
    return v >= 1e6 ? (v / 1e6).toFixed(1) + 'M'
         : v >= 1000 ? (v / 1000).toFixed(1) + 'k' : String(v);
  }
  /* A size and the count it is spread over. Gated on the value being absent
     rather than falsy: a history of forty tool-call messages whose text is
     empty is 0 chars over 40 messages, and rendering that as '—' says the
     turn had no history at all. */
  function pair(v, sub) {
    if (!v && v !== 0) return '—';
    return num(v) + '<span class="dim">/' + num(sub) + '</span>';
  }
  function when(ts) {
    if (!ts) return '—';
    var d = new Date(ts * 1000);
    return d.toTimeString().slice(0, 8);
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function pct(v) {
    if (v === null || v === undefined) return '<span class="dim">—</span>';
    var c = v >= 80 ? 'good' : v >= 40 ? 'warn' : 'bad';
    return '<span class="' + c + '">' + v.toFixed(0) + '%</span>';
  }
  /* Where one turn's wall time went, as one cell. The eye finds the turn whose
     bar is mostly retry-wait or mostly "other" far faster than it finds the
     same fact in four number columns. */
  function split(r) {
    var t = Math.max(1, r.duration_ms);
    function seg(v, color) {
      return '<i style="width:' + (100 * (v || 0) / t) + '%;background:' + color + '"></i>';
    }
    return '<span class="bar" title="llm ' + ms(r.llm_ms) + ' · wait '
      + ms(r.retry_wait_ms) + ' · tools ' + ms(r.tools_wall_ms) + ' · other '
      + ms(r.other_ms) + '">'
      + seg(r.llm_ms, '#7aa2f7') + seg(r.retry_wait_ms, '#f7768e')
      + seg(r.tools_wall_ms, '#9ece6a') + seg(r.other_ms, '#565f89') + '</span>';
  }

  function table(cols, rows) {
    if (!rows.length) {
      /* "nothing yet" alone reads as a broken panel. Nothing is persisted, so
         an instance that has been quiet since its last restart genuinely has
         nothing to show, and saying so is the difference between a panel that
         is empty and one that is broken. */
      return '<p class="dim">nothing since the last restart</p>';
    }
    var h = '<div class="wrap"><table><thead><tr>' + cols.map(function (c) {
      return '<th' + (c.l ? ' class="l"' : '') + '>' + esc(c.h) + '</th>';
    }).join('') + '</tr></thead><tbody>';
    h += rows.map(function (r) {
      return '<tr>' + cols.map(function (c) {
        return '<td' + (c.l ? ' class="l"' : '') + '>' + c.f(r) + '</td>';
      }).join('') + '</tr>';
    }).join('');
    return h + '</tbody></table></div>';
  }

  function cards(t) {
    var items = [
      ['turns & tasks', t.spans], ['llm calls', t.calls],
      ['p50 turn', ms(t.span_ms.p50)], ['p95 turn', ms(t.span_ms.p95)],
      ['prompt tk', num((t.tokens || {}).prompt_tokens)],
      ['completion tk', num((t.tokens || {}).completion_tokens)],
      ['cache hit', t.cache_hit_pct === null ? '—' : t.cache_hit_pct + '%'],
      ['retries', t.retries], ['retry wait', ms(t.retry_wait_ms)],
      ['fallbacks', t.fallbacks], ['call errors', t.call_errors],
      ['empty replies', t.empty_replies], ['truncated', t.truncated_replies],
      ['tool errors', t.tool_errors],
    ];
    return '<div class="cards">' + items.map(function (kv) {
      return '<div class="card"><b>' + esc(kv[1]) + '</b><span>' + kv[0] + '</span></div>';
    }).join('') + '</div>';
  }

  var SPAN_COLS = [
    { h: 'time', l: 1, f: function (r) { return when(r.ts); } },
    { h: 'kind', l: 1, f: function (r) {
        return (r.running ? '<span class="warn">▶ </span>' : '')
          + '<span class="k">' + esc(r.kind) + '</span>'
          + (r.label ? ' <span class="dim">' + esc(r.label) + '</span>' : ''); } },
    { h: 'session', l: 1, f: function (r) {
        return '<span class="dim">' + esc(r.session_key) + '</span>'; } },
    { h: 'model', l: 1, f: function (r) { return esc(r.model); } },
    { h: 'total', f: function (r) {
        return (r.running ? '<span class="warn">' + ms(r.duration_ms) + '…</span>'
                          : ms(r.duration_ms)); } },
    { h: 'where', l: 1, f: split },
    { h: 'llm', f: function (r) { return ms(r.llm_ms); } },
    { h: 'wait', f: function (r) {
        return r.retry_wait_ms ? '<span class="bad">' + ms(r.retry_wait_ms) + '</span>' : '—'; } },
    { h: 'tools', f: function (r) { return r.tools_wall_ms ? ms(r.tools_wall_ms) : '—'; } },
    { h: 'other', f: function (r) { return ms(r.other_ms); } },
    { h: 'calls', f: function (r) { return r.calls; } },
    { h: 'n tools', f: function (r) { return r.tools || '—'; } },
    { h: 'prompt', f: function (r) { return num((r.tokens || {}).prompt_tokens); } },
    { h: 'out', f: function (r) { return num((r.tokens || {}).completion_tokens); } },
    { h: 'cache', f: function (r) { return pct(r.cache_hit_pct); } },
    /* What that prompt is made of, in characters. The token count is the bill;
       these three are the reason for it — and the first time this was asked of
       a live turn the answer was "the history is 3% of it", which is not where
       anyone was looking. */
    { h: 'sys', f: function (r) { return num(r.system_chars); } },
    { h: 'tools def', f: function (r) { return pair(r.tool_chars, r.tool_count); } },
    { h: 'hist', f: function (r) { return pair(r.history_chars, r.history_msgs); } },
    { h: 'stop', l: 1, f: function (r) {
        return '<span class="dim">' + esc(r.stop_reason || '') + '</span>'; } },
  ];

  var CALL_COLS = [
    { h: 'time', l: 1, f: function (r) { return when(r.ts); } },
    { h: 'scope', l: 1, f: function (r) { return '<span class="k">' + esc(r.scope) + '</span>'; } },
    { h: 'model', l: 1, f: function (r) {
        var s = esc(r.served_by);
        if (r.served_by !== r.model) {
          s = '<span class="warn">' + s + '</span> <span class="dim">← ' + esc(r.model) + '</span>';
        }
        return s; } },
    { h: 'total', f: function (r) { return ms(r.duration_ms); } },
    { h: 'req', f: function (r) { return ms(r.request_ms); } },
    { h: 'wait', f: function (r) {
        return r.retry_wait_ms ? '<span class="bad">' + ms(r.retry_wait_ms) + '</span>' : '—'; } },
    { h: 'tries', f: function (r) {
        return r.attempts > 1 ? '<span class="bad">' + r.attempts + '</span>' : r.attempts; } },
    { h: 'msgs', f: function (r) { return r.messages; } },
    { h: 'prompt', f: function (r) { return num((r.tokens || {}).prompt_tokens); } },
    { h: 'cached', f: function (r) { return num((r.tokens || {}).cached_tokens); } },
    { h: 'cache', f: function (r) { return pct(r.cache_hit_pct); } },
    { h: 'out', f: function (r) { return num((r.tokens || {}).completion_tokens); } },
    { h: 'think', f: function (r) { return num((r.tokens || {}).reasoning_tokens); } },
    { h: 'chars', f: function (r) { return num(r.content_chars); } },
    { h: 'calls', f: function (r) { return r.tool_calls || '—'; } },
    { h: 'finish', l: 1, f: function (r) {
        var cls = r.finish_reason === 'error' ? 'bad'
                : (r.empty || r.truncated) ? 'warn' : 'dim';
        var extra = r.empty ? ' EMPTY' : r.truncated ? ' CUT' : '';
        return '<span class="' + cls + '">' + esc(r.finish_reason) + extra + '</span>'; } },
  ];

  function kv(title, obj, cols) {
    var rows = Object.keys(obj || {}).map(function (k) {
      var v = obj[k]; v.name = k; return v;
    });
    return '<h2>' + title + '</h2>' + table(cols, rows);
  }

  function render(d) {
    var h = '';
    h += '<h2>totals</h2>' + cards(d.totals);
    h += '<h2>turns — most recent</h2>' + table(SPAN_COLS, d.recent_spans);
    h += '<h2>turns — slowest held</h2>' + table(SPAN_COLS, d.slowest_spans);
    h += '<h2>llm calls — most recent</h2>' + table(CALL_COLS, d.recent_calls);
    h += '<h2>llm calls — slowest held</h2>' + table(CALL_COLS, d.slowest_calls);
    h += kv('by model', d.by_model, [
      { h: 'model', l: 1, f: function (r) { return esc(r.name); } },
      { h: 'gateway', l: 1, f: function (r) {
          return '<span class="dim">' + esc(r.gateway) + '</span>'; } },
      { h: 'calls', f: function (r) { return r.calls; } },
      { h: 'errors', f: function (r) { return r.errors || '—'; } },
      { h: 'retries', f: function (r) { return r.retries || '—'; } },
      { h: 'p50', f: function (r) { return ms(r.p50_ms); } },
      { h: 'p95', f: function (r) { return ms(r.p95_ms); } },
      { h: 'prompt', f: function (r) { return num(r.tokens.prompt_tokens); } },
      { h: 'out', f: function (r) { return num(r.tokens.completion_tokens); } },
      { h: 'think', f: function (r) { return num(r.tokens.reasoning_tokens); } },
      { h: 'cache', f: function (r) { return pct(r.cache_hit_pct); } },
    ]);
    h += kv('by kind', d.by_kind, [
      { h: 'kind', l: 1, f: function (r) { return esc(r.name); } },
      { h: 'count', f: function (r) { return r.count; } },
      { h: 'p50', f: function (r) { return ms(r.p50_ms); } },
      { h: 'p95', f: function (r) { return ms(r.p95_ms); } },
      { h: 'total', f: function (r) { return ms(r.total_ms); } },
    ]);
    h += kv('by chat', d.by_chat, [
      { h: 'session', l: 1, f: function (r) { return esc(r.name); } },
      { h: 'last', l: 1, f: function (r) { return when(r.last_ts); } },
      { h: 'turns', f: function (r) { return r.spans; } },
      { h: 'calls', f: function (r) { return r.calls; } },
      { h: 'p50', f: function (r) { return ms(r.p50_ms); } },
      { h: 'p95', f: function (r) { return ms(r.p95_ms); } },
      { h: 'prompt', f: function (r) { return num(r.tokens.prompt_tokens); } },
      { h: 'per call', f: function (r) { return num(r.prompt_per_call); } },
      { h: 'cache', f: function (r) { return pct(r.cache_hit_pct); } },
      { h: 'models', l: 1, f: function (r) {
          return '<span class="dim">' + esc(Object.keys(r.models).join(' ')) + '</span>'; } },
    ]);
    h += kv('by tool', d.by_tool, [
      { h: 'tool', l: 1, f: function (r) { return esc(r.name); } },
      { h: 'calls', f: function (r) { return r.calls; } },
      { h: 'errors', f: function (r) {
          return r.errors ? '<span class="bad">' + r.errors + '</span>' : '—'; } },
      { h: 'p50', f: function (r) { return ms(r.p50_ms); } },
      { h: 'p95', f: function (r) { return ms(r.p95_ms); } },
      { h: 'total', f: function (r) { return ms(r.total_ms); } },
      { h: 'result', f: function (r) { return num(r.result_chars); } },
    ]);
    out.innerHTML = h;
    document.getElementById('range').textContent =
      'since ' + when(d.since) + ' · ' + d.counts.spans + ' finished · '
      + (d.counts.running || 0) + ' running · ' + d.counts.calls + ' calls · '
      + d.counts.tools + ' tools' + (d.enabled ? '' : ' · DISABLED');
  }

  async function load() {
    err.textContent = '';
    try {
      /* Relative on purpose. Served from nanobot this resolves to
         /v1/debug/profile exactly as before; served through HomeWeb's
         admin proxy at /alfred/profile.html it resolves to /alfred/profile,
         which is the same data with the secret added on the server side and
         never in the browser. One page, two doors.

         The page's own query has to be carried across by hand: a relative
         reference with a query string of its own REPLACES the base's, so
         `fetch('profile?limit=50')` from /alfred/profile.html?id=3 asks for
         /alfred/profile?limit=50 with no `id` — and the proxy then falls back
         to the viewer's own instance. That rendered one instance's page over
         another instance's numbers, silently, on the one flow the feature
         exists for: diagnosing somebody else's slow turn. */
      var q = new URLSearchParams(location.search);
      q.set('limit', document.getElementById('limit').value);
      var r = await fetch('profile?' + q.toString(),
                          secret ? { headers: { 'X-Debug-Secret': secret } } : {});
      var body = await r.json();
      if (!r.ok) { err.textContent = body.error || ('HTTP ' + r.status); return; }
      render(body);
    } catch (e) {
      err.textContent = String(e && e.message || e);
    }
  }

  document.getElementById('reload').addEventListener('click', load);
  document.getElementById('limit').addEventListener('change', load);
  var timer = null;
  document.getElementById('auto').addEventListener('change', function (e) {
    clearInterval(timer);
    if (e.target.checked) timer = setInterval(load, 5000);
  });
  load();
})();
</script>
</body>
</html>
"""

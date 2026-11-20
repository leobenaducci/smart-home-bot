'use strict';
// Dispositivos / Devices. Relative URLs throughout: the portal carries this
// page under its own prefix.
const ICONS = { light:'💡', switch:'🔌', plug:'🔌', fan:'🌀', cover:'🪟', climate:'🌡️', lock:'🔒',
  media:'📺', vacuum:'🧹', sensor:'📟', binary_sensor:'🚪', button:'🔘', camera:'📷', other:'❔' };
let devices = [], csrf = '';
const t = (k, p) => window.i18n.t(k, p);

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (opts.method && opts.method !== 'GET') headers['X-CSRF-Token'] = csrf;
  const res = await fetch(path, { ...opts, headers });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || res.status);
  return body;
}

function stateText(d) {
  const first = d.primary_state || Object.values(d.states || {})[0];
  if (!first) return '';
  const unit = first.attributes.unit_of_measurement || '';
  return `${first.state}${unit ? ' ' + unit : ''}`;
}

function row(d) {
  const el = document.getElementById('row').content.firstElementChild.cloneNode(true);
  el.classList.toggle('missing', !!d.missing);
  el.querySelector('.icon').textContent = ICONS[d.kind] || ICONS.other;
  const name = el.querySelector('.name'), room = el.querySelector('.room');
  name.value = d.name; room.value = d.room;
  room.placeholder = t('devices.room');
  const found = d.discovered || {};
  const via = (d.routes || []).map(r => r.backend.toUpperCase() + (r.missing ? '?' : '')).join(' · ');
  el.querySelector('.info').textContent =
    [via, [found.manufacturer, found.model].filter(Boolean).join(' '), d.missing ? t('devices.missing') : '']
      .filter(Boolean).join(' — ');
  el.querySelector('.state').textContent = stateText(d);
  const save = (field, input) => input.addEventListener('change', () =>
    update(d.id, { [field]: input.value }).catch(e => alert(e.message)));
  save('name', name); save('room', room);
  const actions = el.querySelector('.actions');
  const button = (label, status, primary) => {
    const b = document.createElement('button');
    b.textContent = t(label); if (primary) b.className = 'primary';
    b.addEventListener('click', () => update(d.id, { status }).catch(e => alert(e.message)));
    actions.append(b);
  };
  if (d.status === 'active' && (d.actions || []).includes('toggle')) {
    const st = (d.primary_state || {}).state;
    const on = ['on', 'open', 'playing', 'heat', 'cool', 'auto', 'cleaning'].includes(st);
    const b = document.createElement('button');
    b.className = 'power' + (on ? ' on' : '');
    b.textContent = on ? t('devices.turn_off') : t('devices.turn_on');
    b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        const { device } = await api(`api/devices/${encodeURIComponent(d.id)}/action`,
          { method: 'POST', body: JSON.stringify({ action: on ? 'off' : 'on' }) });
        devices = devices.map(x => x.id === d.id ? device : x); render();
      } catch (e) { alert(e.message); b.disabled = false; }
    });
    actions.append(b);
  }
  if ((d.actions || []).includes('identify')) {
    const b = document.createElement('button');
    b.textContent = t('devices.identify'); b.title = t('devices.identify_hint');
    b.addEventListener('click', async () => {
      b.disabled = true;
      try { await api(`api/devices/${encodeURIComponent(d.id)}/action`,
        { method: 'POST', body: JSON.stringify({ action: 'identify' }) }); }
      catch (e) { alert(e.message); }
      b.disabled = false;
    });
    actions.append(b);
  }
  if (d.status !== 'ignored') {
    const sel = document.createElement('select');
    sel.className = 'merge';
    const others = devices.filter(x => x.id !== d.id && x.status !== 'ignored' &&
      ['light', 'plug', 'switch'].includes(x.kind) === ['light', 'plug', 'switch'].includes(d.kind));
    sel.append(new Option(t('devices.same_as'), ''));
    others.sort((a, b) => a.name.localeCompare(b.name))
      .forEach(x => sel.append(new Option(`${x.name}${x.room ? ' — ' + x.room : ''}`, x.id)));
    sel.addEventListener('change', async () => {
      const into = devices.find(x => x.id === sel.value);
      if (!into || !confirm(t('devices.merge_confirm', { a: d.name, b: into.name }))) { sel.value = ''; return; }
      try {
        const { device } = await api(`api/devices/${encodeURIComponent(d.id)}/merge`,
          { method: 'POST', body: JSON.stringify({ into: into.id }) });
        devices = devices.filter(x => x.id !== d.id).map(x => x.id === device.id ? device : x); render();
      } catch (e) { alert(e.message); sel.value = ''; }
    });
    actions.append(sel);
  }
  if (d.status === 'new') { button('devices.accept', 'active', true); button('devices.ignore', 'ignored'); }
  else if (d.status === 'active') button('devices.ignore', 'ignored');
  else button('devices.restore', 'active');
  return el;
}

async function update(id, changes) {
  const { device } = await api(`api/devices/${encodeURIComponent(id)}`,
    { method: 'PATCH', body: JSON.stringify(changes) });
  devices = devices.map(d => d.id === id ? device : d);
  render();
}

function render() {
  const q = document.getElementById('filter').value.trim().toLowerCase();
  const match = d => !q || `${d.name} ${d.room} ${d.kind}`.toLowerCase().includes(q);
  const groups = { new: [], active: [], ignored: [] };
  devices.forEach(d => (groups[d.status] || groups.new).push(d));
  const fill = (id, list) => document.getElementById(id).replaceChildren(...list.map(row));
  fill('new-list', groups.new);
  fill('active-list', groups.active.filter(match));
  fill('ignored-list', groups.ignored);
  document.getElementById('new-section').hidden = !groups.new.length;
  document.getElementById('new-count').textContent = groups.new.length ? `(${groups.new.length})` : '';
  document.getElementById('ignored-count').textContent = groups.ignored.length ? `(${groups.ignored.length})` : '';
  document.getElementById('empty').hidden = devices.length > 0;
}

async function refresh() {
  // Skip while someone is typing in a row, so a refresh does not eat the edit.
  if (document.activeElement && document.activeElement.closest('.row')) return;
  devices = (await api('api/devices')).devices;
  render();
}

(async () => {
  const cfg = await api('api/config');
  await window.i18n.load('static/i18n', cfg.locale);
  document.documentElement.lang = cfg.locale;
  window.i18n.apply();
  const pill = document.getElementById('backend');
  const ok = cfg.backends.ha === 'connected';
  pill.textContent = `Home Assistant: ${ok ? t('devices.connected') : cfg.backends.ha} · WiZ: ${
    cfg.backends.wiz === 'running' ? t('devices.connected') : cfg.backends.wiz}`;
  pill.classList.toggle('bad', !ok);
  try { csrf = (await (await fetch('_csrf')).json()).csrf || ''; } catch { /* outside the portal */ }
  document.getElementById('filter').addEventListener('input', render);
  await refresh();
  setInterval(() => refresh().catch(() => {}), 10000);
})();

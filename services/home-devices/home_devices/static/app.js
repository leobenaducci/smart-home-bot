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
  const first = Object.values(d.states || {})[0];
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
  const via = (d.routes || []).map(r => r.backend.toUpperCase()).join(' · ');
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
  pill.textContent = `Home Assistant: ${ok ? t('devices.connected') : cfg.backends.ha}`;
  pill.classList.toggle('bad', !ok);
  try { csrf = (await (await fetch('_csrf')).json()).csrf || ''; } catch { /* outside the portal */ }
  document.getElementById('filter').addEventListener('input', render);
  await refresh();
  setInterval(() => refresh().catch(() => {}), 10000);
})();

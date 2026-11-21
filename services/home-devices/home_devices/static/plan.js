'use strict';
// The floor plan: the house's DXF drawn as SVG, devices on top as HTML
// markers (so they stay finger-sized at any zoom). Tap a marker to switch it;
// in "Move devices" mode, drag markers around, onto the plan from the tray,
// or off the plan to unplace them.
//
// Shares app.js's globals: devices, api, csrf, t, ICONS, render.
const SVGNS = 'http://www.w3.org/2000/svg';
const ON_STATES = ['on', 'open', 'playing', 'heat', 'cool', 'auto', 'cleaning'];
const plan = { list: [], doc: null, id: null, view: null, editing: false, dragging: false };

function store(key, value) {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); }
  catch { return null; }
}

async function upload(path, file, method = 'POST') {
  const res = await fetch(path, { method, body: file,
    headers: { 'Content-Type': 'application/octet-stream', 'X-CSRF-Token': csrf } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.code === 'plan_unreadable'
    ? t('devices.plan_unreadable', { reason: body.error }) : (body.error || res.status));
  return body;
}

// --- drawing the plan --------------------------------------------------------

function el(tag, attrs = {}) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

// DXF is y-up, SVG y-down: every y is negated once, here.
const pt = p => `${p[0]},${-p[1]}`;

function drawDrawing() {
  const svg = document.getElementById('plan-svg');
  svg.replaceChildren();
  const doc = plan.doc;
  if (!doc) return;
  const rooms = el('g', { class: 'rooms' });
  const tint = roomTint(doc);
  if (tint) rooms.append(tint);
  const lines = el('g', { class: 'lines' });
  doc.lines.forEach(ln => lines.append(el(ln.closed ? 'polygon' : 'polyline',
    { points: ln.pts.map(pt).join(' ') })));
  const texts = el('g', { class: 'texts' });
  const ANCHOR = ['start', 'middle', 'end'], BASE = ['hanging', 'central', 'alphabetic'];
  doc.texts.forEach(tx => {
    const a = (tx.attach || 7) - 1;
    const node = el('text', { x: tx.x, y: -tx.y, 'font-size': tx.h, 'text-anchor': ANCHOR[a % 3],
      'dominant-baseline': BASE[Math.floor(a / 3)] });
    if (tx.rot) node.setAttribute('transform', `rotate(${-tx.rot} ${tx.x} ${-tx.y})`);
    tx.text.split('\n').forEach((line, i) => {
      const span = el('tspan', { x: tx.x, dy: i ? '1.25em' : 0 });
      span.textContent = line;
      node.append(span);
    });
    texts.append(node);
  });
  svg.append(rooms, lines, texts);
}

// The rooms the server found, one faint colour each: the grid painted onto a
// canvas once, then laid under the drawing as an image.
const ROOM_COLOURS = [[47, 111, 222], [222, 140, 47], [60, 170, 110], [170, 80, 200], [210, 70, 90],
  [40, 170, 190], [150, 150, 40], [120, 100, 220]];
function roomTint(doc) {
  const g = doc.grid;
  if (!g || !g.runs) return null;
  const canvas = document.createElement('canvas');
  canvas.width = g.w; canvas.height = g.h;
  const ctx = canvas.getContext('2d'), img = ctx.createImageData(g.w, g.h);
  let i = 0;
  for (let k = 0; k < g.runs.length; k += 2) {
    const room = g.runs[k], n = g.runs[k + 1];
    if (room >= 0) {
      const [r, gr, b] = ROOM_COLOURS[room % ROOM_COLOURS.length];
      for (let j = i; j < i + n; j++) {
        // Grid rows run bottom to top; the canvas's top to bottom.
        const row = g.h - 1 - Math.floor(j / g.w), o = (row * g.w + (j % g.w)) * 4;
        img.data[o] = r; img.data[o + 1] = gr; img.data[o + 2] = b; img.data[o + 3] = 255;
      }
    }
    i += n;
  }
  ctx.putImageData(img, 0, 0);
  return el('image', { href: canvas.toDataURL(), x: g.x0, y: -(g.y0 + g.h * g.cell),
    width: g.w * g.cell, height: g.h * g.cell, preserveAspectRatio: 'none' });
}

function fit() {
  if (!plan.doc) return;
  const [x0, y0, x1, y1] = plan.doc.bounds;
  const pad = Math.max(x1 - x0, y1 - y0) * 0.04 || 1;
  setView({ x: x0 - pad, y: -y1 - pad, w: x1 - x0 + 2 * pad, h: y1 - y0 + 2 * pad });
}

function setView(v) {
  plan.view = v;
  document.getElementById('plan-svg').setAttribute('viewBox', `${v.x} ${v.y} ${v.w} ${v.h}`);
  placeMarkers();
}

// Screen pixels <-> drawing units, through the SVG's own matrix (which
// already accounts for preserveAspectRatio's letterboxing).
function toScreen(x, y) {
  const svg = document.getElementById('plan-svg');
  const m = svg.getScreenCTM(), box = document.getElementById('plan-stage').getBoundingClientRect();
  if (!m) return null;
  const p = new DOMPoint(x, -y).matrixTransform(m);
  return { left: p.x - box.left, top: p.y - box.top };
}

function toDrawing(clientX, clientY) {
  const m = document.getElementById('plan-svg').getScreenCTM();
  if (!m) return null;
  const p = new DOMPoint(clientX, clientY).matrixTransform(m.inverse());
  return { x: Math.round(p.x * 1000) / 1000, y: Math.round(-p.y * 1000) / 1000 };
}

// --- device markers ------------------------------------------------------------

function isOn(d) { return ON_STATES.includes((d.primary_state || {}).state); }

function marker(d) {
  const b = document.createElement('button');
  b.className = 'marker' + (isOn(d) ? ' on' : '') + (d.missing ? ' missing' : '');
  b.textContent = ICONS[d.kind] || ICONS.other;
  b.title = `${d.name}${d.room ? ' — ' + d.room : ''}`;
  b.dataset.id = d.id;
  const label = document.createElement('span');
  label.className = 'label'; label.textContent = d.name;
  b.append(label);
  b.addEventListener('pointerdown', e => startDrag(e, d, b));
  return b;
}

function placeMarkers() {
  const layer = document.getElementById('plan-markers');
  if (!plan.doc) { layer.replaceChildren(); return; }
  const here = devices.filter(d => d.status === 'active' && (d.position || {}).plan === plan.id);
  layer.replaceChildren(...here.map(d => {
    const m = marker(d), s = toScreen(d.position.x, d.position.y);
    if (s) { m.style.left = `${s.left}px`; m.style.top = `${s.top}px`; }
    return m;
  }));
}

function fillTray() {
  const tray = document.getElementById('plan-tray');
  const loose = devices.filter(d => d.status === 'active' && !d.position)
    .sort((a, b) => (a.room || '~').localeCompare(b.room || '~') || a.name.localeCompare(b.name));
  document.getElementById('plan-tray-hint').textContent =
    loose.length ? t('devices.plan_unplaced') : t('devices.plan_all_placed');
  tray.replaceChildren(...loose.map(d => {
    const chip = document.createElement('button');
    chip.className = 'chip';
    chip.textContent = `${ICONS[d.kind] || ICONS.other} ${d.name}`;
    chip.addEventListener('pointerdown', e => startDrag(e, d, null));
    return chip;
  }));
}

// --- dragging, and tapping ---------------------------------------------------

function startDrag(e, d, markerEl) {
  if (e.button !== undefined && e.button !== 0) return;
  e.preventDefault(); e.stopPropagation();
  const x0 = e.clientX, y0 = e.clientY;
  let ghost = null;
  const move = ev => {
    if (!ghost && Math.hypot(ev.clientX - x0, ev.clientY - y0) < 6) return;
    if (!plan.editing) return;
    if (!ghost) {
      plan.dragging = true;
      ghost = document.createElement('div');
      ghost.className = 'marker ghost';
      ghost.textContent = ICONS[d.kind] || ICONS.other;
      document.body.append(ghost);
      if (markerEl) markerEl.style.visibility = 'hidden';
    }
    ghost.style.left = `${ev.clientX}px`; ghost.style.top = `${ev.clientY}px`;
  };
  const up = async ev => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    window.removeEventListener('pointercancel', up);
    plan.dragging = false;
    if (!ghost) {                       // a tap
      if (markerEl && !plan.editing) tap(d, markerEl);
      return;
    }
    ghost.remove();
    if (ev.type === 'pointercancel') { placeMarkers(); return; }
    const stage = document.getElementById('plan-stage').getBoundingClientRect();
    const inside = ev.clientX >= stage.left && ev.clientX <= stage.right &&
                   ev.clientY >= stage.top && ev.clientY <= stage.bottom;
    let changes;
    if (inside) {
      const p = toDrawing(ev.clientX, ev.clientY);
      if (!p) return;
      changes = { position: { plan: plan.id, ...p } };
    } else if (markerEl) {
      changes = { position: null };     // dragged off the plan
    } else {
      return;
    }
    try { await update(d.id, changes); }
    catch (err) { alert(err.message); placeMarkers(); }
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
  window.addEventListener('pointercancel', up);
}

async function tap(d, markerEl) {
  if (!(d.actions || []).includes('toggle')) return;
  markerEl.disabled = true;
  try {
    const { device } = await api(`api/devices/${encodeURIComponent(d.id)}/action`,
      { method: 'POST', body: JSON.stringify({ action: isOn(d) ? 'off' : 'on' }) });
    devices = devices.map(x => x.id === d.id ? device : x);
    render();
  } catch (err) { alert(err.message); markerEl.disabled = false; }
}

// --- panning and zooming -----------------------------------------------------

function zoom(factor, clientX, clientY) {
  const v = plan.view; if (!v) return;
  const c = toDrawing(clientX, clientY) || { x: v.x + v.w / 2, y: -(v.y + v.h / 2) };
  const cy = -c.y;
  setView({ x: c.x - (c.x - v.x) * factor, y: cy - (cy - v.y) * factor, w: v.w * factor, h: v.h * factor });
}

function wirePanZoom() {
  const stage = document.getElementById('plan-stage');
  const pointers = new Map();
  let last = null;
  stage.addEventListener('wheel', e => {
    if (!plan.view) return;
    e.preventDefault();
    zoom(Math.exp(e.deltaY * 0.0015), e.clientX, e.clientY);
  }, { passive: false });
  stage.addEventListener('pointerdown', e => {
    if (e.target.closest('.marker')) return;
    stage.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    last = null;
  });
  stage.addEventListener('pointermove', e => {
    if (!pointers.has(e.pointerId) || !plan.view) return;
    const prev = pointers.get(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const m = document.getElementById('plan-svg').getScreenCTM();
    if (!m) return;
    if (pointers.size === 1) {          // pan
      const v = plan.view;
      setView({ ...v, x: v.x - (e.clientX - prev.x) / m.a, y: v.y - (e.clientY - prev.y) / m.d });
    } else if (pointers.size === 2) {   // pinch
      const [a, b] = [...pointers.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      if (last) zoom(last / dist, (a.x + b.x) / 2, (a.y + b.y) / 2);
      last = dist;
    }
  });
  const end = e => { pointers.delete(e.pointerId); last = null; };
  stage.addEventListener('pointerup', end);
  stage.addEventListener('pointercancel', end);
  new ResizeObserver(() => placeMarkers()).observe(stage);
}

// --- plans: choosing, adding, replacing --------------------------------------

async function loadPlans(selectId) {
  plan.list = (await api('api/plans')).plans;
  const want = selectId || plan.id || store('devices.plan');
  const pick = plan.list.find(p => p.id === want) || plan.list[0];
  await choose(pick ? pick.id : null);
}

async function choose(id) {
  plan.id = id;
  plan.doc = id ? await api(`api/plans/${encodeURIComponent(id)}`) : null;
  if (id) store('devices.plan', id);
  drawTabs();
  drawDrawing();
  document.getElementById('plan-body').hidden = !plan.doc;
  if (plan.doc) requestAnimationFrame(fit);
  fillTray();
}

function drawTabs() {
  const tabs = document.getElementById('plan-tabs');
  tabs.replaceChildren(...plan.list.map(p => {
    const b = document.createElement('button');
    b.className = 'tab' + (p.id === plan.id ? ' current' : '');
    b.textContent = p.name;
    b.addEventListener('click', () => choose(p.id).catch(e => alert(e.message)));
    return b;
  }));
  document.getElementById('plan-menu').hidden = !plan.doc;
  document.getElementById('plan-edit').hidden = !plan.doc;
}

function pickFile(then) {
  const input = document.createElement('input');
  input.type = 'file'; input.accept = '.dxf,image/vnd.dxf,application/dxf';
  input.addEventListener('change', () => { if (input.files[0]) then(input.files[0]); });
  input.click();
}

function wirePlanButtons() {
  document.getElementById('plan-add').addEventListener('click', () => pickFile(async file => {
    const name = prompt(t('devices.plan_name_prompt'), file.name.replace(/\.dxf$/i, ''));
    if (name === null) return;
    try {
      const doc = await upload(`api/plans?name=${encodeURIComponent(name)}&source=${encodeURIComponent(file.name)}`, file);
      await loadPlans(doc.id);
    } catch (e) { alert(e.message); }
  }));
  document.getElementById('plan-replace').addEventListener('click', () => pickFile(async file => {
    try {
      await upload(`api/plans/${encodeURIComponent(plan.id)}?source=${encodeURIComponent(file.name)}`, file, 'PUT');
      await loadPlans(plan.id);
    } catch (e) { alert(e.message); }
  }));
  document.getElementById('plan-rename').addEventListener('click', async () => {
    const name = prompt(t('devices.plan_rename'), plan.doc.name);
    if (!name) return;
    try {
      await api(`api/plans/${encodeURIComponent(plan.id)}`, { method: 'PATCH', body: JSON.stringify({ name }) });
      await loadPlans(plan.id);
    } catch (e) { alert(e.message); }
  });
  document.getElementById('plan-delete').addEventListener('click', async () => {
    if (!confirm(t('devices.plan_delete_confirm', { name: plan.doc.name }))) return;
    try {
      await api(`api/plans/${encodeURIComponent(plan.id)}`, { method: 'DELETE' });
      plan.id = null;
      devices = (await api('api/devices')).devices;
      await loadPlans();
      render();
    } catch (e) { alert(e.message); }
  });
  document.getElementById('plan-fit').addEventListener('click', fit);
  document.getElementById('plan-edit').addEventListener('click', () => {
    plan.editing = !plan.editing;
    const b = document.getElementById('plan-edit');
    b.textContent = t(plan.editing ? 'devices.plan_done' : 'devices.plan_edit');
    b.classList.toggle('primary', plan.editing);
    document.getElementById('plan-tray-wrap').hidden = !plan.editing;
    document.getElementById('plan-stage').classList.toggle('editing', plan.editing);
  });
  document.querySelectorAll('#plan-menu button').forEach(b =>
    b.addEventListener('click', () => b.closest('details').removeAttribute('open')));
}

// Called by app.js after every render: the markers and the tray follow the list.
window.planRender = () => { placeMarkers(); fillTray(); };
window.planBusy = () => plan.dragging;

window.planStart = async () => {
  wirePlanButtons();
  wirePanZoom();
  await loadPlans();
};

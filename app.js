/* app.js - Aether Remote phone client.
 *
 * One flat layout {sections:[{tiles:[]}]} drives everything; the three
 * presentation modes only change how that same list is drawn, so switching
 * modes never rearranges a tile.
 */
'use strict';

let L = null;        // layout
let S = null;        // live state
let lib = null;      // library cache
let editing = false;
let dirty = false;
let dragging = null;
let armed = null, armTimer = null;
let suppressUntil = 0, draggingSlider = false;
let activeSec = null;
let spy = null;

const $ = (s) => document.querySelector(s);
const board = $('#board'), rail = $('#rail');

function toast(m){
  const t = $('#toast');
  t.textContent = m; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 1700);
}
function esc(x){
  return String(x == null ? '' : x).replace(/[&<>"]/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

async function api(path, body, method){
  const o = { method: method || (body ? 'POST' : 'GET'), headers:{} };
  if (body){ o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
  const r = await fetch(path, o);
  if (r.status === 401){ location.href = '/login'; throw new Error('logged out'); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok){ const e = new Error(j.error || ('HTTP ' + r.status)); e.data = j; throw e; }
  return j;
}

/* ---------- cell sizing: row height == column width ---------- */
function sizeGrid(){
  // body is capped at 460px, so measure the BODY not the window - otherwise
  // the cell size is computed from a 1920px desktop window and every tile
  // overflows its column.
  const w = document.body.clientWidth - 28;
  const cell = (w - 3 * 8) / 4;
  document.documentElement.style.setProperty('--cell', cell.toFixed(2) + 'px');
}
window.addEventListener('resize', () => { sizeGrid(); });

/* ---------- theme ---------- */
const HEX = /^#[0-9a-fA-F]{6}$/;

function applyTheme(t){
  if (!t) return;
  const r = document.documentElement.style;
  // Only --primary/--secondary/--bg are set; the stylesheet derives panels,
  // borders, glows and gradients from them with color-mix.
  r.setProperty('--primary', HEX.test(t.primary || '') ? t.primary : '#7c3aed');
  r.setProperty('--secondary', HEX.test(t.secondary || '') ? t.secondary : '#22d3ee');
  r.setProperty('--bg', HEX.test(t.bg || '') ? t.bg : '#01020a');
  r.setProperty('--r', (t.radius == null ? 14 : t.radius) + 'px');
  const meta = document.querySelector('meta[name=theme-color]');
  if (meta) meta.setAttribute('content', HEX.test(t.bg || '') ? t.bg : '#01020a');
  document.body.classList.toggle('glass', t.glass !== false);
  document.body.classList.toggle('noscan', t.scanlines === false);
  document.body.classList.toggle('nolabels', t.gameLabels === false);
}

/* ---------- rendering ---------- */
function tileInner(t){
  switch (t.kind){

  case 'slider': {
    const v = S ? S.volume : 0;
    return `<div class="slid">
      <div class="top"><div class="v">${v}</div><div class="u">%</div>
        <div class="k">${esc(t.label || 'Volume')}</div></div>
      <input type="range" min="0" max="100" value="${v}" data-slider="1"
             style="--pct:${v}%">
    </div>`;
  }

  case 'game': {
    const src = `/api/art?id=${encodeURIComponent(t.ref)}`;
    return `<div class="game">
      <img src="${src}" alt="" loading="lazy"
           onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
      <div class="fallback" style="display:none"></div>
      <div class="cap">${esc(t.label)}</div>
      ${isRunning(t) ? '<div class="badge"><i></i>Running</div>' : ''}
    </div>`;
  }

  case 'app': {
    if (t.w >= 2 && t.h >= 2){
      return `<div class="ico">
        <img class="thumb" style="width:40px;height:40px;border-radius:11px"
             src="/api/art?id=${encodeURIComponent(t.ref)}" alt=""
             onerror="this.style.visibility='hidden'">
        <div class="lab">${esc(t.label)}</div></div>`;
    }
    if (t.w === 1){
      return `<div class="ico">
        <img class="thumb" style="width:26px;height:26px"
             src="/api/art?id=${encodeURIComponent(t.ref)}" alt=""
             onerror="this.style.visibility='hidden'">
        <div class="lab">${esc(t.label)}</div></div>`;
    }
    return `<div class="row">
      <img class="thumb" src="/api/art?id=${encodeURIComponent(t.ref)}" alt=""
           onerror="this.style.visibility='hidden'">
      <div style="min-width:0"><div class="nm">${esc(t.label)}</div>
        <div class="sub">${isRunning(t) ? 'Running' : 'App'}</div></div></div>`;
  }

  case 'toggle': {
    const on = toggleOn(t.ref);
    const sub = t.ref === 'keeper' && S && S.keeper && S.keeper.enabled
      ? S.keeper.target + '%' : '';
    if (t.w === 1){
      return `<div class="ico">${iconFor(t.ref)}
        <div class="lab">${esc(sub || t.label)}</div></div>`;
    }
    return `<div class="row">${iconFor(t.ref)}
      <div style="flex-grow:1;min-width:0">
        <div class="nm">${esc(t.label)}</div>
        ${sub ? `<div class="sub">held at ${esc(sub)}</div>` : ''}</div>
      <div class="sw ${on ? 'on' : ''}" style="pointer-events:none"><i></i></div></div>`;
  }

  case 'action': {
    if (t.w === 1) return `<div class="ico">${iconFor(t.ref)}<div class="lab">${esc(t.label)}</div></div>`;
    return `<div class="row">${iconFor(t.ref)}
      <div class="nm" style="flex-grow:1">${esc(t.label)}</div></div>`;
  }

  case 'stream':
    return `<div class="strm">
      <div class="hint">desktop preview<br><span style="font-size:10px;color:#475569">tap to open</span></div>
    </div>`;

  case 'stat': {
    const m = (S && S.memory) || {usedGb:0, totalGb:0, percent:0};
    return `<div class="stat">
      <div><div class="k">RAM</div>
        <div class="v">${m.usedGb}<span style="font-size:10px;color:var(--muted);font-weight:400"> / ${m.totalGb} GB</span></div></div>
      <div class="bars">${[38,52,44,68,m.percent].map(
        (h,i) => `<i style="height:${Math.max(8,h)}%;${i===4?'background:var(--accent2)':''}"></i>`).join('')}</div>
    </div>`;
  }

  case 'scene':
    return `<div class="row">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--cyan)" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.8H20l-4.9 3.6 1.9 5.8L12 14.6 7 18.2l1.9-5.8L4 8.8h6.1z"/></svg>
      <div class="nm" style="flex-grow:1">${esc(t.label)}</div></div>`;

  default:
    return `<div class="ico"><div class="lab">${esc(t.label || t.kind)}</div></div>`;
  }
}

function iconFor(ref){
  const s = 'width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"';
  const map = {
    'mute': `<svg ${s}><path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M22 9l-6 6"/><path d="M16 9l6 6"/></svg>`,
    'keeper': `<svg ${s}><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>`,
    'media.playpause': `<svg ${s}><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>`,
    'media.next': `<svg ${s}><path d="M5 4l10 8-10 8z"/><path d="M19 5v14"/></svg>`,
    'media.prev': `<svg ${s}><path d="M19 4L9 12l10 8z"/><path d="M5 5v14"/></svg>`,
    'screen.off': `<svg ${s}><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8"/></svg>`,
    'power.lock': `<svg ${s}><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>`,
    'power.sleep': `<svg ${s}><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>`,
    'power.restart': `<svg ${s}><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>`,
    'power.shutdown': `<svg ${s}><path d="M12 3v9"/><path d="M6.4 6.4a9 9 0 1 0 11.2 0"/></svg>`,
    'power.signout': `<svg ${s}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>`,
  };
  return `<span style="color:var(--text);display:flex">${map[ref] || `<svg ${s}><circle cx="12" cy="12" r="9"/></svg>`}</span>`;
}

function toggleOn(ref){
  if (!S) return false;
  if (ref === 'mute') return !!S.muted;
  if (ref === 'keeper') return !!(S.keeper && S.keeper.enabled);
  return false;
}

function isRunning(t){
  if (!S || !S.foreground) return false;
  const p = (S.foreground.process || '').toLowerCase().replace('.exe','');
  const n = (t.label || '').toLowerCase();
  return p.length > 2 && n.includes(p);
}

function render(){
  if (!L) return;
  sizeGrid();
  applyTheme(L.theme);

  if (!activeSec || !L.sections.some(s => s.id === activeSec))
    activeSec = L.sections.length ? L.sections[0].id : null;

  rail.innerHTML = L.sections.map(s =>
    `<div class="chip ${s.id === activeSec ? 'on' : ''}" data-jump="${esc(s.id)}">${esc(s.name)}</div>`
  ).join('') + (editing
    ? `<div class="chip" data-addsec="1" style="border-style:dashed;color:var(--accent2)">+ Section</div>` : '');
  rail.style.display = (L.mode === 'scroll' || L.sections.length < 2) ? 'none' : 'flex';

  board.innerHTML = L.sections.map(sec => `
    <section class="sec" id="sec-${esc(sec.id)}">
      <div class="sechead"><div class="t">${esc(sec.name)}</div><div class="l"></div>
        <div class="n">${sec.tiles.length}</div></div>
      <div class="grid" data-sec="${esc(sec.id)}">
        ${sec.tiles.map(t => tileHTML(t, sec.id)).join('')}
        ${editing ? `<div class="tile" data-addtile="${esc(sec.id)}"
            style="border-style:dashed;border-color:var(--line2);grid-column:span 2">
            <div class="ico"><div class="lab" style="color:var(--accent2)">+ Add tile</div></div></div>` : ''}
      </div>
    </section>`).join('');

  $('#fab').innerHTML = editing
    ? `<button class="btn" id="cancelEdit">Cancel</button>
       <button class="btn pri" id="saveEdit">Done</button>`
    : '';

  wireSpy();
  refresh();
}

function tileHTML(t, secId){
  const cls = ['tile'];
  if (editing) cls.push('edit');
  if (!editing && t.kind === 'toggle' && toggleOn(t.ref)) cls.push('on');
  if (t.kind === 'action' && /restart|shutdown|signout/.test(t.ref)) cls.push('danger');
  if (armed === t.id) cls.push('armed');
  return `<div class="${cls.join(' ')}" data-tile="${t.id}" data-sec="${secId}"
    style="grid-column:span ${t.w};grid-row:span ${t.h}">
    ${tileInner(t)}
    ${editing ? '<div class="rm" data-rm="1"><b></b></div><div class="rs" data-rs="1"></div>' : ''}
  </div>`;
}

/* Update live values IN PLACE.
 *
 * The board used to be rebuilt with innerHTML on every 2.5s poll, which
 * re-fetched every <img> (visible flicker) and reset the section rail to the
 * first chip. Nothing structural changes between polls, so touch only the
 * handful of nodes whose values actually moved.
 */
function refresh(){
  if (!L || !S || editing || dragging) return;

  document.querySelectorAll('[data-slider]').forEach(sl => {
    if (draggingSlider || Date.now() < suppressUntil) return;
    if (+sl.value !== S.volume){
      sl.value = S.volume;
      sl.style.setProperty('--pct', S.volume + '%');
    }
    const box = sl.closest('.slid');
    if (box){
      const v = box.querySelector('.v');
      if (v && v.textContent !== String(S.volume)) v.textContent = S.volume;
    }
  });

  for (const sec of L.sections){
    for (const t of sec.tiles){
      const el = board.querySelector('[data-tile="' + t.id + '"]');
      if (!el) continue;

      if (t.kind === 'toggle'){
        const on = toggleOn(t.ref);
        el.classList.toggle('on', on);
        const sw = el.querySelector('.sw');
        if (sw) sw.classList.toggle('on', on);
        if (t.ref === 'keeper'){
          const lab = el.querySelector('.lab');
          const want = (S.keeper && S.keeper.enabled)
            ? S.keeper.target + '%' : t.label;
          if (lab && lab.textContent !== want) lab.textContent = want;
        }
      }

      else if (t.kind === 'stat'){
        const m = S.memory || {};
        const v = el.querySelector('.v');
        if (v) v.innerHTML = `${m.usedGb}<span style="font-size:10px;color:var(--muted);font-weight:400"> / ${m.totalGb} GB</span>`;
        const last = el.querySelector('.bars i:last-child');
        if (last) last.style.height = Math.max(8, m.percent || 0) + '%';
      }

      else if (t.kind === 'game' || t.kind === 'app'){
        const want = isRunning(t);
        const has = !!el.querySelector('.badge');
        if (want && !has && t.kind === 'game'){
          const b = document.createElement('div');
          b.className = 'badge';
          b.innerHTML = '<i></i>Running';
          el.firstElementChild.appendChild(b);
        } else if (!want && has){
          el.querySelector('.badge').remove();
        }
      }
    }
  }
}

/* Keep the rail chip matching the section you are actually looking at. */
function wireSpy(){
  if (spy) spy.disconnect();
  spy = new IntersectionObserver(entries => {
    for (const e of entries){
      if (!e.isIntersecting) continue;
      const id = e.target.id.replace(/^sec-/, '');
      if (id === activeSec) continue;
      activeSec = id;
      rail.querySelectorAll('.chip').forEach(c =>
        c.classList.toggle('on', c.dataset.jump === activeSec));
    }
  }, { rootMargin: '-120px 0px -65% 0px', threshold: 0 });
  document.querySelectorAll('.sec').forEach(s => spy.observe(s));
}

function findTile(id){
  for (const s of L.sections){
    const i = s.tiles.findIndex(t => t.id === id);
    if (i >= 0) return { sec: s, i, t: s.tiles[i] };
  }
  return null;
}

/* ================= interaction ================= */

/* Volume slider: paint instantly, send at most ~8/sec. */
let pending = null, sending = false;
async function flushVolume(){
  if (sending || pending === null) return;
  sending = true;
  const v = pending; pending = null;
  try { S = await api('/api/volume', { value: v }); }
  catch(e){ $('#dot').className = 'dot off'; }
  sending = false;
  suppressUntil = Date.now() + 600;
  if (pending !== null) setTimeout(flushVolume, 60);
}

board.addEventListener('input', e => {
  const sl = e.target.closest('[data-slider]');
  if (!sl) return;
  const v = +sl.value;
  sl.style.setProperty('--pct', v + '%');
  const box = sl.closest('.slid');
  if (box) box.querySelector('.v').textContent = v;
  pending = v; flushVolume();
});
['pointerdown','touchstart'].forEach(ev => board.addEventListener(ev, e => {
  if (e.target.closest('[data-slider]')) draggingSlider = true;
}, {passive:true}));
['pointerup','touchend','touchcancel'].forEach(ev => board.addEventListener(ev, () => {
  if (draggingSlider){ draggingSlider = false; suppressUntil = Date.now() + 700; }
}, {passive:true}));

/* Long-press anywhere on the board enters edit mode - the home-screen gesture. */
let pressTimer = null, pressStart = null;
board.addEventListener('pointerdown', e => {
  if (editing || e.target.closest('[data-slider]')) return;
  const el = e.target.closest('[data-tile]');
  if (!el) return;
  pressStart = { x: e.clientX, y: e.clientY, id: el.dataset.tile };
  clearTimeout(pressTimer);
  pressTimer = setTimeout(() => {
    pressStart = null;
    enterEdit();
    if (navigator.vibrate) navigator.vibrate(12);
  }, 600);
});
board.addEventListener('pointermove', e => {
  if (!pressStart) return;
  if (Math.hypot(e.clientX - pressStart.x, e.clientY - pressStart.y) > 12){
    clearTimeout(pressTimer); pressStart = null;
  }
});
['pointerup','pointercancel'].forEach(ev =>
  board.addEventListener(ev, () => { clearTimeout(pressTimer); pressStart = null; }));

/* Tap */
board.addEventListener('click', async e => {
  if (e.target.closest('[data-slider]')) return;

  const addSec = e.target.closest('[data-addtile]');
  if (addSec){ openAdd(addSec.dataset.addtile); return; }

  const el = e.target.closest('[data-tile]');
  if (!el) return;
  const found = findTile(el.dataset.tile);
  if (!found) return;
  const t = found.t;

  // In edit mode a tap does nothing: removing happens on pointerdown above,
  // and dragging/resizing own the rest.
  if (editing) return;

  if (t.kind === 'stream'){ openDesktop(t); return; }
  if (t.kind === 'slider') return;

  const destructive = t.kind === 'action' && /restart|shutdown|signout/.test(t.ref);
  if (destructive && armed !== t.id){
    armed = t.id; render();
    clearTimeout(armTimer);
    armTimer = setTimeout(() => { armed = null; render(); }, 4000);
    toast('Tap again to ' + t.label.toLowerCase());
    return;
  }
  clearTimeout(armTimer); armed = null;

  el.classList.add('sent');
  setTimeout(() => el.classList.remove('sent'), 500);

  try {
    const body = { kind: t.kind, ref: t.ref };
    if (destructive) body.confirm = true;
    if (t.kind === 'toggle' && t.ref === 'keeper' && S) body.target = S.volume;
    const r = await api('/api/tile', body);
    if (r && r.volume !== undefined) S = r;
    if (t.kind === 'scene') toast('Running ' + t.label);
    render();
  } catch(err){
    el.classList.remove('sent');
    toast(err.message);
  }
});

/* ---------- edit mode: drag to reorder, corner to resize ---------- */
function enterEdit(){
  editing = true; dirty = false;
  document.body.classList.add('editing');
  render();
  toast('Drag to move · corner to resize');
}
function exitEdit(save){
  editing = false;
  document.body.classList.remove('editing');
  if (save && dirty){
    api('/api/layout', L).then(l => { L = l; render(); toast('Layout saved'); })
      .catch(e => toast(e.message));
  } else if (!save && dirty){
    loadLayout().then(render);
  }
  dirty = false;
  render();
}

board.addEventListener('pointerdown', e => {
  if (!editing) return;
  const el = e.target.closest('[data-tile]');
  if (!el) return;

  const found = findTile(el.dataset.tile);
  if (!found) return;

  // Remove is handled HERE, on pointerdown, not on click. A drag elsewhere
  // calls setPointerCapture on the tile, and a captured pointer retargets the
  // subsequent click to the tile itself - so the click handler saw the tile,
  // never the little red X, and deleting silently did nothing.
  if (e.target.closest('[data-rm]')){
    e.preventDefault();
    e.stopPropagation();
    found.sec.tiles.splice(found.i, 1);
    dirty = true;
    if (navigator.vibrate) navigator.vibrate(8);
    render();
    return;
  }

  if (e.target.closest('[data-rs]')){
    const cell = parseFloat(getComputedStyle(document.documentElement)
      .getPropertyValue('--cell'));
    dragging = { mode: 'resize', el, found, x0: e.clientX, y0: e.clientY,
                 w0: found.t.w, h0: found.t.h, cell };
    el.setPointerCapture(e.pointerId);
    e.preventDefault();
    return;
  }

  dragging = { mode: 'move', el, found, moved: false };
  el.setPointerCapture(e.pointerId);
  e.preventDefault();
});

board.addEventListener('pointermove', e => {
  if (!dragging) return;

  if (dragging.mode === 'resize'){
    const dx = e.clientX - dragging.x0, dy = e.clientY - dragging.y0;
    const w = Math.max(1, Math.min(4, dragging.w0 + Math.round(dx / dragging.cell)));
    const h = Math.max(1, Math.min(6, dragging.h0 + Math.round(dy / dragging.cell)));
    if (w !== dragging.found.t.w || h !== dragging.found.t.h){
      dragging.found.t.w = w; dragging.found.t.h = h;
      dragging.el.style.gridColumn = 'span ' + w;
      dragging.el.style.gridRow = 'span ' + h;
      dirty = true;
    }
    return;
  }

  if (!dragging.moved){
    dragging.moved = true;
    dragging.el.classList.add('lift');
  }
  // Follow the finger, then drop into whatever tile is underneath.
  const over = document.elementFromPoint(e.clientX, e.clientY);
  const target = over && over.closest('[data-tile]');
  if (!target || target === dragging.el) return;

  const from = findTile(dragging.el.dataset.tile);
  const to = findTile(target.dataset.tile);
  if (!from || !to) return;

  from.sec.tiles.splice(from.i, 1);
  const dest = findTile(target.dataset.tile);
  if (!dest){ from.sec.tiles.splice(from.i, 0, from.t); return; }
  dest.sec.tiles.splice(dest.i, 0, from.t);
  dirty = true;
  render();
  const again = board.querySelector(`[data-tile="${from.t.id}"]`);
  if (again){ again.classList.add('lift'); dragging.el = again; }
});

['pointerup','pointercancel'].forEach(ev => board.addEventListener(ev, () => {
  if (!dragging) return;
  dragging.el.classList.remove('lift');
  dragging.el.style.gridColumn = '';
  dragging.el.style.gridRow = '';
  dragging = null;
  render();
}));

document.addEventListener('click', e => {
  if (e.target.closest('#saveEdit')) exitEdit(true);
  if (e.target.closest('#cancelEdit')) exitEdit(false);
  const jump = e.target.closest('[data-jump]');
  if (jump){
    activeSec = jump.dataset.jump;
    document.querySelectorAll('#rail .chip').forEach(c => c.classList.remove('on'));
    jump.classList.add('on');
    const sec = document.getElementById('sec-' + activeSec);
    if (sec) sec.scrollIntoView({ behavior:'smooth', block:'start' });
  }
  if (e.target.closest('[data-addsec]')){
    const name = 'Section ' + (L.sections.length + 1);
    L.sections.push({ id: 's' + Date.now().toString(36), name, tiles: [] });
    dirty = true; render();
  }
});

$('#editBtn').addEventListener('click', () => editing ? exitEdit(true) : enterEdit());

/* ================= sheets ================= */
let sheetCtx = null;

function openSheet(title, bodyHTML, footHTML){
  $('#sheetTitle').textContent = title;
  $('#sheetBody').innerHTML = bodyHTML;
  $('#sheetFoot').innerHTML = footHTML || '';
  $('#sheet').classList.add('show');
  $('#scrim').classList.add('show');
}
function closeSheet(){
  $('#sheet').classList.remove('show');
  $('#scrim').classList.remove('show');
  sheetCtx = null;
}
$('#scrim').addEventListener('click', () => closeSheet());

/* ---------- swipe the sheet down to dismiss ----------
 * The grab handle looks draggable, so it has to BE draggable - having to
 * reach for "Done" at the bottom after pulling at the handle is the app
 * lying about its own affordance.
 *
 * Two places start a drag: the handle strip, and the body when it is already
 * scrolled to the top (the iOS pattern - keep pulling past the top and the
 * sheet comes with you).
 */
(function sheetSwipe(){
  const sheet = $('#sheet'), scrim = $('#scrim'), body = $('#sheetBody');
  let start = null, dy = 0, fromBody = false;

  function begin(y, viaBody){
    start = y; dy = 0; fromBody = viaBody;
    sheet.classList.add('dragging');
  }

  function move(y){
    if (start === null) return false;
    dy = y - start;
    if (dy < 0){                       // dragging back up: rubber-band
      dy = Math.max(dy / 3, -40);
    }
    sheet.style.transform = 'translateY(' + dy + 'px)';
    // Fade the scrim out as it goes, so it feels attached to the gesture.
    const h = sheet.offsetHeight || 600;
    scrim.style.opacity = String(Math.max(0, 1 - Math.max(0, dy) / h));
    return dy > 0;
  }

  function end(){
    if (start === null) return;
    const h = sheet.offsetHeight || 600;
    sheet.classList.remove('dragging');
    sheet.style.transform = '';
    scrim.style.opacity = '';
    // Past a quarter of the sheet (or 110px, whichever is smaller) it closes.
    if (dy > Math.min(110, h * 0.25)) closeSheet();
    start = null; dy = 0; fromBody = false;
  }

  const zone = $('#grabzone');
  zone.addEventListener('touchstart', e => begin(e.touches[0].clientY, false), {passive:true});
  zone.addEventListener('touchmove', e => { move(e.touches[0].clientY); }, {passive:true});
  zone.addEventListener('touchend', end, {passive:true});
  zone.addEventListener('touchcancel', end, {passive:true});

  // Mouse, so it also works when someone opens this on a desktop browser.
  zone.addEventListener('pointerdown', e => {
    if (e.pointerType === 'touch') return;
    begin(e.clientY, false);
    zone.setPointerCapture(e.pointerId);
  });
  zone.addEventListener('pointermove', e => {
    if (e.pointerType === 'touch' || start === null) return;
    move(e.clientY);
  });
  ['pointerup','pointercancel'].forEach(ev => zone.addEventListener(ev, e => {
    if (e.pointerType === 'touch') return;
    end();
  }));

  body.addEventListener('touchstart', e => {
    if (body.scrollTop <= 0 && e.touches.length === 1) begin(e.touches[0].clientY, true);
  }, {passive:true});
  body.addEventListener('touchmove', e => {
    if (start === null || !fromBody) return;
    if (body.scrollTop > 0){ end(); return; }   // they started scrolling instead
    move(e.touches[0].clientY);
  }, {passive:true});
  body.addEventListener('touchend', () => { if (fromBody) end(); }, {passive:true});
  body.addEventListener('touchcancel', () => { if (fromBody) end(); }, {passive:true});

  // Escape closes it too, for the desktop case.
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && sheet.classList.contains('show')) closeSheet();
  });
})();

/* ---------- add a tile ---------- */
const CONTROLS = [
  { kind:'slider', ref:'volume', label:'Volume', w:4, h:1 },
  { kind:'toggle', ref:'mute', label:'Mute', w:1, h:1 },
  { kind:'toggle', ref:'keeper', label:'Lock volume', w:2, h:1 },
  { kind:'action', ref:'media.playpause', label:'Play/Pause', w:1, h:1 },
  { kind:'action', ref:'media.next', label:'Next', w:1, h:1 },
  { kind:'action', ref:'media.prev', label:'Previous', w:1, h:1 },
  { kind:'action', ref:'screen.off', label:'Screen off', w:1, h:1 },
  { kind:'stream', ref:'0', label:'Desktop', w:4, h:2 },
  { kind:'stat',   ref:'ram', label:'RAM', w:2, h:1 },
  { kind:'action', ref:'power.lock', label:'Lock PC', w:2, h:1 },
  { kind:'action', ref:'power.sleep', label:'Sleep', w:2, h:1 },
  { kind:'action', ref:'power.restart', label:'Restart', w:2, h:1 },
  { kind:'action', ref:'power.shutdown', label:'Shut down', w:2, h:1 },
  { kind:'action', ref:'power.signout', label:'Sign out', w:2, h:1 },
];

async function openAdd(secId){
  sheetCtx = { secId, tab:'game', sel:new Set() };
  openSheet('Add a tile', '<div style="padding:20px 0;color:var(--muted)">Loading…</div>', '');
  if (!lib){
    try { lib = await api('/api/library'); }
    catch(e){ toast(e.message); return; }
  }
  drawAdd();
}

function drawAdd(){
  const c = sheetCtx;
  const tabs = [['game','Games'],['app','Apps'],['ctrl','Controls'],['scene','Scenes']];
  const items = (lib ? lib.items : []).filter(i =>
    (c.tab === 'game' ? i.kind === 'game' : i.kind === 'app') &&
    (!c.q || i.name.toLowerCase().includes(c.q)));

  let bodyHTML = `
    <input class="field" id="addSearch" placeholder="Search ${lib ? lib.total : 0} items"
           value="${esc(c.q || '')}" style="margin-top:12px">
    <div style="display:flex;gap:7px;margin:12px 0 4px;overflow-x:auto">
      ${tabs.map(([k,n]) => `<div class="chip ${c.tab===k?'on':''}" data-tab="${k}">${n}</div>`).join('')}
    </div>`;

  if (c.tab === 'game'){
    // Games have real box art, so a picture grid is right for them.
    bodyHTML += `<div class="pick" style="margin-top:12px">
      ${items.slice(0,120).map(i => `
        <div class="c ${c.sel.has(i.id)?'sel':''}" data-add="${esc(i.id)}"
             data-kind="${i.kind}" data-name="${esc(i.name)}">
          <img src="/api/art?id=${encodeURIComponent(i.id)}" alt="" loading="lazy"
               onerror="this.style.display='none'">
          <div class="cap">${esc(i.name)}</div>
          <div class="tick"><svg width="11" height="11" viewBox="0 0 24 24" fill="none"
            stroke="#fff" stroke-width="3" stroke-linecap="round"><path d="M20 6L9 17l-5-5"/></svg></div>
        </div>`).join('')}
    </div>
    <div class="btn wide" id="browseBtn" style="border-style:dashed;margin:16px 0 14px">
      Browse the PC for a program</div>`;

  } else if (c.tab === 'app'){
    // Apps have icons, not box art. Forcing them into 2:3 cards looked awful,
    // so they get a proper list - and BROWSING comes first, because picking
    // your own is the good path and the 298 Start Menu entries are the dregs.
    const mine = items.filter(i => i.source === 'Added by you');
    const rest = items.filter(i => i.source !== 'Added by you');
    bodyHTML += `
      <div class="btn wide pri" id="browseBtn" style="margin-top:14px">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff"
          stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
        Browse the PC for a program</div>
      <div style="font-size:10.5px;color:var(--muted);margin-top:8px;line-height:1.45">
        Pick the .exe yourself and it comes in with its real icon. Best results.
      </div>
      ${mine.length ? `
        <div style="display:flex;align-items:center;gap:8px;margin:18px 0 4px">
          <div style="font-size:10px;letter-spacing:1.1px;color:var(--accent2);
            text-transform:uppercase">Added by you</div>
          <div style="flex-grow:1;height:1px;background:var(--line)"></div></div>
        <div class="list">${mine.map(appRow).join('')}</div>` : ''}
      <div style="display:flex;align-items:center;gap:8px;margin:18px 0 4px">
        <div style="font-size:10px;letter-spacing:1.1px;color:var(--accent2);
          text-transform:uppercase">Detected</div>
        <div style="flex-grow:1;height:1px;background:var(--line)"></div>
        <div style="font-size:10px;color:var(--muted)">${rest.length}</div></div>
      <div class="list" style="padding-bottom:14px">
        ${rest.slice(0, c.q ? 80 : 30).map(appRow).join('')}
        ${!c.q && rest.length > 30 ? `<div style="padding:12px 0;font-size:11px;
          color:var(--muted);text-align:center">Search to see the rest</div>` : ''}
      </div>`;

  } else if (c.tab === 'ctrl'){
    bodyHTML += `<div class="list" style="margin-top:6px">
      ${CONTROLS.map((x,i) => `<div class="it" data-ctrl="${i}">
        ${iconFor(x.ref)}
        <div class="nm">${esc(x.label)}<div class="sub">${x.w} × ${x.h}</div></div>
        <div class="ch">+</div></div>`).join('')}</div>`;
  } else {
    const scenes = (L.scenes || []);
    bodyHTML += scenes.length
      ? `<div class="list" style="margin-top:6px">${scenes.map(s =>
          `<div class="it" data-scene="${esc(s.id)}">
            <div class="nm">${esc(s.name)}<div class="sub">${s.steps.length} steps</div></div>
            <div class="ch">+</div></div>`).join('')}</div>
         <div class="btn wide" id="newScene" style="margin:14px 0">Make another scene</div>`
      : sceneIntroHTML();
  }

  const n = sheetCtx.sel.size;
  openSheet('Add a tile', bodyHTML,
    `<div style="flex-grow:1;font-size:12px;color:var(--muted)">
       ${n ? n + ' selected · adds as ' + (sheetCtx.tab==='game' ? '2 × 3' : '2 × 1') : 'Pick something'}</div>
     <button class="btn" id="addCancel">Close</button>
     <button class="btn pri" id="addGo">Add</button>`);

  const si = $('#addSearch');
  if (si && c.focusSearch){ si.focus(); c.focusSearch = false; }
}

function appRow(i){
  return `<div class="it" data-add="${esc(i.id)}" data-kind="app" data-name="${esc(i.name)}">
    <img class="thumb" src="/api/art?id=${encodeURIComponent(i.id)}" alt=""
         loading="lazy" onerror="this.style.visibility='hidden'">
    <div class="nm">${esc(i.name)}<div class="sub">${esc(i.source)}</div></div>
    <div class="ch" style="color:${sheetCtx && sheetCtx.sel.has(i.id)
      ? 'var(--accent2)' : 'var(--muted)'}">${sheetCtx && sheetCtx.sel.has(i.id) ? '✓' : '+'}</div>
  </div>`;
}

function sceneIntroHTML(){
  return `<div style="text-align:center;padding:26px 6px 10px">
    <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="var(--accent2)"
      stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 3l1.9 5.8H20l-4.9 3.6 1.9 5.8L12 14.6 7 18.2l1.9-5.8L4 8.8h6.1z"/></svg>
    <div style="font-size:15px;font-weight:600;margin-top:12px">One tap, several things</div>
    <div style="font-size:12px;color:#94a3b8;line-height:1.55;margin-top:8px">
      A scene runs a list of actions in order. Set the volume, open a game, mute
      everything, lock the PC — whatever you do together anyway.</div>
    <div style="margin-top:16px;border-radius:12px;background:rgba(1,2,10,.5);
      border:1px solid var(--line);padding:12px;text-align:left">
      <div style="font-size:10px;letter-spacing:1px;color:var(--accent2);
        text-transform:uppercase;margin-bottom:7px">For example</div>
      <div style="font-size:11.5px;color:#cbd5e1;line-height:1.7">
        Volume → 35%<br>Unmute<br>Open a movie app<br>Wait 3s · Screen off</div>
    </div>
    <div class="btn pri wide" id="newScene" style="margin-top:16px">Make one</div>
  </div>`;
}

$('#sheetBody').addEventListener('input', e => {
  if (e.target.id === 'addSearch'){
    sheetCtx.q = e.target.value.trim().toLowerCase();
    sheetCtx.focusSearch = true;
    drawAdd();
  }
  if (e.target.id === 'sceneName' && sheetCtx) sheetCtx.name = e.target.value;
});

$('#sheetBody').addEventListener('click', async e => {
  if (!sheetCtx) return;

  const tab = e.target.closest('[data-tab]');
  if (tab){ sheetCtx.tab = tab.dataset.tab; sheetCtx.sel.clear(); drawAdd(); return; }

  const card = e.target.closest('[data-add]');
  if (card){
    const id = card.dataset.add;
    if (sheetCtx.sel.has(id)) sheetCtx.sel.delete(id);
    else sheetCtx.sel.add(id);
    drawAdd();
    return;
  }

  const ctrl = e.target.closest('[data-ctrl]');
  if (ctrl){
    const spec = CONTROLS[+ctrl.dataset.ctrl];
    addTiles([{ ...spec, id: 't' + Math.random().toString(36).slice(2,10) }]);
    return;
  }

  const sc = e.target.closest('[data-scene]');
  if (sc){
    const s = (L.scenes || []).find(x => x.id === sc.dataset.scene);
    if (s) addTiles([{ id:'t'+Math.random().toString(36).slice(2,10),
      kind:'scene', ref:s.id, label:s.name, w:2, h:1 }]);
    return;
  }

  if (e.target.closest('#browseBtn')){ openBrowse(''); return; }
  if (e.target.closest('#newScene')){ openSceneEditor(); return; }
});

function addTiles(tiles){
  const sec = L.sections.find(s => s.id === sheetCtx.secId) || L.sections[0];
  sec.tiles.push(...tiles);
  dirty = true;
  closeSheet();
  if (!editing) enterEdit();
  render();
  toast(tiles.length + ' added — drag it where you want it');
}

$('#sheetFoot').addEventListener('click', e => {
  if (e.target.closest('#addCancel')){ closeSheet(); return; }
  if (e.target.closest('#addGo')){
    const c = sheetCtx;
    if (!c.sel.size){ toast('Nothing selected'); return; }
    const picked = lib.items.filter(i => c.sel.has(i.id));
    addTiles(picked.map(i => ({
      id: 't' + Math.random().toString(36).slice(2,10),
      kind: i.kind === 'game' ? 'game' : 'app',
      ref: i.id, label: i.name,
      w: 2, h: i.kind === 'game' ? 3 : 1,
    })));
    return;
  }
  if (e.target.closest('#saveScene')) saveScene();
  if (e.target.closest('#sheetClose')) closeSheet();
});

/* ---------- browse the PC for a program ---------- */
async function openBrowse(p){
  let d;
  try { d = await api('/api/browse?p=' + encodeURIComponent(p || '')); }
  catch(e){ toast(e.message); return; }

  const body = `
    <div style="font-size:11px;color:var(--muted);margin:12px 0 6px;
      word-break:break-all">${esc(d.path || 'This PC')}</div>
    ${d.error ? `<div style="font-size:12px;color:var(--bad);margin-bottom:8px">${esc(d.error)}</div>` : ''}
    <div class="list">
      ${d.up !== null && d.up !== undefined ? `<div class="it" data-dir="${esc(d.up)}">
        <div class="nm" style="color:var(--accent2)">← Back</div></div>` : ''}
      ${d.entries.map(x => `<div class="it" ${x.dir
        ? `data-dir="${esc(x.path)}"` : `data-exe="${esc(x.path)}" data-nm="${esc(x.name)}"`}>
        <div style="width:20px;display:flex;color:${x.dir ? 'var(--accent2)' : 'var(--muted)'}">
          ${x.dir
            ? '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>'
            : '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="4" y="3" width="16" height="18" rx="2"/><path d="M9 9h6M9 13h6"/></svg>'}
        </div>
        <div class="nm">${esc(x.name)}</div>
        <div class="ch">${x.dir ? '›' : '+'}</div></div>`).join('')}
    </div>`;

  openSheet('Pick a program', body,
    `<div style="flex-grow:1"></div><button class="btn" id="sheetClose">Close</button>`);
  sheetCtx = sheetCtx || {};
}

$('#sheetBody').addEventListener('click', async e => {
  const dir = e.target.closest('[data-dir]');
  if (dir){ openBrowse(dir.dataset.dir); return; }

  const exe = e.target.closest('[data-exe]');
  if (exe){
    try {
      const r = await api('/api/addapp', { path: exe.dataset.exe, name:
        exe.dataset.nm.replace(/\.(exe|lnk|url|bat)$/i, '') });
      lib = null;                       // library changed; refetch next open
      addTiles([{ id:'t'+Math.random().toString(36).slice(2,10), kind:'app',
                  ref: r.item.id, label: r.item.name, w:2, h:1 }]);
    } catch(err){ toast(err.message); }
  }
});

/* ---------- scene editor ---------- */
const STEP_TYPES = [
  { op:'volume', label:'Set volume', arg:'number', def:35 },
  { op:'mute', label:'Mute' }, { op:'unmute', label:'Unmute' },
  { op:'keeper', label:'Lock volume', arg:'bool', def:true },
  { op:'open', label:'Open…', arg:'app' },
  { op:'close', label:'Close…', arg:'exe' },
  { op:'key', label:'Media key', arg:'text', def:'playpause' },
  { op:'type', label:'Type text', arg:'text', def:'' },
  { op:'wait', label:'Wait', arg:'number', def:3 },
  { op:'screenoff', label:'Screen off' },
  { op:'power', label:'Power', arg:'text', def:'lock' },
];

function openSceneEditor(scene){
  sheetCtx = { scene: scene || { id:'sc'+Date.now().toString(36), name:'', steps:[] } };
  drawSceneEditor();
}

function drawSceneEditor(){
  const s = sheetCtx.scene;
  const body = `
    <input class="field" id="sceneName" placeholder="Name it" value="${esc(s.name)}"
           style="margin-top:12px">
    <div style="font-size:10px;letter-spacing:1px;color:var(--accent2);
      text-transform:uppercase;margin:16px 0 8px">Steps</div>
    <div class="list">
      ${s.steps.map((st,i) => `<div class="it">
        <div style="font-size:10px;color:var(--muted);width:12px">${i+1}</div>
        <div class="nm">${esc(stepLabel(st))}</div>
        <div class="ch" data-delstep="${i}">×</div></div>`).join('')}
    </div>
    <div style="font-size:10px;letter-spacing:1px;color:var(--accent2);
      text-transform:uppercase;margin:16px 0 8px">Add a step</div>
    <div style="display:flex;flex-wrap:wrap;gap:7px;padding-bottom:14px">
      ${STEP_TYPES.map((t,i) => `<div class="chip" data-step="${i}">${t.label}</div>`).join('')}
    </div>
    <div style="font-size:11px;color:var(--cyan);line-height:1.5;
      background:rgba(34,211,238,.06);border:1px solid rgba(34,211,238,.2);
      border-radius:11px;padding:10px 12px;margin-bottom:14px">
      Steps run top to bottom. If one fails the scene stops there and tells you which.
    </div>`;
  openSheet(s.name ? 'Edit scene' : 'New scene', body,
    `<div style="flex-grow:1"></div>
     <button class="btn" id="sheetClose">Cancel</button>
     <button class="btn pri" id="saveScene">Save</button>`);
}

function stepLabel(st){
  const t = STEP_TYPES.find(x => x.op === st.op);
  const n = t ? t.label : st.op;
  if (st.op === 'wait') return `Wait ${st.value}s`;
  if (st.op === 'volume') return `Volume → ${st.value}%`;
  if (st.op === 'open' || st.op === 'close') return `${n.replace('…','')} ${st.name || st.ref}`;
  if (st.value !== undefined && st.value !== '') return `${n}: ${st.value}`;
  return n;
}

$('#sheetBody').addEventListener('click', async e => {
  if (!sheetCtx || !sheetCtx.scene) return;

  const del = e.target.closest('[data-delstep]');
  if (del){
    sheetCtx.scene.steps.splice(+del.dataset.delstep, 1);
    drawSceneEditor();
    return;
  }

  const add = e.target.closest('[data-step]');
  if (add){
    const t = STEP_TYPES[+add.dataset.step];
    const step = { op: t.op };
    if (t.arg === 'number'){
      const v = prompt(t.label, t.def);
      if (v === null) return;
      step.value = Number(v) || t.def;
    } else if (t.arg === 'text'){
      const v = prompt(t.label, t.def);
      if (v === null) return;
      step.value = v;
    } else if (t.arg === 'bool'){
      step.value = true;
    } else if (t.arg === 'app' || t.arg === 'exe'){
      if (!lib) lib = await api('/api/library');
      const q = prompt('Which app or game?', '');
      if (!q) return;
      const hit = lib.items.find(i => i.name.toLowerCase().includes(q.toLowerCase()));
      if (!hit){ toast('No match for ' + q); return; }
      step.ref = hit.id; step.name = hit.name;
      if (t.arg === 'exe') step.value = hit.name;
    }
    sheetCtx.scene.steps.push(step);
    drawSceneEditor();
  }
});

async function saveScene(){
  const s = sheetCtx.scene;
  s.name = ($('#sceneName') ? $('#sceneName').value.trim() : '') || 'Scene';
  if (!s.steps.length){ toast('Add at least one step'); return; }
  try {
    L = await api('/api/scene', s);
    closeSheet();
    toast('Scene saved — add it as a tile');
    render();
  } catch(e){ toast(e.message); }
}

/* ---------- settings ---------- */
/* A preset is just a starting pair. Once you pick your own primary or
 * secondary the preset stops being "selected", so it can never quietly
 * overwrite your colours the way it used to. */
const PRESETS = {
  aether: { primary:'#7c3aed', secondary:'#22d3ee', bg:'#01020a' },
  ice:    { primary:'#0ea5e9', secondary:'#a5f3fc', bg:'#020617' },
  ember:  { primary:'#f97316', secondary:'#facc15', bg:'#0a0503' },
  forest: { primary:'#10b981', secondary:'#a3e635', bg:'#04100b' },
  rose:   { primary:'#e11d48', secondary:'#fb7185', bg:'#0d0308' },
  mono:   { primary:'#94a3b8', secondary:'#e2e8f0', bg:'#0a0a0c' },
};

function presetMatches(t, p){
  return t.primary === p.primary && t.secondary === p.secondary && t.bg === p.bg;
}

function colorRow(key, label, hint){
  const val = L.theme[key];
  const choices = key === 'primary'
    ? ['#7c3aed','#0ea5e9','#10b981','#f97316','#e11d48','#94a3b8']
    : ['#22d3ee','#a5f3fc','#a3e635','#facc15','#fb7185','#e2e8f0'];
  return `
    <div style="margin-top:16px">
      <div style="display:flex;align-items:center;gap:10px">
        <div style="flex-grow:1">
          <div style="font-size:13.5px">${label}</div>
          <div style="font-size:10.5px;color:var(--muted);margin-top:2px">${hint}</div>
        </div>
        <div style="font-size:11px;color:var(--muted);font-family:ui-monospace,monospace">${esc(val)}</div>
        <label class="swatch" style="background:${esc(val)};position:relative;
          box-shadow:0 0 0 2px var(--text)">
          <input type="color" value="${esc(val)}" data-color="${key}"
            style="opacity:0;width:100%;height:100%;display:block;cursor:pointer">
        </label>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
        ${choices.map(c => `<div class="swatch ${val===c?'sel':''}"
          data-set="${key}" data-val="${c}" style="background:${c}"></div>`).join('')}
      </div>
    </div>`;
}

function openSettings(){
  const t = L.theme;
  const body = `
    <div style="font-size:10px;letter-spacing:1px;color:var(--accent2);
      text-transform:uppercase;margin:16px 0 9px">Layout</div>
    <div style="display:flex;gap:7px">
      ${[['rail','Scroll + rail'],['scroll','Plain scroll'],['pages','Pages']]
        .map(([k,n]) => `<div class="chip ${L.mode===k?'on':''}" data-mode="${k}"
          style="flex-grow:1;text-align:center;justify-content:center">${n}</div>`).join('')}
    </div>
    <div style="font-size:10.5px;color:var(--muted);margin-top:8px;line-height:1.45">
      All three show the same tiles in the same order — this only changes how they are presented.
    </div>

    <div style="font-size:10px;letter-spacing:1px;color:var(--accent2);
      text-transform:uppercase;margin:20px 0 9px">Start from</div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:9px">
      ${Object.entries(PRESETS).map(([k,p]) => `
        <div data-preset="${k}" style="cursor:pointer">
          <div style="height:46px;border-radius:12px;border:${presetMatches(t,p)
            ? '2px solid ' + p.secondary : '1px solid var(--line)'};
            background:linear-gradient(140deg,${p.bg},${p.primary} 65%,${p.secondary})"></div>
          <div style="font-size:10px;text-align:center;margin-top:6px;
            color:${presetMatches(t,p) ? 'var(--text)' : 'var(--muted)'}">${k}</div>
        </div>`).join('')}
    </div>

    ${colorRow('primary','Primary','fills, sliders, anything that is on')}
    ${colorRow('secondary','Secondary','gradients, edit mode, highlights')}
    ${colorRow('bg','Background','the base the whole app sits on')}

    <div style="margin-top:18px">
      <div class="srow"><div style="flex-grow:1">
        <div class="t">Corner radius</div>
        <div class="d">${t.radius} px</div></div></div>
      <input type="range" min="0" max="26" value="${t.radius}" id="radiusRange"
        style="--pct:${(t.radius/26*100).toFixed(0)}%">

      <div class="srow"><div style="flex-grow:1">
        <div class="t">Glass panels</div><div class="d">blur behind tiles</div></div>
        <div class="sw ${t.glass?'on':''}" data-t="glass"><i></i></div></div>

      <div class="srow"><div style="flex-grow:1">
        <div class="t">Scan lines</div><div class="d">the faint cyan texture</div></div>
        <div class="sw ${t.scanlines?'on':''}" data-t="scanlines"><i></i></div></div>

      <div class="srow"><div style="flex-grow:1">
        <div class="t">Labels on game art</div><div class="d">off lets the box art speak</div></div>
        <div class="sw ${t.gameLabels?'on':''}" data-t="gameLabels"><i></i></div></div>
    </div>

    <div class="btn wide" id="a2hsBtn" style="margin-top:18px">Add to home screen</div>

    <div style="display:flex;gap:9px;margin:9px 0 14px">
      <div class="btn" id="rescanBtn" style="flex-grow:1">Rescan games</div>
      <div class="btn" id="logoutBtn" style="flex-grow:1">Log out</div>
    </div>`;

  openSheet('Settings', body,
    `<div style="flex-grow:1;font-size:11px;color:var(--muted)">Saved as you change it</div>
     <button class="btn" id="sheetClose">Done</button>`);
}

$('#sheetBody').addEventListener('input', e => {
  const col = e.target.closest('[data-color]');
  if (col){
    L.theme[col.dataset.color] = col.value;
    L.theme.preset = 'custom';
    applyTheme(L.theme);
    const lab = col.closest('label');
    if (lab) lab.style.background = col.value;
    const txt = lab && lab.previousElementSibling;
    if (txt) txt.textContent = col.value;
    saveThemeSoon();
    return;
  }
  if (e.target.id === 'radiusRange'){
    L.theme.radius = +e.target.value;
    e.target.style.setProperty('--pct', (L.theme.radius/26*100) + '%');
    applyTheme(L.theme);
    saveThemeSoon();
  }
});

$('#sheetBody').addEventListener('click', async e => {
  const m = e.target.closest('[data-mode]');
  if (m){ L.mode = m.dataset.mode; saveThemeSoon(); openSettings(); render(); return; }

  const p = e.target.closest('[data-preset]');
  if (p){
    Object.assign(L.theme, PRESETS[p.dataset.preset], { preset:p.dataset.preset });
    applyTheme(L.theme); saveThemeSoon(); openSettings(); return;
  }

  const sw2 = e.target.closest('[data-set]');
  if (sw2){
    // Picking a colour clears the preset so it cannot overwrite this later.
    L.theme[sw2.dataset.set] = sw2.dataset.val;
    L.theme.preset = 'custom';
    applyTheme(L.theme); saveThemeSoon(); openSettings(); return;
  }

  const sw = e.target.closest('[data-t]');
  if (sw){
    const k = sw.dataset.t;
    L.theme[k] = !L.theme[k];
    applyTheme(L.theme); saveThemeSoon(); openSettings(); return;
  }

  if (e.target.closest('#a2hsBtn')){
    closeSheet();
    setTimeout(() => showHomeScreenGuide(true), 260);
    return;
  }
  if (e.target.closest('#rescanBtn')){
    toast('Rescanning…');
    try { lib = await api('/api/library?rescan=1'); toast(lib.total + ' found'); }
    catch(err){ toast(err.message); }
    return;
  }
  if (e.target.closest('#logoutBtn')){
    await api('/api/logout', {});
    location.href = '/login';
  }
});

let themeTimer = null;
function saveThemeSoon(){
  clearTimeout(themeTimer);
  themeTimer = setTimeout(() => {
    api('/api/layout', L).then(l => { L = l; }).catch(() => {});
  }, 500);
}

$('#setBtn').addEventListener('click', openSettings);

/* ================= full-screen desktop =================
 *
 * Replaces the old viewer, which was a 16:9 box inside the bottom sheet with
 * a "type a string and press Send" box underneath. You could look at the PC
 * and poke it; you could not use it.
 *
 * Tap where you want to click. There WAS a trackpad mode - drag to move the
 * cursor relatively - and it was removed, because the captured video does
 * not contain the mouse cursor. You were dragging something invisible, which
 * is unusable no matter how well the maths works. Tap-to-click needs no
 * cursor: you aim with your finger.
 *
 * Typing is the part that matters. The input at the bottom is real and
 * visible: tapping it raises the phone keyboard, what you type goes straight
 * to the PC as you type it, and Enter searches. It is visible rather than
 * off-screen for two reasons - you can see the keyboard is aimed at the PC,
 * and you can long-press it to paste from the phone's clipboard, which the
 * Clipboard API will not do over plain HTTP.
 *
 * Mobile keyboards do not report useful keyCodes (they send 229), so what
 * gets sent is worked out by diffing the input's VALUE against what was
 * already sent. That handles typing, pasting and deleting with one rule.
 *
 * The keys a phone keyboard simply does not have - Esc, Tab, Ctrl+C, arrows,
 * Win - live in the toolbar above it, which does not auto-hide.
 */

const FS = {
  on: false, mon: 0, frames: 0, fpsTimer: null,
  barTimer: null, seenHint: false, sent: '',
  // gen bumps on every start/stop so an in-flight frame from the last feed
  // cannot paint over the new one; url is the object URL currently shown.
  gen: 0, url: null, times: [], adapted: false,
};

function fsq(id){ return document.getElementById(id); }

function fsToast(msg, ms){
  const t = fsq('fsToast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.remove('show'), ms || 2200);
}

/* Only the top bar hides. The dock at the bottom is the keyboard and the
   function keys, and a toolbar you have to go looking for is the thing that
   made the first version of this annoying to use. */
function fsBars(show){
  const bar = fsq('fsBar');
  if (!bar) return;
  bar.classList.toggle('hide', !show);
  clearTimeout(FS.barTimer);
  if (show) FS.barTimer = setTimeout(() => fsBars(false), 4000);
}

/* Keep the dock sitting on top of the phone keyboard rather than under it.
   visualViewport shrinks when the keyboard opens; the difference against
   innerHeight is how tall the keyboard is. */
function fsDock(){
  const vv = window.visualViewport, dock = fsq('fsDock');
  if (!dock) return;
  const gap = vv ? Math.max(0, window.innerHeight - vv.height - vv.offsetTop) : 0;
  dock.style.bottom = Math.round(gap) + 'px';
}

function openDesktop(tile){
  const fs = fsq('fs');
  if (!fs) return;
  FS.mon = +((tile && tile.ref) || 0);
  FS.on = true;
  FS.adapted = false;        // judge this connection afresh each time
  fs.classList.add('on');

  const sel = fsq('fsMon');
  sel.innerHTML = ((S && S.monitors) || [{id:0,label:'Main'}]).map(m =>
    `<option value="${m.id}" ${m.id === FS.mon ? 'selected' : ''}>${esc(m.label)}</option>`
  ).join('');

  fsBars(true);
  fsDock();
  fsStartFeed();

  // Real fullscreen where it exists. iOS Safari only allows it for <video>,
  // and a home-screen app has no browser chrome anyway, so a failure here is
  // not a problem worth reporting to the user.
  if (fs.requestFullscreen) fs.requestFullscreen().catch(() => {});
  try {
    if (screen.orientation && screen.orientation.lock)
      screen.orientation.lock('landscape').catch(() => {});
  } catch(e){}

  if (!FS.seenHint){
    FS.seenHint = true;
    try { FS.seenHint = !!localStorage.getItem('aether.fshint'); } catch(e){}
    if (!FS.seenHint){
      fsToast('Tap to click, hold to right-click, two fingers to scroll. ' +
              'Tap the box at the bottom to type.', 4600);
      try { localStorage.setItem('aether.fshint', '1'); } catch(e){}
    }
  }
}

function closeDesktop(){
  const fs = fsq('fs');
  FS.on = false;
  fsStopFeed();
  fsq('fsKeys').blur();
  fs.classList.remove('on');
  clearTimeout(FS.barTimer);
  try { if (document.fullscreenElement) document.exitFullscreen(); } catch(e){}
  try { if (screen.orientation && screen.orientation.unlock)
          screen.orientation.unlock(); } catch(e){}
}

/* ---------- the feed ----------
 * One frame at a time, each requested only after the last one has been
 * painted. This replaced an MJPEG <img src="/api/stream">, which looked
 * simpler and was the reason the preview felt laggy: the server pushes
 * frames whether or not the phone can keep up, so on a connection slower
 * than the stream they pile up in buffers and what you are watching drifts
 * further and further behind the real screen - and tapping a thing you can
 * see is useless if that thing moved two seconds ago.
 *
 * Asking for the next frame only when the last one is on screen cannot fall
 * behind: the round trip IS the frame rate, so the picture is always the
 * newest one, and a slow link costs frame rate instead of latency.
 */
const FPS = { low: 12, medium: 18, high: 24 };

function fsStartFeed(){
  fsStopFeed();
  const gen = ++FS.gen;
  FS.frames = 0;
  FS.times = [];
  FS.fpsTimer = setInterval(() => {
    const s = fsq('fsStat');
    if (s){
      const ms = FS.times.length
        ? Math.round(FS.times.reduce((a, b) => a + b, 0) / FS.times.length) : 0;
      s.textContent = FS.frames + ' fps' + (ms ? ' · ' + ms + ' ms' : '');
    }
    FS.frames = 0;
  }, 1000);
  fsFeedLoop(gen);
}

function fsStopFeed(){
  FS.gen++;
  clearInterval(FS.fpsTimer);
  const feed = fsq('fsFeed');
  if (feed) feed.removeAttribute('src');
  if (FS.url){ URL.revokeObjectURL(FS.url); FS.url = null; }
}

const fsWait = (ms) => new Promise(r => setTimeout(r, ms));

async function fsFeedLoop(gen){
  const feed = fsq('fsFeed');
  let misses = 0;

  while (FS.on && gen === FS.gen){
    const t0 = performance.now();
    const mon = fsq('fsMon').value || 0;
    const q = fsq('fsQ').value || 'medium';
    let url = null;
    try {
      const r = await fetch(`/api/frame?mon=${mon}&q=${q}&t=${Date.now()}`,
                            { cache: 'no-store' });
      if (r.status === 401){ location.href = '/login'; return; }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      url = URL.createObjectURL(await r.blob());
    } catch(e){
      misses++;
      if (gen !== FS.gen) return;
      if (misses === 3) fsToast('Lost the connection to the PC…', 2500);
      await fsWait(Math.min(400 * misses, 2000));
      continue;
    }
    if (gen !== FS.gen){ URL.revokeObjectURL(url); return; }
    misses = 0;

    // Swap only once the new frame has decoded, so the picture never blanks.
    await new Promise(done => {
      feed.onload = feed.onerror = done;
      feed.src = url;
    });
    if (FS.url) URL.revokeObjectURL(FS.url);
    FS.url = url;

    const took = performance.now() - t0;
    FS.frames++;
    FS.times.push(took);
    if (FS.times.length > 12) FS.times.shift();
    fsAdapt();

    // Do not ask faster than the tier's frame rate; a fast link should not
    // sit at 60 requests a second warming the PC up for no visible gain.
    const left = 1000 / (FPS[q] || 18) - took;
    if (left > 4) await fsWait(left);
  }
}

/* A slow link should cost frame rate, not usability. If frames are taking
   long enough that the picture feels dead, drop a tier - once, and say so,
   because silently changing what someone is looking at is worse than lag. */
function fsAdapt(){
  if (FS.adapted || FS.times.length < 8) return;
  const sorted = [...FS.times].sort((a, b) => a - b);
  const median = sorted[sorted.length >> 1];
  if (median < 330) return;
  const sel = fsq('fsQ');
  const next = { high: 'medium', medium: 'low' }[sel.value];
  if (!next) return;
  FS.adapted = true;
  sel.value = next;
  FS.times = [];
  fsToast('Connection is slow — dropped to ' + next + ' quality', 2600);
}

function fsMon(){ return +(fsq('fsMon').value || 0); }

/* Where did that touch land on the actual screen image?
   object-fit:contain letterboxes the feed, so the visible picture is smaller
   than the element and a raw offsetX would be wrong at the edges. */
function fsNorm(cx, cy){
  const feed = fsq('fsFeed');
  const r = feed.getBoundingClientRect();
  const nat = (feed.naturalWidth || 16) / (feed.naturalHeight || 9);
  const box = r.width / r.height;
  let w = r.width, h = r.height, ox = 0, oy = 0;
  if (box > nat){ w = r.height * nat; ox = (r.width - w) / 2; }
  else { h = r.width / nat; oy = (r.height - h) / 2; }
  const x = cx - r.left - ox, y = cy - r.top - oy;
  if (x < 0 || y < 0 || x > w || y > h) return null;
  return { x: x / w, y: y / h, px: cx - r.left, py: cy - r.top };
}

function fsRing(px, py){
  const r = fsq('fsRing');
  if (!r) return;
  r.style.left = px + 'px';
  r.style.top = py + 'px';
  r.animate([{opacity:.95, transform:'translate(-50%,-50%) scale(.4)'},
             {opacity:0,   transform:'translate(-50%,-50%) scale(1.7)'}],
            {duration:420, easing:'ease-out'});
}

/* ---------- pointer ----------
 * Pointer Events rather than touch events, so the same code drives a finger
 * on a phone and a mouse in a desktop browser. Two live pointers means
 * scroll; one means tap (click), hold (right click) or drag.
 */
(function wireFsPointer(){
  const fs = fsq('fs');
  if (!fs) return;

  const pts = new Map();
  let longT = null, moved = false, start = null, scrollY = null;
  let lastTap = 0, lastTapX = 0, lastTapY = 0;

  const onChrome = t => t.closest('#fsBar') || t.closest('#fsDock')
                     || t.closest('#fsExit');

  fs.addEventListener('pointerdown', e => {
    if (onChrome(e.target)) return;         // let the toolbars work normally
    // The toolbars auto-hide; the top strip is how you get them back.
    if (e.clientY < 50){ fsBars(true); return; }

    e.preventDefault();
    pts.set(e.pointerId, {x:e.clientX, y:e.clientY});

    if (pts.size === 2){
      clearTimeout(longT);
      const v = [...pts.values()];
      scrollY = (v[0].y + v[1].y) / 2;
      return;
    }
    if (pts.size > 2) return;

    start = {x:e.clientX, y:e.clientY, t:Date.now(), n:fsNorm(e.clientX, e.clientY)};
    moved = false;
    clearTimeout(longT);
    longT = setTimeout(async () => {
      if (!start || moved) return;
      fsRing(start.x, start.y);
      if (navigator.vibrate) navigator.vibrate(12);
      try {
        if (start.n) await api('/api/click',
          {mon:fsMon(), x:start.n.x, y:start.n.y, button:'right'});
        fsToast('Right click', 900);
      } catch(err){ fsToast(err.message); }
      start = null;
    }, 550);
  });

  fs.addEventListener('pointermove', e => {
    const p = pts.get(e.pointerId);
    if (!p) return;
    p.x = e.clientX; p.y = e.clientY;

    if (pts.size >= 2){
      const v = [...pts.values()];
      const y = (v[0].y + v[1].y) / 2;
      if (scrollY !== null){
        const d = y - scrollY;
        // A wheel notch is 120; this makes a finger-length drag about a
        // page, which is what two-finger scrolling feels like elsewhere.
        if (Math.abs(d) > 6){
          api('/api/scroll', {amount: Math.round(d * 8)}).catch(() => {});
          scrollY = y;
        }
      }
      return;
    }

    if (!start) return;
    if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > 10){
      moved = true;
      clearTimeout(longT);
    }
  });

  async function release(e){
    pts.delete(e.pointerId);
    if (pts.size < 2) scrollY = null;
    if (pts.size > 0 || !start) { if (pts.size === 0) start = null; return; }

    clearTimeout(longT);
    const st = start; start = null;
    const dt = Date.now() - st.t;

    if (moved){
      // A finger that travelled is a drag: real start and end coordinates,
      // so window-dragging and text selection both work.
      if (st.n){
        const end = fsNorm(e.clientX, e.clientY);
        if (end) {
          try { await api('/api/drag', {mon:fsMon(), x1:st.n.x, y1:st.n.y,
                                        x2:end.x, y2:end.y}); }
          catch(err){ fsToast(err.message); }
        }
      }
      return;
    }
    if (dt > 400) return;                 // a slow press that was not a tap

    const now = Date.now();
    const dbl = now - lastTap < 320
              && Math.hypot(st.x - lastTapX, st.y - lastTapY) < 32;
    lastTap = now; lastTapX = st.x; lastTapY = st.y;

    fsRing(st.x, st.y);
    try {
      if (st.n) await api('/api/click',
        {mon:fsMon(), x:st.n.x, y:st.n.y, double: dbl});
    } catch(err){ fsToast(err.message); }
  }

  fs.addEventListener('pointerup', release);
  fs.addEventListener('pointercancel', e => {
    pts.delete(e.pointerId);
    clearTimeout(longT);
    if (pts.size === 0){ start = null; scrollY = null; }
  });
})();

/* ---------- typing ----------
 * What you type goes to the PC as you type it, so the PC's own search box
 * fills in live and Enter searches - which is the whole point.
 *
 * The box keeps its text rather than clearing on every character, because an
 * input that empties itself as you type looks broken, and because you cannot
 * long-press an empty invisible field to paste into it.
 *
 * So what gets sent is a DIFF. Compare the box against what has already been
 * sent: back up over the characters that disappeared, type the ones that
 * appeared. One rule covers typing, autocorrect rewriting a whole word,
 * pasting a paragraph, and holding backspace - none of which report a usable
 * keyCode on a phone (they all report 229).
 */
const FS_KEYS = {
  Enter:'enter', Tab:'tab', Escape:'esc',
  ArrowUp:'up', ArrowDown:'down', ArrowLeft:'left', ArrowRight:'right',
  Delete:'delete', Home:'home', End:'end',
  PageUp:'pageup', PageDown:'pagedown',
};

function fsClearTyping(){
  const box = fsq('fsKeys');
  if (box) box.value = '';
  FS.sent = '';
}

/* One queue for everything typed. Each keystroke is its own HTTP request, and
   requests issued back-to-back can finish out of order - which would spell
   the word wrong on the PC. Chaining them is the fix. */
let fsQueue = Promise.resolve();

function fsSend(fn){
  fsQueue = fsQueue.then(fn).catch(err => fsToast(err.message));
  return fsQueue;
}

/* Send the PC whatever turns `FS.sent` into the box's current contents. */
function fsSync(){
  const box = fsq('fsKeys');
  if (!box) return fsQueue;
  const now = box.value, was = FS.sent;
  if (now === was) return fsQueue;
  FS.sent = now;

  let i = 0;
  while (i < now.length && i < was.length && now[i] === was[i]) i++;
  const back = was.length - i;
  const add = now.slice(i);

  return fsSend(async () => {
    for (let n = 0; n < back; n++) await api('/api/press', {name:'backspace'});
    if (add) await api('/api/type', {text: add});
  });
}

function fsEnterKey(){
  const box = fsq('fsKeys');
  fsSync();
  const p = fsSend(() => api('/api/press', {name:'enter'}));
  // The PC has taken the line; start fresh rather than leaving text behind
  // that the PC no longer has.
  if (box) box.value = '';
  FS.sent = '';
  return p;
}

(function wireFsKeys(){
  const box = fsq('fsKeys');
  if (!box) return;

  box.addEventListener('input', fsSync);

  box.addEventListener('keydown', e => {
    // Backspace with nothing left in the box still means "delete on the PC",
    // which is how you clear a field the PC already had text in.
    if (e.key === 'Backspace' && !box.value){
      e.preventDefault();
      fsSend(() => api('/api/press', {name:'backspace'}));
      return;
    }
    if (e.key === 'Backspace') return;     // let the box edit; input() syncs

    const name = FS_KEYS[e.key];
    const mods = [];
    if (e.ctrlKey) mods.push('ctrl');
    if (e.altKey) mods.push('alt');
    if (e.shiftKey && name) mods.push('shift');
    if (e.metaKey) mods.push('win');

    if (e.key === 'Enter'){ e.preventDefault(); fsEnterKey(); return; }
    if (name){
      e.preventDefault();
      fsSync();
      fsSend(() => api('/api/press', {name, mods}));
      return;
    }
    // A real keyboard sending Ctrl+C: the character never reaches `input`,
    // so it has to be caught here.
    if ((e.ctrlKey || e.altKey || e.metaKey) && e.key.length === 1){
      e.preventDefault();
      fsSend(() => api('/api/press', {name:e.key.toLowerCase(), mods}));
    }
  });

  // Tapping away from the box means the PC is no longer following it, so do
  // not keep diffing against text the PC has moved on from.
  box.addEventListener('blur', () => { box.value = ''; FS.sent = ''; });
  box.addEventListener('focus', () => { box.value = ''; FS.sent = ''; });
})();

/* ---------- the toolbars ---------- */
(function wireFsChrome(){
  const bar = fsq('fsBar'), dock = fsq('fsDock');
  if (!bar || !dock) return;

  fsq('fsExit').onclick = closeDesktop;
  fsq('fsExit').addEventListener('pointerdown', e => e.stopPropagation());
  fsq('fsMon').onchange = fsStartFeed;
  fsq('fsQ').onchange = () => {
    // Choosing a quality by hand means we stop second-guessing it.
    FS.adapted = true;
    fsStartFeed();
  };

  // Right click where the cursor already is - which, after a tap, is where
  // you last tapped. /api/tap clicks in place instead of re-positioning.
  fsq('fsRight').onclick = async () => {
    try { await api('/api/tap', {button:'right'}); fsToast('Right click', 900); }
    catch(err){ fsToast(err.message); }
    fsBars(true);
  };

  fsq('fsEnter').onclick = fsEnterKey;

  // The dock's buttons must not steal focus from the input: on a phone,
  // losing focus closes the keyboard, and pressing Ctrl+C should not shut
  // the keyboard you were typing with.
  dock.addEventListener('pointerdown', e => {
    if (e.target.closest('.fsb')) e.preventDefault();
  });

  dock.addEventListener('click', async e => {
    const pr = e.target.closest('[data-press]');
    const cb = e.target.closest('[data-combo]');
    if (!pr && !cb) return;
    // Anything typed but not yet sent goes first, so Ctrl+A after typing
    // selects what you actually typed.
    fsSync();
    await fsSend(() => {
      if (pr) return api('/api/press', {name: pr.dataset.press});
      const parts = cb.dataset.combo.split('+');
      return api('/api/press', {name: parts.pop(), mods: parts});
    });
    // The PC's field no longer matches the box, so stop diffing against it.
    fsClearTyping();
  });

  bar.addEventListener('pointerdown', () => fsBars(true));

  // Ride above the phone keyboard as it opens and closes.
  if (window.visualViewport){
    window.visualViewport.addEventListener('resize', fsDock);
    window.visualViewport.addEventListener('scroll', fsDock);
  }

  // Leaving fullscreen by the system gesture or Esc should close the viewer
  // too, rather than leaving a stream running behind the board.
  document.addEventListener('fullscreenchange', () => {
    if (!document.fullscreenElement && FS.on && fsq('fs').classList.contains('on')
        && document.visibilityState === 'visible'){
      // Only when the user actually left fullscreen, not when we never got it.
      if (FS._hadFs) closeDesktop();
    }
    FS._hadFs = !!document.fullscreenElement;
  });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && FS.on && document.activeElement !== fsq('fsKeys'))
      closeDesktop();
  });
})();


/* ================= my PCs =================
 *
 * Each PC is its own address, so each is its own browser origin - which
 * means its own cookie, its own login and its own home-screen app. There is
 * no way around that without one PC proxying the others, so switching is
 * honest about what it is: a list of addresses, and tapping one goes there.
 *
 * What makes it bearable is that the session cookie lasts 30 days per PC, so
 * after the first login on each, switching is a single tap.
 *
 * The list lives on the phone, not on the PC, because it is a property of
 * the phone: your phone knows about your machines, and a friend's phone that
 * you paired to one of them should not learn about the rest.
 */
const PCS_KEY = 'aether.pcs';

function pcsLoad(){
  try {
    const raw = localStorage.getItem(PCS_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list.filter(p => p && p.url) : [];
  } catch(e){ return []; }
}

function pcsSave(list){
  try { localStorage.setItem(PCS_KEY, JSON.stringify(list.slice(0, 12))); }
  catch(e){}
}

function pcsNormUrl(u){
  u = String(u || '').trim();
  if (!u) return '';
  if (!/^https?:\/\//i.test(u)) u = 'http://' + u;
  try {
    const p = new URL(u);
    if (!p.port && p.protocol === 'http:') p.port = '8787';
    return p.origin + '/';
  } catch(e){ return ''; }
}

function pcsHere(){ return location.origin + '/'; }

/* What to CALL a PC. `alias` is a name you typed - "Cody's PC" - and it wins
   over `name`, which is the machine's real hostname pulled off the PC. The
   alias lives only on this phone and never touches the actual device name;
   it is purely how this remote presents it to you. */
function pcsDisp(p){
  return (p && p.alias && p.alias.trim()) || (p && p.name) || 'PC';
}

/* The name for the PC we are looking at right now, for the header subtitle,
   so a rename shows up there too and not only inside the switcher. */
function pcsCurrentName(fallback){
  const p = pcsLoad().find(x => x.url === pcsHere());
  return (p && p.alias && p.alias.trim()) || fallback;
}

/* Remember whichever PC we are actually looking at, so the list builds
   itself as you pair phones rather than needing to be typed out. */
async function pcsRemember(){
  try {
    const info = await api('/api/pairinfo');
    const url = pcsNormUrl(info.url) || pcsHere();
    const list = pcsLoad();
    const mine = list.find(p => p.url === pcsHere() || p.url === url);
    if (mine){
      mine.name = info.pc || mine.name;
      mine.url = pcsHere();
    } else {
      list.unshift({name: info.pc || 'This PC', url: pcsHere()});
    }
    pcsSave(list);
  } catch(e){}
}

function openPCs(editIdx){
  const list = pcsLoad();
  const here = pcsHere();

  const dot = (on) => `<div style="width:9px;height:9px;border-radius:50%;
    flex:0 0 auto;background:${on ? 'var(--good,#34d399)' : 'var(--line2)'}"></div>`;

  const rows = list.map((p, i) => {
    if (i === editIdx){
      // This row is being renamed: the name becomes an input. The hostname
      // is the placeholder, so clearing the box and saving falls back to it.
      return `
    <div class="srow">
      ${dot(p.url === here)}
      <div style="flex-grow:1;min-width:0;display:flex;gap:7px">
        <input class="field" id="pcAlias" value="${esc(p.alias || '')}"
          placeholder="${esc(p.name || 'PC')}" autocomplete="off"
          autocapitalize="words" spellcheck="false"
          style="flex-grow:1;min-width:0">
        <button class="btn pri" data-savepc="${i}">Save</button>
        <button class="btn" data-cancelpc="1">Cancel</button>
      </div>
    </div>`;
    }
    return `
    <div class="srow" data-pc="${i}" style="cursor:pointer">
      ${dot(p.url === here)}
      <div style="flex-grow:1;min-width:0">
        <div class="t">${esc(pcsDisp(p))}${p.url === here
          ? ' <span style="color:var(--muted);font-size:11px">· you are here</span>' : ''}</div>
        <div class="d" style="overflow:hidden;text-overflow:ellipsis;
          white-space:nowrap">${esc(p.url)}</div>
      </div>
      <div data-editpc="${i}" style="flex:0 0 auto;padding:6px 9px;
        color:var(--muted);font-size:12px">Rename</div>
      ${p.url === here ? '' :
        `<div data-rmpc="${i}" style="flex:0 0 auto;padding:6px 9px;
           color:var(--bad);font-size:12px">Remove</div>`}
    </div>`;
  }).join('');

  openSheet('My PCs', `
    <div style="margin-top:12px">
      ${rows || '<div class="srow"><div class="d">No PCs saved yet.</div></div>'}
    </div>

    <div style="margin-top:16px">
      <div class="t" style="margin-bottom:7px">Add another PC</div>
      <input class="field" id="pcUrl" placeholder="100.x.x.x:8787"
             autocomplete="off" autocapitalize="off" spellcheck="false"
             inputmode="url">
      <div class="btn wide pri" id="pcAdd" style="margin-top:9px">Check and add</div>
      <div id="pcMsg" style="font-size:12px;color:var(--muted);
        margin-top:9px;line-height:1.5">
        Open Aether Remote on the other PC and use its tray menu &rarr;
        <b>Pair a phone</b> to see its address.
      </div>
    </div>`,
    `<div style="flex-grow:1;font-size:11px;color:var(--muted)">Each PC logs in
      separately, then remembers you for 30 days</div>
     <button class="btn" id="sheetClose">Done</button>`);
}

$('#sheetBody').addEventListener('click', async e => {
  const rm = e.target.closest('[data-rmpc]');
  if (rm){
    const list = pcsLoad();
    list.splice(+rm.dataset.rmpc, 1);
    pcsSave(list);
    openPCs();
    return;
  }

  const edit = e.target.closest('[data-editpc]');
  if (edit){ openPCs(+edit.dataset.editpc); return; }

  if (e.target.closest('[data-cancelpc]')){ openPCs(); return; }

  const save = e.target.closest('[data-savepc]');
  if (save){
    const list = pcsLoad();
    const p = list[+save.dataset.savepc];
    if (p){
      const v = ($('#pcAlias').value || '').trim().slice(0, 40);
      // Empty means "just use the real name" - so we clear the alias rather
      // than store a blank one.
      if (v) p.alias = v; else delete p.alias;
      pcsSave(list);
    }
    openPCs();
    if (p && p.url === pcsHere()) pcsMarkTitle();
    return;
  }

  const row = e.target.closest('[data-pc]');
  if (row){
    const p = pcsLoad()[+row.dataset.pc];
    if (!p || p.url === pcsHere()) return;
    toast('Switching to ' + pcsDisp(p) + '…');
    location.href = p.url;
    return;
  }

  if (e.target.closest('#pcAdd')){
    const box = $('#pcUrl'), msg = $('#pcMsg');
    const url = pcsNormUrl(box.value);
    if (!url){ msg.textContent = 'That does not look like an address.'; return; }
    if (url === pcsHere()){ msg.textContent = 'That is this PC.'; return; }
    if (pcsLoad().some(p => p.url === url)){
      msg.textContent = 'Already in the list.'; return;
    }

    msg.textContent = 'Checking…';
    // /ping is the one route that answers cross-origin. A reachable Aether
    // Remote answers it with its own name; anything else fails here rather
    // than after it has been saved and tapped.
    // A wrong address does not refuse the connection, it hangs - the browser
    // will sit on an unroutable IP far longer than anyone will wait, and the
    // screen would just say "Checking..." forever. Give up after 6 seconds
    // and say so.
    const ac = new AbortController();
    const bail = setTimeout(() => ac.abort(), 6000);
    try {
      const r = await fetch(url + 'ping', {cache:'no-store', signal:ac.signal});
      const j = await r.json();
      if (!j || j.app !== 'remote') throw new Error('not an Aether Remote');
      const list = pcsLoad();
      list.push({name: j.pc || 'PC', url});
      pcsSave(list);
      openPCs();
      toast('Added ' + (j.pc || 'PC') + ' — tap Rename to give it your own name');
    } catch(err){
      msg.innerHTML = 'Could not reach it. Check the PC is on, the address is '
        + 'right, and your phone is on the same network or tailnet as it.';
    } finally {
      clearTimeout(bail);
    }
  }
});

/* The title is the switcher. A caret appears only once there is somewhere
   else to go, so a one-PC user never sees an affordance that does nothing. */
function pcsMarkTitle(){
  const t = $('#title');
  if (!t) return;
  const many = pcsLoad().length > 1;
  t.style.cursor = 'pointer';
  const name = t.textContent.replace(/\s*▾$/, '');
  t.textContent = many ? name + ' ▾' : name;
}

// Always opens: with one PC saved it is still where you add the second.
$('#title').addEventListener('click', openPCs);

/* ================= boot ================= */
async function loadLayout(){
  L = await api('/api/layout');
  return L;
}

async function poll(){
  try {
    if (draggingSlider || Date.now() < suppressUntil) return;
    S = await api('/api/state');
    $('#dot').className = 'dot';
    // The alias (if you set one) wins over the machine's hostname here too.
    $('#meta').textContent = S.time + ' · ' + pcsCurrentName(S.device);
    refresh();          // in place - never a full rebuild, or it flickers
  } catch(e){ $('#dot').className = 'dot off'; }
}

(async function boot(){
  sizeGrid();
  try {
    await loadLayout();
    S = await api('/api/state');
    render();
  } catch(e){
    board.innerHTML = `<div style="padding:40px 10px;text-align:center;color:var(--muted)">
      ${esc(e.message)}</div>`;
  }
  pcsRemember().then(pcsMarkTitle).catch(() => {});
  setInterval(poll, 2500);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
})();


/* ============ add to home screen ============
 * The remote is a web page until someone saves it to their home screen, and
 * then it is an app - full height, no browser chrome, its own icon. Nobody
 * discovers that on their own, so the first time a phone logs in we walk
 * through it once, in that phone's own words.
 *
 * Deliberately quiet: never in the PC app's preview iframe, never on a
 * desktop browser, never once it is already installed, and never twice.
 */
const A2HS_KEY = 'aether.a2hs.v1';

function a2hsPlatform(){
  const ua = navigator.userAgent;
  const ios = /iPad|iPhone|iPod/.test(ua) ||
    // iPadOS reports itself as a Mac; a Mac with a touchscreen is the tell.
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  if (ios) return /CriOS|FxiOS|EdgiOS/.test(ua) ? 'ios-other' : 'ios';
  if (/Android/.test(ua)) return 'android';
  return null;                       // desktop: it already has a window
}

function a2hsInstalled(){
  return window.navigator.standalone === true ||
    (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
}

let deferredInstall = null;          // Android's real install prompt
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  deferredInstall = e;
});

function a2hsStep(n, html, glyph){
  return `<div style="display:flex;gap:13px;align-items:flex-start;padding:13px 0;
      border-top:1px solid var(--line)">
    <div style="width:24px;height:24px;border-radius:50%;flex:0 0 auto;
      background:var(--primary);color:#fff;font-size:12px;font-weight:600;
      display:flex;align-items:center;justify-content:center">${n}</div>
    <div style="flex-grow:1;font-size:13.5px;line-height:1.5">${html}</div>
    ${glyph ? `<div style="flex:0 0 auto;opacity:.85">${glyph}</div>` : ''}
  </div>`;
}

const A2HS_SHARE = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none"
  stroke="var(--accent2)" stroke-width="1.7" stroke-linecap="round"
  stroke-linejoin="round"><path d="M12 15V3"/><path d="M8 7l4-4 4 4"/>
  <path d="M5 12v7a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-7"/></svg>`;

const A2HS_PLUS = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none"
  stroke="var(--accent2)" stroke-width="1.7" stroke-linecap="round">
  <rect x="3.5" y="3.5" width="17" height="17" rx="4"/>
  <path d="M12 8.5v7M8.5 12h7"/></svg>`;

const A2HS_DOTS = `<svg width="20" height="20" viewBox="0 0 24 24" fill="var(--accent2)">
  <circle cx="12" cy="5" r="1.7"/><circle cx="12" cy="12" r="1.7"/>
  <circle cx="12" cy="19" r="1.7"/></svg>`;

function a2hsBody(kind){
  const intro = `<div style="display:flex;gap:13px;align-items:center;margin:14px 0 4px">
      <img src="/static/icon-180.png" alt="" style="width:52px;height:52px;
        border-radius:13px;flex:0 0 auto;box-shadow:0 6px 18px var(--glow)">
      <div style="font-size:13.5px;line-height:1.5;color:var(--muted)">
        Put the remote on your home screen and it opens like a real app &mdash;
        full screen, own icon, no address bar, and it stays logged in.</div>
    </div>`;

  if (kind === 'ios') return intro +
    a2hsStep(1, 'Tap the <b>Share</b> button in Safari &mdash; the square with the ' +
                'arrow, at the bottom of the screen.', A2HS_SHARE) +
    a2hsStep(2, 'Scroll down the list. If you do not see it, tap ' +
                '<b>Edit Actions</b> or <b>More</b> at the bottom.') +
    a2hsStep(3, 'Tap <b>Add to Home Screen</b>, then <b>Add</b> in the corner.',
             A2HS_PLUS) +
    `<div style="font-size:12px;color:var(--muted);padding-top:12px;
       border-top:1px solid var(--line);line-height:1.5">
       Then open it from the new icon instead of Safari.</div>`;

  if (kind === 'ios-other') return intro +
    `<div style="font-size:13.5px;line-height:1.6;padding:14px 0">
       On an iPhone, only <b>Safari</b> can add a page to the home screen.
       Copy this page's address, open it in Safari, then tap
       <b>Share &rarr; Add to Home Screen</b>.</div>`;

  return intro +
    a2hsStep(1, 'Tap the <b>&#8942;</b> menu at the top right of Chrome.', A2HS_DOTS) +
    a2hsStep(2, 'Tap <b>Add to Home screen</b> (some phones call it ' +
                '<b>Install app</b>).', A2HS_PLUS) +
    a2hsStep(3, 'Confirm with <b>Add</b> or <b>Install</b>.');
}

function showHomeScreenGuide(force){
  const kind = a2hsPlatform();
  if (!force){
    if (window.top !== window.self) return;      // the PC app's preview
    if (!kind || a2hsInstalled()) return;
    // Shown a few times at most. "Got it" without installing is usually
    // "not now", not "never" - but three of those is a clear enough answer.
    let seen = 0;
    try { seen = parseInt(localStorage.getItem(A2HS_KEY) || '0', 10) || 0; } catch(e){}
    if (seen >= 3) return;
    try { localStorage.setItem(A2HS_KEY, String(seen + 1)); } catch(e){}
  }
  if (!kind){
    toast('Already on a desktop - no home screen needed.');
    return;
  }
  const canInstall = kind === 'android' && deferredInstall;
  openSheet('Add it to your home screen', a2hsBody(kind),
    `${canInstall ? '<div class="btn wide pri" id="a2hsInstall">Install it for me</div>' : ''}
     <div class="btn wide" id="a2hsDone" style="margin-top:${canInstall ? '8px' : '0'}">
       ${canInstall ? 'I&rsquo;ll do it myself' : 'Got it'}</div>
     <div id="a2hsSkip" style="text-align:center;font-size:12px;color:var(--muted);
       padding:12px 0 2px">Don&rsquo;t show this again</div>`);

  const remember = () => { try { localStorage.setItem(A2HS_KEY, '99'); } catch(e){} };
  const el = id => document.getElementById(id);
  if (el('a2hsInstall')) el('a2hsInstall').onclick = async () => {
    const p = deferredInstall; deferredInstall = null;
    closeSheet(); remember();
    try { p.prompt(); await p.userChoice; } catch(e){}
  };
  el('a2hsDone').onclick = () => { closeSheet(); };
  el('a2hsSkip').onclick = () => { remember(); closeSheet(); };
}

// Let the board paint first - a sheet over a blank grid looks broken.
setTimeout(() => { try { showHomeScreenGuide(false); } catch(e){} }, 1400);

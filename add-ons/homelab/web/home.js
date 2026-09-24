/* Aether Homelab - phone UI.
 * One screen: server stats, VMs/containers, services. Polls /api/state and
 * redraws; taps drive the Proxmox node and the box's systemd/Docker. */
'use strict';
const $ = s => document.querySelector(s);
const S = { stats: {}, sections: [], busy: {} };
let timer = null;

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 2200);
}

async function api(path, body, _tries) {
  const opt = { headers: {} };
  if (body !== undefined) {
    opt.method = 'POST';
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  if (r.status === 401) { showLogin(); throw new Error('unauth'); }
  const txt = await r.text();
  let data = {};
  try { data = txt ? JSON.parse(txt) : {}; } catch (e) {}
  const tries = _tries || 0;
  // The server wants the PIN. Every call comes through here, so no button
  // has to know about it on its own.
  if (r.status === 403 && tries < 2 && data.error === 'locked') {
    await sfUnlock();
    return api(path, body, tries + 1);
  }
  if (r.status === 403 && tries < 2 && data.error === 'stepup') {
    if (data.setup) await sfSetup('first');
    const token = await sfStepUp(data.scope);
    return api(path, { ...(body || {}), stepup: token }, tries + 1);
  }
  if (!r.ok) { const e = new Error(data.error || ('HTTP ' + r.status)); e.data = data; throw e; }
  return data;
}

/* ---- the PIN: asked when the app opens, and for every VM stop / shut
 * down. The SERVER enforces it - this is only the part you see. ---- */
const SFX = { st: null, unlocking: null };

async function sfStatus() {
  const r = await fetch('/api/sf/status', { cache: 'no-store' });
  if (r.status === 401) { showLogin(); throw new Error('unauth'); }
  SFX.st = await r.json();
  return SFX.st;
}

async function sfPost(path, body) {
  const r = await fetch(path, { method: 'POST', cache: 'no-store',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  let j = {};
  try { j = await r.json(); } catch (e) {}
  if (!r.ok) { const e = new Error(j.error || ('HTTP ' + r.status)); e.data = j; throw e; }
  return j;
}

function scopeText(s) {
  if (s === 'guest.shutdown') return 'Shut down a VM';
  if (s === 'guest.stop') return 'Force-stop a VM';
  if (s === 'guest.reboot') return 'Reboot a VM';
  if (s === 'settings') return 'Change the PIN';
  return 'Confirm';
}

/* One dialog for everything. mode: 'unlock' | 'stepup' | 'set'.
   Resolves with the server's answer; rejects on Cancel. */
function sfDialog(mode, scope, opts) {
  opts = opts || {};
  return new Promise((resolve, reject) => {
    const st = SFX.st || {};
    const set = mode === 'set';
    $('#sfTitle').textContent = mode === 'unlock' ? 'Homelab is locked'
      : set ? (opts.first ? 'Protect VM shutdowns' : 'Change your PIN') : scopeText(scope);
    $('#sfSub').textContent = mode === 'unlock' ? 'Enter your PIN to open it.'
      : set ? (opts.first
          ? 'Shutting down a VM needs a PIN every time. Pick one - it also locks this page each time you open it.'
          : 'Asked when you open this page, and for every VM shut down.')
      : 'Enter your PIN to confirm.';
    $('#sfPin').value = ''; $('#sfPin2').value = '';
    $('#sfPin').placeholder = set ? 'New PIN (4 to 12 digits)' : 'PIN';
    $('#sfPin2').hidden = !set;
    $('#sfGo').textContent = mode === 'unlock' ? 'Unlock' : set ? 'Save PIN' : 'Confirm';
    $('#sfOff').hidden = !(set && st.enabled && !opts.first);
    $('#sfCancel').hidden = mode === 'unlock';
    $('#sfErr').textContent = st.locked_for && !set
      ? 'Too many wrong tries. Try again in ' + Math.ceil(st.locked_for / 60) + ' min.' : '';
    $('#sf').hidden = false;
    setTimeout(() => $('#sfPin').focus(), 80);

    const done = (fn, v) => { $('#sf').hidden = true; cleanup(); fn(v); };
    const go = async () => {
      $('#sfGo').disabled = true; $('#sfErr').textContent = '';
      try {
        let res;
        if (set) {
          const a = $('#sfPin').value, b = $('#sfPin2').value;
          if (!/^\d{4,12}$/.test(a)) throw new Error('A PIN is 4 to 12 digits.');
          if (a !== b) throw new Error("The two PINs don't match.");
          res = await sfPost('/api/sf/pin', { pin: a, ...(opts.proof || {}) });
        } else {
          res = await sfPost('/api/sf/verify', { purpose: mode, scope, pin: $('#sfPin').value });
        }
        try { sessionStorage.setItem('hl.unlocked', '1'); } catch (e) {}
        done(resolve, res);
      } catch (e) {
        $('#sfErr').textContent = e.message;
        $('#sfPin').value = '';
        if (e.data && e.data.locked_for && SFX.st) SFX.st.locked_for = e.data.locked_for;
      }
      $('#sfGo').disabled = false;
    };
    const off = async () => {
      try { done(resolve, await sfPost('/api/sf/disable', opts.proof || {})); }
      catch (e) { $('#sfErr').textContent = e.message; }
    };
    const key = e => { if (e.key === 'Enter') go(); };
    const cancel = () => done(reject, new Error('Cancelled'));
    function cleanup() {
      $('#sfGo').removeEventListener('click', go);
      $('#sfOff').removeEventListener('click', off);
      $('#sfCancel').removeEventListener('click', cancel);
      $('#sfPin').removeEventListener('keydown', key);
      $('#sfPin2').removeEventListener('keydown', key);
    }
    $('#sfGo').addEventListener('click', go);
    $('#sfOff').addEventListener('click', off);
    $('#sfCancel').addEventListener('click', cancel);
    $('#sfPin').addEventListener('keydown', key);
    $('#sfPin2').addEventListener('keydown', key);
  });
}

/* The poll and a tap can both hit "locked" at once - one prompt serves both. */
async function sfUnlock() {
  if (!SFX.unlocking) {
    SFX.unlocking = (async () => {
      await sfStatus();
      if (!SFX.st.enabled || SFX.st.unlocked) return;
      await sfDialog('unlock', '');
    })().finally(() => { SFX.unlocking = null; });
  }
  return SFX.unlocking;
}

async function sfStepUp(scope) {
  await sfStatus();
  return (await sfDialog('stepup', scope)).stepup;
}

async function sfSetup(why) {
  await sfStatus();
  // Changing an existing PIN needs the current one first.
  const proof = SFX.st.enabled ? { stepup: await sfStepUp('settings') } : {};
  const res = await sfDialog('set', '', { first: why === 'first', proof });
  await sfStatus();
  toast(res && res.enabled === false ? 'PIN turned off' : 'PIN saved');
}

/* Opening the page always asks - a still-valid unlock from last time
   doesn't count. A reload inside the same visit doesn't ask again. */
async function sfOnLaunch() {
  let st;
  try { st = await sfStatus(); } catch (e) { return; }
  if (!st.enabled) return;
  let fresh = true;
  try { fresh = !sessionStorage.getItem('hl.unlocked'); } catch (e) {}
  if (fresh) { await sfPost('/api/sf/lock').catch(() => {}); st.unlocked = false; }
  if (!st.unlocked) await sfUnlock();
}

/* Away for 5+ minutes -> locked again when you come back. */
const SF_AWAY_MS = 5 * 60 * 1000;
document.addEventListener('visibilitychange', async () => {
  try {
    if (document.hidden) { sessionStorage.setItem('hl.hiddenAt', String(Date.now())); return; }
    const at = +sessionStorage.getItem('hl.hiddenAt') || 0;
    if (!at || Date.now() - at < SF_AWAY_MS) return;
    sessionStorage.removeItem('hl.hiddenAt');
    if (!SFX.st || !SFX.st.enabled) return;
    sessionStorage.removeItem('hl.unlocked');
    await sfPost('/api/sf/lock').catch(() => {});
    await sfUnlock();
  } catch (e) {}
});

$('#pinBtn').addEventListener('click', () => { sfSetup('settings').catch(() => {}); });

async function refresh() {
  try {
    const d = await api('/api/state');
    S.stats = d.stats || {};
    S.sections = d.sections || [];
    hideLogin();
    render();
    if (!U.loaded) {                 // first good load: check for updates
      U.loaded = true;
      updLoad();
      setInterval(updLoad, 30 * 60 * 1000);
    }
    $('#live').style.background = 'var(--good)';
    $('#meta').textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch (e) {
    if (e.message !== 'unauth') {
      $('#live').style.background = 'var(--bad)';
      $('#meta').textContent = "can't reach the server";
    }
  }
}

function startPolling() {
  clearInterval(timer);
  refresh();
  timer = setInterval(refresh, 5000);
}

/* ---- login ---- */
function showLogin() {
  clearInterval(timer);
  $('#mask').classList.add('show');
  // The secret is never sent over the network - the installer shows it as a
  // QR code in the Proxmox shell.
  $('#enroll').textContent = 'First time? Scan the QR code the installer ' +
    'printed in the Proxmox shell, or run  aether-homelab code  in the container.';
  setTimeout(() => $('#code').focus(), 100);
}
function hideLogin() { $('#mask').classList.remove('show'); }

async function doLogin() {
  const code = ($('#code').value || '').replace(/\D/g, '');
  if (code.length !== 6) { $('#err').textContent = 'Enter all 6 digits.'; return; }
  $('#err').textContent = '';
  try {
    await api('/api/login', { code });
    $('#code').value = '';
    hideLogin();
    await sfOnLaunch();
    startPolling();
  } catch (e) {
    $('#err').textContent = e.message || 'Wrong code.';
  }
}

$('#go').addEventListener('click', doLogin);
$('#code').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });

/* ---- rendering ---- */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function statTile(t) {
  const v = S.stats[t.ref] || { big: '—', unit: '', pct: 0, na: true };
  const pct = Math.max(0, Math.min(100, v.pct || 0));
  const col = pct >= 90 ? 'var(--bad)' : pct >= 70 ? 'var(--warn)' : 'var(--primary)';
  return '<div class="tile">'
    + '<div class="t">' + esc(t.label) + '</div>'
    + '<div><div class="big">' + esc(v.big) + '</div>'
    + '<div class="u">' + esc(v.unit || '') + '</div></div>'
    + (v.na ? '' : '<div class="bar"><i style="width:' + pct + '%;background:'
        + col + '"></i></div>')
    + '</div>';
}

function running(status) {
  return status === 'running' || status === 'active';
}

function guestTile(t) {
  const busy = S.busy[t.ref];
  const on = running(t.status);
  const cls = busy ? 'busy' : (on ? 'on' : 'off');
  const state = busy ? (on ? 'stopping…' : 'starting…') : t.status;
  const kind = t.gtype === 'lxc' ? 'CT' : 'VM';
  return '<div class="tile tap" data-guest="' + esc(t.ref) + '" data-on="'
    + (on ? 1 : 0) + '">'
    + '<div class="t"><span class="sdot ' + cls + '"></span>' + esc(t.label) + '</div>'
    + '<div><div class="state">' + esc(state) + '</div>'
    + '<div class="act">' + kind + ' · tap to ' + (on ? 'shut down' : 'start')
    + '</div></div></div>';
}

function svcTile(t, kind) {
  const busy = S.busy[kind + ':' + t.ref];
  const on = running(t.status);
  const cls = busy ? 'busy' : (on ? 'on' : 'off');
  return '<div class="tile tap" data-' + kind + '="' + esc(t.ref) + '">'
    + '<div class="t"><span class="sdot ' + cls + '"></span>' + esc(t.label) + '</div>'
    + '<div><div class="state">' + esc(busy ? 'restarting…' : t.status) + '</div>'
    + '<div class="act">tap to restart</div></div></div>';
}

function tileHTML(t) {
  if (t.kind === 'stat') return statTile(t);
  if (t.kind === 'vm') return guestTile(t);
  if (t.kind === 'service') return svcTile(t, 'service');
  if (t.kind === 'docker') return svcTile(t, 'docker');
  return '';
}

function render() {
  if (!S.sections.length) {
    $('#app').innerHTML = '<div class="empty">Nothing to show yet. Check the '
      + 'Proxmox connection in the add-on’s config.</div>';
    return;
  }
  $('#app').innerHTML = S.sections.map(sec =>
    '<h2>' + esc(sec.name) + '</h2><div class="grid">'
    + sec.tiles.map(tileHTML).join('') + '</div>').join('');
}

/* ---- taps ---- */
$('#app').addEventListener('click', async e => {
  const g = e.target.closest('[data-guest]');
  const svc = e.target.closest('[data-service]');
  const dk = e.target.closest('[data-docker]');

  if (g) {
    const ref = g.dataset.guest;
    const on = g.dataset.on === '1';
    const action = on ? 'shutdown' : 'start';
    if (on && !confirm('Shut down ' + ref + '?')) return;
    S.busy[ref] = true; render();
    try {
      await api('/api/guest', { ref, action });
      toast((on ? 'Shutting down ' : 'Starting ') + ref);
    } catch (err) { if (err.message !== 'Cancelled') toast(err.message); }
    setTimeout(() => { delete S.busy[ref]; refresh(); }, 3500);
    return;
  }
  if (svc || dk) {
    const kind = svc ? 'service' : 'docker';
    const ref = (svc || dk).dataset[kind];
    const key = kind + ':' + ref;
    if (!confirm('Restart ' + ref + '?')) return;
    S.busy[key] = true; render();
    try {
      await api('/api/' + kind, { ref });
      toast('Restarting ' + ref);
    } catch (err) { toast(err.message); }
    setTimeout(() => { delete S.busy[key]; refresh(); }, 3000);
    return;
  }
});

/* ---- updates: same idea as the PC app's bar ---- */
const U = { s: null, busy: false, loaded: false };
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function updLoad(force) {
  try {
    U.s = force ? await api('/api/update/check', {}) : await api('/api/update');
  } catch (e) { U.s = null; }
  updPaint();
}

function updButtons(show) {
  ['#ubInfo', '#ubGo', '#ubSkip'].forEach(s => { $(s).hidden = !show; });
}

function updPaint() {
  if (U.busy) return;                       // mid-update: leave the text be
  const s = U.s;
  if (!s || !s.available) { $('#updbar').hidden = true; return; }
  $('#ubTitle').textContent = 'Version ' + s.latest + ' is available';
  $('#ubSub').textContent = "You're on " + s.current;
  updButtons(true);
  $('#updbar').hidden = false;
}

/* Changelog markdown is escaped first; only a few tags are put back. */
function mdToHTML(md) {
  const inline = t => esc(t)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
  let html = '', inList = false, li = null;
  const flush = () => { if (li !== null) { html += '<li>' + li + '</li>'; li = null; } };
  const close = () => { flush(); if (inList) { html += '</ul>'; inList = false; } };
  for (const raw of String(md || '').replace(/\r/g, '').split('\n')) {
    const l = raw.trim();
    const b = l.match(/^[-*]\s+(.*)$/);
    if (b) { flush(); if (!inList) { html += '<ul>'; inList = true; } li = inline(b[1]); continue; }
    if (li !== null && l && /^\s/.test(raw)) { li += ' ' + inline(l); continue; }
    close();
    if (!l) continue;
    const h = l.match(/^#{1,6}\s+(.*)$/);
    html += h ? '<h4>' + inline(h[1]) + '</h4>' : '<p>' + inline(l) + '</p>';
  }
  close();
  return html || '<p>No notes were published for this version.</p>';
}

async function updNotes() {
  const s = U.s;
  if (!s) return;
  $('#nTitle').textContent = 'Aether Homelab ' + s.latest;
  $('#nBody').innerHTML = '<p style="color:var(--muted)">Loading…</p>';
  $('#notes').hidden = false;
  try {
    const r = await fetch('/api/update/notes?tag=' + encodeURIComponent(s.tag),
                          { cache: 'no-store' });
    $('#nBody').innerHTML = r.ok ? mdToHTML(await r.text())
      : '<p>No notes were published for this version.</p>';
  } catch (e) {
    $('#nBody').innerHTML = "<p>Couldn't load the notes.</p>";
  }
}

async function updGo() {
  const want = U.s && U.s.latest;
  if (!want) return;
  $('#notes').hidden = true;
  U.busy = true;
  updButtons(false);
  $('#ubTitle').textContent = 'Updating to ' + want + '…';
  $('#ubSub').textContent = "The page reloads by itself when it's back.";
  $('#updbar').hidden = false;
  try {
    await api('/api/update/install', {});
  } catch (e) {
    U.busy = false; toast(e.message); updPaint(); return;
  }
  // Wait for the add-on to come back on the new version. If it rolled back,
  // reload anyway after 3 minutes so the page isn't stuck saying "Updating".
  for (let i = 0; i < 90; i++) {
    await sleep(2000);
    try {
      const st = await api('/api/state');
      if (st.version === want) { location.reload(); return; }
    } catch (e) {}
  }
  location.reload();
}

$('#ubInfo').addEventListener('click', updNotes);
$('#ubGo').addEventListener('click', updGo);
$('#nGo').addEventListener('click', updGo);
$('#nClose').addEventListener('click', () => { $('#notes').hidden = true; });
$('#ubSkip').addEventListener('click', async () => {
  if (!U.s) return;
  try { U.s = await api('/api/update/skip', { version: U.s.latest }); } catch (e) {}
  updPaint();
});

(async () => { await sfOnLaunch(); startPolling(); })();

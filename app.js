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

/* ================ platform ================
 * The same phone app runs against a Windows PC and against the Aether
 * Homelab add-on on a Proxmox server. The server says which it is; the tile
 * board, editor, themes, PIN and PC switcher are shared, and only the tile
 * types and the PC-only tools (desktop view, files, clipboard, games) differ.
 * An older PC that doesn't answer /api/platform is a PC.
 */
let PLAT = { kind: 'pc' };
const isPC = () => PLAT.kind !== 'homelab';

async function loadPlatform(){
  try {
    const r = await fetch('/api/platform', { cache: 'no-store' });
    if (r.ok) PLAT = await r.json();
  } catch(e){}
  document.body.dataset.platform = PLAT.kind;
  if (!isPC()){
    const tb = $('#toolsBtn');
    if (tb) tb.style.display = 'none';
    const name = PLAT.name || 'Homelab';
    document.title = 'Aether ' + name;
    const h = $('#title');
    if (h) h.textContent = name;
  }
}

async function api(path, body, method, _tries){
  const o = { method: method || (body ? 'POST' : 'GET'), headers:{} };
  if (body){ o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
  const r = await fetch(path, o);
  if (r.status === 401){ location.href = '/login'; throw new Error('logged out'); }
  const j = await r.json().catch(() => ({}));
  const tries = _tries || 0;
  // The server wants the second factor. Every protected call goes through
  // here, so no button has to know about Face ID / PIN on its own.
  if (r.status === 403 && tries < 2 && j.error === 'locked'){
    await sfUnlock();
    return api(path, body, method, tries + 1);
  }
  if (r.status === 403 && tries < 2 && j.error === 'stepup'){
    if (j.setup) await sfSetup('first');
    const token = await sfStepUp(j.scope);
    return api(path, { ...(body || {}), stepup: token }, method, tries + 1);
  }
  if (!r.ok){ const e = new Error(j.error || ('HTTP ' + r.status)); e.data = j; throw e; }
  return j;
}

/* ================ second factor: Face ID or a PIN ================
 * Opening the app asks for it; so does shut down / restart / sign out, every
 * time. The SERVER enforces both - this is only the part you see. Face ID is
 * a passkey (WebAuthn), which needs the https address; a PIN works anywhere.
 */
const SFX = { st: null, unlocking: null };
const b64u = buf => btoa(String.fromCharCode(...new Uint8Array(buf)))
  .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const unb64u = s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')
  + '==='.slice((s.length + 3) % 4)), c => c.charCodeAt(0)).buffer;

async function sfStatus(){
  const r = await fetch('/api/sf/status', { cache: 'no-store' });
  if (r.status === 401){ location.href = '/login'; throw new Error('logged out'); }
  SFX.st = await r.json();
  return SFX.st;
}

async function sfPost(path, body){
  const r = await fetch(path, { method: 'POST', cache: 'no-store',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok){ const e = new Error(j.error || ('HTTP ' + r.status)); e.data = j; throw e; }
  return j;
}

function credJSON(c){
  const out = { id: c.id, rawId: b64u(c.rawId), type: c.type, response: {} };
  for (const k of ['clientDataJSON', 'attestationObject', 'authenticatorData',
                   'signature', 'userHandle'])
    if (c.response[k]) out.response[k] = b64u(c.response[k]);
  return out;
}

async function passkeyGet(purpose, scope){
  const { publicKey: pk } = await sfPost('/api/sf/options', { purpose, scope });
  pk.challenge = unb64u(pk.challenge);
  pk.allowCredentials = (pk.allowCredentials || []).map(c => ({ ...c, id: unb64u(c.id) }));
  const c = await navigator.credentials.get({ publicKey: pk });
  return credJSON(c);
}

async function passkeyCreate(){
  const { publicKey: pk } = await sfPost('/api/sf/options', { purpose: 'register' });
  pk.challenge = unb64u(pk.challenge);
  pk.user.id = unb64u(pk.user.id);
  pk.excludeCredentials = (pk.excludeCredentials || []).map(c => ({ ...c, id: unb64u(c.id) }));
  const c = await navigator.credentials.create({ publicKey: pk });
  return credJSON(c);
}

const SCOPE_TEXT = {
  'power.shutdown': 'Shut down the PC', 'power.restart': 'Restart the PC',
  'power.signout': 'Sign out of Windows', settings: 'Change Face ID / PIN',
  security: 'Change security settings',
  'guest.shutdown': 'Shut down a VM', 'guest.reboot': 'Reboot a VM',
  'guest.stop': 'Force-stop a VM', 'manage': 'Change what the phone controls',
  upload: 'Upload files to the PC',
};
function scopeText(s){
  if (SCOPE_TEXT[s]) return SCOPE_TEXT[s];
  if (/^scene:/.test(s)) return 'Run a scene that powers off the PC';
  if (/^tile:/.test(s)) return 'Launch - it powers off the PC first';
  return 'Confirm';
}

/* The one prompt, for both unlocking and confirming a command. Resolves with
   whatever the server gave back ({ok} or {ok, stepup}); rejects on Cancel. */
function sfPrompt(purpose, scope){
  return new Promise((resolve, reject) => {
    const st = SFX.st || {};
    const pin = st.method === 'pin';
    const box = $('#sfLock');
    $('#sfTitle').textContent = purpose === 'unlock' ? 'Aether Remote is locked'
                                                      : scopeText(scope);
    $('#sfSub').textContent = purpose === 'unlock'
      ? (pin ? 'Enter your PIN to open it.' : 'Unlock with Face ID to open it.')
      : (pin ? 'Enter your PIN to confirm.' : 'Confirm with Face ID.');
    $('#sfPinRow').hidden = !pin;
    $('#sfGo').textContent = pin ? (purpose === 'unlock' ? 'Unlock' : 'Confirm')
                                 : (purpose === 'unlock' ? 'Unlock with Face ID'
                                                         : 'Confirm with Face ID');
    $('#sfCancel').hidden = purpose === 'unlock';
    $('#sfErr').textContent = st.locked_for
      ? 'Too many wrong tries. Try again in ' + Math.ceil(st.locked_for / 60) + ' min.' : '';
    $('#sfPin').value = '';
    box.hidden = false;
    if (pin) setTimeout(() => $('#sfPin').focus(), 80);

    const done = (fn, v) => { box.hidden = true; cleanup(); fn(v); };
    const go = async () => {
      $('#sfGo').disabled = true; $('#sfErr').textContent = '';
      try {
        const body = { purpose, scope };
        if (pin) body.pin = $('#sfPin').value;
        else body.credential = await passkeyGet(purpose, scope);
        const res = await sfPost('/api/sf/verify', body);
        try { sessionStorage.setItem('aether.unlocked', '1'); } catch (e) {}
        done(resolve, res);
      } catch (e) {
        $('#sfErr').textContent = e.name === 'NotAllowedError'
          ? 'Face ID was cancelled. Tap to try again.' : e.message;
        $('#sfPin').value = '';
        if (e.data && e.data.locked_for) SFX.st.locked_for = e.data.locked_for;
      }
      $('#sfGo').disabled = false;
    };
    const key = e => { if (e.key === 'Enter') go(); };
    const cancel = () => done(reject, new Error('Cancelled'));
    function cleanup(){
      $('#sfGo').removeEventListener('click', go);
      $('#sfCancel').removeEventListener('click', cancel);
      $('#sfPin').removeEventListener('keydown', key);
    }
    $('#sfGo').addEventListener('click', go);
    $('#sfCancel').addEventListener('click', cancel);
    $('#sfPin').addEventListener('keydown', key);
  });
}

/* Many calls can hit "locked" at once (the poll, a tap) - one prompt serves
   them all. */
async function sfUnlock(){
  if (!SFX.unlocking){
    SFX.unlocking = (async () => {
      await sfStatus();
      if (!SFX.st.enabled || SFX.st.unlocked) return;
      await sfPrompt('unlock', '');
    })().finally(() => { SFX.unlocking = null; });
  }
  return SFX.unlocking;
}

async function sfStepUp(scope){
  await sfStatus();
  const res = await sfPrompt('stepup', scope);
  return res.stepup;
}

/* Opening the app always asks - a still-valid unlock from last time doesn't
   count. A reload inside the same visit doesn't ask again. */
async function sfOnLaunch(){
  let st;
  try { st = await sfStatus(); } catch (e) { return; }
  if (!st.enabled || st.local) return;
  let fresh = true;
  try { fresh = !sessionStorage.getItem('aether.unlocked'); } catch (e) {}
  if (fresh){ await sfPost('/api/sf/lock').catch(() => {}); st.unlocked = false; }
  if (!st.unlocked) await sfUnlock();
}

/* Away for 5+ minutes -> locked again when you come back. */
const SF_AWAY_MS = 5 * 60 * 1000;
document.addEventListener('visibilitychange', async () => {
  try {
    if (document.hidden){ sessionStorage.setItem('aether.hiddenAt', String(Date.now())); return; }
    const at = +sessionStorage.getItem('aether.hiddenAt') || 0;
    if (!at || Date.now() - at < SF_AWAY_MS) return;
    sessionStorage.removeItem('aether.hiddenAt');
    if (!SFX.st || !SFX.st.enabled || SFX.st.local) return;
    sessionStorage.removeItem('aether.unlocked');
    await sfPost('/api/sf/lock').catch(() => {});
    await sfUnlock();
  } catch (e) {}
});

/* Setting it up (or changing it). `why` = 'first' when a power command
   needs it and nothing is set up yet. */
function sfSetup(why){
  return new Promise(async (resolve, reject) => {
    let st;
    try { st = await sfStatus(); } catch (e) { return reject(e); }
    const canFace = st.passkeys_possible && !!window.PublicKeyCredential;
    openSheet(why === 'first' ? 'Protect power commands' : 'Face ID & PIN', `
      <div style="font-size:13px;color:var(--muted);line-height:1.55;margin:12px 0 14px">
        ${why === 'first'
          ? (isPC() ? 'Shut down, restart and sign out need Face ID or a PIN every time. Pick one - it also locks the app each time you open it.'
                    : 'Shutting down, rebooting or force-stopping a VM needs a PIN every time. Pick one - it also locks the app each time you open it.')
          : (isPC() ? 'Asked when you open the app, and for every shut down, restart or sign out.'
                    : 'Asked when you open the app, and for every VM shut down, reboot or force stop.')}
      </div>
      ${canFace ? `<div class="btn wide pri" id="sfUseFace">Use Face ID</div>` :
        `<div style="font-size:11.5px;color:var(--muted);margin-bottom:10px">${isPC()
          ? "Face ID needs this PC's secure address (Tailscale with HTTPS). A PIN works here."
          : 'Face ID needs a secure (https) address. This server is on plain http, so it uses a PIN.'}</div>`}
      <div style="margin-top:12px">
        <div class="t" style="margin-bottom:7px">${canFace ? 'Or use a PIN' : 'Choose a PIN'}</div>
        <input class="field" id="sfNewPin" type="password" inputmode="numeric"
               autocomplete="new-password" maxlength="12" placeholder="4 to 12 digits">
        <input class="field" id="sfNewPin2" type="password" inputmode="numeric"
               autocomplete="new-password" maxlength="12" placeholder="Same PIN again"
               style="margin-top:8px">
        <div class="btn wide" id="sfUsePin" style="margin-top:9px">Use this PIN</div>
      </div>
      ${st.enabled && why !== 'first' ? `<div class="btn wide" id="sfOff"
          style="margin-top:18px;color:var(--bad)">Turn off Face ID / PIN</div>` : ''}
      <div id="sfSetErr" style="color:var(--bad);font-size:12px;min-height:16px;margin-top:9px"></div>`,
      `<button class="btn" id="sheetClose">Cancel</button>`);

    const err = m => { const el = $('#sfSetErr'); if (el) el.textContent = m; };
    // Changing an existing one needs the current Face ID / PIN first.
    const proof = async () => st.enabled ? { stepup: await sfStepUp('settings') } : {};
    const finish = async () => {
      try { sessionStorage.setItem('aether.unlocked', '1'); } catch (e) {}
      await sfStatus(); closeSheet(); toast('Saved'); resolve();
    };

    const face = $('#sfUseFace');
    if (face) face.addEventListener('click', async () => {
      try {
        const p = await proof();
        const credential = await passkeyCreate();
        await sfPost('/api/sf/register', { credential, label: navigator.platform || 'Phone', ...p });
        await finish();
      } catch (e) {
        err(e.name === 'NotAllowedError' ? 'Face ID was cancelled.' :
            e.name === 'InvalidStateError' ? 'This phone is already set up.' : e.message);
      }
    });
    $('#sfUsePin').addEventListener('click', async () => {
      const a = $('#sfNewPin').value, b2 = $('#sfNewPin2').value;
      if (!/^\d{4,12}$/.test(a)) return err('A PIN is 4 to 12 digits.');
      if (a !== b2) return err("The two PINs don't match.");
      try { await sfPost('/api/sf/pin', { pin: a, ...(await proof()) }); await finish(); }
      catch (e) { err(e.message); }
    });
    const off = $('#sfOff');
    if (off) off.addEventListener('click', async () => {
      try {
        await sfPost('/api/sf/disable', await proof());
        await sfStatus(); closeSheet(); toast('Face ID / PIN turned off'); resolve();
      } catch (e) { err(e.message); }
    });
    $('#sheetClose').addEventListener('click', () => reject(new Error('cancelled')), { once: true });
  });
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
    // A stat tile names which stat it shows in t.ref (cpu/ram/disk/gpu/temp/
    // battery). The server hands them all over live in S.stats; the tile is
    // just whichever one this is.
    const s = (S && S.stats && S.stats[t.ref]) ||
              {label: t.label || t.ref || 'Stat', big: '—', unit: '', pct: 0};
    const bars = [38, 52, 44, 68, s.pct || 0];
    return `<div class="stat">
      <div><div class="k">${esc(s.label || t.label || 'Stat')}</div>
        <div class="v"${s.bad ? ' style="color:var(--bad)"' : ''}>${esc(String(s.big))}<span style="font-size:10px;color:var(--muted);font-weight:400"> ${esc(s.unit || '')}</span></div></div>
      <div class="bars">${bars.map(
        (h,i) => `<i style="height:${Math.max(8,h)}%;${i===4?'background:var(--accent2)':''}"></i>`).join('')}</div>
    </div>`;
  }

  case 'scene':
    return `<div class="row">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--cyan)" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.8H20l-4.9 3.6 1.9 5.8L12 14.6 7 18.2l1.9-5.8L4 8.8h6.1z"/></svg>
      <div class="nm" style="flex-grow:1">${esc(t.label)}</div></div>`;

  case 'nowplaying':
    return nowPlayingInner(t);

  case 'timer': return timerInner(t);
  case 'appvol': return appvolInner(t);
  case 'audioout': return audiooutInner(t);
  case 'windows': return windowsInner(t);
  case 'gamestats': return gamestatsInner(t);
  case 'chat': return chatInner(t);

  case 'guest':
    return guestInner(t);

  case 'service':
  case 'docker':
    return svcInner(t);

  case 'link':
    return linkInner(t);

  default:
    return `<div class="ico"><div class="lab">${esc(t.label || t.kind)}</div></div>`;
  }
}

/* ---- homelab tiles ---- */
function hlDot(state){
  const c = state === 'on' ? 'var(--good,#34d399)' : state === 'busy'
    ? 'var(--warn,#f2b95e)' : 'var(--line2)';
  return `<span style="width:9px;height:9px;border-radius:50%;flex:0 0 auto;
    background:${c};${state === 'on' ? 'box-shadow:0 0 8px ' + c : ''}"></span>`;
}

function guestInner(t){
  const g = (S && S.guests && S.guests[t.ref]) || null;
  const st = g ? g.status : 'unknown';
  const on = st === 'running';
  const kind = /^lxc:/.test(t.ref) ? 'CT' : 'VM';
  const id = t.ref.split(':')[1] || '';
  const name = esc(t.label || (g && g.name) || t.ref);
  const pct = (v) => Math.max(0, Math.min(100, Math.round(v || 0)));
  if (t.w === 1){
    return `<div class="ico">${hlDot(on ? 'on' : 'off')}
      <div class="lab">${name}</div></div>`;
  }
  const sub = on && g ? `${kind} ${id} · ${pct(g.cpu)}% CPU · ${pct(g.mem)}% RAM`
                      : `${kind} ${id} · ${esc(st)}`;
  const bars = (t.h >= 2 && on && g) ? `
    <div style="display:flex;flex-direction:column;gap:6px;margin-top:10px">
      ${[['CPU', g.cpu], ['RAM', g.mem]].map(([k, v]) => `
        <div style="display:flex;align-items:center;gap:8px;font-size:10.5px;color:var(--muted)">
          <span style="width:26px">${k}</span>
          <span style="flex-grow:1;height:5px;border-radius:3px;background:var(--line2);overflow:hidden">
            <i style="display:block;height:100%;width:${pct(v)}%;background:var(--accent2)"></i></span>
          <span style="width:30px;text-align:right">${pct(v)}%</span></div>`).join('')}
    </div>` : '';
  return `<div style="display:flex;flex-direction:column;justify-content:center;
      height:100%;padding:10px 13px;min-width:0">
    <div style="display:flex;align-items:center;gap:8px;min-width:0">${hlDot(on ? 'on' : 'off')}
      <div style="font-size:12.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;
        white-space:nowrap">${name}</div></div>
    <div style="font-size:9.5px;color:var(--muted);margin-top:4px;overflow:hidden;
      text-overflow:ellipsis;white-space:nowrap">${sub}</div>${bars}</div>`;
}

function svcInner(t){
  const map = t.kind === 'docker' ? (S && S.docker) : (S && S.services);
  const st = (map && map[t.ref]) || 'unknown';
  const on = st === 'running' || st === 'active';
  const label = esc(t.label || t.ref);
  if (t.w === 1) return `<div class="ico">${hlDot(on ? 'on' : 'off')}<div class="lab">${label}</div></div>`;
  return `<div class="row">${hlDot(on ? 'on' : 'off')}
    <div style="flex-grow:1;min-width:0"><div class="nm">${label}</div>
      <div class="sub">${esc(st)} · tap twice to restart</div></div></div>`;
}

function linkInner(t){
  const art = `/api/art?id=${encodeURIComponent('link:' + t.ref)}`;
  const letter = esc(((t.label || '?').trim()[0] || '?').toUpperCase());
  const img = (px) => `<img src="${art}" alt="" style="width:${px}px;height:${px}px;
      border-radius:${Math.round(px / 4)}px;object-fit:cover"
      onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
    <span style="display:none;width:${px}px;height:${px}px;border-radius:${Math.round(px / 4)}px;
      align-items:center;justify-content:center;font-weight:700;font-size:${Math.round(px / 2.2)}px;
      background:linear-gradient(140deg,var(--primary),var(--secondary));color:#fff">${letter}</span>`;
  if (t.w >= 2 && t.h >= 2)
    return `<div class="ico">${img(44)}<div class="lab">${esc(t.label)}</div></div>`;
  if (t.w === 1) return `<div class="ico">${img(28)}<div class="lab">${esc(t.label)}</div></div>`;
  return `<div class="row">${img(30)}<div style="min-width:0">
    <div class="nm">${esc(t.label)}</div><div class="sub">Open</div></div></div>`;
}

/* A guest's own sheet: its numbers and its power buttons. Shut down, reboot
   and force stop ask for the PIN (the server insists); start doesn't. */
function openGuest(t){
  const g = (S && S.guests && S.guests[t.ref]) || {};
  const on = g.status === 'running';
  const kind = /^lxc:/.test(t.ref) ? 'Container' : 'VM';
  const pct = (v) => Math.round(v || 0) + '%';
  const row = (k, v) => `<div class="srow"><div style="flex-grow:1"><div class="t">${k}</div></div>
    <div style="color:var(--muted);font-size:13px">${esc(v)}</div></div>`;
  const btn = (act, text, cls, css) => `<div class="btn wide ${cls || ''}" data-gact="${act}"
    style="margin-top:9px;${css || ''}">${text}</div>`;
  openSheet(t.label || g.name || t.ref, `
    <div style="margin-top:10px">
      ${row(kind, t.ref.split(':')[1] || '')}
      ${row('Status', g.status || 'unknown')}
      ${on ? row('CPU', pct(g.cpu)) + row('Memory', pct(g.mem)) +
             (g.uptime ? row('Up for', g.uptime) : '') : ''}
    </div>
    <div style="margin:14px 0 6px">
      ${on ? btn('shutdown', 'Shut down') + btn('reboot', 'Reboot') +
             btn('stop', 'Force stop', '', 'color:var(--bad)')
           : btn('start', 'Start', 'pri')}
    </div>
    <div style="font-size:11px;color:var(--muted);line-height:1.5;margin-bottom:10px">
      ${on ? 'Shut down asks the guest to stop cleanly. Force stop pulls the plug.' : ''}</div>`,
    `<div style="flex-grow:1"></div><button class="btn" id="sheetClose">Done</button>`);
  $('#sheetBody').querySelectorAll('[data-gact]').forEach(b => b.addEventListener('click', async () => {
    const action = b.dataset.gact;
    b.textContent = '…';
    try {
      await api('/api/tile', { kind: 'guest', ref: t.ref, action });
      closeSheet();
      toast({start: 'Starting ', shutdown: 'Shutting down ', reboot: 'Rebooting ',
             stop: 'Stopping '}[action] + (t.label || t.ref));
      setTimeout(poll, 1500); setTimeout(poll, 5000);
    } catch(err){
      if (err.message !== 'Cancelled') toast(err.message);
      openGuest(t);
    }
  }));
}

/* ================= now playing =================
 * Live from the PC's own media controls (media.py): whatever Windows shows in
 * its volume flyout - Spotify, a YouTube tab, a game launcher. The server
 * says where the track is right now and whether it's moving; between polls
 * the bar is carried forward here, so it glides instead of jumping every
 * 2.5 s. The artwork's main colour (picked on the PC) tints the tile, blurred
 * and darkened behind the cover, the way Spotify does its headers. */
const NP = { at: 0, seen: null, scrub: null, scrubEnd: 0, open: false };
const NPI = {
  prev: '<svg viewBox="0 0 24 24"><path d="M5.5 5h2.2v14H5.5zM20 6v12a1 1 0 0 1-1.6.8l-8.6-6a1 1 0 0 1 0-1.6l8.6-6A1 1 0 0 1 20 6z"/></svg>',
  next: '<svg viewBox="0 0 24 24"><path d="M16.3 5h2.2v14h-2.2zM4 6v12a1 1 0 0 0 1.6.8l8.6-6a1 1 0 0 0 0-1.6l-8.6-6A1 1 0 0 0 4 6z"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M7 4.9v14.2a1.1 1.1 0 0 0 1.7.9l11-7.1a1.1 1.1 0 0 0 0-1.8l-11-7.1A1.1 1.1 0 0 0 7 4.9z"/></svg>',
  pause: '<svg viewBox="0 0 24 24"><rect x="5.5" y="4" width="4.6" height="16" rx="1.3"/><rect x="13.9" y="4" width="4.6" height="16" rx="1.3"/></svg>',
  pad: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8h12a4 4 0 0 1 3.9 4.9l-.9 4a2.5 2.5 0 0 1-4.3 1.1L14.5 16h-5l-2.2 2a2.5 2.5 0 0 1-4.3-1.1l-.9-4A4 4 0 0 1 6 8z"/><path d="M8 11v3M6.5 12.5h3M15.5 12h.01M17.5 13.5h.01"/></svg>',
  note: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>',
  down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>',
  volLo: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 9h3.5L12 5v14l-4.5-4H4z"/></svg>',
  volHi: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 9h3.5L12 5v14l-4.5-4H4z" fill="currentColor" stroke="none"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/></svg>',
};

/* The line under the title: artist / show - or, for a game, when you started. */
function npSub(n){
  if (n.kind === 'game') return 'Started ' + new Date(Date.now() - npPos(n) * 1000)
    .toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return n.artist || n.album || n.appName || '';
}

function npNow(){
  const n = S && S.nowplaying;
  if (!n) return null;
  if (n !== NP.seen){ NP.seen = n; NP.at = performance.now(); }
  return n;
}
function npPos(n){
  let p = +n.pos || 0;
  if (n.kind === 'game') return p + (performance.now() - NP.at) / 1000;   // time in game
  if (n.playing && n.dur) p += (performance.now() - NP.at) / 1000 * (n.rate || 1);
  return Math.max(0, n.dur ? Math.min(p, n.dur) : p);
}
function npTime(sec){
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60),
        x = String(sec % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${x}` : `${m}:${x}`;
}
function npRGB(n){
  const c = n && Array.isArray(n.color) && n.color.length === 3 ? n.color : [88, 70, 160];
  return c.map(v => Math.max(0, Math.min(255, v | 0))).join(',');
}
const npArt = n => n.art ? '/api/np/art?v=' + encodeURIComponent(n.art) : '';
const npIcon = n => n.icon ? '/api/np/icon?v=' + encodeURIComponent(n.icon) : '';
const npCan = n => n.can || { toggle: true, next: true, prev: true, seek: false };

function npBg(n){
  const art = npArt(n) || npIcon(n);
  return `<div class="np-bg" style="--np:${npRGB(n)}">${
    art ? `<img src="${art}" alt="" onerror="this.remove()">` : ''}</div>`;
}
function npCover(n, cls){
  const art = npArt(n), icon = npIcon(n);
  // Posters (a show, a game's box art) keep their shape; everything else is square.
  const ar = art && n.aspect ? ` style="--ar:${Math.max(0.66, Math.min(1, +n.aspect))}"` : '';
  if (art) return `<div class="np-cover ${cls || ''}"${ar}><img src="${art}" alt=""></div>`;
  return `<div class="np-cover is-icon ${cls || ''}" style="--np:${npRGB(n)}">${
    icon ? `<img src="${icon}" alt="" onerror="this.outerHTML=NPI.note">` : NPI.note}</div>`;
}
function npBadge(n){
  const icon = npIcon(n);
  return `<div class="np-app">${icon ? `<img src="${icon}" alt="">` : n.kind === 'game' ? NPI.pad : NPI.note}<span>${
    esc(n.appName || 'Now playing')}</span></div>`;
}
function npSeek(n){
  if (n.kind === 'game') return `<div class="np-seek np-game"><div class="np-times">
    <span><i class="np-livedot"></i>Playing for <b class="np-gt">${npTime(npPos(n))}</b></span></div></div>`;
  if (!(n.dur > 0)) return `<div class="np-seek np-nodur"><div class="np-track"></div>
    <div class="np-times"><span>${n.playing ? 'Live' : ''}</span><span></span></div></div>`;
  const p = npPos(n), pct = (p / n.dur * 100).toFixed(2) + '%';
  return `<div class="np-seek ${npCan(n).seek ? 'can' : ''}">
    <div class="np-track"><i class="np-fill" style="width:${pct}"></i><b class="np-knob" style="left:${pct}"></b></div>
    <div class="np-times"><span class="np-el">${npTime(p)}</span><span class="np-rem">-${npTime(n.dur - p)}</span></div></div>`;
}
const NPS = {
  back10: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3"/><path d="M4.5 3.8v3.6h3.6"/><text x="12.2" y="15.4" text-anchor="middle" font-size="7.2" font-weight="700" fill="currentColor" stroke="none">10</text></svg>',
  fwd10: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 3.8v3.6h-3.6"/><text x="11.8" y="15.4" text-anchor="middle" font-size="7.2" font-weight="700" fill="currentColor" stroke="none">10</text></svg>',
};

function npBtns(n, which){
  if (n.kind === 'game') return `<div class="np-ctl"><button class="np-pill" data-npc="screen">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>
    <span>Show the screen</span></button></div>`;
  const can = npCan(n);
  const b = (op, icon, en, cls) => `<button class="np-b ${cls || ''}" data-npc="${op}"
    aria-label="${op === 'toggle' ? (n.playing ? 'Pause' : 'Play') : op === 'next' ? 'Next'
      : op === 'back10' ? 'Back 10 seconds' : op === 'fwd10' ? 'Forward 10 seconds' : 'Previous'}"
    ${en ? '' : 'disabled'}>${icon}</button>`;
  if (which === 'full'){
    const sk = can.seek && n.dur > 0;
    return `<div class="np-ctl np-ctl-full">${b('prev', NPI.prev, can.prev)}${
      b('back10', NPS.back10, sk, 'np-sk')}${b('toggle', n.playing ? NPI.pause : NPI.play, can.toggle, 'np-pp')}${
      b('fwd10', NPS.fwd10, sk, 'np-sk')}${b('next', NPI.next, can.next)}</div>`;
  }
  return `<div class="np-ctl">${which !== 'pp' ? b('prev', NPI.prev, can.prev) : ''}${
    b('toggle', n.playing ? NPI.pause : NPI.play, can.toggle, 'np-pp')}${
    which !== 'pp' ? b('next', NPI.next, can.next) : ''}</div>`;
}

function npSize(t){
  return t.h >= 3 && t.w >= 2 ? 'l' : t.h >= 2 ? 'm' : 's';
}

function nowPlayingInner(t){
  t = t || { w: 4, h: 2 };
  const n = npNow(), size = npSize(t);
  const cls = `np np-${size}${t.w <= 2 ? ' np-narrow' : ''}${t.w === 1 ? ' np-w1' : ''}`;
  if (!n) return `<div class="${cls} np-empty"><div class="np-idle">${NPI.note}
    <span>Not playing</span>${size !== 's' ? '<small>Play something on the PC and it shows up here.</small>' : ''}</div></div>`;
  const text = `<div class="np-text">${size !== 's' ? npBadge(n) : ''}
    <div class="np-title">${esc(n.title)}</div>
    <div class="np-artist">${esc(npSub(n))}</div></div>`;
  if (t.w === 1) return `<div class="${cls}">${npBg(n)}${npCover(n)}${npBtns(n, 'pp')}</div>`;
  if (size === 's'){
    const line = n.dur > 0 ? `<div class="np-line"><i class="np-fill" style="width:${(npPos(n) / n.dur * 100).toFixed(2)}%"></i></div>` : '';
    return `<div class="${cls}">${npBg(n)}${npCover(n)}${text}${npBtns(n, t.w >= 4 ? 'all' : 'pp')}${line}</div>`;
  }
  if (size === 'l') return `<div class="${cls}" style="--h:${t.h}">${npBg(n)}
    ${npCover(n, 'np-big')}${text}${npSeek(n)}${npBtns(n, 'all')}</div>`;
  return `<div class="${cls}">${npBg(n)}<div class="np-top">${npCover(n)}${text}</div>
    ${npSeek(n)}${npBtns(n, 'all')}</div>`;
}

/* What would make a tile look different - everything but the position,
   which the ticker moves on its own. */
function npKey(t){
  const n = S && S.nowplaying;
  return JSON.stringify([npSize(t), t.w, n && [n.title, n.artist, n.album, n.appName,
    n.art, n.icon, n.playing, n.dur > 0, n.can, n.color, n.kind, n.aspect]]);
}

/* The bar and the clock, four times a second, without touching anything
   else on the page. */
function npTick(){
  const n = npNow();
  if (n && n.kind === 'game'){
    const g = npTime(npPos(n));
    document.querySelectorAll('.np-gt').forEach(e => { if (e.textContent !== g) e.textContent = g; });
    return;
  }
  if (!n || !(n.dur > 0)) return;
  const p = NP.scrub ? NP.scrub.pos : npPos(n);
  const pct = (p / n.dur * 100).toFixed(2) + '%';
  document.querySelectorAll('.np-fill').forEach(e => { e.style.width = pct; });
  document.querySelectorAll('.np-knob').forEach(e => { e.style.left = pct; });
  const el = npTime(p), rem = '-' + npTime(n.dur - p);
  document.querySelectorAll('.np-el').forEach(e => { if (e.textContent !== el) e.textContent = el; });
  document.querySelectorAll('.np-rem').forEach(e => { if (e.textContent !== rem) e.textContent = rem; });
}
setInterval(() => { if (!document.hidden) npTick(); }, 250);

/* Re-draw every now-playing surface (after a tap, before the PC answers). */
function npPaint(){
  if (L && !editing) for (const sec of L.sections) for (const t of sec.tiles){
    if (t.kind !== 'nowplaying') continue;
    const el = board.querySelector('[data-tile="' + t.id + '"]');
    if (el){ el.innerHTML = nowPlayingInner(t); el._npk = npKey(t); }
  }
  if (NP.open) npFullDraw();
}

async function npCmd(op, pos){
  if (op === 'screen'){ closeNowPlaying(); openDesktop({ ref: '0' }); return; }
  if (op === 'back10' || op === 'fwd10'){
    const cur = S && S.nowplaying;
    if (!cur || !(cur.dur > 0)) return;
    pos = Math.max(0, Math.min(cur.dur - 1, npPos(cur) + (op === 'back10' ? -10 : 10)));
    op = 'seek';
  }
  const n = S && S.nowplaying;
  if (n){
    // Answer the finger straight away; the PC's reply corrects it if needed.
    n.pos = op === 'seek' ? pos : npPos(n);
    if (op === 'toggle') n.playing = !n.playing;
    NP.seen = n; NP.at = performance.now();
    npPaint();
  }
  if (navigator.vibrate) navigator.vibrate(8);
  try {
    const j = await api('/api/media', op === 'seek' ? { op, pos } : { op });
    if (S && j.nowplaying !== undefined){ S.nowplaying = j.nowplaying; npPaint(); }
    if (op === 'next' || op === 'prev') setTimeout(poll, 900);
  } catch(err){
    toast(err.message);
    poll();
  }
}

/* Scrubbing: press anywhere on the bar and drag, iOS-style - the bar thickens
   under your finger and the time follows it; letting go seeks the PC. */
function npFrac(e, sk){
  const r = sk.querySelector('.np-track').getBoundingClientRect();
  return Math.max(0, Math.min(1, (e.clientX - r.left) / (r.width || 1)));
}
document.addEventListener('pointerdown', e => {
  const sk = e.target.closest('.np-seek.can');
  const n = S && S.nowplaying;
  if (!sk || editing || !n || !(n.dur > 0)) return;
  // Ours, not the board's: no long-press-to-edit, no tile tap.
  e.stopPropagation(); e.preventDefault();
  NP.scrub = { el: sk, id: e.pointerId, pos: npFrac(e, sk) * n.dur };
  sk.classList.add('scrub');
  npTick();
}, true);
window.addEventListener('pointermove', e => {
  if (!NP.scrub || e.pointerId !== NP.scrub.id) return;
  const n = S && S.nowplaying;
  if (!n) return;
  NP.scrub.pos = npFrac(e, NP.scrub.el) * n.dur;
  npTick();
});
['pointerup', 'pointercancel'].forEach(ev => window.addEventListener(ev, e => {
  if (!NP.scrub || e.pointerId !== NP.scrub.id) return;
  const { el, pos } = NP.scrub;
  NP.scrub = null; NP.scrubEnd = Date.now();
  el.classList.remove('scrub');
  if (ev === 'pointerup') npCmd('seek', Math.round(pos * 10) / 10);
}));

/* The full player: tap the tile and it rises up over everything - big cover,
   the track, the bar, the buttons and the PC's volume. Swipe down to close. */
function npFullEl(){
  let el = document.getElementById('npFull');
  if (!el){
    el = document.createElement('div');
    el.id = 'npFull';
    el.className = 'npf';
    document.body.appendChild(el);
    el.addEventListener('click', e => {
      if (e.target.closest('[data-npclose]')) return closeNowPlaying();
      const c = e.target.closest('[data-npc]');
      if (c && !c.disabled) npCmd(c.dataset.npc);
    });
    el.addEventListener('input', e => {
      if (!e.target.matches('.npf-vol')) return;
      const v = +e.target.value;
      e.target.style.setProperty('--pct', v + '%');
      pending = v; flushVolume();
    });
    el.addEventListener('pointerdown', e => {
      if (e.target.matches('.npf-vol')){ draggingSlider = true; return; }
      if (e.target.closest('.np-seek,button,input')) return;
      NP.drag = { y: e.clientY, id: e.pointerId, dy: 0 };
      el.style.transition = 'none';
    });
    el.addEventListener('pointermove', e => {
      if (!NP.drag || e.pointerId !== NP.drag.id) return;
      NP.drag.dy = Math.max(0, e.clientY - NP.drag.y);
      el.style.transform = `translateY(${NP.drag.dy}px)`;
    });
    const end = () => {
      if (draggingSlider){ draggingSlider = false; suppressUntil = Date.now() + 700; }
      if (!NP.drag) return;
      const dy = NP.drag.dy; NP.drag = null;
      el.style.transition = ''; el.style.transform = '';
      if (dy > 110) closeNowPlaying();
    };
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
  }
  return el;
}
function npFullDraw(){
  const el = npFullEl(), n = npNow();
  const key = JSON.stringify([n && [n.title, n.artist, n.album, n.appName, n.art, n.icon,
    n.playing, n.dur > 0, n.can, n.color, n.kind, n.aspect]]);
  if (el._k !== key){
    el._k = key;
    el.innerHTML = !n
      ? `<div class="np-bg" style="--np:40,40,56"></div><div class="npf-in">
           <div class="npf-head"><button class="npf-x" data-npclose aria-label="Close">${NPI.down}</button></div>
           <div class="np-idle" style="flex:1">${NPI.note}<span>Not playing</span>
             <small>Play something on the PC and it shows up here.</small></div></div>`
      : `${npBg(n)}<div class="npf-in">
           <div class="npf-head"><button class="npf-x" data-npclose aria-label="Close">${NPI.down}</button>
             ${npBadge(n)}<span style="width:38px"></span></div>
           <div class="npf-art">${npCover(n, 'np-big')}</div>
           <div class="npf-meta"><div class="np-title">${esc(n.title)}</div>
             <div class="np-artist">${esc(npSub(n))}</div>
             ${n.album && n.album !== n.title ? `<div class="npf-album">${esc(n.album)}</div>` : ''}
             </div>
           ${npSeek(n)}${npBtns(n, 'full')}
           <div class="npf-volrow">${NPI.volLo}
             <input type="range" class="npf-vol" min="0" max="100" aria-label="PC volume">${NPI.volHi}</div>
         </div>`;
  }
  const vol = el.querySelector('.npf-vol');
  if (vol && S && !draggingSlider && Date.now() >= suppressUntil && +vol.value !== S.volume){
    vol.value = S.volume;
    vol.style.setProperty('--pct', S.volume + '%');
  }
  npTick();
}
function openNowPlaying(){
  NP.open = true;
  const el = npFullEl();
  el._k = null;
  npFullDraw();
  requestAnimationFrame(() => el.classList.add('show'));
}
function closeNowPlaying(){
  NP.open = false;
  const el = document.getElementById('npFull');
  if (el) el.classList.remove('show');
}

function iconFor(ref){
  const s = 'width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"';
  const map = {
    'mute': `<svg ${s}><path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M22 9l-6 6"/><path d="M16 9l6 6"/></svg>`,
    'keeper': `<svg ${s}><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>`,
    'media.playpause': `<svg ${s}><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>`,
    'media.next': `<svg ${s}><path d="M5 4l10 8-10 8z"/><path d="M19 5v14"/></svg>`,
    'media.prev': `<svg ${s}><path d="M19 4L9 12l10 8z"/><path d="M5 5v14"/></svg>`,
    'micmute': `<svg ${s}><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>`,
    'discord.mute': `<svg ${s}><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M4 4l16 16"/></svg>`,
    'discord.deafen': `<svg ${s}><path d="M4 15v-3a8 8 0 0 1 16 0v3"/><rect x="3" y="14" width="4.5" height="6.5" rx="1.6"/><rect x="16.5" y="14" width="4.5" height="6.5" rx="1.6"/><path d="M3 3l18 18"/></svg>`,
    'screen.shot': `<svg ${s}><path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2"/><circle cx="12" cy="12" r="3"/></svg>`,
    'timer': XI.moon.replace('<svg', '<svg width="20" height="20"'),
    'appvol': XI.mixer.replace('<svg', '<svg width="20" height="20"'),
    'audioout': XI.headset.replace('<svg', '<svg width="20" height="20"'),
    'windows': XI.windows.replace('<svg', '<svg width="20" height="20"'),
    'gamestats': XI.pad.replace('<svg', '<svg width="20" height="20"'),
    'chat': XI.spark.replace('<svg', '<svg width="20" height="20"'),
    'media.back10': `<svg ${s}><path d="M4 12a8 8 0 1 0 2.3-5.7"/><path d="M4 4v4h4"/><text x="12" y="15.5" text-anchor="middle" font-size="7.5" font-weight="700" fill="currentColor" stroke="none">10</text></svg>`,
    'media.fwd10': `<svg ${s}><path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v4h-4"/><text x="12" y="15.5" text-anchor="middle" font-size="7.5" font-weight="700" fill="currentColor" stroke="none">10</text></svg>`,
    'media.back30': `<svg ${s}><path d="M4 12a8 8 0 1 0 2.3-5.7"/><path d="M4 4v4h4"/><text x="12" y="15.5" text-anchor="middle" font-size="7.5" font-weight="700" fill="currentColor" stroke="none">30</text></svg>`,
    'media.fwd30': `<svg ${s}><path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v4h-4"/><text x="12" y="15.5" text-anchor="middle" font-size="7.5" font-weight="700" fill="currentColor" stroke="none">30</text></svg>`,
    'screen.off': `<svg ${s}><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8"/></svg>`,
    'power.lock': `<svg ${s}><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>`,
    'power.sleep': `<svg ${s}><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>`,
    'power.restart': `<svg ${s}><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>`,
    'power.shutdown': `<svg ${s}><path d="M12 3v9"/><path d="M6.4 6.4a9 9 0 1 0 11.2 0"/></svg>`,
    'power.signout': `<svg ${s}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>`,
    'volume': `<svg ${s}><path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>`,
    'stream': `<svg ${s}><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/></svg>`,
    'nowplaying': `<svg ${s}><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>`,
    'cpu': `<svg ${s}><rect x="8" y="8" width="8" height="8" rx="1"/><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/></svg>`,
    'gpu': `<svg ${s}><rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="9" cy="12" r="2.5"/><circle cx="16" cy="12" r="1.5"/></svg>`,
    'ram': `<svg ${s}><rect x="2" y="7" width="20" height="10" rx="1"/><path d="M6 17v2M10 17v2M14 17v2M18 17v2"/></svg>`,
    'disk': `<svg ${s}><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/></svg>`,
    'temp': `<svg ${s}><path d="M14 14.76V5a2 2 0 0 0-4 0v9.76a4 4 0 1 0 4 0z"/></svg>`,
    'battery': `<svg ${s}><rect x="2" y="7" width="18" height="10" rx="2"/><path d="M22 11v2"/></svg>`,
    'load': `<svg ${s}><path d="M3 17l5-6 4 3 5-7 4 5"/></svg>`,
    'uptime': `<svg ${s}><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`,
    'net': `<svg ${s}><path d="M7 4v14M3 14l4 4 4-4"/><path d="M17 20V6M13 10l4-4 4 4"/></svg>`,
    'zfs': `<svg ${s}><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></svg>`,
    'guests': `<svg ${s}><rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>`,
    'backup': `<svg ${s}><path d="M12 3v12M7 10l5 5 5-5"/><path d="M5 21h14"/></svg>`,
    'swap': `<svg ${s}><path d="M4 8h13l-3-3M20 16H7l3 3"/></svg>`,
  };
  return `<span style="color:var(--text);display:flex">${map[ref] || `<svg ${s}><circle cx="12" cy="12" r="9"/></svg>`}</span>`;
}

function toggleOn(ref){
  if (!S) return false;
  if (ref === 'mute') return !!S.muted;
  if (ref === 'keeper') return !!(S.keeper && S.keeper.enabled);
  if (ref === 'micmute') return !!(S.mic && S.mic.muted);
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
  if (t.kind === 'service' || t.kind === 'docker') cls.push('danger');
  if (t.kind === 'nowplaying' || t.kind === 'gamestats') cls.push('npt');
  if (!editing && t.kind === 'timer' && S && S.timer && S.timer.on) cls.push('on');
  if (t.accent) cls.push('acc');
  if (armed === t.id) cls.push('armed');
  return `<div class="${cls.join(' ')}" data-tile="${t.id}" data-sec="${secId}"
    style="grid-column:span ${t.w};grid-row:span ${t.h}${t.accent ? ';--acc:' + t.accent : ''}">
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
        const s = (S.stats && S.stats[t.ref]) || null;
        if (s){
          const k = el.querySelector('.k');
          if (k && k.textContent !== s.label) k.textContent = s.label;
          const v = el.querySelector('.v');
          if (v){
            v.innerHTML = `${esc(String(s.big))}<span style="font-size:10px;color:var(--muted);font-weight:400"> ${esc(s.unit || '')}</span>`;
            v.style.color = s.bad ? 'var(--bad)' : '';
          }
          const last = el.querySelector('.bars i:last-child');
          if (last) last.style.height = Math.max(8, s.pct || 0) + '%';
        }
      }

      else if (t.kind === 'nowplaying'){
        // Only when the track, state or buttons changed - the artwork must
        // not reload every poll, and the ticker moves the bar in between.
        if (NP.scrub) continue;
        const k = npKey(t);
        if (el._npk !== k){ el.innerHTML = tileInner(t); el._npk = k; }
      }

      else if (['timer', 'appvol', 'audioout', 'windows', 'gamestats', 'chat'].includes(t.kind)){
        // Only when something visible changed (the clocks tick on their own).
        if (t.kind === 'appvol' && MX.drag) continue;
        const html = tileInner(t);
        const key = html.replace(/(class="(?:gs-t|tm-left)"[^>]*>)[^<]*/g, '$1');
        if (el._xk !== key){ el.innerHTML = html; el._xk = key; }
        el.classList.toggle('on', t.kind === 'timer' && !!(S.timer && S.timer.on));
      }

      else if (t.kind === 'guest' || t.kind === 'service' || t.kind === 'docker'){
        // No images either - but only touch the DOM when something changed.
        const html = tileInner(t);
        if (el._last !== html){ el.innerHTML = html; el._last = html; }
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

/* Long-press anywhere on the board enters edit mode - the home-screen gesture.
   (Not the browser's own "Save image" menu on a game's art.) */
board.addEventListener('contextmenu', e => { if (e.target.closest('[data-tile]')) e.preventDefault(); });
let pressTimer = null, pressStart = null;
board.addEventListener('pointerdown', e => {
  if (editing || e.target.closest('[data-slider]') || e.target.closest('.mxr')) return;
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
  if (e.target.closest('[data-slider]') || e.target.closest('.mxr')) return;

  const addSec = e.target.closest('[data-addtile]');
  if (addSec){ openAdd(addSec.dataset.addtile); return; }

  // Now-playing buttons act on the button, not the whole tile.
  const npc = e.target.closest('[data-npc]');
  if (npc && !editing){
    e.stopPropagation();
    if (!npc.disabled) npCmd(npc.dataset.npc);
    return;
  }
  if (Date.now() - NP.scrubEnd < 400) return;     // the end of a drag, not a tap

  const el = e.target.closest('[data-tile]');
  if (!el) return;
  const found = findTile(el.dataset.tile);
  if (!found) return;
  const t = found.t;

  // In edit mode a tap does nothing: removing happens on pointerdown above,
  // and dragging/resizing own the rest.
  if (editing) return;

  if (t.kind === 'stream'){ openDesktop(t); return; }
  if (t.kind === 'nowplaying'){ openNowPlaying(); return; }
  if (t.kind === 'chat'){ if (CH.info && CH.info.ready) showTab('chat'); else toast('Set the chat up in the PC app → Settings → Chatbox'); return; }
  if (t.kind === 'windows'){ openWindows(); return; }
  if (t.kind === 'gamestats'){ openDesktop({ ref: '0' }); return; }
  if (t.kind === 'appvol'){
    const ic = e.target.closest('.mx-ic'), row = e.target.closest('[data-mxapp]');
    if (ic && row && row.dataset.mxapp){
      const a = MX.apps.find(x => x.app === row.dataset.mxapp);
      if (a) api('/api/mixer', { app: a.app, mute: !a.muted }).then(() => { a.muted = !a.muted; toast((a.muted ? 'Muted ' : 'Unmuted ') + a.name); return mxPoll(); })
        .then(() => refresh()).catch(err => toast(err.message));
    }
    return;
  }
  if (t.kind === 'timer' || t.kind === 'audioout'){
    (t.kind === 'timer' ? timerTap(t) : audiooutTap(t)).catch(err => { if (err.message !== 'Cancelled') toast(err.message); });
    return;
  }
  if (t.kind === 'action' && t.ref === 'screen.shot'){ takeScreenshot(); return; }
  if (t.kind === 'slider') return;
  if (t.kind === 'guest'){ openGuest(t); return; }
  if (t.kind === 'link'){
    const l = S && S.links && S.links[t.ref];
    if (l && l.url) window.open(l.url, '_blank', 'noopener');
    else toast('That link no longer exists');
    return;
  }

  const destructive = t.kind === 'action' && /restart|shutdown|signout/.test(t.ref);
  const restart = t.kind === 'service' || t.kind === 'docker';
  if ((destructive || restart) && armed !== t.id){
    armed = t.id; render();
    clearTimeout(armTimer);
    armTimer = setTimeout(() => { armed = null; render(); }, 4000);
    toast(restart ? 'Tap again to restart ' + t.label : 'Tap again to ' + t.label.toLowerCase());
    return;
  }
  clearTimeout(armTimer); armed = null;

  el.classList.add('sent');
  setTimeout(() => el.classList.remove('sent'), 500);

  try {
    const body = { kind: t.kind, ref: t.ref, id: t.id };
    if (destructive) body.confirm = true;
    if (t.kind === 'toggle' && t.ref === 'keeper' && S)
      body.target = t.opts && t.opts.target != null ? t.opts.target : S.volume;
    const r = await api('/api/tile', body);
    if (r && r.volume !== undefined) S = r;
    if (r && r.nowplaying && S){ S.nowplaying = r.nowplaying; npPaint(); }
    if (t.kind === 'scene') toast('Running ' + t.label);
    if (t.kind === 'toggle' && t.ref === 'micmute') toast(toggleOn('micmute') ? 'Microphone muted' : 'Microphone on');
    if (t.kind === 'action' && t.ref.startsWith('discord.')) toast(t.ref === 'discord.mute' ? 'Discord mute toggled' : 'Discord deafen toggled');
    if (restart) toast('Restarting ' + t.label);
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
  toast('Drag to move · hold for settings');
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

  dragging = { mode: 'move', id: found.t.id, moved: false, x0: e.clientX, y0: e.clientY };
  // Hold still on a tile = its settings. Moving first = dragging it.
  clearTimeout(editHold);
  const holdId = found.t.id;
  editHold = setTimeout(() => {
    if (!dragging || dragging.moved || dragging.id !== holdId) return;
    dragging = null;
    openTileSettings(holdId);
  }, 480);
  // Capture on the tile keeps the gesture alive even as the board re-renders.
  try { el.setPointerCapture(e.pointerId); } catch (err) {}
  e.preventDefault();
});

// On window, not the board: the gesture must keep receiving moves even when a
// re-render swaps out the tile the pointer was captured on, and even when the
// finger strays past the board's edge.
window.addEventListener('pointermove', e => {
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

  // ---- move: a ghost floats under the finger, the others reflow ----
  const cur = board.querySelector(`[data-tile="${dragging.id}"]`);
  if (!dragging.moved && Math.hypot(e.clientX - dragging.x0, e.clientY - dragging.y0) < 8) return;
  if (!dragging.moved){
    clearTimeout(editHold);
    if (!cur) return;
    const r = cur.getBoundingClientRect();
    dragging.moved = true;
    dragging.dx = e.clientX - r.left;
    dragging.dy = e.clientY - r.top;
    const g = cur.cloneNode(true);
    g.classList.add('ghost');
    g.style.position = 'fixed';
    g.style.left = '0'; g.style.top = '0';
    g.style.width = r.width + 'px';
    g.style.height = r.height + 'px';
    g.style.margin = '0';
    g.style.gridColumn = ''; g.style.gridRow = '';
    document.body.appendChild(g);
    dragging.ghost = g;
    document.body.classList.add('dragging');
    cur.classList.add('placeholder');
    if (navigator.vibrate) navigator.vibrate(8);
  }

  dragging.ghost.style.transform =
    `translate(${e.clientX - dragging.dx}px, ${e.clientY - dragging.dy}px) scale(1.05)`;

  // The ghost ignores pointer events, so this finds the tile underneath it.
  const over = document.elementFromPoint(e.clientX, e.clientY);
  const target = over && over.closest('[data-tile]');
  const tid = target && target.dataset.tile;
  if (!tid || tid === dragging.id) return;

  const from = findTile(dragging.id);
  const to = findTile(tid);
  if (!from || !to) return;

  from.sec.tiles.splice(from.i, 1);
  const dest = findTile(tid);                 // re-find: indices shifted
  if (!dest){ from.sec.tiles.splice(from.i, 0, from.t); return; }
  dest.sec.tiles.splice(dest.i, 0, from.t);
  dirty = true;
  render();
  const again = board.querySelector(`[data-tile="${dragging.id}"]`);
  if (again) again.classList.add('placeholder');
});

let editHold = null;
['pointerup', 'pointercancel'].forEach(ev => window.addEventListener(ev, () => {
  clearTimeout(editHold);
  if (!dragging) return;
  if (dragging.ghost) dragging.ghost.remove();
  document.body.classList.remove('dragging');
  if (dragging.el){ dragging.el.style.gridColumn = ''; dragging.el.style.gridRow = ''; }
  const cur = dragging.id && board.querySelector(`[data-tile="${dragging.id}"]`);
  if (cur) cur.classList.remove('placeholder');
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
  const after = sheetOnClose;
  sheetOnClose = null;
  if (after){ try { after(); } catch(e){} }
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
  { g:'Sound', kind:'slider', ref:'volume', label:'Volume', w:4, h:1 },
  { g:'Sound', kind:'toggle', ref:'mute', label:'Mute', w:1, h:1 },
  { g:'Sound', kind:'toggle', ref:'keeper', label:'Lock volume', w:2, h:1 },
  { g:'Sound', kind:'appvol', ref:'', label:'Volume mixer', w:4, h:2, multi:true },
  { g:'Sound', kind:'audioout', ref:'', label:'Audio output', w:2, h:1 },
  { g:'Sound', kind:'toggle', ref:'micmute', label:'Mic', w:1, h:1 },
  { g:'Media', kind:'nowplaying', ref:'', label:'Now playing', w:4, h:2 },
  { g:'Media', kind:'action', ref:'media.playpause', label:'Play/Pause', w:1, h:1 },
  { g:'Media', kind:'action', ref:'media.prev', label:'Previous', w:1, h:1 },
  { g:'Media', kind:'action', ref:'media.next', label:'Next', w:1, h:1 },
  { g:'Media', kind:'action', ref:'media.back10', label:'Back 10s', w:1, h:1 },
  { g:'Media', kind:'action', ref:'media.fwd10', label:'Forward 10s', w:1, h:1 },
  { g:'Media', kind:'action', ref:'media.back30', label:'Back 30s', w:1, h:1 },
  { g:'Media', kind:'action', ref:'media.fwd30', label:'Forward 30s', w:1, h:1 },
  { g:'Media', kind:'timer', ref:'', label:'Sleep timer', w:2, h:1, multi:true, opts:{ minutes:30, action:'pause' } },
  { g:'Screen', kind:'stream', ref:'0', label:'Desktop', w:4, h:2 },
  { g:'Screen', kind:'action', ref:'screen.off', label:'Screen off', w:1, h:1 },
  { g:'Screen', kind:'action', ref:'screen.shot', label:'Screenshot', w:1, h:1 },
  { g:'Screen', kind:'windows', ref:'', label:'Open windows', w:4, h:2 },
  { g:'Games & apps', kind:'gamestats', ref:'', label:'In-game', w:4, h:2 },
  { g:'Games & apps', kind:'chat', ref:'', label:'AI chat', w:4, h:2 },
  { g:'Games & apps', kind:'action', ref:'discord.mute', label:'Discord mute', w:1, h:1 },
  { g:'Games & apps', kind:'action', ref:'discord.deafen', label:'Discord deafen', w:1, h:1 },
  { g:'Stats', kind:'stat', ref:'cpu', label:'CPU', w:2, h:1 },
  { g:'Stats', kind:'stat', ref:'gpu', label:'GPU', w:2, h:1 },
  { g:'Stats', kind:'stat', ref:'ram', label:'RAM', w:2, h:1 },
  { g:'Stats', kind:'stat', ref:'disk', label:'Disk', w:2, h:1 },
  { g:'Stats', kind:'stat', ref:'temp', label:'Temp', w:2, h:1 },
  { g:'Stats', kind:'stat', ref:'battery', label:'Battery', w:2, h:1 },
  { g:'Stats', kind:'stat', ref:'net', label:'Network', w:2, h:1 },
  { g:'Power', kind:'action', ref:'power.lock', label:'Lock PC', w:2, h:1 },
  { g:'Power', kind:'action', ref:'power.sleep', label:'Sleep', w:2, h:1 },
  { g:'Power', kind:'action', ref:'power.restart', label:'Restart', w:2, h:1 },
  { g:'Power', kind:'action', ref:'power.shutdown', label:'Shut down', w:2, h:1 },
  { g:'Power', kind:'action', ref:'power.signout', label:'Sign out', w:2, h:1 },
];

/* Is this control already somewhere on the remote? Two Volume sliders or two
   Now Playing cards are never what anyone wants, so the catalog says so. */
function onBoard(x){
  if (x.multi) return false;
  return !!L && L.sections.some(s => s.tiles.some(t =>
    t.kind === x.kind && String(t.ref || '') === String(x.ref || '')));
}

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
  if (!isPC()) return drawAddHomelab();
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
      ${CONTROLS.map((x,i) => `${i === 0 || CONTROLS[i - 1].g !== x.g
          ? `<div class="grp">${esc(x.g)}</div>` : ''}<div class="it ${onBoard(x) ? 'added' : ''}" data-ctrl="${i}">
        ${iconFor(['nowplaying', 'stream', 'timer', 'appvol', 'audioout', 'windows', 'gamestats', 'chat'].includes(x.kind) ? x.kind : x.ref)}
        <div class="nm">${esc(x.label)}<div class="sub">${x.w} × ${x.h}</div></div>
        <div class="ch">${onBoard(x) ? 'Added' : '+'}</div></div>`).join('')}</div>`;
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

/* The homelab's Add sheet: the server hands over its own catalog (guests,
   server stats, services, links), so it always matches what's really there. */
function drawAddHomelab(){
  const c = sheetCtx;
  const tabs = (lib && lib.tabs) || [];
  if (!c.tab || !tabs.some(x => x.key === c.tab)) c.tab = tabs.length ? tabs[0].key : '';
  const tab = tabs.find(x => x.key === c.tab) || { items: [] };
  const items = tab.items.filter(i => !c.q || (i.label || '').toLowerCase().includes(c.q));
  let body = `
    <input class="field" id="addSearch" placeholder="Search" value="${esc(c.q || '')}"
           style="margin-top:12px">
    <div style="display:flex;gap:7px;margin:12px 0 4px;overflow-x:auto">
      ${tabs.map(x => `<div class="chip ${c.tab === x.key ? 'on' : ''}" data-tab="${esc(x.key)}">${esc(x.name)}</div>`).join('')}
    </div>
    ${tab.note ? `<div style="font-size:11px;color:var(--muted);line-height:1.5;margin:8px 0">${esc(tab.note)}</div>` : ''}
    <div class="list" style="margin-top:6px">
      ${items.map(i => {
        const key = i.kind + '|' + i.ref;
        const sel = c.sel.has(key);
        return `<div class="it" data-hadd="${esc(key)}">
          ${i.kind === 'stat' ? iconFor(i.ref.split(':')[0]) : hlDot(i.on ? 'on' : 'off')}
          <div class="nm">${esc(i.label)}<div class="sub">${esc(i.sub || '')}</div></div>
          <div class="ch" style="color:${sel ? 'var(--accent2)' : 'var(--muted)'}">${sel ? '✓' : '+'}</div></div>`;
      }).join('') || '<div class="srow"><div class="d">Nothing here yet.</div></div>'}
    </div>`;
  if (c.tab === 'link'){
    body += `<div style="margin:16px 0 14px">
      <div class="t" style="margin-bottom:7px">New link</div>
      <input class="field" id="linkName" placeholder="Name - e.g. Jellyfin" style="width:100%">
      <input class="field" id="linkUrl" placeholder="http://192.168.x.x:8096" inputmode="url"
        autocapitalize="off" spellcheck="false" style="width:100%;margin-top:8px">
      <div class="btn wide" id="linkAdd" style="margin-top:9px">Add link</div>
      <div style="font-size:10.5px;color:var(--muted);margin-top:7px;line-height:1.45">
        Give it a picture on the manage page (open this server's address + /manage on a computer).</div>
    </div>`;
  }
  const n = c.sel.size;
  openSheet('Add a tile', body,
    `<div style="flex-grow:1;font-size:12px;color:var(--muted)">${n ? n + ' selected' : 'Pick something'}</div>
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

  const hadd = e.target.closest('[data-hadd]');
  if (hadd){
    const k = hadd.dataset.hadd;
    if (sheetCtx.sel.has(k)) sheetCtx.sel.delete(k); else sheetCtx.sel.add(k);
    drawAdd();
    return;
  }
  if (e.target.closest('#linkAdd')){
    const name = ($('#linkName').value || '').trim(), url = ($('#linkUrl').value || '').trim();
    try {
      const l = await api('/api/links', { name, url });
      lib = await api('/api/library');
      addTiles([{ id: 't' + Math.random().toString(36).slice(2, 10),
                  kind: 'link', ref: l.id, label: l.name, w: 2, h: 1 }]);
    } catch(err){ toast(err.message); }
    return;
  }

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
    if (onBoard(spec)){ toast(spec.label + ' is already on your remote'); return; }
    const { g, multi, ...tile } = spec;
    if (tile.opts) tile.opts = { ...tile.opts };
    addTiles([{ ...tile, id: 't' + Math.random().toString(36).slice(2,10) }]);
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
    if (!isPC()){
      const all = [].concat(...((lib && lib.tabs) || []).map(x => x.items));
      const picked = all.filter(i => c.sel.has(i.kind + '|' + i.ref));
      addTiles(picked.map(i => ({ id: 't' + Math.random().toString(36).slice(2, 10),
        kind: i.kind, ref: i.ref, label: i.label, w: i.w || 2, h: i.h || 1 })));
      return;
    }
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
  const sf = SFX.st || {};
  const sfState = !sf.enabled ? 'Off - power commands will ask you to set it up'
    : sf.method === 'pin' ? 'PIN - asked when you open the app and for power commands'
    : 'Face ID - asked when you open the app and for power commands';
  const body = `
    <div style="font-size:10px;letter-spacing:1px;color:var(--accent2);
      text-transform:uppercase;margin:16px 0 9px">Security</div>
    <div class="srow" id="sfRow" style="cursor:pointer"><div style="flex-grow:1">
      <div class="t">Face ID &amp; PIN</div>
      <div class="d" id="sfRowState">${esc(sfState)}</div></div>
      <div style="color:var(--muted);font-size:12px">${sf.enabled ? 'Change' : 'Set up'}</div></div>

    ${isPC() ? `<div style="font-size:10px;letter-spacing:1px;color:var(--accent2);
      text-transform:uppercase;margin:20px 0 9px">Notifications</div>
    <div class="srow" id="ntRow" style="cursor:pointer"><div style="flex-grow:1">
      <div class="t">Notifications</div>
      <div class="d">Choose what's worth a buzz, and how loud</div></div>
      <div style="color:var(--muted);font-size:12px">Open</div></div>
    <div style="font-size:10px;letter-spacing:1px;color:var(--accent2);
      text-transform:uppercase;margin:20px 0 9px">Now Playing</div>
    <div class="srow" id="jfRow" style="cursor:pointer"><div style="flex-grow:1">
      <div class="t">Jellyfin &amp; Moonfin</div>
      <div class="d" id="jfRowState">Shows the show or movie you're watching</div></div>
      <div style="color:var(--muted);font-size:12px">Set up</div></div>
    <div style="font-size:10.5px;color:var(--muted);margin-top:6px;line-height:1.45">
      Music, YouTube and anything else Windows knows about shows up on its own,
      and so does the game you're playing.</div>` : ''}

    <div style="font-size:10px;letter-spacing:1px;color:var(--accent2);
      text-transform:uppercase;margin:20px 0 9px">Layout</div>
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
      ${isPC() ? '<div class="btn" id="rescanBtn" style="flex-grow:1">Rescan games</div>' : ''}
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
  if (e.target.closest('#sfRow')){
    sfSetup('settings').catch(() => {});
    return;
  }
  if (e.target.closest('#jfRow')){ openJellyfin(); return; }
  if (e.target.closest('#ntRow')){ openNotifications(); return; }
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

$('#setBtn').addEventListener('click', () => {
  openSettings();
  if (isPC()) api('/api/jellyfin').then(j => {
    const el = $('#jfRowState');
    if (el && j.connected) el.textContent = 'Connected to ' + j.server.replace(/^https?:\/\//, '') + ' as ' + j.user;
  }).catch(() => {});
});

/* Jellyfin / Moonfin for Now Playing. Moonfin plays video itself, so Windows
   never hears about it - but the Jellyfin server does. Quick Connect gives the
   PC its own sign-in: approve a code in Jellyfin, no password or key typed. */
let jfTimer = null;
async function openJellyfin(){
  clearInterval(jfTimer);
  let j;
  try { j = await api('/api/jellyfin'); } catch(err){ toast(err.message); return; }
  const draw = (j) => {
    const here = (j.sessions || []).filter(x => x.here);
    let body;
    if (j.connected){
      body = `<div class="jf-ok"><b>Connected</b><span>${esc(j.server)} · ${esc(j.user)}</span></div>
        <div class="d" style="font-size:12.5px;color:var(--muted);line-height:1.5;margin:12px 2px">
          Play something in Moonfin on this PC and Now Playing shows it, with the poster,
          the episode and a live progress bar. ${j.error ? `<br><span style="color:var(--bad)">${esc(j.error)}</span>` : ''}</div>
        ${(j.sessions || []).length ? `<div class="t" style="margin:14px 2px 6px;font-size:12px;color:var(--muted)">Jellyfin apps it can see</div>
          ${j.sessions.map(x => `<div class="srow"><div style="flex-grow:1"><div class="t">${esc(x.client)}</div>
            <div class="d">${esc(x.device)}${x.playing ? ' · playing' : ''}</div></div>
            <div class="d" style="color:${x.here ? 'var(--good)' : 'var(--muted)'}">${x.here ? 'this PC' : 'elsewhere'}</div></div>`).join('')}` : ''}`;
    } else if (j.pending){
      body = `<div class="jf-code">${esc(j.pending)}</div>
        <div class="d" style="font-size:13px;line-height:1.55;text-align:center;margin:4px 8px 14px">
          In Jellyfin on your phone (the web page or the app), open your profile,
          tap <b>Quick Connect</b> and enter this code.<br>This page updates by itself.</div>`;
    } else {
      body = `<div class="d" style="font-size:13px;line-height:1.55;margin:12px 2px">
          Moonfin plays video on its own, so Windows can't see it - but your Jellyfin
          server can. Connect once and Now Playing shows what you're watching.
          You'll approve it with a code in Jellyfin; no password is typed here.</div>
        <input class="field" id="jfServer" type="url" placeholder="https://your-jellyfin" value="${esc(j.server || j.suggested || '')}"
          style="width:100%;margin-top:4px" autocapitalize="off" autocorrect="off">
        ${j.suggested ? '<div class="d" style="font-size:11px;color:var(--muted);margin:6px 2px">Found in Moonfin on this PC.</div>' : ''}
        ${j.qcState === 'expired' ? '<div class="d" style="color:var(--bad);font-size:12px;margin-top:6px">That code expired - start again.</div>' : ''}`;
    }
    openSheet('Jellyfin & Moonfin', body,
      j.connected ? `<button class="btn" id="jfOff">Disconnect</button><div style="flex-grow:1"></div><button class="btn" id="sheetClose">Done</button>`
      : j.pending ? `<div style="flex-grow:1"></div><button class="btn" id="sheetClose">Cancel</button>`
      : `<div style="flex-grow:1"></div><button class="btn pri" id="jfGo">Connect</button>`);
    const go = $('#jfGo');
    if (go) go.onclick = async () => {
      go.disabled = true;
      try { await api('/api/jellyfin/connect', { server: $('#jfServer').value }); openJellyfin(); }
      catch(err){ go.disabled = false; toast(err.message); }
    };
    const off = $('#jfOff');
    if (off) off.onclick = async () => { await api('/api/jellyfin/disconnect', {}); openJellyfin(); };
    const c = $('#sheetClose');
    if (c) c.onclick = () => { clearInterval(jfTimer); closeSheet(); };
  };
  draw(j);
  if (j.pending){
    jfTimer = setInterval(async () => {
      if (!$('#sheet').classList.contains('show')){ clearInterval(jfTimer); return; }
      try {
        const k = await api('/api/jellyfin');
        if (!k.pending){ clearInterval(jfTimer); draw(k); if (k.connected) toast('Jellyfin connected'); }
      } catch(e){}
    }, 2000);
  }
}

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
    // A real keyboard's Ctrl/Cmd+C and +V copy and paste between the phone
    // and the PC, same as the buttons. (+V is left to the paste event.)
    if ((e.ctrlKey || e.metaKey) && !e.altKey && /^[cv]$/i.test(e.key)){
      if (e.key.toLowerCase() === 'c'){ e.preventDefault(); fsCopy(); }
      return;
    }
    // Any other shortcut: the character never reaches `input`, so it has to
    // be caught here.
    if ((e.ctrlKey || e.altKey || e.metaKey) && e.key.length === 1){
      e.preventDefault();
      fsSend(() => api('/api/press', {name:e.key.toLowerCase(), mods}));
    }
  });

  // Pasting into the box (long-press > Paste, or a keyboard's Ctrl+V) goes
  // to the PC in one piece - newlines and all - rather than typed out.
  box.addEventListener('paste', e => {
    const t = e.clipboardData && e.clipboardData.getData('text/plain');
    if (!t) return;
    e.preventDefault();
    fsPaste(t);
  });

  // Tapping away from the box means the PC is no longer following it, so do
  // not keep diffing against text the PC has moved on from.
  box.addEventListener('blur', () => { box.value = ''; FS.sent = ''; });
  box.addEventListener('focus', () => { box.value = ''; FS.sent = ''; });
})();

/* ---------- copy and paste, the way a phone does it ----------
 * Copy: Ctrl+C on the PC, and whatever it copied lands on THIS phone's
 * clipboard - paste it into Messages or anywhere. Paste: this phone's
 * clipboard goes to the PC and Ctrl+V puts it where the cursor is. Text only,
 * never logged. Browsers only allow this over https and only during the tap
 * itself, which is why the copy starts writing before the PC has answered. */
async function fsCopy(){
  fsSync();
  const req = fsQueue.then(() => api('/api/clipboard/copy', {}));
  req.catch(() => {});
  let wrote = false;
  if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write){
    try {
      // Safari: the write must begin inside the tap; its text can arrive later.
      const blob = req.then(r => r.text ? new Blob([r.text], { type: 'text/plain' })
                                        : Promise.reject(new Error('empty')));
      await navigator.clipboard.write([new ClipboardItem({ 'text/plain': blob })]);
      wrote = true;
    } catch(e){}
  }
  let r;
  try { r = await req; }
  catch(err){ fsToast(err.message); return; }
  if (!r.text){
    fsToast(r.changed ? 'That wasn\'t text' : 'Nothing is selected on the PC');
    return;
  }
  if (!wrote){
    try { await navigator.clipboard.writeText(r.text); wrote = true; } catch(e){}
  }
  if (wrote){
    if (navigator.vibrate) navigator.vibrate(10);
    fsToast('Copied to your phone' + (r.truncated ? ' (first 100,000 characters)' : ''), 1300);
  } else {
    const box = fsq('fsClip'), ta = fsq('fsClipText');
    ta.value = r.text;
    box.hidden = false;
    ta.focus(); ta.select();
  }
}

async function fsPaste(text){
  if (text === undefined){
    try {
      // iOS shows its own little "Paste" button for this - that's normal.
      text = await navigator.clipboard.readText();
    } catch(e){
      fsq('fsKeys').focus();
      fsToast('Long-press the typing box and choose Paste', 2800);
      return;
    }
  }
  if (!text){ fsToast('Your phone\'s clipboard is empty'); return; }
  fsSync();
  try {
    await fsSend(() => api('/api/clipboard/paste', { text }));
    fsToast('Pasted on the PC', 1100);
  } catch(err){ fsToast(err.message); }
  fsClearTyping();
}

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
  fsq('fsClipDone').onclick = () => { fsq('fsClip').hidden = true; };
  // Android's long-press menu ("Download image", "Copy") on the picture: no.
  fsq('fs').addEventListener('contextmenu', e => {
    if (!e.target.closest('input,textarea')) e.preventDefault();
  });

  // The dock's buttons must not steal focus from the input: on a phone,
  // losing focus closes the keyboard, and pressing Ctrl+C should not shut
  // the keyboard you were typing with.
  dock.addEventListener('pointerdown', e => {
    if (e.target.closest('.fsb')) e.preventDefault();
  });

  dock.addEventListener('click', async e => {
    const clip = e.target.closest('[data-clip]');
    if (clip){ if (clip.dataset.clip === 'copy') fsCopy(); else fsPaste(); return; }
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

/* Moving from the old http address to https loses browser storage (it
   belongs to one exact address). The server's hand-off page carries the PC
   list across in the URL fragment - here, or stashed by the login page - as
   "import=<list>&from=<old origin>". Merge it in, and point the entry that
   was THIS PC at its new address, so renames, MACs and Pi addresses survive. */
function pcsImport(){
  let raw = null;
  try {
    if (location.hash.indexOf('#import=') === 0) {
      raw = location.hash.slice(1);
      history.replaceState(null, '', location.pathname + location.search);
    } else {
      raw = localStorage.getItem('aether.pcs.import');
    }
    localStorage.removeItem('aether.pcs.import');
  } catch (e) { return; }
  if (!raw) return;
  try {
    const q = new URLSearchParams(raw);
    const incoming = JSON.parse(q.get('import') || '[]');
    const from = (q.get('from') || '').replace(/\/+$/, '') + '/';
    if (!Array.isArray(incoming)) return;
    const list = pcsLoad();
    for (const p of incoming) {
      if (!p || typeof p.url !== 'string') continue;
      const url = p.url === from ? pcsHere() : p.url;
      const have = list.find(x => x.url === url);
      if (have) {
        for (const k of ['alias', 'pi', 'mac', 'name'])
          if (p[k] && !have[k]) have[k] = p[k];
      } else {
        list.push({ ...p, url });
      }
    }
    pcsSave(list);
  } catch (e) {}
}

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
    // Grab this PC's MAC so Wake-on-LAN can find it later, when it's asleep
    // and we can only ask another device (the Pi) to send the magic packet.
    try {
      const w = await api('/api/wol/info');
      const mac = w && w.primary && w.primary.mac;
      if (mac){
        const l2 = pcsLoad();
        const m2 = l2.find(p => p.url === pcsHere());
        if (m2 && m2.mac !== mac){ m2.mac = mac; pcsSave(l2); }
      }
    } catch(e){}
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
    <div class="srow" style="flex-wrap:wrap">
      ${dot(p.url === here)}
      <div style="flex-grow:1;min-width:0;display:flex;gap:7px">
        <input class="field" id="pcAlias" value="${esc(p.alias || '')}"
          placeholder="${esc(p.name || 'PC')}" autocomplete="off"
          autocapitalize="words" spellcheck="false"
          style="flex-grow:1;min-width:0">
        <button class="btn pri" data-savepc="${i}">Save</button>
        <button class="btn" data-cancelpc="1">Cancel</button>
      </div>
      <div style="flex-basis:100%;margin-top:8px">
        <div class="d" style="margin-bottom:5px">Wake sender — the address of your
          always-on Pi that turns this PC on. Leave blank if you don't use Wake on LAN.</div>
        <input class="field" id="pcPi" value="${esc(p.pi || '')}"
          placeholder="192.168.x.x" autocomplete="off" autocapitalize="off"
          spellcheck="false" inputmode="url" style="width:100%">
        <div class="d" style="margin-top:5px">MAC: ${p.mac
          ? '<code>' + esc(p.mac) + '</code>'
          : 'not captured yet — open this remote once while on this PC'}</div>
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
      ${p.url !== here && p.pi && p.mac ?
        `<div data-wakepc="${i}" style="flex:0 0 auto;padding:6px 9px;
           color:var(--good,#34d399);font-size:12px">Wake</div>` : ''}
      <div data-editpc="${i}" style="flex:0 0 auto;padding:6px 9px;
        color:var(--muted);font-size:12px">Edit</div>
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
      const pi = ($('#pcPi') ? $('#pcPi').value : '').trim().slice(0, 80);
      if (pi) p.pi = pi; else delete p.pi;
      pcsSave(list);
    }
    openPCs();
    if (p && p.url === pcsHere()) pcsMarkTitle();
    return;
  }

  const wake = e.target.closest('[data-wakepc]');
  if (wake){
    const p = pcsLoad()[+wake.dataset.wakepc];
    if (!p || !p.pi || !p.mac) return;
    const host = String(p.pi).trim().replace(/^https?:\/\//i, '').replace(/\/+$/, '');
    wake.textContent = 'Waking\u2026';
    // This PC calls the Pi for us. The page is https now, and a browser won't
    // let an https page call the Pi's plain-http address itself.
    try {
      await api('/api/wol/wake', { mac: p.mac, pi: host });
      toast('Wake sent to ' + pcsDisp(p) + ' \u2014 give it a moment to boot.');
    } catch(err){
      const d = err.data || {};
      toast("Couldn't reach the Pi at " + host + (d.sent_here
        ? ' \u2014 sent the wake signal from this PC instead.' : '.'));
    }
    wake.textContent = 'Wake';
    return;
  }

  const row = e.target.closest('[data-pc]');
  if (row){
    const p = pcsLoad()[+row.dataset.pc];
    if (!p || p.url === pcsHere()) return;
    toast('Switching to ' + pcsDisp(p) + '…');
    // Each address keeps its own storage, so bring the list along in the
    // URL fragment (never sent to any server) - every PC and the homelab
    // then share one switcher.
    location.href = p.url + '#import=' + encodeURIComponent(JSON.stringify(pcsLoad()))
      + '&from=';
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
    // An https page isn't allowed to call a plain-http address, and the
    // homelab add-on doesn't answer cross-origin calls at all - so when the
    // phone can't check it itself, this PC checks it instead. Either way it
    // must answer as an Aether app.
    const mixed = location.protocol === 'https:' && /^http:/i.test(url);
    try {
      let j = null;
      if (!mixed){
        try { j = await (await fetch(url + 'ping', {cache:'no-store', signal:ac.signal})).json(); }
        catch(e){ j = null; }
      }
      if (!j) j = await api('/api/pcs/probe', { url });
      if (!j || (j.app !== 'remote' && j.app !== 'aether-homelab'))
        throw new Error('not an Aether app');
      const name = j.pc || (j.app === 'aether-homelab' ? 'Homelab' : 'PC');
      const list = pcsLoad();
      list.push({name, url});
      pcsSave(list);
      openPCs();
      toast('Added ' + name + ' \u2014 tap Rename to give it your own name');
    } catch(err){
      msg.innerHTML = 'Could not reach it. Check it is on, the address is '
        + 'right (including <b>:8788</b> for the homelab add-on), and your phone '
        + 'is on the same network or tailnet as it.';
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

/* ================= tools (clipboard, files) ================= */
async function openTools(){ return openApps(); }
$('#toolsBtn').addEventListener('click', openTools);

/* Running apps, with an End button each. "End" is taskkill /T /F, so it also
   asks for a confirm tap on anything that looks like real work. The server
   refuses to end the remote itself. */
async function openApps(){
  let data;
  try { data = await api('/api/apps'); }
  catch(e){ toast(e.message); return; }
  const apps = data.apps || [];
  let armedPid = null;

  const paint = () => {
    const rows = apps.map(a => `
      <div class="srow">
        <div style="flex:0 0 auto">🗔</div>
        <div style="flex-grow:1;min-width:0">
          <div class="t" style="overflow:hidden;text-overflow:ellipsis;
            white-space:nowrap">${esc(a.title)}</div>
          <div class="d">${esc(a.process)}</div>
        </div>
        <button class="btn sm ${armedPid === a.pid ? 'danger' : ''}"
          data-end="${a.pid}">${armedPid === a.pid ? 'Sure?' : 'End'}</button>
      </div>`).join('');
    openSheet('Running apps', `
      <div style="margin-top:12px">${rows ||
        '<div class="srow"><div class="d">Nothing with a window open.</div></div>'}</div>`,
      `<div style="flex-grow:1"></div>
       <button class="btn" id="appsRefresh">Refresh</button>`);
    $('#appsRefresh').addEventListener('click', openApps);
    $('#sheetBody').querySelectorAll('[data-end]').forEach(b =>
      b.addEventListener('click', async () => {
        const pid = +b.dataset.end;
        if (armedPid !== pid){ armedPid = pid; paint(); return; }  // confirm
        try {
          await api('/api/endtask', { pid });
          toast('Ended');
          setTimeout(openApps, 500);
        } catch(e){ toast(e.message); }
      }));
  };
  paint();
}

/* A read-only file browser. Tap a folder to go in, tap a file to download it
   to the phone. There is no upload or write path anywhere - the server only
   lists and streams. */
/* ================= Files =================
 * The PC's files as their own tab: tap Files in the bar at the bottom and
 * the browser slides in over the remote. Places and drives first, then
 * folders you can search and sort, previews of pictures, video, music, PDFs
 * and text, pick several and download them as one zip, and upload from the
 * phone into your own folders (Face ID / PIN first). The server decides what
 * is readable and writable (files.py); this is only the part you see. */
const FV = {
  path: '', data: null, list: [], sel: new Set(), selecting: false, q: '',
  stack: [],          // where you came from, for Back - like a phone's Files app
  sort: (() => { try { return JSON.parse(localStorage.getItem('aether.fsort')) || { by: 'name', desc: false }; }
                 catch(e){ return { by: 'name', desc: false }; } })(),
  seq: 0, searchT: null, pressT: null, view: null,
};
const FVI = {
  back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg>',
  chev: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5l7 7-7 7"/></svg>',
  search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>',
  sort: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h11M4 12h7M4 17h4"/><path d="M18 5v14M15 16l3 3 3-3"/></svg>',
  up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>',
  down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v12M7 11l5 5 5-5"/><path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  open: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/></svg>',
  remote: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"><rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2"/><rect x="3.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/></svg>',
  files: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4.5l2 2.2H19a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
};
const FV_PLACE = {
  desktop: ['#0a84ff', '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>'],
  documents: ['#5e5ce6', '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4M10 12h5M10 16h5"/>'],
  downloads: ['#30b0c7', '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>'],
  pictures: ['#34c759', '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.8"/><path d="M21 16l-5-5-9 8"/>'],
  videos: ['#ff375f', '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3"/>'],
  music: ['#ff9f0a', '<path d="M9 18V5l11-2v13"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/>'],
  home: ['#8e8e93', '<path d="M4 11l8-7 8 7v9H4z"/><path d="M10 20v-6h4v6"/>'],
};
const fvEnc = encodeURIComponent;

function fvSize(n){
  if (n == null) return '';
  const u = ['bytes', 'KB', 'MB', 'GB', 'TB'];
  let i = 0; while (n >= 1000 && i < u.length - 1){ n /= 1000; i++; }
  return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + ' ' + u[i];
}
function fvDate(t){
  if (!t) return '';
  const d = new Date(t * 1000), now = new Date();
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return 'Today ' + time;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return 'Yesterday ' + time;
  return d.toLocaleDateString([], d.getFullYear() === now.getFullYear()
    ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', year: 'numeric' });
}
const fvThumbable = e => !e.dir && ['image', 'video', 'pdf', 'audio', 'doc', 'sheet', 'slides'].includes(e.kind);
const fvThumb = (e, s) => '/api/files/thumb?p=' + fvEnc(e.path) + '&s=' + (s || 160) + '&m=' + (e.mtime || 0);

function fvGlyph(e){
  if (e.dir) return `<svg class="fv-folder" viewBox="0 0 40 32"><path d="M2.5 6.5A3.5 3.5 0 0 1 6 3h8.6c.9 0 1.8.4 2.4 1l2.4 2.4H34A3.5 3.5 0 0 1 37.5 10v16A3.5 3.5 0 0 1 34 29.5H6A3.5 3.5 0 0 1 2.5 26z"/><path class="fv-folder-lid" d="M2.5 11h35v15A3.5 3.5 0 0 1 34 29.5H6A3.5 3.5 0 0 1 2.5 26z"/></svg>`;
  const ext = (e.name.includes('.') ? e.name.split('.').pop() : '').slice(0, 4).toUpperCase();
  return `<div class="fv-doc k-${esc(e.kind)}"><span>${esc(ext)}</span></div>`;
}

function fvSorted(list){
  const { by, desc } = FV.sort;
  const f = desc ? -1 : 1;
  const name = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
  return list.slice().sort((a, b) => {
    if (a.dir !== b.dir) return a.dir ? -1 : 1;           // folders first, always
    let r = 0;
    if (by === 'date') r = (a.mtime || 0) - (b.mtime || 0);
    else if (by === 'size') r = (a.size || 0) - (b.size || 0);
    else if (by === 'kind') r = (a.kind || '').localeCompare(b.kind || '');
    return (r || name(a, b)) * f;
  });
}

function fvEl(){ return document.getElementById('files'); }

/* Go somewhere new: remember where you were, so Back returns there (not just
   to the parent - from Downloads, Back means "Files", not your user folder). */
function fvOpenPath(path){
  path = path || '';
  if (path === FV.path && !FV.q) return;
  const entry = { path: FV.path, name: fvTitle(), pushed: false };
  FV.stack.push(entry);
  // A history entry too, so the phone's own back gesture / button works here.
  try { history.pushState({ aetherFiles: FV.stack.length }, ''); entry.pushed = true; } catch(e){}
  fvGo(path, { dir: 'fwd' });
}

/* Back: leave a search first, then retrace your steps, then go up a level. */
function fvBack(fromPop){
  if (FV.q){
    FV.q = '';
    const inp = fvEl().querySelector('.fv-search input');
    if (inp) inp.value = '';
    fvSearch('');
    return;
  }
  if (FV.stack.length){
    if (!fromPop && FV.stack[FV.stack.length - 1].pushed){ history.back(); return; }   // -> popstate
    const prev = FV.stack.pop();
    fvGo(prev.path, { dir: 'back' });
    return;
  }
  if (FV.path) fvGo((FV.data && FV.data.path === FV.path && FV.data.up) || '', { dir: 'back' });
}
window.addEventListener('popstate', () => {
  // The phone's own back gesture / button, while Files is open.
  if (FV.stack.length){
    if (document.body.classList.contains('filesOn')) fvBack(true);
    else FV.stack.pop();
  }
});

async function fvGo(path, opts){
  opts = opts || {};
  const seq = ++FV.seq;
  const prev = FV.path;
  FV.path = path || '';
  FV.q = '';
  if (!opts.keepSel){ FV.sel.clear(); FV.selecting = false; }
  const scroller = fvEl().querySelector('.fv-scroll');
  if (!opts.silent){
    fvEl().classList.add('loading');
  }
  let data;
  try { data = await api('/api/files?p=' + fvEnc(FV.path)); }
  catch(err){ if (seq === FV.seq){ fvEl().classList.remove('loading'); toast(err.message); } return; }
  if (seq !== FV.seq) return;
  FV.data = data;
  FV.list = fvSorted(data.entries || []);
  fvEl().classList.remove('loading');
  fvDraw(opts.silent ? null : (opts.dir || (path !== prev ? 'fwd' : null)));
  if (!opts.silent && scroller) scroller.scrollTop = 0;
}

async function fvSearch(q){
  const was = FV.q;
  FV.q = q;
  const seq = ++FV.seq;
  if (!!was !== !!q) fvDraw();
  if (!q){ FV.list = fvSorted((FV.data && FV.data.entries) || []); fvDrawList(); return; }
  const where = FV.path || ((FV.data && FV.data.places || []).find(p => p.icon === 'home') || {}).path || '';
  if (!where) return;
  fvEl().querySelector('.fv-list').innerHTML = '<div class="fv-note">Searching…</div>';
  try {
    const r = await api('/api/files?p=' + fvEnc(where) + '&q=' + fvEnc(q));
    if (seq !== FV.seq) return;
    FV.list = fvSorted(r.entries || []);
    FV.searchDone = r.done;
    fvDrawList();
  } catch(err){ if (seq === FV.seq) toast(err.message); }
}

function fvCrumbs(p){
  if (!p) return [];
  const parts = p.replace(/\\+$/, '').split('\\');
  const out = [];
  let acc = '';
  parts.forEach((x, i) => {
    acc = i === 0 ? x + '\\' : (acc.endsWith('\\') ? acc : acc + '\\') + x;
    out.push({ name: i === 0 ? x : x, path: acc });
  });
  return out;
}

function fvTitle(){
  const d = FV.data;
  if (!FV.path) return 'Files';
  return (d && d.name) || FV.path;
}

function fvDraw(dir){
  const el = fvEl(), d = FV.data || {};
  const home = !FV.path;
  // Back is labelled with where it goes, like iOS.
  const last = FV.stack[FV.stack.length - 1];
  const upName = home ? '' : last ? (last.path ? last.name : 'Files')
    : (d.up ? (d.up.replace(/\\+$/, '').split('\\').pop() || d.up) : 'Files');
  el.querySelector('.fv-bar').innerHTML = `
    ${home && !FV.q ? '<span class="fv-bar-sp"></span>' : `<button class="fv-back" data-fv="up">${FVI.back}<span>${esc(FV.q ? 'Done' : upName)}</span></button>`}
    <span class="grow"></span>
    ${!home && d.writable ? `<button class="fv-ib" data-fv="upload" aria-label="Upload from this phone">${FVI.up}</button>` : ''}
    ${!home ? `<button class="fv-txtbtn" data-fv="select">${FV.selecting ? 'Cancel' : 'Select'}</button>` : ''}`;
  el.querySelector('.fv-title').textContent = fvTitle();
  const s = el.querySelector('.fv-search input');
  s.value = FV.q;
  s.placeholder = home ? 'Search your folders' : 'Search ' + fvTitle();
  el.querySelector('.fv-crumbs').innerHTML = fvCrumbs(FV.path).map((c, i, a) =>
    `<button class="fv-crumb ${i === a.length - 1 ? 'on' : ''}" data-go="${esc(c.path)}">${esc(c.name.replace(/\\$/, ''))}</button>`)
    .join(`<i>${FVI.chev}</i>`);
  const cr = el.querySelector('.fv-crumbs');
  cr.scrollLeft = cr.scrollWidth;              // show where you are, not where C: is
  el.classList.toggle('home', home);
  fvDrawList();
  if (dir){
    const l = el.querySelector('.fv-body');
    l.classList.remove('in-fwd', 'in-back');
    void l.offsetWidth;
    l.classList.add(dir === 'back' ? 'in-back' : 'in-fwd');
  }
}

function fvRow(e, i){
  const sel = FV.sel.has(e.path);
  const sub = e.dir ? (e.where !== undefined && FV.q ? (e.where || 'here') : 'Folder')
                    : [fvSize(e.size), fvDate(e.mtime)].filter(Boolean).join(' · ')
                      + (FV.q && e.where ? ' · ' + e.where : '');
  return `<div class="fv-row ${sel ? 'sel' : ''}" data-i="${i}">
    <div class="fv-check">${FVI.check}</div>
    <div class="fv-ic">${fvGlyph(e)}${fvThumbable(e)
      ? `<img loading="lazy" decoding="async" src="${fvThumb(e)}" alt="" onload="this.parentNode.classList.add('has-img')" onerror="this.remove()">` : ''}</div>
    <div class="fv-meta"><div class="fv-name">${esc(e.name)}</div><div class="fv-sub">${esc(sub)}</div></div>
    ${e.dir && !FV.selecting ? `<i class="fv-go">${FVI.chev}</i>` : ''}
  </div>`;
}

function fvDrawList(){
  const el = fvEl(), d = FV.data || {};
  const box = el.querySelector('.fv-list');
  el.classList.toggle('selecting', FV.selecting);
  if (!FV.path && !FV.q){
    const places = (d.places || []).map(p => {
      const [c, g] = FV_PLACE[p.icon] || FV_PLACE.home;
      return `<button class="fv-place" data-go="${esc(p.path)}"><span class="fv-pic" style="--c:${c}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${g}</svg></span>
        <span>${esc(p.name)}</span></button>`;
    }).join('');
    const drives = (d.drives || []).map(v => {
      const used = v.total ? (1 - v.free / v.total) : 0;
      return `<button class="fv-drive" data-go="${esc(v.path)}">
        <span class="fv-pic" style="--c:${v.icon === 'usb' ? '#ff9f0a' : v.icon === 'net' ? '#64d2ff' : '#8e8e93'}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><rect x="3" y="7" width="18" height="10" rx="2.5"/><circle cx="16.5" cy="12" r="1" fill="currentColor"/><path d="M6.5 12h5"/></svg></span>
        <span class="fv-meta"><span class="fv-name">${esc(v.name)} (${esc(v.letter)}:)</span>
          <span class="fv-usage"><i style="width:${(used * 100).toFixed(1)}%;${used > .9 ? 'background:var(--bad)' : ''}"></i></span>
          <span class="fv-sub">${fvSize(v.free)} free of ${fvSize(v.total)}</span></span>
        <i class="fv-go">${FVI.chev}</i></button>`;
    }).join('');
    box.innerHTML = `<div class="fv-h">Places</div><div class="fv-places">${places}</div>
      <div class="fv-h">Drives</div><div class="fv-group">${drives || '<div class="fv-note">No drives found.</div>'}</div>`;
    fvSelBar();
    return;
  }
  if (d.error && !FV.q){ box.innerHTML = `<div class="fv-note err">${esc(d.error)}</div>`; fvSelBar(); return; }
  const rows = FV.list.map(fvRow).join('');
  const nf = FV.list.filter(e => !e.dir).length, nd = FV.list.length - nf;
  box.innerHTML = rows
    ? `<div class="fv-group">${rows}</div><div class="fv-foot">${
        FV.q ? `${FV.list.length} found${FV.searchDone === false ? ' (stopped early - try a narrower folder)' : ''}`
             : [nd ? nd + ' folder' + (nd === 1 ? '' : 's') : '', nf ? nf + ' file' + (nf === 1 ? '' : 's') : ''].filter(Boolean).join(', ')
               + (d.capped ? ' (first 5,000)' : '')}</div>`
    : `<div class="fv-note">${FV.q ? 'Nothing matches “' + esc(FV.q) + '”.' : 'This folder is empty.'}</div>`;
  fvSelBar();
}

function fvSelBar(){
  const bar = fvEl().querySelector('.fv-selbar');
  if (!FV.selecting){ bar.classList.remove('show'); return; }
  const picked = FV.list.filter(e => FV.sel.has(e.path));
  const bytes = picked.reduce((n, e) => n + (e.size || 0), 0);
  const folders = picked.some(e => e.dir);
  const all = FV.list.length && picked.length === FV.list.length;
  bar.innerHTML = `<button class="fv-txtbtn" data-fv="all">${all ? 'None' : 'All'}</button>
    <div class="fv-selinfo">${picked.length ? picked.length + ' selected' : 'Tap to select'}${
      picked.length ? `<small>${folders ? 'includes folders' : fvSize(bytes)}</small>` : ''}</div>
    <button class="fv-dl" data-fv="download" ${picked.length ? '' : 'disabled'}>${FVI.down}<span>Download</span></button>`;
  bar.classList.add('show');
}

function fvDownload(entries){
  if (!entries.length) return;
  const go = (url, name) => {
    const a = document.createElement('a');
    a.href = url; a.download = name || '';
    document.body.appendChild(a); a.click(); a.remove();
  };
  if (entries.length === 1 && !entries[0].dir){
    go('/api/download?p=' + fvEnc(entries[0].path), entries[0].name);
    toast('Downloading ' + entries[0].name);
    return;
  }
  api('/api/files/zip', { paths: entries.map(e => e.path) }).then(r => {
    go(r.url, r.name);
    toast('Zipping ' + entries.length + ' item' + (entries.length === 1 ? '' : 's') + ' - ' + r.name);
  }).catch(err => toast(err.message));
}

/* ---- the viewer ---- */
function fvViewEl(){
  let v = document.getElementById('fvView');
  if (v) return v;
  v = document.createElement('div');
  v.id = 'fvView';
  v.innerHTML = `<div class="fvv-top"><button class="fvv-ib" data-fvv="close" aria-label="Close">${FVI.x}</button>
      <div class="fvv-name"><b></b><small></small></div>
      <button class="fvv-ib" data-fvv="dl" aria-label="Download">${FVI.down}</button></div>
    <div class="fvv-stage"></div>`;
  document.body.appendChild(v);
  v.addEventListener('click', e => {
    const b = e.target.closest('[data-fvv]');
    if (!b) return;
    const a = b.dataset.fvv, cur = FV.view && FV.view.e;
    if (a === 'close') fvCloseView();
    if (a === 'dl' && cur) fvDownload([cur]);
    if (a === 'open' && cur) window.open('/api/files/raw?p=' + fvEnc(cur.path), '_blank');
  });
  // Swipe: sideways between pictures, down to close.
  let st = null;
  const stage = v.querySelector('.fvv-stage');
  stage.addEventListener('pointerdown', e => {
    if (e.target.closest('video,audio,pre,button')) return;
    st = { x: e.clientX, y: e.clientY, t: Date.now(), id: e.pointerId };
  });
  stage.addEventListener('pointermove', e => {
    if (!st || e.pointerId !== st.id) return;
    const dx = e.clientX - st.x, dy = e.clientY - st.y;
    const img = stage.querySelector('img.fvv-img');
    if (img && !img.classList.contains('zoom')) img.style.transform =
      Math.abs(dy) > Math.abs(dx) && dy > 0 ? `translateY(${dy}px) scale(${1 - Math.min(dy, 400) / 1600})` : `translateX(${dx}px)`;
  });
  const end = e => {
    if (!st || e.pointerId !== st.id) return;
    const dx = e.clientX - st.x, dy = e.clientY - st.y, quick = Date.now() - st.t < 280;
    st = null;
    const img = stage.querySelector('img.fvv-img');
    if (img && !img.classList.contains('zoom')) img.style.transform = '';
    if (Math.abs(dx) < 8 && Math.abs(dy) < 8){
      if (img && e.type === 'pointerup'){
        const now = Date.now();
        if (now - (FV.lastTap || 0) < 300){ img.classList.toggle('zoom'); FV.lastTap = 0; }
        else FV.lastTap = now;
      }
      return;
    }
    if (img && img.classList.contains('zoom')) return;
    if (dy > 110 && Math.abs(dy) > Math.abs(dx)) return fvCloseView();
    if (Math.abs(dx) > 70 || (quick && Math.abs(dx) > 30)) fvStep(dx < 0 ? 1 : -1);
  };
  stage.addEventListener('pointerup', end);
  stage.addEventListener('pointercancel', end);
  return v;
}

function fvStep(d){
  if (!FV.view) return;
  const pics = FV.list.filter(e => e.kind === 'image');
  const i = pics.findIndex(e => e.path === FV.view.e.path);
  if (i < 0) return;
  const n = pics[i + d];
  if (n) fvOpen(n, d > 0 ? 'l' : 'r');
}

async function fvOpen(e, from){
  const v = fvViewEl(), stage = v.querySelector('.fvv-stage');
  FV.view = { e };
  v.querySelector('.fvv-name b').textContent = e.name;
  const pics = FV.list.filter(x => x.kind === 'image');
  const pi = pics.findIndex(x => x.path === e.path);
  v.querySelector('.fvv-name small').textContent = e.kind === 'image' && pics.length > 1
    ? `${pi + 1} of ${pics.length}` : [fvSize(e.size), fvDate(e.mtime)].filter(Boolean).join(' · ');
  const raw = '/api/files/raw?p=' + fvEnc(e.path);
  const ext = (e.name.split('.').pop() || '').toLowerCase();
  const playable = ['mp4', 'm4v', 'mov', 'webm', 'mp3', 'm4a', 'aac', 'wav', 'flac', 'ogg', 'opus'].includes(ext);
  const info = (msg) => `<div class="fvv-info">${fvThumbable(e)
      ? `<img class="fvv-poster" src="${fvThumb(e, 480)}" alt="" onerror="this.remove()">` : `<div class="fvv-big">${fvGlyph(e)}</div>`}
      <b>${esc(e.name)}</b><span>${esc([fvSize(e.size), fvDate(e.mtime)].filter(Boolean).join(' · '))}</span>
      ${msg ? `<small>${msg}</small>` : ''}
      <div class="fvv-acts">${e.kind === 'pdf' ? `<button class="fvv-btn" data-fvv="open">${FVI.open}Open</button>` : ''}
        <button class="fvv-btn pri" data-fvv="dl">${FVI.down}Download</button></div></div>`;
  if (e.kind === 'image'){
    stage.innerHTML = `<img class="fvv-img ${from ? 'from-' + from : ''}" src="/api/files/view?p=${fvEnc(e.path)}&m=${e.mtime || 0}" alt="">`;
    // Warm the neighbours so a swipe is instant.
    [pics[pi - 1], pics[pi + 1]].forEach(n => { if (n) (new Image()).src = '/api/files/view?p=' + fvEnc(n.path) + '&m=' + (n.mtime || 0); });
  } else if (e.kind === 'video' && playable){
    stage.innerHTML = `<video class="fvv-media" src="${raw}" controls playsinline autoplay preload="metadata"></video>`;
  } else if (e.kind === 'audio' && playable){
    stage.innerHTML = info('') ;
    stage.querySelector('.fvv-acts').insertAdjacentHTML('beforebegin', `<audio class="fvv-audio" src="${raw}" controls autoplay></audio>`);
  } else if (e.kind === 'text'){
    stage.innerHTML = '<div class="fv-note">Loading…</div>';
    try {
      const t = await api('/api/files/text?p=' + fvEnc(e.path));
      if (FV.view.e !== e) return;
      if (t.binary){ stage.innerHTML = info("This doesn't look like text."); }
      else {
        stage.innerHTML = `<pre class="fvv-text"></pre>${t.truncated ? '<div class="fv-note">Showing the first 256 KB - download it for the rest.</div>' : ''}`;
        stage.querySelector('pre').textContent = t.text;     // text, never HTML
      }
    } catch(err){ stage.innerHTML = info(esc(err.message)); }
  } else {
    stage.innerHTML = info(e.kind === 'video' || e.kind === 'audio'
      ? "Phones can't play this format - download it and open it in an app like VLC." : '');
  }
  v.classList.add('show');
}

function fvCloseView(){
  const v = document.getElementById('fvView');
  if (!v) return;
  v.classList.remove('show');
  FV.view = null;
  setTimeout(() => { if (!FV.view) v.querySelector('.fvv-stage').innerHTML = ''; }, 300);   // stops video
}

/* ---- uploads ---- */
function fvPickUpload(){
  let inp = document.getElementById('fvPick');
  if (!inp){
    inp = document.createElement('input');
    inp.type = 'file'; inp.multiple = true; inp.id = 'fvPick'; inp.hidden = true;
    document.body.appendChild(inp);
    inp.addEventListener('change', () => { const fl = [...inp.files]; inp.value = ''; if (fl.length) fvUpload(fl); });
  }
  inp.click();
}

async function fvUpload(list){
  const dir = FV.path, name = fvTitle();
  const total = list.reduce((n, f) => n + f.size, 0);
  let ticket;
  try { ticket = (await api('/api/files/upload/begin', { dir })).ticket; }
  catch(err){ if (err.message !== 'Cancelled') toast(err.message); return; }
  let cancelled = false, xhr = null, done = 0;
  const up = [];
  openSheet('Uploading to ' + name, `<div class="fvu">
      <div class="fvu-line"><span id="fvuNow"></span><span id="fvuPct">0%</span></div>
      <div class="fvu-bar"><i id="fvuBar"></i></div>
      <div class="fvu-sub" id="fvuSub">${list.length} file${list.length === 1 ? '' : 's'} · ${fvSize(total)}</div></div>`,
    `<div style="flex-grow:1"></div><button class="btn" id="fvuStop">Stop</button>`);
  $('#fvuStop').onclick = () => { cancelled = true; if (xhr) xhr.abort(); closeSheet(); };
  for (const [i, f] of list.entries()){
    if (cancelled) break;
    const now = $('#fvuNow');
    if (now) now.textContent = (list.length > 1 ? `${i + 1} of ${list.length}: ` : '') + f.name;
    const ok = await new Promise(res => {
      xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/files/upload?t=' + fvEnc(ticket) + '&name=' + fvEnc(f.name));
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.onprogress = ev => {
        const pct = Math.round((done + ev.loaded) / Math.max(1, total) * 100);
        const b = $('#fvuBar'), p = $('#fvuPct');
        if (b) b.style.width = pct + '%';
        if (p) p.textContent = pct + '%';
      };
      xhr.onload = () => {
        let j = {}; try { j = JSON.parse(xhr.responseText); } catch(e){}
        if (xhr.status === 200){ up.push(j.name); res(true); }
        else { toast(f.name + ': ' + (j.error || 'failed')); res(false); }
      };
      xhr.onerror = xhr.onabort = () => res(false);
      xhr.send(f);
    });
    done += f.size;
    if (!ok && !cancelled && list.length > 1) continue;
  }
  if (!cancelled) closeSheet();
  if (up.length){
    if (navigator.vibrate) navigator.vibrate(12);
    toast(`Uploaded ${up.length} file${up.length === 1 ? '' : 's'} to ${name}`);
    if (FV.path === dir){
      await fvGo(dir, { silent: true });
      up.forEach(n => {
        const i = FV.list.findIndex(e => e.name === n);
        const r = i >= 0 && fvEl().querySelector(`.fv-row[data-i="${i}"]`);
        if (r) r.classList.add('fresh');
      });
    }
  }
}

function fvSortSheet(){
  const opts = [['name', 'Name'], ['date', 'Date modified'], ['size', 'Size'], ['kind', 'Kind']];
  openSheet('Sort by', `<div style="margin-top:8px">${opts.map(([k, l]) => `
      <div class="srow" data-sort="${k}" style="cursor:pointer"><div class="t" style="flex-grow:1">${l}</div>
        ${FV.sort.by === k ? `<div style="color:var(--accent2);font-size:13px">${FV.sort.desc
          ? (k === 'name' || k === 'kind' ? 'Z–A' : k === 'date' ? 'Newest' : 'Largest')
          : (k === 'name' || k === 'kind' ? 'A–Z' : k === 'date' ? 'Oldest' : 'Smallest')} ✓</div>` : ''}</div>`).join('')}
    <div class="d" style="font-size:12px;color:var(--muted);margin:10px 2px">Tap the same one again to flip the order. Folders always come first.</div></div>`,
    `<div style="flex-grow:1"></div><button class="btn" id="sheetClose">Done</button>`);
  $('#sheetBody').querySelectorAll('[data-sort]').forEach(r => r.addEventListener('click', () => {
    const k = r.dataset.sort;
    FV.sort = FV.sort.by === k ? { by: k, desc: !FV.sort.desc } : { by: k, desc: k === 'date' || k === 'size' };
    try { localStorage.setItem('aether.fsort', JSON.stringify(FV.sort)); } catch(e){}
    FV.list = fvSorted(FV.list);
    fvDrawList();
    fvSortSheet();
  }));
  const c = $('#sheetClose'); if (c) c.onclick = closeSheet;
}

/* ---- wiring ---- */
function fvBuild(){
  const el = document.createElement('div');
  el.id = 'files';
  el.innerHTML = `<div class="fv-scroll">
      <div class="fv-head">
        <div class="fv-bar"></div>
        <h1 class="fv-title">Files</h1>
        <div class="fv-searchrow">
          <label class="fv-search">${FVI.search}<input type="search" enterkeyhint="search" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
          <button class="fv-ib" data-fv="sort" aria-label="Sort">${FVI.sort}</button>
        </div>
        <div class="fv-crumbs"></div>
      </div>
      <div class="fv-body"><div class="fv-list"></div></div>
    </div>
    <div class="fv-selbar"></div>`;
  document.body.appendChild(el);

  const inp = el.querySelector('.fv-search input');
  inp.addEventListener('input', () => {
    clearTimeout(FV.searchT);
    const q = inp.value.trim();
    FV.searchT = setTimeout(() => fvSearch(q), q ? 350 : 0);
  });
  inp.addEventListener('keydown', e => { if (e.key === 'Enter'){ clearTimeout(FV.searchT); fvSearch(inp.value.trim()); inp.blur(); } });

  el.addEventListener('click', e => {
    const b = e.target.closest('[data-fv]');
    if (b){
      const a = b.dataset.fv;
      if (a === 'up') fvBack();
      if (a === 'select'){ FV.selecting = !FV.selecting; FV.sel.clear(); fvDraw(); }
      if (a === 'all'){
        const all = FV.sel.size === FV.list.length;
        FV.sel = new Set(all ? [] : FV.list.map(x => x.path)); fvDrawList();
      }
      if (a === 'download') fvDownload(FV.list.filter(x => FV.sel.has(x.path)));
      if (a === 'upload') fvPickUpload();
      if (a === 'sort') fvSortSheet();
      return;
    }
    const go = e.target.closest('[data-go]');
    if (go){ fvOpenPath(go.dataset.go); return; }
    const row = e.target.closest('.fv-row');
    if (!row || Date.now() < (FV.pressedAt || 0)) return;
    const en = FV.list[+row.dataset.i];
    if (!en) return;
    if (FV.selecting){
      FV.sel.has(en.path) ? FV.sel.delete(en.path) : FV.sel.add(en.path);
      row.classList.toggle('sel', FV.sel.has(en.path));
      fvSelBar();
      return;
    }
    if (en.dir) fvOpenPath(en.path); else fvOpen(en);
  });

  // Hold a row to start selecting, like Photos / Files on the phone.
  el.addEventListener('pointerdown', e => {
    const row = e.target.closest('.fv-row');
    if (!row || FV.selecting) return;
    const x = e.clientX, y = e.clientY;
    clearTimeout(FV.pressT);
    FV.pressT = setTimeout(() => {
      const en = FV.list[+row.dataset.i];
      if (!en) return;
      FV.selecting = true; FV.sel = new Set([en.path]);
      FV.pressedAt = Date.now() + 500;          // swallow the click that follows
      if (navigator.vibrate) navigator.vibrate(12);
      fvDraw();
    }, 520);
    FV.press = { x, y };
  });
  el.addEventListener('pointermove', e => {
    if (FV.press && Math.hypot(e.clientX - FV.press.x, e.clientY - FV.press.y) > 10){
      clearTimeout(FV.pressT); FV.press = null;
    }
  });
  ['pointerup', 'pointercancel'].forEach(t => el.addEventListener(t, () => { clearTimeout(FV.pressT); FV.press = null; }));
  el.addEventListener('contextmenu', e => { if (e.target.closest('.fv-row')) e.preventDefault(); });

  // Edge-swipe right = back up a folder, the iOS gesture.
  let sw = null;
  el.addEventListener('touchstart', e => {
    const t = e.touches[0];
    sw = t.clientX < 28 && (FV.path || FV.q) ? { x: t.clientX, y: t.clientY } : null;
  }, { passive: true });
  el.addEventListener('touchend', e => {
    if (!sw) return;
    const t = e.changedTouches[0];
    if (t.clientX - sw.x > 80 && Math.abs(t.clientY - sw.y) < 60) fvBack();
    sw = null;
  }, { passive: true });
  return el;
}

/* The tab bar: Remote | Files. PCs only - the homelab has no files to show. */
function showTab(which){
  const files = which === 'files', chatOn = which === 'chat';
  if (files && !fvEl()) fvBuild();
  if (files && !FV.data) fvGo('');
  if (chatOn && !chEl()){ chBuild(); chDraw(); }
  document.body.classList.toggle('filesOn', files);
  document.body.classList.toggle('chatOn', chatOn);
  const f = fvEl();
  if (f) f.classList.toggle('show', files);
  const c = chEl();
  if (c){
    c.classList.toggle('show', chatOn);
    if (chatOn){ chDraw(); if (window.visualViewport){ c.style.height = visualViewport.height + 'px'; } }
    else { chPopClose(); chDrawerClose(); }
  }
  document.querySelectorAll('#tabs [data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === which));
  if (!files) fvCloseView();
}

function setupTabs(){
  if (!isPC() || document.getElementById('tabs')) return;
  const t = document.createElement('nav');
  t.id = 'tabs';
  t.innerHTML = `<button data-tab="remote" class="on">${FVI.remote}<span>Remote</span></button>
    <button data-tab="files">${FVI.files}<span>Files</span></button>`;
  document.body.appendChild(t);
  document.body.classList.add('hastabs');
  t.addEventListener('click', e => {
    const b = e.target.closest('[data-tab]');
    if (!b) return;
    if (b.dataset.tab === 'files' && document.body.classList.contains('filesOn') && (FV.path || FV.q)){
      FV.stack = [];                                // tap Files again = back to the top
      FV.q = '';
      fvGo('', { dir: 'back' });
      return;
    }
    showTab(b.dataset.tab);
  });
}

/* ================= 1.5: new tiles, tile settings, notifications, chat ================= */

/* ---- small shared bits ---- */
const XI = {
  moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>',
  mixer: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6 4v16M12 4v16M18 4v16"/><circle cx="6" cy="14" r="2.2" fill="var(--panel)"/><circle cx="12" cy="8" r="2.2" fill="var(--panel)"/><circle cx="18" cy="16" r="2.2" fill="var(--panel)"/></svg>',
  speaker: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="3" width="14" height="18" rx="2.5"/><circle cx="12" cy="14" r="3.5"/><path d="M12 7h.01"/></svg>',
  headset: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 15v-3a8 8 0 0 1 16 0v3"/><rect x="3" y="14" width="4.5" height="6.5" rx="1.6"/><rect x="16.5" y="14" width="4.5" height="6.5" rx="1.6"/></svg>',
  windows: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><rect x="3" y="5" width="13" height="10" rx="2"/><rect x="8" y="9" width="13" height="10" rx="2" fill="var(--panel)"/></svg>',
  pad: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8h12a4 4 0 0 1 3.9 4.9l-.9 4a2.5 2.5 0 0 1-4.3 1.1L14.5 16h-5l-2.2 2a2.5 2.5 0 0 1-4.3-1.1l-.9-4A4 4 0 0 1 6 8z"/><path d="M8 11v3M6.5 12.5h3M15.5 12h.01M17.5 13.5h.01"/></svg>',
  spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/></svg>',
  send: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 4a1 1 0 0 1 .7.3l6 6a1 1 0 0 1-1.4 1.4L13 7.4V19a1 1 0 1 1-2 0V7.4l-4.3 4.3a1 1 0 0 1-1.4-1.4l6-6A1 1 0 0 1 12 4z"/></svg>',
  stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>',
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  list: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/></svg>',
  back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg>',
  graph: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="2.6"/><circle cx="5" cy="6" r="1.8"/><circle cx="19" cy="6" r="1.8"/><circle cx="5.5" cy="18.5" r="1.8"/><circle cx="18.5" cy="18" r="1.8"/><path d="M6.5 7.2l3.6 3M17.5 7.2l-3.6 3M7 17.3l3.2-3.2M17 16.8l-3.1-2.9"/></svg>',
  down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2.2 2.2 0 0 0 4 0"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13"/></svg>',
};
const mmss = s => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${x}` : `${m}:${x}`; };
const shortDev = n => String(n || '').replace(/^(Speakers|Headphones|Headset|Speaker)\s*\((.*)\)$/i, '$2') || 'Output';
const devIcon = n => /head/i.test(n || '') ? XI.headset : XI.speaker;
const TIMER_ACTS = [['pause', 'Pause media'], ['mute', 'Mute'], ['screenoff', 'Screen off'], ['lock', 'Lock'], ['sleep', 'Sleep'], ['shutdown', 'Shut down']];

/* ---- sleep timer ---- */
function timerInner(t){
  const T = S && S.timer, o = t.opts || {};
  const min = o.minutes || 30, act = (TIMER_ACTS.find(a => a[0] === (o.action || 'pause')) || TIMER_ACTS[0])[1];
  if (T && T.on){
    const what = (TIMER_ACTS.find(a => a[0] === T.action) || [0, ''])[1];
    return `<div class="xt xt-timer on"><div class="xt-ic">${XI.moon}</div>
      <div class="xt-main"><div class="xt-big tm-left" data-at="${T.at}">${mmss(T.left)}</div>
      <div class="xt-sub">then ${esc(what.toLowerCase())} · tap to cancel</div></div></div>`;
  }
  if (t.w === 1) return `<div class="ico">${XI.moon.replace('<svg', '<svg width="20" height="20"')}<div class="lab">${min}m</div></div>`;
  return `<div class="xt xt-timer"><div class="xt-ic">${XI.moon}</div>
    <div class="xt-main"><div class="xt-nm">${esc(t.label || 'Sleep timer')}</div>
    <div class="xt-sub">${min} min · ${esc(act)}</div></div></div>`;
}
setInterval(() => {
  document.querySelectorAll('.tm-left').forEach(e => {
    const left = (+e.dataset.at) - Date.now() / 1000 + (S && S._skew || 0);
    const v = mmss(left);
    if (e.textContent !== v) e.textContent = v;
  });
}, 1000);

async function timerTap(t){
  const T = S && S.timer;
  if (T && T.on){
    S.timer = await api('/api/timer/cancel', {});
    toast('Sleep timer cancelled');
  } else {
    const o = t.opts || {};
    S.timer = await api('/api/timer', { minutes: o.minutes || 30, action: o.action || 'pause' });
    const what = (TIMER_ACTS.find(a => a[0] === S.timer.action) || [0, ''])[1];
    toast(`${what} in ${o.minutes || 30} min`);
  }
  refresh(true);
}

/* ---- volume mixer / one app's volume ---- */
const MX = { apps: [], at: 0, drag: null, pending: {}, sending: false };
const mxIcon = a => a && a.icon ? `<img src="/api/appicon?k=${a.icon}" alt="" onerror="this.style.visibility='hidden'">` : '';
function mxRow(a, label){
  const v = a ? (a.muted ? 0 : a.volume) : 0;
  return `<div class="mx-row ${a ? '' : 'idle'}" data-mxapp="${esc(a ? a.app : '')}">
    <div class="mx-ic">${a ? mxIcon(a) : XI.mixer}</div>
    <div class="mx-mid"><div class="mx-nm">${esc(label || (a && a.name) || 'App')}</div>
      ${a ? `<input type="range" class="mxr" min="0" max="100" value="${v}" style="--pct:${v}%" data-mx="${esc(a.app)}" aria-label="${esc(a.name)} volume">`
          : '<div class="mx-off">Not playing sound</div>'}</div>
    ${a ? `<div class="mx-v">${v}</div>` : ''}</div>`;
}
function appvolInner(t){
  if (t.ref){
    const a = MX.apps.find(x => x.app === t.ref);
    return `<div class="mx one">${mxRow(a, t.label)}</div>`;
  }
  const rows = MX.apps.length ? MX.apps.map(a => mxRow(a)).join('')
    : `<div class="mx-empty">${XI.mixer}<span>Nothing is playing sound</span></div>`;
  return `<div class="mx all"><div class="mx-h">${esc(t.label || 'Volume mixer')}</div><div class="mx-list">${rows}</div></div>`;
}
async function mxPoll(){
  if (!L || !L.sections.some(s => s.tiles.some(t => t.kind === 'appvol'))) return;
  if (MX.drag) return;
  try { MX.apps = (await api('/api/mixer')).apps; MX.at = Date.now(); } catch(e){}
}
function mxFlush(){
  if (MX.sending) return;
  const app = Object.keys(MX.pending)[0];
  if (!app) return;
  const v = MX.pending[app]; delete MX.pending[app];
  MX.sending = true;
  api('/api/mixer', { app, volume: v }).catch(err => toast(err.message))
    .finally(() => { MX.sending = false; setTimeout(mxFlush, 50); });
}
board.addEventListener('input', e => {
  const r = e.target.closest('.mxr');
  if (!r) return;
  const v = +r.value;
  r.style.setProperty('--pct', v + '%');
  const row = r.closest('.mx-row'), lab = row && row.querySelector('.mx-v');
  if (lab) lab.textContent = v;
  const a = MX.apps.find(x => x.app === r.dataset.mx);
  if (a){ a.volume = v; a.muted = false; }
  MX.pending[r.dataset.mx] = v;
  mxFlush();
});
['pointerdown', 'touchstart'].forEach(ev => board.addEventListener(ev, e => {
  if (e.target.closest('.mxr')) MX.drag = Date.now();
}, { passive: true }));
['pointerup', 'touchend', 'touchcancel'].forEach(ev => window.addEventListener(ev, () => {
  if (MX.drag) setTimeout(() => { MX.drag = null; }, 800);
}, { passive: true }));

/* ---- audio output ---- */
function audiooutInner(t){
  const name = S && S.device || 'Output';
  if (t.w === 1) return `<div class="ico">${devIcon(name).replace('<svg', '<svg width="20" height="20"')}<div class="lab">${esc(shortDev(name))}</div></div>`;
  return `<div class="xt"><div class="xt-ic">${devIcon(name)}</div>
    <div class="xt-main"><div class="xt-nm">${esc(shortDev(name))}</div><div class="xt-sub">Tap to switch output</div></div></div>`;
}
async function audiooutTap(t){
  const all = (S && S.devices) || [];
  const pick = ((t.opts && t.opts.devices) || []).filter(id => all.some(d => d.id === id));
  const ring = pick.length ? pick : all.map(d => d.id);
  if (ring.length < 2){ toast('Only one output to choose from'); return; }
  const cur = all.find(d => d.default);
  const i = ring.indexOf(cur && cur.id);
  const next = ring[(i + 1) % ring.length];
  const r = await api('/api/audio/default', { id: next });
  S.devices = r.devices;
  const d = r.devices.find(x => x.id === next);
  S.device = d ? d.name : S.device;
  toast('Output: ' + shortDev(S.device));
  refresh(true);
}

/* ---- open windows ---- */
const WN = { list: [], at: 0 };
function windowsInner(t){
  const n = WN.list.length;
  if (t.h >= 2 && t.w >= 2 && n){
    const max = t.w * t.h * 2;
    return `<div class="wn"><div class="mx-h">${esc(t.label || 'Open windows')}</div><div class="wn-grid">${
      WN.list.slice(0, max).map(w => `<button class="wn-it" data-hwnd="${w.hwnd}" title="${esc(w.title)}">
        ${w.icon ? `<img src="/api/appicon?k=${w.icon}" alt="" onerror="this.outerHTML=XI.windows">` : XI.windows}
        <span>${esc(w.name)}</span></button>`).join('')}</div></div>`;
  }
  if (t.w === 1) return `<div class="ico">${XI.windows.replace('<svg', '<svg width="20" height="20"')}<div class="lab">${n || ''} open</div></div>`;
  return `<div class="xt"><div class="xt-ic">${XI.windows}</div><div class="xt-main">
    <div class="xt-nm">${esc(t.label || 'Open windows')}</div><div class="xt-sub">${n ? n + ' open · tap to switch' : 'Tap to switch'}</div></div></div>`;
}
async function wnPoll(force){
  if (!force && (!L || !L.sections.some(s => s.tiles.some(t => t.kind === 'windows')))) return;
  if (!force && Date.now() - WN.at < 6000) return;
  try { WN.list = (await api('/api/windows')).windows; WN.at = Date.now(); } catch(e){}
}
async function openWindows(){
  await wnPoll(true);
  const rows = WN.list.map(w => `<div class="srow wn-row" data-hwnd="${w.hwnd}" style="cursor:pointer">
      <div class="wn-ri">${w.icon ? `<img src="/api/appicon?k=${w.icon}" alt="">` : XI.windows}</div>
      <div style="flex-grow:1;min-width:0"><div class="t" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(w.title)}</div>
      <div class="d">${esc(w.name)}</div></div></div>`).join('');
  openSheet('Switch to', `<div style="margin-top:10px">${rows || '<div class="srow"><div class="d">Nothing open.</div></div>'}</div>`,
    `<div style="flex-grow:1"></div><button class="btn" id="sheetClose">Done</button>`);
  $('#sheetClose').onclick = closeSheet;
}
async function focusWindow(hwnd){
  const w = WN.list.find(x => String(x.hwnd) === String(hwnd));
  try {
    await api('/api/window/focus', { hwnd: +hwnd });
    if (navigator.vibrate) navigator.vibrate(8);
    toast('Switched to ' + (w ? w.name : 'that window'));
  } catch(err){ toast(err.message); wnPoll(true).then(() => refresh(true)); }
}
document.addEventListener('click', e => {
  const w = e.target.closest('[data-hwnd]');
  if (!w || editing) return;
  e.stopPropagation();
  focusWindow(w.dataset.hwnd);
  if (w.classList.contains('wn-row')) closeSheet();
}, true);

/* ---- in-game card ---- */
function gamestatsInner(t){
  const g = S && S.game, st = (S && S.stats) || {};
  const chip = (k, lab) => { const v = st[k]; return v && !v.na ? `<div class="gs-c"><b>${esc(String(v.big))}<small>${esc(String(v.unit).replace(/^\/.*/, '').split(' ')[0])}</small></b><span>${lab}</span></div>` : ''; };
  const chips = chip('gpu', 'GPU') + chip('temp', 'GPU temp') + chip('cpu', 'CPU') + chip('ram', 'RAM');
  const art = g && g.art ? `<div class="np-bg" style="--np:${npRGB(g)}"><img src="/api/np/art?v=${encodeURIComponent(g.art)}" alt=""></div>` : '';
  if (!g) return `<div class="gs idle"><div class="gs-top"><div class="xt-ic">${XI.pad}</div><div><div class="xt-nm">No game running</div>
    <div class="xt-sub">Live stats while you play</div></div></div>${t.h >= 2 ? `<div class="gs-chips">${chips}</div>` : ''}</div>`;
  return `<div class="gs" style="--np:${npRGB(g)}">${art}<div class="gs-top">
      ${g.art ? `<img class="gs-art" src="/api/np/art?v=${encodeURIComponent(g.art)}" alt="">` : `<div class="xt-ic">${XI.pad}</div>`}
      <div style="min-width:0"><div class="xt-nm">${esc(g.title)}</div>
      <div class="xt-sub"><i class="np-livedot"></i>${esc(g.appName)} · <span class="gs-t" data-from="${g.at - g.pos}">${mmss(g.pos)}</span></div></div></div>
    ${t.h >= 2 ? `<div class="gs-chips">${chips}</div>` : ''}</div>`;
}
setInterval(() => {
  document.querySelectorAll('.gs-t').forEach(e => { const v = mmss(Date.now() / 1000 - +e.dataset.from); if (e.textContent !== v) e.textContent = v; });
}, 1000);

/* ---- chat tile ---- */
function chatInner(t){
  const c = CH.info;
  if (!c || !c.ready) return `<div class="xt xt-chat"><div class="xt-ic">${XI.spark}</div><div class="xt-main">
    <div class="xt-nm">AI chat</div><div class="xt-sub">Set it up in the PC app → Settings → Chatbox</div></div></div>`;
  if (t.w === 1) return `<div class="ico">${XI.spark.replace('<svg', '<svg width="20" height="20"')}<div class="lab">Chat</div></div>`;
  return `<div class="ct"><div class="ct-h">${XI.spark}<span>${esc(t.label || 'Ask AI')}</span><small>${esc(c.model)}</small></div>
    ${t.h >= 2 && CH.last ? `<div class="ct-last">${esc(CH.last)}</div>` : ''}
    <div class="ct-in">Message…</div></div>`;
}

/* ---- screenshot ---- */
function takeScreenshot(){
  const a = document.createElement('a');
  a.href = '/api/screenshot?t=' + Date.now();
  a.download = '';
  document.body.appendChild(a); a.click(); a.remove();
  toast('Screenshot saved');
}

/* ================= tile settings: hold a tile in edit mode =================
 * Everything about one tile on one sheet - its name, size, colour and the
 * things only that kind of tile has. Swipe down (or Done) saves. */
let TS = null;
let sheetOnClose = null;
const ACCENTS = ['', '#7c3aed', '#2563eb', '#0ea5e9', '#10b981', '#84cc16', '#f59e0b', '#f97316', '#ef4444', '#ec4899', '#94a3b8'];
const SIZE_OPTS = [[1,1],[2,1],[4,1],[2,2],[4,2],[2,3],[4,3],[4,4]];
const KIND_NAMES = { slider:'Volume', toggle:'Switch', action:'Button', app:'App', game:'Game', stream:'Desktop preview',
  stat:'Live stat', scene:'Scene', nowplaying:'Now playing', timer:'Sleep timer', appvol:'Volume mixer',
  audioout:'Audio output', windows:'Open windows', gamestats:'In-game', chat:'AI chat', guest:'VM / container',
  service:'Service', docker:'Container', link:'Link', spacer:'Spacer' };
const SEEKS = [5, 10, 15, 30, 60];

async function openTileSettings(id){
  const f = findTile(id);
  if (!f) return;
  if (navigator.vibrate) navigator.vibrate(12);
  TS = { id, d: JSON.parse(JSON.stringify(f.t)), devs: null };
  TS.d.opts = TS.d.opts || {};
  if (TS.d.kind === 'appvol') mxPoll();
  if (TS.d.kind === 'audioout') TS.devs = (S && S.devices) || [];
  drawTileSettings();
  sheetOnClose = applyTileSettings;
}

function tsSection(title, inner){ return `<div class="ts-sec"><div class="ts-h">${title}</div>${inner}</div>`; }

function drawTileSettings(){
  const d = TS.d, o = d.opts;
  let extra = '';
  if (d.kind === 'timer'){
    extra += tsSection('Time', `<div class="ts-chips">${[15, 30, 45, 60, 90, 120].map(m =>
      `<button class="ts-chip ${o.minutes === m || (!o.minutes && m === 30) ? 'on' : ''}" data-tsmin="${m}">${m < 60 ? m + ' min' : (m / 60) + ' h'}</button>`).join('')}</div>`);
    extra += tsSection('Then', `<div class="ts-chips">${TIMER_ACTS.map(([k, n]) =>
      `<button class="ts-chip ${(o.action || 'pause') === k ? 'on' : ''}" data-tsact="${k}">${n}</button>`).join('')}</div>
      ${['sleep', 'shutdown'].includes(o.action) ? '<div class="ts-note">Starting it asks for Face ID / your PIN, and you get a notification a minute before.</div>' : ''}`);
  }
  if (d.kind === 'appvol'){
    const apps = MX.apps;
    extra += tsSection('Shows', `<div class="ts-list">
      <div class="ts-opt ${!d.ref ? 'on' : ''}" data-tsapp=""><div class="mx-ic">${XI.mixer}</div><span>Every app playing sound</span><i>${XI.check}</i></div>
      ${apps.map(a => `<div class="ts-opt ${d.ref === a.app ? 'on' : ''}" data-tsapp="${esc(a.app)}"><div class="mx-ic">${mxIcon(a)}</div><span>${esc(a.name)}</span><i>${XI.check}</i></div>`).join('')}
      ${d.ref && !apps.some(a => a.app === d.ref) ? `<div class="ts-opt on" data-tsapp="${esc(d.ref)}"><div class="mx-ic">${XI.mixer}</div><span>${esc(d.label || d.ref)}</span><i>${XI.check}</i></div>` : ''}
      </div><div class="ts-note">Only apps making sound right now are listed - start the one you want, then come back.</div>`);
  }
  if (d.kind === 'audioout'){
    const sel = o.devices || [];
    extra += tsSection('Switch between', `<div class="ts-list">${(TS.devs || []).map(x =>
      `<div class="ts-opt ${sel.includes(x.id) ? 'on' : ''}" data-tsdev="${esc(x.id)}"><div class="mx-ic">${devIcon(x.name)}</div><span>${esc(shortDev(x.name))}</span><i>${XI.check}</i></div>`).join('')}</div>
      <div class="ts-note">Pick two or more - each tap moves to the next. None picked means all of them.</div>`);
  }
  if (d.kind === 'toggle' && d.ref === 'keeper'){
    const v = o.target == null ? '' : o.target;
    extra += tsSection('Hold the volume at', `<div class="ts-range"><input type="range" min="0" max="100" value="${v === '' ? (S ? S.volume : 30) : v}" id="tsTarget" style="--pct:${v === '' ? (S ? S.volume : 30) : v}%">
      <b id="tsTargetV">${v === '' ? 'current' : v + '%'}</b></div>
      <div class="ts-note">Leave it at "current" to hold whatever the volume is when you tap.</div>`);
  }
  if (d.kind === 'stat'){
    const ks = [['cpu','CPU'],['gpu','GPU'],['temp','GPU temp'],['ram','RAM'],['disk','Disk'],['net','Network'],['battery','Battery']];
    extra += tsSection('Shows', `<div class="ts-chips">${ks.map(([k, n]) => `<button class="ts-chip ${d.ref === k ? 'on' : ''}" data-tsstat="${k}">${n}</button>`).join('')}</div>`);
  }
  if (d.kind === 'action' && /^media\.(back|fwd)\d+$/.test(d.ref)){
    const m = d.ref.match(/^media\.(back|fwd)(\d+)$/);
    extra += tsSection('Jump', `<div class="ts-chips">${['back', 'fwd'].map(k => `<button class="ts-chip ${m[1] === k ? 'on' : ''}" data-tsdir="${k}">${k === 'back' ? 'Back' : 'Forward'}</button>`).join('')}</div>
      <div class="ts-chips" style="margin-top:8px">${SEEKS.map(s => `<button class="ts-chip ${+m[2] === s ? 'on' : ''}" data-tssec="${s}">${s < 60 ? s + 's' : '1 min'}</button>`).join('')}</div>`);
  }
  const accent = ACCENTS.map(c => `<button class="ts-sw ${(d.accent || '') === c ? 'on' : ''}" data-tsacc="${c}"
      style="${c ? '--c:' + c : ''}" aria-label="${c || 'Default colour'}">${c ? '' : '<span>A</span>'}</button>`).join('');
  const sizes = SIZE_OPTS.map(([w, h]) => `<button class="ts-size ${d.w === w && d.h === h ? 'on' : ''}" data-tssize="${w},${h}">
      <i style="--w:${w};--h:${h}"></i><span>${w}×${h}</span></button>`).join('');
  openSheet(KIND_NAMES[d.kind] || 'Tile', `
    <div class="ts-sec" style="margin-top:12px"><div class="ts-h">Name</div>
      <input class="field" id="tsName" value="${esc(d.label || '')}" maxlength="60" placeholder="${esc(KIND_NAMES[d.kind] || '')}" style="width:100%"></div>
    ${extra}
    ${tsSection('Size', `<div class="ts-sizes">${sizes}</div>`)}
    ${tsSection('Colour', `<div class="ts-sws">${accent}</div>`)}
    <button class="ts-rm" id="tsRemove">${XI.trash}<span>Remove tile</span></button>`,
    `<div style="flex-grow:1;font-size:11px;color:var(--muted)">Swipe down to save</div><button class="btn pri" id="tsDone">Done</button>`);
  $('#tsDone').onclick = () => closeSheet();
  $('#tsRemove').onclick = () => {
    const f = findTile(TS.id);
    TS = null; sheetOnClose = null;
    if (f){ f.sec.tiles.splice(f.i, 1); dirty = true; }
    closeSheet(); render(); saveLayoutNow();
  };
  const nm = $('#tsName');
  nm.oninput = () => { TS.d.label = nm.value; };
  const tr = $('#tsTarget');
  if (tr) tr.oninput = () => { TS.d.opts.target = +tr.value; tr.style.setProperty('--pct', tr.value + '%'); $('#tsTargetV').textContent = tr.value + '%'; };
}

$('#sheetBody').addEventListener('click', e => {
  if (!TS) return;
  const d = TS.d, o = d.opts;
  const q = (a) => e.target.closest('[' + a + ']');
  let el;
  if ((el = q('data-tsmin'))) o.minutes = +el.dataset.tsmin;
  else if ((el = q('data-tsact'))) o.action = el.dataset.tsact;
  else if ((el = q('data-tsapp'))){
    d.ref = el.dataset.tsapp;
    const a = MX.apps.find(x => x.app === d.ref);
    if (!d.ref) d.label = 'Volume mixer'; else if (a) d.label = a.name;
  }
  else if ((el = q('data-tsdev'))){
    const id = el.dataset.tsdev, s = new Set(o.devices || []);
    s.has(id) ? s.delete(id) : s.add(id); o.devices = [...s];
  }
  else if ((el = q('data-tsstat'))){
    const old = d.ref; d.ref = el.dataset.tsstat;
    const names = { cpu:'CPU', gpu:'GPU', temp:'GPU temp', ram:'RAM', disk:'Disk', net:'Network', battery:'Battery' };
    if (!d.label || d.label === names[old]) d.label = names[d.ref];
  }
  else if ((el = q('data-tsdir')) || (el = q('data-tssec'))){
    const m = d.ref.match(/^media\.(back|fwd)(\d+)$/);
    const dir = el.dataset.tsdir || m[1], sec = el.dataset.tssec || m[2];
    d.ref = 'media.' + dir + sec;
    d.label = (dir === 'back' ? 'Back ' : 'Forward ') + (+sec < 60 ? sec + 's' : '1 min');
  }
  else if ((el = q('data-tssize'))){ const [w, h] = el.dataset.tssize.split(',').map(Number); d.w = w; d.h = h; }
  else if ((el = q('data-tsacc'))) d.accent = el.dataset.tsacc || undefined;
  else return;
  const sc = $('#sheetBody').scrollTop;
  drawTileSettings();
  $('#sheetBody').scrollTop = sc;
});

function applyTileSettings(){
  if (!TS) return;
  const f = findTile(TS.id), d = TS.d;
  TS = null;
  if (!f) return;
  const nm = document.getElementById('tsName');
  if (nm) d.label = nm.value.trim();
  if (!d.label) d.label = KIND_NAMES[d.kind] || '';
  if (!d.accent) delete d.accent;
  if (!Object.keys(d.opts || {}).length) delete d.opts;
  Object.keys(f.t).forEach(k => delete f.t[k]);
  Object.assign(f.t, d);
  dirty = true;
  render();
  saveLayoutNow();
}
function saveLayoutNow(){
  api('/api/layout', L).then(l => { L = l; dirty = false; render(); toast('Saved'); }).catch(err => toast(err.message));
}

/* ================= notifications ================= */
const NT = { info: null, sub: null, prefs: null };
const TIER_NAMES = { off: 'Off', quiet: 'Quiet', normal: 'Normal', important: 'Important' };
function ntSupported(){
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window && window.isSecureContext;
}
function ntIOSNeedsHome(){
  return /iPhone|iPad|iPod/.test(navigator.userAgent) && !(navigator.standalone || matchMedia('(display-mode: standalone)').matches);
}
async function ntReg(){
  return navigator.serviceWorker.getRegistration('/') || navigator.serviceWorker.register('/sw.js', { scope: '/' });
}
async function openNotifications(){
  let reg = null;
  try {
    if (ntSupported()){ reg = await ntReg(); NT.sub = reg && await reg.pushManager.getSubscription(); }
  } catch(e){ NT.sub = null; }
  try {
    NT.info = await api('/api/push' + (NT.sub ? '?ep=' + encodeURIComponent(NT.sub.endpoint) : ''));
  } catch(err){ toast(err.message); return; }
  NT.prefs = NT.info.prefs || (NT.sub ? null : null);
  let hist = [];
  try { hist = (await api('/api/notifications')).items; } catch(e){}
  drawNotifications(hist);
}
function ntDefault(){
  return { enabled: true, types: Object.fromEntries(NT.info.types.map(t => [t.id, t.default])) };
}
function drawNotifications(hist){
  const on = !!(NT.sub && NT.prefs && NT.prefs.enabled);
  let top;
  if (!ntSupported()){
    top = `<div class="nt-warn">${ntIOSNeedsHome()
      ? 'On iPhone, notifications only work from the Home Screen app. Settings → <b>Add to Home Screen</b>, open it from there, then come back here.'
      : !window.isSecureContext ? 'Notifications need the secure (https) address of this PC.'
      : "This browser can't show notifications."}</div>`;
  } else if (Notification.permission === 'denied'){
    top = `<div class="nt-warn">Notifications are blocked for this app in your phone's settings. Allow them there, then come back.</div>`;
  } else top = '';
  const p = NT.prefs || ntDefault();
  const rows = NT.info.types.map(t => {
    const cur = p.types[t.id] || t.default;
    return `<div class="nt-type ${cur === 'off' ? 'off' : ''}"><div class="nt-tt"><b>${esc(t.name)}</b><span>${esc(t.desc)}</span></div>
      <div class="nt-seg" role="radiogroup" aria-label="${esc(t.name)}">${NT.info.tiers.map(k =>
        `<button class="t-${k} ${cur === k ? 'on' : ''}" data-nttier="${t.id}:${k}" role="radio" aria-checked="${cur === k}">${TIER_NAMES[k]}</button>`).join('')}</div></div>`;
  }).join('');
  const ago = ts => { const s = Date.now() / 1000 - ts; return s < 60 ? 'now' : s < 3600 ? Math.floor(s / 60) + 'm' : s < 86400 ? Math.floor(s / 3600) + 'h' : Math.floor(s / 86400) + 'd'; };
  const recent = hist.length ? hist.slice(0, 8).map(h => `<div class="nt-card"><div class="nt-ci">${XI.bell}</div>
      <div class="nt-cm"><div><b>${esc(h.title)}</b><time>${ago(h.ts)}</time></div><span>${esc(h.body)}</span></div></div>`).join('')
    : '<div class="ts-note" style="margin:0">Nothing yet.</div>';
  openSheet('Notifications', `${top}
    <div class="srow" style="margin-top:8px"><div style="flex-grow:1"><div class="t">Allow notifications</div>
      <div class="d">${on ? 'On for this phone' : 'Off for this phone'}</div></div>
      <div class="sw ${on ? 'on' : ''}" id="ntMaster" ${ntSupported() && Notification.permission !== 'denied' ? '' : 'style="opacity:.4;pointer-events:none"'}><i></i></div></div>
    <div class="${on ? '' : 'nt-dim'}">
      <div class="ts-h" style="margin:18px 2px 6px">What you get</div>
      <div class="nt-legend"><span><i class="t-quiet"></i>Quiet - no sound</span><span><i class="t-normal"></i>Normal</span><span><i class="t-important"></i>Important - stays until you clear it</span></div>
      ${rows}
      <button class="btn wide" id="ntTest" style="margin-top:14px">Send a test notification</button>
    </div>
    <div class="ts-h" style="margin:20px 2px 8px">Recent</div>${recent}
    <div style="height:10px"></div>`,
    `<div style="flex-grow:1;font-size:11px;color:var(--muted)">Each phone has its own settings</div><button class="btn" id="sheetClose">Done</button>`);
  $('#sheetClose').onclick = closeSheet;
  const m = $('#ntMaster');
  if (m) m.onclick = () => ntToggle(!on).catch(err => toast(err.message));
  const t = $('#ntTest');
  if (t) t.onclick = async () => {
    if (!on) return;
    try { await api('/api/push/test', { endpoint: NT.sub.endpoint, tier: 'normal' }); toast('Sent - check your notifications'); }
    catch(err){ toast(err.message); }
  };
  NT.hist = hist;
}
async function ntToggle(want){
  if (want){
    const perm = await Notification.requestPermission();
    if (perm !== 'granted'){ toast('Notifications were not allowed'); return openNotifications(); }
    const reg = await ntReg();
    await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true,
      applicationServerKey: unb64u(NT.info.publicKey) });
    const prefs = Object.assign(NT.prefs || ntDefault(), { enabled: true });
    const dev = /iPhone/.test(navigator.userAgent) ? 'iPhone' : /iPad/.test(navigator.userAgent) ? 'iPad' : /Android/.test(navigator.userAgent) ? 'Android' : 'Browser';
    const r = await api('/api/push/subscribe', { sub: sub.toJSON(), prefs, device: dev });
    NT.sub = sub; NT.prefs = r.prefs;
    toast('Notifications on');
  } else {
    NT.prefs = Object.assign(NT.prefs || ntDefault(), { enabled: false });
    await api('/api/push/prefs', { endpoint: NT.sub.endpoint, prefs: NT.prefs });
    toast('Notifications off');
  }
  drawNotifications(NT.hist || []);
}
$('#sheetBody').addEventListener('click', async e => {
  const b = e.target.closest('[data-nttier]');
  if (!b || !NT.sub || !NT.prefs || !NT.prefs.enabled) return;
  const [type, tier] = b.dataset.nttier.split(':');
  NT.prefs.types[type] = tier;
  const sc = $('#sheetBody').scrollTop;
  drawNotifications(NT.hist || []);
  $('#sheetBody').scrollTop = sc;
  try { NT.prefs = (await api('/api/push/prefs', { endpoint: NT.sub.endpoint, prefs: NT.prefs })).prefs; }
  catch(err){ toast(err.message); }
});
if ('serviceWorker' in navigator) navigator.serviceWorker.addEventListener('message', e => {
  if (e.data && e.data.aetherNotification && /#files/.test(e.data.aetherNotification) && typeof showTab === 'function') showTab('files');
});

/* ================= the AI chat tab ================= */
const CH = { info: null, conv: null, msgs: [], busy: false, ctrl: null, last: '' };
function chEl(){ return document.getElementById('chat'); }

/* Markdown, the safe way: escape everything first, then add back a few
   formats. Links only ever http(s), and they open outside the app. */
function md(src){
  const blocks = [];
  let s = esc(src).replace(/```[\w-]*\n?([\s\S]*?)```/g, (_, code) => { blocks.push(code); return '\u0000' + (blocks.length - 1) + '\u0000'; });
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, '$1<i>$2</i>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,!?:;])/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  const out = [];
  let list = null;
  for (const line of s.split('\n')){
    const ul = line.match(/^\s*[-*•]\s+(.*)/), ol = line.match(/^\s*\d+[.)]\s+(.*)/), h = line.match(/^#{1,4}\s+(.*)/);
    if (ul || ol){
      const tag = ul ? 'ul' : 'ol';
      if (!list || list.tag !== tag){ if (list) out.push(`</${list.tag}>`); list = { tag }; out.push(`<${tag}>`); }
      out.push(`<li>${(ul || ol)[1]}</li>`); continue;
    }
    if (list){ out.push(`</${list.tag}>`); list = null; }
    if (h) out.push(`<h4>${h[1]}</h4>`);
    else if (!line.trim()) out.push('<br>');
    else out.push(`<p>${line}</p>`);
  }
  if (list) out.push(`</${list.tag}>`);
  return out.join('').replace(/(<br>)+/g, '').replace(/\u0000(\d+)\u0000/g, (_, i) => `<pre>${blocks[+i]}</pre>`);
}

function chBuild(){
  const el = document.createElement('div');
  el.id = 'chat';
  el.innerHTML = `<div class="ch-head"><button class="fv-ib" data-ch="drawer" aria-label="Chats and memory">${XI.list}</button>
      <button class="ch-title" type="button" data-ch="model"><b>Chat</b><span class="ch-model"></span></button>
      <button class="fv-ib" data-ch="new" aria-label="New chat">${XI.plus}</button></div>
    <div class="ch-scroll"><div class="ch-msgs"></div></div>
    <form class="ch-comp" autocomplete="off"><div class="ch-box">
      <textarea rows="1" placeholder="Message" enterkeyhint="send"></textarea>
      <div class="ch-row"><button type="button" class="ch-pill" data-ch="model" aria-label="Change model">
        <span class="ch-pm"></span>${XI.down}</button><div class="ch-sp"></div>
        <button type="submit" class="ch-send" aria-label="Send">${XI.send}</button></div></div></form>
    <div class="ch-pop" role="menu"></div>
    <div class="ch-drawer"><div class="ch-dscrim" data-ch="dclose"></div>
      <aside class="ch-dpanel"><div class="ch-dhead"><b>Chats</b>
        <button class="fv-ib" data-ch="new" aria-label="New chat">${XI.plus}</button></div>
        <div class="ch-dbody"></div></aside></div>`;
  document.body.appendChild(el);
  const ta = el.querySelector('textarea'), form = el.querySelector('form'), sc = el.querySelector('.ch-scroll');
  const grow = () => { ta.style.height = 'auto'; ta.style.height = Math.min(140, ta.scrollHeight) + 'px'; chBtn(); };
  ta.addEventListener('input', grow);
  ta.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !('ontouchstart' in window)){ e.preventDefault(); form.requestSubmit(); } });

  /* The keyboard. iPhone's keyboard rises over the page and Safari then
     shoves the page up to keep the box in view - that shove is the jolt. So
     the chat shrinks to fit *as* the keyboard rises (we remember how tall it
     was last time), with the same easing, and the conversation stays pinned
     to its last message the whole way. The real size, when the phone
     reports it, just corrects the guess. */
  let kbGuess = 0, blurT = 0;
  try { kbGuess = +localStorage.getItem('aether.kb') || 0; } catch(e){}
  const atBottom = () => sc.scrollHeight - sc.scrollTop - sc.clientHeight < 80;
  const pin = (ms) => {
    if (!atBottom() && !CH.busy) return;
    const end = performance.now() + ms;
    const step = () => { sc.scrollTop = sc.scrollHeight; if (performance.now() < end) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  };
  const fit = () => {
    if (!el.classList.contains('show') || !window.visualViewport) return;
    const vv = visualViewport, kb = window.innerHeight - vv.height;
    if (kb > 150){ kbGuess = kb; try { localStorage.setItem('aether.kb', Math.round(kb)); } catch(e){} }
    el.style.top = vv.offsetTop + 'px';
    el.style.height = vv.height + 'px';
    pin(420);
  };
  CH.fit = fit;
  if (window.visualViewport){
    let q = 0;
    const later = () => { if (!q) q = requestAnimationFrame(() => { q = 0; fit(); }); };
    visualViewport.addEventListener('resize', later);
    visualViewport.addEventListener('scroll', later);
  }
  ta.addEventListener('focus', () => {
    clearTimeout(blurT);
    chPopClose();
    document.body.classList.add('kbd');
    try { kbGuess = +localStorage.getItem('aether.kb') || kbGuess; } catch(e){}
    const vv = window.visualViewport;
    if (kbGuess && vv && vv.height > window.innerHeight - 60) el.style.height = (window.innerHeight - kbGuess) + 'px';
    pin(450);
  });
  ta.addEventListener('blur', () => {
    blurT = setTimeout(() => {
      document.body.classList.remove('kbd');
      if (window.visualViewport && visualViewport.height > window.innerHeight - 60) el.style.height = window.innerHeight + 'px';
    }, 60);
  });

  form.addEventListener('submit', e => {
    e.preventDefault();
    if (CH.busy){ if (CH.ctrl) CH.ctrl.abort(); return; }
    const text = ta.value.trim();
    if (!text) return;
    ta.value = ''; grow();
    chSend(text);
  });
  // Buttons act on pointer-up when the press started on them: a tap that
  // blurs the keyboard (and moves the page) still lands where you aimed.
  let downOn = null;
  el.addEventListener('pointerdown', e => { downOn = e.target.closest('[data-ch]'); }, true);
  el.addEventListener('pointerup', e => {
    const b = downOn; downOn = null;
    if (!b || !b.isConnected || e.button > 0) return;
    const r = b.getBoundingClientRect();
    if (e.clientX < r.left - 24 || e.clientX > r.right + 24 || e.clientY < r.top - 24 || e.clientY > r.bottom + 24) return;
    e.preventDefault();
    chAct(b);
    b.dataset.fired = Date.now();
  });
  el.addEventListener('click', e => {
    const b = e.target.closest('[data-ch]');
    if (b){ if (Date.now() - (+b.dataset.fired || 0) > 600) chAct(b); return; }
    const sug = e.target.closest('[data-chsug]');
    if (sug){ chSend(sug.dataset.chsug); return; }
    if (!e.target.closest('.ch-pop')) chPopClose();
  });
  el.querySelector('.ch-dbody').addEventListener('click', chDrawerClick);
  return el;
}
function chAct(b){
  const a = b.dataset.ch, ta = chEl().querySelector('textarea');
  if (a === 'new'){ chDrawerClose(); chPopClose(); if (CH.ctrl) CH.ctrl.abort(); CH.conv = null; CH.msgs = []; chDraw(); }
  else if (a === 'drawer'){ chPopClose(); ta.blur(); chDrawerOpen(); }
  else if (a === 'dclose') chDrawerClose();
  else if (a === 'model'){ const p = chEl().querySelector('.ch-pop'); p.classList.contains('show') ? chPopClose() : chModels(b); }
  else if (a === 'memory'){ chDrawerClose(); ta.blur(); memOpen(); }
}
const chShort = id => String(id || '').split('/').pop();

/* ---- the model menu: a small menu that grows out of the button you
   tapped. Only models - nothing else lives here. The key and the provider
   stay on the PC; the phone only ever picks from the list. */
async function chModels(anchor){
  const el = chEl(), pop = el.querySelector('.ch-pop');
  const fromPill = anchor && anchor.classList.contains('ch-pill');
  const er = el.getBoundingClientRect(), ar = anchor.getBoundingClientRect();
  pop.classList.toggle('up', fromPill);
  pop.style.left = fromPill ? Math.max(10, ar.left - er.left) + 'px' : '50%';
  pop.style.top = fromPill ? '' : (ar.bottom - er.top + 6) + 'px';
  pop.style.bottom = fromPill ? (er.bottom - ar.top + 8) + 'px' : '';
  const paint = () => {
    const cur = (CH.info || {}).model || '', ms = CH.models;
    pop.innerHTML = `<div class="ch-ph"><span>Model</span><em>${esc((CH.info || {}).providerName || '')}</em></div>
      ${ms ? (ms.length ? ms.map(m => `<button class="ch-pi${m.id === cur ? ' on' : ''}" data-chpick="${esc(m.id)}" role="menuitem">
        <span class="ch-pt"><b>${esc(chShort(m.id))}</b><small>${esc([m.id.includes('/') ? m.id.split('/')[0] : '', m.detail,
          m.loaded ? 'ready' : ''].filter(Boolean).join(' · '))}</small></span>
        ${m.loaded ? '<i class="ch-live-dot"></i>' : ''}<i class="ch-pc">${m.id === cur ? XI.check : ''}</i></button>`).join('')
        : '<div class="ch-pnote">No models found on the PC.</div>')
        : '<div class="ch-pnote"><span class="spin"></span>Looking…</div>'}`;
    pop.querySelectorAll('[data-chpick]').forEach(b => b.onclick = e => { e.stopPropagation(); chPick(b.dataset.chpick, b); });
  };
  paint();
  requestAnimationFrame(() => pop.classList.add('show'));
  try { CH.models = (await api('/api/chat/models')).models || []; }
  catch(err){ if (!CH.models){ pop.innerHTML = `<div class="ch-pnote bad">${esc(err.message)}</div>`; return; } }
  if (pop.classList.contains('show')) paint();
}
async function chPick(id, b){
  if (id === (CH.info || {}).model){ chPopClose(); return; }
  b.classList.add('busy');
  try {
    const r = await api('/api/chat/model', { model: id });
    CH.info = Object.assign(CH.info || {}, r);
    chPopClose(); chDraw();
    toast('Now using ' + chShort(id));
  } catch(err){ b.classList.remove('busy'); toast(err.message); }
}
function chPopClose(){ const el = chEl(); if (el) el.querySelector('.ch-pop').classList.remove('show'); }

/* ---- the drawer (the ☰ button): your memory, and your chats. It opens at
   once from what's already loaded, then refreshes. */
function chDrawerOpen(){
  const el = chEl(); if (!el) return;
  chDrawerPaint();
  el.querySelector('.ch-drawer').classList.add('open');
  document.body.classList.add('chDrawer');
  api('/api/chat').then(j => { CH.info = Object.assign(CH.info || {}, j); chDrawerPaint(); }).catch(() => {});
}
function chDrawerClose(){ const el = chEl(); if (el) el.querySelector('.ch-drawer').classList.remove('open'); document.body.classList.remove('chDrawer'); }
function chDrawerPaint(){
  const el = chEl(), body = el.querySelector('.ch-dbody'), j = CH.info || {}, convs = j.convs || [];
  const mem = (j.tools || []).find(t => t.id === 'memory' && t.on);
  const day = ts => { const d = new Date(ts * 1000), now = new Date();
    return d.toDateString() === now.toDateString() ? d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
      : d.toLocaleDateString([], { month: 'short', day: 'numeric' }); };
  body.innerHTML = `${mem ? `<button class="ch-memrow" data-ch="memory">${XI.graph}<span><b>Memory</b>
      <small>${j.memory ? `${j.memory} thing${j.memory === 1 ? '' : 's'} it knows about you` : 'Nothing saved yet'}</small></span>${FVI.chev}</button>` : ''}
    <div class="ch-dlabel">Recent</div>
    <div class="ch-dlist">${convs.map(c => `<div class="ch-ci${c.id === CH.conv ? ' on' : ''}" data-chopen="${esc(c.id)}">
        <span class="ch-ct"><b>${esc(c.title || 'Chat')}</b><small>${day(c.updated)}</small></span>
        <button class="ch-cdel" data-chdel="${esc(c.id)}" aria-label="Delete">${XI.trash}</button></div>`).join('')
      || '<div class="ch-dnone">No chats yet.</div>'}</div>
    ${convs.length ? '<button class="ch-dclear" data-chclear>Delete all chats</button>' : ''}`;
}
async function chDrawerClick(e){
  const del = e.target.closest('[data-chdel]'), open = e.target.closest('[data-chopen]'), clear = e.target.closest('[data-chclear]');
  if (del){
    e.stopPropagation();
    const row = del.closest('.ch-ci');
    if (!row.classList.contains('sure')){ row.classList.add('sure'); setTimeout(() => row.classList.remove('sure'), 3000); return; }
    try {
      const j = await api('/api/chat/delete', { id: del.dataset.chdel });
      CH.info.convs = j.convs;
      if (CH.conv === del.dataset.chdel){ CH.conv = null; CH.msgs = []; chDraw(); }
      chDrawerPaint();
    } catch(err){ toast(err.message); }
    return;
  }
  if (clear){
    if (!clear.dataset.sure){ clear.dataset.sure = 1; clear.textContent = 'Sure? Tap again to delete every chat'; clear.classList.add('sure'); return; }
    try { await api('/api/chat/clear', {}); CH.info.convs = []; CH.conv = null; CH.msgs = []; chDraw(); chDrawerPaint(); } catch(err){ toast(err.message); }
    return;
  }
  if (open){
    try {
      const c = await api('/api/chat/conv?id=' + encodeURIComponent(open.dataset.chopen));
      CH.conv = c.id; CH.msgs = c.messages.map(m => ({ role: m.role, text: m.text, tools: (m.tools || []) }));
      chDrawerClose(); chDraw();
    } catch(err){ toast(err.message); }
  }
}
function chBtn(){
  const el = chEl(); if (!el) return;
  const b = el.querySelector('.ch-send'), ta = el.querySelector('textarea');
  b.innerHTML = CH.busy ? XI.stop : XI.send;
  b.classList.toggle('busy', CH.busy);
  b.disabled = !CH.busy && !ta.value.trim();
}
function chMsgHTML(m, i){
  if (m.role === 'user') return `<div class="ch-m me"><div class="ch-b">${esc(m.text).replace(/\n/g, '<br>')}</div></div>`;
  const all = m.tools || [], running = all.find(t => t.ok === undefined);
  const tools = all.filter(t => t.ok !== undefined).map(t => `<div class="ch-tool ${t.ok ? 'ok' : 'bad'}">
      <i>${t.ok ? XI.check : '!'}</i>${esc(t.label)}</div>`).join('');
  // While it works: the orb, and what it's doing right now.
  const live = m.live && (running || !m.text)
    ? `<div class="ch-live"><span class="ch-orbslot"></span><span class="ch-status">${esc(running ? running.label : 'Thinking')}</span></div>` : '';
  return `<div class="ch-m ai" data-i="${i}">${tools ? `<div class="ch-tools">${tools}</div>` : ''}${live}
    ${m.text ? `<div class="ch-b md">${md(m.text)}</div>` : ''}
    ${m.error ? `<div class="ch-err">${esc(m.error)}</div>` : ''}</div>`;
}
function chDraw(){
  const el = chEl(); if (!el) return;
  const c = CH.info || {};
  el.querySelector('.ch-model').textContent = c.model || '';
  el.querySelector('.ch-pm').textContent = chShort(c.model) || 'Pick a model';
  const box = el.querySelector('.ch-msgs');
  if (!CH.msgs.length){
    const on = (c.tools || []).filter(t => t.on).map(t => t.name.toLowerCase());
    const sugg = [];
    if (on.includes('pc status')) sugg.push("How's my PC doing?");
    if (on.includes('web search')) sugg.push("What's new in tech today?");
    if (on.includes('media')) sugg.push('Skip this song');
    if (on.includes('open apps')) sugg.push('Open Spotify');
    box.innerHTML = `<div class="ch-empty"><span class="ch-heroslot"></span><b>What can I help with?</b>
      <span>${on.length ? 'It can use: ' + esc(on.join(', ')) + '.' : 'Just chat - no tools are switched on.'}</span>
      ${c.memory ? `<button class="ch-memlink" data-ch="memory">${XI.graph}It remembers ${c.memory} thing${c.memory === 1 ? '' : 's'} about you</button>` : ''}
      <div class="ch-sugs">${sugg.map(s => `<button data-chsug="${esc(s)}">${esc(s)}</button>`).join('')}</div></div>`;
  } else box.innerHTML = CH.msgs.map(chMsgHTML).join('');
  chOrbMount();
  chBtn();
  const sc = el.querySelector('.ch-scroll');
  sc.scrollTop = sc.scrollHeight;
}
function chPatchLast(){
  const el = chEl(); if (!el) return;
  const i = CH.msgs.length - 1, node = el.querySelector(`.ch-m.ai[data-i="${i}"]`);
  const html = chMsgHTML(CH.msgs[i], i);
  if (node){ node.outerHTML = html; chOrbMount(); } else chDraw();
  const sc = el.querySelector('.ch-scroll');
  if (sc.scrollHeight - sc.scrollTop - sc.clientHeight < 160) sc.scrollTop = sc.scrollHeight;
}
async function chSend(text){
  if (CH.busy) return;
  CH.msgs.push({ role: 'user', text });
  const ai = { role: 'assistant', text: '', tools: [], live: true };
  CH.msgs.push(ai);
  CH.busy = true; CH.ctrl = new AbortController();
  chDraw();
  let frame = 0;
  const paint = () => { if (!frame) frame = requestAnimationFrame(() => { frame = 0; chPatchLast(); }); };
  try {
    const r = await fetch('/api/chat/send', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conv: CH.conv, text }), signal: CH.ctrl.signal });
    if (r.status === 401){ location.href = '/login'; return; }
    if (r.status === 403){ const j = await r.json().catch(() => ({})); if (j.error === 'locked'){ await sfUnlock(); CH.msgs.splice(-2); CH.busy = false; return chSend(text); } }
    if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
    const rd = r.body.getReader(), dec = new TextDecoder();
    let buf = '';
    for (;;){
      const { value, done } = await rd.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf('\n\n')) >= 0){
        const chunk = buf.slice(0, k); buf = buf.slice(k + 2);
        if (!chunk.startsWith('data:')) continue;
        let ev; try { ev = JSON.parse(chunk.slice(5)); } catch(e){ continue; }
        if (ev.t === 'start') CH.conv = ev.conv;
        else if (ev.t === 'text') ai.text += ev.d;
        else if (ev.t === 'tool') ai.tools.push({ label: ev.label });
        else if (ev.t === 'tool_done'){ const t = ai.tools[ai.tools.length - 1]; if (t) t.ok = ev.ok; }
        else if (ev.t === 'error') ai.error = ev.d;
        paint();
      }
    }
  } catch(err){
    if (err.name !== 'AbortError') ai.error = err.message;
    else if (!ai.text) ai.error = 'Stopped';
  }
  ai.live = false;
  ai.tools.forEach(t => { if (t.ok === undefined) t.ok = false; });
  CH.busy = false; CH.ctrl = null;
  CH.last = ai.text ? ai.text.slice(0, 140) : CH.last;
  chPatchLast(); chBtn();
}
async function chLoad(){
  if (!isPC()) return;
  try { CH.info = await api('/api/chat'); } catch(e){ return; }
  const tabs = document.getElementById('tabs');
  if (tabs && CH.info.ready && !tabs.querySelector('[data-tab=chat]')){
    tabs.insertAdjacentHTML('beforeend', `<button data-tab="chat">${XI.spark}<span>Chat</span></button>`);
  }
  if (tabs && !CH.info.ready){ const b = tabs.querySelector('[data-tab=chat]'); if (b) b.remove(); }
}

/* ================= the particle orb ================= */
/* A cloud of points on and inside a sphere. Idle, it breathes and turns
   slowly; while the AI thinks it pulls in tight and spins; while a tool runs
   it sits in between. Always in the theme's two colours, re-read now and
   then so a theme change shows up without a reload. */
function themeRGB(){
  const cs = getComputedStyle(document.documentElement);
  const hex = (v, d) => { const m = /^#?([0-9a-f]{6})$/i.exec((v || '').trim()); const n = m ? parseInt(m[1], 16) : d;
    return [n >> 16 & 255, n >> 8 & 255, n & 255]; };
  return [hex(cs.getPropertyValue('--primary'), 0x7c3aed), hex(cs.getPropertyValue('--secondary'), 0x22d3ee)];
}
const ORB_STATES = {
  idle:  { r: 1,   spin: .16, wob: .045, bright: .8 },
  think: { r: .64, spin: .95, wob: .12,  bright: 1 },
  tool:  { r: .8,  spin: .55, wob: .08,  bright: .95 },
};
class Orb {
  constructor(size, n){
    this.size = size;
    this.d = Math.min(3, window.devicePixelRatio || 1);
    this.c = document.createElement('canvas');
    this.c.className = 'orb-c';
    this.c.width = this.c.height = Math.round(size * this.d);
    this.c.style.width = this.c.style.height = size + 'px';
    this.x = this.c.getContext('2d');
    this.p = [];
    const g = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < n; i++){
      const y = 1 - (i / (n - 1)) * 2, rad = Math.sqrt(1 - y * y), th = g * i;
      this.p.push({ x: Math.cos(th) * rad, y, z: Math.sin(th) * rad,
        k: .3 + .7 * Math.pow(Math.random(), .4), ph: Math.random() * 6.283, f: .6 + Math.random() * 1.4 });
    }
    this.want = ORB_STATES.idle; this.cur = Object.assign({}, this.want);
    this.energy = 0; this.a = Math.random() * 6; this.t = 0; this.fr = 0; this.raf = 0; this.last = 0;
    this.col = themeRGB();
    this.still = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  }
  state(s){
    const w = ORB_STATES[s] || ORB_STATES.idle;
    if (w !== this.want){ this.want = w; this.energy = 1; }
    if (this.still){ this.cur = Object.assign({}, w); this.col = themeRGB(); this.draw(); }
    else this.run();
  }
  run(){ if (!this.raf) this.raf = requestAnimationFrame(ts => this.frame(ts)); }
  frame(ts){
    this.raf = 0;
    if (!this.c.isConnected){ this.last = 0; return; }      // off the page: rest until it's put back
    const dt = this.last ? Math.min(.05, (ts - this.last) / 1000) : .016;
    this.last = ts;
    if (++this.fr % 60 === 0) this.col = themeRGB();
    for (const k in this.cur) this.cur[k] += (this.want[k] - this.cur[k]) * Math.min(1, dt * 3.2);
    this.energy *= Math.pow(.2, dt);
    this.t += dt;
    this.a += dt * (this.cur.spin + this.energy * 2.4);
    this.draw();
    this.run();
  }
  draw(){
    const x = this.x, W = this.c.width, h = W / 2, R = h * .74 * this.cur.r, b = this.cur.bright;
    const [p1, p2] = this.col;
    x.globalCompositeOperation = 'source-over';
    x.clearRect(0, 0, W, W);
    const g = x.createRadialGradient(h, h, 0, h, h, R * 1.3);
    g.addColorStop(0, `rgba(${p1},${.38 * b})`);
    g.addColorStop(.5, `rgba(${p1},${.1 * b})`);
    g.addColorStop(1, `rgba(${p1},0)`);
    x.fillStyle = g; x.fillRect(0, 0, W, W);
    x.globalCompositeOperation = 'lighter';
    const ca = Math.cos(this.a), sa = Math.sin(this.a);
    const tilt = .4 + Math.sin(this.t * .3) * .18, ct = Math.cos(tilt), st = Math.sin(tilt);
    const round = this.size > 40, dot = this.d * (round ? 1.15 : .95), wob = this.cur.wob * 2.2;
    for (const q of this.p){
      const w = 1 + Math.sin(this.t * q.f * 1.8 + q.ph) * wob;
      const px = q.x * q.k * w, py = q.y * q.k * w, pz = q.z * q.k * w;
      const x1 = px * ca + pz * sa, z0 = -px * sa + pz * ca;
      const y1 = py * ct - z0 * st, z1 = py * st + z0 * ct;
      const s = 2.6 / (2.6 + z1), m = (1 - z1) / 2;
      const r = p1[0] + (p2[0] - p1[0]) * m | 0, gg = p1[1] + (p2[1] - p1[1]) * m | 0, bb = p1[2] + (p2[2] - p1[2]) * m | 0;
      x.fillStyle = `rgba(${r},${gg},${bb},${(.22 + .62 * m) * b})`;
      const sz = dot * s * (.7 + .6 * q.k);
      if (round){ x.beginPath(); x.arc(h + x1 * R * s, h + y1 * R * s, sz * .62, 0, 6.283); x.fill(); }
      else x.fillRect(h + x1 * R * s - sz / 2, h + y1 * R * s - sz / 2, sz, sz);
    }
    x.globalCompositeOperation = 'source-over';
  }
}
/* The chat's live line and the empty screen each borrow one orb; it moves
   from node to node as the message re-renders, so it never restarts. */
function chOrbMount(){
  const el = chEl(); if (!el) return;
  const slot = el.querySelector('.ch-orbslot');
  if (slot){
    if (!CH.orb) CH.orb = new Orb(24, 170);
    if (CH.orb.c.parentNode !== slot) slot.appendChild(CH.orb.c);
    const m = CH.msgs[CH.msgs.length - 1];
    CH.orb.state(m && (m.tools || []).some(t => t.ok === undefined) ? 'tool' : 'think');
  }
  const hero = el.querySelector('.ch-heroslot');
  if (hero){
    if (!CH.hero) CH.hero = new Orb(104, 720);
    if (CH.hero.c.parentNode !== hero) hero.appendChild(CH.hero.c);
    CH.hero.state('idle');
  }
}

/* ================= memory: what the chat knows about you ================= */
/* A graph like Obsidian's: you in the middle, your topics around you, and
   each saved fact on the topics it's filed under. Drag to pan, pinch to
   zoom, drag a dot to pull it about, tap one to read or delete it. */
const MG = { el: null, cv: null, ctx: null, nodes: [], links: [], byId: {}, adj: {}, sel: null,
  cam: { x: 0, y: 0, k: 1 }, alpha: 0, raf: 0, ptr: new Map(), drag: null, info: null };

function memOpen(){
  if (!MG.el) memBuild();
  closeSheet();
  MG.el.classList.add('show');
  document.body.classList.add('memOn');
  memSize();
  memLoad();
}
function memClose(){
  if (!MG.el) return;
  MG.el.classList.remove('show');
  document.body.classList.remove('memOn');
  cancelAnimationFrame(MG.raf); MG.raf = 0;
  chLoad().then(() => { const el = chEl(); if (el && el.classList.contains('show')) chDraw(); });
}
function memBuild(){
  const el = document.createElement('div');
  el.id = 'mem';
  el.innerHTML = `<div class="mg-head"><button class="fv-back" data-mg="back">${XI.back}<span>Chat</span></button>
      <div class="mg-title"><b>Memory</b><span class="mg-sub"></span></div><div class="mg-sp"></div></div>
    <div class="mg-wrap"><canvas></canvas></div>
    <div class="mg-card"></div>`;
  document.body.appendChild(el);
  MG.el = el; MG.cv = el.querySelector('canvas'); MG.ctx = MG.cv.getContext('2d');
  el.querySelector('[data-mg=back]').onclick = memClose;
  window.addEventListener('resize', () => { if (el.classList.contains('show')){ memSize(); memKick(.05); } });
  const cv = MG.cv;
  cv.addEventListener('pointerdown', e => {
    cv.setPointerCapture(e.pointerId);
    MG.ptr.set(e.pointerId, { x: e.offsetX, y: e.offsetY, x0: e.offsetX, y0: e.offsetY, t: Date.now() });
    if (MG.ptr.size === 1){
      const n = memHit(e.offsetX, e.offsetY);
      MG.drag = n && n.id !== 'you' ? n : null;
      if (MG.drag){ MG.drag.fx = MG.drag.x; MG.drag.fy = MG.drag.y; memKick(.3); }
    } else MG.drag = null;
  });
  cv.addEventListener('pointermove', e => {
    const p = MG.ptr.get(e.pointerId); if (!p) return;
    const dx = e.offsetX - p.x, dy = e.offsetY - p.y;
    if (MG.ptr.size === 2){
      const [a, b] = [...MG.ptr.values()];
      const d0 = Math.hypot(a.x - b.x, a.y - b.y);
      p.x = e.offsetX; p.y = e.offsetY;
      const d1 = Math.hypot(a.x - b.x, a.y - b.y);
      if (d0 > 0) memZoom(d1 / d0, (a.x + b.x) / 2, (a.y + b.y) / 2);
      return;
    }
    p.x = e.offsetX; p.y = e.offsetY;
    if (MG.drag){
      const w = memWorld(e.offsetX, e.offsetY);
      MG.drag.fx = w.x; MG.drag.fy = w.y; memKick(.3);
    } else { MG.cam.x += dx; MG.cam.y += dy; memPaint(); }
  });
  const up = e => {
    const p = MG.ptr.get(e.pointerId);
    MG.ptr.delete(e.pointerId);
    if (!p) return;
    const moved = Math.hypot(p.x - p.x0, p.y - p.y0);
    if (MG.drag){ delete MG.drag.fx; delete MG.drag.fy; }
    if (moved < 7 && Date.now() - p.t < 450 && MG.ptr.size === 0){
      const n = memHit(p.x0, p.y0);
      MG.sel = n && n.id !== 'you' ? n.id : null;
      memCard(); memPaint();
    }
    MG.drag = null;
  };
  cv.addEventListener('pointerup', up);
  cv.addEventListener('pointercancel', up);
  cv.addEventListener('wheel', e => { e.preventDefault(); memZoom(Math.exp(-e.deltaY / 400), e.offsetX, e.offsetY); }, { passive: false });
  el.querySelector('.mg-card').addEventListener('click', async e => {
    const b = e.target.closest('[data-mgf], [data-mgsel], [data-mgclear]');
    if (!b) return;
    if (b.dataset.mgsel){ MG.sel = b.dataset.mgsel; memCard(); memPaint(); return; }
    if (b.dataset.mgclear !== undefined){
      if (!b.dataset.sure){ b.dataset.sure = 1; b.textContent = 'Sure? Tap again'; b.classList.add('danger'); return; }
      try { memSet(await api('/api/chat/memory/clear', {})); toast('Memory cleared'); } catch(err){ toast(err.message); }
      return;
    }
    b.disabled = true;
    try {
      const j = await api('/api/chat/memory/forget', { id: b.dataset.mgf });
      if (MG.sel === 'f:' + b.dataset.mgf) MG.sel = null;
      memSet(j); toast('Forgotten');
    } catch(err){ b.disabled = false; toast(err.message); }
  });
}
function memSize(){
  const w = MG.cv.parentNode.clientWidth, h = MG.cv.parentNode.clientHeight, d = Math.min(3, window.devicePixelRatio || 1);
  MG.cv.width = Math.round(w * d); MG.cv.height = Math.round(h * d);
  MG.cv.style.width = w + 'px'; MG.cv.style.height = h + 'px';
  MG.dpr = d; MG.w = w; MG.h = h;
  if (!MG.placed){ MG.cam = { x: w / 2, y: h * .42, k: 1 }; MG.placed = true; }
}
async function memLoad(){
  MG.el.querySelector('.mg-sub').textContent = 'Loading…';
  try { memSet(await api('/api/chat/memory')); }
  catch(err){ MG.el.querySelector('.mg-sub').textContent = err.message; }
}
function memSet(j){
  MG.info = j;
  const old = MG.byId, nodes = j.nodes.map(n => Object.assign({}, n));
  MG.byId = {}; nodes.forEach(n => MG.byId[n.id] = n);
  MG.adj = {}; nodes.forEach(n => MG.adj[n.id] = new Set());
  const links = j.links.filter(l => MG.byId[l.s] && MG.byId[l.t]);
  links.forEach(l => { MG.adj[l.s].add(l.t); MG.adj[l.t].add(l.s); });
  for (const n of nodes){
    const o = old[n.id];
    n.r = n.kind === 'you' ? 17 : n.kind === 'topic' ? 7 + Math.min(9, Math.sqrt(n.n) * 2.4) : 4;
    if (o){ n.x = o.x; n.y = o.y; n.vx = o.vx; n.vy = o.vy; continue; }
    if (n.kind === 'you'){ n.x = n.y = 0; }
    else {
      const near = [...MG.adj[n.id]].map(k => old[k] || MG.byId[k]).find(m => m && m.x !== undefined);
      const a = Math.random() * 6.283, d = n.kind === 'topic' ? 120 : 40;
      n.x = (near ? near.x : 0) + Math.cos(a) * d; n.y = (near ? near.y : 0) + Math.sin(a) * d;
    }
    n.vx = n.vy = 0;
  }
  MG.nodes = nodes; MG.links = links;
  if (MG.sel && !MG.byId[MG.sel]) MG.sel = null;
  const fresh = Object.keys(old).length === 0;
  MG.alpha = fresh ? 1 : .5;
  if (fresh) for (let i = 0; i < 160; i++) memTick();      // settle before the first frame
  MG.el.querySelector('.mg-sub').textContent = j.count ? `${j.count} thing${j.count === 1 ? '' : 's'} · ${j.topics} topic${j.topics === 1 ? '' : 's'}` : 'Nothing yet';
  memCard();
  memKick(MG.alpha);
}
function memKick(a){ MG.alpha = Math.max(MG.alpha, a); if (!MG.raf) MG.raf = requestAnimationFrame(memFrame); }
function memFrame(){
  MG.raf = 0;
  if (!MG.el.classList.contains('show')) return;
  memTick(); memPaint();
  if (MG.alpha > .004 || MG.drag) MG.raf = requestAnimationFrame(memFrame);
}
function memTick(){
  const N = MG.nodes, a = MG.alpha;
  for (let i = 0; i < N.length; i++){
    const p = N[i];
    for (let j = i + 1; j < N.length; j++){
      const q = N[j];
      let dx = q.x - p.x, dy = q.y - p.y, d2 = dx * dx + dy * dy;
      if (d2 < 1){ dx = Math.random() - .5; dy = Math.random() - .5; d2 = 1; }
      if (d2 > 90000) continue;
      const f = (p.kind === 'fact' && q.kind === 'fact' ? 520 : 1500) * a / d2, d = Math.sqrt(d2);
      p.vx -= dx / d * f; p.vy -= dy / d * f; q.vx += dx / d * f; q.vy += dy / d * f;
    }
  }
  for (const l of MG.links){
    const s = MG.byId[l.s], t = MG.byId[l.t];
    const want = s.kind === 'you' || t.kind === 'you' ? 125 : 52;
    const dx = t.x - s.x, dy = t.y - s.y, d = Math.max(1, Math.hypot(dx, dy));
    const f = (d - want) / d * .08 * a;
    s.vx += dx * f; s.vy += dy * f; t.vx -= dx * f; t.vy -= dy * f;
  }
  for (const n of N){
    if (n.kind === 'you'){ n.x = n.y = n.vx = n.vy = 0; continue; }
    if (n.fx !== undefined){ n.x = n.fx; n.y = n.fy; n.vx = n.vy = 0; continue; }
    n.vx = (n.vx - n.x * .004 * a) * .78; n.vy = (n.vy - n.y * .004 * a) * .78;
    n.x += n.vx; n.y += n.vy;
  }
  MG.alpha += (0 - MG.alpha) * .02;
}
function memWorld(sx, sy){ return { x: (sx - MG.cam.x) / MG.cam.k, y: (sy - MG.cam.y) / MG.cam.k }; }
function memZoom(f, sx, sy){
  const k = Math.max(.35, Math.min(4, MG.cam.k * f)); f = k / MG.cam.k;
  MG.cam.x = sx - (sx - MG.cam.x) * f; MG.cam.y = sy - (sy - MG.cam.y) * f; MG.cam.k = k;
  memPaint();
}
function memHit(sx, sy){
  const w = memWorld(sx, sy);
  let best = null, bd = 1e9;
  for (const n of MG.nodes){
    const d = Math.hypot(n.x - w.x, n.y - w.y) - n.r;
    if (d < bd){ bd = d; best = n; }
  }
  return bd * MG.cam.k < 14 ? best : null;
}
function memPaint(){
  const x = MG.ctx, d = MG.dpr || 1, [p1, p2] = themeRGB(), k = MG.cam.k;
  const sel = MG.sel, near = sel ? MG.adj[sel] : null;
  const lit = n => !sel || n.id === sel || near.has(n.id);
  x.setTransform(1, 0, 0, 1, 0, 0);
  x.clearRect(0, 0, MG.cv.width, MG.cv.height);
  x.setTransform(d * k, 0, 0, d * k, d * MG.cam.x, d * MG.cam.y);
  x.lineWidth = 1 / k;
  for (const l of MG.links){
    const s = MG.byId[l.s], t = MG.byId[l.t], on = sel && (l.s === sel || l.t === sel);
    x.strokeStyle = on ? `rgba(${p2},.85)` : `rgba(${p1},${sel ? .1 : .32})`;
    x.lineWidth = (on ? 1.6 : 1) / k;
    x.beginPath(); x.moveTo(s.x, s.y); x.lineTo(t.x, t.y); x.stroke();
  }
  for (const n of MG.nodes){
    const on = lit(n);
    x.globalAlpha = on ? 1 : .22;
    if (n.kind === 'you'){
      const g = x.createRadialGradient(n.x - 5, n.y - 6, 2, n.x, n.y, n.r * 2.2);
      g.addColorStop(0, `rgb(${p2})`); g.addColorStop(.42, `rgb(${p1})`); g.addColorStop(.5, `rgba(${p1},.35)`); g.addColorStop(1, `rgba(${p1},0)`);
      x.fillStyle = g; x.beginPath(); x.arc(n.x, n.y, n.r * 2.2, 0, 6.283); x.fill();
    } else {
      if (n.id === sel){ x.fillStyle = `rgba(${p2},.25)`; x.beginPath(); x.arc(n.x, n.y, n.r + 6, 0, 6.283); x.fill(); }
      x.fillStyle = n.kind === 'topic' ? `rgb(${p1})` : `rgba(${p2},.9)`;
      x.beginPath(); x.arc(n.x, n.y, n.r, 0, 6.283); x.fill();
      if (n.kind === 'topic'){ x.strokeStyle = `rgba(${p2},.7)`; x.lineWidth = 1.2 / k; x.stroke(); }
    }
  }
  x.globalAlpha = 1;
  // Labels in screen pixels, so they stay readable at any zoom.
  x.setTransform(d, 0, 0, d, 0, 0);
  x.textAlign = 'center'; x.textBaseline = 'top';
  for (const n of MG.nodes){
    const show = n.kind !== 'fact' || k > 1.7 || n.id === sel || (sel && near.has(n.id));
    if (!show) continue;
    const sx = MG.cam.x + n.x * k, sy = MG.cam.y + (n.y + (n.kind === 'you' ? n.r * 1.4 : n.r)) * k + 4;
    if (sx < -80 || sx > MG.w + 80 || sy < -20 || sy > MG.h + 20) continue;
    const t = n.kind === 'fact' && n.label.length > 34 ? n.label.slice(0, 32) + '…' : n.label;
    x.font = (n.kind === 'fact' ? '500 11px ' : '600 12.5px ') + 'Sora, system-ui, sans-serif';
    x.globalAlpha = lit(n) ? 1 : .3;
    x.fillStyle = n.kind === 'fact' ? 'rgba(226,232,240,.8)' : '#f1f0f7';
    x.lineWidth = 3; x.lineJoin = 'round'; x.strokeStyle = 'rgba(0,0,0,.55)';
    x.strokeText(t, sx, sy); x.fillText(t, sx, sy);
  }
  x.globalAlpha = 1;
}
function memCard(){
  const box = MG.el.querySelector('.mg-card'), j = MG.info || { count: 0 };
  const n = MG.sel && MG.byId[MG.sel];
  const day = ts => new Date(ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
  const factRow = f => `<div class="mg-fact"><span>${esc(f.label)}</span><button class="mg-x" data-mgf="${esc(f.fid)}" aria-label="Forget">${XI.trash}</button></div>`;
  if (!n){
    box.innerHTML = j.count
      ? `<div class="mg-h">What it knows</div><div class="mg-d">${j.count} thing${j.count === 1 ? '' : 's'} it learned from your chats, in ${j.topics} topic${j.topics === 1 ? '' : 's'}. Tap a dot to read it, or delete anything you'd rather it forgot.</div>
         <div class="mg-chips">${MG.nodes.filter(m => m.kind === 'topic').sort((a, b) => b.n - a.n).map(t => `<button data-mgsel="${esc(t.id)}">${esc(t.label)} <i>${t.n}</i></button>`).join('')}</div>
         <button class="btn" data-mgclear>Forget everything</button>`
      : `<div class="mg-h">Nothing yet</div><div class="mg-d">Tell the chat about yourself - the games you play, your setup, people and plans - and it'll remember, and show up here.</div>`;
    return;
  }
  if (n.kind === 'topic'){
    const fs = [...MG.adj[n.id]].map(k => MG.byId[k]).filter(m => m.kind === 'fact');
    box.innerHTML = `<div class="mg-h">${esc(n.label)} <i>${fs.length}</i></div><div class="mg-list">${fs.map(factRow).join('')}</div>`;
  } else {
    box.innerHTML = `<div class="mg-h small">Saved ${day(n.ts)}</div><div class="mg-big">${esc(n.label)}</div>
      <div class="mg-chips">${n.topics.map(t => `<button data-mgsel="${esc('t:' + t.toLowerCase())}">${esc(t)}</button>`).join('')}</div>
      <button class="btn danger" data-mgf="${esc(n.fid)}">Forget this</button>`;
  }
}

/* ================= boot ================= */
async function loadLayout(){
  L = await api('/api/layout');
  return L;
}

async function poll(){
  try {
    if (draggingSlider || Date.now() < suppressUntil) return;
    S = await api('/api/state');
    if (S.timer && S.timer.on) S._skew = Date.now() / 1000 - (S.timer.at - S.timer.left);
    mxPoll(); wnPoll();
    $('#dot').className = 'dot';
    // The alias (if you set one) wins over the machine's hostname here too.
    $('#meta').textContent = S.time + ' · ' + pcsCurrentName(S.device);
    refresh();          // in place - never a full rebuild, or it flickers
    if (NP.open) npFullDraw();
  } catch(e){ $('#dot').className = 'dot off'; }
}

(async function boot(){
  sizeGrid();
  await sfOnLaunch();          // Face ID / PIN first, when it's set up
  await loadPlatform();
  setupTabs();
  chLoad().then(() => { if (L) refresh(); });
  try {
    await loadLayout();
    S = await api('/api/state');
    render();
    Promise.all([mxPoll(), wnPoll(true)]).then(() => refresh());
  } catch(e){
    board.innerHTML = `<div style="padding:40px 10px;text-align:center;color:var(--muted)">
      ${esc(e.message)}</div>`;
  }
  pcsImport();
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

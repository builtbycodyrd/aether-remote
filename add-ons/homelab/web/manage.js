/* Aether Homelab - the manage page (a computer's browser).
 * Same login and PIN as the phone. Changing what the phone can control asks
 * for the PIN; the server enforces that, this page just asks nicely. */
'use strict';
const $ = s => document.querySelector(s);
const esc = x => String(x == null ? '' : x).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let M = null;                 // /api/manage
let svcs = [];                // the services list being edited

function msg(id, text, ok){
  const el = $(id);
  el.textContent = text || '';
  el.className = 'msg ' + (text ? (ok ? 'ok' : 'err') : '');
}

/* PIN prompt: resolves with the PIN, rejects on Cancel. */
function askPin(title){
  return new Promise((resolve, reject) => {
    $('#pinTitle').textContent = title;
    $('#pinIn').value = ''; $('#pinErr').textContent = '';
    $('#pin').hidden = false;
    setTimeout(() => $('#pinIn').focus(), 50);
    const done = (fn, v) => { $('#pin').hidden = true; cleanup(); fn(v); };
    const go = () => done(resolve, $('#pinIn').value);
    const cancel = () => done(reject, new Error('Cancelled'));
    const key = e => { if (e.key === 'Enter') go(); if (e.key === 'Escape') cancel(); };
    function cleanup(){
      $('#pinGo').removeEventListener('click', go);
      $('#pinCancel').removeEventListener('click', cancel);
      $('#pinIn').removeEventListener('keydown', key);
    }
    $('#pinGo').addEventListener('click', go);
    $('#pinCancel').addEventListener('click', cancel);
    $('#pinIn').addEventListener('keydown', key);
  });
}

async function raw(path, body, opts){
  const o = { method: body === undefined ? 'GET' : 'POST', headers: {}, cache: 'no-store', ...(opts || {}) };
  if (body !== undefined && !(body instanceof Blob)){
    o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body);
  } else if (body instanceof Blob){ o.body = body; o.headers['Content-Type'] = body.type || 'application/octet-stream'; }
  const r = await fetch(path, o);
  let j = {};
  try { j = await r.json(); } catch(e){}
  return { r, j };
}

/* Every call goes through here: signed out -> login; locked -> PIN to
   unlock; a protected change -> PIN for that one change. */
async function api(path, body, tries){
  tries = tries || 0;
  const { r, j } = await raw(path, body);
  if (r.status === 401){ location.href = '/login?next=/manage'; throw new Error('signed out'); }
  if (r.status === 403 && j.error === 'locked' && tries < 3){
    const pin = await askPin('Homelab is locked - enter your PIN');
    const v = await raw('/api/sf/verify', { purpose: 'unlock', pin });
    if (!v.r.ok) $('#pinErr').textContent = v.j.error || 'Wrong PIN';
    return api(path, body, tries + 1);
  }
  if (r.status === 403 && j.error === 'stepup' && tries < 3){
    const pin = await askPin('Enter your PIN to confirm');
    const v = await raw('/api/sf/verify', { purpose: 'stepup', scope: j.scope, pin });
    if (!v.r.ok) throw new Error(v.j.error || 'Wrong PIN');
    return api(path, { ...(body || {}), stepup: v.j.stepup }, tries + 1);
  }
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

function draw(){
  const m = M;
  $('#sub').textContent = 'node ' + (m.node || '?') + ' · version ' + m.version;
  $('#px').innerHTML = m.proxmox.ok
    ? `<span class="dot on" style="display:inline-block;margin-right:7px"></span>Connected to
       <b>${esc(m.proxmox.host)}</b> as <code>${esc(m.proxmox.token)}</code>
       - start/stop and read-only, nothing more.`
    : `<span class="dot bad" style="display:inline-block;margin-right:7px"></span>
       Can't reach Proxmox: ${esc(m.proxmox.error || 'no answer yet')}.
       If you think the token leaked or stopped working, run the installer again and pick <b>t</b>.`;

  document.querySelector(`input[name=gmode][value=${m.guests_mode}]`).checked = true;
  $('#guests').innerHTML = m.guests.map(g => `
    <label class="row" style="cursor:pointer">
      <input type="checkbox" data-vmid="${g.vmid}" ${g.shown ? 'checked' : ''}
        ${m.guests_mode === 'all' ? 'disabled' : ''}>
      <span class="dot ${g.status === 'running' ? 'on' : ''}"></span>
      <span class="grow">${esc(g.name)}</span>
      <span class="muted">${g.type === 'lxc' ? 'CT' : 'VM'} ${g.vmid}</span></label>`).join('')
    || '<div class="muted">No guests found.</div>';

  svcs = (m.services || []).map(s => ({ ...s }));
  drawSvcs();
  $('#containers').value = m.containers === 'all' ? 'all' : (m.containers || []).join(', ');

  $('#links').innerHTML = m.links.map(l => `
    <div class="row">
      ${l.art ? `<img class="pic" src="/api/art?id=link:${l.id}&t=${Date.now()}" alt="">`
              : `<div class="pic">${esc((l.name[0] || '?').toUpperCase())}</div>`}
      <div class="grow"><div style="font-size:13.5px">${esc(l.name)}</div>
        <div class="muted" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(l.url)}</div></div>
      <button data-pic="${l.id}">${l.art ? 'Change picture' : 'Add picture'}</button>
      <button data-edit="${l.id}">Edit</button>
      <button class="danger" data-del="${l.id}">Remove</button>
    </div>`).join('') || '<div class="muted">No links yet.</div>';

  const u = m.update || {};
  $('#upd').innerHTML = u.newer
    ? `Version <b>${esc(u.latest)}</b> is out - you're on ${esc(u.current)}.
       <button class="pri" id="updGo" style="margin-left:8px">Update now</button>`
    : `You're on the newest version (${esc(u.current)}).${u.error ? ' <span style="color:var(--bad)">Last check failed: ' + esc(u.error) + '</span>' : ''}
       <button id="updCheck" style="margin-left:8px">Check now</button>`;
  $('#auto').checked = !!m.auto_update;

  const sf = m.sf || {};
  $('#sec').innerHTML = sf.enabled
    ? 'PIN is <b>on</b> - asked when the app opens and for every VM shut down, reboot or force stop.'
    : 'PIN is <b>off</b>. The first time you shut down a VM from the phone it asks you to pick one.';
}

function drawSvcs(){
  $('#svcs').innerHTML = svcs.map((s, i) => `
    <div class="row"><span class="grow"><code>${esc(s.unit)}</code>
      <span class="muted">${s.label && s.label !== s.unit ? esc(s.label) : ''}</span></span>
      <button class="danger" data-rmsvc="${i}">Remove</button></div>`).join('')
    || '<div class="muted">None.</div>';
}

async function load(){
  M = await api('/api/manage');
  draw();
}

document.addEventListener('change', e => {
  if (e.target.name === 'gmode'){
    const all = e.target.value === 'all';
    document.querySelectorAll('#guests input').forEach(c => { c.disabled = all; if (all) c.checked = true; });
  }
});

document.addEventListener('click', async e => {
  const t = e.target;
  try {
    if (t.id === 'svcAdd'){
      const unit = $('#svcUnit').value.trim();
      if (!unit) return;
      svcs.push({ unit, label: $('#svcLabel').value.trim() || unit });
      $('#svcUnit').value = ''; $('#svcLabel').value = '';
      drawSvcs(); msg('#showMsg', 'Not saved yet - press Save.', true);
    }
    if (t.dataset.rmsvc !== undefined){ svcs.splice(+t.dataset.rmsvc, 1); drawSvcs();
      msg('#showMsg', 'Not saved yet - press Save.', true); }

    if (t.id === 'showSave'){
      const mode = document.querySelector('input[name=gmode]:checked').value;
      const guests = mode === 'all' ? 'all'
        : [...document.querySelectorAll('#guests input:checked')].map(c => +c.dataset.vmid);
      const cv = $('#containers').value.trim();
      const containers = cv.toLowerCase() === 'all' ? 'all'
        : cv.split(',').map(x => x.trim()).filter(Boolean);
      await api('/api/manage/show', { guests, services: svcs, containers });
      msg('#showMsg', 'Saved.', true);
      await load();
    }

    if (t.id === 'lAdd'){
      await api('/api/manage/links', { op: 'add', name: $('#lName').value, url: $('#lUrl').value });
      $('#lName').value = ''; $('#lUrl').value = '';
      msg('#lMsg', 'Added - now add it as a tile from the phone.', true);
      await load();
    }
    if (t.dataset.del){
      if (!confirm('Remove this link? Its tiles disappear from the phone too.')) return;
      await api('/api/manage/links', { op: 'delete', id: t.dataset.del });
      await load();
    }
    if (t.dataset.edit){
      const l = M.links.find(x => x.id === t.dataset.edit);
      const name = prompt('Name', l.name); if (name === null) return;
      const url = prompt('Address', l.url); if (url === null) return;
      await api('/api/manage/links', { op: 'edit', id: l.id, name, url });
      await load();
    }
    if (t.dataset.pic){
      const pk = $('#picker');
      pk.value = '';
      pk.onchange = async () => {
        const f = pk.files[0];
        if (!f) return;
        if (f.size > 512 * 1024){ msg('#lMsg', 'Pictures up to 512 KB.'); return; }
        const { r, j } = await raw('/api/manage/art?id=link:' + t.dataset.pic, f);
        if (!r.ok){ msg('#lMsg', j.error || 'Upload failed'); return; }
        msg('#lMsg', 'Picture saved.', true);
        await load();
      };
      pk.click();
    }

    if (t.id === 'updCheck'){ await api('/api/update/check', {}); await load(); }
    if (t.id === 'updGo'){
      await api('/api/update/install', {});
      msg('#updMsg', 'Updating - this page reloads when it’s back.', true);
      const want = M.update.latest;
      for (let i = 0; i < 90; i++){
        await new Promise(r => setTimeout(r, 2000));
        try { const s = await raw('/api/manage'); if (s.r.ok && s.j.version === want){ location.reload(); return; } } catch(err){}
      }
      location.reload();
    }
    if (t.id === 'signout'){
      if (!confirm('Sign out every phone (and this browser)? Each one needs the 6-digit code again.')) return;
      await api('/api/manage/signout-all', {});
      location.href = '/login?next=/manage';
    }
  } catch(err){
    if (err.message === 'Cancelled') return;
    const box = t.closest('.card');
    const m = box && box.querySelector('.msg');
    if (m){ m.textContent = err.message; m.className = 'msg err'; }
    else alert(err.message);
  }
});

$('#auto').addEventListener('change', async e => {
  try { await api('/api/manage/auto', { on: e.target.checked });
        msg('#updMsg', e.target.checked ? 'Automatic updates on.' : 'Automatic updates off.', true); }
  catch(err){ msg('#updMsg', err.message); }
});

load().catch(err => { if (err.message !== 'signed out') $('#sub').textContent = err.message; });

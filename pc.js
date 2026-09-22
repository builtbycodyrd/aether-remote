/* pc.js - the desktop app.
 *
 * Deliberately NOT the phone page on a big screen. This does the things a
 * mouse and a 27" monitor are good at and a thumb is not:
 *   - a table of every game and app, with custom artwork you can drag in
 *   - the layout as an editable list, with real dropdowns instead of pinching
 *   - scenes built from dropdowns instead of the phone's prompt() boxes
 *   - pairing, and network settings
 * ...with a live phone preview beside it so you see the result as you work.
 */
'use strict';

let L = null, lib = null, S = null, view = 'library';
const $ = (s) => document.querySelector(s);
const main = $('#main');

function esc(x){
  return String(x == null ? '' : x).replace(/[&<>"]/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function toast(m){
  const t = $('#toast');
  t.textContent = m; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2000);
}
async function api(path, body, method){
  const o = { method: method || (body ? 'POST' : 'GET'), headers:{} };
  if (body){ o.headers['Content-Type']='application/json'; o.body = JSON.stringify(body); }
  const r = await fetch(path, o);
  if (r.status === 401){ location.href = '/login'; throw new Error('logged out'); }
  const j = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

function refreshPreview(){
  const f = $('#pv');
  f.src = '/?t=' + Date.now();
}
$('#reloadPv').addEventListener('click', refreshPreview);

async function saveLayout(quiet){
  L = await api('/api/layout', L);
  if (!quiet) toast('Saved');
  refreshPreview();
}

/* ======================= LIBRARY ======================= */
function viewLibrary(){
  const games = lib.items.filter(i => i.kind === 'game');
  const apps  = lib.items.filter(i => i.kind === 'app');
  const q = (viewLibrary.q || '').toLowerCase();
  const show = (arr) => arr.filter(i => !q || i.name.toLowerCase().includes(q));

  main.innerHTML = `
    <h2>Library</h2>
    <div class="sub">Everything this PC can launch. Drag an image onto a row to
      replace its artwork — handy for the games Steam has no box art for, and
      impossible to do from a phone.</div>

    <div class="card">
      <h3>Add</h3>
      <div style="display:flex;gap:9px;align-items:center;flex-wrap:wrap">
        <button class="btn pri" id="pickApp">Browse for a program…</button>
        <button class="btn" id="rescan">Rescan games</button>
        <div class="muted" style="font-size:12px" id="scanInfo"></div>
      </div>
    </div>

    <div class="card">
      <h3>Search</h3>
      <input id="libq" placeholder="Filter ${lib.items.length} items"
        value="${esc(viewLibrary.q || '')}" style="width:100%">
    </div>

    <div class="card">
      <h3>Games — ${show(games).length}</h3>
      <table><thead><tr><th style="width:44px"></th><th>Name</th>
        <th style="width:110px">Source</th><th style="width:230px">Artwork</th></tr></thead>
        <tbody>${show(games).map(rowHTML).join('')}</tbody></table>
    </div>

    <div class="card">
      <h3>Apps — ${show(apps).length}</h3>
      <table><thead><tr><th style="width:44px"></th><th>Name</th>
        <th style="width:110px">Source</th><th style="width:230px">Artwork</th></tr></thead>
        <tbody>${show(apps).slice(0, q ? 400 : 60).map(rowHTML).join('')}</tbody></table>
      ${!q && apps.length > 60 ? `<div class="muted" style="font-size:12px;
        padding:10px 10px 0">Showing 60 of ${apps.length}. Search to narrow.</div>` : ''}
    </div>`;

  $('#libq').addEventListener('input', e => {
    viewLibrary.q = e.target.value;
    const pos = e.target.selectionStart;
    viewLibrary();
    const el = $('#libq'); el.focus(); el.setSelectionRange(pos, pos);
  });
  $('#pickApp').addEventListener('click', pickApp);
  $('#rescan').addEventListener('click', async () => {
    toast('Rescanning…');
    lib = await api('/api/library?rescan=1');
    viewLibrary();
    toast(lib.total + ' found');
  });
  wireArtDrops();
}

function rowHTML(i){
  return `<tr data-id="${esc(i.id)}">
    <td><img class="ic" src="/api/art?id=${encodeURIComponent(i.id)}&t=${Date.now()}"
         onerror="this.style.visibility='hidden'"></td>
    <td>${esc(i.name)}</td>
    <td><span class="pill">${esc(i.source)}</span></td>
    <td>
      <div class="drop" data-drop="${esc(i.id)}"
        style="padding:7px 10px;font-size:11.5px;border-width:1px">
        Drop an image, or click
      </div>
    </td></tr>`;
}

function wireArtDrops(){
  main.querySelectorAll('[data-drop]').forEach(d => {
    const id = d.dataset.drop;
    d.addEventListener('dragover', e => { e.preventDefault(); d.classList.add('over'); });
    d.addEventListener('dragleave', () => d.classList.remove('over'));
    d.addEventListener('drop', async e => {
      e.preventDefault(); d.classList.remove('over');
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) await uploadArt(id, f);
    });
    d.addEventListener('click', () => {
      const inp = document.createElement('input');
      inp.type = 'file'; inp.accept = 'image/*';
      inp.onchange = () => { if (inp.files[0]) uploadArt(id, inp.files[0]); };
      inp.click();
    });
  });
}

async function uploadArt(id, file){
  if (!/^image\//.test(file.type)){ toast('That is not an image'); return; }
  if (file.size > 8 * 1024 * 1024){ toast('Too big — keep it under 8 MB'); return; }
  const b64 = await new Promise(res => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(',')[1]);
    r.readAsDataURL(file);
  });
  try {
    await api('/api/art/custom', { id, data: b64, name: file.name });
    toast('Artwork updated');
    viewLibrary();
    refreshPreview();
  } catch(e){ toast(e.message); }
}

async function pickApp(){
  try {
    const r = await api('/api/pickapp', {});
    if (r.cancelled){ return; }
    lib = await api('/api/library?rescan=1');
    viewLibrary();
    toast(r.item.name + ' added');
    refreshPreview();
  } catch(e){ toast(e.message); }
}

/* ======================= LAYOUT ======================= */
const SIZES = [[1,1],[2,1],[4,1],[2,2],[2,3],[4,2],[4,3]];

/* Tiles you can add that DON'T come from the library. Games and apps are
   added by dragging artwork in the Library tab (they carry a launch command
   the server has to have scanned); these are the built-in ones - live stats,
   the desktop preview, volume, and the safe actions. Each is "kind:ref" with
   a default label and size. */
const ADDABLE = [
  ['stat:cpu',        'CPU',            2, 1],
  ['stat:gpu',        'GPU',            2, 1],
  ['stat:ram',        'RAM',            2, 1],
  ['stat:disk',       'Disk',           2, 1],
  ['stat:temp',       'Temperature',    2, 1],
  ['stat:battery',    'Battery',        2, 1],
  ['nowplaying:',     'Now playing',    4, 2],
  ['stream:0',        'Desktop preview',4, 2],
  ['slider:volume',   'Volume',         4, 1],
  ['toggle:mute',     'Mute',           1, 1],
  ['toggle:keeper',   'Lock volume',    2, 1],
  ['action:power.lock',      'Lock PC',    2, 1],
  ['action:media.playpause', 'Play/Pause', 1, 1],
  ['action:screen.off',      'Screen off', 1, 1],
];

// Which tile (if any) has its pre-launch actions open for editing.
let actEdit = null;
// The steps that make sense to run BEFORE launching a game/app - the audio
// setup and a wait. (Scenes get the full catalogue; this is the useful subset
// for "launch at a set volume".)
const PRE_OPS = ['volume', 'mute', 'unmute', 'keeper', 'wait'];

function viewLayout(){
  main.innerHTML = `
    <h2>Layout</h2>
    <div class="sub">The same tiles your phone shows, in the same order. Drag to
      reorder, pick a size from the dropdown — much easier than pinching a tile
      on a 6-inch screen. Changes appear in the preview immediately.</div>
    <div class="card">
      <h3>Presentation</h3>
      <div style="display:flex;gap:8px">
        ${[['rail','Scroll + rail'],['scroll','Plain scroll'],['pages','Pages']]
          .map(([k,n]) => `<button class="btn ${L.mode===k?'pri':''}"
            data-mode="${k}">${n}</button>`).join('')}
      </div>
    </div>
    ${L.sections.map((s, si) => `
      <div class="card">
        <h3>${esc(s.name)} — ${s.tiles.length} tiles</h3>
        <div data-sec="${si}">
          ${s.tiles.map((t, ti) => `
            <div class="tilerow" draggable="true" data-si="${si}" data-ti="${ti}">
              <span class="grip">⠿</span>
              <span class="nm">${esc(t.label || t.kind)}</span>
              <span class="kind">${esc(t.kind)}</span>
              <select data-size="${si}.${ti}">
                ${SIZES.map(([w,h]) => `<option value="${w}x${h}"
                  ${t.w===w&&t.h===h?'selected':''}>${w} × ${h}</option>`).join('')}
              </select>
              ${(t.kind==='app'||t.kind==='game')
                ? `<button class="btn sm" data-acts="${si}.${ti}">Actions${
                    t.actions&&t.actions.length?' ('+t.actions.length+')':''}</button>`
                : ''}
              <button class="btn sm danger" data-del="${si}.${ti}">Remove</button>
            </div>`).join('') || '<div class="muted" style="font-size:12px">Empty</div>'}
        </div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <select data-addsel="${si}" style="flex-grow:1">
            ${ADDABLE.map(([kr, name]) =>
              `<option value="${kr}">${esc(name)}</option>`).join('')}
          </select>
          <button class="btn sm" data-add="${si}">Add tile</button>
        </div>
      </div>`).join('')}
    <button class="btn" id="addSection">Add a section</button>`;

  main.querySelectorAll('[data-mode]').forEach(b =>
    b.addEventListener('click', async () => {
      L.mode = b.dataset.mode; await saveLayout(true); viewLayout();
    }));

  main.querySelectorAll('[data-size]').forEach(sel =>
    sel.addEventListener('change', async () => {
      const [si, ti] = sel.dataset.size.split('.').map(Number);
      const [w, h] = sel.value.split('x').map(Number);
      L.sections[si].tiles[ti].w = w;
      L.sections[si].tiles[ti].h = h;
      await saveLayout(true);
    }));

  main.querySelectorAll('[data-del]').forEach(b =>
    b.addEventListener('click', async () => {
      const [si, ti] = b.dataset.del.split('.').map(Number);
      L.sections[si].tiles.splice(ti, 1);
      await saveLayout(true); viewLayout();
    }));

  main.querySelectorAll('[data-add]').forEach(b =>
    b.addEventListener('click', async () => {
      const si = +b.dataset.add;
      const sel = main.querySelector(`[data-addsel="${si}"]`);
      const spec = ADDABLE.find(a => a[0] === sel.value);
      if (!spec) return;
      const [kr, name, w, h] = spec;
      const [kind, ref] = kr.split(':');
      L.sections[si].tiles.push({ kind, ref, label: name, w, h });
      await saveLayout(true); viewLayout();
      toast(name + ' added');
    }));

  main.querySelectorAll('[data-acts]').forEach(b =>
    b.addEventListener('click', () => {
      const [si, ti] = b.dataset.acts.split('.').map(Number);
      // Toggle: clicking Actions on the open tile closes it.
      actEdit = (actEdit && actEdit.si === si && actEdit.ti === ti)
        ? null : { si, ti };
      viewLayout();
    }));

  if (actEdit) drawActEditor();

  $('#addSection').addEventListener('click', async () => {
    const name = prompt('Section name', 'New section');
    if (!name) return;
    L.sections.push({ id: 's' + Date.now().toString(36), name, tiles: [] });
    await saveLayout(true); viewLayout();
  });

  wireTileDrag();
}

/* The pre-launch actions for one game/app tile. Reuses the same stepRow the
   scene editor draws, but only offers PRE_OPS and saves onto the tile itself
   rather than a scene. */
function drawActEditor(){
  const sec = L.sections[actEdit.si];
  const t = sec && sec.tiles[actEdit.ti];
  if (!t){ actEdit = null; return; }
  t.actions = t.actions || [];

  const card = document.createElement('div');
  card.className = 'card';
  card.style.borderColor = 'var(--line2)';
  card.innerHTML = `
    <h3>Before launching “${esc(t.label || t.kind)}”</h3>
    <div class="sub" style="margin:0 0 10px">These run in order, then the
      ${esc(t.kind)} opens — handy for “set volume to 40, then launch”.</div>
    <div id="asteps">
      ${t.actions.map((st,i) => stepRow(st,i,[])).join('') ||
        '<div class="muted" style="font-size:12.5px;padding-bottom:8px">No actions yet — the tile just launches.</div>'}
    </div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:10px">
      <select id="newAct">
        ${PRE_OPS.map(op => `<option value="${op}">${STEPS[op].label}</option>`).join('')}
      </select>
      <button class="btn sm" id="addAct">Add action</button>
      <div style="flex-grow:1"></div>
      <button class="btn pri" id="doneAct">Done</button>
    </div>`;
  main.appendChild(card);
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  $('#addAct').addEventListener('click', async () => {
    const op = $('#newAct').value;
    const spec = STEPS[op];
    const st = { op };
    if (spec.arg === 'number') st.value = spec.def;
    if (spec.arg === 'bool') st.value = true;
    t.actions.push(st);
    await saveLayout(true); viewLayout();
  });

  $('#doneAct').addEventListener('click', () => { actEdit = null; viewLayout(); });

  card.querySelectorAll('[data-sv]').forEach(el =>
    el.addEventListener('change', async () => {
      const st = t.actions[+el.dataset.sv];
      const arg = (STEPS[st.op] || {}).arg;
      st.value = arg === 'number' ? Number(el.value)
               : arg === 'bool'   ? el.value === 'true'
               : el.value;
      await saveLayout(true);
    }));

  card.querySelectorAll('[data-sdel]').forEach(b =>
    b.addEventListener('click', async () => {
      t.actions.splice(+b.dataset.sdel, 1);
      await saveLayout(true); viewLayout();
    }));
}

function wireTileDrag(){
  let from = null;
  main.querySelectorAll('.tilerow').forEach(row => {
    row.addEventListener('dragstart', () => {
      from = { si:+row.dataset.si, ti:+row.dataset.ti };
      row.classList.add('drag');
    });
    row.addEventListener('dragend', () => row.classList.remove('drag'));
    row.addEventListener('dragover', e => e.preventDefault());
    row.addEventListener('drop', async e => {
      e.preventDefault();
      if (!from) return;
      const to = { si:+row.dataset.si, ti:+row.dataset.ti };
      const [moved] = L.sections[from.si].tiles.splice(from.ti, 1);
      L.sections[to.si].tiles.splice(to.ti, 0, moved);
      from = null;
      await saveLayout(true);
      viewLayout();
    });
  });
}

/* ======================= SCENES ======================= */
const STEPS = {
  volume:    { label:'Set volume',  arg:'number', def:35, unit:'%' },
  mute:      { label:'Mute' },
  unmute:    { label:'Unmute' },
  keeper:    { label:'Lock volume', arg:'bool' },
  open:      { label:'Open',        arg:'item' },
  close:     { label:'Close',       arg:'item' },
  key:       { label:'Media key',   arg:'choice',
               choices:['playpause','next','prev','stop'] },
  type:      { label:'Type text',   arg:'text' },
  wait:      { label:'Wait',        arg:'number', def:3, unit:'s' },
  screenoff: { label:'Screen off' },
  power:     { label:'Power',       arg:'choice',
               choices:['lock','sleep','signout','restart','shutdown'] },
};

let editingScene = null;

function viewScenes(){
  const scenes = L.scenes || [];
  main.innerHTML = `
    <h2>Scenes</h2>
    <div class="sub">One tap on the phone, several things on the PC. Built here
      with dropdowns rather than the phone's little text prompts.</div>
    <div class="card">
      <h3>Your scenes</h3>
      ${scenes.length ? scenes.map(s => `
        <div class="tilerow" style="cursor:default">
          <span class="nm">${esc(s.name)}</span>
          <span class="kind">${s.steps.length} steps</span>
          <button class="btn sm" data-run="${esc(s.id)}">Test</button>
          <button class="btn sm" data-edit="${esc(s.id)}">Edit</button>
          <button class="btn sm danger" data-rm="${esc(s.id)}">Delete</button>
        </div>`).join('')
        : '<div class="muted" style="font-size:12.5px">None yet.</div>'}
      <button class="btn pri" id="newScene" style="margin-top:12px">New scene</button>
    </div>
    <div id="editor"></div>`;

  main.querySelectorAll('[data-run]').forEach(b => b.addEventListener('click',
    async () => { await api('/api/scene', { run:b.dataset.run }); toast('Running'); }));
  main.querySelectorAll('[data-rm]').forEach(b => b.addEventListener('click',
    async () => { L = await api('/api/scene', { delete:b.dataset.rm }); viewScenes(); }));
  main.querySelectorAll('[data-edit]').forEach(b => b.addEventListener('click',
    () => { editingScene = JSON.parse(JSON.stringify(
      (L.scenes||[]).find(s => s.id === b.dataset.edit))); drawEditor(); }));
  $('#newScene').addEventListener('click', () => {
    editingScene = { id:'sc'+Date.now().toString(36), name:'', steps:[] };
    drawEditor();
  });
  if (editingScene) drawEditor();
}

function drawEditor(){
  const s = editingScene;
  const items = lib ? lib.items : [];
  $('#editor').innerHTML = `
    <div class="card">
      <h3>${s.name ? 'Edit' : 'New'} scene</h3>
      <input id="scName" placeholder="Name it" value="${esc(s.name)}"
        style="width:280px;margin-bottom:14px">
      <div id="steps">
        ${s.steps.map((st, i) => stepRow(st, i, items)).join('') ||
          '<div class="muted" style="font-size:12.5px;padding-bottom:8px">No steps yet.</div>'}
      </div>
      <div style="display:flex;gap:8px;align-items:center;margin-top:12px">
        <select id="newStep">
          ${Object.entries(STEPS).map(([k,v]) =>
            `<option value="${k}">${v.label}</option>`).join('')}
        </select>
        <button class="btn sm" id="addStep">Add step</button>
        <div style="flex-grow:1"></div>
        <button class="btn" id="cancelScene">Cancel</button>
        <button class="btn pri" id="saveScene">Save scene</button>
      </div>
    </div>`;

  $('#addStep').addEventListener('click', () => {
    const op = $('#newStep').value;
    const spec = STEPS[op];
    const st = { op };
    if (spec.arg === 'number') st.value = spec.def;
    if (spec.arg === 'bool') st.value = true;
    if (spec.arg === 'text') st.value = '';
    if (spec.arg === 'choice') st.value = spec.choices[0];
    if (spec.arg === 'item' && items.length){ st.ref = items[0].id; st.value = items[0].name; }
    s.steps.push(st);
    drawEditor();
  });
  $('#cancelScene').addEventListener('click', () => { editingScene = null; viewScenes(); });
  $('#saveScene').addEventListener('click', async () => {
    s.name = $('#scName').value.trim() || 'Scene';
    if (!s.steps.length){ toast('Add at least one step'); return; }
    try {
      L = await api('/api/scene', s);
      editingScene = null;
      viewScenes();
      toast('Scene saved — add it as a tile in Layout');
      refreshPreview();
    } catch(e){ toast(e.message); }
  });

  $('#steps').querySelectorAll('[data-sv]').forEach(el =>
    el.addEventListener('change', () => {
      const i = +el.dataset.sv;
      const st = s.steps[i];
      if (el.dataset.field === 'ref'){
        st.ref = el.value;
        const hit = items.find(x => x.id === el.value);
        st.value = hit ? hit.name : '';
      } else {
        st.value = el.type === 'number' ? Number(el.value) : el.value;
      }
    }));
  $('#steps').querySelectorAll('[data-sdel]').forEach(b =>
    b.addEventListener('click', () => {
      s.steps.splice(+b.dataset.sdel, 1); drawEditor();
    }));
}

function stepRow(st, i, items){
  const spec = STEPS[st.op] || { label: st.op };
  let ctl = '';
  if (spec.arg === 'number')
    ctl = `<input type="number" data-sv="${i}" value="${esc(st.value)}"
             style="width:90px"> <span class="muted">${spec.unit||''}</span>`;
  else if (spec.arg === 'text')
    ctl = `<input data-sv="${i}" value="${esc(st.value||'')}" style="width:220px">`;
  else if (spec.arg === 'choice')
    ctl = `<select data-sv="${i}">${spec.choices.map(c =>
      `<option ${st.value===c?'selected':''}>${c}</option>`).join('')}</select>`;
  else if (spec.arg === 'bool')
    ctl = `<select data-sv="${i}"><option value="true" ${st.value?'selected':''}>on</option>
      <option value="false" ${!st.value?'selected':''}>off</option></select>`;
  else if (spec.arg === 'item')
    ctl = `<select data-sv="${i}" data-field="ref" style="max-width:260px">${
      items.map(x => `<option value="${esc(x.id)}" ${st.ref===x.id?'selected':''}
        >${esc(x.name)}</option>`).join('')}</select>`;

  return `<div class="tilerow" style="cursor:default">
    <span class="muted" style="width:16px">${i+1}</span>
    <span class="nm" style="flex:0 0 120px">${esc(spec.label)}</span>
    <span style="flex-grow:1">${ctl}</span>
    <button class="btn sm danger" data-sdel="${i}">×</button>
  </div>`;
}


/* ======================= PHONES / PAIRING ======================= */
function viewPair(){
  main.innerHTML = `
    <h2>Phones</h2>
    <div class="sub">Scan this on a phone to pair it. It stays signed in for
      30 days; after that it asks for a code again.</div>
    <div class="card" style="display:flex;gap:26px;align-items:center">
      <img id="qr" alt="Pairing QR" width="210" height="210"
        style="border-radius:12px;background:#0b0a18">
      <div style="flex-grow:1">
        <div class="muted" style="font-size:11px;letter-spacing:1px;
          text-transform:uppercase;margin-bottom:6px">Address</div>
        <input id="url" readonly style="width:100%;font-family:ui-monospace,monospace">
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn sm" id="copyUrl">Copy</button>
          <button class="btn sm" id="openIt">Open here</button>
        </div>
        <div class="muted" style="font-size:12px;margin-top:14px;line-height:1.6"
          id="netnote"></div>
      </div>
    </div>

    <div class="card">
      <h3>Security</h3>
      <div class="muted" style="font-size:12.5px;line-height:1.6;margin-bottom:12px">
        Every phone signs in with a 6-digit code from your authenticator.
        Signing out all devices forces them to enter a code again. Resetting the
        secret invalidates the authenticator entry itself — you would re-scan a
        new QR on every phone.
      </div>
      <div style="display:flex;gap:9px">
        <button class="btn" id="signoutAll">Sign out all devices</button>
        <button class="btn danger" id="resetTotp">Reset the authenticator secret</button>
      </div>
    </div>`;

  api('/api/pairinfo').then(p => {
    $('#url').value = p.url;
    $('#qr').src = '/api/qr?t=' + Date.now();
    $('#netnote').innerHTML = p.tailscale
      ? 'Reachable over <b>Tailscale</b>, so this works from anywhere the phone has signal.'
      : 'Reachable on your <b>Wi-Fi</b>. Install Tailscale on both devices if you want it away from home.';
  }).catch(e => toast(e.message));

  $('#copyUrl').addEventListener('click', () => {
    $('#url').select(); document.execCommand('copy'); toast('Copied');
  });
  $('#openIt').addEventListener('click', () => window.open($('#url').value, '_blank'));
  $('#signoutAll').addEventListener('click', async () => {
    if (!confirm('Sign out every phone? They will need a code to get back in.')) return;
    await api('/api/security', { action:'revoke' });
    toast('All devices signed out');
  });
  $('#resetTotp').addEventListener('click', async () => {
    if (!confirm('Reset the secret? Every authenticator entry for this PC stops working and you must re-scan.')) return;
    await api('/api/security', { action:'reset' });
    toast('New secret — re-scan the QR');
    viewPair();
  });
}

/* ======================= SETTINGS ======================= */
function viewSettings(){
  const t = L.theme;
  main.innerHTML = `
    <h2>Settings</h2>
    <div class="sub">Network and appearance. Appearance is shared with the phone.</div>

    <div class="card">
      <h3>Network</h3>
      <label class="row"><div class="t">How the phone reaches this PC
        <div class="d">Auto uses Tailscale when it is running, otherwise your Wi-Fi.</div></div>
        <select id="netmode">
          <option value="auto">Auto</option>
          <option value="tailscale">Tailscale only</option>
          <option value="0.0.0.0">Wi-Fi / LAN only</option>
        </select></label>
      <label class="row"><div class="t">Port
        <div class="d">Change it only if something else already uses 8787.</div></div>
        <input id="port" type="number" min="1024" max="65535" style="width:110px"></label>
      <div style="margin-top:12px;display:flex;gap:9px;align-items:center">
        <button class="btn pri" id="saveNet">Save and restart server</button>
        <span class="muted" style="font-size:12px">Your phone reconnects on its own.</span>
      </div>
    </div>

    <div class="card">
      <h3>Appearance</h3>
      <label class="row"><div class="t">Primary
        <div class="d">Fills, sliders, anything that is on.</div></div>
        <input type="color" id="cPrimary" value="${esc(t.primary)}"></label>
      <label class="row"><div class="t">Secondary
        <div class="d">Gradients, edit mode, highlights.</div></div>
        <input type="color" id="cSecondary" value="${esc(t.secondary)}"></label>
      <label class="row"><div class="t">Background</div>
        <input type="color" id="cBg" value="${esc(t.bg)}"></label>
      <label class="row"><div class="t">Corner radius</div>
        <input type="range" id="radius" min="0" max="26" value="${t.radius}"
          style="width:180px"><span class="muted" id="radiusV">${t.radius}px</span></label>
      <label class="row"><div class="t">Glass panels</div>
        <input type="checkbox" id="glass" ${t.glass?'checked':''}></label>
      <label class="row"><div class="t">Scan lines</div>
        <input type="checkbox" id="scanlines" ${t.scanlines?'checked':''}></label>
      <label class="row"><div class="t">Labels on game art</div>
        <input type="checkbox" id="gameLabels" ${t.gameLabels?'checked':''}></label>
    </div>

    <div class="card">
      <h3>Start with Windows</h3>
      <label class="row"><div class="t">Run Aether Remote at logon
        <div class="d" id="autoNote">—</div></div>
        <input type="checkbox" id="autostart"></label>
    </div>

    <div class="card">
      <h3>Wake on LAN</h3>
      <div class="muted" style="font-size:12.5px;line-height:1.6;margin-bottom:12px">
        Let your phone turn this PC on when it's asleep or off. The phone can't
        reach a sleeping PC directly, so a small always-on device on your network
        — your Raspberry Pi 400 is perfect — sends the wake signal for it.
      </div>
      <label class="row"><div class="t">This PC's network card
        <div class="d" id="wolNic">Looking…</div></div></label>
      <label class="row"><div class="t">MAC address
        <div class="d">The phone stores this so the Pi knows what to wake.</div></div>
        <div style="display:flex;gap:8px;align-items:center">
          <input id="wolMac" readonly style="width:170px;font-family:ui-monospace,monospace">
          <button class="btn sm" id="wolCopy">Copy</button>
        </div></label>
      <details style="margin-top:12px">
        <summary style="cursor:pointer;font-weight:600">Step 1 — turn on Wake on LAN in Windows</summary>
        <div class="muted" style="font-size:12.5px;line-height:1.7;margin:10px 0 4px">
          Device Manager → Network adapters → your Ethernet card → Properties →
          <b>Power Management</b>: tick <i>Allow this device to wake the computer</i>
          and <i>Only allow a magic packet…</i>. On the <b>Advanced</b> tab, set
          <i>Wake on Magic Packet</i> to Enabled. Many PCs also need
          <i>Wake-on-LAN</i> (or ErP/Deep Sleep off) enabled in the BIOS.
          Wake works best over <b>wired Ethernet</b>.
        </div>
      </details>
      <details style="margin-top:8px">
        <summary style="cursor:pointer;font-weight:600">Step 2 — set up the Raspberry Pi sender</summary>
        <div class="muted" style="font-size:12.5px;line-height:1.7;margin:10px 0 4px">
          On the Pi (same network as this PC), save the script and run it as a
          service. Then in the phone app's PC switcher, put the Pi's address in
          the PC's <i>Wake sender</i> field. The phone's Wake button calls the Pi.
          <ol style="margin:10px 0 0 18px;padding:0;line-height:1.9">
            <li>Download <code>aether-wake.py</code> below to the Pi
              (e.g. <code>/home/pi/aether-wake.py</code>).</li>
            <li>Create the service:
              <code>sudo nano /etc/systemd/system/aether-wake.service</code>
              and paste the unit shown below.</li>
            <li><code>sudo systemctl enable --now aether-wake</code></li>
            <li>Check it: open <code>http://&lt;pi-ip&gt;:8788/ping</code> in a browser.</li>
          </ol>
        </div>
        <div style="display:flex;gap:8px;margin:12px 0 8px">
          <button class="btn sm" id="wolDl">Download aether-wake.py</button>
          <button class="btn sm" id="wolUnit">Copy the service unit</button>
        </div>
        <pre id="wolUnitBox" style="display:none;white-space:pre-wrap;background:#0b0a18;
          border-radius:10px;padding:12px;font-size:11.5px;overflow:auto"></pre>
      </details>
    </div>

    <div class="card">
      <h3>Version</h3>
      <label class="row"><div class="t"><span id="verLine">—</span>
        <div class="d" id="verNote">Checking…</div></div>
        <button class="btn sm" id="verCheck">Check for updates</button></label>
    </div>`;

  api('/api/settings').then(s => {
    $('#netmode').value = s.host;
    $('#port').value = s.port;
    $('#autostart').checked = !!s.autostart;
    $('#autoNote').textContent = s.autostartDetail || '';
  });

  // ---- Wake on LAN ----
  const wolUnit =
    '[Unit]\n' +
    'Description=Aether wake sender\n' +
    'After=network-online.target\n' +
    'Wants=network-online.target\n\n' +
    '[Service]\n' +
    'ExecStart=/usr/bin/python3 /home/pi/aether-wake.py\n' +
    'Restart=always\n' +
    'User=pi\n\n' +
    '[Install]\n' +
    'WantedBy=multi-user.target\n';
  api('/api/wol/info').then(w => {
    const p = w && w.primary;
    if (!$('#wolMac')) return;               // view changed while we waited
    if (p){
      $('#wolNic').textContent = p.desc || p.name || 'Ethernet';
      $('#wolMac').value = p.mac || '';
    } else {
      $('#wolNic').textContent = 'No wired network card found.';
    }
  }).catch(() => { const n = $('#wolNic'); if (n) n.textContent = 'Could not read the network card.'; });
  $('#wolCopy').addEventListener('click', () => {
    $('#wolMac').select(); document.execCommand('copy'); toast('MAC copied');
  });
  $('#wolDl').addEventListener('click', async () => {
    try {
      const r = await fetch('/api/wol/piscript');
      const txt = await r.text();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([txt], { type: 'text/x-python' }));
      a.download = 'aether-wake.py';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    } catch(e){ toast('Download failed'); }
  });
  $('#wolUnit').addEventListener('click', () => {
    const box = $('#wolUnitBox');
    box.textContent = wolUnit;
    box.style.display = 'block';
    const sel = document.getSelection(), rng = document.createRange();
    rng.selectNodeContents(box); sel.removeAllRanges(); sel.addRange(rng);
    document.execCommand('copy'); sel.removeAllRanges();
    toast('Service unit copied');
  });

  // The way back in after someone has hidden a version with the X.
  const verPaint = () => {
    const s = U.s;
    const line = $('#verLine'), note = $('#verNote');
    if (!line) return;                       // view changed while we waited
    if (!s){
      line.textContent = 'Aether Remote';
      note.textContent = 'Could not reach GitHub to check.';
      return;
    }
    line.textContent = 'Aether Remote ' + s.current;
    note.textContent =
      s.error ? 'Could not check: ' + s.error
      : !s.newer ? 'This is the latest version.'
      : s.available ? s.latest + ' is available.'
      : s.latest + ' is available — you chose to hide it.';
    $('#verCheck').textContent = s.newer && !s.available
      ? 'Show it again' : 'Check for updates';
  };
  verPaint();
  (U.s ? Promise.resolve(U.s) : updLoad()).then(verPaint);

  $('#verCheck').addEventListener('click', async () => {
    const btn = $('#verCheck');
    btn.disabled = true;
    btn.textContent = 'Checking…';
    if (U.s && U.s.newer && !U.s.available){
      try { await api('/api/update/skip', { clear: true }); } catch(e){}
    }
    await updLoad(true);
    btn.disabled = false;
    verPaint();
    if (U.s && !U.s.newer) toast("You're up to date");
  });

  const push = async () => {
    L.theme.primary = $('#cPrimary').value;
    L.theme.secondary = $('#cSecondary').value;
    L.theme.bg = $('#cBg').value;
    L.theme.radius = +$('#radius').value;
    L.theme.glass = $('#glass').checked;
    L.theme.scanlines = $('#scanlines').checked;
    L.theme.gameLabels = $('#gameLabels').checked;
    L.theme.preset = 'custom';
    $('#radiusV').textContent = L.theme.radius + 'px';
    document.documentElement.style.setProperty('--primary', L.theme.primary);
    document.documentElement.style.setProperty('--secondary', L.theme.secondary);
    await saveLayout(true);
  };
  ['cPrimary','cSecondary','cBg','radius','glass','scanlines','gameLabels']
    .forEach(id => $('#' + id).addEventListener('change', push));
  $('#radius').addEventListener('input',
    () => { $('#radiusV').textContent = $('#radius').value + 'px'; });

  $('#saveNet').addEventListener('click', async () => {
    try {
      await api('/api/settings', { host: $('#netmode').value,
                                   port: +$('#port').value });
      toast('Saved — restarting the server');
      setTimeout(refreshPreview, 4000);
    } catch(e){ toast(e.message); }
  });
  $('#autostart').addEventListener('change', async e => {
    try {
      const r = await api('/api/settings', { autostart: e.target.checked });
      $('#autoNote').textContent = r.autostartDetail || '';
      toast(e.target.checked ? 'Will start with Windows' : 'Will not start automatically');
    } catch(err){ toast(err.message); e.target.checked = !e.target.checked; }
  });
}

/* ======================= UPDATES =======================
 * The whole point is that someone who does not think about software updates
 * still gets them. So: a bar, three buttons, plain words. "More info" shows
 * what changed, the X hides this version for good, and nothing downloads
 * until one of those buttons is pressed.
 */
const U = { s: null, busy: false };

async function updLoad(force){
  try {
    U.s = force ? await api('/api/update/check', {}) : await api('/api/update');
  } catch(e){ U.s = null; }
  updPaint();
  return U.s;
}

function updPaint(){
  const bar = $('#updbar'), s = U.s;
  if (U.busy) return;                       // mid-install: leave the text be
  if (!s || !s.available){ bar.classList.add('hide'); return; }
  const mb = s.asset_size ? (s.asset_size / 1048576).toFixed(1) + ' MB' : '';
  $('#ubTitle').textContent = 'Version ' + s.latest + ' is available';
  $('#ubSub').textContent = "You're on " + s.current +
    (mb ? ' · ' + mb + ' download' : '');
  $('#ubActions').classList.remove('hide');
  bar.classList.remove('hide');
}

/* Release notes are Markdown written by a human on GitHub. Escape first,
 * then put back only the handful of tags worth having - the text never gets
 * to bring its own HTML. */
function updNotes(md){
  const inline = (t) => esc(t)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[(.+?)\]\((https?:\/\/[^\s)]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener">$1</a>');
  let html = '', li = null, inList = false;
  const closeLi = () => {
    if (li !== null){ html += '<li>' + li + '</li>'; li = null; }
  };
  const closeList = () => {
    closeLi();
    if (inList){ html += '</ul>'; inList = false; }
  };

  for (const raw of String(md || '').replace(/\r/g, '').split('\n')){
    const l = raw.trim();
    const bullet = l.match(/^[-*]\s+(.*)$/);
    if (bullet){
      closeLi();
      if (!inList){ html += '<ul>'; inList = true; }
      li = inline(bullet[1]);
      continue;
    }
    // An indented line under a bullet is that bullet wrapping, not a new
    // paragraph. Release notes are written in an editor that wraps, so
    // without this a long bullet breaks the list in half.
    if (li !== null && l && /^\s/.test(raw)){
      li += ' ' + inline(l);
      continue;
    }
    closeList();
    if (!l) continue;
    const h = l.match(/^#{1,6}\s+(.*)$/);
    html += h ? '<h4>' + inline(h[1]) + '</h4>' : '<p>' + inline(l) + '</p>';
  }
  closeList();
  return html || '<p class="muted">No notes were published for this release.</p>';
}

function updModal(){
  const s = U.s;
  if (!s) return;
  const when = s.published
    ? ' · released ' + new Date(s.published).toLocaleDateString() : '';
  const m = document.createElement('div');
  m.className = 'mask';
  m.innerHTML = `
    <div class="modal">
      <header>
        <h3>${esc(s.name || ('Version ' + s.latest))}</h3>
        <div class="d">You're on ${esc(s.current)} — ${esc(s.latest)} is
          available${esc(when)}</div>
      </header>
      <div class="body">${updNotes(s.notes)}</div>
      <footer>
        ${s.page ? `<a class="btn sm" href="${esc(s.page)}" target="_blank"
           rel="noopener">View on GitHub</a>` : ''}
        <div class="sp"></div>
        <button class="btn sm" data-x>Not now</button>
        <button class="btn sm pri" data-go>Update now</button>
      </footer>
    </div>`;
  document.body.appendChild(m);
  const close = () => { m.remove(); document.removeEventListener('keydown', key); };
  const key = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', key);
  m.addEventListener('click', e => { if (e.target === m) close(); });
  m.querySelector('[data-x]').addEventListener('click', close);
  m.querySelector('[data-go]').addEventListener('click', () => {
    close(); updInstall();
  });
}

async function updInstall(){
  if (U.busy) return;
  U.busy = true;
  const v = U.s ? U.s.latest : '';
  $('#updbar').classList.remove('hide');
  $('#ubActions').classList.add('hide');
  $('#ubTitle').textContent = 'Downloading version ' + v + '…';
  $('#ubSub').textContent = 'Your layout, artwork and paired phones are ' +
    'kept — only the program itself is replaced.';
  try {
    await api('/api/update/install', {});
    $('#ubTitle').textContent = 'Installing version ' + v + '…';
    $('#ubSub').textContent = 'Aether Remote closes and reopens itself. ' +
      'This page comes back on its own.';
    updWaitForRestart();
  } catch(e){
    U.busy = false;
    $('#ubActions').classList.remove('hide');
    updPaint();
    toast('Update failed: ' + e.message);
  }
}

/* The server is about to be killed and restarted under us. Wait for it to go
 * down FIRST - otherwise the very first poll succeeds against the old copy
 * and we reload into the version we were trying to leave. */
function updWaitForRestart(){
  let tries = 0, wentDown = false;
  const poll = setInterval(async () => {
    tries++;
    let up = false;
    try { up = (await fetch('/ping', { cache: 'no-store' })).ok; }
    catch(e){ up = false; }
    if (!up) wentDown = true;
    else if (wentDown){
      clearInterval(poll);
      $('#ubTitle').textContent = 'Updated — reloading';
      setTimeout(() => location.reload(), 1200);
      return;
    }
    if (tries > 75){
      clearInterval(poll);
      $('#ubSub').textContent = 'Still going. If this stays here, open ' +
        'Aether Remote from the Start menu.';
    }
  }, 2000);
}

$('#ubInfo').addEventListener('click', updModal);
$('#ubGo').addEventListener('click', updInstall);
$('#ubSkip').addEventListener('click', async () => {
  // No state means the bar should not have been on screen at all; hide it
  // rather than telling the server to dismiss a version we cannot name.
  if (!U.s){ $('#updbar').classList.add('hide'); return; }
  try { U.s = await api('/api/update/skip', { version: U.s.latest }); } catch(e){}
  updPaint();
  toast('Hidden until there is a newer version');
  if (view === 'settings') VIEWS.settings();
});

/* ======================= shell ======================= */
const VIEWS = { library: viewLibrary, layout: viewLayout, scenes: viewScenes,
                pair: viewPair, settings: viewSettings };

document.querySelectorAll('[data-view]').forEach(n =>
  n.addEventListener('click', () => {
    view = n.dataset.view;
    document.querySelectorAll('[data-view]').forEach(x =>
      x.classList.toggle('on', x === n));
    VIEWS[view]();
  }));

async function tick(){
  try {
    S = await api('/api/state');
    $('#sysstat').innerHTML =
      `<b>${esc(S.device)}</b><br>Volume ${S.volume}%${S.muted ? ' · muted' : ''}` +
      `<br>RAM ${S.memory.usedGb} / ${S.memory.totalGb} GB` +
      (S.foreground && S.foreground.title
        ? `<br>Front: ${esc(S.foreground.title.slice(0, 22))}` : '');
  } catch(e){ /* the sidebar stat is cosmetic */ }
}

(async function boot(){
  try {
    [L, lib] = await Promise.all([api('/api/layout'), api('/api/library')]);
    const s = await api('/api/settings').catch(() => null);
    if (s && s.pc) $('#pcname').textContent = s.pc;
    document.documentElement.style.setProperty('--primary', L.theme.primary);
    document.documentElement.style.setProperty('--secondary', L.theme.secondary);
    VIEWS[view]();
    tick();
    setInterval(tick, 3000);
    // Cheap: the server answers from a file and only talks to GitHub once
    // a day. Re-asked hourly so a long-running window notices a release.
    updLoad();
    setInterval(updLoad, 60 * 60 * 1000);
  } catch(e){
    main.innerHTML = `<h2>Cannot reach the server</h2>
      <div class="sub">${esc(e.message)}</div>`;
  }
})();

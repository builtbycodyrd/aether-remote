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
              <button class="btn sm danger" data-del="${si}.${ti}">Remove</button>
            </div>`).join('') || '<div class="muted" style="font-size:12px">Empty</div>'}
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

  $('#addSection').addEventListener('click', async () => {
    const name = prompt('Section name', 'New section');
    if (!name) return;
    L.sections.push({ id: 's' + Date.now().toString(36), name, tiles: [] });
    await saveLayout(true); viewLayout();
  });

  wireTileDrag();
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
    </div>`;

  api('/api/settings').then(s => {
    $('#netmode').value = s.host;
    $('#port').value = s.port;
    $('#autostart').checked = !!s.autostart;
    $('#autoNote').textContent = s.autostartDetail || '';
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
  } catch(e){
    main.innerHTML = `<h2>Cannot reach the server</h2>
      <div class="sub">${esc(e.message)}</div>`;
  }
})();

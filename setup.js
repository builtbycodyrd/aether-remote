/* Aether Remote - first-run wizard.

   Runs at the PC only, before anyone can log in. The authenticator step
   will not let you past it until a real code from a real phone has been
   accepted: the alternative is handing someone a PC app they are then
   locked out of, which is the one failure you cannot fix from the phone. */

const card = document.getElementById('card');
const bar  = document.getElementById('steps');

const STEPS = ['welcome', 'network', 'auth', 'phone', 'done'];
let step = 0;
let state = {};          // last /api/setup/state
let picked = 'auto';     // chosen network mode

function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

async function api(path, body){
  const opts = body ? {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  } : undefined;
  const r = await fetch('/api/setup/' + path, opts);
  let j = {};
  try { j = await r.json(); } catch(e){}
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

function drawBar(){
  bar.innerHTML = STEPS.map((_, i) =>
    '<i class="' + (i <= step ? 'on' : '') + '"></i>').join('');
}

function go(n){
  step = Math.max(0, Math.min(STEPS.length - 1, n));
  render();
}

function render(){
  drawBar();
  card.innerHTML = '';
  VIEWS[STEPS[step]]();
}

const VIEWS = {};

// ------------------------------------------------------------- welcome

VIEWS.welcome = () => {
  card.innerHTML = `
    <div class="eyebrow">Aether Remote</div>
    <h1>Let&rsquo;s set up ${esc(state.pc || 'this PC')}</h1>
    <p>This turns your phone into a remote for this computer &mdash; volume and
       media, your games and apps, and the whole desktop as a live screen you
       can tap and type on.</p>
    <ul>
      <li>Choose how your phone reaches this PC</li>
      <li>Set up a login code in your authenticator app</li>
      <li>Pair the phone and put it on your home screen</li>
      <li>Decide whether the remote starts with Windows</li>
    </ul>
    <p class="small">About two minutes. Have your phone nearby &mdash; you
       will need it for steps 2 and 3.</p>
    <div class="nav"><span class="sp"></span>
      <button class="btn pri" id="next">Start</button></div>`;
  document.getElementById('next').onclick = () => go(1);
};

// ------------------------------------------------------------- network

VIEWS.network = () => {
  const ts = state.tailscale;
  const opt = (id, title, desc) => `
    <div class="opt${picked === id ? ' sel' : ''}" data-m="${id}">
      <div class="dot"></div>
      <div><div class="t">${title}</div><div class="d">${desc}</div></div>
    </div>`;

  card.innerHTML = `
    <div class="eyebrow">Step 1 of 4</div>
    <h1>How should your phone reach this PC?</h1>
    <p>Whichever you pick, nothing is opened to the internet and no traffic
       goes through anyone else&rsquo;s server. You can change this later in
       the desktop app.</p>
    ${opt('auto', 'Automatic <span style="opacity:.6;font-size:12px">&mdash; recommended</span>',
          'Uses Tailscale when it is running, and your home Wi-Fi otherwise. ' +
          (ts ? 'Tailscale is running right now.'
              : 'No Tailscale on this PC at the moment, so this means Wi-Fi.'))}
    ${opt('tailscale', 'Tailscale only',
          'Works from anywhere &mdash; mobile data, a friend&rsquo;s Wi-Fi &mdash; and the ' +
          'remote is invisible on your local network. Needs the free Tailscale ' +
          'app on both this PC and the phone. The remote waits for it rather ' +
          'than falling back.' +
          (ts ? '' : ' <b>Tailscale is not running right now.</b>'))}
    ${opt('0.0.0.0', 'Home Wi-Fi only',
          'Simplest. The phone works whenever it is on the same network as ' +
          'this PC, and not when you are out.')}
    <p class="small">A change here applies the next time the remote starts
       &mdash; use Restart in the tray menu if you want it right away.</p>
    <div class="err" id="err"></div>
    <div class="nav">
      <button class="btn" id="back">Back</button><span class="sp"></span>
      <button class="btn pri" id="next">Continue</button>
    </div>`;

  card.querySelectorAll('.opt').forEach(el => {
    el.onclick = () => { picked = el.dataset.m; render(); };
  });
  document.getElementById('back').onclick = () => go(0);
  document.getElementById('next').onclick = async (e) => {
    e.target.disabled = true;
    try {
      await api('network', {host: picked});
      go(2);
    } catch (err) {
      document.getElementById('err').textContent = err.message;
      e.target.disabled = false;
    }
  };
};

// ------------------------------------------------------- authenticator

VIEWS.auth = () => {
  card.innerHTML = `
    <div class="eyebrow">Step 2 of 4</div>
    <h1>Set up your login code</h1>
    <p>Logging in from the phone needs a 6-digit code from an authenticator
       app &mdash; Google Authenticator, Authy, 1Password, or the one built into
       your password manager. Scan this with it.</p>
    <div class="qrbox">
      <img src="/api/setup/qr?of=totp&amp;t=${Date.now()}" alt="QR code">
      <div>
        <p class="small" style="margin-bottom:8px">Can&rsquo;t scan? Add an
           account by hand with this key:</p>
        <code id="secret">loading&hellip;</code>
        <p class="small" style="margin:10px 0 0">Time-based, 6 digits, 30
           seconds &mdash; the defaults in every app.</p>
      </div>
    </div>
    <p>Now type the code it shows, so we know it works before you rely on it:</p>
    <input class="code" id="code" inputmode="numeric" autocomplete="one-time-code"
           maxlength="6" placeholder="000000">
    <div class="err" id="err"></div>
    <div class="nav">
      <button class="btn" id="back">Back</button>
      <button class="btn" id="new" title="Start over with a different key">New key</button>
      <span class="sp"></span>
      <button class="btn pri" id="next" disabled>Verify</button>
    </div>`;

  const code = document.getElementById('code');
  const next = document.getElementById('next');
  const err  = document.getElementById('err');

  api('totp').then(j => {
    document.getElementById('secret').textContent =
      (j.secret || '').replace(/(.{4})/g, '$1 ').trim();
  }).catch(() => {});

  code.focus();
  code.oninput = () => {
    code.value = code.value.replace(/\D/g, '').slice(0, 6);
    next.disabled = code.value.length !== 6;
    err.textContent = '';
    if (code.value.length === 6) verify();
  };
  code.onkeydown = e => { if (e.key === 'Enter' && !next.disabled) verify(); };

  async function verify(){
    next.disabled = true;
    err.className = 'err';
    err.textContent = 'Checking…';
    try {
      await api('verify', {code: code.value});
      err.className = 'err ok';
      err.textContent = 'That worked. Your authenticator is set up.';
      setTimeout(() => go(3), 700);
    } catch (e2) {
      err.textContent = e2.message;
      code.value = '';
      next.disabled = true;
      code.focus();
    }
  }

  next.onclick = verify;
  document.getElementById('back').onclick = () => go(1);
  document.getElementById('new').onclick = async () => {
    // A fresh key, in case the QR was scanned by the wrong phone or the
    // entry got deleted halfway through. Everything already issued dies.
    err.className = 'err';
    err.textContent = '';
    try {
      await api('newsecret', {});
      render();
    } catch (e3) { err.textContent = e3.message; }
  };
};

// ---------------------------------------------------------- pair phone

VIEWS.phone = () => {
  const url = state.url || '';
  card.innerHTML = `
    <div class="eyebrow">Step 3 of 4</div>
    <h1>Point your phone at this</h1>
    <div class="qrbox">
      <img src="/api/setup/qr?of=url&amp;t=${Date.now()}" alt="QR code">
      <div>
        <p class="small" style="margin-bottom:8px">Open the camera, scan, and
           tap the link. Or type it in:</p>
        <code>${esc(url)}</code>
        <p class="small" style="margin:10px 0 0">${
          state.tailscale
            ? 'This is your Tailscale address, so it keeps working when you leave the house.'
            : 'This is your local address &mdash; it works while the phone is on the same Wi-Fi.'
        }</p>
      </div>
    </div>
    <p>The phone will ask for a 6-digit code &mdash; that is the one from the
       app you just set up. After that it walks you through putting the
       remote on your home screen, where it opens like a real app with no
       browser bars.</p>
    <p class="small">Staying logged in lasts 30 days per phone, and you can
       kick a phone off at any time from the desktop app.</p>
    <div class="nav">
      <button class="btn" id="back">Back</button><span class="sp"></span>
      <button class="btn pri" id="next">The phone is in</button>
    </div>`;
  document.getElementById('back').onclick = () => go(2);
  document.getElementById('next').onclick = () => go(4);
};

// ---------------------------------------------------------------- done

VIEWS.done = () => {
  card.innerHTML = `
    <div class="eyebrow">Step 4 of 4</div>
    <h1>One last thing</h1>
    <p>A remote is no use if you have to walk to the PC to start it.</p>
    <div class="check" style="border-bottom:1px solid var(--line)">
      <input type="checkbox" id="auto" ${(!state.done || state.autostart) ? 'checked' : ''}>
      <label for="auto">
        <div>Start Aether Remote when I sign in to Windows</div>
        <div class="d small" style="color:var(--muted);margin-top:2px">
          Sits quietly in the tray. Turn it off any time from there.</div>
      </label>
    </div>
    <div class="err" id="err"></div>
    <div class="nav">
      <button class="btn" id="back">Back</button><span class="sp"></span>
      <button class="btn pri" id="next">Finish setup</button>
    </div>`;

  document.getElementById('back').onclick = () => go(3);
  document.getElementById('next').onclick = async (e) => {
    const err = document.getElementById('err');
    e.target.disabled = true;
    err.className = 'err';
    err.textContent = '';
    try {
      await api('autostart', {on: document.getElementById('auto').checked});
      await api('finish', {});
      finished();
    } catch (e2) {
      err.textContent = e2.message;
      e.target.disabled = false;
    }
  };
};

function finished(){
  step = STEPS.length - 1;
  drawBar();
  card.innerHTML = `
    <div class="done">
      <svg width="54" height="54" viewBox="0 0 24 24" fill="none"
           stroke="var(--good)" stroke-width="1.7" stroke-linecap="round"
           stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"></circle><path d="M8 12.5l2.5 2.5L16 9"></path>
      </svg>
      <h1 style="margin-bottom:8px">You&rsquo;re set</h1>
      <p>${esc(state.pc || 'This PC')} is ready. The desktop app is where you
         pick which games and apps show up, arrange the remote, and change how
         it looks &mdash; and it has a live preview of the phone while you do it.</p>
      <div class="nav" style="justify-content:center">
        <button class="btn pri" id="open">Open the desktop app</button>
      </div>
    </div>`;
  document.getElementById('open').onclick = () => { location.href = '/pc'; };
}

// ---------------------------------------------------------------- boot

(async function boot(){
  try {
    state = await api('state');
  } catch (e) {
    card.innerHTML = '<h1>Can&rsquo;t reach the remote</h1>' +
      '<p>' + esc(e.message) + '</p>' +
      '<p class="small">Setup only answers the PC itself. If you opened this ' +
      'from another device, open it on the PC instead.</p>';
    return;
  }
  picked = ['auto', 'tailscale', '0.0.0.0'].includes(state.host)
    ? state.host : 'auto';
  // Re-running the wizard after it is finished is allowed, but there is no
  // point re-walking someone through a key they already have working.
  if (state.done && state.enrolled) step = 0;
  render();
})();

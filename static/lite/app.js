/* Amelia Lite — client logic.
 *
 * Drives the same HTTP/SSE surface the iOS app uses, so there is no new backend
 * contract to keep in step. Everything is additive; no upstream file is touched.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const state = {
  sid: null, streaming: false, sessions: [], cards: [], es: null,
  model: null, files: [], askBusy: false, skillFilter: 'all', skills: [],
};

/* ---------- transport ---------- */

async function api(path, opts = {}) {
  const r = await fetch(path, {
    credentials: 'same-origin',
    headers: opts.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...opts,
  });
  const txt = await r.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch (_) {}
  if (!r.ok) {
    const e = new Error((data && data.error) || r.statusText || 'request failed');
    e.status = r.status; throw e;
  }
  return data;
}

// Endpoints have grown several shapes upstream. Accept the common ones rather
// than betting on one and silently rendering an empty screen when it differs.
const listOf = (d, ...keys) => {
  if (Array.isArray(d)) return d;
  for (const k of keys) if (d && Array.isArray(d[k])) return d[k];
  return [];
};

/* ---------- auth ---------- */

async function checkAuth() {
  try { const s = await api('/api/auth/status'); return !s.auth_enabled || s.logged_in; }
  catch (_) { return false; }
}

async function signIn() {
  const pw = $('pw').value;
  if (!pw) return;
  $('lerr').textContent = ''; $('go').disabled = true;
  try {
    await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ password: pw }) });
    $('pw').value = ''; $('login').classList.remove('on');
    await boot();
  } catch (e) {
    $('lerr').textContent = e.message === 'Invalid password' ? 'Wrong password.' : e.message;
  } finally { $('go').disabled = false; }
}

/* ---------- projects ---------- */

// A stable colour per project name. Hashing beats a counter because the colour
// then survives reordering, reloads, and a project appearing on another device.
const PROJECT_COLORS = ['#B85D43','#2E7D32','#0D8AFF','#7A5AF8','#B4780F','#0F8A8A','#C2405A','#5B6B7C'];
function projectColor(name) {
  if (!name) return '#9CA3AF';
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return PROJECT_COLORS[h % PROJECT_COLORS.length];
}

// The server's AI organiser labels sessions with a project; when that has not
// run yet, fall back to the workspace folder name, which is what a person would
// call it anyway.
function projectOf(s) {
  return s.project || s.project_label ||
    ((s.workspace_path || s.cwd || s.workspace || '').split('/').filter(Boolean).pop()) ||
    'Unsorted';
}

const updatedAt = (s) => Number(s.last_message_at || s.updated_at || s.created_at || 0);

async function loadProjects() {
  // Prefer the board: its cards already carry the AI-assigned project label and
  // the pending question. Fall back to the plain session list if it is absent.
  let cards = [];
  try {
    const b = await api('/api/kanban/board');
    const cols = listOf(b, 'columns', 'board');
    cols.forEach((c) => listOf(c, 'tasks').forEach((t) => cards.push({ ...t, status: t.status || c.name })));
  } catch (_) {}

  if (!cards.length) {
    try {
      const d = await api('/api/sessions');
      cards = listOf(d, 'sessions', 'items').map((s) => ({
        id: 'session:' + (s.session_id || s.id),
        session_id: s.session_id || s.id,
        title: s.title || 'Untitled session',
        status: s.agent_state || 'running',
        blocked_question: s.blocked_question || '',
        workspace_path: s.cwd || s.workspace,
        message_count: s.message_count,
        last_message_at: s.last_message_at, updated_at: s.updated_at, created_at: s.created_at,
      }));
    } catch (_) { cards = []; }
  }
  state.cards = cards.filter((c) => c.session_id);
  state.sessions = state.cards;
  renderResume(); renderProjectList(); renderBoard();
}

function ago(sec) {
  if (!sec && sec !== 0) return '';
  const s = Number(sec);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}
function agoOf(c) {
  if (c.age_seconds != null) return ago(c.age_seconds);
  const u = updatedAt(c);
  return u ? ago(Math.max(0, Date.now() / 1000 - u)) : '';
}

/* "Pick up where you left off": the most recently touched session, with its
   pending question if the agent is waiting on one. */
function renderResume() {
  const slot = $('resumeSlot');
  if (!state.cards.length) { slot.innerHTML = ''; return; }
  const c = [...state.cards].sort((a, b) => updatedAt(b) - updatedAt(a))[0];
  const proj = projectOf(c);
  slot.innerHTML = '';
  const b = document.createElement('button');
  b.className = 'resume';
  b.innerHTML =
    '<div class="k">Pick up where you left off</div>' +
    '<div class="ttl display"></div><div class="meta"></div>' +
    (c.blocked_question ? '<div class="q"></div>' : '');
  b.querySelector('.ttl').textContent = c.title || 'Untitled session';
  b.querySelector('.meta').textContent = proj + ' · ' + (agoOf(c) || 'no activity yet');
  if (c.blocked_question) b.querySelector('.q').textContent = '↳ ' + c.blocked_question;
  b.onclick = () => { selectSession(c.session_id); showView('chat'); };
  slot.appendChild(b);
}

function renderProjectList() {
  const el = $('projList');
  if (!state.cards.length) { el.innerHTML = '<div class="empty">No sessions yet.<br>Tap + to start one.</div>'; return; }

  const groups = new Map();
  for (const c of state.cards) {
    const p = projectOf(c);
    if (!groups.has(p)) groups.set(p, []);
    groups.get(p).push(c);
  }
  // Projects ordered by their freshest session; sessions newest first inside.
  const ordered = [...groups.entries()]
    .map(([name, list]) => [name, list.sort((a, b) => updatedAt(b) - updatedAt(a))])
    .sort((a, b) => updatedAt(b[1][0]) - updatedAt(a[1][0]));

  el.innerHTML = '';
  for (const [name, list] of ordered) {
    const h = document.createElement('div');
    h.className = 'projhead';
    h.innerHTML = '<span class="pdot"></span><b></b><span></span>';
    h.querySelector('.pdot').style.background = projectColor(name);
    h.querySelector('b').textContent = name;
    h.querySelector('.projhead span:last-child').textContent =
      list.length + (list.length === 1 ? ' session' : ' sessions');
    el.appendChild(h);

    for (const c of list) {
      const r = document.createElement('button');
      r.className = 'row' + (c.session_id === state.sid ? ' sel' : '');
      r.innerHTML = '<span class="t"><b></b><span></span></span><span class="chev">›</span>';
      r.querySelector('b').textContent = c.title || 'Untitled session';
      const bits = [agoOf(c)];
      if (c.blocked_question) bits.unshift('Needs you');
      else if (String(c.status).toLowerCase() === 'running') bits.unshift('Working');
      if (c.message_count) bits.push(c.message_count + ' msgs');
      r.querySelector('.t span').textContent = bits.filter(Boolean).join(' · ');
      r.onclick = () => { selectSession(c.session_id); showView('chat'); };
      el.appendChild(r);
    }
  }
}

/* The board is the same sessions re-sorted by what they need from you. Bucket
   rules mirror the existing web UI (_kanbanUrgencyBucket) so the two agree. */
const BUCKETS = [
  { key: 'needs_you', label: 'Needs you' },
  { key: 'active',    label: 'Active' },
  { key: 'up_next',   label: 'Up next' },
  { key: 'done',      label: 'Done' },
];
function bucketOf(c) {
  const s = String(c.status || '').toLowerCase();
  if (s === 'blocked' || c.blocked_question) return 'needs_you';
  if (Number(c.priority || 0) >= 3) return 'needs_you';
  if (s === 'running') return 'active';
  if (s === 'done' || s === 'archived') return 'done';
  return 'up_next';
}

function renderBoard() {
  const el = $('projBoard');
  if (!state.cards.length) { el.innerHTML = '<div class="empty">Nothing on the board yet.</div>'; return; }
  const by = { needs_you: [], active: [], up_next: [], done: [] };
  state.cards.forEach((c) => by[bucketOf(c)].push(c));
  // Unanswered questions first, then most recent.
  Object.values(by).forEach((l) => l.sort((a, b) => {
    const q = (a.blocked_question ? 0 : 1) - (b.blocked_question ? 0 : 1);
    return q || updatedAt(b) - updatedAt(a);
  }));

  el.innerHTML = '<div class="board" id="boardCols"></div>';
  const cols = $('boardCols');
  for (const b of BUCKETS) {
    const col = document.createElement('div');
    col.className = 'col';
    col.innerHTML = '<div class="colhead"><b></b><span class="n"></span></div><div class="cards"></div>';
    col.querySelector('b').textContent = b.label;
    col.querySelector('.n').textContent = by[b.key].length;
    const host = col.querySelector('.cards');
    if (!by[b.key].length) {
      const e = document.createElement('div');
      e.className = 'empty'; e.style.padding = '26px 8px'; e.style.fontSize = '13.5px';
      e.textContent = b.key === 'needs_you' ? 'Nothing waiting on you.' : 'Empty';
      host.appendChild(e);
    }
    for (const c of by[b.key]) {
      const proj = projectOf(c);
      const card = document.createElement('button');
      card.className = 'card';
      card.style.borderLeftColor = projectColor(proj);
      card.innerHTML = '<b></b><div class="m"></div>' + (c.blocked_question ? '<div class="q"></div>' : '');
      card.querySelector('b').textContent = c.title || 'Untitled session';
      card.querySelector('.m').textContent = proj + ' · ' + (agoOf(c) || '—');
      if (c.blocked_question) card.querySelector('.q').textContent = '↳ ' + c.blocked_question;
      card.onclick = () => { selectSession(c.session_id); showView('chat'); };
      host.appendChild(card);
    }
    cols.appendChild(col);
  }
}

function setProjView(board) {
  $('segList').classList.toggle('on', !board);
  $('segBoard').classList.toggle('on', board);
  $('projList').style.display = board ? 'none' : '';
  $('projBoard').style.display = board ? '' : 'none';
  localStorage.setItem('amelia-lite-projview', board ? 'board' : 'list');
}

async function selectSession(id) {
  state.sid = id;
  localStorage.setItem('amelia-lite-sid', id);
  $('msgs').innerHTML = '';
  renderProjectList();
}

async function newSession() {
  try {
    const d = await api('/api/session/new', { method: 'POST', body: JSON.stringify({ workspace: null, worktree: false }) });
    const id = d.session_id || d.id || (d.session && d.session.session_id);
    if (id) { await loadProjects(); await selectSession(id); showView('chat'); }
  } catch (e) { addMsg('err', 'Could not start a session: ' + e.message); }
}

/* ---------- chat ---------- */

function addMsg(kind, text, host) {
  const parent = host || $('msgs');
  const e = $('chatEmpty'); if (e && !host) e.remove();
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + kind;
  const b = document.createElement('div');
  b.className = 'bub'; b.textContent = text;
  wrap.appendChild(b); parent.appendChild(wrap);
  scrollDown(host ? 'askScroll' : 'chatScroll');
  return b;
}
function scrollDown(id) { const s = $(id || 'chatScroll'); if (s) s.scrollTop = s.scrollHeight; }

async function loadModel() {
  try {
    const d = await api('/api/default-model');
    state.model = d.model || d.default || d.name || null;
  } catch (_) {}
  $('mdlName').textContent = state.model || 'model not set';
}

async function send() {
  const text = $('inp').value.trim();
  if (!text || state.streaming) return;
  if (!state.sid) { await newSession(); if (!state.sid) return; }

  $('inp').value = ''; autoGrow();
  addMsg('me', text);
  setStreaming(true);

  let bubble = null, acc = '', thinkBubble = null, think = '';
  try {
    const body = { session_id: state.sid, message: text, profile: 'default' };
    if (state.files.length) body.attachments = state.files;
    const d = await api('/api/chat/start', { method: 'POST', body: JSON.stringify(body) });
    state.files = []; renderFiles();

    const streamId = d.stream_id || d.streamId;
    if (!streamId) throw new Error('server did not return a stream id');

    const es = new EventSource('/api/chat/stream?stream_id=' + encodeURIComponent(streamId), { withCredentials: true });
    state.es = es;

    es.addEventListener('token', (e) => {
      let t = ''; try { t = JSON.parse(e.data).text || ''; } catch (_) { return; }
      if (!bubble) { bubble = addMsg('bot', ''); bubble.classList.add('cursor'); }
      acc += t; bubble.textContent = acc; scrollDown();
    });
    es.addEventListener('reasoning', (e) => {
      let t = ''; try { const d2 = JSON.parse(e.data); t = d2.text || d2.reasoning || ''; } catch (_) { return; }
      if (!thinkBubble) thinkBubble = addMsg('think', '');
      think += t; thinkBubble.textContent = think; scrollDown();
    });
    es.addEventListener('tool', (e) => {
      let n = 'working'; try { const d2 = JSON.parse(e.data); n = d2.name || d2.tool || n; } catch (_) {}
      addMsg('tool', n);
    });
    es.addEventListener('approval', () => { loadApprovalCount(); showPromo('Amelia needs your approval'); });

    const finish = () => {
      if (bubble) bubble.classList.remove('cursor');
      setStreaming(false);
      try { es.close(); } catch (_) {}
      state.es = null; loadProjects();
    };
    es.addEventListener('done', finish);
    es.addEventListener('stream_end', finish);
    es.addEventListener('error', (e) => {
      let m = ''; try { m = JSON.parse(e.data).message || ''; } catch (_) {}
      if (m) addMsg('err', m);
      finish();
    });
    es.onerror = () => { if (state.streaming && es.readyState === EventSource.CLOSED) finish(); };
  } catch (e) { addMsg('err', e.message); setStreaming(false); }
}

function setStreaming(on) {
  state.streaming = on;
  $('send').disabled = on || !$('inp').value.trim();
  $('mdlState').textContent = on ? 'thinking…' : 'idle';
  showPromo(on ? 'Amelia is working…' : null);
}

function showPromo(text) {
  const el = $('promo');
  if (!text) { el.classList.remove('on'); document.querySelectorAll('.scroll').forEach((s) => s.classList.remove('promoOn')); return; }
  $('promoTxt').textContent = text; el.classList.add('on');
  document.querySelectorAll('.scroll').forEach((s) => s.classList.add('promoOn'));
}

function renderFiles() {
  const el = $('files'); el.innerHTML = '';
  state.files.forEach((f) => {
    const c = document.createElement('span');
    c.className = 'chipf'; c.textContent = (f.name || f) + '';
    el.appendChild(c);
  });
}

async function pickFiles(ev) {
  const list = [...(ev.target.files || [])];
  if (!list.length) return;
  for (const f of list) {
    const fd = new FormData(); fd.append('file', f);
    try {
      const d = await api('/api/upload', { method: 'POST', body: fd });
      state.files.push({ name: f.name, path: d.path || d.id || d.url });
    } catch (_) {
      addMsg('err', 'Upload failed for ' + f.name);
    }
  }
  renderFiles(); ev.target.value = '';
}

/* ---------- approvals count ---------- */

async function loadApprovalCount() {
  let n = 0;
  try { n = listOf(await api('/api/approval/pending'), 'pending', 'approvals', 'items').length; } catch (_) {}
  for (const id of ['hdrBadge', 'tabBadge']) {
    const b = $(id); b.textContent = n; b.style.display = n > 0 ? 'grid' : 'none';
  }
}

/* ---------- connections ---------- */

// The point of this screen is not a status list — it is teaching what each
// connection makes possible, and naming a self-hostable alternative so the
// choice is informed rather than defaulted.
const GUIDE = {
  cloudflare: { name: 'Cloudflare', unlocks: ['DNS and zones managed for you', 'R2 object storage for uploads and backups', 'Workers for edge APIs and gateways'], alt: 'Self-hosted alternative: Caddy or Traefik for routing, MinIO for S3-style storage.' },
  hetzner:    { name: 'Hetzner',    unlocks: ['Cheap always-on servers for agents and databases', 'Volumes and snapshots for backups', 'Runs anything a container can run'], alt: 'Alternatives: OVH or Netcup for similar pricing; your own hardware if it is already on.' },
  railway:    { name: 'Railway',    unlocks: ['Push-to-deploy web services', 'Managed Postgres and Redis', 'Cron jobs without a server to babysit'], alt: 'Self-hosted alternative: Coolify or Dokploy on a Hetzner box — same workflow, flat cost.' },
  vercel:     { name: 'Vercel',     unlocks: ['Next.js hosting with preview deploys', 'Edge functions and image optimisation'], alt: 'Self-hosted alternative: Coolify, or Next.js standalone behind Caddy.' },
  github:     { name: 'GitHub',     unlocks: ['Read and write your repos', 'Open PRs and read CI results', 'Deploy keys scoped to one repo'], alt: 'Self-hosted alternative: Forgejo or Gitea.' },
  openai:     { name: 'OpenAI',     unlocks: ['Powers Ask AI recommendations', 'Vision and file understanding', 'A fallback when local models are busy'], alt: 'Local alternative: the models already on your GPU box — free and private.' },
};

// Rung 1 of the ladder: what we can say with NO permissions granted, because
// Amelia already runs on this machine. Reads the aggregate file the scanner
// writes — counts only, never project names or paths, because /static/ is
// unauthenticated while /api/ is not.
async function renderStack() {
  let agg = null;
  try {
    const r = await fetch('stack.public.json', { cache: 'no-store' });
    if (r.ok) agg = await r.json();
  } catch (_) {}

  const box = document.createElement('div');
  box.className = 'conn';

  if (!agg || !agg.projects) {
    box.innerHTML =
      '<div class="top"><b>Your stack</b><span class="state st-off">Not scanned</span></div>' +
      '<p>Amelia can read the manifests of the projects already on this machine — ' +
      'no account connection and no repo access needed. It never opens your source.</p>' +
      '<div class="act"><button class="btn-s">How to scan</button></div>';
    box.querySelector('button').onclick = () => showPromo('Run: amelia-stack-scan');
    return box;
  }

  const chips = (items) => items.map(([n, c]) => '<span class="chip">' + n + ' ×' + c + '</span>').join('');
  box.innerHTML =
    '<div class="top"><b>Your stack</b><span class="state st-on">' + agg.projects + ' projects</span></div>' +
    '<p style="margin-bottom:8px">Detected from manifests on this machine. No accounts connected yet.</p>' +
    '<div class="grouplbl" style="margin:14px 0 8px">Frameworks</div><div class="filterbar">' + chips(agg.frameworks.slice(0, 6)) + '</div>' +
    '<div class="grouplbl" style="margin:14px 0 8px">Deploys to</div><div class="filterbar">' + chips(agg.hosting.slice(0, 6)) + '</div>' +
    '<div class="grouplbl" style="margin:14px 0 8px">Paid services in code</div><div class="filterbar">' + chips(agg.paid_services.slice(0, 8)) + '</div>';

  // The savings line is the point of all this — but it is an ESTIMATE from
  // dependency manifests, not from a bill. Say so, and never state a figure
  // as if it were measured: a migration that lands over budget destroys the
  // trust that made someone connect in the first place.
  const host = Object.fromEntries(agg.hosting);
  const managed = (host['Vercel'] || 0) + (host['Railway'] || 0) + (host['Heroku'] || 0) + (host['Fly.io'] || 0);
  if (managed >= 3) {
    const s = document.createElement('div');
    s.style.cssText = 'margin-top:14px;padding:14px;border-radius:14px;background:var(--surfaceAlt)';
    s.innerHTML =
      '<b style="font-size:14.5px">Possible saving</b>' +
      '<p style="margin-top:6px">' + managed + ' projects target managed hosts (' +
      [['Vercel', host['Vercel']], ['Railway', host['Railway']], ['Heroku', host['Heroku']], ['Fly.io', host['Fly.io']]]
        .filter(([, n]) => n).map(([n, c]) => n + ' ×' + c).join(', ') +
      '). Most of these fit on one small server.</p>' +
      '<p style="margin-top:8px;font-size:12.5px;color:var(--ink3)">' +
      'Estimated from code, not from your bills. Connect the accounts to replace this ' +
      'guess with real numbers — seats and bandwidth often do not move with the compute.</p>' +
      '<div class="act"><button class="btn-s">Connect billing to check</button></div>';
    s.querySelector('button').onclick = () => showPromo('Billing connect is not wired up yet.');
    box.appendChild(s);
  }
  return box;
}

async function renderConnections() {
  const el = $('connList');
  el.innerHTML = '<div class="empty">Checking…</div>';

  let providers = [], pending = [];
  try { providers = listOf(await api('/api/providers'), 'providers', 'items'); } catch (_) {}
  try { pending = listOf(await api('/api/approval/pending'), 'pending', 'approvals', 'items'); } catch (_) {}

  const connected = new Set(providers.map((p) => String(p.name || p.id || p.provider || '').toLowerCase()));
  el.innerHTML = '';
  el.appendChild(await renderStack());

  if (pending.length) {
    const c = document.createElement('div');
    c.className = 'conn';
    c.innerHTML = '<div class="top"><b>Waiting on you</b><span class="state st-need">' + pending.length + ' pending</span></div>' +
      '<p>Amelia has paused until you approve these.</p>';
    el.appendChild(c);
    pending.forEach((a) => {
      const r = document.createElement('div');
      r.className = 'conn';
      r.innerHTML = '<div class="top"><b></b><span class="state st-need">Approve</span></div><p></p>';
      r.querySelector('b').textContent = a.title || a.tool || 'Approval needed';
      r.querySelector('p').textContent = (a.command || a.detail || '').slice(0, 220);
      el.appendChild(r);
    });
  }

  for (const key of Object.keys(GUIDE)) {
    const g = GUIDE[key];
    const on = connected.has(key);
    const c = document.createElement('div');
    c.className = 'conn';
    c.innerHTML =
      '<div class="top"><b></b><span class="state ' + (on ? 'st-on' : 'st-off') + '">' + (on ? 'Connected' : 'Not connected') + '</span></div>' +
      '<ul class="unlocks">' + g.unlocks.map(() => '<li></li>').join('') + '</ul>' +
      '<div class="alt"></div>' +
      '<div class="act"><button class="' + (on ? 'btn-g' : 'btn-s') + '">' + (on ? 'Disconnect' : 'Connect') + '</button></div>';
    c.querySelector('b').textContent = g.name;
    c.querySelectorAll('.unlocks li').forEach((li, i) => { li.textContent = g.unlocks[i]; });
    c.querySelector('.alt').textContent = g.alt;
    c.querySelector('.act button').onclick = () => {
      showPromo(on ? 'Disconnecting is not wired up yet.' : 'Connecting ' + g.name + ' is not wired up yet.');
    };
    el.appendChild(c);
  }
}

/* ---------- profile ---------- */

async function renderProfile() {
  $('pMail').textContent = location.hostname || 'this device';
  $('pState').textContent = state.streaming ? 'Working' : 'Connected';
  $('themeVal').textContent = document.documentElement.getAttribute('data-theme') === 'dark' ? 'Dark' : 'Light';

  await renderModels();

  // Devices — the two machines the brain and vault already span.
  const dEl = $('pDevices'); dEl.innerHTML = '';
  [['This device', location.hostname || 'browser', true],
   ['GPU box', 'tailnet · always on', true]].forEach(([n, sub, ok]) => {
    const r = document.createElement('div');
    r.className = 'row';
    r.innerHTML = '<span class="t"><b></b><span></span></span><span class="dis">Disconnect</span>';
    r.querySelector('b').textContent = n;
    r.querySelector('.t span').textContent = sub;
    r.querySelector('.dis').onclick = () => showPromo('Device management is not wired up yet.');
    dEl.appendChild(r);
  });
  const add = document.createElement('button');
  add.className = 'row';
  add.innerHTML = '<span class="t"><b>Add a device</b><span>Pair another machine or phone</span></span><span class="chev">›</span>';
  add.onclick = () => showPromo('Pairing is not wired up yet.');
  dEl.appendChild(add);

  await renderSkills();
}

async function renderSkills() {
  if (!state.skills.length) {
    try { state.skills = listOf(await api('/api/skills'), 'skills', 'items'); } catch (_) { state.skills = []; }
  }
  const fEl = $('skillFilter');
  if (!fEl.dataset.built) {
    [['all', 'Most used'], ['az', 'A–Z']].forEach(([k, label]) => {
      const b = document.createElement('button');
      b.className = 'chip' + (state.skillFilter === k ? ' on' : '');
      b.textContent = label;
      b.onclick = () => { state.skillFilter = k; fEl.dataset.built = ''; fEl.innerHTML = ''; renderSkills(); };
      fEl.appendChild(b);
    });
    fEl.dataset.built = '1';
  }

  const el = $('pSkills'); el.innerHTML = '';
  let list = [...state.skills];
  // "Most used" is a real ordering when the server reports a count, and an
  // honest fallback to A–Z when it does not — rather than a fake ranking.
  if (state.skillFilter === 'az') list.sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
  else list.sort((a, b) => Number(b.uses || b.use_count || 0) - Number(a.uses || a.use_count || 0)
    || String(a.name || '').localeCompare(String(b.name || '')));

  if (!list.length) { el.innerHTML = '<div class="empty" style="padding:22px">No skills installed.</div>'; return; }
  list.slice(0, 40).forEach((s) => {
    const r = document.createElement('div');
    r.className = 'row';
    r.innerHTML = '<span class="t"><b></b><span></span></span>';
    r.querySelector('b').textContent = s.name || s.id || 'skill';
    const uses = Number(s.uses || s.use_count || 0);
    r.querySelector('.t span').textContent = (uses ? uses + ' uses · ' : '') + String(s.description || '').slice(0, 70);
    el.appendChild(r);
  });
}


/* ---------- models ---------- */

// Local endpoints already running on Richy's two machines. Prefilling these is
// the difference between "connect a local model" being a five-minute lookup and
// being one tap.
const LOCAL_PRESETS = [
  { label: 'Ollama on the GPU box', provider: 'ollama', base_url: 'http://100.86.83.64:11434/v1' },
  { label: 'MLX on this Mac',       provider: 'openai-compatible', base_url: 'http://127.0.0.1:8791/v1' },
];

async function renderModels() {
  const el = $('pModels');
  el.innerHTML = '<div class="empty" style="padding:18px">Loading models…</div>';

  let models = [];
  try {
    const d = await api('/api/models');
    // The endpoint returns a dict whose model list has moved around upstream;
    // take whichever array is actually there rather than betting on one key.
    models = listOf(d, 'models', 'items', 'data', 'available');
    if (!models.length && d && typeof d === 'object') {
      for (const v of Object.values(d)) if (Array.isArray(v) && v.length) { models = v; break; }
    }
  } catch (_) {}

  el.innerHTML = '';
  if (!models.length) {
    el.innerHTML = '<div class="empty" style="padding:18px">No models reported.</div>';
  }

  models.slice(0, 25).forEach((m) => {
    const name = (typeof m === 'string') ? m : (m.id || m.name || m.model || '');
    if (!name) return;
    const provider = (typeof m === 'object' && (m.provider || m.owned_by)) || 'local';
    const inUse = name === state.model;
    const r = document.createElement('button');
    r.className = 'row' + (inUse ? ' sel' : '');
    r.innerHTML = '<span class="t"><b></b><span></span></span><span class="chev"></span>';
    r.querySelector('b').textContent = name;
    r.querySelector('.t span').textContent = provider + (inUse ? ' · in use' : '');
    r.querySelector('.chev').textContent = inUse ? '✓' : '›';
    r.onclick = () => useModel(name, typeof m === 'object' ? m.provider : null);
    el.appendChild(r);
  });

  const add = document.createElement('button');
  add.className = 'row';
  add.innerHTML = '<span class="t"><b>Add a local model</b><span>Ollama, MLX, or anything OpenAI-compatible</span></span><span class="chev">›</span>';
  add.onclick = showLocalForm;
  el.appendChild(add);
}

async function useModel(name, provider) {
  try {
    await api('/api/model/set', {
      method: 'POST',
      body: JSON.stringify({ scope: 'main', model: name, provider: provider || 'auto' }),
    });
    state.model = name;
    $('mdlName').textContent = name;
    showPromo('Now using ' + name);
    renderModels();
  } catch (e) { showPromo('Could not switch model: ' + e.message); }
}

function showLocalForm() {
  const el = $('pModels');
  if ($('localForm')) { $('localForm').scrollIntoView({ block: 'center' }); return; }
  const box = document.createElement('div');
  box.id = 'localForm';
  box.className = 'conn';
  box.innerHTML =
    '<div class="top"><b>Connect a local model</b></div>' +
    '<p>Runs on your own hardware. Nothing leaves your machines.</p>' +
    '<div class="filterbar" id="presetRow"></div>' +
    '<input id="lfUrl" class="lfin" placeholder="http://host:port/v1" />' +
    '<input id="lfModel" class="lfin" placeholder="model name (e.g. qwen3.8:27b)" />' +
    '<div class="act"><button class="btn-s" id="lfSave">Connect</button>' +
    '<button class="btn-g" id="lfCancel">Cancel</button></div>';
  el.appendChild(box);

  const pr = box.querySelector('#presetRow');
  LOCAL_PRESETS.forEach((p) => {
    const b = document.createElement('button');
    b.className = 'chip'; b.textContent = p.label;
    b.onclick = () => { $('lfUrl').value = p.base_url; box.dataset.provider = p.provider; };
    pr.appendChild(b);
  });

  box.querySelector('#lfCancel').onclick = () => box.remove();
  box.querySelector('#lfSave').onclick = async () => {
    const base_url = $('lfUrl').value.trim(), model = $('lfModel').value.trim();
    if (!base_url || !model) { showPromo('Need both a URL and a model name.'); return; }
    try {
      await api('/api/providers/self-hosted', {
        method: 'POST',
        body: JSON.stringify({
          provider: box.dataset.provider || 'openai-compatible',
          base_url, model, activate: true,
        }),
      });
      box.remove();
      showPromo('Connected ' + model);
      await loadModel(); await renderModels();
    } catch (e) { showPromo('Could not connect: ' + e.message); }
  };
  box.scrollIntoView({ block: 'center' });
}

/* ---------- Ask AI ---------- */

const SUGGESTIONS = [
  'What could I self-host instead of what I pay for?',
  'Which local model fits my GPU box for planning work?',
  'Where is my stack fragile right now?',
];

function renderAskSuggestions() {
  const el = $('askSugg');
  if (el.dataset.built) return;
  SUGGESTIONS.forEach((q) => {
    const b = document.createElement('button');
    b.textContent = q;
    b.onclick = () => { $('askInp').value = q; askGrow(); askSend(); };
    el.appendChild(b);
  });
  el.dataset.built = '1';
}

// Ask AI runs through the same agent backend, but framed as advice and
// explicitly told not to touch anything. It is a different mode, not a
// different engine — so there is no second integration to maintain.
async function askSend() {
  const text = $('askInp').value.trim();
  if (!text || state.askBusy) return;
  $('askInp').value = ''; askGrow();
  addMsg('me', text, $('askMsgs'));
  state.askBusy = true; $('askSend').disabled = true; $('askState').textContent = 'thinking…';
  $('askSugg').style.display = 'none';

  const framing =
    'You are advising on this developer\'s stack. Answer with concrete options, ' +
    'including self-hostable and open-source alternatives, and say plainly when ' +
    'a paid service is genuinely the better choice. Do not run commands or change anything.\n\n';

  try {
    const s = await api('/api/session/new', { method: 'POST', body: JSON.stringify({ workspace: null, worktree: false }) });
    const sid = s.session_id || s.id;
    const d = await api('/api/chat/start', { method: 'POST', body: JSON.stringify({ session_id: sid, message: framing + text, profile: 'default' }) });
    const streamId = d.stream_id || d.streamId;
    const es = new EventSource('/api/chat/stream?stream_id=' + encodeURIComponent(streamId), { withCredentials: true });
    let bub = null, acc = '';
    es.addEventListener('token', (e) => {
      let t = ''; try { t = JSON.parse(e.data).text || ''; } catch (_) { return; }
      if (!bub) { bub = addMsg('bot', '', $('askMsgs')); bub.classList.add('cursor'); }
      acc += t; bub.textContent = acc; scrollDown('askScroll');
    });
    const done = () => {
      if (bub) bub.classList.remove('cursor');
      state.askBusy = false; $('askSend').disabled = !$('askInp').value.trim();
      $('askState').textContent = 'idle';
      try { es.close(); } catch (_) {}
    };
    es.addEventListener('done', done);
    es.addEventListener('stream_end', done);
    es.onerror = () => { if (es.readyState === EventSource.CLOSED) done(); };
  } catch (e) {
    addMsg('err', e.message, $('askMsgs'));
    state.askBusy = false; $('askSend').disabled = false; $('askState').textContent = 'idle';
  }
}

/* ---------- chrome ---------- */

const TITLES = { chat: 'Amelia', projects: 'Projects', ask: 'Ask AI', conn: 'Connect', profile: 'Profile' };

function showView(v) {
  document.querySelectorAll('.view').forEach((s) => s.classList.toggle('on', s.id === 'v-' + v));
  document.querySelectorAll('.tab').forEach((t) => {
    const on = t.dataset.v === v;
    t.classList.toggle('on', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  $('wordmark').textContent = TITLES[v] || 'Amelia';
  $('hdr').classList.remove('away');
  $('bar').classList.remove('compact');
  if (v === 'projects') loadProjects();
  if (v === 'conn') renderConnections();
  if (v === 'profile') renderProfile();
  if (v === 'ask') renderAskSuggestions();
}

function wireChrome() {
  let last = 0;
  const THRESH = 10, TOP = 50;
  document.querySelectorAll('.scroll').forEach((sc) => {
    sc.addEventListener('scroll', () => {
      const y = sc.scrollTop, d = y - last;
      if (y < TOP) { $('hdr').classList.remove('away'); $('bar').classList.remove('compact'); last = y; return; }
      if (Math.abs(d) < THRESH) return;
      $('hdr').classList.toggle('away', d > 0);
      // The bar tightens rather than leaving, so the primary control is always
      // reachable while reading.
      $('bar').classList.toggle('compact', d > 0);
      last = y;
    }, { passive: true });
  });
}

function autoGrow() {
  const t = $('inp');
  t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 128) + 'px';
  $('send').disabled = state.streaming || !t.value.trim();
}
function askGrow() {
  const t = $('askInp');
  t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 128) + 'px';
  $('askSend').disabled = state.askBusy || !t.value.trim();
}

// Light is set explicitly: this design was specified as the white one, so the
// OS's dark preference must not silently override it.
function applyTheme(t) { document.documentElement.setAttribute('data-theme', t === 'dark' ? 'dark' : 'light'); }

/* ---------- boot ---------- */

async function boot() {
  await loadModel();
  await loadProjects();
  loadApprovalCount();
  const saved = localStorage.getItem('amelia-lite-sid');
  if (saved && state.cards.some((c) => c.session_id === saved)) await selectSession(saved);
  else if (state.cards.length) await selectSession([...state.cards].sort((a, b) => updatedAt(b) - updatedAt(a))[0].session_id);
  setProjView(localStorage.getItem('amelia-lite-projview') === 'board');
}

(async function init() {
  applyTheme(localStorage.getItem('amelia-lite-theme') || 'light');
  wireChrome();

  $('go').onclick = signIn;
  $('pw').addEventListener('keydown', (e) => { if (e.key === 'Enter') signIn(); });
  $('send').onclick = send;
  $('inp').addEventListener('input', autoGrow);
  $('inp').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && window.matchMedia('(pointer:fine)').matches) { e.preventDefault(); send(); }
  });
  $('attach').onclick = () => $('fileInput').click();
  $('fileInput').addEventListener('change', pickFiles);

  $('askSend').onclick = askSend;
  $('askInp').addEventListener('input', askGrow);

  $('newBtn').onclick = newSession;
  $('connBtn').onclick = () => showView('conn');
  $('promoX').onclick = () => showPromo(null);
  $('segList').onclick = () => setProjView(false);
  $('segBoard').onclick = () => setProjView(true);

  $('themeRow').onclick = () => {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    localStorage.setItem('amelia-lite-theme', next); applyTheme(next); renderProfile();
  };
  $('signOutRow').onclick = async () => {
    try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) {}
    location.reload();
  };

  document.querySelectorAll('.tab').forEach((t) => { t.onclick = () => showView(t.dataset.v); });

  if (await checkAuth()) await boot();
  else { $('login').classList.add('on'); $('pw').focus(); }
})();

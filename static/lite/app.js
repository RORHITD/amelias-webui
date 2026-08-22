/* Amelia Lite — client logic.
 *
 * Talks to the same HTTP/SSE surface the iOS app uses, so there is no new
 * backend contract to maintain. Everything here is additive: no upstream file
 * is touched, which is what keeps `git pull` from upstream conflict-free.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const state = { sid: null, streaming: false, sessions: [], es: null };

/* ---------- transport ---------- */

// Cookies carry the session, so every call is same-origin with credentials.
async function api(path, opts = {}) {
  const r = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const txt = await r.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch (_) { /* non-JSON is fine */ }
  if (!r.ok) {
    const e = new Error((data && data.error) || r.statusText || 'request failed');
    e.status = r.status;
    throw e;
  }
  return data;
}

/* ---------- auth ---------- */

async function checkAuth() {
  try {
    const s = await api('/api/auth/status');
    // auth_enabled false means the server is wide open (its default) — either
    // way, "can we proceed" is the only question this screen answers.
    return !s.auth_enabled || s.logged_in;
  } catch (_) { return false; }
}

async function signIn() {
  const pw = $('pw').value;
  if (!pw) return;
  $('lerr').textContent = '';
  $('go').disabled = true;
  try {
    await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ password: pw }) });
    $('pw').value = '';
    $('login').classList.remove('on');
    await boot();
  } catch (e) {
    $('lerr').textContent = e.message === 'Invalid password' ? 'Wrong password.' : e.message;
  } finally {
    $('go').disabled = false;
  }
}

/* ---------- sessions ---------- */

async function loadSessions() {
  try {
    const d = await api('/api/sessions');
    // The endpoint has grown several shapes upstream; accept the common ones
    // rather than guessing one and rendering an empty list on mismatch.
    state.sessions = Array.isArray(d) ? d : (d.sessions || d.items || []);
  } catch (_) { state.sessions = []; }
  renderSessions();
}

function sessionTitle(s) {
  return s.title || s.name || s.summary || (s.workspace ? s.workspace.split('/').pop() : null) || 'Untitled session';
}

function renderSessions() {
  const el = $('sessList');
  if (!state.sessions.length) {
    el.innerHTML = '<div class="empty">No sessions yet.<br>Tap + to start one.</div>';
    return;
  }
  el.innerHTML = '';
  for (const s of state.sessions) {
    const id = s.session_id || s.id;
    const row = document.createElement('button');
    row.className = 'row' + (id === state.sid ? ' sel' : '');
    row.innerHTML =
      '<span class="dot" style="' + (id === state.sid ? '' : 'opacity:.22') + '"></span>' +
      '<span class="t"><b></b><span></span></span>';
    row.querySelector('b').textContent = sessionTitle(s);
    row.querySelector('.t span').textContent = s.workspace || id || '';
    row.onclick = () => { selectSession(id); showView('chat'); };
    el.appendChild(row);
  }
}

async function selectSession(id) {
  state.sid = id;
  localStorage.setItem('amelia-lite-sid', id);
  $('msgs').innerHTML = '';
  $('sub').textContent = 'session ready';
  renderSessions();
}

async function newSession() {
  try {
    const d = await api('/api/session/new', {
      method: 'POST',
      body: JSON.stringify({ workspace: null, worktree: false }),
    });
    const id = d.session_id || d.id || (d.session && d.session.session_id);
    if (id) { await loadSessions(); await selectSession(id); showView('chat'); }
  } catch (e) {
    addMsg('err', 'Could not start a session: ' + e.message);
  }
}

/* ---------- chat ---------- */

function addMsg(kind, text) {
  $('chatEmpty') && $('chatEmpty').remove();
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + kind;
  const b = document.createElement('div');
  b.className = 'bub';
  b.textContent = text;
  wrap.appendChild(b);
  $('msgs').appendChild(wrap);
  scrollDown();
  return b;
}

function scrollDown() {
  const s = $('chatScroll');
  s.scrollTop = s.scrollHeight;
}

async function send() {
  const text = $('inp').value.trim();
  if (!text || state.streaming) return;
  if (!state.sid) { await newSession(); if (!state.sid) return; }

  $('inp').value = '';
  autoGrow();
  addMsg('me', text);
  setStreaming(true);

  let bubble = null;
  let acc = '';

  try {
    const d = await api('/api/chat/start', {
      method: 'POST',
      body: JSON.stringify({ session_id: state.sid, message: text, profile: 'default' }),
    });
    const streamId = d.stream_id || d.streamId;
    if (!streamId) throw new Error('server did not return a stream id');

    const es = new EventSource('/api/chat/stream?stream_id=' + encodeURIComponent(streamId), { withCredentials: true });
    state.es = es;

    // Assistant text arrives as `token` events carrying {text}. Append into one
    // bubble rather than creating a node per token.
    es.addEventListener('token', (e) => {
      let t = '';
      try { t = JSON.parse(e.data).text || ''; } catch (_) { return; }
      if (!bubble) { bubble = addMsg('bot', ''); bubble.classList.add('cursor'); }
      acc += t;
      bubble.textContent = acc;
      scrollDown();
    });

    // Tool activity is shown, quietly, because a silent gap while the agent
    // works reads as a hang.
    es.addEventListener('tool', (e) => {
      let n = 'working…';
      try { const d2 = JSON.parse(e.data); n = d2.name || d2.tool || n; } catch (_) {}
      addMsg('tool', '⚙ ' + n);
    });

    const finish = () => {
      if (bubble) bubble.classList.remove('cursor');
      setStreaming(false);
      try { es.close(); } catch (_) {}
      state.es = null;
      loadSessions();
    };
    es.addEventListener('done', finish);
    es.addEventListener('stream_end', finish);
    es.addEventListener('error', (e) => {
      let m = '';
      try { m = JSON.parse(e.data).message || ''; } catch (_) {}
      if (m) addMsg('err', m);
      finish();
    });
    // A dropped connection fires onerror with no data; don't leave the UI stuck.
    es.onerror = () => { if (state.streaming && es.readyState === EventSource.CLOSED) finish(); };
  } catch (e) {
    addMsg('err', e.message);
    setStreaming(false);
  }
}

function setStreaming(on) {
  state.streaming = on;
  $('send').disabled = on || !$('inp').value.trim();
  $('sub').textContent = on ? 'thinking…' : 'ready';
}

/* ---------- chrome ---------- */

function showView(v) {
  document.querySelectorAll('.view').forEach((s) => s.classList.toggle('on', s.id === 'v-' + v));
  document.querySelectorAll('.tab').forEach((t) => {
    const on = t.dataset.v === v;
    t.classList.toggle('on', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  $('title').textContent = v === 'chat' ? 'Amelia' : 'Sessions';
  if (v === 'sessions') loadSessions();
}

// Scrolling down means reading — give the pixels back. Scrolling up means
// reaching for a control, so the header returns. The threshold stops iOS
// rubber-band bounce from flickering it.
function wireChrome() {
  let last = 0;
  const THRESH = 10, TOP = 50;
  document.querySelectorAll('.scroll').forEach((sc) => {
    sc.addEventListener('scroll', () => {
      const y = sc.scrollTop, d = y - last;
      if (y < TOP) { $('hdr').classList.remove('away'); last = y; return; }
      if (Math.abs(d) < THRESH) return;
      $('hdr').classList.toggle('away', d > 0);
      last = y;
    }, { passive: true });
  });
}

function autoGrow() {
  const t = $('inp');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 132) + 'px';
  $('send').disabled = state.streaming || !t.value.trim();
}

function applyTheme(t) {
  if (t) document.documentElement.setAttribute('data-theme', t);
  else document.documentElement.removeAttribute('data-theme');
}

/* ---------- boot ---------- */

async function boot() {
  $('sub').textContent = 'ready';
  await loadSessions();
  const saved = localStorage.getItem('amelia-lite-sid');
  const known = state.sessions.some((s) => (s.session_id || s.id) === saved);
  if (saved && known) await selectSession(saved);
  else if (state.sessions.length) await selectSession(state.sessions[0].session_id || state.sessions[0].id);
}

(async function init() {
  applyTheme(localStorage.getItem('amelia-lite-theme'));
  wireChrome();

  $('go').onclick = signIn;
  $('pw').addEventListener('keydown', (e) => { if (e.key === 'Enter') signIn(); });
  $('send').onclick = send;
  $('newBtn').onclick = newSession;
  $('inp').addEventListener('input', autoGrow);
  $('inp').addEventListener('keydown', (e) => {
    // Enter sends; Shift+Enter breaks the line. On touch keyboards Enter is a
    // newline, so the button stays the primary path there.
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && window.matchMedia('(pointer:fine)').matches) {
      e.preventDefault(); send();
    }
  });
  $('themeBtn').onclick = () => {
    const cur = document.documentElement.getAttribute('data-theme');
    const dark = window.matchMedia('(prefers-color-scheme:dark)').matches;
    const next = cur ? (cur === 'dark' ? 'light' : null) : (dark ? 'light' : 'dark');
    if (next) localStorage.setItem('amelia-lite-theme', next);
    else localStorage.removeItem('amelia-lite-theme');
    applyTheme(next);
  };
  document.querySelectorAll('.tab').forEach((t) => { t.onclick = () => showView(t.dataset.v); });

  if (await checkAuth()) await boot();
  else { $('login').classList.add('on'); $('pw').focus(); }
})();

// A.R.I.A. app shell: the always-visible sidebar, the router that swaps
// views without reloading (so a running chat, agent or thought stream
// survives a tab switch), the state poll that keeps the sidebar honest,
// and the composer every talking surface shares.

import { $, $$, h, clear, api, bus, title, toast } from './js/core.js';
import { talk, submit, newChat, openChat, deleteChat, toggleVoice,
         toggleListening, voiceInput } from './js/talk.js';
import * as chat from './js/chat.js';
import * as projects from './js/projects.js';
import * as board from './js/board.js';
import * as agents from './js/agents.js';
import * as orb from './js/orb.js';
import * as settings from './js/settings.js';

const VIEWS = { chat, projects, board, agents, orb, settings };
const PATHS = { chat: '/', projects: '/projects', board: '/board',
                agents: '/agents', orb: '/orb', settings: '/settings' };
const TITLES = { chat: 'A.R.I.A.', projects: 'A.R.I.A. — Projects',
                 board: 'A.R.I.A. — Pipeline', agents: 'A.R.I.A. — Agents',
                 orb: 'A.R.I.A. — Orb', settings: 'A.R.I.A. — Settings' };
let current = null;
export let appState = null;

// ------------------------------------------------------------- composer

// One composer, three homes (chat, orb, dock). Each instance reflects the
// same shared state: busy, listening, voice on/off, the loaded skill.
export function makeComposer({ placeholder, hint = true } = {}) {
  const input = h('textarea', { rows: 1, placeholder: placeholder ||
    'Message A.R.I.A…  (“capture …”, “work on <project>”, “search for …”, “status”)' });
  const chips = h('div', { class: 'chips' });
  const hintEl = h('span', { class: 'c-hint' });
  const mic = h('button', { class: 'iconbtn', title: 'voice input', text: '🎤',
                            onclick: toggleListening });
  const mute = h('button', { class: 'iconbtn', title: 'spoken replies on/off',
                             onclick: toggleVoice });
  const send = h('button', { class: 'iconbtn send', title: 'send', text: '↑' });
  const fire = () => {
    const v = input.value.trim();
    if (!v || talk.busy) return;
    input.value = '';
    input.style.height = 'auto';
    submit(v);
  };
  send.onclick = fire;
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = input.scrollHeight + 'px';
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); fire(); }
  });
  if (voiceInput === 'none') {
    mic.disabled = true;
    mic.title = 'no microphone access in this browser';
  }
  const sync = () => {
    send.disabled = talk.busy;
    mic.classList.toggle('on', talk.listening);
    mute.textContent = talk.voiceOn ? '🔊' : '🔇';
    clear(chips, talk.current && talk.current.skill
      ? h('span', { class: 'chip', text: 'skill: ' + talk.current.skill }) : null);
    if (hint) hintEl.textContent = voiceInput === 'whisper'
      ? 'voice runs locally via whisper — click 🎤, speak, click again' : '';
  };
  for (const ev of ['talk:busy', 'talk:listening', 'talk:voice', 'talk:skill',
                    'talk:reset'])
    bus.on(ev, sync);
  bus.on('talk:interim', text => {
    hintEl.textContent = text ? '… ' + text : '';
  });
  sync();
  const el = h('div', { class: 'composer' }, chips, input,
               h('div', { class: 'c-row' }, hintEl, mic, mute, send));
  el.focusInput = () => input.focus();
  return el;
}

// --------------------------------------------------------------- router

function parse(loc = location) {
  const view = Object.keys(PATHS).find(v => PATHS[v] === loc.pathname) || 'chat';
  const q = new URLSearchParams(loc.search);
  return { view, slug: q.get('p') || '', tab: q.get('tab') || '',
           file: q.get('f') || '' };
}

// `quiet`: only the address changes (a view that has already drawn the
// new state itself, e.g. picking a file, keeps the URL shareable without
// reloading everything).
export function go({ view, slug = '', tab = '', file = '' },
                   { replace = false, quiet = false } = {}) {
  if (!VIEWS[view]) view = 'chat';
  const q = new URLSearchParams();
  if (view === 'projects') {
    if (slug) q.set('p', slug);
    if (tab) q.set('tab', tab);
    if (file && tab === 'files') q.set('f', file);
  }
  const url = PATHS[view] + (q.toString() ? '?' + q : '');
  if (url !== location.pathname + location.search)
    history[replace ? 'replaceState' : 'pushState'](null, '', url);
  if (!quiet) show({ view, slug, tab, file });
}

function show({ view, slug, tab, file }) {
  if (current && current !== view) VIEWS[current].hide?.();
  for (const v of Object.keys(VIEWS))
    $('#view-' + v).hidden = v !== view;
  for (const a of $$('#nav a'))
    a.classList.toggle('here', a.dataset.view === view);
  $('#dock').hidden = view === 'chat' || view === 'orb';
  document.title = TITLES[view];
  current = view;
  VIEWS[view].show({ slug, tab, file });
}

addEventListener('popstate', () => show(parse()));
document.addEventListener('click', e => {
  const a = e.target.closest('a[data-view]');
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
  e.preventDefault();
  go({ view: a.dataset.view, slug: a.dataset.slug || '', tab: a.dataset.tab || '',
       file: a.dataset.file || '' });
});
bus.on('nav', target => go(target));

// -------------------------------------------------------------- sidebar

function renderRecents() {
  clear($('#recents'), talk.chats.map(c => {
    const b = h('button', { class: 'recent' + (talk.current && c.id === talk.current.id ? ' active' : ''),
                            title: c.title, onclick: () => { openChat(c.id); go({ view: 'chat' }); } },
      h('span', { text: c.title }),
      h('span', { class: 'del', text: '✕', title: 'delete chat',
                  onclick: e => { e.stopPropagation(); deleteChat(c.id); } }));
    return b;
  }));
}
$('#new-chat').onclick = () => { newChat(); go({ view: 'chat' }); };
bus.on('talk:chats', renderRecents);

// Which version is running — and what to do when it is not the newest.
// The server restarts onto new code (aria update); this page is then the
// old app talking to a new server, so it reloads itself — at once if
// nothing would be lost, otherwise when you click.
let bootCode = null;
function renderVersion(v) {
  let el = $('#version-now');
  if (!el) { el = h('button', { id: 'version-now', type: 'button' }); $('#side-foot').append(el); }
  if (v.code && bootCode === null) bootCode = v.code;
  const moved = !!v.code && v.code !== bootCode;       // the server restarted
  const draft = ($('#view-chat textarea') || {}).value || '';
  if (moved && !talk.busy && !draft.trim()) { location.reload(); return; }
  const when = v.date ? ' · ' + v.date : '';
  el.className = moved || v.stale ? 'attention' : '';
  const how = 'A newer version is on disk than the one running. Restart her onto it with:  aria update --restart  '
    + '(or: systemctl --user restart aria). If autopilot is mid-step, it begins that step again.';
  el.textContent = moved ? '⟳ updated — click to reload'
    : v.stale ? '⟳ update ready — restart'
    : v.commit ? 'version ' + v.commit + when : '';
  el.title = moved ? 'A.R.I.A. was updated and restarted; this window still shows the old app'
    : v.stale ? how
    : v.commit ? (v.branch || '') + ' @ ' + v.commit + (v.subject ? ' — ' + v.subject : '') + '\nUpdate with: aria update' : '';
  el.onclick = moved ? () => location.reload() : v.stale ? () => toast(how, 'info', 12000) : null;
  el.hidden = !el.textContent;
}

function renderState(st) {
  appState = st;
  const hl = st.health;
  $('#dot').className = 'dot' + (hl.ok ? ' ok' : '');
  $('#health-txt').textContent = hl.ok
    ? hl.model + (hl.smart && hl.smart !== hl.model ? ' · ' + hl.smart : '')
    : (hl.server ? 'model missing — ' + hl.model : 'model server offline');
  $('#health').title = hl.ok ? 'fast · smart tiers reachable' : (hl.error || '');
  const gz = st.garage || {}, gl = gz.loaded || [];
  let gnow = $('#garage-now');
  if (!gnow) { gnow = h('span', { id: 'garage-now' }); $('#health').after(gnow); }
  gnow.textContent = (gz.now && gz.now.message) ? '⏳ ' + gz.now.message
    : gl.length ? '◆ giant: ' + gl.map(r => r + (gz.labels[r] ? ' (' + gz.labels[r].split('-').slice(0, 2).join('-') + ')' : '')).join(', ')
    : '';
  gnow.hidden = !gnow.textContent;
  renderVersion(st.version || {});
  for (const [key, on] of Object.entries(st.switches)) {
    const el = $('#sw-' + key);
    if (el) el.classList.toggle('on', !!on);
  }
  const running = st.projects.filter(p => p.autopilot === 'running');
  const asking = st.projects.filter(p => !p.approved && p.has_plan);
  // tasks that wait for the owner's decision outrank "she is working"
  const owed = st.projects.reduce((n, p) => n + (p.needs_you || []).length, 0);
  const pp = $('#pip-projects');
  pp.className = 'pip' + (owed ? ' ask' : running.length ? ' live' : asking.length ? ' ask' : '');
  pp.textContent = owed ? String(owed) : running.length ? '' : asking.length ? String(asking.length) : '';
  pp.title = owed ? owed + ' task(s) waiting for your decision'
    : running.length ? 'autopilot is working'
    : asking.length ? asking.length + ' plan(s) waiting for your approval' : '';
  const pa = $('#pip-agents');
  pa.className = 'pip' + (st.missions ? ' count' : '');
  pa.textContent = st.missions ? String(st.missions) : '';
  $('#working-h').hidden = !running.length;
  clear($('#working'), running.map(p => {
    const owes = (p.needs_you || []).length;
    const parked = owes && p.phase === 'waiting';       // nothing else is left
    return h('a', { class: 'work' + (p.waiting ? ' waiting' : ''), href: '/projects?p=' + p.slug,
             dataset: { view: 'projects', slug: p.slug, tab: parked ? 'kanban' : 'live' },
             title: p.waiting || p.current || p.phase || 'working' },
      h('div', { class: 'w-name' }, h('span', { text: title(p.slug) }),
        owes && !parked ? h('span', { class: 'w-ask', text: 'needs you',
          title: owes + ' task(s) waiting for your decision' }) : null),
      h('div', { class: 'w-task', text: parked ? 'waiting on your decision'
        : p.waiting ? 'waiting — ' + p.waiting
        : (p.current || p.phase || 'working') + ' · iteration ' + p.iteration }),
      h('div', { class: 'w-bar' }, h('i', { style: {
        width: (p.total ? Math.round(100 * p.done / p.total) : 0) + '%' } })));
  }));
  bus.emit('state', st);
}

async function poll() {
  try {
    renderState(await api.get('/api/state'));
  } catch {
    $('#dot').className = 'dot';
    $('#health-txt').textContent = 'server unreachable';
  }
}
bus.on('projects:changed', poll);
bus.on('settings:changed', poll);

async function pollBoardCount() {           // the inbox pip
  try {
    const b = await api.get('/api/board');
    const n = b.inbox.length;
    const pb = $('#pip-board');
    pb.className = 'pip' + (n ? ' count' : '');
    pb.textContent = n ? String(n) : '';
    pb.title = n ? n + ' note(s) in the inbox' : '';
  } catch { /* the health line already says the server is away */ }
}
bus.on('pipeline:changed', pollBoardCount);

// ---------------------------------------------------------- mini avatar

(function avatar() {
  const cv = $('#avatar'), ctx = cv.getContext('2d');
  const pts = [], N = 90;
  for (let i = 0; i < N; i++) {
    const y = 1 - (i / (N - 1)) * 2, r = Math.sqrt(1 - y * y), th = i * 2.399963229728653;
    pts.push({ x: Math.cos(th) * r, y, z: Math.sin(th) * r });
  }
  const COLORS = { idle: [69, 214, 255], listening: [74, 222, 128],
                   thinking: [251, 191, 36], speaking: [69, 214, 255] };
  const cur = [69, 214, 255];
  let a = 0;
  (function frame() {
    requestAnimationFrame(frame);
    if (document.hidden) return;
    const tgt = COLORS[talk.state] || COLORS.idle;
    for (let i = 0; i < 3; i++) cur[i] += (tgt[i] - cur[i]) * .08;
    a += talk.state === 'thinking' ? .06 : talk.state === 'speaking' ? .035 : .012;
    ctx.clearRect(0, 0, 60, 60);
    const [r, g, b] = cur.map(Math.round);
    for (const p of pts) {
      const x = p.x * Math.cos(a) + p.z * Math.sin(a);
      const z = -p.x * Math.sin(a) + p.z * Math.cos(a);
      const persp = 1.6 / (1.6 + z);
      ctx.fillStyle = `rgba(${r},${g},${b},${.25 + (1 - (z + 1) / 2) * .65})`;
      ctx.beginPath();
      ctx.arc(30 + x * 24 * persp, 30 + p.y * 24 * persp, (1 - (z + 1) / 2) * 1.6 + .4, 0, 7);
      ctx.fill();
    }
  })();
})();

// ------------------------------------------------------------------ dock
// On every view except Chat and the Orb, A.R.I.A. is one line away: ask
// or command from wherever you are; her reply floats above the dock.

(function dock() {
  $('#dock-composer-slot').append(makeComposer({
    placeholder: 'Ask A.R.I.A. from here…  (“work on …”, “status”, “open the board”)',
    hint: false }));
  const box = $('#dock-reply');
  let timer = null, lastUser = '';
  const hide = () => { box.hidden = true; };
  const render = msg => {
    if (current === 'chat' || current === 'orb') return;
    if (msg.role === 'user') { lastUser = msg.content; return; }
    if (msg.role !== 'assistant' && msg.role !== 'sys') return;
    clear(box,
      h('div', { class: 'who' }, 'A.R.I.A.', h('span', { class: 'grow' }),
        h('button', { text: 'open chat', onclick: () => { hide(); go({ view: 'chat' }); } }),
        h('button', { text: '✕', title: 'dismiss', onclick: hide })),
      lastUser ? h('div', { class: 'asked', text: '“' + lastUser + '”' }) : null,
      h('div', { class: 'said', text: msg.content || msg.status || '…' }));
    box.hidden = false;
    clearTimeout(timer);
    if (!msg.pending) timer = setTimeout(hide, 14000);
  };
  bus.on('talk:add', render);
  bus.on('talk:update', render);
  box.addEventListener('mouseenter', () => clearTimeout(timer));
  box.addEventListener('mouseleave', () => { timer = setTimeout(hide, 6000); });
})();

// ------------------------------------------------------------------ boot

for (const v of Object.values(VIEWS)) v.init?.({ go, makeComposer });
renderRecents();
show(parse());
poll();
pollBoardCount();
setInterval(poll, 5000);
setInterval(pollBoardCount, 20000);

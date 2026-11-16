// The Projects view: pick a project, pass it through the gate, and watch
// autopilot work it — Kanban board, Gantt chart, live activity, the brief
// she wrote for herself, every change with its reasoning, and the report.

import { $, $$, h, clear, api, stream, bus, md, modal, confirmBox, toast,
         badge, ago, title } from './core.js';

const TABS = ['kanban', 'gantt', 'live', 'brief', 'changes', 'report'];
const PALETTE = ['#45d6ff', '#4ade80', '#fbbf24', '#c084fc', '#fb7185',
                 '#34d399', '#f97316', '#60a5fa'];
const PHASE_SAY = { assessing: 'Taking stock of the project…',
                    planning: 'Planning the next batch…',
                    reflecting: 'Updating the project memory…',
                    auditing: 'Auditing the work against the requirements…',
                    reporting: 'Writing the final report…',
                    starting: 'Starting up…' };
let cur = '', tab = 'kanban', data = null, list = [], visible = false;
let fullTimer = null, liveTimer = null, sig = '', busyPlan = false;
let goFn = null;

// ------------------------------------------------------------ loading

async function loadList() {
  try { list = (await api.get('/api/projects')).projects; }
  catch { list = []; }
  if (cur && !list.some(p => p.slug === cur)) cur = '';
  if (!cur && list.length) {
    cur = (list.find(p => p.autopilot === 'running')
        || list.find(p => !p.approved && p.has_plan) || list[0]).slug;
  }
  renderPicker();
}

async function refresh(force = false) {
  if (!cur) { data = null; render(true); return; }
  try {
    const next = await api.get('/api/project?slug=' + encodeURIComponent(cur));
    if (next.slug !== cur) return;                // the user moved on meanwhile
    data = next;
  } catch (e) {
    if (e.status === 404) { cur = ''; await loadList(); return refresh(true); }
    return;
  }
  render(force);
}

async function pollLive() {
  if (!visible || !cur || !data) return;
  const running = data.state.status === 'running';
  if (!running && tab !== 'live') return;
  let lv;
  try { lv = await api.get('/api/project/live?slug=' + encodeURIComponent(cur)); }
  catch { return; }
  if (lv.slug !== cur) return;
  const before = JSON.stringify([data.state.status, data.state.phase,
    (data.state.current || {}).id, data.state.waiting,
    data.plan.tasks.map(t => t.id + t.status + t.attempts)]);
  const after = JSON.stringify([lv.state.status, lv.state.phase,
    (lv.state.current || {}).id, lv.state.waiting,
    lv.tasks.map(t => t.id + t.status + t.attempts)]);
  data.state = lv.state;
  data.activity = lv.activity;
  data.live = lv.live;
  if (before !== after) { await refresh(); bus.emit('projects:changed'); return; }
  renderBar();
  if (tab === 'live') updateLive();
}

// ------------------------------------------------------------ top bar

function renderPicker() {
  const sel = h('select', { onchange: e => select(e.target.value) });
  const label = p => title(p.slug) + ' — ' + (
    p.autopilot === 'running' ? 'working' :
    !p.approved ? (p.has_plan ? 'needs your approval' : 'idea only') :
    p.autopilot === 'idle' ? 'ready' : p.autopilot);
  for (const p of list)
    sel.append(h('option', { value: p.slug, text: label(p), selected: p.slug === cur }));
  clear($('#proj-picker'), list.length ? sel : null);
}

function stateText(st) {
  if (st.status === 'running') {
    if (st.waiting) return ['waiting', 'waiting'];
    return ['running', 'working · iteration ' + st.iteration];
  }
  return [st.status, { idle: 'idle', stopped: 'stopped', complete: 'complete',
                       stalled: 'paused — stalled' }[st.status] || st.status];
}

function renderBar() {
  const has = !!data;
  for (const id of ['proj-steer', 'proj-plan', 'proj-go', 'proj-stop', 'proj-state'])
    $('#' + id).hidden = !has;
  $('#proj-tabs').hidden = !has;
  if (!has) return;
  const st = data.state, running = st.status === 'running';
  const [cls, text] = stateText(st);
  const pill = $('#proj-state');
  pill.className = 'pill ' + (data.approved ? cls : 'ask');
  pill.textContent = data.approved ? text : 'awaiting your approval';
  pill.title = st.waiting || st.stop_reason || '';
  const goBtn = $('#proj-go');
  goBtn.hidden = running;
  goBtn.textContent = !data.approved ? '✓ Approve & start'
    : (st.status === 'idle' ? '▶ Start autopilot' : '▶ Resume autopilot');
  goBtn.disabled = !data.approved && !data.has_plan;
  goBtn.title = !data.approved && !data.has_plan
    ? 'this idea has no plan yet — run develop on the Pipeline page' : '';
  $('#proj-stop').hidden = !running;
  $('#proj-steer').hidden = !data.approved;
  $('#proj-plan').hidden = !data.approved || running;
  $('#proj-plan').disabled = busyPlan;
  $('#proj-plan').textContent = busyPlan ? 'Planning…' : 'Plan';
  for (const b of $$('#proj-tabs button')) b.classList.toggle('on', b.dataset.tab === tab);
  const meta = $('#proj-meta');
  const s = st.stats || {};
  clear(meta,
    s.done ? h('span', { text: s.done + ' done' }) : null,
    s.failed ? h('span', { text: '· ' + s.failed + ' failed' }) : null,
    gitChip());
}

function gitChip() {
  const g = data.git || {};
  if (!g.repo) return null;
  return h('button', { class: 'badge', title: 'git: ' + (g.last || '') +
      (g.remote ? '\nremote: ' + g.remote : '\nno GitHub remote yet'),
      text: '⎇ ' + (g.branch || 'git') + (g.remote ? ' ↗' : ''),
      onclick: gitDialog });
}

function gitDialog() {
  const g = data.git;
  const body = h('div', {},
    h('dl', { class: 'kv' },
      h('dt', { text: 'branch' }), h('dd', { text: g.branch || '—' }),
      h('dt', { text: 'last commit' }), h('dd', { text: g.last || '—' }),
      h('dt', { text: 'remote' }), h('dd', { text: g.remote || 'none — not on GitHub yet' })),
    h('p', { class: 'hint', text: g.github_access
      ? 'Autopilot commits each verified task to aria/autopilot and never touches your base branch. Pushing opens (or updates) a pull request.'
      : 'GitHub is switched off in Settings — commits stay on this machine.' }));
  const actions = [];
  if (g.github_access && !g.remote)
    actions.push({ label: 'Publish to GitHub (private)', kind: 'primary', run: async close => {
      try { const r = await api.post('/api/github', { slug: cur, action: 'publish' });
            toast('Published: ' + r.url, 'good'); close(); refresh(true); }
      catch (e) { toast(e.message, 'bad'); } } });
  if (g.github_access && g.remote)
    actions.push({ label: 'Push & open pull request', kind: 'primary', run: async close => {
      try { const r = await api.post('/api/github', { slug: cur, action: 'push' });
            toast(r.say, 'good', 8000); close(); refresh(true); }
      catch (e) { toast(e.message, 'bad'); } } });
  modal('Repository — ' + title(cur), body, { actions });
}

// ------------------------------------------------------------ actions

async function select(slug) {
  cur = slug; data = null; sig = '';
  goFn({ view: 'projects', slug, tab }, { replace: true });
}

async function start() {
  if (!data) return;
  if (!data.approved) {
    const ok = await confirmBox('Approve this plan?', [
      h('p', {}, h('b', { text: title(cur) }), data.pitch ? ' — ' + data.pitch : ''),
      'Approving turns the proposed plan, exactly as it stands, into the project\'s requirements, moves the project into its own workspace, and lets A.R.I.A. start working on it.',
      'This is the one decision that is yours. You can stop her at any time.',
    ], 'Approve & start');
    if (!ok) return;
    try { await api.post('/api/autopilot', { slug: cur, action: 'approve' }); }
    catch (e) { toast(e.message, 'bad'); return; }
  }
  try {
    await api.post('/api/autopilot', { slug: cur, action: 'start' });
    toast('Autopilot engaged on ' + title(cur), 'good');
    tab = 'live';
  } catch (e) { toast(e.message, 'bad'); }
  bus.emit('projects:changed', cur);
  await loadList();
  refresh(true);
}

async function stop() {
  try {
    await api.post('/api/autopilot', { slug: cur, action: 'stop' });
    toast('Stopping — she finishes the step in flight, then writes the report.');
  } catch (e) { toast(e.message, 'bad'); }
  refresh(true);
}

function steer() {
  const box = h('textarea', { placeholder: 'e.g. "focus on the command line first", "use SQLite, not Postgres", "skip the web UI"' });
  const notes = (data.steering || '').split('\n').filter(l => l.startsWith('- ')).slice(-5);
  modal('Guide ' + title(cur), h('div', {},
    h('p', { class: 'hint', text: 'Guidance is binding on every plan from here on. Work that has not started is replanned around it; nothing stops.' }),
    box,
    notes.length ? h('div', { class: 'hint', style: { marginTop: '10px' } }, 'Earlier guidance:',
      h('ul', {}, notes.map(n => h('li', { text: n.slice(2) })))) : null), {
    actions: [{ label: 'Send guidance', kind: 'primary', run: async close => {
      const text = box.value.trim();
      if (!text) return;
      try { await api.post('/api/autopilot', { slug: cur, action: 'steer', text });
            toast('Noted — she will plan around it.', 'good'); close(); refresh(true); }
      catch (e) { toast(e.message, 'bad'); } } }] });
  box.focus();
}

async function planNow() {
  if (busyPlan) return;
  busyPlan = true; renderBar();
  try {
    await stream('/api/plan', { slug: cur }, ev => {
      if (ev.event === 'error') toast(ev.log, 'bad', 7000);
    });
  } catch (e) { toast(e.message, 'bad'); }
  busyPlan = false;
  refresh(true);
}

// ------------------------------------------------------------ tabs

function render(force = false) {
  renderBar();
  const body = $('#proj-body');
  if (!data) {
    clear(body, h('div', { class: 'empty' },
      h('div', { class: 'big', text: 'No projects yet' }),
      'Capture an idea in chat or on the Pipeline page. Once it has a plan it shows up here for your approval — after that A.R.I.A. works it on her own.'));
    return;
  }
  const s = JSON.stringify([tab, data.approved, data.state.status, data.state.phase,
    data.state.waiting, data.state.iteration, (data.state.current || {}).id,
    data.plan.tasks.map(t => [t.id, t.status, t.attempts]),
    data.changes.length, data.report.length, data.assessment.length,
    data.memory.length, data.steering.length]);
  if (!force && s === sig) { if (tab === 'live') updateLive(); return; }
  sig = s;
  const keep = body.scrollTop;
  clear(body, notices(), ({ kanban, gantt, live, brief, changes, report })[tab]());
  body.scrollTop = keep;
  if (tab === 'live') updateLive();
}

function notices() {
  const out = [], st = data.state;
  if (!data.approved) {
    out.push(h('div', { class: 'gatebar' },
      h('div', { class: 'g-t' }, h('b', { text: 'Waiting for your approval. ' }),
        data.has_plan ? 'Read the proposed plan, then approve it — that is the only step that needs you.'
                      : 'This idea has no plan yet; run develop on the Pipeline page.'),
      data.has_plan ? h('button', { class: 'btn', text: 'Read the plan', onclick: () => openDoc('proposed-plan') }) : null,
      data.has_plan ? h('button', { class: 'btn primary', text: '✓ Approve & start', onclick: start }) : null));
  }
  if (st.status === 'running' && st.waiting)
    out.push(h('div', { class: 'notice warn', text: 'Waiting: ' + st.waiting + '. She will pick up where she left off on her own.' }));
  if (st.status === 'stalled')
    out.push(h('div', { class: 'notice bad', text: 'She paused herself: ' + (st.stop_reason || 'no progress') + '. See the Report, give her guidance (Steer), then resume.' }));
  if (st.status === 'complete')
    out.push(h('div', { class: 'notice good', text: 'Run complete — ' + (st.stop_reason || 'the goal is met') + '. The Report lists every change.' }));
  if (st.git_off && st.status === 'running')
    out.push(h('div', { class: 'notice warn', text: 'Git commits are paused for this run: ' + st.git_off }));
  return out;
}

// ---- kanban

function taskCard(t) {
  const cls = 'task' + (t.status === 'inprogress' ? ' active' : '') +
    (t.status === 'reviewing' ? ' reviewing' : '') +
    (t.status === 'failed' ? ' failed' : '') + (t.status === 'skipped' ? ' skipped' : '');
  const meta = [t.phase, '~' + t.est_hours + 'h'];
  if (t.attempts > 1) meta.push('attempt ' + t.attempts);
  const model = t.model_used || t.model;
  return h('button', { class: cls, onclick: () => openTask(t) },
    h('span', { class: 'tid', text: t.id }),
    h('div', { class: 't', text: t.title }),
    h('div', { class: 'm', text: meta.join(' · ') }),
    badge(t.agent, 'who'),
    model ? badge(model.replace(/^ollama\//, ''), 'model') : null,
    (t.skills || []).map(sk => badge(sk, 'skill')),
    t.research ? badge('web', 'web') : null,
    t.status === 'failed' ? badge('failed', 'bad') : null,
    t.status === 'skipped' ? badge('skipped') : null);
}

function kanban() {
  const tasks = data.plan.tasks || [];
  const st = data.state;
  const done = new Set(tasks.filter(t => t.status === 'done').map(t => t.id));
  const known = new Set(tasks.map(t => t.id));
  const ready = t => t.depends_on.every(d => done.has(d) || !known.has(d));
  const closed = tasks.filter(t => ['done', 'failed', 'skipped'].includes(t.status)).reverse();
  const cols = [
    ['To Do', 'ready', tasks.filter(t => t.status === 'todo' && ready(t))],
    ['Planning', 'queued', tasks.filter(t => t.status === 'todo' && !ready(t))],
    ['In Progress', '', tasks.filter(t => t.status === 'inprogress')],
    ['Reviewing', 'verifying', tasks.filter(t => t.status === 'reviewing')],
    ['Done', data.plan.archived ? '+' + data.plan.archived + ' archived' : '', closed],
  ];
  const ghost = st.status === 'running' && PHASE_SAY[st.phase];
  if (!tasks.length && !ghost)
    return h('div', { class: 'empty' },
      h('div', { class: 'big', text: data.approved ? 'No tasks yet' : 'Not started' }),
      data.approved ? 'Press Start — she will take stock of the project, then plan it: every task gets an agent, the skills it needs, and a model.'
                    : 'Once you approve the plan, the task board fills in here.');
  return h('div', { id: 'kanban' }, cols.map(([name, sub, items]) => {
    const col = h('div', { class: 'kcol' },
      h('h2', {}, name, ' ', h('span', { class: 'n', text: items.length || '' }),
        sub ? h('span', { class: 'sub', text: sub }) : null));
    const cards = h('div', { class: 'kcards' });
    if (name === 'Planning' && ghost)
      cards.append(h('div', { class: 'task ghost' }, h('div', { class: 't', text: ghost })));
    items.forEach(t => cards.append(taskCard(t)));
    col.append(cards);
    return col;
  }));
}

// ---- gantt

function gantt() {
  const tasks = (data.plan.tasks || []).filter(t => t.status !== 'skipped');
  if (!tasks.length)
    return h('div', { class: 'empty' }, h('div', { class: 'big', text: 'No plan to chart yet' }),
      'The Gantt chart is drawn from the plan: each task\'s estimate and what it depends on.');
  const by = Object.fromEntries(tasks.map(t => [t.id, t]));
  const iters = [...new Set(tasks.map(t => t.iteration || 0))].sort((a, b) => a - b);
  const start = {}, end = {};
  let offset = 0;
  const rows = [];
  for (const it of iters) {              // batches run one after another
    const batch = tasks.filter(t => (t.iteration || 0) === it);
    const calc = (t, seen) => {
      if (end[t.id] !== undefined) return end[t.id];
      if (seen.has(t.id)) return offset;
      seen.add(t.id);
      const deps = t.depends_on.map(d => by[d]).filter(d => d && (d.iteration || 0) === it);
      start[t.id] = Math.max(offset, ...deps.map(d => calc(d, seen)));
      end[t.id] = start[t.id] + (t.est_hours || 1);
      return end[t.id];
    };
    batch.forEach(t => calc(t, new Set()));
    rows.push({ header: 'Iteration ' + it });
    batch.sort((a, b) => start[a.id] - start[b.id]).forEach(t => rows.push({ task: t }));
    offset = Math.max(offset, ...batch.map(t => end[t.id]));
  }
  const total = Math.max(1, offset);
  const host = h('div', { id: 'gantt' });
  const W = Math.max(760, ($('#proj-body').clientWidth || 1100) - 70);
  const LAB = Math.min(300, Math.round(W * .3)), PADR = 150, ROW = 30, TOP = 30;
  const plotW = W - LAB - PADR;
  const X = hrs => LAB + (hrs / total) * plotW;
  const H = TOP + rows.length * ROW + 10;
  const NS = 'http://www.w3.org/2000/svg';
  const el = (name, attrs, text) => {
    const n = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    if (text != null) n.textContent = text;
    return n;
  };
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H });
  const defs = el('defs');
  const marker = el('marker', { id: 'arr', viewBox: '0 0 8 8', refX: 7, refY: 4,
                                markerWidth: 7, markerHeight: 7, orient: 'auto' });
  marker.append(el('path', { d: 'M0,0 L8,4 L0,8 z', fill: '#4a6690' }));
  const stripes = el('pattern', { id: 'work', width: 8, height: 8,
    patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
  stripes.append(el('rect', { width: 4, height: 8, fill: 'rgba(255,255,255,.28)' }));
  defs.append(marker, stripes);
  svg.append(defs);
  // time axis
  const step = total <= 12 ? 1 : total <= 30 ? 2 : total <= 80 ? 5 : total <= 200 ? 10 : 25;
  for (let hrs = 0; hrs <= total + .001; hrs += step) {
    svg.append(el('line', { x1: X(hrs), y1: TOP - 8, x2: X(hrs), y2: H - 6,
                            stroke: '#162233', 'stroke-width': 1 }));
    svg.append(el('text', { x: X(hrs), y: 14, fill: '#4d5f77', 'font-size': 10.5,
                            'text-anchor': 'middle' }, hrs + 'h'));
  }
  const colors = {};
  let ci = 0;
  const yOf = {};
  rows.forEach((r, i) => { if (r.task) yOf[r.task.id] = TOP + i * ROW; });
  // dependency connectors first, so bars sit on top of them
  for (const r of rows) {
    const t = r.task;
    if (!t) continue;
    for (const d of t.depends_on) {
      if (yOf[d] === undefined || end[d] === undefined) continue;
      // leave from under the end of the dependency's bar, drop to this
      // row, arrive at the bar's start — clear of the labels beside bars
      const x2 = X(start[t.id]), y2 = yOf[t.id] + ROW / 2;
      const x1 = Math.min(X(end[d]) - 5, x2 - 9);
      const y1 = yOf[d] + (yOf[d] < yOf[t.id] ? 23 : 7);
      svg.append(el('path', { d: `M${x1},${y1} V${y2} H${x2 - 1}`,
        fill: 'none', stroke: '#4a6690', 'stroke-width': 1.3, 'marker-end': 'url(#arr)' }));
    }
  }
  rows.forEach((r, i) => {
    const y = TOP + i * ROW;
    if (r.header) {
      svg.append(el('text', { x: 2, y: y + 19, fill: '#7d8fa5', 'font-size': 10.5,
        'letter-spacing': 1.5 }, r.header.toUpperCase()));
      svg.append(el('line', { x1: 2, y1: y + 25, x2: W - 4, y2: y + 25, stroke: '#1a2638' }));
      return;
    }
    const t = r.task;
    if (!(t.phase in colors)) colors[t.phase] = PALETTE[ci++ % PALETTE.length];
    const col = t.status === 'failed' ? '#f87171' : colors[t.phase];
    const x = X(start[t.id]), w = Math.max(7, X(end[t.id]) - x);
    const g = el('g', { class: 'g-row', style: 'cursor:pointer' });
    g.append(el('rect', { class: 'hit', x: 0, y: y + 1, width: W, height: ROW - 2, fill: 'transparent' }));
    const max = Math.floor((LAB - 44) / 6.1);
    g.append(el('text', { x: 4, y: y + 19, fill: '#4d5f77', 'font-size': 10.5,
                          'font-family': 'monospace' }, t.id));
    g.append(el('text', { x: 38, y: y + 19, fill: t.status === 'done' ? '#7d8fa5' : '#d7e2ee',
      'font-size': 12 }, t.title.length > max ? t.title.slice(0, max - 1) + '…' : t.title));
    const bar = { x, y: y + 8, width: w, height: 14, rx: 4 };
    if (t.status === 'done') g.append(el('rect', { ...bar, fill: col }));
    else if (t.status === 'failed') g.append(el('rect', { ...bar, fill: col, opacity: .55 }));
    else {
      g.append(el('rect', { ...bar, fill: col, opacity: t.status === 'todo' ? .22 : .6,
                            stroke: col, 'stroke-width': 1 }));
      if (t.status === 'inprogress' || t.status === 'reviewing')
        g.append(el('rect', { ...bar, fill: 'url(#work)' }));
    }
    const mark = { done: '✓', failed: '✗', inprogress: '● working',
                   reviewing: '◐ reviewing' }[t.status] || '';
    g.append(el('text', { x: x + w + 7, y: y + 19, fill: '#7d8fa5', 'font-size': 10.5 },
      [t.agent, '~' + t.est_hours + 'h', mark].filter(Boolean).join(' · ')));
    g.append(el('title', {}, `${t.id} — ${t.title}\n${t.agent}` +
      (t.model_used || t.model ? ' on ' + (t.model_used || t.model) : '') +
      `\nstatus: ${t.status} · estimate ${t.est_hours}h` +
      (t.depends_on.length ? '\nafter: ' + t.depends_on.join(', ') : '')));
    g.addEventListener('click', () => openTask(t));
    svg.append(g);
  });
  const legend = h('div', { class: 'gantt-legend' },
    Object.entries(colors).map(([p, c]) => h('span', {}, h('i', { style: { background: c } }), p)),
    h('span', { class: 'grow' }),
    h('span', {}, h('i', { style: { background: '#7d8fa5' } }), 'done'),
    h('span', {}, h('i', { style: { background: 'repeating-linear-gradient(45deg,#7d8fa5 0 3px,#3a4658 3px 6px)' } }), 'in progress'),
    h('span', {}, h('i', { style: { background: 'transparent', border: '1px solid #7d8fa5' } }), 'to do'),
    h('span', { text: '→ depends on · estimated ' + Math.round(total * 10) / 10 + 'h of work' }));
  host.append(legend, svg);
  return host;
}

// ---- live

function live() {
  return h('div', { id: 'live' },
    h('div', { class: 'panel' }, h('h2', {}, 'Right now'), h('div', { class: 'p-body', id: 'live-now' })),
    h('div', { class: 'panel' }, h('h2', {}, 'Activity'), h('div', { class: 'p-body feed', id: 'live-feed' })));
}

function updateLive() {
  const now = $('#live-now'), feed = $('#live-feed');
  if (!now || !data) return;
  const st = data.state, lv = data.live || {}, c = st.current;
  const kids = [];
  if (st.status !== 'running') {
    kids.push(h('div', { class: 'now', text: { idle: 'Autopilot is not running.',
      stopped: 'Stopped at your request.', complete: 'Run complete.',
      stalled: 'Paused — she stopped making progress.' }[st.status] || st.status }));
    kids.push(h('div', { class: 'now-sub', text: st.stop_reason || (data.approved
      ? 'Press Start and this panel shows her thinking as it happens.'
      : 'Approve the plan to begin.') }));
  } else if (st.waiting) {
    kids.push(h('div', { class: 'now', text: 'Waiting' }));
    kids.push(h('div', { class: 'now-sub', text: st.waiting }));
  } else if (c) {
    kids.push(h('div', { class: 'now', text: c.title }));
    kids.push(h('div', { class: 'now-sub' }, badge(c.agent, 'who'), badge(c.model, 'model'),
      c.attempt > 1 ? badge('attempt ' + c.attempt) : null,
      ' ' + (st.phase === 'reviewing' ? 'being verified' : 'in progress') + ' · started ' + ago(c.started)));
  } else {
    kids.push(h('div', { class: 'now', text: PHASE_SAY[st.phase] || 'Working…' }));
    kids.push(h('div', { class: 'now-sub', text: 'iteration ' + st.iteration }));
  }
  if (st.status === 'running' && !st.waiting && (lv.text || lv.label)) {
    kids.push(h('div', { class: 'hint', text: (lv.label || '') + (lv.text ? '' : ' …') }));
    if (lv.text) kids.push(h('div', { class: 'tokens live', text: lv.text }));
  }
  const tok = now.querySelector('.tokens');
  const pinned = !tok || tok.scrollHeight - tok.scrollTop - tok.clientHeight < 60;
  clear(now, kids);
  const tok2 = now.querySelector('.tokens');
  if (tok2 && pinned) now.scrollTop = now.scrollHeight;
  const stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
  const own = cur + ': ';              // every line here is about this project
  clear(feed, (data.activity || []).map(ev =>
    h('div', { class: 'ev ' + ev.kind }, h('time', { text: (ev.t || '').slice(11, 19) }),
      h('span', { text: ev.text.startsWith(own) ? ev.text.slice(own.length) : ev.text }))));
  if (!(data.activity || []).length)
    feed.append(h('div', { class: 'dim', text: 'Nothing yet.' }));
  if (stick) feed.scrollTop = feed.scrollHeight;
}

// ---- brief

// her documents open with their own "# Title" and a dateline; inside a
// titled panel that is noise
const body = text => md(text.replace(/^# .*\n+(\*[^\n]*\*\n+)?/, ''));

function panel(name, node, extra) {
  return h('div', { class: 'panel' }, h('h2', {}, name, h('span', { class: 'grow' }), extra || null),
    h('div', { class: 'p-body' }, node));
}

function brief() {
  const docs = data.auto_dir === '.aria' ? h('span', {},
    h('button', { class: 'btn small', text: 'Goal', onclick: () => openFile('.aria/GOAL.md') })) : h('span', {},
    h('button', { class: 'btn small', text: 'Requirements', onclick: () => openDoc('requirements') }), ' ',
    h('button', { class: 'btn small', text: 'Plan', onclick: () => openDoc('proposed-plan') }), ' ',
    h('button', { class: 'btn small', text: 'Research', onclick: () => openDoc('research') }), ' ',
    h('button', { class: 'btn small', text: 'Idea', onclick: () => openDoc('idea') }));
  const notes = (data.steering || '').split('\n').filter(l => l.startsWith('- '));
  return h('div', {},
    h('div', { class: 'row', style: { marginBottom: '12px' } },
      h('span', { class: 'hint', text: 'Source documents:' }), docs),
    h('div', { id: 'brief' },
      panel('Assessment — what needs doing, what she can do, the gaps',
        data.assessment ? body(data.assessment)
          : h('div', { class: 'dim', text: 'Written when autopilot first starts: what the project needs, which agents, skills, models and tools cover it, and what they cannot do.' })),
      panel('Memory — what she has learned so far',
        data.memory ? body(data.memory)
          : h('div', { class: 'dim', text: 'Rewritten after every batch: the state of the project, decisions made, lessons, and what is still to do. This — not a transcript — is what the next plan starts from.' })),
      panel('Your guidance',
        notes.length ? h('ul', {}, notes.map(n => h('li', { text: n.slice(2) })))
          : h('div', { class: 'dim', text: 'None yet. Use Steer to redirect her without stopping.' }),
        data.approved ? h('button', { class: 'btn small', text: 'Steer', onclick: steer }) : null)));
}

// ---- changes

function fileLink(path) {
  return h('a', { class: 'filelink', text: path, onclick: () => openFile(path) });
}

function changeCard(r) {
  const files = (r.files || []).map(f => h('div', {}, fileLink(f.path), ' ',
    h('span', { class: 'delta' }, f.action + ' ',
      h('span', { class: 'add', text: '+' + f.added }), ' ',
      h('span', { class: 'del', text: '−' + f.removed }))));
  const refs = [fileLink(r.output)];
  if (r.commit) refs.push(' · commit ', h('code', { text: r.commit }));
  for (const s of r.sources || [])
    refs.push(' · ', h('a', { href: s.url, target: '_blank', rel: 'noopener noreferrer', text: s.title || s.url }));
  const row = (k, v) => v && (!Array.isArray(v) || v.length) ? [h('dt', { text: k }), h('dd', {}, v)] : [];
  return h('div', { class: 'change' + (r.status === 'failed' ? ' failed' : '') },
    h('div', { class: 'c-head' },
      h('span', { class: 'c-title', text: r.title }),
      badge(r.task), badge(r.agent, 'who'),
      r.model ? badge(r.model.replace(/^ollama\//, ''), 'model') : null,
      (r.skills || []).map(s => badge(s, 'skill')),
      r.status === 'failed' ? badge('failed', 'bad') : null,
      r.attempts > 1 ? badge('attempt ' + r.attempts) : null,
      h('span', { class: 'c-when', text: (r.t || '').replace('T', ' ').slice(0, 16) })),
    h('dl', {},
      row('Changed', files.length ? files : (r.status === 'failed' ? 'nothing — the attempts were rolled back' : 'no files — written output only')),
      row('Why', r.why),
      row('What', r.notes),
      row(r.status === 'failed' ? 'Rejected' : 'Verified', (r.feedback || 'passed') + (r.verified === false && r.status !== 'failed' ? ' (unreviewed)' : '')),
      row('References', refs)));
}

function changes() {
  const rows = data.changes || [];
  if (!rows.length)
    return h('div', { class: 'empty' }, h('div', { class: 'big', text: 'No changes yet' }),
      'Every task she completes is recorded here: the files it touched, why it was done, how it was verified, and what it drew on.');
  const out = [];
  const keys = [...new Set(rows.map(r => r.run + '/' + r.iteration))].reverse();
  for (const key of keys) {
    const [run, it] = key.split('/');
    out.push(h('div', { class: 'iter-h', text: 'Run ' + run + ' · iteration ' + it }));
    rows.filter(r => r.run + '/' + r.iteration === key).reverse().forEach(r => out.push(changeCard(r)));
  }
  return h('div', {}, out);
}

// ---- report

function report() {
  if (!data.report)
    return h('div', { class: 'empty' }, h('div', { class: 'big', text: 'No report yet' }),
      'When a run ends — because you stopped it, the goal was met, or she paused herself — the final report appears here: every change she made, with references and reasoning.');
  return h('div', { class: 'doc' }, md(data.report));
}

// ------------------------------------------------------------ dialogs

async function openFile(path) {
  try {
    const r = await api.get('/api/file?slug=' + encodeURIComponent(cur) + '&path=' + encodeURIComponent(path));
    modal(path, /\.md$/i.test(path) ? md(r.content) : h('pre', { class: 'raw', text: r.content }), { wide: true });
  } catch {
    toast(path + ' is not in the project (it may have been rolled back)', 'bad');
  }
}

async function openDoc(name) {
  for (const path of ['docs/' + name + '.md', name + '.md']) {
    try {
      const r = await api.get('/api/file?slug=' + encodeURIComponent(cur) + '&path=' + encodeURIComponent(path));
      modal(title(cur) + ' — ' + name.replace('-', ' '), md(r.content), { wide: true });
      return;
    } catch { /* try the next location */ }
  }
  toast('No ' + name.replace('-', ' ') + ' document for this project yet.', 'bad');
}

async function openTask(t) {
  const model = t.model_used || t.model || 'agent default';
  const body = h('div', {},
    h('dl', { class: 'kv' },
      h('dt', { text: 'status' }), h('dd', { text: t.status + (t.attempts > 1 ? ' · attempt ' + t.attempts : '') }),
      h('dt', { text: 'agent' }), h('dd', { text: t.agent }),
      h('dt', { text: 'model' }), h('dd', { text: model }),
      h('dt', { text: 'skills' }), h('dd', { text: (t.skills || []).join(', ') || '—' }),
      h('dt', { text: 'depends on' }), h('dd', { text: t.depends_on.join(', ') || '—' }),
      h('dt', { text: 'done when' }), h('dd', { text: t.done_when }),
      t.why ? [h('dt', { text: 'why' }), h('dd', { text: t.why })] : null,
      t.feedback ? [h('dt', { text: 'review' }), h('dd', { text: t.feedback })] : null,
      (t.files || []).length ? [h('dt', { text: 'files' }), h('dd', {}, t.files.map(f => h('div', {}, fileLink(f))))] : null),
    h('div', { class: 'section-h', text: 'What to produce' }),
    h('p', { text: t.detail || '—' }),
    h('div', { id: 'task-out' }));
  modal(t.id + ' — ' + t.title, body, { wide: true });
  if (!t.attempts) return;                       // nothing has been run yet
  try {
    const r = await api.get('/api/file?slug=' + encodeURIComponent(cur) +
      '&path=' + encodeURIComponent(data.auto_dir + '/task-' + t.id + '.md'));
    const out = $('#task-out');
    if (out) clear(out, h('div', { class: 'section-h', text: 'Output (every attempt)' }),
      h('pre', { class: 'raw', text: r.content }));
  } catch { /* not run yet */ }
}

// ------------------------------------------------------------ lifecycle

export function init({ go }) {
  goFn = go;
  $('#proj-go').onclick = start;
  $('#proj-stop').onclick = stop;
  $('#proj-steer').onclick = steer;
  $('#proj-plan').onclick = planNow;
  for (const b of $$('#proj-tabs button'))
    b.onclick = () => { tab = b.dataset.tab; go({ view: 'projects', slug: cur, tab }, { replace: true }); };
  bus.on('projects:changed', async slug => {
    if (!visible) return;
    await loadList();
    if (typeof slug === 'string' && slug && slug !== cur && !data) cur = slug;
    refresh();
  });
  let resizeTimer = null;
  addEventListener('resize', () => {
    if (!visible || tab !== 'gantt') return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => render(true), 150);
  });
}

export async function show({ slug, tab: wanted }) {
  visible = true;
  if (TABS.includes(wanted)) tab = wanted;
  const changed = slug && slug !== cur;
  if (changed) { cur = slug; data = null; sig = ''; }
  render(true);
  await loadList();
  await refresh(true);
  clearInterval(fullTimer); clearInterval(liveTimer);
  fullTimer = setInterval(() => { if (visible) { loadList(); refresh(); } }, 10000);
  liveTimer = setInterval(pollLive, 1500);
}

export function hide() {
  visible = false;
  clearInterval(fullTimer); clearInterval(liveTimer);
}

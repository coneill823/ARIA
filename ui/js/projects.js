// The Projects view: pick a project, pass it through the gate, and watch
// autopilot work it — Kanban board, Gantt chart, live activity, the files
// themselves, the brief she wrote for herself, every change with its
// reasoning, and every run's report.

import { $, $$, h, clear, api, stream, bus, md, modal, confirmBox, toast,
         badge, ago, title, resolvePath, store } from './core.js';

const TABS = ['kanban', 'gantt', 'live', 'files', 'run', 'brief', 'changes', 'report'];
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
// files: the project's file list (and where it lives on disk)
let filesInfo = null, fileSet = new Set(), filesSig = '', fileSel = '';
let fileView = null, showRecords = false, openDirs = new Set();
// history: which report is shown, the full change record, open Gantt bands
let reportSel = '', reportCache = {}, allChanges = null, ganttOpen = new Set();
// live: her earlier outputs (newest first), the one being looked at (null =
// right now), and those already fetched in full
let liveOuts = [], liveSel = null, liveOut = {};
// files: a file's history, and the change being compared (null = the file)
let fileHist = null, fileDiff = null, fileMode = 'file';
let diffSplit = store.get('aria_diff_split', true);
// run: how the project is started, the program you started, its output
let runInfo = null, runText = '', runNext = 0, runTimer = null, runStamp = 0;
let checkRes = null, checking = false;

const fmtDur = s => {
  if (s > 0 && s < 1) return '<1s';
  s = Math.max(0, Math.round(s || 0));
  if (s < 60) return s + 's';
  if (s < 600) return Math.floor(s / 60) + 'm ' + (s % 60 ? (s % 60) + 's' : '').trim();
  if (s < 3600) return Math.round(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h' + (Math.round(s % 3600 / 60) ? ' ' + Math.round(s % 3600 / 60) + 'm' : '');
  return Math.floor(s / 86400) + 'd ' + Math.round(s % 86400 / 3600) + 'h';
};
const fmtSize = n => n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB'
  : (n / 1048576).toFixed(1) + ' MB';
// a model spec the way a person would say it: "giant/coder" and "coder"
// are the coder giant, "ollama/qwen3:30b" is qwen3:30b
const GIANT = /^(?:giant\/)?(general|research|coder)$/;
const isGiant = spec => GIANT.test(spec || '');
const modelName = spec => {
  const g = GIANT.exec(spec || '');
  return g ? g[1] + ' giant' : (spec || '').replace(/^ollama\//, '');
};
// how long a task takes on the chart: measured, else predicted from the
// measured pace, else (nothing has finished yet) the planner's own guess
const taskSecs = t => t.took || (['inprogress', 'reviewing'].includes(t.status)
    ? Math.max(t.elapsed || 0, t.predict || 0) : 0)
  || t.predict || (t.est_hours || 1) * (data.pace || 3600);
const timeNote = t => t.took ? 'took ' + fmtDur(t.took)
  : ['inprogress', 'reviewing'].includes(t.status) && t.elapsed
    ? fmtDur(t.elapsed) + ' so far' + (t.predict ? ' (≈ ' + fmtDur(t.predict) + ')' : '')
  : t.predict ? '≈ ' + fmtDur(t.predict)
  : t.status === 'todo' ? 'size ' + (t.est_hours || 1) : '';

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
    loadFiles();
  } catch (e) {
    if (e.status === 404) { cur = ''; await loadList(); return refresh(true); }
    return;
  }
  render(force);
}

async function loadFiles(force = false) {
  if (!cur || !data) return;
  const want = cur + '|' + data.changes_total + '|' + (data.reports || []).length
    + '|' + data.plan.tasks.filter(t => t.status === 'done').length;
  if (!force && want === filesSig) return;
  try {
    const got = await api.get('/api/files?slug=' + encodeURIComponent(cur));
    if (got.slug !== cur) return;
    filesInfo = got; filesSig = want;
    fileSet = new Set(got.files.map(f => f.path));
    // something changed: the selected file's history may have grown
    if (fileHist && !fileHist.loading) fileHist = null;
    if (visible) render(true);            // paths in what is shown become links
  } catch { /* the page still works without the list */ }
}

// What the markdown renderer needs to turn a file reference into a link:
// `base` is where the document itself lives inside the project.
function links(base = '') {
  return {
    file: ref => {
      const plain = ref.replace(/^\.\//, '').replace(/[.,;:]$/, '');
      if (fileSet.has(plain)) return plain;
      const rel = base ? resolvePath(base, ref) : null;
      return rel && fileSet.has(rel) ? rel : null;
    },
    open: path => openFile(path),
  };
}
const autoDoc = name => (data.auto_dir || 'autopilot') + '/' + name;

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
    data.plan.tasks.map(t => t.id + t.status + t.attempts + (t.needs_human ? '?' : ''))]);
  const after = JSON.stringify([lv.state.status, lv.state.phase,
    (lv.state.current || {}).id, lv.state.waiting,
    lv.tasks.map(t => t.id + t.status + t.attempts + (t.needs_human ? '?' : ''))]);
  data.state = lv.state;
  data.activity = lv.activity;
  data.live = lv.live;
  liveOuts = lv.outputs || [];
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
    p.autopilot === 'idle' ? 'ready' :
    p.autopilot === 'budget' ? 'budget reached' : p.autopilot) +
    ((p.needs_you || []).length ? ' · needs your decision' : '');
  for (const p of list)
    sel.append(h('option', { value: p.slug, text: label(p), selected: p.slug === cur }));
  clear($('#proj-picker'), list.length ? sel : null);
}

// she has done everything that does not need the owner and is parked
const onYou = () => data.state.status === 'running' && data.state.phase === 'waiting'
  && (data.needs_you || []).length > 0;

function stateText(st) {
  if (st.status === 'running') {
    if (onYou()) return ['ask', 'waiting on you'];
    if (st.waiting) return ['waiting', 'waiting'];
    return ['running', 'working · iteration ' + st.iteration];
  }
  return [st.status, { idle: 'idle', stopped: 'stopped', complete: 'complete',
                       stalled: 'paused — stalled',
                       budget: 'paused — budget reached' }[st.status] || st.status];
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
    h('button', { class: 'badge', text: '📁 ' + shortPath(data.path),
      title: 'The project lives in ' + data.path + ' — click to browse its files',
      onclick: () => setTab('files') }),
    gitChip());
}

const shortPath = p => {
  const parts = (p || '').split('/').filter(Boolean);
  return parts.length > 2 ? '…/' + parts.slice(-2).join('/') : p;
};

function setTab(name) {
  tab = name;
  goFn({ view: 'projects', slug: cur, tab, file: fileSel }, { replace: true });
}

async function openOnDesktop(how, path = '') {
  try {
    const r = await api.post('/api/open', { slug: cur, how, path });
    toast(r.say.charAt(0).toUpperCase() + r.say.slice(1) + '.', 'good');
  } catch (e) { toast(e.message, 'bad', 9000); }
}

async function copyText(text, what) {
  try { await navigator.clipboard.writeText(text); toast(what + ' copied.', 'good'); }
  catch { toast(text, 'info', 9000); }
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

function forget() {                 // everything that belongs to one project
  data = null; sig = ''; filesInfo = null; fileSet = new Set(); filesSig = '';
  fileSel = ''; fileView = null; reportSel = ''; reportCache = {};
  allChanges = null; ganttOpen = new Set(); openDirs = new Set();
  liveOuts = []; liveSel = null; fileHist = null; fileDiff = null; fileMode = 'file';
  runInfo = null; runText = ''; runNext = 0; checkRes = null; checking = false;
  clearTimeout(runTimer);
}

async function select(slug) {
  cur = slug; forget();
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
    data.plan.tasks.map(t => [t.id, t.status, t.attempts, !!t.needs_human]),
    data.changes.length, data.report.length, data.assessment.length,
    data.memory.length, data.steering.length, (data.reports || []).length,
    (data.history || []).length, data.pace, fileSet.size, fileSel,
    !!fileView && fileView.path, showRecords, reportSel, !!allChanges,
    [...ganttOpen].join(), [...openDirs].join(), runStamp, checking,
    (data.review || '').length, (data.state.refine || {}).stage]);
  if (!force && s === sig) { if (tab === 'live') updateLive(); return; }
  sig = s;
  const keep = body.scrollTop;
  clear(body, notices(), ({ kanban, gantt, live, files, run: runTab, brief, changes, report })[tab]());
  if (tab === 'run') afterRunDraw();
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
  const owed = data.needs_you || [];
  if (owed.length) {
    const find = id => data.plan.tasks.find(t => t.id === id);
    out.push(h('div', { class: 'gatebar' },
      h('div', { class: 'g-t' },
        h('b', { text: owed.length === 1 ? 'One task is yours to decide. '
                                         : owed.length + ' tasks are yours to decide. ' }),
        onYou() ? 'Everything that does not depend on your answer is done — she is waiting for you.'
          : st.status === 'running' ? 'She is carrying on with everything that does not depend on your answer.'
          : 'Nothing that builds on them can move until you answer.'),
      owed.slice(0, 3).map(g => h('button', { class: 'btn', title: g.why || '',
        text: g.id + ' — ' + (g.title.length > 34 ? g.title.slice(0, 33) + '…' : g.title),
        onclick: () => openTask(find(g.id)) })),
      owed.length > 3 ? h('span', { class: 'hint', text: '+' + (owed.length - 3) + ' more on the board' }) : null));
  }
  if (st.status === 'running' && st.waiting && !onYou())
    out.push(h('div', { class: 'notice warn', text: 'Waiting: ' + st.waiting + '. She will pick up where she left off on her own.' }));
  if (st.status === 'budget')
    out.push(h('div', { class: 'notice warn', text: 'She paused at the budget you set: ' + (st.stop_reason || 'budget reached') + '. Resume begins a new run with a fresh budget — change the limits in Settings.' }));
  if (st.status === 'stalled')
    out.push(h('div', { class: 'notice bad', text: 'She paused herself: ' + (st.stop_reason || 'no progress') + '. See the Report, give her guidance (Steer), then resume.' }));
  if (st.status === 'complete')
    out.push(h('div', { class: 'notice good row' },
      h('span', { class: 'grow', text: 'Run complete — ' + (st.stop_reason || 'the goal is met') + '. The Report lists every change.' }),
      tab !== 'run' ? h('button', { class: 'btn small', text: '▶ Try it', title: 'run the software yourself', onclick: () => setTab('run') }) : null));
  const ref = st.refine;
  if (st.status === 'running' && ref && ref.stage === 'review')
    out.push(h('div', { class: 'notice', text: 'Reviewing the whole codebase — ' + (ref.total - (ref.queue || []).length) + ' of ' + ref.total +
      ' files read, ' + (ref.findings || []).length + ' finding(s) so far. Refinements follow; each must leave the code\'s behaviour unchanged.' }));
  if (st.git_off && st.status === 'running')
    out.push(h('div', { class: 'notice warn', text: 'Git commits are paused for this run: ' + st.git_off }));
  return out;
}

// ---- kanban

function taskCard(t) {
  const gated = t.needs_human && t.status === 'todo';
  const cls = 'task' + (t.status === 'inprogress' ? ' active' : '') +
    (t.status === 'reviewing' ? ' reviewing' : '') + (gated ? ' gated' : '') +
    (t.status === 'failed' ? ' failed' : '') + (t.status === 'skipped' ? ' skipped' : '');
  const said = (t.decision || {}).answer;
  const meta = [t.phase, timeNote(t)].filter(Boolean);
  if (t.attempts > 1) meta.push('attempt ' + t.attempts);
  const model = t.model_used || t.model;
  return h('button', { class: cls, onclick: () => openTask(t) },
    h('span', { class: 'tid', text: t.id }),
    h('div', { class: 't', text: t.title }),
    h('div', { class: 'm', text: meta.join(' · ') }),
    badge(t.agent, 'who'),
    model ? h('span', { class: 'badge model' + (isGiant(model) ? ' giant' : ''),
      title: t.model_why ? modelName(model) + ' — ' + t.model_why : 'the model this task runs on',
      text: modelName(model) }) : null,
    t.hard && !isGiant(model) ? h('span', { class: 'badge', text: 'hard',
      title: 'the planner marked this as hard — no giant could take it' }) : null,
    (t.skills || []).map(sk => badge(sk, 'skill')),
    t.research ? badge('web', 'web') : null,
    t.refine ? badge('refine', 'skill') : null,
    gated ? badge('needs you', 'ask') : null,
    t.held && t.status === 'todo' ? badge('behind a decision', 'ask') : null,
    said === 'approved' ? badge('approved by you', 'ok') : null,
    t.status === 'failed' ? badge('failed', 'bad') : null,
    t.status === 'skipped' ? badge(said === 'declined' ? 'declined by you' : 'skipped') : null);
}

function kanban() {
  const tasks = data.plan.tasks || [];
  const st = data.state;
  const done = new Set(tasks.filter(t => t.status === 'done').map(t => t.id));
  const known = new Set(tasks.map(t => t.id));
  const ready = t => t.depends_on.every(d => done.has(d) || !known.has(d));
  const closed = tasks.filter(t => ['done', 'failed', 'skipped'].includes(t.status)).reverse();
  const first = (a, b) => (b.needs_human ? 1 : 0) - (a.needs_human ? 1 : 0);
  const cols = [
    ['To Do', 'ready', tasks.filter(t => t.status === 'todo' && ready(t)).sort(first)],
    ['Planning', 'queued', tasks.filter(t => t.status === 'todo' && !ready(t))],
    ['In Progress', '', tasks.filter(t => t.status === 'inprogress')],
    ['Reviewing', 'verifying', tasks.filter(t => t.status === 'reviewing')],
    ['Done', '', closed],
  ];
  const earlier = (data.history || []).slice().reverse();
  const ghost = st.status === 'running' && (PHASE_SAY[st.phase] ||
    (/^reviewing the code/.test(st.phase || '') ? st.phase.charAt(0).toUpperCase() + st.phase.slice(1) + '…' : ''));
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
    if (name === 'Done' && earlier.length) {
      // older finished work is kept off the board to keep it quick — it
      // is all still here
      const more = h('button', { class: 'btn small more', text: 'Show ' + earlier.length + ' earlier',
        onclick: () => { more.remove(); earlier.forEach(t => cards.append(taskCard(t))); } });
      cards.append(more);
    }
    col.append(cards);
    return col;
  }));
}

// ---- gantt
//
// Everything the project has planned, in measured time — and the road
// still ahead. The newest work is at the top: first the milestones not
// yet reached (the furthest highest), then the current batch, then older
// batches folded to a line each. A finished task is as long as it took, a
// pending one as long as tasks of its size have been taking here, and a
// milestone ahead as long as its estimated number of tasks would take.
// Arrows are dependencies: a bar cannot start before what points at it.

let ganttRun = store.get('aria_gantt_run', false);     // only the current run
let ganttRoad = store.get('aria_gantt_road', true);    // the road ahead

function ganttOptions(road, hidden) {
  const opt = (label, on, set, tip) => {
    const cb = h('input', { type: 'checkbox' });
    cb.checked = on;
    cb.onchange = () => { set(cb.checked); render(true); };
    return h('label', { class: 'gopt', title: tip }, cb, ' ' + label);
  };
  const reached = road.filter(m => m.status === 'done').length;
  return h('div', { class: 'gantt-opts' },
    opt('Current run only', ganttRun, v => { ganttRun = v; store.set('aria_gantt_run', v); },
        'hide everything from earlier runs'),
    opt('Road ahead', ganttRoad, v => { ganttRoad = v; store.set('aria_gantt_road', v); },
        'the milestones not yet reached, and what each one waits for'),
    ganttRun && hidden ? h('span', { class: 'hint', text: hidden + ' earlier task' + (hidden === 1 ? '' : 's') + ' hidden' }) : null,
    h('span', { class: 'grow' }),
    road.length ? h('span', { class: 'road-sum', title: 'the road to the goal — click a milestone in the chart for what it needs' },
      road.map(m => h('i', { class: m.status, title: m.id + ' — ' + m.title + ' (' + ROAD_SAY[m.status] + ')' })),
      ' ' + reached + ' of ' + road.length + ' milestones reached') : null);
}

const ROAD_SAY = { done: 'reached', active: 'in progress', todo: 'ahead', skipped: 'not needed' };

function openMilestone(m, road) {
  const title = id => { const x = road.find(r => r.id === id); return x ? id + ' — ' + x.title : id; };
  const mine = [...(data.plan.tasks || []), ...(data.history || [])].filter(t => t.milestone === m.id);
  const after = road.filter(r => r.depends_on.includes(m.id));
  modal(m.id + ' — ' + m.title, h('div', {},
    h('dl', { class: 'kv' },
      h('dt', { text: 'status' }), h('dd', { text: ROAD_SAY[m.status] || m.status }),
      h('dt', { text: 'reached when' }), h('dd', { text: m.outcome || '—' }),
      h('dt', { text: 'waits for' }), h('dd', { text: m.depends_on.map(title).join('; ') || 'nothing — it can start at any time' }),
      h('dt', { text: 'unlocks' }), h('dd', { text: after.map(r => r.id + ' — ' + r.title).join('; ') || '—' }),
      h('dt', { text: 'work' }), h('dd', { text: m.done + ' task(s) done' + (m.open ? ', ' + m.open + ' open' : '')
        + (m.failed ? ', ' + m.failed + ' failed' : '') + ' — estimated about ' + (m.est_tasks || 3) + ' in all' })),
    mine.length ? h('div', { class: 'ms-tasks' }, mine.map(t =>
      h('button', { class: 'btn small', text: t.id + ' — ' + t.title + ' (' + t.status + ')', onclick: () => openTask(t) }))) : null));
}

function gantt() {
  const st = data.state;
  const firstIt = st.run_first_iteration || 0;
  const all = [...(data.history || []), ...(data.plan.tasks || [])]
    .filter(t => t.status !== 'skipped');
  const tasks = ganttRun ? all.filter(t => (t.iteration || 0) > firstIt) : all;
  const road = (data.roadmap || {}).milestones || [];
  const ahead = ganttRoad ? road.filter(m => m.status === 'todo' || m.status === 'active') : [];
  const options = ganttOptions(road, all.length - tasks.length);
  if (!tasks.length && !ahead.length)
    return h('div', { id: 'gantt' }, options,
      h('div', { class: 'empty' }, h('div', { class: 'big', text: all.length ? 'Nothing in this run yet' : 'No plan to chart yet' }),
        all.length ? 'Untick “Current run only” to see the earlier runs.'
                   : 'The Gantt chart is drawn from the plan: what depends on what, and how long each task takes.'));
  const by = Object.fromEntries(tasks.map(t => [t.id, t]));
  const iters = [...new Set(tasks.map(t => t.iteration || 0))].sort((a, b) => a - b);
  const open = it => {                    // recent or unfinished batches start open
    const batch = tasks.filter(t => (t.iteration || 0) === it);
    const live = batch.some(t => !['done', 'failed'].includes(t.status));
    const dflt = live || iters.indexOf(it) >= iters.length - 1;
    return ganttOpen.has(it) ? !dflt : dflt;
  };
  const start = {}, end = {};
  let offset = 0, spent = 0, left = 0;
  const bands = [];
  for (const it of iters) {              // batches run one after another
    const batch = tasks.filter(t => (t.iteration || 0) === it);
    const from = offset;
    const calc = (t, seen) => {
      if (end[t.id] !== undefined) return end[t.id];
      if (seen.has(t.id)) return offset;
      seen.add(t.id);
      const deps = t.depends_on.map(d => by[d]).filter(d => d && (d.iteration || 0) === it);
      start[t.id] = Math.max(offset, ...deps.map(d => calc(d, seen)));
      end[t.id] = start[t.id] + taskSecs(t);
      return end[t.id];
    };
    batch.forEach(t => calc(t, new Set()));
    for (const t of batch) {
      if (t.took) spent += t.took;
      else if (!['done', 'failed'].includes(t.status)) left += Math.max(0, taskSecs(t) - (t.elapsed || 0));
    }
    offset = Math.max(offset, ...batch.map(t => end[t.id]));
    const done = batch.filter(t => t.status === 'done').length;
    const failed = batch.filter(t => t.status === 'failed').length;
    bands.push({ it, from, to: offset, open: open(it),
      tasks: batch.sort((a, b) => start[a.id] - start[b.id]),
      note: batch.length + ' task' + (batch.length === 1 ? '' : 's') + ' · ' +
        fmtDur(batch.reduce((n, t) => n + (t.took || 0), 0) || offset - from) +
        (done ? ' · ✓ ' + done : '') + (failed ? ' · ✗ ' + failed : '') });
  }
  // The road ahead: a milestone in progress runs from its first task to
  // the end of what is planned plus what is still unplanned; one not yet
  // started begins when everything it waits for has ended.
  const finished = all.filter(t => t.status === 'done' && t.took);
  const perTask = finished.length ? finished.reduce((n, t) => n + t.took, 0) / finished.length
                                  : (data.pace || 3600);
  let roadLeft = 0;
  for (const m of ahead) {               // roadmap order: what it waits for comes first
    const key = 'ms:' + m.id;
    const mine = tasks.filter(t => t.milestone === m.id && end[t.id] !== undefined);
    const waits = m.depends_on.map(d => end['ms:' + d]).filter(v => v !== undefined);
    const unplanned = Math.max(mine.length ? 0 : 1, (m.est_tasks || 3) - m.done - m.open) * perTask;
    if (mine.length) {
      start[key] = Math.min(...mine.map(t => start[t.id]));
      end[key] = Math.max(offset, ...mine.map(t => end[t.id])) + unplanned;
    } else {
      start[key] = Math.max(offset, ...waits);
      end[key] = start[key] + unplanned;
    }
    roadLeft += unplanned;
  }
  const total = Math.max(1, offset, ...ahead.map(m => end['ms:' + m.id]));
  const roadOpen = !ganttOpen.has('road');
  // rows, top to bottom: the furthest future first, the oldest past last
  const rows = [];
  if (ahead.length) {
    rows.push({ band: 'road', open: roadOpen, from: Math.min(...ahead.map(m => start['ms:' + m.id])), to: total,
      label: 'THE ROAD AHEAD', note: ahead.length + ' milestone' + (ahead.length === 1 ? '' : 's') + ' to go'
        + (roadLeft ? ' · about ' + fmtDur(roadLeft) + ' not yet planned' : '') });
    if (roadOpen) [...ahead].reverse().forEach(m => rows.push({ ms: m }));
  }
  for (const b of [...bands].reverse()) {
    rows.push({ band: b.it, open: b.open, from: b.from, to: b.to, note: b.note,
      label: 'ITERATION ' + b.it + (b.it > firstIt && st.status === 'running' && b === bands[bands.length - 1] ? ' · NOW' : '') });
    if (b.open) [...b.tasks].reverse().forEach(t => rows.push({ task: t }));
  }
  const host = h('div', { id: 'gantt' });
  const W = Math.max(760, ($('#proj-body').clientWidth || 1100) - 70);
  const LAB = Math.min(300, Math.round(W * .3)), PADR = 200, ROW = 26, TOP = 28;
  const plotW = W - LAB - PADR;
  const X = s => LAB + (s / total) * plotW;
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
  const arrow = (id, fill) => {
    const marker = el('marker', { id, viewBox: '0 0 8 8', refX: 7, refY: 4,
                                  markerWidth: 7, markerHeight: 7, orient: 'auto' });
    marker.append(el('path', { d: 'M0,0 L8,4 L0,8 z', fill }));
    return marker;
  };
  const stripes = el('pattern', { id: 'work', width: 8, height: 8,
    patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
  stripes.append(el('rect', { width: 4, height: 8, fill: 'rgba(255,255,255,.28)' }));
  defs.append(arrow('arr', '#4a6690'), arrow('arr-ms', '#8f6bc4'), stripes);
  svg.append(defs);
  // time axis: a step that gives about ten marks
  const step = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400,
                28800, 43200, 86400, 172800, 604800].find(s => total / s <= 11) || 1209600;
  for (let s = 0; s <= total + .001; s += step) {
    svg.append(el('line', { x1: X(s), y1: TOP - 8, x2: X(s), y2: H - 6,
                            stroke: '#162233', 'stroke-width': 1 }));
    svg.append(el('text', { x: X(s), y: 14, fill: '#4d5f77', 'font-size': 10.5,
                            'text-anchor': 'middle' }, s ? fmtDur(s) : '0'));
  }
  if (offset > 0 && offset < total)       // where the plan ends and the road begins
    svg.append(el('line', { x1: X(offset), y1: TOP - 8, x2: X(offset), y2: H - 6,
                            stroke: '#8f6bc4', 'stroke-width': 1, 'stroke-dasharray': '3 4', opacity: .7 }));
  const colors = {};
  let ci = 0;
  const yOf = {};
  rows.forEach((r, i) => {
    if (r.task) yOf[r.task.id] = TOP + i * ROW;
    if (r.ms) yOf['ms:' + r.ms.id] = TOP + i * ROW;
  });
  // dependency connectors first, so bars sit on top of them: leave from
  // the end of what is waited for, run to this row, arrive at the bar's
  // start — clear of the labels beside the bars
  const connect = (from, to, stroke, mark) => {
    if (yOf[from] === undefined || yOf[to] === undefined || end[from] === undefined) return;
    const x2 = X(start[to]), y2 = yOf[to] + ROW / 2;
    const x1 = Math.min(X(end[from]) - 5, x2 - 9);
    const y1 = yOf[from] + (yOf[from] < yOf[to] ? ROW - 6 : 6);
    // work can run out of order: a milestone begun before the one it waits
    // for. Then come back from that one's bar, so the line starts at
    // something instead of in the empty part of its row.
    const late = start[from] !== undefined && X(start[from]) > x1;
    const d = late ? `M${X(start[from])},${yOf[from] + ROW / 2} H${x1} V${y2} H${x2 - 1}`
                   : `M${x1},${y1} V${y2} H${x2 - 1}`;
    svg.append(el('path', { d, fill: 'none', stroke, 'stroke-width': 1.3,
                            'marker-end': `url(#${mark})` }));
  };
  for (const r of rows) {
    if (r.task) for (const d of r.task.depends_on) connect(d, r.task.id, '#4a6690', 'arr');
    if (r.ms) for (const d of r.ms.depends_on) connect('ms:' + d, 'ms:' + r.ms.id, '#8f6bc4', 'arr-ms');
  }
  const titleOf = id => { const m = road.find(r => r.id === id); return m ? m.title : ''; };
  rows.forEach((r, i) => {
    const y = TOP + i * ROW;
    if (r.band !== undefined) {
      const g = el('g', { class: 'g-band', style: 'cursor:pointer' });
      g.append(el('rect', { x: 0, y: y + 1, width: W, height: ROW - 2, fill: 'transparent' }));
      g.append(el('text', { x: 2, y: y + 17, fill: r.band === 'road' ? '#a78bda' : '#7d8fa5', 'font-size': 10.5,
        'letter-spacing': 1.5 }, (r.open ? '▾ ' : '▸ ') + r.label));
      // (the label is letter-spaced: about 8 px a character, marker included)
      g.append(el('text', { x: 10 + (r.label.length + 2) * 8.1, y: y + 17, fill: '#4d5f77', 'font-size': 10.5 }, r.note));
      if (!r.open)                         // folded: the whole band as one bar
        g.append(el('rect', { x: X(r.from), y: y + 8, width: Math.max(7, X(r.to) - X(r.from)),
                              height: 10, rx: 4, fill: r.band === 'road' ? '#8f6bc4' : '#7d8fa5', opacity: .4 }));
      g.append(el('line', { x1: 2, y1: y + ROW - 3, x2: W - 4, y2: y + ROW - 3, stroke: '#1a2638' }));
      g.append(el('title', {}, r.open ? 'click to fold' : 'click to unfold'));
      g.addEventListener('click', () => {
        ganttOpen.has(r.band) ? ganttOpen.delete(r.band) : ganttOpen.add(r.band);
        render(true);
      });
      svg.append(g);
      return;
    }
    const max = Math.floor((LAB - 44) / 6.1);
    const clip = s => (s.length > max ? s.slice(0, max - 1) + '…' : s);
    if (r.ms) {                            // a milestone still ahead
      const m = r.ms, key = 'ms:' + m.id;
      const x = X(start[key]), w = Math.max(9, X(end[key]) - x);
      const g = el('g', { class: 'g-row g-ms', style: 'cursor:pointer' });
      g.append(el('rect', { class: 'hit', x: 0, y: y + 1, width: W, height: ROW - 2, fill: 'transparent' }));
      g.append(el('text', { x: 4, y: y + 17, fill: '#8f6bc4', 'font-size': 10.5, 'font-family': 'monospace' }, m.id));
      g.append(el('text', { x: 38, y: y + 17, fill: '#c9b8ec', 'font-size': 12 }, clip(m.title)));
      const bar = { x, y: y + 7, width: w, height: 12, rx: 6 };
      g.append(el('rect', { ...bar, fill: '#8f6bc4', 'fill-opacity': m.status === 'active' ? .3 : .1,
                            stroke: '#8f6bc4', 'stroke-width': 1.2, 'stroke-dasharray': m.status === 'active' ? '' : '5 3' }));
      if (m.status === 'active') g.append(el('rect', { ...bar, fill: 'url(#work)', opacity: .7 }));
      const size = end[key] - start[key];
      g.append(el('text', { x: x + w + 7, y: y + 17, fill: '#7d8fa5', 'font-size': 10.5 },
        (m.status === 'active' ? '▶ ' + m.done + ' of ~' + Math.max(m.est_tasks || 3, m.done + m.open) + ' tasks'
                               : '○ ~' + (m.est_tasks || 3) + ' tasks') + ' · ≈ ' + fmtDur(size)
        + (m.depends_on.length ? ' · after ' + m.depends_on.join(', ') : '')));
      g.append(el('title', {}, `${m.id} — ${m.title}\n${ROAD_SAY[m.status]}` +
        (m.outcome ? '\nreached when: ' + m.outcome : '') +
        (m.depends_on.length ? '\nwaits for: ' + m.depends_on.map(d => d + ' ' + titleOf(d)).join('; ') : '')));
      g.addEventListener('click', () => openMilestone(m, road));
      svg.append(g);
      return;
    }
    const t = r.task;
    if (!(t.phase in colors)) colors[t.phase] = PALETTE[ci++ % PALETTE.length];
    const col = t.status === 'failed' ? '#f87171' : colors[t.phase];
    const x = X(start[t.id]), w = Math.max(7, X(end[t.id]) - x);
    const g = el('g', { class: 'g-row', style: 'cursor:pointer' });
    g.append(el('rect', { class: 'hit', x: 0, y: y + 1, width: W, height: ROW - 2, fill: 'transparent' }));
    g.append(el('text', { x: 4, y: y + 17, fill: '#4d5f77', 'font-size': 10.5,
                          'font-family': 'monospace' }, t.id));
    g.append(el('text', { x: 38, y: y + 17, fill: t.status === 'done' ? '#7d8fa5' : '#d7e2ee',
      'font-size': 12 }, clip(t.title)));
    const bar = { x, y: y + 7, width: w, height: 12, rx: 4 };
    if (t.status === 'done') g.append(el('rect', { ...bar, fill: col }));
    else if (t.status === 'failed') g.append(el('rect', { ...bar, fill: col, opacity: .55 }));
    else if (t.needs_human)          // waiting on the owner: amber, dashed
      g.append(el('rect', { ...bar, fill: '#fbbf24', 'fill-opacity': .14,
                            stroke: '#fbbf24', 'stroke-width': 1.2, 'stroke-dasharray': '4 3' }));
    else {
      g.append(el('rect', { ...bar, fill: col, opacity: t.status === 'todo' ? .22 : .6,
                            stroke: col, 'stroke-width': 1 }));
      if (t.status === 'inprogress' || t.status === 'reviewing')
        g.append(el('rect', { ...bar, fill: 'url(#work)' }));
    }
    const mark = { done: '✓', failed: '✗', inprogress: '● working',
                   reviewing: '◐ reviewing' }[t.status]
      || (t.needs_human ? '⚑ needs you' : t.held ? 'behind a decision' : '');
    g.append(el('text', { x: x + w + 7, y: y + 17, fill: '#7d8fa5', 'font-size': 10.5 },
      [t.agent, timeNote(t), mark, t.milestone].filter(Boolean).join(' · ')));
    g.append(el('title', {}, `${t.id} — ${t.title}\n${t.agent}` +
      (t.model_used || t.model ? ' on ' + modelName(t.model_used || t.model) : '') +
      (t.model_why ? ' — ' + t.model_why : '') +
      `\nstatus: ${t.status}` + (timeNote(t) ? ' · ' + timeNote(t) : '') +
      (t.milestone ? '\ntowards: ' + t.milestone + ' ' + titleOf(t.milestone) : '') +
      (t.first_started || t.started ? '\nstarted ' + (t.first_started || t.started).replace('T', ' ') : '') +
      (t.finished ? '\nfinished ' + t.finished.replace('T', ' ') : '') +
      (t.needs_human ? '\nwaiting for your decision' : '') +
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
    ahead.length ? h('span', {}, h('i', { style: { background: 'rgba(143,107,196,.12)', border: '1px dashed #8f6bc4', borderRadius: '6px' } }), 'milestone ahead') : null,
    tasks.some(t => t.needs_human) ? h('span', {}, h('i', { style: { background: 'rgba(251,191,36,.14)', border: '1px dashed #fbbf24' } }), 'needs you') : null,
    h('span', {}, '→ waits for'));
  const pace = h('div', { class: 'gantt-pace' }, data.pace
    ? [h('b', { text: fmtDur(spent) }), ' of work done',
       left + roadLeft > 0 ? [' · about ', h('b', { text: fmtDur(left + roadLeft) }), ' to the goal at the pace measured so far'] : null,
       h('span', { class: 'hint', text: ' — newest on top; a finished bar is as long as the task took, the rest are predicted from that.' })]
    : h('span', { class: 'hint', text: 'Newest on top. Nothing has finished yet, so these lengths are the planner\'s own guess — measured times replace them as tasks complete.' }));
  host.append(options, legend, pace, svg);
  return host;
}

// ---- live

// "Right now" is what she is writing as she writes it. The arrows beside
// it step back through what she wrote before — every draft, plan and
// verdict, whole — and forward again to the present.

function live() {
  return h('div', { id: 'live' },
    h('div', { class: 'panel' },
      h('h2', { class: 'pager' },
        h('button', { class: 'pg', id: 'live-older', text: '◀', title: 'the output before this one', onclick: () => stepLive(1) }),
        h('span', { id: 'live-title', text: 'Right now' }),
        h('button', { class: 'pg', id: 'live-newer', text: '▶', title: 'the output after this one', onclick: () => stepLive(-1) }),
        h('span', { class: 'grow' }),
        h('span', { class: 'hint', id: 'live-pos' })),
      h('div', { class: 'p-body', id: 'live-now' })),
    h('div', { class: 'panel' }, h('h2', {}, 'Activity'), h('div', { class: 'p-body feed', id: 'live-feed' })));
}

// Step to an older (+1) or newer (-1) output. The one being looked at is
// remembered by its number, so it stays put while new ones arrive.
function stepLive(by) {
  const at = liveSel == null ? -1 : liveOuts.findIndex(o => o.n === liveSel);
  const to = Math.max(-1, Math.min(liveOuts.length - 1, at + by));
  liveSel = to < 0 ? null : liveOuts[to].n;
  updateLive();
}

// One earlier output, in full.
function pastOutput(o) {
  const full = liveOut[cur + '/' + o.n];
  if (!full) {
    api.get('/api/project/output?slug=' + encodeURIComponent(cur) + '&n=' + o.n)
      .then(got => { liveOut[cur + '/' + o.n] = got; if (liveSel === o.n) updateLive(); })
      .catch(e => { liveOut[cur + '/' + o.n] = { text: '', error: e.message }; if (liveSel === o.n) updateLive(); });
  }
  // the same clock as the activity log beside it
  const when = new Date(o.ended * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
  return [
    h('div', { class: 'now', text: o.title ? o.task + ' — ' + o.title : o.label }),
    h('div', { class: 'now-sub' }, o.title ? badge(o.label, 'who') : null,
      o.attempt > 1 ? badge('attempt ' + o.attempt) : null,
      ' finished ' + when + ' · took ' + fmtDur(o.ended - o.started) + ' · ' + o.chars.toLocaleString() + ' characters'),
    !full ? h('div', { class: 'hint', text: 'fetching it…' })
      : full.error ? h('div', { class: 'hint', text: full.error })
      : h('div', { class: 'tokens past', text: full.text })];
}

function updateLive() {
  const now = $('#live-now'), feed = $('#live-feed');
  if (!now || !data) return;
  const st = data.state, lv = data.live || {}, c = st.current;
  const kids = [];
  let at = liveSel == null ? -1 : liveOuts.findIndex(o => o.n === liveSel);
  if (liveSel != null && at < 0) liveSel = null;         // no longer kept
  $('#live-title').textContent = at < 0 ? 'Right now' : at === 0 ? 'The output before' : (at + 1) + ' outputs back';
  $('#live-older').disabled = at >= liveOuts.length - 1;
  $('#live-newer').disabled = at < 0;
  $('#live-pos').textContent = !liveOuts.length ? ''
    : at < 0 ? '◀ ' + liveOuts.length + ' earlier output' + (liveOuts.length === 1 ? '' : 's')
    : (at + 1) + ' of ' + liveOuts.length;
  if (at >= 0) {
    const shown = now.dataset.shown === String(liveSel) && now.querySelector('.tokens.past');
    if (!shown) { clear(now, pastOutput(liveOuts[at])); now.scrollTop = 0; }
    now.dataset.shown = now.querySelector('.tokens.past') ? String(liveSel) : '';
  } else {
  now.dataset.shown = '';
  if (st.status !== 'running') {
    kids.push(h('div', { class: 'now', text: { idle: 'Autopilot is not running.',
      stopped: 'Stopped at your request.', complete: 'Run complete.',
      stalled: 'Paused — she stopped making progress.',
      budget: 'Paused — the budget you set was reached.' }[st.status] || st.status }));
    kids.push(h('div', { class: 'now-sub', text: st.stop_reason || (data.approved
      ? 'Press Start and this panel shows her thinking as it happens.'
      : 'Approve the plan to begin.') }));
  } else if (onYou()) {
    kids.push(h('div', { class: 'now', text: 'Waiting on you' }));
    kids.push(h('div', { class: 'now-sub', text: 'Everything that does not need your decision is done. Still yours to answer:' }));
    for (const g of data.needs_you)
      kids.push(h('div', {}, h('button', { class: 'btn small', text: g.id + ' — ' + g.title,
        onclick: () => openTask(data.plan.tasks.find(t => t.id === g.id)) })));
  } else if (st.waiting) {
    kids.push(h('div', { class: 'now', text: 'Waiting' }));
    kids.push(h('div', { class: 'now-sub', text: st.waiting }));
  } else if (c) {
    kids.push(h('div', { class: 'now', text: c.title }));
    kids.push(h('div', { class: 'now-sub' }, badge(c.agent, 'who'), badge(modelName(c.model), 'model'),
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
  if (st.status !== 'running' && liveOuts.length)
    kids.push(h('div', { class: 'hint', text: '◀ steps back through what she wrote — the most recent output is one click away.' }));
  const pinned = now.scrollHeight - now.scrollTop - now.clientHeight < 60;
  clear(now, kids);
  if (now.querySelector('.tokens') && pinned) now.scrollTop = now.scrollHeight;
  }
  const stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
  const own = cur + ': ';              // every line here is about this project
  clear(feed, (data.activity || []).map(ev =>
    h('div', { class: 'ev ' + ev.kind }, h('time', { text: (ev.t || '').slice(11, 19) }),
      h('span', { text: ev.text.startsWith(own) ? ev.text.slice(own.length) : ev.text }))));
  if (!(data.activity || []).length)
    feed.append(h('div', { class: 'dim', text: 'Nothing yet.' }));
  if (stick) feed.scrollTop = feed.scrollHeight;
}

// ---- files
//
// Where the project lives and what is in it: a tree on the left, the file
// on the right, and one click to the real thing — the folder in your file
// manager, the project in your editor, a terminal in its directory.

function fileTree(rows) {
  const root = { dirs: {}, files: [] };
  for (const f of rows) {
    const parts = f.path.split('/');
    let node = root;
    for (const d of parts.slice(0, -1)) node = node.dirs[d] ||= { dirs: {}, files: [] };
    node.files.push(f);
  }
  const draw = (node, prefix, depth) => {
    const out = [];
    for (const name of Object.keys(node.dirs).sort()) {
      const path = prefix + name;
      const isOpen = depth === 0 ? !openDirs.has(path) : openDirs.has(path);
      out.push(h('button', { class: 'fnode dir', style: { paddingLeft: 8 + depth * 14 + 'px' },
        onclick: () => { openDirs.has(path) ? openDirs.delete(path) : openDirs.add(path); render(true); } },
        (isOpen ? '▾ ' : '▸ ') + name + '/'));
      if (isOpen) out.push(...draw(node.dirs[name], path + '/', depth + 1));
    }
    for (const f of node.files)
      out.push(h('button', { class: 'fnode' + (f.path === fileSel ? ' on' : '') + (f.by ? ' touched' : ''),
        style: { paddingLeft: 8 + depth * 14 + 'px' }, title: f.path + (f.by ? '\nlast changed by ' + f.by.task + ' — ' + f.by.title : ''),
        onclick: () => pickFile(f.path) },
        h('span', { text: f.path.split('/').pop() }),
        f.by ? h('span', { class: 'fby', text: f.by.task }) : null));
    return out;
  };
  return draw(root, '', 0);
}

function pickFile(path) {
  if (path !== fileSel) { fileHist = null; fileDiff = null; fileMode = 'file'; }
  fileSel = path; fileView = null;
  // make sure the file can be seen in the tree: folders below the top
  // level are shut unless opened, top-level ones are open unless shut
  const parts = path.split('/').slice(0, -1);
  parts.forEach((_, i) => {
    const dir = parts.slice(0, i + 1).join('/');
    if (i === 0) openDirs.delete(dir); else openDirs.add(dir);
  });
  const f = (filesInfo || { files: [] }).files.find(x => x.path === path);
  if (f && f.record) showRecords = true;
  tab = 'files';
  goFn({ view: 'projects', slug: cur, tab, file: path }, { replace: true, quiet: true });
  render(true);
}

async function loadFileView() {
  const path = fileSel;
  try {
    const r = await api.get('/api/file?slug=' + encodeURIComponent(cur) + '&path=' + encodeURIComponent(path));
    if (path !== fileSel) return;
    fileView = r;
  } catch {
    if (path !== fileSel) return;
    fileView = { path, missing: true };
  }
  render(true);
}

function fileBody(v) {
  if (v.missing) return h('div', { class: 'dim', text: 'This file is not in the project any more (it may have been rolled back or renamed).' });
  if (v.image) return h('div', { class: 'fimg' }, h('img', { alt: v.path,
    src: '/api/file/raw?slug=' + encodeURIComponent(cur) + '&path=' + encodeURIComponent(v.path) }));
  if (v.binary) return h('div', { class: 'dim', text: 'Not a text file (' + fmtSize(v.size) + '). Open the folder to get at it.' });
  if (/\.md$/i.test(v.path) && !store.get('files-raw', false))
    return h('div', { class: 'doc' }, md(v.content, links(v.path)));
  const lines = v.content.replace(/\n$/, '').split('\n');
  return h('div', { class: 'code' },
    h('pre', { class: 'gutter', text: lines.map((_, i) => i + 1).join('\n') }),
    h('pre', { class: 'src', text: lines.join('\n') }));
}

// A file's history: every verified change to it, and each change as
// GitHub would show it — the previous version beside the modified one.

async function loadFileHistory() {
  const path = fileSel;
  fileHist = { path, loading: true, changes: [] };
  try {
    const r = await api.get('/api/file/history?slug=' + encodeURIComponent(cur) + '&path=' + encodeURIComponent(path));
    if (path !== fileSel) return;
    fileHist = r;
  } catch {
    if (path !== fileSel) return;
    fileHist = { path, changes: [] };
  }
  render(true);
}

async function loadDiff(task) {
  const path = fileSel;
  fileDiff = { task, loading: true };
  render(true);
  let got;
  try {
    got = { task, data: await api.get('/api/file/diff?slug=' + encodeURIComponent(cur)
      + '&path=' + encodeURIComponent(path) + '&task=' + encodeURIComponent(task)) };
  } catch (e) { got = { task, error: e.message }; }
  if (path !== fileSel || !fileDiff || fileDiff.task !== task) return;
  fileDiff = got;
  render(true);
}

// Open one change to one file, from anywhere (the Changes tab, a task).
function openDiff(path, task) {
  pickFile(path);
  fileMode = 'history';
  loadDiff(task);
}

function diffTable(d) {
  const cell = (cls, text) => h('td', { class: cls, text: text == null ? '' : text });
  const body = h('tbody');
  for (const r of d.rows) {
    if (r.k === 'skip') {
      body.append(h('tr', { class: 'skip' }, h('td', { colSpan: diffSplit ? 4 : 3,
        text: '⋯ ' + r.n + ' unchanged line' + (r.n === 1 ? '' : 's') })));
    } else if (diffSplit) {
      body.append(h('tr', { class: r.k },
        cell('n', r.ln), cell('l' + (r.l == null ? ' none' : ''), r.l),
        cell('n', r.rn), cell('r' + (r.r == null ? ' none' : ''), r.r)));
    } else {
      if (r.k === 'same')
        body.append(h('tr', { class: 'same' }, cell('n', r.ln), cell('n', r.rn), cell('u', '  ' + r.l)));
      if (r.l != null && r.k !== 'same')
        body.append(h('tr', { class: 'del' }, cell('n', r.ln), cell('n', ''), cell('u l', '− ' + r.l)));
      if (r.r != null && r.k !== 'same')
        body.append(h('tr', { class: 'add' }, cell('n', ''), cell('n', r.rn), cell('u r', '+ ' + r.r)));
    }
  }
  const col = w => h('col', w ? { style: { width: w } } : {});
  return h('table', { class: 'diff' + (diffSplit ? ' split' : ' unified') },
    // (a fixed-layout table takes its column widths from here, not from
    // the two wide header cells)
    h('colgroup', {}, diffSplit ? [col('46px'), col(), col('46px'), col()]
                                : [col('46px'), col('46px'), col()]),
    diffSplit ? h('thead', {}, h('tr', {},
      h('th', { colSpan: 2, text: d.before ? 'Previous version' : 'Previous version — the file did not exist' }),
      h('th', { colSpan: 2, text: d.latest ? 'Modified version — as it is now' : 'Modified version' }))) : null,
    body);
}

function historyView() {
  const changes = (fileHist && fileHist.path === fileSel && fileHist.changes) || [];
  if (fileHist && fileHist.loading) return h('div', { class: 'dim', text: 'Reading its history…' });
  if (!changes.length)
    return h('div', { class: 'dim', text: 'No recorded changes to this file — it was here before she started, or was written by hand.' });
  const sel = fileDiff ? fileDiff.task : changes[0].task;
  if (!fileDiff) loadDiff(sel);
  const at = changes.findIndex(c => c.task === sel);
  const list = h('div', { class: 'hlist' }, changes.map(c =>
    h('button', { class: 'hrow' + (c.task === sel ? ' on' : ''), onclick: () => loadDiff(c.task),
      title: (c.why ? 'why: ' + c.why + '\n' : '') + c.agent + ' on ' + modelName(c.model) },
      h('span', { class: 'tid', text: c.task }),
      h('span', { class: 'ht', text: c.title }),
      h('span', { class: 'plus', text: '+' + c.added }), h('span', { class: 'minus', text: '−' + c.removed }),
      h('span', { class: 'hw', text: c.action === 'created' ? 'created' : ago(c.t) }))));
  const d = fileDiff && fileDiff.task === sel ? fileDiff : null;
  return h('div', { class: 'hist' }, list,
    h('div', { class: 'dbar' },
      h('button', { class: 'btn small', text: '◀ older', disabled: at >= changes.length - 1,
        onclick: () => loadDiff(changes[at + 1].task) }),
      h('button', { class: 'btn small', text: 'newer ▶', disabled: at <= 0,
        onclick: () => loadDiff(changes[at - 1].task) }),
      d && d.data ? h('span', { class: 'hint' }, h('span', { class: 'plus', text: '+' + d.data.added }), ' ',
        h('span', { class: 'minus', text: '−' + d.data.removed }), ' in ',
        h('button', { class: 'linkish', text: sel + ' — ' + d.data.title, onclick: () => openTask(findTask(sel)) })) : null,
      h('span', { class: 'grow' }),
      h('button', { class: 'btn small', text: diffSplit ? 'Unified' : 'Side by side',
        onclick: () => { diffSplit = !diffSplit; store.set('aria_diff_split', diffSplit); render(true); } })),
    !d || d.loading ? h('div', { class: 'dim', text: 'Comparing…' })
      : d.error ? h('div', { class: 'dim', text: d.error })
      : d.data.rows.length ? h('div', { class: 'dwrap' }, diffTable(d.data))
      : h('div', { class: 'dim', text: 'This change left the file exactly as it was.' }));
}

function files() {
  if (!filesInfo) { loadFiles(true); return h('div', { class: 'empty', text: 'Reading the project folder…' }); }
  const tools = filesInfo.tools || {};
  const rows = filesInfo.files.filter(f => showRecords || !f.record);
  const hidden = filesInfo.files.length - rows.length;
  if (fileSel && (!fileView || fileView.path !== fileSel)) loadFileView();
  if (fileSel && (!fileHist || fileHist.path !== fileSel)) loadFileHistory();
  const sel = filesInfo.files.find(f => f.path === fileSel);
  const v = fileView && fileView.path === fileSel ? fileView : null;
  const isMd = sel && /\.md$/i.test(sel.path);
  const nChanges = fileHist && fileHist.path === fileSel ? fileHist.changes.length : 0;
  const mode = fileMode === 'history' ? 'history' : 'file';
  const seg = (name, label) => h('button', { class: mode === name ? 'on' : '', text: label,
    onclick: () => { fileMode = name; if (name === 'file') fileDiff = null; render(true); } });
  return h('div', { id: 'files' },
    h('div', { class: 'fbar' },
      h('span', { class: 'fwhere', title: 'where this project lives on your machine' }, '📁 ', h('code', { text: filesInfo.path })),
      h('button', { class: 'btn small', text: 'Copy path', onclick: () => copyText(filesInfo.path, 'Path') }),
      h('button', { class: 'btn small', text: 'Open folder', disabled: !tools.folder,
        title: tools.folder ? 'in your file manager' : 'xdg-open is not installed', onclick: () => openOnDesktop('folder') }),
      h('button', { class: 'btn small', text: 'Open in ' + (tools.editor || 'editor'), disabled: !tools.editor,
        title: tools.editor ? 'the whole project' : 'no editor found — set one in Settings', onclick: () => openOnDesktop('editor') }),
      h('button', { class: 'btn small', text: 'Terminal here', disabled: !tools.terminal,
        title: tools.terminal ? 'a terminal in this folder' : 'no terminal found — set one in Settings', onclick: () => openOnDesktop('terminal') }),
      h('span', { class: 'grow' }),
      h('label', { class: 'toggle hint' }, h('input', { type: 'checkbox', checked: showRecords,
        onchange: e => { showRecords = e.target.checked; render(true); } }),
        ' her records' + (hidden ? ' (' + hidden + ')' : ''))),
    h('div', { class: 'fsplit' },
      h('div', { class: 'ftree' }, rows.length ? fileTree(rows)
        : h('div', { class: 'dim', text: 'No files yet — they appear here as she writes them.' })),
      h('div', { class: 'fview' }, !sel && !fileSel
        ? h('div', { class: 'empty' }, h('div', { class: 'big', text: rows.length + ' file' + (rows.length === 1 ? '' : 's') }),
            'Pick a file on the left. Files she has changed carry the task that last touched them — and keep their earlier versions.')
        : [h('div', { class: 'fhead' },
            h('code', { class: 'fpath', text: fileSel }),
            sel ? h('span', { class: 'hint', text: (sel.lines ? sel.lines + ' lines · ' : '') + fmtSize(sel.size) }) : null,
            sel && sel.by ? h('button', { class: 'badge', title: 'the task that last changed this file — click to open it',
              text: sel.by.task + ' — ' + (sel.by.title.length > 40 ? sel.by.title.slice(0, 39) + '…' : sel.by.title),
              onclick: () => openTask(findTask(sel.by.task)) }) : null,
            h('span', { class: 'grow' }),
            h('span', { class: 'seg', title: 'the file as it is, or what each change did to it' },
              seg('file', 'File'), seg('history', 'Changes' + (nChanges ? ' (' + nChanges + ')' : ''))),
            isMd && mode === 'file' ? h('button', { class: 'btn small', text: store.get('files-raw', false) ? 'Rendered' : 'Source',
              onclick: () => { store.set('files-raw', !store.get('files-raw', false)); render(true); } }) : null,
            h('button', { class: 'btn small', text: 'Copy path',
              onclick: () => copyText(filesInfo.path + '/' + fileSel, 'Path') }),
            h('button', { class: 'btn small', text: 'Open in ' + (tools.editor || 'editor'), disabled: !tools.editor || !sel,
              onclick: () => openOnDesktop('editor', fileSel) })),
           h('div', { class: 'fbody' }, mode === 'history' ? historyView()
             : v ? fileBody(v) : h('div', { class: 'dim', text: 'Loading…' }))])));
}

const findTask = id => (data.plan.tasks || []).find(t => t.id === id)
  || (data.history || []).find(t => t.id === id);

// ---- brief

// her documents open with their own "# Title" and a dateline; inside a
// titled panel that is noise
const body = (text, base) => md(text.replace(/^# .*\n+(\*[^\n]*\*\n+)?/, ''), links(base));

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
        data.assessment ? body(data.assessment, autoDoc('ASSESSMENT.md'))
          : h('div', { class: 'dim', text: 'Written when autopilot first starts: what the project needs, which agents, skills, models and tools cover it, and what they cannot do.' })),
      panel('Memory — what she has learned so far',
        data.memory ? body(data.memory, autoDoc('MEMORY.md'))
          : h('div', { class: 'dim', text: 'Rewritten after every batch: the state of the project, decisions made, lessons, and what is still to do. This — not a transcript — is what the next plan starts from.' })),
      panel('Code review — what is worth refining',
        data.review ? body(data.review, autoDoc('REVIEW.md'))
          : h('div', { class: 'dim', text: 'When the goal is met she reads every source file, writes down what is worth changing here, and makes the refinements that leave the code\'s behaviour exactly as it is — each judged on its diff, and on the project\'s own tests where they can be run. Ask for it any time.' }),
        data.approved ? h('button', { class: 'btn small', text: 'Review & refine now', onclick: refineNow }) : null),
      panel('Your guidance',
        notes.length ? h('ul', {}, notes.map(n => h('li', { text: n.slice(2) })))
          : h('div', { class: 'dim', text: 'None yet. Use Steer to redirect her without stopping.' }),
        data.approved ? h('button', { class: 'btn small', text: 'Steer', onclick: steer }) : null)));
}

async function refineNow() {
  try {
    await api.post('/api/autopilot', { slug: cur, action: 'refine' });
    toast('She will read the whole codebase and refine it — watch the Live tab.', 'good', 6000);
  } catch (e) { toast(e.message, 'bad', 8000); }
  bus.emit('projects:changed', cur);
  refresh(true);
}

// ---- run
//
// Try the software yourself. The command is shown; it starts when you
// press Run, in the project's folder on this machine, and its output
// streams here. A.R.I.A. never presses this button: nothing in chat, by
// voice or in autopilot can start a program.

async function loadRunner() {
  try {
    const got = await api.get('/api/runner?slug=' + encodeURIComponent(cur));
    if (got.slug !== cur) return;
    runInfo = got; runStamp++;
    if (got.status.running) pollRun();
  } catch (e) { runInfo = { slug: cur, error: e.message, run: [], check: [], setup: [], status: {}, sandbox: {} }; runStamp++; }
  if (visible && tab === 'run') render(true);
}

async function pollRun() {
  clearTimeout(runTimer);
  if (!cur) return;
  let o;
  try { o = await api.get('/api/runner/output?slug=' + encodeURIComponent(cur) + '&since=' + runNext); }
  catch { return; }
  if (o.dropped) runText = '… (earlier output dropped)\n';
  runText += o.text; runNext = o.next;
  const was = runInfo && runInfo.status.running;
  if (runInfo) runInfo.status = o;
  const out = $('#run-out');
  if (out && visible && tab === 'run') {
    const stick = out.scrollHeight - out.scrollTop - out.clientHeight < 60;
    out.textContent = runText || '(no output yet)';
    if (stick) out.scrollTop = out.scrollHeight;
    drawRunStatus();
  }
  if (o.running) runTimer = setTimeout(pollRun, 700);
  else if (was) { runStamp++; if (visible && tab === 'run') render(true); loadFiles(true); }
}

function drawRunStatus() {
  const el = $('#run-status');
  if (!el || !runInfo) return;
  const s = runInfo.status;
  clear(el,
    s.running ? [h('span', { class: 'pill running', text: 'running' }), h('code', { text: s.cmd }),
                 h('span', { class: 'hint', text: s.sandboxed ? 'sandboxed' : 'not sandboxed' })]
      : s.cmd ? [h('span', { class: 'pill ' + (s.exit === 0 ? 'complete' : 'stalled'),
                             text: s.exit === 0 ? 'finished' : 'exited with code ' + s.exit }), h('code', { text: s.cmd })]
      : h('span', { class: 'hint', text: 'Nothing has been run yet.' }),
    (s.urls || []).map(u => h('a', { class: 'btn small primary', href: u, target: '_blank', rel: 'noopener noreferrer', text: 'Open ' + u })),
    h('span', { class: 'grow' }),
    s.running ? h('button', { class: 'btn small danger', text: '■ Stop', onclick: () => runAct('stop') }) : null);
}

async function runAct(action, extra = {}) {
  try {
    const r = await api.post('/api/runner', { slug: cur, action, ...extra });
    if (action === 'start') { runText = ''; runNext = 0; }
    if (runInfo && action !== 'save') runInfo.status = r;
    if (action === 'save') runInfo = r;
    runStamp++;
    render(true);
    if (action === 'start' || action === 'stop') pollRun();
    return r;
  } catch (e) { toast(e.message, 'bad', 9000); }
}

async function runCommand(cmd, boxed) {
  cmd = cmd.trim();
  if (!cmd) return;
  if (runInfo.status.running) return toast('Something is already running — stop it first.', 'bad');
  const key = 'run-ok:' + cur;
  const seen = store.get(key, []);
  if (!seen.includes(cmd)) {
    const ok = await confirmBox('Run this on your machine?', [
      h('pre', { class: 'raw', text: cmd }),
      h('p', {}, 'In ', h('code', { text: runInfo.path }), '.'),
      boxed ? 'Sandboxed: it can change files only inside that folder. It can still read your files, use the network and open a window.'
            : 'Not sandboxed: it runs with your own permissions, like a command typed in a terminal.',
      'This is code a model wrote. Read the command before you run it.',
    ], '▶ Run');
    if (!ok) return;
    store.set(key, [...seen, cmd].slice(-30));
  }
  runAct('start', { cmd, sandboxed: boxed });
}

async function runChecks() {
  checking = true; checkRes = null; render(true);
  try { checkRes = await api.post('/api/runner', { slug: cur, action: 'check' }); }
  catch (e) { checkRes = { ran: false, why: e.message }; }
  checking = false; render(true);
}

function runRow(c, boxed, kind) {
  const input = h('input', { type: 'text', value: c.cmd, spellcheck: 'false' });
  input.addEventListener('keydown', e => { if (e.key === 'Enter') runCommand(input.value, boxed()); });
  return h('div', { class: 'runrow' },
    h('span', { class: 'rname', text: c.name + (c.mine ? ' (yours)' : '') }),
    input,
    h('button', { class: 'btn small', text: 'Save', title: 'remember this command for the project',
      onclick: () => runAct('save', { kind, name: c.name, cmd: input.value }) }),
    h('button', { class: 'btn small primary', text: '▶ Run', onclick: () => runCommand(input.value, boxed()) }));
}

function runTab() {
  if (!runInfo || runInfo.slug !== cur) { loadRunner(); return h('div', { class: 'empty', text: 'Looking at how this project is run…' }); }
  if (runInfo.error) return h('div', { class: 'empty', text: runInfo.error });
  const sb = runInfo.sandbox || {};
  const box = h('input', { type: 'checkbox', checked: sb.available && store.get('run-sandbox', true), disabled: !sb.available,
    onchange: e => store.set('run-sandbox', e.target.checked) });
  const boxed = () => box.checked;
  const custom = h('input', { type: 'text', placeholder: 'any command — it runs in the project folder', spellcheck: 'false' });
  custom.addEventListener('keydown', e => { if (e.key === 'Enter') runCommand(custom.value, boxed()); });
  const line = h('input', { type: 'text', placeholder: 'type a line for the running program, Enter to send', spellcheck: 'false' });
  line.addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    runAct('input', { text: line.value }); line.value = '';
  });
  const none = !runInfo.run.length && !runInfo.setup.length;
  const cr = checkRes;
  return h('div', { id: 'run' },
    h('div', { class: 'fbar' },
      h('span', { class: 'fwhere' }, '📁 ', h('code', { text: runInfo.path })),
      h('span', { class: 'grow' }),
      h('label', { class: 'toggle hint', title: sb.available ? 'bubblewrap: the program can change files only inside the project folder'
                                                             : (sb.why || 'no sandbox available') },
        box, sb.available ? ' sandbox — it can only change files in this folder' : ' no sandbox here'),
      h('button', { class: 'btn small', text: 'Terminal here', disabled: !(runInfo.tools || {}).terminal,
        onclick: () => openOnDesktop('terminal') })),
    !sb.available ? h('div', { class: 'notice warn', text: 'Programs run with your own permissions here: ' + (sb.why || 'no sandbox') + '.' }) : null,
    h('div', { class: 'panel' }, h('h2', {}, 'Run it'),
      h('div', { class: 'p-body' },
        none ? h('div', { class: 'dim', text: 'Nothing here says how to start this project yet (no main.py, package.json, Makefile…). Type a command below — Save keeps it for next time.' }) : null,
        runInfo.setup.map(c => runRow(c, boxed, 'setup')),
        runInfo.run.map(c => runRow(c, boxed, 'run')),
        h('div', { class: 'runrow' }, h('span', { class: 'rname', text: 'Your own' }), custom,
          h('button', { class: 'btn small', text: 'Save', onclick: () => custom.value.trim() && runAct('save', { kind: 'run', name: 'Mine', cmd: custom.value }) }),
          h('button', { class: 'btn small primary', text: '▶ Run', onclick: () => runCommand(custom.value, boxed()) })),
        h('div', { class: 'hint', style: { marginTop: '8px' }, text: 'It starts when you press Run — never on its own. A.R.I.A. cannot press this for you, in chat, by voice or in autopilot.' }))),
    h('div', { class: 'panel' }, h('h2', {}, 'Output', h('span', { class: 'grow' })),
      h('div', { class: 'p-body' },
        h('div', { class: 'row', id: 'run-status' }),
        h('pre', { class: 'raw console', id: 'run-out', text: runText || '(no output yet)' }),
        runInfo.status.running ? line : null)),
    h('div', { class: 'panel' }, h('h2', {}, 'Tests — the way she runs them',
        h('span', { class: 'grow' }),
        h('button', { class: 'btn small', text: checking ? 'Running…' : 'Run the tests', disabled: checking, onclick: runChecks })),
      h('div', { class: 'p-body' },
        h('div', { class: 'hint', text: runInfo.checks === 'off' ? 'Running tests is switched off in Settings.'
          : runInfo.check.length ? 'After every code change she runs ' + runInfo.check[0].cmd + (runInfo.checks === 'direct' ? ' (unsandboxed — your setting)' : sb.available ? ' in a sandbox: no network, a scratch copy of the project, nothing else writable' : ' — but only inside a sandbox, and there is none here, so it is not run') + '. A change that breaks passing tests is rejected.'
          : 'This project has no tests yet. Once it does, she runs them after every code change.' }),
        cr ? h('div', { class: 'notice ' + (cr.ran ? (cr.ok ? 'good' : 'bad') : 'warn'), style: { marginTop: '10px' } },
          cr.ran ? (cr.ok ? '✓ passed — ' : '✗ failing — ') + (cr.summary || '') + ' (' + cr.cmd + ', ' + cr.seconds + 's)'
                 : 'Not run — ' + (cr.why || 'no reason given')) : null,
        cr && cr.output ? h('pre', { class: 'raw console', text: cr.output }) : null)));
}

function afterRunDraw() {
  drawRunStatus();
  const out = $('#run-out');
  if (out) out.scrollTop = out.scrollHeight;
}

// ---- changes

function fileLink(path) {
  return h('a', { class: 'filelink', text: path, href: '#', title: 'open ' + path,
                  onclick: e => { e.preventDefault(); openFile(path); } });
}

function changeCard(r) {
  const files = (r.files || []).map(f => h('div', {}, fileLink(f.path), ' ',
    h('span', { class: 'delta' }, f.action + ' ',
      h('span', { class: 'add', text: '+' + f.added }), ' ',
      h('span', { class: 'del', text: '−' + f.removed })),
    // what this change did to the file: the previous version beside the new
    r.status === 'done' && (f.kept || r.commit) ? [' ', h('button', { class: 'linkish',
      text: 'see the change', title: 'previous version | modified version',
      onclick: () => openDiff(f.path, r.task) })] : null));
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
      r.refine ? badge('refine', 'skill') : null,
      r.status === 'failed' ? badge('failed', 'bad') : null,
      r.attempts > 1 ? badge('attempt ' + r.attempts) : null,
      h('span', { class: 'c-when', text: (r.t || '').replace('T', ' ').slice(0, 16) })),
    h('dl', {},
      row('Changed', files.length ? files : (r.status === 'failed' ? 'nothing — the attempts were rolled back' : 'no files — written output only')),
      row('Why', r.why),
      row('What', r.notes),
      row(r.status === 'failed' ? 'Rejected' : 'Verified', (r.feedback || 'passed') + (r.verified === false && r.status !== 'failed' ? ' (unreviewed)' : '')),
      row('Tests', r.checks ? (r.checks.ran
        ? (r.checks.ok ? '✓ ' : '✗ still failing, as before this task — ') + (r.checks.summary || 'ok') + ' · ' + r.checks.cmd + (r.checks.sandboxed ? ' (sandboxed)' : '')
        : 'not run — ' + (r.checks.why || '')) : ''),
      row('You decided', r.decision ? decisionText(r.decision) : ''),
      row('References', refs)));
}

const decisionText = d => d.answer + (d.via ? ' via ' + d.via : '') +
  (d.note ? ' — “' + d.note + '”' : '');

async function loadAllChanges() {
  try { allChanges = (await api.get('/api/changes?slug=' + encodeURIComponent(cur))).changes; }
  catch (e) { toast(e.message, 'bad'); }
  render(true);
}

function changes() {
  const rows = allChanges || data.changes || [];
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
  if (!allChanges && data.changes_total > rows.length)
    out.push(h('div', { class: 'row', style: { margin: '14px 0' } },
      h('span', { class: 'hint', text: 'Showing the latest ' + rows.length + ' of ' + data.changes_total + ' recorded changes.' }),
      h('button', { class: 'btn small', text: 'Show all ' + data.changes_total, onclick: loadAllChanges })));
  return h('div', {}, out);
}

// ---- report

async function pickReport(name) {
  reportSel = name;
  if (name && reportCache[name] === undefined) {
    const path = autoDoc('reports/' + name);
    try {
      reportCache[name] = (await api.get('/api/file?slug=' + encodeURIComponent(cur) +
                                          '&path=' + encodeURIComponent(path))).content;
    } catch { reportCache[name] = ''; toast('That report is no longer on disk.', 'bad'); }
  }
  render(true);
}

function report() {
  const all = data.reports || [];
  if (!data.report && !all.length)
    return h('div', { class: 'empty' }, h('div', { class: 'big', text: 'No report yet' }),
      'When a run ends — because you stopped it, the goal was met, or she paused herself — the final report appears here: every change she made, with references and reasoning. Every run keeps its own.');
  // newest first; the newest is the same document as FINAL-REPORT.md
  const shown = reportSel && reportSel !== (all[0] || {}).name ? reportSel : '';
  const text = shown ? reportCache[shown] : data.report;
  const base = shown ? autoDoc('reports/' + shown) : autoDoc('FINAL-REPORT.md');
  const picker = all.length > 1 ? h('div', { class: 'reports' },
    h('span', { class: 'hint', text: all.length + ' reports — one per run:' }),
    all.map((r, i) => h('button', { class: 'btn small' + ((shown ? r.name === shown : i === 0) ? ' on' : ''),
      title: r.title, text: 'Run ' + r.run + (i === 0 ? ' (latest)' : ''),
      onclick: () => pickReport(i === 0 ? '' : r.name) })),
    h('span', { class: 'grow' }),
    h('button', { class: 'btn small', text: 'Show file', title: base, onclick: () => pickFile(base) })) : null;
  return h('div', {}, picker,
    text === undefined ? h('div', { class: 'dim', text: 'Loading…' })
      : h('div', { class: 'doc' }, md(text, links(base))));
}

// ------------------------------------------------------------ dialogs

// A quick look at a file without leaving the page you are reading; the
// buttons take you to the real thing.
async function openFile(path) {
  let r;
  try {
    r = await api.get('/api/file?slug=' + encodeURIComponent(cur) + '&path=' + encodeURIComponent(path));
  } catch {
    toast(path + ' is not in the project (it may have been rolled back)', 'bad');
    return;
  }
  const tools = (filesInfo || {}).tools || {};
  const where = h('div', { class: 'fwhere small' }, '📁 ', h('code', { text: r.abs || path }));
  const view = r.image ? h('div', { class: 'fimg' }, h('img', { alt: path,
      src: '/api/file/raw?slug=' + encodeURIComponent(cur) + '&path=' + encodeURIComponent(path) }))
    : r.binary ? h('div', { class: 'dim', text: 'Not a text file (' + fmtSize(r.size) + ').' })
    : /\.md$/i.test(path) ? md(r.content, links(path)) : h('pre', { class: 'raw', text: r.content });
  const close = modal(path, h('div', {}, where, view), { wide: true, actions: [
    { label: 'Copy path', run: () => copyText(r.abs || path, 'Path') },
    { label: 'Show in Files', run: c => { c(); pickFile(path); } },
    { label: 'Open folder', run: () => openOnDesktop('folder', path) },
    ...(tools.editor ? [{ label: 'Open in ' + tools.editor, kind: 'primary',
                          run: () => openOnDesktop('editor', path) }] : []),
  ] });
  return close;
}

async function openDoc(name) {
  for (const path of ['docs/' + name + '.md', name + '.md']) {
    try {
      const r = await api.get('/api/file?slug=' + encodeURIComponent(cur) + '&path=' + encodeURIComponent(path));
      modal(title(cur) + ' — ' + name.replace('-', ' '), md(r.content, links(path)), { wide: true });
      return;
    } catch { /* try the next location */ }
  }
  toast('No ' + name.replace('-', ' ') + ' document for this project yet.', 'bad');
}

async function decideTask(t, approve, note, close) {
  try {
    await api.post(approve ? '/api/approve-task' : '/api/skip-task',
                   { slug: cur, id: t.id, note });
    toast(approve ? t.id + ' approved — she will run it' + (note ? ' with your decision.' : '.')
                  : t.id + ' skipped — she will plan around it.', 'good');
    close();
  } catch (e) { toast(e.message, 'bad'); }
  bus.emit('projects:changed', cur);
  refresh(true);
}

async function openTask(t) {
  if (!t) return;
  const model = t.model_used || t.model || 'agent default';
  const gated = t.needs_human && t.status === 'todo';
  const note = h('textarea', { rows: 2, placeholder:
    'Your decision, if the task needs one — e.g. “use the free tier”, “call it Larder”. She will follow it exactly.' });
  const body = h('div', {},
    gated ? h('div', { class: 'gatebox' },
      h('b', { text: 'This one is yours to decide.' }),
      ' She will not run it — or anything that builds on it — until you answer. '
      + 'Approve it to let her go ahead, or skip it and she plans around it.',
      note) : null,
    h('dl', { class: 'kv' },
      h('dt', { text: 'status' }), h('dd', { text: t.status + (t.attempts > 1 ? ' · attempt ' + t.attempts : '') }),
      h('dt', { text: 'agent' }), h('dd', { text: t.agent }),
      h('dt', { text: 'model' }), h('dd', { text: (model === 'agent default' ? model : modelName(model))
        + (t.model_why ? ' — ' + t.model_why : '') }),
      t.milestone ? [h('dt', { text: 'towards' }), h('dd', { text: t.milestone + ' — '
        + ((((data.roadmap || {}).milestones || []).find(m => m.id === t.milestone) || {}).title || '') })] : null,
      h('dt', { text: 'skills' }), h('dd', { text: (t.skills || []).join(', ') || '—' }),
      h('dt', { text: 'depends on' }), h('dd', { text: t.depends_on.join(', ') || '—' }),
      h('dt', { text: 'done when' }), h('dd', { text: t.done_when }),
      t.why ? [h('dt', { text: 'why' }), h('dd', { text: t.why })] : null,
      t.decision ? [h('dt', { text: 'you decided' }), h('dd', { text: decisionText(t.decision) })] : null,
      t.held && t.status === 'todo' ? [h('dt', { text: 'on hold' }), h('dd', { text: 'it builds on a task that is waiting for your decision' })] : null,
      t.feedback ? [h('dt', { text: 'review' }), h('dd', { text: t.feedback })] : null,
      (t.files || []).length ? [h('dt', { text: 'files' }), h('dd', {}, t.files.map(f => h('div', {}, fileLink(f))))] : null),
    h('div', { class: 'section-h', text: 'What to produce' }),
    h('p', { text: t.detail || '—' }),
    h('div', { id: 'task-out' }));
  modal(t.id + ' — ' + t.title, body, { wide: true, actions: gated ? [
    { label: 'Skip it', run: close => decideTask(t, false, note.value.trim(), close) },
    { label: '✓ Approve', kind: 'primary', run: close => decideTask(t, true, note.value.trim(), close) },
  ] : [] });
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
    b.onclick = () => { tab = b.dataset.tab; go({ view: 'projects', slug: cur, tab, file: fileSel }, { replace: true }); };
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

export async function show({ slug, tab: wanted, file }) {
  visible = true;
  if (TABS.includes(wanted)) tab = wanted;
  const changed = slug && slug !== cur;
  if (changed) { cur = slug; forget(); }
  if (file) { fileSel = file; fileView = null; }
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

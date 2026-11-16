// The Pipeline view: capture, the five stages of an idea's journey, and
// the "mind" — the live thought stream of triage and develop runs. Runs
// started from chat or by voice show up in the same stream.

import { $, h, clear, api, eventSource, bus, md, modal, toast, badge, title } from './core.js';

let visible = false, running = null, tokensEl = null, autoPipeline = true;
let brainReady = false, projects = [];
const log = () => $('#log');

// ------------------------------------------------------------- cards

function card(titleText, excerpt, badges = [], onclick = null) {
  const d = h('div', { class: 'card' },
    h('div', { class: 't', text: titleText }),
    excerpt ? h('div', { class: 'x', text: excerpt }) : null,
    badges);
  if (onclick) d.onclick = onclick; else d.style.cursor = 'default';
  return d;
}

async function refresh() {
  let b;
  try { b = await api.get('/api/board'); } catch { return; }
  const fill = (id, items, count) => {
    clear($('#c-' + id), items);
    $('#n-' + id).textContent = (count ?? items.length) || '';
  };
  fill('inbox', b.inbox.map(n => {
    const c = card(n.file, n.excerpt, n.type === 'unclear' ? [badge('needs your answer', 'unclear')] : [],
                   () => openNote(n.path));
    if (n.type === 'unclear') c.classList.add('needs');
    return c;
  }));
  fill('processing', b.processing.map(n => card(n.file, n.excerpt, [], () => openNote(n.path))));

  fill('potential', b.potential.map(p => {
    const c = card(title(p.slug), p.pitch,
      [badge(p.approved ? 'approved' : 'awaiting approval', p.approved ? 'approved' : 'proposed'),
       p.rounds > 1 ? badge('round ' + p.rounds) : null,
       p.exported ? badge('in obsidian', 'who') : null],
      () => p.path && openNote(p.path));
    if (!p.approved) c.classList.add('needs');
    c.append(h('div', { class: 'actions' },
      h('a', { class: 'btn small primary', href: '/projects?p=' + p.slug,
               dataset: { view: 'projects', slug: p.slug, tab: 'kanban' },
               text: p.approved ? 'Open' : 'Review & approve',
               onclick: e => e.stopPropagation() }),
      h('button', { class: 'btn small', text: 'Refine',
                    onclick: e => { e.stopPropagation(); refine(p.slug); } }),
      h('button', { class: 'btn small', text: '→ Obsidian',
                    title: brainReady ? 'mirror this project into your vault' : 'set your vault path in Settings first',
                    onclick: e => { e.stopPropagation(); exportBrain(p.slug); } })));
    return c;
  }));

  fill('active', b.active.map(p => {
    const info = projects.find(x => x.slug === p.slug) || {};
    const live = info.autopilot === 'running';
    const c = card(title(p.slug),
      live ? (info.waiting ? 'waiting — ' + info.waiting : (info.current || info.phase || 'working'))
           : p.phase,
      [live ? badge('autopilot · iteration ' + info.iteration, 'ok')
            : (info.autopilot && info.autopilot !== 'idle' ? badge(info.autopilot) : null),
       info.total ? badge(info.done + '/' + info.total + ' tasks') : null],
      () => bus.emit('nav', { view: 'projects', slug: p.slug, tab: live ? 'live' : 'kanban' }));
    if (live) c.classList.add('running');
    return c;
  }));

  const kids = [];
  let kCount = 0;
  for (const [sub, files] of Object.entries(b.knowledge)) {
    kids.push(h('div', { class: 'k-head', text: sub + (sub === 'lists'
      ? ` · ${b.open_tasks} open task${b.open_tasks === 1 ? '' : 's'}` : '') }));
    files.forEach(f => kids.push(card(f.file, '', [], () => openNote(f.path))));
    kCount += files.length;
  }
  fill('knowledge', kids, kCount);
}

async function openNote(path) {
  try {
    const r = await api.get('/api/note?path=' + encodeURIComponent(path));
    modal(r.path, md(r.content), { wide: true });
  } catch (e) { toast(e.message, 'bad'); }
}

function refine(slug) {
  const box = h('textarea', { placeholder: 'I like X, but…' });
  modal('Refine: ' + title(slug), h('div', {},
    h('p', { class: 'hint', text: 'Say what you\'d change — "drop the SMS part", "make it a browser extension instead", "budget of £50". Your words are attached to the idea verbatim and it goes back through Processing for a fresh plan. The previous plan is kept as an older version.' }),
    box), {
    actions: [{ label: 'Send back through the pipeline', kind: 'primary', run: async close => {
      const feedback = box.value.trim();
      if (!feedback) return;
      try {
        await api.post('/api/refine', { slug, feedback });
        close();
        line('banner', `── feedback attached to ${slug} — re-planning ──`);
        refresh();
        run('develop', { slug });
      } catch (e) { toast(e.message, 'bad'); }
    } }] });
  box.focus();
}

async function exportBrain(slug) {
  try {
    const r = await api.post('/api/export', { slug });
    toast('Mirrored into your vault: ' + r.path, 'good', 6000);
    refresh();
  } catch (e) { toast(e.message, 'bad', 7000); }
}

// --------------------------------------------------------------- mind

function line(cls, text) {
  log().querySelector('.empty')?.remove();
  const d = h('div', { class: 'ev-line ' + cls, text });
  log().append(d);
  log().scrollTop = log().scrollHeight;
  return d;
}
function closeTokens() { tokensEl?.classList.remove('live'); tokensEl = null; }

function handle(ev) {
  switch (ev.event) {
    case 'start': closeTokens(); line('banner', '── ' + ev.log + ' ──'); break;
    case 'note': {
      closeTokens();
      line('note-h', '▸ ' + ev.file).append(h('span', { class: 'x', text: ev.excerpt }));
      break;
    }
    case 'prompt': {
      closeTokens();
      log().append(h('details', {}, h('summary', { text: '▸ ' + ev.label }), h('pre', { text: ev.text })));
      break;
    }
    case 'thinking':
      closeTokens();
      line('think', '… ' + ev.label);
      tokensEl = h('div', { class: 'tokens live' });
      log().append(tokensEl);
      break;
    case 'token':
      if (tokensEl) { tokensEl.textContent += ev.text; tokensEl.scrollTop = tokensEl.scrollHeight; }
      break;
    case 'decision': {
      closeTokens();
      log().append(h('div', { class: 'decision' },
        h('span', { class: 'k', text: ev.type }), ' ',
        h('span', { text: Math.round(ev.confidence * 100) + '% confident' }),
        h('div', { text: ev.reasoning }),
        h('div', { class: 'conf-bar' }, h('i', { style: { width: (ev.confidence * 100) + '%' } }))));
      break;
    }
    case 'sources': closeTokens(); line('moved', '⌕ ' + ev.log); break;
    case 'question': closeTokens(); line('quest', '? needs you: ' + ev.questions.join(' | ')); break;
    case 'moved': case 'file_written': closeTokens(); line('moved', '→ ' + ev.to); break;
    case 'error': closeTokens(); line('err', '✗ ' + ev.log); break;
    case 'heartbeat': line('think', ev.log); break;
    case 'stage_done': closeTokens(); line('banner', '── ' + ev.log + ' ──'); refresh(); break;
  }
  log().scrollTop = log().scrollHeight;
}

function setButtons() {
  $('#btn-triage').disabled = $('#btn-develop').disabled = !!running;
}

function run(stage, params = {}) {
  if (running) { toast('A run is already in progress.'); return; }
  $('#mind').classList.remove('closed');
  running = eventSource('/api/run?' + new URLSearchParams({ stage, ...params }), handle, ok => {
    running = null;
    closeTokens();
    setButtons();
    if (!ok) line('err', '✗ the stream ended unexpectedly (is another run in progress?)');
    refresh();
    bus.emit('pipeline:changed');
    bus.emit('projects:changed');
  });
  setButtons();
}

// ---------------------------------------------------------- lifecycle

export function init() {
  $('#capture').onsubmit = async e => {
    e.preventDefault();
    const text = $('#idea').value.trim();
    if (!text) return;
    try {
      const j = await api.post('/api/capture', { text });
      $('#idea').value = '';
      await refresh();
      bus.emit('pipeline:changed');
      if (autoPipeline && !running) run('auto', { file: j.file });
      else toast('Captured — ' + j.file, 'good');
    } catch (err) { toast(err.message, 'bad'); }
  };
  $('#btn-triage').onclick = () => run('triage');
  $('#btn-develop').onclick = () => run('develop');
  $('#btn-mind').onclick = () => $('#mind').classList.toggle('closed');
  // runs started from chat or by voice think out loud here too
  bus.on('pipeline:event', ev => {
    if (ev.event === 'start') $('#mind').classList.remove('closed');
    handle(ev);
  });
  bus.on('pipeline:changed', () => { if (visible) refresh(); });
  bus.on('state', st => {
    autoPipeline = st.switches.auto_pipeline;
    brainReady = st.switches.brain;
    const before = JSON.stringify(projects.map(p => [p.slug, p.autopilot, p.current, p.done]));
    projects = st.projects;
    if (visible && before !== JSON.stringify(projects.map(p => [p.slug, p.autopilot, p.current, p.done])))
      refresh();
  });
}

export function show() { visible = true; refresh(); }
export function hide() { visible = false; }

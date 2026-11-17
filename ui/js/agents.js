// The Agents view: the standing team, the library of specialists that
// installed skill sources bring along, background missions, and a bench
// to run any of them by hand.

import { $, h, clear, api, stream, bus, toast, badge } from './core.js';

let visible = false, picked = null, busy = false, timer = null;
let library = [], showAll = false;

function agentCard(a, mini = false) {
  const el = h('button', { class: 'agent' + (mini ? ' mini' : '') +
      (picked && picked.name === a.name ? ' sel' : ''),
      onclick: () => pick(a) },
    h('h3', { text: '⬡ ' + a.name }),
    h('div', { class: 'role', text: mini && a.role.length > 150 ? a.role.slice(0, 149) + '…' : a.role }),
    badge(a.model, 'model'),
    (a.skills || []).map(s => badge(s, 'skill')),
    a.web ? badge('web research', 'web') : null,
    a.library ? badge(a.source) : null);
  return el;
}

function pick(a) {
  picked = a;
  clear($('#agent-picked'), h('b', { text: a.name }), ' — ', a.role || 'no description',
    a.library ? h('span', { class: 'dim', text: '  (library specialist from ' + a.source + ')' }) : null);
  $('#agent-picked').classList.remove('hint');
  draw();
}

function draw() {
  for (const el of document.querySelectorAll('#agents-list .agent'))
    el.classList.toggle('sel', !!picked && el.firstChild.textContent === '⬡ ' + picked.name);
}

function renderLibrary() {
  const q = $('#library-filter').value.trim().toLowerCase();
  const rows = library.filter(a => !q || a.name.includes(q) || a.role.toLowerCase().includes(q));
  const shown = showAll || q ? rows : rows.slice(0, 8);
  clear($('#agents-library'), shown.map(a => agentCard(a, true)),
    rows.length > shown.length ? h('button', { class: 'btn small ghost',
      text: 'show all ' + rows.length, onclick: () => { showAll = true; renderLibrary(); } }) : null);
}

async function load() {
  let a, s;
  try {
    [a, s] = await Promise.all([api.get('/api/agents'), api.get('/api/skills')]);
  } catch { return; }
  clear($('#agents-core'), a.agents.map(x => agentCard(x)));
  library = a.library;
  $('#n-library').textContent = library.length || '';
  $('#library-filter').hidden = library.length < 9;
  $('#library-hint').textContent = library.length
    ? 'Specialists that came with your installed skill sources. Autopilot offers the relevant ones to the planner for each project — you can also run one here.'
    : 'None installed. `bin/aria install-skills` brings in ECC\'s specialist agents (reviewers, build-fixers, architects…) along with its skills.';
  renderLibrary();
  $('#skills-note').textContent = s.skills.length
    ? s.skills.length + ' skills installed — autopilot picks the relevant ones per task'
    : 'no skills installed yet — run bin/aria install-skills';
  if (!picked && a.agents.length) pick(a.agents[0]);
}

async function run() {
  const task = $('#agent-task').value.trim();
  if (!task || busy) return;
  if (!picked) { toast('Pick an agent on the left first.'); return; }
  busy = true;
  $('#agent-run').disabled = true;
  const out = $('#agent-out');
  clear(out, h('div', { class: 'dim', text: '⬡ ' + picked.name + ' working…' }), h('div', {}));
  const body = out.lastChild;
  try {
    await stream('/api/agent', { name: picked.name, task }, ev => {
      if (ev.event === 'token') { body.textContent += ev.text; out.scrollTop = out.scrollHeight; }
      else if (ev.event === 'start' && ev.log) out.firstChild.textContent = '⬡ ' + ev.log;
      else if (ev.event === 'thinking' && ev.label) out.firstChild.textContent = '⬡ ' + ev.label + '…';
      else if (ev.event === 'error') body.textContent += '\n⚠ ' + ev.log;
    });
    out.append(h('div', { class: 'dim', text: '— done' }));
  } catch (e) {
    out.append(h('div', { class: 'dim', text: '⚠ ' + e.message }));
  }
  busy = false;
  $('#agent-run').disabled = false;
}

async function queue() {
  const task = $('#agent-task').value.trim();
  if (!task) return;
  if (!picked) { toast('Pick an agent on the left first.'); return; }
  try {
    await api.post('/api/mission', { agent: picked.name, task });
    toast('Mission queued for the ' + picked.name + '.', 'good');
    $('#agent-task').value = '';
    loadMissions();
    bus.emit('projects:changed');
  } catch (e) { toast(e.message, 'bad'); }
}

async function loadMissions() {
  let r;
  try { r = await api.get('/api/missions'); } catch { return; }
  const mark = { running: '⟳ ', queued: '… ', done: '✓ ', failed: '✗ ' };
  clear($('#missions'), r.missions.slice().reverse().map(m =>
    h('button', { class: 'agent mini', onclick: async () => {
        try {
          const n = await api.get('/api/note?path=' + encodeURIComponent(m.path));
          clear($('#agent-out'), h('div', { text: n.content }));
        } catch (e) { toast(e.message, 'bad'); }
      } },
      h('h3', { text: (mark[m.status] || '') + m.id }),
      h('div', { class: 'role', text: m.task }),
      badge(m.status, m.status === 'done' ? 'ok' : m.status === 'failed' ? 'bad' : 'model'))));
  if (!r.missions.length)
    $('#missions').append(h('div', { class: 'hint', text: 'No missions yet — say “mission strategist to …” in chat, or queue one on the right.' }));
}

export function init() {
  $('#agent-run').onclick = run;
  $('#agent-mission').onclick = queue;
  $('#library-filter').oninput = renderLibrary;
}

export function show() {
  visible = true;
  load();
  loadMissions();
  clearInterval(timer);
  timer = setInterval(() => { if (visible) loadMissions(); }, 15000);
}

export function hide() { visible = false; clearInterval(timer); }

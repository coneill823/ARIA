// Settings: everything that is switchable, in one place — her models,
// her reach (the web, your Obsidian brain, GitHub) and how autopilot
// behaves. Saved to system/config.yml by the server.

import { $, h, clear, api, bus, toast } from './core.js';

let cfg = null;
const fields = {};
let found = null, scanning = false, garageTimer = null;

// ---- the giants: one GGUF model per role, swapped in on demand ----------

const STATE_SAY = {
  unset: 'not set', stopped: 'ready to load', loading: 'loading…',
  ready: 'loaded', external: 'served by another llama-server',
  incomplete: 'incomplete', failed: 'failed to load',
};

function giantRow(row) {
  const key = 'giant_' + row.role;
  const input = h('input', { type: 'text', value: (cfg.giants || {})[row.role] || '',
    placeholder: 'path to the first part, …-00001-of-0000N.gguf' });
  fields[key] = () => input.value.trim();
  const busy = row.state === 'loading';
  const btn = row.state === 'ready'
    ? h('button', { class: 'btn small', text: 'Unload', onclick: () => garage('unload', row.role) })
    : ['stopped', 'failed'].includes(row.state)
      ? h('button', { class: 'btn small', text: row.state === 'failed' ? 'Retry' : 'Load now',
                      onclick: () => garage('load', row.role) })
      : null;
  return h('div', { class: 'field giant' },
    h('label', { text: row.role }),
    input,
    h('div', { class: 'giant-state' },
      h('span', { class: 'pill ' + ({ ready: 'running', external: 'running', loading: 'waiting',
                                       failed: 'stalled', incomplete: 'ask' }[row.state] || ''),
                  text: STATE_SAY[row.state] || row.state }),
      row.label ? h('span', { class: 'hint', text: row.label + (row.gb ? ' · ' + row.gb + ' GB' : '')
        + (row.parts > 1 ? ' · ' + row.found + ' of ' + row.parts + ' parts' : '') }) : null,
      btn, busy ? h('span', { class: 'spin' }) : null),
    h('div', { class: 'hint', text: 'For ' + row.says + '.' + (row.error ? ' ' + row.error : '') }));
}

async function garage(action, role) {
  try {
    await api.post('/api/garage', { action, role });
    toast(action === 'load' ? 'Loading the ' + role + ' giant — the first load can take several minutes.'
                            : 'The ' + role + ' giant is unloaded.', 'good', 6000);
  } catch (e) { toast(e.message, 'bad', 8000); }
  refreshGarage();
}

async function refreshGarage() {
  try { cfg.garage = await api.get('/api/garage'); } catch { return; }
  const host = $('#giant-rows');
  if (!host) return;
  clear(host, Object.values(cfg.garage.roles).map(giantRow));
  const loading = Object.values(cfg.garage.roles).some(r => r.state === 'loading');
  clearTimeout(garageTimer);
  if (loading) garageTimer = setTimeout(refreshGarage, 3000);
}

async function scanModels(folder) {
  scanning = true; renderFound();
  try { found = (await api.post('/api/garage', { action: 'scan', folder })).found; }
  catch (e) { toast(e.message, 'bad'); found = []; }
  scanning = false; renderFound();
}

function renderFound() {
  const host = $('#giant-found');
  if (!host) return;
  if (scanning) return clear(host, h('div', { class: 'hint', text: 'Looking for .gguf models…' }));
  if (!found) return clear(host);
  if (!found.length) return clear(host, h('div', { class: 'hint', text: 'No .gguf models found there.' }));
  clear(host, h('table', { class: 'found' }, found.map(m => {
    const sel = h('select', {}, [['', 'use as…'], ['general', 'general'], ['research', 'research'], ['coder', 'coder']]
      .map(([v, n]) => h('option', { value: v, text: n, selected: v === m.guess })));
    return h('tr', {},
      h('td', {}, h('b', { text: m.name }), h('div', { class: 'hint', text: m.path })),
      h('td', { text: m.gb + ' GB' }),
      h('td', { text: m.complete ? (m.parts > 1 ? m.parts + ' parts' : '1 file') : m.why, class: m.complete ? '' : 'warn' }),
      h('td', {}, sel, ' ', h('button', { class: 'btn small', text: 'Use', onclick: async () => {
        if (!sel.value) return toast('Pick a role first.', 'bad');
        try {
          cfg = await api.post('/api/config', { ['giant_' + sel.value]: m.path });
          toast(m.name + ' is now the ' + sel.value + ' giant.', 'good');
          render();
        } catch (e) { toast(e.message, 'bad', 8000); }
      } })));
  })));
}

function text(key, label, hint, placeholder = '') {
  const input = h('input', { type: 'text', value: cfg[key] ?? '', placeholder });
  fields[key] = () => input.value.trim();
  return h('div', { class: 'field' }, h('label', { text: label }), input,
           hint ? h('div', { class: 'hint', text: hint }) : null);
}

function toggle(key, label, hint) {
  const input = h('input', { type: 'checkbox' });
  input.checked = !!cfg[key];
  fields[key] = () => (input.checked ? 'true' : 'false');
  return h('div', { class: 'field' }, h('label', { text: label }),
           h('label', { class: 'toggle' }, input, h('span', { class: 'hint', text: hint || '' })));
}

function choice(key, label, options, hint) {
  const sel = h('select', {}, options.map(([value, name]) =>
    h('option', { value, text: name, selected: String(cfg[key]) === value })));
  fields[key] = () => sel.value;
  return h('div', { class: 'field' }, h('label', { text: label }), sel,
           hint ? h('div', { class: 'hint', text: hint }) : null);
}

function section(titleText, hint, ...kids) {
  return h('div', { class: 'set' }, h('h2', { text: titleText }),
           h('div', { class: 'hint', text: hint }), kids);
}

function render() {
  for (const k of Object.keys(fields)) delete fields[k];
  const tier = (key, hint) => {
    const input = h('input', { type: 'text', value: (cfg.tiers_set || {})[key] || '',
                               placeholder: 'now: ' + ((cfg.tiers || {})[key] || '—') });
    fields[key] = () => input.value.trim();
    return h('div', { class: 'field' }, h('label', { text: key }), input,
             h('div', { class: 'hint', text: hint }));
  };
  clear($('#settings-col'),
    section('Models', 'Three tiers, each backend/model. Autopilot gives every task the tier — or the specific model — it judges necessary. These are the everyday models: she plans, reviews and chats on them, so they should be quick.',
      tier('fast', 'triage, chat, summaries, memory — e.g. ollama/llama3.1:8b'),
      tier('smart', 'plans, code, verification — e.g. ollama/qwen3:30b'),
      tier('deep', 'the hardest reasoning; leave empty to fall back to smart — or name a giant\'s role: general, research, coder'),
      text('context_tokens', 'Context window', 'Tokens requested from Ollama for every call, and the budget prompts are sized to. 0 leaves the backend\'s own setting alone.')),
    section('Giant models', 'Your biggest models, one role each. When a task needs one, A.R.I.A. starts its llama-server and unloads the others first — giants share one GPU and one page cache. A model that is still downloading is skipped until every part is there.',
      h('div', { id: 'giant-rows' }, Object.values((cfg.garage || {}).roles || {}).map(giantRow)),
      h('div', { class: 'field' }, h('label', { text: 'Find models' }),
        h('div', { class: 'row' },
          (() => { const f = h('input', { type: 'text', id: 'giant-folder', placeholder: 'a folder, or leave empty for the usual places (vendor/models, ~/models, ~/Downloads)' });
                   return [f, ' ', h('button', { class: 'btn', text: 'Scan', onclick: () => scanModels(f.value.trim()) })]; })()),
        h('div', { id: 'giant-found' })),
      choice('giants_use', 'When to use them', [
        ['auto', 'when the work is worth the wait (the planner decides)'],
        ['always', 'always: code to coder, research to research, the rest to general'],
        ['never', 'only where I name them']],
        'Giants answer at about 0.5–2 words a second: one task can take an hour. Planning and review stay on your fast tiers.'),
      text('giants_loaded', 'Loaded at once', 'How many giants may be in memory together. 1 is safest on 16 GB VRAM / 64 GB RAM.'),
      text('giants_idle_minutes', 'Unload when idle (minutes)', '0 = keep the last one loaded.')),
    section('Internet', 'On: “search for …” and “read <url>” work in chat and by voice, new ideas are researched on the live web, and autopilot tasks can pull in sourced material. Searches leave your machine while this is on.',
      toggle('web_access', 'Web access', cfg.web_access ? 'on — searching via ' + (cfg.web_providers.join(' → ') || 'nothing (search disabled)') : 'off — she will say plainly that she cannot browse'),
      choice('web_search', 'Search provider', [
        ['auto', 'auto — first that answers'], ['duckduckgo', 'DuckDuckGo (no key)'],
        ['brave', 'Brave API (key in ARIA_KEY_BRAVE)'], ['searxng', 'your SearxNG'],
        ['wikipedia', 'Wikipedia only'], ['none', 'none — fetch only']]),
      text('web_search_url', 'SearxNG URL', 'Only for your own instance, JSON format enabled.', 'http://localhost:8888/search'),
      text('web_allowlist', 'Limit fetching to', 'Comma-separated sites. Empty = any public site. Your local network is always off limits.', 'en.wikipedia.org, docs.python.org')),
    section('Obsidian brain', 'She writes only inside <vault>/ARIA/: project hubs, notes you dictate (“note to brain: …”), research with its sources, and a daily journal.',
      text('obsidian_vault', 'Vault folder', cfg.obsidian_vault ? (cfg.brain_ready ? 'connected' : 'that folder does not exist') : 'not set', '/home/you/Documents/SecondBrain'),
      toggle('brain_journal', 'Daily journal', 'a line for each thing she does')),
    section('Git & GitHub', 'Autopilot commits every verified task when a project is its own repository — on the branch aria/autopilot, never on your base branch.',
      toggle('autopilot_git', 'Commit each task', 'local commits only'),
      toggle('github_access', 'GitHub', cfg.gh ? 'push the branch and keep a pull request open (gh CLI found)' : 'needs the gh CLI: install it, then run gh auth login'),
      text('github_base_branch', 'Base branch', 'Pull requests target this branch.')),
    section('Autopilot', 'How long she keeps going on her own.',
      choice('autopilot_after_goal', 'When the goal is met', [
        ['improve', 'keep improving until I stop her'], ['stop', 'write the report and stop']],
        'Either way the goal is audited against the requirements first.'),
      text('autopilot_max_hours', 'Stop after (hours)', '0 = no limit. At a budget she finishes the step in flight, writes the report and pauses; resuming is a new run with a fresh budget.'),
      text('autopilot_max_tasks', 'Stop after (tasks)', '0 = no limit. Counts tasks finished in one run, verified or failed.'),
      text('autopilot_max_iterations', 'Stop after (batches)', '0 = no limit.'),
      text('autopilot_stall_limit', 'Pause when stuck after', 'Batches in a row that complete nothing before she pauses herself as stalled.')),
    section('Your desktop', 'What the Files tab opens a project with. Leave empty and she uses the first one she finds on this machine.',
      text('editor', 'Editor', (cfg.desktop || {}).editor ? 'using: ' + cfg.desktop.editor : 'none found — name the command of yours, e.g. code, zed, kate', 'code'),
      text('terminal', 'Terminal', (cfg.desktop || {}).terminal ? 'using: ' + cfg.desktop.terminal : 'none found — e.g. konsole --workdir', 'konsole --workdir')),
    section('Pipeline', 'What happens when an idea is captured.',
      toggle('auto_pipeline', 'Auto-pipeline', 'triage and plan every new idea straight away — your approval is still required before any work starts')),
    h('div', { class: 'savebar' },
      h('span', { class: 'hint', text: 'Saved to system/config.yml' }),
      h('button', { class: 'btn', text: 'Revert', onclick: load }),
      h('button', { class: 'btn primary', text: 'Save settings', onclick: save })));
}

async function load() {
  try { cfg = await api.get('/api/config'); render(); renderFound(); refreshGarage(); }
  catch (e) { clear($('#settings-col'), h('div', { class: 'empty', text: e.message })); }
}

async function save() {
  const body = {};
  for (const [key, read] of Object.entries(fields)) body[key] = read();
  for (const key of ['context_tokens', 'autopilot_max_hours', 'autopilot_max_tasks',
                     'autopilot_max_iterations', 'giants_idle_minutes'])
    if (body[key] === '') body[key] = '0';
  if (body.giants_loaded === '') body.giants_loaded = '1';
  if (body.autopilot_stall_limit === '') delete body.autopilot_stall_limit;
  if (!body.github_base_branch) delete body.github_base_branch;
  try {
    cfg = await api.post('/api/config', body);
    toast('Settings saved.', 'good');
    render();
    bus.emit('settings:changed');
  } catch (e) { toast(e.message, 'bad', 7000); }
}

export function init() {}
export function show() { load(); }
export function hide() { clearTimeout(garageTimer); }

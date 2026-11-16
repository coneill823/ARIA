// Settings: everything that is switchable, in one place — her models,
// her reach (the web, your Obsidian brain, GitHub) and how autopilot
// behaves. Saved to system/config.yml by the server.

import { $, h, clear, api, bus, toast } from './core.js';

let cfg = null;
const fields = {};

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
    section('Models', 'Three tiers, each backend/model. Autopilot gives every task the tier — or the specific model — it judges necessary.',
      tier('fast', 'triage, chat, summaries, memory — e.g. ollama/llama3.1:8b'),
      tier('smart', 'plans, code, verification — e.g. ollama/qwen3:30b'),
      tier('deep', 'the hardest reasoning; leave empty to fall back to smart'),
      text('context_tokens', 'Context window', 'Tokens requested from Ollama for every call, and the budget prompts are sized to. 0 leaves the backend\'s own setting alone.')),
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
      text('autopilot_max_hours', 'Stop after (hours)', '0 = no limit. She finishes the step in flight and writes the report.'),
      text('autopilot_max_iterations', 'Stop after (batches)', '0 = no limit.')),
    section('Pipeline', 'What happens when an idea is captured.',
      toggle('auto_pipeline', 'Auto-pipeline', 'triage and plan every new idea straight away — your approval is still required before any work starts')),
    h('div', { class: 'savebar' },
      h('span', { class: 'hint', text: 'Saved to system/config.yml' }),
      h('button', { class: 'btn', text: 'Revert', onclick: load }),
      h('button', { class: 'btn primary', text: 'Save settings', onclick: save })));
}

async function load() {
  try { cfg = await api.get('/api/config'); render(); }
  catch (e) { clear($('#settings-col'), h('div', { class: 'empty', text: e.message })); }
}

async function save() {
  const body = {};
  for (const [key, read] of Object.entries(fields)) body[key] = read();
  for (const key of ['context_tokens', 'autopilot_max_hours', 'autopilot_max_iterations'])
    if (body[key] === '') body[key] = '0';
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
export function hide() {}

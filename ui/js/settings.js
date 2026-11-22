// Settings: everything that is switchable, in one place — her models,
// her reach (the web, your Obsidian brain, GitHub) and how autopilot
// behaves. The server saves them to system/config.local.yml — the owner's
// own file, which updates never touch (config.yml holds the defaults).

import { $, h, clear, api, bus, toast } from './core.js';
import { sayNow } from './talk.js';

let cfg = null;
const fields = {};
let found = null, scanning = false, garageTimer = null, voiceTimer = null;
const testing = {};             // role -> when its end-to-end test was asked for

// ---- the giants: one GGUF model per role, swapped in on demand ----------

const STATE_SAY = {
  unset: 'not set', stopped: 'ready to load', loading: 'loading…',
  ready: 'loaded', external: 'served by another llama-server',
  incomplete: 'incomplete', failed: 'failed to load', blocked: 'cannot start',
};

const took = s => (s < 90 ? Math.round(s) + ' s' : (s / 60).toFixed(1) + ' min');

// The outcome of the last end-to-end test: the proof that this model
// really loads and answers on this machine (or the reason it does not).
function testLine(row) {
  const t = row.test;
  if (testing[row.role] && !(t && t.at * 1000 >= testing[row.role]))
    return h('div', { class: 'hint test' }, h('span', { class: 'spin' }),
      ' Testing: loading it, then asking it one question. This can take several minutes — the result appears here.');
  if (!t) return null;
  const when = new Date(t.at * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  return t.ok
    ? h('div', { class: 'hint test ok', text: '✓ Tested ' + when + ': loaded in ' + took(t.load_seconds)
        + ', answered in ' + took(t.seconds) + ' (' + (t.words / Math.max(t.seconds, .1)).toFixed(1)
        + ' words a second) — “' + t.reply + '”' })
    : h('div', { class: 'hint test bad', text: '✗ Test failed ' + when + ': ' + t.error });
}

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
  const test = ['stopped', 'failed', 'ready'].includes(row.state)
    ? h('button', { class: 'btn small', text: 'Test', title: 'load it and ask it one question — proof that it works here',
                    onclick: () => garage('test', row.role) }) : null;
  return h('div', { class: 'field giant' },
    h('label', { text: row.role }),
    input,
    h('div', { class: 'giant-state' },
      h('span', { class: 'pill ' + ({ ready: 'running', external: 'running', loading: 'waiting',
                                       failed: 'stalled', blocked: 'stalled', incomplete: 'ask' }[row.state] || ''),
                  text: STATE_SAY[row.state] || row.state }),
      row.label ? h('span', { class: 'hint', text: row.label + (row.gb ? ' · ' + row.gb + ' GB' : '')
        + (row.parts > 1 ? ' · ' + row.found + ' of ' + row.parts + ' parts' : '') }) : null,
      btn, test, busy ? h('span', { class: 'spin' }) : null),
    h('div', { class: 'hint', text: 'For ' + row.says + '.' + (row.error ? ' ' + row.error : '') }),
    testLine(row));
}

async function garage(action, role) {
  try {
    await api.post('/api/garage', { action, role });
    if (action === 'test') testing[role] = Date.now();
    toast(action === 'load' ? 'Loading the ' + role + ' giant — the first load can take several minutes.'
        : action === 'test' ? 'Testing the ' + role + ' giant — the result will appear under it.'
        : 'The ' + role + ' giant is unloaded.', 'good', 6000);
  } catch (e) { toast(e.message, 'bad', 8000); }
  refreshGarage();
}

async function refreshGarage() {
  try { cfg.garage = await api.get('/api/garage'); } catch { return; }
  const host = $('#giant-rows');
  if (!host) return;
  const rows = Object.values(cfg.garage.roles);
  for (const r of rows)                       // a test that has reported is over
    if (testing[r.role] && r.test && r.test.at * 1000 >= testing[r.role]) delete testing[r.role];
  clear(host, rows.map(giantRow));
  const waiting = rows.some(r => r.state === 'loading') || Object.keys(testing).length;
  clearTimeout(garageTimer);
  if (waiting) garageTimer = setTimeout(refreshGarage, 3000);
}

// ---- her voice and her ears ---------------------------------------------

function voiceStatus() {
  const v = cfg.voice, t = v.tts, s = v.stt, job = v.install;
  const line = (label, ok, textOk, why) => h('div', { class: 'vrow' },
    h('span', { class: 'pill ' + (ok ? 'running' : 'ask'), text: label }),
    h('span', { class: 'hint', text: ok ? textOk : why }));
  const missing = !t.natural || !s.engine;
  return h('div', { id: 'voice-status' },
    line('speaking', !!t.engine, t.engine === 'piper'
      ? 'her own voice, made on this machine (' + t.voice + ')'
      : 'a plain synthetic voice (espeak) — set up local speech below for a natural one', t.why),
    line('listening', !!s.engine, 'recognised on this machine (' + s.model + ' model)', s.why),
    job.running ? h('div', { class: 'hint test' }, h('span', { class: 'spin' }), ' ' + job.step + '…') : null,
    job.error && !job.running ? h('div', { class: 'hint test bad', text: '✗ ' + job.error }) : null,
    missing && !job.running ? h('div', { class: 'row' },
      h('button', { class: 'btn primary', text: 'Set up local speech', onclick: installVoice }),
      h('span', { class: 'hint', text: 'Downloads about 700 MB into vendor/voice — a natural voice and a recogniser, in a private Python environment. Nothing is installed system-wide, and nothing you say leaves this machine.' })) : null);
}

async function installVoice() {
  try {
    await api.post('/api/voice', { action: 'install' });
    toast('Setting up local speech — this takes a few minutes; progress shows here.', 'good', 7000);
  } catch (e) { toast(e.message, 'bad', 8000); }
  refreshVoice();
}

async function refreshVoice() {
  try { cfg.voice = await api.get('/api/voice'); } catch { return; }
  const host = $('#voice-status');
  if (!host) return;
  host.replaceWith(voiceStatus());
  clearTimeout(voiceTimer);
  if (cfg.voice.install.running) voiceTimer = setTimeout(refreshVoice, 2500);
  else bus.emit('settings:changed');          // the chat learns what she has now
}

function voicePicker() {
  const v = cfg.voice, have = new Set(v.voices);
  const names = [...new Set([...v.voices, ...v.suggested.map(x => x.name)])];
  const says = Object.fromEntries(v.suggested.map(x => [x.name, x.says]));
  const current = v.tts.engine === 'piper' ? v.tts.voice : (cfg.voice_name || '');
  const sel = h('select', {}, names.map(n => h('option', { value: n, selected: n === current,
    text: n + (says[n] ? ' — ' + says[n] : '') + (have.has(n) ? '' : '  (download, ~65 MB)') })));
  const use = h('button', { class: 'btn small', text: 'Use', onclick: async () => {
    use.disabled = true;
    toast(have.has(sel.value) ? 'Switching voice…' : 'Downloading ' + sel.value + '…', 'good', 5000);
    try {
      await api.post('/api/voice', { action: 'voice', voice: sel.value });
      cfg = await api.get('/api/config');
      render();
      bus.emit('settings:changed');
      setTimeout(() => sayNow('This is how I sound now.'), 400);
    } catch (e) { toast(e.message, 'bad', 8000); use.disabled = false; }
  } });
  const sample = h('button', { class: 'btn small', text: '▶ Hear her',
    onclick: () => sayNow('Good evening. All systems are operational, and I am listening.') });
  return h('div', { class: 'field' }, h('label', { text: 'Voice' }),
    v.set_up ? h('div', { class: 'row' }, sel, ' ', use, ' ', sample)
             : h('div', { class: 'row' }, sample),
    h('div', { class: 'hint', text: v.set_up
      ? 'Voices are downloaded once and kept. Hear them all at rhasspy.github.io/piper-samples.'
      : 'More voices become available once local speech is set up.' }));
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
      text('context_tokens', 'Context window', 'Tokens requested from Ollama for every call, and the budget prompts are sized to. A model\'s prompt, its reasoning and its answer all share it. 0 leaves the backend\'s own setting alone.'),
      toggle('ollama_autostart', 'Start the model server', 'when she starts — or finds it away — she starts Ollama herself, the way it is installed on this machine'),
      toggle('models_warm', 'Load the quick model at start', 'so the first reply does not wait for the model to be read from disk'),
      text('ollama_keep_alive', 'Keep models loaded for', 'How long the fast and smart models stay in memory after they were last used: 30m, 2h, -1 for always. Empty leaves it to Ollama (5 minutes), as a deep-tier model always is. A giant that needs the memory still gets it.'),
      text('ollama_command', 'Start command', 'Leave empty: she works out how Ollama is run on this machine. Only where it is started in a way of its own (a container, say) — the command that starts it, e.g. docker start ollama'),
      choice('thinking', 'Models that reason first', [
        ['auto', 'automatic — reason where there is time and room for it'],
        ['always', 'always — leave every model to itself'],
        ['never', 'never — ask for the answer alone']],
        'For models that think at length before they answer (qwen3, deepseek-r1). Reasoning costs time and room in the window: on "auto" chat and triage stay quick, and the bigger calls reason only if the window can hold the reasoning and the answer — a larger context window lets her think more. The deep tier and the giants are left alone.')),
    section('Giant models', 'Your biggest models, one role each. When a task needs one, A.R.I.A. starts its llama-server and unloads the others first — giants share one GPU and one page cache. A model that is still downloading is skipped until every part is there.',
      h('div', { id: 'giant-rows' }, Object.values((cfg.garage || {}).roles || {}).map(giantRow)),
      h('div', { class: 'field' }, h('label', { text: 'Find models' }),
        h('div', { class: 'row' },
          (() => { const f = h('input', { type: 'text', id: 'giant-folder', placeholder: 'a folder, or leave empty for the usual places (vendor/models, ~/models, ~/Downloads)' });
                   return [f, ' ', h('button', { class: 'btn', text: 'Scan', onclick: () => scanModels(f.value.trim()) })]; })()),
        h('div', { id: 'giant-found' })),
      choice('giants_use', 'When to use them', [
        ['auto', 'for hard tasks, and for any task the everyday model fails twice'],
        ['always', 'always: code to coder, research to research, the rest to general'],
        ['never', 'only where I name them']],
        'Giants answer at about 0.5–2 words a second: one task can take an hour. Planning and review stay on your fast tiers. A role with no model of its own is covered by the next best giant. Every task card shows which model it runs on, and why.'),
      text('giants_loaded', 'Loaded at once', 'How many giants may be in memory together. 1 is safest on 16 GB VRAM / 64 GB RAM.'),
      text('giants_idle_minutes', 'Unload when idle (minutes)', 'A giant nobody has used for this long is unloaded, so it does not sit on the memory. 0 = keep the last one loaded.'),
      toggle('giants_free_memory', 'Take turns with Ollama', 'giants and everyday models share the memory in turn: Ollama unloads before a giant loads, and a giant that has finished its answer is unloaded before the next everyday call (never one mid-answer)')),
    section('Voice', 'How she speaks and listens. In her own window there is no browser speech service to borrow, so both happen on this machine.',
      voiceStatus(),
      voicePicker(),
      text('voice_speed', 'Speaking speed', '1 is natural; 0.8 slower, 1.2 quicker.'),
      choice('voice_engine', 'Her voice comes from', [
        ['auto', 'this machine when it has a natural voice, else the browser'],
        ['local', 'this machine, always'],
        ['browser', 'the browser\'s own voices']]),
      choice('voice_input', 'She listens with', [
        ['auto', 'this machine when it can, else the browser'],
        ['local', 'this machine, always (nothing leaves it)'],
        ['browser', 'the browser\'s speech service (Chrome sends audio to Google)']]),
      text('stt_model', 'Recognition model', 'tiny, base, small or medium — bigger is more accurate and slower. base is a good start.')),
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
    section('Autopilot', 'How long she keeps going on her own, and how her work is checked.',
      choice('autopilot_after_goal', 'When the goal is met', [
        ['improve', 'keep improving until I stop her'], ['stop', 'write the report and stop']],
        'Either way the goal is audited against the requirements first.'),
      toggle('autopilot_refine', 'Review & refine', 'when the goal is met, read the whole codebase and refine it without changing what it does'),
      choice('autopilot_checks', 'Run the project\'s tests', [
        ['sandbox', 'after every code change, in a sandbox'],
        ['direct', 'after every code change, without a sandbox'],
        ['off', 'never']],
        (cfg.sandbox || {}).available
          ? 'Sandbox ready (bubblewrap): no network, the machine read-only, a scratch copy of the project. A change that breaks passing tests is rejected.'
          : 'No sandbox on this machine — ' + ((cfg.sandbox || {}).why || 'bubblewrap is missing') + '. Until there is one, “in a sandbox” means the tests are not run, and the report says so.'),
      text('autopilot_max_hours', 'Stop after (hours)', '0 = no limit. At a budget she finishes the step in flight, writes the report and pauses; resuming is a new run with a fresh budget.'),
      text('autopilot_max_tasks', 'Stop after (tasks)', '0 = no limit. Counts tasks finished in one run, verified or failed.'),
      text('autopilot_max_iterations', 'Stop after (batches)', '0 = no limit.'),
      text('autopilot_stall_limit', 'Pause when stuck after', 'Batches in a row that complete nothing before she pauses herself as stalled.')),
    section('Your desktop', 'What the Files tab opens a project with. Leave empty and she uses the first one she finds on this machine.',
      text('editor', 'Editor', (cfg.desktop || {}).editor ? 'using: ' + cfg.desktop.editor : 'none found — name the command of yours, e.g. code, zed, kate', 'code'),
      text('terminal', 'Terminal', (cfg.desktop || {}).terminal ? 'using: ' + cfg.desktop.terminal : 'none found — e.g. konsole --workdir', 'konsole --workdir'),
      toggle('desktop_notifications', 'Notifications', 'tell me on the desktop when a task waits on my decision and when a run ends')),
    section('Pipeline', 'What happens when an idea is captured.',
      toggle('auto_pipeline', 'Auto-pipeline', 'triage and plan every new idea straight away — your approval is still required before any work starts'),
      toggle('business_plans', 'Business plans', 'with every plan she also writes a business plan, what a complete proposal still needs, and who does what — her part and yours'),
      text('based_in', 'Where you are based', 'e.g. United Kingdom. Legal form, tax, licences and data protection differ by country: with this set she names the bodies and rules that apply to you; without it she says what to check.')),
    h('div', { class: 'savebar' },
      h('span', { class: 'hint', text: 'Saved to system/config.local.yml — your own file; updates never touch it' }),
      h('button', { class: 'btn', text: 'Revert', onclick: load }),
      h('button', { class: 'btn primary', text: 'Save settings', onclick: save })));
}

async function load() {
  try {
    cfg = await api.get('/api/config');
    render(); renderFound(); refreshGarage();
    if (cfg.voice.install.running) refreshVoice();
  } catch (e) { clear($('#settings-col'), h('div', { class: 'empty', text: e.message })); }
}

async function save() {
  const body = {};
  for (const [key, read] of Object.entries(fields)) body[key] = read();
  for (const key of ['context_tokens', 'autopilot_max_hours', 'autopilot_max_tasks',
                     'autopilot_max_iterations', 'giants_idle_minutes'])
    if (body[key] === '') body[key] = '0';
  if (body.giants_loaded === '') body.giants_loaded = '1';
  if (body.voice_speed === '') body.voice_speed = '1';
  if (!body.stt_model) delete body.stt_model;
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
export function hide() { clearTimeout(garageTimer); clearTimeout(voiceTimer); }

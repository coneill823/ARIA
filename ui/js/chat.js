// The Chat view: the thread of the current conversation and its composer,
// with the orb — her presence — behind them. All behaviour lives in
// talk.js; this file only draws.

import { $, h, clear, api, bus, toast, md } from './core.js';
import { talk, submit, approveFromChat } from './talk.js';
import * as orb from './orb.js';

const nodes = new WeakMap();          // message object -> its DOM node
let col, thread;

function scrollEnd() { thread.scrollTop = thread.scrollHeight; }

function greet() {
  const tries = ['what are you working on', 'run triage', 'open the projects page',
                 'search for the latest on local LLMs'];
  return h('div', { class: 'greet' },
    h('div', { class: 'big' }, 'How can ', h('em', { text: 'A.R.I.A.' }), ' help today?'),
    h('div', {}, 'Capture an idea, set her to work on a project, ask what changed —',
      h('br'), 'or just talk.'),
    h('div', { class: 'tries' }, tries.map(t =>
      h('button', { text: t, onclick: () => submit(t) }))));
}

function sourcesBlock(msg) {
  if (!msg.sources || !msg.sources.length) return null;
  const save = h('button', { text: 'save to brain', title: 'keep this answer and its sources in your Obsidian vault',
    onclick: async () => {
      try {
        const r = await api.post('/api/brain', {
          title: msg.question || 'web research', text: msg.content,
          sources: msg.sources });
        toast('Saved to your brain: ' + r.name, 'good');
      } catch (e) { toast(e.message, 'bad'); }
    } });
  return h('div', { class: 'sources' },
    h('div', { class: 's-h' }, 'Sources', save),
    msg.sources.map((s, i) => h('a', { href: s.url, target: '_blank', rel: 'noopener noreferrer' },
      h('span', { class: 's-n', text: '[' + (i + 1) + ']' }), s.title || s.url)));
}

function gateBlock(msg) {
  if (!msg.approve) return null;
  const slug = msg.approve;
  return h('div', { class: 'gate' },
    h('div', { class: 'g-t', text: 'This plan is waiting for your approval — the one decision that is yours.' }),
    h('a', { class: 'btn small', href: '/projects?p=' + slug, dataset: { view: 'projects', slug, tab: 'brief' }, text: 'Read the plan' }),
    h('button', { class: 'btn small primary', text: 'Approve & start',
      onclick: e => { e.target.closest('.gate').remove(); msg.approve = null; approveFromChat(slug, true); } }));
}

// A reply about a project carries where it lives and the files it
// mentions — each a link into the Files tab, so "go and look at X" is one
// click.
function projectBlock(msg) {
  const p = msg.project;
  if (!p) return null;
  const to = (file, text, cls) => h('a', { class: cls, text,
    href: '/projects?p=' + p.slug + '&tab=files' + (file ? '&f=' + encodeURIComponent(file) : ''),
    dataset: { view: 'projects', slug: p.slug, tab: 'files', file: file || '' } });
  return h('div', { class: 'sources files' },
    h('div', { class: 's-h' }, 'Files', to('', 'browse them all', 'btn small')),
    h('div', { class: 'where' }, '📁 ', h('code', { text: p.where })),
    (p.files || []).map(f => to(f, f, 'filelink')));
}

function draw(msg) {
  let node = nodes.get(msg);
  const fresh = !node;
  if (msg.role === 'user') {
    node ||= h('div', { class: 'turn you' }, h('div', { class: 'bubble' }));
    node.firstChild.textContent = msg.content;
  } else if (msg.role === 'assistant') {
    node ||= h('div', { class: 'turn aria' }, h('div', { class: 'who', text: 'A.R.I.A.' }),
               h('div', { class: 'body' }), h('div', { class: 'extras' }));
    const body = node.children[1];
    // her replies are markdown (lists, code, emphasis): draw them as such
    if (msg.error || !msg.content) body.textContent = msg.content;
    else clear(body, md(msg.content));
    body.classList.toggle('err', !!msg.error);
    clear(node.children[2],
      msg.status ? h('div', { class: 'status', text: msg.status }) : null,
      msg.pending ? null : sourcesBlock(msg),
      msg.pending ? null : projectBlock(msg),
      msg.pending ? null : gateBlock(msg));
  } else if (msg.role === 'agent') {
    node ||= h('div', { class: 'agentcard' }, h('div', { class: 'ah' }), h('div', { class: 'body' }));
    node.firstChild.textContent = '⬡ ' + msg.name + (msg.pending ? ' — working' : ' — done');
    node.children[1].textContent = msg.content;
    node.children[1].style.whiteSpace = 'pre-wrap';
  } else {
    node ||= h('div', { class: 'turn sys' });
    node.textContent = msg.content;
  }
  if (fresh) { nodes.set(msg, node); col.append(node); }
  return node;
}

function render() {
  const msgs = talk.current ? talk.current.messages : [];
  clear(col);
  orb.setDim(msgs.length > 0);
  if (!msgs.length) { col.append(greet()); return; }
  for (const m of msgs) { nodes.delete(m); draw(m); }
  scrollEnd();
}

const STATE_SAYS = { listening: 'listening…', thinking: 'thinking…', speaking: 'speaking' };

export function init({ makeComposer }) {
  col = $('#thread-col');
  thread = $('#thread');
  const composer = makeComposer();
  $('#composer-slot').append(composer);
  bus.on('talk:reset', render);
  bus.on('talk:add', msg => {
    col.querySelector('.greet')?.remove();
    orb.setDim(true);
    draw(msg);
    scrollEnd();
  });
  bus.on('talk:state', s => {
    const el = $('#orb-state');
    el.textContent = STATE_SAYS[s] || '';
    el.dataset.state = s;
  });
  bus.on('talk:update', msg => {
    if (!nodes.has(msg)) return;
    const pinned = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
    draw(msg);
    if (pinned) scrollEnd();
  });
  init.focus = () => composer.focusInput();
  render();
}

export function show() {
  orb.show();
  scrollEnd();
  init.focus?.();
}

export function hide() { orb.hide(); }

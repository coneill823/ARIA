// Shared building blocks for every view: DOM helpers, the API client, a
// tiny event bus, a safe markdown renderer, toasts and the modal.
//
// Rule for this whole UI: text that came from a model, a file or the web
// is only ever placed with textContent — never innerHTML.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function')
      el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(3)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export function clear(el, ...kids) {
  el.replaceChildren(...kids.flat().filter(Boolean));
  return el;
}

// ------------------------------------------------------------------ api

async function request(path, options) {
  let resp;
  try {
    resp = await fetch(path, { cache: 'no-store', ...options });
  } catch {
    throw Object.assign(new Error("can't reach the A.R.I.A. server"),
                        { offline: true });
  }
  let body = {};
  try { body = await resp.json(); } catch { /* empty or non-JSON */ }
  if (!resp.ok)
    throw Object.assign(new Error(body.error || resp.statusText),
                        { status: resp.status, body });
  return body;
}

export const api = {
  get: path => request(path),
  post: (path, body) =>
    request(path, { method: 'POST', body: JSON.stringify(body || {}) }),
};

// POST a request whose answer is a stream of `data: {json}` events.
export async function stream(path, body, onEvent) {
  let resp;
  try {
    resp = await fetch(path, { method: 'POST', body: JSON.stringify(body) });
  } catch {
    throw new Error("can't reach the A.R.I.A. server");
  }
  if (!resp.ok || !resp.body) {
    let j = {};
    try { j = await resp.json(); } catch { /* not JSON */ }
    throw Object.assign(new Error(j.error || 'request failed'),
                        { status: resp.status, body: j });
  }
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      if (!chunk.startsWith('data: ')) continue;
      let ev;
      try { ev = JSON.parse(chunk.slice(6)); } catch { continue; }
      onEvent(ev);
    }
  }
}

// GET stream (Server-Sent Events) for pipeline stages.
export function eventSource(path, onEvent, onEnd) {
  const es = new EventSource(path);
  let closed = false;
  const end = ok => { if (!closed) { closed = true; es.close(); onEnd?.(ok); } };
  es.onmessage = m => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.event === 'done') end(true); else onEvent(ev);
  };
  es.onerror = () => end(false);
  return () => end(false);
}

// ------------------------------------------------------------------ bus

const subs = {};
export const bus = {
  on(name, fn) {
    (subs[name] ||= []).push(fn);
    return () => { subs[name] = subs[name].filter(f => f !== fn); };
  },
  emit(name, data) {
    for (const fn of subs[name] || []) {
      try { fn(data); } catch (e) { console.error(e); }
    }
  },
};

export const store = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(key);
      return v == null ? fallback : JSON.parse(v);
    } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* full or blocked */ }
  },
};

// ------------------------------------------------------------- markdown
// Small and safe: builds DOM nodes, never parses HTML. Covers what
// A.R.I.A. writes — headings, lists, tables, code, quotes, links.

// Links: [text](https://…) opens outside; [text](relative/path) and a
// `code span` that names a file are handed to `opts.file` — the caller
// says whether that is a file it can show, and what a click does. Nothing
// else ever becomes a link, so no document can smuggle in a scheme.
const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[[^\]\n]+\]\([^)\s]+\))|(https?:\/\/[^\s<>)\]]+)|(\*[^*\s][^*\n]*\*)/g;

function fileLink(opts, target, kids) {
  const path = opts && opts.file ? opts.file(target) : null;
  if (!path) return null;
  return h('a', { class: 'filelink', title: 'open ' + path, href: '#',
                  onclick: e => { e.preventDefault(); opts.open(path); } }, kids);
}

function inline(text, into, opts) {
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) into.append(text.slice(last, m.index));
    const t = m[0];
    if (m[1]) {
      const code = h('code', { text: t.slice(1, -1) });
      into.append(fileLink(opts, t.slice(1, -1), code) || code);
    } else if (m[2]) into.append(h('strong', {}, ...inlineKids(t.slice(2, -2), opts)));
    else if (m[3]) {
      const mm = /^\[([^\]]+)\]\((.+)\)$/.exec(t);
      if (/^https?:\/\//i.test(mm[2]))
        into.append(h('a', { href: mm[2], target: '_blank', rel: 'noopener noreferrer' },
                      ...inlineKids(mm[1])));
      else {                              // a path: a file we can show, or plain text
        let target = mm[2];
        try { target = decodeURIComponent(target); } catch { /* keep as written */ }
        const label = inlineKids(mm[1]);  // no opts: a code span inside is not a second link
        into.append(fileLink(opts, target, label) || h('span', {}, label));
      }
    } else if (m[4]) {
      const url = t.replace(/[.,;:!?]+$/, '');
      into.append(h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', text: url }));
      if (url.length < t.length) into.append(t.slice(url.length));
    } else if (m[5]) into.append(h('em', { text: t.slice(1, -1) }));
    last = m.index + t.length;
  }
  if (last < text.length) into.append(text.slice(last));
  return into;
}
const inlineKids = (text, opts) => [...inline(text, h('span'), opts).childNodes];

// Resolve a link written inside a document at `base` (a path within the
// project) to a path within the project; null if it climbs out of it.
export function resolvePath(base, href) {
  const parts = /^\//.test(href) ? [] : base.split('/').slice(0, -1);
  for (const seg of href.split(/[?#]/)[0].split('/')) {
    if (!seg || seg === '.') continue;
    if (seg === '..') { if (!parts.length) return null; parts.pop(); }
    else parts.push(seg);
  }
  return parts.join('/');
}

export function md(src, opts) {
  const root = h('div', { class: 'md' });
  const lines = String(src || '').replace(/\r/g, '').split('\n');
  let i = 0;
  if (lines[0] === '---') {                 // skip YAML frontmatter
    const end = lines.indexOf('---', 1);
    if (end > 0) i = end + 1;
  }
  let para = [];
  const flush = () => {
    if (para.length) root.append(inline(para.join(' '), h('p'), opts));
    para = [];
  };
  while (i < lines.length) {
    const line = lines[i];
    const fence = /^\s*(`{3,}|~{3,})(.*)$/.exec(line);
    if (fence) {
      flush();
      const body = [];
      i++;
      while (i < lines.length && !new RegExp('^\\s*' + fence[1][0] + '{' + fence[1].length + ',}\\s*$').test(lines[i]))
        body.push(lines[i++]);
      i++;
      root.append(h('pre', {}, h('code', { text: body.join('\n') })));
      continue;
    }
    if (/^\s*<!--/.test(line)) {            // html comments: drop
      while (i < lines.length && !lines[i].includes('-->')) i++;
      i++;
      continue;
    }
    const head = /^(#{1,6})\s+(.*)$/.exec(line);
    if (head) {
      flush();
      root.append(inline(head[2], h('h' + Math.min(head[1].length + 1, 6)), opts));
      i++;
      continue;
    }
    if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) { flush(); root.append(h('hr')); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || '')) {
      flush();
      const cells = l => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      const table = h('table', {}, h('thead', {}, h('tr', {}, cells(line).map(c => inline(c, h('th'), opts)))));
      const tbody = h('tbody');
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i]))
        tbody.append(h('tr', {}, cells(lines[i++]).map(c => inline(c, h('td'), opts))));
      table.append(tbody);
      root.append(h('div', { class: 'md-table' }, table));
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      flush();
      const body = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i]))
        body.push(lines[i++].replace(/^\s*>\s?/, ''));
      root.append(h('blockquote', {}, ...md(body.join('\n'), opts).childNodes));
      continue;
    }
    const item = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line);
    if (item) {
      flush();
      const ordered = /\d/.test(item[2]);
      const list = h(ordered ? 'ol' : 'ul');
      const base = item[1].length;
      while (i < lines.length) {
        const m = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i]);
        if (!m || m[1].length < base) break;
        if (m[1].length > base) {           // nested: indent as a sub-line
          const li = list.lastElementChild;
          const sub = [];
          while (i < lines.length && /^\s+\S/.test(lines[i]) &&
                 lines[i].search(/\S/) > base) sub.push(lines[i++].slice(base + 2));
          if (li) li.append(...md(sub.join('\n'), opts).childNodes);
          continue;
        }
        const li = h('li');
        const box = /^\[( |x|X)\]\s+(.*)$/.exec(m[3]);
        if (box) {
          li.classList.add('task-item');
          li.append(h('span', { class: 'box' + (box[1] === ' ' ? '' : ' on'),
                                text: box[1] === ' ' ? '☐' : '☑' }));
          inline(box[2], li, opts);
        } else inline(m[3], li, opts);
        list.append(li);
        i++;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) &&
               !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
          li.append(' ');
          inline(lines[i++].trim(), li, opts);
        }
      }
      root.append(list);
      continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    para.push(line.trim());
    i++;
  }
  flush();
  return root;
}

// ----------------------------------------------------------------- misc

export function toast(text, kind = 'info', ms = 4200) {
  const box = $('#toasts');
  const el = h('div', { class: 'toast ' + kind, text });
  box.append(el);
  setTimeout(() => { el.classList.add('gone'); setTimeout(() => el.remove(), 320); }, ms);
}

// A simple modal. `body` is a node; returns a close function.
export function modal(title, body, { wide = false, actions = [] } = {}) {
  const overlay = $('#modal');
  const close = () => { overlay.classList.remove('open'); clear($('#modal-body')); };
  $('#modal-title').textContent = title;
  $('#modal-box').classList.toggle('wide', wide);
  clear($('#modal-body'), body);
  clear($('#modal-actions'), actions.map(a =>
    h('button', { class: 'btn ' + (a.kind || ''), text: a.label,
                  onclick: () => a.run(close) })));
  $('#modal-actions').hidden = !actions.length;
  overlay.classList.add('open');
  overlay.onclick = e => { if (e.target === overlay) close(); };
  $('#modal-close').onclick = close;
  return close;
}

export function confirmBox(title, text, okLabel, kind = 'primary') {
  return new Promise(resolve => {
    modal(title, h('div', { class: 'confirm' }, ...[].concat(text).map(t =>
      typeof t === 'string' ? h('p', { text: t }) : t)), {
      actions: [
        { label: 'Cancel', run: close => { close(); resolve(false); } },
        { label: okLabel, kind, run: close => { close(); resolve(true); } },
      ],
    });
  });
}

export const badge = (text, cls = '') => h('span', { class: 'badge ' + cls, text });

export function ago(stamp) {
  if (!stamp) return '';
  const t = new Date(stamp.replace(' ', 'T')).getTime();
  if (isNaN(t)) return stamp;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  return Math.floor(s / 86400) + ' d ago';
}

export const title = slug => String(slug || '').replace(/-/g, ' ');

// One conversation with A.R.I.A., shared by every surface that can talk to
// her: the Chat view, the Orb, and the dock at the bottom of the other
// views. This module owns the chats (localStorage), the request pipeline,
// the actions her replies can trigger, and voice in/out. Views just
// listen on the bus and draw.
//
// Bus events:  talk:reset            the visible thread changed wholesale
//              talk:add (msg)        a message was appended
//              talk:update (msg)     a message's content/extras changed
//              talk:state (state)    idle | listening | thinking | speaking
//              talk:chats            the list of chats changed
//              talk:interim (text)   speech being recognised

import { api, stream, eventSource, bus, store } from './core.js';

export const talk = {
  chats: store.get('aria_chats', []),
  current: null,
  busy: false,
  state: 'idle',
  voiceOn: store.get('aria_voice', true),
  listening: false,
  micLevel: 0,
};

const MAX_CHATS = 30;

// ------------------------------------------------------------- chats

function save() { store.set('aria_chats', talk.chats.slice(0, MAX_CHATS)); }

export function newChat() {
  if (talk.current && !talk.current.messages.length) {
    bus.emit('talk:reset');
    return talk.current;                       // don't stack empty chats
  }
  talk.current = { id: Date.now().toString(36), title: 'New chat',
                   messages: [], skill: null };
  talk.chats.unshift(talk.current);
  save();
  bus.emit('talk:chats');
  bus.emit('talk:reset');
  return talk.current;
}

export function openChat(id) {
  const c = talk.chats.find(c => c.id === id);
  if (!c) return;
  talk.current = c;
  bus.emit('talk:chats');
  bus.emit('talk:reset');
}

export function deleteChat(id) {
  talk.chats = talk.chats.filter(c => c.id !== id);
  save();
  if (talk.current && talk.current.id === id) {
    talk.current = talk.chats[0] || null;
    if (!talk.current) newChat();
    else bus.emit('talk:reset');
  }
  bus.emit('talk:chats');
}

function ensureChat() {
  if (!talk.current) talk.current = talk.chats[0] || null;
  if (!talk.current) newChat();
  return talk.current;
}

function add(msg) {
  ensureChat().messages.push(msg);
  bus.emit('talk:add', msg);
  return msg;
}

function setState(state) {
  talk.state = state;
  bus.emit('talk:state', state);
}

// ------------------------------------------------------------- submit

export async function submit(text) {
  text = (text || '').trim();
  if (!text || talk.busy) return;
  const chat = ensureChat();
  talk.busy = true;
  bus.emit('talk:busy', true);
  if (!chat.messages.length) {
    chat.title = text.slice(0, 42) + (text.length > 42 ? '…' : '');
    bus.emit('talk:chats');
  }
  add({ role: 'user', content: text });
  setState('thinking');
  const reply = add({ role: 'assistant', content: '', pending: true });
  let action = null;
  try {
    await stream('/api/chat', {
      messages: chat.messages
        .filter(m => (m.role === 'user' || m.role === 'assistant') && m.content)
        .slice(-16).map(m => ({ role: m.role, content: m.content })),
      skill: chat.skill || undefined,
    }, ev => {
      if (ev.event === 'token') {
        reply.content += ev.text;
        reply.status = '';
      } else if (ev.event === 'status') {
        reply.status = ev.text;
      } else if (ev.event === 'sources') {
        reply.sources = ev.sources || [];
        reply.question = ev.question || '';
      } else if (ev.event === 'action') {
        reply.content = ev.say || reply.content;
        action = ev;
      } else if (ev.event === 'error') {
        reply.content = '⚠ ' + ev.log;
        reply.error = true;
      } else return;
      bus.emit('talk:update', reply);
    });
  } catch (e) {
    reply.content = '⚠ ' + e.message;
    reply.error = true;
  }
  reply.pending = false;
  reply.status = '';
  if (action && action.action === 'needs-approval') reply.approve = action.slug;
  bus.emit('talk:update', reply);
  save();
  talk.busy = false;
  bus.emit('talk:busy', false);
  setState('idle');
  speak(reply.content);
  if (action) runAction(action);
}

function sys(text) { return add({ role: 'sys', content: text }); }

function runAction(ev) {
  const chat = talk.current;
  switch (ev.action) {
    case 'use-skill':
      if (ev.name) { chat.skill = ev.name; save(); bus.emit('talk:skill'); }
      break;
    case 'drop-skill':
      chat.skill = null; save(); bus.emit('talk:skill');
      break;
    case 'agent':
      runAgent(ev.name, ev.task);
      break;
    case 'triage': case 'develop': case 'auto':
      runStage(ev.action, ev.file);
      break;
    case 'navigate':
      bus.emit('nav', { view: ev.view, tab: ev.tab, slug: ev.slug });
      break;
    case 'capture':
      bus.emit('pipeline:changed');
      break;
    case 'approve': case 'autopilot-start': case 'autopilot-stop':
    case 'steer': case 'github-push':
      bus.emit('projects:changed', ev.slug);
      break;
  }
}

export function runStage(stage, file) {
  const qs = new URLSearchParams({ stage });
  if (file) qs.set('file', file);
  setState('thinking');
  const line = sys('→ running ' + stage + '…');
  eventSource('/api/run?' + qs, ev => {
    if (ev.event === 'moved' || ev.event === 'file_written') sys('→ ' + ev.to);
    bus.emit('pipeline:event', ev);
  }, ok => {
    line.content = ok ? '✓ ' + stage + ' finished — see the pipeline board'
                      : '⚠ ' + stage + ' did not finish (is another run in progress?)';
    bus.emit('talk:update', line);
    save();
    setState('idle');
    bus.emit('pipeline:changed');
    bus.emit('projects:changed');
    if (ok) speak(stage + ' finished.');
  });
}

export async function runAgent(name, task) {
  setState('thinking');
  const msg = add({ role: 'agent', name, content: '', pending: true });
  try {
    await stream('/api/agent', { name, task }, ev => {
      if (ev.event === 'token') msg.content += ev.text;
      else if (ev.event === 'sources') msg.sources = ev.sources;
      else if (ev.event === 'error') msg.content += '\n⚠ ' + ev.log;
      else return;
      bus.emit('talk:update', msg);
    });
  } catch (e) {
    msg.content += '\n⚠ ' + e.message;
  }
  msg.pending = false;
  bus.emit('talk:update', msg);
  save();
  setState('idle');
  speak('The ' + name + ' is done.');
}

export async function approveFromChat(slug, start) {
  try {
    await api.post('/api/autopilot', { slug, action: 'approve' });
    if (start) await api.post('/api/autopilot', { slug, action: 'start' });
    sys('✓ ' + slug + ' approved' + (start ? ' — autopilot engaged' : ''));
    bus.emit('projects:changed', slug);
    if (start) bus.emit('nav', { view: 'projects', slug, tab: 'live' });
  } catch (e) {
    sys('⚠ ' + e.message);
  }
  save();
}

// ------------------------------------------------------------- voice out

let chosenVoice = null;
function pickVoice() {
  const vs = speechSynthesis.getVoices();
  chosenVoice = vs.find(v => /en/i.test(v.lang) && /female|aria|zira|samantha|karen/i.test(v.name))
             || vs.find(v => /^en/i.test(v.lang)) || vs[0] || null;
}
if ('speechSynthesis' in window) {
  pickVoice();
  speechSynthesis.onvoiceschanged = pickVoice;
}

export function speak(text) {
  if (!talk.voiceOn || !('speechSynthesis' in window)) return;
  // read the answer, not its punctuation: no citations, links or symbols
  const clean = String(text || '').replace(/\[\d+\]/g, '')
    .replace(/https?:\/\/\S+/g, 'a link').replace(/[`*_#>]/g, '')
    .replace(/^[⚠✓→]\s*/, '').trim().slice(0, 900);
  if (!clean) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(clean);
  if (chosenVoice) u.voice = chosenVoice;
  u.rate = 1.04; u.pitch = 1.05;
  u.onstart = () => setState('speaking');
  u.onend = u.onerror = () => { if (talk.state === 'speaking') setState('idle'); };
  speechSynthesis.speak(u);
}

export function toggleVoice() {
  talk.voiceOn = !talk.voiceOn;
  store.set('aria_voice', talk.voiceOn);
  if (!talk.voiceOn && 'speechSynthesis' in window) speechSynthesis.cancel();
  bus.emit('talk:voice', talk.voiceOn);
}

// ------------------------------------------------------------- voice in
// Chromium has the Web Speech API; elsewhere (Firefox) the audio is
// recorded and transcribed locally by the server's faster-whisper.

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
export const voiceInput = SR ? 'browser'
  : (navigator.mediaDevices && window.MediaRecorder ? 'whisper' : 'none');
let rec = null, mediaRec = null, meterStop = null;

if (SR) {
  rec = new SR();
  rec.lang = navigator.language || 'en-GB';
  rec.interimResults = true;
  rec.onresult = e => {
    let final = '', part = '';
    for (const r of e.results) (r.isFinal ? final += r[0].transcript : part += r[0].transcript);
    if (part) bus.emit('talk:interim', part);
    if (final) { bus.emit('talk:interim', ''); stopListening(); submit(final.trim()); }
  };
  rec.onend = () => { if (talk.listening) stopListening(); };
  rec.onerror = ev => {
    stopListening();
    sys('mic: ' + ev.error + (ev.error === 'not-allowed'
      ? ' — allow microphone access and try again' : ''));
  };
}

async function meter() {             // lets the orb breathe with your voice
  try {
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    const ctx = new AudioContext();
    const an = ctx.createAnalyser();
    an.fftSize = 256;
    ctx.createMediaStreamSource(mic).connect(an);
    const buf = new Uint8Array(an.frequencyBinCount);
    let on = true;
    (function loop() {
      if (!on) return;
      an.getByteFrequencyData(buf);
      talk.micLevel = buf.reduce((a, b) => a + b, 0) / buf.length / 255;
      requestAnimationFrame(loop);
    })();
    meterStop = () => { on = false; talk.micLevel = 0;
                        mic.getTracks().forEach(t => t.stop()); ctx.close(); };
  } catch { /* no meter — the orb still works */ }
}

async function startRecorder() {
  const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
  const mime = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : 'audio/ogg';
  const chunks = [];
  mediaRec = new MediaRecorder(mic, { mimeType: mime });
  mediaRec.ondataavailable = e => chunks.push(e.data);
  mediaRec.onstop = async () => {
    mic.getTracks().forEach(t => t.stop());
    setState('thinking');
    bus.emit('talk:interim', 'transcribing locally…');
    try {
      const r = await fetch('/api/stt', { method: 'POST',
        headers: { 'Content-Type': mime }, body: new Blob(chunks, { type: mime }) });
      const j = await r.json();
      bus.emit('talk:interim', '');
      if (r.ok && j.text) submit(j.text);
      else { sys('voice: ' + (j.error || 'no speech detected')); setState('idle'); }
    } catch {
      bus.emit('talk:interim', '');
      sys('voice: transcription failed');
      setState('idle');
    }
  };
  mediaRec.start();
}

export function startListening() {
  if (talk.listening || voiceInput === 'none') return;
  talk.listening = true;
  setState('listening');
  bus.emit('talk:listening', true);
  if (rec) { meter(); try { rec.start(); } catch { /* already started */ } }
  else startRecorder().catch(e => { stopListening(); sys('mic: ' + e.message); });
}

export function stopListening() {
  if (!talk.listening) return;
  talk.listening = false;
  bus.emit('talk:listening', false);
  if (talk.state === 'listening') setState('idle');
  meterStop?.(); meterStop = null;
  if (rec) { try { rec.stop(); } catch { /* not running */ } }
  if (mediaRec && mediaRec.state === 'recording') mediaRec.stop();
}

export const toggleListening = () =>
  talk.listening ? stopListening() : startListening();

// boot: resume the most recent chat
talk.current = talk.chats[0] || null;

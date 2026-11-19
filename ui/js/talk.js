// One conversation with A.R.I.A., shared by every surface that can talk to
// her: the Chat view (with the orb behind it) and the dock at the bottom
// of the other views. This module owns the chats (localStorage), the
// request pipeline, the actions her replies can trigger, and voice in/out.
// Views just listen on the bus and draw.
//
// Bus events:  talk:reset            the visible thread changed wholesale
//              talk:add (msg)        a message was appended
//              talk:update (msg)     a message's content/extras changed
//              talk:state (state)    idle | listening | thinking | speaking
//              talk:chats            the list of chats changed
//              talk:interim (text)   speech being recognised
//              talk:voice            what she speaks/listens with changed

import { api, stream, eventSource, bus, store } from './core.js';

export const talk = {
  chats: store.get('aria_chats', []),
  current: null,
  busy: false,
  state: 'idle',
  voiceOn: store.get('aria_voice', true),
  listening: false,
  micLevel: 0,
  voice: null,          // /api/voice: what this machine can speak/listen with
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
  stopSpeaking();                    // you are talking: she stops to listen
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
  pending = new AbortController();
  try {
    await stream('/api/chat', {
      messages: chat.messages
        .filter(m => (m.role === 'user' || m.role === 'assistant') && m.content && !m.error)
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
    }, pending.signal);
  } catch (e) {
    if (e.name === 'AbortError') {              // you stopped waiting
      reply.content = (reply.content ? reply.content + ' ' : '') + '(stopped)';
      reply.stopped = true;
    } else {
      reply.content = '⚠ ' + e.message;
      reply.error = true;
    }
  }
  pending = null;
  if (!reply.content && !reply.error) {
    // the stream ended without a word: say so instead of an empty bubble
    reply.content = '⚠ I did not get an answer from the model — it may be busy with a big task. Try again in a moment.';
    reply.error = true;
  }
  reply.pending = false;
  reply.status = '';
  if (action && action.action === 'needs-approval') reply.approve = action.slug;
  if (action && action.slug && action.where) {
    // a reply about a project: where it lives, and its files as links
    reply.project = { slug: action.slug, where: action.where,
                      files: (action.files || []).slice(0, 12) };
  }
  bus.emit('talk:update', reply);
  save();
  talk.busy = false;
  bus.emit('talk:busy', false);
  setState('idle');
  if (!reply.error && !reply.stopped) speak(reply.content);
  if (action) runAction(action);
}

// Stop waiting for the reply in hand (the ■ button while she is thinking).
let pending = null;
export function stopReply() { pending?.abort(); }

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
    case 'steer': case 'github-push': case 'task-decision':
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

// ------------------------------------------------------------- voice
// A browser can speak and listen only if it has a speech service behind it
// (Chrome borrows Google's). A.R.I.A.'s own window has none — there she was
// mute and deaf. So the machine can do both itself (/api/tts and /api/stt,
// see bin/aria_voice.py), and the page uses whichever there is:
//
//   local    this machine's own voice and recogniser — private, and the
//            only kind that works in her own window
//   browser  the browser's speech, where it exists
//
// /api/voice says what the machine has; Settings → Voice sets a preference.

const DESKTOP = /ARIA-Desktop/.test(navigator.userAgent);
const BrowserSR = DESKTOP ? null
  : (window.SpeechRecognition || window.webkitSpeechRecognition);
const canRecord = !!(navigator.mediaDevices && window.MediaRecorder);

export async function loadVoice() {
  try { talk.voice = await api.get('/api/voice'); } catch { /* server away */ }
  bus.emit('talk:voice', talk.voiceOn);
}

function browserVoices() {
  return !DESKTOP && 'speechSynthesis' in window && speechSynthesis.getVoices().length > 0;
}

// How she would speak right now: 'local' | 'browser' | 'none'.
export function speechOut() {
  const v = talk.voice, pref = v ? v.prefs.engine : 'auto';
  const local = !!(v && v.tts.engine);
  if (pref === 'browser') return browserVoices() ? 'browser' : 'none';
  if (pref === 'local') return local ? 'local' : 'none';
  if (local && v.tts.natural) return 'local';      // a natural voice wins
  if (browserVoices()) return 'browser';
  return local ? 'local' : 'none';
}

// How she would listen: 'local' | 'browser' | 'missing' (a microphone, but
// no recogniser) | 'none'.
export function speechIn() {
  const v = talk.voice, pref = v ? v.prefs.input : 'auto';
  const local = !!(v && v.stt.engine) && canRecord;
  if (pref === 'browser') return BrowserSR ? 'browser' : 'none';
  if (pref === 'local') return local ? 'local' : canRecord ? 'missing' : 'none';
  if (local) return 'local';
  if (BrowserSR) return 'browser';
  return canRecord ? 'missing' : 'none';
}

// ------------------------------------------------------------- voice out

let audioCtx = null, outMeter = null, playing = null, speakGen = 0, noVoiceSaid = false;
const levelBuf = new Uint8Array(128);

function audio() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    outMeter = audioCtx.createAnalyser();
    outMeter.fftSize = 256;
    outMeter.connect(audioCtx.destination);
  }
  if (audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
  return audioCtx;
}
// a page may only start sound once the person has touched it
for (const ev of ['pointerdown', 'keydown'])
  addEventListener(ev, () => {
    if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
  }, { passive: true });

// How loud she is at this instant, 0…1 — the orb moves with her voice.
export function outLevel() {
  if (!outMeter || talk.state !== 'speaking') return 0;
  outMeter.getByteFrequencyData(levelBuf);
  let sum = 0;
  for (const b of levelBuf) sum += b;
  return sum / levelBuf.length / 255;
}

// Read the answer, not its punctuation: no code, citations, links, symbols.
function sayable(text) {
  return String(text || '')
    .replace(/```[\s\S]*?```/g, ' (there is code here) ')
    .replace(/A\.R\.I\.A\.?/g, 'Aria')            // her name, not four letters
    .replace(/\[(\d+)\]/g, '').replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/https?:\/\/\S+/g, 'a link').replace(/[`*_#>|]/g, '')
    .replace(/^[⚠✓→]\s*/, '').replace(/\s+/g, ' ').trim().slice(0, 1200);
}

// Sentence-sized pieces: the first is heard while the rest are being made.
function pieces(text) {
  const out = [];
  let cur = '';
  for (const s of text.match(/[^.!?…:;]+[.!?…:;]*\s*/g) || [text]) {
    if (cur && (cur + s).length > 240) { out.push(cur.trim()); cur = ''; }
    cur += s;
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}

export function stopSpeaking() {
  speakGen++;
  if (playing) { try { playing.stop(); } catch { /* already over */ } playing = null; }
  if ('speechSynthesis' in window) speechSynthesis.cancel();
  if (talk.state === 'speaking') setState('idle');
}

export function speak(text) {
  if (!talk.voiceOn) return;
  const clean = sayable(text);
  if (!clean) return;
  stopSpeaking();
  const how = speechOut();
  if (how === 'local') speakLocal(clean);
  else if (how === 'browser') speakBrowser(clean);
  else if (!noVoiceSaid && talk.voice) {
    noVoiceSaid = true;               // once, not after every reply
    sys('I have no voice on this machine yet — ' + (talk.voice.tts.why || 'see Settings → Voice')
        + '. (🔊 below turns spoken replies off.)');
  }
}

async function speakLocal(text) {
  const gen = speakGen;
  const ctx = audio();
  const make = async part => {
    const r = await fetch('/api/tts', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: part }) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || 'she has no voice here');
    return ctx.decodeAudioData(await r.arrayBuffer());
  };
  const parts = pieces(text);
  let next = make(parts[0]);
  for (let i = 0; i < parts.length; i++) {
    let buf;
    try { buf = await next; } catch (e) {
      if (gen === speakGen) { sys('voice: ' + e.message); if (talk.state === 'speaking') setState('idle'); }
      return;
    }
    if (gen !== speakGen) return;                 // interrupted meanwhile
    next = i + 1 < parts.length ? make(parts[i + 1]) : null;
    next?.catch(() => {});                        // reported when awaited
    if (talk.state === 'idle') setState('speaking');
    await new Promise(done => {
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(outMeter);
      src.onended = done;
      playing = src;
      src.start();
    });
    if (gen !== speakGen) return;
  }
  playing = null;
  if (talk.state === 'speaking') setState('idle');
}

let chosenVoice = null;
function pickVoice() {
  const vs = speechSynthesis.getVoices();
  chosenVoice = vs.find(v => /en/i.test(v.lang) && /female|aria|zira|samantha|karen/i.test(v.name))
             || vs.find(v => /^en/i.test(v.lang)) || vs[0] || null;
  bus.emit('talk:voice', talk.voiceOn);
}
if ('speechSynthesis' in window) {
  pickVoice();
  speechSynthesis.onvoiceschanged = pickVoice;
}

function speakBrowser(text) {
  const gen = speakGen;
  const u = new SpeechSynthesisUtterance(text.slice(0, 900));
  if (chosenVoice) u.voice = chosenVoice;
  u.rate = 1.04; u.pitch = 1.05;
  u.onstart = () => { if (gen === speakGen && talk.state === 'idle') setState('speaking'); };
  u.onend = u.onerror = () => { if (gen === speakGen && talk.state === 'speaking') setState('idle'); };
  speechSynthesis.speak(u);
}

// Speak now, whatever the 🔊 switch says (Settings: "hear her").
export function sayNow(text) {
  const was = talk.voiceOn;
  talk.voiceOn = true;
  try { speak(text); } finally { talk.voiceOn = was; }
}

export function toggleVoice() {
  talk.voiceOn = !talk.voiceOn;
  store.set('aria_voice', talk.voiceOn);
  if (!talk.voiceOn) stopSpeaking();
  bus.emit('talk:voice', talk.voiceOn);
}

// ------------------------------------------------------------- voice in

let rec = null, mediaRec = null, meterStop = null, capTimer = null;

function browserRecogniser() {
  if (rec || !BrowserSR) return rec;
  rec = new BrowserSR();
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
  return rec;
}

// Watch a microphone's level; `seen(level)` is called every frame.
function watch(mic, seen) {
  const ctx = audio();
  const an = ctx.createAnalyser();
  an.fftSize = 256;
  const src = ctx.createMediaStreamSource(mic);
  src.connect(an);
  const buf = new Uint8Array(an.frequencyBinCount);
  let on = true;
  (function loop() {
    if (!on) return;
    an.getByteFrequencyData(buf);
    talk.micLevel = buf.reduce((a, b) => a + b, 0) / buf.length / 255;
    seen?.(talk.micLevel);
    requestAnimationFrame(loop);
  })();
  return () => { on = false; talk.micLevel = 0; src.disconnect(); };
}

async function meter() {             // lets the orb breathe with your voice
  try {
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    const stop = watch(mic);
    meterStop = () => { stop(); mic.getTracks().forEach(t => t.stop()); };
  } catch { /* no meter — recognition still works */ }
}

// What the microphone recorded, as 16 kHz mono WAV: the recogniser then
// needs no audio decoder of its own. If this browser cannot decode its own
// recording, the recording is sent as it is.
async function toWav(blob) {
  try {
    const decoded = await audio().decodeAudioData(await blob.arrayBuffer());
    const rate = 16000, frames = Math.max(1, Math.ceil(decoded.duration * rate));
    const off = new OfflineAudioContext(1, frames, rate);
    const src = off.createBufferSource();
    src.buffer = decoded;
    src.connect(off.destination);
    src.start();
    const mono = (await off.startRendering()).getChannelData(0);
    const view = new DataView(new ArrayBuffer(44 + mono.length * 2));
    const tag = (at, s) => { for (let i = 0; i < s.length; i++) view.setUint8(at + i, s.charCodeAt(i)); };
    tag(0, 'RIFF'); view.setUint32(4, 36 + mono.length * 2, true); tag(8, 'WAVE');
    tag(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true);
    view.setUint16(34, 16, true); tag(36, 'data'); view.setUint32(40, mono.length * 2, true);
    for (let i = 0; i < mono.length; i++)
      view.setInt16(44 + i * 2, Math.max(-1, Math.min(1, mono[i])) * 0x7fff, true);
    return { body: view.buffer, type: 'audio/wav' };
  } catch {
    return { body: blob, type: (blob.type || 'audio/webm').split(';')[0] };
  }
}

async function startRecorder() {
  const mic = await navigator.mediaDevices.getUserMedia(
    { audio: { echoCancellation: true, noiseSuppression: true } });
  const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/ogg']
    .find(m => MediaRecorder.isTypeSupported(m));
  const chunks = [];
  const recorder = mediaRec = new MediaRecorder(mic, mime ? { mimeType: mime } : undefined);
  recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  // Hands-free: once she has heard you, a pause of a second and a half
  // ends your turn; a click on 🎤 ends it at once.
  let heard = false, quiet = 0;
  const stopWatch = watch(mic, level => {
    if (level > 0.07) { heard = true; quiet = 0; }
    else if (heard) {
      quiet ||= performance.now();
      if (performance.now() - quiet > 1500) stopListening();
    }
  });
  capTimer = setTimeout(stopListening, 60000);
  recorder.onstop = async () => {
    stopWatch();
    mic.getTracks().forEach(t => t.stop());
    if (!chunks.length) { setState('idle'); return; }
    setState('thinking');
    bus.emit('talk:interim', 'working out what you said…');
    try {
      const { body, type } = await toWav(new Blob(chunks, { type: recorder.mimeType || mime || 'audio/webm' }));
      const lang = (navigator.language || 'en').slice(0, 2);
      const r = await fetch('/api/stt?lang=' + lang, { method: 'POST',
        headers: { 'Content-Type': type }, body });
      const j = await r.json();
      bus.emit('talk:interim', '');
      if (r.ok && j.text) submit(j.text);
      else { sys('voice: ' + (j.error || 'I did not catch anything — try again a little closer to the microphone')); setState('idle'); }
    } catch {
      bus.emit('talk:interim', '');
      sys('voice: I could not work out what was said');
      setState('idle');
    }
  };
  recorder.start(250);
}

function micWhy(e) {
  if (e.name === 'NotAllowedError') return 'microphone access was refused — allow it for this page and try again';
  if (e.name === 'NotFoundError') return 'no microphone was found';
  return e.message || String(e);
}

export function startListening() {
  if (talk.listening) return;
  const how = speechIn();
  if (how === 'none') { sys('voice: this window cannot record from a microphone.'); return; }
  if (how === 'missing') {
    sys('voice: I cannot listen on this machine yet — '
        + ((talk.voice && talk.voice.stt.why) || 'no recogniser is installed')
        + '. Settings → Voice has a button for it.');
    return;
  }
  stopSpeaking();                    // she does not record herself
  talk.listening = true;
  setState('listening');
  bus.emit('talk:listening', true);
  if (how === 'browser') {
    meter();
    try { browserRecogniser().start(); } catch { /* already started */ }
  } else {
    startRecorder().catch(e => { stopListening(); sys('mic: ' + micWhy(e)); });
  }
}

export function stopListening() {
  if (!talk.listening) return;
  talk.listening = false;
  clearTimeout(capTimer);
  bus.emit('talk:listening', false);
  if (talk.state === 'listening') setState('idle');
  meterStop?.(); meterStop = null;
  if (rec) { try { rec.stop(); } catch { /* not running */ } }
  if (mediaRec && mediaRec.state === 'recording') mediaRec.stop();
}

export const toggleListening = () =>
  talk.listening ? stopListening() : startListening();

// boot: resume the most recent chat; find out what she can speak with
talk.current = talk.chats[0] || null;
loadVoice();
bus.on('settings:changed', loadVoice);

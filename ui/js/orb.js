// The orb: A.R.I.A. as a presence behind the conversation. A sphere of
// points that idles, listens, thinks and speaks. It lives in the
// background of the Chat view — bright and centred while the chat is
// empty or she is listening or speaking, a faint glow behind the thread
// the rest of the time, so it never competes with what is written.
//
//   idle       slow cyan drift
//   listening  green, breathes with your voice
//   thinking   amber, fast swirl
//   speaking   cyan, moves with hers

import { $ } from './core.js';
import { talk, outLevel } from './talk.js';

let visible = false, started = false;

function start(canvas) {
  const ctx = canvas.getContext('2d');
  const DPR = Math.min(devicePixelRatio || 1, 2);
  let W = 0, H = 0;
  const resize = () => {
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = W * DPR; canvas.height = H * DPR;
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  };
  new ResizeObserver(resize).observe(canvas);
  // a sphere of dots (fibonacci lattice) rotating in 3D, projected to 2D
  const N = 520, pts = [];
  for (let i = 0; i < N; i++) {
    const y = 1 - (i / (N - 1)) * 2, r = Math.sqrt(1 - y * y), th = i * 2.399963229728653;
    pts.push({ x: Math.cos(th) * r, y, z: Math.sin(th) * r,
               tw: Math.random() * Math.PI * 2, hub: Math.random() < .06 });
  }
  const links = [];
  for (let i = 0; i < N; i++)
    for (let j = i + 1; j < N; j++) {
      const dx = pts[i].x - pts[j].x, dy = pts[i].y - pts[j].y, dz = pts[i].z - pts[j].z;
      if (dx * dx + dy * dy + dz * dz < .026) links.push([i, j]);
    }
  const STATES = {
    idle:      { speed: .12, color: [69, 214, 255],  pulse: .015, jitter: 0 },
    listening: { speed: .25, color: [74, 222, 128],  pulse: .05,  jitter: 0 },
    thinking:  { speed: .95, color: [251, 191, 36],  pulse: .03,  jitter: .012 },
    speaking:  { speed: .35, color: [69, 214, 255],  pulse: .04,  jitter: .004 },
  };
  const cur = { speed: .12, pulse: .015, jitter: 0, color: [69, 214, 255], glow: 1, voice: 0 };
  let a1 = 0, a2 = 0, t = 0;
  (function frame() {
    requestAnimationFrame(frame);
    if (!visible || document.hidden || !W) return;
    const tgt = STATES[talk.state] || STATES.idle;
    cur.speed += (tgt.speed - cur.speed) * .05;
    cur.pulse += (tgt.pulse - cur.pulse) * .08;
    cur.jitter += (tgt.jitter - cur.jitter) * .08;
    for (let i = 0; i < 3; i++) cur.color[i] += (tgt.color[i] - cur.color[i]) * .06;
    // behind a conversation she is a glow; with the floor to herself — an
    // empty chat, listening, speaking — she is fully there
    const quiet = canvas.dataset.dim === '1' && talk.state !== 'listening' && talk.state !== 'speaking';
    cur.glow += ((quiet ? (talk.state === 'thinking' ? .34 : .2) : 1) - cur.glow) * .06;
    const level = talk.state === 'listening' ? talk.micLevel
                : talk.state === 'speaking' ? outLevel() : 0;
    cur.voice += (level - cur.voice) * .35;
    t += .016; a1 += .004 * (1 + cur.speed * 6); a2 += .0017 * (1 + cur.speed * 6);
    ctx.clearRect(0, 0, W, H);
    const cx = W / 2, cy = H / 2 - 30;
    const R = Math.min(W, H) * .25 * (1 + Math.sin(t * 2.1) * cur.pulse + cur.voice * .4);
    const [cr, cg, cb] = cur.color.map(Math.round);
    const s1 = Math.sin(a1), c1 = Math.cos(a1), s2 = Math.sin(a2), c2 = Math.cos(a2);
    const proj = new Array(N);
    for (let i = 0; i < N; i++) {
      const p = pts[i];
      let x = p.x * c1 + p.z * s1, z = -p.x * s1 + p.z * c1;
      let y2 = p.y * c2 - z * s2;
      z = p.y * s2 + z * c2;
      if (cur.jitter) {
        x += Math.sin(t * 13 + p.tw) * cur.jitter;
        y2 += Math.cos(t * 11 + p.tw) * cur.jitter;
      }
      const persp = 1.6 / (1.6 + z);
      proj[i] = { px: cx + x * R * persp, py: cy + y2 * R * persp, depth: (z + 1) / 2 };
    }
    ctx.globalAlpha = cur.glow;
    ctx.lineWidth = .6;
    for (const [i, j] of links) {
      const a = proj[i], b = proj[j], d = (a.depth + b.depth) / 2;
      ctx.strokeStyle = `rgba(${cr},${cg},${cb},${((1 - d) * .16 + .02).toFixed(3)})`;
      ctx.beginPath(); ctx.moveTo(a.px, a.py); ctx.lineTo(b.px, b.py); ctx.stroke();
    }
    for (let i = 0; i < N; i++) {
      const p = pts[i], q = proj[i];
      const alpha = (0.14 + (1 - q.depth) * .6) * (.75 + Math.sin(t * 3 + p.tw) * .25);
      const size = ((1 - q.depth) * 2.1 + .4) * (p.hub ? 1.9 : 1);
      if (p.hub) {
        ctx.fillStyle = `rgba(${cr},${cg},${cb},${(alpha * .35).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(q.px, q.py, size * 2.6, 0, 7); ctx.fill();
      }
      ctx.fillStyle = `rgba(${cr},${cg},${cb},${alpha.toFixed(3)})`;
      ctx.beginPath(); ctx.arc(q.px, q.py, size, 0, 7); ctx.fill();
    }
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * .8);
    g.addColorStop(0, `rgba(${cr},${cg},${cb},.10)`);
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g;
    ctx.fillRect(cx - R, cy - R, R * 2, R * 2);
    ctx.globalAlpha = 1;
  })();
}

// `dim`: there is a conversation on top of her.
export function setDim(dim) {
  const canvas = $('#orb');
  if (canvas) canvas.dataset.dim = dim ? '1' : '0';
}

export function show() {
  visible = true;
  if (!started) { started = true; start($('#orb')); }
}

export function hide() { visible = false; }

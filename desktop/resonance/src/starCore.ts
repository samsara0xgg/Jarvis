// Star-core (星核): a glass ball with stars inside and two glowing eyes. One WebGL program
// paints her inside and her glass; a skin only changes that program's numbers, so one skin
// can morph into another. Ported from the approved study (handoff lab/xinghe.html).
const PI = Math.PI, TAU = 2 * PI, D = PI / 180;
const B = 1.3; // half-size of the square the GL layers cover, in ball radii
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const clamp = (v: number, a: number, b: number) => v < a ? a : v > b ? b : v;
const smooth = (a: number, b: number, x: number) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
const rnd = (a: number, b: number) => a + Math.random() * (b - a);

export type Spring = { value: number; velocity: number };
export const spring = (value: number): Spring => ({ value, velocity: 0 });
// Damped harmonic spring; damping 1 never overshoots. Returns whether it still moves.
export function step(s: Spring, goal: number, hz: number, damping: number, dt: number) {
  const w = 2 * Math.PI * hz;
  for (let left = dt; left > 1e-6; left -= 1 / 240) {
    const h = Math.min(left, 1 / 240);
    s.velocity += (-w * w * (s.value - goal) - 2 * damping * w * s.velocity) * h;
    s.value += s.velocity * h;
  }
  if (Math.abs(s.value - goal) < 1e-3 && Math.abs(s.velocity) < 1e-2) { s.value = goal; s.velocity = 0; return false; }
  return true;
}

// ---------- light: every expression has its own eye colour, glow, nebula palette and rim ----------
type RGB = [number, number, number];
type Light = { eye: RGB; glow: RGB; n: RGB[]; rim: RGB; b: number };
const hex = (h: string) => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16) / 255) as RGB;
const rgba = (c: RGB, a = 1) => `rgba(${Math.round(c[0] * 255)},${Math.round(c[1] * 255)},${Math.round(c[2] * 255)},${a})`;
const light = (eye: string, glow: string, n: string[], rim: string, b: number): Light => ({ eye: hex(eye), glow: hex(glow), n: n.map(hex), rim: hex(rim), b });
const LIGHT = {
  base: light('#f5f7ff', '#9db4ff', ['#2b2a78', '#5b3aa8', '#1f5a8a'], '#8fa4ff', 1),
  dim: light('#aab2c8', '#5b6690', ['#161a38', '#24204a', '#132a44'], '#46507a', .5),
  cool: light('#f7fbff', '#a8d4ff', ['#1e3a86', '#3a5ac0', '#1a6a9a'], '#9cc8ff', 1.15),
  warm: light('#fff8ef', '#ffc98f', ['#6a3450', '#a8563a', '#3f2a78'], '#ffb98a', 1.1),
  gold: light('#fffaf0', '#ffd77a', ['#7a4a1a', '#c08a2a', '#5a2e6e'], '#ffd27a', 1.3),
  blush: light('#fff5f9', '#ff9ecb', ['#7a2a5e', '#c0487e', '#4a2a80'], '#ff9ec8', 1.05),
  angry: light('#fff1ee', '#ff6a55', ['#6a0f10', '#b42a18', '#3a0a1c'], '#ff5a48', 1.15),
  alert: light('#ffeceb', '#ff4d4d', ['#7a0c14', '#c21e22', '#40081a'], '#ff3b3b', 1.2),
  think: light('#f8f4ff', '#bb94ff', ['#3a1f86', '#7a3ac0', '#23307a'], '#b98cff', 1.1),
  listen: light('#f2fbff', '#8fe3ff', ['#0f4a6e', '#1f7aa0', '#2a3a8a'], '#8fe3ff', 1.05),
  speak: light('#f2fff9', '#6fe0b4', ['#0e5044', '#1f8a70', '#1f3a78'], '#6fe0b4', 1.1),
  work: light('#f2f6ff', '#6c9cff', ['#12307e', '#2a5ad0', '#1a2a6a'], '#6c9cff', 1.15),
  memory: light('#fbf6ff', '#e0b4ff', ['#4a2a6a', '#8a4a8a', '#2a3a7a'], '#e6b8ff', .95),
  deny: light('#e8dcdc', '#c86a6a', ['#3a1418', '#5a1e24', '#221a3a'], '#b85a5a', .7),
  off: light('#6b7080', '#2a2f40', ['#0a0c16', '#0e0f1c', '#0a1220'], '#2a3048', .08),
};
type LightKey = keyof typeof LIGHT;
const copyLight = (l: Light): Light => ({ eye: [...l.eye], glow: [...l.glow], n: l.n.map(c => [...c] as RGB), rim: [...l.rim], b: l.b });
// Eases the current light toward another; returns how far apart they were.
function mixLight(cur: Light, to: Light, a: number) {
  let gap = 0;
  const mix = (c: RGB, t: RGB) => { for (let i = 0; i < 3; i++) { gap = Math.max(gap, Math.abs(t[i] - c[i])); c[i] += (t[i] - c[i]) * a; } };
  mix(cur.eye, to.eye); mix(cur.glow, to.glow); mix(cur.rim, to.rim);
  for (let j = 0; j < 3; j++) mix(cur.n[j], to.n[j]);
  return gap;
}

// ---------- skins: the same glass, different insides ----------
type Glass = { neb: number; stars: number; soft: number; gal: number; aur: number; refr: number; frost: number; irid: number };
export const SKINS = {
  glass: { name: '深空玻璃', gl: { neb: .3, stars: 1, soft: 0, gal: 0, aur: 0, refr: 1, frost: 0, irid: 0 } },
  nebula: { name: '星云', gl: { neb: 1, stars: .75, soft: 0, gal: 0, aur: 0, refr: .7, frost: 0, irid: 0 } },
  galaxy: { name: '银河', gl: { neb: .18, stars: .65, soft: 0, gal: 1, aur: 0, refr: .8, frost: 0, irid: 0 } },
  frost: { name: '磨砂', gl: { neb: .4, stars: .9, soft: 1, gal: 0, aur: 0, refr: .25, frost: 1, irid: 0 } },
  aurora: { name: '极光', gl: { neb: .12, stars: .7, soft: 0, gal: 0, aur: 1, refr: .8, frost: 0, irid: 1 } },
} satisfies Record<string, { name: string; gl: Glass }>;
export type Skin = keyof typeof SKINS;
export const SKIN_KEYS = Object.keys(SKINS) as Skin[];
export const isSkin = (value: unknown): value is Skin => typeof value === 'string' && Object.hasOwn(SKINS, value);

// ---------- expressions ----------
// Eyes in ball radii. tilt 90 lays both flat, 0 stands them upright; cut trims the top along a lid line
// (cutT tips it, positive = inner end lower, the angry lid), cutB lifts the bottom like a smiling cheek.
type Shape = { sep: number; y: number; len: number; w: number; tilt: number; lean: number; bend: number; lid: number; cut: number; cutT: number; cutB: number; head: number;
  yR?: number; lenR?: number; wR?: number; tiltR?: number; bendR?: number; lidR?: number; cutR?: number; cutTR?: number; cutBR?: number; aL?: number; aR?: number };
type Effect = 'hop' | 'jolt' | 'squash' | 'shake' | 'ripple' | 'spin' | 'burst';
type Gaze = 'free' | 'still' | 'away' | 'up' | 'loop' | 'scan' | 'sweepY' | 'recall' | 'read';
type Frame = { at: number; eyes?: Partial<Shape>; light?: LightKey; lightK?: number; bright?: number; tremble?: number; spin?: number; gy?: number; fx?: Effect };
type Expr = {
  name: string; eyes: Partial<Shape>; light?: LightKey; lightK?: number; bright?: number; gaze?: Gaze; gx?: number; gy?: number;
  blink?: [number, number]; blinkSlow?: boolean; spin?: number; breathe?: [number, number]; bob?: number; bounce?: [number, number];
  sink?: number; tall?: number; tremble?: number; flicker?: number; blush?: number; voice?: boolean; lidWave?: number; spring?: [number, number];
  enter?: Effect[]; antics?: ('spin' | 'hop')[]; fx?: { zzz?: boolean; sparkle?: boolean; orbit?: boolean }; sway?: [number, number];
  seq?: { frames: Frame[]; end: number }; freeze?: boolean;
};
const BASE: Shape = { sep: .3, y: .08, len: .52, w: .21, tilt: 14, lean: 0, bend: 0, lid: 1, cut: 0, cutT: 0, cutB: 0, head: 0 };
const SLEEP = { sep: .3, y: .18, len: .3, w: .075, tilt: 90, bend: -.05 };
const SMILE = { sep: .31, y: .1, len: .46, w: .15, tilt: 90, bend: .17 };
// Listening, receiving and replying never show two parallel upright bars, which read as a pause button.
// They build on two shapes instead: a tilted head, and two odd-sized round eyes.
const TILT = { sep: .28, y: .06, len: .42, w: .22, tilt: 0, head: 12, lenR: .3 };
const ODD = { sep: .29, y: .04, len: .04, w: .27, wR: .33, tilt: 0, head: -10 };
const LINES = { sep: .42, y: .04, len: .42, w: .19, tilt: 90 };
export type ExprId = '00' | '02' | '10' | '13' | '14' | '21' | '30' | '31' | '31b' | '31c' | '31d' | '32' | '33' | '34' | '35' | '35b' | '36' | '37' | '38' | '39' | '39b' | '39c' | '40' | '41' | 'home' | 'rest' | 'doze' | 'glance' | 'peek';
export const EXPRESSIONS: Record<ExprId, Expr> = {
  // Where she is decides her face first: flat "— —" in the island, round dots when she glances out, low eyes when she peeks.
  home: { name: '', eyes: LINES, gaze: 'still' },
  // With her glass showing at home she stays awake there, blinking and looking around, and dozes on the same lines after a long quiet spell.
  rest: { name: '', eyes: { sep: .3, y: .06, len: .42, w: .21, tilt: 10 }, gaze: 'free', blink: [2600, 6000] },
  doze: { name: '', eyes: LINES, light: 'dim', gaze: 'still', breathe: [.02, 5], bob: .01, fx: { zzz: true } },
  glance: { name: '', eyes: { sep: .36, y: .02, len: .18, w: .18, tilt: 0 }, gaze: 'free' },
  peek: { name: '', eyes: { sep: .27, y: .3, len: .26, w: .2, tilt: 12 }, gaze: 'free', blink: [2400, 6600] },
  '00': { name: '睡眠', eyes: { ...SLEEP, head: -4 }, light: 'dim', gaze: 'still', gy: .2, spin: .05, breathe: [.022, 5.5], bob: .012, fx: { zzz: true } },
  '02': { name: '待机', eyes: {}, gaze: 'free', blink: [2400, 6600], antics: ['spin', 'hop'] },
  '10': { name: '开心', eyes: SMILE, light: 'warm', spin: .6, bounce: [.035, 900], fx: { sparkle: true }, enter: ['hop'], antics: ['spin'] },
  '13': { name: '惊讶', eyes: { sep: .3, y: .03, len: .02, w: .37, tilt: 0 }, light: 'cool', gaze: 'still', gy: -.05, blink: [5000, 9000], spring: [8, .38], enter: ['jolt'],
    seq: { frames: [{ at: 0, eyes: { w: .42, sep: .31 }, bright: 1.8 }, { at: 260, bright: 1.1 }], end: 500 } },
  '14': { name: '害羞', eyes: { sep: .27, y: .13, len: .28, w: .17, tilt: 8, head: -9 }, light: 'blush', lightK: 1.6, blush: 1, gaze: 'away', gx: -.6, gy: .35, sway: [2, 3], blink: [1200, 2800], spin: .3 },
  '21': { name: '生气', eyes: { sep: .27, y: .1, len: .38, w: .23, tilt: -10, cut: .4, cutT: 30 }, light: 'angry', lightK: 12, gaze: 'free', tremble: .006, blink: [4000, 8000], spin: 1.2, enter: ['squash'] },
  '30': { name: '思考中', eyes: { sep: .27, y: -.02, len: .36, w: .19, tilt: 0, lean: 16 }, light: 'think', gaze: 'up', blink: [3000, 6000], spin: 1.1, fx: { orbit: true } },
  // Receiving and replying come in several takes; each time she picks one at random.
  '31': { name: '接收任务·歪头点一下', eyes: TILT, light: 'listen', gaze: 'still', blink: [2400, 6000], seq: { frames: [
    { at: 0, eyes: { lid: .08 } }, { at: 120, eyes: { head: -8 }, bright: 1.4, gy: .25, fx: 'ripple' }, { at: 460, eyes: {} }], end: 800 } },
  '31b': { name: '接收任务·眨一只眼', eyes: TILT, light: 'listen', gaze: 'still', blink: [2400, 6000], seq: { frames: [
    { at: 0, eyes: { lidR: .08 } }, { at: 280, eyes: {}, bright: 1.4, fx: 'ripple' }], end: 650 } },
  '31c': { name: '接收任务·大小眼一亮', eyes: ODD, light: 'listen', gaze: 'still', blink: [2400, 6000], seq: { frames: [
    { at: 0, eyes: { w: .18, wR: .2 }, bright: .7 }, { at: 170, eyes: { w: .33, wR: .38 }, bright: 1.8, spin: 3, fx: 'ripple' }, { at: 540, bright: 1.1, spin: .5 }], end: 850 } },
  '31d': { name: '接收任务·大小眼转头', eyes: ODD, light: 'listen', gaze: 'still', blink: [2400, 6000], enter: ['jolt'], seq: { frames: [
    { at: 0, eyes: { head: 12 } }, { at: 260, eyes: {}, bright: 1.4, fx: 'ripple' }], end: 700 } },
  '32': { name: '处理中', eyes: { sep: .27, y: .1, len: .3, w: .2, tilt: 0, cut: .15, cutT: 8 }, light: 'work', gaze: 'loop', blink: [3000, 6000], spin: 1.7 },
  '33': { name: '任务完成', eyes: SMILE, light: 'warm', spin: .5, enter: ['hop', 'burst'], seq: { frames: [
    { at: 0, light: 'gold', bright: 1.4, spin: 3 }, { at: 1400, light: 'warm', bright: 1, spin: .5 },
  ], end: 1500 } },
  '34': { name: '出错', eyes: { sep: .3, y: .05, len: .04, w: .3, cut: .12, cutT: 18, tilt: 0 }, light: 'alert', gaze: 'still', tremble: .004, blink: [3000, 6000], spin: .3, flicker: .15,
    seq: { frames: [
      { at: 0, light: 'alert', lightK: 40, tremble: .03 }, { at: 140, light: 'base', lightK: 40, tremble: .03 },
      { at: 280, light: 'alert', lightK: 40, tremble: .02 }, { at: 420, light: 'base', lightK: 40 }, { at: 560, light: 'alert', lightK: 14 },
    ], end: 800 } },
  '35': { name: '等待输入·歪头', eyes: TILT, light: 'listen', gaze: 'still', gx: .15, gy: -.05, sway: [3, 3.2], blink: [2400, 6000], spin: .35, breathe: [.012, 2.8] },
  '35b': { name: '等待输入·大小眼', eyes: ODD, light: 'listen', gaze: 'still', gx: -.1, sway: [4, 3], blink: [2400, 6000], spin: .35, breathe: [.012, 2.8] },
  '36': { name: '联网加载', eyes: { sep: .29, y: .07, len: .42, w: .21, tilt: 0 }, light: 'work', gaze: 'still', lidWave: .9, spin: .7 },
  '37': { name: '复述回忆', eyes: { sep: .28, y: .05, len: .56, w: .22, tilt: 0 }, light: 'memory', gaze: 'recall', blink: [3000, 6000], spin: -.5 },
  '38': { name: '拒绝', eyes: { sep: .3, y: .12, len: .32, w: .25, tilt: 0, cut: .38, cutT: -4 }, light: 'deny', gaze: 'still', gx: -.4, gy: .3, blink: [2500, 5000], spin: .15, enter: ['shake'] },
  '39': { name: '输出回复·歪头念', eyes: TILT, light: 'speak', gaze: 'read', voice: true, blink: [2400, 5000], spin: .8 },
  '39b': { name: '输出回复·歪头轻晃', eyes: TILT, light: 'speak', gaze: 'still', gy: .05, voice: true, sway: [7, 1.5], blink: [2400, 5000], spin: .8 },
  '39c': { name: '输出回复·大小眼说', eyes: ODD, light: 'speak', gaze: 'still', gy: .05, voice: true, sway: [6, 1.8], blink: [2400, 5000], spin: .8 },
  '40': { name: '检索资料', eyes: { sep: .28, y: .07, len: .34, w: .2, tilt: 0 }, light: 'work', gaze: 'scan', blink: [4000, 8000], spin: 2.4 },
  '41': { name: '关机', eyes: { sep: .3, y: .12, len: .32, w: .17, tilt: 0, cut: .5 }, light: 'off', gaze: 'still', spin: 0, freeze: true, seq: { frames: [
    { at: 0, eyes: { len: .4, w: .2, cut: 0 }, light: 'base', bright: .9 },
    { at: 350, eyes: { len: .32, w: .17, cut: .3 }, light: 'base', bright: .6 },
    { at: 800, light: 'off', lightK: 3 },
  ], end: 1600 } },
};
// The twelve work states first, then five feelings: what the tray can play on demand.
export const PREVIEW: ExprId[] = ['30', '31', '31b', '31c', '31d', '32', '33', '34', '35', '35b', '36', '37', '38', '39', '39b', '39c', '40', '41', '10', '14', '13', '00', '21'];
// The takes she picks from at random each time.
export const TAKES = { listen: ['35', '35b'], receive: ['31', '31b', '31c', '31d'], reply: ['39', '39b', '39c'] } satisfies Record<string, ExprId[]>;
export const pick = (ids: ExprId[]) => ids[Math.floor(Math.random() * ids.length)];
const FACES = new Set<ExprId>(['home', 'rest', 'doze', 'glance', 'peek']);

// ---------- eyes ----------
const EYE_KEYS = ['x', 'y', 'len', 'w', 'rot', 'bend', 'lid', 'cut', 'cutT', 'cutB', 'a'] as const;
type Eye = Record<typeof EYE_KEYS[number], number>;
function eyeTargets(s: Shape): Eye[] {
  return [
    { x: -s.sep, y: s.y, len: s.len, w: s.w, rot: 90 + s.tilt + s.lean, bend: s.bend, lid: s.lid, cut: s.cut, cutT: s.cutT, cutB: s.cutB, a: s.aL ?? 1 },
    { x: s.sep, y: s.y + (s.yR ?? 0), len: s.lenR ?? s.len, w: s.wR ?? s.w, rot: 90 - (s.tiltR ?? s.tilt) + s.lean, bend: s.bendR ?? s.bend,
      lid: s.lidR ?? s.lid, cut: s.cutR ?? s.cut, cutT: s.cutTR ?? s.cutT, cutB: s.cutBR ?? s.cutB, a: s.aR ?? 1 },
  ];
}
// A point on the face (unit sphere) turned by yaw/pitch; z < 0 means it went round the back.
function project(x: number, y: number, yaw: number, pitch: number) {
  const z = Math.sqrt(Math.max(0, 1 - x * x - y * y));
  const x1 = x * Math.cos(yaw) + z * Math.sin(yaw), z1 = -x * Math.sin(yaw) + z * Math.cos(yaw);
  const y1 = y * Math.cos(pitch) + z1 * Math.sin(pitch), z2 = -y * Math.sin(pitch) + z1 * Math.cos(pitch);
  return [x1, y1, z2];
}
type EyePose = { pts: number[][]; c: number[]; w: number; lid: number; cut: number; cutT: number; cutB: number; a: number; side: number; head: number };
function eyeSet(L: Eye, R: Eye, gx: number, gy: number, turn: number, head: number): EyePose[] {
  const rf = .9, yaw = gx * .5 + turn, pitch = gy * .4, h = head * D, out: EyePose[] = [];
  for (const [side, e] of [[-1, L], [1, R]] as const) {
    const cx = e.x * Math.cos(h) - e.y * Math.sin(h), cy = e.x * Math.sin(h) + e.y * Math.cos(h);
    const a = (e.rot + head) * D, ux = Math.cos(a) * e.len / 2, uy = Math.sin(a) * e.len / 2;
    let nx = -Math.sin(a), ny = Math.cos(a);
    if (ny > 0) { nx = -nx; ny = -ny; }
    const b = e.bend * 2, n = Math.abs(e.bend) > .005 ? 9 : 2, pts: number[][] = [];
    for (let i = 0; i < n; i++) {
      const t = i / (n - 1), u = 1 - t;
      const x = cx + u * u * -ux + 2 * u * t * nx * b + t * t * ux, y = cy + u * u * -uy + 2 * u * t * ny * b + t * t * uy;
      const p = project(x / rf, y / rf, yaw, pitch);
      pts.push([p[0] * rf, p[1] * rf]);
    }
    const pc = project(cx / rf, cy / rf, yaw, pitch);
    out.push({ pts, c: [pc[0] * rf, pc[1] * rf], w: e.w * (.5 + .5 * Math.max(0, pc[2])), lid: e.lid, cut: e.cut, cutT: e.cutT, cutB: e.cutB,
      a: e.a * smooth(-.02, .22, pc[2]), side, head });
  }
  return out;
}
// One eye: a glowing capsule, optionally trimmed by a lid line from the top and a cheek curve from below.
function drawEye(c: CanvasRenderingContext2D, R: number, E: EyePose, col: string) {
  c.translate(E.c[0] * R, E.c[1] * R);
  const lid = Math.max(.06, E.lid), hw = E.w * R / 2;
  let x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9;
  const P = E.pts.map(([x, y]) => [(x - E.c[0]) * R, (y - E.c[1]) * R]);
  for (const [x, y] of P) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y * lid); y1 = Math.max(y1, y * lid); }
  const top = y0 - hw * lid, bot = y1 + hw * lid, H = bot - top, W = x1 - x0 + 2 * hw, big = W + H + 20;
  if (E.cut > .004) {
    const yc = top + E.cut * H, k = Math.tan(((E.side < 0 ? E.cutT : -E.cutT) + E.head) * D);
    c.beginPath(); c.moveTo(-big, yc - k * big); c.lineTo(big, yc + k * big); c.lineTo(big, big); c.lineTo(-big, big); c.closePath(); c.clip();
  }
  if (E.cutB > .004) {
    const yb = bot - E.cutB * H, half = W / 2 + 2, sag = H * .4, mx = (x0 + x1) / 2;
    c.beginPath(); c.moveTo(mx - half, yb + sag); c.quadraticCurveTo(mx, yb - sag, mx + half, yb + sag);
    c.lineTo(mx + half, -big); c.lineTo(mx - half, -big); c.closePath(); c.clip();
  }
  c.scale(1, lid);
  c.beginPath();
  P.forEach(([x, y], i) => i ? c.lineTo(x, y) : c.moveTo(x, y));
  c.lineCap = 'round'; c.lineJoin = 'round';
  c.strokeStyle = col; c.lineWidth = E.w * R; c.stroke();
  c.globalCompositeOperation = 'lighter'; c.strokeStyle = 'rgba(255,255,255,.45)'; c.lineWidth = E.w * R * .42; c.stroke();
}
const blinkCurve = (k: number) => k < 0 ? 1 : k < .075 ? 1 - .92 * (k / .075) ** 2 : k < .11 ? .08 : k < .26 ? .08 + .92 * (1 - (1 - (k - .11) / .15) ** 3) : 1;

// ---------- WebGL: layer 0 is her inside, layer 1 her glass surface ----------
const FS = `#version 300 es
precision highp float;
in vec2 v;
out vec4 o;
uniform int uLayer;
uniform float uPx, uT, uQ;
uniform mat3 uRot;
uniform vec3 uN1, uN2, uN3, uRim, uEyeC, uEyeL, uEyeR;
uniform float uNeb, uStars, uSoft, uGal, uAur, uRefr, uFrost, uIrid, uBright;

float h31(vec3 p) { p = fract(p * vec3(.1031, .1030, .0973)); p += dot(p, p.yzx + 33.33); return fract((p.x + p.y) * p.z); }
vec3 h33(vec3 p) { p = fract(p * vec3(.1031, .1030, .0973)); p += dot(p, p.yxz + 33.33); return fract((p.xxy + p.yxx) * p.zyx); }
float vn(vec3 x) {
  vec3 i = floor(x), f = fract(x);
  f = f * f * (3. - 2. * f);
  return mix(mix(mix(h31(i), h31(i + vec3(1., 0., 0.)), f.x), mix(h31(i + vec3(0., 1., 0.)), h31(i + vec3(1., 1., 0.)), f.x), f.y),
             mix(mix(h31(i + vec3(0., 0., 1.)), h31(i + vec3(1., 0., 1.)), f.x), mix(h31(i + vec3(0., 1., 1.)), h31(i + vec3(1., 1., 1.)), f.x), f.y), f.z);
}
float fbm(vec3 p) {
  float s = 0., a = .5, n = 0.;
  for (int i = 0; i < 5; i++) { if (float(i) >= uQ) break; s += a * vn(p); n += a; p = p * 2.03 + vec3(3.1, 1.7, 5.3); a *= .5; }
  return s / n;
}
// Stars pinned to a shell of radius r inside the ball; far = the side facing away from us.
vec3 shell(vec3 e, vec3 d, float r, float far, float dens) {
  float b = dot(e, d), c = dot(e, e) - r * r, disc = b * b - c;
  if (disc < 0.) return vec3(0.);
  float t = far > .5 ? -b + sqrt(disc) : -b - sqrt(disc);
  if (t < 0.) return vec3(0.);
  vec3 q = uRot * (e + d * t);
  vec3 g = q * dens, id = floor(g), f = fract(g);
  vec3 hh = h33(id);
  if (hh.x > .4) return vec3(0.);
  vec3 j = .25 + .5 * h33(id + 17.);
  float dd = length(f - j);
  // everything a star draws stays inside its own cell (the jitter keeps it .25 from the walls)
  float pix = uPx * dens * 1.4;
  float sz0 = mix(.035, .085, hh.y * hh.y) * (1. + uSoft * .8);
  float sz = min(max(sz0, pix), .14), k = sz0 / max(sz0, pix);
  k *= k;
  float core = smoothstep(sz, 0., dd) * k * (1. - uSoft * .6);
  float halo = exp(-dd * dd / (sz * sz * (2.5 + uSoft * 2.))) * (1. - smoothstep(.16, .245, dd)) * .2 * k * (1. + uSoft * 2.5);
  float tw = .6 + .4 * sin(uT * (1.3 + 3.1 * hh.z) + hh.y * 40.);
  vec3 col = mix(vec3(.74, .82, 1.), vec3(1., .87, .74), step(.8, hh.z));
  return col * (core + halo) * tw * (.9 + 4. * hh.y * hh.y);
}
float softbox(vec3 R, vec3 K, vec2 hs, float rough) {
  float f = dot(R, K);
  if (f <= 0.) return 0.;
  vec3 T1 = normalize(cross(K, vec3(0., 0., 1.))), T2 = cross(K, T1);
  vec2 uv = vec2(dot(R, T1), dot(R, T2)) / f;
  vec2 q = abs(uv) - hs + .06;
  float dd = length(max(q, 0.)) + min(max(q.x, q.y), 0.) - .06;
  return smoothstep(rough, -rough, dd) * mix(1.3, .08, smoothstep(-1., .9, uv.y / hs.y));
}
void main() {
  float r = length(v), aa = uPx * 1.2;
  if (uLayer == 0) {
    if (r > 1.) {
      float halo = exp(-(r - 1.) * 22.) * .07 * min(uBright, 1.2);
      vec3 hc = uRim * halo;
      o = vec4(hc, max(hc.r, max(hc.g, hc.b)));
      return;
    }
    float z = sqrt(max(0., 1. - r * r));
    vec3 n = vec3(v, z), I = vec3(0., 0., -1.);
    vec3 d = normalize(mix(I, refract(I, n, 1. / 1.5), uRefr));
    vec3 e = n;
    float tEx = -2. * dot(e, d);
    vec3 col = vec3(.006, .008, .02);
    float trans = 1.;
    int N = int(uQ * 2.5);
    float dt = tEx / float(N), jit = h31(vec3(gl_FragCoord.xy, 3.));
    vec3 eL = vec3(uEyeL.xy, sqrt(max(0., 1. - dot(uEyeL.xy, uEyeL.xy))) * .88);
    vec3 eR = vec3(uEyeR.xy, sqrt(max(0., 1. - dot(uEyeR.xy, uEyeR.xy))) * .88);
    for (int i = 0; i < 12; i++) {
      if (i >= N) break;
      vec3 p = e + d * ((float(i) + jit) * dt);
      vec3 q = uRot * p;
      float fall = 1. - dot(p, p) * .5, dn = 0.;
      vec3 em = vec3(0.);
      if (uNeb > 0.) {
        float den = smoothstep(.44, .76, fbm(q * 1.7 + vec3(0., uT * .03, 0.))) * uNeb * fall;
        vec3 c = mix(uN1, uN2, smoothstep(.3, .75, vn(q * 2.1 + 7.)));
        c = mix(c, uN3, smoothstep(.55, .9, vn(q * 1.3 + 13.)));
        em += c * den * (2.4 + 3. * den * den); dn += den;
      }
      if (uAur > 0.) {
        float hg = q.y + .22 * sin(q.x * 3.1 + uT * .5) + .12 * sin(q.z * 4.7 - uT * .37);
        float den = exp(-hg * hg * 50.) * (.3 + .7 * vn(vec3(q.x * 9., q.z * 9., uT * .25))) * uAur * fall;
        vec3 c = mix(vec3(.15, 1., .62), vec3(.6, .35, 1.), smoothstep(-.4, .5, q.x + q.y * .6));
        em += mix(c, uN2 * 1.6, .3) * den * 3.2; dn += den * .4;
      }
      if (uGal > 0.) em += vec3(1., .8, .58) * exp(-dot(q, q) * 26.) * 2.2 * uGal;
      float l = exp(-dot(p - eL, p - eL) * 11.) * uEyeL.z + exp(-dot(p - eR, p - eR) * 11.) * uEyeR.z;
      em += uEyeC * l * (.05 + dn * 1.4);
      col += trans * em * dt;
      trans *= exp(-dn * dt * 2.);
    }
    if (uGal > 0.) {
      float ct = cos(1.15), st = sin(1.15);
      mat3 G = mat3(1., 0., 0., 0., ct, st, 0., -st, ct);
      vec3 q0 = G * (uRot * e), qd = G * (uRot * d);
      float tp = -q0.y / qd.y;
      if (tp > 0. && tp < tEx) {
        vec3 gp = q0 + qd * tp;
        float rr = length(gp.xz), ang = atan(gp.z, gp.x);
        float arm = pow(.5 + .5 * cos(2. * ang - 7.5 * log(rr + .04)), 3.);
        float den = exp(-rr * 4.) * (.2 + 1.4 * arm) * smoothstep(.95, .3, rr);
        den *= .5 + smoothstep(.35, .7, fbm(gp * 7.));
        vec3 c = mix(vec3(1., .86, .62), mix(vec3(.55, .66, 1.), uN2 * 1.8, .35), smoothstep(.04, .32, rr));
        c += vec3(1., .45, .75) * smoothstep(.72, .9, vn(gp * 22.)) * arm * smoothstep(.9, .3, rr) * 1.6;
        col += trans * c * den * uGal * .45 / max(.3, abs(qd.y));
      }
    }
    vec3 far = shell(e, d, .9, 1., 7.) + shell(e, d, .72, 1., 9.) * .8;
    vec3 near = shell(e, d, .9, 0., 7.) + shell(e, d, .72, 0., 9.) * .9 + shell(e, d, .54, 0., 12.) * .75 + shell(e, d, .36, 0., 16.) * .6;
    col += (far * trans * .6 + near * mix(1., trans, .4)) * uStars;
    col *= uBright;
    col *= mix(.45, 1., smoothstep(0., .6, z));
    col += uRim * pow(1. - z, 6.) * .5 * min(uBright, 1.2);
    col += uRim * exp(-(v.x * v.x * 6. + (v.y - .83) * (v.y - .83) * 45.)) * .55 * uRefr * min(uBright, 1.2);
    col = 1. - exp(-col * 1.1);
    col += (h31(vec3(gl_FragCoord.xy, 9.)) - .5) / 255.;
    float cov = 1. - smoothstep(1. - aa, 1., r);
    o = vec4(clamp(col, 0., 1.) * cov, cov);
  } else {
    if (r > 1.) { o = vec4(0.); return; }
    float z = sqrt(max(0., 1. - r * r));
    vec3 n = vec3(v, z);
    vec3 R = vec3(2. * z * n.x, 2. * z * n.y, 2. * z * z - 1.);
    float fr = .04 + .96 * pow(1. - z, 5.);
    float rough = .02 + uFrost * .3;
    // The key light sits a little behind her, so its reflection hugs the upper-left rim
    // instead of lying over the left eye like a brow.
    float env = 30. * softbox(R, normalize(vec3(-.55, -.72, -.2)), vec2(.38, .24), rough)
              + 2.2 * softbox(R, normalize(vec3(.85, .05, -.5)), vec2(.07, .7), rough + .06)
              + .9 * smoothstep(.1, -.9, R.y) + .15 * smoothstep(-.1, .8, R.y) + .04;
    vec3 col = vec3(.93, .96, 1.) * env * fr * (1. - uFrost * .45);
    col += vec3(.8, .85, 1.) * pow(max(0., dot(n, normalize(vec3(-.4, -.55, .75)))), 6.) * .16 * uFrost;
    col += vec3(.06, .07, .09) * uFrost;
    col += (h31(vec3(gl_FragCoord.xy, 5.)) - .5) * .07 * uFrost;
    vec3 film = .5 + .5 * cos(6.2832 * (vec3(0., .33, .67) + (1. - z) * 1.8 + v.y * .3));
    col += film * pow(1. - z, 2.2) * .36 * uIrid;
    col += vec3(.9, .95, 1.) * smoothstep(1. - aa * 3., 1., r) * .22 * smoothstep(.2, -.8, v.x * .6 + v.y * .8);
    col = max(1. - exp(-col * 1.1), 0.);
    float cov = 1. - smoothstep(1. - aa, 1., r);
    o = vec4(col * cov, max(col.r, max(col.g, col.b)) * cov);
  }
}`;
type Uniforms = { t: number; q: number; rot: Float32Array; n: RGB[]; rim: RGB; eyeC: RGB; eyeL: RGB; eyeR: RGB; bright: number };
function makeGL() {
  const canvas = document.createElement('canvas');
  const gl = canvas.getContext('webgl2', { premultipliedAlpha: true, preserveDrawingBuffer: true, antialias: false, alpha: true });
  if (!gl) return null;
  const shader = (type: number, src: string) => {
    const s = gl.createShader(type)!;
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { console.warn(gl.getShaderInfoLog(s)); return null; }
    return s;
  };
  const vs = shader(gl.VERTEX_SHADER, `#version 300 es
in vec2 p;
uniform float uB;
out vec2 v;
void main() { v = vec2(p.x, -p.y) * uB; gl_Position = vec4(p, 0., 1.); }`);
  const fs = shader(gl.FRAGMENT_SHADER, FS);
  if (!vs || !fs) return null;
  const program = gl.createProgram();
  gl.attachShader(program, vs); gl.attachShader(program, fs); gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) { console.warn(gl.getProgramInfoLog(program)); return null; }
  gl.useProgram(program);
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(program, 'p');
  gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const U = Object.fromEntries(['uLayer', 'uB', 'uPx', 'uT', 'uQ', 'uRot', 'uN1', 'uN2', 'uN3', 'uRim', 'uEyeC', 'uEyeL', 'uEyeR', 'uNeb', 'uStars', 'uSoft', 'uGal', 'uAur', 'uRefr', 'uFrost', 'uIrid', 'uBright']
    .map(n => [n, gl.getUniformLocation(program, n)]));
  gl.uniform1f(U.uB, B);
  // Every fragment of the square is written (transparent outside the ball), so nothing needs clearing.
  // Both layers go side by side into one canvas, so a frame stalls on the GPU once, not per copy.
  function render(S: number, u: Uniforms, m: Glass) {
    if (canvas.width !== 2 * S || canvas.height !== S) { canvas.width = 2 * S; canvas.height = S; }
    gl!.uniform1f(U.uPx, 2 * B / S); gl!.uniform1f(U.uT, u.t); gl!.uniform1f(U.uQ, u.q);
    gl!.uniformMatrix3fv(U.uRot, false, u.rot);
    gl!.uniform3fv(U.uN1, u.n[0]); gl!.uniform3fv(U.uN2, u.n[1]); gl!.uniform3fv(U.uN3, u.n[2]);
    gl!.uniform3fv(U.uRim, u.rim); gl!.uniform3fv(U.uEyeC, u.eyeC); gl!.uniform3fv(U.uEyeL, u.eyeL); gl!.uniform3fv(U.uEyeR, u.eyeR);
    gl!.uniform1f(U.uNeb, m.neb); gl!.uniform1f(U.uStars, m.stars); gl!.uniform1f(U.uSoft, m.soft); gl!.uniform1f(U.uGal, m.gal); gl!.uniform1f(U.uAur, m.aur);
    gl!.uniform1f(U.uRefr, m.refr); gl!.uniform1f(U.uFrost, m.frost); gl!.uniform1f(U.uIrid, m.irid); gl!.uniform1f(U.uBright, u.bright);
    for (const layer of [0, 1]) { gl!.viewport(layer * S, 0, S, S); gl!.uniform1i(U.uLayer, layer); gl!.drawArrays(gl!.TRIANGLES, 0, 6); }
  }
  return { canvas, render };
}
let shared: ReturnType<typeof makeGL> | undefined;
const GL = () => shared === undefined ? (shared = makeGL()) : shared;
function rotMat(yaw: number, pitch: number) {
  const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
  const m = [cy, sy * sp, sy * cp, 0, cp, -sp, -sy, cy * sp, cy * cp];
  return new Float32Array([m[0], m[3], m[6], m[1], m[4], m[7], m[2], m[5], m[8]]);
}
function circle(c: CanvasRenderingContext2D, x: number, y: number, r: number, fill: string | CanvasGradient) { c.beginPath(); c.arc(x, y, r, 0, TAU); c.fillStyle = fill; c.fill(); }
function radial(c: CanvasRenderingContext2D, x: number, y: number, r: number, stops: [number, string][]) {
  const g = c.createRadialGradient(x, y, 0, x, y, r);
  for (const [o, col] of stops) g.addColorStop(o, col);
  return g;
}
function sparkle(c: CanvasRenderingContext2D, x: number, y: number, r: number, col: string) {
  c.beginPath(); c.moveTo(x, y - r); c.quadraticCurveTo(x, y, x + r, y); c.quadraticCurveTo(x, y, x, y + r);
  c.quadraticCurveTo(x, y, x - r, y); c.quadraticCurveTo(x, y, x, y - r); c.fillStyle = col; c.fill();
}

// ---------- one character: update once per frame, then paint in layers ----------
export type CoreInput = { expr: ExprId; look: [number, number] | null; still: boolean; pressed: boolean; charge: number };
type Particle = { k: 'z' | 'spark' | 'star'; x: number; y: number; vx: number; vy: number; age: number; life: number; s: number; r: number; c?: RGB };
type State = { L: Eye; R: Eye; head: number; gx: number; gy: number; yaw: number; t: number; env: number; sx: number; sy: number; yOff: number; jx: number;
  spin: number; bright: number; blush: number; orbitK: number; voice: number; eyes: EyePose[]; ripple: number; flash: number };

export class Core {
  skin: Skin;
  mat: Glass;
  morph: { from: Glass; to: Skin; at: number; stage: number } | null = null;
  E: Record<keyof Eye, ReturnType<typeof spring>>[];
  s = { head: spring(0), gx: spring(0), gy: spring(0), stretch: spring(1), lift: spring(0), spinV: spring(.22) };
  light = copyLight(LIGHT.base); bright = 1; blush = 0; orbitK = 0; voiceK = 0;
  seed = Math.random() * 10; spin = Math.random() * TAU; spinStart = -1; spinDur = 1100;
  blinkAt = 0; blinkStart = -1; sacAt = 0; sac: [number, number] = [0, 0];
  hopStart = -1; hopH = 0; hopDur = 380; anticAt = 0; shakeAt = -1; rippleAt = -1; flashAt = -1; celebrateUntil = 0;
  fx: Particle[] = []; zAt = 0; sparkAt = 0; expr: ExprId | null = null; t0 = 0; fired = new Set<number>();
  st!: State;
  constructor(skin: Skin) {
    this.skin = skin; this.mat = { ...SKINS[skin].gl };
    this.E = eyeTargets(BASE).map(t => Object.fromEntries(EYE_KEYS.map(k => [k, spring(t[k])])) as Record<keyof Eye, ReturnType<typeof spring>>);
  }
  hop(now: number, h: number, dur = 380) { if (!reduced.matches) { this.hopStart = now; this.hopH = h; this.hopDur = dur; } }
  effect(name: Effect, now: number) {
    const s = this.s;
    if (name === 'hop') this.hop(now, .2);
    else if (name === 'jolt') { s.stretch.velocity += 2.4; s.lift.velocity -= 1.4; }
    else if (name === 'squash') s.stretch.velocity -= 2.2;
    else if (name === 'shake') this.shakeAt = now;
    else if (name === 'ripple') this.rippleAt = now;
    else if (name === 'spin') { if (!reduced.matches) { this.spinStart = now; this.spinDur = 1100; } }
    else if (name === 'burst' && !reduced.matches) {
      for (let i = 0; i < 18; i++) {
        const a = -PI / 2 + rnd(-1.35, 1.35), sp = rnd(1.2, 2.6);
        this.fx.push({ k: 'star', x: rnd(-.25, .25), y: rnd(-.35, .1), vx: Math.cos(a) * sp, vy: Math.sin(a) * sp - .3, age: 0, life: rnd(1, 1.7), s: rnd(.05, .1), r: rnd(0, PI), c: Math.random() < .5 ? [1, .88, .55] : [1, 1, 1] });
      }
    }
  }
  enter(id: ExprId, now: number) {
    this.expr = id; this.t0 = now; this.fired = new Set();
    const x = EXPRESSIONS[id];
    for (const f of x.enter ?? []) this.effect(f, now);
    this.anticAt = now + rnd(6000, 14000);
    if (x.blink) this.blinkAt = now + rnd(600, 2400);
  }
  // A costume change: crouch, jump and turn round; at the top a flash, and she lands in the new skin.
  change(to: Skin, now: number) {
    if (to === this.skin && !this.morph) return;
    this.morph = { from: { ...this.mat }, to, at: now, stage: 0 };
    this.s.stretch.velocity -= 2.2;
  }
  changing(now: number) {
    const m = this.morph;
    if (!m) return;
    const k = now - m.at, to = SKINS[m.to].gl, a = smooth(380, 640, k);
    if (m.stage === 0 && k >= 140) { m.stage = 1; this.hop(now, .38, 720); if (!reduced.matches) { this.spinStart = now; this.spinDur = 900; } }
    if (m.stage === 1 && k >= 500) { m.stage = 2; this.skin = m.to; this.flashAt = now; this.effect('burst', now); }
    for (const key of Object.keys(to) as (keyof Glass)[]) this.mat[key] = m.from[key] + (to[key] - m.from[key]) * a;
    if (m.stage === 2 && k >= 860) { this.morph = null; this.celebrateUntil = now + 1800; }
  }
  gaze(x: Expr, ov: Frame | null, now: number, t: number, look: [number, number] | null): [number, number, boolean] {
    const mode = x.gaze ?? 'free', gx0 = x.gx ?? 0, gy0 = ov?.gy ?? x.gy ?? 0;
    if (mode === 'free') {
      if (look) return [look[0], look[1], false];
      if (now >= this.sacAt) { const far = Math.random() < .4; this.sac = [rnd(-1, 1) * (far ? .55 : .18), rnd(-1, 1) * (far ? .3 : .1)]; this.sacAt = now + rnd(700, 3300); }
      return [this.sac[0], this.sac[1], true];
    }
    if (mode === 'away') return [gx0 + .05 * Math.sin(t * .7), gy0 + .04 * Math.sin(t * .9), false];
    if (mode === 'up') return [.45 + .15 * Math.sin(t * .8), -.5 + .05 * Math.sin(t * 1.3), false];
    if (mode === 'loop') return [.32 * Math.cos(t * 4.4), .1 + .2 * Math.sin(t * 4.4), true];
    if (mode === 'scan') { const ph = (t * 1.7) % 2, tri = ph < 1 ? ph : 2 - ph; return [-.65 + 1.3 * smooth(0, 1, tri), .12, true]; }
    if (mode === 'sweepY') return [.12 * Math.sin(t * .5), -.05 + .22 * Math.sin(t * 1.15), false];
    if (mode === 'recall') return [-.35 + .12 * Math.sin(t * .6), -.5 + .06 * Math.sin(t * .9), false];
    if (mode === 'read') { const ph = (t * .55) % 1, line = Math.floor((t * .55) % 3); return [ph < .85 ? -.35 + .7 * ph / .85 : .35 - .7 * (ph - .85) / .15, .08 + .07 * line, ph >= .85]; }
    return [gx0, gy0, false];
  }
  // Returns whether anything still moves; `still` (resting in the island) turns idle life off so she can settle.
  update(now: number, dt: number, input: CoreInput) {
    const id = now < this.celebrateUntil && !FACES.has(input.expr) ? '10' : input.expr;
    if (id !== this.expr) this.enter(id, now);
    const s = this.s, t = now / 1000, x = EXPRESSIONS[id], el = now - this.t0, seq = !!x.seq && el < x.seq.end;
    let ov: Frame | null = null;
    if (x.seq && seq) for (const [i, f] of x.seq.frames.entries()) if (el >= f.at) {
      ov = f;
      if (f.fx && !this.fired.has(i)) { this.fired.add(i); this.effect(f.fx, now); }
    }
    const frozen = x.freeze && x.seq && el >= x.seq.end - 200;
    const calm = reduced.matches || frozen || input.still ? 0 : 1;
    let moving = false;
    // eyes
    const shape: Shape = { ...BASE, ...x.eyes, ...(ov?.eyes ?? {}) };
    const tg = eyeTargets(shape);
    const [eh, ed] = x.spring ?? (ov ? [9, .8] : [5.2, .62]);
    for (let i = 0; i < 2; i++) for (const k of EYE_KEYS) moving = step(this.E[i][k], tg[i][k], k === 'a' ? 14 : eh, k === 'a' ? 1 : ed, dt) || moving;
    // gaze
    let [gx, gy, fast] = this.gaze(x, ov, now, t, input.look);
    if (this.shakeAt >= 0) { const k = (now - this.shakeAt) / 800; if (k < 1) { gx = .55 * Math.sin(k * 3 * TAU) * (1 - k); fast = true; } else this.shakeAt = -1; }
    if (!calm && x.gaze !== 'still') { gx = 0; gy = 0; }
    moving = step(s.gx, gx, fast ? 9 : 3.2, .9, dt) || moving;
    moving = step(s.gy, gy, fast ? 9 : 3.2, .9, dt) || moving;
    const sway = x.sway ? x.sway[0] * Math.sin(t * TAU / x.sway[1]) : 0;
    moving = step(s.head, shape.head + calm * (sway + 2.2 * Math.sin(t * .37 + this.seed)), 3, .8, dt) || moving;
    // blink: quick close, slower open, sometimes twice; the right eye a hair later
    if (x.blink && calm && this.blinkStart < 0 && now >= this.blinkAt && !input.pressed) this.blinkStart = now;
    let bl = 1, br = 1;
    if (this.blinkStart >= 0) {
      const k = (now - this.blinkStart) / 1000 * (x.blinkSlow ? .45 : 1);
      bl = blinkCurve(k); br = blinkCurve(k - .022);
      if (k > .3) { this.blinkStart = -1; this.blinkAt = now + (Math.random() < .18 ? 150 : rnd(...(x.blink ?? [3000, 6000]))); }
    }
    if (x.lidWave) { const w = Math.sin(t * TAU / x.lidWave); bl *= 1 - .92 * smooth(.1, .9, w); br *= 1 - .92 * smooth(.1, .9, -w); }
    // antics: a full turn (eyes go round the back) or a small hop
    if (x.antics && calm && now >= this.anticAt) {
      if (x.antics[Math.floor(Math.random() * x.antics.length)] === 'spin') this.effect('spin', now); else this.hop(now, .14);
      this.anticAt = now + rnd(9000, 18000);
    }
    this.changing(now);
    let yaw = 0;
    if (this.spinStart >= 0) { const k = (now - this.spinStart) / this.spinDur; if (k < 1) yaw = TAU * smooth(0, 1, k); else this.spinStart = -1; }
    // body: holding her down squashes her further while a costume change charges up
    const charge = input.charge;
    moving = step(s.stretch, input.pressed ? .86 - .05 * charge : (x.tall ?? 1) * (x.sink ? .97 : 1), input.pressed ? 10 : 4.2, input.pressed ? .9 : .45, dt) || moving;
    moving = step(s.lift, x.sink ?? 0, 3, .8, dt) || moving;
    this.voiceK += ((x.voice ? 1 : 0) - this.voiceK) * (1 - Math.exp(-3 * dt));
    const phrase = (.5 + .5 * Math.sin(t * 1.7 - .8)) ** 2, syl = (.5 + .5 * Math.sin(t * 7.3 + 1.3 * Math.sin(t * 2.1))) ** 2;
    const env = calm * this.voiceK * phrase * (.35 + .65 * syl);
    const br0 = x.breathe ?? [.007, 4.2];
    const stretch = s.stretch.value * (1 + calm * br0[0] * Math.sin(t * TAU / br0[1]) + .028 * env);
    let hopY = 0;
    if (this.hopStart >= 0) {
      const k = (now - this.hopStart) / this.hopDur;
      if (k >= 1) { this.hopStart = -1; s.stretch.velocity -= this.hopH > .3 ? 2.4 : 1.4; } else hopY = -this.hopH * 4 * k * (1 - k);
    }
    const bounce = x.bounce && calm ? -x.bounce[0] * Math.abs(Math.sin(PI * now / x.bounce[1])) : 0;
    const bob = calm * (x.bob ?? .03) * Math.sin(t * TAU / 3.3 + this.seed);
    const trem = ((ov?.tremble ?? x.tremble ?? 0) + .012 * charge) * (reduced.matches ? 0 : 1);
    const jx = trem * (Math.sin(t * 57.1) + Math.sin(t * 83.7 + 1.3)) * .5;
    // stars spin faster while she charges
    step(s.spinV, calm * (ov?.spin ?? x.spin ?? .22) + 2.6 * charge, 1.2, 1, dt);
    this.spin += s.spinV.value * dt;
    // light
    const L = LIGHT[ov?.light ?? x.light ?? 'base'], gap = mixLight(this.light, L, 1 - Math.exp(-(ov?.lightK ?? x.lightK ?? 5) * dt));
    let bt = (ov?.bright ?? x.bright ?? 1) * L.b * (1 + .25 * env) * (1 + .3 * charge);
    if (x.flicker && calm) bt *= 1 - x.flicker * (.5 + .5 * Math.sin(t * 37) * Math.sin(t * 23.3));
    const dim = Math.abs(bt - this.bright);
    this.bright += (bt - this.bright) * (1 - Math.exp(-(ov ? 14 : 6) * dt));
    this.blush += ((x.blush ?? 0) - this.blush) * (1 - Math.exp(-2 * dt));
    this.orbitK += ((x.fx?.orbit ? 1 : 0) - this.orbitK) * (1 - Math.exp(-3 * dt));
    // particles
    if (x.fx?.zzz && calm && now >= this.zAt) { this.fx.push({ k: 'z', x: .5, y: -.72, vx: .18, vy: -.4, age: 0, life: 2.8, s: .14, r: 0 }); this.zAt = now + 1300; }
    if (x.fx?.sparkle && calm && now >= this.sparkAt) {
      for (let i = 0; i < 2; i++) { const a = rnd(0, TAU), rr = rnd(1.08, 1.28); this.fx.push({ k: 'spark', x: Math.cos(a) * rr, y: Math.sin(a) * rr - .1, vx: 0, vy: -.05, age: 0, life: .8, s: rnd(.07, .11), r: 0 }); }
      this.sparkAt = now + rnd(1300, 2600);
    }
    for (const p of this.fx) {
      p.age += dt; p.x += p.vx * dt; p.y += p.vy * dt;
      if (p.k === 'star') { p.vy += 3.2 * dt; p.r += dt * 4; }
    }
    this.fx = this.fx.filter(p => p.age < p.life);
    // what the painters read
    const [eL, eR] = this.E.map((e, i) => {
      const o = Object.fromEntries(EYE_KEYS.map(k => [k, e[k].value])) as Eye;
      o.lid = Math.max(.04, o.lid * (i ? br : bl) * (input.pressed ? .35 : 1));
      o.len = Math.max(0, o.len * (1 + .08 * env)); o.w = Math.max(.02, o.w); o.a = clamp(o.a, 0, 1);
      return o;
    });
    const phase = (at: number, ms: number) => { if (at < 0) return -1; const k = (now - at) / ms; return k < 1 ? k : -1; };
    this.rippleAt = phase(this.rippleAt, 900) < 0 ? -1 : this.rippleAt;
    this.flashAt = phase(this.flashAt, 420) < 0 ? -1 : this.flashAt;
    this.st = {
      L: eL, R: eR, head: s.head.value, gx: s.gx.value, gy: s.gy.value, yaw, t, env,
      sx: (1 + .012 * env) / Math.sqrt(stretch), sy: stretch, yOff: hopY + bob + bounce + s.lift.value, jx,
      spin: this.spin, bright: this.bright, blush: this.blush, orbitK: this.orbitK, voice: this.voiceK,
      eyes: eyeSet(eL, eR, s.gx.value, s.gy.value, yaw, s.head.value), ripple: phase(this.rippleAt, 900), flash: phase(this.flashAt, 420),
    };
    return !input.still || moving || gap > .004 || dim > .004 || this.blinkStart >= 0 || this.hopStart >= 0 || this.spinStart >= 0
      || this.shakeAt >= 0 || this.rippleAt >= 0 || this.flashAt >= 0 || this.fx.length > 0 || !!this.morph || seq;
  }
  // Her own motion on top of where she is: hop, tremble, breathing, squash. `k` fades it out inside the island.
  pose(k: number): [number, number, number, number] {
    const st = this.st;
    return [st.jx * k, st.yOff * k, 1 + (st.sx - 1) * k, 1 + (st.sy - 1) * k];
  }
  // Renders both GL layers for this frame at S device pixels; false without WebGL2.
  render(S: number, q: number) {
    const gl = GL();
    if (!gl) return false;
    const st = this.st, L = this.light, [eL, eR] = st.eyes;
    const spill = (e: EyePose): RGB => [e.c[0], e.c[1], e.a * clamp(e.lid, 0, 1) * (.75 + .6 * st.env) * Math.min(1.3, st.bright + .2)];
    gl.render(S, { t: st.t, q, rot: rotMat(-(st.spin + st.gx * .5 + st.yaw), .3 + st.gy * .4), n: L.n, rim: L.rim, eyeC: L.glow, eyeL: spill(eL), eyeR: spill(eR), bright: st.bright }, this.mat);
    return true;
  }
  // The painters below draw at the ball centre in ball radii R, under the caller's pose.
  inside(c: CanvasRenderingContext2D, R: number, S: number) {
    const st = this.st, gl = GL();
    if (gl) c.drawImage(gl.canvas, 0, 0, S, S, -B * R, -B * R, 2 * B * R, 2 * B * R);
    if (st.blush > .01) {
      c.save(); c.globalCompositeOperation = 'lighter';
      for (const e of st.eyes) {
        const x = e.c[0] * R * 1.25, y = (e.c[1] + .22) * R;
        circle(c, x, y, .24 * R, radial(c, x, y, .24 * R, [[0, `rgba(255,120,170,${.42 * st.blush})`], [1, 'rgba(255,120,170,0)']]));
      }
      c.restore();
    }
    if (st.voice > .01) {
      const a = st.voice * (.25 + .75 * st.env / Math.max(.2, st.voice));
      c.save(); c.strokeStyle = rgba(this.light.glow, .55 * a); c.lineCap = 'round'; c.lineWidth = R * .06;
      c.beginPath(); c.arc(0, 0, R * .84, (90 - 22 - 18 * st.env) * D, (90 + 22 + 18 * st.env) * D); c.stroke(); c.restore();
    }
  }
  glass(c: CanvasRenderingContext2D, R: number, S: number) {
    const st = this.st, gl = GL();
    if (gl) c.drawImage(gl.canvas, S, 0, S, S, -B * R, -B * R, 2 * B * R, 2 * B * R);
    if (st.ripple >= 0) {
      const k = st.ripple;
      c.save(); c.beginPath(); c.arc(0, 0, R, 0, TAU); c.clip();
      c.strokeStyle = rgba(this.light.glow, .6 * (1 - k)); c.lineWidth = R * .07 * (1 - k * .5);
      c.beginPath(); c.arc(0, .05 * R, R * (.15 + .95 * smooth(0, 1, k)), 0, TAU); c.stroke(); c.restore();
    }
    // the costume-change flash: white from the centre, gone in under half a second
    if (st.flash >= 0) {
      const a = (1 - st.flash) ** 2;
      circle(c, 0, 0, R * (1.05 + .35 * st.flash), radial(c, 0, 0, R * (1.05 + .35 * st.flash), [[0, `rgba(255,255,255,${.95 * a})`], [.7, `rgba(235,240,255,${.6 * a})`], [1, 'rgba(235,240,255,0)']]));
    }
  }
  eyes(c: CanvasRenderingContext2D, R: number) {
    const col = rgba(this.light.eye);
    for (const E of this.st.eyes) { if (E.a < .01) continue; c.save(); c.globalAlpha = E.a; drawEye(c, R, E, col); c.restore(); }
  }
  // The eyes glow through the glass: colour and blur in ball radii.
  glow(): [string, number] { return [rgba(this.light.glow, .9), .34 * Math.min(1.2, .4 + .6 * this.st.bright)]; }
  // Motes circling the head (side -1 behind her, 1 in front) and loose particles, around the ball centre.
  orbit(c: CanvasRenderingContext2D, R: number, side: number) {
    const st = this.st;
    if (st.orbitK < .01) return;
    c.save(); c.globalCompositeOperation = 'lighter';
    for (let i = 0; i < 5; i++) {
      const a = st.t * 1.5 + i * TAU / 5, z = Math.sin(a);
      if (Math.sign(z) !== side) continue;
      const x = Math.cos(a) * 1.24 * R, y = (-.62 + .2 * z) * R + st.yOff * R, r = R * (.045 + .02 * z);
      circle(c, x, y, r * 3, radial(c, x, y, r * 3, [[0, rgba(this.light.glow, .5 * st.orbitK)], [1, rgba(this.light.glow, 0)]]));
      circle(c, x, y, r, rgba([1, 1, 1], .9 * st.orbitK * (.6 + .4 * z * side)));
    }
    c.restore();
  }
  particles(c: CanvasRenderingContext2D, R: number, d: number) {
    for (const p of this.fx) {
      const k = p.age / p.life, fade = Math.min(1, p.age / .2) * (1 - smooth(.6, 1, k));
      c.save();
      if (p.k === 'z') {
        c.font = `600 ${Math.round((p.s + .14 * k) * R * 10) / 10}px ui-monospace, monospace`;
        c.fillStyle = rgba(this.light.glow, .85 * fade); c.fillText('z', p.x * R, p.y * R);
      } else {
        c.translate(p.x * R, p.y * R); c.rotate(p.r);
        c.shadowColor = rgba(p.c ?? this.light.glow, .9); c.shadowBlur = R * .1 * d;
        sparkle(c, 0, 0, p.s * R * (p.k === 'spark' ? Math.sin(k * PI) : 1), rgba(p.c ?? [1, 1, 1], fade));
      }
      c.restore();
    }
  }
}

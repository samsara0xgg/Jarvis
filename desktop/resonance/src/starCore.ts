// Star-core (星核): a glass ball with stars inside and two glowing eyes. One WebGL program
// paints her inside and her glass; a skin only changes that program's numbers, so one skin
// can morph into another. Ported from the approved study (handoff lab/xinghe.html).
// inline, so WebGL may read it wherever the page is loaded from
import nebulaUrl from './assets/skins/icon-sky.jpg?inline';
const PI = Math.PI, TAU = 2 * PI, D = PI / 180;
export const B = 1.3; // half-size of the square the GL layers cover, in ball radii
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
  // Think mode (ADR 0064): the ordinary thinking violet, deeper and quieter, for as long as the mode is on.
  deep: light('#f1edff', '#9a86ff', ['#170d4a', '#351c7a', '#10184a'], '#9a86ff', .92),
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
// tex: a painted sky (the app icon's) in place of the generated nebula.
type Glass = { neb: number; stars: number; soft: number; gal: number; aur: number; refr: number; frost: number; irid: number; tex: number };
export const SKINS = {
  glass: { name: '深空玻璃', gl: { neb: .3, stars: 1, soft: 0, gal: 0, aur: 0, refr: 1, frost: 0, irid: 0, tex: 0 } },
  nebula: { name: '星云', gl: { neb: 1, stars: .75, soft: 0, gal: 0, aur: 0, refr: .7, frost: 0, irid: 0, tex: 0 } },
  galaxy: { name: '银河', gl: { neb: .18, stars: .65, soft: 0, gal: 1, aur: 0, refr: .8, frost: 0, irid: 0, tex: 0 } },
  frost: { name: '磨砂', gl: { neb: .4, stars: .9, soft: 1, gal: 0, aur: 0, refr: .25, frost: 1, irid: 0, tex: 0 } },
  aurora: { name: '极光', gl: { neb: .12, stars: .7, soft: 0, gal: 0, aur: 1, refr: .8, frost: 0, irid: 1, tex: 0 } },
  codex: { name: '图标同款', gl: { neb: 0, stars: .35, soft: 0, gal: 0, aur: 0, refr: .7, frost: 0, irid: 0, tex: 1 } },
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
export type ExprId = '00' | '02' | '10' | '13' | '14' | '21' | '30' | '31' | '31b' | '31c' | '31d' | '32' | '33' | '34' | '35' | '35b' | '36' | '37' | '38' | '39' | '39b' | '39c' | '40' | '41' | 'ask' | 'fin' | 'deep' | 'home' | 'rest' | 'doze' | 'glance' | 'peek';
export const EXPRESSIONS: Record<ExprId, Expr> = {
  // Where she is decides her face first: flat "— —" in the island, round dots when she glances out, low eyes when she peeks.
  home: { name: '', eyes: LINES, gaze: 'still' },
  // With her glass showing at home she stays awake there, blinking and looking around, and dozes on the same lines after a long quiet spell.
  rest: { name: '', eyes: { sep: .3, y: .08, len: .36, w: .245, tilt: 9 }, gaze: 'free', blink: [2600, 6000] },
  doze: { name: '', eyes: LINES, light: 'dim', gaze: 'still', breathe: [.02, 5], bob: .01, fx: { zzz: true } },
  glance: { name: '', eyes: { sep: .36, y: .02, len: .18, w: .18, tilt: 0 }, gaze: 'free' },
  peek: { name: '', eyes: { sep: .27, y: .32, len: .36, w: .245, tilt: 9 }, gaze: 'free', blink: [2400, 6600] },
  '00': { name: '睡眠', eyes: { ...SLEEP, head: -4 }, light: 'dim', gaze: 'still', gy: .2, spin: .05, breathe: [.022, 5.5], bob: .012, fx: { zzz: true } },
  '02': { name: '待机', eyes: { len: .36, w: .245, tilt: 9, y: .1 }, gaze: 'free', blink: [2400, 6600], antics: ['spin', 'hop'] },
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
  // Agent notices (the notice lab): warm and looking down at the card when a session needs you; the done face
  // in green when one has finished. Her light is the card's light, so the panel takes the event's colour.
  ask: { name: '等你', eyes: { len: .52, w: .21, tilt: 14 }, light: 'warm', gaze: 'still', gx: 0, gy: .32, sway: [3, 3.2], blink: [2400, 6000], spin: .35, breathe: [.012, 2.8], enter: ['hop'] },
  // Think mode: looking far up and holding it, blinking slowly; no stars circle her.
  deep: { name: '深想', eyes: { sep: .27, y: -.02, len: .3, w: .17, tilt: 0, cut: .16 }, light: 'deep', gaze: 'still', gx: .38, gy: -.52, blink: [4200, 8000], blinkSlow: true, spin: .35, breathe: [.014, 5] },
  fin: { name: '做完了', eyes: SMILE, light: 'speak', spin: .5, enter: ['hop', 'burst'], seq: { frames: [
    { at: 0, light: 'gold', bright: 1.4, spin: 3 }, { at: 1400, light: 'speak', bright: 1, spin: .5 },
  ], end: 1500 } },
};
// The twelve work states first, then five feelings: what Settings plays with "Play all".
export const PREVIEW: ExprId[] = ['30', '31', '31b', '31c', '31d', '32', '33', '34', '35', '35b', '36', '37', '38', '39', '39b', '39c', '40', '41', '10', '14', '13', '00', '21'];
// The takes she picks from at random each time.
export const TAKES = { listen: ['35', '35b'], receive: ['31', '31b', '31c', '31d'], reply: ['39', '39b', '39c'] } satisfies Record<string, ExprId[]>;
export const pick = (ids: ExprId[]) => ids[Math.floor(Math.random() * ids.length)];
const FACES = new Set<ExprId>(['home', 'rest', 'doze', 'glance', 'peek']);
// Motion v2. Faces she falls asleep into, and how far apart two faces are (1 = too far to morph, hide it behind a blink).
const SLEEPY = new Set<ExprId>(['home', 'doze', '00']);
const shapeOf = (id: ExprId): Shape => ({ ...BASE, ...EXPRESSIONS[id].eyes });
const gapOf = (a: Shape, b: Shape) => Math.max(Math.abs(a.tilt + a.lean - b.tilt - b.lean) / 50, Math.abs(a.len - b.len) / .24,
  Math.abs((a.lenR ?? a.len) - (b.lenR ?? b.len)) / .24, Math.abs(a.w - b.w) / .14, Math.abs((a.wR ?? a.w) - (b.wR ?? b.w)) / .14,
  Math.abs(a.head - b.head) / 18, Math.abs(a.cut - b.cut) / .3, Math.abs(a.sep - b.sep) / .1);
// blink: a face change hides behind a blink; lead: eyes jump first, body follows; attend: looks at a moving cursor, loses interest when it stops;
// pet: squints when the cursor rests on her; perk: lights up the moment something starts; hop: crouches before a jump; lag: eyes trail her body.
export type Motion = { blink: boolean; lead: boolean; attend: boolean; pet: boolean; perk: boolean; hop: boolean; lag: boolean };
export const MOTION_V1: Motion = { blink: false, lead: false, attend: false, pet: false, perk: false, hop: false, lag: false };
export const MOTION_V2: Motion = { blink: true, lead: true, attend: true, pet: true, perk: true, hop: true, lag: true };

// ---------- life: layers that run together on top of the face ----------
// alive: breathing, blinking and small idle moments, paced by her energy; attend: what she looks at and for how long;
// mood: energy and joy tint every face; react: short gestures stacked on top; voice: real voice levels drive her.
export type Layers = { alive: boolean; attend: boolean; mood: boolean; react: boolean; voice: boolean };
export const LAYERS_ON: Layers = { alive: true, attend: true, mood: true, react: true, voice: true };
export const LAYERS_OFF: Layers = { alive: false, attend: false, mood: false, react: false, voice: false };
export type Mood = { energy: number; joy: number };
export const NEUTRAL: Mood = { energy: .6, joy: .5 };
export type ActKind = 'nod' | 'flinch' | 'wince' | 'bulge' | 'wiggle' | 'drift' | 'yawn' | 'sigh' | 'glow' | 'swirl';
type Act = { kind: ActKind; at: number; dur: number; amp: number; dir: number };
const ACT_MS: Record<ActKind, number> = { nod: 460, flinch: 620, wince: 700, bulge: 700, wiggle: 800, drift: 2600, yawn: 2100, sigh: 1700, glow: 600, swirl: 900 };
export const ACT_NAME: Record<ActKind, string> = { nod: '点头', flinch: '一缩', wince: '皱一下', bulge: '鼓一下', wiggle: '扭一扭', drift: '飘一下',
  yawn: '打哈欠', sigh: '叹口气', glow: '亮一下', swirl: '转一下星星' };
// A thing that wants her attention (a notice card): where it is, in gaze units, and when it showed up.
export type Poi = { g: [number, number]; at: number; why: string };
const IDLE = new Set<ExprId>(['rest', '02', 'peek', 'home', 'glance']);
const LISTEN = new Set<ExprId>(['35', '35b']);
const YOU: [number, number] = [0, -.03];
const lerp = (a: number, b: number, k: number) => a + (b - a) * k;
const lerp3 = (a: RGB, b: RGB, k: number): RGB => [lerp(a[0], b[0], k), lerp(a[1], b[1], k), lerp(a[2], b[2], k)];
const blendLight = (a: Light, b: Light, k: number): Light => k <= 0 ? a
  : { eye: lerp3(a.eye, b.eye, k), glow: lerp3(a.glow, b.glow, k), n: a.n.map((c, i) => lerp3(c, b.n[i], k)), rim: lerp3(a.rim, b.rim, k), b: lerp(a.b, b.b, k) };
// Joy curves an open eye's lower edge into a smile or lets the inner ends rise into a worried lid; energy is handled where she paints.
function moodShape(s: Shape, joy: number): Shape {
  const o = { ...s }, flat = Math.abs(s.tilt) > 60;
  if (joy > .55 && !flat) o.cutB = Math.max(o.cutB, (joy - .55) * .62);
  if (joy > .55 && flat && s.bend > 0) o.bend = s.bend + (joy - .55) * .12;
  if (joy < .45 && !flat && s.cut < .05) { o.cut = (.45 - joy) * .55; o.cutT = -16; o.y += (.45 - joy) * .08; }
  return o;
}

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
uniform sampler2D uTex;
uniform float uTexK, uTexA, uTint;
uniform vec2 uTexO;
uniform vec3 uTintC;

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
    if (uTexK > 0.) {
      // the painted sky turns slowly about her centre, slides a little with her gaze, and takes on the
      // expression's hue away from rest; a painting overexposes fast, so brightening past 1 is gentler
      float ca = cos(uTexA), sa = sin(uTexA);
      vec3 tc = texture(uTex, mat2(ca, sa, -sa, ca) * (v - uTexO * z) * .5 + .5, -.8).rgb;
      tc = mix(tc, uTintC * dot(tc, vec3(.3, .55, .15)) * 2.2, uTint) * (uBright > 1. ? 1. + (uBright - 1.) * .4 : uBright);
      col += -log(1. - min(tc, .996)) / 1.1 * uTexK;
    }
    col *= mix(.45, 1., smoothstep(0., .6, z));
    col += uRim * pow(1. - z, 6.) * .5 * min(uBright, 1.2);
    col = 1. - exp(-col * 1.1);
    col += (h31(vec3(gl_FragCoord.xy, 9.)) - .5) / 255.;
    float cov = 1. - smoothstep(1. - aa, 1., r);
    o = vec4(clamp(col, 0., 1.) * cov, cov);
  } else {
    // Only her own light (GLASS_GLOW): no highlight, environment reflection,
    // glass rim, frost lighting or iridescent film. Expression ripples still paint above it.
    o = vec4(0.);
  }
}`;
type Uniforms = { t: number; q: number; rot: Float32Array; n: RGB[]; rim: RGB; eyeC: RGB; eyeL: RGB; eyeR: RGB; bright: number;
  texA: number; texO: [number, number]; tint: number; tintC: RGB };
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
  const U = Object.fromEntries(['uLayer', 'uB', 'uPx', 'uT', 'uQ', 'uRot', 'uN1', 'uN2', 'uN3', 'uRim', 'uEyeC', 'uEyeL', 'uEyeR', 'uNeb', 'uStars', 'uSoft', 'uGal', 'uAur', 'uRefr', 'uFrost', 'uIrid', 'uBright',
    'uTex', 'uTexK', 'uTexA', 'uTexO', 'uTint', 'uTintC']
    .map(n => [n, gl.getUniformLocation(program, n)]));
  gl.uniform1f(U.uB, B);
  // The painted sky: black until its picture has loaded.
  const tex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([0, 0, 0, 255]));
  for (const [k, val] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]])
    gl.texParameteri(gl.TEXTURE_2D, k, val);
  gl.uniform1i(U.uTex, 0);
  const img = new Image();
  img.onload = () => { gl.bindTexture(gl.TEXTURE_2D, tex); gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img); gl.generateMipmap(gl.TEXTURE_2D); };
  img.src = nebulaUrl;
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
    gl!.uniform1f(U.uTexK, m.tex); gl!.uniform1f(U.uTexA, u.texA); gl!.uniform2fv(U.uTexO, u.texO); gl!.uniform1f(U.uTint, u.tint); gl!.uniform3fv(U.uTintC, u.tintC);
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
// vel: how fast her body travels, in her radii per second (v2 lets the eyes trail it).
// hear: your voice level 0..1; say: her own voice level (none = the old made-up mouth); dim: fades her light (offline).
export type CoreInput = { expr: ExprId; look: [number, number] | null; still: boolean; pressed: boolean; vel?: [number, number];
  hear?: number; say?: number; mood?: Mood; poi?: Poi | null; dim?: number; deep?: boolean };
type Particle = { k: 'z' | 'spark' | 'star'; x: number; y: number; vx: number; vy: number; age: number; life: number; s: number; r: number; c?: RGB };
type State = { L: Eye; R: Eye; head: number; gx: number; gy: number; yaw: number; t: number; env: number; sx: number; sy: number; yOff: number; jx: number;
  spin: number; bright: number; blush: number; orbitK: number; voice: number; eyes: EyePose[]; ripple: number; flash: number; lx: number; ly: number; dx: number };

export class Core {
  skin: Skin;
  mat: Glass;
  morph: { from: Glass; to: Skin; at: number; stage: number } | null = null;
  E: Record<keyof Eye, ReturnType<typeof spring>>[];
  s = { head: spring(0), gx: spring(0), gy: spring(0), stretch: spring(1), lift: spring(0), spinV: spring(.22),
    ex: spring(0), ey: spring(0), lagX: spring(0), lagY: spring(0), lagB: spring(0), pet: spring(0) };
  motion: Motion;
  // Optional observer used by character previews and acceptance checks.
  beat: (name: string) => void = () => {};
  from: Shape | null = null; swapAt = 0; swapUntil = 0; drowse = 0; twice = false; perkAt = -1;
  micro: [number, number] = [0, 0]; microAt = 0; petOn = false;
  attn = { on: false, last: [0, 0] as [number, number], checkAt: 0, bored: 0, back: 0, since: 0 };
  light = copyLight(LIGHT.base); bright = 1; blush = 0; orbitK = 0; voiceK = 0;
  seed = Math.random() * 10; spin = Math.random() * TAU; texA = 0; spinStart = -1; spinDur = 1100;
  blinkAt = 0; blinkStart = -1; sacAt = 0; sac: [number, number] = [0, 0];
  hopStart = -1; hopH = 0; hopDur = 380; anticAt = 0; shakeAt = -1; rippleAt = -1; flashAt = -1; celebrateUntil = 0;
  fx: Particle[] = []; zAt = 0; sparkAt = 0; expr: ExprId | null = null; t0 = 0; fired = new Set<number>();
  // life
  layers: Layers; temper = .5; acts: Act[] = []; focus: { g: [number, number]; until: number; why: string } | null = null;
  idleAt = 0; checkAt = 0; sighAt = 0; hearK = 0; sayK = 0; talkFrom = -1; hushAt = -1; nodAt = 0; sayFrom = -1; sayHush = -1;
  tiltSide = 1; tiltAt = 0; poiAt = -1; heardAt = -1e9; yawnAt = 0; tired = false; mood = { energy: spring(NEUTRAL.energy), joy: spring(NEUTRAL.joy) };
  // Current attention and gesture, also available to character previews.
  looking = ''; lastAct = { name: '', at: -1e9 };
  st!: State;
  constructor(skin: Skin, motion: Motion = MOTION_V1, layers: Layers = LAYERS_OFF) {
    this.skin = skin; this.mat = { ...SKINS[skin].gl }; this.motion = motion; this.layers = layers;
    this.E = eyeTargets(BASE).map(t => Object.fromEntries(EYE_KEYS.map(k => [k, spring(t[k])])) as Record<keyof Eye, ReturnType<typeof spring>>);
  }
  hop(now: number, h: number, dur = 380) {
    if (reduced.matches) return;
    // v2: a crouch first, then the jump.
    if (this.motion.hop) { this.s.stretch.velocity -= 1.2 + 5 * h; this.hopStart = now + 90; this.beat('先蹲再跳'); } else this.hopStart = now;
    this.hopH = h; this.hopDur = dur;
  }
  // v2: the moment something starts she lights up and her eyes open a little wider.
  perk(now: number) {
    if (!this.motion.perk) return;
    this.perkAt = now; if (!reduced.matches) this.s.lift.velocity -= 1.6;
    this.beat('一亮');
  }
  // A short gesture stacked on whatever she is doing; idle moments belong to the alive layer, the rest to react.
  act(kind: ActKind, now: number, amp = 1, layer: keyof Layers = 'react', dir = Math.random() < .5 ? -1 : 1) {
    if (!this.layers[layer] || reduced.matches) return;
    if (kind === 'swirl') this.s.spinV.velocity += 5 * amp;
    this.acts.push({ kind, at: now, dur: ACT_MS[kind], amp, dir });
    this.lastAct = { name: ACT_NAME[kind], at: now }; this.beat(ACT_NAME[kind]);
  }
  // Her eyes go to a point for a while; a far jump takes a blink with it.
  lookAt(g: [number, number], now: number, ms: number, why: string) {
    if (!this.layers.attend) return;
    if (this.blinkStart < 0 && Math.hypot(g[0] - this.s.ex.value, g[1] - this.s.ey.value) > .5 && Math.random() < .5) this.blinkStart = now;
    this.focus = { g, until: now + ms, why }; this.beat(why);
  }
  // Something new showed up (a star beside the notch): a turn of the head when she is free, a flick of the eyes when busy.
  notice(g: [number, number], now: number, why: string) {
    if (!this.layers.attend || !this.expr) return;
    const free = IDLE.has(this.expr) || this.expr === 'ask';
    if (!free && LISTEN.has(this.expr)) return;
    this.lookAt(g, now, free ? rnd(900, 1300) : 380, why);
    if (free) this.perk(now);
  }
  // One small moment when nothing is going on; how often, and which, depend on her energy, joy and temper.
  idle(now: number, e: number, j: number) {
    const lively = this.temper, opts: [() => void, number][] = [];
    if (this.layers.attend) {
      opts.push([() => this.lookAt([rnd(-.65, .65), rnd(-.4, .25)], now, rnd(700, 1500), '瞟一眼别处'), 26 - 10 * lively]);
      opts.push([() => this.lookAt(YOU, now, rnd(1000, 1700), '看看你'), 14]);
    }
    opts.push([() => this.act('bulge', now, .6 + .6 * lively, 'alive'), 6 + 14 * lively]);
    opts.push([() => this.act('wiggle', now, .6 + .6 * lively, 'alive'), 4 + 12 * lively]);
    opts.push([() => this.act('drift', now, .7 + .5 * lively, 'alive'), 10]);
    opts.push([() => this.act('swirl', now, .6 + .8 * lively, 'alive'), 6 + 8 * lively]);
    if (e < .4) opts.push([() => this.act('yawn', now, 1, 'alive'), 60 * (.4 - e) / .4 + 10]);
    if (j < .42) opts.push([() => this.act('sigh', now, 1, 'alive'), 16]);
    let r = Math.random() * opts.reduce((a, o) => a + o[1], 0);
    for (const [run, w] of opts) if ((r -= w) <= 0) { run(); return; }
  }
  // The per-frame rules of the life layers that decide something; the pose changes are in update.
  life(now: number, id: ExprId, e: number, j: number, poi: Poi | null | undefined) {
    const Lf = this.layers;
    // voice: while you talk she changes the angle now and then; at a pause after a phrase she nods.
    if (LISTEN.has(id) && Lf.voice) {
      if (this.hearK > .15) this.heardAt = now;
      if (this.hearK > .2) {
        if (this.talkFrom < 0) this.talkFrom = now;
        this.hushAt = -1;
        if (!this.tiltAt) this.tiltAt = now + rnd(3200, 4800);
        else if (now > this.tiltAt) { this.tiltSide *= -1; this.tiltAt = now + rnd(3200, 5200); this.beat('换个角度听'); }
      } else if (this.hearK < .08 && this.talkFrom >= 0) {
        if (this.hushAt < 0) this.hushAt = now;
        else if (now - this.hushAt > 200) {
          if (now - this.talkFrom > 600 && now - this.nodAt > 1100 && Math.random() < .8) { this.nodAt = now; this.act('nod', now, .8 + .4 * this.temper); }
          this.talkFrom = -1; this.hushAt = -1;
        }
      }
    }
    // voice: between her own sentences she blinks, and often looks at you.
    if (this.voiceK > .5 && Lf.voice) {
      if (this.sayK > .15) { if (this.sayFrom < 0) this.sayFrom = now; this.sayHush = -1; }
      else if (this.sayK < .05 && this.sayFrom >= 0) {
        if (this.sayHush < 0) this.sayHush = now;
        else if (now - this.sayHush > 240) {
          if (now - this.sayFrom > 600) {
            if (this.blinkStart < 0) this.blinkStart = now;
            if (Math.random() < .7) this.lookAt(YOU, now, 560, '说完一句，看你一眼');
            if (Math.random() < .35) this.act('nod', now, .5);
          }
          this.sayFrom = -1; this.sayHush = -1;
        }
      }
    }
    // attend: while thinking she checks on you now and then; while a card waits she looks up at you between reading it.
    if (Lf.attend && this.checkAt && now > this.checkAt && (id === '30' || id === 'ask')) {
      this.lookAt(YOU, now, id === '30' ? 650 : 900, id === '30' ? '瞄你一眼：我还在想' : '抬头看你：你看这个');
      this.checkAt = now + (id === '30' ? rnd(4200, 6000) : rnd(3000, 5000));
    }
    // attend: something new wants her (a card): a small start, then her eyes go to it.
    if (poi && poi.at !== this.poiAt) { this.poiAt = poi.at; this.act('flinch', now, .45); this.lookAt(poi.g, now, 1400, poi.why); }
    if (!poi) this.poiAt = -1;
    // alive: when she gets tired she yawns soon after, not just by chance.
    if (Lf.alive && Lf.mood && e < .3 !== this.tired) { this.tired = e < .3; if (this.tired) this.yawnAt = now + rnd(1500, 3000); }
    if (this.yawnAt && now > this.yawnAt && (IDLE.has(id) || id === 'ask')) { this.yawnAt = 0; this.act('yawn', now, 1, 'alive'); }
    // alive: a long wait wears on her.
    if (id === 'ask' && Lf.alive && now > this.sighAt) { this.act('sigh', now, 1, 'alive'); this.sighAt = now + rnd(8000, 12000); }
    // alive: small moments when nothing else is going on
    if (Lf.alive && (IDLE.has(id) || id === 'ask') && now >= this.idleAt) {
      if (this.idleAt) this.idle(now, e, j);
      this.idleAt = now + rnd(5000, 11000) * lerp(1.6, .4, this.temper) * (1 + 1.2 * Math.max(0, .6 - e)) * (id === 'ask' ? 1.6 : 1);
    }
  }
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
    const was = this.expr;
    this.expr = id; this.t0 = now; this.fired = new Set();
    const x = EXPRESSIONS[id];
    for (const f of x.enter ?? []) this.effect(f, now);
    this.anticAt = now + rnd(6000, 14000) * (this.layers.alive ? lerp(1.8, .6, this.temper) : 1);
    if (x.blink) this.blinkAt = now + rnd(600, 2400);
    this.tiltAt = 0; this.talkFrom = -1; this.hushAt = -1; this.sayFrom = -1; this.sayHush = -1;
    this.checkAt = now + (id === '30' ? 3200 : id === 'ask' ? 2800 : 0); this.sighAt = now + 11000;
    if (!this.motion.blink || !was) return;
    // v2: falling asleep is slow; waking, or a face too far from the last to morph into, happens behind a blink (two when waking).
    if (SLEEPY.has(id)) { this.drowse = now + 1600; this.beat('慢慢闭眼'); return; }
    this.drowse = 0;
    const first = x.seq?.frames[0]?.eyes, lidded = !!first && ('lid' in first || 'lidR' in first);
    const waking = SLEEPY.has(was);
    if (lidded || !(waking || gapOf(shapeOf(was), shapeOf(id)) >= 1)) return;
    this.from = shapeOf(was); this.blinkStart = now; this.swapAt = now + 70; this.swapUntil = now + 300; this.twice = waking;
    this.beat(waking ? '醒来眨两下' : '眨眼换脸');
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
  gaze(x: Expr, ov: Frame | null, now: number, t: number, look: [number, number] | null, poi: Poi | null | undefined): [number, number, boolean] {
    const mode = x.gaze ?? 'free', gx0 = x.gx ?? 0, gy0 = ov?.gy ?? x.gy ?? 0, id = this.expr!;
    if (this.motion.lead && now >= this.microAt) { this.micro = [rnd(-.035, .035), rnd(-.025, .025)]; this.microAt = now + rnd(350, 900); }
    // attend: a glance she chose wins for its moment; while you talk she looks at you; a card holds her eyes between check-ins.
    if (this.focus && now < this.focus.until) { this.looking = this.focus.why; return [this.focus.g[0] + this.micro[0], this.focus.g[1] + this.micro[1], false]; }
    this.focus = null;
    if (this.layers.attend) {
      if (LISTEN.has(id) && now - this.heardAt < 1600) { this.looking = '看着你（你在说话）'; return [YOU[0] + .06 * this.tiltSide + this.micro[0], YOU[1] + this.micro[1], false]; }
      if (id === 'ask' && poi) { this.looking = poi.why; return [poi.g[0] + this.micro[0], poi.g[1] + this.micro[1], false]; }
    }
    this.looking = { free: '', still: '看前面', away: '害羞，看别处', up: '往上想', loop: '转着圈想', scan: '扫一遍', sweepY: '上下看', recall: '往回想', read: '边说边看' }[mode];
    if (mode === 'free') {
      const m = this.motion;
      const [mx, my] = m.lead ? this.micro : [0, 0];
      if (look && m.attend) {
        // v2: a moving cursor catches her eye; once it sits still for a few seconds she looks elsewhere, then checks back now and then.
        const a = this.attn;
        if (now >= a.checkAt) {
          const jump = Math.hypot(look[0] - a.last[0], look[1] - a.last[1]), moved = jump > .02;
          a.last = [look[0], look[1]]; a.checkAt = now + 60;
          if (moved && m.lead && jump > .5 && this.blinkStart < 0 && Math.random() < .6) { this.blinkStart = now; this.beat('远看顺便眨眼'); }
          if (moved) {
            if (!a.on) { a.on = true; a.since = now; this.perk(now); this.beat('注意到鼠标'); }
            a.bored = now + rnd(1800, 3400);
            // life: a cursor that never stops gets boring too
            if (this.layers.attend && now - a.since > rnd(6000, 9000)) a.bored = now;
          }
          else if (a.on && now > a.bored) { a.on = false; a.back = now + rnd(2600, 5600); this.sacAt = now + rnd(120, 360); this.beat('看腻了，看别处'); }
          else if (!a.on && now > a.back) { a.on = true; a.bored = now + rnd(700, 1200); this.beat('瞟你一眼'); }
        }
        if (a.on) { this.looking = '看鼠标'; return [look[0] + mx, look[1] + my, false]; }
      } else if (look) { this.looking = '看鼠标'; return [look[0], look[1], false]; }
      else this.attn.on = false;
      this.looking = '随便看看';
      if (now >= this.sacAt) {
        const far = Math.random() < .4, next: [number, number] = [rnd(-1, 1) * (far ? .55 : .18), rnd(-1, 1) * (far ? .3 : .1)];
        if (m.lead && this.blinkStart < 0 && Math.hypot(next[0] - this.sac[0], next[1] - this.sac[1]) > .5 && Math.random() < .6) { this.blinkStart = now; this.beat('远看顺便眨眼'); }
        this.sac = next; this.sacAt = now + rnd(700, 3300);
      }
      return [this.sac[0] + mx, this.sac[1] + my, !m.lead];
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
    // life: her mood glides to what the page asks; voice levels rise fast and fall slower
    const Lf = this.layers, mood = input.mood ?? NEUTRAL;
    step(this.mood.energy, Lf.mood ? mood.energy : NEUTRAL.energy, .5, 1, dt); step(this.mood.joy, Lf.mood ? mood.joy : NEUTRAL.joy, .5, 1, dt);
    const e = clamp(this.mood.energy.value, 0, 1), j = clamp(this.mood.joy.value, 0, 1), low = Math.max(0, .6 - e), high = Math.max(0, e - .6);
    const hear = Lf.voice ? input.hear ?? 0 : 0;
    this.hearK += (hear - this.hearK) * (1 - Math.exp(-(hear > this.hearK ? 25 : 7) * dt));
    const sayOn = Lf.voice && input.say !== undefined, say = sayOn ? input.say! : 0;
    this.sayK += (say - this.sayK) * (1 - Math.exp(-(say > this.sayK ? 30 : 10) * dt));
    const lean = LISTEN.has(id) && Lf.voice ? this.hearK : 0;
    const effort = id === '30' && Lf.alive ? smooth(0, 7000, el) : 0;
    let ov: Frame | null = null;
    if (x.seq && seq) for (const [i, f] of x.seq.frames.entries()) if (el >= f.at) {
      ov = f;
      if (f.fx && !this.fired.has(i)) { this.fired.add(i); this.effect(f.fx, now); }
    }
    const frozen = x.freeze && x.seq && el >= x.seq.end - 200;
    const calm = reduced.matches || frozen || input.still ? 0 : 1;
    if (calm) this.life(now, id, e, j, input.poi);
    let moving = false;
    // eyes
    let shape: Shape = this.motion.blink && now < this.swapAt && this.from ? this.from : { ...BASE, ...x.eyes, ...(ov?.eyes ?? {}) };
    // v2: the cursor resting right on her makes her squint happily.
    const near = this.motion.pet && (x.gaze ?? 'free') === 'free' && !!input.look && (!this.motion.attend || this.attn.on) && Math.hypot(input.look[0], input.look[1]) < .3;
    if (near !== this.petOn) { this.petOn = near; if (near) this.beat('摸头眯眼'); }
    moving = step(s.pet, near ? 1 : 0, 4, 1, dt) || moving;
    if (s.pet.value > .005) shape = { ...shape, cutB: Math.max(shape.cutB, .36 * s.pet.value), y: shape.y + .03 * s.pet.value };
    if (Lf.mood) shape = moodShape(shape, j);
    // alive: the longer she thinks, the harder she concentrates
    if (effort) shape = { ...shape, cut: Math.max(shape.cut, .16 * effort), cutT: 6 };
    const tg = eyeTargets(shape);
    let [eh, ed] = x.spring ?? (ov ? [9, .8] : [5.2, .62]);
    if (this.motion.blink && now >= this.swapAt && now < this.swapUntil) [eh, ed] = [18, 1];
    else if (this.motion.blink && now < this.drowse) [eh, ed] = [1.3, 1];
    for (let i = 0; i < 2; i++) for (const k of EYE_KEYS) moving = step(this.E[i][k], tg[i][k], k === 'a' ? 14 : eh, k === 'a' ? 1 : ed, dt) || moving;
    // gaze
    let [gx, gy, fast] = this.gaze(x, ov, now, t, input.look, input.poi);
    if (Lf.mood) gy += .22 * low; // tired eyes sink
    if (this.shakeAt >= 0) { const k = (now - this.shakeAt) / 800; if (k < 1) { gx = .55 * Math.sin(k * 3 * TAU) * (1 - k); fast = true; } else this.shakeAt = -1; }
    if (!calm && x.gaze !== 'still') { gx = 0; gy = 0; }
    if (this.motion.lead && !fast) {
      // v2: the eyes jump there first; her body and the stars inside turn after them.
      moving = step(s.ex, gx, 12, .9, dt) || moving; moving = step(s.ey, gy, 12, .9, dt) || moving;
      moving = step(s.gx, gx, 2.1, .95, dt) || moving; moving = step(s.gy, gy, 2.1, .95, dt) || moving;
    } else {
      moving = step(s.gx, gx, fast ? 9 : 3.2, .9, dt) || moving;
      moving = step(s.gy, gy, fast ? 9 : 3.2, .9, dt) || moving;
      Object.assign(s.ex, { value: s.gx.value, velocity: s.gx.velocity }); Object.assign(s.ey, { value: s.gy.value, velocity: s.gy.velocity });
    }
    const sway = x.sway ? x.sway[0] * Math.sin(t * TAU / x.sway[1]) : 0;
    const tilt = lean ? 5 * this.tiltSide * Math.min(1, this.hearK * 3) : 0;
    moving = step(s.head, shape.head + calm * (sway * (1 - .7 * lean) + tilt + 2.2 * Math.sin(t * .37 + this.seed)), 3, .8, dt) || moving;
    // blink: quick close, slower open, sometimes twice; the right eye a hair later
    if (x.blink && calm && this.blinkStart < 0 && now >= this.blinkAt && !input.pressed) this.blinkStart = now;
    let bl = 1, br = 1;
    if (this.blinkStart >= 0) {
      const k = (now - this.blinkStart) / 1000 * (x.blinkSlow ? .45 : 1) * (Lf.alive ? 1 - .9 * low : 1);
      bl = blinkCurve(k); br = blinkCurve(k - .022);
      if (k > .3) {
        this.blinkStart = -1; this.blinkAt = now + (Math.random() < .18 ? 150 : rnd(...(x.blink ?? [3000, 6000])) * (Lf.alive ? 1 + 1.5 * low - .4 * high : 1));
        if (this.twice) { this.twice = false; this.blinkStart = now + 110; }
      }
    }
    if (x.lidWave) { const w = Math.sin(t * TAU / x.lidWave); bl *= 1 - .92 * smooth(.1, .9, w); br *= 1 - .92 * smooth(.1, .9, -w); }
    // antics: a full turn (eyes go round the back) or a small hop
    if (x.antics && calm && now >= this.anticAt) {
      if (x.antics[Math.floor(Math.random() * x.antics.length)] === 'spin') this.effect('spin', now); else this.hop(now, .14);
      this.anticAt = now + rnd(9000, 18000) * (Lf.alive ? lerp(1.8, .6, this.temper) : 1);
    }
    this.changing(now);
    let yaw = 0;
    if (this.spinStart >= 0) { const k = (now - this.spinStart) / this.spinDur; if (k < 1) yaw = TAU * smooth(0, 1, k); else this.spinStart = -1; }
    // body: holding her down squashes her
    moving = step(s.stretch, input.pressed ? .86 : (x.tall ?? 1) * (x.sink ? .97 : 1), input.pressed ? 10 : 4.2, input.pressed ? .9 : .45, dt) || moving;
    moving = step(s.lift, x.sink ?? 0, 3, .8, dt) || moving;
    this.voiceK += ((x.voice ? 1 : 0) - this.voiceK) * (1 - Math.exp(-3 * dt));
    const phrase = (.5 + .5 * Math.sin(t * 1.7 - .8)) ** 2, syl = (.5 + .5 * Math.sin(t * 7.3 + 1.3 * Math.sin(t * 2.1))) ** 2;
    // voice: her real level moves her when there is one; otherwise the old made-up rhythm
    const env = sayOn ? this.sayK * this.voiceK : calm * this.voiceK * phrase * (.35 + .65 * syl);
    let hopY = 0, rise = 0;
    if (this.hopStart >= 0 && now >= this.hopStart) {
      const k = (now - this.hopStart) / this.hopDur;
      if (k >= 1) { this.hopStart = -1; s.stretch.velocity -= this.hopH > .3 ? 2.4 : 1.4; }
      else { hopY = -this.hopH * 4 * k * (1 - k); if (this.motion.hop) rise = .1 * Math.max(0, 1 - 2.2 * k); }
    }
    // v2: lights up for a moment (perk)
    const pk = this.perkAt >= 0 ? (now - this.perkAt) / 360 : -1;
    if (pk >= 1) this.perkAt = -1;
    const bump = pk >= 0 && pk < 1 ? Math.sin(pk * PI) : 0;
    let br0 = x.breathe ?? [.007, 4.2];
    if (Lf.alive) br0 = [br0[0] * (1 + 1.4 * low), br0[1] * (1 + .9 * low - .3 * high)];
    // react: short gestures add up on top of everything else
    let aGy = 0, aY = 0, aSt = 1, aLid = 1, aLen = 1, aHead = 0, aX = 0, aBr = 1, aCheek = 0, aJx = 0;
    for (const a of this.acts) {
      const k = (now - a.at) / a.dur;
      if (k >= 1 || k < 0) continue;
      const env1 = Math.sin(PI * k) * a.amp;
      if (a.kind === 'nod') { aGy += .22 * env1; aY += .035 * env1; aLid *= 1 - .22 * env1; }
      else if (a.kind === 'flinch') {
        const shr = k < .35 ? Math.sin(PI * k / .35) * a.amp : 0, wide = k > .3 ? Math.sin(PI * (k - .3) / .7) * a.amp : 0;
        aSt *= 1 - .13 * shr; aLid *= 1 - .75 * shr; aLen *= 1 + .14 * wide; aY -= .03 * wide; aBr *= 1 + .2 * wide;
      } else if (a.kind === 'wince') { aLid *= 1 - .45 * env1; aCheek = Math.max(aCheek, .3 * env1); aJx += .012 * env1 * Math.sin(now * .06); aSt *= 1 - .05 * env1; }
      else if (a.kind === 'bulge') aSt *= 1 + .075 * Math.sin(TAU * k) * (1 - k) * a.amp;
      else if (a.kind === 'wiggle') aHead += 8 * Math.sin(3 * TAU * k) * (1 - k) * a.amp;
      else if (a.kind === 'drift') { aX += .16 * a.dir * smooth(0, .35, k) * (1 - smooth(.6, 1, k)) * a.amp; aHead += 4 * a.dir * env1; }
      else if (a.kind === 'yawn') {
        const c = smooth(0, .3, k) * (1 - smooth(.7, 1, k)) * a.amp;
        aLid *= 1 - .8 * c; aCheek = Math.max(aCheek, .25 * c); aSt *= 1 + .08 * c; aGy -= .28 * c; aBr *= 1 - .18 * c;
      } else if (a.kind === 'sigh') {
        const inh = k < .45 ? Math.sin(PI * k / .45) : 0, exh = k > .4 ? Math.sin(PI * (k - .4) / .6) : 0;
        aSt *= 1 + (.04 * inh - .03 * exh) * a.amp; aBr *= 1 - .15 * exh * a.amp; aGy += .1 * exh * a.amp; aLid *= 1 - .2 * exh * a.amp;
      } else if (a.kind === 'glow') { aBr *= 1 + .35 * env1; aLen *= 1 + .06 * env1; }
    }
    this.acts = this.acts.filter(a => now - a.at < a.dur);
    const stretch = s.stretch.value * (1 + calm * br0[0] * Math.sin(t * TAU / br0[1]) + .028 * env) * (1 + rise) * aSt * (1 + .035 * lean);
    const bounce = x.bounce && calm ? -x.bounce[0] * Math.abs(Math.sin(PI * now / x.bounce[1])) : 0;
    const bob = calm * (x.bob ?? .03) * (Lf.alive ? .55 + .75 * e : 1) * Math.sin(t * TAU / 3.3 + this.seed);
    const trem = (ov?.tremble ?? x.tremble ?? 0) * (reduced.matches ? 0 : 1);
    const jx = trem * (Math.sin(t * 57.1) + Math.sin(t * 83.7 + 1.3)) * .5 + aJx;
    step(s.spinV, calm * (ov?.spin ?? x.spin ?? .22) * (Lf.mood ? .45 + .9 * e : 1) * (1 + .9 * effort), 1.2, 1, dt);
    this.spin += s.spinV.value * dt;
    // light
    // mood: joy warms her light, low joy or energy dims it; offline dims it further
    let L = LIGHT[input.deep ? 'deep' : ov?.light ?? x.light ?? 'base'];
    if (Lf.mood) {
      if (j > .55) L = blendLight(L, LIGHT.warm, (j - .55) * .8);
      L = blendLight(L, LIGHT.dim, Math.min(.6, Math.max(0, .45 - j) * .8 + Math.max(0, .4 - e) * .9));
    }
    if (input.dim) L = blendLight(L, LIGHT.dim, input.dim);
    const gap = mixLight(this.light, L, 1 - Math.exp(-(ov?.lightK ?? x.lightK ?? 5) * dt));
    let bt = (ov?.bright ?? x.bright ?? 1) * L.b * (1 + .25 * env) * (1 + .25 * bump) * (Lf.mood ? .82 + .3 * e : 1) * (1 + .2 * lean);
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
    // mood: heavy lids when she is tired, a little wider when she is lively
    const lidMul = Lf.mood ? 1 - 1.1 * low ** 1.5 : 1, lenMul = (Lf.mood ? 1 + .12 * high : 1) * aLen * (1 + .1 * lean);
    const [eL, eR] = this.E.map((sp, i) => {
      const o = Object.fromEntries(EYE_KEYS.map(k => [k, sp[k].value])) as Eye;
      o.lid = Math.max(.04, o.lid * (i ? br : bl) * (input.pressed ? .35 : 1) * lidMul * aLid);
      o.len = Math.max(0, o.len * (1 + .08 * env + .08 * bump) * lenMul); o.w = Math.max(.02, o.w * (1 + .16 * bump)); o.a = clamp(o.a, 0, 1);
      o.cutB = Math.max(o.cutB, aCheek);
      return o;
    });
    const phase = (at: number, ms: number) => { if (at < 0) return -1; const k = (now - at) / ms; return k < 1 ? k : -1; };
    this.rippleAt = phase(this.rippleAt, 900) < 0 ? -1 : this.rippleAt;
    this.flashAt = phase(this.flashAt, 420) < 0 ? -1 : this.flashAt;
    // v2: the eyes float in the glass: they trail her body (flight, hops) and settle with a small overshoot.
    let ox = 0, oy = 0;
    if (this.motion.lag) {
      const [vx, vy] = input.vel ?? [0, 0], body = hopY + bounce;
      moving = step(s.lagX, clamp(-vx * .012, -.1, .1), 7, .5, dt) || moving;
      moving = step(s.lagY, clamp(-vy * .012, -.1, .1), 7, .5, dt) || moving;
      moving = step(s.lagB, body, 9, .5, dt) || moving;
      ox = s.lagX.value; oy = s.lagY.value + s.lagB.value - body;
    }
    const eyes = eyeSet(eL, eR, s.ex.value, s.ey.value + aGy, yaw, s.head.value + aHead);
    if (ox || oy) for (const e of eyes) { e.c[0] += ox; e.c[1] += oy; for (const p of e.pts) { p[0] += ox; p[1] += oy; } }
    const leanOn = this.motion.lead ? 1 : 0;
    this.st = {
      L: eL, R: eR, head: s.head.value + aHead, gx: s.gx.value, gy: s.gy.value + .5 * aGy, yaw, t, env, lx: leanOn * .04 * s.gx.value, ly: leanOn * .025 * s.gy.value,
      sx: (1 + .012 * env) / Math.sqrt(stretch), sy: stretch, yOff: hopY + bob + bounce + s.lift.value + aY + .03 * lean, jx, dx: aX,
      spin: this.spin, bright: this.bright * aBr, blush: this.blush, orbitK: this.orbitK, voice: this.voiceK,
      eyes, ripple: phase(this.rippleAt, 900), flash: phase(this.flashAt, 420),
    };
    return !input.still || moving || gap > .004 || dim > .004 || this.blinkStart >= 0 || this.hopStart >= 0 || this.spinStart >= 0
      || this.shakeAt >= 0 || this.rippleAt >= 0 || this.flashAt >= 0 || this.fx.length > 0 || !!this.morph || seq || this.perkAt >= 0 || now < this.swapUntil
      || this.acts.length > 0 || !!this.focus || (Lf.alive && calm > 0);
  }
  // Her own motion on top of where she is: hop, tremble, breathing, squash. `k` fades it out inside the island.
  pose(k: number): [number, number, number, number] {
    const st = this.st;
    return [(st.jx + st.lx + st.dx) * k, (st.yOff + st.ly) * k, 1 + (st.sx - 1) * k, 1 + (st.sy - 1) * k];
  }
  // Renders both GL layers for this frame at S device pixels; false without WebGL2.
  render(S: number, q: number) {
    const gl = GL();
    if (!gl) return false;
    const st = this.st, L = this.light, [eL, eR] = st.eyes;
    const spill = (e: EyePose): RGB => [e.c[0], e.c[1], e.a * clamp(e.lid, 0, 1) * (.75 + .6 * st.env) * Math.min(1.3, st.bright + .2)];
    // How far her light is from rest decides how much the painted sky takes on its hue.
    let gap = 0;
    for (let j = 0; j < 3; j++) for (let i = 0; i < 3; i++) gap = Math.max(gap, Math.abs(L.n[j][i] - LIGHT.base.n[j][i]));
    const avg = [0, 1, 2].map(i => (L.n[0][i] + L.n[1][i] + L.n[2][i]) / 3), top = Math.max(...avg, 1e-3);
    gl.render(S, { t: st.t, q, rot: rotMat(-(st.spin + st.gx * .5 + st.yaw), .3 + st.gy * .4), n: L.n, rim: L.rim, eyeC: L.glow, eyeL: spill(eL), eyeR: spill(eR), bright: st.bright,
      texA: -st.spin * .3 + this.texA, texO: [st.gx * .06, st.gy * .06], tint: clamp(gap * 3, 0, .8), tintC: avg.map(c => c / top) as RGB }, this.mat);
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

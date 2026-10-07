import { COLOR, type MarkLook } from './AgentMarks';

// The "your turn" icon beside the notch (ADR 0057), from the notch lab's icon library: its own star, in a colour no
// session state uses. Allen picked 信标 in 幻彩; the other three icons and colours stay here for a later pick.
export type TurnIcon = 'beacon' | 'twin' | 'comet' | 'ring';
export type TurnHue = 'iris' | 'fuchsia' | 'aqua' | 'gold';
export const TURN_ICON: TurnIcon = 'beacon', TURN_HUE: TurnHue = 'iris';

type C3 = [number, number, number];
const HUES: Record<Exclude<TurnHue, 'iris'>, C3> = { fuchsia: [255, 95, 205], aqua: [90, 225, 235], gold: [255, 236, 190] };
// 幻彩 drifts through pink, gold and ice blue.
const IRIS: C3[] = [[255, 110, 200], [255, 214, 150], [120, 220, 255]];
export const rgba = (c: C3, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
export const tint = (c: C3, k: number) => c.map(v => Math.round(v + (255 - v) * k)) as C3;
export function hueAt(t: number, hue: TurnHue = TURN_HUE): C3 {
  if (hue !== 'iris') return HUES[hue];
  const x = (t * .18) % 1 * 3, i = Math.floor(x), f = x - i, a = IRIS[i % 3], b = IRIS[(i + 1) % 3];
  return a.map((v, k) => Math.round(v + (b[k] - v) * f)) as C3;
}
function fillOf(c: CanvasRenderingContext2D, t: number, r: number, hue: TurnHue): string | CanvasGradient {
  if (hue !== 'iris') return rgba(tint(HUES[hue], .35));
  const a = t * .7, g = c.createLinearGradient(-Math.cos(a) * r, -Math.sin(a) * r, Math.cos(a) * r, Math.sin(a) * r);
  IRIS.forEach((col, k) => g.addColorStop(k / 2, rgba(tint(col, .25))));
  return g;
}
function star4(c: CanvasRenderingContext2D, ro: number, ri: number, rot = 0, x = 0, y = 0) {
  c.beginPath();
  for (let i = 0; i < 8; i++) { const r = i % 2 ? ri : ro, a = rot + i * Math.PI / 4 - Math.PI / 2; c.lineTo(x + Math.cos(a) * r, y + Math.sin(a) * r); }
  c.closePath();
}
// Each draws in world units round the origin (a session star is about 11 across); `glow` is 0 for the pixel copy.
// `quiet` keeps the beacon's rings in: they only say something arrived, for a few seconds.
const ICONS: Record<TurnIcon, (c: CanvasRenderingContext2D, t: number, glow: number, hue: TurnHue, quiet: boolean) => void> = {
  // 信标: an eight-point star, long and short rays, sending out two rings in turn.
  beacon: (c, t, glow, hue, quiet) => {
    const col = hueAt(t, hue);
    if (!quiet) for (const off of [0, .5]) {
      const ph = (t / 1.6 + off) % 1;
      c.shadowBlur = 0; c.strokeStyle = rgba(col, .75 * (1 - ph)); c.lineWidth = .8; c.beginPath(); c.arc(0, 0, 4 + ph * 5.5, 0, Math.PI * 2); c.stroke();
    }
    c.shadowColor = rgba(col, .95); c.shadowBlur = 6 * glow; c.fillStyle = fillOf(c, t, 6, hue);
    c.beginPath();
    for (let i = 0; i < 16; i++) { const r = i % 2 ? 1.2 : (i / 2) % 2 ? 3.1 : 5.9, a = i * Math.PI / 8 - Math.PI / 2; c.lineTo(Math.cos(a) * r, Math.sin(a) * r); }
    c.closePath(); c.fill();
  },
  // 双星: a big four-point sparkle and a small one that twinkles on its own beat.
  twin: (c, t, glow, hue) => {
    const col = hueAt(t, hue);
    c.shadowColor = rgba(col, .9); c.shadowBlur = 6 * glow; c.fillStyle = fillOf(c, t, 6, hue);
    star4(c, 5.6, 1.35, 0, -1.2, 1.2); c.fill();
    const k = .55 + .45 * Math.max(0, Math.sin(t * 3.1));
    c.fillStyle = rgba(tint(col, .55)); star4(c, 2.9 * k, .75 * k, 0, 4.6, -4.4); c.fill();
  },
  // 流星: a bright head with a tail sweeping back towards the notch, a few sparks drifting off it.
  comet: (c, t, glow, hue) => {
    const col = hueAt(t, hue), hx = 2.6, hy = -1.8, tx = -8.5, ty = 4.4, len = .85 + .15 * Math.sin(t * 2.3);
    const g = c.createLinearGradient(hx, hy, hx + (tx - hx) * len, hy + (ty - hy) * len);
    g.addColorStop(0, rgba(col, .95)); g.addColorStop(1, rgba(col, 0));
    const nx = -(ty - hy), ny = tx - hx, nl = Math.hypot(nx, ny), w = 2.1;
    c.fillStyle = g; c.beginPath();
    c.moveTo(hx + nx / nl * w, hy + ny / nl * w); c.lineTo(hx + (tx - hx) * len, hy + (ty - hy) * len); c.lineTo(hx - nx / nl * w, hy - ny / nl * w); c.closePath(); c.fill();
    for (let i = 0; i < 3; i++) {
      const p = (t * .6 + i / 3) % 1, x = hx + (tx - hx) * p + Math.sin(t * 4 + i) * .7, y = hy + (ty - hy) * p - 1.2 * p;
      c.fillStyle = rgba(tint(col, .5), .8 * (1 - p)); c.beginPath(); c.arc(x, y, .55, 0, Math.PI * 2); c.fill();
    }
    c.shadowColor = rgba(col, .95); c.shadowBlur = 7 * glow; c.fillStyle = 'rgb(255,250,240)';
    star4(c, 4, 1.2, 0, hx, hy); c.fill();
  },
  // 星环: a star inside a tilted ring, a spark going round the front of it.
  ring: (c, t, glow, hue) => {
    const col = hueAt(t, hue), tilt = -.38, a = t * 2.1;
    const ring = (from: number, to: number, alpha: number) => { c.strokeStyle = rgba(tint(col, .2), alpha); c.lineWidth = 1; c.beginPath(); c.ellipse(0, 0, 7.6, 2.5, tilt, from, to); c.stroke(); };
    c.shadowBlur = 0; ring(Math.PI, Math.PI * 2, .45);
    c.shadowColor = rgba(col, .9); c.shadowBlur = 6 * glow; c.fillStyle = fillOf(c, t, 5, hue); star4(c, 4.8, 1.3, 0); c.fill();
    c.shadowBlur = 3 * glow; ring(0, Math.PI, .95);
    if (Math.sin(a) > 0) {
      const x = Math.cos(a) * 7.6, y = Math.sin(a) * 2.5, rx = x * Math.cos(tilt) - y * Math.sin(tilt), ry = x * Math.sin(tilt) + y * Math.cos(tilt);
      c.fillStyle = 'rgb(255,250,240)'; c.beginPath(); c.arc(rx, ry, .9, 0, Math.PI * 2); c.fill();
    }
  },
};
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
export const DOT_RING_S = 1.2;
// The pixel look: the same icon drawn at 11 px, its alpha snapped, then blown up without smoothing.
let pix: CanvasRenderingContext2D | null = null;
export function drawTurnIcon(c: CanvasRenderingContext2D, look: MarkLook, t: number, since: number, px: number, quiet = false, icon = TURN_ICON, hue = TURN_HUE) {
  if (reduced.matches) { t = 1.2; since = 99; }
  // A new arrival makes it jump once.
  const pop = since < .5 ? 1 + .6 * (1 - since / .5) : 1;
  c.save(); c.scale(pop, pop); c.lineCap = 'round'; c.lineJoin = 'round';
  // 点线环 keeps the turn in the needs-you amber: the point, which sends out two rings as something arrives and then
  // stays still.
  if (look === 'dot') {
    const col = COLOR.wait;
    if (!quiet && since < 2 * DOT_RING_S) { const ph = (since % DOT_RING_S) / DOT_RING_S; c.strokeStyle = rgba(col, .7 * (1 - ph)); c.lineWidth = .8; c.beginPath(); c.arc(0, 0, 3.6 + ph * 4.5, 0, Math.PI * 2); c.stroke(); }
    c.shadowColor = rgba(col, .85); c.shadowBlur = 3 * px; c.fillStyle = rgba(tint(col, .15));
    c.beginPath(); c.arc(0, 0, 3.2, 0, Math.PI * 2); c.fill();
  } else if (look === 'pixel') {
    if (!pix) { const cv = document.createElement('canvas'); cv.width = cv.height = 11; pix = cv.getContext('2d', { willReadFrequently: true })!; }
    pix.setTransform(1, 0, 0, 1, 0, 0); pix.clearRect(0, 0, 11, 11);
    pix.setTransform(1 / 1.85, 0, 0, 1 / 1.85, 5.5, 5.5); ICONS[icon](pix, t, 0, hue, quiet);
    const img = pix.getImageData(0, 0, 11, 11);
    for (let i = 3; i < img.data.length; i += 4) img.data[i] = img.data[i] > 90 ? 255 : 0;
    pix.putImageData(img, 0, 0);
    c.imageSmoothingEnabled = false; c.shadowColor = rgba(hueAt(t, hue), .8); c.shadowBlur = 4 * px;
    c.drawImage(pix.canvas, -5.5 * 1.85, -5.5 * 1.85, 11 * 1.85, 11 * 1.85);
  } else ICONS[icon](c, t, px, hue, quiet);
  c.restore();
}

// 月亮: where parked sessions wait (先放着). A crescent with a small star beside it, quiet: no rings, no drifting
// colour, at a session star's weight.
const MOON: C3 = [206, 216, 255];
export const MOON_RGB = MOON.join(',');
export function drawMoon(c: CanvasRenderingContext2D, t: number, px: number) {
  if (reduced.matches) t = 1.2;
  c.save();
  c.beginPath(); c.rect(-12, -12, 24, 24); c.arc(1.9, -1.7, 3.5, 0, Math.PI * 2); c.clip('evenodd');
  c.shadowColor = rgba(MOON, .6); c.shadowBlur = 4 * px; c.fillStyle = rgba(tint(MOON, .15), .9);
  c.beginPath(); c.arc(-.5, .5, 4.2, 0, Math.PI * 2); c.fill();
  c.restore();
  const k = .5 + .5 * Math.max(0, Math.sin(t * 1.3));
  c.fillStyle = rgba(tint(MOON, .5), .45 + .5 * k); star4(c, 1.6 * (.7 + .3 * k), .45, 0, 4.1, -3.9); c.fill();
}

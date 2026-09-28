// The refined notch finish from the approved round-two study: fitted diffuse light,
// sharp stars and light along the lower edge. Its backdrop stays the island's black.
import { B, type Core } from './starCore';

export type Rect = { l: number; t: number; r: number; b: number };
export type HomeFinish = 'original' | 'refined';
const neb = document.createElement('canvas'), buf = document.createElement('canvas');
const rgb = (v: number[]) => v.map(u => Math.round(u * 255)).join(',');

// Her inside faded to no edge, `N` device pixels square.
function inside(core: Core, S: number) {
  const N = Math.min(S, 320), nc = neb.getContext('2d')!;
  if (neb.width !== N) neb.width = neb.height = N;
  nc.setTransform(1, 0, 0, 1, 0, 0); nc.clearRect(0, 0, N, N); nc.setTransform(1, 0, 0, 1, N / 2, N / 2);
  core.inside(nc, N / 2 / B, S);
  nc.globalCompositeOperation = 'destination-in';
  const m = nc.createRadialGradient(0, 0, 0, 0, 0, N / 2);
  m.addColorStop(0, '#000'); m.addColorStop(.3, 'rgba(0,0,0,.92)'); m.addColorStop(.62, 'rgba(0,0,0,.4)'); m.addColorStop(.9, 'rgba(0,0,0,0)');
  nc.fillStyle = m; nc.fillRect(-N / 2, -N / 2, N, N); nc.globalCompositeOperation = 'source-over';
  return neb;
}

export type HomePaint = { x: number; y: number; R: number; scale: number; inside: number; rect: Rect; path: Path2D; d: number; S: number; now: number };
export function paintRefinedHome(c: CanvasRenderingContext2D, core: Core, o: HomePaint) {
  if (o.inside < .01) return;
  const rim = rgb(core.light.rim), { x, y, rect: h } = o;
  // refined: build the glow in a buffer the size of the pocket, then fit it to the pocket with an elliptical fade
  const n = inside(core, o.S), d = o.d, hw = h.r - h.l, hh = h.b - h.t, r = B * o.R * o.scale * 1.95;
  const W = Math.max(1, Math.ceil(hw * d)), H = Math.max(1, Math.ceil(hh * d));
  if (buf.width !== W || buf.height !== H) { buf.width = W; buf.height = H; }
  const b = buf.getContext('2d')!;
  b.setTransform(1, 0, 0, 1, 0, 0); b.clearRect(0, 0, W, H); b.setTransform(d, 0, 0, d, -h.l * d, -h.t * d);
  // diffused through the glass, then the stars again, sharp and faint, so it glitters instead of blurring
  b.filter = `blur(${(1.1 * d).toFixed(1)}px)`; b.globalAlpha = 1; b.drawImage(n, x - r, y - r, 2 * r, 2 * r);
  b.globalCompositeOperation = 'lighter'; b.globalAlpha = .45; b.drawImage(n, x - r, y - r, 2 * r, 2 * r); b.filter = 'none';
  b.globalAlpha = .6; b.drawImage(n, x - r, y - r, 2 * r, 2 * r);
  // fitted to the pocket: an ellipse round her that fades out before any edge, so nothing is cut flat
  b.globalCompositeOperation = 'destination-in'; b.globalAlpha = 1;
  const ex = x, ey = Math.min(Math.max(y, h.t + hh * .45), h.b - hh * .45), rx = Math.min(hw * .62, r * 1.05), ry = hh * .6;
  b.save(); b.translate(ex, ey); b.scale(1, ry / rx);
  const m = b.createRadialGradient(0, 0, 0, 0, 0, rx); m.addColorStop(0, '#000'); m.addColorStop(.55, 'rgba(0,0,0,.95)'); m.addColorStop(.8, 'rgba(0,0,0,.5)'); m.addColorStop(1, 'rgba(0,0,0,0)');
  b.fillStyle = m; b.fillRect(-rx, -rx, 2 * rx, 2 * rx); b.restore();
  b.globalCompositeOperation = 'source-over';
  c.save(); c.clip(o.path);
  // Only her nebula emits light. Tinting the whole pocket paints a separate
  // grey-blue rectangle over the black shared by the notch and its wings.
  c.globalAlpha = o.inside;
  c.drawImage(buf, h.l, h.t, hw, hh);
  // the lower edge catches her light, brightest right under her; a softer band sits just above it
  const at = (u: number) => Math.min(1, Math.max(0, (u - h.l) / hw));
  const g = c.createLinearGradient(h.l, 0, h.r, 0);
  g.addColorStop(0, `rgba(${rim},0)`); g.addColorStop(at(x - 80), `rgba(${rim},0)`); g.addColorStop(at(x), `rgba(${rim},.85)`); g.addColorStop(at(x + 80), `rgba(${rim},0)`); g.addColorStop(1, `rgba(${rim},0)`);
  c.strokeStyle = g; c.lineWidth = .9; c.beginPath(); c.moveTo(h.l, h.b - .55); c.lineTo(h.r, h.b - .55); c.stroke();
  c.globalAlpha = o.inside * .3; c.lineWidth = 3.5; c.beginPath(); c.moveTo(h.l, h.b - 2.2); c.lineTo(h.r, h.b - 2.2); c.stroke();
  c.restore();
}

// While she is out, the pocket keeps a little warmth where she was, and after a while only a pilot light.
export function paintAway(c: CanvasRenderingContext2D, rgbGlow: string, rect: Rect, path: Path2D, anchor: { x: number; y: number }, warm: number, t: number) {
  c.save(); c.clip(path);
  if (warm > .01) {
    const g = c.createRadialGradient(anchor.x, anchor.y, 0, anchor.x, anchor.y, (rect.r - rect.l) * .45);
    g.addColorStop(0, `rgba(${rgbGlow},${.16 * warm})`); g.addColorStop(1, `rgba(${rgbGlow},0)`);
    c.fillStyle = g; c.fillRect(rect.l, rect.t, rect.r - rect.l, rect.b - rect.t);
  }
  const a = .35 + .25 * Math.sin(t * 1.3);
  c.fillStyle = `rgba(${rgbGlow},${a})`; c.shadowColor = `rgba(${rgbGlow},.9)`; c.shadowBlur = 6;
  c.beginPath(); c.arc(anchor.x, anchor.y + 1, 1.1, 0, Math.PI * 2); c.fill();
  c.restore();
}

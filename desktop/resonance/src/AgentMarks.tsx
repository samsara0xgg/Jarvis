import { useEffect, useRef } from 'react';

// Agent status marks in the two looks Allen picked from the notice lab: 星芒, a four-point star (the 星 in her
// name), and 像素, dot-matrix glyphs. The stars beside the notch, their panels, the home Agents row and the
// Agents page all draw the same mark. A mark draws in world units centred on the origin (1 unit = 1 pt at the
// notch); `px` is device pixels per unit, for the glow; `since` is seconds in this state.
export type MarkLook = 'spark' | 'pixel';
export const isMarkLook = (value: unknown): value is MarkLook => value === 'spark' || value === 'pixel';
// work = working, pack = compacting its context, wait = needs you, done = finished and not looked at yet,
// err = stopped on an error, seen = finished and looked at.
export type MarkState = 'work' | 'pack' | 'wait' | 'done' | 'err' | 'seen';
type C3 = [number, number, number];
export const COLOR: Record<MarkState, C3> = { work: [108, 156, 255], pack: [187, 148, 255], wait: [255, 201, 143], done: [111, 224, 180], err: [255, 106, 90], seen: [170, 178, 210] };

const TAU = Math.PI * 2;
const rgba = (c: C3, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
const tint = (c: C3, k: number) => c.map(v => Math.round(v + (255 - v) * k)) as C3;
const easeBack = (k: number) => { const s = 1.8; k -= 1; return 1 + k * k * ((s + 1) * k + s); };
const flick = (t: number) => Math.sin(t * 17) > .55 && Math.sin(t * 2.3) > 0 ? .35 : 1;
const halo = (ctx: CanvasRenderingContext2D, c: C3, px: number, blur: number) => { ctx.shadowColor = rgba(c, .85); ctx.shadowBlur = blur * px; };
function star(ctx: CanvasRenderingContext2D, ro: number | number[], ri: number, rot: number, x = 0, y = 0) {
  ctx.beginPath();
  for (let i = 0; i < 8; i++) {
    const r = i % 2 ? ri : Array.isArray(ro) ? ro[i >> 1] : ro, a = rot + i * Math.PI / 4 - Math.PI / 2;
    ctx.lineTo(x + Math.cos(a) * r, y + Math.sin(a) * r);
  }
  ctx.closePath();
}

// Pixel glyphs, 1.85 units a pixel.
const P = 1.85;
type Cell = [number, number];
const bitmap = (rows: string[]) => rows.flatMap((r, y) => [...r].flatMap((ch, x): Cell[] => ch === '#' ? [[x - (r.length - 1) / 2, y - (rows.length - 1) / 2]] : []));
const QMARK = bitmap(['.###.', '#...#', '....#', '...#.', '..#..', '.....', '..#..']);
// Left to right, so the tick draws itself in order.
const CHECK = bitmap(['......#', '.....#.', '#...#..', '.#.#...', '..#....']).sort((a, b) => a[0] - b[0]);
const BANG = bitmap(['##', '##', '##', '##', '..', '##']);
const box = (n: number) => { const o: Cell[] = []; for (let x = -n; x <= n; x++) for (let y = -n; y <= n; y++) if (Math.max(Math.abs(x), Math.abs(y)) === n) o.push([x, y]); return o; };
const BOXES: Cell[][] = [box(2), box(1), [[0, 0]], box(1)];
const RING: Cell[] = [];
for (let i = -2; i <= 2; i++) RING.push([i, -2]);
for (let i = -1; i <= 2; i++) RING.push([2, i]);
for (let i = 1; i >= -2; i--) RING.push([i, 2]);
for (let i = 1; i >= -1; i--) RING.push([-2, i]);
function cells(ctx: CanvasRenderingContext2D, list: Cell[], c: C3, a = 1) {
  ctx.fillStyle = rgba(tint(c, .3), a);
  for (const [x, y] of list) ctx.fillRect(x * P - P / 2 + .12, y * P - P / 2 + .12, P - .24, P - .24);
}

type Draw = (ctx: CanvasRenderingContext2D, st: MarkState, c: C3, t: number, since: number, px: number) => void;
const DRAW: Record<MarkLook, Draw> = {
  // Working: turns slowly with a satellite going round; compacting: shrinks to a point and spins; needs you:
  // a ring of light keeps leaving it; finished: flares once into an eight-point star; stopped: a point broken
  // off, trembling; looked at: a small still grey star.
  spark: (ctx, st, c, t, since, px) => {
    if (st === 'seen') { ctx.fillStyle = rgba(c, .5); star(ctx, 4.2, 1.2, 0); ctx.fill(); return; }
    halo(ctx, c, px, 5); ctx.fillStyle = rgba(tint(c, .6));
    if (st === 'work') {
      star(ctx, 5.2 * (.86 + .14 * Math.sin(t * 5)), 1.4, t * 1.1); ctx.fill();
      const a = t * 3.2;
      ctx.fillStyle = rgba(c, Math.sin(a) < 0 ? .45 : 1); ctx.beginPath(); ctx.arc(Math.cos(a) * 6.8, Math.sin(a) * 2.4, .85, 0, TAU); ctx.fill();
    } else if (st === 'pack') {
      const k = .5 + .5 * Math.cos(t * 3.9), ro = 1.6 + 3.8 * k; star(ctx, ro, Math.min(ro * .8, 1.4), t * (1.5 + 4 * (1 - k))); ctx.fill();
    } else if (st === 'err') {
      ctx.globalAlpha = flick(t); ctx.translate(Math.sin(t * 50) * .3, 0); star(ctx, [5.2, 5.2, 2, 5.2], 1.4, 0); ctx.fill();
    } else if (st === 'wait') {
      const ph = (t % 2.4) / 2.4; star(ctx, 5, 1.45, 0, 0, Math.sin(t * 2.6) * .6); ctx.fill();
      ctx.shadowBlur = 0; ctx.strokeStyle = rgba(c, .85 * (1 - ph)); ctx.lineWidth = .8; ctx.beginPath(); ctx.arc(0, 0, 3.5 + ph * 4.5, 0, TAU); ctx.stroke();
    } else {
      const b = since < .6 ? 1 + .7 * (1 - since / .6) : 1, sh = .92 + .08 * Math.sin(t * 3);
      star(ctx, 5.4 * b * sh, 1.3, 0); ctx.fill(); ctx.globalAlpha = .8; star(ctx, 3.4 * b, 1, Math.PI / 4); ctx.fill();
    }
  },
  // Working: three cells chase round a square; compacting: squares close in on the centre; needs you: a
  // blinking question mark; finished: a tick that draws itself; stopped: an exclamation mark; looked at: the
  // same tick, grey and still.
  pixel: (ctx, st, c, t, since, px) => {
    if (st === 'seen') { cells(ctx, CHECK, c, .45); return; }
    halo(ctx, c, px, 3.5);
    if (st === 'work') { cells(ctx, RING, c, .14); const h = Math.floor(t * 14) % 16; [1, .6, .3].forEach((a, k) => cells(ctx, [RING[(h - k + 16) % 16]], c, a)); }
    else if (st === 'pack') cells(ctx, BOXES[Math.floor(t * 3.3) % 4], c);
    else if (st === 'err') cells(ctx, BANG, c, flick(t));
    else if (st === 'wait') cells(ctx, QMARK, c, t % 1.1 < .75 ? 1 : .3);
    else cells(ctx, CHECK.slice(0, since < .5 ? Math.ceil(CHECK.length * since / .5) : CHECK.length), c);
  },
};
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
export function drawMark(ctx: CanvasRenderingContext2D, look: MarkLook, st: MarkState, t: number, since: number, px: number) {
  if (reduced.matches) { t = 1.2; since = 99; }
  ctx.save(); ctx.shadowBlur = 0; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  // A new mark, or one that changed state, pops in.
  if (since < .46 && st !== 'seen') { const k = easeBack(Math.max(.01, since / .46)); ctx.scale(k, k); }
  DRAW[look](ctx, st, COLOR[st], t, since, px);
  ctx.restore();
}
export const seedOf = (key: string) => [...key].reduce((a, ch) => a + ch.charCodeAt(0), 0) * .37 % 7;

// One clock for every mark on screen, about 30 frames a second. A painter says whether it drew; with nothing
// visible the clock only checks back every 100 ms.
type Painter = (now: number) => boolean;
const painters = new Set<Painter>();
let pending = false;
const schedule = (ms: number) => {
  if (pending || !painters.size) return;
  pending = true;
  setTimeout(() => requestAnimationFrame(now => { pending = false; let drew = false; painters.forEach(p => { drew = p(now) || drew; }); schedule(drew ? 30 : 100); }), ms);
};
export function useClock(paint: Painter) {
  const latest = useRef(paint);
  latest.current = paint;
  useEffect(() => {
    const painter: Painter = now => latest.current(now);
    painters.add(painter); schedule(0);
    return () => { painters.delete(painter); };
  }, []);
}
export const dpr = () => Math.min(2, devicePixelRatio || 1);
export const shown = (el: HTMLElement) => el.checkVisibility({ opacityProperty: true, visibilityProperty: true });

// One mark at `size` css px; its canvas is larger than its box so the glow is not cut off.
export function AgentMark({ look, state, id = '', size = 14 }: { look: MarkLook; state: MarkState; id?: string; size?: number }) {
  const ref = useRef<HTMLCanvasElement>(null), since = useRef({ state, at: performance.now() });
  if (since.current.state !== state) since.current = { state, at: performance.now() };
  const box = size * 1.6;
  useClock(now => {
    const cv = ref.current;
    if (!cv || !shown(cv)) return false;
    const d = dpr(), w = Math.round(box * d), k = size / 16;
    if (cv.width !== w) cv.width = cv.height = w;
    const ctx = cv.getContext('2d')!;
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, w, w);
    ctx.setTransform(d * k, 0, 0, d * k, w / 2, w / 2);
    drawMark(ctx, look, state, now / 1000 + seedOf(id), (now - since.current.at) / 1000, d * k);
    return true;
  });
  return <span className="agent-mark" style={{ position: 'relative', display: 'inline-block', flex: 'none', width: size, height: size }}>
    <canvas ref={ref} data-look={look} data-state={state} aria-hidden="true"
      style={{ position: 'absolute', left: (size - box) / 2, top: (size - box) / 2, width: box, height: box, pointerEvents: 'none' }}/>
  </span>;
}

// A mark's cell in the row beside the notch, and the room a count takes after it.
export const CELL: Record<MarkLook, number> = { spark: 16, pixel: 17 }, COUNT_W = 9;

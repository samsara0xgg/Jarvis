// Canvas paths, colours and reveal timing ported from the approved B01 publication.
import { COL, glyph, rgba, tint } from './glyph';
import { clamp } from './motion';
import { activityAt, type Trail } from './timeline';
export type Geo = {
  width: number; x0: number; x1: number; span: number; cell: number; rowH: number; band: [number, number]; minutes: boolean;
  y(i: number): number; yx(i: number, x: number): number; top: number; bottom: number;
  xOf(t: number): number; tOf(x: number): number; guides: number[]; gaps?: { a: number; b: number }[];
  // How far the sky is slid right to show older time, and how far it can go (0: it all fits).
  pan: number; panMax: number;
};
type DrawOptions = {
  now: number; p: number; geo: Geo; dev: number; sel?: number; pt?: number; span?: [number, number];
  a?: number; thick?: number; focus?: number; base?: number; ndx?: number; cy?: number; nm?: boolean;
  conn?: { x: number; y: number; x2: number; y2: number; a?: number } | null;
  // The times you were away (minutes; b null while you still are), drawn as the away line.
  aways?: { a: number; b: number | null }[];
  // The words' card: the lines that cross the sky (guides, the needle, the away line) pass behind it, not through it.
  card?: { x: number; y: number; w: number; h: number } | null;
};
// Everything but the card, for what would otherwise run through its words.
const besides = (c: CanvasRenderingContext2D, g: Geo, card: DrawOptions['card']) => {
  if (!card) return;
  c.beginPath(); c.rect(0, 0, g.width, g.bottom + 40); c.roundRect(card.x, card.y, card.w, card.h, 12); c.clip('evenodd');
};
// How long ago, in the axis's words: minutes, then hours, then days.
export const ago = (m: number) => m < .5 ? '现在' : m < 60 ? `${Math.round(m)} 分前` : m < 600 ? `${+(m / 60).toFixed(1)} 小时前`
  : m < 1440 ? `${Math.round(m / 60)} 小时前` : `${+(m / 1440).toFixed(1)} 天前`;
// How long you were away, in the same words.
const gone = (m: number) => m < 60 ? `${Math.max(1, Math.round(m))} 分` : m < 1440 ? `${+(m / 60).toFixed(m < 600 ? 1 : 0)} 小时` : `${+(m / 1440).toFixed(1)} 天`;
const UI = '-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif';
const pl = (c: CanvasRenderingContext2D, a: number, b: number, f: (x: number) => number) => {
  c.moveTo(a, f(a)); for (let x = a + 4; x < b; x += 4) c.lineTo(x, f(x)); c.lineTo(b, f(b));
};
// The ruler's marks, in minutes ago. Every cell between two marks is equally wide and time runs evenly inside it, so a
// cell near now holds minutes and one far back holds hours or days: recent time spreads out, a long history still fits.
const MARKS = [5, 15, 30, 60, 120, 240, 480, 1440, 2880, 5760, 11520, 23040, 43200];
// recent: how far back the sky opens (the stretch you have been working in); all: how far back any session goes. Each
// step of the ruler is a cell as wide as the next, and the recent stretch fills the sky from its oldest moment, not from
// the next mark past it; older time goes on past the left edge in cells as wide, and the sky slides right (pan) to it.
export function geometry(width: number, now: number, recent: number, all: number, height: number, bend: (i: number, x: number) => number, pan = 0): Geo {
  const x0 = 30, x1 = width - 262, range = x1 - x0, ladder = [0, ...MARKS], reach = Math.max(all, recent);
  // How many cells back a moment is: whole cells to the mark before it, and the part of the next.
  const cells = (m: number) => {
    let i = 1; while (i < ladder.length - 1 && m > ladder[i]) i++;
    return i - 1 + (m - ladder[i - 1]) / (ladder[i] - ladder[i - 1]);
  };
  const cell = range / cells(recent), extent = cell * cells(reach), panMax = Math.max(0, extent - range);
  pan = clamp(pan, 0, panMax);
  const xAgo = (m: number) => x1 + pan - cell * cells(clamp(m, 0, reach));
  const agoX = (x: number) => {
    const k = clamp((x1 + pan - x) / cell, 0, cells(reach)), i = Math.min(ladder.length - 2, Math.floor(k));
    return ladder[i] + (ladder[i + 1] - ladder[i]) * (k - i);
  };
  return { width, x0, x1, span: reach, cell, rowH: 27, band: [18, width], minutes: true,
    y: i => 82 + i * 27, yx: (i, x) => 82 + i * 27 + bend(i, x), top: 56, bottom: 56 + height - 22,
    xOf: at => xAgo(now - at), tOf: x => now - agoX(x), guides: MARKS.filter(m => m <= reach), pan, panMax,
  };
}
// 离开线: while you were away. A thin bracket just above the time axis, from the moment you left to the moment you came
// back, with how long in a break of its line; it is a note on the time, so nothing in the sky above it is touched.
function awayLines(c: CanvasRenderingContext2D, aways: { a: number; b: number | null }[], g: Geo, dev: number, now: number) {
  for (const aw of aways) {
    const a = Math.max(aw.a, now - g.span), b = Math.min(aw.b ?? now, now);
    if (b <= a) continue;
    const xa = g.xOf(a), xb = g.xOf(b);
    if (xb - xa < 1 || xb < dev) continue;
    // The left end shows only once the exposure has developed that far back.
    const x0 = Math.max(dev, xa), y0 = g.bottom - 10, open = aw.b === null, edge = xa >= dev;
    const label = `你不在 · ${gone(b - aw.a)}`;
    c.font = `500 10px ${UI}`; c.textBaseline = 'middle';
    // The words sit on the bracket, in a break of its line, when they fit; else just left of it.
    const w = c.measureText(label).width, inside = xb - x0 > w + 24, mid = (x0 + xb) / 2;
    c.lineWidth = 1; c.strokeStyle = 'rgba(214,224,255,.55)';
    c.beginPath();
    if (edge) { c.moveTo(x0, y0 + 4); c.lineTo(x0, y0); } else c.moveTo(x0, y0);
    if (inside) { c.lineTo(mid - w / 2 - 5, y0); c.moveTo(mid + w / 2 + 5, y0); }
    c.lineTo(xb, y0);
    if (!open) c.lineTo(xb, y0 + 4);
    c.stroke();
    c.fillStyle = 'rgba(214,224,255,.74)';
    if (inside) { c.textAlign = 'center'; c.fillText(label, mid, y0 + .5); }
    else { c.textAlign = 'right'; c.fillText(label, x0 - 6, y0 + .5); }
  }
}
export function drawSky(e: CanvasRenderingContext2D, t: { id: string }[], n: Record<string, Trail>, r: DrawOptions) {
    let { now: i, p: a, geo: o } = r,
      { x0: s, x1: c } = o,
      l = r.a ?? 1,
      u = r.thick ?? 1,
      d = c - (c - s) * (1 - (1 - r.dev) ** 3);
    if (a <= 0.01 || l <= 0.01) return;
    (e.save(), (e.globalAlpha = Math.min(1, a * 1.4) * l), besides(e, o, r.card));
    for (let t of o.guides) {
      let n = o.xOf(i - t);
      n < d - 0.5 ||
        ((e.strokeStyle = `rgba(157,180,255,.06)`),
        (e.lineWidth = 1),
        e.beginPath(),
        e.moveTo(n, o.top + 8),
        e.lineTo(n, o.bottom),
        e.stroke());
    }
    let f = e.createLinearGradient(0, o.top, 0, o.bottom);
    (f.addColorStop(0, `rgba(157,180,255,0)`),
      f.addColorStop(0.12, `rgba(157,180,255,.28)`),
      f.addColorStop(1, `rgba(157,180,255,.05)`),
      (e.strokeStyle = f),
      e.setLineDash([2, 4]),
      e.beginPath(),
      e.moveTo(c, o.top + 4),
      e.lineTo(c, o.bottom),
      e.stroke(),
      e.setLineDash([]),
      awayLines(e, r.aways ?? [], o, d, i),
      e.restore(),
      t.forEach((t, s) => {
        let f = o.y(s),
          Yx = o.yx ? (x: number) => o.yx(s, x) : () => f,
          p = n[t.id],
          m = Math.max(0, Math.min(1, (a - 0.25 - s * 0.02) / 0.45)),
          fo = r.focus === void 0 ? 1 : 1 - Math.min(0.42, Math.abs(s - r.focus) * 0.11);
        if (m <= 0 || !p) return;
        (e.save(),
          (e.globalAlpha = m * l * fo),
          r.sel === s &&
            o.band[1] >= o.width &&
            ((e.fillStyle = `rgba(157,180,255,.055)`),
            e.fillRect(
              o.band[0],
              f - o.rowH / 2 + 1,
              o.band[1] - o.band[0],
              o.rowH - 2,
            )));
        if (r.sel === s && r.span) {
          let t = Math.max(d, o.xOf(r.span[0])),
            n = o.xOf(r.span[1]);
          n - t > 1 &&
            ((e.fillStyle = `rgba(157,180,255,.10)`),
            (e.strokeStyle = `rgba(157,180,255,.3)`),
            (e.lineWidth = 1),
            e.beginPath(),
            e.roundRect(t - 4, f - 6.5, n - t + 8, 13, 6.5),
            e.fill(),
            e.stroke());
        }
        let h = Math.max(p.segs[0].a, i - o.span);
        ((e.strokeStyle = `rgba(196,204,238,${r.base ?? 0.07})`),
          (e.lineWidth = 1),
          e.beginPath(),
          pl(e, Math.max(d, o.xOf(h)), c, Yx),
          e.stroke());
        for (let g of o.gaps ?? []) {
          let gx = (o.xOf(g.a) + o.xOf(g.b)) / 2,
            f = Yx(gx);
          gx < d ||
            ((e.strokeStyle = `rgba(196,204,238,.42)`),
            (e.lineWidth = 1),
            e.beginPath(),
            e.moveTo(gx - 4.5, f + 4),
            e.lineTo(gx - 1.5, f - 4),
            e.moveTo(gx + 1.5, f + 4),
            e.lineTo(gx + 4.5, f - 4),
            e.stroke());
        }
        for (let n of p.segs) {
          let r = Math.max(n.a, i - o.span),
            a = Math.min(n.b ?? i, i);
          if (a <= r) continue;
          let s = Math.max(d, o.xOf(r)),
            c = o.xOf(a);
          if (!(c <= s)) {
            if (n.k === `work`) {
              let n = tint(COL.work, 0.35);
              e.lineCap = `round`;
              for (let r = s; r < c; r += 3) {
                let i = activityAt(p, o.tOf(r));
                ((e.strokeStyle = `rgba(${n},${0.16 + 0.56 * i})`),
                  (e.lineWidth = (0.9 + 1.8 * i) * u),
                  e.beginPath(),
                  e.moveTo(r, Yx(r)),
                  e.lineTo(Math.min(c, r + 3.2), Yx(Math.min(c, r + 3.2))),
                  e.stroke());
              }
            } else
              n.k === `wait`
                ? (e.save(),
                  (e.shadowColor = `rgba(255,201,143,.8)`),
                  (e.shadowBlur = 8),
                  (e.strokeStyle = rgba(COL.wait, 0.92, 0.15)),
                  (e.lineWidth = 2.6 * u),
                  (e.lineCap = `round`),
                  e.beginPath(),
                  pl(e, s, c, Yx),
                  e.stroke(),
                  e.restore(),
                  o.minutes &&
                    n.b === null &&
                    ((e.font = `600 10px "IBM Plex Mono", ui-monospace, monospace`),
                    // Only where the wait is long enough to hold its words clear of the head: the row says it anyway.
                    c - s > e.measureText(`${Math.round(i - n.a)} 分`).width + 22) &&
                    ((e.fillStyle = rgba(COL.wait, 0.95)),
                    (e.textBaseline = `bottom`),
                    e.fillText(`${Math.round(i - n.a)} 分`, s + 2, Yx(s) - 4)))
                : n.k === `stop` &&
                  ((e.strokeStyle = `rgba(255,106,90,.28)`),
                  e.setLineDash([2, 5]),
                  (e.lineWidth = 1.2 * u),
                  e.beginPath(),
                  pl(e, s, c, Yx),
                  e.stroke(),
                  e.setLineDash([]));
          }
        }
        // Marks closer than a few pixels (a busy hour far back) merge into one brighter point instead of piling up.
        let ms = p.marks.filter(t => t.t >= i - o.span).map(t => ({ ...t, x: o.xOf(t.t) }))
            .filter(t => t.x >= d && t.x <= o.x1 - 9).sort((a, b) => a.x - b.x),
          groups: (typeof ms)[] = [];
        for (let t of ms) groups.at(-1) && t.x - groups.at(-1)!.at(-1)!.x < 7 ? groups.at(-1)!.push(t) : groups.push([t]);
        for (let g of groups) {
          let t = g[0], n = g.length > 1 ? (g[0].x + g.at(-1)!.x) / 2 : t.x,
            f = Yx(n);
          if (g.length > 1 && !g.some(m => m.k === `err`)) {
            let k = Math.min(5, g.length - 1);
            ((e.fillStyle = `rgba(244,246,255,.95)`),
              e.fillRect(n - 0.8, f - 9 - k, 1.6, 6 + k),
              e.beginPath(),
              e.arc(n, f - 10.5 - k, 1.4 + k * 0.25, 0, Math.PI * 2),
              e.fill());
            continue;
          }
          if (g.length > 1) t = g.find(m => m.k === `err`)!;
          t.k === `you`
              ? ((e.fillStyle = `rgba(240,242,255,.85)`),
                e.fillRect(n - 0.6, f - 9, 1.2, 6),
                r.sel === s &&
                  (e.beginPath(),
                  e.arc(n, f - 10.5, 1.8, 0, Math.PI * 2),
                  e.fill()))
              : t.k === `her`
                ? ((e.fillStyle = `rgba(157,180,255,.9)`),
                  e.beginPath(),
                  e.arc(n, f - 7, 1.6, 0, Math.PI * 2),
                  e.fill())
                : t.k === `done`
                  ? (e.save(),
                    e.translate(n, f),
                    e.scale(0.62, 0.62),
                    glyph(e, `done`, { calm: !0, lift: 1.2 }),
                    e.restore())
                  : ((e.strokeStyle = rgba(COL.err, 0.95)),
                    (e.lineWidth = 1.6),
                    e.beginPath(),
                    e.moveTo(n - 3.5, f - 3.5),
                    e.lineTo(n + 3.5, f + 3.5),
                    e.moveTo(n + 3.5, f - 3.5),
                    e.lineTo(n - 3.5, f + 3.5),
                    e.stroke());
        }
        if (r.sel === s && r.pt !== void 0) {
          let t = r.ndx ?? o.xOf(r.pt),
            f = r.cy ?? o.y(s);
          (e.save(),
            (e.shadowColor = `rgba(233,236,245,.9)`),
            (e.shadowBlur = 8),
            (e.fillStyle = `#fff`),
            e.beginPath(),
            e.arc(t, f - 10.5, 3.4, 0, Math.PI * 2),
            e.fill(),
            e.restore(),
            t < o.x1 - 9 &&
              ((e.strokeStyle = `rgba(233,236,245,.5)`),
              (e.lineWidth = 1),
              e.beginPath(),
              e.moveTo(t, f - 7),
              e.lineTo(t, f + 9),
              e.stroke()));
        }
        e.restore();
      }));
    if (r.ndx !== void 0 && r.pt !== void 0 && !r.nm) {
      let t = r.ndx,
        n = e.createLinearGradient(0, o.top, 0, o.bottom);
      (n.addColorStop(0, `rgba(233,236,245,0)`),
        n.addColorStop(0.2, `rgba(233,236,245,.1)`),
        n.addColorStop(1, `rgba(233,236,245,.16)`),
        e.save(),
        (e.globalAlpha = Math.min(1, a * 1.4) * l),
        e.save(),
        besides(e, o, r.card),
        (e.strokeStyle = n),
        (e.lineWidth = 1),
        e.beginPath(),
        e.moveTo(t, o.top + 6),
        e.lineTo(t, o.bottom - 4),
        e.stroke(),
        e.restore());
      let s = Math.max(0, i - o.tOf(t)),
        c = ago(s);
      ((e.font = `500 10px "IBM Plex Mono", ui-monospace, monospace`),
        (e.textAlign = `center`),
        (e.textBaseline = `middle`));
      let u = e.measureText(c).width + 12,
        // On the axis's own line, in place of the label it steps over.
        f = o.bottom + 4,
        p = Math.min(o.x1 - u / 2, Math.max(o.x0 + u / 2, t));
      ((e.fillStyle = `rgb(24,27,52)`),
        e.beginPath(),
        e.roundRect(p - u / 2, f - 8, u, 16, 8),
        e.fill(),
        (e.strokeStyle = `rgba(233,236,245,.28)`),
        e.stroke(),
        (e.fillStyle = `rgba(238,240,251,.95)`),
        e.fillText(c, p, f + 0.5),
        e.restore());
    }
    if (r.conn) {
      let { x: t, y: n, x2: s, y2: c } = r.conn;
      (e.save(),
        (e.globalAlpha = Math.min(1, a * 1.4) * l * (r.conn.a ?? 1)),
        (e.strokeStyle = `rgba(157,180,255,.55)`),
        (e.lineWidth = 1),
        e.beginPath(),
        e.moveTo(t, n + 8),
        e.lineTo(t, n + 15.5),
        e.lineTo(s, n + 15.5),
        e.lineTo(s, c),
        e.stroke(),
        e.restore());
    }
  }

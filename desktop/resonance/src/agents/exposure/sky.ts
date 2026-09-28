// Canvas paths, colours and reveal timing ported from the approved B01 publication.
import { COL, glyph, rgba, tint } from './glyph';
import { clamp } from './motion';
import { activityAt, type Trail } from './timeline';
export type Geo = {
  width: number; x0: number; x1: number; span: number; cell: number; rowH: number; band: [number, number]; minutes: boolean;
  y(i: number): number; yx(i: number, x: number): number; top: number; bottom: number;
  xOf(t: number): number; tOf(x: number): number; guides: number[]; gaps?: { a: number; b: number }[];
};
type DrawOptions = {
  now: number; p: number; geo: Geo; dev: number; sel?: number; pt?: number; span?: [number, number];
  a?: number; thick?: number; focus?: number; base?: number; ndx?: number; cy?: number; nm?: boolean;
  conn?: { x: number; y: number; x2: number; y2: number; a?: number } | null;
};
// How long ago, in the axis's words: minutes, then hours, then days.
export const ago = (m: number) => m < .5 ? '现在' : m < 60 ? `${Math.round(m)} 分前` : m < 600 ? `${+(m / 60).toFixed(1)} 小时前`
  : m < 2880 ? `${Math.round(m / 60)} 小时前` : `${+(m / 1440).toFixed(1)} 天前`;
const pl = (c: CanvasRenderingContext2D, a: number, b: number, f: (x: number) => number) => {
  c.moveTo(a, f(a)); for (let x = a + 4; x < b; x += 4) c.lineTo(x, f(x)); c.lineTo(b, f(b));
};
// The ruler's marks, in minutes ago. Every cell between two marks is equally wide and time runs evenly inside it, so a
// cell near now holds minutes and one far back holds hours or days: recent time spreads out, a long history still fits.
const MARKS = [5, 15, 30, 60, 120, 240, 480, 1440, 2880, 10080, 20160, 43200];
export function geometry(width: number, now: number, span: number, height: number, bend: (i: number, x: number) => number): Geo {
  const x0 = 30, x1 = width - 262, range = x1 - x0;
  // The far edge is the first mark past the oldest session, so the ruler always ends on a whole mark.
  const edge = MARKS.find(m => m >= span) ?? span, ticks = [0, ...MARKS.filter(m => m < edge), edge], cell = range / (ticks.length - 1);
  const xAgo = (m: number) => {
    m = clamp(m, 0, edge);
    let i = 1; while (i < ticks.length - 1 && m > ticks[i]) i++;
    return x1 - cell * (i - 1 + (m - ticks[i - 1]) / (ticks[i] - ticks[i - 1]));
  };
  const agoX = (x: number) => {
    const k = clamp((x1 - x) / cell, 0, ticks.length - 1), i = Math.min(ticks.length - 2, Math.floor(k));
    return ticks[i] + (ticks[i + 1] - ticks[i]) * (k - i);
  };
  return { width, x0, x1, span: edge, cell, rowH: 27, band: [18, width], minutes: true,
    y: i => 82 + i * 27, yx: (i, x) => 82 + i * 27 + bend(i, x), top: 56, bottom: 56 + height - 22,
    xOf: at => xAgo(now - at), tOf: x => now - agoX(x), guides: ticks.slice(1),
  };
}
export function drawSky(e: CanvasRenderingContext2D, t: { id: string }[], n: Record<string, Trail>, r: DrawOptions) {
    let { now: i, p: a, geo: o } = r,
      { x0: s, x1: c } = o,
      l = r.a ?? 1,
      u = r.thick ?? 1,
      d = c - (c - s) * (1 - (1 - r.dev) ** 3);
    if (a <= 0.01 || l <= 0.01) return;
    (e.save(), (e.globalAlpha = Math.min(1, a * 1.4) * l));
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
                    c - s > 26 &&
                    ((e.fillStyle = rgba(COL.wait, 0.95)),
                    (e.font = `600 10px "IBM Plex Mono", ui-monospace, monospace`),
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
        for (let t of p.marks) {
          if (t.t < i - o.span) continue;
          let n = o.xOf(t.t),
            f = Yx(n);
          n < d ||
            (t.k === `you`
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
                    e.stroke()));
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
            (e.strokeStyle = `rgba(233,236,245,.5)`),
            (e.lineWidth = 1),
            e.beginPath(),
            e.moveTo(t, f - 7),
            e.lineTo(t, f + 9),
            e.stroke());
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
        (e.strokeStyle = n),
        (e.lineWidth = 1),
        e.beginPath(),
        e.moveTo(t, o.top + 6),
        e.lineTo(t, o.bottom + 2),
        e.stroke());
      let s = Math.max(0, i - o.tOf(t)),
        c = ago(s);
      ((e.font = `500 10px "IBM Plex Mono", ui-monospace, monospace`),
        (e.textAlign = `center`),
        (e.textBaseline = `middle`));
      let u = e.measureText(c).width + 12,
        f = o.bottom + 12,
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

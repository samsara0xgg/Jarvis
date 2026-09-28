export type IslandRect = { l: number; r: number; d: number };

// ---------- the black shape: everything hangs from the top edge ----------
// Each rect is [left, right] with a depth from the screen top. The outline of their union has concave shoulders
// where it meets the screen edge and rounded steps wherever the depth changes, so her island, the notch, the marks
// and whatever grows out of them read as one piece.
function runPath(run: IslandRect[]) {
  const S = 6, first = run[0], last = run.at(-1)!, rb = (s: IslandRect) => Math.min(24, s.d / 3, (s.r - s.l) / 2);
  const r0 = rb(first);
  let p = `M ${first.l - S} -20 L ${first.l - S} 0 Q ${first.l} 0 ${first.l} ${S} L ${first.l} ${first.d - r0} Q ${first.l} ${first.d} ${first.l + r0} ${first.d}`;
  for (let i = 0; i < run.length - 1; i++) {
    const a = run[i], b = run[i + 1], x = a.r, gap = Math.abs(b.d - a.d);
    if (b.d > a.d) {
      const rv = Math.min(rb(b), gap / 2), rc = Math.min(12, gap - rv, (a.r - a.l) / 2);
      p += ` L ${x - rc} ${a.d} Q ${x} ${a.d} ${x} ${a.d + rc} L ${x} ${b.d - rv} Q ${x} ${b.d} ${x + rv} ${b.d}`;
    } else {
      const rv = Math.min(rb(a), gap / 2), rc = Math.min(12, gap - rv, (b.r - b.l) / 2);
      p += ` L ${x - rv} ${a.d} Q ${x} ${a.d} ${x} ${a.d - rv} L ${x} ${b.d + rc} Q ${x} ${b.d} ${x + rc} ${b.d}`;
    }
  }
  const r1 = rb(last);
  return p + ` L ${last.r - r1} ${last.d} Q ${last.r} ${last.d} ${last.r} ${last.d - r1} L ${last.r} ${S} Q ${last.r} 0 ${last.r + S} 0 L ${last.r + S} -20 Z`;
}
export function skyline(rects: IslandRect[]) {
  const rs = rects.filter(r => r.r - r.l > .5 && r.d > .5);
  const xs = [...new Set(rs.flatMap(r => [r.l, r.r]))].sort((a, b) => a - b), segs: IslandRect[] = [];
  for (let i = 0; i < xs.length - 1; i++) {
    const a = xs[i], b = xs[i + 1];
    if (b - a < .01) continue;
    const m = (a + b) / 2, d = Math.max(0, ...rs.filter(r => r.l <= m && r.r >= m).map(r => r.d)), last = segs.at(-1);
    if (last && Math.abs(last.d - d) < .01 && Math.abs(last.r - a) < .01) last.r = b; else segs.push({ l: a, r: b, d });
  }
  let path = '', run: IslandRect[] = [];
  const flush = () => { if (run.length) path += runPath(run); run = []; };
  for (const s of segs) {
    if (s.d <= 0) { flush(); continue; }
    if (run.length && Math.abs(run.at(-1)!.r - s.l) > .01) flush();
    run.push(s);
  }
  flush();
  return path;
}

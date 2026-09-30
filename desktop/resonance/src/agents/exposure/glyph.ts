// Reused from the B01 design source; canonical snapshot: docs/design/agents-long-exposure.html.
// The notch's four-pointed star, drawn for the Agents window. Colour says the state; a shape says it again for anyone
// who cannot tell the colours apart, or has motion turned off: finished-unread carries a dot, working is hollow,
// waiting is full and warm, an error has one arm broken off.
export type St = 'work' | 'pack' | 'wait' | 'done' | 'read' | 'err';
export type C3 = [number, number, number];
export const COL: Record<St, C3> = {
  work: [108, 156, 255], pack: [187, 148, 255], wait: [255, 201, 143], done: [111, 224, 180], read: [196, 204, 238], err: [255, 106, 90],
};
export const tint = (c: C3, k: number) => c.map(v => Math.round(v + (255 - v) * k)).join(',');
export const rgba = (c: C3, a = 1, k = 0) => `rgba(${tint(c, k)},${a})`;

export function sparkPath(c: CanvasRenderingContext2D, ro: number | number[], ri: number, rot = 0, x = 0, y = 0) {
  c.beginPath();
  for (let i = 0; i < 8; i++) {
    const r = i % 2 ? ri : Array.isArray(ro) ? ro[i >> 1] : ro, a = rot + i * Math.PI / 4 - Math.PI / 2;
    c.lineTo(x + Math.cos(a) * r, y + Math.sin(a) * r);
  }
  c.closePath();
}

export type GlyphOpts = { lit?: boolean; t?: number; since?: number; hover?: boolean; calm?: boolean; lift?: number; scale?: number };
// One star at the origin, in css px (the caller has set the device transform).
export function glyph(c: CanvasRenderingContext2D, st: St, o: GlyphOpts = {}) {
  const t = o.calm ? 0 : o.t ?? 0, since = o.since ?? 9, col = COL[st], lift = o.lift ?? 1;
  const pop = since < .42 ? .6 + .4 * Math.min(1, since / .42) : 1;
  c.save(); c.scale(pop * (o.scale ?? 1), pop * (o.scale ?? 1));
  if (o.lit) {
    // The open one alone shines: a bright star with a thin cross of light, a little warm if it waits on you.
    const k = st === 'wait' ? .55 : .72, L = 11 * (.94 + .06 * Math.sin(t * 1.9));
    c.lineWidth = .6;
    for (const [dx, dy] of [[1, 0], [0, 1]]) {
      const g = c.createLinearGradient(-dx * L, -dy * L, dx * L, dy * L);
      g.addColorStop(0, rgba(col, 0, .6)); g.addColorStop(.5, rgba(col, .6, .6)); g.addColorStop(1, rgba(col, 0, .6));
      c.strokeStyle = g; c.beginPath(); c.moveTo(-dx * L, -dy * L); c.lineTo(dx * L, dy * L); c.stroke();
    }
    c.shadowColor = rgba(col, .9, .3); c.shadowBlur = 6;
    c.fillStyle = rgba(col, 1, k); sparkPath(c, 5.6, 1.35); c.fill();
    c.globalAlpha = .7; sparkPath(c, 3.2, .9, Math.PI / 4); c.fill();
    c.restore();
    return;
  }
  const hov = o.hover ? 1.6 : 1;
  if (st === 'work' || st === 'pack') {
    // Working is hollow and turns slowly; with motion off it stays hollow and still.
    const a = (.5 + .16 * Math.sin(t * 2.2)) * lift * hov;
    c.strokeStyle = rgba(col, Math.min(1, a), .35); c.lineWidth = 1.15; c.lineJoin = 'round';
    const ro = st === 'pack' ? 3.4 + 1.2 * (.5 + .5 * Math.cos(t * 3.9)) : 4.7;
    sparkPath(c, ro, 1.5, t * .45); c.stroke();
  } else if (st === 'wait') {
    const a = (.62 + .22 * (.5 + .5 * Math.cos(t * 1.6))) * lift * hov;
    c.shadowColor = rgba(col, .7); c.shadowBlur = 4;
    c.fillStyle = rgba(col, Math.min(1, a), .25); sparkPath(c, 4.9, 1.3); c.fill();
  } else if (st === 'done') {
    c.fillStyle = rgba(col, Math.min(1, .72 * lift * hov), .3); sparkPath(c, 4.5, 1.2); c.fill();
    // unread: a small dot at its shoulder
    c.beginPath(); c.arc(5.2, -5.2, 1.35, 0, Math.PI * 2); c.fill();
  } else if (st === 'err') {
    // one arm broken off, its tip a small chip just beyond the break: reads as broken without the red, and as a whole
    // star rather than an arrow
    c.fillStyle = rgba(col, Math.min(1, .82 * lift * hov), .2); sparkPath(c, [4.9, 2.4, 4.9, 4.9], 1.3); c.fill();
    c.beginPath(); c.moveTo(3.3, 0); c.lineTo(4.1, -.62); c.lineTo(4.9, 0); c.lineTo(4.1, .62); c.closePath(); c.fill();
  } else {
    c.fillStyle = rgba(col, Math.min(1, .24 * lift * hov), .6); sparkPath(c, 4.2, 1.1); c.fill();
  }
  c.restore();
}


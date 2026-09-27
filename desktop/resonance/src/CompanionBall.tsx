import { useEffect, useRef, type RefObject } from 'react';
import { Core, spring, step, type ExprId, type Skin } from './starCore';

export const R = 26;
// Held this long, a poke becomes a costume change instead.
export const HOLD_MS = 650;
const HOME_SCALE = .5;
export type Place = 'home' | 'peek' | 'out' | 'dock';
export type Point = { x: number; y: number };
export type Lobe = { left: number; right: number; height: number; notched: boolean; tucked?: boolean };
// lift: how far the canvas rides up with the tucked island (0 while the Dashboard or a notice holds her).
// `homeFace`: she wears `expr` and follows `look` even at home (a notice hangs from the notch and she watches it from there).
// `away`: out at the text caret for dictation (ADR 0058), drawn by another window; `happy` when she comes back from pasting.
// `deep`: think mode is on and she is idle, so her eyes keep its colour, in the island too (ADR 0064).
export type BallTarget = { place: Place; expr: ExprId; pressed: boolean; anchors: Record<Place, Point>; homeGlass: boolean; lift: number; homeFace?: boolean;
  away?: boolean; happy?: boolean; deep?: boolean };
// With her glass showing at home, this long without the cursor moving sends her to sleep there.
const DOZE_MS = 10 * 60_000;
const clamp01 = (v: number) => Math.max(0, Math.min(1, v));
const smooth = (a: number, b: number, v: number) => { const k = clamp01((v - a) / (b - a)); return k * k * (3 - 2 * k); };
const backOut = (e: number, c = 1.6) => 1 + (c + 1) * (e - 1) ** 3 + c * (e - 1) ** 2;
export type BallHandle = { nudge: () => void; arrive: () => void; change: (skin: Skin) => void; hop: (height: number) => void };

// x Hz, x damping, y Hz, y damping. A slower x than y bends every flight into a curve.
const travel: Record<Place, [number, number, number, number]> = {
  home: [2.4, .92, 2.6, .9], peek: [3, .8, 3.2, .7], out: [2.6, .75, 2.6, .62], dock: [1.5, .85, 2.2, .72],
};
// The software extension left of the camera: flush with the hardware cutout, concave
// shoulders at the screen edge, and its right side tucked under the cutout.
function lobePath({ left, right, height: h, notched }: Lobe) {
  const s = 6, r = 10;
  const side = `M ${left - s} -20 L ${left - s} 0 Q ${left} 0 ${left} ${s} L ${left} ${h - r} Q ${left} ${h} ${left + r} ${h}`;
  return notched ? `${side} L ${right} ${h} L ${right} -20 Z`
    : `${side} L ${right - r} ${h} Q ${right} ${h} ${right} ${h - r} L ${right} ${s} Q ${right} 0 ${right + s} 0 L ${right + s} -20 Z`;
}

export function CompanionBall({ width, height, lobe, lift, target, look, handle, skin, label, onPress, onRelease, onCancel, onMove }: {
  width: number; height: number; lobe: Lobe; lift: number; target: BallTarget; look: RefObject<Point | null>; handle: RefObject<BallHandle | null>;
  skin: Skin; label: string; onPress: () => void; onRelease: () => void; onCancel: () => void; onMove: () => void;
}) {
  const latest = useRef(target), moved = useRef(onMove), firstSkin = useRef(skin), size = useRef({ width, height });
  const lobeD = lobePath(lobe), island = useRef({ lobe, path: new Path2D(lobeD) });
  if (island.current.lobe !== lobe) island.current = { lobe, path: new Path2D(lobeD) };
  moved.current = onMove; size.current = { width, height };
  const wake = useRef(() => {});
  useEffect(() => { latest.current = target; wake.current(); });
  const silhouette = useRef<SVGCircleElement>(null), islandShape = useRef<SVGPathElement>(null), canvas = useRef<HTMLCanvasElement>(null), hit = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const cv = canvas.current!, ctx = cv.getContext('2d')!, core = new Core(firstSkin.current);
    // The eyes are drawn apart first, so one blur gives them their glow.
    const eyes = document.createElement('canvas'), ectx = eyes.getContext('2d')!;
    const start = latest.current.anchors.home;
    const s = { x: spring(start.x), y: spring(start.y), scale: spring(HOME_SCALE), shine: spring(0), pivot: spring(0), dock: spring(0), fold: spring(1) };
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0, last = 0, d = 0, wanted: Place = 'home', shown: Place = 'home', switchAt = 0, pressedAt = -1;
    let glanceUntil = 0, sleep: ReturnType<typeof setTimeout> | undefined, tick: ReturnType<typeof setTimeout> | undefined, lit = '';
    let movedAt = performance.now(), px = NaN, py = NaN;
    let away = false, awayAt = -1e9, happyUntil = 0, leftHome = true;
    const fit = () => {
      const k = Math.min(2, devicePixelRatio || 1), w = Math.round(size.current.width * k), h = Math.round(size.current.height * k);
      if (k === d && cv.width === w && cv.height === h) return;
      d = k; cv.width = w; cv.height = h;
      eyes.width = eyes.height = Math.ceil(3.4 * R * k);
    };
    const draw = (now: number) => {
      frame = 0; fit();
      const t = latest.current, firm = reduced.matches;
      const dt = Math.min(last ? (now - last) / 1000 : 1 / 60, 1 / 20);
      last = now;
      // Going home: look up at the island for a beat, then fly.
      if (t.place !== wanted) { wanted = t.place; switchAt = now + (wanted === 'home' && shown !== 'home' ? 170 : 0); }
      if (now >= switchAt) shown = wanted;
      const atHome = shown === 'home', leaving = wanted === 'home' && !atHome, docked = shown === 'dock';
      const glance = atHome && now < glanceUntil;
      // With her glass showing at home she stays awake there: she blinks, watches a nearby cursor, and dozes after a long quiet spell.
      const glassHome = t.homeGlass, p = look.current;
      if (p && (p.x !== px || p.y !== py)) { px = p.x; py = p.y; movedAt = now; }
      const dozing = glassHome && atHome && now - movedAt > DOZE_MS;
      const anchor = t.anchors[shown], [xHz, xDamp, yHz, yDamp] = travel[shown];
      const moving = [
        step(s.x, anchor.x, xHz, firm ? 1 : xDamp, dt), step(s.y, anchor.y, yHz, firm ? 1 : yDamp, dt),
        step(s.scale, atHome ? HOME_SCALE : 1, 2.6, .95, dt), step(s.shine, atHome && !glassHome ? 0 : 1, 3, 1, dt),
        step(s.pivot, docked ? 1 : 0, 3, 1, dt), step(s.dock, docked ? 1 : 0, 3, 1, dt),
      ].some(Boolean);
      // Gaze: toward the cursor or caret, softer with distance; straight ahead in the island.
      let gaze: [number, number] | null = null;
      const point = leaving ? { x: s.x.value, y: -400 } : atHome && !glance && !glassHome && !t.homeFace ? null : look.current;
      if (point) {
        const dx = point.x - s.x.value, dy = point.y - s.y.value, dist = Math.hypot(dx, dy) || 1;
        const k = dist / (dist + 90) * (dist < 280 ? 1 : Math.max(.35, 1 - (dist - 280) / 700));
        // Resting at home she only follows a cursor that comes near; otherwise she looks around on her own.
        if (!(atHome && !leaving && glassHome && dist > 260 && !t.homeFace)) gaze = [dx / dist * k, shown === 'peek' ? Math.max(0, dy / dist * k) : dy / dist * k];
      }
      // Holding her charges a costume change: she squashes further, shivers and her stars speed up.
      if (!t.pressed) pressedAt = -1; else if (pressedAt < 0) pressedAt = now;
      const charge = pressedAt < 0 ? 0 : Math.min(1, Math.max(0, (now - pressedAt - 200) / (HOLD_MS - 200)));
      // ADR 0058, dictation: she crouches and slips sideways into the notch, and the island folds after her like a
      // door; coming back it opens first and she slides out, a little too far, smiling when the words went in.
      if (!!t.away !== away) { away = !!t.away; awayAt = now; happyUntil = !away && t.happy ? now + 1100 : 0; if (away) leftHome = shown === 'home'; }
      const ae = now - awayAt, { lobe: shape } = island.current;
      const gone = shape.notched ? shape.right - 14 : s.x.value, edge = shape.notched ? shape.right - 22 : Infinity;
      let slipX: number | null = null, slipY = 0, sx = 1, sy = 1, seen = 1, hidden = false;
      if (away) {
        if (ae < 170 && leftHome) {
          const c = smooth(0, 70, ae), u = clamp01((ae - 70) / 100), e = u * u;
          slipX = s.x.value + (gone - s.x.value) * e; slipY = c - e; sx = 1 + .12 * c + .6 * e; sy = 1 - .25 * c - .35 * e; seen = 1 - smooth(.5, 1, u);
        } else hidden = true;
      } else if (ae < 280) {
        if (ae < 80) hidden = true;
        else { const u = clamp01((ae - 80) / 200), f = 1 - smooth(0, .6, u); slipX = gone + (s.x.value - gone) * backOut(u); sx = 1 + .6 * f; sy = 1 - .4 * f; seen = smooth(0, .35, u); }
      }
      const clipX = slipX === null ? Infinity : edge;
      step(s.fold, away && ae > 110 ? 0 : 1, 4.2, .9, dt);
      if (s.fold.value < .999) islandShape.current!.setAttribute('transform', `translate(${shape.right} 0) scale(${Math.max(0, s.fold.value)} 1) translate(${-shape.right} 0)`);
      else islandShape.current!.removeAttribute('transform');
      const face: ExprId = now < happyUntil ? '10' : atHome && !t.homeFace ? (glassHome ? (dozing ? 'doze' : 'rest') : glance ? 'glance' : 'home') : shown === 'peek' ? 'peek' : t.expr;
      const busy = core.update(now, dt, { expr: face, look: gaze, still: atHome && !glance && !glassHome && !t.homeFace, pressed: t.pressed, charge, deep: t.deep && face !== '10' });

      // Where she is (spring position, flight squash, the pivot on the Dashboard edge), then her own motion.
      const a = s.shine.value * seen, scale = s.scale.value, x = slipX ?? s.x.value, y = s.y.value + slipY, pivot = s.pivot.value * R;
      const speed = Math.hypot(s.x.velocity, s.y.velocity), flight = firm ? 0 : Math.min(.09, speed / 4000);
      const angle = Math.atan2(s.y.velocity, s.x.velocity), squat = 1 - .05 * s.dock.value;
      const [jx, jy, bx, by] = core.pose(a);
      const pose = (c: CanvasRenderingContext2D, cx: number, cy: number) => {
        c.translate(cx, cy + pivot * scale); c.rotate(angle); c.scale(1 + flight, 1 / Math.sqrt(1 + flight)); c.rotate(-angle);
        c.scale(scale * sx, scale * squat * sy); c.translate(jx * R, jy * R - pivot); c.scale(bx, by);
      };
      const deg = angle * 180 / Math.PI;
      silhouette.current!.setAttribute('transform', `translate(${x} ${y + pivot * scale}) rotate(${deg}) scale(${1 + flight} ${1 / Math.sqrt(1 + flight)}) rotate(${-deg}) scale(${scale} ${scale * squat}) translate(${jx * R} ${jy * R - pivot}) scale(${bx} ${by})`);
      // The goo only matters where the ball meets the island, and not while it is tucked away or out at the caret.
      silhouette.current!.style.display = y - R * scale - island.current.lobe.height < 26 && !island.current.lobe.tucked && slipX === null && !hidden ? '' : 'none';

      const { lobe, path } = island.current, S = Math.round(2 * 1.3 * R * d);
      ctx.setTransform(d, 0, 0, d, 0, 0); ctx.clearRect(0, 0, size.current.width, size.current.height);
      // The notch's left edge is the mouth of her tunnel.
      ctx.save(); ctx.beginPath(); ctx.rect(0, 0, Math.min(clipX, size.current.width), size.current.height); ctx.clip();
      if (!hidden && a > .01 && core.render(S, 3)) {
        ctx.save(); ctx.globalAlpha = a;
        ctx.save(); ctx.translate(x, y); ctx.scale(scale, scale); core.orbit(ctx, R, -1); ctx.restore();
        ctx.save(); pose(ctx, x, y);
        // A soft shadow while she floats; a contact shadow once she sits on the Dashboard.
        const g = ctx.createRadialGradient(0, .28 * R, 0, 0, .28 * R, 1.15 * R);
        g.addColorStop(0, `rgba(0,0,0,${.45 * (1 - s.dock.value)})`); g.addColorStop(.6, `rgba(0,0,0,${.3 * (1 - s.dock.value)})`); g.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, .28 * R, 1.15 * R, 0, 2 * Math.PI); ctx.fill();
        if (s.dock.value > .01) {
          ctx.save(); ctx.translate(0, .98 * R); ctx.scale(1, .18);
          const c = ctx.createRadialGradient(0, 0, 0, 0, 0, .8 * R);
          c.addColorStop(0, `rgba(0,0,0,${.6 * s.dock.value})`); c.addColorStop(1, 'rgba(0,0,0,0)');
          ctx.fillStyle = c; ctx.beginPath(); ctx.arc(0, 0, .8 * R, 0, 2 * Math.PI); ctx.fill(); ctx.restore();
        }
        core.inside(ctx, R, S); core.glass(ctx, R, S);
        ctx.restore(); ctx.restore();
        // The island hides her body above its edge (unless her glass shows at home); a short fade keeps that edge from reading as a seam.
        if (!glassHome) {
          ctx.save(); ctx.globalCompositeOperation = 'destination-out'; ctx.fillStyle = '#000'; ctx.fill(path);
          const fade = ctx.createLinearGradient(0, lobe.height, 0, lobe.height + 9);
          fade.addColorStop(0, '#000'); fade.addColorStop(1, 'rgba(0,0,0,0)');
          ctx.fillStyle = fade; ctx.fillRect(lobe.left, lobe.height, lobe.right - lobe.left, 9); ctx.restore();
        }
      }
      // The eyes stay on top everywhere, the island included.
      const E = eyes.width, [glow, blur] = core.glow();
      if (!hidden) {
        ectx.setTransform(d, 0, 0, d, 0, 0); ectx.clearRect(0, 0, E, E);
        pose(ectx, E / 2 / d, E / 2 / d); core.eyes(ectx, R);
        ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.globalAlpha = seen; ctx.shadowColor = glow; ctx.shadowBlur = blur * R * scale * d;
        ctx.drawImage(eyes, x * d - E / 2, y * d - E / 2); ctx.restore();
      }
      if (!hidden && a > .01) { ctx.save(); ctx.globalAlpha = a; ctx.translate(x, y); ctx.scale(scale, scale); core.orbit(ctx, R, 1); core.particles(ctx, R, d); ctx.restore(); }
      ctx.restore();
      if (cv.dataset.skin !== core.skin) cv.dataset.skin = core.skin;
      if (cv.dataset.face !== core.expr) cv.dataset.face = core.expr ?? '';
      // Her light, for the Dashboard hanging under her.
      const light = core.light.glow.map(v => Math.round(v * 255)).join(' ');
      if (light !== lit) { lit = light; document.documentElement.style.setProperty('--glow', light); }

      hit.current!.style.transform = `translate(${x - R - 4}px, ${y - R - 4 - t.lift}px) scale(${scale})`;
      if (hit.current!.dataset.place !== shown) hit.current!.dataset.place = shown;
      // She can slide under a resting cursor; hit testing must follow her, not only the mouse.
      if (moving) moved.current();
      const tripping = (away ? ae < 200 : ae < 1200) || Math.abs(s.fold.value - (away ? 0 : 1)) > .002;
      if (!atHome || moving || now < switchAt || tripping || busy && !glassHome) frame = requestAnimationFrame(draw);
      // Awake at home she needs no more than 30 frames a second, dozing 10.
      else if (glassHome) tick = setTimeout(() => { tick = undefined; frame = requestAnimationFrame(draw); }, dozing ? 100 : 33);
      else sleep ??= setTimeout(() => { sleep = undefined; glanceUntil = performance.now() + 1500; wake.current(); }, 22000 + Math.random() * 18000);
    };
    wake.current = () => {
      if (sleep) { clearTimeout(sleep); sleep = undefined; }
      if (tick) { clearTimeout(tick); tick = undefined; }
      if (!frame) { last = 0; frame = requestAnimationFrame(draw); }
    };
    handle.current = {
      nudge: () => { core.s.gy.velocity += 1.6; wake.current(); },
      // A new screen: already in its island, with a small bump out of it and a look around.
      arrive: () => {
        const a = latest.current.anchors.home;
        Object.assign(s.x, { value: a.x, velocity: 0 }); Object.assign(s.y, { value: a.y, velocity: 140 });
        Object.assign(s.scale, { value: HOME_SCALE, velocity: 0 }); Object.assign(s.shine, { value: 0, velocity: 0 });
        wanted = shown = 'home'; switchAt = 0; glanceUntil = performance.now() + 1400;
        wake.current();
      },
      change: next => { core.change(next, performance.now()); wake.current(); },
      hop: height => { core.hop(performance.now(), height); wake.current(); },
    };
    wake.current();
    return () => { cancelAnimationFrame(frame); clearTimeout(sleep); clearTimeout(tick); wake.current = () => {}; handle.current = null; };
  }, []);
  return <>
    <svg className="companion-stage" width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <defs>
        {/* Blur plus an alpha threshold: the island and the ball join like one liquid body. */}
        <filter id="cb-goo" x="-40%" y="-60%" width="180%" height="220%" colorInterpolationFilters="sRGB">
          <feGaussianBlur stdDeviation="4"/>
          <feColorMatrix values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 18 -8"/>
        </filter>
      </defs>
      <g filter="url(#cb-goo)"><path ref={islandShape} className="companion-island" d={lobeD} style={lift ? { transform: `translateY(${-lift}px)` } : undefined}/><circle ref={silhouette} r={R}/></g>
    </svg>
    <canvas ref={canvas} className="companion-canvas" style={{ width, height, transform: target.lift ? `translateY(${-target.lift}px)` : undefined }} aria-hidden="true"/>
    <button ref={hit} className="companion-hit" data-hit aria-label={label} style={{ width: 2 * R + 8, height: 2 * R + 8 }}
      onPointerDown={event => { if (event.button !== 0) return; event.currentTarget.setPointerCapture(event.pointerId); onPress(); }}
      onPointerUp={onRelease} onPointerCancel={onCancel} onLostPointerCapture={onCancel}
      onClick={event => { if (event.detail === 0) { onPress(); onRelease(); } }}/>
  </>;
}

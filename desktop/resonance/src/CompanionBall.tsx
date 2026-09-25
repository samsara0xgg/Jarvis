import { useEffect, useRef, type RefObject } from 'react';

export const R = 26;
const HOME_SCALE = .5;
export type Place = 'home' | 'peek' | 'out' | 'dock';
export type Mood = 'idle' | 'listening' | 'speaking';
export type Point = { x: number; y: number };
export type Lobe = { left: number; right: number; height: number; notched: boolean };
export type BallTarget = { place: Place; mood: Mood; pressed: boolean; hearing: boolean; anchors: Record<Place, Point> };
export type BallHandle = { nudge: () => void; arrive: () => void };

type Eyes = { sep: number; y: number; length: number; width: number; tilt: number };
// Capsule eyes in ball radii. Tilt 90 lays both flat ("— —"), 0 stands them upright;
// between the two they rotate through "/ \" like the physical ball's display.
const eyes = {
  slit: { sep: .44, y: .02, length: .34, width: .13, tilt: 90 },
  glance: { sep: .36, y: .02, length: .18, width: .18, tilt: 0 },
  peek: { sep: .25, y: .32, length: .16, width: .18, tilt: 14 },
  idle: { sep: .25, y: -.02, length: .3, width: .19, tilt: 20 },
  listening: { sep: .24, y: .04, length: .4, width: .2, tilt: 0 },
  speaking: { sep: .25, y: .01, length: .24, width: .2, tilt: 0 },
} satisfies Record<string, Eyes>;
// x Hz, x damping, y Hz, y damping. A slower x than y bends every flight into a curve.
const travel: Record<Place, [number, number, number, number]> = {
  home: [2.4, .92, 2.6, .9], peek: [3, .8, 3.2, .7], out: [2.6, .75, 2.6, .62], dock: [1.5, .85, 2.2, .72],
};
type Spring = { value: number; velocity: number };
const spring = (value: number): Spring => ({ value, velocity: 0 });
// Damped harmonic spring; damping 1 never overshoots. Returns whether it still moves.
function step(s: Spring, goal: number, hz: number, damping: number, dt: number) {
  const w = 2 * Math.PI * hz;
  for (let left = dt; left > 1e-6; left -= 1 / 240) {
    const h = Math.min(left, 1 / 240);
    s.velocity += (-w * w * (s.value - goal) - 2 * damping * w * s.velocity) * h;
    s.value += s.velocity * h;
  }
  if (Math.abs(s.value - goal) < 1e-3 && Math.abs(s.velocity) < 1e-2) { s.value = goal; s.velocity = 0; return false; }
  return true;
}
const arc = (r: number, from: number, to: number) => {
  const p = (a: number) => `${(r * Math.cos(a * Math.PI / 180)).toFixed(2)} ${(r * Math.sin(a * Math.PI / 180)).toFixed(2)}`;
  return `M ${p(from)} A ${r} ${r} 0 0 1 ${p(to)}`;
};
// The software extension left of the camera: flush with the hardware cutout, concave
// shoulders at the screen edge, and its right side tucked under the cutout.
function lobePath({ left, right, height: h, notched }: Lobe) {
  const s = 6, r = 10;
  const side = `M ${left - s} -20 L ${left - s} 0 Q ${left} 0 ${left} ${s} L ${left} ${h - r} Q ${left} ${h} ${left + r} ${h}`;
  return notched ? `${side} L ${right} ${h} L ${right} -20 Z`
    : `${side} L ${right - r} ${h} Q ${right} ${h} ${right} ${h - r} L ${right} ${s} Q ${right} 0 ${right + s} 0 L ${right + s} -20 Z`;
}

export function CompanionBall({ width, height, lobe, target, look, handle, label, onPress, onRelease, onCancel, onMove }: {
  width: number; height: number; lobe: Lobe; target: BallTarget; look: RefObject<Point | null>; handle: RefObject<BallHandle | null>;
  label: string; onPress: () => void; onRelease: () => void; onCancel: () => void; onMove: () => void;
}) {
  const latest = useRef(target), island = useRef(lobe), moved = useRef(onMove);
  island.current = lobe; moved.current = onMove;
  const wake = useRef(() => {});
  useEffect(() => { latest.current = target; wake.current(); });
  const silhouette = useRef<SVGCircleElement>(null), body = useRef<SVGGElement>(null), float = useRef<SVGCircleElement>(null);
  const contact = useRef<SVGEllipseElement>(null), mint = useRef<SVGGElement>(null), face = useRef<SVGGElement>(null);
  const eyeL = useRef<SVGGElement>(null), eyeR = useRef<SVGGElement>(null), lineL = useRef<SVGLineElement>(null), lineR = useRef<SVGLineElement>(null);
  const hit = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const start = latest.current.anchors.home;
    const s = {
      x: spring(start.x), y: spring(start.y), scale: spring(HOME_SCALE), shine: spring(0), stretch: spring(1), pivot: spring(0),
      dock: spring(0), mint: spring(0), squint: spring(1), gx: spring(0), gy: spring(0),
      sep: spring(eyes.slit.sep), ey: spring(eyes.slit.y), length: spring(eyes.slit.length), width: spring(eyes.slit.width), left: spring(180), right: spring(0),
    };
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0, last = 0, time = 0, wanted: Place = 'home', shown: Place = 'home', switchAt = 0;
    let blinkAt = performance.now() + 2500, blinkStart = -1, glanceUntil = 0, sleep: ReturnType<typeof setTimeout> | undefined;
    const draw = (now: number) => {
      frame = 0;
      const t = latest.current, calm = reduced.matches ? 0 : 1, firm = reduced.matches;
      const dt = Math.min(last ? (now - last) / 1000 : 1 / 60, 1 / 20);
      last = now; time += dt;
      // Going home: look up at the island for a beat, then fly.
      if (t.place !== wanted) { wanted = t.place; switchAt = now + (wanted === 'home' && shown !== 'home' ? 170 : 0); }
      if (now >= switchAt) shown = wanted;
      const atHome = shown === 'home', leaving = wanted === 'home' && !atHome, docked = shown === 'dock';
      const mood: Mood = atHome || shown === 'peek' ? 'idle' : t.mood;
      const glance = atHome && now < glanceUntil;
      const shape: Eyes = atHome ? (glance ? eyes.glance : eyes.slit) : shown === 'peek' ? eyes.peek : eyes[mood];
      const anchor = t.anchors[shown], [xHz, xDamp, yHz, yDamp] = travel[shown], eyeDamp = firm ? 1 : .62;
      const moving = [
        step(s.x, anchor.x, xHz, firm ? 1 : xDamp, dt), step(s.y, anchor.y, yHz, firm ? 1 : yDamp, dt),
        step(s.scale, atHome ? HOME_SCALE : 1, 2.6, .95, dt), step(s.shine, atHome ? 0 : 1, 3, 1, dt),
        // Press flattens fast; release rebounds on a soft, slightly wobbly spring.
        step(s.stretch, t.pressed ? .88 : (mood === 'listening' ? 1.04 : 1) * (docked ? .95 : 1), t.pressed ? 10 : 4.2, t.pressed ? .9 : firm ? 1 : .45, dt),
        step(s.pivot, docked ? 1 : 0, 3, 1, dt), step(s.dock, docked ? 1 : 0, 3, 1, dt),
        step(s.mint, mood === 'speaking' ? 1 : 0, 2.5, 1, dt), step(s.squint, t.pressed ? .38 : 1, 9, .8, dt),
        step(s.sep, shape.sep, 5.2, eyeDamp, dt), step(s.ey, shape.y, 5.2, eyeDamp, dt),
        step(s.length, shape.length, 5.2, eyeDamp, dt), step(s.width, shape.width, 5.2, eyeDamp, dt),
        step(s.left, 90 + shape.tilt, 5.2, eyeDamp, dt), step(s.right, 90 - shape.tilt, 5.2, eyeDamp, dt),
      ].some(Boolean);
      // Gaze: toward the cursor or caret, softer with distance; straight ahead in the island.
      let gx = 0, gy = 0;
      const point = leaving ? { x: s.x.value, y: -400 } : atHome && !glance ? null : look.current;
      if (point) {
        const dx = point.x - s.x.value, dy = point.y - s.y.value, d = Math.hypot(dx, dy) || 1;
        const k = d / (d + 90) * (d < 280 ? 1 : Math.max(.35, 1 - (d - 280) / 700));
        gx = dx / d * k; gy = dy / d * k;
      }
      if (mood === 'listening') { gx *= .5; gy *= .5; }
      if (shown === 'peek') gy = Math.max(gy, 0);
      const gazing = [step(s.gx, gx, 3.2, .9, dt), step(s.gy, gy, 3.2, .9, dt)].some(Boolean);
      // Blink now and then while the eyes are open: quick close, slower open, sometimes twice.
      if (atHome && !glance || t.pressed) { if (blinkStart < 0) blinkAt = Math.max(blinkAt, now + 1200); }
      else if (blinkStart < 0 && now >= blinkAt) blinkStart = now;
      let lid = 1;
      if (blinkStart >= 0) {
        const k = (now - blinkStart) / 1000;
        if (k >= .26) { blinkStart = -1; blinkAt = now + (Math.random() < .18 ? 140 : 2600 + Math.random() * 4200); }
        else lid = k < .075 ? 1 - .92 * (k / .075) ** 2 : k < .11 ? .08 : .08 + .92 * (1 - (1 - (k - .11) / .15) ** 3);
      }
      // Life outside the island: breathing, a slow float, and speech moving the whole body.
      const phrase = (.5 + .5 * Math.sin(time * 1.7 - .8)) ** 2, syllable = (.5 + .5 * Math.sin(time * 7.3 + 1.3 * Math.sin(time * 2.1))) ** 2;
      const env = s.mint.value * phrase * (.35 + .65 * syllable);
      const breath = mood === 'listening' ? .012 * Math.sin(time * 2 * Math.PI / 2.8) + (t.hearing ? .012 * syllable : 0) : .007 * Math.sin(time * 2 * Math.PI / 4.2);
      const bob = docked ? 0 : .9 * Math.sin(time * 2 * Math.PI / 3.3 + 1);
      const alive = calm * s.shine.value;
      const scale = s.scale.value, stretch = s.stretch.value * (1 + alive * (breath + .028 * env));
      const sx = (1 + alive * .012 * env) / Math.sqrt(stretch), sy = stretch;
      // Squash and stretch along the flight path, capped so it reads as soft, not liquid.
      const speed = Math.hypot(s.x.velocity, s.y.velocity), flight = calm * Math.min(.09, speed / 4000);
      const angle = Math.atan2(s.y.velocity, s.x.velocity) * 180 / Math.PI;
      const x = s.x.value, y = s.y.value + alive * (bob - 1.4 * env), pivot = s.pivot.value * R;
      const transform = `translate(${x} ${y + pivot * scale}) rotate(${angle}) scale(${1 + flight} ${1 / Math.sqrt(1 + flight)}) rotate(${-angle}) scale(${scale * sx} ${scale * sy}) translate(0 ${-pivot})`;
      silhouette.current!.setAttribute('transform', transform);
      // The goo only matters where the ball meets the island.
      silhouette.current!.style.display = y - R * scale - island.current.height < 26 ? '' : 'none';
      body.current!.setAttribute('transform', transform);
      body.current!.style.opacity = String(s.shine.value);
      float.current!.style.opacity = String(.45 * (1 - s.dock.value));
      contact.current!.style.opacity = String(.6 * s.dock.value);
      mint.current!.style.opacity = String(.45 * s.mint.value + .55 * env);
      face.current!.setAttribute('transform', transform);
      const fx = 1 - .16 * Math.abs(s.gx.value), lidY = lid * s.squint.value, length = s.length.value * R;
      for (const [eye, line, side, rotation] of [[eyeL, lineL, -1, s.left.value], [eyeR, lineR, 1, s.right.value]] as const) {
        eye.current!.setAttribute('transform', `translate(${(side * s.sep.value + .17 * s.gx.value) * R} ${(s.ey.value + .13 * s.gy.value) * R}) scale(${fx} ${lidY}) rotate(${rotation})`);
        line.current!.setAttribute('x1', String(-length / 2)); line.current!.setAttribute('x2', String(length / 2));
        line.current!.setAttribute('stroke-width', String(s.width.value * R));
      }
      hit.current!.style.transform = `translate(${x - R - 4}px, ${y - R - 4}px) scale(${scale})`;
      if (hit.current!.dataset.place !== shown) hit.current!.dataset.place = shown;
      // She can slide under a resting cursor; hit testing must follow her, not only the mouse.
      if (moving) moved.current();
      if (!atHome || moving || gazing || blinkStart >= 0 || glance || now < switchAt) frame = requestAnimationFrame(draw);
      else sleep ??= setTimeout(() => { sleep = undefined; glanceUntil = performance.now() + 1500; wake.current(); }, 22000 + Math.random() * 18000);
    };
    wake.current = () => {
      if (sleep) { clearTimeout(sleep); sleep = undefined; }
      if (!frame) { last = 0; frame = requestAnimationFrame(draw); }
    };
    handle.current = {
      nudge: () => { s.gy.velocity += 1.6; wake.current(); },
      // A new screen: already in its island, with a small bump out of it and a look around.
      arrive: () => {
        const a = latest.current.anchors.home;
        Object.assign(s.x, { value: a.x, velocity: 0 }); Object.assign(s.y, { value: a.y, velocity: 140 });
        Object.assign(s.scale, { value: HOME_SCALE, velocity: 0 }); Object.assign(s.shine, { value: 0, velocity: 0 });
        wanted = shown = 'home'; switchAt = 0; glanceUntil = performance.now() + 1400;
        wake.current();
      },
    };
    wake.current();
    return () => { cancelAnimationFrame(frame); clearTimeout(sleep); wake.current = () => {}; handle.current = null; };
  }, []);
  const lobeD = lobePath(lobe);
  return <>
    <svg className="companion-stage" width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <defs>
        {/* Blur plus an alpha threshold: the island and the ball join like one liquid body. */}
        <filter id="cb-goo" x="-40%" y="-60%" width="180%" height="220%" colorInterpolationFilters="sRGB">
          <feGaussianBlur stdDeviation="4"/>
          <feColorMatrix values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 18 -8"/>
        </filter>
        <filter id="cb-blur-5" x="-60%" y="-60%" width="220%" height="220%"><feGaussianBlur stdDeviation="5"/></filter>
        <filter id="cb-blur-2" x="-60%" y="-100%" width="220%" height="300%"><feGaussianBlur stdDeviation="2"/></filter>
        <filter id="cb-glow" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="1.3"/></filter>
        <filter id="cb-soften" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation=".45"/></filter>
        <radialGradient id="cb-body" gradientUnits="userSpaceOnUse" cx={-.28 * R} cy={-.38 * R} r={1.5 * R}>
          <stop offset="0" stopColor="#34373f"/><stop offset=".18" stopColor="#1b1d22"/><stop offset=".45" stopColor="#0a0b0d"/>
          <stop offset=".75" stopColor="#030304"/><stop offset="1" stopColor="#000"/>
        </radialGradient>
        {/* Glass thickness catches the room along the lower edge. */}
        <radialGradient id="cb-rim" gradientUnits="userSpaceOnUse" cx="0" cy={-.3 * R} r={1.3 * R}>
          <stop offset=".74" stopColor="#96a0b4" stopOpacity="0"/><stop offset=".9" stopColor="#96a0b4" stopOpacity=".1"/>
          <stop offset="1" stopColor="#cdd4e4" stopOpacity=".36"/>
        </radialGradient>
        <radialGradient id="cb-sheen"><stop offset="0" stopColor="#fff" stopOpacity=".5"/><stop offset="1" stopColor="#fff" stopOpacity="0"/></radialGradient>
        <linearGradient id="cb-edge" gradientUnits="userSpaceOnUse" x1={-R} y1="0" x2={.4 * R} y2={-R}>
          <stop offset="0" stopColor="#fff" stopOpacity="0"/><stop offset=".45" stopColor="#fff" stopOpacity=".3"/><stop offset="1" stopColor="#fff" stopOpacity="0"/>
        </linearGradient>
        <linearGradient id="cb-fade" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#000"/><stop offset="1" stopColor="#000" stopOpacity="0"/></linearGradient>
        {/* The island hides the glass above its edge; the fade keeps that edge from reading as a seam. */}
        <mask id="cb-mask" maskUnits="userSpaceOnUse" x="0" y="0" width={width} height={height}>
          <rect width={width} height={height} fill="#fff"/>
          <path d={lobeD} fill="#000"/>
          <rect x={lobe.left} y={lobe.height} width={lobe.right - lobe.left} height="9" fill="url(#cb-fade)"/>
        </mask>
      </defs>
      <g filter="url(#cb-goo)"><path d={lobeD}/><circle ref={silhouette} r={R}/></g>
      <g mask="url(#cb-mask)">
        <g ref={body} opacity="0">
          <circle ref={float} cy={.28 * R} r={.92 * R} fill="#000" filter="url(#cb-blur-5)"/>
          <ellipse ref={contact} cy={.98 * R} rx={.72 * R} ry={.13 * R} fill="#000" opacity="0" filter="url(#cb-blur-2)"/>
          <circle r={R} fill="url(#cb-body)"/>
          <circle r={R} fill="url(#cb-rim)"/>
          <ellipse cx={-.34 * R} cy={-.46 * R} rx={.4 * R} ry={.24 * R} transform={`rotate(-32 ${-.34 * R} ${-.46 * R})`} fill="url(#cb-sheen)"/>
          <ellipse cx={-.47 * R} cy={-.56 * R} rx={.11 * R} ry={.06 * R} transform={`rotate(-38 ${-.47 * R} ${-.56 * R})`} fill="#fff" opacity=".85" filter="url(#cb-soften)"/>
          <path d={arc(R - .8, 195, 290)} fill="none" stroke="url(#cb-edge)" strokeWidth=".9" strokeLinecap="round"/>
          <g ref={mint} opacity="0" fill="none" strokeLinecap="round" style={{ stroke: 'var(--mint)' }}>
            <path d={arc(R - 1.4, 100, 145)} strokeWidth="3.5" opacity=".6" filter="url(#cb-glow)"/>
            <path d={arc(R - 1.4, 100, 145)} strokeWidth="1.2" opacity=".85"/>
          </g>
        </g>
      </g>
      <g ref={face}>
        <use href="#cb-eyes" filter="url(#cb-glow)" opacity=".5"/>
        <g id="cb-eyes" stroke="#f4f1ea" strokeLinecap="round">
          <g ref={eyeL}><line ref={lineL}/></g><g ref={eyeR}><line ref={lineR}/></g>
        </g>
      </g>
    </svg>
    <button ref={hit} className="companion-hit" data-hit aria-label={label} style={{ width: 2 * R + 8, height: 2 * R + 8 }}
      onPointerDown={event => { if (event.button !== 0) return; event.currentTarget.setPointerCapture(event.pointerId); onPress(); }}
      onPointerUp={onRelease} onPointerCancel={onCancel} onLostPointerCapture={onCancel}
      onClick={event => { if (event.detail === 0) { onPress(); onRelease(); } }}/>
  </>;
}

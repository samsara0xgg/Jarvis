import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { skyline } from './islandShape';
import { spring, step } from './starCore';
import { MOTION, SPRINGS } from './motion';

// The island reaches toward a detached Dashboard inside the native docking area.
export function DockingDrop({ near, width, top, center }: { near: boolean; width: number; top: number; center: number }) {
  const path = useRef<SVGPathElement>(null), phase = useRef(spring(0));
  const latest = useRef({ near, top, center }), wake = useRef(() => {});
  latest.current = { near, top, center };
  useLayoutEffect(() => {
    const el = path.current!, reduced = matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0, last = 0;
    const draw = (now: number) => {
      frame = 0;
      const { near: on, top: y, center: x } = latest.current;
      const dt = Math.min(last ? (now - last) / 1000 : 1 / 60, .05); last = now;
      if (!on) phase.current.velocity = Math.min(0, phase.current.velocity);
      if (reduced.matches) { phase.current.value = Number(on); phase.current.velocity = 0; }
      const moving = !reduced.matches && step(phase.current, Number(on), SPRINGS.panel.frequency / (on ? 1 : MOTION.exit), SPRINGS.panel.damping, dt);
      const h = 22 * Math.max(0, phase.current.value);
      el.setAttribute('d', h < .01 ? '' : `M ${x - 70} ${y} C ${x - 28} ${y},${x - 24.5} ${y + h},${x} ${y + h} C ${x + 24.5} ${y + h},${x + 28} ${y},${x + 70} ${y} Z`);
      el.dataset.depth = String(h);
      if (moving) frame = requestAnimationFrame(draw);
    };
    wake.current = () => { if (!frame) { last = 0; frame = requestAnimationFrame(draw); } };
    const changed = () => wake.current();
    reduced.addEventListener('change', changed); wake.current();
    return () => { cancelAnimationFrame(frame); reduced.removeEventListener('change', changed); wake.current = () => {}; };
  }, []);
  useLayoutEffect(() => wake.current(), [near, top, center]);
  return <svg className="dashboard-docking-drop" width={width} height={top + 24} aria-hidden="true" focusable="false"><path ref={path} fill="#000"/></svg>;
}

// One opaque outline grows from the menu bar. Colour starts below its black
// header, so neither the hardware cutout nor a translucent seam can show through.
export function DuskDashboard({ open, top, width, left, islandLeft, islandRight, lightX, children, detached = false, onDetach }: {
  open: boolean; top: number; width: number; left: number; islandLeft: number; islandRight: number; lightX: number; children: ReactNode; detached?: boolean; onDetach?: () => void;
}) {
  const root = useRef<HTMLDivElement>(null), content = useRef<HTMLDivElement>(null), outline = useRef<SVGPathElement>(null);
  const latest = useRef(open), wake = useRef(() => {});
  const drag = useRef<{ id: number; y: number; tear: number; native: boolean } | null>(null), busy = useRef(false);
  const [error, setError] = useState('');
  latest.current = open;
  const detach = async (dragging = false) => {
    if (busy.current || !window.jarvis?.dashboard || detached) return;
    busy.current = true; setError('');
    try { onDetach?.(); await window.jarvis.dashboard('detach', { height: root.current?.offsetHeight, dragging }); if (drag.current) drag.current.native = true; }
    catch { setError('Could not open the Dashboard window. Try again.'); }
    finally { busy.current = false; if (drag.current) drag.current.tear = 0; wake.current(); }
  };
  useLayoutEffect(() => {
    const el = root.current!, body = content.current!, path = outline.current!, height = spring(0);
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0, last = 0, goal = el.offsetHeight;
    const draw = (now: number) => {
      frame = 0;
      const on = latest.current, dt = Math.min(last ? (now - last) / 1000 : 1 / 60, .05); last = now;
      const moving = reduced.matches ? false : step(height, on ? goal : 0, SPRINGS.panel.frequency / (on ? 1 : MOTION.exit), SPRINGS.panel.damping, dt);
      if (reduced.matches) height.value = on ? goal : 0;
      const h = Math.max(0, height.value), shown = h > .5, tear = drag.current?.tear ?? 0;
      const neck = Math.max(90, 300 - tear / 52 * 210), cx = left + 180, y = top + tear;
      const torn = `${skyline([{ l: islandLeft, r: islandRight, d: top }])} M ${cx - 150} ${top - 3} C ${cx - 150} ${y - 8},${cx - neck / 2} ${y - 8},${cx - neck / 2} ${y + 4} L ${cx + neck / 2} ${y + 4} C ${cx + neck / 2} ${y - 8},${cx + 150} ${y - 8},${cx + 150} ${top - 3} Z`;
      path.setAttribute('d', detached || !shown ? '' : tear > 0 ? torn : skyline([{ l: islandLeft, r: islandRight, d: top }, { l: left, r: left + 360, d: top + h }]));
      el.style.visibility = shown ? 'visible' : 'hidden';
      body.style.clipPath = `inset(0 0 ${Math.max(0, goal - h)}px 0 round 0 0 24px 24px)`;
      body.style.opacity = on && h >= goal * .6 ? '1' : '0';
      el.style.setProperty('--dusk-height', `${h}px`);
      el.style.setProperty('--dusk-tear', `${tear}px`);
      el.dataset.reveal = String(h);
      if (moving) frame = requestAnimationFrame(draw);
    };
    wake.current = () => { if (!frame) { last = 0; frame = requestAnimationFrame(draw); } };
    const measure = () => { goal = el.offsetHeight; if (detached) window.jarvis?.dashboardSize?.(Math.max(goal, window.innerHeight)); wake.current(); };
    const observer = new ResizeObserver(measure); observer.observe(el); measure();
    return () => { observer.disconnect(); cancelAnimationFrame(frame); wake.current = () => {}; };
  }, [top, width, left, islandLeft, islandRight, detached]);
  useLayoutEffect(() => wake.current(), [open]);
  useLayoutEffect(() => {
    const el = root.current!;
    const tear = () => { if (latest.current) void detach(); };
    const key = (event: KeyboardEvent) => {
      if (!latest.current || !event.metaKey || !event.shiftKey || event.repeat || !['ArrowDown', 'ArrowUp'].includes(event.key)) return;
      if (event.key === 'ArrowDown' && !detached) { event.preventDefault(); void detach(); }
      if (event.key === 'ArrowUp' && detached) { event.preventDefault(); void window.jarvis?.dashboard?.('attach'); }
    };
    el.addEventListener('dashboard-detach', tear); window.addEventListener('keydown', key);
    return () => { el.removeEventListener('dashboard-detach', tear); window.removeEventListener('keydown', key); };
  }, [detached]);
  const finish = () => {
    if (!drag.current) return;
    if (drag.current.native) window.jarvis?.dashboardDrag?.('end');
    drag.current = null; wake.current();
  };
  return <div ref={root} className={`companion-dashboard ${open ? 'is-open' : ''} ${detached ? 'is-detached' : ''}`} data-hit={open || undefined} inert={!open}
    style={{ left, top, '--dusk-light-x': `${lightX - left + 40}px` } as CSSProperties}
    onPointerDown={event => {
      if (event.button !== 0 || !window.jarvis?.dashboard || !(event.target as Element).closest('.corner,.pg-head') || (event.target as Element).closest('button,input,textarea,a,label')) return;
      event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId);
      drag.current = { id: event.pointerId, y: event.clientY, tear: 0, native: detached };
      if (detached) window.jarvis.dashboardDrag?.('start');
    }}
    onPointerMove={event => {
      const active = drag.current; if (!active || active.id !== event.pointerId) return;
      if (active.native) { window.jarvis?.dashboardDrag?.('move'); return; }
      const dy = Math.max(0, event.clientY - active.y);
      active.tear = 70 * Math.log1p(Math.min(dy, 78) / 70); wake.current();
      if (dy > 78) void detach(true);
    }} onPointerUp={finish} onPointerCancel={finish} onLostPointerCapture={() => { if (!busy.current && !drag.current?.native) finish(); }}>
    <svg className="dusk-outline" width={width} height="100%" style={{ left: -left, top: -top, height: `calc(100% + ${top}px + 20px)` }} aria-hidden="true"><path ref={outline} fill="#000"/></svg>
    <div className="dusk-material" aria-hidden="true"/>
    <div ref={content} className="dusk-content">{children}</div>
    {error && <p className="dashboard-window-error" role="alert">{error}</p>}
  </div>;
}

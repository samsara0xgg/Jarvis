import { useEffect, useRef, type RefObject } from 'react';

type Target = { open: boolean; wide: boolean; selected: number | null; slow: boolean };
type Spring = { value: number; velocity: number };

// Geometry, not scale: text keeps its actual raster size during every morph.
// The same springs survive target changes, including a reversal before settling.
export function useDashboardMotion(root: RefObject<HTMLDivElement | null>, target: Target) {
  const latest = useRef(target);
  const wake = useRef(() => {});
  useEffect(() => { latest.current = target; if (root.current) root.current.dataset.motion = 'moving'; wake.current(); }, [target.open, target.wide, target.selected, target.slow, root]);
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const surface = el.querySelector<HTMLElement>('.dashboard-surface')!;
    const board = el.querySelector<HTMLElement>('.dashboard-board')!;
    const cards = [...el.querySelectorAll<HTMLElement>('.dashboard-module')];
    const springs = new Map<string, Spring>();
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0, last = 0, moving = false;
    const step = (key: string, goal: number, dt: number, tension = 15) => {
      let s = springs.get(key);
      if (!s) { s = { value: goal, velocity: 0 }; springs.set(key, s); }
      const offset = s.value - goal;
      const momentum = s.velocity + tension * offset;
      const decay = Math.exp(-tension * dt);
      s.value = goal + (offset + momentum * dt) * decay;
      s.velocity = (s.velocity - tension * momentum * dt) * decay;
      if (reduced.matches || (Math.abs(s.value - goal) < .015 && Math.abs(s.velocity) < .04)) { s.value = goal; s.velocity = 0; }
      else moving = true;
      return s.value;
    };
    const draw = (now: number) => {
      frame = 0;
      if (document.hidden) { last = 0; return; }
      const t = latest.current;
      const dt = Math.min(last ? (now - last) / 1000 : 1 / 60, .05) / (t.slow ? 3 : 1);
      last = now; moving = false;
      const compact = !!el.closest('.compact-dashboard');
      const width = step('width', el.closest('.embedded-dashboard') ? el.clientWidth : Math.min(t.wide ? (compact ? 330 : 600) : (compact ? 282 : 470), el.clientWidth), dt);
      const narrow = width < 370;
      const height = step('height', t.selected === null ? (compact ? 206 : narrow ? 340 : 300) : (compact ? 296 : t.wide ? 414 : 366), dt);
      const opening = step('opening', Number(t.open), dt, 17);
      const innerWidth = width - (compact ? 24 : 34);
      const gap = compact ? 8 : 10;
      const cellWidth = (innerWidth - gap) / 2;
      // Base positions remain stable while the selected module grows over them.
      const baseHeight = compact ? 206 : narrow ? 340 : 300;
      const cellHeight = (baseHeight - gap) / 2;
      surface.style.width = `${width}px`;
      surface.style.opacity = `${opening}`;
      surface.style.transform = `translateY(${(1 - opening) * -14}px)`;
      surface.style.clipPath = `inset(0 0 ${(1 - opening) * 100}% 0 round 26px)`;
      surface.style.visibility = opening === 0 ? 'hidden' : 'visible';
      board.style.height = `${height}px`;
      cards.forEach((card, i) => {
        const active = t.selected === i;
        const x = step(`${i}.x`, active ? 0 : (i % 2) * (cellWidth + gap), dt);
        const y = step(`${i}.y`, active ? 0 : Math.floor(i / 2) * (cellHeight + gap), dt);
        const w = step(`${i}.w`, active ? innerWidth : cellWidth, dt);
        const h = step(`${i}.h`, active ? height : cellHeight, dt);
        const opacity = step(`${i}.opacity`, t.selected === null || active ? 1 : 0, dt, 22);
        const detail = step(`${i}.detail`, Number(active), dt, 17);
        card.style.transform = `translate(${x}px, ${y}px)`;
        card.style.width = `${w}px`; card.style.height = `${h}px`;
        card.style.opacity = `${opacity}`;
        card.style.setProperty('--detail-progress', String(detail));
        // Keep the expanding/returning tile above its siblings until it settles.
        card.style.zIndex = detail > .001 || active ? '2' : '1';
      });
      el.dataset.motion = moving ? 'moving' : 'settled';
      el.dataset.openProgress = opening.toFixed(4);
      if (moving) frame = requestAnimationFrame(draw);
      else last = 0;
    };
    const resume = () => { if (!frame) frame = requestAnimationFrame(draw); };
    wake.current = resume;
    const observer = new ResizeObserver(resume); observer.observe(el);
    document.addEventListener('visibilitychange', resume);
    reduced.addEventListener('change', resume);
    resume();
    return () => { cancelAnimationFrame(frame); observer.disconnect(); document.removeEventListener('visibilitychange', resume); reduced.removeEventListener('change', resume); wake.current = () => {}; };
  }, [root]);
}

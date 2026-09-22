import { useLayoutEffect, useRef, type RefObject } from 'react';

type Spring = { value: number; velocity: number };
const advance = (state: Spring, goal: number, dt: number, tension: number, precision = .0005) => {
  const offset = state.value - goal, momentum = state.velocity + tension * offset;
  const decay = Math.exp(-tension * dt);
  state.value = goal + (offset + momentum * dt) * decay;
  state.velocity = (state.velocity - tension * momentum * dt) * decay;
  if (Math.abs(state.value - goal) < precision && Math.abs(state.velocity) < precision * 2) { state.value = goal; state.velocity = 0; }
};

// Retain geometry and velocity across reversals. Reserve space before fading
// full-size text in; fade text out before releasing space, never a text curtain.
export function usePanelMotion(root: RefObject<HTMLElement | null>, content: RefObject<HTMLDivElement | null>, open: boolean, collapsed: boolean) {
  const target = useRef({ open, collapsed });
  const wake = useRef(() => {});
  useLayoutEffect(() => { target.current = { open, collapsed }; wake.current(); }, [open, collapsed]);
  useLayoutEffect(() => {
    const el = root.current!, inner = content.current!, body = inner.querySelector<HTMLElement>('.stack-body')!;
    const h: Spring = { value: 0, velocity: 0 }, alpha: Spring = { value: 0, velocity: 0 }, bodyAlpha: Spring = { value: 0, velocity: 0 };
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0, last = 0;
    let bodyRevealed = false;
    const draw = (now: number) => {
      frame = 0;
      const dt = Math.min(last ? (now - last) / 1000 : 1 / 60, .04); last = now;
      const t = target.current, height = t.open ? t.collapsed ? 36 : inner.scrollHeight : 0;
      const expanding = height >= h.value;
      // The dashboard and transcript children stay mounted and visually open.
      // This layer is the sole owner of their entry/exit and collapse motion.
      const alphaGoal = t.open && (h.value >= Math.min(height, 36) - .5) ? 1 : 0;
      if (!t.open || t.collapsed) bodyRevealed = false;
      const bodyGoal = t.open && !t.collapsed && (bodyRevealed || h.value >= height - .5) ? 1 : 0;
      // Once shown, child geometry changes resize this shell without replaying entry.
      if (bodyGoal) bodyRevealed = true;
      if (reduced.matches) {
        h.value = height; alpha.value = Number(t.open); bodyAlpha.value = Number(t.open && !t.collapsed);
        h.velocity = alpha.velocity = bodyAlpha.velocity = 0;
      } else {
        advance(alpha, alphaGoal, dt, 24);
        advance(bodyAlpha, bodyGoal, dt, 30);
        const mayShrink = t.open ? !t.collapsed || bodyAlpha.value < .015 : alpha.value < .015;
        if (expanding || mayShrink) advance(h, height, dt, 28, .08);
      }
      const settled = Math.abs(h.value - height) < .1 && Math.abs(alpha.value - Number(t.open)) < .001 && Math.abs(bodyAlpha.value - Number(t.open && !t.collapsed)) < .001;
      if (settled) { h.value = height; alpha.value = Number(t.open); bodyAlpha.value = Number(t.open && !t.collapsed); h.velocity = alpha.velocity = bodyAlpha.velocity = 0; }
      el.style.height = `${h.value}px`;
      inner.style.opacity = String(alpha.value);
      inner.style.transform = `translateY(${(1 - alpha.value) * -6}px)`;
      body.style.opacity = String(bodyAlpha.value);
      body.style.transform = `translateY(${(1 - bodyAlpha.value) * -6}px)`;
      el.dataset.motion = settled ? 'settled' : 'moving';
      // Border and native glass survive the entire exit, including the last row.
      const stack = el.closest<HTMLElement>('.panel-stack')!;
      const occupied = [...stack.querySelectorAll<HTMLElement>('.stack-module')].reduce((sum, node) => sum + node.getBoundingClientRect().height, 0);
      stack.style.setProperty('--stack-presence', String(Math.min(1, occupied / 36)));
      el.dispatchEvent(new Event('capsule-motion', { bubbles: true }));
      if (!settled) frame = requestAnimationFrame(draw); else last = 0;
    };
    const resume = () => { el.dataset.motion = 'moving'; if (!frame) frame = requestAnimationFrame(draw); };
    wake.current = resume;
    const observer = new ResizeObserver(resume); observer.observe(inner);
    reduced.addEventListener('change', resume);
    resume();
    return () => { cancelAnimationFrame(frame); observer.disconnect(); reduced.removeEventListener('change', resume); wake.current = () => {}; };
  }, [root, content]);
}

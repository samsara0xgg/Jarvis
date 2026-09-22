import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react';

type Target = { open: boolean; wide: boolean; selected: number | null; slow: boolean; providerLayout: boolean };
type Spring = { value: number; velocity: number };

// Geometry, not scale: text keeps its actual raster size during every morph.
// The same springs survive target changes, including a reversal before settling.
export function useDashboardMotion(root: RefObject<HTMLDivElement | null>, target: Target) {
  const latest = useRef(target);
  const wake = useRef(() => {});
  useEffect(() => { latest.current = target; if (root.current) root.current.dataset.motion = 'moving'; wake.current(); }, [target.open, target.wide, target.selected, target.slow, target.providerLayout, root]);
  useLayoutEffect(() => {
    const el = root.current;
    if (!el) return;
    const surface = el.querySelector<HTMLElement>('.dashboard-surface')!;
    const board = el.querySelector<HTMLElement>('.dashboard-board')!;
    const notifications = el.querySelector<HTMLElement>('.dashboard-notification-reveal');
    const notificationContent = el.querySelector<HTMLElement>('.dashboard-notifications');
    const homeBar = el.querySelector<HTMLElement>('.dashboard-home-bar');

    const quotaSummary = el.querySelector<HTMLElement>('.overview-usage');
    const cards = [...el.querySelectorAll<HTMLElement>('.dashboard-module')];
    const springs = new Map<string, Spring>();
    const embedded = !!el.closest('.embedded-dashboard');
    const reveal = { value: embedded ? 0 : Number(latest.current.open), from: embedded ? 0 : Number(latest.current.open), goal: embedded ? 0 : Number(latest.current.open), elapsed: .2 };
    let frame = 0, last = 0, moving = false;
    const step = (key: string, goal: number, dt: number, tension = 15) => {
      let s = springs.get(key);
      if (!s) { s = { value: goal, velocity: 0 }; springs.set(key, s); }
      const offset = s.value - goal;
      const momentum = s.velocity + tension * offset;
      const decay = Math.exp(-tension * dt);
      s.value = goal + (offset + momentum * dt) * decay;
      s.velocity = (s.velocity - tension * momentum * dt) * decay;
      if (Math.abs(s.value - goal) < .015 && Math.abs(s.velocity) < .04) { s.value = goal; s.velocity = 0; }
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
      const overviewHeight = compact ? 108 + Math.max(120, (quotaSummary?.offsetHeight ?? 132) + 24) : narrow ? 340 : 300;
      const accordion = el.querySelector<HTMLElement>('.quota-accordion');
      const quotaHeight = accordion ? Math.min(420, accordion.offsetHeight + 38) : t.providerLayout ? 365 : 420;
      const height = step('height', t.selected === null ? overviewHeight : (t.selected === 1 ? 286 : t.selected === 2 ? quotaHeight : compact ? 296 : t.wide ? 414 : 366), dt);
      const goal = Number(t.open);
      if (reveal.goal !== goal) { reveal.from = reveal.value; reveal.goal = goal; reveal.elapsed = 0; }
      reveal.elapsed = Math.min(.2, reveal.elapsed + dt);
      const eased = 1 - Math.pow(1 - reveal.elapsed / .2, 3);
      reveal.value = reveal.from + (reveal.goal - reveal.from) * eased;
      const opening = reveal.value;
      if (reveal.elapsed < .2) moving = true;
      const innerWidth = width - (compact ? 24 : 34);
      const gap = compact ? 8 : 10;
      const cellWidth = (innerWidth - gap) / 2;
      // Base positions remain stable while the selected module grows over them.
      const baseHeight = overviewHeight;
      const topHeight = compact ? 100 : (baseHeight - gap) / 2;
      const bottomHeight = baseHeight - gap - topHeight;
      surface.style.width = `${width}px`;
      // Embedded appearance is owned by PanelStack; this hook only morphs tiles.
      surface.style.opacity = embedded ? '1' : String(opening);
      surface.style.transform = embedded ? 'none' : `translateY(${(1 - opening) * -6}px)`;
      surface.style.clipPath = 'none';
      surface.style.visibility = opening === 0 ? 'hidden' : 'visible';
      board.style.height = `${height}px`;
      if (homeBar) {
        homeBar.style.opacity = String(step('returnOpacity', Number(t.selected !== null), dt));
        homeBar.style.visibility = t.selected === null ? 'hidden' : 'visible';
      }
      if (notifications && notificationContent) {
        const notificationHeight = step('notificationHeight', t.selected === null && notificationContent.offsetHeight ? notificationContent.offsetHeight + 10 : 0, dt);
        notifications.style.height = `${notificationHeight}px`;
        notifications.style.opacity = String(step('notificationOpacity', Number(t.selected === null), dt, 22));
      }
      cards.forEach((card, i) => {
        const active = t.selected === i;
        const x = step(`${i}.x`, active ? 0 : (i % 2) * (cellWidth + gap), dt);
        const y = step(`${i}.y`, active ? 0 : Math.floor(i / 2) * (topHeight + gap), dt);
        const w = step(`${i}.w`, active ? innerWidth : cellWidth, dt);
        const h = step(`${i}.h`, active ? height : i < 2 ? topHeight : bottomHeight, dt);
        const opacity = step(`${i}.opacity`, t.selected === null || active ? 1 : 0, dt, 22);
        // Content follows the actual card width, not a separate fade clock.
        const detail = Math.max(0, Math.min(1, (w - cellWidth) / Math.max(1, innerWidth - cellWidth)));
        card.style.transform = `translate(${x}px, ${y}px)`;
        card.style.width = `${w}px`; card.style.height = `${h}px`;
        card.style.opacity = `${opacity}`;
        card.style.setProperty('--detail-progress', String(detail));
        // Keep the expanding/returning tile above its siblings until it settles.
        card.style.zIndex = detail > .001 || active ? '2' : '1';
      });
      if (embedded) {
        // Report natural content height to the outer retained motion layer.
        el.style.height = `${surface.offsetHeight}px`;
        el.style.overflow = 'hidden';
        el.dispatchEvent(new Event('capsule-motion', { bubbles: true }));
      }
      el.dataset.motion = moving ? 'moving' : 'settled';
      el.dataset.openProgress = opening.toFixed(4);
      if (moving) frame = requestAnimationFrame(draw);
      else last = 0;
    };
    const resume = () => { if (!frame) frame = requestAnimationFrame(draw); };
    wake.current = resume;
    const observer = new ResizeObserver(resume); observer.observe(el);
    if (quotaSummary) observer.observe(quotaSummary);
    if (notificationContent) observer.observe(notificationContent);
    const observeQuota = () => {
      el.querySelectorAll('.quota-accordion').forEach(node => observer.observe(node));
      resume();
    };
    const contentObserver = new MutationObserver(observeQuota);
    contentObserver.observe(board, { childList: true, subtree: true });
    el.querySelectorAll('.quota-accordion').forEach(node => observer.observe(node));
    document.addEventListener('visibilitychange', resume);
    draw(performance.now());
    return () => { cancelAnimationFrame(frame); observer.disconnect(); contentObserver.disconnect(); document.removeEventListener('visibilitychange', resume); wake.current = () => {}; };
  }, [root]);
}

import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react';

type Target = { open: boolean; wide: boolean; selected: number | null; slow: boolean; providerLayout: boolean; scrollOffset: number };
type Spring = { value: number; velocity: number };

// Geometry, not scale: text keeps its actual raster size during every morph.
// The same springs survive target changes, including a reversal before settling.
export function useDashboardMotion(root: RefObject<HTMLDivElement | null>, target: Target) {
  const latest = useRef(target);
  const wake = useRef(() => {});
  useEffect(() => { latest.current = target; if (root.current) root.current.dataset.motion = 'moving'; wake.current(); }, [target.open, target.wide, target.selected, target.slow, target.providerLayout, target.scrollOffset, root]);
  useLayoutEffect(() => {
    const el = root.current;
    if (!el) return;
    const surface = el.querySelector<HTMLElement>('.dashboard-surface')!;
    const board = el.querySelector<HTMLElement>('.dashboard-board')!;
    const viewport = el.querySelector<HTMLElement>('.dashboard-viewport')!;
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
      // Fixed two-column grid: content never changes a tile's dimensions.
      const docked = !!el.closest('.notch-docked');
      const width = docked ? (Number.parseFloat(getComputedStyle(el).getPropertyValue('--notch-width')) || 540) - 44 : 300;
      const gap = 8, cellWidth = (width - 24 - gap) / 2, innerWidth = cellWidth * 2 + gap;
      const cellHeight = docked ? Math.max(70, (Number.parseFloat(getComputedStyle(el).getPropertyValue('--notch-content-height')) - 112) / 3) : cellWidth;
      const overviewHeight = cellHeight * 3 + gap * 2;
      const accordion = el.querySelector<HTMLElement>('.quota-accordion');
      const quotaHeight = accordion ? Math.min(420, accordion.offsetHeight + 38) : t.providerLayout ? 365 : 420;
      const height = step('height', t.selected === null || t.selected === 4 ? overviewHeight : (t.selected === 1 ? 286 : t.selected === 2 ? quotaHeight : compact ? 296 : t.wide ? 414 : 366), dt);
      const goal = Number(t.open);
      if (reveal.goal !== goal) { reveal.from = reveal.value; reveal.goal = goal; reveal.elapsed = 0; }
      reveal.elapsed = Math.min(.2, reveal.elapsed + dt);
      const eased = 1 - Math.pow(1 - reveal.elapsed / .2, 3);
      reveal.value = reveal.from + (reveal.goal - reveal.from) * eased;
      const opening = reveal.value;
      if (reveal.elapsed < .2) moving = true;
      el.style.setProperty('--dashboard-cell', `${cellWidth}px`);
      el.style.setProperty('--dashboard-gap', `${gap}px`);
      surface.style.width = `${width}px`;
      // Embedded appearance is owned by PanelStack; this hook only morphs tiles.
      surface.style.opacity = embedded ? '1' : String(opening);
      surface.style.transform = embedded ? 'none' : `translateY(${(1 - opening) * -6}px)`;
      surface.style.clipPath = 'none';
      surface.style.visibility = opening === 0 ? 'hidden' : 'visible';
      // Keep the viewport's scroll origin while a lower tile expands in place.
      const contentHeight = t.selected === null ? cards.length > 4 ? cellHeight * 4 + gap * 3 : overviewHeight : height + t.scrollOffset;
      board.style.height = `${contentHeight}px`;
      viewport.style.height = `${height}px`;
      viewport.style.overflowY = t.selected === null ? 'auto' : 'hidden';
      if (t.selected !== null) viewport.scrollTop = t.scrollOffset;
      if (homeBar) {
        homeBar.style.opacity = String(step('returnOpacity', Number(t.selected !== null), dt));
        homeBar.style.visibility = t.selected === null ? 'hidden' : 'visible';
      }
      if (notifications && notificationContent) {
        const notificationHeight = step('notificationHeight', t.selected === null && notificationContent.offsetHeight ? notificationContent.offsetHeight + 10 : 0, dt);
        notifications.style.height = `${notificationHeight}px`;
        notifications.style.opacity = String(step('notificationOpacity', Number(t.selected === null), dt, 22));
      }
      cards.forEach(card => {
        // Place by module id, not DOM order: the plugins cell is absent without a controller.
        const i = Number(card.dataset.module);
        const active = t.selected === i;
        const baseX = i === 2 || i === 5 ? cellWidth + gap : 0;
        const baseY = i === 4 || i === 5 ? 3 * (cellHeight + gap) : i === 1 ? 2 * (cellHeight + gap) : i === 3 ? cellHeight + gap : 0;
        const x = step(`${i}.x`, active ? 0 : baseX, dt);
        const y = step(`${i}.y`, active ? t.scrollOffset : baseY, dt);
        const w = step(`${i}.w`, active || i === 1 ? innerWidth : cellWidth, dt);
        const h = step(`${i}.h`, active ? height : i === 2 ? 2 * cellHeight + gap : cellHeight, dt);
        const opacity = step(`${i}.opacity`, t.selected === null || active ? 1 : 0, dt, 22);
        // The Codex summary already spans both columns, so width is not a detail signal.
        const detail = step(`${i}.detail`, Number(active), dt);
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

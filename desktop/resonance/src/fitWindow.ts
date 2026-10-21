// The companion's window follows what the page shows (ADR-free: a frame size, not a contract). The page is laid out on a
// fixed stage (720 wide, the notch's top inset plus 690 tall, set in electron/companion.ts); the window is that stage's
// top-left corner and only its right and bottom edges move, so nothing on screen ever shifts when it resizes.
// Grow at once and ahead of the content; shrink only after the content has held still for SETTLE_MS.
type Box = { w: number; h: number };
// Which parts count as visible. A part reaches past its box only by its own outer box-shadow, read from its computed style
// (SVG paths and the Dashboard's flat black have none). `pad` overrides that where the drawing is on a canvas: the ball's glow and
// sparks. `always`: counted even when it looks hidden, so the island never needs a resize to fade in.
const PARTS: { sel: string; pad?: number; always?: boolean }[] = [
  { sel: '.companion-island-target', always: true },
  { sel: '.companion-hit', pad: 40, always: true },
  { sel: '.notch-shape path' },
  // The panes' inner box is their finished size even while the pane is still growing out of the island.
  { sel: '.notch-pane-in' },
  { sel: '.talk' },
  { sel: '.companion-dashboard' },
  { sel: '.companion-chip' },
  { sel: '.companion-menu' },
  { sel: '.dashboard-docking-drop path' },
];
const AHEAD_X = 120, AHEAD_Y = 200, SETTLE_MS = 600, SAVING = 2;

// How far the outer box-shadows of `el` reach past its right and bottom edges (a shadow's blur reaches about its radius).
function reach(el: Element) {
  let r = 0, b = 0;
  if (!(el instanceof HTMLElement)) return { r, b };
  const list = getComputedStyle(el).boxShadow;
  if (list === 'none') return { r, b };
  for (const one of list.split(/,(?![^(]*\))/)) {
    if (/\binset\b/.test(one)) continue;
    const [x, y, blur = 0, spread = 0] = (one.replace(/rgba?\([^)]*\)/, '').match(/-?[\d.]+px/g) ?? []).map(parseFloat);
    r = Math.max(r, x + blur + spread); b = Math.max(b, y + blur + spread);
  }
  return { r, b };
}

function measure(root: HTMLElement, full: Box): Box {
  // Dragging the finished mark out of the menu bar covers the whole stage with its catch layer; the puffs it leaves follow it.
  if (root.querySelector('.notch-catch')) return full;
  let w = 0, h = 0;
  for (const part of PARTS) for (const el of root.querySelectorAll<SVGGraphicsElement | HTMLElement>(part.sel)) {
    if (!part.always && !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const out = part.pad === undefined ? reach(el) : { r: part.pad, b: part.pad };
    // A pill's island has a 6 pt shoulder on its right; beside a notch that edge is under the camera.
    const shoulder = part.sel === '.companion-island-target' && Math.abs((r.left + r.right) / 2 - full.w / 2) < 1 ? 6 : 0;
    w = Math.max(w, r.right + out.r + shoulder); h = Math.max(h, r.bottom + out.b);
  }
  return { w: Math.min(full.w, Math.ceil(w)), h: Math.min(full.h, Math.ceil(h)) };
}

// `stage`: the whole stage's size now. `send`: asks the main process for a window of this size.
// It looks when the page may have changed, never on a timer of its own: after any change to the page (a style or a class set to the value it
// already had is not one), a resize, a transition or an animation (then every frame until the last one that ends has ended), a hover, a focus,
// a scroll, an image or a font. Returns `reset` (the main process put the whole stage back: shrink again once settled) and a stop function.
export function fitWindow(root: HTMLElement, stage: () => Box, send: (size: Box) => void) {
  let cur: Box = { w: Infinity, h: Infinity }, key = '', timer: ReturnType<typeof setTimeout> | undefined, frame = 0;
  // What asks for the viewport's height (the Dashboard's room, the notch canvas) still gets the stage's, not the window's.
  const real = Object.getOwnPropertyDescriptor(window, 'innerHeight');
  Object.defineProperty(window, 'innerHeight', { configurable: true, get: () => stage().h });
  const look = () => {
    const full = stage(), need = measure(root, full);
    if (need.w > cur.w || need.h > cur.h) {
      clearTimeout(timer); timer = undefined; key = '';
      cur = { w: need.w > cur.w ? Math.min(full.w, need.w + AHEAD_X) : cur.w, h: need.h > cur.h ? Math.min(full.h, need.h + AHEAD_Y) : cur.h };
      send(cur);
      return;
    }
    const now = `${need.w}x${need.h}`;
    if (now === key) return;
    key = now; clearTimeout(timer); timer = undefined;
    if (cur.w - need.w >= SAVING || cur.h - need.h >= SAVING) timer = setTimeout(() => { timer = undefined; cur = need; send(need); }, SETTLE_MS);
  };
  // Endless animations (a pulse) are not waited for: they stay where they were measured.
  const moving = () => root.getAnimations({ subtree: true }).some(a => a.playState === 'running' && a.effect?.getComputedTiming().endTime !== Infinity);
  const tick = () => { frame = 0; look(); if (moving()) schedule(); };
  const schedule = () => { if (!frame) frame = requestAnimationFrame(tick); };
  // Her goo (the stage's SVG) is drawn every frame she moves and sizes nothing.
  const changed = (records: MutationRecord[]) => {
    const matters = (r: MutationRecord) => r.type !== 'attributes' ? true : (r.target as Element).getAttribute(r.attributeName!) !== r.oldValue && !(r.target as Element).closest('.companion-stage');
    if (records.some(matters)) schedule();
  };
  const watch = new MutationObserver(changed);
  watch.observe(root, { subtree: true, childList: true, characterData: true, attributes: true, attributeOldValue: true });
  const events = ['transitionrun', 'transitionend', 'transitioncancel', 'animationstart', 'animationiteration', 'animationend', 'animationcancel', 'pointerover', 'pointerout', 'focusin', 'focusout', 'scroll', 'load', 'error'];
  for (const name of events) root.addEventListener(name, schedule, true);
  window.addEventListener('resize', schedule); document.fonts?.addEventListener('loadingdone', schedule);
  schedule();
  // The stage is taller than a fitted window, so a scrollIntoView anywhere could scroll the stage's own box (or the page) and lift the whole
  // stage under the notch (live 2026-10-07, the Memory page). The stage never scrolls.
  const pin = () => { if (root.scrollTop || root.scrollLeft) root.scrollTo(0, 0); if (window.scrollX || window.scrollY) window.scrollTo(0, 0); };
  root.addEventListener('scroll', pin); window.addEventListener('scroll', pin);
  return {
    reset: () => { cur = { w: Infinity, h: Infinity }; key = ''; schedule(); },
    stop: () => {
      cancelAnimationFrame(frame); frame = 0; clearTimeout(timer); watch.disconnect();
      for (const name of events) root.removeEventListener(name, schedule, true);
      window.removeEventListener('resize', schedule); document.fonts?.removeEventListener('loadingdone', schedule);
      root.removeEventListener('scroll', pin); window.removeEventListener('scroll', pin);
      if (real) Object.defineProperty(window, 'innerHeight', real); else delete (window as { innerHeight?: number }).innerHeight;
    },
  };
}

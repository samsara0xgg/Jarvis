// The companion's window follows what the page shows (ADR-free: a frame size, not a contract). The page is laid out on a
// fixed stage (640 wide, the notch's top inset plus 690 tall, set in electron/companion.ts); the window is that stage's
// top-left corner and only its right and bottom edges move, so nothing on screen ever shifts when it resizes.
// Grow at once and ahead of the content; shrink only after the content has held still for SETTLE_MS.
type Box = { w: number; h: number };
// Which parts count as visible, and how far each reaches past its box (right, bottom): shadows, glow, a ball's sparks.
// `always`: counted even when it looks hidden, so the island never needs a resize to fade in.
const PARTS: { sel: string; r: number; b: number; always?: boolean }[] = [
  { sel: '.companion-island-target', r: 8, b: 2, always: true },
  { sel: '.companion-hit', r: 40, b: 40, always: true },
  { sel: '.notch-shape path', r: 12, b: 12 },
  // The panes' inner box is their finished size even while the pane is still growing out of the island.
  { sel: '.notch-pane-in', r: 16, b: 24 },
  { sel: '.talk', r: 40, b: 64 },
  { sel: '.companion-dashboard', r: 24, b: 48 },
  { sel: '.companion-chip', r: 8, b: 8 },
  { sel: '.companion-menu', r: 24, b: 24 },
  { sel: '.dashboard-docking-drop path', r: 4, b: 4 },
];
const AHEAD_X = 120, AHEAD_Y = 200, SETTLE_MS = 600, STEP = 8;

function measure(root: HTMLElement, full: Box): Box {
  // Dragging the finished mark out of the menu bar covers the whole stage with its catch layer; the puffs it leaves follow it.
  if (root.querySelector('.notch-catch')) return full;
  let w = 0, h = 0;
  for (const part of PARTS) for (const el of root.querySelectorAll<SVGGraphicsElement | HTMLElement>(part.sel)) {
    if (!part.always && !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    w = Math.max(w, r.right + part.r); h = Math.max(h, r.bottom + part.b);
  }
  return { w: Math.min(full.w, Math.ceil(w / STEP) * STEP), h: Math.min(full.h, Math.ceil(h / STEP) * STEP) };
}

// `stage`: the whole stage's size now. `send`: asks the main process for a window of this size.
// Returns `reset` (the main process put the whole stage back: shrink again once settled) and a stop function.
export function fitWindow(root: HTMLElement, stage: () => Box, send: (size: Box) => void) {
  let cur: Box = { w: Infinity, h: Infinity }, key = '', timer: ReturnType<typeof setTimeout> | undefined, frame = 0;
  // What asks for the viewport's height (the Dashboard's room, the notch canvas) still gets the stage's, not the window's.
  const real = Object.getOwnPropertyDescriptor(window, 'innerHeight');
  Object.defineProperty(window, 'innerHeight', { configurable: true, get: () => stage().h });
  const tick = () => {
    frame = requestAnimationFrame(tick);
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
    if (cur.w - need.w >= 16 || cur.h - need.h >= 16) timer = setTimeout(() => { timer = undefined; cur = need; send(need); }, SETTLE_MS);
  };
  frame = requestAnimationFrame(tick);
  return {
    reset: () => { cur = { w: Infinity, h: Infinity }; key = ''; },
    stop: () => {
      cancelAnimationFrame(frame); clearTimeout(timer);
      if (real) Object.defineProperty(window, 'innerHeight', real); else delete (window as { innerHeight?: number }).innerHeight;
    },
  };
}

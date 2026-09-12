import { useEffect } from 'react';

// Match the visible rounded surface, including detached circles, rather than its
// transparent rectangular corners. Shadows intentionally do not receive input.
function visibleSurface(target: Element, x: number, y: number): HTMLElement | null {
  const surface = target.closest<HTMLElement>('[data-glass]');
  if (!surface) return null;
  const r = surface.getBoundingClientRect();
  if (x < r.left || x > r.right || y < r.top || y > r.bottom) return null;
  const radius = Math.min(Number(surface.dataset.glass), r.width / 2, r.height / 2);
  const cx = Math.max(r.left + radius, Math.min(x, r.right - radius));
  const cy = Math.max(r.top + radius, Math.min(y, r.bottom - radius));
  return Math.hypot(x - cx, y - cy) <= radius ? surface : null;
}

export function useCapsuleDrag(enabled: boolean) {
  useEffect(() => {
    if (!enabled || !window.jarvis) return;
    let gesture: { pointer: number; x: number; y: number; target: Element; moved: boolean } | null = null;
    let suppressClick = false;
    let passthrough: boolean | undefined;
    const hitTest = (e: MouseEvent) => {
      const target = e.target as Element;
      const glass = target.closest('[data-glass]');
      const hit = glass ? !!visibleSurface(target, e.clientX, e.clientY) : !!target.closest('button, textarea, input, select, .detail-body');
      const ignore = !gesture && !hit;
      if (ignore !== passthrough) { passthrough = ignore; window.jarvis!.passthrough(ignore); }
    };
    const down = (e: PointerEvent) => {
      if (e.button !== 0 || !e.isPrimary) return;
      suppressClick = false;
      const target = e.target as Element;
      if (target.closest('input, select, .detail-body') || !visibleSurface(target, e.clientX, e.clientY)) return;
      // Every capsule surface, including its buttons, accepts the same gesture.
      const capture = target.closest('button, textarea') ?? target.closest('[data-glass]')!;
      gesture = { pointer: e.pointerId, x: e.screenX, y: e.screenY, target: capture, moved: false };
      capture.setPointerCapture(e.pointerId);
      passthrough = false;
      window.jarvis!.passthrough(false);
      window.jarvis!.drag('start', { x: e.screenX, y: e.screenY });
    };
    const move = (e: PointerEvent) => {
      if (!gesture || e.pointerId !== gesture.pointer) return;
      if (Math.hypot(e.screenX - gesture.x, e.screenY - gesture.y) >= 4) {
        gesture.moved = true;
        suppressClick = true;
        document.documentElement.classList.add('dragging-capsule');
        window.jarvis!.drag('move', { x: e.screenX, y: e.screenY });
        e.preventDefault();
      }
    };
    const finish = (e?: PointerEvent) => {
      if (!gesture || (e && e.pointerId !== gesture.pointer)) return;
      const finished = gesture;
      // macOS can deliver capture loss at the release position before pointerup.
      const released = e?.type === 'pointerup' || (e?.type === 'lostpointercapture' && e.buttons === 0);
      suppressClick = finished.moved || !!(released && e && Math.hypot(e.screenX - finished.x, e.screenY - finished.y) >= 4);
      gesture = null;
      if (finished.target.hasPointerCapture(finished.pointer)) finished.target.releasePointerCapture(finished.pointer);
      document.documentElement.classList.remove('dragging-capsule');
      window.jarvis!.drag('end', released && e ? { x: e.screenX, y: e.screenY } : undefined);
    };
    const click = (e: MouseEvent) => {
      // detail=0 is keyboard/assistive activation and is never swallowed.
      if (suppressClick && e.detail !== 0) { e.preventDefault(); e.stopImmediatePropagation(); suppressClick = false; }
    };
    const stop = () => finish();
    window.addEventListener('pointerdown', down, true);
    window.addEventListener('pointermove', move, true);
    window.addEventListener('pointerup', finish, true);
    window.addEventListener('pointercancel', finish, true);
    window.addEventListener('lostpointercapture', finish, true);
    window.addEventListener('blur', stop);
    window.addEventListener('click', click, true);
    window.addEventListener('mousemove', hitTest);
    return () => {
      finish();
      window.removeEventListener('pointerdown', down, true);
      window.removeEventListener('pointermove', move, true);
      window.removeEventListener('pointerup', finish, true);
      window.removeEventListener('pointercancel', finish, true);
      window.removeEventListener('lostpointercapture', finish, true);
      window.removeEventListener('blur', stop);
      window.removeEventListener('click', click, true);
      window.removeEventListener('mousemove', hitTest);
    };
  }, [enabled]);
}

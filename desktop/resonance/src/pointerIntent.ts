export type PointerPoint = { x: number; y: number };
export type PointerRect = { left: number; right: number; top: number; bottom: number };

export const HOVER_DWELL_MS = 140;
export const HOVER_EXIT_MS = 280;
export const HOVER_SPEED = .25;
// The Dashboard: a rest on the island opens it; it folds once the pointer has been off it and the island this long.
export const DASHBOARD_DWELL_MS = 300;
export const DASHBOARD_EXIT_MS = 600;
const WINDOW_MS = 60;

// Sample on each animation frame, including a stationary pointer: stopping naturally
// ages movement out of the same 60 ms window used to reject a pass through the menu bar.
export class PointerIntent {
  private samples: (PointerPoint & { at: number })[] = [];
  speed = Infinity;

  sample(point: PointerPoint | null | undefined, now: number) {
    if (!point) { this.samples = []; this.speed = Infinity; return this.speed; }
    this.samples.push({ ...point, at: now });
    while (this.samples.length > 2 && this.samples[1].at <= now - WINDOW_MS) this.samples.shift();
    let distance = 0;
    for (let i = 1; i < this.samples.length; i++) {
      const a = this.samples[i - 1], b = this.samples[i];
      const fraction = Math.min(1, (b.at - Math.max(a.at, now - WINDOW_MS)) / Math.max(1, b.at - a.at));
      distance += Math.hypot(b.x - a.x, b.y - a.y) * Math.max(0, fraction);
    }
    const elapsed = Math.min(WINDOW_MS, now - this.samples[0].at);
    this.speed = elapsed > 0 ? distance / elapsed : Infinity;
    return this.speed;
  }

  // The next pointer segment intersects the panel. This bridges the small gap while
  // it grows, without holding a panel for a pointer moving away from it.
  headingTo(rect: PointerRect) {
    const a = this.samples[0], b = this.samples.at(-1);
    if (!a || !b || b.at <= a.at) return false;
    const vx = (b.x - a.x) / (b.at - a.at), vy = (b.y - a.y) / (b.at - a.at);
    if (Math.hypot(vx, vy) < .01) return false;
    let enter = 0, leave = HOVER_EXIT_MS;
    for (const [position, velocity, min, max] of [[b.x, vx, rect.left, rect.right], [b.y, vy, rect.top, rect.bottom]]) {
      if (Math.abs(velocity) < .0001) { if (position < min || position > max) return false; continue; }
      const t0 = (min - position) / velocity, t1 = (max - position) / velocity;
      enter = Math.max(enter, Math.min(t0, t1)); leave = Math.min(leave, Math.max(t0, t1));
    }
    return leave >= enter && leave >= 0;
  }
}

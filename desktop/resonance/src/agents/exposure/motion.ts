// B01 spring integration and easing, shared by every moving part.
export const PI = Math.PI, TAU = 2 * PI;
export const clamp = (v: number, a = 0, b = 1) => v < a ? a : v > b ? b : v;
export const lerp = (a: number, b: number, k: number) => a + (b - a) * k;
export const smooth = (a: number, b: number, x: number) => { const t = clamp((x - a) / (b - a)); return t * t * (3 - 2 * t); };
export const easeOut = (k: number) => 1 - (1 - clamp(k)) ** 3;
export const easeInOut = (k: number) => { k = clamp(k); return k < .5 ? 4 * k * k * k : 1 - (-2 * k + 2) ** 3 / 2; };
export const reduced = matchMedia('(prefers-reduced-motion: reduce)');
export const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
// A stable small number per id, so a session's look never depends on where it sits in a list.
export const hash = (s: string) => { let h = 2166136261; for (const ch of s) h = Math.imul(h ^ ch.charCodeAt(0), 16777619); return (h >>> 0) / 4294967296; };

export type Spring = { value: number; velocity: number };
export const spring = (value: number): Spring => ({ value, velocity: 0 });
// Damped harmonic spring (hz, damping ratio); returns whether it still moves.
export function step(s: Spring, goal: number, hz: number, damping: number, dt: number) {
  if (reduced.matches) { s.value = goal; s.velocity = 0; return false; }
  const w = TAU * hz;
  for (let left = dt; left > 1e-6; left -= 1 / 240) {
    const h = Math.min(left, 1 / 240);
    s.velocity += (-w * w * (s.value - goal) - 2 * damping * w * s.velocity) * h;
    s.value += s.velocity * h;
  }
  if (Math.abs(s.value - goal) < 1e-3 && Math.abs(s.velocity) < 1e-2) { s.value = goal; s.velocity = 0; return false; }
  return true;
}


export const dpr = () => Math.min(2, devicePixelRatio || 1);

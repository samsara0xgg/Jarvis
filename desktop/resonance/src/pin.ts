import type { L } from './companionSettings';

// ADR 0200: the bus trip Allen pinned, as GET /inherent/notices serves it (`departure`). The wing draws it from these absolute
// times and the local clock, so the countdown needs no polling of its own.
export type Departure = { id: string; route: string; board_stop: string; leave_at_ms: number; departs_at_ms: number; leave_at: string; departs: string; arrive_at: string; to: string; stale: boolean };
export const isDeparture = (d: unknown): d is Departure => {
  const o = d as Partial<Departure> | null;
  return !!o && typeof o.id === 'string' && typeof o.leave_at_ms === 'number' && typeof o.departs_at_ms === 'number' && typeof o.route === 'string';
};

// At 5 min or less to leave, and from then until the bus goes, the pill turns amber (the wing's turn colour).
export const PIN_AMBER_MIN = 5;
type T = (l: L) => string;
const route = (d: Departure) => d.route.replace(/\s*→\s*/g, '→');
// `🚌 28 · 12 分` until it is time to leave, then `🚌 28 · 该走了` until the bus has gone (the daemon stops serving it then; this also holds the pill back a beat early).
export function pinLabel(d: Departure, now: number, t: T): { text: string; amber: boolean; gone: boolean } {
  const minutes = Math.ceil((d.leave_at_ms - now) / 60_000);
  if (now >= d.departs_at_ms) return { text: '', amber: false, gone: true };
  const left = now >= d.leave_at_ms ? t(['Go now', '该走了']) : t([`${minutes} min`, `${minutes} 分`]);
  return { text: `🚌 ${route(d)} · ${left}${d.stale ? ' ?' : ''}`, amber: minutes <= PIN_AMBER_MIN, gone: false };
}
// The widest the pill's words get, for a width that does not change with the minute.
export const pinWidest = (d: Departure, t: T) => [`🚌 ${route(d)} · ${t(['88 min', '88 分'])} ?`, `🚌 ${route(d)} · ${t(['Go now', '该走了'])} ?`, `✕ ${t(['Unpin', '取消'])}`];
// The hover line: `18:02 出门 → Shelbourne at Pear 上 28 路 → 18:20 到家`.
export function pinTrip(d: Departure, t: T): string {
  const to = d.to === 'home' ? t(['home', '家']) : d.to === 'school' ? t(['school', '学校']) : d.to;
  const arrive = t([`${d.arrive_at} arrive ${to}`.trim(), `${d.arrive_at} 到${d.to === 'home' || d.to === 'school' ? '' : ' '}${to}`]);
  return t([`Leave ${d.leave_at} → bus ${route(d)} from ${d.board_stop} → ${arrive}`, `${d.leave_at} 出门 → ${d.board_stop} 上 ${d.route.split('→')[0].trim()} 路${d.route.includes('→') ? `（转 ${d.route.split('→').slice(1).join('→').trim()}）` : ''} → ${arrive}`]);
}

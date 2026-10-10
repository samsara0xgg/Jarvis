import type { L } from './companionSettings';

// ADR 0200: the bus trip Allen pinned, as GET /inherent/notices serves it (`departure`). The wing draws it from these absolute
// times and the local clock, so the countdown needs no polling of its own.
// ADR 0204: a pin holds up to 3 trips. The top-level fields are the current one (the earliest still catchable; `id` is the pin's), `trips` all the catchable ones
// with their own live status (`id` is the trip's), `more` how many others there are.
export type DepTrip = { id: string; route: string; board_stop: string; leave_at_ms: number; departs_at_ms: number; leave_at: string; departs: string; arrive_at: string; to: string; stale: boolean; delay_min?: number; checked_at_ms?: number | null };
export type Departure = DepTrip & { more?: number; trips?: DepTrip[] };
export const isDeparture = (d: unknown): d is Departure => {
  const o = d as Partial<Departure> | null;
  return !!o && typeof o.id === 'string' && typeof o.leave_at_ms === 'number' && typeof o.departs_at_ms === 'number' && typeof o.route === 'string';
};

// At 5 min or less to leave, and from then until the bus goes, the pill turns amber (the wing's turn colour).
export const PIN_AMBER_MIN = 5;
type T = (l: L) => string;
const route = (d: DepTrip) => d.route.replace(/\s*→\s*/g, '→');
const plus = (d: Departure) => d.more ? ` +${d.more}` : '';
// `🚌 28 · 12 分` until it is time to leave, then `🚌 28 · 该走了` until the bus has gone (the daemon stops serving it then; this also holds the pill back a beat early).
export function pinLabel(d: Departure, now: number, t: T): { text: string; amber: boolean; gone: boolean } {
  const minutes = Math.ceil((d.leave_at_ms - now) / 60_000);
  if (now >= d.departs_at_ms) return { text: '', amber: false, gone: true };
  const left = now >= d.leave_at_ms ? t(['Go now', '该走了']) : t([`${minutes} min`, `${minutes} 分`]);
  return { text: `🚌 ${route(d)} · ${left}${plus(d)}${d.stale ? ' ?' : ''}`, amber: minutes <= PIN_AMBER_MIN, gone: false };
}
// The widest the pill's words get, for a width that does not change with the minute.
export const pinWidest = (d: Departure, t: T) => [`🚌 ${route(d)} · ${t(['88 min', '88 分'])}${plus(d)} ?`, `🚌 ${route(d)} · ${t(['Go now', '该走了'])}${plus(d)} ?`];
// `19:34 到家`, or `19:34 到 Mayfair Mall` for any other place.
const arrivePhrase = (at: string, place: string, t: T) => {
  const to = place === 'home' ? t(['home', '家']) : place === 'school' ? t(['school', '学校']) : place, bare = place === 'home' || place === 'school';
  return t([`${at} arrive ${to}`.trim(), `${at} 到${bare ? '' : ' '}${to}`]);
};
// The hover line: `18:02 出门 → Shelbourne at Pear 上 28 路 → 18:20 到家`.
export function pinTrip(d: DepTrip, t: T): string {
  const arrive = arrivePhrase(d.arrive_at, d.to, t);
  return t([`Leave ${d.leave_at} → bus ${route(d)} from ${d.board_stop} → ${arrive}`, `${d.leave_at} 出门 → ${d.board_stop} 上 ${d.route.split('→')[0].trim()} 路${d.route.includes('→') ? `（转 ${d.route.split('→').slice(1).join('→').trim()}）` : ''} → ${arrive}`]);
}

// ADR 0203: what the pin's card says the refresh knows: `实时 · 晚 2 分`, `准点`, `可能不准`; nothing before the first check.
export function pinStatus(d: DepTrip, t: T): string {
  if (d.stale) return t(['May be off', '可能不准']);
  if (d.checked_at_ms == null) return '';
  const m = d.delay_min ?? 0;
  return m > 0 ? t([`Live · ${m} min late`, `实时 · 晚 ${m} 分`]) : m < 0 ? t([`Live · ${-m} min early`, `实时 · 早 ${-m} 分`]) : t(['On time', '准点']);
}

// ADR 0205: the bus card in the conversation, the `trip` of the ask card a `transit` answer puts up: one row per option (`index` is what a click sends back with
// `offer_id`; the pinned trip's id is `offer_id-index`), and how long the other ways take.
export type OfferRow = { index: number; route: string; board_stop: string; leave_at: string; departs: string; arrive_at: string; to: string };
export type Way = { minutes: number; km: number };
export type TripCardData = { offer_id: string; to: string; options: OfferRow[]; modes?: { drive?: Way; walk?: Way } };
export const tripTid = (c: TripCardData, o: OfferRow) => `${c.offer_id}-${o.index}`;
// `19:16 出门 → 19:34 到家`; the route is the row's chip.
export const offerLine = (o: OfferRow, t: T) => t([`Leave ${o.leave_at} → ${arrivePhrase(o.arrive_at, o.to, t)}`, `${o.leave_at} 出门 → ${arrivePhrase(o.arrive_at, o.to, t)}`]);
export const offerRoute = (o: OfferRow) => o.route.replace(/\s*→\s*/g, '→');
// `开车 12 分 · 走路 40 分`; an hour or more reads `1 小时 5 分`.
const span = (m: number, t: T) => m < 60 ? t([`${m} min`, `${m} 分`]) : t([`${Math.floor(m / 60)} h ${m % 60} min`, `${Math.floor(m / 60)} 小时 ${m % 60} 分`]);
export const modesLine = (c: TripCardData, t: T) => [c.modes?.drive && `${t(['Drive', '开车'])} ${span(c.modes.drive.minutes, t)}`, c.modes?.walk && `${t(['Walk', '走路'])} ${span(c.modes.walk.minutes, t)}`].filter(Boolean).join(' · ');

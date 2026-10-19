import type { L } from './companionSettings';

// The owner's day as GET /inherent/day serves it (docs/spec.html; ADR 0199, 0235): items in time order, structured and
// never worded. This file reads them for the notch's timeline (ADR 0296): the wing's one line and the pane's rows; the bar
// above the rows is barView.ts. Nothing here is made up: a field that did not arrive is a row without it, a kind this file
// does not know is left out. Pure (no DOM), so scripts/verify-notch-timeline.mjs runs it in node.
type Top = { name: string; seconds: number };
export type DayItem = {
  id: string; lane: 'you' | 'her'; kind: string; start_ms: number; end_ms: number | null; state: 'done' | 'now' | 'next';
  place?: string; kept_place?: { id: string; name: string; role: string }; activity?: string; minutes?: number;
  text?: string; active_s?: number; projects?: Top[]; apps?: Top[]; app?: string;
  route?: string; board_stop?: string; alight_stop?: string | null; turns?: number; note?: string;
  title?: string; source?: string; confirmed?: boolean; private?: boolean; to?: string; leave_at_ms?: number;
  attachment?: string; amount?: string | number; currency?: string; direction?: string; counterparty?: string; messages?: number; chats?: number;
  fired?: boolean; acknowledged?: boolean;
  // The pane's own, laid on by `buildDay` (`tidy` never reads them from the brain): `you`, he confirmed this commitment here; `leaving`, he said it is not his and it is on its way out.
  you?: boolean; leaving?: boolean;
  stages?: Stage[]; energy_kcal?: number; distance_m?: number; avg_hr?: number; project?: string; account?: string;
  credit?: string; ended_reason?: string; detail?: string;
};
// One stretch of a night as the watch told it; they may overlap (a phone's `in_bed` under the watch's stages).
export type Stage = { stage: string; start_ms: number; end_ms: number };
// What the day's envelope adds (null when the brain does not know): the body's numbers, and the weather in °C with the sun's times.
export type Vitals = { steps: number | null; resting_hr: number | null; hrv: number | null };
export type Weather = { condition: string; high: number | null; low: number | null; sunrise_ms: number | null; sunset_ms: number | null };
// One of the day's highlights: an item's id, the words for it (his, else hers, else none), and who chose it (`him`, or `her`).
export type Highlight = { id: string; label: string; by: string };
export type DayReply = {
  date: string; tz: string; start_ms: number; end_ms: number; now_ms: number; items: DayItem[];
  labels?: { title: string; source?: string; private?: boolean }[]; missing?: string[];
  line?: { text: string; draft?: boolean; written_at?: string } | null; highlights?: Highlight[];
  summary?: Vitals | null; weather?: Weather | null;
};
export type Say = (l: L) => string;
export type Rgb = readonly [number, number, number];
export type Icon = 'moon' | 'walk' | 'run' | 'bike' | 'hike' | 'swim' | 'strength' | 'yoga' | 'sport' | 'car' | 'bus' | 'talk' | 'picture' | 'event' | 'reminder' | 'work' | 'code' | 'call' | 'home' | 'school' | 'pin' | 'payment' | 'chat';
// What the day's numbers count (the chips under the bar) and what a lens lights: a row or a span of the bar is one of these or none.
export type LensKind = 'sleep' | 'trip' | 'workout' | 'work' | 'call' | 'talk' | 'payment' | 'picture';
// A Claude Code session is Mac work: it lights with the Mac lens, but the Mac's number is its own (barView.numbers leaves sessions out).
const LENSES: Record<string, LensKind> = { sleep: 'sleep', move: 'trip', ride: 'trip', workout: 'workout', work: 'work', code: 'work', call: 'call', talk: 'talk', payment: 'payment', picture: 'picture' };
export const lensOf = (kind: string): LensKind | null => LENSES[kind] ?? null;

// ---------- what the brain sends is read, never trusted ----------
// The day arrives over the network from a brain that keeps changing: a field may be missing, null or another shape. `tidy` copies an
// item field by field with the type it should have, so nothing downstream has to ask; an item with no id, kind or time is left out.
// An envelope with no usable bounds cannot be drawn at all: `tidyReply` throws, and the pane says so in a line (TimelineGuard.tsx).
const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const str = (v: unknown) => typeof v === 'string' ? v : undefined;
const fin = (v: unknown) => typeof v === 'number' && Number.isFinite(v) ? v : undefined;
// A time is a number a Date can hold (8.64e15 ms either way): one past it cannot be written as a clock, and would take the whole day with it.
const MAX_MS = 8.64e15, time = (v: unknown): v is number => typeof v === 'number' && Math.abs(v) <= MAX_MS, timeOr = (v: unknown) => time(v) ? v : null;
// A name the brain sent, looked up in a table: only the table's own keys count (`constructor` is no stage and no weather).
const own = <T>(table: Record<string, T>, key: string): T | undefined => Object.hasOwn(table, key) ? table[key] : undefined;
const bool = (v: unknown) => typeof v === 'boolean' ? v : undefined;
const STRINGS = ['place', 'activity', 'text', 'app', 'route', 'board_stop', 'alight_stop', 'note', 'title', 'source', 'to', 'attachment', 'currency', 'direction', 'counterparty', 'project', 'account', 'credit', 'ended_reason', 'detail'] as const;
const NUMBERS = ['minutes', 'active_s', 'turns', 'messages', 'chats', 'energy_kcal', 'distance_m', 'avg_hr'] as const;
const FLAGS = ['confirmed', 'private', 'fired', 'acknowledged'] as const;
export function tidy(raw: unknown): DayItem | null {
  if (!isObj(raw)) return null;
  const { id, kind, start_ms: start, end_ms: end } = raw;
  if (typeof id !== 'string' || !id || typeof kind !== 'string' || !time(start) || end != null && !time(end)) return null;
  const out: Record<string, unknown> = { id, kind, lane: raw.lane === 'her' ? 'her' : 'you', start_ms: start, end_ms: end ?? null, state: raw.state === 'now' || raw.state === 'next' ? raw.state : 'done' };
  for (const k of STRINGS) { const v = str(raw[k]); if (v !== undefined) out[k] = v; }
  for (const k of NUMBERS) { const v = fin(raw[k]); if (v !== undefined) out[k] = v; }
  if (time(raw.leave_at_ms)) out.leave_at_ms = raw.leave_at_ms;
  for (const k of FLAGS) { const v = bool(raw[k]); if (v !== undefined) out[k] = v; }
  if (typeof raw.amount === 'string' || fin(raw.amount) !== undefined) out.amount = raw.amount;
  const kept = raw.kept_place;
  if (isObj(kept) && typeof kept.id === 'string' && typeof kept.name === 'string') out.kept_place = { id: kept.id, name: kept.name, role: str(kept.role) ?? '' };
  if (Array.isArray(raw.stages)) out.stages = raw.stages.flatMap(x => isObj(x) && typeof x.stage === 'string' && time(x.start_ms) && time(x.end_ms) ? [{ stage: x.stage, start_ms: x.start_ms as number, end_ms: x.end_ms as number }] : []);
  for (const k of ['projects', 'apps'] as const) {
    const v = raw[k];
    if (Array.isArray(v)) out[k] = v.flatMap(x => isObj(x) && typeof x.name === 'string' ? [{ name: x.name, seconds: fin(x.seconds) ?? 0 }] : []);
  }
  return out as DayItem;
}
const each = <T>(v: unknown, pick: (x: unknown) => T | null): T[] => Array.isArray(v) ? v.flatMap(x => pick(x) ?? []) : [];
// The day's highlights as the brain sends them (in the day, and in the answer to a mark).
export const marksOf = (v: unknown): Highlight[] => each(v, x => isObj(x) && typeof x.id === 'string' ? { id: x.id, label: str(x.label) ?? '', by: str(x.by) ?? '' } : null);
export function tidyReply(raw: unknown): DayReply {
  if (!isObj(raw) || typeof raw.date !== 'string' || !time(raw.start_ms) || !time(raw.end_ms) || (raw.end_ms as number) <= (raw.start_ms as number)) throw new Error('the day has no usable bounds');
  return {
    date: raw.date, tz: str(raw.tz) ?? '', start_ms: raw.start_ms as number, end_ms: raw.end_ms as number, now_ms: timeOr(raw.now_ms) ?? (raw.start_ms as number),
    items: each(raw.items, tidy),
    labels: each(raw.labels, x => isObj(x) && typeof x.title === 'string' ? { title: x.title, source: str(x.source), private: bool(x.private) } : null),
    missing: each(raw.missing, x => typeof x === 'string' ? x : null),
    line: isObj(raw.line) && typeof raw.line.text === 'string' ? { text: raw.line.text, draft: bool(raw.line.draft), written_at: str(raw.line.written_at) } : null,
    highlights: marksOf(raw.highlights),
    summary: isObj(raw.summary) ? { steps: fin(raw.summary.steps) ?? null, resting_hr: fin(raw.summary.resting_hr) ?? null, hrv: fin(raw.summary.hrv) ?? null } : null,
    weather: isObj(raw.weather) ? { condition: str(raw.weather.condition) ?? '', high: fin(raw.weather.high) ?? null, low: fin(raw.weather.low) ?? null, sunrise_ms: timeOr(raw.weather.sunrise_ms), sunset_ms: timeOr(raw.weather.sunset_ms) } : null,
  };
}
// A day the pane could not read is said once in the app's log (`[timeline] ...`, which electron/companion.ts passes on), not every frame.
const told = new Set<string>();
export function report(where: string, error: unknown) {
  const frame = error instanceof Error ? error.stack?.split('\n').find(x => /^\s+at /.test(x))?.trim().slice(0, 140) : '';
  const text = `[timeline] ${where}: ${error instanceof Error ? error.message : String(error)}${frame ? ` ${frame}` : ''}`;
  if (told.has(text)) return;
  told.add(text); console.warn(text);
}

// ---------- time and words ----------
const clocks = new Map<string, Intl.DateTimeFormat>();
// `09:05` in the zone the day was cut in (the brain's `tz`), not the Mac's.
export function clock(ms: number, tz: string) {
  let f = clocks.get(tz);
  if (!f) {
    try { f = new Intl.DateTimeFormat('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }); }
    catch { f = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }); }
    clocks.set(tz, f);
  }
  return f.format(ms);
}
// `40 分`, `1 时 40 分`, `3 时`.
export function dur(min: number, t: Say) {
  const m = Math.max(0, Math.round(min)), h = Math.floor(m / 60), r = m % 60;
  return h === 0 ? t([`${m} min`, `${m} 分`]) : r === 0 ? t([`${h} h`, `${h} 时`]) : t([`${h} h ${r} min`, `${h} 时 ${r} 分`]);
}
// How long a stay has lasted, as hours:minutes (`3:12`; a stay from yesterday reads `26:05`).
const since = (ms: number) => { const m = Math.max(0, Math.floor(ms / 60_000)); return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}`; };
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
// `10月19日 周日`: from the date's own digits, so no zone can move it a day.
export function dateTitle(date: string, t: Say) {
  const m = DATE.exec(date);
  if (!m) return date;
  const [y, mo, d] = [+m[1], +m[2], +m[3]], wd = new Date(Date.UTC(y, mo - 1, d)).getUTCDay();
  return t([`${DAYS[wd]}, ${MONTHS[mo - 1]} ${d}`, `${mo}月${d}日 周${'日一二三四五六'[wd]}`]);
}
export function shiftDate(date: string, days: number) {
  const m = DATE.exec(date);
  return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3] + days)).toISOString().slice(0, 10) : date;
}

// ---------- places ----------
// Home is warm, school is blue, any other place he keeps has a hue of its own that never changes (picked from its id, FNV-1a: the
// phone does the same), a stay with no name of his is a neutral haze. None is amber: amber is the turn mark's.
const HOME: Rgb = [255, 168, 150], SCHOOL: Rgb = [100, 200, 255], NEUTRAL: Rgb = [196, 204, 238];
const OTHERS: Rgb[] = [[240, 132, 218], [132, 152, 255], [172, 124, 255], [111, 224, 180]];
export function fnv(text: string) {
  let h = 2166136261;
  for (const b of new TextEncoder().encode(text)) { h ^= b; h = Math.imul(h, 16777619) >>> 0; }
  return h >>> 0;
}
const clean = (s?: unknown) => typeof s === 'string' ? s.trim() : '';
// An item whose times are not numbers is left out: a time that cannot be formatted would take the whole pane down with it.
const sound = (it: unknown): it is DayItem => isObj(it) && time(it.start_ms) && (it.end_ms == null || time(it.end_ms));
export type StayLook = { name: string; unnamed: boolean; kept: boolean; rgb: Rgb; icon: Icon };
// The name he gave the place, else the first comma-separated part of what the phone called it, else none.
export function stayLook(it: DayItem, t: Say): StayLook {
  const kept = it.kept_place && clean(it.kept_place.name) ? it.kept_place : null, phone = clean(it.place).split(/[,，]/)[0].trim();
  const name = kept ? clean(kept.name) : phone, role = kept?.role;
  return { name: name || t(['An unnamed place', '没名字的地方']), unnamed: !name, kept: !!kept,
    rgb: kept ? role === 'home' ? HOME : role === 'school' ? SCHOOL : OTHERS[fnv(kept.id) % OTHERS.length] : NEUTRAL,
    icon: role === 'home' ? 'home' : role === 'school' ? 'school' : 'pin' };
}

const WAYS: Record<string, [Icon, L]> = { walking: ['walk', ['Walking', '步行']], running: ['run', ['Running', '跑步']], cycling: ['bike', ['Cycling', '骑车']], automotive: ['car', ['Driving', '开车']] };
const moveIcon = (activity?: string): Icon => WAYS[activity ?? '']?.[0] ?? 'walk';
const moveWords = (activity: string | undefined, t: Say) => t(WAYS[activity ?? '']?.[1] ?? ['Moving', '移动']);
const rideWords = (it: DayItem, t: Say) => { const r = clean(it.route).replace(/\s*→\s*/g, '→'); return r ? t([`Bus ${r}`, `${r} 路`]) : t(['Bus', '公交']); };

// ---------- the wing's one line ----------
// The place of the stay that covers now and how long it has lasted (`家 · 3:12`); while he moves, the move in words and how long
// (`27 路 · 12 分`, `步行 · 6 分`); else `今天`. It is drawn every frame on a canvas, where nothing catches a throw: a day this cannot read
// is reported once, and the wing falls back to the plain place, or to nothing.
export function wingText(day: DayReply | null, now: number, t: Say): { head: string; tail: string } {
  try { return wingLine(day, now, t); }
  catch (error) { report('the wing', error); return { head: plainPlace(day, now), tail: '' }; }
}
function wingLine(day: DayReply | null, now: number, t: Say) {
  let moving: DayItem | undefined, stay: DayItem | undefined;
  for (const it of Array.isArray(day?.items) ? day!.items : []) {
    if (!sound(it) || it.start_ms > now || it.end_ms != null && it.end_ms <= now) continue;
    if ((it.kind === 'ride' || it.kind === 'move') && (!moving || it.start_ms >= moving.start_ms)) moving = it;
    else if (it.kind === 'stay' && (!stay || it.start_ms >= stay.start_ms)) stay = it;
  }
  if (moving) return { head: moving.kind === 'ride' ? rideWords(moving, t) : moveWords(moving.activity, t), tail: dur(Math.floor((now - moving.start_ms) / 60_000), t) };
  if (stay) { const look = stayLook(stay, t); return { head: look.unnamed ? t(['Staying', '停留']) : look.name, tail: since(now - stay.start_ms) }; }
  return { head: t(['Today', '今天']), tail: '' };
}
function plainPlace(day: DayReply | null, now: number) {
  try {
    for (const it of day!.items) if (sound(it) && it.kind === 'stay' && it.start_ms <= now && (it.end_ms == null || it.end_ms > now)) return clean(it.kept_place?.name) || clean(it.place).split(/[,，]/)[0].trim();
  } catch { /* nothing to say */ }
  return '';
}

// ---------- the pane ----------
// What can be handed to her (ADR 0214): a kind the brain's `about` takes, with an id of the shape it accepts (a commitment's has an @ in it). A
// bad one refuses the whole turn. A run and a Claude Code session are not kinds it takes.
const ABOUT_KINDS = new Set(['reminder', 'event', 'todo', 'stay', 'move', 'sleep', 'work', 'call', 'talk', 'picture', 'ride', 'departure', 'payment', 'claude_chat', 'claude_day']), ITEM_ID = /^[A-Za-z0-9_\-=+/.|:@]{1,256}$/;
export const askable = (kind: string, id: string) => ABOUT_KINDS.has(kind) && ITEM_ID.test(id);
// What can be marked as what mattered (ADR 0296): any item of the day with an id the brain takes (the same set).
export const starrable = (id: string) => ITEM_ID.test(id);

// A row of the list. The time column is `at` (when it began; `ago` marks one that began the day before), the right column `right` (its length, how
// long until it, or its value, which `value` says to write in ink). `kind` and `lens`: what the row is, and which of the day's numbers it is
// counted in (a lens on the bar dims the rows that are not). A `connector` is a way he went (a move, a ride, a short stop) and `onWay` is
// something that happened on one: together they are a trip, and the pane draws one rail through them. `wait`: it waits on him; `leaving`: it is on its way out.
export type Row = {
  key: string; kind: string; lens: LensKind | null; icon: Icon | 'dot'; title: string; detail: string; at: string; ago: string; right: string; value: boolean;
  future: boolean; star: boolean; hers: boolean; wait: boolean; leaving: boolean; connector: boolean; onWay: boolean;
};
// `star`: null when the stay is not one of the day's highlights, else its label (which may be empty); `hers`: it is her pick, not his mark.
export type StayHead = { key: string; name: string; unnamed: boolean; rgb: Rgb; icon: Icon; when: string; dur: string; star: string | null; hers: boolean };
export type Block = { type: 'row'; t: number; row: Row } | { type: 'now'; t: number; key: string; label: string } | { type: 'stay'; t: number; key: string; head: StayHead; items: Block[] };
// Where a row or a stay header sits in time (ms; a moment has t1 = t0), and what the owner or she marked as what mattered.
export type Span = { key: string; kind: string; t0: number; t1: number };
export type Mark = { label: string; by: string };
export type DayModel = {
  title: string; today: boolean; now: number; tz: string; reply: DayReply; items: DayItem[]; chips: { text: string; more: boolean }[]; line: { text: string; draft: boolean } | null;
  blocks: Block[]; foot: string; empty: boolean; span: Map<string, Span>; index: Span[]; stars: Map<string, Mark>; waiting: string[];
  weather: WeatherLook | null; vitals: Vital[];
  // What an open row reads (dayOpen.ts): each row and each stay's header by key, and the sessions that are folded into one row.
  rows: Map<string, Row>; heads: Map<string, StayHead>; folds: Map<string, DayItem[]>;
};

// The weather in a few words and a glyph each. WeatherKit names its conditions in words (`partlyCloudy`, `isolatedThunderstorms`), so a name
// is read without case or separators; one this does not know (`frigid`, `hot`) leaves only the temperatures.
export type Sky = 'sun' | 'cloudSun' | 'cloud' | 'rain' | 'storm' | 'snow' | 'fog' | 'wind';
const RAIN: [Sky, L] = ['rain', ['Rain', '雨']], DRIZZLE: [Sky, L] = ['rain', ['Light rain', '小雨']], STORM: [Sky, L] = ['storm', ['Thunderstorms', '雷雨']], SNOW: [Sky, L] = ['snow', ['Snow', '雪']],
  FOG: [Sky, L] = ['fog', ['Fog', '雾']], WIND: [Sky, L] = ['wind', ['Windy', '风']];
const CONDITIONS: Record<string, [Sky, L]> = {
  clear: ['sun', ['Clear', '晴']], mostlyclear: ['cloudSun', ['Mostly clear', '少云']], partlycloudy: ['cloudSun', ['Partly cloudy', '多云']], mostlycloudy: ['cloud', ['Mostly cloudy', '多云']], cloudy: ['cloud', ['Overcast', '阴']],
  drizzle: DRIZZLE, sunshowers: DRIZZLE, freezingdrizzle: DRIZZLE, rain: RAIN, heavyrain: RAIN, freezingrain: RAIN,
  isolatedthunderstorms: STORM, scatteredthunderstorms: STORM, thunderstorms: STORM, strongstorms: STORM, tropicalstorm: STORM, hurricane: STORM,
  snow: SNOW, heavysnow: SNOW, flurries: SNOW, sunflurries: SNOW, sleet: SNOW, wintrymix: SNOW, blizzard: SNOW, blowingsnow: SNOW, hail: SNOW,
  foggy: FOG, haze: FOG, smoky: FOG, blowingdust: FOG, breezy: WIND, windy: WIND,
};
// `sky` is null for a condition this does not know (its word is then empty); `temps` is `14° / 8°` (high, low), or what of it there is; `sun` is
// what a rest on the weather says (`日出 07:12 · 日落 18:21`, in the zone the day was cut in), empty when the brain sent neither time.
export type WeatherLook = { sky: Sky | null; word: string; temps: string; sun: string };
export function weatherOf(w: Weather | null | undefined, t: Say, tz = ''): WeatherLook | null {
  if (!w) return null;
  const hit = own(CONDITIONS, clean(w.condition).toLowerCase().replace(/[^a-z]/g, ''));
  const temps = [w.high, w.low].filter((x): x is number => x != null).map(x => `${Math.round(x) || 0}°`).join(' / ');
  const sun = [w.sunrise_ms != null && t([`Sunrise ${clock(w.sunrise_ms, tz)}`, `日出 ${clock(w.sunrise_ms, tz)}`]), w.sunset_ms != null && t([`Sunset ${clock(w.sunset_ms, tz)}`, `日落 ${clock(w.sunset_ms, tz)}`])].filter((x): x is string => !!x).join(' · ');
  return hit || temps ? { sky: hit?.[0] ?? null, word: hit ? t(hit[1]) : '', temps, sun } : null;
}
// The body's numbers for the day, as quiet values (`8,240 步`, `静息 58`, `HRV 42`): only the ones that were reported.
export type Vital = { key: string; before: string; value: string; after: string };
export function vitalsOf(v: Vitals | null | undefined, t: Say): Vital[] {
  const out: Vital[] = [];
  if (v?.steps != null) out.push({ key: 'steps', before: '', value: Math.round(v.steps).toLocaleString('en-US'), after: t(['steps', '步']) });
  if (v?.resting_hr != null) out.push({ key: 'resting', before: t(['Resting', '静息']), value: String(Math.round(v.resting_hr)), after: '' });
  if (v?.hrv != null) out.push({ key: 'hrv', before: 'HRV', value: String(Math.round(v.hrv)), after: '' });
  return out;
}

// What a source gives the day, named as the phone names it (`还没读到：Mac 的时间`).
const SOURCES: Record<string, L> = { phone: ['The phone’s records', '手机的记录'], reminders: ['Reminders', '提醒'], mac: ['Mac time', 'Mac 的时间'], talks: ['Conversations', '对话'], rides: ['Bus rides', '公交乘车'],
  pictures: ['Pictures', '图片'], calendar: ['The calendar', '日历'], departures: ['Buses', '公交'], commitments: ['What he has to go to', '要去的事'] };
export const PAYERS: Record<string, L> = { wechat_pay: ['WeChat Pay', '微信'], alipay: ['Alipay', '支付宝'], boc: ['Bank of China', '中国银行'] };
const SYMBOLS: Record<string, string> = { CAD: '$', USD: 'US$', CNY: '¥', EUR: '€', GBP: '£', JPY: 'JP¥' };
// `$12.40`, `¥68.00`; a currency with no sign of its own is written after the number (`12.40 CHF`).
export function moneyText(amount: unknown, currency?: string) {
  const a = clean(typeof amount === 'number' ? amount.toFixed(2) : String(amount ?? '')), c = clean(currency).toUpperCase();
  return !a ? '' : c in SYMBOLS ? `${SYMBOLS[c]}${a}` : `${a} ${c}`.trim();
}
// What can stand inside a stay; moves and rides never do, they are the ways between.
const NESTED = new Set(['sleep', 'workout', 'talk', 'picture', 'event', 'reminder', 'work', 'code', 'call', 'departure', 'payment', 'claude_chat', 'claude_day']);
// What happens in one moment (the rest has a length).
const INSTANT = new Set(['reminder', 'picture', 'departure', 'payment']);
export const SLEEP: Rgb = [112, 120, 205], TRANSIT: Rgb = [196, 208, 255], REM: Rgb = [150, 160, 235];

// ---------- a night's stages ----------
// What each stage looks like on the bar (the sleep hue at an alpha; REM the lighter hue) and which wins where stages overlap, as two sources
// that recorded the same night leave them: awake over REM over deep over core over unspecified over a phone's `in_bed`. A name this does not
// know is read as unspecified.
const STAGES: Record<string, { rgb: Rgb; a: number; rank: number }> = {
  awake: { rgb: SLEEP, a: .25, rank: 5 }, rem: { rgb: REM, a: .8, rank: 4 }, deep: { rgb: SLEEP, a: 1, rank: 3 }, core: { rgb: SLEEP, a: .78, rank: 2 },
  unspecified: { rgb: SLEEP, a: .6, rank: 1 }, in_bed: { rgb: SLEEP, a: .25, rank: 0 },
};
export const stageLook = (stage: string) => own(STAGES, stage) ?? STAGES.unspecified;
// The night between `a` and `b` as stretches that do not overlap, each with the stage that wins there (so a night two sources told is counted
// once); where no stage was reported it is unspecified. Pure and cheap (a night has under a hundred stages), so the bar, the hover card, the open
// row's totals and its hypnogram all ask.
export function stageSlices(it: DayItem, a: number, b: number): { stage: string; s: number; e: number }[] {
  const segs = (it.stages ?? []).flatMap(x => { const s = Math.max(x.start_ms, a), e = Math.min(x.end_ms, b); return e > s ? [{ stage: own(STAGES, x.stage) ? x.stage : 'unspecified', s, e }] : []; });
  const cuts = [...new Set([a, b, ...segs.flatMap(x => [x.s, x.e])])].sort((x, y) => x - y), out: { stage: string; s: number; e: number }[] = [];
  for (let i = 0; i + 1 < cuts.length; i++) {
    let best: (typeof segs)[number] | undefined;
    for (const x of segs) if (x.s <= cuts[i] && x.e >= cuts[i + 1] && (!best || STAGES[x.stage].rank > STAGES[best.stage].rank)) best = x;
    const stage = best?.stage ?? 'unspecified', last = out.at(-1);
    if (last && last.stage === stage && last.e === cuts[i]) last.e = cuts[i + 1]; else out.push({ stage, s: cuts[i], e: cuts[i + 1] });
  }
  return out;
}
// How long the night was in each stage, in minutes.
export function stageMinutes(it: DayItem) {
  const minutes: Record<string, number> = {};
  for (const x of stageSlices(it, it.start_ms, it.end_ms ?? it.start_ms)) minutes[x.stage] = (minutes[x.stage] ?? 0) + (x.e - x.s) / 60_000;
  return minutes;
}
// How long a set of sessions that may overlap covered, in minutes: two that ran side by side count once.
export function covered(list: { start_ms: number; end_ms: number | null }[], open: number) {
  let total = 0, to = -Infinity;
  for (const x of [...list].sort((a, b) => a.start_ms - b.start_ms)) {
    const end = Math.max(x.start_ms, x.end_ms ?? open);
    total += Math.max(0, end - Math.max(x.start_ms, to)); to = Math.max(to, end);
  }
  return total / 60_000;
}

// ---------- a workout in words ----------
const SPORTS: [RegExp, Icon, L][] = [[/run/, 'run', ['Running', '跑步']], [/walk/, 'walk', ['Walking', '步行']], [/cycl|bik/, 'bike', ['Cycling', '骑车']], [/hik/, 'hike', ['Hiking', '徒步']],
  [/swim/, 'swim', ['Swimming', '游泳']], [/strength|weight|lift/, 'strength', ['Strength training', '力量训练']], [/yoga/, 'yoga', ['Yoga', '瑜伽']]];
// The activity's icon and word, and what the watch counted: how far, how much energy, the heart's average. An activity this does not know is
// a workout with what the phone wrote about it.
export function workoutOf(it: DayItem, t: Say) {
  const hit = SPORTS.find(([re]) => re.test(clean(it.activity).toLowerCase())), m = it.distance_m ?? 0;
  const facts = [
    m > 0 ? m >= 1000 ? t([`${(m / 1000).toFixed(1)} km`, `${(m / 1000).toFixed(1)} 公里`]) : t([`${Math.round(m)} m`, `${Math.round(m)} 米`]) : '',
    it.energy_kcal ? t([`${Math.round(it.energy_kcal)} kcal`, `${Math.round(it.energy_kcal)} 千卡`]) : '',
    it.avg_hr ? t([`avg heart rate ${Math.round(it.avg_hr)}`, `平均心率 ${Math.round(it.avg_hr)}`]) : '',
  ].filter(Boolean);
  return { icon: hit?.[1] ?? 'sport' as Icon, word: t(hit?.[2] ?? ['Workout', '运动']), facts, note: hit ? '' : clean(it.text).split('\n')[0] };
}
// A Claude Code session's account, as the letter it is known by.
export const accountOf = (it: DayItem) => /^[ab]$/i.test(clean(it.account)) ? clean(it.account).toUpperCase() : '';
type Ctx = { t: Say; tz: string; reply: DayReply; now: number; today: boolean; marks: Map<string, string>; stars: Map<string, Mark> };
const hersOf = (id: string, c: Ctx) => c.stars.get(id)?.by === 'her';

export const names = (list?: Top[]) => (list ?? []).map(x => clean(x.name)).filter(Boolean);
export { clean, moveIcon, moveWords, rideWords };
// The end of a span: `24:00` at the day's end, `次日 02:10` past it.
export const endText = (ms: number, reply: DayReply, tz: string, t: Say) => ms === reply.end_ms ? '24:00' : (ms > reply.end_ms ? t(['next day ', '次日 ']) : '') + clock(ms, tz);
const dayBefore = (it: DayItem, c: Ctx) => it.start_ms < c.reply.start_ms ? c.t(['yesterday ', '昨天 ']) : '';
export const EVENT_SOURCES: Record<string, L> = { mail: ['Mail', '邮件'], said: ['You said', '你说的'], plan: ['Plan', '计划'] };
const TWO_HOURS = 2 * 3_600_000;
function rowOf(it: DayItem, c: Ctx): Row | null {
  const { t, tz, reply } = c;
  let icon: Icon, title: string, detail = '', right = '', value = false, wait = false;
  switch (it.kind) {
    case 'sleep': icon = 'moon'; title = t(['Sleep', '睡觉']); break;
    case 'move': icon = moveIcon(it.activity); title = moveWords(it.activity, t); break;
    case 'ride': icon = 'bus'; title = rideWords(it, t); detail = [clean(it.board_stop), clean(it.alight_stop)].filter(Boolean).join(' → '); break;
    case 'workout': { const w = workoutOf(it, t); icon = w.icon; title = w.word; detail = w.facts.join(' · ') || w.note; break; }
    case 'code':
      icon = 'code'; title = [t(['Claude Code', 'Claude Code']), clean(it.title)].filter(Boolean).join(' · ');
      detail = [clean(it.project), accountOf(it)].filter(Boolean).join(' · '); break;
    case 'talk': icon = 'talk'; title = t(['Talked with her', '和她说话']); detail = it.turns ? t([`${it.turns} ${it.turns === 1 ? 'line' : 'lines'}`, `${it.turns} 句`]) : ''; break;
    case 'picture': icon = 'picture'; title = t(['Photo', '照片']); detail = clean(it.note).split('\n')[0]; break;
    case 'event': {
      // Where it came from (no source: his calendar), that it is not his yet (read from mail), and where; a private one says no more.
      icon = 'event'; title = it.private ? t(['Private', '私事']) : clean(it.title) || t(['Event', '日程']);
      const unsure = it.source === 'mail' && it.confirmed === false, word = it.source === undefined ? t(['Calendar', '日历']) : EVENT_SOURCES[it.source] ? t(EVENT_SOURCES[it.source]) : '';
      wait = unsure && !it.leaving && (it.end_ms ?? it.start_ms) > c.now;
      detail = it.private ? '' : [word, unsure && t(['not confirmed', '还没确认']), it.you && t(['you confirmed it', '你确认了']), clean(it.place)].filter(Boolean).join(' · '); break;
    }
    case 'reminder':
      icon = 'reminder'; title = clean(it.text) || t(['Reminder', '提醒']);
      wait = it.fired === true && it.acknowledged === false;
      detail = it.acknowledged ? t(['Acknowledged', '你知道了']) : it.fired ? t(['It rang', '响过了']) : it.start_ms > c.now ? t(['Not due yet', '还没到点']) : ''; break;
    case 'work': {
      icon = 'work'; title = t(['Working on the Mac', '在 Mac 上工作']);
      detail = [names(it.projects)[0], names(it.apps)[0]].filter(Boolean).join(' · ');
      const active = it.active_s ? dur(it.active_s / 60, t) : '';
      right = active && t([`active ${active}`, `活跃 ${active}`]); break;
    }
    case 'call': icon = 'call'; title = clean(it.app) ? t([`Call · ${it.app}`, `通话 · ${it.app}`]) : t(['Call', '通话']); break;
    case 'departure':
      icon = 'bus'; title = clean(it.title) || rideWords(it, t); value = true;
      detail = clean(it.to) ? t([`to ${it.to}`, `去 ${it.to}`]) : '';
      right = it.leave_at_ms != null ? t([`leave ${clock(it.leave_at_ms, tz)}`, `${clock(it.leave_at_ms, tz)} 出门`]) : ''; break;
    case 'payment': {
      // Who, what and through which app on the left; the amount where a length would be (money out is plain, money in has a +).
      icon = 'payment'; value = true; const who = clean(it.counterparty), what = clean(it.text), via = PAYERS[clean(it.source)];
      title = who || what || t(['Payment', '付款']);
      detail = [who && what !== who ? what : '', via ? t(via) : ''].filter(Boolean).join(' · ');
      right = `${it.direction === 'in' ? '+' : ''}${moneyText(it.amount, it.currency)}`; break;
    }
    case 'claude_chat':
      icon = 'chat'; title = clean(it.text) || t(['Claude chat', 'Claude 对话']); detail = t(['With Claude', '和 Claude']);
      right = it.messages ? t([`${it.messages} msgs`, `${it.messages} 条`]) : ''; break;
    case 'claude_day': {
      icon = 'chat'; const n = it.chats ?? 0;
      title = t(['Claude day', 'Claude 日报']);
      detail = [n ? t([`${n} ${n === 1 ? 'chat' : 'chats'}`, `${n} 个对话`]) : '', ...clean(it.text).split(/\s*;\s*/)].filter(Boolean).join(' · '); break;
    }
    default: return null;
  }
  const instant = INSTANT.has(it.kind), future = c.today && it.start_ms > c.now;
  // Started and not over: the length so far; over: its length; a moment (a reminder, a photo, a one-line talk) has none.
  const going = !instant && it.end_ms == null && it.state !== 'next', span = !instant && it.end_ms != null && it.end_ms - it.start_ms >= 60_000;
  let length = 0;
  if (going) length = ((c.today ? c.now : reply.end_ms) - it.start_ms) / 60_000;
  else if (span) length = (it.end_ms! - it.start_ms) / 60_000;
  if (it.kind === 'sleep' && it.minutes != null) length = it.minutes;
  if (!right && length > 0 && it.kind !== 'claude_day') right = dur(Math.max(1, length), t);
  // What is still to come within two hours says how long until it (a departure keeps its own words).
  if (future && !value && it.start_ms - c.now <= TWO_HOURS) { const left = dur(Math.ceil((it.start_ms - c.now) / 60_000), t); right = t([`in ${left}`, `还有 ${left}`]); }
  const mark = c.marks.get(it.id);
  if (mark) { detail = [title, detail].filter(Boolean).join(' · '); title = mark; }
  return { key: it.id, kind: it.kind, lens: lensOf(it.kind), icon, title, detail, at: clock(it.start_ms, tz), ago: it.start_ms < reply.start_ms ? t(['yd', '昨']) : '', right, value, future, star: c.marks.has(it.id), hers: hersOf(it.id, c), wait, leaving: it.leaving === true,
    connector: it.kind === 'move' || it.kind === 'ride', onWay: false };
}
// A short stop on the way is a row of the trip: how long, and the place the phone called it.
function stopRow(it: DayItem, c: Ctx): Row {
  const { t } = c, minutes = Math.max(1, (it.end_ms! - it.start_ms) / 60_000), title = t([`Stopped ${dur(minutes, t)}`, `停了 ${dur(minutes, t)}`]), place = clean(it.place).split(/[,，]/)[0].trim(), mark = c.marks.get(it.id);
  return { key: it.id, kind: 'stay', lens: null, icon: 'dot', title: mark || title, detail: mark ? [title, place].filter(Boolean).join(' · ') : place, at: clock(it.start_ms, c.tz), ago: it.start_ms < c.reply.start_ms ? t(['yd', '昨']) : '', right: '', value: false,
    future: false, star: c.marks.has(it.id), hers: hersOf(it.id, c), wait: false, leaving: false, connector: true, onWay: false };
}

// The Claude Code sessions of one stay that were not marked, two or more, are one row: how many, the two newest titles, and how long they
// covered together (sessions that ran side by side count once).
function foldRow(list: DayItem[], stay: DayItem, c: Ctx): Row {
  const { t, tz, reply } = c, n = list.length, first = list[0], open = c.today ? c.now : reply.end_ms;
  const newest = [...list].sort((a, b) => b.start_ms - a.start_ms).slice(0, 2).map(x => clean(x.title)).filter(Boolean);
  return { key: `code:${stay.id}`, kind: 'code', lens: 'work', icon: 'code', title: t([`Claude Code · ${n} sessions`, `Claude Code · ${n} 个会话`]), detail: newest.join(' · '), at: clock(first.start_ms, tz),
    ago: first.start_ms < reply.start_ms ? t(['yd', '昨']) : '', right: dur(Math.max(1, covered(list, open)), t), value: false, future: false, star: false, hers: false, wait: false, leaving: false, connector: false, onWay: false };
}

function stayHead(it: DayItem, c: Ctx): StayHead {
  const { t, tz, reply } = c, look = stayLook(it, t), before = dayBefore(it, c);
  const end = it.end_ms ?? (c.today ? c.now : reply.end_ms);
  const when = it.end_ms == null ? t([`from ${before}${clock(it.start_ms, tz)}`, `${before}${clock(it.start_ms, tz)} 起`]) : `${before}${clock(it.start_ms, tz)}–${endText(it.end_ms, reply, tz, t)}`;
  return { key: it.id, name: look.name, unnamed: look.unnamed, rgb: look.rgb, icon: look.icon, when, dur: dur(Math.max(1, (end - it.start_ms) / 60_000), t), star: c.marks.get(it.id) ?? null, hers: hersOf(it.id, c) };
}

const byTime = (a: DayItem, b: DayItem) => a.start_ms - b.start_ms || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
const SHORT_STOP = 15 * 60_000;
const isWay = (it?: DayItem) => it?.kind === 'move' || it?.kind === 'ride';
const keptName = (it: DayItem) => it.kept_place ? clean(it.kept_place.name) : '';
// The day as the pane reads it: each stay as a group with what happened inside it, the ways he went between stays (a move, a bus ride,
// a short stop) and whatever fell outside any stay (a sleep before he got home), all in time order; on today a now line, with what is
// still to come below it. An unnamed place he stayed at for under 15 minutes between two ways is a stop on the way, not a place.
// What the pane has just done to items of the day that the brain has not said back yet: the commitments he confirmed here (`you`) and those he said are not his (`leaving`).
export type Local = { you?: ReadonlySet<string>; leaving?: ReadonlySet<string> };
export function buildDay(input: DayReply, now: number, t: Say, local: Local = {}): DayModel {
  const reply = tidyReply(input), tz = reply.tz, today = now >= reply.start_ms && now < reply.end_ms;
  if (local.you?.size || local.leaving?.size) reply.items = reply.items.map(it => ({ ...it, ...local.you?.has(it.id) && { you: true }, ...local.leaving?.has(it.id) && { leaving: true } }));
  // A highlight's label is the owner's or hers; with neither, the brain writes `kind HH:MM`, which says less than the row does.
  const marks = new Map<string, string>(), stars = new Map<string, Mark>();
  for (const h of reply.highlights ?? []) {
    marks.set(h.id, /^[a-z_]+ \d{1,2}:\d{2}$/i.test(clean(h.label)) ? '' : clean(h.label));
    stars.set(h.id, { label: marks.get(h.id)!, by: h.by });
  }
  const c: Ctx = { t, tz, reply, now, today, marks, stars };
  const seen = new Set<string>(), items: DayItem[] = [];
  for (const it of reply.items) if (!seen.has(it.id) && (it.kind === 'stay' || isWay(it) || NESTED.has(it.kind))) { seen.add(it.id); items.push(it); }
  items.sort(byTime);
  const seq = items.filter(it => it.kind === 'stay' || isWay(it)), stops = new Set<string>();
  seq.forEach((it, i) => { if (it.kind === 'stay' && !keptName(it) && it.end_ms != null && it.end_ms - it.start_ms < SHORT_STOP && isWay(seq[i - 1]) && isWay(seq[i + 1])) stops.add(it.id); });
  const stays = items.filter(it => it.kind === 'stay' && !stops.has(it.id)), inside = new Map<string, Block[]>(stays.map(s => [s.id, []]));
  const tops: { rank: number; block: Block }[] = [], span = new Map<string, Span>(), openEnd = today ? now : reply.end_ms;
  const reach = (it: DayItem) => INSTANT.has(it.kind) || it.end_ms == null && it.state === 'next' ? it.start_ms : Math.max(it.start_ms, it.end_ms ?? openEnd);
  const place = (it: DayItem) => span.set(it.id, { key: it.id, kind: it.kind, t0: it.start_ms, t1: reach(it) });
  // What began while he was at a place is that place's.
  const homeOf = (it: DayItem) => NESTED.has(it.kind) ? [...stays].reverse().find(s => s.start_ms <= it.start_ms && (s.end_ms == null || it.start_ms < s.end_ms)) : undefined;
  const sessions = new Map<DayItem, DayItem[]>(), folds = new Map<string, { stay: DayItem; list: DayItem[] }>(), folded = new Set<string>();
  const rows = new Map<string, Row>(), heads = new Map<string, StayHead>(), foldedRows = new Map<string, DayItem[]>();
  for (const it of items) { const home = it.kind === 'code' && !marks.has(it.id) ? homeOf(it) : undefined; if (home) sessions.set(home, [...sessions.get(home) ?? [], it]); }
  for (const [stay, list] of sessions) if (list.length > 1) { folds.set(list[0].id, { stay, list }); list.forEach(x => folded.add(x.id)); }
  for (const it of items) {
    const fold = folds.get(it.id);
    if (folded.has(it.id) && !fold) continue;
    const row = fold ? foldRow(fold.list, fold.stay, c) : it.kind !== 'stay' ? rowOf(it, c) : stops.has(it.id) ? stopRow(it, c) : null;
    if (!row) continue;
    rows.set(row.key, row);
    if (fold) foldedRows.set(row.key, fold.list);
    if (fold) span.set(row.key, { key: row.key, kind: 'code', t0: it.start_ms, t1: Math.max(...fold.list.map(reach)) }); else place(it);
    const block: Block = { type: 'row', t: it.start_ms, row }, home = fold ? fold.stay : homeOf(it);
    if (home) inside.get(home.id)!.push(block); else tops.push({ rank: it.kind === 'sleep' ? 0 : row.connector ? 2 : 3, block });
  }
  for (const s of stays) { place(s); const head = stayHead(s, c); heads.set(s.id, head); tops.push({ rank: 1, block: { type: 'stay', t: s.start_ms, key: s.id, head, items: inside.get(s.id)! } }); }
  tops.sort((a, b) => a.block.t - b.block.t || a.rank - b.rank);
  // What happened while he was on the way belongs to the trip.
  const legs = tops.flatMap(x => x.block.type === 'row' && x.block.row.connector ? [span.get(x.block.row.key)!] : []);
  for (const { block } of tops) if (block.type === 'row' && !block.row.connector && block.row.kind !== 'sleep' && legs.some(g => g.t0 <= block.t && block.t < g.t1)) block.row.onWay = true;
  const blocks = tops.map(x => x.block);
  if (today) {
    const holder = stays.find(s => s.start_ms <= now && (s.end_ms == null || now < s.end_ms)), list = holder ? inside.get(holder.id)! : blocks, at = list.findIndex(b => b.t > now);
    list.splice(at < 0 ? list.length : at, 0, { type: 'now', t: now, key: 'now', label: t([`Now ${clock(now, tz)}`, `现在 ${clock(now, tz)}`]) });
  }
  const chips = (reply.labels ?? []).map(l => l.private ? t(['Private', '私事']) : clean(l.title)).filter(Boolean).map(text => ({ text, more: false }));
  if (chips.length > 2) chips.splice(2, chips.length, { text: `+${chips.length - 2}`, more: true });
  const down = (reply.missing ?? []).map(k => t(own(SOURCES, k) ?? [k, k]));
  const foot = down.length ? t([`Not read yet: ${down.join(' · ')}`, `还没读到：${down.join(' · ')}`]) : '';
  const line = clean(reply.line?.text) ? { text: clean(reply.line!.text), draft: !!reply.line!.draft } : null;
  // Every row and stay header in the order the list shows them (a stay before what began with it), for "which row is at this minute".
  const index = [...span.values()].sort((a, b) => a.t0 - b.t0 || +(b.kind === 'stay') - +(a.kind === 'stay') || (a.key < b.key ? -1 : 1));
  // What waits on him, in the order the list shows it.
  const waiting: string[] = [], walk = (list: Block[]) => { for (const b of list) { if (b.type === 'row') { if (b.row.wait) waiting.push(b.row.key); } else if (b.type === 'stay') walk(b.items); } };
  walk(blocks);
  return { title: dateTitle(reply.date, t), today, now, tz, reply, items, chips, line, blocks, foot, empty: !items.length && !line && !chips.length, span, index, stars, waiting, weather: weatherOf(reply.weather, t, tz), vitals: vitalsOf(reply.summary, t), rows, heads, folds: foldedRows };
}

// The list with only what overlaps [a, b]: a stay stays while it does, with only the rows inside it that do; the now line when now is in.
// The night's letter on his Claude chats is about the whole day, not a stretch of it, so it never counts as overlapping.
export function within(blocks: Block[], span: Map<string, Span>, a: number, b: number): Block[] {
  const out: Block[] = [];
  for (const block of blocks) {
    if (block.type === 'now') { if (block.t >= a && block.t <= b) out.push(block); continue; }
    const s = span.get(block.type === 'row' ? block.row.key : block.key), hit = !!s && (s.t0 === s.t1 ? s.t0 >= a && s.t0 <= b : s.t0 < b && s.t1 > a);
    if (block.type === 'row') { if (hit && block.row.kind !== 'claude_day') out.push(block); continue; }
    const items = within(block.items, span, a, b);
    if (hit || items.length) out.push({ ...block, items });
  }
  return out;
}

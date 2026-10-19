import { clean, clock, covered, dur, endText, lensOf, moneyText, moveIcon, moveWords, names, rideWords, SLEEP, stageLook, stageMinutes, stageSlices, stayLook, TRANSIT, workoutOf, type DayItem, type DayModel, type Icon, type LensKind, type Rgb, type Say } from './dayView.ts';

// The day in one bar (ADR 0296): the axis, the pieces the bar is made of, what the pointer finds at a minute (the stretch it is in and
// what else was going on), a range of the bar, and the day's numbers. Pure (no DOM), so scripts/verify-notch-timeline.mjs runs it in
// node. The rows under the bar are dayView.ts; the two meet only at an item's id.
const HOUR = 3_600_000, MIN = 60_000;
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
export const hm = (min: number) => { const m = Math.max(0, Math.round(min)); return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}`; };

// ---------- the axis ----------
// The local hour `h` (0 to 24) of the day in ms: the day is a calendar day, 23 or 25 hours long where the clocks change, so an hour
// is looked for where the day's own clock says so.
export function hourAt(reply: { start_ms: number; end_ms: number }, tz: string, h: number) {
  if (h >= 24) return reply.end_ms;
  const base = reply.start_ms + h * HOUR, shifted = base + (reply.end_ms - reply.start_ms - 24 * HOUR), want = `${String(h).padStart(2, '0')}:00`;
  return clock(base, tz) === want ? base : clock(shifted, tz) === want ? shifted : h >= 3 ? shifted : base;
}
const floorHour = (ms: number, tz: string) => Math.floor(ms / MIN) * MIN - Number(clock(ms, tz).slice(3)) * MIN;
// When he woke: the end of the main sleep (the one with the most sleep in it) that ends on this day.
export function wakeOf(model: DayModel): number | null {
  const { reply } = model;
  let best: DayItem | null = null;
  for (const it of model.items) if (it.kind === 'sleep' && it.end_ms != null && it.end_ms > reply.start_ms && it.end_ms <= reply.end_ms && (!best || (it.minutes ?? 0) >= (best.minutes ?? 0))) best = it;
  return best?.end_ms ?? null;
}
// What takes the bar's start back to its hour: a stay alone never does (it is a place he was, not a thing he did).
const REACH = new Set(['move', 'ride', 'talk', 'picture', 'event', 'work', 'call', 'payment', 'workout', 'code']);
// The bar starts at 06:00, or at the hour before he woke when that is later (a long sleep is not worth a third of the bar); what he did
// before that takes it back to the hour it began in, and it always holds now. It ends at 24:00.
export function axisOf(model: DayModel) {
  const { reply, tz } = model, six = hourAt(reply, tz, 6), wake = wakeOf(model);
  let t0 = wake === null ? six : Math.max(six, floorHour(wake - HOUR, tz));
  if (model.today) t0 = Math.min(t0, floorHour(model.now, tz));
  t0 = clamp(t0, reply.start_ms, reply.end_ms - HOUR);
  for (const it of model.items) if (REACH.has(it.kind) && it.start_ms >= reply.start_ms && it.start_ms < t0) t0 = Math.min(t0, floorHour(it.start_ms, tz));
  return { t0: Math.max(reply.start_ms, t0), t1: reply.end_ms };
}

// ---------- the pieces ----------
// `shade`: a night with stages is painted by stage, each slice the share of the piece it covers (f0 to f1, 0 to 1) in its own colour.
export type BarSeg = { key: string; row: string; kind: 'stay' | 'trip' | 'sleep'; s: number; e: number; f0: number; f1: number; rgb: Rgb; a: number; tone: 'dark' | 'light'; icon: Icon; name: string; short: string; mono: boolean;
  shade?: { f0: number; f1: number; rgb: Rgb; a: number }[] };
// What is still to come on today: an event as a dashed box over its time, a reminder or a departure as a tick.
// `leaving`: an event he has just said is not his, on its way out.
export type Sched = { key: string; kind: 'event' | 'reminder' | 'departure'; s: number; e: number; f0: number; f1: number; short: string; leaving: boolean };
export type BarStar = { id: string; f: number; label: string; hers: boolean };
export type BarModel = {
  t0: number; t1: number; segs: BarSeg[]; sched: Sched[]; stars: BarStar[]; ticks: { label: string; f: number }[]; nowF: number | null;
  lens: Partial<Record<LensKind, { f0: number; f1: number }[]>>; sun: { kind: 'rise' | 'set'; f: number }[];
};
// The clock an end is written in: the day's end is 24:00, never the next day's 00:00 (the card, the band's edge and the cut all say it so).
export const endClock = (ms: number, reply: { end_ms: number }, tz: string) => ms >= reply.end_ms ? '24:00' : clock(ms, tz);
export const fOf = (bar: { t0: number; t1: number }, ms: number) => (ms - bar.t0) / (bar.t1 - bar.t0);
export const msOf = (bar: { t0: number; t1: number }, f: number) => bar.t0 + f * (bar.t1 - bar.t0);

// Dark ink on a light fill, ink on a dark one, whichever reads better over the black the bar sits on (WCAG contrast).
const INK: Rgb = [238, 240, 251], DARK: Rgb = [11, 13, 26], GROUND: Rgb = [15, 15, 15];
const lin = (c: number) => { const v = c / 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; };
const lum = (c: readonly number[]) => .2126 * lin(c[0]) + .7152 * lin(c[1]) + .0722 * lin(c[2]);
export function toneOn(rgb: Rgb, a: number): 'dark' | 'light' {
  const L = lum(rgb.map((c, i) => c * a + GROUND[i] * (1 - a)));
  return (L + .05) / (lum(DARK) + .05) >= (lum(INK) + .05) / (L + .05) ? 'dark' : 'light';
}
// The richest label a piece of `w` px has room for, with 6 px each side: icon and name, name, icon, nothing.
export const ICON_PX = 12, GAP_PX = 4, PAD_PX = 6;
export type Fit = 'both' | 'name' | 'icon' | 'none';
export function fit(w: number, textW: number, text: string): Fit {
  const room = w - 2 * PAD_PX;
  return text && ICON_PX + GAP_PX + textW <= room ? 'both' : text && textW <= room ? 'name' : ICON_PX <= room ? 'icon' : 'none';
}

const BAR = new Set(['stay', 'sleep', 'move', 'ride']);
// Where things overlap the one that wins is painted (sleep, then a way he went, then a place), so every piece is one thing; the
// track stays empty where nothing was recorded.
export function buildBar(model: DayModel, t: Say): BarModel | null {
  const { reply, items, now, today } = model;
  if (!items.length || !Number.isFinite(reply.start_ms) || !Number.isFinite(reply.end_ms) || reply.end_ms <= reply.start_ms) return null;
  const { t0, t1 } = axisOf(model), f = (ms: number) => (ms - t0) / (t1 - t0), limit = today ? Math.min(now, t1) : t1;
  const covers: { it: DayItem; s: number; e: number; pr: number }[] = [];
  for (const it of items) {
    if (!BAR.has(it.kind)) continue;
    const s = Math.max(it.start_ms, t0), e = Math.min(it.end_ms ?? limit, limit);
    if (e > s) covers.push({ it, s, e, pr: it.kind === 'stay' ? 1 : it.kind === 'sleep' ? 3 : 2 });
  }
  const cuts = [...new Set(covers.flatMap(x => [x.s, x.e]))].sort((x, y) => x - y), pieces: { it: DayItem; s: number; e: number }[] = [];
  for (let i = 0; i + 1 < cuts.length; i++) {
    let best: (typeof covers)[number] | undefined;
    for (const x of covers) if (x.s <= cuts[i] && x.e >= cuts[i + 1] && (!best || x.pr > best.pr || x.pr === best.pr && x.s > best.s)) best = x;
    if (!best) continue;
    const last = pieces.at(-1);
    if (last && last.it === best.it && last.e === cuts[i]) last.e = cuts[i + 1]; else pieces.push({ it: best.it, s: cuts[i], e: cuts[i + 1] });
  }
  const segs = pieces.map(({ it, s, e }, i): BarSeg => {
    const base = { key: `${it.id}#${i}`, row: it.id, s, e, f0: f(s), f1: f(e) };
    if (it.kind === 'stay') {
      const look = stayLook(it, t), a = look.kept ? .9 : .32;
      return { ...base, kind: 'stay', rgb: look.rgb, a, tone: toneOn(look.rgb, a), icon: look.icon, name: look.name, short: look.unnamed ? '' : look.name, mono: false };
    }
    if (it.kind === 'sleep') {
      const minutes = it.minutes ?? ((it.end_ms ?? limit) - it.start_ms) / MIN;
      const shade = it.stages?.length ? stageSlices(it, s, e).map(x => ({ f0: (x.s - s) / (e - s), f1: (x.e - s) / (e - s), rgb: stageLook(x.stage).rgb, a: stageLook(x.stage).a })) : undefined;
      return { ...base, kind: 'sleep', rgb: SLEEP, a: .9, tone: toneOn(SLEEP, .9), icon: 'moon', name: t(['Sleep', '睡觉']), short: hm(minutes), mono: true, shade };
    }
    // A way he went is a light haze with a hatch over it (drawn by the style), so it reads as dark ground.
    const ride = it.kind === 'ride', route = clean(it.route).replace(/\s*→\s*/g, '→');
    return { ...base, kind: 'trip', rgb: TRANSIT, a: .28, tone: 'light', icon: ride ? 'bus' : moveIcon(it.activity), name: ride ? rideWords(it, t) : moveWords(it.activity, t), short: ride ? route || t(['Bus', '公交']) : moveWords(it.activity, t), mono: false };
  });
  const sched: Sched[] = [];
  if (today) for (const it of items) {
    const title = it.private ? t(['Private', '私事']) : clean(it.title) || t(['Event', '日程']);
    if (it.kind === 'event' && (it.end_ms ?? it.start_ms) > now) {
      const s = Math.max(it.start_ms, now, t0), e = Math.min(it.end_ms ?? it.start_ms, t1);
      if (e > s) sched.push({ key: it.id, kind: 'event', s, e, f0: f(s), f1: f(e), short: title, leaving: it.leaving === true });
    } else if ((it.kind === 'reminder' || it.kind === 'departure') && it.start_ms > now && it.start_ms < t1) sched.push({ key: it.id, kind: it.kind, s: it.start_ms, e: it.start_ms, f0: f(it.start_ms), f1: f(it.start_ms), short: '', leaving: false });
  }
  // What mattered: a lit star at the start of each highlighted item that is on the bar.
  const stars: BarStar[] = [];
  for (const [id, mark] of model.stars) {
    const it = items.find(x => x.id === id);
    if (it && it.start_ms <= t1 && (it.end_ms ?? limit) >= t0) stars.push({ id, f: clamp(f(it.start_ms), 0, 1), label: mark.label, hers: mark.by === 'her' });
  }
  stars.sort((x, y) => x.f - y.f);
  // Where each kind of the day's numbers happened (a moment is a point: f0 = f1), for a lens.
  const lens: BarModel['lens'] = {};
  for (const it of items) {
    const k = lensOf(it.kind);
    if (!k) continue;
    const end = it.end_ms ?? (it.kind === 'payment' || it.kind === 'picture' ? it.start_ms : limit);
    if (it.start_ms > t1 || end < t0) continue;
    (lens[k] ??= []).push({ f0: clamp(f(it.start_ms), 0, 1), f1: clamp(f(Math.min(end, t1)), 0, 1) });
  }
  const ticks: BarModel['ticks'] = [];
  for (let h = 0; h <= 24; h += 3) { const ms = hourAt(reply, model.tz, h); if (ms >= t0 && ms <= t1) ticks.push({ label: String(h).padStart(2, '0'), f: f(ms) }); }
  // Where the sun comes up and goes down, when the day has them on its bar.
  const sun: BarModel['sun'] = [];
  for (const [kind, ms] of [['rise', reply.weather?.sunrise_ms], ['set', reply.weather?.sunset_ms]] as const) if (ms != null && f(ms) >= 0 && f(ms) <= 1) sun.push({ kind, f: f(ms) });
  return { t0, t1, segs, sched, stars, ticks, nowF: today ? clamp(f(now), 0, 1) : null, lens, sun };
}

// ---------- what the pointer finds ----------
const when = (it: DayItem, m: DayModel, t: Say) => {
  const { reply, tz } = m, limit = m.today ? m.now : reply.end_ms, ago = it.start_ms < reply.start_ms ? t(['yesterday ', '昨天 ']) : '', from = ago + clock(it.start_ms, tz);
  if (it.end_ms == null) return { range: t([`from ${from}`, `${from} 起`]), length: dur(Math.max(1, (limit - it.start_ms) / MIN), t) };
  const minutes = it.kind === 'sleep' && it.minutes != null ? it.minutes : (it.end_ms - it.start_ms) / MIN;
  return { range: it.end_ms > it.start_ms ? `${from}–${endText(it.end_ms, reply, tz, t)}` : from, length: it.end_ms - it.start_ms >= MIN || it.kind === 'sleep' ? dur(Math.max(1, minutes), t) : '' };
};
export type Line = { key: string; kind: string; icon: Icon; text: string; side: string; star: boolean; picture: string | null };
// In the order they are worth a glance; a moment (a photo, a payment) is "at" a minute within two of it.
const ORDER = ['work', 'code', 'call', 'talk', 'workout', 'picture', 'payment', 'event', 'claude_chat', 'reminder', 'departure'], POINT = new Set(['picture', 'payment', 'reminder', 'departure']);
export const NEAR_MIN = 10, AT_MIN = 2;
function lineOf(it: DayItem, m: DayModel, t: Say): Line {
  const span = when(it, m, t), time = clock(it.start_ms, m.tz), mark = m.stars.get(it.id), base = { key: it.id, kind: it.kind, star: !!mark, picture: null as string | null };
  const put = (icon: Icon, text: string, side: string): Line => ({ ...base, icon, text: mark?.label || text, side });
  switch (it.kind) {
    case 'work': {
      const top = [names(it.projects)[0], names(it.apps)[0]].filter(Boolean).join(' · '), active = it.active_s ? dur(it.active_s / 60, t) : '';
      return put('work', top || t(['Working on the Mac', '在 Mac 上工作']), active ? t([`${active} active`, `活跃 ${active}`]) : span.length);
    }
    case 'code': return put('code', [t(['Claude Code', 'Claude Code']), clean(it.title)].filter(Boolean).join(' · '), span.length);
    case 'workout': { const w = workoutOf(it, t); return put(w.icon, [w.word, w.facts[0]].filter(Boolean).join(' · '), span.length); }
    case 'call': return put('call', clean(it.app) ? t([`Call · ${it.app}`, `通话 · ${it.app}`]) : t(['Call', '通话']), span.length);
    case 'talk': return put('talk', it.turns ? t([`Talked with her · ${it.turns} ${it.turns === 1 ? 'line' : 'lines'}`, `和她说话 · ${it.turns} 句`]) : t(['Talked with her', '和她说话']), span.length || time);
    case 'picture': return { ...put('picture', clean(it.note).split('\n')[0] || t(['Photo', '照片']), time), picture: clean(it.attachment) || null };
    case 'payment': {
      const sign = it.direction === 'in' ? '+' : '';
      return put('payment', [clean(it.counterparty) || clean(it.text), `${sign}${moneyText(it.amount, it.currency)}`].filter(Boolean).join(' · '), time);
    }
    case 'event': return put('event', it.private ? t(['Private', '私事']) : clean(it.title) || t(['Event', '日程']), span.range);
    case 'claude_chat': return put('chat', [t(['Claude', 'Claude']), clean(it.text)].filter(Boolean).join(' · '), it.messages ? t([`${it.messages} msgs`, `${it.messages} 条`]) : span.length);
    case 'reminder': return put('reminder', clean(it.text) || t(['Reminder', '提醒']), time);
    default: return put('bus', clean(it.title) || rideWords(it, t), t([`departs ${time}`, `${time} 发车`]));
  }
}
export type Peek = {
  ms: number; at: string; id: string; row: string | null; lines: Line[]; star: { label: string; by: string } | null;
  stretch: { name: string; rgb: Rgb | null; a: number; range: string; length: string; note?: string };
};
// A night with stages says its deep sleep and its REM on the card (`深睡 1:10 · REM 1:40`), whichever of them it had.
function stageNote(it: DayItem, t: Say) {
  if (!it.stages?.length) return undefined;
  const mins = stageMinutes(it);
  return ([['deep', t(['Deep', '深睡'])], ['rem', 'REM']] as const).filter(([k]) => (mins[k] ?? 0) >= 1).map(([k, word]) => `${word} ${hm(mins[k])}`).join(' · ') || undefined;
}
// The row of the piece of the bar at a minute: the stretch there, or an event still to come. A gap has none, so the pointer in one lifts nothing.
export const pieceAt = (bar: BarModel, ms: number): string | null =>
  bar.segs.find(x => x.s <= ms && ms < x.e)?.row ?? bar.sched.find(x => x.kind === 'event' && x.s <= ms && ms <= x.e)?.key ?? null;
// The list row a click at a minute takes the list to: the piece there, else the next row after it (or the last).
export const rowAt = (model: DayModel, bar: BarModel, ms: number): string | null => pieceAt(bar, ms) ?? (model.index.find(x => x.t0 >= ms) ?? model.index.at(-1))?.key ?? null;
export function peek(model: DayModel, bar: BarModel, ms: number, t: Say): Peek {
  const { items, today, now, reply } = model, limit = today ? now : reply.end_ms;
  const seg = bar.segs.find(x => x.s <= ms && ms < x.e), event = seg ? undefined : bar.sched.find(x => x.kind === 'event' && x.s <= ms && ms <= x.e);
  const item = items.find(x => x.id === (seg?.row ?? event?.key));
  let id: string, stretch: Peek['stretch'];
  if (seg && item) { id = seg.key; stretch = { name: seg.name, rgb: seg.rgb, a: seg.kind === 'stay' ? Math.max(.6, seg.a) : 1, ...when(item, model, t), note: seg.kind === 'sleep' ? stageNote(item, t) : undefined }; }
  else if (event && item) { id = event.key; stretch = { name: event.short, rgb: null, a: 1, ...when(item, model, t) }; }
  else if (ms > limit) { id = 'later'; stretch = { name: t(['Not yet', '还没到']), rgb: null, a: 1, range: '', length: '' }; }
  else {
    // Nothing was recorded here: say how long, from the piece before to the piece after.
    const lo = Math.max(bar.t0, ...bar.segs.filter(x => x.e <= ms).map(x => x.e)), hi = Math.min(limit, ...bar.segs.filter(x => x.s > ms).map(x => x.s));
    id = `gap:${lo}`;
    stretch = { name: t(['Nothing recorded', '没有记录']), rgb: null, a: 1, range: `${clock(lo, model.tz)}–${endClock(hi, reply, model.tz)}`, length: dur(Math.max(1, (hi - lo) / MIN), t) };
  }
  const near: { it: DayItem; rank: number; d: number; at: boolean }[] = [];
  for (const it of items) {
    const rank = ORDER.indexOf(it.kind);
    if (rank < 0 || it.id === item?.id) continue;
    const s = it.start_ms, e = POINT.has(it.kind) ? s : Math.max(s, it.end_ms ?? limit), d = ms < s ? s - ms : ms > e ? ms - e : 0;
    if (d <= NEAR_MIN * MIN) near.push({ it, rank, d, at: d <= (POINT.has(it.kind) ? AT_MIN * MIN : 0) });
  }
  near.sort((x, y) => +!x.at - +!y.at || x.rank - y.rank || x.d - y.d || x.it.start_ms - y.it.start_ms);
  // Sessions of Claude Code run side by side: more than one near is one line (how many, how long together), so they cannot fill the card.
  const codes = near.filter(x => x.it.kind === 'code' && !model.stars.has(x.it.id)), lead = codes.length > 1 ? codes[0] : null;
  const lines = near.filter(x => !lead || x === lead || !codes.includes(x)).slice(0, 4).map(x => x !== lead ? lineOf(x.it, model, t)
    : { key: `code:${x.it.id}`, kind: 'code', icon: 'code' as Icon, text: t([`Claude Code · ${codes.length} sessions`, `Claude Code · ${codes.length} 个会话`]), side: dur(Math.max(1, covered(codes.map(c => c.it), limit)), t), star: false, picture: null });
  const mark = item ? model.stars.get(item.id) ?? null : null;
  return { ms, at: clock(ms, model.tz), id, row: pieceAt(bar, ms), lines, star: mark, stretch };
}

// ---------- the day's numbers, and a range of the bar ----------
export type Num = { kind: LensKind; label: string; value: string; long: string };
const LABELS: Record<LensKind, [string, string]> = { sleep: ['Sleep', '睡'], trip: ['On the way', '路上'], workout: ['Workout', '运动'], work: ['Mac', 'Mac'], call: ['Calls', '通话'], talk: ['With her', '和她'], payment: ['Spent', '花'], picture: ['Photos', '照片'] };
// What each number adds up: the items it counts, each with its end (one still going ends at `limit`), the ms of it inside the window and that
// as a share of its length (an instant counts whole). Sleep is counted whole by the day it ends on; in a window, by the share of it inside (as
// is Mac time: a block's active seconds are spread over its length). The chips, a range's card and a number's card all count with this.
export type Counted = { it: DayItem; s: number; e: number; lap: number; share: number };
export function counted(model: DayModel, win?: { a: number; b: number }): Record<LensKind, Counted[]> {
  const { reply, today, now } = model, a = win?.a ?? reply.start_ms, b = win?.b ?? reply.end_ms, limit = today ? Math.min(now, b) : b;
  const by: Record<LensKind, Counted[]> = { sleep: [], trip: [], workout: [], work: [], call: [], talk: [], payment: [], picture: [] };
  for (const it of model.items) {
    const k = lensOf(it.kind);
    if (!k || it.kind === 'code') continue;
    const s = it.start_ms, e = it.end_ms ?? limit, lap = Math.max(0, Math.min(e, b) - Math.max(s, a)), share = e > s ? lap / (e - s) : 1;
    const counts = k === 'sleep' ? win ? lap > 0 : e > a && e <= b : k === 'talk' ? e >= a && s <= b : k === 'payment' || k === 'picture' ? s >= a && s <= b : k === 'work' ? share > 0 : lap > 0;
    if (counts) by[k].push({ it, s, e, lap, share });
  }
  return by;
}
// What the day, or a window of it, adds up to: only what is not nothing.
export function numbers(model: DayModel, t: Say, win?: { a: number; b: number }): Num[] {
  const by = counted(model, win), out = new Map<string, { n: number; sum: number }>(), add = (list: Counted[], f: (c: Counted) => number) => list.reduce((n, c) => n + f(c), 0);
  const sum: Record<LensKind, number> = {
    sleep: add(by.sleep, c => (c.it.minutes ?? (c.e - c.s) / MIN) * (win ? c.share : 1)), trip: add(by.trip, c => c.lap / MIN), workout: add(by.workout, c => c.lap / MIN), call: add(by.call, c => c.lap / MIN),
    work: add(by.work, c => (c.it.active_s ?? (c.e - c.s) / 1000) / 60 * c.share), talk: by.talk.length, payment: by.payment.length, picture: by.picture.length,
  };
  for (const { it } of by.payment) {
    const amount = Number.parseFloat(String(it.amount));
    if (it.direction === 'out' && Number.isFinite(amount)) { const c = clean(it.currency).toUpperCase(), o = out.get(c) ?? { n: 0, sum: 0 }; out.set(c, { n: o.n + 1, sum: o.sum + amount }); }
  }
  const nums: Num[] = [];
  for (const kind of Object.keys(LABELS) as LensKind[]) {
    const label = t(LABELS[kind]), n = sum[kind];
    let value = '', long = '';
    if (kind === 'sleep' || kind === 'trip' || kind === 'work') { if (Math.round(n) > 0) { value = hm(n); long = dur(n, t); } }
    else if (kind === 'call' || kind === 'workout') { if (Math.round(n) > 0) value = long = dur(n, t); }
    else if (kind === 'talk') { if (n > 0) value = long = t([`${n}×`, `${n} 次`]); }
    else if (kind === 'picture') { if (n > 0) value = long = String(n); }
    else if (out.size) {
      // The money out in the currency he paid in most; where he paid in more than one, an … says the number is not all of it.
      const [cur, o] = [...out].sort((x, y) => y[1].n - x[1].n || y[1].sum - x[1].sum)[0];
      value = long = moneyText(o.sum.toFixed(2), cur) + (out.size > 1 ? '…' : '');
    }
    if (value) nums.push({ kind, label, value, long });
  }
  return nums;
}

export type Summary = { a: number; b: number; range: string; length: string; places: { name: string; rgb: Rgb; length: string }[]; nums: Num[] };
// A range is chosen in whole minutes: the band, its card, the chip and the cut all say the same times.
export const snap = (ms: number) => Math.round(ms / MIN) * MIN;
export function summarize(model: DayModel, bar: BarModel, a: number, b: number, t: Say): Summary {
  [a, b] = [snap(Math.min(a, b)), snap(Math.max(a, b))];
  const by = new Map<string, { rgb: Rgb; min: number }>();
  for (const x of bar.segs) {
    const lap = Math.min(x.e, b) - Math.max(x.s, a);
    if (x.kind === 'stay' && lap > 0) by.set(x.name, { rgb: x.rgb, min: (by.get(x.name)?.min ?? 0) + lap / MIN });
  }
  const places = [...by].sort((x, y) => y[1].min - x[1].min).slice(0, 3).map(([name, v]) => ({ name, rgb: v.rgb, length: dur(Math.max(1, v.min), t) }));
  return { a, b, range: `${clock(a, model.tz)}–${endClock(b, model.reply, model.tz)}`, length: dur((b - a) / MIN, t), places, nums: numbers(model, t, { a, b }) };
}

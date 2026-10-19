import type { L } from './companionSettings';
import { endClock, hm } from './barView.ts';
import { accountOf, askable, clean, clock, dur, endText, moneyText, rideWords, stageMinutes, stageSlices, starrable, stayLook, workoutOf, type DayItem, type DayModel, type Icon, type Say } from './dayView.ts';

// What a row says when it is opened (ADR 0296, spec-r3 section 8), and what can be done from it. Pure (no DOM), so
// scripts/verify-notch-timeline.mjs runs it in node. Nothing here is made up: a part exists only where the brain sent what it
// shows, and an action exists only where its route works today (the rename of a kept place, the acknowledgement of a reminder that
// rang, a turn that is handed to her). DayTimeline.tsx and RowOpen.tsx draw it.
// An open row adds to its two lines and its right column, it does not say them again: a move's word and length, a ride's route, a
// payment's who and what are the row's already. What the row may have cut (a title over 20 characters, a second line over 40) is said in
// full. The rows that had little else to open to: a call's app, a Claude chat's count of messages, and a workout's pace or speed, each
// with when it ran.
export type Tone = 'ink' | 'muted';
export type Words = { text: string; tone: Tone };
export type Bar = { name: string; text: string; share: number };
// One Claude Code session: when it began, its title, `project · A`, how long, and its recap in full.
export type Session = { id: string; at: string; title: string; meta: string; length: string; recap: string };
// What the night looked like: where each stage was on the row's own time scale, and the totals in words.
export type Night = { from: number; to: number; at: [string, string]; slices: { stage: string; s: number; e: number }[]; totals: string };
export type Part =
  | { type: 'lines'; lines: Words[] }
  | { type: 'talk'; day: string; from: number; to: number }
  | { type: 'picture'; id: string; note: string; show: boolean }
  | { type: 'bars'; heading: string; bars: Bar[] }
  | { type: 'night'; night: Night }
  | { type: 'sessions'; list: Session[] }
  | { type: 'titles'; list: string[] };
// `rename`: the kept place under a stay, and the name it has; `ack`: the reminder that rang and waits for a `知道了`; `summarize`: a work block
// can be handed to her at one tap; `star`: the item can be marked as what mattered (every item with an id the brain takes; a folded row of
// sessions is no one item); `commit`: the hex of a commitment mail noted that is not his yet, which he can say is his or is not;
// `name`: where a stay was that is no place he keeps, which he can give a name (and so keep).
export type Open = { key: string; kind: string; parts: Part[]; rename: { id: string; name: string } | null; name: { lat: number; lng: number } | null; ack: string | null; summarize: boolean; star: boolean; commit: string | null };
// What a turn about a row carries (ADR 0214): exactly these four, the brain refuses a turn whose `about` has more or a bad id.
export type About = { kind: string; id: string; title: string; start_ms: number };
export type Ask = { about: About; icon: Icon; at: string };

export const ATTACHMENT = /^[0-9a-f]{32}$/;
// A commitment's item is `commitment-<hex>@<day>` (ADR 0274); the route that confirms or drops it takes the hex between the two.
const COMMITMENT = /^commitment-([0-9a-f]{32})@/;
const say = (text: string, tone: Tone = 'muted'): Words => ({ text, tone });
const lines = (...list: (Words | false | '' | null | undefined)[]): Part => ({ type: 'lines', lines: list.filter((x): x is Words => !!x) });
// The words the row already has (its title: 20 characters, its second line: 40) are said again in the open row only when they are long enough that the row's one line may have cut them.
const LONG = 20, WIDE = 40, again = (text: string, limit = LONG) => text.length > limit ? say(text) : null;
// How long an item lasted, or has lasted so far; a thing still to come has no length.
const lengthOf = (it: DayItem, m: DayModel) => it.end_ms == null && it.state === 'next' ? 0 : Math.max(0, ((it.end_ms ?? (m.today ? m.now : m.reply.end_ms)) - it.start_ms) / 60_000);
// `14:05–14:50 · 45 分`, `18:50 起`, or the time alone for a moment.
function span(it: DayItem, m: DayModel, t: Say) {
  const a = clock(it.start_ms, m.tz);
  if (it.end_ms == null) return it.state === 'next' ? a : t([`From ${a}`, `${a} 起`]);
  return it.end_ms - it.start_ms >= 60_000 ? `${a}–${endText(it.end_ms, m.reply, m.tz, t)} · ${dur(lengthOf(it, m), t)}` : a;
}

// How a ride ended, in plain words (jarvis/state/rides.py END_REASONS); a reason this does not know says nothing.
const ENDED: Record<string, L> = {
  left_vehicle: ['You got off', '你下车了'], passed_stop: ['It reached your stop', '到了你要下的站'], said: ['You said you got off', '你说你下车了'],
  schedule_end: ['The bus finished its run', '这趟车跑完了'], replaced: ['You boarded another bus', '你换了另一班车'],
};
// The stays he left before a move and reached after it (within half an hour), by name: where the move went from and to.
function around(m: DayModel, it: DayItem, t: Say) {
  const stays = m.items.filter(x => x.kind === 'stay'), gap = 30 * 60_000, name = (s?: DayItem) => s ? stayLook(s, t).name : '';
  const before = stays.filter(x => x.end_ms != null && x.end_ms <= it.start_ms + 60_000 && x.end_ms >= it.start_ms - gap).sort((a, b) => b.end_ms! - a.end_ms!)[0];
  const after = it.end_ms == null ? undefined : stays.filter(x => x.start_ms >= it.end_ms! - 60_000 && x.start_ms <= it.end_ms! + gap).sort((a, b) => a.start_ms - b.start_ms)[0];
  return [name(before), name(after)];
}
// Where a move went from and to, or a ride (by its stops): `家 → A&W`, `From 家`, `To A&W`; nothing when neither is known.
export function fromTo(m: DayModel, it: DayItem, t: Say) {
  if (it.kind === 'ride') return [clean(it.board_stop), clean(it.alight_stop)].filter(Boolean).join(' → ');
  const [from, to] = around(m, it, t);
  return from && to ? `${from} → ${to}` : from ? t([`From ${from}`, `从 ${from} 出发`]) : to ? t([`To ${to}`, `去 ${to}`]) : '';
}
// The biggest five of what the work block was spent on, each as its share of the active time.
export function bars(list: DayItem['projects'], active: number, t: Say): Bar[] {
  const top = (list ?? []).filter(x => clean(x.name) && x.seconds > 0).sort((a, b) => b.seconds - a.seconds).slice(0, 5), base = active > 0 ? active : Math.max(...top.map(x => x.seconds), 1);
  return top.map(x => ({ name: clean(x.name), text: dur(Math.max(1, x.seconds / 60), t), share: Math.max(0, Math.min(1, x.seconds / base)) }));
}
export function night(it: DayItem, m: DayModel, t: Say): Night | null {
  if (!it.stages?.length) return null;
  const from = it.start_ms, to = it.end_ms ?? m.now, slices = stageSlices(it, from, to), mins = stageMinutes({ ...it, end_ms: to }), awake = slices.filter(x => x.stage === 'awake').length;
  const totals = [mins.deep >= 1 && `${t(['Deep', '深睡'])} ${hm(mins.deep)}`, mins.core >= 1 && `${t(['Core', '核心'])} ${hm(mins.core)}`, mins.rem >= 1 && `REM ${hm(mins.rem)}`, awake > 0 && t([`Awake ${awake}×`, `醒 ${awake} 次`])]
    .filter((x): x is string => !!x).join(' · ');
  return { from, to, at: [clock(from, m.tz), clock(to, m.tz)], slices, totals };
}
const session = (it: DayItem, m: DayModel, t: Say): Session => ({ id: it.id, at: clock(it.start_ms, m.tz), title: clean(it.title) || t(['Claude Code', 'Claude Code']), meta: [clean(it.project), accountOf(it)].filter(Boolean).join(' · '),
  length: dur(Math.max(1, lengthOf(it, m)), t), recap: clean(it.detail) });

export function openOf(m: DayModel, key: string, t: Say): Open | null {
  const sessions = m.folds.get(key), it = sessions ? sessions[0] : m.items.find(x => x.id === key);
  if (!it) return null;
  const parts: Part[] = [];
  let rename: Open['rename'] = null, name: Open['name'] = null, ack: string | null = null, summarize = false, commit: string | null = null;
  switch (sessions ? 'fold' : it.kind) {
    case 'fold': parts.push({ type: 'sessions', list: [...sessions!].sort((a, b) => a.start_ms - b.start_ms).map(x => session(x, m, t)) }); break;
    case 'code': parts.push({ type: 'sessions', list: [session(it, m, t)] }); break;
    case 'stay': {
      const look = stayLook(it, t), place = clean(it.place);
      parts.push(lines(place && say(place), look.kept && say(t([`A place you keep · ${look.name}`, `常去的地方 · ${look.name}`]))));
      if (it.kept_place && clean(it.kept_place.name)) rename = { id: it.kept_place.id, name: clean(it.kept_place.name) };
      else if (m.heads.has(key) && it.lat !== undefined && it.lng !== undefined && Math.abs(it.lat) <= 90 && Math.abs(it.lng) <= 180) name = { lat: it.lat, lng: it.lng };
      break;
    }
    case 'move': {
      const words = fromTo(m, it, t);
      parts.push(lines(words && say(words)));
      break;
    }
    case 'ride': {
      const credit = clean(it.credit), ended = it.end_ms == null ? it.state === 'next' ? '' : t(['Still on it', '还在车上']) : ENDED[clean(it.ended_reason)] ? t(ENDED[clean(it.ended_reason)]) : '';
      parts.push(lines(again([clean(it.board_stop), clean(it.alight_stop)].filter(Boolean).join(' → '), WIDE), credit && say(t([`Live: ${credit}`, `实时：${credit}`])), ended && say(ended)));
      break;
    }
    case 'talk':
      parts.push({ type: 'talk', day: m.reply.date, from: it.start_ms, to: it.end_ms ?? it.start_ms });
      if (it.turns) parts.push(lines(say(t([`${it.turns} ${it.turns === 1 ? 'line' : 'lines'} in all`, `共 ${it.turns} 句`]))));
      break;
    case 'picture': {
      // The row already says the first line of the note: the open row says it (all of it) when there is more than that to say.
      const id = ATTACHMENT.test(clean(it.attachment)) ? clean(it.attachment) : '', note = clean(it.note), show = note.includes('\n') || note.length > LONG;
      if (id || show) parts.push({ type: 'picture', id, note, show });
      break;
    }
    case 'event': {
      // Where it came from, that it is not his yet and the place are the row's second line; the time range, that mail has confirmed it, and its state are new.
      const sure = it.source === 'mail' && it.confirmed === true;
      const state = it.state === 'now' ? t(['Happening now', '正在进行']) : it.state === 'next' ? t(['Not yet', '还没开始']) : t(['Over', '已结束']);
      parts.push(lines(say([span(it, m, t), sure ? t(['confirmed', '已确认']) : '', state].filter(Boolean).join(' · ')), !it.private && again(clean(it.place))));
      if (it.source === 'mail' && it.confirmed === false && !it.leaving) commit = COMMITMENT.exec(it.id)?.[1] ?? null;
      break;
    }
    case 'reminder': {
      parts.push(lines(again(clean(it.text))));
      if (it.fired === true && it.acknowledged === false) ack = it.id;
      break;
    }
    case 'departure': {
      // The row has the route, where to and when to leave; the stop only if its title does not carry it.
      const stop = clean(it.board_stop);
      if (stop && !(m.rows.get(key)?.title ?? '').includes(stop)) parts.push(lines(say([rideWords(it, t), stop].join(' · '))));
      break;
    }
    case 'work': {
      const active = it.active_s ? dur(it.active_s / 60, t) : '', total = dur(Math.max(1, lengthOf(it, m)), t);
      parts.push(lines(say(active ? t([`Active ${active} · ${total} in all`, `活跃 ${active} · 共 ${total}`]) : t([`${total} in all`, `共 ${total}`]))));
      for (const [heading, list] of [[t(['Projects', '项目']), it.projects], [t(['Apps', '应用']), it.apps]] as const) {
        const b = bars(list, it.active_s ?? 0, t);
        if (b.length) parts.push({ type: 'bars', heading, bars: b });
      }
      summarize = askable(it.kind, it.id);
      break;
    }
    case 'sleep': {
      const n = night(it, m, t);
      if (n) parts.push({ type: 'night', night: n }, lines(n.totals && say(n.totals)));
      else parts.push(lines(say(span(it, m, t))));
      break;
    }
    // When it ran, to the minute, with the pace or speed (only with a distance); what was counted is the row's second line, said again only when long.
    case 'workout': {
      const w = workoutOf(it, t);
      parts.push(lines(say([span(it, m, t), w.pace].filter(Boolean).join(' · ')), again([w.dist, w.kcal, w.hr].filter(Boolean).join(' · '), WIDE)));
      break;
    }
    case 'call': parts.push(lines(say([span(it, m, t), clean(it.app)].filter(Boolean).join(' · ')))); break;
    case 'payment': {
      // Who, what and through which app are the row's; which way the money went and in what currency are new.
      const way = it.direction === 'in' ? t(['Received', '收入']) : it.direction === 'out' ? t(['Spent', '支出']) : '', cur = clean(it.currency).toUpperCase();
      parts.push(lines(say([way, moneyText(it.amount, it.currency), cur].filter(Boolean).join(' · '))));
      break;
    }
    case 'claude_chat': {
      const title = clean(it.text), n = it.messages ?? 0, count = n > 0 ? t([`${n} ${n === 1 ? 'message' : 'messages'}`, `${n} 条`]) : '';
      parts.push(lines(again(title), say([span(it, m, t), count].filter(Boolean).join(' · '))));
      break;
    }
    case 'claude_day': {
      const titles = clean(it.text).split(/\s*;\s*/).filter(Boolean);
      if (titles.length) parts.push({ type: 'titles', list: titles });
      break;
    }
    default: return null;
  }
  // What the open row says first is in ink, the rest is muted.
  const shown = parts.filter(p => p.type !== 'lines' || p.lines.length > 0), top = shown[0];
  if (top?.type === 'lines') top.lines[0] = { ...top.lines[0], tone: 'ink' };
  return { key, kind: sessions ? 'code' : it.kind, parts: shown, rename, name, ack, summarize, star: !sessions && starrable(it.id), commit };
}

// One line of the day's conversation as GET /inherent/memory/day/{day}/records gives it (ts read to ms), and what a talk row shows of its span:
// his first line in it and her first reply to that, each in one line (a record's own line breaks and its record marker are not a line's).
export type Rec = { id: string; ts: number; who: 'user' | 'jarvis'; text: string };
const MARKER = /\s*\[record_id=[^\]]*\]/g, oneLine = (s: string) => s.replace(MARKER, '').replace(/\s+/g, ' ').trim();
export function talkLines(list: Rec[], from: number, to: number): { you: string; her: string } | null {
  const you = list.find(r => r.who === 'user' && r.ts >= from - 2000 && r.ts <= to + 2000 && oneLine(r.text));
  if (!you) return null;
  const her = list.find(r => r.who === 'jarvis' && r.ts >= you.ts && r.ts <= to + 10 * 60_000 && oneLine(r.text));
  return { you: oneLine(you.text), her: her ? oneLine(her.text) : '' };
}

// Whether opening the row shows anything: a part, or a button that works (so a row never opens to nothing).
export const hasBody = (o: Open, canAsk: boolean) => o.parts.length > 0 || !!o.rename || !!o.name || !!o.ack || o.summarize || o.star || !!o.commit || canAsk;

// What a turn about this row carries and what the input row shows of it: null when the brain would refuse it (a kind it does not take, an id
// of the wrong shape, a folded row that is no one item).
export function askOf(m: DayModel, key: string, t: Say): Ask | null {
  const it = m.items.find(x => x.id === key);
  if (!it || !askable(it.kind, it.id)) return null;
  const head = m.heads.get(key), row = m.rows.get(key), title = (head?.name ?? row?.title ?? '').trim();
  return { about: { kind: it.kind, id: it.id, title: title || t(['An item of the day', '这一条']), start_ms: Math.round(it.start_ms) }, icon: row?.icon && row.icon !== 'dot' ? row.icon : head?.icon ?? 'pin', at: clock(it.start_ms, m.tz) };
}

// What he can say about a stretch of the day (ADR 0302): a range of the shown day, cut to the day and to now, that he may tell her was this or that. A stretch is
// what lasts and has begun: a stay, a way he went, the night, work, a call, a run, a session. Not her rows (a reminder, a talk), a point (a picture, a payment,
// a bus to catch), an event, the day's letter on his chats, or anything still to come. A range is at least a minute (the chip says it to the minute).
export type Said = { day: string; a: number; b: number; range: string };
const NOT_STRETCH = new Set(['reminder', 'talk', 'picture', 'payment', 'departure', 'event', 'claude_day']);
export function sayRange(m: DayModel, a: number, b: number): Said | null {
  const lo = Math.round(Math.max(a, m.reply.start_ms)), hi = Math.round(Math.min(b, m.today ? m.now : m.reply.end_ms));
  return hi - lo >= 60_000 ? { day: m.reply.date, a: lo, b: hi, range: `${clock(lo, m.tz)}–${endClock(hi, m.reply, m.tz)}` } : null;
}
// The range of a row that is one item (a folded row of sessions is none), or null when the row is no stretch.
export function sayOf(m: DayModel, key: string): Said | null {
  const it = m.items.find(x => x.id === key);
  return !it || it.lane === 'her' || NOT_STRETCH.has(it.kind) ? null : sayRange(m, it.start_ms, it.end_ms ?? Infinity);
}
// The body of POST /inherent/day/answer: his words for the range, trimmed, 1 to 200 characters counted as the brain counts them (by character, not by UTF-16 unit);
// null when there are none or too many, so nothing is sent.
export const SAY_MAX = 200;
export function answerOf(said: Said, text: string) {
  const words = text.trim();
  return words && [...words].length <= SAY_MAX ? { day: said.day, start_ms: said.a, end_ms: said.b, text: words } : null;
}

import type { L } from './companionSettings';

// The owner's day as GET /inherent/day serves it (docs/spec.html; ADR 0199, 0235): items in time order, structured and
// never worded. This file reads them for the notch's timeline (ADR 0296): the wing's one line, and the pane's rows and bar.
// Nothing here is made up: a field that did not arrive is a row without it, a kind this file does not know is left out.
// Pure (no DOM), so scripts/verify-notch-timeline.mjs runs it in node.
type Top = { name: string; seconds: number };
export type DayItem = {
  id: string; lane: 'you' | 'her'; kind: string; start_ms: number; end_ms: number | null; state: 'done' | 'now' | 'next';
  place?: string; kept_place?: { id: string; name: string; role: string }; activity?: string; minutes?: number;
  text?: string; active_s?: number; projects?: Top[]; apps?: Top[]; app?: string;
  route?: string; board_stop?: string; alight_stop?: string | null; turns?: number; note?: string;
  title?: string; source?: string; confirmed?: boolean; private?: boolean; to?: string; leave_at_ms?: number;
  amount?: string; currency?: string; direction?: string; counterparty?: string; messages?: number; chats?: number;
};
export type DayReply = {
  date: string; tz: string; start_ms: number; end_ms: number; now_ms: number; items: DayItem[];
  labels?: { title: string; source?: string; private?: boolean }[]; missing?: string[];
  line?: { text: string; draft?: boolean; written_at?: string } | null; highlights?: { id: string; label: string; by: string }[];
};
export type Say = (l: L) => string;
export type Rgb = readonly [number, number, number];
export type Icon = 'moon' | 'walk' | 'run' | 'bike' | 'car' | 'bus' | 'talk' | 'picture' | 'event' | 'reminder' | 'work' | 'call' | 'home' | 'school' | 'pin' | 'payment' | 'chat';

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
const clean = (s?: string | null) => (s ?? '').trim();
// An item whose times are not numbers is left out: a time that cannot be formatted would take the whole pane down with it.
const sound = (it: DayItem) => Number.isFinite(it.start_ms) && (it.end_ms == null || Number.isFinite(it.end_ms));
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
// (`27 路 · 12 分`, `步行 · 6 分`); else `今天`.
export function wingText(day: DayReply | null, now: number, t: Say): { head: string; tail: string } {
  let moving: DayItem | undefined, stay: DayItem | undefined;
  for (const it of day?.items ?? []) {
    if (!sound(it) || it.start_ms > now || it.end_ms != null && it.end_ms <= now) continue;
    if ((it.kind === 'ride' || it.kind === 'move') && (!moving || it.start_ms >= moving.start_ms)) moving = it;
    else if (it.kind === 'stay' && (!stay || it.start_ms >= stay.start_ms)) stay = it;
  }
  if (moving) return { head: moving.kind === 'ride' ? rideWords(moving, t) : moveWords(moving.activity, t), tail: dur(Math.floor((now - moving.start_ms) / 60_000), t) };
  if (stay) { const look = stayLook(stay, t); return { head: look.unnamed ? t(['Staying', '停留']) : look.name, tail: since(now - stay.start_ms) }; }
  return { head: t(['Today', '今天']), tail: '' };
}

// ---------- the pane ----------
export type Row = { key: string; icon: Icon; title: string; detail: string; when: string; dur: string; future: boolean; star: boolean; connector: boolean };
// `star`: null when the stay is not one of the day's highlights, else its label (which may be empty).
export type StayHead = { key: string; name: string; unnamed: boolean; rgb: Rgb; icon: Icon; when: string; dur: string; star: string | null };
export type Block = { type: 'row'; t: number; row: Row } | { type: 'now'; t: number; key: string; label: string } | { type: 'stay'; t: number; key: string; head: StayHead; items: Block[] };
export type BarSeg = { key: string; row: string; kind: 'stay' | 'move' | 'sleep'; f0: number; f1: number; rgb: Rgb; a: number; icon: Icon; label: string; name: string };
export type Bar = { segs: BarSeg[]; ticks: { label: string; f: number }[]; nowF: number | null };
export type DayModel = { title: string; today: boolean; chips: { text: string; more: boolean }[]; line: { text: string; draft: boolean } | null; blocks: Block[]; bar: Bar | null; foot: string; empty: boolean };

const SOURCES: Record<string, L> = { phone: ['The phone', '手机'], reminders: ['Reminders', '提醒'], mac: ['Mac time', 'Mac 的时间'], talks: ['Conversations', '对话'], rides: ['Bus rides', '公交乘车'],
  pictures: ['Pictures', '图片'], calendar: ['The calendar', '日历'], departures: ['Pinned buses', '钉着的公交'], commitments: ['What he has to go to', '要去的事'] };
const PAYERS: Record<string, L> = { wechat_pay: ['WeChat Pay', '微信'], alipay: ['Alipay', '支付宝'], boc: ['Bank of China', '中国银行'] };
const SYMBOLS: Record<string, string> = { CAD: '$', USD: 'US$', CNY: '¥', EUR: '€', GBP: '£', JPY: 'JP¥' };
// `$12.40`, `¥68.00`; a currency with no sign of its own is written after the number (`12.40 CHF`).
export function moneyText(amount: unknown, currency?: string) {
  const a = clean(typeof amount === 'number' ? amount.toFixed(2) : String(amount ?? '')), c = clean(currency).toUpperCase();
  return !a ? '' : c in SYMBOLS ? `${SYMBOLS[c]}${a}` : `${a} ${c}`.trim();
}
// What can stand inside a stay; moves and rides never do, they are the ways between.
const NESTED = new Set(['sleep', 'talk', 'picture', 'event', 'reminder', 'work', 'call', 'departure', 'payment', 'claude_chat', 'claude_day']);
const BAR_KINDS = new Set(['stay', 'sleep', 'move', 'ride']);
const SLEEP: Rgb = [112, 120, 205], TRANSIT: Rgb = [214, 222, 250];
type Ctx = { t: Say; tz: string; reply: DayReply; now: number; today: boolean; marks: Map<string, string> };

const names = (list?: Top[]) => (list ?? []).map(x => clean(x.name)).filter(Boolean);
// The end of a span: `24:00` at the day's end, `次日 02:10` past it.
const endText = (ms: number, reply: DayReply, tz: string, t: Say) => ms === reply.end_ms ? '24:00' : (ms > reply.end_ms ? t(['next day ', '次日 ']) : '') + clock(ms, tz);
const dayBefore = (it: DayItem, c: Ctx) => it.start_ms < c.reply.start_ms ? c.t(['yesterday ', '昨天 ']) : '';
function rowOf(it: DayItem, c: Ctx): Row | null {
  const { t, tz, reply } = c;
  let icon: Icon, title: string, detail = '', instant = false, amount = '';
  switch (it.kind) {
    case 'sleep': icon = 'moon'; title = t(['Sleep', '睡觉']); break;
    case 'move': icon = moveIcon(it.activity); title = moveWords(it.activity, t); break;
    case 'ride': icon = 'bus'; title = rideWords(it, t); detail = [clean(it.board_stop), clean(it.alight_stop)].filter(Boolean).join(' → '); break;
    case 'talk': icon = 'talk'; title = t(['Talked with her', '和她说话']); detail = it.turns ? t([`${it.turns} ${it.turns === 1 ? 'line' : 'lines'}`, `${it.turns} 句`]) : ''; break;
    case 'picture': icon = 'picture'; title = t(['Photo', '照片']); detail = clean(it.note).split('\n')[0]; instant = true; break;
    case 'event':
      icon = 'event'; title = it.private ? t(['Private', '私事']) : clean(it.title) || t(['Event', '日程']);
      detail = it.private ? '' : [clean(it.place), it.source === 'mail' && it.confirmed === false ? t(['read from mail, not confirmed', '从邮件里读到，还没确认']) : ''].filter(Boolean).join(' · '); break;
    case 'reminder': icon = 'reminder'; title = clean(it.text) || t(['Reminder', '提醒']); instant = true; break;
    case 'work': {
      icon = 'work'; title = t(['Working on the Mac', '在 Mac 上工作']);
      const top = [...new Set([...names(it.projects), ...names(it.apps)])].slice(0, 3).join(' · '), active = it.active_s ? dur(it.active_s / 60, t) : '';
      detail = [top, active && t([`active ${active}`, `活跃 ${active}`])].filter(Boolean).join(' · '); break;
    }
    case 'call': icon = 'call'; title = clean(it.app) ? t([`Call · ${it.app}`, `通话 · ${it.app}`]) : t(['Call', '通话']); break;
    case 'departure':
      icon = 'bus'; title = clean(it.title) || rideWords(it, t); instant = true;
      detail = [clean(it.to) && t([`to ${it.to}`, `去 ${it.to}`]), it.leave_at_ms != null && t([`leave ${clock(it.leave_at_ms, tz)}`, `${clock(it.leave_at_ms, tz)} 出门`])].filter(Boolean).join(' · '); break;
    case 'payment': {
      // Plain for now: who, what and through which app on the left, the amount (money out reads −, money in +) where a length would be.
      icon = 'payment'; instant = true; const who = clean(it.counterparty), what = clean(it.text), via = PAYERS[clean(it.source)];
      title = who || what || t(['Payment', '付款']);
      detail = [who && what !== who ? what : '', via ? t(via) : ''].filter(Boolean).join(' · ');
      amount = `${it.direction === 'out' ? '−' : it.direction === 'in' ? '+' : ''}${moneyText(it.amount, it.currency)}`; break;
    }
    case 'claude_chat':
      icon = 'chat'; title = clean(it.text) || t(['Claude chat', 'Claude 对话']);
      detail = [t(['With Claude', '和 Claude']), it.messages ? t([`${it.messages} ${it.messages === 1 ? 'message' : 'messages'}`, `${it.messages} 条消息`]) : ''].filter(Boolean).join(' · '); break;
    case 'claude_day': {
      icon = 'chat'; const n = it.chats ?? 0;
      title = n ? t([`Talked with Claude · ${n} ${n === 1 ? 'chat' : 'chats'}`, `和 Claude 聊了 ${n} 个对话`]) : t(['Talked with Claude', '和 Claude 聊过']);
      detail = clean(it.text).split(/\s*;\s*/).filter(Boolean).join(' · '); break;
    }
    default: return null;
  }
  const connector = it.kind === 'move' || it.kind === 'ride';
  // Started and not over: `14:50 起` and the length so far; over: `14:50–15:19`; a moment (a reminder, a photo, a one-line talk): its time.
  const going = !instant && it.end_ms == null && it.state !== 'next', span = !instant && it.end_ms != null && it.end_ms - it.start_ms >= 60_000;
  let when = dayBefore(it, c) + clock(it.start_ms, tz), length = 0;
  if (going) { when = t([`from ${when}`, `${when} 起`]); length = ((c.today ? c.now : reply.end_ms) - it.start_ms) / 60_000; }
  else if (span) { when += `–${endText(it.end_ms!, reply, tz, t)}`; length = (it.end_ms! - it.start_ms) / 60_000; }
  if (it.kind === 'sleep' && it.minutes != null) length = it.minutes;
  const long = length > 0 ? dur(Math.max(1, length), t) : '';
  // A move or a ride says its length in its words (`27 路 · 29 分`); every other span, beside its time.
  if (connector && long) title = `${title} · ${long}`;
  const mark = c.marks.get(it.id);
  if (mark) { detail = [title, detail].filter(Boolean).join(' · '); title = mark; }
  return { key: it.id, icon, title, detail, when, dur: connector ? '' : amount || long, future: c.today && it.start_ms > c.now, star: c.marks.has(it.id), connector };
}

function stayHead(it: DayItem, c: Ctx): StayHead {
  const { t, tz, reply } = c, look = stayLook(it, t), before = dayBefore(it, c);
  const end = it.end_ms ?? (c.today ? c.now : reply.end_ms);
  const when = it.end_ms == null ? t([`from ${before}${clock(it.start_ms, tz)}`, `${before}${clock(it.start_ms, tz)} 起`]) : `${before}${clock(it.start_ms, tz)}–${endText(it.end_ms, reply, tz, t)}`;
  return { key: it.id, name: look.name, unnamed: look.unnamed, rgb: look.rgb, icon: look.icon, when, dur: dur(Math.max(1, (end - it.start_ms) / 60_000), t), star: c.marks.get(it.id) ?? null };
}

const byTime = (a: DayItem, b: DayItem) => a.start_ms - b.start_ms || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
// The day as the pane reads it: each stay as a group with what happened inside it, the ways he went between stays (a move, a bus
// ride) and whatever fell outside any stay (a sleep before he got home), all in time order; on today a now line, with what is
// still to come below it.
export function buildDay(reply: DayReply, now: number, t: Say): DayModel {
  const tz = reply.tz, today = now >= reply.start_ms && now < reply.end_ms;
  // A highlight's label is the owner's or hers; with neither, the brain writes `kind HH:MM`, which says less than the row does.
  const marks = new Map<string, string>();
  for (const h of reply.highlights ?? []) marks.set(h.id, /^[a-z_]+ \d{1,2}:\d{2}$/i.test(clean(h.label)) ? '' : clean(h.label));
  const c: Ctx = { t, tz, reply, now, today, marks };
  const seen = new Set<string>(), items: DayItem[] = [];
  for (const it of reply.items ?? []) if (sound(it) && !seen.has(it.id) && (it.kind === 'stay' || it.kind === 'move' || it.kind === 'ride' || NESTED.has(it.kind))) { seen.add(it.id); items.push(it); }
  items.sort(byTime);
  const stays = items.filter(it => it.kind === 'stay'), inside = new Map<string, Block[]>(stays.map(s => [s.id, []]));
  const tops: { rank: number; block: Block }[] = [];
  for (const it of items) {
    const row = it.kind === 'stay' ? null : rowOf(it, c);
    if (!row) continue;
    const block: Block = { type: 'row', t: it.start_ms, row };
    // What began while he was at a place is that place's.
    const home = NESTED.has(it.kind) ? [...stays].reverse().find(s => s.start_ms <= it.start_ms && (s.end_ms == null || it.start_ms < s.end_ms)) : undefined;
    if (home) inside.get(home.id)!.push(block); else tops.push({ rank: it.kind === 'sleep' ? 0 : row.connector ? 2 : 3, block });
  }
  for (const s of stays) tops.push({ rank: 1, block: { type: 'stay', t: s.start_ms, key: s.id, head: stayHead(s, c), items: inside.get(s.id)! } });
  tops.sort((a, b) => a.block.t - b.block.t || a.rank - b.rank);
  const blocks = tops.map(x => x.block);
  if (today) {
    const holder = stays.find(s => s.start_ms <= now && (s.end_ms == null || now < s.end_ms)), list = holder ? inside.get(holder.id)! : blocks, at = list.findIndex(b => b.t > now);
    list.splice(at < 0 ? list.length : at, 0, { type: 'now', t: now, key: 'now', label: t([`Now ${clock(now, tz)}`, `现在 ${clock(now, tz)}`]) });
  }
  const chips = (reply.labels ?? []).map(l => l.private ? t(['Private', '私事']) : clean(l.title)).filter(Boolean).map(text => ({ text, more: false }));
  if (chips.length > 2) chips.splice(2, chips.length, { text: `+${chips.length - 2}`, more: true });
  const down = (reply.missing ?? []).map(k => t(SOURCES[k] ?? [k, k]));
  const foot = down.length ? t([`${down.join(', ')} ${down.length === 1 ? "hasn't" : "haven't"} come in yet`, `${down.join('、')}还没进来`]) : '';
  const line = clean(reply.line?.text) ? { text: clean(reply.line!.text), draft: !!reply.line!.draft } : null;
  const ok = Number.isFinite(reply.start_ms) && Number.isFinite(reply.end_ms) && reply.end_ms > reply.start_ms;
  return { title: dateTitle(reply.date, t), today, chips, line, blocks, bar: items.length && ok ? barOf(items, c) : null, foot, empty: !items.length && !line && !chips.length };
}

// ---------- the overview bar ----------
// One bar for the day, from the first thing or 06:00, whichever is earlier, to midnight. Where things overlap, the one that wins is
// painted (a way he went over a sleep over a stay), so every piece is one thing; the track stays empty where nothing was recorded.
function barOf(items: DayItem[], c: Ctx): Bar {
  const { reply, now, today, t } = c, delta = (reply.end_ms - reply.start_ms) - 24 * 3_600_000, at = (h: number) => h === 24 ? reply.end_ms : reply.start_ms + h * 3_600_000 + delta;
  const a0 = Math.max(reply.start_ms, Math.min(items[0].start_ms, at(6))), a1 = reply.end_ms, f = (ms: number) => (ms - a0) / (a1 - a0);
  const covers: { it: DayItem; s: number; e: number; pr: number }[] = [];
  for (const it of items) {
    if (!BAR_KINDS.has(it.kind)) continue;
    const s = Math.max(it.start_ms, a0), e = Math.min(it.end_ms ?? (today ? now : a1), a1);
    if (e > s) covers.push({ it, s, e, pr: it.kind === 'stay' ? 1 : it.kind === 'sleep' ? 2 : 3 });
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
    const base = { key: `${it.id}#${i}`, row: it.id, f0: f(s), f1: f(e) };
    if (it.kind === 'stay') { const look = stayLook(it, t); return { ...base, kind: 'stay', rgb: look.rgb, a: look.kept ? .9 : .32, icon: look.icon, label: look.unnamed ? '' : look.name, name: look.name }; }
    if (it.kind === 'sleep') return { ...base, kind: 'sleep', rgb: SLEEP, a: .9, icon: 'moon', label: t(['Sleep', '睡觉']), name: t(['Sleep', '睡觉']) };
    const words = it.kind === 'ride' ? rideWords(it, t) : moveWords(it.activity, t);
    return { ...base, kind: 'move', rgb: TRANSIT, a: .5, icon: it.kind === 'ride' ? 'bus' : moveIcon(it.activity), label: it.kind === 'ride' ? words : '', name: words };
  });
  const ticks = [6, 12, 18, 24].map(h => ({ label: String(h).padStart(2, '0'), f: f(at(h)) })).filter(x => x.f >= 0);
  return { segs, ticks, nowF: today ? Math.min(1, Math.max(0, f(now))) : null };
}

// Which pieces of the bar carry a name or an icon, by their width in px: a label when it fits, else the icon. A piece too narrow for
// either (under 8 px), or whose icon would crowd the one before it, is not drawn as a sliver: it is counted in a "+N" chip.
export const ICON_PX = 12, MIN_PX = 8, NOTE_PAD = 4;
export function annotate(segs: BarSeg[], width: number, measure: (text: string) => number) {
  const drawn: { seg: BarSeg; x: number; w: number; label: boolean }[] = [], merged: BarSeg[] = [];
  let edge = -Infinity;
  for (const seg of segs) {
    const x = seg.f0 * width, w = (seg.f1 - seg.f0) * width;
    if (w < MIN_PX) { merged.push(seg); continue; }
    if (seg.label && w >= ICON_PX + 3 + Math.ceil(measure(seg.label)) + 2 * NOTE_PAD) { drawn.push({ seg, x, w, label: true }); edge = x + w; continue; }
    if (x + w / 2 - ICON_PX / 2 < edge) { merged.push(seg); continue; }
    drawn.push({ seg, x, w, label: false }); edge = x + w / 2 + ICON_PX / 2;
  }
  return { drawn, merged };
}

import { counted, hm, numbers, type Counted, type Num } from './barView.ts';
import { ATTACHMENT, bars, fromTo, night, type Bar, type Night } from './dayOpen.ts';
import { clean, clock, covered, dur, moneyText, moveWords, rideWords, stageMinutes, stageSlices, vitalsOf, workoutOf, type DayModel, type Icon, type LensKind, type Say } from './dayView.ts';

// What a number of the day has behind it (ADR 0296): the card that hangs under the numbers when one is pinned. Pure (no DOM), so
// scripts/verify-notch-timeline.mjs runs it in node and the phone can read the same lines. Everything is from the day reply: a line whose
// inputs did not arrive is left out, never written as 0 or a dash, and a number with nothing behind it has no card.
const MIN = 60_000;
export type Cell = { label: string; value: string };
// One thing in a list: its glyph, what it is, and what is known of it, each fact on its own.
export type Entry = { icon: Icon | null; lead: string; facts: string[] };
export type Pic = { id: string; note: string; at: string };
export type CardPart =
  | { type: 'cells'; cells: Cell[] }
  | { type: 'night'; night: Night }
  | { type: 'bars'; groups: { heading: string; bars: Bar[] }[] }
  | { type: 'entries'; entries: Entry[]; more: number }
  | { type: 'pictures'; list: Pic[]; more: number };
// `value` is the number's own (the chip says it too), or the Mac's, which says more.
export type Card = { kind: LensKind; label: string; value: string; parts: CardPart[] };

const cell = (label: string, value: string): Cell | false => value !== '' && { label, value };
const cells = (...list: (Cell | false)[]): CardPart => ({ type: 'cells', cells: list.filter((x): x is Cell => !!x) });
const times = (n: number, t: Say) => t([`${n}×`, `${n} 次`]);
// The most that is listed before `+N` says there is more.
const LIST = 4;
const listed = (all: Entry[]): CardPart => ({ type: 'entries', entries: all.slice(0, LIST), more: Math.max(0, all.length - LIST) });
type Ctx = { m: DayModel; t: Say; nums: Num[]; num: Num };
const vital = (c: Ctx, key: string) => vitalsOf(c.m.reply.summary, c.t).find(v => v.key === key)?.value ?? '';

// ---------- sleep ----------
const ASLEEP = new Set(['core', 'deep', 'rem']);
// The night is the one with the most sleep in it (as the bar's wake time is). In bed: the first in_bed or awake stage before he fell asleep, which is
// what the time it took is measured from. A night no stages came for is its start and its end.
function sleepCard(c: Ctx, list: Counted[]): CardPart[] {
  const { m, t } = c, it = list.reduce((best, x) => (x.it.minutes ?? 0) >= (best.it.minutes ?? 0) ? x : best).it, end = it.end_ms, stages = it.stages ?? [];
  const slices = stages.length ? stageSlices(it, it.start_ms, end ?? m.now) : [], asleep = slices.filter(x => ASLEEP.has(x.stage));
  const fell = stages.length ? asleep[0]?.s : it.start_ms;
  const before = fell === undefined ? [] : stages.filter(x => (x.stage === 'in_bed' || x.stage === 'awake') && x.start_ms < fell).map(x => x.start_ms), bed = before.length ? Math.min(...before) : undefined;
  const toSleep = fell !== undefined && bed !== undefined ? Math.round((fell - bed) / MIN) : 0;
  const inBed = covered(stages.filter(x => x.stage === 'in_bed'), 0), slept = asleep.reduce((n, x) => n + x.e - x.s, 0) / MIN, share = inBed > 0 && slept > 0 && slept <= inBed ? Math.round(slept / inBed * 100) : 0;
  const mins: Record<string, number> = slices.length ? stageMinutes({ ...it, end_ms: end ?? m.now }) : {}, woken = slices.filter((x, i) => x.stage === 'awake' && slices.slice(0, i).some(y => ASLEEP.has(y.stage)) && slices.slice(i + 1).some(y => ASLEEP.has(y.stage))).length;
  const stage = (word: string, min?: number) => cell(word, min !== undefined && min >= 1 ? hm(min) : '');
  const n = night(it, m, t);
  return [
    cells(cell(t(['In bed', '上床']), bed !== undefined ? clock(bed, m.tz) : ''), cell(t(['Asleep', '入睡']), fell !== undefined ? clock(fell, m.tz) : ''), cell(t(['Woke up', '醒来']), end != null ? clock(end, m.tz) : ''),
      cell(t(['To fall asleep', '入睡用时']), toSleep > 0 ? dur(toSleep, t) : ''), cell(t(['Sleep efficiency', '睡眠效率']), share > 0 ? `${share}%` : '')),
    cells(stage(t(['Deep', '深睡']), mins.deep), stage(t(['Core', '核心']), mins.core), stage('REM', mins.rem), cell(t(['Wake-ups', '夜醒']), woken > 0 ? times(woken, t) : '')),
    ...(n ? [{ type: 'night', night: n } as CardPart] : []),
    cells(cell(t(["Day's resting HR", '当天静息心率']), vital(c, 'resting')), cell(t(["Day's HRV", '当天 HRV']), vital(c, 'hrv'))),
  ];
}

// ---------- on the way ----------
function tripCard(c: Ctx, list: Counted[]): CardPart[] {
  const { m, t } = c, modes = new Map<string, number>();
  for (const x of list) { const word = x.it.kind === 'ride' ? t(['Bus', '公交']) : moveWords(x.it.activity, t); modes.set(word, (modes.get(word) ?? 0) + x.lap); }
  const longest = list.reduce((best, x) => x.lap > best.lap ? x : best), way = fromTo(m, longest.it, t);
  const what = [longest.it.kind === 'ride' ? rideWords(longest.it, t) : moveWords(longest.it.activity, t), dur(longest.lap / MIN, t), way].filter(Boolean).join(' · ');
  return [
    cells(...[...modes].sort((a, b) => b[1] - a[1]).map(([word, ms]) => cell(word, Math.round(ms / MIN) >= 1 ? dur(ms / MIN, t) : ''))),
    cells(cell(t(['In all', '共']), t([`${list.length} ${list.length === 1 ? 'trip' : 'trips'}`, `${list.length} 段`])), cell(t(['Longest', '最长']), what), cell(t(['Steps', '步数']), vital(c, 'steps'))),
  ];
}

// ---------- workouts ----------
function workoutCard(c: Ctx, list: Counted[]): CardPart[] {
  const { m, t } = c;
  const all = list.map((x): Entry => {
    const w = workoutOf(x.it, t);
    return { icon: w.icon, lead: w.word, facts: [clock(x.it.start_ms, m.tz), dur(x.lap / MIN, t), w.dist, w.pace, w.kcal, w.hr, w.facts.length ? '' : w.note].filter(Boolean) };
  });
  // The steps belong to the way he went; a day with none of that says them here.
  return [listed(all), cells(...(c.nums.some(n => n.kind === 'trip') ? [] : [cell(t(['Steps', '步数']), vital(c, 'steps'))]))];
}

// ---------- the Mac ----------
function workCard(c: Ctx, list: Counted[]): { value: string; parts: CardPart[] } {
  const { m, t } = c, total = list.reduce((n, x) => n + x.lap / MIN, 0), active = list.reduce((n, x) => n + (x.it.active_s ?? 0), 0);
  const sums = (key: 'projects' | 'apps') => { const by = new Map<string, number>(); for (const { it } of list) for (const x of it[key] ?? []) if (clean(x.name) && x.seconds > 0) by.set(clean(x.name), (by.get(clean(x.name)) ?? 0) + x.seconds); return [...by].map(([name, seconds]) => ({ name, seconds })); };
  const groups = [{ heading: t(['Projects', '项目']), bars: bars(sums('projects'), active, t) }, { heading: t(['Apps', '应用']), bars: bars(sums('apps'), active, t) }].filter(g => g.bars.length);
  // Claude Code sessions are not in the Mac's number; they are said here: how many, and in which projects.
  const code = m.items.filter(x => x.kind === 'code'), by = new Map<string, number>();
  for (const x of code) if (clean(x.project)) by.set(clean(x.project), (by.get(clean(x.project)) ?? 0) + 1);
  const projects = [...by].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1));
  const session: Entry[] = code.length ? [{ icon: 'code', lead: 'Claude Code', facts: [t([`${code.length} ${code.length === 1 ? 'session' : 'sessions'}`, `${code.length} 个会话`]),
    ...projects.slice(0, LIST).map(([name, n]) => n > 1 ? `${name} ×${n}` : name), projects.length > LIST ? `+${projects.length - LIST}` : ''].filter(Boolean) }] : [];
  const span = `${clock(Math.min(...list.map(x => x.s)), m.tz)}–${clock(Math.max(...list.map(x => x.e)), m.tz)}`;
  return { value: [span, t([`active ${c.num.long}`, `活跃 ${c.num.long}`]), t([`${dur(total, t)} in all`, `共 ${dur(total, t)}`])].join(' · '),
    parts: [...(groups.length ? [{ type: 'bars', groups } as CardPart] : []), ...(session.length ? [{ type: 'entries', entries: session, more: 0 } as CardPart] : [])] };
}

// ---------- calls ----------
function callCard(c: Ctx, list: Counted[]): CardPart[] {
  const { m, t } = c, apps = new Map<string, { n: number; ms: number }>(), longest = list.reduce((best, x) => x.lap > best.lap ? x : best);
  for (const x of list) { const app = clean(x.it.app) || t(['Other', '其他']), o = apps.get(app) ?? { n: 0, ms: 0 }; apps.set(app, { n: o.n + 1, ms: o.ms + x.lap }); }
  return [
    cells(cell(t(['Calls', '通话']), times(list.length, t)), cell(t(['Longest', '最长']), `${dur(longest.lap / MIN, t)} · ${clock(longest.it.start_ms, m.tz)}`)),
    listed([...apps].sort((a, b) => b[1].ms - a[1].ms).map(([app, o]): Entry => ({ icon: 'call', lead: app, facts: [times(o.n, t), dur(o.ms / MIN, t)] }))),
  ];
}

// ---------- talks with her ----------
// A talk's topic is the words it was marked with (his, else hers); a talk nobody marked has none.
function talkCard(c: Ctx, list: Counted[]): CardPart[] {
  const { m, t } = c, turns = list.reduce((n, x) => n + (x.it.turns ?? 0), 0);
  const topics = list.filter(x => m.stars.get(x.it.id)?.label).map((x): Entry => ({ icon: 'talk', lead: m.stars.get(x.it.id)!.label, facts: [clock(x.it.start_ms, m.tz), x.it.turns ? t([`${x.it.turns} ${x.it.turns === 1 ? 'line' : 'lines'}`, `${x.it.turns} 句`]) : ''].filter(Boolean) }));
  return [
    cells(cell(t(['Lines', '共']), turns > 0 ? t([`${turns}`, `${turns} 句`]) : ''), cell(t(['Between', '时段']), `${clock(Math.min(...list.map(x => x.s)), m.tz)}–${clock(Math.max(...list.map(x => x.e)), m.tz)}`)),
    ...(topics.length ? [listed(topics)] : []),
  ];
}

// ---------- money ----------
function paymentCard(c: Ctx, list: Counted[]): CardPart[] {
  const { t } = c, out = new Map<string, { n: number; sum: number }>(), into = new Map<string, { n: number; sum: number }>(), who = new Map<string, { name: string; cur: string; sum: number }>();
  for (const { it } of list) {
    const amount = Number.parseFloat(String(it.amount)), cur = clean(it.currency).toUpperCase();
    if (!Number.isFinite(amount) || it.direction !== 'out' && it.direction !== 'in') continue;
    const side = it.direction === 'out' ? out : into, o = side.get(cur) ?? { n: 0, sum: 0 };
    side.set(cur, { n: o.n + 1, sum: o.sum + amount });
    const name = clean(it.counterparty) || clean(it.text);
    if (it.direction === 'out' && name) { const k = `${name}\n${cur}`, w = who.get(k) ?? { name, cur, sum: 0 }; who.set(k, { ...w, sum: w.sum + amount }); }
  }
  const said = (side: Map<string, { n: number; sum: number }>) => [...side].sort((a, b) => b[1].n - a[1].n || b[1].sum - a[1].sum).map(([cur, o]) => moneyText(o.sum.toFixed(2), cur)).join(' · ');
  return [
    cells(cell(t(['Spent', '支出']), said(out)), cell(t(['Received', '收入']), said(into)), cell(t(['Payments', '笔数']), t([`${list.length}`, `${list.length} 笔`]))),
    listed([...who.values()].sort((a, b) => b.sum - a.sum).slice(0, 3).map((x): Entry => ({ icon: 'payment', lead: x.name, facts: [moneyText(x.sum.toFixed(2), x.cur)] }))),
  ];
}

// ---------- photos ----------
const SIX = 6;
function pictureCard(c: Ctx, list: Counted[]): CardPart[] {
  const have = list.filter(x => ATTACHMENT.test(clean(x.it.attachment)));
  return have.length ? [{ type: 'pictures', list: have.slice(0, SIX).map(x => ({ id: clean(x.it.attachment), note: clean(x.it.note), at: clock(x.it.start_ms, c.m.tz) })), more: Math.max(0, have.length - SIX) }] : [];
}

// The card for one number of the day; null when the day has no such number. Parts that came out empty are not there.
export function cardOf(m: DayModel, kind: LensKind, t: Say, nums: Num[] = numbers(m, t)): Card | null {
  const num = nums.find(n => n.kind === kind), list = counted(m)[kind];
  if (!num || !list.length) return null;
  const c: Ctx = { m, t, nums, num };
  let value = num.long, parts: CardPart[] = [];
  switch (kind) {
    case 'sleep': parts = sleepCard(c, list); break;
    case 'trip': parts = tripCard(c, list); break;
    case 'workout': parts = workoutCard(c, list); break;
    case 'work': ({ value, parts } = workCard(c, list)); break;
    case 'call': parts = callCard(c, list); break;
    case 'talk': parts = talkCard(c, list); break;
    case 'payment': parts = paymentCard(c, list); break;
    case 'picture': parts = pictureCard(c, list); break;
  }
  return { kind, label: num.label, value, parts: parts.filter(p => p.type === 'cells' ? p.cells.length > 0 : p.type === 'entries' ? p.entries.length > 0 : true) };
}

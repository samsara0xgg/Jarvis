import type { L } from './companionSettings';
import { hm } from './barView.ts';
import { accountOf, askable, clean, clock, dur, endText, EVENT_SOURCES, moneyText, moveWords, PAYERS, rideWords, stageMinutes, stageSlices, stayLook, workoutOf, type DayItem, type DayModel, type Icon, type Say } from './dayView.ts';

// What a row says when it is opened (ADR 0296, spec-r3 section 8), and what can be done from it. Pure (no DOM), so
// scripts/verify-notch-timeline.mjs runs it in node. Nothing here is made up: a part exists only where the brain sent what it
// shows, and an action exists only where its route works today (the rename of a kept place, the acknowledgement of a reminder that
// rang, a turn that is handed to her). DayTimeline.tsx and RowOpen.tsx draw it.
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
  | { type: 'picture'; id: string; note: string }
  | { type: 'bars'; heading: string; bars: Bar[] }
  | { type: 'night'; night: Night }
  | { type: 'sessions'; list: Session[] }
  | { type: 'titles'; list: string[] };
// `rename`: the kept place under a stay, and the name it has; `ack`: the reminder that rang and waits for a `知道了`; `summarize`: a work block
// can be handed to her at one tap.
export type Open = { key: string; kind: string; parts: Part[]; rename: { id: string; name: string } | null; ack: string | null; summarize: boolean };
// What a turn about a row carries (ADR 0214): exactly these four, the brain refuses a turn whose `about` has more or a bad id.
export type About = { kind: string; id: string; title: string; start_ms: number };
export type Ask = { about: About; icon: Icon; at: string };

const ATTACHMENT = /^[0-9a-f]{32}$/;
const say = (text: string, tone: Tone = 'muted'): Words => ({ text, tone });
const lines = (...list: (Words | false | '' | null | undefined)[]): Part => ({ type: 'lines', lines: list.filter((x): x is Words => !!x) });
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
// The biggest five of what the work block was spent on, each as its share of the active time.
function bars(list: DayItem['projects'], active: number, t: Say): Bar[] {
  const top = (list ?? []).filter(x => clean(x.name) && x.seconds > 0).sort((a, b) => b.seconds - a.seconds).slice(0, 5), base = active > 0 ? active : Math.max(...top.map(x => x.seconds), 1);
  return top.map(x => ({ name: clean(x.name), text: dur(Math.max(1, x.seconds / 60), t), share: Math.max(0, Math.min(1, x.seconds / base)) }));
}
function night(it: DayItem, m: DayModel, t: Say): Night | null {
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
  let rename: Open['rename'] = null, ack: string | null = null, summarize = false;
  switch (sessions ? 'fold' : it.kind) {
    case 'fold': parts.push({ type: 'sessions', list: [...sessions!].sort((a, b) => a.start_ms - b.start_ms).map(x => session(x, m, t)) }); break;
    case 'code': parts.push({ type: 'sessions', list: [session(it, m, t)] }); break;
    case 'stay': {
      const look = stayLook(it, t), place = clean(it.place);
      parts.push(lines(place && say(place, 'ink'), look.kept && say(t([`A place you keep · ${look.name}`, `常去的地方 · ${look.name}`]))));
      if (it.kept_place && clean(it.kept_place.name)) rename = { id: it.kept_place.id, name: clean(it.kept_place.name) };
      break;
    }
    case 'move': {
      const [from, to] = around(m, it, t);
      parts.push(lines(say([moveWords(it.activity, t), dur(Math.max(1, lengthOf(it, m)), t)].join(' · '), 'ink'),
        (from || to) && say(from && to ? `${from} → ${to}` : from ? t([`From ${from}`, `从 ${from} 出发`]) : t([`To ${to}`, `去 ${to}`]))));
      break;
    }
    case 'ride': {
      const credit = clean(it.credit), ended = it.end_ms == null ? it.state === 'next' ? '' : t(['Still on it', '还在车上']) : ENDED[clean(it.ended_reason)] ? t(ENDED[clean(it.ended_reason)]) : '';
      parts.push(lines(say([clean(it.board_stop), clean(it.alight_stop)].filter(Boolean).join(' → '), 'ink'), credit && say(t([`Live: ${credit}`, `实时：${credit}`])), ended && say(ended)));
      break;
    }
    case 'talk':
      parts.push({ type: 'talk', day: m.reply.date, from: it.start_ms, to: it.end_ms ?? it.start_ms });
      if (it.turns) parts.push(lines(say(t([`${it.turns} ${it.turns === 1 ? 'line' : 'lines'} in all`, `共 ${it.turns} 句`]))));
      break;
    case 'picture': parts.push({ type: 'picture', id: ATTACHMENT.test(clean(it.attachment)) ? clean(it.attachment) : '', note: clean(it.note) }); break;
    case 'event': {
      const source = it.source === undefined ? t(['Calendar', '日历']) : EVENT_SOURCES[it.source] ? t(EVENT_SOURCES[it.source]) : '';
      const unsure = it.source === 'mail' && it.confirmed === false, sure = it.source === 'mail' && it.confirmed === true;
      const state = it.state === 'now' ? t(['Happening now', '正在进行']) : it.state === 'next' ? t(['Not yet', '还没开始']) : t(['Over', '已结束']);
      parts.push(lines(say(span(it, m, t), 'ink'), !it.private && clean(it.place) && say(clean(it.place)),
        say([source && t([`Source: ${source}`, `来源：${source}`]), unsure ? t(['not confirmed yet', '还没确认']) : sure ? t(['confirmed', '已确认']) : '', state].filter(Boolean).join(' · '))));
      break;
    }
    case 'reminder': {
      const state = it.acknowledged ? t(['You know about it', '你知道了']) : it.fired ? t(['It rang', '响过了']) : it.start_ms > m.now ? t(['Not due yet', '还没到点']) : '';
      parts.push(lines(clean(it.text) && say(clean(it.text), 'ink'), say([`${t(['Due', '提醒时间'])} ${clock(it.start_ms, m.tz)}`, state].filter(Boolean).join(' · '))));
      if (it.fired === true && it.acknowledged === false) ack = it.id;
      break;
    }
    case 'departure': {
      const stop = clean(it.board_stop), leave = it.leave_at_ms != null ? clock(it.leave_at_ms, m.tz) : '', go = clock(it.start_ms, m.tz);
      parts.push(lines(say([rideWords(it, t), stop].filter(Boolean).join(' · '), 'ink'), clean(it.to) && say(t([`To ${it.to}`, `去 ${it.to}`])),
        say(leave ? t([`Leave ${leave} · Departs ${go}`, `${leave} 出门 · ${go} 发车`]) : t([`Departs ${go}`, `${go} 发车`]))));
      break;
    }
    case 'work': {
      const active = it.active_s ? dur(it.active_s / 60, t) : '', total = dur(Math.max(1, lengthOf(it, m)), t);
      parts.push(lines(say(active ? t([`Active ${active} · ${total} in all`, `活跃 ${active} · 共 ${total}`]) : t([`${total} in all`, `共 ${total}`]), 'ink')));
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
      else parts.push(lines(say(span(it, m, t), 'ink')));
      break;
    }
    case 'workout': {
      const w = workoutOf(it, t);
      parts.push(lines(say([w.word, dur(Math.max(1, lengthOf(it, m)), t)].join(' · '), 'ink'), (w.facts.length > 0 || w.note) && say(w.facts.join(' · ') || w.note)));
      break;
    }
    case 'call': parts.push(lines(say(span(it, m, t), 'ink'), clean(it.app) && say(clean(it.app)))); break;
    case 'payment': {
      const way = it.direction === 'in' ? t(['Received', '收入']) : it.direction === 'out' ? t(['Spent', '支出']) : '', via = PAYERS[clean(it.source)], who = clean(it.counterparty), what = clean(it.text), cur = clean(it.currency).toUpperCase();
      parts.push(lines(say([way, moneyText(it.amount, it.currency), cur].filter(Boolean).join(' · '), 'ink'), (who || what) && say([who, what && what !== who ? what : ''].filter(Boolean).join(' · ')), via && say(t(via))));
      break;
    }
    case 'claude_chat':
      parts.push(lines(clean(it.text) && say(clean(it.text), 'ink'), say([t(['With Claude', '和 Claude']), it.messages ? t([`${it.messages} msgs`, `${it.messages} 条`]) : '', span(it, m, t)].filter(Boolean).join(' · '))));
      break;
    case 'claude_day': {
      const n = it.chats ?? 0, titles = clean(it.text).split(/\s*;\s*/).filter(Boolean);
      if (titles.length) parts.push({ type: 'titles', list: titles });
      if (n) parts.push(lines(say(t([`${n} ${n === 1 ? 'chat' : 'chats'}`, `${n} 个对话`]))));
      break;
    }
    default: return null;
  }
  return { key, kind: sessions ? 'code' : it.kind, parts: parts.filter(p => p.type !== 'lines' || p.lines.length > 0), rename, ack, summarize };
}

// What a turn about this row carries and what the input row shows of it: null when the brain would refuse it (a kind it does not take, an id
// of the wrong shape, a folded row that is no one item).
export function askOf(m: DayModel, key: string, t: Say): Ask | null {
  const it = m.items.find(x => x.id === key);
  if (!it || !askable(it.kind, it.id)) return null;
  const head = m.heads.get(key), row = m.rows.get(key), title = (head?.name ?? row?.title ?? '').trim();
  return { about: { kind: it.kind, id: it.id, title: title || t(['An item of the day', '这一条']), start_ms: Math.round(it.start_ms) }, icon: row?.icon && row.icon !== 'dot' ? row.icon : head?.icon ?? 'pin', at: clock(it.start_ms, m.tz) };
}

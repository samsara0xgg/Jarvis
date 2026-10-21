import { useEffect, useMemo, useRef, useState } from 'react';
import { CalendarBlank, CheckCircle, Circle, WarningCircle } from '@phosphor-icons/react';
import { clock, dur } from '../dayView';
import { postRoute, useRoute, type Today, type TodayEvent, type Todo } from '../homeData';
import type { BarMark, DoorChip, DoorCtx, DoorPage, DoorRow } from '../doors';

// 待办: today's calendar and his open to-dos (the Dashboard's Today block reads the same route), as a page under the bar. Events and to-dos that have a time are rows in time order, each a point
// on the bar (a to-do amber once it is past due and still open, an event dim); the ones with no time (all-day events, to-dos with a date only or none) come after a heading. The route has no other
// day, so another day only says so. The quick button ticks a to-do (as the Dashboard does: at once, and put back if To Do cannot be reached); a ticked one stays faded, with the way back, until the next
// read leaves it out.
const NOTICE_MS = 4000, TWO_HOURS = 2 * 3_600_000, END_OF_DAY = '23:59';
type Ev = { e: TodayEvent; from: number; to: number | null };
type Td = { x: Todo; due: number | null; timed: boolean; late: boolean; done: boolean };

const eventsOf = (data: Today | null): TodayEvent[] => Array.isArray(data?.events) ? data.events.filter(e => !!e && typeof e.id === 'string' && typeof e.title === 'string') : [];
const todosOf = (data: Today | null): Todo[] => Array.isArray(data?.todos) ? data.todos.filter(x => !!x && typeof x.id === 'string' && typeof x.title === 'string') : [];
const dateOf = (ms: number, tz: string) => { try { return new Intl.DateTimeFormat('en-US', { timeZone: tz || undefined, month: 'numeric', day: 'numeric' }).format(ms); } catch { return ''; } };

export function useTodoDoor(ctx: DoorCtx): DoorPage | null {
  const { port, open, t, lang, day } = ctx;
  const route = useRoute<Today>(port, '/inherent/today', open, 5 * 60_000);
  const [ticked, setTicked] = useState<ReadonlySet<string>>(() => new Set()), [notice, setNotice] = useState<string | null>(null), timer = useRef(0);
  useEffect(() => () => clearTimeout(timer.current), []);
  // A read that no longer lists a to-do has dropped it: its tick is done with.
  useEffect(() => { const ids = new Set(todosOf(route.data).map(x => x.id)); setTicked(s => s.size && [...s].some(id => !ids.has(id)) ? new Set([...s].filter(id => ids.has(id))) : s); }, [route.data]);
  const tick = async (id: string, done: boolean) => {
    const mark = (on: boolean) => setTicked(s => { const next = new Set(s); if (on) next.add(id); else next.delete(id); return next; });
    mark(done);
    if (!port) return;
    try { await postRoute(port, '/inherent/today/todo', { id, done }); }
    catch {
      mark(!done);
      setNotice(t(["Couldn't reach To Do.", '连不上 To Do。']));
      clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setNotice(null), NOTICE_MS);
    }
  };
  return useMemo((): DoorPage => {
    const todos = todosOf(route.data), events = eventsOf(route.data);
    const count = (n: number) => n ? String(n) : null;
    const inDay = (ms: number) => ms >= day.start && ms < day.end;
    const loading = !!port && !route.data && !route.failed && !route.missing;
    // Another day: the route knows only today.
    if (!day.today) return { count: count(todos.filter(x => !ticked.has(x.id)).length), line: t(['To-dos are for today only.', '待办只看今天的。']), chips: [], marks: [], rows: [], nowAt: null, empty: t(['Go back to today to see the list', '回到今天，看今天的清单']) };

    const evs: Ev[] = events.map(e => ({ e, from: Date.parse(e.start), to: e.end ? Date.parse(e.end) : NaN })).filter(x => Number.isFinite(x.from)).map(x => ({ ...x, to: Number.isFinite(x.to) ? x.to : null }));
    // A to-do has a time when its due has an hour of its own: To Do keeps a date-only due as the end of that day (23:59).
    const tds: Td[] = todos.map(x => {
      const due = x.due ? Date.parse(x.due) : NaN, has = Number.isFinite(due), done = ticked.has(x.id);
      return { x, due: has ? due : null, timed: has && inDay(due) && clock(due, day.tz) !== END_OF_DAY, late: has && due < day.now && !done, done };
    });
    const allDay = (v: Ev) => v.e.all_day === true, timedEvs = evs.filter(v => !allDay(v)), key = { ev: (v: Ev) => `ev:${v.e.id}`, td: (d: Td) => `td:${d.x.id}` };
    const btn = (label: string, onClick: () => void) => <button type="button" className="dt-ask" tabIndex={-1} onClick={onClick}>{label}</button>;

    const evRow = (v: Ev): DoorRow => {
      const all = allDay(v), future = !all && v.from > day.now, over = !all && (v.to ?? v.from) <= day.now;
      // What is to come within two hours says how long until it, as the day's list words it.
      const left = future && v.from - day.now <= TWO_HOURS ? dur(Math.ceil((v.from - day.now) / 60_000), t) : '';
      return { key: key.ev(v), at: all ? null : v.from, icon: <CalendarBlank size={13} aria-hidden="true"/>, title: v.e.title,
        sub: all ? t(['All day', '全天']) : v.to === null ? clock(v.from, day.tz) : `${clock(v.from, day.tz)}–${clock(v.to, day.tz)}`,
        side: left ? t([`in ${left}`, `还有 ${left}`]) : undefined, tone: future ? 'future' : over ? 'read' : undefined };
    };
    const tdRow = (d: Td): DoorRow => ({
      key: key.td(d), at: d.timed ? d.due : null, time: d.timed || d.due === null ? undefined : inDay(d.due) ? t(['Today', '今天']) : dateOf(d.due, day.tz),
      icon: d.done ? <CheckCircle size={14} weight="fill" aria-hidden="true"/> : <Circle size={14} aria-hidden="true"/>, title: d.x.title, tag: d.late ? { text: t(['Overdue', '过期']), amber: true } : null,
      sub: t(['To-do', '待办']), quick: d.done ? btn(t(['Undo', '撤销']), () => void tick(d.x.id, false)) : btn(t(['Done', '完成']), () => void tick(d.x.id, true)), tone: d.done ? 'read' : undefined,
    });
    const byTime = (a: DoorRow, b: DoorRow) => a.at! - b.at! || (a.key < b.key ? -1 : 1), byDue = (a: Td, b: Td) => (a.due ?? Infinity) - (b.due ?? Infinity) || (a.x.id < b.x.id ? -1 : 1);
    const timed = [...timedEvs.map(evRow), ...tds.filter(d => d.timed).map(tdRow)].sort(byTime);
    const rest = [...evs.filter(allDay).map(evRow), ...tds.filter(d => !d.timed).sort(byDue).map(tdRow)];

    // A point at each to-do's due time (amber when it is past due and open) and a dim one at each event's start; what has no time has no mark.
    const marks: BarMark[] = [
      ...tds.filter(d => d.timed && !d.done).map((d): BarMark => ({ kind: 'point', at: d.due!, tone: d.late ? 'amber' : 'on', key: key.td(d) })),
      ...timedEvs.filter(v => inDay(v.from)).map((v): BarMark => ({ kind: 'point', at: v.from, tone: 'dim', key: key.ev(v) })),
    ];
    const late = tds.filter(d => d.late).sort(byDue), openTds = tds.filter(d => !d.done);
    const chips: DoorChip[] = [
      { id: 'todo', icon: <CheckCircle size={12} aria-hidden="true"/>, label: t(['To-dos', '待办']), value: String(openTds.length) },
      { id: 'event', icon: <CalendarBlank size={12} aria-hidden="true"/>, label: t(['Events', '日程']), value: String(evs.length) },
    ];
    if (late.length) chips.push({ id: 'late', icon: <WarningCircle size={12} aria-hidden="true"/>, label: t(['Overdue', '过期']), value: String(late.length), amber: true, only: new Set(late.map(key.td)) });

    // The line, written from the facts: the first to-do past its time; else how many things are left today and the first two; else that all is done.
    const ahead = [...[...timedEvs.filter(v => (v.to ?? v.from) > day.now).map(v => ({ at: v.from, title: v.e.title })), ...tds.filter(d => d.timed && !d.done).map(d => ({ at: d.due!, title: d.x.title }))].sort((a, b) => a.at - b.at).map(x => x.title),
      ...tds.filter(d => !d.timed && !d.done).sort(byDue).map(d => d.x.title)];
    const n = ahead.length, [a, b] = ahead;
    const line = late.length ? late.length === 1 ? t([`${late[0].x.title} is overdue.`, `${late[0].x.title} 过了时间还没做。`]) : t([`${late[0].x.title} and ${late.length - 1} more are overdue.`, `${late[0].x.title} 等 ${late.length} 件过了时间还没做。`])
      : n ? t([`${n} left today: ${n === 1 ? a : `${a}, ${b}${n > 2 ? ` and ${n - 2} more` : ''}`}.`, `今天还有 ${n} 件：${n === 1 ? a : `${a}、${b}${n > 2 ? ' 等' : ''}`}。`])
      : t(['Everything for today is done.', '今天的都做完了。']);

    return {
      count: count(openTds.length), line: notice ?? (route.data ? line : null), chips, marks, nowAt: day.now, rows: [...timed, ...rest], untimed: rest.length ? t(['No set time', '没定时间']) : null,
      empty: !port ? t(['Not connected to Jarvis yet', '还没连上 Jarvis']) : route.missing ? t(["This Jarvis can't give to-dos yet", '这台 Jarvis 还不会给待办']) : t(['Nothing on the list today', '今天没有事']),
      loading, failed: !route.data && route.failed,
    };
  }, [route.data, route.failed, route.missing, ticked, notice, port, day.start, day.end, day.today, day.now, day.tz, lang]);
}

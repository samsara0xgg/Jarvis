import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowBendUpLeft, EnvelopeSimple, Megaphone } from '@phosphor-icons/react';
import { useRoute, type Mail } from '../homeData';
import { MailLetter } from '../MailPage';
import { useMailActs } from '../useMailActs';
import { clock } from '../dayView';
import type { BarMark, DoorChip, DoorCtx, DoorPage, DoorRow } from '../doors';
import { DoorHead } from './parts';

// 信: the unread letters (the Dashboard's Mail page reads the same route), as a page of the day under the bar: the letters that came on the shown day, oldest first, each a point on the bar;
// on today also the unread ones from before it, after a heading, with their dates. A letter opens in the pane (the Dashboard's letter, with its acts and its reply draft); a letter he has
// archived, trashed or marked as read leaves the list and its row stays 5 s saying so, with the way back (useMailActs.ts).
const GONE_MS = 5000, NOTICE_MS = 4000;
type Tomb = { m: Mail; text: string; undo: () => void };
type Entry = { m: Mail; ms: number; tomb?: Tomb };

const unreadOf = (data: unknown): Mail[] => { const list = (data as { unread?: unknown } | null)?.unread; return Array.isArray(list) ? list.filter((m): m is Mail => !!m && typeof (m as Mail).id === 'string') : []; };
// The letter in the circle: the sender's first letter or character.
const initial = (name: string) => (Array.from(name.trim())[0] ?? '?').toUpperCase();
// Long names are cut to their first word in a sentence ("Northwind Recruiting" is Northwind); a Chinese sentence is spaced where it meets a Latin word or a number.
const short = (name: string) => { const n = name.replace(/<[^>]*>/g, '').trim(), w = n.split(/\s+/); return n.length > 12 && w.length > 1 ? w[0] : n; };
const spaced = (zh: string) => zh.replace(/([一-鿿])([A-Za-z0-9])/g, '$1 $2').replace(/([A-Za-z0-9])([一-鿿])/g, '$1 $2');
const dateOf = (ms: number, tz: string) => { if (!Number.isFinite(ms)) return ''; try { return new Intl.DateTimeFormat('en-US', { timeZone: tz || undefined, month: 'numeric', day: 'numeric' }).format(ms); } catch { return ''; } };
const rank = (ms: number) => Number.isFinite(ms) ? ms : -Infinity;

export function useMailDoor(ctx: DoorCtx): DoorPage | null {
  const { port, open, t, lang, day } = ctx;
  const route = useRoute<{ unread: Mail[] }>(port, '/inherent/mail', open, 5 * 60_000);
  const all = useMemo(() => unreadOf(route.data), [route.data]);
  const [tombs, setTombs] = useState<Tomb[]>([]), [notice, setNotice] = useState<string | null>(null), timers = useRef<number[]>([]);
  useEffect(() => () => timers.current.forEach(clearTimeout), []);
  const later = (fn: () => void, ms: number) => { timers.current.push(window.setTimeout(fn, ms)); };
  // What came of an act: a letter that went leaves its row for 5 s as a row that says what was done, with 撤销; what did not go through is said in the page's line for 4 s.
  const acts = useMailActs(port, t, route.reload, (text, undo, ids) => {
    const m = undo && ids?.length ? all.find(x => x.id === ids[0]) : undefined;
    if (!m || !undo) { setNotice(text); later(() => setNotice(n => n === text ? null : n), NOTICE_MS); return; }
    const drop = () => setTombs(ts => ts.filter(x => x.m.id !== m.id));
    setTombs(ts => [...ts.filter(x => x.m.id !== m.id), { m, text, undo: () => void Promise.resolve(undo()).then(drop) }]);
    later(drop, GONE_MS);
  });
  return useMemo((): DoorPage => {
    const live = all.filter(m => !acts.gone.includes(m.id));
    const entries: Entry[] = [...live.map(m => ({ m, ms: Date.parse(m.received) })), ...tombs.map(x => ({ m: x.m, ms: Date.parse(x.m.received), tomb: x }))];
    const inDay = (e: Entry) => Number.isFinite(e.ms) && e.ms >= day.start && e.ms < day.end;
    // Today also has what is unread from before it (or from no time that can be read); another day only what came on it.
    const timed = entries.filter(inDay).sort((a, b) => a.ms - b.ms || (a.m.id < b.m.id ? -1 : 1));
    const before = day.today ? entries.filter(e => !inDay(e) && !(Number.isFinite(e.ms) && e.ms >= day.end)).sort((a, b) => rank(b.ms) - rank(a.ms) || (a.m.id < b.m.id ? -1 : 1)) : [];
    const here = [...timed, ...before].filter(e => !e.tomb).map(e => e.m), wants = here.filter(m => m.reply === 'yes'), junk = here.filter(m => m.junk), seen = here.filter(m => !m.junk);
    const ask = (m: Mail) => m.reply === 'yes';

    const rowOf = (e: Entry, untimed: boolean): DoorRow => {
      const { m } = e, name = m.from?.trim() || m.address || '?';
      const base = { key: e.tomb ? `gone:${m.id}` : m.id, at: untimed ? null : e.ms, time: untimed ? dateOf(e.ms, day.tz) : undefined, icon: initial(name), who: true, title: name };
      if (e.tomb) return { ...base, sub: e.tomb.text, side: <button type="button" className="dt-ask" tabIndex={-1} onClick={e.tomb.undo}>{t(['Undo', '撤销'])}</button>, tone: 'read' };
      return {
        ...base, tag: ask(m) ? { text: t(['Reply', '要回']), amber: true } : null, opens: true, tone: m.junk ? 'read' : undefined,
        sub: m.junk ? `${t(['Looks like an ad', '像广告'])} · ${m.subject}` : m.subject,
      };
    };

    // A point on the bar at each letter of the day that is still here: amber if it wants a reply, dim if it looks like an ad.
    const marks: BarMark[] = timed.filter(e => !e.tomb).map(({ m, ms }) => ({ kind: 'point', at: ms, tone: ask(m) ? 'amber' : m.junk ? 'dim' : 'on', key: m.id }));
    const chips: DoorChip[] = [];
    if (wants.length) chips.push({ id: 'reply', icon: <ArrowBendUpLeft size={12} aria-hidden="true"/>, label: t(['Reply', '要回']), value: String(wants.length), amber: true, only: new Set(wants.map(m => m.id)) });
    chips.push({ id: 'unread', icon: <EnvelopeSimple size={12} aria-hidden="true"/>, label: t(['Unread', '没看']), value: String(seen.length) });
    if (junk.length) chips.push({ id: 'ads', icon: <Megaphone size={12} aria-hidden="true"/>, label: t(['Ads', '像广告']), value: String(junk.length), only: new Set(junk.map(m => m.id)) });

    // The line, written from the facts: who wants a reply (two names at most, newest first) and how many more are unread; or how many are unread and none wants a reply; or all read.
    const rest = seen.filter(m => !ask(m)).length, from = [...new Set(wants.sort((a, b) => rank(Date.parse(b.received)) - rank(Date.parse(a.received))).map(m => short(m.from?.trim() || m.address || '?')))];
    let line: string;
    if (!day.today) line = seen.length ? t([`Mail from that day; ${seen.length} still unread.`, spaced(`这天到的信，还有${seen.length}封没看。`)]) : t(['Mail from that day; all read.', '这天到的信，都看过了。']);
    else if (from.length) {
      const [a, b] = from, who = from.length === 1 ? [`${a} wants`, `${a}的信`] : from.length === 2 ? [`${a} and ${b} want`, `${a}和${b}的信`] : [`${a}, ${b} and ${from.length - 2} more want`, `${a}、${b}等${from.length}人的信`];
      line = t([`${who[0]} a reply${rest ? `; ${rest} more unread.` : '.'}`, spaced(`${who[1]}要回${rest ? `，另外${rest}封没看。` : '。'}`)]);
    } else line = seen.length ? t([`${seen.length} unread; none need a reply.`, spaced(`${seen.length}封没看，没有要回的。`)]) : t(['All your mail is read.', '信都看过了。']);

    const loading = !!port && !route.data && !route.failed && !route.missing;
    return {
      count: live.some(m => !m.junk) ? String(live.filter(m => !m.junk).length) : null, countAmber: wants.length > 0,
      // The letter the right column opens on: the newest one that wants a reply (here, on the shown day or from before it), else the newest unread one that is no ad.
      rest: [...wants].sort((a, b) => rank(Date.parse(b.received)) - rank(Date.parse(a.received)))[0]?.id ?? [...seen].sort((a, b) => rank(Date.parse(b.received)) - rank(Date.parse(a.received)))[0]?.id ?? null,
      line: notice ?? (route.data ? line : null), chips, marks, nowAt: day.today ? day.now : null,
      rows: [...timed.map(e => rowOf(e, false)), ...before.map(e => rowOf(e, true))], untimed: before.length ? t(['Earlier, unread', '更早没看的']) : null,
      empty: !port ? t(['Not connected to Jarvis yet', '还没连上 Jarvis']) : route.missing ? t(["This Jarvis can't give mail yet", '这台 Jarvis 还不会给信']) : day.today ? t(['No unread mail', '没有没看的信']) : t(['No unread mail from that day', '这天没有没看的信']),
      loading, failed: !route.data && route.failed,
      detail: key => {
        const m = live.find(x => x.id === key);
        if (!m) return null;
        const name = m.from?.trim() || m.address || '', ms = Date.parse(m.received);
        // The head is the board's (the circle, who, the address, when); the letter under it keeps its subject, its words and its acts (MailPage.tsx), its own who-line is hidden (day-doors.css).
        return { title: name, body: <>
          <DoorHead icon={initial(name)} who title={name} sub={m.address || undefined} time={Number.isFinite(ms) ? ms >= day.start && ms < day.end ? clock(ms, day.tz) : dateOf(ms, day.tz) : undefined} t={t}/>
          <div className="ad dt-ad"><MailLetter key={m.id} port={port} letter={m} onAct={kind => void acts.act(kind, m)}/></div></> };
      },
    };
  }, [all, acts.gone, tombs, notice, route.data, route.failed, route.missing, port, day.start, day.end, day.today, day.now, day.tz, lang]);
}

import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent, type ReactNode } from 'react';
import { CaretLeft, CaretRight, Sparkle, Star } from '@phosphor-icons/react';
import { tr, useCompanionSettings, type L } from './companionSettings';
import { postRoute, useRoute, type Route } from './homeData';
import { askable, buildDay, changeId, clock, dateTitle, keptOf, marksOf, report, shiftDate, starrable, within, type Block, type DayItem, type DayModel, type DayReply, type Highlight, type Row, type WeatherLook } from './dayView';
import { answerOf, askOf, hasBody, headOf, openOf, sayOf, type About, type Ask, type Rec, type Said } from './dayOpen';
import { AskDock, type DockState } from './AskDock';
import { DayBar, useBarState, type PageBar } from './DayBar';
import { DoorDetail, DoorNumbers, DoorRowView, Doors, inRange } from './DayDoors';
import { DOORS, useDoors, type DoorCtx, type DoorKey, type DoorPage, type DoorRow, type ToolRenderer } from './doors';
import { numbers } from './barView';
import { Ic, SKIES } from './dayIcons';
import { DayStage, partsOf } from './DayStage';
import { MOTION } from './motion';
import { DetailPage, EASE, reduced, Unfold, Viewer, type Acts } from './RowOpen';
import { StarToggle, type StarMark } from './StarToggle';
import { TimelineGuard } from './TimelineGuard';
import './day-timeline.css';
import './day-open.css';

// The timeline pane (ADR 0308): the drop pane's other content, grown from the notch's right side. One story top to bottom: the date,
// her sentence about the day, one bar for the whole day (DayBar.tsx) with the day's numbers under it, and the day itself as a list; on
// today a now line, with what is still to come below it. V2 (820 px) is two columns under the bar: the list on the left, never moving, and on the right the
// stage (`.dt-side`): what is selected in the list (its detail page, RowOpen.tsx, which can be asked about: AskDock.tsx), or with nothing selected on today the day's
// now (DayStage.tsx). The pane is one size, whatever is in it; on its bottom edge a row of doors (doors.ts,
// DayDoors.tsx) swaps what is under the bar (the line, the numbers, the rows) for another page of the same day, the bar lighting that page's own moments.
// `feed` is today as the wing already reads it; other days are read here.
const rgb = (c: readonly number[]) => c.join(' ');
// The keys of the rows and stay headers in the order the list shows them.
const listed = (blocks: Block[]): string[] => blocks.flatMap(b => b.type === 'row' ? [b.row.key] : b.type === 'stay' ? [b.key, ...listed(b.items)] : []);
// Where `el` is inside the scrolling `list`.
const offsetIn = (el: HTMLElement, list: HTMLElement) => el.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop;

// Motion (reduced: nothing moves, what appears fades in over 120 ms). These are Web Animations on the elements themselves: React keeps the
// classes, and a row that is hovered while it comes up is not interrupted.
// On open the rows in view come up one after another: 6 px and a fade, 220 ms each, 25 ms apart, the first ten. The pane's own contents show
// once it is mostly down (the sessions' rows rise then too), so they start then.
function rise(list: HTMLElement) {
  const go = () => {
    const view = list.getBoundingClientRect();
    [...list.querySelectorAll<HTMLElement>('.dt-row, .dt-sh, .dt-now')].filter(el => { const r = el.getBoundingClientRect(); return r.bottom > view.top && r.top < view.bottom; }).slice(0, 10)
      .forEach((el, i) => el.animate(reduced.matches ? [{ opacity: 0 }, {}] : [{ opacity: 0, transform: 'translateY(6px)' }, {}], { duration: reduced.matches ? 120 : 220, delay: reduced.matches ? 0 : i * 25, easing: EASE, fill: 'backwards' }));
  };
  const pane = list.closest('.notch-pane');
  if (!pane || pane.classList.contains('is-open')) { go(); return () => {}; }
  const watch = new MutationObserver(() => { if (pane.classList.contains('is-open')) { watch.disconnect(); go(); } });
  watch.observe(pane, { attributes: true, attributeFilter: ['class'] });
  return () => watch.disconnect();
}
// An item that arrives while the day is open grows out of nothing: its height from 0 and a fade, 240 ms.
function grow(el: HTMLElement) {
  const style = getComputedStyle(el), full = { height: `${el.offsetHeight}px`, marginTop: style.marginTop, opacity: style.opacity };
  el.animate(reduced.matches ? [{ opacity: 0 }, {}] : [{ height: '0px', marginTop: '0px', opacity: 0, overflow: 'hidden' }, { ...full, overflow: 'hidden' }], { duration: reduced.matches ? 120 : 240, easing: EASE });
}
// An item he has just said is not his collapses out of the list: its height to nothing and a fade, 240 ms (reduced motion: a 120 ms fade). It stays as it
// ended until the day drops it.
function away(el: HTMLElement) {
  const style = getComputedStyle(el);
  return el.animate(reduced.matches ? [{ opacity: 1 }, { opacity: 0 }] : [{ height: `${el.offsetHeight}px`, marginTop: style.marginTop, opacity: 1, overflow: 'hidden' }, { height: '0px', marginTop: '0px', opacity: 0, overflow: 'hidden' }],
    { duration: reduced.matches ? 120 : 240, easing: EASE, fill: 'forwards' });
}
// A day change is a shared axis: the day that was slides away the way time came from, fading (80 ms), and the day that is slides in from the
// other side (140 ms). Never both at once, so two days' words are never on top of each other. The list moves 16 px, the rest of the pane 8.
// The list cut to a range, or whole again, goes the same way without the slide (`dir` 0): only the list fades.
const parts = (root: HTMLElement | null) => [...(root?.children ?? [])].filter((el): el is HTMLElement => el instanceof HTMLElement && !el.classList.contains('dt-head') && !el.classList.contains('dt-doors'));
const reach = (el: HTMLElement) => el.classList.contains('dt-stage') ? 16 : 8;
const leave = (els: HTMLElement[], dir: number) => els.map(el => el.animate([{ opacity: 0, transform: `translateX(${-dir * reach(el)}px)` }], { duration: 80, easing: 'ease-in', fill: 'forwards' }));
const enter = (els: HTMLElement[], dir: number) => els.forEach(el => el.animate(reduced.matches ? [{ opacity: 0 }, {}] : [{ opacity: 0, transform: `translateX(${dir * reach(el)}px)` }, {}], { duration: reduced.matches ? 120 : 140, easing: EASE, fill: 'backwards' }));

// What the rows and the stays' headers share: what a click on one does, which can be asked about, which open to a detail page and which can be
// marked, and the quiet line under a row.
type Cx = {
  click: (e: MouseEvent, key: string) => void; tick: () => void;
  asks: ReadonlySet<string>; opens: ReadonlySet<string>; ask: ((key: string) => void) | null;
  star: { can: ReadonlySet<string>; marks: ReadonlyMap<string, StarMark>; refused: Readonly<Record<string, number>>; press: (key: string) => void };
  line: { key: string; text: string; on: boolean };
};
// hot: what the pointer is on; pick: the selected row (what a click, the bar or the keys chose). Both are bracketed on the bar; the pointer's wins while it is on something.
type Hover = { hot: string | null; onHot: (key: string | null) => void; pick: string | null; t: (l: L) => string; cx: Cx };

// Something that goes stays long enough to fade: `on` turns false and it is gone 200 ms later (at once under reduced motion).
function useFade(on: boolean) {
  const [here, setHere] = useState(on);
  if (on && !here) setHere(true);
  useEffect(() => {
    if (on || !here) return;
    const timer = setTimeout(() => setHere(false), reduced.matches ? 0 : 200);
    return () => clearTimeout(timer);
  }, [on, here]);
  return here;
}
// A count that changes rolls: the old number goes down and out and the new one comes in from above (a count that drops reads as dropping).
function Roll({ n }: { n: number }) {
  const [shown, setShown] = useState(n), [was, setWas] = useState<number | null>(null);
  if (n !== shown) { setWas(shown); setShown(n); }
  useEffect(() => {
    if (was === null) return;
    const timer = setTimeout(() => setWas(null), reduced.matches ? 0 : 240);
    return () => clearTimeout(timer);
  }, [was, shown]);
  return <span className="dt-roll">{was !== null && <i key={`o${was}`} className="is-old" aria-hidden="true">{was}</i>}<i key={`n${shown}`} className={was !== null ? 'is-new' : undefined}>{shown}</i></span>;
}
// A right click on a row asks her about it (when it can be asked about); anywhere else the click is the system's.
const menu = (cx: Cx, key: string) => (e: MouseEvent) => { if (cx.ask && cx.asks.has(key)) { e.preventDefault(); cx.ask(key); } };
// The lit star on a row's glyph or beside a stay's name: his filled, hers outlined; it fades in when the item is marked and out when it is not.
function Lit({ on, hers, className }: { on: boolean; hers: boolean; className: string }) {
  const here = useFade(on);
  return here ? <Star size={10} weight={hers ? 'bold' : 'fill'} className={`${className}${hers ? ' is-hers' : ''}${on ? '' : ' is-gone'}`} aria-label="★"/> : null;
}
// What hangs under a row or a stay's header: the quiet line that has something to say about it (4 s). It opens and closes in place; its words stay while it closes.
function Under({ keyed, cx }: { keyed: string; cx: Cx }) {
  return <Unfold open={cx.line.on && cx.line.key === keyed} className="dt-fold is-line" tick={cx.tick} onSettle={cx.tick} render={() => <p className="dt-hint" role="status">{cx.line.text}</p>}/>;
}

// A row: the time it began, its glyph (a lit star on it when it mattered), its words (an amber badge when it waits on him), and at the
// right its length, how long until it, or its value, then a › when it has a detail. The glyphs of one stay or one trip hang on a rail. A click selects it: its detail is the right column's.
// The pointer lights a row by moving on it, not by arriving: a list that scrolls under a resting pointer (the keys, the wheel) leaves what the keys chose alone.
function RowView({ row, hot, onHot, pick, t, how, cx }: { row: Row; how: string } & Hover) {
  const badge = useFade(row.wait), plain = !cx.opens.has(row.key), box = useRef<HTMLDivElement>(null), seg = row.seg, sub = !!row.detail || !!seg?.counts.length;
  useLayoutEffect(() => { if (row.leaving && box.current) away(box.current); }, [row.leaving]);
  return <div ref={box} className={`dt-row${sub ? ' has-detail' : ''}${seg ? ` is-seg${seg.unknown ? ' is-unknown' : ''}` : ''}${row.future ? ' is-future' : ''}${row.connector ? ' is-leg' : ''}${how}${hot === row.key ? ' is-hot' : ''}${pick === row.key ? ' is-pick' : ''}${plain ? ' is-still' : ''}${row.leaving ? ' is-leaving' : ''}`} data-key={row.key} data-kind={row.kind} data-lk={seg ? seg.lenses.join(' ') || undefined : row.lens ?? undefined} style={seg?.rgb ? { '--c': rgb(seg.rgb) } as CSSProperties : undefined}
    aria-current={pick === row.key ? 'true' : undefined} onPointerMove={() => onHot(row.key)} onPointerLeave={() => onHot(null)} onClick={e => cx.click(e, row.key)} onContextMenu={menu(cx, row.key)}>
    <time className="dt-at">{row.ago && <small>{row.ago}</small>}{row.at}</time>
    <i className="dt-ic">{row.icon === 'dot' ? <span className="dt-pip"/> : <Ic icon={row.icon}/>}<Lit on={row.star} hers={row.hers} className="dt-badge"/></i>
    <span className="dt-tx"><span className="dt-ttl"><b>{row.title}</b>{badge && <i className={`dt-wait${row.wait ? '' : ' is-gone'}`}>{t(['Waiting', '等你'])}</i>}</span>
      {seg ? sub && <em className="dt-sub">{row.detail && <span>{row.detail}</span>}{seg.counts.map(c => <i key={c.icon} className="dt-cn"><Ic icon={c.icon} size={11}/>{c.n}</i>)}</em> : row.detail && <em>{row.detail}</em>}</span>
    <span className={`dt-tm${row.value ? ' is-value' : ''}`}>{row.right}</span>
    <Under keyed={row.key} cx={cx}/>
  </div>;
}
// The day's weather after its date: a glyph, a word, the high and the low. A condition this does not know is just the temperatures.
function Forecast({ w }: { w: WeatherLook }) {
  const Glyph = w.sky ? SKIES[w.sky] : null;
  return <span className="dt-wx" data-sky={w.sky ?? undefined}>{Glyph && <Glyph size={14} aria-hidden="true"/>}{w.word && <span>{w.word}</span>}{w.temps && <em>{w.temps}</em>}{w.sun && <i className="dt-wx-tip" role="tooltip">{w.sun}</i>}</span>;
}
// The day's labels (all-day events) in the room the header leaves them: whole ones, and a +N for those that do not fit (never half a word). The room
// changes with the day (the pill that comes or goes), so it starts again from all of them when it does; the first label always stays, cut to fit if it must (`onCut` says so,
// and the 理一下 pill then gives its words up).
function Chips({ chips, onCut }: { chips: DayModel['chips']; onCut: () => void }) {
  const box = useRef<HTMLSpanElement>(null), [drop, setDrop] = useState(0);
  const labels = chips.filter(c => !c.more), rest = chips.reduce((n, c) => n + (c.more ? Number(c.text.slice(1)) || 0 : 0), 0) + drop;
  useLayoutEffect(() => { if ([...box.current?.children ?? []].some(c => c.scrollWidth > c.clientWidth + 1)) { if (drop < labels.length - 1) setDrop(drop + 1); else onCut(); } });
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    let width = el.clientWidth;
    const ro = new ResizeObserver(() => { if (el.clientWidth !== width) { width = el.clientWidth; setDrop(0); } });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return <span ref={box} className="dt-chips">{labels.slice(0, labels.length - drop).map(c => <i key={c.text} className="dt-chip">{c.text}</i>)}{rest > 0 && <i className="dt-chip is-more">+{rest}</i>}</span>;
}
// The pill that says how many things wait on him: it goes when none does, and a count that changes rolls.
function Waits({ n, first, t }: { n: number; first: () => void; t: (l: L) => string }) {
  const last = useRef(n), here = useFade(n > 0);
  if (n > 0) last.current = n;
  return here ? <button type="button" className={`dt-waits${n > 0 ? '' : ' is-gone'}`} tabIndex={n > 0 ? 0 : -1} onClick={first}><Roll n={n || last.current}/>{t([' waiting', ' 件等你'])}</button> : null;
}
const onWay = (b?: Block) => b?.type === 'row' && (b.row.connector || b.row.onWay);
// A stay: its header (the place, how long, a star when it mattered) and what happened in it, hung on the stay's own rail.
function StayView({ b, gap, hot, onHot, pick, t, cx }: { b: Extract<Block, { type: 'stay' }>; gap: string } & Hover) {
  const plain = !cx.opens.has(b.key);
  return <section className={`dt-stay${gap}`} style={{ '--c': rgb(b.head.rgb) } as CSSProperties}>
    <header className={`dt-sh${b.head.unnamed ? ' is-plain' : ''}${b.items.length ? ' has-rows' : ''}${hot === b.key ? ' is-hot' : ''}${pick === b.key ? ' is-pick' : ''}${plain ? ' is-still' : ''}`} data-key={b.key} aria-current={pick === b.key ? 'true' : undefined}
      onPointerMove={() => onHot(b.key)} onPointerLeave={() => onHot(null)} onClick={e => cx.click(e, b.key)} onContextMenu={menu(cx, b.key)}>
      <span className="dt-at"/><i className="dt-dot"/>
      <span className="dt-tx"><span className="dt-ttl"><b>{b.head.name}</b><Lit on={b.head.star !== null} hers={b.head.hers} className="dt-star"/></span>{b.head.star && <em>{b.head.star}</em>}</span>
      <span className="dt-tm">{b.head.when}<small> · {b.head.dur}</small></span>
      {!plain && <i className="dt-chev" aria-hidden="true"/>}
      <Under keyed={b.key} cx={cx}/>
    </header>
    {b.items.length > 0 && <div className="dt-in"><Blocks blocks={b.items} hot={hot} onHot={onHot} pick={pick} t={t} cx={cx} inside/></div>}
  </section>;
}
// The blocks of one level, told apart in units: a stay with what is in it, a trip (the ways he went and what happened on them), and the
// rest. A unit starts 12 px under the one before it; the now line stays with the unit it follows. A trip's rail is dashed, from its first
// glyph to its last; what is in a stay hangs on that stay's own, from its dot to the last row.
function Blocks({ blocks, hot, onHot, pick, t, cx, inside }: { blocks: Block[]; inside?: boolean } & Hover) {
  let last = '';
  const end = blocks.map(b => b.type).lastIndexOf('row');
  return <>{blocks.map((b, i) => {
    const prev = blocks[i - 1], next = blocks[i + 1];
    const unit = b.type === 'stay' ? `stay ${b.key}` : b.type === 'row' ? onWay(b) ? 'trip' : 'rest' : prev && prev.type !== 'stay' ? last : 'rest';
    const gap = i > 0 && unit !== last ? ' is-gap' : '';
    last = unit;
    if (b.type === 'now') return <div key={b.key} className={`dt-now${gap}${(inside ? i < end : onWay(prev) && onWay(next)) ? ` is-rail${inside ? '' : ' is-trip'}` : ''}`}><span>{b.label}</span></div>;
    if (b.type === 'row') {
      const how = inside ? ` is-rail${i === end ? ' is-end' : ''}` : b.row.seg ? ` is-rail${prev?.type === 'row' && prev.row.seg ? '' : ' is-top'}${next?.type === 'row' && next.row.seg ? '' : ' is-end'}` : onWay(b) ? ` is-rail is-trip${onWay(prev) ? '' : ' is-top'}${onWay(next) ? '' : ' is-end'}` : '';
      return <RowView key={b.row.key} row={b.row} hot={hot} onHot={onHot} pick={pick} t={t} cx={cx} how={gap + how}/>;
    }
    return <StayView key={b.key} b={b} gap={gap} hot={hot} onHot={onHot} pick={pick} t={t} cx={cx}/>;
  })}</>;
}

// The minute the pane is drawn for. Lengths, countdowns and the now line count whole minutes, so the pane looks at the clock every second
// while it is open and draws again when the minute turns.
function useMinute(fast: boolean) {
  const [minute, setMinute] = useState(() => Math.floor(Date.now() / 60_000));
  useEffect(() => {
    const tick = () => setMinute(Math.floor(Date.now() / 60_000));
    tick();
    const id = setInterval(tick, fast ? 1000 : 60_000);
    return () => clearInterval(id);
  }, [fast]);
  return minute * 60_000;
}

// The records of a day (his lines and hers, oldest first), a page of 300 at a time; what a talk row shows of its span is picked from them.
async function readRecords(port: string, day: string): Promise<Rec[]> {
  const out: Rec[] = [];
  for (let offset = 0; offset < 3000;) {
    const r = await fetch(`http://127.0.0.1:${port}/inherent/memory/day/${encodeURIComponent(day)}/records?limit=300&offset=${offset}`, { signal: AbortSignal.timeout(8000) });
    if (!r.ok) throw new Error(`records ${r.status}`);
    const body = await r.json() as { records?: unknown; total?: unknown };
    const page = Array.isArray(body.records) ? body.records : [];
    for (const x of page) {
      const rec = x as Record<string, unknown> | null, ts = typeof rec?.ts === 'string' ? Date.parse(rec.ts) : NaN;
      if (rec && typeof rec.text === 'string' && (rec.who === 'user' || rec.who === 'jarvis') && Number.isFinite(ts)) out.push({ id: String(rec.id ?? ''), ts, who: rec.who, text: rec.text });
    }
    offset += page.length;
    if (!page.length || offset >= Number(body.total)) break;
  }
  return out;
}

// A door page the door's hook has nothing for yet is a blank one.
const BLANK: DoorPage = { count: null, line: null, chips: [], marks: [], rows: [], nowAt: null };
const NO_ITEMS: DayItem[] = [];
// A door's page comes in as the number card does: 180 ms on the pane's ease, rising 4 px (reduced motion: a 120 ms fade); what it replaces is gone at once.
const doorIn = (els: HTMLElement[]) => els.forEach(el => el.animate(reduced.matches ? [{ opacity: 0 }, {}] : [{ opacity: 0, transform: 'translateY(4px)' }, {}], { duration: reduced.matches ? 120 : 180, easing: reduced.matches ? 'linear' : EASE, fill: 'backwards' }));

type Renaming = { key: string; place: string; from: string; draft: string; state: 'edit' | 'busy' | 'failed' };
type Note = { place: string; from: string; change: string | null; state: 'done' | 'failed' };
type Naming = { key: string; draft: string; role: string | null; state: 'edit' | 'busy' | 'failed' };
type Docked = { ask: Ask | null; say: Said | null; state: DockState; nonce: number };

// `swap`: said when what the pane shows has just been swapped for other content in one frame (a day, a cut list), so the pane takes the new height with it.
export function DayTimeline({ port, feed, open, ask, hold, swap, tool, sessions }: { port: string | null; feed: Route<DayReply>; open: boolean; ask?: (text: string, about?: About) => Promise<void>; hold?: (on: boolean) => void; swap?: () => void; tool?: ToolRenderer; sessions?: DoorCtx['sessions'] }) {
  const [{ lang }] = useCompanionSettings(), t = (l: L) => tr(lang, l);
  // null is today, which the wing already reads; another day is read here.
  const [date, setDate] = useState<string | null>(null);
  const other = useRoute<DayReply>(port, `/inherent/day?date=${date ?? ''}`, open && date !== null, 60_000);
  const route = date === null ? feed : other;
  const reply = route.data && (date === null || route.data.date === date) ? route.data : null;
  // What is drawn is the last reply that came, under its own date. While the next day is on its way it stays, dimmed, so the pane does not jump; when
  // it has come the day that was leaves and the new one enters, with its date and the pane's new height. An update of the day already drawn is drawn at once.
  const [shown, setShown] = useState<DayReply | null>(reply), now = useMinute(open), root = useRef<HTMLDivElement>(null), entering = useRef(0), leaving = useRef<Animation[]>([]);
  useEffect(() => {
    if (!reply || reply === shown) return;
    const dir = shown && shown.date !== reply.date ? reply.date > shown.date ? 1 : -1 : 0;
    if (!dir || reduced.matches) { entering.current = dir; setShown(reply); return; }
    let live = true;
    leaving.current = leave(parts(root.current), dir);
    void Promise.all(leaving.current.map(a => a.finished)).then(() => { if (live) { entering.current = dir; setShown(reply); } }, () => {});
    return () => { live = false; leaving.current.forEach(a => a.cancel()); leaving.current = []; };
  }, [reply]);
  useLayoutEffect(() => {
    const dir = entering.current;
    entering.current = 0;
    leaving.current.forEach(a => a.cancel()); leaving.current = [];
    if (dir) { enter(parts(root.current), dir); swap?.(); }
  }, [shown]);
  // Every time it opens, today is read again (the pane stays mounted through a short fold: Notch.tsx).
  useEffect(() => { if (open) feed.reload(); }, [open]);
  // Left open past midnight, the pane is on a day that has ended: today is read at once, not at the next poll, and again every 5 s for as long as
  // the brain still answers with the old day.
  const ends = feed.data?.end_ms, over = open && typeof ends === 'number' && now >= ends;
  useEffect(() => {
    if (!over) return;
    feed.reload();
    const id = setInterval(feed.reload, 5000);
    return () => clearInterval(id);
  }, [over]);
  // What he has just done that the daemon has not said back yet, laid over what was read so the whole pane (the bar, the list, the count)
  // shows it at once while the day is read again: a reminder acknowledged, a kept place renamed, a place he has just named (`keeps`, until the day is read again), a commitment he confirmed (`sure`) or
  // dropped (`dropping` while it collapses out, then `gone`), a star. His marks are the day's highlights as he made them (`base` null: the brain has not answered yet) and
  // then as the brain answered, which stand until the next read of the day.
  const [acks, setAcks] = useState<ReadonlySet<string>>(() => new Set()), [names, setNames] = useState<ReadonlyMap<string, string>>(() => new Map());
  const [sure, setSure] = useState<ReadonlySet<string>>(() => new Set()), [dropping, setDropping] = useState<ReadonlySet<string>>(() => new Set()), [gone, setGone] = useState<ReadonlySet<string>>(() => new Set());
  const [keeps, setKeeps] = useState<ReadonlyMap<string, { kept: NonNullable<DayItem['kept_place']>; base: DayReply | null }>>(() => new Map());
  const [marked, setMarked] = useState<{ date: string; list: Highlight[]; base: DayReply | null } | null>(null), shownNow = useRef(shown);
  shownNow.current = shown;
  const laid = useMemo(() => {
    const stars = shown && marked && marked.date === shown.date && (!marked.base || marked.base === shown) ? marked.list : null;
    if (!shown || !acks.size && !names.size && !keeps.size && !sure.size && !gone.size && !stars) return shown;
    const lay = (x: DayItem): DayItem[] => {
      if (gone.has(x.id)) return [];
      if (acks.has(x.id)) x = { ...x, acknowledged: true };
      if (sure.has(x.id)) x = { ...x, confirmed: true };
      const mine = keeps.get(x.id);
      if (!x.kept_place && mine?.base === shown) x = { ...x, kept_place: mine.kept };
      if (x.kept_place && names.has(x.kept_place.id)) x = { ...x, kept_place: { ...x.kept_place, name: names.get(x.kept_place.id)! } };
      return [x];
    };
    return { ...shown, ...stars && { highlights: stars }, items: shown.items.flatMap(lay) };
  }, [shown, acks, names, keeps, sure, gone, marked]);
  // A day that cannot be built (no usable bounds) says so in a line; a day that builds but fails to draw falls to the guards below.
  const built = useMemo(() => {
    if (!laid) return { model: null, failed: false };
    try { return { model: buildDay(laid, now, t, { you: sure, leaving: dropping }), failed: false }; }
    catch (error) { report('the day', error); return { model: null, failed: true }; }
  }, [laid, now, lang, sure, dropping]);
  const model = built.model;
  // The kinds the day has a number for: a lens that is not one of them (its chip is not there) lights nothing.
  const lensKinds = useMemo(() => new Set(model ? numbers(model, t).map(n => n.kind) : []), [model, lang]);
  // The doors (doors.ts): the one he is on, and every door's page (all their hooks run: the doors show their counts). The pane keeps its door for as long as it is mounted
  // (Notch.tsx remakes it a minute after a fold, on today).
  const [door, setDoor] = useState<DoorKey>('today');
  const pages = useDoors({ port, open, t, lang, ask, items: model?.items ?? NO_ITEMS,
    day: { date: model?.reply.date ?? '', tz: model?.tz ?? '', start: model?.reply.start_ms ?? 0, end: model?.reply.end_ms ?? 0, now: model?.now ?? now, today: model?.today ?? false }, tool, sessions });
  const page = door === 'today' ? null : pages[door] ?? BLANK;
  const todayDate = feed.data?.date ?? null, at = date ?? todayDate;
  // What is drawn is not the day asked for: it is on its way, or (`lost`) it did not come and the day that is drawn is not to be left under the new date.
  // The date over the day is that of the day drawn until the one asked for is drawn; one that did not come is said under its own date.
  const stale = !!shown && !!at && shown.date !== at, lost = stale && !reply && (route.failed || route.missing), here = stale && !lost && shown ? shown.date : at;
  const onToday = date === null && here === todayDate;
  const step = (days: number) => { if (!at) return; const next = shiftDate(at, days); setDate(next === todayDate ? null : next); };
  // A read that does not come says so after a few seconds; the daemon is asked again on its own.
  const [slow, setSlow] = useState(false);
  useEffect(() => { setSlow(false); if (reply) return; const timer = setTimeout(() => setSlow(true), 6000); return () => clearTimeout(timer); }, [!!reply, date]);

  const [hot, setHot] = useState<string | null>(null), [selected, setSelected] = useState<string | null>(null), sideBox = useRef<HTMLDivElement>(null), list = useRef<HTMLDivElement>(null), placed = useRef(''), ui = useBarState(), uiRef = useRef(ui);
  uiRef.current = ui;
  // The range the list is cut to as it is drawn: it follows `ui.filter` once the list has faded out (the bar answers at once; the list and the chip that
  // says so change together), and the list comes in cut, or whole, as a day does. `wanted` is a row to go to when the whole day is back.
  const [cut, setCut] = useState(ui.filter), fading = useRef<Animation[]>([]), cutting = useRef(false), wanted = useRef<string | null>(null);
  // What a door page shows: its rows cut to the range on the bar (a row with no time is out while it is cut), and the rows the lit chip keeps (null: no chip is lit).
  const pageRows = useMemo(() => page ? cut ? page.rows.filter(r => inRange(page.marks, r, Math.min(cut.a, cut.b), Math.max(cut.a, cut.b))) : page.rows : [], [page, cut]);
  const keep = page && ui.chip ? page.chips.find(c => c.id === ui.chip)?.only ?? null : null;
  const edges = () => { const l = list.current; if (!l) return; l.classList.toggle('more-below', l.scrollTop + l.clientHeight < l.scrollHeight - 2); l.classList.toggle('more-above', l.scrollTop > 2); };
  // A day opens where it matters: today at its now line, another day from the top; so does the list cut to a range, and the whole day again.
  // Each time the pane opens its rows in view come up; a pane that folded and opened again keeps its place (it is the one he left).
  const rose = useRef(false), stopRise = useRef(() => {});
  useEffect(() => () => stopRise.current(), []);
  useEffect(() => { if (!open) rose.current = false; }, [open]);
  useLayoutEffect(() => {
    const l = list.current, spot = `${door}:${shown?.date}:${date}:${cut ? 'cut' : ''}`;
    if (l && model && shown) {
      if (placed.current !== spot) {
        placed.current = spot; release();
        const line = l.querySelector<HTMLElement>('.dt-now');
        l.scrollTop = line ? Math.max(0, offsetIn(line, l) - l.clientHeight * .6) : 0;
      }
      if (open && !rose.current) { rose.current = true; stopRise.current(); stopRise.current = rise(l); }
    }
    edges();
  });
  // What the day had when it was last drawn: an item that was not there then, on the same day, grows in (a new stay with all that is in it as one); a row that is not there any
  // more collapses out where it was (a copy of it as it was drawn, kept with the rest then; a list cut to another range is another list, so nothing collapses across a cut).
  const seen = useRef<{ date: string; keys: Set<string>; cut: typeof cut; rows: { key: string; copy: HTMLElement | null }[] } | null>(null);
  useLayoutEffect(() => {
    if (!model || door !== 'today') { seen.current = null; return; }
    const keys = new Set(model.index.map(x => x.key)), before = seen.current, l = list.current;
    seen.current = { date: model.reply.date, keys, cut, rows: [...l?.querySelectorAll<HTMLElement>('[data-key]') ?? []].map(el => ({ key: el.dataset.key!, copy: el.classList.contains('dt-row') && !el.classList.contains('is-leaving') ? el.cloneNode(true) as HTMLElement : null })) };
    if (!before || before.date !== model.reply.date || !l) return;
    const fresh = [...keys].filter(k => !before.keys.has(k)).flatMap(k => { const el = l.querySelector<HTMLElement>(`[data-key="${CSS.escape(k)}"]`); return el ? [el.classList.contains('dt-sh') ? el.parentElement! : el] : []; });
    fresh.filter(el => !fresh.some(o => o !== el && o.contains(el))).forEach(grow);
    if (before.cut !== cut) return;
    const there = new Map([...l.querySelectorAll<HTMLElement>('[data-key]')].map(el => [el.dataset.key!, el]));
    let last: Element | null = null;
    for (const { key, copy } of before.rows) {
      const el = there.get(key);
      if (el) { last = el; continue; }
      if (!copy) continue;
      copy.removeAttribute('data-key'); copy.setAttribute('aria-hidden', 'true'); copy.setAttribute('inert', '');
      if (last) last.after(copy); else l.prepend(copy);
      last = copy;
      away(copy).finished.then(() => copy.remove(), () => copy.remove());
    }
  }, [model, door]);
  // The list scrolls to `to` in `ms`, easing out (at once under reduced motion); a new one takes over from wherever the last had got to.
  const glide = useRef(0);
  const slide = (to: number, ms: number) => {
    const l = list.current;
    if (!l) return;
    const from = l.scrollTop, end = Math.max(0, Math.min(l.scrollHeight - l.clientHeight, to)), began = performance.now();
    cancelAnimationFrame(glide.current);
    if (reduced.matches || Math.abs(end - from) < 1) { l.scrollTop = end; return; }
    const tick = (at: number) => { const k = Math.min(1, (at - began) / ms); l.scrollTop = from + (end - from) * (1 - (1 - k) ** 3); if (k < 1) glide.current = requestAnimationFrame(tick); };
    glide.current = requestAnimationFrame(tick);
  };
  // The row's class is rewritten when a render changes it (the pointer leaving what it was on), so a flash in progress is put back.
  const flashed = useRef<HTMLElement | null>(null), flashEnd = useRef(0);
  useLayoutEffect(() => { const el = flashed.current; if (el && !el.classList.contains('is-flash')) el.classList.add('is-flash'); });
  // A tap on a row that has no detail flashes it (600 ms).
  const flash = (key: string) => {
    const el = list.current?.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"]`);
    if (!el) return null;
    el.classList.remove('is-flash'); void el.offsetWidth; el.classList.add('is-flash');
    flashed.current = el; clearTimeout(flashEnd.current); flashEnd.current = window.setTimeout(() => { flashed.current = null; }, 600);
    return el;
  };
  // A click on the bar, or on a piece of it, takes the row there to the top of the list in 300 ms, and the row flashes.
  const jump = (key: string) => {
    const l = list.current, el = flash(key);
    if (l && el) slide(offsetIn(el, l) - 12, 300);
  };
  // The row is scrolled into view, 24 px clear of the list's edges, in `ms`.
  const show = (key: string, ms = 180) => {
    const l = list.current, el = l?.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"]`);
    if (!l || !el) return;
    const top = offsetIn(el, l), margin = 24;
    if (top < l.scrollTop + margin) slide(top - margin, ms);
    else if (top + el.offsetHeight > l.scrollTop + l.clientHeight - margin) slide(top + el.offsetHeight - l.clientHeight + margin, ms);
  };
  // A quiet line that opens under a row (what did not go through) is brought up to 24 px clear of the list's foot. Where the list has no scroll left to give at
  // its foot it is given room below its last row, which stays only while it is in view (trim), and never more than a third of the list.
  const follow = () => {
    const l = list.current, el = l?.querySelector<HTMLElement>(':has(> .dt-fold.is-line)');
    if (!l || !el || l.scrollHeight <= l.clientHeight) return;
    const view = l.getBoundingClientRect(), box = el.getBoundingClientRect(), top = box.top - view.top, margin = 24, want = Math.min(top, Math.max(margin, view.height - margin - box.height));
    if (Math.abs(top - want) < .5) return;
    const to = l.scrollTop + top - want, max = l.scrollHeight - l.clientHeight, room = parseFloat(l.style.paddingBottom) || 0;
    if (to > max) l.style.paddingBottom = `${Math.min(room + to - max, Math.max(room, view.height / 3))}px`;
    l.scrollTop = Math.max(0, to);
  };
  const trim = () => {
    const l = list.current;
    if (!l || !l.style.paddingBottom) return;
    const foot = Math.min(parseFloat(l.style.paddingBottom) || 0, l.scrollHeight - l.clientHeight - l.scrollTop);
    if (foot > 0) l.style.paddingBottom = `${(parseFloat(l.style.paddingBottom) || 0) - foot}px`;
  };
  const release = () => { const l = list.current; if (l) l.style.paddingBottom = ''; };
  // The waiting pill takes the first row that waits on him to the top of the list (the whole day again when the list is cut).
  const waitFirst = (key: string) => { if (door !== 'today') { wanted.current = key; pickDoor('today'); } else if (cut) { wanted.current = key; ui.setFilter(null); } else jump(key); };
  // A tap on the bar goes to its row; a row the cut has left out is not there to go to, so the whole day comes back first.
  const reveal = (key: string) => { if (cut && !list.current?.querySelector(`[data-key="${CSS.escape(key)}"]`)) { wanted.current = key; ui.setFilter(null); } else jump(key); };
  // What shows the rows (the list, or the line that says the cut has none) fades out, is swapped for the new cut, and comes in; a row asked for is gone to once it is there.
  const rowsEl = () => root.current?.querySelector<HTMLElement>(':scope > .dt-stage > .dt-list, :scope > .dt-say') ?? null;
  useEffect(() => {
    if (ui.filter === cut) return;
    const el = rowsEl();
    cutting.current = true;
    if (!el || reduced.matches) { setCut(ui.filter); return; }
    let live = true;
    fading.current = leave([el], 0);
    void Promise.all(fading.current.map(a => a.finished)).then(() => { if (live) setCut(ui.filter); }, () => {});
    return () => { live = false; cutting.current = false; fading.current.forEach(a => a.cancel()); fading.current = []; };
  }, [ui.filter]);
  useLayoutEffect(() => {
    if (!cutting.current) return;
    cutting.current = false;
    fading.current.forEach(a => a.cancel()); fading.current = [];
    const el = rowsEl();
    if (el) { enter([el], 0); swap?.(); }
    if (wanted.current) { const key = wanted.current; wanted.current = null; jump(key); }
  }, [cut]);

  // ---- opening a row, and what can be done from it (RowOpen.tsx, AskDock.tsx) ----
  // Which rows can be asked about: those whose kind and id the brain takes (dayOpen.askOf says what is handed over).
  const asks = useMemo<ReadonlySet<string>>(() => new Set([...(model?.items ?? []).filter(x => askable(x.kind, x.id)).map(x => x.id), ...model?.segs.keys() ?? []]), [model]);
  const canAsk = (key: string) => !!ask && asks.has(key);
  // Which items can be marked as what mattered (every one with an id the brain takes), and who marked those that are: him, or her.
  const starrables = useMemo<ReadonlySet<string>>(() => new Set((model?.items ?? []).filter(x => starrable(x.id)).map(x => x.id)), [model]);
  const marks = useMemo<ReadonlyMap<string, StarMark>>(() => new Map([...model?.stars ?? []].map(([k, m]) => [k, m.by === 'her' ? 'her' : 'him'])), [model]);
  // Which rows open to something (a part, or a button that works): a row never opens to nothing, and one that does not is drawn as it is.
  const opens = useMemo<ReadonlySet<string>>(() => new Set(model ? [...model.rows.keys(), ...model.heads.keys()].filter(k => { const o = openOf(model, k, t); return !!o && hasBody(o, !!ask && asks.has(k)); }) : []), [model, asks, !!ask]);
  // On a door page the rows that open are the ones its hook says do; `opensNow` is those of the page that is drawn.
  const pageOpens = useMemo<ReadonlySet<string>>(() => new Set(pageRows.filter(r => r.opens).map(r => r.key)), [pageRows]);
  const opensNow = door === 'today' ? opens : pageOpens;
  // A row with a detail is selected: its detail is the right column's, and the list, the bar and the keys stay where they are. A row without one only flashes.
  // The page's row before (-1) or after (1) it among the rows that open, in the list's order; null at either end.
  const aside = (dir: 1 | -1) => { const i = selected ? order.indexOf(selected) : -1; return i < 0 ? null : order[i + dir] ?? null; };
  const turn = (dir: 1 | -1) => { const to = aside(dir); if (to) { setSelected(to); show(to); } };
  const drop = () => setSelected(null);
  // A button of a door page's row that is marked `data-opens` is a click on the row (doors.ts).
  const click = (e: MouseEvent, key: string) => {
    if ((e.target as Element).closest('.dt-fold, button:not([data-opens]), input, a')) return;
    if (opensNow.has(key)) setSelected(key); else flash(key);
  };
  // A click on the bar (a range is not one): on a piece that opens, that row is selected and taken into view; on one that does not, it flashes; anywhere else the selection goes.
  const onPage = (piece: string | null) => {
    if (piece === selected) return;
    if (piece && opensNow.has(piece)) { setSelected(piece); show(piece, 300); } else if (piece) jump(piece); else setSelected(null);
  };
  // The right column's content is swapped in 140 ms: a fade and 6 px of the first part (reduced motion: a fade).
  useLayoutEffect(() => {
    const el = sideBox.current;
    if (!el || !open) return;
    const q = reduced.matches, opt = { duration: q ? 120 : MOTION.fast, easing: q ? 'linear' : EASE };
    el.animate([{ opacity: 0 }, {}], opt);
    if (!q) el.firstElementChild?.animate([{ transform: 'translateX(6px)' }, {}], opt);
  }, [selected, door, open]);
  // After a change the day is read again at once (today's too when it is another day that is open, for the wing's line).
  const again = () => { route.reload(); if (route !== feed) feed.reload(); };
  // 理一下 (ADR 0310): today's newest part of the day is written now, which takes the brain up to a minute. `running` for as long as it does (a press in the meantime does
  // nothing); then what came of it for a moment: a new run (today is read again and the pane takes it as an update of the day), `same` (nothing to change) or `failed`.
  const [tidy, setTidy] = useState<'idle' | 'running' | 'same' | 'failed'>('idle'), tidying = useRef(false);
  const tidyUp = async () => {
    if (!port || tidying.current) return;
    tidying.current = true; setTidy('running');
    let done: 'idle' | 'same' | 'failed' = 'failed';
    try {
      const out = await postRoute(port, '/inherent/day/tidy', undefined, 90_000) as { ok?: unknown; written?: unknown };
      if (out.ok === true) { done = out.written === true ? 'idle' : 'same'; if (out.written === true) feed.reload(); }
    } catch { /* the pill says so */ }
    tidying.current = false; setTidy(done);
  };
  // With no room for the first label whole beside the pill it is only its sparkle (idle: a rest on it says what it is; what it says while it runs or after is always in words). The room is
  // looked at again from the whole pill when what is in the header changes.
  const [tight, setTight] = useState(false), head = useRef<HTMLDivElement>(null), shape = `${model?.chips.map(c => c.text).join('|')}/${model?.waiting.length}/${model?.weather ? 1 : 0}/${lang}/${tidy}`;
  useLayoutEffect(() => setTight(false), [shape]);
  // The header's own width is not the pill's to change (a pane that was folded has none): when it changes the room is looked at again.
  useLayoutEffect(() => {
    const el = head.current;
    if (!el) return;
    let width = el.clientWidth;
    const ro = new ResizeObserver(() => { if (el.clientWidth !== width) { width = el.clientWidth; setTight(false); } });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  useEffect(() => { if (tidy !== 'same' && tidy !== 'failed') return; const timer = setTimeout(() => setTidy('idle'), tidy === 'same' ? 2000 : 3000); return () => clearTimeout(timer); }, [tidy]);
  // What each button is doing, by `what:key`: busy, failed, or (a turn handed over) sent.
  const [work, setWork] = useState<Readonly<Record<string, string>>>({});
  const put = (what: string, key: string, state: string) => setWork(w => ({ ...w, [`${what}:${key}`]: state }));
  // 知道了: a reminder that rang is acknowledged on the brain; the pane shows it at once and reads the day again.
  const acknowledge = async (id: string) => {
    if (!port) return;
    put('ack', id, 'busy');
    try { await postRoute(port, `/inherent/notices/${encodeURIComponent(id)}`, { action: 'dismissed' }); setAcks(a => new Set(a).add(id)); put('ack', id, ''); }
    catch { put('ack', id, 'failed'); }
    again();
  };
  // 是我的 / 不是: what mail said he has to go to (ADR 0274), confirmed or dropped on the brain. The brain answers first, so one that did not go through stays as
  // it was and says so; then the pane shows it and reads the day again. Confirmed: the badge fades, the count rolls down and the row says he confirmed it.
  // Dropped: the row collapses out and the bar's box fades (the day is read again once it has gone). Already confirmed (409) is what he asked for; a
  // commitment she no longer has (404) goes the way of one he dropped.
  const settle = async (key: string, hex: string, action: 'confirm' | 'cancel') => {
    if (!port) return;
    put('commit', key, 'busy');
    let status = 0;
    try { await postRoute(port, `/inherent/commitments/${hex}`, { action }); }
    catch (error) { status = (error as { status?: number }).status ?? -1; }
    const mine = action === 'confirm' && (status === 0 || status === 409), out = status === 404 || action === 'cancel' && status === 0;
    if (!mine && !out) { put('commit', key, 'failed'); return; }
    put('commit', key, '');
    if (mine) { setSure(s => new Set(s).add(key)); again(); return; }
    setDropping(s => new Set(s).add(key));
    setTimeout(() => { setGone(s => new Set(s).add(key)); setSelected(p => p === key ? null : p); again(); }, reduced.matches ? 120 : 240);
  };
  // A quiet line under a row for 4 s: what did not go through, or why (`on` goes false when it is time; the words stay while it closes).
  const [line, setLine] = useState({ key: '', text: '', on: false });
  const tell = (key: string, text: string) => setLine({ key, text, on: true });
  useEffect(() => { if (!line.on) return; const timer = setTimeout(() => setLine(l => ({ ...l, on: false })), 4000); return () => clearTimeout(timer); }, [line]);
  // ★: his mark on an item of the day, or his taking it off (a pick of hers that he marks becomes his; one he takes off is hers no longer). It shows at
  // once; the brain's answer, the day's highlights after it, replaces what was shown. Five of his marks is the cap: that is a 409, and the star shakes
  // (when five are already standing the star does not pretend, it only asks).
  const [refused, setRefused] = useState<Readonly<Record<string, number>>>({}), asked = useRef(0);
  const star = async (key: string) => {
    const day = model?.reply.date, was = model?.stars.get(key);
    if (!port || !model || !day || !starrable(key)) return;
    const on = !was || was.by === 'her', list = model.reply.highlights ?? [], full = on && [...model.stars.values()].filter(m => m.by !== 'her').length >= 5, turn = ++asked.current;
    if (!full) setMarked({ date: day, base: null, list: on ? [...list.filter(h => h.id !== key), { id: key, label: was?.label ?? '', by: 'him' }] : list.filter(h => h.id !== key) });
    try {
      const out = await postRoute(port, '/inherent/day/highlight', { day, id: key, on }) as { highlights?: unknown };
      if (turn === asked.current) setMarked({ date: day, base: shownNow.current, list: marksOf(out.highlights) });
    } catch (error) {
      if (turn === asked.current) {
        const capped = (error as { status?: number }).status === 409;
        setMarked(null);
        if (capped) setRefused(r => ({ ...r, [key]: (r[key] ?? 0) + 1 }));
        tell(key, capped ? t(['At most 5 marks. Unmark one first', '最多标 5 个，先取消一个']) : t(["Couldn't mark it. Try again later", '没标上，稍后再试']));
      }
    }
    again();
  };
  // 改名字: the name of a kept place, in the stay's header. Enter saves; a quiet line says so for 5 s, with 撤销.
  const [rename, setRename] = useState<Renaming | null>(null), [note, setNote] = useState<Note | null>(null), lastNote = useRef(note);
  if (note) lastNote.current = note;
  const startRename = (key: string) => { const o = model && openOf(model, key, t); if (o?.rename) { setLeft(false); setRename({ key, place: o.rename.id, from: o.rename.name, draft: o.rename.name, state: 'edit' }); } };
  const saveRename = async () => {
    const r = rename, name = r?.draft.trim();
    if (!r || !name || r.state === 'busy' || !port) return;
    if (name === r.from) { setRename(null); return; }
    setRename({ ...r, state: 'busy' });
    try {
      const out = await postRoute(port, `/inherent/memory/places/${encodeURIComponent(r.place)}`, { name }) as { change?: unknown };
      setNames(m => new Map(m).set(r.place, name)); setNote({ place: r.place, from: r.from, change: changeId(out.change), state: 'done' }); setRename(null); again();
    } catch { setRename(x => x && { ...x, state: 'failed' }); }
  };
  const undo = async () => {
    const n = note;
    if (!n?.change || !port) return;
    try { await postRoute(port, '/inherent/memory/places/undo', { change_id: n.change }); setNames(m => new Map(m).set(n.place, n.from)); setNote(null); again(); }
    catch { setNote({ ...n, state: 'failed' }); }
  };
  useEffect(() => { if (!note) return; const timer = setTimeout(() => setNote(null), 5000); return () => clearTimeout(timer); }, [note]);
  // 起个名字: a place that has no name is kept under the one he types, at the stay's own point, and as the kind of place he picked (none picked: the brain's `other`).
  // Enter keeps it; the place the brain answers is laid over the stay at once (the bar and the rows take its name and hue) and the day is read again.
  const [naming, setNaming] = useState<Naming | null>(null);
  const startNaming = (key: string) => { const o = model && openOf(model, key, t); if (o?.name) { setLeft(false); setNaming({ key, draft: '', role: null, state: 'edit' }); } };
  const saveNaming = async () => {
    const n = naming, name = n?.draft.trim(), at = n && model && openOf(model, n.key, t)?.name;
    if (!n || !name || !at || n.state === 'busy' || !port) return;
    setNaming({ ...n, state: 'busy' });
    try {
      const out = await postRoute(port, '/inherent/memory/places', { name, lat: at.lat, lng: at.lng, ...n.role && { role: n.role } }) as { place?: unknown };
      const kept = keptOf(out.place);
      if (kept) setKeeps(m => new Map(m).set(n.key, { kept, base: shownNow.current }));
      setNaming(null); again();
    } catch { setNaming(x => x && { ...x, state: 'failed' }); }
  };
  // 问她: an input slides up at the pane's foot with what it is about as a chip; Enter hands the turn over; 已发给她 for 1.5 s and it slides away.
  const [dock, setDock] = useState<Docked | null>(null), [draft, setDraft] = useState(''), lastDock = useRef(dock);
  if (dock) lastDock.current = dock;
  const openAsk = (key: string) => {
    const a = model && ask ? askOf(model, key, t) : null;
    if (a) { if (dock?.say) setDraft(''); setLeft(false); setDock(d => ({ ask: a, say: null, state: d?.state === 'busy' ? 'busy' : 'idle', nonce: (d?.nonce ?? 0) + 1 })); }
  };
  const sendAsk = async () => {
    const d = dock, text = draft.trim();
    if (!d || !text || d.state === 'busy' || !ask) return;
    setDock({ ...d, state: 'busy' });
    try { await ask(d.ask?.lead ? `${d.ask.lead}${text}` : text, d.ask?.lead ? undefined : d.ask?.about); setDraft(''); setDock(x => x && { ...x, state: 'sent' }); }
    catch { setDock(x => x && { ...x, state: 'failed' }); }
  };
  useEffect(() => { if (dock?.state !== 'sent') return; const timer = setTimeout(() => setDock(null), 1500); return () => clearTimeout(timer); }, [dock?.state]);
  // 这段在干嘛: the same input for a range of the day (an open stretch, or the range dragged on the bar), with the range as its chip. Enter hands his words to the
  // brain's one route for them (ADR 0302); 记下了 for 1.5 s, then the day is read again. What he wrote shows through the doing words the brain serves, never laid on here.
  // A failure stays under the input until he edits. The input goes with the day it was opened on.
  const openSay = (said: Said) => {
    if (dock?.state === 'busy') return;
    if (dock && !dock.say) setDraft('');
    setLeft(false); setDock(d => ({ ask: null, say: said, state: 'idle', nonce: (d?.nonce ?? 0) + 1 }));
  };
  const sendSay = async () => {
    const d = dock, body = d?.say && answerOf(d.say, draft);
    if (!d || !body || d.state === 'busy' || !port) return;
    setDock({ ...d, state: 'busy' });
    try { await postRoute(port, '/inherent/day/answer', body); }
    catch { setDock(x => x && { ...x, state: 'failed' }); return; }
    setDraft(''); setDock(x => x && { ...x, state: 'sent' }); again();
  };
  // 让她总结这段: one turn at once, about the work block.
  const summarize = async (key: string) => {
    const a = model && ask ? askOf(model, key, t) : null;
    if (!a || !ask) return;
    put('sum', key, 'busy');
    try { await ask(t(['Summarize this stretch', '总结一下这一段']), a.about); put('sum', key, 'sent'); }
    catch { put('sum', key, 'failed'); }
  };
  // The talk's words come from the day's records, read once for a day (again after half a minute).
  const records = useRef(new Map<string, { at: number; list: Promise<Rec[]> }>());
  const readDay = (day: string) => {
    const hit = records.current.get(day);
    if (hit && Date.now() - hit.at < 30_000) return hit.list;
    const list = readRecords(port ?? '', day);
    records.current.set(day, { at: Date.now(), list });
    list.catch(() => records.current.delete(day));
    return list;
  };
  // The picture large over the window.
  const [viewer, setViewer] = useState<{ id: string; note: string } | null>(null);
  // While a field has the text cursor or the picture is up the pane is held open (Notch.tsx takes the keys too), whatever the pointer does;
  // a field he has left (clicked away, or the window lost the keys) no longer holds it.
  const [left, setLeft] = useState(false), [typing, setTyping] = useState(false);
  const holding = !!viewer || (!!dock || !!rename || !!naming) && !left || typing;
  useEffect(() => { hold?.(holding); return () => hold?.(false); }, [holding]);
  // The selection, the field, the input and the picture are the day's: they go with it. The field, the input and the picture go with the pane
  // too; the selection stays through a fold, for the pane that comes back as it was (one that comes back later is made afresh, with nothing selected).
  useEffect(() => { setHot(null); drop(); }, [date, shown?.date, cut]);
  useEffect(() => { if (dock?.say) { setDock(null); setDraft(''); } }, [date, shown?.date]);
  useEffect(() => { if (selected && model && door === 'today' && !model.rows.has(selected) && !model.heads.has(selected)) drop(); }, [model]);
  // A row of a door page that is gone from the page (a letter he has just archived) takes its detail with it.
  useEffect(() => { if (selected && page && !page.rows.some(r => r.key === selected)) drop(); }, [page]);
  useEffect(() => { if (rename && selected !== rename.key) setRename(null); }, [selected]);
  useEffect(() => { if (naming && selected !== naming.key) setNaming(null); }, [selected]);
  useEffect(() => { if (!open) { release(); setRename(null); setNaming(null); setDock(null); setDraft(''); setViewer(null); setNote(null); setTyping(false); } }, [open]);
  useEffect(() => { if (!selected) setTyping(false); }, [selected]);
  // The detail page of a row: its title and when, what dayOpen made of it, and the buttons that work for it, each with what it is doing.
  const detail = (key: string): ReactNode => {
    const o = model && openOf(model, key, t), head = model && headOf(model, key, t);
    if (!o || !head) return null;
    const said = model && sayOf(model, key), sum = work[`sum:${key}`] as 'busy' | 'sent' | 'failed' | undefined, acking = o.ack ? work[`ack:${o.ack}`] : '', saying = work[`commit:${key}`];
    const acts: Acts = {
      port, t, records: readDay, picture: (id, text) => setViewer({ id, note: text }),
      ask: canAsk(key) ? () => openAsk(key) : null,
      rename: o.rename ? { state: rename?.key === key ? rename.state === 'failed' ? 'failed' : 'editing' : 'idle', start: () => startRename(key) } : null,
      name: o.name ? { state: naming?.key === key ? naming.state === 'edit' ? 'editing' : naming.state : 'idle', draft: naming?.key === key ? naming.draft : '', role: naming?.key === key ? naming.role : null, start: () => startNaming(key),
        set: text => setNaming(n => n && { ...n, draft: text, state: n.state === 'failed' ? 'edit' : n.state }), pick: role => setNaming(n => n && { ...n, role }), save: () => void saveNaming(), field: on => setLeft(!on) } : null,
      ack: o.ack ? { state: acking === 'busy' ? 'busy' : acking === 'failed' ? 'failed' : 'idle', press: () => void acknowledge(o.ack!) } : null,
      commit: o.commit ? { state: saying === 'busy' ? 'busy' : saying === 'failed' ? 'failed' : 'idle', confirm: () => void settle(key, o.commit!, 'confirm'), cancel: () => void settle(key, o.commit!, 'cancel') } : null,
      summarize: o.summarize && canAsk(key) ? { state: sum ?? 'idle', press: () => void summarize(key) } : null,
      say: said ? () => openSay(said) : null,
      star: o.star ? <StarToggle mark={marks.get(key) ?? null} nth={refused[key] ?? 0} press={() => void star(key)} t={t}/> : null,
      line: line.on && line.key === key ? line.text : '',
    };
    const renaming = rename?.key === key ? { draft: rename.draft, busy: rename.state === 'busy', set: (text: string) => setRename(r => r && { ...r, draft: text, state: r.state === 'failed' ? 'edit' : r.state }), save: () => void saveRename(), field: (on: boolean) => setLeft(!on) } : null;
    return <DetailPage key={key} head={head} open={o} acts={acts} rename={renaming} back={drop} home={model?.today ? t(['Now', '现在']) : t(['The day', '这一天'])} strip={model && model.segs.has(key) ? partsOf(model, model.segs.get(key)!, model.now) : []}/>;
  };
  // The rows and their order come before the door's detail, which asks `aside` for its neighbours as this is drawn.
  const rows = model && cut ? within(model.blocks, model.span, Math.min(cut.a, cut.b), Math.max(cut.a, cut.b)) : model?.blocks ?? [];
  const order = door === 'today' ? listed(rows).filter(k => opens.has(k)) : pageRows.filter(r => r.opens).map(r => r.key);
  // The detail of a door page's row (its hook makes the title and the body; a `bare` one is the body alone, a page with a head of its own).
  const doorDetail = (key: string) => {
    const d = page?.detail?.(key, drop);
    return d ? { bare: !!d.bare, node: d.bare ? <Fragment key={key}>{d.body}</Fragment>
      : <DoorDetail key={key} title={d.title} body={d.body} back={drop} prev={aside(-1) ? () => turn(-1) : null} next={aside(1) ? () => turn(1) : null} t={t}/> } : null;
  };
  // A bare page up: it takes the whole right column, goes back by its own ‹ (below), and the frame's keys are not for it.
  const shownDoor = page && selected ? doorDetail(selected) : null, tooled = !!selected && !!shownDoor?.bare;
  // A door: what was on goes at once (the selection, the row the pointer had, the chip, the day's lens, the range being chosen), and the new page fades in
  // (the effect below). The range the list is cut to stays: the bar is the same day on every door.
  const pickDoor = (next: DoorKey) => {
    if (next === door) return;
    setDoor(next); setHot(null); drop();
    ui.setPinned(null); ui.setPreview(null); ui.setChipPinned(null); ui.setChipPreview(null); ui.setSel(null);
  };
  const fadedDoor = useRef(door);
  useLayoutEffect(() => {
    if (fadedDoor.current === door) return;
    fadedDoor.current = door;
    doorIn([...root.current?.querySelectorAll<HTMLElement>(':scope > .dt-line, :scope > .dt-numbers, :scope > .dt-stage, :scope > .dt-say') ?? []]);
    // back on today from the waiting pill: the row it was for is taken to the top
    if (door === 'today' && wanted.current) { const key = wanted.current; wanted.current = null; reveal(key); }
  }, [door]);
  // A line under the selected row waits: its detail says it itself.
  const cx: Cx = { click, tick: () => { follow(); edges(); }, asks, opens, ask: ask ? openAsk : null, line: selected && line.key === selected ? { ...line, on: false } : line, star: { can: starrables, marks, refused, press: key => void star(key) } };

  // Esc lets go of one thing at a time, nearest first: the picture, the input, the name being changed or given, the selection, the bar's range, lens (or a door's chip) and
  // the list's cut to a range, then a door goes back to today, and then the pane itself (the pane's own Esc listener was added after this one, so this one answers
  // first). A folded pane, which stays mounted, answers nothing.
  // A bare page goes back by itself: a field it has the cursor in lets go first, then its ‹ is pressed (a category, an item, or the page itself, which calls `drop`).
  const toolBack = () => {
    const pg = sideBox.current, at = document.activeElement, back = pg?.querySelector<HTMLElement>('.pg-back');
    if (at instanceof HTMLElement && pg?.contains(at) && at.matches('input, select, textarea')) at.blur(); else if (back) back.click(); else drop();
  };
  const back = useRef<() => boolean>(() => false);
  back.current = () => {
    if (!open) return false;
    if (viewer) setViewer(null);
    else if (dock) { setDock(null); setDraft(''); }
    else if (rename) setRename(null);
    else if (naming) setNaming(null);
    else if (selected) { if (tooled) toolBack(); else drop(); }
    else if (uiRef.current.escape()) { /* one thing of the bar's let go */ }
    else if (door !== 'today') pickDoor('today');
    else return false;
    return true;
  };
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape' && !e.isComposing && back.current()) { e.preventDefault(); e.stopImmediatePropagation(); } };
    window.addEventListener('keydown', key, true);
    return () => { window.removeEventListener('keydown', key, true); cancelAnimationFrame(glide.current); };
  }, []);
  // His range, the cut and the lens in the hand belong to the day they were made on: they go when another day is asked for and again when it is
  // drawn (a day that rolls over at midnight is a new day with nobody asking), before it is painted so the new day never shows the old cut.
  useLayoutEffect(() => { ui.setSel(null); ui.setFilter(null); ui.setPreview(null); ui.setChipPinned(null); ui.setChipPreview(null); setCut(null); }, [date, shown?.date]);

  // The keys of an open day (the window has key focus only after a click pinned it: Notch.tsx): ⌘1-6 the doors, ↑ ↓ the row before and after among those with a detail, which the right column
  // follows at once and the bar frames (the list keeps it in view), ← → or ⌘← ⌘→ the day before and after, A asks her about the selected row, S marks it (or takes the mark off), Tab moves
  // the focus between the two columns. With nothing selected ↑ ↓ start from where he is looking: ↓ at the first row under the now line (else the top of the list), ↑ at the last row
  // above it (else the bottom).
  const go = (days: number) => { if (at && !(days > 0 && date === null)) step(days); };
  const walk = (dir: 1 | -1) => {
    const l = list.current, rows = l ? [...l.querySelectorAll<HTMLElement>('[data-key]')].filter(r => opensNow.has(r.dataset.key!)) : [];
    if (!l || !rows.length) return;
    let next = selected ? rows.findIndex(r => r.dataset.key === selected) : -1;
    if (next >= 0) next = Math.max(0, Math.min(rows.length - 1, next + dir));
    else {
      const view = l.getBoundingClientRect(), line = l.querySelector('.dt-now')?.getBoundingClientRect(), seen = !!line && line.top >= view.top && line.bottom <= view.bottom;
      const from = dir > 0 ? seen ? line!.bottom : view.top : seen ? line!.top : view.bottom;
      next = dir > 0 ? rows.findIndex(r => r.getBoundingClientRect().top >= from - 1) : rows.reduce((last, r, i) => r.getBoundingClientRect().bottom <= from + 1 ? i : last, -1);
      if (next < 0) next = dir > 0 ? rows.length - 1 : 0;
    }
    const key = rows[next].dataset.key!;
    setSelected(key); setHot(null); show(key);
  };
  // Tab: out of the right column into the list, or into the right column (its first button, else the column itself).
  const swapFocus = () => {
    const side = sideBox.current, l = list.current, at = document.activeElement;
    if (side && at && side.contains(at)) l?.focus({ preventScroll: true });
    else (side?.querySelector<HTMLElement>('button, a, input, textarea, select') ?? side)?.focus({ preventScroll: true });
  };
  const keys = useRef({ open, go, walk, selected, star, can: starrables, tooled, door: pickDoor, ask: openAsk, canAsk, swapFocus });
  keys.current = { open, go, walk, selected, star, can: starrables, tooled, door: pickDoor, ask: openAsk, canAsk, swapFocus };
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      const k = keys.current, target = e.target as Element | null;
      if (!k.open || e.isComposing || e.altKey || target?.closest?.('input,textarea,select,[contenteditable]')) return;
      const cmd = e.metaKey || e.ctrlKey;
      // a bare page up (更多, 额度) has keys of its own: only the doors are the frame's then
      if (cmd && !e.shiftKey && /^[1-6]$/.test(e.key)) { if (!e.repeat) k.door(DOORS[Number(e.key) - 1].key); }
      else if (k.tooled) return;
      else if (cmd && !e.shiftKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) { if (!e.repeat) k.go(e.key === 'ArrowLeft' ? -1 : 1); }
      else if (e.key === 'Tab' && !cmd) k.swapFocus();
      else if (cmd || e.shiftKey) return;
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { if (!e.repeat) k.go(e.key === 'ArrowLeft' ? -1 : 1); }
      else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') k.walk(e.key === 'ArrowDown' ? 1 : -1);
      else if (e.key.toLowerCase() === 'a' && k.selected && k.canAsk(k.selected)) { if (!e.repeat) k.ask(k.selected); }
      else if (e.key.toLowerCase() === 's' && k.selected && k.can.has(k.selected)) { if (!e.repeat) void k.star(k.selected); }
      else return;
      e.preventDefault(); e.stopPropagation();
    };
    window.addEventListener('keydown', key, true);
    return () => window.removeEventListener('keydown', key, true);
  }, []);

  const title = here ? dateTitle(here, t) : model?.title ?? '';
  const say = !port ? t(['Not connected to Jarvis yet', '还没连上 Jarvis']) : route.missing ? t(["This Jarvis can't give a day yet", '这台 Jarvis 还不会给出一天']) : slow || route.failed ? t(["Couldn't read this day. Trying again.", '没读到这一天，会再试一次']) : t(['Reading the day…', '读着这一天…']);
  const kept = note ?? lastNote.current, shownDock = dock ?? lastDock.current;
  // A door page as the frame draws it: its numbers row (a chip with `only` is a lens), its rows (the now line among the timed ones, the untimed ones after their heading), or the words that say why
  // there are none. A click on the bar goes to its row; on a door page that row is also the one chosen.
  const barPage: PageBar | null = page && { marks: page.marks, mute: keep, numbers: <DoorNumbers page={page} pinned={ui.chipPinned} preview={ui.setChipPreview} pin={ui.setChipPinned} t={t}/> };
  // A tap on the bar: the row that opens is selected (the door's too), and either way it is taken to the top of the list and flashes.
  const barJump = (key: string) => { if (opensNow.has(key)) setSelected(key); reveal(key); };
  const hasList = page ? pageRows.length > 0 : rows.length > 0;
  // A page whose rows have no time at all (更多) has no time column to keep.
  const timeless = !!page && pageRows.length > 0 && pageRows.every(r => r.at === null && !r.time);
  const pageSay = page && (!page.rows.length ? page.loading ? t(['Reading…', '读着…']) : page.failed ? t(["Couldn't read this page. Trying again.", '没读到这一页，会再试一次']) : page.empty ?? t(['Nothing here yet', '这里还没有东西']) : t(['Nothing in this stretch', '这一段里没有']));
  const doorBlocks = (m: DayModel, p: DoorPage): ReactNode => {
    const timed = pageRows.filter(r => r.at !== null), rest = pageRows.filter(r => r.at === null), lo = cut ? Math.min(cut.a, cut.b) : -Infinity, hi = cut ? Math.max(cut.a, cut.b) : Infinity;
    const nowAt = p.nowAt !== null && p.nowAt >= lo && p.nowAt <= hi ? p.nowAt : null, next = nowAt === null ? -1 : timed.findIndex(r => r.at! > nowAt), split = next < 0 ? timed.length : next;
    const one = (r: DoorRow) => <DoorRowView key={r.key} row={r} tz={m.tz} hot={hot === r.key} pick={selected === r.key} off={!!keep && !keep.has(r.key)} onHot={setHot} onClick={click}/>;
    return <>{timed.slice(0, split).map(one)}{nowAt !== null && <div className="dt-now"><span>{t([`Now ${clock(nowAt, m.tz)}`, `现在 ${clock(nowAt, m.tz)}`])}</span></div>}{timed.slice(split).map(one)}
      {rest.length > 0 && p.untimed && <p className="dt-uh">{p.untimed}</p>}{rest.map(one)}</>;
  };
  return <div ref={root} className={`dt${stale && !lost ? ' is-loading' : ''}${open ? '' : ' is-folded'}`} data-door={door} data-lens={!page && ui.lens && lensKinds.has(ui.lens) ? ui.lens : undefined} role="region" aria-label={t(['Day timeline', '一天的时间线'])}>
    <div ref={head} className="dt-head">
      <b className="dt-date">{title}</b>{here === todayDate ? <em className="dt-today">{t(['Today', '今天'])}</em> : <button type="button" className="dt-back" onClick={() => setDate(null)}>{t(['Back to today', '回到今天'])}</button>}
      <TimelineGuard where="the header" reset={shown} fallback={null}>
        {!lost && model?.weather && <Forecast w={model.weather}/>}
        {!lost && model && <Chips key={model.chips.map(c => c.text).join('|')} chips={model.chips} onCut={() => setTight(true)}/>}
        <Waits n={lost ? 0 : model?.waiting.length ?? 0} first={() => { const k = model?.waiting[0]; if (k) waitFirst(k); }} t={t}/>
      </TimelineGuard>
      {onToday && model && !lost && <button type="button" className={`dt-tidy${tidy === 'running' ? ' is-running' : ''}${tight && tidy === 'idle' ? ' is-tight' : ''}`} aria-busy={tidy === 'running'} title={tight && tidy === 'idle' ? t(['Tidy', '理一下']) : undefined} aria-label={tight && tidy === 'idle' ? t(['Tidy', '理一下']) : undefined} onClick={() => void tidyUp()}>
        <Sparkle size={12} weight="fill" aria-hidden="true"/>
        <span key={tidy}>{tidy === 'running' ? t(['Tidying…', '在理…']) : tidy === 'same' ? t(['No change', '没有要改的']) : tidy === 'failed' ? t(["Didn't work", '没理成，等会儿再试']) : t(['Tidy', '理一下'])}</span></button>}
      <span className="dt-nav">
        <button type="button" aria-label={t(['The day before', '前一天'])} disabled={!at} onClick={() => step(-1)}><CaretLeft size={13}/></button>
        <button type="button" aria-label={t(['The day after', '后一天'])} disabled={!at || date === null} onClick={() => step(1)}><CaretRight size={13}/></button>
      </span>
    </div>
    {built.failed ? <p className="dt-say">{t(["Couldn't draw this day", '这一天没画出来'])}</p> : !model || lost ? <p className="dt-say">{say}</p> : <TimelineGuard where="the pane" reset={shown} fallback={<p className="dt-say">{t(["Couldn't draw this day", '这一天没画出来'])}</p>}>
      {page ? <p className="dt-line is-door">{page.line}</p> : model.line && <p className={`dt-line${model.line.draft ? ' is-draft' : ''}`}>{model.line.text}</p>}
      {model.empty && !page ? <p className="dt-say">{t(['Nothing recorded for this day yet', '这一天还没有记下什么'])}</p> : <>
        <DayBar model={model} port={port} ui={ui} cut={cut} sweep={tidy === 'running' && onToday} hot={hot ?? selected} open={selected} door={barPage} onHot={setHot} onJump={barJump} onPage={onPage} onPicture={(id, note) => setViewer({ id, note })} onSay={openSay}/>
        {barPage && !model.items.length && !model.segs.size && barPage.numbers}
        {hasList ? <div className={`dt-stage${tooled ? ' is-tool' : ''}`}>
          <div ref={list} className={`dt-list${timeless ? ' is-timeless' : ''}`} tabIndex={-1} onScroll={() => { trim(); edges(); }} onWheel={() => cancelAnimationFrame(glide.current)}>{page ? doorBlocks(model, page) : <Blocks blocks={rows} hot={hot} onHot={setHot} pick={selected} t={t} cx={cx}/>}</div>
          <div ref={sideBox} className="dt-side" tabIndex={-1} data-page={selected ?? undefined} onFocus={e => { if (page && (e.target as Element).matches('input, textarea')) setTyping(true); }} onBlur={e => { if ((e.target as Element).matches('input, textarea')) setTyping(false); }}>
            {selected ? page ? shownDoor?.node : detail(selected) : !page && <DayStage model={model} now={now} t={t} onPick={onPage} sessions={sessions}
              acts={{ ack: id => void acknowledge(id), commit: (key, hex, action) => void settle(key, hex, action), state: w => work[w] as 'busy' | 'failed' | '' | undefined }}
              onAgent={id => { pickDoor('agents'); setSelected(id); }}/>}</div>
        </div>
          : page ? <p className="dt-say">{pageSay}</p> : cut && <p className="dt-say">{t(['Nothing was recorded in this stretch', '这一段没有记下什么'])}</p>}
      </>}
      {model.foot && <p className="dt-foot">{model.foot}</p>}
      <Unfold className="dt-fold is-dock" open={!!note} tick={cx.tick} onSettle={cx.tick} render={() => kept && <div className="dt-note" role="status"><span>{kept.state === 'failed' ? t(["Couldn't undo it", '没撤销成功']) : t(['Renamed', '改好了'])}</span>
        {kept.state === 'done' && kept.change && <>{' · '}<button type="button" onClick={() => void undo()}>{t(['Undo', '撤销'])}</button></>}</div>}/>
      <Unfold className="dt-fold is-dock" open={!!dock} tick={cx.tick} onSettle={cx.tick} render={() => shownDock && <AskDock about={shownDock.ask} say={shownDock.say?.range ?? null} draft={draft} state={shownDock.state} nonce={shownDock.nonce} onDraft={text => { setDraft(text); setDock(d => d && d.state === 'failed' ? { ...d, state: 'idle' } : d); }}
        onSend={() => void (dock?.say ? sendSay() : sendAsk())} onDrop={() => setDock(d => d && { ...d, ask: null })} onField={on => setLeft(!on)} t={t}/>}/>
    </TimelineGuard>}
    <Doors on={door} pages={pages} pick={pickDoor} t={t}/>
    {viewer && port && <Viewer port={port} id={viewer.id} note={viewer.note} t={t} onClose={() => setViewer(null)}/>}
  </div>;
}

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent, type ReactNode } from 'react';
import { CaretLeft, CaretRight, Star } from '@phosphor-icons/react';
import { tr, useCompanionSettings, type L } from './companionSettings';
import { postRoute, useRoute, type Route } from './homeData';
import { askable, buildDay, dateTitle, report, shiftDate, within, type Block, type DayModel, type DayReply, type Row, type WeatherLook } from './dayView';
import { askOf, hasBody, openOf, type About, type Ask, type Rec } from './dayOpen';
import { AskDock, type DockState } from './AskDock';
import { DayBar, useBarState } from './DayBar';
import { numbers } from './barView';
import { Ic, SKIES } from './dayIcons';
import { EASE, OpenBody, reduced, Unfold, Viewer, type Acts } from './RowOpen';
import { TimelineGuard } from './TimelineGuard';
import './day-timeline.css';
import './day-open.css';

// The timeline pane (ADR 0296): the drop pane's other content, grown from the notch's right side. One story top to bottom: the date,
// her sentence about the day, one bar for the whole day (DayBar.tsx) with the day's numbers under it, and the day itself as a list; on
// today a now line, with what is still to come below it. A row opens in place (RowOpen.tsx) and can be asked about (AskDock.tsx).
// `feed` is today as the wing already reads it; other days are read here.
const rgb = (c: readonly number[]) => c.join(' ');
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
// A day change is a shared axis: the day that was slides away the way time came from, fading (80 ms), and the day that is slides in from the
// other side (140 ms). Never both at once, so two days' words are never on top of each other. The list moves 16 px, the rest of the pane 8.
const parts = (root: HTMLElement | null) => [...(root?.children ?? [])].filter((el): el is HTMLElement => el instanceof HTMLElement && !el.classList.contains('dt-head'));
const reach = (el: HTMLElement) => el.classList.contains('dt-list') ? 16 : 8;
const leave = (root: HTMLElement | null, dir: number) => parts(root).map(el => el.animate([{ opacity: 0, transform: `translateX(${-dir * reach(el)}px)` }], { duration: 80, easing: 'ease-in', fill: 'forwards' }));
const enter = (root: HTMLElement | null, dir: number) => parts(root).forEach(el => el.animate(reduced.matches ? [{ opacity: 0 }, {}] : [{ opacity: 0, transform: `translateX(${dir * reach(el)}px)` }, {}], { duration: reduced.matches ? 120 : 140, easing: EASE, fill: 'backwards' }));

// What the rows and the stays' headers share: which one is open, what a click on one does, what an open one holds, which can be asked about,
// and the name being changed in a stay's header.
type Cx = {
  opened: string | null; click: (e: MouseEvent, key: string) => void; body: (key: string) => ReactNode; tick: () => void; fit: (key: string, grow: number) => void;
  asks: ReadonlySet<string>; opens: ReadonlySet<string>; ask: ((key: string) => void) | null;
  rename: { key: string; draft: string; busy: boolean; set: (text: string) => void; save: () => void; field: (on: boolean) => void } | null;
};
// hot: what the pointer is on; pick: what the keys chose. Both are bracketed on the bar; the pointer's wins while it is on something.
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
// 问她 on a row's hover: it takes the place of the right column while the row is lit (and never opens the row).
function AskButton({ keyed, cx, t }: { keyed: string; cx: Cx; t: (l: L) => string }) {
  return cx.ask && cx.asks.has(keyed) ? <button type="button" className="dt-ask" tabIndex={-1} onClick={e => { e.stopPropagation(); cx.ask!(keyed); }}>{t(['Ask her', '问她'])}</button> : null;
}

// A row: the time it began, its glyph (a lit star on it when it mattered), its words (an amber badge when it waits on him), and at the
// right its length, how long until it, or its value. The glyphs of one stay or one trip hang on a rail. A click opens it in place.
// The pointer lights a row by moving on it, not by arriving: a list that scrolls under a resting pointer (the keys, the wheel) leaves what the keys chose alone.
function RowView({ row, hot, onHot, pick, t, how, cx }: { row: Row; how: string } & Hover) {
  const open = cx.opened === row.key, badge = useFade(row.wait), plain = !cx.opens.has(row.key);
  return <div className={`dt-row${row.detail ? ' has-detail' : ''}${row.future ? ' is-future' : ''}${row.connector ? ' is-leg' : ''}${how}${hot === row.key ? ' is-hot' : ''}${pick === row.key ? ' is-pick' : ''}${open ? ' is-open' : ''}${plain ? ' is-still' : ''}`} data-key={row.key} data-kind={row.kind} data-lk={row.lens ?? undefined}
    aria-current={pick === row.key ? 'true' : undefined} aria-expanded={plain ? undefined : open} onPointerMove={() => onHot(row.key)} onPointerLeave={() => onHot(null)} onClick={e => cx.click(e, row.key)}>
    <time className="dt-at">{row.ago && <small>{row.ago}</small>}{row.at}</time>
    <i className="dt-ic">{row.icon === 'dot' ? <span className="dt-pip"/> : <Ic icon={row.icon}/>}{row.star && <Star size={10} weight="fill" className="dt-badge" aria-label="★"/>}</i>
    <span className="dt-tx"><span className="dt-ttl"><b>{row.title}</b>{badge && <i className={`dt-wait${row.wait ? '' : ' is-gone'}`}>{t(['Waiting', '等你'])}</i>}</span>{row.detail && <em>{row.detail}</em>}</span>
    <AskButton keyed={row.key} cx={cx} t={t}/>
    <span className={`dt-tm${row.value ? ' is-value' : ''}`}>{row.right}</span>
    <Unfold open={open} render={() => cx.body(row.key)} tick={cx.tick} onOpen={grow => cx.fit(row.key, grow)} onSettle={() => cx.fit(row.key, 0)}/>
  </div>;
}
// The name of a kept place being changed, in the stay's own header: the cursor is in it with the name selected; Enter saves, Esc lets go.
function RenameField({ draft, busy, set, save, field, t }: NonNullable<Cx['rename']> & { t: (l: L) => string }) {
  const box = useRef<HTMLInputElement>(null);
  useEffect(() => { box.current?.focus({ preventScroll: true }); box.current?.select(); }, []);
  return <input ref={box} className="dt-rename" type="text" autoComplete="off" spellCheck={false} aria-label={t(['Rename', '改名字'])} value={draft} readOnly={busy}
    onChange={e => set(e.target.value)} onFocus={() => field(true)} onBlur={() => field(false)} onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); save(); } }}/>;
}
// The day's weather after its date: a glyph, a word, the high and the low. A condition this does not know is just the temperatures.
function Forecast({ w }: { w: WeatherLook }) {
  const Glyph = w.sky ? SKIES[w.sky] : null;
  return <span className="dt-wx" data-sky={w.sky ?? undefined}>{Glyph && <Glyph size={14} aria-hidden="true"/>}{w.word && <span>{w.word}</span>}{w.temps && <em>{w.temps}</em>}{w.sun && <i className="dt-wx-tip" role="tooltip">{w.sun}</i>}</span>;
}
// The day's labels (all-day events) in the room the header leaves them: whole ones, and a +N for those that do not fit (never half a word). The room
// changes with the day (the pill that comes or goes), so it starts again from all of them when it does; the first label always stays, cut to fit if it must.
function Chips({ chips }: { chips: DayModel['chips'] }) {
  const box = useRef<HTMLSpanElement>(null), [drop, setDrop] = useState(0);
  const labels = chips.filter(c => !c.more), rest = chips.reduce((n, c) => n + (c.more ? Number(c.text.slice(1)) || 0 : 0), 0) + drop;
  useLayoutEffect(() => { if (drop < labels.length - 1 && [...box.current?.children ?? []].some(c => c.scrollWidth > c.clientWidth + 1)) setDrop(drop + 1); });
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
      const how = inside ? ` is-rail${i === end ? ' is-end' : ''}` : onWay(b) ? ` is-rail is-trip${onWay(prev) ? '' : ' is-top'}${onWay(next) ? '' : ' is-end'}` : '';
      return <RowView key={b.row.key} row={b.row} hot={hot} onHot={onHot} pick={pick} t={t} cx={cx} how={gap + how}/>;
    }
    const open = cx.opened === b.key, plain = !cx.opens.has(b.key);
    return <section key={b.key} className={`dt-stay${gap}`} style={{ '--c': rgb(b.head.rgb) } as CSSProperties}>
      <header className={`dt-sh${b.head.unnamed ? ' is-plain' : ''}${b.items.length ? ' has-rows' : ''}${hot === b.key ? ' is-hot' : ''}${pick === b.key ? ' is-pick' : ''}${open ? ' is-open' : ''}${plain ? ' is-still' : ''}`} data-key={b.key} aria-current={pick === b.key ? 'true' : undefined} aria-expanded={plain ? undefined : open}
        onPointerMove={() => onHot(b.key)} onPointerLeave={() => onHot(null)} onClick={e => cx.click(e, b.key)}>
        <span className="dt-at"/><i className="dt-dot"/>
        <span className="dt-tx"><span className="dt-ttl">{cx.rename?.key === b.key ? <RenameField {...cx.rename} t={t}/> : <b>{b.head.name}</b>}{b.head.star !== null && <Star size={10} weight="fill" className="dt-star" aria-label="★"/>}</span>{b.head.star && <em>{b.head.star}</em>}</span>
        <AskButton keyed={b.key} cx={cx} t={t}/>
        <span className="dt-tm">{b.head.when}<small> · {b.head.dur}</small></span>
        <Unfold open={open} render={() => cx.body(b.key)} tick={cx.tick} onOpen={grow => cx.fit(b.key, grow)} onSettle={() => cx.fit(b.key, 0)}/>
      </header>
      {b.items.length > 0 && <div className="dt-in"><Blocks blocks={b.items} hot={hot} onHot={onHot} pick={pick} t={t} cx={cx} inside/></div>}
    </section>;
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

type Renaming = { key: string; place: string; from: string; draft: string; state: 'edit' | 'busy' | 'failed' };
type Note = { place: string; from: string; change: string | null; state: 'done' | 'failed' };
type Docked = { ask: Ask | null; state: DockState; nonce: number };

export function DayTimeline({ port, feed, open, ask, hold }: { port: string | null; feed: Route<DayReply>; open: boolean; ask?: (text: string, about?: About) => Promise<void>; hold?: (on: boolean) => void }) {
  const [{ lang }] = useCompanionSettings(), t = (l: L) => tr(lang, l);
  // null is today, which the wing already reads; another day is read here.
  const [date, setDate] = useState<string | null>(null);
  const other = useRoute<DayReply>(port, `/inherent/day?date=${date ?? ''}`, open && date !== null, 60_000);
  const route = date === null ? feed : other;
  const reply = route.data && (date === null || route.data.date === date) ? route.data : null;
  // What is drawn is the last reply that came. While the next day is on its way it stays, dimmed, so the pane does not jump; when it has come
  // the day that was leaves and the new one enters. An update of the day already drawn is drawn at once.
  const [shown, setShown] = useState<DayReply | null>(reply), now = useMinute(open), root = useRef<HTMLDivElement>(null), entering = useRef(0), leaving = useRef<Animation[]>([]);
  useEffect(() => {
    if (!reply || reply === shown) return;
    const dir = shown && shown.date !== reply.date ? reply.date > shown.date ? 1 : -1 : 0;
    if (!dir || reduced.matches) { entering.current = dir; setShown(reply); return; }
    let live = true;
    leaving.current = leave(root.current, dir);
    void Promise.all(leaving.current.map(a => a.finished)).then(() => { if (live) { entering.current = dir; setShown(reply); } }, () => {});
    return () => { live = false; leaving.current.forEach(a => a.cancel()); leaving.current = []; };
  }, [reply]);
  useLayoutEffect(() => {
    const dir = entering.current;
    entering.current = 0;
    leaving.current.forEach(a => a.cancel()); leaving.current = [];
    if (dir) enter(root.current, dir);
  }, [shown]);
  useEffect(() => { feed.reload(); }, []);
  // What he has just done that the daemon has not said back yet, laid over what was read so the whole pane (the bar, the list, the count)
  // shows it at once while the day is read again: a reminder acknowledged, a kept place renamed.
  const [acks, setAcks] = useState<ReadonlySet<string>>(() => new Set()), [names, setNames] = useState<ReadonlyMap<string, string>>(() => new Map());
  const laid = useMemo(() => !shown || !acks.size && !names.size ? shown : { ...shown, items: shown.items.map(x => acks.has(x.id) ? { ...x, acknowledged: true }
    : x.kept_place && names.has(x.kept_place.id) ? { ...x, kept_place: { ...x.kept_place, name: names.get(x.kept_place.id)! } } : x) }, [shown, acks, names]);
  // A day that cannot be built (no usable bounds) says so in a line; a day that builds but fails to draw falls to the guards below.
  const built = useMemo(() => {
    if (!laid) return { model: null, failed: false };
    try { return { model: buildDay(laid, now, t), failed: false }; }
    catch (error) { report('the day', error); return { model: null, failed: true }; }
  }, [laid, now, lang]);
  const model = built.model;
  // The kinds the day has a number for: a lens that is not one of them (its chip is not there) lights nothing.
  const lensKinds = useMemo(() => new Set(model ? numbers(model, t).map(n => n.kind) : []), [model, lang]);
  const todayDate = feed.data?.date ?? null, at = date ?? todayDate;
  // What is drawn is not the day asked for: it is on its way, or (`lost`) it did not come and the day that is drawn is not to be left under the new date.
  const stale = !!shown && !!at && shown.date !== at, lost = stale && !reply && (route.failed || route.missing);
  const step = (days: number) => { if (!at) return; const next = shiftDate(at, days); setDate(next === todayDate ? null : next); };
  // A read that does not come says so after a few seconds; the daemon is asked again on its own.
  const [slow, setSlow] = useState(false);
  useEffect(() => { setSlow(false); if (reply) return; const timer = setTimeout(() => setSlow(true), 6000); return () => clearTimeout(timer); }, [!!reply, date]);

  const [hot, setHot] = useState<string | null>(null), [pick, setPick] = useState<string | null>(null), [opened, setOpened] = useState<string | null>(null), list = useRef<HTMLDivElement>(null), placed = useRef(''), ui = useBarState(), uiRef = useRef(ui);
  uiRef.current = ui;
  const edges = () => { const l = list.current; if (!l) return; l.classList.toggle('more-below', l.scrollTop + l.clientHeight < l.scrollHeight - 2); l.classList.toggle('more-above', l.scrollTop > 2); };
  // A day opens where it matters: today at its now line, another day from the top; so does the list cut to a range, and the whole day again.
  // The first time the list is there its rows in view come up.
  const rose = useRef(false), stopRise = useRef(() => {});
  useEffect(() => () => stopRise.current(), []);
  useLayoutEffect(() => {
    const l = list.current, spot = `${shown?.date}:${date}:${ui.filter ? 'cut' : ''}`;
    if (l && model && shown && placed.current !== spot) {
      placed.current = spot;
      const line = l.querySelector<HTMLElement>('.dt-now');
      l.scrollTop = line ? Math.max(0, offsetIn(line, l) - l.clientHeight * .6) : 0;
      if (!rose.current) { rose.current = true; stopRise.current = rise(l); }
    }
    edges();
  });
  // What the day had when it was last drawn: an item that was not there then, on the same day, grows in (a new stay with all that is in it as one).
  const seen = useRef<{ date: string; keys: Set<string> } | null>(null);
  useLayoutEffect(() => {
    if (!model) return;
    const keys = new Set(model.index.map(x => x.key)), before = seen.current, l = list.current;
    seen.current = { date: model.reply.date, keys };
    if (!before || before.date !== model.reply.date || !l) return;
    const fresh = [...keys].filter(k => !before.keys.has(k)).flatMap(k => { const el = l.querySelector<HTMLElement>(`[data-key="${CSS.escape(k)}"]`); return el ? [el.classList.contains('dt-sh') ? el.parentElement! : el] : []; });
    fresh.filter(el => !fresh.some(o => o !== el && o.contains(el))).forEach(grow);
  }, [model]);
  // The list scrolls to `to` in `ms`, easing out (at once under reduced motion); a new one takes over from wherever the last had got to.
  // `more`: how much taller the list is about to be (a row that is opening), so the end is not cut to what is there now.
  const glide = useRef(0);
  const slide = (to: number, ms: number, more = 0) => {
    const l = list.current;
    if (!l) return;
    const from = l.scrollTop, end = Math.max(0, Math.min(l.scrollHeight + more - l.clientHeight, to)), began = performance.now();
    cancelAnimationFrame(glide.current);
    if (reduced.matches || Math.abs(end - from) < 1) { l.scrollTop = end; return; }
    const tick = (at: number) => { const k = Math.min(1, (at - began) / ms); l.scrollTop = from + (end - from) * (1 - (1 - k) ** 3); if (k < 1) glide.current = requestAnimationFrame(tick); };
    glide.current = requestAnimationFrame(tick);
  };
  // A click on the bar, or on a piece of it, takes the row there to the top of the list in 300 ms, and the row flashes.
  const jump = (key: string) => {
    const l = list.current, el = l?.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"]`);
    if (!l || !el) return;
    el.classList.remove('is-flash'); void el.offsetWidth; el.classList.add('is-flash');
    slide(offsetIn(el, l) - 12, 300);
  };
  // A row that opens is kept whole in view: if it would end under the list's foot the list moves to bring it up (its top stays in view when it is
  // taller than the list). It is asked once as the row starts to grow (with how much it will; a row that is closing above it gives its height back
  // meanwhile) and once more when it has finished, and again when what it holds changes.
  const fit = (key: string, more: number) => {
    const l = list.current, el = l?.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"]`);
    if (!l || !el) return;
    const mine = el.querySelector(':scope > .dt-fold'), closing = more > 0 ? [...l.querySelectorAll<HTMLElement>('.dt-fold')].filter(f => f !== mine && !el.contains(f) && el.compareDocumentPosition(f) & Node.DOCUMENT_POSITION_PRECEDING).reduce((n, f) => n + f.offsetHeight, 0) : 0;
    const top = offsetIn(el, l) - closing, full = el.offsetHeight + more, margin = 24;
    if (top < l.scrollTop + margin) slide(top - margin, 260, more);
    else if (top + full > l.scrollTop + l.clientHeight - margin) slide(Math.min(top - margin, top + full - l.clientHeight + margin), 260, more);
  };
  // The waiting pill takes the first row that waits on him to the top of the list (the whole day again when the list is cut).
  const waitFirst = (key: string) => { if (ui.filter) { ui.setFilter(null); requestAnimationFrame(() => jump(key)); } else jump(key); };
  // A tap on the bar goes to its row; a row the cut has left out is not there to go to, so the whole day comes back first.
  const reveal = (key: string) => { if (ui.filter && !list.current?.querySelector(`[data-key="${CSS.escape(key)}"]`)) { ui.setFilter(null); requestAnimationFrame(() => jump(key)); } else jump(key); };

  // ---- opening a row, and what can be done from it (RowOpen.tsx, AskDock.tsx) ----
  // Which rows can be asked about: those whose kind and id the brain takes (dayOpen.askOf says what is handed over).
  const asks = useMemo<ReadonlySet<string>>(() => new Set((model?.items ?? []).filter(x => askable(x.kind, x.id)).map(x => x.id)), [model]);
  const canAsk = (key: string) => !!ask && asks.has(key);
  // Which rows open to something (a part, or a button that works): a row never opens to nothing, and one that does not is drawn as it is.
  const opens = useMemo<ReadonlySet<string>>(() => new Set(model ? [...model.rows.keys(), ...model.heads.keys()].filter(k => { const o = openOf(model, k, t); return !!o && hasBody(o, !!ask && asks.has(k)); }) : []), [model, asks, !!ask]);
  const toggle = (key: string) => { if (opened === key) setOpened(null); else if (opens.has(key)) setOpened(key); };
  const click = (e: MouseEvent, key: string) => {
    if ((e.target as Element).closest('.dt-fold, button, input, a')) return;
    setPick(key); toggle(key);
  };
  // After a change the day is read again at once (today's too when it is another day that is open, for the wing's line).
  const again = () => { route.reload(); if (route !== feed) feed.reload(); };
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
      const out = await postRoute(port, `/inherent/memory/places/${encodeURIComponent(r.place)}`, { name }) as { change?: { id?: unknown } | null };
      setNames(m => new Map(m).set(r.place, name)); setNote({ place: r.place, from: r.from, change: typeof out.change?.id === 'string' ? out.change.id : null, state: 'done' }); setRename(null); again();
    } catch { setRename(x => x && { ...x, state: 'failed' }); }
  };
  const undo = async () => {
    const n = note;
    if (!n?.change || !port) return;
    try { await postRoute(port, '/inherent/memory/places/undo', { change_id: n.change }); setNames(m => new Map(m).set(n.place, n.from)); setNote(null); again(); }
    catch { setNote({ ...n, state: 'failed' }); }
  };
  useEffect(() => { if (!note) return; const timer = setTimeout(() => setNote(null), 5000); return () => clearTimeout(timer); }, [note]);
  // 问她: an input slides up at the pane's foot with what it is about as a chip; Enter hands the turn over; 已发给她 for 1.5 s and it slides away.
  const [dock, setDock] = useState<Docked | null>(null), [draft, setDraft] = useState(''), lastDock = useRef(dock);
  if (dock) lastDock.current = dock;
  const openAsk = (key: string) => {
    const a = model && ask ? askOf(model, key, t) : null;
    if (a) { setLeft(false); setDock(d => ({ ask: a, state: d?.state === 'busy' ? 'busy' : 'idle', nonce: (d?.nonce ?? 0) + 1 })); }
  };
  const sendAsk = async () => {
    const d = dock, text = draft.trim();
    if (!d || !text || d.state === 'busy' || !ask) return;
    setDock({ ...d, state: 'busy' });
    try { await ask(text, d.ask?.about); setDraft(''); setDock(x => x && { ...x, state: 'sent' }); }
    catch { setDock(x => x && { ...x, state: 'failed' }); }
  };
  useEffect(() => { if (dock?.state !== 'sent') return; const timer = setTimeout(() => setDock(null), 1500); return () => clearTimeout(timer); }, [dock?.state]);
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
  const [left, setLeft] = useState(false);
  const holding = !!viewer || (!!dock || !!rename) && !left;
  useEffect(() => { hold?.(holding); return () => hold?.(false); }, [holding]);
  // The open row, the field, the input and the picture are the day's: they go with it, and with the pane.
  useEffect(() => { setPick(null); setHot(null); setOpened(null); }, [date, ui.filter]);
  useEffect(() => { if (opened && model && !model.rows.has(opened) && !model.heads.has(opened)) setOpened(null); }, [model]);
  useEffect(() => { if (rename && opened !== rename.key) setRename(null); }, [opened]);
  useEffect(() => { if (!open) { setOpened(null); setRename(null); setDock(null); setDraft(''); setViewer(null); setNote(null); } }, [open]);
  // The body of an open row: what dayOpen made of it, and the buttons that work for it, each with what it is doing.
  const body = (key: string): ReactNode => {
    const o = model && openOf(model, key, t);
    if (!o) return null;
    const sum = work[`sum:${key}`] as 'busy' | 'sent' | 'failed' | undefined, acking = o.ack ? work[`ack:${o.ack}`] : '';
    const acts: Acts = {
      port, t, records: readDay, picture: (id, text) => setViewer({ id, note: text }),
      ask: canAsk(key) ? () => openAsk(key) : null,
      rename: o.rename ? { state: rename?.key === key ? rename.state === 'failed' ? 'failed' : 'editing' : 'idle', start: () => startRename(key) } : null,
      ack: o.ack ? { state: acking === 'busy' ? 'busy' : acking === 'failed' ? 'failed' : 'idle', press: () => void acknowledge(o.ack!) } : null,
      summarize: o.summarize && canAsk(key) ? { state: sum ?? 'idle', press: () => void summarize(key) } : null,
    };
    return <OpenBody open={o} acts={acts}/>;
  };
  const cx: Cx = { opened, click, body, tick: edges, fit, asks, opens, ask: ask ? openAsk : null,
    rename: rename && { key: rename.key, draft: rename.draft, busy: rename.state === 'busy', set: text => setRename(r => r && { ...r, draft: text, state: r.state === 'failed' ? 'edit' : r.state }), save: () => void saveRename(), field: on => setLeft(!on) } };

  // Esc lets go of one thing at a time, nearest first: the picture, the input, the name being changed, the open row, the bar's lens, its range and
  // the list's cut to a range, and then the pane itself (the pane's own Esc listener was added after this one, so this one answers first). A folded
  // pane, which stays mounted, answers nothing.
  const back = useRef<() => boolean>(() => false);
  back.current = () => {
    if (!open) return false;
    if (viewer) setViewer(null);
    else if (dock) { setDock(null); setDraft(''); }
    else if (rename) setRename(null);
    else if (opened) setOpened(null);
    else return uiRef.current.escape();
    return true;
  };
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape' && !e.isComposing && back.current()) { e.preventDefault(); e.stopImmediatePropagation(); } };
    window.addEventListener('keydown', key, true);
    return () => { window.removeEventListener('keydown', key, true); cancelAnimationFrame(glide.current); };
  }, []);
  useEffect(() => { ui.setSel(null); ui.setFilter(null); ui.setPreview(null); }, [date]);

  // The keys of an open day (the window has key focus only after a click pinned it: Notch.tsx): ← → the day before and after, ↑ ↓ the row
  // before and after, which the bar brackets and the list keeps in view, Enter or Space opens the one chosen (or closes it). With nothing
  // chosen they start from where he is looking: ↓ at the first row under the now line (else the top of the list), ↑ at the last row above it
  // (else the bottom).
  const go = (days: number) => { if (at && !(days > 0 && date === null)) step(days); };
  const walk = (dir: 1 | -1) => {
    const l = list.current, rows = l ? [...l.querySelectorAll<HTMLElement>('[data-key]')] : [];
    if (!l || !rows.length) return;
    let next = pick ? rows.findIndex(r => r.dataset.key === pick) : -1;
    if (next >= 0) next = Math.max(0, Math.min(rows.length - 1, next + dir));
    else {
      const view = l.getBoundingClientRect(), line = l.querySelector('.dt-now')?.getBoundingClientRect(), seen = !!line && line.top >= view.top && line.bottom <= view.bottom;
      const from = dir > 0 ? seen ? line!.bottom : view.top : seen ? line!.top : view.bottom;
      next = dir > 0 ? rows.findIndex(r => r.getBoundingClientRect().top >= from - 1) : rows.reduce((last, r, i) => r.getBoundingClientRect().bottom <= from + 1 ? i : last, -1);
      if (next < 0) next = dir > 0 ? rows.length - 1 : 0;
    }
    const el = rows[next], top = offsetIn(el, l), margin = 24;
    setPick(el.dataset.key!); setHot(null);
    if (top < l.scrollTop + margin) slide(top - margin, 180);
    else if (top + el.offsetHeight > l.scrollTop + l.clientHeight - margin) slide(top + el.offsetHeight - l.clientHeight + margin, 180);
  };
  const keys = useRef({ open, go, walk, pick, toggle });
  keys.current = { open, go, walk, pick, toggle };
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      const k = keys.current, target = e.target as Element | null;
      if (!k.open || e.isComposing || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey || target?.closest?.('input,textarea,select,[contenteditable]')) return;
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { if (!e.repeat) k.go(e.key === 'ArrowLeft' ? -1 : 1); }
      else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') k.walk(e.key === 'ArrowDown' ? 1 : -1);
      else if ((e.key === 'Enter' || e.key === ' ') && k.pick && !target?.closest?.('button,a')) { if (!e.repeat) k.toggle(k.pick); }
      else return;
      e.preventDefault(); e.stopPropagation();
    };
    window.addEventListener('keydown', key, true);
    return () => window.removeEventListener('keydown', key, true);
  }, []);
  const rows = model && ui.filter ? within(model.blocks, model.span, Math.min(ui.filter.a, ui.filter.b), Math.max(ui.filter.a, ui.filter.b)) : model?.blocks ?? [];

  const title = at ? dateTitle(at, t) : model?.title ?? '';
  const say = !port ? t(['Not connected to Jarvis yet', '还没连上 Jarvis']) : route.missing ? t(["This Jarvis can't give a day yet", '这台 Jarvis 还不会给出一天']) : slow || route.failed ? t(["Couldn't read this day. Trying again.", '没读到这一天，会再试一次']) : t(['Reading the day…', '读着这一天…']);
  const kept = note ?? lastNote.current, shownDock = dock ?? lastDock.current;
  return <div ref={root} className={`dt${stale && !lost ? ' is-loading' : ''}${open ? '' : ' is-folded'}`} data-lens={ui.lens && lensKinds.has(ui.lens) ? ui.lens : undefined} role="region" aria-label={t(['Day timeline', '一天的时间线'])}>
    <div className="dt-head">
      <b className="dt-date">{title}</b>{date === null ? <em className="dt-today">{t(['Today', '今天'])}</em> : <button type="button" className="dt-back" onClick={() => setDate(null)}>{t(['Back to today', '回到今天'])}</button>}
      <TimelineGuard where="the header" reset={shown} fallback={null}>
        {!lost && model?.weather && <Forecast w={model.weather}/>}
        {!lost && model && <Chips key={model.chips.map(c => c.text).join('|')} chips={model.chips}/>}
        <Waits n={lost ? 0 : model?.waiting.length ?? 0} first={() => { const k = model?.waiting[0]; if (k) waitFirst(k); }} t={t}/>
      </TimelineGuard>
      <span className="dt-nav">
        <button type="button" aria-label={t(['The day before', '前一天'])} disabled={!at} onClick={() => step(-1)}><CaretLeft size={13}/></button>
        <button type="button" aria-label={t(['The day after', '后一天'])} disabled={!at || date === null} onClick={() => step(1)}><CaretRight size={13}/></button>
      </span>
    </div>
    {built.failed ? <p className="dt-say">{t(["Couldn't draw this day", '这一天没画出来'])}</p> : !model || lost ? <p className="dt-say">{say}</p> : <TimelineGuard where="the pane" reset={shown} fallback={<p className="dt-say">{t(["Couldn't draw this day", '这一天没画出来'])}</p>}>
      {model.line && <p className={`dt-line${model.line.draft ? ' is-draft' : ''}`}>{model.line.text}</p>}
      {model.empty ? <p className="dt-say">{t(['Nothing recorded for this day yet', '这一天还没有记下什么'])}</p> : <>
        <DayBar model={model} port={port} ui={ui} hot={hot ?? pick ?? opened} onHot={setHot} onJump={reveal}/>
        {rows.length > 0 ? <div ref={list} className="dt-list" onScroll={edges} onWheel={() => cancelAnimationFrame(glide.current)}><Blocks blocks={rows} hot={hot} onHot={setHot} pick={pick} t={t} cx={cx}/></div>
          : ui.filter && <p className="dt-say">{t(['Nothing was recorded in this stretch', '这一段没有记下什么'])}</p>}
      </>}
      {model.foot && <p className="dt-foot">{model.foot}</p>}
      <Unfold className="dt-fold is-dock" open={!!note} tick={edges} render={() => kept && <div className="dt-note" role="status"><span>{kept.state === 'failed' ? t(["Couldn't undo it", '没撤销成功']) : t(['Renamed', '改好了'])}</span>
        {kept.state === 'done' && kept.change && <>{' · '}<button type="button" onClick={() => void undo()}>{t(['Undo', '撤销'])}</button></>}</div>}/>
      <Unfold className="dt-fold is-dock" open={!!dock} tick={edges} render={() => shownDock && <AskDock about={shownDock.ask} draft={draft} state={shownDock.state} nonce={shownDock.nonce} onDraft={text => { setDraft(text); setDock(d => d && d.state === 'failed' ? { ...d, state: 'idle' } : d); }}
        onSend={() => void sendAsk()} onDrop={() => setDock(d => d && { ...d, ask: null })} onField={on => setLeft(!on)} t={t}/>}/>
    </TimelineGuard>}
    {viewer && port && <Viewer port={port} id={viewer.id} note={viewer.note} t={t} onClose={() => setViewer(null)}/>}
  </div>;
}

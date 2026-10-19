import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { CaretLeft, CaretRight, Star } from '@phosphor-icons/react';
import { tr, useCompanionSettings, type L } from './companionSettings';
import { useRoute, type Route } from './homeData';
import { askable, buildDay, dateTitle, report, shiftDate, within, type Block, type DayModel, type DayReply, type Row, type WeatherLook } from './dayView';
import { DayBar, useBarState } from './DayBar';
import { Ic, SKIES } from './dayIcons';
import { TimelineGuard } from './TimelineGuard';
import './day-timeline.css';

// The timeline pane (ADR 0296): the drop pane's other content, grown from the notch's right side. One story top to bottom: the date,
// her sentence about the day, one bar for the whole day (DayBar.tsx) with the day's numbers under it, and the day itself as a list; on
// today a now line, with what is still to come below it. `feed` is today as the wing already reads it; other days are read here.
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const rgb = (c: readonly number[]) => c.join(' ');
// Where `el` is inside the scrolling `list`.
const offsetIn = (el: HTMLElement, list: HTMLElement) => el.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop;

// Motion (reduced: nothing moves, what appears fades in over 120 ms). These are Web Animations on the elements themselves: React keeps the
// classes, and a row that is hovered while it comes up is not interrupted.
const EASE = 'cubic-bezier(.16, 1, .3, 1)';
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

// Handing a row to her is B2's: the button is written and waits behind this.
const ASK_HER = false;
// hot: what the pointer is on; pick: what the keys chose. Both are bracketed on the bar; the pointer's wins while it is on something.
type Hover = { hot: string | null; onHot: (key: string | null) => void; pick: string | null; t: (l: L) => string };

// A row: the time it began, its glyph (a lit star on it when it mattered), its words (an amber badge when it waits on him), and at the
// right its length, how long until it, or its value. The glyphs of one stay or one trip hang on a rail.
// The pointer lights a row by moving on it, not by arriving: a list that scrolls under a resting pointer (the keys, the wheel) leaves what the keys chose alone.
function RowView({ row, hot, onHot, pick, t, how }: { row: Row; how: string } & Hover) {
  return <div className={`dt-row${row.detail ? ' has-detail' : ''}${row.future ? ' is-future' : ''}${row.connector ? ' is-leg' : ''}${how}${hot === row.key ? ' is-hot' : ''}${pick === row.key ? ' is-pick' : ''}`} data-key={row.key} data-kind={row.kind} data-lk={row.lens ?? undefined}
    aria-current={pick === row.key ? 'true' : undefined} onPointerMove={() => onHot(row.key)} onPointerLeave={() => onHot(null)}>
    <time className="dt-at">{row.ago && <small>{row.ago}</small>}{row.at}</time>
    <i className="dt-ic">{row.icon === 'dot' ? <span className="dt-pip"/> : <Ic icon={row.icon}/>}{row.star && <Star size={10} weight="fill" className="dt-badge" aria-label="★"/>}</i>
    <span className="dt-tx"><span className="dt-ttl"><b>{row.title}</b>{row.wait && <i className="dt-wait">{t(['Waiting', '等你'])}</i>}</span>{row.detail && <em>{row.detail}</em>}</span>
    {ASK_HER && askable(row.kind, row.key) && <button type="button" className="dt-ask" tabIndex={-1}>{t(['Ask her', '问她'])}</button>}
    <span className={`dt-tm${row.value ? ' is-value' : ''}`}>{row.right}</span>
  </div>;
}
// The day's weather after its date: a glyph, a word, the high and the low. A condition this does not know is just the temperatures.
function Forecast({ w }: { w: WeatherLook }) {
  const Glyph = w.sky ? SKIES[w.sky] : null;
  return <span className="dt-wx" data-sky={w.sky ?? undefined}>{Glyph && <Glyph size={14} aria-hidden="true"/>}{w.word && <span>{w.word}</span>}{w.temps && <em>{w.temps}</em>}</span>;
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
const onWay = (b?: Block) => b?.type === 'row' && (b.row.connector || b.row.onWay);
// The blocks of one level, told apart in units: a stay with what is in it, a trip (the ways he went and what happened on them), and the
// rest. A unit starts 12 px under the one before it; the now line stays with the unit it follows. A trip's rail is dashed, from its first
// glyph to its last; what is in a stay hangs on that stay's own, from its dot to the last row.
function Blocks({ blocks, hot, onHot, pick, t, inside }: { blocks: Block[]; inside?: boolean } & Hover) {
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
      return <RowView key={b.row.key} row={b.row} hot={hot} onHot={onHot} pick={pick} t={t} how={gap + how}/>;
    }
    return <section key={b.key} className={`dt-stay${gap}`} style={{ '--c': rgb(b.head.rgb) } as CSSProperties}>
      <header className={`dt-sh${b.head.unnamed ? ' is-plain' : ''}${b.items.length ? ' has-rows' : ''}${hot === b.key ? ' is-hot' : ''}${pick === b.key ? ' is-pick' : ''}`} data-key={b.key} aria-current={pick === b.key ? 'true' : undefined} onPointerMove={() => onHot(b.key)} onPointerLeave={() => onHot(null)}>
        <span className="dt-at"/><i className="dt-dot"/>
        <span className="dt-tx"><span className="dt-ttl"><b>{b.head.name}</b>{b.head.star !== null && <Star size={10} weight="fill" className="dt-star" aria-label="★"/>}</span>{b.head.star && <em>{b.head.star}</em>}</span>
        <span className="dt-tm">{b.head.when}<small> · {b.head.dur}</small></span>
      </header>
      {b.items.length > 0 && <div className="dt-in"><Blocks blocks={b.items} hot={hot} onHot={onHot} pick={pick} t={t} inside/></div>}
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

export function DayTimeline({ port, feed, open }: { port: string | null; feed: Route<DayReply>; open: boolean }) {
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
  // A day that cannot be built (no usable bounds) says so in a line; a day that builds but fails to draw falls to the guards below.
  const built = useMemo(() => {
    if (!shown) return { model: null, failed: false };
    try { return { model: buildDay(shown, now, t), failed: false }; }
    catch (error) { report('the day', error); return { model: null, failed: true }; }
  }, [shown, now, lang]);
  const model = built.model;
  const todayDate = feed.data?.date ?? null, at = date ?? todayDate;
  const step = (days: number) => { if (!at) return; const next = shiftDate(at, days); setDate(next === todayDate ? null : next); };
  // A read that does not come says so after a few seconds; the daemon is asked again on its own.
  const [slow, setSlow] = useState(false);
  useEffect(() => { setSlow(false); if (reply) return; const timer = setTimeout(() => setSlow(true), 6000); return () => clearTimeout(timer); }, [!!reply, date]);

  const [hot, setHot] = useState<string | null>(null), [pick, setPick] = useState<string | null>(null), list = useRef<HTMLDivElement>(null), placed = useRef(''), ui = useBarState(), uiRef = useRef(ui);
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
  // A click on the bar, or on a piece of it, takes the row there to the top of the list in 300 ms, and the row flashes.
  const jump = (key: string) => {
    const l = list.current, el = l?.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"]`);
    if (!l || !el) return;
    el.classList.remove('is-flash'); void el.offsetWidth; el.classList.add('is-flash');
    slide(offsetIn(el, l) - 12, 300);
  };
  // The waiting pill takes the first row that waits on him to the top of the list (the whole day again when the list is cut).
  const waitFirst = (key: string) => { if (ui.filter) { ui.setFilter(null); requestAnimationFrame(() => jump(key)); } else jump(key); };
  // The bar's lens, its range and the list's cut to a range: Esc lets go of one at a time before the pane itself closes (the pane's own
  // Esc listener was added after this one, so this one answers first).
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape' && !e.isComposing && uiRef.current.escape()) { e.preventDefault(); e.stopImmediatePropagation(); } };
    window.addEventListener('keydown', key, true);
    return () => { window.removeEventListener('keydown', key, true); cancelAnimationFrame(glide.current); };
  }, []);
  useEffect(() => { ui.setSel(null); ui.setFilter(null); }, [date]);
  // What the keys chose goes with the day, and with a cut that may have taken its row out of the list.
  useEffect(() => { setPick(null); setHot(null); }, [date, ui.filter]);

  // The keys of an open day (the window has key focus only after a click pinned it: Notch.tsx): ← → the day before and after, ↑ ↓ the row
  // before and after, which the bar brackets and the list keeps in view. With nothing chosen they start from where he is looking: ↓ at the
  // first row under the now line (else the top of the list), ↑ at the last row above it (else the bottom).
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
  const keys = useRef({ open, go, walk });
  keys.current = { open, go, walk };
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      const k = keys.current;
      if (!k.open || e.isComposing || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey || (e.target as Element | null)?.closest?.('input,textarea,select,[contenteditable]')) return;
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { if (!e.repeat) k.go(e.key === 'ArrowLeft' ? -1 : 1); }
      else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') k.walk(e.key === 'ArrowDown' ? 1 : -1);
      else return;
      e.preventDefault(); e.stopPropagation();
    };
    window.addEventListener('keydown', key, true);
    return () => window.removeEventListener('keydown', key, true);
  }, []);
  const rows = model && ui.filter ? within(model.blocks, model.span, Math.min(ui.filter.a, ui.filter.b), Math.max(ui.filter.a, ui.filter.b)) : model?.blocks ?? [];

  const title = at ? dateTitle(at, t) : model?.title ?? '';
  const say = !port ? t(['Not connected to Jarvis yet', '还没连上 Jarvis']) : route.missing ? t(["This Jarvis can't give a day yet", '这台 Jarvis 还不会给出一天']) : slow || route.failed ? t(["Couldn't read this day. Trying again.", '没读到这一天，会再试一次']) : t(['Reading the day…', '读着这一天…']);
  return <div ref={root} className={`dt${shown && at && shown.date !== at ? ' is-loading' : ''}`} data-lens={ui.lens ?? undefined} role="region" aria-label={t(['Day timeline', '一天的时间线'])}>
    <div className="dt-head">
      <b className="dt-date">{title}</b>{date === null ? <em className="dt-today">{t(['Today', '今天'])}</em> : <button type="button" className="dt-back" onClick={() => setDate(null)}>{t(['Back to today', '回到今天'])}</button>}
      <TimelineGuard where="the header" reset={shown} fallback={null}>
        {model?.weather && <Forecast w={model.weather}/>}
        {model && <Chips key={model.chips.map(c => c.text).join('|')} chips={model.chips}/>}
        {model && model.waiting.length > 0 && <button type="button" className="dt-waits" onClick={() => waitFirst(model.waiting[0])}>{t([`${model.waiting.length} waiting`, `${model.waiting.length} 件等你`])}</button>}
      </TimelineGuard>
      <span className="dt-nav">
        <button type="button" aria-label={t(['The day before', '前一天'])} disabled={!at} onClick={() => step(-1)}><CaretLeft size={13}/></button>
        <button type="button" aria-label={t(['The day after', '后一天'])} disabled={!at || date === null} onClick={() => step(1)}><CaretRight size={13}/></button>
      </span>
    </div>
    {built.failed ? <p className="dt-say">{t(["Couldn't draw this day", '这一天没画出来'])}</p> : !model ? <p className="dt-say">{say}</p> : <TimelineGuard where="the pane" reset={shown} fallback={<p className="dt-say">{t(["Couldn't draw this day", '这一天没画出来'])}</p>}>
      {model.line && <p className={`dt-line${model.line.draft ? ' is-draft' : ''}`}>{model.line.text}</p>}
      {model.empty ? <p className="dt-say">{t(['Nothing recorded for this day yet', '这一天还没有记下什么'])}</p> : <>
        <DayBar model={model} port={port} ui={ui} hot={hot ?? pick} onHot={setHot} onJump={jump}/>
        {rows.length > 0 ? <div ref={list} className="dt-list" onScroll={edges} onWheel={() => cancelAnimationFrame(glide.current)}><Blocks blocks={rows} hot={hot} onHot={setHot} pick={pick} t={t}/></div>
          : ui.filter && <p className="dt-say">{t(['Nothing was recorded in this stretch', '这一段没有记下什么'])}</p>}
      </>}
      {model.foot && <p className="dt-foot">{model.foot}</p>}
    </TimelineGuard>}
  </div>;
}

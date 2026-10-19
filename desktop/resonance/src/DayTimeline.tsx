import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { CaretLeft, CaretRight, Star } from '@phosphor-icons/react';
import { tr, useCompanionSettings, type L } from './companionSettings';
import { useNow, useRoute, type Route } from './homeData';
import { buildDay, dateTitle, shiftDate, within, type Block, type DayReply, type Row } from './dayView';
import { DayBar, useBarState } from './DayBar';
import { Ic } from './dayIcons';
import './day-timeline.css';

// The timeline pane (ADR 0296): the drop pane's other content, grown from the notch's right side. One story top to bottom: the date,
// her sentence about the day, one bar for the whole day (DayBar.tsx) with the day's numbers under it, and the day itself as a list; on
// today a now line, with what is still to come below it. `feed` is today as the wing already reads it; other days are read here.
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const rgb = (c: readonly number[]) => c.join(' ');
// Where `el` is inside the scrolling `list`.
const offsetIn = (el: HTMLElement, list: HTMLElement) => el.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop;

type Hover = { hot: string | null; onHot: (key: string | null) => void };

function RowView({ row, hot, onHot }: { row: Row } & Hover) {
  return <div className={`dt-row${row.future ? ' is-future' : ''}${row.connector ? ' is-way' : ''}${hot === row.key ? ' is-hot' : ''}`} data-key={row.key} data-kind={row.kind} data-lk={row.lens ?? undefined} onPointerEnter={() => onHot(row.key)} onPointerLeave={() => onHot(null)}>
    <i className="dt-ic"><Ic icon={row.icon}/></i>
    <span className="dt-tx"><b>{row.title}{row.star && <Star size={10} weight="fill" className="dt-star" aria-label="★"/>}</b>{row.detail && <em>{row.detail}</em>}</span>
    <span className="dt-tm">{row.when}{row.dur && <small>{row.dur}</small>}</span>
  </div>;
}
function Blocks({ blocks, hot, onHot }: { blocks: Block[] } & Hover) {
  return <>{blocks.map(b => b.type === 'row' ? <RowView key={b.row.key} row={b.row} hot={hot} onHot={onHot}/>
    : b.type === 'now' ? <div key={b.key} className="dt-now"><span>{b.label}</span></div>
    : <section key={b.key} className="dt-stay" style={{ '--c': rgb(b.head.rgb) } as CSSProperties}>
      <header className={`dt-sh${b.head.unnamed ? ' is-plain' : ''}${hot === b.key ? ' is-hot' : ''}`} data-key={b.key} onPointerEnter={() => onHot(b.key)} onPointerLeave={() => onHot(null)}>
        <i className="dt-dot"/><span className="dt-tx"><b>{b.head.name}{b.head.star !== null && <Star size={10} weight="fill" className="dt-star" aria-label="★"/>}</b>{b.head.star && <em>{b.head.star}</em>}</span>
        <span className="dt-tm">{b.head.when}<small>{b.head.dur}</small></span>
      </header>
      {b.items.length > 0 && <div className="dt-in"><Blocks blocks={b.items} hot={hot} onHot={onHot}/></div>}
    </section>)}</>;
}

export function DayTimeline({ port, feed, open }: { port: string | null; feed: Route<DayReply>; open: boolean }) {
  const [{ lang }] = useCompanionSettings(), t = (l: L) => tr(lang, l);
  // null is today, which the wing already reads; another day is read here.
  const [date, setDate] = useState<string | null>(null);
  const other = useRoute<DayReply>(port, `/inherent/day?date=${date ?? ''}`, open && date !== null, 60_000);
  const route = date === null ? feed : other;
  const reply = route.data && (date === null || route.data.date === date) ? route.data : null;
  // While the next day is on its way the last one stays, dimmed, so the pane does not jump.
  const last = useRef<DayReply | null>(null);
  if (reply) last.current = reply;
  const shown = reply ?? last.current, now = useNow(open ? 30_000 : 3_600_000);
  useEffect(() => { feed.reload(); }, []);
  const model = useMemo(() => shown ? buildDay(shown, now, t) : null, [shown, now, lang]);
  const todayDate = feed.data?.date ?? null, at = date ?? todayDate;
  const step = (days: number) => { if (!at) return; const next = shiftDate(at, days); setDate(next === todayDate ? null : next); };
  // A read that does not come says so after a few seconds; the daemon is asked again on its own.
  const [slow, setSlow] = useState(false);
  useEffect(() => { setSlow(false); if (reply) return; const timer = setTimeout(() => setSlow(true), 6000); return () => clearTimeout(timer); }, [!!reply, date]);

  const [hot, setHot] = useState<string | null>(null), list = useRef<HTMLDivElement>(null), placed = useRef(''), ui = useBarState(), uiRef = useRef(ui);
  uiRef.current = ui;
  const edges = () => { const l = list.current; if (!l) return; l.classList.toggle('more-below', l.scrollTop + l.clientHeight < l.scrollHeight - 2); l.classList.toggle('more-above', l.scrollTop > 2); };
  // A day opens where it matters: today at its now line, another day from the top; so does the list cut to a range, and the whole day again.
  useLayoutEffect(() => {
    const l = list.current, spot = `${shown?.date}:${date}:${ui.filter ? 'cut' : ''}`;
    if (l && model && shown && placed.current !== spot) {
      placed.current = spot;
      const line = l.querySelector<HTMLElement>('.dt-now');
      l.scrollTop = line ? Math.max(0, offsetIn(line, l) - l.clientHeight * .6) : 0;
    }
    edges();
  });
  // A click on the bar, or on a piece of it, takes the row there to the top of the list in 300 ms, and the row flashes.
  const glide = useRef(0);
  const jump = (key: string) => {
    const l = list.current, el = l?.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"]`);
    if (!l || !el) return;
    const from = l.scrollTop, to = Math.max(0, Math.min(l.scrollHeight - l.clientHeight, offsetIn(el, l) - 12)), began = performance.now();
    cancelAnimationFrame(glide.current);
    el.classList.remove('is-flash'); void el.offsetWidth; el.classList.add('is-flash');
    if (reduced.matches || Math.abs(to - from) < 1) { l.scrollTop = to; return; }
    const tick = (at: number) => { const k = Math.min(1, (at - began) / 300); l.scrollTop = from + (to - from) * (1 - (1 - k) ** 3); if (k < 1) glide.current = requestAnimationFrame(tick); };
    glide.current = requestAnimationFrame(tick);
  };
  // The bar's lens, its range and the list's cut to a range: Esc lets go of one at a time before the pane itself closes (the pane's own
  // Esc listener was added after this one, so this one answers first).
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape' && !e.isComposing && uiRef.current.escape()) { e.preventDefault(); e.stopImmediatePropagation(); } };
    window.addEventListener('keydown', key, true);
    return () => { window.removeEventListener('keydown', key, true); cancelAnimationFrame(glide.current); };
  }, []);
  useEffect(() => { ui.setSel(null); ui.setFilter(null); }, [date]);
  const rows = model && ui.filter ? within(model.blocks, model.span, Math.min(ui.filter.a, ui.filter.b), Math.max(ui.filter.a, ui.filter.b)) : model?.blocks ?? [];

  const title = at ? dateTitle(at, t) : model?.title ?? '';
  const say = !port ? t(['Not connected to Jarvis yet', '还没连上 Jarvis']) : route.missing ? t(["This Jarvis can't give a day yet", '这台 Jarvis 还不会给出一天']) : slow || route.failed ? t(["Couldn't read this day. Trying again.", '没读到这一天，会再试一次']) : t(['Reading the day…', '读着这一天…']);
  return <div className={`dt${shown && !reply ? ' is-loading' : ''}`} data-lens={ui.lens ?? undefined} role="region" aria-label={t(['Day timeline', '一天的时间线'])}>
    <div className="dt-head">
      <b className="dt-date">{title}</b>{date === null && <em className="dt-today">{t(['Today', '今天'])}</em>}
      <span className="dt-chips">{model?.chips.map(c => <i key={c.text} className={`dt-chip${c.more ? ' is-more' : ''}`}>{c.text}</i>)}</span>
      <span className="dt-nav">
        <button type="button" aria-label={t(['The day before', '前一天'])} disabled={!at} onClick={() => step(-1)}><CaretLeft size={13}/></button>
        <button type="button" aria-label={t(['The day after', '后一天'])} disabled={!at || date === null} onClick={() => step(1)}><CaretRight size={13}/></button>
      </span>
    </div>
    {!model ? <p className="dt-say">{say}</p> : <>
      {model.line && <p className={`dt-line${model.line.draft ? ' is-draft' : ''}`}>{model.line.text}</p>}
      {model.empty ? <p className="dt-say">{t(['Nothing recorded for this day yet', '这一天还没有记下什么'])}</p> : <>
        <DayBar model={model} port={port} ui={ui} hot={hot} onHot={setHot} onJump={jump}/>
        {rows.length > 0 ? <div ref={list} className="dt-list" onScroll={edges} onWheel={() => cancelAnimationFrame(glide.current)}><Blocks blocks={rows} hot={hot} onHot={setHot}/></div>
          : ui.filter && <p className="dt-say">{t(['Nothing was recorded in this stretch', '这一段没有记下什么'])}</p>}
      </>}
      {model.foot && <p className="dt-foot">{model.foot}</p>}
    </>}
  </div>;
}

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { Bell, Bicycle, Bus, CalendarBlank, Car, CaretLeft, CaretRight, ChatCircleDots, GraduationCap, House, Image as PictureIcon, Laptop, MapPin, Moon, PersonSimpleRun, PersonSimpleWalk, Phone, Star, type Icon as PhosphorIcon } from '@phosphor-icons/react';
import { tr, useCompanionSettings, type L } from './companionSettings';
import { useNow, useRoute, type Route } from './homeData';
import { annotate, buildDay, dateTitle, shiftDate, type Bar, type Block, type DayReply, type Icon, type Row } from './dayView';
import './day-timeline.css';

// The timeline pane (ADR 0296): the drop pane's other content, grown from the notch's right side. One story top to bottom: the date,
// her sentence about the day, one bar for the whole day, and the day itself as a list; on today a now line, with what is still to come
// below it. `feed` is today as the wing already reads it; other days are read here.
const ICONS: Record<Icon, PhosphorIcon> = { moon: Moon, walk: PersonSimpleWalk, run: PersonSimpleRun, bike: Bicycle, car: Car, bus: Bus, talk: ChatCircleDots, picture: PictureIcon, event: CalendarBlank,
  reminder: Bell, work: Laptop, call: Phone, home: House, school: GraduationCap, pin: MapPin };
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const NOTE_FONT = '600 10.5px -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif';
const rgb = (c: readonly number[]) => c.join(' ');
let measuring: CanvasRenderingContext2D | null = null;
const textWidth = (text: string) => { const ctx = measuring ??= document.createElement('canvas').getContext('2d')!; ctx.font = NOTE_FONT; return ctx.measureText(text).width; };
// Where `el` is inside the scrolling `list`.
const offsetIn = (el: HTMLElement, list: HTMLElement) => el.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop;

type Hover = { hot: string | null; onHot: (key: string | null) => void };
function Ic({ icon, size = 13 }: { icon: Icon; size?: number }) { const Glyph = ICONS[icon]; return <Glyph size={size} aria-hidden="true"/>; }

function RowView({ row, hot, onHot }: { row: Row } & Hover) {
  return <div className={`dt-row${row.future ? ' is-future' : ''}${row.connector ? ' is-way' : ''}${hot === row.key ? ' is-hot' : ''}`} data-key={row.key} onPointerEnter={() => onHot(row.key)} onPointerLeave={() => onHot(null)}>
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

// The day in one bar. Pieces too short to name are not drawn as slivers: they are counted in a "+N" chip.
function Overview({ bar, hot, onHot, onJump }: { bar: Bar; hot: string | null; onHot: (key: string | null) => void; onJump: (key: string) => void }) {
  const box = useRef<HTMLDivElement>(null), [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = box.current!, ro = new ResizeObserver(() => setWidth(el.clientWidth));
    setWidth(el.clientWidth); ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const { drawn, merged } = useMemo(() => annotate(bar.segs, width, textWidth), [bar, width]);
  const [{ lang }] = useCompanionSettings();
  const first = merged[0];
  return <div ref={box} className="dt-bar">
    <div className="dt-notes">
      {drawn.map(d => <span key={d.seg.key} className={`dt-note ${d.label ? 'is-label' : 'is-icon'}`} style={{ left: d.x, width: d.w, '--c': rgb(d.seg.rgb) } as CSSProperties}><Ic icon={d.seg.icon} size={12}/>{d.label && <span>{d.seg.label}</span>}</span>)}
      {first && <button type="button" className="dt-more" title={merged.map(m => m.name).join(lang === 'zh' ? '、' : ', ')} onClick={() => onJump(first.row)}>+{merged.length}</button>}
    </div>
    <div className="dt-scale">
      <div className="dt-track">
        {drawn.map(d => <i key={d.seg.key} role="button" aria-label={d.seg.name} className={`dt-seg${hot === d.seg.row ? ' is-hot' : ''}`} style={{ left: `${d.seg.f0 * 100}%`, width: `${(d.seg.f1 - d.seg.f0) * 100}%`, '--c': rgb(d.seg.rgb), '--a': d.seg.a } as CSSProperties}
          onPointerEnter={() => onHot(d.seg.row)} onPointerLeave={() => onHot(null)} onClick={() => onJump(d.seg.row)}/>)}
      </div>
      {bar.nowF !== null && <i className="dt-nowtick" style={{ left: `${bar.nowF * 100}%` }}/>}
    </div>
    <div className="dt-hours" aria-hidden="true">{bar.ticks.map(k => <span key={k.label} className={k.f < .04 ? 'is-start' : k.f > .96 ? 'is-end' : ''} style={{ left: `${k.f * 100}%` }}>{k.label}</span>)}</div>
  </div>;
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

  const [hot, setHot] = useState<string | null>(null), list = useRef<HTMLDivElement>(null), placed = useRef('');
  const edges = () => { const l = list.current; if (!l) return; l.classList.toggle('more-below', l.scrollTop + l.clientHeight < l.scrollHeight - 2); l.classList.toggle('more-above', l.scrollTop > 2); };
  // A day opens where it matters: today at its now line, another day from the top.
  useLayoutEffect(() => {
    const l = list.current;
    if (l && model && shown && placed.current !== `${shown.date}:${date}`) {
      placed.current = `${shown.date}:${date}`;
      const line = l.querySelector<HTMLElement>('.dt-now');
      l.scrollTop = line ? Math.max(0, offsetIn(line, l) - l.clientHeight * .6) : 0;
    }
    edges();
  });
  const jump = (key: string) => {
    const l = list.current, el = l?.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"]`);
    if (!l || !el) return;
    l.scrollTo({ top: Math.max(0, offsetIn(el, l) - 6), behavior: reduced.matches ? 'auto' : 'smooth' });
    el.classList.remove('is-flash'); void el.offsetWidth; el.classList.add('is-flash');
  };

  const title = at ? dateTitle(at, t) : model?.title ?? '';
  const say = !port ? t(['Not connected to Jarvis yet', '还没连上 Jarvis']) : route.missing ? t(["This Jarvis can't give a day yet", '这台 Jarvis 还不会给出一天']) : slow || route.failed ? t(["Couldn't read this day. Trying again.", '没读到这一天，会再试一次']) : t(['Reading the day…', '读着这一天…']);
  return <div className={`dt${shown && !reply ? ' is-loading' : ''}`} role="region" aria-label={t(['Day timeline', '一天的时间线'])}>
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
        {model.bar && <Overview bar={model.bar} hot={hot} onHot={setHot} onJump={jump}/>}
        {model.blocks.length > 0 && <div ref={list} className="dt-list" onScroll={edges}><Blocks blocks={model.blocks} hot={hot} onHot={setHot}/></div>}
      </>}
      {model.foot && <p className="dt-foot">{model.foot}</p>}
    </>}
  </div>;
}

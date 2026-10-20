import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react';
import { Coins, ChatCircleDots, Image as PictureIcon, Laptop, Moon, Path, PersonSimpleRun, Phone, Star, SunHorizon, X, type Icon as PhosphorIcon } from '@phosphor-icons/react';
import { tr, useCompanionSettings, type L } from './companionSettings';
import { spring, step } from './starCore';
import { buildBar, endClock, fit, fOf, msOf, numbers, PAD_PX, peek, pieceAt, rowAt, snap, summarize, type BarSeg, type BarStar, type Line, type Peek, type Summary } from './barView';
import { clock, type DayModel, type Icon, type LensKind, type Rgb } from './dayView';
import { cardOf } from './numberCard';
import { CardBody, Swap } from './CardView';
import { EASE, SayWhat } from './RowOpen';
import { sayRange, type Said } from './dayOpen';
import { Ic } from './dayIcons';
import './day-bar.css';

// The day in one bar (ADR 0308), above the rows: what each stretch is, written in it; now; what is still to come; the stars of what
// mattered. The pointer reads it: a hairline and a card with what the stretch was and what else was going on, a click jumps the list
// there, a drag chooses a range. Under it the day's numbers, each a lens. The rows are DayTimeline's: they meet this at a row's key.
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const LABEL = '600 11px -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif', MONO = '600 11px "JetBrains Mono", Menlo, monospace';
const CARD_W = 280, EDGE = 4, TAP_PX = 3, FOLLOW_HZ = 11.7, ATTACHMENT = /^[0-9a-f]{32}$/;
const rgb = (c: readonly number[]) => c.join(' ');
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
let measuring: CanvasRenderingContext2D | null = null;
const textWidth = (text: string, mono: boolean) => { const ctx = measuring ??= document.createElement('canvas').getContext('2d')!; ctx.font = mono ? MONO : LABEL; return Math.ceil(ctx.measureText(text).width); };

// Kind colours belong to a lens and to the numbers' icons only.
const KIND: Record<LensKind, Rgb> = { sleep: [112, 120, 205], trip: [196, 208, 255], workout: [156, 224, 120], work: [108, 156, 255], call: [111, 224, 180], talk: [172, 124, 255], payment: [240, 132, 218], picture: [255, 240, 220] };
const KIND_ICON: Record<LensKind, PhosphorIcon> = { sleep: Moon, trip: Path, workout: PersonSimpleRun, work: Laptop, call: Phone, talk: ChatCircleDots, payment: Coins, picture: PictureIcon };

// What the bar's controls hold, kept by the pane: the lens (pinned by a click, or previewed while a chip is under the pointer), the range
// being chosen, the range the list is cut to. Esc lets go of one at a time.
export type Range = { a: number; b: number };
export function useBarState() {
  const [pinned, setPinned] = useState<LensKind | null>(null), [preview, setPreview] = useState<LensKind | null>(null);
  const [sel, setSel] = useState<Range | null>(null), [filter, setFilter] = useState<Range | null>(null);
  const live = useRef({ pinned, sel, filter });
  live.current = { pinned, sel, filter };
  // The pinned lens lets go with the preview of the chip the pointer may still be on, or that chip would hold it.
  const escape = () => { const s = live.current; if (s.sel) setSel(null); else if (s.pinned) { setPinned(null); setPreview(null); } else if (s.filter) setFilter(null); else return false; return true; };
  return { pinned, setPinned, preview, setPreview, lens: preview ?? pinned, sel, setSel, filter, setFilter, escape };
}
export type BarState = ReturnType<typeof useBarState>;

function Thumb({ id, port, icon }: { id: string | null; port: string | null; icon: Icon }) {
  const [broken, setBroken] = useState(false);
  return id && port && ATTACHMENT.test(id) && !broken
    ? <img className="dt-thumb" alt="" width={48} height={48} decoding="async" src={`http://127.0.0.1:${port}/inherent/attachments/${id}`} onError={() => setBroken(true)}/>
    : <span className="dt-cic"><Ic icon={icon} size={14}/></span>;
}
function CardLine({ l, port }: { l: Line; port: string | null }) {
  return <li className={`dt-cline${l.picture ? ' has-pic' : ''}`}>
    {l.picture ? <Thumb id={l.picture} port={port} icon={l.icon}/> : <span className="dt-cic"><Ic icon={l.icon} size={13}/></span>}
    <span className="dt-ctx">{l.star && <Star size={10} weight="fill" className="dt-cstar" aria-hidden="true"/>}{l.text}</span>
    <span className="dt-cside">{l.side}</span>
  </li>;
}
function PeekBody({ p, port, t }: { p: Peek; port: string | null; t: (l: L) => string }) {
  const { stretch } = p;
  return <>
    <div className="dt-ch">
      <i className={`dt-cdot${stretch.rgb ? '' : ' is-hollow'}`} style={stretch.rgb ? { '--c': rgb(stretch.rgb), '--a': stretch.a } as CSSProperties : undefined}/>
      <b>{stretch.name}</b><span>{[stretch.range, stretch.length].filter(Boolean).join(' · ')}</span>
    </div>
    {stretch.place && <div className="dt-cnote is-place">{stretch.place}</div>}
    {stretch.note && <div className="dt-cnote">{stretch.note}</div>}
    {p.star && <div className="dt-cmark"><Star size={11} weight="fill" aria-hidden="true"/><span>{p.star.label || (p.star.by === 'her' ? t(['She marked this', '她觉得这段重要']) : t(['You marked this', '你标过这段']))}</span></div>}
    {p.lines.length ? <ul className="dt-cl">{p.lines.map(l => <CardLine key={l.key} l={l} port={port}/>)}</ul> : <p className="dt-cnone">{t(['Nothing else was recorded then', '这段时间没有别的记录'])}</p>}
    <p className="dt-cfoot">{t(['Click to jump here · drag to pick a range', '点一下跳到这里 · 拖动框一段'])}</p>
  </>;
}
function RangeBody({ s, say, onSay, onOnly, t }: { s: Summary; say: Said | null; onSay: (said: Said) => void; onOnly: () => void; t: (l: L) => string }) {
  return <>
    <div className="dt-ch"><b>{s.range}</b><span>{s.length}</span></div>
    {s.places.length + s.nums.length ? <ul className="dt-cl is-range">
      {s.places.map(p => <li key={p.name} className="dt-cline"><span className="dt-cic"><i className="dt-cdot" style={{ '--c': rgb(p.rgb), '--a': 1 } as CSSProperties}/></span><span className="dt-ctx">{p.name}</span><span className="dt-cside">{p.length}</span></li>)}
      {s.nums.map(n => { const Glyph = KIND_ICON[n.kind]; return <li key={n.kind} className="dt-cline"><span className="dt-cic" style={{ color: `rgb(${rgb(KIND[n.kind])})` }}><Glyph size={13} aria-hidden="true"/></span><span className="dt-ctx">{n.label}</span><span className="dt-cside">{n.long}</span></li>; })}
    </ul> : <p className="dt-cnone">{t(['Nothing was recorded in this stretch', '这一段没有记下什么'])}</p>}
    <div className="dt-cbtns"><button type="button" className="dt-cbtn" onClick={onOnly}>{t(['Show only this', '只看这段'])}</button>
      {say && <SayWhat press={() => onSay(say)} t={t}/>}</div>
  </>;
}

// What mattered: a lit star at the start of each highlighted item, his filled and hers outlined. One that comes fades in (the style does it); one
// that goes stays 200 ms and fades out (120 under reduced motion). The day's own stars are drawn again for another day (the key), never faded.
function Stars({ stars }: { stars: BarStar[] }) {
  const [gone, setGone] = useState<BarStar[]>([]), last = useRef(stars), live = useRef(true);
  useEffect(() => () => { live.current = false; }, []);
  useEffect(() => {
    const now = new Set(stars.map(s => s.id)), left = last.current.filter(s => !now.has(s.id));
    last.current = stars;
    if (!left.length && !gone.length) return;
    setGone(g => [...g.filter(s => !now.has(s.id)), ...left]);
    if (left.length) setTimeout(() => { if (live.current) setGone(g => g.filter(s => !left.includes(s))); }, reduced.matches ? 120 : 200);
  }, [stars]);
  const one = (s: BarStar, away: boolean) => <i key={s.id} className={`dt-lit${s.hers ? ' is-hers' : ''}${away ? ' is-gone' : ''}`} data-star={s.id} style={{ left: `${s.f * 100}%` }}><Star size={9} weight={s.hers ? 'bold' : 'fill'}/></i>;
  return <>{stars.map(s => one(s, false))}{gone.map(s => one(s, true))}</>;
}

// `open`: the row whose detail page is up; its span stays marked, and a click on the bar goes to `onPage` (the piece it is on, or null) instead of `onJump`.
export function DayBar({ model, port, ui, cut, hot, open, onHot, onJump, onPage, onPicture, onSay }: { model: DayModel; port: string | null; ui: BarState; cut: Range | null; hot: string | null; open: string | null; onHot: (key: string | null) => void; onJump: (key: string) => void; onPage: (piece: string | null) => void; onPicture: (id: string, note: string) => void; onSay: (said: Said) => void }) {
  const [{ lang }] = useCompanionSettings(), t = (l: L) => tr(lang, l);
  const bar = useMemo(() => buildBar(model, t), [model, lang]), nums = useMemo(() => numbers(model, t), [model, lang]);
  const box = useRef<HTMLDivElement>(null), track = useRef<HTMLDivElement>(null), card = useRef<HTMLDivElement>(null), rcard = useRef<HTMLDivElement>(null), ncard = useRef<HTMLDivElement>(null), nIn = useRef<HTMLDivElement>(null), [width, setWidth] = useState(0);
  const [hover, setHover] = useState<{ x: number; left: number } | null>(null), [on, setOn] = useState(false), [band, setBand] = useState<Range | null>(null);
  // The pinned number's card hangs under the numbers; a range being chosen or chosen has the room (its card wins), and the number's comes back when the range goes.
  const numCard = useMemo(() => ui.pinned ? cardOf(model, ui.pinned, t, nums) : null, [model, lang, nums, ui.pinned]), numKind = numCard && !ui.sel && !band ? numCard.kind : null;
  useLayoutEffect(() => {
    const el = track.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    setWidth(el.clientWidth); ro.observe(el);
    return () => ro.disconnect();
  }, [!!bar]);
  // A lens whose number the day does not have (the day was changed) lets go, pinned or previewed by a chip that is not there to say it has left.
  useEffect(() => { if (ui.pinned && !nums.some(n => n.kind === ui.pinned)) ui.setPinned(null); if (ui.preview && !nums.some(n => n.kind === ui.preview)) ui.setPreview(null); }, [nums]);

  // The card opens after the pointer has rested 50 ms on the bar.
  useEffect(() => {
    if (!hover) { setOn(false); return; }
    const timer = setTimeout(() => setOn(true), 50);
    return () => clearTimeout(timer);
  }, [!!hover]);
  // The pane is as tall as what is in it: on a short day the range card and a number's, which hang over the list, make room for themselves.
  useLayoutEffect(() => {
    const dt = box.current?.closest<HTMLElement>('.dt'), c = rcard.current ?? ncard.current;
    if (!dt || !c) return;
    dt.style.minHeight = `${Math.ceil(c.getBoundingClientRect().bottom - dt.getBoundingClientRect().top) + 12}px`;
    return () => { dt.style.minHeight = ''; };
  }, [ui.sel, numKind]);
  // A number's card comes in over 180 ms on the pane's ease, rising 4 px; under reduced motion it fades in over 120.
  useLayoutEffect(() => {
    if (!numKind) return;
    const quiet = reduced.matches;
    nIn.current?.animate(quiet ? [{ opacity: 0 }, { opacity: 1 }] : [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: quiet ? 120 : 180, easing: quiet ? 'linear' : EASE });
  }, [!numKind]);
  // A click away from the range card lets it go.
  useEffect(() => {
    if (!ui.sel) return;
    const down = (e: PointerEvent) => { if (!(e.target as Element | null)?.closest?.('.dt-card.is-range')) ui.setSel(null); };
    window.addEventListener('pointerdown', down, true);
    return () => window.removeEventListener('pointerdown', down, true);
  }, [!!ui.sel]);

  const laid = useMemo(() => bar ? bar.segs.map(s => { const w = (s.f1 - s.f0) * width - 1; return { s, w, how: s.hole ? 'none' as const : fit(w, textWidth(s.short, s.mono), s.short) }; }) : [], [bar, width]);
  const boxes = useMemo(() => bar ? bar.sched.filter(x => x.kind === 'event').map(x => { const w = (x.f1 - x.f0) * width; return { x, w, how: fit(w, textWidth(x.short, false), x.short) }; }) : [], [bar, width]);
  const ms = (x: number) => msOf(bar!, width ? x / width : 0);
  const bounds = () => { const el = box.current!, h = el.closest('.dt')!.getBoundingClientRect(), b = el.getBoundingClientRect(); return { min: h.left - b.left + EDGE, max: h.right - b.left - EDGE - CARD_W }; };
  const left = (center: number) => { const { min, max } = bounds(); return clamp(center - CARD_W / 2, min, Math.max(min, max)); };

  // The pointer: a press and a release within 3 px is a click, a press and a move of more is a range.
  const press = useRef<{ x0: number; drag: boolean } | null>(null);
  const at = (e: { clientX: number }) => clamp(e.clientX - track.current!.getBoundingClientRect().left, 0, track.current!.clientWidth);
  const down = (e: ReactPointerEvent) => {
    if (e.button !== 0 || !bar) return;
    press.current = { x0: at(e), drag: false };
    e.currentTarget.setPointerCapture(e.pointerId);
    ui.setSel(null);
  };
  // A press whose release was never heard (the capture was taken, the button came up elsewhere) lets go, and the pointer is a hover again.
  const release = () => { press.current = null; setBand(null); };
  const move = (e: ReactPointerEvent) => {
    if (!bar) return;
    if (press.current && e.buttons === 0) release();
    const x = at(e), p = press.current;
    if (p && !p.drag && Math.abs(x - p.x0) > TAP_PX) { p.drag = true; setHover(null); onHot(null); }
    if (p?.drag) setBand({ a: snap(ms(p.x0)), b: snap(ms(x)) });
    else { setHover({ x, left: left(x) }); onHot(pieceAt(bar, ms(x))); }
  };
  // A drag that ends where it began (to the minute) is a click: a range of no length is none.
  const up = (e: ReactPointerEvent) => {
    const p = press.current;
    press.current = null;
    if (!p || !bar) return;
    const x = at(e), a = snap(ms(p.x0)), b = snap(ms(x));
    setBand(null);
    if (p.drag && a !== b) { ui.setSel({ a: Math.min(a, b), b: Math.max(a, b) }); return; }
    if (open) { onPage(pieceAt(bar, ms(x))); return; }
    const key = rowAt(model, bar, ms(x));
    if (key) onJump(key);
  };
  const leave = () => { if (press.current) return; setHover(null); onHot(null); };

  // The card follows the pointer on a critically damped spring (about 90 ms to settle); under reduced motion it jumps.
  const follow = useRef({ sp: spring(0), goal: 0, raf: 0, last: 0, shown: false });
  const target = hover?.left ?? 0;
  useLayoutEffect(() => {
    const f = follow.current, el = card.current;
    if (!el || !hover) { f.shown = false; return; }
    f.goal = target;
    const draw = () => { el.style.transform = `translateX(${f.sp.value}px)`; };
    if (reduced.matches || !f.shown) { f.sp.value = target; f.sp.velocity = 0; f.shown = true; draw(); return; }
    if (f.raf) return;
    f.last = performance.now();
    const tick = (now: number) => {
      const moving = step(f.sp, f.goal, FOLLOW_HZ, 1, Math.min(.05, (now - f.last) / 1000));
      f.last = now; draw();
      f.raf = moving ? requestAnimationFrame(tick) : 0;
    };
    f.raf = requestAnimationFrame(tick);
  }, [target, !!hover]);
  useEffect(() => () => cancelAnimationFrame(follow.current.raf), []);

  // What the card says is two layers that swap when the stretch changes: the words that were fade out, then the new ones fade in (the style
  // does that), never both at once; and the card's height goes to the new words' meanwhile (150 ms, from where it is if it was on its way).
  const live = hover && bar ? peek(model, bar, ms(hover.x), t) : null;
  const slots = useRef<{ cur: 0 | 1; at: [Peek | null, Peek | null] }>({ cur: 0, at: [null, null] });
  if (live) { const s = slots.current; if (s.at[s.cur]?.id !== live.id) s.cur = s.cur ? 0 : 1; s.at[s.cur] = live; }
  // Only the height's own animation is kept in hand and started again: the card's CSS transitions (its rise) are not this effect's to cancel.
  const cardIn = useRef<HTMLDivElement>(null), needed = useRef<number | null>(null), sizing = useRef<Animation | null>(null);
  useLayoutEffect(() => {
    const el = cardIn.current, before = needed.current;
    if (!el || !live) { needed.current = null; return; }
    const moving = sizing.current?.playState === 'running', from = moving ? el.getBoundingClientRect().height : before;
    sizing.current?.cancel();
    const to = needed.current = el.getBoundingClientRect().height;
    sizing.current = from != null && Math.abs(from - to) > .5 && !reduced.matches ? el.animate([{ height: `${from}px` }, { height: `${to}px` }], { duration: 150, easing: 'cubic-bezier(.16, 1, .3, 1)' }) : null;
  });

  if (!bar) return null;
  const sel = band ?? ui.sel, shown = sel ?? ui.filter, a = shown && Math.min(shown.a, shown.b), b = shown && Math.max(shown.a, shown.b);
  const summary = ui.sel ? summarize(model, bar, ui.sel.a, ui.sel.b, t) : null, lens = ui.lens && nums.some(n => n.kind === ui.lens) ? ui.lens : null;
  // A day with no place in it (talks and no location) says so in the empty past, if the words fit there.
  const voidText = t(['No location recorded', '没有位置记录']), voidW = (bar.nowF ?? 1) * width, showVoid = bar.segs.length === 0 && voidW >= textWidth(voidText, false) + 2 * PAD_PX;
  // The sun's marks sit in the hours row. An hour's number always wins: a mark that would sit on one is only a tick under the bar.
  const hours = bar.ticks.map(k => ({ k, px: k.f < .02 ? 6 : k.f > .98 ? width - 6 : k.f * width }));
  const suns = bar.sun.map(x => { const px = clamp(x.f * width, 6, Math.max(6, width - 6)); return { ...x, px, bare: hours.some(h => Math.abs(h.px - px) < 14) }; });
  const bx = (x: number) => `${clamp(x, 0, 1) * 100}%`, edge = (x: number) => ({ left: `clamp(16px, ${x}px, calc(100% - 16px))` });
  const lifted = live?.row ?? null, spanned = hot && hot !== lifted && hot !== open ? model.span.get(hot) : null, opened = open ? model.span.get(open) : null;
  return <>
    <div ref={box} className={`dt-bar${lens ? ' is-lens' : ''}${opened ? ' has-open' : ''}`} data-from={clock(bar.t0, model.tz)} role="group" aria-label={t(['The day in one bar', '一天的总览条'])} style={lens ? { '--k': rgb(KIND[lens]) } as CSSProperties : undefined}>
      <div className="dt-scrub" aria-hidden="true">
        {bar.nowF !== null && !hover && !sel && <span className="dt-time is-now" style={{ left: `clamp(16px, ${bar.nowF * 100}%, calc(100% - 16px))` }}>{clock(model.now, model.tz)}</span>}
        {hover && !band && <span className="dt-time" style={edge(hover.x)}>{live?.at}</span>}
        {sel && a !== null && b !== null && (fOf(bar, b) - fOf(bar, a)) * width < 36
          ? <span className="dt-time" style={edge((fOf(bar, a) + fOf(bar, b)) / 2 * width)}>{clock(a, model.tz)}–{endClock(b, model.reply, model.tz)}</span>
          : sel && a !== null && b !== null && <><span className="dt-time" style={edge(fOf(bar, a) * width)}>{clock(a, model.tz)}</span><span className="dt-time" style={edge(fOf(bar, b) * width)}>{endClock(b, model.reply, model.tz)}</span></>}
      </div>
      <div className="dt-trackbox">
        <div ref={track} className="dt-track" onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={release} onLostPointerCapture={release} onPointerLeave={leave}>
          <i className="dt-ground" style={{ width: bar.nowF === null ? '100%' : bx(bar.nowF) }}/>
          {bar.nowF !== null && <i className="dt-ground is-future" style={{ left: bx(bar.nowF) }}/>}
          {laid.map(({ s, w, how }) => <i key={s.key} className={`dt-seg is-${s.kind} is-${s.tone}${lifted === s.row ? ' is-lift' : ''}${spanned && hot === s.row ? ' is-hot' : ''}`}
            style={{ left: `calc(${s.f0 * 100}% + .5px)`, width: `max(1px, calc(${(s.f1 - s.f0) * 100}% - 1px))`, '--c': rgb(s.rgb), '--a': s.a } as CSSProperties}>
            {how !== 'none' && <span className={`dt-sl${how === 'icon' ? ' is-icon' : ''}`}>{how !== 'name' && <Ic icon={s.icon} size={12}/>}{how !== 'icon' && <b className={s.mono ? 'is-mono' : ''}>{s.short}</b>}</span>}
          </i>)}
          {showVoid && <i className="dt-void" style={{ width: bx(bar.nowF ?? 1) }}>{voidText}</i>}
          {boxes.map(({ x, how }) => <i key={x.key} data-sched={x.key} className={`dt-sched is-event${spanned && hot === x.key ? ' is-hot' : ''}${x.leaving ? ' is-leaving' : ''}`} style={{ left: `${x.f0 * 100}%`, width: `${(x.f1 - x.f0) * 100}%` }}>
            {how !== 'none' && <span className={`dt-sl${how === 'icon' ? ' is-icon' : ''}`}>{how !== 'name' && <Ic icon="event" size={12}/>}{how !== 'icon' && <b>{x.short}</b>}</span>}
          </i>)}
          {bar.sched.filter(x => x.kind !== 'event').map(x => <i key={x.key} className="dt-sched is-tick" style={{ left: `${x.f0 * 100}%` }}><u/></i>)}
          {lens && (bar.lens[lens] ?? []).map((m, i) => <i key={i} className={`dt-lm${m.f1 - m.f0 < .004 ? ' is-point' : ''}`} style={{ left: `${m.f0 * 100}%`, width: `${Math.max(0, m.f1 - m.f0) * 100}%` }}/>)}
        </div>
        <div className="dt-over" aria-hidden="true">
          {sel && a !== null && b !== null && <i className="dt-band" style={{ left: bx(fOf(bar, a)), width: `${(clamp(fOf(bar, b), 0, 1) - clamp(fOf(bar, a), 0, 1)) * 100}%` }}/>}
          {!sel && ui.filter && a !== null && b !== null && <i className="dt-band is-applied" style={{ left: bx(fOf(bar, a)), width: `${(clamp(fOf(bar, b), 0, 1) - clamp(fOf(bar, a), 0, 1)) * 100}%` }}/>}
          {[spanned, opened].map((x, i) => x && x.t1 >= bar.t0 && x.t0 <= bar.t1 && <i key={i} className={`dt-bracket${i ? ' is-open' : ''}`} style={{ left: bx(fOf(bar, x.t0)), width: `max(3px, ${(clamp(fOf(bar, x.t1), 0, 1) - clamp(fOf(bar, x.t0), 0, 1)) * 100}%)` }}/>)}
          {bar.dots.map(d => <i key={d.key} className={`dt-bdot${lens === d.kind ? ' is-on' : ''}`} data-dot={d.key} style={{ left: bx(d.f), '--k': rgb(KIND[d.kind]) } as CSSProperties}/>)}
          <Stars key={model.reply.date} stars={bar.stars}/>
          {bar.nowF !== null && <i className="dt-nowtick" style={{ left: bx(bar.nowF) }}/>}
          {hover && !band && <i className="dt-hair" style={{ left: hover.x }}/>}
        </div>
      </div>
      <div className="dt-hours" aria-hidden="true">
        {hours.map(({ k }) => <span key={k.label} className={k.f < .02 ? 'is-start' : k.f > .98 ? 'is-end' : ''} style={{ left: `${k.f * 100}%` }}>{k.label}</span>)}
        {suns.map(x => <span key={x.kind} className={`dt-sun${x.bare ? ' is-tick' : ''}`} data-sun={x.kind} style={{ left: x.px }}>{!x.bare && <SunHorizon size={11}/>}</span>)}
      </div>
      <div ref={card} className={`dt-card${on && !ui.sel && !band ? ' is-on' : ''}`} aria-hidden="true">
        <div ref={cardIn} className="dt-card-in">{slots.current.at.map((p, i) => <div key={i} className={`dt-slot${i === slots.current.cur ? ' is-cur' : ''}`}>{p && <PeekBody p={p} port={port} t={t}/>}</div>)}</div>
      </div>
      {summary && <div ref={rcard} className="dt-card is-range is-on" style={{ transform: `translateX(${left((fOf(bar, summary.a) + fOf(bar, summary.b)) / 2 * width)}px)` }}>
        <div className="dt-card-in"><div className="dt-slot is-cur"><RangeBody s={summary} say={sayRange(model, summary.a, summary.b)} onSay={onSay} t={t} onOnly={() => { ui.setFilter({ a: summary.a, b: summary.b }); ui.setSel(null); }}/></div></div>
      </div>}
    </div>
    {nums.length + model.vitals.length > 0 && <div className="dt-numbers" role="group" aria-label={t(['The day in numbers', '这一天的数字'])}>
      {nums.map(n => { const Glyph = KIND_ICON[n.kind]; return <button key={n.kind} type="button" className={`dt-num${ui.pinned === n.kind ? ' is-pinned' : ''}`} aria-pressed={ui.pinned === n.kind} style={{ '--k': rgb(KIND[n.kind]) } as CSSProperties}
        onPointerEnter={() => ui.setPreview(n.kind)} onPointerLeave={() => ui.setPreview(null)} onClick={() => ui.setPinned(ui.pinned === n.kind ? null : n.kind)}>
        <Glyph size={12} aria-hidden="true"/><span>{n.label}</span><em>{n.value}</em></button>; })}
      {model.vitals.length > 0 && <div className="dt-vitals">{model.vitals.map(v => <span key={v.key} className="dt-vit" data-vital={v.key}>{v.before && <small>{v.before}</small>}<em>{v.value}</em>{v.after && <small>{v.after}</small>}</span>)}</div>}
      {numCard && numKind && <div ref={ncard} className="dt-card is-num is-on" role="region" aria-label={numCard.label}>
        <div ref={nIn} className="dt-card-in"><Swap id={numCard.kind}><CardBody card={numCard} Glyph={KIND_ICON[numCard.kind]} color={`rgb(${rgb(KIND[numCard.kind])})`} port={port} onPicture={onPicture} t={t}/></Swap></div>
      </div>}
    </div>}
    {cut && <div className="dt-filter"><span>{t(['Only ', '只看 '])}<b>{clock(Math.min(cut.a, cut.b), model.tz)}–{endClock(Math.max(cut.a, cut.b), model.reply, model.tz)}</b></span>
      <button type="button" aria-label={t(['Show the whole day', '看回整天'])} onClick={() => ui.setFilter(null)}><X size={11} weight="bold"/></button></div>}
  </>;
}

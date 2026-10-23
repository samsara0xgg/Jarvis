import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { Coins, ChatCircleDots, Image as PictureIcon, Laptop, Moon, Path, PersonSimpleRun, Phone, SunHorizon, X, type Icon as PhosphorIcon } from '@phosphor-icons/react';
import { tr, useCompanionSettings, type L } from './companionSettings';
import { buildBar, endClock, fit, fOf, msOf, numbers, PAD_PX, pieceAt, rowAt, snap, summarize, type BarBy, type BarSeg, type Summary } from './barView';
import { clock, type DayModel, type Icon, type LensKind, type Rgb } from './dayView';
import { EASE, SayWhat } from './RowOpen';
import { sayOf, sayRange, type Said } from './dayOpen';
import { Ic } from './dayIcons';
import { DoorMarks, extentOf, nearestMark, type Extent } from './DayDoors';
import type { BarMark } from './doors';
import './day-bar.css';

// The day in one bar (ADR 0308), above the rows: 26 px, what each stretch is, written in it when the name fits (its icon always); now; what is still to
// come. The pointer reads it: the minute under the bar, the stretch under it lit together with its row; a click jumps the list there, a drag chooses a
// range. The row that is selected has a frame round its span that glides when the selection changes. Under it the day's numbers on one line, each a filter. A stretch
// nothing recorded is a dashed "?" and a click on it asks what it was. The rows are DayTimeline's: they meet this at a row's key.
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const LABEL = '600 11px -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif', MONO = '600 11px "JetBrains Mono", Menlo, monospace';
const CARD_W = 280, EDGE = 4, TAP_PX = 3, FLASH_MS = 600, PIN_HALF = 19;
const rgb = (c: readonly number[]) => c.join(' ');
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
let measuring: CanvasRenderingContext2D | null = null;
const textWidth = (text: string, mono: boolean) => { const ctx = measuring ??= document.createElement('canvas').getContext('2d')!; ctx.font = mono ? MONO : LABEL; return Math.ceil(ctx.measureText(text).width); };

// Kind colours belong to a filter (the lens) and to the numbers' icons only.
const KIND: Record<LensKind, Rgb> = { sleep: [112, 120, 205], trip: [196, 208, 255], workout: [156, 224, 120], work: [108, 156, 255], call: [111, 224, 180], talk: [172, 124, 255], payment: [240, 132, 218], picture: [255, 240, 220] };
const KIND_ICON: Record<LensKind, PhosphorIcon> = { sleep: Moon, trip: Path, workout: PersonSimpleRun, work: Laptop, call: Phone, talk: ChatCircleDots, payment: Coins, picture: PictureIcon };

// What the bar's controls hold, kept by the pane: the lens (pinned by a click, or previewed while a chip is under the pointer), the same for a door page's chip
// (the day's lens and a page's chip are never both: the numbers row is the one or the other), the range being chosen, the range the list is cut to. Esc lets go of one at a time.
export type Range = { a: number; b: number };
export function useBarState() {
  const [pinned, setPinned] = useState<LensKind | null>(null), [preview, setPreview] = useState<LensKind | null>(null);
  const [chipPinned, setChipPinned] = useState<string | null>(null), [chipPreview, setChipPreview] = useState<string | null>(null);
  const [sel, setSel] = useState<Range | null>(null), [filter, setFilter] = useState<Range | null>(null);
  const live = useRef({ pinned, chipPinned, sel, filter });
  live.current = { pinned, chipPinned, sel, filter };
  // The pinned lens lets go with the preview of the chip the pointer may still be on, or that chip would hold it.
  const escape = () => {
    const s = live.current;
    if (s.sel) setSel(null);
    else if (s.pinned) { setPinned(null); setPreview(null); }
    else if (s.chipPinned) { setChipPinned(null); setChipPreview(null); }
    else if (s.filter) setFilter(null);
    else return false;
    return true;
  };
  return { pinned, setPinned, preview, setPreview, lens: preview ?? pinned, chipPinned, setChipPinned, chipPreview, setChipPreview, chip: chipPreview ?? chipPinned, sel, setSel, filter, setFilter, escape };
}
export type BarState = ReturnType<typeof useBarState>;

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

// What the bar is drawn from, on a day with stretches: a glass thumb under the side that is on. A click on a side moves it there. It can be pressed and dragged along the
// toggle: it grows while it is held, the bar switches the moment its middle crosses the toggle's, and on release it springs to the nearer side (the side that is on).
function ByToggle({ value, onChange, t }: { value: BarBy; onChange: (by: BarBy) => void; t: (l: L) => string }) {
  const box = useRef<HTMLDivElement>(null), held = useRef<{ x0: number; base: number; w: number } | null>(null), live = useRef(value), [drag, setDrag] = useState<number | null>(null);
  live.current = value;
  const down = (e: ReactPointerEvent) => {
    if (e.button !== 0 || !(e.target as Element).closest('button[aria-pressed="true"]')) return;
    const w = (box.current!.clientWidth - 4) / 2, base = value === 'place' ? w : 0;
    held.current = { x0: e.clientX, base, w };
    box.current!.setPointerCapture(e.pointerId);
    setDrag(base);
  };
  const move = (e: ReactPointerEvent) => {
    const h = held.current;
    if (!h) return;
    const x = clamp(h.base + e.clientX - h.x0, 0, h.w), side: BarBy = x + h.w / 2 > h.w ? 'place' : 'doing';
    setDrag(x);
    if (side !== live.current) { live.current = side; onChange(side); }
  };
  const up = () => { held.current = null; setDrag(null); };
  return <div ref={box} className={`dt-by${drag !== null ? ' is-held' : ''}`} data-by={value} role="group" aria-label={t(['What the bar shows', '总览条画的是'])} style={drag !== null ? { '--x': `${drag}px` } as CSSProperties : undefined}
    onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up} onLostPointerCapture={up}>
    <i className="dt-gl" aria-hidden="true"><i/></i>
    {(['doing', 'place'] as const).map(k => <button key={k} type="button" aria-pressed={value === k} onClick={() => onChange(k)}>{k === 'doing' ? t(['Doing', '在干嘛']) : t(['Place', '在哪'])}</button>)}
  </div>;
}

// While the day is being tidied (ADR 0310) a soft band of light crosses the bar from left to right, 1.6 s a pass, over and over (the style does it). When the tidy ends the pass it is
// on is finished first, so it never stops half way. Under reduced motion there is none.
function Sweep({ on }: { on: boolean }) {
  const [here, setHere] = useState(on);
  if (on && !here) setHere(true);
  return here && !reduced.matches ? <i className="dt-sweep" aria-hidden="true" onAnimationIteration={() => { if (!on) setHere(false); }}/> : null;
}

// A door's page on the bar (doors.ts): only 今天 draws the day, so the bar keeps its track, the hours and now, and the page's marks are drawn on it (`mute`: the keys of the rows a lit chip keeps,
// the marks of the others are drawn at .25); the page's numbers row goes under the bar in place of the day's. What the pointer and a click find is then the page's marks, not the day's pieces.
export type PageBar = { marks: BarMark[]; mute: ReadonlySet<string> | null; numbers: ReactNode };
// `open`: the row that is selected (its detail is up); a frame sits round its span and glides when it changes, and a click on the bar goes to `onPage` (the piece it is on, or null) instead of `onJump`.
// `sweep`: the day is being tidied. `door`: a door's page is under the bar.
export function DayBar({ model, port, ui, cut, sweep, hot, open, door, onHot, onJump, onPage, onPicture, onSay }: { model: DayModel; port: string | null; ui: BarState; cut: Range | null; sweep: boolean; hot: string | null; open: string | null; door: PageBar | null; onHot: (key: string | null) => void; onJump: (key: string) => void; onPage: (piece: string | null) => void; onPicture: (id: string, note: string) => void; onSay: (said: Said) => void }) {
  const [{ lang, barBy }, setSettings] = useCompanionSettings(), t = (l: L) => tr(lang, l);
  const bar = useMemo(() => buildBar(model, t, barBy), [model, lang, barBy]), nums = useMemo(() => numbers(model, t), [model, lang]);
  const box = useRef<HTMLDivElement>(null), track = useRef<HTMLDivElement>(null), [width, setWidth] = useState(0);
  const frame = useRef<{ date: string; at: CSSProperties } | null>(null), epoch = useRef(0), [hover, setHover] = useState<{ x: number } | null>(null), [band, setBand] = useState<Range | null>(null), [flash, setFlash] = useState<string | null>(null), flashEnd = useRef(0);
  // The chips are one line: those that do not fit at the right are hidden (they keep their room, so what fits is measured the same each time); `cutAt` is the first that is.
  const strip = useRef<HTMLDivElement>(null), [cutAt, setCutAt] = useState(Infinity);
  useLayoutEffect(() => {
    const el = strip.current;
    if (!el) return;
    const fits = () => { const room = el.clientWidth, over = [...el.children].findIndex(c => (c as HTMLElement).offsetLeft + (c as HTMLElement).offsetWidth > room + .5); setCutAt(over < 0 ? Infinity : over); };
    fits();
    const ro = new ResizeObserver(fits);
    ro.observe(el);
    return () => ro.disconnect();
  }, [nums, model.vitals, lang, !!door, !!bar]);
  useLayoutEffect(() => {
    const el = track.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    setWidth(el.clientWidth); ro.observe(el);
    return () => ro.disconnect();
  }, [!!bar]);
  // A bar that is not made of the pieces it was made of, on the same day and drawn the same way (a new run of the day, ADR 0310), crosses over: the bar as it was drawn is laid over the new
  // one and fades out, 240 ms (a 120 ms fade under reduced motion). The copy is taken each time the bar is drawn, so it is the last one there was.
  const drawn = useRef<{ day: string; by: BarBy; pieces: string; copy: HTMLElement } | null>(null);
  useLayoutEffect(() => {
    const el = track.current;
    if (!el || !bar) { drawn.current = null; return; }
    const was = drawn.current, pieces = bar.segs.map(s => s.key).join('|');
    if (was && was.day === model.reply.date && was.by === barBy && was.pieces !== pieces) {
      const old = was.copy;
      old.classList.add('is-ghost'); old.setAttribute('aria-hidden', 'true'); old.setAttribute('inert', '');
      el.after(old);
      old.animate([{ opacity: 1 }, { opacity: 0 }], { duration: reduced.matches ? 120 : 240, easing: reduced.matches ? 'linear' : EASE, fill: 'forwards' }).finished.then(() => old.remove(), () => old.remove());
    }
    const copy = el.cloneNode(true) as HTMLElement;
    copy.querySelector('.dt-sweep')?.remove();
    drawn.current = { day: model.reply.date, by: barBy, pieces, copy };
  }, [bar]);
  // A lens whose number the day does not have (the day was changed) lets go, pinned or previewed by a chip that is not there to say it has left.
  useEffect(() => { if (ui.pinned && !nums.some(n => n.kind === ui.pinned)) ui.setPinned(null); if (ui.preview && !nums.some(n => n.kind === ui.preview)) ui.setPreview(null); }, [nums]);

  // A row that flashes (DayTimeline puts `is-flash` on it: a tap on a row with no detail, a jump from the bar) flashes its span here for the same 600 ms. The row's own animation
  // is the signal, so the bar needs nothing passed in; under reduced motion the row does not flash and neither does the bar.
  useEffect(() => {
    const root = box.current?.closest('.dt');
    if (!root) return;
    const seen = (e: Event) => {
      const key = (e.target as HTMLElement).dataset?.key;
      if ((e as AnimationEvent).animationName !== 'dt-flash' || !key) return;
      setFlash(key); clearTimeout(flashEnd.current); flashEnd.current = window.setTimeout(() => setFlash(null), FLASH_MS);
    };
    root.addEventListener('animationstart', seen);
    return () => { root.removeEventListener('animationstart', seen); clearTimeout(flashEnd.current); };
  }, [!!bar]);
  // A click away from the range card lets it go.
  useEffect(() => {
    if (!ui.sel) return;
    const down = (e: PointerEvent) => { if (!(e.target as Element | null)?.closest?.('.dt-card.is-range')) ui.setSel(null); };
    window.addEventListener('pointerdown', down, true);
    return () => window.removeEventListener('pointerdown', down, true);
  }, [!!ui.sel]);

  const laid = useMemo(() => bar ? bar.segs.map(s => { const w = (s.f1 - s.f0) * width - 1; return { s, w, how: fit(w, textWidth(s.short, s.mono), s.short) }; }) : [], [bar, width]);
  const boxes = useMemo(() => bar ? bar.sched.filter(x => x.kind === 'event').map(x => { const w = (x.f1 - x.f0) * width - 1; return { x, w, how: fit(w, textWidth(x.short, false), x.short) }; }) : [], [bar, width]);
  const ms = (x: number) => msOf(bar!, width ? x / width : 0);
  const bounds = () => { const el = box.current!, h = el.closest('.dt')!.getBoundingClientRect(), b = el.getBoundingClientRect(); return { min: h.left - b.left + EDGE, max: h.right - b.left - EDGE - CARD_W }; };
  const left = (center: number) => { const { min, max } = bounds(); return clamp(center - CARD_W / 2, min, Math.max(min, max)); };

  // The pointer: a press and a release within 3 px is a click, a press and a move of more is a range.
  const press = useRef<{ x0: number; drag: boolean } | null>(null);
  const at = (e: { clientX: number }) => clamp(e.clientX - track.current!.getBoundingClientRect().left, 0, track.current!.clientWidth);
  // On a door page what the pointer finds is the row whose mark is nearest it (within 8 px), not the day's piece under it.
  const near = (e: { clientX: number; clientY: number }) => door && bar && track.current ? nearestMark(door.marks, bar, track.current.clientWidth, at(e), e.clientY - track.current.getBoundingClientRect().top) : null;
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
    else { setHover({ x }); onHot(door ? near(e) : pieceAt(bar, ms(x))); }
  };
  // A drag that ends where it began (to the minute) is a click: a range of no length is none.
  const up = (e: ReactPointerEvent) => {
    const p = press.current;
    press.current = null;
    if (!p || !bar) return;
    const x = at(e), a = snap(ms(p.x0)), b = snap(ms(x));
    setBand(null);
    if (p.drag && a !== b) { ui.setSel({ a: Math.min(a, b), b: Math.max(a, b) }); return; }
    // A stretch nothing recorded asks what it was: the same input a range has (the row is selected as for any click).
    const hole = door ? null : pieceAt(bar, ms(x)), said = hole && model.segs.get(hole)?.unknown ? sayOf(model, hole) : null;
    if (said) onSay(said);
    if (open) { onPage(door ? near(e) : pieceAt(bar, ms(x))); return; }
    const key = door ? near(e) : rowAt(model, bar, ms(x));
    if (key) onJump(key);
  };
  const leave = () => { if (press.current) return; setHover(null); onHot(null); };

  if (!bar) return null;
  const sel = band ?? ui.sel, shown = sel ?? ui.filter, a = shown && Math.min(shown.a, shown.b), b = shown && Math.max(shown.a, shown.b);
  const summary = ui.sel ? summarize(model, bar, ui.sel.a, ui.sel.b, t) : null, lens = !door && ui.lens && nums.some(n => n.kind === ui.lens) ? ui.lens : null;
  // A day with no place in it (talks and no location) says so in the empty past, if the words fit there.
  const voidText = t(['No location recorded', '没有位置记录']), voidW = (bar.nowF ?? 1) * width, showVoid = bar.segs.length === 0 && voidW >= textWidth(voidText, false) + 2 * PAD_PX;
  // The sun's marks sit in the hours row. An hour's number always wins: a mark that would sit on one is only a tick under the bar.
  const hours = bar.ticks.map(k => ({ k, px: k.f < .02 ? 6 : k.f > .98 ? width - 6 : k.f * width }));
  const suns = bar.sun.map(x => { const px = clamp(x.f * width, 6, Math.max(6, width - 6)); return { ...x, px, bare: hours.some(h => Math.abs(h.px - px) < 14) }; });
  const bx = (x: number) => `${clamp(x, 0, 1) * 100}%`, edge = (x: number) => ({ left: `clamp(${PIN_HALF}px, ${x}px, calc(100% - ${PIN_HALF}px))` });
  // What is lit together: with the pointer on the bar, the piece under it (by place the place's, not the stretch's); otherwise the row the list has lit (under its pointer or chosen by the keys),
  // by place every place its span crosses. On a door page the pointer lifts none of the day's pieces, and the span marked is the row's own marks (a point is a 10 px box).
  type Bracket = { t0: number; t1: number; point?: boolean };
  const cursor = hover && !band && !door ? ms(hover.x) : null, under = cursor === null ? null : bar.segs.find(x => x.s <= cursor && cursor < x.e) ?? null;
  const underEvent = cursor === null || under ? null : bar.sched.find(x => x.kind === 'event' && x.s <= cursor && cursor <= x.e)?.key ?? null;
  const placed = barBy === 'place' && model.segs.size > 0, heard = !door && !hover && hot ? model.span.get(hot) : undefined;
  const lights = (s: BarSeg) => !door && (cursor !== null ? under === s : hot !== null && (placed ? !!heard && s.s < Math.max(heard.t1, heard.t0 + 1) && s.e > heard.t0 : s.row === hot));
  // A row that flashes flashes the pieces of the bar it is: its own, or those its span crosses.
  const flashed = flash !== null ? model.span.get(flash) : undefined, owned = flash !== null && bar.segs.some(x => x.row === flash);
  const pulses = (s: BarSeg) => flash !== null && (s.row === flash || !owned && !!flashed && s.s < Math.max(flashed.t1, flashed.t0 + 1) && s.e > flashed.t0);
  const opened: Bracket | null | undefined = open ? door ? extentOf(door.marks, open) : model.span.get(open) : null;
  const inBar = (x: Bracket | null | undefined): x is Bracket => !!x && x.t1 >= bar.t0 && x.t0 <= bar.t1;
  const geo = (x: Bracket): CSSProperties => x.point ? { left: `calc(${bx(fOf(bar, x.t0))} - 5px)`, width: 10 } : { left: bx(fOf(bar, x.t0)), width: `max(4px, ${(clamp(fOf(bar, x.t1), 0, 1) - clamp(fOf(bar, x.t0), 0, 1)) * 100}%)` };
  // The selection's frame is one piece that stays: it glides to the new span (the style does it), and fades when nothing is selected, leaving where it was. One that comes back to a bar with no
  // frame (the first, or another day's) is made in place, not glided in from nowhere.
  const framed = inBar(opened) ? opened : null, was = frame.current;
  if (framed) { if (!was || was.date !== model.reply.date) epoch.current++; frame.current = { date: model.reply.date, at: geo(framed) }; }
  else if (was && was.date !== model.reply.date) frame.current = null;
  // On a door page a row the pointer is on is only hinted: its marks have no piece of the day to light.
  const hint = door && hot && hot !== open ? extentOf(door.marks, hot) : null;
  // A door page's row that is lit (under the pointer, or chosen by the keys) says its mark's time under the bar, as a range does.
  const timesOf = (e: Extent) => {
    const f0 = clamp(fOf(bar, e.t0), 0, 1) * width, f1 = clamp(fOf(bar, e.t1), 0, 1) * width, one = e.point || f1 - f0 < 36;
    return one ? [{ x: (f0 + f1) / 2, text: e.point ? clock(e.t0, model.tz) : `${clock(e.t0, model.tz)}–${endClock(e.t1, model.reply, model.tz)}` }] : [{ x: f0, text: clock(e.t0, model.tz) }, { x: f1, text: endClock(e.t1, model.reply, model.tz) }];
  };
  const marked = door && !hover && !sel && hot ? extentOf(door.marks, hot) : null;
  // What is said under the bar, in the hours row: the minute at the pointer, a range's two ends (or its one time where they would meet), a lit door row's times.
  const pins: { x: number; text: string }[] = [];
  if (hover && !band) pins.push({ x: hover.x, text: clock(ms(hover.x), model.tz) });
  if (marked && marked.t1 >= bar.t0 && marked.t0 <= bar.t1) pins.push(...timesOf(marked));
  if (sel && a !== null && b !== null) {
    if ((fOf(bar, b) - fOf(bar, a)) * width < 36) pins.push({ x: (fOf(bar, a) + fOf(bar, b)) / 2 * width, text: `${clock(a, model.tz)}–${endClock(b, model.reply, model.tz)}` });
    else pins.push({ x: fOf(bar, a) * width, text: clock(a, model.tz) }, { x: fOf(bar, b) * width, text: endClock(b, model.reply, model.tz) });
  }
  const by = model.segs.size > 0 && <ByToggle value={barBy} onChange={k => setSettings({ barBy: k })} t={t}/>;
  return <>
    <div ref={box} className={`dt-bar${lens ? ' is-lens' : ''}${door ? ' is-page' : ''}${opened ? ' has-open' : ''}`} data-from={clock(bar.t0, model.tz)} role="group" aria-label={t(['The day in one bar', '一天的总览条'])} style={lens ? { '--k': rgb(KIND[lens]) } as CSSProperties : undefined}>
      <div className="dt-trackbox">
        <div ref={track} className="dt-track" onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={release} onLostPointerCapture={release} onPointerLeave={leave}>
          <i className="dt-ground" style={{ width: bar.nowF === null ? '100%' : bx(bar.nowF) }}/>
          {bar.nowF !== null && <i className="dt-ground is-future" style={{ left: bx(bar.nowF) }}/>}
          {!door && laid.map(({ s, w, how }) => <i key={s.key} className={`dt-seg is-${s.kind} is-${s.tone}${s.hole ? ' is-hole' : ''}${lights(s) ? ' is-hot' : ''}${pulses(s) ? ' is-pulse' : ''}`}
            style={{ left: `calc(${s.f0 * 100}% + .5px)`, width: `max(1px, calc(${(s.f1 - s.f0) * 100}% - 1px))`, '--c': rgb(s.rgb), '--a': s.a } as CSSProperties}>
            {how !== 'none' && <span className={`dt-sl${how === 'icon' ? ' is-icon' : ''}`}><Ic icon={s.hole ? 'unknown' : s.icon} size={12}/>{how === 'both' && <b className={s.mono ? 'is-mono' : ''}>{s.short}</b>}</span>}
          </i>)}
          {showVoid && !door && <i className="dt-void" style={{ width: bx(bar.nowF ?? 1) }}>{voidText}</i>}
          {!door && boxes.map(({ x, how }) => <i key={x.key} data-sched={x.key} className={`dt-sched is-event${hot === x.key && !door && cursor === null || underEvent === x.key ? ' is-hot' : ''}${flash === x.key ? ' is-pulse' : ''}${x.leaving ? ' is-leaving' : ''}`} style={{ left: `${x.f0 * 100}%`, width: `${(x.f1 - x.f0) * 100}%` }}>
            {how !== 'none' && <span className={`dt-sl${how === 'icon' ? ' is-icon' : ''}`}><Ic icon="event" size={12}/>{how === 'both' && <b>{x.short}</b>}</span>}
          </i>)}
          {!door && bar.sched.filter(x => x.kind !== 'event').map(x => <i key={x.key} className="dt-sched is-tick" style={{ left: `${x.f0 * 100}%` }}><u/></i>)}
          <Sweep on={sweep}/>
          {lens && (bar.lens[lens] ?? []).map((m, i) => <i key={i} className={`dt-lm${m.f1 - m.f0 < .004 ? ' is-point' : ''}`} style={{ left: `${m.f0 * 100}%`, width: `${Math.max(0, m.f1 - m.f0) * 100}%` }}/>)}
          {door && <DoorMarks marks={door.marks} bar={bar} mute={door.mute}/>}
        </div>
        <div className="dt-over" aria-hidden="true">
          {sel && a !== null && b !== null && <i className="dt-band" style={{ left: bx(fOf(bar, a)), width: `${(clamp(fOf(bar, b), 0, 1) - clamp(fOf(bar, a), 0, 1)) * 100}%` }}/>}
          {!sel && ui.filter && a !== null && b !== null && <i className="dt-band is-applied" style={{ left: bx(fOf(bar, a)), width: `${(clamp(fOf(bar, b), 0, 1) - clamp(fOf(bar, a), 0, 1)) * 100}%` }}/>}
          {inBar(hint) && <i className="dt-bracket" style={geo(hint)}/>}
          <i key={epoch.current} className={`dt-bracket is-open${framed ? '' : ' is-off'}`} style={frame.current?.at ?? { left: '50%', width: 0 }}/>
          {bar.nowF !== null && <i className="dt-nowtick" style={{ left: bx(bar.nowF) }}/>}
        </div>
      </div>
      <div className="dt-hours" aria-hidden="true">
        {hours.map(({ k, px }) => <span key={k.label} className={`${k.f < .02 ? 'is-start' : k.f > .98 ? 'is-end' : ''}${pins.some(x => Math.abs(x.x - px) < PIN_HALF + 10) ? ' is-under' : ''}`} style={{ left: `${k.f * 100}%` }}>{k.label}</span>)}
        {pins.map((x, i) => <i key={i} className="dt-minute" style={edge(x.x)}>{x.text}</i>)}
        {suns.map(x => <span key={x.kind} className={`dt-sun${x.bare ? ' is-tick' : ''}`} data-sun={x.kind} style={{ left: x.px }}>{!x.bare && <SunHorizon size={11}/>}</span>)}
      </div>
      {summary && <div className="dt-card is-range is-on" style={{ transform: `translateX(${left((fOf(bar, summary.a) + fOf(bar, summary.b)) / 2 * width)}px)` }}>
        <div className="dt-card-in"><div className="dt-slot is-cur"><RangeBody s={summary} say={sayRange(model, summary.a, summary.b)} onSay={onSay} t={t} onOnly={() => { ui.setFilter({ a: summary.a, b: summary.b }); ui.setSel(null); }}/></div></div>
      </div>}
    </div>
    {door ? door.numbers : (nums.length + model.vitals.length > 0 || by) && <div className="dt-numbers" role="group" aria-label={t(['The day in numbers', '这一天的数字'])}>
      <div ref={strip} className="dt-nums">
        {nums.map((n, i) => { const Glyph = KIND_ICON[n.kind], cut = i >= cutAt; return <button key={n.kind} type="button" className={`dt-num${ui.pinned === n.kind ? ' is-pinned' : ''}${cut ? ' is-cut' : ''}`} aria-pressed={ui.pinned === n.kind} aria-hidden={cut || undefined} tabIndex={cut ? -1 : undefined} data-chip={n.kind} style={{ '--k': rgb(KIND[n.kind]) } as CSSProperties}
          onPointerEnter={() => ui.setPreview(n.kind)} onPointerLeave={() => ui.setPreview(null)} onClick={() => ui.setPinned(ui.pinned === n.kind ? null : n.kind)}>
          <Glyph size={12} aria-hidden="true"/><span>{n.label}</span><em>{n.value}</em></button>; })}
        {model.vitals.map((v, i) => <span key={v.key} className={`dt-vit${nums.length + i >= cutAt ? ' is-cut' : ''}`} data-vital={v.key}>{v.before && <small>{v.before}</small>}<em>{v.value}</em>{v.after && <small>{v.after}</small>}</span>)}
      </div>
      {by}
    </div>}
    {cut && <div className="dt-filter"><span>{t(['Only ', '只看 '])}<b>{clock(Math.min(cut.a, cut.b), model.tz)}–{endClock(Math.max(cut.a, cut.b), model.reply, model.tz)}</b></span>
      <button type="button" aria-label={t(['Show the whole day', '看回整天'])} onClick={() => ui.setFilter(null)}><X size={11} weight="bold"/></button></div>}
  </>;
}

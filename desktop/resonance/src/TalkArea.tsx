import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode, type RefObject } from 'react';
import { ArrowUp, Keyboard, Microphone, Stop } from '@phosphor-icons/react';
import { Markdown, inline } from './Markdown';
import { tr, type L, type Lang } from './companionSettings';
import type { Line } from './model';
import { HEARD_MS, HEIGHT, LINGER_MS, MEMORY_MS, WIDTH, itemsOf, kindOf, pace, said as saidCount, sentences, type Captions, type Item, type Kind, type Voice } from './talk';
import './talk-area.css';

// Springs as CSS linear() curves: response in seconds, damping fraction (1 = no overshoot).
const linearOK = typeof CSS !== 'undefined' && CSS.supports('animation-timing-function', 'linear(0, 1)');
function spring(response: number, damping: number) {
  const w = 2 * Math.PI / response, z = damping, wd = w * Math.sqrt(Math.max(1e-4, 1 - z * z));
  const x = (t: number) => z >= 1 ? 1 - Math.exp(-w * t) * (1 + w * t) : 1 - Math.exp(-z * w * t) * (Math.cos(wd * t) + z * w / wd * Math.sin(wd * t));
  let T = 0;
  while (T < 2.5 && Math.abs(1 - x(T)) + Math.exp(-z * w * T) > .002) T += .01;
  const n = 40, points: number[] = [];
  for (let i = 0; i <= n; i++) points.push(+x(T * i / n).toFixed(4));
  points[n] = 1;
  return { d: Math.round(T * 1000), e: linearOK ? `linear(${points.join(',')})` : 'cubic-bezier(.2,.9,.3,1.08)' };
}
// Opening answers a touch with a slight rebound; closing does not rebound.
const OPEN = spring(.42, .8), CLOSE = spring(.3, 1);
const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const rise = (el: Element, delay = 120) => {
  if (!reduced()) el.animate([{ opacity: 0, translate: '0 6px', filter: 'blur(4px)' }, { opacity: 1, translate: '0 0', filter: 'blur(0)' }], { duration: 220, delay, easing: 'cubic-bezier(.2,.8,.2,1)', fill: 'backwards' });
};
// The written part comes up this long after her spoken line, so the area grows twice: to the line, then to the rest.
const STAGGER = 700;

type Spoken = { p: HTMLElement; chars: HTMLElement[]; spans: HTMLElement[]; lit: number };
type Registry = Map<string, Spoken>;
type Fly = { text: string; rect: DOMRect };

// Her spoken line, a span per character grouped by sentence: the driver lights them as she says them.
const Said = memo(function Said({ id, text, reg }: { id: string; text: string; reg: Registry }) {
  const el = useRef<HTMLParagraphElement>(null);
  useLayoutEffect(() => {
    const p = el.current!;
    reg.set(id, { p, chars: [...p.querySelectorAll<HTMLElement>('i')], spans: [...p.children] as HTMLElement[], lit: -1 });
    return () => { reg.delete(id); };
  }, [id, text, reg]);
  return <p ref={el} className="tk-s" data-id={id}>{sentences(text).map((sentence, i) => <span key={i}>{sentence.map((ch, j) => <i key={j}>{ch}</i>)}</span>)}</p>;
});

// The written part: runs of list items become the grouped list (a time, then the words as written), headings and everything else
// go through the same Markdown the Dashboard uses.
const ITEM = /^(\s*)([-*•]|\d+[.)])\s+(.*)$/, TIME = /^(\d{1,2}[:：]\d{2}(?:\s*[-–~～至]\s*\d{1,2}[:：]\d{2})?)\s+(.*)$/;
function Written({ text }: { text: string }) {
  const out: ReactNode[] = [];
  let md: string[] = [], rows: { lead: string; body: string }[] = [];
  const flushMd = () => { if (md.join('').trim()) out.push(<Markdown key={out.length} text={md.join('\n')}/>); md = []; };
  const flushRows = () => {
    if (rows.length) out.push(<div className="doc" key={out.length}>{rows.map((row, i) => <div key={i}>{row.lead && <time>{row.lead}</time>}<span>{inline(row.body)}</span></div>)}</div>);
    rows = [];
  };
  for (const line of text.split('\n')) {
    const m = ITEM.exec(line);
    if (m && !m[1]) {
      flushMd();
      const when = TIME.exec(m[3]);
      rows.push(when ? { lead: when[1], body: when[2] } : { lead: /^\d/.test(m[2]) ? m[2] : '', body: m[3] });
    } else if (rows.length && (m || /^\s+\S/.test(line))) rows[rows.length - 1].body += ` ${(m ? m[3] : line).trim()}`;
    else { flushRows(); md.push(line); }
  }
  flushRows(); flushMd();
  return <>{out}</>;
}

function Her({ it, reg, think, ready }: { it: Item; reg: Registry; think: string; ready: boolean }) {
  return <div className={`tk-h ${it.failed ? 'is-err' : ''}`} data-line={it.id}>
    {think && <small className="tk-think">{think}</small>}
    {it.spoken && <Said id={it.id} text={it.spoken} reg={reg}/>}
    {it.written && ready && <div className="tk-w" data-written><Written text={it.written}/></div>}
  </div>;
}

export type TalkProps = {
  lang: Lang; x: number; y: number;
  // Whether it is on screen at all, the caption level that applies now, and what there is to show.
  open: boolean; level: Captions; lines: Line[];
  voice: Voice; hearing: boolean;
  // Her voice is off: nothing is being said, so her words are all there to read, not lit as they go.
  silent: boolean;
  // Think mode (ADR 0064) for this turn: the deep look, the seconds counting, and how long each deep answer took.
  deep: { look: boolean; secs: number; thoughts: { turn: string; secs: number }[] };
  field: boolean; draft: string; micPaused: boolean;
  onDraft: (value: string) => void; onSend: () => void; // `empty`: closing it leaves nothing to show.
  onField: (open: boolean, empty?: boolean) => void; onMic: () => void; onEnd: () => void;
  // `onUp`: it is up (she stays out); false from the moment it folds into her. `onSettle`: a shape change has come to rest.
  onUp: (up: boolean) => void; onSettle: () => void;
  boxRef: RefObject<HTMLDivElement | null>; inputRef: RefObject<HTMLTextAreaElement | null>;
};
type Row = 'ft' | 'fd';

export function TalkArea(p: TalkProps) {
  const t = (l: L) => tr(p.lang, l);
  const box = p.boxRef, trEl = useRef<HTMLDivElement>(null), ftEl = useRef<HTMLDivElement>(null), fdEl = useRef<HTMLFormElement>(null), flyEl = useRef<HTMLDivElement>(null);
  const live = useRef(p); live.current = p;
  const [, redraw] = useState(0);
  const [row, setRow] = useState<Row>('ft'), [away, setAway] = useState(false), [fieldH, setFieldH] = useState(36);
  const reg = useRef<Registry>(new Map()), clocks = useRef(new Map<string, { text: string; clock: number[] }>());
  // The motion's own state: where the shape is, what is pending, and whether the reader has scrolled away from her.
  const ctl = useRef({ at: 'gone' as 'gone' | Kind, closing: false, timers: [] as number[], staged: false, follow: true, progUntil: 0, wheelAt: 0, since: 0, fly: null as Fly | null, rise: false, back: false, shape: { w: 0, h: 0 }, lbW: 0, litEl: null as HTMLElement | null });
  const c = ctl.current;

  const items = useMemo(() => itemsOf(p.lines, p.level), [p.lines, p.level]);
  // The pill and the footer show what you just said for a moment, so you see what she heard.
  const lastYou = useMemo(() => [...p.lines].reverse().find(l => l.who === 'you'), [p.lines]);
  const freshMs = p.level === 'brief' && lastYou ? lastYou.at + HEARD_MS - Date.now() : 0;
  // It fades out first, then the pill closes up around what is left.
  const FADE = 220, fading = freshMs > 0 && freshMs <= FADE;
  useEffect(() => {
    if (freshMs <= 0) return;
    const id = window.setTimeout(() => redraw(n => n + 1), freshMs > FADE ? freshMs - FADE : freshMs + 20);
    return () => clearTimeout(id);
  }, [freshMs, lastYou?.id]);
  // Her written part is held back a moment behind her spoken line.
  const now = Date.now();
  const ready = (it: Item) => !it.spoken || now - it.at >= STAGGER;
  const waits = items.filter(it => it.written && !ready(it)).map(it => it.at + STAGGER - now);
  useEffect(() => {
    if (!waits.length) return;
    const id = window.setTimeout(() => redraw(n => n + 1), Math.min(...waits) + 20);
    return () => clearTimeout(id);
  });

  const state = p.voice === 'off' || p.silent && p.voice === 'speaking' ? 'idle' : p.voice === 'speaking' ? 'speaking' : p.voice === 'thinking' ? 'thinking' : p.hearing ? 'hearing' : 'listening';
  const secs = p.deep.secs;
  const status = state === 'idle' ? '' : state === 'thinking' ? secs > 0 ? t([`Thinking ${secs} s`, `深想 ${secs} 秒`]) : t(['Thinking', '在想'])
    : state === 'speaking' ? t(['Speaking · poke to interrupt', '在说 · 戳她打断']) : t(['Listening', '在听']);
  const heard = freshMs > 0 && lastYou ? lastYou.text.replace(/\s+/g, ' ') : '';
  const expanded = items.some(it => it.at > c.since);
  const kind = kindOf(p.level, items, p.field, expanded);
  // The pill shows what you just said, then nothing but the state's glyph (and, with the middle level, the seconds a deep answer is taking);
  // the footer under the area says what she is doing.
  const label = kind === 'pill' ? heard || (p.level === 'brief' && state === 'thinking' && secs > 0 ? status : '') : heard || status;
  // While it folds away it keeps what it was showing.
  const cur = { items, kind, state, label, heard: !!heard, fading, deep: p.deep.look, ready };
  const frozen = useRef(cur);
  if (p.open) frozen.current = cur;
  const v = frozen.current;
  const shim = v.state === 'thinking' && !v.heard;

  // ---- shape ----
  // The natural size of what it is now showing, measured on a hidden copy: the live element is mid-animation, and measuring it
  // returns the in-between height. It is measured without the 360 cap, so that how much there is to scroll is known. `bare`: as just a
  // row of state, without the transcript.
  const measure = (width: number | null, bare?: Kind) => {
    const b = box.current!, copy = b.cloneNode(true) as HTMLElement;
    copy.removeAttribute('data-hit'); copy.removeAttribute('data-glass'); copy.removeAttribute('inert');
    copy.querySelectorAll('[id]').forEach(e => e.removeAttribute('id'));
    Object.assign(copy.style, { width: width === null ? 'max-content' : `${width}px`, height: 'auto', maxHeight: 'none', visibility: 'hidden', pointerEvents: 'none', transition: 'none', animation: 'none', opacity: '1', scale: '1', translate: '-50% 0' });
    if (bare) { copy.dataset.kind = bare; copy.querySelector<HTMLElement>('.talk-tr')!.style.display = 'none'; }
    b.after(copy);
    const size = { w: copy.offsetWidth, h: copy.offsetHeight };
    copy.remove();
    return size;
  };
  const later = (ms: number, run: () => void) => { c.timers.push(window.setTimeout(run, ms)); };
  const stopTimers = () => { c.timers.forEach(clearTimeout); c.timers = []; };
  // The pill's label grows or shrinks by the same spring as the shape, so the end button stays inside the shape, pinned to its right
  // edge, while it widens to show what you said or closes up around the glyph.
  const lbEl = () => ftEl.current?.querySelector<HTMLElement>('.lb') ?? null;
  const labelWidth = () => { const el = lbEl(); if (el && !ftEl.current!.hidden) c.lbW = el.offsetWidth; };
  const growLabel = (sp: { d: number; e: string }) => {
    const el = lbEl(); if (!el || ftEl.current!.hidden) return;
    const mine = el.getAnimations().filter(a => a.id === 'lbw'), now = el.offsetWidth;
    mine.forEach(a => a.cancel());
    const to = el.offsetWidth, from = mine.length ? now : c.lbW; c.lbW = to;
    if (!reduced() && Math.abs(from - to) > .5) el.animate([{ width: `${from}px` }, { width: `${to}px` }], { duration: sp.d, easing: sp.e, id: 'lbw' });
  };
  const morph = (w: number, h: number, r: number, sp = OPEN, fit = false) => {
    if (fit) growLabel(sp);
    const b = box.current!, cs = getComputedStyle(b);
    const from = { width: cs.width, height: cs.height, borderRadius: cs.borderTopLeftRadius }, to = { width: `${w}px`, height: `${h}px`, borderRadius: `${r}px` };
    b.getAnimations().filter(a => a.id === 'shape').forEach(a => a.cancel());
    Object.assign(b.style, to);
    c.shape = { w, h };
    if (!reduced()) b.animate([from, to], { duration: sp.d, easing: sp.e, id: 'shape' }).finished.then(() => live.current.onSettle(), () => undefined);
    else live.current.onSettle();
  };
  const rowEl = () => row === 'fd' ? fdEl.current! : ftEl.current!;
  const rowH = () => { const e = rowEl(); return e.hidden ? 44 : e.offsetHeight; };
  // Words cut off above or below fade out at that edge.
  const fades = () => {
    const el = trEl.current; if (!el) return;
    el.classList.toggle('more', el.scrollTop > 2);
    el.classList.toggle('below', el.scrollHeight - el.scrollTop - el.clientHeight > 2);
  };
  // Scrolled to the newest, given the height all of it would take at full width (the live element may still be narrower, and so taller).
  const toEnd = (natural: number, smooth = false) => {
    const el = trEl.current!, top = Math.max(0, natural - HEIGHT);
    if (smooth && !reduced()) { c.progUntil = performance.now() + 700; el.scrollTo({ top, behavior: 'smooth' }); }
    else { el.scrollTop = top; fades(); }
  };
  const clearInline = () => {
    const b = box.current!;
    b.getAnimations({ subtree: true }).forEach(a => a.cancel());
    for (const k of ['width', 'height', 'border-radius', 'opacity', 'visibility', 'translate', 'scale']) b.style.removeProperty(k);
    for (const e of [trEl.current, ftEl.current, fdEl.current] as HTMLElement[]) for (const k of ['width', 'height', 'box-sizing', 'align-self', 'flex', 'opacity', 'display']) e.style.removeProperty(k);
  };
  // A capsule that is about to hold words widens to 360 first, then grows down; the words come up once the shape is in.
  const widenThenGrow = () => {
    const tr = trEl.current!, hasWords = tr.childElementCount > 0;
    if (hasWords) { tr.style.opacity = '0'; c.staged = true; }
    morph(WIDTH, measure(WIDTH, 'area').h, 22);
    later(140, () => {
      const natural = measure(WIDTH).h, grown = Math.min(natural, HEIGHT);
      morph(WIDTH, grown, 22);
      c.follow = true; setAway(false); toEnd(natural);
      if (hasWords) { tr.style.removeProperty('opacity'); rise(tr); }
      c.staged = false; riseNew();
    });
  };
  // Grow (or shrink) to what is showing now.
  const settle = (to: Kind) => {
    if (to === 'area') {
      const natural = measure(WIDTH).h, h = Math.min(natural, HEIGHT);
      labelWidth();
      if (c.shape.w < WIDTH - 4) widenThenGrow();
      else {
        if (Math.abs(c.shape.h - h) > .5) morph(WIDTH, h, 22);
        if (!c.staged && c.follow) { if (c.litEl) followLit(c.litEl); else toEnd(natural, true); }
      }
    } else {
      const n = measure(null), h = to === 'pill' ? 36 : 44;
      if (Math.abs(c.shape.w - n.w) > .5 || Math.abs(c.shape.h - h) > .5) morph(n.w, h, h / 2, OPEN, true); else labelWidth();
    }
    c.at = to;
  };
  // Each line that is new comes up (6 pt, out of a blur), unless the whole transcript is coming up with the shape.
  const riseNew = () => {
    const el = trEl.current!;
    el.querySelectorAll<HTMLElement>('[data-line]:not([data-seen])').forEach(n => { n.setAttribute('data-seen', ''); if (!c.staged && !c.fly) rise(n); });
    el.querySelectorAll<HTMLElement>('[data-written]:not([data-seen])').forEach(w => {
      w.setAttribute('data-seen', '');
      if (c.staged) return;
      [...w.children].forEach(part => {
        if (part.classList.contains('doc') && !reduced()) [...part.children].forEach((r, i) => r.animate([{ opacity: 0, translate: '0 6px' }, { opacity: 1, translate: '0 0' }], { duration: 260, delay: 120 + i * 90, easing: 'cubic-bezier(.2,.8,.2,1)', fill: 'backwards' }));
        else rise(part);
      });
    });
  };
  // It grows out from under her as a capsule, then to what there is to show.
  const appear = (to: Kind) => {
    const b = box.current!;
    clearInline(); stopTimers();
    c.staged = false; c.follow = true; setAway(false);
    Object.assign(b.style, { visibility: 'visible', opacity: '1', width: '36px', height: '36px', borderRadius: '18px' });
    c.shape = { w: 36, h: 36 };
    live.current.onUp(true);
    if (!reduced()) b.animate([{ opacity: 0, scale: '.6', translate: '-50% -14px' }, { opacity: 1, scale: '1', translate: '-50% 0' }], { duration: OPEN.d, easing: OPEN.e, delay: 70, fill: 'backwards' }); // she comes out of the island first
    if (to === 'area') widenThenGrow();
    else { const n = measure(null); c.lbW = 0; morph(n.w, to === 'pill' ? 36 : 44, to === 'pill' ? 18 : 22, OPEN, true); }
    c.at = to;
    rise(rowEl(), 60);
    riseNew();
  };
  // Folding away: the words freeze in place and fade with a blur while the area folds to a capsule, which shrinks up into her and fades.
  const close = () => {
    const b = box.current!;
    c.closing = true; stopTimers();
    const finish = () => { clearInline(); c.at = 'gone'; c.closing = false; c.since = Date.now(); c.shape = { w: 0, h: 0 }; redraw(n => n + 1); };
    if (reduced()) { live.current.onUp(false); finish(); return; }
    const folding = c.at === 'area' || c.at === 'capsule' && trEl.current!.childElementCount > 0;
    let wait = 0;
    if (folding) {
      for (const e of [trEl.current!, ftEl.current!, fdEl.current!]) Object.assign(e.style, { width: `${e.offsetWidth}px`, height: `${e.offsetHeight}px`, boxSizing: 'border-box', alignSelf: 'center', flex: 'none' });
      [...trEl.current!.children, rowEl()].forEach(e => e.animate([{ opacity: 1, filter: 'blur(0)' }, { opacity: 0, filter: 'blur(3px)' }], { duration: 180, fill: 'forwards' }));
      morph(Math.min(measure(null, 'capsule').w, 200), 44, 22, CLOSE);
      wait = 180 + Math.round(CLOSE.d * .4);
    }
    later(wait, () => {
      morph(36, 36, 18, CLOSE);
      b.animate([{ opacity: 1, scale: '1', translate: '-50% 0' }, { opacity: 0, scale: '.5', translate: '-50% -30px' }], { duration: 300, easing: 'cubic-bezier(.5,0,.9,.4)', fill: 'forwards' });
      later(140, () => live.current.onUp(false));
      later(340, finish);
    });
  };
  // Opened again while folding: carry on from the shape it has now.
  const abortClose = () => {
    const b = box.current!, cs = getComputedStyle(b), w = cs.width, h = cs.height, r = cs.borderTopLeftRadius;
    stopTimers(); clearInline();
    Object.assign(b.style, { visibility: 'visible', opacity: '1', width: w, height: h, borderRadius: r });
    c.closing = false; c.shape = { w: parseFloat(w), h: parseFloat(h) };
    live.current.onUp(true);
  };

  // ---- the words she is saying ----
  const clockFor = (it: Item) => {
    const hit = clocks.current.get(it.id);
    if (hit && hit.text === it.spoken) return hit.clock;
    const clock = pace(it.spoken); clocks.current.set(it.id, { text: it.spoken, clock }); return clock;
  };
  const paint = (r: Spoken, lit: number) => {
    if (r.lit === lit) return;
    const [a, b] = r.lit < 0 ? [0, r.chars.length] : [Math.min(r.lit, lit), Math.max(r.lit, lit)];
    for (let j = a; j < b; j++) r.chars[j].classList.toggle('on', j < lit);
    let k = 0;
    for (const sp of r.spans) { const end = k + sp.children.length; sp.classList.toggle('done', end < lit && lit < r.chars.length); k = end; }
    r.p.classList.toggle('all', lit >= r.chars.length);
    r.lit = lit;
  };
  // Light up as far as she has got. The daemon only says when she has finished, so how far is estimated from when she began.
  const view = useRef(v); view.current = v;
  const drive = () => {
    let front: HTMLElement | null = null;
    for (const it of view.current.items) {
      const r = it.who === 'her' && it.spoken ? reg.current.get(it.id) : undefined;
      if (!r) continue;
      const total = r.chars.length, done = it.failed || live.current.silent || (it.said && it.cutAt === undefined);
      const lit = done ? total : it.queued ? 0 : Math.min(total, saidCount(clockFor(it), ((it.cutAt ?? Date.now()) - it.from) / 1000));
      paint(r, lit);
      if (!done && !it.queued && it.cutAt === undefined) front = r.chars[Math.max(0, lit - 1)] ?? null;
    }
    c.litEl = front;
    return front;
  };
  const followLit = (front: HTMLElement | null) => {
    const el = trEl.current;
    if (!el || !front || !c.follow || c.at !== 'area' || c.staged) return;
    // The shape may still be growing: follow by the room it is growing to, not the room it has now.
    const room = Math.min(c.shape.h, HEIGHT) - rowH();
    const top = el.getBoundingClientRect().top, bottom = front.getBoundingClientRect().bottom - top + el.scrollTop;
    if (bottom > el.scrollTop + room - 12) {
      c.progUntil = performance.now() + 700;
      el.scrollTo({ top: Math.max(0, Math.min(bottom - room * .7, el.scrollHeight - room)), behavior: reduced() ? 'auto' : 'smooth' });
    }
  };
  const speaking = v.items.some(it => it.who === 'her' && it.spoken && !it.said);
  const steps = useRef({ drive, followLit }); steps.current = { drive, followLit };
  useEffect(() => {
    if (!speaking) return;
    const id = window.setInterval(() => steps.current.followLit(steps.current.drive()), 90);
    return () => clearInterval(id);
  }, [speaking]);

  // ---- the bottom row: your state, or the field you type in ----
  const swapTo = (want: Row) => {
    if (c.at === 'gone' || c.closing || reduced() || !live.current.open) { setRow(want); return; }
    const from = want === 'fd' ? ftEl.current! : fdEl.current!;
    if (c.fly) { setRow(want); c.rise = true; c.back = true; return; }
    from.animate([{ opacity: 1, scale: '1' }, { opacity: 0, scale: '.97' }], { duration: 120, easing: 'ease-in', fill: 'forwards' });
    later(120, () => { from.getAnimations().forEach(a => a.cancel()); setRow(want); c.rise = true; });
  };
  useLayoutEffect(() => {
    const want: Row = p.field ? 'fd' : 'ft';
    if (want !== row) swapTo(want);
  }, [p.field, row]);
  useEffect(() => {
    if (row !== 'fd' || !p.open) return;
    const ta = p.inputRef.current; if (!ta) return;
    const ids = [0, 120, 320, 700].map(ms => window.setTimeout(() => { if (document.activeElement !== ta) ta.focus({ preventScroll: true }); }, ms));
    return () => ids.forEach(clearTimeout);
  }, [row, p.open]);
  // ---- keep the shape and the words in step with what is showing ----
  const sig = [p.open, v.kind, row, v.items.map(it => `${it.id}:${it.spoken.length}:${it.written.length}:${v.ready(it)}`).join(), v.label, fieldH, p.level, p.silent].join('|');
  useLayoutEffect(() => {
    drive();
    if (!p.open) { if (c.at !== 'gone' && !c.closing) close(); return; }
    if (c.closing) abortClose();
    if (c.at === 'gone' && row !== (p.field ? 'fd' : 'ft')) return; // the row it opens on is on its way
    if (c.at === 'gone') appear(v.kind);
    else { settle(v.kind); riseNew(); }
    if (c.rise && row === (p.field ? 'fd' : 'ft')) { c.rise = false; if (c.back) { c.back = false; rowEl().animate([{ opacity: 0 }, { opacity: 1 }], { duration: 260, delay: Math.round(OPEN.d * .5), easing: 'ease-out', fill: 'backwards' }); /* after the sent words have left it */ } else rise(rowEl(), 0); }
    runFly();
    followLit(drive());
    fades();
  }, [sig]);
  useEffect(() => {
    const el = trEl.current; if (!el) return;
    const watch = new ResizeObserver(fades);
    watch.observe(el);
    return () => watch.disconnect();
  }, []);
  useEffect(() => () => { stopTimers(); }, []);

  // The field grows with its words, to six lines, then scrolls.
  // (Its width is still changing while the area opens, so it is fitted again whenever that changes; empty, it is one line, whatever
  // its placeholder would take at a width it does not have yet.)
  const fit = () => {
    const ta = p.inputRef.current; if (!ta) return;
    ta.style.height = 'auto';
    const h = ta.value ? Math.max(36, Math.min(ta.scrollHeight, 136)) : 36;
    ta.style.height = `${h}px`;
    setFieldH(h);
  };
  useLayoutEffect(fit, [p.draft, row]);
  useEffect(() => {
    const ta = p.inputRef.current; if (!ta || row !== 'fd') return;
    let width = ta.offsetWidth;
    const watch = new ResizeObserver(() => { if (ta.offsetWidth !== width) { width = ta.offsetWidth; fit(); } });
    watch.observe(ta);
    return () => watch.disconnect();
  }, [row]);

  // Sending: a copy of your words flies from the field to its place in the transcript, shrinking and dimming on the way. It is drawn in
  // the area, not in the transcript, which clips what leaves it.
  const submit = () => {
    const ta = p.inputRef.current, text = p.draft.trim();
    if (!text) return;
    if (ta && !reduced()) c.fly = { text, rect: ta.getBoundingClientRect() };
    c.follow = true; setAway(false);
    p.onSend();
  };
  const runFly = () => {
    const f = c.fly; if (!f) return;
    const mine = [...trEl.current!.querySelectorAll<HTMLElement>('.tk-u')].at(-1);
    if (row === 'fd' || !p.open) return;
    c.fly = null;
    const host = flyEl.current!, b = box.current!;
    if (!mine || mine.dataset.flown) return;
    mine.dataset.flown = '1'; mine.style.visibility = 'hidden';
    const done = () => { mine.style.removeProperty('visibility'); ghost.remove(); };
    const pr = b.getBoundingClientRect(), ghost = document.createElement('span');
    ghost.className = 'tk-ghost'; ghost.textContent = f.text;
    Object.assign(ghost.style, { left: `${f.rect.left + 14 - pr.left}px`, top: `${f.rect.top + 8 - pr.top}px`, width: `${f.rect.width - 28}px` });
    host.appendChild(ghost);
    toEnd(measure(WIDTH).h);
    const to = mine.getBoundingClientRect();
    ghost.animate([{ translate: '0 0', scale: '1' }, { translate: `${to.left - (f.rect.left + 14)}px ${to.top - (f.rect.top + 8)}px`, scale: '.893', color: 'rgb(238 240 251 / .55)' }],
      { duration: OPEN.d, easing: OPEN.e, fill: 'forwards' }).finished.then(done, done);
  };

  // ---- reading back through it ----
  const onScroll = () => {
    const el = trEl.current!;
    fades();
    // Only a scroll the reader made counts: ours (following her words) leaves the follow on.
    if (performance.now() - c.wheelAt > 450) return;
    const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (atEnd !== c.follow) { c.follow = atEnd; setAway(!atEnd); }
  };
  const latest = () => {
    const el = trEl.current!;
    c.follow = true; setAway(false); c.progUntil = performance.now() + 700;
    const front = c.litEl;
    if (front) followLit(front);
    el.scrollTo({ top: el.scrollHeight, behavior: reduced() ? 'auto' : 'smooth' });
  };

  const field = <form ref={fdEl} className="talk-fd" hidden={row !== 'fd'} onSubmit={e => { e.preventDefault(); submit(); }}>
    <button type="button" className={`ib mic ${p.micPaused ? 'dim' : ''}`} aria-label={t(['Back to voice', '回到语音'])} title={p.micPaused ? t(['Microphone paused while you type. Back to voice', '打字时麦克风暂停，点一下回到语音']) : t(['Talk instead', '改用语音'])} onClick={p.onMic}><Microphone/></button>
    <textarea ref={p.inputRef} className="box" rows={1} aria-label={t(['Type to her', '文字输入'])} value={p.draft} enterKeyHint="send"
      placeholder={p.micPaused ? t(['The microphone pauses while you type', '打字时麦克风暂停']) : t(['Say something…', '和她说点什么…'])}
      onChange={e => p.onDraft(e.target.value)}
      onKeyDown={(e: KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Escape') { e.preventDefault(); p.onField(false, v.items.length === 0); }
        else if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); }
      }}/>
    <button type="submit" className={`send ${p.draft.trim() ? '' : 'off'}`} disabled={!p.draft.trim()} aria-label={t(['Send', '发送'])}><ArrowUp weight="bold"/></button>
  </form>;

  return <div ref={box} className="talk" data-kind={v.kind} data-state={v.state} data-deep={v.deep || undefined} data-hit={p.open || undefined} data-glass="css" inert={!p.open}
    style={{ left: p.x, top: p.y }} role="region" aria-label={t(['Conversation', '对话'])}>
    <span className="tk-deep" aria-hidden="true"/>
    <div ref={trEl} className="talk-tr" role="log" aria-live="polite" onScroll={onScroll} onWheel={() => { c.wheelAt = performance.now(); }}>
      {v.items.map(it => it.who === 'you'
        ? <span key={it.id} className="tk-u" data-line={it.id}>{it.spoken}</span>
        : <Her key={it.id} it={it} reg={reg.current} ready={v.ready(it)} think={(() => { const secs = p.deep.thoughts.find(th => th.turn === it.turn)?.secs; return secs ? t([`Thought for ${secs.toFixed(1)} s`, `想了 ${secs.toFixed(1)} 秒`]) : ''; })()}/>)}
    </div>
    {away && v.items.length > 0 && <div className="tk-latest"><button type="button" onClick={latest}>{t(['Back to latest', '回到最新'])}</button></div>}
    <div ref={ftEl} className="talk-ft" hidden={row !== 'ft'}>
      <span className={`gl ${v.state}`} aria-hidden="true"><b/><b/><b/></span>
      <span className={`lb ${shim ? 'shim' : ''} ${v.heard ? 'heard' : ''} ${v.fading ? 'fade' : ''}`} key={v.heard ? 'heard' : v.state}>{v.label}</span>
      <span className="sp"/>
      <button type="button" className="ib kb" aria-label={t(['Type to her', '文字输入'])} onClick={() => p.onField(true)}><Keyboard/></button>
      <button type="button" className="ib st" aria-label={t(['End voice', '结束语音'])} onClick={p.onEnd}><Stop weight="fill"/></button>
    </div>
    {field}
    <div ref={flyEl} className="tk-fly" aria-hidden="true"/>
  </div>;
}

// When it is up: from the moment she starts listening (or you press the keyboard) until a turn is over and she is not listening,
// then the area folds away LINGER_MS later, never while the pointer is over it. Opening again within MEMORY_MS continues the same
// conversation; after that it starts empty.
export function usePresence({ engaged, over, onOpen }: { engaged: boolean; over: () => boolean; onOpen: (stale: boolean) => void }) {
  const [open, setOpen] = useState(false);
  const closedAt = useRef(0), armed = useRef(true), seen = useRef(0), now = useRef(engaged);
  const latest = useRef({ over, onOpen }); latest.current = { over, onOpen };
  now.current = engaged;
  useEffect(() => {
    if (!engaged) armed.current = true; // an end the person asked for ignores whatever is still winding down
    if (engaged && armed.current && !open) { latest.current.onOpen(closedAt.current > 0 && Date.now() - closedAt.current > MEMORY_MS); setOpen(true); }
  }, [engaged]);
  useEffect(() => {
    if (!open || engaged) return;
    const idle = Date.now();
    let timer = 0;
    const check = () => {
      if (latest.current.over()) seen.current = Date.now();
      if (Date.now() - idle >= LINGER_MS && Date.now() - seen.current >= 1000) { closedAt.current = Date.now(); setOpen(false); }
      else timer = window.setTimeout(check, 250);
    };
    timer = window.setTimeout(check, 250);
    return () => clearTimeout(timer);
  }, [open, engaged]);
  // An end the person asked for while it is still engaged waits for that to wind down (the next engaged-to-idle edge re-arms it); when
  // nothing is engaged there is no such edge to come, and the next conversation must open it.
  const dismiss = () => { if (now.current) armed.current = false; closedAt.current = Date.now(); setOpen(false); };
  return { open, dismiss };
}

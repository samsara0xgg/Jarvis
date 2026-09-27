import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import { ArrowSquareOut, X } from '@phosphor-icons/react';
import { AGENT_NAME, openLabel, type Agent, type AgentState } from './agents';
import { AgentMark, CELL, COUNT_W, drawMark, dpr, seedOf, useClock, type MarkLook } from './AgentMarks';
import { drawTurnIcon, hueAt, rgba, tint } from './beacon';
import { ended } from './Notices';
import { spring, step } from './starCore';

// Beside the notch, after the notch lab (ADR 0057). Right of the camera: the "your turn" beacon, then a star per
// session, working, stopped, finished. A colour shows up to three stars; from four it folds into one star with a
// count. Resting on a star grows its panel down out of the notch; everything hangs from the top edge as one black
// shape, the pops and needs-you cards too. A finished or stopped star you have seen stays, dimmer, until you drag
// it down out of the menu bar or press Clear.
type Point = { x: number; y: number };
type Group = 'work' | 'err' | 'done';
type Box = { key: string; kind: 'turn' | 'one' | 'stack'; group: Group; ids: string[]; x0: number; x1: number; cx: number };
type Rect = { l: number; r: number; d: number };
export type NotchGeo = { width: number; top: number; notchR: number; baseL: number };
export type NotchAct = { jump: (a: Agent) => void; answer: (id: string) => void; read: (ids: string[]) => void; clear: (ids: string[]) => void };
// A pop names sessions; a card is a needs-you card the companion builds.
export type NotchNote = { key: string; pop?: string[]; card?: ReactNode; onClose: () => void };

const GROUPS: Group[] = ['work', 'err', 'done'];
// Compacting is a working session tidying its context for a moment, so it counts with working.
const groupOf = (st: AgentState): Group => st === 'err' || st === 'done' ? st : 'work';
const GLABEL: Record<Group, string> = { work: 'working', err: 'stopped', done: 'finished' };
const STATE: Record<AgentState, string> = { wait: 'Needs you', err: 'Stopped', done: 'Done', work: 'Working', pack: 'Compacting' };
// Your turn: asking first, then stopped, then finished.
const TURN_ORDER: AgentState[] = ['wait', 'err', 'done'];
const FOLD_AT = 4, PAD = 6, TURN_W = 18 + COUNT_W + 3, DROP_W = 320, CARD_W = 480, DRAG_OUT = 30, FX_H = 150;
const clamp = (v: number, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, v));
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const plainLine = (text: string) => text.replace(/[*`#>]/g, '').trim();

// ---------- the black shape: everything hangs from the top edge ----------
// Each rect is [left, right] with a depth from the screen top. The outline of their union has concave shoulders
// where it meets the screen edge and rounded steps wherever the depth changes, so the notch, the stars and whatever
// grows out of them read as one piece.
function runPath(run: Rect[]) {
  const S = 6, first = run[0], last = run.at(-1)!, rb = (s: Rect) => Math.min(22, s.d / 3, (s.r - s.l) / 2);
  const r0 = rb(first);
  let p = `M ${first.l - S} -20 L ${first.l - S} 0 Q ${first.l} 0 ${first.l} ${S} L ${first.l} ${first.d - r0} Q ${first.l} ${first.d} ${first.l + r0} ${first.d}`;
  for (let i = 0; i < run.length - 1; i++) {
    const a = run[i], b = run[i + 1], x = a.r, gap = Math.abs(b.d - a.d);
    if (b.d > a.d) {
      const rv = Math.min(rb(b), gap / 2), rc = Math.min(12, gap - rv, (a.r - a.l) / 2);
      p += ` L ${x - rc} ${a.d} Q ${x} ${a.d} ${x} ${a.d + rc} L ${x} ${b.d - rv} Q ${x} ${b.d} ${x + rv} ${b.d}`;
    } else {
      const rv = Math.min(rb(a), gap / 2), rc = Math.min(12, gap - rv, (b.r - b.l) / 2);
      p += ` L ${x - rv} ${a.d} Q ${x} ${a.d} ${x} ${a.d - rv} L ${x} ${b.d + rc} Q ${x} ${b.d} ${x + rc} ${b.d}`;
    }
  }
  const r1 = rb(last);
  return p + ` L ${last.r - r1} ${last.d} Q ${last.r} ${last.d} ${last.r} ${last.d - r1} L ${last.r} ${S} Q ${last.r} 0 ${last.r + S} 0 L ${last.r + S} -20 Z`;
}
function skyline(rects: Rect[]) {
  const rs = rects.filter(r => r.r - r.l > .5 && r.d > .5);
  const xs = [...new Set(rs.flatMap(r => [r.l, r.r]))].sort((a, b) => a - b), segs: Rect[] = [];
  for (let i = 0; i < xs.length - 1; i++) {
    const a = xs[i], b = xs[i + 1];
    if (b - a < .01) continue;
    const m = (a + b) / 2, d = Math.max(0, ...rs.filter(r => r.l <= m && r.r >= m).map(r => r.d)), last = segs.at(-1);
    if (last && Math.abs(last.d - d) < .01 && Math.abs(last.r - a) < .01) last.r = b; else segs.push({ l: a, r: b, d });
  }
  let path = '', run: Rect[] = [];
  const flush = () => { if (run.length) path += runPath(run); run = []; };
  for (const s of segs) {
    if (s.d <= 0) { flush(); continue; }
    if (run.length && Math.abs(run.at(-1)!.r - s.l) > .01) flush();
    run.push(s);
  }
  flush();
  return path;
}
// Width follows the content (the longest name), between a floor and a ceiling.
function autoWidth(el: HTMLElement, min: number, max: number) {
  el.style.width = 'max-content'; el.style.maxWidth = `${max}px`;
  // offsetWidth rounds; two spare pixels keep a name that just fits from tipping into an ellipsis.
  const w = Math.round(clamp(el.offsetWidth + 2, min, Math.max(min, max)));
  el.style.width = `${w}px`; el.style.maxWidth = '';
  return w;
}

// ---------- what the panels show ----------
// The "your turn" icon at `size` css px, for a panel's label.
function TurnMark({ look, size = 11 }: { look: MarkLook; size?: number }) {
  const ref = useRef<HTMLCanvasElement>(null), box = size * 1.6;
  useClock(now => {
    const cv = ref.current;
    if (!cv || !cv.checkVisibility({ opacityProperty: true, visibilityProperty: true })) return false;
    const d = dpr(), w = Math.round(box * d), k = size / 16;
    if (cv.width !== w) cv.width = cv.height = w;
    const ctx = cv.getContext('2d')!;
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, w, w); ctx.setTransform(d * k, 0, 0, d * k, w / 2, w / 2);
    drawTurnIcon(ctx, look, now / 1000, 99, d * k);
    return true;
  });
  return <span className="agent-mark" style={{ position: 'relative', display: 'inline-block', flex: 'none', width: size, height: size }}>
    <canvas ref={ref} aria-hidden="true" style={{ position: 'absolute', left: (size - box) / 2, top: (size - box) / 2, width: box, height: box }}/></span>;
}
function Head({ a, look }: { a: Agent; look: MarkLook }) {
  return <div className="c-head"><AgentMark look={look} state={a.state} id={a.id} size={12}/>
    <div className="c-t"><div className="c-top"><b>{a.title}</b><span className="age">{a.age}</span></div>
      <div className="c-meta"><span className={`tagc ${a.agent}`}>{AGENT_NAME[a.agent]}</span>{a.project && <span>{a.project}</span>}
        {a.branch && <><span className="dot">·</span><span>{a.branch.replace(/^worktree-/, '')}</span></>}</div></div></div>;
}
// What one session is doing or came to, and what you can do about it: a single star's panel, or a row opened in a stack.
function Detail({ a, act }: { a: Agent; act: NotchAct }) {
  const open = openLabel(a), go = open && <button type="button" className="btn btn-ghost" onClick={() => act.jump(a)}>{open}<ArrowSquareOut size={12}/></button>;
  if (ended(a.state)) return <>
    <p className="c-now is-end"><b>{a.state === 'err' ? 'Stopped' : 'Result'}</b><span>{plainLine(a.state === 'err' ? a.error || a.last : a.last) || 'Done'}</span></p>
    <div className="c-choice">{go}<button type="button" className="btn btn-ghost" onClick={() => act.clear([a.id])}><X size={12}/>Clear</button></div></>;
  return <><p className="c-now"><b>Now</b><span>{plainLine(a.last) || STATE[a.state]}</span></p>{go && <div className="c-choice">{go}</div>}</>;
}
function Peek({ a, look, act }: { a: Agent; look: MarkLook; act: NotchAct }) {
  return <div className="nt-card"><div className="c-bar"><span className={`c-label is-${a.state}`}><i/>{STATE[a.state]}</span></div>
    <Head a={a} look={look}/>{a.you && <p className="c-you"><b>You</b><span>{a.you}</span></p>}<Detail a={a} act={act}/></div>;
}
// One session on your turn: its star and name. Asking opens its card (or its app, when Jarvis holds no prompt);
// finished or stopped goes to it, and ✕ marks it read without going.
function TurnRow({ a, look, act, tag }: { a: Agent; look: MarkLook; act: NotchAct; tag?: ReactNode }) {
  const ask = a.state === 'wait', go = () => ask && a.request ? act.answer(a.id) : act.jump(a);
  return <div className={`u-row${ask ? ' is-ask' : ''}`} role="button" tabIndex={0} data-id={a.id}
    title={ask ? a.request ? 'Asking you · open the card to answer' : `Waiting for you in ${a.where}` : 'Go to it'}
    onClick={go} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } }}>
    <AgentMark look={look} state={a.state} id={a.id} size={12}/><span>{a.title}{tag}</span>
    {ask ? <span/> : <span className="u-acts"><i><ArrowSquareOut size={12}/></i>
      <i role="button" aria-label="Mark as read" title="Mark as read" className="u-read" onClick={e => { e.stopPropagation(); act.read([a.id]); }}><X size={12}/></i></span>}
  </div>;
}
function TurnList({ turn, look, act }: { turn: Agent[]; look: MarkLook; act: NotchAct }) {
  const done = turn.filter(a => a.state !== 'wait');
  return <div className="nt-card"><div className="c-bar"><span className="c-label is-turn"><TurnMark look={look}/>Your turn · {turn.length}</span>
    {done.length > 0 && <button type="button" className="c-x" onClick={() => act.read(done.map(a => a.id))}>Mark finished read</button>}</div>
    <div className="u-list">{turn.map(a => <TurnRow key={a.id} a={a} look={look} act={act}/>)}</div></div>;
}
// A folded colour: a star and a name per row. A click opens a row in place; a click anywhere on it that is not a
// button folds it back. Pointing does nothing.
function Stack({ group, members, open, toggle, look, act }: { group: Group; members: Agent[]; open: ReadonlySet<string>; toggle: (id: string) => void; look: MarkLook; act: NotchAct }) {
  return <div className="nt-card"><div className="c-bar"><span className={`c-label is-${group}`}><i/>{members.length} {GLABEL[group]}</span>
    {group !== 'work' && <button type="button" className="c-x" onClick={() => act.clear(members.map(a => a.id))}>Clear all</button>}</div>
    <div className="s-list">{members.map(a => <div key={a.id} className={`s-row${open.has(a.id) ? ' is-open' : ''}`} data-id={a.id}
      onClick={e => { if (!(e.target as Element).closest('button')) toggle(a.id); }}>
      <div className="s-head" role="button" tabIndex={0} aria-expanded={open.has(a.id)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(a.id); } }}>
        <AgentMark look={look} state={a.state} id={a.id} size={12}/><b>{a.title}</b></div>
      {open.has(a.id) && <div className="s-body"><Detail a={a} act={act}/></div>}
    </div>)}</div></div>;
}
function Pop({ agents, look, act, onClose }: { agents: Agent[]; look: MarkLook; act: NotchAct; onClose: () => void }) {
  const errs = agents.filter(a => a.state === 'err').length, all = errs === agents.length, n = agents.length;
  const label = n === 1 ? errs ? 'Stopped' : 'Done' : all ? `${n} stopped` : errs ? `${n} need a look` : `${n} done`;
  return <div className="nt-card pop"><div className="c-bar"><span className={`c-label is-${all ? 'err' : 'done'}`}><i/>{label}</span>
    <button type="button" className="c-x" aria-label="Close" onClick={onClose}><X size={12}/></button></div>
    <div className="u-list">{agents.map(a => <TurnRow key={a.id} a={a} look={look} act={act} tag={a.state === 'err' && !all ? <em> stopped</em> : null}/>)}</div></div>;
}

export function Notch({ look, agents, unread, cleared, geo, cursor, note, quiet, act, onNoteHover }: {
  look: MarkLook; agents: Agent[]; unread: ReadonlySet<string>; cleared: ReadonlySet<string>; geo: NotchGeo;
  cursor: RefObject<Point>; note: NotchNote | null; quiet: boolean; act: NotchAct; onNoteHover: (on: boolean) => void;
}) {
  const turn = agents.filter(a => a.state === 'wait' || ended(a.state) && unread.has(a.id)).sort((a, b) => TURN_ORDER.indexOf(a.state) - TURN_ORDER.indexOf(b.state));
  // A star per session that is not on your turn and not cleared.
  const stars = agents.filter(a => a.state !== 'wait' && !(ended(a.state) && (unread.has(a.id) || cleared.has(a.id))));
  const [dropFor, setDropFor] = useState<string | null>(null), [openRows, setOpenRows] = useState<ReadonlySet<string>>(new Set()), [dragging, setDragging] = useState(false);
  const root = useRef<HTMLDivElement>(null), shape = useRef<SVGPathElement>(null), fx = useRef<HTMLCanvasElement>(null), hit = useRef<HTMLDivElement>(null);
  const drop = useRef<HTMLDivElement>(null), dropIn = useRef<HTMLDivElement>(null), noteP = useRef<HTMLDivElement>(null), noteIn = useRef<HTMLDivElement>(null);
  const st = useRef({
    boxes: [] as Box[], wingTarget: 0, opened: false, dirty: true, lastCx: 0, innerL: 0, lastIn: 0, wantKey: null as string | null, wantAt: 0, dropFor: null as string | null,
    dropGoal: { w: DROP_W, d: 0 }, noteGoal: { w: 24, d: 0 }, noteL: 0, noteRest: 0, onNote: false,
    s: { ww: spring(0), dx: spring(0), dw: spring(24), dd: spring(0), nx: spring(0), nw: spring(24), nd: spring(0) },
    press: null as { box: Box; x: number; y: number } | null, drag: null as { box: Box; x: number; y: number } | null,
    puffs: [] as { x: number; y: number; c: string; at: number }[], seen: new Map<string, AgentState>(), changed: new Map<string, number>(), turnN: 0, turnAt: -1e9,
  }).current;
  const L = useRef({ look, turn, stars, geo, note, quiet, act, onNoteHover });
  L.current = { look, turn, stars, geo, note, quiet, act, onNoteHover };
  // The panels keep showing what they showed while they shrink away.
  const lastDrop = useRef<string | null>(null), lastNote = useRef<NotchNote | null>(null);
  if (dropFor) lastDrop.current = dropFor;
  if (note) lastNote.current = note;
  // Who a star stands for right now, which can differ from when the row was laid out while it held still.
  const membersOf = (box: Box): Agent[] => box.kind === 'turn' ? L.current.turn
    : L.current.stars.filter(a => box.kind === 'stack' ? groupOf(a.state) === box.group : a.id === box.ids[0]);
  const members = (key: string | null): Agent[] => { const box = key ? st.boxes.find(b => b.key === key) : undefined; return box ? membersOf(box) : []; };
  const openDrop = (key: string | null) => { if (key === st.dropFor) return; st.dropFor = key; st.dirty = true; setDropFor(key); setOpenRows(new Set()); };
  const toggle = (id: string) => setOpenRows(rows => { const next = new Set(rows); if (!next.delete(id)) next.add(id); return next; });
  useLayoutEffect(() => { st.dirty = true; });
  useEffect(() => {
    const ro = new ResizeObserver(() => { st.dirty = true; });
    [dropIn.current!, noteIn.current!].forEach(el => ro.observe(el));
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    let raf = 0, timer: ReturnType<typeof setTimeout> | undefined, last = 0;
    // The stars and the panes, laid out and drawn. True while something is still moving.
    const frame = (now: number, dt: number) => {
      const { look, turn, stars, geo: g, note, quiet, onNoteHover } = L.current, s = st.s, top = g.top;
      const firm = reduced.matches, go = (sp: typeof s.ww, goal: number, hz: number, damp: number) => step(sp, goal, hz, firm ? 1 : damp, dt);
      const p = cursor.current, ww = Math.max(0, s.ww.value);
      const inWing = !!p && p.y <= top + 2 && p.x >= g.notchR && p.x <= g.notchR + ww + 2;
      // Each colour in the wing, left to right. Nothing moves while the pointer is on the wing or a panel is open:
      // the stars keep their places, their looks and counts follow along, and the row catches up once you leave.
      if (st.drag || !st.boxes.length || !(st.dropFor || inWing)) {
        const held = st.drag?.box.ids ?? [], slots: Omit<Box, 'x0' | 'x1' | 'cx'>[] = [];
        if (turn.length) slots.push({ key: 'turn', kind: 'turn', group: 'work', ids: turn.map(a => a.id) });
        for (const group of GROUPS) {
          const ts = stars.filter(a => groupOf(a.state) === group && !held.includes(a.id));
          if (ts.length >= FOLD_AT) slots.push({ key: `stack:${group}`, kind: 'stack', group, ids: ts.map(a => a.id) });
          else slots.push(...ts.map(a => ({ key: a.id, kind: 'one' as const, group, ids: [a.id] })));
        }
        let x = g.notchR + PAD;
        st.boxes = slots.map(b => {
          const w = b.kind === 'turn' ? TURN_W : CELL[look] + (b.kind === 'stack' ? COUNT_W : 0);
          const box = { ...b, x0: x, x1: x + w, cx: x + (b.kind === 'turn' ? 10 : CELL[look] / 2) };
          x += w;
          return box;
        });
        st.wingTarget = slots.length ? x + PAD - g.notchR : 0;
        // What the row shows, for the checks: `turn2 work done workx4 ...`.
        const marks = st.boxes.map(b => b.kind === 'turn' ? `turn${turn.length}` : b.kind === 'stack' ? `${b.group}x${b.ids.length}` : stars.find(a => a.id === b.ids[0])?.state).join(' ');
        if (root.current!.dataset.marks !== marks) root.current!.dataset.marks = marks;
      }
      // Resting on a star opens its panel after 90 ms; from one panel to the next is at once; it closes 260 ms
      // after the pointer has left both. A notice closes it.
      if (!st.drag) {
        const inDrop = !!p && !!st.dropFor && p.x >= s.dx.value && p.x <= s.dx.value + s.dw.value && p.y >= top - 2 && p.y <= s.dd.value;
        const slot = inWing ? st.boxes.find(b => p!.x >= b.x0 && p!.x < b.x1) : undefined;
        let want: string | null = null;
        if (!note && !quiet && slot) { want = slot.key; st.lastIn = now; } else if (!note && !quiet && inDrop) { want = st.dropFor; st.lastIn = now; }
        if (want !== st.wantKey) { st.wantKey = want; st.wantAt = now; }
        if (want && want !== st.dropFor && (st.dropFor || now - st.wantAt > 90)) openDrop(want);
        else if (!want && st.dropFor && (note || quiet || now - st.lastIn > 260)) openDrop(null);
        if (st.dropFor && !members(st.dropFor).length) openDrop(null);
      }
      // The page opens with the stars already out, not grown in from nothing.
      if (!st.opened) { st.opened = true; s.ww.value = st.wingTarget; }
      let moving = go(s.ww, st.wingTarget, 3.2, .8);
      const W = g.width, dIn = dropIn.current!, nIn = noteIn.current!;
      if (st.dirty) {
        st.dirty = false;
        const box = st.boxes.find(b => b.key === st.dropFor);
        if (st.dropFor && box) {
          // Your turn is as wide as its longest name; past the screen edge it slides left instead of cutting names.
          const w = st.dropFor === 'turn' ? autoWidth(dIn, 240, Math.min(440, W - 16)) : (dIn.style.width = `${DROP_W}px`, DROP_W);
          st.dropGoal = { w, d: top + dIn.offsetHeight + 2 };
        }
        if (note) {
          let w: number;
          // A pop hangs where your turn lives, so it shrinks straight into the beacon; a card hangs in the middle.
          if (note.pop) { st.noteL = g.notchR - 6; w = autoWidth(nIn, 180, W - st.noteL - 8); st.noteRest = st.noteL + 12; }
          else { w = Math.min(CARD_W, W - 16); nIn.style.width = `${w}px`; st.noteL = W / 2 - w / 2; st.noteRest = W / 2; }
          st.noteGoal = { w, d: Math.min(top + 640, top + nIn.offsetHeight + 2) };
        }
      }
      // The drop under a star (or the list under the beacon).
      const box = st.dropFor ? st.boxes.find(b => b.key === st.dropFor) : undefined;
      if (box) st.lastCx = box.cx;
      const open = !!box, dw = st.dropGoal.w;
      const dg = open ? { l: clamp(st.lastCx - (st.dropFor === 'turn' ? 22 : dw / 2), 8, W - dw - 8), w: dw, d: st.dropGoal.d } : { l: st.lastCx - 12, w: 24, d: top * .6 };
      if (open && s.dd.value < top) { s.dx.value = st.lastCx - 12; s.dw.value = 24; }
      moving = go(s.dx, dg.l, 3.4, .86) || moving; moving = go(s.dw, dg.w, 3.4, .86) || moving; moving = go(s.dd, dg.d, 3.2, .8) || moving;
      // The notice: a pop beside the beacon, or a card in the middle.
      const noteOpen = !!note;
      const ng = noteOpen ? { l: st.noteL, w: st.noteGoal.w, d: st.noteGoal.d } : { l: st.noteRest - 12, w: 24, d: top * .6 };
      if (noteOpen && s.nd.value < top) { s.nx.value = st.noteRest - 12; s.nw.value = 24; }
      moving = go(s.nx, ng.l, 2.9, .84) || moving; moving = go(s.nw, ng.w, 2.9, .84) || moving; moving = go(s.nd, ng.d, 2.9, .8) || moving;
      const onNote = noteOpen && !!p && p.x >= s.nx.value && p.x <= s.nx.value + s.nw.value && p.y >= top && p.y <= s.nd.value;
      if (onNote !== st.onNote) { st.onNote = onNote; onNoteHover(onNote); }
      // One black piece: the notch, the stars, and whatever hangs from them.
      const wingR = g.notchR + Math.max(0, s.ww.value), rects: Rect[] = [];
      if (wingR > g.notchR + .5) rects.push({ l: g.baseL, r: wingR, d: top });
      if (open || s.dd.value > top + 1) rects.push({ l: s.dx.value, r: s.dx.value + s.dw.value, d: s.dd.value });
      if (noteOpen || s.nd.value > top + 1) rects.push({ l: s.nx.value, r: s.nx.value + s.nw.value, d: s.nd.value });
      shape.current!.setAttribute('d', skyline(rects));
      if (open) st.innerL = dg.l;
      place(drop.current!, dIn, s.dx.value, s.dw.value, s.dd.value, st.innerL, st.dropGoal.d, open, top);
      place(noteP.current!, nIn, s.nx.value, s.nw.value, s.nd.value, st.noteL, st.noteGoal.d, noteOpen, top);
      Object.assign(hit.current!.style, { left: `${g.notchR}px`, width: `${Math.max(0, s.ww.value)}px`, height: `${top}px` });
      draw(now, top, look);
      return moving || !!st.drag || st.puffs.length > 0;
    };
    const place = (pane: HTMLElement, inner: HTMLElement, left: number, w: number, d: number, goalL: number, goalD: number, open: boolean, top: number) => {
      const h = Math.max(0, d - top), k = open ? clamp(h / Math.max(1, goalD - top)) : 0;
      pane.style.transform = `translate(${left}px,${top}px)`; pane.style.width = `${Math.max(0, w)}px`; pane.style.height = `${h}px`;
      pane.style.visibility = h < 1 ? 'hidden' : 'visible';
      pane.classList.toggle('is-open', open && k > .6);
      inner.style.left = `${goalL - left}px`;
      inner.style.opacity = String(open ? clamp((k - .45) / .5) : clamp(h / 60) * .6);
    };
    // The stars, clipped to the wing as it grows; a star being dragged; the puffs left where one was cleared.
    const draw = (now: number, top: number, look: MarkLook) => {
      const cv = fx.current!, { geo: g, turn, stars } = L.current, d = dpr(), W = g.width, t = now / 1000;
      if (cv.width !== Math.round(W * d) || cv.height !== Math.round(FX_H * d)) { cv.width = Math.round(W * d); cv.height = Math.round(FX_H * d); }
      const ctx = cv.getContext('2d')!;
      ctx.setTransform(d, 0, 0, d, 0, 0); ctx.clearRect(0, 0, W, FX_H);
      // A session changing state, or joining a folded colour, replays its flourish.
      for (const a of stars) { if (st.seen.get(a.id) !== a.state) { if (st.seen.has(a.id)) st.changed.set(a.id, now); st.seen.set(a.id, a.state); } }
      if (turn.length > st.turnN) st.turnAt = now;
      st.turnN = turn.length;
      const byId = new Map(stars.map(a => [a.id, a]));
      ctx.save(); ctx.beginPath(); ctx.rect(g.notchR, 0, Math.max(0, st.s.ww.value), top); ctx.clip();
      for (const b of st.boxes) {
        const one = b.kind === 'one' ? byId.get(b.ids[0]) : undefined, state: AgentState = one ? one.state : b.group;
        const dim = b.kind !== 'turn' && ended(state), count = b.kind === 'turn' ? turn.length : b.kind === 'stack' ? stars.filter(a => groupOf(a.state) === b.group).length : 0;
        ctx.save(); ctx.translate(b.cx, top / 2);
        if (dim) ctx.globalAlpha = .6;
        if (b.kind === 'turn') drawTurnIcon(ctx, look, t, (now - st.turnAt) / 1000, d);
        else { const since = Math.max(0, ...b.ids.map(id => st.changed.get(id) ?? 0)); drawMark(ctx, look, state, t + seedOf(b.key), since ? (now - since) / 1000 : 99, d); }
        ctx.restore();
        if (count) {
          ctx.save(); if (dim) ctx.globalAlpha = .6;
          ctx.font = '600 9px "JetBrains Mono", Menlo, monospace'; ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
          ctx.fillStyle = b.kind === 'turn' ? rgba(tint(hueAt(t), .45)) : 'rgba(220,226,250,.9)';
          ctx.fillText(String(count), b.kind === 'turn' ? b.x0 + 19 : b.x0 + CELL[look] - 1, top / 2 + .5); ctx.restore();
        }
        if (st.dropFor === b.key) {
          const c = b.kind === 'turn' ? hueAt(t).join(',') : { wait: '255,201,143', err: '255,106,90', done: '111,224,180', work: '108,156,255', pack: '187,148,255' }[state];
          ctx.fillStyle = `rgba(${c},.9)`; ctx.shadowColor = `rgba(${c},.9)`; ctx.shadowBlur = 6 * d;
          ctx.beginPath(); ctx.roundRect(b.cx - 5, top - 3.5, 10, 1.6, .8); ctx.fill(); ctx.shadowBlur = 0;
        }
      }
      ctx.restore();
      const dr = st.drag;
      if (dr) {
        const out = dr.y > top + DRAG_OUT, n = membersOf(dr.box).length, state = dr.box.kind === 'one' ? byId.get(dr.box.ids[0])?.state ?? dr.box.group : dr.box.group;
        ctx.save(); ctx.translate(dr.x, dr.y);
        ctx.fillStyle = '#000'; ctx.beginPath(); ctx.arc(0, 0, n > 1 ? 13 : 11, 0, Math.PI * 2); ctx.fill();
        ctx.globalAlpha = out ? .55 : 1;
        drawMark(ctx, look, state, t + seedOf(dr.box.key), 99, d);
        if (dr.box.kind === 'stack') { ctx.font = '600 9px "JetBrains Mono", Menlo, monospace'; ctx.textBaseline = 'middle'; ctx.fillStyle = 'rgba(220,226,250,.9)'; ctx.fillText(String(n), 7, 1); }
        ctx.globalAlpha = 1; ctx.font = '600 10.5px -apple-system, "PingFang SC", sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
        ctx.fillStyle = 'rgba(255,255,255,.95)'; ctx.shadowColor = 'rgba(0,0,0,.6)'; ctx.shadowBlur = 4 * d;
        ctx.fillText(out ? 'Let go to clear' : 'Drag out of the menu bar', 0, 16);
        ctx.restore();
      }
      st.puffs = st.puffs.filter(f => now - f.at < 480);
      for (const f of st.puffs) {
        const k = (now - f.at) / 480;
        ctx.fillStyle = `rgba(${f.c},${1 - k})`;
        for (let i = 0; i < 8; i++) { const a = i / 8 * Math.PI * 2, r = 4 + 14 * k; ctx.beginPath(); ctx.arc(f.x + Math.cos(a) * r, f.y + Math.sin(a) * r, 1.6 * (1 - k * .6), 0, Math.PI * 2); ctx.fill(); }
      }
    };
    // Full speed while anything moves; otherwise the marks' 30 fps, and 10 fps with nothing beside the notch.
    const tick = (now: number) => {
      const dt = Math.min(last ? (now - last) / 1000 : 1 / 60, 1 / 20);
      last = now;
      if (frame(now, dt)) raf = requestAnimationFrame(tick);
      else { raf = 0; timer = setTimeout(() => { raf = requestAnimationFrame(tick); }, st.boxes.length || L.current.note ? 33 : 100); }
    };
    raf = requestAnimationFrame(tick);
    return () => { cancelAnimationFrame(raf); clearTimeout(timer); };
  }, []);

  // Press a finished or stopped star (or its stack) and pull it down out of the menu bar to clear it; anywhere
  // higher, it goes back.
  const letGo = (clear: boolean) => {
    const d = st.drag;
    st.press = st.drag = null; setDragging(false);
    if (!d || !clear || d.y <= geo.top + DRAG_OUT) return;
    const ids = membersOf(d.box).map(a => a.id);
    if (!ids.length) return;
    st.puffs.push({ x: d.x, y: d.y, c: d.box.group === 'err' ? '255,122,102' : '111,224,180', at: performance.now() });
    act.clear(ids);
  };
  const dropKey = dropFor ?? lastDrop.current, dropBox = st.boxes.find(b => b.key === dropKey), shownNote = note ?? lastNote.current;
  const list = members(dropKey);
  const dropNode = !dropKey || !list.length ? null : dropKey === 'turn' ? <TurnList turn={list} look={look} act={act}/>
    : dropBox?.kind === 'stack' ? <Stack group={dropBox.group} members={list} open={openRows} toggle={toggle} look={look} act={act}/> : <Peek a={list[0]} look={look} act={act}/>;
  const popAgents = shownNote?.pop?.map(id => agents.find(a => a.id === id)).filter((a): a is Agent => !!a) ?? [];
  return <div ref={root} className="notch">
    <svg className="notch-shape" aria-hidden="true"><path ref={shape}/></svg>
    <canvas ref={fx} className="notch-fx" data-look={look} aria-hidden="true" style={{ width: geo.width, height: FX_H }}/>
    <div ref={hit} className="notch-hit" data-hit aria-hidden="true"
      onPointerDown={e => {
        const b = st.boxes.find(x => e.clientX >= x.x0 && e.clientX < x.x1);
        if (!b || b.kind === 'turn' || b.group === 'work') return;
        st.press = { box: b, x: e.clientX, y: e.clientY }; e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={e => {
        if (!st.press) return;
        const q = { x: e.clientX, y: e.clientY };
        if (!st.drag && Math.hypot(q.x - st.press.x, q.y - st.press.y) > 4) { st.drag = { box: st.press.box, ...q }; setDragging(true); openDrop(null); }
        if (st.drag) Object.assign(st.drag, q);
      }}
      onPointerUp={() => letGo(true)} onPointerCancel={() => letGo(false)}/>
    <div ref={drop} className="notch-pane notch-drop" data-hit={dropFor ? true : undefined} role="dialog" aria-label="Session">
      <div ref={dropIn} className="notch-pane-in">{dropNode}</div></div>
    <div ref={noteP} className="notch-pane notch-note" data-hit={note ? true : undefined} role="alertdialog" aria-label="Agent notice">
      <div ref={noteIn} className="notch-pane-in">{shownNote && (shownNote.pop ? popAgents.length > 0 && <Pop agents={popAgents} look={look} act={act} onClose={shownNote.onClose}/> : shownNote.card)}</div></div>
    {dragging && <div className="notch-catch" data-hit aria-hidden="true"/>}
  </div>;
}

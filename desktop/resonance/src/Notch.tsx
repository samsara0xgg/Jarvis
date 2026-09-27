import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type ReactNode, type RefObject } from 'react';
import { Archive, ArrowSquareOut, ArrowUUpLeft, ArrowUp, CaretLeft, CaretRight, Moon, X } from '@phosphor-icons/react';
import { openLabel, readConversation, requestLine, sendReply, type Agent, type AgentState, type Said } from './agents';
import { AgentMark, drawMark, dpr, seedOf, useClock, type MarkLook } from './AgentMarks';
import { drawMoon, drawTurnIcon, hueAt, MOON_RGB, rgba, tint } from './beacon';
import { Markdown } from './Markdown';
import { ended } from './Notices';
import { spring, step } from './starCore';

// Beside the notch, after the notch lab (ADR 0069, 0070). Right of the camera, one mark per group with its count:
// your turn (the beacon), working, finished, parked (the moon). Only working moves; the beacon sends out its rings
// for a few seconds after news. Resting anywhere on the row opens one panel that is the whole island growing down,
// every session one line; its pops and needs-you cards grow out of the island the same way. ⌥Tab opens the same
// panel and holds it for the keys: ↑↓ pick a session, → opens its card (asking) or its page (the conversation,
// with a box that types a reply into the session), ⏎ goes to it, Esc closes. A finished session is archived from
// the panel or by dragging the finished mark down out of the menu bar; anything on your turn can be parked.
type Point = { x: number; y: number };
type Kind = 'turn' | 'work' | 'done' | 'moon';
type Box = { key: Kind; x0: number; x1: number; cx: number };
type Rect = { l: number; r: number; d: number };
type Sec = { key: Kind; label: string; ts: Agent[] };
type Keys = { view: 'list' | 'page'; id: string; i: number; at: number };
export type NotchGeo = { width: number; top: number; notchR: number; lobeL: number };
export type NotchAct = {
  jump: (a: Agent) => void; answer: (id: string) => void; read: (ids: string[]) => void; back: () => void;
  archive: (ids: string[]) => void; park: (ids: string[]) => void; unpark: (ids: string[]) => void;
};
// A pop names sessions; a card is a needs-you card the companion builds, for session `id` when it has one.
export type NotchNote = { key: string; id?: string; pop?: string[]; card?: ReactNode; onClose: () => void };

// Your turn: asking first, then stopped, then finished.
const TURN_ORDER: AgentState[] = ['wait', 'err', 'done'];
const PAD = 6, GCELL = 30, GCX = 9, ALL_W = 400, PAGE_W = 520, CARD_W = 480, DRAG_OUT = 30, FX_H = 150, DWELL_MS = 1500;
const clamp = (v: number, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, v));
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const working = (st: AgentState) => st === 'work' || st === 'pack';

// ---------- the black shape: everything hangs from the top edge ----------
// Each rect is [left, right] with a depth from the screen top. The outline of their union has concave shoulders
// where it meets the screen edge and rounded steps wherever the depth changes, so her island, the notch, the marks
// and whatever grows out of them read as one piece.
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
// The beacon or the moon at `size` css px, for a panel's label.
function IconMark({ look, moon = false, size = 11 }: { look: MarkLook; moon?: boolean; size?: number }) {
  const ref = useRef<HTMLCanvasElement>(null), box = size * 1.6;
  useClock(now => {
    const cv = ref.current;
    if (!cv || !cv.checkVisibility({ opacityProperty: true, visibilityProperty: true })) return false;
    const d = dpr(), w = Math.round(box * d), k = size / 16;
    if (cv.width !== w) cv.width = cv.height = w;
    const ctx = cv.getContext('2d')!;
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, w, w); ctx.setTransform(d * k, 0, 0, d * k, w / 2, w / 2);
    if (moon) drawMoon(ctx, 99, d * k); else drawTurnIcon(ctx, look, now / 1000, 99, d * k);
    return true;
  });
  return <span className="agent-mark" style={{ position: 'relative', display: 'inline-block', flex: 'none', width: size, height: size }}>
    <canvas ref={ref} aria-hidden="true" style={{ position: 'absolute', left: (size - box) / 2, top: (size - box) / 2, width: box, height: box }}/></span>;
}
// What a session is at, in a few words: the question it asks, what it is doing, or how it ended and when.
const statusOf = (a: Agent) => a.state === 'wait' ? a.request ? requestLine(a.request) : `Waiting for you in ${a.where}`
  : a.state === 'err' ? `Stopped${a.error ? ` · ${a.error}` : ''}` : a.state === 'done' ? `Done · ${a.age}` : a.state === 'pack' ? 'Compacting' : a.last || 'Working';
const asking = (a: Agent) => a.state === 'wait' && !!a.request;
function Act({ tip, onClick, children }: { tip: string; onClick: () => void; children: ReactNode }) {
  return <i role="button" aria-label={tip} title={tip} onClick={e => { e.stopPropagation(); onClick(); }}>{children}</i>;
}
// One session on one line: a still star, its name (bold while it needs a look), what it is at, and on hover what
// can be done about it. A click opens the card of one that is asking, else goes to it.
function Row({ a, kind, cur, look, act, page }: { a: Agent; kind: Kind; cur: boolean; look: MarkLook; act: NotchAct; page: (id: string) => void }) {
  const open = openLabel(a), go = () => asking(a) ? act.answer(a.id) : act.jump(a);
  const acts = <>
    {!asking(a) && open && <Act tip={open} onClick={() => act.jump(a)}><ArrowSquareOut size={12}/></Act>}
    {!asking(a) && <Act tip="What it said · reply" onClick={() => page(a.id)}><CaretRight size={12}/></Act>}
    {kind === 'turn' && <Act tip="Park it: out of your turn, no reminders, until you take it back" onClick={() => act.park([a.id])}><Moon size={12} weight="fill"/></Act>}
    {kind === 'turn' && a.state !== 'wait' && <Act tip="Mark as read" onClick={() => act.read([a.id])}><X size={12}/></Act>}
    {kind === 'moon' && <Act tip="Take back to your turn" onClick={() => act.unpark([a.id])}><ArrowUUpLeft size={12}/></Act>}
    {(kind === 'done' || kind === 'moon' && a.state !== 'wait') && <Act tip="Archive: done with it, kept to find again" onClick={() => act.archive([a.id])}><Archive size={12}/></Act>}
  </>;
  return <div className={`a-row${a.state === 'wait' ? ' is-ask' : ''}${kind === 'turn' ? ' is-due' : ''}${cur ? ' is-cur' : ''}`} role="button" tabIndex={-1} data-id={a.id}
    title={asking(a) ? 'Open the card to answer' : open || a.title} onClick={go}>
    <AgentMark look={look} state={a.state} id={a.id} size={12} still/><b>{a.title}</b><span className="a-st">{statusOf(a)}</span>
    <span className="a-acts">{acts}</span></div>;
}
const Hints = ({ keys }: { keys: [string, string][] }) => <p className="k-hint">{keys.map(([k, v]) => <span key={k}><kbd>{k}</kbd>{v}</span>)}</p>;
// `done`: a whole group was handled from its heading, so the panel folds away (unless the keys hold it).
function Panel({ secs, hot, cur, look, act, page, done }: { secs: Sec[]; hot: string; cur: string; look: MarkLook; act: NotchAct; page: (id: string) => void; done: () => void }) {
  const list = useRef<HTMLDivElement>(null), shownHot = useRef('');
  // A list taller than the panel fades at the edge with more behind it; a newly lit part, or the row the keys
  // moved to, is brought into view.
  const edges = () => { const l = list.current; if (!l) return; l.classList.toggle('more-below', l.scrollTop + l.clientHeight < l.scrollHeight - 2); l.classList.toggle('more-above', l.scrollTop > 2); };
  useLayoutEffect(() => {
    const l = list.current!, row = l.querySelector<HTMLElement>('.a-row.is-cur'), sec = l.querySelector<HTMLElement>(`[data-sec="${hot}"]`);
    if (row) {
      if (row.offsetTop < l.scrollTop + 26) l.scrollTop = row.offsetTop - 26;
      else if (row.offsetTop + row.offsetHeight > l.scrollTop + l.clientHeight - 30) l.scrollTop = row.offsetTop + row.offsetHeight - l.clientHeight + 30;
    } else if (sec && hot !== shownHot.current && (sec.offsetTop < l.scrollTop || sec.offsetTop + 60 > l.scrollTop + l.clientHeight)) l.scrollTop = sec.offsetTop - 2;
    shownHot.current = hot;
    edges();
  });
  const tail = (s: Sec) => s.key === 'turn' ? s.ts.some(a => a.state !== 'wait') && <button type="button" onClick={() => { act.read(s.ts.filter(a => a.state !== 'wait').map(a => a.id)); done(); }}>Mark finished read</button>
    : s.key === 'done' ? <button type="button" onClick={() => act.archive(s.ts.map(a => a.id))}>Archive all</button>
    : s.key === 'moon' ? <button type="button" onClick={() => { act.unpark(s.ts.map(a => a.id)); done(); }}>Take all back</button> : null;
  return <div className="nt-card all"><div ref={list} className="a-list" onScroll={edges}>{secs.map(s =>
    <div key={s.key} className={`a-sec${s.key === hot ? ' is-hot' : ''}`} data-sec={s.key}>
      <div className={`a-h is-${s.key}`}><span>{s.key === 'moon' && <IconMark look={look} moon/>}{s.label}<em>{s.ts.length}</em></span>{tail(s)}</div>
      {s.ts.map(a => <Row key={a.id} a={a} kind={s.key} cur={a.id === cur} look={look} act={act} page={page}/>)}
    </div>)}</div>
    {cur && <Hints keys={[['↑↓', 'choose'], ['→', 'open'], ['⏎', 'go to it'], ['esc', 'close']]}/>}</div>;
}
// One session's page: the conversation (Allen's words right, its end-of-turn answers rendered left), what it is doing
// now, and a box whose line Jarvis types into the session. A working session takes it once it stops; a Codex one is
// read here and answered in Codex.
function Page({ a, port, look, act, draft, setDraft, back, keys }: {
  a: Agent; port: string | null; look: MarkLook; act: NotchAct; draft: string; setDraft: (text: string) => void; back: () => void; keys: boolean;
}) {
  const [said, setSaid] = useState<Said[] | null>(null), [mine, setMine] = useState<string[]>([]), [sending, setSending] = useState(false), [why, setWhy] = useState('');
  const list = useRef<HTMLDivElement>(null), input = useRef<HTMLInputElement>(null), bottom = useRef(true);
  const claude = a.agent === 'claude' && !!port;
  useEffect(() => {
    if (!claude) return;
    let stop = false;
    void readConversation(port!, a.id).then(m => { if (!stop && m) setSaid(m); });
    return () => { stop = true; };
  }, [a.id, a.at, a.state]);
  const base = said ?? [...(a.you ? [{ who: 'you' as const, text: a.you }] : []), ...(a.last && ended(a.state) ? [{ who: 'it' as const, text: a.last }] : [])];
  // Words sent from here show at once, until the record has them.
  const recorded = base.filter(m => m.who === 'you').slice(-3).map(m => m.text.replace(/\s+/g, ' ').trim());
  const pending = mine.filter(t => !recorded.includes(t));
  useLayoutEffect(() => { const l = list.current; if (l && bottom.current) l.scrollTop = l.scrollHeight; });
  useEffect(() => { if (!input.current?.disabled) input.current?.focus({ preventScroll: true }); }, [a.id, a.replyable]);
  const busy = working(a.state) || sending || pending.length > 0;
  const send = async (e: FormEvent) => {
    e.preventDefault();
    const text = draft.replace(/\s+/g, ' ').trim().slice(0, 4000);
    if (!text || busy || !a.replyable || !port) return;
    setWhy(''); setSending(true); setMine(m => [...m, text]); setDraft(''); bottom.current = true;
    const fail = await sendReply(port, a.id, text);
    setSending(false);
    if (fail) { setWhy(fail); setMine(m => m.filter(t => t !== text)); setDraft(text); }
  };
  const open = openLabel(a);
  const box = a.agent === 'codex' ? <p className="r-note">Codex is read here only · answer it in Codex</p>
    : asking(a) ? null
    : a.kind !== 'background' ? <p className="r-note">Answer it in its terminal</p>
    : <form className="pg-input c-reply" onSubmit={send}>
      <input ref={input} value={draft} onChange={e => setDraft(e.target.value)} disabled={!a.replyable || busy}
        placeholder={sending ? 'Sending…' : working(a.state) || pending.length ? 'Still working · you can reply once it stops' : a.replyable ? 'Reply…' : 'It cannot take a reply right now'}/>
      <button className="send" aria-label="Send" disabled={!draft.trim() || busy || !a.replyable}><ArrowUp size={13} weight="bold"/></button></form>;
  return <div className="nt-card reply">
    <div className="r-head"><button type="button" className="r-back" aria-label="Back to the list" onClick={back}><CaretLeft size={14}/></button>
      <AgentMark look={look} state={a.state} id={a.id} size={12} still/><b>{a.title}</b><span className="a-st">{statusOf(a)}</span>
      {open && <button type="button" className="nc-go" title={open} onClick={() => act.jump(a)}><ArrowSquareOut size={12}/></button>}</div>
    <div ref={list} className="m-list" onScroll={e => { const l = e.currentTarget; bottom.current = l.scrollTop >= l.scrollHeight - l.clientHeight - 4; }}>
      {!base.length && !pending.length && <p className="r-none">Nothing said yet</p>}
      {base.map((m, k) => m.who === 'you' ? <div key={k} className="m-you">{m.text}</div> : <div key={k} className="m-it md"><Markdown text={m.text}/></div>)}
      {pending.map(t => <div key={`p:${t}`} className="m-you is-pending">{t}</div>)}
      {working(a.state) && <div className="m-now"><AgentMark look={look} state={a.state} id={a.id} size={10}/><span>{(a.last || 'Working').replace(/[.…]+$/, '')}…</span></div>}
      {asking(a) && <div className="m-req"><p className="nc-what">{requestLine(a.request!)}</p>
        <div className="nc-choice"><button type="button" className="btn btn-warm" onClick={() => act.answer(a.id)}>Answer</button></div></div>}
    </div>
    {why && <p className="r-why">{why}</p>}
    {box}
    {keys && <Hints keys={[['←', 'back'], ...(a.replyable && !busy ? [['⏎', 'send'] as [string, string]] : []), ['esc', 'close']]}/>}
  </div>;
}
// One session on a pop: its star and name. Asking opens its card; finished goes to it, ✕ marks it read.
function PopRow({ a, look, act, tag }: { a: Agent; look: MarkLook; act: NotchAct; tag?: ReactNode }) {
  const ask = a.state === 'wait', go = () => ask && a.request ? act.answer(a.id) : act.jump(a);
  return <div className={`u-row${ask ? ' is-ask' : ''}`} role="button" tabIndex={0} data-id={a.id}
    title={ask ? a.request ? 'Asking you · open the card to answer' : `Waiting for you in ${a.where}` : 'Go to it'}
    onClick={go} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } }}>
    <AgentMark look={look} state={a.state} id={a.id} size={12}/><span>{a.title}{tag}</span>
    <span className="u-acts">
      <Act tip="Park it: out of your turn, no reminders, until you take it back" onClick={() => act.park([a.id])}><Moon size={12} weight="fill"/></Act>
      {!ask && <Act tip="Mark as read" onClick={() => act.read([a.id])}><X size={12}/></Act>}</span>
  </div>;
}
function Pop({ agents, look, act, onClose }: { agents: Agent[]; look: MarkLook; act: NotchAct; onClose: () => void }) {
  const errs = agents.filter(a => a.state === 'err').length, all = errs === agents.length, n = agents.length;
  const label = n === 1 ? errs ? 'Stopped' : 'Done' : all ? `${n} stopped` : errs ? `${n} need a look` : `${n} done`;
  return <div className="nt-card pop"><div className="c-bar"><span className={`c-label is-${all ? 'err' : 'done'}`}><i/>{label}</span>
    <button type="button" className="c-x" aria-label="Close" onClick={onClose}><X size={12}/></button></div>
    <div className="u-list">{agents.map(a => <PopRow key={a.id} a={a} look={look} act={act} tag={a.state === 'err' && !all ? <em> stopped</em> : null}/>)}</div></div>;
}

export function Notch({ look, agents, unread, parked, archived, geo, cursor, note, quiet, act, onNoteHover, port, keys, onKeys, onViewing }: {
  look: MarkLook; agents: Agent[]; unread: ReadonlySet<string>; parked: ReadonlyMap<string, number>; archived: ReadonlySet<string>; geo: NotchGeo;
  cursor: RefObject<Point>; note: NotchNote | null; quiet: boolean; act: NotchAct; onNoteHover: (on: boolean) => void;
  port: string | null; keys: number; onKeys: (on: boolean) => void; onViewing: (id: string | null) => void;
}) {
  const out = (a: Agent) => !parked.has(a.id);
  const turn = agents.filter(a => out(a) && (a.state === 'wait' || ended(a.state) && unread.has(a.id))).sort((a, b) => TURN_ORDER.indexOf(a.state) - TURN_ORDER.indexOf(b.state));
  const work = agents.filter(a => out(a) && working(a.state));
  const fin = agents.filter(a => out(a) && ended(a.state) && !unread.has(a.id) && !archived.has(a.id));
  const moon = agents.filter(a => parked.has(a.id)).sort((a, b) => parked.get(b.id)! - parked.get(a.id)!);
  const secs = ([{ key: 'turn', label: 'Your turn', ts: turn }, { key: 'work', label: 'Working', ts: work }, { key: 'done', label: 'Finished', ts: fin },
    { key: 'moon', label: 'Parked', ts: moon }] as Sec[]).filter(s => s.ts.length);
  const order = secs.flatMap(s => s.ts.map(a => a.id));
  const [open, setOpen] = useState(false), [hot, setHot] = useState(''), [dragging, setDragging] = useState(false);
  const [kb, setKb] = useState<Keys | null>(null), [kbCard, setKbCard] = useState(''), [drafts] = useState(() => new Map<string, string>()), [, redraw] = useState(0);
  // The row under the keys can leave (parked, archived, answered elsewhere): the keys stay at the same height.
  if (kb && kb.view === 'list' && order.length && !order.includes(kb.id)) { kb.i = Math.min(kb.i, order.length - 1); kb.id = order[kb.i]; }
  const kbAgent = kb ? agents.find(a => a.id === kb.id) : undefined;
  const paged = kb?.view === 'page' && kbAgent && !kbCard ? kbAgent : undefined;
  const root = useRef<HTMLDivElement>(null), shape = useRef<SVGPathElement>(null), fx = useRef<HTMLCanvasElement>(null), hit = useRef<HTMLDivElement>(null);
  const drop = useRef<HTMLDivElement>(null), dropIn = useRef<HTMLDivElement>(null), noteP = useRef<HTMLDivElement>(null), noteIn = useRef<HTMLDivElement>(null);
  const st = useRef({
    boxes: [] as Box[], wingTarget: 0, opened: false, dirty: true, innerL: 0, lastIn: 0, wantAt: 0, want: false, open: false, hot: '',
    dropGoal: { w: ALL_W, d: 0 }, noteGoal: { w: 24, d: 0 }, noteL: 0, onNote: false,
    s: { ww: spring(0), dx: spring(0), dw: spring(24), dd: spring(0), nx: spring(0), nw: spring(24), nd: spring(0) },
    press: null as { x: number; y: number } | null, drag: null as { x: number; y: number } | null,
    puffs: [] as { x: number; y: number; c: string; at: number }[], workAt: -1e9, workN: 0, turnIds: new Set<string>(), turnAt: -1e9,
    // Sessions on their way into the moon, from where the pointer was, and when the last one landed.
    flights: [] as { st: AgentState; x: number; y: number; at: number }[], moonAt: -1e9, parkedIds: new Set<string>(),
  }).current;
  const L = useRef({ look, turn, work, fin, moon, geo, note, quiet, onNoteHover, held: false, pageW: false });
  L.current = { look, turn, work, fin, moon, geo, note, quiet, onNoteHover, held: !!kb && !kbCard, pageW: !!paged };
  const members = (key: Kind) => ({ turn: L.current.turn, work: L.current.work, done: L.current.fin, moon: L.current.moon })[key];
  const setPanel = (on: boolean) => { if (on === st.open) return; st.open = on; st.dirty = true; setOpen(on); };
  const setHotKey = (key: string) => { if (key === st.hot) return; st.hot = key; setHot(key); };
  // The keys: ⌥Tab (from the main process) opens the list and holds it; a row's → opens a page with the mouse too.
  const kbOpen = (id = '', view: Keys['view'] = 'list') => {
    const at = order.indexOf(id), i = Math.max(0, at);
    if (!order.length) return;
    setKb({ view: at < 0 ? 'list' : view, id: order[i], i, at: performance.now() }); setKbCard(''); setPanel(true); onKeys(true);
  };
  const kbClose = () => { setKb(null); setKbCard(''); onKeys(false); (document.activeElement as HTMLElement | null)?.blur?.(); };
  const lastKeys = useRef(keys);
  useEffect(() => { if (keys === lastKeys.current) return; lastKeys.current = keys; if (kb) kbClose(); else kbOpen(); }, [keys]);
  const openCard = (id: string) => { if (kb) setKbCard(id); act.answer(id); };
  const A: NotchAct = { ...act, answer: openCard, jump: a => { if (kb) kbClose(); setPanel(false); act.jump(a); } };
  // A card opened from the list is gone (answered, parked, folded): back to the list.
  useEffect(() => { if (kbCard && note?.id !== kbCard) setKbCard(''); }, [note?.key]);
  useEffect(() => { onViewing(paged?.id ?? null); }, [paged?.id]);
  // Nothing left to hold: the keys let go. A page whose session left goes back to the list.
  useEffect(() => { if (kb && !order.length) kbClose(); else if (kb?.view === 'page' && !kbAgent) setKb({ ...kb, view: 'list' }); }, [order.length, !!kbAgent]);
  // The keys light the group of the row they are on, under its mark too.
  const kbHot = kb ? secs.find(s => s.ts.some(a => a.id === kb.id))?.key ?? '' : '';
  useEffect(() => { if (kbHot) setHotKey(kbHot); }, [kbHot]);
  // Parked just now: its star flies from where the pointer was into the moon.
  useEffect(() => {
    const p = cursor.current;
    for (const a of moon) if (!st.parkedIds.has(a.id) && p) st.flights.push({ st: a.state, x: p.x, y: p.y, at: performance.now() });
    st.parkedIds = new Set(moon.map(a => a.id));
  }, [moon.map(a => a.id).join()]);
  // 1.5 s on its page is looking at it.
  useEffect(() => {
    if (!paged || !unread.has(paged.id) || paged.state === 'wait') return;
    const t = setTimeout(() => act.read([paged.id]), Math.max(0, DWELL_MS - (performance.now() - kb!.at)));
    return () => clearTimeout(t);
  }, [paged?.id, unread.has(paged?.id ?? '')]);
  useEffect(() => {
    if (!kb) return;
    const key = (e: KeyboardEvent) => {
      const stop = () => { e.preventDefault(); e.stopPropagation(); }, k = e.key, field = e.target instanceof HTMLInputElement ? e.target : null;
      if (kbCard) { if (k === 'Escape' || (k === 'ArrowLeft' && !field)) { stop(); act.back(); setKbCard(''); } return; }
      if (k === 'Escape') { stop(); kbClose(); return; }
      // On a page ← goes back unless there is text in the box to move through.
      if (kb.view === 'page') { if (k === 'ArrowLeft' && !field?.value) { stop(); setKb({ ...kb, view: 'list' }); } return; }
      const a = agents.find(x => x.id === kb.id);
      if (k === 'ArrowDown' || k === 'ArrowUp') { stop(); const i = clamp(kb.i + (k === 'ArrowDown' ? 1 : -1), 0, order.length - 1); setKb({ ...kb, i, id: order[i] }); }
      else if (k === 'ArrowRight' && a) { stop(); if (asking(a)) openCard(a.id); else setKb({ ...kb, view: 'page', at: performance.now() }); }
      else if (k === 'Enter' && a) { stop(); A.jump(a); }
    };
    // A click anywhere outside her window lets go of the keys.
    const blur = () => kbClose();
    window.addEventListener('keydown', key, true); window.addEventListener('blur', blur);
    return () => { window.removeEventListener('keydown', key, true); window.removeEventListener('blur', blur); };
  });
  useLayoutEffect(() => { st.dirty = true; });
  useEffect(() => {
    const ro = new ResizeObserver(() => { st.dirty = true; });
    [dropIn.current!, noteIn.current!].forEach(el => ro.observe(el));
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    let raf = 0, timer: ReturnType<typeof setTimeout> | undefined, last = 0;
    // The island opens whole, her lobe to the last mark, one piece growing down: at least as wide as the island,
    // wider when the content needs it, and then wider on both sides round the island's middle.
    const span = (w: number) => {
      const g = L.current.geo, l = g.lobeL, r = g.notchR + Math.max(0, st.wingTarget), ww = Math.max(w, r - l);
      return { l: clamp((l + r) / 2 - ww / 2, 8, g.width - ww - 8), w: ww };
    };
    // The marks and the panes, laid out and drawn. True while something is still moving.
    const frame = (now: number, dt: number) => {
      const { look, turn, work, fin, moon, geo: g, note, quiet, onNoteHover, held, pageW } = L.current, s = st.s, top = g.top;
      const firm = reduced.matches, go = (sp: typeof s.ww, goal: number, hz: number, damp: number) => step(sp, goal, hz, firm ? 1 : damp, dt);
      const p = cursor.current, ww = Math.max(0, s.ww.value);
      const inWing = !!p && p.y <= top + 2 && p.x >= g.notchR && p.x <= g.notchR + ww + 2;
      // One mark per group, so the row is at most four marks. Nothing moves while the pointer is on the row or the
      // panel is open; the counts follow along, and the row catches up once you leave.
      if (st.drag || !st.boxes.length || !(st.open || inWing)) {
        const kinds = ([['turn', turn], ['work', work], ['done', fin], ['moon', moon]] as const).filter(([, ts]) => ts.length).map(([k]) => k);
        let x = g.notchR + PAD;
        st.boxes = kinds.map(key => { const b = { key, x0: x, x1: x + GCELL, cx: x + GCX }; x += GCELL; return b; });
        st.wingTarget = kinds.length ? x + PAD - g.notchR : 0;
        // What the row shows, for the checks: `turn2 work4 done3 moon1`.
        const marks = st.boxes.map(b => `${b.key}${members(b.key).length}`).join(' ');
        if (root.current!.dataset.marks !== marks) root.current!.dataset.marks = marks;
      }
      // Resting anywhere on the row opens the panel after 90 ms; it closes 260 ms after the pointer has left the
      // row and the panel. A notice closes it; the keys hold it open.
      if (!st.drag) {
        const inDrop = !!p && st.open && p.x >= s.dx.value && p.x <= s.dx.value + s.dw.value && p.y >= top - 2 && p.y <= s.dd.value;
        const slot = inWing ? st.boxes.find(b => p!.x >= b.x0 && p!.x < b.x1) : undefined;
        const want = held || (!note && !quiet && (!!slot || inDrop));
        if (!held && slot) setHotKey(slot.key);
        if (want && (slot || inDrop)) st.lastIn = now;
        if (want !== st.want) { st.want = want; st.wantAt = now; }
        if (want && !st.open && (held || now - st.wantAt > 90)) setPanel(true);
        else if (!want && st.open && (note || quiet || now - st.lastIn > 260)) setPanel(false);
        if (st.open && !held && !st.boxes.some(b => members(b.key).length)) setPanel(false);
      }
      // The page opens with the marks already out, not grown in from nothing.
      if (!st.opened) { st.opened = true; s.ww.value = st.wingTarget; }
      let moving = go(s.ww, st.wingTarget, 3.2, .8);
      const W = g.width, dIn = dropIn.current!, nIn = noteIn.current!, room = Math.min(440, W - (g.notchR - 6) - 8);
      if (st.dirty) {
        st.dirty = false;
        if (st.open) { const sp = span(pageW ? PAGE_W : ALL_W); dIn.style.width = `${sp.w}px`; st.dropGoal = { w: sp.w, d: top + dIn.offsetHeight + 2 }; }
        if (note) {
          const sp = span(note.pop ? autoWidth(nIn, 240, room) : Math.min(CARD_W, W - 16));
          nIn.style.width = `${sp.w}px`; st.noteL = sp.l;
          st.noteGoal = { w: sp.w, d: Math.min(top + 640, top + nIn.offsetHeight + 2) };
        }
      }
      // Closed, both panes rest inside the island, so they grow out of it and fold back into it.
      const isl = span(0), rest = { l: isl.l, w: isl.w, d: top * .6 }, dw = st.dropGoal.w;
      const dg = st.open ? { l: span(dw).l, w: dw, d: st.dropGoal.d } : rest;
      if (st.open && s.dd.value < top) { s.dx.value = isl.l; s.dw.value = isl.w; }
      moving = go(s.dx, dg.l, 3.4, .86) || moving; moving = go(s.dw, dg.w, 3.4, .86) || moving; moving = go(s.dd, dg.d, 3.2, .8) || moving;
      const noteOpen = !!note && !held;
      const ng = noteOpen ? { l: st.noteL, w: st.noteGoal.w, d: st.noteGoal.d } : rest;
      if (noteOpen && s.nd.value < top) { s.nx.value = isl.l; s.nw.value = isl.w; }
      moving = go(s.nx, ng.l, 2.9, .84) || moving; moving = go(s.nw, ng.w, 2.9, .84) || moving; moving = go(s.nd, ng.d, 2.9, .8) || moving;
      const onNote = noteOpen && !!p && p.x >= s.nx.value && p.x <= s.nx.value + s.nw.value && p.y >= top && p.y <= s.nd.value;
      if (onNote !== st.onNote) { st.onNote = onNote; onNoteHover(onNote); }
      // One black piece: her island, the notch, the marks, and whatever hangs from them.
      const wingR = g.notchR + Math.max(0, s.ww.value), dropOut = st.open || s.dd.value > top + 1, noteOut = noteOpen || s.nd.value > top + 1, rects: Rect[] = [];
      if (wingR > g.notchR + .5 || dropOut || noteOut) rects.push({ l: g.lobeL, r: Math.max(g.notchR, wingR), d: top });
      if (dropOut) rects.push({ l: s.dx.value, r: s.dx.value + s.dw.value, d: s.dd.value });
      if (noteOut) rects.push({ l: s.nx.value, r: s.nx.value + s.nw.value, d: s.nd.value });
      shape.current!.setAttribute('d', skyline(rects));
      if (st.open) st.innerL = dg.l;
      place(drop.current!, dIn, s.dx.value, s.dw.value, s.dd.value, st.innerL, st.dropGoal.d, st.open, top);
      place(noteP.current!, nIn, s.nx.value, s.nw.value, s.nd.value, st.noteL, st.noteGoal.d, noteOpen, top);
      Object.assign(hit.current!.style, { left: `${g.notchR}px`, width: `${Math.max(0, s.ww.value)}px`, height: `${top}px` });
      draw(now, top, look);
      return moving || !!st.drag || st.puffs.length > 0 || st.flights.length > 0 || now - st.moonAt < 500;
    };
    const place = (pane: HTMLElement, inner: HTMLElement, left: number, w: number, d: number, goalL: number, goalD: number, open: boolean, top: number) => {
      const h = Math.max(0, d - top), k = open ? clamp(h / Math.max(1, goalD - top)) : 0;
      pane.style.transform = `translate(${left}px,${top}px)`; pane.style.width = `${Math.max(0, w)}px`; pane.style.height = `${h}px`;
      pane.style.visibility = h < 1 ? 'hidden' : 'visible';
      pane.classList.toggle('is-open', open && k > .6);
      inner.style.left = `${goalL - left}px`;
      inner.style.opacity = String(open ? clamp((k - .45) / .5) : clamp(h / 60) * .6);
    };
    // The marks, clipped to the wing as it grows; the finished mark being dragged; the puffs left where it went.
    const draw = (now: number, top: number, look: MarkLook) => {
      const cv = fx.current!, { geo: g, turn, work } = L.current, d = dpr(), W = g.width, t = now / 1000;
      if (cv.width !== Math.round(W * d) || cv.height !== Math.round(FX_H * d)) { cv.width = Math.round(W * d); cv.height = Math.round(FX_H * d); }
      const ctx = cv.getContext('2d')!;
      ctx.setTransform(d, 0, 0, d, 0, 0); ctx.clearRect(0, 0, W, FX_H);
      // News makes its mark jump once; the beacon rings for 4 s after each arrival.
      if (turn.some(a => !st.turnIds.has(a.id))) st.turnAt = now;
      if (work.length > st.workN) st.workAt = now;
      st.turnIds = new Set(turn.map(a => a.id)); st.workN = work.length;
      ctx.save(); ctx.beginPath(); ctx.rect(g.notchR, 0, Math.max(0, st.s.ww.value), top); ctx.clip();
      for (const b of st.boxes) {
        const ts = members(b.key);
        if (!ts.length) continue;
        ctx.save(); ctx.translate(b.cx, top / 2);
        if (b.key === 'turn') drawTurnIcon(ctx, look, t, (now - st.turnAt) / 1000, d, now - st.turnAt > 4000);
        else if (b.key === 'work') drawMark(ctx, look, ts.every(a => a.state === 'pack') ? 'pack' : 'work', t + seedOf('work'), (now - st.workAt) / 1000, d);
        else if (b.key === 'done') { ctx.globalAlpha = .55; drawMark(ctx, look, ts.every(a => a.state === 'err') ? 'err' : 'done', 0, 99, d); }
        else { const k = (now - st.moonAt) / 1000, pop = k < .5 ? 1 + .5 * (1 - k / .5) : 1; ctx.globalAlpha = .85; ctx.scale(pop, pop); drawMoon(ctx, 99, d); }
        ctx.restore();
        ctx.save(); ctx.font = '600 9px "JetBrains Mono", Menlo, monospace'; ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
        ctx.fillStyle = b.key === 'turn' ? rgba(tint(hueAt(t), .45)) : 'rgba(214,222,250,.62)';
        ctx.fillText(String(ts.length), b.cx + 9, top / 2 + .5); ctx.restore();
        if (st.open && st.hot === b.key) {
          const c = b.key === 'turn' ? hueAt(t).join(',') : b.key === 'moon' ? MOON_RGB : b.key === 'work' ? '108,156,255' : '111,224,180';
          ctx.fillStyle = `rgba(${c},.9)`; ctx.shadowColor = `rgba(${c},.9)`; ctx.shadowBlur = 6 * d;
          ctx.beginPath(); ctx.roundRect(b.cx - 5, top - 3.5, 10, 1.6, .8); ctx.fill(); ctx.shadowBlur = 0;
        }
      }
      ctx.restore();
      // A parked session's star flies up into the moon (or to the end of the row before the moon is out).
      const moonBox = st.boxes.find(b => b.key === 'moon'), tx = moonBox?.cx ?? g.notchR + Math.max(0, st.s.ww.value) - 10, ty = top / 2;
      st.flights = st.flights.filter(f => {
        const k = (now - f.at) / 560;
        if (k >= 1) { st.moonAt = now; return false; }
        const e = 1 - (1 - k) ** 3, u = 1 - e, x = u * u * f.x + 2 * u * e * tx + e * e * tx, y = u * u * f.y + 2 * u * e * f.y + e * e * ty;
        ctx.save(); ctx.translate(x, y); ctx.scale(1 - .35 * e, 1 - .35 * e); ctx.globalAlpha = 1 - .5 * e;
        drawMark(ctx, look, f.st, t, 99, d); ctx.restore();
        return true;
      });
      const dr = st.drag;
      if (dr) {
        const out = dr.y > top + DRAG_OUT, ts = members('done');
        ctx.save(); ctx.translate(dr.x, dr.y);
        ctx.fillStyle = '#000'; ctx.beginPath(); ctx.arc(0, 0, 13, 0, Math.PI * 2); ctx.fill();
        ctx.globalAlpha = out ? .55 : 1;
        drawMark(ctx, look, ts.every(a => a.state === 'err') ? 'err' : 'done', 0, 99, d);
        ctx.font = '600 9px "JetBrains Mono", Menlo, monospace'; ctx.textBaseline = 'middle'; ctx.fillStyle = 'rgba(220,226,250,.9)'; ctx.fillText(String(ts.length), 7, 1);
        ctx.globalAlpha = 1; ctx.font = '600 10.5px -apple-system, "PingFang SC", sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
        ctx.fillStyle = 'rgba(255,255,255,.95)'; ctx.shadowColor = 'rgba(0,0,0,.6)'; ctx.shadowBlur = 4 * d;
        ctx.fillText(out ? 'Let go to archive' : 'Drag out of the menu bar', 0, 16);
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

  // Press the finished mark and pull it down out of the menu bar to archive them all; anywhere higher, it goes back.
  const letGo = (keep: boolean) => {
    const d = st.drag;
    st.press = st.drag = null; setDragging(false);
    if (!d || !keep || d.y <= geo.top + DRAG_OUT) return;
    const ids = members('done').map(a => a.id);
    if (!ids.length) return;
    st.puffs.push({ x: d.x, y: d.y, c: members('done').every(a => a.state === 'err') ? '255,122,102' : '111,224,180', at: performance.now() });
    act.archive(ids);
  };
  const cur = kb?.view === 'list' && !kbCard ? kb.id : '';
  const panel = paged
    ? <Page key={paged.id} a={paged} port={port} look={look} act={A} draft={drafts.get(paged.id) ?? ''} keys={!!kb}
      setDraft={text => { drafts.set(paged.id, text); redraw(n => n + 1); }} back={() => setKb({ ...kb!, view: 'list' })}/>
    : secs.length ? <Panel secs={secs} hot={kb ? kbHot : hot} cur={cur} look={look} act={A} page={id => kbOpen(id, 'page')} done={() => { if (!kb) setPanel(false); }}/> : null;
  const popAgents = note?.pop?.map(id => agents.find(a => a.id === id)).filter((a): a is Agent => !!a) ?? [];
  // The panes keep what they showed while they fold away.
  const lastNote = useRef<NotchNote | null>(null), lastPanel = useRef<ReactNode>(null);
  if (note) lastNote.current = note;
  if (open && panel) lastPanel.current = panel;
  const shownNote = note ?? lastNote.current;
  return <div ref={root} className="notch">
    <svg className="notch-shape" aria-hidden="true"><path ref={shape}/></svg>
    <canvas ref={fx} className="notch-fx" data-look={look} aria-hidden="true" style={{ width: geo.width, height: FX_H }}/>
    <div ref={hit} className="notch-hit" data-hit aria-hidden="true"
      onPointerDown={e => {
        const b = st.boxes.find(x => e.clientX >= x.x0 && e.clientX < x.x1);
        if (b?.key !== 'done') return;
        st.press = { x: e.clientX, y: e.clientY }; e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={e => {
        if (!st.press) return;
        const q = { x: e.clientX, y: e.clientY };
        if (!st.drag && Math.hypot(q.x - st.press.x, q.y - st.press.y) > 4) { st.drag = q; setDragging(true); setPanel(false); }
        if (st.drag) Object.assign(st.drag, q);
      }}
      onPointerUp={() => letGo(true)} onPointerCancel={() => letGo(false)}/>
    <div ref={drop} className="notch-pane notch-drop" data-hit={open ? true : undefined} role="dialog" aria-label="Sessions">
      <div ref={dropIn} className="notch-pane-in">{open ? panel : lastPanel.current}</div></div>
    <div ref={noteP} className="notch-pane notch-note" data-hit={note && !(kb && !kbCard) ? true : undefined} role="alertdialog" aria-label="Agent notice">
      <div ref={noteIn} className="notch-pane-in">{shownNote && (shownNote.pop ? popAgents.length > 0 && <Pop agents={popAgents} look={look} act={A} onClose={shownNote.onClose}/> : shownNote.card)}</div></div>
    {dragging && <div className="notch-catch" data-hit aria-hidden="true"/>}
  </div>;
}

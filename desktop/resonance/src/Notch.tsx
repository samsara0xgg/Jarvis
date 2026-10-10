import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type ReactNode, type RefObject } from 'react';
import { Archive, ArrowSquareOut, ArrowUUpLeft, ArrowUp, CaretLeft, CaretRight, Moon, X } from '@phosphor-icons/react';
import { ago, readConversation, requestLine, sendReply, type Agent, type AgentState, type Said } from './agents';
import { AgentMark, COLOR, drawMark, dpr, seedOf, useClock, type MarkLook } from './AgentMarks';
import { DOT_RING_S, drawMoon, drawTurnIcon, hueAt, MOON_RGB, rgba, tint } from './beacon';
import { Markdown } from './Markdown';
import { ended, NoticeFlightContext, openTip, type Glow } from './Notices';
import { useT, type L } from './companionSettings';
import { readStartrail } from './startrail';
import { spring, step } from './starCore';
import { HOVER_DWELL_MS, HOVER_EXIT_MS, HOVER_SPEED, PointerIntent } from './pointerIntent';
import { MOTION, SPRINGS } from './motion';
import { skyline, type IslandRect as Rect } from './islandShape';
import { pinLabel, pinTrip, pinWidest, type Departure } from './pin';

// Beside the notch, after the notch lab (ADR 0069, 0070). Right of the camera, one mark per group with its count:
// your turn (the beacon), working, finished, parked (the moon). Only working moves; the beacon sends out its rings
// for a few seconds after news. Resting anywhere on the row opens one panel that is the whole island growing down,
// every session one line; its pops and needs-you cards grow out of the island the same way. ⌥Tab opens the same
// panel and holds it for the keys: ↑↓ pick a session, → opens its card (asking) or its page (the conversation,
// with a box that types a reply into the session), ⏎ goes to it, Esc closes. A finished session is archived from
// the panel or by dragging the finished mark down out of the menu bar; anything on your turn can be parked.
// A glow (ADR 0187) is one more amber point in the turn group: it counts there and arrives like anything new, and its rows
// follow the sessions on the list: a click opens its place, the ✕ clears it. It is a mark, so only dnd keeps it back.
// A pinned bus trip (ADR 0200) is a pill after the marks: `🚌 28 · 12 分` counts down to when he must leave, amber from 5 min and
// 该走了 until the bus goes; resting on it shows the whole trip, a click opens its card (ADR 0202). It never opens the panel,
// and where the Dashboard leaves no room for it beside the marks it steps aside before they do.
type Point = { x: number; y: number };
export type Kind = 'turn' | 'work' | 'done' | 'moon';
// While the Dashboard hangs below, the wing opens nothing of its own: the pointer on a mark tells the Dashboard which group,
// and a press sends it to that group.
export type NotchAside = { hover: (key: Kind | null) => void; open: (key: Kind) => void };
type Box = { key: Kind; x0: number; x1: number; cx: number };
type Sec = { key: Kind; label: string; ts: Agent[]; gs?: Glow[] };
type Keys = { view: 'list' | 'page'; id: string; i: number; at: number };
export type NotchGeo = { width: number; top: number; notchR: number; lobeL: number };
export type NotchAct = {
  jump: (a: Agent) => void; answer: (id: string) => void; read: (ids: string[]) => void; back: () => void;
  archive: (ids: string[]) => void; park: (ids: string[]) => void; unpark: (ids: string[]) => void;
};
// The glows on the wing, and what a click on one and its ✕ do.
export type NotchGlow = { items: Glow[]; open: (g: Glow) => void; clear: (g: Glow) => void };
// The pinned bus trip, and what the pill's ✕ does.
export type NotchPin = { item: Departure | null; open: (d: Departure) => void };
// A pop names sessions; a card is a needs-you card the companion builds, for session `id` when it has one.
// A pop carries its 合适吗 row in `rate` (ADR 0160).
export type NotchNote = { key: string; id?: string; pop?: string[]; card?: ReactNode; rate?: ReactNode; onClose: () => void };

// Your turn: asking first, then stopped, then finished.
const TURN_ORDER: AgentState[] = ['wait', 'err', 'done'];
const PAD = 4, DONE_FADE_MS = 10 * 60_000, GCELL = 26, GCX = 6, WING_W = 112, DOT_WING_W = 64, DOT_R = 2.6, DOT_COUNT_X = 7.5, DOT_GAP = 5, DIGIT_W = 6.1, POP_W = 360, ALL_W = 440, PAGE_W = 560, CARD_W = 440, DRAG_OUT = 30, DWELL_MS = 1500, PIN_H = 16, PIN_PAD = 6, PIN_GAP = 5, PIN_TIP_MS = 250;
const PIN_FONT = '600 10px "JetBrains Mono", Menlo, monospace';
const clamp = (v: number, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, v));
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const countPulse = (progress: number) => reduced.matches ? 1 : 1 + .45 * Math.sin(Math.PI * progress);
const working = (st: AgentState) => st === 'work' || st === 'pack';
// The mark the turn wears in 点线环: its first member's, the turn being sorted most urgent first; a glow makes it the amber point.
const turnLead = (look: MarkLook, turn: Agent[], glows = 0): 'wait' | 'err' | 'done' => look === 'dot' && !glows && turn[0] && turn[0].state !== 'wait' ? turn[0].state === 'err' ? 'err' : 'done' : 'wait';
// The panel's words in the chosen language (the panels get them from `useT`, the canvas from `L.current.say`).
type T = (l: L) => string;
// The pinned trip's pill is as wide as its widest words, so it does not shift when the minutes lose a digit.
let measuring: CanvasRenderingContext2D | null = null;
const pinWidth = (d: Departure, say: T) => {
  const ctx = measuring ??= document.createElement('canvas').getContext('2d')!;
  ctx.font = PIN_FONT;
  return Math.ceil(Math.max(...pinWidest(d, say).map(w => ctx.measureText(w).width))) + 2 * PIN_PAD;
};

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
// agents.ts's `ago` ("now", "3m", "2h", "1d") in words.
const since = (age: string, t: T) => {
  const m = /^(\d+)([mhd])$/.exec(age);
  if (!m) return age === 'now' ? t(['just now', '刚刚']) : age;
  return m[2] === 'm' ? t([`${m[1]}m ago`, `${m[1]} 分钟前`]) : m[2] === 'h' ? t([`${m[1]}h ago`, `${m[1]} 小时前`]) : t([`${m[1]}d ago`, `${m[1]} 天前`]);
};
const waitingIn = (a: Agent, t: T) => a.where === 'Background' ? t(['Waiting for you in the background', '在后台等你']) : t([`Waiting for you in ${a.where}`, `在 ${a.where} 里等你`]);
// What a session is at, in a few words: the question it asks, what it is doing, or how it ended and when.
const statusOf = (a: Agent, t: T) => a.state === 'wait' ? a.request ? requestLine(a.request) : waitingIn(a, t)
  : a.state === 'err' ? `${t(['Stopped', '出错停了'])}${a.error ? ` · ${a.error}` : ''}` : a.state === 'done' ? t([`Done · ${since(a.age, t)}`, `做完了 · ${since(a.age, t)}`])
  : a.state === 'pack' ? t(['Compacting', '在压缩']) : a.last || t(['Working', '在干活']);
const asking = (a: Agent) => a.state === 'wait' && !!a.request;
function Act({ tip, onClick, children }: { tip: string; onClick: () => void; children: ReactNode }) {
  return <i role="button" aria-label={tip} title={tip} onClick={e => { e.stopPropagation(); onClick(); }}>{children}</i>;
}
// One session on one line: a still star, its name (bold while it needs a look), what it is at, and on hover what
// can be done about it. A click opens the card of one that is asking, else goes to it.
function Row({ a, kind, cur, look, act, page, index }: { a: Agent; kind: Kind; cur: boolean; look: MarkLook; act: NotchAct; page: (id: string) => void; index: number }) {
  const t = useT(), open = openTip(a, t), go = () => asking(a) ? act.answer(a.id) : act.jump(a);
  const acts = <>
    {!asking(a) && open && <Act tip={open} onClick={() => act.jump(a)}><ArrowSquareOut size={12}/></Act>}
    {!asking(a) && <Act tip={t(['What it said · reply', '看对话 · 回复'])} onClick={() => page(a.id)}><CaretRight size={12}/></Act>}
    {kind === 'turn' && <Act tip={t(['Park it: out of your turn, no reminders, until you take it back', '先放着：不再轮到你，也不提醒，直到你拿回来'])} onClick={() => act.park([a.id])}><Moon size={12} weight="fill"/></Act>}
    {kind === 'turn' && a.state !== 'wait' && <Act tip={t(['Mark as read', '标为已读'])} onClick={() => act.read([a.id])}><X size={12}/></Act>}
    {kind === 'moon' && <Act tip={t(['Take back to your turn', '拿回来，重新轮到你'])} onClick={() => act.unpark([a.id])}><ArrowUUpLeft size={12}/></Act>}
    {(kind === 'done' || kind === 'moon' && a.state !== 'wait') && <Act tip={t(['Archive: done with it, kept to find again', '归档：处理完了，之后还能找到'])} onClick={() => act.archive([a.id])}><Archive size={12}/></Act>}
  </>;
  return <div className={`a-row${a.state === 'wait' ? ' is-ask' : ''}${kind === 'turn' ? ' is-due' : ''}${cur ? ' is-cur' : ''}`} role="button" tabIndex={-1} data-id={a.id} style={{ animationDelay: `${Math.min(index, 4) * MOTION.stagger}ms` }}
    title={asking(a) ? t(['Open the card to answer', '打开卡片来回答']) : open || a.title} onClick={go}>
    <AgentMark look={look} state={a.state} id={a.id} size={12} still/><b>{a.title}</b><span className="a-st">{statusOf(a, t)}</span>
    <span className="a-acts">{acts}</span></div>;
}
// One glow on the turn list: an amber point, its title, its one line and how long ago. A click opens its place and counts it seen; ✕ clears it.
function GlowRow({ g, look, glow, index }: { g: Glow; look: MarkLook; glow: NotchGlow; index: number }) {
  const t = useT(), age = g.at === undefined ? '' : since(ago(g.at), t);
  return <div className="a-row is-due is-glow" role="button" tabIndex={-1} data-glow={g.id} style={{ animationDelay: `${Math.min(index, 4) * MOTION.stagger}ms` }}
    title={g.line || g.title} onClick={() => glow.open(g)}>
    <AgentMark look={look} state="wait" id={g.id} size={12} still/><b>{g.title}</b><span className="a-st">{g.line}</span><time className="a-ago">{age}</time>
    <span className="a-acts"><Act tip={t(['Clear', '清掉'])} onClick={() => glow.clear(g)}><X size={12}/></Act></span></div>;
}
const Hints = ({ keys }: { keys: [string, string][] }) => <p className="k-hint">{keys.map(([k, v]) => <span key={k}><kbd>{k}</kbd>{v}</span>)}</p>;
// `done`: a whole group was handled from its heading, so the panel folds away (unless the keys hold it).
function Panel({ secs, hot, cur, look, act, glow, page, done }: { secs: Sec[]; hot: string; cur: string; look: MarkLook; act: NotchAct; glow?: NotchGlow; page: (id: string) => void; done: () => void }) {
  let rowIndex = 0;
  const t = useT(), list = useRef<HTMLDivElement>(null), shownHot = useRef('');
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
  const tail = (s: Sec) => s.key === 'turn' ? s.ts.some(a => a.state !== 'wait') && <button type="button" onClick={() => { act.read(s.ts.filter(a => a.state !== 'wait').map(a => a.id)); done(); }}>{t(['Mark finished as read', '已完成的标为已读'])}</button>
    : s.key === 'done' ? <button type="button" onClick={() => act.archive(s.ts.map(a => a.id))}>{t(['Archive all', '全部归档'])}</button>
    : s.key === 'moon' ? <button type="button" onClick={() => { act.unpark(s.ts.map(a => a.id)); done(); }}>{t(['Take all back', '全部拿回来'])}</button> : null;
  return <div className="nt-card all"><div ref={list} className="a-list" onScroll={edges}>{secs.map(s =>
    <div key={s.key} className={`a-sec${s.key === hot ? ' is-hot' : ''}`} data-sec={s.key}>
      <div className={`a-h is-${s.key}`}><span>{s.key === 'moon' && <IconMark look={look} moon/>}{s.label}<em>{s.ts.length + (s.gs?.length ?? 0)}</em></span>{tail(s)}</div>
      {s.ts.map(a => <Row key={a.id} a={a} kind={s.key} cur={a.id === cur} look={look} act={act} page={page} index={rowIndex++}/>)}
      {glow && s.gs?.map(g => <GlowRow key={g.id} g={g} look={look} glow={glow} index={rowIndex++}/>)}
    </div>)}</div>
    {cur && <Hints keys={[['↑↓', t(['choose', '选择'])], ['→', t(['open', '打开'])], ['⏎', t(['go to it', '前往'])], ['esc', t(['close', '关闭'])]]}/>}</div>;
}
// One session's page: the conversation (Allen's words right, its end-of-turn answers rendered left), what it is doing
// now, and a box whose line Jarvis types into the session. A working session takes it once it stops; a Codex one is
// read here and answered in Codex, one of Startrail's in Startrail.
function Page({ a, port, look, act, draft, setDraft, back, keys }: {
  a: Agent; port: string | null; look: MarkLook; act: NotchAct; draft: string; setDraft: (text: string) => void; back: () => void; keys: boolean;
}) {
  const t = useT();
  const [said, setSaid] = useState<Said[] | null>(null), [mine, setMine] = useState<string[]>([]), [sending, setSending] = useState(false), [why, setWhy] = useState('');
  const list = useRef<HTMLDivElement>(null), input = useRef<HTMLInputElement>(null), bottom = useRef(true);
  const claude = a.agent === 'claude' && !!port;
  useEffect(() => {
    if (!claude && !a.host) return;
    let stop = false;
    void (a.host ? readStartrail(a.id) : readConversation(port!, a.id)).then(m => { if (!stop && m) setSaid(m); });
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
  const open = openTip(a, t);
  const box = a.host ? !asking(a) && <p className="r-note">{t(['Read-only here · reply in Startrail', '这里只能看 · 请到 Startrail 里回复'])}</p>
    : a.agent === 'codex' ? <p className="r-note">{t(['Codex sessions are read-only here · answer in Codex', 'Codex 会话在这里只能看 · 请到 Codex 里回答'])}</p>
    : asking(a) ? null
    : a.kind !== 'background' ? <p className="r-note">{t(['Answer it in its terminal', '请到它的终端里回答'])}</p>
    : <form className="pg-input c-reply" onSubmit={send}>
      <input ref={input} value={draft} onChange={e => setDraft(e.target.value)} disabled={!a.replyable || busy}
        placeholder={sending ? t(['Sending…', '发送中…']) : working(a.state) || pending.length ? t(['Still working · you can reply once it stops', '还在干活 · 它停下后才能回复']) : a.replyable ? t(['Reply…', '回复…']) : t(['It cannot take a reply right now', '它现在没法接收回复'])}/>
      <button className="send" aria-label={t(['Send', '发送'])} disabled={!draft.trim() || busy || !a.replyable}><ArrowUp size={13} weight="bold"/></button></form>;
  return <div className="nt-card reply">
    <div className="r-head"><button type="button" className="r-back" aria-label={t(['Back to the list', '返回列表'])} onClick={back}><CaretLeft size={14}/></button>
      <AgentMark look={look} state={a.state} id={a.id} size={12} still/><b>{a.title}</b><span className="a-st">{statusOf(a, t)}</span>
      {open && <button type="button" className="nc-go" title={open} onClick={() => act.jump(a)}><ArrowSquareOut size={12}/></button>}</div>
    <div ref={list} className="m-list" onScroll={e => { const l = e.currentTarget; bottom.current = l.scrollTop >= l.scrollHeight - l.clientHeight - 4; }}>
      {!base.length && !pending.length && <p className="r-none">{t(['No messages yet', '还没有对话'])}</p>}
      {base.map((m, k) => m.who === 'you' ? <div key={k} className="m-you">{m.text}</div> : <div key={k} className="m-it md"><Markdown text={m.text}/></div>)}
      {pending.map(t => <div key={`p:${t}`} className="m-you is-pending">{t}</div>)}
      {working(a.state) && <div className="m-now"><AgentMark look={look} state={a.state} id={a.id} size={10}/><span>{(a.last || t(['Working', '在干活'])).replace(/[.…]+$/, '')}…</span></div>}
      {asking(a) && <div className="m-req"><p className="nc-what">{requestLine(a.request!)}</p>
        <div className="nc-choice"><button type="button" className="btn btn-warm" onClick={() => act.answer(a.id)}>{t(['Answer', '去回答'])}</button></div></div>}
    </div>
    {why && <p className="r-why">{why}</p>}
    {box}
    {keys && <Hints keys={[['←', t(['back', '返回'])], ...(a.replyable && !busy ? [['⏎', t(['send', '发送'])] as [string, string]] : []), ['esc', t(['close', '关闭'])]]}/>}
  </div>;
}
// One session on a pop: its star and name. Asking opens its card; finished goes to it, ✕ marks it read.
function PopRow({ a, look, act, tag }: { a: Agent; look: MarkLook; act: NotchAct; tag?: ReactNode }) {
  const t = useT(), ask = a.state === 'wait', go = () => ask && a.request ? act.answer(a.id) : act.jump(a);
  return <div className={`u-row${ask ? ' is-ask' : ''}`} role="button" tabIndex={0} data-id={a.id}
    title={ask ? a.request ? t(['Asking you · open the card to answer', '在问你 · 打开卡片回答']) : waitingIn(a, t) : t(['Go to it', '前往这个会话'])}
    onClick={go} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } }}>
    <AgentMark look={look} state={a.state} id={a.id} size={12}/><span>{a.title}{tag}</span>
    <span className="u-acts">
      <Act tip={t(['Park it: out of your turn, no reminders, until you take it back', '先放着：不再轮到你，也不提醒，直到你拿回来'])} onClick={() => act.park([a.id])}><Moon size={12} weight="fill"/></Act>
      {!ask && <Act tip={t(['Mark as read', '标为已读'])} onClick={() => act.read([a.id])}><X size={12}/></Act>}</span>
  </div>;
}
function Pop({ agents, look, act, rate, onClose }: { agents: Agent[]; look: MarkLook; act: NotchAct; rate?: ReactNode; onClose: () => void }) {
  const t = useT(), errs = agents.filter(a => a.state === 'err').length, all = errs === agents.length, n = agents.length;
  const label = n === 1 ? errs ? t(['Stopped', '出错停了']) : t(['Done', '做完了']) : all ? t([`${n} stopped`, `${n} 个出错停了`]) : errs ? t([`${n} need a look`, `${n} 个要看一眼`]) : t([`${n} done`, `${n} 个做完了`]);
  return <div className="nt-card pop"><div className="c-bar"><span className={`c-label is-${all ? 'err' : 'done'}`}><i/>{label}</span>
    <button type="button" className="c-x" aria-label={t(['Close', '关闭'])} onClick={onClose}><X size={12}/></button></div>
    <div className="u-list">{agents.map(a => <PopRow key={a.id} a={a} look={look} act={act} tag={a.state === 'err' && !all ? <em> {t(['stopped', '出错停了'])}</em> : null}/>)}</div>{rate}</div>;
}

export function Notch({ look, agents, unread, parked, archived, geo, cursor, note, quiet, edge, aside, act, glow, pin, onNoteHover, port, keys, onKeys, onViewing, onJoinedChange }: {
  look: MarkLook; agents: Agent[]; unread: ReadonlySet<string>; parked: ReadonlyMap<string, number>; archived: ReadonlySet<string>; geo: NotchGeo;
  cursor: RefObject<Point>; note: NotchNote | null; quiet: boolean; edge: number | null; aside?: NotchAside; act: NotchAct; glow?: NotchGlow; pin?: NotchPin; onNoteHover: (on: boolean) => void;
  port: string | null; keys: number; onKeys: (on: boolean) => void; onViewing: (id: string | null) => void;
  onJoinedChange?: (joined: boolean) => void;
}) {
  const t = useT(), out = (a: Agent) => !parked.has(a.id);
  const turn = agents.filter(a => out(a) && (a.state === 'wait' || ended(a.state) && unread.has(a.id))).sort((a, b) => TURN_ORDER.indexOf(a.state) - TURN_ORDER.indexOf(b.state));
  const work = agents.filter(a => out(a) && working(a.state));
  const fin = agents.filter(a => out(a) && ended(a.state) && !unread.has(a.id) && !archived.has(a.id));
  const moon = agents.filter(a => parked.has(a.id)).sort((a, b) => parked.get(b.id)! - parked.get(a.id)!);
  const glows = glow?.items ?? [];
  const secs = ([{ key: 'turn', label: t(['Your turn', '轮到你']), ts: turn, gs: glows }, { key: 'work', label: t(['Working', '在干活']), ts: work }, { key: 'done', label: t(['Finished', '做完了']), ts: fin },
    { key: 'moon', label: t(['Parked', '先放着']), ts: moon }] as Sec[]).filter(s => s.ts.length || s.gs?.length);
  const order = secs.flatMap(s => s.ts.map(a => a.id));
  const [open, setOpen] = useState(false), [hot, setHot] = useState(''), [dragging, setDragging] = useState(false);
  const [kb, setKb] = useState<Keys | null>(null), [kbCard, setKbCard] = useState(''), [drafts] = useState(() => new Map<string, string>()), [, redraw] = useState(0);
  // The row under the keys can leave (parked, archived, answered elsewhere): the keys stay at the same height.
  if (kb && kb.view === 'list' && order.length && !order.includes(kb.id)) { kb.i = Math.min(kb.i, order.length - 1); kb.id = order[kb.i]; }
  const kbAgent = kb ? agents.find(a => a.id === kb.id) : undefined;
  const paged = kb?.view === 'page' && kbAgent && !kbCard ? kbAgent : undefined;
  const root = useRef<HTMLDivElement>(null), tip = useRef<HTMLDivElement>(null), shape = useRef<SVGPathElement>(null), fx = useRef<HTMLCanvasElement>(null), hit = useRef<HTMLDivElement>(null);
  const drop = useRef<HTMLDivElement>(null), dropIn = useRef<HTMLDivElement>(null), noteP = useRef<HTMLDivElement>(null), noteIn = useRef<HTMLDivElement>(null);
  const st = useRef({
    boxes: [] as Box[], wingTarget: 0, opened: false, dirty: true, innerL: 0, lastIn: 0, wantAt: 0, want: false, open: false, hot: '',
    dropGoal: { w: ALL_W, d: 0 }, noteGoal: { w: 24, d: 0 }, noteL: 0, onNote: false,
    s: { ww: spring(0), dx: spring(0), dw: spring(24), dd: spring(0), nx: spring(0), nw: spring(24), nd: spring(0) },
    swipe: 0, swipeT: 0 as ReturnType<typeof setTimeout> | 0,
    press: null as { x: number; y: number } | null, drag: null as { x: number; y: number } | null,
    puffs: [] as { x: number; y: number; c: string; at: number }[], workAt: -1e9, workN: 0, turnIds: new Set<string>(), turnAt: -1e9,
    // Sessions on their way into the moon, from where the pointer was, and when the last one landed.
    flights: [] as { id: string; to: Kind; st: AgentState; x: number; y: number; at: number }[],
    bumpAt: { turn: -1e9, work: -1e9, done: -1e9, moon: -1e9 }, parkedIds: new Set<string>(),
    pin: null as { x0: number; x1: number } | null, pinOn: -1e9, pinId: '', pinLabel: '',
    popOrigins: new Map<string, Point>(), intent: new PointerIntent(), resolvedNote: '', workLandingUntil: 0, joined: false, asideKey: null as Kind | null,
  }).current;
  const L = useRef({ look, turn, work, fin, moon, glows, pin, geo, note, quiet, edge, aside, onNoteHover, onJoinedChange, held: false, pageW: false, say: t });
  L.current = { look, turn, work, fin, moon, glows, pin, geo, note, quiet, edge, aside, onNoteHover, onJoinedChange, held: !!kb && !kbCard, pageW: !!paged, say: t };
  const members = (key: Kind) => ({ turn: L.current.turn, work: L.current.work, done: L.current.fin, moon: L.current.moon })[key];
  // What a group's mark counts: its sessions, and for the turn its glows too.
  const size = (key: Kind) => members(key).length + (key === 'turn' ? L.current.glows.length : 0);
  const setPanel = (on: boolean) => { if (on === st.open) return; st.open = on; st.dirty = true; setOpen(on); };
  const setHotKey = (key: string) => { if (key === st.hot) return; st.hot = key; setHot(key); };
  const returnApproval = (id: string, point: Point) => {
    if (!note || st.resolvedNote === note.key) return;
    st.resolvedNote = note.key;
    if (!noteP.current?.classList.contains('is-open')) return;
    // Keep a destination through the daemon's next session poll, even if this
    // was the only waiting session and Working has not arrived in the snapshot.
    st.workLandingUntil = performance.now() + 3000;
    st.flights.push({ id, to: 'work', st: 'wait', ...point, at: performance.now() });
  };
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
    for (const a of moon) if (!st.parkedIds.has(a.id) && p) st.flights.push({ id: a.id, to: 'moon', st: a.state, x: p.x, y: p.y, at: performance.now() });
    st.parkedIds = new Set(moon.map(a => a.id));
  }, [moon.map(a => a.id).join()]);
  // A pop returns each star to its current group. Counts wait for the flight to
  // land; the notice queue continues to own whether a session has been read.
  const previousPop = useRef<{ key: string; agents: Agent[] } | null>(null);
  useLayoutEffect(() => {
    const previous = previousPop.current;
    if (previous && previous.key !== note?.key && !L.current.held && !quiet) {
      for (const a of previous.agents) {
        if (parked.has(a.id) || archived.has(a.id) || !agents.some(x => x.id === a.id)) continue;
        const origin = st.popOrigins.get(a.id);
        if (!origin) continue;
        st.flights.push({ id: a.id, to: unread.has(a.id) ? 'turn' : 'done', st: a.state, ...origin, at: performance.now() });
      }
      st.popOrigins.clear();
    }
    previousPop.current = note?.pop ? { key: note.key, agents: agents.filter(a => note.pop!.includes(a.id)) } : null;
  }, [note?.key, note?.pop?.join()]);
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
      const target = e.target instanceof HTMLElement ? e.target : null;
      if (target?.closest('button,input,textarea,[contenteditable],[role="menu"],[role="listbox"],[role="combobox"]') && !root.current?.contains(target)) return;
      if (kbCard) {
        // A visible approval owns Escape as Deny; its document listener follows
        // this window listener. Other cards keep the island's Back behavior.
        if (k === 'Escape' && noteP.current?.classList.contains('is-open') && noteIn.current?.querySelector('[data-deny]')) return;
        if (k === 'Escape' || (k === 'ArrowLeft' && !field)) { stop(); act.back(); setKbCard(''); } return;
      }
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
    // The wing never reaches past `edge` (the Dashboard's side while it hangs below): one that would folds into
    // the notch, and the panel's Agents row shows the same marks.
    const wingGoal = () => { const e = L.current.edge; return e !== null && L.current.geo.notchR + st.wingTarget > e ? 0 : st.wingTarget; };
    // Her lobe to the last mark. Closed it may lean right when the marks outgrow her side.
    const island = () => { const g = L.current.geo; return { l: g.lobeL, w: g.notchR + Math.max(0, wingGoal()) - g.lobeL }; };
    // Three widths share the notch's centre; the skyline joins their shoulders to the wing.
    const span = (w: number) => { const ww = Math.min(w, L.current.geo.width - 16); return { l: (L.current.geo.width - ww) / 2, w: ww }; };
    // The marks and the panes, laid out and drawn. True while something is still moving.
    const frame = (now: number, dt: number) => {
      const { look, geo: g, note, quiet, onNoteHover, held, pageW } = L.current, s = st.s, top = g.top;
      const firm = reduced.matches, go = (sp: typeof s.ww, goal: number, hz: number, damp: number) => step(sp, goal, hz, firm ? 1 : damp, dt);
      const p = cursor.current, ww = Math.max(0, s.ww.value);
      const speed = st.intent.sample(p, now);
      const inWing = !!p && p.y <= top + 2 && p.x >= g.notchR && p.x <= g.notchR + ww + 2;
      // The pinned trip (ADR 0200): the pointer on its pill shows the trip in one line; it never opens the panel.
      const pinNow = L.current.pin?.item ?? null, onPin = !!p && inWing && !!pinNow && !!st.pin && p.x >= st.pin.x0 && p.x < st.pin.x1;
      if (onPin) { if (st.pinOn < 0) st.pinOn = now; } else st.pinOn = -1;
      st.pinId = pinNow?.id ?? '';
      if (tip.current) {
        const show = onPin && !L.current.note && now - st.pinOn >= PIN_TIP_MS && !L.current.held;
        if (show && pinNow) {
          const line = pinTrip(pinNow, L.current.say);
          if (tip.current.textContent !== line) tip.current.textContent = line;
          tip.current.style.transform = `translate(${st.pin!.x1}px,${top + 6}px) translateX(-100%)`;
        }
        tip.current.classList.toggle('is-on', show);
      }
      // One mark per group, so the row is at most four marks. Nothing moves while the pointer is on the row or the
      // panel is open; the counts follow along, and the row catches up once you leave.
      if (st.drag || !st.boxes.length || !(st.open || inWing) || now < st.workLandingUntil && !st.boxes.some(b => b.key === 'work')) {
        const kinds = (['turn', 'work', 'done', 'moon'] as const).filter(key => size(key) || key === 'work' && now < st.workLandingUntil);
        // The pinned trip (ADR 0200) is a pill after the marks. Where the Dashboard leaves no room for it beside the marks it steps aside, the marks stay.
        const pinned = L.current.pin?.item ?? null, pinW = pinned ? pinWidth(pinned, L.current.say) : 0;
        const lay = (withPin: boolean) => {
          const pw = withPin ? pinW : 0, lead = kinds.length ? PIN_GAP : 0;
          if (look === 'dot') {
            // 点线环 matches her lobe on the notch's other side, 64 pt, and only grows when the counts need the room.
            const widths = kinds.map(key => DOT_COUNT_X + DIGIT_W * Math.min(3, String(size(key)).length));
            const marksW = widths.reduce((sum, w) => sum + w, 0) + DOT_GAP * Math.max(0, kinds.length - 1), used = marksW + (pw ? lead + pw : 0);
            st.wingTarget = kinds.length || pw ? Math.max(DOT_WING_W, Math.ceil(used + 2 * PAD)) : 0;
            let x = g.notchR + (st.wingTarget - used) / 2;
            st.boxes = kinds.map((key, i) => { const b = { key, x0: x - DOT_GAP / 2, x1: x + widths[i] + DOT_GAP / 2, cx: x + DOT_R }; x += widths[i] + DOT_GAP; return b; });
            st.pin = pw ? { x0: kinds.length ? x - DOT_GAP + lead : x, x1: (kinds.length ? x - DOT_GAP + lead : x) + pw } : null;
          } else if (pw) {
            let x = g.notchR + PAD;
            st.boxes = kinds.map(key => { const b = { key, x0: x, x1: x + GCELL, cx: x + GCX }; x += GCELL; return b; });
            const x0 = kinds.length ? x + lead : x;
            st.pin = { x0, x1: x0 + pw };
            st.wingTarget = Math.ceil(st.pin.x1 - g.notchR + PAD);
          } else {
            let x = g.notchR + PAD + (4 - kinds.length) * GCELL / 2;
            st.boxes = kinds.map(key => { const b = { key, x0: x, x1: x + GCELL, cx: x + GCX }; x += GCELL; return b; });
            st.wingTarget = kinds.length ? WING_W : 0;
            st.pin = null;
          }
        };
        lay(!!pinned);
        const e = L.current.edge;
        if (pinned && e !== null && g.notchR + st.wingTarget > e) lay(false);
        // What the row shows, for the checks: `turn2 work4 done3 moon1`.
        const marks = [...st.boxes.map(b => `${b.key}${size(b.key)}`), ...st.pin ? ['pin'] : []].join(' ');
        if (root.current!.dataset.marks !== marks) root.current!.dataset.marks = marks;
      }
      // A slow 140 ms dwell signals intent. The exit grace bridges the growing
      // panel; a pointer travelling into it keeps it open. Clicks act immediately.
      if (!st.drag) {
        const inDrop = !!p && st.open && p.x >= s.dx.value && p.x <= s.dx.value + s.dw.value && p.y >= top - 2 && p.y <= s.dd.value;
        const slot = inWing ? st.boxes.find(b => p!.x >= b.x0 && p!.x < b.x1) : undefined;
        const approaching = st.open && st.intent.headingTo({ left: s.dx.value, right: s.dx.value + s.dw.value, top, bottom: s.dd.value });
        const want = held || (!note && !quiet && (inWing && !onPin || inDrop || approaching));
        if (!held && slot) setHotKey(slot.key);
        const ak = L.current.aside && slot ? slot.key : null;
        if (ak !== st.asideKey) { st.asideKey = ak; L.current.aside?.hover(ak); }
        if (want && (inWing || inDrop || approaching)) st.lastIn = now;
        if (want !== st.want) { st.want = want; st.wantAt = now; }
        if (want && !st.open && (held || now - st.wantAt >= HOVER_DWELL_MS && speed < HOVER_SPEED)) setPanel(true);
        else if (!want && st.open && (note || quiet || now - st.lastIn > HOVER_EXIT_MS)) setPanel(false);
        if (st.open && !held && !st.boxes.some(b => size(b.key))) setPanel(false);
      }
      // The page opens with the marks already out, not grown in from nothing.
      if (!st.opened) { st.opened = true; s.ww.value = wingGoal(); }
      let moving = go(s.ww, wingGoal(), SPRINGS.control.frequency, SPRINGS.control.damping);
      const dIn = dropIn.current!, nIn = noteIn.current!;
      if (st.dirty) {
        st.dirty = false;
        if (st.open) { const sp = span(pageW ? PAGE_W : ALL_W); dIn.style.width = `${sp.w}px`; st.dropGoal = { w: sp.w, d: top + dIn.offsetHeight + 2 }; }
        if (note) {
          const sp = span(note.pop ? POP_W : CARD_W);
          nIn.style.width = `${sp.w}px`; st.noteL = sp.l;
          st.noteGoal = { w: sp.w, d: Math.min(top + 640, top + nIn.offsetHeight + 2) };
        }
      }
      // Closed, both panes rest inside the island, so they grow out of it and fold back into it.
      const isl = island(), rest = { l: isl.l, w: isl.w, d: top * .6 }, dw = st.dropGoal.w;
      const dg = st.open ? { l: span(dw).l, w: dw, d: st.dropGoal.d } : rest;
      if (st.open && s.dd.value < top) { s.dx.value = isl.l; s.dw.value = isl.w; }
      if (!st.open) for (const [sp, goal] of [[s.dx, dg.l], [s.dw, dg.w], [s.dd, dg.d]] as const) {
        if (sp.velocity * (goal - sp.value) < 0) sp.velocity = 0;
      }
      const panelHz = SPRINGS.panel.frequency / (st.open ? 1 : MOTION.exit);
      moving = go(s.dx, dg.l, panelHz, SPRINGS.panel.damping) || moving; moving = go(s.dw, dg.w, panelHz, SPRINGS.panel.damping) || moving; moving = go(s.dd, dg.d, panelHz, SPRINGS.panel.damping) || moving;
      const noteOpen = !!note && !held && note.key !== st.resolvedNote;
      const ng = noteOpen ? { l: st.noteL, w: st.noteGoal.w, d: st.noteGoal.d } : rest;
      if (noteOpen && s.nd.value < top) { s.nx.value = isl.l; s.nw.value = isl.w; }
      if (!noteOpen) for (const [sp, goal] of [[s.nx, ng.l], [s.nw, ng.w], [s.nd, ng.d]] as const) {
        if (sp.velocity * (goal - sp.value) < 0) sp.velocity = 0;
      }
      const noteHz = SPRINGS.panel.frequency / (noteOpen ? 1 : MOTION.exit);
      moving = go(s.nx, ng.l, noteHz, SPRINGS.panel.damping) || moving; moving = go(s.nw, ng.w, noteHz, SPRINGS.panel.damping) || moving; moving = go(s.nd, ng.d, noteHz, SPRINGS.panel.damping) || moving;
      if (note?.pop && noteOpen) for (const row of nIn.querySelectorAll<HTMLElement>('.u-row')) {
        const mark = row.querySelector('.agent-mark')?.getBoundingClientRect();
        if (mark && row.dataset.id) st.popOrigins.set(row.dataset.id, { x: mark.x + mark.width / 2, y: mark.y + mark.height / 2 });
      }
      const onNote = noteOpen && !!p && p.x >= s.nx.value && p.x <= s.nx.value + s.nw.value && p.y >= top && p.y <= s.nd.value;
      if (onNote !== st.onNote) { st.onNote = onNote; onNoteHover(onNote); }
      // One black piece: her island, the notch, the marks, and whatever hangs from them.
      const wingR = g.notchR + Math.max(0, s.ww.value), dropOut = st.open || s.dd.value > top + 1, noteOut = noteOpen || s.nd.value > top + 1, rects: Rect[] = [];
      // The home shares this surface until the last pane has folded inside it,
      // including list/page/card handoffs and the visible tail of closing.
      const joined = dropOut || noteOut;
      if (joined !== st.joined) { st.joined = joined; L.current.onJoinedChange?.(joined); }
      if (wingR > g.notchR + .5 || dropOut || noteOut) rects.push({ l: g.lobeL, r: Math.max(g.notchR, wingR), d: top });
      if (dropOut) rects.push({ l: s.dx.value, r: s.dx.value + s.dw.value, d: s.dd.value });
      if (noteOut) rects.push({ l: s.nx.value, r: s.nx.value + s.nw.value, d: s.nd.value });
      shape.current!.setAttribute('d', skyline(rects));
      if (st.open) st.innerL = dg.l;
      place(drop.current!, dIn, s.dx.value, s.dw.value, s.dd.value, st.innerL, st.dropGoal.d, st.open, top);
      place(noteP.current!, nIn, s.nx.value, s.nw.value, s.nd.value, st.noteL, st.noteGoal.d, noteOpen, top);
      Object.assign(hit.current!.style, { left: `${g.notchR}px`, width: `${Math.max(0, s.ww.value)}px`, height: `${top}px` });
      draw(now, top, look);
      return moving || !!st.drag || st.puffs.length > 0 || st.flights.length > 0 || Object.values(st.bumpAt).some(at => now - at < MOTION.medium);
    };
    const place = (pane: HTMLElement, inner: HTMLElement, left: number, w: number, d: number, goalL: number, goalD: number, open: boolean, top: number) => {
      const h = Math.max(0, d - top), k = open ? clamp(h / Math.max(1, goalD - top)) : 0;
      pane.style.transform = `translate(${left}px,${top}px)`; pane.style.width = `${Math.max(0, w)}px`; pane.style.height = `${h}px`;
      pane.style.visibility = h < 1 ? 'hidden' : 'visible';
      pane.classList.toggle('is-open', open && k > .6);
      inner.style.left = `${goalL - left}px`;
      inner.style.transitionDuration = open ? 'var(--motion-fast)' : 'var(--exit-fast)';
      inner.style.transitionTimingFunction = open ? 'var(--ease-out)' : 'var(--ease-in)';
      inner.style.opacity = open && k >= .6 ? '1' : '0';
    };
    // The marks, clipped to the wing as it grows; the finished mark being dragged; the puffs left where it went.
    const draw = (now: number, top: number, look: MarkLook) => {
      const cv = fx.current!, { geo: g, turn, work, glows, note, say } = L.current, d = dpr(), W = g.width, H = window.innerHeight, t = now / 1000;
      // H is the stage's height (fitWindow), not the fitted window's: the canvas is that tall on the page too, or a short window squeezes the marks into its top.
      if (cv.width !== Math.round(W * d) || cv.height !== Math.round(H * d)) { cv.width = Math.round(W * d); cv.height = Math.round(H * d); cv.style.height = `${H}px`; }
      const ctx = cv.getContext('2d')!;
      ctx.setTransform(d, 0, 0, d, 0, 0); ctx.clearRect(0, 0, W, H);
      // News makes its mark jump once; the beacon rings for 4 s after each arrival. A new glow is news to the turn mark too.
      const turnIds = [...turn.map(a => a.id), ...glows.map(x => `glow:${x.id}`)];
      if (turnIds.some(id => !st.turnIds.has(id))) st.turnAt = now;
      if (work.length > st.workN) st.workAt = now;
      st.turnIds = new Set(turnIds); st.workN = work.length;
      ctx.save(); ctx.beginPath(); ctx.rect(g.notchR, 0, Math.max(0, st.s.ww.value), top); ctx.clip();
      const counts: string[] = [];
      for (const b of st.boxes) {
        const ts = members(b.key);
        if (!size(b.key) && !(b.key === 'work' && now < st.workLandingUntil)) continue;
        const pulse = countPulse(clamp((now - st.bumpAt[b.key]) / MOTION.medium));
        ctx.save(); ctx.translate(b.cx, top / 2); ctx.scale(.8, .8);
        // 点线环 shows the turn as its most urgent member: the amber point that needs you, the red one that stopped and
        // never blinks, or, when all of them finished, the done ring, which fades once 10 min pass without a look.
        const lead = turnLead(look, turn, glows.length);
        if (b.key === 'turn' && lead !== 'wait') {
          if (lead === 'done' && ts.every(a => Date.now() - (a.at ?? Date.now()) > DONE_FADE_MS)) ctx.globalAlpha = .55;
          drawMark(ctx, look, lead, 0, (now - st.turnAt) / 1000, d);
        } else if (b.key === 'turn') drawTurnIcon(ctx, look, t, (now - st.turnAt) / 1000, d, now - st.turnAt > 4000);
        else if (b.key === 'work') drawMark(ctx, look, ts.length && ts.every(a => a.state === 'pack') ? 'pack' : 'work', t + seedOf('work'), (now - st.workAt) / 1000, d);
        else if (b.key === 'done') { ctx.globalAlpha = .55; drawMark(ctx, look, ts.every(a => a.state === 'err') ? 'err' : 'done', 0, 99, d); }
        else { ctx.globalAlpha = .85; drawMoon(ctx, 99, d); }
        ctx.restore();
        const dot = look === 'dot', turnRgb = dot ? COLOR[lead] : hueAt(t);
        ctx.save(); ctx.font = `600 ${dot ? 10 : 10.5}px "JetBrains Mono", Menlo, monospace`; ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
        ctx.fillStyle = b.key === 'turn' ? rgba(tint(turnRgb, .45)) : 'rgba(214,222,250,.62)';
        const hidden = new Set([...(note?.pop ?? []), ...st.flights.filter(f => f.to === b.key).map(f => f.id)]);
        const count = ts.filter(a => !hidden.has(a.id)).length + (b.key === 'turn' ? glows.length : 0);
        counts.push(`${b.key}${count}`);
        ctx.translate(b.cx + (dot ? DOT_COUNT_X - DOT_R : 7), top / 2 + .5); ctx.scale(pulse, pulse);
        ctx.fillText(count > 99 ? '99+' : String(count), 0, 0, 16); ctx.restore();
        if ((st.open || st.asideKey) && st.hot === b.key) {
          const c = b.key === 'turn' ? turnRgb.join(',') : b.key === 'moon' ? MOON_RGB : b.key === 'work' ? '108,156,255' : '111,224,180';
          ctx.fillStyle = `rgba(${c},.9)`; ctx.shadowColor = `rgba(${c},.9)`; ctx.shadowBlur = 6 * d;
          ctx.beginPath(); ctx.roundRect(b.cx - 5, top - 3.5, 10, 1.6, .8); ctx.fill(); ctx.shadowBlur = 0;
        }
      }
      // The pinned trip: a pill in the wing's own mark colours, amber from 5 min, its countdown read from the clock each frame.
      const trip = L.current.pin?.item;
      if (st.pin && trip) {
        const shown = pinLabel(trip, Date.now(), say), words = shown.text, c = COLOR.wait.join(',');
        if (!shown.gone) {
          ctx.save();
          if (trip.stale) ctx.globalAlpha = .6;
          ctx.fillStyle = shown.amber ? `rgba(${c},.16)` : 'rgba(214,222,250,.1)';
          ctx.beginPath(); ctx.roundRect(st.pin.x0, top / 2 - PIN_H / 2, st.pin.x1 - st.pin.x0, PIN_H, PIN_H / 2); ctx.fill();
          ctx.font = PIN_FONT; ctx.textBaseline = 'middle'; ctx.textAlign = 'center';
          ctx.fillStyle = shown.amber ? rgba(tint(COLOR.wait, .45)) : 'rgba(214,222,250,.62)';
          ctx.fillText(words, (st.pin.x0 + st.pin.x1) / 2, top / 2 + .5);
          ctx.restore();
        }
        if (root.current!.dataset.pin !== words) root.current!.dataset.pin = words;
      } else if (root.current!.dataset.pin) root.current!.dataset.pin = '';
      ctx.restore();
      // Pops and parked stars share the same arc, landing in their own group.
      const ty = top / 2;
      st.flights = st.flights.filter(f => {
        const k = reduced.matches ? 1 : (now - f.at) / MOTION.slow;
        const tx = st.boxes.find(b => b.key === f.to)?.cx ?? g.notchR + Math.max(0, st.s.ww.value) - 10;
        if (k >= 1) { st.bumpAt[f.to] = now; return false; }
        const e = 1 - (1 - k) ** 3, u = 1 - e, x = u * u * f.x + 2 * u * e * tx + e * e * tx, y = u * u * f.y + 2 * u * e * f.y + e * e * ty;
        ctx.save(); ctx.translate(x, y); ctx.scale(1 - .35 * e, 1 - .35 * e); ctx.globalAlpha = 1 - .5 * e;
        drawMark(ctx, look, f.st, t, 99, d); ctx.restore();
        return true;
      });
      root.current!.dataset.counts = counts.join(' ');
      root.current!.dataset.flights = String(st.flights.length);
      const ringing = look === 'dot' ? turnLead(look, turn, glows.length) === 'wait' && now - st.turnAt < 2000 * DOT_RING_S : now - st.turnAt <= 4000;
      root.current!.dataset.rings = st.boxes.some(b => b.key === 'turn') && ringing ? '1' : '';
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
        ctx.fillText(out ? say(['Release to archive', '松手归档']) : say(['Drag down to archive', '往下拖来归档']), 0, 16);
        ctx.restore();
      }
      st.puffs = st.puffs.filter(f => now - f.at < MOTION.slow);
      for (const f of st.puffs) {
        const k = (now - f.at) / MOTION.slow;
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
    return () => { cancelAnimationFrame(raf); clearTimeout(timer); st.joined = false; L.current.onJoinedChange?.(false); };
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
    : secs.length ? <Panel secs={secs} hot={kb ? kbHot : hot} cur={cur} look={look} act={A} glow={glow && { ...glow, open: g => { if (kb) kbClose(); setPanel(false); glow.open(g); } }}
      page={id => kbOpen(id, 'page')} done={() => { if (!kb) setPanel(false); }}/> : null;
  const popAgents = note?.pop?.map(id => agents.find(a => a.id === id)).filter((a): a is Agent => !!a) ?? [];
  const lastPopAgents = useRef<Agent[]>([]);
  if (note?.pop) lastPopAgents.current = popAgents;
  // The panes keep what they showed while they fold away.
  const lastNote = useRef<NotchNote | null>(null), lastPanel = useRef<ReactNode>(null);
  if (note) lastNote.current = note;
  if (open && panel) lastPanel.current = panel;
  const shownNote = note ?? lastNote.current;
  return <div ref={root} className="notch">
    <svg className="notch-shape" aria-hidden="true"><path ref={shape}/></svg>
    <div ref={tip} className="notch-pin-tip" aria-hidden="true"/>
    <canvas ref={fx} className="notch-fx" data-look={look} aria-hidden="true" style={{ width: geo.width }}/>
    <div ref={hit} className="notch-hit" data-hit aria-hidden="true"
      onPointerDown={e => {
        // The pinned trip's pill: a click opens its card (ADR 0202). It opens no panel.
        const trip = L.current.pin?.item;
        if (st.pin && trip && e.clientX >= st.pin.x0 && e.clientX < st.pin.x1) {
          L.current.pin!.open(trip);
          return;
        }
        const b = st.boxes.find(x => e.clientX >= x.x0 && e.clientX < x.x1);
        if (L.current.aside) { if (b) L.current.aside.open(b.key); return; }
        if (b) setHotKey(b.key);
        st.lastIn = performance.now(); setPanel(true);
        if (note) kbOpen();
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
    <div ref={drop} className="notch-pane notch-drop" data-hit={open ? true : undefined} role="dialog" aria-label={t(['Sessions', '会话'])}>
      <div ref={dropIn} className="notch-pane-in">{open ? panel : lastPanel.current}</div></div>
    <div ref={noteP} className="notch-pane notch-note" data-hit={note && !(kb && !kbCard) ? true : undefined} role="alertdialog" aria-label={t(['Notification', '通知'])}>
      <div ref={noteIn} className="notch-pane-in" onWheel={e => {
        // A sideways swipe on a notice puts it away; a slow drift or a vertical scroll does not.
        if (!shownNote || Math.abs(e.deltaX) < Math.abs(e.deltaY) * 2) return;
        st.swipe += e.deltaX;
        clearTimeout(st.swipeT); st.swipeT = setTimeout(() => { st.swipe = 0; }, 200);
        if (Math.abs(st.swipe) > 90) { st.swipe = 0; shownNote.onClose(); }
      }}><NoticeFlightContext.Provider value={returnApproval}>{shownNote && (shownNote.pop ? lastPopAgents.current.length > 0 && <Pop agents={lastPopAgents.current} look={look} act={A} rate={shownNote.rate} onClose={shownNote.onClose}/> : shownNote.card)}</NoticeFlightContext.Provider></div></div>
    {dragging && <div className="notch-catch" data-hit aria-hidden="true"/>}
  </div>;
}

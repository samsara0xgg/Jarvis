import { useEffect, useReducer, useRef, useState, type ReactNode } from 'react';
import { ArrowSquareOut, ArrowUp, Check, Moon } from '@phosphor-icons/react';
import { AGENT_NAME, loadMarks, openLabel, saveMark, type Agent, type AgentRequest, type AgentState } from './agents';
import { AgentMark, type MarkLook } from './AgentMarks';
import { Markdown } from './Markdown';
import { palette, play, scoreOf } from './soundKit';
import type { ExprId } from './starCore';
import './notices.css';

// Agent notices, after the notch lab (ADR 0057, 0069). A session that finishes or stops while Allen is not looking at
// it joins his turn and pops up its name from the island for 5 s; one that needs him gets a card hanging from the
// island, answered right there; it folds away after 30 s untouched and comes back once, softer, 10 minutes later.
// Park (先放着) takes a session off his turn: no pops, no cards, no reminder, until he takes it back or it does
// something new. Finishes within 1.5 s share one pop; needs-you cards go ahead of pops; nothing shows while she
// talks, while the Dashboard is open or while the keys hold the island, and it all comes up after. Nothing pops for
// the session he is looking at, in Ghostty or on its page in the island.
const POP_MS = 5000, FOLD_MS = 30_000, REMIND_MS = 600_000, TOGETHER_MS = 1500, CONFIRM_MS = 850;
type Base = { key: string; id: string; at: number; reminded?: boolean };
export type Notice = Base & (
  | { kind: 'pop'; ids: string[] }
  // Needs you, answered elsewhere: a Codex approval, or a Claude prompt Jarvis is not holding.
  | { kind: 'wait'; line: string }
  | { kind: 'req'; req: AgentRequest });
type Arrival = Notice extends infer N ? N extends Notice ? Omit<N, 'key' | 'at'> : never : never;
export const needs = (n: Notice) => n.kind !== 'pop';
export const ended = (state: AgentState) => state === 'done' || state === 'err';
// One question's pick: an option, several options, or typed words.
type Pick = number | number[] | string;
type Card = { qi: number; picks: (Pick | undefined)[]; review: boolean; feedback: boolean; ok: string; pending?: boolean; error?: string };

// The sound kit from 星核的声音: palette 水滴·脆, the "fifths" score. The context sleeps between cues.
let audio: { ctx: AudioContext; out: GainNode; sleep?: ReturnType<typeof setTimeout> } | null = null;
export function noticeCue(name: 'ask' | 'done' | 'error' | 'send' | 'close', volume: number, gain = 1) {
  const cue = scoreOf('fifths')[name];
  if (!cue || volume <= 0) return;
  try {
    if (!audio) { const ctx = new AudioContext(), out = ctx.createGain(); out.connect(ctx.destination); audio = { ctx, out }; }
    const a = audio;
    clearTimeout(a.sleep);
    a.out.gain.value = volume / .35 * .8;
    void a.ctx.resume().then(() => {
      const handle = play(a.ctx, a.out, cue, palette('dropCrisp'), gain);
      a.sleep = setTimeout(() => void a.ctx.suspend().catch(() => {}), (handle.end - a.ctx.currentTime) * 1000 + 300);
    });
  } catch { /* sound is optional */ }
}

// Allen's turn (ADR 0057, 0069): finished or stopped sessions he has not looked at, the ones he parked and the ones he
// archived. With a daemon they live there, for every surface; her profile keeps each session's last state, so a
// finish while she was closed still counts, and a copy of the lists for a companion without a daemon.
const TURN = 'companion-turn-v1', KEEP_MS = 2 * 86_400_000;
type Kept = { unread: string[]; cleared: string[]; last: Record<string, [AgentState, number]> };
function loadKept(): Kept {
  try {
    const v = JSON.parse(localStorage.getItem(TURN) ?? '{}');
    return { unread: Array.isArray(v.unread) ? v.unread : [], cleared: Array.isArray(v.cleared) ? v.cleared : [], last: v.last && typeof v.last === 'object' ? v.last : {} };
  } catch { return { unread: [], cleared: [], last: {} }; }
}

type Tone = 'ask' | 'done' | 'error';
// The queue behind the pops and cards. `hold` keeps every one back (she is talking, the Dashboard is open, the keys
// hold the island) except a card brought forward on purpose; `watched` is the session Allen has been looking at in
// Ghostty for 1.5 s, `viewing` the one whose page is open in the island.
export function useNotices({ port, agents, hold, watched, viewing, cue, answer }: {
  port: string | null; agents: Agent[]; hold: boolean; watched: string | null; viewing: string | null; cue: (name: Tone | 'send' | 'close', gain?: number) => void;
  answer: (req: AgentRequest, body: { decision: 'allow' | 'always' | 'deny'; answers?: Record<string, string>; message?: string }) => Promise<boolean>;
}) {
  const [, bump] = useReducer((x: number) => x + 1, 0);
  const [s] = useState(() => {
    const kept = loadKept();
    return {
      queue: [] as Notice[], folded: [] as Notice[], cards: new Map<string, Card>(), shownReqs: new Set<string>(),
      unread: new Set(kept.unread), archived: new Set(kept.cleared), parked: new Map<string, number>(), last: kept.last,
      soundAt: -1e9, shown: '', openedAt: 0, peek: false, touched: '', forced: '',
      // Sessions changed here before the daemon's marks arrived: their marks stay as she set them.
      loaded: false, early: new Set<string>(),
      // Her face for a moment after an answer: pleased (with a hop) or refusing.
      over: null as { face: ExprId; until: number; hop: boolean } | null,
      timers: new Set<ReturnType<typeof setTimeout>>(),
    };
  });
  const save = () => { try { localStorage.setItem(TURN, JSON.stringify({ unread: [...s.unread], cleared: [...s.archived], last: s.last })); } catch { /* the list just is not remembered */ } };
  const persist = (id: string) => { if (!s.loaded) s.early.add(id); if (port) saveMark(port, id, { unread: s.unread.has(id), park: s.parked.has(id), archive: s.archived.has(id) }); };
  // The daemon's marks win; the first time it has none, her profile's lists move there.
  useEffect(() => {
    if (!port) return;
    void loadMarks(port).then(marks => {
      if (!marks) return;
      const ids = Object.keys(marks), mine = { unread: s.unread, archived: s.archived, parked: s.parked };
      s.loaded = true;
      if (!ids.length) { new Set([...s.unread, ...s.archived, ...s.parked.keys()]).forEach(persist); return; }
      s.unread = new Set(ids.filter(id => marks[id].unread));
      s.archived = new Set(ids.filter(id => marks[id].archived_ms));
      s.parked = new Map(ids.filter(id => marks[id].parked_ms).map(id => [id, marks[id].parked_ms!]));
      for (const id of s.early) {
        if (mine.unread.has(id)) s.unread.add(id); else s.unread.delete(id);
        if (mine.archived.has(id)) s.archived.add(id); else s.archived.delete(id);
        if (mine.parked.has(id)) s.parked.set(id, mine.parked.get(id)!); else s.parked.delete(id);
      }
      save(); bump();
    });
  }, [port]);
  const later = (ms: number, run: () => void) => { const t = setTimeout(() => { s.timers.delete(t); run(); }, ms); s.timers.add(t); };
  useEffect(() => () => s.timers.forEach(clearTimeout), []);
  const byId = new Map(agents.map(a => [a.id, a]));
  const live = useRef({ byId, hold });
  live.current = { byId, hold };
  const card = (n: Notice) => { let c = s.cards.get(n.key); if (!c) s.cards.set(n.key, c = { qi: 0, picks: [], review: false, feedback: false, ok: '' }); return c; };
  const toneOf = (n: Notice): Tone => needs(n) ? 'ask' : n.kind === 'pop' && n.ids.every(id => byId.get(id)?.state === 'err') ? 'error' : 'done';
  const sound = (n: Notice, gain = 1) => { const now = performance.now(); if (now - s.soundAt > TOGETHER_MS) { s.soundAt = now; cue(toneOf(n), gain); } };

  const arrive = (a: Arrival) => {
    if (s.parked.has(a.id)) return;
    const now = performance.now(), n = { ...a, key: `${a.kind}:${a.id}:${now}`, at: now } as Notice, head = s.queue[0], tail = s.queue.at(-1);
    if (n.kind === 'pop') {
      // Into the pop on screen, or the one that came up less than 1.5 s ago.
      if (head?.kind === 'pop' && !live.current.hold) { head.ids = [...head.ids.filter(x => x !== n.id), n.id]; return; }
      if (tail?.kind === 'pop' && now - tail.at < TOGETHER_MS) { tail.ids.push(n.id); tail.at = now; return; }
      s.queue.push(n);
      return;
    }
    // Needs-you cards go ahead of pops; the one on screen keeps its place.
    const i = s.queue.findIndex((m, j) => j > 0 && !needs(m));
    if (i < 0) s.queue.push(n); else s.queue.splice(i, 0, n);
  };
  // Their names leave any pop; `cards` takes their needs-you cards away too.
  const drop = (ids: string[], cards = false) => {
    for (const n of s.queue) if (n.kind === 'pop') n.ids = n.ids.filter(id => !ids.includes(id));
    const keep = (n: Notice) => n.kind === 'pop' ? n.ids.length > 0 : !(cards && ids.includes(n.id));
    s.queue = s.queue.filter(keep); s.folded = s.folded.filter(keep);
  };
  // Out of his turn: looked at, opened or marked.
  const read = (ids: string[]) => {
    ids.forEach(id => { if (s.unread.delete(id)) { if (!s.loaded) s.early.add(id); if (port) saveMark(port, id, { seen: true }); } });
    drop(ids); save(); bump();
  };
  // Done with: off the island until it does something new, kept for the Agents window's archive.
  const archive = (ids: string[]) => {
    ids.forEach(id => { s.archived.add(id); s.unread.delete(id); s.parked.delete(id); persist(id); });
    cue('close'); drop(ids); save(); bump();
  };
  // 先放着: off his turn and quiet, a question still waiting, until he takes it back or it does something new.
  const park = (ids: string[]) => {
    ids.forEach(id => { s.parked.set(id, Date.now()); persist(id); });
    cue('close', .7); drop(ids, true); bump();
  };
  const unpark = (ids: string[]) => {
    ids.forEach(id => { s.parked.delete(id); if (ended(live.current.byId.get(id)?.state ?? 'work')) s.unread.add(id); persist(id); });
    save(); bump();
  };
  // What changed since the last poll. A session met for the first time only notifies through a held prompt.
  const key = agents.map(a => `${a.id}:${a.state}:${a.request?.id ?? ''}`).join('|');
  useEffect(() => {
    const now = Date.now(), touched = new Set<string>();
    for (const a of agents) {
      const was = s.last[a.id]?.[0], looking = a.id === watched || a.id === viewing;
      s.last[a.id] = [a.state, now];
      // A background session shows no dialog of its own while Jarvis holds its prompt, so that card comes even
      // while he looks at the session; an interactive one asks in his terminal at the same time. A new question
      // is something new: it brings a parked session back.
      if (a.request && !s.shownReqs.has(a.request.id)) {
        s.shownReqs.add(a.request.id);
        if (s.parked.delete(a.id)) touched.add(a.id);
        if (!looking || a.kind === 'background') arrive({ kind: 'req', id: a.id, req: a.request });
      }
      if (!was || was === a.state) continue;
      // Anything new brings it back from the moon or the archive.
      const unarchived = s.archived.delete(a.id), unparked = s.parked.delete(a.id);
      if (unarchived || unparked) touched.add(a.id);
      if (!ended(a.state)) {
        // Working again (or asking): it leaves his turn.
        if (s.unread.delete(a.id)) touched.add(a.id);
        if (a.state === 'wait' && !a.request && !looking) arrive({ kind: 'wait', id: a.id, line: a.last });
      } else if (!ended(was) && !looking) { s.unread.add(a.id); touched.add(a.id); arrive({ kind: 'pop', id: a.id, ids: [a.id] }); }
    }
    touched.forEach(persist);
    for (const [id, [, at]] of Object.entries(s.last)) if (!byId.has(id) && now - at > KEEP_MS) delete s.last[id];
    for (const set of [s.unread, s.archived, s.parked]) for (const id of set.keys()) if (!s.last[id]) set.delete(id);
    // A needs-you card whose session no longer waits on it was answered elsewhere.
    const stale = (n: Notice) => {
      if (!needs(n) || card(n).ok) return false;
      const a = byId.get(n.id);
      return !a || a.state !== 'wait' || (n.kind === 'req' ? a.request?.id !== n.req.id : !!a.request);
    };
    s.queue = s.queue.filter(n => !stale(n));
    s.folded = s.folded.filter(n => !stale(n));
    // Looking at a session on his turn reads it.
    if (watched && s.unread.has(watched)) read([watched]); else { save(); bump(); }
  }, [key, watched, viewing]);

  const current = hold && s.queue[0]?.key !== s.forced ? undefined : s.queue[0];
  // A notice coming up: her sound (once per 1.5 s), and the clock for her error face.
  if ((current?.key ?? '') !== s.shown) {
    s.shown = current?.key ?? '';
    if (current) { s.openedAt = performance.now(); sound(current); }
  }
  const next = () => { s.queue.shift(); s.forced = ''; bump(); };
  const fold = () => {
    const n = s.queue[0];
    if (!n || !needs(n)) return;
    s.queue.shift(); s.folded.push(n); s.forced = '';
    if (!n.reminded) later(REMIND_MS, () => remind(n));
    bump();
  };
  // Once, ten minutes on: she peeks out of the island with a softer sound, then the card comes back.
  const remind = (n: Notice) => {
    const i = s.folded.indexOf(n);
    if (i < 0 || s.parked.has(n.id)) return;
    s.folded.splice(i, 1); n.reminded = true; s.peek = true; bump();
    s.soundAt = -1e9; sound(n, .5); s.soundAt = performance.now();
    later(1100, () => { s.peek = false; if (!s.parked.has(n.id)) { s.queue.unshift(n); s.shown = n.key; s.openedAt = performance.now(); } bump(); });
  };
  // A card brought up from the island's list and put back with Esc: it waits behind the beacon, no reminder.
  const back = () => { if (s.queue[0] && needs(s.queue[0])) { s.queue.shift(); s.forced = ''; bump(); } };
  const [hover, setHovering] = useState(false);
  const setHover = (on: boolean) => { if (on && s.queue[0]) s.touched = s.queue[0].key; setHovering(on); };
  const ok = current ? card(current).ok : '';
  const size = current?.kind === 'pop' ? current.ids.length : 0;
  useEffect(() => {
    if (!current || hover || ok) return;
    // A pop the pointer has been on goes 1.5 s after it leaves.
    const t = setTimeout(() => needs(current) ? fold() : next(), needs(current) ? FOLD_MS : s.touched === current.key ? 1500 : POP_MS);
    return () => clearTimeout(t);
  }, [current?.key, hover, ok, size]);

  const resolve = async (n: Notice & { kind: 'req' }, text: string, body: Parameters<typeof answer>[1]) => {
    const c = card(n);
    if (c.ok || c.pending) return;
    c.pending = true; c.error = ''; bump();
    const yes = body.decision !== 'deny';
    try { c.ok = await answer(n.req, body) ? text : 'Already answered somewhere else'; }
    catch { c.error = 'Could not send your answer. Try again.'; return; }
    finally { c.pending = false; bump(); }
    s.over = { face: yes ? '02' : '38', until: performance.now() + 900, hop: yes };
    cue(yes ? 'send' : 'close');
    bump();
    later(CONFIRM_MS, () => { if (s.queue[0] === n) next(); });
  };
  // A session waiting on him, from his turn, the island's list or the Agents page: its card comes to the front,
  // even while the island is held.
  const focus = (id: string) => {
    const f = s.folded.findIndex(n => n.id === id && needs(n)), q = s.queue.findIndex(n => n.id === id && needs(n));
    if (f >= 0) s.queue.unshift(...s.folded.splice(f, 1));
    else if (q > 0) s.queue.unshift(...s.queue.splice(q, 1));
    else if (q < 0) { const a = live.current.byId.get(id); if (a?.request) s.queue.unshift({ key: `req:${id}:${performance.now()}`, id, at: performance.now(), kind: 'req', req: a.request }); }
    s.forced = s.queue[0]?.id === id ? s.queue[0].key : '';
    bump();
  };
  return { current, count: s.queue.filter(needs).length, peek: s.peek, openedAt: s.openedAt, over: s.over, card: current ? card(current) : null,
    unread: s.unread as ReadonlySet<string>, archived: s.archived as ReadonlySet<string>, parked: s.parked as ReadonlyMap<string, number>,
    read, archive, park, unpark, setHover, next, fold, back, resolve, focus, bump };
}

// ---------- the card ----------
type Question = { question: string; header?: string; multiSelect?: boolean; options?: { label: string; description?: string }[] };
type Body = Parameters<Parameters<typeof useNotices>[0]['answer']>[1];
const shortPath = (p: string) => p.split('/').filter(Boolean).slice(-2).join('/');
// An edit as diff lines: removed, then added, at most 14.
function diffLines(tool: string, i: Record<string, unknown>) {
  const lines = (text: unknown, sign: string) => typeof text === 'string' ? text.split('\n').map(t => [sign, t] as const) : [];
  const edits = Array.isArray(i.edits) ? i.edits as Record<string, unknown>[] : [i];
  const all = tool === 'Write' ? lines(i.content, '+') : edits.flatMap(e => [...lines(e.old_string, '-'), ...lines(e.new_string, '+')]);
  return { shown: all.slice(0, 14), more: Math.max(0, all.length - 14), add: all.filter(l => l[0] === '+').length, del: all.filter(l => l[0] === '-').length };
}
const pickText = (q: Question, p: Pick | undefined) => typeof p === 'number' ? q.options?.[p]?.label ?? '' : Array.isArray(p) ? p.map(k => q.options?.[k]?.label).join(', ') : p ?? '';

// A needs-you card: what the session wants, answered right on it.
export function NoticeCard({ n, agent, card, count, look, onPark, onOpen, onResolve, onChange }: {
  n: Notice & { kind: 'req' | 'wait' }; agent?: Agent; card: Card; count: number; look: MarkLook;
  onPark: () => void; onOpen: (agent: Agent) => void; onResolve: (text: string, body: Body) => void; onChange: () => void;
}) {
  const [typed, setTyped] = useState(''), [feedback, setFeedback] = useState(''), [alwaysAllowed, setAlwaysAllowed] = useState(false);
  const allowButton = useRef<HTMLButtonElement>(null);
  // Cards arrive without a keyboard gesture. A bare Return must never approve
  // one, including when its primary button happens to have focus.
  useEffect(() => {
    if (n.kind !== 'req' || n.req.tool === 'AskUserQuestion' || card.ok) return;
    const key = (e: KeyboardEvent) => {
      if (e.key !== 'Enter' || e.isComposing) return;
      const button = allowButton.current, pane = button?.closest('.notch-pane');
      if (!button?.checkVisibility({ opacityProperty: true, visibilityProperty: true }) || pane && !pane.classList.contains('is-open')) return;
      const target = e.target instanceof HTMLElement ? e.target : null;
      if (target?.closest('button,input,textarea,[contenteditable]') && !button.closest('.nc')?.contains(target)) return;
      if (e.target instanceof HTMLInputElement && n.req.tool === 'ExitPlanMode' && !e.metaKey) return;
      e.preventDefault(); e.stopImmediatePropagation();
      if (e.metaKey && !e.repeat && !card.pending) allowButton.current?.click();
    };
    window.addEventListener('keydown', key, true);
    return () => window.removeEventListener('keydown', key, true);
  }, [n.key, card.ok, card.pending]);
  const who = agent ? AGENT_NAME[agent.agent] : 'It';
  const label = n.kind === 'wait' ? 'Needs you' : n.req.tool === 'AskUserQuestion' ? `${who} asks` : n.req.tool === 'ExitPlanMode' ? 'Plan to review' : 'Needs your OK';
  const bar = <div className="nc-bar">
    <span className="nc-label is-wait"><i/>{label}{count > 1 && <em> · 1 of {count}</em>}</span>
  </div>;
  const open = agent && openLabel(agent);
  const head = agent && <>
    <div className="nc-head">
      <AgentMark look={look} state="wait" id={agent.id} size={12}/>
      <div className="nc-t">
        <div className="nc-top"><b>{agent.title}</b><span className="age">now</span></div>
        <div className="nc-tags"><span className={`tagc ${agent.agent}`}>{who}</span>{agent.project && <span className="tagc">{agent.project}</span>}
          {agent.branch && <span className="tagc">{agent.branch.replace(/^worktree-/, '')}</span>}</div>
      </div>
      <div className="nc-actions"><button type="button" className="nc-icon nc-park" aria-label="Park" title="Park: out of your turn until you take it back" onClick={onPark}><Moon size={14} weight="fill"/></button>
        {open && <button type="button" className="nc-icon" aria-label={open} title={open} onClick={() => onOpen(agent)}><ArrowSquareOut size={14}/></button>}</div>
    </div>
    {agent.you && <p className="nc-you"><b>You</b>{agent.you}</p>}
  </>;
  if (card.ok) return <div className="nc">{bar}{head}<p className="nc-ok"><Check size={14} weight="bold"/><span>{card.ok}</span></p></div>;
  const choice = (always: string) => {
    const request = n.kind === 'req' ? n.req : null;
    const project = agent?.project || request?.cwd.split('/').filter(Boolean).at(-1) || request?.cwd;
    const bashRule = request?.tool === 'Bash' ? always.match(/^Don't ask again for Bash\((.*)\)$/)?.[1] ?? String(request.input.command ?? '') : '';
    return <>{always && <label className="nc-always" title={always}>
      <input type="checkbox" checked={alwaysAllowed} disabled={card.pending} onChange={e => setAlwaysAllowed(e.target.checked)}/>
      <span>{bashRule ? <>Always allow <code>{bashRule}</code></> : always.replace("Don't ask again for", 'Always allow')} in <b>{project}</b></span>
    </label>}<div className="nc-choice">
      <button type="button" className="btn btn-ghost" disabled={card.pending} onClick={() => onResolve(`Denied · ${who} will try another way`, { decision: 'deny' })}>Deny</button>
      <button ref={allowButton} type="button" className="btn btn-warm" disabled={card.pending} onClick={() => onResolve(alwaysAllowed && always ? `Allowed · ${always.replace("Don't ask", "won't ask")}` : `Allowed · ${who} continues`, { decision: alwaysAllowed && always ? 'always' : 'allow' })}>Allow <kbd>⌘⏎</kbd></button>
    </div></>;
  };
  let body: ReactNode = null;
  if (n.kind === 'wait') body = <p className="nc-what">{n.line || 'Waiting for you'}{agent && !open ? ` · answer it in ${agent.where}` : ''}</p>;
  else {
    const { tool, input: i, cwd, always } = n.req;
    if (tool === 'Bash') body = <><p className="nc-what">{typeof i.description === 'string' && i.description ? i.description : 'Wants to run a command'}</p>
      <pre className="nc-box nc-cmd"><span>{shortPath(cwd)} $</span> {String(i.command ?? '')}</pre>{choice(always)}</>;
    else if (typeof i.file_path === 'string' && ['Edit', 'Write', 'MultiEdit'].includes(tool)) {
      const d = diffLines(tool, i);
      body = <><p className="nc-what">Wants to {tool === 'Write' ? 'write' : 'edit'} a file</p>
        <div className="nc-box nc-diff"><span className="nc-file">{shortPath(i.file_path)}<em><span className="a">+{d.add}</span> <span className="d">−{d.del}</span></em></span>
          {d.shown.map(([sign, t], k) => <code key={k} className={sign === '+' ? 'add' : 'del'}>{sign === '-' ? '−' : '+'} {t}</code>)}
          {d.more > 0 && <code>… {d.more} more lines</code>}</div>{choice(always)}</>;
    } else if (tool === 'ExitPlanMode') {
      body = <><div className="nc-box nc-plan"><Markdown text={String(i.plan ?? '')}/></div>
        {card.feedback && <form className="pg-input" onSubmit={e => { e.preventDefault(); if (feedback.trim()) onResolve(`Sent · ${who} keeps planning`, { decision: 'deny', message: feedback.trim() }); }}>
          <input autoFocus placeholder="What should change?" value={feedback} onChange={e => setFeedback(e.target.value)}/>
          <button className="send" aria-label="Send" disabled={!feedback.trim()}><ArrowUp size={13} weight="bold"/></button></form>}
        <div className="nc-choice"><button type="button" className="btn btn-ghost" onClick={() => { card.feedback = !card.feedback; onChange(); }}>{card.feedback ? 'Cancel' : 'Keep planning'}</button>
          <button ref={allowButton} type="button" className="btn btn-warm" disabled={card.pending} onClick={() => onResolve(`Plan approved · ${who} starts`, { decision: 'allow' })}>Approve plan <kbd>⌘⏎</kbd></button></div></>;
    } else if (tool === 'AskUserQuestion') {
      const qs = (Array.isArray(i.questions) ? i.questions : []) as Question[], q = qs[card.qi], pick = card.picks[card.qi], multi = qs.length > 1;
      const send = () => onResolve(`Answered · ${who} continues`, { decision: 'allow', answers: Object.fromEntries(qs.map((qq, k) => [qq.question, pickText(qq, card.picks[k])])) });
      // One question: picking answers it. Several: picking moves on, and the last step shows every answer before sending.
      const step = () => { setTyped(''); if (card.qi < qs.length - 1) card.qi++; else if (multi) card.review = true; else return send(); onChange(); };
      const choose = (value: Pick) => { card.picks[card.qi] = value; onChange(); if (!q.multiSelect) setTimeout(step, multi ? 260 : 220); };
      body = card.review
        ? <div className="nc-qwrap"><p className="nc-what">Your answers</p>
          <ol className="nc-review">{qs.map((qq, k) => <li key={k}><span className="qchip">{qq.header || `Q${k + 1}`}</span>{pickText(qq, card.picks[k]) || '—'}</li>)}</ol>
          <div className="nc-choice"><button type="button" className="btn btn-ghost" onClick={() => { card.review = false; onChange(); }}>Back</button>
            <button type="button" className="btn btn-warm" onClick={send}>Send answers</button></div></div>
        : q && <div className="nc-qwrap">
          {multi && <div className="nc-qbar"><span className="qdots">{qs.map((_, k) => <i key={k} className={k === card.qi ? 'on' : k < card.qi ? 'done' : ''}/>)}</span><span>Question {card.qi + 1} of {qs.length}</span></div>}
          <p className="nc-qt">{q.header && <span className="qchip">{q.header}</span>}{q.question}</p>
          <div className="nc-opts">{(q.options ?? []).map((o, k) => {
            const on = Array.isArray(pick) ? pick.includes(k) : pick === k;
            return <button type="button" key={k} className={`opt${on ? ' is-on' : ''}`}
              onClick={() => choose(q.multiSelect ? (Array.isArray(pick) ? (on ? pick.filter(x => x !== k) : [...pick, k]) : [k]) : k)}>
              <i>{on ? <Check size={11} weight="bold"/> : k + 1}</i><span><b>{o.label}</b>{o.description && <small>{o.description}</small>}</span></button>;
          })}</div>
          <form className="pg-input" onSubmit={e => { e.preventDefault(); if (!typed.trim()) return; card.picks[card.qi] = typed.trim(); step(); }}>
            <input placeholder="Or type an answer…" value={typed} onChange={e => setTyped(e.target.value)}/>
            <button className="send" aria-label="Use this answer" disabled={!typed.trim()}><ArrowUp size={13} weight="bold"/></button></form>
          {(multi || q.multiSelect) && <div className="nc-nav">
            <button type="button" className="btn-text" disabled={!card.qi} onClick={() => { card.qi--; onChange(); }}>‹ Back</button>
            <button type="button" className="btn-text" disabled={pick === undefined || (Array.isArray(pick) && !pick.length)} onClick={step}>{multi ? 'Next ›' : 'Send ›'}</button></div>}
        </div>;
    } else body = <><p className="nc-what">Wants to use {tool.replace(/^mcp__([^_]+)__/, '$1 · ')}</p>
      <pre className="nc-box">{JSON.stringify(i, null, 1).slice(0, 600)}</pre>{choice(always)}</>;
  }
  return <div className="nc">{bar}{head}{body}{card.error && <p className="r-why" role="alert">{card.error}</p>}</div>;
}

import { useEffect, useReducer, useRef, useState, type ReactNode } from 'react';
import { ArrowSquareOut, ArrowUp, Check, X } from '@phosphor-icons/react';
import { AGENT_NAME, type AgentRequest, type ShownAgent } from './agents';
import { AgentMark, type MarkLook } from './AgentMarks';
import { Markdown } from './Markdown';
import { palette, play, scoreOf } from './soundKit';
import type { ExprId } from './starCore';
import './notices.css';

// Agent notices, after the notice lab: when a session finishes, needs you or stops, her Dashboard panel opens
// just tall enough for that one event and she docks on it. Results close themselves after 8 s; a needs-you
// card folds away after 30 s untouched and comes back once, softer, 10 minutes later. Finishes that land
// within 1.5 s share one card; needs-you cards go ahead of results; nothing shows while she talks or while
// the Dashboard is open, and it all comes up after. A held Claude Code prompt (ADR 0049) is answered on the card.
const AUTO_MS = 8000, FOLD_MS = 30_000, REMIND_MS = 600_000, TOGETHER_MS = 1500, CONFIRM_MS = 850;
type Base = { key: string; id: string; at: number; reminded?: boolean };
export type Notice = Base & (
  | { kind: 'done'; line: string; sum: string }
  | { kind: 'dones'; ids: string[]; lines: string[] }
  // Needs you, answered elsewhere: a Codex approval, or a Claude prompt Jarvis is not holding.
  | { kind: 'wait'; line: string }
  | { kind: 'req'; req: AgentRequest }
  | { kind: 'err'; text: string });
type Arrival = Notice extends infer N ? N extends Notice ? Omit<N, 'key' | 'at'> : never : never;
export const needs = (n: Notice) => n.kind === 'req' || n.kind === 'wait';
// One question's pick: an option, several options, or typed words.
type Pick = number | number[] | string;
type Card = { qi: number; picks: (Pick | undefined)[]; review: boolean; feedback: boolean; ok: string };
const firstLine = (text: string) => text.split('\n').find(line => line.trim())?.replace(/^[#>*\-\s]+/, '').trim() ?? '';

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

type Tone = 'ask' | 'done' | 'error';
const toneOf = (n: Notice): Tone => needs(n) ? 'ask' : n.kind === 'err' ? 'error' : 'done';

// The queue behind the cards. `hold` keeps every card back (she is talking, the Dashboard is open).
export function useNotices({ agents, hold, cue, answer, onSeen }: {
  agents: ShownAgent[]; hold: boolean; cue: (name: Tone | 'send' | 'close', gain?: number) => void;
  answer: (req: AgentRequest, body: { decision: 'allow' | 'always' | 'deny'; answers?: Record<string, string>; message?: string }) => Promise<boolean>;
  onSeen: (ids: string[]) => void;
}) {
  const [, bump] = useReducer((x: number) => x + 1, 0);
  const s = useRef({
    queue: [] as Notice[], folded: [] as Notice[], cards: new Map<string, Card>(), shownReqs: new Set<string>(),
    prev: null as Map<string, { state: string; req: string }> | null, soundAt: -1e9, shown: '', openedAt: 0, peek: false,
    // Her face for a moment after an answer: pleased (with a hop) or refusing.
    over: null as { face: ExprId; until: number; hop: boolean } | null,
    timers: new Set<ReturnType<typeof setTimeout>>(),
  }).current;
  const later = (ms: number, run: () => void) => { const t = setTimeout(() => { s.timers.delete(t); run(); }, ms); s.timers.add(t); };
  useEffect(() => () => s.timers.forEach(clearTimeout), []);
  const byId = new Map(agents.map(a => [a.id, a]));
  const live = useRef(byId);
  live.current = byId;
  const card = (n: Notice) => { let c = s.cards.get(n.key); if (!c) s.cards.set(n.key, c = { qi: 0, picks: [], review: false, feedback: false, ok: '' }); return c; };
  const sound = (n: Notice, gain = 1) => { const now = performance.now(); if (now - s.soundAt > TOGETHER_MS) { s.soundAt = now; cue(toneOf(n), gain); } };

  const arrive = (a: Arrival) => {
    const now = performance.now(), n = { ...a, key: `${a.kind}:${a.id}:${now}`, at: now } as Notice, tail = s.queue.at(-1);
    // Finished within 1.5 s of the last result: one card for all of them.
    if (n.kind === 'done' && tail && (tail.kind === 'done' || tail.kind === 'dones') && now - tail.at < TOGETHER_MS) {
      const merged: Notice = tail.kind === 'dones' ? { ...tail, ids: [...tail.ids, n.id], lines: [...tail.lines, n.line], at: now }
        : { key: tail.key, id: tail.id, at: now, kind: 'dones', ids: [tail.id, n.id], lines: [tail.line, n.line] };
      s.queue[s.queue.length - 1] = merged;
      return;
    }
    // Needs-you cards go ahead of results; the card on screen keeps its place.
    if (needs(n)) { const i = s.queue.findIndex((m, j) => j > 0 && !needs(m)); if (i < 0) s.queue.push(n); else s.queue.splice(i, 0, n); } else s.queue.push(n);
  };
  // What changed since the last poll. A session met for the first time only notifies through a held prompt.
  const key = agents.map(a => `${a.id}:${a.state}:${a.request?.id ?? ''}`).join('|');
  useEffect(() => {
    const prev = s.prev;
    s.prev = new Map(agents.map(a => [a.id, { state: a.state, req: a.request?.id ?? '' }]));
    for (const a of agents) {
      const was = prev?.get(a.id)?.state;
      if (a.request && !s.shownReqs.has(a.request.id)) { s.shownReqs.add(a.request.id); arrive({ kind: 'req', id: a.id, req: a.request }); }
      if (!was || was === a.state) continue;
      if (a.state === 'done' && ['work', 'pack', 'wait'].includes(was)) arrive({ kind: 'done', id: a.id, line: firstLine(a.last), sum: a.last });
      else if (a.state === 'err') arrive({ kind: 'err', id: a.id, text: a.error ?? '' });
      else if (a.state === 'wait' && !a.request) arrive({ kind: 'wait', id: a.id, line: a.last });
    }
    // A needs-you card whose session no longer waits on it was answered elsewhere.
    const stale = (n: Notice) => {
      if (!needs(n) || card(n).ok) return false;
      const a = byId.get(n.id);
      return !a || a.state !== 'wait' || (n.kind === 'req' ? a.request?.id !== n.req.id : !!a.request);
    };
    s.queue = s.queue.filter(n => !stale(n));
    s.folded = s.folded.filter(n => !stale(n));
    bump();
  }, [key]);

  const current = hold ? undefined : s.queue[0];
  // A card coming up: her sound (once per 1.5 s), and the clock for her error face.
  if ((current?.key ?? '') !== s.shown) {
    s.shown = current?.key ?? '';
    if (current) { s.openedAt = performance.now(); sound(current); }
  }
  const next = (seen: boolean) => {
    const n = s.queue.shift();
    if (n && seen) onSeen(n.kind === 'dones' ? n.ids : [n.id]);
    bump();
  };
  const fold = () => {
    const n = s.queue[0];
    if (!n || !needs(n)) return;
    s.queue.shift(); s.folded.push(n);
    if (!n.reminded) later(REMIND_MS, () => remind(n));
    bump();
  };
  // Once, ten minutes on: she peeks out of the island with a softer sound, then the card comes back.
  const remind = (n: Notice) => {
    const i = s.folded.indexOf(n);
    if (i < 0) return;
    s.folded.splice(i, 1); n.reminded = true; s.peek = true; bump();
    s.soundAt = -1e9; sound(n, .5); s.soundAt = performance.now();
    later(1100, () => { s.peek = false; s.queue.unshift(n); s.shown = n.key; s.openedAt = performance.now(); bump(); });
  };
  const [hover, setHover] = useState(false);
  const ok = current ? card(current).ok : '';
  useEffect(() => {
    if (!current || hover || ok) return;
    const t = setTimeout(() => needs(current) ? fold() : next(false), needs(current) ? FOLD_MS : AUTO_MS);
    return () => clearTimeout(t);
  }, [current?.key, hover, ok]);

  const resolve = async (n: Notice & { kind: 'req' }, text: string, body: Parameters<typeof answer>[1]) => {
    const c = card(n);
    if (c.ok) return;
    const yes = body.decision !== 'deny';
    c.ok = await answer(n.req, body) ? text : 'Already answered somewhere else';
    s.over = { face: yes ? '02' : '38', until: performance.now() + 900, hop: yes };
    cue(yes ? 'send' : 'close');
    bump();
    later(CONFIRM_MS, () => { if (s.queue[0] === n) next(true); });
  };
  // A waiting session from the Agents page or the hover list: its card comes to the front.
  const focus = (id: string) => {
    const f = s.folded.findIndex(n => n.id === id && needs(n)), q = s.queue.findIndex(n => n.id === id && needs(n));
    if (f >= 0) s.queue.unshift(...s.folded.splice(f, 1));
    else if (q > 0) s.queue.unshift(...s.queue.splice(q, 1));
    else if (q < 0) { const a = live.current.get(id); if (a?.request) s.queue.unshift({ key: `req:${id}:${performance.now()}`, id, at: performance.now(), kind: 'req', req: a.request }); }
    bump();
  };
  return { current, count: s.queue.length, peek: s.peek, openedAt: s.openedAt, over: s.over, card: current ? card(current) : null,
    setHover, next, fold, resolve, focus, bump };
}

// ---------- the card ----------
type Question = { question: string; header?: string; multiSelect?: boolean; options?: { label: string; description?: string }[] };
type Body = Parameters<Parameters<typeof useNotices>[0]['answer']>[1];
const LABEL: Record<Notice['kind'], [string, string]> = { done: ['Finished', 'done'], dones: ['', 'done'], err: ['Stopped', 'err'], wait: ['Needs you', 'wait'], req: ['Needs your OK', 'wait'] };
const shortPath = (p: string) => p.split('/').filter(Boolean).slice(-2).join('/');
// An edit as diff lines: removed, then added, at most 14.
function diffLines(tool: string, i: Record<string, unknown>) {
  const lines = (text: unknown, sign: string) => typeof text === 'string' ? text.split('\n').map(t => [sign, t] as const) : [];
  const edits = Array.isArray(i.edits) ? i.edits as Record<string, unknown>[] : [i];
  const all = tool === 'Write' ? lines(i.content, '+') : edits.flatMap(e => [...lines(e.old_string, '-'), ...lines(e.new_string, '+')]);
  return { shown: all.slice(0, 14), more: Math.max(0, all.length - 14), add: all.filter(l => l[0] === '+').length, del: all.filter(l => l[0] === '-').length };
}
const pickText = (q: Question, p: Pick | undefined) => typeof p === 'number' ? q.options?.[p]?.label ?? '' : Array.isArray(p) ? p.map(k => q.options?.[k]?.label).join(', ') : p ?? '';

export function NoticeCard({ n, agent, card, count, total, look, onClose, onLater, onOpen, onAll, onResolve, onChange }: {
  n: Notice; agent?: ShownAgent; card: Card; count: number; total: number; look: MarkLook;
  onClose: () => void; onLater: () => void; onOpen: (id: string) => void; onAll: () => void;
  onResolve: (text: string, body: Body) => void; onChange: () => void;
}) {
  const [typed, setTyped] = useState(''), [feedback, setFeedback] = useState('');
  const who = agent ? AGENT_NAME[agent.agent] : 'It';
  const wait = needs(n), [label, tone] = n.kind === 'dones' ? [`${n.ids.length} finished`, 'done'] : n.kind === 'req' && n.req.tool === 'AskUserQuestion' ? [`${who} asks`, 'wait']
    : n.kind === 'req' && n.req.tool === 'ExitPlanMode' ? ['Plan to review', 'wait'] : LABEL[n.kind];
  const bar = <div className="nc-bar">
    <span className={`nc-label is-${tone}`}><i/>{label}{count > 1 && <em> · 1 of {count}</em>}</span>
    {wait ? <button type="button" className="nc-x" title="Put it away; she reminds you once in 10 minutes" onClick={onLater}>Later</button>
      : <button type="button" className="nc-x" aria-label="Close" onClick={onClose}><X size={13}/></button>}
  </div>;
  // Only Codex threads can be opened from here; a Claude session's terminal tab cannot be found yet.
  const canOpen = agent?.agent === 'codex';
  const head = n.kind !== 'dones' && agent && <>
    <div className="nc-head">
      <AgentMark look={look} state={agent.mark === 'seen' ? 'done' : agent.mark} id={agent.id} size={12}/>
      <div className="nc-t">
        <div className="nc-top"><b>{agent.title}</b><span className="age">now</span></div>
        <div className="nc-tags"><span className={`tagc ${agent.agent}`}>{who}</span>{agent.project && <span className="tagc">{agent.project}</span>}
          {agent.where !== 'Codex' && <span className="tagc">{agent.where}</span>}</div>
      </div>
      {canOpen && <button type="button" className="nc-go" aria-label={`Open in ${agent.where}`} title={`Open in ${agent.where}`} onClick={() => onOpen(agent.id)}><ArrowSquareOut size={14}/></button>}
    </div>
    {agent.you && <p className="nc-you"><b>You</b>{agent.you}</p>}
  </>;
  if (card.ok) return <div className="nc">{bar}{head}<p className="nc-ok"><Check size={14} weight="bold"/><span>{card.ok}</span></p></div>;
  const choice = (always: string) => <div className="nc-choice">
    <button type="button" className="btn btn-ghost" onClick={() => onResolve(`Denied · ${who} will try another way`, { decision: 'deny' })}>Deny</button>
    {always && <button type="button" className="btn btn-ghost" title={always} onClick={() => onResolve(`Allowed · ${always.replace("Don't ask", "won't ask")}`, { decision: 'always' })}>Always</button>}
    <button type="button" className="btn btn-warm" onClick={() => onResolve(`Allowed · ${who} continues`, { decision: 'allow' })}>Allow</button>
  </div>;
  let body: ReactNode = null;
  if (n.kind === 'done') body = n.sum && <div className="nc-sum"><Markdown text={n.sum}/></div>;
  else if (n.kind === 'dones') body = <div className="nc-rows">{n.ids.map((id, k) => <div className="nc-row" key={id}><AgentMark look={look} state="done" id={id} size={12}/>
    <span className="nc-line">{n.lines[k] || 'Finished'}</span></div>)}</div>;
  else if (n.kind === 'err') { const [what, ...rest] = n.text.split(': '); body = <p className="nc-err"><b>{what || 'Stopped'}</b>{rest.join(': ')}</p>; }
  else if (n.kind === 'wait') body = <p className="nc-what">{n.line || 'Waiting for you'}{agent && !canOpen ? ` · answer it in ${agent.where}` : ''}</p>;
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
          <button type="button" className="btn btn-warm" onClick={() => onResolve(`Plan approved · ${who} starts`, { decision: 'allow' })}>Approve plan</button></div></>;
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
  return <div className="nc">{bar}{head}{body}<button type="button" className="nc-all" onClick={onAll}>All {total} sessions</button></div>;
}

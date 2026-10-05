import { createContext, useContext, useEffect, useLayoutEffect, useReducer, useRef, useState, type ReactNode } from 'react';
import { ArrowSquareOut, ArrowUp, Check, Moon, X } from '@phosphor-icons/react';
import { jobKind, jobStamp } from './JobsPage';
import { postRoute } from './homeData';
import { AGENT_NAME, loadMarks, openLabel, requestLine, saveMark, type Agent, type AgentRequest, type AgentState } from './agents';
import { AgentMark, type MarkLook, type MarkState } from './AgentMarks';
import { tr, type Lang } from './companionSettings';
import { Markdown } from './Markdown';
import { palette, play, scoreOf } from './soundKit';
import type { ExprId } from './starCore';
import type { Quiet } from './model';
import './notices.css';

// Agent notices, after the notch lab (ADR 0057, 0069). A session that finishes or stops while Allen is not looking at
// it joins his turn and pops up its name from the island for 5 s; one that needs him gets a card hanging from the
// island, answered right there; it folds away after 30 s untouched and comes back once, softer, 10 minutes later.
// Park (先放着) takes a session off his turn: no pops, no cards, no reminder, until he takes it back or it does
// something new. Finishes within 1.5 s share one pop; needs-you cards go ahead of pops; nothing shows while she
// talks, while the Dashboard is open or while the keys hold the island, and it all comes up after. From the quiet level
// `no-pop` up (ADR 0153) nothing is queued: arrivals wait in `held` (marks and stars go on as they were) and come up
// together, needs-you first, when the level drops. Nothing pops for
// the session he is looking at, in Ghostty or on its page in the island.
const POP_MS = 5000, DIGEST_MS = 30_000, FOLD_MS = 30_000, REMIND_MS = 600_000, TOGETHER_MS = 1500, CONFIRM_MS = 850;
type Base = { key: string; id: string; at: number; reminded?: boolean };
export type Notice = Base & (
  | { kind: 'pop'; ids: string[] }
  // Needs you, answered elsewhere: a Codex approval, or a Claude prompt Jarvis is not holding.
  | { kind: 'wait'; line: string }
  | { kind: 'req'; req: AgentRequest }
  // What waited while the level was no-pop or dnd (ADR 0153): one session at a time, the most important first.
  | { kind: 'digest'; items: Notice[] }
  // Job mail (ADR 0155), from GET /inherent/notices: one mail, and what waited while the level was no-pop or dnd.
  | { kind: 'mail'; job: JobNotice }
  | { kind: 'jobs'; job: JobNotice });
// The daemon's notice as it arrives; it has already applied the quiet level. `level`: card (silent), card_sound or speak (both with her cue).
export type JobItem = { id?: string; title?: string; line?: string; company?: string; role?: string; at?: string; event_at?: string | null; event_text?: string | null; mail_kind?: string; count?: number };
export type JobNotice = JobItem & { id: string; kind: 'mail' | 'digest'; title: string; line?: string; level?: string; link?: 'jobs'; items?: JobItem[] };
const isJob = (n: Notice): n is Notice & { kind: 'mail' | 'jobs' } => n.kind === 'mail' || n.kind === 'jobs';
// What the daemon is told about a notice: POST /inherent/notices/{id} { action: 'seen' } or { action: 'feedback', reaction }.
const tell = (port: string | null, id: string, body: { action: 'seen' } | { action: 'feedback'; reaction: string }) => { if (port) void postRoute(port, `/inherent/notices/${encodeURIComponent(id)}`, body).catch(() => undefined); };
type Arrival = Notice extends infer N ? N extends Notice ? Omit<N, 'key' | 'at'> : never : never;
export const needs = (n: Notice) => n.kind === 'req' || n.kind === 'wait';
// ADR 0153: from `no-pop` up the island shows no card and no name pop.
const noCards = (quiet: Quiet) => quiet === 'no-pop' || quiet === 'dnd';
export const ended = (state: AgentState) => state === 'done' || state === 'err';
// One question's pick: an option, several options, or typed words.
type Pick = number | number[] | string;
type Card = { qi: number; picks: (Pick | undefined)[]; review: boolean; feedback: boolean; ok: string; pending?: boolean; error?: string; resolved?: boolean };
// The card reports only a confirmed response. Its host owns the visual flight.
export const NoticeFlightContext = createContext<((id: string, point: { x: number; y: number }) => void) | null>(null);

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
// Ghostty for 1.5 s, `viewing` the one whose page is open in the island. Startrail's sessions (`a.host`) are looked at
// while its window is in front (`agentsFront`), and their marks are the host's: `mark` changes them there.
export function useNotices({ port, poll, agents, hold, quiet, inClaude, watched, viewing, agentsFront, audio, cue, answer, mark }: {
  port: string | null; poll: boolean; agents: Agent[]; hold: boolean; quiet: Quiet; inClaude: boolean; watched: string | null; viewing: string | null; agentsFront: boolean;
  // The daemon's last word on whether sound may play (`audio_private` of GET /inherent/notices); undefined while it has said nothing.
  audio: { current: boolean | undefined }; cue: (name: Tone | 'send' | 'close', gain?: number) => void;
  answer: (req: AgentRequest, body: { decision: 'allow' | 'always' | 'deny'; answers?: Record<string, string>; message?: string }, id: string) => Promise<boolean>;
  mark: (id: string, change: { seen: true } | { parked: boolean; archived: boolean }) => void;
}) {
  const [, bump] = useReducer((x: number) => x + 1, 0);
  const [s] = useState(() => {
    const kept = loadKept();
    return {
      queue: [] as Notice[], folded: [] as Notice[], held: [] as Notice[], cards: new Map<string, Card>(), shownReqs: new Set<string>(), dismissed: new Set<string>(),
      unread: new Set(kept.unread), archived: new Set(kept.cleared), parked: new Map<string, number>(), last: kept.last,
      soundAt: -1e9, shown: '', openedAt: 0, peek: false, touched: '', forced: '',
      // Job mail: the notice ids taken in (one card each, never again) and those already told `seen`.
      jobIds: new Set<string>(), seen: new Set<string>(),
      // Sessions changed here before the daemon's marks arrived: their marks stay as she set them.
      loaded: false, early: new Set<string>(),
      // The marks each of Startrail's sessions had when the lists last took them from the host.
      synced: new Map<string, string>(),
      // Her face for a moment after an answer: pleased (with a hop) or refusing.
      over: null as { face: ExprId; until: number; hop: boolean } | null,
      timers: new Set<ReturnType<typeof setTimeout>>(),
    };
  });
  const save = () => { try { localStorage.setItem(TURN, JSON.stringify({ unread: [...s.unread], cleared: [...s.archived], last: s.last })); } catch { /* the list just is not remembered */ } };
  const persist = (id: string) => {
    if (live.current.byId.get(id)?.host) { mark(id, { parked: s.parked.has(id), archived: s.archived.has(id) }); return; }
    if (!s.loaded) s.early.add(id); if (port) saveMark(port, id, { unread: s.unread.has(id), park: s.parked.has(id), archive: s.archived.has(id) });
  };
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
      s.synced.clear(); save(); bump();
    });
  }, [port]);
  const later = (ms: number, run: () => void) => { const t = setTimeout(() => { s.timers.delete(t); run(); }, ms); s.timers.add(t); };
  useEffect(() => () => s.timers.forEach(clearTimeout), []);
  const byId = new Map(agents.map(a => [a.id, a]));
  const live = useRef({ byId, hold, quiet, inClaude });
  live.current = { byId, hold, quiet, inClaude };
  const card = (n: Notice) => { let c = s.cards.get(n.key); if (!c) s.cards.set(n.key, c = { qi: 0, picks: [], review: false, feedback: false, ok: '' }); return c; };
  const toneOf = (n: Notice): Tone => needs(n) ? 'ask' : isJob(n) ? n.job.level === 'speak' ? 'ask' : 'done' : n.kind === 'pop' && n.ids.every(id => byId.get(id)?.state === 'err') ? 'error' : 'done';
  // Job mail at level card is silent.
  const sound = (n: Notice, gain = 1) => { if (isJob(n) && n.job.level !== 'card_sound' && n.job.level !== 'speak') return; const now = performance.now(); if (now - s.soundAt > TOGETHER_MS) { s.soundAt = now; cue(toneOf(n), gain); } };

  // The level rising takes what is on the island into `held`; it dropping brings it all back, needs-you first, then errors, then done.
  useEffect(() => {
    if (noCards(quiet)) {
      // A digest on the island goes back to the items it listed. Job mail is the daemon's to bring back (its own digest): one not yet shown is forgotten here, so it comes again.
      for (const n of s.queue) if (isJob(n) && !s.seen.has(n.id)) s.jobIds.delete(n.id);
      s.held = [...s.queue.filter(n => !isJob(n)).flatMap(n => n.kind === 'digest' ? n.items : [n]), ...s.folded, ...s.held]; s.queue = []; s.folded = []; s.forced = ''; bump();
    } else if (s.held.length) {
      const rank = (n: Notice) => needs(n) ? 0 : toneOf(n) === 'error' ? 1 : 2, at = performance.now();
      // One entry per session, the most important of what it did; two or more of them make one digest, one is just shown.
      const items = [...new Map(s.held.filter(n => !s.parked.has(n.id)).sort((a, b) => rank(a) - rank(b) || a.at - b.at).reverse().map(n => [n.id, n])).values()].sort((a, b) => rank(a) - rank(b) || a.at - b.at);
      s.held = [];
      if (items.length === 1) s.queue.push(items[0]);
      else if (items.length) {
        // The asks stay on the island's list, so a row in the digest can bring its card up.
        s.folded.push(...items.filter(needs));
        s.queue.push({ kind: 'digest', key: `digest:${at}`, id: '', at, items });
      }
      bump();
    }
  }, [noCards(quiet)]);

  // One ask is one card: the session and what it says (ADR 0153).
  const askKey = (n: { kind: string; id: string; req?: AgentRequest; line?: string }) => `${n.id}|${n.req ? requestLine(n.req) : n.line}`;
  const arrive = (a: Arrival) => {
    // Job mail waits in the queue like any card (`hold`, in Claude); at no-pop and dnd it is not taken in, the daemon keeps it.
    if (a.kind === 'mail' || a.kind === 'jobs') {
      if (noCards(live.current.quiet) || s.jobIds.has(a.id)) return;
      s.jobIds.add(a.id); const now = performance.now();
      s.queue.push({ ...a, key: `${a.kind}:${a.id}:${now}`, at: now } as Notice);
      return;
    }
    // A project thread's session is not his to answer here; while he is in Claude a pop or ask line has no use, but
    // a prompt Jarvis holds waits behind `hold` until he leaves (a background session has no dialog of its own).
    if (s.parked.has(a.id) || live.current.byId.get(a.id)?.fromProject || live.current.inClaude && a.kind !== 'req') return;
    if (a.kind === 'req' || a.kind === 'wait') {
      const k = askKey(a);
      if (s.dismissed.has(k) || [...s.queue, ...s.folded, ...s.held].some(m => needs(m) && askKey(m) === k)) return;
    }
    const now = performance.now(), n = { ...a, key: `${a.kind}:${a.id}:${now}`, at: now } as Notice, head = s.queue[0], tail = s.queue.at(-1);
    // Kept for when the level drops: a newer notice of a session takes the place of its older one.
    if (noCards(live.current.quiet)) { s.held = [...s.held.filter(h => !(h.id === n.id && h.kind === n.kind)), n]; return; }
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
  // GET /inherent/notices every 5 s while this window is up: the daemon's job mail and digest become cards. Its other notices are the
  // Dashboard's; a 404 means job mail is off, and the poll stops without a word.
  useEffect(() => {
    if (!port || !poll) return;
    let stop = false, timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const r = await fetch(`http://127.0.0.1:${port}/inherent/notices`, { signal: AbortSignal.timeout(4000) });
        if (r.status === 404) { audio.current = undefined; return; }
        if (!r.ok) audio.current = false;
        if (r.ok && !stop) {
          const { notices, audio_private } = await r.json() as { notices?: JobNotice[]; audio_private?: boolean };
          audio.current = typeof audio_private === 'boolean' ? audio_private : undefined;
          for (const n of Array.isArray(notices) ? notices : []) if ((n.kind === 'mail' || n.kind === 'digest') && typeof n.id === 'string' && typeof n.title === 'string') arrive({ kind: n.kind === 'mail' ? 'mail' : 'jobs', id: n.id, job: n });
          bump();
        }
      } catch { audio.current = false; /* daemon away: nothing new may sound; the next tick retries */ }
      if (!stop) timer = setTimeout(load, 5000);
    };
    void load();
    return () => { stop = true; clearTimeout(timer); };
  }, [port, poll]);
  // Their names leave any pop; `cards` takes their needs-you cards away too.
  const drop = (ids: string[], cards = false) => {
    for (const n of s.queue) if (n.kind === 'pop') n.ids = n.ids.filter(id => !ids.includes(id));
    const keep = (n: Notice) => n.kind === 'pop' ? n.ids.length > 0 : !(cards && ids.includes(n.id));
    s.queue = s.queue.filter(keep); s.folded = s.folded.filter(keep);
  };
  // Out of his turn: looked at, opened or marked.
  const read = (ids: string[]) => {
    ids.forEach(id => { if (!s.unread.delete(id)) return; if (live.current.byId.get(id)?.host) mark(id, { seen: true }); else { if (!s.loaded) s.early.add(id); if (port) saveMark(port, id, { seen: true }); } });
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
    ids.forEach(id => { const a = live.current.byId.get(id); s.parked.delete(id); if (!a?.host && ended(a?.state ?? 'work')) s.unread.add(id); persist(id); });
    save(); bump();
  };
  // Startrail's sessions keep their marks in its host: the lists take what it says each time it says something new.
  for (const a of agents) {
    if (!a.host) continue;
    const k = `${+a.host.unread}${+a.host.parked}${+a.host.archived}`;
    if (s.synced.get(a.id) === k) continue;
    s.synced.set(a.id, k);
    if (a.host.unread) s.unread.add(a.id); else if (s.unread.delete(a.id)) drop([a.id]);
    if (!a.host.parked) s.parked.delete(a.id); else if (!s.parked.has(a.id)) { s.parked.set(a.id, Date.now()); drop([a.id], true); }
    if (a.host.archived) s.archived.add(a.id); else s.archived.delete(a.id);
  }
  // What changed since the last poll. A session met for the first time only notifies through a held prompt.
  const key = agents.map(a => `${a.id}:${a.state}:${a.request?.id ?? ''}`).join('|');
  useEffect(() => {
    const now = Date.now(), touched = new Set<string>();
    for (const a of agents) {
      const was = s.last[a.id]?.[0], looking = a.id === watched || a.id === viewing || !!a.host && agentsFront;
      s.last[a.id] = [a.state, now];
      if (a.state !== 'wait') for (const k of s.dismissed) if (k.startsWith(`${a.id}|`)) s.dismissed.delete(k);
      // A background session shows no dialog of its own while Jarvis holds its prompt, so that card comes even
      // while he looks at the session; an interactive one asks in his terminal at the same time. A new question
      // is something new: it brings a parked session back (Startrail's stay parked, as in her queue).
      if (a.request && !s.shownReqs.has(a.request.id)) {
        s.shownReqs.add(a.request.id);
        if (!a.host && s.parked.delete(a.id)) touched.add(a.id);
        if (!looking || a.kind === 'background') arrive({ kind: 'req', id: a.id, req: a.request });
      }
      if (!was || was === a.state) continue;
      // Startrail's host keeps their marks: a change is only told here, a finish when the host counts it unread.
      if (a.host) {
        if (looking) continue;
        // A finish that turned out to ask (ADR 0125) takes the place of the pop it came with.
        if (a.state === 'wait' && !a.request) { if (ended(was)) drop([a.id]); arrive({ kind: 'wait', id: a.id, line: a.last }); }
        else if (ended(a.state) && !ended(was) && a.host.unread) arrive({ kind: 'pop', id: a.id, ids: [a.id] });
        continue;
      }
      // Anything new brings it back from the moon or the archive.
      const unarchived = s.archived.delete(a.id), unparked = s.parked.delete(a.id);
      if (unarchived || unparked) touched.add(a.id);
      if (!ended(a.state)) {
        // Working again (or asking): it leaves his turn.
        if (s.unread.delete(a.id)) touched.add(a.id);
        // A finish that turned out to ask (ADR 0125) takes the place of the pop it came with.
        if (a.state === 'wait' && !a.request && !looking) { if (ended(was)) drop([a.id]); arrive({ kind: 'wait', id: a.id, line: a.last }); }
      } else if (!ended(was) && !looking) { s.unread.add(a.id); touched.add(a.id); arrive({ kind: 'pop', id: a.id, ids: [a.id] }); }
    }
    touched.forEach(persist);
    for (const [id, [, at]] of Object.entries(s.last)) if (!byId.has(id) && now - at > KEEP_MS) delete s.last[id];
    for (const set of [s.unread, s.archived, s.parked]) for (const id of set.keys()) if (!s.last[id]) set.delete(id);
    // A needs-you card whose session no longer waits on it was answered elsewhere.
    const stale = (n: Notice) => {
      if (!needs(n) || card(n).ok || card(n).pending) return false;
      const a = byId.get(n.id);
      return !a || a.state !== 'wait' || (n.kind === 'req' ? a.request?.id !== n.req.id : !!a.request);
    };
    s.queue = s.queue.filter(n => !stale(n));
    s.folded = s.folded.filter(n => !stale(n));
    // Looking at a session on his turn reads it.
    if (watched && s.unread.has(watched)) read([watched]); else { save(); bump(); }
  }, [key, watched, viewing]);

  const current = (hold || inClaude) && s.queue[0]?.key !== s.forced ? undefined : s.queue[0];
  // A notice coming up: her sound (once per 1.5 s), and the clock for her error face.
  if ((current?.key ?? '') !== s.shown) {
    s.shown = current?.key ?? '';
    if (current) {
      s.openedAt = performance.now(); sound(current);
      if (isJob(current) && !s.seen.has(current.id)) { s.seen.add(current.id); tell(port, current.id, { action: 'seen' }); }
    }
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
    if (!live.current.inClaude) { s.soundAt = -1e9; sound(n, .5); s.soundAt = performance.now(); }
    later(1100, () => { s.peek = false; if (!s.parked.has(n.id)) { s.queue.unshift(n); s.shown = n.key; s.openedAt = performance.now(); } bump(); });
  };
  // Swipe, Esc, the × or a click away: gone for good, no moon and no reminder. It stays on his list while it waits.
  const dismiss = () => {
    const n = s.queue[0];
    if (!n) return;
    if (isJob(n)) { if (!card(n).ok) tell(port, n.id, { action: 'feedback', reaction: 'dismissed' }); return next(); }
    if (!needs(n)) return next();
    s.queue.shift(); s.dismissed.add(askKey(n)); s.forced = ''; bump();
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
    const t = setTimeout(() => needs(current) ? fold() : next(), needs(current) ? FOLD_MS : current.kind === 'digest' || isJob(current) ? DIGEST_MS : s.touched === current.key ? 1500 : POP_MS);
    return () => clearTimeout(t);
  }, [current?.key, hover, ok, size]);

  // The 合适吗 row on a mail card or the summary: the answer goes to the daemon (for a summary, it applies to every mail in it), the card says thanks and goes.
  const rate = (n: Notice & { kind: 'mail' | 'jobs' }, reaction: string, text: string) => {
    const c = card(n);
    if (c.ok) return;
    tell(port, n.id, { action: 'feedback', reaction }); c.ok = text; bump();
    later(CONFIRM_MS, () => { if (s.queue[0] === n) next(); });
  };
  const resolve = async (n: Notice & { kind: 'req' }, text: string, body: Parameters<typeof answer>[1]) => {
    const c = card(n);
    if (c.ok || c.pending) return false;
    c.pending = true; c.error = ''; bump();
    const yes = body.decision !== 'deny';
    try {
      c.resolved = await answer(n.req, body, n.id);
      c.ok = c.resolved ? text : 'Already answered somewhere else';
    }
    catch { c.error = 'Could not send your answer. Try again.'; return false; }
    finally { c.pending = false; bump(); }
    if (c.resolved) { s.over = { face: yes ? '02' : '38', until: performance.now() + 900, hop: yes }; cue(yes ? 'send' : 'close'); }
    bump();
    later(CONFIRM_MS, () => { if (s.queue[0] === n) next(); });
    return c.resolved === true;
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
    read, archive, park, unpark, setHover, hovering: hover, next, fold, dismiss, back, resolve, rate, focus, bump };
}

// ---------- what waited ----------
// ADR 0153: leaving no-pop or dnd. One line says how many sessions did something while Allen was away, then one row each:
// asks first, then stops, then what finished. A row opens its session; an ask's brings its card up.
export function DigestCard({ n, agents, lang, look, onOpen, onAnswer }: {
  n: Notice & { kind: 'digest' }; agents: Agent[]; lang: Lang; look: MarkLook; onOpen: (agent: Agent) => void; onAnswer: (id: string) => void;
}) {
  const byId = new Map(agents.map(a => [a.id, a]));
  const line = (m: Notice): [MarkState, string] => needs(m) ? ['wait', tr(lang, ['Needs you', '要你回答'])]
    : m.kind === 'pop' && m.ids.every(id => byId.get(id)?.state === 'err') ? ['err', tr(lang, ['Stopped on an error', '出错停了'])] : ['done', tr(lang, ['Finished', '做完了'])];
  return <div className="nc nc-digest">
    <div className="nc-bar"><span className="nc-label"><i/>{tr(lang, [`${n.items.length} things while you were away`, `你不在时有 ${n.items.length} 件事`])}</span></div>
    <ul className="nc-away">{n.items.map(m => {
      const a = byId.get(m.id) ?? (m.kind === 'pop' ? byId.get(m.ids[0]) : undefined), [state, what] = line(m);
      return <li key={m.key}><button type="button" className="nc-away-row" onClick={() => { if (needs(m)) onAnswer(m.id); else if (a) onOpen(a); }}>
        <AgentMark look={look} state={state} id={m.id} size={12}/><b>{a?.title ?? tr(lang, ['A session', '一个会话'])}</b><span>{what}</span></button></li>;
    })}</ul>
  </div>;
}

// ---------- job mail ----------
// Esc puts the visible card away; fields and menus keep their own Escape.
function useEscape(root: { current: HTMLElement | null }, on: boolean, run: () => void, key: string) {
  useEffect(() => {
    if (!on) return;
    const down = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.isComposing || e.repeat) return;
      const pane = root.current?.closest('.notch-pane');
      if (!root.current?.checkVisibility({ opacityProperty: true, visibilityProperty: true }) || pane && !pane.classList.contains('is-open')) return;
      if (e.target instanceof HTMLElement && e.target.closest('input,textarea,[contenteditable],[role="menu"],[role="listbox"],[role="combobox"]')) return;
      e.preventDefault(); e.stopImmediatePropagation();
      run();
    };
    document.addEventListener('keydown', down, true);
    return () => document.removeEventListener('keydown', down, true);
  }, [key, on]);
}
// The five levels the 合适吗 row offers, in the daemon's words; the first two keep it to the ledger or a glow only.
const LEVELS: [string, string | null][] = [['记下', null], ['亮一下', null], ['卡片', 'card'], ['卡片带声', 'card_sound'], ['开口', 'speak']];
const when = (job: JobItem) => [job.company, job.role, jobStamp(job.at)].filter(Boolean) as string[];

// The 合适吗 row of a mail card and of the summary: folded to one word, opens to 对 and the five levels (the card's own marked), and folds after 10 s untouched.
function RateRow({ card, level, lang, onRate, onChange }: { card: Card; level: string; lang: Lang; onRate: (reaction: string, text: string) => void; onChange: () => void }) {
  const touched = useRef(performance.now());
  // `card.feedback` is whether the row is open.
  useEffect(() => {
    if (!card.feedback || card.ok) return;
    touched.current = performance.now();
    const t = setInterval(() => { if (performance.now() - touched.current > 10_000) { card.feedback = false; onChange(); } }, 1000);
    return () => clearInterval(t);
  }, [card.feedback, card.ok]);
  return card.ok ? <p className="nc-ok"><Check size={14} weight="bold"/><span>{card.ok}</span></p>
    : !card.feedback ? <button type="button" className="nc-rate-open" aria-expanded="false" onClick={() => { card.feedback = true; onChange(); }}>{tr(lang, ['Right level?', '合适吗'])}</button>
    : <div className="nc-rate" onPointerMove={() => { touched.current = performance.now(); }} onFocus={() => { touched.current = performance.now(); }}>
      <span className="nc-rate-q">{tr(lang, ['Right level?', '合适吗'])}</span>
      <button type="button" className="btn btn-warm nc-right" onClick={() => onRate('right', tr(lang, ['Noted · that level fits', '记下了 · 这个级别合适']))}>对</button>
      <div className="nc-levels" role="group" aria-label={tr(lang, ['Or pick the level it should have', '或者选它该有的级别'])}>{LEVELS.map(([name, id]) =>
        <button key={name} type="button" className={`nc-lv${id === level ? ' is-now' : ''}`} aria-pressed={id === level} onClick={() => onRate(`level:${name}`, tr(lang, [`Noted · ${name}`, `记下了 · ${name}`]))}>{name}</button>)}</div>
    </div>;
}

// One job mail: its title and line, who and when as chips, and a small row asking whether this was the right level (folded away).
export function MailNotice({ n, card, lang, onDismiss, onRate, onChange }: {
  n: Notice & { kind: 'mail' }; card: Card; lang: Lang; onDismiss: () => void; onRate: (reaction: string, text: string) => void; onChange: () => void;
}) {
  const root = useRef<HTMLDivElement>(null), { job } = n;
  const level = job.level === 'speak' || job.level === 'card_sound' ? job.level : 'card', [kindClass, kindName] = jobKind(job.mail_kind);
  useEscape(root, !card.ok, onDismiss, n.key);
  const chips = when(job), event = job.event_at ? jobStamp(job.event_at, true) : '';
  return <div ref={root} className="nc nc-mail">
    <div className="nc-bar"><span className={`nc-label ${job.mail_kind ? kindClass : 'is-other'}`}><i/>{job.mail_kind ? tr(lang, kindName) : tr(lang, ['Job mail', '求职邮件'])}</span>
      <button type="button" className="nc-x nc-dismiss" aria-label="Dismiss" title={tr(lang, ['Dismiss', '关掉'])} onClick={onDismiss}><X size={14}/></button></div>
    <p className="nc-mail-t">{job.title}</p>
    {job.line && <p className="nc-what">{job.line}</p>}
    {(chips.length > 0 || event) && <div className="nc-tags">{chips.map((c, i) => <span key={i} className="tagc">{c}</span>)}{event && <span className="tagc">{tr(lang, ['Event', '日程'])} {event}</span>}</div>}
    <RateRow card={card} level={level} lang={lang} onRate={onRate} onChange={onChange}/>
  </div>;
}

// A summary row after the company: its role, and how many mails the thread holds; one row alone leaves the count to the title.
const rowTail = (m: JobItem, rows: number, lang: Lang) => {
  const n = m.count ?? 1, parts = [m.role, rows > 1 && n > 1 ? tr(lang, [`${n} emails`, `${n} 封往来`]) : ''].filter(Boolean);
  return parts.length ? `· ${parts.join(' · ')}` : '';
};
// The time cell of a summary row: the interview (or other event) time as the key fact, from the daemon's `event_at` (24 h, this
// machine's zone) or else its short `event_text`; with neither, when the latest mail came.
const rowWhen = (m: JobItem, lang: Lang) => {
  const fact = jobStamp(m.event_at, true) || m.event_text;
  return fact ? <time className="is-event">{tr(lang, m.mail_kind === 'interview' ? ['Interview', '面试'] : ['Event', '日程'])} {fact}</time> : <time>{jobStamp(m.at)}</time>;
};
// The daemon's one summary of what waited or came in a burst (ADR 0158, 0159). Rows only tell, and the 合适吗 row rates the whole card; the button opens the Dashboard's job ledger (no Gmail link).
export function JobsDigestCard({ n, card, lang, onDismiss, onOpen, onRate, onChange }: { n: Notice & { kind: 'jobs' }; card: Card; lang: Lang; onDismiss: () => void; onOpen: () => void; onRate: (reaction: string, text: string) => void; onChange: () => void }) {
  const root = useRef<HTMLDivElement>(null), items = Array.isArray(n.job.items) ? n.job.items : [];
  useEscape(root, !card.ok, onDismiss, n.key);
  return <div ref={root} className="nc nc-digest nc-jobs">
    <div className="nc-bar"><span className="nc-label is-other"><i/>{n.job.title}</span>
      <button type="button" className="nc-x nc-dismiss" aria-label="Dismiss" title={tr(lang, ['Dismiss', '关掉'])} onClick={onDismiss}><X size={14}/></button></div>
    <ul className="nc-away nc-jobrows">{items.map((m, i) => { const [cls, name] = jobKind(m.mail_kind);
      return <li key={m.id ?? i} className="nc-jobrow"><em className={`jk ${cls}`}>{tr(lang, name)}</em>
        <span className="nc-jr-who"><b>{m.company || m.title}</b> <span className="nc-jr-role">{rowTail(m, items.length, lang)}</span></span>{rowWhen(m, lang)}</li>; })}</ul>
    {n.job.link === 'jobs' && <button type="button" className="btn btn-warm nc-open-jobs" onClick={onOpen}>{tr(lang, ['Open the job list', '打开求职记录'])}</button>}
    <RateRow card={card} level={n.job.level === 'card_sound' ? 'card_sound' : 'card'} lang={lang} onRate={onRate} onChange={onChange}/>
  </div>;
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
export function NoticeCard({ n, agent, card, count, look, onPark, onDismiss, onOpen, onResolve, onChange }: {
  n: Notice & { kind: 'req' | 'wait' }; agent?: Agent; card: Card; count: number; look: MarkLook;
  onPark: () => void; onDismiss: () => void; onOpen: (agent: Agent) => void; onResolve: (text: string, body: Body) => void; onChange: () => void;
}) {
  const [typed, setTyped] = useState(''), [feedback, setFeedback] = useState(''), [alwaysAllowed, setAlwaysAllowed] = useState(false);
  const allowButton = useRef<HTMLButtonElement>(null), denyButton = useRef<HTMLButtonElement>(null), root = useRef<HTMLDivElement>(null), flew = useRef(false);
  const fly = useContext(NoticeFlightContext);
  useLayoutEffect(() => {
    if (!card.resolved || flew.current || !fly || n.kind !== 'req' || n.req.tool === 'AskUserQuestion') return;
    const mark = root.current?.querySelector('.agent-mark')?.getBoundingClientRect();
    if (!mark) return;
    flew.current = true;
    fly(n.id, { x: mark.x + mark.width / 2, y: mark.y + mark.height / 2 });
  }, [card.resolved, fly, n.id, n.kind]);
  // Cards arrive without a keyboard gesture. A bare Return must never approve
  // one, including when its primary button happens to have focus.
  useEffect(() => {
    if (n.kind !== 'req' || n.req.tool === 'AskUserQuestion' || card.ok) return;
    const key = (e: KeyboardEvent) => {
      if (e.key !== 'Enter' || e.isComposing) return;
      const button = allowButton.current, pane = button?.closest('.notch-pane');
      if (!button?.checkVisibility({ opacityProperty: true, visibilityProperty: true }) || pane && !pane.classList.contains('is-open')) return;
      const target = e.target instanceof HTMLElement ? e.target : null;
      if (target?.closest('button,input,textarea,[contenteditable],[role="menu"],[role="listbox"],[role="combobox"]') && !button.closest('.nc')?.contains(target)) return;
      if (e.target instanceof HTMLInputElement && n.req.tool === 'ExitPlanMode' && !e.metaKey) return;
      e.preventDefault(); e.stopImmediatePropagation();
      if (e.metaKey && !e.repeat && !card.pending) allowButton.current?.click();
    };
    document.addEventListener('keydown', key, true);
    return () => document.removeEventListener('keydown', key, true);
  }, [n.key, card.ok, card.pending]);
  // Esc puts a visible card away without answering it (never Deny); fields and menus keep their own Escape.
  useEffect(() => {
    if (card.ok) return;
    const key = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.isComposing || e.repeat || card.pending) return;
      const pane = root.current?.closest('.notch-pane');
      if (!root.current?.checkVisibility({ opacityProperty: true, visibilityProperty: true }) || pane && !pane.classList.contains('is-open')) return;
      if (e.target instanceof HTMLElement && e.target.closest('input,textarea,[contenteditable],[role="menu"],[role="listbox"],[role="combobox"]')) return;
      e.preventDefault(); e.stopImmediatePropagation();
      onDismiss();
    };
    document.addEventListener('keydown', key, true);
    return () => document.removeEventListener('keydown', key, true);
  }, [n.key, card.ok, card.pending]);
  // A question's options answer to their digits, as in the Agents window; words typed into its field stay words.
  useEffect(() => {
    if (n.kind !== 'req' || n.req.tool !== 'AskUserQuestion' || card.ok || card.review) return;
    const key = (e: KeyboardEvent) => {
      if (!/^[1-9]$/.test(e.key) || e.metaKey || e.ctrlKey || e.altKey || e.isComposing || card.pending) return;
      const pane = root.current?.closest('.notch-pane'), opt = root.current?.querySelectorAll<HTMLButtonElement>('.nc-opts .opt')[Number(e.key) - 1];
      if (!opt?.checkVisibility({ opacityProperty: true, visibilityProperty: true }) || pane && !pane.classList.contains('is-open')) return;
      if (e.target instanceof HTMLElement && e.target.closest('input,textarea,[contenteditable],[role="menu"],[role="listbox"],[role="combobox"]')) return;
      e.preventDefault(); e.stopImmediatePropagation();
      if (!e.repeat) opt.click();
    };
    document.addEventListener('keydown', key, true);
    return () => document.removeEventListener('keydown', key, true);
  }, [n.key, card.ok, card.review, card.pending]);
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
      <div className="nc-actions"><button type="button" className="nc-icon nc-dismiss" aria-label="Dismiss" title="Dismiss: it stays on your list" onClick={onDismiss}><X size={14}/></button>
        <button type="button" className="nc-icon nc-park" aria-label="Park" title="Park: out of your turn until you take it back" onClick={onPark}><Moon size={14} weight="fill"/></button>
        {open && <button type="button" className="nc-icon" aria-label={open} title={open} onClick={() => onOpen(agent)}><ArrowSquareOut size={14}/></button>}</div>
    </div>
    {agent.you && <p className="nc-you"><b>You</b>{agent.you}</p>}
  </>;
  if (card.ok) return <div ref={root} className="nc">{bar}{head}<p className="nc-ok"><Check size={14} weight="bold"/><span>{card.ok}</span></p></div>;
  const choice = (always: string) => {
    const request = n.kind === 'req' ? n.req : null;
    const project = agent?.project || request?.cwd.split('/').filter(Boolean).at(-1) || request?.cwd;
    const bashRule = request?.tool === 'Bash' ? always.match(/^Don't ask again for Bash\((.*)\)$/)?.[1] ?? String(request.input.command ?? '') : '';
    return <>{always && <label className="nc-always" title={always}>
      <input type="checkbox" checked={alwaysAllowed} disabled={card.pending} onChange={e => setAlwaysAllowed(e.target.checked)}/>
      <span>{bashRule ? <>Always allow <code>{bashRule}</code></> : always.replace("Don't ask again for", 'Always allow')} in <b>{project}</b></span>
    </label>}<div className="nc-choice">
      <button ref={denyButton} type="button" className="btn btn-ghost" data-deny disabled={card.pending} onClick={() => onResolve(`Denied · ${who} will try another way`, { decision: 'deny' })}>Deny</button>
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
  return <div ref={root} className="nc">{bar}{head}{body}{card.error && <p className="r-why" role="alert">{card.error}</p>}</div>;
}

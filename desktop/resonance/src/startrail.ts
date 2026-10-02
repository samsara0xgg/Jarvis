import { useEffect, useMemo, useReducer, useState } from 'react';
import type { Event, Item, Req, Sess } from '../electron/agents/types';
import { asksYou, queue, waitOf, waitingSince } from './agents/queue';
import { ago, type Agent, type AgentRequest, type Answer, type Said } from './agents';

// Startrail's sessions in Jarvis's notch (ADR 0073, 0095, 0104). The page follows the agent host's own event stream and
// answers through its routes, as the Agents window does: main adds the host's key to every request on the host's port
// (bridge.ts), so the page never holds it. Main names the port only where it runs the host (the dev build; not the
// design checks, not the packaged app). While the owner leaves `notify.notch` on (the host's settings), the notch says
// what Startrail's own banners would: its sessions join the rows beside the notch, in her queue's order (queue.ts), a
// request drops as a card answered right there, a finish pops its name; turned off, the notch leaves them alone.
export const HOST = new URLSearchParams(location.search).get('agents');
const API = `http://127.0.0.1:${HOST}`;

// What the notch needs of a conversation, by item index: its requests and what you said.
type Line = { req: Req; done: boolean } | { you: string } | null;
const lineOf = (it: Item): Line => it.k === 'req' ? { req: it.req, done: !!it.done } : it.k === 'you' ? { you: it.text } : null;
const pendingOf = (l: Line[]) => l.find((x): x is { req: Req; done: boolean } => !!x && 'req' in x && !x.done)?.req;
const youOf = (l: Line[]) => [...l].reverse().find((x): x is { you: string } => !!x && 'you' in x)?.you ?? '';
// What changes a row: the rest of a session (what it is doing now, its context, its tasks) moves nothing here.
const keyOf = (s: Sess) => [s.st, s.asks, s.title, s.project, s.branch, s.summary, s.now, s.unread, s.parked, s.archived, s.term, s.updated].join('|');

// A request as Jarvis's card reads one: Claude Code's own shapes, as the daemon hands them over. Claude's "always"
// keeps its own rule for this command or tool; for an edit it means accepting edits for the session, and Codex's holds
// for the session too, so the card offers it only where it means always. A form is answered in Startrail.
function requestOf(s: Sess, r: Req): AgentRequest | undefined {
  const always = (what: string) => s.agent === 'claude' && r.tool !== 'Ask' && r.tool !== 'Plan' && r.tool !== 'Form' && r.always ? `Don't ask again for ${what}` : '';
  if (r.tool === 'Bash') return { id: r.id, tool: 'Bash', input: { command: r.cmd, description: r.why }, cwd: r.cwd, always: always(`Bash(${r.cmd})`) };
  if (r.tool === 'Edit') {
    const del = r.diff.filter(d => d[0] === '-').map(d => d[1]), add = r.diff.filter(d => d[0] === '+').map(d => d[1]);
    return { id: r.id, tool: 'Edit', input: { file_path: r.file, ...del.length ? { old_string: del.join('\n') } : {}, ...add.length ? { new_string: add.join('\n') } : {} }, cwd: s.cwd, always: '' };
  }
  if (r.tool === 'Tool') {
    let input: Record<string, unknown>;
    try { const v = JSON.parse(r.detail); input = v && typeof v === 'object' && !Array.isArray(v) ? v : { detail: r.detail }; } catch { input = { detail: r.detail }; }
    return { id: r.id, tool: r.name, input, cwd: s.cwd, always: always(r.name) };
  }
  if (r.tool === 'Ask') return { id: r.id, tool: 'AskUserQuestion', cwd: s.cwd, always: '',
    input: { questions: r.qs.map(q => ({ question: q.q, header: q.head, multiSelect: q.multi, options: q.opts.map(([label, description]) => ({ label, description })) })) } };
  if (r.tool === 'Plan') return { id: r.id, tool: 'ExitPlanMode', input: { plan: r.plan }, cwd: s.cwd, always: '' };
  return undefined;
}
function rowOf(s: Sess, l: Line[] | undefined): Agent {
  const req = s.st === 'wait' && l ? pendingOf(l) : undefined;
  return { id: s.id, agent: s.agent, title: s.title, project: s.project, branch: s.branch || undefined, state: asksYou(s) ? 'wait' : s.st, where: 'Startrail',
    age: ago(s.updated), you: l ? youOf(l) : '', last: (s.st === 'work' || s.st === 'pack') && s.now || s.summary, at: waitingSince(s),
    request: req && requestOf(s, req), error: s.st === 'err' ? s.summary : undefined, host: { unread: s.unread, parked: s.parked, archived: s.archived } };
}

// The rows, waiting ones first in her queue's order; `ids` are every session the host holds, so the daemon's own row of
// one (a session taken in from a terminal) gives way. One held in a terminal is the terminal's (ADR 0096): its row
// stays the daemon's.
export function useStartrail() {
  const [version, bump] = useReducer((x: number) => x + 1, 0);
  const [st] = useState(() => ({ ss: new Map<string, Sess>(), lines: new Map<string, Line[]>(), loading: new Set<string>(), on: true,
    // The row last shown per session: one that has just asked keeps it until its request is read.
    shown: new Map<string, Agent>() }));
  useEffect(() => {
    if (!HOST) return;
    // A waiting session's conversation, read once; the event stream keeps it from then on.
    const load = async (id: string) => {
      if (st.loading.has(id) || st.lines.has(id)) return;
      st.loading.add(id);
      let items: Item[] = [];
      try { const r = await fetch(`${API}/sessions/${encodeURIComponent(id)}`, { signal: AbortSignal.timeout(8000) }); if (r.ok) items = (await r.json()).items ?? []; }
      catch { /* its row shows it waiting all the same */ }
      st.loading.delete(id);
      if (!st.ss.has(id)) return;
      st.lines.set(id, items.map(lineOf)); bump();
    };
    const es = new EventSource(`${API}/events`);
    es.onmessage = m => {
      const e = JSON.parse(m.data) as Event;
      if (e.t === 'hello') {
        st.ss = new Map(e.sessions.map(s => [s.id, s])); st.lines.clear(); st.loading.clear(); st.on = e.settings?.notify?.notch !== false;
        e.sessions.filter(s => s.st === 'wait').forEach(s => void load(s.id));
      } else if (e.t === 'settings') {
        if (st.on === (e.settings.notify?.notch !== false)) return;
        st.on = !st.on;
      } else if (e.t === 'sess') {
        const was = st.ss.get(e.s.id);
        st.ss.set(e.s.id, e.s);
        if (e.s.st === 'wait') void load(e.s.id);
        if (was && keyOf(was) === keyOf(e.s)) return;
      } else if (e.t === 'gone') { st.ss.delete(e.id); st.lines.delete(e.id); st.shown.delete(e.id); }
      else if (e.t === 'items') {
        const l = st.lines.get(e.id);
        if (!l) return;
        const before = `${pendingOf(l)?.id}|${youOf(l)}`;
        if (e.from > l.length) { st.lines.delete(e.id); if (st.ss.get(e.id)?.st === 'wait') void load(e.id); }
        else { l.length = e.from; l.push(...e.items.map(lineOf)); if (`${pendingOf(l)?.id}|${youOf(l)}` === before) return; }
      } else return;
      bump();
    };
    return () => es.close();
  }, []);
  return useMemo(() => {
    if (!st.on) return { rows: [] as Agent[], ids: new Set<string>() };
    const ss = [...st.ss.values()].filter(s => !s.term), shown = ss.filter(s => !s.archived);
    const rows = [...queue(shown), ...shown.filter(s => !waitOf(s)).sort((a, b) => b.updated - a.updated)].map(s => {
      const last = st.shown.get(s.id), row = s.st === 'wait' && !st.lines.has(s.id) && last ? last : rowOf(s, st.lines.get(s.id));
      st.shown.set(s.id, row);
      return row;
    });
    return { rows, ids: new Set(ss.map(s => s.id)) };
  }, [version]);
}

// Allen's answer from the card, in the host's words: each question's answer by its place. True once it went in; false
// when the request was answered somewhere else or the session is gone; it throws when the host could not be reached.
export async function answerStartrail(id: string, req: AgentRequest, body: Answer) {
  const qs = req.tool === 'AskUserQuestion' && Array.isArray(req.input.questions) ? req.input.questions as { question: string }[] : [];
  const r = await fetch(`${API}/sessions/${encodeURIComponent(id)}/answer`, { method: 'POST', headers: { 'content-type': 'application/json' }, signal: AbortSignal.timeout(8000),
    body: JSON.stringify({ req: req.id, decision: body.decision, ...body.answers ? { answers: qs.map(q => [body.answers![q.question] ?? '']) } : {}, ...body.message ? { text: body.message } : {} }) });
  if (r.ok) return true;
  if (r.status === 404 || r.status === 409) return false;
  throw new Error(`the host answered ${r.status}`);
}
// A mark set in the notch goes to the host, which keeps Startrail's marks and hands them on to the daemon (ADR 0069).
export function markStartrail(id: string, change: { seen: true } | { parked: boolean; archived: boolean }) {
  void fetch(`${API}/sessions/${encodeURIComponent(id)}/meta`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(change), signal: AbortSignal.timeout(5000) }).catch(() => {});
}
// A session's conversation for the island's page: what you said and each answer.
export async function readStartrail(id: string): Promise<Said[] | null> {
  try {
    const r = await fetch(`${API}/sessions/${encodeURIComponent(id)}`, { signal: AbortSignal.timeout(8000) });
    const items = r.ok ? (await r.json()).items as Item[] | undefined : undefined;
    return Array.isArray(items) ? items.flatMap(it => it.k === 'you' || it.k === 'it' ? [{ who: it.k, text: it.text }] : []) : null;
  } catch { return null; }
}

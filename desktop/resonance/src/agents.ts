import { useEffect, useState } from 'react';
import type { CodexSession } from './CodexModule';
import type { MarkState } from './AgentMarks';

// Agents: Claude Code and Codex sessions together. With a daemon port, Codex rows come from its Codex hook and
// Claude rows from /inherent/claude-sessions (ADR 0046); a Claude permission prompt Jarvis holds rides on its
// row as `request`, answered through /inherent/claude-requests (ADR 0049). Without a port every row is a demo.
export type AgentState = 'wait' | 'work' | 'pack' | 'done' | 'err';
// What a Claude session asks permission for: the tool and its input as Claude Code sent them, where it runs,
// and the words for "don't ask again" (empty when Claude Code offers none).
export type AgentRequest = { id: string; tool: string; input: Record<string, unknown>; cwd: string; always: string };
export type Agent = {
  id: string; agent: 'claude' | 'codex'; state: AgentState; title: string; project: string; branch?: string; where: string; age: string;
  you: string; last: string; sub?: boolean; request?: AgentRequest; error?: string; at?: number; // last change, for Settings › Agents' stale limit
  // A Claude session's kind, and for a background one the id `claude attach` takes (ADR 0057).
  kind?: 'interactive' | 'background'; job?: string;
};
// A row as the companion sees it: with the mark it wears and its one line for the hover list.
export type ShownAgent = Agent & { mark: MarkState; line: string };
export const AGENT_NAME = { claude: 'Claude', codex: 'Codex' };
// Where a session can be opened from the notch (ADR 0057): a Codex thread, or a Claude session's Ghostty terminal
// (a background one attaches in a new tab when no terminal shows it). Other terminals cannot be found.
export const openLabel = (a: Agent) => a.agent === 'codex' ? 'Open in Codex' : a.kind === 'background' || a.where === 'Ghostty' ? 'Open in Ghostty' : '';
export const DEMO_AGENTS: Agent[] = [
  { id: 'usage', state: 'wait', agent: 'codex', project: 'jarvis', title: 'Adjust the usage page', where: 'Codex', age: '2m', you: 'make the usage rings match', last: 'Wants to run npm run build' },
  { id: 'inner', state: 'work', agent: 'claude', project: 'jarvis', branch: 'companion-ball', title: 'Dashboard inner pages', where: 'Ghostty', age: '4m', you: 'add the plugins page and fix the bottom bar', last: 'Editing the design page…' },
  { id: 'review', state: 'work', agent: 'claude', sub: true, project: 'jarvis', branch: 'companion-ball', title: 'Check the panel pages', where: 'Ghostty', age: '1m', you: 'check every page against the design', last: 'Comparing the Usage page…' },
  { id: 'voice', state: 'work', agent: 'codex', project: 'jarvis', title: 'Fix voice reconnect', where: 'Codex', age: '9m', you: 'the voice drops after the Mac sleeps', last: 'Reading the reconnect logic…' },
  { id: 'aec', state: 'done', agent: 'claude', project: 'jarvis', title: 'Mac echo cancel', where: 'zellij', age: '1h', you: 'why does it keep saying “mm”?', last: 'Found it: a search result was read aloud.' },
  { id: 'loop', state: 'done', agent: 'codex', project: 'jarvis', title: 'Evaluate the minimal loop', where: 'Codex', age: '2h', you: 'what’s the smallest loop that works?', last: 'Summarized the loop and what’s left.' },
  { id: 'cap', state: 'done', agent: 'claude', project: 'typlus', title: 'Long dictation cap', where: 'Ghostty', age: '3h', you: 'long notes get cut off', last: 'Raised the cap and installed the build.' },
];
const ago = (ms: number) => { const m = Math.round((Date.now() - ms) / 60_000); return m < 1 ? 'now' : m < 60 ? `${m}m` : m < 1440 ? `${Math.floor(m / 60)}h` : `${Math.floor(m / 1440)}d`; };
export const fromCodex = (r: CodexSession): Agent => ({
  id: r.session_id, agent: 'codex', state: r.state === 'needs_input' ? 'wait' : r.state === 'running' ? 'work' : 'done',
  title: r.title || r.prompt || 'Codex session', project: r.cwd.split('/').filter(Boolean).pop() ?? '', where: 'Codex',
  age: ago(r.since_ms), you: r.prompt, last: r.state === 'finished' ? r.last_message : r.detail, at: r.since_ms,
});
const base = (path: string) => path.split('/').filter(Boolean).pop() ?? path;
// One line for what a request wants, for the Agents rows and the hover list.
export function requestLine(r: AgentRequest) {
  const i = r.input;
  if (r.tool === 'Bash') return `Wants to run ${String(i.command ?? '').split('\n')[0]}`;
  if (typeof i.file_path === 'string') return `Wants to ${r.tool === 'Write' ? 'write' : 'edit'} ${base(i.file_path)}`;
  if (r.tool === 'AskUserQuestion') return `Asks: ${(i.questions as { question?: string }[] | undefined)?.[0]?.question ?? 'a question'}`;
  if (r.tool === 'ExitPlanMode') return 'Plan ready for review';
  return `Wants to use ${r.tool.replace(/^mcp__([^_]+)__/, '$1 ')}`;
}
type ClaudeSession = {
  session_id: string; kind: 'interactive' | 'background'; job_id?: string; phase: 'needs_input' | 'working' | 'done'; title: string; project: string; branch: string; where: string; prompt: string; activity: string;
  last_message: string; updated_ms: number; compacting?: boolean; error?: string; request?: AgentRequest | null;
};
export const fromClaude = (r: ClaudeSession): Agent => ({
  id: r.session_id, agent: 'claude', title: r.title || r.prompt || 'Claude session', project: r.project, branch: r.branch || undefined,
  state: r.request || r.phase === 'needs_input' ? 'wait' : r.error ? 'err' : r.phase === 'working' ? r.compacting ? 'pack' : 'work' : 'done',
  where: r.where === 'background' ? 'Background' : r.where, age: ago(r.updated_ms), you: r.prompt,
  last: r.request ? requestLine(r.request) : r.compacting ? 'Compacting its context' : r.phase === 'done' ? r.last_message : r.activity || r.last_message,
  request: r.request ?? undefined, error: r.error || undefined, at: r.updated_ms, kind: r.kind, job: r.job_id || undefined,
});
// Polled all the time: the marks beside the notch and the notices read them too. A daemon that does not serve
// the route yet simply has no Claude rows.
export function useClaudeSessions(port: string | null) {
  const [rows, setRows] = useState<ClaudeSession[]>([]);
  useEffect(() => {
    if (!port) return;
    let stop = false, timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const r = await fetch(`http://127.0.0.1:${port}/inherent/claude-sessions`, { signal: AbortSignal.timeout(5000) });
        const data = r.ok ? await r.json() : null;
        if (!stop && Array.isArray(data?.sessions)) setRows(data.sessions.filter((x: ClaudeSession) => typeof x?.session_id === 'string' && typeof x.phase === 'string'));
      } catch { /* daemon away; the next tick retries */ }
      if (!stop) timer = setTimeout(load, 1500);
    };
    void load();
    return () => { stop = true; clearTimeout(timer); };
  }, [port]);
  return rows;
}
// Allen's answer to a held request. `answers` maps each question to the chosen label(s); `message` goes back
// to Claude with a deny (why not, or what to change in a plan). False when the request is gone: answered in the
// terminal, or the session stopped waiting.
export type Answer = { decision: 'allow' | 'always' | 'deny'; answers?: Record<string, string>; message?: string };
export async function answerRequest(port: string, id: string, answer: Answer) {
  try {
    const r = await fetch(`http://127.0.0.1:${port}/inherent/claude-requests/${encodeURIComponent(id)}`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(answer), signal: AbortSignal.timeout(5000) });
    return r.ok;
  } catch { return false; }
}

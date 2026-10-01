// Claude Code through the Claude Agent SDK (ADR 0073): one `claude` child per session that has something to do, fed by a
// message queue that never ends, so the session lives between turns. Permission prompts, questions and plan approval
// all come through canUseTool and wait for Allen's answer in the window.
import { execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { existsSync, realpathSync } from 'node:fs';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { deleteSession, forkSession, getSessionInfo, getSessionMessages, listSessions, query, renameSession, type ElicitationResult, type McpServerStatus, type Options,
  type PermissionResult, type PermissionUpdate, type Query, type SDKControlGetContextUsageResponse, type SDKMessage, type SDKUserMessage, type SpawnedProcess,
  type SpawnOptions } from '@anthropic-ai/claude-agent-sdk';
import { EventEmitter } from 'node:events';
import net from 'node:net';
import { PassThrough } from 'node:stream';
import { attach, attached } from './files.js';
import { fieldsOf } from './form.js';
import { catalogChanged, Http, KEEPER, kt, log, pic, rode, sent, type Driver, type Session } from './host.js';
import { ask, lines, parse, type Head } from './keeper.js';
import { auth, keyEnv } from './settings.js';
import type { Choice, Ctx, CtxRow, Diff, Field, File, Mcp, Pic, Req, Step, Task } from './types.js';
import { plural, tr } from './lang.js';

// In the dev build Allen's subscription, in the packaged app the owner's own key or cloud account (ADR 0094), and never
// a key or token this process happens to have; nothing that says this runs inside another Claude Code session. The
// marker keeps Jarvis's own PermissionRequest hook (ADR 0049) out of sessions this window answers itself. Read each
// time, so what the login shell added to PATH once the host started counts. `session`: false for a claude that is not
// a session (updating the install), which never gets the key.
export const claudeEnv = (session = true): Record<string, string | undefined> => ({ ...Object.fromEntries(Object.entries(process.env)
  .filter(([k]) => !/^(ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|CLAUDECODE|CLAUDE_CODE_|CLAUDE_JOB)/.test(k))), JARVIS_AGENTS_HOST: '1', ...session ? keyEnv() : {} });
// The Claude Code install on this Mac, which moves to each new release (and its new models) without a Jarvis release;
// the SDK's own pinned copy only where there is none. JARVIS_AGENTS_CLAUDE names another binary (a check's stand-in).
const OWN = `${homedir()}/.local/bin/claude`;
export const EXE = process.env.JARVIS_AGENTS_CLAUDE || (existsSync(OWN) ? OWN : undefined);
// What actually runs: EXE, or the build the SDK finds beside its own package.
export function claudeExe() {
  if (EXE) return EXE;
  try { return createRequire(import.meta.url).resolve(`@anthropic-ai/claude-agent-sdk-${process.platform}-${process.arch}/claude`); } catch { return ''; }
}
const PROMPT = { type: 'preset', preset: 'claude_code' } as const;
const MODES = (): [string, string][] => [['auto', tr('自动', 'Auto')], ['default', tr('改之前问我', 'Ask before edits')], ['acceptEdits', tr('自动接受修改', 'Auto-accept edits')], ['plan', tr('计划模式', 'Plan mode')], ['bypassPermissions', tr('完全放开', 'Full access')]];
const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];

// A queue the session reads from; it ends only when the session is let go.
function pushable<T>() {
  const buf: T[] = [];
  let wake: (() => void) | null = null, done = false;
  return {
    push(v: T) { buf.push(v); wake?.(); wake = null; },
    end() { done = true; wake?.(); wake = null; },
    async *[Symbol.asyncIterator]() {
      for (;;) {
        while (buf.length) yield buf.shift()!;
        if (done) return;
        await new Promise<void>(r => { wake = r; });
      }
    },
  };
}
type Pending = { resolve: (r: PermissionResult) => void; name: string; input: Record<string, unknown>; suggestions?: PermissionUpdate[] }
  // An MCP server's form, or a page it wants opened (C3)
  | { form: (r: ElicitationResult) => void; fields: Field[]; url?: string };
// Claude Code's /btw as the SDK sends it (C7): the SDK has the call but does not declare it.
type Sideways = { askSideQuestion(question: string, o: { history: { question: string; response: string }[]; signal: AbortSignal }): Promise<{ response: string } | null> };
// Claude Code's own sign-in to an MCP server (its /mcp), as the SDK sends it: the SDK has the call but does not declare it.
type SignIn = { mcpAuthenticate(serverName: string): Promise<{ authUrl?: string; requiresUserAction?: boolean } | undefined> };
// prev: the mode to go back to when a plan is approved · usage: tokens the last answer was sent with · block: the text
// block streaming in · creates: TaskCreate calls waiting for the id their result gives
type Rt = {
  q?: Query; input?: ReturnType<typeof pushable<SDKUserMessage>>; fresh?: boolean;
  pending: Map<string, Pending>; queued: Map<string, { text: string; files: Pic[] }>;
  tasks: Map<string, [string, 0 | 1 | 2]>; creates: Map<string, Record<string, unknown>>;
  interrupted?: boolean; usage?: number; prev?: string; block: string;
  // After a host restart: what the transcript already gave, whether the turn is still going, whether a result the
  // keeper replays is news (the turn ended while no host watched) or one already shown
  seen?: Set<string>; open?: boolean; news?: boolean;
  // titled: turns that ended here, so a title Claude Code generated after one of the first few is taken (B14) · gone:
  // until the child last let go has ended
  titled?: number; gone?: Promise<void>;
  // The thought being streamed: its step's key and when it began.
  think?: string; thinkAt?: number;
};
const rt = (s: Session): Rt => (s.rt.claude ??= { pending: new Map(), queued: new Map(), tasks: new Map(), creates: new Map(), block: '' }) as Rt;
// A reset (/clear, a plan run with a clean context) starts a transcript of its own: the session reads them all back and
// goes on in the last one.
const ids = (s: Session) => [s.s.id, ...s.s.resets ?? []];
const cur = (s: Session) => ids(s).at(-1)!;
const CLEARED = () => tr('上下文清空了 · 上面的它已经不记得了', 'Context cleared · it no longer remembers anything above');

// ---------- what a tool call looks like as a step ----------
const str = (v: unknown) => typeof v === 'string' ? v : '';
const rel = (s: Session, p: string) => p && p.startsWith(`${s.s.cwd}/`) ? p.slice(s.s.cwd.length + 1) : p.replace(homedir(), '~');
// The lines that changed, with two lines around them, from the text before and after.
export function diffOf(before: string, after: string): Diff {
  const a = before.split('\n'), b = after.split('\n');
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  let j = 0;
  while (j < a.length - i && j < b.length - i && a[a.length - 1 - j] === b[b.length - 1 - j]) j++;
  const out: Diff = [];
  for (const l of a.slice(Math.max(0, i - 2), i)) out.push([' ', l]);
  for (const l of a.slice(i, a.length - j)) out.push(['-', l]);
  for (const l of b.slice(i, b.length - j)) out.push(['+', l]);
  for (const l of a.slice(a.length - j, Math.min(a.length, a.length - j + 2))) out.push([' ', l]);
  return out.slice(0, 400);
}
const counts = (d: Diff) => ({ add: d.filter(x => x[0] === '+').length, del: d.filter(x => x[0] === '-').length });
function stepOf(s: Session, name: string, input: Record<string, unknown>): Step | null {
  const file = rel(s, str(input.file_path) || str(input.notebook_path));
  if (name === 'Bash') return { k: 'bash', t: str(input.command).split('\n')[0] };
  if (name === 'Read') return { k: 'read', t: file };
  if (name === 'Edit') { const diff = diffOf(str(input.old_string), str(input.new_string)); return { k: 'edit', t: file, diff, ...counts(diff) }; }
  if (name === 'Write') { const lines = str(input.content).split('\n'); return { k: 'edit', t: file, add: lines.length, del: 0, diff: lines.slice(0, 200).map(l => ['+', l]) }; }
  if (name === 'NotebookEdit') return { k: 'edit', t: file };
  if (name === 'Grep') return { k: 'search', t: `"${str(input.pattern)}"${input.path ? ` in ${rel(s, str(input.path))}` : ''}` };
  if (name === 'Glob') return { k: 'search', t: str(input.pattern) };
  if (name === 'Agent' || name === 'Task') return { k: 'agent', t: [str(input.subagent_type), str(input.description)].filter(Boolean).join(' · ') };
  if (name === 'WebFetch') return { k: 'web', t: str(input.url) };
  if (name === 'WebSearch') return { k: 'web', t: str(input.query) };
  if (name === 'Skill') return { k: 'tool', t: `Skill · ${str(input.skill) || str(input.command)}` };
  if (/^(TodoWrite|TaskCreate|TaskUpdate|TaskList|TaskGet|AskUserQuestion|ExitPlanMode|EnterPlanMode)$/.test(name)) return null;
  const mcp = /^mcp__(.+?)__(.+)$/.exec(name);
  return { k: 'tool', t: mcp ? `${mcp[1]} · ${mcp[2]}` : name };
}
// The plan: TodoWrite's list, or the Task tools' list on models that have those instead.
function planOf(s: Session, name: string, input: Record<string, unknown>, result?: unknown) {
  const r = rt(s), st = (x: unknown): 0 | 1 | 2 => x === 'completed' ? 2 : x === 'in_progress' ? 1 : 0;
  if (name === 'TodoWrite' && Array.isArray(input.todos)) {
    s.plan(input.todos.map((t: Record<string, unknown>) => [str(t.content), st(t.status)]));
    return;
  }
  if (name === 'TaskCreate') {
    const id = str((result as { task?: { id?: unknown } } | undefined)?.task?.id) || String(r.tasks.size + 1);
    r.tasks.set(id, [str(input.subject), 0]);
  } else if (name === 'TaskUpdate') {
    const id = str(input.taskId), t = r.tasks.get(id);
    if (!t) return;
    if (input.status === 'deleted') r.tasks.delete(id);
    else r.tasks.set(id, [str(input.subject) || t[0], input.status ? st(input.status) : t[1]]);
  } else return;
  s.plan([...r.tasks.values()]);
}
function reqOf(s: Session, id: string, name: string, input: Record<string, unknown>, o: { title?: string; description?: string; displayName?: string; suggestions?: PermissionUpdate[] }): Req {
  const always = o.suggestions?.length ? tr('以后都允许', 'Always allow') : '';
  if (name === 'AskUserQuestion' && Array.isArray(input.questions)) return { id, tool: 'Ask', qs: input.questions.map((q: Record<string, unknown>) => ({
    q: str(q.question), head: str(q.header), multi: q.multiSelect === true,
    opts: Array.isArray(q.options) ? q.options.map((x: Record<string, unknown>) => [str(x.label), str(x.description)] as [string, string]) : [] })) };
  if (name === 'ExitPlanMode') return { id, tool: 'Plan', plan: str(input.plan) };
  if (name === 'Bash') return { id, tool: 'Bash', why: str(input.description) || o.title || '', cmd: str(input.command), cwd: s.s.cwd, always };
  const st = stepOf(s, name, input);
  if (st?.k === 'edit') return { id, tool: 'Edit', why: o.title ?? '', file: st.t, diff: st.diff ?? [], always };
  return { id, tool: 'Tool', why: o.title || o.description || '', name: o.displayName || st?.t || name, detail: JSON.stringify(input, null, 1).slice(0, 800), always };
}
// A request a Claude session in a terminal stopped on, as the daemon holds it (ADR 0049): the same card as one of ours.
export const heldReq = (id: string, tool: string, input: Record<string, unknown>, cwd: string, always: boolean) =>
  reqOf({ s: { cwd } } as Session, id, tool, input, always ? { suggestions: [{} as PermissionUpdate] } : {});
const textOf = (c: unknown): string => typeof c === 'string' ? c : Array.isArray(c) ? c.map(b => b?.type === 'text' ? str(b.text) : '').filter(Boolean).join('\n') : '';
// Pictures a tool gave back (a screenshot, an image it read).
const picsOf = (c: unknown): Pic[] => Array.isArray(c) ? c.filter(b => b?.type === 'image' && b.source?.type === 'base64')
  .map((b, k) => pic(tr(`图片 ${k + 1}`, `Image ${k + 1}`), `data:${b.source.media_type};base64,${b.source.data}`)) : [];
const firstLine = (t: string) => oneLine(t.split('\n').find(l => l.trim()) ?? '', 80);

// ---------- one message from the session, live or from its transcript ----------
type Block = { type: string; id?: string; name?: string; input?: Record<string, unknown>; text?: string; thinking?: string; title?: string; tool_use_id?: string; content?: unknown; is_error?: boolean;
  source?: { type: string; media_type?: string; data?: string } };
// Live, Allen's own words are already on screen when he sends them; from a transcript they are read back here. Each of
// his messages and each answer keeps its uuid, the point fork and rewind name. `prev`: when the transcript's entry
// before this one was written.
function said(s: Session, m: SDKMessage | { type: string; uuid?: string; message?: unknown; tool_use_result?: unknown; parent_tool_use_id?: string | null }, at?: number, live = false, prev?: number) {
  const msg = (m as { message?: { content?: unknown } }).message, parent = (m as { parent_tool_use_id?: string | null }).parent_tool_use_id, uuid = str((m as { uuid?: unknown }).uuid);
  if (m.type === 'assistant') {
    // A sub-agent's own thinking, text and calls go under the step that started it (B16).
    if (parent) {
      for (const b of (msg?.content ?? []) as Block[]) {
        if (b.type === 'text' && b.text?.trim()) s.sub(parent, undefined, { k: 'say', t: b.text }, at);
        else if (b.type === 'thinking' && b.thinking?.trim()) s.sub(parent, undefined, { k: 'think', t: firstLine(b.thinking), out: b.thinking }, at);
        else if (b.type === 'tool_use' && b.name && b.id) { const st = stepOf(s, b.name, b.input ?? {}); if (st) s.sub(parent, b.id, st, at); }
      }
      return;
    }
    const u = (msg as { usage?: Record<string, number> } | undefined)?.usage;
    if (u) rt(s).usage = (u.input_tokens ?? 0) + (u.cache_read_input_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0);
    if (uuid) s.ref(uuid);
    for (const [i, b] of ((msg?.content ?? []) as Block[]).entries()) {
      if (b.type === 'text' && b.text) s.say(b.text, at);
      // Its thinking, as the summary Claude Code is asked for (B18), and how long it thought: live, the step it has been
      // since the thought began; from a transcript, the time since the entry before.
      else if (b.type === 'thinking') {
        const r = rt(s), text = b.thinking?.trim() ? b.thinking : '';
        if (live && r.think) { s.toolDone(r.think, { ...text ? { t: firstLine(text), out: text } : {}, ms: Date.now() - (r.thinkAt ?? Date.now()) }); r.think = undefined; }
        else if (text) s.tool(`${uuid || randomUUID()}:think:${i}`, { k: 'think', t: firstLine(text), out: text, ...!live && at && prev && at > prev ? { ms: at - prev } : {} }, at);
      }
      else if (b.type === 'tool_use' && b.name && b.id) {
        const input = b.input ?? {}, st = stepOf(s, b.name, input);
        if (st) s.tool(b.id, st, at);
        else if (b.name === 'TodoWrite' || b.name === 'TaskUpdate') planOf(s, b.name, input);
        else if (b.name === 'TaskCreate') rt(s).creates.set(b.id, input);
      }
    }
    return;
  }
  if (m.type === 'user') {
    const content = msg?.content;
    if (Array.isArray(content) && content.some((b: Block) => b.type === 'tool_result')) {
      for (const b of content as Block[]) {
        if (b.type !== 'tool_result' || !b.tool_use_id) continue;
        const created = rt(s).creates.get(b.tool_use_id);
        if (created) { rt(s).creates.delete(b.tool_use_id); planOf(s, 'TaskCreate', created, (m as { tool_use_result?: unknown }).tool_use_result); continue; }
        const r = (m as { tool_use_result?: Record<string, unknown> }).tool_use_result, patch: Partial<Step> = { ok: !b.is_error }, pics = picsOf(b.content);
        const out = r && typeof r === 'object' && ('stdout' in r || 'stderr' in r) ? [str(r.stdout), str(r.stderr)].filter(Boolean).join('\n') : textOf(b.content);
        if (b.is_error || out) patch.out = out.slice(0, 6000);
        if (pics.length) patch.pics = pics;
        if (r && Array.isArray(r.structuredPatch)) {
          const diff: Diff = (r.structuredPatch as { lines?: string[] }[]).flatMap(h => (h.lines ?? []).map(l => [l[0] === '+' ? '+' : l[0] === '-' ? '-' : ' ', l.slice(1)] as Diff[number]));
          if (diff.length) Object.assign(patch, { diff: diff.slice(0, 400), ...counts(diff) });
        }
        s.toolDone(b.tool_use_id, patch);
        // A shell sent to the background says where its output goes, so its task can be read while it runs.
        const file = r && typeof r === 'object' && str(r.backgroundTaskId) ? /Output is being written to: (\S+\.output)/.exec(textOf(b.content))?.[1] : undefined;
        if (file) s.task(str(r!.backgroundTaskId), { out: file });
      }
      return;
    }
    // A sub-agent's instructions are in its step already.
    if (parent || live) return;
    // Allen's own words; Claude Code's bookkeeping in angle brackets is not. Files that are not pictures were named on
    // lines of their own at the end.
    const raw = textOf(content), cmd = /<command-name>([^<]*)<\/command-name>[\s\S]*?(?:<command-args>([^<]*)<\/command-args>)?/.exec(raw), { text, paths } = attached(raw);
    const blocks = Array.isArray(content) ? content as Block[] : [];
    const files = [...blocks.filter(b => b.type === 'image').map((b, k) => pic(tr(`图片 ${k + 1}`, `Image ${k + 1}`), b.source?.type === 'base64' ? `data:${b.source.media_type};base64,${b.source.data}` : '')),
      ...blocks.filter(b => b.type === 'document').map((b, k) => ({ name: str(b.title) || `PDF ${k + 1}` })), ...paths.map(p => ({ name: p.replace(/\/$/, '').split('/').pop() || p, path: p }))];
    if (cmd) s.you(`${cmd[1]} ${cmd[2] ?? ''}`.trim(), [], at, uuid);
    else if (/^\s*<(local-command|system-reminder|command-)/.test(raw)) return;
    else if (/^\[Request interrupted/.test(raw)) s.note(tr('你打断了这一轮', 'You interrupted this turn'));
    else if (text.trim() || files.length) s.you(text.trim(), files, at, uuid);
  }
}

// ---------- the live session ----------
// The child runs in the keeper (ADR 0098), not under this process: the SDK talks to it through one keeper connection,
// and opening the session again after a host restart carries on with the same child. What the keeper replays and the
// transcript already showed is dropped here. A child being let go (released to change its folders, stopped) is gone
// before the session opens again: the keeper would otherwise hand the new query the child it is about to end.
function viaKeeper(s: Session) {
  return (o: SpawnOptions): SpawnedProcess => {
    const r = rt(s), stdin = new PassThrough(), stdout = new PassThrough(), sock = net.connect(KEEPER), was = r.gone;
    const p = Object.assign(new EventEmitter(), { stdin, stdout, killed: false, exitCode: null as number | null,
      kill() {
        if (!p.killed) {
          p.killed = true;
          r.gone = new Promise<void>(done => { sock.once('close', () => done()); setTimeout(done, 5000).unref(); });
          void ask(KEEPER, { op: 'kill', key: s.s.id }).catch(e => log('keeper kill', e));
        }
        return true;
      } });
    // What the SDK writes follows the line that opens the session.
    const start = () => { sock.write(`${JSON.stringify({ op: 'open', key: s.s.id, spawn: { command: o.command, args: o.args, cwd: o.cwd, env: o.env } })}\n`); stdin.on('data', d => sock.write(d)); };
    if (was) void was.then(start); else start();
    let head: Head | null = null, replay = 0;
    lines(sock, l => {
      if (!head) { head = parse(l) as Head; replay = head.replay; return; }
      if (replay > 0) {
        replay--;
        const m = parse(l);
        // The keeper keeps a child's last result until its next one, so only the last line replayed can be the end of
        // a turn this host missed; one before it ended a turn the last host saw.
        if ((m.uuid && r.seen?.has(m.uuid)) || (m.type === 'result' && (!r.news || replay > 0))) return;
      }
      stdout.write(`${l}\n`);
    });
    // The SDK ends its input only when it lets the session go: the child goes with it.
    stdin.on('end', () => p.kill());
    sock.on('close', () => { stdout.end(); p.exitCode = 0; p.emit('exit', 0, null); });
    sock.on('error', e => p.emit('error', e));
    return p as unknown as SpawnedProcess;
  };
}
function ensure(s: Session) {
  const r = rt(s);
  if (r.q) return r;
  const a = auth();
  if (!a.ready) throw new Error(a.why);
  const input = r.input = pushable<SDKUserMessage>();
  // File checkpoints make rewind possible (B13; they do not cover what a shell command changed); a sub-agent's text and
  // Claude's summarized thinking come through so the window can show them (B16, B18); the extra folders are the
  // session's own (C5). Bypassing permissions is allowed as a mode, never the default (C4).
  const options: Options = {
    cwd: s.s.cwd, env: claudeEnv(), pathToClaudeCodeExecutable: EXE, systemPrompt: PROMPT, includePartialMessages: true,
    model: s.s.model || undefined, effort: (EFFORTS.includes(s.s.effort) ? s.s.effort : undefined) as Options['effort'],
    permissionMode: (MODES().some(m => m[0] === s.s.mode) ? s.s.mode : 'auto') as Options['permissionMode'], allowDangerouslySkipPermissions: true,
    enableFileCheckpointing: true, forwardSubagentText: true, extraArgs: { 'thinking-display': 'summarized' },
    ...s.s.dirs?.length ? { additionalDirectories: s.s.dirs } : {},
    canUseTool: (name, input, o) => new Promise<PermissionResult>(resolve => {
      const id = o.toolUseID || randomUUID();
      r.pending.set(id, { resolve, name, input, suggestions: o.suggestions });
      s.ask(reqOf(s, id, name, input, o));
      o.signal.addEventListener('abort', () => { if (r.pending.delete(id)) s.answered(id, tr('没回答', 'Not answered')); });
    }),
    // An MCP server's form, or a page it wants opened, a sign-in most often (C3); one that is not a web page is refused.
    onElicitation: (e, o) => new Promise<ElicitationResult | null>(resolve => {
      const url = e.mode === 'url' ? str(e.url) : '', server = e.displayName || e.serverName;
      if (e.mode === 'url' && !/^https?:\/\//i.test(url)) { s.note(tr(`${server} 要打开的不是网页，先拒绝了`, `${server} asked to open something that is not a web page, so it was declined`)); resolve({ action: 'decline' }); return; }
      const id = o.requestId || randomUUID(), fields = url ? [] : fieldsOf(e.requestedSchema);
      r.pending.set(id, { form: resolve, fields, ...url ? { url } : {} });
      s.ask({ id, tool: 'Form', server, why: e.message || str(e.title), fields, ...url ? { url } : {} });
      o.signal.addEventListener('abort', () => { if (r.pending.delete(id)) s.answered(id, tr('没回答', 'Not answered')); });
    }),
    spawnClaudeCodeProcess: viaKeeper(s),
    ...(r.fresh ? { sessionId: s.s.id } : { resume: cur(s) }),
  };
  r.fresh = false;
  const q = r.q = query({ prompt: input, options });
  void (async () => {
    try { for await (const m of q) frame(s, m); }
    catch (e) { log('claude session', s.s.id, e); if (r.q === q) s.end(undefined, false, 'err', tr(`Claude 停了：${String(e instanceof Error ? e.message : e).slice(0, 120)}`, `Claude stopped: ${String(e instanceof Error ? e.message : e).slice(0, 120)}`)); }
    finally { if (r.q === q) { r.q = undefined; r.input = undefined; } }
  })();
  return r;
}
function frame(s: Session, m: SDKMessage) {
  const r = rt(s), any = m as Record<string, unknown>;
  // After a reset the session goes on under a session id of its own (conversation_reset's new_conversation_id is not
  // it). What the keeper replays carries ids already known.
  if (typeof any.session_id === 'string' && any.session_id && !ids(s).includes(any.session_id)) {
    r.tasks.clear(); r.usage = undefined;
    s.set({ resets: [...s.s.resets ?? [], any.session_id], ctx: 0 });
    s.note(CLEARED());
  }
  // A message sent while it worked is taken when Claude Code starts it: folded into the running turn at its next step,
  // or run as the next turn.
  if (any.type === 'command_lifecycle' && any.state === 'started' && typeof any.command_uuid === 'string') {
    const q = r.queued.get(any.command_uuid);
    if (q) { r.queued.delete(any.command_uuid); s.dequeue(q.text); s.you(q.text, q.files, undefined, any.command_uuid); s.looked(any.command_uuid); s.begin(); }
    return;
  }
  if (m.type === 'stream_event') {
    if (m.parent_tool_use_id) return;
    const e = m.event as { type: string; content_block?: { type: string }; delta?: { type: string; text?: string } };
    // A thought is a step from its first moment, so the window can count its seconds.
    if (e.type === 'content_block_start') { r.block = ''; if (e.content_block?.type === 'thinking') { r.think = `think:${randomUUID()}`; r.thinkAt = Date.now(); s.tool(r.think, { k: 'think', t: '' }); } }
    else if (e.type === 'content_block_delta' && e.delta?.type === 'text_delta') { r.block += e.delta.text ?? ''; s.delta(r.block); }
    return;
  }
  if (m.type === 'assistant' || m.type === 'user') { said(s, m, undefined, true); return; }
  if (m.type === 'result') {
    const res = m as Record<string, any>, model = Object.values((res.modelUsage ?? {}) as Record<string, { contextWindow?: number }>)[0];
    if (model?.contextWindow && r.usage) s.set({ ctx: Math.min(100, Math.round(r.usage / model.contextWindow * 100)) });
    const aborted = String(res.terminal_reason ?? '').startsWith('aborted') || !!r.interrupted;
    r.interrupted = false; r.think = undefined;
    // What Allen sent while it worked runs next, interrupted or not.
    if (r.queued.size) { s.end(undefined, true); if (aborted) s.note(tr('你打断了这一轮', 'You interrupted this turn')); return; }
    if (aborted) { s.end(undefined, false, 'done', tr('你打断了这一轮', 'You interrupted this turn'), false); s.note(tr('你打断了这一轮 · 发一句就能接着来', 'You interrupted this turn · send a line to continue')); return; }
    if (res.is_error || res.subtype !== 'success') s.end(undefined, false, 'err', oneLine((res.errors as string[] | undefined)?.join(' · ') || str(res.result) || tr('出错了', 'Error')));
    else { s.end(); if ((r.titled = (r.titled ?? 0) + 1) <= 3) void titleOf(s); }
    return;
  }
  if (m.type !== 'system') return;
  const sub = any.subtype;
  if (sub === 'init') { if (Array.isArray(any.slash_commands)) cmdCache.set(s.s.cwd, { at: Date.now(), list: (any.slash_commands as string[]).map(c => [`/${c}`, ''] as [string, string]) }); }
  else if (sub === 'status') s.set(any.status === 'compacting' ? { st: 'pack', now: tr('在压缩上下文', 'Compacting context') } : s.s.st === 'pack' ? { st: 'work', now: tr('在想', 'Thinking') } : {});
  else if (sub === 'compact_boundary') s.note(tr('上下文压缩过了', 'Context compacted'));
  else if (sub === 'api_retry') s.set({ now: tr(`API 出错，第 ${any.attempt} 次重试`, `API error, retry ${any.attempt}`), summary: tr(`API 出错，第 ${any.attempt} 次重试`, `API error, retry ${any.attempt}`) });
  else if (sub === 'background_tasks_changed' && Array.isArray(any.tasks)) {
    const ts = any.tasks as { task_id: string; description: string }[];
    s.set({ bg: ts.length ? tr(`${ts.length} 个后台任务 · ${ts.map(t => t.description).join('、').slice(0, 80)}`, `${plural(ts.length, 'background task')} · ${ts.map(t => t.description).join(', ').slice(0, 80)}`) : undefined });
  }
  // Its background work, one entry per task (B17); the call that started one knows it, so a sub-agent stops on its own.
  else if (sub === 'task_started') {
    s.task(str(any.task_id), { kind: str(any.task_type) || (any.subagent_type ? 'local_agent' : 'task'), what: str(any.description) || str(any.subagent_type), st: 'run', since: Date.now(),
      ...any.is_backgrounded === false ? { fg: true } : {} });
    if (typeof any.tool_use_id === 'string') s.toolDone(any.tool_use_id, { task: str(any.task_id) });
  }
  else if (sub === 'task_updated') {
    const p = (any.patch ?? {}) as Record<string, unknown>, st = TASK_ST[str(p.status)];
    s.task(str(any.task_id), { ...st ? { st } : {}, ...p.description ? { what: str(p.description) } : {}, ...typeof p.end_time === 'number' ? { ended: p.end_time } : {},
      ...p.is_backgrounded === true ? { fg: false } : {} });
  }
  else if (sub === 'task_notification') s.task(str(any.task_id), { st: TASK_ST[str(any.status)] ?? 'done', ended: Date.now(), ...any.output_file ? { out: str(any.output_file) } : {} });
  // Skills and commands that came or went while it ran.
  else if (sub === 'commands_changed' && Array.isArray(any.commands)) cmdCache.set(s.s.cwd, { at: Date.now(), list: (any.commands as { name: string; description?: string; argumentHint?: string }[]).map(c => [`/${c.name}`, [c.description, c.argumentHint].filter(Boolean).join(' · ')] as [string, string]) });
  else if (sub === 'permission_denied') s.note(tr(`自动模式没让它${str(any.tool_name) ? `用 ${any.tool_name}` : '做这一步'}${any.message ? `：${oneLine(str(any.message), 80)}` : ''}`, `Auto mode did not let it ${str(any.tool_name) ? `use ${any.tool_name}` : 'do this step'}${any.message ? `: ${oneLine(str(any.message), 80)}` : ''}`));
}
const oneLine = (t: string, n = 120) => { const x = t.replace(/\s+/g, ' ').trim(); return x.length > n ? `${x.slice(0, n - 1)}…` : x; };
const TASK_ST: Record<string, Task['st']> = { pending: 'run', running: 'run', paused: 'run', completed: 'done', failed: 'fail', killed: 'stop', stopped: 'stop' };
// Claude Code names a session itself once its first exchange is under way, sometimes a turn later; the SDK reads that
// name, or one given with /rename in a terminal, as customTitle. The row takes it unless Allen named it here (B14).
// The name is asked for on the side while the turn runs, so a quick turn can end before it is written: looked for once
// more a few seconds later.
async function titleOf(s: Session, again = true) {
  if (s.s.named) return;
  const t = oneLine((await getSessionInfo(cur(s), { dir: s.s.cwd }).catch(() => undefined))?.customTitle ?? '', 80);
  if (t && t !== s.s.title) s.set({ title: t });
  else if (!t && again) setTimeout(() => void titleOf(s, false), 5000).unref();
}

// ---------- the context window, as /context counts it (getContextUsage, token counts, not estimates) ----------
const CTX_NAME = (): Record<string, string> => ({ 'System prompt': tr('系统提示词', 'System prompt'), 'System tools': tr('内置工具', 'Built-in tools'), 'MCP server instructions': tr('MCP 说明', 'MCP instructions'), 'MCP tools': tr('MCP 工具', 'MCP tools'),
  'Custom agents': tr('自定义 agent', 'Custom agents'), 'Memory files': tr('记忆文件', 'Memory files'), Skills: 'Skills', Messages: tr('对话', 'Conversation'), 'Compact buffer': tr('留给压缩', 'Reserved for compaction'), 'Free space': tr('还空着', 'Free') });
// The biggest few, the rest as one line.
function top(xs: [string, number][], n = 6): [string, number][] {
  const s = xs.filter(x => x[1] > 0).sort((a, b) => b[1] - a[1]);
  return s.length > n + 1 ? [...s.slice(0, n), [tr(`其余 ${s.length - n} 个`, `${s.length - n} more`), s.slice(n).reduce((a, x) => a + x[1], 0)]] : s;
}
function ctxOf(s: Session, u: SDKControlGetContextUsageResponse): Ctx {
  const b = u.messageBreakdown, msgs = u.categories.find(c => c.name === 'Messages')?.tokens ?? 0;
  // The split inside the conversation is Claude Code's own estimate and does not add up to the counted total, so it is
  // scaled to that total.
  const parts: [string, number][] = b ? [[tr('工具结果', 'Tool results'), b.toolResultTokens], [tr('它写的', 'What it wrote'), b.assistantMessageTokens], [tr('工具调用', 'Tool calls'), b.toolCallTokens], [tr('你说的', 'What you said'), b.userMessageTokens],
    [tr('附带的提醒和说明', 'Attached reminders and notes'), b.attachmentTokens], [tr('其他', 'Other'), b.redirectedContextTokens + b.unattributedTokens]] : [];
  const k = msgs / (parts.reduce((a, p) => a + p[1], 0) || 1), scale = (xs: [string, number][]) => xs.map(([n, t]) => [n, Math.round(t * k)] as [string, number]);
  const tools = top(scale((b?.toolCallsByType ?? []).map(t => [t.name, t.callTokens + t.resultTokens])), 5);
  const sub: Record<string, CtxRow['sub']> = {
    'System prompt': top((u.systemPromptSections ?? []).map(x => [x.name, x.tokens])),
    'System tools': top((u.systemTools ?? []).map(x => [x.name, x.tokens])),
    'MCP tools': top(u.mcpTools.filter(x => x.isLoaded !== false).map(x => [`${x.serverName} · ${x.name}`, x.tokens])),
    'Custom agents': top(u.agents.map(x => [x.agentType, x.tokens])),
    'Memory files': u.memoryFiles.map(x => [rel(s, x.path), x.tokens]),
    Skills: top((u.skills?.skillFrontmatter ?? []).map(x => [x.name, x.tokens])),
    Messages: parts.length ? [tr('大约的分法', 'Approximate split'), ...top(scale(parts)), ...tools.length ? [tr('最占地方的工具', 'Tools taking the most space'), ...tools] : []] : [],
  };
  const rows: CtxRow[] = u.categories.filter(c => c.kind !== 'deferred').map(c => ({ n: CTX_NAME()[c.name] ?? c.name, t: c.tokens,
    ...c.kind === 'buffer' ? { kind: 'buf' as const } : c.kind === 'free' ? { kind: 'free' as const } : {}, ...sub[c.name]?.length ? { sub: sub[c.name] } : {} }));
  const fixed = u.totalTokens - msgs, mem = u.categories.find(c => c.name === 'Memory files')?.tokens ?? 0;
  const deferred = u.categories.filter(c => c.kind === 'deferred').reduce((a, c) => a + c.tokens, 0);
  const say: [string, string] = u.percentage >= 75 ? [tr('快满了。', 'Nearly full.'), u.isAutoCompactEnabled ? tr('再满一点它会自己压缩。', ' It compacts itself when it fills up a little more.') : tr('发 /compact 能把对话缩成一段摘要。', ' Send /compact to shrink the conversation into a summary.')]
    : msgs > fixed ? [tr('对话占了大头', 'The conversation takes most of it'), tools[0] ? tr(`，里面最多的是 ${tools[0][0]} 的来回，大约 ${kt(tools[0][1])}。`, `, mostly the back-and-forth with ${tools[0][0]}, about ${kt(tools[0][1])}.`) : tr('。', '.')]
    : [tr('还很空。', 'Plenty of room.'), tr(`一开会话，系统提示、工具、记忆和 Skills 就先占了 ${kt(fixed)}${mem ? `，其中记忆文件 ${kt(mem)}` : ''}。`, ` A new session starts with the system prompt, tools, memory and Skills taking ${kt(fixed)}${mem ? `, of which memory files ${kt(mem)}` : ''}.`)];
  return { used: u.totalTokens, max: u.maxTokens, model: u.model, rows, say,
    foot: [...deferred ? [tr(`还有 ${kt(deferred)} 的工具没载入，用到才占地方`, `${kt(deferred)} of tools are not loaded yet and only take space when used`)] : [], u.isAutoCompactEnabled ? tr('快满时会自己压缩', 'Compacts itself when nearly full') : tr('自动压缩关着 · 快满了要自己发 /compact', 'Auto-compact is off · send /compact yourself when it is nearly full')] };
}

// ---------- menus: what a fresh session in a folder offers, asked of a session that never gets a message ----------
const cmdCache = new Map<string, { at: number; list: [string, string][] }>();
let models: [string, string][] = [], efforts: string[] = [], probedAt = '', account: Record<string, string> | null = null;
// Commands whose screen in the terminal the window draws itself (B8): the third entry names its place for them.
const OWN_UI = (): [string, string, string][] => [['/rewind', tr('回到之前的某一句', 'Go back to an earlier message'), 'rewind'], ['/resume', tr('接手终端里开的会话', 'Take over a session opened in Terminal'), 'import'], ['/export', tr('导出整段对话', 'Export the whole conversation'), 'export'],
  ['/permissions', tr('换权限模式', 'Change the permission mode'), 'mode'], ['/memory', tr('打开 CLAUDE.md', 'Open CLAUDE.md'), 'memory'], ['/tasks', tr('后台任务', 'Background tasks'), 'tasks'], ['/ide', tr('用编辑器打开', 'Open in an editor'), 'editor'],
  ['/login', tr('看用的哪份登录，没登就在这里登', 'See which login is used, sign in here if none'), 'doctor'], ['/status', tr('看版本、登录和后台开没开', 'See the version, the login and whether the host is running'), 'doctor'], ['/doctor', tr('体检：claude、codex、git 都装好没有', 'Check-up: are claude, codex and git installed'), 'doctor'], ['/diff', tr('看它改了什么', 'See what it changed'), 'changes'],
  ['/add-dir', tr('让它也能动另一个文件夹', 'Let it work in another folder too'), 'dirs'], ['/model', tr('换模型', 'Change the model'), 'model'], ['/effort', tr('换力度', 'Change the effort'), 'effort'], ['/new', tr('开新会话', 'Start a new session'), 'new'], ['/fork', tr('从某一句之前分出一个新会话', 'Fork a new session from before a message'), 'fork'],
  ['/btw', tr('侧问：不打断它，也不进对话', 'Side question: does not interrupt it or enter the conversation'), 'btw'], ['/mcp', tr('看它用的 MCP，在这里开关', 'See the MCP servers it uses and switch them here'), 'mcp']];
const withUi = (list: [string, string][]): [string, string, string?][] => [...OWN_UI(), ...list.filter(c => !OWN_UI().some(u => u[0] === c[0]))];
const version = () => EXE ? realpathSync(EXE) : '';
async function probe(cwd: string) {
  probedAt = version();
  const input = pushable<SDKUserMessage>();
  const q = query({ prompt: input, options: { cwd, env: claudeEnv(), pathToClaudeCodeExecutable: EXE, systemPrompt: PROMPT } });
  try {
    const init = await Promise.race([q.initializationResult(), new Promise<never>((_, no) => setTimeout(() => no(new Error(tr('Claude 没有及时答应', 'Claude did not answer in time'))), 30000))]);
    cmdCache.set(cwd, { at: Date.now(), list: init.commands.map(c => [`/${c.name}`, [c.description, c.argumentHint].filter(Boolean).join(' · ')] as [string, string]) });
    account = Object.fromEntries(Object.entries(init.account ?? {}).filter(([, v]) => typeof v === 'string')) as Record<string, string>;
    const ms = init.models.map(m => [m.value, m.displayName] as [string, string]);
    const es = [...new Set(init.models.flatMap(m => m.supportedEffortLevels ?? []))];
    if (ms.length && JSON.stringify(ms) !== JSON.stringify(models)) { models = ms; efforts = es.length ? EFFORTS.filter(e => es.includes(e as never)) : EFFORTS; void catalogChanged(); }
  } finally { q.close(); input.end(); }
}
let probed: Promise<void> | null = null;
// Nothing promises Claude Code updates itself when only the SDK runs it, so the host asks for the update every hour;
// whoever moved the install (this or a terminal session), the menus are read again and pushed to open windows.
function update() {
  execFile(OWN, ['update'], { env: claudeEnv(false), timeout: 300_000 }, e => {
    if (e) log('claude update', e.message.slice(0, 200));
    if (probed && version() !== probedAt) probed = probe(homedir()).catch(e => { log('claude probe', e); probed = null; });
  });
}
if (EXE === OWN) { update(); setInterval(update, 3_600_000).unref(); }

export const claude: Driver = {
  // The packaged app starts no claude of its own before it has the owner's key: one without it would sign in with this
  // Mac's own Claude Code login (ADR 0094).
  async catalog(): Promise<Choice> {
    if (auth().ready) probed ??= probe(homedir()).catch(e => { log('claude probe', e); probed = null; });
    return { models, efforts: efforts.length ? efforts : EFFORTS, modes: MODES(), always: tr('以后都允许', 'Always allow') };
  },
  async create(s) { rt(s).fresh = true; return randomUUID(); },
  // Pictures go as images and PDFs as documents; any other file is named at the end of the text (B7).
  async send(s, text, files: File[]) {
    const r = rt(s), uuid = randomUUID(), x = await sent(files, 'claude'), full = attach(text, x.paths);
    const blocks = [...x.images.map(i => ({ type: 'image' as const, source: { type: 'base64' as const, media_type: i.type as 'image/png', data: i.data } })),
      ...x.pdfs.map(p => ({ type: 'document' as const, title: p.name, source: { type: 'base64' as const, media_type: 'application/pdf' as const, data: p.data } }))];
    const busy = !!r.q && (s.s.st === 'work' || s.s.st === 'pack' || s.s.st === 'wait');
    if (busy) { r.queued.set(uuid, { text, files: x.pics }); s.enqueue(text); }
    else { s.you(text, x.pics, undefined, uuid); s.begin(); }
    ensure(s).input!.push({ type: 'user', message: { role: 'user', content: blocks.length ? [...blocks, { type: 'text', text: full }] : full }, parent_tool_use_id: null, uuid });
  },
  // A message it has not taken yet can still be taken back (B11).
  async unqueue(s, text) {
    const r = rt(s), hit = [...r.queued].find(([, q]) => rode(q.text).text === text);
    if (!hit || !r.q) return false;
    const ok = await (r.q as unknown as { cancelAsyncMessage(uuid: string): Promise<boolean> }).cancelAsyncMessage(hit[0]).catch(() => false);
    if (ok) { r.queued.delete(hit[0]); s.dequeue(text); }
    return ok;
  },
  async stopTask(s, id) {
    const q = rt(s).q;
    if (!q) throw new Error(tr('它现在没在跑', 'It is not running right now'));
    await q.stopTask(id);
  },
  async rewind(s, at, dry) {
    const q = ensure(s).q!, r = await q.rewindFiles(at, { dryRun: dry });
    return { can: r.canRewind, ...r.error ? { why: r.error } : {}, files: r.filesChanged ?? [], add: r.insertions ?? 0, del: r.deletions ?? 0 };
  },
  answer(s, a) {
    const r = rt(s), p = r.pending.get(a.req);
    if (!p) return;
    r.pending.delete(a.req);
    let done: string;
    // A form: what was filled in (the host checked it against the fields), not given, or the call it came from cancelled.
    if ('form' in p) {
      if (a.decision === 'deny') { p.form({ action: 'decline' }); done = tr('不提供，继续', 'Declined, continuing'); }
      else if (a.decision === 'cancel') { p.form({ action: 'cancel' }); done = tr('取消了', 'Cancelled'); }
      else { p.form({ action: 'accept', ...p.url ? {} : { content: (a.values ?? {}) as ElicitationResult['content'] } }); done = p.url ? tr('同意打开网页', 'Agreed to open the page') : tr('已提供', 'Provided'); }
    } else if (p.name === 'AskUserQuestion') {
      const qs = (p.input.questions ?? []) as { question: string }[];
      if (a.decision === 'deny') { p.resolve({ behavior: 'deny', message: 'The user dismissed the question.' }); done = tr('没回答', 'Not answered'); }
      else {
        const answers = Object.fromEntries(qs.map((q, i) => [q.question, a.text !== undefined && i === 0 ? a.text : (a.answers?.[i] ?? []).join(', ')]));
        p.resolve({ behavior: 'allow', updatedInput: { ...p.input, answers } });
        done = tr(`你${a.text !== undefined ? '回答' : '选了'}：${Object.values(answers).filter(Boolean).join(' · ')}`, `You ${a.text !== undefined ? 'answered' : 'chose'}: ${Object.values(answers).filter(Boolean).join(' · ')}`);
      }
    } else if (p.name === 'ExitPlanMode') {
      if (a.decision === 'deny') { p.resolve({ behavior: 'deny', message: a.text || 'The user wants to rethink this plan before you start.' }); done = a.text ? tr(`再想想：${a.text}`, `Think again: ${a.text}`) : tr('再想想', 'Think again'); }
      else {
        const next = r.prev && r.prev !== 'plan' ? r.prev : 'auto';
        p.resolve({ behavior: 'allow', updatedInput: p.input, updatedPermissions: [{ type: 'setMode', mode: next as 'auto', destination: 'session' }] });
        s.set({ mode: next });
        done = tr('就这么做', 'Go ahead');
      }
    } else if (a.decision === 'deny') { p.resolve({ behavior: 'deny', message: a.text || 'The user said no.' }); done = a.text ? tr(`拒绝了：${a.text}`, `Denied: ${a.text}`) : tr('拒绝了', 'Denied'); }
    else {
      p.resolve({ behavior: 'allow', updatedInput: p.input, ...(a.decision === 'always' && p.suggestions?.length ? { updatedPermissions: p.suggestions } : {}) });
      done = a.decision === 'always' ? tr('已允许 · 以后都允许', 'Allowed · always') : tr('已允许', 'Allowed');
    }
    s.answered(a.req, done);
  },
  async interrupt(s) {
    const r = rt(s);
    if (!r.q) return;
    r.interrupted = true;
    await r.q.interrupt();
  },
  async release(s) {
    const r = rt(s), q = r.q;
    for (const p of r.pending.values()) {
      if ('form' in p) p.form({ action: 'cancel' });
      else p.resolve({ behavior: 'deny', message: 'Session released.', interrupt: true });
    }
    r.pending.clear();
    for (const [, x] of r.queued) s.dequeue(x.text);
    r.queued.clear();
    r.q = undefined;
    q?.close(); r.input?.end(); r.input = undefined;
    if (s.s.st === 'work' || s.s.st === 'pack' || s.s.st === 'wait') s.end(undefined, true);
  },
  async set(s, k, v) {
    const q = rt(s).q;
    if (k === 'mode' && v === 'plan') rt(s).prev = s.s.mode;
    if (!q) return;
    if (k === 'model') await q.setModel(v);
    else if (k === 'effort') await q.applyFlagSettings({ effortLevel: v as 'high' });
    else await q.setPermissionMode(v as 'auto');
  },
  async rename(s, title) { await renameSession(cur(s), title, { dir: s.s.cwd }); },
  async load(s) {
    const r = rt(s);
    r.tasks.clear();
    const parts = await Promise.all(ids(s).map(id => getSessionMessages(id, { dir: s.s.cwd }).catch(() => [])));
    r.seen = new Set(parts.flat().map(m => m.uuid));
    const read = (msgs: typeof parts[number]) => {
      let prev: number | undefined;
      for (const m of msgs) {
        const t = Date.parse(String((m as Record<string, unknown>).timestamp ?? '')), at = Number.isFinite(t) ? t : undefined;
        said(s, m as never, at, false, prev);
        prev = at ?? prev;
      }
    };
    await s.build(async () => {
      parts.forEach((msgs, k) => {
        // The line sits after the /clear that drew it, where it came live.
        const lead = k && /<command-name>\/(clear|reset|new)</.test(textOf((msgs[0]?.message as { content?: unknown } | undefined)?.content)) ? 1 : 0;
        read(msgs.slice(0, lead));
        if (k) s.note(CLEARED());
        read(msgs.slice(lead));
      });
    }, r.open);
    r.open = false;
  },
  // A child the keeper kept through a host restart: read what the transcript has, leave a running turn open, then
  // take the child back; the keeper replays what the transcript does not have yet.
  async adopt(s, busy, news) {
    const r = rt(s);
    Object.assign(r, { open: busy, news });
    s.items = null;
    await s.ensureLoaded();
    ensure(s);
  },
  // Up to a message: the transcript that holds it (after a clear, a later one) is copied up to that message, or up to
  // the one before it. Without a title Claude Code names the copy "<its name> (fork)".
  async fork(s, at, before, title) {
    if (!at) return (await forkSession(cur(s), { dir: s.s.cwd, title })).sessionId;
    for (const id of [...ids(s)].reverse()) {
      const msgs = await getSessionMessages(id, { dir: s.s.cwd }).catch(() => []), i = msgs.findIndex(m => m.uuid === at);
      if (i < 0) continue;
      const upTo = before ? msgs[i - 1]?.uuid : at;
      return upTo ? (await forkSession(id, { dir: s.s.cwd, upToMessageId: upTo, title })).sessionId : null;
    }
    throw new Error(tr('它的记录里没有这一句', 'That message is not in its history'));
  },
  // A transcript that is already gone counts as deleted.
  async remove(s) {
    await claude.release(s);
    for (const id of ids(s)) await deleteSession(id, { dir: s.s.cwd }).catch(e => { if (!/not found/i.test(String(e))) throw e; });
  },
  async commands(cwd, s) {
    const q = s ? rt(s).q : undefined;
    if (q) return withUi((await q.supportedCommands()).map(c => [`/${c.name}`, [c.description, c.argumentHint].filter(Boolean).join(' · ')] as [string, string]));
    const c = cmdCache.get(cwd);
    if (auth().ready && (!c || Date.now() - c.at > 10 * 60_000 || c.list.every(x => !x[1]))) await probe(cwd).catch(e => log('claude probe', cwd, e));
    return withUi(cmdCache.get(cwd)?.list ?? []);
  },
  // Its own record of every session in a folder (B12).
  async outside(cwd) {
    const list = await listSessions({ ...cwd ? { dir: cwd } : {}, limit: 200 }).catch(() => []);
    return list.map(i => ({ agent: 'claude' as const, id: i.sessionId, title: oneLine(i.summary || i.firstPrompt || i.sessionId, 80), cwd: i.cwd || cwd, updated: i.lastModified,
      ...i.gitBranch ? { branch: i.gitBranch } : {} }));
  },
  async account() { return account; },
  resume: s => `claude --resume ${cur(s)}`,
  async context(s) { return asking(s, async q => ctxOf(s, await q.getContextUsage()), true); },
  async mcp(s) { return asking(s, (q, live) => servers(q, live, !live)); },
  // Switching one off holds in this folder for every Claude Code session, a terminal's too; connecting again needs it
  // running. A sign-in is Claude Code's own: the session's claude gives the page to open and waits for the browser to
  // come back to it, so the session starts for it, and connects the server once it is signed in to.
  async mcpAct(s, name, act) {
    if (act === 'login') {
      const q = ensure(s).q!, one = (await servers(q, true, false)).find(m => m.name === name);
      if (!one) throw new Http(404, tr('没有这个 MCP', 'No such MCP server'));
      if (!one.can.includes('login')) throw new Http(409, tr('它不用登录', 'It needs no sign-in'));
      const r = await (q as unknown as SignIn).mcpAuthenticate(name), url = str(r?.authUrl);
      if (r?.requiresUserAction === false) return servers(q, true, true);
      if (!/^https?:\/\//i.test(url)) throw new Http(502, tr('Claude Code 没给能打开的登录页', 'Claude Code gave no sign-in page to open'));
      return { url };
    }
    return asking(s, async (q, live) => {
      const one = (await servers(q, live, false)).find(m => m.name === name);
      if (!one) throw new Http(404, tr('没有这个 MCP', 'No such MCP server'));
      if (!one.can.includes(act)) throw new Http(409, act === 'reconnect' ? tr('它现在没在跑，下次开始时会重新连', 'It is not running right now and will reconnect on the next start') : act === 'on' ? tr('它开着', 'It is already on') : tr('它关着', 'It is already off'));
      if (act === 'reconnect') await q.reconnectMcpServer(name);
      else await q.toggleMcpServer(name, act === 'on');
      return servers(q, live, true);
    });
  },
  // As Claude Code's /btw asks it; an idle session is asked through a claude of its own that reads the conversation back.
  async side(s, text, history, signal) {
    return asking(s, async q => {
      const r = await (q as unknown as Sideways).askSideQuestion(text, { history: history.map(([question, response]) => ({ question, response })), signal });
      if (!r?.response) throw new Http(502, tr('Claude 没答上来', 'Claude did not answer'));
      return r.response;
    }, true);
  },
};

// A running session is asked directly; an idle one through a query of its own in its folder that ends once it has
// answered, so looking never keeps a process around. `resume`: what is asked needs the conversation.
async function asking<T>(s: Session, f: (q: Query, live: boolean) => Promise<T>, resume = false): Promise<T> {
  const running = rt(s).q;
  if (running) return f(running, true);
  const input = pushable<SDKUserMessage>();
  const q = query({ prompt: input, options: { cwd: s.s.cwd, env: claudeEnv(), pathToClaudeCodeExecutable: EXE, systemPrompt: PROMPT, model: s.s.model || undefined,
    permissionMode: (MODES().some(m => m[0] === s.s.mode) ? s.s.mode : 'auto') as Options['permissionMode'], ...resume ? { resume: cur(s) } : {} } });
  try { return await f(q, false); } finally { q.close(); input.end(); }
}
// Its MCP servers (C3). `settle`: wait, up to 15 seconds, for the ones still connecting (a query that has just started,
// one just switched on or connected again).
const MCP_ST: Record<McpServerStatus['status'], Mcp['st']> = { connected: 'on', pending: 'wait', 'needs-auth': 'auth', failed: 'fail', disabled: 'off' };
async function servers(q: Query, live: boolean, settle: boolean): Promise<Mcp[]> {
  let list = await q.mcpServerStatus();
  for (let i = 0; settle && i < 50 && list.some(m => m.status === 'pending'); i++) { await new Promise(r => setTimeout(r, 300)); list = await q.mcpServerStatus(); }
  return list.map(m => {
    const st = MCP_ST[m.status] ?? 'wait', scope = m.source || m.scope;
    return { name: m.name, st, ...m.tools ? { tools: m.tools.length } : {}, ...scope ? { scope } : {},
      ...st === 'fail' && m.error ? { why: oneLine(m.error, 200) } : st === 'auth' ? { why: tr('要登录', 'Sign-in needed') } : {},
      can: st === 'off' ? ['on'] : [...live ? ['off', 'reconnect'] as const : ['off'] as const, ...st === 'auth' ? ['login'] as const : []] };
  });
}

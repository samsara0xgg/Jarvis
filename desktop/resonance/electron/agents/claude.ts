// Claude Code through the Claude Agent SDK (ADR 0067): one `claude` child per session that has something to do, fed by a
// message queue that never ends, so the session lives between turns. Permission prompts, questions and plan approval
// all come through canUseTool and wait for Allen's answer in the window.
import { randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { deleteSession, forkSession, getSessionMessages, query, renameSession, type Options, type PermissionResult, type PermissionUpdate,
  type Query, type SDKMessage, type SDKUserMessage } from '@anthropic-ai/claude-agent-sdk';
import { catalogChanged, log, type Driver, type Session } from './host.js';
import type { Choice, Diff, File, Req, Step } from './types.js';

// Allen's subscription, never an API key; and nothing that says this runs inside another Claude Code session. The
// marker keeps Jarvis's own PermissionRequest hook (ADR 0049) out of sessions this window answers itself.
const ENV: Record<string, string | undefined> = Object.fromEntries(Object.entries(process.env)
  .filter(([k]) => !/^(ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|CLAUDECODE|CLAUDE_CODE_|CLAUDE_JOB)/.test(k)));
ENV.JARVIS_AGENTS_HOST = '1';
const PROMPT = { type: 'preset', preset: 'claude_code' } as const;
const MODES: [string, string][] = [['auto', '自动'], ['default', '改之前问我'], ['acceptEdits', '自动接受修改'], ['plan', '计划模式']];
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
type Pending = { resolve: (r: PermissionResult) => void; name: string; input: Record<string, unknown>; suggestions?: PermissionUpdate[] };
// prev: the mode to go back to when a plan is approved · usage: tokens the last answer was sent with · block: the text
// block streaming in · creates: TaskCreate calls waiting for the id their result gives
type Rt = {
  q?: Query; input?: ReturnType<typeof pushable<SDKUserMessage>>; fresh?: boolean;
  pending: Map<string, Pending>; queued: Map<string, { text: string; files: string[] }>;
  tasks: Map<string, [string, 0 | 1 | 2]>; creates: Map<string, Record<string, unknown>>;
  interrupted?: boolean; usage?: number; prev?: string; block: string;
};
const rt = (s: Session): Rt => (s.rt.claude ??= { pending: new Map(), queued: new Map(), tasks: new Map(), creates: new Map(), block: '' }) as Rt;

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
  const always = o.suggestions?.length ? '以后都允许' : '';
  if (name === 'AskUserQuestion' && Array.isArray(input.questions)) return { id, tool: 'Ask', qs: input.questions.map((q: Record<string, unknown>) => ({
    q: str(q.question), head: str(q.header), multi: q.multiSelect === true,
    opts: Array.isArray(q.options) ? q.options.map((x: Record<string, unknown>) => [str(x.label), str(x.description)] as [string, string]) : [] })) };
  if (name === 'ExitPlanMode') return { id, tool: 'Plan', plan: str(input.plan) };
  if (name === 'Bash') return { id, tool: 'Bash', why: str(input.description) || o.title || '', cmd: str(input.command), cwd: s.s.cwd, always };
  const st = stepOf(s, name, input);
  if (st?.k === 'edit') return { id, tool: 'Edit', why: o.title ?? '', file: st.t, diff: st.diff ?? [], always };
  return { id, tool: 'Tool', why: o.title || o.description || '', name: o.displayName || st?.t || name, detail: JSON.stringify(input, null, 1).slice(0, 800), always };
}
const textOf = (c: unknown): string => typeof c === 'string' ? c : Array.isArray(c) ? c.map(b => b?.type === 'text' ? str(b.text) : '').filter(Boolean).join('\n') : '';

// ---------- one message from the session, live or from its transcript ----------
type Block = { type: string; id?: string; name?: string; input?: Record<string, unknown>; text?: string; tool_use_id?: string; content?: unknown; is_error?: boolean };
// Live, Allen's own words are already on screen when he sends them; from a transcript they are read back here.
function said(s: Session, m: SDKMessage | { type: string; message?: unknown; tool_use_result?: unknown; parent_tool_use_id?: string | null }, at?: number, live = false) {
  const msg = (m as { message?: { content?: unknown } }).message;
  if (m.type === 'assistant') {
    if ((m as { parent_tool_use_id?: string | null }).parent_tool_use_id) return;
    const u = (msg as { usage?: Record<string, number> } | undefined)?.usage;
    if (u) rt(s).usage = (u.input_tokens ?? 0) + (u.cache_read_input_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0);
    for (const b of (msg?.content ?? []) as Block[]) {
      if (b.type === 'text' && b.text) s.say(b.text, at);
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
    if ((m as { parent_tool_use_id?: string | null }).parent_tool_use_id) return;
    const content = msg?.content;
    if (Array.isArray(content) && content.some((b: Block) => b.type === 'tool_result')) {
      for (const b of content as Block[]) {
        if (b.type !== 'tool_result' || !b.tool_use_id) continue;
        const created = rt(s).creates.get(b.tool_use_id);
        if (created) { rt(s).creates.delete(b.tool_use_id); planOf(s, 'TaskCreate', created, (m as { tool_use_result?: unknown }).tool_use_result); continue; }
        const r = (m as { tool_use_result?: Record<string, unknown> }).tool_use_result, patch: Partial<Step> = { ok: !b.is_error };
        const out = r && typeof r === 'object' && ('stdout' in r || 'stderr' in r) ? [str(r.stdout), str(r.stderr)].filter(Boolean).join('\n') : textOf(b.content);
        if (b.is_error || out) patch.out = out.slice(0, 6000);
        if (r && Array.isArray(r.structuredPatch)) {
          const diff: Diff = (r.structuredPatch as { lines?: string[] }[]).flatMap(h => (h.lines ?? []).map(l => [l[0] === '+' ? '+' : l[0] === '-' ? '-' : ' ', l.slice(1)] as Diff[number]));
          if (diff.length) Object.assign(patch, { diff: diff.slice(0, 400), ...counts(diff) });
        }
        s.toolDone(b.tool_use_id, patch);
      }
      return;
    }
    if (live) return;
    // Allen's own words; Claude Code's bookkeeping in angle brackets is not.
    const text = textOf(content), cmd = /<command-name>([^<]*)<\/command-name>[\s\S]*?(?:<command-args>([^<]*)<\/command-args>)?/.exec(text);
    const files = Array.isArray(content) ? content.filter((b: Block) => b.type === 'image').map((_: unknown, k: number) => `图片 ${k + 1}`) : [];
    if (cmd) s.you(`${cmd[1]} ${cmd[2] ?? ''}`.trim());
    else if (/^\s*<(local-command|system-reminder|command-)/.test(text)) return;
    else if (/^\[Request interrupted/.test(text)) s.note('你打断了这一轮');
    else if (text.trim() || files.length) s.you(text.trim(), files);
  }
}

// ---------- the live session ----------
function ensure(s: Session) {
  const r = rt(s);
  if (r.q) return r;
  const input = r.input = pushable<SDKUserMessage>();
  const options: Options = {
    cwd: s.s.cwd, env: ENV, systemPrompt: PROMPT, includePartialMessages: true,
    model: s.s.model || undefined, effort: (EFFORTS.includes(s.s.effort) ? s.s.effort : undefined) as Options['effort'],
    permissionMode: (MODES.some(m => m[0] === s.s.mode) ? s.s.mode : 'auto') as Options['permissionMode'],
    canUseTool: (name, input, o) => new Promise<PermissionResult>(resolve => {
      const id = o.toolUseID || randomUUID();
      r.pending.set(id, { resolve, name, input, suggestions: o.suggestions });
      s.ask(reqOf(s, id, name, input, o));
      o.signal.addEventListener('abort', () => { if (r.pending.delete(id)) s.answered(id, '没回答'); });
    }),
    stderr: d => log('claude', s.s.id.slice(0, 8), d.trim().slice(0, 400)),
    ...(r.fresh ? { sessionId: s.s.id } : { resume: s.s.id }),
  };
  r.fresh = false;
  const q = r.q = query({ prompt: input, options });
  void (async () => {
    try { for await (const m of q) frame(s, m); }
    catch (e) { log('claude session', s.s.id, e); if (r.q === q) s.end(undefined, false, 'err', `Claude 停了：${String(e instanceof Error ? e.message : e).slice(0, 120)}`); }
    finally { if (r.q === q) { r.q = undefined; r.input = undefined; } }
  })();
  return r;
}
function frame(s: Session, m: SDKMessage) {
  const r = rt(s), any = m as Record<string, unknown>;
  // A message sent while it worked is taken when Claude Code starts it: folded into the running turn at its next step,
  // or run as the next turn.
  if (any.type === 'command_lifecycle' && any.state === 'started' && typeof any.command_uuid === 'string') {
    const q = r.queued.get(any.command_uuid);
    if (q) { r.queued.delete(any.command_uuid); s.dequeue(q.text); s.you(q.text, q.files); s.begin(); }
    return;
  }
  if (m.type === 'stream_event') {
    if (m.parent_tool_use_id) return;
    const e = m.event as { type: string; content_block?: { type: string }; delta?: { type: string; text?: string } };
    if (e.type === 'content_block_start') { r.block = ''; if (e.content_block?.type === 'thinking') s.set({ now: '在想' }); }
    else if (e.type === 'content_block_delta' && e.delta?.type === 'text_delta') { r.block += e.delta.text ?? ''; s.delta(r.block); }
    return;
  }
  if (m.type === 'assistant' || m.type === 'user') { said(s, m, undefined, true); return; }
  if (m.type === 'result') {
    const res = m as Record<string, any>, model = Object.values((res.modelUsage ?? {}) as Record<string, { contextWindow?: number }>)[0];
    if (model?.contextWindow && r.usage) s.set({ ctx: Math.min(100, Math.round(r.usage / model.contextWindow * 100)) });
    const aborted = String(res.terminal_reason ?? '').startsWith('aborted') || !!r.interrupted;
    r.interrupted = false;
    // What Allen sent while it worked runs next, interrupted or not.
    if (r.queued.size) { s.end(undefined, true); if (aborted) s.note('你打断了这一轮'); return; }
    if (aborted) { s.end(undefined, false, 'done', '你打断了这一轮', false); s.note('你打断了这一轮 · 发一句就能接着来'); return; }
    if (res.is_error || res.subtype !== 'success') s.end(undefined, false, 'err', oneLine((res.errors as string[] | undefined)?.join(' · ') || str(res.result) || '出错了'));
    else s.end();
    return;
  }
  if (m.type !== 'system') return;
  const sub = any.subtype;
  if (sub === 'init') { if (Array.isArray(any.slash_commands)) cmdCache.set(s.s.cwd, { at: Date.now(), list: (any.slash_commands as string[]).map(c => [`/${c}`, ''] as [string, string]) }); }
  else if (sub === 'status') s.set(any.status === 'compacting' ? { st: 'pack', now: '在压缩上下文' } : s.s.st === 'pack' ? { st: 'work', now: '在想' } : {});
  else if (sub === 'compact_boundary') s.note('上下文压缩过了');
  else if (sub === 'api_retry') s.set({ now: `API 出错，第 ${any.attempt} 次重试`, summary: `API 出错，第 ${any.attempt} 次重试` });
  else if (sub === 'background_tasks_changed' && Array.isArray(any.tasks)) {
    const ts = any.tasks as { task_id: string; description: string }[];
    s.set({ bg: ts.length ? `${ts.length} 个后台任务 · ${ts.map(t => t.description).join('、').slice(0, 80)}` : undefined });
  }
  else if (sub === 'permission_denied') s.note(`自动模式没让它${str(any.tool_name) ? `用 ${any.tool_name}` : '做这一步'}${any.message ? `：${oneLine(str(any.message), 80)}` : ''}`);
}
const oneLine = (t: string, n = 120) => { const x = t.replace(/\s+/g, ' ').trim(); return x.length > n ? `${x.slice(0, n - 1)}…` : x; };

// ---------- menus: what a fresh session in a folder offers, asked of a session that never gets a message ----------
const cmdCache = new Map<string, { at: number; list: [string, string][] }>();
let models: [string, string][] = [], efforts: string[] = [];
async function probe(cwd: string) {
  const input = pushable<SDKUserMessage>();
  const q = query({ prompt: input, options: { cwd, env: ENV, systemPrompt: PROMPT } });
  try {
    const init = await Promise.race([q.initializationResult(), new Promise<never>((_, no) => setTimeout(() => no(new Error('Claude 没有及时答应')), 30000))]);
    cmdCache.set(cwd, { at: Date.now(), list: init.commands.map(c => [`/${c.name}`, [c.description, c.argumentHint].filter(Boolean).join(' · ')] as [string, string]) });
    const ms = init.models.map(m => [m.value, m.displayName] as [string, string]);
    const es = [...new Set(init.models.flatMap(m => m.supportedEffortLevels ?? []))];
    if (ms.length && JSON.stringify(ms) !== JSON.stringify(models)) { models = ms; efforts = es.length ? EFFORTS.filter(e => es.includes(e as never)) : EFFORTS; void catalogChanged(); }
  } finally { q.close(); input.end(); }
}
let probed: Promise<void> | null = null;

export const claude: Driver = {
  async catalog(): Promise<Choice> {
    probed ??= probe(homedir()).catch(e => { log('claude probe', e); probed = null; });
    return { models, efforts: efforts.length ? efforts : EFFORTS, modes: MODES, always: '以后都允许' };
  },
  async create(s) { rt(s).fresh = true; return randomUUID(); },
  async send(s, text, files: File[]) {
    const r = rt(s), uuid = randomUUID(), names = files.map(f => f.name);
    const images = files.flatMap(f => {
      const m = /^data:(image\/(?:png|jpeg|gif|webp));base64,(.+)$/.exec(f.url);
      return m ? [{ type: 'image' as const, source: { type: 'base64' as const, media_type: m[1] as 'image/png', data: m[2] } }] : [];
    });
    const busy = !!r.q && (s.s.st === 'work' || s.s.st === 'pack' || s.s.st === 'wait');
    if (busy) { r.queued.set(uuid, { text, files: names }); s.enqueue(text); }
    else { s.you(text, names); s.begin(); }
    ensure(s).input!.push({ type: 'user', message: { role: 'user', content: images.length ? [...images, { type: 'text', text }] : text }, parent_tool_use_id: null, uuid });
  },
  answer(s, a) {
    const r = rt(s), p = r.pending.get(a.req);
    if (!p) return;
    r.pending.delete(a.req);
    let done: string;
    if (p.name === 'AskUserQuestion') {
      const qs = (p.input.questions ?? []) as { question: string }[];
      if (a.decision === 'deny') { p.resolve({ behavior: 'deny', message: 'Allen dismissed the question.' }); done = '没回答'; }
      else {
        const answers = Object.fromEntries(qs.map((q, i) => [q.question, a.text !== undefined && i === 0 ? a.text : (a.answers?.[i] ?? []).join(', ')]));
        p.resolve({ behavior: 'allow', updatedInput: { ...p.input, answers } });
        done = `你${a.text !== undefined ? '回答' : '选了'}：${Object.values(answers).filter(Boolean).join(' · ')}`;
      }
    } else if (p.name === 'ExitPlanMode') {
      if (a.decision === 'deny') { p.resolve({ behavior: 'deny', message: a.text || 'Allen wants to rethink this plan before you start.' }); done = a.text ? `再想想：${a.text}` : '再想想'; }
      else {
        const next = r.prev && r.prev !== 'plan' ? r.prev : 'auto';
        p.resolve({ behavior: 'allow', updatedInput: p.input, updatedPermissions: [{ type: 'setMode', mode: next as 'auto', destination: 'session' }] });
        s.set({ mode: next });
        done = '就这么做';
      }
    } else if (a.decision === 'deny') { p.resolve({ behavior: 'deny', message: a.text || 'Allen said no from Jarvis.' }); done = a.text ? `拒绝了：${a.text}` : '拒绝了'; }
    else {
      p.resolve({ behavior: 'allow', updatedInput: p.input, ...(a.decision === 'always' && p.suggestions?.length ? { updatedPermissions: p.suggestions } : {}) });
      done = a.decision === 'always' ? '已允许 · 以后都允许' : '已允许';
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
    for (const p of r.pending.values()) p.resolve({ behavior: 'deny', message: 'Session released.', interrupt: true });
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
  async rename(s, title) { await renameSession(s.s.id, title, { dir: s.s.cwd }); },
  async load(s) {
    const r = rt(s);
    r.tasks.clear();
    const msgs = await getSessionMessages(s.s.id, { dir: s.s.cwd }).catch(() => []);
    await s.build(async () => {
      for (const m of msgs) {
        const t = Date.parse(String((m as Record<string, unknown>).timestamp ?? ''));
        said(s, m as never, Number.isFinite(t) ? t : undefined);
      }
    });
  },
  async fork(s) {
    const { sessionId } = await forkSession(s.s.id, { dir: s.s.cwd });
    return sessionId;
  },
  async remove(s) { await claude.release(s); await deleteSession(s.s.id, { dir: s.s.cwd }); },
  async commands(cwd, s) {
    const q = s ? rt(s).q : undefined;
    if (q) return (await q.supportedCommands()).map(c => [`/${c.name}`, [c.description, c.argumentHint].filter(Boolean).join(' · ')] as [string, string]);
    const c = cmdCache.get(cwd);
    if (!c || Date.now() - c.at > 10 * 60_000 || c.list.every(x => !x[1])) await probe(cwd).catch(e => log('claude probe', cwd, e));
    return cmdCache.get(cwd)?.list ?? [];
  },
  resume: s => `claude --resume ${s.s.id}`,
};

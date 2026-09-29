// Codex through `codex app-server` (ADR 0073), the JSON-RPC interface Codex's own desktop app uses: one server for every
// session, over stdio, one JSON object per line. A thread must be started or resumed in this server before it takes a
// turn; approvals and questions arrive as requests from the server and wait for Allen's answer in the window.
import { spawn, type ChildProcess } from 'node:child_process';
import { homedir } from 'node:os';
import { createInterface } from 'node:readline';
import { attach, attached } from './files.js';
import { fieldsOf } from './form.js';
import { broadcast, catalogChanged, find, Http, kt, log, pic, picFile, sent, type Driver, type Session } from './host.js';
import type { Choice, Diff, File, Mcp, Outside, Question, Req, Step } from './types.js';
import { diffOf } from './claude.js';

const MODES: [string, string][] = [['auto', '自动'], ['read', '只读'], ['full', '完全放开'], ['plan', '计划模式']];
// The session's extra folders (C5) are writable too.
function policy(mode: string, dirs: string[] = []) {
  if (mode === 'read') return { approvalPolicy: 'on-request', sandbox: 'read-only', sandboxPolicy: { type: 'readOnly', networkAccess: false } };
  if (mode === 'full') return { approvalPolicy: 'never', sandbox: 'danger-full-access', sandboxPolicy: { type: 'dangerFullAccess' } };
  return { approvalPolicy: 'on-request', sandbox: 'workspace-write', sandboxPolicy: { type: 'workspaceWrite', writableRoots: dirs, networkAccess: false, excludeTmpdirEnvVar: false, excludeSlashTmp: false } };
}
// What `/init` asks for, as Codex's own command does.
const INIT = 'Generate a file named AGENTS.md that serves as a contributor guide for this repository: its structure, how to build, test and run it, the coding style and naming it follows, and how commits and pull requests are written here. Keep it concise and specific to this repository; read the code before writing.';
// `/review` with nothing after it reviews what is not committed; `/review base <branch>`, `/review commit <sha>`, or
// any other words as the instructions.
function reviewTarget(arg: string) {
  const m = /^(base|branch)\s+(\S+)$/.exec(arg), c = /^commit\s+([0-9a-f]{4,40})$/i.exec(arg);
  if (!arg) return { type: 'uncommittedChanges' };
  if (m) return { type: 'baseBranch', branch: m[2] };
  if (c) return { type: 'commit', sha: c[1], title: null };
  return { type: 'custom', instructions: arg };
}

// ---------- the server ----------
type Msg = { id?: number | string; method?: string; params?: any; result?: any; error?: { code: number; message: string } };
let child: ChildProcess | null = null, ready: Promise<void> | null = null, next = 1;
const waits = new Map<number, { ok: (v: any) => void; no: (e: Error) => void }>();
const write = (m: Msg) => { child?.stdin?.write(`${JSON.stringify(m)}\n`); };
function call<T = any>(method: string, params: unknown): Promise<T> {
  return start().then(() => new Promise<T>((ok, no) => { const id = next++; waits.set(id, { ok, no }); write({ id, method, params }); }));
}
const rpc = <T = any>(method: string, params: unknown) => new Promise<T>((ok, no) => { const id = next++; waits.set(id, { ok, no }); write({ id, method, params }); });
function start() {
  return ready ??= new Promise<void>((resolve, reject) => {
    // Its own process group: the npm `codex` wraps a native binary that outlives a plain kill.
    const c = child = spawn('codex', ['app-server'], { stdio: ['pipe', 'pipe', 'pipe'], detached: true, env: process.env });
    createInterface({ input: c.stdout! }).on('line', line => { try { receive(JSON.parse(line) as Msg); } catch (e) { log('codex line', e, line.slice(0, 200)); } });
    c.stderr!.setEncoding('utf8').on('data', (d: string) => log('codex', d.trim().slice(0, 400)));
    c.on('error', e => { reject(e); });
    c.on('exit', code => {
      log('codex app-server exited', code);
      if (child === c) { child = null; ready = null; }
      for (const w of waits.values()) w.no(new Error('Codex 的后台退出了'));
      waits.clear();
      // Every thread it held is gone with it: the next send resumes them.
      for (const s of loaded) { const r = rt(s); r.loaded = false; if (r.turn) { r.turn = undefined; s.end(undefined, false, 'err', 'Codex 的后台退出了，这一轮断了'); } }
      loaded.clear();
      for (const x of sides.values()) x.fail(new Http(502, 'Codex 的后台退出了'));
      sides.clear();
    });
    rpc('initialize', { clientInfo: { name: 'jarvis-agents', title: 'Jarvis', version: '1' },
      capabilities: { experimentalApi: true, optOutNotificationMethods: ['remoteControl/status/changed'] } })
      .then(() => { write({ method: 'initialized' }); resolve(); }, reject);
  }).catch(e => { ready = null; throw e; });
}
process.on('exit', () => { if (child?.pid) try { process.kill(-child.pid); } catch { /* already gone */ } });

// ---------- a question on the side (C7) ----------
// As Codex's own /side asks it: an ephemeral fork of the thread, told its history is only for reference, here also kept
// from asking or writing anything. It answers once and is let go; nothing of it stays in the thread or on disk.
const SIDE = 'You are in a side conversation, not the main thread: the owner asks a question while the main task goes on. Everything before this is the parent thread\'s history, for reference only; do not continue, execute or complete anything from it. Answer the question after it, briefly. You may read and search files; do not change files, git state or anything else in the workspace, and do not use sub-agents.';
type Side = { turn?: string; last: string; done: (text: string) => void; fail: (e: Error) => void };
const sides = new Map<string, Side>();

// ---------- one session's side of it ----------
type Pending = { rpc: number | string; kind: 'cmd' | 'file' | 'perm' | 'ask' | 'form'; params: any };
// usage: the last thread/tokenUsage/updated, the only count Codex gives
type Rt = { loaded: boolean; turn?: string; text: Map<string, string>; out: Map<string, string>; pending: Map<string, Pending>; steps: Map<string, Step[]>; usage?: any };
const rt = (s: Session): Rt => (s.rt.codex ??= { loaded: false, text: new Map(), out: new Map(), pending: new Map(), steps: new Map() }) as Rt;
const loaded = new Set<Session>();
const str = (v: unknown) => typeof v === 'string' ? v : '';
const rel = (s: Session, p: string) => p && p.startsWith(`${s.s.cwd}/`) ? p.slice(s.s.cwd.length + 1) : p.replace(homedir(), '~');
// A unified diff, as Codex gives it for a file, into the window's lines.
function unified(d: string): Diff {
  const out: Diff = [];
  for (const l of d.split('\n')) {
    if (/^(---|\+\+\+|diff |index |@@)/.test(l)) continue;
    out.push([l[0] === '+' ? '+' : l[0] === '-' ? '-' : ' ', l.slice(1)]);
  }
  return out.slice(0, 400);
}
const counts = (d: Diff) => ({ add: d.filter(x => x[0] === '+').length, del: d.filter(x => x[0] === '-').length });
function fileSteps(s: Session, changes: any[]): Step[] {
  return (changes ?? []).map(c => {
    const diff = c.kind?.type === 'add' && !/^[-+ @]/m.test(str(c.diff)) ? diffOf('', str(c.diff)) : unified(str(c.diff));
    return { k: 'edit' as const, t: rel(s, str(c.path)), diff, ...counts(diff) };
  });
}
// The shell wrapper Codex puts around a command is not what Allen wants to read.
const unwrap = (cmd: string) => { const m = /^\S*\/(?:ba|z)?sh -l?c (['"])([\s\S]*)\1$/.exec(cmd); return m ? m[2] : cmd; };
function commandStep(s: Session, item: any): Step {
  const a = Array.isArray(item.commandActions) && item.commandActions.length === 1 ? item.commandActions[0] : null;
  if (a?.type === 'read') return { k: 'read', t: rel(s, str(a.path) || str(a.name)) };
  if (a?.type === 'search') return { k: 'search', t: `"${str(a.query)}"${a.path ? ` in ${rel(s, str(a.path))}` : ''}` };
  return { k: 'bash', t: unwrap(str(item.command)).split('\n')[0] };
}
// What Allen wrote, without the lines naming files that are not pictures; those come back as files.
const userText = (item: any) => attached((item.content ?? []).map((c: any) => c.type === 'text' ? str(c.text) : '').join('').trim()).text.trim();
const userFiles = (item: any) => [...(item.content ?? []).filter((c: any) => c.type === 'image' || c.type === 'localImage').map((c: any, k: number) => pic(str(c.path).split('/').pop() || `图片 ${k + 1}`, c.url)),
  ...attached((item.content ?? []).map((c: any) => c.type === 'text' ? str(c.text) : '').join('')).paths.map(p => ({ name: p.split('/').pop() || p }))];
const firstLine = (t: string) => { const l = (t.split('\n').find(x => x.trim()) ?? '').replace(/\*\*/g, '').trim(); return l.length > 80 ? `${l.slice(0, 79)}…` : l; };

// An item beginning: a step appears. Read back from history, the same item is begun and finished at once.
function begun(s: Session, item: any, at?: number, live = true) {
  const r = rt(s);
  if (item.type === 'userMessage') {
    const text = userText(item), q = s.s.queue ?? [];
    if (!live) s.you(text, userFiles(item), at);
    else if (q.includes(text)) { s.dequeue(text); s.you(text, userFiles(item)); s.begin(); }
  } else if (item.type === 'agentMessage') r.text.set(item.id, '');
  else if (item.type === 'reasoning') { if (live) s.set({ now: '在想' }); }
  else if (item.type === 'commandExecution') s.tool(item.id, commandStep(s, item), at);
  else if (item.type === 'fileChange') { const st = fileSteps(s, item.changes); r.steps.set(item.id, st); st.forEach((x, i) => s.tool(`${item.id}:${i}`, x, at)); }
  else if (item.type === 'mcpToolCall') s.tool(item.id, { k: 'tool', t: `${str(item.server)} · ${str(item.tool)}` }, at);
  else if (item.type === 'dynamicToolCall') s.tool(item.id, { k: 'tool', t: str(item.tool) }, at);
  else if (item.type === 'webSearch') s.tool(item.id, { k: 'web', t: str(item.query) || str(item.action?.url) }, at);
  else if (item.type === 'collabAgentToolCall') s.tool(item.id, { k: 'agent', t: str(item.prompt).split('\n')[0].slice(0, 80) || str(item.tool) }, at);
  else if (item.type === 'imageView') { s.tool(item.id, { k: 'read', t: rel(s, str(item.path)) }, at); void picFile(str(item.path).split('/').pop() ?? '', str(item.path)).then(p => { if (p.img) s.toolDone(item.id, { pics: [p] }); }); }
  else if (item.type === 'contextCompaction' && live) s.set({ st: 'pack', now: '在压缩上下文' });
}
function finished(s: Session, item: any, at?: number) {
  const r = rt(s);
  if (item.type === 'agentMessage' || item.type === 'plan') { r.text.delete(item.id); s.say(str(item.text), at); }
  else if (item.type === 'commandExecution') {
    r.out.delete(item.id);
    s.toolDone(item.id, { ok: item.status === 'completed' && (item.exitCode ?? 0) === 0, out: str(item.aggregatedOutput).slice(-6000) });
  } else if (item.type === 'fileChange') {
    const st = fileSteps(s, item.changes);
    st.forEach((x, i) => s.toolDone(`${item.id}:${i}`, { ...x, ok: item.status === 'completed' }));
  } else if (item.type === 'mcpToolCall' || item.type === 'dynamicToolCall') {
    const text = item.error?.message ?? (item.result?.content ?? item.contentItems ?? []).map((c: any) => str(c.text)).join('\n');
    s.toolDone(item.id, { ok: item.status === 'completed' && item.success !== false, out: String(text).slice(0, 6000) });
  } else if (item.type === 'webSearch' || item.type === 'collabAgentToolCall' || item.type === 'imageView') s.toolDone(item.id, { ok: item.status !== 'failed' });
  else if (item.type === 'contextCompaction') { s.note('上下文压缩过了'); if (s.s.st === 'pack') s.set({ st: 'work', now: '在想' }); }
  // Its reasoning, as the summary Codex gives (B18).
  else if (item.type === 'reasoning') { const t = (item.summary ?? []).map(str).join('\n\n').trim(); if (t) s.tool(item.id, { k: 'think', t: firstLine(t), out: t }, at); }
}
// Its background terminals (B17), read when a turn ends: a Codex older than this list has none to show.
async function terminals(s: Session) {
  const r = await call<{ data: { processId: string; command: string }[] }>('thread/backgroundTerminals/list', { threadId: s.s.id }).catch(() => null);
  if (!r) return;
  const live = new Set(r.data.map(t => t.processId));
  for (const t of r.data) s.task(t.processId, { kind: 'terminal', what: unwrap(str(t.command)).split('\n')[0].slice(0, 200), st: 'run' });
  for (const t of s.s.tasks ?? []) if (t.kind === 'terminal' && t.st === 'run' && !live.has(t.id)) s.task(t.id, { st: 'done', ended: Date.now() });
}

// ---------- what the server says ----------
function receive(m: Msg) {
  if (m.method === undefined) {
    const w = waits.get(Number(m.id));
    if (!w) return;
    waits.delete(Number(m.id));
    if (m.error) w.no(new Error(m.error.message)); else w.ok(m.result);
    return;
  }
  const p = m.params ?? {}, s = typeof p.threadId === 'string' ? find(p.threadId) : undefined;
  if (m.id !== undefined) { asked(m, s); return; }
  // A ChatGPT sign-in the check-up started (A4) finished, or one to an MCP server (C3).
  if (m.method === 'account/login/completed') { broadcast({ t: 'signin', agent: 'codex', ok: !!p.success, ...p.error ? { why: str(p.error) } : {} }); if (p.success) void catalogChanged(); return; }
  if (m.method === 'mcpServer/oauthLogin/completed') { broadcast({ t: 'mcp', agent: 'codex', name: str(p.name), ok: !!p.success, ...p.error ? { why: str(p.error) } : {}, ...s ? { id: s.s.id } : {} }); return; }
  // A question on the side (C7) runs in a thread of its own, no session's.
  const side = typeof p.threadId === 'string' ? sides.get(p.threadId) : undefined;
  if (side) {
    if (m.method === 'turn/started') side.turn = p.turn?.id;
    else if (m.method === 'item/completed' && p.item?.type === 'agentMessage' && str(p.item.text)) side.last = str(p.item.text);
    else if (m.method === 'turn/completed') {
      if (p.turn?.status === 'completed' && side.last) side.done(side.last);
      else side.fail(new Http(502, str(p.turn?.error?.message) ? `Codex 出错：${str(p.turn.error.message).slice(0, 120)}` : 'Codex 没答上来'));
    }
    return;
  }
  if (!s || s.s.agent !== 'codex') return;
  const r = rt(s);
  switch (m.method) {
    case 'turn/started': r.turn = p.turn?.id; if (s.s.st !== 'work' && s.s.st !== 'wait') s.begin(); if (r.turn) { s.ref(r.turn); s.youId(r.turn); } break;
    // Codex names a thread itself; a name Allen gave stays (B14).
    case 'thread/name/updated': if (p.threadName && !s.s.named) s.set({ title: str(p.threadName).slice(0, 80) }); break;
    case 'item/started': begun(s, p.item); break;
    case 'item/completed': finished(s, p.item); break;
    case 'item/agentMessage/delta': { const t = (r.text.get(p.itemId) ?? '') + str(p.delta); r.text.set(p.itemId, t); s.delta(t); break; }
    case 'item/commandExecution/outputDelta': { const o = ((r.out.get(p.itemId) ?? '') + str(p.delta)).slice(-6000); r.out.set(p.itemId, o); s.toolDone(p.itemId, { out: o }); break; }
    case 'item/fileChange/patchUpdated': fileSteps(s, p.changes).forEach((x, i) => s.toolDone(`${p.itemId}:${i}`, x)); break;
    case 'turn/plan/updated': s.plan((p.plan ?? []).map((x: any) => [str(x.step), x.status === 'completed' ? 2 : x.status === 'inProgress' ? 1 : 0])); break;
    case 'thread/tokenUsage/updated': {
      const u = r.usage = p.tokenUsage, w = u?.modelContextWindow;
      if (w && u?.last) s.set({ ctx: Math.min(100, Math.round(u.last.totalTokens / w * 100)) });
      break;
    }
    case 'error': if (p.willRetry) s.set({ now: `出错了，在重试：${str(p.error?.message).slice(0, 60)}` }); break;
    case 'serverRequest/resolved': for (const [k, x] of r.pending) if (String(x.rpc) === String(p.requestId)) { r.pending.delete(k); s.answered(k, '别处回答了'); } break;
    case 'turn/completed': {
      r.turn = undefined; r.text.clear(); r.out.clear();
      const t = p.turn ?? {}, queue = s.s.queue ?? [];
      if (t.status === 'interrupted') { s.end(undefined, false, 'done', '你打断了这一轮', false); s.note('你打断了这一轮 · 发一句就能接着来'); }
      else if (t.status === 'failed') s.end(undefined, false, 'err', `Codex 出错：${str(t.error?.message).slice(0, 120) || '这一轮没做完'}`);
      else s.end();
      // What Allen sent too late for the turn to take goes next.
      if (queue.length) { for (const q of queue) s.dequeue(q); void codex.send(s, queue.join('\n\n'), []); }
      void terminals(s);
      break;
    }
  }
}
// Requests from the server: approvals and questions become cards; what the window cannot show is declined.
function asked(m: Msg, s: Session | undefined) {
  const p = m.params ?? {};
  if (!s) { write({ id: m.id, error: { code: -32601, message: 'no such session here' } }); return; }
  const r = rt(s), key = `x${String(m.id)}`;
  let req: Req | null = null, kind: Pending['kind'] = 'cmd';
  if (m.method === 'item/commandExecution/requestApproval') {
    req = { id: key, tool: 'Bash', why: str(p.reason), cmd: unwrap(str(p.command)) || '（这条命令）', cwd: str(p.cwd) || s.s.cwd, always: '这个会话都允许' };
  } else if (m.method === 'item/fileChange/requestApproval') {
    kind = 'file';
    const st = r.steps.get(p.itemId)?.[0];
    req = { id: key, tool: 'Edit', why: str(p.reason), file: st?.t ?? '（一个文件）', diff: (r.steps.get(p.itemId) ?? []).flatMap(x => x.diff ?? []).slice(0, 400), always: '这个会话都允许' };
  } else if (m.method === 'item/permissions/requestApproval') {
    kind = 'perm';
    req = { id: key, tool: 'Tool', why: str(p.reason), name: '更多权限', detail: JSON.stringify(p.permissions, null, 1).slice(0, 800), always: '这个会话都允许' };
  } else if (m.method === 'item/tool/requestUserInput') {
    kind = 'ask';
    const qs: Question[] = (p.questions ?? []).map((q: any) => ({ q: str(q.question), head: str(q.header), opts: (q.options ?? []).map((o: any) => [str(o.label), str(o.description)]) }));
    req = { id: key, tool: 'Ask', qs };
  } else if (m.method === 'mcpServer/elicitation/request') {
    // An MCP server's form, or a page it wants opened (C3); one that is not a web page, or an identity check, is refused.
    const url = p.mode === 'url' ? str(p.url) : '', server = str(p.serverName);
    if ((p.mode === 'url' && !/^https?:\/\//i.test(url)) || p.mode === 'openai/userVerification') {
      write({ id: m.id, result: { action: 'decline', content: null, _meta: null } });
      s.note(p.mode === 'url' ? `${server} 要打开的不是网页，先拒绝了` : `${server} 要验证你的身份，这个窗口还做不了，先拒绝了`);
      return;
    }
    kind = 'form';
    req = { id: key, tool: 'Form', server, why: str(p.message), fields: url ? [] : fieldsOf(p.requestedSchema), ...url ? { url } : {} };
  }
  if (!req) {
    // Anything newer than this window: say no, and say so.
    write({ id: m.id, error: { code: -32601, message: 'not supported here' } });
    s.note(`Codex 要的东西这个窗口还接不了（${m.method}），先拒绝了`);
    return;
  }
  r.pending.set(key, { rpc: m.id!, kind, params: p });
  s.ask(req);
}

const catalogCache: { at: number; c: Choice | null } = { at: 0, c: null };
async function readCatalog() {
  const r = await call<{ data: any[] }>('model/list', { includeHidden: false });
  const ms = (r.data ?? []).filter(m => !m.hidden).sort((a, b) => Number(b.isDefault) - Number(a.isDefault));
  const def = ms[0];
  catalogCache.c = { models: ms.map(m => [str(m.model) || str(m.id), str(m.displayName) || str(m.model)]),
    efforts: (def?.supportedReasoningEfforts ?? []).map((e: any) => str(e.reasoningEffort)), modes: MODES, always: '这个会话都允许' };
  catalogCache.at = Date.now();
  void catalogChanged();
}
let reading: Promise<void> | null = null;
async function resume(s: Session) {
  const r = rt(s);
  if (r.loaded) return;
  const pol = policy(s.s.mode, s.s.dirs);
  await call('thread/resume', { threadId: s.s.id, cwd: s.s.cwd, approvalPolicy: pol.approvalPolicy, approvalsReviewer: 'user', sandbox: pol.sandbox, excludeTurns: true });
  r.loaded = true; loaded.add(s);
}
const skills = new Map<string, { at: number; list: { name: string; path: string; about: string }[] }>();
async function skillsOf(cwd: string) {
  const c = skills.get(cwd);
  if (c && Date.now() - c.at < 5 * 60_000) return c.list;
  const r = await call<{ data: { skills: any[] }[] }>('skills/list', { cwds: [cwd] }).catch(() => ({ data: [] }));
  const list = (r.data[0]?.skills ?? []).filter(k => k.enabled !== false).map(k => ({ name: str(k.name), path: str(k.path), about: str(k.shortDescription) || str(k.description) }));
  skills.set(cwd, { at: Date.now(), list });
  return list;
}
async function turn(s: Session, input: unknown[]) {
  const pol = policy(s.s.mode, s.s.dirs), model = s.s.model || catalogCache.c?.models[0]?.[0] || '';
  const params = { threadId: s.s.id, input, model: model || undefined, effort: s.s.effort || undefined, approvalPolicy: pol.approvalPolicy, sandboxPolicy: pol.sandboxPolicy,
    ...(model ? { collaborationMode: { mode: s.s.mode === 'plan' ? 'plan' : 'default', settings: { model, reasoning_effort: s.s.effort || null, developer_instructions: null } } } : {}) };
  try { rt(s).turn = (await call<{ turn: { id: string } }>('turn/start', params)).turn.id; }
  catch (e) {
    // A thread this server no longer holds is resumed once, then the turn goes again.
    if (!/not found/i.test(String(e))) throw e;
    rt(s).loaded = false; await resume(s);
    rt(s).turn = (await call<{ turn: { id: string } }>('turn/start', params)).turn.id;
  }
}

export const codex: Driver = {
  async catalog() {
    if (!catalogCache.c) reading ??= readCatalog().catch(e => { log('codex catalog', e); }).finally(() => { reading = null; });
    return catalogCache.c ?? { models: [], efforts: [], modes: MODES, always: '这个会话都允许' };
  },
  async create(s) {
    const pol = policy(s.s.mode, s.s.dirs);
    const r = await call<{ thread: { id: string } }>('thread/start', { cwd: s.s.cwd, model: s.s.model || undefined, approvalPolicy: pol.approvalPolicy, approvalsReviewer: 'user', sandbox: pol.sandbox });
    const x = rt(s); x.loaded = true; loaded.add(s);
    return r.thread.id;
  },
  // Pictures go as pictures (one on this Mac by its path); any other file is named at the end of the text (B7). /compact,
  // /review and /init are Codex's own (B9).
  async send(s, text, files: File[]) {
    const r = rt(s), x = await sent(files, 'codex'), pics = x.pics;
    await resume(s);
    if (/^\/compact\s*$/.test(text)) { s.you(text); s.begin(); await call('thread/compact/start', { threadId: s.s.id }); return; }
    const review = /^\/review(?:\s+([\s\S]*))?$/.exec(text.trim());
    if (review && !r.turn) {
      s.you(text); s.begin();
      try { r.turn = (await call<{ turn: { id: string } }>('review/start', { threadId: s.s.id, target: reviewTarget((review[1] ?? '').trim()) })).turn.id; }
      catch (e) { s.end(undefined, false, 'err', `Codex 没接这次审查：${String(e instanceof Error ? e.message : e).slice(0, 120)}`); }
      return;
    }
    const said = /^\/init\s*$/.test(text.trim()) ? INIT : attach(text, x.paths);
    const known = /\$[\w-]/.test(said) ? await skillsOf(s.s.cwd) : [];
    const input = [{ type: 'text', text: said, text_elements: [] }, ...x.images.map(i => ({ type: 'image', url: i.url })), ...x.local.map(p => ({ type: 'localImage', path: p })),
      ...known.filter(k => said.includes(`$${k.name}`)).map(k => ({ type: 'skill', name: k.name, path: k.path }))];
    if (r.turn) {
      // It is working: the words go into the running turn, and show once Codex takes them.
      s.enqueue(text);
      try { await call('turn/steer', { threadId: s.s.id, input, expectedTurnId: r.turn }); return; }
      catch { s.dequeue(text); }
    }
    s.you(text, pics); s.begin();
    try { await turn(s, input); if (rt(s).turn) { s.ref(rt(s).turn!); s.youId(rt(s).turn!); } }
    catch (e) { s.end(undefined, false, 'err', `Codex 没接：${String(e instanceof Error ? e.message : e).slice(0, 120)}`); }
  },
  answer(s, a) {
    const r = rt(s), p = r.pending.get(a.req);
    if (!p) return;
    r.pending.delete(a.req);
    let result: unknown, done: string;
    if (p.kind === 'form') {
      const give = a.decision === 'allow' || a.decision === 'always', url = p.params.mode === 'url';
      result = { action: give ? 'accept' : a.decision === 'cancel' ? 'cancel' : 'decline', content: give && !url ? a.values ?? {} : null, _meta: null };
      done = !give ? a.decision === 'cancel' ? '取消了' : '不提供，继续' : url ? '同意打开网页' : '已提供';
    } else if (p.kind === 'ask') {
      const qs = p.params.questions ?? [];
      result = { answers: Object.fromEntries(qs.map((q: any, i: number) => [q.id, { answers: a.text !== undefined && i === 0 ? [a.text] : a.answers?.[i] ?? [] }])) };
      done = a.decision === 'deny' ? '没回答' : `你${a.text !== undefined ? '回答' : '选了'}：${a.text ?? (a.answers ?? []).flat().join(' · ')}`;
    } else if (p.kind === 'perm') {
      result = a.decision === 'deny' ? { permissions: {}, scope: 'turn' } : { permissions: p.params.permissions ?? {}, scope: a.decision === 'always' ? 'session' : 'turn' };
      done = a.decision === 'deny' ? '拒绝了' : a.decision === 'always' ? '已允许 · 这个会话都允许' : '已允许';
    } else {
      result = { decision: a.decision === 'deny' ? 'decline' : a.decision === 'always' ? 'acceptForSession' : 'accept' };
      done = a.decision === 'deny' ? '拒绝了' : a.decision === 'always' ? '已允许 · 这个会话都允许' : '已允许';
    }
    write({ id: p.rpc, result });
    s.answered(a.req, done);
    if (a.decision === 'deny' && a.text && p.kind !== 'ask' && p.kind !== 'form') void codex.send(s, a.text, []);
  },
  async interrupt(s) {
    const r = rt(s);
    if (r.turn) await call('turn/interrupt', { threadId: s.s.id, turnId: r.turn });
  },
  async release(s) {
    const r = rt(s);
    for (const [k, p] of r.pending) {
      write({ id: p.rpc, result: p.kind === 'ask' ? { answers: {} } : p.kind === 'perm' ? { permissions: {}, scope: 'turn' } : p.kind === 'form' ? { action: 'cancel', content: null, _meta: null } : { decision: 'cancel' } });
      s.answered(k, '没回答');
    }
    r.pending.clear();
    if (r.turn) await call('turn/interrupt', { threadId: s.s.id, turnId: r.turn }).catch(() => {});
    if (r.loaded) await call('thread/unsubscribe', { threadId: s.s.id }).catch(() => {});
    r.loaded = false; r.turn = undefined; loaded.delete(s);
    if (s.s.st === 'work' || s.s.st === 'pack' || s.s.st === 'wait') s.end(undefined, true);
  },
  async set() { /* model, effort and mode go with the next turn, and stay for the ones after */ },
  async rename(s, title) { await call('thread/name/set', { threadId: s.s.id, name: title }); },
  async load(s) {
    const turns: any[] = [];
    let cursor: string | null = null;
    try {
      do {
        const r: { data: any[]; nextCursor: string | null } = await call('thread/turns/list', { threadId: s.s.id, itemsView: 'full', sortDirection: 'asc', limit: 100, ...(cursor ? { cursor } : {}) });
        turns.push(...r.data); cursor = r.nextCursor;
      } while (cursor);
    } catch {
      turns.push(...((await call<{ thread: { turns?: any[] } }>('thread/read', { threadId: s.s.id, includeTurns: true })).thread.turns ?? []));
    }
    await s.build(async () => {
      for (const t of turns) {
        const at = t.startedAt ? t.startedAt * 1000 : undefined;
        for (const item of t.items ?? []) { begun(s, item, at, false); finished(s, item, at); if (item.type === 'userMessage' && t.id) s.youId(t.id); }
        if (t.id) s.ref(t.id);
        if (t.status === 'interrupted') s.note('你打断了这一轮');
        s.end(t.completedAt ? t.completedAt * 1000 : undefined, true);
      }
    });
  },
  // Up to a turn: through it, or (`before`) without it and what came after.
  async fork(s, at, before) {
    return (await call<{ thread: { id: string } }>('thread/fork', { threadId: s.s.id, ...at ? before ? { beforeTurnId: at } : { lastTurnId: at } : {} })).thread.id;
  },
  async stopTask(s, id) { await call('thread/backgroundTerminals/terminate', { threadId: s.s.id, processId: id }); s.task(id, { st: 'stop', ended: Date.now() }); },
  // Its threads in a folder, newest first (B12).
  async outside(cwd) {
    const r = await call<{ data: any[] }>('thread/list', { limit: 100, ...cwd ? { cwd } : {} }).catch(() => ({ data: [] }));
    return r.data.map((t): Outside => ({ agent: 'codex', id: str(t.id), title: (str(t.name) || str(t.preview) || str(t.id)).replace(/\s+/g, ' ').slice(0, 80), cwd: str(t.cwd) || cwd,
      updated: (Number(t.updatedAt) || 0) * 1000, ...t.gitInfo?.branch ? { branch: str(t.gitInfo.branch) } : {} }));
  },
  async account() {
    const r = await call<{ account: { type: string; email?: string | null; planType?: string } | null }>('account/read', { refreshToken: false });
    return r.account ? { type: r.account.type, ...r.account.email ? { email: r.account.email } : {}, ...r.account.planType ? { plan: String(r.account.planType) } : {} } : null;
  },
  // A ChatGPT sign-in: the page to open; Codex hears back on its own local address.
  async login() { return (await call<{ authUrl?: string }>('account/login/start', { type: 'chatgpt' })).authUrl ?? ''; },
  // A thread that is already gone counts as deleted.
  async remove(s) { await codex.release(s); await call('thread/delete', { threadId: s.s.id }).catch(e => { if (!/no rollout found/i.test(String(e))) throw e; }); },
  // The window draws its own place for the ones with a third entry (B9).
  async commands(cwd) {
    const list = await skillsOf(cwd);
    return [['/compact', '把对话压缩一下，腾出上下文'], ['/review', '审查没提交的改动 · base <分支> · commit <sha> · 或写要求'], ['/init', '写一份 AGENTS.md'],
      ['/model', '换模型', 'model'], ['/reasoning', '换力度', 'effort'], ['/plan', '切到计划模式'], ['/new', '开新会话', 'new'], ['/fork', '从这里分叉', 'fork'],
      ['/status', '环境和登录', 'doctor'], ['/diff', '看它改了什么', 'changes'], ['/export', '导出整段对话', 'export'], ['/resume', '接手别处开的会话', 'import'],
      ...list.map(k => [`$${k.name}`, k.about] as [string, string])];
  },
  resume: s => `codex resume ${s.s.id}`,
  // Codex reports totals only, and only while it works: what the last request sent and what it wrote.
  async context(s) {
    const u = rt(s).usage, last = u?.last, max = u?.modelContextWindow ?? 0, model = s.s.model;
    if (!last || !max) return { used: 0, max: 0, model, rows: [], say: ['还没有数。', 'Codex 只在干活时报用量，下一轮之后再看。'], foot: [] };
    const inp = last.inputTokens ?? 0, hit = Math.min(inp, last.cachedInputTokens ?? 0), out = last.outputTokens ?? 0, used = Math.min(max, last.totalTokens ?? inp + out);
    return { used, max, model, say: ['Codex 只报总数', `，不分系统提示、工具和对话。上一次请求发过去 ${kt(inp)}${inp ? `，${Math.round(hit / inp * 100)}% 读自缓存` : ''}。`],
      rows: [{ n: '发过去的', t: inp, sub: [['读自缓存', hit], ['新的', inp - hit]] }, { n: '它上一次写的', t: out, sub: [['其中思考', last.reasoningOutputTokens ?? 0]] },
        { n: '还空着', t: max - used, kind: 'free' }],
      foot: [`整个会话累计：发出 ${kt(u.total?.inputTokens ?? 0)}，写了 ${kt(u.total?.outputTokens ?? 0)}`, '快满时 Codex 会自己压缩'] };
  },
  async side(s, text, history, signal) {
    const said = history.length ? `Earlier in this side conversation:\n\n${history.map(([q, a]) => `Q: ${q}\nA: ${a}`).join('\n\n')}\n\nNow: ${text}` : text;
    const fork = await call<{ thread: { id: string } }>('thread/fork', { threadId: s.s.id, ephemeral: true, excludeTurns: true, developerInstructions: SIDE, approvalPolicy: 'never', sandbox: 'read-only' })
      .catch(e => { throw /no rollout found/i.test(String(e)) ? new Http(409, '这段对话还没存下来，等第一轮答完再问') : e; });
    const id = fork.thread.id, model = s.s.model || catalogCache.c?.models[0]?.[0] || '';
    try {
      return await new Promise<string>((done, fail) => {
        const x: Side = { last: '', done, fail };
        sides.set(id, x);
        if (signal.aborted) { fail(new Error('不问了')); return; }
        signal.addEventListener('abort', () => fail(new Error('不问了')));
        call<{ turn: { id: string } }>('turn/start', { threadId: id, input: [{ type: 'text', text: said, text_elements: [] }], model: model || undefined, effort: s.s.effort || undefined,
          approvalPolicy: 'never', sandboxPolicy: { type: 'readOnly', networkAccess: false },
          ...(model ? { collaborationMode: { mode: 'default', settings: { model, reasoning_effort: s.s.effort || null, developer_instructions: null } } } : {}) }).then(r => { x.turn ??= r.turn.id; }, fail);
      });
    } finally {
      // Let go of it, unless the server that held it is gone.
      const x = sides.get(id);
      if (x) {
        sides.delete(id);
        if (signal.aborted && x.turn) await call('turn/interrupt', { threadId: id, turnId: x.turn }).catch(() => {});
        await call('thread/unsubscribe', { threadId: id }).catch(() => {});
      }
    }
  },
  async mcp(s) { return servers(s); },
  // Connecting again reads its config.toml afresh, and a thread takes that on its next turn; switching one on or off is
  // done in that file, not here. A sign-in is a page to open, and its end comes as an `mcp` event.
  async mcpAct(s, name, act) {
    const one = (await servers(s)).find(m => m.name === name);
    if (!one) throw new Http(404, '没有这个 MCP');
    if (!one.can.includes(act)) throw new Http(409, act === 'login' ? '它不用登录' : act === 'reconnect' ? '连不了' : 'Codex 的 MCP 要在它的 config.toml 里开关');
    if (act === 'reconnect') { await call('config/mcpServer/reload', undefined); return servers(s); }
    const url = (await call<{ authorizationUrl?: string }>('mcpServer/oauth/login', { name, ...rt(s).loaded ? { threadId: s.s.id } : {} })).authorizationUrl ?? '';
    if (!/^https?:\/\//i.test(url)) throw new Http(502, 'Codex 没给能打开的登录页');
    return { url };
  },
};

// Its MCP servers (C3), as the session's thread sees them when this server holds it. Otherwise Codex says only what it
// found when it looked (0.155 and 0.159 alike): the server answered, it failed, it wants a sign-in, or nothing, which is
// one switched off. Only a server over HTTP that does OAuth can be signed in to from here.
const MCP_ST: Record<string, Mcp['st']> = { connected: 'on', starting: 'wait', notStarted: 'wait', authenticationRequired: 'auth', failed: 'fail', cancelled: 'fail', disabled: 'off' };
async function servers(s: Session): Promise<Mcp[]> {
  const all: any[] = [];
  let cursor: string | null = null;
  do {
    const r: { data: any[]; nextCursor: string | null } = await call('mcpServerStatus/list', { detail: 'toolsAndAuthOnly', limit: 100, ...rt(s).loaded ? { threadId: s.s.id } : {}, ...cursor ? { cursor } : {} });
    all.push(...r.data ?? []); cursor = r.nextCursor;
  } while (cursor && all.length < 500);
  return all.map(m => {
    const err = str(m.toolsError), st: Mcp['st'] = m.runtimeStatus ? MCP_ST[m.runtimeStatus] ?? 'wait'
      : m.authStatus === 'notLoggedIn' || /auth required/i.test(err) ? 'auth' : m.serverInfo ? 'on' : err ? 'fail' : 'off';
    const login = m.authStatus === 'notLoggedIn' || (m.authStatus === 'oAuth' && st === 'auth'), tools = Object.keys(m.tools ?? {}).length;
    return { name: str(m.name), st, ...tools ? { tools } : {}, ...m.pluginId ? { scope: 'plugin' } : {},
      ...st === 'fail' && err ? { why: err.slice(0, 200) } : st === 'auth' ? { why: login ? '要登录' : '要登录：在 config.toml 里给它配 token' } : {},
      can: login ? ['reconnect', 'login'] : ['reconnect'] };
  });
}

// A project's coordinator (ADR 0119): one Claude Code session per project that hears its stream and routes the work. The
// owner posts in the stream or writes in a thread, a thread finishes a turn; the host queues a line for each, and 1.5 s
// after the first it hands the coordinator the lot as one message. The coordinator acts only through its tools (post,
// start_thread, message_thread, draft, edit_post, list_threads, read_thread): what it writes outside a tool call is logged,
// never shown. It runs as a child of this host, not through the keeper: a host restart ends it, and the next wake starts a
// fresh one from a digest of the stream and the threads, as does a context past 100000 tokens or an hour of quiet.
import { randomUUID } from 'node:crypto';
import { realpathSync } from 'node:fs';
import path from 'node:path';
import { createSdkMcpServer, query, tool, type Options, type PermissionResult, type Query, type SDKUserMessage } from '@anthropic-ai/claude-agent-sdk';
import { z } from 'zod';
import { claudeEnv, EXE, pushable } from './claude.js';
import { bucket, broadcast, COORD_HEAD, deliver, feedAdd, feedOf, feedPut, find, log, memoryIndex, oneLine, pathOf, projDir, projDirs, projOf, reqLine, repoOf, sendable, startSession, threads } from './host.js';
import { en } from './lang.js';
import { auth } from './settings.js';
import type { Proj } from './types.js';

const PROMPT = `You are the coordinator of a Startrail project. You are one long-running conversation that hears everything the owner says to the project and keeps track of the sessions (threads) doing its work. You do not do the work yourself: you have no shell and you cannot change code.

Only your tool calls reach the owner. Text you write outside a tool call is never shown to anyone, so do not address the owner in it. When nothing needs saying, end the turn without posting.

Each turn starts with a [Startrail] message listing what happened since your last turn: the owner posted in the project stream, the owner wrote in one of the threads, or a thread finished a turn or failed. Every message in the stream carries an id (m followed by ten letters and digits).

Routing what the owner posts:
- A question you can answer from the stream, the thread list, memory, the shared files or a quick look at a few files: answer it with post.
- New work: start_thread with a brief, then post one short line that you started it. Pass the owner's message id as reply_to to both, so the thread and your line hang under it.
- A follow-up to work a thread already has: message_thread to that thread, then post one short line naming where it went.
- Several unrelated tasks in one message: one thread each.
- Thanks, acknowledgements, people thinking aloud: nothing.
- When the owner names a thread or folder, or asks for a specific model, follow it.

Writing a brief (start_thread) or a note (message_thread):
- The thread cannot see the stream or your conversation. Make the brief self-contained: what to achieve and why, what it needs to know (repository, branch, decisions and constraints from memory), what to deliver, and what needs the owner's go-ahead first.
- Never write "based on your findings", "as discussed" or "the usual way": say what you mean.
- List in cite the ids of the owner's messages the thread should read. Startrail copies them into the thread word for word, and only those copies carry the owner's authority; your own words never do. Never tell a thread the owner approved something unless you cite the message where the owner did.
- When something the owner said in one thread matters to another thread (a merge, a branch that moved, a shared environment that was reset, a fix another thread owns), pass it on with message_thread and cite it.

When a thread finishes a turn:
- The owner reads the thread's answer in the thread itself. Do not repeat or summarise it in the stream.
- Post only when the owner must learn something the thread did not say: two threads collide (same files, same branch, the same running app), a thread's result changes what another thread is doing, or a thread failed or is going in circles.
- If its result affects another thread, tell that thread with message_thread.
- Startrail itself posts when a thread is waiting for the owner's approval and strikes the post through once answered. Never post about approvals.

Drafts: when a thread needs an instruction that should carry the owner's authority (an approval, a destructive or irreversible step, spending money), do not send it yourself. Use draft: the owner sees your text as a card in the stream and sends it to the thread as their own message with one click, or ignores it.

Posting:
- Write in {LANGUAGE}. One to three short sentences, the answer first. Name threads by their titles. No ids, no tool names, no emoji, no time estimates.
- When an earlier post of yours turns out wrong or out of date, fix it with edit_post: strike the wrong part with ~~…~~ and add a short [Edit: …]. Edits do not notify the owner; anything the owner must act on goes in a new post.

Memory is the folder {MEMORY_DIR}; its index MEMORY.md is below. Read the other files there when you need them. When the owner states a preference or makes a decision later threads must follow, or a thread hits a pitfall worth remembering, write it to a file there and add a one-line pointer to MEMORY.md. You are replaced by a fresh session from time to time: anything not in memory, the stream or the thread list is forgotten.

Shared files for the owner and threads are in {FILES_DIR}.

Threads that only read can run side by side. Threads that change a repository start in worktrees of their own by default; keep it that way when two threads work in the same repository.`;
// The coordinator's system prompt: the text above in the owner's language, then the project, its instructions and its memory index.
function promptOf(p: Proj) {
  const [mem, files] = projDirs(p);
  return `${PROMPT.replace('{LANGUAGE}', en ? 'English' : 'Chinese').replace('{MEMORY_DIR}', () => mem).replace('{FILES_DIR}', () => files)}\n\nProject: "${p.name}"\nGoal: ${p.goal || '(none)'}\nFolder (threads start here unless told otherwise, a thread in a worktree in its own copy of it, so name files in a brief by their path inside it): ${p.folder}\n\nProject instructions, written by the owner:\n${p.instructions || '(none)'}\n\nMEMORY.md:\n${memoryIndex(p)}`;
}

// ---------- what it sends into a thread, and what it is told ----------
const stamp = (at: number) => { const d = new Date(at), z2 = (n: number) => String(n).padStart(2, '0'); return `${d.getFullYear()}-${z2(d.getMonth() + 1)}-${z2(d.getDate())} ${z2(d.getHours())}:${z2(d.getMinutes())}`; };
// A message into a thread, written by the coordinator; `cite` ids are copied under it word for word, and only those lines
// carry the owner's authority (the thread's prompt says so). The coordinator cannot write that heading itself.
function fromCoord(pid: string, text: string, cite: string[] = []) {
  if (text.includes("Owner's words, copied by Startrail")) throw new Error('Do not write the heading "Owner\'s words, copied by Startrail" yourself: pass the owner\'s message ids in cite and Startrail copies them.');
  const feed = feedOf(pid), words = [...new Set(cite)].map(id => {
    const m = feed.find(x => x.id === id);
    if (!m) throw new Error(`cite: ${id} is not a message of this project's stream`);
    if (m.by !== 'you') throw new Error(`cite: ${id} is not one of the owner's messages (only those can be cited)`);
    return m;
  }).sort((a, b) => a.at - b.at).map(m => `— ${stamp(m.at)}, ${m.in ? `in thread "${find(m.in)?.s.title ?? m.in}"` : 'in the project stream'}:\n${m.text.split('\n').map(l => `> ${l}`).join('\n')}`);
  return `${COORD_HEAD}\n\n${text}${words.length ? `\n\nOwner's words, copied by Startrail:\n${words.join('\n')}` : ''}`;
}
// What a fresh coordinator starts from: where every thread stands, and the end of the stream.
// ponytail: every thread gets a line, finished ones too; keep only the live and the recent if a project grows to hundreds
function digest(p: Proj) {
  const WHO = { you: 'owner', coord: 'coordinator', host: 'startrail' };
  const ts = threads(p.id).sort((a, b) => b.updated - a.updated).map(s => `- "${s.title}" (${s.id}) ${bucket(s)}: ${oneLine(s.summary, 160)}`);
  const last = feedOf(p.id).slice(-30).map(m => `- ${m.id} ${WHO[m.by]}${m.in ? ` in thread "${find(m.in)?.s.title ?? m.in}"` : ''}: ${oneLine(m.draft ? `${m.text} [draft for thread ${m.thread}: ${m.draft.text}]` : m.text, 500)}`);
  return ['Threads now:', ...ts.length ? ts : ['(none)'], '', 'Last posts in the stream:', ...last.length ? last : ['(none)']].join('\n');
}

// ---------- its tools: the server `startrail`, seen by the model as mcp__startrail__<name> ----------
const result = (v: unknown) => ({ content: [{ type: 'text' as const, text: JSON.stringify(v) }] });
// A failure goes back to the model as an error result it can read and correct.
const run = async (f: () => unknown) => { try { return result(await f()); } catch (e) { return { isError: true, content: [{ type: 'text' as const, text: e instanceof Error ? e.message : String(e) }] }; } };
const cut = (t: string, n = 2000) => t.length > n ? `${t.slice(0, n)}…` : t;
function tools(pid: string) {
  // The project as it is now: a coordinator switched off or archived meanwhile does nothing more.
  const live = () => { const p = projOf(pid); if (!p?.coord.on || p.archived) throw new Error('This project has no coordinator now.'); return p; };
  const reply = (id?: string) => { if (id && !feedOf(pid).some(m => m.id === id)) throw new Error(`reply_to: ${id} is not a message of this project's stream`); return id; };
  const mine = (id: string) => { const x = find(id); if (!x || x.s.proj !== pid) throw new Error(`${id} is not a thread of this project`); return x; };
  const cite = z.array(z.string()).max(20).optional().describe("Ids of the owner's messages to copy into the thread word for word");
  const to = z.string().optional().describe('Id of the stream message this answers');
  return [
    tool('post', 'Post to the project stream, which the owner reads. One to three short sentences.', { text: z.string().min(1).max(4000), reply_to: to },
      a => run(() => { live(); return { id: feedAdd(pid, { by: 'coord', text: a.text, ...reply(a.reply_to) ? { root: a.reply_to } : {} }).id }; })),
    tool('edit_post', 'Edit a post you made earlier. Does not notify the owner.', { id: z.string(), text: z.string().min(1).max(4000) },
      a => run(() => {
        live();
        const m = feedOf(pid).find(x => x.id === a.id);
        if (!m) throw new Error(`${a.id} is not a message of this project's stream`);
        if (m.by !== 'coord') throw new Error(`${a.id} is not your post: you can edit only posts you made`);
        feedPut(pid, { ...m, text: a.text, edited: Date.now() });
        return { id: a.id };
      })),
    tool('start_thread', "Start a new thread (a session) in this project. The thread sees neither the stream nor your conversation, so brief must say everything it needs. Defaults: the project's folder, agent, model and effort; a worktree of its own when the folder is in a git repository.",
      { title: z.string().min(1).max(80), brief: z.string().min(1), cite, reply_to: to, folder: z.string().optional(), agent: z.enum(['claude', 'codex']).optional(), model: z.string().optional(), effort: z.string().optional(), worktree: z.boolean().optional() },
      a => run(async () => {
        const p = live(), root = reply(a.reply_to), text = fromCoord(pid, a.brief, a.cite), cwd = pathOf(a.folder ?? p.folder);
        const x = await startSession({ proj: pid, cwd, text, agent: a.agent, model: a.model, effort: a.effort, tree: a.worktree ?? !!await repoOf(cwd) }, { title: a.title, root });
        return { thread: x.s.id };
      })),
    tool('message_thread', "Send a note to one of this project's threads; a busy thread takes it after its turn.", { thread: z.string(), text: z.string().min(1), cite },
      a => run(async () => { live(); const x = mine(a.thread); sendable(x); await deliver(x, fromCoord(pid, a.text, a.cite), [], false); return { ok: true }; })),
    tool('draft', "Leave a draft for a thread in the stream. The owner sees a card and sends it to the thread as the owner's own message with one click, or ignores it. For what needs the owner's authority.",
      { thread: z.string(), text: z.string().min(1), note: z.string().max(4000).optional().describe('A line to the owner above the card') },
      a => run(() => { live(); mine(a.thread); return { id: feedAdd(pid, { by: 'coord', text: a.note ?? '', thread: a.thread, draft: { text: a.text } }).id }; })),
    tool('list_threads', "List this project's threads: id, title, agent, bucket, state, summary, last update, folder and branch.", {},
      () => run(() => threads(pid).sort((a, b) => b.updated - a.updated).map(s => ({ id: s.id, title: s.title, agent: s.agent, bucket: bucket(s), st: s.st, summary: s.summary, updated: new Date(s.updated).toISOString(), cwd: s.cwd, branch: s.branch })))),
    tool('read_thread', "Read the end of one of this project's threads: what was said, the request it waits on, a line for each group of steps.", { thread: z.string(), last: z.number().int().min(1).max(40).optional().describe('How many entries, 10 by default') },
      a => run(async () => {
        const x = mine(a.thread);
        await x.kept; await x.ensureLoaded();
        const lines = (x.items ?? []).slice(-(a.last ?? 10)).flatMap(it =>
          it.k === 'you' ? [`${it.by === 'coord' ? 'Coordinator' : 'Owner'}: ${cut(it.text)}`] : it.k === 'it' ? [`Agent: ${cut(it.text)}`] : it.k === 'note' ? [`Note: ${cut(it.text)}`]
          : it.k === 'req' && !it.done ? [`Waiting for the owner: ${reqLine(it.req)}`]
          : it.k === 'steps' ? [`Steps: ${it.steps.length}${it.took ? ` in ${it.took}` : ''} (${[...new Set(it.steps.map(s => s.k))].join(', ')})`] : []);
        return `"${x.s.title}" (${x.s.id}) ${x.s.st}\n${lines.join('\n')}`;
      })),
  ];
}

// ---------- the child ----------
// Whether a file, any link in its path followed, is inside one of the folders.
const real = (p: string): string => { try { return realpathSync(p); } catch { return path.join(real(path.dirname(p)), path.basename(p)); } };
const inside = (file: string, dirs: string[]) => path.isAbsolute(file) && dirs.some(d => real(path.resolve(file)).startsWith(real(d) + path.sep));
const NAMES = ['post', 'edit_post', 'start_thread', 'message_thread', 'draft', 'list_threads', 'read_thread'];

class Coord {
  q?: Query; input?: ReturnType<typeof pushable<SDKUserMessage>>;
  st: 'work' | 'idle' | 'err' = 'idle';
  private lines: string[] = [];
  private timer?: ReturnType<typeof setTimeout>;
  // The messages given to the child and not yet answered by a result · the context its last answer was given (tokens) ·
  // when its last result came
  private inflight = new Set<string>();
  private ctx = 0;
  private doneAt = 0;
  constructor(private id: string) {}

  wake(line: string) {
    this.lines.push(line);
    this.timer ??= setTimeout(() => { this.timer = undefined; this.flush(); }, 1500);
  }
  private flush() {
    const p = projOf(this.id), lines = this.lines.splice(0);
    if (!p?.coord.on || p.archived || !lines.length) return;
    let text = `[Startrail] ${stamp(Date.now())}\n${lines.map(l => `- ${l}`).join('\n')}`;
    try {
      if (!this.q || this.ctx > 100_000 || (this.doneAt && Date.now() - this.doneAt > 60 * 60e3)) { this.drop(); this.start(p); text = `${digest(p)}\n\n${text}`; }
      const uuid = randomUUID();
      this.inflight.add(uuid);
      this.input!.push({ type: 'user', message: { role: 'user', content: text }, parent_tool_use_id: null, uuid });
      this.set('work');
    } catch (e) { this.fail(e); }
  }
  private start(p: Proj) {
    const a = auth();
    if (!a.ready) throw new Error(a.why || 'Claude is not signed in');
    const dirs = projDirs(p), input = this.input = pushable<SDKUserMessage>();
    this.ctx = 0; this.doneAt = 0; this.inflight.clear();
    // Only its tools: Read, Glob and Grep, Edit and Write inside memory/ and files/, and the startrail server. No user
    // or project settings, CLAUDE.md, hooks, plugins or other MCP servers. No model set (a project made before the menu
    // was read) is Sonnet, not whatever Claude Code would pick.
    const q = this.q = query({ prompt: input, options: {
      cwd: projDir(p.id), env: claudeEnv(), pathToClaudeCodeExecutable: EXE, model: p.coord.model || 'sonnet', effort: p.coord.effort as Options['effort'],
      systemPrompt: promptOf(p), settingSources: [], strictMcpConfig: true, persistSession: false,
      tools: ['Read', 'Glob', 'Grep', 'Edit', 'Write'], allowedTools: ['Read', 'Glob', 'Grep', ...NAMES.map(n => `mcp__startrail__${n}`)],
      // alwaysLoad: with the built-in tools cut down there is no tool search to reach tools deferred behind it.
      mcpServers: { startrail: createSdkMcpServer({ name: 'startrail', alwaysLoad: true, tools: tools(p.id) }) },
      canUseTool: async (name, tin): Promise<PermissionResult> => (name === 'Edit' || name === 'Write') && typeof tin.file_path === 'string' && inside(tin.file_path, dirs)
        ? { behavior: 'allow', updatedInput: tin } : { behavior: 'deny', message: "The coordinator may only write inside the project's memory/ and files/ folders." },
    } });
    void (async () => {
      try { for await (const m of q) this.frame(m as Record<string, any>); if (this.q === q) this.fail(new Error('the coordinator stopped')); }
      catch (e) { if (this.q === q) this.fail(e); }
    })();
  }
  private frame(m: Record<string, any>) {
    if (m.type === 'assistant' && !m.parent_tool_use_id) {
      // What it was given with its last answer is the context it works in.
      const u = m.message?.usage;
      if (u) this.ctx = (u.input_tokens ?? 0) + (u.cache_read_input_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0);
      for (const b of m.message?.content ?? []) if (b.type === 'text' && b.text?.trim()) log('coordinator', this.id, oneLine(b.text, 300));
    } else if (m.type === 'result') {
      this.doneAt = Date.now();
      if (m.is_error || m.subtype !== 'success') { this.fail(new Error(oneLine((m.errors as string[] | undefined)?.join(' · ') || String(m.result ?? '') || 'the coordinator failed', 200))); return; }
      const ids: string[] = m.user_message_uuids ?? (m.user_message_uuid ? [m.user_message_uuid] : []);
      if (ids.length) for (const id of ids) this.inflight.delete(id); else this.inflight.clear();
      if (!this.inflight.size) this.set('idle');
    }
  }
  // Let go of the child; the next wake starts a fresh one.
  private drop() {
    const q = this.q;
    this.q = undefined; this.input?.end(); this.input = undefined;
    q?.close();
  }
  private fail(e: unknown) {
    log('coordinator', this.id, e);
    this.drop();
    this.set('err', oneLine(e instanceof Error ? e.message : String(e), 200));
  }
  private set(st: Coord['st'], why?: string) {
    if (st === this.st && !why) return;
    this.st = st;
    broadcast({ t: 'coord', proj: this.id, st, ...why ? { why } : {} });
  }
  stop() {
    clearTimeout(this.timer); this.timer = undefined; this.lines = [];
    this.drop();
    this.set('idle');
  }
}

const coords = new Map<string, Coord>();
export const coordSt = (id: string) => coords.get(id)?.st ?? 'idle';
// Something the coordinator should hear of; nothing when the project has none (off, or archived).
export function coordWake(id: string, line: string) {
  const p = projOf(id);
  if (!p?.coord.on || p.archived) return;
  let c = coords.get(id);
  if (!c) coords.set(id, c = new Coord(id));
  c.wake(line);
}
// A project changed: switched off, archived, given another model or effort, or anything its prompt says, its child ends
// (the next wake starts one).
export function coordSync(p: Proj, was: Proj) {
  const same = (['name', 'goal', 'folder', 'instructions'] as const).every(k => p[k] === was[k]) && p.coord.model === was.coord.model && p.coord.effort === was.coord.effort;
  if (!p.coord.on || p.archived || !same) coords.get(p.id)?.stop();
}
export function coordStopAll() { for (const c of coords.values()) c.stop(); }

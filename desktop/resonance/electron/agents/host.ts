// The agent host (ADR 0073): one long-lived process that runs every coding-agent session Jarvis starts, so a turn keeps
// going while the companion or the daemon restarts. The companion starts it when nothing answers on its port; the
// Agents window talks to it over local HTTP and one event stream. Conversations are read back from each agent's own
// transcript; this process keeps only what the agents do not: pinned, archived, which agent, the worktree it made.
import http from 'node:http';
import { createHash, randomUUID } from 'node:crypto';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { existsSync, mkdirSync, renameSync, statSync, writeFileSync } from 'node:fs';
import { copyFile, mkdir, open, readFile, readdir, rename, stat, unlink, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import type { Agent, Answer, Catalog, Choice, Ctx, Doctor, Event, File, Item, Live, Mcp, McpAct, Outside, Pic, Project, Req, Rx, Service, Sess, St, Step, Task, Usage, UsageWindow } from './types.js';
import { claude, claudeExe, heldReq } from './claude.js';
import { codex } from './codex.js';
import { loginPath, version, which } from './doctor.js';
import { findFiles, GIT, keepUpload, peek, pruneOld, resolveRefs, sendFile } from './files.js';
import { contentOf } from './form.js';
import { hostKey } from './key.js';
import { ask, socketFor, startKeeper, type Kid } from './keeper.js';
import { LABEL, Landing, REOPEN, dirtyOf, shellEnv } from './land.js';
import { baseOf, changes, fileDiff, revert } from './review.js';
import { auth, forgetKey, loadSettings, PACKAGED, patchSettings, saveKey, settings } from './settings.js';
import { inputTerm, killTerm, openTerm, resizeTerm, streamTerm } from './term.js';

const exec = promisify(execFile);
const ROOT = process.env.JARVIS_RUNTIME_ROOT ?? path.join(homedir(), '.jarvis');
export const DIR = process.env.JARVIS_AGENTS_DIR ?? path.join(ROOT, 'agents');
const PORT = Number(process.env.JARVIS_AGENTS_PORT ?? 8016);
// Where the keeper (ADR 0098) answers: the Claude Code children live there, not under this process.
export const KEEPER = socketFor(DIR);
export const log = (...a: unknown[]) => console.log(new Date().toISOString(), ...a);

// ---------- what each agent's wire must do ----------
export type Driver = {
  catalog(): Promise<Choice>;
  // A new session in `cwd`; its first message comes through send().
  create(s: Session): Promise<string>;
  send(s: Session, text: string, files: File[]): Promise<void>;
  answer(s: Session, a: Answer): void;
  interrupt(s: Session): Promise<void>;
  // Let go of the agent process; the session stays and resumes on the next send.
  release(s: Session): Promise<void>;
  set(s: Session, k: 'model' | 'effort' | 'mode', v: string): Promise<void>;
  rename(s: Session, title: string): Promise<void>;
  // Read the whole conversation back from the agent's own transcript.
  load(s: Session): Promise<void>;
  // A new session with this one's conversation: all of it, or up to the point `at` names (an item's `id`), with that
  // point (`before`: without it), named `title` when given. Null when nothing comes before it: the caller starts a fresh
  // session.
  fork(s: Session, at?: string, before?: boolean, title?: string): Promise<string | null>;
  remove(s: Session): Promise<void>;
  // The slash commands and skills for `cwd`, from the session itself when it runs; a third entry names the window's
  // own place for a command it handles itself (B8, B9).
  commands(cwd: string, s?: Session): Promise<[string, string, string?][]>;
  // What a terminal types to continue it.
  resume(s: Session): string;
  // What fills its context window now.
  context(s: Session): Promise<Ctx>;
  // Its MCP servers as this session sees them (C3); one switched on or off, connected again, or signed in to, then the
  // list again, or the page the sign-in opens.
  mcp?(s: Session): Promise<Mcp[]>;
  mcpAct?(s: Session, name: string, act: McpAct): Promise<Mcp[] | { url: string }>;
  // A question on the side (C7): answered from the conversation so far, a running turn's work included, without
  // disturbing it, and kept out of it. `history`: this side talk's earlier questions and answers.
  side?(s: Session, text: string, history: [string, string][], signal: AbortSignal): Promise<string>;
  // Take back a child the keeper kept through a restart; `news`: a turn that ends in what it replays was not seen.
  adopt?(s: Session, busy: boolean, news: boolean): Promise<void>;
  // Files as they were when the message `at` was sent (Claude's checkpoints): what would change, or change them.
  rewind?(s: Session, at: string, dry: boolean): Promise<{ can: boolean; why?: string; files: string[]; add: number; del: number }>;
  // Take back a message sent while it worked, before the agent took it.
  unqueue?(s: Session, text: string): Promise<boolean>;
  // Stop one of its background tasks, and read what one wrote.
  stopTask?(s: Session, id: string): Promise<void>;
  // Its sessions in `cwd` (every folder when empty) that the window did not start (B12).
  outside(cwd: string): Promise<Outside[]>;
  // Who it is signed in as, for the check-up; and, for Codex, a sign-in page to open.
  account(): Promise<Record<string, string> | null>;
  login?(): Promise<string>;
};
const DRIVERS: Record<Agent, Driver> = { claude, codex };

// ---------- time and text ----------
export const took = (ms: number) => {
  const s = Math.max(1, Math.round(ms / 1000));
  if (s < 60) return `${s} 秒`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} 分钟` : `${Math.floor(m / 60)} 小时${m % 60 ? ` ${m % 60} 分` : ''}`;
};
// A token count as the popover says it: 950, 12.4k, 958k, 1M.
export const kt = (n: number) => n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${n >= 1e5 ? Math.round(n / 1000) : +(n / 1000).toFixed(1)}k` : String(Math.round(n));
// ---------- pictures: a copy of each image sent with a message, named by its content, so the window can show it ----------
// The transcript stays the record; a copy written more than 30 days ago goes when the host starts, and reading its session
// back writes it again.
const IMAGES = path.join(DIR, 'images');
export function pic(name: string, url: unknown): Pic {
  const m = typeof url === 'string' ? /^data:image\/(png|jpeg|gif|webp);base64,/.exec(url) : null;
  if (!m) return { name };
  const buf = Buffer.from((url as string).slice(m[0].length), 'base64'), img = `${createHash('sha256').update(buf).digest('hex').slice(0, 32)}.${m[1]}`;
  try { writeFileSync(path.join(IMAGES, img), buf, { flag: 'wx' }); }
  catch (e) { if ((e as NodeJS.ErrnoException).code !== 'EEXIST') { log('picture', name, String(e)); return { name }; } }
  return { name, img };
}
// A picture on this Mac (a file dropped on the window, an image Codex looked at) as a copy the window can show.
const MIME: Record<string, string> = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp', pdf: 'application/pdf' };
const mimeOf = (p: string) => MIME[path.extname(p).slice(1).toLowerCase()];
export async function picFile(name: string, file: string): Promise<Pic> {
  const type = mimeOf(file), buf = type?.startsWith('image/') ? await readFile(file).catch(() => null) : null;
  return buf && buf.length <= 20 << 20 ? pic(name, `data:${type};base64,${buf.toString('base64')}`) : { name };
}
// What a message's files become: pictures as pictures (Codex takes one on this Mac by its path), PDFs as documents for
// Claude, and any other file kept here (or found where it is) and named in the message. `pics` is what the
// conversation shows.
export type Sent = { images: { type: string; data: string; url: string }[]; local: string[]; pdfs: { name: string; data: string }[]; paths: string[]; pics: Pic[] };
const UPLOADS = path.join(DIR, 'uploads');
export async function sent(files: File[], agent: Agent): Promise<Sent> {
  const out: Sent = { images: [], local: [], pdfs: [], paths: [], pics: [] };
  for (const f of files) {
    let url = f.url;
    if (f.path) {
      const type = mimeOf(f.path), dir = (await stat(f.path)).isDirectory();
      if (!dir && type?.startsWith('image/') && agent === 'codex') { out.local.push(f.path); out.pics.push(await picFile(f.name, f.path)); continue; }
      const buf = !dir && type && (type.startsWith('image/') || agent === 'claude') ? await readFile(f.path) : null;
      if (!buf || buf.length > 30 << 20) { out.paths.push(f.path); out.pics.push({ name: f.name }); continue; }
      url = `data:${type};base64,${buf.toString('base64')}`;
    }
    const m = /^data:(image\/(?:png|jpeg|gif|webp)|application\/pdf);base64,(.+)$/s.exec(url ?? '');
    if (m && m[1] === 'application/pdf' && agent === 'claude') { out.pdfs.push({ name: f.name, data: m[2] }); out.pics.push({ name: f.name }); }
    else if (m && m[1] !== 'application/pdf') { out.images.push({ type: m[1], data: m[2], url: url! }); out.pics.push(pic(f.name, url)); }
    else { out.paths.push(await keepUpload(UPLOADS, f.name, url!)); out.pics.push({ name: f.name }); }
  }
  return out;
}
async function pruneImages() {
  await mkdir(IMAGES, { recursive: true });
  for (const f of await readdir(IMAGES)) {
    const p = path.join(IMAGES, f);
    if (Date.now() - (await stat(p)).mtimeMs > 30 * 864e5) await unlink(p).catch(() => {});
  }
}
const oneLine = (t: string, n = 120) => { const x = t.replace(/\s+/g, ' ').trim(); return x.length > n ? `${x.slice(0, n - 1)}…` : x; };
// ---------- reactions (m-rx): they never wake the agent; the next message you send carries the ones still waiting as a
// line in front of your words, and what you said shows which went with it ----------
export const RX = ['👍', '❤️', '😂', '🎉', '🤔', '👀', '🙏', '👎'];
const RIDE = /^\[Reactions: ([^\n]*)\]\n\n/;
// What you said without that line, and the reactions it carried.
export function rode(text: string) {
  const m = RIDE.exec(text);
  return m ? { text: text.slice(m[0].length), ride: [...m[1].matchAll(/(?:^|; )(\S+) on (?:your reply|my message) /g)].map(x => x[1]).filter(e => RX.includes(e)) } : { text, ride: [] };
}
// A finished row shows the start of its last answer: the first sentence, without markdown.
export const firstSentence = (text: string) => oneLine((text.split('\n').find(l => l.trim() && !l.startsWith('```')) ?? '')
  .replace(/^\s*(?:[-*#>]+|\d+[.)])\s*/, '').replace(/\*\*|`/g, '').split(/(?<=[。！？])|(?<=[.!?])\s+(?=[A-Z"'(])/)[0].replace(/[：:]\s*$/, ''));
const base = (p: string) => p.split('/').pop() ?? p;
const NOW: Record<Step['k'], (t: string) => string> = {
  read: t => `在读 ${base(t)}`, edit: t => `在改 ${base(t)}`, bash: t => `在跑 ${oneLine(t, 40)}`, search: t => `在搜 ${oneLine(t, 40)}`,
  agent: () => '在开子任务', web: () => '在查网页', tool: t => `在用 ${oneLine(t, 40)}`, say: () => '在写', think: () => '在想',
};
export function reqLine(r: Req) {
  if (r.tool === 'Ask') return `问你：${oneLine(r.qs[0]?.q ?? '', 60)}`;
  if (r.tool === 'Plan') return '计划写好了，等你点头';
  if (r.tool === 'Bash') return `想跑 ${oneLine(r.cmd, 60)}`;
  if (r.tool === 'Edit') return `想改 ${base(r.file)}`;
  if (r.tool === 'Form') return r.url ? `${r.server} 要你打开一个网页` : `${r.server} 要你填一张表`;
  return `想用 ${r.name}`;
}

// ---------- one session: its row, its conversation, and the turn being built ----------
// last: when the last thing in this turn happened, so a turn read back from a transcript knows how long it took ·
// tools: where each call's step is (a sub-agent's call: its index under the step that started it) · ref: the point the
// answer being written ends at, for fork and rewind
type Turn = { group: number; start?: number; last?: number; pending: string | null; block: string; tools: Map<string, [number, number, number?]>; plan: number; ref?: string };
export class Session {
  items: Item[] | null = null;
  live: string | null = null;
  // The driver's own state: its process, its pending requests.
  rt: Record<string, any> = {};
  private turn: Turn | null = null;
  private quiet = false;
  private liveAt = 0;
  private liveTimer: ReturnType<typeof setTimeout> | undefined;
  // B23: the conversation is read the first time something needs it, not when the host starts. `kept`: its child, kept
  // by the keeper through a host restart, being taken back; whatever acts on the session waits for that first.
  kept?: Promise<void>;
  private loading?: Promise<void>;
  // The reactions a message Claude keeps in its queue carries, by its words: taking it back puts them back to waiting.
  carrying = new Map<string, [string, string][]>();
  constructor(public s: Sess, public repo: string) {}
  get driver() { return DRIVERS[this.s.agent]; }
  // ADR 0097: its landing, made the first time it is asked for.
  private landingOf?: Landing;
  get landing() { return this.landingOf ??= new Landing(this); }

  // `daemon`: these are the daemon's marks coming in, not a change to send it.
  set(p: Partial<Sess>, daemon = false) {
    if (p.st && p.st !== this.s.st && !this.quiet) {
      const at = Date.now();
      this.s.trace = [...this.s.trace ?? [], { at, st: p.st }];
      this.s.created ??= at;
    }
    const changed: string[] = [];
    for (const [k, v] of Object.entries(p)) if ((this.s as Record<string, unknown>)[k] !== v) { (this.s as Record<string, unknown>)[k] = v; changed.push(k); }
    if (!changed.length || this.quiet) return;
    if (changed.includes('st')) this.landingOf?.saw(this.s.st);
    if (!daemon) {
      const m: Record<string, boolean> = {};
      if (changed.includes('unread')) Object.assign(m, this.s.unread ? { unread: true } : { seen: true });
      if (changed.includes('parked')) m.park = this.s.parked;
      if (changed.includes('archived')) m.archive = this.s.archived;
      if (Object.keys(m).length) markOut(this.s.id, m);
    }
    broadcast({ t: 'sess', s: this.s });
    save();
  }
  private emit(from: number) { if (this.items && !this.quiet) broadcast({ t: 'items', id: this.s.id, from, items: this.items.slice(from) }); }
  changed(i: number) { this.emit(i); }
  private time(at?: number) { return at ?? (this.quiet ? undefined : Date.now()); }
  private push(it: Item) { if (it.at !== undefined && (!this.s.created || it.at < this.s.created)) this.s.created = it.at; this.items!.push(it); this.emit(this.items!.length - 1); return this.items!.length - 1; }
  private showLive() {
    const t = this.turn, text = t ? [t.pending, t.block].filter(Boolean).join('\n\n') : '';
    this.live = text || null;
    if (this.quiet) return;
    // The answer as it is written, at most twenty times a second.
    clearTimeout(this.liveTimer);
    const send = () => { this.liveAt = Date.now(); broadcast({ t: 'live', id: this.s.id, text: this.live }); };
    const wait = 50 - (Date.now() - this.liveAt);
    if (wait <= 0 || this.live === null) send(); else this.liveTimer = setTimeout(send, wait);
  }

  // Reading a transcript builds the same items a live turn does, then shows them at once.
  // `open`: the turn is still running, so it stays open for what comes next.
  async build(f: () => Promise<void>, open = false) {
    this.items = []; this.turn = null; this.quiet = true;
    try { await f(); if (!open) this.end(undefined, true); } finally { this.quiet = false; this.live = null; }
    this.emit(0);
    if (open) this.showLive();
  }
  // ----- the turn -----
  begin() {
    this.turn = { group: -1, pending: null, block: '', tools: new Map(), plan: -1 };
    // Tasks that ended go when the next turn begins.
    if (!this.quiet) this.set({ st: 'work', stopped: false, since: Date.now(), now: '在想', summary: '在想', updated: Date.now(),
      ...this.s.tasks?.some(t => t.st !== 'run') ? { tasks: this.s.tasks.filter(t => t.st === 'run') } : {} });
  }
  private need() { if (!this.turn) this.begin(); return this.turn!; }
  // The line of reactions it carried is not what you said: it shows as the reactions under it.
  you(text: string, files: Pic[] = [], at?: number, id?: string) {
    this.end(undefined, true);
    const r = rode(text);
    if (!this.quiet) this.carrying.delete(r.text);
    this.push({ k: 'you', text: r.text, at: this.time(at), ...(files.length ? { files } : {}), ...(id ? { id } : {}), ...(r.ride.length ? { ride: r.ride } : {}) });
  }
  // The agent took a message you sent while it worked: its 👀 on it (m-eyes).
  looked(id: string) { const k = `you:${id}`; this.set({ rx: { ...this.s.rx, [k]: { ...this.s.rx?.[k], by: '👀' } } }); }
  // The point in the agent's own record this answer ends at (Claude: the message uuid; Codex: the turn).
  ref(id: string) { this.need().ref = id; }
  // Codex names a turn only once it started: the message that started it gets its id then.
  youId(id: string) {
    let i = (this.items?.length ?? 0) - 1;
    while (i >= 0 && this.items![i].k !== 'you') i--;
    const it = i >= 0 ? this.items![i] as Item & { k: 'you' } : null;
    if (it && !it.id) { it.id = id; this.changed(i); }
  }
  // The live group of steps, made when the first step of a stretch arrives.
  private group(at?: number) {
    const t = this.need(), last = this.items!.length - 1;
    t.last = this.time(at);
    if (t.group === last && t.group >= 0) return t.group;
    t.group = this.push({ k: 'steps', steps: [], live: true, at: this.time(at) });
    t.start = t.last;
    return t.group;
  }
  // What it wrote before a step goes into the steps, not the conversation.
  private flush(at?: number) {
    const t = this.turn;
    if (!t || t.pending === null) return;
    const g = this.group(at), it = this.items![g] as Item & { k: 'steps' };
    it.steps.push({ k: 'say', t: t.pending });
    t.pending = null; t.block = '';
    this.changed(g); this.showLive();
  }
  // Text as it streams in: the whole of the current block so far.
  delta(text: string) {
    const t = this.need();
    t.block = text; this.showLive();
    if (!this.quiet && this.s.now !== '在写') this.set({ now: '在写', summary: '在写回答' });
  }
  // A text block is complete.
  say(text: string, at?: number) {
    if (!text.trim()) return;
    const t = this.need();
    t.last = this.time(at);
    t.pending = t.pending ? `${t.pending}\n\n${text}` : text; t.block = '';
    this.showLive();
  }
  tool(key: string, step: Step, at?: number) {
    this.flush(at);
    const g = this.group(at), it = this.items![g] as Item & { k: 'steps' };
    it.steps.push({ ...step, at: this.time(at) });
    this.turn!.tools.set(key, [g, it.steps.length - 1]);
    this.changed(g);
    if (!this.quiet) this.set({ now: NOW[step.k](step.t), summary: NOW[step.k](step.t), updated: Date.now() });
  }
  toolDone(key: string, patch: Partial<Step>) {
    const at = this.turn?.tools.get(key);
    if (!at) return;
    const it = this.items![at[0]] as Item & { k: 'steps' }, st = at[2] === undefined ? it.steps[at[1]] : it.steps[at[1]].sub?.[at[2]];
    if (!st) return;
    Object.assign(st, patch);
    this.changed(at[0]);
  }
  // A sub-agent's own step, under the step of the call that started it (`parent`: that call's key; B16).
  sub(parent: string, key: string | undefined, step: Step, at?: number) {
    const p = this.turn?.tools.get(parent);
    if (!p || p[2] !== undefined) return;
    const it = this.items![p[0]] as Item & { k: 'steps' }, sub = it.steps[p[1]].sub ??= [];
    if (sub.length >= 300) return;
    sub.push({ ...step, at: this.time(at) });
    if (key) this.turn!.tools.set(key, [p[0], p[1], sub.length - 1]);
    this.changed(p[0]);
  }
  // Background work it started (B17): one entry per task, the ended ones kept until the next turn begins.
  task(id: string, patch: Partial<Task>) {
    if (!id || this.quiet) return;
    const ts = [...this.s.tasks ?? []], i = ts.findIndex(t => t.id === id);
    if (i >= 0) ts[i] = { ...ts[i], ...patch };
    else if (patch.what || patch.kind) ts.push({ id, kind: 'task', what: '', st: 'run', since: Date.now(), ...patch });
    else return;
    this.set({ tasks: ts.slice(-20) });
  }
  plan(todos: [string, 0 | 1 | 2][]) {
    const t = this.need();
    if (t.plan >= 0 && this.items![t.plan]?.k === 'plan') { (this.items![t.plan] as Item & { k: 'plan' }).todos = todos; this.changed(t.plan); return; }
    this.flush();
    t.plan = this.push({ k: 'plan', todos });
  }
  note(text: string) { this.flush(); this.push({ k: 'note', text }); }
  ask(req: Req) {
    this.flush();
    this.push({ k: 'req', req, at: this.time() });
    if (!this.quiet) this.set({ st: 'wait', now: undefined, summary: reqLine(req), updated: Date.now() });
  }
  answered(id: string, done: string) {
    const i = this.items?.findIndex(it => it.k === 'req' && it.req.id === id && !it.done) ?? -1;
    if (i < 0) return;
    (this.items![i] as Item & { k: 'req' }).done = done;
    this.items![i].ended = this.time();
    this.changed(i);
    if (!this.pending()) this.set({ st: 'work', now: '在想', summary: '在想' });
  }
  pending() { return this.items?.find(it => it.k === 'req' && !it.done) as (Item & { k: 'req' }) | undefined; }
  // The turn ends: what it wrote last is the answer, the steps fold, and the row says how it went. `why` is the row's
  // line when the answer is not; a turn Allen ended himself is not unread.
  end(at?: number, silent = false, st: St = 'done', why = '', unread = true) {
    const t = this.turn;
    if (t) {
      if (t.group >= 0) {
        const g = this.items![t.group] as Item & { k: 'steps' };
        if (g?.k === 'steps' && g.live) { g.live = false; if (t.start !== undefined) g.took = took((at ?? (this.quiet ? t.last : undefined) ?? Date.now()) - t.start); this.changed(t.group); }
      }
      const text = [t.pending, t.block].filter(Boolean).join('\n\n');
      if (text) this.push({ k: 'it', text, at: this.time(at ?? (this.quiet ? t.last : undefined)), ...t.ref ? { id: t.ref } : {} });
      this.turn = null;
      clearTimeout(this.liveTimer); this.live = null;
      if (!this.quiet) broadcast({ t: 'live', id: this.s.id, text: null });
    }
    // Requests nobody answered die with the turn.
    for (const it of this.items ?? []) if (it.k === 'req' && !it.done) it.done = '没回答';
    if (silent || this.quiet) return;
    const last = [...this.items ?? []].reverse().find(it => it.k === 'it');
    this.set({ st, now: undefined, since: undefined, updated: Date.now(), unread,
      summary: why || (st === 'err' ? '出错了' : last?.k === 'it' ? firstSentence(last.text) : this.s.summary) });
    void this.measure();
  }
  // What landing would take now, for the conversation's 一键落地.
  async measure() { const d = await dirtyOf(this); if (JSON.stringify(d) !== JSON.stringify(this.s.dirty)) this.set({ dirty: d }); }
  // A message sent while it worked waits here until the agent takes it.
  enqueue(text: string) { this.set({ queue: [...this.s.queue ?? [], rode(text).text] }); }
  dequeue(text: string) {
    const q = [...this.s.queue ?? []], i = q.indexOf(rode(text).text);
    if (i >= 0) q.splice(i, 1);
    this.set({ queue: q.length ? q : undefined });
  }
  // Read once, however many ask for it at the same time.
  async ensureLoaded() {
    if (this.items) return;
    await (this.loading ??= this.driver.load(this).catch(e => { log('load', this.s.id, e); this.items = [{ k: 'note', text: `读不出这个会话的记录：${String(e)}` }]; })
      .finally(() => { this.loading = undefined; }));
  }
}

// ---------- the sessions, kept in one file ----------
const sessions = new Map<string, Session>();
export const find = (id: string) => sessions.get(id);
const FILE = path.join(DIR, 'sessions.json');
const rows = () => JSON.stringify({ v: 1, sessions: [...sessions.values()].map(x => {
  const { now, since, queue, stopped, bg, land, dirty, tasks, ...keep } = x.s;
  return { ...keep, repo: x.repo };
}) }, null, 1);
// Written 300 ms after a change however many more follow, one write at a time, and at once when the host is told to
// stop; never before the list was read back, so a host that could not read it does not write it over.
let saving: ReturnType<typeof setTimeout> | undefined, writing = Promise.resolve(), restored = false;
function save() {
  if (!restored) return;
  saving ??= setTimeout(() => {
    saving = undefined;
    const text = rows();
    writing = writing.then(async () => {
      await mkdir(DIR, { recursive: true });
      await writeFile(`${FILE}.tmp`, text);
      await rename(`${FILE}.tmp`, FILE);
    }).catch(e => log('save', e));
  }, 300);
}
function saveNow() {
  if (!restored) return;
  clearTimeout(saving); saving = undefined;
  mkdirSync(DIR, { recursive: true });
  writeFileSync(`${FILE}.stop`, rows());
  renameSync(`${FILE}.stop`, FILE);
}
async function restore(kids: Map<string, Kid>) {
  const data = JSON.parse(await readFile(FILE, 'utf8').catch(() => '{"sessions":[]}'));
  restored = true;
  for (const { repo, ...s } of data.sessions as (Sess & { repo: string })[]) {
    // A turn that was running goes on in the keeper; without its child there, it went with the host.
    if ((s.st === 'work' || s.st === 'wait' || s.st === 'pack') && !kids.has(s.id)) Object.assign(s, {
      st: 'err', trace: [...s.trace ?? [], { at: Date.now(), st: 'err' }],
      summary: 'Jarvis 的后台重启了，这一轮断了 · 发一句接着来',
    });
    s.parked ??= false;
    sessions.set(s.id, new Session(s, repo));
  }
  // What each would land, a few at a time so a long list does not start forty gits at once.
  void (async () => { const all = [...sessions.values()].filter(x => !x.s.archived); for (let i = 0; i < all.length; i += 4) await Promise.all(all.slice(i, i + 4).map(x => x.measure().catch(() => {}))); })();
}
// Another session working in the same folder (a fork): its worktree stays for it.
export const sharing = (x: Session) => [...sessions.values()].some(o => o !== x && o.s.cwd === x.s.cwd);
const busy = (x: Session) => x.s.st === 'work' || x.s.st === 'wait' || x.s.st === 'pack';

// ---------- the event stream ----------
const clients = new Set<http.ServerResponse>();
export function broadcast(e: Event) {
  const line = `data: ${JSON.stringify(e)}\n\n`;
  for (const c of clients) c.write(line);
}
let catalog: Catalog | null = null;
async function getCatalog(): Promise<Catalog> {
  if (!catalog) {
    const [c, x] = await Promise.all([claude.catalog(), codex.catalog()]);
    catalog = { claude: c, codex: x };
  }
  return catalog;
}
// A driver learned more about its models: everyone gets the new menus.
export async function catalogChanged() { catalog = null; broadcast({ t: 'catalog', catalog: await getCatalog() }); }

// ---------- git: projects, worktrees ----------
const git = async (cwd: string, ...args: string[]) => (await exec('git', [...GIT, '-C', cwd, ...args], { maxBuffer: 64 << 20 })).stdout;
// The repository a folder belongs to, with a worktree counted as its main checkout.
async function repoOf(cwd: string) {
  try { return path.dirname((await git(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir')).trim()); } catch { return ''; }
}
async function branchOf(cwd: string) { try { return (await git(cwd, 'branch', '--show-current')).trim(); } catch { return ''; } }
// A worktree of its own, from `from` (a branch or a commit; the repository's HEAD when empty), with the ignored files its
// .worktreeinclude names copied in, as Claude Code's own worktrees do.
async function worktree(repo: string, hint: string, from = '') {
  const slug = hint.toLowerCase().match(/[a-z0-9]+/g)?.slice(0, 4).join('-').slice(0, 32) || 'session';
  const name = `${slug}-${randomUUID().slice(0, 4)}`, at = path.join(repo, '.claude', 'worktrees', name), branch = `worktree-${name}`;
  const start = from ? (await git(repo, 'rev-parse', '--verify', '--quiet', `${from}^{commit}`).catch(() => '')).trim() : 'HEAD';
  if (!start) throw new Http(400, `没有 ${from} 这个分支或提交`);
  await git(repo, 'worktree', 'add', '-b', branch, at, start);
  await include(repo, at);
  return { cwd: at, branch };
}
async function include(repo: string, at: string) {
  if (!existsSync(path.join(repo, '.worktreeinclude'))) return;
  const listed = (await git(repo, 'ls-files', '--others', '--ignored', '--exclude-from=.worktreeinclude').catch(() => '')).split('\n').filter(Boolean), some = listed.slice(0, 500);
  // Only what git itself ignores: anything else is in the worktree already, or nobody's.
  const ignored = some.length ? (await git(repo, 'check-ignore', '--', ...some).catch(e => String((e as { stdout?: string }).stdout ?? ''))).split('\n').filter(Boolean) : [];
  for (const f of ignored) {
    await mkdir(path.dirname(path.join(at, f)), { recursive: true });
    await copyFile(path.join(repo, f), path.join(at, f)).catch(e => log('worktreeinclude', f, String(e)));
  }
  if (listed.length > some.length) log('worktreeinclude', repo, `${listed.length} files, copied the first ${some.length}`);
}
// The script the owner set for a repository's new worktrees (installing, copying, building), run in the worktree before
// its first turn; a failure is said in the conversation and the turn goes ahead.
async function setup(x: Session) {
  const cmd = settings.setup?.[x.repo];
  if (!cmd) return;
  x.set({ now: '在准备 worktree', summary: '在准备 worktree' });
  const env = shellEnv(), r = await new Promise<{ code: number; out: string }>(done => {
    let out = '';
    const c = spawn(env.SHELL || '/bin/zsh', ['-lc', cmd], { cwd: x.s.cwd, detached: true, stdio: ['ignore', 'pipe', 'pipe'], env: { ...env, JARVIS_WORKTREE: x.s.cwd, JARVIS_REPO: x.repo } });
    const keep = (b: Buffer) => { out = (out + b.toString('utf8')).slice(-8000); };
    c.stdout!.on('data', keep); c.stderr!.on('data', keep);
    const timer = setTimeout(() => { out += '\n超过 10 分钟，停掉了'; try { process.kill(-c.pid!, 'SIGTERM'); } catch { c.kill(); } }, 10 * 60e3);
    c.on('error', e => { clearTimeout(timer); done({ code: 1, out: String(e) }); });
    c.on('close', code => { clearTimeout(timer); done({ code: code ?? 1, out }); });
  });
  x.note(r.code ? `worktree 的准备脚本没跑成（退出码 ${r.code}）：${oneLine(r.out.trim().split('\n').slice(-5).join(' · '), 400)}` : 'worktree 准备好了');
}
// The project list (A8): the folders sessions ran in, the latest first, then folders the owner added, then the git
// repositories in ~/Projects.
async function projects(): Promise<Project[]> {
  const out = new Map<string, Project>(), used = new Map<string, number>();
  for (const x of sessions.values()) { const p = x.repo || x.s.cwd; used.set(p, Math.max(used.get(p) ?? 0, x.s.updated)); }
  const add = async (p: string, more: Partial<Project>) => {
    const had = out.get(p);
    if (had) { Object.assign(had, more); return; }
    if (!(await stat(p).catch(() => null))?.isDirectory()) return;
    out.set(p, { path: p, name: path.basename(p), git: !!(await stat(path.join(p, '.git')).catch(() => null)), ...more });
  };
  for (const [p, at] of [...used].sort((a, b) => b[1] - a[1])) await add(p, { used: at });
  for (const p of settings.folders ?? []) await add(p, { added: true });
  const home = path.join(homedir(), 'Projects');
  for (const d of (await readdir(home, { withFileTypes: true }).catch(() => [])).sort((a, b) => a.name.localeCompare(b.name))) {
    const p = path.join(home, d.name);
    if (d.isDirectory() && !d.name.startsWith('.') && !out.has(p) && await stat(path.join(p, '.git')).catch(() => null)) await add(p, {});
  }
  return [...out.values()];
}

// ---------- the daemon's marks (ADR 0069): unread, parked and archived, one file the notch shares ----------
const DAEMON = `http://127.0.0.1:${process.env.JARVIS_INHERENT_BRIDGE_PORT ?? '8006'}`;
async function daemon(route: string, body?: unknown) {
  const r = await fetch(DAEMON + route, { method: body ? 'POST' : 'GET', signal: AbortSignal.timeout(5000),
    headers: { Authorization: `Bearer ${token || await readToken()}`, ...(body ? { 'Content-Type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) await readToken();
  if (!r.ok) throw new Error(`daemon answered ${r.status}`);
  return r.json();
}
// A mark that does not reach the daemon stays in this host's file, and the next change sends it again.
function markOut(id: string, change: Record<string, boolean>) { daemon(`/inherent/agent-marks/${encodeURIComponent(id)}`, change).catch(e => log('marks out', id, String(e))); }
async function marksIn() {
  const r = await daemon('/inherent/agent-marks').catch(() => null) as { marks?: Record<string, { unread?: boolean; parked_ms?: number | null; archived_ms?: number | null }> } | null;
  for (const [id, m] of Object.entries(r?.marks ?? {})) sessions.get(id)?.set({ unread: !!m.unread, parked: !!m.parked_ms, archived: !!m.archived_ms }, true);
}

// ---------- the routes ----------
type Req0 = http.IncomingMessage;
// ADR 0095: the host's own key opens it, so the window connects with no daemon running. The daemon's local key, which
// this process also uses to reach the daemon, still opens it for a companion from before the host had a key.
let key = '', token = '';
async function readToken() {
  try { token = JSON.parse(await readFile(path.join(ROOT, 'plugin-access.json'), 'utf8')).token ?? ''; } catch { token = ''; }
  return token;
}
async function authorized(req: Req0) {
  const got = req.headers.authorization ?? '';
  if (got === `Bearer ${key}`) return true;
  if (token && got === `Bearer ${token}`) return true;
  // Read again in case it was made after this process started.
  return !!(await readToken()) && got === `Bearer ${token}`;
}
async function body(req: Req0): Promise<Record<string, any>> {
  const chunks: Buffer[] = [];
  let n = 0;
  for await (const c of req) { n += c.length; if (n > 48 << 20) throw new Error('太大了'); chunks.push(c); }
  return n ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {};
}
// `need` names what the window can answer with: 'auth' is Claude's sign-in in the packaged app (ADR 0094), 'force' a
// second, sure press.
export class Http extends Error { constructor(public code: number, msg: string, public need?: string) { super(msg); } }
// A Claude session of Startrail's own needs the packaged app's sign-in before anything starts a claude for it.
const signedIn = (agent: Agent) => { const a = auth(); if (agent === 'claude' && !a.ready) throw new Http(409, a.why ?? '', 'auth'); };
// Everyone gets what changed; a sign-in that became ready (or went) reads Claude's menus again.
async function settingsChanged(was: boolean) {
  broadcast({ t: 'settings', settings, auth: auth() });
  if (auth().ready !== was) await catalogChanged();
}
const need = (id: string) => { const x = sessions.get(id); if (!x) throw new Http(404, '没有这个会话'); return x; };
const str = (v: unknown, name: string) => { if (typeof v !== 'string') throw new Http(400, `${name} 不对`); return v; };
const pathOf = (p: string) => p ? path.resolve(p.replace(/^~(?=\/|$)/, homedir())) : '';
// Files sent with a message: a data: URL, or a file or folder on this Mac by its path (one dropped on the window).
const fileList = (v: unknown): File[] => Array.isArray(v) ? v.slice(0, 20).filter(f => typeof f?.name === 'string'
  && (typeof f.url === 'string' ? f.url.startsWith('data:') : typeof f.path === 'string' && path.isAbsolute(f.path) && existsSync(f.path)))
  .map(f => typeof f.url === 'string' ? { name: f.name, url: f.url } : { name: f.name, path: f.path }) : [];
// Folders a session may work in besides its own (C5).
const dirList = (v: unknown) => Array.isArray(v) ? [...new Set(v.filter((d): d is string => typeof d === 'string' && path.isAbsolute(d) && existsSync(d) && statSync(d).isDirectory()))].slice(0, 20) : [];
// What a file is compared against in the preview: what the session's review compares against.
const baseFor = (x: Session) => baseOf(x).then(b => b.base, () => 'HEAD');

async function route(req: Req0, res: http.ServerResponse, url: URL): Promise<unknown> {
  const m = req.method ?? 'GET', parts = url.pathname.split('/').filter(Boolean);
  if (m === 'GET' && url.pathname === '/health') return { ok: true, pid: process.pid };
  if (m === 'GET' && url.pathname === '/events') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*' });
    await ready;
    clients.add(res);
    req.on('close', () => clients.delete(res));
    const hello: Event = { t: 'hello', sessions: [...sessions.values()].map(x => x.s), catalog: await getCatalog(), settings, auth: auth() };
    res.write(`data: ${JSON.stringify(hello)}\n\n`);
    void marksIn();
    return undefined;
  }
  if (m === 'GET' && parts[0] === 'images' && parts.length === 2) {
    const f = /^[0-9a-f]{32}\.(png|jpeg|gif|webp)$/.exec(parts[1]), buf = f && await readFile(path.join(IMAGES, parts[1])).catch(() => null);
    if (!buf) throw new Http(404, '没有这张图');
    res.writeHead(200, { 'Content-Type': `image/${f![1]}`, 'Cache-Control': 'max-age=31536000, immutable', 'Access-Control-Allow-Origin': '*' });
    res.end(buf);
    return undefined;
  }
  // Nothing below runs before the host has the login shell's PATH, its keeper and the rows back.
  await ready.catch(() => {});
  // ---- what the owner sets, and the key Startrail's Claude sessions sign in with in the packaged app (ADR 0094) ----
  if (url.pathname === '/settings' || url.pathname === '/settings/key') {
    if (m === 'GET' && url.pathname === '/settings') return { settings, auth: auth() };
    const was = auth().ready;
    let r = {};
    if (m === 'POST' && url.pathname === '/settings') await patchSettings(await body(req));
    else if (m === 'POST') r = await saveKey(str((await body(req)).key, 'key'));
    else if (m === 'DELETE' && url.pathname === '/settings/key') await forgetKey();
    else throw new Http(405, '不行');
    await settingsChanged(was);
    return { ...r, settings, auth: auth() };
  }
  // `projects` is the list as paths, as the window before this one reads it.
  if (url.pathname === '/projects') {
    if (m === 'POST' || m === 'DELETE') {
      const p = pathOf(m === 'POST' ? str((await body(req)).path, 'path') : url.searchParams.get('path') ?? '');
      if (m === 'POST' && !(await stat(p).catch(() => null))?.isDirectory()) throw new Http(400, '没有这个文件夹');
      await patchSettings({ folders: [...m === 'POST' ? [p] : [], ...(settings.folders ?? []).filter(f => f !== p)] });
      await settingsChanged(auth().ready);
    } else if (m !== 'GET') throw new Http(405, '不行');
    const list = await projects();
    return { projects: list.map(p => p.path), list };
  }
  if (m === 'GET' && url.pathname === '/doctor') return doctor();
  if (m === 'POST' && url.pathname === '/doctor/login') {
    if ((await body(req)).agent !== 'codex' || !codex.login) throw new Http(400, '只有 Codex 在这里登录');
    return { url: await codex.login() };
  }
  // A session's folder, or for a session not started yet the folder and agent it will have.
  const where = () => {
    const id = url.searchParams.get('id');
    if (id) { const x = need(id); return { cwd: x.s.cwd, agent: x.s.agent, x }; }
    return { cwd: pathOf(url.searchParams.get('cwd') ?? '') || homedir(), agent: (url.searchParams.get('agent') === 'codex' ? 'codex' : 'claude') as Agent, x: undefined };
  };
  if (m === 'GET' && url.pathname === '/files') return { files: await findFiles(where().cwd, url.searchParams.get('q') ?? '') };
  if (url.pathname === '/resolve') {
    const refs = m === 'POST' ? (await body(req)).refs : url.searchParams.getAll('ref');
    return { found: await resolveRefs(where().cwd, Array.isArray(refs) ? refs.filter((r): r is string => typeof r === 'string') : []) };
  }
  if (m === 'GET' && url.pathname === '/peek') { const w = where(); return peek(w.cwd, url.searchParams.get('ref') ?? '', async () => w.x ? baseFor(w.x) : 'HEAD'); }
  if (m === 'GET' && url.pathname === '/branches') {
    const cwd = where().cwd, out = await git(cwd, 'for-each-ref', '--sort=-committerdate', '--format=%(refname)%09%(committerdate:unix)', 'refs/heads', 'refs/remotes').catch(() => '');
    const branches = out.split('\n').filter(l => l && !/^refs\/remotes\/[^\t]+\/HEAD\t/.test(l)).slice(0, 300)
      .map(l => { const [ref, t] = l.split('\t'); return { name: ref.replace(/^refs\/(heads|remotes)\//, ''), remote: ref.startsWith('refs/remotes/'), at: Number(t) * 1000 }; });
    return { current: await branchOf(cwd), branches };
  }
  if (m === 'GET' && url.pathname === '/commands') { const w = where(); return { commands: await DRIVERS[w.agent].commands(w.cwd, w.x) }; }
  if (m === 'GET' && url.pathname === '/search') return { hits: await search(url.searchParams.get('q') ?? '', url.searchParams.get('all') === '1') };
  // ---- sessions started outside the window (B12, ADR 0096) ----
  if (m === 'GET' && url.pathname === '/import') return { sessions: await outsideOf(pathOf(url.searchParams.get('cwd') ?? '')) };
  if (m === 'POST' && url.pathname === '/import') {
    const b = await body(req);
    return { id: await take(b.agent === 'codex' ? 'codex' : 'claude', str(b.id, 'id'), typeof b.cwd === 'string' ? pathOf(b.cwd) : '', b.force === true) };
  }
  // One of them read without taking it in, for the window's read-only view; and what the Claude ones in a terminal are
  // doing now, with the request each stopped on, answered through the daemon that holds it (ADR 0049).
  if (m === 'GET' && url.pathname === '/import/read') {
    return { items: await readOutside(url.searchParams.get('agent') === 'codex' ? 'codex' : 'claude', str(url.searchParams.get('id'), 'id'), pathOf(url.searchParams.get('cwd') ?? '')) };
  }
  if (m === 'GET' && url.pathname === '/import/live') return { live: await liveOutside() };
  if (m === 'POST' && url.pathname === '/import/answer') {
    const b = await body(req), decision = b.decision === 'deny' || b.decision === 'always' ? b.decision : 'allow';
    // A question's answers go back as {question: label}; a no carries what was typed.
    const answers = b.answers && typeof b.answers === 'object' && !Array.isArray(b.answers)
      ? Object.fromEntries(Object.entries(b.answers as Record<string, unknown>).filter((e): e is [string, string] => typeof e[1] === 'string')) : undefined;
    await daemon(`/inherent/claude-requests/${encodeURIComponent(str(b.req, 'req'))}`, { decision, ...answers ? { answers } : {}, ...typeof b.text === 'string' && b.text.trim() ? { message: b.text.trim() } : {} })
      .catch((e: unknown) => { throw new Http(409, /\b404\b/.test(String(e)) ? '它已经不在等了：终端那边答过了，或者它往下走了' : '没送到 Jarvis 后台：它开着吗？'); });
    return { ok: true };
  }
  if (m === 'POST' && url.pathname === '/sessions') {
    const b = await body(req), agent = b.agent === 'codex' ? 'codex' : 'claude', text = str(b.text, 'text').trim(), files = fileList(b.files), dirs = dirList(b.dirs);
    signedIn(agent);
    let cwd = pathOf(str(b.cwd, 'cwd'));
    if (!(await stat(cwd).catch(() => null))?.isDirectory()) throw new Http(400, '没有这个文件夹');
    if (!text && !files.length) throw new Http(400, '要它做什么？');
    const repo = await repoOf(cwd), from = typeof b.base === 'string' ? b.base.trim() : '';
    let branch = await branchOf(cwd), tree = false;
    if (b.tree && repo) { ({ cwd, branch } = await worktree(repo, text, from)); tree = true; }
    const cat = (await getCatalog())[agent];
    const s: Sess = { id: '', agent, title: oneLine(text || files[0]?.name || '新会话', 48), cwd, project: base(repo || cwd), branch, tree,
      st: 'work', pinned: false, parked: false, archived: false, unread: false, created: Date.now(), trace: [{ at: Date.now(), st: 'work' }], updated: Date.now(), summary: '在想',
      model: typeof b.model === 'string' ? b.model : cat.models[0]?.[0] ?? '', effort: typeof b.effort === 'string' ? b.effort : 'high',
      mode: typeof b.mode === 'string' ? b.mode : cat.modes[0]?.[0] ?? '', ctx: 0, ...dirs.length ? { dirs } : {}, ...tree && from ? { base: from } : {} };
    const x = new Session(s, repo);
    x.items = [];
    s.id = await x.driver.create(x);
    sessions.set(s.id, x);
    broadcast({ t: 'sess', s });
    save();
    // A new worktree's setup script runs first, so the answer comes back before it ends; a send that fails then shows
    // on the row.
    if (tree && settings.setup?.[repo]) void setup(x).then(() => x.driver.send(x, text, files)).catch(e => { log('first send', s.id, e); x.end(undefined, false, 'err', `没发出去：${oneLine(String(e instanceof Error ? e.message : e), 120)}`); });
    else await x.driver.send(x, text, files);
    return { id: s.id };
  }
  // ---- the workbench (ADR 0085, 0086): plan usage, Jarvis's services and logs, a terminal per session ----
  if (m === 'GET' && url.pathname === '/usage') return usage();
  if (m === 'GET' && url.pathname === '/services') return { services: await services() };
  if (m === 'POST' && parts[0] === 'services' && parts[2] === 'restart') {
    const label = LABEL[parts[1]];
    if (!label) throw new Http(404, '没有这个服务');
    if (!(await services()).some(v => v.name === parts[1] && v.loaded)) throw new Http(409, '这台 Mac 上没有装这个服务');
    // The companion takes the window with it; the one that comes up opens it again where it was.
    const b = await body(req);
    if (parts[1] === 'companion' && typeof b.id === 'string') { await mkdir(DIR, { recursive: true }); await writeFile(REOPEN(), JSON.stringify({ id: b.id, at: Date.now() })); }
    await exec('launchctl', ['kickstart', '-k', `gui/${process.getuid?.() ?? 501}/${label}`], { timeout: 60000 });
    return { ok: true };
  }
  if (m === 'GET' && url.pathname === '/logs') return logs(url.searchParams.get('name') ?? '', Number(url.searchParams.get('from') ?? -1));
  if (parts[0] === 'term' && parts[1]) {
    const x = need(parts[1]), verb = parts[2] ?? '';
    if (m === 'GET' && verb === 'stream') { streamTerm(x.s.id, req, res); return undefined; }
    if (m === 'DELETE' && !verb) { killTerm(x.s.id); return { ok: true }; }
    if (m !== 'POST') throw new Http(405, '不行');
    const b = await body(req), size = (v: unknown, lo: number) => Math.max(lo, Math.min(500, Math.round(Number(v)) || lo));
    if (!verb) {
      if (!existsSync(x.s.cwd)) throw new Http(409, '这个会话的文件夹已经不在了');
      await openTerm(x.s.id, x.s.cwd, size(b.cols, 20), size(b.rows, 4));
      return { ok: true };
    }
    if (verb === 'input') { if (typeof b.data === 'string' && b.data.length <= 65536) inputTerm(x.s.id, b.data); return { ok: true }; }
    if (verb === 'resize') { resizeTerm(x.s.id, size(b.cols, 20), size(b.rows, 4)); return { ok: true }; }
    throw new Http(404, '没有这个动作');
  }
  if (parts[0] !== 'sessions' || !parts[1]) throw new Http(404, '没有这个地方');
  const x = need(parts[1]), verb = parts[2] ?? '';
  await x.kept;
  if (m === 'GET' && !verb) { await x.ensureLoaded(); return { items: x.items, live: x.live }; }
  if (m === 'GET' && verb === 'peek') return peek(x.s.cwd, url.searchParams.get('ref') ?? '', () => baseFor(x), [x.s.cwd, ...x.s.dirs ?? []]);
  // A picture, sound, video or PDF of its folders, for the preview to show itself (anything after /file names it).
  if (m === 'GET' && verb === 'file') { await sendFile(res, [x.s.cwd, ...x.s.dirs ?? []], x.s.cwd, url.searchParams.get('ref') ?? '', req.headers.range); return undefined; }
  // ---- the review (B6): what it changed, one file's diff, one file put back ----
  if (m === 'GET' && verb === 'changes') return parts[3] === 'diff' ? fileDiff(x, url.searchParams.get('path') ?? '') : changes(x);
  if (m === 'GET' && verb === 'export') { await x.ensureLoaded(); return exported(x); }
  // What putting the files back to a point would change (B13).
  if (m === 'GET' && verb === 'rewind') {
    if (!x.driver.rewind) throw new Http(409, 'Codex 不记文件的检查点');
    if (x.s.term) throw new Http(409, '在终端里，先拿回来');
    signedIn(x.s.agent);
    await x.ensureLoaded();
    const p = pointOf(x, url.searchParams.get('at') ?? '');
    return { ...p.checkpoint ? await x.driver.rewind(x, p.checkpoint, true) : { can: true, files: [], add: 0, del: 0 }, kind: p.kind };
  }
  if (m === 'GET' && verb === 'tasks' && parts[3]) {
    const t = x.s.tasks?.find(y => y.id === parts[3]);
    if (!t) throw new Http(404, '没有这个任务');
    return { task: t, out: t.out && path.isAbsolute(t.out) ? await tail(t.out) : '' };
  }
  if (m === 'DELETE' && !verb) {
    const force = url.searchParams.get('force') === '1';
    let kept = '';
    killTerm(x.s.id);
    if (busy(x)) await x.driver.interrupt(x).catch(() => {});
    await x.driver.release(x).catch(() => {});
    // A fork shares its session's worktree: it goes with the last session in it.
    if (x.s.tree && x.repo && !x.s.gone && existsSync(x.s.cwd) && !sharing(x)) {
      // Git's own checks decide: a worktree with changes, or a branch with commits nothing else has, stays, unless
      // Allen says to delete it anyway; then everything in it is kept first.
      const merged = await git(x.repo, 'merge-base', '--is-ancestor', x.s.branch, 'HEAD').then(() => true, () => false);
      if (!merged && !force) throw new Http(409, `${x.s.branch} 上还有没合进去的提交：先合进去，或者确定就删（会先备份）`, 'force');
      if (force) {
        kept = await keep(x);
        await git(x.repo, 'worktree', 'remove', '--force', x.s.cwd);
        await git(x.repo, 'branch', '-D', x.s.branch).catch(() => {});
      } else {
        try { await git(x.repo, 'worktree', 'remove', x.s.cwd); } catch { throw new Http(409, 'worktree 里还有没提交的改动：先提交，或者确定就删（会先备份）', 'force'); }
        await git(x.repo, 'branch', '-d', x.s.branch).catch(() => {});
      }
    }
    // The agent can refuse too (Codex keeps a thread a fork still reads from): then the session stays.
    try { await x.driver.remove(x); } catch (e) { log('remove', x.s.id, e); throw new Http(409, `删不掉：${e instanceof Error ? e.message : String(e)}`); }
    sessions.delete(x.s.id);
    broadcast({ t: 'gone', id: x.s.id });
    save();
    return { ok: true, ...kept ? { kept } : {} };
  }
  if (m === 'GET' && verb === 'context') {
    if (x.s.term) throw new Http(409, '在终端里，拿回来才看得到');
    signedIn(x.s.agent);
    const c = await x.driver.context(x);
    // The ring takes the measured number.
    if (c.max) x.set({ ctx: Math.min(100, Math.round(c.used / c.max * 100)) });
    return c;
  }
  if (m === 'GET' && verb === 'mcp') {
    if (!x.driver.mcp) throw new Http(409, '看不到它的 MCP');
    if (x.s.term) throw new Http(409, '在终端里，拿回来才看得到');
    signedIn(x.s.agent);
    return { servers: await x.driver.mcp(x) };
  }
  if (m !== 'POST') throw new Http(405, '不行');
  const b = await body(req);
  if (verb === 'land') {
    // ADR 0097: start (or go on from a stop) by the way picked, stop after this step, the push's yes or no, let Claude
    // fix what stopped it, stay on the branch, and the commit title the owner wrote.
    const l = x.landing, a = b.action;
    if (a === 'start') { if (x.s.term) throw new Http(409, '在终端里，先拿回来'); l.start(b.via === 'merge' || b.via === 'pr' ? b.via : undefined); }
    else if (a === 'resume') l.resume();
    else if (a === 'stop') l.stop();
    else if (a === 'allow') l.allow();
    else if (a === 'deny') l.deny();
    else if (a === 'stay') l.stay();
    else if (a === 'msg') l.message(str(b.msg, 'msg'));
    else if (a === 'fix') { if (x.s.term) throw new Http(409, '在终端里，先拿回来'); signedIn(x.s.agent); await l.fix(); }
    else throw new Http(400, '没有这个动作');
    return { ok: true };
  }
  if (verb === 'send') {
    if (x.s.term) throw new Http(409, '在终端里，先拿回来');
    if (x.s.gone || !existsSync(x.s.cwd)) throw new Http(409, '这个会话已经落地，它的 worktree 清掉了：开个新会话接着做');
    const text = str(b.text, 'text').trim(), files = fileList(b.files);
    if (!text && !files.length) return { ok: true };
    // The model, the effort and plan mode typed as a command: the host sets them, as the menus do (B9).
    const cmd = files.length ? null : /^\/(model|effort|reasoning|plan)(?:\s+(\S+))?$/.exec(text);
    if (cmd && (cmd[1] === 'plan' || cmd[2])) { await typed(x, cmd[1], cmd[2] ?? ''); return { ok: true }; }
    signedIn(x.s.agent);
    await x.ensureLoaded();
    x.set({ unread: false, updated: Date.now() });
    if (x.s.archived && x.s.vers) await current(x);
    await x.driver.send(x, carry(x, text), files);
    return { ok: true };
  }
  // A reaction of yours on a message, on or off (m-rx): kept here, never sent by itself.
  if (verb === 'rx') {
    const k = b.k === 'it' ? 'it' : 'you', at = str(b.at, 'at'), e = str(b.e, 'e');
    if (!RX.includes(e)) throw new Http(400, '没有这个表情');
    await x.ensureLoaded();
    if (!x.items?.some(it => it.k === k && it.id === at)) throw new Http(404, '这个会话里没有这一句');
    const key = `${k}:${at}`, r = x.s.rx?.[key] ?? {}, on = !r.mine?.includes(e), rx = { ...x.s.rx };
    const mine = on ? [...r.mine ?? [], e] : (r.mine ?? []).filter(y => y !== e), sent = (r.sent ?? []).filter(y => y !== e);
    const next: Rx = { ...mine.length ? { mine } : {}, ...sent.length ? { sent } : {}, ...r.by ? { by: r.by } : {} };
    if (Object.keys(next).length) rx[key] = next; else delete rx[key];
    x.set({ rx: Object.keys(rx).length ? rx : undefined });
    return { ok: true, rx: next };
  }
  if (verb === 'edit') return { id: await edit(x, b) };
  if (verb === 'answer') {
    const open = x.pending()?.req;
    if (!open || open.id !== b.req) throw new Http(409, '这张请求已经处理过了');
    // A form (C3) can also be cancelled, and what was filled in must fit its fields before it goes.
    const form = open.tool === 'Form' ? open : null;
    const decision = b.decision === 'deny' || (b.decision === 'cancel' && !form) ? 'deny' : b.decision === 'cancel' ? 'cancel' : b.decision === 'always' ? 'always' : 'allow';
    const values = form && (decision === 'allow' || decision === 'always') ? contentOf(form.fields, b.values && typeof b.values === 'object' ? b.values : {}) : undefined;
    if (typeof values === 'string') throw new Http(400, values);
    const a: Answer = { req: str(b.req, 'req'), decision, answers: Array.isArray(b.answers) ? b.answers.map((q: unknown) => Array.isArray(q) ? q.map(String) : []) : undefined,
      text: typeof b.text === 'string' ? b.text : undefined, ...values ? { values } : {} };
    x.driver.answer(x, a);
    return { ok: true };
  }
  // An MCP server switched on or off, connected again, or signed in to (C3): the list after it, or the page to open.
  if (verb === 'mcp') {
    const act: McpAct | null = b.action === 'on' || b.action === 'off' || b.action === 'reconnect' || b.action === 'login' ? b.action : null;
    if (!act) throw new Http(400, '没有这个动作');
    if (!x.driver.mcpAct) throw new Http(409, '管不了它的 MCP');
    if (x.s.term) throw new Http(409, '在终端里，先拿回来');
    signedIn(x.s.agent);
    const r = await x.driver.mcpAct(x, str(b.name, 'name'), act);
    return Array.isArray(r) ? { ok: true, servers: r } : r;
  }
  // A question on the side (C7): the answer comes back to this request, and the session's own turn goes on. `history`
  // is [question, answer] pairs of this side talk; closing the request drops the question.
  if (verb === 'side') {
    if (!x.driver.side) throw new Http(409, '侧问不了');
    if (x.s.term) throw new Http(409, '在终端里，先拿回来');
    if (x.s.gone || !existsSync(x.s.cwd)) throw new Http(409, '这个会话已经落地，它的 worktree 清掉了');
    const text = str(b.text, 'text').trim();
    if (!text) throw new Http(400, '要问什么？');
    signedIn(x.s.agent);
    await x.ensureLoaded();
    if (!x.items?.some(i => i.k === 'you')) throw new Http(409, '先说一句，才能侧问');
    const history = (Array.isArray(b.history) ? b.history : []).filter((h: unknown): h is [string, string] => Array.isArray(h) && typeof h[0] === 'string' && typeof h[1] === 'string').slice(-20);
    const gone = new AbortController();
    res.once('close', () => { if (!res.writableFinished) gone.abort(); });
    try { return { text: await x.driver.side(x, text, history, gone.signal) }; }
    catch (e) { if (gone.signal.aborted) throw new Http(499, '不问了'); throw e; }
  }
  if (verb === 'interrupt') { if (busy(x)) await x.driver.interrupt(x); return { ok: true }; }
  if (verb === 'stop') {
    if (x.s.st === 'work' || x.s.st === 'wait') await x.driver.interrupt(x).catch(() => {});
    await x.driver.release(x);
    x.set({ stopped: true, st: x.s.st === 'err' ? 'err' : 'done', now: undefined });
    return { ok: true };
  }
  if (verb === 'set') {
    const k: 'model' | 'effort' | 'mode' | null = b.key === 'model' || b.key === 'effort' || b.key === 'mode' ? b.key : null;
    if (!k) throw new Http(400, 'key 不对');
    await setKey(x, k, str(b.value, 'value'));
    return { ok: true };
  }
  if (verb === 'meta') {
    const p: Partial<Sess> = {};
    if (typeof b.pinned === 'boolean') p.pinned = b.pinned;
    if (typeof b.archived === 'boolean') { p.archived = b.archived; if (b.archived) Object.assign(p, { pinned: false, parked: false }); }
    if (typeof b.parked === 'boolean') p.parked = b.parked;
    // A name Allen gives is never replaced by one the agent generates (B14).
    if (typeof b.title === 'string' && b.title.trim()) { p.title = oneLine(b.title, 80); p.named = true; await x.driver.rename(x, p.title).catch(e => log('rename', x.s.id, e)); }
    if (b.seen === true) p.unread = false;
    if (p.archived && (x.s.st === 'work' || x.s.st === 'wait')) { await x.driver.interrupt(x).catch(() => {}); await x.driver.release(x).catch(() => {}); }
    x.set(p);
    return { ok: true };
  }
  if (verb === 'fork') return { id: await fork(x, b) };
  // Files put back as they were at a point, the conversation staying as it is (B13).
  if (verb === 'rewind') {
    if (!x.driver.rewind) throw new Http(409, 'Codex 不记文件的检查点');
    if (x.s.term) throw new Http(409, '在终端里，先拿回来');
    if (busy(x)) throw new Http(409, '它还在干活，先打断');
    signedIn(x.s.agent);
    await x.ensureLoaded();
    const p = pointOf(x, str(b.at, 'at'));
    if (!p.checkpoint) return { can: true, files: [], add: 0, del: 0 };
    const r = await x.driver.rewind(x, p.checkpoint, false);
    if (!r.can) throw new Http(409, r.why ? `文件回不去：${r.why}` : '文件回不去了');
    x.note(`文件退回到了${p.kind === 'you' ? '你发这一句之前' : '这个回答结束时'}的样子 · ${r.files.length} 个文件`);
    void x.measure();
    return r;
  }
  if (verb === 'changes' && parts[3] === 'revert') {
    if (busy(x)) throw new Http(409, '它还在干活，先打断再撤');
    const r = await revert(x, str(b.path, 'path'), TRASH);
    void x.measure();
    return r;
  }
  // A message sent while it worked, taken back before the agent took it (B11).
  if (verb === 'queue') {
    if (b.action !== 'cancel') throw new Http(400, '没有这个动作');
    if (!x.driver.unqueue) throw new Http(409, 'Codex 收下就放进这一轮了，撤不回来');
    if (!await x.driver.unqueue(x, str(b.text, 'text'))) throw new Http(409, '它已经收下了，撤不回来');
    // The reactions it carried wait for the next message again.
    const took = x.carrying.get(b.text), rx = { ...x.s.rx };
    if (took) {
      x.carrying.delete(b.text);
      for (const [k, e] of took) if (rx[k]) rx[k] = { ...rx[k], sent: rx[k].sent?.filter(y => y !== e) };
      x.set({ rx });
    }
    return { ok: true };
  }
  if (verb === 'tasks' && parts[3] && parts[4] === 'stop') {
    if (!x.s.tasks?.some(y => y.id === parts[3] && y.st === 'run')) throw new Http(404, '没有这个在跑的任务');
    if (!x.driver.stopTask) throw new Http(409, '停不了');
    await x.driver.stopTask(x, parts[3]);
    return { ok: true };
  }
  if (verb === 'dirs') {
    if (x.s.agent === 'claude' && busy(x)) throw new Http(409, '它还在干活，这一轮做完再加');
    const dirs = dirList(b.dirs);
    x.set({ dirs: dirs.length ? dirs : undefined });
    // Claude Code reads them as it starts, so the next message starts it again with them; Codex takes them each turn.
    if (x.s.agent === 'claude') await x.driver.release(x);
    if (x.items) x.note(dirs.length ? `它也能动这些文件夹了：${dirs.map(d => d.replace(homedir(), '~')).join('、')}` : '它只动自己的文件夹了');
    return { ok: true, dirs };
  }
  if (verb === 'release') {
    if (x.s.st === 'work') throw new Http(409, '它还在干活，等这一步做完或先打断');
    await x.driver.release(x);
    x.set({ term: true });
    await x.ensureLoaded();
    x.note(`在终端里打开 · ${x.driver.resume(x)}`);
    return { cwd: x.s.cwd, cmd: x.driver.resume(x) };
  }
  if (verb === 'takeback') {
    x.set({ term: false, stopped: false });
    await x.driver.load(x);
    x.note('回到 Jarvis · 接着终端停下的地方');
    const last = [...x.items ?? []].reverse().find(it => it.k === 'it');
    x.set({ summary: last?.k === 'it' ? firstSentence(last.text) : x.s.summary, updated: Date.now() });
    return { ok: true };
  }
  throw new Http(404, '没有这个动作');
}

// ---------- what the routes do ----------
async function setKey(x: Session, k: 'model' | 'effort' | 'mode', v: string) {
  if (x.s[k] === v) return;
  await x.driver.set(x, k, v);
  x.set({ [k]: v });
  if (x.items) x.note(`${k === 'model' ? '模型' : k === 'effort' ? '力度' : '模式'}换成 ${label(x.s.agent, k, v)}${x.s.st === 'work' ? ' · 从下一步开始' : ''}`);
}
// `/model x` (a model's id or name, or part of one), `/effort x` (`/reasoning x`, as Codex says it) and `/plan`.
async function typed(x: Session, name: string, arg: string) {
  const c = (await getCatalog())[x.s.agent], low = arg.toLowerCase();
  if (name === 'plan') return setKey(x, 'mode', 'plan');
  if (name === 'model') {
    const hit = c.models.find(([v, l]) => v.toLowerCase() === low || l.toLowerCase() === low) ?? c.models.find(([v, l]) => v.toLowerCase().includes(low) || l.toLowerCase().includes(low));
    if (!hit) throw new Http(400, `没有 ${arg} 这个模型`);
    return setKey(x, 'model', hit[0]);
  }
  const e = c.efforts.find(v => v.toLowerCase() === low);
  if (!e) throw new Http(400, `力度只有 ${c.efforts.join('、')}`);
  return setKey(x, 'effort', e);
}
// A point of the conversation (what you said, or an answer, by its `id`) and the checkpoint its files go back to: what
// you said goes back to before it, an answer to the next thing you said, or with nothing after it to where they are now.
function pointOf(x: Session, at: string) {
  const items = x.items ?? [], i = at ? items.findIndex(it => (it.k === 'you' || it.k === 'it') && it.id === at) : -1;
  if (i < 0) throw new Http(404, '这个会话里没有这一句');
  if (items[i].k === 'you') return { kind: 'you' as const, checkpoint: at };
  const next = items.slice(i + 1).find((it): it is Item & { k: 'you' } => it.k === 'you' && !!it.id);
  return { kind: 'it' as const, checkpoint: next?.id ?? null };
}
// A new session from this one (B13): the whole conversation, or up to a point (`at`; `before` leaves that point
// out), with the files put back as they were there (`code`, Claude's checkpoints), this session archived (`archive`), and
// a first message (`text`, `files`). With nothing before the point, the new session starts empty and needs that message.
// `back` (/rewind): the same conversation gone back to before that point rather than a second one: it keeps the name, its
// place in the list and its pins, and one quiet line says what went back instead of the fork line.
async function fork(x: Session, b: Record<string, any>) {
  await x.ensureLoaded();
  const at = typeof b.at === 'string' && b.at ? b.at : undefined, before = b.before === true, code = b.code === true;
  const text = typeof b.text === 'string' ? b.text.trim() : '', files = fileList(b.files), point = at ? pointOf(x, at) : null;
  const back = b.back === true, said = oneLine((x.items ?? []).find((it): it is Item & { k: 'you' } => it.k === 'you' && it.id === at)?.text ?? '', 18);
  if (code && !x.driver.rewind) throw new Http(409, 'Codex 不记文件的检查点，只能分叉对话');
  if (code && busy(x)) throw new Http(409, '它还在干活，先打断再退文件');
  if (code || text || files.length) signedIn(x.s.agent);
  const cp = code ? point?.checkpoint ?? null : null;
  if (cp) { const d = await x.driver.rewind!(x, cp, true); if (!d.can) throw new Http(409, d.why ? `文件回不去：${d.why}` : '文件回不去了'); }
  const id = await x.driver.fork(x, at, before);
  if (!id && !text && !files.length) throw new Http(409, '这是第一句，前面没有可以留下的：写一句新的发出去');
  const f = new Session({ ...x.s, id: id ?? '', title: `${x.s.title}（分叉）`, named: false, pinned: false, parked: false, archived: false, unread: false, st: 'done', updated: Date.now(),
    trace: [...x.s.trace ?? [], { at: Date.now(), st: 'done' }],
    now: undefined, since: undefined, queue: undefined, stopped: undefined, term: undefined, resets: undefined, tasks: undefined, bg: undefined, land: undefined }, x.repo);
  if (id) { sessions.set(id, f); await f.ensureLoaded(); }
  else { f.items = []; f.s.id = await f.driver.create(f); sessions.set(f.s.id, f); }
  if (back) f.set({ title: x.s.title, named: x.s.named, pinned: x.s.pinned, parked: x.s.parked });
  else f.note(`从「${x.s.title}」${at ? `的${before ? '这一句之前' : '这一句'}` : ''}分叉 · 两边各走各的，用的是同一个文件夹`);
  broadcast({ t: 'sess', s: f.s });
  save();
  if (cp) { const r = await x.driver.rewind!(x, cp, false); f.note(back ? `退回到你说「${said}」之前 · 对话和 ${r.files.length} 个文件` : `文件退回到了那时的样子 · ${r.files.length} 个文件`); void x.measure(); }
  else if (back) f.note(`退回到你说「${said}」之前 · 只退了对话`);
  if (b.archive === true) {
    if (busy(x)) await x.driver.interrupt(x).catch(() => {});
    await x.driver.release(x).catch(() => {});
    x.set({ archived: true, pinned: false, parked: false });
  }
  if (text || files.length) await f.driver.send(f, text, files);
  return f.s.id;
}
// The reactions still waiting (m-rx) ride with a message that starts a turn, or that Claude keeps in its queue: one line
// in front of your words, naming each message by its first words; from then on they count as carried. Codex takes a
// message sent while it works into the running turn, so there they wait for the next one; a command goes on its own.
function carry(x: Session, text: string) {
  if (text.startsWith('/') || (x.s.agent === 'codex' && busy(x))) return text;
  const items = x.items ?? [], rx = { ...x.s.rx }, parts: string[] = [], took: [string, string][] = [];
  for (const [key, r] of Object.entries(rx)) {
    const wait = (r.mine ?? []).filter(e => !r.sent?.includes(e)), k = key.slice(0, key.indexOf(':')), id = key.slice(k.length + 1);
    const it = wait.length ? items.find((i): i is Item & { k: 'you' | 'it' } => (i.k === 'you' || i.k === 'it') && i.k === k && i.id === id) : undefined;
    if (!it) continue;
    const words = oneLine(it.text.replace(/[*`#>]/g, ''), 32).replace(/"/g, "'");
    parts.push(...wait.map(e => `${e} on ${k === 'it' ? 'your reply' : 'my message'} "${words}"`));
    rx[key] = { ...r, sent: [...r.sent ?? [], ...wait] };
    took.push(...wait.map(e => [key, e] as [string, string]));
  }
  if (!parts.length) return text;
  x.set({ rx });
  if (busy(x)) x.carrying.set(text, took);
  return `[Reactions: ${parts.join('; ')}]\n\n${text}`;
}
// The same messages in two versions of a conversation: the n-th thing you said and the n-th answer, by their keys.
function same(a: Item[], b: Item[]) {
  const out = new Map<string, string>();
  for (const k of ['you', 'it'] as const) {
    const ids = (xs: Item[]) => xs.map(it => it.k === k ? it.id : undefined).filter((id): id is string => !!id), ys = ids(b);
    ids(a).forEach((id, n) => { if (ys[n]) out.set(`${k}:${id}`, `${k}:${ys[n]}`); });
  }
  return out;
}
// What you said, changed and sent again (m-edit): a running turn stops, the conversation goes back to before that message
// (Claude: the files too, from its checkpoints; Codex: the conversation only), and the new words go with the old
// message's pictures. The new session reads as the same conversation, with its title, place in the list, marks and the
// reactions on what stays; this one is archived as the version before it, and the page steps between them (m-ver).
async function edit(x: Session, b: Record<string, any>) {
  if (x.s.term) throw new Http(409, '在终端里，先拿回来');
  if (x.s.gone || !existsSync(x.s.cwd)) throw new Http(409, '这个会话已经落地，它的 worktree 清掉了：开个新会话接着做');
  const at = str(b.at, 'at'), text = str(b.text, 'text').trim();
  if (!text) throw new Http(400, '要改成什么？');
  signedIn(x.s.agent);
  await x.ensureLoaded();
  const items = x.items ?? [], i = items.findIndex(it => it.k === 'you' && it.id === at);
  if (i < 0) throw new Http(404, '这个会话里没有这一句');
  // An older version edited becomes the current one first.
  if (x.s.archived && x.s.vers) await current(x);
  const old = items[i] as Item & { k: 'you' }, was = busy(x), marks = { pinned: x.s.pinned, parked: x.s.parked };
  // Archived first, so the turn it stops is not news anywhere; what it had queued is taken back before the stop, so it
  // never runs. Its agent is let go only at the end: the checkpoints are read through the one it has.
  const queued = x.s.queue ?? [];
  x.set({ archived: true, pinned: false, parked: false, queue: undefined });
  let f: Session, d: Awaited<ReturnType<NonNullable<Driver['rewind']>>> | null = null;
  try {
    if (was) {
      for (const q of queued) await x.driver.unqueue?.(x, q).catch(() => false);
      await x.driver.interrupt(x).catch(() => {});
      // Its record has the stopped turn in it before anything is cut from it.
      for (let n = 0; n < 50 && busy(x); n++) await new Promise(ok => setTimeout(ok, 100));
    }
    if (x.driver.rewind) d = await x.driver.rewind(x, at, true).catch(e => { log('edit rewind', x.s.id, e); return null; });
    const id = await x.driver.fork(x, at, true, x.s.title);
    f = new Session({ ...x.s, ...marks, id: id ?? '', archived: false, unread: false, st: 'done', updated: Date.now(), now: undefined, since: undefined, queue: undefined,
      stopped: undefined, term: undefined, resets: undefined, tasks: undefined, bg: undefined, land: undefined, dirty: undefined, rx: undefined, vers: undefined }, x.repo);
    if (id) { sessions.set(id, f); await f.ensureLoaded(); }
    else { f.items = []; f.s.id = await f.driver.create(f); sessions.set(f.s.id, f); }
  } catch (e) { x.set({ archived: false, ...marks }); throw e; }
  // What stays keeps its reactions and the versions of what was edited before; this message gets a family of versions.
  const map = same(items.slice(0, i), f.items ?? []), root = x.s.vers?.root ?? x.s.id, had = x.s.vers?.at[`you:${at}`], fam = had?.[0] ?? `${x.s.id}:${at}`;
  const n = Math.max(1, ...[...sessions.values()].flatMap(o => Object.values(o.s.vers?.at ?? {})).filter(v => v[0] === fam).map(v => v[1])) + 1, keep = { root, at: {} as Record<string, [string, number]> };
  f.s.rx = Object.fromEntries(Object.entries(x.s.rx ?? {}).filter(([k]) => map.has(k)).map(([k, v]) => [map.get(k)!, v]));
  for (const [k, v] of Object.entries(x.s.vers?.at ?? {})) if (map.has(k)) keep.at[map.get(k)!] = v;
  f.s.vers = keep;
  x.set({ vers: { root, at: { ...x.s.vers?.at, [`you:${at}`]: had ?? [fam, 1] } }, ...was ? { st: 'done' as St, now: undefined, since: undefined } : {} });
  let back = 0;
  if (d?.can) back = (await x.driver.rewind!(x, at, false)).files.length;
  await x.driver.release(x).catch(() => {});
  f.note(['改过这一句', was && '那一轮停下了', !x.driver.rewind ? 'Codex 只回退对话，文件不动' : !d?.can ? '这一句没有文件的检查点，文件还是现在的样子'
    : back ? `之后改的 ${back} 个文件回去了` : ''].filter(Boolean).join(' · '));
  broadcast({ t: 'sess', s: f.s });
  save();
  if (f.s.parked) markOut(f.s.id, { park: true });
  void x.measure(); void f.measure();
  // The old message's pictures go with the new words.
  const pics = (await Promise.all((old.files ?? []).filter(p => p.img).map(async p => {
    const buf = await readFile(path.join(IMAGES, p.img!)).catch(() => null);
    return buf ? { name: p.name, url: `data:${mimeOf(p.img!)};base64,${buf.toString('base64')}` } : null;
  }))).filter((p): p is File & { url: string } => !!p);
  await f.driver.send(f, carry(f, text), pics);
  const you = [...f.items ?? []].reverse().find((it): it is Item & { k: 'you' } => it.k === 'you');
  if (you?.id) f.set({ vers: { root, at: { ...keep.at, [`you:${you.id}`]: [fam, n] } } });
  return f.s.id;
}
// Writing in an older version of a conversation you edited makes it the current one again (m-ver): it comes back into the
// list with the title and marks of the version that was there, and that one is archived.
async function current(x: Session) {
  const p: Partial<Sess> = { archived: false };
  for (const o of sessions.values()) {
    if (o === x || o.s.archived || o.s.vers?.root !== x.s.vers?.root) continue;
    const was = busy(o);
    Object.assign(p, { pinned: o.s.pinned, parked: o.s.parked, title: o.s.title, named: o.s.named });
    o.set({ archived: true, pinned: false, parked: false, queue: undefined });
    if (was) await o.driver.interrupt(o).catch(() => {});
    await o.driver.release(o).catch(() => {});
    if (was) o.set({ st: 'done', now: undefined, since: undefined });
  }
  if (p.title && p.title !== x.s.title) await x.driver.rename(x, p.title).catch(e => log('rename', x.s.id, e));
  x.set(p);
}
// Before a worktree goes against git's own checks: everything in it that git does not ignore, committed or not, as one
// commit on top of its branch under refs/startrail/trash/ (`git log <ref>` shows it, `git branch <name> <ref>` brings
// it back).
async function keep(x: Session) {
  const idx = path.join(DIR, `trash-index-${process.pid}-${Date.now()}`), env = { ...process.env, GIT_INDEX_FILE: idx };
  const g = async (...a: string[]) => (await exec('git', ['-C', x.s.cwd, '-c', 'user.name=Startrail', '-c', 'user.email=startrail@localhost', ...a], { env, maxBuffer: 64 << 20 })).stdout.trim();
  try {
    await g('read-tree', 'HEAD');
    await g('add', '-A');
    const commit = await g('commit-tree', await g('write-tree'), '-p', 'HEAD', '-m', `startrail: kept when "${x.s.title}" was deleted`);
    const ref = `refs/startrail/trash/${x.s.branch.replace(/[^\w.-]+/g, '-')}-${Date.now()}`;
    await g('update-ref', ref, commit);
    return ref;
  } finally { await unlink(idx).catch(() => {}); }
}
const TRASH = path.join(DIR, 'trash');
// The last 64 kB a background task wrote.
async function tail(file: string) {
  const fh = await open(file, 'r').catch(() => null);
  if (!fh) return '';
  try { const size = (await fh.stat()).size, n = Math.min(size, 64e3), b = Buffer.alloc(n); await fh.read(b, 0, n, size - n); return b.toString('utf8'); }
  finally { await fh.close(); }
}
// Sessions started outside the window (a terminal, Codex's own app) in a folder, or everywhere, that the window does not
// have; one that moved in the last two minutes may still be open there (B12, ADR 0096).
const RECENT = 120e3;
async function outsideOf(cwd: string): Promise<Outside[]> {
  const known = new Set([...sessions.values()].flatMap(x => [x.s.id, ...x.s.resets ?? []]));
  const lists = await Promise.all((Object.keys(DRIVERS) as Agent[]).map(a => DRIVERS[a].outside(cwd).catch(e => { log('outside', a, String(e)); return [] as Outside[]; })));
  return lists.flat().filter(o => !known.has(o.id)).map(o => Date.now() - o.updated < RECENT ? { ...o, recent: true } : o).sort((a, b) => b.updated - a.updated).slice(0, 200);
}
// One of them as its transcript has it, read into a row nobody lists: the window shows it and nothing here changes.
async function readOutside(agent: Agent, id: string, cwd: string) {
  if ([...sessions.values()].some(x => x.s.id === id || x.s.resets?.includes(id))) throw new Http(409, '这个会话已经在列表里了');
  const o = (await DRIVERS[agent].outside(cwd)).find(y => y.id === id);
  if (!o) throw new Http(404, '找不到这个会话');
  const x = new Session({ id, agent, title: o.title, cwd: o.cwd, project: base(o.cwd), branch: o.branch ?? '', tree: false, st: 'done', pinned: false, parked: false,
    archived: false, unread: false, updated: o.updated, summary: '', model: '', effort: '', mode: '', ctx: 0 }, '');
  await x.driver.load(x);
  return x.items ?? [];
}
// The daemon's board (ADR 0046): the Claude Code sessions this window does not hold that are running or waiting now.
async function liveOutside(): Promise<Live[]> {
  const known = new Set([...sessions.values()].flatMap(x => [x.s.id, ...x.s.resets ?? []]));
  const board = await daemon('/inherent/claude-sessions').catch(() => null) as { sessions?: Record<string, any>[] } | null;
  return (board?.sessions ?? []).filter(r => typeof r.session_id === 'string' && !known.has(r.session_id)).map((r): Live => {
    const q = r.request && typeof r.request.id === 'string' ? r.request : null;
    return { id: r.session_id, st: q || r.phase === 'needs_input' ? 'wait' : r.phase === 'working' ? 'work' : 'done',
      ...q ? { req: heldReq(q.id, String(q.tool ?? ''), q.input && typeof q.input === 'object' ? q.input : {}, String(q.cwd || r.cwd || ''), !!q.always) } : {} };
  });
}
// One of them, taken in: read back from its own transcript, it goes on here; a recent one takes a second, sure press.
async function take(agent: Agent, id: string, cwd: string, force: boolean) {
  if ([...sessions.values()].some(x => x.s.id === id || x.s.resets?.includes(id))) throw new Http(409, '这个会话已经在列表里了');
  const o = (await DRIVERS[agent].outside(cwd)).find(y => y.id === id) ?? (cwd ? (await DRIVERS[agent].outside('')).find(y => y.id === id) : undefined);
  if (!o) throw new Http(404, '找不到这个会话');
  if (!force && Date.now() - o.updated < RECENT) throw new Http(409, '它两分钟内还在别处动过，可能还开着：先在那边关掉，确定就再点一次', 'force');
  if (!(await stat(o.cwd).catch(() => null))?.isDirectory()) throw new Http(409, '它的文件夹已经不在了');
  const repo = await repoOf(o.cwd), cat = (await getCatalog())[agent];
  // A worktree where Claude Code makes its own (`claude --worktree`) lands like one this window made.
  const tree = !!repo && o.cwd.startsWith(`${path.join(repo, '.claude', 'worktrees')}/`);
  const s: Sess = { id, agent, title: o.title, cwd: o.cwd, project: base(repo || o.cwd), branch: await branchOf(o.cwd), tree,
    st: 'done', pinned: false, parked: false, archived: false, unread: false, created: o.updated, trace: [{ at: Date.now(), st: 'done' }], updated: Date.now(),
    summary: '从别处接手', model: cat.models[0]?.[0] ?? '', effort: 'high', mode: cat.modes[0]?.[0] ?? '', ctx: 0 };
  const x = new Session(s, repo);
  sessions.set(id, x);
  await x.ensureLoaded();
  const last = [...x.items ?? []].reverse().find(it => it.k === 'it');
  if (last?.k === 'it') s.summary = firstSentence(last.text);
  x.note(`从${agent === 'claude' ? '终端' : ' Codex '}接手 · 在这里接着聊，那边别同时开着`);
  broadcast({ t: 'sess', s });
  save();
  void x.measure();
  return id;
}
// Every session's title, summary and conversation (read now if nobody has opened it yet); with `all`, archived ones too.
async function search(q: string, all: boolean) {
  const want = q.trim().toLowerCase(), hits: { id: string; item?: number; text: string }[] = [];
  if (!want) return hits;
  for (const x of [...sessions.values()].sort((a, b) => b.s.updated - a.s.updated)) {
    if ((x.s.archived && !all) || hits.length >= 200) continue;
    if (`${x.s.title}\n${x.s.summary}`.toLowerCase().includes(want)) hits.push({ id: x.s.id, text: x.s.title });
    await x.kept;
    await x.ensureLoaded();
    (x.items ?? []).forEach((it, i) => {
      const j = (it.k === 'you' || it.k === 'it') && hits.length < 200 ? it.text.toLowerCase().indexOf(want) : -1;
      if (j >= 0) hits.push({ id: x.s.id, item: i, text: oneLine(`${j > 40 ? '…' : ''}${(it as { text: string }).text.slice(Math.max(0, j - 40), j + want.length + 80)}`, 160) });
    });
  }
  return hits;
}
// The whole conversation as Markdown (C2): what you said, its answers, each step on one line.
const STEP_NAME: Record<Step['k'], string> = { read: '读', edit: '改', bash: '跑', search: '搜', agent: '子任务', web: '网页', tool: '工具', say: '说', think: '想' };
function exported(x: Session) {
  const who = x.s.agent === 'claude' ? 'Claude' : 'Codex', when = (at?: number) => at ? ` · ${new Date(at).toLocaleString('zh-CN', { hour12: false })}` : '';
  const out = [`# ${x.s.title}`, '', `${who} · ${x.s.cwd.replace(homedir(), '~')}${x.s.branch ? ` · ${x.s.branch}` : ''}`, ''];
  for (const it of x.items ?? []) {
    if (it.k === 'you') out.push(`## 你${when(it.at)}`, '', it.text, ...it.files?.length ? ['', `附带：${it.files.map(f => f.name).join('、')}`] : [], '');
    else if (it.k === 'it') out.push(`## ${who}${when(it.at)}`, '', it.text, '');
    else if (it.k === 'steps') out.push(...it.steps.map(st => `- ${STEP_NAME[st.k]} ${oneLine(st.t, 200)}`), '');
    else if (it.k === 'plan') out.push(...it.todos.map(([t, d]) => `- [${d === 2 ? 'x' : ' '}] ${t}`), '');
    else if (it.k === 'req') out.push(`> ${reqLine(it.req)}${it.done ? ` · ${it.done}` : ''}`, '');
    else out.push(`> ${it.text}`, '');
  }
  return { name: `${x.s.title.replace(/[/\\:*?"<>|]+/g, ' ').trim().slice(0, 80) || 'session'}.md`, text: out.join('\n') };
}
// The check-up (A4): the PATH the host searches, which Claude Code it runs and who it is signed in as, Codex and git
// where found, and whether the daemon answers.
async function doctor(): Promise<Doctor> {
  const exe = claudeExe();
  const [cv, cx, gx, up] = await Promise.all([exe ? version(exe) : undefined, which('codex'), which('git'),
    fetch(`${DAEMON}/api/health`, { signal: AbortSignal.timeout(2000) }).then(r => r.ok, () => false)]);
  const [xv, gv, acct, ca] = await Promise.all([cx ? version(cx) : undefined, gx ? version(gx) : undefined,
    cx ? codex.account().then(a => ({ a }), (e: unknown) => ({ e: oneLine(e instanceof Error ? e.message : String(e), 200) })) : { a: null },
    claude.account().catch(() => null)]);
  return { packaged: PACKAGED, path: (process.env.PATH ?? '').split(':').filter(Boolean),
    claude: { exe, ...cv ? { version: cv } : {}, own: !!exe && !exe.includes('claude-agent-sdk-'), auth: auth(), ...ca && auth().ready ? { account: ca } : {} },
    codex: { found: !!cx, ...cx ? { path: cx } : {}, ...xv ? { version: xv } : {}, ...'a' in acct ? { account: acct.a } : { error: acct.e } },
    git: { found: !!gx, ...gv ? { version: gv } : {} }, daemon: { up } };
}

// ---------- what the workbench reads ----------
// The daemon's last reading of the plans (ADR 0018), for the composer's ring.
async function usage(): Promise<Usage> {
  const r = await daemon('/inherent/usage').catch(() => null) as { services?: Record<string, { status?: string; data?: { plan?: string; windows?: UsageWindow[] } }> } | null;
  const out: Usage = {};
  for (const a of ['claude', 'codex'] as Agent[]) { const v = r?.services?.[a]; if (v?.status === 'ok' && v.data?.windows) out[a] = { plan: v.data.plan ?? '', windows: v.data.windows }; }
  return out;
}
// Jarvis's two LaunchAgents as launchd sees them: running or not, and since when.
async function services(): Promise<Service[]> {
  return Promise.all((Object.entries(LABEL) as [Service['name'], string][]).map(async ([name, label]) => {
    const out = await exec('launchctl', ['print', `gui/${process.getuid?.() ?? 501}/${label}`], { timeout: 5000 }).then(r => r.stdout, () => '');
    const pid = Number(/\bpid = (\d+)/.exec(out)?.[1]) || undefined;
    const started = pid ? await exec('ps', ['-o', 'lstart=', '-p', String(pid)], { timeout: 5000 }).then(r => Date.parse(r.stdout.trim()), () => NaN) : NaN;
    return { name, label, loaded: !!out, running: /\bstate = running/.test(out), ...(pid ? { pid } : {}), ...(Number.isFinite(started) ? { since: started } : {}) };
  }));
}
// The logs the 日志 tab reads: from a byte offset on, or the last 48 kB to start with.
const LOGS: Record<string, string> = { companion: 'resonance.out.log', 'companion 错误': 'resonance.err.log', daemon: 'daemon.err.log', 后台: 'agents-host.out.log' };
async function logs(name: string, from: number) {
  const file = LOGS[name] ?? LOGS.companion, p = path.join(ROOT, 'logs', file), size = (await stat(p).catch(() => null))?.size ?? 0;
  if (from >= 0 && from === size) return { name, file, size, text: '' };
  const start = from >= 0 && from < size ? from : Math.max(0, size - 48e3);
  const fh = await open(p, 'r').catch(() => null);
  if (!fh) return { name, file, size: 0, text: '' };
  try { const b = Buffer.alloc(Math.min(size - start, 256e3)); await fh.read(b, 0, b.length, start); let t = b.toString('utf8'); if (start && from < 0) t = t.slice(t.indexOf('\n') + 1); return { name, file, size: start + b.length, text: t }; }
  finally { await fh.close(); }
}
function label(agent: Agent, k: 'model' | 'effort' | 'mode', v: string) {
  const c = catalog?.[agent];
  return (k === 'model' ? c?.models : k === 'mode' ? c?.modes : undefined)?.find(x => x[0] === v)?.[1] ?? v;
}

// B23: the window gets the list as soon as the rows are back. A conversation is read when something opens it; a child
// the keeper kept is taken back after, and whatever acts on its session waits for that.
let ready: Promise<unknown> = Promise.resolve();
async function boot() {
  // An app opened from Finder has only the system's PATH; the login shell knows where codex, git and the gates' tools are.
  await loginPath();
  const kids = new Map<string, Kid>();
  try {
    await startKeeper(DIR, KEEPER, path.join(ROOT, 'logs', 'agents-keeper.log'));
    for (const k of await ask<Kid[]>(KEEPER, { op: 'list' })) kids.set(k.key, k);
  } catch (e) { log('keeper', e); }
  await loadSettings();
  await restore(kids);
  await pruneImages();
  await Promise.all([pruneOld(UPLOADS), pruneOld(TRASH)]);
  for (const x of sessions.values()) {
    const k = kids.get(x.s.id);
    if (k && x.driver.adopt && !x.s.archived) x.kept = takeBack(x, k);
  }
  // A child whose session is gone (deleted while no host ran) has nobody to answer to.
  for (const key of kids.keys()) if (!sessions.has(key)) void ask(KEEPER, { op: 'kill', key }).catch(() => {});
}
// Its conversation read and the child taken back, a turn it was running going on here.
async function takeBack(x: Session, k: Kid) {
  const was = busy(x);
  try {
    await x.driver.adopt!(x, k.busy, was);
    if (k.busy && x.s.st !== 'wait' && x.s.st !== 'pack') x.set({ st: 'work', now: x.s.now ?? '在想' });
    log('took back', x.s.id.slice(0, 8), k.busy ? 'busy' : 'idle');
  } catch (e) { log('boot', x.s.id, e); }
}

export async function main() {
  key = hostKey(DIR);
  ready = boot();
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');
    if (req.method === 'OPTIONS') {
      res.writeHead(204, { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, POST, DELETE', 'Access-Control-Allow-Headers': 'Content-Type, Authorization', 'Access-Control-Max-Age': '600' });
      res.end(); return;
    }
    const reply = (code: number, v: unknown) => {
      if (res.headersSent) { res.end(); return; }
      res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Access-Control-Allow-Origin': '*' });
      res.end(JSON.stringify(v));
    };
    if (!(await authorized(req))) { reply(401, { error: '没有钥匙' }); return; }
    try {
      const out = await route(req, res, url);
      if (out !== undefined) reply(200, out);
    } catch (e) {
      // A plain error may carry the status it wants (a key that is refused, a file the preview refuses).
      const code = e instanceof Http ? e.code : (e as { status?: number }).status ?? 500;
      if (code >= 500) log(req.method, url.pathname, e);
      reply(code, { error: e instanceof Error ? e.message : String(e), ...e instanceof Http && e.need ? { need: e.need } : {} });
    }
  });
  // Only this machine: the port is the lock, so a second host started by accident exits here.
  server.on('error', e => { log('listen', e); process.exit(1); });
  // Told to stop, the host writes the list and takes its claude children (its own process group) with it: left behind,
  // they run their turn on unseen while the next host resumes the same session beside them. Exiting also runs codex's
  // exit hook.
  let stopping = false;
  for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP'] as const) process.on(sig, () => {
    if (stopping) return;
    stopping = true;
    try { saveNow(); } catch (e) { log('save', e); }
    try { process.kill(-process.pid, 'SIGTERM'); } catch { /* not a group leader: started by hand */ }
    process.exit(0);
  });
  server.listen(PORT, '127.0.0.1', () => log(`agent host on ${PORT}, ${sessions.size} sessions`));
  // A comment line every 20 s keeps the stream open through idle stretches; while a window watches, the notch's marks
  // come in at the same pace.
  setInterval(() => { for (const c of clients) c.write(': \n\n'); if (clients.size) void marksIn(); }, 20000).unref();
  void marksIn();
}
if (process.argv[1] && import.meta.url.endsWith(path.basename(process.argv[1]))) void main();

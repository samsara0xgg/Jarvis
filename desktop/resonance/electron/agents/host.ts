// The agent host (ADR 0073): one long-lived process that runs every coding-agent session Jarvis starts, so a turn keeps
// going while the companion or the daemon restarts. The companion starts it when nothing answers on its port; the
// Agents window talks to it over local HTTP and one event stream. Conversations are read back from each agent's own
// transcript; this process keeps only what the agents do not: pinned, archived, which agent, the worktree it made.
import http from 'node:http';
import { createHash, randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { writeFileSync } from 'node:fs';
import { mkdir, readFile, readdir, rename, stat, unlink, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import type { Agent, Answer, Catalog, Choice, Ctx, Event, File, Item, Pic, Req, Sess, St, Step } from './types.js';
import { claude } from './claude.js';
import { codex } from './codex.js';
import { ask, startKeeper, type Kid } from './keeper.js';

const exec = promisify(execFile);
const ROOT = process.env.JARVIS_RUNTIME_ROOT ?? path.join(homedir(), '.jarvis');
export const DIR = process.env.JARVIS_AGENTS_DIR ?? path.join(ROOT, 'agents');
const PORT = Number(process.env.JARVIS_AGENTS_PORT ?? 8016);
// Where the keeper (ADR 0082) answers: the Claude Code children live there, not under this process.
export const KEEPER = path.join(DIR, 'keeper.sock');
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
  fork(s: Session): Promise<string>;
  remove(s: Session): Promise<void>;
  // The slash commands and skills for `cwd`, from the session itself when it runs.
  commands(cwd: string, s?: Session): Promise<[string, string][]>;
  // What a terminal types to continue it.
  resume(s: Session): string;
  // What fills its context window now.
  context(s: Session): Promise<Ctx>;
  // Take back a child the keeper kept through a restart; `news`: a turn that ends in what it replays was not seen.
  adopt?(s: Session, busy: boolean, news: boolean): Promise<void>;
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
async function pruneImages() {
  await mkdir(IMAGES, { recursive: true });
  for (const f of await readdir(IMAGES)) {
    const p = path.join(IMAGES, f);
    if (Date.now() - (await stat(p)).mtimeMs > 30 * 864e5) await unlink(p).catch(() => {});
  }
}
const oneLine = (t: string, n = 120) => { const x = t.replace(/\s+/g, ' ').trim(); return x.length > n ? `${x.slice(0, n - 1)}…` : x; };
// A finished row shows the start of its last answer: the first sentence, without markdown.
export const firstSentence = (text: string) => oneLine((text.split('\n').find(l => l.trim() && !l.startsWith('```')) ?? '')
  .replace(/^\s*(?:[-*#>]+|\d+[.)])\s*/, '').replace(/\*\*|`/g, '').split(/(?<=[。！？])|(?<=[.!?])\s+(?=[A-Z"'(])/)[0].replace(/[：:]\s*$/, ''));
const base = (p: string) => p.split('/').pop() ?? p;
const NOW: Record<Step['k'], (t: string) => string> = {
  read: t => `在读 ${base(t)}`, edit: t => `在改 ${base(t)}`, bash: t => `在跑 ${oneLine(t, 40)}`, search: t => `在搜 ${oneLine(t, 40)}`,
  agent: () => '在开子任务', web: () => '在查网页', tool: t => `在用 ${oneLine(t, 40)}`, say: () => '在写',
};
export function reqLine(r: Req) {
  if (r.tool === 'Ask') return `问你：${oneLine(r.qs[0]?.q ?? '', 60)}`;
  if (r.tool === 'Plan') return '计划写好了，等你点头';
  if (r.tool === 'Bash') return `想跑 ${oneLine(r.cmd, 60)}`;
  if (r.tool === 'Edit') return `想改 ${base(r.file)}`;
  return `想用 ${r.name}`;
}

// ---------- one session: its row, its conversation, and the turn being built ----------
// last: when the last thing in this turn happened, so a turn read back from a transcript knows how long it took
type Turn = { group: number; start?: number; last?: number; pending: string | null; block: string; tools: Map<string, [number, number]>; plan: number };
export class Session {
  items: Item[] | null = null;
  live: string | null = null;
  // The driver's own state: its process, its pending requests.
  rt: Record<string, any> = {};
  private turn: Turn | null = null;
  private quiet = false;
  private liveAt = 0;
  private liveTimer: ReturnType<typeof setTimeout> | undefined;
  constructor(public s: Sess, public repo: string) {}
  get driver() { return DRIVERS[this.s.agent]; }

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
    if (!this.quiet) this.set({ st: 'work', stopped: false, since: Date.now(), now: '在想', summary: '在想', updated: Date.now() });
  }
  private need() { if (!this.turn) this.begin(); return this.turn!; }
  you(text: string, files: Pic[] = [], at?: number) {
    this.end(undefined, true);
    this.push({ k: 'you', text, at: this.time(at), ...(files.length ? { files } : {}) });
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
    const it = this.items![at[0]] as Item & { k: 'steps' };
    Object.assign(it.steps[at[1]], patch);
    this.changed(at[0]);
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
      if (text) this.push({ k: 'it', text, at: this.time(at ?? (this.quiet ? t.last : undefined)) });
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
  }
  // A message sent while it worked waits here until the agent takes it.
  enqueue(text: string) { this.set({ queue: [...this.s.queue ?? [], text] }); }
  dequeue(text: string) {
    const q = [...this.s.queue ?? []], i = q.indexOf(text);
    if (i >= 0) q.splice(i, 1);
    this.set({ queue: q.length ? q : undefined });
  }
  async ensureLoaded() { if (!this.items) { try { await this.driver.load(this); } catch (e) { log('load', this.s.id, e); this.items = [{ k: 'note', text: `读不出这个会话的记录：${String(e)}` }]; } } }
}

// ---------- the sessions, kept in one file ----------
const sessions = new Map<string, Session>();
export const find = (id: string) => sessions.get(id);
const FILE = path.join(DIR, 'sessions.json');
let saving: ReturnType<typeof setTimeout> | undefined;
function save() {
  clearTimeout(saving);
  saving = setTimeout(async () => {
    const rows = [...sessions.values()].map(x => {
      const { now, since, queue, stopped, bg, ...keep } = x.s;
      return { ...keep, repo: x.repo };
    });
    await mkdir(DIR, { recursive: true });
    await writeFile(`${FILE}.tmp`, JSON.stringify({ v: 1, sessions: rows }, null, 1));
    await rename(`${FILE}.tmp`, FILE);
  }, 300);
}
async function restore(kids: Map<string, Kid>) {
  const data = JSON.parse(await readFile(FILE, 'utf8').catch(() => '{"sessions":[]}'));
  for (const { repo, ...s } of data.sessions as (Sess & { repo: string })[]) {
    // A turn that was running goes on in the keeper; without its child there, it went with the host.
    if ((s.st === 'work' || s.st === 'wait' || s.st === 'pack') && !kids.has(s.id)) Object.assign(s, {
      st: 'err', trace: [...s.trace ?? [], { at: Date.now(), st: 'err' }],
      summary: 'Jarvis 的后台重启了，这一轮断了 · 发一句接着来',
    });
    s.parked ??= false;
    sessions.set(s.id, new Session(s, repo));
  }
}

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

// ---------- git: projects, files, worktrees ----------
const git = async (cwd: string, ...args: string[]) => (await exec('git', ['-C', cwd, ...args], { maxBuffer: 64 << 20 })).stdout;
// The repository a folder belongs to, with a worktree counted as its main checkout.
async function repoOf(cwd: string) {
  try { return path.dirname((await git(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir')).trim()); } catch { return ''; }
}
async function branchOf(cwd: string) { try { return (await git(cwd, 'branch', '--show-current')).trim(); } catch { return ''; } }
async function worktree(repo: string, hint: string) {
  const slug = hint.toLowerCase().match(/[a-z0-9]+/g)?.slice(0, 4).join('-').slice(0, 32) || 'session';
  const name = `${slug}-${randomUUID().slice(0, 4)}`, at = path.join(repo, '.claude', 'worktrees', name), branch = `worktree-${name}`;
  await git(repo, 'worktree', 'add', '-b', branch, at, 'HEAD');
  return { cwd: at, branch };
}
async function projects() {
  const home = path.join(homedir(), 'Projects'), seen = new Map<string, number>();
  for (const x of sessions.values()) if (x.repo) seen.set(x.repo, Math.max(seen.get(x.repo) ?? 0, x.s.updated));
  for (const d of await readdir(home, { withFileTypes: true }).catch(() => [])) {
    if (!d.isDirectory() || d.name.startsWith('.')) continue;
    const p = path.join(home, d.name), g = await stat(path.join(p, '.git')).catch(() => null);
    if (g && !seen.has(p)) seen.set(p, 0);
  }
  return [...seen].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([p]) => p);
}
const fileCache = new Map<string, { at: number; list: string[] }>();
async function files(cwd: string, q: string) {
  let c = fileCache.get(cwd);
  if (!c || Date.now() - c.at > 15000) {
    const out = await git(cwd, 'ls-files', '-co', '--exclude-standard').catch(() => '');
    c = { at: Date.now(), list: out.split('\n').filter(Boolean) };
    fileCache.set(cwd, c);
  }
  const want = q.toLowerCase();
  const hits = c.list.filter(f => f.toLowerCase().includes(want));
  // A match in the file's own name beats one in its folders.
  return hits.sort((a, b) => Number(!base(b).toLowerCase().includes(want)) - Number(!base(a).toLowerCase().includes(want)) || a.length - b.length).slice(0, 40);
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
// The daemon's local key: the window's requests carry it, and this process uses it to reach the daemon.
let token = '';
async function readToken() {
  try { token = JSON.parse(await readFile(path.join(ROOT, 'plugin-access.json'), 'utf8')).token ?? ''; } catch { token = ''; }
  return token;
}
async function authorized(req: Req0) {
  const got = req.headers.authorization ?? '';
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
class Http extends Error { constructor(public code: number, msg: string) { super(msg); } }
const need = (id: string) => { const x = sessions.get(id); if (!x) throw new Http(404, '没有这个会话'); return x; };
const str = (v: unknown, name: string) => { if (typeof v !== 'string') throw new Http(400, `${name} 不对`); return v; };
const fileList = (v: unknown): File[] => Array.isArray(v) ? v.filter(f => typeof f?.name === 'string' && typeof f?.url === 'string' && f.url.startsWith('data:')) : [];

async function route(req: Req0, res: http.ServerResponse, url: URL): Promise<unknown> {
  const m = req.method ?? 'GET', parts = url.pathname.split('/').filter(Boolean);
  if (m === 'GET' && url.pathname === '/health') return { ok: true, pid: process.pid };
  if (m === 'GET' && url.pathname === '/events') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*' });
    await ready;
    clients.add(res);
    req.on('close', () => clients.delete(res));
    const hello: Event = { t: 'hello', sessions: [...sessions.values()].map(x => x.s), catalog: await getCatalog() };
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
  if (m === 'GET' && url.pathname === '/projects') return { projects: await projects() };
  // A session's folder, or for a session not started yet the folder and agent it will have.
  const where = () => {
    const id = url.searchParams.get('id');
    if (id) { const x = need(id); return { cwd: x.s.cwd, agent: x.s.agent, x }; }
    return { cwd: path.resolve(url.searchParams.get('cwd') ?? homedir()), agent: (url.searchParams.get('agent') === 'codex' ? 'codex' : 'claude') as Agent, x: undefined };
  };
  if (m === 'GET' && url.pathname === '/files') return { files: await files(where().cwd, url.searchParams.get('q') ?? '') };
  if (m === 'GET' && url.pathname === '/commands') { const w = where(); return { commands: await DRIVERS[w.agent].commands(w.cwd, w.x) }; }
  if (m === 'POST' && url.pathname === '/sessions') {
    const b = await body(req), agent = b.agent === 'codex' ? 'codex' : 'claude', text = str(b.text, 'text').trim(), files = fileList(b.files);
    let cwd = path.resolve(str(b.cwd, 'cwd').replace(/^~(?=\/|$)/, homedir()));
    if (!(await stat(cwd).catch(() => null))?.isDirectory()) throw new Http(400, '没有这个文件夹');
    if (!text && !files.length) throw new Http(400, '要它做什么？');
    const repo = await repoOf(cwd);
    let branch = await branchOf(cwd), tree = false;
    if (b.tree && repo) { ({ cwd, branch } = await worktree(repo, text)); tree = true; }
    const cat = (await getCatalog())[agent];
    const s: Sess = { id: '', agent, title: oneLine(text || files[0]?.name || '新会话', 48), cwd, project: base(repo || cwd), branch, tree,
      st: 'work', pinned: false, parked: false, archived: false, unread: false, created: Date.now(), trace: [{ at: Date.now(), st: 'work' }], updated: Date.now(), summary: '在想',
      model: typeof b.model === 'string' ? b.model : cat.models[0]?.[0] ?? '', effort: typeof b.effort === 'string' ? b.effort : 'high',
      mode: typeof b.mode === 'string' ? b.mode : cat.modes[0]?.[0] ?? '', ctx: 0 };
    const x = new Session(s, repo);
    x.items = [];
    s.id = await x.driver.create(x);
    sessions.set(s.id, x);
    broadcast({ t: 'sess', s });
    save();
    await x.driver.send(x, text, files);
    return { id: s.id };
  }
  if (parts[0] !== 'sessions' || !parts[1]) throw new Http(404, '没有这个地方');
  const x = need(parts[1]), verb = parts[2] ?? '';
  if (m === 'GET' && !verb) { await x.ensureLoaded(); return { items: x.items, live: x.live }; }
  if (m === 'DELETE' && !verb) {
    if (x.s.st === 'work' || x.s.st === 'wait') await x.driver.interrupt(x).catch(() => {});
    await x.driver.release(x).catch(() => {});
    // A fork shares its session's worktree: it goes with the last session in it.
    if (x.s.tree && x.repo && ![...sessions.values()].some(o => o !== x && o.s.cwd === x.s.cwd)) {
      // Git's own checks decide: a worktree with changes, or a branch with commits nothing else has, stays.
      if (!(await git(x.repo, 'merge-base', '--is-ancestor', x.s.branch, 'HEAD').then(() => true, () => false))) throw new Http(409, `${x.s.branch} 上还有没合进去的提交，先合进去或者自己删`);
      try { await git(x.repo, 'worktree', 'remove', x.s.cwd); } catch { throw new Http(409, 'worktree 里还有没提交的改动，先提交或者自己删'); }
      await git(x.repo, 'branch', '-d', x.s.branch).catch(() => {});
    }
    // The agent can refuse too (Codex keeps a thread a fork still reads from): then the session stays.
    try { await x.driver.remove(x); } catch (e) { log('remove', x.s.id, e); throw new Http(409, `删不掉：${e instanceof Error ? e.message : String(e)}`); }
    sessions.delete(x.s.id);
    broadcast({ t: 'gone', id: x.s.id });
    save();
    return { ok: true };
  }
  if (m === 'GET' && verb === 'context') {
    if (x.s.term) throw new Http(409, '在终端里，拿回来才看得到');
    const c = await x.driver.context(x);
    // The ring takes the measured number.
    if (c.max) x.set({ ctx: Math.min(100, Math.round(c.used / c.max * 100)) });
    return c;
  }
  if (m !== 'POST') throw new Http(405, '不行');
  const b = await body(req);
  if (verb === 'send') {
    if (x.s.term) throw new Http(409, '在终端里，先拿回来');
    const text = str(b.text, 'text').trim(), files = fileList(b.files);
    if (!text && !files.length) return { ok: true };
    await x.ensureLoaded();
    x.set({ unread: false, updated: Date.now() });
    await x.driver.send(x, text, files);
    return { ok: true };
  }
  if (verb === 'answer') {
    if (x.pending()?.req.id !== b.req) throw new Http(409, '这张请求已经处理过了');
    const a: Answer = { req: str(b.req, 'req'), decision: b.decision === 'deny' ? 'deny' : b.decision === 'always' ? 'always' : 'allow',
      answers: Array.isArray(b.answers) ? b.answers.map((q: unknown) => Array.isArray(q) ? q.map(String) : []) : undefined, text: typeof b.text === 'string' ? b.text : undefined };
    x.driver.answer(x, a);
    return { ok: true };
  }
  if (verb === 'interrupt') { if (x.s.st === 'work' || x.s.st === 'pack' || x.s.st === 'wait') await x.driver.interrupt(x); return { ok: true }; }
  if (verb === 'stop') {
    if (x.s.st === 'work' || x.s.st === 'wait') await x.driver.interrupt(x).catch(() => {});
    await x.driver.release(x);
    x.set({ stopped: true, st: x.s.st === 'err' ? 'err' : 'done', now: undefined });
    return { ok: true };
  }
  if (verb === 'set') {
    const k: 'model' | 'effort' | 'mode' | null = b.key === 'model' || b.key === 'effort' || b.key === 'mode' ? b.key : null;
    if (!k) throw new Http(400, 'key 不对');
    const v = str(b.value, 'value');
    if (x.s[k] === v) return { ok: true };
    await x.driver.set(x, k, v);
    x.set({ [k]: v });
    if (x.items) x.note(`${k === 'model' ? '模型' : k === 'effort' ? '力度' : '模式'}换成 ${label(x.s.agent, k, v)}${x.s.st === 'work' ? ' · 从下一步开始' : ''}`);
    return { ok: true };
  }
  if (verb === 'meta') {
    const p: Partial<Sess> = {};
    if (typeof b.pinned === 'boolean') p.pinned = b.pinned;
    if (typeof b.archived === 'boolean') { p.archived = b.archived; if (b.archived) Object.assign(p, { pinned: false, parked: false }); }
    if (typeof b.parked === 'boolean') p.parked = b.parked;
    if (typeof b.title === 'string' && b.title.trim()) { p.title = oneLine(b.title, 80); await x.driver.rename(x, p.title).catch(e => log('rename', x.s.id, e)); }
    if (b.seen === true) p.unread = false;
    if (p.archived && (x.s.st === 'work' || x.s.st === 'wait')) { await x.driver.interrupt(x).catch(() => {}); await x.driver.release(x).catch(() => {}); }
    x.set(p);
    return { ok: true };
  }
  if (verb === 'fork') {
    await x.ensureLoaded();
    const id = await x.driver.fork(x);
    const f = new Session({ ...x.s, id, title: `${x.s.title}（分叉）`, pinned: false, parked: false, archived: false, unread: false, st: 'done', updated: Date.now(),
      trace: [...x.s.trace ?? [], { at: Date.now(), st: 'done' }],
      now: undefined, since: undefined, queue: undefined, stopped: undefined, term: undefined }, x.repo);
    sessions.set(id, f);
    await f.ensureLoaded();
    f.note(`从「${x.s.title}」分叉 · 两边各走各的，用的是同一个文件夹`);
    broadcast({ t: 'sess', s: f.s });
    save();
    return { id };
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
function label(agent: Agent, k: 'model' | 'effort' | 'mode', v: string) {
  const c = catalog?.[agent];
  return (k === 'model' ? c?.models : k === 'mode' ? c?.modes : undefined)?.find(x => x[0] === v)?.[1] ?? v;
}

// The window is shown what is ready: every child taken back and every conversation read.
let ready: Promise<unknown> = Promise.resolve();
async function boot() {
  const kids = new Map<string, Kid>();
  try {
    await startKeeper(KEEPER, path.join(ROOT, 'logs', 'agents-keeper.log'));
    for (const k of await ask<Kid[]>(KEEPER, { op: 'list' })) kids.set(k.key, k);
  } catch (e) { log('keeper', e); }
  await restore(kids);
  await pruneImages();
  const live = [...sessions.values()].filter(x => !x.s.archived);
  await Promise.all(live.map(async x => {
    const k = kids.get(x.s.id);
    try {
      if (k && x.driver.adopt) {
        const was = x.s.st === 'work' || x.s.st === 'wait' || x.s.st === 'pack';
        await x.driver.adopt(x, k.busy, was);
        if (k.busy && x.s.st !== 'wait' && x.s.st !== 'pack') x.set({ st: 'work', now: x.s.now ?? '在想' });
        log('took back', x.s.id.slice(0, 8), k.busy ? 'busy' : 'idle');
      } else await x.ensureLoaded();
    } catch (e) { log('boot', x.s.id, e); }
  }));
  // A child whose session is gone (deleted while no host ran) has nobody to answer to.
  for (const key of kids.keys()) if (!sessions.has(key)) void ask(KEEPER, { op: 'kill', key }).catch(() => {});
}

export async function main() {
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
      if (!(e instanceof Http)) log(req.method, url.pathname, e);
      reply(e instanceof Http ? e.code : 500, { error: e instanceof Error ? e.message : String(e) });
    }
  });
  // Only this machine: the port is the lock, so a second host started by accident exits here.
  server.on('error', e => { log('listen', e); process.exit(1); });
  // Told to stop, the host takes its claude children (its own process group) with it: left behind, they run their turn
  // on unseen while the next host resumes the same session beside them. Exiting also runs codex's exit hook.
  let stopping = false;
  for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP'] as const) process.on(sig, () => {
    if (stopping) return;
    stopping = true;
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

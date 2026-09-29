// ADR 0085: landing. One line from a session's changes to main, run here in the host so it keeps going while the window
// or the companion restarts (it restarts the companion itself). It is docs/git-guide.md §3 drawn as seven steps: the
// changes, the gates, a commit in the commit skill's format with explicit paths and every hook, a fast-forward into
// main, a restart of what the change touches, the push once Allen says so, and the worktree gone when git agrees.
import { execFile, spawn, type ChildProcess } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, open, readFile, rm, writeFile } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import { query } from '@anthropic-ai/claude-agent-sdk';
import { claudeEnv, EXE } from './claude.js';
import { DIR, log, sharing, type Session } from './host.js';
import { auth } from './settings.js';
import type { Land, LandSt } from './types.js';

const exec = promisify(execFile);
const STEPS = ['diff', 'gate', 'commit', 'merge', 'restart', 'push', 'clean'] as const;
type Id = typeof STEPS[number];
const at = (id: Id) => STEPS.indexOf(id);
type Fail = { why: string; acts: NonNullable<Land['acts']> };
type Done = 'ok' | 'skip' | 'wait' | Fail;
const HAIKU = 'claude-haiku-4-5-20251001';

// Gates and git run as Allen's shell would: his tool paths, and nothing of this process's own Electron-as-Node setup.
// Read each time, so what the login shell added to PATH once the host started counts.
export function shellEnv(): Record<string, string> {
  const e: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) if (v !== undefined && !/^(ELECTRON_|JARVIS_AGENTS_)/.test(k)) e[k] = v;
  e.PATH = [path.join(homedir(), '.local/share/mise/shims'), path.join(homedir(), '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin', e.PATH || '/usr/bin:/bin:/usr/sbin:/sbin'].join(':');
  return e;
}
const git = async (cwd: string, ...args: string[]) => (await exec('git', ['-C', cwd, ...args], { maxBuffer: 64 << 20, env: shellEnv() })).stdout;
const kill = (c: ChildProcess) => { try { process.kill(-c.pid!, 'SIGTERM'); } catch { c.kill('SIGTERM'); } };
// A command in its own process group, so stopping a gate stops everything it started; the last 96 kB of what it said.
function run(cmd: string, args: string[], cwd: string, hold?: (c: ChildProcess | null) => void, ms = 20 * 60e3) {
  return new Promise<{ code: number; out: string }>(done => {
    let out = '', ended = false;
    const c = spawn(cmd, args, { cwd, env: shellEnv(), detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
    const end = (code: number) => { if (ended) return; ended = true; clearTimeout(timer); hold?.(null); done({ code, out }); };
    const timer = setTimeout(() => { out += `\n超过 ${Math.round(ms / 60e3)} 分钟，停掉了`; kill(c); }, ms);
    hold?.(c);
    const keep = (b: Buffer) => { out = (out + b.toString('utf8')).slice(-96e3); };
    c.stdout!.on('data', keep); c.stderr!.on('data', keep);
    c.on('error', e => { out += String(e); end(1); });
    c.on('close', code => end(code ?? 1));
  });
}
// What a failure says, in a few lines: the ones that look like the error, else the last ones.
function tail(out: string, n = 5) {
  const lines = out.replace(/\x1b\[[0-9;]*m/g, '').split('\n').map(l => l.trimEnd()).filter(l => l.trim());
  const bad = lines.filter(l => /(error|fail|✕|✗|assert|refused|rejected|conflict|fatal|denied|not found)/i.test(l));
  return (bad.length ? bad.slice(-n) : lines.slice(-n)).join('\n').slice(0, 900) || '没有输出';
}
async function lines(file: string) {
  try {
    const f = await open(file, 'r');
    try { const b = Buffer.alloc(2 << 20), { bytesRead } = await f.read(b, 0, b.length, 0); const t = b.subarray(0, bytesRead); return t.includes(0) ? 0 : t.toString('utf8').split('\n').length - (t.at(-1) === 10 ? 1 : 0); }
    finally { await f.close(); }
  } catch { return 0; }
}

// ---------- what a session would land ----------
// Its own worktree lands against main; a session working on main itself lands what origin has not got yet.
export type Changes = { top: string; branch: string; base: string; linear: boolean; files: [string, number, number][]; ahead: number; uncommitted: string[] };
export async function changesOf(x: Session): Promise<Changes | null> {
  if (!existsSync(x.s.cwd)) return null;
  const top = (await git(x.s.cwd, 'rev-parse', '--show-toplevel').catch(() => '')).trim();
  if (!top) return null;
  const branch = (await git(top, 'branch', '--show-current').catch(() => '')).trim();
  let base = '', linear = true;
  if (branch === 'main') base = (await git(top, 'rev-parse', '--verify', '-q', '@{u}').catch(() => '')).trim() || 'HEAD';
  else if (branch && x.s.tree) {
    base = (await git(top, 'merge-base', 'main', 'HEAD').catch(() => '')).trim();
    linear = await git(top, 'merge-base', '--is-ancestor', 'main', 'HEAD').then(() => true, () => false);
  }
  if (!base) return null;
  const files = new Map<string, [number, number]>();
  for (const e of (await git(top, 'diff', '--numstat', '--no-renames', '-z', base)).split('\0')) {
    const m = /^(\d+|-)\t(\d+|-)\t(.+)$/s.exec(e);
    if (m) files.set(m[3], [m[1] === '-' ? 0 : Number(m[1]), m[2] === '-' ? 0 : Number(m[2])]);
  }
  const uncommitted: string[] = [];
  for (const e of (await git(top, 'status', '--porcelain=v1', '-z', '--no-renames', '--untracked-files=all')).split('\0')) {
    // A folder here is another repository inside this one (a nested worktree): never landed.
    if (e.length < 4 || e.endsWith('/')) continue;
    const p = e.slice(3);
    uncommitted.push(p);
    if (e.startsWith('??') && !files.has(p)) files.set(p, [await lines(path.join(top, p)), 0]);
  }
  const ahead = Number((await git(top, 'rev-list', '--count', `${base}..HEAD`).catch(() => '0')).trim()) || 0;
  return { top, branch, base, linear, files: [...files].map(([p, [a, d]]) => [p, a, d]), ahead, uncommitted };
}
export async function dirtyOf(x: Session) {
  const c = await changesOf(x).catch(() => null);
  if (!c || (!c.files.length && !c.ahead)) return undefined;
  return { n: c.files.length, add: c.files.reduce((n, f) => n + f[1], 0), del: c.files.reduce((n, f) => n + f[2], 0), ahead: c.ahead };
}

// ---------- the Jarvis repository's gates and services (docs/git-guide.md §1 and §3) ----------
const isJarvis = (top: string) => existsSync(path.join(top, 'jarvis', '__init__.py')) && existsSync(path.join(top, 'desktop', 'resonance', 'package.json'));
const PY = /\.py$|^(jarvis|tests|scripts|tools|plugins|config)\/|^(pyproject\.toml|uv\.lock|\.importlinter)$/;
const DESK = /^desktop\/resonance\//;
const ADR = /^docs\/adr\//;
const DAEMON = /^(jarvis|plugins|config)\/|^(uv\.lock|pyproject\.toml)$/;
type GateDef = { n: string; cmd: string; args: string[]; dir: string; group: 'py' | 'desk' | 'adr'; count?: (out: string) => string };
const UV = (...a: string[]) => ['run', '--frozen', '--no-sync', ...a];
function gatesFor(top: string, paths: string[]): { defs: GateDef[]; say: string } {
  if (!isJarvis(top)) return { defs: [], say: '这个仓库没配门禁' };
  const py = paths.some(p => PY.test(p)), desk = paths.some(p => DESK.test(p)), adr = paths.some(p => ADR.test(p)), d = path.join(top, 'desktop', 'resonance');
  const defs: GateDef[] = [];
  if (py) {
    if (!existsSync(path.join(top, '.venv', 'bin', 'ruff'))) defs.push({ n: '环境', cmd: 'bash', args: ['scripts/init.sh', '--fix'], dir: top, group: 'py' });
    defs.push(
      { n: 'lint-imports', cmd: 'uv', args: UV('lint-imports'), dir: top, group: 'py', count: o => { const m = /Contracts: (\d+) kept, (\d+) broken/.exec(o); return m ? `KEPT (${m[1]}/${Number(m[1]) + Number(m[2])})` : 'KEPT'; } },
      { n: 'ruff', cmd: 'uv', args: UV('ruff', 'check', '.'), dir: top, group: 'py', count: () => 'clean' },
      { n: 'mypy', cmd: 'uv', args: UV('mypy', '--strict', 'jarvis', 'tests', 'scripts', 'tools'), dir: top, group: 'py', count: o => { const m = /no issues found in (\d+) source files/.exec(o); return m ? `strict clean (${m[1]} files)` : 'strict clean'; } },
      { n: 'pytest', cmd: 'uv', args: UV('pytest', 'tests', '-m', 'not live_llm', '-x', '-q'), dir: top, group: 'py', count: o => { const m = /(\d+) passed/.exec(o); return m ? `${m[1]}/${m[1]}` : 'pass'; } },
      { n: 'uv audit', cmd: 'uv', args: ['audit', '--frozen', '--preview-features', 'audit'], dir: top, group: 'py', count: o => { const m = /in (\d+) packages/.exec(o); return m ? `clean (${m[1]} packages)` : 'clean'; } },
    );
  }
  if (desk) {
    const checks = (o: string) => { const m = /(\d+) checks passed/.exec(o); return m ? `${m[1]}/${m[1]}` : 'pass'; };
    if (!existsSync(path.join(d, 'node_modules'))) defs.push({ n: 'npm ci', cmd: 'npm', args: ['ci'], dir: d, group: 'desk' });
    defs.push(
      { n: 'tsc', cmd: 'npx', args: ['tsc', '--noEmit'], dir: d, group: 'desk', count: () => 'clean' },
      { n: 'build', cmd: 'npm', args: ['run', 'build'], dir: d, group: 'desk', count: () => 'ok' },
      { n: 'verify-companion', cmd: 'node', args: ['scripts/verify-companion.mjs'], dir: d, group: 'desk', count: checks },
      { n: 'verify-companion-live', cmd: 'node', args: ['scripts/verify-companion-live.mjs'], dir: d, group: 'desk', count: checks },
    );
  }
  if (adr) defs.push({ n: 'check_adrs', cmd: 'uv', args: UV('python', 'scripts/check_adrs.py'), dir: top, group: 'adr', count: () => 'clean' });
  const say = py && desk ? 'Python 和 desktop 都改了：Tier 1 五项，再加类型检查、构建、两套 companion 验收'
    : py ? 'Python 的改动：Tier 1 五项（lint-imports、ruff、mypy、验收、uv audit）'
    : desk ? 'desktop 的改动：类型检查、构建、两套 companion 验收'
    : adr ? 'ADR 的格式检查' : '只改了文档，不用跑门禁';
  return { defs, say };
}
function restartFor(top: string, paths: string[]) {
  if (!isJarvis(top)) return { labels: [] as string[], say: '这个仓库没有要重启的' };
  const daemon = paths.some(p => DAEMON.test(p)), companion = paths.some(p => DESK.test(p));
  const labels = [...daemon ? ['daemon'] : [], ...companion ? ['companion'] : []];
  const say = daemon && companion ? 'daemon 和 desktop/resonance 都改了，两个都重启' : companion ? '只改了 desktop/resonance，所以只重启 companion'
    : daemon ? '改了 daemon 跑的代码，所以只重启 daemon' : '没碰 daemon 和 companion，不用重启';
  return { labels, say };
}
export const LABEL: Record<string, string> = { daemon: 'com.allen.jarvis', companion: 'com.allen.jarvis.resonance' };
const kick = (label: string) => run('launchctl', ['kickstart', '-k', `gui/${process.getuid?.() ?? 501}/${label}`], homedir(), undefined, 60e3);
// The companion restart takes the Agents window with it; the companion that comes up opens it again on this session.
export const REOPEN = () => path.join(DIR, 'reopen.json');

// ---------- the commit message: drafted from the diff with the commit skill as the instructions ----------
const SKILL_FALLBACK = 'Title: `type(scope): English description`, at most 72 characters, Conventional Commits types. Body: a one-line opener, then one bullet per file or file group, "- <path> — <what this file contributes>", English, wrapped near 72 columns.';
async function draftMessage(top: string, title: string, said: string, paths: string[]): Promise<{ title: string; body: string } | null> {
  // Without Claude's sign-in (the packaged app with no key yet) the title is left to Allen.
  if (!auth().ready) return null;
  const skill = await readFile(path.join(top, '.claude', 'skills', 'commit', 'SKILL.md'), 'utf8').catch(() => SKILL_FALLBACK);
  const guide = await readFile(path.join(top, 'docs', 'git-guide.md'), 'utf8').then(t => /## 2\.[\s\S]*?(?=\n## 3\.)/.exec(t)?.[0] ?? '', () => '');
  const stat = await git(top, 'diff', '--stat', 'HEAD', '--', ...paths).catch(() => '');
  let diff = await git(top, 'diff', 'HEAD', '--', ...paths).catch(() => '');
  for (const p of paths) if (!stat.includes(p) && existsSync(path.join(top, p))) diff += `\n--- new file ${p}\n${(await readFile(path.join(top, p), 'utf8').catch(() => '')).slice(0, 3000)}`;
  const system = `You write one git commit message, following the project's commit skill below. Write only: the title line, a blank line, a one-line opener, a blank line, then one bullet per file or tightly related file group ("- <path> — <what it contributes>"), wrapped near 72 columns. English only. No Tier 1 or verification line, no trailers, no code fences, no commentary.\n\n<commit-skill>\n${skill.slice(0, 8000)}\n</commit-skill>${guide ? `\n\n<scopes>\n${guide.slice(0, 7000)}\n</scopes>` : ''}`;
  const prompt = `The coding session: ${title}\n\nWhat it said last:\n${said.slice(0, 3000)}\n\nChanged files:\n${stat.slice(0, 4000)}\n\nThe diff (may be cut):\n${diff.slice(0, 40000)}`;
  const q = query({ prompt, options: { model: HAIKU, maxTurns: 1, tools: [], systemPrompt: system, cwd: top, env: claudeEnv(), pathToClaudeCodeExecutable: EXE, settingSources: [], persistSession: false } });
  let text = '';
  for await (const m of q) if (m.type === 'result' && m.subtype === 'success') text = m.result;
  const ls = text.replace(/```[a-z]*\n?|```/g, '').trim().split('\n');
  const head = ls.findIndex(l => l.trim()), t = ls[head]?.trim() ?? '';
  if (!/^[a-z]+(\([^)\s]+\))?!?: \S/.test(t) || t.length > 72) return null;
  return { title: t, body: ls.slice(head + 1).join('\n').trim() };
}

// ---------- the line itself ----------
const fresh = (x: Session): Land => ({
  s: 'run', i: 0, steps: STEPS.map(() => ({ st: 'todo' as LandSt })), files: [], gates: [], msg: '', branch: x.s.branch, into: 'main',
});
export class Landing {
  land: Land;
  // tok: bumped to abandon whatever run is going · halt: stop once this step is done · go: the push was allowed
  private tok = 0; private halt = false; private go = false; private child: ChildProcess | null = null;
  private c: Changes | null = null; private defs: GateDef[] = []; private outs = new Map<string, string>(); private wall = 0;
  private body = ''; private edited = false; private draft: Promise<void> | null = null; private failed = 0; private fixing = false;
  // pre: a title Allen wrote before pressing 一键落地; it is the commit's, and nothing is drafted
  private pre = '';
  constructor(private x: Session) { this.land = fresh(x); }
  private emit() { this.x.set({ land: structuredClone(this.land) }); }
  private mark(i: number, st: LandSt, ms?: number) { this.land.steps[i] = { ...this.land.steps[i], st, ...(ms !== undefined ? { ms } : {}) }; }
  get busy() { return ['run', 'stopping', 'wait', 'fixing'].includes(this.land.s) && !!this.x.s.land; }

  start() {
    if (this.busy) return;
    const s = this.x.s.land?.s;
    if (s === 'paused' || s === 'fail') { this.resume(); return; }
    this.land = fresh(this.x); this.land.msg = this.pre; this.edited = !!this.pre; this.pre = '';
    this.body = ''; this.outs.clear(); this.draft = null; this.go = false;
    void this.from(0);
  }
  resume() {
    const s = this.x.s.land?.s;
    if (s !== 'paused' && s !== 'fail') return;
    void this.from(this.land.i);
  }
  // Esc or 打断: a gate stops at once, it changes nothing; any other step finishes first, so git never stops half way.
  stop() {
    if (this.land.s !== 'run') return;
    if (STEPS[this.land.i] === 'gate') {
      this.tok++; if (this.child) kill(this.child);
      this.land.gates = this.land.gates.map(g => ({ n: g.n, st: 'todo' }));
      this.pause(at('gate'), '门禁可以立刻停，它不改任何东西。「继续」会从门禁重新跑。');
      return;
    }
    this.halt = true; this.land.s = 'stopping'; this.emit();
  }
  allow() { if (this.land.s !== 'wait') return; this.go = true; void this.from(at('push')); }
  deny() { if (this.land.s !== 'wait') return; this.pause(at('push'), '没推：main 已经合好了，只是留在本地。「继续」会再问你一次。'); }
  // 留在分支上: the line stops where it is; nothing else is undone.
  stay() { this.tok++; if (this.child) kill(this.child); this.fixing = false; this.x.set({ land: undefined }); void this.refresh(); }
  message(msg: string) {
    const t = msg.replace(/\s*\n[\s\S]*/, '').slice(0, 200);
    if (!this.x.s.land || this.x.s.land.s === 'done') { this.pre = t; return; }
    this.land.msg = t; this.edited = true; this.emit();
  }
  // 让 Claude 修: what stopped the line goes to the session; when its turn ends, the line runs again from that step.
  async fix() {
    if (this.land.s !== 'fail') return;
    const i = this.land.i, g = this.land.gates.find(x => x.st === 'er');
    const what = STEPS[i] === 'gate' && g ? `「门禁」：${g.n} 没过` : `「${['改动', '门禁', '提交', '合入 main', '重启', '推送', '清理'][i]}」`;
    const text = `落地停在${what}。\n\n\`\`\`\n${(g ? this.outs.get(g.n) ?? '' : '').split('\n').slice(-60).join('\n') || this.land.why}\n\`\`\`\n\n请把它修好。修完我会从这一步重新跑落地，不用你提交或合入。`;
    this.failed = i; this.fixing = true;
    this.land.s = 'fixing'; this.land.why = `${this.x.s.agent === 'codex' ? 'Codex' : 'Claude'} 在修，修完从「${['改动', '门禁', '提交', '合入 main', '重启', '推送', '清理'][i]}」重新跑`; this.land.acts = ['stay'];
    this.emit();
    await this.x.ensureLoaded();
    this.x.set({ unread: false, updated: Date.now() });
    await this.x.driver.send(this.x, text, []);
  }
  // The session's state moved: a fix that ended runs the line again; one that stopped leaves it paused.
  saw(st: string) {
    if (!this.fixing || this.x.s.land?.s !== 'fixing') return;
    if (st === 'done') { this.fixing = false; this.land.gates = []; this.draft = null; void this.from(this.failed <= at('gate') ? 0 : this.failed); }
    else if (st === 'err') { this.fixing = false; this.pause(this.failed, `${this.x.s.agent === 'codex' ? 'Codex' : 'Claude'} 没修完：它停了。`); }
  }
  private pause(i: number, why: string) {
    this.land.s = 'paused'; this.land.i = i; this.mark(i, 'paused'); this.land.why = why; this.land.acts = ['resume', 'stay']; this.emit();
  }
  private async refresh() { this.x.set({ dirty: await dirtyOf(this.x) }); }

  private async from(i: number) {
    const tok = ++this.tok, live = () => tok === this.tok;
    this.halt = false;
    for (let k = i; k < STEPS.length; k++) {
      if (!live()) return;
      if (this.halt) { this.halt = false; this.pause(k, '打断在这里：上一步跑完了，这一步还没开始。'); return; }
      Object.assign(this.land, { s: 'run', i: k, why: undefined, acts: undefined });
      this.mark(k, 'run'); this.emit();
      const t0 = Date.now();
      let r: Done;
      try { r = await this[STEPS[k]](live); } catch (e) { r = { why: tail(String(e instanceof Error ? e.message : e)), acts: ['resume', 'stay'] }; }
      if (!live()) return;
      if (r === 'wait') return;
      if (typeof r === 'object') { Object.assign(this.land, { s: 'fail', why: r.why, acts: r.acts }); this.mark(k, 'fail'); this.emit(); return; }
      this.mark(k, r, Date.now() - t0);
    }
    this.land.s = 'done'; this.land.i = STEPS.length - 1; this.emit();
    void this.refresh();
  }

  // ----- the steps -----
  private async diff(_: () => boolean): Promise<Done> {
    const c = this.c = await changesOf(this.x);
    if (!c) return { why: this.x.s.tree || this.x.s.branch === 'main' ? '读不出这个会话的 git 状态' : '这个会话不在自己的 worktree 里，也不在 main 上：落地只走这两种', acts: ['stay'] };
    if (!c.files.length && !c.ahead) return { why: '没有要落地的改动', acts: ['stay'] };
    const paths = c.files.map(f => f[0]);
    this.land.files = c.files; this.land.branch = c.branch;
    const g = gatesFor(c.top, paths), r = restartFor(c.top, paths);
    this.defs = g.defs; this.land.gates = g.defs.map(d => ({ n: d.n, st: 'todo' })); this.land.restart = r.labels;
    const s = this.land.steps, tree = c.branch !== 'main';
    s[at('gate')].d = g.say;
    s[at('commit')].d = c.uncommitted.length ? 'commit skill 起草，跑之前可以改' : '改动都已经提交了';
    s[at('merge')] = { ...s[at('merge')], d: tree ? undefined : '就在 main 上', cmd: tree ? [...c.linear ? [] : ['git rebase main'], `git merge --ff-only ${c.branch}`] : undefined };
    s[at('restart')] = { ...s[at('restart')], d: r.say, cmd: r.labels.map(l => `launchctl kickstart -k gui/$UID/${LABEL[l]}`) };
    s[at('push')] = { ...s[at('push')], d: '等你点头', cmd: ['git push origin main'] };
    s[at('clean')] = { ...s[at('clean')], d: tree ? undefined : '不是它自己的 worktree', cmd: tree ? ['git worktree remove', 'git branch -d'] : undefined };
    if (c.uncommitted.length && !this.draft && !this.edited) this.startDraft(c);
    return 'ok';
  }
  private startDraft(c: Changes) {
    this.land.drafting = true;
    const last = [...this.x.items ?? []].reverse().find(it => it.k === 'it');
    this.draft = draftMessage(c.top, this.x.s.title, last?.k === 'it' ? last.text : '', c.uncommitted)
      .then(d => { if (d) { if (!this.edited) this.land.msg = d.title; this.body = d.body; } }, e => log('draft', this.x.s.id, e))
      .finally(() => { this.land.drafting = false; if (this.x.s.land) this.emit(); });
  }
  private async gate(live: () => boolean): Promise<Done> {
    if (!this.defs.length) return 'skip';
    const t0 = Date.now();
    for (const [k, d] of this.defs.entries()) {
      this.land.gates[k] = { n: d.n, st: 'run' }; this.emit();
      const r = await run(d.cmd, d.args, d.dir, c => { this.child = c; });
      if (!live()) return 'skip';
      this.outs.set(d.n, r.out);
      if (r.code !== 0) { this.land.gates[k] = { n: d.n, st: 'er' }; return { why: `${d.n} 没过：\n${tail(r.out)}`, acts: ['fix', 'stay'] }; }
      const say = d.count?.(r.out);
      this.land.gates[k] = { n: d.n, st: 'ok', ...(say && /\d/.test(say) && d.group === 'desk' ? { say: `${d.n} ${say}` } : {}) };
    }
    this.wall = (Date.now() - t0) / 1000;
    return 'ok';
  }
  // What the gates printed, as the commit skill's evidence lines; never a number no command printed.
  private evidence() {
    const said = (n: string) => { const d = this.defs.find(x => x.n === n), o = this.outs.get(n); return d && o !== undefined ? d.count?.(o) ?? 'ok' : undefined; };
    const lines: string[] = [];
    if (this.defs.some(d => d.group === 'py')) lines.push(`Tier 1: lint-imports ${said('lint-imports')} · ruff ${said('ruff')} · mypy ${said('mypy')} · ${said('pytest')} acceptance checks pass · uv audit ${said('uv audit')} · wall ${this.wall.toFixed(2)}s (${this.wall < 30 ? '< 30s budget' : 'over 30s budget'}).`);
    else if (this.c && isJarvis(this.c.top) && !this.defs.length) lines.push('Tier 1: docs-only — gates not run.');
    if (this.defs.some(d => d.group === 'desk')) lines.push(`Desktop: tsc ${said('tsc')} · build ${said('build')} · verify-companion ${said('verify-companion')} · verify-companion-live ${said('verify-companion-live')}.`);
    if (this.defs.some(d => d.group === 'adr')) lines.push(`ADR check: ${said('check_adrs')}.`);
    return lines.join('\n');
  }
  private async commit(_: () => boolean): Promise<Done> {
    const c = this.c!;
    if (!c.uncommitted.length) return 'skip';
    if (this.draft) await Promise.race([this.draft, new Promise(r => setTimeout(r, 90e3))]);
    const title = this.land.msg.trim();
    if (!title) return { why: '提交标题是空的：在上面写一句，再按「继续」', acts: ['resume', 'stay'] };
    const body = this.body || c.uncommitted.map(p => `- ${p}`).join('\n');
    const file = path.join(tmpdir(), `jarvis-land-${this.x.s.id}.txt`);
    await writeFile(file, [title, body, this.evidence()].filter(Boolean).join('\n\n') + '\n');
    try {
      for (let k = 0; k < c.uncommitted.length; k += 200) {
        const r = await run('git', ['add', '--', ...c.uncommitted.slice(k, k + 200)], c.top, h => { this.child = h; });
        if (r.code !== 0) return { why: tail(r.out), acts: ['fix', 'stay'] };
      }
      const r = await run('git', ['commit', '-F', file], c.top, h => { this.child = h; });
      if (r.code !== 0) return { why: `提交没成：\n${tail(r.out)}`, acts: ['fix', 'stay'] };
    } finally { await rm(file, { force: true }); }
    return 'ok';
  }
  private async merge(_: () => boolean): Promise<Done> {
    const c = this.c!;
    if (c.branch === 'main') return 'skip';
    const repo = this.x.repo, head = (await git(repo, 'branch', '--show-current').catch(() => '')).trim();
    if (head !== 'main') return { why: `主检出现在在 ${head || '一个分离的提交'} 上，不在 main：先切回 main 再继续`, acts: ['resume', 'stay'] };
    if (!await git(c.top, 'merge-base', '--is-ancestor', 'main', 'HEAD').then(() => true, () => false)) {
      // Only this branch's own commits move onto main; a conflict that needs judgment stops the line with both sides named.
      const r = await run('git', ['rebase', 'main'], c.top, h => { this.child = h; });
      if (r.code !== 0) {
        const both = (await git(c.top, 'diff', '--name-only', '--diff-filter=U').catch(() => '')).split('\n').filter(Boolean);
        await run('git', ['rebase', '--abort'], c.top);
        return { why: both.length ? `合入冲突：${both.slice(0, 4).join('、')}${both.length > 4 ? ` 等 ${both.length} 个` : ''}两边都改了` : `挪到 main 上没成：\n${tail(r.out)}`, acts: ['fix', 'stay'] };
      }
    }
    const r = await run('git', ['merge', '--ff-only', c.branch], repo, h => { this.child = h; });
    return r.code === 0 ? 'ok' : { why: `合不进 main：\n${tail(r.out)}`, acts: ['resume', 'stay'] };
  }
  private async restart(_: () => boolean): Promise<Done> {
    const ls = this.land.restart ?? [];
    if (!ls.length) return 'skip';
    for (const l of ls) {
      if (l === 'companion') { await mkdir(DIR, { recursive: true }); await writeFile(REOPEN(), JSON.stringify({ id: this.x.s.id, at: Date.now() })); }
      const r = await kick(LABEL[l]);
      if (r.code !== 0) return { why: `${l} 没重启：\n${tail(r.out)}`, acts: ['resume', 'stay'] };
    }
    return 'ok';
  }
  private async push(_: () => boolean): Promise<Done> {
    const repo = this.x.repo;
    if (!(await git(repo, 'remote', 'get-url', 'origin').catch(() => '')).trim()) return 'skip';
    if (!this.go) {
      Object.assign(this.land, { s: 'wait', why: '推送前停下来等你：你点「允许」才推。拒绝的话，main 留在本地。', acts: ['allow', 'deny'] });
      this.mark(at('push'), 'wait'); this.emit();
      return 'wait';
    }
    this.go = false;
    const r = await run('git', ['push', 'origin', 'main'], repo, h => { this.child = h; }, 5 * 60e3);
    return r.code === 0 ? 'ok' : { why: `没推上去：\n${tail(r.out)}`, acts: ['resume', 'stay'] };
  }
  private async clean(_: () => boolean): Promise<Done> {
    const c = this.c!;
    if (c.branch === 'main' || !this.x.s.tree) return 'skip';
    if (sharing(this.x)) { this.land.steps[at('clean')].d = '分叉还在用这个 worktree，先留着'; return 'skip'; }
    await this.x.driver.release(this.x).catch(() => {});
    const r = await run('git', ['worktree', 'remove', c.top], this.x.repo, h => { this.child = h; });
    if (r.code !== 0) return { why: `worktree 没删掉：\n${tail(r.out)}`, acts: ['resume', 'stay'] };
    await run('git', ['branch', '-d', c.branch], this.x.repo);
    this.x.set({ tree: false, gone: true });
    return 'ok';
  }
}

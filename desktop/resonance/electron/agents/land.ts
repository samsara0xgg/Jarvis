// ADR 0097: landing. One line from a session's changes to its repository's default branch, run here in the host so it
// keeps going while the window or the companion restarts (it can restart the companion itself). Seven steps: the
// changes, the gates, a commit with explicit paths and every hook, a fast-forward into the default branch, a restart of
// what the change touches, the push once the owner says so, and the worktree gone when git agrees. A pull request
// landing pushes the session's branch instead and opens the pull request: no merge, no restart, the worktree stays. In
// the Jarvis repository it is docs/git-guide.md §3.
import { execFile, spawn, type ChildProcess } from 'node:child_process';
import { existsSync } from 'node:fs';
import { access, constants, mkdir, open, readFile, rm, writeFile } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import { query } from '@anthropic-ai/claude-agent-sdk';
import { claudeEnv, EXE } from './claude.js';
import { which } from './doctor.js';
import { GIT } from './files.js';
import { DIR, log, sharing, type Session } from './host.js';
import { auth, settings } from './settings.js';
import type { Land, LandSt, LandVia } from './types.js';

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
const git = async (cwd: string, ...args: string[]) => (await exec('git', [...GIT, '-C', cwd, ...args], { maxBuffer: 64 << 20, env: shellEnv() })).stdout;
const kill = (c: ChildProcess) => { try { process.kill(-c.pid!, 'SIGTERM'); } catch { c.kill('SIGTERM'); } };
// A command in its own process group, so stopping a gate stops everything it started; the last 96 kB of what it said.
function run(cmd: string, args: string[], cwd: string, hold?: (c: ChildProcess | null) => void, ms = 20 * 60e3, extra: Record<string, string> = {}) {
  return new Promise<{ code: number; out: string }>(done => {
    let out = '', ended = false;
    const c = spawn(cmd, args, { cwd, env: { ...shellEnv(), ...extra }, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
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
// The branch a repository lands into: the one origin's HEAD names (a clone records it), else main, else master, else
// whatever its main checkout is on.
const has = (top: string, ref: string) => git(top, 'show-ref', '--verify', '-q', ref).then(() => true, () => false);
export async function defaultBranch(top: string) {
  const o = (await git(top, 'symbolic-ref', '-q', '--short', 'refs/remotes/origin/HEAD').catch(() => '')).trim();
  if (o.startsWith('origin/')) return o.slice('origin/'.length);
  for (const b of ['main', 'master']) if (await has(top, `refs/heads/${b}`)) return b;
  return (await git(top, 'branch', '--show-current').catch(() => '')).trim() || 'main';
}
// A branch lands against where it left the default branch (origin's copy when there is no local one); a session
// working on the default branch itself lands what origin has not got yet.
export type Changes = { top: string; branch: string; into: string; base: string; linear: boolean; files: [string, number, number][]; ahead: number; uncommitted: string[] };
export async function changesOf(x: Session): Promise<Changes | null> {
  if (!existsSync(x.s.cwd)) return null;
  const top = (await git(x.s.cwd, 'rev-parse', '--show-toplevel').catch(() => '')).trim();
  if (!top) return null;
  const branch = (await git(top, 'branch', '--show-current').catch(() => '')).trim();
  if (!branch) return null;
  const into = await defaultBranch(top);
  let base = '', linear = true;
  if (branch === into) base = (await git(top, 'rev-parse', '--verify', '-q', '@{u}').catch(() => '')).trim() || 'HEAD';
  else {
    const ref = await has(top, `refs/heads/${into}`) ? into : `origin/${into}`;
    base = (await git(top, 'merge-base', ref, 'HEAD').catch(() => '')).trim();
    linear = await git(top, 'merge-base', '--is-ancestor', ref, 'HEAD').then(() => true, () => false);
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
  return { top, branch, into, base, linear, files: [...files].map(([p, [a, d]]) => [p, a, d]), ahead, uncommitted };
}
// How the changes can land, the default first (ADR 0097). Merge fast-forwards the session's own worktree branch into the
// default branch, or commits on the default branch itself; a pull request needs origin and a branch that is not the
// default, and is the only way for one outside the session's own worktree. The owner's choice for the repository
// comes first, then merge in Jarvis and a pull request anywhere else.
const originOf = async (top: string) => (await git(top, 'remote', 'get-url', 'origin').catch(() => '')).trim();
export async function waysOf(x: Session, c: Changes): Promise<LandVia[]> {
  const origin = !!await originOf(c.top), onto = c.branch !== c.into;
  const ways: LandVia[] = !onto ? ['merge'] : x.s.tree ? origin ? ['merge', 'pr'] : ['merge'] : origin ? ['pr'] : [];
  const want = settings.land?.[x.repo]?.via ?? (isJarvis(c.top) ? 'merge' : 'pr');
  return ways.includes(want) ? [want, ...ways.filter(w => w !== want)] : ways;
}
export async function dirtyOf(x: Session) {
  const c = await changesOf(x).catch(() => null);
  if (!c || (!c.files.length && !c.ahead)) return undefined;
  const ways = await waysOf(x, c);
  if (!ways.length) return undefined;
  // A branch whose pull request a landing opened has nothing to land until something is new since its last push.
  if (ways[0] === 'pr' && x.s.pr && !c.uncommitted.length && await git(c.top, 'rev-list', '--count', '@{u}..HEAD').then(n => n.trim() === '0', () => false)) return undefined;
  // Review 18: the landing shows only its own steps, and the first one in a repository that could go either way asks.
  const paths = c.files.map(f => f[0]), gates = gatesFor(x.repo, c.top, paths).defs.map(d => d.n), restart = restartFor(x.repo, c.top, paths).labels;
  const ask = ways.length > 1 && !settings.land?.[x.repo]?.via && !isJarvis(c.top), local = !await originOf(c.top);
  return { n: c.files.length, add: c.files.reduce((n, f) => n + f[1], 0), del: c.files.reduce((n, f) => n + f[2], 0), ahead: c.ahead, into: c.into, ways,
    ...ask ? { ask } : {}, ...gates.length ? { gates } : {}, ...restart.length ? { restart } : {}, ...local ? { local } : {} };
}

// ---------- the gates and the restart: the owner's for the repository, else the Jarvis repository's own ----------
// The owner's gates and restart command run in their login shell, as the setup script of a new worktree does.
const SHELL = () => process.env.SHELL || '/bin/zsh';
const cut = (t: string, n = 40) => t.length > n ? `${t.slice(0, n - 1)}…` : t;
// The Jarvis repository's (docs/git-guide.md §1 and §3).
const isJarvis = (top: string) => existsSync(path.join(top, 'jarvis', '__init__.py')) && existsSync(path.join(top, 'desktop', 'resonance', 'package.json'));
const PY = /\.py$|^(jarvis|tests|scripts|tools|plugins|config)\/|^(pyproject\.toml|uv\.lock|\.importlinter)$/;
const DESK = /^desktop\/resonance\//;
const ADR = /^docs\/adr\//;
const DAEMON = /^(jarvis|plugins|config)\/|^(uv\.lock|pyproject\.toml)$/;
type GateDef = { n: string; cmd: string; args: string[]; dir: string; group: 'py' | 'desk' | 'adr' | 'own'; count?: (out: string) => string };
const UV = (...a: string[]) => ['run', '--frozen', '--no-sync', ...a];
function gatesFor(repo: string, top: string, paths: string[]): { defs: GateDef[]; say: string } {
  const own = settings.land?.[repo]?.gates;
  if (own?.length) return { defs: own.map(g => ({ n: cut(g), cmd: SHELL(), args: ['-lc', g], dir: top, group: 'own' })), say: `你给这个仓库配的门禁，${own.length} 项` };
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
// own: the owner's restart command for the repository, run in the main checkout once the default branch has the change.
function restartFor(repo: string, top: string, paths: string[]): { labels: string[]; own?: string; say: string } {
  const own = settings.land?.[repo]?.restart;
  if (own) return { labels: [cut(own)], own, say: '跑你给这个仓库配的重启命令' };
  if (!isJarvis(top)) return { labels: [], say: '这个仓库没配重启' };
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
// gh opens the pull request. JARVIS_AGENTS_GH names the one to use instead (a check's stand-in); while no file is
// there, there is no gh.
async function ghExe() {
  const own = process.env.JARVIS_AGENTS_GH;
  if (own === undefined) return which('gh', shellEnv().PATH);
  return access(own, constants.X_OK).then(() => own, () => undefined);
}

// ---------- the commit message: drafted from the diff with the commit skill as the instructions ----------
// A repository with no commit skill gets a message in the style of its own recent commits.
async function draftMessage(top: string, title: string, said: string, paths: string[]): Promise<{ title: string; body: string } | null> {
  // Without Claude's sign-in (the packaged app with no key yet) the title is left to the owner.
  if (!auth().ready) return null;
  const skill = await readFile(path.join(top, '.claude', 'skills', 'commit', 'SKILL.md'), 'utf8').catch(() => '');
  const guide = skill ? await readFile(path.join(top, 'docs', 'git-guide.md'), 'utf8').then(t => /## 2\.[\s\S]*?(?=\n## 3\.)/.exec(t)?.[0] ?? '', () => '') : '';
  const recent = skill ? '' : (await git(top, 'log', '-15', '--no-merges', '--format=%s').catch(() => '')).trim();
  const stat = await git(top, 'diff', '--stat', 'HEAD', '--', ...paths).catch(() => '');
  let diff = await git(top, 'diff', 'HEAD', '--', ...paths).catch(() => '');
  for (const p of paths) if (!stat.includes(p) && existsSync(path.join(top, p))) diff += `\n--- new file ${p}\n${(await readFile(path.join(top, p), 'utf8').catch(() => '')).slice(0, 3000)}`;
  const system = skill
    ? `You write one git commit message, following the project's commit skill below. Write only: the title line, a blank line, a one-line opener, a blank line, then one bullet per file or tightly related file group ("- <path> — <what it contributes>"), wrapped near 72 columns. English only. No Tier 1 or verification line, no trailers, no code fences, no commentary.\n\n<commit-skill>\n${skill.slice(0, 8000)}\n</commit-skill>${guide ? `\n\n<scopes>\n${guide.slice(0, 7000)}\n</scopes>` : ''}`
    : `You write one git commit message in the style of this repository's recent commit titles below: the same language, form and prefixes. Write only: the title line, at most 72 characters, a blank line, then a short body of what changed and why, wrapped near 72 columns. No trailers, no code fences, no commentary.${recent ? `\n\n<recent-titles>\n${recent.slice(0, 3000)}\n</recent-titles>` : ''}`;
  const prompt = `The coding session: ${title}\n\nWhat it said last:\n${said.slice(0, 3000)}\n\nChanged files:\n${stat.slice(0, 4000)}\n\nThe diff (may be cut):\n${diff.slice(0, 40000)}`;
  const q = query({ prompt, options: { model: HAIKU, maxTurns: 1, tools: [], systemPrompt: system, cwd: top, env: claudeEnv(), pathToClaudeCodeExecutable: EXE, settingSources: [], persistSession: false } });
  let text = '';
  for await (const m of q) if (m.type === 'result' && m.subtype === 'success') text = m.result;
  const ls = text.replace(/```[a-z]*\n?|```/g, '').trim().split('\n');
  const head = ls.findIndex(l => l.trim()), t = ls[head]?.trim() ?? '';
  if (!t || t.length > 72 || (skill && !/^[a-z]+(\([^)\s]+\))?!?: \S/.test(t))) return null;
  return { title: t, body: ls.slice(head + 1).join('\n').trim() };
}

// ---------- the line itself ----------
// The gates and the restart it expects are there from the start, so the window draws the steps this landing has at once.
const fresh = (x: Session): Land => ({
  s: 'run', i: 0, steps: STEPS.map(() => ({ st: 'todo' as LandSt })), files: [], gates: (x.s.dirty?.gates ?? []).map(n => ({ n, st: 'todo' as const })), msg: '', branch: x.s.branch,
  into: x.s.dirty?.into ?? 'main', via: x.s.dirty?.ways[0] ?? 'merge', ...x.s.dirty?.restart ? { restart: x.s.dirty.restart } : {},
});
export class Landing {
  land: Land;
  // tok: bumped to abandon whatever run is going · halt: stop once this step is done · go: the push was allowed
  private tok = 0; private halt = false; private go = false; private child: ChildProcess | null = null;
  private c: Changes | null = null; private defs: GateDef[] = []; private outs = new Map<string, string>(); private wall = 0;
  private body = ''; private edited = false; private draft: Promise<void> | null = null; private failed = 0; private fixing = false;
  // pre: a title the owner wrote before pressing 一键落地; it is the commit's, and nothing is drafted · want: the way the
  // owner picked, taken when this session can land that way · own: the owner's restart command for the repository
  private pre = ''; private want?: LandVia; private own?: string;
  constructor(private x: Session) { this.land = fresh(x); }
  private emit() { this.x.set({ land: structuredClone(this.land) }); }
  private mark(i: number, st: LandSt, ms?: number) { this.land.steps[i] = { ...this.land.steps[i], st, ...(ms !== undefined ? { ms } : {}) }; }
  get busy() { return ['run', 'stopping', 'wait', 'fixing'].includes(this.land.s) && !!this.x.s.land; }

  start(via?: LandVia) {
    if (this.busy) return;
    const s = this.x.s.land?.s;
    if (s === 'paused' || s === 'fail') { this.resume(); return; }
    this.want = via;
    this.land = fresh(this.x); this.land.msg = this.pre; this.edited = !!this.pre; this.pre = '';
    if (via && this.x.s.dirty?.ways.includes(via)) { this.land.via = via; if (via === 'pr') this.land.restart = undefined; }
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
  deny() {
    if (this.land.s !== 'wait') return;
    const { via, into, branch } = this.land;
    this.pause(at('push'), `没推：${via === 'pr' ? '提交留在这个分支上，没开 PR' : branch === into ? `提交留在本地的 ${into} 上` : `${into} 已经合好了，只是留在本地`}。「继续」会再问你一次。`);
  }
  // A step's name as the window shows it.
  private title(i: number) {
    return STEPS[i] === 'merge' ? `合进 ${this.land.into}` : STEPS[i] === 'push' && this.land.via === 'pr' ? '推分支、开 PR' : ['改动', '门禁', '提交', '', '重启', '推送', '清理 worktree'][i];
  }
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
    const what = STEPS[i] === 'gate' && g ? `「门禁」：${g.n} 没过` : `「${this.title(i)}」`;
    const text = `落地停在${what}。\n\n\`\`\`\n${(g ? this.outs.get(g.n) ?? '' : '').split('\n').slice(-60).join('\n') || this.land.why}\n\`\`\`\n\n请把它修好。修完我会从这一步重新跑落地，不用你提交或合入。`;
    this.failed = i; this.fixing = true;
    this.land.s = 'fixing'; this.land.why = `${this.x.s.agent === 'codex' ? 'Codex' : 'Claude'} 在修，修完从「${this.title(i)}」重新跑`; this.land.acts = ['stay'];
    this.emit();
    await this.x.ensureLoaded();
    this.x.set({ unread: false, updated: Date.now() });
    await this.x.driver.send(this.x, text, []);
  }
  // The session's state moved: a fix that ended runs the line again; one that stopped leaves it paused.
  saw(st: string) {
    if (!this.fixing || this.x.s.land?.s !== 'fixing') return;
    if (st === 'done') { this.fixing = false; this.land.gates = this.land.gates.map(g => ({ n: g.n, st: 'todo' })); this.draft = null; void this.from(this.failed <= at('gate') ? 0 : this.failed); }
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
    if (!c) return { why: '读不出这个会话的 git 状态：不在 git 仓库里，或者没在一个分支上', acts: ['stay'] };
    if (!c.files.length && !c.ahead) return { why: '没有要落地的改动', acts: ['stay'] };
    const ways = await waysOf(this.x, c);
    if (!ways.length) return { why: `这个会话不在自己的 worktree 里，也不在 ${c.into} 上，仓库又没有 origin：没法落地`, acts: ['stay'] };
    const via = this.want && ways.includes(this.want) ? this.want : ways[0], pr = via === 'pr', into = c.into;
    const paths = c.files.map(f => f[0]);
    Object.assign(this.land, { files: c.files, branch: c.branch, into, via });
    const g = gatesFor(this.x.repo, c.top, paths), r = restartFor(this.x.repo, c.top, paths);
    this.defs = g.defs; this.land.gates = g.defs.map(d => ({ n: d.n, st: 'todo' })); this.land.restart = pr ? [] : r.labels; this.own = r.own;
    const s = this.land.steps, onto = c.branch !== into, tree = onto && this.x.s.tree, gh = pr && !!await ghExe(), origin = !!await originOf(c.top);
    s[at('gate')].d = g.say;
    s[at('commit')].d = !c.uncommitted.length ? '改动都已经提交了' : existsSync(path.join(c.top, '.claude', 'skills', 'commit', 'SKILL.md')) ? 'commit skill 起草，跑之前可以改' : '照这个仓库最近的提交起草，跑之前可以改';
    s[at('merge')] = { ...s[at('merge')], d: pr ? `开 PR，不合进 ${into}` : onto ? undefined : `就在 ${into} 上`, cmd: !pr && onto ? [...c.linear ? [] : [`git rebase ${into}`], `git merge --ff-only ${c.branch}`] : undefined };
    s[at('restart')] = { ...s[at('restart')], d: pr ? '开 PR 不重启' : r.say, cmd: pr ? undefined : r.own ? [r.own] : r.labels.map(l => `launchctl kickstart -k gui/$UID/${LABEL[l]}`) };
    s[at('push')] = { ...s[at('push')], d: !origin ? '这个仓库没有 origin，不推' : pr && !gh ? '等你点头 · 没装 gh，推完给你开 PR 的链接' : '等你点头',
      cmd: !origin ? undefined : pr ? [`git push -u origin ${c.branch}`, ...gh ? [`gh pr create --base ${into} --head ${c.branch}`] : []] : [`git push origin ${into}`] };
    s[at('clean')] = { ...s[at('clean')], d: pr ? 'PR 还开着，worktree 留着接着改' : tree ? undefined : '不是它自己的 worktree', cmd: !pr && tree ? ['git worktree remove', 'git branch -d'] : undefined };
    if (c.uncommitted.length && !this.draft && !this.edited) this.startDraft(c);
    return 'ok';
  }
  // Its last answer goes with the diff, so the conversation is read first when nothing has opened it yet (B23).
  private startDraft(c: Changes) {
    this.land.drafting = true;
    this.draft = this.x.ensureLoaded().then(() => {
      const last = [...this.x.items ?? []].reverse().find(it => it.k === 'it');
      return draftMessage(c.top, this.x.s.title, last?.k === 'it' ? last.text : '', c.uncommitted);
    }).then(d => { if (d) { if (!this.edited) this.land.msg = d.title; this.body = d.body; } }, e => log('draft', this.x.s.id, e))
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
    const c = this.c!, into = this.land.into;
    if (this.land.via === 'pr' || c.branch === into) return 'skip';
    const repo = this.x.repo, head = (await git(repo, 'branch', '--show-current').catch(() => '')).trim();
    if (head !== into) return { why: `主检出现在在 ${head || '一个分离的提交'} 上，不在 ${into}：先切回 ${into} 再继续`, acts: ['resume', 'stay'] };
    if (!await git(c.top, 'merge-base', '--is-ancestor', into, 'HEAD').then(() => true, () => false)) {
      // Only this branch's own commits move onto the default branch; a conflict that needs judgment stops the line with
      // both sides named.
      const r = await run('git', ['rebase', into], c.top, h => { this.child = h; });
      if (r.code !== 0) {
        const both = (await git(c.top, 'diff', '--name-only', '--diff-filter=U').catch(() => '')).split('\n').filter(Boolean);
        await run('git', ['rebase', '--abort'], c.top);
        return { why: both.length ? `合入冲突：${both.slice(0, 4).join('、')}${both.length > 4 ? ` 等 ${both.length} 个` : ''}两边都改了` : `挪到 ${into} 上没成：\n${tail(r.out)}`, acts: ['fix', 'stay'] };
      }
    }
    const r = await run('git', ['merge', '--ff-only', c.branch], repo, h => { this.child = h; });
    return r.code === 0 ? 'ok' : { why: `合不进 ${into}：\n${tail(r.out)}`, acts: ['resume', 'stay'] };
  }
  private async restart(_: () => boolean): Promise<Done> {
    if (this.land.via === 'pr') return 'skip';
    if (this.own) {
      const r = await run(SHELL(), ['-lc', this.own], this.x.repo, h => { this.child = h; }, 10 * 60e3);
      return r.code === 0 ? 'ok' : { why: `重启命令没跑成：\n${tail(r.out)}`, acts: ['resume', 'stay'] };
    }
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
    const repo = this.x.repo, { via, into } = this.land;
    if (!await originOf(repo)) return 'skip';
    if (!this.go) {
      Object.assign(this.land, { s: 'wait', acts: ['allow', 'deny'],
        why: via === 'pr' ? '推送前停下来等你：你点「允许」才推这个分支、开 PR。' : `推送前停下来等你：你点「允许」才推。拒绝的话，${into} 留在本地。` });
      this.mark(at('push'), 'wait'); this.emit();
      return 'wait';
    }
    this.go = false;
    if (via === 'pr') return this.pullRequest();
    const r = await run('git', ['push', 'origin', into], repo, h => { this.child = h; }, 5 * 60e3);
    return r.code === 0 ? 'ok' : { why: `没推上去：\n${tail(r.out)}`, acts: ['resume', 'stay'] };
  }
  // The branch goes to origin and a pull request asks for it against the default branch, opened with gh when it is
  // installed: the only commit's title and body, or the latest title over a list of them all. Without gh, the address
  // the remote gave for opening one. A pull request already open for the branch has just been updated by the push.
  private async pullRequest(): Promise<Done> {
    const c = this.c!, into = this.land.into, step = this.land.steps[at('push')];
    const p = await run('git', ['push', '-u', 'origin', c.branch], c.top, h => { this.child = h; }, 5 * 60e3);
    if (p.code !== 0) return { why: `分支没推上去：\n${tail(p.out)}`, acts: ['resume', 'stay'] };
    const link = [...p.out.matchAll(/^remote:\s+(https?:\/\/\S+)/gm)].map(m => m[1]).find(u => /pull|merge_request|compare/i.test(u)), exe = await ghExe();
    if (!exe) { this.land.pr = link; step.d = link ? '分支推上去了。没装 gh：点链接开 PR' : '分支推上去了。没装 gh，PR 要你自己开'; return 'ok'; }
    const ref = await has(c.top, `refs/heads/${into}`) ? into : `origin/${into}`;
    const log = (await git(c.top, 'log', '--reverse', '--format=%s%x1f%b%x1e', `${ref}..HEAD`)).split('\x1e').map(e => e.trim()).filter(Boolean).map(e => e.split('\x1f'));
    const title = log.at(-1)?.[0] || c.branch, body = log.length === 1 ? (log[0][1] ?? '').trim() : log.map(e => `- ${e[0]}`).join('\n');
    const r = await run(exe, ['pr', 'create', '--base', into, '--head', c.branch, '--title', title, '--body', body], c.top, h => { this.child = h; }, 2 * 60e3,
      { GH_PROMPT_DISABLED: '1', GH_NO_UPDATE_NOTIFIER: '1' });
    const url = /https?:\/\/\S+\/pull\/\d+/.exec(r.out)?.[0];
    if (r.code === 0 || (url && /already exists/i.test(r.out))) {
      this.land.pr = url ?? link; step.d = r.code === 0 ? 'PR 开好了' : '分支推上去了，PR 本来就开着';
      if (url) this.x.set({ pr: url });
      return 'ok';
    }
    return { why: `分支推上去了，PR 没开成：\n${tail(r.out)}`, acts: ['resume', 'stay'] };
  }
  private async clean(_: () => boolean): Promise<Done> {
    const c = this.c!;
    if (this.land.via === 'pr' || c.branch === this.land.into || !this.x.s.tree) return 'skip';
    if (sharing(this.x)) { this.land.steps[at('clean')].d = '分叉还在用这个 worktree，先留着'; return 'skip'; }
    await this.x.driver.release(this.x).catch(() => {});
    const r = await run('git', ['worktree', 'remove', c.top], this.x.repo, h => { this.child = h; });
    if (r.code !== 0) return { why: `worktree 没删掉：\n${tail(r.out)}`, acts: ['resume', 'stay'] };
    await run('git', ['branch', '-d', c.branch], this.x.repo);
    this.x.set({ tree: false, gone: true });
    return 'ok';
  }
}

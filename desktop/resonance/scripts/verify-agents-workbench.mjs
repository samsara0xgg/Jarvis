// Run after npm run build. The workbench's host side (ADR 0085-0087) against throwaway git repositories: the real agent
// host on a spare port, a stand-in daemon for the plans, a stand-in Claude Code (fake-claude.mjs) for the menus the host
// reads, nothing of Allen's sessions, services or remote touched. `--live` runs the real Claude Code instead and lets
// landing draft a commit title with it (one small Haiku call on the subscription).
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url)), app = path.join(here, '..');
const live = process.argv.includes('--live');
const tmp = await mkdtemp(path.join(os.tmpdir(), 'jarvis-workbench-check-'));
const git = (cwd, ...a) => execFileSync('git', ['-C', cwd, ...a], { encoding: 'utf8', env: { ...process.env, GIT_AUTHOR_NAME: 't', GIT_AUTHOR_EMAIL: 't@t', GIT_COMMITTER_NAME: 't', GIT_COMMITTER_EMAIL: 't@t' } }).trim();
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${detail}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };

// ---------- two repositories: a plain one with an origin, and one that looks like Jarvis ----------
const origin = path.join(tmp, 'origin.git'), repo = path.join(tmp, 'repo');
execFileSync('git', ['init', '-q', '--bare', '-b', 'main', origin]);
execFileSync('git', ['init', '-q', '-b', 'main', repo]); git(repo, 'remote', 'add', 'origin', origin);
await mkdir(path.join(repo, 'src'));
await writeFile(path.join(repo, 'README.md'), '# Check\n\nA [link](https://example.com).\n');
await writeFile(path.join(repo, 'src', 'a.ts'), 'export const a = 1;\nexport const b = 2;\n');
await writeFile(path.join(repo, 'index.html'), '<!doctype html><title>x</title><p>page</p>');
await writeFile(path.join(repo, 'shared.txt'), 'base\n');
await writeFile(path.join(repo, '.gitignore'), '.claude/\n');
git(repo, 'add', '-A'); git(repo, 'commit', '-q', '-m', 'init'); git(repo, 'push', '-q', '-u', 'origin', 'main');
const tree = (name, edit) => { const at = path.join(repo, '.claude', 'worktrees', name); git(repo, 'worktree', 'add', '-q', '-b', `worktree-${name}`, at, 'HEAD'); return edit(at).then(() => at); };
// t1: one change committed, one not yet, one new file; lands all the way to origin.
const t1 = await tree('t1', async at => {
  await writeFile(path.join(at, 'src', 'a.ts'), 'export const a = 1;\nexport const b = 3;\n'); git(at, 'commit', '-qam', 'change b');
  await writeFile(path.join(at, 'src', 'c.ts'), 'export const c = 1;\n');
  await writeFile(path.join(at, 'README.md'), '# Check\n\nA [link](https://example.com).\n\nMore.\n');
});
// t5 (--live only): an uncommitted change and no title, so landing drafts one.
const t5 = await tree('t5', async at => { await writeFile(path.join(at, 'src', 'double.ts'), 'export const double = (x: number) => x * 2;\n'); });
// t2: conflicts with a change main gets after it branched.
const t2 = await tree('t2', async at => { await writeFile(path.join(at, 'shared.txt'), 'mine\n'); git(at, 'commit', '-qam', 'mine'); });
await writeFile(path.join(repo, 'shared.txt'), 'theirs\n'); git(repo, 'commit', '-qam', 'theirs');
const jr = path.join(tmp, 'jarvis');
await mkdir(path.join(jr, 'jarvis'), { recursive: true }); await mkdir(path.join(jr, 'desktop', 'resonance'), { recursive: true });
await writeFile(path.join(jr, 'jarvis', '__init__.py'), ''); await writeFile(path.join(jr, 'desktop', 'resonance', 'package.json'), '{}');
execFileSync('git', ['init', '-q', '-b', 'main', jr]); git(jr, 'add', '-A'); git(jr, 'commit', '-q', '-m', 'init');
// t3: a daemon-side Python change in a repository shaped like Jarvis: its gates run and fail here, so nothing restarts.
const t3 = path.join(jr, '.claude', 'worktrees', 't3');
git(jr, 'worktree', 'add', '-q', '-b', 'worktree-t3', t3, 'HEAD');
await writeFile(path.join(t3, 'jarvis', 'x.py'), 'X = 1\n'); git(t3, 'add', '-A'); git(t3, 'commit', '-q', '-m', 'x');

// ---------- a runtime root of its own, a stand-in daemon, the real host ----------
const root = path.join(tmp, 'root'), dir = path.join(root, 'agents');
await mkdir(path.join(root, 'logs'), { recursive: true }); await mkdir(dir, { recursive: true });
await writeFile(path.join(root, 'plugin-access.json'), JSON.stringify({ token: 'workbench-check' }));
await writeFile(path.join(root, 'logs', 'resonance.out.log'), 'companion  single-instance lock held\ncompanion  ready\n');
const row = (id, cwd, branch, r, extra = {}) => ({ id, agent: 'claude', title: id, cwd, project: path.basename(r), branch, tree: true, st: 'done', pinned: false, parked: false, archived: false,
  unread: false, updated: Date.now(), summary: '', model: '', effort: '', mode: '', ctx: 0, repo: r, ...extra });
await writeFile(path.join(dir, 'sessions.json'), JSON.stringify({ v: 1, sessions: [
  row('00000000-0000-4000-8000-000000000001', t1, 'worktree-t1', repo), row('00000000-0000-4000-8000-000000000002', t2, 'worktree-t2', repo),
  row('00000000-0000-4000-8000-000000000003', t3, 'worktree-t3', jr), row('00000000-0000-4000-8000-000000000004', repo, 'main', repo, { tree: false }),
  row('00000000-0000-4000-8000-000000000005', t5, 'worktree-t5', repo),
] }));
const PLANS = { services: { claude: { status: 'ok', data: { plan: '5X', windows: [{ key: 'five_hour', label: '5 小时', percent: 12, resets_at: '2026-09-29T17:00:00+00:00' }, { key: 'seven_day', label: '7 天 · 总', percent: 100, resets_at: '2026-10-06T12:00:00+00:00' }] } } } };
const daemon = http.createServer((q, r) => { r.writeHead(200, { 'Content-Type': 'application/json' }); r.end(JSON.stringify(q.url === '/inherent/usage' ? PLANS : q.url === '/inherent/agent-marks' ? { marks: {} } : {})); });
await new Promise(r => daemon.listen(0, '127.0.0.1', r));
const port = 18000 + Math.floor(Math.random() * 1000), API = `http://127.0.0.1:${port}`, H = { Authorization: 'Bearer workbench-check', 'Content-Type': 'application/json' };
const electron = path.join(app, 'node_modules', '.bin', 'electron');
const host = spawn(electron, [path.join(app, 'dist-electron', 'agents', 'host.js')], { stdio: ['ignore', 'pipe', 'pipe'],
  env: { ...process.env, ELECTRON_RUN_AS_NODE: '1', JARVIS_RUNTIME_ROOT: root, JARVIS_AGENTS_DIR: dir, JARVIS_AGENTS_PORT: String(port), JARVIS_INHERENT_BRIDGE_PORT: String(daemon.address().port),
    ...live ? {} : { JARVIS_AGENTS_CLAUDE: path.join(here, 'fake-claude.mjs'), CLAUDE_CONFIG_DIR: path.join(tmp, 'claude-config') } } });
let hostLog = ''; host.stdout.on('data', b => { hostLog += b; }); host.stderr.on('data', b => { hostLog += b; });
// The host starts a keeper for its Claude children (ADR 0082); it goes with the check.
const done = async () => { host.kill(); daemon.close(); try { execFileSync('pkill', ['-f', `keeper.js ${dir}`]); } catch { /* none left */ } await rm(tmp, { recursive: true, force: true }); };
process.on('uncaughtException', async e => { console.error(e, '\n--- host ---\n', hostLog.slice(-3000)); await done(); process.exit(1); });

const call = async (route, body, method = body === undefined ? 'GET' : 'POST') => {
  const r = await fetch(API + route, { method, headers: H, body: body === undefined ? undefined : JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  return { status: r.status, ...j };
};
for (let i = 0; i < 80; i++) { if ((await fetch(`${API}/health`, { headers: H }).catch(() => null))?.ok) break; await new Promise(r => setTimeout(r, 150)); }
// The event stream, kept: the latest row of every session.
const rows = new Map();
const events = await fetch(`${API}/events`, { headers: H });
void (async () => {
  let buf = '';
  try { for await (const chunk of events.body) {
    buf += Buffer.from(chunk).toString('utf8');
    for (let at = buf.indexOf('\n\n'); at >= 0; at = buf.indexOf('\n\n')) {
      const line = buf.slice(0, at); buf = buf.slice(at + 2);
      if (!line.startsWith('data: ')) continue;
      const e = JSON.parse(line.slice(6));
      if (e.t === 'hello') for (const s of e.sessions) rows.set(s.id, s);
      if (e.t === 'sess') rows.set(e.s.id, e.s);
    }
  } } catch { /* the host went away at the end */ }
})();
const until = async (what, pred, ms = 20000) => {
  const t0 = Date.now();
  for (;;) { const v = pred(); if (v) return v; if (Date.now() - t0 > ms) throw new Error(`timed out: ${what} · ${JSON.stringify([...rows.values()].map(s => [s.id.slice(-1), s.land?.s, s.land?.i, s.land?.why]))}`); await new Promise(r => setTimeout(r, 60)); }
};
const S5 = '00000000-0000-4000-8000-000000000005';
const S1 = '00000000-0000-4000-8000-000000000001', S2 = '00000000-0000-4000-8000-000000000002', S3 = '00000000-0000-4000-8000-000000000003', S4 = '00000000-0000-4000-8000-000000000004';

// ---------- what each session would land ----------
const d1 = await until('t1 measured', () => rows.get(S1)?.dirty);
check('dirty counts committed, uncommitted and new files against main', d1.n === 3 && d1.ahead === 1 && d1.add >= 3, JSON.stringify(d1));
const d4 = (await until('hello', () => rows.get(S4)?.dirty));
check('a session on main lands what origin has not got', d4.ahead === 1 && d4.n === 1, JSON.stringify(d4));

// ---------- previews ----------
const peek = ref => call(`/sessions/${S1}/peek?ref=${encodeURIComponent(ref)}`);
const pm = await peek('README.md'), pt = await peek('src/a.ts:2'), pw = await peek('index.html'), pn = await peek('nope/missing.md');
check('markdown comes as text', pm.kind === 'md' && pm.text.includes('More.'));
check('code comes with what changed against main', pt.kind === 'text' && pt.add === 1 && pt.del === 1 && pt.diff.some(d => d[0] === '+' && d[1].includes('b = 3')));
check('a page comes as a file address for the preview browser', pw.kind === 'web' && pw.url.startsWith('file://') && pw.url.endsWith('/index.html'));
check('a missing file is a 404 with its name', pn.status === 404 && pn.error.includes('nope/missing.md'));

// ---------- the plans, services and logs ----------
const u = await call('/usage');
check('plans come through from the daemon', u.claude?.plan === '5X' && u.claude.windows.length === 2);
const sv = await call('/services');
check('both LaunchAgents are listed', sv.services.map(s => s.name).join() === 'daemon,companion' && sv.services.every(s => typeof s.running === 'boolean'));
const lg = await call('/logs?name=companion'), lg2 = await call(`/logs?name=companion&from=${lg.size}`);
check('the log tab reads the file, then only what is new', lg.text.includes('companion  ready') && lg2.text === '');

// ---------- the terminal: a real shell in the session's folder ----------
await call(`/term/${S1}`, { cols: 80, rows: 12 });
const out = []; const ts = await fetch(`${API}/term/${S1}/stream`, { headers: H });
void (async () => { try { for await (const c of ts.body) out.push(Buffer.from(c).toString('utf8')); } catch { /* closed */ } })();
await call(`/term/${S1}/input`, { data: 'echo wb-$((40+2)) && pwd\r' });
await until('shell echo', () => out.join('').replace(/\\u001b\[[0-9;?]*[a-zA-Z]/g, '').includes('wb-42'), 15000);
check('the terminal runs in the session folder', out.join('').includes(path.basename(t1)));
await call(`/term/${S1}`, undefined, 'DELETE');

// ---------- landing all the way: t1 into main and origin ----------
await call(`/sessions/${S1}/land`, { action: 'msg', msg: 'feat(check): land the workbench check' });
await call(`/sessions/${S1}/land`, { action: 'start' });
let l = await until('t1 at push', () => rows.get(S1)?.land?.s === 'wait' && rows.get(S1).land);
check('changes list every file with its lines', l.files.length === 3 && l.files.some(f => f[0] === 'src/c.ts'));
check('no gates and no restart outside Jarvis', l.steps[1].st === 'skip' && l.steps[4].st === 'skip');
check('the commit takes the typed title, only explicit paths, hooks on', git(repo, 'log', '-1', '--format=%s', 'main') === 'feat(check): land the workbench check' && git(t1, 'status', '--porcelain') === '');
check('main fast-forwards to the branch, no merge commit', git(repo, 'rev-parse', 'main') === git(t1, 'rev-parse', 'HEAD') && git(repo, 'log', '--merges', '--oneline', 'main') === '');
check('the push waits for Allen', l.steps[5].st === 'wait' && l.acts.join() === 'allow,deny' && git(origin, 'rev-parse', 'main') !== git(repo, 'rev-parse', 'main'));
await call(`/sessions/${S1}/land`, { action: 'deny' });
l = await until('t1 denied', () => rows.get(S1)?.land?.s === 'paused' && rows.get(S1).land);
check('saying no keeps main local and pauses at the push', l.i === 5 && l.why.includes('留在本地'));
await call(`/sessions/${S1}/land`, { action: 'resume' });
await until('t1 asks again', () => rows.get(S1)?.land?.s === 'wait');
await call(`/sessions/${S1}/land`, { action: 'allow' });
l = await until('t1 landed', () => rows.get(S1)?.land?.s === 'done' && rows.get(S1).land);
check('the push reaches origin', git(origin, 'rev-parse', 'main') === git(repo, 'rev-parse', 'main'));
check('the worktree and its branch are gone, the session stays', !existsSync(t1) && !git(repo, 'branch', '--list', 'worktree-t1') && rows.get(S1).gone === true && rows.get(S1).tree === false);
check('each finished step has its time', l.steps.filter(s => s.st === 'ok').every(s => typeof s.ms === 'number'));
const gone = await call(`/sessions/${S1}/send`, { text: 'more' });
check('a landed session with its worktree gone refuses new messages', gone.status === 409);

// ---------- a conflict stops the line with both sides named, and leaves the branch as it was ----------
await call(`/sessions/${S2}/land`, { action: 'start' });
l = await until('t2 conflict', () => rows.get(S2)?.land?.s === 'fail' && rows.get(S2).land);
check('a conflict names the file and offers the fix or the branch', l.i === 3 && l.why.includes('shared.txt') && l.acts.join() === 'fix,stay');
check('the rebase was put back', git(t2, 'status', '--porcelain') === '' && (await readFile(path.join(t2, 'shared.txt'), 'utf8')) === 'mine\n' && !existsSync(path.join(t2, '.git', 'rebase-merge')));
await call(`/sessions/${S2}/land`, { action: 'stay' });
await until('t2 idle', () => rows.get(S2) && !rows.get(S2).land);
check('留在分支上 ends the line and leaves main alone', git(repo, 'log', '-1', '--format=%s', 'main') === 'feat(check): land the workbench check');

// ---------- Jarvis's gates: a Python change picks Tier 1, a failing gate stops the line before anything restarts ----------
await call(`/sessions/${S3}/land`, { action: 'start' });
l = await until('t3 gate', () => rows.get(S3)?.land?.s === 'fail' && rows.get(S3).land, 60000);
check('a Python change picks the Tier 1 gates', ['lint-imports', 'ruff', 'mypy', 'pytest', 'uv audit'].every(n => l.gates.some(g => g.n === n)) && l.steps[1].d.includes('Tier 1'));
check('a daemon-side change plans a daemon restart', l.restart.join() === 'daemon' && l.steps[4].cmd[0].endsWith('com.allen.jarvis'));
check('a red gate stops at the gates with what it said', l.i === 1 && l.gates.some(g => g.st === 'er') && l.acts.join() === 'fix,stay' && l.steps[4].st === 'todo');
await call(`/sessions/${S3}/land`, { action: 'stay' });

// ---------- --live: the commit title drafted by Claude from the diff, in the commit skill's shape ----------
if (live) {
  await call(`/sessions/${S5}/land`, { action: 'start' });
  l = await until('t5 drafted', () => { const x = rows.get(S5)?.land; return x && !x.drafting && x.msg && x; }, 120000);
  check('Claude drafts a Conventional Commits title', /^[a-z]+(\([^)\s]+\))?!?: \S/.test(l.msg) && l.msg.length <= 72, l.msg);
  console.log(`  drafted: ${l.msg}`);
  await call(`/sessions/${S5}/land`, { action: 'stay' });
}
console.log(`${checks.length} checks passed`);
await done();
process.exit(0);

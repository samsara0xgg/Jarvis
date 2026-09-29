// Run after npm run build. The workbench's host side (ADR 0086, 0087, 0097) against throwaway git repositories: the real
// agent host on a spare port, a stand-in daemon for the plans, a stand-in Claude Code (fake-claude.mjs) for the menus
// the host reads and the commit titles it drafts, a stand-in gh, nothing of Allen's sessions, services or remote
// touched. `--live` runs the real Claude Code instead and lets landing draft a commit title with it (one small Haiku
// call on the subscription).
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { existsSync, realpathSync } from 'node:fs';
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
// t5 (--live only): an uncommitted change and no title, so landing drafts one, with the commit skill as its guide.
const t5 = await tree('t5', async at => {
  await writeFile(path.join(at, 'src', 'double.ts'), 'export const double = (x: number) => x * 2;\n');
  await mkdir(path.join(at, '.claude', 'skills', 'commit'), { recursive: true });
  await writeFile(path.join(at, '.claude', 'skills', 'commit', 'SKILL.md'), 'Title: `type(scope): English description`, Conventional Commits, at most 72 characters.\n');
});
// t2: conflicts with a change main gets after it branched.
const t2 = await tree('t2', async at => { await writeFile(path.join(at, 'shared.txt'), 'mine\n'); git(at, 'commit', '-qam', 'mine'); });
await writeFile(path.join(repo, 'shared.txt'), 'theirs\n'); git(repo, 'commit', '-qam', 'theirs');
const jr = path.join(tmp, 'jarvis');
await mkdir(path.join(jr, 'jarvis'), { recursive: true }); await mkdir(path.join(jr, 'desktop', 'resonance'), { recursive: true });
await writeFile(path.join(jr, 'jarvis', '__init__.py'), ''); await writeFile(path.join(jr, 'desktop', 'resonance', 'package.json'), '{}');
execFileSync('git', ['init', '-q', '-b', 'main', jr]); git(jr, 'add', '-A'); git(jr, 'commit', '-q', '-m', 'init');
execFileSync('git', ['init', '-q', '--bare', '-b', 'main', path.join(tmp, 'jarvis-origin.git')]); git(jr, 'remote', 'add', 'origin', path.join(tmp, 'jarvis-origin.git'));
// t3: a daemon-side Python change in a repository shaped like Jarvis: its gates run and fail here, so nothing restarts.
const t3 = path.join(jr, '.claude', 'worktrees', 't3');
git(jr, 'worktree', 'add', '-q', '-b', 'worktree-t3', t3, 'HEAD');
await writeFile(path.join(t3, 'jarvis', 'x.py'), 'X = 1\n'); git(t3, 'add', '-A'); git(t3, 'commit', '-q', '-m', 'x');
// ADR 0097: a clone whose default branch is trunk. Its origin says where to open a pull request for a new branch, as
// GitHub does. t6 opens a pull request (twice), t7 merges, t8 opens one with no gh installed; repo3 is another clone
// working on a branch in its main checkout.
const origin2 = path.join(tmp, 'origin2.git'), repo2 = path.join(tmp, 'repo2'), repo3 = path.join(tmp, 'repo3'), seed = path.join(tmp, 'seed');
execFileSync('git', ['init', '-q', '--bare', '-b', 'trunk', origin2]);
await writeFile(path.join(origin2, 'hooks', 'post-receive'), `#!/bin/sh
while read old new ref; do b=\${ref#refs/heads/}; [ "$b" = trunk ] && continue
echo "Create a pull request for '$b' on GitHub by visiting:"; echo "     https://github.com/example/check/pull/new/$b"; done
`, { mode: 0o755 });
execFileSync('git', ['init', '-q', '-b', 'trunk', seed]); await writeFile(path.join(seed, 'README.md'), '# 二号\n');
git(seed, 'add', '-A'); git(seed, 'commit', '-q', '-m', '初始化仓库'); git(seed, 'push', '-q', origin2, 'trunk');
for (const r of [repo2, repo3]) { execFileSync('git', ['clone', '-q', origin2, r]); await writeFile(path.join(r, '.git', 'info', 'exclude'), '.claude/\n'); }
const tree2 = async (name, edit) => { const at = path.join(repo2, '.claude', 'worktrees', name); git(repo2, 'worktree', 'add', '-q', '-b', `worktree-${name}`, at, 'HEAD'); await edit(at); return at; };
const t6 = await tree2('t6', at => writeFile(path.join(at, 'six.ts'), 'export const six = 6;\n'));
const t7 = await tree2('t7', at => writeFile(path.join(at, 'seven.ts'), 'export const seven = 7;\n'));
const t8 = await tree2('t8', at => writeFile(path.join(at, 'eight.ts'), 'export const eight = 8;\n'));
git(repo3, 'switch', '-q', '-c', 'feature'); await writeFile(path.join(repo3, 'three.ts'), 'export const three = 3;\n'); git(repo3, 'add', '-A'); git(repo3, 'commit', '-q', '-m', '三');
// The stand-in gh: it notes what it was asked and answers with a pull request, or, once it has opened one, with gh's
// own words for a branch whose pull request is already open.
const ghBin = path.join(tmp, 'bin', 'gh'), ghCalls = path.join(tmp, 'gh-calls.jsonl'), marks = path.join(tmp, 'marks.txt'), fakeLog = path.join(tmp, 'fake-claude.log');
await mkdir(path.dirname(ghBin));
await writeFile(ghBin, `#!/usr/bin/env node
const fs = require('fs'), again = fs.existsSync(${JSON.stringify(ghCalls)});
fs.appendFileSync(${JSON.stringify(ghCalls)}, JSON.stringify({ args: process.argv.slice(2), cwd: process.cwd(), prompt: process.env.GH_PROMPT_DISABLED ?? null }) + '\\n');
if (again) { console.error('a pull request for branch "worktree-t6" into branch "trunk" already exists:\\nhttps://github.com/example/check/pull/7'); process.exit(1); }
console.log('https://github.com/example/check/pull/7');
`, { mode: 0o755 });

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
  row('00000000-0000-4000-8000-000000000006', t6, 'worktree-t6', repo2), row('00000000-0000-4000-8000-000000000007', t7, 'worktree-t7', repo2),
  row('00000000-0000-4000-8000-000000000008', t8, 'worktree-t8', repo2), row('00000000-0000-4000-8000-000000000009', repo3, 'feature', repo3, { tree: false }),
] }));
const PLANS = { services: { claude: { status: 'ok', data: { plan: '5X', windows: [{ key: 'five_hour', label: '5 小时', percent: 12, resets_at: '2026-09-29T17:00:00+00:00' }, { key: 'seven_day', label: '7 天 · 总', percent: 100, resets_at: '2026-10-06T12:00:00+00:00' }] } } } };
const daemon = http.createServer((q, r) => { r.writeHead(200, { 'Content-Type': 'application/json' }); r.end(JSON.stringify(q.url === '/inherent/usage' ? PLANS : q.url === '/inherent/agent-marks' ? { marks: {} } : {})); });
await new Promise(r => daemon.listen(0, '127.0.0.1', r));
const port = 18000 + Math.floor(Math.random() * 1000), API = `http://127.0.0.1:${port}`, H = { Authorization: 'Bearer workbench-check', 'Content-Type': 'application/json' };
const electron = path.join(app, 'node_modules', '.bin', 'electron');
// Without --live the host gets nothing of this machine's environment: a home of its own, node and the system tools, and
// who commits. --live keeps the real environment for the real Claude Code's sign-in.
const home = path.join(tmp, 'home');
await mkdir(home);
const own = { PATH: `${path.dirname(process.execPath)}:/usr/bin:/bin`, HOME: home, SHELL: '/bin/sh', USER: os.userInfo().username, LANG: 'en_US.UTF-8', TMPDIR: os.tmpdir(),
  GIT_AUTHOR_NAME: 't', GIT_AUTHOR_EMAIL: 't@t', GIT_COMMITTER_NAME: 't', GIT_COMMITTER_EMAIL: 't@t',
  JARVIS_AGENTS_CLAUDE: path.join(here, 'fake-claude.mjs'), CLAUDE_CONFIG_DIR: path.join(tmp, 'claude-config'), FAKE_CLAUDE_LOG: fakeLog };
const host = spawn(electron, [path.join(app, 'dist-electron', 'agents', 'host.js')], { stdio: ['ignore', 'pipe', 'pipe'],
  env: { ...live ? process.env : own, ELECTRON_RUN_AS_NODE: '1', JARVIS_RUNTIME_ROOT: root, JARVIS_AGENTS_DIR: dir, JARVIS_AGENTS_PORT: String(port),
    JARVIS_INHERENT_BRIDGE_PORT: String(daemon.address().port), JARVIS_AGENTS_GH: ghBin } });
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
const S5 = '00000000-0000-4000-8000-000000000005', S6 = '00000000-0000-4000-8000-000000000006', S7 = '00000000-0000-4000-8000-000000000007';
const S8 = '00000000-0000-4000-8000-000000000008', S9 = '00000000-0000-4000-8000-000000000009';
const S1 = '00000000-0000-4000-8000-000000000001', S2 = '00000000-0000-4000-8000-000000000002', S3 = '00000000-0000-4000-8000-000000000003', S4 = '00000000-0000-4000-8000-000000000004';

// ---------- what each session would land ----------
const d1 = await until('t1 measured', () => rows.get(S1)?.dirty);
check('dirty counts committed, uncommitted and new files against main', d1.n === 3 && d1.ahead === 1 && d1.add >= 3, JSON.stringify(d1));
check('outside Jarvis a worktree branch opens a pull request by default, or merges', d1.into === 'main' && d1.ways.join() === 'pr,merge', JSON.stringify(d1));
const d4 = (await until('hello', () => rows.get(S4)?.dirty));
check('a session on main lands what origin has not got, and only by merging', d4.ahead === 1 && d4.n === 1 && d4.ways.join() === 'merge', JSON.stringify(d4));
const d3 = await until('t3 measured', () => rows.get(S3)?.dirty), d6 = await until('t6 measured', () => rows.get(S6)?.dirty), d9 = await until('repo3 measured', () => rows.get(S9)?.dirty);
check('Jarvis merges by default', d3.ways.join() === 'merge,pr', JSON.stringify(d3));
check('the default branch is the one origin names', d6.into === 'trunk' && d6.n === 1 && d6.ways.join() === 'pr,merge', JSON.stringify(d6));
check('a branch outside its own worktree can only open a pull request', d9.into === 'trunk' && d9.ahead === 1 && d9.ways.join() === 'pr', JSON.stringify(d9));

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
await call(`/sessions/${S1}/land`, { action: 'start', via: 'merge' });
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
await call(`/sessions/${S2}/land`, { action: 'start', via: 'merge' });
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

// ---------- ADR 0097: another repository, with the owner's gates and restart for it ----------
const pwd = '$(pwd -P)', gates = ['test -f README.md', `echo "gate ${pwd}" >> '${marks}'`], restart = `echo "restart ${pwd}" >> '${marks}'`;
await call('/settings', { land: { [repo2]: { gates, restart }, 'relative/repo': { gates: ['true'] }, [repo3]: { via: 'sideways' } } });
const set = (await call('/settings')).settings.land;
check('a repository\'s landing settings keep only what is valid', Object.keys(set).join() === repo2 && set[repo2].gates.join('|') === gates.join('|') && set[repo2].restart === restart, JSON.stringify(set));
const marked = () => readFile(marks, 'utf8').catch(() => '');
// t6: a pull request, its title drafted from the repository's own recent commits (typed with --live, which drafts once).
if (live) await call(`/sessions/${S6}/land`, { action: 'msg', msg: '开一个 PR' });
await call(`/sessions/${S6}/land`, { action: 'start' });
l = await until('t6 at push', () => rows.get(S6)?.land?.s === 'wait' && rows.get(S6).land);
check('the owner\'s gates run in the session\'s folder', l.gates.length === 2 && l.gates.every(g => g.st === 'ok') && (await marked()).includes(`gate ${realpathSync(t6)}`), JSON.stringify(l.gates));
check('a pull request lands into trunk with no merge and no restart', l.via === 'pr' && l.into === 'trunk' && l.steps[3].st === 'skip' && l.steps[4].st === 'skip'
  && l.steps[5].cmd.join(' | ') === 'git push -u origin worktree-t6 | gh pr create --base trunk --head worktree-t6' && git(repo2, 'rev-parse', 'trunk') === git(origin2, 'rev-parse', 'trunk'), JSON.stringify(l.steps));
const drafted = (await readFile(fakeLog, 'utf8').catch(() => '')).split('\n').filter(Boolean).map(e => JSON.parse(e)).find(e => e.ev === 'control' && e.system?.includes('<recent-titles>'));
if (!live) check('with no commit skill, the title is drafted from the repository\'s recent commits', !!drafted?.system.includes('初始化仓库') && git(t6, 'log', '-1', '--format=%s') === l.msg && l.msg.length <= 72, l.msg);
check('the pull request waits for the owner', l.why.includes('开 PR') && l.acts.join() === 'allow,deny' && !existsSync(ghCalls));
await call(`/sessions/${S6}/land`, { action: 'allow' });
l = await until('t6 landed', () => rows.get(S6)?.land?.s === 'done' && rows.get(S6).land);
const asked = JSON.parse((await readFile(ghCalls, 'utf8')).split('\n')[0]);
check('the branch reaches origin and gh opens the pull request against trunk', git(origin2, 'rev-parse', 'worktree-t6') === git(t6, 'rev-parse', 'HEAD')
  && asked.args.slice(0, 8).join(' ') === `pr create --base trunk --head worktree-t6 --title ${l.msg}` && asked.prompt === '1'
  && l.pr === 'https://github.com/example/check/pull/7' && rows.get(S6).pr === l.pr, JSON.stringify(asked));
check('trunk is untouched, nothing restarts, and the worktree stays for the pull request', git(origin2, 'rev-parse', 'trunk') === git(repo2, 'rev-parse', 'trunk')
  && existsSync(t6) && l.steps[6].st === 'skip' && !rows.get(S6).gone && !(await marked()).includes('restart'));
await until('t6 has nothing new', () => rows.get(S6) && !rows.get(S6).dirty);
check('with its pull request open and nothing new, the row has nothing to land', !rows.get(S6).dirty);
// Something new: landing again pushes it to the same pull request.
await writeFile(path.join(t6, 'six-more.ts'), 'export const more = 6;\n');
await call(`/sessions/${S6}/land`, { action: 'msg', msg: '再补一点' });
await call(`/sessions/${S6}/land`, { action: 'start' });
await until('t6 again at push', () => rows.get(S6)?.land?.s === 'wait');
await call(`/sessions/${S6}/land`, { action: 'allow' });
l = await until('t6 landed again', () => rows.get(S6)?.land?.s === 'done' && rows.get(S6).land);
check('landing again updates the branch and finds its pull request open', git(origin2, 'rev-parse', 'worktree-t6') === git(t6, 'rev-parse', 'HEAD')
  && git(t6, 'log', '-1', '--format=%s') === '再补一点' && l.pr === 'https://github.com/example/check/pull/7' && l.steps[5].d.includes('本来就开着'), JSON.stringify(l.steps[5]));
// t7: merging, picked over the default, into trunk; the owner's restart runs in the main checkout.
await call(`/sessions/${S7}/land`, { action: 'msg', msg: '合进主干' });
await call(`/sessions/${S7}/land`, { action: 'start', via: 'merge' });
l = await until('t7 at push', () => rows.get(S7)?.land?.s === 'wait' && rows.get(S7).land);
check('merging fast-forwards trunk and runs the owner\'s restart in the main checkout', l.via === 'merge' && git(repo2, 'rev-parse', 'trunk') === git(t7, 'rev-parse', 'HEAD')
  && l.steps[4].st === 'ok' && l.restart.join() === restart.slice(0, 39) + '…' && (await marked()).includes(`restart ${realpathSync(repo2)}`), JSON.stringify(l.steps[4]));
check('the push names trunk', l.why.includes('trunk') && l.steps[5].cmd.join() === 'git push origin trunk');
await call(`/sessions/${S7}/land`, { action: 'allow' });
l = await until('t7 landed', () => rows.get(S7)?.land?.s === 'done' && rows.get(S7).land);
check('trunk reaches origin and the worktree goes', git(origin2, 'rev-parse', 'trunk') === git(repo2, 'rev-parse', 'trunk') && !existsSync(t7) && rows.get(S7).gone === true);
// t8: no gh here, so the address the remote gave for a pull request.
await rm(ghBin);
await call(`/sessions/${S8}/land`, { action: 'msg', msg: '没有 gh' });
await call(`/sessions/${S8}/land`, { action: 'start' });
l = await until('t8 at push', () => rows.get(S8)?.land?.s === 'wait' && rows.get(S8).land);
check('with no gh, the push step says it will give the link', l.via === 'pr' && l.steps[5].d.includes('没装 gh') && l.steps[5].cmd.join() === 'git push -u origin worktree-t8');
await call(`/sessions/${S8}/land`, { action: 'allow' });
l = await until('t8 landed', () => rows.get(S8)?.land?.s === 'done' && rows.get(S8).land);
check('with no gh, the branch is pushed and the remote\'s link is where to open the pull request', git(origin2, 'rev-parse', 'worktree-t8') === git(t8, 'rev-parse', 'HEAD')
  && l.pr === 'https://github.com/example/check/pull/new/worktree-t8' && !rows.get(S8).pr, JSON.stringify(l));

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

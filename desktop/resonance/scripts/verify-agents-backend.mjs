// Run after npm run build. The agent host's side of Startrail (ADR 0094-0096 and the release audit's gaps) against
// throwaway folders, with stand-ins for everything outside the host: Claude Code (scripts/fake-claude.mjs), the login
// Keychain, Anthropic's API and the daemon; Codex's app-server (scripts/fake-codex.mjs) for one host of its own. Nothing
// here reaches Anthropic or OpenAI or spends anything, and nothing of this Mac's own sessions, Keychain items or Claude
// Code login is read or touched: the hosts get a home, a Claude Code config folder and a PATH of their own.
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { appendFileSync, chmodSync, existsSync, readFileSync, realpathSync, statSync, symlinkSync, utimesSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url)), app = path.join(here, '..');
const tmp = realpathSync(await mkdtemp(path.join(os.tmpdir(), 'jarvis-agents-backend-')));
const HOME = path.join(tmp, 'home'), CONFIG = path.join(HOME, '.claude'), BIN = path.join(tmp, 'bin'), FAKE = path.join(here, 'fake-claude.mjs');
const G = { ...process.env, GIT_AUTHOR_NAME: 't', GIT_AUTHOR_EMAIL: 't@t', GIT_COMMITTER_NAME: 't', GIT_COMMITTER_EMAIL: 't@t' };
const git = (cwd, ...a) => execFileSync('git', ['-C', cwd, ...a], { encoding: 'utf8', env: G }).trim();
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---------- a home with a repository in ~/Projects, a folder outside git, and stand-ins on a PATH of their own ----------
const repo = path.join(HOME, 'Projects', 'app'), plain = path.join(tmp, 'plain'), other = path.join(tmp, 'other');
await mkdir(path.join(repo, 'src'), { recursive: true }); await mkdir(path.join(plain, 'sub'), { recursive: true }); await mkdir(path.join(plain, 'node_modules'), { recursive: true });
await mkdir(other, { recursive: true }); await mkdir(BIN, { recursive: true }); await mkdir(CONFIG, { recursive: true });
await writeFile(path.join(repo, 'README.md'), '# App\n\nA readme.\n');
await writeFile(path.join(repo, 'src', 'a.ts'), 'export const a = 1;\nexport const b = 2;\n');
await writeFile(path.join(repo, 'notes.txt'), 'line one\nline two\n');
await writeFile(path.join(repo, 'index.html'), '<!doctype html><title>x</title>');
await writeFile(path.join(repo, 'song.mp3'), 'ID3');
await writeFile(path.join(repo, 'blob.dat'), Buffer.from([1, 0, 2, 0, 3]));
await writeFile(path.join(repo, '.gitignore'), '.env\n.claude/\nbig.log\n');
await writeFile(path.join(repo, '.worktreeinclude'), '.env\n');
git(path.dirname(repo), 'init', '-q', '-b', 'main', repo);
git(repo, 'add', '-A'); git(repo, 'commit', '-q', '-m', 'init');
await writeFile(path.join(repo, '.env'), 'SECRET=1\n');
await writeFile(path.join(repo, 'big.log'), `${'x'.repeat(99)}\n`.repeat(30000));
await writeFile(path.join(plain, 'x.txt'), 'x'); await writeFile(path.join(plain, 'sub', 'y.md'), '# y'); await writeFile(path.join(plain, 'node_modules', 'z.js'), '');
symlinkSync(process.execPath, path.join(BIN, 'node'));
// A login shell that reads no profile, so nothing of this machine's own setup comes in.
await writeFile(path.join(BIN, 'sh'), '#!/bin/sh\nexec /bin/bash --noprofile --norc "$@"\n'); chmodSync(path.join(BIN, 'sh'), 0o755);
// macOS's security tool: generic passwords kept in a file, every call and what it read on stdin logged.
const SEC = path.join(BIN, 'security.mjs'), SECLOG = path.join(tmp, 'security.log');
await writeFile(SEC, `#!/usr/bin/env node
import { appendFileSync, existsSync, readFileSync, writeFileSync } from 'node:fs';
const file = ${JSON.stringify(path.join(tmp, 'keychain.json'))}, argv = process.argv.slice(2);
const items = existsSync(file) ? JSON.parse(readFileSync(file, 'utf8')) : {}, save = () => writeFileSync(file, JSON.stringify(items));
const flag = (a, f) => a[a.indexOf(f) + 1];
function one(a) {
  const k = flag(a, '-s') + '|' + flag(a, '-a');
  if (a[0] === 'find-generic-password') { if (!(k in items)) return 44; process.stdout.write(items[k] + '\\n'); return 0; }
  if (a[0] === 'add-generic-password') { items[k] = Buffer.from(flag(a, '-X'), 'hex').toString('utf8'); save(); return 0; }
  if (a[0] === 'delete-generic-password') { if (!(k in items)) return 44; delete items[k]; save(); return 0; }
  return 1;
}
let stdin = '';
if (argv[0] === '-i') { stdin = readFileSync(0, 'utf8'); }
appendFileSync(${JSON.stringify(SECLOG)}, JSON.stringify({ argv, stdin }) + '\\n');
if (argv[0] !== '-i') process.exit(one(argv));
for (const line of stdin.split('\\n').filter(Boolean)) { const a = line.match(/"[^"]*"|\\S+/g).map(x => x.replace(/^"|"$/g, '')); const c = one(a); if (c) process.exit(c); }
`); chmodSync(SEC, 0o755);

// ---------- Anthropic's API (only the model list a key is checked with) and the daemon ----------
const GOOD = `sk-ant-api03-stand-in-${'k'.repeat(24)}Q7kZ`, BAD = `sk-ant-api03-refused-${'k'.repeat(24)}`;
const apiCalls = [];
const api = http.createServer((q, r) => {
  apiCalls.push({ url: q.url, key: q.headers['x-api-key'] });
  const ok = q.url.startsWith('/v1/models') && q.headers['x-api-key'] === GOOD;
  r.writeHead(q.url.startsWith('/v1/models') ? ok ? 200 : 401 : 404, { 'Content-Type': 'application/json' }); r.end(ok ? '{"data":[]}' : '{}');
});
const daemon = http.createServer((q, r) => { r.writeHead(200, { 'Content-Type': 'application/json' }); r.end(JSON.stringify(q.url === '/inherent/agent-marks' ? { marks: {} } : {})); });
await new Promise(r => api.listen(0, '127.0.0.1', r)); await new Promise(r => daemon.listen(0, '127.0.0.1', r));
const freePort = () => new Promise(r => { const s = http.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => r(p)); }); });

// ---------- a host: its own runtime root, the event stream kept ----------
const electron = path.join(app, 'node_modules', '.bin', 'electron'), hosts = [];
// `first`: a folder at the front of its PATH (where the stand-in Codex is).
async function startHost(name, root, packaged, first = '') {
  const dir = path.join(root, 'agents'), port = await freePort(), log = path.join(tmp, `${name}-claude.log`), cxlog = path.join(tmp, `${name}-codex.log`);
  await mkdir(path.join(root, 'logs'), { recursive: true });
  await writeFile(path.join(root, 'plugin-access.json'), JSON.stringify({ token: `daemon-${name}` }));
  const env = { PATH: `${first ? `${first}:` : ''}${BIN}:/usr/bin:/bin`, HOME, SHELL: path.join(BIN, 'sh'), USER: os.userInfo().username, LANG: 'en_US.UTF-8', TMPDIR: os.tmpdir(), ELECTRON_RUN_AS_NODE: '1',
    JARVIS_RUNTIME_ROOT: root, JARVIS_AGENTS_DIR: dir, JARVIS_AGENTS_PORT: String(port), JARVIS_INHERENT_BRIDGE_PORT: String(daemon.address().port),
    JARVIS_AGENTS_CLAUDE: FAKE, JARVIS_AGENTS_SECURITY: SEC, CLAUDE_CONFIG_DIR: CONFIG, ANTHROPIC_BASE_URL: `http://127.0.0.1:${api.address().port}`, FAKE_CLAUDE_LOG: log, FAKE_CODEX_LOG: cxlog,
    ...packaged ? { JARVIS_AGENTS_PACKAGED: '1' } : {} };
  const proc = spawn(electron, [path.join(app, 'dist-electron', 'agents', 'host.js')], { stdio: ['ignore', 'pipe', 'pipe'], env });
  const h = { name, root, dir, port, log, proc, out: '', rows: new Map(), events: [], API: `http://127.0.0.1:${port}` };
  proc.stdout.on('data', b => { h.out += b; }); proc.stderr.on('data', b => { h.out += b; });
  hosts.push(h);
  for (let i = 0; i < 100 && !existsSync(path.join(dir, 'host-key')); i++) await sleep(100);
  h.key = readFileSync(path.join(dir, 'host-key'), 'utf8');
  for (let i = 0; i < 100; i++) { if ((await fetch(`${h.API}/health`, { headers: { Authorization: `Bearer ${h.key}` } }).catch(() => null))?.ok) break; await sleep(150); }
  h.stream = await fetch(`${h.API}/events`, { headers: { Authorization: `Bearer ${h.key}` } });
  void (async () => {
    let buf = '';
    try { for await (const chunk of h.stream.body) {
      buf += Buffer.from(chunk).toString('utf8');
      for (let at = buf.indexOf('\n\n'); at >= 0; at = buf.indexOf('\n\n')) {
        const line = buf.slice(0, at); buf = buf.slice(at + 2);
        if (!line.startsWith('data: ')) continue;
        const e = JSON.parse(line.slice(6));
        h.events.push(e);
        if (e.t === 'hello') for (const s of e.sessions) h.rows.set(s.id, s);
        if (e.t === 'sess') h.rows.set(e.s.id, e.s);
        if (e.t === 'gone') h.rows.delete(e.id);
      }
    } } catch { /* the host went away */ }
  })();
  h.call = async (route, body, method = body === undefined ? 'GET' : 'POST', key = h.key) => {
    const r = await fetch(h.API + route, { method, headers: { ...key ? { Authorization: `Bearer ${key}` } : {}, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    return { status: r.status, ...j };
  };
  // What the stand-in Claude Code, and the stand-in Codex, did for this host.
  h.claude = () => existsSync(log) ? readFileSync(log, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l)) : [];
  h.codex = () => existsSync(cxlog) ? readFileSync(cxlog, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l)) : [];
  return h;
}
async function stopHost(h) {
  h.proc.kill(); await new Promise(r => h.proc.exitCode !== null ? r() : h.proc.once('exit', r));
  try { execFileSync('pkill', ['-f', `keeper.js ${h.dir}`]); } catch { /* none left */ }
}
// A keeper's socket outside the agents folder (the folder too deep for one) goes with the check too.
const sockOutside = h => path.join(os.tmpdir(), `jarvis-agents-${process.getuid()}`, `${createHash('sha256').update(h.dir).digest('hex').slice(0, 16)}.sock`);
const done = async () => {
  for (const h of hosts) { h.proc.kill(); try { execFileSync('pkill', ['-f', `keeper.js ${h.dir}`]); } catch { /* none left */ } await rm(sockOutside(h), { force: true }); }
  api.close(); daemon.close();
  if (process.env.KEEP_TMP) console.log(`kept ${tmp}`); else await rm(tmp, { recursive: true, force: true });
};
process.on('uncaughtException', async e => {
  console.error(e);
  for (const h of hosts) console.error(`\n--- ${h.name} ---\n`, h.out.slice(-4000), '\n--- its keeper ---\n', md5(path.join(h.root, 'logs', 'agents-keeper.log'))?.slice(-3000));
  await done(); process.exit(1);
});
process.on('unhandledRejection', e => { throw e; });
const until = async (what, pred, ms = 20000) => {
  const t0 = Date.now();
  for (;;) { const v = await pred(); if (v) return v; if (Date.now() - t0 > ms) throw new Error(`timed out: ${what}`); await sleep(60); }
};
// One message, and its turn to the end: the stand-in finished it and the row says so.
async function turn(h, id, text, extra = {}) {
  const n = h.claude().filter(e => e.ev === 'turn end').length, r = await h.call(`/sessions/${id}/send`, { text, ...extra });
  assert.equal(r.status, 200, JSON.stringify(r));
  await until(`turn "${text}"`, () => h.claude().filter(e => e.ev === 'turn end').length > n && !['work', 'wait', 'pack'].includes(h.rows.get(id)?.st));
  return (await h.call(`/sessions/${id}`)).items;
}
const md5 = f => existsSync(f) ? readFileSync(f, 'utf8') : null;

// ======================= the dev build: Allen's own sign-in =======================
const A = await startHost('dev', path.join(tmp, 'root'), false);

// ---------- ADR 0095: the host's own key ----------
const keyMode = statSync(path.join(A.dir, 'host-key')).mode & 0o777;
check('the host makes a key of its own, readable only by the owner', /^[0-9a-f]{64}$/.test(A.key) && keyMode === 0o600, keyMode.toString(8));
check('no key, or a wrong one, is refused', (await A.call('/health', undefined, 'GET', '')).status === 401 && (await A.call('/health', undefined, 'GET', 'nope')).status === 401);
check('the host key opens it', (await A.call('/health')).ok === true);
check('the daemon token still opens it, for an older companion', (await A.call('/health', undefined, 'GET', 'daemon-dev')).ok === true);

// ---------- ADR 0094 in the dev build: Claude Code's own sign-in, no key kept ----------
const s0 = await A.call('/settings');
check('the dev build runs on this Mac\'s own Claude Code sign-in', s0.auth.packaged === false && s0.auth.mode === 'subscription' && s0.auth.ready === true);
const k0 = await A.call('/settings/key', { key: GOOD }), k1 = await A.call('/settings/key', undefined, 'DELETE');
check('the dev build keeps no key', k0.status === 409 && k1.status === 409 && !apiCalls.length && !existsSync(SECLOG), k0.error);

// ---------- settings: only what the window may set, each checked ----------
const s1 = await A.call('/settings', { notify: { done: false }, editor: 'cursor', terminal: 'ghostty', folders: [plain, 'relative/x', plain], setup: { [repo]: 'true', relative: 'x' }, provider: 'nope', awake: true, junk: 1 });
check('settings keep only valid values', JSON.stringify(s1.settings) === JSON.stringify({ notify: { done: false, wait: true, err: true }, editor: 'cursor', terminal: 'ghostty', folders: [plain], setup: { [repo]: 'true' } }), s1.settings);
const s2 = await A.call('/settings', { editor: null, setup: null });
check('null clears a setting, and the file keeps what is left', s2.settings.editor === undefined && s2.settings.setup === undefined && JSON.parse(readFileSync(path.join(A.dir, 'settings.json'), 'utf8')).settings.terminal === 'ghostty');
check('every window hears a settings change', A.events.some(e => e.t === 'settings' && e.settings.terminal === 'ghostty') && A.events[0]?.t === 'hello' && A.events[0].auth?.ready === true);

// ---------- A8: projects ----------
const p0 = await A.call('/projects');
check('~/Projects repositories and added folders are projects', p0.list.some(p => p.path === repo && p.git) && p0.list.some(p => p.path === plain && p.added), p0.list);
await A.call('/projects', { path: other });
const p1 = await A.call(`/projects?path=${encodeURIComponent(other)}`, undefined, 'DELETE');
check('a folder is added and removed', !p1.projects.includes(other) && (await A.call('/projects', { path: path.join(tmp, 'missing') })).status === 400);

// ---------- menus from the stand-in: models, efforts, commands ----------
const cat = await until('catalog from the stand-in', () => A.events.find(e => e.t === 'catalog' && e.catalog.claude.models.length)?.catalog.claude);
check('models and efforts come from Claude Code itself', cat.models[0][0] === 'fake-sonnet' && cat.efforts.join() === 'low,medium,high,max' && cat.modes.some(m => m[0] === 'bypassPermissions'), cat);
const cm = (await A.call(`/commands?cwd=${encodeURIComponent(repo)}`)).commands;
check('commands: the window\'s own screens first, then Claude Code\'s', cm.find(c => c[0] === '/rewind')?.[2] === 'rewind' && cm.find(c => c[0] === '/resume')?.[2] === 'import' && cm.some(c => c[0] === '/fake-skill'), cm.slice(0, 3));

// ---------- A4: the check-up ----------
const doc = await A.call('/doctor');
check('the check-up names the Claude Code it runs and who it is signed in as', doc.claude.exe === FAKE && doc.claude.version === '9.9.9 (Claude Code)' && doc.claude.own && doc.claude.account?.email === 'owner@example.com', doc.claude);
check('the check-up finds git, no Codex, and the daemon', doc.git.found && doc.codex.found === false && doc.daemon.up === true && doc.path[0] === BIN, doc);

// ---------- @ files, links in answers, the preview ----------
const f0 = (await A.call(`/files?cwd=${encodeURIComponent(repo)}&q=readme`)).files, f1 = (await A.call(`/files?cwd=${encodeURIComponent(plain)}&q=`)).files;
check('@ finds files by the letters typed, in git and out of it', f0[0] === 'README.md' && f1.includes('sub/y.md') && !f1.some(f => f.includes('node_modules')), { f0, f1 });
const rs = (await A.call(`/resolve?cwd=${encodeURIComponent(repo)}&ref=README.md&ref=${encodeURIComponent('src/a.ts:2')}&ref=nope.md&ref=src`)).found;
check('only paths that exist become links, with their line', rs['README.md']?.dir === false && rs['src/a.ts:2']?.line === 2 && rs.src?.dir === true && !rs['nope.md'], rs);
const pk = ref => A.call(`/peek?cwd=${encodeURIComponent(repo)}&ref=${encodeURIComponent(ref)}`);
const [pm, pt, pw, pd, pq, pa, pb, pn] = await Promise.all(['README.md', 'src/a.ts:2', 'index.html', 'src', 'blob.dat', 'song.mp3', 'big.log', 'nope.md'].map(pk));
check('the preview: markdown, text at a line, a page, a folder', pm.kind === 'md' && pt.kind === 'text' && pt.line === 2 && pw.kind === 'web' && pw.url.startsWith('file://') && pd.kind === 'dir' && pd.entries.some(e => e.name === 'a.ts'));
check('the preview: Quick Look for binaries, media, the tail of a big file, 404', pq.kind === 'quicklook' && pa.kind === 'media' && pb.kind === 'text' && pb.cut === true && pb.text.length <= 1 << 20 && pn.status === 404);

// ---------- a session: the stand-in's first turn ----------
const c0 = await A.call('/sessions', { agent: 'claude', cwd: repo, text: 'hello PLAN', model: 'fake-sonnet', effort: 'high', mode: 'default' });
const S = c0.id;
await until('first turn', () => h0done());
function h0done() { return A.claude().some(e => e.ev === 'turn end') && A.rows.get(S)?.st === 'done'; }
const start = A.claude().find(e => e.ev === 'start' && e.args.includes(`--session-id=${S}`));
check('a session starts Claude Code under its own id, with checkpoints, thinking and bypass allowed as a mode', !!start && start.args.includes('--allow-dangerously-skip-permissions') && start.args.join(' ').includes('--thinking-display summarized')
  && start.args.includes('--include-partial-messages') && start.args.join(' ').includes('--permission-mode default'), start?.args);
check('the dev build hands it no key and marks it as the window\'s', start.key === null && start.token === null && start.host === '1', start);
let items = (await A.call(`/sessions/${S}`)).items;
const you0 = items.find(i => i.k === 'you'), it0 = items.find(i => i.k === 'it');
check('what you said and the answer carry the ids fork and rewind name', you0?.text === 'hello PLAN' && /^[0-9a-f-]{36}$/.test(you0.id) && /^[0-9a-f-]{36}$/.test(it0?.id ?? ''), items);
check('B18: its thinking is a step', items.some(i => i.k === 'steps' && i.steps.some(s => s.k === 'think' && s.t === '**Reading the ask**' && s.out.includes('hello PLAN'))));
check('the plan shows as a list', items.some(i => i.k === 'plan' && JSON.stringify(i.todos) === JSON.stringify([['Read the code', 2], ['Change it', 1]])));
await until('B14: the name Claude Code gave it', () => A.rows.get(S)?.title === 'Stand-in: hello PLAN');
check('B14: the row takes the name Claude Code gave the session', A.rows.get(S).unread === true && A.rows.get(S).summary === '好的，做完了：hello PLAN。');

// ---------- B6: what it changed, one file's diff, one file put back ----------
items = await turn(A, S, 'EDIT notes.txt');
check('an edit is a step with its lines', items.some(i => i.k === 'steps' && i.steps.some(s => s.k === 'edit' && s.t === 'notes.txt' && s.add === 1)));
const ch = await A.call(`/sessions/${S}/changes`);
check('the review lists the changed file', ch.files.length === 1 && ch.files[0].path === 'notes.txt' && ch.files[0].st === 'M' && ch.files[0].add === 1, ch);
const df = await A.call(`/sessions/${S}/changes/diff?path=notes.txt`);
check('one file\'s diff', df.add === 1 && df.diff.some(d => d[0] === '+' && d[1] === 'edited by the stand-in'), df);
check('a file outside the repository is refused', (await A.call(`/sessions/${S}/changes/revert`, { path: '../../outside.txt' })).status === 400);
const rv = await A.call(`/sessions/${S}/changes/revert`, { path: 'notes.txt' });
check('putting a file back keeps what it held first', md5(path.join(repo, 'notes.txt')) === 'line one\nline two\n' && md5(rv.kept)?.includes('edited by the stand-in') && rv.kept.startsWith(path.join(A.dir, 'trash')), rv);

// ---------- B13: files back to a point, from Claude Code's checkpoints ----------
items = await turn(A, S, 'EDIT notes.txt');
const you1 = items.filter(i => i.k === 'you').at(-1);
const rw0 = await A.call(`/sessions/${S}/rewind?at=${you1.id}`);
check('rewind first says what would change', rw0.can === true && rw0.kind === 'you' && rw0.files.length === 1 && md5(path.join(repo, 'notes.txt')).includes('edited'), rw0);
const rw1 = await A.call(`/sessions/${S}/rewind`, { at: you1.id });
items = (await A.call(`/sessions/${S}`)).items;
check('rewind puts the files back and says so', rw1.files.length === 1 && md5(path.join(repo, 'notes.txt')) === 'line one\nline two\n' && items.at(-1).k === 'note' && items.at(-1).text.includes('1 个文件'), rw1);
const rwc = A.claude().filter(e => e.subtype === 'rewind_files').map(e => e.request.dry_run);
check('Claude Code was asked, dry run first', JSON.stringify(rwc) === '[true,false]', rwc);
check('an unknown point is a 404', (await A.call(`/sessions/${S}/rewind?at=nope`)).status === 404);

// ---------- B11: a message sent while it works waits, and can be taken back ----------
await A.call(`/sessions/${S}/send`, { text: 'SLOW one' });
await until('working', () => A.rows.get(S)?.st === 'work');
await A.call(`/sessions/${S}/send`, { text: 'second' });
await until('queued', () => A.rows.get(S)?.queue?.includes('second'));
const qc = await A.call(`/sessions/${S}/queue`, { action: 'cancel', text: 'second' });
await until('SLOW one done', () => A.claude().some(e => e.ev === 'turn end' && e.how === 'ok' && A.claude().find(t => t.ev === 'turn' && t.text === 'SLOW one')?.uuid === e.uuid) && A.rows.get(S)?.st === 'done');
await sleep(300);
check('a queued message is taken back before Claude Code takes it', qc.ok === true && !A.rows.get(S).queue && !A.claude().some(e => e.ev === 'turn' && e.text === 'second') && A.claude().some(e => e.subtype === 'cancel_async_message'), qc);
await A.call(`/sessions/${S}/send`, { text: 'SLOW two' });
await until('working', () => A.rows.get(S)?.st === 'work');
await A.call(`/sessions/${S}/send`, { text: 'third' });
await until('third ran', () => A.claude().some(e => e.ev === 'turn end' && A.claude().find(t => t.ev === 'turn' && t.text === 'third')?.uuid === e.uuid) && A.rows.get(S)?.st === 'done');
items = (await A.call(`/sessions/${S}`)).items;
const yous = items.filter(i => i.k === 'you').map(i => i.text);
check('a queued message runs next, shown where it ran', yous.slice(-2).join() === 'SLOW two,third' && !A.rows.get(S).queue && items.at(-1).k === 'it', yous);
check('one already taken cannot be taken back', (await A.call(`/sessions/${S}/queue`, { action: 'cancel', text: 'third' })).status === 409);

// ---------- interrupting ----------
await A.call(`/sessions/${S}/send`, { text: 'SLOW three' });
await until('working', () => A.rows.get(S)?.st === 'work');
await A.call(`/sessions/${S}/interrupt`, {});
await until('interrupted', () => A.rows.get(S)?.st === 'done');
check('an interrupted turn ends as yours, not unread', A.rows.get(S).summary === '你打断了这一轮' && A.rows.get(S).unread === false);

// ---------- B17: background tasks ----------
await turn(A, S, 'TASK');
const task = A.rows.get(S).tasks?.[0];
check('a background task is listed while it runs', task?.kind === 'local_bash' && task.st === 'run' && task.what === 'Wait in the background', A.rows.get(S).tasks);
const st = await A.call(`/sessions/${S}/tasks/${task.id}/stop`, {});
await until('task stopped', () => A.rows.get(S).tasks?.[0]?.st === 'stop');
const to = await A.call(`/sessions/${S}/tasks/${task.id}`);
check('a task is stopped, and what it wrote can be read', st.ok && to.out.includes('waiting in the background') && (await A.call(`/sessions/${S}/tasks/${task.id}/stop`, {})).status === 404, to);

// ---------- B16: a sub-agent's own steps ----------
items = await turn(A, S, 'SUB');
const agent = items.flatMap(i => i.k === 'steps' ? i.steps : []).find(s => s.k === 'agent');
check('a sub-agent\'s thinking, calls and words go under its step', agent?.t === 'Explore · Look around' && agent.sub?.map(s => s.k).join() === 'think,read,say' && agent.sub[1].ok === true, agent);

// ---------- a permission, answered "always" ----------
await A.call(`/sessions/${S}/send`, { text: 'ASK' });
const req = await until('asked', async () => A.rows.get(S)?.st === 'wait' && (await A.call(`/sessions/${S}`)).items.find(i => i.k === 'req' && !i.done)?.req);
check('a command waits for the owner, with "always" offered', req.tool === 'Bash' && req.cmd === 'echo asked' && req.always === '以后都允许' && A.rows.get(S).summary === '想跑 echo asked', req);
await A.call(`/sessions/${S}/answer`, { req: req.id, decision: 'always' });
await until('answered turn', () => A.claude().some(e => e.ev === 'permission') && A.rows.get(S)?.st === 'done');
const perm = A.claude().find(e => e.ev === 'permission');
items = (await A.call(`/sessions/${S}`)).items;
check('"always" goes back with the rule Claude Code suggested', perm.behavior === 'allow' && perm.updatedPermissions?.[0]?.rules?.[0]?.ruleContent === 'echo asked' && items.some(i => i.k === 'req' && i.done === '已允许 · 以后都允许'), perm);
check('an answered request cannot be answered again', (await A.call(`/sessions/${S}/answer`, { req: req.id, decision: 'deny' })).status === 409);

// ---------- the context ring, the model, the effort ----------
const cx = await A.call(`/sessions/${S}/context`);
check('the context comes from Claude Code\'s own count', cx.used === 20000 && cx.max === 200000 && cx.rows[0].n === '系统提示词' && cx.rows.some(r => r.kind === 'free') && A.rows.get(S).ctx === 10, cx);
await A.call(`/sessions/${S}/set`, { key: 'model', value: 'fake-opus' });
await A.call(`/sessions/${S}/send`, { text: '/effort max' });
await until('model and effort set', () => A.rows.get(S)?.model === 'fake-opus' && A.rows.get(S)?.effort === 'max');
check('model and effort go to the running Claude Code', A.claude().some(e => e.subtype === 'set_model' && e.request.model === 'fake-opus') && A.claude().some(e => e.subtype === 'apply_flag_settings' && e.request.settings?.effortLevel === 'max'));

// ---------- B7: pictures, a PDF, a file and a dropped file with a message ----------
const png = `data:image/png;base64,${Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4a40000000049454e44ae426082', 'hex').toString('base64')}`;
await writeFile(path.join(tmp, 'dropped.txt'), 'dropped');
items = await turn(A, S, 'look at these', { files: [{ name: 'a.png', url: png }, { name: 'doc.pdf', url: `data:application/pdf;base64,${Buffer.from('%PDF-1.4').toString('base64')}` },
  { name: 'notes.md', url: `data:text/markdown;base64,${Buffer.from('# notes').toString('base64')}` }, { name: 'dropped.txt', path: path.join(tmp, 'dropped.txt') }] });
const sentTurn = A.claude().filter(e => e.ev === 'turn' && e.text.startsWith('look at these')).at(-1), youF = items.filter(i => i.k === 'you').at(-1);
const kept = path.join(A.dir, 'uploads');
check('B7: a picture goes as an image, a PDF as a document, other files by path', sentTurn.blocks.join() === 'image,document,text' && sentTurn.text.includes('Attached files:') && sentTurn.text.includes(path.join(tmp, 'dropped.txt')) && sentTurn.text.includes(kept), sentTurn);
check('B7: the message shows its files, the picture as a copy the window can open', youF.text === 'look at these' && youF.files.map(f => f.name).join() === 'a.png,doc.pdf,notes.md,dropped.txt' && /^[0-9a-f]{32}\.png$/.test(youF.files[0].img ?? ''), youF);
const img = await fetch(`${A.API}/images/${youF.files[0].img}`, { headers: { Authorization: `Bearer ${A.key}` } });
check('the picture\'s copy is served', img.status === 200 && img.headers.get('content-type') === 'image/png');

// ---------- C2 export, C1 search, B14 a name you give stays ----------
await A.call(`/sessions/${S}/meta`, { title: '我起的名字' });
await turn(A, S, 'one more');
check('B14: a name you gave is never replaced', A.rows.get(S).title === '我起的名字' && A.rows.get(S).named === true);
const ex = await A.call(`/sessions/${S}/export`);
check('C2: the conversation exports as Markdown', ex.name === '我起的名字.md' && ex.text.startsWith('# 我起的名字') && ex.text.includes('## 你') && ex.text.includes('## Claude') && ex.text.includes('- 改 notes.txt'), ex.text.slice(0, 200));
const sr = (await A.call('/search?q=HELLO')).hits;
check('search finds what was said', sr.some(h => h.id === S && typeof h.item === 'number' && h.text.includes('hello PLAN')), sr);

// ---------- B13: forks ----------
const fk0 = await A.call(`/sessions/${S}/fork`, { at: you0.id });
await until('fork row', () => A.rows.get(fk0.id));
const fItems = (await A.call(`/sessions/${fk0.id}`)).items;
check('a fork up to a message keeps the conversation to there', A.rows.get(fk0.id).title === '我起的名字（分叉）' && fItems.filter(i => i.k === 'you').map(i => i.text).join() === 'hello PLAN' && fItems.at(-1).k === 'note', fItems);
check('a fork before the first message needs a message of its own', (await A.call(`/sessions/${S}/fork`, { at: you0.id, before: true })).status === 409);
const fk1 = await A.call(`/sessions/${S}/fork`, { at: you0.id, before: true, text: 'start over' });
await until('fresh fork answered', () => A.claude().some(e => e.ev === 'turn' && e.text === 'start over') && A.rows.get(fk1.id)?.st === 'done');
check('...and with one starts fresh', A.claude().some(e => e.ev === 'start' && e.args.includes(`--session-id=${fk1.id}`)));

// ---------- C3: MCP servers, and their forms ----------
const named = (list, n) => list?.find(v => v.name === n), openReq = async id => A.rows.get(id)?.st === 'wait' && (await A.call(`/sessions/${id}`)).items.find(i => i.k === 'req' && !i.done)?.req;
const starts0 = A.claude().filter(e => e.ev === 'start').length, m0 = await A.call(`/sessions/${fk0.id}/mcp`), shot = A.claude().filter(e => e.ev === 'start').slice(starts0);
await until('the one-shot ends', () => shot[0] && A.claude().some(e => e.ev === 'end' && e.pid === shot[0].pid));
check('C3: an idle session\'s MCP servers come from a Claude Code of its own, which waits for them to connect and ends', named(m0.servers, 'docs')?.st === 'on' && named(m0.servers, 'docs').tools === 2
  && named(m0.servers, 'docs').scope === 'user' && named(m0.servers, 'docs').can.join() === 'off' && named(m0.servers, 'tracker')?.st === 'auth' && named(m0.servers, 'tracker').why.includes('/mcp')
  && named(m0.servers, 'flaky')?.st === 'fail' && named(m0.servers, 'flaky').why.includes('ECONNREFUSED') && shot.length === 1 && !shot[0].args.some(a => a.startsWith('--resume'))
  && A.claude().filter(e => e.pid === shot[0].pid && e.subtype === 'mcp_status').length >= 2, { m0, shot });
const r0 = await A.call(`/sessions/${fk0.id}/mcp`, { name: 'docs', action: 'reconnect' }), off0 = await A.call(`/sessions/${fk0.id}/mcp`, { name: 'docs', action: 'off' });
check('C3: an idle session switches one off, but connects one again only while it runs', r0.status === 409 && off0.ok && named(off0.servers, 'docs')?.st === 'off' && named(off0.servers, 'docs').can.join() === 'on', { r0, off0 });
const m1 = await A.call(`/sessions/${S}/mcp`);
check('C3: switched off, it is off for every session in that folder; a running one can connect one again', named(m1.servers, 'docs')?.st === 'off' && named(m1.servers, 'flaky')?.can.join() === 'off,reconnect', m1);
const rc = await A.call(`/sessions/${S}/mcp`, { name: 'flaky', action: 'reconnect' }), on1 = await A.call(`/sessions/${S}/mcp`, { name: 'docs', action: 'on' });
check('C3: a running session connects one again, and switches one back on and waits for it', named(rc.servers, 'flaky')?.st === 'on' && named(rc.servers, 'flaky').tools === 1 && named(on1.servers, 'docs')?.st === 'on'
  && A.claude().some(e => e.subtype === 'mcp_reconnect' && e.request.serverName === 'flaky') && A.claude().some(e => e.subtype === 'mcp_toggle' && e.request.serverName === 'docs' && e.request.enabled === true), { rc, on1 });
const mx = await Promise.all([{ name: 'tracker', action: 'login' }, { name: 'nope', action: 'off' }, { name: 'docs', action: 'explode' }].map(b => A.call(`/sessions/${S}/mcp`, b)));
check('C3: Claude Code signs in to an MCP server only in its terminal; an unknown server or action is refused', mx[0].status === 409 && mx[0].error.includes('/mcp') && mx[1].status === 404 && mx[2].status === 400, mx);
await A.call(`/sessions/${S}/send`, { text: 'FORM' });
const form = await until('a form', () => openReq(S)), fd = k => form.fields.find(f => f.key === k);
check('C3: an MCP server\'s form comes as its fields', form.tool === 'Form' && form.server === 'docs' && form.why === 'Where should it go?' && form.fields.map(f => `${f.key}:${f.kind}`).join() === 'name:text,count:int,public:bool,color:one,tags:many'
  && fd('name').need && fd('name').min === 2 && fd('count').max === 5 && fd('public').def === false && fd('color').def === 'blue' && fd('color').opts.map(o => o.join('=')).join() === 'red=Red,blue=Blue'
  && fd('tags').max === 2 && A.rows.get(S).summary === 'docs 要你填一张表', form);
const fe = await Promise.all([{ count: 2 }, { name: 'Report', count: 9 }, { name: 'Report', count: 3, tags: ['a', 'b', 'c'] }].map(values => A.call(`/sessions/${S}/answer`, { req: form.id, decision: 'allow', values })));
check('C3: what does not fit the form is refused, and the form stays open', fe[0].status === 400 && fe[0].error === '「Name」要填' && fe[1].status === 400 && fe[2].status === 400 && A.rows.get(S).st === 'wait', fe);
await A.call(`/sessions/${S}/answer`, { req: form.id, decision: 'allow', values: { name: 'Report', count: '3', tags: ['a', 'b'] } });
await until('the form answered', () => A.claude().some(e => e.ev === 'elicitation') && A.rows.get(S)?.st === 'done');
const el0 = A.claude().find(e => e.ev === 'elicitation').response;
items = (await A.call(`/sessions/${S}`)).items;
check('C3: 提供 sends what was filled in, and what the form held to start with', JSON.stringify(el0) === JSON.stringify({ action: 'accept', content: { name: 'Report', count: 3, public: false, color: 'blue', tags: ['a', 'b'] } })
  && items.some(i => i.k === 'req' && i.req.id === form.id && i.done === '已提供'), el0);
// One more of each: the form not given, cancelled, the page opened, and a command's request cancelled.
async function answered(text, decision, what = 'elicitation') {
  const n = A.claude().filter(e => e.ev === what).length;
  await A.call(`/sessions/${S}/send`, { text });
  const r = await until(`${text} asks`, () => openReq(S)), summary = A.rows.get(S).summary;
  await A.call(`/sessions/${S}/answer`, { req: r.id, decision });
  await until(`${text} answered`, () => A.claude().filter(e => e.ev === what).length > n && A.rows.get(S)?.st === 'done');
  return { r, summary, got: A.claude().filter(e => e.ev === what).at(-1), done: (await A.call(`/sessions/${S}`)).items.find(i => i.k === 'req' && i.req.id === r.id)?.done };
}
const fn = await answered('FORM', 'deny'), fc = await answered('FORM', 'cancel'), fl = await answered('LINK', 'allow'), pc = await answered('ASK', 'cancel', 'permission');
check('C3: 不提供，继续 and 取消 go back as decline and cancel', JSON.stringify(fn.got.response) === '{"action":"decline"}' && fn.done === '不提供，继续' && JSON.stringify(fc.got.response) === '{"action":"cancel"}' && fc.done === '取消了', { fn, fc });
check('C3: a page an MCP server wants opened, and yes to it', fl.r.tool === 'Form' && fl.r.url === 'https://docs.example.com/login' && !fl.r.fields.length && fl.summary === 'docs 要你打开一个网页'
  && JSON.stringify(fl.got.response) === '{"action":"accept"}' && fl.done === '同意打开网页', fl);
check('C3: cancel on any other request is a no', pc.got.behavior === 'deny' && pc.done === '拒绝了', pc);

// ---------- C7: a question on the side ----------
// A request that goes away before its answer, as a window closed mid-question: the error fetch ends with.
const dropped = (h, id, text) => fetch(`${h.API}/sessions/${id}/side`, { method: 'POST', headers: { Authorization: `Bearer ${h.key}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ text }), signal: AbortSignal.timeout(500) }).then(() => 'answered', e => e.name);
await A.call(`/sessions/${S}/send`, { text: 'SLOW side SLOW' });
await until('working', () => A.rows.get(S)?.st === 'work');
const sd0 = await A.call(`/sessions/${S}/side`, { text: 'what now' }), sdSt = A.rows.get(S)?.st, sdGone = await dropped(A, S, 'SLOW why');
await until('the side question dropped', () => A.claude().some(e => e.ev === 'side cancelled'));
await until('SLOW side SLOW done', () => A.claude().some(e => e.ev === 'turn end' && e.how === 'ok' && A.claude().find(t => t.ev === 'turn' && t.text === 'SLOW side SLOW')?.uuid === e.uuid) && A.rows.get(S)?.st === 'done');
items = (await A.call(`/sessions/${S}`)).items;
const sq0 = A.claude().find(e => e.ev === 'side');
check('C7: a question on the side is answered while the turn goes on, which ends as it would, and it stays out of the conversation', sd0.text === '侧答：what now' && sdSt === 'work'
  && sq0.question === 'what now' && sq0.during && sq0.history === null && !JSON.stringify(items).includes('what now') && !JSON.stringify(items).includes('侧答'), { sd0, sdSt, sq0 });
check('C7: a question whose request goes away is dropped', sdGone === 'TimeoutError' && A.claude().filter(e => e.ev === 'side').length === 1, sdGone);
const starts1 = A.claude().filter(e => e.ev === 'start').length;
const sd1 = await A.call(`/sessions/${fk0.id}/side`, { text: 'and then', history: [['what now', '侧答：what now'], 'junk', ['x']] }), shot1 = A.claude().filter(e => e.ev === 'start').slice(starts1);
await until('the side one-shot ends', () => shot1[0] && A.claude().some(e => e.ev === 'end' && e.pid === shot1[0].pid));
const sq1 = A.claude().filter(e => e.ev === 'side').at(-1);
check('C7: an idle session is asked through a Claude Code of its own that reads the conversation back, with the side talk so far', sd1.text === '侧答：and then' && shot1.length === 1
  && shot1[0].args.includes(`--resume=${fk0.id}`) && sq1.during === false && JSON.stringify(sq1.history) === JSON.stringify([{ question: 'what now', response: '侧答：what now' }]), { sd1, shot1, sq1 });
check('C7: a question has to say something', (await A.call(`/sessions/${S}/side`, { text: '  ' })).status === 400 && (await A.call(`/sessions/${S}/side`, {})).status === 400);

// ---------- C5: other folders ----------
const dr = await A.call(`/sessions/${S}/dirs`, { dirs: [plain, 'relative', path.join(tmp, 'missing')] });
await turn(A, S, 'with more folders');
const again = A.claude().filter(e => e.ev === 'start' && e.args.includes(`--resume=${S}`)).at(-1);
check('C5: extra folders restart it with --add-dir', JSON.stringify(dr.dirs) === JSON.stringify([plain]) && again.args.join(' ').includes(`--add-dir ${plain}`), again?.args);

// ---------- the terminal and back ----------
const rel = await A.call(`/sessions/${S}/release`, {});
check('handing it to the terminal gives the command', rel.cmd === `claude --resume ${S}` && (await A.call(`/sessions/${S}/send`, { text: 'x' })).status === 409);
await A.call(`/sessions/${S}/takeback`, {});
check('taking it back reads what happened there', A.rows.get(S).term === false);

// ---------- B12, ADR 0096: sessions started outside the window ----------
// A transcript Claude Code wrote for a session started in a terminal, last changed at `when`.
async function outside(id, when, said) {
  const dirOf = path.join(CONFIG, 'projects', repo.replace(/[^a-zA-Z0-9]/g, '-')), f = path.join(dirOf, `${id}.jsonl`), u1 = crypto.randomUUID(), u2 = crypto.randomUUID();
  const base = { isSidechain: false, userType: 'external', cwd: repo, sessionId: id, version: '9.9.9', gitBranch: 'main', timestamp: new Date(when).toISOString() };
  const lines = [{ ...base, parentUuid: null, type: 'user', message: { role: 'user', content: said }, uuid: u1 },
    { ...base, parentUuid: u1, type: 'assistant', message: { id: 'msg_x', type: 'message', role: 'assistant', model: 'fake-sonnet', content: [{ type: 'text', text: 'Answered in the terminal. More here.' }] }, uuid: u2 }];
  await mkdir(dirOf, { recursive: true });
  await writeFile(f, `${lines.map(l => JSON.stringify(l)).join('\n')}\n`);
  utimesSync(f, new Date(when), new Date(when));
}
const OLD = crypto.randomUUID(), NEW = crypto.randomUUID();
await outside(OLD, Date.now() - 10 * 60e3, 'from the terminal'); await outside(NEW, Date.now(), 'still open there');
const im = (await A.call(`/import?cwd=${encodeURIComponent(repo)}`)).sessions;
check('the window lists sessions it does not have, a recent one marked', im.find(o => o.id === OLD)?.title === 'from the terminal' && im.find(o => o.id === NEW)?.recent === true && !im.some(o => o.id === S || o.id === fk0.id), im);
const i0 = await A.call('/import', { agent: 'claude', id: NEW, cwd: repo });
check('a recent one takes a second, sure press', i0.status === 409 && i0.need === 'force', i0);
check('...and then comes in', (await A.call('/import', { agent: 'claude', id: NEW, cwd: repo, force: true })).id === NEW);
const i1 = await A.call('/import', { agent: 'claude', id: OLD });
await until('imported row', () => A.rows.get(OLD));
const iItems = (await A.call(`/sessions/${OLD}`)).items;
check('taken in under its own id, read back from its transcript', i1.id === OLD && A.rows.get(OLD).title === 'from the terminal' && A.rows.get(OLD).summary === 'Answered in the terminal.' && iItems[0].text === 'from the terminal', iItems);
check('the same session cannot come in twice', (await A.call('/import', { agent: 'claude', id: OLD })).status === 409);
await turn(A, OLD, 'go on here');
check('it goes on under the same id', A.claude().some(e => e.ev === 'start' && e.args.includes(`--resume=${OLD}`)));

// ---------- worktrees: a base, .worktreeinclude, the setup script, and deleting one with work in it ----------
await A.call('/settings', { setup: { [repo]: 'printf "key=%s\\n" "${ANTHROPIC_API_KEY:-none}" > setup-ran.txt; pwd >> setup-ran.txt; cat .env >> setup-ran.txt' } });
check('a base that is not there is refused', (await A.call('/sessions', { agent: 'claude', cwd: repo, text: 'x', tree: true, base: 'no-such-branch' })).status === 400);
const w = await A.call('/sessions', { agent: 'claude', cwd: repo, text: 'tree work', tree: true, base: 'main' });
await until('tree turn', () => A.claude().some(e => e.ev === 'turn' && e.text === 'tree work') && A.rows.get(w.id)?.st === 'done');
const W = A.rows.get(w.id), ran = md5(path.join(W.cwd, 'setup-ran.txt')) ?? '';
check('a worktree starts from its base in .claude/worktrees', W.tree && W.base === 'main' && W.cwd.startsWith(path.join(repo, '.claude', 'worktrees')) && W.branch.startsWith('worktree-tree-work'), W);
check('ignored files .worktreeinclude names are copied in, and the setup script runs there first', ran.split('\n').join('|') === `key=none|${W.cwd}|SECRET=1|`, ran);
check('the setup script is noted in the conversation', (await A.call(`/sessions/${w.id}`)).items.some(i => i.k === 'note' && i.text === 'worktree 准备好了'));
check('branches list the repository\'s, the current one first', (await A.call(`/branches?cwd=${encodeURIComponent(repo)}`)).current === 'main');
await writeFile(path.join(W.cwd, 'committed.txt'), 'c'); git(W.cwd, 'add', 'committed.txt'); git(W.cwd, 'commit', '-q', '-m', 'work on the branch');
await writeFile(path.join(W.cwd, 'loose.txt'), 'not committed');
const d0 = await A.call(`/sessions/${w.id}`, undefined, 'DELETE');
check('a worktree with work nothing else has asks before it goes', d0.status === 409 && d0.need === 'force' && existsSync(W.cwd), d0);
const d1 = await A.call(`/sessions/${w.id}?force=1`, undefined, 'DELETE');
check('deleting it anyway keeps everything in it under refs/startrail/trash first', d1.ok && d1.kept.startsWith('refs/startrail/trash/') && !existsSync(W.cwd)
  && git(repo, 'show', `${d1.kept}:loose.txt`) === 'not committed' && git(repo, 'log', '-1', '--format=%s', `${d1.kept}^`) === 'work on the branch', d1);

// ---------- deleting a session deletes its transcript ----------
const tr = path.join(CONFIG, 'projects', repo.replace(/[^a-zA-Z0-9]/g, '-'), `${fk1.id}.jsonl`);
const had = existsSync(tr), dl = await A.call(`/sessions/${fk1.id}`, undefined, 'DELETE');
await until('gone from the list', () => !A.rows.has(fk1.id));
check('deleting a session deletes its transcript', had && dl.ok && !existsSync(tr));

// ---------- a restart in the middle of things: the list it left, a turn it was running, and B23 ----------
// OLD is in a slow turn when the host is stopped; the keeper keeps its child for the new host to take back.
await A.call(`/sessions/${OLD}/send`, { text: 'SLOW across the restart' });
await until('the slow turn', () => A.rows.get(OLD)?.st === 'work');
const listOf = h => [...h.rows.values()].map(r => [r.id, r.title, !!r.named, r.archived].join(' ')).sort().join('\n'), left = listOf(A);
A.proc.kill(); await new Promise(r => A.proc.exitCode !== null ? r() : A.proc.once('exit', r));
const A2 = await startHost('dev-again', A.root, false);
await until('the list', () => A2.events[0]?.t === 'hello');
check('a host stopped in the middle of things comes back to the list it left', listOf(A2) === left, { left, back: listOf(A2) });
// B23: S has no child (it went to the terminal and came back), so nothing reads it until it is opened, and a line its
// transcript gains after the new host started is there when it is.
const trS = path.join(CONFIG, 'projects', A2.rows.get(S).cwd.replace(/[^a-zA-Z0-9]/g, '-'), `${S}.jsonl`);
const lastS = readFileSync(trS, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l)).filter(e => e.uuid && !e.isSidechain).at(-1).uuid, u3 = crypto.randomUUID();
const later = { isSidechain: false, userType: 'external', cwd: A2.rows.get(S).cwd, sessionId: S, version: '9.9.9', gitBranch: 'main', timestamp: new Date().toISOString() };
appendFileSync(trS, `${JSON.stringify({ ...later, parentUuid: lastS, type: 'user', message: { role: 'user', content: 'said after the restart' }, uuid: u3 })}\n${
  JSON.stringify({ ...later, parentUuid: u3, type: 'assistant', message: { id: 'msg_y', type: 'message', role: 'assistant', model: 'fake-sonnet', content: [{ type: 'text', text: 'Heard after the restart.' }] }, uuid: crypto.randomUUID() })}\n`);
const sLater = (await A2.call(`/sessions/${S}`)).items;
check('B23: a conversation is read when it is opened, not when the host starts', sLater.at(-1)?.k === 'it' && sLater.at(-1).text === 'Heard after the restart.', sLater.slice(-2));
const sr2 = (await A2.call('/search?q=hello%20PLAN')).hits;
check('B23: search reads the conversations nobody has opened yet', sr2.some(h => h.id === fk0.id && typeof h.item === 'number'), sr2);
await until('the slow turn ends under the new host', () => A2.rows.get(OLD)?.st === 'done');
const oLater = (await A2.call(`/sessions/${OLD}`)).items;
check('a turn running through a restart is taken back and ends here, in the same child', oLater.filter(i => i.k === 'it').at(-1)?.text.startsWith('好的，做完了：SLOW across the restart')
  && !A2.claude().some(e => e.ev === 'start' && e.sid === OLD), oLater.slice(-2));
await stopHost(A2);

// ======================= Codex, through a stand-in app-server =======================
const CXBIN = path.join(tmp, 'codex-bin');
await mkdir(CXBIN, { recursive: true });
await writeFile(path.join(CXBIN, 'codex'), `#!/bin/sh\nexec node "${path.join(here, 'fake-codex.mjs')}" "$@"\n`); chmodSync(path.join(CXBIN, 'codex'), 0o755);
const X = await startHost('codex', path.join(tmp, 'root-codex'), false, CXBIN);
const XS = (await X.call('/sessions', { agent: 'codex', cwd: repo, text: 'hello codex' })).id;
await until('the Codex turn', () => X.codex().some(e => e.ev === 'turn end') && X.rows.get(XS)?.st === 'done');
const xm0 = await X.call(`/sessions/${XS}/mcp`);
check('C3: Codex\'s MCP servers, as the session\'s thread sees them', named(xm0.servers, 'docs')?.st === 'on' && named(xm0.servers, 'docs').tools === 2 && named(xm0.servers, 'docs').can.join() === 'reconnect'
  && named(xm0.servers, 'off')?.st === 'off' && named(xm0.servers, 'broken')?.st === 'fail' && named(xm0.servers, 'broken').why.includes('No such file')
  && named(xm0.servers, 'remote')?.st === 'auth' && named(xm0.servers, 'remote').can.join() === 'reconnect,login' && X.codex().some(e => e.method === 'mcpServerStatus/list' && e.params.threadId === XS), xm0);
const xl = await X.call(`/sessions/${XS}/mcp`, { name: 'remote', action: 'login' }), xev = await until('the MCP sign-in', () => X.events.find(e => e.t === 'mcp'));
const xr = await X.call(`/sessions/${XS}/mcp`, { name: 'docs', action: 'reconnect' }), xx = await Promise.all([{ name: 'docs', action: 'off' }, { name: 'docs', action: 'login' }].map(b => X.call(`/sessions/${XS}/mcp`, b)));
check('C3: Codex signs in to an MCP server over HTTP through a page and says when that is done; it reads its config again; it switches none on or off here', xl.url === 'https://remote.example.com/authorize?client_id=fake'
  && xev.agent === 'codex' && xev.name === 'remote' && xev.ok === true && xev.id === XS && xr.ok && named(xr.servers, 'remote')?.st === 'on' && X.codex().some(e => e.method === 'config/mcpServer/reload')
  && xx[0].status === 409 && xx[1].status === 409, { xl, xev, xr, xx });
await X.call(`/sessions/${XS}/stop`, {});
const xm1 = await X.call(`/sessions/${XS}/mcp`);
check('C3: ...and, with no thread of the session\'s loaded, as its config does', named(xm1.servers, 'docs')?.st === 'on' && named(xm1.servers, 'off')?.st === 'off' && named(xm1.servers, 'broken')?.st === 'fail'
  && named(xm1.servers, 'remote')?.st === 'on' && X.codex().filter(e => e.method === 'mcpServerStatus/list').at(-1).params.threadId === undefined, xm1);
async function xAnswered(text, body) {
  const n = X.codex().filter(e => e.ev === 'elicitation').length;
  await X.call(`/sessions/${XS}/send`, { text });
  const r = body && await until(`${text} asks`, async () => X.rows.get(XS)?.st === 'wait' && (await X.call(`/sessions/${XS}`)).items.find(i => i.k === 'req' && !i.done)?.req);
  const bad = body?.values ? await X.call(`/sessions/${XS}/answer`, { req: r.id, decision: 'allow', values: {} }) : null;
  if (body) await X.call(`/sessions/${XS}/answer`, { req: r.id, ...body });
  await until(`${text} answered`, () => X.codex().filter(e => e.ev === 'elicitation').length > n && X.rows.get(XS)?.st === 'done');
  const items = (await X.call(`/sessions/${XS}`)).items;
  return { r, bad, got: X.codex().filter(e => e.ev === 'elicitation').at(-1).response, done: r && items.find(i => i.k === 'req' && i.req.id === r.id)?.done, items };
}
const xf = await xAnswered('FORM', { decision: 'allow', values: { name: 'Report' } }), xd = await xAnswered('FORM', { decision: 'deny' }), xk = await xAnswered('FORM', { decision: 'cancel' });
check('C3: a Codex MCP form comes as its fields, and what does not fit it is refused', xf.r.tool === 'Form' && xf.r.server === 'docs' && xf.r.fields.map(f => `${f.key}:${f.kind}`).join() === 'name:text,count:int' && xf.bad.status === 400, xf);
check('C3: 提供, 不提供，继续 and 取消 go back to Codex as it takes them', JSON.stringify(xf.got) === '{"action":"accept","content":{"name":"Report","count":1},"_meta":null}' && xf.done === '已提供'
  && JSON.stringify(xd.got) === '{"action":"decline","content":null,"_meta":null}' && xd.done === '不提供，继续' && JSON.stringify(xk.got) === '{"action":"cancel","content":null,"_meta":null}' && xk.done === '取消了', { xf, xd, xk });
const xu = await xAnswered('LINK', { decision: 'allow' }), xv = await xAnswered('VERIFY');
check('C3: a page to open from Codex, and an identity check this window cannot do, refused with a note', xu.r.url === 'https://docs.example.com/login' && JSON.stringify(xu.got) === '{"action":"accept","content":null,"_meta":null}'
  && xv.got.action === 'decline' && xv.items.some(i => i.k === 'note' && i.text === 'docs 要验证你的身份，这个窗口还做不了，先拒绝了'), { xu, xv: xv.got });
await X.call(`/sessions/${XS}/send`, { text: 'SLOW' });
await until('Codex working', () => X.rows.get(XS)?.st === 'work');
const xs0 = await X.call(`/sessions/${XS}/side`, { text: 'why so slow', history: [['q1', 'a1']] }), xsSt = X.rows.get(XS)?.st, xsGone = await dropped(X, XS, 'SLOW');
await until('the Codex turn beside the side questions', () => X.rows.get(XS)?.st === 'done');
const xforks = X.codex().filter(e => e.method === 'thread/fork'), xturns = X.codex().filter(e => e.method === 'turn/start' && e.params.threadId !== XS), xitems = (await X.call(`/sessions/${XS}`)).items;
check('C7: Codex answers on the side from an ephemeral, read-only fork of the thread, while its own turn goes on to its end', xs0.text === '侧答：Earlier in this side conversation:\n\nQ: q1\nA: a1\n\nNow: why so slow'
  && xsSt === 'work' && xforks.length === 2 && xforks.every(e => e.params.threadId === XS && e.params.ephemeral === true && e.params.sandbox === 'read-only' && e.params.approvalPolicy === 'never'
  && e.params.developerInstructions.includes('side conversation')) && xturns.length === 2 && xturns.every(e => e.params.sandboxPolicy.type === 'readOnly' && e.params.approvalPolicy === 'never')
  && xitems.at(-1).k === 'it' && xitems.at(-1).text === '好的，做完了：SLOW。' && !JSON.stringify(xitems).includes('侧答'), { xs0, xsSt, xforks, xturns, last: xitems.at(-1) });
const [xf1, xf2] = xturns.map(e => e.params.threadId), xun = X.codex().filter(e => e.method === 'thread/unsubscribe').map(e => e.params.threadId);
check('C7: each fork is let go once it answers, and one whose request went away is stopped first', xsGone === 'TimeoutError' && xun.includes(xf1) && xun.includes(xf2)
  && X.codex().some(e => e.method === 'turn/interrupt' && e.params.threadId === xf2) && !X.codex().some(e => e.method === 'turn/interrupt' && e.params.threadId === xf1), { xsGone, xun });
const XN = (await X.call('/sessions', { agent: 'codex', cwd: repo, text: 'SLOW first' })).id;
await until('the first Codex turn working', () => X.rows.get(XN)?.st === 'work');
const xn = await X.call(`/sessions/${XN}/side`, { text: 'already?' });
check('C7: before Codex has kept a turn of the conversation there is nothing to fork yet', xn.status === 409 && xn.error.includes('还没存下来'), xn);
await stopHost(X);

// ======================= the installed app: the owner's own key (ADR 0094) =======================
// Its runtime root is deep enough that <agents>/keeper.sock cannot be a socket (104 bytes on macOS, 108 here).
const deep = path.join(tmp, 'the-installed-apps-runtime-root-whose-path-is-too-long-for-a-socket');
let B = await startHost('packaged', deep, true);
const b0 = await B.call('/settings');
check('the installed app starts with no Claude sign-in and says what is missing', b0.auth.packaged && b0.auth.mode === 'key' && b0.auth.ready === false && b0.auth.why === '先填一个 Anthropic API key', b0.auth);
const bc = (await B.call(`/commands?cwd=${encodeURIComponent(repo)}`)).commands;
const bs = await B.call('/sessions', { agent: 'claude', cwd: repo, text: 'hi' });
check('without it no Claude session starts', bs.status === 409 && bs.need === 'auth' && bs.error === '先填一个 Anthropic API key', bs);
check('...and no Claude Code runs at all, not even for the menus', bc.every(c => c[2]) && !B.claude().some(e => e.ev === 'start') && !(await B.call('/doctor')).claude.account);
check('something that is not a key is refused before anything is asked', (await B.call('/settings/key', { key: 'short' })).status === 400 && !apiCalls.length);
const bad = await B.call('/settings/key', { key: BAD });
check('a key Anthropic refuses is not kept', bad.status === 400 && bad.error === 'Anthropic 说这个 key 不对' && apiCalls.at(-1).key === BAD && !existsSync(path.join(tmp, 'keychain.json')), bad);
const good = await B.call('/settings/key', { key: GOOD });
const account = realpathSync(B.dir), stored = JSON.parse(readFileSync(path.join(tmp, 'keychain.json'), 'utf8'));
check('a key that lists models is kept in the Keychain, in an item of its own', good.verified === true && good.auth.ready && good.auth.hint === '…Q7kZ' && JSON.parse(stored[`Jarvis|${account}`]).ANTHROPIC_API_KEY === GOOD, good);
const secLog = readFileSync(SECLOG, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l));
check('the key never shows in a process\'s arguments: hex on security\'s stdin', !secLog.some(c => c.argv.join(' ').includes(GOOD) || c.argv.join(' ').includes(Buffer.from(GOOD).toString('hex')))
  && secLog.some(c => c.argv[0] === '-i' && c.stdin.includes(Buffer.from(JSON.stringify({ ANTHROPIC_API_KEY: GOOD })).toString('hex')) && c.stdin.includes(`-a "${account}"`)), secLog.map(c => c.argv));
await until('menus once signed in', () => B.events.find(e => e.t === 'catalog' && e.catalog.claude.models.length));
const bs1 = await B.call('/sessions', { agent: 'claude', cwd: repo, text: 'on my key' });
await until('packaged turn', () => B.claude().some(e => e.ev === 'turn end') && B.rows.get(bs1.id)?.st === 'done');
const bStarts = B.claude().filter(e => e.ev === 'start');
check('every Claude Code the host starts for Startrail gets the key', bStarts.length >= 2 && bStarts.every(e => e.key === '…Q7kZ' && e.token === null && e.host === '1'), bStarts);
const sock = sockOutside(B), priv = path.dirname(sock);
check('an agents folder too deep for a socket gets its keeper in a folder only the owner can enter', Buffer.byteLength(path.join(B.dir, 'keeper.sock')) >= 108
  && !existsSync(path.join(B.dir, 'keeper.sock')) && statSync(sock).isSocket() && (statSync(priv).mode & 0o777) === 0o700
  && execFileSync('pgrep', ['-f', `keeper.js ${B.dir} ${sock}`]).toString().trim() !== '');
// The owner's own terminal claude keeps its own sign-in: the host's environment, which terminals and setup scripts copy, has no key.
await B.call('/settings', { setup: { [repo]: 'printf "key=%s\\n" "${ANTHROPIC_API_KEY:-none}" > setup-ran.txt' } });
const bw = await B.call('/sessions', { agent: 'claude', cwd: repo, text: 'tree on my key', tree: true });
await until('packaged tree turn', () => B.claude().some(e => e.ev === 'turn' && e.text === 'tree on my key') && B.rows.get(bw.id)?.st === 'done');
check('a setup script, like a terminal, runs without the key', md5(path.join(B.rows.get(bw.id).cwd, 'setup-ran.txt')) === 'key=none\n');
const term = await B.call(`/term/${bs1.id}`, { cols: 80, rows: 12 });
if (term.ok) {
  const out = []; const ts = await fetch(`${B.API}/term/${bs1.id}/stream`, { headers: { Authorization: `Bearer ${B.key}` } });
  void (async () => { try { for await (const c of ts.body) out.push(Buffer.from(c).toString('utf8')); } catch { /* closed */ } })();
  await B.call(`/term/${bs1.id}/input`, { data: 'echo "k=${ANTHROPIC_API_KEY:-none}${ANTHROPIC_API_KEY:+set}"\r' });
  await until('shell echo', () => out.join('').includes('k=none'), 15000);
  check('the terminal the window opens has no key', !out.join('').includes('Q7kZ'));
  await B.call(`/term/${bs1.id}`, undefined, 'DELETE');
} else console.log(`SKIP the terminal (${term.error ?? term.status}): node-pty is not built for this platform`);
await stopHost(B);
B = await startHost('packaged', deep, true);
const b1 = await B.call('/settings');
check('the key is read back from the Keychain when the host starts', b1.auth.ready && b1.auth.hint === '…Q7kZ', b1.auth);
await B.call('/settings', { provider: 'bedrock' });
check('Bedrock needs its region', (await B.call('/settings')).auth.why === '选了 Amazon Bedrock，还没填区域');
await B.call('/settings', { bedrock: { region: 'us-east-1', profile: 'work' } });
const br = await B.call('/sessions', { agent: 'claude', cwd: repo, text: 'on bedrock' });
await until('bedrock turn', () => B.claude().some(e => e.ev === 'turn' && e.text === 'on bedrock') && B.rows.get(br.id)?.st === 'done');
const brStart = B.claude().filter(e => e.ev === 'start').at(-1);
check('with Bedrock chosen it runs on the owner\'s AWS settings, not the key', brStart.bedrock === '1' && brStart.region === 'us-east-1' && brStart.key === null, brStart);
await B.call('/settings', { provider: 'anthropic' });
const fg = await B.call('/settings/key', undefined, 'DELETE');
check('forgetting the key deletes the Keychain item', fg.auth.ready === false && !(`Jarvis|${account}` in JSON.parse(readFileSync(path.join(tmp, 'keychain.json'), 'utf8'))));
check('...and Claude sessions stop starting', (await B.call('/sessions', { agent: 'claude', cwd: repo, text: 'no key' })).need === 'auth');
check('nothing asked Anthropic for anything but the model list', apiCalls.every(c => c.url.startsWith('/v1/models')), apiCalls);

console.log(`\n${checks.length} checks passed`);
await done();

// Run after npm run build. Startrail's projects (threads that share instructions, memory and a folder) against throwaway
// folders, with stand-ins for everything outside the host: Claude Code (scripts/fake-claude.mjs), Codex's app-server
// (scripts/fake-codex.mjs) and the daemon. Nothing here reaches Anthropic or OpenAI or spends anything, and nothing of
// this Mac's own sessions or Claude Code login is read or touched: the hosts get a home, a Claude Code config folder and
// a PATH of their own.
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { chmodSync, existsSync, readFileSync, realpathSync, statSync, symlinkSync, writeFileSync } from 'node:fs';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url)), app = path.join(here, '..');
const tmp = realpathSync(await mkdtemp(path.join(os.tmpdir(), 'jarvis-agents-projects-')));
const HOME = path.join(tmp, 'home'), CONFIG = path.join(HOME, '.claude'), BIN = path.join(tmp, 'bin'), CXBIN = path.join(tmp, 'codex-bin'), FAKE = path.join(here, 'fake-claude.mjs');
const folder = path.join(HOME, 'atlas'), work = path.join(tmp, 'work');
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const DAY = 864e5;

// ---------- the host's own functions, for the rules that need no host: where a thread stands, what a project says ----------
process.env.JARVIS_AGENTS_DIR = path.join(tmp, 'unit');
const { bucket, projPrompt } = await import(path.join(app, 'dist-electron', 'agents', 'host.js'));
const row = o => ({ st: 'done', archived: false, updated: Date.now(), ...o });
const table = [
  [{ archived: true, st: 'work' }, 'done'], [{ st: 'wait' }, 'wait'], [{ st: 'err' }, 'wait'], [{ st: 'work' }, 'work'], [{ st: 'pack' }, 'work'],
  [{ st: 'work', pr: 'u' }, 'work'], [{ st: 'wait', land: { s: 'run' } }, 'wait'], [{ land: { s: 'run' }, pr: 'u' }, 'landing'], [{ land: { s: 'fail' } }, 'landing'],
  [{ land: { s: 'done' }, pr: 'u' }, 'review'], [{ pr: 'u', updated: Date.now() - 30 * DAY }, 'review'], [{ updated: Date.now() - 8 * DAY }, 'done'],
  [{ updated: Date.now() - 6 * DAY }, 'idle'], [{}, 'idle'],
];
const wrong = table.filter(([o, want]) => bucket(row(o)) !== want).map(([o]) => JSON.stringify(o));
check('a thread\'s bucket: the first rule that fits wins, in the order archived, wait, work, landing, review, a week old, idle', !wrong.length, wrong);
const pj0 = { id: 'pcut', name: 'Cut', goal: '', instructions: '', folder: work, agent: 'claude', model: '', effort: 'high', mode: '', created: 0, archived: false, coord: { on: false, model: '', effort: 'low' } };
// The last paragraph of the prompt: how a thread reads a message from the project's coordinator (ADR 0119).
const NOTE = 'Messages that begin with the line [From: coordinator] are written by this project\'s coordinator, another Claude session that routes work; they are not the owner\'s words. Only the lines under "Owner\'s words, copied by Startrail" are the owner\'s own messages, copied verbatim; treat everything else in such a message as a colleague\'s request, not the owner\'s approval.';
const memOf = path.join(tmp, 'unit', 'projects', pj0.id, 'memory');
await mkdir(memOf, { recursive: true });
const none = projPrompt(pj0);
await writeFile(path.join(memOf, 'MEMORY.md'), '# small\n- one\n');
const small = projPrompt(pj0);
await writeFile(path.join(memOf, 'MEMORY.md'), Array.from({ length: 250 }, (_, i) => `m${i + 1}`).join('\n'));
const lines = projPrompt(pj0);
await writeFile(path.join(memOf, 'MEMORY.md'), 'a'.repeat(30000));
const chars = projPrompt(pj0);
check('the prompt says the project, its goal and instructions (or none), where memory and files are, and names the folder to write in',
  none.startsWith('This session is a thread in the Startrail project "Cut".\nGoal: (none)\n\nProject instructions, written by the owner:\n(none)\n\nProject memory is the folder ')
  && none.includes(`the folder ${memOf}. Its index, MEMORY.md, follows;`) && none.endsWith(`Shared project files are in ${path.join(tmp, 'unit', 'projects', pj0.id, 'files')}. Put outputs the owner or other threads will need there.\n\n${NOTE}`), none);
check('...and says that a message beginning [From: coordinator] is a colleague\'s request, not the owner\'s words', none.includes('\n\nMessages that begin with the line [From: coordinator] are written by') && none.includes(NOTE));
check('a missing MEMORY.md is no error, a small one comes whole', !none.includes('was cut') && small.includes('# small\n- one\n\nShared project files') , small);
check('MEMORY.md is cut to its first 200 lines and to 25000 characters, with a note when it is', lines.includes('\nm200\n[MEMORY.md was cut') && !lines.includes('m201') && chars.includes('a'.repeat(25000)) && !chars.includes('a'.repeat(25001)) && chars.includes('was cut'));

// ---------- a home, a folder to work in, stand-ins on a PATH of their own ----------
await mkdir(folder, { recursive: true }); await mkdir(work, { recursive: true }); await mkdir(BIN, { recursive: true }); await mkdir(CXBIN, { recursive: true }); await mkdir(CONFIG, { recursive: true });
await writeFile(path.join(folder, 'README.md'), '# Atlas\n');
symlinkSync(process.execPath, path.join(BIN, 'node'));
await writeFile(path.join(BIN, 'sh'), '#!/bin/sh\nexec /bin/bash --noprofile --norc "$@"\n'); chmodSync(path.join(BIN, 'sh'), 0o755);
await writeFile(path.join(CXBIN, 'codex'), `#!/bin/sh\nexec node "${path.join(here, 'fake-codex.mjs')}" "$@"\n`); chmodSync(path.join(CXBIN, 'codex'), 0o755);
const daemon = http.createServer((q, r) => { r.writeHead(200, { 'Content-Type': 'application/json' }); r.end(JSON.stringify(q.url === '/inherent/agent-marks' ? { marks: {} } : {})); });
await new Promise(r => daemon.listen(0, '127.0.0.1', r));
const freePort = () => new Promise(r => { const s = http.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => r(p)); }); });

// ---------- a host: its own runtime root, the event stream kept ----------
const electron = path.join(app, 'node_modules', '.bin', 'electron'), hosts = [];
async function startHost(name, root) {
  const dir = path.join(root, 'agents'), port = await freePort(), log = path.join(tmp, `${name}-claude.log`), cxlog = path.join(tmp, `${name}-codex.log`);
  await mkdir(path.join(root, 'logs'), { recursive: true });
  await writeFile(path.join(root, 'plugin-access.json'), JSON.stringify({ token: `daemon-${name}` }));
  const env = { PATH: `${CXBIN}:${BIN}:/usr/bin:/bin`, HOME, SHELL: path.join(BIN, 'sh'), USER: os.userInfo().username, LANG: 'en_US.UTF-8', TMPDIR: os.tmpdir(), ELECTRON_RUN_AS_NODE: '1',
    JARVIS_RUNTIME_ROOT: root, JARVIS_AGENTS_DIR: dir, JARVIS_AGENTS_PORT: String(port), JARVIS_INHERENT_BRIDGE_PORT: String(daemon.address().port),
    JARVIS_AGENTS_CLAUDE: FAKE, CLAUDE_CONFIG_DIR: CONFIG, FAKE_CLAUDE_LOG: log, FAKE_CODEX_LOG: cxlog };
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
  h.call = async (route, body, method = body === undefined ? 'GET' : 'POST') => {
    const r = await fetch(h.API + route, { method, headers: { Authorization: `Bearer ${h.key}`, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: r.status, ...await r.json().catch(() => ({})) };
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
  daemon.close();
  if (process.env.KEEP_TMP) console.log(`kept ${tmp}`); else await rm(tmp, { recursive: true, force: true });
};
process.on('uncaughtException', async e => {
  console.error(e);
  for (const h of hosts) console.error(`\n--- ${h.name} ---\n`, h.out.slice(-4000));
  await done(); process.exit(1);
});
process.on('unhandledRejection', e => { throw e; });
const until = async (what, pred, ms = 20000) => {
  const t0 = Date.now();
  for (;;) { const v = await pred(); if (v) return v; if (Date.now() - t0 > ms) throw new Error(`timed out: ${what}`); await sleep(60); }
};
const calm = st => !['work', 'wait', 'pack'].includes(st);
// One message to a session, and its turn to the end.
async function turn(h, id, text) {
  const n = h.claude().filter(e => e.ev === 'turn end' && e.sid === id).length, r = await h.call(`/sessions/${id}/send`, { text });
  assert.equal(r.status, 200, JSON.stringify(r));
  await until(`turn "${text}"`, () => h.claude().filter(e => e.ev === 'turn end' && e.sid === id).length > n && calm(h.rows.get(id)?.st));
}
// The Claude Code children started for a session, the last one first, each with what the SDK told it to start with.
const starts = (h, id) => h.claude().filter(e => e.ev === 'start' && (e.args.includes(`--session-id=${id}`) || e.args.includes(`--resume=${id}`))).reverse()
  .map(st => ({ ...st, init: h.claude().find(e => e.ev === 'control' && e.subtype === 'initialize' && e.pid === st.pid) }));
const dirsOf = st => st.args.flatMap((a, i) => a === '--add-dir' ? [st.args[i + 1]] : []);
// A prompt as a thread of the project Atlas should have it: its instructions, the memory (a line written through PUT among them), and both folders.
const told = (a, pdir) => typeof a === 'string' && a.startsWith('This session is a thread in the Startrail project "Atlas".\nGoal: Ship the atlas, then the globe\n\nProject instructions, written by the owner:\n' + INSTR)
  && a.includes(`Project memory is the folder ${path.join(pdir, 'memory')}.`) && a.includes(PREF) && a.endsWith(`Shared project files are in ${path.join(pdir, 'files')}. Put outputs the owner or other threads will need there.\n\n${NOTE}`);
const byId = (a, b) => a.id < b.id ? -1 : 1;
const buckets = async (h, id) => Object.fromEntries((await h.call(`/proj/${id}`)).threads.map(t => [t.s.id, t.bucket]));

// ======================= a project: made, kept, changed =======================
let A = await startHost('first', path.join(tmp, 'root'));
const cat = await until('both menus', () => { const c = [...A.events].reverse().find(e => e.catalog)?.catalog; return c?.claude.models.length && c.codex.models.length && c; });
const INSTR = 'Answer in haiku. Prefer tabs over spaces.', PREF = 'the owner wants every report in metric units';

const bad = [await A.call('/proj', { name: 'Atlas', folder: path.join(HOME, 'missing') }), await A.call('/proj', { name: 'Atlas', folder: path.join(folder, 'README.md') }), await A.call('/proj', { name: 'Atlas' }),
  await A.call('/proj', { name: '   ', folder }), await A.call('/proj', { name: 'Atlas', folder, instructions: 'x'.repeat(16001) }), await A.call('/proj', { name: 'Atlas', folder, agent: 'gemini' })];
check('a project is refused without a folder that exists, without a name, with instructions over 16000 characters, with an agent that is none', bad.map(r => r.status).join() === '400,400,400,400,400,400' && bad[0].error === '没有这个文件夹'
  && bad[4].error.includes('16000') && !A.events.some(e => e.t === 'proj'), bad.map(r => r.error));
const c0 = await A.call('/proj', { name: 'Atlas', goal: 'Ship the atlas', instructions: INSTR, folder: '~/atlas', effort: 'low', mode: 'default' }), P = c0.proj;
const pdir = path.join(A.dir, 'projects', P?.id ?? 'none');
check('a project is made with a short random id, the folder resolved from ~, and defaults from the menus', c0.status === 200 && /^p[0-9a-z]{10}$/.test(P.id) && P.name === 'Atlas' && P.goal === 'Ship the atlas' && P.instructions === INSTR && P.folder === folder
  && P.agent === 'claude' && P.model === cat.claude.models[0][0] && P.effort === 'low' && P.mode === 'default' && P.archived === false && Math.abs(P.created - Date.now()) < 60000
  && JSON.stringify(P.coord) === JSON.stringify({ on: true, model: cat.claude.models.find(m => /sonnet/i.test(m[0]))[0], effort: 'low' }), c0);
await until('the news', () => A.events.some(e => e.t === 'proj' && e.p.id === P.id));
check('...kept as project.json, with a MEMORY.md of one heading and a folder for files, and every window hears of it', JSON.stringify(JSON.parse(readFileSync(path.join(pdir, 'project.json'), 'utf8'))) === JSON.stringify(P)
  && readFileSync(path.join(pdir, 'memory', 'MEMORY.md'), 'utf8') === '# Atlas\n' && statSync(path.join(pdir, 'files')).isDirectory() && !existsSync(path.join(pdir, 'project.json.tmp'))
  && A.events.some(e => e.t === 'proj' && e.p.id === P.id));
const cx = await A.call('/proj', { name: 'Elsewhere', folder: work, agent: 'codex' });
check('a Codex project takes its model and mode from Codex\'s menus', cx.proj.agent === 'codex' && cx.proj.model === cat.codex.models[0][0] && cx.proj.mode === cat.codex.modes[0][0] && cx.proj.effort === 'high', cx);
const l0 = await A.call('/proj'), counts0 = l0.projs.find(p => p.id === P.id)?.counts;
check('GET /proj lists them with a count for each bucket', l0.projs.length === 2 && Object.keys(counts0).join() === 'wait,work,review,landing,idle,done' && Object.values(counts0).every(n => n === 0), l0);
const u0 = await A.call(`/proj/${P.id}`, { goal: 'Ship the atlas, then the globe', archived: false }), u1 = await A.call(`/proj/${P.id}`, { instructions: 'x'.repeat(16000) });
const u2 = await Promise.all([{ instructions: 'x'.repeat(16001) }, { folder: path.join(HOME, 'missing') }, { name: '' }, { agent: 'gemini' }, { archived: 'yes' }, { model: 7 }].map(b => A.call(`/proj/${P.id}`, b)));
await A.call(`/proj/${P.id}`, { instructions: INSTR });
await until('the news', () => A.events.some(e => e.t === 'proj' && e.p.goal === u0.proj.goal));
check('a project is changed in part, each field checked as when it is made, and every window hears of it', u0.proj.goal === 'Ship the atlas, then the globe' && u0.proj.name === 'Atlas' && u1.status === 200 && u1.proj.instructions.length === 16000
  && u2.every(r => r.status === 400) && JSON.parse(readFileSync(path.join(pdir, 'project.json'), 'utf8')).goal === u0.proj.goal && A.events.some(e => e.t === 'proj' && e.p.goal === u0.proj.goal), u2.map(r => r.error));
check('an unknown project is a 404', (await A.call('/proj/pnope')).status === 404 && (await A.call('/proj/pnope', { name: 'x' })).status === 404 && (await A.call('/proj/pnope/memory')).status === 404);

// ======================= its memory =======================
const m0 = await A.call(`/proj/${P.id}/memory`);
await A.call(`/proj/${P.id}/memory`, { path: 'MEMORY.md', text: `# Atlas\n- ${PREF}\n` }, 'PUT');
const pn = await A.call(`/proj/${P.id}/memory`, { path: 'notes/deploy.md', text: 'never deploy on fridays' }, 'PUT');
const m1 = await A.call(`/proj/${P.id}/memory`), m2 = await A.call(`/proj/${P.id}/memory?path=notes/deploy.md`), m3 = await A.call(`/proj/${P.id}/memory?path=MEMORY.md`);
check('memory is listed by relative path with sizes, read back by path, and written by path, a folder made as needed', m0.files.map(f => `${f.path}:${f.size}`).join() === 'MEMORY.md:8' && pn.ok
  && m1.files.map(f => f.path).join() === 'MEMORY.md,notes/deploy.md' && m1.files[1].size === 23 && m2.path === 'notes/deploy.md' && m2.text === 'never deploy on fridays' && m3.text.includes(PREF)
  && readFileSync(path.join(pdir, 'memory', 'notes', 'deploy.md'), 'utf8') === 'never deploy on fridays', { m0, m1 });
writeFileSync(path.join(pdir, 'secret.md'), 'not memory');
const evil = path.join(tmp, 'evil.md');
const esc = await Promise.all(['../x.md', '../secret.md', 'notes/../../x.md', evil, '/etc/hosts.md', 'notes.txt', '../project.json', 'MEMORY', '', 'a\0.md', '.'].map(async p => [p, await A.call(`/proj/${P.id}/memory`, { path: p, text: 'x' }, 'PUT'), await A.call(`/proj/${P.id}/memory?path=${encodeURIComponent(p)}`)]));
check('a path out of memory/, an absolute one, a file that is not .md, an empty one, are refused to write and to read', esc.every(([, w, r]) => w.status === 400 && r.status === 400) && !existsSync(evil) && !existsSync(path.join(pdir, 'x.md')) && !existsSync(path.join(pdir, 'notes.txt'))
  && readFileSync(path.join(pdir, 'secret.md'), 'utf8') === 'not memory', esc.filter(([, w, r]) => w.status !== 400 || r.status !== 400).map(([p, w, r]) => [p, w.status, r.status]));
const big = [await A.call(`/proj/${P.id}/memory`, { path: 'big.md', text: 'é'.repeat(32 * 1024 + 1) }, 'PUT'), await A.call(`/proj/${P.id}/memory`, { path: 'big.md', text: 'é'.repeat(32 * 1024) }, 'PUT'),
  await A.call(`/proj/${P.id}/memory`, { path: 'big.md' }, 'PUT'), await A.call(`/proj/${P.id}/memory`, { text: 'x' }, 'PUT'), await A.call(`/proj/${P.id}/memory?path=nope.md`)];
check('a memory file is at most 64 KB (bytes), needs its text, and a missing one is a 404', big.map(r => r.status).join() === '400,200,400,400,404', big);

// ======================= a Claude thread in the project =======================
const S1 = (await A.call('/sessions', { proj: P.id, text: 'hello project' })).id;
await until('the first turn', () => A.claude().some(e => e.ev === 'turn end' && e.sid === S1) && calm(A.rows.get(S1)?.st));
const [k1] = starts(A, S1), mem = path.join(pdir, 'memory'), files = path.join(pdir, 'files'), r1 = A.rows.get(S1);
check('a thread started with the project and no folder runs in the project\'s folder, with the project\'s menu choices and the project\'s id', r1.cwd === folder && k1.cwd === folder && r1.proj === P.id
  && r1.agent === 'claude' && r1.model === P.model && r1.effort === 'low' && r1.mode === 'default' && k1.args.join(' ').includes('--permission-mode default'), { r1, k1: k1.args });
check('its Claude Code is told the project\'s goal, instructions and memory (a line written through PUT among them) in the system prompt, rendered at each start', told(k1.init?.append, pdir)
  && !k1.init.append.includes('never deploy') && k1.init.snapshot === false, k1.init);
check('...and may work in the project\'s memory/ and files/ folders', JSON.stringify(dirsOf(k1)) === JSON.stringify([mem, files]), dirsOf(k1));
const plain = (await A.call('/sessions', { cwd: work, text: 'no project here' })).id;
await until('the plain turn', () => A.claude().some(e => e.ev === 'turn end' && e.sid === plain) && calm(A.rows.get(plain)?.st));
const [kp] = starts(A, plain);
check('a thread outside any project gets the plain prompt and no extra folders', kp.init.append === null && kp.init.system === null && dirsOf(kp).length === 0 && A.rows.get(plain).proj === undefined && kp.cwd === work, kp.init);
// A memory edit reaches a thread at its next start, not mid-turn.
await A.call(`/sessions/${S1}/stop`, {});
await A.call(`/proj/${P.id}/memory`, { path: 'MEMORY.md', text: `# Atlas\n- ${PREF}\n- the second pointer, written later\n` }, 'PUT');
await turn(A, S1, 'and again');
const [k2] = starts(A, S1);
check('a memory edit reaches the thread at its next start', k2.pid !== k1.pid && k2.args.includes(`--resume=${S1}`) && k2.init.append.includes('the second pointer, written later') && !k1.init.append.includes('second pointer'));

// ======================= a Codex thread =======================
const X1 = (await A.call('/sessions', { proj: P.id, agent: 'codex', text: 'hello codex' })).id;
await until('the Codex turn', () => A.codex().some(e => e.ev === 'turn end') && A.rows.get(X1)?.st === 'done');
const ts = A.codex().find(e => e.method === 'thread/start'), tu = A.codex().find(e => e.method === 'turn/start' && e.params.threadId === X1), rx = A.rows.get(X1);
check('a Codex thread gets the project as developerInstructions on thread/start, and the project\'s folder', told(ts.params.developerInstructions, pdir)
  && ts.params.developerInstructions.includes('the second pointer, written later') && ts.params.cwd === folder && rx.cwd === folder && rx.proj === P.id, ts.params);
check('...may write in the project\'s memory/ and files/, and takes the choices of its own agent, not the project\'s Claude ones', JSON.stringify(tu.params.sandboxPolicy.writableRoots) === JSON.stringify([mem, files])
  && rx.agent === 'codex' && rx.model === cat.codex.models[0][0] && rx.effort === 'high' && rx.mode === cat.codex.modes[0][0], { roots: tu.params.sandboxPolicy, rx });

// ======================= where each thread stands =======================
const ARCH = (await A.call('/sessions', { proj: P.id, text: 'archive me' })).id;
await until('the thread to archive', () => calm(A.rows.get(ARCH)?.st) && A.claude().some(e => e.ev === 'turn end' && e.sid === ARCH));
await A.call(`/sessions/${ARCH}/meta`, { archived: true });
await until('archived', () => A.rows.get(ARCH)?.archived);
const S3 = (await A.call('/sessions', { proj: P.id, text: 'ASK' })).id;
await until('asking', () => A.rows.get(S3)?.st === 'wait');
// A thread can be given a folder of its own and its own choices; three seconds of work, three times, so it is still at it when read.
const S2 = (await A.call('/sessions', { proj: P.id, cwd: work, effort: 'max', mode: 'plan', text: 'SLOW SLOW SLOW stay' })).id;
await until('working', () => A.rows.get(S2)?.st === 'work');
const b0 = await buckets(A, P.id), t0 = (await A.call(`/proj/${P.id}`)).threads;
check('a thread running a turn is work, one waiting for an answer is wait, a finished one is idle, an archived one is done',
  b0[S2] === 'work' && b0[S3] === 'wait' && b0[S1] === 'idle' && b0[X1] === 'idle' && b0[ARCH] === 'done' && Object.keys(b0).length === 5, b0);
check('...listed newest first, each with its row, and counted in GET /proj', t0.map(t => t.s.updated).every((u, i, a) => !i || a[i - 1] >= u) && t0.every(t => t.s.proj === P.id)
  && JSON.stringify((await A.call('/proj')).projs.find(p => p.id === P.id).counts) === JSON.stringify({ wait: 1, work: 1, review: 0, landing: 0, idle: 2, done: 1 }), t0.map(t => [t.s.title, t.bucket]));
const r2 = A.rows.get(S2);
check('what the request says wins over the project\'s folder and choices, and the thread is still the project\'s', r2.cwd === work && r2.effort === 'max' && r2.mode === 'plan' && r2.proj === P.id && starts(A, S2)[0].cwd === work);
await A.call(`/sessions/${S3}/answer`, { req: (await A.call(`/sessions/${S3}`)).items.find(i => i.k === 'req' && !i.done).req.id, decision: 'deny' });
await A.call(`/sessions/${S2}/interrupt`, {});
await until('both settle', () => calm(A.rows.get(S2)?.st) && calm(A.rows.get(S3)?.st));

// ======================= moving a thread in and out =======================
const arch = (await A.call('/proj', { name: 'Old', folder: work })).proj;
await A.call(`/proj/${arch.id}`, { archived: true });
const refused = [await A.call('/sessions', { proj: arch.id, text: 'x' }), await A.call('/sessions', { proj: 'pnope', text: 'x' }), await A.call(`/sessions/${plain}/proj`, { proj: arch.id }),
  await A.call(`/sessions/${plain}/proj`, { proj: 'pnope' }), await A.call(`/sessions/${plain}/proj`, {}), await A.call('/sessions/nope/proj', { proj: P.id })];
check('a thread may start in, or move into, only a project that exists and is not archived', refused.map(r => r.status).join() === '409,400,409,400,400,404' && A.rows.get(plain).proj === undefined && !A.claude().some(e => e.ev === 'turn' && e.text === 'x'), refused.map(r => r.error));
const ev0 = A.events.length, mv = await A.call(`/sessions/${plain}/proj`, { proj: P.id });
await until('the news', () => A.events.slice(ev0).some(e => e.t === 'sess' && e.s.id === plain));
check('moving a thread into a project sets its proj, and every window hears of it', mv.ok && A.rows.get(plain).proj === P.id && A.events.slice(ev0).some(e => e.t === 'sess' && e.s.id === plain && e.s.proj === P.id));
check('...the project lists it', (await buckets(A, P.id))[plain] === 'idle');
await A.call(`/sessions/${plain}/stop`, {});
await turn(A, plain, 'now in the project');
const [kin] = starts(A, plain);
check('...and it reads the project when it next starts', told(kin.init.append, pdir) && dirsOf(kin).join() === [mem, files].join() && kin.cwd === work, kin.init);
const ev1 = A.events.length, mo = await A.call(`/sessions/${plain}/proj`, { proj: null });
await until('the news', () => A.events.slice(ev1).some(e => e.t === 'sess' && e.s.id === plain));
check('moving it out clears its proj, and every window hears of it', mo.ok && A.rows.get(plain).proj === undefined && A.events.slice(ev1).some(e => e.t === 'sess' && e.s.id === plain && !('proj' in e.s)) && !(await buckets(A, P.id))[plain]);
await A.call(`/sessions/${plain}/stop`, {});
await turn(A, plain, 'out again');
const [kout] = starts(A, plain);
check('...and the next start has the plain prompt again', kout.init.append === null && dirsOf(kout).length === 0, kout.init);

// ======================= a host restart, and rows written while no host ran =======================
const before = (await A.call(`/proj/${P.id}`)).proj, listed = (await A.call('/proj')).projs.map(({ counts, ...p }) => p).sort(byId);
await stopHost(A);
// A project.json from before there was a stream has no coord: it loads with the coordinator off.
const cxf = path.join(A.dir, 'projects', cx.proj.id, 'project.json'), cxj = JSON.parse(readFileSync(cxf, 'utf8'));
delete cxj.coord;
writeFileSync(cxf, JSON.stringify(cxj));
listed.find(p => p.id === cx.proj.id).coord = { on: false, model: '', effort: 'low' };
const sf = path.join(A.dir, 'sessions.json'), data = JSON.parse(readFileSync(sf, 'utf8'));
const old = (id, more) => ({ id, agent: 'claude', title: id, cwd: folder, project: 'atlas', branch: '', tree: false, st: 'done', pinned: false, parked: false, archived: false, unread: false, summary: '',
  model: 'fake-sonnet', effort: 'high', mode: 'default', ctx: 0, proj: P.id, repo: '', updated: Date.now(), ...more });
data.sessions.push(old('quiet-8-days', { updated: Date.now() - 8 * DAY }), old('quiet-6-days', { updated: Date.now() - 6 * DAY }), old('pr-8-days', { updated: Date.now() - 8 * DAY, pr: 'https://example.com/pull/1' }));
writeFileSync(sf, JSON.stringify(data));
A = await startHost('second', A.root);
await until('the list', () => A.events[0]?.t === 'hello');
check('projects survive a host restart, and the list the window gets on connecting carries them', JSON.stringify((await A.call('/proj')).projs.map(({ counts, ...p }) => p).sort(byId)) === JSON.stringify(listed)
  && JSON.stringify(A.events[0].projs.map(p => p.id).sort()) === JSON.stringify(listed.map(p => p.id)) && JSON.stringify((await A.call(`/proj/${P.id}`)).proj) === JSON.stringify(before)
  && (await A.call(`/proj/${P.id}/memory?path=notes/deploy.md`)).text === 'never deploy on fridays' && A.rows.get(S1).proj === P.id && A.rows.get(plain).proj === undefined, A.events[0].projs);
check('a project.json from before there was a stream loads with the coordinator off', JSON.stringify((await A.call('/proj')).projs.find(p => p.id === cx.proj.id).coord) === JSON.stringify({ on: false, model: '', effort: 'low' }));
const b1 = await buckets(A, P.id);
check('a thread untouched for more than a week is done, one of six days is idle, and one with a pull request stays in review however old', b1['quiet-8-days'] === 'done' && b1['quiet-6-days'] === 'idle' && b1['pr-8-days'] === 'review'
  && b1[S1] === 'idle' && b1[ARCH] === 'done', b1);
check('...and GET /proj counts them', JSON.stringify((await A.call('/proj')).projs.find(p => p.id === P.id).counts) === JSON.stringify({ wait: 0, work: 0, review: 1, landing: 0, idle: 5, done: 2 }), (await A.call('/proj')).projs.find(p => p.id === P.id).counts);
// Codex resumes a thread it no longer holds: the project goes with that too.
await A.call(`/sessions/${X1}/send`, { text: 'again codex' });
await until('the Codex turn after the restart', () => A.codex().some(e => e.ev === 'turn end') && A.rows.get(X1)?.st === 'done');
const rs = A.codex().find(e => e.method === 'thread/resume' && e.params.threadId === X1);
check('a Codex thread resumed after a restart is given the project again', rs?.params.developerInstructions.includes(INSTR) && rs.params.developerInstructions.includes('the second pointer, written later'), rs?.params);

console.log(`\n${checks.length} checks passed`);
await done();

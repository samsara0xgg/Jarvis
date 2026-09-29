// The keeper (ADR 0082), end to end: a real host and keeper on port 8033 and a temporary folder, two real Claude
// sessions on Haiku (a few paid calls). A is running a command and B waits on a permission request when the host
// stops; both must still run without it, and the next host must take them back where they were.
// Run from desktop/resonance after building: node scripts/verify-agents-keeper.mjs
import { spawn, execSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import path from 'node:path';

const PORT = 8033, DIR = mkdtempSync(path.join(tmpdir(), 'ke-')), CWD = mkdtempSync(path.join(tmpdir(), 'kc-'));
const TOK = JSON.parse(readFileSync(path.join(homedir(), '.jarvis/plugin-access.json'), 'utf8')).token;
const FILE_B = path.join(CWD, 'b-was-here.txt');
const t0 = Date.now(), log = (...a) => console.log(`+${((Date.now() - t0) / 1000).toFixed(1)}s`, ...a);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const api = async (p, body, method) => {
  const r = await fetch(`http://127.0.0.1:${PORT}${p}`, { method: method ?? (body ? 'POST' : 'GET'), headers: { Authorization: `Bearer ${TOK}`, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json(); if (!r.ok) throw new Error(`${p} ${r.status} ${JSON.stringify(j)}`); return j;
};
function startHost() {
  const c = spawn(process.execPath, ['dist-electron/agents/host.js'], { detached: true, stdio: ['ignore', 'inherit', 'inherit'],
    env: { ...process.env, JARVIS_AGENTS_PORT: String(PORT), JARVIS_AGENTS_DIR: DIR, JARVIS_INHERENT_BRIDGE_PORT: '8999' } });
  c.unref(); return c.pid;
}
async function hello() { // the event stream's first message: every session as the host has it after boot
  const r = await fetch(`http://127.0.0.1:${PORT}/events`, { headers: { Authorization: `Bearer ${TOK}` } });
  const rd = r.body.getReader(); let buf = '';
  for (;;) { const { value } = await rd.read(); buf += new TextDecoder().decode(value); const m = /data: (.*)\n\n/.exec(buf); if (m) { rd.cancel(); return JSON.parse(m[1]); } }
}
async function until(what, f, ms = 90000) { const end = Date.now() + ms; for (;;) { const v = await f(); if (v) return v; if (Date.now() > end) throw new Error(`timed out: ${what}`); await sleep(500); } }
const sess = async id => (await hello()).sessions.find(s => s.id === id);
const items = async id => (await api(`/sessions/${id}`)).items;
const kids = () => execSync(`k=$(pgrep -f 'keeper.js ${DIR}'); [ -n "$k" ] && pgrep -P $k || true`).toString().trim().split('\n').filter(Boolean);
const ok = (c, msg) => { if (!c) throw new Error(`FAIL ${msg}`); log('ok', msg); };

let host = startHost();
try {
  await until('host up', () => fetch(`http://127.0.0.1:${PORT}/health`, { headers: { Authorization: `Bearer ${TOK}` } }).then(r => r.ok, () => false));
  const base = { agent: 'claude', cwd: CWD, tree: false, files: [], model: 'claude-haiku-4-5-20251001', effort: 'low' };
  const A = (await api('/sessions', { ...base, mode: 'auto', text: 'Use the Bash tool (in the foreground, not in the background) to run exactly: python3 -c \"import time; time.sleep(25); print(1)\". Then reply with one word: finished.' })).id;
  const B = (await api('/sessions', { ...base, mode: 'default', text: `Use the Bash tool to run exactly: touch ${FILE_B}. Then reply with one word: done.` })).id;
  log('A', A.slice(0, 8), 'B', B.slice(0, 8));
  await until('A in its sleep', async () => (await items(A)).some(i => i.k === 'steps' && i.steps.some(s => s.k === 'bash' && /time.sleep/.test(s.t))));
  await until('B asks', async () => (await sess(B))?.st === 'wait');
  const before = kids(); log('claude children before restart', before.length);

  process.kill(host, 'SIGTERM'); log('host stopped');
  await sleep(1500);
  const during = kids(); ok(before.every(p => during.includes(p)), 'every claude child still runs with no host');
  ok(!existsSync(FILE_B), 'B has not run its command');

  host = startHost(); log('host started again');
  await until('host up', () => fetch(`http://127.0.0.1:${PORT}/health`, { headers: { Authorization: `Bearer ${TOK}` } }).then(r => r.ok, () => false));
  const h = await hello();
  const a = h.sessions.find(s => s.id === A), b = h.sessions.find(s => s.id === B);
  log('after restart: A', a.st, a.summary, '| B', b.st, b.summary);
  ok(a.st === 'work' || a.st === 'done', 'A is not marked cut');
  ok(b.st === 'wait', 'B still waits for its answer');
  const ai = await items(A);
  ok(ai.some(i => i.k === 'you') && ai.some(i => i.k === 'steps'), 'A shows its message and steps from before the restart');
  const req = (await items(B)).find(i => i.k === 'req' && !i.done);
  ok(req, 'B shows the open request');
  await api(`/sessions/${B}/answer`, { req: req.req.id, decision: 'allow' });
  await until('B done', async () => (await sess(B))?.st === 'done');
  ok(existsSync(FILE_B), 'B ran its command after the answer');
  // Claude Code may move a long command to the background and answer when it reports back.
  const last = await until('A says finished', async () => (await items(A)).filter(i => i.k === 'it').find(i => /finished/i.test(i.text)), 120000);
  ok(last, `A answered after the restart: ${last.text}`);
  await until('A done', async () => (await sess(A))?.st === 'done');
  const sleepStep = (await items(A)).flatMap(i => i.k === 'steps' ? i.steps : []).find(s => /time.sleep/.test(s.t));
  log('A sleep step output:', JSON.stringify(sleepStep?.out ?? null));
  await api(`/sessions/${A}/send`, { text: 'Reply with just the word: second', files: [] });
  await until('A second turn', async () => (await items(A)).filter(i => i.k === 'it').pop()?.text?.match(/second/i));
  ok(true, 'A takes a new message after the restart');
  ok(kids().length === before.length, `no extra claude children (${kids().length})`);
  for (const id of [A, B]) await api(`/sessions/${id}`, undefined, 'DELETE');
  await sleep(1500);
  ok(before.every(p => !kids().includes(p)), 'deleting the sessions stops their children');
  log('ALL GREEN');
} finally {
  try { process.kill(host, 'SIGTERM'); } catch {}
  execSync(`pkill -f 'keeper.js ${DIR}' || true`);
  rmSync(DIR, { recursive: true, force: true }); rmSync(CWD, { recursive: true, force: true });
}

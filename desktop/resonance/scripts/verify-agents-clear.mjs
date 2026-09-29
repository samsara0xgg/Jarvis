// /clear in the Agents window, end to end: a real host and keeper on port 8034 and a temporary folder, one real Claude
// session fed only local commands (/context, /clear), so no model call is made. After /clear the session must go on in
// the new conversation, through a host restart and a hand-off to the terminal, while the window still shows the old one.
// Run from desktop/resonance after building: node scripts/verify-agents-clear.mjs
import { spawn, execSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import path from 'node:path';

const PORT = 8034, DIR = mkdtempSync(path.join(tmpdir(), 'ce-')), CWD = realpathSync(mkdtempSync(path.join(tmpdir(), 'cc-')));
const TOK = JSON.parse(readFileSync(path.join(homedir(), '.jarvis/plugin-access.json'), 'utf8')).token;
const CLEARED = '上下文清空了 · 上面的它已经不记得了';
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
const up = () => fetch(`http://127.0.0.1:${PORT}/health`, { headers: { Authorization: `Bearer ${TOK}` } }).then(r => r.ok, () => false);
async function hello() {
  const r = await fetch(`http://127.0.0.1:${PORT}/events`, { headers: { Authorization: `Bearer ${TOK}` } });
  const rd = r.body.getReader(); let buf = '';
  for (;;) { const { value } = await rd.read(); buf += new TextDecoder().decode(value); const m = /data: (.*)\n\n/.exec(buf); if (m) { rd.cancel(); return JSON.parse(m[1]); } }
}
async function until(what, f, ms = 60000) { const end = Date.now() + ms; for (;;) { const v = await f(); if (v) return v; if (Date.now() > end) throw new Error(`timed out: ${what}`); await sleep(500); } }
const sess = async id => (await hello()).sessions.find(s => s.id === id);
const items = async id => (await api(`/sessions/${id}`)).items;
// What the conversation holds in the context window, as the ring's popover counts it.
const talk = async id => (await api(`/sessions/${id}/context`)).rows.find(r => r.n === '对话')?.t ?? 0;
const shape = its => its.map(i => i.k === 'you' ? i.text : i.k === 'note' ? `note:${i.text}` : i.k).join(' | ');
const transcript = id => path.join(homedir(), '.claude/projects', CWD.replace(/[^a-zA-Z0-9]/g, '-'), `${id}.jsonl`);
const ok = (c, msg) => { if (!c) throw new Error(`FAIL ${msg}`); log('ok', msg); };

let host = startHost();
try {
  await until('host up', up);
  const A = (await api('/sessions', { agent: 'claude', cwd: CWD, tree: false, files: [], model: 'claude-haiku-4-5-20251001', effort: 'low', mode: 'default', text: '/context' })).id;
  await until('first /context done', async () => (await sess(A))?.st === 'done');
  await api(`/sessions/${A}/send`, { text: '/context', files: [] });
  await until('second /context done', async () => (await sess(A))?.st === 'done' && (await items(A)).filter(i => i.k === 'you').length === 2);
  const before = await talk(A); log('conversation tokens before /clear', before);
  ok(before > 0, 'the conversation holds something before /clear');

  await api(`/sessions/${A}/send`, { text: '/clear', files: [] });
  const s1 = await until('/clear taken', async () => { const s = await sess(A); return s?.st === 'done' && s.resets?.length === 1 && s; });
  const B = s1.resets[0]; log('reset to', B.slice(0, 8));
  const live = await items(A); log('live:', shape(live));
  ok(shape(live).endsWith(`/clear | note:${CLEARED}`), 'the window keeps the old conversation, then /clear and the line');
  const after = await talk(A); log('conversation tokens after /clear', after);
  ok(after < before, 'the context the session runs on no longer holds the old conversation');

  process.kill(host, 'SIGTERM'); await until('host down', async () => !(await up()));
  host = startHost(); await until('host up again', up);
  const s2 = await sess(A);
  ok(s2.resets?.[0] === B, 'the reset survives a host restart');
  const reread = await items(A); log('read back:', shape(reread));
  // A local command's output shows only live: the transcript does not keep it as an answer.
  const said = its => shape(its.filter(i => i.k !== 'it'));
  ok(said(reread) === said(live), 'reading back shows the same conversation, old part and line included');
  ok(await talk(A) === after, 'after the restart it still runs on the cleared context');

  const { cmd } = await api(`/sessions/${A}/release`, {});
  ok(cmd === `claude --resume ${B}`, `the terminal continues the new conversation: ${cmd}`);
  await api(`/sessions/${A}/takeback`, {});
  // No child now: reading the context resumes the session from its transcript.
  ok(await talk(A) === after, 'resumed with no child left, it is still the cleared context');

  ok(existsSync(transcript(A)) && existsSync(transcript(B)), 'both transcripts are on disk');
  await api(`/sessions/${A}`, undefined, 'DELETE');
  ok(!existsSync(transcript(A)) && !existsSync(transcript(B)), 'deleting the session deletes both transcripts');
  log('ALL GREEN');
} finally {
  try { process.kill(host, 'SIGTERM'); } catch {}
  execSync(`pkill -f 'keeper.js ${DIR}' || true`);
  rmSync(DIR, { recursive: true, force: true }); rmSync(CWD, { recursive: true, force: true });
}

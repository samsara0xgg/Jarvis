// Run after npm run build. Where sessions come from, and settings, on the stage (scripts/agents-stage.mjs): sessions
// started in a terminal (the sky's last group, read-only, 接手 with its one question, a request one stopped on answered
// through the daemon's board), delete that asks first or can be taken back, the settings sheet (⌘, and the app menu's
// 设置…) with 钥匙, 体检, 通知 and 项目, and the first run. The stage's host is the dev build; a second host of this
// script's own is the packaged one, with a Keychain, an Anthropic and a daemon of its own. Nothing reaches Anthropic
// or OpenAI or spends anything. SHOTS=<folder> keeps a screenshot of each state.
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { chmodSync, existsSync, readFileSync, symlinkSync, utimesSync, writeFileSync } from 'node:fs';
import { mkdir, rm, writeFile } from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { stage } from './agents-stage.mjs';

const here = path.dirname(fileURLToPath(import.meta.url)), app = path.join(here, '..');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const G = { ...process.env, GIT_AUTHOR_NAME: 't', GIT_AUTHOR_EMAIL: 't@t', GIT_COMMITTER_NAME: 't', GIT_COMMITTER_EMAIL: 't@t' };
const git = (cwd, ...a) => execFileSync('git', ['-C', cwd, ...a], { encoding: 'utf8', env: G }).trim();

const st = await stage();
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
const p = st.page;
// What the window's main process would do, for what this work added to window.agents: the app menu's 设置… (its
// listener kept to call), and 发一条试试; a folder picked is whatever the check puts in window.__folder.
await st.context.addInitScript(() => {
  const calls = window.__agentsCalls, a = window.agents;
  a.onSettings = cb => { window.__agentsSettings = cb; return () => {}; };
  a.notifyTest = (...x) => { calls.push(['notifyTest', ...x]); return Promise.resolve(true); };
  a.folder = (...x) => { calls.push(['folder', ...x]); return Promise.resolve(window.__folder ?? ''); };
});
// What the page sent that changes something, host by host.
const sent = [];
p.on('request', r => {
  const u = new URL(r.url());
  if (r.method() === 'GET' || !/^\/(import|sessions|settings|projects|doctor)/.test(u.pathname)) return;
  let body = null; try { body = r.postDataJSON(); } catch { /* none */ }
  sent.push({ m: r.method(), path: `${u.pathname}${u.search}`, port: u.port, body });
});
const calls = () => p.evaluate(() => window.__agentsCalls);
const text = async sel => (await p.locator(sel).first().innerText().catch(() => '')).replace(/\s+/g, ' ');
const has = async sel => await p.locator(sel).count() > 0;
const seen = async sel => p.locator(sel).first().isVisible().catch(() => false);
const waitFor = (what, fn, ms = 10000) => st.until(what, fn, ms);
const toast = () => text('.toast:not([hidden])');
// The title's ··· shows when the title is pointed at (the long exposure keeps it quiet otherwise).
const moreMenu = async () => { await p.hover('.m-head:not(.fr-rh) .h-main'); await p.click('.m-head:not(.fr-rh) [data-act="menu"][data-v="more"]'); };

// A session started in a terminal: the stand-in run directly in a folder, as `claude` would be, its first turn done.
async function terminal(home, cwd, say, ageMs = 0) {
  const sid = randomUUID(), config = path.join(home, '.claude');
  const c = spawn(process.execPath, [path.join(here, 'fake-claude.mjs'), '--session-id', sid, '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose'],
    { cwd, env: { PATH: process.env.PATH, HOME: home, CLAUDE_CONFIG_DIR: config }, stdio: ['pipe', 'pipe', 'ignore'] });
  let buf = '', gone = false;
  c.on('exit', () => { gone = true; });
  c.stdout.on('data', b => { buf += b; });
  c.stdin.write(`${JSON.stringify({ type: 'user', message: { role: 'user', content: say }, uuid: randomUUID(), parent_tool_use_id: null, session_id: sid })}\n`);
  await st.until(`terminal turn "${say}"`, () => buf.includes('"type":"result"') || gone, 15000);
  c.stdin.end();
  await st.until('terminal exit', () => gone, 5000);
  const file = path.join(config, 'projects', cwd.replace(/[^a-zA-Z0-9]/g, '-'), `${sid}.jsonl`);
  assert.ok(existsSync(file), `no transcript for ${say}`);
  if (ageMs) { const t = (Date.now() - ageMs) / 1000; utimesSync(file, t, t); }
  return { sid, file };
}
const touch = f => { const t = Date.now() / 1000; utimesSync(f, t, t); };

// ---------- a packaged host of this script's own: a Keychain that keeps, an Anthropic that checks, a daemon with a board ----------
async function packagedHost() {
  const tmp = path.join(st.tmp, 'packaged'), HOME = path.join(tmp, 'home'), BIN = path.join(tmp, 'bin'), root = path.join(tmp, 'root'), dir = path.join(root, 'agents');
  const site = path.join(HOME, 'Projects', 'site'), keychain = path.join(tmp, 'keychain.json');
  await mkdir(site, { recursive: true }); await mkdir(BIN, { recursive: true }); await mkdir(path.join(root, 'logs'), { recursive: true }); await mkdir(path.join(HOME, '.claude'), { recursive: true });
  await writeFile(path.join(site, 'README.md'), '# Site\n');
  git(path.dirname(site), 'init', '-q', '-b', 'main', site); git(site, 'add', '-A'); git(site, 'commit', '-q', '-m', 'init');
  symlinkSync(process.execPath, path.join(BIN, 'node'));
  await writeFile(path.join(BIN, 'sh'), '#!/bin/sh\nexec /bin/bash --noprofile --norc "$@"\n'); chmodSync(path.join(BIN, 'sh'), 0o755);
  await writeFile(path.join(BIN, 'codex'), `#!/bin/sh\nexec node "${path.join(here, 'fake-codex.mjs')}" "$@"\n`); chmodSync(path.join(BIN, 'codex'), 0o755);
  // macOS's security tool, as far as the host uses it: one generic password, kept in a file.
  await writeFile(path.join(tmp, 'security.mjs'), `import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
const f = ${JSON.stringify(keychain)}, [cmd] = process.argv.slice(2);
if (cmd === 'find-generic-password') { if (!existsSync(f)) process.exit(44); process.stdout.write(readFileSync(f, 'utf8') + '\\n'); }
else if (cmd === 'delete-generic-password') { if (!existsSync(f)) process.exit(44); rmSync(f); }
else if (cmd === '-i') { let s = ''; process.stdin.on('data', d => { s += d; }); process.stdin.on('end', () => { const hex = / -X ([0-9a-f]+)/.exec(s)?.[1]; if (!hex) process.exit(1); writeFileSync(f, Buffer.from(hex, 'hex').toString('utf8')); }); }
else process.exit(2);
`);
  await writeFile(path.join(BIN, 'security'), `#!/bin/sh\nexec node "${path.join(tmp, 'security.mjs')}" "$@"\n`); chmodSync(path.join(BIN, 'security'), 0o755);
  const good = `sk-ant-api03-${'a'.repeat(40)}WXYZ`;
  const api = http.createServer((q, r) => { const ok = q.url.startsWith('/v1/models') && q.headers['x-api-key'] === good; r.writeHead(ok ? 200 : 401, { 'Content-Type': 'application/json' }); r.end(ok ? '{"data":[]}' : '{"error":{"type":"authentication_error"}}'); });
  // The daemon: marks, health, the board of Claude Code sessions (ADR 0046) and the requests it holds (ADR 0049).
  const board = [], answered = [];
  const daemon = http.createServer((q, r) => {
    let b = ''; q.on('data', d => { b += d; }); q.on('end', () => {
      const send = (code, o) => { r.writeHead(code, { 'Content-Type': 'application/json' }); r.end(JSON.stringify(o)); };
      if (q.url === '/inherent/agent-marks') return send(200, { marks: {} });
      if (q.url === '/inherent/claude-sessions') return send(200, { sessions: board });
      const m = /^\/inherent\/claude-requests\/([^/?]+)/.exec(q.url);
      if (m && q.method === 'POST') {
        const row = board.find(x => x.request?.id === decodeURIComponent(m[1]));
        if (!row) return send(404, { error: 'not held' });
        answered.push({ id: decodeURIComponent(m[1]), auth: q.headers.authorization, ...JSON.parse(b || '{}') });
        row.request = null; row.phase = 'working';
        return send(200, { ok: true });
      }
      send(200, {});
    });
  });
  await new Promise(r => api.listen(0, '127.0.0.1', r)); await new Promise(r => daemon.listen(0, '127.0.0.1', r));
  await writeFile(path.join(root, 'plugin-access.json'), JSON.stringify({ token: 'daemon-packaged' }));
  const port = await new Promise(r => { const s = http.createServer().listen(0, '127.0.0.1', () => { const n = s.address().port; s.close(() => r(n)); }); });
  const env = { PATH: `${BIN}:/usr/bin:/bin`, HOME, SHELL: path.join(BIN, 'sh'), USER: os.userInfo().username, LANG: 'en_US.UTF-8', TMPDIR: os.tmpdir(), ELECTRON_RUN_AS_NODE: '1',
    JARVIS_RUNTIME_ROOT: root, JARVIS_AGENTS_DIR: dir, JARVIS_AGENTS_PORT: String(port), JARVIS_INHERENT_BRIDGE_PORT: String(daemon.address().port), JARVIS_AGENTS_PACKAGED: '1',
    JARVIS_AGENTS_CLAUDE: path.join(here, 'fake-claude.mjs'), JARVIS_AGENTS_SECURITY: path.join(BIN, 'security'), CLAUDE_CONFIG_DIR: path.join(HOME, '.claude'),
    ANTHROPIC_BASE_URL: `http://127.0.0.1:${api.address().port}`, FAKE_CLAUDE_LOG: path.join(tmp, 'claude.log'), FAKE_CODEX_LOG: path.join(tmp, 'codex.log'), FAKE_CODEX_SIGNED_OUT: '1' };
  const proc = spawn(path.join(app, 'node_modules', '.bin', 'electron'), [path.join(app, 'dist-electron', 'agents', 'host.js')], { stdio: ['ignore', 'pipe', 'pipe'], env });
  let out = ''; proc.stdout.on('data', b => { out += b; }); proc.stderr.on('data', b => { out += b; });
  for (let i = 0; i < 100 && !existsSync(path.join(dir, 'host-key')); i++) await sleep(100);
  const key = readFileSync(path.join(dir, 'host-key'), 'utf8'), API = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i++) { if ((await fetch(`${API}/health`, { headers: { Authorization: `Bearer ${key}` } }).catch(() => null))?.ok) break; await sleep(150); }
  const call = async (route, body, method = body === undefined ? 'GET' : 'POST') => {
    const r = await fetch(API + route, { method, headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: r.status, ...await r.json().catch(() => ({})) };
  };
  await st.context.route(`${API}/**`, route => route.continue({ headers: { ...route.request().headers(), authorization: `Bearer ${key}` } }));
  return { HOME, site, keychain, good, board, answered, port, call, log: () => out,
    async close() {
      const gone = new Promise(r => { if (proc.exitCode !== null) r(); else proc.on('exit', r); });
      proc.kill(); try { execFileSync('pkill', ['-f', `keeper.js ${dir}`]); } catch { /* none left */ }
      await Promise.race([gone, sleep(3000)]); api.close(); daemon.close();
    } };
}

let P = null;
try {
  // ---------- the window's own sessions, and three started in a terminal ----------
  const own = await st.session('hello there');
  const own2 = await st.session('second one');
  const tOld = await terminal(st.HOME, st.repo, 'PLAN the release notes', 3600e3);
  const tMid = await terminal(st.HOME, st.repo, 'tidy the readme', 1800e3);
  const tNew = await terminal(st.HOME, st.repo, 'look at the tests');
  const listed = (await st.call('/import')).sessions ?? [];
  const byId = id => listed.find(o => o.id === id);
  check('GET /import lists the sessions started in a terminal, newest first, the one that moved in the last two minutes marked recent',
    byId(tOld.sid) && byId(tMid.sid) && byId(tNew.sid)?.recent === true && !byId(tOld.sid).recent && !byId(own) && listed.indexOf(byId(tNew.sid)) < listed.indexOf(byId(tOld.sid)), listed);
  const read = await st.call(`/import/read?agent=claude&id=${tOld.sid}&cwd=${encodeURIComponent(st.repo)}`);
  check('GET /import/read reads one from its transcript without taking it in', read.status === 200 && read.items.some(i => i.k === 'you' && i.text === 'PLAN the release notes')
    && read.items.some(i => i.k === 'it') && !st.row(tOld.sid) && (await st.call('/import')).sessions.some(o => o.id === tOld.sid), read);
  const readOwn = await st.call(`/import/read?agent=claude&id=${own}&cwd=${encodeURIComponent(st.repo)}`);
  check('GET /import/read refuses a session the window already has (409)', readOwn.status === 409, readOwn);

  // ---------- the sky's last group ----------
  await st.open(own);
  await p.click('.bw-pull');
  await waitFor('the terminal line in the sky', async () => /终端里还有 3 个/.test(await text('.bw-more .fr-tcount')));
  check('the long exposure\'s sky ends with 「终端里还有 3 个」 under the window\'s own rows', await p.evaluate(() => {
    const more = document.querySelector('.bw-more'), last = [...document.querySelectorAll('.bw-rows .bw-row')].pop();
    return !!more && !!last && more.getBoundingClientRect().top >= last.getBoundingClientRect().bottom - 1 && Number(more.dataset.h) > 0;
  }));
  await p.click('.bw-more .fr-tcount');
  await waitFor('three terminal rows', async () => await p.locator('.bw-more .fr-trow').count() === 3);
  await p.waitForTimeout(400);
  const trows = await p.locator('.bw-more .fr-trow').evaluateAll(els => els.map(e => ({ id: e.dataset.fr, tag: e.querySelector('.fr-tg')?.textContent, left: e.getBoundingClientRect().left })));
  const ownLeft = await p.locator('.bw-rows .bw-row').first().evaluate(e => e.getBoundingClientRect().left);
  check('opened, the group lists each one marked 终端, newest first, its names in line with the sky\'s own',
    trows.length === 3 && trows.every(r => r.tag === '终端') && trows[0].id === tNew.sid && Math.abs(trows[0].left - ownLeft) < 2 && /收起终端里的 3 个/.test(await text('.bw-more .fr-tcount')), { trows, ownLeft });
  await st.shot('fr-term');

  // ---------- read-only, reached from the keyboard: ↓ goes on past the sky's own rows into the terminal's ----------
  for (let i = 0; i < 20 && !(await has(`.bw-more .fr-trow.sel[data-fr="${tOld.sid}"]`)); i++) { await p.keyboard.press('ArrowDown'); await p.waitForTimeout(60); }
  check('↓ reaches the terminal\'s rows at the foot of the sky', await has(`.bw-more .fr-trow.sel[data-fr="${tOld.sid}"]`) && await has('#win.sky-on'));
  await p.keyboard.press('Enter');
  await waitFor('the read-only view', async () => await has('#win.fr-ro-on') && /PLAN the release notes/.test(await text('.fr-rov .c-items')));
  await p.waitForTimeout(400);
  const head = await text('.fr-rh');
  check('a terminal session opens read-only: its conversation, its header marked 在终端里 · 只能看, a bar in place of the composer',
    !(await has('#win.sky-on')) && /在终端里 · 只能看/.test(head) && await seen('.fr-rob') && /这里只能看/.test(await text('.fr-rob'))
    && !(await p.locator('.pn.chat').evaluate(e => getComputedStyle(e).visibility === 'visible')) && await seen('.fr-rov .c-items .you'), head);
  await st.shot('fr-term-ro');
  const before = await p.inputValue('#msg');
  await p.keyboard.press('x');
  await waitFor('the nudge', async () => /接手以后才能在这里说/.test(await toast()));
  check('typing there says it lives in the terminal and writes nothing into the window\'s composer', await p.inputValue('#msg') === before && await has('#win.fr-ro-on'));
  await p.keyboard.press('Escape');
  check('esc there reaches nothing behind it: the view stays and the window\'s own session is not interrupted', await has('#win.fr-ro-on') && st.row(own).st === 'done' && !st.row(own).stopped);

  // ---------- 接手 asks once ----------
  await p.click('.fr-rob [data-act="fr-take"]');
  await waitFor('the take question', async () => await seen('.fr-tkc') && /自己的登录/.test(await text('.fr-tkc')));
  const q1 = await text('.fr-tkc');
  check('接手 asks once, inside the window: what changes, 算了 esc · 接手 ⏎', /接手「.+」？/.test(q1) && /终端里那个就别再用了/.test(q1) && /算了/.test(q1) && await has('.fr-tkc [data-act="fr-take-yes"]'), q1);
  await p.evaluate(() => { document.querySelector('.toast').hidden = true; });
  await st.shot('fr-take');
  await p.keyboard.press('Escape');
  await waitFor('the question gone', async () => !(await has('.fr-tkc')) && await seen('.fr-rob'));
  check('esc takes the question back, still read-only', await has('#win.fr-ro-on'));
  await p.click('.fr-rob [data-act="fr-take"]');
  await waitFor('the take question again', () => seen('.fr-tkc'));
  await p.evaluate(() => document.activeElement?.blur());
  await p.keyboard.press('Enter');
  await waitFor('taken in', () => !!st.row(tOld.sid));
  await waitFor('the window on it', async () => !(await has('#win.fr-ro-on')) && /PLAN the release notes/.test(await text('.host .conv')));
  const take1 = sent.find(x => x.m === 'POST' && x.path === '/import' && x.body?.id === tOld.sid);
  check('⏎ takes it in with POST /import and the window opens on it, in its list', take1?.body?.force === false && st.row(tOld.sid).summary !== '' && /从终端接手/.test(await text('.host .conv'))
    && !(await st.call('/import')).sessions.some(o => o.id === tOld.sid), take1);

  // ---------- one that moved a moment ago, found with /resume ----------
  const refuse = await st.call('/import', { agent: 'claude', id: tNew.sid, cwd: st.repo });
  check('POST /import answers 409 with need \'force\' for one that moved in the last two minutes', refuse.status === 409 && refuse.need === 'force', refuse);
  touch(tNew.file);
  await p.click('#msg');
  await p.keyboard.type('/resume');
  await p.waitForTimeout(700);
  for (let i = 0; i < 3 && (await p.inputValue('#msg')).trim(); i++) { await p.keyboard.press('Enter'); await p.waitForTimeout(500); }
  await waitFor('the sky open on the terminal group', async () => await has('#win.sky-on') && /收起终端里的 2 个/.test(await text('.bw-more .fr-tcount')));
  check('/resume opens the sky on the terminal group', await p.locator('.bw-more .fr-trow').count() === 2);
  await p.click(`.bw-more .fr-trow[data-fr="${tNew.sid}"]`);
  await waitFor('read-only again', () => has('#win.fr-ro-on'));
  await p.hover('.fr-rh .h-main');
  await p.click('.fr-rh [data-act="fr-take"]');
  await waitFor('the take question with the warm line', async () => /两分钟内还在终端里动过/.test(await text('.fr-tkc')));
  await st.shot('fr-take-recent');
  await p.click('.fr-tkc [data-act="fr-take-yes"]');
  await waitFor('the recent one taken in', () => !!st.row(tNew.sid));
  const take2 = sent.find(x => x.m === 'POST' && x.path === '/import' && x.body?.id === tNew.sid);
  check('for one that may still be open the question says so, and 接手 is the second, sure press (force)', take2?.body?.force === true, take2);

  // ---------- delete: a branch with commits not merged asks first ----------
  const A = await st.session('EDIT README.md', { tree: true });
  await waitFor('A dirty', () => st.row(A)?.dirty?.n > 0);
  git(st.row(A).cwd, 'commit', '-qam', 'work in the worktree');
  await st.send(A, 'hello again');
  await waitFor('A ahead', () => st.row(A)?.dirty?.ahead === 1);
  await st.open(A);
  await moreMenu();
  await waitFor('the ··· menu', () => seen('.pop.on [data-act="fr-del"]'));
  check('删除 is in the title\'s ··· menu', /删除/.test(await text('.pop.on [data-act="fr-del"]')));
  await p.click('.pop.on [data-act="fr-del"]');
  await waitFor('the delete question', () => seen('.pop.on.fr-ask'));
  const askA = await text('.pop.fr-ask');
  check('a session whose branch has commits not merged asks first, inside the window: 删掉 · 先留着, 先留着 focused',
    askA.includes(`删掉「${st.row(A).title}」？`) && askA.includes(`${st.row(A).branch} 上还有没合进去的提交。删之前会先备份。`) && await p.evaluate(() => document.activeElement?.dataset.act === 'fr-del-no'), askA);
  await st.shot('fr-del');
  const branchA = st.row(A).branch;
  await p.click('.pop.fr-ask [data-act="fr-del-yes"]');
  await waitFor('A gone', () => !st.row(A));
  await waitFor('the kept toast', async () => /备份在 refs\/startrail\/trash/.test(await toast()));
  check('删掉 deletes it with ?force=1 and the toast says where its work is kept', sent.some(x => x.m === 'DELETE' && x.path === `/sessions/${A}?force=1`)
    && git(st.repo, 'for-each-ref', '--format=%(refname)', 'refs/startrail/trash').length > 0 && !git(st.repo, 'branch', '--list', branchA), await toast());

  // ---------- a row's right click; uncommitted changes ask too, and 先留着 keeps it ----------
  const B = await st.session('EDIT notes.txt', { tree: true });
  await waitFor('B dirty', () => st.row(B)?.dirty?.n > 0);
  await st.open(own);
  await p.click('.bw-pull');
  await waitFor('B in the sky', () => has(`.bw-rows [data-session="${B}"]`));
  await p.click(`.bw-rows [data-session="${B}"]`, { button: 'right' });
  await waitFor('the row menu', () => seen('.pop.on [data-act="fr-del"]'));
  const rowMenu = await text('.pop.on');
  check('a sky row\'s right click has its lines and 删除', /置顶/.test(rowMenu) && /归档/.test(rowMenu) && /删除/.test(rowMenu), rowMenu);
  await p.click('.pop.on [data-act="fr-del"]');
  await waitFor('the second question', () => seen('.pop.on.fr-ask'));
  check('uncommitted changes in its worktree ask the same way', /worktree 里还有没提交的改动。删之前会先备份。/.test(await text('.pop.fr-ask')));
  await p.click('.pop.fr-ask [data-act="fr-del-no"]');
  await p.waitForTimeout(600);
  check('先留着 keeps it, nothing sent', !(await seen('.pop.on')) && st.row(B) && !st.row(B).archived && !sent.some(x => x.m === 'DELETE' && x.path.startsWith(`/sessions/${B}`)));
  await p.keyboard.press('Escape');

  // ---------- a clean one goes at once, and 撤销 takes it back ----------
  const D = await st.session('clean D');
  await st.open(D);
  await moreMenu();
  await p.click('.pop.on [data-act="fr-del"]');
  await waitFor('the undo toast', async () => await seen('.toast .fr-undo') && /删了「/.test(await toast()));
  await waitFor('D archived meanwhile', () => st.row(D)?.archived === true);
  check('a clean one goes at once (no question), the window moves on, and the toast has 撤销', !(await has('.pop.on.fr-ask')) && !(await has(`.bw-rows [data-session="${D}"]`))
    && await p.evaluate(id => !document.querySelector('.m-head .h-t')?.textContent?.includes(id), 'clean D'));
  await st.shot('fr-del-undo');
  await p.click('.toast .fr-undo');
  await waitFor('D back', () => st.row(D)?.archived === false);
  await p.waitForTimeout(5600);
  check('撤销 inside the hold takes it back: nothing deleted after the hold', !!st.row(D) && !sent.some(x => x.m === 'DELETE' && x.path.startsWith(`/sessions/${D}`)) && /clean D/.test(await text('.m-head .h-t')));

  // ---------- the hold ends: the host deletes it; one git would lose something after all asks then ----------
  const C = await st.session('clean C', { tree: true });
  // The host measures what a session would land when its turn ends; the change is made after that, behind its back.
  await sleep(1500);
  assert.ok(st.row(C) && !st.row(C).dirty, 'C has nothing to land yet');
  writeFileSync(path.join(st.row(C).cwd, 'README.md'), '# changed behind the window\n');
  await st.open(C);
  await moreMenu();
  await p.click('.pop.on [data-act="fr-del"]');
  await waitFor('C held', async () => await seen('.toast .fr-undo'));
  await waitFor('the question after the hold', () => seen('.pop.on.fr-ask'), 9000);
  check('a delete the host refuses at the end (409, need \'force\': call() carries need) brings the question back, the session kept',
    /没提交的改动/.test(await text('.pop.fr-ask')) && sent.some(x => x.m === 'DELETE' && x.path === `/sessions/${C}`) && !!st.row(C) && st.row(C).archived === false);
  await p.click('.pop.fr-ask [data-act="fr-del-no"]');

  // ---------- the hold ending in a delete, from a sky row's right click ----------
  await st.open(D);
  if (!(await has('#win.sky-on'))) await p.click('.bw-pull');
  await waitFor('D in the sky', () => has(`.bw-rows [data-session="${D}"]`));
  await p.click(`.bw-rows [data-session="${D}"]`, { button: 'right' });
  await waitFor('the row menu', () => seen('.pop.on [data-act="fr-del"]'));
  await p.click('.pop.on [data-act="fr-del"]');
  await waitFor('D deleted after the hold', () => !st.row(D), 9000);
  check('when the hold ends the host deletes it (DELETE, no force)', sent.some(x => x.m === 'DELETE' && x.path === `/sessions/${D}`));
  await p.keyboard.press('Escape');

  // ---------- settings: ⌘, and the app menu, on the dev host ----------
  await st.open(own);
  await p.keyboard.press('Control+,');
  await waitFor('the sheet', () => seen('.fr-set'));
  const nav = await text('.fr-nav');
  check('⌘, opens settings as a sheet inside the window: 钥匙 · 体检 · 通知 · 项目, no held page', await has('#win.fr-modal') && /钥匙.*体检.*通知.*项目/.test(nav) && !/打开方式|仓库|语言|手机|定时/.test(nav), nav);
  await waitFor('the dev key card', async () => /开发版/.test(await text('.fr-set [data-card="claude"]')));
  check('钥匙 on the dev build: it uses this Mac\'s own Claude Code login and keeps no key', !(await has('.fr-set [data-f="key"]')) && /开发版不存 key/.test(await text('.fr-set')));
  await st.shot('fr-key-dev');
  await p.keyboard.press('Escape');
  await waitFor('the sheet closed', async () => !(await has('#win.fr-modal')));
  await p.evaluate(() => window.__agentsSettings());
  await waitFor('the sheet from the menu', () => seen('.fr-set'));
  check('the app menu\'s 设置… (the window told over agents-settings) opens the same sheet', await has('#win.fr-modal'));

  // 体检
  await p.click('.fr-nav [data-p="doc"]');
  await waitFor('the check-up rows', async () => await p.locator('.fr-dr').count() === 4 && !(await has('.fr-dr.q')));
  const doc = await p.locator('.fr-dr').evaluateAll(els => els.map(e => [e.dataset.k, e.classList.contains('ok'), e.textContent]));
  check('体检 has a row each for Claude Code, Codex, git and the host\'s daemon, read from GET /doctor', doc.map(d => d[0]).join() === 'claude,codex,git,daemon' && doc.every(d => d[1]) && /owner@example\.com/.test(doc[1][2]), doc);
  await st.shot('fr-check-dev');

  // 通知
  await p.click('.fr-nav [data-p="notify"]');
  await waitFor('the switches', () => has('.fr-sw[data-k="done"]'));
  const sw = await p.locator('.fr-sw').evaluateAll(els => Object.fromEntries(els.map(e => [e.dataset.k, e.getAttribute('aria-checked')])));
  check('通知: 做完了 off by default, waiting and errors on, the notch on, no Dock row', sw.done === 'false' && sw.wait === 'true' && sw.err === 'true' && sw.notch === 'true' && !/Dock/.test(await text('.fr-set')), sw);
  await p.click('.fr-sw[data-k="done"]');
  await waitFor('done saved', async () => (await st.call('/settings')).settings?.notify?.done === true);
  await p.click('.fr-sw[data-k="done"]');
  await waitFor('done off again', async () => (await st.call('/settings')).settings?.notify?.done === false && await has('.fr-sw[data-k="done"][aria-checked="false"]'));
  await p.click('.fr-sw[data-k="notch"]');
  await waitFor('notch off', async () => (await st.call('/settings')).settings?.notify?.notch === false && await has('.fr-sw[data-k="notch"][aria-checked="false"]'));
  await p.click('.fr-sw[data-k="notch"]');
  await waitFor('notch on again', async () => (await st.call('/settings')).settings?.notify?.notch === undefined && await has('.fr-sw[data-k="notch"][aria-checked="true"]'));
  check('the notch switch: on unless turned off, the rest kept as they were', (await st.call('/settings')).settings?.notify?.done === false);
  check('a switch is kept at once with POST /settings', sent.filter(x => x.path === '/settings' && x.body?.notify).length >= 2 && await has('.fr-sw[data-k="done"][aria-checked="false"]'));
  await p.click('.fr-set [data-act="fr-test"]');
  await waitFor('the test note', async () => (await calls()).some(c => c[0] === 'notifyTest'));
  const note = (await calls()).find(c => c[0] === 'notifyTest');
  check('「发一条试试」 asks the window for one notification (agents-notify-test) and says where to look', note.length >= 4 && typeof note[1] === 'string' && /发了一条/.test(await toast()), note);
  await st.shot('fr-notify');

  // 项目
  const extra = path.join(st.tmp, 'elsewhere', 'notes-app');
  await mkdir(extra, { recursive: true });
  await p.click('.fr-nav [data-p="proj"]');
  await waitFor('the projects', () => has('.fr-pj'));
  const groups = await text('.fr-pl0');
  check('项目: recent first, then ~/Projects', /最近用过.*app.*/.test(groups) && groups.indexOf('最近用过') < (groups.indexOf('~/Projects 里的') >>> 0), groups);
  await p.evaluate(f => { window.__folder = f; }, extra);
  await p.click('.fr-set [data-act="fr-add"]');
  await waitFor('added', async () => /你加的/.test(await text('.fr-pl0')) && await has(`.fr-pj[data-path="${extra}"]`));
  check('添加文件夹 adds one (POST /projects), listed under 你加的 with ✕', (await st.call('/projects')).projects.includes(extra) && await has(`.fr-pj[data-path="${extra}"] [data-act="fr-unadd"]`));
  await st.shot('fr-proj');
  await p.hover(`.fr-pj[data-path="${extra}"]`);
  await p.click(`.fr-pj[data-path="${extra}"] [data-act="fr-unadd"]`);
  await waitFor('removed', async () => !(await has(`.fr-pj[data-path="${extra}"]`)));
  check('an added one can be removed (DELETE /projects), the folder itself left alone', !(await st.call('/projects')).projects.includes(extra) && existsSync(extra));
  await p.keyboard.press('Escape');
  await waitFor('closed', async () => !(await has('#win.fr-modal')));

  // /doctor opens the check-up
  await p.click('#msg');
  await p.keyboard.type('/doctor');
  await p.waitForTimeout(700);
  for (let i = 0; i < 3 && (await p.inputValue('#msg')).trim(); i++) { await p.keyboard.press('Enter'); await p.waitForTimeout(500); }
  await waitFor('the sheet on 体检', async () => await seen('.fr-set') && /体检/.test(await text('.fr-ph h3')));
  check('/doctor (and /status, /login) opens settings on 体检', true);
  await p.keyboard.press('Escape');

  // ---------- the host's own changes: 做完了 stays off unless it is sent on ----------
  const n1 = await st.call('/settings', { notify: { wait: true, err: true } });
  const n2 = await st.call('/settings', { notify: { done: 'yes', wait: false } });
  check('POST /settings keeps 做完了 off unless it is sent as true', n1.settings.notify.done === false && n2.settings.notify.done === false && n2.settings.notify.wait === false, [n1.settings.notify, n2.settings.notify]);
  const win = readFileSync(path.join(app, 'dist-electron', 'agentsWindow.js'), 'utf8'), pre = readFileSync(path.join(app, 'dist-electron', 'preload.cjs'), 'utf8');
  check('the built window: no 做完了 notification until it is switched on, a 设置… ⌘, item that tells the page, and a test notification',
    !/done: true, wait: true, err: true/.test(win) && (win.match(/done: false, wait: true, err: true/g) ?? []).length === 2 && /label: '设置…', accelerator: 'CommandOrControl\+,'/.test(win)
    && /send\('agents-settings'\)/.test(win) && /ipcMain\.handle\('agents-notify-test'/.test(win) && /agents-settings/.test(pre) && /agents-notify-test/.test(pre));

  // ---------- the packaged host ----------
  P = await packagedHost();
  const tAsk = await terminal(P.HOME, P.site, 'run the tests', 600e3);
  P.board.push({ session_id: tAsk.sid, phase: 'needs_input', cwd: P.site, updated_ms: Date.now(),
    request: { id: 'held-1', tool: 'Bash', input: { command: 'npm test', description: 'Run the tests' }, cwd: P.site, always: 'Always allow npm test' } });
  const live = await P.call('/import/live');
  check('GET /import/live reads the daemon\'s board: the terminal session waiting, with the request it stopped on as the window\'s own card',
    live.live?.length === 1 && live.live[0].id === tAsk.sid && live.live[0].st === 'wait' && live.live[0].req?.tool === 'Bash' && live.live[0].req.cmd === 'npm test' && live.live[0].req.always === '以后都允许', live);
  const notHeld = await P.call('/import/answer', { req: 'nobody', decision: 'allow' });
  check('POST /import/answer for a request no longer held says so (409)', notHeld.status === 409 && /不在等了/.test(notHeld.error), notHeld);
  const a0 = await P.call('/settings');
  check('the packaged host without a key is not ready', a0.auth.packaged === true && a0.auth.ready === false, a0.auth);

  // the first run
  const web = new URL(p.url()).origin, open2 = async () => {
    await p.goto(`${web}/agents.html?port=${P.port}`);
    await p.waitForFunction(() => !document.querySelector('#win')?.classList.contains('booting'), null, { timeout: 15000 });
    await p.waitForTimeout(600);
  };
  await p.evaluate(() => localStorage.removeItem('agents.first'));
  await open2();
  await waitFor('the first run', () => seen('.fr-first'));
  check('a host that is not ready opens on the first run, inside the window: step 1 of 3, 钥匙', await has('#win.fr-modal') && /第 1 步/.test(await text('.fr-first h3')) && await seen('.fr-first [data-f="key"]'));
  await p.click('.fr-first [data-act="fr-fskip"]');
  await waitFor('skipped', async () => !(await seen('.fr-first')));
  check('every step can be skipped: 跳过 lands in the normal window and says where settings are', /跳过了/.test(await toast()) && await p.evaluate(() => localStorage.getItem('agents.first')) === 'done');
  await p.evaluate(() => localStorage.removeItem('agents.first'));
  await open2();
  await waitFor('the first run again', () => seen('.fr-first [data-f="key"]'));
  await st.shot('fr-first');
  await p.fill('.fr-first [data-f="key"]', `sk-ant-api03-${'b'.repeat(40)}0000`);
  await p.keyboard.press('Enter');
  await waitFor('refused', async () => /Anthropic 说这个 key 不对/.test(await text('.fr-first [data-card="claude"]')));
  check('钥匙: a key Anthropic refuses is not kept, and it says so', !existsSync(P.keychain));
  await p.fill('.fr-first [data-f="key"]', P.good);
  await p.click('.fr-first [data-act="fr-verify"]');
  await waitFor('kept', async () => /存好了 · …WXYZ/.test(await text('.fr-first [data-card="claude"]')));
  const kept = JSON.parse(readFileSync(P.keychain, 'utf8'));
  const shown = await p.evaluate(() => document.querySelector('.fr-first')?.innerHTML ?? '');
  check('钥匙: a pasted key is checked with Anthropic first, kept in the Keychain, only its last four shown', kept.ANTHROPIC_API_KEY === P.good && !shown.includes(P.good.slice(0, 20))
    && (await P.call('/settings')).auth.ready === true && (await P.call('/settings')).auth.hint === '…WXYZ');
  await st.shot('fr-key');
  check('Codex\'s card: not signed in, with its ChatGPT sign-in', /没登录/.test(await text('.fr-first [data-card="codex"]')));
  await p.click('.fr-first [data-act="fr-fnext"]');
  await waitFor('step 2', async () => /第 2 步/.test(await text('.fr-first h3')) && await p.locator('.fr-first .fr-dr').count() === 4 && !(await has('.fr-first .fr-dr.q')));
  const bad = await p.locator('.fr-first .fr-dr.bad').evaluateAll(els => els.map(e => [e.dataset.k, e.querySelector('[data-act="fr-fix"]')?.textContent]));
  check('体检 marks what is missing ✕ with a button to fix it', bad.length === 1 && bad[0][0] === 'codex' && bad[0][1] === '登录', bad);
  await st.shot('fr-check');
  await p.click('.fr-first .fr-dr.bad [data-act="fr-fix"]');
  await waitFor('Codex signed in', async () => /owner@example\.com/.test(await text('.fr-first .fr-dr[data-k="codex"]')) && await has('.fr-first .fr-dr[data-k="codex"].ok'), 12000);
  check('the fix signs Codex in: its page opened in the browser (POST /doctor/login), the row read again until ✓', (await calls()).some(c => c[0] === 'openUrl' && c[1] === 'https://auth.openai.com/fake')
    && sent.some(x => x.path === '/doctor/login' && x.body?.agent === 'codex' && x.port === String(P.port)) && /Codex 登好了/.test(await toast()));
  await p.click('.fr-first [data-act="fr-fnext"]');
  await waitFor('step 3', async () => /第 3 步/.test(await text('.fr-first h3')) && await has(`.fr-first [data-act="fr-fproj"][data-path="${P.site}"]`));
  check('项目 comes last; 开始 waits for a folder', await has('.fr-first [data-act="fr-fgo"][disabled]'));
  await p.click(`.fr-first [data-act="fr-fproj"][data-path="${P.site}"]`);
  await st.shot('fr-first-proj');
  await p.click('.fr-first [data-act="fr-fgo"]');
  const siteName = P.site.split('/').filter(Boolean).pop();
  await waitFor('the slip on it', async () => !(await seen('.fr-first')) && await seen('.slip') && (await text('.slip')).includes(siteName));
  check('开始 ends in the normal window, the slip dropped on the folder picked, ready to write', !(await has('#win.fr-modal')) && await p.evaluate(() => !!document.activeElement?.closest('.slip')));
  await p.keyboard.press('Escape');

  // a request a terminal session stopped on, answered here
  await p.keyboard.press('Alt+ArrowUp');
  await waitFor('the terminal rows in the sky', () => seen(`.bw-more [data-fr="${tAsk.sid}"]`));
  check('with none of its own sessions, the sky still opens on 新会话 and the terminal ones, the asking one saying 等你批', /等你批/.test(await text(`.bw-more [data-fr="${tAsk.sid}"]`)));
  await p.click(`.bw-more [data-fr="${tAsk.sid}"]`);
  await waitFor('the request card', async () => await seen('.fr-rq .req') && /npm test/.test(await text('.fr-rq .req')));
  const card = await text('.fr-rq');
  check('opened, it shows the request it stopped on, as the window\'s own card, and that the terminal waits too', /要你批准 · 跑一条命令/.test(card) && /以后都允许/.test(card) && /终端里也在等，哪边先批都算/.test(card)
    && /在终端里等你批/.test(await text('.fr-rob')), card);
  await st.shot('fr-term-ask');
  await p.evaluate(() => document.activeElement?.blur());
  await p.keyboard.press('Enter');
  await waitFor('answered', () => P.answered.length === 1);
  check('⏎ answers it: POST /import/answer, relayed to the daemon that holds it (POST /inherent/claude-requests/{id})', P.answered[0].id === 'held-1' && P.answered[0].decision === 'allow'
    && P.answered[0].auth === 'Bearer daemon-packaged' && sent.some(x => x.path === '/import/answer' && x.body?.req === 'held-1'), P.answered);
  await waitFor('the card gone', async () => !(await has('.fr-rq .req')) && /在终端里跑着/.test(await text('.fr-rob')), 8000);
  check('answered, the card leaves and the view says it runs on in the terminal', /终端那边接着跑/.test(await toast()));

  // 接手 before a key: the packaged question sends you to 钥匙 first
  await p.keyboard.press('Control+,');
  await waitFor('the sheet', () => seen('.fr-set [data-act="fr-forget"]'));
  await p.click('.fr-set [data-act="fr-forget"]');
  await waitFor('forgotten', async () => (await P.call('/settings')).auth.ready === false);
  check('删掉 forgets the key: gone from the Keychain, the packaged host not ready again', !JSON.parse(existsSync(P.keychain) ? readFileSync(P.keychain, 'utf8') : '{}').ANTHROPIC_API_KEY);
  await p.keyboard.press('Escape');
  await waitFor('closed', async () => !(await has('#win.fr-modal')));
  await p.click('.fr-rob [data-act="fr-take"]');
  await waitFor('the take question without a key', async () => /还开不了/.test(await text('.fr-tkc')));
  await p.click('.fr-tkc [data-act="fr-take-key"]');
  await waitFor('the sheet on 钥匙', async () => await seen('.fr-set') && /钥匙/.test(await text('.fr-ph h3')));
  check('接手 with no key yet says so and opens 钥匙 instead of taking it in', !sent.some(x => x.path === '/import' && x.port === String(P.port)));
  await p.keyboard.press('Escape');

  check('no errors on the page', !st.errors.length, st.errors);
  console.log(`\n${checks.length} checks passed`);
} catch (e) {
  console.error(e, '\n--- host ---\n', st.log().slice(-3000), P ? `\n--- packaged host ---\n${P.log().slice(-3000)}` : '');
  if (st.shots) await st.shot('failed').catch(() => {});
  process.exitCode = 1;
} finally {
  await P?.close();
  // The stage removes its folder right after killing its host, which may still be writing there: once more after it.
  await st.close().catch(async () => { await sleep(1000); await rm(st.tmp, { recursive: true, force: true }); });
}

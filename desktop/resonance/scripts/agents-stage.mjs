// Run after npm run build. A stage for checking Startrail's page itself: a real agent host on stand-in agents
// (scripts/fake-claude.mjs, scripts/fake-codex.mjs), the built page served from dist/, and Chromium showing it the way
// the window does: the host key rides on every request the page makes (Electron adds it in the window), and
// window.agents is a stand-in that records what the page asked of the window. Nothing here reaches Anthropic or
// OpenAI or spends anything, and nothing of this machine's own sessions, Keychain or Claude Code login is touched.
//
//   const st = await stage();                       // host, page server, Chromium
//   const id = await st.session('hello PLAN');     // a Claude session, its first turn done
//   await st.open(id); await st.shot('first');      // the page on that session; a screenshot in st.shots
//   await st.close();
import { execFileSync, spawn } from 'node:child_process';
import { chmodSync, existsSync, readFileSync, realpathSync, symlinkSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url)), app = path.join(here, '..');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const freePort = () => new Promise(r => { const s = http.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => r(p)); }); });
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.json': 'application/json', '.webp': 'image/webp' };

export async function stage({ viewport = { width: 1280, height: 820 }, shots = process.env.SHOTS, headless = true } = {}) {
  if (!existsSync(path.join(app, 'dist', 'agents.html')) || !existsSync(path.join(app, 'dist-electron', 'agents', 'host.js'))) throw new Error('run npm run build first');
  const tmp = realpathSync(await mkdtemp(path.join(os.tmpdir(), 'jarvis-agents-stage-')));
  const HOME = path.join(tmp, 'home'), CONFIG = path.join(HOME, '.claude'), BIN = path.join(tmp, 'bin');
  const G = { ...process.env, GIT_AUTHOR_NAME: 't', GIT_AUTHOR_EMAIL: 't@t', GIT_COMMITTER_NAME: 't', GIT_COMMITTER_EMAIL: 't@t' };
  const git = (cwd, ...a) => execFileSync('git', ['-C', cwd, ...a], { encoding: 'utf8', env: G }).trim();

  // ---------- a home with a repository in ~/Projects, and the stand-ins on a PATH of their own ----------
  const repo = path.join(HOME, 'Projects', 'app');
  await mkdir(path.join(repo, 'src'), { recursive: true }); await mkdir(BIN, { recursive: true }); await mkdir(CONFIG, { recursive: true });
  await writeFile(path.join(repo, 'README.md'), '# App\n\nA readme with a list:\n\n- one\n- two\n');
  await writeFile(path.join(repo, 'src', 'a.ts'), 'export const a = 1;\nexport const b = 2;\n');
  await writeFile(path.join(repo, 'notes.txt'), 'line one\nline two\n');
  await writeFile(path.join(repo, '.gitignore'), '.claude/\n');
  git(path.dirname(repo), 'init', '-q', '-b', 'main', repo);
  git(repo, 'add', '-A'); git(repo, 'commit', '-q', '-m', 'init');
  symlinkSync(process.execPath, path.join(BIN, 'node'));
  await writeFile(path.join(BIN, 'sh'), '#!/bin/sh\nexec /bin/bash --noprofile --norc "$@"\n'); chmodSync(path.join(BIN, 'sh'), 0o755);
  await writeFile(path.join(BIN, 'codex'), `#!/bin/sh\nexec node "${path.join(here, 'fake-codex.mjs')}" "$@"\n`); chmodSync(path.join(BIN, 'codex'), 0o755);
  // macOS's security tool: nothing kept; the dev build reads no key anyway.
  await writeFile(path.join(BIN, 'security'), '#!/bin/sh\nexit 44\n'); chmodSync(path.join(BIN, 'security'), 0o755);

  // ---------- the daemon (marks only) and Anthropic's API (the model list only) ----------
  const daemon = http.createServer((q, r) => { r.writeHead(200, { 'Content-Type': 'application/json' }); r.end(JSON.stringify(q.url === '/inherent/agent-marks' ? { marks: {} } : {})); });
  const api = http.createServer((q, r) => { r.writeHead(q.url.startsWith('/v1/models') ? 200 : 404, { 'Content-Type': 'application/json' }); r.end('{"data":[]}'); });
  await new Promise(r => daemon.listen(0, '127.0.0.1', r)); await new Promise(r => api.listen(0, '127.0.0.1', r));

  // ---------- the host ----------
  const root = path.join(tmp, 'root'), dir = path.join(root, 'agents'), port = await freePort(), log = path.join(tmp, 'claude.log'), cxlog = path.join(tmp, 'codex.log');
  await mkdir(path.join(root, 'logs'), { recursive: true });
  await writeFile(path.join(root, 'plugin-access.json'), JSON.stringify({ token: 'daemon-stage' }));
  const env = { PATH: `${BIN}:/usr/bin:/bin`, HOME, SHELL: path.join(BIN, 'sh'), USER: os.userInfo().username, LANG: 'en_US.UTF-8', TMPDIR: os.tmpdir(), ELECTRON_RUN_AS_NODE: '1',
    JARVIS_RUNTIME_ROOT: root, JARVIS_AGENTS_DIR: dir, JARVIS_AGENTS_PORT: String(port), JARVIS_INHERENT_BRIDGE_PORT: String(daemon.address().port),
    JARVIS_AGENTS_CLAUDE: path.join(here, 'fake-claude.mjs'), JARVIS_AGENTS_SECURITY: path.join(BIN, 'security'), CLAUDE_CONFIG_DIR: CONFIG,
    ANTHROPIC_BASE_URL: `http://127.0.0.1:${api.address().port}`, FAKE_CLAUDE_LOG: log, FAKE_CODEX_LOG: cxlog };
  const proc = spawn(path.join(app, 'node_modules', '.bin', 'electron'), [path.join(app, 'dist-electron', 'agents', 'host.js')], { stdio: ['ignore', 'pipe', 'pipe'], env });
  let out = ''; proc.stdout.on('data', b => { out += b; }); proc.stderr.on('data', b => { out += b; });
  for (let i = 0; i < 100 && !existsSync(path.join(dir, 'host-key')); i++) await sleep(100);
  const key = readFileSync(path.join(dir, 'host-key'), 'utf8'), API = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i++) { if ((await fetch(`${API}/health`, { headers: { Authorization: `Bearer ${key}` } }).catch(() => null))?.ok) break; await sleep(150); }
  const call = async (route, body, method = body === undefined ? 'GET' : 'POST') => {
    const r = await fetch(API + route, { method, headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: r.status, ...await r.json().catch(() => ({})) };
  };
  const lines = f => existsSync(f) ? readFileSync(f, 'utf8').split('\n').filter(Boolean).map(l => JSON.parse(l)) : [];
  const until = async (what, pred, ms = 20000) => {
    const t0 = Date.now();
    for (;;) { const v = await pred(); if (v) return v; if (Date.now() - t0 > ms) throw new Error(`timed out: ${what}`); await sleep(60); }
  };
  // The rows as the event stream tells them, the way the page learns them.
  const rows = new Map(), events = [];
  const stream = await fetch(`${API}/events`, { headers: { Authorization: `Bearer ${key}` } });
  void (async () => {
    let buf = '';
    try { for await (const chunk of stream.body) {
      buf += Buffer.from(chunk).toString('utf8');
      for (let at = buf.indexOf('\n\n'); at >= 0; at = buf.indexOf('\n\n')) {
        const line = buf.slice(0, at); buf = buf.slice(at + 2);
        if (!line.startsWith('data: ')) continue;
        const e = JSON.parse(line.slice(6));
        events.push(e);
        if (e.t === 'hello') for (const s of e.sessions) rows.set(s.id, s);
        if (e.t === 'sess') rows.set(e.s.id, e.s);
        if (e.t === 'gone') rows.delete(e.id);
      }
    } } catch { /* the host went away */ }
  })();
  const row = id => rows.get(id);
  const settled = s => s && !['work', 'pack'].includes(s.st);

  // ---------- the built page, served as the window loads it ----------
  const dist = path.join(app, 'dist');
  const web = http.createServer(async (q, r) => {
    const u = new URL(q.url, 'http://x'), f = path.join(dist, decodeURIComponent(u.pathname === '/' ? '/agents.html' : u.pathname));
    if (!f.startsWith(dist) || !existsSync(f)) { r.writeHead(404); r.end(); return; }
    r.writeHead(200, { 'Content-Type': TYPES[path.extname(f)] ?? 'application/octet-stream' }); r.end(await readFile(f));
  });
  await new Promise(r => web.listen(0, '127.0.0.1', r));

  // ---------- Chromium, as the window ----------
  const { chromium } = await import('playwright');
  // CHROMIUM names a browser to use; else the one this machine keeps for Playwright, else Playwright's own.
  const exe = [process.env.CHROMIUM, '/opt/pw-browsers/chromium'].find(p => p && existsSync(p));
  const browser = await chromium.launch({ headless, ...exe ? { executablePath: exe } : {} });
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2 });
  await context.route(`${API}/**`, route => route.continue({ headers: { ...route.request().headers(), authorization: `Bearer ${key}` } }));
  await context.addInitScript(() => {
    const calls = []; window.__agentsCalls = calls;
    const rec = (k, v) => (...a) => { calls.push([k, ...a]); return v; };
    window.agents = { presence: rec('presence'), onNext: cb => { window.__agentsNext = cb; return () => {}; }, onOpen: cb => { window.__agentsOpen = cb; return () => {}; },
      folder: rec('folder', Promise.resolve('')), terminal: rec('terminal', Promise.resolve(false)), reveal: rec('reveal', Promise.resolve()),
      openUrl: rec('openUrl', Promise.resolve()), openPath: rec('openPath', Promise.resolve()), cloud: rec('cloud', Promise.resolve(false)),
      terminals: rec('terminals', Promise.resolve([])), revealFile: rec('revealFile', Promise.resolve()), quickLook: rec('quickLook', Promise.resolve()),
      editors: rec('editors', Promise.resolve([])), openInEditor: rec('openInEditor', Promise.resolve()), pathOf: f => `/dropped/${f?.name ?? 'file'}` };
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
  const pageURL = (q = '') => `http://127.0.0.1:${web.address().port}/agents.html?port=${port}${q}`;
  if (shots) await mkdir(shots, { recursive: true });

  const st = {
    tmp, repo, HOME, API, key, call, until, row, rows, events, page, browser, context, errors, shots,
    claude: () => lines(log), codex: () => lines(cxlog),
    // A session with its first turn done (or stopped on what it asks), the way the window starts one.
    async session(text, o = {}) {
      const r = await call('/sessions', { agent: 'claude', cwd: repo, text, model: o.agent === 'codex' ? undefined : 'fake-sonnet', ...o });
      if (!r.id) throw new Error(`no session: ${JSON.stringify(r)}`);
      if (o.wait !== false) await until(`first turn of ${r.id}`, () => settled(row(r.id)));
      return r.id;
    },
    // One more message, to the end of its turn.
    async send(id, text, extra = {}) {
      const n = row(id)?.trace?.length ?? 0, r = await call(`/sessions/${id}/send`, { text, ...extra });
      if (r.status !== 200) throw new Error(`send: ${JSON.stringify(r)}`);
      await until(`turn "${text}"`, () => (row(id)?.trace?.length ?? 0) > n && settled(row(id)));
    },
    // The page, on one session or on the list; ready once the boot line is gone.
    async open(id = '', q = '') {
      await page.goto(pageURL(`${id ? `&open=${encodeURIComponent(id)}` : ''}${q}`));
      await page.waitForFunction(() => !document.querySelector('#win')?.classList.contains('booting'), null, { timeout: 15000 });
      await page.waitForTimeout(600);
    },
    async shot(name, clip) {
      if (!shots) return null;
      const f = path.join(shots, `${name}.png`);
      await page.screenshot({ path: f, ...clip ? { clip } : {} });
      return f;
    },
    async close() {
      await browser.close().catch(() => {});
      stream.body?.cancel().catch(() => {}); proc.kill(); try { execFileSync('pkill', ['-f', `keeper.js ${dir}`]); } catch { /* none left */ }
      web.close(); daemon.close(); api.close();
      if (process.env.KEEP_TMP) console.log(`kept ${tmp}`); else await rm(tmp, { recursive: true, force: true });
    },
    log: () => out,
  };
  return st;
}

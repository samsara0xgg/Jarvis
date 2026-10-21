// The day pane's Agents door and 待办 door (src/doors/agents.tsx, src/doors/todo.tsx) inside the doors frame (src/DayTimeline.tsx, src/DayDoors.tsx). Headless Chrome against the built page with the
// native bridge stubbed and a fake daemon that serves a day, the day before it, Claude Code and Codex sessions (one waiting on a permission, one working, finished ones) and today's calendar and to-dos,
// and, for one scenario, a stand-in for Startrail's agent host (its event stream and the routes the page answers and marks through). Run after `npm run build`.
// Screenshots (2x) of both pages land in $OUT (default verify-out/doors-agents/, not committed).
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = process.env.OUT ?? path.join(root, 'verify-out/doors-agents');
mkdirSync(out, { recursive: true });
const web = Number(process.env.COMPANION_PORT ?? 5411), daemonPort = Number(process.env.COMPANION_DAEMON_PORT ?? 9011), daemon = `http://127.0.0.1:${daemonPort}`;
const hostPort = daemonPort + 1;   // Startrail's agent host: never listened on, the page's requests to it are answered by the browser's routes
const checks = [], errors = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name} ${detail}`); checks.push(name); console.log('ok', name); };
const same = (a, b, d = .75) => Math.abs(a - b) <= d;

// ---------- the day: 2026-10-19 in Vancouver (PDT, UTC-7), now 21:50; the day before it ----------
const DATE = '2026-10-19', YESTERDAY = '2026-10-18', TZ = 'America/Vancouver';
const T = (h, m = 0, day = 19) => Date.UTC(2026, 9, day, h + 7, m);
const NOW = T(21, 50);
const iso = (h, m, day = 19) => `2026-10-${String(day).padStart(2, '0')}T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00-07:00`;
const it = (id, kind, start, end, extra = {}, lane = 'you') => ({ id, lane, kind, start_ms: start, end_ms: end, state: end === null ? (start > NOW ? 'next' : 'now') : end <= NOW ? 'done' : start > NOW ? 'next' : 'now', ...extra });
const todayReply = () => ({
  date: DATE, tz: TZ, start_ms: T(0), end_ms: T(24), now_ms: NOW,
  items: [it('sleep-1', 'sleep', T(0, 55), T(9, 5), { minutes: 470 }), it('stay-home-1', 'stay', T(9, 5), T(13, 15), { place: '12 Example St, Victoria', kept_place: { id: 'p-home', name: '家', role: 'home' } })],
  labels: [], missing: [], line: { text: '今天在家做 Jarvis。', draft: false, written_at: '2026-10-19T21:30:00-07:00' }, highlights: [],
});
// yesterday has two Claude Code sessions (the day's own `code` items, the only thing another day knows of Agents)
const yesterdayReply = () => ({
  date: YESTERDAY, tz: TZ, start_ms: T(0, 0, 18), end_ms: T(24, 0, 18), now_ms: NOW,
  items: [
    it('y-sleep', 'sleep', T(1, 10, 18), T(8, 40, 18), { minutes: 450 }),
    it('code-aaa', 'code', T(10, 0, 18), T(11, 30, 18), { title: '改登录页', project: 'jarvis', account: 'a', session_id: 'aaa' }),
    it('code-bbb', 'code', T(10, 30, 18), T(10, 50, 18), { title: '', project: 'typlus', account: 'b', session_id: 'bbb' }),
  ],
  labels: [], missing: [], line: null, highlights: [],
});

// ---------- sessions: the five the mock shows (two Codex ones finished, a Claude one finished, one waiting on a permission, one working) ----------
const claude = (id, extra = {}) => ({ agent: 'claude', session_id: id, kind: 'interactive', phase: 'done', title: id, project: 'jarvis', branch: 'main', cwd: '/x/jarvis', where: 'Ghostty', prompt: `do ${id}`, activity: '', last_message: '',
  asks: null, replyable: false, request: null, started_ms: null, updated_ms: NOW, from_project: false, ...extra });
const codex = (id, extra = {}) => ({ session_id: id, state: 'finished', cwd: '/x/jarvis', model: '', prompt: id, detail: '', last_message: '', since_ms: NOW, ...extra });
const BASH = { id: 'req-1', tool: 'Bash', input: { command: 'npm test', description: 'run the tests' }, cwd: '/x/jarvis', always: '' };
const ASK = { id: 'req-2', tool: 'AskUserQuestion', input: { questions: [{ question: '用哪个方案？', header: '方案', multiSelect: false, options: [{ label: 'A', description: 'a' }, { label: 'B', description: 'b' }] }] }, cwd: '/x/jarvis', always: '' };
const baseClaude = () => [
  claude('c-ci', { title: '修 CI', phase: 'done', last_message: 'CI 变绿了', started_ms: T(16, 10), updated_ms: T(17, 25) }),
  claude('c-line', { title: '时间线', phase: 'needs_input', request: BASH, activity: '', started_ms: T(19, 40), updated_ms: T(21, 40), prompt: '跑一下测试' }),
  claude('c-exam', { title: '工具考试', phase: 'working', activity: '第 3 轮 · 41 / 60', started_ms: T(20, 15), updated_ms: T(21, 49) }),
];
const baseCodex = () => [
  codex('r-readme', { prompt: '改 README', last_message: '改了 2 个文件', since_ms: T(10, 40) }),
  codex('r-tests', { prompt: '整理测试', last_message: '6 个测试快了一倍', since_ms: T(21, 5) }),
];
// Startrail's sessions, as its agent host's event stream holds them: a Claude one that waits on a command (it began 18:30, asked at 21:30), a Codex one that finished (no start known).
const sess = (id, extra = {}) => ({ id, agent: 'claude', title: id, cwd: '/x/app', project: 'app', branch: 'main', tree: false, st: 'done', pinned: false, parked: false, archived: false, unread: false, updated: NOW, summary: '', model: '', effort: '', mode: '', ctx: 0, ...extra });
const hostRows = () => [
  sess('st-wait', { title: '接上登录', st: 'wait', created: T(18, 30), updated: T(21, 30), trace: [{ at: T(21, 30), st: 'wait' }] }),
  sess('st-done', { title: '整理文档', agent: 'codex', updated: T(15, 0), summary: '文档整理好了' }),
];
const HOST_ITEMS = { 'st-wait': [{ k: 'you', text: '接上登录' }, { k: 'it', text: '登录接好了，我跑一下测试。' }, { k: 'req', req: { id: 'sreq-1', tool: 'Bash', why: '跑测试', cmd: 'npm test', cwd: '/x/app', always: '' } }] };
const CONVERSATION = [{ who: 'you', text: '跑一下测试' }, { who: 'it', text: '好的，我先**跑**测试，再看结果。' }];

// ---------- today's calendar and to-dos ----------
const baseToday = () => ({
  weather: null,
  events: [
    { id: 'ev-class', title: 'CSC370 讲课', start: iso(14, 0), end: iso(15, 20) },
    { id: 'ev-mom', title: '和妈妈通话', start: iso(22, 0), end: iso(22, 30) },
    { id: 'ev-bday', title: '爸爸生日', start: iso(0, 0), all_day: true },
  ],
  todos: [
    { id: 'td-a3', title: 'CSC370 A3 作业', due: iso(17, 0) },
    { id: 'td-pay', title: '交房租', due: iso(23, 30) },
    { id: 'td-lee', title: '回复 Lee 教授' },
    { id: 'td-read', title: '读论文', due: iso(23, 59) },
    { id: 'td-old', title: '续借图书证', due: iso(23, 59, 17) },
  ],
});

// ---------- the built page ----------
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
// CHROMIUM names a browser to use; else the one a container keeps for Playwright; else this Mac's Chrome.
const exe = [process.env.CHROMIUM, '/opt/pw-browsers/chromium'].find(p => p && existsSync(p));
const browser = await chromium.launch({ headless: true, ...exe ? { executablePath: exe } : { channel: 'chrome' }, args: ['--disable-web-security', '--mute-audio'] });

// A session that waits on him is a card on the island, and a card keeps the day from opening (while the day is up a new one is held back): so those arrive once the day is open (`arrive`).
const waits = r => r.phase === 'needs_input' || !!r.request;
// A page on the fake daemon. `s` is what the daemon holds and what it was sent: `claude`, `codex`, `today` (null: a daemon that has no such route), `host` (Startrail's sessions, when the page is given a host) and `posts`
// (the host's under `host:`); `s.refuse`: routes that answer with an error.
async function boot({ lang = 'zh', agentsWindow = true, claude: c = baseClaude(), codex: x = baseCodex(), today = baseToday(), host = null } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const s = { posts: [], refuse: new Set(), claude: c.filter(r => !waits(r)), later: c.filter(waits), codex: x, today, marks: {}, reads: [], polls: 0, host: (host ?? []).filter(r => r.st !== 'wait'), hostLater: (host ?? []).filter(r => r.st === 'wait'), hellos: 0 };
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
  await page.addInitScript(([settings, windowed]) => {
    localStorage.setItem('companion-settings-v1', JSON.stringify(settings));
    const jump = (...a) => { (window.__jumps ??= []).push(a); };
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {}, wearing: () => {}, onDictation: () => () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, onGhostty: () => () => {}, onAgentsPresence: () => () => {}, onMouseDown: () => () => {}, onClaudeFront: () => () => {}, plugins: async () => ({}),
      jumpGhostty: async (title, job) => jump('ghostty', title, job), openCodex: async id => jump('codex', id),
      ...windowed ? { openAgents: id => jump('agents', id ?? '') } : {},
    };
    window.WebSocket = class { constructor() { setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
  }, [{ lang }, agentsWindow]);
  await page.route(`${daemon}/**`, async route => {
    const req = route.request(), url = new URL(req.url()), post = req.method() === 'POST', p = url.pathname;
    const json = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    const body = () => JSON.parse(req.postData() ?? '{}');
    let m;
    if (p === '/inherent/day') {
      const date = url.searchParams.get('date'), no = Number(date?.slice(-2));   // any other day is an empty one of its own
      return json(!date || date === DATE ? todayReply() : date === YESTERDAY ? yesterdayReply() : { ...yesterdayReply(), date, start_ms: T(0, 0, no), end_ms: T(24, 0, no), items: [] });
    }
    if (p === '/inherent/claude-sessions') { s.polls++; return json({ sessions: s.claude, error: null }); }
    if (p === '/inherent/codex-sessions') return json({ sessions: s.codex });
    if (!post && (m = p.match(/^\/inherent\/claude-sessions\/([^/]+)\/conversation$/))) { s.reads.push(decodeURIComponent(m[1])); return json({ messages: CONVERSATION }); }
    if (post && (m = p.match(/^\/inherent\/claude-sessions\/([^/]+)\/reply$/))) { s.posts.push({ path: p, body: body() }); return s.refuse.has('reply') ? json({ detail: 'no' }, 409) : json({ ok: true }); }
    // The answer to a held permission: the daemon takes it, or (refused) the request is gone.
    if (post && (m = p.match(/^\/inherent\/claude-requests\/([^/]+)$/))) { s.posts.push({ path: p, body: body() }); return s.refuse.has('request') ? json({ detail: 'gone' }, 409) : json({ ok: true }); }
    if (p === '/inherent/agent-marks' && !post) return json({ marks: s.marks });
    if (post && (m = p.match(/^\/inherent\/agent-marks\/([^/]+)$/))) {
      const id = decodeURIComponent(m[1]), b = body();
      s.posts.push({ path: p, body: b });
      s.marks[id] = { unread: !!b.unread, parked_ms: b.park ? NOW : null, archived_ms: b.archive ? NOW : null };
      return json({ ok: true });
    }
    if (p === '/inherent/today' && !post) return s.today === null ? json({ detail: 'Not Found' }, 404) : s.today === 'fail' ? json({ detail: 'down' }, 500) : json(s.today);
    if (post && p === '/inherent/today/todo') { s.posts.push({ path: p, body: body() }); return s.refuse.has('todo') ? json({ detail: 'down' }, 502) : json({ ok: true }); }
    if (p === '/inherent/controls') return json({ mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });
  // The agent host (only when the page is given one): its event stream says `hello` with the sessions and is read again a second later, its routes answer and record.
  if (host) await page.route(`http://127.0.0.1:${hostPort}/**`, async route => {
    const req = route.request(), p = new URL(req.url()).pathname, post = req.method() === 'POST', json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    let m;
    if (p === '/events') { s.hellos++; return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `retry: 1000\ndata: ${JSON.stringify({ t: 'hello', sessions: s.host, settings: {} })}\n\n` }); }
    if (!post && (m = p.match(/^\/sessions\/([^/]+)$/))) return json({ items: HOST_ITEMS[decodeURIComponent(m[1])] ?? [] });
    if (post && (m = p.match(/^\/sessions\/([^/]+)\/(answer|meta)$/))) { s.posts.push({ path: `host:${p}`, body: JSON.parse(req.postData() ?? '{}') }); return json({}); }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
  });
  await page.clock.setFixedTime(new Date(NOW));
  await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${daemonPort}${host ? `&agents=${hostPort}` : ''}`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
  // The native bridge tells the page where the pointer is; a test moves the real one and tells the page the same.
  const move = async (x, y) => { await page.mouse.move(x, y); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [x, y]); };
  const read = () => page.evaluate(() => { const n = document.querySelector('.notch'); return { tl: n.dataset.tl, door: n.querySelector('.dt')?.dataset.door ?? null }; });
  const until = (fn, arg, timeout = 5000) => page.waitForFunction(fn, arg, { timeout });
  const lineX = async () => { const at = await page.evaluate(() => document.querySelector('.notch').dataset.lineAt), [x0, x1] = at.split('-').map(Number); return (x0 + x1) / 2; };
  const press = async (x, y) => { await move(x, y); await page.mouse.down(); await page.mouse.up(); };
  const rect = sel => page.evaluate(sel => { const e = document.querySelector(sel); return e ? e.getBoundingClientRect().toJSON() : null; }, sel);
  // A list longer than the pane opens scrolled to the now line: what is pointed at is first brought into view.
  const reveal = sel => page.evaluate(sel => document.querySelector(sel)?.scrollIntoView({ block: 'nearest' }), sel);
  const pressOn = async sel => { await reveal(sel); const r = await rect(sel); assert.ok(r, `${sel} is on the page`); await press(r.x + r.width / 2, r.y + r.height / 2); };
  const hoverOn = async sel => { await reveal(sel); const r = await rect(sel); assert.ok(r, `${sel} is on the page`); await move(r.x + r.width / 2, r.y + r.height / 2); };
  const texts = sel => page.evaluate(sel => [...document.querySelectorAll(sel)].map(e => (e.innerText || e.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim()), sel);
  const keys = () => page.evaluate(() => [...document.querySelectorAll('.notch .dt-row[data-key]')].map(e => e.dataset.key));
  const door = async key => { await pressOn(`.notch .dt-door[data-door="${key}"]`); await until(key => document.querySelector('.notch .dt')?.dataset.door === key, key); await page.waitForTimeout(420); };
  // The day pane opened by a click on the wing's line (it stays up), with the rows drawn and settled.
  const openDay = async () => {
    await until(() => document.querySelector('.notch')?.dataset.line);
    await press(await lineX(), 14);
    await until(() => document.querySelector('.notch-drop.is-open .dt-door'), undefined, 8000);
    await page.waitForTimeout(1000);
  };
  // Where the frame's parts are: the pane (the dialog, its inside, the black shape), the day inside it, and what must never move in it.
  const geo = () => page.evaluate(() => {
    const r = e => { const b = e?.getBoundingClientRect(); return b ? b.toJSON() : null; }, q = s => document.querySelector(s), dt = q('.notch .dt');
    return { drop: r(q('.notch-drop')), inner: r(q('.notch-pane-in')), shape: r(q('.notch-shape path')), dt: r(dt), head: r(dt.querySelector('.dt-head')), bar: r(dt.querySelector('.dt-bar')), track: r(dt.querySelector('.dt-track')), doors: r(dt.querySelector('.dt-doors')), from: dt.querySelector('.dt-bar')?.dataset.from ?? null };
  });
  // The page's marks on the bar: for each, its row's key, its kind and tone, where it is (x and width) and, for a span, which lane (its top under the track's top: 2, 9 or 16).
  const marks = () => page.evaluate(() => {
    const track = document.querySelector('.notch .dt-track').getBoundingClientRect();
    return [...document.querySelectorAll('.notch .dt-track .dt-mk')].map(e => { const b = e.getBoundingClientRect(); return { key: e.dataset.mark, kind: e.classList.contains('is-span') ? 'span' : 'point', tone: ['on', 'dim', 'amber'].find(t => e.classList.contains(`is-${t}`)),
      x: b.left, w: b.width, mid: b.left + b.width / 2, top: Math.round(b.top - track.top), muted: e.classList.contains('is-muted') }; });
  });
  // The rows as the page shows them, in order, with the now line among them.
  const rows = () => page.evaluate(() => [...document.querySelectorAll('.notch .dt-list .dt-row, .notch .dt-list .dt-now, .notch .dt-list .dt-uh')].map(e => {
    if (e.classList.contains('dt-now')) return { key: 'now', text: e.innerText.replace(/\s+/g, ' ').trim() };
    if (e.classList.contains('dt-uh')) return { key: 'heading', text: e.innerText.trim() };
    const q = s => e.querySelector(s)?.innerText.replace(/\s+/g, ' ').trim() ?? '';
    return { key: e.dataset.key, at: q('.dt-at'), title: q('.dt-ttl b'), tag: q('.dt-ttl i'), sub: q('.dt-tx > em'), side: q('.dt-tm'), quick: [...e.querySelectorAll('.dt-quick button')].map(b => b.innerText.trim()), cls: e.className, icon: e.querySelector('.dt-ic')?.className ?? '' };
  }));
  // The waiting sessions come in, and the page has read two lists since (it reads every 1.5 s; the host's stream is read again every second).
  const arrive = async () => {
    s.claude = [...s.claude, ...s.later]; s.later = [];
    s.host = [...s.host, ...s.hostLater]; s.hostLater = [];
    const n = s.polls, h = s.hellos;
    while (s.polls < n + 2 || host && s.hellos < h + 2) await page.waitForTimeout(100);
    await page.waitForTimeout(300);
  };
  const shot = async (name, g) => page.screenshot({ path: path.join(out, `${name}.png`), clip: { x: 60, y: 0, width: 520, height: Math.ceil(Math.max(g.drop.bottom, g.dt.bottom)) + 2 } });
  return { page, ctx, s, move, read, until, press, pressOn, hoverOn, texts, keys, door, openDay, arrive, geo, marks, rows, rect, shot };
}
// What moved between two looks at the frame: the sizes of the pane and its black shape, and the places of the head, the bar and the doors.
const moved = (g0, g1) => [['pane', 'drop', 'height'], ['pane inside', 'inner', 'height'], ['shape', 'shape', 'height'], ['day', 'dt', 'height'], ['day', 'dt', 'top'], ['head', 'head', 'top'], ['bar', 'bar', 'top'], ['doors', 'doors', 'top']]
  .filter(([, k, p]) => !same(g0[k][p], g1[k][p])).map(([name, k, p]) => `${name} ${p} ${g0[k][p]} -> ${g1[k][p]}`);
// The x of a moment on the bar the geometry describes (the bar runs from the hour in `data-from` to 24:00 of the day).
const xAt = (g, ms, day = 19) => { const [h, m] = g.from.split(':').map(Number), t0 = T(h, m, day), t1 = T(24, 0, day); return g.track.left + (ms - t0) / (t1 - t0) * g.track.width; };
const lanes = { 0: 2, 1: 9, 2: 16 }, order = list => list.map(r => r.key);

try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }

  {
    const a = await boot();
    await a.openDay();
    await a.arrive();
    await a.until(() => document.querySelector('.notch .dt-door[data-door="agents"]')?.textContent === 'Agents2');
    const g0 = await a.geo();

    // ---- Agents: the frame, the numbers, the line ----
    check('the door says how many sessions are live (a waiting one and a working one), in amber because one waits on him',
      (await a.texts('.notch .dt-door[data-door="agents"]'))[0] === 'Agents 2' && await a.page.locator('.notch .dt-door[data-door="agents"] em.is-amber').count() === 1, (await a.texts('.notch .dt-door')).join('|'));
    await a.door('agents');
    const g1 = await a.geo();
    check('on Agents the pane, its shape, the day, the head, the bar and the doors row are where they were on 今天 and the same size (584 pt)', moved(g0, g1).length === 0 && same(g1.dt.height, 584), moved(g0, g1).join('; '));
    check('the line is written from the facts: the one that waits by name, and how many more run',
      (await a.texts('.notch .dt-line'))[0] === '时间线 在等你点头；另外 1 个在跑。', (await a.texts('.notch .dt-line')).join());
    check('the numbers: 等你 1 (amber, a lens), 在跑 1, 今天做完 3 (a Claude session and the two Codex ones), and the link to the Agents window',
      (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '等你 1|在跑 1|今天做完 3' && await a.page.locator('.notch .dt-numbers .dt-num.is-amber').count() === 1
      && await a.page.locator('.notch .dt-numbers button.dt-num').count() === 1 && (await a.texts('.notch .dt-numbers .dt-go'))[0] === 'Agents 窗口', (await a.texts('.notch .dt-numbers .dt-num')).join('|'));

    // ---- Agents: the rows ----
    const list = await a.rows();
    const order = list.map(r => r.key);
    check('the rows are the day\'s sessions, oldest start first (a Codex one, which does not say when it began, by its last change), with the now line after them',
      order.join() === 'r-readme,c-ci,c-line,c-exam,r-tests,now' && list.slice(0, 5).map(r => r.at).join('|') === '10:40|16:10|19:40|20:15|21:05' && list[5].text === '现在 21:50', JSON.stringify(list.map(r => [r.key, r.at])));
    const by = Object.fromEntries(list.map(r => [r.key, r]));
    check('a row says who and what: the brand, the title, what it asks / does / how it ended',
      by['c-ci'].title === '修 CI' && by['c-ci'].sub === 'Claude · 做完了 · CI 变绿了' && by['c-line'].sub === 'Claude · 想跑 npm test' && by['c-exam'].sub === 'Claude · 第 3 轮 · 41 / 60'
      && by['r-readme'].sub === 'Codex · 做完了 · 改了 2 个文件' && by['r-tests'].title === '整理测试', JSON.stringify(list.map(r => r.sub)));
    check('the circle holds the brand\'s logo (Claude\'s for Claude, OpenAI\'s for Codex), as a mask that takes the circle\'s ink',
      await a.page.evaluate(() => [...document.querySelectorAll('.notch .dt-list .dt-row')].map(r => { const l = r.querySelector('.dt-ic .dt-logo'), s = l && getComputedStyle(l); return !!l && /svg/.test(s.maskImage) && s.width === '15px'; }).every(Boolean))
      && await a.page.evaluate(() => { const m = [...document.querySelectorAll('.notch .dt-list .dt-row')].map(r => getComputedStyle(r.querySelector('.dt-logo')).maskImage); return m[0] !== m[1] && m[1] === m[2]; }));
    check('only the one that waits on him has the amber tag 等你; the live ones have the lit ring, the finished ones do not',
      by['c-line'].tag === '等你' && ['c-ci', 'c-exam', 'r-readme', 'r-tests'].every(k => by[k].tag === '') && by['c-line'].icon.includes('is-on') && by['c-exam'].icon.includes('is-on') && !by['c-ci'].icon.includes('is-on') && !by['r-tests'].icon.includes('is-on'));
    check('the side says how long it ran: a finished one to its last change, a live one to now (a working one with the live dot); a Codex session says nothing',
      by['c-ci'].side === '1 时 15 分' && by['c-exam'].side === '1 时 35 分' && by['c-line'].side === '2 时 10 分' && by['r-readme'].side === '' && by['r-tests'].side === ''
      && await a.page.locator('.notch .dt-row[data-key="c-exam"] .dt-tm .dt-live i').count() === 1 && await a.page.locator('.notch .dt-row[data-key="c-line"] .dt-tm .dt-live').count() === 0, JSON.stringify(list.map(r => r.side)));

    // ---- Agents: the marks on the bar ----
    const mk = await a.marks(), ofKey = key => mk.filter(m => m.key === key);
    const span = (key, tone) => mk.find(m => m.key === key && m.kind === 'span' && m.tone === tone);
    const ci = span('c-ci', 'dim'), lineOn = span('c-line', 'on'), lineWait = span('c-line', 'amber'), exam = span('c-exam', 'on');
    check('each session is a span over the time it ran: a finished one dim, a working one lit to now, and the one that waits lit until it stopped and amber since (21:40 to 21:50)',
      ci && lineOn && lineWait && exam && [[ci, T(16, 10), T(17, 25)], [lineOn, T(19, 40), T(21, 40)], [lineWait, T(21, 40), T(21, 50)], [exam, T(20, 15), T(21, 50)]].every(([m, from, to]) => same(m.x, xAt(g1, from), 1.2) && same(m.x + m.w, xAt(g1, to), 1.2)), JSON.stringify(mk));
    check('the spans are packed into lanes by start: the 16:10 and the 19:40 one share the first lane, the 20:15 one (while it runs) is in the second',
      ci.top === lanes[0] && lineOn.top === lanes[0] && lineWait.top === lanes[0] && exam.top === lanes[1], JSON.stringify([ci.top, lineOn.top, lineWait.top, exam.top]));
    check('a Codex session, which says only when it last changed, is a dim point there (10:40 and 21:05)',
      ofKey('r-readme').length === 1 && ofKey('r-readme')[0].kind === 'point' && ofKey('r-readme')[0].tone === 'dim' && same(ofKey('r-readme')[0].mid, xAt(g1, T(10, 40)), 1.2)
      && ofKey('r-tests')[0].kind === 'point' && same(ofKey('r-tests')[0].mid, xAt(g1, T(21, 5)), 1.2), JSON.stringify(mk));
    check('amber is only the waiting part: nothing else on the bar is amber', mk.filter(m => m.tone === 'amber').length === 1 && lineWait.tone === 'amber');
    await a.shot('agents', g1);

    // ---- Agents: the waiting row's buttons ----
    await a.hoverOn('.notch .dt-row[data-key="c-line"]');
    await a.page.waitForTimeout(250);
    const hot = await a.rows(), hotLine = hot.find(r => r.key === 'c-line');
    check('on a rest on the row that waits (with a permission it holds) the buttons are 先放着 and 同意, and 同意 is the amber filled pill',
      hotLine.quick.join('|') === '先放着|同意' && await a.page.evaluate(() => { const b = [...document.querySelectorAll('.notch .dt-row[data-key="c-line"] .dt-quick button')], c = b.map(x => getComputedStyle(x).backgroundColor); return c[1] === 'rgb(255, 201, 143)' && c[0] !== c[1] && getComputedStyle(document.querySelector('.notch .dt-row[data-key="c-line"] .dt-quick')).opacity === '1'; }), JSON.stringify(hotLine.quick));
    check('every other row has just 打开 (a Claude session in Ghostty, a Codex session in Codex)', hot.filter(r => r.key !== 'c-line' && r.key !== 'now').every(r => r.quick.join() === '打开'), JSON.stringify(hot.map(r => [r.key, r.quick])));
    await a.shot('agents-hover', g1);
    await a.pressOn('.notch .dt-row[data-key="c-ci"] .dt-quick button');
    await a.hoverOn('.notch .dt-row[data-key="c-ci"]');
    await a.pressOn('.notch .dt-row[data-key="c-ci"] .dt-quick button');
    await a.hoverOn('.notch .dt-row[data-key="r-tests"]');
    await a.pressOn('.notch .dt-row[data-key="r-tests"] .dt-quick button');
    const jumps = await a.page.evaluate(() => window.__jumps ?? []);
    check('打开 goes to where it runs and does not open the row: a Claude session in Ghostty (by its title), a Codex one in Codex', jumps.some(j => j[0] === 'ghostty' && j[1] === '修 CI') && jumps.some(j => j[0] === 'codex' && j[1] === 'r-tests') && await a.page.locator('.notch .dt-page[data-page]').count() === 0, JSON.stringify(jumps));

    // ---- Agents: allow ----
    await a.hoverOn('.notch .dt-row[data-key="c-line"]');
    await a.pressOn('.notch .dt-row[data-key="c-line"] .dt-quick button.is-yes');
    await a.page.waitForTimeout(500);
    const allowed = a.s.posts.filter(p => p.path.startsWith('/inherent/claude-requests/'));
    check('同意 answers the request the daemon holds: a POST to /inherent/claude-requests/req-1 with the decision allow', allowed.length === 1 && allowed[0].path === '/inherent/claude-requests/req-1' && allowed[0].body.decision === 'allow', JSON.stringify(allowed));
    const after = await a.rows(), line1 = after.find(r => r.key === 'c-line');
    check('the row stops waiting at once (no tag, no buttons, no amber, "已同意"), and so do the door, the chip and the line',
      line1.tag === '' && line1.sub === 'Claude · 已同意' && line1.quick.join() === '打开' && (await a.marks()).every(m => m.tone !== 'amber') && (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '在跑 2|今天做完 3'
      && (await a.texts('.notch .dt-door[data-door="agents"]'))[0] === 'Agents 2' && await a.page.locator('.notch .dt-door[data-door="agents"] em.is-amber').count() === 0 && (await a.texts('.notch .dt-line'))[0] === '2 个在跑。', JSON.stringify(after.map(r => [r.key, r.tag, r.sub])));
    await a.ctx.close();
  }

  {
    // ---- allow refused: the request is gone ----
    const a = await boot();
    a.s.refuse.add('request');
    await a.openDay(); await a.arrive(); await a.door('agents');
    const rows0 = await a.geo();
    await a.hoverOn('.notch .dt-row[data-key="c-line"]');
    await a.pressOn('.notch .dt-row[data-key="c-line"] .dt-quick button.is-yes');
    await a.page.waitForTimeout(500);
    const row = (await a.rows()).find(r => r.key === 'c-line');
    check('when the daemon says the request is gone, the row waits again and the line says it could not be sent', row.tag === '等你' && (await a.texts('.notch .dt-line'))[0] === '没送出去，可能已经有人回答了。', JSON.stringify(row));
    // ---- park and take back ----
    await a.hoverOn('.notch .dt-row[data-key="c-line"]');
    await a.pressOn('.notch .dt-row[data-key="c-line"] .dt-quick button:first-child');
    await a.page.waitForTimeout(500);
    const parked = a.s.posts.filter(p => p.path === '/inherent/agent-marks/c-line');
    check('先放着 is the notch\'s own park: his mark goes to the daemon (agent-marks, park true) and the row is parked',
      parked.length >= 1 && parked.at(-1).body.park === true && (await a.rows()).find(r => r.key === 'c-line').tag === '先放着', JSON.stringify(parked));
    const p1 = await a.rows(), pl = p1.find(r => r.key === 'c-line'), seen = { cls: pl.cls, quick: pl.quick, amber: (await a.marks()).filter(m => m.tone === 'amber').length, nums: (await a.texts('.notch .dt-numbers .dt-num')).join('|'),
      door: (await a.texts('.notch .dt-door[data-door="agents"]'))[0], doorAmber: await a.page.locator('.notch .dt-door[data-door="agents"] em.is-amber').count(), line: (await a.texts('.notch .dt-line'))[0] };
    check('a parked session does not wait on him any more: no amber (the tag, the door, the chip, the bar) and the page says so; it can be taken back',
      seen.cls.includes('is-read') && seen.quick.join('|') === '拿回来|打开' && seen.amber === 0 && seen.nums === '在跑 1|今天做完 3' && seen.doorAmber === 0 && seen.line === '1 个在跑；1 个先放着。', JSON.stringify(seen));
    await a.hoverOn('.notch .dt-row[data-key="c-line"]');
    await a.pressOn('.notch .dt-row[data-key="c-line"] .dt-quick button:first-child');
    await a.page.waitForTimeout(500);
    const back = a.s.posts.filter(p => p.path === '/inherent/agent-marks/c-line').at(-1);
    check('拿回来 gives it back to his turn: park false to the daemon, and it waits (amber) again', back.body.park === false && (await a.rows()).find(r => r.key === 'c-line').tag === '等你' && (await a.marks()).some(m => m.tone === 'amber'), JSON.stringify(back));

    // ---- a row's page ----
    await a.pressOn('.notch .dt-row[data-key="c-line"] .dt-ic');
    await a.until(() => document.querySelector('.notch .dt-page[data-page="c-line"] .m-you'), undefined, 6000);
    await a.page.waitForTimeout(500);
    const pg = await a.page.evaluate(() => ({ title: document.querySelector('.notch .dt-page .dt-ph b')?.innerText, you: [...document.querySelectorAll('.notch .dt-page .m-you')].map(e => e.innerText), it: [...document.querySelectorAll('.notch .dt-page .m-it')].map(e => e.innerText), strong: document.querySelectorAll('.notch .dt-page .m-it strong').length,
      at: document.querySelector('.notch .dt-ag-top p')?.innerText, buttons: [...document.querySelectorAll('.notch .dt-page button.dt-ask')].map(b => b.innerText.trim()), ask: document.querySelector('.notch .dt-ag-ask')?.innerText, reply: !!document.querySelector('.notch .dt-page .pg-input') }));
    check('a click on a row opens its page: the title, where it runs, the conversation (his words, its answer with the bold rendered), what it asks, and the buttons',
      pg.title === '时间线' && pg.you.join() === '跑一下测试' && pg.it.join().startsWith('好的，我先跑测试') && pg.strong === 1 && pg.at === 'Claude · Ghostty · main' && pg.ask === '想跑 npm test'
      && pg.buttons.join('|') === '在它那里打开|先放着|同意' && !pg.reply && a.s.reads.includes('c-line'), JSON.stringify(pg));
    const g2 = await a.geo();
    check('and the pane has not changed size for it', moved(rows0, g2).length === 0 && same(g2.dt.height, 584), moved(rows0, g2).join('; '));
    await a.shot('agents-page', g2);
    await a.pressOn('.notch .dt-page button.dt-ask');
    const jumps = await a.page.evaluate(() => window.__jumps ?? []);
    check('在它那里打开 goes to where it runs', jumps.some(j => j[0] === 'ghostty' && j[1] === '时间线'), JSON.stringify(jumps));
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(500);
    check('Esc closes the page and stays on Agents', (await a.read()).door === 'agents' && await a.page.locator('.notch .dt-page[data-page]').count() === 0);
    await a.ctx.close();
  }

  {
    // ---- a question or a plan is not answered by one tap; a session from yesterday; one that does not say when it began; an idle background one takes a reply ----
    const c = [...baseClaude(), claude('c-ask', { title: '选方案', phase: 'needs_input', request: ASK, started_ms: T(18, 0), updated_ms: T(21, 0) }),
      claude('c-night', { title: '夜间整理', kind: 'background', where: 'background', job_id: 'job9', phase: 'working', activity: '整理笔记', started_ms: T(23, 0, 18), updated_ms: T(21, 49) }),
      claude('c-bare', { title: '写周报', phase: 'done', last_message: '写好了', started_ms: null, updated_ms: T(13, 0) }),
      claude('c-bg', { title: '后台问答', kind: 'background', where: 'background', job_id: 'job1', phase: 'done', replyable: true, last_message: '你想怎么改？', started_ms: T(12, 0), updated_ms: T(12, 30) })];
    const a = await boot({ claude: c, codex: [...baseCodex(), codex('r-run', { state: 'running', prompt: '跑回归', detail: 'npm run check', since_ms: T(21, 20) })] });
    await a.openDay(); await a.arrive(); await a.door('agents');
    const list = await a.rows(), by = Object.fromEntries(list.map(r => [r.key, r]));
    await a.hoverOn('.notch .dt-row[data-key="c-ask"]');
    const ask = (await a.rows()).find(r => r.key === 'c-ask');
    check('a question it asks is read in its words and is not allowed by one tap: the buttons are 先放着 and 打开', by['c-ask'].sub === 'Claude · 在问：用哪个方案？' && by['c-ask'].tag === '等你' && ask.quick.join('|') === '先放着|打开', JSON.stringify([by['c-ask'].sub, ask.quick]));
    check('a session that began yesterday has the date in the time column, and its span starts at the bar\'s left edge',
      by['c-night'].at === '10/18' && by['c-night'].side === '22 时 50 分' && order(list)[0] === 'c-night', JSON.stringify([by['c-night'].at, by['c-night'].side, order(list)]));
    const g = await a.geo(), mk = await a.marks(), night = mk.find(m => m.key === 'c-night');
    check('and a span from before the day is cut at the day: it starts at the track\'s left edge and runs to now', night.kind === 'span' && same(night.x, g.track.left, 1.2) && same(night.x + night.w, xAt(g, T(21, 50)), 1.2) && night.tone === 'on', JSON.stringify(night));
    check('a Claude session that does not say when it began is a point at its last change, with no length', by['c-bare'].at === '13:00' && by['c-bare'].side === '' && mk.filter(m => m.key === 'c-bare').length === 1 && mk.find(m => m.key === 'c-bare').kind === 'point', JSON.stringify([by['c-bare'], mk.filter(m => m.key === 'c-bare')]));
    const runMk = mk.filter(m => m.key === 'r-run');
    check('a running session that does not say when it began (a Codex one) is lit from its last change (21:20) to now, with no length',
      runMk.length === 1 && runMk[0].kind === 'span' && runMk[0].tone === 'on' && same(runMk[0].x, xAt(g, T(21, 20)), 1.2) && same(runMk[0].x + runMk[0].w, xAt(g, T(21, 50)), 1.2) && by['r-run'].at === '21:20' && by['r-run'].side === '' && by['r-run'].sub === 'Codex · npm run check', JSON.stringify([by['r-run'], runMk]));
    check('the numbers and the line follow: two wait, three run, and the line counts the waiting ones',
      (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '等你 2|在跑 3|今天做完 5' && (await a.texts('.notch .dt-line'))[0] === '2 个在等你点头；另外 3 个在跑。', (await a.texts('.notch .dt-numbers .dt-num')).join('|') + (await a.texts('.notch .dt-line')).join());
    // a lit chip is a lens
    await a.hoverOn('.notch .dt-numbers button.dt-num');
    await a.page.waitForTimeout(300);
    const lens = await a.rows(), mk2 = await a.marks();
    check('the 等你 chip is a lens: the rows it does not keep step back, and so do their marks', lens.filter(r => r.cls?.includes('is-off')).map(r => r.key).sort().join() === 'c-bare,c-bg,c-ci,c-exam,c-night,r-readme,r-run,r-tests' && mk2.filter(m => m.muted).length > 0 && mk2.filter(m => !m.muted).every(m => ['c-line', 'c-ask'].includes(m.key)), JSON.stringify(lens.map(r => [r.key, r.cls?.includes('is-off')])));
    await a.hoverOn('.notch .dt-head');
    // the idle background session takes a reply
    await a.pressOn('.notch .dt-row[data-key="c-bg"] .dt-ic');
    await a.until(() => document.querySelector('.notch .dt-page[data-page="c-bg"] .pg-input input'), undefined, 6000);
    await a.page.waitForTimeout(500);
    await a.page.fill('.notch .dt-page .pg-input input', '把标题改短一点');
    // with the text cursor in the box the pane holds itself open: the pointer leaves it for longer than the grace and it stays
    await a.move(20, 700);
    await a.page.waitForTimeout(1200);
    check('the pane is held open while he types into the box, with the pointer away from it', await a.page.locator('.notch-drop.is-open .dt-page .pg-input input').count() === 1 && await a.page.evaluate(() => document.activeElement?.closest?.('.pg-input') !== null));
    await a.hoverOn('.notch .dt-head');
    await a.page.keyboard.press('Enter');
    await a.page.waitForTimeout(600);
    const reply = a.s.posts.filter(p => p.path === '/inherent/claude-sessions/c-bg/reply');
    check('an idle background session takes a line typed into it: POST reply with the text, and the line shows at once on its page (and the box waits for the record)',
      reply.length === 1 && reply[0].body.text === '把标题改短一点' && (await a.texts('.notch .dt-page .m-you')).includes('把标题改短一点'), JSON.stringify(reply));
    await a.ctx.close();
  }

  {
    // ---- Startrail's sessions are rows with the others, answered and marked through the agent host (never the daemon) ----
    const a = await boot({ claude: [], codex: [], host: hostRows() });
    await a.openDay(); await a.arrive(); await a.door('agents');
    const list = await a.rows(), by = Object.fromEntries(list.map(r => [r.key, r])), g = await a.geo(), mk = await a.marks();
    check('Startrail\'s sessions are rows: by their start (a Codex one that does not say: its last change), the brand of the agent they run, what they ask or how they ended, how long they ran',
      order(list).join() === 'st-done,st-wait,now' && by['st-wait'].at === '18:30' && by['st-wait'].title === '接上登录' && by['st-wait'].sub === 'Claude · 想跑 npm test' && by['st-wait'].tag === '等你' && by['st-wait'].side === '3 时 20 分'
      && by['st-done'].at === '15:00' && by['st-done'].sub === 'Codex · 做完了 · 文档整理好了' && by['st-done'].side === '', JSON.stringify(list.map(r => [r.key, r.at, r.sub, r.side])));
    const wait = mk.filter(m => m.key === 'st-wait'), done = mk.filter(m => m.key === 'st-done');
    check('their marks: the waiting one is lit from its start to when it asked (21:30) and amber since; the finished one is a dim point at its last change (15:00)',
      wait.length === 2 && wait[0].tone === 'on' && wait[1].tone === 'amber' && same(wait[0].x, xAt(g, T(18, 30)), 1.2) && same(wait[0].x + wait[0].w, xAt(g, T(21, 30)), 1.2) && same(wait[1].x + wait[1].w, xAt(g, T(21, 50)), 1.2)
      && done.length === 1 && done[0].kind === 'point' && done[0].tone === 'dim' && same(done[0].mid, xAt(g, T(15, 0)), 1.2), JSON.stringify(mk));
    await a.hoverOn('.notch .dt-row[data-key="st-wait"]');
    check('the waiting one offers 先放着 and 同意 (the request came from the host\'s conversation)', (await a.rows()).find(r => r.key === 'st-wait').quick.join('|') === '先放着|同意');
    await a.pressOn('.notch .dt-row[data-key="st-wait"] .dt-quick button:first-child');
    await a.page.waitForTimeout(400);
    const metas = () => a.s.posts.filter(p => p.path === 'host:/sessions/st-wait/meta');
    check('先放着 is the host\'s own mark (parked true, archived false) and nothing goes to the daemon for it; the row is parked',
      metas().at(-1)?.body.parked === true && metas().at(-1).body.archived === false && !a.s.posts.some(p => p.path.startsWith('/inherent/agent-marks/')) && (await a.rows()).find(r => r.key === 'st-wait').tag === '先放着', JSON.stringify(a.s.posts));
    await a.hoverOn('.notch .dt-row[data-key="st-wait"]');
    await a.pressOn('.notch .dt-row[data-key="st-wait"] .dt-quick button:first-child');
    await a.page.waitForTimeout(400);
    check('拿回来 takes the mark back at the host (parked false), and the row waits again', metas().at(-1).body.parked === false && (await a.rows()).find(r => r.key === 'st-wait').tag === '等你');
    // its page reads the host's conversation
    await a.pressOn('.notch .dt-row[data-key="st-wait"] .dt-ic');
    await a.until(() => document.querySelector('.notch .dt-page[data-page="st-wait"] .m-it'), undefined, 6000);
    const pg = await a.page.evaluate(() => ({ you: [...document.querySelectorAll('.notch .dt-page .m-you')].map(e => e.innerText), it: [...document.querySelectorAll('.notch .dt-page .m-it')].map(e => e.innerText), at: document.querySelector('.notch .dt-ag-top p')?.innerText, ask: document.querySelector('.notch .dt-ag-ask')?.innerText }));
    check('its page is the host\'s conversation (his words and its answer), where it runs, and what it asks', pg.you.join() === '接上登录' && pg.it.join() === '登录接好了，我跑一下测试。' && pg.at === 'Claude · Startrail · main' && pg.ask === '想跑 npm test', JSON.stringify(pg));
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(500);
    await a.hoverOn('.notch .dt-row[data-key="st-wait"]');
    await a.pressOn('.notch .dt-row[data-key="st-wait"] .dt-quick button.is-yes');
    await a.page.waitForTimeout(500);
    const answers = a.s.posts.filter(p => p.path === 'host:/sessions/st-wait/answer');
    check('同意 answers through the agent host (POST /sessions/<id>/answer with the request and allow), not the daemon', answers.length === 1 && answers[0].body.req === 'sreq-1' && answers[0].body.decision === 'allow' && !a.s.posts.some(p => p.path.startsWith('/inherent/claude-requests/')) && (await a.rows()).find(r => r.key === 'st-wait').sub === 'Claude · 已同意', JSON.stringify(a.s.posts));
    await a.hoverOn('.notch .dt-row[data-key="st-done"]');
    await a.pressOn('.notch .dt-row[data-key="st-done"] .dt-quick button');
    check('打开 on a Startrail session opens the Agents window on it', (await a.page.evaluate(() => window.__jumps ?? [])).some(j => j[0] === 'agents' && j[1] === 'st-done'));
    await a.ctx.close();
  }

  {
    // ---- another day: only that day's Claude Code sessions, as finished ones ----
    const a = await boot();
    await a.openDay();
    const g0 = await a.geo();
    await a.door('agents');
    await a.page.keyboard.press('ArrowLeft');
    await a.until(() => document.querySelector('.notch .dt-date')?.textContent === '10月18日 周日' && document.querySelectorAll('.notch .dt-row[data-key]').length === 2, undefined, 6000); await a.page.waitForTimeout(600);
    const g1 = await a.geo(), list = await a.rows(), mk = await a.marks();
    check('the day before shows that day\'s Claude Code sessions from the day itself: oldest first, title or project, project and account, how long; no now line, no buttons',
      list.map(r => [r.key, r.at, r.title, r.sub, r.side, r.quick.length].join('/')).join('|') === 'code-aaa/10:00/改登录页/Claude · jarvis · A/1 时 30 分/0|code-bbb/10:30/typlus/Claude · typlus · B/20 分/0', JSON.stringify(list));
    check('their spans are dim and packed into lanes (the 10:30 one overlaps the 10:00 one, so it is in the second lane)',
      mk.length === 2 && mk.every(m => m.kind === 'span' && m.tone === 'dim') && mk[0].top === lanes[0] && mk[1].top === lanes[1] && same(mk[0].x, xAt(g1, T(10, 0, 18), 18), 1.2) && same(mk[0].x + mk[0].w, xAt(g1, T(11, 30, 18), 18), 1.2), JSON.stringify(mk));
    check('the line and the number say it is that day\'s, and the pane has not moved',
      (await a.texts('.notch .dt-line'))[0] === '这天开了 2 个会话。' && (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '做完 2' && moved(g0, g1).length === 0, moved(g0, g1).join('; '));
    await a.page.keyboard.press('ArrowLeft');
    await a.until(() => document.querySelector('.notch .dt-date')?.textContent === '10月17日 周六', undefined, 6000); await a.page.waitForTimeout(600);
    check('a day with none says so: the line, the page, and one number fewer', (await a.texts('.notch .dt-line'))[0] === '这天没开 Agents。' && (await a.texts('.notch .dt-say')).join() === '这天没有会话');
    await a.ctx.close();
  }

  {
    // ---- English; no Agents window to open; nothing at all ----
    const a = await boot({ lang: 'en', agentsWindow: false, claude: [], codex: [] });
    await a.openDay(); await a.door('agents');
    check('with no sessions the line says none was opened, there is no count on the door and no link in the app that cannot open the window',
      (await a.texts('.notch .dt-line'))[0] === 'No Agents today.' && (await a.texts('.notch .dt-say')).join() === 'No sessions today' && (await a.texts('.notch .dt-door[data-door="agents"]'))[0] === 'Agents' && await a.page.locator('.notch .dt-numbers .dt-go').count() === 0
      && (await a.texts('.notch .dt-numbers .dt-num')).join('|') === 'Running 0|Done today 0', (await a.texts('.notch .dt-numbers .dt-num')).join('|'));
    await a.ctx.close();
    const b = await boot({ lang: 'en' });
    await b.openDay(); await b.arrive(); await b.door('agents');
    const list = await b.rows();
    check('in English the line, the numbers, the tag and what the row says are English',
      (await b.texts('.notch .dt-line'))[0] === '时间线 is waiting for you; 1 more running.' && (await b.texts('.notch .dt-numbers .dt-num')).join('|') === 'Waiting 1|Running 1|Done today 3' && list.find(r => r.key === 'c-line').tag === 'Waiting'
      && list.find(r => r.key === 'c-line').sub === 'Claude · Wants to run npm test' && list.find(r => r.key === 'c-ci').sub === 'Claude · Done · CI 变绿了' && list.find(r => r.key === 'c-ci').side === '1 h 15 min' && (await b.texts('.notch .dt-numbers .dt-go'))[0] === 'Agents window', JSON.stringify(list.map(r => [r.key, r.tag, r.sub, r.side])));
    await b.pressOn('.notch .dt-numbers .dt-go');
    check('the link opens the Agents window', (await b.page.evaluate(() => window.__jumps ?? [])).some(j => j[0] === 'agents' && j[1] === ''));
    await b.ctx.close();
  }

  // =============================================== 待办 ===============================================
  {
    const a = await boot();
    await a.openDay();
    await a.until(() => document.querySelector('.notch .dt-door[data-door="todo"] em'));
    const g0 = await a.geo();
    check('the door counts the open to-dos', (await a.texts('.notch .dt-door[data-door="todo"]'))[0] === '待办 5' && await a.page.locator('.notch .dt-door[data-door="todo"] em.is-amber').count() === 0);
    await a.door('todo');
    const g1 = await a.geo();
    check('on 待办 the pane, its shape, the day, the head, the bar and the doors row are where they were on 今天 and the same size (584 pt)', moved(g0, g1).length === 0 && same(g1.dt.height, 584), moved(g0, g1).join('; '));
    check('the line puts what is past its time first (the earliest due, and how many more)', (await a.texts('.notch .dt-line'))[0] === '续借图书证 等 2 件过了时间还没做。', (await a.texts('.notch .dt-line')).join());
    check('the numbers: 待办 5, 日程 3, and 过期 2 in amber (a lens); no link (the app has nothing to open for it)',
      (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '待办 5|日程 3|过期 2' && await a.page.locator('.notch .dt-numbers button.dt-num.is-amber').count() === 1 && await a.page.locator('.notch .dt-numbers .dt-go').count() === 0, (await a.texts('.notch .dt-numbers .dt-num')).join('|'));
    const list = await a.rows(), by = Object.fromEntries(list.map(r => [r.key, r]));
    check('the rows: what has a time in time order with the now line among them, then the heading 没定时间 and the rest (all-day events first, then to-dos by due, none last)',
      list.map(r => r.key).join() === 'ev:ev-class,td:td-a3,now,ev:ev-mom,td:td-pay,heading,ev:ev-bday,td:td-old,td:td-read,td:td-lee' && list.slice(0, 5).map(r => r.at ?? r.text).join('|') === '14:00|17:00|现在 21:50|22:00|23:30' && list[5].text === '没定时间', JSON.stringify(list.map(r => [r.key, r.at])));
    check('an event says when it is (HH:MM–HH:MM, or 全天); one still to come is in the future look and says how long until it (within two hours); one that is over is faded',
      by['ev:ev-class'].sub === '14:00–15:20' && by['ev:ev-class'].cls.includes('is-read') && by['ev:ev-mom'].sub === '22:00–22:30' && by['ev:ev-mom'].cls.includes('is-future') && by['ev:ev-mom'].side === '还有 10 分' && by['ev:ev-bday'].sub === '全天' && by['ev:ev-bday'].at === '', JSON.stringify([by['ev:ev-class'], by['ev:ev-mom']]));
    check('a to-do past its time has the amber tag 过期; a date-only due is untimed and says its day in the time column (今天, or the date), none says nothing',
      by['td:td-a3'].tag === '过期' && by['td:td-old'].tag === '过期' && by['td:td-pay'].tag === '' && by['td:td-read'].at === '今天' && by['td:td-old'].at === '10/17' && by['td:td-lee'].at === '' && by['td:td-a3'].sub === '待办', JSON.stringify(list.filter(r => r.key.startsWith('td')).map(r => [r.key, r.at, r.tag])));
    check('the circle is an open circle on a to-do and the calendar glyph on an event', await a.page.evaluate(() => { const g = k => document.querySelector(`.notch .dt-row[data-key="${k}"] .dt-ic svg`)?.innerHTML.length ?? 0; return g('td:td-a3') > 0 && g('td:td-a3') !== g('ev:ev-class'); }));
    const mk = await a.marks(), ofKey = key => mk.find(m => m.key === key);
    check('the marks: a point at each to-do\'s due time (amber when it is past due and open, else lit) and a dim one at each timed event\'s start; nothing for what has no time',
      mk.length === 4 && ofKey('td:td-a3').tone === 'amber' && ofKey('td:td-pay').tone === 'on' && ofKey('ev:ev-class').tone === 'dim' && ofKey('ev:ev-mom').tone === 'dim'
      && [['td:td-a3', T(17, 0)], ['td:td-pay', T(23, 30)], ['ev:ev-class', T(14, 0)], ['ev:ev-mom', T(22, 0)]].every(([k, ms]) => ofKey(k).kind === 'point' && same(ofKey(k).mid, xAt(g1, ms), 1.2)), JSON.stringify(mk));
    await a.shot('todo', g1);

    // ---- tick ----
    await a.hoverOn('.notch .dt-row[data-key="td:td-a3"]');
    await a.page.waitForTimeout(250);
    check('a rest on a to-do offers 完成, and an event offers nothing', (await a.rows()).find(r => r.key === 'td:td-a3').quick.join() === '完成' && (await a.rows()).filter(r => r.key.startsWith('ev:')).every(r => r.quick.length === 0));
    await a.pressOn('.notch .dt-row[data-key="td:td-a3"] .dt-quick button');
    await a.page.waitForTimeout(400);
    const ticked = a.s.posts.filter(p => p.path === '/inherent/today/todo');
    const l1 = await a.rows(), t1 = l1.find(r => r.key === 'td:td-a3');
    check('完成 ticks it as the Dashboard does: POST /inherent/today/todo {id, done: true}',
      ticked.length === 1 && ticked[0].body.id === 'td-a3' && ticked[0].body.done === true, JSON.stringify(ticked));
    await a.hoverOn('.notch .dt-row[data-key="td:td-a3"]');
    await a.page.waitForTimeout(250);
    const l2 = await a.rows(), t2 = l2.find(r => r.key === 'td:td-a3'), mk2 = await a.marks();
    check('it stays as a faded row with a check in the circle and the way back (撤销), no amber tag, no mark; the numbers, the door and the line follow',
      t1.cls.includes('is-read') && t1.tag === '' && t2.quick.join() === '撤销' && !mk2.some(m => m.key === 'td:td-a3') && (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '待办 4|日程 3|过期 1'
      && (await a.texts('.notch .dt-door[data-door="todo"]'))[0] === '待办 4' && (await a.texts('.notch .dt-line'))[0] === '续借图书证 过了时间还没做。', JSON.stringify([t1, t2]));
    await a.pressOn('.notch .dt-row[data-key="td:td-a3"] .dt-quick button');
    await a.page.waitForTimeout(400);
    const undone = a.s.posts.filter(p => p.path === '/inherent/today/todo').at(-1);
    check('撤销 puts it back: POST done false, the row is open again with its amber tag and mark', undone.body.done === false && (await a.rows()).find(r => r.key === 'td:td-a3').tag === '过期' && (await a.marks()).some(m => m.key === 'td:td-a3' && m.tone === 'amber'), JSON.stringify(undone));
    await a.ctx.close();
  }

  {
    // ---- a tick the daemon cannot take; the lens ----
    const a = await boot();
    a.s.refuse.add('todo');
    await a.openDay(); await a.door('todo');
    await a.hoverOn('.notch .dt-row[data-key="td:td-pay"]');
    await a.pressOn('.notch .dt-row[data-key="td:td-pay"] .dt-quick button');
    await a.page.waitForTimeout(500);
    const back = (await a.rows()).find(r => r.key === 'td:td-pay'), told = { quick: back.quick, faded: back.cls.includes('is-read'), line: (await a.texts('.notch .dt-line'))[0], door: (await a.texts('.notch .dt-door[data-door="todo"]'))[0] };
    check('a tick that To Do cannot take is put back (open again, 完成 on offer, counted), and the page\'s line says so', told.quick.join() === '完成' && !told.faded && told.line === '连不上 To Do。' && told.door === '待办 5', JSON.stringify(told));
    await a.hoverOn('.notch .dt-head');
    await a.hoverOn('.notch .dt-numbers button.dt-num');
    await a.page.waitForTimeout(300);
    const lens = await a.rows();
    check('the 过期 chip is a lens: the rows it keeps (the two to-dos past their time) are not stepped back, the rest are', lens.filter(r => r.cls?.includes('is-off')).map(r => r.key).sort().join() === 'ev:ev-bday,ev:ev-class,ev:ev-mom,td:td-lee,td:td-pay,td:td-read', JSON.stringify(lens.map(r => [r.key, r.cls?.includes('is-off')])));
    await a.ctx.close();
  }

  {
    // ---- the line when nothing is past its time; when everything is done; other days; no route ----
    const t0 = baseToday();
    t0.todos = t0.todos.filter(x => ['td-pay', 'td-lee'].includes(x.id));
    const a = await boot({ today: t0 });
    await a.openDay(); await a.door('todo');
    check('with nothing past its time the line says how many things are left today and the first two (a still-to-come event counts; one that is over does not)',
      (await a.texts('.notch .dt-line'))[0] === '今天还有 3 件：和妈妈通话、交房租 等。' && (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '待办 2|日程 3', (await a.texts('.notch .dt-line')).join());
    await a.page.keyboard.press('ArrowLeft');
    await a.until(() => document.querySelector('.notch .dt-date')?.textContent === '10月18日 周日', undefined, 6000); await a.page.waitForTimeout(600);
    const g = await a.geo();
    check('another day says that to-dos are for today only, with no rows, no marks and no numbers; the door still counts',
      (await a.texts('.notch .dt-line'))[0] === '待办只看今天的。' && (await a.keys()).length === 0 && (await a.marks()).length === 0 && (await a.texts('.notch .dt-numbers .dt-num')).length === 0 && (await a.texts('.notch .dt-door[data-door="todo"]'))[0] === '待办 2' && same(g.dt.height, 584));
    await a.ctx.close();
    const t1 = { weather: null, events: [], todos: [] };
    const b = await boot({ today: t1 });
    await b.openDay(); await b.door('todo');
    check('with nothing to do the line says it is all done and the page says the list is empty', (await b.texts('.notch .dt-line'))[0] === '今天的都做完了。' && (await b.texts('.notch .dt-say')).join() === '今天没有事' && (await b.texts('.notch .dt-door[data-door="todo"]'))[0] === '待办');
    await b.ctx.close();
    const c = await boot({ today: null });
    await c.openDay(); await c.door('todo');
    check('a daemon that has no such route says so, with no line and no rows', (await c.texts('.notch .dt-say')).join() === '这台 Jarvis 还不会给待办' && (await c.texts('.notch .dt-line'))[0] === '');
    await c.ctx.close();
    const d = await boot({ lang: 'en' });
    await d.openDay(); await d.door('todo');
    const list = await d.rows();
    check('in English the line, the numbers, the heading and the rows are English',
      (await d.texts('.notch .dt-line'))[0] === '续借图书证 and 1 more are overdue.' && (await d.texts('.notch .dt-numbers .dt-num')).join('|') === 'To-dos 5|Events 3|Overdue 2' && list.find(r => r.key === 'heading').text === 'No set time'
      && list.find(r => r.key === 'ev:ev-mom').side === 'in 10 min' && list.find(r => r.key === 'td:td-a3').tag === 'Overdue' && list.find(r => r.key === 'ev:ev-bday').sub === 'All day' && list.find(r => r.key === 'td:td-read').at === 'Today', JSON.stringify(list.map(r => [r.key, r.tag, r.side])));
    await d.ctx.close();
  }

  check('no page error from any of it', errors.length === 0, errors.join(' | '));
  console.log(`\n${checks.length} checks passed; screenshots in ${out}`);
} catch (e) {
  if (errors.length) console.error(`page errors: ${errors.join(' | ')}`);
  throw e;
} finally {
  await browser.close();
  server.kill();
}

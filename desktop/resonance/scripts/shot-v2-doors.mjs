// WP4 (the V2 doors) in pictures: 信, 待办, Agents, 额度 and 更多 in the two-column pane (820 pt on a stage of 836), each with nothing selected and with a row selected,
// plus checks on what the right column holds and on the doors row. Headless Chrome against the built page (run `npx vite build` first) with the native bridge stubbed and a
// fake daemon that serves a day, letters, sessions, to-dos, usage and its history, memory and settings, as scripts/verify-day-doors-*.mjs do. PNGs land in $OUT (default verify-out/v2-doors/).
// DOORS_REST=1 is for a DayTimeline that selects a page's `rest` row on entering the door (see doors.ts); without it the checks that need that are skipped.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = process.env.OUT ?? path.join(root, 'verify-out/v2-doors');
mkdirSync(out, { recursive: true });
const web = Number(process.env.COMPANION_PORT ?? 5189), daemonPort = Number(process.env.COMPANION_DAEMON_PORT ?? 8789), daemon = `http://127.0.0.1:${daemonPort}`;
const REST = process.env.DOORS_REST === '1';
const STAGE = 836;
const errors = [], check = (name, pass, detail = '') => { assert.ok(pass, `${name} ${detail}`); console.log('ok', name); };
const skip = name => console.log('skip', name);

// ---------- the day: 2026-10-19 in Vancouver (PDT, UTC-7), now 21:50 ----------
const DATE = '2026-10-19', TZ = 'America/Vancouver';
const T = (h, m = 0, day = 19) => Date.UTC(2026, 9, day, h + 7, m);
const NOW = T(21, 50), iso = (h, m, day = 19) => `2026-10-${day}T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00-07:00`;
const it = (id, kind, start, end, extra = {}, lane = 'you') => ({ id, lane, kind, start_ms: start, end_ms: end, state: end === null ? (start > NOW ? 'next' : 'now') : end <= NOW ? 'done' : start > NOW ? 'next' : 'now', ...extra });
const home = { id: 'p-home', name: '家', role: 'home' }, uvic = { id: 'p-uvic', name: 'UVic', role: 'school' };
const day = () => ({
  date: DATE, tz: TZ, start_ms: T(0), end_ms: T(24), now_ms: NOW,
  items: [
    it('sleep-1', 'sleep', T(0, 55), T(9, 5), { minutes: 470 }),
    it('stay-home-1', 'stay', T(9, 5), T(11, 25), { place: '12 Example St, Victoria', kept_place: home }),
    it('move-1', 'move', T(11, 25), T(13, 5), { activity: 'automotive' }),
    it('stay-uvic', 'stay', T(13, 15), T(18, 15), { place: 'University of Victoria, Victoria', kept_place: uvic }),
    it('stay-home-3', 'stay', T(18, 50), null, { place: '12 Example St, Victoria', kept_place: home }),
    it('work-2', 'work', T(19, 35), T(21, 40), { active_s: 6900, projects: [{ name: 'Jarvis', seconds: 6000 }], apps: [{ name: 'Ghostty', seconds: 4200 }] }),
  ],
  labels: [], missing: [], line: { text: '今天一半在路上，一半在 UVic。', draft: false, written_at: '2026-10-19T21:30:00-07:00' }, highlights: [],
});
// ---------- letters ----------
const mail = (id, from, subject, received, extra = {}) => ({ id, from, address: `${id}@example.com`, thread_id: `th-${id}`, subject, received, reply: null, junk: false, importance: 1, category: null, ...extra });
const LETTERS = [
  mail('m-nw', 'Northwind Recruiting', 'Interview times for next week', iso(9, 40), { reply: 'yes' }),
  mail('m-reg', 'UVic Registrar', 'Your fall schedule', iso(11, 2), { reply: 'fyi' }),
  mail('m-shop', 'ShopDeals', 'Everything 40% off this week', iso(13, 20), { junk: true }),
  mail('m-mom', '妈妈', '周末回家吃饭吗？', iso(15, 5), { reply: 'yes' }),
  mail('m-st4', 'Studio 4', '今晚的陶艺课还来吗？', iso(16, 5), { reply: 'yes' }),
  mail('m-gh', 'GitHub', '[jarvis] Run failed: verify', iso(16, 30), { reply: 'fyi' }),
  mail('m-lib', 'UVic Library', 'Book due Friday', iso(18, 40), { reply: 'fyi' }),
  mail('m-old', 'Studio Lin', 'Invoice 1042', iso(10, 0, 17)),
];
// ---------- sessions ----------
const claude = (id, extra = {}) => ({ agent: 'claude', session_id: id, kind: 'interactive', phase: 'done', title: id, project: 'jarvis', branch: 'main', cwd: '/x/jarvis', where: 'Ghostty', prompt: `do ${id}`, activity: '', last_message: '',
  asks: null, replyable: false, request: null, started_ms: null, updated_ms: NOW, from_project: false, ...extra });
const codex = (id, extra = {}) => ({ session_id: id, state: 'finished', cwd: '/x/jarvis', model: '', prompt: id, detail: '', last_message: '', since_ms: NOW, ...extra });
const BASH = { id: 'req-1', tool: 'Bash', input: { command: 'npm test', description: 'run the tests' }, cwd: '/x/jarvis', always: '' };
const SESSIONS = [
  claude('c-ci', { title: '修 CI', phase: 'done', last_message: 'CI 变绿了', started_ms: T(16, 10), updated_ms: T(17, 25) }),
  claude('c-line', { title: '时间线', phase: 'needs_input', request: BASH, started_ms: T(19, 40), updated_ms: T(21, 40), prompt: '跑一下测试' }),
  claude('c-exam', { title: '工具考试', phase: 'working', activity: '第 3 轮 · 41 / 60', started_ms: T(20, 15), updated_ms: T(21, 49) }),
];
const CODEX = [codex('r-readme', { prompt: '改 README', last_message: '改了 2 个文件', since_ms: T(10, 40) }), codex('r-tests', { prompt: '整理测试', last_message: '6 个测试快了一倍', since_ms: T(21, 5) })];
const CONVERSATION = [{ who: 'you', text: '跑一下测试' }, { who: 'it', text: '好的，我先跑测试，再看结果。' }];
// ---------- today's calendar and to-dos ----------
const TODAY = {
  weather: null,
  events: [{ id: 'ev-class', title: '线性代数小测', start: iso(15, 30), end: iso(16, 20) }, { id: 'ev-pot', title: '陶艺课', start: iso(22, 0), end: iso(23, 0) }],
  todos: [{ id: 'td-lib', title: '续借图书证', due: iso(23, 59, 18) }, { id: 'td-pill', title: '吃药', due: iso(21, 30) }, { id: 'td-mia', title: '给 Mia 回电话', due: iso(23, 0) }, { id: 'td-la', title: '交线性代数作业', due: iso(23, 59) }],
};
// ---------- usage ----------
const Z = ms => new Date(ms).toISOString().replace('.000Z', '+00:00');
const win = (key, label, percent, resets) => ({ key, label, percent, resets_at: resets === null ? null : Z(resets) });
const five = (percent, reset) => win('five_hour', '5 小时', percent, reset);
const week = percent => win('seven_day', '7 天 · 总', percent, T(0, 0, 26));
const reading = (at, windows) => ({ at_ms: at, status: 'ok', windows });
const R = { w1: T(15, 30), w2: T(21, 10), w3: T(2, 10, 20) };
const STAIRS = [
  [R.w1, [[T(10, 35), 3], [T(10, 50), 8], [T(11, 20), 15]]],
  [R.w2, [[T(16, 15), 2], [T(17, 25), 30], [T(20, 30), 54], [T(21, 5), 64]]],
  [R.w3, [[T(21, 12), 3], [T(21, 38), 27], [T(21, 50), 38]]],
];
const POOL = { claude: STAIRS.flatMap(([reset, steps]) => steps.map(([at, p]) => reading(at, [five(p, reset), week(61)]))), codex: [] };
const svc = (data, status = 'ok') => ({ status, observed_at_ms: NOW, data });
const USAGE = { services: {
  claude: svc({ plan: '20X', windows: [five(38, R.w3), week(61)] }),
  codex: svc({ plan: 'Pro', reset_credits: 0, windows: [win('primary_window', '7 天', 24, T(0, 0, 26))] }),
  openai: svc({ today_usd: 0.84, month_usd: 12.6, by_model: [], by_key: [] }), anthropic: svc({ today_usd: 0.31, month_usd: 4.2, by_model: [], official: { today_usd: 2.36, month_usd: 25.4, by_model: [] } }),
  deepseek: svc({ balances: { CNY: 58 } }),
} };
// ---------- 更多 ----------
const ITEMS = [
  { id: 'k1', section: '关于我', text: '你在 UVic 读计算机，这学期有线性代数。', pinned: false }, { id: 'k2', section: '关于我', text: '妈妈周末常叫你回家吃饭。', pinned: true },
  { id: 'k3', section: '习惯', text: '睡前不看手机。', pinned: false }, { id: 'k4', section: '习惯', text: '周六和朋友吃饭。', pinned: false },
];
const MEMORY = {
  version: { id: 'v3', ts: iso(5, 0), origin: 'nightly' }, items: ITEMS.length, days: 3, chars: 90, max_chars: 4000, booted_max_chars: 40000,
  sections: ['关于我', '习惯'].map(name => ({ name, items: ITEMS.filter(i => i.section === name).map(({ id, text, pinned }) => ({ id, text, pinned })) })),
  new: { day: '2026-10-18', ts: iso(5, 0), version: 'v2', entries: [{ kind: 'add', id: 'k3', section: '习惯', text: '睡前不看手机。', quote: null }, { kind: 'rewrite', id: 'k4', section: '习惯', text: '周六和朋友吃饭。', before: '周日和朋友吃饭。', quote: null }] },
};
const BRIEF = { date: DATE, summary: '今天交作业，下午去 UVic。', items: 2, sections: [{ key: 'items', title: '今天', rows: [{ text: '交作业' }, { text: '下午去 UVic' }] }] };
const SETTINGS = { values: { reply_language: 'follow', wake_threshold: .95, tts_voice: 'Warm Bestie', tts_volume: 1, conversation_model: 'sonnet', conversation_effort: 'low' }, options: { conversation_model: ['opus', 'sonnet', 'haiku'], conversation_effort: { sonnet: ['low', 'medium', 'high'] }, tts_voice: ['Warm Bestie'] }, defaults: {} };

// ---------- the built page ----------
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const exe = [process.env.CHROMIUM, '/opt/pw-browsers/chromium'].find(p => p && existsSync(p));
const browser = await chromium.launch({ headless: true, ...exe ? { executablePath: exe } : { channel: 'chrome' }, args: ['--disable-web-security', '--mute-audio'] });
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const ctx = await browser.newContext({ viewport: { width: STAGE, height: 760 }, deviceScaleFactor: 2, timezoneId: TZ });
  const page = await ctx.newPage();
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
  await page.addInitScript(([width]) => {
    localStorage.setItem('companion-settings-v1', JSON.stringify({ lang: 'zh' }));
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: width, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {}, wearing: () => {}, onDictation: () => () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, onGhostty: () => () => {}, onAgentsPresence: () => () => {}, onMouseDown: () => () => {}, onClaudeFront: () => () => {}, plugins: async () => ({}),
      openMail: async () => true, openAgents: () => {}, jumpGhostty: async () => {}, openCodex: async () => {},
    };
    window.WebSocket = class { constructor() { setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
  }, [STAGE]);
  // the waiting session comes once the day is open (a card on the island would keep the day from opening)
  const live = { waiting: false };
  await page.route(`${daemon}/**`, async route => {
    const req = route.request(), url = new URL(req.url()), p = url.pathname, post = req.method() === 'POST';
    const json = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    let m;
    if (p === '/inherent/day') return json(day());
    if (p === '/inherent/mail' && !post) return json({ unread: LETTERS });
    if (/^\/inherent\/mail\/[^/]+\/(draft|summary)$/.test(p)) return json({ detail: 'none' }, 404);
    if (!post && (m = p.match(/^\/inherent\/mail\/([^/]+)$/))) {
      const l = LETTERS.find(x => x.id === decodeURIComponent(m[1]));
      return l ? json({ ...l, to: 'me@example.com', text: `${l.subject}? 你爸说想吃你做的红烧肉。要是忙就下周，提前跟我说一声。`, layout: 'text' }) : json({ detail: 'no such letter' }, 404);
    }
    if (p === '/inherent/today' && !post) return json(TODAY);
    if (p === '/inherent/claude-sessions') return json({ sessions: live.waiting ? SESSIONS : SESSIONS.filter(s => s.phase !== 'needs_input'), error: null });
    if (p === '/inherent/codex-sessions') return json({ sessions: CODEX });
    if (!post && /\/conversation$/.test(p)) return json({ messages: CONVERSATION });
    if (p === '/inherent/usage') return json(USAGE);
    if (p === '/inherent/usage/history') {
      const from = +url.searchParams.get('from_ms'), to = +url.searchParams.get('to_ms');
      return json({ services: { claude: POOL.claude.filter(r => r.at_ms >= from && r.at_ms <= to) } });
    }
    if (p === '/inherent/memory') return json(MEMORY);
    if (p === '/inherent/brief') return json(BRIEF);
    if (p === '/inherent/settings') return json(SETTINGS);
    if (p === '/inherent/view') return json({ ok: true });
    if (p === '/inherent/controls') return json({ mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' });
    if (p === '/inherent/agent-marks') return json({ marks: {} });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });
  await page.clock.setFixedTime(new Date(NOW));
  await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${daemonPort}`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
  const move = async (x, y) => { await page.mouse.move(x, y); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [x, y]); };
  const rect = sel => page.evaluate(sel => { const e = document.querySelector(sel); return e ? e.getBoundingClientRect().toJSON() : null; }, sel);
  const press = async (x, y) => { await move(x, y); await page.mouse.down(); await page.mouse.up(); };
  const pressOn = async sel => { const r = await rect(sel); assert.ok(r, `${sel} is on the page`); await press(r.x + r.width / 2, r.y + r.height / 2); };
  const settle = ms => page.waitForTimeout(ms);
  const park = () => move(STAGE - 70, 560);
  const sel = () => page.evaluate(() => document.querySelector('.notch .dt-list [aria-current="true"]')?.dataset.key ?? null);
  const doorOn = () => page.evaluate(() => document.querySelector('.notch .dt')?.dataset.door);
  const shot = async name => { const g = await rect('.notch-drop'); await page.screenshot({ path: path.join(out, `${name}.png`), clip: { x: 0, y: 0, width: STAGE, height: Math.ceil(g.bottom) + 12 } }); };
  const side = () => page.evaluate(() => { const s = document.querySelector('.notch .dt-side'); return { n: s?.children.length ?? -1, text: (s?.innerText ?? '').replace(/\s+/g, ' ').trim() }; });
  const stageVar = () => page.evaluate(() => { const l = document.querySelector('.notch .dt-list')?.getBoundingClientRect(), s = document.querySelector('.notch .dt-side')?.getBoundingClientRect(); return { l: l?.width, r: s?.width }; });
  const go = async (n, key) => { await page.keyboard.press(`Control+${n}`); await settle(800); check(`⌘${n} is the door ${key}`, await doorOn() === key); await park(); await settle(300); };

  await page.waitForFunction(() => document.querySelector('.notch')?.dataset.line, undefined, { timeout: 8000 });
  const [a0, a1] = (await page.evaluate(() => document.querySelector('.notch').dataset.lineAt)).split('-').map(Number);
  await press((a0 + a1) / 2, 14);
  await page.waitForSelector('.notch-drop.is-open .dt-row', { timeout: 8000 });
  await settle(1200);
  live.waiting = true; await settle(2500);
  await park();

  // ---- 信 ----
  await go(2, 'mail');
  const mailRows = await page.locator('.notch .dt-list .dt-row').count();
  check('信: the letters are the list on the left', mailRows >= 8, String(mailRows));
  if (REST) check('信: nothing selected shows the letter that most wants a reply in the right column (the newest 要回)', await sel() === 'm-st4' && (await side()).text.includes('今晚的陶艺课'), `${await sel()} ${(await side()).text}`);
  else skip('信 rest');
  await shot('mail-rest');
  await pressOn('.notch .dt-list .dt-row[data-key="m-mom"]'); await settle(900); await park(); await settle(200);
  check('信: a letter selected shows its page in the right column', await sel() === 'm-mom' && (await side()).text.includes('周末回家吃饭吗'), (await side()).text);
  await shot('mail-selected');
  if (process.env.DEBUG_JS) console.log(JSON.stringify(await page.evaluate(process.env.DEBUG_JS)));

  // ---- 待办 ----
  await go(3, 'todo');
  if (REST) check('待办: nothing selected shows the first overdue to-do', await sel() === 'td:td-lib' && (await side()).text.includes('续借图书证'), `${await sel()} ${(await side()).text}`);
  else skip('待办 rest');
  await shot('todo-rest');
  await pressOn('.notch .dt-list .dt-row[data-key="td:td-pill"]'); await settle(900); await park(); await settle(200);
  check('待办: a to-do selected shows buttons in the right column', await sel() === 'td:td-pill' && await page.locator('.notch .dt-side button').count() >= 2, (await side()).text);
  await shot('todo-selected');
  await pressOn('.notch .dt-side .dd-head .dt-ask'); await settle(300);
  check('待办: 问她 in the head opens one line to type into', await page.locator('.notch .dt-side .dd-ask input').count() === 1 && await page.evaluate(() => document.activeElement?.closest('.dd-ask') !== null));
  await page.keyboard.press('Escape'); await settle(300);
  check('...and Esc leaves it (the frame lets go of the selection with it)', await page.locator('.notch .dt-side .dd-ask').count() === 0);
  await park();

  // ---- Agents ----
  await go(4, 'agents');
  if (REST) check('Agents: nothing selected shows the session that waits on him', await sel() === 'c-line', String(await sel()));
  else skip('Agents rest');
  await shot('agents-rest');
  await pressOn('.notch .dt-list .dt-row[data-key="c-exam"]'); await settle(900); await park(); await settle(200);
  check('Agents: a session selected shows its talk in the right column', await sel() === 'c-exam' && await page.locator('.notch .dt-side .dt-ag').count() === 1);
  await shot('agents-selected');

  // ---- 额度 ----
  await go(5, 'quota');
  if (REST) check('额度: nothing selected shows the current 5-hour window', (await side()).text.includes('38%'), (await side()).text);
  else skip('额度 rest');
  await shot('quota-rest');
  const rowsQ = await page.locator('.notch .dt-list .dt-row').count();
  await pressOn('.notch .dt-list .dt-row:not(.is-future)'); await settle(900); await park(); await settle(200);
  check('额度: a window selected shows its page in the right column', rowsQ >= 3 && (await side()).text.includes('%'), (await side()).text);
  await shot('quota-selected');

  // ---- 更多 ----
  await go(6, 'more');
  const w = await stageVar();
  check('更多: the left column is the narrow menu (184 px)', Math.abs(w.l - 184) < 3 && w.r > 560, JSON.stringify(w));
  if (REST) check('更多: nothing selected shows the memory page', (await side()).text.includes('记着'), (await side()).text);
  else skip('更多 rest');
  await shot('more-rest');
  if (process.env.DEBUG_MORE) console.log(JSON.stringify(await page.evaluate(() => [...document.querySelector('.notch .dt').children].map(e => [e.className.slice(0, 40), Math.round(e.getBoundingClientRect().height)]))));
  await pressOn('.notch .dt-list .dt-row[data-key="memory"]'); await settle(1200); await park(); await settle(200);
  check('更多: 记忆 selected shows the memory page in the right column', await sel() === 'memory' && (await side()).text.includes('记着'), (await side()).text);
  await shot('more-selected');
  await pressOn('.notch .dt-list .dt-row[data-key="settings"]'); await settle(1200); await park();
  await shot('more-settings');

  // ---- the doors row ----
  const d = await page.evaluate(() => [...document.querySelectorAll('.notch .dt-door')].map(e => ({ k: e.dataset.door, h: e.getBoundingClientRect().height, on: e.classList.contains('is-on'), em: e.querySelector('em')?.textContent ?? '' })));
  check('the doors row: six doors, 26 px high, one lit (更多), counts on 信 待办 Agents 额度', d.length === 6 && d.every(x => Math.abs(x.h - 26) < .5) && d.filter(x => x.on).length === 1 && d.find(x => x.on).k === 'more' && !!d.find(x => x.k === 'quota').em, JSON.stringify(d));
  await page.keyboard.press('Control+1'); await settle(600);
  check('⌘1 is back on 今天', await doorOn() === 'today');
  check('no errors on the page', errors.length === 0, errors.join(' | '));
  await ctx.close();
} finally {
  await browser.close();
  server.kill();
}

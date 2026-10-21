// The 更多 door of the day pane (src/doors/more.tsx): the old Dashboard's other pages, each opened alone in the pane (AroundDashboard `only`, DayTimeline.tsx `bare`). Headless Chrome against
// the built page with the native bridge stubbed and a fake daemon that serves a day and what the opened pages read. Run after `npm run build`.
// The pages the door lists are opened one at a time: the pane is the size it is on 今天 throughout, the page's own ‹ and Esc go back by its steps (an item, a category, a field) and then to the
// list, and a page reads only what it shows. Then a build of its own (a stand-in for the 额度 door that opens every Dashboard page through `ctx.tool`, in a temp folder; `dist` is not touched)
// shows that every `Page` renders inside the pane, `usage` among them.
// Screenshots (2x) of the list and of 设置 land in $OUT (default verify-out/doors-more/, not committed).
import { chromium } from 'playwright';
import { build } from 'vite';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = process.env.OUT ?? path.join(root, 'verify-out/doors-more');
mkdirSync(out, { recursive: true });
const web = Number(process.env.COMPANION_PORT ?? 5431), webAll = Number(process.env.COMPANION_PORT_ALL ?? web + 10), daemonPort = Number(process.env.COMPANION_DAEMON_PORT ?? 9031), daemon = `http://127.0.0.1:${daemonPort}`;
const checks = [], errors = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name} ${detail}`); checks.push(name); console.log('ok', name); };
const same = (a, b, d = .5) => Math.abs(a - b) <= d;

// ---------- the day: 2026-10-19 in Vancouver (PDT, UTC-7), now 21:50 ----------
const DATE = '2026-10-19', TZ = 'America/Vancouver';
const T = (h, m = 0, day = 19) => Date.UTC(2026, 9, day, h + 7, m);
const NOW = T(21, 50);
const iso = (h, m, day = 19) => `2026-10-${day}T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00-07:00`;
const it = (id, kind, start, end, extra = {}, lane = 'you') => ({ id, lane, kind, start_ms: start, end_ms: end, state: end === null ? (start > NOW ? 'next' : 'now') : end <= NOW ? 'done' : start > NOW ? 'next' : 'now', ...extra });
const home = { id: 'p-home', name: '家', role: 'home' };
const dayReply = date => ({
  date, tz: TZ, start_ms: T(0), end_ms: T(24), now_ms: NOW,
  items: [
    it('sleep-1', 'sleep', T(0, 55), T(9, 5), { minutes: 470 }),
    it('stay-home-1', 'stay', T(9, 5), T(18, 15), { place: '12 Example St, Victoria', kept_place: home }),
    it('talk-2', 'talk', T(19, 30), T(19, 36), { turns: 4 }, 'her'),
  ],
  labels: [], missing: [], line: { text: '今天一半在路上，一半在 UVic。', draft: false, written_at: '2026-10-19T21:30:00-07:00' }, highlights: [],
});

// ---------- what the pages read ----------
const BRIEF = { date: DATE, summary: '今天有两件事：交作业，下午去 UVic。', items: 2, sections: [{ key: 'items', title: '今天', rows: [{ text: '交作业' }, { text: '下午去 UVic' }] }] };
const ITEMS = [
  { id: 'k1', section: '关于我', text: '住在维多利亚，在 UVic 读书。', pinned: false }, { id: 'k2', section: '关于我', text: '早上先看邮件。', pinned: true },
  { id: 'k3', section: '习惯', text: '睡前不看手机。', pinned: false }, { id: 'k4', section: '习惯', text: '周六和朋友吃饭。', pinned: false },
];
const memoryOverview = () => ({
  version: { id: 'v3', ts: iso(5, 0), origin: 'nightly' }, items: ITEMS.length, days: 3, chars: 90, max_chars: 4000, booted_max_chars: 40000,
  sections: ['关于我', '习惯'].map(name => ({ name, items: ITEMS.filter(i => i.section === name).map(({ id, text, pinned }) => ({ id, text, pinned })) })),
  new: { day: '2026-10-18', ts: iso(5, 0), version: 'v2', entries: [{ kind: 'add', id: 'k3', section: '习惯', text: '睡前不看手机。', quote: null }, { kind: 'rewrite', id: 'k4', section: '习惯', text: '周六和朋友吃饭。', before: '周日和朋友吃饭。', quote: null }] },
});
const SETTINGS = {
  values: { reply_language: 'follow', final_asr_language: '', wake_threshold: .95, tts_voice: 'Warm Bestie', tts_volume: 1, output_device: 'System default', input_device: 'System default', gpt_live: true, timesink: true, keep_audio: true,
    conversation_model: 'sonnet', conversation_effort: 'low', board_light: 'direction', board_brightness: .4, board_speed: 8, board_color: '#002040' },
  options: { conversation_model: ['opus', 'sonnet', 'haiku'], conversation_effort: { sonnet: ['low', 'medium', 'high'] }, tts_voice: ['Warm Bestie'], output_device: ['System default'], input_device: ['System default'] }, defaults: {},
};
const JOBS = { ledger: [], applications: [{ id: 'a1', company: 'Northwind', role: 'Designer', status: 'interviewing', status_auto: true, applied_at: '2026-10-10', last_at: iso(9, 12), count: 1, note: '', source: 'mail',
  mails: [{ message_id: 'jm1', kind: 'interview', received_at: iso(9, 12), subject: 'Interview times — please confirm' }] }], skipped: [], rules: [] };
const days7 = Array.from({ length: 7 }, (_, i) => `2026-10-${13 + i}`);
const PROJECTS = { days: days7, projects: [{ id: 'jarvis', name: 'Jarvis', seconds: 41_400, today_seconds: 5_400, days: [7_200, 3_600, 9_000, 5_400, 7_200, 3_600, 5_400], last_seen: iso(20, 0), commits: { count: 3, items: [] }, recent: [{ app: 'Ghostty', label: 'cc | jarvis', seconds: 1_260, last_seen: iso(20, 0) }] }],
  other: { seconds: 21_600, days: [3_000, 3_000, 3_000, 3_000, 3_000, 3_000, 3_600], count: 140 }, unsorted: { seconds: 0, days: [0, 0, 0, 0, 0, 0, 0], count: 0 },
  coverage: { timesink: 'available', git: 'available' }, latest_observed_at: iso(21, 40), sorted_at: iso(21, 41), refreshing: false, outcome: 'reused', error: null };
const USAGE = { services: { claude: { status: 'ok', observed_at_ms: NOW, data: { plan: 'Max', windows: [{ key: 'five_hour', label: '5 小时', percent: 38, resets_at: iso(23, 10) }, { key: 'seven_day', label: '7 天', percent: 61, resets_at: iso(23, 10, 21) }] } } } };

// ---------- the pages the door lists ----------
const PAGES = [['conversation', '聊天记录', '对话'], ['memory', '记忆', '记忆'], ['projects', '项目', '项目'], ['plugins', '插件', '插件'], ['settings', '设置', '设置'], ['brief', '早报', '早报'], ['jobs', '求职邮件', '求职邮件']];
const ALL_PAGES = ['conversation', 'now', 'agents', 'usage', 'plugins', 'projects', 'settings', 'arrange', 'brief', 'mail', 'memory', 'jobs'];
const ALL_TITLES = { conversation: '对话', now: '现在', agents: 'Agents', usage: '用量', plugins: '插件', projects: '项目', settings: '设置', arrange: '编辑首页', brief: '早报', mail: '邮件', memory: '记忆', jobs: '求职邮件' };

// ---------- the built page, and a build of its own whose 额度 door opens every page of the Dashboard ----------
const vite = path.join(root, 'node_modules/.bin/vite');
const servers = [spawn(vite, ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' })];
const everyPage = mkdtempSync(path.join(os.tmpdir(), 'doors-more-'));
const STAND_IN = `import type { DoorCtx, DoorPage } from '../doors';
const PAGES = ${JSON.stringify(ALL_PAGES)} as const;
export function useQuotaDoor({ tool }: DoorCtx): DoorPage | null {
  return { count: null, line: 'every page', chips: [], marks: [], nowAt: null, rows: PAGES.map(p => ({ key: p, at: null, icon: p[0], title: p, opens: true })),
    detail: (key, back) => tool ? { title: key, body: tool(key as (typeof PAGES)[number], back), bare: true } : null };
}`;
await build({ root, configFile: false, logLevel: 'silent', base: './', build: { outDir: everyPage, emptyOutDir: true, rollupOptions: { input: { index: path.join(root, 'index.html') } } },
  plugins: [{ name: 'every-page-door', enforce: 'pre', transform: (_code, id) => id.split('?')[0].endsWith('/src/doors/quota.tsx') ? { code: STAND_IN, map: null } : null }] });
servers.push(spawn(vite, ['preview', '--port', String(webAll), '--strictPort', '--outDir', everyPage], { cwd: root, stdio: 'ignore' }));
// CHROMIUM names a browser to use; else the one a container keeps for Playwright; else this Mac's Chrome.
const exe = [process.env.CHROMIUM, '/opt/pw-browsers/chromium'].find(p => p && existsSync(p));
const browser = await chromium.launch({ headless: true, ...exe ? { executablePath: exe } : { channel: 'chrome' }, args: ['--disable-web-security', '--mute-audio'] });

// A page on the fake daemon. `s.gets`: what the page read (path and query); `s.posts`: what it sent; `s.views`: what it told the daemon is on screen (ADR 0176).
async function boot({ lang = 'zh', at = web } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2, timezoneId: TZ });
  const page = await ctx.newPage();
  const s = { gets: [], posts: [], views: [] };
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
  await page.addInitScript(([settings]) => {
    localStorage.setItem('companion-settings-v1', JSON.stringify(settings));
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {}, wearing: () => {}, onDictation: () => () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, onGhostty: () => () => {}, onAgentsPresence: () => () => {}, onMouseDown: () => () => {}, onClaudeFront: () => () => {}, plugins: async () => ({}),
    };
    window.WebSocket = class { constructor() { setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
  }, [{ lang }]);
  await page.route(`${daemon}/**`, async route => {
    const req = route.request(), url = new URL(req.url()), post = req.method() === 'POST', p = url.pathname, body = post ? JSON.parse(req.postData() || '{}') : null;
    const json = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    let m;
    if (post) { s.posts.push({ path: p, body }); if (p === '/inherent/view') s.views.push(body); } else s.gets.push(p + url.search);
    if (p === '/inherent/day') return json(dayReply(url.searchParams.get('date') || DATE));
    if (p === '/inherent/brief') return json(BRIEF);
    if (p === '/inherent/memory') return json(memoryOverview());
    if ((m = p.match(/^\/inherent\/memory\/item\/(\w+)$/))) {
      const i = ITEMS.find(x => x.id === m[1]);
      return i ? json({ id: i.id, section: i.section, text: i.text, pinned: i.pinned, number: ITEMS.indexOf(i) + 1, chars: i.text.length, sources: [], born: { ts: iso(5, 0), origin: 'nightly', day: '2026-10-18' }, edits: [], edit_count: 0, reminder: false }) : json({ detail: 'gone' }, 404);
    }
    if (p === '/inherent/settings') return json(SETTINGS);
    if (p === '/inherent/jobs') return json(JOBS);
    if (p === '/inherent/projects') return json(PROJECTS);
    if (p === '/inherent/projects/refresh') return json(PROJECTS);
    if (p === '/inherent/usage') return json(USAGE);
    if (p === '/inherent/view') return json({ ok: true });
    if (p === '/inherent/controls') return json({ mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' });
    if (p === '/inherent/claude-sessions') return json({ sessions: [], error: null });
    if (p === '/inherent/agent-marks') return json({ marks: {} });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });
  await page.clock.setFixedTime(new Date(NOW));
  await page.goto(`http://127.0.0.1:${at}/?companion=1&port=${daemonPort}`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
  // The native bridge tells the page where the pointer is; a test moves the real one and tells the page the same.
  const move = async (x, y) => { await page.mouse.move(x, y); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [x, y]); };
  const read = () => page.evaluate(() => { const n = document.querySelector('.notch'); return { tl: n.dataset.tl, door: n.querySelector('.dt')?.dataset.door ?? null }; });
  const until = (fn, arg, timeout = 5000) => page.waitForFunction(fn, arg, { timeout });
  const lineX = async () => { const at = await page.evaluate(() => document.querySelector('.notch').dataset.lineAt), [x0, x1] = at.split('-').map(Number); return (x0 + x1) / 2; };
  const press = async (x, y) => { await move(x, y); await page.mouse.down(); await page.mouse.up(); };
  const rect = sel => page.evaluate(sel => { const e = document.querySelector(sel); return e ? e.getBoundingClientRect().toJSON() : null; }, sel);
  const pressOn = async sel => { const r = await rect(sel); assert.ok(r, `${sel} is on the page`); await press(r.x + r.width / 2, r.y + r.height / 2); };
  const hoverOn = async sel => { const r = await rect(sel); assert.ok(r, `${sel} is on the page`); await move(r.x + r.width / 2, r.y + r.height / 2); };
  const texts = sel => page.evaluate(sel => [...document.querySelectorAll(sel)].map(e => (e.innerText || e.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim()), sel);
  const keys = () => page.evaluate(() => [...document.querySelectorAll('.notch .dt-row[data-key]')].map(e => e.dataset.key));
  const door = async key => { await pressOn(`.notch .dt-door[data-door="${key}"]`); await until(key => document.querySelector('.notch .dt')?.dataset.door === key, key); await page.waitForTimeout(420); };
  // The day pane opened by a click on the wing's line (it stays up; a second click folds it), with the rows drawn and settled.
  const openDay = async () => {
    await until(() => document.querySelector('.notch')?.dataset.line);
    await press(await lineX(), 14);
    await until(() => document.querySelector('.notch-drop.is-open .dt-row'), undefined, 8000);
    await page.waitForTimeout(1000);
  };
  const wing = async () => { await press(await lineX(), 14); await page.waitForTimeout(900); };
  // Where the frame's parts are: the pane (the dialog, its inside, the black shape), the day inside it, and what must never move in it.
  const geo = () => page.evaluate(() => {
    const r = e => { const b = e?.getBoundingClientRect(); return b ? b.toJSON() : null; }, q = s => document.querySelector(s), dt = q('.notch .dt');
    return { drop: r(q('.notch-drop')), inner: r(q('.notch-pane-in')), shape: r(q('.notch-shape path')), dt: r(dt), head: r(dt.querySelector('.dt-head')), bar: r(dt.querySelector('.dt-bar')), track: r(dt.querySelector('.dt-track')), doors: r(dt.querySelector('.dt-doors')) };
  });
  // A Dashboard page in the pane: where it is and what it is made of.
  const tool = () => page.evaluate(() => {
    const r = e => { const b = e?.getBoundingClientRect(); return b ? b.toJSON() : null; }, q = s => document.querySelector(s), stage = q('.notch .dt-stage'), pg = stage?.querySelector(':scope > .dt-page[data-page]');
    const ad = pg?.querySelector(':scope > .ad.is-only'), view = ad?.querySelector(':scope > .view'), sec = view?.querySelector(':scope > section.page'), body = sec?.querySelector(':scope > .pg-body'), cs = ad && getComputedStyle(ad);
    return { stageClass: stage?.className ?? null, stage: r(stage), page: r(pg), ad: r(ad), view: r(view), sec: r(sec), body: r(body), dt: r(q('.notch .dt')), doors: r(q('.notch .dt-doors')),
      pad: cs ? [cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft].join(' ') : null, viewStyle: view?.getAttribute('style') ?? null, inner: ad?.dataset.page ?? null, frame: pg?.dataset.page ?? null,
      title: sec?.querySelector(':scope > .pg-head h3')?.textContent ?? null, meta: sec?.querySelector(':scope > .pg-head .meta')?.textContent ?? null, back: !!sec?.querySelector(':scope > .pg-head .pg-back'),
      ownHead: !!pg?.querySelector('.dt-pin, .dt-ph, .dt-pn'), scroll: body ? getComputedStyle(body).overflowY : null, pageScrolls: pg ? pg.scrollHeight > pg.clientHeight + 1 : null, lists: stage?.querySelector(':scope > .dt-list') ? getComputedStyle(stage.querySelector(':scope > .dt-list')).visibility : null };
  });
  const shot = async (name, g) => page.screenshot({ path: path.join(out, `${name}.png`), clip: { x: 60, y: 0, width: 520, height: Math.ceil(Math.max(g.drop.bottom, g.dt.bottom)) + 2 } });
  return { page, ctx, s, move, read, until, press, pressOn, hoverOn, texts, keys, door, openDay, wing, geo, tool, rect, shot };
}
// What moved between two looks at the frame: the sizes of the pane and its black shape, and the places of the head, the bar and the doors.
const moved = (g0, g1) => [['pane', 'drop', 'height'], ['pane inside', 'inner', 'height'], ['shape', 'shape', 'height'], ['day', 'dt', 'height'], ['day', 'dt', 'top'], ['head', 'head', 'top'], ['bar', 'bar', 'top'], ['doors', 'doors', 'top']]
  .filter(([, k, p]) => !same(g0[k][p], g1[k][p])).map(([name, k, p]) => `${name} ${p} ${g0[k][p]} -> ${g1[k][p]}`);
const inside = (b, dt, doors) => !!b && b.left >= dt.left - 1 && b.right <= dt.right + 1 && b.top >= dt.top && b.bottom <= doors.top + 1;
const GONE = /\/inherent\/(today|notices|mail|jobs|setup|projects)\b/;

try {
  for (const port of [web, webAll]) for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${port}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }

  {
    const a = await boot();
    await a.openDay();
    const g0 = await a.geo();
    const board = await a.page.evaluate(() => [...document.querySelectorAll('.notch .dt-door')].map(e => e.dataset.door).join());
    check('the pane opens on 今天 with the doors row as the frame made it, 更多 last', board === 'today,mail,todo,agents,quota,more' && same(g0.dt.height, 584), board);

    // ---- the list ----
    await a.door('more');
    const g1 = await a.geo();
    check('更多 is lit, and the pane, its shape, the day, the head, the bar and the doors row are where they were on 今天', await a.page.locator('.notch .dt-door[aria-current="page"]').getAttribute('data-door') === 'more' && moved(g0, g1).length === 0, moved(g0, g1).join('; '));
    check('the line says what is here, and there are no chips, no link, no marks on the bar, no now line and no number on the door',
      (await a.texts('.notch .dt-line'))[0] === '聊天记录、记忆、项目、插件和设置都在这里。' && await a.page.locator('.notch .dt-numbers .dt-num, .notch .dt-numbers .dt-go').count() === 0 && await a.page.locator('.notch .dt-mk').count() === 0
      && await a.page.locator('.notch .dt-bar.is-page').count() === 1 && await a.page.locator('.notch .dt-list .dt-now').count() === 0 && await a.page.locator('.notch .dt-door[data-door="more"] em').count() === 0, (await a.texts('.notch .dt-line')).join());
    await a.until(() => document.querySelector('.notch .dt-row[data-key="memory"] .dt-tx em'));
    const rows = await a.page.evaluate(() => [...document.querySelectorAll('.notch .dt-row[data-key]')].map(r => ({ key: r.dataset.key, title: r.querySelector('.dt-ttl b')?.textContent, sub: r.querySelector('.dt-tx em')?.textContent ?? null, at: r.querySelector('.dt-at')?.textContent, icon: !!r.querySelector('.dt-ic svg'), opens: !r.classList.contains('is-still') })));
    check('seven rows, in this order: 聊天记录, 记忆, 项目, 插件, 设置, 早报, 求职邮件, each with a glyph in its circle, no time, and each opens', rows.map(r => `${r.key}:${r.title}`).join() === PAGES.map(([k, t]) => `${k}:${t}`).join()
      && rows.every(r => r.icon && r.opens && r.at === ''), JSON.stringify(rows));
    const gutter = await a.page.evaluate(() => [...document.querySelectorAll('.notch .dt-row[data-key]')].map(r => r.querySelector('.dt-ic').getBoundingClientRect().left - r.getBoundingClientRect().left));
    check('rows with no time have no time column: each circle starts within 8 pt of its row\'s edge', gutter.length === 7 && gutter.every(x => x >= 0 && x <= 8), JSON.stringify(gutter));
    check('a row says what the Dashboard\'s home says of its page where it can: how many notes are kept and how many are new, and today\'s brief in its first line; the others say nothing',
      rows.find(r => r.key === 'memory').sub === '记着 4 条 · 2 条新的' && rows.find(r => r.key === 'brief').sub === BRIEF.summary && rows.filter(r => r.sub).map(r => r.key).join() === 'memory,brief', JSON.stringify(rows.map(r => [r.key, r.sub])));
    await a.hoverOn('.notch .dt-head');
    await a.shot('more-list', g1);

    // ---- 设置: the page opens alone, inside the pane ----
    const mark = a.s.gets.length;
    await a.pressOn('.notch .dt-row[data-key="settings"] .dt-tx');
    await a.until(() => document.querySelector('.notch .dt-page[data-page="settings"] .ad.is-only .pg-head h3'), undefined, 8000); await a.page.waitForTimeout(800);
    let tl = await a.tool(), g = await a.geo();
    check('a row opens the Dashboard\'s own settings page in the pane: its ‹ and its title (设置) at the top, the frame draws no head of its own, and the pane is the size it was',
      tl.title === '设置' && tl.back && !tl.ownHead && tl.inner === 'settings' && tl.frame === 'settings' && moved(g0, g).length === 0, `${JSON.stringify([tl.title, tl.back, tl.ownHead, tl.inner, tl.frame])} ${moved(g0, g).join('; ')}`);
    check('the stage gives it all the room (from under the bar to the doors row), the page fills the stage and its page is up from the first frame; the list waits hidden',
      /\bis-page\b/.test(tl.stageClass) && /\bis-tool\b/.test(tl.stageClass) && same(tl.page.height, tl.stage.height, 1) && same(tl.ad.height, tl.page.height, 1) && tl.doors.top - tl.stage.bottom >= 0 && tl.doors.top - tl.stage.bottom <= 24 && tl.lists === 'hidden', JSON.stringify([tl.stageClass, tl.stage, tl.page, tl.ad, tl.doors, tl.lists]));
    check('it is inside the pane (not out past its edges, not under the doors), with none of the panel\'s box: no padding, no height of the home on its view',
      inside(tl.sec, tl.dt, tl.doors) && inside(tl.ad, tl.dt, tl.doors) && tl.pad === '0px 0px 0px 0px' && tl.viewStyle === null && same(tl.sec.height, tl.view.height, 1), JSON.stringify([tl.sec, tl.dt, tl.pad, tl.viewStyle]));
    check('the page scrolls inside itself (its body), not the frame\'s page', tl.scroll === 'auto' && tl.pageScrolls === false && tl.body.height > 100, JSON.stringify([tl.scroll, tl.pageScrolls, tl.body]));
    check('it is the settings page: the Jarvis connection in its head and the categories under it', /Jarvis 已连接/.test(tl.meta ?? '') && await a.page.locator('.notch .dt-page .ad.is-only .st-cat').count() >= 5, tl.meta);
    const read = a.s.gets.slice(mark).filter(x => !/^\/inherent\/(day|claude-sessions|usage|work-state|controls|agent-marks|memory|brief)\b/.test(x));
    check('a page that is shown alone reads only what it shows: the settings, and none of the home\'s (today, notices, mail, jobs, the voice, projects), and the projects are not sorted for it',
      read.some(x => x.startsWith('/inherent/settings')) && !a.s.gets.slice(mark).some(x => GONE.test(x)) && !a.s.posts.some(x => x.path === '/inherent/projects/refresh'), JSON.stringify([read, a.s.gets.slice(mark)]));
    check('and it tells the daemon what is on screen, as the Dashboard does: settings', a.s.views.at(-1)?.page === 'settings', JSON.stringify(a.s.views.at(-1)));
    await a.hoverOn('.notch .dt-head'); await a.page.waitForTimeout(250);
    await a.shot('more-settings', g);

    // ---- the frame's keys leave it alone ----
    const title0 = (await a.texts('.notch .dt-date'))[0];
    for (const key of ['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Space', 'Enter']) await a.page.keyboard.press(key);
    await a.page.waitForTimeout(500);
    tl = await a.tool();
    check('← → ↑ ↓ Space and Enter do nothing to a page shown alone: the same day, the same page, still up', (await a.texts('.notch .dt-date'))[0] === title0 && tl.frame === 'settings' && tl.title === '设置' && /\bis-page\b/.test(tl.stageClass), JSON.stringify([title0, tl.frame, tl.title]));

    // ---- deeper steps go back inside the page ----
    await a.pressOn('.notch .dt-page .ad.is-only .st-cat'); await a.page.waitForTimeout(500);
    tl = await a.tool();
    const category = tl.title;
    check('a category of the settings opens in the page (its title is the category\'s)', category !== '设置' && category !== null && /\bis-tool\b/.test(tl.stageClass), category);
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(500);
    tl = await a.tool();
    check('Esc goes back one step inside the page: the settings again, the page still up and the pane the size it was', tl.title === '设置' && /\bis-page\b/.test(tl.stageClass) && moved(g0, await a.geo()).length === 0, tl.title);
    await a.pressOn('.notch .dt-page .ad.is-only .st-cat'); await a.page.waitForTimeout(400);
    await a.pressOn('.notch .dt-page .ad.is-only .pg-head .pg-back'); await a.page.waitForTimeout(500);
    tl = await a.tool();
    check('the page\'s ‹ does the same from a category', tl.title === '设置' && /\bis-page\b/.test(tl.stageClass), tl.title);

    // ---- ‹ at the root of the page, and Esc, go back to the list ----
    await a.pressOn('.notch .dt-page .ad.is-only .pg-head .pg-back'); await a.page.waitForTimeout(800);
    check('‹ at the root of the page slides it away and the list is back on 更多, as it was; the pane has not moved',
      await a.page.locator('.notch .dt-page').count() === 0 && !/\bis-page\b|\bis-tool\b/.test((await a.tool()).stageClass) && (await a.keys()).join() === PAGES.map(([k]) => k).join() && (await a.read()).door === 'more' && moved(g0, await a.geo()).length === 0, (await a.tool()).stageClass);
    check('and it told the daemon that nothing is on screen any more', a.s.views.at(-1)?.page === null, JSON.stringify(a.s.views.at(-1)));
    await a.pressOn('.notch .dt-row[data-key="settings"] .dt-tx');
    await a.until(() => document.querySelector('.notch .dt-page[data-page="settings"] .ad.is-only .pg-head h3'), undefined, 8000); await a.page.waitForTimeout(800);
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(800);
    check('Esc at the root of the page does the same, and stays on 更多', await a.page.locator('.notch .dt-page').count() === 0 && (await a.read()).door === 'more' && (await a.keys()).length === 7);

    // ---- 记忆 ----
    await a.pressOn('.notch .dt-row[data-key="memory"] .dt-tx');
    await a.until(() => document.querySelectorAll('.notch .dt-page[data-page="memory"] .ad.is-only .mem-row').length === 4, undefined, 8000); await a.page.waitForTimeout(600);
    tl = await a.tool(); g = await a.geo();
    check('记忆 opens the memory page in the pane: its title, how many are kept in its head, the four notes it keeps, and the pane the size it was',
      tl.title === '记忆' && tl.meta === '记着 4 条' && inside(tl.sec, tl.dt, tl.doors) && moved(g0, g).length === 0 && a.s.gets.some(x => x === '/inherent/memory'), `${tl.title} ${tl.meta} ${moved(g0, g).join('; ')}`);
    check('and it did not sort the projects or read the home\'s routes for it', !a.s.posts.some(x => x.path === '/inherent/projects/refresh'));
    await a.hoverOn('.notch .dt-head'); await a.shot('more-memory', g);
    await a.hoverOn('.notch .dt-page .ad.is-only .pg-body'); await a.page.mouse.wheel(0, 260); await a.page.waitForTimeout(500);
    const scrolled = await a.page.evaluate(() => document.querySelector('.notch .dt-page .ad.is-only .pg-body').scrollTop);
    check('the wheel scrolls the page inside the pane, and the frame stays where it is', scrolled > 100 && moved(g0, await a.geo()).length === 0 && !(await a.tool()).pageScrolls, String(scrolled));
    await a.page.evaluate(() => document.querySelector('.notch .dt-page .ad.is-only .mem-row').scrollIntoView({ block: 'nearest' })); await a.page.waitForTimeout(200);
    await a.pressOn('.notch .dt-page .ad.is-only .mem-row'); await a.page.waitForTimeout(700);
    tl = await a.tool();
    check('a note opens inside the page (its text is there, the page is still the memory page, and nothing of the frame\'s head)', /住在维多利亚|早上先看邮件/.test(await a.page.locator('.notch .dt-page .ad.is-only .pg-body').innerText()) && tl.title === '记忆' && !tl.ownHead && tl.inner === 'memory');
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(600);
    check('Esc goes back from the note to the notes, inside the page', await a.page.locator('.notch .dt-page[data-page="memory"] .ad.is-only .mem-row').count() === 4 && (await a.read()).door === 'more');
    await a.page.evaluate(() => document.querySelector('.notch .dt-page .ad.is-only .mem-row').scrollIntoView({ block: 'nearest' })); await a.page.waitForTimeout(200);
    await a.pressOn('.notch .dt-page .ad.is-only .mem-row'); await a.page.waitForTimeout(700);

    // ---- a fold keeps the page, as it keeps any open page ----
    await a.wing();
    check('the pane folds with the page up, and the page tells the daemon that nothing is on screen', (await a.read()).tl === '' && a.s.views.at(-1)?.page === null, JSON.stringify([await a.read(), a.s.views.at(-1)]));
    await a.wing();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-page[data-page="memory"]'), undefined, 8000); await a.page.waitForTimeout(600);
    check('and when it is opened again soon after, it is the page that was left: 记忆 with the note open, and on screen again as far as the daemon is told',
      (await a.read()).door === 'more' && /住在维多利亚|早上先看邮件/.test(await a.page.locator('.notch .dt-page .ad.is-only .pg-body').innerText()) && a.s.views.at(-1)?.page === 'memory', JSON.stringify(a.s.views.at(-1)));
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(500);
    await a.pressOn('.notch .dt-page .ad.is-only .pg-head .pg-back'); await a.page.waitForTimeout(800);
    check('and ‹ takes it back to the list', await a.page.locator('.notch .dt-page').count() === 0 && (await a.keys()).length === 7);

    // ---- a field the page has the cursor in lets go on the first Esc ----
    await a.pressOn('.notch .dt-row[data-key="plugins"] .dt-tx');
    await a.until(() => document.querySelector('.notch .dt-page[data-page="plugins"] .ad.is-only input[type="search"]'), undefined, 8000); await a.page.waitForTimeout(700);
    await a.pressOn('.notch .dt-page .ad.is-only input[type="search"]'); await a.page.keyboard.type('git');
    check('the plugins page has its search field with the cursor in it', await a.page.evaluate(() => document.activeElement?.matches('.ad.is-only input[type="search"]')) && await a.page.locator('.notch .dt-page .ad.is-only input[type="search"]').inputValue() === 'git');
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(400);
    check('the first Esc lets go of the field and the page stays', !await a.page.evaluate(() => document.activeElement?.matches('input')) && await a.page.locator('.notch .dt-page[data-page="plugins"]').count() === 1);
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(800);
    check('the second goes back to the list', await a.page.locator('.notch .dt-page').count() === 0 && (await a.read()).door === 'more');

    // ---- the rest of the pages ----
    const sent = a.s.posts.length;
    const seen = [];
    for (const [key, , title] of PAGES.filter(([k]) => !['settings', 'memory', 'plugins'].includes(k))) {
      await a.pressOn(`.notch .dt-row[data-key="${key}"] .dt-tx`);
      await a.until(key => document.querySelector(`.notch .dt-page[data-page="${key}"] .ad.is-only .pg-head h3`), key, 8000); await a.page.waitForTimeout(700);
      const t = await a.tool(), gg = await a.geo();
      if (t.title !== title || !t.back || t.ownHead || !inside(t.sec, t.dt, t.doors) || moved(g0, gg).length || /\bis-tool\b/.test(t.stageClass) === false) seen.push(`${key}: ${t.title} ${moved(g0, gg).join(', ')}`);
      await a.pressOn('.notch .dt-page .ad.is-only .pg-head .pg-back'); await a.page.waitForTimeout(700);
      if (await a.page.locator('.notch .dt-page').count() !== 0) seen.push(`${key}: ‹ did not go back`);
    }
    check('聊天记录, 项目, 早报 and 求职邮件 each open their page in the pane under its own title (对话, 项目, 早报, 求职邮件), inside it, with the pane the size it was, and ‹ closes each', seen.length === 0, seen.join('; '));
    check('only the projects page sorts the projects (one POST of /inherent/projects/refresh, for the one page that is about them)', a.s.posts.slice(sent).filter(x => x.path === '/inherent/projects/refresh').length === 1, JSON.stringify(a.s.posts.slice(sent).map(x => x.path)));
    await a.ctx.close();
  }

  // ---- English ----
  {
    const a = await boot({ lang: 'en' });
    await a.openDay();
    await a.door('more');
    await a.until(() => document.querySelector('.notch .dt-row[data-key="memory"] .dt-tx em'));
    check('in English the line, the rows and the memory row\'s words follow', (await a.texts('.notch .dt-line'))[0] === 'Your conversation, memory, projects, plugins and settings live here.'
      && (await a.texts('.notch .dt-row .dt-ttl b')).join('|') === 'Conversation|Memory|Projects|Plugins|Settings|Morning brief|Job mail' && (await a.texts('.notch .dt-row[data-key="memory"] .dt-tx em'))[0] === '4 kept · 2 new', (await a.texts('.notch .dt-line')).join());
    await a.pressOn('.notch .dt-row[data-key="settings"] .dt-tx');
    await a.until(() => document.querySelector('.notch .dt-page[data-page="settings"] .ad.is-only .pg-head h3'), undefined, 8000);
    check('and the page is the Dashboard\'s in English', (await a.tool()).title === 'Settings');
    await a.ctx.close();
  }

  // ---- every page of the Dashboard through `ctx.tool` (the stand-in door, in the build of its own) ----
  {
    const a = await boot({ at: webAll });
    await a.openDay();
    const g0 = await a.geo();
    await a.door('quota');
    const bad = [];
    for (const key of ALL_PAGES) {
      await a.page.evaluate(key => document.querySelector(`.notch .dt-row[data-key="${key}"]`).scrollIntoView({ block: 'nearest' }), key);
      await a.pressOn(`.notch .dt-row[data-key="${key}"] .dt-tx`);
      await a.until(key => document.querySelector(`.notch .dt-page[data-page="${key}"] .ad.is-only .pg-head h3`), key, 8000); await a.page.waitForTimeout(600);
      const t = await a.tool(), g = await a.geo();
      if (t.title !== ALL_TITLES[key] || t.inner !== key || !t.back || t.ownHead || !inside(t.sec, t.dt, t.doors) || moved(g0, g).length) bad.push(`${key}: ${t.title} ${moved(g0, g).join(', ')}`);
      if (key === 'usage') await a.shot('usage', g);
      await a.pressOn('.notch .dt-page .ad.is-only .pg-head .pg-back'); await a.page.waitForTimeout(600);
      if (await a.page.locator('.notch .dt-page').count() !== 0) bad.push(`${key}: ‹ did not go back`);
    }
    check('through the renderer the pane is handed (`ctx.tool`), every page of the Dashboard opens alone inside the pane, under its own title and ‹, with the pane the size it was: ' + ALL_PAGES.join(', '), bad.length === 0, bad.join('; '));
    await a.page.evaluate(() => document.querySelector('.notch .dt-row[data-key="usage"]').scrollIntoView({ block: 'nearest' }));
    await a.pressOn('.notch .dt-row[data-key="usage"] .dt-tx');
    await a.until(() => document.querySelector('.notch .dt-page[data-page="usage"] .ad.is-only .dial'), undefined, 8000);
    check('the usage page shows Claude\'s 5-hour window at 38% from the daemon, in the pane', await a.page.locator('.notch .dt-page[data-page="usage"] .ad.is-only .dial[aria-valuenow="38"]').count() === 1);
    await a.ctx.close();
  }

  check('no page error from any of it', errors.length === 0, errors.join('; '));
  console.log(`\n${checks.length} checks passed; screenshots in ${out}`);
} finally {
  await browser.close();
  for (const server of servers) server.kill();
  rmSync(everyPage, { recursive: true, force: true });
}
if (errors.length) { console.error('page errors:', errors); process.exit(1); }

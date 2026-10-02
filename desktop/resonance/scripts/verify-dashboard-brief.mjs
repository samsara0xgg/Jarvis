// The home's morning brief, in headless Chrome against the built page. Run after `npm run build`.
// A fake daemon (routed HTTP plus a fake WebSocket) serves a brief written the way the daemon now writes it: a short
// main line, and a reading view with the day's items, what is open and what Allen named. Nothing is spoken or sent.
// It checks that the card is compact, that the brief shows the first time the panel opens that morning and not again
// (not after a reopen, not after a restart), that it is back the next day, that closing it with the × can be undone,
// and that the page is sectioned with each item's activity under its title. Screenshots land in evidence/dashboard-brief/.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = process.env.BRIEF_EVIDENCE_DIR ?? path.join(root, 'evidence/dashboard-brief');
mkdirSync(dir, { recursive: true });
const web = Number(process.env.BRIEF_PORT ?? 5208), daemon = 8797;
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const checks = [], check = (name, pass) => { assert.ok(pass, name); checks.push(name); console.log(`PASS ${name}`); };
const KEY = 'companion-brief-read-v1';
const today = new Date().toLocaleDateString('en-CA');
const brief = {
  date: today, items: 3,
  summary: '昨天主要在推进 Startrail 英文化与语音链路：英文版已合并到 main，结构化输出的语音实测找出了三处问题并修了其中两处，夜里还跑了一次夜间挂机的验证。',
  body: ['昨天主要在推进 Startrail 英文化与语音链路：英文版已合并到 main，结构化输出的语音实测找出了三处问题并修了其中两处，夜里还跑了一次夜间挂机的验证。', '',
    '## 昨天做了什么',
    '- **Startrail 英文化** · 已合并到 main', '  - 把界面文案做成中英双语，跟随 Jarvis 的语言设置。',
    '- **语音结构化输出** · 已合并到 main', '  - 让回答分成说出口的和写下来的两部分，修了 TTS 语言判断和停止方块。',
    '- **夜间挂机验证** · 据代理自述已完成，尚未核实', '  - Codex 会话里说跑通了整条流程。', '',
    '## 还没完成', '- 结构化输出语音实测的 4-7 项还在等 Allen 决定。', '',
    '## 你说过的下一步', '- 把早报改成每天早上第一次打开才出现。'].join('\n'),
};
const fixtures = {
  '/inherent/language': { language: 'zh' }, '/inherent/think': { on: false, on_words: '深想', turn_id: null }, '/inherent/brief': brief,
  '/inherent/today': { weather: { now_c: 12, high_c: 15, summary: 'Rain from 9 PM', hours: [] }, events: [], todos: [{ id: 't1', title: 'Send cover letter' }] },
  '/inherent/mail': { unread: [] }, '/inherent/notices': { notices: [] }, '/inherent/setup': { keys: {}, voice_models: { state: 'ready', done: 0, total: 0 } },
};
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security'] });
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const context = await browser.newContext({ viewport: { width: 640, height: 780 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route(`http://127.0.0.1:${daemon}/**`, route => {
    const { pathname } = new URL(route.request().url());
    const headers = { 'access-control-allow-origin': '*' };
    if (fixtures[pathname]) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(fixtures[pathname]), headers });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}', headers });
  });
  await page.addInitScript(() => {
    try { if (!localStorage.getItem('companion-settings-v1')) localStorage.setItem('companion-settings-v1', JSON.stringify({ lang: 'zh' })); } catch { /* private window */ }
    window.jarvis = { placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }), onPlacement: () => () => {}, onDisplayLeave: () => () => {},
      displayReady: () => {}, companionSettings: () => {}, onCursor: cb => { window.__cursor = cb; return () => {}; }, onCommand: () => () => {}, passthrough: () => {}, focus: async () => {}, material: () => {} };
    window.__sockets = [];
    window.WebSocket = class { constructor(url) { this.url = url; window.__sockets.push(this); setTimeout(() => this.onopen?.(), 0); } send() {} close() { this.onclose?.(); } };
  });
  const load = async () => {
    await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${daemon}`);
    await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
    await page.waitForFunction(() => window.__sockets?.length >= 1);
    await page.waitForTimeout(400);
  };
  const island = () => page.locator('.companion-island-target').click({ force: true });
  const open = async () => { await island(); await page.waitForSelector('.companion-dashboard.is-open'); await page.waitForTimeout(1200); };
  const close = async () => {
    for (let i = 0; i < 2 && await page.locator('.companion-dashboard.is-open').count(); i++) { await island(); await page.waitForTimeout(300); }
    await page.waitForFunction(() => !document.querySelector('.companion-dashboard.is-open'), null, { timeout: 4000 });
  };
  const shot = name => page.screenshot({ path: path.join(dir, `${name}.png`), clip: { x: 130, y: 0, width: 380, height: 780 } });
  const shows = () => page.locator('.ad [data-block="brief"]').count().then(n => n === 1);
  const stored = () => page.evaluate(key => localStorage.getItem(key), KEY);

  await load();
  await open();
  check('B1 the first time the panel opens that morning the brief is on the home', await shows());
  const card = await page.evaluate(() => { const el = document.querySelector('.ad [data-block="brief"]'), text = el.querySelector('.text'), r = el.getBoundingClientRect();
    return { height: r.height, lines: Math.round(text.getBoundingClientRect().height / parseFloat(getComputedStyle(text).lineHeight)), head: el.querySelector('.label').textContent, meta: el.querySelector('.meta').textContent, text: text.textContent }; });
  check('B2 the card is compact: its heading, the count and at most two lines, not the whole report', card.height <= 100 && card.lines <= 2 && card.head === '早报' && card.meta === '3 条');
  check('B3 the card holds the day’s main line and none of the report’s audit words', card.text.startsWith('昨天主要在推进') && !/有实证|引用|提交 [0-9a-f]{7}/.test(card.text));
  check('B4 the rows below it are still in reach: Today follows the card with no scrolling needed', await page.evaluate(() => {
    const list = document.querySelector('.ad .home-list').getBoundingClientRect(), today = document.querySelector('.ad [data-block="today"]').getBoundingClientRect();
    return today.bottom <= list.bottom; }));
  await shot('1-first-open');
  check('B5 seen on the home is not yet read: nothing is kept while the panel is open', (await stored()) === '');

  await page.locator('.ad [data-block="brief"] .fill').click(); await page.waitForTimeout(900);
  const view = await page.evaluate(() => { const md = document.querySelector('.ad .brief-md .md');
    return { title: document.querySelector('.ad .pg-head h3').textContent, lead: md.firstElementChild.tagName, sections: [...md.querySelectorAll('h5')].map(h => h.textContent),
      items: md.querySelectorAll(':scope > div > ul > li').length, nested: md.querySelectorAll(':scope > div > ul > li > ul > li').length, raw: md.textContent }; });
  check('B6 the page opens on the main line, then the sections in the order a morning wants them',
    view.title === '早报' && view.lead === 'P' && view.sections.join('|') === '昨天做了什么|还没完成|你说过的下一步');
  check('B7 each item of yesterday has its activity under its title, and none shows a citation or commit number', view.items === 5 && view.nested === 3 && !/引用|abc1234|证据/.test(view.raw));
  await shot('2-page');
  await page.locator('.ad .pg-back').click(); await page.waitForTimeout(800);
  check('B8 reading it takes it off the home', !(await shows()));
  await close();
  check('B9 and the day is kept', (await stored()) === today);

  // Not read, only seen: the panel closes and the brief is spent all the same.
  await page.evaluate(key => localStorage.removeItem(key), KEY);
  await page.reload(); await page.waitForFunction(() => window.__sockets?.length >= 1); await page.waitForTimeout(400);
  await open();
  check('B10 a morning that was not read shows it again on the first open', await shows());
  await close();
  check('B11 closing the panel counts as having seen it', (await stored()) === today);
  await open();
  check('B12 the second open that day has no brief and the home is shorter for it', !(await shows()));
  await shot('3-second-open');
  await close();

  await page.reload(); await page.waitForFunction(() => window.__sockets?.length >= 1); await page.waitForTimeout(400);
  await open();
  check('B13 a restart does not bring it back that day', !(await shows()));
  await close();

  await page.evaluate(key => localStorage.setItem(key, '2020-01-01'), KEY);
  await page.reload(); await page.waitForFunction(() => window.__sockets?.length >= 1); await page.waitForTimeout(400);
  await open();
  check('B14 the next morning’s brief is back', await shows());

  await page.locator('.ad [data-block="brief"]').hover();
  await page.locator('.ad [data-block="brief"] .mx').click(); await page.waitForTimeout(700);
  check('B15 the × closes it and counts as read', !(await shows()) && (await stored()) === today);
  await page.locator('.ad .toast button').click(); await page.waitForTimeout(500);
  check('B16 Undo brings the card back and the day with it', (await shows()) && (await stored()) === '2020-01-01');
  await close();

  // No brief yet (404) and a brief with no count.
  delete fixtures['/inherent/brief'];
  await page.evaluate(key => localStorage.removeItem(key), KEY);
  await page.reload(); await page.waitForFunction(() => window.__sockets?.length >= 1); await page.waitForTimeout(400);
  await open();
  check('B17 until yesterday’s report is saved there is no brief on the home', !(await shows()));
  await close();
  fixtures['/inherent/brief'] = { ...brief, items: 0 };
  await page.reload(); await page.waitForFunction(() => window.__sockets?.length >= 1); await page.waitForTimeout(400);
  await open();
  check('B18 a brief with no item count says there is more to read', (await page.locator('.ad [data-block="brief"] .meta').textContent()) === '看全文');
  check('B19 the page raised no errors', errors.length === 0);
  console.log(`\n${checks.length} checks passed`);
} finally {
  await browser.close();
  server.kill();
}

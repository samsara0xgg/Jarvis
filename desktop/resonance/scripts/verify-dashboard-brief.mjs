// The home's morning brief, in headless Chrome against the built page. Run after `npm run build`.
// A fake daemon (routed HTTP plus a fake WebSocket) serves a brief written the way the daemon now writes it: a short
// main line and sections of short rows: the day's items with how each ended, what is open, what Allen named, what he decided.
// Nothing is spoken or sent.
// It checks that the card is compact, that the brief shows the first time the panel opens that morning and not again
// (not after a reopen, not after a restart), that it is back the next day, that closing it with the × can be undone,
// and that the page is a scannable digest: rows with a status word, notes opened on demand, a long section cut with "N more",
// the decided and suggested behind one fold, all on one screen. Screenshots land in evidence/dashboard-brief/.
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
const LEAD = '昨天主要在推进 Startrail 英文化与语音链路：英文版已合并到 main，结构化输出的语音实测找出了三处问题并修了其中两处，夜里还跑了一次夜间挂机的验证。';
const item = (text, tag, label, status, note) => ({ text, tag, label, status, note });
const brief = {
  date: today, items: 3, summary: LEAD, lead: LEAD,
  sections: [
    { key: 'items', title: '昨天做了什么', rows: [
      item('Startrail 英文化', 'done', '已完成', '已合并到 main', '把界面文案做成中英双语，跟随 Jarvis 的语言设置。'),
      item('语音结构化输出', 'done', '已完成', '已合并到 main', '让回答分成说出口的和写下来的两部分，修了 TTS 语言判断和停止方块。'),
      item('夜间挂机验证', 'check', '未核实', '据代理自述已完成，尚未核实', 'Codex 会话里说跑通了整条流程。')] },
    { key: 'open', title: '还没完成', rows: [{ text: '结构化输出语音实测的 4-7 项还在等 Allen 决定。' }] },
    { key: 'next', title: '你说过的下一步', rows: [{ text: '把早报改成每天早上第一次打开才出现。' }] },
    { key: 'decisions', title: '定下来的事', rows: [{ text: '早报只在每天早上第一次打开时出现。' }, { text: '看全文的入口留在首页的早报卡片上。' }] },
  ],
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
  const view = await page.evaluate(() => { const bf = document.querySelector('.ad .bf'), body = document.querySelector('.ad .pg-body');
    const tags = [...bf.querySelectorAll('[data-section=items] .bf-tag')].map(el => el.textContent);
    return { title: document.querySelector('.ad .pg-head h3').textContent, lead: bf.firstElementChild.className, leadText: bf.firstElementChild.textContent,
      sections: [...bf.querySelectorAll(':scope > .bf-sec h4')].map(h => h.firstChild.textContent), tags, rows: bf.querySelectorAll('[data-section=items] li').length,
      raw: bf.textContent, fits: body.scrollHeight <= body.clientHeight + 1, later: bf.querySelector('.bf-later .fold')?.textContent }; });
  check('B6 the page opens on the main line, then the sections in the order a morning wants them',
    view.title === '早报' && view.lead === 'bf-lead' && view.leadText.startsWith('昨天主要在推进') && view.sections.join('|') === '昨天做了什么|还没完成|你说过的下一步');
  check('B7 each item of yesterday is one short row with a word for how it ended, and none shows a citation or commit number',
    view.rows === 3 && view.tags.join('|') === '已完成|已完成|未核实' && !/引用|abc1234|证据/.test(view.raw));
  await shot('2-page');
  check('B7a the whole page fits the panel without scrolling', view.fits);
  check('B7b what was decided waits behind one fold that says how much is in it', view.later === '定下来的事 2');
  const note = page.locator('.ad .bf [data-section=items] li').first().locator('.bf-more-body');
  check('B7c notes are closed until asked for', (await note.evaluate(el => el.getBoundingClientRect().height)) === 0 && await note.evaluate(el => el.inert));
  await page.locator('.ad .bf [data-section=items] li').first().locator('button.bf-row').click(); await page.waitForTimeout(500);
  await shot('2b-note-open');
  check('B7d a row opens to its note and closes again', (await note.evaluate(el => el.getBoundingClientRect().height)) > 20 && (await note.textContent()).startsWith('把界面文案')
    && await (async () => { await page.locator('.ad .bf [data-section=items] li').first().locator('button.bf-row').click(); await page.waitForTimeout(500); return (await note.evaluate(el => el.getBoundingClientRect().height)) === 0; })());
  await page.locator('.ad .bf-later .fold').click(); await page.waitForTimeout(500);
  await shot('2c-fold-open');
  check('B7e the fold opens onto the decided', (await page.locator('.ad .bf-later [data-section=decisions] li').count()) === 2 && await page.locator('.ad .bf-later .fold-body').evaluate(el => !el.inert));
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
  await close();

  // A busy day: nine items. The page lists five and offers the rest; the panel scrolls rather than grows.
  const busy = Array.from({ length: 9 }, (_, i) => item(`第 ${i + 1} 件事`, i % 3 ? 'done' : 'going', i % 3 ? '已完成' : '进行中', '', i % 2 ? '这一件的经过。' : undefined));
  fixtures['/inherent/brief'] = { ...brief, items: 9, sections: [{ key: 'items', title: '昨天做了什么', rows: busy }, ...brief.sections.slice(1)] };
  await page.evaluate(key => localStorage.removeItem(key), KEY);
  await page.reload(); await page.waitForFunction(() => window.__sockets?.length >= 1); await page.waitForTimeout(400);
  await open();
  await page.locator('.ad [data-block="brief"] .fill').click(); await page.waitForTimeout(900);
  const five = await page.locator('.ad .bf [data-section=items] li').count(), rest = await page.locator('.ad .bf [data-section=items] .bf-rest').textContent();
  check('B19 a section of nine lists five and says how many more', five === 5 && rest === '还有 4 项');
  await shot('4-busy-day');
  await page.locator('.ad .bf [data-section=items] .bf-rest').click(); await page.waitForTimeout(300);
  check('B20 and the rest are one click away', (await page.locator('.ad .bf [data-section=items] li').count()) === 9 && (await page.locator('.ad .bf-rest').count()) === 0);
  await page.locator('.ad .pg-back').click(); await page.waitForTimeout(800);
  await close();

  // A day with no work in the report: the main line alone, and a plain note instead of empty headings.
  fixtures['/inherent/brief'] = { date: today, items: 0, summary: '昨天没有记录到可汇报的工作。', lead: '昨天没有记录到可汇报的工作。', sections: [] };
  await page.evaluate(key => localStorage.removeItem(key), KEY);
  await page.reload(); await page.waitForFunction(() => window.__sockets?.length >= 1); await page.waitForTimeout(400);
  await open();
  await page.locator('.ad [data-block="brief"] .fill').click(); await page.waitForTimeout(900);
  check('B21 a day with no work shows the main line and one plain note, no empty sections',
    (await page.locator('.ad .bf-sec').count()) === 0 && (await page.locator('.ad .bf .muted').textContent()) === '昨天没有记录到工作。');
  await shot('5-no-work');
  check('B22 the page raised no errors', errors.length === 0);
  console.log(`\n${checks.length} checks passed`);
} finally {
  await browser.close();
  server.kill();
}

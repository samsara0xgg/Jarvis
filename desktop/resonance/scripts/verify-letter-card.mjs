// The notch's send card shows the whole letter, in headless Chrome against the built page. Run after `npm run build`.
// A fake daemon serves one waiting letter card: the body the 2026-09-26 22:38 card cut to its first line.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = path.join(root, 'evidence', 'letter-card');
mkdirSync(dir, { recursive: true });
const web = Number(process.env.COMPANION_PORT ?? 5197), port = 8798, daemon = `http://127.0.0.1:${port}`;
const body = '嗨，\n\n想给你发封邮件问候一下。希望你最近一切顺利，学习和生活都进展不错。有空的话，期待听听你最近怎么样。\n\n祝好！';
const card = { id: 'C1', tool: 'mcp__gmail__gmail_send', action: '发这封邮件', source: 'gmail', letter: true,
  args: { to: 'friend@example.com', subject: '给你的一封问候', body } };
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security'] });
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const page = await (await browser.newContext({ viewport: { width: 640, height: 722 }, deviceScaleFactor: 2 })).newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionMenu: () => {},
      onCursor: () => () => {}, onCommand: () => () => {}, passthrough: () => {}, focus: async () => {}, material: () => {},
      codexTitles: async () => ({}), watchGhostty: () => {}, onGhostty: () => () => {},
    };
    window.WebSocket = class { constructor() { setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
  });
  await page.route(`${daemon}/**`, route => {
    const { pathname } = new URL(route.request().url());
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    if (pathname === '/inherent/confirmation') return json({ card });
    if (pathname === '/inherent/clarification') return json({ card: null });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
  });
  await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${port}`);
  const box = page.locator('.notch .ac-body');
  await box.waitFor({ timeout: 8000 });
  await page.waitForTimeout(1200);
  const seen = await box.evaluate(el => ({ value: el.value, client: el.clientHeight, scroll: el.scrollHeight,
    lines: (() => { const c = el.cloneNode(); c.value = el.value; c.style.height = '0'; c.style.minHeight = '0'; el.after(c); const h = c.scrollHeight; c.remove(); return h; })() }));
  await page.screenshot({ path: path.join(dir, 'notch-letter.png'), clip: { x: 60, y: 0, width: 520, height: 400 } });
  assert.equal(seen.value, body, 'the box holds the whole letter');
  assert.ok(seen.client >= seen.scroll, `the whole letter is visible (box ${seen.client}px, text ${seen.scroll}px)`);
  assert.ok(seen.client <= seen.lines + 8, `the box fits the letter, no gap below it (box ${seen.client}px, lines ${seen.lines}px)`);
  assert.deepEqual(errors, [], 'no page errors');
  console.log(`PASS the notch letter card shows all ${seen.scroll}px of the body`);
} finally { await browser.close(); server.kill(); }

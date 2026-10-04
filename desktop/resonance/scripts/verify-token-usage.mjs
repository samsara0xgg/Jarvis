// The Usage page's Token section on real data, in headless Chrome against the built page. Run after `npm run build`.
// window.jarvis.tokenUsage is wired to dist-electron/tokenUsage.js, the same function the IPC handler calls, so it
// reads the real Claude Code, Codex and Hermes logs through ccusage. Everything else the page asks the daemon for is empty.
// Writes evidence/token-usage/*.png. Reads the machine's logs; sends and writes nothing.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { tokenUsage } from '../dist-electron/tokenUsage.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = path.join(root, 'evidence', 'token-usage');
mkdirSync(dir, { recursive: true });
const web = Number(process.env.COMPANION_PORT ?? 5193), daemon = 'http://127.0.0.1:8798';
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const check = (name, pass) => { assert.ok(pass, name); console.log(`PASS ${name}`); };
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security'] });
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const page = await (await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 })).newPage();
  const errors = [], asked = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.exposeFunction('__tokenUsage', async refresh => { asked.push(refresh); return tokenUsage(refresh); });
  await page.route(`${daemon}/**`, route => route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' }));
  await page.addInitScript(() => {
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: () => () => {}, onCommand: () => () => {}, passthrough: () => {}, focus: async () => {}, material: () => {},
      codexTitles: async () => ({}), openAccount: async () => true, tokenUsage: refresh => window.__tokenUsage(refresh),
      plugins: async () => ({ request: null, plugins: [] }),
    };
    window.WebSocket = class { constructor() { setTimeout(() => this.onclose?.(), 0); } send() {} close() {} };
  });
  await page.goto(`http://127.0.0.1:${web}/?companion=1&port=8798`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
  await page.waitForTimeout(800);
  await page.locator('.companion-island-target').click({ force: true });
  await page.locator('.companion-dashboard.is-open').waitFor();
  await page.waitForTimeout(600);
  await page.locator('.ad [data-row="usage"]').evaluate(el => (el.matches('button') ? el : el.querySelector('button')).click());
  await page.waitForTimeout(500);

  const section = page.locator('.ad .tku');
  await section.waitFor();
  check('the section counts first', (await section.textContent()).includes('Counting') || await section.locator('.tku-total').count() === 1);
  await section.locator('.tku-total').waitFor({ timeout: 60_000 });
  check('opening the page asked main without forcing a rerun', asked.length === 1 && asked[0] === false);
  const total = () => section.locator('.tku-total b').textContent();
  const seven = await total();
  check('7 days is the default, with 7 bars', (await section.locator('.tku-seg [aria-pressed="true"]').textContent()) === '7 days' && await section.locator('.tku-bars > span').count() === 7);
  await section.scrollIntoViewIfNeeded();
  await page.evaluate(() => document.querySelector('.ad .pg-body').scrollTo(0, document.querySelector('.ad .tku').offsetTop - 66));
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(dir, 'usage-token-7d.png'), clip: { x: 150, y: 0, width: 340, height: 900 } });

  await section.locator('.tku-seg button', { hasText: 'Today' }).click();
  check('Today hides the daily bars', await section.locator('.tku-bars').count() === 0);
  await section.locator('.tku-seg button', { hasText: '30 days' }).click();
  check('30 days has 30 bars and at least the 7-day total', await section.locator('.tku-bars > span').count() === 30 && parseFloat((await total()).slice(1).replace(',', '')) >= parseFloat(seven.slice(1).replace(',', '')));
  await section.locator('.tku-ses > button').first().click();
  check('a session row opens its folder and per-model lines', await section.locator('.tku-det').count() === 1 && (await section.locator('.tku-det').textContent()).includes('$'));
  const more = section.locator('.tku-more');
  const count = Number((await more.textContent()).match(/\d+/)[0]);
  await more.click();
  check('"Show all N" lists every session in range', await section.locator('.tku-ses').count() === count);
  await page.screenshot({ path: path.join(dir, 'usage-token-30d.png'), clip: { x: 150, y: 0, width: 340, height: 900 } });

  await page.locator('.ad .us-sync').click();
  await page.waitForTimeout(8000);
  check('the page refresh button forces a rerun here too', asked.at(-1) === true);
  check('no page errors', errors.length === 0);
} finally {
  await browser.close();
  server.kill();
}

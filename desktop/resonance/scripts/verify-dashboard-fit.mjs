// The home Dashboard follows its blocks: the input sits right under the last block, up to the panel's tallest height,
// beyond which the list scrolls. Run after `npx vite build`; evidence lands in evidence/dashboard-fit/.
// Scenes: offline daemon (few blocks), demo data (many blocks), the same offline home in the detached window,
// and a page (Settings), which keeps its full height whatever the home holds.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = process.env.FIT_EVIDENCE_DIR ?? path.join(root, 'evidence/dashboard-fit');
mkdirSync(dir, { recursive: true });
const web = Number(process.env.FIT_PORT ?? 5211), daemon = 8798, base = `http://127.0.0.1:${web}`;
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const checks = [], check = (name, pass, detail = '') => { assert.ok(pass, `${name} ${detail}`); checks.push(name); console.log(`PASS ${name}${detail ? ` (${detail})` : ''}`); };
const VIEW_MIN = 466, VIEW_MAX = 600;
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security'] });
const errors = [];

async function scene({ offline, detached = false, height = 900 }) {
  const context = await browser.newContext({ viewport: { width: detached ? 360 : 640, height }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  page.on('pageerror', e => errors.push(e.message));
  // Offline: every request to the daemon fails and the socket closes at once, the way the screenshot's panel saw it.
  if (offline) await page.route(`http://127.0.0.1:${daemon}/**`, route => route.abort());
  await page.clock.setFixedTime(new Date('2026-09-27T12:00:00'));
  await page.addInitScript(({ detached, offline }) => {
    // The owner's home had no Today block; hiding it gives the same four.
    if (offline) try { localStorage.setItem('companion-settings-v1', JSON.stringify({ hidden: ['today'] })); } catch { /* private window */ }
    window.__size = [];
    window.jarvis = { placement: async () => ({ docked: false, topInset: detached ? 0 : 32, notchWidth: detached ? 0 : 185, surfaceWidth: detached ? 360 : 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, onDictation: () => () => {}, wearing: () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: () => () => {}, onCommand: () => () => {}, passthrough: () => {}, focus: async () => {}, material: () => {},
      dashboard: async () => ({ detached, open: true }), onDashboard: () => () => {}, dashboardDrag: () => {}, dashboardVisible: () => {},
      dashboardSize: h => window.__size.push(h) };
    window.WebSocket = class { constructor() { setTimeout(() => this.onclose?.(), 0); } send() {} close() {} };
  }, { detached, offline });
  await page.goto(`${base}/?companion=1${offline ? `&port=${daemon}` : ''}${detached ? '&detached=1' : ''}`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:#b48296!important}' });
  if (!detached) { await page.waitForSelector('.companion-island-target'); await page.waitForTimeout(400); await page.locator('.companion-island-target').click({ force: true }); }
  await page.waitForSelector('.companion-dashboard.is-open');
  await page.waitForFunction(() => { const el = document.querySelector('.companion-dashboard'); return Math.abs(Number(el.dataset.reveal) - el.offsetHeight) < .5; });
  await page.waitForTimeout(1200);
  return { context, page };
}

// The gap the owner saw: the last block's bottom to the input's top, plus what the panel and the list are doing.
const measure = page => page.evaluate(() => {
  const r = el => el.getBoundingClientRect(), list = document.querySelector('.ad .home-list'), kids = [...document.querySelectorAll('.ad .home-inner > *')];
  const last = r(kids.at(-1)), input = r(document.querySelector('.ad .cmp')), panel = document.querySelector('.companion-dashboard');
  return { blocks: kids.map(k => k.dataset.block), gap: input.top - last.bottom, listGap: input.top - r(list).bottom, view: r(document.querySelector('.ad .view')).height, panel: panel.offsetHeight,
    scrolls: list.scrollHeight > list.clientHeight + 1, listH: list.clientHeight, header: document.querySelector('.ad .next-event').textContent, windowH: innerHeight };
});

try {
  for (let i = 0; i < 50; i++) { try { await fetch(`${base}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }

  // 1. A daemon that is away: few blocks, so the input follows the last of them.
  {
    const { context, page } = await scene({ offline: true }), m = await measure(page);
    console.log('offline', JSON.stringify(m));
    check('F1 the scene is the owner\'s: offline, a handful of blocks', m.header.startsWith('Offline') && m.blocks.join() === 'agents,now,usage,tiles', m.blocks.join(','));
    check('F2 the list is not scrolling: every block is shown', !m.scrolls);
    check('F3 the input sits right under the last block', m.gap >= 0 && m.gap <= 16, `gap ${m.gap.toFixed(1)}px`);
    check('F4 the panel is shorter than its old floor', m.view < VIEW_MIN, `view ${m.view}px`);
    await page.screenshot({ path: path.join(dir, 'few-blocks.png'), clip: { x: 130, y: 0, width: 380, height: 760 } });
    // The page keeps its full height whatever the home holds.
    await page.locator('.ad .cb[data-row="settings"]').click(); await page.waitForTimeout(900);
    const pageView = await page.evaluate(() => document.querySelector('.ad .view').getBoundingClientRect().height);
    check('F5 a page opened from the short home still has the full panel height', pageView >= VIEW_MIN, `view ${pageView}px`);
    await page.screenshot({ path: path.join(dir, 'few-blocks-settings.png'), clip: { x: 130, y: 0, width: 380, height: 760 } });
    await page.locator('.ad .pg-back').click(); await page.waitForTimeout(900);
    check('F6 back on the home the panel closes up again', (await measure(page)).view === m.view);
    await context.close();
  }

  // 2. Many blocks (the demo home): up to the tallest height, then the list scrolls.
  {
    const { context, page } = await scene({ offline: false }), m = await measure(page);
    console.log('many', JSON.stringify(m));
    check('M1 the demo home has many blocks', m.blocks.length >= 7, m.blocks.join(','));
    check('M2 the panel is capped at its tallest height', m.view === VIEW_MAX, `view ${m.view}px`);
    check('M3 the list scrolls', m.scrolls);
    check('M4 the input still sits right under the list', m.listGap >= 0 && m.listGap <= 16, `gap ${m.listGap.toFixed(1)}px`);
    const moved = await page.evaluate(() => { const l = document.querySelector('.ad .home-list'); l.scrollTop = 9999; return l.scrollTop; });
    check('M5 the list scrolls to its end', moved > 0, `scrollTop ${moved}`);
    await page.screenshot({ path: path.join(dir, 'many-blocks.png'), clip: { x: 130, y: 0, width: 380, height: 760 } });
    await context.close();
  }

  // 3. The detached window: same panel, same height; the window only has to be at least as tall.
  {
    const { context, page } = await scene({ offline: true, detached: true }), m = await measure(page);
    console.log('detached', JSON.stringify(m), JSON.stringify(await page.evaluate(() => window.__size)));
    check('D1 detached and offline: the input sits right under the last block', m.gap >= 0 && m.gap <= 16 && !m.scrolls, `gap ${m.gap.toFixed(1)}px`);
    check('D2 the detached panel is shorter than its old floor', m.view < VIEW_MIN, `view ${m.view}px`);
    await page.screenshot({ path: path.join(dir, 'detached-few-blocks.png') });
    await context.close();
  }
  check('no renderer errors', errors.length === 0, errors.join(' | '));
  console.log(`${checks.length} checks passed`);
} finally { await browser.close(); server.kill(); }

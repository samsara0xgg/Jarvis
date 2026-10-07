// The companion window fits what is shown (src/fitWindow.ts), in headless Chrome against the built page: no window is opened.
// A stand-in `window.jarvis.fit` records every size the page asks for; a frame sampler checks that what is shown never
// reaches past the last size asked for (the window would clip it), that the window grows before content, and that it shrinks
// once the content has settled. Run after `npm run build`.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const web = Number(process.env.FIT_PORT ?? 5197);
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
const check = (name, pass, detail = '') => { assert.ok(pass, `${name} ${detail}`); console.log(`PASS ${name}`); };
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const page = await (await browser.newContext({ viewport: { width: 640, height: 722 } })).newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(() => {
    // The window starts as the whole stage; every ask replaces it.
    window.__sent = { w: 640, h: 722 }; window.__asks = []; window.__over = 0; window.__cursorCb = null;
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: cb => { window.__cursorCb = cb; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {},
      fit: (w, h) => { window.__sent = { w, h }; window.__asks.push({ w, h, at: performance.now() }); },
    };
    // Every frame: how far any shown part reaches past the window as last asked for.
    // What is painted: the panes and the Dashboard's material as they grow, not the finished boxes the window is sized for.
    const parts = ['.companion-island-target', '.notch-shape path', '.notch-pane', '.talk', '.dusk-material', '.companion-chip', '.companion-menu'];
    const sample = () => {
      for (const sel of parts) for (const el of document.querySelectorAll(sel)) {
        if (!el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
        const r = el.getBoundingClientRect();
        if (r.width) window.__over = Math.max(window.__over, r.right - window.__sent.w, r.bottom - window.__sent.h);
      }
      requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  });
  await page.goto(`http://127.0.0.1:${web}/?companion=1`);
  await page.waitForTimeout(1500);
  const sent = () => page.evaluate(() => window.__sent);
  const shown = sel => page.evaluate(sel => { const r = document.querySelector(sel).getBoundingClientRect(); return { right: r.right, bottom: r.bottom }; }, sel);

  let s = await sent();
  check('idle: the window shrank to the island and the ball (not the 640 x 722 stage)', s.w < 640 && s.h <= 140, JSON.stringify(s));
  const island = await shown('.companion-island-target');
  check('idle: it still holds the island', s.w >= island.right && s.h >= island.bottom, JSON.stringify(s));

  // A poke starts the demo conversation: the talk area opens under her.
  await page.evaluate(() => { window.__over = 0; });
  await page.locator('.companion-hit').click({ force: true });
  await page.waitForFunction(() => document.querySelector('.talk')?.hasAttribute('data-hit'));
  await page.waitForTimeout(1200);
  s = await sent();
  let talk = await shown('.talk');
  check('talk area up: the window holds it, shadow included', s.w >= talk.right + 32 - 1 && s.h >= talk.bottom + 56 - 1, JSON.stringify({ s, talk }));
  check('talk area up: it opened without ever reaching past the window by more than a frame of growth', await page.evaluate(() => window.__over) < 30, String(await page.evaluate(() => window.__over)));

  // The Dashboard opens from the island and grows to its page.
  await page.evaluate(() => { window.__over = 0; });
  await page.locator('.companion-island-target').click({ force: true });
  await page.waitForFunction(() => document.querySelector('.companion-dashboard.is-open'));
  await page.waitForTimeout(1500);
  s = await sent();
  const dash = await shown('.companion-dashboard');
  check('Dashboard open: the window holds it', s.w >= dash.right && s.h >= dash.bottom, JSON.stringify({ s, dash }));
  const edge = await page.evaluate(() => Math.max(...['.notch-shape path', '.companion-dashboard'].map(q => document.querySelector(q).getBoundingClientRect().right)));
  check('Dashboard open and settled: the window ends within 2 pt of the visible right edge', s.w - edge <= 2 && s.w >= edge, JSON.stringify({ s, edge }));
  check('Dashboard open: it grew without clipping beyond a frame of growth', await page.evaluate(() => window.__over) < 30, String(await page.evaluate(() => window.__over)));
  check('the page still sees the stage\'s height, not the window\'s', await page.evaluate(() => window.innerHeight) === 722 || await page.evaluate(() => window.innerHeight) === Math.min(await page.evaluate(() => screen.height), 722));

  // Closing it, and the talk area folding: the window comes back to the island, only after it has settled.
  await page.locator('.companion-island-target').click({ force: true });
  const before = await page.evaluate(() => window.__asks.length);
  await page.waitForTimeout(400);
  check('closing: no shrink while the Dashboard is still folding away', await page.evaluate(n => window.__asks.length === n, before));
  await page.waitForTimeout(14000);
  s = await sent();
  // (The pointer still rests on the island, so she peeks a little below it.)
  check('everything folded: the window is the island again', s.w < 640 && s.h <= 160, JSON.stringify(s));
  check('no page errors', errors.length === 0, errors.join(' | '));
  console.log('asks:', await page.evaluate(() => window.__asks.map(a => `${a.w}x${a.h}`).join(' ')));
} finally { await browser.close(); server.kill(); }

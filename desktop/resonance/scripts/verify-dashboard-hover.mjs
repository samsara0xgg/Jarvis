// The Dashboard comes by a rest on the notch and folds by itself (without a notch, only by a click, and she is out of sight until the pointer comes near); a rest on her is only a peek, so a click on her starts voice;
// passing under the island leaves her home.
// Headless Chrome against the built page with the native bridge stubbed, like verify-companion.
// Run after `npm run build`. Screenshots land in evidence/dashboard-hover/.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = process.env.COMPANION_EVIDENCE_DIR ?? path.join(root, 'evidence/dashboard-hover');
mkdirSync(dir, { recursive: true });
const port = Number(process.env.COMPANION_PORT ?? 5193);
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(port), '--strictPort'], { cwd: root, stdio: 'ignore' });
const checks = [], check = (name, pass) => { assert.ok(pass, name); checks.push(name); console.log('ok', name); };
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${port}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const page = await (await browser.newContext({ viewport: { width: 640, height: 722 }, deviceScaleFactor: 2 })).newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.__state = { passthrough: true };
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: callback => { window.__placement = callback; return () => {}; },
      onDisplayLeave: () => () => {}, onDictation: () => () => {}, wearing: () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; },
      onCommand: () => () => {},
      passthrough: value => { window.__state.passthrough = value; },
      focus: async on => { window.__state.focus = on; },
      material: () => {},
    };
  });
  await page.goto(`http://127.0.0.1:${port}/?companion=1`);
  const hit = page.locator('.companion-hit');
  const place = () => hit.getAttribute('data-place');
  const open = () => page.locator('.companion-dashboard.is-open').count().then(n => n === 1);
  const move = async (x, y) => { await page.mouse.move(x, y); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [x, y]); };
  // A rest: the native feed only sends a point when it changes, so a resting pointer is one sample, then silence.
  const glide = async (from, to, ms) => { const steps = Math.max(2, Math.round(ms / 16)); for (let i = 1; i <= steps; i++) { await move(from.x + (to.x - from.x) * i / steps, from.y + (to.y - from.y) * i / steps); await page.waitForTimeout(16); } };
  const shot = name => page.screenshot({ path: path.join(dir, `${name}.png`), clip: { x: 120, y: 0, width: 400, height: 300 } });
  const far = { x: 600, y: 560 };
  await page.waitForTimeout(800);
  check('rests in the island', await place() === 'home');

  // Passing under the island, slowly or resting, does not bring her out.
  await glide({ x: 100, y: 60 }, { x: 196, y: 60 }, 300); await page.waitForTimeout(900);
  check('a rest just under the island leaves her home', await place() === 'home' && !await open());
  check('… and puts no keyboard chip beside her', await page.locator('.companion-chip.is-open').count() === 0);
  await glide({ x: 196, y: 60 }, { x: 520, y: 60 }, 200); await page.waitForTimeout(400);
  check('sliding along under the menu bar does not either', await place() === 'home' && !await open());
  await shot('01-passed-under');

  // A quick pass along the menu bar row, across the island, does not open it.
  await move(...Object.values(far)); await page.waitForTimeout(300);
  await glide({ x: 60, y: 12 }, { x: 600, y: 12 }, 180); await page.waitForTimeout(500);
  check('a quick pass across the island does not open the Dashboard', !await open());

  // A rest on her (left of the notch) is only a peek: the Dashboard stays shut, and a click on her starts voice.
  await move(...Object.values(far)); await page.waitForTimeout(300);
  await move(190, 14); await page.waitForTimeout(1200);
  check('a rest on her makes her peek and leaves the Dashboard shut', await place() === 'peek' && !await open());
  await hit.click({ force: true }); await page.locator('.talk[data-hit]').waitFor({ timeout: 3000 });
  check('… so a click on her starts voice, with no Dashboard', !await open());
  await hit.click({ force: true }); // while she listens, a second click ends voice
  await move(...Object.values(far)); await page.waitForFunction(() => !document.querySelector('.talk[data-hit]'), null, { timeout: 16000 });
  await page.waitForFunction(() => document.querySelector('.companion-hit')?.dataset.place === 'home', null, { timeout: 16000 });
  // A rest on the notch opens it, after about 0.3 s.
  await move(320, 14);
  await page.waitForTimeout(200);
  check('0.2 s into a rest on the notch it is not open yet', !await open());
  await page.waitForTimeout(400);
  check('0.6 s into it the Dashboard is open', await open());
  await page.waitForTimeout(500);
  check('… and she stays in her home', await place() === 'home');
  await shot('02-rest-on-the-notch');
  // Leaving folds it ~0.6 s later; coming back within that keeps it.
  await move(320, 200); await page.waitForTimeout(300);
  await move(...Object.values(far)); await page.waitForTimeout(350);
  check('0.35 s after leaving it is still up', await open());
  await move(320, 200); await page.waitForTimeout(900);
  check('back on the panel within 0.6 s, it stays', await open());
  await move(...Object.values(far)); await page.waitForTimeout(1000);
  check('1 s after leaving, it has folded by itself', !await open());

  // A click opens it too, and that one folds by itself as well.
  await move(320, 14);
  await page.locator('.companion-island-target').click({ position: { x: 155, y: 14 }, force: true });
  await page.waitForTimeout(100);
  check('a click on the notch opens it at once', await open());
  await move(...Object.values(far)); await page.waitForTimeout(1000);
  check('a click-opened Dashboard folds by itself once the pointer leaves', !await open());
  // A click right after a rest opened it (the same reach) keeps it; a second click closes it.
  await move(320, 14); await page.locator('.companion-dashboard.is-open').waitFor();
  await page.locator('.companion-island-target').click({ position: { x: 155, y: 14 }, force: true }); await page.waitForTimeout(200);
  check('a click on a Dashboard a rest just opened keeps it', await open());
  await page.locator('.companion-island-target').click({ position: { x: 155, y: 14 }, force: true }); await page.waitForTimeout(500);
  check('another click closes it', !await open());
  await move(...Object.values(far)); await page.waitForTimeout(400);

  // Typing in it holds it up with the pointer away.
  await move(320, 14); await page.locator('.companion-dashboard.is-open').waitFor();
  await move(320, 300); await page.waitForTimeout(800);
  // Without a daemon (no port) the panel has no 「问问 Jarvis…」 field; a stand-in field in the panel holds it the same way.
  await page.evaluate(() => { if (!document.querySelector('.companion-dashboard textarea')) document.querySelector('.companion-dashboard .dusk-content').append(Object.assign(document.createElement('textarea'), { className: 'stand-in' })); });
  const field = page.locator('.companion-dashboard textarea').first();
  await field.focus(); await page.keyboard.type('明天');
  await move(...Object.values(far)); await page.waitForTimeout(1200);
  check('while you type in it, it stays with the pointer away', await open());
  await page.evaluate(() => document.activeElement?.blur());
  await move(far.x - 1, far.y); await page.waitForTimeout(1000);
  check('once the field lets go, it folds', !await open());

  // A screen with no notch: with nothing going on and the pointer away, all of her is out of sight and takes no clicks; the pointer
  // coming near shows the pill alone. A rest never opens the Dashboard there, a click on an end of the pill does; its middle is her.
  const stowed = () => page.locator('.companion.is-stowed').count().then(n => n === 1);
  await page.evaluate(() => window.__placement({ docked: false, topInset: 32, notchWidth: 0, surfaceWidth: 640, compactWidth: 0, displayId: 2 }));
  await page.waitForTimeout(900);
  check('without a notch and with the pointer away, she is out of sight and lets clicks through', await stowed() && await page.evaluate(() => getComputedStyle(document.querySelector('.companion')).opacity === '0' && window.__state.passthrough));
  await move(320, 50); await page.waitForTimeout(400);
  check('the pointer coming near shows the pill', !await stowed() && await page.evaluate(() => getComputedStyle(document.querySelector('.companion')).opacity === '1'));
  await move(320, 14); await page.waitForTimeout(900);
  check('without a notch a rest on the middle of the pill (her) only peeks', !await open() && await place() === 'peek');
  await move(375, 14); await page.waitForTimeout(900);
  check('… and a rest on an end of the pill does not open the Dashboard', !await open());
  await shot('03-external-pill');
  await page.mouse.click(375, 14); await page.waitForTimeout(150);
  check('… a click on it does', await open());
  await move(...Object.values(far)); await page.waitForTimeout(1000);
  check('… and it folds when the pointer leaves', !await open());
  await page.waitForTimeout(800);
  check('… and she goes out of sight again', await stowed());
  await move(320, 60); await page.waitForTimeout(900);
  check('without a notch, a rest under the pill leaves her home', await place() === 'home');

  check(`no page errors (${errors.join(' | ')})`, errors.length === 0);
  console.log(`${checks.length} checks passed`);
} finally {
  await browser.close();
  server.kill();
}

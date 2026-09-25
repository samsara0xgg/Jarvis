// Companion prototype: scenes 01-07 in headless Chrome against the built page.
// The native bridge is stubbed; a fake hardware cutout sits on top like the real notch.
// Run after `npm run build`. Screenshots land in evidence/companion/.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = process.env.COMPANION_EVIDENCE_DIR ?? path.join(root, 'evidence/companion');
mkdirSync(dir, { recursive: true });
const port = 5191;
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(port), '--strictPort'], { cwd: root, stdio: 'ignore' });
const checks = [], check = (name, pass) => { assert.ok(pass, name); checks.push(name); };
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${port}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const context = await browser.newContext({ viewport: { width: 640, height: 592 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.__state = { passthrough: true, glass: [], ready: 0 };
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: callback => { window.__placement = callback; return () => {}; },
      onDisplayLeave: callback => { window.__leave = callback; return () => {}; },
      displayReady: () => { window.__state.ready++; },
      onCursor: callback => { window.__cursor = callback; return () => {}; },
      onCommand: callback => { window.__command = callback; return () => {}; },
      passthrough: value => { window.__state.passthrough = value; },
      focus: async () => {},
      material: rects => { window.__state.glass = rects; },
    };
  });
  await page.goto(`http://127.0.0.1:${port}/?companion=1`);
  // Desktop stand-in: a quiet blue-gray wallpaper, the menu bar, and the opaque camera cutout.
  await page.addStyleTag({ content: `html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}
    body::before{content:'';position:fixed;inset:0 0 auto;height:32px;background:rgb(255 255 255/.18);backdrop-filter:blur(20px)}
    body::after{content:'';position:fixed;z-index:10;pointer-events:none;top:0;left:227.5px;width:185px;height:32px;background:#000;border-radius:0 0 10px 10px}
    body.external::after{display:none}` });
  const hit = page.locator('.companion-hit');
  const place = () => hit.getAttribute('data-place');
  const move = async (x, y) => { await page.mouse.move(x, y); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [x, y]); };
  const shot = (name, clip = { x: 120, y: 0, width: 400, height: 210 }) => page.screenshot({ path: path.join(dir, `${name}.png`), clip });
  const waitPlace = async value => { await page.waitForFunction(v => document.querySelector('.companion-hit')?.dataset.place === v, value); await page.waitForTimeout(900); };
  const lobe = { x: 195.5, y: 16 }, out = { x: 195.5, y: 72 };

  await page.waitForTimeout(800);
  check('01 rests in the island', await place() === 'home');
  await shot('01-home');
  await move(170, 10);
  await page.waitForTimeout(80);
  check('01 the island takes clicks, so menu bar items hidden behind it are never hit', await page.evaluate(() => window.__state.passthrough === false));
  await move(140, 10);
  await page.waitForTimeout(80);
  check('01 the menu bar beside the island still gets its clicks', await page.evaluate(() => window.__state.passthrough === true));
  await move(lobe.x - 20, 14);
  await waitPlace('peek');
  check('01 peeks when the cursor approaches the island', true);
  await shot('01-peek');
  await move(out.x, out.y - 6);
  await waitPlace('out');
  check('02 comes out under the island on hover', await page.evaluate(() => window.__state.passthrough === false));
  check('02 keyboard chip appears beside her', await page.locator('.companion-chip.is-open').count() === 1);
  await shot('02-out-chip');
  await move(460, 400);
  await page.waitForTimeout(80);
  check('02 empty space passes clicks through', await page.evaluate(() => window.__state.passthrough === true));
  await move(out.x, out.y);
  await page.waitForTimeout(400);
  await move(out.x + 26 + 12 + 16, out.y);
  await page.locator('.companion-chip button').click();
  check('03 composer opens beneath her', await page.locator('.companion-composer.is-open').count() === 1);
  await page.keyboard.type('帮我整理今天的任务', { delay: 60 });
  await page.waitForTimeout(700);
  check('03 draft is typed into the composer', await page.locator('.companion-composer input').inputValue() === '帮我整理今天的任务');
  check('03 composer has native glass behind it', await page.evaluate(() => window.__state.glass.some(r => Math.round(r.width) === 300 && r.opacity > .9)));
  await shot('03-composer', { x: 20, y: 0, width: 400, height: 210 });
  await page.keyboard.press('Enter');
  await page.locator('.companion-bubble.is-open').waitFor();
  await page.waitForTimeout(500);
  await shot('03-reply');
  await page.waitForFunction(() => !document.querySelector('.companion-bubble.is-open'), null, { timeout: 6000 });
  check('03 text reply types out and clears', true);

  // Poke: press and hold briefly, then release into listening.
  await move(out.x, out.y);
  const box = await hit.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(160);
  await shot('02-press');
  await page.mouse.up();
  await page.locator('.companion-strip.is-open').waitFor();
  check('04 poke starts listening with a live caption strip', true);
  await page.waitForFunction(() => document.querySelector('.strip-text')?.textContent === '把今天的任务整理一下', null, { timeout: 5000 });
  await page.waitForTimeout(250);
  await shot('04-listening');
  await page.locator('.companion-bubble.is-open').waitFor({ timeout: 5000 });
  await page.waitForTimeout(500);
  check('05 she answers in a bubble beneath her', (await page.locator('.companion-bubble').textContent()).includes('好，我来整理。'));
  await shot('05-speaking');
  await hit.click({ force: true });
  await page.waitForTimeout(300);
  check('05 poke while speaking interrupts and keeps listening', await page.locator('.companion-strip.is-open').count() === 1 && await page.locator('.companion-bubble.is-open').count() === 0);
  await hit.click({ force: true });
  await page.waitForTimeout(300);
  check('05 poke while listening ends voice', await page.locator('.companion-strip.is-open').count() === 0);

  await move(320, 14);
  await page.locator('.companion-dashboard.is-open').waitFor();
  await waitPlace('dock');
  check('06 dashboard opens from the notch and she docks on its edge', true);
  await shot('06-dock', { x: 120, y: 0, width: 400, height: 300 });
  await move(320, 200);
  await page.waitForTimeout(200);
  await move(600, 560);
  await page.waitForFunction(() => !document.querySelector('.companion-dashboard.is-open'), null, { timeout: 3000 });
  await waitPlace('home');
  check('06 closing the dashboard sends her home', true);
  await shot('06-home-again');

  // 07: the cursor rests on an external screen with no notch. She sinks into this island,
  // Electron moves the window only after display-ready, and she comes up dead centre there.
  const centre = async () => { const b = await hit.boundingBox(); return b.x + b.width / 2; };
  await page.evaluate(() => window.__leave());
  await page.waitForFunction(() => window.__state.ready === 1, null, { timeout: 2000 });
  check('07 leaving a screen waits for her to go home before the window moves', await place() === 'home');
  await page.evaluate(() => { document.body.classList.add('external'); window.__placement({ docked: false, topInset: 32, notchWidth: 0, surfaceWidth: 640, compactWidth: 0, displayId: 2 }); });
  await page.waitForTimeout(900);
  check('07 on a screen without a notch she lives dead centre', await place() === 'home' && Math.abs(await centre() - 320) < 2);
  await shot('07-external-home', { x: 120, y: 0, width: 400, height: 210 });
  await move(262, 10);
  await page.waitForTimeout(80);
  check('07 the pill takes clicks too', await page.evaluate(() => window.__state.passthrough === false));
  await move(320, 14);
  await waitPlace('peek');
  check('07 the pill centre makes her peek', true);
  await move(320, out.y);
  await waitPlace('out');
  check('07 she comes out straight below the pill', Math.abs(await centre() - 320) < 2);
  await shot('07-external-out', { x: 120, y: 0, width: 400, height: 210 });
  await move(600, 560);
  await waitPlace('home');
  await move(268, 14);
  await page.locator('.companion-dashboard.is-open').waitFor();
  await waitPlace('dock');
  check('07 a wing of the pill opens the Dashboard', true);
  await shot('07-external-dock', { x: 120, y: 0, width: 400, height: 300 });
  await move(600, 560);
  await waitPlace('home');
  check('no page errors', errors.length === 0);
  await context.close();
  writeFileSync(path.join(dir, 'verification.json'), JSON.stringify({ checks, errors }, null, 2));
  console.log(`${checks.length} checks passed; evidence in ${dir}`);
} finally {
  await browser.close();
  server.kill();
}

// Run against the final npm run build output; this script never builds it.
// DUSK_URL reuses a preview server. DUSK_EVIDENCE_DIR changes the evidence folder.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';

const require = createRequire(import.meta.url);
// Playwright bundles pngjs, so screenshot decoding needs no extra dependency.
const { PNG } = require(path.join(path.dirname(require.resolve('playwright-core/package.json')), 'lib/utilsBundle.js'));
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = process.env.DUSK_EVIDENCE_DIR ?? path.join(root, 'evidence/dusk-dashboard');
const port = Number(process.env.DUSK_PORT ?? 5205);
const url = process.env.DUSK_URL ?? `http://127.0.0.1:${port}`;
const server = process.env.DUSK_URL ? null : spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(port), '--strictPort'], { cwd: root, stdio: 'ignore' });
const report = { matrix: [], attached: null, detached: null, rendererErrors: [] };
mkdirSync(dir, { recursive: true });
let browser;

async function fixture({ dpr = 2, top = 32, notch = 185, detached = false } = {}) {
  const context = await browser.newContext({ viewport: { width: detached ? 360 : 640, height: 760 }, deviceScaleFactor: dpr });
  const page = await context.newPage();
  page.on('pageerror', error => report.rendererErrors.push(error.message));
  await page.clock.setFixedTime(new Date('2026-09-27T12:00:00'));
  await page.addInitScript(({ top, notch, detached }) => {
    const subscribers = new Set();
    const state = window.__dusk = {
      calls: [], drags: [], failNext: false, value: { detached, open: detached },
      push(value) { state.value = value; subscribers.forEach(callback => callback(value)); },
    };
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: detached ? 0 : top, notchWidth: detached ? 0 : notch, surfaceWidth: detached ? 360 : 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, onDictation: () => () => {},
      wearing: () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: () => () => {}, onCommand: () => () => {}, passthrough: () => {}, focus: async () => {}, material: () => {},
      dashboard: async (action, options) => {
        state.calls.push({ action, options });
        if (action === 'state') return state.value;
        if (action === 'detach' && state.failNext) { state.failNext = false; throw new Error('Acceptance fixture: native window failed'); }
        if (action === 'detach') state.push({ detached: true, open: true });
        if (action === 'attach') state.push({ detached: false, open: true });
        if (action === 'open') state.push({ ...state.value, open: true });
        if (action === 'close') state.push({ ...state.value, open: false });
        return state.value;
      },
      onDashboard: callback => { subscribers.add(callback); return () => subscribers.delete(callback); },
      dashboardDrag: phase => state.drags.push(phase), dashboardSize: () => {}, dashboardVisible: () => {},
    };
  }, { top, notch, detached });
  await page.goto(`${url}/?companion=1${detached ? '&detached=1' : ''}`);
  // A bright desktop catches transparent gaps; hardware is represented only at its actual cutout.
  await page.addStyleTag({ content: `html,body{height:100%}body{background:#b48296!important}
    ${!detached && notch ? `body::after{content:'';position:fixed;z-index:20;pointer-events:none;top:0;left:${(640 - notch) / 2}px;width:${notch}px;height:${top}px;background:#000;border-radius:0 0 10px 10px}` : ''}` });
  if (!detached) {
    await page.waitForFunction(top => Math.abs(document.querySelector('.companion-island-target')?.getBoundingClientRect().height - top) < .1, top);
    await page.locator('.companion-island-target').click();
  }
  await settled(page);
  return { context, page };
}

async function settled(page) {
  await page.waitForFunction(() => {
    const el = document.querySelector('.companion-dashboard');
    return el?.classList.contains('is-open') && Math.abs(Number(el.dataset.reveal) - el.offsetHeight) < .1 && getComputedStyle(el).visibility === 'visible';
  });
  await page.waitForTimeout(160);
}

async function isolate(page) {
  // Keep the production material, edge treatment, stars, shape, and layout intact.
  return page.addStyleTag({ content: '.dusk-content,.companion-canvas,.companion-stage,.notch{visibility:hidden!important}' });
}

function pixel(png, dpr, x, y) {
  const at = (Math.floor(y * dpr) * png.width + Math.floor(x * dpr)) * 4;
  return Array.from(png.data.subarray(at, at + 3));
}

function blackRectangle(png, dpr, x0, y0, x1, y1, label) {
  let brightest = 0;
  for (let y = Math.ceil(y0 * dpr); y < Math.floor(y1 * dpr); y++) {
    for (let x = Math.ceil(x0 * dpr); x < Math.floor(x1 * dpr); x++) {
      const at = (y * png.width + x) * 4;
      brightest = Math.max(brightest, png.data[at], png.data[at + 1], png.data[at + 2]);
    }
  }
  assert.equal(brightest, 0, `${label}: expected opaque pure black, brightest channel ${brightest}`);
  return brightest;
}

function inspectAttached(buffer, dpr, box, label) {
  const png = PNG.sync.read(buffer), x = box.x + 180;
  const band = blackRectangle(png, dpr, box.x + 8, box.y, box.x + box.width - 8, box.y + 28, `${label} 0–27 pt band`);
  const bridge = blackRectangle(png, dpr, box.x + 8, 0, box.x + box.width - 8, box.y, `${label} menu connection`);
  // Inspect actual raster samples, including each authored stop, rather than CSS strings.
  const strip = [];
  for (let y = Math.ceil((box.y + 28) * dpr); y <= Math.floor((box.y + 94) * dpr); y++) strip.push(pixel(png, dpr, x, y / dpr));
  const jump = Math.max(...strip.slice(1).map((rgb, i) => Math.max(...rgb.map((channel, c) => Math.abs(channel - strip[i][c])))));
  assert.ok(jump <= 4, `${label}: abrupt gradient change (${jump} RGB levels per physical pixel)`);
  assert.ok(Math.max(...strip[0]) <= 2, `${label}: gradient must emerge from black at 28 pt`);
  assert.ok(Math.max(...strip.at(-1)) >= 12, `${label}: colour must be visible below the transition`);
  const glass = [140, 250, box.height - 50].map(y => pixel(png, dpr, x, box.y + y));
  assert.ok(glass.every(rgb => Math.max(...rgb) >= 10 && Math.max(...rgb) <= 85), `${label}: lower material must retain dark glass colour (${JSON.stringify(glass)})`);
  return { band, bridge, maximumAdjacentJump: jump, stops: [28, 48, 70, 92].map(y => ({ y, rgb: pixel(png, dpr, x, box.y + y) })), glass };
}

async function dragHeader(page, distance) {
  const box = await page.locator('.next-event').boundingBox();
  assert.ok(box, 'header text must remain available as a drag handle');
  const x = box.x + Math.min(30, box.width / 2), y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x, y + distance, { steps: 6 });
  await page.mouse.up();
  await page.waitForTimeout(200);
}

async function detachCalls(page) {
  return page.evaluate(() => window.__dusk.calls.filter(call => call.action === 'detach'));
}

try {
  for (let i = 0; ; i++) {
    try { const response = await fetch(url); if (response.ok) break; } catch {}
    assert.ok(i < 50, `built preview did not start at ${url}`);
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const moods = [
    { name: 'blue', glow: '108 156 255' },
    { name: 'red', glow: '255 80 60' },
    { name: 'white', glow: '255 255 255' },
    { name: 'deep', glow: '255 255 255', deep: true },
  ];
  for (const dpr of [1, 2]) for (const top of [28.5, 32]) for (const notch of [185, 0]) {
    const { context, page } = await fixture({ dpr, top, notch });
    try {
      const box = await page.locator('.companion-dashboard').boundingBox();
      assert.ok(Math.abs(box.y - top) < .01 && box.width === 360, 'attached geometry must use the actual display inset');
      await isolate(page);
      for (const mood of moods) {
        const colour = await page.evaluate(mood => {
          const panel = document.querySelector('.companion-dashboard'), ad = panel.querySelector('.ad');
          panel.style.setProperty('--glow', mood.glow);
          ad.toggleAttribute('data-deep', !!mood.deep);
          return { dusk: getComputedStyle(panel).getPropertyValue('--dusk-glow').trim(), deep: getComputedStyle(panel).getPropertyValue('--deep').trim() };
        }, mood);
        assert.equal(colour.dusk, mood.deep ? colour.deep : mood.glow, 'mood/deep fixture must reach the real material variable');
        const buffer = await page.screenshot({ animations: 'disabled' });
        const label = `DPR ${dpr}, top ${top}, notch ${notch}, ${mood.name}`;
        report.matrix.push({ dpr, top, notch, mood: mood.name, ...inspectAttached(buffer, dpr, box, label) });
        if (dpr === 2 && top === 32 && notch === 185 && mood.name === 'blue') writeFileSync(path.join(dir, 'attached-background.png'), buffer);
      }
    } finally { await context.close(); }
  }

  const attached = await fixture();
  try {
    const page = attached.page;
    // A real header control must still navigate, without beginning a tear gesture.
    await page.locator('.corner [data-row="settings"]').click();
    await page.locator('.ad[data-page="settings"]').waitFor();
    assert.equal((await detachCalls(page)).length, 0);
    await page.locator('.pg-back').click();
    await page.locator('.ad:not([data-page])').waitFor();
    await page.waitForTimeout(200);
    await page.evaluate(() => { window.__dusk.failNext = true; });
    await page.keyboard.press('Meta+Shift+ArrowDown');
    await page.getByRole('alert').waitFor();
    assert.equal((await detachCalls(page)).length, 1, 'Cmd Shift Down must request detachment');
    assert.ok(await page.locator('.companion-dashboard').evaluate(el => el.classList.contains('is-open')), 'failed native detachment must leave the attached dashboard open');
    await dragHeader(page, 70);
    assert.equal((await detachCalls(page)).length, 1, 'a drag below 78 pt must not detach');
    assert.equal(await page.locator('.companion-dashboard').evaluate(el => Number.parseFloat(el.style.getPropertyValue('--dusk-tear'))), 0, 'a short drag must return to its origin');
    await dragHeader(page, 95);
    const dragging = await detachCalls(page);
    assert.equal(dragging.length, 2, 'a header drag above 78 pt must request detachment');
    assert.equal(dragging[1].options.dragging, true);
    await page.waitForFunction(() => !document.querySelector('.companion-dashboard').classList.contains('is-open'));
    await page.evaluate(() => window.__dusk.push({ detached: false, open: true }));
    await settled(page);
    await page.keyboard.press('Meta+Shift+ArrowDown');
    const calls = await detachCalls(page);
    assert.equal(calls.length, 3);
    assert.equal(calls[2].options.dragging, false, 'keyboard detachment must not start native window dragging');
    await page.waitForFunction(() => !document.querySelector('.companion-dashboard').classList.contains('is-open'));
    report.attached = { failedDetachPreservedPanel: true, shortDragReturned: true, nativeStateClosedPanel: true, detachRequests: calls };
  } finally { await attached.context.close(); }

  const detached = await fixture({ detached: true });
  try {
    const page = detached.page;
    assert.equal(await page.locator('.companion-canvas,.companion-stage,.companion-island-target').count(), 0, 'a detached window must not contain her island/home');
    assert.equal(await page.locator('.dusk-outline path').getAttribute('d'), '', 'a detached window must not draw a menu-bar bridge');
    const isolation = await isolate(page);
    const png = PNG.sync.read(await page.screenshot({ animations: 'disabled' }));
    const topPixels = [6, 14, 27].map(y => pixel(png, 2, 180, y));
    assert.ok(topPixels.every(rgb => Math.max(...rgb) >= 12), 'detached glass must begin at the top without a black band');
    await isolation.evaluate(el => el.remove());
    await page.locator('.corner [data-row="settings"]').click();
    await page.locator('.ad[data-page="settings"]').waitFor();
    await page.locator('.pg-back').click();
    await page.locator('.ad:not([data-page])').waitFor();
    await page.waitForTimeout(200);
    const input = page.locator('.cmp input');
    await input.fill('Detached dashboard acceptance');
    await page.locator('.cmp .send').click();
    assert.equal(await input.inputValue(), '', 'the detached composer must submit normally');
    await page.locator('.you').filter({ hasText: 'Detached dashboard acceptance' }).waitFor();
    await dragHeader(page, 20);
    const dragPhases = await page.evaluate(() => window.__dusk.drags);
    assert.ok(dragPhases.includes('start') && dragPhases.includes('move') && dragPhases.includes('end'), 'detached header dragging must reach the native bridge');
    await page.keyboard.press('Meta+Shift+ArrowUp');
    const attachRequests = await page.evaluate(() => window.__dusk.calls.filter(call => call.action === 'attach'));
    assert.equal(attachRequests.length, 1, 'Cmd Shift Up must request reattachment');
    const inputBox = await input.boundingBox();
    assert.ok(inputBox.y + inputBox.height <= 760, 'detached composer must remain inside the window');
    await page.screenshot({ path: path.join(dir, 'detached.png'), animations: 'disabled' });
    report.detached = { topPixels, contentNavigation: true, composerSubmitted: true, dragPhases, attachRequests };
  } finally { await detached.context.close(); }
  assert.deepEqual(report.rendererErrors, []);
  report.ok = true;
  console.log(JSON.stringify({ ok: true, pixelCases: report.matrix.length, maximumAdjacentJump: Math.max(...report.matrix.map(item => item.maximumAdjacentJump)), attached: report.attached, detached: report.detached, evidence: dir }));
} catch (error) {
  report.ok = false;
  report.failure = error.stack ?? String(error);
  throw error;
} finally {
  writeFileSync(path.join(dir, 'checks.json'), JSON.stringify(report, null, 2));
  await browser?.close();
  server?.kill();
}

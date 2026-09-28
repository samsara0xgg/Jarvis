// Built Companion acceptance, including its real character canvas and SVG stage.
// Optionally capture DUSK_JOIN_PHASE=before, then rebuild and run the default
// after phase in the same evidence directory for closed-home pixel comparisons.
// Without a baseline, the seam and renderer checks still run. Only text is hidden.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const require = createRequire(import.meta.url);
const { PNG } = require(path.join(path.dirname(require.resolve('playwright-core/package.json')), 'lib/utilsBundle.js'));
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = process.env.DUSK_JOIN_EVIDENCE_DIR ?? path.join(root, 'evidence/dusk-join');
const phase = process.env.DUSK_JOIN_PHASE ?? 'after';
const port = Number(process.env.DUSK_JOIN_PORT ?? 5209);
const url = process.env.DUSK_JOIN_URL ?? `http://127.0.0.1:${port}`;
const server = process.env.DUSK_JOIN_URL ? null : spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(port), '--strictPort'], { cwd: root, stdio: 'ignore' });
const baselinePath = path.join(dir, 'before.json');
const before = phase !== 'before' && existsSync(baselinePath) ? JSON.parse(readFileSync(baselinePath, 'utf8')) : null;
const report = { phase, cases: [], errors: [] };
const TOP = 32, offsets = [-22, -20, -18, 18, 20, 22];
mkdirSync(dir, { recursive: true });
let browser;

function samples(buffer, dpr, anchor) {
  const png = PNG.sync.read(buffer), strips = offsets.map(dx => {
    const rgba = [];
    for (let py = (TOP - 5) * dpr; py < (TOP + 3) * dpr; py++) {
      const at = (py * png.width + Math.floor((anchor + dx) * dpr)) * 4;
      rgba.push(Array.from(png.data.subarray(at, at + 4)));
    }
    return { dx, rgba };
  });
  const jumps = strips.flatMap(({ rgba }) => rgba.slice(1).map((pixel, i) => Math.max(...pixel.slice(0, 3).map((c, k) => Math.abs(c - rgba[i][k])))));
  const edge = strips.flatMap(({ rgba }) => rgba.slice(3 * dpr, 5 * dpr).flatMap(pixel => pixel.slice(0, 3)));
  return { strips, maxJump: Math.max(...jumps), maxEdge: Math.max(...edge) };
}
function difference(a, b) {
  return Math.max(...a.strips.flatMap((strip, i) => strip.rgba.slice(0, 5 * a.dpr).flatMap((rgba, j) => rgba.map((v, k) => Math.abs(v - b.strips[i].rgba[j][k])))));
}
async function runCase(dpr, notch, finish) {
  const context = await browser.newContext({ viewport: { width: 640, height: 760 }, deviceScaleFactor: dpr });
  try {
    const page = await context.newPage();
    page.on('pageerror', error => report.errors.push(error.message));
    // Fixed animation time and RNG make the unchanged closed renderer comparable
    // across the before/after builds; the actual draw functions still execute.
    await page.clock.install({ time: new Date('2026-09-27T12:00:00Z') });
    await page.clock.pauseAt(new Date('2026-09-27T12:00:01Z'));
    await page.addInitScript(({ notch, finish }) => {
      let seed = 41;
      Math.random = () => { seed = (1664525 * seed + 1013904223) >>> 0; return seed / 4294967296; };
      localStorage.setItem('companion-wardrobe-v1', JSON.stringify({ skin: 'glass', auto: false, home: 'dark', homeFinish: finish, marks: 'spark' }));
      const subscribers = new Set();
      let state = { detached: false, open: false };
      window.jarvis = {
        placement: async () => ({ docked: false, topInset: 32, notchWidth: notch, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
        onPlacement: () => () => {}, onDisplayLeave: () => () => {}, onDictation: () => () => {}, wearing: () => {}, displayReady: () => {}, companionSettings: () => {},
        onCursor: () => () => {}, onCommand: () => () => {}, passthrough: () => {}, focus: async () => {}, material: () => {},
        dashboard: async action => {
          if (action === 'state') return state;
          state = { detached: action === 'detach', open: action !== 'close' };
          subscribers.forEach(callback => callback(state)); return state;
        },
        onDashboard: callback => { subscribers.add(callback); return () => subscribers.delete(callback); },
        dashboardMessage: () => {}, onDashboardMessage: () => () => {}, dashboardDrag: () => {}, dashboardVisible: () => {}, dashboardSize: () => {},
      };
    }, { notch, finish });
    await page.goto(`${url}/?companion=1`);
    await page.addStyleTag({ content: `html,body{height:100%}body{background:#b48296!important}.dusk-content{visibility:hidden!important}
      ${notch ? `body::after{content:'';position:fixed;z-index:20;pointer-events:none;top:0;left:${(640 - notch) / 2}px;width:${notch}px;height:32px;background:#000;border-radius:0 0 10px 10px}` : ''}` });
    await page.clock.runFor(1600);
    await page.waitForFunction(finish => document.querySelector('.companion-canvas')?.dataset.homeFinish === finish, finish);
    const anchor = notch ? (640 - notch) / 2 - 32 : 320;
    const capture = async () => ({ dpr, ...samples(await page.screenshot(), dpr, anchor) });
    const closed = await capture();
    await page.locator('.companion-island-target').evaluate(el => el.click());
    await page.clock.runFor(1000);
    assert.ok(await page.locator('.companion-dashboard').evaluate(el => el.classList.contains('is-open')), 'real Companion must open its attached dashboard');
    const visible = await page.locator('.companion-canvas,.companion-stage').evaluateAll(els => els.every(el => getComputedStyle(el).visibility === 'visible' && getComputedStyle(el).opacity !== '0'));
    assert.ok(visible, 'character canvas and SVG stage must participate in the screenshot');
    const openBuffer = await page.screenshot(), attached = { dpr, ...samples(openBuffer, dpr, anchor) };
    if (dpr === 2 && notch === 0 && finish === 'refined') writeFileSync(path.join(dir, `${phase}-composite.png`), openBuffer);
    await page.locator('.companion-island-target').evaluate(el => el.click());
    await page.clock.runFor(1000);
    const closedAgain = await capture();
    await page.locator('.companion-island-target').evaluate(el => el.click()); await page.clock.runFor(1000);
    await page.keyboard.press('Meta+Shift+ArrowDown'); await page.clock.runFor(1000);
    assert.ok(await page.locator('.companion-dashboard').evaluate(el => !el.classList.contains('is-open')), 'successful detachment must remove the attached material');
    const detached = await capture();
    const result = { dpr, notch, finish, closed, attached, closedAgain, detached };
    report.cases.push(result);
    console.log(JSON.stringify({ phase, dpr, notch, finish, attachedJump: attached.maxJump, attachedEdge: attached.maxEdge, closedEdge: closed.maxEdge, detachedEdge: detached.maxEdge }));
    return result;
  } finally { await context.close(); }
}

try {
  for (let i = 0; ; i++) {
    try { if ((await fetch(url)).ok) break; } catch {}
    assert.ok(i < 50, `preview failed to start: ${url}`); await new Promise(resolve => setTimeout(resolve, 100));
  }
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  for (const dpr of [1, 2]) for (const notch of [0, 185]) for (const finish of ['original', 'refined']) await runCase(dpr, notch, finish);
  assert.deepEqual(report.errors, []);
  if (phase !== 'before') {
    for (const actual of report.cases) {
      const label = `DPR ${actual.dpr}, notch ${actual.notch}, ${actual.finish}`;
      assert.ok(actual.attached.maxJump <= 8, `${label}: attached character lower boundary has a bright discontinuity (${actual.attached.maxJump} RGB levels)`);
      assert.ok(actual.attached.maxEdge <= 8, `${label}: character rim must fade into the black attachment (${actual.attached.maxEdge})`);
      if (before) {
        const previous = before.cases.find(item => item.dpr === actual.dpr && item.notch === actual.notch && item.finish === actual.finish);
        assert.ok(previous, `${label}: baseline exists`);
        actual.closedDifference = difference(actual.closed, previous.closed);
        actual.closedAgainDifference = difference(actual.closedAgain, previous.closedAgain);
        actual.detachedDifference = difference(actual.detached, previous.detached);
        assert.ok(actual.closedDifference <= 2, `${label}: closed home changed by ${actual.closedDifference} RGB levels`);
        assert.ok(actual.closedAgainDifference <= 2, `${label}: closing did not restore the prior edge (${actual.closedAgainDifference})`);
        assert.ok(actual.detachedDifference <= 2, `${label}: detachment did not restore the prior edge (${actual.detachedDifference})`);
      }
    }
  }
  report.ok = true;
} catch (error) {
  report.ok = false; report.failure = error.stack ?? String(error); throw error;
} finally {
  writeFileSync(path.join(dir, `${phase}.json`), JSON.stringify(report, null, 2));
  await browser?.close(); server?.kill();
}

// Built Companion acceptance, including its real character canvas and SVG stage.
// DUSK_JOIN_PHASE=before records an old renderer for evidence; after validates
// every state directly. Attached panes join the home; the closed home retains its
// rim and nebula while its distant background agrees with the black wing.
// Character, content and every production material stay visible throughout the
// real UI entry paths. Animated closed pixels are not a preservation baseline.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';
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
const report = { phase, cases: [], finishComparisons: [], errors: [], unexpectedRequests: [] };
const TOP = 32, offsets = [-22, -20, -18, 18, 20, 22];
const fixturePort = '61986', fixtureOrigin = `http://127.0.0.1:${fixturePort}`;
const started = performance.now();
mkdirSync(dir, { recursive: true });
let browser;

function samples(buffer, dpr, anchor, notch) {
  const png = PNG.sync.read(buffer), strips = offsets.map(dx => {
    const rgba = [];
    for (let py = (TOP - 5) * dpr; py < (TOP + 3) * dpr; py++) {
      const at = (py * png.width + Math.floor((anchor + dx) * dpr)) * 4;
      rgba.push(Array.from(png.data.subarray(at, at + 4)));
    }
    return { dx, rgba };
  });
  const jumpsIn = rows => rows.slice(1).map((pixel, i) => Math.max(...pixel.slice(0, 3).map((c, k) => Math.abs(c - rows[i][k]))));
  const jumps = strips.flatMap(({ rgba }) => jumpsIn(rgba));
  // Test the seam itself (y30..32), excluding moving stars below the boundary.
  const seamJumps = strips.flatMap(({ rgba }) => jumpsIn(rgba.slice(3 * dpr, 6 * dpr)));
  // Closed samples end inside the island: the desktop immediately below its
  // silhouette has an intentional contrast that is unrelated to the old rim.
  const interiorJumps = strips.flatMap(({ rgba }) => jumpsIn(rgba.slice(0, 5 * dpr)));
  const edge = strips.flatMap(({ rgba }) => rgba.slice(3 * dpr, 5 * dpr).flatMap(pixel => pixel.slice(0, 3)));
  // These upper-body pixels avoid the eyes and bottom mask. Both finishes must
  // retain visible material, and their diffuse/elliptical treatments must differ.
  const body = [-24, -20, 20, 24].flatMap(dx => [6, 9, 12, 15].map(y => {
    const at = (Math.floor(y * dpr) * png.width + Math.floor((anchor + dx) * dpr)) * 4;
    return { dx, y, rgb: Array.from(png.data.subarray(at, at + 3)) };
  }));
  // With a notch the left pocket is only 64px wide. Its left+12px is inside
  // the nebula, so use the outermost interior instead. The right samples lie
  // under the simulated hardware; the unobstructed left samples are the guard.
  const background = (notch ? [-30, 44] : [-54, 54]).flatMap(dx => (notch ? [4] : [4, 8, 12]).map(y => {
    const at = (Math.floor(y * dpr) * png.width + Math.floor((anchor + dx) * dpr)) * 4;
    return { dx, y, rgb: Array.from(png.data.subarray(at, at + 3)) };
  }));
  const wingX = notch ? (640 + notch) / 2 + 3 : anchor + 69;
  const wingAt = (8 * dpr * png.width + Math.floor(wingX * dpr)) * 4;
  const wing = Array.from(png.data.subarray(wingAt, wingAt + 3));
  const backgroundDifference = Math.max(...background.flatMap(point => point.rgb.map((value, k) => Math.abs(value - wing[k]))));
  return { strips, body, background, wing, backgroundDifference, maxJump: Math.max(...jumps), seamMaxJump: Math.max(...seamJumps), interiorMaxJump: Math.max(...interiorJumps), maxEdge: Math.max(...edge) };
}
async function runCase(dpr, notch, finish) {
  const context = await browser.newContext({ viewport: { width: 640, height: 760 }, deviceScaleFactor: dpr });
  try {
    const page = await context.newPage();
    page.on('pageerror', error => report.errors.push(error.message));
    const common = { kind: 'background', project: 'acceptance', branch: 'join-check', where: 'background', prompt: 'Check the attached surface', activity: 'Checking the join', last_message: 'The check is complete', updated_ms: Date.parse('2026-09-27T12:00:00Z'), replyable: true };
    let sessions = [{ ...common, session_id: 'join-work', phase: 'working', title: 'Working join fixture' }, { ...common, session_id: 'join-done', phase: 'done', title: 'Finished join fixture' }];
    await page.route(`${fixtureOrigin}/**`, async route => {
      const req = route.request(), pathname = new URL(req.url()).pathname;
      let data, status = 200;
      if (pathname === '/inherent/claude-sessions') data = { sessions };
      else if (pathname === '/inherent/codex-sessions') data = { sessions: [] };
      else if (pathname === '/inherent/agent-marks') data = { marks: {} };
      else if (pathname.startsWith('/inherent/agent-marks/')) data = {};
      else if (pathname.endsWith('/conversation') && pathname.includes('/claude-sessions/')) data = { messages: [{ who: 'you', text: 'Keep the character visible while checking the surface join.' }, { who: 'it', text: 'This conversation is a local acceptance fixture.' }] };
      else if (pathname === '/inherent/conversation') data = { rows: [] };
      else if (['/inherent/confirmation', '/inherent/clarification'].includes(pathname)) data = { card: null };
      else if (pathname === '/inherent/controls') data = { mic_muted: false, speech_muted: false, conversation: false };
      else if (pathname === '/inherent/language') data = { language: 'en' };
      else if (pathname === '/inherent/think') data = { on: false, on_words: 'think deeply', off_words: 'stop thinking' };
      else { status = 404; data = {}; }
      if (req.method() === 'POST' && !['/inherent/controls', '/inherent/projects/refresh'].includes(pathname) && !pathname.startsWith('/inherent/agent-marks/')) report.unexpectedRequests.push(pathname);
      await route.fulfill({ status, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' }, body: JSON.stringify(data) });
    });
    await page.routeWebSocket(`ws://127.0.0.1:${fixturePort}/**`, () => {});
    // Fixed animation time and RNG make the two finishes comparable while the
    // actual draw functions, character gaze and daemon-driven states execute.
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
        onCursor: callback => { window.__joinCursor = callback; return () => {}; }, onCommand: callback => { window.__joinCommand = callback; return () => {}; }, passthrough: () => {}, focus: async () => {}, material: () => {}, codexTitles: async () => ({}),
        dashboard: async action => {
          if (action === 'state') return state;
          state = { detached: action === 'detach', open: action !== 'close' };
          subscribers.forEach(callback => callback(state)); return state;
        },
        onDashboard: callback => { subscribers.add(callback); return () => subscribers.delete(callback); },
        dashboardMessage: () => {}, onDashboardMessage: () => () => {}, dashboardDrag: () => {}, dashboardVisible: () => {}, dashboardSize: () => {},
      };
    }, { notch, finish });
    await page.goto(`${url}/?companion=1&port=${fixturePort}`);
    await page.addStyleTag({ content: `html,body{height:100%}body{background:#b48296!important}
      ${notch ? `body::after{content:'';position:fixed;z-index:20;pointer-events:none;top:0;left:${(640 - notch) / 2}px;width:${notch}px;height:32px;background:#000;border-radius:0 0 10px 10px}` : ''}` });
    await page.clock.runFor(1600);
    await page.waitForFunction(finish => document.querySelector('.companion-canvas')?.dataset.homeFinish === finish, finish);
    const anchor = notch ? (640 - notch) / 2 - 32 : 320;
    const capture = async () => ({ dpr, ...samples(await page.screenshot(), dpr, anchor, notch) });
    const advance = async ms => { await page.clock.runFor(ms); await page.waitForTimeout(20); };
    const cursor = async (x, y) => { await page.evaluate(point => window.__joinCursor(point), { x, y }); await page.mouse.move(x, y); };
    const panelsClosed = () => page.evaluate(() => !document.querySelector('.notch-drop').classList.contains('is-open') && !document.querySelector('.notch-note').classList.contains('is-open'));
    const waitPane = async (selector, message) => {
      for (let i = 0; i < 4; i++) { await advance(800); if (await page.locator(selector).count()) return; }
      throw new Error(`${message}: ${await page.locator('.notch').evaluate(el => JSON.stringify({ marks: el.dataset.marks, content: el.innerText }))}`);
    };
    const closedBuffer = await page.screenshot(), closed = { dpr, ...samples(closedBuffer, dpr, anchor, notch) };
    if (dpr === 2 && notch === 0 && finish === 'refined') writeFileSync(path.join(dir, `${phase}-closed.png`), closedBuffer);
    await page.locator('.companion-island-target').evaluate(el => el.click());
    await page.clock.runFor(1000);
    assert.ok(await page.locator('.companion-dashboard').evaluate(el => el.classList.contains('is-open')), 'real Companion must open its attached dashboard');
    const visible = await page.locator('.companion-canvas,.companion-stage').evaluateAll(els => els.every(el => getComputedStyle(el).visibility === 'visible' && getComputedStyle(el).opacity !== '0'));
    assert.ok(visible, 'character canvas and SVG stage must participate in the screenshot');
    const openBuffer = await page.screenshot(), attached = { dpr, ...samples(openBuffer, dpr, anchor, notch) };
    if (dpr === 2 && notch === 0 && finish === 'refined') writeFileSync(path.join(dir, `${phase}-composite.png`), openBuffer);
    await page.locator('.companion-island-target').evaluate(el => el.click());
    await page.clock.runFor(1000);
    const closedAgain = await capture();
    await page.locator('.companion-island-target').evaluate(el => el.click()); await page.clock.runFor(1000);
    await page.keyboard.press('Meta+Shift+ArrowDown'); await page.clock.runFor(1000);
    assert.ok(await page.locator('.companion-dashboard').evaluate(el => !el.classList.contains('is-open')), 'successful detachment must remove the attached material');
    const detached = await capture();
    await page.evaluate(() => window.jarvis.dashboard('close'));
    await cursor(620, 700); await advance(1000);
    await page.waitForFunction(() => document.querySelector('.notch')?.dataset.marks.includes('work1 done1'));
    const notchSurfaces = {}, notchClosed = {}, transitions = {};
    const checkTransitions = phase !== 'before' && dpr === 2 && notch === 0 && finish === 'refined';
    const takeSurface = async name => {
      const buffer = await page.screenshot(); notchSurfaces[name] = { dpr, ...samples(buffer, dpr, anchor, notch) };
      // One representative per actual entry, while all eight combinations keep
      // their metrics. Nothing in the screenshot is hidden or replaced.
      if (dpr === 2 && notch === 0 && finish === 'refined') writeFileSync(path.join(dir, `${phase}-${name}.png`), buffer);
    };
    const closeSurface = async name => {
      await cursor(620, 700); await page.keyboard.press('Escape');
      if (checkTransitions && name === 'working') {
        await advance(320);
        const pane = await page.locator('.notch-drop').evaluate(el => ({ open: el.classList.contains('is-open'), height: el.getBoundingClientRect().height, visible: getComputedStyle(el).visibility }));
        assert.ok(!pane.open && pane.height > 12 && pane.visible === 'visible', `closing frame must retain an actual visible pane: ${JSON.stringify(pane)}`);
        transitions.closing = { ...(await capture()), pane };
        await advance(880);
      } else await advance(1200);
      assert.ok(await panelsClosed(), `${name}: closing must finish before its home sample`);
      notchClosed[name] = await capture();
    };
    const hoverGroup = async group => {
      const point = await page.locator('.notch-hit').evaluate((el, group) => {
        const groups = document.querySelector('.notch').dataset.marks.split(' ').map(mark => mark.replace(/\d+$/, ''));
        const index = groups.indexOf(group); if (index < 0) throw new Error(`Missing ${group} mark`);
        return { x: el.getBoundingClientRect().left + 4 + (4 - groups.length) * 13 + index * 26 + 6, y: 16 };
      }, group);
      await cursor(point.x, point.y); await waitPane(`.notch-drop.is-open [data-sec="${group}"].is-hot`, `${group} hover`);
    };
    await hoverGroup('work'); await takeSurface('working'); await closeSurface('working');
    await hoverGroup('done'); await takeSurface('finished'); await closeSurface('finished');
    await hoverGroup('work');
    await page.locator('.a-row[data-id="join-work"] [aria-label="What it said · reply"]').evaluate(el => el.click());
    if (checkTransitions) { await advance(80); transitions.listToPage = await capture(); }
    await waitPane('.notch-drop.is-open .r-head', 'session page');
    assert.ok((await page.locator('.notch-drop').innerText()).includes('local acceptance fixture'), 'the real session page must load its conversation');
    await takeSurface('session');
    if (checkTransitions) {
      await page.locator('.notch-drop .r-back').evaluate(el => el.click());
      await advance(80); transitions.pageToList = await capture();
      assert.ok(await page.locator('.notch-drop.is-open .a-row.is-cur').count(), 'Back must retain the keyboard list');
    }
    await closeSurface('session');
    await page.evaluate(() => window.__joinCommand('agent-keys'));
    await waitPane('.notch-drop.is-open .a-row.is-cur', 'keyboard list');
    await takeSurface('keyboard'); await closeSurface('keyboard');
    sessions = sessions.map(session => session.session_id === 'join-work' ? { ...session, phase: 'done' } : session);
    await advance(1800); await waitPane('.notch-note.is-open .c-x', 'completion notification');
    await takeSurface('notification');
    await page.locator('.notch-note .c-x').evaluate(el => el.click());
    await advance(1200); assert.ok(await panelsClosed(), 'notification Close must restore the home');
    notchClosed.notification = await capture();
    sessions = sessions.map(session => session.session_id === 'join-work' ? { ...session, phase: 'needs_input', request: { id: 'join-approval', tool: 'Bash', input: { command: 'echo acceptance-only', description: 'A local fixture; no command runs' }, cwd: '/tmp/acceptance', always: '' } } : session);
    await advance(1800); await waitPane('.notch-note.is-open .nc-choice', 'approval note');
    await takeSurface('approval');
    await page.locator('.notch-note button[aria-label="Park"]').evaluate(el => el.click());
    await advance(1200); assert.ok(await panelsClosed(), 'Park must close the approval without deciding it');
    notchClosed.approval = await capture();
    const result = { dpr, notch, finish, closed, attached, closedAgain, detached, notchSurfaces, notchClosed, transitions };
    report.cases.push(result);
    console.log(JSON.stringify({ phase, dpr, notch, finish, attachedJump: attached.seamMaxJump, attachedEdge: attached.maxEdge, closedBackgroundDifference: closed.backgroundDifference, closedRim: closed.maxEdge, notchSurfaces: Object.fromEntries(Object.entries(notchSurfaces).map(([name, value]) => [name, { jump: value.seamMaxJump, edge: value.maxEdge }])) }));
    return result;
  } finally { await context.close(); }
}

try {
  for (let i = 0; ; i++) {
    try { if ((await fetch(url)).ok) break; } catch {}
    assert.ok(i < 50, `preview failed to start: ${url}`); await new Promise(resolve => setTimeout(resolve, 100));
  }
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  // Emit the disputed refined/no-notch closed background first for review.
  await runCase(2, 0, 'refined');
  for (const dpr of [1, 2]) for (const notch of [0, 185]) for (const finish of ['original', 'refined']) {
    if (dpr === 2 && notch === 0 && finish === 'refined') continue;
    await runCase(dpr, notch, finish);
  }
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.unexpectedRequests, [], 'surface navigation must not submit, reply, or decide an approval');
  if (phase !== 'before') {
    for (const actual of report.cases) {
      const label = `DPR ${actual.dpr}, notch ${actual.notch}, ${actual.finish}`;
      assert.ok(actual.attached.seamMaxJump <= 8, `${label}: attached character lower boundary has a bright discontinuity (${actual.attached.seamMaxJump} RGB levels)`);
      assert.ok(actual.attached.maxEdge <= 8, `${label}: character rim must fade into the black attachment (${actual.attached.maxEdge})`);
      for (const [name, sample] of Object.entries(actual.notchSurfaces)) {
        assert.ok(sample.seamMaxJump <= 8, `${label}, ${name}: character lower boundary has a bright discontinuity (${sample.seamMaxJump})`);
        assert.ok(sample.maxEdge <= 8, `${label}, ${name}: the character rim must join the expanded pane (${sample.maxEdge})`);
      }
      for (const [name, sample] of Object.entries(actual.transitions)) {
        assert.ok(sample.seamMaxJump <= 8 && sample.maxEdge <= 8, `${label}, ${name}: the moving pane exposes a bright home rim (${sample.seamMaxJump}/${sample.maxEdge})`);
      }
      for (const [name, sample] of Object.entries({ closed: actual.closed, closedAgain: actual.closedAgain, detached: actual.detached, ...actual.notchClosed })) {
        assert.ok(sample.backgroundDifference <= 4, `${label}, ${name}: the distant home background differs from its black wing (${sample.backgroundDifference})`);
        assert.ok(sample.maxEdge >= 12, `${label}, ${name}: the requested closed-home rim was erased (${sample.maxEdge})`);
      }
      actual.bodyPeak = Math.max(...actual.closed.body.flatMap(point => point.rgb));
      assert.ok(actual.bodyPeak >= 5, `${label}: the home material was erased instead of blended (${actual.bodyPeak})`);
    }
    for (const dpr of [1, 2]) for (const notch of [0, 185]) {
      const original = report.cases.find(item => item.dpr === dpr && item.notch === notch && item.finish === 'original').closed.body;
      const refined = report.cases.find(item => item.dpr === dpr && item.notch === notch && item.finish === 'refined').closed.body;
      const differences = original.flatMap((point, i) => point.rgb.map((value, k) => Math.abs(value - refined[i].rgb[k])));
      const comparison = { dpr, notch, maxDifference: Math.max(...differences), meanDifference: differences.reduce((a, b) => a + b, 0) / differences.length };
      report.finishComparisons.push(comparison);
      assert.ok(comparison.maxDifference >= 3 && comparison.meanDifference >= .3, `DPR ${dpr}, notch ${notch}: original and refined must retain distinct visible materials (${JSON.stringify(comparison)})`);
    }
  }
  report.ok = true;
} catch (error) {
  report.ok = false; report.failure = error.stack ?? String(error); throw error;
} finally {
  report.elapsedSeconds = Number(((performance.now() - started) / 1000).toFixed(2));
  writeFileSync(path.join(dir, `${phase}.json`), JSON.stringify(report, null, 2));
  await browser?.close(); server?.kill();
}

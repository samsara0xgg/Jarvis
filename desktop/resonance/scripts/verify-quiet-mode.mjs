// ADR 0153, in headless Chrome against the built page: the quiet level is the daemon's. A fake daemon answers
// `controls`; the page shows what it says (mark on the island, her menu) and its notice cues stay silent in
// `quiet` while the card still shows. Silent: no desktop window, no audio. Run after `npm run build`.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = path.join(root, 'evidence', 'quiet-mode');
mkdirSync(dir, { recursive: true });
const daemonPort = 8798, web = Number(process.env.COMPANION_PORT ?? 5193), daemon = `http://127.0.0.1:${daemonPort}`;
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security', '--mute-audio'] });
const check = (name, pass) => { assert.ok(pass, name); console.log(`PASS ${name}`); };
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const page = await (await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 })).newPage();
  const errors = [], posts = [];
  page.on('pageerror', error => errors.push(error.message));
  let controls = { mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' };
  const session = phase => ({ agent: 'claude', session_id: 'c-1', kind: 'interactive', phase, title: 'Wire the companion', project: 'jarvis', branch: 'main', cwd: '/x', where: 'Ghostty', prompt: 'wire it', activity: 'Wants to run npm test', last_message: 'Needs your decision', started_ms: Date.now() - 60_000, updated_ms: Date.now(), request: null, compacting: false, error: '' });
  let board = [session('working')];
  await page.addInitScript(() => {
    window.__audio = 0;
    const Real = window.AudioContext;
    window.AudioContext = class extends Real { constructor(...a) { super(...a); window.__audio++; } };
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, onGhostty: () => () => {}, plugins: async () => ({}),
    };
    window.__sockets = [];
    window.WebSocket = class { constructor() { window.__sockets.push(this); setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
    window.__emit = (op, payload) => window.__sockets.at(-1).onmessage({ data: JSON.stringify({ op, payload }) });
  });
  await page.route(`${daemon}/**`, route => {
    const url = new URL(route.request().url()), method = route.request().method();
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    if (url.pathname === '/inherent/controls') {
      const body = JSON.parse(route.request().postData() || '{}');
      if (method === 'POST' && body.quiet) posts.push(body.quiet);
      controls = { ...controls, ...body }; return json(controls);
    }
    if (url.pathname === '/inherent/claude-sessions') return json({ sessions: board, error: null });
    if (url.pathname === '/inherent/agent-marks') return json({ marks: {} });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });
  await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${daemonPort}`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
  await page.waitForTimeout(800);
  const shot = async (name, clip = { x: 0, y: 0, width: 640, height: 560 }) => { await page.waitForTimeout(1000); await page.screenshot({ path: path.join(dir, `${name}.png`), clip }); };
  const mark = () => page.locator('.companion-quiet').count();
  const menu = async () => { await page.locator('.companion-hit').first().dispatchEvent('contextmenu', { clientX: 300, clientY: 90, bubbles: true, cancelable: true }); await page.waitForTimeout(250); };
  const card = () => page.locator('.notch-note .nc').count();

  check('normal: no mark on the island', await mark() === 0);
  // The notice for a session that needs him comes with its cue, while the level is off.
  board = [session('needs_input')];
  await page.waitForFunction(() => document.querySelector('.notch-note .nc'), null, { timeout: 6000 });
  check('normal: the card shows', await card() === 1);
  check('normal: its cue sounds', await page.evaluate(() => window.__audio) >= 1);
  await shot('normal-card');

  // Her menu sets it on the daemon; the page then shows what the daemon answered.
  await menu();
  check('menu lists Quiet beside Normal', await page.locator('.companion-menu [role=menuitemradio]').evaluateAll(els => els.map(e => e.textContent).filter(t => /^(Normal|Quiet)/.test(t)).length) === 2);
  await shot('menu');
  await page.locator('.companion-menu button', { hasText: /^Quiet/ }).click();
  await page.waitForFunction(() => document.querySelector('.companion-quiet'), null, { timeout: 3000 });
  check('menu: the daemon was asked for quiet', posts.at(-1) === 'quiet');
  check('quiet: the mark shows on the island', await mark() === 1);
  console.log(await page.evaluate(() => { const e = document.querySelector('.companion-quiet'), r = e.getBoundingClientRect(), c = getComputedStyle(e); return JSON.stringify({ r, color: c.color, z: c.zIndex, op: c.opacity, under: document.elementFromPoint(r.x + 6, r.y + 6)?.className }); }));
  await shot('quiet-mark');
  console.log(await page.evaluate(() => { const e = document.querySelector('.companion-quiet'); const s = e.querySelector('svg'); return JSON.stringify({ z: getComputedStyle(e).zIndex, svg: s?.outerHTML.slice(0, 160), vis: getComputedStyle(e).visibility, fill: getComputedStyle(s).fill }); }));
  await page.screenshot({ path: path.join(dir, 'quiet-mark-zoom.png'), clip: { x: 160, y: 0, width: 48, height: 32 } });

  // A new notice in quiet: the card still shows, with no sound.
  const before = await page.evaluate(() => window.__audio);
  board = [session('working')]; await page.waitForTimeout(1800);
  board = [{ ...session('needs_input'), session_id: 'c-2', title: 'Second session' }]; 
  await page.waitForFunction(() => document.querySelector('.notch-note .nc'), null, { timeout: 6000 });
  await page.waitForTimeout(500);
  check('quiet: the card still shows', await card() === 1);
  check('quiet: no cue sounded', await page.evaluate(() => window.__audio) === before);
  await shot('quiet-card');

  // The level is the daemon's: a push from it (the voice phrase's path) changes the page with no click.
  await page.evaluate(() => window.__emit('controls', { mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' }));
  await page.waitForFunction(() => !document.querySelector('.companion-quiet'), null, { timeout: 3000 });
  check('a daemon push to off clears the mark', await mark() === 0);
  await page.evaluate(() => window.__emit('controls', { mic_muted: false, speech_muted: false, conversation: false, quiet: 'dnd' }));
  await page.waitForFunction(() => document.querySelector('.companion-quiet'), null, { timeout: 3000 });
  check('a daemon push to dnd shows a mark', await mark() === 1);
  check('no page errors', errors.length === 0);
  if (errors.length) console.log(errors);
} finally { await browser.close(); server.kill(); }

// ADR 0153 notch rules, in headless Chrome against the built page: a project thread's session never joins the queue,
// nothing shows while he is in Claude, a card is put away by Esc, the × , a swipe or a press elsewhere (no moon, no
// return), and one ask is one card. Silent: no desktop window, no audio. Run after `npm run build`.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = path.join(root, 'evidence', 'quiet-notch');
mkdirSync(dir, { recursive: true });
const daemonPort = 8799, web = Number(process.env.COMPANION_PORT ?? 5194), daemon = `http://127.0.0.1:${daemonPort}`;
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security', '--mute-audio'] });
const check = (name, pass) => { assert.ok(pass, name); console.log(`PASS ${name}`); };
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const page = await (await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 })).newPage();
  const errors = [], posts = [];
  page.on('pageerror', error => errors.push(error.message));
  let controls = { mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' };
  const session = phase => ({ agent: 'claude', session_id: 'c-1', kind: 'interactive', phase, title: 'Wire the companion', project: 'jarvis', branch: 'main', cwd: '/x', where: 'Ghostty', prompt: 'wire it', activity: 'Wants to run npm test', last_message: 'Needs your decision', started_ms: Date.now() - 60_000, updated_ms: Date.now(), request: null, from_project: false, compacting: false, error: '' });
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
      watchGhostty: () => {}, onGhostty: () => () => {}, onMouseDown: cb => { window.__down = cb; return () => {}; }, onClaudeFront: cb => { window.__front = cb; return () => {}; }, plugins: async () => ({}),
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
  const card = () => page.locator('.notch-note.is-open .nc').count();
  const gone = () => page.waitForFunction(() => !document.querySelector('.notch-note.is-open .nc'), null, { timeout: 3000 });
  const shows = () => page.waitForFunction(() => document.querySelector('.notch-note.is-open .nc'), null, { timeout: 6000 });
  const ask = (id, extra = {}) => ({ ...session('needs_input'), session_id: id, title: `Session ${id}`, ...extra });
  const req = (id, command, rid) => ({ ...session('needs_input'), session_id: id, title: `Session ${id}`, request: { id: rid, tool: 'Bash', input: { command }, cwd: '/x', always: '' } });
  const working = id => ({ ...session('working'), session_id: id, title: `Session ${id}` });
  // A session is first seen working, then asks: the way a real one arrives.
  const arrive = async (...rows) => { board = rows.map(r => working(r.session_id)); await page.waitForTimeout(1800); board = rows; };

  // (a) a session a project thread started in ~/Projects never joins his queue
  await arrive(ask('p-1', { from_project: true }));
  await page.waitForTimeout(2500);
  check('a project thread session shows no card', await card() === 0);
  board = [];

  // (c) Esc puts a card away without parking it
  await arrive(req('c-1', 'npm test', 'r-1'));
  await shows();
  await page.mouse.move(5, 600);
  await page.waitForTimeout(900);
  await shot('card');
  await page.keyboard.press('Escape');
  await gone();
  check('Esc puts the card away', await card() === 0);
  // The same ask under a new request id (same text) does not make a card again; a different one does.
  board = [req('c-1', 'npm test', 'r-2')];
  await page.waitForTimeout(2500);
  check('the same ask after a dismiss makes no card', await card() === 0);
  board = [working('c-1')]; await page.waitForTimeout(1800);
  board = [req('c-1', 'rm -rf build', 'r-3')];
  await shows();
  check('a different ask comes up as a card', /rm -rf build/.test(await page.locator('.notch-note.is-open .nc').innerText()));
  // The × button
  await page.locator('.notch-note.is-open .nc-dismiss').click();
  await gone();
  check('the × puts the card away', await card() === 0);

  // (c) a press elsewhere on screen
  board = [working('c-2')]; await page.waitForTimeout(1800);
  board = [req('c-2', 'ls', 'r-4')];
  await shows();
  await page.mouse.move(5, 600);
  await page.evaluate(() => window.__down());
  await page.waitForTimeout(300);
  check('a press within 0.8 s of its coming up leaves it', await card() === 1);
  await page.waitForTimeout(700);
  await page.evaluate(() => window.__down());
  await gone();
  check('a press elsewhere puts the card away', await card() === 0);

  // (c) a sideways swipe
  board = [working('c-3')]; await page.waitForTimeout(1800);
  board = [req('c-3', 'pwd', 'r-5')];
  await shows();
  const box = await page.locator('.notch-note.is-open .nc').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, 60);
  await page.waitForTimeout(300);
  check('a vertical scroll keeps the card', await card() === 1);
  await page.mouse.wheel(120, 0); await page.mouse.wheel(120, 0);
  await gone();
  check('a sideways swipe puts the card away', await card() === 0);

  // (d) one ask is one card: the same ask again while its card is up adds nothing
  board = [working('c-4')]; await page.waitForTimeout(1800);
  board = [ask('c-4')];
  await shows();
  board = [working('c-4')]; await page.waitForTimeout(1800);
  board = [ask('c-4')]; await page.waitForTimeout(2500);
  check('the same ask twice is one card', !/1 of/.test(await page.locator('.notch-note.is-open .nc-label').innerText()));
  await page.keyboard.press('Escape');
  await gone();

  // (b) in Claude: no card, no sound; leaving brings back only a held prompt
  await page.evaluate(() => window.__front(true));
  board = [working('c-5'), working('c-6')]; await page.waitForTimeout(1800);
  board = [ask('c-5'), req('c-6', 'make', 'r-6')];
  await page.waitForTimeout(2500);
  check('in Claude: no card shows', await card() === 0);
  await shot('in-claude');
  await page.evaluate(() => window.__front(false));
  await shows();
  const text = await page.locator('.notch-note.is-open .nc').innerText();
  check('leaving Claude: the held prompt comes up', /make/.test(text));
  await page.waitForTimeout(900);
  await page.keyboard.press('Escape');
  await gone();
  check('and the ask line that came in meanwhile did not make a card', await card() === 0);
  check('no page errors', errors.length === 0);
  if (errors.length) console.log(errors);
} finally { await browser.close(); server.kill(); }

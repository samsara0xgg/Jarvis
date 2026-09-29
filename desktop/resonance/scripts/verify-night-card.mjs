// ADR 0093: the night run's cards on the companion, against a fake daemon. Run after `npm run build`.
// Her menu starts a run; the bedtime card counts down and can go dark now or cancel; the night card is dim and ends
// the run; the morning card tells the night and goes with its ×, for good. She sleeps in the island through the night.
// CHROMIUM_PATH runs it on a Chromium where Chrome is not installed.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = path.join(root, 'evidence', 'night-card');
mkdirSync(dir, { recursive: true });
const port = '8799', daemon = `http://127.0.0.1:${port}`, web = Number(process.env.NIGHT_CARD_PORT ?? 5196);
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const checks = [], check = (name, pass) => { assert.ok(pass, name); checks.push(name); console.log(`PASS ${name}`); };
const exe = process.env.CHROMIUM_PATH;
const browser = await chromium.launch({ headless: true, ...exe ? { executablePath: exe } : { channel: 'chrome' }, args: ['--disable-web-security'] });
const hm = ms => { const d = new Date(ms); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const context = await browser.newContext({ viewport: { width: 640, height: 722 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));

  // The fake daemon's run: started at bedtime, the screen off 8 s later, awake two hours, the morning at 06:00.
  const H = 3_600_000, posts = [];
  const bed = new Date(); bed.setDate(bed.getDate() - 1); bed.setHours(23, 0, 0, 0);
  const six = new Date(); six.setHours(6, 0, 0, 0); if (six.getTime() <= Date.now()) six.setDate(six.getDate() + 1);
  let state = { night: null, last: null, hours: 2, laptop: true }, runs = 0;
  const act = body => {
    const run = state.night;
    if (body.action === 'start' && !run) {
      const at = Date.now();
      state = { ...state, night: { id: `n${++runs}`, phase: 'starting', started_ms: at, until_ms: at + 2 * H, wake_at_ms: six.getTime(), dark_at_ms: at + 8000, released_ms: null, guarded: true } };
    } else if (body.action === 'dark' && run?.phase === 'starting') state = { ...state, night: { ...run, phase: 'dark', dark_at_ms: Date.now() } };
    else if (body.action === 'end' && run) {
      // The morning card reads last night's times: bedtime yesterday, let go two hours later, up just now.
      const cancelled = run.phase === 'starting', started = cancelled ? run.started_ms : bed.getTime();
      state = { ...state, night: null, last: { id: run.id, started_ms: started, until_ms: started + 2 * H, released_ms: cancelled ? null : started + 2 * H, ended_ms: Date.now(),
        reason: cancelled ? 'cancelled' : 'ended', slept_ms: null, restored: { brightness: !cancelled, volume: !cancelled } } };
    }
    return state;
  };
  await page.addInitScript(() => {
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {},
    };
    // A WebSocket that never leaves the page.
    window.WebSocket = class { constructor(url) { this.url = url; setTimeout(() => this.onopen?.(), 0); } send() {} close() { this.onclose?.(); } };
  });
  await page.route(`${daemon}/**`, async route => {
    const url = new URL(route.request().url()), method = route.request().method();
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    if (url.pathname === '/inherent/night') {
      if (method !== 'POST') return json(state);
      const body = JSON.parse(route.request().postData() || '{}');
      posts.push(body);
      return json(act(body));
    }
    if (url.pathname === '/inherent/language') return json({ language: 'zh' });
    if (url.pathname === '/inherent/controls') return json({ mic_muted: false, speech_muted: false, conversation: false });
    if (url.pathname === '/inherent/confirmation' || url.pathname === '/inherent/clarification') return json({ card: null });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });

  await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${port}`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
  await page.evaluate(() => window.__cursor({ x: 600, y: 560 }));
  await page.waitForTimeout(1800);
  const card = page.locator('.notch-note .nc-night');
  const face = () => page.evaluate(() => document.querySelector('.companion-canvas')?.dataset.face);
  const shot = name => page.screenshot({ path: path.join(dir, `${name}.png`), clip: { x: 100, y: 0, width: 440, height: 260 } });
  const shown = async () => { await card.waitFor({ state: 'visible', timeout: 5000 }); await page.waitForFunction(() => document.querySelector('.notch-note')?.classList.contains('is-open')); await page.waitForTimeout(700); };
  check('no run, no card', await card.count() === 0);

  await page.locator('.companion-hit').click({ button: 'right', force: true });
  await page.getByRole('menuitem', { name: '睡了，挂 2 小时' }).click();
  await shown();
  check('her menu starts the default run', posts.length === 1 && posts[0].action === 'start' && posts[0].hours === undefined);
  const until = hm(state.night.until_ms);
  const bedtime = await card.textContent();
  check(`the bedtime card says until when, counts down and warns about the lid (${bedtime})`,
    bedtime.includes(`挂到 ${until}`) && /\d+ 秒后熄屏/.test(bedtime) && bedtime.includes('起来时调回') && bedtime.includes('06:00 前醒来只看一眼') && bedtime.includes('合上盖子'));
  check('she watches the bedtime card from the island', await face() === 'ask');
  await shot('01-bedtime');

  await card.getByRole('button', { name: '现在熄屏' }).click();
  await page.waitForFunction(() => document.querySelector('.notch-note .nc-night')?.classList.contains('is-dim'));
  await page.waitForTimeout(700);
  const night = await card.textContent();
  check('the screen goes now: the card turns dim and says the run goes on', posts.at(-1).action === 'dark' && night.includes('还在挂着') && night.includes(`醒到 ${until}`));
  check('she sleeps in the island', await face() === '00');
  await shot('02-night');
  state = { ...state, night: { ...state.night, released_ms: state.night.until_ms } };
  await page.waitForFunction(text => document.querySelector('.notch-note .nc-night')?.textContent.includes(text), `${until} 已放开防睡`, { timeout: 4000 });
  check('after the deadline the card says the hold is gone', true);
  await shot('03-released');

  await card.getByRole('button', { name: '我起来了' }).click();
  await page.waitForFunction(() => document.querySelector('.notch-note .nc-night')?.classList.contains('is-morning'));
  await page.waitForTimeout(700);
  const morning = await card.textContent();
  check(`the morning card tells last night (${morning})`, posts.at(-1).action === 'end' && morning.includes('昨晚') && morning.includes('23:00 – 01:00')
    && morning.includes('一直醒着') && morning.includes('亮度和声音'));
  check('she is pleased to see you', await face() === 'fin');
  await shot('04-morning');
  await card.getByRole('button', { name: '知道了' }).click();
  await page.waitForTimeout(900);
  const folded = () => page.evaluate(() => !document.querySelector('.notch-note')?.classList.contains('is-open'));
  check('its × puts the morning card away', await folded() && await page.evaluate(() => localStorage.getItem('companion-night-seen-v1')) === 'n1');
  await page.reload();
  await page.evaluate(() => window.__cursor({ x: 600, y: 560 }));
  await page.waitForTimeout(2200);
  check('and it stays away after a reload', await card.count() === 0);

  await page.locator('.companion-hit').click({ button: 'right', force: true });
  await page.getByRole('menuitem', { name: '睡了，挂 2 小时' }).click();
  await shown();
  await card.getByRole('button', { name: '不挂了' }).click();
  await page.waitForTimeout(900);
  check('cancelled before dark: nothing to tell in the morning', posts.at(-1).action === 'end' && state.last.reason === 'cancelled' && await folded());
  check('no page errors', errors.length === 0);
  writeFileSync(path.join(dir, 'checks.json'), JSON.stringify({ checks, errors }, null, 2));
  console.log(`${checks.length} night card checks passed`);
} finally {
  await browser.close();
  server.kill();
}

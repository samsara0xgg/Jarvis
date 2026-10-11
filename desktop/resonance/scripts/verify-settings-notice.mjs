// The settings notice (ADR 0227), the client's half, in headless Chrome against the built page and a fake daemon (route mocking):
// GET /inherent/notices serves `kind: "settings"` with two choices; the card shows its title, line and both buttons; a choice posts
// { action: 'feedback', reaction: <choice id> } and the card says what happens next (restart when idle, restart by hand, kept);
// the × and Esc send dismissed; the card stays up untouched (no auto-fold, a press elsewhere does not close it); a notice with a changed
// set of keys comes back, an unchanged one does not. Silent: no desktop window, no audio. Run after `npm run build`.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = path.join(root, 'evidence', 'settings-notice');
mkdirSync(dir, { recursive: true });
const daemonPort = Number(process.env.DAEMON_PORT ?? 8798), web = Number(process.env.COMPANION_PORT ?? 5196), daemon = `http://127.0.0.1:${daemonPort}`;
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security', '--mute-audio'] });
let passed = 0;
const check = (name, pass) => { assert.ok(pass, name); passed++; console.log(`PASS ${name}`); };
const wait = ms => new Promise(r => setTimeout(r, ms));
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await wait(100); } }
  const page = await (await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 })).newPage();
  const errors = [], posts = [];
  let notices = [], restart;
  page.on('pageerror', error => errors.push(error.message));
  const notice = (keys, id = 'settings-macbook') => ({ id, kind: 'settings', title: 'Settings differ', level: 'card', at: new Date().toISOString(), device: 'macbook', keys,
    line: `${keys.length} setting(s) on macbook differ from the brain's: ${keys.join(', ')}`,
    choices: [{ id: 'device', label: "Use this computer's" }, { id: 'brain', label: "Use the brain's" }] });
  await page.addInitScript(() => {
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, openUrl: async () => true, openMail: async () => true, onGhostty: () => () => {}, onMouseDown: cb => { window.__down = cb; return () => {}; }, onClaudeFront: cb => { window.__front = cb; return () => {}; }, plugins: async () => ({}),
    };
    window.__sockets = [];
    window.WebSocket = class { constructor() { window.__sockets.push(this); setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
  });
  await page.route(`${daemon}/**`, route => {
    const url = new URL(route.request().url()), method = route.request().method(), p = url.pathname;
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    if (p === '/inherent/controls') return json({ mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' });
    if (p === '/inherent/claude-sessions') return json({ sessions: [], error: null });
    if (p === '/inherent/agent-marks') return json({ marks: {} });
    if (p === '/inherent/notices' && method === 'GET') return json({ notices, audio_private: false, hold: null });
    if (p.startsWith('/inherent/notices/') && method === 'POST') { posts.push({ id: decodeURIComponent(p.split('/').pop()), body: JSON.parse(route.request().postData() || '{}') }); return json({ ok: true, ...(restart ? { restart } : {}) }); }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });
  const open = async () => { await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${daemonPort}`); await page.waitForTimeout(800); };
  const card = () => page.locator('.notch-note.is-open .nc-settings');
  const shows = () => page.waitForFunction(() => document.querySelector('.notch-note.is-open .nc-settings'), null, { timeout: 8000 });
  const gone = () => page.waitForFunction(() => !document.querySelector('.notch-note.is-open .nc'), null, { timeout: 8000 });
  const of = id => posts.filter(x => x.id === id).map(x => JSON.stringify(x.body));

  // (a) the card: title, line, both choices as buttons; nothing is sent by showing it.
  notices = [notice(['observer.usage.enabled', 'onboarded'])]; restart = 'idle';
  await open(); await shows();
  const text = await card().innerText();
  check('the card shows its title and the differing keys', /Settings differ/.test(text) && /observer\.usage\.enabled/.test(text) && /onboarded/.test(text));
  const buttons = await page.locator('.notch-note.is-open .nc-settings .nc-choice .btn').allInnerTexts();
  check('both choices are buttons, in the daemon\'s words', buttons.join('|') === "Use this computer's|Use the brain's");
  check('showing the card sends nothing', posts.length === 0);
  await page.screenshot({ path: path.join(dir, 'settings-card.png'), clip: { x: 0, y: 0, width: 640, height: 360 } });

  // (b) untouched and with a press elsewhere, it stays: a decision is not folded away or dismissed by looking elsewhere.
  await page.mouse.move(5, 600);
  await page.waitForTimeout(6500);
  await page.evaluate(() => window.__down());
  await page.waitForTimeout(1200);
  check('it stays up untouched and through a press elsewhere', await card().count() === 1 && posts.length === 0);

  // (c) a choice posts feedback with the choice id as the reaction, and the card says what happens next.
  await card().locator('.btn', { hasText: "Use this computer's" }).click();
  await page.waitForSelector('.notch-note.is-open .nc-settings .nc-ok');
  check('the first choice posts feedback "device"', of('settings-macbook').at(-1) === '{"action":"feedback","reaction":"device"}');
  check('the card says it restarts once idle', /restarts once Jarvis is idle/.test(await card().innerText()));
  await gone();
  check('the card goes by itself after the outcome shows', await page.locator('.notch-note.is-open .nc').count() === 0);

  // (d) the same set of keys, still listed by the daemon, does not come back; a changed set does.
  await page.waitForTimeout(6000);
  check('an unchanged set is not shown again', await page.locator('.notch-note.is-open .nc').count() === 0);
  notices = [notice(['observer.usage.enabled', 'memory.retain_audio'])]; restart = undefined;
  await shows();
  check('a changed set of keys comes back', /memory\.retain_audio/.test(await card().innerText()));
  await card().locator('.btn', { hasText: "Use the brain's" }).click();
  await page.waitForSelector('.notch-note.is-open .nc-settings .nc-ok');
  check('the second choice posts feedback "brain" and says it kept the brain\'s', of('settings-macbook').at(-1) === '{"action":"feedback","reaction":"brain"}' && /Kept the brain/.test(await card().innerText()));
  await gone();

  // (e) a manual restart (the daemon is not supervised) is said plainly; Esc and the × dismiss.
  notices = [notice(['onboarded', 'language'])]; restart = 'manual';
  await shows();
  await card().locator('.btn', { hasText: "Use this computer's" }).click();
  await page.waitForSelector('.notch-note.is-open .nc-settings .nc-ok');
  check('"manual" tells him to restart Jarvis', /Restart Jarvis to apply/.test(await card().innerText()));
  await gone();
  notices = [notice(['onboarded', 'language', 'llm.provider'])];
  await shows();
  await page.waitForTimeout(700); // Esc is ignored until the card has faded in (checkVisibility)
  await page.keyboard.press('Escape'); await gone();
  check('Esc sends dismissed', of('settings-macbook').at(-1) === '{"action":"feedback","reaction":"dismissed"}');
  notices = [notice(['onboarded', 'llm.provider'])];
  await shows();
  await card().locator('.nc-dismiss').click(); await gone();
  check('the × sends dismissed', of('settings-macbook').at(-1) === '{"action":"feedback","reaction":"dismissed"}');

  // (f) a card that arrives while a hold keeps it off the screen (the Claude app in front): the island shows it as waiting and lists it,
  // nothing is sent, and it comes up by itself once the hold lifts; a held card can also be brought up from the list, or cleared there.
  const counts = () => page.evaluate(() => document.querySelector('.notch')?.dataset.counts ?? '');
  const waitCounts = (re, ms = 9000) => page.waitForFunction(re => new RegExp(re).test(document.querySelector('.notch')?.dataset.counts ?? ''), re.source, { timeout: ms });
  const listRow = id => page.locator(`.notch-drop .a-sec[data-sec="turn"] .a-row[data-glow="${id}"]`);
  // The pointer sweeps slowly along the wing, right of the notch, until the island's list opens under it.
  const hover = async () => {
    await page.evaluate(() => window.__cursor({ x: 20, y: 500 })); await page.waitForTimeout(500);
    for (let x = 410; x < 560 && !await page.locator('.notch-drop.is-open').count(); x += 6) { await page.evaluate(x => window.__cursor({ x, y: 10 }), x); await page.waitForTimeout(60); }
    await page.waitForSelector('.notch-drop.is-open', { timeout: 3000 });
  };
  await page.evaluate(() => window.__front(true));
  notices = [notice(['onboarded', 'held.one'], 'settings-held')]; restart = undefined;
  await waitCounts(/turn1/);
  await page.waitForTimeout(1200);
  check('held in Claude: the island counts it as waiting on his turn', /turn1/.test(await counts()));
  check('held in Claude: no card is on screen and nothing was sent', await page.locator('.notch-note.is-open .nc').count() === 0 && of('settings-held').length === 0);
  await hover();
  await listRow('settings-held').waitFor({ timeout: 5000 });
  check('held in Claude: the list names it with its line', /Settings differ/.test(await listRow('settings-held').innerText()) && /held\.one/.test(await listRow('settings-held').innerText()));
  await page.screenshot({ path: path.join(dir, 'settings-held-list.png'), clip: { x: 0, y: 0, width: 640, height: 360 } });
  await page.evaluate(() => window.__front(false));
  await shows();
  check('the hold lifts: the card comes up, and the waiting mark is gone', /held\.one/.test(await card().innerText()) && !/turn\d/.test(await counts()));
  await page.waitForTimeout(700); await page.keyboard.press('Escape'); await gone();
  check('Esc on it sends dismissed', of('settings-held').at(-1) === '{"action":"feedback","reaction":"dismissed"}');
  // the list brings a held card up while he is still in Claude, with its own choices; the row's ✕ dismisses it unseen.
  await page.evaluate(() => window.__front(true));
  notices = [notice(['onboarded', 'held.two'], 'settings-held')];
  await waitCounts(/turn1/); await hover();
  await listRow('settings-held').evaluate(el => el.click()); // the panel is still springing: a plain click waits for it to be stable
  await shows();
  check('the list row brings the held card up in Claude', /held\.two/.test(await card().innerText()));
  await card().locator('.btn', { hasText: "Use the brain's" }).click();
  await page.waitForSelector('.notch-note.is-open .nc-settings .nc-ok');
  check('answering it posts the choice', of('settings-held').at(-1) === '{"action":"feedback","reaction":"brain"}');
  await gone(); await waitCounts(/^(?!.*turn)/);
  notices = [notice(['onboarded', 'held.three'], 'settings-held')];
  await waitCounts(/turn1/); await hover();
  await listRow('settings-held').locator('.a-acts [role="button"]').evaluate(el => el.click());
  await waitCounts(/^(?!.*turn)/);
  check('the row\'s ✕ sends dismissed and clears the mark', of('settings-held').at(-1) === '{"action":"feedback","reaction":"dismissed"}' && !/turn\d/.test(await counts()));
  await page.evaluate(() => window.__front(false));

  // (g) settled elsewhere: the daemon stops listing a card, so it leaves the queue (held in Claude) and the screen, and nothing is posted.
  const before = posts.length;
  await page.evaluate(() => window.__front(true));
  notices = [notice(['onboarded', 'gone.one'], 'settings-gone')];
  await waitCounts(/turn1/);
  notices = [];
  await waitCounts(/^(?!.*turn)/);
  await page.evaluate(() => window.__front(false));
  await page.waitForTimeout(1200);
  check('a held card the daemon stops listing disappears, and nothing is posted', await page.locator('.notch-note.is-open .nc').count() === 0 && posts.length === before);
  notices = [notice(['onboarded', 'gone.two'], 'settings-gone')];
  await shows();
  notices = [];
  await gone();
  check('the card on screen closes when the daemon stops listing it, and nothing is posted', posts.length === before);
  notices = [notice(['onboarded', 'gone.three'], 'settings-gone')];
  await shows();
  check('a changed set of keys still comes back afterwards', /gone\.three/.test(await card().innerText()));
  notices = [];
  await gone();

  check('no page errors', errors.length === 0);
  if (errors.length) console.log(errors);
  console.log(`${passed} checks passed`);
} finally { await browser.close(); server.kill(); }

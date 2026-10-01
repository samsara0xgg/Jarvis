// ADR 0058 dictation page beside the caret, in headless Chrome against the built page: what goes in, and when.
// The native bridge and the daemon's NDJSON stream are stubbed; each scene drives one dictation end to end.
// Run after `npm run build`.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdirSync } from 'node:fs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const port = Number(process.env.DICTATION_PORT ?? 5192);
const dir = path.join(root, 'evidence/dictation');
mkdirSync(dir, { recursive: true });
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(port), '--strictPort'], { cwd: root, stdio: 'ignore' });
const checks = [], check = (name, pass) => { assert.ok(pass, name); checks.push(name); };
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
const SPOKEN = '把 Typlus 的悬浮窗改成星核的样子。', FIXED = '把 Typlus 的悬浮窗改成星核的样子！';
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${port}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    const on = {};
    window.__log = [];
    window.__on = on;
    window.dictation = {
      onStart: cb => { on.start = cb; }, onFinish: cb => { on.finish = cb; }, onCancel: cb => { on.cancel = cb; }, onCursor: cb => { on.cursor = cb; },
      paste: text => window.__log.push(['paste', text]), copy: text => window.__log.push(['copy', text]),
      target: async () => window.__target ?? 'ok',
      home: happy => window.__log.push(['home', happy]), done: () => window.__log.push(['done']),
      passthrough: () => {}, focus: value => window.__log.push(['focus', value]), open: page => window.__log.push(['open', page]), again: () => window.__log.push(['again']),
    };
    // The daemon: one open stream per dictation, fed line by line from the test.
    window.fetch = async url => {
      if (String(url).endsWith('/inherent/dictation/stop')) { window.__log.push(['stop']); return new Response('{"ok":true}'); }
      const stream = new ReadableStream({ start(controller) { window.__daemon = {
        push: line => controller.enqueue(new TextEncoder().encode(`${JSON.stringify(line)}\n`)), close: () => controller.close() }; } });
      return new Response(stream, { status: 200 });
    };
  });
  await page.goto(`http://127.0.0.1:${port}/dictation.html`);
  const log = () => page.evaluate(() => window.__log.splice(0));
  const push = line => page.evaluate(l => window.__daemon.push(l), line);
  const start = async (trusted = true) => {
    await page.evaluate(trusted => window.__on.start({ caret: trusted ? { l: 300, t: 300, r: 302, b: 318 } : null, lineRight: 480,
      element: trusted ? { l: 100, t: 280, r: 900, b: 520 } : null, pointer: { x: 600, y: 600 }, top: 32, skin: 'glass', lang: 'zh', port: '9999',
      trusted, grantee: 'node', context: { app: 'Notes', window: '', selected: '', before: '' } }), trusted);
    await page.waitForTimeout(500); // up through her hole and listening
    await push({ level: .4 });
  };
  const tapHer = () => page.locator('#hit').click({ force: true });
  const box = page.locator('#bubble textarea');

  // Finishing by tapping her: the words come back in a box that has the keyboard, and nothing goes in yet.
  await start();
  await tapHer();
  await page.waitForTimeout(100);
  check('01 tapping her while she listens finishes and says she will let you edit', (await log()).some(([k]) => k === 'stop')
    && (await page.locator('#bubble').textContent()).includes('写好先给你改'));
  await push({ state: 'thinking', seconds: 3 });
  await push({ text: SPOKEN, raw: SPOKEN });
  await page.waitForTimeout(150);
  const opened = await log();
  check('01 the polished words open in the box, which takes the keyboard; nothing is pasted',
    await box.inputValue() === SPOKEN && await page.evaluate(() => document.activeElement?.tagName === 'TEXTAREA')
    && opened.some(([k, v]) => k === 'focus' && v === true) && !opened.some(([k]) => k === 'paste'));
  await page.screenshot({ path: path.join(dir, '01-edit.png'), clip: { x: 250, y: 200, width: 700, height: 260 } });
  await box.evaluate(el => el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true })));
  await page.waitForTimeout(100);
  check('01 Enter while an input method composes stays in the box', await box.count() === 1 && !(await log()).some(([k]) => k === 'paste'));
  await box.fill(FIXED);
  await box.press('Enter');
  await page.waitForTimeout(600);
  const pasted = await log();
  check('01 Enter gives the keyboard back, then pastes the fixed words and she goes home happy',
    pasted.findIndex(([k, v]) => k === 'focus' && v === false) < pasted.findIndex(([k]) => k === 'paste')
    && pasted.some(([k, v]) => k === 'paste' && v === FIXED) && pasted.some(([k, v]) => k === 'home' && v === true));
  await page.waitForTimeout(700);
  await log();

  // Tapping her while she thinks also opens the box; tapping her again pastes it.
  await start();
  await page.evaluate(() => window.__on.finish());
  await page.waitForTimeout(80);
  await tapHer();
  await push({ state: 'thinking', seconds: 2 });
  await push({ text: SPOKEN, raw: SPOKEN });
  await page.waitForTimeout(150);
  check('02 a tap while she thinks opens the box too', await box.inputValue() === SPOKEN);
  await tapHer();
  await page.waitForTimeout(600);
  check('02 tapping her again pastes what is in the box', (await log()).some(([k, v]) => k === 'paste' && v === SPOKEN));
  await page.waitForTimeout(700);
  await log();

  // Finishing with the right ⌥ pastes straight away, as before.
  await start();
  await page.evaluate(() => window.__on.finish());
  await push({ state: 'thinking', seconds: 2 });
  await push({ text: SPOKEN, raw: SPOKEN });
  await page.waitForTimeout(600);
  const direct = await log();
  check('03 finishing with the right ⌥ pastes the words with no box', direct.some(([k, v]) => k === 'paste' && v === SPOKEN) && !direct.some(([k]) => k === 'focus'));
  await page.waitForTimeout(700);
  await log();

  // Esc in the box, and a box emptied before Enter: nothing goes in.
  await start();
  await tapHer();
  await push({ state: 'thinking', seconds: 2 });
  await push({ text: SPOKEN, raw: SPOKEN });
  await page.waitForTimeout(150);
  await box.press('Escape');
  await page.waitForTimeout(900);
  const escaped = await log();
  check('04 Esc in the box cancels: the keyboard goes back and nothing is pasted', escaped.some(([k, v]) => k === 'focus' && v === false) && !escaped.some(([k]) => k === 'paste'));
  await start();
  await tapHer();
  await push({ state: 'thinking', seconds: 2 });
  await push({ text: SPOKEN, raw: SPOKEN });
  await page.waitForTimeout(150);
  await box.fill('  ');
  await box.press('Enter');
  await page.waitForTimeout(900);
  check('04 an emptied box pastes nothing', !(await log()).some(([k]) => k === 'paste'));
  await page.waitForTimeout(700);
  await log();

  // No Accessibility: she says so by the mouse with a button to the switch, and the card after keeps it.
  await start(false);
  const warned = await page.locator('#bubble').textContent();
  check('05 without Accessibility she says so and names the row to turn on', warned.includes('还没有辅助功能权限') && warned.includes('在列表里打开“node”'));
  await page.locator('#bubble .go').click();
  check('05 its button opens the Accessibility pane', (await log()).some(([k, v]) => k === 'open' && v === 'accessibility'));
  await page.evaluate(() => window.__on.finish());
  await push({ state: 'thinking', seconds: 2 });
  await push({ text: SPOKEN, raw: SPOKEN });
  await page.waitForTimeout(200);
  await page.locator('#bubble.card .go').click();
  const carded = await log();
  check('05 the copied card offers the same button', (await page.locator('#bubble.card b').textContent()) === '还没有辅助功能权限' && carded.some(([k, v]) => k === 'copy' && v === SPOKEN) && carded.some(([k, v]) => k === 'open' && v === 'accessibility'));
  await page.screenshot({ path: path.join(dir, '05-no-access-card.png'), clip: { x: 450, y: 450, width: 700, height: 330 } });

  // The right ⌥ while that card is still up: the card goes and the next dictation starts.
  await page.evaluate(() => window.__on.finish());
  check('06 the right ⌥ over a card still up clears it and asks for a new dictation', (await log()).some(([k]) => k === 'again') && await page.locator('#bubble').isHidden());
  await start();
  check('06 the new one comes up listening with no card left', await page.locator('#bubble').isHidden());
  await tapHer();
  await push({ state: 'thinking', seconds: 2 });
  await push({ text: SPOKEN, raw: SPOKEN });
  await page.waitForTimeout(150);
  await log();
  // The right ⌥ while fixing the words pastes them, like Enter.
  await page.evaluate(() => window.__on.finish());
  await page.waitForTimeout(600);
  check('07 the right ⌥ in the box pastes what is there', (await log()).some(([k, v]) => k === 'paste' && v === SPOKEN));
  await page.waitForTimeout(700);
  await log();

  // ADR 0110: the text box she started at lost its focus for good, or another app came to the front: the words are
  // copied and shown, never pasted into whatever has the keyboard now.
  for (const where of ['lost', 'elsewhere']) {
    await page.evaluate(w => { window.__target = w; }, where);
    await start();
    await page.evaluate(() => window.__on.finish());
    await push({ state: 'thinking', seconds: 2 });
    await push({ text: SPOKEN, raw: SPOKEN });
    await page.waitForTimeout(600);
    const gone = await log();
    check(`08 ${where}: copied and shown in a card, not pasted`, !gone.some(([k]) => k === 'paste')
      && gone.some(([k, v]) => k === 'copy' && v === SPOKEN) && (await page.locator('#bubble.card b').textContent()) === '原来的输入框不在了');
    await page.locator('#bubble.card .x').click();
    await page.waitForTimeout(700);
    await log();
  }
  await page.evaluate(() => { window.__target = 'blind'; });
  await start();
  await page.evaluate(() => window.__on.finish());
  await push({ state: 'thinking', seconds: 2 });
  await push({ text: SPOKEN, raw: SPOKEN });
  await page.waitForTimeout(600);
  check('08 blind (the app shows no field to check against): pasted as before', (await log()).some(([k, v]) => k === 'paste' && v === SPOKEN));
  check('no page errors', errors.length === 0);
  console.log(`${checks.length} checks passed`);
} finally {
  await browser.close();
  server.kill();
}

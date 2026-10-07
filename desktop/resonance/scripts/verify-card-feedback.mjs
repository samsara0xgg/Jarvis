// ADR 0160: the 合适吗 row on every proactive card that is not job mail, in headless Chrome against the built page and a fake
// daemon (route mocking). Each card kind (an agent finish, a finish that asks, a needs-you ask, what waited while quiet, the
// night card and the morning one) shows the row folded, posts a snapshot to POST /inherent/cards/{id} when it comes up
// (title and counts, never what the agent wrote) and the right body when Allen answers, dismisses or uses the card;
// the cards he is in the middle of (confirmation, ask, bedtime) have no row. Silent: no desktop window, no audio.
// Run after `npm run build`.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = path.join(root, 'evidence', 'card-feedback');
mkdirSync(dir, { recursive: true });
const daemonPort = Number(process.env.DAEMON_PORT ?? 8798), web = Number(process.env.COMPANION_PORT ?? 5197), daemon = `http://127.0.0.1:${daemonPort}`;
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security', '--mute-audio'] });
let passed = 0;
const check = (name, pass) => { assert.ok(pass, name); passed++; console.log(`PASS ${name}`); };
const wait = ms => new Promise(r => setTimeout(r, ms));
const M = 60_000;
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await wait(100); } }
  const page = await (await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 })).newPage();
  const errors = [], posts = [], answers = [];
  let board = [], night = { night: null, last: null, hours: 2, laptop: true }, confirmation = null, clarification = null;
  let controls = { mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' };
  page.on('pageerror', error => errors.push(error.message));
  const session = (id, phase, extra = {}) => ({ agent: 'claude', session_id: id, kind: 'interactive', phase, title: `Session ${id}`, project: 'jarvis', branch: 'main', cwd: '/x', where: 'Ghostty', prompt: 'go', activity: 'Wants to run npm test', last_message: 'Needs your decision', started_ms: Date.now() - 60_000, updated_ms: Date.now(), request: null, from_project: false, compacting: false, error: '', ...extra });
  const working = id => session(id, 'working');
  const ask = (id, extra) => session(id, 'done', { asks: true, last_message: 'Which of the two do you want, A or B?', ...extra });
  const req = (id, rid, command) => session(id, 'needs_input', { request: { id: rid, tool: 'Bash', input: { command }, cwd: '/x', always: '' } });
  await page.addInitScript(() => {
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
    const url = new URL(route.request().url()), method = route.request().method(), p = url.pathname;
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    if (p === '/inherent/controls') return json(controls);
    if (p === '/inherent/claude-sessions') return json({ sessions: board, error: null });
    if (p === '/inherent/agent-marks') return json({ marks: {} });
    if (p === '/inherent/night') return json(night);
    if (p === '/inherent/confirmation' && method === 'GET') return json({ card: confirmation });
    if (p === '/inherent/clarification' && method === 'GET') return json({ card: clarification });
    if (p.startsWith('/inherent/cards/') && method === 'POST') { posts.push({ id: decodeURIComponent(p.slice('/inherent/cards/'.length)), body: JSON.parse(route.request().postData() || '{}') }); return json({ ok: true }); }
    if (p.startsWith('/inherent/claude-requests/') && method === 'POST') { answers.push(decodeURIComponent(p.split('/').pop())); return json({ ok: true }); }
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });
  const open = async () => {
    await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${daemonPort}`);
    await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
    await page.waitForTimeout(800);
  };
  const shot = async (name, clip = { x: 0, y: 0, width: 640, height: 480 }) => { await page.waitForTimeout(900); await page.screenshot({ path: path.join(dir, `${name}.png`), clip }); };
  const note = '.notch-note.is-open';
  const shows = (sel = '.nc, .nt-card.pop') => page.waitForFunction(s => document.querySelector(`.notch-note.is-open ${s.split(', ').join(', .notch-note.is-open ')}`), sel, { timeout: 8000 });
  const gone = () => page.waitForFunction(() => !document.querySelector('.notch-note.is-open .nc, .notch-note.is-open .nt-card, .notch-note.is-open .ac'), null, { timeout: 5000 });
  const mine = id => posts.filter(x => x.id === id).map(x => x.body);
  const last = () => posts.at(-1);
  const settle = async () => { await page.mouse.move(5, 600); await page.waitForTimeout(700); };
  // A session is first seen working, then does the thing: the way a real one arrives.
  const arrive = async (...rows) => { board = rows.map(r => working(r.session_id)); await page.waitForTimeout(1800); board = rows; };
  const rows = `${note} .nc-rate-open`;

  await open();

  // (a) an agent finish: the name pop. The row is folded in the pop; the snapshot carries a title and counts only.
  await arrive(session('f-1', 'done', { last_message: 'SECRET-BODY the answer is 42' }));
  await shows('.nt-card.pop');
  const pop = posts.find(x => /^pop:f-1:/.test(x.id));
  check('a finish pop posts one snapshot: kind pop, a level, a title and counts', !!pop && pop.body.action === 'seen' && pop.body.kind === 'pop' && pop.body.level === 'card_sound' && pop.body.facts.title === 'Session f-1' && pop.body.facts.count === 1 && JSON.stringify(pop.body.facts.states) === '["done"]');
  check('the snapshot has no message text', !/SECRET|answer is/.test(JSON.stringify(posts)));
  check('the pop shows the 合适吗 link, folded', await page.locator(`${note} .nt-card.pop .nc-rate-open`).count() === 1 && await page.locator(`${note} .nt-card.pop .nc-rate`).count() === 0);
  await shot('pop-folded', { x: 100, y: 0, width: 440, height: 220 });
  await page.locator(`${note} .nc-rate-open`).click();
  await page.waitForSelector(`${note} .nt-card.pop .nc-rate`);
  check('it opens to five levels with the shown one marked', (await page.locator(`${note} .nc-lv`).allInnerTexts()).join('|') === 'Log only|Glow|Card|Card + sound|Speak' && await page.locator(`${note} .nc-lv.is-now`).innerText() === 'Card + sound');
  await shot('pop-open', { x: 100, y: 0, width: 440, height: 300 });
  await page.locator(`${note} .nc-lv`, { hasText: 'Glow' }).click();
  await gone();
  check('a level on the pop posts feedback level:亮一下 for the same id and the pop goes', last().id === pop.id && JSON.stringify(last().body) === '{"action":"feedback","reaction":"level:亮一下"}');
  check('and no dismissed follows it', !mine(pop.id).some(b => b.action === 'dismissed'));
  board = [];

  // (b) a pop closed with its ×: dismissed, once.
  await arrive(session('f-2', 'done'));
  await shows('.nt-card.pop');
  await page.locator(`${note} .nt-card.pop .c-x`).click();
  await gone();
  const p2 = posts.find(x => /^pop:f-2:/.test(x.id));
  check('the pop ×  posts dismissed once', !!p2 && JSON.stringify(mine(p2.id).slice(1)) === '[{"action":"dismissed"}]');
  board = [];

  // (c) a finish that asks (ADR 0125): a needs-you card. The row stays on the card after a click, which only says thanks.
  await arrive(ask('w-1'));
  await shows('.nc');
  await settle();
  const wait1 = posts.find(x => /^wait:w-1:/.test(x.id));
  check('a finish that asks posts kind wait with its title and agent, not its words', !!wait1 && wait1.body.kind === 'wait' && wait1.body.facts.title === 'Session w-1' && wait1.body.facts.agent === 'claude' && !/which of the two/i.test(JSON.stringify(wait1.body)));
  check('the needs-you card has the folded 合适吗 link', await page.locator(`${rows}`).count() === 1);
  await page.locator(rows).click();
  await page.locator(`${note} .nc-lv`, { hasText: 'Speak' }).click();
  await page.waitForSelector(`${note} .nc-ok`);
  check('开口 posts level:开口 and the card stays with a thanks line', JSON.stringify(last().body) === '{"action":"feedback","reaction":"level:开口"}' && await page.locator(`${note} .nc`).count() === 1);
  await page.keyboard.press('Escape'); await gone();
  check('putting a rated card away posts no dismissed', !mine(wait1.id).some(b => b.action === 'dismissed'));
  board = [];

  // (d) a needs-you ask with Allow and Deny: its id is the request's; the command is never in the snapshot; Esc is dismissed; the card's own button is acted.
  await arrive(req('r-1', 'req-9', 'echo SECRET-TOKEN > /tmp/x'));
  await shows('.nc');
  await settle();
  const r1 = mine('req:req-9');
  check('a request card posts under its request id with kind req, the tool and no command', r1.length === 1 && r1[0].kind === 'req' && r1[0].facts.tool === 'Bash' && !/SECRET/.test(JSON.stringify(posts)));
  check('the request card has the folded link too, and Allow and Deny are still there', await page.locator(rows).count() === 1 && await page.locator(`${note} [data-deny]`).count() === 1);
  await shot('req-folded', { x: 100, y: 0, width: 440, height: 360 });
  await page.keyboard.press('Escape'); await gone();
  check('Esc on an unrated ask posts dismissed', JSON.stringify(mine('req:req-9').at(-1)) === '{"action":"dismissed"}');
  board = [working('r-2')]; await page.waitForTimeout(1800);
  board = [req('r-2', 'req-10', 'ls')];
  await shows('.nc');
  await settle();
  await page.locator(`${note} .btn-warm`, { hasText: 'Allow' }).click();
  await gone();
  check('Allow answers the request and posts acted for it', answers.includes('req-10') && JSON.stringify(mine('req:req-10').at(-1)) === '{"action":"feedback","reaction":"acted"}');
  board = [];

  // (e) what waited at no-pop: one digest, rated like a card. Two sessions did something while he was away.
  controls = { ...controls, quiet: 'no-pop' };
  await open();
  await arrive(session('d-1', 'done'), req('d-2', 'req-11', 'make'));
  await page.waitForTimeout(1500);
  check('at no-pop nothing is shown and nothing is posted for it', await page.locator(`${note} .nc, ${note} .nt-card`).count() === 0 && !posts.some(x => /^digest:|d-1|req:req-11/.test(x.id)));
  controls = { ...controls, quiet: 'off' };
  await page.evaluate(q => window.__emit('controls', q), controls);
  await shows('.nc-digest');
  await settle();
  const dg = posts.find(x => /^digest:/.test(x.id));
  check('the digest posts kind digest with its counts', !!dg && dg.body.kind === 'digest' && dg.body.facts.count === 2 && dg.body.facts.needs === 1 && dg.body.facts.done === 1);
  check('the digest has the folded link', await page.locator(`${note} .nc-digest .nc-rate-open`).count() === 1);
  await page.locator(rows).click();
  await page.locator(`${note} .nc-right`).click();
  await gone();
  check('对 on the digest posts right and closes it', JSON.stringify(last().body) === '{"action":"feedback","reaction":"right"}' && last().id === dg.id);
  board = []; controls = { ...controls, quiet: 'off' };

  // (f) the night: the card when the screen wakes has the row (it stays); the bedtime card has none; the morning one has it and closes.
  const run = (phase, extra = {}) => ({ id: 'n1', phase, started_ms: Date.now() - 3 * 60 * M, until_ms: Date.now() + 60 * M, cap_ms: Date.now() + 9 * 60 * M, wake_at_ms: null, dark_at_ms: null, stay: false, released_ms: null, guarded: true,
    watch: { seen: true, blind: false, blind_since_ms: null, lists: { claude: true }, busy: 0, quiet_ms: null, sessions: [] }, ...extra });
  night = { ...night, night: run('starting') };
  await open();
  await shows('.ac');
  check('the bedtime card (his own start of the run) has no row', await page.locator(`${note} .ac`).count() === 1 && await page.locator(`${note} .nc-rate-open`).count() === 0 && !posts.some(x => /^night:/.test(x.id)));
  await shot('night-bedtime-no-row', { x: 100, y: 0, width: 440, height: 360 });
  night = { ...night, night: run('glance') };
  await page.waitForFunction(() => document.querySelector('.notch-note.is-open .nc-rate-open'), null, { timeout: 8000 });
  const glance = posts.find(x => /^night:n1:night$/.test(x.id));
  check('the card when the screen wakes posts kind night once and shows the folded link', !!glance && glance.body.kind === 'night' && glance.body.level === 'card' && glance.body.facts.phase === 'glance' && await page.locator(rows).count() === 1);
  await settle();
  await page.locator(rows).click();
  await page.locator(`${note} .nc-lv`, { hasText: 'Card' }).first().click();
  await page.waitForSelector(`${note} .nc-ok`);
  check('rating it posts the level and the card stays (the run goes on)', JSON.stringify(last().body) === '{"action":"feedback","reaction":"level:卡片"}' && await page.locator(`${note} .ac`).count() === 1);
  const last0 = { id: 'n1', started_ms: Date.now() - 8 * 60 * M, until_ms: Date.now() - 6 * 60 * M, released_ms: Date.now() - 5 * 60 * M, release_reason: 'settled', ended_ms: Date.now() - M, reason: 'returned', slept_ms: null,
    restored: { brightness: true, volume: true }, watch: { seen: true, blind: false, blind_since_ms: null, lists: { claude: true }, busy: 0, quiet_ms: null, sessions: [], monitor_ms: Date.now() - 5 * 60 * M, extra_ms: 0, busy_at_deadline: 0, busy_at_release: 0 }, totals: { nights: 1, extra_ms: 0, blind: 0 } };
  night = { ...night, night: null, last: last0 };
  await page.waitForFunction(() => document.querySelector('.notch-note.is-open .is-morning .nc-rate-open'), null, { timeout: 8000 });
  const morning = posts.find(x => /^night:n1:morning$/.test(x.id));
  check('the morning card posts kind morning and shows the folded link', !!morning && morning.body.kind === 'morning' && await page.locator(`${note} .is-morning .nc-rate-open`).count() === 1);
  await shot('morning-folded', { x: 100, y: 0, width: 440, height: 420 });
  await page.locator(rows).click();
  await page.locator(`${note} .nc-right`).click();
  await gone();
  check('对 on the morning card posts right and closes it, without a dismissed', JSON.stringify(mine('night:n1:morning').slice(1)) === '[{"action":"feedback","reaction":"right"}]');
  night = { night: null, last: null, hours: 2, laptop: true };

  // (g) the cards he is in the middle of: a confirmation and an ask card have no row and post nothing.
  const before = posts.length;
  await open();
  confirmation = { id: 'c-1', tool: 'gmail_send', action: 'Send this email', source: 'gmail', letter: false, args: { to: 'x@example.com' } };
  await page.waitForSelector(`${note} .ac`, { timeout: 8000 });
  check('a confirmation card (his own request) has no row', await page.locator(`${note} .nc-rate-open`).count() === 0);
  confirmation = null;
  clarification = { id: 'q-1', question: 'Which day?', fields: [{ label: 'Day' }] };
  await page.waitForFunction(() => document.querySelector('.notch-note.is-open [data-question]'), null, { timeout: 8000 });
  check('an ask card (details for his request) has no row', await page.locator(`${note} .nc-rate-open`).count() === 0 && posts.length === before);
  clarification = null;

  // (h) in Chinese, as Allen reads it: the folded link says 合适吗 on a pop and on a needs-you card (evidence shots).
  await page.evaluate(() => localStorage.setItem('companion-settings-v1', JSON.stringify({ lang: 'zh' })));
  await open();
  await arrive(session('z-1', 'done'));
  await shows('.nt-card.pop');
  check('zh: the pop link reads 合适吗', (await page.locator(`${note} .nt-card.pop .nc-rate-open`).innerText()) === '合适吗');
  await shot('pop-folded-zh', { x: 100, y: 0, width: 440, height: 220 });
  await page.keyboard.press('Escape');
  board = [working('z-2')]; await page.waitForTimeout(1800);
  board = [req('z-2', 'req-z', 'npm test')];
  await page.locator(`${note} .nt-card.pop .c-x`).click().catch(() => {});
  await shows('.nc');
  await settle();
  check('zh: the needs-you link reads 合适吗', (await page.locator(`${note} .nc .nc-rate-open`).innerText()) === '合适吗');
  await shot('req-folded-zh', { x: 100, y: 0, width: 440, height: 360 });
  await page.keyboard.press('Escape'); await gone();
  await page.evaluate(() => localStorage.removeItem('companion-settings-v1'));

  check('no page errors', errors.length === 0);
  if (errors.length) console.log(errors);
  console.log(`${passed} checks passed`);
} finally { await browser.close(); server.kill(); }

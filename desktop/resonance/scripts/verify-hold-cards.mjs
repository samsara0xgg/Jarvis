// ADR 0163: the daemon's `hold` (a call, or he is away; GET /inherent/notices, or /inherent/moment while job mail is off) holds every
// card the client makes itself, in headless Chrome against the built page and a fake daemon (route mocking). With `call` or `away`
// an agent finish, a needs-you ask, a finish that asks, the night glance and the morning card show nothing and play no cue, and
// nothing is posted or answered; with the hold gone they come up (several as one digest, one alone as itself) with `held_by` and
// `held_s` in the snapshot, and the held ask is still answered from the queue. His own clicks go through a hold, his bedtime card
// is never held, an older daemon (no field) behaves as before, an unreachable daemon lets go after a few polls, and a call is
// believed for 3 h at most. Silent: no desktop window, no audio. Run after `npm run build`.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = path.join(root, 'evidence', 'hold-cards');
mkdirSync(dir, { recursive: true });
const daemonPort = Number(process.env.DAEMON_PORT ?? 8799), web = Number(process.env.COMPANION_PORT ?? 5198), daemon = `http://127.0.0.1:${daemonPort}`;
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security', '--mute-audio'] });
let passed = 0;
const check = (name, pass) => { assert.ok(pass, name); passed++; console.log(`PASS ${name}`); };
const M = 60_000;
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const page = await (await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 })).newPage();
  const errors = [], posts = [], answers = [];
  // `hold`: what the fake daemon says (undefined leaves the field out, as an older daemon does). `mode`: where it says it (`notices`,
  // or `moment` with job mail off: notices are a 404). `down`: the daemon does not answer.
  let hold = undefined, mode = 'notices', down = false, served = [], priv = true, quietNow = 'off', acked = [], board = [], night = { night: null, last: null, hours: 2, laptop: true };
  page.on('pageerror', error => errors.push(error.message));
  const session = (id, phase, extra = {}) => ({ agent: 'claude', session_id: id, kind: 'interactive', phase, title: `Session ${id}`, project: 'jarvis', branch: 'main', cwd: '/x', where: 'Ghostty', prompt: 'go', activity: 'Wants to run npm test', last_message: 'Needs your decision', started_ms: Date.now() - 60_000, updated_ms: Date.now(), request: null, from_project: false, compacting: false, error: '', ...extra });
  const working = id => session(id, 'working');
  const done = id => session(id, 'done');
  const ask = id => session(id, 'done', { asks: true, last_message: 'Which of the two do you want, A or B?' });
  const req = (id, rid, command) => session(id, 'needs_input', { request: { id: rid, tool: 'Bash', input: { command }, cwd: '/x', always: '' } });
  await page.addInitScript(() => {
    window.__cues = 0;
    const Real = window.AudioContext, resume = Real.prototype.resume;
    Real.prototype.resume = function (...a) { window.__cues++; return resume.apply(this, a); };
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: cb => { window.__command = cb; return () => {}; },
      passthrough: () => {}, focus: async () => {}, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, onGhostty: () => () => {}, onMouseDown: cb => { window.__down = cb; return () => {}; }, onClaudeFront: cb => { window.__front = cb; return () => {}; }, plugins: async () => ({}),
    };
    window.__sockets = [];
    window.WebSocket = class { constructor() { window.__sockets.push(this); setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
  });
  await page.route(`${daemon}/**`, route => {
    const url = new URL(route.request().url()), method = route.request().method(), p = url.pathname;
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    const absent = () => route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
    if (down) return route.abort();
    if (p === '/inherent/notices' && method === 'GET') return mode === 'notices' ? json({ notices: served, audio_private: priv, ...(hold === undefined ? {} : { hold }) }) : absent();
    if (p === '/inherent/moment' && method === 'GET') return mode === 'moment' ? json(hold === undefined ? {} : { hold }) : absent();
    if (p === '/inherent/controls') return json({ mic_muted: false, speech_muted: false, conversation: false, quiet: quietNow });
    if (p === '/inherent/claude-sessions') return json({ sessions: board, error: null });
    if (p === '/inherent/agent-marks') return json({ marks: {} });
    if (p === '/inherent/night') return json(night);
    if ((p === '/inherent/confirmation' || p === '/inherent/clarification') && method === 'GET') return json({ card: null });
    if (p.startsWith('/inherent/notices/') && method === 'POST') { acked.push(`${decodeURIComponent(p.slice('/inherent/notices/'.length))}:${JSON.parse(route.request().postData() || '{}').action}`); return json({ ok: true }); }
    if (p.startsWith('/inherent/cards/') && method === 'POST') { posts.push({ id: decodeURIComponent(p.slice('/inherent/cards/'.length)), body: JSON.parse(route.request().postData() || '{}') }); return json({ ok: true }); }
    if (p.startsWith('/inherent/claude-requests/') && method === 'POST') { answers.push(decodeURIComponent(p.split('/').pop())); return json({ ok: true }); }
    return absent();
  });
  const open = async () => {
    await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${daemonPort}`);
    await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
    await page.waitForTimeout(900);
  };
  const note = '.notch-note.is-open', any = `${note} .nc, ${note} .nt-card, ${note} .ac`;
  const cards = () => page.locator(any).count();
  const cues = () => page.evaluate(() => window.__cues);
  const shows = (sel, ms = 12_000) => page.waitForFunction(s => document.querySelector(s), sel, { timeout: ms });
  const mine = re => posts.filter(x => re.test(x.id));
  const settle = async () => { await page.mouse.move(5, 600); await page.waitForTimeout(700); };
  const arrive = async (...rows) => { board = rows.map(r => working(r.session_id)); await page.waitForTimeout(1800); board = rows; };
  const reset = () => { posts.length = 0; answers.length = 0; board = []; night = { night: null, last: null, hours: 2, laptop: true }; hold = undefined; mode = 'notices'; down = false; served = []; priv = true; quietNow = 'off'; acked.length = 0; };
  const run = (phase, extra = {}) => ({ id: 'n1', phase, started_ms: Date.now() - 3 * 60 * M, until_ms: Date.now() + 60 * M, cap_ms: Date.now() + 9 * 60 * M, wake_at_ms: null, dark_at_ms: null, stay: false, released_ms: null, guarded: true,
    watch: { seen: true, blind: false, blind_since_ms: null, lists: { claude: true }, busy: 0, quiet_ms: null, sessions: [] }, ...extra });
  const last0 = () => ({ id: 'n1', started_ms: Date.now() - 8 * 60 * M, until_ms: Date.now() - 6 * 60 * M, released_ms: Date.now() - 5 * 60 * M, release_reason: 'settled', ended_ms: Date.now() - M, reason: 'returned', slept_ms: null,
    restored: { brightness: true, volume: true }, watch: { seen: true, blind: false, blind_since_ms: null, lists: { claude: true }, busy: 0, quiet_ms: null, sessions: [], monitor_ms: Date.now() - 5 * 60 * M, extra_ms: 0, busy_at_deadline: 0, busy_at_release: 0 }, totals: { nights: 1, extra_ms: 0, blind: 0 } });

  // (a) a call: a finish, a needs-you ask and a finish that asks make no card, no cue and no post; then the hold ends and they come up as one digest.
  hold = 'call';
  await open();
  await arrive(done('h-1'), req('h-2', 'req-h2', 'echo held'), ask('h-3'));
  await page.waitForTimeout(2500);
  check('call: a finish, a needs-you ask and a finish that asks show no card', await cards() === 0);
  check('call: no cue sounded and nothing was posted or answered', await cues() === 0 && posts.length === 0 && answers.length === 0);
  await page.waitForTimeout(6500);
  check('call: still nothing after more polls, and the ask is not auto-answered', await cards() === 0 && answers.length === 0 && posts.length === 0);
  hold = null;
  await shows(`${note} .nc-digest`);
  await settle();
  const rowsText = (await page.locator(`${note} .nc-away-row`).allInnerTexts()).join(' | ');
  check('the hold ends: one digest with all three, none lost', await page.locator(`${note} .nc-away-row`).count() === 3 && /h-1/.test(rowsText) && /h-2/.test(rowsText) && /h-3/.test(rowsText));
  check('the cue sounds once the hold has ended', await cues() > 0);
  const digest = posts.find(x => /^digest:/.test(x.id));
  check('the digest snapshot says what held it and for how long', !!digest && digest.body.situation.held_by === 'call' && digest.body.situation.held_s >= 5 && digest.body.facts.count === 3);
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(dir, 'digest-after-call.png'), clip: { x: 100, y: 0, width: 440, height: 320 } });
  await page.locator(`${note} .nc-away-row`, { hasText: 'h-2' }).click();
  await page.locator(`${note} .btn-warm`, { hasText: 'Allow' }).click();
  await page.waitForTimeout(600);
  check('the held ask is still there to answer, from the digest', answers.includes('req-h2'));

  // (b) away: one finish alone comes up as itself, not a digest, with its held_by.
  reset(); hold = 'away';
  await open();
  await arrive(done('h-4'));
  await page.waitForTimeout(2500);
  check('away: a finish shows no pop and plays no cue', await cards() === 0 && await cues() === 0);
  hold = null;
  await shows(`${note} .nt-card.pop`);
  const pop = mine(/^pop:h-4:/)[0];
  check('the hold ends: the one finish comes up as its own pop, not a digest', !!pop && await page.locator(`${note} .nc-digest`).count() === 0);
  check('its snapshot records held_by away', pop.body.situation.held_by === 'away' && pop.body.situation.held_s >= 1);

  // (c) the night glance and the morning card wait; the bedtime card, his own start, does not.
  reset(); hold = 'call'; night = { ...night, night: run('starting') };
  await open();
  await shows(`${note} .ac`, 8000);
  check('call: the bedtime card (his own start of the run) still shows', await cards() === 1);
  // The state arrives after the first poll has said `call` (until it answers, nothing is known and nothing is held).
  reset(); hold = 'call';
  await open();
  night = { ...night, night: run('glance') };
  await page.waitForTimeout(4000);
  check('call: the night glance card does not show', await cards() === 0 && mine(/^night:/).length === 0);
  hold = null;
  await shows(`${note} .nc-rate-open`);
  const glance = mine(/^night:n1:night$/)[0];
  check('the hold ends: the glance card is there, with held_by call', !!glance && glance.body.kind === 'night' && glance.body.situation.held_by === 'call');
  reset(); hold = 'away';
  await open();
  night = { ...night, last: last0() };
  await page.waitForTimeout(4000);
  check('away: the morning card does not show', await page.locator(`${note} .is-morning`).count() === 0 && mine(/^night:/).length === 0);
  hold = null;
  await shows(`${note} .is-morning .nc-rate-open`);
  const morning = mine(/^night:n1:morning$/)[0];
  check('the hold ends: the morning card comes up, with held_by away', !!morning && morning.body.kind === 'morning' && morning.body.situation.held_by === 'away');

  // (d) an older daemon (no field) and an explicit null: as before, a finish shows with its cue at once.
  for (const [name, value] of [['an older daemon (no hold field)', undefined], ['hold null', null]]) {
    reset(); hold = value;
    await open();
    await arrive(done('o-1'));
    await shows(`${note} .nt-card.pop`, 8000);
    await page.waitForTimeout(400);
    check(`${name}: the finish pops with its cue, as today`, await cues() > 0 && !('held_by' in (mine(/^pop:o-1:/)[0]?.body.situation ?? {})));
  }

  // (e) job mail off: notices are a 404 and the hold is read from /inherent/moment.
  reset(); mode = 'moment'; hold = 'call';
  await open();
  await arrive(done('m-1'));
  await page.waitForTimeout(2500);
  check('job mail off: the hold is read from /inherent/moment and holds the finish', await cards() === 0 && await cues() === 0);
  hold = null;
  await shows(`${note} .nt-card.pop`);
  check('job mail off: it comes up when the hold ends', mine(/^pop:m-1:/)[0]?.body.situation.held_by === 'call');
  // Both routes gone (moment off too): one try of each, then calm.
  reset(); mode = 'none';
  await open();
  await arrive(done('m-2'));
  await shows(`${note} .nt-card.pop`, 8000);
  check('no hold route at all: a finish pops as today', await cards() >= 1 && errors.length === 0);

  // (f) his own click goes through a hold: a held ask is brought up from the island's list and answered.
  reset(); hold = 'call';
  await open();
  await arrive(req('k-1', 'req-k1', 'echo mine'));
  await page.waitForTimeout(2500);
  check('call: the ask is held back', await cards() === 0);
  await page.evaluate(() => window.__command('agent-keys'));
  await page.locator('.notch-drop .a-row[data-id="k-1"]').click();
  await shows(`${note} .nc`, 8000);
  await page.waitForTimeout(900);
  await page.screenshot({ path: path.join(dir, 'own-click-in-a-call.png'), clip: { x: 100, y: 0, width: 440, height: 360 } });
  await page.locator(`${note} .btn-warm`, { hasText: 'Allow' }).click();
  await page.waitForTimeout(600);
  check('call: his own click brings the card up and Allow answers it', answers.includes('req-k1'));

  // (g) an unreachable daemon lets go: the last word is kept for a few polls, then it is as if nothing was held.
  reset(); hold = 'call';
  await open();
  await arrive(done('u-1'));
  await page.waitForTimeout(2000);
  down = true;
  await page.waitForTimeout(6500);
  check('daemon unreachable: one missed poll keeps the hold', await cards() === 0);
  await shows(any, 20_000);
  check('daemon unreachable for good: the hold lets go and the finish comes up', await cards() >= 1);

  // (h) a call is believed for 3 h at most: the clock goes forward and the held finish comes up though the daemon still says call.
  reset(); hold = 'call';
  await page.clock.install();
  await open();
  await arrive(done('c-1'));
  await page.waitForTimeout(2500);
  check('call: held at first', await cards() === 0);
  await page.clock.fastForward('03:00:30');
  await shows(any, 8000);
  check('a call older than 3 h no longer holds', await cards() >= 1);

  // (g) ADR 0178: a reminder rides the job-mail notice and goes through the quiet level, a call or away hold and a speaker output, with its cue.
  const reminder = { id: 'reminder-1', kind: 'mail', title: 'one-on-one with the employer', line: 'In 30 minutes: one-on-one with the employer', level: 'card_sound', text: 'one-on-one with the employer', at: new Date().toISOString(), company: '', role: '', event_at: new Date(Date.now() + 30 * M).toISOString(), mail_kind: 'reminder' };
  for (const [name, setup] of [['a call', () => { hold = 'call'; }], ['away', () => { hold = 'away'; }], ['no-pop', () => { quietNow = 'no-pop'; }], ['dnd', () => { quietNow = 'dnd'; }]]) {
    reset(); setup(); priv = false; served = [reminder];
    await open();
    await shows(`${note} .nc-mail`, 8000);
    check(`${name}: the reminder shows`, await cards() === 1);
    check(`${name}: it is labelled Reminder or 提醒, with the line, and has no 合适吗 row`, /reminder|提醒/i.test(await page.locator(`${note} .nc-label`).innerText()) && /In 30 minutes/.test(await page.locator(`${note} .nc-mail`).innerText()) && await page.locator(`${note} .nc-rate-open, ${note} .nc-rate`).count() === 0);
    check(`${name}: its cue sounds although the output is not private`, await cues() > 0);
    check(`${name}: the daemon is told it was seen`, acked.includes('reminder-1:seen'));
  }
  // A reminder that arrives under a hold, behind a held finish, still shows; the finish waits for the end of the hold.
  reset(); hold = 'call'; priv = true; served = [reminder];
  await open();
  await arrive(done('r-1'));
  await page.waitForTimeout(2500);
  check('a call: the reminder shows while the finish stays held', await page.locator(`${note} .nc-mail`).count() === 1 && await page.locator(`${note} .nt-card.pop`).count() === 0);
  // Nobody there: the card does not fold after the usual 30 s, and only a dismissal tells the daemon it was taken in.
  await settle();
  await page.waitForTimeout(32_000);
  check('the reminder is still on the island after 32 s untouched', await page.locator(`${note} .nc-mail`).count() === 1 && !acked.some(x => /:feedback$/.test(x)));
  await page.locator(`${note} .nc-dismiss`).click();
  await page.waitForTimeout(600);
  check('dismissing it puts it away and tells the daemon', await page.locator(`${note} .nc-mail`).count() === 0 && acked.includes('reminder-1:feedback'));

  check('no page errors', errors.length === 0);
  if (errors.length) console.log(errors);
  console.log(`${passed} checks passed`);
} finally { await browser.close(); server.kill(); }

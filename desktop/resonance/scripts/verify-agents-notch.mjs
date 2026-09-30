// Run after npm run build. Jarvis's notch with Startrail's sessions: a real agent host on the stand-in agents
// (scripts/agents-stage.mjs) and the companion page in Chromium, following the host as the companion does: its requests
// to the host carry the key the way main adds it, and the page never holds it. The daemon is a stand-in with no
// sessions of its own; window.jarvis records what the page asks of main (the Agents window opened on a session) and
// plays the Agents window coming to the front. SHOTS=<folder> keeps a screenshot of each state.
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { stage } from './agents-stage.mjs';
import { queue } from '../src/agents/queue.ts';

const here = path.dirname(fileURLToPath(import.meta.url)), dist = path.join(here, '..', 'dist'), shots = process.env.SHOTS;
const st = await stage({ viewport: { width: 640, height: 722 } });
const checks = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name}${detail ? `: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`); checks.push(name); console.log(`PASS ${name}`); };
const listen = server => new Promise(r => server.listen(0, '127.0.0.1', () => r(server.address().port)));
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.json': 'application/json', '.webp': 'image/webp' };

// ---------- the built companion page, and a daemon with nothing of its own ----------
const web = http.createServer(async (q, r) => {
  const f = path.join(dist, decodeURIComponent(new URL(q.url, 'http://x').pathname));
  if (!f.startsWith(dist) || !existsSync(f)) { r.writeHead(404); r.end(); return; }
  r.writeHead(200, { 'Content-Type': TYPES[path.extname(f)] ?? 'application/octet-stream' }); r.end(await readFile(f));
});
// Marks the page sets on the daemon, kept to show that none of Startrail's go there.
const daemonMarks = [];
const daemon = http.createServer((q, r) => {
  const u = new URL(q.url, 'http://x'), cors = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'content-type', 'Access-Control-Allow-Methods': 'GET, POST' };
  if (q.method === 'OPTIONS') { r.writeHead(204, cors); r.end(); return; }
  const json = (v, code = 200) => { r.writeHead(code, { ...cors, 'Content-Type': 'application/json' }); r.end(JSON.stringify(v)); };
  if (q.method === 'POST' && u.pathname.startsWith('/inherent/agent-marks/')) { daemonMarks.push(decodeURIComponent(u.pathname.split('/').pop())); json({}); return; }
  const fixed = { '/inherent/claude-sessions': { sessions: [] }, '/inherent/codex-sessions': { sessions: [] }, '/inherent/agent-marks': { marks: {} },
    '/inherent/confirmation': { card: null }, '/inherent/clarification': { card: null }, '/inherent/think': { on: false, on_words: '(?!)', off_words: '(?!)' },
    '/inherent/language': { language: 'en' }, '/inherent/conversation': { rows: [] } }[u.pathname];
  json(fixed ?? { detail: 'Not Found' }, fixed ? 200 : 404);
});
const webPort = await listen(web), daemonPort = await listen(daemon);

const page = await st.context.newPage();
const errors = [];
page.on('pageerror', e => errors.push(e.message));
page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
await page.addInitScript(() => {
  const s = window.__state = { opened: [], oks: [] };
  // What each answered card said, however briefly it showed.
  addEventListener('DOMContentLoaded', () => new MutationObserver(() => {
    for (const e of document.querySelectorAll('.nc-ok')) if (!s.oks.includes(e.textContent)) s.oks.push(e.textContent);
  }).observe(document.body, { childList: true, subtree: true, characterData: true }));
  // Her link to the daemon's voice turns stays in the page.
  window.WebSocket = class { constructor(url) { this.url = url; setTimeout(() => this.onopen?.(), 0); } send() {} close() { this.onclose?.(); } };
  window.jarvis = {
    placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
    onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {}, wearing: () => {},
    onCursor: cb => { window.__cursor = cb; return () => {}; }, onCommand: cb => { window.__command = cb; return () => {}; },
    passthrough: () => {}, focus: async () => {}, material: () => {},
    codexTitles: async () => ({}), openCodex: async () => true, watchGhostty: () => {}, onGhostty: () => () => {}, jumpGhostty: async () => true,
    openAgents: id => { s.opened.push(id ?? ''); },
    onAgentsPresence: cb => { window.__presence = cb; cb({ active: false, ids: [] }); return () => {}; },
  };
});

// ---------- what the notch shows ----------
const notch = () => page.evaluate(() => {
  const root = document.querySelector('.notch'), pane = root?.querySelector('.notch-note');
  return { marks: root?.dataset.marks ?? '', counts: root?.dataset.counts ?? '', rings: root?.dataset.rings === '1', open: !!pane?.classList.contains('is-open'),
    card: pane?.querySelector('.nc')?.innerText.replace(/\s+/g, ' ').trim() ?? '', pop: pane?.querySelector('.nt-card.pop')?.innerText.replace(/\s+/g, ' ').trim() ?? '',
    popIds: [...pane?.querySelectorAll('.nt-card.pop .u-row') ?? []].map(e => e.dataset.id) };
});
const until = async (what, pred, ms = 15000) => { const t0 = Date.now(); for (;;) { const n = await notch(); if (await pred(n)) return n; if (Date.now() - t0 > ms) throw new Error(`timed out: ${what} (${JSON.stringify(n)})`); await page.waitForTimeout(80); } };
const count = (n, kind) => Number(new RegExp(`\\b${kind}(\\d+)`).exec(n.counts)?.[1] ?? 0);
// A card for that session, down and faded in: keys reach only a card that shows.
const cardFor = async id => {
  const n = await until(`the card for ${id}`, n => n.open && n.card);
  await page.waitForFunction(() => getComputedStyle(document.querySelector('.notch-note .notch-pane-in')).opacity === '1');
  const title = st.row(id)?.title;
  assert.ok(!title || n.card.includes(title), `the card is ${id}'s: ${n.card}`);
  return n;
};
// Whatever hangs from the notch goes: a pop is closed with its ✕, and the pane folds.
const clear = async () => {
  for (let i = 0; i < 40; i++) {
    const n = await notch();
    if (!n.open && !n.card && !n.pop) return;
    if (n.pop) await page.locator('.notch-note .nt-card.pop .c-x').click().catch(() => {});
    await page.waitForTimeout(250);
  }
  throw new Error(`the notch did not clear: ${JSON.stringify(await notch())}`);
};
const shot = async (name, clip = { x: 60, y: 0, width: 520, height: 430 }) => { if (shots) await page.screenshot({ path: path.join(shots, `${name}.png`), clip }); };
// Her queue over what the host says, by the one rule (src/agents/queue.ts).
const expected = () => queue([...st.rows.values()]).map(s => s.id);
const pendingReq = async id => (await st.call(`/sessions/${id}`)).items.find(i => i.k === 'req' && !i.done)?.req;
// What the answered card said, once it says something with `what` in it.
const said = what => page.waitForFunction(w => window.__state.oks.find(t => t.includes(w)), what, { timeout: 15000 }).then(h => h.jsonValue());
const log = (ev, sid) => st.claude().filter(l => l.ev === ev && (!sid || l.sid === sid));

try {
  // A finished, unread session before the page is there: met for the first time, it counts in 轮到你 and pops nothing.
  const first = await st.session('hello first');
  await page.goto(`http://127.0.0.1:${webPort}/index.html?companion=1&port=${daemonPort}&agents=${new URL(st.API).port}`);
  await page.addStyleTag({ content: `html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}
    body::before{content:'';position:fixed;inset:0 0 auto;height:32px;background:rgb(255 255 255/.18);backdrop-filter:blur(20px)}` });
  let n = await until('the notch shows Startrail\'s session', n => n.marks.includes('turn1'));
  await page.waitForTimeout(600);
  n = await notch();
  check('the companion follows the host with the key main adds: a finished, unread Startrail session counts in 轮到你', count(n, 'turn') === 1 && expected().join() === first);
  check('a session met for the first time pops nothing', !n.open && !n.pop);

  // ---- a request to allow drops as a card, and ⌘⏎ answers it through the host ----
  const ask = await st.session('hello ASK', { wait: false });
  n = await cardFor(ask);
  const tags = await page.locator('.notch-note .nc-tags').innerText();
  check('a request to allow drops from the notch as a card: who, project, branch, your message, why and the command',
    /needs your ok/i.test(n.card) && /Claude/.test(tags) && /app/.test(tags) && /main/.test(tags) && /You\s*hello ASK/.test(n.card) && n.card.includes('Say asked') && n.card.includes('echo asked'), n.card);
  check('the card offers always-allow for its command in the project, deny on esc and allow on ⌘⏎',
    n.card.includes('Always allow echo asked in app') && /Deny esc/.test(n.card) && /Allow ⌘⏎/.test(n.card), n.card);
  check('the card can open the session in Startrail, or park it', await page.locator('.notch-note .nc-actions [aria-label="Open in Startrail"]').count() === 1 && await page.locator('.notch-note .nc-park').count() === 1);
  n = await until('the beacon counts the ask', n => count(n, 'turn') === 2);
  check('轮到你 is her queue: the ask and the finished one, the ask first', count(n, 'turn') === expected().length && expected()[0] === ask, { counts: n.counts, queue: expected() });
  check('a new one waiting makes the beacon send out its rings', n.rings);
  await page.waitForTimeout(700);
  await shot('away-jv-card');
  await shot('beacon', { x: 380, y: 0, width: 200, height: 40 });
  await page.locator('.notch-note .nc-always input').check();
  await page.keyboard.press('Meta+Enter');
  const allowed = await said('Allowed');
  await st.until('the stand-in was let through', () => log('permission', ask).length);
  check('⌘⏎ with always-allow ticked allows it through the host\'s /answer, keeping Claude Code\'s rule',
    allowed === "Allowed · won't ask again for Bash(echo asked)" && log('permission', ask).some(l => l.behavior === 'allow' && l.updatedPermissions?.length), { allowed, log: log('permission', ask) });
  await st.until('the asking turn ends', () => st.row(ask)?.st === 'done');

  // ---- a finish pops only its name, and the pop goes to it in Startrail ----
  n = await until('its finish pops', n => n.popIds.includes(ask) && n.pop);
  check('a finished session pops its name, and only that', /^done/i.test(n.pop) && n.popIds.length === 1 && !n.card, n);
  await page.waitForTimeout(500);
  await shot('away-jv-pop');
  await page.locator(`.notch-note .nt-card.pop .u-row[data-id="${ask}"]`).click();
  await st.until('the host counts it read', () => st.row(ask)?.unread === false);
  check('a click on the popped name opens it in Startrail and reads it there', (await page.evaluate(() => window.__state.opened)).includes(ask));
  await clear();

  // ---- a question: its options answer to their digits ----
  const asks = await st.session('hello QUESTION', { wait: false });
  n = await cardFor(asks);
  check('a question drops as a card with its options numbered', /claude asks/i.test(n.card) && n.card.includes('Which way?') && /1 Left/.test(n.card) && /2 Right/.test(n.card), n.card);
  await page.waitForTimeout(500);
  await shot('away-jv-question');
  await page.keyboard.press('2');
  await st.until('the stand-in has the answer', () => log('question', asks).length);
  check('a digit picks that option and the answer goes in through the host', log('question', asks)[0].answers?.['Which way?'] === 'Right', log('question', asks));
  await until('its finish pops', n => n.popIds.includes(asks) && n.pop);
  await clear();

  // ---- esc denies ----
  const deny = await st.session('again ASK', { wait: false });
  await cardFor(deny);
  await page.keyboard.press('Escape');
  await said('Denied');
  await st.until('the stand-in was told no', () => log('permission', deny).length);
  check('esc denies it through the host', log('permission', deny)[0].behavior === 'deny', log('permission', deny));
  await until('its finish pops', n => n.popIds.includes(deny) && n.pop);
  await clear();

  // ---- park: the moon, kept by the host ----
  const park = await st.session('park ASK', { wait: false });
  await cardFor(park);
  const before = daemonMarks.length;
  await page.locator('.notch-note .nc-park').click();
  await st.until('the host parks it', () => st.row(park)?.parked === true);
  n = await until('the notch shows the moon', n => n.marks.includes('moon1') && !n.open);
  check('park sends it to the moon through the host, out of 轮到你', count(n, 'turn') === expected().length && !expected().includes(park), { counts: n.counts, queue: expected() });
  check('none of Startrail\'s marks go to the daemon from the notch', daemonMarks.length === before && !daemonMarks.some(id => st.row(id)), daemonMarks);

  // ---- the Agents window in front: its sessions are looked at there ----
  const open = await st.session('open ASK', { wait: false });
  await cardFor(open);
  await page.locator('.notch-note .nc-actions [aria-label="Open in Startrail"]').click();
  check('↗ on the card opens that session in Startrail', (await page.evaluate(() => window.__state.opened)).at(-1) === open);
  await page.evaluate(ids => window.__presence({ active: true, ids }), [...st.rows.keys()]);
  n = await until('the card gives way to the window', n => !n.open && !n.card);
  check('while the Agents window is in front, the notch leaves its sessions to it', !/turn/.test(n.marks), n);
  const req = await pendingReq(open);
  await st.call(`/sessions/${open}/answer`, { req: req.id, decision: 'allow' });
  await st.until('it finishes in the window', () => st.row(open)?.st === 'done');
  const front = await st.session('front ASK', { wait: false });
  await st.until('it asks while the window is in front', () => st.row(front)?.st === 'wait');
  await page.waitForTimeout(800);
  await page.evaluate(() => window.__presence({ active: false, ids: [] }));
  await page.waitForTimeout(2000);
  n = await notch();
  check('what happened while the window was in front is not told again: no card, no pop', !n.open && !n.card && !n.pop, n);
  check('but it waits in 轮到你, in her queue', count(n, 'turn') === expected().length && expected()[0] === front, { counts: n.counts, queue: expected() });
  await page.evaluate(() => window.__command('agent-keys'));
  await page.locator(`.notch-drop .a-row[data-id="${front}"]`).click();
  n = await cardFor(front);
  check('its card comes from the notch\'s list when asked for', /needs your ok/i.test(n.card));
  await page.keyboard.press('Escape');
  await st.until('the stand-in was told no', () => log('permission', front).length);
  // Its card goes, the list is back, and esc lets go of the keys.
  await until('its card has gone', n => !n.open);
  await page.waitForFunction(() => document.querySelector('.notch-drop')?.classList.contains('is-open'));
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.notch-drop')?.classList.contains('is-open'));
  await clear();

  // ---- stopped: its name pops, as stopped ----
  const broken = await st.session('broken FAIL', { wait: false });
  n = await until('the stop pops', n => n.popIds.includes(broken) && n.pop);
  check('a session that stopped pops its name as stopped', /^stopped/i.test(n.pop), n.pop);
  await clear();

  // ---- the queue beside the notch: asks, then stops, then finished ones; the longest waiting first ----
  const last = await st.session('last ASK', { wait: false });
  await cardFor(last);
  await st.until('every row settled', () => [...st.rows.values()].every(s => s.st !== 'work' && s.st !== 'pack'));
  await page.evaluate(() => window.__command('agent-keys'));
  await page.locator('.notch-drop .a-sec[data-sec="turn"] .a-row').first().waitFor();
  const listed = await page.locator('.notch-drop .a-sec[data-sec="turn"] .a-row').evaluateAll(els => els.map(e => e.dataset.id));
  n = await notch();
  check('the notch\'s 轮到你 list and count are her queue, in its order', listed.join() === expected().join() && count(n, 'turn') === listed.length, { listed, queue: expected(), counts: n.counts });
  check('the queue holds asks, then the stopped one, then finished ones', st.row(listed[0]).st === 'wait' && st.row(listed[1]).st === 'err' && listed.slice(2).every(id => st.row(id).st === 'done'));
  await page.waitForTimeout(400);
  await shot('beacon-list', { x: 60, y: 0, width: 520, height: 330 });
  await page.keyboard.press('Escape');

  // ---- notify.notch: the owner hands these moments back to Startrail's own banners ----
  const off = await st.call('/settings', { notify: { done: true, wait: true, err: true, notch: false } });
  check('the host keeps notify.notch, on unless turned off', off.settings?.notify?.notch === false && (await st.call('/settings')).settings.notify.notch === false);
  n = await until('the notch lets Startrail go', n => !/turn|moon/.test(n.marks) && !n.open);
  const quiet = await st.session('quiet ASK', { wait: false });
  await st.until('it asks', () => st.row(quiet)?.st === 'wait');
  await page.waitForTimeout(2000);
  n = await notch();
  check('with notify.notch off the notch shows nothing of Startrail\'s: no card, no count', !n.open && !n.card && !/turn|moon/.test(n.marks), n);
  await shot('away-notch-off', { x: 60, y: 0, width: 520, height: 120 });
  await st.call(`/sessions/${quiet}/answer`, { req: (await pendingReq(quiet)).id, decision: 'deny' });
  const on = await st.call('/settings', { notify: {} });
  check('turned on again (or never set), the notch takes them', on.settings?.notify?.notch !== false);
  await until('Startrail is back beside the notch', n => count(n, 'turn') === expected().length && n.marks.includes('moon1'));

  check('no errors on the page', !errors.length && !st.errors.length, [...errors, ...st.errors]);
  console.log(`\n${checks.length} checks passed`);
} catch (e) {
  console.error(e, '\n--- host ---\n', st.log().slice(-3000));
  if (errors.length) console.error('--- page ---\n', errors.join('\n'));
  process.exitCode = 1;
} finally {
  await st.close(); web.close(); daemon.close();
}

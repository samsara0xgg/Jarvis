// WP0 (the V2 frame) in a picture: the pane at 820 pt on a stage of 836, 今天 with nothing selected, 今天 with a row selected, and 信 with a letter selected,
// plus a few checks on the keys and the frame's geometry. Headless Chrome against the built page (run `npm run build` first) with the native bridge stubbed and a
// fake daemon that serves a day and some letters, as scripts/verify-notch-timeline.mjs does. PNGs land in $OUT (default verify-out/v2/, not committed).
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = process.env.OUT ?? path.join(root, 'verify-out/v2');
mkdirSync(out, { recursive: true });
const web = Number(process.env.COMPANION_PORT ?? 5188), daemonPort = Number(process.env.COMPANION_DAEMON_PORT ?? 8788), daemon = `http://127.0.0.1:${daemonPort}`;
const STAGE = 836, PANE = 820;
const errors = [], check = (name, pass, detail = '') => { assert.ok(pass, `${name} ${detail}`); console.log('ok', name); };

// ---------- the day: 2026-10-19 in Vancouver (PDT, UTC-7), now 21:50 ----------
const DATE = '2026-10-19', TZ = 'America/Vancouver';
const T = (h, m = 0, day = 19) => Date.UTC(2026, 9, day, h + 7, m);
const NOW = T(21, 50), iso = (h, m) => `2026-10-19T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00-07:00`;
const it = (id, kind, start, end, extra = {}, lane = 'you') => ({ id, lane, kind, start_ms: start, end_ms: end, state: end === null ? (start > NOW ? 'next' : 'now') : end <= NOW ? 'done' : start > NOW ? 'next' : 'now', ...extra });
const home = { id: 'p-home', name: '家', role: 'home' }, uvic = { id: 'p-uvic', name: 'UVic', role: 'school' };
// what he was doing, as the brain serves the doing lane (set for the chips pass at the end; the first pass runs on stays alone): a stretch with no words is the bar's dashed "?"
let SPANS = [];
const span = (id, from, to, over = {}) => ({ id, lane: 'doing', kind: 'doing', start_ms: from, end_ms: to, title: null, detail: null, category: 'unknown', basis: 'observed', confidence: 'high', confirmed: false, clues: [], question: null, place_id: null, item_ids: [], ...over });
const day = () => ({
  date: DATE, tz: TZ, start_ms: T(0), end_ms: T(24), now_ms: NOW,
  items: [
    it('sleep-1', 'sleep', T(0, 55), T(9, 5), { minutes: 470 }),
    it('stay-home-1', 'stay', T(9, 5), T(11, 25), { place: '12 Example St, Victoria', kept_place: home }),
    it('talk-1', 'talk', T(10, 2), T(10, 9), { turns: 6 }, 'her'),
    it('work-1', 'work', T(10, 30), T(11, 20), { active_s: 2520, projects: [{ name: 'Jarvis', seconds: 2100 }], apps: [{ name: 'Xcode', seconds: 1500 }, { name: 'Ghostty', seconds: 1200 }] }),
    it('move-1', 'move', T(11, 25), T(13, 5), { activity: 'automotive' }),
    it('stay-aw', 'stay', T(13, 15), T(13, 55), { place: 'A&W, 1234 Douglas St, Victoria' }),
    it('move-3', 'move', T(13, 55), T(14, 5), { activity: 'walking' }),
    it('ride-1', 'ride', T(14, 50), T(15, 19), { route: '27', board_stop: 'Shelbourne at Pear', alight_stop: 'UVic Exchange', source: 'tapped', ended_reason: 'left_vehicle', credit: null }),
    it('stay-uvic', 'stay', T(15, 19), T(18, 15), { place: 'University of Victoria, Victoria', kept_place: uvic }),
    it('ride-2', 'ride', T(18, 15), T(18, 50), { route: '27', board_stop: 'UVic Exchange', alight_stop: 'Shelbourne at Pear', source: 'tapped', ended_reason: 'left_vehicle', credit: null }),
    it('stay-home-3', 'stay', T(18, 50), null, { place: '12 Example St, Victoria', kept_place: home }),
    it('work-2', 'work', T(19, 35), T(21, 40), { active_s: 6900, projects: [{ name: 'Jarvis', seconds: 6000 }], apps: [{ name: 'Ghostty', seconds: 4200 }, { name: 'Claude', seconds: 1800 }] }),
    it('reminder-1', 'reminder', T(22, 0), null, { text: '把垃圾拿出去', fired: false, acknowledged: false }, 'her'),
    it('event-1', 'event', T(22, 30), T(23, 30), { title: '小组讨论', place: 'Library', private: false, source: 'mail', confirmed: false }),
  ],
  spans: SPANS,
  labels: [], missing: [], line: { text: '今天一半在路上，一半在 UVic。晚上回家后写了会儿代码。', draft: false, written_at: '2026-10-19T21:30:00-07:00' }, highlights: [],
});
const mail = (id, from, subject, received, extra = {}) => ({ id, from, address: `${id}@example.com`, thread_id: `th-${id}`, subject, received, reply: null, junk: false, importance: 1, category: null, ...extra });
const LETTERS = [
  mail('m-nw', 'Northwind Recruiting', 'Interview times — please confirm', iso(9, 12), { reply: 'yes' }),
  mail('m-reg', 'UVic Registrar', 'Your fall schedule', iso(11, 2), { reply: 'fyi' }),
  mail('m-mom', '妈妈', '周末回家吃饭吗？', iso(15, 5), { reply: 'yes' }),
  mail('m-gh', 'GitHub', '[jarvis] Run failed: verify', iso(16, 30), { reply: 'fyi' }),
];

// ---------- the built page ----------
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const exe = [process.env.CHROMIUM, '/opt/pw-browsers/chromium'].find(p => p && existsSync(p));
const browser = await chromium.launch({ headless: true, ...exe ? { executablePath: exe } : { channel: 'chrome' }, args: ['--disable-web-security', '--mute-audio'] });
try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const ctx = await browser.newContext({ viewport: { width: STAGE, height: 760 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
  await page.addInitScript(([width]) => {
    localStorage.setItem('companion-settings-v1', JSON.stringify({ lang: 'zh' }));
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: width, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {}, wearing: () => {}, onDictation: () => () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, onGhostty: () => () => {}, onAgentsPresence: () => () => {}, onMouseDown: () => () => {}, onClaudeFront: () => () => {}, plugins: async () => ({}),
      openMail: async () => true,
    };
    window.WebSocket = class { constructor() { setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
  }, [STAGE]);
  await page.route(`${daemon}/**`, async route => {
    const req = route.request(), url = new URL(req.url()), p = url.pathname, post = req.method() === 'POST';
    const json = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    let m;
    if (p === '/inherent/day') return json(day());
    if (p === '/inherent/mail' && !post) return json({ unread: LETTERS });
    if (/^\/inherent\/mail\/[^/]+\/(draft|summary)$/.test(p)) return json({ detail: 'none' }, 404);
    if (!post && (m = p.match(/^\/inherent\/mail\/([^/]+)$/))) {
      const l = LETTERS.find(x => x.id === decodeURIComponent(m[1]));
      return l ? json({ ...l, to: 'me@example.com', text: `The letter itself: ${l.subject}.`, layout: 'text' }) : json({ detail: 'no such letter' }, 404);
    }
    if (p === '/inherent/controls') return json({ mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' });
    if (p === '/inherent/claude-sessions') return json({ sessions: [], error: null });
    if (p === '/inherent/agent-marks') return json({ marks: {} });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });
  await page.clock.setFixedTime(new Date(NOW));
  await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${daemonPort}`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
  const move = async (x, y) => { await page.mouse.move(x, y); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [x, y]); };
  const rect = sel => page.evaluate(sel => { const e = document.querySelector(sel); return e ? e.getBoundingClientRect().toJSON() : null; }, sel);
  const press = async (x, y) => { await move(x, y); await page.mouse.down(); await page.mouse.up(); };
  const pressOn = async sel => { const r = await rect(sel); assert.ok(r, `${sel} is on the page`); await press(r.x + r.width / 2, r.y + r.height / 2); };
  const settle = ms => page.waitForTimeout(ms);
  // the pointer rests inside the pane, on the right column's empty foot (outside it the pane would fold)
  const park = () => move(STAGE - 70, 520);
  const sel = () => page.evaluate(() => document.querySelector('.notch .dt-list [aria-current="true"]')?.dataset.key ?? null);
  const shot = async name => { const g = await rect('.notch-drop'); await page.screenshot({ path: path.join(out, `${name}.png`), clip: { x: 0, y: 0, width: STAGE, height: Math.ceil(g.bottom) + 12 } }); };

  // open the day: a click on the wing's line pins it
  await page.waitForFunction(() => document.querySelector('.notch')?.dataset.line, undefined, { timeout: 8000 });
  const [a0, a1] = (await page.evaluate(() => document.querySelector('.notch').dataset.lineAt)).split('-').map(Number);
  await press((a0 + a1) / 2, 14);
  await page.waitForSelector('.notch-drop.is-open .dt-row', { timeout: 8000 });
  await settle(1200);
  await park();

  // ---- 今天, nothing selected ----
  const g = await page.evaluate(() => { const r = s => document.querySelector(s)?.getBoundingClientRect().toJSON(); return { drop: r('.notch-drop'), inner: r('.notch-pane-in'), dt: r('.notch .dt'), list: r('.notch .dt-list'), side: r('.notch .dt-side'), stage: r('.notch .dt-stage'), head: r('.notch .dt-head'), bar: r('.notch .dt-bar'), doors: r('.notch .dt-doors'), home: !!document.querySelector('.notch .dt-side .ds') }; });
  check('the pane is 820 wide on the stage of 836, centred', Math.abs(g.drop.width - PANE) < 1.5 && Math.abs((g.drop.left + g.drop.right) / 2 - STAGE / 2) < 1.5, JSON.stringify(g.drop));
  check('the day is 584 tall', Math.abs(g.dt.height - 584) < .5, String(g.dt.height));
  check('the header, the bar and the doors span the pane; below the bar the list is at the left and the right column at the right, one hairline between', g.bar.width > 760 && g.doors.width > 300 && g.list.right < g.side.left && g.list.width > 400 && g.list.width < 460 && g.side.width > 320 && Math.abs(g.side.right - g.stage.right) < 1 && g.stage.top > g.bar.bottom - 1 && g.stage.bottom < g.doors.top, JSON.stringify(g));
  check('with nothing selected the right column holds the home slot (DayStage)', g.home);
  check('rows that have a detail end in a chevron', await page.locator('.notch .dt-list .dt-row:not(.is-still)').count() > 3);
  await shot('today-nothing');

  // ---- 今天, a row selected: the right column follows, the list does not move ----
  const before = await rect('.notch .dt-list'), top0 = await page.evaluate(() => document.querySelector('.notch .dt-list').scrollTop);
  await pressOn('.notch .dt-list .dt-row[data-key="work-2"]');
  await settle(500);
  check('a click on a row selects it: ring on the row, its detail in the right column, the home slot gone', await sel() === 'work-2' && await page.locator('.notch .dt-side .dt-pin').count() === 1 && await page.locator('.notch .dt-side .ds').count() === 0);
  check('the list and the pane did not move', Math.abs((await rect('.notch .dt-list')).top - before.top) < .5 && Math.abs((await rect('.notch-drop')).height - g.drop.height) < .5 && await page.evaluate(() => document.querySelector('.notch .dt-list').scrollTop) === top0);
  await park(); await settle(200);
  await shot('today-selected');
  await page.keyboard.press('ArrowUp'); await settle(300);
  const up = await sel();
  check('↑ selects the row above that has a detail, and the right column follows', !!up && up !== 'work-2' && await page.locator('.notch .dt-side .dt-pin').count() === 1, String(up));
  await page.keyboard.press('Escape'); await settle(300);
  const esc = { sel: await sel(), home: await page.locator('.notch .dt-side .ds').count(), open: await page.locator('.notch-drop.is-open').count() };
  check('Esc clears the selection back to the home slot, and the pane stays open', esc.sel === null && esc.home === 1 && esc.open === 1, JSON.stringify(esc));
  await page.keyboard.press('ArrowDown'); await settle(300);
  check('↓ with nothing selected selects a row', await sel() !== null);
  await page.keyboard.press('Tab'); await settle(100);
  check('Tab moves the focus into the right column, and again back to the list', await page.evaluate(() => !!document.activeElement?.closest('.dt-side')));
  await page.keyboard.press('Tab'); await settle(100);
  check('...and back to the list', await page.evaluate(() => document.activeElement?.classList.contains('dt-list')));
  await page.keyboard.press('Escape'); await settle(200);

  // ---- 信, a letter selected ----
  await page.keyboard.press('Control+2'); await settle(700);
  check('⌘2 is the second door, 信', await page.evaluate(() => document.querySelector('.notch .dt')?.dataset.door) === 'mail');
  await page.waitForFunction(() => document.querySelector('.notch .dt-list [aria-current="true"]'), undefined, { timeout: 5000 }).catch(() => {});
  check('on 信 the letters are on the left and the door opens on its rest row (the newest letter that wants a reply): its page is in the right column', await page.locator('.notch .dt-list .dt-row').count() >= 4 && await sel() === 'm-mom' && await page.locator('.notch .dt-side .dt-pin').count() === 1, String(await sel()));
  await pressOn('.notch .dt-list .dt-row[data-key="m-nw"]'); await settle(900);
  check('a letter selected: its page is in the right column', await sel() === 'm-nw' && await page.locator('.notch .dt-side .dt-pin').count() === 1);
  await park(); await settle(200);
  await shot('mail-selected');
  await page.keyboard.press('Control+1'); await settle(600);
  check('⌘1 is back on 今天, the selection gone', await page.evaluate(() => document.querySelector('.notch .dt')?.dataset.door) === 'today' && await sel() === null);
  // ---- the chips and the stretch nothing recorded: the same day with what he was doing laid over it ----
  SPANS = [span('d-sleep', T(0, 55), T(9, 5), { title: 'sleep', category: 'sleep' }), span('d-work', T(10, 30), T(11, 20), { title: 'Jarvis', category: 'work', item_ids: ['work-1'] }), span('d-lunch', T(13, 15), T(13, 55), { title: '吃午饭', category: 'meal', basis: 'stated', confirmed: true }), span('d-hole', T(14, 5), T(14, 50))];
  await page.reload();
  await page.waitForFunction(() => document.querySelector('.notch')?.dataset.line, undefined, { timeout: 8000 });
  const [b0, b1] = (await page.evaluate(() => document.querySelector('.notch').dataset.lineAt)).split('-').map(Number);
  await press((b0 + b1) / 2, 14);
  await page.waitForSelector('.notch-drop.is-open .dt-row', { timeout: 8000 });
  await settle(1200); await park();
  // ---- the chips row: one line, each chip a filter ----
  const chips = () => page.evaluate(() => {
    const box = document.querySelector('.notch .dt-numbers')?.getBoundingClientRect(), cs = [...document.querySelectorAll('.notch .dt-nums .dt-num:not(.is-cut)')].map(e => e.getBoundingClientRect());
    const op = sel => [...document.querySelectorAll(sel)].map(e => +getComputedStyle(e).opacity);
    return { n: cs.length, h: box?.height, tops: new Set(cs.map(r => Math.round(r.top))).size, by: !!document.querySelector('.notch .dt-numbers .dt-by'), card: document.querySelectorAll('.notch .dt-card').length,
      lens: document.querySelector('.notch .dt')?.dataset.lens ?? null, pressed: document.querySelectorAll('.notch .dt-num[aria-pressed="true"]').length,
      marks: document.querySelectorAll('.notch .dt-lm').length, rows: op('.notch .dt-list .dt-row[data-lk~="work"]'), others: op('.notch .dt-list .dt-row:not([data-lk~="work"])'), segs: op('.notch .dt-seg') };
  });
  const c0 = await chips();
  check('the chips are one line (24 px, every chip at one height) with the 在干嘛 / 在哪 toggle at the right, and no lens card', c0.n >= 3 && c0.h <= 26 && c0.tops === 1 && c0.by && c0.card === 0 && c0.lens === null, JSON.stringify(c0));
  const mac = '.notch .dt-numbers .dt-num[data-chip="work"]';
  await move(...(await rect(mac).then(r => [r.x + r.width / 2, r.y + r.height / 2]))); await settle(300);
  const c1 = await chips();
  check('a chip under the pointer previews its filter (nothing pinned)', c1.lens === 'work' && c1.pressed === 0 && c1.marks > 0, JSON.stringify(c1));
  await pressOn(mac); await park(); await settle(400);
  const c2 = await chips();
  check('a click pins it: only the Mac work is lit on the bar, only its rows are lit in the list, the rest step back; no card', c2.lens === 'work' && c2.pressed === 1 && c2.marks > 0 && c2.card === 0 && c2.rows.length > 0 && c2.rows.every(o => o === 1) && c2.others.every(o => o < 1) && c2.segs.every(o => o < .5) && c2.tops === 1, JSON.stringify(c2));
  await shot('today-chip-filter');
  await pressOn(mac); await park(); await settle(400);
  const c3 = await chips();
  check('a second click on the chip clears the filter: everything is lit again', c3.lens === null && c3.pressed === 0 && c3.marks === 0 && c3.others.join() === c0.others.join() && c3.segs.every(o => o === 1), JSON.stringify(c3));
  await pressOn(mac); await park(); await settle(300);
  await page.keyboard.press('Escape'); await settle(400);
  const c4 = await chips();
  check('Esc clears a pinned filter and leaves the pane open', c4.lens === null && c4.pressed === 0 && await page.locator('.notch-drop.is-open').count() === 1, JSON.stringify(c4));
  await shot('today-chip-cleared');

  // too many chips for the line: the last ones are hidden, never wrapped (the strip is squeezed to 300 px here; a full day at 820 does the same with its vitals)
  await page.addStyleTag({ content: '.notch .dt-nums{max-width:300px!important}' }); await settle(300);
  const squeezed = await page.evaluate(() => { const all = [...document.querySelectorAll('.notch .dt-nums > *')], cut = all.filter(e => e.classList.contains('is-cut')), strip = document.querySelector('.notch .dt-nums').getBoundingClientRect(); return { all: all.length, cut: cut.length, tops: new Set(all.map(e => Math.round(e.getBoundingClientRect().top))).size, shownInside: all.filter(e => !e.classList.contains('is-cut')).every(e => e.getBoundingClientRect().right <= strip.right + .5) }; });
  check('chips that do not fit are hidden from the end, on the same line', squeezed.cut > 0 && squeezed.cut < squeezed.all && squeezed.tops === 1 && squeezed.shownInside, JSON.stringify(squeezed));
  await shot('today-chips-squeezed');
  await page.addStyleTag({ content: '.notch .dt-nums{max-width:none!important}' }); await settle(300);

  // ---- the stretch nothing recorded: a click on the dashed "?" asks about that span ----
  const hole = await rect('.notch .dt-seg.is-hole');
  check('a stretch nothing recorded is a dashed box with a question mark on the bar', !!hole && await page.locator('.notch .dt-seg.is-hole svg').count() > 0);
  await press(hole.x + hole.width / 2, hole.y + hole.height / 2); await settle(700);
  const ask = await page.evaluate(() => ({ dock: !!document.querySelector('.notch .dt-dock'), range: document.querySelector('.notch .dt-about.is-range time')?.textContent ?? '' }));
  check('a click on it opens the 这段在干嘛 input with that span as its chip', ask.dock && /^\d\d:\d\d–\d\d:\d\d$/.test(ask.range), JSON.stringify(ask));
  await shot('today-hole-ask');
  await page.keyboard.press('Escape'); await settle(300);
  await page.keyboard.press('Escape'); await settle(300);
  await park();

  check('no errors on the page', errors.length === 0, errors.join(' | '));
  await ctx.close();
} finally {
  await browser.close();
  server.kill();
}

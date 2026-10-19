// The notch's right side is a module (ADR 0296): the stars as before (`marks`), or the day's timeline (the default): the turn mark
// alone, one line (`家 · 3:12`, or the way he is going), the pinned trip. Resting on the line, or a click on it, drops the day from
// the island as the Dashboard comes: rest opens it, a click pins it, leaving folds it, Esc folds it, and the two never hang together.
// Headless Chrome against the built page with the native bridge stubbed and a fake daemon that serves /inherent/day.
// Run after `npm run build`. Screenshots land in evidence/notch-timeline/ (COMPANION_EVIDENCE_DIR moves them).
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { annotate, buildDay, clock, dateTitle, fnv, shiftDate, wingText } from '../src/dayView.ts';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = process.env.COMPANION_EVIDENCE_DIR ?? path.join(root, 'evidence/notch-timeline');
mkdirSync(dir, { recursive: true });
const web = Number(process.env.COMPANION_PORT ?? 5199), daemonPort = Number(process.env.COMPANION_DAEMON_PORT ?? 8797), daemon = `http://127.0.0.1:${daemonPort}`;
const checks = [], check = (name, pass, detail = '') => { assert.ok(pass, `${name} ${detail}`); checks.push(name); console.log('ok', name); };

// ---------- a realistic day: 2026-10-19 in Vancouver (PDT, UTC-7), now 21:50 ----------
const DATE = '2026-10-19', TZ = 'America/Vancouver';
const T = (h, m = 0, day = 19) => Date.UTC(2026, 9, day, h + 7, m);
const NOW = T(21, 50);
const it = (id, kind, start, end, extra = {}, lane = 'you') => ({ id, lane, kind, start_ms: start, end_ms: end, state: end === null ? (start > NOW ? 'next' : 'now') : end <= NOW ? 'done' : start > NOW ? 'next' : 'now', ...extra });
const home = { id: 'p-home', name: '家', role: 'home' }, uvic = { id: 'p-uvic', name: 'UVic', role: 'school' };
const makeDay = (over = {}) => ({
  date: DATE, tz: TZ, start_ms: T(0), end_ms: T(24), now_ms: NOW,
  items: [
    it('sleep-1', 'sleep', T(0, 55), T(9, 5), { minutes: 470 }),
    it('stay-home-1', 'stay', T(9, 5), T(11, 25), { place: '12 Example St, Victoria', kept_place: home }),
    it('reminder-2', 'reminder', T(9, 30), null, { text: '给银行打电话', fired: true, acknowledged: true }, 'her'),
    it('talk-1', 'talk', T(10, 2), T(10, 9), { turns: 6 }, 'her'),
    it('move-1', 'move', T(11, 25), T(13, 5), { activity: 'automotive' }),
    it('move-2', 'move', T(13, 5), T(13, 15), { activity: 'walking' }),
    it('stay-aw', 'stay', T(13, 15), T(13, 55), { place: 'A&W, 1234 Douglas St, Victoria' }),
    it('picture-1', 'picture', T(13, 30), T(13, 30), { attachment: 'a'.repeat(32), note: '汉堡和薯条\n在 A&W 吃的午饭' }),
    it('move-3', 'move', T(13, 55), T(14, 5), { activity: 'walking' }),
    it('stay-home-2', 'stay', T(14, 5), T(14, 50), { place: '12 Example St, Victoria', kept_place: home }),
    it('ride-1', 'ride', T(14, 50), T(15, 19), { route: '27', board_stop: 'Shelbourne at Pear', alight_stop: 'UVic Exchange', source: 'tapped', ended_reason: 'left_vehicle', credit: null }),
    it('talk-3', 'talk', T(14, 55), T(14, 55), { turns: 1 }, 'her'),
    it('stay-uvic', 'stay', T(15, 19), T(18, 15), { place: 'University of Victoria, Victoria', kept_place: uvic }),
    it('ride-2', 'ride', T(18, 15), T(18, 50), { route: '27', board_stop: 'UVic Exchange', alight_stop: 'Shelbourne at Pear', source: 'tapped', ended_reason: 'left_vehicle', credit: null }),
    it('stay-home-3', 'stay', T(18, 50), null, { place: '12 Example St, Victoria', kept_place: home }),
    it('talk-2', 'talk', T(19, 30), T(19, 36), { turns: 4 }, 'her'),
    it('reminder-1', 'reminder', T(22, 0), null, { text: '把垃圾拿出去', fired: false, acknowledged: false }, 'her'),
    it('departure-1', 'departure', T(22, 40), T(22, 40), { title: '27 · Shelbourne at Pear', route: '27', board_stop: 'Shelbourne at Pear', to: 'UVic', leave_at_ms: T(22, 33) }),
    it('event-1', 'event', T(22, 30), T(23, 30), { title: '小组讨论', place: 'Library', private: false }),
    it('event-2', 'event', T(23, 40), T(23, 55), { title: 'Dr. appointment', source: 'said', confirmed: true, private: true, place: 'Clinic' }),
  ],
  labels: [{ title: '期中周', source: 'calendar' }], missing: ['mac'],
  line: { text: '今天一半在路上，一半在 UVic。晚上回家后，和你聊了两次。', draft: false, written_at: '2026-10-19T21:30:00-07:00' },
  highlights: [{ id: 'stay-uvic', label: '下午在 UVic', by: 'her' }],
  ...over,
});
// The day before: two stays and a ride, no sentence yet.
const YESTERDAY = '2026-10-18';
const yesterday = () => ({
  date: YESTERDAY, tz: TZ, start_ms: T(0, 0, 18), end_ms: T(24, 0, 18), now_ms: NOW,
  items: [
    { id: 'y-sleep', lane: 'you', kind: 'sleep', start_ms: T(1, 10, 18), end_ms: T(8, 40, 18), state: 'done', minutes: 450 },
    { id: 'y-home', lane: 'you', kind: 'stay', start_ms: T(8, 40, 18), end_ms: T(24, 0, 18), state: 'done', place: '12 Example St, Victoria', kept_place: home },
  ],
  labels: [], missing: [], line: null, highlights: [],
});

// ---------- the model, in node ----------
const zh = l => l[1], en = l => l[0];
const day = makeDay();
check('a place keeps its hue: FNV-1a of its id, as on the phone', fnv('a') === 0xE40C292C);
check('the wing says where he is and for how long', JSON.stringify(wingText(day, NOW, zh)) === '{"head":"家","tail":"3:00"}');
check('the wing in English', JSON.stringify(wingText(day, NOW, en)) === '{"head":"家","tail":"3:00"}');
check('on a bus ride, its words and minutes', JSON.stringify(wingText(day, T(14, 57), zh)) === '{"head":"27 路","tail":"7 分"}' && wingText(day, T(14, 57), en).head === 'Bus 27');
check('walking, in words and minutes', JSON.stringify(wingText(day, T(13, 11), zh)) === '{"head":"步行","tail":"6 分"}');
check('driving, in words and minutes', JSON.stringify(wingText(day, T(12, 5), zh)) === '{"head":"开车","tail":"40 分"}');
check('a place he did not name has no name on the wing', wingText(makeDay({ items: [it('s', 'stay', T(20, 0), null, {})] }), NOW, zh).head === '停留');
check('with nothing under now, 今天', JSON.stringify(wingText(makeDay({ items: [] }), NOW, zh)) === '{"head":"今天","tail":""}' && wingText(null, NOW, zh).head === '今天');
check('a stay from yesterday reads hours past 24', wingText(makeDay({ items: [it('s', 'stay', T(19, 45, 18), null, { place: 'X' })] }), NOW, zh).tail === '26:05');
check('dates are named from their own digits', dateTitle(DATE, zh) === '10月19日 周一' && dateTitle(DATE, en) === 'Mon, Oct 19' && shiftDate(DATE, -19) === '2026-09-30');

const flat = (blocks, out = []) => { for (const b of blocks) { if (b.type === 'row') out.push({ ...b.row, as: 'row' }); else if (b.type === 'now') out.push({ title: b.label, as: 'now' }); else { out.push({ title: b.head.name, detail: b.head.star ?? '', when: b.head.when, dur: b.head.dur, as: 'stay', rgb: b.head.rgb, unnamed: b.head.unnamed }); flat(b.items, out); } } return out; };
const model = buildDay(day, NOW, zh), rows = flat(model.blocks);
const row = title => rows.find(r => r.title === title);
check('the day is titled, with her sentence, the label and the missing source', model.title === '10月19日 周一' && model.today && model.line.text.startsWith('今天一半在路上') && model.chips[0].text === '期中周' && model.foot === 'Mac 的时间还没进来');
check('stays are named by the kept place, else the phone\'s first part', rows.filter(r => r.as === 'stay').map(r => r.title).join() === '家,A&W,家,UVic,家');
check('home is warm, school is blue, an unnamed-by-him stay is the neutral haze', row('家').rgb.join() === '255,168,150' && row('UVic').rgb.join() === '100,200,255' && row('A&W').rgb.join() === '196,204,238');
check('a stay with nothing to call it says so', buildDay(makeDay({ items: [it('s', 'stay', T(20, 0), null, {})] }), NOW, zh).blocks[0].head.name === '没名字的地方');
check('durations read 40 分 and 1 时 40 分', row('开车 · 1 时 40 分') && row('步行 · 10 分') && rows.find(r => r.as === 'stay' && r.title === 'UVic').dur === '2 时 56 分' && rows.find(r => r.as === 'stay' && r.title === 'A&W').dur === '40 分');
check('times are HH:mm in the day\'s zone', rows.find(r => r.as === 'stay' && r.title === 'UVic').when === '15:19–18:15' && rows.find(r => r.as === 'stay' && r.title === '家').when === '09:05–11:25');
check('a bus ride is a connector with its stops', (r => r.connector && r.detail === 'Shelbourne at Pear → UVic Exchange' && r.icon === 'bus')(row('27 路 · 29 分')));
check('a talk is 和她说话 with its lines; a photo is 照片 with its first line', row('和她说话').detail === '6 句' && row('照片').detail === '汉堡和薯条');
check('a private event is 私事 and says no more; a reminder is its text', row('私事').detail === '' && row('小组讨论').detail === 'Library' && row('给银行打电话') && row('把垃圾拿出去').future);
check('her highlight stars the stay and gives her words', row('UVic').detail === '下午在 UVic' && row('家').detail === '');
check('the now line sits in the stay he is in, after what has begun and before what is to come', (i => i > 0 && rows[i - 1].title === '和她说话' && rows[i + 1].title === '把垃圾拿出去' && rows[i + 1].future)(rows.findIndex(r => r.as === 'now')) && row('现在 21:50'));
check('the overview bar spans the day and its now tick is at 21:50', model.bar.ticks.map(t => t.label).join() === '06,12,18,24' && Math.abs(model.bar.nowF - (21 * 60 + 50 - 55) / (24 * 60 - 55)) < .01);
{
  // Mac time (a work block, a call) comes with a Mac that has reported, which the realistic day above has not.
  const mac = buildDay(makeDay({ missing: [], items: [it('work-1', 'work', T(15, 30), T(17, 40), { active_s: 7800, projects: [{ name: 'jarvis', seconds: 5000 }], apps: [{ name: 'Ghostty', seconds: 3000 }] }), it('call-1', 'call', T(19, 0), T(19, 25), { app: 'Zoom' })] }), NOW, zh);
  const r = flat(mac.blocks);
  check('Mac time: a work block says its projects and apps and how long it was active; a call its app', r.find(x => x.title === '在 Mac 上工作')?.detail === 'jarvis · Ghostty · 活跃 2 时 10 分' && r.some(x => x.title === '通话 · Zoom') && mac.foot === '');
}
{
  const odd = makeDay({ items: [it('bad-1', 'stay', NaN, null, { place: 'X' }), it('bad-2', 'move', T(21, 0), 'later', { activity: 'walking' }), it('unknown-1', 'weather', T(20, 0), null, {}), it('ok-1', 'stay', T(20, 0), null, { place: 'Y' })] });
  const r = flat(buildDay(odd, NOW, zh).blocks).filter(x => x.as !== 'now');
  check('an item the pane cannot read (no time, a time that is not a number, a kind it does not know) is left out, never a crash', r.length === 1 && r[0].title === 'Y' && wingText(odd, NOW, zh).head === 'Y');
}
{
  // A day the clocks cut short or stretch: the bar's hour marks still sit at their local hours.
  const stay = start => ({ id: 's', lane: 'you', kind: 'stay', start_ms: start, end_ms: null, state: 'now', place: 'X', kept_place: home });
  const spring = { date: '2026-03-08', tz: TZ, start_ms: Date.UTC(2026, 2, 8, 8), end_ms: Date.UTC(2026, 2, 9, 7), items: [stay(Date.UTC(2026, 2, 8, 8))], labels: [], missing: [], line: null, highlights: [] };
  const fall = { ...spring, date: '2026-11-01', start_ms: Date.UTC(2026, 10, 1, 7), end_ms: Date.UTC(2026, 10, 2, 8), items: [stay(Date.UTC(2026, 10, 1, 7))] };
  for (const [name, d, hours] of [['a 23-hour day', spring, 23], ['a 25-hour day', fall, 25]]) {
    const bar = buildDay(d, d.start_ms + 12 * 3_600_000, zh).bar, at = k => clock(Math.round((d.start_ms + k.f * (d.end_ms - d.start_ms)) / 60_000) * 60_000, TZ);
    check(`on ${name} the bar's hour marks sit at their local hours`, (d.end_ms - d.start_ms) / 3_600_000 === hours && bar.ticks.map(at).join() === '06:00,12:00,18:00,00:00', bar.ticks.map(at).join());
  }
}
const fits = annotate(model.bar.segs, 488, s => s.length * 7);
check('pieces too short to name are counted, not drawn as slivers', fits.drawn.every(d => d.w >= 8) && fits.merged.length > 0 && fits.drawn.some(d => d.label));
check('no dot or bar is amber: amber is the turn mark\'s', [...rows.filter(r => r.rgb), ...model.bar.segs].every(x => !(x.rgb[0] > 240 && x.rgb[1] > 170 && x.rgb[1] < 200 && x.rgb[2] < 100)));

// ---------- the built page ----------
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security', '--mute-audio'] });
const errors = [];
const NOTCH_R = 412.5, TOP = 32;
// A page on a fake daemon: `served` is what /inherent/day answers, `board` the sessions, `departure` the pinned trip, `hits` the day reads it got.
async function boot({ wing, marks, lang = 'zh', openBy, board = [], served = makeDay(), departure = null } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const s = { board, served, departure, hits: [], controls: { mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' } };
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
  await page.addInitScript(([wardrobe, settings]) => {
    if (wardrobe) localStorage.setItem('companion-wardrobe-v1', JSON.stringify(wardrobe));
    localStorage.setItem('companion-settings-v1', JSON.stringify(settings));
    window.__audio = 0;
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {}, wearing: () => {}, onDictation: () => () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, onGhostty: () => () => {}, onAgentsPresence: cb => { window.__presence = cb; return () => {}; }, onMouseDown: cb => { window.__down = cb; return () => {}; }, onClaudeFront: cb => { window.__front = cb; return () => {}; }, plugins: async () => ({}),
    };
    window.__sockets = [];
    window.WebSocket = class { constructor() { window.__sockets.push(this); setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
    window.__emit = (op, payload) => window.__sockets.at(-1).onmessage({ data: JSON.stringify({ op, payload }) });
  }, [wing || marks ? { ...wing && { wing }, ...marks && { marks } } : null, { lang, ...(openBy ? { openBy } : {}) }]);
  await page.route(`${daemon}/**`, route => {
    const url = new URL(route.request().url());
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    if (url.pathname === '/inherent/day') {
      s.hits.push(url.search);
      const date = url.searchParams.get('date');
      return json(!date || date === DATE ? s.served : date === YESTERDAY ? yesterday() : { ...yesterday(), date, items: [] });
    }
    if (url.pathname === '/inherent/controls') return json(s.controls);
    if (url.pathname === '/inherent/claude-sessions') return json({ sessions: s.board, error: null });
    if (url.pathname === '/inherent/notices' && s.departure) return json({ notices: [], departure: s.departure });
    if (url.pathname === '/inherent/agent-marks') return json({ marks: {} });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });
  await page.clock.setFixedTime(new Date(NOW));
  await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${daemonPort}`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
  const move = async (x, y) => { await page.mouse.move(x, y); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [x, y]); };
  // A rest: the native feed sends a point when it changes, so a resting pointer is one sample, then silence.
  const glide = async (from, to, ms) => { const steps = Math.max(2, Math.round(ms / 16)); for (let i = 1; i <= steps; i++) { await move(from.x + (to.x - from.x) * i / steps, from.y + (to.y - from.y) * i / steps); await page.waitForTimeout(16); } };
  const read = () => page.evaluate(() => {
    const n = document.querySelector('.notch'), drop = n.querySelector('.notch-drop');
    return { wing: n.dataset.wing, line: n.dataset.line, at: n.dataset.lineAt, marks: n.dataset.marks, counts: n.dataset.counts, tl: n.dataset.tl, pin: n.dataset.pin,
      dropOpen: drop.classList.contains('is-open'), dt: !!drop.querySelector('.dt'), list: !!drop.querySelector('.a-list'), dash: !!document.querySelector('.companion-dashboard.is-open'),
      card: !!document.querySelector('.notch-note.is-open .nc'), hit: (r => ({ left: r.left, right: r.right }))(n.querySelector('.notch-hit').getBoundingClientRect()) };
  });
  const until = (fn, arg, timeout = 4000) => page.waitForFunction(fn, arg, { timeout });
  const lineX = async () => { const { at } = await read(); const [a, b] = at.split('-').map(Number); return { x0: a, x1: b, mid: (a + b) / 2 }; };
  return { page, ctx, s, move, glide, read, until, lineX, far: { x: 600, y: 700 }, shot: (name, clip) => page.screenshot({ path: path.join(dir, `${name}.png`), clip }) };
}

try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const session = (id, phase, extra = {}) => ({ agent: 'claude', session_id: id, kind: 'interactive', phase, title: `Session ${id}`, project: 'jarvis', branch: 'main', cwd: '/x', where: 'Ghostty', prompt: 'wire it', activity: 'Wants to run npm test', last_message: 'Needs your decision', started_ms: NOW - 60_000, updated_ms: NOW, request: null, from_project: false, compacting: false, error: '', ...extra });
  const ask = id => session(id, 'needs_input'), working = id => session(id, 'working');
  // A session is first seen working, then asks: the way a real one arrives.
  const arrive = async (a, ...rows) => { a.s.board = rows.map(r => working(r.session_id)); await a.page.waitForTimeout(1800); a.s.board = rows; };
  const line = async a => { await a.until(() => document.querySelector('.notch')?.dataset.line === '家 · 3:00'); return a.lineX(); };
  const tl = a => a.read().then(r => r.tl);
  // Polls until `ok(read)` holds and returns the last read: the frame loop, React and the spring each take their turn, slowly on a busy machine.
  const seen = async (a, ok, ms = 4000) => { const end = Date.now() + ms; let r; do { r = await a.read(); if (ok(r)) return r; await a.page.waitForTimeout(40); } while (Date.now() < end); return r; };
  // The day's state is `want` ('' folded, 'open', 'pinned'); open or pinned means the day is mounted and the sessions are gone.
  const settle = (a, want, ms = 3000) => seen(a, r => r.tl === want && (want === '' || r.dt && !r.list), ms);

  // ---- the setting: timeline by default, stars when chosen ----
  {
    const a = await boot();
    await line(a);
    const r = await a.read();
    check('with nothing chosen the wing is the timeline: where he is and for how long', r.wing === 'timeline' && r.line === '家 · 3:00' && r.marks === 'line', JSON.stringify(r));
    check('the wing is at least 64 pt wide', r.hit.right - r.hit.left >= 63.5);
    await a.shot('01-wing-closed', { x: 200, y: 0, width: 300, height: 60 });
    await a.ctx.close();
  }
  {
    const a = await boot({ wing: 'marks', board: [working('w-1'), ask('a-1')] });
    await a.page.evaluate(() => window.__front(true));
    await a.until(() => /turn1/.test(document.querySelector('.notch')?.dataset.marks ?? ''));
    const r = await a.read();
    check('the stars are the wing when chosen: the marks as before, no line', r.wing === 'marks' && !r.line && r.marks === 'turn1 work1', JSON.stringify(r));
    await a.shot('02-stars-wing', { x: 200, y: 0, width: 300, height: 60 });
    await a.move(412.5 + 14, 14);
    const open = await seen(a, r => r.dropOpen && r.list);
    check('in the stars a rest on a mark still opens the sessions', open.dropOpen && open.list && !open.dt && open.tl === '', JSON.stringify(open));
    await a.ctx.close();
  }
  {
    // The turn mark is the only one the timeline wing keeps, and it opens the sessions on a click, never on a rest.
    const a = await boot({ board: [working('w-1'), ask('a-1')] });
    await a.page.evaluate(() => window.__front(true));
    await a.until(() => document.querySelector('.notch')?.dataset.marks === 'turn1 line');
    await a.until(() => document.querySelector('.notch')?.dataset.line === '家 · 3:00');
    const r = await a.read(), seg = await a.lineX(), turn = { x: (r.hit.left + seg.x0) / 2, y: 14 };
    check('beside the line only the turn mark stays: working, finished and parked are not shown', r.marks === 'turn1 line' && r.counts === 'turn1', JSON.stringify(r));
    await a.shot('03-turn-and-line', { x: 200, y: 0, width: 300, height: 60 });
    await a.glide(a.far, turn, 250); await a.page.waitForTimeout(900);
    let n = await a.read();
    check('a rest on the turn mark opens nothing', !n.dropOpen && n.tl === '');
    await a.page.mouse.down(); await a.page.mouse.up();
    n = await seen(a, r => r.dropOpen && r.list && !r.dt);
    check('a click on the turn mark opens today\'s sessions panel, all of them', n.dropOpen && n.list && !n.dt && await a.page.locator('.notch-drop .a-row').count() === 2, JSON.stringify(n));
    await a.glide(turn, { x: seg.mid, y: 14 }, 120);
    n = await settle(a, 'open');
    check('a rest on the line brings the day, and the sessions leave: never both', n.tl === 'open' && n.dt && !n.list, JSON.stringify(n));
    await a.glide({ x: seg.mid, y: 14 }, turn, 120);
    await a.page.mouse.down(); await a.page.mouse.up();
    n = await seen(a, r => r.tl === '' && r.list && !r.dt);
    check('a click on the turn mark takes the sessions back, and the day goes', n.tl === '' && n.list && !n.dt, JSON.stringify(n));
    await a.ctx.close();
  }

  // The pinned trip keeps its place after the line, and a rest or a click on it never brings the day.
  {
    const trip = { id: 'dep-1', route: '27', board_stop: 'Shelbourne at Pear', leave_at_ms: NOW + 12 * 60_000, departs_at_ms: NOW + 15 * 60_000, leave_at: '22:02', departs: '22:05', arrive_at: '22:40', to: 'school', stale: false };
    const a = await boot({ departure: trip });
    await a.until(() => { const n = document.querySelector('.notch'); return n?.dataset.marks === 'line pin' && n.dataset.line === '家 · 3:00' && n.dataset.pin; }, undefined, 8000);
    const r = await a.read(), seg = await a.lineX(), pill = { x: seg.x1 + 5 + 14, y: 14 };
    check('the pinned trip is a pill after the line, in the same wing', r.marks === 'line pin' && /27 · 12 分/.test(r.pin) && r.hit.right >= seg.x1 + 5 + 60, JSON.stringify({ ...r, seg }));
    await a.shot('11-line-and-pill', { x: 200, y: 0, width: 400, height: 60 });
    await a.glide(a.far, pill, 250);
    await a.until(() => document.querySelector('.notch-pin-tip')?.classList.contains('is-on'), undefined, 3000);
    await a.page.waitForTimeout(700);
    let n = await a.read();
    check('a rest on the pill shows its trip and does not bring the day', n.tl === '' && !n.dropOpen);
    await a.page.mouse.down(); await a.page.mouse.up(); await a.page.waitForTimeout(700);
    n = await a.read();
    check('a click on the pill does not bring the day either', n.tl === '' && !n.dt);
    await a.ctx.close();
  }
  // The turn mark's other two looks (Settings › Agent marks) sit before the line just the same.
  for (const look of ['spark', 'pixel']) {
    const a = await boot({ marks: look, board: [ask('a-1')] });
    await a.page.evaluate(() => window.__front(true));
    await a.until(() => document.querySelector('.notch')?.dataset.marks === 'turn1 line' && document.querySelector('.notch')?.dataset.line === '家 · 3:00');
    const r = await a.read(), seg = await a.lineX();
    check(`the ${look} look: the turn mark, then the line, inside the wing`, r.marks === 'turn1 line' && seg.x0 >= r.hit.left + 26 && seg.x1 <= r.hit.right - 3, JSON.stringify({ ...r, seg }));
    await a.shot(`10-${look}-look`, { x: 200, y: 0, width: 330, height: 60 });
    await a.ctx.close();
  }

  // ---- Settings › Her look › Right of the notch ----
  {
    const a = await boot();
    await line(a);
    await a.move(320, 14);
    await a.page.locator('.companion-dashboard.is-open').waitFor();
    await a.move(320, 200); await a.page.waitForTimeout(900);
    await a.page.locator('.ad .corner [data-row="settings"]').click(); await a.page.waitForTimeout(700);
    await a.page.locator('.ad [data-cat="look"]').click(); await a.page.waitForTimeout(500);
    const item = a.page.locator('.ad [data-item="wing"]'), wardrobe = () => a.page.evaluate(() => JSON.parse(localStorage.getItem('companion-wardrobe-v1') ?? '{}'));
    check('Settings › Her look offers 刘海右边: 星星 or 时间线, next to the marks\' look', (await item.innerText()).includes('刘海右边') && (await item.locator('button').allInnerTexts()).join() === '星星,时间线' && await a.page.locator('.ad [data-item="wing"] + [data-item="marks"]').count() === 1);
    await item.locator('button', { hasText: '星星' }).click();
    await a.until(() => document.querySelector('.notch')?.dataset.wing === 'marks');
    check('choosing 星星 puts the stars back, and the choice is kept', (await wardrobe()).wing === 'marks' && !(await a.read()).line);
    await item.locator('button', { hasText: '时间线' }).click();
    await a.until(() => document.querySelector('.notch')?.dataset.wing === 'timeline' && document.querySelector('.notch')?.dataset.line === '家 · 3:00');
    check('and 时间线 brings the line back', (await wardrobe()).wing === 'timeline');
    await a.ctx.close();
  }

  // ---- the line follows the clock and the day ----
  {
    const a = await boot();
    await line(a);
    await a.page.clock.setFixedTime(new Date(NOW + 60_000));
    await a.until(() => document.querySelector('.notch')?.dataset.line === '家 · 3:01');
    check('a minute later the line says 3:01', true);
    await a.page.clock.setFixedTime(new Date(T(14, 57)));
    await a.until(() => document.querySelector('.notch')?.dataset.line === '27 路 · 7 分');
    check('on the bus the line is its words and minutes', true);
    await a.page.clock.setFixedTime(new Date(T(13, 11)));
    await a.until(() => document.querySelector('.notch')?.dataset.line === '步行 · 6 分');
    check('on foot, walking and minutes', true);
    await a.ctx.close();
    const e = await boot({ lang: 'en' });
    await e.page.clock.setFixedTime(new Date(T(14, 57)));
    await e.until(() => document.querySelector('.notch')?.dataset.line === 'Bus 27 · 7 min');
    check('and in English', true);
    await e.ctx.close();
    const long = await boot({ served: makeDay({ items: [it('s', 'stay', T(18, 50), null, { place: 'x', kept_place: { id: 'p-long', name: 'Mayfair Mall Shopping Centre Victoria', role: 'other' } })] }) });
    await long.until(() => /…/.test(document.querySelector('.notch')?.dataset.line ?? ''));
    const r = await long.read(), [x0, x1] = r.at.split('-').map(Number);
    check('a long place gives way to the minutes: cut with …, the line at most 112 pt', r.line.endsWith('… · 3:00') && x1 - x0 <= 112, JSON.stringify(r));
    await long.shot('09-long-place', { x: 300, y: 0, width: 340, height: 60 });
    await long.ctx.close();
  }

  // ---- the day's pane summons and dismisses as the Dashboard does ----
  {
    const a = await boot();
    const seg = await line(a), on = { x: seg.mid, y: 14 };
    await a.glide(a.far, on, 300);
    await a.page.waitForTimeout(100);
    check('a rest does not open it before the dwell', await tl(a) === '');
    let r = await settle(a, 'open');
    check('resting on the line opens the day, unpinned', r.tl === 'open' && r.dt && !r.list, JSON.stringify(r));
    await a.until(() => document.querySelector('.notch-drop')?.classList.contains('is-open') && document.querySelector('.dt-date'));
    await a.page.waitForTimeout(500);
    await a.shot('04-pane-open', { x: 40, y: 0, width: 560, height: 700 });
    await a.page.mouse.down(); await a.page.mouse.up();
    check('a click on an open day pins it', (await settle(a, 'pinned', 2000)).tl === 'pinned');
    await a.glide(on, a.far, 120);
    await a.page.waitForTimeout(300);
    check('leaving does not fold it at once', await tl(a) === 'pinned');
    r = await settle(a, '', 2500);
    check('leaving the line and the pane folds it after the grace', r.tl === '');
    await a.until(() => !document.querySelector('.notch-drop')?.classList.contains('is-open'));

    await a.move(on.x, on.y); await a.page.mouse.down(); await a.page.mouse.up();
    check('a click opens it pinned at once, without the dwell', (await settle(a, 'pinned', 2000)).tl === 'pinned');
    await a.page.mouse.down(); await a.page.mouse.up();
    check('a click on a pinned day closes it', (await settle(a, '', 2000)).tl === '');
    await a.page.waitForTimeout(1200);
    check('and a rest on the line after that click does not open it again', await tl(a) === '');
    await a.glide(on, a.far, 120); await a.page.waitForTimeout(300);
    await a.glide(a.far, on, 200); r = await settle(a, 'open');
    check('after the pointer has been away a rest opens it again', r.tl === 'open');

    // Esc
    await a.page.keyboard.press('Escape');
    r = await settle(a, '', 1500);
    check('Esc folds it', r.tl === '');
    await a.glide(on, a.far, 120); await a.page.waitForTimeout(300);

    // Heading to the pane, and a press that began in it, hold it.
    await a.glide(a.far, on, 200); await settle(a, 'open');
    await a.until(() => document.querySelector('.notch-drop')?.classList.contains('is-open'));
    await a.glide(on, { x: 330, y: 260 }, 500);
    await a.page.waitForTimeout(1300);
    check('going down into the pane keeps it open', await tl(a) === 'open');
    await a.page.mouse.down();
    await a.glide({ x: 330, y: 260 }, a.far, 200); await a.page.waitForTimeout(1500);
    check('a press that began in the pane holds it, though the pointer went far', await tl(a) === 'open');
    await a.page.mouse.up();
    r = await settle(a, '', 2500);
    check('and once it is let go the pane folds after the grace', r.tl === '');
    await a.ctx.close();
  }
  {
    const a = await boot({ openBy: 'click' });
    const seg = await line(a), on = { x: seg.mid, y: 14 };
    await a.glide(a.far, on, 300); await a.page.waitForTimeout(1200);
    check('with the Dashboard set to open on a click, a rest on the line opens nothing', await tl(a) === '');
    await a.page.mouse.down(); await a.page.mouse.up();
    check('and a click opens it', (await settle(a, 'pinned', 2000)).tl === 'pinned');
    await a.ctx.close();
  }

  // ---- never together with the Dashboard ----
  {
    const a = await boot();
    const seg = await line(a), on = { x: seg.mid, y: 14 }, notch = { x: 320, y: 14 };
    const dash = () => a.read().then(r => r.dash);
    await a.glide(a.far, on, 200); await settle(a, 'open');
    await a.glide(on, notch, 120);
    const end = Date.now() + 3000;
    while (Date.now() < end && !await dash()) await a.page.waitForTimeout(50);
    await a.page.waitForTimeout(500);
    let r = await a.read();
    check('opening the Dashboard folds the day', r.dash && r.tl === '', JSON.stringify(r));
    await a.shot('05-dashboard-open', { x: 40, y: 0, width: 560, height: 520 });
    // The wing steps aside only where the Dashboard leaves no room: with this short line it stays, so a click on it is a hand-over.
    await a.glide(notch, on, 90);
    await a.page.waitForTimeout(40);
    await a.page.mouse.down(); await a.page.mouse.up();
    r = await settle(a, 'pinned', 2000);
    await a.page.waitForTimeout(800);
    r = await a.read();
    check('a click on the line while the Dashboard hangs folds it and opens the day, pinned', r.tl === 'pinned' && !r.dash && r.dt, JSON.stringify(r));
    await a.glide(on, a.far, 100); await a.page.waitForTimeout(2200);

    // A rest on the line while the Dashboard is held up (a press that began in it) does nothing.
    await a.glide(a.far, notch, 200);
    const gone = Date.now() + 3000;
    while (Date.now() < gone && !await dash()) await a.page.waitForTimeout(50);
    await a.page.waitForTimeout(600);
    await a.move(320, 200); await a.page.mouse.down();
    await a.glide({ x: 320, y: 200 }, on, 300); await a.page.waitForTimeout(1200);
    r = await a.read();
    check('while the Dashboard hangs a rest on the line does nothing', r.dash && r.tl === '', JSON.stringify(r));
    await a.page.mouse.up();
    await a.ctx.close();
  }

  // ---- the room beside the notch, and the quiet rules ----
  {
    // The Dashboard leaves 87.5 pt beside the notch: a line too long for it folds the whole wing, as four marks would (ADR 0187).
    const a = await boot({ served: makeDay({ items: [it('s', 'stay', T(18, 50), null, { place: 'x', kept_place: { id: 'p-long', name: 'Mayfair Mall Shopping Centre Victoria', role: 'other' } })] }) });
    await a.until(() => /…/.test(document.querySelector('.notch')?.dataset.line ?? ''));
    const before = await a.read();
    await a.move(320, 14);
    await a.page.locator('.companion-dashboard.is-open').waitFor();
    await a.move(320, 200);
    const folded = await seen(a, r => r.hit.right - r.hit.left < 1);
    check('where the Dashboard leaves no room the wing folds, line and all', before.hit.right - before.hit.left > 100 && folded.hit.right - folded.hit.left < 1, JSON.stringify({ before: before.hit, folded: folded.hit }));
    await a.move(a.far.x, a.far.y);
    const back = await seen(a, r => r.hit.right - r.hit.left > 100, 6000);
    check('and the wing comes back when the Dashboard folds', back.hit.right - back.hit.left > 100, JSON.stringify(back.hit));
    await a.ctx.close();
  }
  {
    // While Startrail's window is in front nothing shows beside the notch (ADR 0153), the day included.
    const a = await boot();
    const seg = await line(a), on = { x: seg.mid, y: 14 };
    await a.page.evaluate(() => window.__presence({ active: true, ids: [] }));
    await a.glide(a.far, on, 200); await a.page.waitForTimeout(1000);
    check('with the Agents window in front a rest on the line brings nothing', await tl(a) === '');
    await a.page.mouse.down(); await a.page.mouse.up(); await a.page.waitForTimeout(500);
    check('and a click brings nothing either', await tl(a) === '');
    await a.page.evaluate(() => window.__presence({ active: false, ids: [] }));
    await a.page.mouse.down(); await a.page.mouse.up();
    check('once it is away a click brings the day', (await settle(a, 'pinned', 2000)).tl === 'pinned');
    await a.ctx.close();
  }

  // ---- notices keep working, and wait while the day is open ----
  {
    const a = await boot();
    const seg = await line(a), on = { x: seg.mid, y: 14 };
    await arrive(a, ask('c-1'));
    await a.until(() => document.querySelector('.notch-note.is-open .nc'), undefined, 8000);
    check('a card still drops from the notch in the timeline wing', await a.page.locator('.notch-note.is-open .nc').count() === 1);
    await a.glide(a.far, on, 200); await a.page.waitForTimeout(900);
    check('while it is up a rest on the line does not bring the day', (await a.read()).tl === '');
    await a.page.mouse.down(); await a.page.mouse.up(); await a.page.waitForTimeout(300);
    check('and neither does a click', (await a.read()).tl === '');
    await a.page.keyboard.press('Escape');
    await a.until(() => !document.querySelector('.notch-note.is-open .nc'));
    await a.page.mouse.down(); await a.page.mouse.up();
    check('once the card is away a click opens the day', (await settle(a, 'pinned', 2000)).tl === 'pinned');
    await arrive(a, ask('c-2'));
    await a.page.waitForTimeout(3000);
    check('a notice that comes while the day is open waits', await a.page.locator('.notch-note.is-open .nc').count() === 0);
    await a.page.keyboard.press('Escape');
    await a.until(() => document.querySelector('.notch-note.is-open .nc'), undefined, 8000);
    check('and comes up once the day is folded', await a.page.locator('.notch-note.is-open .nc').count() === 1);
    await a.ctx.close();
  }

  // ---- the pane's words ----
  {
    const a = await boot();
    const seg = await line(a), on = { x: seg.mid, y: 14 };
    await a.page.mouse.move(on.x, on.y); await a.move(on.x, on.y); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-row'), undefined, 8000);
    await a.page.waitForTimeout(700);
    const text = sel => a.page.locator(sel).allInnerTexts();
    check('the header: the date, the day\'s tag, her label', (await text('.dt-date'))[0] === '10月19日 周一' && (await text('.dt-today'))[0] === '今天' && (await text('.dt-chip'))[0] === '期中周');
    check('her sentence, in the serif', (await text('.dt-line'))[0].startsWith('今天一半在路上') && await a.page.locator('.dt-line').evaluate(el => /Songti|Serif|serif/.test(getComputedStyle(el).fontFamily)));
    check('the footnote names what has not come in', (await text('.dt-foot'))[0] === 'Mac 的时间还没进来');
    const heads = (await text('.dt-sh .dt-tx b')).map(x => x.trim());
    check('stay headers, in order, by name', heads.join() === '家,A&W,家,UVic,家');
    const titles = (await text('.dt-row .dt-tx b')).map(x => x.trim());
    check('rows say what happened, by their words', ['和她说话', '照片', '开车 · 1 时 40 分', '步行 · 10 分', '27 路 · 29 分', '私事', '给银行打电话', '小组讨论'].every(w => titles.includes(w)), titles.join('|'));
    check('a photo shows its first line; a ride its stops', (await text('.dt-row .dt-tx em')).includes('汉堡和薯条') && (await text('.dt-row .dt-tx em')).includes('Shelbourne at Pear → UVic Exchange'));
    check('the now line is at 21:50, with what is still to come below it, muted', (await text('.dt-now'))[0].trim() === '现在 21:50' && await a.page.locator('.dt-now ~ .dt-row.is-future').count() >= 3);
    check('the overview bar draws the day: pieces, a now tick, the hours', await a.page.locator('.dt-seg').count() > 5 && await a.page.locator('.dt-nowtick').count() === 1 && (await text('.dt-hours span')).join() === '06,12,18,24');
    check('what the bar cannot name is counted, not drawn thin', await a.page.locator('.dt-more').count() === 1);
    check('the now line and now tick are warm white, not amber', await a.page.evaluate(() => { const els = [...document.querySelectorAll('.dt-now, .dt-nowtick')]; return els.length === 2 && els.every(el => { const c = getComputedStyle(el); return /rgb\(255, 240, 220\)/.test(c.color + c.backgroundColor); }); }));
    check('the pane moves its rows (a transition) unless reduced motion is asked for', await a.page.evaluate(() => getComputedStyle(document.querySelector('.dt-row')).transitionDuration) !== '0s');
    check('the home dot is warm and the school dot is blue', await a.page.evaluate(() => { const c = [...document.querySelectorAll('.dt-sh .dt-dot')].map(d => getComputedStyle(d).backgroundColor); return c[0] === 'rgb(255, 168, 150)' && c[3] === 'rgb(100, 200, 255)'; }));
    // Scrolled: the list opens at the now line; the top is the morning.
    await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = 0; }); await a.page.waitForTimeout(300);
    await a.shot('06-pane-scrolled-top', { x: 40, y: 0, width: 560, height: 700 });
    await a.page.evaluate(() => { const l = document.querySelector('.dt-list'); l.scrollTop = l.scrollHeight; }); await a.page.waitForTimeout(300);
    await a.shot('07-pane-scrolled-bottom', { x: 40, y: 0, width: 560, height: 700 });
    const jumped = await a.page.evaluate(async () => {
      const l = document.querySelector('.dt-list'), before = l.scrollTop; document.querySelector('.dt-seg').dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await new Promise(r => setTimeout(r, 900)); return before - l.scrollTop;
    });
    check('a click on a piece of the bar scrolls the list to its row', jumped > 100, String(jumped));
    // The day before, and back.
    check('no day after today', await a.page.locator('.dt-nav button').nth(1).isDisabled());
    await a.page.locator('.dt-nav button').first().click();
    await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日', undefined, 6000);
    await a.page.waitForTimeout(600);
    check('the day before is read by date; it has no sentence and no today tag', a.s.hits.includes('?date=2026-10-18') && await a.page.locator('.dt-line').count() === 0 && await a.page.locator('.dt-today').count() === 0 && !await a.page.locator('.dt-nav button').nth(1).isDisabled());
    check('a past day has no now line', await a.page.locator('.dt-now').count() === 0);
    await a.shot('08-yesterday', { x: 40, y: 0, width: 560, height: 520 });
    await a.page.locator('.dt-nav button').first().click();
    await a.until(() => document.querySelector('.dt-date')?.textContent === '10月17日 周六' && document.querySelector('.dt-say'), undefined, 6000);
    check('a day with nothing in it says so in a sentence, with no bar', (await text('.dt-say'))[0] === '这一天还没有记下什么' && await a.page.locator('.dt-bar').count() === 0);
    await a.page.locator('.dt-nav button').nth(1).click();
    await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日', undefined, 6000);
    await a.page.locator('.dt-nav button').nth(1).click();
    await a.until(() => document.querySelector('.dt-date')?.textContent === '10月19日 周一', undefined, 6000);
    check('and forward to today again', await a.page.locator('.dt-today').count() === 1);
    await a.ctx.close();
  }
  {
    // prefers-reduced-motion: the pane's transitions go.
    const a = await boot();
    await a.page.emulateMedia({ reducedMotion: 'reduce' });
    const seg = await line(a);
    await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-row'), undefined, 8000);
    const still = await a.page.evaluate(() => ['.dt-row', '.dt-seg', '.dt-nav button'].map(q => getComputedStyle(document.querySelector(q)).transitionDuration));
    check('with reduced motion the pane has no transitions', still.every(d => d === '0s'), still.join());
    await a.ctx.close();
  }
  {
    // Do not disturb: the marks stay as they were when it began, so does the day.
    const a = await boot();
    const seg = await line(a), on = { x: seg.mid, y: 14 };
    await a.page.evaluate(() => window.__emit('controls', { quiet: 'dnd' }));
    await a.page.waitForTimeout(600);
    a.s.served = makeDay({ line: { text: '换了一句话。', draft: false } });
    await a.page.mouse.move(on.x, on.y); await a.move(on.x, on.y); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-line'), undefined, 8000);
    await a.page.waitForTimeout(1200);
    check('at do-not-disturb the day is as it was when it began', (await a.page.locator('.dt-line').innerText()).startsWith('今天一半在路上'));
    await a.page.keyboard.press('Escape');
    await a.page.evaluate(() => window.__emit('controls', { quiet: 'off' }));
    await a.page.waitForTimeout(600);
    await a.glide(on, a.far, 100); await a.page.waitForTimeout(400);
    await a.move(on.x, on.y); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-line')?.textContent.startsWith('换了'), undefined, 8000);
    check('and after it the day is read again', true);
    await a.ctx.close();
  }
  {
    // A daemon that has no day yet, and a pane before it has read one: plain sentences, no spinner.
    const a = await boot({ served: null });
    await a.page.unroute(`${daemon}/**`);
    await a.page.route(`${daemon}/**`, route => route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' }));
    await a.page.reload();
    await a.page.waitForTimeout(1500);
    const r = await a.read();
    check('with no day to read the wing says 今天', r.line === '今天', JSON.stringify(r));
    const x = (r.at ?? '').split('-').map(Number), mid = (x[0] + x[1]) / 2;
    await a.move(mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-say'), undefined, 8000);
    check('and the pane says so in a sentence', (await a.page.locator('.dt-say').innerText()).includes('还不会给出一天'));
    await a.ctx.close();
  }

  console.log(`\n${checks.length} checks passed; screenshots in ${dir}`);
} finally {
  await browser.close();
  server.kill();
}
if (errors.length) { console.error('page errors:', errors); process.exit(1); }

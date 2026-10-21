// The 额度 door of the day pane (src/doors/quota.tsx): Claude's 5-hour limit as a page of the day under the bar. A level on the bar for each window (past ones dim, the running one lit and carried on
// dashed to the bar's end), a row each, the now line, the reset to come, the line, the numbers and the count on the door. Headless Chrome against the built page with the native bridge stubbed and a
// fake daemon that serves the day, the days before it, /inherent/usage and /inherent/usage/history (the route is built on another branch: this fake reads a pool of readings the way that route
// does, the last reading before the span leading each list). Run after `npm run build`.
// Screenshots (2x) land in $OUT (default verify-out/doors-quota/, not committed).
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = process.env.OUT ?? path.join(root, 'verify-out/doors-quota');
mkdirSync(out, { recursive: true });
const web = Number(process.env.COMPANION_PORT ?? 5421), daemonPort = Number(process.env.COMPANION_DAEMON_PORT ?? 9021), daemon = `http://127.0.0.1:${daemonPort}`;
const checks = [], errors = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name} ${detail}`); checks.push(name); console.log('ok', name); };
const same = (a, b, d = .5) => Math.abs(a - b) <= d;

// ---------- the days: 2026-10-19 in Vancouver (PDT, UTC-7), now 21:50, and the three before it ----------
const DATE = '2026-10-19', TZ = 'America/Vancouver';
const T = (h, m = 0, day = 19) => Date.UTC(2026, 9, day, h + 7, m);
const NOW = T(21, 50);
const it = (id, kind, start, end, extra = {}, lane = 'you') => ({ id, lane, kind, start_ms: start, end_ms: end, state: end === null ? (start > NOW ? 'next' : 'now') : end <= NOW ? 'done' : start > NOW ? 'next' : 'now', ...extra });
const home = { id: 'p-home', name: '家', role: 'home' }, uvic = { id: 'p-uvic', name: 'UVic', role: 'school' };
const todayReply = () => ({
  date: DATE, tz: TZ, start_ms: T(0), end_ms: T(24), now_ms: NOW,
  items: [
    it('sleep-1', 'sleep', T(0, 55), T(9, 5), { minutes: 470 }),
    it('stay-home-1', 'stay', T(9, 5), T(11, 25), { place: '12 Example St, Victoria', kept_place: home }),
    it('move-1', 'move', T(11, 25), T(13, 5), { activity: 'automotive' }),
    it('stay-uvic', 'stay', T(13, 15), T(18, 15), { place: 'University of Victoria, Victoria', kept_place: uvic }),
    it('ride-2', 'ride', T(18, 15), T(18, 50), { route: '27', board_stop: 'UVic Exchange', alight_stop: 'Shelbourne at Pear', source: 'tapped', ended_reason: 'left_vehicle', credit: null }),
    it('stay-home-3', 'stay', T(18, 50), null, { place: '12 Example St, Victoria', kept_place: home }),
    it('talk-2', 'talk', T(19, 30), T(19, 36), { turns: 4 }, 'her'),
    it('reminder-1', 'reminder', T(22, 0), null, { text: '把垃圾拿出去', fired: false, acknowledged: false }, 'her'),
  ],
  labels: [], missing: [], line: { text: '今天一半在路上，一半在 UVic。', draft: false, written_at: '2026-10-19T21:30:00-07:00' }, highlights: [],
});
// A past day with a night and a stay (its bar starts at 07:00, the hour before he woke).
const pastReply = no => ({
  date: `2026-10-${no}`, tz: TZ, start_ms: T(0, 0, no), end_ms: T(24, 0, no), now_ms: NOW,
  items: [
    { id: `sleep-${no}`, lane: 'you', kind: 'sleep', start_ms: T(1, 10, no), end_ms: T(8, 40, no), state: 'done', minutes: 450 },
    { id: `home-${no}`, lane: 'you', kind: 'stay', start_ms: T(8, 40, no), end_ms: T(24, 0, no), state: 'done', place: '12 Example St, Victoria', kept_place: home },
  ],
  labels: [], missing: [], line: null, highlights: [],
});

// ---------- the readings the poll wrote (it writes one only when a number changes) ----------
// The 19th has three windows: 10:30-15:30 (to 15%), 16:10-21:10 (to 64%) and the one running, 21:10-02:10 (38% at 21:50). Before them: on the 17th one from 11:00 and one from 22:00 that goes on past
// midnight; on the 18th one from 06:20 (it began before that day's bar, which starts at 07:00) and one from 18:40. So the 17th has two windows, the 18th three (the first is over before its bar begins).
const Z = ms => new Date(ms).toISOString().replace('.000Z', '+00:00');   // a reset as the daemon writes it: UTC, whole seconds
const win = (key, label, percent, resets) => ({ key, label, percent, resets_at: resets === null ? null : Z(resets) });
const five = (percent, reset) => win('five_hour', '5 小时', percent, reset);
const week = percent => win('seven_day', '7 天 · 总', percent, T(0, 0, 26));
const reading = (at, windows, status = 'ok') => ({ at_ms: at, status, windows });
const R = { a: T(16, 0, 17), b: T(3, 0, 18), c: T(11, 20, 18), d: T(23, 40, 18), w1: T(15, 30), w2: T(21, 10), w3: T(2, 10, 20) };
const STAIRS = [
  [R.a, T(11, 0, 17), [[T(11, 5, 17), 6], [T(12, 30, 17), 20], [T(14, 55, 17), 31]]],
  [R.b, T(22, 0, 17), [[T(22, 15, 17), 4], [T(23, 0, 17), 12], [T(23, 50, 17), 22]]],
  [R.c, T(6, 20, 18), [[T(6, 30, 18), 10], [T(6, 50, 18), 35], [T(7, 30, 18), 40], [T(9, 0, 18), 45]]],
  [R.d, T(18, 40, 18), [[T(18, 45, 18), 7], [T(20, 30, 18), 28], [T(23, 10, 18), 47]]],
  [R.w1, T(10, 30), [[T(10, 35), 3], [T(10, 50), 8], [T(11, 5), 12], [T(11, 20), 15]]],
  [R.w2, T(16, 10), [[T(16, 15), 2], [T(16, 40), 9], [T(17, 5), 19], [T(17, 25), 30], [T(19, 40), 34], [T(20, 5), 45], [T(20, 30), 54], [T(21, 5), 64]]],
  [R.w3, T(21, 10), [[T(21, 12), 3], [T(21, 25), 14], [T(21, 38), 27], [T(21, 50), 38]]],
];
const POOL = {
  claude: [
    reading(T(9, 0, 17), [week(40)]),                                  // no 5-hour window running
    ...STAIRS.flatMap(([reset, , steps]) => steps.map(([at, p]) => reading(at, [five(p, reset), week(41 + p / 4)]))),
    reading(T(16, 5, 17), [five(0, null), week(45)]),                  // after a reset: the 5-hour window idle (a percent, no reset)
    reading(T(11, 25, 18), [week(48)]),
    reading(T(23, 45, 18), [week(52)]),                                // the last before the 19th: idle
    reading(T(11, 40), [], 'error'),                                   // a poll that failed: not 0%
    reading(T(15, 35), [five(0, null), week(57)]),
  ].sort((a, b) => a.at_ms - b.at_ms),
  codex: [reading(T(10, 40), [win('primary_window', '7 天', 8, T(0, 0, 26))])],
};
// What the route answers for a span: per service the last reading before it, then the readings in it; a service with nothing in reach is absent.
const historyFor = (from, to) => ({ services: Object.fromEntries(Object.entries(POOL).flatMap(([name, list]) => {
  const before = list.filter(r => r.at_ms < from).slice(-1), within = list.filter(r => r.at_ms >= from && r.at_ms <= to);
  return before.length + within.length ? [[name, [...before, ...within]]] : [];
})) });

// ---------- what the usage route says now ----------
const svc = (data, status = 'ok') => ({ status, observed_at_ms: NOW, data, ...status === 'unconfigured' && { error: 'no login' } });
const PLAIN = {
  claude: svc({ plan: '20X', windows: [five(38, R.w3), week(61), win('seven_day_fable', '7 天 · Fable', 24, T(0, 0, 26))] }),
  openai: svc({ today_usd: 0.84, month_usd: 12.6, by_model: [], by_key: [] }),
  anthropic: svc({ today_usd: 0.31, month_usd: 4.2, by_model: [], official: { today_usd: 2.36, month_usd: 25.4, by_model: [] } }),
  deepseek: svc({ balances: { CNY: 58 } }),
  minimax: svc({}, 'unconfigured'),
};
const USAGE = {
  plain: { services: PLAIN },
  full: { services: { ...PLAIN, codex: svc({ plan: 'Pro', reset_credits: 0, windows: [win('primary_window', '7 天', 46, T(0, 0, 26)), win('secondary_window', '30 天', 12, T(0, 0, 40))] }),
    openai: svc({ ...PLAIN.openai.data, balance_usd: 12.5 }), minimax: svc({ balance: 15.36 }), anthropic: svc({ ...PLAIN.anthropic.data, official: { ...PLAIN.anthropic.data.official, balance_usd: 179.4 } }) } },
  none: { services: {} },
  idle: { services: { claude: svc({ plan: '20X', windows: [five(0, null), week(61)] }) } },
};

// ---------- the built page ----------
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const exe = [process.env.CHROMIUM, '/opt/pw-browsers/chromium'].find(p => p && existsSync(p));
const browser = await chromium.launch({ headless: true, ...exe ? { executablePath: exe } : { channel: 'chrome' }, args: ['--disable-web-security', '--mute-audio'] });

// A page on the fake daemon. `s.usage`: which usage answer (a key of USAGE, 'fail' for a 500, 'missing' for a 404); `s.history`: 'ok', 'none' (nothing in reach), 'fail', 'missing' (the route is not served);
// `s.hits`: how many times each route was asked; `s.asked`: the spans the history was asked for; `s.bad`: asks a real daemon would refuse (a 400); `s.slow`: a span's `from_ms` to the ms its answer waits.
async function boot({ lang = 'zh', usage = 'plain', history = 'ok' } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const s = { usage, history, hits: {}, asked: [], bad: [], slow: new Map() };
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
  await page.addInitScript(([settings]) => {
    localStorage.setItem('companion-settings-v1', JSON.stringify(settings));
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth: 185, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {}, wearing: () => {}, onDictation: () => () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async () => {}, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, onGhostty: () => () => {}, onAgentsPresence: () => () => {}, onMouseDown: () => () => {}, onClaudeFront: () => () => {}, plugins: async () => ({}),
    };
    window.WebSocket = class { constructor() { setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
  }, [{ lang }]);
  await page.route(`${daemon}/**`, async route => {
    const req = route.request(), url = new URL(req.url()), p = url.pathname;
    const json = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    s.hits[p] = (s.hits[p] ?? 0) + 1;
    if (p === '/inherent/day') {
      const date = url.searchParams.get('date'), no = Number(date?.slice(-2));
      return json(!date || date === DATE ? todayReply() : no >= 17 && no < 19 ? pastReply(no) : { ...pastReply(18), date, start_ms: T(0, 0, no), end_ms: T(24, 0, no), items: [] });
    }
    if (p === '/inherent/usage') return s.usage === 'fail' ? json({ detail: 'down' }, 500) : s.usage === 'missing' ? json({ detail: 'Not Found' }, 404) : json(USAGE[s.usage]);
    if (p === '/inherent/usage/history') {
      const from = url.searchParams.get('from_ms') ?? '', to = url.searchParams.get('to_ms') ?? '';
      if (!/^\d+$/.test(from) || !/^\d+$/.test(to) || +to < +from || +to - +from > 48 * 3_600_000) { s.bad.push(url.search); return json({ detail: 'bad span' }, 400); }
      s.asked.push({ from: +from, to: +to });
      if (s.slow.has(+from)) await new Promise(resolve => setTimeout(resolve, s.slow.get(+from)));
      return s.history === 'fail' ? json({ detail: 'down' }, 500) : s.history === 'missing' ? json({ detail: 'Not Found' }, 404) : json(s.history === 'none' ? { services: {} } : historyFor(+from, +to));
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
  const read = () => page.evaluate(() => { const n = document.querySelector('.notch'); return { tl: n.dataset.tl, door: n.querySelector('.dt')?.dataset.door ?? null }; });
  const until = (fn, arg, timeout = 6000) => page.waitForFunction(fn, arg, { timeout });
  const lineX = async () => { const at = await page.evaluate(() => document.querySelector('.notch').dataset.lineAt), [x0, x1] = at.split('-').map(Number); return (x0 + x1) / 2; };
  const press = async (x, y) => { await move(x, y); await page.mouse.down(); await page.mouse.up(); };
  const rect = sel => page.evaluate(sel => { const e = document.querySelector(sel); return e ? e.getBoundingClientRect().toJSON() : null; }, sel);
  const pressOn = async sel => { const r = await rect(sel); assert.ok(r, `${sel} is on the page`); await press(r.x + r.width / 2, r.y + r.height / 2); };
  const hoverOn = async sel => { const r = await rect(sel); assert.ok(r, `${sel} is on the page`); await move(r.x + r.width / 2, r.y + r.height / 2); };
  const texts = sel => page.evaluate(sel => [...document.querySelectorAll(sel)].map(e => (e.innerText || e.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim()), sel);
  const keys = () => page.evaluate(() => [...document.querySelectorAll('.notch .dt-row[data-key]')].map(e => e.dataset.key));
  const door = async key => { await pressOn(`.notch .dt-door[data-door="${key}"]`); await until(key => document.querySelector('.notch .dt')?.dataset.door === key, key); await page.waitForTimeout(420); };
  const openDay = async () => {
    await until(() => document.querySelector('.notch')?.dataset.line);
    await press(await lineX(), 14);
    await until(() => document.querySelector('.notch-drop.is-open .dt-row'), undefined, 8000);
    await page.waitForTimeout(1000);
  };
  const geo = () => page.evaluate(() => {
    const r = e => { const b = e?.getBoundingClientRect(); return b ? b.toJSON() : null; }, q = s => document.querySelector(s), dt = q('.notch .dt');
    return { drop: r(q('.notch-drop')), inner: r(q('.notch-pane-in')), shape: r(q('.notch-shape path')), dt: r(dt), head: r(dt.querySelector('.dt-head')), bar: r(dt.querySelector('.dt-bar')), track: r(dt.querySelector('.dt-track')), doors: r(dt.querySelector('.dt-doors')), from: dt.querySelector('.dt-bar')?.dataset.from ?? null };
  });
  // The levels on the bar, in the order they are drawn: tone, the area's points and the line's, and the dashed run if it has one (viewBox units: x 0..1000 across the bar, y 0..24 down the track).
  const levels = () => page.evaluate(() => [...document.querySelectorAll('.notch .dt-track svg.dt-mk-lv')].map(svg => {
    const pts = el => (el?.getAttribute('points') ?? '').trim().split(/\s+/).filter(Boolean).map(p => p.split(',').map(Number)), run = svg.querySelector('.dt-lv-run');
    return { tone: ['on', 'dim', 'amber'].find(t => svg.classList.contains(`is-${t}`)), area: pts(svg.querySelector('.dt-lv-fill')), line: pts(svg.querySelector('.dt-lv-top')), view: svg.getAttribute('viewBox'), muted: svg.classList.contains('is-muted'),
      run: run ? { x1: +run.getAttribute('x1'), y1: +run.getAttribute('y1'), x2: +run.getAttribute('x2'), y2: +run.getAttribute('y2'), dash: getComputedStyle(run).strokeDasharray } : null };
  }));
  const shot = async (name, g) => page.screenshot({ path: path.join(out, `${name}.png`), clip: { x: 60, y: 0, width: 520, height: Math.ceil(Math.max(g.drop.bottom, g.dt.bottom)) + 2 } });
  return { page, ctx, s, move, read, until, press, pressOn, hoverOn, texts, keys, door, openDay, geo, levels, rect, shot };
}
const moved = (g0, g1) => [['pane', 'drop', 'height'], ['pane inside', 'inner', 'height'], ['shape', 'shape', 'height'], ['day', 'dt', 'height'], ['day', 'dt', 'top'], ['head', 'head', 'top'], ['bar', 'bar', 'top'], ['doors', 'doors', 'top']]
  .filter(([, k, p]) => !same(g0[k][p], g1[k][p])).map(([name, k, p]) => `${name} ${p} ${g0[k][p]} -> ${g1[k][p]}`);
// The bar runs from the hour in `data-from` to 24:00 of its day. Where a moment is on it: in pixels of the page, and in the level's own units (x 0..1000, y 0..24 from the top; 0% at the foot, 100% at 1).
const axis = (g, day = 19) => { const [h, m] = g.from.split(':').map(Number); return { t0: T(h, m, day), t1: T(24, 0, day) }; };
const xAt = (g, ms, day = 19) => { const { t0, t1 } = axis(g, day); return g.track.left + (ms - t0) / (t1 - t0) * g.track.width; };
const unit = (g, ms, v, day = 19) => { const { t0, t1 } = axis(g, day); return [Math.max(0, Math.min(1000, (ms - t0) / (t1 - t0) * 1000)), 1 + 23 * (1 - v)]; };
// The stair a window's readings make, written out from the readings alone: from `from` at the window's start, up where each reading says so, held to `end`.
const stair = (start, steps, end, from = 0) => {
  const pts = [], put = (at, v) => { const p = pts.at(-1); if (!p || p[0] !== at || p[1] !== v) pts.push([at, v]); };
  let v = from; put(start, v);
  for (const [at, p] of steps) { if (p / 100 !== v) put(at, v); put(at, p / 100); v = p / 100; }
  put(Math.max(end, steps.at(-1)[0]), v);
  return pts;
};
const closeTo = (a, b, d = .16) => a.length === b.length && a.every((p, i) => same(p[0], b[i][0], d) && same(p[1], b[i][1], d));
const OF = Object.fromEntries(STAIRS.map(([reset, start, steps]) => [reset, { start, steps, end: reset }]));
const stairOf = (g, reset, day = 19, end = OF[reset].end) => stair(OF[reset].start, OF[reset].steps, end).map(([ms, v]) => unit(g, ms, v, day));
const wKey = reset => `w${reset}`;

try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }

  // ================= 今天: three windows, the third running =================
  {
    const a = await boot();
    await a.page.waitForTimeout(1500);
    check('before the pane is opened the door does not ask for the history (the day is read, and the Dashboard polls usage by itself, but no one has asked for a span of readings)', (a.s.hits['/inherent/day'] ?? 0) > 0 && !a.s.hits['/inherent/usage/history'], JSON.stringify(a.s.hits));
    await a.openDay();
    await a.until(() => /\d%/.test(document.querySelector('.notch .dt-door[data-door="quota"]')?.textContent ?? ''));
    const g0 = await a.geo();
    check('the door shows Claude\'s 5-hour percent as its count: 额度 38%', (await a.texts('.notch .dt-door[data-door="quota"]'))[0] === '额度 38%', (await a.texts('.notch .dt-door')).join('|'));
    check('and it asked for the history of the day: from its start to its end, in whole milliseconds (nothing a real daemon would refuse)', a.s.asked.length >= 1 && a.s.asked.every(x => x.from === T(0) && x.to === T(24)) && a.s.bad.length === 0, JSON.stringify([a.s.asked, a.s.bad]));
    await a.door('quota');
    await a.until(() => document.querySelectorAll('.notch .dt-row[data-key]').length === 4);
    const g1 = await a.geo();
    check('on 额度 the pane, its shape, the day, the head, the bar and the doors row are where they were on 今天 and the same size (584 pt)', moved(g0, g1).length === 0 && same(g1.dt.height, 584), moved(g0, g1).join('; '));
    check('the line is written from the facts: this 5 hours, its reset, and the week', (await a.texts('.notch .dt-line'))[0] === '这 5 小时用了 38%，02:10 重置；这周用了 61%。', (await a.texts('.notch .dt-line')).join());
    check('the numbers row: 5 小时 38%, 本周 61%, API 今天 $3.20 (OpenAI 0.84 and the whole Anthropic account 2.36, not the 0.31 Jarvis made itself), DeepSeek ≈ $8.17; no Codex, no OpenAI, MiniMax or Anthropic balance (none reported), and no link',
      (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '5 小时 38%|本周 61%|API 今天 $3.20|DeepSeek ≈ $8.17' && await a.page.locator('.notch .dt-numbers .dt-go').count() === 0, (await a.texts('.notch .dt-numbers .dt-num')).join('|'));
    check('and they only say a number: no chip is a lens', await a.page.locator('.notch .dt-numbers .dt-num.is-info').count() === 4 && await a.page.locator('.notch .dt-numbers button').count() === 0);

    // ---- the rows ----
    const ks = { w1: wKey(R.w1), w2: wKey(R.w2), w3: wKey(R.w3) };
    const order = await a.page.evaluate(() => [...document.querySelectorAll('.notch .dt-list .dt-row, .notch .dt-list .dt-now')].map(e => e.dataset.key ?? 'now'));
    check('one row per window of the day, oldest first, the now line after the running one and the reset after that', order.join() === [ks.w1, ks.w2, ks.w3, 'now', 'reset'].join(), order.join());
    const rows = await a.page.evaluate(() => Object.fromEntries([...document.querySelectorAll('.notch .dt-row[data-key]')].map(r => [r.dataset.key, {
      at: r.querySelector('.dt-at')?.textContent, title: r.querySelector('.dt-ttl b')?.textContent, sub: r.querySelector('.dt-tx em')?.textContent, side: r.querySelector('.dt-tm')?.textContent, value: !!r.querySelector('.dt-tm .is-value'),
      ring: r.querySelector('.dt-ic')?.classList.contains('is-on'), future: r.classList.contains('is-future'), still: r.classList.contains('is-still'), glyph: !!r.querySelector('.dt-ic svg') }])));
    const want = (at, title, sub, side, extra = {}) => JSON.stringify({ at, title, sub, side, value: false, ring: false, future: false, still: false, glyph: true, ...extra });
    check('a past window\'s row: when it began, 5 小时额度, from and to, and how high it got (用到 15%, 用到 64%)',
      JSON.stringify(rows[ks.w1]) === want('10:30', '5 小时额度', '10:30 – 15:30', '用到 15%') && JSON.stringify(rows[ks.w2]) === want('16:10', '5 小时额度', '16:10 – 21:10', '用到 64%'), JSON.stringify([rows[ks.w1], rows[ks.w2]]));
    check('the running window\'s row says 已用 38% in the value look, its glyph ring is lit', JSON.stringify(rows[ks.w3]) === want('21:10', '5 小时额度', '21:10 – 02:10', '已用 38%', { value: true, ring: true }), JSON.stringify(rows[ks.w3]));
    check('the reset to come: its clock, 重置, "a new window starts", how long is left (4 时 20 分, as the day writes durations), in the future look', JSON.stringify(rows.reset) === want('02:10', '重置', '这 5 小时重新算', '还有 4 时 20 分', { future: true }), JSON.stringify(rows.reset));
    check('every row is a way into a page (the Dashboard\'s usage page): none is still', await a.page.locator('.notch .dt-row[data-key].is-still').count() === 0 && await a.page.locator('.notch .dt-row[data-key]:not(.is-still)').count() === 4);

    // ---- the levels on the bar ----
    const lv = await a.levels();
    check('the bar has three levels, one per window: the first two dim, the one that is running lit', lv.length === 3 && lv.map(x => x.tone).join() === 'dim,dim,on' && lv.every(x => x.area.length > 2 && x.view === '0 0 1000 24'), JSON.stringify(lv.map(x => [x.tone, x.area.length, x.view])));
    check('each level is its window\'s stair: 0 at its start, up at the time of each reading, held to the window\'s end (15:30, 21:10); a failed poll at 11:40 and an idle reading at 15:35 move nothing',
      closeTo(lv[0].line, stairOf(g1, R.w1)) && closeTo(lv[1].line, stairOf(g1, R.w2)), JSON.stringify([lv[0].line, stairOf(g1, R.w1)]));
    check('the running window\'s stair goes on to now (21:50) at 38%, the height of 38% of the track', closeTo(lv[2].line, stairOf(g1, R.w3, 19, NOW)) && same(lv[2].line.at(-1)[1], 1 + 23 * .62, .1), JSON.stringify([lv[2].line.slice(-3), stairOf(g1, R.w3, 19, NOW).slice(-3)]));
    const run = lv[2].run;
    check('and from now it is carried on dashed (3 3) to the bar\'s end, at 38%', !!run && same(run.x1, unit(g1, NOW, .38)[0], .1) && run.x2 === 1000 && same(run.y1, 1 + 23 * .62, .1) && same(run.y2, run.y1, .01) && /3(px)?,? 3/.test(run.dash), JSON.stringify(run));
    check('a window that is over is not carried on', lv[0].run === null && lv[1].run === null);
    check('the area of each is closed on the foot of the track (its polygon runs from the start to the end of the line along y = 24)', lv.every(x => x.area[0][1] === 24 && x.area.at(-1)[1] === 24 && x.area[0][0] === x.line[0][0] && x.area.at(-1)[0] === x.line.at(-1)[0]));
    check('the bar is in page mode: what he was doing steps back to .25', await a.page.locator('.notch .dt-bar.is-page').count() === 1 && await a.page.evaluate(() => getComputedStyle(document.querySelector('.notch .dt-bar.is-page .dt-seg')).opacity === '0.25'));
    await a.hoverOn('.notch .dt-head');
    await a.shot('quota', g1);

    // ---- pointer on a row, and on the bar ----
    await a.hoverOn(`.notch .dt-row[data-key="${ks.w1}"]`); await a.page.waitForTimeout(250);
    const hot = await a.page.evaluate(() => ({ hot: [...document.querySelectorAll('.notch .dt-row.is-hot')].map(e => e.dataset.key), brackets: [...document.querySelectorAll('.notch .dt-bracket')].map(e => { const b = e.getBoundingClientRect(); return { l: b.left, r: b.right }; }),
      times: [...document.querySelectorAll('.notch .dt-scrub .dt-time:not(.is-now)')].map(e => e.innerText) }));
    check('the pointer on a window\'s row brackets its level from its start to its end and says both times above the bar (10:30, 15:30)', hot.hot.join() === ks.w1 && hot.brackets.length === 1 && same(hot.brackets[0].l, xAt(g1, T(10, 30)), 1.5) && same(hot.brackets[0].r, xAt(g1, T(15, 30)), 1.5) && hot.times.join() === '10:30,15:30', JSON.stringify(hot));
    const y = g1.track.top + g1.track.height / 2;
    await a.press(xAt(g1, T(18, 0)), y); await a.page.waitForTimeout(500);
    const picked = await a.page.evaluate(() => ({ pick: [...document.querySelectorAll('.notch .dt-row.is-pick')].map(e => e.dataset.key), open: !!document.querySelector('.notch .dt-page[data-page]') }));
    check('a click on the bar under a level (18:00) picks that window\'s row and opens nothing', picked.pick.join() === ks.w2 && !picked.open, JSON.stringify(picked));
    await a.pressOn(`.notch .dt-row[data-key="${ks.w1}"] .dt-tx`);
    await a.until(() => document.querySelector('.notch .dt-page .ad.is-only[data-page="usage"] .pg-head .pg-back'), undefined, 8000); await a.page.waitForTimeout(600);
    const opened = await a.page.evaluate(() => { const pg = document.querySelector('.notch .dt-stage > .dt-page[data-page]'); return { stage: document.querySelector('.notch .dt-stage')?.className ?? '', ownHead: !!pg?.querySelector('.dt-pin, .dt-ph, .dt-pn'),
      dial: !!pg?.querySelector('.ad.is-only .dial[aria-valuenow="38"]'), door: document.querySelector('.notch .dt')?.dataset.door }; });
    check('a click on a window\'s row opens the Dashboard\'s own usage page in the pane, under its own ‹ and with no head of the frame\'s: Claude\'s 5-hour window at 38%, still on 额度, and the frame has not moved',
      /\bis-tool\b/.test(opened.stage) && !opened.ownHead && opened.dial && opened.door === 'quota' && moved(g1, await a.geo()).length === 0, JSON.stringify(opened));
    await a.pressOn('.notch .dt-page .ad.is-only .pg-head .pg-back'); await a.page.waitForTimeout(800);
    check('its ‹ at the page\'s root takes it back to 额度\'s rows, and the frame has not moved', await a.page.locator('.notch .dt-page').count() === 0 && (await a.keys()).join() === [ks.w1, ks.w2, ks.w3, 'reset'].join() && moved(g1, await a.geo()).length === 0, (await a.keys()).join());

    // ---- a range of the day cuts the page ----
    const from = xAt(g1, T(17, 30)) + .1, to = xAt(g1, T(19, 0)) + .1;
    await a.move(from, y); await a.page.mouse.down(); await a.move((from + to) / 2, y); await a.move(to, y); await a.page.waitForTimeout(150); await a.page.mouse.up(); await a.page.waitForTimeout(300);
    await a.pressOn('.notch .dt-card.is-range .dt-cbtn'); await a.page.waitForTimeout(450);
    check('a range of 17:30 to 19:00 cuts the page to the window that overlaps it; the other windows and the reset leave, and the frame does not move', (await a.keys()).join() === ks.w2 && (await a.texts('.notch .dt-filter'))[0] === '只看 17:30–19:00' && moved(g1, await a.geo()).length === 0, (await a.keys()).join());
    await a.pressOn('.notch .dt-filter button'); await a.page.waitForTimeout(450);
    check('the chip gives the whole day back', (await a.keys()).length === 4);

    // ---- the other doors and back: the count stays, the page is the same ----
    await a.door('today'); await a.door('quota'); await a.until(() => document.querySelectorAll('.notch .dt-row[data-key]').length === 4);
    check('another door and back to 额度 is the same page, and the door count is still 38%', (await a.keys()).length === 4 && (await a.texts('.notch .dt-door[data-door="quota"]'))[0] === '额度 38%');

    // ---- ← goes to the 18th and stays on 额度; the history is asked for that day ----
    await a.hoverOn('.notch .dt-head');
    await a.page.keyboard.press('ArrowLeft');
    await a.until(() => document.querySelector('.notch .dt-date')?.textContent === '10月18日 周日' && document.querySelectorAll('.notch .dt-row[data-key]').length === 3, undefined, 8000); await a.page.waitForTimeout(500);
    const g2 = await a.geo(), y18 = await a.levels();
    const k18 = [wKey(R.b), wKey(R.c), wKey(R.d)];
    check('← goes to the 18th and stays on 额度: its history is asked for that day, and the rows are the three windows that touched it, oldest first (the first began on the 17th, 22:00)',
      (await a.read()).door === 'quota' && a.s.asked.some(x => x.from === T(0, 0, 18) && x.to === T(24, 0, 18)) && (await a.keys()).join() === k18.join()
      && (await a.texts('.notch .dt-row .dt-at')).join('|') === '22:00|06:20|18:40' && (await a.texts('.notch .dt-row .dt-tx em')).join('|') === '22:00 – 03:00|06:20 – 11:20|18:40 – 23:40' && (await a.texts('.notch .dt-row .dt-tm')).join('|') === '用到 22%|用到 45%|用到 47%', `${(await a.keys()).join()} ${(await a.texts('.notch .dt-row .dt-tm')).join('|')}`);
    check('on that day there is no now line, no reset, no running window: nothing is lit and nothing is carried on', await a.page.locator('.notch .dt-now').count() === 0 && !(await a.keys()).includes('reset') && y18.every(x => x.tone === 'dim' && x.run === null) && await a.page.locator('.notch .dt-row.is-future, .notch .dt-ic.is-on').count() === 0);
    check('the line and the number are that day\'s: three windows, the highest 47%; not today\'s', (await a.texts('.notch .dt-line'))[0] === '这天开了 3 段 5 小时，最高用到 47%。' && (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '最高 47%', `${(await a.texts('.notch .dt-line'))[0]} ${(await a.texts('.notch .dt-numbers .dt-num')).join('|')}`);
    check('the door still counts now: 额度 38%', (await a.texts('.notch .dt-door[data-door="quota"]'))[0] === '额度 38%');
    check('the pane, the head, the bar and the doors have not moved for the day change', moved(g1, g2).length === 0, moved(g1, g2).join('; '));
    // that day's bar starts at 07:00: the window that ended at 03:00 is wholly before it (no level), the one that began at 06:20 is cut at 07:00 (no wall at the left edge), the evening one is whole
    check('the bar of the 18th starts at 07:00 and draws two levels: the window that ended at 03:00 is before it, so it has none', g2.from === '07:00' && y18.length === 2, `${g2.from} ${y18.length}`);
    const cutAt = [unit(g2, T(7, 0, 18), .35, 18), ...stair(T(6, 20, 18), OF[R.c].steps, R.c).filter(([ms]) => ms > T(7, 0, 18)).map(([ms, v]) => unit(g2, ms, v, 18))];
    check('the window that began at 06:20 begins at the bar\'s edge, at the level it had there (35%, since 06:50), and goes on as its stair does: no wall of its steps before 07:00 along the left edge',
      closeTo(y18[0].line, cutAt) && y18[0].line.filter(p => p[0] === 0).length === 1, JSON.stringify([y18[0].line.slice(0, 4), cutAt.slice(0, 4)]));
    check('the evening window\'s stair is whole, from 18:40 to its end at 23:40', closeTo(y18[1].line, stairOf(g2, R.d, 18)), JSON.stringify(y18[1].line));
    await a.shot('quota-yesterday', g2);
    // the day before is asked for and its history is slow: until it comes the page is the reading page, not the 18th's windows under the 17th's date
    a.s.slow.set(T(0, 0, 17), 1200);
    await a.page.keyboard.press('ArrowLeft');
    await a.until(() => document.querySelector('.notch .dt-date')?.textContent === '10月17日 周六', undefined, 8000); await a.page.waitForTimeout(300);
    check('while the history of another day is on its way the page is not the day before\'s: it says 读着…, with no rows and no levels, and the frame has not moved',
      (await a.texts('.notch .dt-say')).join() === '读着…' && (await a.keys()).length === 0 && (await a.levels()).length === 0 && moved(g2, await a.geo()).length === 0, `${(await a.texts('.notch .dt-say')).join()} ${(await a.keys()).join()} ${(await a.levels()).length}`);
    await a.until(() => document.querySelectorAll('.notch .dt-row[data-key]').length === 2, undefined, 8000); await a.page.waitForTimeout(500);
    const g3 = await a.geo(), y17 = await a.levels();
    // on the 17th the window that began at 22:00 goes on past midnight: its level is cut at the bar's end (24:00), at the height it has there
    const cutEnd = [...stair(T(22, 0, 17), OF[R.b].steps, T(24, 0, 17)).map(([ms, v]) => unit(g3, ms, v, 17))];
    check('the 17th: two windows, and the one that goes on past midnight is cut at the bar\'s end (24:00) at its level there (22%); nothing is drawn beyond the bar', y17.length === 2 && closeTo(y17[1].line, cutEnd) && y17[1].line.at(-1)[0] === 1000 && y17.every(x => x.line.every(p => p[0] >= 0 && p[0] <= 1000)), JSON.stringify(y17[1]?.line.slice(-3)));
    await a.page.keyboard.press('ArrowRight'); await a.page.keyboard.press('ArrowRight');
    await a.until(() => document.querySelector('.notch .dt-date')?.textContent === '10月19日 周一' && document.querySelectorAll('.notch .dt-row[data-key]').length === 4 && document.querySelectorAll('.notch .dt-track svg.dt-mk-lv').length === 3, undefined, 8000); await a.page.waitForTimeout(500);
    const g4 = await a.geo(), back = await a.levels();
    check('→ twice is back on today: the four rows, the three levels (the running one carried on), the numbers of now, and nothing moved', moved(g1, g4).length === 0 && back.length === 3 && back[2].tone === 'on' && !!back[2].run && (await a.texts('.notch .dt-numbers .dt-num')).length === 4);
    await a.ctx.close();
  }

  // ================= a daemon that does not serve the history: the running window alone, flat at its percent =================
  {
    const a = await boot({ history: 'missing' });
    await a.openDay(); await a.door('quota');
    await a.until(() => document.querySelectorAll('.notch .dt-row[data-key]').length === 2);
    const g = await a.geo(), lv = await a.levels(), start = T(21, 10);
    check('with no history (a 404) the bar draws the running window alone: one level, lit, from 21:10 (its reset less 5 h) flat at 38% to now, then carried on dashed to the bar\'s end',
      lv.length === 1 && lv[0].tone === 'on' && closeTo(lv[0].line, [unit(g, start, .38), unit(g, NOW, .38)]) && !!lv[0].run && lv[0].run.x2 === 1000 && same(lv[0].run.y1, 1 + 23 * .62, .1), JSON.stringify(lv));
    const order = await a.page.evaluate(() => [...document.querySelectorAll('.notch .dt-list .dt-row, .notch .dt-list .dt-now')].map(e => e.dataset.key ?? 'now'));
    check('and says nothing about it: the row of that window, the now line and the reset, the same line and numbers, and no word of a missing route', order.join() === [wKey(R.w3), 'now', 'reset'].join() && (await a.texts('.notch .dt-line'))[0] === '这 5 小时用了 38%，02:10 重置；这周用了 61%。' && (await a.texts('.notch .dt-say')).length === 0 && (await a.texts('.notch .dt-numbers .dt-num')).length === 4);
    check('the door still says 额度 38% and the pane is the size it is on 今天', (await a.texts('.notch .dt-door[data-door="quota"]'))[0] === '额度 38%' && same(g.dt.height, 584));
    await a.page.keyboard.press('ArrowLeft');
    await a.until(() => document.querySelector('.notch .dt-date')?.textContent === '10月18日 周日', undefined, 8000); await a.page.waitForTimeout(700);
    check('on another day with no history there is nothing to draw, and the page says why', (await a.keys()).length === 0 && (await a.texts('.notch .dt-say')).join() === '这台 Jarvis 还不会给额度的历史' && (await a.levels()).length === 0);
    await a.ctx.close();
  }
  {
    const a = await boot({ history: 'fail' });
    await a.openDay(); await a.door('quota');
    await a.until(() => document.querySelectorAll('.notch .dt-row[data-key]').length === 2);
    const g = await a.geo(), lv = await a.levels();
    check('a history that fails (a 500) on today is the same: the running window alone, flat at 38%, carried on dashed, and the page is whole', lv.length === 1 && lv[0].tone === 'on' && lv[0].line.length === 2 && !!lv[0].run && same(g.dt.height, 584) && (await a.texts('.notch .dt-say')).length === 0, JSON.stringify(lv.map(x => x.line)));
    await a.ctx.close();
  }

  // ================= the numbers: Codex and every balance =================
  {
    const a = await boot({ usage: 'full' });
    await a.openDay();
    const g0 = await a.geo();
    await a.door('quota'); await a.until(() => document.querySelectorAll('.notch .dt-row[data-key]').length === 4);
    const g1 = await a.geo();
    check('with Codex and the balances reported the numbers are: 5 小时, 本周, Codex 46% (its primary window, whatever its length), API 今天, then each account that has a balance (DeepSeek, OpenAI, MiniMax, Anthropic)',
      (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '5 小时 38%|本周 61%|Codex 46%|API 今天 $3.20|DeepSeek ≈ $8.17|OpenAI $12.50|MiniMax $15.36|Anthropic $179.40', (await a.texts('.notch .dt-numbers .dt-num')).join('|'));
    check('and however many chips wrap onto more lines the pane, the head, the bar and the doors row stay where they are', moved(g0, g1).length === 0, moved(g0, g1).join('; '));
    await a.hoverOn('.notch .dt-head');
    await a.shot('quota-full', g1);
    await a.ctx.close();
  }

  // ================= the usage route down, the history up: the windows are the history's, the numbers of now are not there =================
  {
    const a = await boot({ usage: 'fail' });
    await a.openDay();
    const g0 = await a.geo();
    await a.door('quota');
    await a.until(() => document.querySelectorAll('.notch .dt-row[data-key]').length === 4);
    const g1 = await a.geo(), lv = await a.levels();
    check('with the usage route down the page is still drawn from the history (the three windows and the reset, as the last reading says), with no line, no numbers and no count on the door, and the frame has not moved',
      lv.length === 3 && lv.map(x => x.tone).join() === 'dim,dim,on' && !!lv[2].run && (await a.keys()).join() === [wKey(R.w1), wKey(R.w2), wKey(R.w3), 'reset'].join() && (await a.texts('.notch .dt-line')).join() === '' && (await a.texts('.notch .dt-numbers .dt-num')).length === 0
      && (await a.texts('.notch .dt-door[data-door="quota"]'))[0] === '额度' && moved(g0, g1).length === 0, `${lv.length} ${(await a.keys()).join()} ${(await a.texts('.notch .dt-door[data-door="quota"]'))[0]} ${moved(g0, g1).join('; ')}`);
    await a.ctx.close();
  }

  // ================= nothing to show, said in words =================
  for (const [name, opts, line, say, door] of [
    ['no usage is set up on this daemon (it answers with no services)', { usage: 'none', history: 'none' }, '还没读到额度。', '这台 Jarvis 还没设置额度', '额度'],
    ['the 5-hour window is idle (a percent and no reset), and nothing was recorded today', { usage: 'idle', history: 'none' }, '这 5 小时用了 0%；这周用了 61%。', '今天还没有额度记录', '额度 0%'],
    ['both routes fail', { usage: 'fail', history: 'fail' }, '', '没读到这一页，会再试一次', '额度'],
  ]) {
    const a = await boot(opts);
    await a.openDay();
    const g0 = await a.geo();
    await a.door('quota');
    await a.page.waitForTimeout(600);
    const g1 = await a.geo();
    check(`when ${name} the page says ${say}: ${line ? `the line "${line}", ` : 'no line, '}no rows and no levels, the door reads ${door}, and the frame has not moved`,
      (await a.texts('.notch .dt-line')).join() === line && (await a.texts('.notch .dt-say')).join() === say && (await a.keys()).length === 0 && (await a.levels()).length === 0 && (await a.texts('.notch .dt-door[data-door="quota"]'))[0] === door && moved(g0, g1).length === 0,
      `${(await a.texts('.notch .dt-line'))[0]} / ${(await a.texts('.notch .dt-say')).join()} / ${(await a.texts('.notch .dt-door[data-door="quota"]'))[0]} ${moved(g0, g1).join('; ')}`);
    await a.ctx.close();
  }

  // ================= the same page in English =================
  {
    const a = await boot({ lang: 'en', usage: 'full' });
    await a.openDay();
    await a.until(() => /\d%/.test(document.querySelector('.notch .dt-door[data-door="quota"]')?.textContent ?? ''));
    const g0 = await a.geo();
    await a.door('quota'); await a.until(() => document.querySelectorAll('.notch .dt-row[data-key]').length === 4);
    const rows = await a.page.evaluate(() => [...document.querySelectorAll('.notch .dt-row[data-key]')].map(r => [r.querySelector('.dt-ttl b')?.textContent, r.querySelector('.dt-tx em')?.textContent, r.querySelector('.dt-tm')?.textContent].join(' | ')));
    check('in English: the door is Usage 38%, the line (the longest of the page, it stays on one line) and the numbers follow, and so do the rows, and the frame has not moved',
      (await a.texts('.notch .dt-door[data-door="quota"]'))[0] === 'Usage 38%' && (await a.texts('.notch .dt-line'))[0] === '38% of this 5-hour window used, resets at 02:10; 61% this week.'
      && (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '5-hour 38%|Week 61%|Codex 46%|API today $3.20|DeepSeek ≈ $8.17|OpenAI $12.50|MiniMax $15.36|Anthropic $179.40'
      && rows.join('; ') === '5-hour window | 10:30 – 15:30 | Up to 15%; 5-hour window | 16:10 – 21:10 | Up to 64%; 5-hour window | 21:10 – 02:10 | Used 38%; Resets | A new window starts | in 4 h 20 min'
      && moved(g0, await a.geo()).length === 0, `${(await a.texts('.notch .dt-line'))[0]} ${rows.join('; ')} ${moved(g0, await a.geo()).join('; ')}`);
    await a.page.keyboard.press('ArrowLeft');
    await a.until(() => document.querySelector('.notch .dt-date')?.textContent === 'Sun, Oct 18' && document.querySelectorAll('.notch .dt-row[data-key]').length === 3, undefined, 8000); await a.page.waitForTimeout(400);
    check('and on another day: "3 five-hour windows that day, up to 47%." with the one number, Highest 47%', (await a.texts('.notch .dt-line'))[0] === '3 five-hour windows that day, up to 47%.' && (await a.texts('.notch .dt-numbers .dt-num')).join('|') === 'Highest 47%', `${(await a.texts('.notch .dt-line'))[0]}`);
    await a.page.keyboard.press('ArrowRight');
    await a.until(() => document.querySelector('.notch .dt-date')?.textContent === 'Mon, Oct 19' && document.querySelectorAll('.notch .dt-row[data-key]').length === 4, undefined, 8000); await a.page.waitForTimeout(500);
    await a.hoverOn('.notch .dt-head');
    await a.shot('quota-en', await a.geo());
    await a.ctx.close();
  }

  check('no page error from any of it', errors.length === 0, errors.join('; '));
  console.log(`\n${checks.length} checks passed; screenshots in ${out}`);
} finally {
  await browser.close();
  server.kill();
}
if (errors.length) { console.error('page errors:', errors); process.exit(1); }

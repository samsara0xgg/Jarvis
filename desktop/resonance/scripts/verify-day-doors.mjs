// The day pane's doors (src/DayTimeline.tsx, src/DayDoors.tsx, src/doors/): the pane is one size whichever door he is on, a door swaps only what is under the bar (the line, the numbers
// row, the rows), the bar stays the day and lights the page's own moments, and 信 is the unread mail as a page of the day. Headless Chrome against the built page with the native
// bridge stubbed and a fake daemon that serves a day, the day before it and the unread letters. Run after `npm run build`.
// Screenshots (2x) of 今天 and 信 land in $OUT (default verify-out/doors/, not committed).
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = process.env.OUT ?? path.join(root, 'verify-out/doors');
mkdirSync(out, { recursive: true });
const web = Number(process.env.COMPANION_PORT ?? 5190), daemonPort = Number(process.env.COMPANION_DAEMON_PORT ?? 8796), daemon = `http://127.0.0.1:${daemonPort}`;
const checks = [], errors = [];
const check = (name, pass, detail = '') => { assert.ok(pass, `${name} ${detail}`); checks.push(name); console.log('ok', name); };
const same = (a, b, d = .5) => Math.abs(a - b) <= d;

// ---------- the day: 2026-10-19 in Vancouver (PDT, UTC-7), now 21:50; the day before it ----------
const DATE = '2026-10-19', YESTERDAY = '2026-10-18', TZ = 'America/Vancouver';
const T = (h, m = 0, day = 19) => Date.UTC(2026, 9, day, h + 7, m);
const NOW = T(21, 50);
const iso = (h, m, day = 19) => `2026-10-${day}T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00-07:00`;
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
const yesterdayReply = () => ({
  date: YESTERDAY, tz: TZ, start_ms: T(0, 0, 18), end_ms: T(24, 0, 18), now_ms: NOW,
  items: [
    { id: 'y-sleep', lane: 'you', kind: 'sleep', start_ms: T(1, 10, 18), end_ms: T(8, 40, 18), state: 'done', minutes: 450 },
    { id: 'y-home', lane: 'you', kind: 'stay', start_ms: T(8, 40, 18), end_ms: T(24, 0, 18), state: 'done', place: '12 Example St, Victoria', kept_place: home },
  ],
  labels: [], missing: [], line: null, highlights: [],
});

// ---------- the unread letters: six came today (two want a reply, one is an ad), one is from yesterday ----------
const mail = (id, from, subject, received, extra = {}) => ({ id, from, address: `${id}@example.com`, thread_id: `th-${id}`, subject, received, reply: null, junk: false, importance: 1, category: null, ...extra });
const LETTERS = [
  mail('m-nw', 'Northwind Recruiting', 'Interview times — please confirm', iso(9, 12), { reply: 'yes' }),
  mail('m-reg', 'UVic Registrar', 'Your fall schedule', iso(11, 2), { reply: 'fyi' }),
  mail('m-ad', 'ShopDeals', 'Everything 40% off this week', iso(13, 20), { junk: true }),
  mail('m-mom', '妈妈', '周末回家吃饭吗？', iso(15, 5), { reply: 'yes' }),
  mail('m-gh', 'GitHub', '[jarvis] Run failed: verify', iso(16, 30), { reply: 'fyi' }),
  mail('m-lib', 'UVic Library', 'Book due Friday', iso(18, 40), { reply: 'fyi' }),
  mail('m-old', 'Studio Lin', 'Invoice 1042', iso(14, 25, 18), { reply: 'fyi' }),
];
const OF_TODAY = LETTERS.filter(l => l.id !== 'm-old'), AT = Object.fromEntries(LETTERS.map(l => [l.id, Date.parse(l.received)]));
const KEYS_TODAY = ['m-nw', 'm-reg', 'm-ad', 'm-mom', 'm-gh', 'm-lib', 'm-old'];

// ---------- the built page ----------
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
// CHROMIUM names a browser to use; else the one a container keeps for Playwright; else this Mac's Chrome.
const exe = [process.env.CHROMIUM, '/opt/pw-browsers/chromium'].find(p => p && existsSync(p));
const browser = await chromium.launch({ headless: true, ...exe ? { executablePath: exe } : { channel: 'chrome' }, args: ['--disable-web-security', '--mute-audio'] });

// Every Web Animation the page starts is written down (what it ran on, its keyframes and timing), so what was asked for can be read.
const record = () => {
  const animate = Element.prototype.animate;
  window.__anims = [];
  Element.prototype.animate = function (kf, opt) {
    window.__anims.push({ cls: String(this.className?.baseVal ?? this.className), kf: JSON.parse(JSON.stringify(kf)), opt: typeof opt === 'number' ? { duration: opt } : JSON.parse(JSON.stringify(opt ?? {})) });
    return animate.call(this, kf, opt);
  };
};
// A page on the fake daemon. `s.posts`: what the page sent to the mail routes; `s.refuse`: mail acts that answer with an error; `s.gone`: letters the daemon no longer lists.
async function boot({ lang = 'zh', mail: mail_ = 'ok', animations = false, reduced = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const s = { posts: [], refuse: new Set(), gone: new Set(), mail: mail_ };
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
      openMail: async id => { (window.__gmail ??= []).push(id); return true; },
    };
    window.WebSocket = class { constructor() { setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
  }, [{ lang }]);
  await page.route(`${daemon}/**`, async route => {
    const req = route.request(), url = new URL(req.url()), post = req.method() === 'POST', p = url.pathname;
    const json = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    let m;
    if (p === '/inherent/day') {
      const date = url.searchParams.get('date');
      const no = Number(date?.slice(-2));   // any other day is an empty one of its own
      return json(!date || date === DATE ? todayReply() : date === YESTERDAY ? yesterdayReply() : { ...yesterdayReply(), date, start_ms: T(0, 0, no), end_ms: T(24, 0, no), items: [] });
    }
    // `s.mail`: how the list is answered: 'slow' (after 4 s, inside the page's 5 s limit), 'fail' (a 500), 'missing' (a 404, a brain that does not serve it)
    if (p === '/inherent/mail' && !post) {
      if (s.mail === 'slow') await new Promise(r => setTimeout(r, 4000));
      if (s.mail === 'fail') return json({ detail: 'down' }, 500);
      if (s.mail === 'missing') return json({ detail: 'Not Found' }, 404);
      return json({ unread: LETTERS.filter(l => !s.gone.has(l.id)) });
    }
    if (post && (m = p.match(/^\/inherent\/mail\/(archive|trash|read|unarchive|untrash|unread)$/))) {
      const ids = JSON.parse(req.postData() ?? '{}').ids ?? [];
      s.posts.push({ path: p, ids });
      if (s.refuse.has(m[1])) return json({ detail: 'refused' }, 500);
      for (const id of ids) (/^un/.test(m[1]) ? s.gone.delete(id) : s.gone.add(id));
      return json({ ok: true });
    }
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
  if (animations) await page.addInitScript(record);
  if (reduced) await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.clock.setFixedTime(new Date(NOW));
  await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${daemonPort}`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
  // The native bridge tells the page where the pointer is; a test moves the real one and tells the page the same.
  const move = async (x, y) => { await page.mouse.move(x, y); await page.evaluate(([x, y]) => window.__cursor({ x, y }), [x, y]); };
  const read = () => page.evaluate(() => { const n = document.querySelector('.notch'); return { tl: n.dataset.tl, door: n.querySelector('.dt')?.dataset.door ?? null }; });
  const until = (fn, arg, timeout = 5000) => page.waitForFunction(fn, arg, { timeout });
  const lineX = async () => { const at = await page.evaluate(() => document.querySelector('.notch').dataset.lineAt), [x0, x1] = at.split('-').map(Number); return (x0 + x1) / 2; };
  const press = async (x, y) => { await move(x, y); await page.mouse.down(); await page.mouse.up(); };
  const rect = sel => page.evaluate(sel => { const e = document.querySelector(sel); return e ? e.getBoundingClientRect().toJSON() : null; }, sel);
  const pressOn = async sel => { const r = await rect(sel); assert.ok(r, `${sel} is on the page`); await press(r.x + r.width / 2, r.y + r.height / 2); };
  const hoverOn = async sel => { const r = await rect(sel); assert.ok(r, `${sel} is on the page`); await move(r.x + r.width / 2, r.y + r.height / 2); };
  const texts = sel => page.evaluate(sel => [...document.querySelectorAll(sel)].map(e => (e.innerText || e.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim()), sel);
  const keys = () => page.evaluate(() => [...document.querySelectorAll('.notch .dt-row[data-key]')].map(e => e.dataset.key));
  const door = async key => { await pressOn(`.notch .dt-door[data-door="${key}"]`); await until(key => document.querySelector('.notch .dt')?.dataset.door === key, key); await page.waitForTimeout(420); };
  // The day pane opened by a click on the wing's line (it stays up), with the rows drawn and settled.
  const openDay = async () => {
    await until(() => document.querySelector('.notch')?.dataset.line);
    await press(await lineX(), 14);
    await until(() => document.querySelector('.notch-drop.is-open .dt-row'), undefined, 8000);
    await page.waitForTimeout(1000);
  };
  // Where the frame's parts are: the pane (the dialog, its inside, the black shape), the day inside it, and what must never move in it.
  const geo = () => page.evaluate(() => {
    const r = e => { const b = e?.getBoundingClientRect(); return b ? b.toJSON() : null; }, q = s => document.querySelector(s), dt = q('.notch .dt');
    return { drop: r(q('.notch-drop')), inner: r(q('.notch-pane-in')), shape: r(q('.notch-shape path')), dt: r(dt), head: r(dt.querySelector('.dt-head')), bar: r(dt.querySelector('.dt-bar')), track: r(dt.querySelector('.dt-track')), doors: r(dt.querySelector('.dt-doors')), from: dt.querySelector('.dt-bar')?.dataset.from ?? null };
  });
  // The marks of the page on the bar: key, tone, and the x of the middle of each.
  const marks = () => page.evaluate(() => [...document.querySelectorAll('.notch .dt-track .dt-mk.is-point')].map(e => { const b = e.getBoundingClientRect(); return { key: e.dataset.mark, tone: ['on', 'dim', 'amber'].find(t => e.classList.contains(`is-${t}`)), x: b.left + b.width / 2, muted: e.classList.contains('is-muted') }; }));
  const shot = async (name, g) => page.screenshot({ path: path.join(out, `${name}.png`), clip: { x: 60, y: 0, width: 520, height: Math.ceil(Math.max(g.drop.bottom, g.dt.bottom)) + 2 } });
  return { page, ctx, s, move, read, until, press, pressOn, hoverOn, texts, keys, door, openDay, geo, marks, rect, shot };
}
// What moved between two looks at the frame: the sizes of the pane and its black shape, and the places of the head, the bar and the doors.
const moved = (g0, g1) => [['pane', 'drop', 'height'], ['pane inside', 'inner', 'height'], ['shape', 'shape', 'height'], ['day', 'dt', 'height'], ['day', 'dt', 'top'], ['head', 'head', 'top'], ['bar', 'bar', 'top'], ['doors', 'doors', 'top']]
  .filter(([, k, p]) => !same(g0[k][p], g1[k][p])).map(([name, k, p]) => `${name} ${p} ${g0[k][p]} -> ${g1[k][p]}`);
// The x of a moment on the bar the geometry describes (the bar runs from the hour in `data-from` to 24:00 of the day).
const xAt = (g, ms, day = 19) => { const [h, m] = g.from.split(':').map(Number), t0 = T(h, m, day), t1 = T(24, 0, day); return g.track.left + (ms - t0) / (t1 - t0) * g.track.width; };

try {
  for (let i = 0; i < 50; i++) { try { await fetch(`http://127.0.0.1:${web}/`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }

  {
    const a = await boot();
    await a.openDay();
    await a.until(() => document.querySelector('.notch .dt-door[data-door="mail"] em'));

    // ---- 今天: the frame ----
    const g0 = await a.geo();
    check('the pane opens on 今天: the doors 今天, 信 (with its unread count), 待办, Agents, 额度 and the dots, in that order, 今天 lit',
      (await a.texts('.notch .dt-door')).join('|') === '今天|信 6|待办|Agents|额度|更多' && await a.page.locator('.notch .dt-door[aria-current="page"]').getAttribute('data-door') === 'today');
    check('the doors row is the last thing in the day, on its bottom edge, under the bar', g0.dt.bottom - g0.doors.bottom <= 20 && g0.doors.top > g0.bar.bottom + 8 && g0.doors.height >= 24, JSON.stringify([g0.dt, g0.doors]));
    check('the day is one fixed size: 584 pt', same(g0.dt.height, 584), String(g0.dt.height));
    check('on 今天 the line is the day\'s sentence and the bar is the day, not a page', (await a.texts('.notch .dt-line'))[0].includes('今天一半在路上') && await a.page.locator('.notch .dt-bar.is-page').count() === 0 && await a.page.locator('.notch .dt-mk').count() === 0);
    await a.hoverOn('.notch .dt-head');
    await a.shot('today', g0);

    // ---- 信: the same frame, and the page ----
    await a.door('mail');
    const g1 = await a.geo();
    check('on 信 the pane, its shape, the day, the head, the bar and the doors row are where they were on 今天 and the same size', moved(g0, g1).length === 0, moved(g0, g1).join('; '));
    check('on 信 the door is lit, and the line is written from the facts: who wants a reply, and how many more are unread',
      await a.page.locator('.notch .dt-door[aria-current="page"]').getAttribute('data-door') === 'mail' && (await a.texts('.notch .dt-line'))[0] === '妈妈和 Northwind 的信要回，另外 4 封没看。', (await a.texts('.notch .dt-line')).join());
    check('the numbers row is the page\'s: 要回 2, 没看 6, 像广告 1, and no link (the app has no way to open the inbox)', (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '要回 2|没看 6|像广告 1' && await a.page.locator('.notch .dt-numbers .dt-go').count() === 0, (await a.texts('.notch .dt-numbers .dt-num')).join('|'));
    const shown = { keys: (await a.keys()).join(), heading: (await a.texts('.notch .dt-uh')).join(), at: (await a.texts('.notch .dt-row .dt-at')).join('|') };
    check('the rows: this day\'s letters oldest first, then the earlier unread ones after their heading, with the date in the time column',
      shown.keys === KEYS_TODAY.join() && shown.heading === '更早没看的' && shown.at === '09:12|11:02|13:20|15:05|16:30|18:40|10/18', JSON.stringify(shown));
    const order = await a.page.evaluate(() => [...document.querySelectorAll('.notch .dt-list .dt-row, .notch .dt-list .dt-now, .notch .dt-list .dt-uh')].map(e => e.dataset.key ?? e.className.replace(/\bdt-|\s.*/g, '')));
    check('the now line is after the last letter of the day and before the heading', order.join() === [...KEYS_TODAY.slice(0, 6), 'now', 'uh', 'm-old'].join(), order.join());
    const anatomy = await a.page.evaluate(() => Object.fromEntries([...document.querySelectorAll('.notch .dt-row[data-key]')].map(r => [r.dataset.key, {
      ic: r.querySelector('.dt-ic')?.textContent, who: r.querySelector('.dt-ic')?.classList.contains('is-who'), title: r.querySelector('.dt-ttl b')?.textContent, tag: r.querySelector('.dt-ttl i')?.textContent ?? null,
      amber: !!r.querySelector('.dt-ttl i.dt-wait'), sub: r.querySelector('.dt-tx em')?.textContent ?? null, read: r.classList.contains('is-read'), quick: [...r.querySelectorAll('.dt-quick button')].map(b => b.textContent) }])));
    const want = (ic, title, tag, sub, extra = {}) => JSON.stringify({ ic, who: true, title, tag, amber: !!tag, sub, read: false, quick: ['打开'], ...extra });
    check('a letter\'s row: the sender\'s first letter in the circle, the sender, the amber 要回 on the two that want a reply, the subject under it, 打开 as its quick button',
      JSON.stringify(anatomy['m-nw']) === want('N', 'Northwind Recruiting', '要回', 'Interview times — please confirm') && JSON.stringify(anatomy['m-mom']) === want('妈', '妈妈', '要回', '周末回家吃饭吗？')
      && JSON.stringify(anatomy['m-reg']) === want('U', 'UVic Registrar', null, 'Your fall schedule') && JSON.stringify(anatomy['m-old']) === want('S', 'Studio Lin', null, 'Invoice 1042'), JSON.stringify(anatomy));
    check('an ad is faded, says so before its subject, and its quick button is 归档', JSON.stringify(anatomy['m-ad']) === want('S', 'ShopDeals', null, '像广告 · Everything 40% off this week', { read: true, quick: ['归档'] }), JSON.stringify(anatomy['m-ad']));

    // ---- 信: the bar in page mode ----
    const mk = await a.marks();
    check('the bar is in page mode: what he was doing steps back to .25 and the page lights its own moments', await a.page.locator('.notch .dt-bar.is-page').count() === 1
      && await a.page.evaluate(() => { const e = document.querySelector('.notch .dt-bar.is-page .dt-seg, .notch .dt-bar.is-page .dt-ground'); return !!e && getComputedStyle(e).opacity === '0.25'; }));
    check('one point on the bar for each letter of the day (the one from yesterday has none), each at its minute: 09:12 11:02 13:20 15:05 16:30 18:40',
      mk.length === 6 && OF_TODAY.every(l => { const m = mk.find(x => x.key === l.id); return m && same(m.x, xAt(g1, AT[l.id]), 1); }), JSON.stringify([mk, g1.from, g1.track]));
    check('amber for the two that want a reply, dim for the ad, the rest lit', mk.filter(m => m.tone === 'amber').map(m => m.key).sort().join() === 'm-mom,m-nw' && mk.filter(m => m.tone === 'dim').map(m => m.key).join() === 'm-ad' && mk.filter(m => m.tone === 'on').length === 3, JSON.stringify(mk));
    check('the amber is the app\'s amber (255 201 143), nothing else is', await a.page.evaluate(() => [...document.querySelectorAll('.notch .dt-mk.is-point')].every(e => (getComputedStyle(e).backgroundColor === 'rgb(255, 201, 143)') === e.classList.contains('is-amber'))));
    await a.hoverOn('.notch .dt-head');
    await a.shot('mail', g1);

    // ---- pointer on a row, and on a mark ----
    await a.page.evaluate(() => document.querySelector('.notch .dt-row[data-key="m-reg"]').scrollIntoView({ block: 'nearest' }));
    await a.hoverOn('.notch .dt-row[data-key="m-reg"]'); await a.page.waitForTimeout(250);
    const hot = await a.page.evaluate(() => ({ hot: [...document.querySelectorAll('.notch .dt-row.is-hot')].map(e => e.dataset.key), brackets: [...document.querySelectorAll('.notch .dt-bracket')].map(e => { const b = e.getBoundingClientRect(); return { x: b.left + b.width / 2, w: b.width }; }),
      times: [...document.querySelectorAll('.notch .dt-scrub .dt-time:not(.is-now)')].map(e => e.innerText) }));
    check('the pointer on a letter\'s row brackets its mark (a 10 pt box centred on it) and shows its time above the bar', hot.hot.join() === 'm-reg' && hot.brackets.length === 1 && same(hot.brackets[0].w, 10, .5) && same(hot.brackets[0].x, xAt(g1, AT['m-reg']), 1) && hot.times.join() === '11:02', JSON.stringify(hot));
    check('the now label stays while the pointer is on a row', await a.page.locator('.notch .dt-scrub .dt-time.is-now').count() === 1);
    await a.shot('mail-hover', g1);
    const y = g1.track.top + g1.track.height / 2;
    await a.move(xAt(g1, AT['m-mom']) + 3, y); await a.page.waitForTimeout(250);
    const onBar = await a.page.evaluate(() => ({ hot: [...document.querySelectorAll('.notch .dt-row.is-hot')].map(e => e.dataset.key), brackets: document.querySelectorAll('.notch .dt-bracket').length, times: [...document.querySelectorAll('.notch .dt-scrub .dt-time:not(.is-now)')].map(e => e.innerText) }));
    check('the pointer within 8 pt of a mark on the bar lights that letter\'s row and brackets the mark; the bar\'s own time follows the pointer, as on 今天', onBar.hot.join() === 'm-mom' && onBar.brackets === 1 && onBar.times.length === 1, JSON.stringify(onBar));
    await a.press(xAt(g1, AT['m-lib']) - 2, y); await a.page.waitForTimeout(500);
    const picked = await a.page.evaluate(() => ({ pick: [...document.querySelectorAll('.notch .dt-row.is-pick')].map(e => e.dataset.key), open: !!document.querySelector('.notch .dt-page[data-page]') }));
    check('a click on a mark picks its row (and brings it into view) without opening it', picked.pick.join() === 'm-lib' && !picked.open && await a.page.evaluate(() => { const l = document.querySelector('.notch .dt-list').getBoundingClientRect(), r = document.querySelector('.notch .dt-row[data-key="m-lib"]').getBoundingClientRect(); return r.top >= l.top - 1 && r.bottom <= l.bottom + 1; }), JSON.stringify(picked));
    await a.page.keyboard.press('ArrowUp'); await a.page.waitForTimeout(250);
    check('↑ walks the page\'s rows: the pick moves to the letter before', (await a.page.evaluate(() => [...document.querySelectorAll('.notch .dt-row.is-pick')].map(e => e.dataset.key))).join() === 'm-gh');
    await a.page.keyboard.press('Enter'); await a.page.waitForTimeout(700);

    // ---- a letter opens in the pane ----
    const opened = await a.page.evaluate(() => { const p = document.querySelector('.notch .dt-page[data-page]'); return p ? { title: p.querySelector('.dt-ph b')?.textContent, subject: p.querySelector('.mp-subject')?.textContent, back: !!p.querySelector('.dt-pb'), acts: [...p.querySelectorAll('.mp-bar [data-act]')].map(e => e.dataset.act).join() } : null; });
    await a.until(() => /The letter itself/.test(document.querySelector('.notch .dt-page[data-page] .mp-body')?.textContent ?? ''));
    check('Enter opens the picked letter in the pane: the Dashboard\'s letter (its sender, subject and acts) under a ‹ head', opened && opened.title === 'GitHub' && opened.subject === '[jarvis] Run failed: verify' && opened.back && opened.acts === 'draft,gmail,read,trash,archive', JSON.stringify(opened));
    const g2 = await a.geo();
    check('and the pane is the size it was', moved(g1, g2).length === 0, moved(g1, g2).join('; '));
    await a.hoverOn('.notch .dt-head'); await a.page.waitForTimeout(200);
    await a.shot('mail-letter', g2);
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(600);
    check('Esc closes the letter first: the list is back on 信', (await a.read()).door === 'mail' && await a.page.locator('.notch .dt-page[data-page]').count() === 0 && (await a.keys()).length === 7);

    // ---- a number that is a lens ----
    await a.hoverOn('.notch .dt-num[data-chip="reply"]'); await a.page.waitForTimeout(300);
    const lens = async () => ({ off: await a.page.evaluate(() => [...document.querySelectorAll('.notch .dt-row.is-off')].map(e => e.dataset.key)), muted: (await a.marks()).filter(m => m.muted).map(m => m.key).sort() });
    let l = await lens();
    check('the pointer on 要回 previews it: the rows that are not those two step back, and so do their marks', l.off.join() === 'm-reg,m-ad,m-gh,m-lib,m-old' && l.muted.join() === 'm-ad,m-gh,m-lib,m-reg', JSON.stringify(l));
    await a.press(...await a.rect('.notch .dt-num[data-chip="reply"]').then(r => [r.x + r.width / 2, r.y + r.height / 2]));
    await a.hoverOn('.notch .dt-head'); await a.page.waitForTimeout(300);
    l = await lens();
    check('a click pins it: it stays when the pointer goes', l.off.length === 5 && await a.page.locator('.notch .dt-num[data-chip="reply"][aria-pressed="true"]').count() === 1);
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(300);
    l = await lens();
    check('Esc lets go of it, and stays on 信', l.off.length === 0 && l.muted.length === 0 && (await a.read()).door === 'mail');

    // ---- a range of the day cuts the page ----
    const dragFrom = xAt(g1, T(10)) + .1, dragTo = xAt(g1, T(14)) + .1;
    await a.move(dragFrom, y); await a.page.mouse.down(); await a.move((dragFrom + dragTo) / 2, y); await a.move(dragTo, y); await a.page.waitForTimeout(150); await a.page.mouse.up(); await a.page.waitForTimeout(300);
    await a.pressOn('.notch .dt-card.is-range .dt-cbtn'); await a.page.waitForTimeout(450);
    const cut = { keys: await a.keys(), chip: (await a.texts('.notch .dt-filter'))[0], heads: await a.page.locator('.notch .dt-uh').count() };
    check('a dragged range and 只看这段 cut the page to the letters in it (11:02 and 13:20), the heading of the earlier ones goes, and a 只看 10:00–14:00 chip says so', cut.keys.join() === 'm-reg,m-ad' && cut.chip === '只看 10:00–14:00' && cut.heads === 0, JSON.stringify(cut));
    check('the cut does not move the frame', moved(g1, await a.geo()).length === 0);
    await a.pressOn('.notch .dt-filter button'); await a.page.waitForTimeout(450);
    check('the chip gives the whole day back', (await a.keys()).join() === KEYS_TODAY.join() && await a.page.locator('.notch .dt-filter').count() === 0);

    // ---- an ad is archived from its row, and taken back ----
    a.s.refuse.add('archive');
    await a.page.evaluate(() => document.querySelector('.notch .dt-row[data-key="m-ad"]').scrollIntoView({ block: 'nearest' }));
    await a.hoverOn('.notch .dt-row[data-key="m-ad"]'); await a.page.waitForTimeout(200);
    await a.pressOn('.notch .dt-row[data-key="m-ad"] .dt-quick button'); await a.page.waitForTimeout(500);
    check('an archive the daemon refuses leaves the letter where it was and says so in the line', (await a.keys()).join() === KEYS_TODAY.join() && (await a.texts('.notch .dt-line'))[0] === '归档没成功，请再试一次。' && a.s.posts.at(-1).path === '/inherent/mail/archive', (await a.texts('.notch .dt-line')).join());
    await a.until(() => !/归档没成功/.test(document.querySelector('.notch .dt-line')?.textContent ?? ''), undefined, 7000);
    check('and a few seconds later the line is the page\'s own again', (await a.texts('.notch .dt-line'))[0] === '妈妈和 Northwind 的信要回，另外 4 封没看。', (await a.texts('.notch .dt-line')).join());
    a.s.refuse.clear();
    await a.page.evaluate(() => document.querySelector('.notch .dt-row[data-key="m-ad"]').scrollIntoView({ block: 'nearest' }));
    await a.hoverOn('.notch .dt-row[data-key="m-ad"]'); await a.page.waitForTimeout(200);
    await a.pressOn('.notch .dt-row[data-key="m-ad"] .dt-quick button'); await a.page.waitForTimeout(500);
    const gone = { keys: await a.keys(), tomb: await a.texts('.notch .dt-row[data-key="gone:m-ad"] .dt-tx'), button: await a.texts('.notch .dt-row[data-key="gone:m-ad"] button'), chips: await a.texts('.notch .dt-numbers .dt-num'), marks: (await a.marks()).length };
    check('归档 on the ad POSTs its id to /inherent/mail/archive; its row becomes a line that says what was done, with 撤销; the 像广告 number and the ad\'s mark are gone',
      JSON.stringify(a.s.posts.at(-1)) === JSON.stringify({ path: '/inherent/mail/archive', ids: ['m-ad'] }) && gone.keys.join() === 'm-nw,m-reg,gone:m-ad,m-mom,m-gh,m-lib,m-old' && gone.tomb[0] === 'ShopDeals 已归档 1 封 · 仍在“所有邮件”里' && gone.button.join() === '撤销'
      && gone.chips.join('|') === '要回 2|没看 6' && gone.marks === 5, JSON.stringify(gone));
    await a.pressOn('.notch .dt-row[data-key="gone:m-ad"] button'); await a.page.waitForTimeout(700);
    const back = { keys: await a.keys(), chips: await a.texts('.notch .dt-numbers .dt-num'), marks: (await a.marks()).length };
    check('撤销 POSTs /inherent/mail/unarchive and the letter, its number and its mark are back', JSON.stringify(a.s.posts.at(-1)) === JSON.stringify({ path: '/inherent/mail/unarchive', ids: ['m-ad'] }) && back.keys.join() === KEYS_TODAY.join() && back.chips.join('|') === '要回 2|没看 6|像广告 1' && back.marks === 6, JSON.stringify(back));

    // ---- a letter acted on from its own page: the page goes, the row stays a moment and says what was done ----
    await a.page.evaluate(() => document.querySelector('.notch .dt-row[data-key="m-reg"]').scrollIntoView({ block: 'nearest' }));
    await a.pressOn('.notch .dt-row[data-key="m-reg"] .dt-tx');
    await a.until(() => /The letter itself/.test(document.querySelector('.notch .dt-page[data-page] .mp-body')?.textContent ?? '')); await a.page.waitForTimeout(500);
    await a.pressOn('.notch .dt-page[data-page] .mp-bar [data-act="read"]'); await a.page.waitForTimeout(900);
    const read = { page: await a.page.locator('.notch .dt-page[data-page]').count(), keys: await a.keys(), tomb: await a.texts('.notch .dt-row[data-key="gone:m-reg"] .dt-tx'), chips: await a.texts('.notch .dt-numbers .dt-num'), count: (await a.texts('.notch .dt-door[data-door="mail"]'))[0] };
    check('「标为已读」 on the letter\'s own page POSTs /inherent/mail/read; the page slides back to the list, the letter\'s row says 已标为已读 with 撤销, and the numbers and the door count it as gone',
      JSON.stringify(a.s.posts.at(-1)) === JSON.stringify({ path: '/inherent/mail/read', ids: ['m-reg'] }) && read.page === 0 && read.keys.join() === 'm-nw,gone:m-reg,m-ad,m-mom,m-gh,m-lib,m-old' && read.tomb[0] === 'UVic Registrar 已标为已读'
      && read.chips.join('|') === '要回 2|没看 5|像广告 1' && read.count === '信 5', JSON.stringify([read, a.s.posts.slice(-2)]));
    await a.pressOn('.notch .dt-row[data-key="gone:m-reg"] button'); await a.page.waitForTimeout(700);
    check('撤销 POSTs /inherent/mail/unread and the letter is back as it was', JSON.stringify(a.s.posts.at(-1)) === JSON.stringify({ path: '/inherent/mail/unread', ids: ['m-reg'] }) && (await a.keys()).join() === KEYS_TODAY.join() && (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '要回 2|没看 6|像广告 1');

    // ---- ← and → keep the door ----
    await a.hoverOn('.notch .dt-head');
    const before = await a.geo();
    await a.page.keyboard.press('ArrowLeft');
    await a.until(() => [...document.querySelectorAll('.notch .dt-row[data-key]')].map(e => e.dataset.key).join() === 'm-old', undefined, 6000); await a.page.waitForTimeout(500);
    const g3 = await a.geo(), y1 = await a.marks();
    check('← goes to the day before and stays on 信: only the letter that came on that day, and the line says so', (await a.read()).door === 'mail' && (await a.texts('.notch .dt-line'))[0] === '这天到的信，还有 1 封没看。' && (await a.texts('.notch .dt-date'))[0] === '10月18日 周日' && await a.page.locator('.notch .dt-doors .dt-door.is-on[data-door="mail"]').count() === 1, (await a.texts('.notch .dt-line')).join());
    check('and one point on that day\'s bar, at 14:25, and one number: 没看 1', y1.length === 1 && y1[0].key === 'm-old' && same(y1[0].x, xAt(g3, AT['m-old'], 18), 1) && (await a.texts('.notch .dt-numbers .dt-num')).join('|') === '没看 1', JSON.stringify([y1, g3.from]));
    check('the pane is the size it was after ←, and the head, the bar and the doors are where they were', moved(before, g3).length === 0, moved(before, g3).join('; '));
    await a.page.keyboard.press('ArrowRight');
    await a.until(() => document.querySelectorAll('.notch .dt-row[data-key]').length === 7, undefined, 6000); await a.page.waitForTimeout(500);
    const g4 = await a.geo();
    check('→ is back on this day, still on 信, nothing moved', (await a.read()).door === 'mail' && (await a.keys()).join() === KEYS_TODAY.join() && moved(before, g4).length === 0, moved(before, g4).join('; '));

    // ---- a day with nothing recorded and no letters: no bar to light, still the same frame ----
    await a.page.keyboard.press('ArrowLeft');
    await a.until(() => document.querySelector('.notch .dt-date')?.textContent === '10月18日 周日' && document.querySelectorAll('.notch .dt-row[data-key]').length === 1, undefined, 6000);
    await a.page.keyboard.press('ArrowLeft');
    await a.until(() => document.querySelector('.notch .dt-date')?.textContent === '10月17日 周六' && !document.querySelector('.notch .dt-row[data-key]'), undefined, 6000); await a.page.waitForTimeout(500);
    const g5 = await a.geo(), none = { say: await a.texts('.notch .dt-say'), line: await a.texts('.notch .dt-line'), chips: await a.texts('.notch .dt-numbers .dt-num'), bar: await a.page.locator('.notch .dt-bar').count() };
    check('a day with no letters says so, the line says they are all read, the one number is 没看 0, and with no bar there is nothing to light', none.say.join() === '这天没有没看的信' && none.line.join() === '这天到的信，都看过了。' && none.chips.join('|') === '没看 0' && none.bar === 0, JSON.stringify(none));
    check('and the pane, the head and the doors row have not moved for it', g5.dt.height === before.dt.height && g5.drop.height === before.drop.height && same(g5.doors.top, before.doors.top) && same(g5.head.top, before.head.top), moved(before, { ...g5, bar: before.bar }).join('; '));
    await a.page.keyboard.press('ArrowRight'); await a.page.keyboard.press('ArrowRight');
    await a.until(() => document.querySelector('.notch .dt-date')?.textContent === '10月19日 周一' && document.querySelectorAll('.notch .dt-row[data-key]').length === 7, undefined, 6000); await a.page.waitForTimeout(500);

    // ---- every door is the same frame ----
    const seen = [];
    for (const key of ['todo', 'agents', 'quota', 'more', 'today']) {
      await a.door(key);
      const g = await a.geo(), bad = moved(g0, g);
      if (bad.length || (key !== 'today') !== (await a.page.locator('.notch .dt-bar.is-page').count() === 1)) seen.push(`${key}: ${bad.join(', ') || 'bar mode'}`);
    }
    check('on every other door the pane, the head, the bar and the doors row are where they are on 今天, and the bar is a page\'s (the day itself on 今天)', seen.length === 0, seen.join('; '));

    // ---- Esc ----
    await a.door('mail');
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(500);
    let r = await a.read();
    check('Esc on 信, with nothing else to let go of, goes back to 今天 and the pane stays', r.door === 'today' && r.tl !== '', JSON.stringify(r));
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(900);
    r = await a.read();
    check('a second Esc folds the pane', r.tl === '', JSON.stringify(r));
    await a.ctx.close();
  }

  // ---- the switch: the new page fades in, nothing slides ----
  for (const reduced of [false, true]) {
    const a = await boot({ animations: true, reduced });
    await a.openDay();
    await a.page.evaluate(() => { window.__anims.length = 0; });
    await a.door('mail');
    const ran = await a.page.evaluate(() => window.__anims.slice());
    const fades = ran.filter(x => /\bdt-(line|numbers|stage)\b/.test(x.cls)), still = ran.filter(x => /\bdt-(head|doors|bar)\b/.test(x.cls));
    const ease = 'cubic-bezier(.16, 1, .3, 1)';
    check(reduced ? 'under reduced motion the new page only fades in: 120 ms, linear, no rise'
      : 'a door fades the new line, numbers row and rows in over 180 ms on the pane\'s ease, rising 4 px; what it replaces is gone at once',
      fades.length === 3 && fades.every(x => x.opt.duration === (reduced ? 120 : 180) && x.opt.easing === (reduced ? 'linear' : ease) && x.kf[0].opacity === 0 && (reduced ? x.kf[0].transform === undefined : x.kf[0].transform === 'translateY(4px)')), JSON.stringify(fades));
    check(`and nothing else moves ${reduced ? '' : '(no slide: the head, the bar and the doors row are not animated, and no animation goes sideways)'}`.trim(), still.length === 0 && ran.every(x => !JSON.stringify(x.kf).includes('translateX')), JSON.stringify([still, ran.filter(x => JSON.stringify(x.kf).includes('translateX'))]));
    await a.ctx.close();
  }

  // ---- a page that has no letters to show says why ----
  for (const [mode, say] of [['slow', '读着…'], ['fail', '没读到这一页，会再试一次'], ['missing', '这台 Jarvis 还不会给信']]) {
    const a = await boot({ mail: mode });
    await a.openDay();
    const on = await a.geo();
    await a.door('mail');
    const now = await a.geo();
    check(`${{ slow: 'while the letters are being read', fail: 'when the daemon cannot give them', missing: 'when this daemon has no such route' }[mode]} the page says ${say}, has no line and no rows, and the frame has not moved`,
      (await a.texts('.notch .dt-say')).join() === say && (await a.texts('.notch .dt-line'))[0] === '' && (await a.keys()).length === 0 && moved(on, now).length === 0, `${(await a.texts('.notch .dt-say')).join()} ${moved(on, now).join('; ')}`);
    if (mode === 'slow') {
      await a.until(() => document.querySelectorAll('.notch .dt-row[data-key]').length === 7, undefined, 10000);
      check('and when they come the rows are drawn where the words were', (await a.texts('.notch .dt-say')).length === 0);
    }
    await a.ctx.close();
  }

  // ---- the same page in English ----
  {
    const a = await boot({ lang: 'en' });
    await a.openDay();
    await a.until(() => document.querySelector('.notch .dt-door[data-door="mail"] em'));
    await a.door('mail');
    check('in English the doors are Today, Mail, To-dos, Agents, Usage and the dots; the line, the numbers and the heading follow',
      (await a.texts('.notch .dt-door')).join('|') === 'Today|Mail 6|To-dos|Agents|Usage|More' && (await a.texts('.notch .dt-line'))[0] === '妈妈 and Northwind want a reply; 4 more unread.'
      && (await a.texts('.notch .dt-numbers .dt-num')).join('|') === 'Reply 2|Unread 6|Ads 1' && (await a.texts('.notch .dt-uh')).join() === 'Earlier, unread', `${(await a.texts('.notch .dt-door')).join('|')} ${(await a.texts('.notch .dt-line'))[0]} ${(await a.texts('.notch .dt-numbers .dt-num')).join('|')}`);
    check('an ad says so in English, and its quick button is Archive', (await a.texts('.notch .dt-row[data-key="m-ad"] .dt-tx em'))[0] === 'Looks like an ad · Everything 40% off this week' && (await a.page.locator('.notch .dt-row[data-key="m-ad"] .dt-quick button').textContent()) === 'Archive');
    await a.ctx.close();
  }

  check('no page error from any of it', errors.length === 0, errors.join('; '));
  console.log(`\n${checks.length} checks passed; screenshots in ${out}`);
} finally {
  await browser.close();
  server.kill();
}
if (errors.length) { console.error('page errors:', errors); process.exit(1); }

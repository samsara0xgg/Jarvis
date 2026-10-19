// Records how the Mac day pane moves, frame by frame: an audit, not a test. It makes no assertions and changes nothing in src/.
// Headless Chrome (deviceScaleFactor 2) against the built page, the native bridge stubbed and the same fake daemon and fixture day as
// verify-notch-timeline.mjs (boot() and the day are copied from there; that script is untouched). Each scene is run twice from a fresh boot:
//   pass "numbers": a per-frame sampler only (the pane's box, the list's scrollTop, every row's box, the animations running, long tasks, frame gaps),
//     so the screencast's own cost cannot show up as dropped frames;
//   pass "frames": the same sampler plus a CDP screencast, for the contact sheets.
// Run after `npm run build`: COMPANION_PORT=5799 COMPANION_DAEMON_PORT=8799 CHROMIUM="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" node scripts/capture-pane-motion.mjs
// ONLY=2,5 runs those scenes; PASSES=numbers|frames runs one pass; EXPLORE=1 prints the list's rows and stops; SUFFIX=-after names the takes <take>-after.
// Output: .claude/dispatch/shots-motion/<take>.png (contact sheet) and <take>.json (raw numbers + a summary) per take.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { deflateSync, crc32 } from 'node:zlib';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const out = process.env.COMPANION_EVIDENCE_DIR ?? path.resolve(root, '../../.claude/dispatch/shots-motion');
mkdirSync(out, { recursive: true });
const web = Number(process.env.COMPANION_PORT ?? 5799), daemonPort = Number(process.env.COMPANION_DAEMON_PORT ?? 8799), daemon = `http://127.0.0.1:${daemonPort}`;
const only = (process.env.ONLY ?? '').split(',').filter(Boolean), passes = process.env.PASSES ? [process.env.PASSES] : ['numbers', 'frames'];
const wanted = n => !only.length || only.includes(String(n));
const suffix = process.env.SUFFIX ?? '';
const wait = ms => new Promise(r => setTimeout(r, ms));

// ---------- the fixture day (copied from verify-notch-timeline.mjs) ----------
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
    it('reminder-2', 'reminder', T(9, 30), null, { text: '给银行打电话', fired: true, acknowledged: false }, 'her'),
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
    it('event-1', 'event', T(22, 30), T(23, 30), { title: '小组讨论', place: 'Library', private: false, source: 'mail', confirmed: false }),
    it('event-2', 'event', T(23, 40), T(23, 55), { title: 'Dr. appointment', source: 'said', confirmed: true, private: true, place: 'Clinic' }),
  ],
  labels: [{ title: '期中周', source: 'calendar' }], missing: ['mac'],
  line: { text: '今天一半在路上，一半在 UVic。晚上回家后，和你聊了两次。', draft: false, written_at: '2026-10-19T21:30:00-07:00' },
  highlights: [{ id: 'stay-uvic', label: '下午在 UVic', by: 'her' }],
  ...over,
});
// The same day with what the Mac and the imports bring in: two work blocks, a FaceTime call, a payment, a Claude chat and the night's letter
// on his Claude chats; and a second star, his, on the payment (the brain's default label, `kind HH:MM`, says nothing the row does not).
const richDay = (over = {}) => makeDay({
  missing: [],
  items: [
    ...makeDay().items,
    it('work-1', 'work', T(10, 30), T(11, 20), { active_s: 2520, projects: [{ name: 'Jarvis', seconds: 2100 }], apps: [{ name: 'Xcode', seconds: 1500 }, { name: 'Ghostty', seconds: 1200 }] }),
    it('work-2', 'work', T(19, 35), T(21, 40), { active_s: 6900, projects: [{ name: 'Jarvis', seconds: 6000 }], apps: [{ name: 'Ghostty', seconds: 4200 }, { name: 'Claude', seconds: 1800 }] }),
    it('call-1', 'call', T(16, 40), T(16, 58), { app: 'FaceTime' }),
    it('pay-1', 'payment', T(13, 20), T(13, 20), { source: 'alipay', amount: '12.40', currency: 'CAD', direction: 'out', counterparty: 'A&W', text: 'Teen Burger combo' }),
    it('chat-1', 'claude_chat', T(20, 10), T(20, 42), { source: 'claude', text: 'The bar for the day', messages: 14 }),
    it('claude-day-1', 'claude_day', T(9, 40), T(22, 5), { source: 'claude_daily', text: 'The bar for the day; A tool exam', chats: 2 }),
  ],
  highlights: [{ id: 'stay-uvic', label: '下午在 UVic', by: 'her' }, { id: 'pay-1', label: 'payment 13:20', by: 'him' }],
  ...over,
});
// A day of a few items, for the axis.
const at4 = n => T(Math.floor(n / 100), n % 100);
const NIGHT = [['core', 5], ['deep', 45], ['core', 145], ['awake', 155], ['rem', 225], ['core', 320], ['deep', 350], ['rem', 425], ['core', 510], ['awake', 520], ['core', 550], ['rem', 625], ['core', 650], ['awake', 700]]
  .map(([stage, end], i, list) => ({ stage, start_ms: i ? at4(list[i - 1][1]) : T(23, 10, 18), end_ms: at4(end) }));
const code = (n, account, from, to, title, project) => it(`code-s${n}`, 'code', at4(from), at4(to), { title, project, account, session_id: `s${n}`, detail: `${title} 的小结` });
const CODE = [code(1, 'a', 810, 905, 'Phase A 的 review 修复', 'jarvis'), code(2, 'b', 840, 935, 'iOS 时间线的列表', 'jarvis-ios'), code(3, 'a', 950, 1040, '给 verify 加 fixtures', 'jarvis'),
  code(4, 'b', 1020, 1105, '周报草稿', 'notes'), code(5, 'b', 1540, 1630, 'Notch 的 hover 卡', 'jarvis'), code(6, 'a', 1600, 1710, '读论文做笔记', 'thesis')];
const fullDay = (over = {}) => richDay({
  items: [
    ...richDay().items.filter(x => x.id !== 'sleep-1' && x.id !== 'stay-home-1'),
    it('sleep-1', 'sleep', T(23, 10, 18), T(7, 0), { minutes: 440, stages: [{ stage: 'in_bed', start_ms: T(23, 0, 18), end_ms: T(7, 10) }, ...NIGHT] }),
    it('stay-home-1', 'stay', T(7, 0), T(11, 25), { place: '12 Example St, Victoria', kept_place: home }),
    it('workout-1', 'workout', T(7, 20), T(7, 55), { activity: 'running', text: 'Outdoor Run', energy_kcal: 240, distance_m: 3200, avg_hr: 142 }),
    ...CODE,
  ],
  weather: { condition: 'partlyCloudy', high: 14.2, low: 7.6, sunrise_ms: T(7, 25), sunset_ms: T(18, 5) },
  summary: { steps: 8240, resting_hr: 58, hrv: 42 },
  ...over,
});
// A picture the fake daemon serves for an attachment id: 96 x 96, a warm gradient with a plate on it.
const PICTURE = (() => {
  const w = 96, h = 96, raw = Buffer.alloc((w * 3 + 1) * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const d = Math.hypot(x - 48, y - 52), plate = d < 34, rim = d < 38;
    const [r, g, b] = plate ? [240 - d, 196 - d * 1.2, 120 - d * .6] : rim ? [250, 244, 236] : [60 + x * .8, 90 + y * .6, 140 - y * .4];
    raw.set([r, g, b], y * (w * 3 + 1) + 1 + x * 3);
  }
  const chunk = (type, data) => { const len = Buffer.alloc(4), crc = Buffer.alloc(4), body = Buffer.concat([Buffer.from(type), data]); len.writeUInt32BE(data.length); crc.writeUInt32BE(crc32(body)); return Buffer.concat([len, body, crc]); };
  const head = Buffer.alloc(13); head.writeUInt32BE(w, 0); head.writeUInt32BE(h, 4); head[8] = 8; head[9] = 2;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', head), chunk('IDAT', deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
})();
const PIC = 'a'.repeat(32);
// A second, larger picture (640 x 420) for the one that has to be cut to 240 tall and shown large.
const BIG = 'c'.repeat(32);
const BIGPICTURE = (() => {
  const w = 640, h = 420, raw = Buffer.alloc((w * 3 + 1) * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const sky = y < 210, d = Math.hypot(x - 430, y - 150), sun = d < 54;
    const [r, g, b] = sun ? [255, 226, 160] : sky ? [70 + y * .35 + x * .05, 110 + y * .3, 190 - y * .2] : [50 + (y - 210) * .25, 96 + Math.sin(x / 40) * 16 + (y - 210) * .3, 70 + x * .03];
    raw.set([r, g, b], y * (w * 3 + 1) + 1 + x * 3);
  }
  const chunk = (type, data) => { const len = Buffer.alloc(4), crc = Buffer.alloc(4), body = Buffer.concat([Buffer.from(type), data]); len.writeUInt32BE(data.length); crc.writeUInt32BE(crc32(body)); return Buffer.concat([len, body, crc]); };
  const head = Buffer.alloc(13); head.writeUInt32BE(w, 0); head.writeUInt32BE(h, 4); head[8] = 8; head[9] = 2;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', head), chunk('IDAT', deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
})();
// The day's conversation as the records route gives it (oldest first, `ts` an ISO string): an earlier talk, the one at 10:02 (his first line, her
// long first reply, then more), and the one at 14:55 with one line of his; one record of the brain's own kind that is neither his nor hers is left out.
const iso = ms => new Date(ms).toISOString();
const records = () => [
  { id: 'r-1', ts: iso(T(8, 31)), who: 'user', text: '早' }, { id: 'r-2', ts: iso(T(8, 31) + 3000), who: 'jarvis', text: '早，今天降温，带件外套。' },
  { id: 'r-3', ts: iso(T(10, 2)), who: 'user', text: '帮我看看今天下午有什么安排，顺便提醒我给银行打电话\n这件事别忘了' },
  { id: 'r-4', ts: iso(T(10, 2) + 5000), who: 'jarvis', text: '下午三点有一个小组讨论，在图书馆；银行的电话我已经提醒过你一次，九点半的那条到现在还没有确认，需要的话我可以改成中午再提醒一次。' },
  { id: 'r-5', ts: iso(T(10, 4)), who: 'user', text: '好' }, { id: 'r-6', ts: iso(T(10, 4) + 2000), who: 'jarvis', text: '好的。' },
  { id: 'r-7', ts: iso(T(14, 55)), who: 'user', text: '27 路下一班什么时候' }, { id: 'r-8', ts: iso(T(14, 55) + 4000), who: 'jarvis', text: '还有 6 分钟。' },
  { id: 'r-9', ts: iso(T(19, 30)), who: 'user', text: '今天先到这里' },
];
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

const bigDay = () => fullDay({ items: [...fullDay().items, it('picture-2', 'picture', T(16, 20), T(16, 20), { attachment: BIG, note: '傍晚的海边，风很大' })] });
// The day before, as a full day (the same items a day earlier, all of them over): a brain that has a real yesterday.
const fullYesterday = () => {
  const d = bigDay(), D = 86_400_000, mv = x => x == null ? x : x - D;
  return { ...d, date: YESTERDAY, start_ms: mv(d.start_ms), end_ms: mv(d.end_ms), now_ms: NOW, highlights: [], weather: d.weather && { ...d.weather, sunrise_ms: mv(d.weather.sunrise_ms), sunset_ms: mv(d.weather.sunset_ms) },
    items: d.items.map(x => ({ ...x, start_ms: mv(x.start_ms), end_ms: x.end_ms == null ? mv(d.end_ms) : mv(x.end_ms), state: 'done', ...x.stages && { stages: x.stages.map(g => ({ ...g, start_ms: mv(g.start_ms), end_ms: mv(g.end_ms) })) } })) };
};

// ---------- in the page: the sampler ----------
// Runs once per page. cap.start() begins one sample per animation frame; cap.stop() returns them. A sample is compact: boxes are [left, top, width, height].
function installSampler() {
  const cap = window.__cap = { on: false, t0: 0, samples: [], inputs: [], marks: [], ptr: { x: -1, y: -1 }, long: [], ev: [], last: 0 };
  const lt = new PerformanceObserver(l => { for (const e of l.getEntries()) cap.long.push([e.startTime, e.duration]); });
  const et = new PerformanceObserver(l => { for (const e of l.getEntries()) cap.ev.push([e.name, e.startTime, e.duration, e.processingStart, e.processingEnd]); });
  try { lt.observe({ type: 'longtask' }); } catch { /* not supported */ }
  try { et.observe({ type: 'event', durationThreshold: 16 }); } catch { /* not supported */ }
  for (const type of ['pointerdown', 'pointerup', 'click', 'wheel', 'keydown']) window.addEventListener(type, e => { if (cap.on) cap.inputs.push([type, e.timeStamp - cap.t0]); }, true);
  const r1 = n => Math.round(n * 10) / 10;
  const R = el => { if (!el) return null; const r = el.getBoundingClientRect(); return [r1(r.left), r1(r.top), r1(r.width), r1(r.height)]; };
  const q = s => document.querySelector(s);
  const sample = now => {
    const m0 = performance.now(), drop = q('.notch-drop'), inn = drop?.querySelector('.notch-pane-in'), list = q('.dt-list'), s = { t: r1(now - cap.t0), gap: cap.last ? r1(now - cap.last) : 0 };
    cap.last = now;
    s.pane = R(drop); s.vis = drop && getComputedStyle(drop).visibility === 'visible' ? 1 : 0; s.open = drop?.classList.contains('is-open') ? 1 : 0;
    s.inn = R(inn); s.op = inn ? Math.round(+getComputedStyle(inn).opacity * 100) / 100 : null; s.dt = R(q('.dt'));
    s.head = R(q('.dt-head')); s.line = R(q('.dt-line')); s.bar = R(q('.dt-bar')); s.nums = R(q('.dt-numbers')); s.card = R(q('.dt-card.is-on')); s.bracket = R(q('.dt-bracket')); s.band = R(q('.dt-band')); s.filter = R(q('.dt-filter'));
    s.date = q('.dt-date')?.textContent ?? null; s.loading = q('.dt.is-loading') ? 1 : 0; s.mounted = q('.dt') ? 1 : 0;
    if (list) {
      const lr = list.getBoundingClientRect();
      s.list = [r1(lr.top), r1(lr.height), r1(list.scrollTop), list.scrollHeight, list.clientHeight, (list.classList.contains('more-above') ? 1 : 0) + (list.classList.contains('more-below') ? 2 : 0)];
      s.rows = [...list.querySelectorAll('[data-key]')].map(e => { const r = e.getBoundingClientRect(); return [e.dataset.key, r1(r.top), r1(r.height)]; });
      s.folds = [...list.querySelectorAll('.dt-fold')].map(f => [f.parentElement?.dataset.key ?? '?', r1(f.getBoundingClientRect().height)]);
      s.hot = [...list.querySelectorAll('.is-hot')].map(e => e.dataset.key); s.pick = [...list.querySelectorAll('.is-pick')].map(e => e.dataset.key);
      const under = document.elementFromPoint(cap.ptr.x, cap.ptr.y)?.closest('[data-key]');
      s.under = under && list.contains(under) ? [under.dataset.key, r1(under.getBoundingClientRect().top - lr.top)] : null;
    }
    s.ptr = [cap.ptr.x, cap.ptr.y];
    s.an = [...new Set(document.getAnimations().filter(a => a.playState === 'running').map(a => {
      const el = a.effect?.target, cls = String(el?.className?.baseVal ?? el?.className ?? el?.tagName ?? '').split(' ')[0] || el?.tagName;
      return `${a.constructor.name === 'CSSTransition' ? 'T' : a.constructor.name === 'CSSAnimation' ? 'C' : 'A'}:${cls}${a.transitionProperty ? ':' + a.transitionProperty : ''}${a.animationName ? ':' + a.animationName : ''}`;
    }))];
    s.work = r1(performance.now() - m0);
    return s;
  };
  // A sample is read from a resize observer made in the frame's callback: resize observers are told in the order they were made, so it comes after the
  // pane's own (which scroll the list to hold a row, DayTimeline.tsx) and sees what the frame will paint: not the layout before them, which a frame
  // callback or a task between frames would read.
  const probe = document.createElement('i');
  probe.style.cssText = 'position:fixed;left:-9px;top:0;width:1px;height:1px;pointer-events:none;opacity:0';
  const frame = now => {
    if (!cap.on) return;
    const look = new ResizeObserver(() => { look.disconnect(); if (cap.on) cap.samples.push(sample(now)); });
    look.observe(probe); requestAnimationFrame(frame);
  };
  cap.start = () => { if (!probe.isConnected) document.body.append(probe); cap.samples = []; cap.inputs = []; cap.marks = []; cap.long = []; cap.ev = []; cap.last = 0; cap.t0 = performance.now(); cap.on = true; requestAnimationFrame(frame); return performance.timeOrigin + cap.t0; };
  cap.mark = name => { const t = r1(performance.now() - cap.t0); cap.marks.push([name, t]); return t; };
  cap.stop = () => {
    cap.on = false;
    for (const e of lt.takeRecords()) cap.long.push([e.startTime, e.duration]);
    for (const e of et.takeRecords()) cap.ev.push([e.name, e.startTime, e.duration, e.processingStart, e.processingEnd]);
    const t0 = cap.t0;
    return { epoch0: performance.timeOrigin + t0, samples: cap.samples, inputs: cap.inputs, marks: cap.marks, long: cap.long.filter(x => x[0] >= t0).map(x => [r1(x[0] - t0), r1(x[1])]),
      ev: cap.ev.filter(x => x[1] >= t0).map(x => ({ type: x[0], at: r1(x[1] - t0), dur: x[2], proc: r1(x[4] - x[3]), delay: r1(x[3] - x[1]) })),
      anims: (window.__anims ?? []).filter(a => a.at >= t0).map(a => ({ at: a.at - Math.round(t0), cls: a.cls, key: a.key, kf: a.kf, opt: a.opt })) };
  };
}
// The state of the pane at a moment, for the before/after of a take.
function paneState(keys = []) {
  const list = document.querySelector('.dt-list'), drop = document.querySelector('.notch-drop');
  const box = el => { if (!el) return null; const r = el.getBoundingClientRect(); return { top: +r.top.toFixed(1), bottom: +r.bottom.toFixed(1), height: +r.height.toFixed(1), left: +r.left.toFixed(1), width: +r.width.toFixed(1) }; };
  if (!list) return { pane: box(drop), date: document.querySelector('.dt-date')?.textContent ?? null, list: null };
  const lr = list.getBoundingClientRect(), now = list.querySelector('.dt-now');
  const rows = [...list.querySelectorAll('[data-key]')].map(e => ({ key: e.dataset.key, r: e.getBoundingClientRect(), open: e.getAttribute('aria-expanded') }));
  const first = rows.find(x => x.r.bottom > lr.top + 1), last = [...rows].reverse().find(x => x.r.top < lr.bottom - 1);
  return { date: document.querySelector('.dt-date')?.textContent ?? null, pane: box(drop), dt: box(document.querySelector('.dt')), list: { top: +lr.top.toFixed(1), height: +lr.height.toFixed(1), scrollTop: +list.scrollTop.toFixed(1), scrollHeight: list.scrollHeight, max: list.scrollHeight - list.clientHeight },
    open: rows.filter(x => x.open === 'true').map(x => x.key), first: first && { key: first.key, top: +(first.r.top - lr.top).toFixed(1) }, last: last && { key: last.key, bottom: +(lr.bottom - last.r.bottom).toFixed(1) },
    now: now ? +(now.getBoundingClientRect().top - lr.top).toFixed(1) : null, mounted: true,
    focus: Object.fromEntries(keys.map(k => { const x = rows.find(r => r.key === k); return [k, x ? { top: +(x.r.top - lr.top).toFixed(1), bottom: +(x.r.bottom - lr.top).toFixed(1), height: +x.r.height.toFixed(1), listHeight: +lr.height.toFixed(1), open: x.open } : null]; })) };
}
// The rows of the list with their boxes (list-relative), and which of them open.
function rowsInfo() {
  const list = document.querySelector('.dt-list');
  if (!list) return [];
  const lr = list.getBoundingClientRect();
  return [...list.querySelectorAll('[data-key]')].map(e => { const r = e.getBoundingClientRect(); return { key: e.dataset.key, kind: e.dataset.kind ?? (e.classList.contains('dt-sh') ? 'stay-head' : ''), top: r.top - lr.top, bottom: r.bottom - lr.top, h: r.height, openable: e.hasAttribute('aria-expanded'), expanded: e.getAttribute('aria-expanded') === 'true', ax: r.left, ay: r.top, w: r.width, lt: lr.top, lh: lr.height, lx: lr.left }; });
}

// ---------- the page, the fake daemon, the pointer ----------
let server = null, serverLog = '';
// The built page is served by `vite preview`; if it has died it is started again.
async function ensureServer() {
  const up = async () => { try { await fetch(`http://127.0.0.1:${web}/`); return true; } catch { return false; } };
  if (await up()) return;
  server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  server.stdout.on('data', d => { serverLog += d; }); server.stderr.on('data', d => { serverLog += d; });
  server.on('exit', code => { if (!closing) console.error(`vite preview exited (${code}):`, serverLog.slice(-600)); });
  for (let i = 0; i < 80 && !(await up()); i++) await wait(100);
}
let closing = false;
await ensureServer();
const browser = await chromium.launch({ headless: true, ...process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : { channel: 'chrome' }, args: ['--disable-web-security', '--mute-audio'] });
const sheetPage = await browser.newPage();
const errors = [];
// Every Web Animation the page starts is written down (what, on what, with which keyframes and timing).
const record = () => {
  const animate = Element.prototype.animate;
  window.__anims = [];
  Element.prototype.animate = function (kf, opt) {
    window.__anims.push({ cls: String(this.className?.baseVal ?? this.className), key: this.dataset?.key ?? this.parentElement?.dataset?.key ?? null, kf: JSON.parse(JSON.stringify(kf)), opt: typeof opt === 'number' ? { duration: opt } : JSON.parse(JSON.stringify(opt ?? {})), at: Math.round(performance.now()) });
    return animate.call(this, kf, opt);
  };
};
async function boot({ served = bigDay(), lang = 'zh', dayLag = 0, yesterdayFull = false, notchWidth = 185 } = {}) {
  await ensureServer();
  const ctx = await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const s = { served, dayLag, yesterdayFull, records: records(), controls: { mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' } };
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
  await page.addInitScript(([settings, notchWidth]) => {
    localStorage.setItem('companion-settings-v1', JSON.stringify(settings));
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {}, wearing: () => {}, onDictation: () => () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async on => { (window.__focus ??= []).push(on); }, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, onGhostty: () => () => {}, onAgentsPresence: cb => { window.__presence = cb; return () => {}; }, onMouseDown: cb => { window.__down = cb; return () => {}; }, onClaudeFront: cb => { window.__front = cb; return () => {}; }, plugins: async () => ({}),
    };
    window.__sockets = [];
    window.WebSocket = class { constructor() { window.__sockets.push(this); setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
  }, [{ lang }, notchWidth]);
  await page.addInitScript(record);
  await page.route(`${daemon}/**`, async route => {
    const url = new URL(route.request().url());
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    if (url.pathname === '/inherent/day') {
      const date = url.searchParams.get('date');
      if (s.dayLag && date && date !== DATE) await wait(s.dayLag);
      return json(!date || date === DATE ? s.served : date === YESTERDAY ? (s.yesterdayFull ? fullYesterday() : yesterday()) : { ...yesterday(), date, items: [] });
    }
    if (url.pathname.startsWith('/inherent/attachments/')) {
      return url.pathname.endsWith(`/${PIC}`) ? route.fulfill({ status: 200, contentType: 'image/png', body: PICTURE }) : url.pathname.endsWith(`/${BIG}`) ? route.fulfill({ status: 200, contentType: 'image/png', body: BIGPICTURE }) : route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"gone"}' });
    }
    if (url.pathname.startsWith('/inherent/memory/day/') && url.pathname.endsWith('/records')) {
      const offset = Number(url.searchParams.get('offset') ?? 0), limit = Number(url.searchParams.get('limit') ?? 120);
      return json({ day: url.pathname.split('/')[4], total: s.records.length, offset, records: s.records.slice(offset, offset + limit) });
    }
    if (url.pathname === '/inherent/controls') return json(s.controls);
    if (url.pathname === '/inherent/claude-sessions') return json({ sessions: [], error: null });
    if (url.pathname === '/inherent/agent-marks') return json({ marks: {} });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });
  await page.clock.setFixedTime(new Date(NOW));
  await page.goto(`http://127.0.0.1:${web}/?companion=1&port=${daemonPort}`);
  await page.addStyleTag({ content: 'html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}' });
  await page.evaluate(installSampler);
  const ptr = { x: -1, y: -1 };
  const move = async (x, y) => { ptr.x = x; ptr.y = y; await page.mouse.move(x, y); await page.evaluate(([x, y]) => { window.__cap.ptr = { x, y }; window.__cursor({ x, y }); }, [x, y]); };
  // A pointer glides (one sample every ~16 ms, the cadence of the native feed); a smaller `step` is a quicker hand.
  const glide = async (to, ms, step = 16) => { const from = { ...ptr }, steps = Math.max(2, Math.round(ms / step)); for (let i = 1; i <= steps; i++) { await move(from.x + (to.x - from.x) * i / steps, from.y + (to.y - from.y) * i / steps); await wait(step); } };
  const until = (fn, arg, timeout = 6000) => page.waitForFunction(fn, arg, { timeout });
  const lineX = async () => { const at = await page.evaluate(() => document.querySelector('.notch').dataset.lineAt); const [a, b] = at.split('-').map(Number); return { x0: a, x1: b, mid: (a + b) / 2 }; };
  const a = { page, ctx, s, move, glide, until, lineX, ptr, far: { x: 600, y: 700 }, away: { x: 630, y: 120 } };
  // The pane opened the way he opens it: the pointer rests on the line.
  a.openPane = async () => {
    await until(() => document.querySelector('.notch')?.dataset.line === '家 · 3:00');
    const seg = await lineX(); a.seg = seg;
    await move(a.far.x, a.far.y); await wait(100);
    await glide({ x: seg.mid, y: 14 }, 300);
    await until(() => document.querySelector('.notch-drop.is-open .dt-row'), undefined, 8000);
    await wait(900);
  };
  return a;
}

// ---------- one take: the sampler (and the screencast on the second pass) around one trigger ----------
const SCREEN = { format: 'jpeg', quality: 90, maxWidth: 1280, maxHeight: 1800, everyNthFrame: 1 };
async function withScreen(a) {
  const cdp = await a.ctx.newCDPSession(a.page), rec = { cur: null };
  cdp.on('Page.screencastFrame', f => { rec.cur?.push({ data: f.data, ts: f.metadata.timestamp * 1000 }); cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }).catch(() => {}); });
  a.screen = { start: async () => { rec.cur = []; await cdp.send('Page.startScreencast', SCREEN); }, stop: async () => { await cdp.send('Page.stopScreencast'); const f = rec.cur; rec.cur = null; return f; } };
}
async function take(a, name, { ms, lead = 150, focus = [] }, trigger) {
  const before = await a.page.evaluate(paneState, focus);
  if (a.screen) { await a.screen.start(); await wait(200); }
  const nodeBefore = Date.now(), epoch0 = await a.page.evaluate(() => window.__cap.start()), nodeAfter = Date.now(), nodeT0 = (nodeBefore + nodeAfter) / 2;
  await wait(lead);
  const trig = await a.page.evaluate(() => window.__cap.mark('trigger')), tTrig = Date.now();
  await trigger();
  const spent = Date.now() - tTrig;
  await wait(Math.max(0, ms - spent));
  const data = await a.page.evaluate(() => window.__cap.stop());
  const frames = a.screen ? await a.screen.stop() : null;
  const after = await a.page.evaluate(paneState, focus);
  return { name, ms, trig, focus, before, after, nodeT0, ...data, frames };
}

// ---------- the numbers ----------
const q = (xs, p) => { if (!xs.length) return null; const s = [...xs].sort((a, b) => a - b); return s[Math.min(s.length - 1, Math.floor(p * s.length))]; };
const r1 = n => n == null ? n : Math.round(n * 10) / 10;
function runs(series, eps = .05) {
  // maximal runs of consecutive samples whose value changes by more than eps
  const out = []; let cur = null;
  for (let i = 1; i < series.length; i++) {
    const d = series[i].v - series[i - 1].v;
    if (Math.abs(d) > eps) { if (!cur) cur = { start: series[i - 1].rel, end: series[i].rel, from: series[i - 1].v, to: series[i].v, steps: [], frames: 0 }; cur.end = series[i].rel; cur.to = series[i].v; cur.steps.push(r1(d)); cur.frames++; }
    else if (cur) { out.push(cur); cur = null; }
  }
  if (cur) out.push(cur);
  return out.map(c => ({ start: r1(c.start), end: r1(c.end), ms: r1(c.end - c.start), from: r1(c.from), to: r1(c.to), delta: r1(c.to - c.from), frames: c.frames, maxStep: r1(Math.max(...c.steps.map(Math.abs))), steps: c.steps.length <= 24 ? c.steps : undefined }));
}
function analyse(tk) {
  const S = tk.samples.map(s => ({ ...s, rel: s.t - tk.trig })), P = S.filter(s => s.rel >= -4), sum = { frames: P.length };
  // frame cadence
  const gaps = P.slice(1).map(s => s.gap), slow = P.slice(1).map((s, i) => ({ s, vis: s.vis || P[i].vis })).filter(x => x.s.gap > 20).map(({ s, vis }) => ({ at: r1(s.rel), gap: r1(s.gap), lost: Math.max(0, Math.round(s.gap / 16.67) - 1), work: s.work, paneVisible: !!vis }));
  sum.gaps = { mean: r1(gaps.reduce((x, y) => x + y, 0) / Math.max(1, gaps.length)), median: r1(q(gaps, .5)), p95: r1(q(gaps, .95)), max: r1(Math.max(0, ...gaps)), over20: slow.length, lostFrames: slow.reduce((n, x) => n + x.lost, 0), over20WhilePaneVisible: slow.filter(x => x.paneVisible).length, lostWhilePaneVisible: slow.filter(x => x.paneVisible).reduce((n, x) => n + x.lost, 0), slow };
  sum.samplerWork = { mean: r1(P.reduce((x, s) => x + s.work, 0) / Math.max(1, P.length)), max: r1(Math.max(0, ...P.map(s => s.work))) };
  sum.longTasks = tk.long.map(([at, dur]) => ({ at: r1(at - tk.trig), dur })).filter(x => x.at >= -4);
  sum.slowEvents = tk.ev.map(e => ({ ...e, at: r1(e.at - tk.trig) })).filter(e => e.at >= -4);
  // the pane
  const withPane = P.filter(s => s.pane), h = withPane.map(s => ({ rel: s.rel, v: s.pane[3] })), w = withPane.map(s => ({ rel: s.rel, v: s.pane[2] }));
  if (h.length) {
    const h0 = h[0].v, h1 = h.at(-1).v, first = h.find(x => Math.abs(x.v - h0) > .5), last = [...h].reverse().find(x => Math.abs(x.v - h1) > 1);
    sum.pane = { h0: r1(h0), h1: r1(h1), startLag: first ? r1(first.rel) : null, settleAt: last ? r1(h[h.indexOf(last) + 1]?.rel ?? last.rel) : r1(h[0].rel), peak: r1(Math.max(...h.map(x => x.v))), low: r1(Math.min(...h.map(x => x.v))),
      overshoot: r1(h1 > h0 ? Math.max(0, Math.max(...h.map(x => x.v)) - h1) : h1 < h0 ? Math.max(0, h1 - Math.min(...h.map(x => x.v))) : Math.max(Math.max(...h.map(x => x.v)) - h1, h1 - Math.min(...h.map(x => x.v)))), maxStep: r1(Math.max(0, ...h.slice(1).map((x, i) => Math.abs(x.v - h[i].v)))),
      w0: r1(w[0].v), w1: r1(w.at(-1).v), l0: r1(withPane[0].pane[0]), l1: r1(withPane.at(-1).pane[0]), settledInWindow: !last || h.indexOf(last) < h.length - 2 };
    // content hanging below the pane (clipped) and pane left empty below the content, in px (2 px is the pane's own margin)
    const clip = withPane.filter(s => s.inn).map(s => ({ rel: s.rel, v: r1(s.inn[1] + s.inn[3] - (s.pane[1] + s.pane[3]) + 2) }));
    sum.paneClip = { maxClipped: r1(Math.max(0, ...clip.map(x => x.v))), at: clip.length ? clip.reduce((m, x) => x.v > m.v ? x : m, clip[0]).rel : null, framesClipped: clip.filter(x => x.v > 4).length, maxEmpty: r1(Math.max(0, ...clip.map(x => -x.v))) };
    const op = P.filter(s => s.op != null).map(s => ({ rel: s.rel, v: s.op })), up = op.find(x => x.v > .02), full = op.find(x => x.v >= .99);
    sum.paneContent = { opacityFrom: up ? r1(up.rel) : null, opaqueAt: full ? r1(full.rel) : null, startOpacity: op[0]?.v, endOpacity: op.at(-1)?.v };
    sum.paneMoves = runs(h, .5).map(m => ({ ...m, steps: undefined }));
  }
  // the list's scrollTop
  const sc = P.filter(s => s.list).map(s => ({ rel: s.rel, v: s.list[2] }));
  if (sc.length) {
    const moves = runs(sc, .05);
    sum.scroll = { start: r1(sc[0].v), end: r1(sc.at(-1).v), max: r1(Math.max(...P.filter(s => s.list).map(s => s.list[3] - s.list[4]))), moves, jumps: moves.filter(m => m.frames <= 2 && Math.abs(m.delta) > 4),
      reversals: moves.length > 1 ? moves.map(m => m.delta).slice(1).filter((d, i) => Math.sign(d) !== Math.sign(moves[i].delta)).length : 0 };
    const lh = P.filter(s => s.list).map(s => ({ rel: s.rel, v: s.list[1] })), lm = runs(lh, .5);
    sum.listHeight = { h0: r1(lh[0].v), h1: r1(lh.at(-1).v), min: r1(Math.min(...lh.map(x => x.v))), max: r1(Math.max(...lh.map(x => x.v))), moves: lm.map(m => ({ ...m, steps: undefined })) };
  }
  // layout shifts: a row's position in the content (its top plus the scroll) changing by more than 4 px in one frame
  const shifts = [];
  for (let i = 1; i < P.length; i++) {
    const a = P[i - 1], b = P[i];
    if (!a.rows || !b.rows || !a.list || !b.list) continue;
    const ca = new Map(a.rows.map(r => [r[0], r[1] - a.list[0] + a.list[2]])), moved = [];
    for (const r of b.rows) { const c0 = ca.get(r[0]); if (c0 == null) continue; const d = (r[1] - b.list[0] + b.list[2]) - c0; if (Math.abs(d) > 4) moved.push([r[0], r1(d)]); }
    if (moved.length) {
      const anim = [...new Set([...a.an ?? [], ...b.an ?? []])].filter(x => x.startsWith('A:'));
      shifts.push({ at: r1(b.rel), n: moved.length, maxKey: moved.reduce((m, x) => Math.abs(x[1]) > Math.abs(m[1]) ? x : m, moved[0]), anim, explained: anim.length > 0 });
    }
  }
  sum.contentShifts = { frames: shifts.length, unexplained: shifts.filter(x => !x.explained), biggest: shifts.reduce((m, x) => Math.abs(x.maxKey[1]) > Math.abs(m?.maxKey[1] ?? 0) ? x : m, null), list: shifts.length <= 40 ? shifts : undefined };
  // what is under the pointer: the row, and where it sits in the list, frame by frame while the pointer is still
  const drift = [];
  for (let i = 1; i < P.length; i++) {
    const a = P[i - 1], b = P[i];
    if (!a.under && !b.under) continue;
    const still = a.ptr[0] === b.ptr[0] && a.ptr[1] === b.ptr[1];
    if (!still) continue;
    if ((a.under?.[0] ?? null) !== (b.under?.[0] ?? null)) drift.push({ at: r1(b.rel), from: a.under?.[0] ?? null, to: b.under?.[0] ?? null });
  }
  // where in the row under it the pointer sits (list-relative pointer y minus the row's top): a row sliding away from under a pointer that does not move shows as this growing
  const offs = P.filter(s => s.under && s.list).map(s => ({ rel: s.rel, key: s.under[0], v: r1(s.ptr[1] - s.list[0] - s.under[1]) }));
  sum.underPointer = { keyChangesWhilePointerStill: drift.length, first: drift.slice(0, 6), keys: [...new Set(P.filter(s => s.under).map(s => s.under[0]))],
    offset: offs.length ? { start: offs[0].v, end: offs.at(-1).v, min: Math.min(...offs.map(x => x.v)), max: Math.max(...offs.map(x => x.v)), maxStep: r1(Math.max(0, ...offs.slice(1).map((x, i) => x.key === offs[i].key ? Math.abs(x.v - offs[i].v) : 0))) } : null };
  // rows asked for: their travel in the list's view, and where they end
  sum.focus = {};
  for (const key of tk.focus) {
    const ser = P.map(s => { const r = s.rows?.find(x => x[0] === key); return r && s.list ? { rel: s.rel, top: r[1] - s.list[0], h: r[2], bottom: r[1] + r[2] - s.list[0], lh: s.list[1] } : null; }).filter(Boolean);
    if (!ser.length) continue;
    const tops = ser.map(x => ({ rel: x.rel, v: x.top })), moves = runs(tops, .3), last = ser.at(-1);
    sum.focus[key] = { top0: r1(ser[0].top), top1: r1(last.top), h0: r1(ser[0].h), h1: r1(last.h), bottom1: r1(last.bottom), listH: r1(last.lh), outsideBottom: r1(Math.max(0, last.bottom - last.lh)), outsideTop: r1(Math.max(0, -last.top)),
      moves: moves.map(m => ({ ...m, steps: undefined })), maxStepTop: r1(Math.max(0, ...tops.slice(1).map((x, i) => Math.abs(x.v - tops[i].v)))), heightMoves: runs(ser.map(x => ({ rel: x.rel, v: x.h })), .3).map(m => ({ ...m, steps: undefined })) };
  }
  sum.animations = tk.anims.map(x => ({ at: x.at - Math.round(tk.trig), cls: x.cls.split(' ')[0], key: x.key, ms: x.opt.duration, delay: x.opt.delay, easing: x.opt.easing, from: x.kf?.[0], to: x.kf?.at(-1) })).filter(x => x.at >= -4);
  return sum;
}

// ---------- the contact sheet ----------
const CELL = { cols: 4, rows: 4, w: 360, crop: { x: 50, y: 0, w: 540, h: 680 } };
async function sheet(tk, file) {
  const f = tk.frames;
  if (!f?.length) return null;
  // The sheet covers the motion: from a little before the first thing that moved to a little after the last (the whole take if nothing did).
  const S = tk.samples.map(x => ({ ...x, rel: x.t - tk.trig })), sig = x => JSON.stringify([x.pane, x.op, x.list?.[2], x.rows?.map(r => [r[0], r[1] - x.list[0] + x.list[2], r[2]]), x.card, x.bracket, x.band, x.bar, x.head, x.filter, x.date, x.mounted]);
  let first = null, last = null;
  for (let i = 1; i < S.length; i++) if (S[i].rel > -4 && sig(S[i]) !== sig(S[i - 1])) { first ??= S[i].rel; last = S[i].rel; }
  const t0 = tk.nodeT0 + tk.trig, n = CELL.cols * CELL.rows, from = first === null ? -60 : Math.max(-60, first - 80), to = first === null ? tk.ms : Math.min(tk.ms, Math.max(last + 120, from + 400)), times = Array.from({ length: n }, (_, i) => from + (to - from) * i / (n - 1)), rel = f.map(x => x.ts - t0);
  const pick = times.map(t => { let k = -1; for (let i = 0; i < f.length; i++) if (rel[i] <= t) k = i; return k < 0 ? 0 : k; });
  const url = await sheetPage.evaluate(async ({ imgs, labels, subs, cell }) => {
    const H = Math.round(cell.w * cell.crop.h / cell.crop.w), strip = 22, canvas = document.createElement('canvas');
    canvas.width = cell.cols * cell.w; canvas.height = cell.rows * (H + strip);
    const ctx = canvas.getContext('2d'); ctx.fillStyle = '#20242c'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    const cache = new Map();
    for (let i = 0; i < imgs.length; i++) {
      const col = i % cell.cols, row = Math.floor(i / cell.cols), x = col * cell.w, y = row * (H + strip);
      if (!cache.has(imgs[i])) { const img = new Image(); img.src = 'data:image/jpeg;base64,' + imgs[i]; await img.decode(); cache.set(imgs[i], img); }
      const img = cache.get(imgs[i]), k = img.naturalWidth / 640;
      ctx.drawImage(img, cell.crop.x * k, cell.crop.y * k, cell.crop.w * k, cell.crop.h * k, x, y, cell.w, H);
      ctx.fillStyle = '#2b303a'; ctx.fillRect(x, y + H, cell.w, strip);
      ctx.fillStyle = '#fff'; ctx.font = '600 13px -apple-system, sans-serif'; ctx.fillText(labels[i], x + 8, y + H + 15);
      ctx.fillStyle = '#9aa3b2'; ctx.font = '11px -apple-system, sans-serif'; ctx.textAlign = 'right'; ctx.fillText(subs[i], x + cell.w - 8, y + H + 15); ctx.textAlign = 'left';
      ctx.strokeStyle = '#444b58'; ctx.lineWidth = 1; ctx.strokeRect(x + .5, y + .5, cell.w - 1, H + strip - 1);
    }
    return canvas.toDataURL('image/png');
  }, { imgs: pick.map(k => f[k].data), labels: times.map(t => `${t >= 0 ? '+' : ''}${Math.round(t)} ms`), subs: pick.map(k => `frame ${rel[k] >= 0 ? '+' : ''}${Math.round(rel[k])}`), cell: CELL });
  writeFileSync(file, Buffer.from(url.split(',')[1], 'base64'));
  const iv = rel.slice(1).map((x, i) => x - rel[i]).filter(d => d > 0);
  return { frames: f.length, window: [r1(from), r1(to)], captured: [r1(rel[0]), r1(rel.at(-1))], intervalMedian: r1(q(iv, .5)), intervalP90: r1(q(iv, .9)), intervalMax: r1(Math.max(0, ...iv)), perSecond: r1(f.length / (tk.ms / 1000)) };
}

// ---------- scenes ----------
const results = {};
async function save(name, r) {
  const main = r.numbers ?? r.frames, summary = analyse(main);
  const withScreen = r.numbers && r.frames ? analyse(r.frames) : null;
  const screencast = r.frames ? await sheet(r.frames, path.join(out, `${name}.png`)) : null;
  const strip = tk => ({ ...tk, frames: undefined });
  writeFileSync(path.join(out, `${name}.json`), JSON.stringify({ take: name, pass: r.numbers ? 'numbers (no screencast)' : 'frames', viewport: { width: 640, height: 900, deviceScaleFactor: 2 }, ...strip(main), summary,
    screencast, withScreencast: withScreen && { gaps: withScreen.gaps, longTasks: withScreen.longTasks, pane: withScreen.pane, scroll: withScreen.scroll && { start: withScreen.scroll.start, end: withScreen.scroll.end, moves: withScreen.scroll.moves } } }));
  results[name] = { summary, screencast, before: main.before, after: main.after };
  console.log(`saved ${name}`);
}
async function runScene(n, opts, fn) {
  if (!wanted(n)) return;
  const reps = Number(process.env.REPEAT ?? 1);
  for (let rep = 1; rep <= reps; rep++) {
    const res = {};
    for (const pass of passes) {
      const a = await boot(opts);
      if (pass === 'frames') await withScreen(a);
      try { await fn(a, async (name, o, trig) => { const tag = (reps > 1 ? `${name}-r${rep}` : name) + suffix, tk = await take(a, tag, o, trig); (res[tag] ??= {})[pass] = tk; return tk; }); }
      catch (e) { console.error(`scene ${n} (${pass}) failed:`, e); }
      await a.ctx.close();
    }
    for (const [name, r] of Object.entries(res)) await save(name, r);
  }
}

// ---------- helpers for the scenes ----------
const rowsOf = a => a.page.evaluate(rowsInfo);
const click = a => async () => { await a.page.mouse.down(); await wait(40); await a.page.mouse.up(); };
// A row's spot to press: 170 px into it, 14 px down (the first line of its head).
const spot = r => ({ x: r.ax + 170, y: r.ay + 14 });
const toRow = async (a, key, ms = 220) => { const r = (await rowsOf(a)).find(x => x.key === key); await a.glide(spot(r), ms); await wait(260); return r; };
// Moves the list without animation: to the top, to the end, to a fraction of its range, or so that the row `key` ends `gap` px above the list's foot.
const scrollList = (a, { to = 'top', f = 0, key = '', gap = 24 } = {}) => a.page.evaluate(({ to, f, key, gap }) => {
  const l = document.querySelector('.dt-list');
  if (to === 'top') l.scrollTop = 0; else if (to === 'end') l.scrollTop = l.scrollHeight; else if (to === 'frac') l.scrollTop = (l.scrollHeight - l.clientHeight) * f;
  else { const lr = l.getBoundingClientRect(), e = l.querySelector(`[data-key="${key}"]`).getBoundingClientRect(); l.scrollTop += e.bottom - (lr.bottom - gap); }
}, { to, f, key, gap });
const settleList = () => wait(350);
// The row in the middle of the list's view that opens (a stay's header first), fully inside it.
const middle = (rows, kind) => {
  const lh = rows[0].lh, ok = rows.filter(r => r.openable && !r.expanded && r.top >= 28 && r.bottom <= lh - 28 && (!kind || r.kind === kind)).sort((x, y) => Math.abs((x.top + x.bottom) / 2 - lh / 2) - Math.abs((y.top + y.bottom) / 2 - lh / 2));
  return ok[0];
};
// A second row to open: the nearest below the first that is whole in the view, else the nearest above.
const another = (rows, key) => {
  const lh = rows[0].lh, i = rows.findIndex(r => r.key === key), ok = rows.filter((r, j) => j !== i && r.openable && !r.expanded && r.top >= 28 && r.bottom <= lh - 28);
  return ok.filter((r, _) => rows.indexOf(r) > i)[0] ?? ok.filter(r => rows.indexOf(r) < i).at(-1);
};

if (process.env.EXPLORE) {
  const a = await boot();
  await a.openPane();
  console.log(JSON.stringify(await a.page.evaluate(paneState), null, 1));
  for (const r of await rowsOf(a)) console.log(`${r.key.padEnd(22)} ${String(r.kind).padEnd(10)} top ${r.top.toFixed(0).padStart(5)} h ${r.h.toFixed(0).padStart(3)} ${r.openable ? 'opens' : 'still'}`);
  console.log(await a.page.evaluate(() => { const l = document.querySelector('.dt-list'); return { scrollTop: l.scrollTop, scrollHeight: l.scrollHeight, clientHeight: l.clientHeight, epoch: performance.timeOrigin + performance.now(), nowNode: 0 }; }), Date.now());
  await a.ctx.close(); await browser.close(); closing = true; server?.kill();
  process.exit(0);
}

// 1. The pane opening from the wing (the pointer rests on the line), and folding when the pointer leaves.
await runScene(1, {}, async (a, T) => {
  await a.until(() => document.querySelector('.notch')?.dataset.line === '家 · 3:00');
  const seg = await a.lineX();
  await a.move(a.far.x, a.far.y); await wait(400);
  await T('1-open-from-wing', { ms: 1800 }, () => a.glide({ x: seg.mid, y: 14 }, 300));
  await wait(700);
  await T('1-fold-on-leave', { ms: 2800 }, () => a.glide(a.away, 250));
});

// 2. Opening a row in the middle of the list (a stay), a second while the first is open, closing them; the list at the top, and the row near the list's foot.
//    top: the stay in view with the list at the top; bottom: the stay in the middle of the day, the list scrolled so it ends 24 px above the list's foot.
for (const variant of ['top', 'bottom']) await runScene(2, {}, async (a, T) => {
  await a.openPane();
  await scrollList(a); await settleList();
  const all = await rowsOf(a), heads = all.filter(r => r.kind === 'stay-head' && r.openable);
  const K = variant === 'top' ? middle(all, 'stay-head') : [...heads].sort((x, y) => Math.abs(x.top - 780) - Math.abs(y.top - 780))[0];
  if (variant === 'bottom') { await scrollList(a, { to: 'row', key: K.key }); await settleList(); }
  await toRow(a, K.key);
  await T(`2-${variant}-open-first`, { ms: 1100, focus: [K.key] }, click(a));
  await wait(600);
  const K2 = another(await rowsOf(a), K.key);
  await toRow(a, K2.key);
  await T(`2-${variant}-open-second`, { ms: 1100, focus: [K.key, K2.key] }, click(a));
  await wait(600);
  await toRow(a, K2.key);
  await T(`2-${variant}-close-second`, { ms: 800, focus: [K.key, K2.key] }, click(a));
  await wait(500);
  await toRow(a, K.key); await click(a)(); await wait(900);
  await toRow(a, K.key);
  await T(`2-${variant}-close-first`, { ms: 800, focus: [K.key, K2.key] }, click(a));
});

// 3. Opening the last row of the list (near the pane's bottom), closing it, and opening 问她 from it (the input slides up at the pane's foot).
await runScene(3, {}, async (a, T) => {
  await a.openPane();
  await scrollList(a, { to: 'end' }); await settleList();
  const last = (await rowsOf(a)).filter(r => r.openable).at(-1);
  await toRow(a, last.key);
  await T('3-open-last-row', { ms: 1200, focus: [last.key] }, click(a));
  await wait(600);
  await toRow(a, last.key);
  await T('3-close-last-row', { ms: 800, focus: [last.key] }, click(a));
  await wait(500);
  await toRow(a, last.key); await click(a)(); await wait(900);
  const ask = await a.page.evaluate(() => { const b = [...document.querySelectorAll('.dt-fold button')].find(x => /问她|Ask her/.test(x.textContent)), r = b?.getBoundingClientRect(); return r && { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
  if (ask) { await a.glide(ask, 240); await wait(300); await T('3-open-ask-dock-on-last-row', { ms: 1200, focus: [last.key] }, click(a)); }
});

// 4. The pane folded and opened again after the list was scrolled and a row was open.
await runScene(4, {}, async (a, T) => {
  await a.openPane();
  const scene = async () => {
    await scrollList(a, { to: 'frac', f: .45 }); await settleList();
    const K = middle(await rowsOf(a));
    await toRow(a, K.key); await click(a)(); await wait(800);
    return K.key;
  };
  const K = await scene();
  await T('4-fold-after-scroll-and-open', { ms: 2800, focus: [K] }, () => a.glide(a.away, 250));
  await T('4-reopen', { ms: 1800, focus: [K] }, () => a.glide({ x: a.seg.mid, y: 14 }, 300));
  await wait(900);
});

// 5. Clicking the bar (it takes the list to a row), dragging a range on the bar, showing only that range, and the whole day again.
await runScene(5, {}, async (a, T) => {
  await a.openPane();
  const track = await a.page.evaluate(() => { const r = document.querySelector('.dt-track').getBoundingClientRect(); return { x: r.left, y: r.top + r.height / 2, w: r.width }; });
  const at = f => ({ x: track.x + track.w * f, y: track.y });
  await a.glide(at(.12), 260); await wait(400);
  await T('5-click-bar-early', { ms: 1100 }, click(a));
  await wait(500);
  await a.glide(at(.8), 260); await wait(400);
  await T('5-click-bar-late', { ms: 1100 }, click(a));
  await wait(500);
  await a.glide(at(.3), 260); await wait(300);
  await T('5-drag-range', { ms: 1300 }, async () => {
    await a.page.mouse.down(); await wait(30);
    for (let i = 1; i <= 14; i++) { await a.move(at(.3 + .2 * i / 14).x, track.y); await wait(18); }
    await a.page.mouse.up();
  });
  await wait(500);
  const btn = await a.page.evaluate(() => { const r = document.querySelector('.dt-cbtn')?.getBoundingClientRect(); return r && { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
  if (btn) { await a.glide(btn, 260); await wait(300); await T('5-show-only-this', { ms: 1200 }, click(a)); await wait(500); }
  const clear = await a.page.evaluate(() => { const r = document.querySelector('.dt-filter button')?.getBoundingClientRect(); return r && { x: r.left + r.width / 2, y: r.top + r.height / 2 }; });
  if (clear) { await a.glide(clear, 260); await wait(300); await T('5-clear-filter', { ms: 1200 }, click(a)); }
});

// 6. Going to the day before and back: a day before of two rows (the fixture's), a full one, a full one that takes 250 ms to come.
for (const [tag, opts] of [['sparse', {}], ['full', { yesterdayFull: true }], ['full-lag250', { yesterdayFull: true, dayLag: 250 }]]) await runScene(6, opts, async (a, T) => {
  await a.openPane();
  const button = n => a.page.evaluate(n => { const r = document.querySelectorAll('.dt-nav button')[n].getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }, n);
  await a.glide(await button(0), 260); await wait(300);
  await T(`6-${tag}-day-before`, { ms: 1500 }, click(a));
  await wait(700);
  await a.glide(await button(1), 160); await wait(300);
  await T(`6-${tag}-back-to-today`, { ms: 1500 }, click(a));
  if (tag === 'full') {
    await wait(600); await a.glide(await button(0), 200); await wait(250); await click(a)(); await wait(1200);
    await a.glide(a.away, 250); await wait(2200);
    await T('6-full-reopen-after-day-before', { ms: 1800 }, () => a.glide({ x: a.seg.mid, y: 14 }, 300));
  }
});

// 7. Hovering rows quickly down the list and back up: a quick hand, 350 px in 250 ms (about 1400 px/s), a pointer event as often as the browser takes one.
const sweep = async (a, to, ms) => {
  const from = { ...a.ptr }, t0 = Date.now();
  for (let i = 1; ; i++) {
    const k = Math.min(1, (Date.now() - t0) / ms), x = from.x + (to.x - from.x) * k, y = from.y + (to.y - from.y) * k;
    a.ptr.x = x; a.ptr.y = y;
    await a.page.mouse.move(x, y);
    if (i % 4 === 0 || k === 1) await a.page.evaluate(([x, y]) => { window.__cap.ptr = { x, y }; window.__cursor({ x, y }); }, [x, y]);
    if (k === 1) break;
  }
};
await runScene(7, {}, async (a, T) => {
  await a.openPane();
  await scrollList(a); await settleList();
  const info = (await rowsOf(a))[0], x = info.lx + 200, y0 = info.lt + 20, y1 = info.lt + info.lh - 12;
  await a.glide({ x, y: y0 }, 300); await wait(400);
  await T('7-hover-sweep-down', { ms: 1000 }, () => sweep(a, { x, y: y1 }, 250));
  await wait(400);
  await T('7-hover-sweep-up', { ms: 1000 }, () => sweep(a, { x, y: y0 }, 250));
});

console.log(JSON.stringify(Object.fromEntries(Object.entries(results).map(([k, v]) => [k, { gaps: v.summary.gaps.over20, lost: v.summary.gaps.lostFrames, long: v.summary.longTasks.length, screencast: v.screencast?.perSecond }])), null, 1));
if (errors.length) console.log('page errors:', [...new Set(errors)].slice(0, 5));
await browser.close(); closing = true; server?.kill();

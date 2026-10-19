// The notch's right side is a module (ADR 0296): the stars as before (`marks`), or the day's timeline (the default): the turn mark
// alone, one line (`家 · 3:12`, or the way he is going), the pinned trip. Resting on the line, or a click on it, drops the day from
// the island as the Dashboard comes: rest opens it, a click pins it, leaving folds it, Esc folds it, and the two never hang together.
// Headless Chrome against the built page with the native bridge stubbed and a fake daemon that serves /inherent/day.
// Run after `npm run build`. Screenshots land in evidence/notch-timeline/ (COMPANION_EVIDENCE_DIR moves them).
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildDay, clock, dateTitle, fnv, moneyText, shiftDate, wingText, within } from '../src/dayView.ts';
import { axisOf, buildBar, fit, fOf, hm, msOf, numbers, peek, rowAt, summarize, toneOn } from '../src/barView.ts';
import { deflateSync, crc32 } from 'node:zlib';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = process.env.COMPANION_EVIDENCE_DIR ?? path.join(root, 'evidence/notch-timeline');
mkdirSync(dir, { recursive: true });
const web = Number(process.env.COMPANION_PORT ?? 5199), daemonPort = Number(process.env.COMPANION_DAEMON_PORT ?? 8797), daemon = `http://127.0.0.1:${daemonPort}`;
// ONLY=bar runs the bar's checks alone (the others are the slice before it): quicker to work on.
const only = process.env.ONLY ?? '', all = !only;
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
const small = (items, over = {}) => makeDay({ items, labels: [], missing: [], line: null, highlights: [], ...over });
// What a brain we cannot see might send: a missing time, a field in another shape, a list that is not one.
const brokenItems = () => [
  { id: 'x-null', lane: 'you', kind: 'move', start_ms: null, end_ms: null, state: 'now', activity: 'walking' },
  { id: 'x-work', lane: 'you', kind: 'work', start_ms: T(10, 30), end_ms: T(11, 20), state: 'done', active_s: '42 min', projects: 'Jarvis', apps: { name: 'Xcode' } },
  { id: 'x-stay', lane: 'you', kind: 'stay', start_ms: T(20, 0), end_ms: null, state: 'now', place: 12, kept_place: 'home' },
  { id: 'x-sleep', lane: 'you', kind: 'sleep', start_ms: T(0, 30), end_ms: T(0, 50), state: 'done', minutes: 'lots' },
  { id: 'x-ride', lane: 'you', kind: 'ride', start_ms: T(16, 0), end_ms: T(16, 20), state: 'done', route: 27, board_stop: {}, alight_stop: ['x'] },
  null, 'x', 7,
];
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
const bar0 = buildBar(model, zh);
check('the overview bar spans the day from the hour before he woke, and its now tick is at 21:50', clock(bar0.t0, TZ) === '08:00' && Math.abs(bar0.nowF - (13 * 60 + 50) / (16 * 60)) < .001);
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
  const bad = makeDay({ items: [...makeDay().items, ...brokenItems()], labels: {}, highlights: 'x', missing: 3 }), told = [], warn = console.warn;
  console.warn = text => told.push(String(text));
  let built, said, fallback;
  try {
    built = buildDay(bad, NOW, zh);
    said = wingText(bad, NOW, zh);
    const sick = { items: [{ get kind() { throw new Error('boom'); }, start_ms: T(18, 50) }] };
    fallback = [wingText(sick, NOW, zh), wingText(sick, NOW, zh)];
  } finally { console.warn = warn; }
  const r = flat(built.blocks).filter(x => x.as === 'row');
  check('an item in another shape is read as far as it goes: the Mac block without its lists, the stay without a name, the ride without stops', r.some(x => x.title === '在 Mac 上工作' && x.detail === '') && r.some(x => x.title.startsWith('公交')) && flat(built.blocks).some(x => x.as === 'stay' && x.title === '没名字的地方') && r.some(x => x.title === '睡觉'));
  check('a day the brain sends with a list that is not a list still builds: no labels, no highlights, nothing missing', built.chips.length === 0 && built.stars.size === 0 && built.foot === '' && said.head === '停留');
  assert.throws(() => buildDay({ ...makeDay(), start_ms: null }, NOW, zh), /no usable bounds/);
  assert.throws(() => buildDay({ ...makeDay(), end_ms: T(0) }, NOW, zh), /no usable bounds/);
  assert.throws(() => buildDay(null, NOW, zh), /no usable bounds/);
  check('a day with no usable bounds cannot be built, and says so', true);
  check('the wing never throws: a day it cannot read gives the plain place, or nothing, and the log hears of it once', fallback[0].head === '' && fallback[1].head === '' && told.length === 1 && told[0].startsWith('[timeline] the wing: boom'), JSON.stringify(told));
  check('electron passes the pane\'s [timeline] lines to its log as it does [dashboard] ones', /startsWith\('\[dashboard\]'\) \|\| event\.message\.startsWith\('\[timeline\]'\)/.test(readFileSync(path.join(root, 'electron/companion.ts'), 'utf8')));
}
{
  // A day the clocks cut short or stretch: the bar's hour marks still sit at their local hours.
  const stay = start => ({ id: 's', lane: 'you', kind: 'stay', start_ms: start, end_ms: null, state: 'now', place: 'X', kept_place: home });
  const spring = { date: '2026-03-08', tz: TZ, start_ms: Date.UTC(2026, 2, 8, 8), end_ms: Date.UTC(2026, 2, 9, 7), items: [stay(Date.UTC(2026, 2, 8, 8))], labels: [], missing: [], line: null, highlights: [] };
  const fall = { ...spring, date: '2026-11-01', start_ms: Date.UTC(2026, 10, 1, 7), end_ms: Date.UTC(2026, 10, 2, 8), items: [stay(Date.UTC(2026, 10, 1, 7))] };
  for (const [name, d, hours] of [['a 23-hour day', spring, 23], ['a 25-hour day', fall, 25]]) {
    const bar = buildBar(buildDay(d, d.start_ms + 12 * 3_600_000, zh), zh), at = k => clock(Math.round(msOf(bar, k.f) / 60_000) * 60_000, TZ);
    check(`on ${name} the bar's hour marks sit at their local hours`, (d.end_ms - d.start_ms) / 3_600_000 === hours && bar.ticks.map(at).join() === '06:00,09:00,12:00,15:00,18:00,21:00,00:00', bar.ticks.map(at).join());
  }
}
check('a label is the richest that fits with 6 px each side: icon and name, name, icon, nothing', fit(100, 30, '家') === 'both' && fit(50, 30, '家') === 'name' && fit(30, 30, '家') === 'icon' && fit(20, 30, '家') === 'none' && fit(60, 0, '') === 'icon');
check('no dot or bar is amber: amber is the turn mark\'s', [...rows.filter(r => r.rgb), ...bar0.segs].every(x => !(x.rgb[0] > 240 && x.rgb[1] > 170 && x.rgb[1] < 200 && x.rgb[2] < 100)));

// ---------- the bar's own model ----------
{
  const axis = (items, now = NOW) => clock(axisOf(buildDay(small(items), now, zh)).t0, TZ);
  const sleep = (from, to, day = 19) => it('sl', 'sleep', T(from[0], from[1], day), T(to[0], to[1]), { minutes: 400 });
  check('the bar starts at 06:00 with no sleep to go by, and a stay alone never takes it back', axis([it('s', 'stay', T(1, 0), null, { place: 'X' })]) === '06:00');
  check('it starts at the hour before he woke: 08:00 for a 09:05 wake, 10:00 for a 11:40 wake', axis([sleep([0, 55], [9, 5])]) === '08:00' && axis([sleep([2, 0], [11, 40])]) === '10:00');
  check('an early riser starts at 06:00; a move before it takes the start back to its hour; a reminder does not', axis([sleep([22, 0, 18], [5, 10])]) === '06:00'
    && axis([sleep([22, 0, 18], [5, 10]), it('m', 'move', T(5, 20), T(5, 50), { activity: 'running' })]) === '05:00' && axis([sleep([0, 55], [9, 5]), it('r', 'reminder', T(6, 40), null, { text: 'x' })]) === '08:00');
  check('what he did before the wake takes the start back to its hour (a talk at 07:10 to 07:00, a work block at 02:30 to 02:00)', axis([sleep([0, 55], [9, 5]), it('t', 'talk', T(7, 10), T(7, 12), { turns: 2 })]) === '07:00'
    && axis([sleep([0, 55], [9, 5]), it('w', 'work', T(2, 30), T(3, 30), { active_s: 3000 })]) === '02:00');
  check('on today the bar always holds now: at 05:30 it starts at 05:00', axis([sleep([22, 0, 18], [5, 0])], T(5, 30)) === '05:00');
  check('the hours are ticked every three: 09 12 15 18 21 24 from 08:00', bar0.ticks.map(k => k.label).join() === '09,12,15,18,21,24' && bar0.ticks.at(-1).f === 1);
  check('a stay he named is dark ink on its warm or blue fill; a haze and a way he went are ink', toneOn([255, 168, 150], .9) === 'dark' && toneOn([100, 200, 255], .9) === 'dark' && toneOn([196, 204, 238], .32) === 'light' && toneOn([196, 208, 255], .28) === 'light');
  const ms = Object.fromEntries(bar0.segs.map(x => [x.row, x]));
  check('a sleep wins over the stay it lies on, a trip over a stay; sleep reads moon and duration, a ride its route', ms['sleep-1'].kind === 'sleep' && ms['sleep-1'].short === '7:50' && ms['ride-1'].short === '27' && ms['move-1'].short === '开车' && ms['stay-uvic'].short === 'UVic' && ms['stay-aw'].short === 'A&W');
  check('what is still to come on the bar: tonight\'s event as a box, the reminder and the bus as ticks', bar0.sched.map(x => `${x.kind}:${x.key}`).join() === 'reminder:reminder-1,event:event-1,departure:departure-1,event:event-2');
  check('the star of what mattered is at its item\'s start: 15:19 for the afternoon at UVic', bar0.stars.length === 1 && Math.abs(msOf(bar0, bar0.stars[0].f) - T(15, 19)) < 1000);

  const rm = buildDay(richDay(), NOW, zh), rbar = buildBar(rm, zh), at = (h, m) => peek(rm, rbar, T(h, m), zh);
  const lines = p => p.lines.map(l => `${l.kind}:${l.text}|${l.side}`);
  let p = at(14, 20);
  check('at 14:20 the stretch is 家, 14:05–14:50, 45 分, and nothing else was going on', p.stretch.name === '家' && p.stretch.range === '14:05–14:50' && p.stretch.length === '45 分' && p.lines.length === 0 && p.row === 'stay-home-2');
  p = at(11, 0);
  check('at 11:00 the work block says its top project and app, and how long it was active', lines(p).join() === 'work:Jarvis · Xcode|活跃 42 分' && p.row === 'stay-home-1');
  p = at(13, 22);
  check('at 13:22 the payment (2 min away) and the photo (8 min away) are what else: the payment as counterparty and amount', p.stretch.name === 'A&W' && lines(p).join() === 'payment:A&W · −$12.40|13:20,picture:汉堡和薯条|13:30');
  p = at(20, 15);
  check('at 20:15 the work block and the Claude chat, in that order; the stay is 18:50 起, 3 时', lines(p).join() === 'work:Jarvis · Ghostty|活跃 1 时 55 分,claude_chat:Claude · The bar for the day|14 条' && p.stretch.range === '18:50 起' && p.stretch.length === '3 时');
  p = at(16, 50);
  check('at 16:50 the call, and the stay is one of the day\'s stars with her words', lines(p).join() === 'call:通话 · FaceTime|18 分' && p.star.label === '下午在 UVic' && p.stretch.name === 'UVic');
  p = at(22, 35);
  check('at 22:35, still to come, the stretch is tonight\'s event and the bus is what else', p.stretch.name === '小组讨论' && p.stretch.range === '22:30–23:30' && lines(p).join() === 'departure:27 · Shelbourne at Pear|22:40 发车' && p.row === 'event-1');
  p = at(22, 10);
  check('at 22:10 nothing is recorded yet, and the reminder 10 min back is what else', p.stretch.name === '还没到' && lines(p).join() === 'reminder:把垃圾拿出去|22:00' && p.row === 'event-1');
  const gap = (() => { const d = buildDay(small([it('a', 'stay', T(9, 0), T(11, 0), { place: 'X' }), it('b', 'stay', T(12, 0), T(13, 0), { place: 'Y' })]), NOW, zh); return peek(d, buildBar(d, zh), T(11, 30), zh); })();
  check('between two stays: nothing recorded, from where the one ended to where the next began', gap.stretch.name === '没有记录' && gap.stretch.range === '11:00–12:00' && gap.stretch.length === '1 时' && gap.row === 'b');
  check('the row at a minute: the piece there, else the next row; the morning sleep at 08:30', [rowAt(rm, rbar, T(14, 20)), rowAt(rm, rbar, T(15, 0)), rowAt(rm, rbar, T(8, 30)), rowAt(rm, rbar, T(22, 15))].join() === 'stay-home-2,ride-1,sleep-1,event-1');
  check('the day\'s numbers, only the ones that are not nothing, in order: sleep, on the way, Mac, calls, her, money, photos',
    numbers(rm, zh).map(n => `${n.label} ${n.value}`).join() === '睡 7:50,路上 3:04,Mac 2:37,通话 18 分,和她 3 次,花 $12.40,照片 1');
  check('with nothing from the Mac there are no Mac or call numbers; in English the words are English', !numbers(buildDay(day, NOW, zh), zh).some(n => n.kind === 'work' || n.kind === 'call') && numbers(rm, en).map(n => n.label).join() === 'Sleep,On the way,Mac,Calls,With her,Spent,Photos');
  const paid = (cur, amount, dir = 'out') => ({ source: 'alipay', amount, currency: cur, direction: dir, counterparty: 'X', text: 'y' });
  const mixed = numbers(buildDay(small([it('p1', 'payment', T(10, 0), T(10, 0), paid('CNY', '8.00')), it('p2', 'payment', T(11, 0), T(11, 0), paid('CNY', '12.50')), it('p3', 'payment', T(12, 0), T(12, 0), paid('CAD', '4.00')), it('p4', 'payment', T(13, 0), T(13, 0), paid('CNY', '100.00', 'in'))]), NOW, zh), zh);
  check('money out in the currency paid in most, with … when it was more than one; money in is not counted', mixed.find(n => n.kind === 'payment').value === '¥20.50…' && moneyText('3.50', 'CAD') === '$3.50' && moneyText('9.00', 'CHF') === '9.00 CHF');
  const sm = summarize(rm, rbar, T(18, 0), T(14, 0), zh);
  check('a range, whichever way it was dragged: 14:00–18:00, 4 时, the places with their lengths (the most first), what else', sm.range === '14:00–18:00' && sm.length === '4 时' && sm.places.map(x => `${x.name} ${x.length}`).join() === 'UVic 2 时 41 分,家 45 分'
    && sm.nums.map(n => `${n.label} ${n.long}`).join() === '路上 34 分,通话 18 分,和她 1 次');
  const cut = within(rm.blocks, rm.span, T(14, 0), T(18, 0)), keys = (bs, out = []) => { for (const b of bs) { out.push(b.type === 'row' ? b.row.key : b.key); if (b.type === 'stay') keys(b.items, out); } return out; };
  check('the list cut to a range keeps what overlaps it: its stays with only the rows inside them that do, and the ways between', keys(cut).join() === 'move-3,stay-home-2,ride-1,talk-3,stay-uvic,call-1');
  const rows2 = flat(rm.blocks);
  check('the Mac, the imports and the Claude letter are plain rows: the payment (who, what, through which app, the amount), a chat, the letter', (r => r.detail === 'Teen Burger combo · 支付宝' && r.dur === '−$12.40')(rows2.find(r => r.title === 'A&W' && r.as === 'row'))
    && rows2.some(r => r.title === 'The bar for the day' && r.detail === '和 Claude · 14 条消息') && rows2.some(r => r.title === '和 Claude 聊了 2 个对话' && r.detail === 'The bar for the day · A tool exam'));
  check('a row says what it is for a lens: work is work, a ride is a way, a stay header is none', rm.blocks.length > 0 && flat(rm.blocks).filter(r => r.as === 'row').every(r => r.kind && (r.lens === null) === !['sleep', 'move', 'ride', 'work', 'call', 'talk', 'payment', 'picture'].includes(r.kind)));
}

// ---------- the built page ----------
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security', '--mute-audio'] });
const errors = [];
const NOTCH_R = 412.5, TOP = 32;
// A page on a fake daemon: `served` is what /inherent/day answers, `board` the sessions, `departure` the pinned trip, `hits` the day reads it got.
async function boot({ wing, marks, lang = 'zh', openBy, board = [], served = makeDay(), departure = null, init = null } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const s = { board, served, departure, hits: [], pictures: [], controls: { mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' } };
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
  if (init) await page.addInitScript(init, NOW);
  await page.route(`${daemon}/**`, route => {
    const url = new URL(route.request().url());
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    if (url.pathname === '/inherent/day') {
      s.hits.push(url.search);
      const date = url.searchParams.get('date');
      return json(!date || date === DATE ? s.served : date === YESTERDAY ? yesterday() : { ...yesterday(), date, items: [] });
    }
    if (url.pathname.startsWith('/inherent/attachments/')) {
      s.pictures.push(url.pathname);
      return url.pathname.endsWith(`/${PIC}`) ? route.fulfill({ status: 200, contentType: 'image/png', body: PICTURE }) : route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"gone"}' });
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
  if (all) {
    const a = await boot();
    await line(a);
    const r = await a.read();
    check('with nothing chosen the wing is the timeline: where he is and for how long', r.wing === 'timeline' && r.line === '家 · 3:00' && r.marks === 'line', JSON.stringify(r));
    check('the wing is at least 64 pt wide', r.hit.right - r.hit.left >= 63.5);
    await a.shot('01-wing-closed', { x: 200, y: 0, width: 300, height: 60 });
    await a.ctx.close();
  }
  if (all) {
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
  if (all) {
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
  if (all) {
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
  if (all) for (const look of ['spark', 'pixel']) {
    const a = await boot({ marks: look, board: [ask('a-1')] });
    await a.page.evaluate(() => window.__front(true));
    await a.until(() => document.querySelector('.notch')?.dataset.marks === 'turn1 line' && document.querySelector('.notch')?.dataset.line === '家 · 3:00');
    // The wing grows on a spring: read it once it has reached the room its line needs, not on the frame the words arrive.
    const seg = await a.lineX(), r = await seen(a, v => v.hit.right >= seg.x1 + 3, 8000);
    check(`the ${look} look: the turn mark, then the line, inside the wing`, r.marks === 'turn1 line' && seg.x0 >= r.hit.left + 26 && seg.x1 <= r.hit.right - 3, JSON.stringify({ ...r, seg }));
    await a.shot(`10-${look}-look`, { x: 200, y: 0, width: 330, height: 60 });
    await a.ctx.close();
  }

  // ---- Settings › Her look › Right of the notch ----
  if (all) {
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
  if (all) {
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
  if (all) {
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
  if (all) {
    const a = await boot({ openBy: 'click' });
    const seg = await line(a), on = { x: seg.mid, y: 14 };
    await a.glide(a.far, on, 300); await a.page.waitForTimeout(1200);
    check('with the Dashboard set to open on a click, a rest on the line opens nothing', await tl(a) === '');
    await a.page.mouse.down(); await a.page.mouse.up();
    check('and a click opens it', (await settle(a, 'pinned', 2000)).tl === 'pinned');
    await a.ctx.close();
  }

  // ---- never together with the Dashboard ----
  if (all) {
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
  if (all) {
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
  if (all) {
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
  if (all) {
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
  if (all) {
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
    check('the overview bar draws the day: pieces, a now tick, the hours from the morning (09 for a 09:05 wake)', await a.page.locator('.dt-seg').count() > 5 && await a.page.locator('.dt-nowtick').count() === 1 && (await text('.dt-hours span')).join() === '09,12,15,18,21,24');
    check('what the bar cannot name has no note above it and no count: its words are inside it or not there', await a.page.locator('.dt-more, .dt-notes').count() === 0);
    check('the now line and now tick are warm white, not amber', await a.page.evaluate(() => { const els = [...document.querySelectorAll('.dt-now, .dt-nowtick')]; return els.length === 2 && els.every(el => { const c = getComputedStyle(el); return /rgb\(255, 240, 220\)/.test(c.color + c.backgroundColor); }); }));
    check('the pane moves its rows (a transition) unless reduced motion is asked for', await a.page.evaluate(() => getComputedStyle(document.querySelector('.dt-row')).transitionDuration) !== '0s');
    check('the home dot is warm and the school dot is blue', await a.page.evaluate(() => { const c = [...document.querySelectorAll('.dt-sh .dt-dot')].map(d => getComputedStyle(d).backgroundColor); return c[0] === 'rgb(255, 168, 150)' && c[3] === 'rgb(100, 200, 255)'; }));
    // Scrolled: the list opens at the now line; the top is the morning.
    await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = 0; }); await a.page.waitForTimeout(300);
    await a.shot('06-pane-scrolled-top', { x: 40, y: 0, width: 560, height: 700 });
    await a.page.evaluate(() => { const l = document.querySelector('.dt-list'); l.scrollTop = l.scrollHeight; }); await a.page.waitForTimeout(300);
    await a.shot('07-pane-scrolled-bottom', { x: 40, y: 0, width: 560, height: 700 });
    const tk = await a.page.locator('.dt-track').boundingBox(), before = await a.page.evaluate(() => document.querySelector('.dt-list').scrollTop);
    await a.page.mouse.click(tk.x + 20, tk.y + tk.height / 2);
    await a.page.waitForTimeout(900);
    const jumped = before - await a.page.evaluate(() => document.querySelector('.dt-list').scrollTop);
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
  if (all) {
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
  if (all) {
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
  if (all) {
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


  // ---- a day the pane cannot read never takes the window with it ----
  if (all || only === 'guard') {
    // `boom` makes the clock throw for the one moment the pane's bar writes (now): a failure inside a render, which only a guard can hold.
    const a = await boot({ served: makeDay({ items: [...makeDay().items, ...brokenItems()], labels: {}, highlights: 'x', missing: 3 }), init: now => {
      const own = Object.getOwnPropertyDescriptor(Intl.DateTimeFormat.prototype, 'format');
      Object.defineProperty(Intl.DateTimeFormat.prototype, 'format', { configurable: true, get() { const f = own.get.call(this); return (d, ...rest) => { if (window.__boom && d === now) throw new Error('boom'); return f(d, ...rest); }; } });
    } });
    const told = [], caught = errors.length;
    a.page.on('console', m => { if (m.text().startsWith('[timeline]')) told.push(m.text()); });
    await a.until(() => document.querySelector('.notch')?.dataset.line === '停留 · 1:50');
    const seg = await a.lineX();
    await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-row'), undefined, 8000);
    await a.page.waitForTimeout(500);
    const her = () => a.page.evaluate(() => !!document.querySelector('main.companion .companion-hit') && !!document.querySelector('.notch .notch-hit') && !!document.querySelector('.companion-canvas'));
    check('a day with items in other shapes still draws: rows, the bar, her sentence, and she is there', await a.page.locator('.dt-row').count() >= 8 && await a.page.locator('.dt-track').count() === 1 && await a.page.locator('.dt-say').count() === 0 && await her());
    // a failure while drawing: the pane gives a line, the day buttons work, she stays
    await a.page.evaluate(() => { window.__boom = true; });
    await a.page.locator('.dt-row').nth(2).hover();
    await a.until(() => document.querySelector('.dt-say'), undefined, 3000);
    const say = await a.page.locator('.dt-say').innerText();
    check('a failure while drawing the day leaves a quiet line in the pane, the date and day buttons where they were, and her in the window', say === '这一天没画出来' && await a.page.locator('.dt-date').innerText() === '10月19日 周一' && await a.page.locator('.dt-nav button').first().isEnabled() && await her(), say);
    check('and the log hears of it: a [timeline] line, once', told.filter(x => /^\[timeline\] the pane: boom/.test(x)).length === 1, JSON.stringify(told));
    await a.shot('guard-01-pane-failed', { x: 40, y: 0, width: 560, height: 300 });
    await a.page.evaluate(() => { window.__boom = false; });
    await a.page.locator('.dt-nav button').first().click();
    await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日' && document.querySelector('.dt-track'), undefined, 6000);
    check('the day before draws again from the same buttons', await a.page.locator('.dt-say').count() === 0 && await her());
    // a day the brain sends with no bounds at all
    await a.page.keyboard.press('Escape');
    await a.until(() => document.querySelector('.notch')?.dataset.tl === '', undefined, 3000);
    a.s.served = { ...makeDay(), start_ms: null };
    await a.page.waitForTimeout(400);
    await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-say')?.textContent === '这一天没画出来', undefined, 8000);
    check('a day with no usable bounds says so in a line, with the day buttons, and the log hears of it', await a.page.locator('.dt-nav button').first().isEnabled() && told.some(x => /^\[timeline\] the day: the day has no usable bounds/.test(x)) && await her());
    a.page.removeAllListeners('console');
    check('and none of it was an uncaught error', errors.slice(caught).every(x => /boom|The above error|Error: /.test(x)));
    errors.length = caught;
    await a.ctx.close();
  }

  // ---- the bar: what it draws, what the pointer finds on it, a range of it, a lens ----
  if (all || only === 'bar') {
    const rich = buildDay(richDay(), NOW, zh), rbar = buildBar(rich, zh), t0 = rbar.t0, t1 = T(24);
    const a = await boot({ served: richDay() });
    const seg = await line(a);
    await a.page.mouse.move(seg.mid, 14); await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-track') && document.querySelector('.dt-row'), undefined, 8000);
    await a.page.waitForTimeout(800);
    const text = sel => a.page.locator(sel).allInnerTexts();
    const g = await a.page.evaluate(() => { const r = document.querySelector('.dt-track').getBoundingClientRect(), p = document.querySelector('.notch-drop').getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width, height: r.height, pane: { left: p.left, right: p.right, top: p.top } }; });
    const xAt = (h, m = 0) => g.left + (T(h, m) - t0) / (t1 - t0) * g.width, y = g.top + g.height / 2;
    // Away from the bar but in the pane: a pointer that leaves the pane folds it after a grace, pinned or not.
    const away = { x: g.left + 200, y: g.top - 48 };
    const rect = sel => a.page.locator(sel).first().evaluate(el => el.getBoundingClientRect().toJSON());

    // at rest
    check('the bar is 24 px tall with a 7 px radius, as wide as the pane\'s content, from 08:00 for a 09:05 wake', g.height === 24 && await a.page.locator('.dt-track').evaluate(el => getComputedStyle(el).borderTopLeftRadius === '7px') && g.width > 470 && await a.page.locator('.dt-bar').getAttribute('data-from') === '08:00');
    check('the hours are ticked every three: 09 12 15 18 21 24', (await text('.dt-hours span')).join() === '09,12,15,18,21,24' && await a.page.locator('.dt-hours span').last().evaluate(el => getComputedStyle(el).transform) !== 'none');
    check('nothing is drawn above the bar but the time of now: no label row, no +N chip', await a.page.locator('.dt-notes, .dt-more, .dt-note').count() === 0 && await a.page.evaluate(() => {
      const top = document.querySelector('.dt-track').getBoundingClientRect().top, up = [...document.querySelectorAll('.dt-bar *')].filter(el => !el.children.length && el.textContent.trim() && el.getBoundingClientRect().bottom <= top + .5);
      return up.length === 1 && up[0].textContent === '21:50' && up[0].classList.contains('is-now');
    }));
    const inside = await a.page.evaluate(() => [...document.querySelectorAll('.dt-seg, .dt-sched.is-event')].filter(el => el.querySelector('.dt-sl')).map(el => {
      const box = el.getBoundingClientRect(), parts = [...el.querySelectorAll('.dt-sl > svg, .dt-sl > b')].map(n => n.getBoundingClientRect());
      return { name: el.querySelector('.dt-sl').textContent || 'icon', left: Math.min(...parts.map(r => r.left)) - box.left, right: box.right - Math.max(...parts.map(r => r.right)), w: box.width, icon: !!el.querySelector('.dt-sl > svg'), word: !!el.querySelector('.dt-sl > b') };
    }));
    check('every label lies inside its piece with at least 6 px each side, or an icon alone, centred', inside.length >= 5 && inside.every(x => x.w >= 24 && (x.left >= 5.5 || x.left >= 0 && !x.word) && (x.right >= 5.5 || !x.word && x.right >= 0)) && inside.some(x => x.icon && x.word) && inside.some(x => x.icon && !x.word) && inside.some(x => x.word), JSON.stringify(inside));
    check('the pieces say what they are in words where they fit: a place its name, a ride its route, sleep its moon', (await text('.dt-seg .dt-sl')).filter(Boolean).join().includes('UVic') && await a.page.locator('.dt-seg.is-sleep .dt-sl svg').count() === 1);
    check('a stay he named is dark ink, a way he went is ink and hatched, still to come is dashed', await a.page.evaluate(() => {
      const dark = [...document.querySelectorAll('.dt-seg.is-dark .dt-sl')].every(el => getComputedStyle(el).color.startsWith('rgba(11, 13, 26')), trip = getComputedStyle(document.querySelector('.dt-seg.is-trip')), box = getComputedStyle(document.querySelector('.dt-sched.is-event'));
      return dark && document.querySelectorAll('.dt-seg.is-dark').length >= 2 && /repeating-linear-gradient/.test(trip.backgroundImage) && box.borderTopStyle === 'dashed';
    }));
    const now = await rect('.dt-nowtick'), nowAt = g.left + (NOW - t0) / (t1 - t0) * g.width, nowLabel = await rect('.dt-time.is-now');
    check('now is a 2 px warm white marker 5 px past the bar each side, at 21:50, its time above it; it breathes over 2.4 s', Math.abs(now.left + now.width / 2 - nowAt) < 1 && now.width === 2 && Math.abs(now.top - (g.top - 5)) < .6 && Math.abs(now.height - 34) < 1 && nowLabel.bottom <= now.top + .5
      && await a.page.locator('.dt-nowtick').evaluate(el => { const c = getComputedStyle(el, '::after'); return c.animationName === 'dt-breathe' && c.animationDuration === '2.4s' && c.animationIterationCount === 'infinite'; }));
    const stars = await a.page.locator('.dt-lit').evaluateAll(els => els.map(el => { const r = el.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; }));
    check('what mattered is a lit star on the top edge at each starred item\'s start: 15:19 and 13:20', stars.length === 2 && stars.every(s => Math.abs(s.y - g.top) < 1) && Math.abs(stars[0].x - xAt(13, 20)) < 1.5 && Math.abs(stars[1].x - xAt(15, 19)) < 1.5);
    check('the numbers are chips under the bar, each with its words', (await text('.dt-num')).join('|').replace(/\n/g, ' ') === '睡\n7:50|路上\n3:04|Mac\n2:37|通话\n18 分|和她\n3 次|花\n$12.40|照片\n1'.replace(/\n/g, ' '));
    check('the extra kinds the brain serves are rows: the payment, the Claude chat and the letter', (await text('.dt-row .dt-tx b')).some(x => x.trim() === 'A&W') && (await text('.dt-row .dt-tx em')).some(x => x === 'Teen Burger combo · 支付宝') && (await text('.dt-row .dt-tm')).some(x => /−\$12\.40/.test(x)) && (await text('.dt-row .dt-tx b')).some(x => x.trim() === 'The bar for the day') && (await text('.dt-row .dt-tx b')).some(x => x.trim() === '和 Claude 聊了 2 个对话'));
    await a.shot('bar-01-rest', { x: 40, y: 0, width: 560, height: 700 });

    // hover: three places on the bar
    const card = () => a.page.evaluate(() => {
      const c = document.querySelector('.dt-card.is-on:not(.is-range)');
      if (!c) return null;
      const cur = c.querySelector('.dt-slot.is-cur'), norm = el => el.innerText.replace(/\s*\n\s*/g, ' ').trim(), hair = document.querySelector('.dt-hair');
      return { head: norm(cur.querySelector('.dt-ch')), mark: cur.querySelector('.dt-cmark') ? norm(cur.querySelector('.dt-cmark')) : null, lines: [...cur.querySelectorAll('.dt-cline')].map(norm), none: cur.querySelector('.dt-cnone')?.innerText ?? null, foot: cur.querySelector('.dt-cfoot').innerText,
        rect: c.getBoundingClientRect().toJSON(), hair: hair?.getBoundingClientRect().toJSON(), times: [...document.querySelectorAll('.dt-time')].map(e => e.innerText), thumbs: cur.querySelectorAll('img.dt-thumb').length, hot: [...document.querySelectorAll('.dt-row.is-hot, .dt-sh.is-hot')].map(e => e.dataset.key) };
    });
    const list0 = await a.page.evaluate(() => { const l = document.querySelector('.dt-list'); return { top: l.getBoundingClientRect().top, scroll: l.scrollTop }; });
    const over = async (h, m) => { await a.move(xAt(h, m), y); await a.page.waitForTimeout(220); return card(); };
    const same = (x, want, tol = 1.5) => Math.abs(x - want) <= tol;
    let c = await over(14, 20);
    check('hovering 14:20: the stretch is 家, its range and length; nothing else was going on; the row is lit, the list did not move', c && c.head === '家 14:05–14:50 · 45 分' && c.lines.length === 0 && c.none === '这段时间没有别的记录' && c.foot === '点一下跳到这里 · 拖动框一段' && c.hot.join() === 'stay-home-2', JSON.stringify(c));
    const lst = await a.page.evaluate(() => { const l = document.querySelector('.dt-list'); return { top: l.getBoundingClientRect().top, scroll: l.scrollTop }; });
    check('the card opens at the pointer, 280 wide, under the bar and its hours (6 px below them), inside the pane, over the list without moving it; the hairline and the time follow the pointer', c && c.rect.width === 280 && same(c.rect.top, g.top + 24 + 23, 1) && c.rect.left >= g.pane.left && c.rect.right <= g.pane.right && same(c.rect.left + c.rect.width / 2, xAt(14, 20), 2)
      && lst.top === list0.top && lst.scroll === list0.scroll && c.hair && same(c.hair.left + c.hair.width / 2, xAt(14, 20), 1) && c.times.length === 1 && Math.abs(Number(c.times[0].slice(3)) - 20) <= 1 && c.times[0].startsWith('14:'), JSON.stringify({ c, lst, list0 }));
    await a.shot('bar-02-hover-stay', { x: 40, y: 0, width: 560, height: 700 });
    c = await over(20, 15);
    check('hovering 20:15: the stay he is in (18:50 起, 3 时) and, in order, the work block and the Claude chat; the row lit is that stay\'s', c && c.head === '家 18:50 起 · 3 时' && c.lines.length === 2 && /^Jarvis · Ghostty\s+活跃 1 时 55 分$/.test(c.lines[0]) && /^Claude · The bar for the day\s+14 条$/.test(c.lines[1]) && c.hot.join() === 'stay-home-3', JSON.stringify(c));
    c = await over(16, 50);
    check('hovering 16:50: the stay and her words for why it mattered, the call; the card has moved with the pointer', c && c.head === 'UVic 15:19–18:15 · 2 时 56 分' && c.mark === '下午在 UVic' && c.lines.length === 1 && /^通话 · FaceTime\s+18 分$/.test(c.lines[0]) && same(c.rect.left + c.rect.width / 2, xAt(16, 50), 2), JSON.stringify(c));
    await a.shot('bar-02b-hover-starred-stay', { x: 40, y: 0, width: 560, height: 700 });
    c = await over(15, 0);
    check('hovering the ride: its words, range and length, and its row is the one lit', c && c.head === '27 路 14:50–15:19 · 29 分' && c.hot.join() === 'ride-1' && c.lines.length === 1 && /^和她说话 · 1 句/.test(c.lines[0]), JSON.stringify(c));
    await a.shot('bar-03-hover-trip', { x: 40, y: 0, width: 560, height: 700 });
    c = await over(13, 26);
    check('hovering 13:26: the photo (with its picture) and the payment (counterparty and amount), the photo first; the starred payment shows its star', c && c.head === 'A&W 13:15–13:55 · 40 分' && c.lines.length === 2 && /^汉堡和薯条\s+13:30$/.test(c.lines[0]) && /^A&W · −\$12\.40\s+13:20$/.test(c.lines[1]) && c.thumbs === 1, JSON.stringify(c));
    check('the picture is read from the daemon by its attachment id, and shows', a.s.pictures.includes(`/inherent/attachments/${PIC}`) && await a.page.locator('.dt-card.is-on img.dt-thumb').evaluate(el => el.naturalWidth === 96 && el.clientWidth === 48 && el.clientHeight === 48));
    await a.shot('bar-04-hover-picture-payment', { x: 40, y: 0, width: 560, height: 700 });
    c = await over(22, 35);
    check('hovering what is still to come: the event is the stretch, the bus is what else', c && c.head === '小组讨论 22:30–23:30 · 1 时' && c.lines.length === 1 && /^27 · Shelbourne at Pear\s+22:40 发车$/.test(c.lines[0]) && c.hot.join() === 'event-1', JSON.stringify(c));
    await a.shot('bar-04b-hover-future', { x: 40, y: 0, width: 560, height: 700 });
    // near the right edge the card is clamped inside the pane
    await a.move(g.left + g.width - 2, y); await a.page.waitForTimeout(400);
    c = await card();
    check('at the bar\'s far end the card stays inside the pane', c && c.rect.right <= g.pane.right && c.rect.right > g.pane.right - 20, JSON.stringify(c?.rect));
    await a.move(g.left + 2, y); await a.page.waitForTimeout(400);
    c = await card();
    check('and at its first end', c && c.rect.left >= g.pane.left && c.rect.left < g.pane.left + 20, JSON.stringify(c?.rect));
    await a.move(away.x, away.y); await a.page.waitForTimeout(300);
    check('leaving the bar takes the card, the hairline and the lit row away', await a.page.locator('.dt-card.is-on').count() === 0 && await a.page.locator('.dt-hair').count() === 0 && await a.page.locator('.dt-row.is-hot, .dt-sh.is-hot').count() === 0);

    // the card follows the pointer on a spring and its words cross-fade when the stretch changes: sampled frame by frame
    const follow = async (x0, x1) => {
      await a.move(x0, y); await a.page.waitForTimeout(300);
      const done = a.page.evaluate(() => new Promise(resolve => {
        const c = document.querySelector('.dt-card:not(.is-range)'), seen = [], t0 = performance.now();
        const tick = now => { seen.push([c.getBoundingClientRect().left, ...[...c.querySelectorAll('.dt-slot')].map(el => +getComputedStyle(el).opacity)]); if (now - t0 < 500) requestAnimationFrame(tick); else resolve(seen); };
        requestAnimationFrame(tick);
      }));
      await a.page.waitForTimeout(100); await a.move(x1, y);
      const seen = await done, from = seen[0][0], to = seen.at(-1)[0], span = Math.abs(to - from);
      return { span, between: seen.filter(([v]) => Math.abs(v - from) > span * .1 && Math.abs(v - to) > span * .1).length, fading: seen.filter(([, p, q]) => p > .05 && p < .95 || q > .05 && q < .95).length };
    };
    const fl = await follow(xAt(10), xAt(20));
    check('the card follows the pointer on a spring (on its way for a few frames, then there) and its words cross-fade when the stretch changes', fl.span > 60 && fl.between >= 1 && fl.fading >= 1, JSON.stringify(fl));
    await a.move(away.x, away.y); await a.page.waitForTimeout(300);

    // a frame sheet of the card, frame by frame (a CDP screencast): coming in under the pointer, then following it to the next stretch
    {
      const cdp = await a.ctx.newCDPSession(a.page), got = [];
      cdp.on('Page.screencastFrame', ev => { got.push({ at: ev.metadata.timestamp, data: ev.data }); cdp.send('Page.screencastFrameAck', { sessionId: ev.sessionId }).catch(() => {}); });
      await cdp.send('Page.startScreencast', { format: 'png', everyNthFrame: 1 });
      await a.page.waitForTimeout(200);
      const ta = Date.now() / 1000; await a.move(xAt(14, 20), y); await a.page.waitForTimeout(450);
      const tb = Date.now() / 1000; await a.move(xAt(16, 50), y); await a.page.waitForTimeout(450);
      await cdp.send('Page.stopScreencast'); await cdp.detach();
      const pick = (from, n) => { const win = got.filter(f => f.at >= from && f.at <= from + .3); return Array.from({ length: Math.min(n, win.length) }, (_, i) => win[Math.round(i * (win.length - 1) / Math.max(1, n - 1))]).map(f => ({ ...f, ms: Math.round((f.at - from) * 1000) })); };
      const tiles = [...pick(ta, 6), ...pick(tb, 6)];
      const sheet = await a.ctx.newPage();
      await sheet.setViewport?.({ width: 1600, height: 900 }) ?? await sheet.setViewportSize({ width: 1600, height: 900 });
      await sheet.setContent(`<body style="margin:0;padding:8px;background:#222;display:grid;grid-template-columns:repeat(3,520px);gap:8px;font:12px monospace;color:#fff">${tiles.map((f, i) => `<div style="position:relative;width:520px;height:250px;overflow:hidden;border-radius:6px;background:#000 url(data:image/png;base64,${f.data}) -20px -100px/640px 900px no-repeat"><span style="position:absolute;left:6px;bottom:4px;padding:1px 5px;border-radius:3px;background:#0008">${i < 6 ? 'in' : 'follow'} +${f.ms} ms</span></div>`).join('')}</body>`);
      await sheet.screenshot({ path: path.join(dir, 'bar-frames.png'), fullPage: true, scale: 'css' });
      await sheet.close();
      check('the frame sheet of the card coming in and following the pointer is made from the screencast', tiles.length >= 8, String(tiles.length));
      await a.move(away.x, away.y); await a.page.waitForTimeout(300);
    }

    // click: the list takes the row there to its top in 300 ms, easing out, and the row flashes
    await a.page.evaluate(() => {
      const l = document.querySelector('.dt-list'); l.scrollTop = 0; window.__scroll = []; window.__up = 0;
      addEventListener('pointerup', () => { window.__up ||= performance.now(); }, true);
      const tick = now => { window.__scroll.push([now, l.scrollTop]); if (!window.__up || now - window.__up < 1100) requestAnimationFrame(tick); };
      requestAnimationFrame(tick);
    });
    await a.move(xAt(15, 30), y); await a.page.waitForTimeout(150); await a.page.mouse.down(); await a.page.mouse.up();
    await a.page.waitForTimeout(1300);
    const gl = await a.page.evaluate(() => {
      const up = window.__up, s = window.__scroll.filter(([t]) => t >= up - 1), v0 = s[0][1], v1 = s.at(-1)[1], dir = Math.sign(v1 - v0);
      const done = s.find(([, v]) => Math.abs(v - v1) <= 1.5), dur = done ? done[0] - up : null, mid = s.find(([t]) => t - up >= (dur ?? 300) / 2);
      const l = document.querySelector('.dt-list'), h = document.querySelector('[data-key="stay-uvic"]'), cs = getComputedStyle(h);
      return { v0, v1, dur, half: mid ? (mid[1] - v0) / (v1 - v0) : null, steady: s.every(([, v], i) => i === 0 || (v - s[i - 1][1]) * dir >= -.5), frames: s.filter(([, v]) => Math.abs(v - v0) > 1 && Math.abs(v - v1) > 1).length,
        top: h.getBoundingClientRect().top - l.getBoundingClientRect().top, flash: h.classList.contains('is-flash') && cs.animationName === 'dt-flash' && cs.animationDuration === '0.6s' };
    });
    check('a click at 15:30 takes the UVic stay to the top of the list (12 px down) in about 300 ms, easing out, one way, through more than a jump', Math.abs(gl.v1 - gl.v0) > 300 && gl.dur > 150 && gl.dur < 500 && gl.half > .6 && gl.steady && gl.frames >= 2 && Math.abs(gl.top - 12) <= 2, JSON.stringify(gl));
    check('and that row flashes (white .08 to nothing over 600 ms)', gl.flash, JSON.stringify(gl));
    await a.move(away.x, away.y); await a.page.waitForTimeout(200);

    // drag: a range, its card, the list cut to it, and a chip to clear it
    const drag = async (x0, x1) => { await a.move(x0, y); await a.page.mouse.down(); await a.move((x0 + x1) / 2, y); await a.move(x1, y); await a.page.waitForTimeout(120); };
    const px = x => msOf(rbar, (x - g.left) / g.width), x14 = xAt(14) + .1, x18 = xAt(18) + .1, ranged = summarize(rich, rbar, px(x14), px(x18), zh);
    await drag(x14, x18);
    const mid = await a.page.evaluate(() => ({ band: document.querySelector('.dt-band')?.getBoundingClientRect().toJSON() ?? null, times: [...document.querySelectorAll('.dt-time')].map(e => e.innerText), card: document.querySelectorAll('.dt-card.is-on').length, hair: document.querySelectorAll('.dt-hair').length, hot: document.querySelectorAll('.is-hot').length }));
    check('dragging draws a band from where it began to the pointer, its two times above its edges, and no hover card or hairline', mid.band && Math.abs(mid.band.left - x14) < 2 && Math.abs(mid.band.right - x18) < 2 && mid.times.join() === '14:00,18:00' && mid.card === 0 && mid.hair === 0 && mid.hot === 0, JSON.stringify(mid));
    await a.page.mouse.up(); await a.page.waitForTimeout(250);
    const rc = await a.page.evaluate(() => {
      const c = document.querySelector('.dt-card.is-range.is-on');
      if (!c) return null;
      const norm = el => el.innerText.replace(/\s*\n\s*/g, ' ').trim(), band = document.querySelector('.dt-band');
      return { head: norm(c.querySelector('.dt-ch')), lines: [...c.querySelectorAll('.dt-cline')].map(norm), buttons: [...c.querySelectorAll('button')].map(norm), rect: c.getBoundingClientRect().toJSON(), band: band.getBoundingClientRect().toJSON(), on: document.querySelectorAll('.dt-card.is-on').length };
    });
    check('a range says its span and length, the places in it with their lengths (most first), then the other numbers; one button, 只看这段', rc && rc.head === `${ranged.range} ${ranged.length}` && rc.head === '14:00–18:00 4 时' && rc.lines.join('|') === [...ranged.places.map(p => `${p.name} ${p.length}`), ...ranged.nums.map(n => `${n.label} ${n.long}`)].join('|') && rc.lines.length >= 4 && rc.buttons.join() === '只看这段' && rc.on === 1, JSON.stringify(rc));
    check('the range card sits where the hover card does, 280 wide, centred on the range, inside the pane; the band stays', rc && rc.rect.width === 280 && same(rc.rect.top, g.top + 24 + 23, 1) && same(rc.rect.left + 140, (x14 + x18) / 2, 2) && rc.rect.left >= g.pane.left && rc.rect.right <= g.pane.right && Math.abs(rc.band.left - x14) < 2 && Math.abs(rc.band.right - x18) < 2, JSON.stringify(rc));
    await a.shot('bar-05-range-card', { x: 40, y: 0, width: 560, height: 700 });
    await a.page.locator('.dt-cbtn').click(); await a.page.waitForTimeout(350);
    const cut = await a.page.evaluate(() => ({
      heads: [...document.querySelectorAll('.dt-sh')].map(e => e.dataset.key), rows: [...document.querySelectorAll('.dt-row')].map(e => e.dataset.key).sort(), scroll: document.querySelector('.dt-list').scrollTop,
      chip: document.querySelector('.dt-filter')?.innerText.replace(/\s+/g, ' ').trim(), applied: document.querySelectorAll('.dt-band.is-applied').length, bands: document.querySelectorAll('.dt-band').length, card: document.querySelectorAll('.dt-card.is-on').length, now: document.querySelectorAll('.dt-now').length }));
    check('「只看这段」 cuts the list to what overlaps the range (two stays, the ways and moments in them, from the top), with a 只看 14:00–18:00 chip and the range kept faintly on the bar', cut.heads.join() === 'stay-home-2,stay-uvic' && cut.rows.join() === 'call-1,move-3,ride-1,talk-3' && cut.scroll === 0 && cut.chip === '只看 14:00–18:00' && cut.applied === 1 && cut.bands === 1 && cut.card === 0 && cut.now === 0, JSON.stringify(cut));
    await a.shot('bar-05b-cut-to-range', { x: 40, y: 0, width: 560, height: 700 });
    await a.page.locator('.dt-filter button').click(); await a.page.waitForTimeout(350);
    check('the chip\'s × gives back the whole day: five stays, the now line, no chip', await a.page.locator('.dt-sh').count() === 5 && await a.page.locator('.dt-now').count() === 1 && await a.page.locator('.dt-filter, .dt-band').count() === 0);

    // lenses: a number under the pointer previews, a click pins, a click or Esc lets go
    const chipAt = async word => { const b = await a.page.locator('.dt-num', { hasText: word }).boundingBox(); return { x: b.x + b.width / 2, y: b.y + b.height / 2 }; };
    const lensNow = () => a.page.evaluate(() => ({ lens: document.querySelector('.dt')?.dataset.lens ?? null, pinned: [...document.querySelectorAll('.dt-num.is-pinned')].map(e => e.innerText.split('\n')[0]), pressed: document.querySelectorAll('.dt-num[aria-pressed="true"]').length }));
    const looks = () => a.page.evaluate(() => {
      const o = q => { const e = document.querySelector(q); return e ? getComputedStyle(e).opacity : null; };
      return { seg: o('.dt-seg.is-stay'), ground: o('.dt-ground'), sleep: o('.dt-row[data-key="sleep-1"]'), talk: o('.dt-row[data-key="talk-2"]'), ride: o('.dt-row[data-key="ride-1"]'), head: o('.dt-sh'), marks: [...document.querySelectorAll('.dt-lm')].map(e => { const r = e.getBoundingClientRect(); return { left: r.left, width: r.width, rgb: getComputedStyle(e).backgroundColor }; }) };
    });
    let p0 = await chipAt('睡'); await a.move(p0.x, p0.y); await a.page.waitForTimeout(350);
    let lk = await looks(), ln = await lensNow();
    const sleepLens = rbar.lens.sleep;
    check('a number under the pointer previews its lens: the bar steps back (.25) and draws the sleep in its colour, its row stays, the other rows (.35) and stay headers (.6) step back', ln.lens === 'sleep' && ln.pinned.length === 0 && lk.seg === '0.25' && lk.ground === '0.25' && lk.sleep === '1' && lk.talk === '0.35' && lk.head === '0.6' && lk.marks.length === sleepLens.length && lk.marks.length === 1 && lk.marks[0].rgb === 'rgb(112, 120, 205)' && Math.abs(lk.marks[0].left - g.left) < 1.5, JSON.stringify({ lk, ln }));
    p0 = await chipAt('Mac'); await a.move(p0.x, p0.y); await a.page.waitForTimeout(350);
    await a.shot('bar-06-lens-mac-preview', { x: 40, y: 0, width: 560, height: 700 });
    lk = await looks();
    check('the Mac lens lights the work blocks in blue, and the next number takes over from the last', (await lensNow()).lens === 'work' && lk.marks.length === rbar.lens.work.length && lk.marks.length === 2 && lk.marks.every(m => m.rgb === 'rgb(108, 156, 255)'), JSON.stringify(lk));
    await a.move(away.x, away.y); await a.page.waitForTimeout(350);
    check('leaving the number lets the lens go', (await lensNow()).lens === null && (await looks()).seg === '1');
    p0 = await chipAt('路上'); await a.move(p0.x, p0.y); await a.page.mouse.down(); await a.page.mouse.up(); await a.move(away.x, away.y); await a.page.waitForTimeout(350);
    lk = await looks(); ln = await lensNow();
    const ring = await a.page.locator('.dt-num.is-pinned').evaluate(el => getComputedStyle(el).boxShadow);
    check('a click pins it: a 1 px ink ring on the number, aria-pressed, and the lens stays with the pointer away; five ways drawn in the pale ink', ln.lens === 'trip' && ln.pinned.join() === '路上' && ln.pressed === 1 && /inset/.test(ring) && /238, 240, 251/.test(ring) && lk.marks.length === 5 && lk.marks.every(m => m.rgb === 'rgb(196, 208, 255)') && lk.ride === '1' && lk.talk === '0.35' && lk.head === '0.6' && lk.seg === '0.25', JSON.stringify({ lk, ln, ring }));
    await a.shot('bar-07-lens-trip-pinned', { x: 40, y: 0, width: 560, height: 700 });
    p0 = await chipAt('通话'); await a.move(p0.x, p0.y); await a.page.waitForTimeout(300);
    const over2 = (await lensNow()).lens; await a.move(away.x, away.y); await a.page.waitForTimeout(300);
    const back2 = (await lensNow()).lens;
    check('another number under the pointer previews over the pinned one, and the pinned one comes back', over2 === 'call' && back2 === 'trip', JSON.stringify({ over2, back2, p0, at: await a.page.evaluate(([x, y]) => { const e = document.elementFromPoint(x, y); return e ? e.tagName + '.' + e.className + ' < ' + (e.parentElement?.className ?? '') : null; }, [p0.x, p0.y]) }));
    p0 = await chipAt('路上'); await a.move(p0.x, p0.y); await a.page.mouse.down(); await a.page.mouse.up(); await a.move(away.x, away.y); await a.page.waitForTimeout(300);
    check('a click on the pinned number lets it go', (await lensNow()).lens === null && (await lensNow()).pressed === 0);
    p0 = await chipAt('通话'); await a.move(p0.x, p0.y); await a.page.mouse.down(); await a.page.mouse.up(); await a.move(away.x, away.y); await a.page.waitForTimeout(300);
    const pinnedCall = (await lensNow()).lens;
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(300);
    check('Esc lets a pinned lens go and leaves the pane open', pinnedCall === 'call' && (await lensNow()).lens === null && (await a.read()).dropOpen);

    // a short day (the day before has two stays and no words): the cards still fit in the pane
    await a.page.locator('.dt-nav button').first().click();
    await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日' && document.querySelector('.dt-track'), undefined, 6000);
    await a.page.waitForTimeout(700);
    const g2 = await a.page.evaluate(() => { const r = document.querySelector('.dt-track').getBoundingClientRect(), p = document.querySelector('.notch-drop').getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width, bottom: p.bottom, pane: { left: p.left, right: p.right } }; });
    const y2 = g2.top + 12, cardBox = () => a.page.evaluate(() => { const c = document.querySelector('.dt-card.is-on'); return c ? c.getBoundingClientRect().toJSON() : null; });
    await a.move(g2.left + g2.width * .5, y2); await a.page.waitForTimeout(300);
    let cb = await cardBox();
    check('on a short day the hover card is inside the pane', cb && cb.bottom <= g2.bottom - 6, JSON.stringify({ cb, bottom: g2.bottom }));
    await a.shot('bar-08-yesterday-hover', { x: 40, y: 0, width: 560, height: 520 });
    const before = g2.bottom;
    await a.move(g2.left + 6, y2); await a.page.mouse.down(); await a.move(g2.left + g2.width * .3, y2); await a.move(g2.left + g2.width * .6, y2); await a.page.mouse.up(); await a.page.waitForTimeout(900);
    cb = await cardBox();
    const g3 = await a.page.evaluate(() => document.querySelector('.notch-drop').getBoundingClientRect().bottom), btn = await a.page.locator('.dt-cbtn').boundingBox();
    check('and so is the range card with its button, even when it is taller than the day: the pane makes room for it, and gives it back', cb && cb.bottom <= g3 - 6 && g3 > before + 10 && btn.y + btn.height <= g3, JSON.stringify({ cb, bottom: g3, before }));
    await a.shot('bar-09-yesterday-range', { x: 40, y: 0, width: 560, height: 520 });
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(900);
    check('and when the range goes the pane is as short as it was', Math.abs(await a.page.evaluate(() => document.querySelector('.notch-drop').getBoundingClientRect().bottom) - before) < 2);

    await a.ctx.close();
  }
  // ---- the bar's Esc ladder, a picture that is gone, and reduced motion ----
  if (all || only === 'bar') {
    const rich = buildDay(richDay(), NOW, zh), rbar = buildBar(rich, zh), t0 = rbar.t0, t1 = T(24);
    const a = await boot({ served: richDay() });
    const seg = await line(a);
    await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-track') && document.querySelector('.dt-row'), undefined, 8000);
    await a.page.waitForTimeout(700);
    const g = await a.page.evaluate(() => { const r = document.querySelector('.dt-track').getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width }; });
    const xAt = (h, m = 0) => g.left + (T(h, m) - t0) / (t1 - t0) * g.width, y = g.top + 12, away = { x: g.left + 200, y: g.top - 48 };
    const drag = async (x0, x1) => { await a.move(x0, y); await a.page.mouse.down(); await a.move((x0 + x1) / 2, y); await a.move(x1, y); await a.page.mouse.up(); await a.page.waitForTimeout(250); };
    const ladder = () => a.page.evaluate(() => ({ range: document.querySelectorAll('.dt-card.is-range').length, lens: document.querySelector('.dt')?.dataset.lens ?? null, cut: document.querySelectorAll('.dt-filter').length, open: document.querySelector('.notch-drop')?.classList.contains('is-open') ?? false }));
    await drag(xAt(14) + .1, xAt(18) + .1); await a.page.locator('.dt-cbtn').click(); await a.page.waitForTimeout(250);
    const chip = await a.page.locator('.dt-num', { hasText: '路上' }).boundingBox();
    await a.move(chip.x + chip.width / 2, chip.y + chip.height / 2); await a.page.mouse.down(); await a.page.mouse.up(); await a.move(away.x, away.y);
    await drag(xAt(11) + .1, xAt(12) + .1);
    const all3 = await ladder();
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(200); const e1 = await ladder();
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(200); const e2 = await ladder();
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(200); const e3 = await ladder();
    check('Esc lets go one thing at a time: the range, then the lens, then the cut, and only then the pane',
      all3.range === 1 && all3.lens === 'trip' && all3.cut === 1 && all3.open
      && e1.range === 0 && e1.lens === 'trip' && e1.cut === 1 && e1.open
      && e2.lens === null && e2.cut === 1 && e2.open
      && e3.cut === 0 && e3.open, JSON.stringify({ all3, e1, e2, e3 }));
    await a.page.keyboard.press('Escape');
    const closed = await seen(a, r => r.tl === '', 3000);
    check('and the next Esc folds the pane', closed.tl === '' && !closed.dropOpen, JSON.stringify(closed));
    await a.ctx.close();
  }
  if (all || only === 'bar') {
    // a picture the daemon no longer has: the line keeps its icon, nothing is broken
    const day = richDay(); day.items.find(i => i.id === 'picture-1').attachment = 'b'.repeat(32);
    const rbar = buildBar(buildDay(day, NOW, zh), zh), t0 = rbar.t0, t1 = T(24);
    const a = await boot({ served: day });
    const seg = await line(a);
    await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-track') && document.querySelector('.dt-row'), undefined, 8000);
    await a.page.waitForTimeout(700);
    const g = await a.page.evaluate(() => { const r = document.querySelector('.dt-track').getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width }; });
    await a.move(g.left + (T(13, 26) - t0) / (t1 - t0) * g.width, g.top + 12); await a.page.waitForTimeout(600);
    const gone = await a.page.evaluate(() => { const l = [...document.querySelectorAll('.dt-card.is-on .dt-slot.is-cur .dt-cline')].find(e => e.classList.contains('has-pic')); return l ? { img: l.querySelectorAll('img').length, icon: l.querySelectorAll('.dt-cic svg').length, text: l.innerText.replace(/\s+/g, ' ').trim() } : null; });
    check('a picture the daemon no longer has leaves its note and an icon, not a broken image', gone && gone.img === 0 && gone.icon === 1 && gone.text === '汉堡和薯条 13:30' && a.s.pictures.includes(`/inherent/attachments/${'b'.repeat(32)}`), JSON.stringify(gone));
    await a.ctx.close();
  }
  if (all || only === 'bar') {
    // reduced motion: the marker stops breathing, the card does not slide or follow, a click jumps, nothing flashes
    const rich = buildDay(richDay(), NOW, zh), rbar = buildBar(rich, zh), t0 = rbar.t0, t1 = T(24);
    const a = await boot({ served: richDay() });
    await a.page.emulateMedia({ reducedMotion: 'reduce' });
    const seg = await line(a);
    await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-track') && document.querySelector('.dt-row'), undefined, 8000);
    await a.page.waitForTimeout(700);
    const g = await a.page.evaluate(() => { const r = document.querySelector('.dt-track').getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width }; });
    const xAt = (h, m = 0) => g.left + (T(h, m) - t0) / (t1 - t0) * g.width, y = g.top + 12;
    await a.move(xAt(10), y); await a.page.waitForTimeout(300);
    const still = await a.page.evaluate(() => ({ breath: getComputedStyle(document.querySelector('.dt-nowtick'), '::after').animationName, cardIn: getComputedStyle(document.querySelector('.dt-card-in')).transitionDuration, cardMove: getComputedStyle(document.querySelector('.dt-card-in')).transform, row: getComputedStyle(document.querySelector('.dt-row')).transitionDuration, seg: getComputedStyle(document.querySelector('.dt-seg')).transitionDuration, num: getComputedStyle(document.querySelector('.dt-num')).transitionDuration }));
    check('with reduced motion the now marker does not breathe and the card, the rows and the pieces do not slide or ease', still.breath === 'none' && still.cardIn === '0s' && still.cardMove === 'none' && still.row === '0s' && still.seg === '0s', JSON.stringify(still));
    const done = a.page.evaluate(() => new Promise(resolve => {
      const c = document.querySelector('.dt-card:not(.is-range)'), seen = [], t0 = performance.now();
      const tick = now => { seen.push(c.getBoundingClientRect().left); if (now - t0 < 400) requestAnimationFrame(tick); else resolve(seen); };
      requestAnimationFrame(tick);
    }));
    await a.page.waitForTimeout(100); await a.move(xAt(20), y);
    const lefts = await done, from = lefts[0], to = lefts.at(-1), span = Math.abs(to - from);
    check('and the card is under the pointer from the next frame, with no spring', span > 60 && lefts.filter(v => Math.abs(v - from) > span * .1 && Math.abs(v - to) > span * .1).length === 0, JSON.stringify({ span, lefts: lefts.slice(0, 8) }));
    await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = document.querySelector('.dt-list').scrollHeight; });
    await a.move(xAt(15, 30), y); await a.page.waitForTimeout(100); await a.page.mouse.down(); await a.page.mouse.up(); await a.page.waitForTimeout(100);
    const inst = await a.page.evaluate(() => { const l = document.querySelector('.dt-list'), h = document.querySelector('[data-key="stay-uvic"]'); return { top: h.getBoundingClientRect().top - l.getBoundingClientRect().top, anim: getComputedStyle(h).animationName }; });
    check('a click jumps at once (no glide) and the row does not flash', Math.abs(inst.top - 12) <= 2 && inst.anim === 'none', JSON.stringify(inst));
    await a.ctx.close();
  }

  console.log(`\n${checks.length} checks passed; screenshots in ${dir}`);
} finally {
  await browser.close();
  server.kill();
}
if (errors.length) { console.error('page errors:', errors); process.exit(1); }

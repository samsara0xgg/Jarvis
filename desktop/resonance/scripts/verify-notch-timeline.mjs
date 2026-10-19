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
import { askable, buildDay, changeId, clock, covered, dateTitle, doingAt, doingOf, doingWords, fnv, keptOf, lensOf, moneyText, shiftDate, stageLook, stageMinutes, stageSlices, tidy, tidyReply, weatherOf, wingText, within, workoutOf } from '../src/dayView.ts';
import { axisOf, buildBar, counted, fit, fOf, hm, msOf, numbers, peek, pieceAt, rowAt, summarize, toneOn } from '../src/barView.ts';
import { answerOf, askOf, fromTo, hasBody, openOf, sayOf, sayRange, talkLines } from '../src/dayOpen.ts';
import { cardOf } from '../src/numberCard.ts';
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
// The other days the list has to hold up on, from what real days look like (no sleep item yet, the Mac missing on the brain, a few highlights).
const at4 = n => T(Math.floor(n / 100), n % 100);
const ev = (id, kind, from, to, extra = {}, lane = 'you') => it(id, kind, at4(from), to === null ? null : at4(to), extra, lane);
// A fragmented day, as a phone's GPS makes them: 15 stays (four of them an unnamed few minutes between two ways, a stop and not a place; four more
// a minute or two beside a place), 11 moves (the last one going on), 2 rides, 3 talks, a reminder, an event, 4 highlights.
const gym = { id: 'p-gym', name: '健身房', role: 'other' };
const fragmentedDay = () => makeDay({
  items: [
    ev('f-home-1', 'stay', 650, 825, { place: '12 Example St, Victoria', kept_place: home }),
    ev('f-w1', 'move', 825, 837, { activity: 'walking' }),
    ev('f-s2', 'stay', 837, 842, { place: 'Shelbourne at Pear, Victoria' }),
    ev('f-r1', 'ride', 842, 905, { route: '14', board_stop: 'Shelbourne at Pear', alight_stop: 'UVic Exchange', source: 'tapped', ended_reason: 'left_vehicle', credit: null }),
    ev('f-talk-1', 'talk', 850, 853, { turns: 3 }, 'her'),
    ev('f-w2', 'move', 905, 912, { activity: 'walking' }),
    ev('f-uvic-1', 'stay', 912, 1140, { place: 'University of Victoria, Victoria', kept_place: uvic }),
    ev('f-talk-2', 'talk', 1010, 1018, { turns: 8 }, 'her'),
    ev('f-s3b', 'stay', 1140, 1144, { place: 'University of Victoria, Victoria' }),
    ev('f-w3', 'move', 1144, 1148, { activity: 'walking' }),
    ev('f-sub', 'stay', 1148, 1220, { place: 'Student Union Building, 3800 Finnerty Rd, Victoria' }),
    ev('f-w4', 'move', 1220, 1228, { activity: 'walking' }),
    ev('f-s5', 'stay', 1228, 1234, { place: 'McPherson Library, Victoria' }),
    ev('f-w5', 'move', 1234, 1241, { activity: 'walking' }),
    ev('f-uvic-2', 'stay', 1241, 1508, { place: 'University of Victoria, Victoria', kept_place: uvic }),
    ev('f-s6b', 'stay', 1508, 1511, { place: 'University of Victoria, Victoria' }),
    ev('f-w6', 'move', 1511, 1518, { activity: 'walking' }),
    ev('f-s7', 'stay', 1518, 1523, { place: 'Finnerty Rd, Victoria' }),
    ev('f-r2', 'ride', 1523, 1551, { route: '26', board_stop: 'Finnerty at Ring Rd', alight_stop: 'Mayfair Mall', source: 'tapped', ended_reason: 'left_vehicle', credit: null }),
    ev('f-w7', 'move', 1551, 1555, { activity: 'walking' }),
    ev('f-mall', 'stay', 1555, 1646, { place: 'Mayfair Mall, 3147 Douglas St, Victoria' }),
    ev('f-s8b', 'stay', 1646, 1649, { place: 'Mayfair Mall, 3147 Douglas St, Victoria' }),
    ev('f-w8', 'move', 1649, 1655, { activity: 'walking' }),
    ev('f-s9', 'stay', 1655, 1701, { place: 'Parking Lot, Victoria' }),
    ev('f-w9', 'move', 1701, 1720, { activity: 'automotive' }),
    ev('f-gym', 'stay', 1720, 1834, { place: 'Gym, Victoria', kept_place: gym }),
    ev('f-s10b', 'stay', 1834, 1838, { place: 'Gym, Victoria' }),
    ev('f-w10', 'move', 1838, 1852, { activity: 'automotive' }),
    ev('f-home-2', 'stay', 1852, 2130, { place: '12 Example St, Victoria', kept_place: home }),
    ev('f-remind', 'reminder', 1900, null, { text: '交房租', fired: true, acknowledged: true }, 'her'),
    ev('f-talk-3', 'talk', 2030, 2036, { turns: 5 }, 'her'),
    ev('f-w11', 'move', 2130, null, { activity: 'walking' }),
    ev('f-event', 'event', 2300, 2330, { title: '电话会议', source: 'plan' }),
  ],
  labels: [], missing: ['mac'],
  line: { text: '今天几乎都在 UVic，下午去了一趟 Mayfair，晚上从健身房回来。', draft: false, written_at: '2026-10-19T21:30:00-07:00' },
  highlights: [{ id: 'f-uvic-1', label: '上午的课', by: 'her' }, { id: 'f-gym', label: '', by: 'him' }, { id: 'f-r2', label: '去 Mayfair', by: 'her' }, { id: 'f-talk-3', label: 'talk 20:30', by: 'him' }],
});
// A day of talks and nothing else: no place was recorded.
const talkDay = () => makeDay({
  items: [[810, 818, 4], [940, 944, 2], [1115, 1130, 9], [1330, 1333, 1], [1505, 1512, 6], [1820, 1822, 1], [2045, 2100, 12]].map(([a, b, n], i) => ev(`k-${i + 1}`, 'talk', a, b, { turns: n }, 'her')),
  labels: [], missing: ['mac'], line: null, highlights: [],
});
// Two and a half hours into the day: one stay since midnight, and one event this afternoon.
const EARLY = T(2, 30);
const earlyDay = () => makeDay({
  now_ms: EARLY, labels: [], missing: ['mac'], line: null, highlights: [],
  items: [ev('e-home', 'stay', 0, null, { place: '12 Example St, Victoria', kept_place: home, state: 'now' }), ev('e-event', 'event', 1500, 1600, { title: '牙医', state: 'next' })],
});
// What the brain added (spec-r3 section 11), on one day: the weather and the body's numbers on the envelope; a night with its stages (23:10 to 07:00:
// 70 min of deep sleep, 100 of REM, 270 of core, three times awake for 30, and a phone's `in_bed` under all of it that begins before and ends after);
// a 35 min run; six Claude Code sessions, four at home (two pairs side by side) and two at UVic (side by side).
// Each stage runs from the end of the one before (the first from 23:10 the day before) to the time given (hhmm).
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
// What mail noted that he has to go to (ADR 0274): the item is `commitment-<hex>@<day>`, and the hex is what the brain's route takes. Two that mail has not
// confirmed (22:10 and 23:00, both still to come: they wait on him) and one he said himself.
const HEX = { a: 'c1'.repeat(16), b: 'd2'.repeat(16), c: 'e3'.repeat(16) };
const CM = Object.fromEntries(Object.entries(HEX).map(([k, h]) => [k, `commitment-${h}@${DATE}`]));
const commitDay = (over = {}) => fullDay({
  items: [
    ...fullDay().items.filter(x => x.kind !== 'event'),
    it(CM.a, 'event', T(22, 10), T(22, 50), { title: '组会', place: 'Library', private: false, source: 'mail', confirmed: false }),
    it(CM.b, 'event', T(23, 0), T(23, 30), { title: '牙医', place: 'Clinic', private: false, source: 'mail', confirmed: false }),
    it(CM.c, 'event', T(23, 40), T(23, 55), { title: '跑步', private: false, source: 'said', confirmed: true }),
  ],
  ...over,
});
// Stays he has not named, with the point the phone had (ADR 0296): an address the phone knew (A&W), a spot it had no address for, one with no point at all, a
// kept school beside them, and a stop on the way (5 minutes between two walks), which is no place.
const nameDay = (over = {}) => small([
  it('stay-uvic', 'stay', T(9, 0), T(12, 0), { place: 'University of Victoria, Victoria', kept_place: uvic, lat: 48.4634, lng: -123.3117 }),
  it('move-a', 'move', T(12, 0), T(12, 10), { activity: 'walking' }),
  it('stay-stop', 'stay', T(12, 10), T(12, 15), { place: 'Finnerty Rd, Victoria', lat: 48.4668, lng: -123.3135 }),
  it('move-b', 'move', T(12, 15), T(12, 25), { activity: 'walking' }),
  it('stay-aw', 'stay', T(13, 15), T(13, 55), { place: 'A&W, 1234 Douglas St, Victoria', lat: 48.4284, lng: -123.3656 }),
  it('stay-spot', 'stay', T(15, 0), T(18, 30), { lat: 48.4631, lng: -123.3123 }),
  it('stay-bare', 'stay', T(19, 0), T(20, 0), { place: 'Parkade, Victoria' }),
], over);
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

// What he was doing (ADR 0302), laid over makeDay's stays, each as the brain serves a span of the doing lane: titled from facts (work at home with a project, a
// ride by its route, sleep), a home span with no title, his own answer (stated), and two that must never show: a planned one over now and one of another lane.
const sp = (id, from, to, over = {}) => ({ id, lane: 'doing', kind: 'segment', start_ms: at4(from), end_ms: to === null ? null : at4(to), title: null, detail: null, category: 'unknown', basis: 'observed', confidence: 1,
  confirmed: false, clues: [], question: null, place_id: null, item_ids: [], ...over });
const SPANS = [
  sp('d-sleep', 55, 905, { title: 'sleep', category: 'sleep' }),
  sp('d-work', 930, 1120, { title: 'Jarvis', category: 'work' }),
  sp('d-snack', 1315, 1335, { title: '吃午饭', category: 'meal', basis: 'stated', confirmed: true }),
  sp('d-home', 1405, 1450, { category: 'home' }),
  sp('d-ride', 1450, 1519, { title: '27', category: 'commute' }),
  sp('d-study', 1519, 1730, { title: '在图书馆写作业', category: 'study', basis: 'stated', confirmed: true }),
  sp('d-eve', 1935, null, { title: 'Jarvis', category: 'work' }),
  { ...sp('d-place', 2000, 2200, { title: '不该看到' }), lane: 'place' },
  sp('d-plan', 2100, 2300, { title: '睡前读书', category: 'rest', kind: 'planned', basis: 'inferred' }),
];
const doingDay = (over = {}) => makeDay({ spans: SPANS, ...over });

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
check('the day is titled, with her sentence, the label and the missing source', model.title === '10月19日 周一' && model.today && model.line.text.startsWith('今天一半在路上') && model.chips[0].text === '期中周' && model.foot === '还没读到：Mac 的时间');
check('stays are named by the kept place, else the phone\'s first part', rows.filter(r => r.as === 'stay').map(r => r.title).join() === '家,A&W,家,UVic,家');
check('home is warm, school is blue, an unnamed-by-him stay is the neutral haze', row('家').rgb.join() === '255,168,150' && row('UVic').rgb.join() === '100,200,255' && row('A&W').rgb.join() === '196,204,238');
check('a stay with nothing to call it says so', buildDay(makeDay({ items: [it('s', 'stay', T(20, 0), null, {})] }), NOW, zh).blocks[0].head.name === '没名字的地方');
check('durations read 40 分 and 1 时 40 分, in the right column', row('开车').right === '1 时 40 分' && row('步行').right === '10 分' && rows.find(r => r.as === 'stay' && r.title === 'UVic').dur === '2 时 56 分' && rows.find(r => r.as === 'stay' && r.title === 'A&W').dur === '40 分');
check('times are HH:mm in the day\'s zone', rows.find(r => r.as === 'stay' && r.title === 'UVic').when === '15:19–18:15' && rows.find(r => r.as === 'stay' && r.title === '家').when === '09:05–11:25');
check('a bus ride is a connector with its stops and its length', (r => r.connector && r.detail === 'Shelbourne at Pear → UVic Exchange' && r.icon === 'bus' && r.right === '29 分')(row('27 路')));
check('a talk is 和她说话 with its lines; a photo is 照片 with its first line', row('和她说话').detail === '6 句' && row('照片').detail === '汉堡和薯条');
check('a private event is 私事 and says no more; an event says where it came from and where; a reminder is its text', row('私事').detail === '' && row('小组讨论').detail === '邮件 · 还没确认 · Library' && row('给银行打电话') && row('把垃圾拿出去').future);
check('her highlight stars the stay and gives her words', row('UVic').detail === '下午在 UVic' && row('家').detail === '');
check('the now line sits in the stay he is in, after what has begun and before what is to come', (i => i > 0 && rows[i - 1].title === '和她说话' && rows[i + 1].title === '把垃圾拿出去' && rows[i + 1].future)(rows.findIndex(r => r.as === 'now')) && row('现在 21:50'));
const bar0 = buildBar(model, zh);
check('the overview bar spans the day from the hour before he woke, and its now tick is at 21:50', clock(bar0.t0, TZ) === '08:00' && Math.abs(bar0.nowF - (13 * 60 + 50) / (16 * 60)) < .001);
{
  // Mac time (a work block, a call) comes with a Mac that has reported, which the realistic day above has not.
  const mac = buildDay(makeDay({ missing: [], items: [it('work-1', 'work', T(15, 30), T(17, 40), { active_s: 7800, projects: [{ name: 'jarvis', seconds: 5000 }], apps: [{ name: 'Ghostty', seconds: 3000 }] }), it('call-1', 'call', T(19, 0), T(19, 25), { app: 'Zoom' })] }), NOW, zh);
  const r = flat(mac.blocks);
  check('Mac time: a work block says its top project and app and, at the right, how long it was active; a call its app', (w => w?.detail === 'jarvis · Ghostty' && w.right === '活跃 2 时 10 分')(r.find(x => x.title === '在 Mac 上工作')) && r.some(x => x.title === '通话 · Zoom') && mac.foot === '');
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
  // Absurd values from the brain skip the item (or the field) they are in; they never fail the day. A time is a number a Date can hold
  // (8.64e15 ms either way); a name that is a key of every object ('constructor') is no stage, no weather and no source.
  const MAX = 8.64e15, huge = [1e300, MAX + 2, -MAX - 2, Infinity], kept = flat(buildDay(makeDay(), NOW, zh).blocks).filter(x => x.as !== 'now').length;
  const withItems = bad => buildDay(makeDay({ items: [...makeDay().items, ...bad] }), NOW, zh), counted = d => flat(d.blocks).filter(x => x.as !== 'now').length;
  check('an item whose time no Date can hold is left out and the rest of the day stands: a start of 1e300, past 8.64e15, before -8.64e15; an end of 1e300',
    huge.every(v => counted(withItems([it('x1', 'stay', v, null, { place: 'X' })])) === kept) && counted(withItems([it('x2', 'move', T(21, 0), 1e300, { activity: 'walking' })])) === kept);
  const departure = extra => flat(withItems([it('x3', 'departure', T(22, 50), T(22, 50), { title: '27', route: '27', board_stop: 'A', ...extra })]).blocks).find(r => r.key === 'x3');
  check('a time field no Date can hold is dropped from its item: the departure keeps its row without when to leave', departure({ leave_at_ms: 1e300 }).right === '' && departure({ leave_at_ms: T(22, 33) }).right === '22:33 出门');
  check('and so is a stage whose time none can hold', tidy(it('s1', 'sleep', T(1, 0), T(8, 0), { stages: [{ stage: 'deep', start_ms: -1e300, end_ms: 1e300 }, { stage: 'core', start_ms: T(1, 0), end_ms: T(2, 0) }] })).stages.map(x => x.stage).join() === 'core');
  check('the day\'s own bounds, now and the sun\'s times are held to the same: bounds fail the day with a line, now falls back to the start, the sun\'s time is left out',
    huge.every(v => { try { buildDay({ ...makeDay(), start_ms: v }, NOW, zh); return false; } catch (e) { return /no usable bounds/.test(String(e.message)); } }) && tidyReply({ ...makeDay(), now_ms: 1e300 }).now_ms === T(0)
    && buildDay(makeDay({ weather: { condition: 'clear', high: 14, low: 8, sunrise_ms: 1e300, sunset_ms: T(18, 5) } }), NOW, zh).weather.sun === '日落 18:05');
  for (const name of ['constructor', '__proto__', 'toString', 'hasOwnProperty', 'valueOf']) {
    const night = it('s2', 'sleep', T(1, 0), T(8, 0), { minutes: 420, stages: [{ stage: name, start_ms: T(1, 0), end_ms: T(8, 0) }] }), d = buildDay(small([night, it('s3', 'stay', T(8, 0), null, { place: 'X' })]), NOW, zh);
    const item = d.items.find(x => x.id === 's2'), piece = buildBar(d, zh).segs.find(x => x.row === 's2');
    check(`a stage called ${name} is the stage nobody told: unspecified, with a look, on the bar and in the totals`, stageSlices(item, T(1, 0), T(8, 0)).every(x => x.stage === 'unspecified') && piece.shade.length > 0 && piece.shade.every(x => Array.isArray(x.rgb) && x.a === stageLook('unspecified').a)
      && stageLook(name) === stageLook('unspecified') && Object.keys(stageMinutes(item)).join() === 'unspecified');
  }
  check('a weather called constructor is no weather: the temperatures alone, or nothing', weatherOf({ condition: 'constructor', high: 14, low: 8, sunrise_ms: null, sunset_ms: null }, zh)?.sky === null && weatherOf({ condition: 'constructor', high: null, low: null, sunrise_ms: null, sunset_ms: null }, zh) === null);
  check('and a source called constructor is named as it is told, in the line for what has not been read', buildDay(makeDay({ missing: ['mac', 'constructor'] }), NOW, zh).foot === '还没读到：Mac 的时间 · constructor');
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
  check('at 13:22 the payment (2 min away) and the photo (8 min away) are what else: the payment as counterparty and amount, plain when it is money out', p.stretch.name === 'A&W' && lines(p).join() === 'payment:A&W · $12.40|13:20,picture:汉堡和薯条|13:30');
  p = at(20, 15);
  check('at 20:15 the work block and the Claude chat, in that order; the stay is 18:50 起, 3 时', lines(p).join() === 'work:Jarvis · Ghostty|活跃 1 时 55 分,claude_chat:Claude · The bar for the day|14 条' && p.stretch.range === '18:50 起' && p.stretch.length === '3 时');
  p = at(16, 50);
  check('at 16:50 the call, and the stay is one of the day\'s stars with her words', lines(p).join() === 'call:通话 · FaceTime|18 分' && p.star.label === '下午在 UVic' && p.stretch.name === 'UVic');
  p = at(22, 35);
  check('at 22:35, still to come, the stretch is tonight\'s event and the bus is what else', p.stretch.name === '小组讨论' && p.stretch.range === '22:30–23:30' && lines(p).join() === 'departure:27 · Shelbourne at Pear|22:40 发车' && p.row === 'event-1');
  p = at(22, 10);
  check('at 22:10 nothing is recorded yet, and the reminder 10 min back is what else', p.stretch.name === '还没到' && lines(p).join() === 'reminder:把垃圾拿出去|22:00' && p.row === null);
  const gap = (() => { const d = buildDay(small([it('a', 'stay', T(9, 0), T(11, 0), { place: 'X' }), it('b', 'stay', T(12, 0), T(13, 0), { place: 'Y' })]), NOW, zh); return peek(d, buildBar(d, zh), T(11, 30), zh); })();
  check('between two stays: nothing recorded, from where the one ended to where the next began', gap.stretch.name === '没有记录' && gap.stretch.range === '11:00–12:00' && gap.stretch.length === '1 时' && gap.row === null);
  check('the row a click goes to at a minute: the piece there, else the next row; the morning sleep at 08:30; the piece a pointer is on: none in a gap', [rowAt(rm, rbar, T(14, 20)), rowAt(rm, rbar, T(15, 0)), rowAt(rm, rbar, T(8, 30)), rowAt(rm, rbar, T(22, 15))].join() === 'stay-home-2,ride-1,sleep-1,event-1' && pieceAt(rbar, T(22, 15)) === null && pieceAt(rbar, T(14, 20)) === 'stay-home-2' && pieceAt(rbar, T(22, 35)) === 'event-1');
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
  check('the imports and the Claude letter are plain rows: the payment (who, what, through which app, the amount in ink), a chat, the letter', (r => r.detail === 'Teen Burger combo · 支付宝' && r.right === '$12.40' && r.value)(rows2.find(r => r.title === 'A&W' && r.as === 'row'))
    && rows2.some(r => r.title === 'The bar for the day' && r.detail === '和 Claude' && r.right === '14 条') && rows2.some(r => r.title === 'Claude 日报' && r.detail === '2 个对话 · The bar for the day · A tool exam'));
  check('a row says what it is for a lens: work is work, a ride is a way, a stay header is none', rm.blocks.length > 0 && flat(rm.blocks).filter(r => r.as === 'row').every(r => r.kind && (r.lens === null) === !['sleep', 'move', 'ride', 'work', 'call', 'talk', 'payment', 'picture'].includes(r.kind)));
}

// ---------- what he was doing, in node (ADR 0302) ----------
{
  const span = (category, title, basis = 'observed') => doingOf(sp('x', 900, 1000, { category, title, basis })), say = (category, title, basis, t) => doingWords(span(category, title, basis), t);
  check('a span without a title has no words: the title missing, empty or blank is none, in every category and basis', ['work', 'commute', 'exercise', 'sleep', 'home', 'unknown'].every(c => ['observed', 'inferred', 'stated'].every(b => [null, undefined, '', '  \n'].every(title => say(c, title, b, zh) === null))));
  check('work the program cut is 做 <project> / On <project>', say('work', 'Jarvis', 'observed', zh) === '做 Jarvis' && say('work', 'Jarvis', 'observed', en) === 'On Jarvis');
  check('a ride is by its route as the pane words it: 27 路 / Bus 27', say('commute', '27', 'observed', zh) === '27 路' && say('commute', '27', 'observed', en) === 'Bus 27' && say('commute', '27 → 14', 'observed', zh) === '27→14 路');
  check('a call is <app> 通话 / Call · <app>: the program titles it with the app and gives it no category of its own', say('unknown', 'FaceTime', 'observed', zh) === 'FaceTime 通话' && say('unknown', 'FaceTime', 'observed', en) === 'Call · FaceTime');
  check('a workout is its activity as the pane words it (跑步 / Running), else the title as it came', say('exercise', 'running', 'observed', zh) === '跑步' && say('exercise', 'Running', 'observed', en) === 'Running' && say('exercise', 'cycling', 'observed', zh) === '骑车' && say('exercise', 'pickleball', 'observed', zh) === 'pickleball');
  check('sleep is 睡觉 / Asleep', say('sleep', 'sleep', 'observed', zh) === '睡觉' && say('sleep', 'sleep', 'observed', en) === 'Asleep');
  check('any other category the program cut says its title as it is', say('home', 'Home', 'observed', zh) === 'Home' && say('study', 'COMP 101', 'observed', en) === 'COMP 101' && say('meal', ' 吃饭 ', 'observed', zh) === '吃饭');
  check('what a model wrote or he said is a phrase already: shown as given, whatever the category', ['work', 'commute', 'unknown', 'exercise', 'sleep', 'meal'].every(c => say(c, '考驾照笔试', 'inferred', zh) === '考驾照笔试' && say(c, '下午打羽毛球', 'stated', en) === '下午打羽毛球'));
  check('a basis it does not know is shown as given too', say('work', 'x', '', zh) === 'x' && say('work', 'x', 'guessed', zh) === 'x' && doingOf(sp('x', 900, 1000, { basis: undefined, title: 'x' })).basis === '');
  check('the doing lane is read field by field: only its own lane, not a planned one, an id and times are needed, a title that is not text is none, no category is unknown',
    doingOf(sp('a', 900, 1000, { lane: 'place' })) === null && doingOf(sp('a', 900, 1000, { kind: 'planned' })) === null && doingOf({ ...sp('a', 900, 1000), id: 7 }) === null && doingOf({ ...sp('a', 900, 1000), id: '' }) === null
    && doingOf({ ...sp('a', 900, 1000), start_ms: null }) === null && doingOf({ ...sp('a', 900, 1000), end_ms: 'x' }) === null && doingOf({ ...sp('a', 900, 1000), start_ms: 1e300 }) === null && doingOf(null) === null && doingOf('x') === null
    && doingOf(sp('a', 900, 1000, { title: 7 })).title === null && doingOf(sp('a', 900, 1000, { category: undefined })).category === 'unknown' && doingOf(sp('a', 900, null)).end_ms === null);
  const tidied = tidyReply(doingDay()).spans;
  check('the day keeps its doing spans (not the planned one, not another lane) and drops the rest of what a span carries; an older brain has none', tidied.map(x => x.id).join() === 'd-sleep,d-work,d-snack,d-home,d-ride,d-study,d-eve'
    && tidied.every(x => Object.keys(x).join() === 'id,lane,start_ms,end_ms,title,category,basis') && tidyReply(makeDay()).spans.length === 0 && tidyReply({ ...makeDay(), spans: 'x' }).spans.length === 0 && tidyReply({ ...makeDay(), spans: [null, 7, { lane: 'doing' }] }).spans.length === 0);
  check('what began last is what he is doing, if two spans cover a minute; one with no words does not count', doingAt(tidied, T(10, 0), zh).words === '做 Jarvis' && doingAt(tidied, T(14, 20), zh) === null && doingAt(tidied, T(11, 20), zh) === null
    && doingAt([...tidied, doingOf(sp('later', 940, 1100, { title: '回邮件', basis: 'inferred' }))], T(10, 0), zh).words === '回邮件' && doingAt([...tidied, doingOf(sp('later', 940, 1100, { title: null }))], T(10, 0), zh).words === '做 Jarvis' && doingAt(undefined, T(10, 0), zh) === null);

  // the wing
  const dd = doingDay(), w = (at, t = zh, d = dd) => JSON.stringify(wingText(d, at, t));
  check('the wing says what he is doing inside a work span, and since when: 做 Jarvis · 0:30 (On Jarvis in English)', w(T(10, 0)) === '{"head":"做 Jarvis","tail":"0:30"}' && wingText(dd, T(10, 0), en).head === 'On Jarvis');
  check('a span with no title leaves the place: 家 for a home span with nothing to call it', w(T(14, 20)) === '{"head":"家","tail":"0:15"}');
  check('his own answer leads, as he said it', w(T(16, 0)) === '{"head":"在图书馆写作业","tail":"0:41"}' && w(T(13, 20)) === '{"head":"吃午饭","tail":"0:05"}');
  check('a ride says the route, and the minutes since it began, as a move\'s (7 分)', w(T(14, 57)) === '{"head":"27 路","tail":"7 分"}' && wingText(dd, T(14, 57), en).head === 'Bus 27');
  check('the span that goes on covers now: 做 Jarvis · 2:15, and neither the planned span over now nor the span of another lane is heard', w(NOW) === '{"head":"做 Jarvis","tail":"2:15"}');
  check('past a span\'s end (to the minute) the place is back, and a ride or a drive with no span is worded as it was: 家 · 2:17, 27 路 · 15 分, 开车 · 40 分',
    w(T(11, 20)) === '{"head":"家","tail":"2:15"}' && w(T(11, 22)) === '{"head":"家","tail":"2:17"}' && w(T(18, 30)) === '{"head":"27 路","tail":"15 分"}' && w(T(12, 5)) === '{"head":"开车","tail":"40 分"}');
  check('a day with no spans, with spans that are not a list or are not spans, or with none that has words, says what it always did', [undefined, [], 'x', 7, [null, 7, 'y', { lane: 'doing' }], [sp('n', 0, null)], [sp('n', 0, null, { category: 'home' })]].every(spans => wingText(makeDay({ spans }), NOW, zh).head === '家' && wingText(makeDay({ spans }), NOW, zh).tail === '3:00'));

  // the bar's pieces
  const dm = buildDay(doingDay(), NOW, zh), dbar = buildBar(dm, zh), seg = Object.fromEntries(dbar.segs.map(x => [x.row, x]));
  check('a stay is named by the span that covers at least half of it: 做 Jarvis over the 140 min at home, of which the span has 110; the place stays its colour and icon',
    seg['stay-home-1'].name === '做 Jarvis' && seg['stay-home-1'].short === '做 Jarvis' && seg['stay-home-1'].place === '家' && seg['stay-home-1'].rgb.join() === '255,168,150' && seg['stay-home-1'].icon === 'home');
  check('and by his answer over the school (131 of 176 min), the school still blue with its icon', seg['stay-uvic'].name === '在图书馆写作业' && seg['stay-uvic'].place === 'UVic' && seg['stay-uvic'].rgb.join() === '100,200,255' && seg['stay-uvic'].icon === 'school');
  check('by a span that goes on, as far as now: the evening at home is 做 Jarvis (135 of 180 min)', seg['stay-home-3'].name === '做 Jarvis' && seg['stay-home-3'].place === '家');
  check('exactly half is enough: 吃午饭 covers 20 of the 40 min at A&W, and the haze stays the haze', seg['stay-aw'].name === '吃午饭' && seg['stay-aw'].short === '吃午饭' && seg['stay-aw'].place === 'A&W' && seg['stay-aw'].rgb.join() === '196,204,238');
  check('a span with no title leaves the place\'s name', seg['stay-home-2'].name === '家' && seg['stay-home-2'].short === '家' && seg['stay-home-2'].place === '家');
  const plain = buildBar(buildDay(makeDay(), NOW, zh), zh), bare = x => x.segs.map(s => JSON.stringify(s));
  check('trips and sleep keep their own names, and a stay\'s place is what the bar said before', dbar.segs.every((x, i) => x.kind === 'stay' ? x.place === plain.segs[i].name : JSON.stringify(x) === JSON.stringify({ ...plain.segs[i], place: plain.segs[i].name })) && dbar.segs.filter(x => x.kind !== 'stay').length > 3);
  check('a day with no spans, with an empty list, or with spans none of which has words, is the bar it was', bare(buildBar(buildDay(makeDay({ spans: [] }), NOW, zh), zh)).join() === bare(plain).join()
    && bare(buildBar(buildDay(makeDay({ spans: [sp('a', 0, null, { category: 'home' }), sp('b', 900, 1100, { title: '  ' })] }), NOW, zh), zh)).join() === bare(plain).join());
  const stay = (spans, from = 900, to = 1000) => buildBar(buildDay(small([ev('s', 'stay', from, to, { place: 'X, Victoria' })], { spans }), NOW, zh), zh).segs.find(x => x.kind === 'stay');
  const word = (a, b, over = {}) => sp(`w${a}-${b}`, a, b, { title: `${a}-${b}`, basis: 'stated', ...over });
  check('half is the line: 30 of 60 min names the stay, 29 does not; two spans that are 20 min each do not, though together they cover 40', stay([word(900, 930)]).name === '900-930' && stay([word(900, 929)]).name === 'X' && stay([word(900, 920), word(920, 940)]).name === 'X');
  check('of two that each cover half, the one covering most wins; a span with no words, however much it covers, names nothing and takes nothing from one that has', stay([word(900, 935), word(920, 1000)]).name === '920-1000' && stay([word(900, 1000, { title: null }), word(910, 920)]).name === 'X' && stay([word(900, 935, { title: null }), word(900, 931)]).name === '900-931');
  check('a span runs past the stay\'s edges and still names it for what lies inside; one that ends before it began names nothing', stay([word(800, 1100)]).name === '800-1100' && stay([word(1100, 1200)]).name === 'X' && stay([word(1000, 1100)]).name === 'X');
  check('a stay still going is measured to now: 50 min since 21:00, a span from 21:20 that goes on covers 30 of them', buildBar(buildDay(small([ev('s', 'stay', 2100, null, { place: 'X' })], { spans: [sp('o', 2120, null, { title: '看剧', basis: 'inferred' })] }), NOW, zh), zh).segs[0].name === '看剧');
  const rng = summarize(dm, dbar, T(14, 0), T(18, 0), zh), rng0 = summarize(buildDay(makeDay(), NOW, zh), plain, T(14, 0), T(18, 0), zh);
  check('a range\'s card still counts places, not what he was doing: 家 and UVic with their lengths, as without spans', JSON.stringify(rng.places) === JSON.stringify(rng0.places) && rng.places.map(x => x.name).join() === 'UVic,家');

  // the hover card
  const hp = (at, t = zh, m = dm, b = dbar) => peek(m, b, at, t), tx = x => `${x.stretch.name}|${x.stretch.place ?? ''}|${x.stretch.range}|${x.stretch.length}`;
  check('hovering the work: the title is what he was doing with its own time, the place its second line', tx(hp(T(10, 0))) === '做 Jarvis|家|09:30–11:20|1 时 50 分' && hp(T(10, 0)).row === 'stay-home-1');
  check('in English', tx(hp(T(10, 0), en, buildDay(doingDay(), NOW, en), buildBar(buildDay(doingDay(), NOW, en), en))).startsWith('On Jarvis|家|'));
  check('a minute of the same stay outside the span is the place as it was, with no second line (11:22 is after the work; 14:20 has a span with no title)', tx(hp(T(11, 22))) === '家||09:05–11:25|2 时 20 分' && tx(hp(T(14, 20))) === '家||14:05–14:50|45 分');
  check('hovering where his answer is, and where it is not on the same stay: 吃午饭 over A&W at 13:20, A&W at 13:40', tx(hp(T(13, 20))) === '吃午饭|A&W|13:15–13:35|20 分' && tx(hp(T(13, 40))) === 'A&W||13:15–13:55|40 分');
  check('a ride says its route once: the span\'s words are the piece\'s name, so there is no second line; the sleep likewise', tx(hp(T(14, 57))) === '27 路||14:50–15:19|29 分' && hp(T(8, 30)).stretch.name === '睡觉' && hp(T(8, 30)).stretch.place === undefined);
  const tripDay = buildDay(small([ev('r', 'ride', 1000, 1030, { route: '27' })], { spans: [sp('k', 1000, 1030, { title: '去考驾照', basis: 'stated' })] }), NOW, zh);
  const holeDay = (spans) => { const m = buildDay(small([ev('a', 'stay', 900, 1000, { place: 'X' }), ev('b', 'stay', 1100, 1200, { place: 'Y' })], { spans }), NOW, zh); return tx(peek(m, buildBar(m, zh), T(10, 30), zh)); };
  check('where nothing was recorded but he said what he did, the card says that with its own time; else 没有记录 from piece to piece', holeDay([sp('g', 1015, 1045, { title: '吃饭', basis: 'stated' })]) === '吃饭||10:15–10:45|30 分' && holeDay([]) === '没有记录||10:00–11:00|1 时');
  check('a way he went with his words over it leads with them and keeps its own name under: 去考驾照 over 27 路', tx(peek(tripDay, buildBar(tripDay, zh), T(10, 10), zh)).startsWith('去考驾照|27 路|'));
  check('the card\'s id changes with the span, so its words swap as they do for another stretch, and it is the piece\'s when there is no span', hp(T(10, 0)).id.includes('~d-work') && hp(T(13, 20)).id !== hp(T(13, 40)).id && !hp(T(11, 22)).id.includes('~') && hp(T(10, 0)).id === hp(T(10, 1)).id);
  const peeks = [T(10, 0), T(14, 20), T(16, 50), T(20, 15), T(22, 35), T(22, 10), T(8, 30)].map(at => peek(buildDay(makeDay(), NOW, zh), plain, at, zh)), peeks0 = [T(10, 0), T(14, 20), T(16, 50), T(20, 15), T(22, 35), T(22, 10), T(8, 30)].map(at => peek(buildDay(makeDay({ spans: [] }), NOW, zh), buildBar(buildDay(makeDay({ spans: [] }), NOW, zh), zh), at, zh));
  check('a day with no spans has the card it always had: no second line, the piece\'s own id', JSON.stringify(peeks) === JSON.stringify(peeks0) && peeks.every(x => x.stretch.place === undefined && !x.id.includes('~')));
}

// ---------- the list's anatomy, in node ----------
{
  const m = buildDay(day, NOW, zh), rs = flat(m.blocks), r = title => rs.find(x => x.title === title);
  check('what waits on him is counted in list order: the reminder that rang and was not answered, the event read from mail and not confirmed', m.waiting.join() === 'reminder-2,event-1' && r('给银行打电话').wait && r('小组讨论').wait && !r('把垃圾拿出去').wait && !r('私事').wait && r('给银行打电话').detail === '响过了');
  const ended = buildDay(makeDay({ items: [it('x', 'event', T(9, 0), T(10, 0), { title: 'a', source: 'mail', confirmed: false }), it('y', 'reminder', T(8, 0), null, { text: 'b', fired: true, acknowledged: true }, 'her'), it('z', 'reminder', T(23, 0), null, { text: 'c', fired: false, acknowledged: false }, 'her')] }), NOW, zh);
  check('an event that is over does not wait; a reminder that was answered says so; one not yet due says that', ended.waiting.length === 0 && flat(ended.blocks).find(x => x.title === 'b').detail === '你知道了' && flat(ended.blocks).find(x => x.title === 'c').detail === '还没到点');
  check('what is still to come within two hours says how long until it: 还有 10 分, 还有 40 分, 还有 1 时 50 分; the bus keeps its words', r('把垃圾拿出去').right === '还有 10 分' && r('小组讨论').right === '还有 40 分' && r('私事').right === '还有 1 时 50 分'
    && r('27 · Shelbourne at Pear').right === '22:33 出门' && r('27 · Shelbourne at Pear').value && r('27 · Shelbourne at Pear').detail === '去 UVic');
  check('beyond two hours a row says its own length, and what is over has no countdown', flat(buildDay(day, T(19, 0), zh).blocks).find(x => x.title === '私事').right === '15 分' && !/还有/.test(rs.filter(x => !x.future).map(x => x.right).join()));
  check('a payment is plain when it is money out and has a + when it is money in', flat(buildDay(small([it('p', 'payment', T(10, 0), T(10, 0), { source: 'alipay', amount: '12.40', currency: 'CAD', direction: 'out', counterparty: 'X' }), it('q', 'payment', T(11, 0), T(11, 0), { source: 'alipay', amount: '50.00', currency: 'CAD', direction: 'in', counterparty: 'Y' })]), NOW, zh).blocks).filter(x => x.as === 'row').map(x => x.right).join() === '$12.40,+$50.00');
    check('only what the brain\'s about takes can be handed to her: a kind it knows (a ride, a bus, a payment, a Claude chat and day, a run and a session among them) with an id of its shape (1 to 256 characters of its set, an @ among them: a commitment\'s)',
    askable('stay', 'stay-home-1') && askable('talk', 'talk-1') && askable('picture', 'a'.repeat(32)) && askable('event', 'cal|2026-10-19:1/x=y+z.w') && askable('todo', 'T_1-2')
    && askable('ride', 'ride-1') && askable('departure', 'departure-1') && askable('payment', 'pay-1') && askable('claude_chat', 'chat-1') && askable('claude_day', 'claude-day-1') && askable('event', `commitment-${'ab'.repeat(16)}@2026-10-19`) && askable('event', 'cal-1@example.com')
    && askable('workout', 'workout-1') && askable('code', 'code-1') && !askable('weather', 'w-1') && !askable('event', 'a b@c') && !askable('event', 'a#b') && !askable('talk', '') && !askable('talk', 'x'.repeat(257)) && askable('talk', 'x'.repeat(256)) && !askable('stay', 'a b'));
  check('a row that began the day before says so beside its time', (x => x.at === '23:20' && x.ago === '昨')(flat(buildDay(small([it('sl', 'sleep', T(23, 20, 18), T(7, 0), { minutes: 400 })]), NOW, zh).blocks).find(y => y.as === 'row')));

  const fm = buildDay(fragmentedDay(), NOW, zh), fr = flat(fm.blocks), stops = fr.filter(x => x.icon === 'dot');
  check('four unnamed stays of a few minutes between two ways are stops on the trip (停了 5 分, the phone\'s place beside), and only they', stops.map(x => `${x.title}|${x.detail}`).join() === '停了 5 分|Shelbourne at Pear,停了 6 分|McPherson Library,停了 5 分|Finnerty Rd,停了 6 分|Parking Lot' && stops.every(x => x.connector && x.right === ''));
  check('every other stay is a place with a header: the 15 stays less the four stops; a kept place always, an unnamed one for more than 15 minutes, a minute or two beside a place',
    fr.filter(x => x.as === 'stay').map(x => x.title).join() === '家,UVic,University of Victoria,Student Union Building,UVic,University of Victoria,Mayfair Mall,Mayfair Mall,健身房,Gym,家');
  const trip = fm.blocks.filter(b => b.type === 'row' && (b.row.connector || b.row.onWay)).map(b => b.row.key);
  check('what happened on the way belongs to the trip: the talk on the 14 is on it; a talk inside a stay is not', fm.blocks.find(b => b.type === 'row' && b.row.key === 'f-talk-1').row.onWay && !trip.includes('f-talk-2') && trip.slice(0, 5).join() === 'f-w1,f-s2,f-r1,f-talk-1,f-w2');
  check('the highlight\'s words are the row\'s title and the row says it mattered; the label that says nothing leaves the title', (x => x.title === '去 Mayfair' && x.star && x.detail === '26 路 · Finnerty at Ring Rd → Mayfair Mall')(fr.find(y => y.key === 'f-r2')) && fr.find(y => y.key === 'f-talk-3').title === '和她说话' && fr.find(y => y.key === 'f-talk-3').star);
  const stopTalk = flat(buildDay(small([it('a', 'move', T(10, 0), T(10, 10), { activity: 'walking' }), it('s', 'stay', T(10, 10), T(10, 16), { place: 'Bus stop, X' }), it('t', 'talk', T(10, 12), T(10, 14), { turns: 2 }, 'her'), it('b', 'ride', T(10, 16), T(10, 40), { route: '14' })]), NOW, zh).blocks);
  check('what happened during a stop stays in the trip with it, after it', stopTalk.map(x => x.title).join() === '步行,停了 6 分,和她说话,14 路,现在 21:50');
  const tm = buildDay(talkDay(), NOW, zh);
  check('a day of talks alone: seven rows, no stay, a sentence for the missing Mac', flat(tm.blocks).filter(x => x.title === '和她说话').length === 7 && !flat(tm.blocks).some(x => x.as === 'stay') && tm.foot === '还没读到：Mac 的时间');
}

// ---------- the new data, in node (spec-r3 section 11) ----------
{
  const sky = (condition, high = 14, low = 8, t = zh) => weatherOf({ condition, high, low, sunrise_ms: null, sunset_ms: null }, t);
  const names = [['clear', '晴', 'sun'], ['mostlyClear', '少云', 'cloudSun'], ['partlyCloudy', '多云', 'cloudSun'], ['MostlyCloudy', '多云', 'cloud'], ['cloudy', '阴', 'cloud'], ['drizzle', '小雨', 'rain'], ['sunShowers', '小雨', 'rain'],
    ['rain', '雨', 'rain'], ['heavyRain', '雨', 'rain'], ['isolatedThunderstorms', '雷雨', 'storm'], ['snow', '雪', 'snow'], ['wintry_mix', '雪', 'snow'], ['foggy', '雾', 'fog'], ['haze', '雾', 'fog'], ['windy', '风', 'wind'], ['breezy', '风', 'wind']];
  check('the weather is a glyph and a word for each WeatherKit name, whatever its case or separators: 晴 少云 多云 阴 小雨 雨 雷雨 雪 雾 风', names.every(([name, word, glyph]) => { const w = sky(name); return w.word === word && w.sky === glyph && w.temps === '14° / 8°'; }));
  check('the temperatures are the high and the low, rounded; a name it does not know shows only them, and nothing known shows nothing', sky('partlyCloudy', 14.2, 7.6).temps === '14° / 8°' && sky('frigid', -3, -9).temps === '-3° / -9°' && sky('frigid', -3, -9).sky === null && sky('frigid', -3, -9).word === ''
    && sky('clear', 14, null).temps === '14°' && sky('clear', -0.4, null).temps === '0°' && sky('clear', null, null).temps === '' && sky('clear', null, null).word === '晴' && sky('frigid', null, null) === null && weatherOf(null, zh) === null && weatherOf(undefined, zh) === null);
  check('in English the words are English', sky('partlyCloudy', 14, 8, en).word === 'Partly cloudy' && sky('thunderstorms', 14, 8, en).word === 'Thunderstorms' && sky('cloudy', 14, 8, en).word === 'Overcast');
  const sunny = (rise, set, t = zh) => weatherOf({ condition: 'clear', high: 14, low: 8, sunrise_ms: rise, sunset_ms: set }, t, TZ).sun;
  check('a rest on the weather says when the sun comes up and goes down, in the day\'s own zone: 日出 07:12 · 日落 18:21; one time alone is one; none is nothing; in English too',
    sunny(T(7, 12), T(18, 21)) === '日出 07:12 · 日落 18:21' && sunny(T(7, 12), null) === '日出 07:12' && sunny(null, T(18, 21)) === '日落 18:21' && sunny(null, null) === '' && sunny(T(7, 12), T(18, 21), en) === 'Sunrise 07:12 · Sunset 18:21'
    && buildDay(fullDay({ weather: { condition: 'clear', high: 14, low: 8, sunrise_ms: T(7, 12), sunset_ms: T(18, 21) } }), NOW, zh).weather.sun === '日出 07:12 · 日落 18:21');
  const lost = missing => buildDay(makeDay({ missing }), NOW, zh).foot;
  check('what has not been read is worded as the phone words it, several joined with · , and in English the same',
    lost(['mac']) === '还没读到：Mac 的时间' && lost(['phone', 'reminders', 'mac', 'talks', 'calendar', 'departures']) === '还没读到：手机的记录 · 提醒 · Mac 的时间 · 对话 · 日历 · 公交' && lost([]) === ''
    && buildDay(makeDay({ missing: ['mac', 'calendar'] }), NOW, en).foot === 'Not read yet: Mac time · The calendar');

  const fm = buildDay(fullDay(), NOW, zh), fb = buildBar(fm, zh), fr = flat(fm.blocks), fk = key => fr.find(r => r.key === key);
  check('the body\'s numbers are quiet values in order, steps with its thousands comma; a null value is left out, and so is a day with none', fm.vitals.map(v => `${v.before}|${v.value}|${v.after}`).join() === '|8,240|步,静息|58|,HRV|42|'
    && buildDay(fullDay({ summary: { steps: null, resting_hr: 61.4, hrv: null } }), NOW, zh).vitals.map(v => v.value).join() === '61' && buildDay(fullDay({ summary: { steps: null, resting_hr: null, hrv: null } }), NOW, zh).vitals.length === 0
    && buildDay(fullDay({ summary: null }), NOW, zh).vitals.length === 0 && buildDay(fullDay({ summary: 'x', weather: 3 }), NOW, zh).vitals.length === 0 && buildDay(fullDay({ weather: 3 }), NOW, zh).weather === null
    && buildDay(fullDay(), NOW, en).vitals.map(v => `${v.before}${v.after}`).join() === 'steps,Resting,HRV');

  // a night with stages
  const piece = fb.segs.find(x => x.row === 'sleep-1'), item = fm.items.find(x => x.id === 'sleep-1'), night = stageSlices(item, item.start_ms, item.end_ms);
  const mins = stageMinutes(item);
  check('a night\'s stages: 70 min of deep sleep, 100 of REM, 270 of core, 30 awake; the phone\'s in_bed that runs past both ends is cut to the night and never counts', Math.round(mins.deep) === 70 && Math.round(mins.rem) === 100 && Math.round(mins.core) === 270 && Math.round(mins.awake) === 30 && !mins.in_bed && Object.values(mins).reduce((x, y) => x + y, 0) === 470);
  const wide = stageSlices(item, T(23, 0, 18), T(7, 10));
  check('where stages overlap the watch\'s wins over in_bed; in_bed shows only where nothing else was told; the slices run on without a gap or an overlap', wide.length === 16 && wide[0].stage === 'in_bed' && wide.at(-1).stage === 'in_bed' && wide.every((x, i) => i === 0 || x.s === wide[i - 1].e) && wide[0].s === T(23, 0, 18) && wide.at(-1).e === T(7, 10) && wide.slice(1, -1).map(x => x.stage).join() === 'core,deep,core,awake,rem,core,deep,rem,core,awake,core,rem,core,awake');
  check('a stretch nobody told, a stage it does not know and a night with no stages at all are all unspecified', stageSlices({ ...item, stages: [{ stage: 'asleep', start_ms: T(1, 0), end_ms: T(2, 0) }] }, T(0, 30), T(2, 30)).map(x => `${x.stage}:${(x.e - x.s) / 60_000}`).join() === 'unspecified:120'
    && stageSlices({ ...item, stages: [{ stage: 'deep', start_ms: T(1, 0), end_ms: T(2, 0) }] }, T(0, 30), T(2, 30)).map(x => `${x.stage}:${(x.e - x.s) / 60_000}`).join() === 'unspecified:30,deep:60,unspecified:30' && stageSlices({ ...item, stages: undefined }, T(1, 0), T(2, 0)).map(x => x.stage).join() === 'unspecified');
  const sh = piece.shade.map(x => `${x.rgb.join(' ')}@${x.a}:${x.f0.toFixed(4)}-${x.f1.toFixed(4)}`).join();
  check('the bar\'s sleep piece (06:00 to 07:00 of the axis) is shaded by stage under its label: REM the lighter hue at .8, core at .78, awake at .25, in the share each took', clock(fb.t0, TZ) === '06:00' && sh === '150 160 235@0.8:0.0000-0.4167,112 120 205@0.78:0.4167-0.8333,112 120 205@0.25:0.8333-1.0000', sh);
  check('deep sleep is the sleep hue at 1 and unspecified at .6; a night with no stages keeps its one fill', [['deep', 1], ['unspecified', .6]].every(([stage, a]) => { const d = buildBar(buildDay(fullDay({ items: [it('sleep-1', 'sleep', T(5, 0), T(7, 0), { minutes: 120, stages: [{ stage, start_ms: T(5, 0), end_ms: T(7, 0) }] }), it('s', 'stay', T(7, 0), null, { place: 'X', kept_place: home })] }), NOW, zh), zh); return d.segs[0].shade.length === 1 && d.segs[0].shade[0].a === a && d.segs[0].shade[0].rgb.join() === '112,120,205'; })
    && buildBar(buildDay(richDay(), NOW, zh), zh).segs.find(x => x.kind === 'sleep').shade === undefined);
  const note = t => peek(fm, buildBar(fm, t), T(6, 30), t).stretch;
  check('on the sleep the hover card adds its deep sleep and REM: 深睡 1:10 · REM 1:40 (Deep 1:10 · REM 1:40); the night without stages adds nothing', note(zh).name === '睡觉' && note(zh).note === '深睡 1:10 · REM 1:40' && note(en).note === 'Deep 1:10 · REM 1:40' && note(zh).range === '昨天 23:10–07:00' && note(zh).length === '7 时 20 分'
    && peek(buildDay(richDay(), NOW, zh), buildBar(buildDay(richDay(), NOW, zh), zh), T(8, 30), zh).stretch.note === undefined);
  const only1 = (stage, mn) => { const d = buildDay(fullDay({ items: [it('sleep-1', 'sleep', T(5, 0), T(7, 0), { minutes: 120, stages: [{ stage, start_ms: T(5, 0), end_ms: T(5, 0) + mn * 60_000 }] }), it('s', 'stay', T(7, 0), null, { place: 'X', kept_place: home })] }), NOW, zh); return peek(d, buildBar(d, zh), T(6, 30), zh).stretch.note; };
  check('and says only the one it had: REM alone, deep alone, neither when it was core and awake', only1('rem', 90) === 'REM 1:30' && only1('deep', 45) === '深睡 0:45' && only1('core', 90) === undefined);

  // a night two sources told: the watch's stages and a second source's overlap. Every minute takes ONE stage (awake over REM over deep over core
  // over unspecified over in_bed) and the totals, the hover card, the hypnogram and the bar all read that one timeline, so nothing counts twice.
  {
    const ORDER = ['awake', 'rem', 'deep', 'core', 'unspecified', 'in_bed'], N0 = T(23, 0, 18), M = n => N0 + n * 60_000;
    // minutes from 23:00: the watch (core 23:00, deep 01:00, core 02:30, REM 04:00, core 05:00, awake 06:30, then off the wrist) and a second source over it
    const watch = [['core', 0, 120], ['deep', 120, 210], ['core', 210, 300], ['rem', 300, 360], ['core', 360, 450], ['awake', 450, 460]];
    const other = [['in_bed', -10, 490], ['deep', 90, 240], ['awake', 180, 190], ['rem', 270, 330], ['unspecified', 450, 470]];
    const told = [...watch, ...other].sort((x, y) => x[1] - y[1] || x[2] - y[2]);
    const stages = told.map(([stage, from, to]) => ({ stage, start_ms: M(from), end_ms: M(to) }));
    // the oracle: one minute at a time, the first stage in the order that covers it
    const oracle = n => ORDER.find(s => told.some(([stage, from, to]) => stage === s && from <= n && n < to)) ?? 'unspecified';
    const per = {}, runs = [];
    for (let n = 0; n < 480; n++) { const s = oracle(n); per[s] = (per[s] ?? 0) + 1; if (runs.at(-1)?.[0] === s) runs.at(-1)[2] = n + 1; else runs.push([s, n, n + 1]); }
    const sleep = it('sleep-2', 'sleep', M(0), M(480), { minutes: 460, stages });
    const d = buildDay(small([sleep, it('talk-e', 'talk', T(0, 10), T(0, 12), { turns: 1 }, 'her'), it('home', 'stay', T(7, 0), null, { place: 'X', kept_place: home })]), NOW, zh);
    const item2 = d.items.find(x => x.id === 'sleep-2'), mins2 = stageMinutes(item2), naive = {};
    for (const [stage, from, to] of told) naive[stage] = (naive[stage] ?? 0) + Math.max(0, Math.min(to, 480) - Math.max(from, 0));
    check('the fixture night does overlap: counting every stage as told would give the deep sleep twice (240 min for 140) and 1180 minutes for a night of 480', naive.deep === 240 && per.deep === 140 && Object.values(naive).reduce((x, y) => x + y, 0) === 1180, JSON.stringify({ naive, per }));
    check('a night is counted once: each stage\'s minutes are the oracle\'s, minute by minute (awake over REM over deep over core over unspecified over in_bed), and together they are the night, 480', ORDER.every(s => Math.round(mins2[s] ?? 0) === (per[s] ?? 0)) && Math.round(Object.values(mins2).reduce((x, y) => x + y, 0)) === 480 && per.deep === 140 && per.core === 210 && per.rem === 90 && per.awake === 20, JSON.stringify({ mins2, per }));
    check('the slices are that timeline in runs, with no gap and no overlap, and the same stage next to itself is one run (the deep sleep both sources told, the REM, the awake of 06:30)',
      JSON.stringify(stageSlices(item2, M(0), M(480)).map(x => [x.stage, (x.s - N0) / 60_000, (x.e - N0) / 60_000])) === JSON.stringify(runs) && runs.length === 10, JSON.stringify(runs));
    const n2 = openOf(d, 'sleep-2', zh).parts.find(x => x.type === 'night').night;
    check('the open row\'s totals read it: 深睡 2:20 · 核心 3:30 · REM 1:30 · 醒 2 次, in English too', n2.totals === '深睡 2:20 · 核心 3:30 · REM 1:30 · 醒 2 次' && openOf(buildDay(small([sleep]), NOW, en), 'sleep-2', en).parts.find(x => x.type === 'night').night.totals === 'Deep 2:20 · Core 3:30 · REM 1:30 · Awake 2×', n2.totals);
    check('and its hypnogram is those runs', JSON.stringify(n2.slices.map(x => [x.stage, (x.s - N0) / 60_000, (x.e - N0) / 60_000])) === JSON.stringify(runs) && n2.at.join() === '23:00,07:00');
    const b2 = buildBar(d, zh), p2 = b2.segs.find(x => x.row === 'sleep-2'), cover = (b2.t0 - N0) / 60_000, span2 = (p2.e - p2.s) / 60_000;
    check('the hover card adds its deep sleep and REM from it: 深睡 2:20 · REM 1:30', peek(d, b2, T(3, 0), zh).stretch.note === '深睡 2:20 · REM 1:30');
    check('the bar\'s shading reads it: one slice per run inside the piece, joined end to end from 0 to 1, each in the look of the stage that wins there',
      clock(b2.t0, TZ) === '00:00' && p2.shade.length === runs.filter(r => r[2] > cover).length && p2.shade[0].f0 === 0 && Math.abs(p2.shade.at(-1).f1 - 1) < 1e-9 && p2.shade.every((x, i) => i === 0 || Math.abs(x.f0 - p2.shade[i - 1].f1) < 1e-9)
      && p2.shade.every(x => { const look = stageLook(oracle(Math.floor(cover + (x.f0 + x.f1) / 2 * span2))); return x.rgb.join() === look.rgb.join() && x.a === look.a; }), JSON.stringify(p2.shade.map(x => `${x.a}:${x.f0.toFixed(3)}-${x.f1.toFixed(3)}`)));
  }

  // the two days the clocks change (2026-03-08 and 2026-11-01 in Vancouver): a time is the wall clock's, a minute is a real minute, and the
  // last hour, 23:00 to 24:00, is on the bar
  for (const [name, date, start, end, hours] of [['the 23-hour day', '2026-03-08', Date.UTC(2026, 2, 8, 8), Date.UTC(2026, 2, 9, 7), 23], ['the 25-hour day', '2026-11-01', Date.UTC(2026, 10, 1, 7), Date.UTC(2026, 10, 2, 8), 25]]) {
    const spring = hours === 23, U = spring ? (h, m = 0) => Date.UTC(2026, 2, 8, h, m) : (h, m = 0) => Date.UTC(2026, 10, 1, h, m), later = spring ? Date.UTC(2026, 2, 9, 6) : Date.UTC(2026, 10, 2, 7);
    const mk = (id, kind, from, to, extra = {}) => ({ id, lane: 'you', kind, start_ms: from, end_ms: to, state: 'done', ...extra }), stg = (stage, a, b) => ({ stage, start_ms: a, end_ms: b });
    // the night began at 23:30 the evening before (7 h of it on the 23-hour day, 9 on the 25-hour one); the clocks change at 02:00, inside the deep sleep
    const nightFrom = spring ? U(7, 30) : U(6, 30), nightTo = spring ? U(14, 30) : U(15, 30);
    const stagesDst = spring ? [stg('core', U(7, 30), U(9, 30)), stg('deep', U(9, 30), U(10, 30)), stg('core', U(10, 30), U(13)), stg('rem', U(13), U(14)), stg('awake', U(14), U(14, 30))]
      : [stg('core', U(6, 30), U(9, 30)), stg('deep', U(9, 30), U(11)), stg('core', U(11), U(13)), stg('rem', U(13), U(14, 30)), stg('core', U(14, 30), U(15, 30))];
    const items = [mk('sl', 'sleep', nightFrom, nightTo, { minutes: 400, stages: stagesDst }), spring ? mk('ev', 'event', U(9, 30), U(10, 30), { title: '夜班' }) : mk('ev', 'event', U(7, 45), U(9, 15), { title: '夜班' }),
      mk('early', 'stay', start, U(11), { place: 'X' }), mk('home', 'stay', U(11), later, { place: 'Y' }), mk('late', 'stay', later, end, { place: 'Library' })];
    const dm = buildDay({ date, tz: TZ, start_ms: start, end_ms: end, now_ms: end + 3_600_000, items, labels: [], missing: [], line: null, highlights: [] }, end + 3_600_000, zh), db = buildBar(dm, zh);
    const ngt = openOf(dm, 'sl', zh).parts.find(x => x.type === 'night').night, real = (spring ? 7 : 9) * 60;
    check(`on ${name} the night\'s times are the wall clock's, 23:30 to 07:30, and its minutes are real: ${real / 60} h, and the stages add up to them`,
      ngt.at.join() === '23:30,07:30' && Math.round(Object.values(stageMinutes(dm.items.find(x => x.id === 'sl'))).reduce((x, y) => x + y, 0)) === real && ngt.slices.at(-1).e - ngt.slices[0].s === real * 60_000, JSON.stringify(ngt.slices.map(x => `${x.stage}:${(x.e - x.s) / 60_000}`)));
    check(`${name}: the totals are real minutes (${spring ? '深睡 1:00 · 核心 4:30 · REM 1:00 · 醒 1 次' : '深睡 1:30 · 核心 6:00 · REM 1:30'})`, ngt.totals === (spring ? '深睡 1:00 · 核心 4:30 · REM 1:00 · 醒 1 次' : '深睡 1:30 · 核心 6:00 · REM 1:30'), ngt.totals);
    const evText = openOf(dm, 'ev', zh).parts[0].lines[0].text;
    check(`${name}: an event over the change says ${spring ? '01:30–03:30 for 1 时' : '00:45–01:15 for 1 时 30 分'}, the minutes it ran`, evText.startsWith(spring ? '01:30–03:30 · 1 时 ·' : '00:45–01:15 · 1 时 30 分 ·'), evText);
    check(`${name}: a stay over the change is as long as the minutes it ran, not the clock's difference (${spring ? '00:00–04:00 is 3 时' : '00:00–03:00 is 4 时'})`, dm.heads.get('early').when === (spring ? '00:00–04:00' : '00:00–03:00') && dm.heads.get('early').dur === (spring ? '3 时' : '4 时'), JSON.stringify(dm.heads.get('early')));
    const lastHour = db.segs.find(x => x.row === 'late'), hit = peek(dm, db, lastHour.s + 30 * 60_000, zh);
    check(`${name}: the bar ends where the day does and the last hour, 23:00 to 24:00, is on it: a piece of one real hour that reaches the end`, db.t1 === end && lastHour.e === end && lastHour.f1 === 1 && lastHour.e - lastHour.s === 3_600_000 && clock(lastHour.s, TZ) === '23:00' && db.ticks.at(-1).label === '24' && db.ticks.at(-1).f === 1,
      JSON.stringify({ t1: db.t1 - end, f1: lastHour.f1, len: (lastHour.e - lastHour.s) / 60_000, from: clock(lastHour.s, TZ) }));
    check(`${name}: the pointer in the middle of it finds 23:30 and the stretch 23:00–24:00 of an hour, and so does a range over it`, hit.at === '23:30' && hit.stretch.range === '23:00–24:00' && hit.stretch.length === '1 时' && hit.row === 'late' && rowAt(dm, db, end - 60_000) === 'late'
      && (r => r.range === '23:00–24:00' && r.length === '1 时')(summarize(dm, db, lastHour.s, end, zh)), JSON.stringify({ at: hit.at, stretch: hit.stretch, row: hit.row }));
  }

  // a workout
  const run = fk('workout-1');
  check('a workout is a row with the activity\'s glyph, the activity in Chinese, what was counted and its length: 跑步, 3.2 公里 · 240 千卡 · 平均心率 142, 35 分', run.title === '跑步' && run.icon === 'run' && run.detail === '3.2 公里 · 240 千卡 · 平均心率 142' && run.right === '35 分' && run.lens === 'workout' && run.at === '07:20' && !run.connector);
  const one = (extra, t = zh) => flat(buildDay(small([it('w', 'workout', T(9, 0), T(9, 20), extra)]), NOW, t).blocks).find(r => r.key === 'w');
  const sports = [['walking', '步行', 'walk'], ['cycling', '骑车', 'bike'], ['hiking', '徒步', 'hike'], ['swimming', '游泳', 'swim'], ['functionalStrengthTraining', '力量训练', 'strength'], ['traditionalStrengthTraining', '力量训练', 'strength'], ['yoga', '瑜伽', 'yoga'], ['Running', '跑步', 'run'], ['elliptical', '运动', 'sport'], [undefined, '运动', 'sport']];
  check('each activity has its word and glyph (running, walking, cycling, hiking, swimming, strength, yoga); any other is 运动', sports.every(([activity, word, icon]) => { const r = one({ activity }); return r.title === word && r.icon === icon; }));
  check('the detail is only what is there: metres under a kilometre, kilometres above; no detail for a known activity with nothing counted; the phone\'s words for one it does not know', one({ activity: 'swimming', distance_m: 800 }).detail === '800 米' && one({ activity: 'running', distance_m: 10_000 }).detail === '10.0 公里'
    && one({ activity: 'yoga', energy_kcal: 90.4 }).detail === '90 千卡' && one({ activity: 'running', avg_hr: 150.6 }).detail === '平均心率 151' && one({ activity: 'yoga', text: 'Yoga' }).detail === '' && one({ activity: 'elliptical', text: 'Elliptical\nsecond line' }).detail === 'Elliptical'
    && one({ activity: 'running', distance_m: 3200, energy_kcal: 240 }, en).detail === '3.2 km · 240 kcal' && one({ activity: 'running', avg_hr: 142 }, en).detail === 'avg heart rate 142' && one({ activity: 'hiking' }, en).title === 'Hiking');
  check('a workout and a Claude Code session are counted in a lens: 运动 is the workout\'s length (the chip, in its place); the Mac\'s own number is the Mac\'s alone', lensOf('workout') === 'workout' && lensOf('code') === 'work'
    && numbers(fm, zh).map(n => `${n.label} ${n.value}`).join() === '睡 7:20,路上 3:04,运动 35 分,Mac 2:37,通话 18 分,和她 3 次,花 $12.40,照片 1' && numbers(fm, en).map(n => n.label).join() === 'Sleep,On the way,Workout,Mac,Calls,With her,Spent,Photos');
  check('the lens draws a workout on the bar though it is not a piece of it: one span, 07:20 to 07:55; the Mac\'s spans are the blocks and the sessions', !fb.segs.some(x => x.row === 'workout-1') && fb.lens.workout.length === 1 && Math.abs(msOf(fb, fb.lens.workout[0].f0) - T(7, 20)) < 1000 && Math.abs(msOf(fb, fb.lens.workout[0].f1) - T(7, 55)) < 1000 && fb.lens.work.length === 8);

  // Claude Code
  const home1 = fk('code:stay-home-1'), uvic1 = fk('code:stay-uvic');
  check('six sessions across two stays are two rows: Claude Code · 4 个会话 at home (the two newest titles, the time they covered together, 2 时 40 分) and Claude Code · 2 个会话 at UVic (1 时 30 分); side by side they count once',
    home1.title === 'Claude Code · 4 个会话' && home1.detail === '周报草稿 · 给 verify 加 fixtures' && home1.right === '2 时 40 分' && home1.at === '08:10' && home1.icon === 'code' && home1.lens === 'work' && home1.kind === 'code' && !home1.connector && !home1.wait
    && uvic1.title === 'Claude Code · 2 个会话' && uvic1.detail === '读论文做笔记 · Notch 的 hover 卡' && uvic1.right === '1 时 30 分' && uvic1.at === '15:40' && fr.filter(r => r.kind === 'code').length === 2 && !fr.some(r => /^code-s/.test(r.key ?? '')));
  check('a folded row stands where its first session began, inside the stay: between the run and the reminder at home', (keys => keys.indexOf('workout-1') < keys.indexOf('code:stay-home-1') && keys.indexOf('code:stay-home-1') < keys.indexOf('reminder-2'))(fr.map(r => r.key)) && fm.span.get('code:stay-home-1').t0 === T(8, 10) && fm.span.get('code:stay-home-1').t1 === T(11, 5) && fm.span.get('code:stay-uvic').t1 === T(17, 10)
    && !fm.span.has('code-s1') && fm.index.some(x => x.key === 'code:stay-home-1') && !fm.index.some(x => /^code-s/.test(x.key)));
  const lone = (extra, more = []) => flat(buildDay(small([it('h', 'stay', T(8, 0), T(12, 0), { place: 'X', kept_place: home }), ...more, it('c1', 'code', T(9, 0), T(9, 45), { title: '周报草稿', project: 'notes', account: 'b', ...extra })]), NOW, zh).blocks);
  check('one session alone is a normal row: Claude Code · its title, its project and account, its length; an account that is not a or b, or no project, leaves that out', (r => r.title === 'Claude Code · 周报草稿' && r.detail === 'notes · B' && r.right === '45 分' && r.icon === 'code' && r.lens === 'work' && r.key === 'c1')(lone({}).find(x => x.key === 'c1'))
    && lone({ account: 'x' }).find(x => x.key === 'c1').detail === 'notes' && lone({ project: undefined }).find(x => x.key === 'c1').detail === 'B' && lone({ title: undefined, project: undefined, account: undefined }).find(x => x.key === 'c1').title === 'Claude Code');
  const two = [it('c2', 'code', T(9, 30), T(10, 0), { title: '第二个', project: 'p', account: 'a' }), it('c3', 'code', T(10, 20), T(10, 50), { title: '第三个', project: 'p', account: 'a' })];
  check('three in one stay are one row: how many, the two newest titles, and the time they covered between them (09:00 to 10:00 and 10:20 to 10:50)', (r => r.length === 1 && r[0].key === 'code:h' && r[0].title === 'Claude Code · 3 个会话' && r[0].detail === '第三个 · 第二个' && r[0].right === '1 时 30 分')(lone({}, two).filter(r => r.kind === 'code')));
  const marked = buildDay(small([it('h', 'stay', T(8, 0), T(12, 0), { place: 'X', kept_place: home }), it('c1', 'code', T(9, 0), T(9, 45), { title: '周报草稿', project: 'notes', account: 'b' }), ...two], { highlights: [{ id: 'c2', label: '最重要的一次', by: 'him' }] }), NOW, zh);
  const mr = flat(marked.blocks).filter(r => r.kind === 'code');
  check('with one of three marked: the marked one is its own row under his words, the other two fold', mr.map(r => r.key).join() === 'code:h,c2' && mr.find(r => r.key === 'c2').title === '最重要的一次' && mr.find(r => r.key === 'c2').star && mr.find(r => r.key === 'code:h').title === 'Claude Code · 2 个会话', mr.map(r => r.key).join());
  const strays = flat(buildDay(small([it('c1', 'code', T(9, 0), T(9, 45), { title: 'a' }), it('c2', 'code', T(9, 30), T(10, 0), { title: 'b' })]), NOW, zh).blocks).filter(r => r.kind === 'code');
  check('sessions that began outside any stay are rows of their own', strays.map(r => r.key).join() === 'c1,c2');
  check('a session is Mac work in the lens, never in the Mac\'s number: with sessions alone the day has no Mac chip', numbers(buildDay(small([it('c1', 'code', T(9, 0), T(9, 45), { title: 'a' })]), NOW, zh), zh).length === 0 && numbers(buildDay(small([it('c1', 'code', T(9, 0), T(9, 45), { title: 'a' }), it('w', 'workout', T(7, 0), T(7, 30), { activity: 'yoga' })]), NOW, zh), zh).map(n => n.label).join() === '运动');
  check('the covered time of sessions that overlap counts once', covered([{ start_ms: 0, end_ms: 60_000 * 55 }, { start_ms: 60_000 * 30, end_ms: 60_000 * 85 }, { start_ms: 60_000 * 100, end_ms: null }], 60_000 * 110) === 95);

  // the card
  const lines = (p) => p.lines.map(l => `${l.kind}:${l.text}|${l.side}`).join();
  check('the card at 07:35, in the run: the workout as 跑步 · 3.2 公里 with its length', lines(peek(fm, fb, T(7, 35), zh)) === 'workout:跑步 · 3.2 公里|35 分');
  check('the card at 08:30 with sessions side by side: one line, how many and how long together, never one line for each', lines(peek(fm, fb, T(8, 30), zh)) === 'code:Claude Code · 2 个会话|1 时 25 分' && lines(peek(fm, fb, T(16, 20), zh)) === 'code:Claude Code · 2 个会话|1 时 30 分');
  check('the card\'s order is work, code, call, talk, workout, picture, payment, event: at 10:30 the Mac block before the sessions', lines(peek(fm, fb, T(10, 30), zh)) === 'work:Jarvis · Xcode|活跃 42 分,code:Claude Code · 2 个会话|1 时 15 分');
  const solo = buildDay(small([it('h', 'stay', T(8, 0), T(12, 0), { place: 'X', kept_place: home }), it('c1', 'code', T(9, 0), T(9, 45), { title: '周报草稿', project: 'notes', account: 'b' }), it('t', 'talk', T(9, 20), T(9, 25), { turns: 2 }, 'her'), it('p', 'picture', T(9, 22), T(9, 22), { note: '一张图' })]), NOW, zh);
  check('one session near is its own line: Claude Code · its title; before the talk and the photo', lines(peek(solo, buildBar(solo, zh), T(9, 23), zh)) === 'code:Claude Code · 周报草稿|45 分,talk:和她说话 · 2 句|5 分,picture:一张图|09:22', lines(peek(solo, buildBar(solo, zh), T(9, 23), zh)));
  check('what a workout or a session did before the day\'s start takes the bar back to its hour (a run at 05:20 to 05:00, a session at 04:10 to 04:00)', (d => clock(axisOf(buildDay(small(d), NOW, zh)).t0, TZ))([it('sl', 'sleep', T(22, 0, 18), T(5, 10), { minutes: 400 }), it('w', 'workout', T(5, 20), T(5, 50), { activity: 'running' })]) === '05:00'
    && (d => clock(axisOf(buildDay(small(d), NOW, zh)).t0, TZ))([it('sl', 'sleep', T(22, 0, 18), T(5, 10), { minutes: 400 }), it('c', 'code', T(4, 10), T(4, 50), { title: 'x' })]) === '04:00');
  const cut = within(fm.blocks, fm.span, T(8, 0), T(9, 0)), ks = (bs, out = []) => { for (const b of bs) { out.push(b.type === 'row' ? b.row.key : b.key); if (b.type === 'stay') ks(b.items, out); } return out; };
  check('the list cut to 08:00–09:00 keeps the folded row and its stay, and not the run before it', ks(cut).join() === 'stay-home-1,code:stay-home-1' && !ks(cut).includes('workout-1'));
  check('the bar\'s sun: sunrise and sunset where they fall on the axis (07:25 and 18:05 from 06:00), none off it', fb.sun.map(x => `${x.kind}@${Math.round(msOf(fb, x.f) / 60_000)}`).join() === `rise@${Math.round(T(7, 25) / 60_000)},set@${Math.round(T(18, 5) / 60_000)}` && buildBar(buildDay(fullDay({ weather: { condition: 'clear', high: 1, low: 0, sunrise_ms: T(4, 0), sunset_ms: null } }), NOW, zh), zh).sun.length === 0
    && buildBar(buildDay(fullDay({ weather: null }), NOW, zh), zh).sun.length === 0);
}

// ---------- the open rows, in node (spec-r3 section 8) ----------
{
  const m = buildDay(fullDay(), NOW, zh), me = buildDay(fullDay(), NOW, en), open = (key, model = m, say = model === me ? en : zh) => openOf(model, key, say);
  const lines = (key, model = m, say) => open(key, model, say).parts.filter(p => p.type === 'lines').flatMap(p => p.lines.map(l => l.text)), part = (key, type, model = m) => open(key, model).parts.find(p => p.type === type);
  const heads = [...m.heads.keys()], keys = [...m.rows.keys(), ...heads];
  const empty = keys.filter(k => !open(k)?.parts.length), bare = keys.filter(k => !hasBody(open(k), !!askOf(m, k, zh)));
  check('every row of the day and every stay is known; a key that is no row opens nothing; the departure, whose two lines and right column say it all, has nothing to show (its button is 问她, as the reminders have theirs)', keys.length >= 28 && keys.every(k => open(k) !== null) && open('now') === null && open('nope') === null && open('') === null && bare.length === 0 && empty.includes('reminder-2') && empty.includes('departure-1'), `${bare} | ${empty}`);
  check('a stay he keeps: the place text, then 常去的地方 · 家, and the kept place to rename; one he did not name has the place text only and nothing to rename',
    lines('stay-home-1').join('|') === '12 Example St, Victoria|常去的地方 · 家' && JSON.stringify(open('stay-home-1').rename) === '{"id":"p-home","name":"家"}' && lines('stay-aw').join('|') === 'A&W, 1234 Douglas St, Victoria' && open('stay-aw').rename === null);
  check('a move: from and to by the names of the stays either side (within half an hour), one side alone says which; its word and length are the row\'s and are not said again',
    lines('move-1').join('|') === '家 → A&W' && lines('move-2').join('|') === '去 A&W' && lines('move-3').join('|') === 'A&W → 家'
    && lines('f-w11', buildDay(fragmentedDay(), NOW, zh)).join('|') === '从 家 出发' && lines('move-3', me).join('|') === 'A&W → 家');
  const ride = (reason, over = {}) => lines('r', buildDay(small([it('r', 'ride', T(14, 50), T(15, 19), { route: '27', board_stop: 'A', alight_stop: 'B', ended_reason: reason, credit: null, ...over })]), NOW, zh)).join('|');
  check('a ride: how it ended in plain words for each reason the brain has; a reason it does not know says nothing; one still going is still on; the route is the row\'s second line and is not said again',
    ride('left_vehicle') === '你下车了' && ride('passed_stop') === '到了你要下的站' && ride('said') === '你说你下车了' && ride('schedule_end') === '这趟车跑完了' && ride('replaced') === '你换了另一班车' && ride('mystery') === ''
    && lines('r', buildDay(small([it('r', 'ride', T(21, 30), null, { route: '27', board_stop: 'A', alight_stop: null })]), NOW, zh)).join('|') === '还在车上');
  const far = { board_stop: 'Shelbourne Street at Mckenzie Avenue', alight_stop: 'University of Victoria Exchange Bay 4' };
  check('and when it is credited, 实时：BC Transit; in English Live: BC Transit, You got off; a route too long for the row\'s line (over 40) is said in full, first', ride('left_vehicle', { credit: 'BC Transit' }) === '实时：BC Transit|你下车了' && lines('ride-1', me).join('|') === 'You got off'
    && ride('left_vehicle', far) === 'Shelbourne Street at Mckenzie Avenue → University of Victoria Exchange Bay 4|你下车了');
  check('a talk is read from the day\'s records between its first and last line (the part says which), and says 共 6 句 under them',
    JSON.stringify(part('talk-1', 'talk')) === JSON.stringify({ type: 'talk', day: DATE, from: T(10, 2), to: T(10, 9) }) && lines('talk-1').join() === '共 6 句' && lines('talk-3').join() === '共 1 句' && lines('talk-1', me).join() === '6 lines in all');
  check('a picture: its id and the note in full, lines and all (the row has said the first line: a note of one short line is not said again); one with no id kept shows the note; one with neither has nothing to show',
    JSON.stringify(part('picture-1', 'picture')) === JSON.stringify({ type: 'picture', id: PIC, note: '汉堡和薯条\n在 A&W 吃的午饭', show: true })
    && JSON.stringify(part('p', 'picture', buildDay(small([it('p', 'picture', T(13, 30), T(13, 30), { attachment: PIC, note: '一张' })]), NOW, zh))) === JSON.stringify({ type: 'picture', id: PIC, note: '一张', show: false })
    && part('p', 'picture', buildDay(small([it('p', 'picture', T(13, 30), T(13, 30), { attachment: 'zz', note: '一张\n两行' })]), NOW, zh)).id === ''
    && openOf(buildDay(small([it('p', 'picture', T(13, 30), T(13, 30), { attachment: 'zz', note: '一张' })]), NOW, zh), 'p', zh).parts.length === 0);
  const ev = (extra, state = 'next') => lines('e', buildDay(small([it('e', 'event', T(22, 30), T(23, 30), { title: 'x', ...extra })]), NOW, zh)).join('|');
  check('an event: its time range and where it stands (what the row has, the source, whether mail has it unconfirmed, and the place, is not said again); mail that is confirmed says so; a place too long for the row\'s line is said in full; a private one never says its place',
    lines('event-1').join('|') === '22:30–23:30 · 1 时 · 还没开始' && lines('event-2').join('|') === '23:40–23:55 · 15 分 · 还没开始' && lines('event-1', me).join('|') === '22:30–23:30 · 1 h · Not yet'
    && ev({ source: 'mail', confirmed: true }) === '22:30–23:30 · 1 时 · 已确认 · 还没开始' && ev({ place: '1234 Douglas Street, Victoria, British Columbia' }) === '22:30–23:30 · 1 时 · 还没开始|1234 Douglas Street, Victoria, British Columbia'
    && ev({ place: '1234 Douglas Street, Victoria, British Columbia', private: true }) === '22:30–23:30 · 1 时 · 还没开始');
  check('a reminder: its time, its text and how it stands are the row\'s; only one that rang and was not acknowledged has a 知道了 to press (and says which)',
    lines('reminder-2').join('|') === '' && open('reminder-2').ack === 'reminder-2' && lines('reminder-1').join('|') === '' && open('reminder-1').ack === null
    && open('f-remind', buildDay(fragmentedDay(), NOW, zh)).ack === null && lines('f-remind', buildDay(fragmentedDay(), NOW, zh)).join('|') === '');
  const wordy = '把下周三要交的那份课程报告的第二稿发给 Prof. Lee 之前先过一遍参考文献';
  check('the words the row already has for its title are not said again, unless they are long enough for the row to have cut them (over 20): then the reminder says them in full, and a Claude chat too, before when it ran', lines('r', buildDay(small([it('r', 'reminder', T(22, 0), null, { text: wordy, fired: false, acknowledged: false }, 'her')]), NOW, zh)).join('|') === `${wordy}`);
  const dep = extra => lines('d', buildDay(small([it('d', 'departure', T(22, 40), T(22, 40), { route: '27', board_stop: 'Shelbourne at Pear', to: 'UVic', leave_at_ms: T(22, 33), ...extra })]), NOW, zh)).join('|');
  check('a departure: the route, the stop, where to and when to leave are the row\'s; the stop is said only when the row\'s title does not carry it', lines('departure-1').join('|') === '' && dep({ title: '27 · Shelbourne at Pear' }) === '' && dep({ title: '' }) === '27 路 · Shelbourne at Pear' && dep({ title: '', board_stop: '' }) === '');
  const w = part('work-2', 'bars') && m.items.find(x => x.id === 'work-2'), bars = key => open(key).parts.filter(p => p.type === 'bars');
  check('a work block: active and whole time, then its projects and its apps as bars with their lengths and their share of the active time',
    lines('work-2').join() === '活跃 1 时 55 分 · 共 2 时 5 分' && bars('work-2').map(b => `${b.heading}:${b.bars.map(x => `${x.name} ${x.text} ${x.share.toFixed(2)}`).join('/')}`).join('|') === '项目:Jarvis 1 时 40 分 0.87|应用:Ghostty 1 时 10 分 0.61/Claude 30 分 0.26' && open('work-2').summarize === true);
  const many = buildDay(small([it('w', 'work', T(10, 0), T(14, 0), { active_s: 12000, projects: Array.from({ length: 7 }, (_, i) => ({ name: `P${i}`, seconds: 1000 + i * 100 })), apps: [{ name: '', seconds: 5 }, { name: 'Z', seconds: 0 }] })]), NOW, zh);
  check('at most five of each, the biggest first; a nameless one and one of no time are not listed, and with none of a kind there is no list of it',
    openOf(many, 'w', zh).parts.filter(p => p.type === 'bars').length === 1 && openOf(many, 'w', zh).parts.find(p => p.type === 'bars').bars.map(x => x.name).join() === 'P6,P5,P4,P3,P2');
  const night = part('sleep-1', 'night').night;
  check('a night: the stages on the row\'s own time scale from 23:10 to 07:00, and the totals in the order deep, core, REM, then how many times awake',
    night.from === T(23, 10, 18) && night.to === T(7, 0) && night.at.join() === '23:10,07:00' && night.totals === '深睡 1:10 · 核心 4:30 · REM 1:40 · 醒 3 次' && night.slices.length > 10 && night.slices.every((x, i, l) => x.e > x.s && (i === 0 || l[i - 1].e <= x.s))
    && part('sleep-1', 'night', me).night.totals === 'Deep 1:10 · Core 4:30 · REM 1:40 · Awake 3×' && lines('sleep-1').join() === night.totals);
  check('a night with no stages is only its time and length', lines('sleep-1', buildDay(makeDay(), NOW, zh)).join() === '00:55–09:05 · 8 时 10 分' && !part('sleep-1', 'night', buildDay(makeDay(), NOW, zh)));
  check('a run: when it ran, to the minute, with its pace (what was counted is the row\'s); a call: when it ran and the app; a payment: which way, the amount and the currency (who, what and through what are the row\'s)',
    lines('workout-1').join('|') === `07:20–07:55 · 35 分 · 10'56"/公里` && lines('workout-1', me).join('|') === `07:20–07:55 · 35 min · 10'56"/km`
    && lines('call-1').join('|') === '16:40–16:58 · 18 分 · FaceTime' && lines('call-1', me).join('|') === '16:40–16:58 · 18 min · FaceTime' && lines('pay-1').join('|') === '支出 · $12.40 · CAD' && lines('pay-1', me).join('|') === 'Spent · $12.40 · CAD');
  const wk = (over, say = zh) => lines('w', buildDay(small([it('w', 'workout', T(7, 20), T(7, 55), { activity: 'running', ...over })]), NOW, say), say).join('|');
  check('a workout: no distance, no pace (a pace is a distance over a time); a bike has a speed, not a pace; what was counted is the row\'s, and even all of it fits the row\'s 40 characters',
    wk({ energy_kcal: 240, avg_hr: 142 }) === '07:20–07:55 · 35 分' && wk({ distance_m: 3200 }) === `07:20–07:55 · 35 分 · 10'56"/公里`
    && wk({ activity: 'cycling', distance_m: 10_000, energy_kcal: 300 }) === '07:20–07:55 · 35 分 · 17.1 公里/时' && wk({ activity: 'cycling', distance_m: 10_000 }, en) === '07:20–07:55 · 35 min · 17.1 km/h' && wk({}) === '07:20–07:55 · 35 分'
    && wk({ distance_m: 42_195, energy_kcal: 2_400, avg_hr: 158 }, en) === `07:20–07:55 · 35 min · 0'50"/km`,
    [wk({ energy_kcal: 240, avg_hr: 142 }), wk({ distance_m: 3200 }), wk({ activity: 'cycling', distance_m: 10_000, energy_kcal: 300 }), wk({}), wk({ distance_m: 42_195, energy_kcal: 2_400, avg_hr: 158 }, en)].join(' // '));
  const cl = (over, say = zh) => lines('c', buildDay(small([it('c', 'call', T(16, 40), T(16, 58), over)]), NOW, say), say).join('|');
  check('a call with no app says only when it ran; one still going says since when', cl({}) === '16:40–16:58 · 18 分' && cl({ app: '  ' }) === '16:40–16:58 · 18 分' && lines('c', buildDay(small([it('c', 'call', T(21, 30), null, { app: 'WeChat' })]), NOW, zh)).join('|') === '21:30 起 · WeChat');
  const ch = (over, say = zh) => lines('c', buildDay(small([it('c', 'claude_chat', T(20, 10), T(20, 42), { source: 'claude', ...over })]), NOW, say), say).join('|');
  check('a Claude chat: when it ran with how many messages, and its title in full only when the row may have cut it',
    lines('chat-1').join('|') === '20:10–20:42 · 32 分 · 14 条' && lines('chat-1', me).join('|') === '20:10–20:42 · 32 min · 14 messages'
    && ch({ text: 'A title that is longer than the twenty characters the row keeps', messages: 1 }, en) === 'A title that is longer than the twenty characters the row keeps|20:10–20:42 · 32 min · 1 message'
    && ch({ text: '', messages: 3 }) === '20:10–20:42 · 32 分 · 3 条' && ch({ text: 'x' }) === '20:10–20:42 · 32 分' && ch({ text: 'x', messages: 0 }) === '20:10–20:42 · 32 分');
  check('a Claude day: its titles as a list, in full', JSON.stringify(part('claude-day-1', 'titles').list) === '["The bar for the day","A tool exam"]' && lines('claude-day-1').join() === '');
  const first = key => open(key).parts[0]?.lines?.[0]?.tone, tones = (key, model = m) => open(key, model).parts.flatMap(p => p.type === 'lines' ? p.lines.map(l => l.tone) : []).join();
  check('what an open row says first is in ink and the rest is muted (a talk\'s 共 N 句, which comes after his line and hers, is muted)', first('stay-home-1') === 'ink' && tones('stay-home-1') === 'ink,muted' && first('call-1') === 'ink' && tones('event-1') === 'ink' && tones('chat-1') === 'ink' && tones('workout-1') === 'ink' && tones('call-1') === 'ink' && tones('talk-1') === 'muted' && tones('sleep-1') === 'muted'
    && tones('work-2') === 'ink' && tones('r', buildDay(small([it('r', 'ride', T(14, 50), T(15, 19), { route: '27', board_stop: 'A', alight_stop: 'B', ended_reason: 'left_vehicle', credit: 'BC Transit' })]), NOW, zh)) === 'ink,muted', [first('stay-home-1'), tones('stay-home-1'), tones('talk-1'), tones('sleep-1')].join(' '));
  const home = part('code:stay-home-1', 'sessions').list, uv = part('code:stay-uvic', 'sessions').list;
  check('the sessions folded into a row are listed oldest first, each with when it began, its title, project · account, how long and its recap in full; one alone is its own row and opens to its recap',
    home.map(x => `${x.at} ${x.meta} ${x.length}`).join() === '08:10 jarvis · A 55 分,08:40 jarvis-ios · B 55 分,09:50 jarvis · A 50 分,10:20 notes · B 45 分' && home[0].title === 'Phase A 的 review 修复' && home[0].recap === 'Phase A 的 review 修复 的小结' && uv.length === 2
    && JSON.stringify(part('s', 'sessions', buildDay(small([code(1, 'a', 810, 905, 'T', 'jarvis')].map(x => ({ ...x, id: 's' }))), NOW, zh)).list.map(x => [x.title, x.meta, x.recap])) === '[["T","jarvis · A","T 的小结"]]');
  // what a talk row says of its span, from the day's records
  const recs = records().map(r => ({ ...r, ts: Date.parse(r.ts) })), said = (from, to) => talkLines(recs, from, to);
  check('a talk shows his first line in its span and her first reply to it, one line each (a line break is a space), and nothing of another talk; a span with no line of his shows nothing; a reply that is not there is left out',
    JSON.stringify(said(T(10, 2), T(10, 9))) === JSON.stringify({ you: '帮我看看今天下午有什么安排，顺便提醒我给银行打电话 这件事别忘了', her: records()[3].text }) && said(T(14, 55), T(14, 55)).you === '27 路下一班什么时候' && said(T(14, 55), T(14, 55)).her === '还有 6 分钟。'
    && said(T(8, 31), T(8, 31)).you === '早' && said(T(16, 0), T(16, 5)) === null && said(T(19, 30), T(19, 30)).her === '' && said(T(19, 30), T(19, 30)).you === '今天先到这里');
  const lone = (id, kind, extra = {}) => openOf(buildDay(small([it(id, kind, T(13, 30), T(13, 30), extra)]), NOW, zh), id, zh);
  check('a record with nothing in it but its marker is no line, and a row opens to something only when it has a part or a button that works (the star is one: every item the brain can mark has it; an id it would refuse has none)', talkLines([{ id: 'a', ts: 5, who: 'user', text: '  [record_id=abc]  ' }, { id: 'b', ts: 6, who: 'user', text: '嗯' }], 0, 10).you === '嗯'
    && hasBody(open('stay-aw'), true) && hasBody(open('stay-aw'), false) && hasBody(lone('p', 'picture', { attachment: 'zz' }), false) && lone('p', 'picture', { attachment: 'zz' }).parts.length === 0
    && !hasBody(lone('p q', 'picture', { attachment: 'zz' }), false) && hasBody(lone('p q', 'picture', { attachment: 'zz' }), true) && !hasBody(lone('p q', 'stay', {}), false) && hasBody(lone('s', 'stay', {}), false));
  // what a turn about a row carries
  const ask = (key, model = m) => askOf(model, key, model === me ? en : zh);
  check('what is handed to her is exactly {kind, id, title, start_ms} (the title as the row says it, the time as a whole number), with the icon and time the input shows',
    JSON.stringify(ask('talk-1')) === JSON.stringify({ about: { kind: 'talk', id: 'talk-1', title: '和她说话', start_ms: T(10, 2) }, icon: 'talk', at: '10:02' }) && JSON.stringify(ask('stay-home-1').about) === JSON.stringify({ kind: 'stay', id: 'stay-home-1', title: '家', start_ms: T(7, 0) })
    && ask('stay-home-1').icon === 'home' && ask('reminder-2').about.title === '给银行打电话' && ask('sleep-1').about.title === '睡觉'
    && Number.isInteger(ask('picture-1').about.start_ms) && Object.keys(ask('event-1').about).join() === 'kind,id,title,start_ms', JSON.stringify(ask('talk-1')));
  const refused = ['code:stay-home-1', 'now', 'nope'], fits = ['stay-home-1', 'stay-aw', 'move-1', 'sleep-1', 'work-1', 'call-1', 'talk-1', 'picture-1', 'event-1', 'reminder-1', 'ride-1', 'departure-1', 'pay-1', 'chat-1', 'claude-day-1', 'workout-1'];
  const COMMIT = `commitment-${'ab12'.repeat(8)}@${DATE}`, cm = buildDay(small([it(COMMIT, 'event', T(22, 0), T(23, 0), { title: '导师见面', source: 'mail', confirmed: false })]), NOW, zh);
  check('a row that is no one item (the folded row of sessions), a key that is no row, or an id the brain would refuse has nothing to hand over; every kind it takes does, a run, a ride, a bus, money, Claude and a commitment (its id has an @) among them',
    refused.every(k => ask(k) === null) && fits.every(k => ask(k)?.about.id === k) && askOf(buildDay(small([it('x'.repeat(257), 'talk', T(10, 0), T(10, 5), { turns: 1 })]), NOW, zh), 'x'.repeat(257), zh) === null
    && askOf(buildDay(small([it('a b', 'talk', T(10, 0), T(10, 5), { turns: 1 })]), NOW, zh), 'a b', zh) === null && askOf(cm, COMMIT, zh)?.about.id === COMMIT, refused.filter(k => ask(k) !== null).join());
  const handed = key => JSON.stringify(ask(key)?.about);
  check('what is handed over for the kinds that can be asked since the brain took them is exactly {kind, id, title, start_ms}, titled as the row says it: a ride by its route, a bus by its title, money by who, a Claude chat by its name, a Claude day, and a commitment with its @ id',
    handed('ride-1') === JSON.stringify({ kind: 'ride', id: 'ride-1', title: '27 路', start_ms: T(14, 50) }) && handed('departure-1') === JSON.stringify({ kind: 'departure', id: 'departure-1', title: '27 · Shelbourne at Pear', start_ms: T(22, 40) })
    && handed('pay-1') === JSON.stringify({ kind: 'payment', id: 'pay-1', title: 'A&W', start_ms: T(13, 20) }) && handed('chat-1') === JSON.stringify({ kind: 'claude_chat', id: 'chat-1', title: 'The bar for the day', start_ms: T(20, 10) })
    && handed('claude-day-1') === JSON.stringify({ kind: 'claude_day', id: 'claude-day-1', title: 'Claude 日报', start_ms: T(9, 40) }) && JSON.stringify(askOf(cm, COMMIT, zh).about) === JSON.stringify({ kind: 'event', id: COMMIT, title: '导师见面', start_ms: T(22, 0) })
    && ask('pay-1').icon === 'payment' && ask('chat-1').at === '20:10', [handed('ride-1'), handed('departure-1'), handed('pay-1')].join(' '));
  check('in English the input says Ask her about the row it holds, with its own title', ask('talk-1', me).about.title === 'Talked with her' && ask('stay-home-1', me).about.title === '家');
  // the brain takes a run and a Claude Code session (main a8ebde17): an opened run and a session alone hand over their own kind and id; the folded row of sessions is no one item
  const stayX = it('h', 'stay', T(8, 0), T(12, 0), { place: 'X' }), c1 = it('c1', 'code', T(9, 0), T(9, 45), { title: '周报草稿', project: 'notes', account: 'b' });
  const oneC = buildDay(small([stayX, c1]), NOW, zh), manyC = buildDay(small([stayX, c1, it('c2', 'code', T(9, 30), T(10, 0), { title: '第二个' })]), NOW, zh);
  check('an opened run hands her {workout, its id}, a session alone {code, its id} titled as its row says it; the folded row of sessions (code:<stay>) hands over nothing',
    JSON.stringify(ask('workout-1').about) === JSON.stringify({ kind: 'workout', id: 'workout-1', title: '跑步', start_ms: T(7, 20) }) && hasBody(open('workout-1'), true)
    && JSON.stringify(askOf(oneC, 'c1', zh).about) === JSON.stringify({ kind: 'code', id: 'c1', title: 'Claude Code · 周报草稿', start_ms: T(9, 0) }) && hasBody(openOf(oneC, 'c1', zh), true)
    && manyC.rows.has('code:h') && !manyC.rows.has('c1') && askOf(manyC, 'code:h', zh) === null, [ask('workout-1'), askOf(oneC, 'c1', zh), askOf(manyC, 'code:h', zh)].map(x => JSON.stringify(x?.about)).join(' '));
}

// ---------- a number's card, in node (ADR 0296): what is behind each number of the day ----------
// A card as lines: the head, then each part (cells split by /, the entries of a list and the groups of bars by //, a hypnogram by its two ends).
const cardLines = card => card === null ? null : [`${card.label}|${card.value}`, ...card.parts.map(p => p.type === 'cells' ? p.cells.map(c => `${c.label} ${c.value}`).join('/')
  : p.type === 'entries' ? p.entries.map(e => `${e.lead}: ${e.facts.join(' · ')}`).join('//') + (p.more ? ` +${p.more}` : '')
  : p.type === 'bars' ? p.groups.map(g => `${g.heading}: ${g.bars.map(b => `${b.name} ${b.text}`).join(', ')}`).join('//')
  : p.type === 'night' ? `night ${p.night.at.join('-')}` : `pictures ${p.list.map(x => `${x.id[0]}@${x.at}`).join(',')}${p.more ? ` +${p.more}` : ''}`)];
{
  const fz = buildDay(fullDay(), NOW, zh), fe = buildDay(fullDay(), NOW, en), same = (got, want, detail = '') => JSON.stringify(got) === JSON.stringify(want) || (console.log(JSON.stringify(got)), false);
  const lines = (model, kind, t = zh) => cardLines(cardOf(model, kind, t));
  check('the sleep card: the head is the number; in bed, asleep, woke up as clocks, how long it took, the share of the time in bed (the phone\'s in_bed 23:00 to 07:10), deep, core and REM, the times he woke between sleeps (2: the last awake stretch is the end of the night), the night, and the day\'s own heart rate and HRV',
    same(lines(fz, 'sleep'), ['睡|7 时 20 分', '上床 23:00/入睡 23:10/醒来 07:00/入睡用时 10 分/睡眠效率 90%', '深睡 1:10/核心 4:30/REM 1:40/夜醒 2 次', 'night 23:10-07:00', '当天静息心率 58/当天 HRV 42']));
  check('and in English', same(lines(fe, 'sleep', en), ['Sleep|7 h 20 min', 'In bed 23:00/Asleep 23:10/Woke up 07:00/To fall asleep 10 min/Sleep efficiency 90%', 'Deep 1:10/Core 4:30/REM 1:40/Wake-ups 2×', 'night 23:10-07:00', "Day's resting HR 58/Day's HRV 42"]));
  check('on the way: the total, the time in each way (the most first; none he did not go), how many, the longest with where it went from and to as its open row words it, and the day\'s steps',
    same(lines(fz, 'trip'), ['路上|3 时 4 分', '开车 1 时 40 分/公交 1 时 4 分/步行 20 分', '共 5 段/最长 开车 · 1 时 40 分 · 家 → A&W/步数 8,240']));
  check('a workout is one line: what, when it began, how long, how far, how fast (min per km, since it is a run), the energy and the heart',
    same(lines(fz, 'workout'), ['运动|35 分', `跑步: 07:20 · 35 分 · 3.2 公里 · 10'56"/公里 · 240 千卡 · 平均心率 142`]));
  check('the Mac: when it began and ended, the active time, the time in all; the projects and the apps of the day as bars (the biggest first, summed over the blocks); the Claude Code sessions and their projects (not in the Mac\'s number)',
    same(lines(fz, 'work'), ['Mac|10:30–21:40 · 活跃 2 时 37 分 · 共 2 时 55 分', '项目: Jarvis 2 时 15 分//应用: Ghostty 1 时 30 分, Claude 30 分, Xcode 25 分', 'Claude Code: 6 个会话 · jarvis ×3 · jarvis-ios · notes · thesis']));
  check('and in English', same(lines(fe, 'work', en), ['Mac|10:30–21:40 · active 2 h 37 min · 2 h 55 min in all', 'Projects: Jarvis 2 h 15 min//Apps: Ghostty 1 h 30 min, Claude 30 min, Xcode 25 min', 'Claude Code: 6 sessions · jarvis ×3 · jarvis-ios · notes · thesis']));
  check('calls: how many and the longest with its clock, then each app with its calls and its time; with her: the lines in all and when he talked first and last; money: out and how many payments, then who it went to; photos: a thumbnail each',
    same(lines(fz, 'call'), ['通话|18 分', '通话 1 次/最长 18 分 · 16:40', 'FaceTime: 1 次 · 18 分']) && same(lines(fz, 'talk'), ['和她|3 次', '共 11 句/时段 10:02–19:36'])
    && same(lines(fz, 'payment'), ['花|$12.40', '支出 $12.40/笔数 1 笔', 'A&W: $12.40']) && same(lines(fz, 'picture'), ['照片|1', 'pictures a@13:30']));
  check('the head of every card is its chip: the label and the value the chip says (the Mac\'s says more, with it)', numbers(fz, zh).length === 8 && numbers(fz, zh).every(n => { const c = cardOf(fz, n.kind, zh); return c && c.kind === n.kind && c.label === n.label && (n.kind === 'work' ? c.value.includes(n.long) : c.value === n.long); })
    && numbers(fe, en).every(n => { const c = cardOf(fe, n.kind, en); return c && c.label === n.label; }));
  check('a kind the day has no number for has no card (nothing counted: no photo, a Claude Code session is not Mac time)', ['workout', 'work', 'call', 'payment'].every(k => cardOf(buildDay(makeDay(), NOW, zh), k, zh) === null)
    && cardOf(buildDay(small([it('c1', 'code', T(9, 0), T(9, 45), { title: 'a', project: 'x' })]), NOW, zh), 'work', zh) === null && cardOf(buildDay(small([]), NOW, zh), 'sleep', zh) === null);

  // sleep: what is left out when its inputs are not there
  const night = (stages, over = {}, mins = 440, end = T(7, 0)) => lines(buildDay(small([it('sleep-1', 'sleep', T(23, 10, 18), end, { minutes: mins, stages })], over), NOW, zh), 'sleep');
  const inBed = (a, b) => ({ stage: 'in_bed', start_ms: a, end_ms: b });
  check('time in bed, how long it took and the share of it are only with a stage before sleep (in_bed or awake) and, for the share, an in_bed stage: a night without the phone\'s in_bed has neither the time in bed nor the share',
    same(night(NIGHT).slice(0, 2), ['睡|7 时 20 分', '入睡 23:10/醒来 07:00']));
  check('an awake stretch before the first sleep is the time he went to bed and how long it took (10 min), but not a share: that wants the in_bed',
    same(night([{ stage: 'awake', start_ms: T(23, 0, 18), end_ms: T(23, 10, 18) }, ...NIGHT]).slice(1, 3), ['上床 23:00/入睡 23:10/醒来 07:00/入睡用时 10 分', '深睡 1:10/核心 4:30/REM 1:40/夜醒 2 次']));
  check('an in_bed that begins with the first sleep has the share (440 of 480 min: 92%) and no time in bed or time it took; one shorter than the sleep has none',
    same(night([inBed(T(23, 10, 18), T(7, 10)), ...NIGHT])[1], '入睡 23:10/醒来 07:00/睡眠效率 92%') && same(night([inBed(T(23, 10, 18), T(1, 0)), ...NIGHT])[1], '入睡 23:10/醒来 07:00'));
  check('a night with no stages is the clocks of its row and nothing else: no stage, no hypnogram, no time it took; the heart rate and HRV only where the day has them (one of them alone is alone)',
    same(lines(buildDay(makeDay(), NOW, zh), 'sleep'), ['睡|7 时 50 分', '入睡 00:55/醒来 09:05']) && same(night(NIGHT, { summary: { steps: 9, resting_hr: null, hrv: 42 } }).at(-1), '当天 HRV 42') && same(night(NIGHT, { summary: { steps: 9, resting_hr: null, hrv: null } }).at(-1), 'night 23:10-07:00'));
  check('a night still going has the clocks it has (no woke up), the stages to now, and no time he woke between sleeps',
    same(night(NIGHT.slice(0, 4), {}, 100, null), ['睡|1 时 40 分', '入睡 23:10', '深睡 0:40/核心 1:55', 'night 23:10-21:50']));
  check('the times he woke are the awake stretches with sleep on both sides (the open row counts every awake stretch: 3)', cardOf(fz, 'sleep', zh).parts[1].cells.at(-1).value === '2 次' && openOf(fz, 'sleep-1', zh).parts.find(p => p.type === 'night').night.totals.endsWith('醒 3 次'));

  // on the way, workouts, the Mac and the rest
  const day = (items, over) => buildDay(small(items, over), NOW, zh);
  check('on the way: each way he went with its time (a bus is 公交, one the brain has no word for is 移动), the longest of them (a ride by its route and stops), and no steps where the day has none',
    same(lines(day([it('m1', 'move', T(8, 0), T(8, 30), { activity: 'running' }), it('m2', 'move', T(9, 0), T(10, 0), { activity: 'cycling' }), it('m3', 'move', T(11, 0), T(11, 20), { activity: 'scooter' }), it('r1', 'ride', T(12, 0), T(12, 45), { route: '14', board_stop: 'A St', alight_stop: 'B St' })]), 'trip'),
      ['路上|2 时 35 分', '骑车 1 时/公交 45 分/跑步 30 分/移动 20 分', '共 4 段/最长 骑车 · 1 时'])
    && same(lines(day([it('r1', 'ride', T(12, 0), T(12, 45), { route: '14', board_stop: 'A St', alight_stop: 'B St' })]), 'trip').at(-1), '共 1 段/最长 14 路 · 45 分 · A St → B St')
    && same(lines(buildDay(small([it('r1', 'ride', T(12, 0), T(12, 45), { route: '14', board_stop: 'A St', alight_stop: 'B St' })]), NOW, en), 'trip', en).at(-1), 'In all 1 trip/Longest Bus 14 · 45 min · A St → B St'));
  check('the run\'s pace is minutes per km walking or running and km per hour cycling, only with a distance and a length: none for swimming or a hike, for no distance, or for one that has not ended',
    (w => w('walking', 2000, 30).pace === `15'00"/公里` && w('running', 5000, 25).pace === `5'00"/公里` && w('cycling', 20000, 60).pace === '20.0 公里/时' && w('swimming', 800, 30).pace === '' && w('hiking', 4000, 90).pace === '' && w('running', 0, 30).pace === '' && w('running', undefined, 30).pace === ''
      && w('running', 5000, null).pace === '' && w('running', 5000, 0.5).pace === '' && workoutOf({ ...day([it('w', 'workout', T(7, 0), T(7, 35), { activity: 'running', distance_m: 3200 })]).items[0] }, en).pace === `10'56"/km` && w('cycling', 20000, 60, en).pace === '20.0 km/h'
      && w('running', 3200, 35).facts.join() === '3.2 公里,240 千卡,平均心率 142')((a, m, min, t = zh) => workoutOf({ id: 'w', lane: 'you', kind: 'workout', state: 'done', start_ms: T(7, 0), end_ms: min === null ? null : T(7, 0) + min * 60_000, activity: a, distance_m: m, energy_kcal: 240, avg_hr: 142 }, t)));
  const runs = [it('w1', 'workout', T(7, 0), T(7, 30), { activity: 'walking', distance_m: 2000 }), it('w2', 'workout', T(18, 0), T(19, 0), { activity: 'cycling', distance_m: 20000, energy_kcal: 500 }), it('w3', 'workout', T(20, 0), T(20, 30), { activity: 'swimming', distance_m: 800 }), it('w4', 'workout', T(21, 0), T(21, 10), { activity: 'elliptical', text: 'Elliptical\nsecond' }), it('w5', 'workout', T(5, 0), T(5, 20), { activity: 'yoga' })];
  check('a workout line has only what was counted (the phone\'s words for one it does not know), at most four are listed and the rest are counted, and the steps are said here only when the day has no way he went',
    same(lines(day(runs, { summary: { steps: 4321, resting_hr: null, hrv: null } }), 'workout'), ['运动|2 时 30 分', `瑜伽: 05:00 · 20 分//步行: 07:00 · 30 分 · 2.0 公里 · 15'00"/公里//骑车: 18:00 · 1 时 · 20.0 公里 · 20.0 公里/时 · 500 千卡//游泳: 20:00 · 30 分 · 800 米 +1`, '步数 4,321'])
    && same(lines(day([runs[4], it('m1', 'move', T(8, 0), T(8, 30), { activity: 'walking' })], { summary: { steps: 4321, resting_hr: null, hrv: null } }), 'workout'), ['运动|20 分', '瑜伽: 05:00 · 20 分']));
  const many = day([it('w', 'work', T(10, 0), T(14, 0), { active_s: 12000, projects: Array.from({ length: 7 }, (_, i) => ({ name: `P${i}`, seconds: 1000 + i * 100 })), apps: [{ name: '', seconds: 5 }, { name: 'Z', seconds: 0 }] }), it('w2', 'work', T(15, 0), T(16, 0), { projects: [{ name: 'P0', seconds: 600 }] })]);
  check('the Mac: at most five projects (the biggest first, the same name in two blocks is one), no list for apps there are none of, and no line for sessions where there are none',
    (c => c.parts.length === 1 && c.parts[0].groups.length === 1 && c.parts[0].groups[0].bars.map(b => b.name).join() === 'P0,P6,P5,P4,P3' && c.value === '10:00–16:00 · 活跃 4 时 20 分 · 共 5 时')(cardOf(many, 'work', zh)));
  const calls = day([it('c1', 'call', T(10, 0), T(10, 20), { app: 'FaceTime' }), it('c2', 'call', T(11, 0), T(11, 5), { app: 'WeChat' }), it('c3', 'call', T(12, 0), T(12, 40)), it('c4', 'call', T(13, 0), T(13, 10), { app: 'FaceTime' })]);
  check('calls by app, the longest first; one with no app is 其他', same(lines(calls, 'call'), ['通话|1 时 15 分', '通话 4 次/最长 40 分 · 12:00', '其他: 1 次 · 40 分//FaceTime: 2 次 · 30 分//WeChat: 1 次 · 5 分']));
  check('with her: a talk has a topic where it was marked with words (the brain\'s own "talk 15:00" is not words); the lines in all only where the talks say how many',
    same(lines(day([it('t1', 'talk', T(9, 0), T(9, 5), { turns: 3 }, 'her'), it('t2', 'talk', T(15, 0), T(15, 5), {}, 'her')], { highlights: [{ id: 't1', label: '周末去哪', by: 'him' }, { id: 't2', label: 'talk 15:00', by: 'him' }] }), 'talk'), ['和她|2 次', '共 3 句/时段 09:00–15:05', '周末去哪: 09:00 · 3 句'])
    && same(lines(day([it('t2', 'talk', T(15, 0), T(15, 5), {}, 'her')]), 'talk'), ['和她|1 次', '时段 15:00–15:05']));
  const pay = (id, h, amount, currency, direction, who, text) => it(id, 'payment', T(h, 0), T(h, 0), { amount, currency, direction, ...who && { counterparty: who }, ...text && { text } });
  check('money: what went out and what came in in each currency (the currency he paid in most first), how many payments there were, and the three it went to most (a payment with no amount is counted, not added)',
    same(lines(day([pay('p1', 9, '12.4', 'CAD', 'out', 'A&W'), pay('p2', 10, 88, 'CNY', 'out', '', '午饭'), pay('p3', 11, '30', 'CAD', 'in', 'Sam'), pay('p4', 12, '5', 'CAD', 'out', 'A&W'), pay('p5', 13, '7', 'CAD', 'out', 'Cafe'), pay('p6', 14, '1', 'CAD', 'out', 'Kiosk'), pay('p7', 15, 'x', 'CAD', 'out', 'Bad')]), 'payment'),
      ['花|$25.40…', '支出 $25.40 · ¥88.00/收入 $30.00/笔数 7 笔', '午饭: ¥88.00//A&W: $17.40//Cafe: $7.00']));
  const pics = Array.from({ length: 8 }, (_, i) => it(`pic${i}`, 'picture', T(9 + i, 0), T(9 + i, 0), { attachment: i === 2 ? 'zz' : String(i).repeat(32), note: `n${i}` }));
  check('photos: the first six that can be shown, with the rest counted (one whose picture is not kept is counted, not shown); with none that can be shown the card is only its head',
    same(lines(day(pics), 'picture'), ['照片|8', 'pictures 0@09:00,1@10:00,3@12:00,4@13:00,5@14:00,6@15:00 +1']) && same(lines(day([it('pic1', 'picture', T(9, 0), T(9, 0), { attachment: 'zz' })]), 'picture'), ['照片|1']));
  check('where a move went and a ride is where its row words it', fromTo(fz, fz.items.find(x => x.id === 'move-1'), zh) === '家 → A&W' && fromTo(fz, fz.items.find(x => x.id === 'move-2'), zh) === '去 A&W' && fromTo(fz, fz.items.find(x => x.id === 'ride-1'), zh) === 'Shelbourne at Pear → UVic Exchange'
    && fromTo(fe, fe.items.find(x => x.id === 'move-2'), en) === 'To A&W' && fromTo(fz, { ...fz.items.find(x => x.id === 'ride-1'), board_stop: '', alight_stop: null }, zh) === '');
  const win = { a: T(10, 0), b: T(14, 0) }, by = counted(fz, win);
  check('what the chips, a range and a card count is counted once (counted): the range of 10:00 to 14:00 has the Mac block at 10:30 and not the one at 19:35, the talk at 10:02, the drive and the walks, the last of them only up to 14:00',
    by.work.map(x => x.it.id).join() === 'work-1' && by.talk.map(x => x.it.id).join() === 'talk-1' && by.trip.map(x => x.it.id).join() === 'move-1,move-2,move-3' && by.picture.map(x => x.it.id).join() === 'picture-1' && by.payment.map(x => x.it.id).join() === 'pay-1' && by.sleep.length === 0
    && by.trip.find(x => x.it.id === 'move-3').lap === 5 * 60_000 && by.trip.find(x => x.it.id === 'move-1').lap === 100 * 60_000 && numbers(fz, zh, win).map(n => `${n.label} ${n.value}`).join() === '路上 1:55,Mac 0:42,和她 1 次,花 $12.40,照片 1');
}

// ---------- the star, in node (ADR 0296): who chose it, and what can be marked ----------
{
  const day = (hl, items) => buildDay(small(items ?? [it('s-1', 'stay', T(9, 0), T(11, 0), { place: 'Cafe Fort, Victoria' }), it('t-1', 'talk', T(10, 10), T(10, 20), { turns: 2 }, 'her'), it('w-1', 'workout', T(7, 0), T(7, 40), { activity: 'running' }), it('c-1', 'code', T(8, 0), T(8, 30), { title: 'x', project: 'p' }), it('p q', 'picture', T(10, 30), T(10, 30), { note: 'x' })], { highlights: hl }), NOW, zh);
  const both = day([{ id: 's-1', label: '一个上午', by: 'her' }, { id: 't-1', label: 'talk 10:10', by: 'him' }, { id: 'w-1', label: '', by: '' }]);
  const stay = both.heads.get('s-1'), talk = both.rows.get('t-1'), run = both.rows.get('w-1');
  check('a star says who chose it: by "her" is her pick (an outline), "him" or anything else his mark (filled); a stay\'s header says it too, and a row nobody marked says neither',
    stay.star === '一个上午' && stay.hers && talk.star && !talk.hers && run.star && !run.hers && !both.rows.get('c-1').star && !both.rows.get('c-1').hers && !both.rows.get('p q').hers && both.stars.get('s-1').by === 'her' && both.stars.get('t-1').by === 'him');
  const bar = buildBar(both, zh);
  check('the bar\'s lit star keeps who chose it: his filled, hers outlined', bar.stars.length === 3 && bar.stars.find(x => x.id === 's-1').hers === true && bar.stars.find(x => x.id === 't-1').hers === false && bar.stars.find(x => x.id === 'w-1').hers === false, JSON.stringify(bar.stars));
  const o = id => openOf(both, id, zh);
  check('every item the brain can mark opens with a star (a stay, a talk, a run, a session, a commitment with its @ id); one with an id it would refuse (a space) has none; the folded row of sessions is no one item',
    ['s-1', 't-1', 'w-1', 'c-1'].every(k => o(k).star) && o('p q').star === false && openOf(buildDay(small([it('commitment-ab@2026-10-19', 'event', T(22, 0), T(23, 0), { title: 'x' })]), NOW, zh), 'commitment-ab@2026-10-19', zh).star
    && Object.values(fullDayOpen()).every(x => x === true), JSON.stringify(fullDayOpen()));
  function fullDayOpen() { const f = buildDay(fullDay(), NOW, zh); return Object.fromEntries([...f.rows.keys(), ...f.heads.keys()].map(k => [k, k.startsWith('code:') ? !openOf(f, k, zh).star : openOf(f, k, zh).star])); }
  check('what the brain sends of the highlights is read field by field: an id is needed, a label that is not text is none, a `by` that is not text is none', tidyReply({ ...makeDay(), highlights: [{ id: 'a', label: 7, by: 'her' }, { label: 'x' }, { id: 'b', label: 'y', by: 3 }, null] }).highlights.map(h => `${h.id}:${h.label}:${h.by}`).join() === 'a::her,b:y:');
}

// ---------- what mail said, in node (ADR 0274, 0296): the two buttons are there only where the route works ----------
{
  const mk = (id, extra) => it(id, 'event', T(22, 10), T(22, 50), { title: '组会', place: 'Library', private: false, ...extra });
  const day = (item, t = zh, local) => buildDay(small([item]), NOW, t, local), openIt = (item, local) => openOf(day(item, zh, local), item.id, zh), row = (item, t = zh, local) => day(item, t, local).rows.get(item.id);
  const un = mk(CM.a, { source: 'mail', confirmed: false }), you = { you: new Set([CM.a]) }, going = { leaving: new Set([CM.a]) }, sure = { ...un, confirmed: true };
  check('an event mail noted that is not his yet has the two buttons, and what the brain takes is the hex of its id; the row opens to them even with nothing to ask',
    openIt(un).commit === HEX.a && hasBody({ ...openIt(un), parts: [], rename: null, ack: null, summarize: false, star: false }, false));
  check('none for one mail has confirmed, one he said himself, a calendar event, an event whose id is no commitment\'s (or not a whole hex), a commitment that is on its way out',
    [sure, mk(CM.a, { source: 'said', confirmed: true }), mk(CM.a, {}), mk('event-1', { source: 'mail', confirmed: false }), mk('commitment-ab@2026-10-19', { source: 'mail', confirmed: false })].every(x => openIt(x).commit === null) && openIt(un, going).commit === null);
  check('it waits on him (the badge, the count) and its second line says it is not confirmed; once he has confirmed it neither does, and the line says he did; one on its way out waits no longer',
    row(un).wait && row(un).detail === '邮件 · 还没确认 · Library' && !row(sure, zh, you).wait && row(sure, zh, you).detail === '邮件 · 你确认了 · Library' && !row(un, zh, going).wait && row(un, zh, going).leaving && !row(un).leaving
    && day(un).waiting.join() === CM.a && day(sure, zh, you).waiting.length === 0 && day(un, zh, going).waiting.length === 0, JSON.stringify([row(un), row(sure, zh, you), row(un, zh, going)].map(r => [r.wait, r.detail, r.leaving])));
  check('the open row of one he has confirmed says confirmed, in English the line says you confirmed it, and a private one says no more than before',
    openIt(sure, you).parts[0].lines[0].text.includes('已确认') && row(sure, en, you).detail === 'Mail · you confirmed it · Library' && row({ ...sure, private: true }, zh, you).detail === '');
  check('on the bar the box of one on its way out knows it, so it can fade; the others do not', buildBar(day(un, zh, going), zh).sched.find(x => x.key === CM.a).leaving === true && buildBar(day(un), zh).sched.find(x => x.key === CM.a).leaving === false);
  check('what the pane lays over an item is never read from the brain: a `you` or a `leaving` in a day it sends changes nothing',
    tidy({ id: 'x', kind: 'event', start_ms: T(9), end_ms: T(10), you: true, leaving: true }).you === undefined && tidy({ id: 'x', kind: 'event', start_ms: T(9), end_ms: T(10), you: true, leaving: true }).leaving === undefined
    && row({ ...un, you: true, leaving: true }).leaving === false && row({ ...un, you: true, leaving: true }).detail === '邮件 · 还没确认 · Library');
}

// ---------- the change a place write answers (ADR 0234): a string, which the rename's 撤销 is shown from ----------
check('the change a place write answers is read as the string it is (an id inside an object is read too), and none when nothing changed',
  changeId('place-change:ab') === 'place-change:ab' && changeId({ id: 'chg-1' }) === 'chg-1' && changeId(null) === null && changeId('') === null && changeId({ id: 3 }) === null);

// ---------- naming a place, in node (ADR 0296): where the offer is, and what the brain's answers are read as ----------
{
  const m = buildDay(nameDay(), NOW, zh), o = id => openOf(m, id, zh);
  check('a stay that is no place he keeps, with the point the phone had, opens to the naming at that point; a kept place opens to the rename instead; a stay with no point and a stop on the way have neither',
    JSON.stringify(o('stay-aw').name) === '{"lat":48.4284,"lng":-123.3656}' && o('stay-aw').rename === null && JSON.stringify(o('stay-spot').name) === '{"lat":48.4631,"lng":-123.3123}' && o('stay-uvic').name === null && o('stay-uvic').rename.name === 'UVic'
    && o('stay-bare').name === null && m.rows.has('stay-stop') && !m.heads.has('stay-stop') && o('stay-stop').name === null, JSON.stringify(['stay-aw', 'stay-spot', 'stay-uvic', 'stay-bare', 'stay-stop'].map(k => o(k).name)));
  check('the offer is what opens a stay that has no address to say (a spot the phone had no words for): without it that row would open to nothing', o('stay-spot').parts.length === 0 && hasBody({ ...o('stay-spot'), star: false }, false) && !hasBody({ ...o('stay-spot'), star: false, name: null }, false));
  check('a point off the globe is not offered, and lat and lng are read like the other numbers (a text is none)',
    openOf(buildDay(small([it('s', 'stay', T(9), T(11), { lat: 91, lng: 0 })]), NOW, zh), 's', zh).name === null && tidy({ id: 's', kind: 'stay', start_ms: T(9), end_ms: T(10), lat: '48.4', lng: null }).lat === undefined && tidy({ id: 's', kind: 'stay', start_ms: T(9), end_ms: T(10), lat: -33.9, lng: 151.2 }).lng === 151.2);
  check('what the brain answers to a place write is read: the place by its id and name (a role it does not say is none)',
    JSON.stringify(keptOf({ id: 'place:ab', name: 'A&W', role: 'other', radius_m: 100 })) === '{"id":"place:ab","name":"A&W","role":"other"}' && keptOf({ id: 'p', name: 'x' }).role === '' && keptOf({ name: 'x' }) === null && keptOf(null) === null);
}

// ---------- saying what a stretch was, in node (ADR 0302): which rows offer it, the range it holds, and the body that is sent ----------
{
  const m = buildDay(fullDay(), NOW, zh), held = key => { const x = sayOf(m, key); return x && `${x.range}|${x.a}|${x.b}`; }, want = (a, b) => `${clock(a, TZ)}–${b === T(24) ? '24:00' : clock(b, TZ)}|${a}|${b}`;
  const alone = (list, key) => sayOf(buildDay(small(list), NOW, zh), key);
  check('a stay, a way he went, the night, work, a call, a run, a Claude chat and a session each hold the range they cover, its own start and end (the night is cut to the day: 00:00 to 07:00)',
    held('stay-home-1') === want(T(7, 0), T(11, 25)) && held('move-1') === want(T(11, 25), T(13, 5)) && held('ride-2') === want(T(18, 15), T(18, 50)) && held('sleep-1') === want(T(0), T(7, 0))
    && held('work-2') === want(T(19, 35), T(21, 40)) && held('call-1') === want(T(16, 40), T(16, 58)) && held('workout-1') === want(T(7, 20), T(7, 55)) && held('chat-1') === want(T(20, 10), T(20, 42)) && held('code-s5') === want(at4(1540), at4(1630)),
    ['stay-home-1', 'move-1', 'ride-2', 'sleep-1', 'work-2', 'call-1', 'workout-1', 'chat-1', 'code-s5'].map(held).join('  '));
  check('one still going ends at now (the stay since 18:50 holds 18:50 to 21:50) and one that is set to end later than now is cut to it; the day it is for is the shown day\'s date',
    held('stay-home-3') === want(T(18, 50), NOW) && sayOf(m, 'stay-home-3').day === DATE && alone([it('w', 'work', T(21, 0), T(22, 30))], 'w').range === '21:00–21:50');
  check('not her rows (a reminder, a talk, or any row of hers), not a point (a picture, a payment, a bus to catch), not an event (mail\'s or his own), not the letter on his Claude chats, not the folded row of sessions (no one item), not a row that is not there',
    ['reminder-1', 'reminder-2', 'talk-1', 'talk-2', 'picture-1', 'pay-1', 'departure-1', 'event-1', 'event-2', 'claude-day-1', 'code:stay-home-1', 'nothing'].every(k => sayOf(m, k) === null)
    && alone([it('h', 'move', T(10, 0), T(11, 0), { activity: 'walking' }, 'her')], 'h') === null);
  check('nothing still to come and nothing under a minute: a stay that begins after now, a walk that began 30 s ago, a stay of 59 s are none; a stay of a minute is one',
    alone([it('a', 'stay', T(22, 30), null, { place: 'X' })], 'a') === null && alone([it('b', 'move', NOW - 30_000, null, { activity: 'walking' })], 'b') === null && alone([it('c', 'stay', T(10, 0), T(10, 0) + 59_000, { place: 'X' })], 'c') === null
    && alone([it('d', 'stay', T(10, 0), T(10, 1), { place: 'X' })], 'd').range === '10:00–10:01');
  const past = buildDay(small([it('n', 'sleep', T(23, 0), T(7, 0, 20)), it('r', 'stay', T(9, 0), T(10, 0), { place: 'X' })]), T(8, 0, 20), zh);
  check('on a day that has ended the range is cut to the day, not to now: a night that goes on past midnight ends at 24:00, still that day\'s date',
    sayOf(past, 'n').range === '23:00–24:00' && sayOf(past, 'n').b === T(24) && sayOf(past, 'n').day === DATE && sayOf(past, 'r').range === '09:00–10:00');
  check('a dragged range is cut the same way, to the day and to now; one that is all still to come, or under a minute, holds none',
    sayRange(m, T(14, 0), T(18, 0)).range === '14:00–18:00' && sayRange(m, T(14, 0), T(23, 0)).range === '14:00–21:50' && sayRange(m, T(14, 0), T(23, 0)).b === NOW && sayRange(m, T(14, 0), T(23, 0)).day === DATE
    && sayRange(m, T(0), T(24)).range === '00:00–21:50' && sayRange(m, T(22, 0), T(23, 0)) === null && sayRange(m, T(14, 0), T(14, 0)) === null && sayRange(m, T(14, 0), T(14, 0) + 59_000) === null);
  const said = sayRange(m, T(14, 10), T(14, 50)), body = text => answerOf(said, text);
  check('the body is the day\'s date, the range in whole ms and his words trimmed, in that order and nothing else', JSON.stringify(body('  躺着休息 \n')) === `{"day":"${DATE}","start_ms":${T(14, 10)},"end_ms":${T(14, 50)},"text":"躺着休息"}`, JSON.stringify(body('  躺着休息 ')));
  check('nothing is sent for nothing (empty, blanks, a line break) or for more than 200 characters; 200 is fine, counted by character as the brain counts (200 emoji are 200), 201 is not',
    [body(''), body('   '), body('\n\t'), body('字'.repeat(201)), body('😀'.repeat(201))].every(x => x === null) && body('字'.repeat(200)).text.length === 200 && body('😀'.repeat(200)).text === '😀'.repeat(200) && body(` ${'字'.repeat(200)} `).text === '字'.repeat(200));
  const odd = sayOf(buildDay(small([it('o', 'stay', T(10, 0) + 400.4, T(11, 0) + 999.6, { place: 'X' })]), NOW, zh), 'o');
  check('times are whole ms (a fraction is rounded), as the route takes them', Number.isInteger(odd.a) && Number.isInteger(odd.b) && odd.a === T(10, 0) + 400 && odd.b === T(11, 0) + 1000, JSON.stringify(odd));
}

// ---------- the built page ----------
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(web), '--strictPort'], { cwd: root, stdio: 'ignore' });
const browser = await chromium.launch({ headless: true, channel: 'chrome', args: ['--disable-web-security', '--mute-audio'] });
const errors = [];
const NOTCH_R = 412.5, TOP = 32;
// Every Web Animation the page starts is written down (what, on what, with which keyframes and timing), so what was asked for can be read.
const record = () => {
  const animate = Element.prototype.animate;
  window.__anims = [];
  Element.prototype.animate = function (kf, opt) {
    window.__anims.push({ cls: String(this.className?.baseVal ?? this.className), key: this.dataset?.key ?? null, tag: this.tagName, parent: String(this.parentElement?.className?.baseVal ?? this.parentElement?.className ?? ''), kf: JSON.parse(JSON.stringify(kf)), opt: typeof opt === 'number' ? { duration: opt } : JSON.parse(JSON.stringify(opt ?? {})), at: Math.round(performance.now()) });
    return animate.call(this, kf, opt);
  };
};
const anims = a => a.page.evaluate(() => window.__anims.slice());
// A page on a fake daemon: `served` is what /inherent/day answers, `board` the sessions, `departure` the pinned trip, `hits` the day reads it got.
async function boot({ wing, marks, lang = 'zh', openBy, board = [], served = makeDay(), departure = null, init = null, at = NOW, fastPolls = false, notchWidth = 185 } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 640, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  // `posts`: what the page sent (path and body); `refuse`: routes that answer with an error; `changes`: place renames the daemon can undo; `lag`: how long
  // the writes take to answer (ms); `hidden`: the picks of hers he has taken off.
  const s = { board, served, departure, hits: [], pictures: [], posts: [], reads: [], refuse: new Set(), changes: new Map(), lag: 0, hidden: new Set(), records: records(), controls: { mic_muted: false, speech_muted: false, conversation: false, quiet: 'off' } };
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|net::ERR/.test(m.text())) errors.push(m.text()); });
  await page.addInitScript(([wardrobe, settings, notchWidth]) => {
    if (wardrobe) localStorage.setItem('companion-wardrobe-v1', JSON.stringify(wardrobe));
    localStorage.setItem('companion-settings-v1', JSON.stringify(settings));
    window.__audio = 0;
    window.jarvis = {
      placement: async () => ({ docked: false, topInset: 32, notchWidth, surfaceWidth: 640, compactWidth: 0, displayId: 1 }),
      onPlacement: () => () => {}, onDisplayLeave: () => () => {}, displayReady: () => {}, companionSettings: () => {}, wearing: () => {}, onDictation: () => () => {},
      onCursor: callback => { window.__cursor = callback; return () => {}; }, onCommand: () => () => {},
      passthrough: () => {}, focus: async on => { (window.__focus ??= []).push(on); }, material: () => {}, codexTitles: async () => ({}),
      watchGhostty: () => {}, onGhostty: () => () => {}, onAgentsPresence: cb => { window.__presence = cb; return () => {}; }, onMouseDown: cb => { window.__down = cb; return () => {}; }, onClaudeFront: cb => { window.__front = cb; return () => {}; }, plugins: async () => ({}),
    };
    window.__sockets = [];
    window.WebSocket = class { constructor() { window.__sockets.push(this); setTimeout(() => this.onopen?.(), 0); } send() {} close() {} };
    window.__emit = (op, payload) => window.__sockets.at(-1).onmessage({ data: JSON.stringify({ op, payload }) });
  }, [wing || marks ? { ...wing && { wing }, ...marks && { marks } } : null, { lang, ...(openBy ? { openBy } : {}) }, notchWidth]);
  if (init) await page.addInitScript(init, NOW);
  await page.route(`${daemon}/**`, async route => {
    const url = new URL(route.request().url());
    const json = value => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(value) });
    if (url.pathname === '/inherent/day') {
      s.hits.push(url.search);
      const date = url.searchParams.get('date');
      // `dayLag`: how long the days other than today take to come (ms)
      if (s.dayLag && date && date !== DATE) await new Promise(r => setTimeout(r, s.dayLag));
      // `days` / `days-404`: the days other than today answer with an error, or with a 404 (a brain that does not serve them)
      if (date && date !== DATE && (s.refuse.has('days') || s.refuse.has('days-404'))) return route.fulfill({ status: s.refuse.has('days-404') ? 404 : 500, contentType: 'application/json', body: '{"detail":"no"}' });
      return json(!date || date === DATE ? s.served : date === YESTERDAY ? yesterday() : { ...yesterday(), date, items: [] });
    }
    if (url.pathname.startsWith('/inherent/attachments/')) {
      s.pictures.push(url.pathname);
      return url.pathname.endsWith(`/${PIC}`) ? route.fulfill({ status: 200, contentType: 'image/png', body: PICTURE }) : url.pathname.endsWith(`/${BIG}`) ? route.fulfill({ status: 200, contentType: 'image/png', body: BIGPICTURE }) : route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"gone"}' });
    }
    // what the page writes: the routes the brain has today for a rename, its undo, an acknowledgement and a turn about a row
    const req = route.request(), post = req.method() === 'POST', refused = (why, status = 500) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ detail: why }) });
    const sent = () => { try { return JSON.parse(req.postData() ?? 'null'); } catch { return null; } };
    if (post && s.lag) await new Promise(r => setTimeout(r, s.lag));
    const rename = (place, name) => { for (const x of s.served.items) if (x.kept_place?.id === place) x.kept_place = { ...x.kept_place, name }; };
    if (post && url.pathname === '/inherent/memory/places/undo') {
      const body = sent(); s.posts.push({ path: url.pathname, body });
      const was = s.changes.get(body?.change_id);
      if (s.refuse.has('undo') || !was) return refused('no such change', 404);
      rename(was.place, was.from); return json({ ok: true });
    }
    // ADR 0296: a place kept at a point he gives. As the brain does it: a name of 1 to 40 characters (spaces collapsed), a role of home, school or other (none is other), a point
    // on the globe; a name that is kept already is refused (400). The answer is the place and the change as the string it is; every stay within 100 m of the point is in the place from then on.
    if (post && url.pathname === '/inherent/memory/places') {
      const body = sent(); s.posts.push({ path: url.pathname, body });
      const name = String(body?.name ?? '').split(/\s+/).filter(Boolean).join(' ');
      if (s.refuse.has('place') || !name || name.length > 40 || body.role != null && !['home', 'school', 'other'].includes(body.role) || !(Math.abs(body.lat) <= 90 && Math.abs(body.lng) <= 180)) return refused('refused', 400);
      if (s.served.items.some(x => x.kept_place?.name.toLowerCase() === name.toLowerCase())) return refused('that name is kept already', 400);
      const made = (s.made = (s.made ?? 0) + 1).toString(16).padStart(12, '0'), place = { id: `place:${made}`, name, role: body.role ?? 'other', radius_m: 100, has_centre: true, address: null, origin: 'page' };
      for (const x of s.served.items) if (x.kind === 'stay' && !x.kept_place && Math.hypot((x.lat - body.lat) * 111_000, (x.lng - body.lng) * 74_000) <= 100) x.kept_place = { id: place.id, name, role: place.role };
      return json({ ok: true, place, change: `place-change:${made}` });
    }
    if (post && url.pathname.startsWith('/inherent/memory/places/')) {
      const body = sent(), place = decodeURIComponent(url.pathname.split('/').pop()); s.posts.push({ path: url.pathname, body });
      if (s.refuse.has('rename')) return refused('refused');
      const was = s.served.items.find(x => x.kept_place?.id === place)?.kept_place?.name, change = `chg-${s.changes.size + 1}`;
      s.changes.set(change, { place, from: was }); rename(place, body.name);
      return json({ ok: true, place: { id: place, name: body.name }, change });
    }
    if (post && url.pathname.startsWith('/inherent/notices/')) {
      const body = sent(), id = decodeURIComponent(url.pathname.split('/').pop()), item = s.served.items.find(x => x.id === id); s.posts.push({ path: url.pathname, body });
      if (s.refuse.has('notices') || !item?.fired) return refused('not a notice', 404);
      item.acknowledged = true; return json({ ok: true });
    }
    // ADR 0296: what mail said he has to go to. As the brain does it: the path's hex is the id's between `commitment-` and `@`; a commitment no one has is a 404, a
    // confirm of one confirmed already a 409, a cancel drops it. `commitments` refuses the whole route.
    if (post && url.pathname.startsWith('/inherent/commitments/')) {
      const body = sent(), hex = decodeURIComponent(url.pathname.split('/').pop()); s.posts.push({ path: url.pathname, body });
      if (s.refuse.has('commitments')) return refused('refused', 502);
      const item = s.served.items.find(x => x.id.startsWith(`commitment-${hex}@`));
      if (!item) return refused('no such commitment', 404);
      if (body?.action === 'confirm') { if (item.confirmed) return refused('it is confirmed already', 409); item.confirmed = true; return json({ ok: true }); }
      if (body?.action !== 'cancel') return refused('bad action', 422);
      s.served.items = s.served.items.filter(x => x !== item); return json({ ok: true });
    }
    // ADR 0296: his mark on an item of a day. As the brain does it: his marks first (five at most, a sixth is a 409; marking what he marked answers 200), her picks
    // in the room he leaves, one he took off is hidden; an id no item has is a 404. The answer is the day's highlights after it.
    if (post && url.pathname === '/inherent/day/highlight') {
      const body = sent(); s.posts.push({ path: url.pathname, body });
      if (s.refuse.has('highlight')) return refused('refused', 502);
      const item = s.served.items.find(x => x.id === body?.id), list = s.served.highlights ?? [], his = list.filter(h => h.by !== 'her'), standing = his.find(h => h.id === body?.id);
      if (!item) return refused('nothing on that day has that id', 404);
      if (body.on && !standing && his.length >= 5) return refused('5 highlights of that day are marked; unmark one first', 409);
      const stamp = `${item.kind} ${clock(item.start_ms, TZ)}`, mine = body.on ? [...his.filter(h => h.id !== body.id), { id: body.id, label: body.label ?? standing?.label ?? list.find(h => h.id === body.id)?.label ?? stamp, by: 'him' }] : his.filter(h => h.id !== body.id);
      if (body.on) s.hidden.delete(body.id); else s.hidden.add(body.id);
      const hers = list.filter(h => h.by === 'her' && h.id !== body.id && !s.hidden.has(h.id)).slice(0, Math.max(0, 5 - mine.length));
      const at = id => s.served.items.find(x => x.id === id)?.start_ms ?? 0;
      s.served.highlights = [...mine, ...hers].sort((x, y) => at(x.id) - at(y.id));
      return json({ ok: true, highlights: s.served.highlights });
    }
    // ADR 0302: his words for a range of a day. As the brain does it: the shown day's date, whole ms with the end after the start, the range inside the day and up to now,
    // touching an item the day serves, his words trimmed to 1 to 200 characters (else a 422); `answer` refuses the route (a 502, it cannot write). The answer is the
    // span written (basis stated, his words as its title); the day is served with it from then on.
    if (post && url.pathname === '/inherent/day/answer') {
      const body = sent(), day = s.served, text = String(body?.text ?? '').trim(); s.posts.push({ path: url.pathname, body });
      if (s.refuse.has('answer')) return refused('it cannot be written', 502);
      if (body?.day !== day.date || !Number.isInteger(body.start_ms) || !Number.isInteger(body.end_ms) || body.end_ms <= body.start_ms || body.start_ms < day.start_ms || body.end_ms > day.end_ms || body.end_ms > at
        || !day.items.some(x => x.start_ms < body.end_ms && (x.end_ms ?? at) > body.start_ms) || !text || [...text].length > 200) return refused('not a range of that day', 422);
      const span = { ...sp(`d-said-${s.posts.length}`, 0, null), start_ms: body.start_ms, end_ms: body.end_ms, title: text, category: 'unknown', basis: 'stated' };
      (day.spans ??= []).push(span);
      return json({ ok: true, span });
    }
    if (post && url.pathname === '/inherent/submit') {
      s.posts.push({ path: url.pathname, body: sent() });
      return s.refuse.has('submit') ? refused('refused', 422) : json({ turn_id: `turn-${s.posts.length}` });
    }
    if (url.pathname.startsWith('/inherent/memory/day/') && url.pathname.endsWith('/records')) {
      s.reads.push(url.pathname + url.search);
      if (s.refuse.has('records')) return refused('down');
      const offset = Number(url.searchParams.get('offset') ?? 0), limit = Number(url.searchParams.get('limit') ?? 120);
      return json({ day: url.pathname.split('/')[4], total: s.records.length, offset, records: s.records.slice(offset, offset + limit) });
    }
    if (url.pathname === '/inherent/controls') return json(s.controls);
    if (url.pathname === '/inherent/claude-sessions') return json({ sessions: s.board, error: null });
    if (url.pathname === '/inherent/notices' && s.departure) return json({ notices: [], departure: s.departure });
    if (url.pathname === '/inherent/agent-marks') return json({ marks: {} });
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
  });
  await page.clock.setFixedTime(new Date(at));
  // The daemon's polls (30 s, 60 s) come every 300 ms, so a change on the daemon reaches an open pane at once (after the clock, which wraps the timers too).
  if (fastPolls) await page.addInitScript(() => { const timeout = window.setTimeout; window.setTimeout = (fn, ms, ...rest) => timeout(fn, ms === 30000 || ms === 60000 ? 300 : ms, ...rest); });
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

  // The pinned trip keeps its place after the line, and a rest or a click on it never brings the day (ONLY=pin runs it alone).
  if (all || only === 'pin') {
    const trip = { id: 'dep-1', route: '27', board_stop: 'Shelbourne at Pear', leave_at_ms: NOW + 12 * 60_000, departs_at_ms: NOW + 15 * 60_000, leave_at: '22:02', departs: '22:05', arrive_at: '22:40', to: 'school', stale: false };
    const a = await boot({ departure: trip });
    await a.until(() => { const n = document.querySelector('.notch'); return n?.dataset.marks === 'line pin' && n.dataset.line === '家 · 3:00' && n.dataset.pin; }, undefined, 8000);
    // The wing grows on a spring: read it once it has reached the room the pill needs, not on the frame the pill arrives (it is 150 ms from there to its width).
    const seg = await a.lineX(), r = await seen(a, v => v.hit.right >= seg.x1 + 5 + 60, 8000), pill = { x: seg.x1 + 5 + 14, y: 14 };
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

  // ---- a screen without a notch (live 2026-10-19 on his second screen): the pill stays out of sight until the pointer comes near; the line
  // beside it and the day hanging from it count as near, so neither goes from under the pointer (ONLY=pill) ----
  if (all || only === 'pill') {
    const a = await boot({ notchWidth: 0 });
    const stowed = () => a.page.evaluate(() => document.querySelector('.companion').classList.contains('is-stowed'));
    await line(a);
    check('on a screen without a notch she is out of sight while the pointer is away', await stowed());
    const seg = await a.lineX(), end = { x: seg.x1 - 4, y: 14 };
    // the pill is 132 wide at the stage's centre (320); its own approach reaches 40 past its right edge
    check('the line runs past where the pill\'s own approach ends', end.x > 320 + 66 + 40, JSON.stringify(seg));
    await a.glide(a.far, end, 300);
    await a.page.waitForTimeout(150);
    check('a pointer at the far end of the line brings her into sight', !await stowed());
    let r = await settle(a, 'open');
    await a.page.waitForTimeout(400);
    check('a rest there opens the day, and she stays in sight while the pointer rests on the line', r.tl === 'open' && r.dt && !await stowed(), JSON.stringify(r));
    await a.glide(end, { x: 330, y: 300 }, 300);
    await a.page.waitForTimeout(900);
    r = await a.read();
    check('going down into the day keeps it open and in sight', r.tl === 'open' && r.dropOpen && !await stowed(), JSON.stringify(r));
    // past the pane's right edge (580) for less than its grace (600 ms) but longer than the pill's own (280 ms)
    await a.glide({ x: 330, y: 300 }, { x: 620, y: 300 }, 60);
    await a.page.waitForTimeout(400);
    r = await a.read();
    check('a pointer that slips out of the day for less than its grace finds it open and in sight', r.tl === 'open' && !await stowed(), JSON.stringify(r));
    await a.glide({ x: 620, y: 300 }, { x: 330, y: 300 }, 60);
    await a.page.waitForTimeout(300);
    await a.glide({ x: 330, y: 300 }, a.far, 150);
    r = await settle(a, '', 2500);
    check('leaving folds the day after its grace', r.tl === '');
    await a.page.waitForTimeout(700);
    check('and then she goes out of sight again', await stowed());
    await a.ctx.close();
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
    const before = await seen(a, r => r.hit.right - r.hit.left > 100, 8000);
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
    check('her sentence, in the pane\'s own sans face at 500 (yilun 10-19: the serif read faint)', (await text('.dt-line'))[0].startsWith('今天一半在路上') && await a.page.locator('.dt-line').evaluate(el => { const c = getComputedStyle(el), p = getComputedStyle(el.parentElement); return !/Songti|Serif/.test(c.fontFamily) && c.fontFamily === p.fontFamily && c.fontWeight === '500'; }));
    check('the footnote names what has not been read, as the phone words it: 还没读到：Mac 的时间', (await text('.dt-foot'))[0] === '还没读到：Mac 的时间');
    const heads = (await text('.dt-sh .dt-tx b')).map(x => x.trim());
    check('stay headers, in order, by name', heads.join() === '家,A&W,家,UVic,家');
    const titles = (await text('.dt-row .dt-tx b')).map(x => x.trim());
    check('rows say what happened, by their words', ['和她说话', '照片', '开车', '步行', '27 路', '私事', '给银行打电话', '小组讨论'].every(w => titles.includes(w)), titles.join('|'));
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
    // after a minute the pane opens on today again
    await a.page.evaluate(() => { const now = performance.now.bind(performance); performance.now = () => now() + 61_000; });
    await a.page.waitForTimeout(400);
    await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-say')?.textContent === '这一天没画出来', undefined, 8000);
    check('a day with no usable bounds says so in a line, with the day buttons, and the log hears of it', await a.page.locator('.dt-nav button').first().isEnabled() && told.some(x => /^\[timeline\] the day: the day has no usable bounds/.test(x)) && await her());
    a.page.removeAllListeners('console');
    check('and none of it was an uncaught error', errors.slice(caught).every(x => /boom|The above error|Error: /.test(x)));
    errors.length = caught;
    await a.ctx.close();
  }


  // What the list must hold on any day: rows and headers one under another, none crossing another, nothing between them but a gap of 12 or none,
  // every word inside its row, the heights of the design (40, 52 with a second line, a stay's header 40, the now line 28).
  const audit = a => a.page.evaluate(() => {
    const list = document.querySelector('.dt-list'), kids = [...list.querySelectorAll('.dt-row, .dt-sh, .dt-now')], bad = [], box = el => el.getBoundingClientRect();
    kids.sort((x, y) => box(x).top - box(y).top);
    kids.forEach((el, i) => {
      const r = box(el), name = el.dataset.key ?? el.className, want = el.classList.contains('dt-now') ? 28 : el.classList.contains('has-detail') ? 52 : 40;
      if (Math.abs(r.height - want) > .5) bad.push(`${name} is ${r.height} tall, not ${want}`);
      if (i) { const gap = r.top - box(kids[i - 1]).bottom; if (Math.abs(gap) > .5 && Math.abs(gap - 12) > .5) bad.push(`${name} is ${gap} under the one before`); }
      for (const part of el.querySelectorAll('.dt-at, .dt-ttl, .dt-tx > em, .dt-tm')) { const p = box(part); if (p.left < r.left - .5 || p.right > r.right + .5 || p.top < r.top - .5 || p.bottom > r.bottom + .5) bad.push(`${name} ${part.className} is outside its row`); }
      const tx = el.querySelector('.dt-tx'), tm = el.querySelector('.dt-tm'), at = el.querySelector('.dt-at');
      if (tx && tm && box(tx).right > box(tm).left + .5) bad.push(`${name}: the words run into the right column`);
      if (at && at.scrollWidth > at.clientWidth + 1 || tm && tm.scrollWidth > tm.clientWidth + 1) bad.push(`${name}: a time or a length is cut`);
      const ttl = el.querySelector('.dt-ttl'), em = el.querySelector('.dt-tx > em'); if (ttl && em && box(ttl).bottom > box(em).top + .5) bad.push(`${name}: the title runs into its second line`);
    });
    return { n: kids.length, bad };
  });
  const openDay = async a => {
    await a.until(() => document.querySelector('.notch')?.dataset.line);
    const seg = await a.lineX();
    await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-row, .notch-drop.is-open .dt-say'), undefined, 8000);
    await a.page.waitForTimeout(800);
  };
  const text = (a, sel) => a.page.locator(sel).allInnerTexts();
  const rect = (a, sel) => a.page.locator(sel).first().evaluate(el => el.getBoundingClientRect().toJSON());

  // ---- the list: every row has the same anatomy, on any day ----
  if (all || only === 'list') {

    {
      const a = await boot({ served: richDay() });
      await openDay(a);
      const au = await audit(a);
      check('the main day: no row crosses another, every word is inside its row, rows are 40 or 52 tall, headers 40, the now line 28, units 12 apart', au.n >= 25 && au.bad.length === 0, au.bad.join('; '));
      const geo = await a.page.evaluate(() => {
        const ics = [...document.querySelectorAll('.dt-row .dt-ic')].map(e => e.getBoundingClientRect()), dots = [...document.querySelectorAll('.dt-sh .dt-dot')].map(e => e.getBoundingClientRect());
        const ats = [...document.querySelectorAll('.dt-row .dt-at')].map(e => e.getBoundingClientRect().right), cx = r => r.left + r.width / 2;
        const rail = getComputedStyle(document.querySelector('.dt-sh.has-rows'), '::after'), railRow = getComputedStyle(document.querySelector('.dt-in .dt-row'), '::before');
        const head = document.querySelector('.dt-sh.has-rows').getBoundingClientRect();
        return { icW: new Set(ics.map(r => r.width)).size === 1 && ics[0].width, icX: new Set(ics.map(r => Math.round(cx(r) * 2))).size, dotX: dots.every(d => Math.abs(cx(d) - cx(ics[0])) < 1), dotW: new Set(dots.map(d => d.width)).size === 1 && dots[0].width,
          atX: new Set(ats.map(x => Math.round(x * 2))).size, railW: rail.width, railBg: rail.backgroundColor, railRowW: railRow.width, railLeft: head.left + parseFloat(rail.left) + parseFloat(rail.width) / 2 - cx(ics[0]) };
      });
      check('the columns line up: the time right-aligned at one edge, every glyph 28 across at one centre, the stay\'s 8 px dot on that centre, its rail 2 px in its hue at .35 on it too', geo.icW === 28 && geo.icX === 1 && geo.dotX && geo.dotW === 8 && geo.atX === 1 && geo.railW === '2px' && geo.railBg === 'rgba(255, 168, 150, 0.35)' && geo.railRowW === '2px' && Math.abs(geo.railLeft) < 1.2, JSON.stringify(geo));
      const ends = await a.page.evaluate(() => {
        const q = key => document.querySelector(`[data-key="${key}"]`), pseudo = (el, w = '::before') => getComputedStyle(el, w), half = el => el.getBoundingClientRect().height / 2;
        const lastIn = q('work-1'), mid = q('reminder-2'), ride = q('ride-1'), after = q('talk-3');
        return { lastEnd: pseudo(lastIn).bottom === `${half(lastIn)}px`, midFull: pseudo(mid).bottom === '0px' && pseudo(mid).top === '0px', tripTop: pseudo(ride).top === `${half(ride)}px` && pseudo(ride).bottom === '0px', tripEnd: pseudo(after).bottom === `${half(after)}px` && pseudo(after).top === '0px',
          dashed: /repeating-linear-gradient/.test(pseudo(ride).backgroundImage) && /repeating-linear-gradient/.test(pseudo(after).backgroundImage), cls: [ride.className, after.className] };
      });
      check('a rail ends at the last glyph: the stay\'s at its last row, the trip\'s dashed from the ride\'s glyph to the talk on it', ends.lastEnd && ends.midFull && ends.tripTop && ends.tripEnd && ends.dashed, JSON.stringify(ends));
      const type = await a.page.evaluate(() => {
        const cs = (q, w) => { const e = document.querySelector(q); return e ? getComputedStyle(e, w) : null; };
        const b = cs('.dt-row[data-key="talk-1"] .dt-tx b'), em = cs('.dt-row[data-key="talk-1"] .dt-tx > em'), at = cs('.dt-row[data-key="talk-1"] .dt-at'), sh = cs('.dt-sh[data-key="stay-uvic"] .dt-tx b'), now = cs('.dt-now', '::after');
        return { title: `${b.fontSize}/${b.fontWeight}`, detail: em.fontSize, mono: `${at.fontSize} ${/JetBrains|Menlo|monospace/.test(at.fontFamily)}`, stay: `${sh.fontSize}/${sh.fontWeight}`, rule: `${now.height} ${now.backgroundColor}` };
      });
      check('type: a row\'s title 14/500, its detail 12, times mono 11.5, a stay\'s name 15/600, the now rule 1 px warm white', type.title === '14px/500' && type.detail === '12px' && type.mono === '11.5px true' && type.stay === '15px/600' && /^1px rgba?\(255, 240, 220/.test(type.rule), JSON.stringify(type));
      const fut = await a.page.evaluate(() => { const r = document.querySelector('.dt-row.is-future'), ic = getComputedStyle(r.querySelector('.dt-ic')); return { opacity: getComputedStyle(r).opacity, border: ic.borderTopStyle }; });
      check('what is still to come is at .62 with a dashed glyph', fut.opacity === '0.62' && fut.border === 'dashed', JSON.stringify(fut));
      const rightCol = await text(a, '.dt-row .dt-tm');
      check('the right column: lengths, countdowns (还有 10 分, 还有 40 分), the bus\'s 22:33 出门, the payment\'s amount in ink', ['还有 10 分', '还有 40 分', '22:33 出门', '$12.40', '1 时 40 分', '29 分', '活跃 42 分'].every(x => rightCol.includes(x))
        && await a.page.locator('.dt-row[data-key="pay-1"] .dt-tm').evaluate(el => getComputedStyle(el).color === 'rgb(238, 240, 251)'), rightCol.join('|'));
      const stayRight = await text(a, '.dt-sh .dt-tm');
      check('a stay says its range and its length at the right; the one he is in says since when and how long: 18:50 起 · 3 时', stayRight.includes('09:05–11:25 · 2 时 20 分') && stayRight.includes('18:50 起 · 3 时'), stayRight.join('|'));
      await a.page.clock.setFixedTime(new Date(NOW + 60_000));
      await a.until(() => document.querySelector('.dt-now')?.textContent.trim() === '现在 21:51', undefined, 4000);
      check('and a minute later the now line and the running stay\'s length have turned: 现在 21:51, 18:50 起 · 3 时 1 分', (await text(a, '.dt-sh .dt-tm')).includes('18:50 起 · 3 时 1 分') && (await text(a, '.dt-row .dt-tm')).includes('还有 9 分'));
      await a.page.clock.setFixedTime(new Date(NOW));
      await a.until(() => document.querySelector('.dt-now')?.textContent.trim() === '现在 21:50', undefined, 4000);

      // what waits on him
      const pill = await a.page.locator('.dt-waits').evaluate(el => ({ text: el.innerText, color: getComputedStyle(el).color, bg: getComputedStyle(el).backgroundColor }));
      const badges = await a.page.locator('.dt-wait').evaluateAll(els => els.map(el => ({ key: el.closest('.dt-row').dataset.key, text: el.innerText, color: getComputedStyle(el).color })));
      check('the header says 2 件等你 in amber, and the two rows that wait carry an amber 等你 badge after their titles', pill.text === '2 件等你' && pill.color === 'rgb(255, 201, 143)' && badges.map(b => `${b.key}:${b.text}`).join() === 'reminder-2:等你,event-1:等你' && badges.every(b => b.color === 'rgb(255, 201, 143)'), JSON.stringify({ pill, badges }));
      await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = document.querySelector('.dt-list').scrollHeight; });
      await a.page.waitForTimeout(150);
      await a.page.locator('.dt-waits').click(); await a.page.waitForTimeout(80);
      const flashing = await a.page.evaluate(() => document.querySelector('[data-key="reminder-2"]').classList.contains('is-flash'));
      await a.page.waitForTimeout(700);
      const placed = await a.page.evaluate(() => { const l = document.querySelector('.dt-list'), r = document.querySelector('[data-key="reminder-2"]'); return r.getBoundingClientRect().top - l.getBoundingClientRect().top; });
      check('a click on the pill takes the first row that waits to the top of the list, and it flashes', flashing && Math.abs(placed - 12) <= 2, String(placed));
      // the edges fade over 16 px where there is more beyond them
      const edge = async top => { await a.page.evaluate(top => { const l = document.querySelector('.dt-list'); l.scrollTop = top < 0 ? l.scrollHeight : top; }, top); await a.page.waitForTimeout(150); return a.page.evaluate(() => { const l = document.querySelector('.dt-list'); return { above: l.classList.contains('more-above'), below: l.classList.contains('more-below'), mask: getComputedStyle(l).webkitMaskImage }; }); };
      const e0 = await edge(0), e1 = await edge(300), e2 = await edge(-1);
      check('the list fades at an edge that has more past it, over 16 px: the bottom alone at the top, both in the middle, the top alone at the end, so a row half under the chips reads as scrolling',
        !e0.above && e0.below && /rgb\(0, 0, 0\) calc\(100% - 16px\)/.test(e0.mask) && e1.above && e1.below && /16px.*calc\(100% - 16px\)/.test(e1.mask) && e2.above && !e2.below && /rgb\(0, 0, 0\) 16px/.test(e2.mask) && ![e0, e1, e2].some(e => /30px|14px/.test(e.mask)), JSON.stringify([e0, e1, e2]));
      await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = 0; });
      await a.shot('list-01-rest', { x: 40, y: 0, width: 560, height: 700 });
      await a.ctx.close();
    }
    {
      const a = await boot({ served: fragmentedDay() });
      await openDay(a);
      const au = await audit(a);
      check('the fragmented day (15 stays, 11 moves, 2 rides, a stop four times): the same holds, nothing crowds or crosses', au.n === 34 && au.bad.length === 0, au.bad.join('; '));
      const dom = await a.page.evaluate(() => ({ heads: document.querySelectorAll('.dt-sh').length, pips: document.querySelectorAll('.dt-pip').length, trip: [...document.querySelectorAll('.dt-row.is-trip')].map(e => e.dataset.key + (e.classList.contains('is-top') ? '^' : '') + (e.classList.contains('is-end') ? '$' : '')),
        now: (n => ({ gap: n.getBoundingClientRect().top - document.querySelector('[data-key="f-w11"]').getBoundingClientRect().bottom, text: n.innerText.trim() }))(document.querySelector('.dt-now')), foot: document.querySelector('.dt-foot')?.innerText }));
      check('eleven places with headers and four stops with a small dot in the trip; the trips are one rail each; the now line hangs from the walk he is on', dom.heads === 11 && dom.pips === 4 && dom.trip.slice(0, 5).join() === 'f-w1^,f-s2,f-r1,f-talk-1,f-w2$' && dom.now.gap === 0 && dom.now.text === '现在 21:50' && dom.foot === '还没读到：Mac 的时间', JSON.stringify(dom));
      await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = 0; }); await a.page.waitForTimeout(300);
      await a.shot('list-02-fragmented-top', { x: 40, y: 0, width: 560, height: 700 });
      await a.page.evaluate(() => { const l = document.querySelector('.dt-list'); l.scrollTop = l.scrollHeight; }); await a.page.waitForTimeout(300);
      await a.shot('list-03-fragmented-bottom', { x: 40, y: 0, width: 560, height: 700 });
      await a.ctx.close();
    }
    {
      const a = await boot({ served: talkDay() });
      await openDay(a);
      const au = await audit(a);
      check('a day of talks alone: seven rows and the now line one under another, nothing crowds', au.n === 8 && au.bad.length === 0 && await a.page.locator('.dt-sh').count() === 0, au.bad.join('; '));
      const vd = await a.page.evaluate(() => { const v = document.querySelector('.dt-void'), tb = document.querySelector('.dt-track').getBoundingClientRect(), now = document.querySelector('.dt-nowtick').getBoundingClientRect(), r = v?.getBoundingClientRect(); return v && { text: v.textContent, color: getComputedStyle(v).color, size: getComputedStyle(v).fontSize, inTrack: r.left >= tb.left - .5 && r.right <= tb.right + .5 && r.top >= tb.top - .5 && r.bottom <= tb.bottom + .5, past: r.right <= now.left + 1.5, centred: Math.abs((r.left + r.right) / 2 - (tb.left + now.left + 1) / 2) < 2, segs: document.querySelectorAll('.dt-seg').length, still: getComputedStyle(v).pointerEvents, faint: getComputedStyle(document.querySelector('.dt-hours')).color }; });
      check('a day with talks and no place says 没有位置记录 in the empty past of its bar: as faint as the hours under it, centred in the past, inside the bar, and the pointer goes through it',
        vd && vd.text === '没有位置记录' && vd.segs === 0 && vd.inTrack && vd.past && vd.centred && vd.still === 'none' && vd.size === '11px' && vd.color === vd.faint, JSON.stringify(vd));
      await a.shot('list-04-talks-only', { x: 40, y: 0, width: 560, height: 520 });
      await a.ctx.close();
    }
    {
      // in English; and where the past is too short to hold the words (a talk at 02:10, now 02:30) there are none
      const a = await boot({ served: talkDay(), lang: 'en' });
      await openDay(a);
      check('in English: No location recorded', (await text(a, '.dt-void'))[0] === 'No location recorded');
      await a.ctx.close();
      const b = await boot({ served: makeDay({ now_ms: EARLY, items: [ev('n-talk', 'talk', 210, 215, { turns: 2 }, 'her')], labels: [], missing: [], line: null, highlights: [] }), at: EARLY });
      await openDay(b);
      check('where the past is too short to hold the words (02:30, the bar from 02:00) there are none', await b.page.locator('.dt-void').count() === 0 && await b.page.locator('.dt-bar').getAttribute('data-from') === '02:00' && await b.page.locator('.dt-seg').count() === 0);
      await b.ctx.close();
    }
    {
      const a = await boot({ served: earlyDay(), at: EARLY });
      await openDay(a);
      const au = await audit(a);
      check('at 02:30 with one stay since midnight and an event this afternoon: the bar starts at 02:00 (the hour now is in), the now line is inside the stay on its rail, the event below it, nothing crowds', au.bad.length === 0 && await a.page.locator('.dt-bar').getAttribute('data-from') === '02:00' && await a.page.locator('.dt-void').count() === 0 && await a.page.locator('.dt-sh').count() === 1 && (await text(a, '.dt-now'))[0].trim() === '现在 02:30' && (await text(a, '.dt-sh .dt-tm'))[0] === '00:00 起 · 2 时 30 分' && await a.page.locator('.dt-now ~ .dt-row.is-future').count() === 1 && await a.page.locator('.dt-now.is-rail').count() === 1, au.bad.join('; '));
      await a.shot('list-05-early-morning', { x: 40, y: 0, width: 560, height: 520 });
      await a.ctx.close();
    }
    {
      const a = await boot();
      await openDay(a);
      await a.page.locator('.dt-nav button').first().click();
      await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日' && document.querySelector('.dt-track'), undefined, 6000);
      await a.page.waitForTimeout(500);
      const au = await audit(a);
      check('yesterday: the same list, no now line, no countdown; the tag is a button that goes back to today', au.bad.length === 0 && await a.page.locator('.dt-now').count() === 0 && await a.page.locator('.dt-today').count() === 0 && (await text(a, '.dt-back'))[0] === '回到今天', au.bad.join('; '));
      await a.shot('list-06-yesterday', { x: 40, y: 0, width: 560, height: 520 });
      await a.page.locator('.dt-back').click();
      await a.until(() => document.querySelector('.dt-date')?.textContent === '10月19日 周一' && document.querySelector('.dt-today'), undefined, 6000);
      check('and 回到今天 is today again, with its tag', await a.page.locator('.dt-back').count() === 0 && await a.page.locator('.dt-today').count() === 1);
      await a.ctx.close();
    }
  }

  // ---- the new data: the weather and the body's numbers, a night's stages, a run, the sessions of Claude Code ----
  if (all || only === 'data') {
    // The colour of the page at points (CSS px from the clip's top left): the clip is screenshotted, drawn on a canvas in the page and read there.
    const pixels = async (a, clip, points) => {
      const png = (await a.page.screenshot({ clip })).toString('base64');
      return a.page.evaluate(async ([png, points]) => {
        const img = new Image(); img.src = `data:image/png;base64,${png}`; await img.decode();
        const c = document.createElement('canvas'); c.width = img.width; c.height = img.height;
        const g = c.getContext('2d'); g.drawImage(img, 0, 0);
        return points.map(([x, y]) => [...g.getImageData(Math.round(x * 2), Math.round(y * 2), 1, 1).data].slice(0, 3));
      }, [png, points]);
    };
    const near = (got, want, tol = 8) => got.every((v, i) => Math.abs(v - want[i]) <= tol);
    {
      const a = await boot({ served: fullDay() });
      await openDay(a);
      const au = await audit(a);
      check('the full day (a night with stages, a run, six sessions as two rows, the weather and the body): no row crosses another, every word is inside its row, the heights hold', au.n >= 25 && au.bad.length === 0, `${au.n} ${au.bad.join('; ')}`);
      const head = await a.page.evaluate(() => {
        const h = document.querySelector('.dt-head'), wx = h.querySelector('.dt-wx'), box = e => e.getBoundingClientRect(), hb = box(h);
        return { order: [...h.children].map(e => e.className.split(' ')[0]).join(), words: [...wx.querySelectorAll('span, em')].map(e => e.textContent).join('|'), sky: wx.dataset.sky, svg: !!wx.querySelector('svg'), mono: /JetBrains|Menlo|monospace/.test(getComputedStyle(wx.querySelector('em')).fontFamily),
          afterTag: Math.round(box(wx).left - box(h.querySelector('.dt-today')).right), beforeChips: Math.round(box(h.querySelector('.dt-chips')).left - box(wx).right), inside: [...h.children].every(e => box(e).left >= hb.left - .5 && box(e).right <= hb.right + .5) };
      });
      check('the header: the weather after the date and its tag, before the chips: a glyph, 多云, and 14° / 8° in mono; nothing leaves the header', head.order === 'dt-date,dt-today,dt-wx,dt-chips,dt-waits,dt-nav' && head.words === '多云|14° / 8°' && head.sky === 'cloudSun' && head.svg && head.mono && head.afterTag === 8 && head.beforeChips === 8 && head.inside, JSON.stringify(head));
      const nums = await a.page.evaluate(() => {
        const box = document.querySelector('.dt-numbers'), g = box.querySelector('.dt-vitals'), vit = [...g.querySelectorAll('.dt-vit')], cs = getComputedStyle(vit[0]);
        return { chips: [...box.querySelectorAll('.dt-num')].map(b => [...b.querySelectorAll('span, em')].map(e => e.textContent).join(' ')), vitals: vit.map(v => [...v.children].map(c => c.textContent).join(' ')), after: g.previousElementSibling?.className.split(' ')[0],
          bg: cs.backgroundColor, tag: vit[0].tagName, controls: g.querySelectorAll('button, a, input').length, mono: /JetBrains|Menlo|monospace/.test(getComputedStyle(vit[0].querySelector('em')).fontFamily), faint: getComputedStyle(vit[0].querySelector('small')).color, value: getComputedStyle(vit[0].querySelector('em')).color,
          row: [...box.children].map(e => Math.round(e.getBoundingClientRect().top)).filter((v, i, l) => l.indexOf(v) === i).length };
      });
      check('after the lens chips, the body\'s numbers as quiet values: 8,240 步, 静息 58, HRV 42 (a faint label and a mono value, no background, nothing to press), on the chips\' own line', nums.chips.join() === '睡 7:20,路上 3:04,运动 35 分,Mac 2:37,通话 18 分,和她 3 次,花 $12.40,照片 1' && nums.vitals.join() === '8,240 步,静息 58,HRV 42' && nums.after === 'dt-num' && nums.bg === 'rgba(0, 0, 0, 0)' && nums.controls === 0 && nums.mono && nums.faint !== nums.value, JSON.stringify(nums));
      const rowsOf = await a.page.evaluate(() => {
        const q = key => { const r = document.querySelector(`[data-key="${key}"]`); return r && { title: r.querySelector('.dt-tx b').textContent, detail: r.querySelector('.dt-tx > em')?.textContent ?? '', right: r.querySelector('.dt-tm').textContent, h: r.getBoundingClientRect().height, lk: r.dataset.lk, svg: !!r.querySelector('.dt-ic svg'), at: r.querySelector('.dt-at').textContent, rail: r.classList.contains('is-rail') }; };
        return { run: q('workout-1'), home: q('code:stay-home-1'), uvic: q('code:stay-uvic'), strays: document.querySelectorAll('[data-key^="code-s"]').length };
      });
      check('the run is a row: 跑步, 3.2 公里 · 240 千卡 · 平均心率 142, 35 分, 52 tall, a glyph, the 运动 lens; each stay\'s sessions are one row: Claude Code · 4 个会话 (the two newest titles, 2 时 40 分) and · 2 个会话 (1 时 30 分), a terminal glyph, the Mac lens; no session shows on its own',
        rowsOf.run.title === '跑步' && rowsOf.run.detail === '3.2 公里 · 240 千卡 · 平均心率 142' && rowsOf.run.right === '35 分' && rowsOf.run.h === 52 && rowsOf.run.lk === 'workout' && rowsOf.run.svg && rowsOf.run.at === '07:20'
        && rowsOf.home.title === 'Claude Code · 4 个会话' && rowsOf.home.detail === '周报草稿 · 给 verify 加 fixtures' && rowsOf.home.right === '2 时 40 分' && rowsOf.home.h === 52 && rowsOf.home.lk === 'work' && rowsOf.home.svg && rowsOf.home.rail
        && rowsOf.uvic.title === 'Claude Code · 2 个会话' && rowsOf.uvic.right === '1 时 30 分' && rowsOf.strays === 0, JSON.stringify(rowsOf));

      // the bar: the night shaded by stage, the sun
      const sb = await a.page.locator('.dt-seg.is-sleep').boundingBox();
      const sleep = await a.page.evaluate(() => { const e = document.querySelector('.dt-seg.is-sleep'); return { has: e.classList.contains('has-shade'), shade: e.style.getPropertyValue('--shade'), image: getComputedStyle(e).backgroundImage.slice(0, 15) }; });
      check('the night on the bar is painted by stage: a gradient of hard stops, REM the lighter hue, core, then awake', sleep.has && sleep.image === 'linear-gradient' && sleep.shade === 'linear-gradient(to right, rgb(150 160 235 / 0.8) 0.00% 41.67%, rgb(112 120 205 / 0.78) 41.67% 83.33%, rgb(112 120 205 / 0.25) 83.33% 100.00%)', JSON.stringify(sleep));
      await a.page.mouse.move(5, 5); await a.page.waitForTimeout(300);
      const clip = { x: sb.x - 3, y: sb.y - 3, width: sb.width + 6, height: sb.height + 6 };
      const [rem, core, awake] = await pixels(a, clip, [[3 + sb.width * .12, 5], [3 + sb.width * .78 * .98, 5], [3 + sb.width * .94, 5]].map(([x, y]) => [x, y]));
      check('and it shows: the REM stretch is the lightest, core darker, awake the faintest of the three (the sleep hue at .8, .78 and .25 over the ground)', near(rem, [123, 131, 191], 10) && near(core, [91, 97, 163], 10) && near(awake, [39, 41, 63], 10) && rem[2] > core[2] && core[2] > awake[2], JSON.stringify({ rem, core, awake }));
      await a.page.mouse.move(sb.x + sb.width / 2, sb.y + sb.height / 2); await a.page.waitForTimeout(400);
      const card = await a.page.evaluate(() => { const c = document.querySelector('.dt-card.is-on .dt-slot.is-cur'); return { head: c?.querySelector('.dt-ch b')?.textContent, note: c?.querySelector('.dt-cnote')?.textContent, range: c?.querySelector('.dt-ch span')?.textContent }; });
      check('hovering the sleep says what the night had: 睡觉, 昨天 23:10–07:00 · 7 时 20 分, and 深睡 1:10 · REM 1:40 under it', card.head === '睡觉' && card.note === '深睡 1:10 · REM 1:40' && card.range === '昨天 23:10–07:00 · 7 时 20 分', JSON.stringify(card));
      await a.shot('data-02-sleep-card', { x: 40, y: 0, width: 560, height: 300 });
      await a.page.mouse.move(5, 5); await a.page.waitForTimeout(300);
      const sun = await a.page.evaluate(() => {
        const hours = document.querySelector('.dt-hours'), tb = document.querySelector('.dt-track').getBoundingClientRect(), mark = e => { const r = e.getBoundingClientRect(); return r.left + r.width / 2 - tb.left; };
        const marks = [...hours.querySelectorAll('.dt-sun')], labels = [...hours.querySelectorAll('span:not(.dt-sun)')], one = e => { const st = getComputedStyle(e); return { kind: e.dataset.sun, at: mark(e) / tb.width, glyph: !!e.querySelector('svg'), w: st.width, h: st.height, bg: st.backgroundColor, top: e.getBoundingClientRect().top - tb.bottom }; };
        return { marks: marks.map(one), text: marks.map(e => e.textContent).join(''), before: marks.map(e => getComputedStyle(e, '::before').content).join(), labels: labels.map(e => e.textContent).join(),
          glyphsClear: marks.filter(e => e.querySelector('svg')).every(m => labels.every(l => Math.abs(mark(l) - mark(m)) > 11)), over: marks.map(m => labels.some(l => Math.abs(mark(l) - mark(m)) <= 11)) };
      });
      const f = ms => (ms - T(6, 0)) / (18 * 3_600_000), [rise, set] = sun.marks;
      check('the sun in the hours row, no words: at 07:25 it is a little sun on the horizon with a hairline, where no hour is near; every hour\'s number is written (06 09 12 15 18 21 24)',
        rise.kind === 'rise' && Math.abs(rise.at - f(T(7, 25))) < .004 && rise.glyph && rise.w === '11px' && sun.labels === '06,09,12,15,18,21,24' && sun.glyphsClear && sun.over.join() === 'false,true' && sun.text === '', JSON.stringify(sun));
      check('and at 18:05 (the 18 is under it) the hour wins: the sun is only a 1 x 4 px warm tick hanging from the bar, without the glyph and without its hairline',
        set.kind === 'set' && Math.abs(set.at - f(T(18, 5))) < .004 && !set.glyph && set.w === '1px' && set.h === '4px' && set.bg === 'rgba(255, 224, 186, 0.8)' && set.top >= 0 && set.top <= 3.5 && sun.before.split(',')[1] === 'none', JSON.stringify(sun));
      // a rest on the weather says when the sun comes up and goes down
      const tip0 = await a.page.evaluate(() => { const t = document.querySelector('.dt-wx-tip'); return t && { text: t.textContent, op: getComputedStyle(t).opacity, vis: getComputedStyle(t).visibility }; });
      const wx = await a.page.locator('.dt-wx').boundingBox();
      await a.page.mouse.move(wx.x + wx.width / 2, wx.y + wx.height / 2); await a.page.waitForTimeout(600);
      const tip1 = await a.page.evaluate(() => { const t = document.querySelector('.dt-wx-tip'), r = t.getBoundingClientRect(), h = document.querySelector('.dt-wx').getBoundingClientRect(); return { text: t.textContent, op: getComputedStyle(t).opacity, vis: getComputedStyle(t).visibility, below: r.top - h.bottom, size: getComputedStyle(t).fontSize, none: getComputedStyle(t).pointerEvents }; });
      check('a rest on the weather says 日出 07:25 · 日落 18:05 in a small card under it (nothing before the rest, and the pointer goes through it)', tip0.text === '日出 07:25 · 日落 18:05' && tip0.op === '0' && tip0.vis === 'hidden' && tip1.text === '日出 07:25 · 日落 18:05' && tip1.op === '1' && tip1.vis === 'visible' && tip1.below >= 3 && tip1.below <= 10 && tip1.none === 'none', JSON.stringify({ tip0, tip1 }));
      await a.shot('data-06-weather-tip', { x: 40, y: 20, width: 560, height: 120 });
      await a.page.mouse.move(5, 5); await a.page.waitForTimeout(400);
      await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = 0; }); await a.page.waitForTimeout(300);
      await a.shot('data-01-full-day-top', { x: 40, y: 0, width: 560, height: 700 });
      await a.shot('data-03-header-and-bar', { x: 40, y: 20, width: 560, height: 190 });
      await a.page.evaluate(() => { const l = document.querySelector('.dt-list'); l.scrollTop = l.scrollHeight; }); await a.page.waitForTimeout(300);
      await a.shot('data-04-full-day-bottom', { x: 40, y: 0, width: 560, height: 700 });

      // the lens: 运动 lights the run on the bar and keeps its row; Mac lights the blocks, the sessions and their rows
      const op = key => a.page.locator(`[data-key="${key}"]`).evaluate(el => getComputedStyle(el).opacity);
      await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = 0; });
      await a.page.locator('.dt-num', { hasText: '运动' }).click(); await a.page.waitForTimeout(400);
      const lens1 = await a.page.evaluate(() => ({ lens: document.querySelector('.dt').dataset.lens, spans: document.querySelectorAll('.dt-lm').length, k: getComputedStyle(document.querySelector('.dt-lm')).backgroundColor }));
      check('the 运动 lens lights the run on the bar in (156, 224, 120) and keeps its row, the rest steps back', lens1.lens === 'workout' && lens1.spans === 1 && lens1.k === 'rgb(156, 224, 120)' && await op('workout-1') === '1' && await op('talk-1') === '0.35' && await op('code:stay-home-1') === '0.35', JSON.stringify(lens1));
      await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(200);
      await a.page.locator('.dt-num', { hasText: 'Mac' }).click(); await a.page.waitForTimeout(400);
      const lens2 = await a.page.evaluate(() => ({ lens: document.querySelector('.dt').dataset.lens, spans: document.querySelectorAll('.dt-lm').length }));
      check('the Mac lens lights the sessions with the work: 8 spans on the bar (2 blocks, 6 sessions), and the folded rows stay as the blocks do', lens2.lens === 'work' && lens2.spans === 8 && await op('code:stay-home-1') === '1' && await op('code:stay-uvic') === '1' && await op('work-1') === '1' && await op('talk-1') === '0.35' && await op('workout-1') === '0.35', JSON.stringify(lens2));
      await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(200);
      await a.ctx.close();
    }
    {
      // a name the weather does not have, and a body that reported one number
      const a = await boot({ served: fullDay({ weather: { condition: 'frigid', high: 3, low: -4, sunrise_ms: null, sunset_ms: null }, summary: { steps: null, resting_hr: 58, hrv: null } }) });
      await openDay(a);
      const r = await a.page.evaluate(() => ({ wx: document.querySelector('.dt-wx')?.textContent, svg: !!document.querySelector('.dt-wx svg'), sky: document.querySelector('.dt-wx')?.dataset.sky, vit: [...document.querySelectorAll('.dt-vit')].map(v => v.textContent), suns: document.querySelectorAll('.dt-sun').length, labels: document.querySelector('.dt-hours').textContent }));
      check('an unknown weather shows only 3° / -4° with no glyph; a body with one number shows that one; no sun times, no sun marks, all seven hours', r.wx === '3° / -4°' && !r.svg && r.sky === undefined && r.vit.join() === '静息58' && r.suns === 0 && r.labels === '06091215182124', JSON.stringify(r));
      await a.ctx.close();
    }
    {
      const a = await boot({ served: fullDay({ weather: null, summary: null }) });
      await openDay(a);
      const r = await a.page.evaluate(() => ({ wx: document.querySelectorAll('.dt-wx').length, vit: document.querySelectorAll('.dt-vitals, .dt-vit').length, suns: document.querySelectorAll('.dt-sun').length, chips: document.querySelectorAll('.dt-num').length }));
      check('without the weather and the body the header and the chips are as they were: nothing drawn, nothing empty', r.wx === 0 && r.vit === 0 && r.suns === 0 && r.chips === 8, JSON.stringify(r));
      await a.ctx.close();
    }
    {
      const a = await boot({ served: fullDay(), lang: 'en' });
      await openDay(a);
      const r = await a.page.evaluate(() => ({ wx: [...document.querySelectorAll('.dt-wx span, .dt-wx em')].map(e => e.textContent).join('|'), vit: [...document.querySelectorAll('.dt-vit')].map(v => [...v.children].map(c => c.textContent).join(' ')),
        chip: [...document.querySelectorAll('.dt-num')].map(b => [...b.querySelectorAll('span, em')].map(e => e.textContent).join(' ')).join(), run: document.querySelector('[data-key="workout-1"] .dt-tx b').textContent, runDetail: document.querySelector('[data-key="workout-1"] .dt-tx > em').textContent,
        fold: document.querySelector('[data-key="code:stay-home-1"] .dt-tx b').textContent, head: document.querySelector('.dt-head').scrollWidth <= document.querySelector('.dt-head').clientWidth }));
      check('in English: Partly cloudy 14° / 8°, 8,240 steps, Resting 58, HRV 42, Workout 35 min, Running 3.2 km · 240 kcal · avg heart rate 142, Claude Code · 4 sessions; the header still holds',
        r.wx === 'Partly cloudy|14° / 8°' && r.vit.join() === '8,240 steps,Resting 58,HRV 42' && /Workout 35 min/.test(r.chip) && r.run === 'Running' && r.runDetail === '3.2 km · 240 kcal · avg heart rate 142' && r.fold === 'Claude Code · 4 sessions' && r.head, JSON.stringify(r));
      await a.ctx.close();
    }
    {
      // the header on a crowded day: the weather, three labels, two things waiting. Labels are whole or they are a +N, and they stay between the weather and the pill.
      const a = await boot({ served: fullDay({ labels: [{ title: '期中周' }, { title: '黄金周假期' }, { title: '牙医' }] }) });
      await openDay(a);
      const r = await a.page.evaluate(() => {
        const h = document.querySelector('.dt-head'), hb = h.getBoundingClientRect(), box = q => h.querySelector(q).getBoundingClientRect(), chips = box('.dt-chips');
        return { inside: [...h.children].every(e => e.getBoundingClientRect().right <= hb.right + .5 && e.getBoundingClientRect().left >= hb.left - .5), between: chips.left >= box('.dt-wx').right + 7.5 && chips.right <= box('.dt-waits').left - 7.5,
          labels: [...document.querySelectorAll('.dt-chip')].map(c => c.textContent).join(), cut: [...document.querySelectorAll('.dt-chip')].some(c => c.scrollWidth > c.clientWidth), waits: document.querySelector('.dt-waits').textContent, one: h.getBoundingClientRect().height };
      });
      check('the header on a crowded day (weather, three labels, 2 件等你, the day buttons): nothing leaves it, the labels that do not fit whole become +2, none shows half a word, and the header stays one line', r.inside && r.between && r.labels === '期中周,+2' && !r.cut && r.waits === '2 件等你' && r.one < 30, JSON.stringify(r));
      await a.shot('data-05-crowded-header', { x: 40, y: 20, width: 560, height: 190 });
      await a.ctx.close();
    }
    {
      // one label, the usual case: it is whole
      const a = await boot({ served: fullDay() });
      await openDay(a);
      const r = await a.page.evaluate(() => { const c = document.querySelector('.dt-chip'); return { cut: c.scrollWidth > c.clientWidth, text: c.textContent }; });
      check('with one label, the usual case, the label is whole beside the weather', r.text === '期中周' && !r.cut, JSON.stringify(r));
      await a.ctx.close();
    }
  }

  // ---- the list under the pointer, what the keys choose, the day's keys, and the window's key focus ----
  if (all || only === 'keys') {
    const rich = buildDay(richDay(), NOW, zh), rbar = buildBar(rich, zh);
    const a = await boot({ served: richDay() });
    const seg = await line(a), on = { x: seg.mid, y: 14 };
    let r;
    const focusCalls = () => a.page.evaluate(() => (window.__focus ?? []).join());
    await a.glide(a.far, on, 300);
    await settle(a, 'open');
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-row'), undefined, 8000);
    await a.page.waitForTimeout(700);
    const geo = await a.page.evaluate(() => { const r = document.querySelector('.dt-track').getBoundingClientRect(); return { left: r.left, width: r.width }; });
    const px = ms => geo.left + Math.min(1, Math.max(0, fOf(rbar, ms))) * geo.width;
    const reveal = key => a.page.evaluate(k => { const l = document.querySelector('.dt-list'), el = document.querySelector(`[data-key="${k}"]`); l.scrollTop += el.getBoundingClientRect().top - l.getBoundingClientRect().top - 120; }, key);
    const centre = async key => { const b = await a.page.locator(`[data-key="${key}"]`).boundingBox(); return { x: b.x + b.width / 2, y: b.y + b.height / 2 }; };
    const lit = () => a.page.evaluate(() => {
      const b = document.querySelector('.dt-bracket')?.getBoundingClientRect();
      return { hot: [...document.querySelectorAll('.dt-row.is-hot, .dt-sh.is-hot')].map(e => e.dataset.key), pick: [...document.querySelectorAll('.dt-row.is-pick, .dt-sh.is-pick')].map(e => e.dataset.key), current: [...document.querySelectorAll('[aria-current="true"]')].map(e => e.dataset.key),
        bracket: b ? { left: b.left, width: b.width } : null, lifted: document.querySelectorAll('.dt-seg.is-hot, .dt-sched.is-hot').length };
    });
    const want = key => { const x = rich.span.get(key); return { left: px(x.t0), width: Math.max(3, px(x.t1) - px(x.t0)) }; };
    const sameSpan = (b, key) => { const w = want(key); return !!b && Math.abs(b.left - w.left) < 1.6 && Math.abs(b.width - w.width) < 1.6; };
    const away = async () => { const b = await a.page.locator('.dt-head').boundingBox(); await a.move(b.x + 60, b.y + b.height / 2); await a.page.waitForTimeout(350); };
    const date = () => a.page.locator('.dt-date').innerText();

    check('a rest on the line opens the day and asks for no key focus', (await a.read()).tl === 'open' && await focusCalls() === '');
    await reveal('ride-1'); await a.page.waitForTimeout(150);
    await a.move(...Object.values(await centre('ride-1'))); await a.page.waitForTimeout(350);
    let m = await lit();
    const look = await a.page.evaluate(() => { const r = getComputedStyle(document.querySelector('[data-key="ride-1"]')), s = document.querySelector('.dt-seg.is-hot'), b = getComputedStyle(document.querySelector('.dt-bracket')); return { bg: r.backgroundColor, radius: r.borderTopLeftRadius, filter: s && getComputedStyle(s).filter, ring: b.borderTopColor }; });
    check('a pointer on a row lights it (white .04, radius 10), brackets its span on the bar with a 1.5 px ink outline and lifts the piece 12%', m.hot.join() === 'ride-1' && look.bg === 'rgba(255, 255, 255, 0.04)' && look.radius === '10px' && sameSpan(m.bracket, 'ride-1') && m.lifted === 1 && look.filter === 'brightness(1.12)' && look.ring === 'rgba(238, 240, 251, 0.92)'
      && /\.dt-bracket \{[^}]*border:1\.5px solid rgb\(var\(--ink\) \/ \.92\)/.test(readFileSync(path.join(root, 'src/day-bar.css'), 'utf8')), JSON.stringify({ m, look, want: want('ride-1') }));
    check('and hovering asks for no key focus', await focusCalls() === '');
    await a.shot('keys-01-hover-row', { x: 40, y: 0, width: 560, height: 700 });
    await reveal('stay-uvic'); await a.page.waitForTimeout(150);
    await a.move(...Object.values(await centre('stay-uvic'))); await a.page.waitForTimeout(350);
    m = await lit();
    check('a pointer on a stay\'s header brackets the whole stay (15:19 to 18:15)', m.hot.join() === 'stay-uvic' && sameSpan(m.bracket, 'stay-uvic'), JSON.stringify({ m, want: want('stay-uvic') }));
    await away();
    m = await lit();
    check('leaving the row clears the light and the bracket', m.hot.length === 0 && m.pick.length === 0 && m.bracket === null && m.lifted === 0, JSON.stringify(m));

    // a click on the line pins the day, and that click (only) takes the window's key focus
    await a.move(on.x, on.y); await a.page.mouse.down(); await a.page.mouse.up();
    check('a click that pins the day takes key focus, once', (await settle(a, 'pinned', 2000)).tl === 'pinned' && await focusCalls() === 'true', await focusCalls());

    const press = async (k, n = 1) => { for (let i = 0; i < n; i++) await a.page.keyboard.press(k); await a.page.waitForTimeout(n > 4 ? 600 : 330); };
    // with nothing chosen and the now line out of view the keys start from the top of the list (↓) or its bottom (↑)
    await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = 0; }); await a.page.waitForTimeout(200);
    await press('ArrowDown'); m = await lit();
    check('↓ with nothing chosen and the now line out of view takes the first row in view, and brackets its stretch of the bar', m.pick.join() === 'sleep-1' && sameSpan(m.bracket, 'sleep-1'), JSON.stringify({ m, want: want('sleep-1') }));
    // ← → are days; they are not read in a text field or with a modifier
    await press('ArrowLeft');
    await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日' && document.querySelector('.dt-track'), undefined, 6000);
    m = await lit();
    check('← is the day before, and the row chosen goes with the day', await date() === '10月18日 周日' && m.pick.length === 0 && m.bracket === null);
    await press('ArrowRight');
    await a.until(() => document.querySelector('.dt-date')?.textContent === '10月19日 周一' && document.querySelector('.dt-today'), undefined, 6000);
    await press('ArrowRight');
    check('→ is the day after, and on today it does nothing (tomorrow is never read)', await date() === '10月19日 周一' && !a.s.hits.some(h => h.includes('2026-10-20')), a.s.hits.join());
    await a.page.evaluate(() => { const i = document.createElement('input'); i.id = 'probe'; document.querySelector('.dt').append(i); i.focus(); });
    await press('ArrowLeft'); await press('ArrowDown'); m = await lit();
    check('in a text field the arrows are the field\'s: no day changes, no row is chosen', await date() === '10月19日 周一' && m.pick.length === 0);
    await a.page.evaluate(() => document.getElementById('probe').remove());
    await a.page.keyboard.press('Meta+ArrowLeft'); await a.page.keyboard.press('Alt+ArrowDown'); await a.page.waitForTimeout(300); m = await lit();
    check('with ⌘ or ⌥ held they are not the pane\'s either', await date() === '10月19日 周一' && m.pick.length === 0);

    // with the now line in view they start from it: ↑ at the last row above it, ↓ at the first row below it
    await press('ArrowUp'); m = await lit();
    check('↑ with nothing chosen and the now line in view takes the last row above it (the Claude chat of 20:10)', m.pick.join() === 'chat-1' && sameSpan(m.bracket, 'chat-1'), JSON.stringify({ m, want: want('chat-1') }));
    await press('ArrowLeft'); await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日' && document.querySelector('.dt-track'), undefined, 6000);
    await press('ArrowRight'); await a.until(() => document.querySelector('.dt-date')?.textContent === '10月19日 周一' && document.querySelector('.dt-today'), undefined, 6000);
    await press('ArrowDown');
    m = await lit();
    const ring = await a.page.evaluate(() => { const c = getComputedStyle(document.querySelector('.is-pick')); return { bg: c.backgroundColor, shadow: c.boxShadow }; });
    check('↓ with nothing chosen takes the first row under the now line (the 22:00 reminder), rings it, brackets its moment on the bar, and says so to a screen reader', m.pick.join() === 'reminder-1' && m.current.join() === 'reminder-1' && sameSpan(m.bracket, 'reminder-1') && ring.bg === 'rgba(255, 255, 255, 0.07)' && /inset/.test(ring.shadow), JSON.stringify({ m, ring }));
    await a.shot('keys-02-picked-row', { x: 40, y: 0, width: 560, height: 700 });
    await press('ArrowDown'); m = await lit();
    check('↓ again is the next row: the event at 22:30, its hour on the bar', m.pick.join() === 'event-1' && sameSpan(m.bracket, 'event-1'), JSON.stringify(m));
    await press('ArrowUp', 2); m = await lit();
    check('↑ twice goes back past the reminder and over the now line to the chat before it', m.pick.join() === 'chat-1' && sameSpan(m.bracket, 'chat-1'), JSON.stringify(m));
    await press('ArrowUp', 60); m = await lit();
    const top = await a.page.evaluate(() => document.querySelector('.dt-list').scrollTop);
    check('↑ stops at the first row of the day and the list is at its top', m.pick.join() === 'sleep-1' && top === 0, JSON.stringify({ m, top }));

    // down through the day: the row is always inside the list, clear of the fades, and the list eases to it
    const where = () => a.page.evaluate(() => { const l = document.querySelector('.dt-list'), r = l.getBoundingClientRect(), p = document.querySelector('.is-pick').getBoundingClientRect();
      return { top: p.top - r.top, bottom: r.bottom - p.bottom, atTop: l.scrollTop < 1, atEnd: l.scrollTop > l.scrollHeight - l.clientHeight - 1, key: document.querySelector('.is-pick').dataset.key }; });
    let bad = [], visited = [];
    for (let i = 0; i < 40; i++) {
      await a.page.keyboard.press('ArrowDown'); await a.page.waitForTimeout(260);
      const w = await where(); visited.push(w.key);
      if (w.top < -.5 || w.bottom < -.5 || w.top < 20 && !w.atTop || w.bottom < 20 && !w.atEnd) bad.push(`${w.key} ${w.top.toFixed(0)}/${w.bottom.toFixed(0)}`);
    }
    const everyRow = await a.page.evaluate(() => [...document.querySelectorAll('.dt-list [data-key]')].map(e => e.dataset.key));
    check('going down the whole day, each row chosen is fully inside the list, 20 px clear of its fading edges unless the list can go no further, and every row is reached in order', bad.length === 0 && visited.slice(0, everyRow.length - 1).join() === everyRow.slice(1).join() && visited.at(-1) === everyRow.at(-1), bad.join('; ') + ' | ' + visited.length + ' ' + everyRow.length);
    await press('ArrowUp', 6);
    const sampled = a.page.evaluate(() => new Promise(resolve => { const l = document.querySelector('.dt-list'), seen = [], t0 = performance.now(); const tick = now => { seen.push(l.scrollTop); if (now - t0 < 420) requestAnimationFrame(tick); else resolve(seen); }; requestAnimationFrame(tick); }));
    await a.page.keyboard.press('ArrowUp'); await a.page.keyboard.press('ArrowUp'); await a.page.keyboard.press('ArrowUp'); await a.page.keyboard.press('ArrowUp'); await a.page.keyboard.press('ArrowUp'); await a.page.keyboard.press('ArrowUp');
    const trace = await sampled, steps = new Set(trace.map(v => Math.round(v))).size;
    check('the list eases to the row (several positions on the way, none going back) rather than jumping', steps >= 4 && trace.every((v, i) => i === 0 || v <= trace[i - 1] + .5) && trace[0] > trace.at(-1) + 20, JSON.stringify({ steps, trace: trace.filter((_, i) => i % 3 === 0) }));

    // what the pointer is on wins while it is on something, and what the keys chose comes back when it leaves
    await press('ArrowDown', 3); m = await lit();
    const chosen = m.pick[0];
    await reveal('move-1'); await a.page.waitForTimeout(200);
    await a.move(...Object.values(await centre('move-1'))); await a.page.waitForTimeout(350);
    m = await lit();
    const over = sameSpan(m.bracket, 'move-1') && m.hot.join() === 'move-1' && m.pick.join() === chosen;
    await away(); m = await lit();
    check('the pointer\'s row has the bracket while it is on it; the keys\' row has it back when the pointer leaves', over && m.hot.length === 0 && sameSpan(m.bracket, chosen), JSON.stringify({ m, chosen }));

    // a list that scrolls under a resting pointer does not move what the keys chose
    await press('ArrowUp', 60);
    const rest = await centre('stay-home-1'); await a.move(rest.x, rest.y); await a.page.waitForTimeout(300);
    await press('ArrowDown', 14); m = await lit();
    const under = await a.page.evaluate(([x, y]) => document.elementFromPoint(x, y)?.closest('[data-key]')?.dataset.key ?? null, [rest.x, rest.y]);
    check('and a list scrolling under a pointer at rest does not take the choice or the bracket from the keys', m.pick.length === 1 && m.hot.length === 0 && under !== null && under !== m.pick[0] && sameSpan(m.bracket, m.pick[0]), JSON.stringify({ m, under }));
    check('through all of it no key focus was asked for but the pinning click\'s', await focusCalls() === 'true', await focusCalls());
    const quickAsk = await a.page.evaluate(() => [...document.querySelectorAll('.dt-row, .dt-sh')].map(el => [el.dataset.key, !!el.querySelector(':scope > .dt-quick > .dt-ask')]));
    check('the quick 问她 is on exactly the rows the brain takes a turn about: those with an item to hand over (askOf), and not a row that is no one item', quickAsk.length > 10 && quickAsk.some(([, has]) => has) && quickAsk.every(([key, has]) => has === (askOf(rich, key, zh) !== null)), JSON.stringify(quickAsk.filter(([key, has]) => has !== (askOf(rich, key, zh) !== null))));
    await a.page.keyboard.press('Escape');
    r = await settle(a, '', 2000);
    check('Esc folds the day and gives the key focus back', r.tl === '' && await focusCalls() === 'true,false', await focusCalls());
    // unpinned by the same click that pinned it
    await a.move(on.x, on.y); await a.page.mouse.down(); await a.page.mouse.up();
    await settle(a, 'pinned', 2000);
    await a.page.mouse.down(); await a.page.mouse.up();
    r = await settle(a, '', 2000);
    check('a click on the pinned day closes it and gives the focus back too', r.tl === '' && await focusCalls() === 'true,false,true,false', await focusCalls());
    await a.ctx.close();
  }

  // ---- motion in the list: the rise on open, the day change, an item that arrives ----
  if (all || only === 'motion') {
    const openIt = async a => {
      const seg = await line(a);
      await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
      await a.until(() => document.querySelector('.notch-drop.is-open .dt-row'), undefined, 8000);
      return seg;
    };
    // The rows the list shows in its window, in order, as the page lays them out (animations finished, so no transform moves them).
    const inView = a => a.page.evaluate(() => {
      document.getAnimations().filter(x => x.effect.getComputedTiming().iterations !== Infinity).forEach(x => x.finish());
      const l = document.querySelector('.dt-list'), v = l.getBoundingClientRect();
      return [...l.querySelectorAll('.dt-row, .dt-sh, .dt-now')].filter(el => { const r = el.getBoundingClientRect(); return r.bottom > v.top && r.top < v.bottom; }).map(el => el.dataset.key ?? 'now');
    });
    // A sheet of the screencast: one tile per wanted moment (the nearest frame), the part of the page in `crop` at `k`. The page was run slowed to
    // `rate` of its speed, so the moments are in the animation's own milliseconds from `start` (epoch seconds).
    const sheet = async (a, name, got, start, rate, offsets, label, crop, k) => {
      const nearest = ms => got.reduce((best, f) => Math.abs(f.at - start - ms / 1000 / rate) < Math.abs(best.at - start - ms / 1000 / rate) ? f : best, got[0]);
      const tiles = offsets.map(ms => ({ ...nearest(ms), ms })), w = Math.round(crop.w * k), h = Math.round(crop.h * k), cols = 4;
      const page = await a.ctx.newPage();
      await page.setViewportSize({ width: cols * (w + 8) + 8, height: Math.ceil(tiles.length / cols) * (h + 8) + 8 });
      await page.setContent(`<body style="margin:0;padding:8px;background:#222;display:grid;grid-template-columns:repeat(${cols},${w}px);gap:8px;font:11px monospace;color:#fff">${tiles.map(f => {
        const png = Buffer.from(f.data, 'base64'), scale = 640 * k / png.readUInt32BE(16);
        return `<div style="position:relative;width:${w}px;height:${h}px;overflow:hidden;border-radius:6px;background:#000 url(data:image/png;base64,${f.data}) ${-crop.x * k}px ${-crop.y * k}px/${png.readUInt32BE(16) * scale}px ${png.readUInt32BE(20) * scale}px no-repeat"><span style="position:absolute;left:6px;bottom:4px;padding:1px 5px;border-radius:3px;background:#000a">${label} +${f.ms} ms</span></div>`;
      }).join('')}</body>`);
      await page.screenshot({ path: path.join(dir, `${name}.png`), fullPage: true, scale: 'css' });
      await page.close();
      return new Set(tiles.map(f => f.at)).size;
    };
    const screencast = async a => {
      const cdp = await a.ctx.newCDPSession(a.page), got = [];
      cdp.on('Page.screencastFrame', ev => { got.push({ at: ev.metadata.timestamp, data: ev.data }); cdp.send('Page.screencastFrameAck', { sessionId: ev.sessionId }).catch(() => {}); });
      await cdp.send('Page.startScreencast', { format: 'png', everyNthFrame: 1 });
      return { got, stop: async () => { await cdp.send('Page.stopScreencast'); await cdp.detach(); } };
    };

    {
      const a = await boot({ served: richDay(), init: record });
      const seg = await line(a);
      await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
      await a.until(() => document.querySelector('.notch-drop.is-open .dt-row'), undefined, 8000);
      await a.page.waitForTimeout(700);
      const log = (await anims(a)).filter(x => /dt-row|dt-sh|dt-now/.test(x.cls));
      const shown = await inView(a);
      const key = x => x.key ?? 'now';
      check('on open the rows in view come up one after another: at most ten, those in the window in order, 6 px and a fade, 220 ms each, 25 ms apart; none out of view',
        log.length === Math.min(10, shown.length) && log.map(key).join() === shown.slice(0, 10).join() && log.every((x, i) => x.opt.delay === i * 25 && x.opt.duration === 220 && x.opt.fill === 'backwards' && x.kf[0].opacity === 0 && x.kf[0].transform === 'translateY(6px)'), JSON.stringify({ log: log.map(x => [key(x), x.opt.delay]), shown }));
      check('nothing is left hidden or moved after it: every row is at its own opacity and in place', await a.page.evaluate(() => [...document.querySelectorAll('.dt-row, .dt-sh, .dt-now')].every(el => { const c = getComputedStyle(el); return (c.opacity === '1' || c.opacity === '0.62') && c.transform === 'none'; }) && document.getAnimations().filter(x => x.effect.getComputedTiming().iterations !== Infinity).length === 0));
      check('and nothing grew: the day as it opened is not new', (await anims(a)).every(x => x.kf[0]?.height !== '0px'));

      // a day change: the day that was leaves (80 ms) before the new one enters (140 ms); the list moves 16 px, the rest of the pane 8
      await a.page.evaluate(() => { window.__anims.length = 0; });
      await a.page.waitForTimeout(150);
      await a.page.keyboard.press('ArrowLeft');
      await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日' && document.querySelector('.dt-track'), undefined, 6000);
      await a.page.waitForTimeout(700);
      let go = await anims(a);
      const out = go.filter(x => x.opt.duration === 80), inn = go.filter(x => x.opt.duration === 140), cls = x => x.cls.split(' ')[0];
      check('going to the day before: the day that was leaves first, each part sliding to the right and fading (80 ms): the list 16 px, the bar and the rest 8',
        out.length >= 4 && out.every(x => x.kf.length === 1 && x.kf[0].opacity === 0 && x.opt.fill === 'forwards' && x.kf[0].transform === `translateX(${cls(x) === 'dt-list' ? 16 : 8}px)`) && ['dt-line', 'dt-bar', 'dt-numbers', 'dt-list'].every(c => out.some(x => cls(x) === c)), JSON.stringify(out.map(x => [cls(x), x.kf[0].transform])));
      check('then the new day enters from the left (140 ms), after the first has gone: -16 px for the list, -8 for the rest',
        inn.length >= 2 && inn.every(x => x.kf.length === 2 && x.kf[0].opacity === 0 && x.kf[0].transform === `translateX(-${cls(x) === 'dt-list' ? 16 : 8}px)` && x.opt.fill === 'backwards') && Math.min(...inn.map(x => x.at)) >= Math.max(...out.map(x => x.at)) + 70, JSON.stringify({ out: out.map(x => x.at), inn: inn.map(x => [cls(x), x.at, x.kf[0].transform]) }));
      check('and when it is over nothing is left faded or shifted', await a.page.evaluate(() => [...document.querySelector('.dt').children].every(el => { const c = getComputedStyle(el); return c.opacity === '1' && c.transform === 'none'; }) && document.getAnimations().filter(x => x.effect.getComputedTiming().iterations !== Infinity && x.effect.target?.closest?.('.dt')).length === 0));
      await a.page.evaluate(() => { window.__anims.length = 0; });
      await a.page.keyboard.press('ArrowRight');
      await a.until(() => document.querySelector('.dt-date')?.textContent === '10月19日 周一' && document.querySelector('.dt-today') && document.querySelector('.dt-now'), undefined, 6000);
      await a.page.waitForTimeout(500);
      go = await anims(a);
      check('going forward the other way: the old day slides left (to -16 px), the new comes in from the right (+16 px)', go.some(x => x.opt.duration === 80 && x.kf[0].transform === 'translateX(-16px)') && go.some(x => x.opt.duration === 140 && x.kf[0].transform === 'translateX(16px)'), JSON.stringify(go.map(x => [x.opt.duration, x.kf[0].transform])));
      // days pressed faster than they are read: nothing is left half-faded
      await a.page.keyboard.press('ArrowLeft'); await a.page.waitForTimeout(40); await a.page.keyboard.press('ArrowLeft'); await a.page.waitForTimeout(60); await a.page.keyboard.press('ArrowRight'); await a.page.waitForTimeout(30); await a.page.keyboard.press('ArrowRight');
      await a.until(() => document.querySelector('.dt-date')?.textContent === '10月19日 周一' && document.querySelector('.dt-today') && !document.querySelector('.dt.is-loading'), undefined, 6000);
      await a.page.waitForTimeout(600);
      check('days pressed faster than they are read settle on today, whole: nothing faded, shifted or still moving', await a.page.evaluate(() => [...document.querySelector('.dt').children].every(el => { const c = getComputedStyle(el); return c.opacity === '1' && c.transform === 'none'; }) && document.getAnimations().filter(x => x.effect.getComputedTiming().iterations !== Infinity && x.effect.target?.closest?.('.dt')).length === 0) && await a.page.locator('.dt-list .dt-row').count() > 10);
      await a.ctx.close();
    }
    {
      // the same two moves at a tenth of their speed, so the screencast catches them frame by frame; the sheets are made once it has stopped (a page
      // opened for them while it runs changes the size of the frames after)
      const a = await boot({ served: richDay(), init: record }), rate = .1;
      const cdp = await a.ctx.newCDPSession(a.page), got = [];
      cdp.on('Page.screencastFrame', ev => { got.push({ at: ev.metadata.timestamp, data: ev.data }); cdp.send('Page.screencastFrameAck', { sessionId: ev.sessionId }).catch(() => {}); });
      await cdp.send('Animation.enable'); await cdp.send('Animation.setPlaybackRate', { playbackRate: rate });
      await cdp.send('Page.startScreencast', { format: 'png', everyNthFrame: 1 });
      await openIt(a);
      await a.page.waitForTimeout(5200);
      const rise = (await anims(a)).filter(x => /dt-row|dt-sh|dt-now/.test(x.cls));
      await a.page.evaluate(() => { window.__anims.length = 0; });
      await a.page.keyboard.press('ArrowLeft');
      await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日' && document.querySelector('.dt-track'), undefined, 6000);
      await a.page.waitForTimeout(3600);
      await cdp.send('Page.stopScreencast');
      // the page's clock is fixed to the day's date, so its times are tied to the screencast's (real epoch seconds) by one reading of both
      const here = await a.page.evaluate(() => performance.now()), wall = Date.now() / 1000, epoch = x => wall - (here - x.at) / 1000;
      const leaving = (await anims(a)).filter(x => x.opt.duration === 80);
      await cdp.detach();
      check('the frame sheets of the rise and of the day change are made from a screencast of both at a tenth of their speed',
        rise.length > 3 && leaving.length > 0
        && await sheet(a, 'motion-open-rise', got, epoch(rise[0]), rate, [0, 30, 60, 90, 120, 150, 180, 210, 240, 280, 340, 440], 'rise', { x: 40, y: 230, w: 560, h: 440 }, .56) >= 10
        && await sheet(a, 'motion-day-change', got, epoch(leaving[0]), rate, [0, 20, 40, 60, 80, 100, 120, 140, 160, 180, 200, 230], 'day', { x: 40, y: 60, w: 560, h: 520 }, .5) >= 10);
      await a.ctx.close();
    }
    {
      // reduced motion: a 120 ms fade, nothing moves
      const a = await boot({ served: richDay(), init: record });
      await a.page.emulateMedia({ reducedMotion: 'reduce' });
      await openIt(a);
      await a.page.waitForTimeout(500);
      let log = (await anims(a)).filter(x => /dt-row|dt-sh|dt-now/.test(x.cls));
      check('with reduced motion the rows in view fade in over 120 ms, all together, and nothing moves', log.length >= 6 && log.every(x => x.opt.duration === 120 && !x.opt.delay && x.kf.every(f => f.transform === undefined)), JSON.stringify(log.slice(0, 2)));
      await a.page.evaluate(() => { window.__anims.length = 0; });
      await a.page.keyboard.press('ArrowLeft');
      await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日' && document.querySelector('.dt-track'), undefined, 6000);
      await a.page.waitForTimeout(400);
      log = await anims(a);
      check('and a day change swaps at once and fades the new day in over 120 ms, with no sliding out or in', !log.some(x => x.opt.duration === 80) && log.filter(x => x.opt.duration === 120).length >= 2 && log.every(x => x.kf.every(f => f.transform === undefined)), JSON.stringify(log.map(x => [x.opt.duration, x.kf])));
      await a.ctx.close();
    }
    {
      // an item that arrives while the day is open grows out of nothing
      const a = await boot({ served: richDay(), init: record, fastPolls: true });
      await openIt(a);
      await a.page.waitForTimeout(1200);
      await a.page.evaluate(() => { window.__anims.length = 0; });
      const extra = it('talk-new', 'talk', T(21, 20), T(21, 26), { turns: 3 }, 'her');
      a.s.served = richDay({ items: [...richDay().items, extra] });
      await a.until(() => document.querySelector('[data-key="talk-new"]'), undefined, 6000);
      await a.page.waitForTimeout(500);
      const grew = (await anims(a)).filter(x => x.kf[0]?.height === '0px');
      check('a talk that arrives on the open day grows in: height from 0 to its 52 and a fade, 240 ms; nothing else on the day moves',
        grew.length === 1 && grew[0].key === 'talk-new' && grew[0].kf[0].opacity === 0 && grew[0].kf[1].height === '52px' && grew[0].kf[1].opacity === '1' && grew[0].opt.duration === 240, JSON.stringify(grew));
      const row = await a.page.locator('[data-key="talk-new"]').evaluate(el => ({ h: el.getBoundingClientRect().height, anim: el.getAnimations().length }));
      check('and when it has grown it is a row like the others, 52 tall', row.h === 52 && row.anim === 0, JSON.stringify(row));
      await a.page.evaluate(() => { window.__anims.length = 0; });
      await a.page.waitForTimeout(1200);
      check('a poll that brings nothing new, and the minute turning, grow nothing', (await anims(a)).every(x => x.kf[0]?.height !== '0px'));
      await a.ctx.close();
    }
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
    check('the extra kinds the brain serves are rows: the payment, the Claude chat and the letter', (await text('.dt-row .dt-tx b')).some(x => x.trim() === 'A&W') && (await text('.dt-row .dt-tx em')).some(x => x === 'Teen Burger combo · 支付宝') && (await text('.dt-row .dt-tm')).some(x => x === '$12.40') && (await text('.dt-row .dt-tx b')).some(x => x.trim() === 'The bar for the day') && (await text('.dt-row .dt-tx b')).some(x => x.trim() === 'Claude 日报'));
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
    check('hovering 13:26: the photo (with its picture) and the payment (counterparty and amount), the photo first; the starred payment shows its star', c && c.head === 'A&W 13:15–13:55 · 40 分' && c.lines.length === 2 && /^汉堡和薯条\s+13:30$/.test(c.lines[0]) && /^A&W · \$12\.40\s+13:20$/.test(c.lines[1]) && c.thumbs === 1, JSON.stringify(c));
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
    check('the card follows the pointer on a spring (on its way for a few frames, then there) and its words fade when the stretch changes', fl.span > 60 && fl.between >= 1 && fl.fading >= 1, JSON.stringify(fl));
    await a.move(away.x, away.y); await a.page.waitForTimeout(300);

    // the words change as a fade through, never a cross-fade: every animation of the card is held and its clock set, so the same instants are read each time
    {
      await a.move(xAt(14, 20), y); await a.page.waitForTimeout(400);
      const h0 = (await card()).rect.height;
      await a.move(xAt(16, 50), y);
      const got = await a.page.evaluate(async () => {
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        const c = document.querySelector('.dt-card:not(.is-range)'), inner = c.querySelector('.dt-card-in'), slots = [...c.querySelectorAll('.dt-slot')], cur = slots.find(e => e.classList.contains('is-cur')), old = slots.find(e => e !== cur);
        const anims = c.getAnimations({ subtree: true });
        anims.forEach(x => x.pause());
        const read = t => { anims.forEach(x => { x.currentTime = t; }); return { t, old: +getComputedStyle(old).opacity, now: +getComputedStyle(cur).opacity, y: new DOMMatrix(getComputedStyle(cur).transform).m42, h: inner.getBoundingClientRect().height }; };
        const out = [0, 30, 60, 75, 105, 150, 180].map(read);
        const kinds = anims.map(x => x.transitionProperty ?? 'height');
        anims.forEach(x => x.cancel());
        return { out, kinds };
      });
      const hf = (await card()).rect.height, [s0, s30, s60, s75, s105, s150, s180] = got.out, apart = (x, v) => Math.abs(x - v) < .06;
      check('a change of words is a fade through: the old are fading at 30 ms and gone at 60, the new have not begun until 60 and are whole at 150, and never are both seen (the least of the two is 0 at every instant)',
        apart(s0.old, 1) && apart(s0.now, 0) && apart(s30.old, .5) && apart(s30.now, 0) && apart(s60.old, 0) && apart(s60.now, 0) && s75.old === 0 && s75.now > 0 && s75.now < .3 && apart(s105.now, .5) && apart(s150.now, 1) && apart(s180.now, 1)
        && got.out.every(x => Math.min(x.old, x.now) < .02), JSON.stringify(got.out));
      check('the new words come 2 px up: 2 px down while the old go, 0 when they are whole', apart(s60.y, 2) && s105.y > 0 && s105.y < 2 && apart(s150.y, 0), JSON.stringify(got.out.map(x => x.y)));
      check('and the card\'s height goes from the old words\' to the new words\' over the 150 ms (so the card never jumps)', Math.abs(hf - h0) > 10 && Math.abs(s0.h - h0) < 1 && Math.abs(s150.h - hf) < 1 && s75.h > Math.min(h0, hf) + .5 && s75.h < Math.max(h0, hf) - .5 && got.kinds.includes('height'), JSON.stringify({ h0, hf, hs: got.out.map(x => x.h), kinds: got.kinds }));
      await a.move(away.x, away.y); await a.page.waitForTimeout(300);
    }

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
    check('a range says its span and length, the places in it with their lengths (most first), then the other numbers; two buttons, 只看这段 and the quiet 这段在干嘛', rc && rc.head === `${ranged.range} ${ranged.length}` && rc.head === '14:00–18:00 4 时' && rc.lines.join('|') === [...ranged.places.map(p => `${p.name} ${p.length}`), ...ranged.nums.map(n => `${n.label} ${n.long}`)].join('|') && rc.lines.length >= 4 && rc.buttons.join() === '只看这段,这段在干嘛' && rc.on === 1, JSON.stringify(rc));
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
    {
      // a change of words under reduced motion: still one after the other, but only fades: no slide, and the card's height does not run
      await a.move(xAt(14, 20), y); await a.page.waitForTimeout(400);
      await a.move(xAt(16, 50), y);
      const got = await a.page.evaluate(async () => {
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        const c = document.querySelector('.dt-card:not(.is-range)'), inner = c.querySelector('.dt-card-in'), slots = [...c.querySelectorAll('.dt-slot')], cur = slots.find(e => e.classList.contains('is-cur')), old = slots.find(e => e !== cur);
        const anims = c.getAnimations({ subtree: true });
        anims.forEach(x => x.pause());
        const read = t => { anims.forEach(x => { x.currentTime = t; }); return { t, old: +getComputedStyle(old).opacity, now: +getComputedStyle(cur).opacity, y: new DOMMatrix(getComputedStyle(cur).transform).m42, h: inner.getBoundingClientRect().height }; };
        const out = [0, 30, 60, 120, 180, 240].map(read), kinds = anims.map(x => x.transitionProperty ?? 'height');
        anims.forEach(x => x.cancel());
        return { out, kinds };
      });
      const apart = (x, v) => Math.abs(x - v) < .06;
      check('under reduced motion the words are still not seen together, they only fade (old out in 60 ms, new in over 120 ms after them), they do not slide, and the card\'s height does not run',
        got.out.every(x => Math.min(x.old, x.now) < .02 && x.y === 0) && apart(got.out[1].old, .5) && apart(got.out[3].now, .5) && apart(got.out[5].now, 1) && !got.kinds.includes('height') && new Set(got.out.map(x => Math.round(x.h))).size === 1, JSON.stringify(got));
    }
    await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = document.querySelector('.dt-list').scrollHeight; });
    await a.move(xAt(15, 30), y); await a.page.waitForTimeout(100); await a.page.mouse.down(); await a.page.mouse.up(); await a.page.waitForTimeout(100);
    const inst = await a.page.evaluate(() => { const l = document.querySelector('.dt-list'), h = document.querySelector('[data-key="stay-uvic"]'); return { top: h.getBoundingClientRect().top - l.getBoundingClientRect().top, anim: getComputedStyle(h).animationName }; });
    check('a click jumps at once (no glide) and the row does not flash', Math.abs(inst.top - 12) <= 2 && inst.anim === 'none', JSON.stringify(inst));
    await a.ctx.close();
  }

  if (all || only === 'doing') {
    // ADR 0302: what he was doing leads. The wing, the bar's pieces and the hover card say it; the place stays the colour, the icon and the list's groups.
    const dm = buildDay(doingDay(), NOW, zh), dbar = buildBar(dm, zh), t0 = dbar.t0, t1 = T(24);
    const a = await boot({ served: doingDay() });
    await a.until(() => document.querySelector('.notch')?.dataset.line === '做 Jarvis · 2:15');
    const r = await a.read();
    check('the wing leads with what he is doing and since when: 做 Jarvis · 2:15, where the place alone said 家 · 3:00', r.wing === 'timeline' && r.line === '做 Jarvis · 2:15', JSON.stringify(r));
    await a.shot('doing-01-wing', { x: 330, y: 0, width: 260, height: 60 });
    const seg = await a.lineX();
    await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-track') && document.querySelector('.dt-row'), undefined, 8000);
    await a.page.waitForTimeout(700);
    const g = await a.page.evaluate(() => { const r = document.querySelector('.dt-track').getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width }; });
    const xAt = (h, m = 0) => g.left + (T(h, m) - t0) / (t1 - t0) * g.width, y = g.top + 12;
    const pieces = await a.page.evaluate(() => [...document.querySelectorAll('.dt-seg.is-stay')].map(el => ({ label: el.querySelector('.dt-sl b')?.textContent ?? null, colour: el.style.getPropertyValue('--c'), icon: !!el.querySelector('.dt-sl svg') })));
    const named = pieces.filter(x => x.label === '做 Jarvis');
    check('the bar names the stays he worked in by it, in the place\'s colour (the warm home, with its house where it fits beside the words), and none of the pieces says 家; the school keeps its blue and its icon under his answer',
      named.length === 2 && named.every(x => x.colour === '255 168 150') && named.some(x => x.icon) && !pieces.some(x => x.label === '家') && pieces.some(x => x.colour === '100 200 255' && x.icon), JSON.stringify(pieces));
    check('the list keeps its place groups: 家, A&W, 家, UVic, 家', (await a.page.locator('.dt-sh .dt-tx b').allInnerTexts()).map(x => x.trim()).join() === '家,A&W,家,UVic,家');
    await a.shot('doing-02-bar', { x: 40, y: 0, width: 560, height: 700 });
    const card = () => a.page.evaluate(() => {
      const c = document.querySelector('.dt-card.is-on:not(.is-range)');
      if (!c) return null;
      const cur = c.querySelector('.dt-slot.is-cur'), norm = el => el.innerText.replace(/\s*\n\s*/g, ' ').trim();
      return { head: norm(cur.querySelector('.dt-ch')), place: cur.querySelector('.dt-cnote.is-place')?.innerText ?? null, notes: cur.querySelectorAll('.dt-cnote').length };
    });
    const over = async (h, m) => { await a.move(xAt(h, m), y); await a.page.waitForTimeout(260); return card(); };
    let c = await over(10, 0);
    check('hovering the work: the title is 做 Jarvis with its own time, and the place 家 under it', c && c.head === '做 Jarvis 09:30–11:20 · 1 时 50 分' && c.place === '家', JSON.stringify(c));
    await a.shot('doing-03-hover-work', { x: 40, y: 0, width: 560, height: 700 });
    c = await over(14, 20);
    check('hovering the home span that has no title: the place as ever, no second line', c && c.head === '家 14:05–14:50 · 45 分' && c.place === null && c.notes === 0, JSON.stringify(c));
    c = await over(13, 20);
    check('hovering his answer over A&W: 吃午饭 with A&W under it; a minute later in the stay, past the answer, A&W alone', c && c.head === '吃午饭 13:15–13:35 · 20 分' && c.place === 'A&W' && (await over(13, 45))?.head === 'A&W 13:15–13:55 · 40 分', JSON.stringify(c));
    c = await over(14, 57);
    check('hovering the ride: its route once, no second line', c && c.head === '27 路 14:50–15:19 · 29 分' && c.place === null, JSON.stringify(c));
    await a.ctx.close();
    const b = await boot({ served: doingDay(), lang: 'en' });
    const en1 = await seen(b, r => /^On Jarvis · 2:15$/.test(r.line ?? ''), 6000);
    check('in English the wing reads On Jarvis · 2:15, whole', en1.line === 'On Jarvis · 2:15' && wingText(doingDay(), NOW, en).head === 'On Jarvis', JSON.stringify(en1));
    await b.ctx.close();
  }

  // ---- the open rows (spec-r3 section 8): a click opens a row in place, and what it says and can do depends on what it is ----
  // The full day and a picture that has to be cut to 240 tall (640 x 420), at 16:20.
  const bigDay = () => fullDay({ items: [...fullDay().items, it('picture-2', 'picture', T(16, 20), T(16, 20), { attachment: BIG, note: '傍晚的海边，风很大' })] });
  const orow = (a, key) => a.page.locator(`[data-key="${key}"]`).first();
  const tap = async (a, key) => { await orow(a, key).scrollIntoViewIfNeeded(); await orow(a, key).click({ position: { x: 170, y: 14 } }); };
  const rest = (a, ms = 520) => a.page.waitForTimeout(ms);
  const folds = a => a.page.locator('.dt-list .dt-fold').count();
  const expanded = (a, key) => orow(a, key).getAttribute('aria-expanded');
  const said = async (a, key) => (await orow(a, key).locator('.dt-body').first().innerText()).split('\n').filter(Boolean);
  const shotRow = async (a, name, key) => {
    const box = await orow(a, key).boundingBox(), list = await a.page.locator('.dt-list').boundingBox(), top = Math.max(0, Math.min(box.y, list.y + list.height - 60) - 150);
    await a.shot(name, { x: 40, y: top, width: 560, height: Math.min(560, 760 - top) });
  };
  if (all || only === 'open') {
    const full = buildDay(bigDay(), NOW, zh), fbar = buildBar(full, zh);
    const a = await boot({ served: bigDay() });
    await openDay(a);
    const geo = await a.page.evaluate(() => { const r = document.querySelector('.dt-track').getBoundingClientRect(); return { left: r.left, width: r.width }; });
    const px = ms => geo.left + Math.min(1, Math.max(0, fOf(fbar, ms))) * geo.width;
    const bracket = () => a.page.evaluate(() => { const b = document.querySelector('.dt-bracket')?.getBoundingClientRect(); return b ? { left: b.left, width: b.width } : null; });
    const sameSpan = (b, key) => { const x = full.span.get(key), w = { left: px(x.t0), width: Math.max(3, px(x.t1) - px(x.t0)) }; return !!b && Math.abs(b.left - w.left) < 1.6 && Math.abs(b.width - w.width) < 1.6; };

    // ---- opening and closing ----
    const before = await a.page.evaluate(() => { const r = document.querySelector('[data-key="move-1"]'); r.scrollIntoView({ block: 'center' }); const q = el => el.getBoundingClientRect(); return { top: q(r).top, h: q(r).height, next: q(document.querySelector('[data-key="move-1"]').nextElementSibling).top }; });
    // the open is read as it runs: the height of the content frame by frame, its opacity, and the timings the animations were given
    const run = await a.page.evaluate(async () => {
      const el = document.querySelector('[data-key="move-1"]');
      el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await new Promise(r => requestAnimationFrame(r));
      const fold = el.querySelector('.dt-fold'), body = fold.querySelector('.dt-body'), t0 = performance.now(), samples = [];
      const timings = fold.getAnimations({ subtree: true }).map(x => { const t = x.effect.getTiming(); return { on: x.effect.target === fold ? 'fold' : 'body', duration: t.duration, delay: t.delay, easing: t.easing }; });
      await new Promise(done => { const tick = () => { samples.push({ t: performance.now() - t0, h: fold.getBoundingClientRect().height, o: Number(getComputedStyle(body).opacity) }); if (performance.now() - t0 < 420) requestAnimationFrame(tick); else done(); }; tick(); });
      return { timings, samples, final: body.getBoundingClientRect().height };
    });
    const first = run.samples[0], last = run.samples.at(-1), grown = run.samples.find(x => x.h >= run.final - .5);
    check('a row opens with a spring of 260 ms and its words fade in after 60 ms (200 ms long), the same ease the rest of the pane uses', run.timings.some(x => x.on === 'fold' && x.duration === 260 && x.delay === 0 && x.easing === 'cubic-bezier(0.16, 1, 0.3, 1)') && run.timings.some(x => x.on === 'body' && x.duration === 200 && x.delay === 60), JSON.stringify(run.timings));
    check('what it holds grows from nothing to its height frame by frame, never back, and the words are not there at first', first.h < run.final * .5 && run.samples.every((x, i, l) => i === 0 || x.h >= l[i - 1].h - .5) && first.o < .05 && last.h >= run.final - .5 && last.o > .99 && grown.t > 120 && grown.t < 380, JSON.stringify({ first, last, final: run.final, grown: grown?.t }));
    const after = await a.page.evaluate(() => { const r = document.querySelector('[data-key="move-1"]'), q = el => el.getBoundingClientRect(), b = r.querySelector('.dt-body p'), title = r.querySelector('.dt-tx b'); return { top: q(r).top, h: q(r).height, bodyLeft: q(b).left, titleLeft: q(title).left, open: r.getAttribute('aria-expanded'), next: q(r.nextElementSibling).top, folds: document.querySelectorAll('.dt-list .dt-fold').length }; });
    check('the row stays where it was and grows downward by its content: the content hangs from the title\'s column and the rows under it move down by as much', Math.abs(after.top - before.top) < 40 && after.h > before.h + 60 && Math.abs(after.bodyLeft - after.titleLeft) < 1.5 && after.open === 'true' && after.folds === 1 && Math.abs((after.next - before.next) - (after.h - before.h)) < 2, JSON.stringify({ before, after }));
    await rest(a, 200);
    await tap(a, 'talk-1'); await rest(a);
    check('only one row is open at a time: opening another closes the one that was', await folds(a) === 1 && await expanded(a, 'move-1') === 'false' && await expanded(a, 'talk-1') === 'true');
    await tap(a, 'talk-1'); await rest(a, 450);
    check('a click on the open row closes it (it folds away, and nothing is left of it)', await folds(a) === 0 && await expanded(a, 'talk-1') === 'false');
    await tap(a, 'stay-home-1'); await rest(a);
    await orow(a, 'stay-home-1').locator('.dt-lines p').first().click(); await rest(a, 300);
    check('a click inside what is open does not close it', await expanded(a, 'stay-home-1') === 'true' && await folds(a) === 1);
    await a.move(...Object.values(await a.page.locator('.dt-head').boundingBox().then(b => [b.x + 60, b.y + b.height / 2]))); await a.page.waitForTimeout(350);
    check('and the open row keeps its bracket on the bar when the pointer goes away', sameSpan(await bracket(), 'stay-home-1'), JSON.stringify(await bracket()));

    // ---- the keys: Enter or Space opens the row they chose; Esc lets go of one thing at a time ----
    const key = async (k, ms = 420) => { await a.page.keyboard.press(k); await a.page.waitForTimeout(ms); };
    const chosen = () => a.page.evaluate(() => document.querySelector('.is-pick')?.dataset.key);
    await key('Escape');
    check('Esc closes the open row first, and the pane stays up', await folds(a) === 0 && (await a.read()).tl === 'pinned', JSON.stringify(await a.read()));
    await key('ArrowDown');
    const next = await chosen();
    check('↓ chooses the row after the one the click chose', next === 'workout-1', next);
    await key('Enter');
    check('Enter opens the row the keys chose', await expanded(a, next) === 'true' && await folds(a) === 1);
    await key('Enter');
    check('Enter again closes it', await expanded(a, next) === 'false' && await folds(a) === 0);
    await key(' ');
    check('Space opens it too', await expanded(a, next) === 'true' && await folds(a) === 1);
    await key('ArrowDown');
    check('moving the choice on leaves what is open open, and the bar\'s bracket follows the choice', await expanded(a, next) === 'true' && await chosen() !== next && sameSpan(await bracket(), await chosen()), `${await chosen()} ${JSON.stringify(await bracket())}`);
    await key('Enter');
    check('and Enter on the new choice opens that one and closes the first', await folds(a) === 1 && await expanded(a, next) === 'false' && await expanded(a, await chosen()) === 'true');
    await key('Escape');
    // the order of Esc: the open row, then the lens, then the pane
    await a.page.locator('.dt-num').first().click(); await a.page.waitForTimeout(250);
    await tap(a, 'move-1'); await rest(a);
    const lens = () => a.page.locator('.dt').getAttribute('data-lens');
    check('a lens can be pinned while a row is open', !!await lens() && await folds(a) === 1, `${await lens()} ${await folds(a)}`);
    await key('Escape');
    check('Esc: the open row first, the lens stays', await folds(a) === 0 && !!await lens() && (await a.read()).tl === 'pinned');
    await key('Escape');
    check('Esc: then the lens, the pane stays', !await lens() && (await a.read()).tl === 'pinned');
    await key('Escape', 700);
    check('Esc: and then the pane', (await a.read()).tl === '', JSON.stringify(await a.read()));
    await a.ctx.close();
  }

  // ---- each kind opened: what it says, and the buttons that work for it ----
  if (all || only === 'open') {
    const full = buildDay(bigDay(), NOW, zh);
    const a = await boot({ served: bigDay() });
    await openDay(a);
    const kinds = [
      ['stay-home-1', 'open-01-stay-kept', '12 Example St, Victoria|常去的地方 · 家|改名字|问她|这段在干嘛'],
      ['stay-aw', 'open-20-stay-plain', 'A&W, 1234 Douglas St, Victoria|问她|这段在干嘛'],
      ['move-1', 'open-21-move', '家 → A&W|问她|这段在干嘛'],
      ['ride-1', 'open-02-ride', '你下车了|问她|这段在干嘛'],
      ['event-1', 'open-05-event-mail', '22:30–23:30 · 1 时 · 还没开始|问她'],
      ['event-2', 'open-22-event-said', '23:40–23:55 · 15 分 · 还没开始|问她'],
      ['reminder-2', 'open-06-reminder-waiting', '知道了|问她'],
      ['workout-1', 'open-24-workout', `07:20–07:55 · 35 分 · 10'56"/公里|问她|这段在干嘛`],
      ['call-1', 'open-25-call', '16:40–16:58 · 18 分 · FaceTime|问她|这段在干嘛'],
      ['pay-1', 'open-08-payment', '支出 · $12.40 · CAD|问她'],
      ['chat-1', 'open-26-claude-chat', '20:10–20:42 · 32 分 · 14 条|问她|这段在干嘛'],
      ['claude-day-1', 'open-27-claude-day', 'The bar for the day|A tool exam|问她'],
      ['work-2', 'open-07-work', '活跃 1 时 55 分 · 共 2 时 5 分|项目|Jarvis|1 时 40 分|应用|Ghostty|1 时 10 分|Claude|30 分|让她总结这段|问她|这段在干嘛'],
      ['sleep-1', 'open-09-sleep', '清醒|REM|核心|深睡|23:10|07:00|深睡 1:10 · 核心 4:30 · REM 1:40 · 醒 3 次|问她|这段在干嘛'],
    ];
    const bad = [];
    for (const [key, name, want] of kinds) {
      await tap(a, key); await rest(a, 480);
      const got = (await said(a, key)).join('|');
      if (got !== want) bad.push(`${key}: ${got}`);
      if (name) await shotRow(a, name, key);
    }
    check('each kind says what it has when opened, in lines of its own, with only the buttons that work for it', bad.length === 0, bad.join('\n'));
    // what the row says in its two lines is not said again
    const lit = await a.page.evaluate(() => { const r = document.querySelector('[data-key="event-1"]'); return { cursor: getComputedStyle(r).cursor, expanded: r.getAttribute('aria-expanded') }; });
    await tap(a, 'event-1'); await rest(a, 480);
    check('a row that opens has the pointer and says whether it is open; a row still to come is drawn at full strength while it is open (and quiet again when it is closed)', lit.cursor === 'pointer' && lit.expanded === 'false' && await orow(a, 'event-1').evaluate(r => getComputedStyle(r).opacity) === '1', JSON.stringify(lit));
    await tap(a, 'event-1'); await rest(a, 500);
    check('and quiet again when it is closed', Number(await orow(a, 'event-1').evaluate(r => getComputedStyle(r).opacity)) < .7);

    // the talk: his line and hers from the day's records, one line each, clipped; the records were read once
    await tap(a, 'talk-1'); await rest(a, 520);
    const talk = await orow(a, 'talk-1').locator('.dt-body').evaluate(b => ({ lines: [...b.querySelectorAll('.dt-said p')].map(p => ({ who: p.querySelector('small').textContent, text: p.querySelector('span').textContent, h: p.getBoundingClientRect().height, clipped: p.querySelector('span').scrollWidth > p.querySelector('span').clientWidth })), all: b.innerText.split('\n').filter(Boolean) }));
    check('a talk shows his first line in it and her first reply to that, one line each and clipped to the row (his line break is a space), and 共 6 句 under them', talk.lines.length === 2 && talk.lines[0].who === '你' && talk.lines[0].text === '帮我看看今天下午有什么安排，顺便提醒我给银行打电话 这件事别忘了' && talk.lines[1].who === '她' && talk.lines[1].text.startsWith('下午三点有一个小组讨论')
      && talk.lines.every(x => x.h <= 20) && talk.lines[1].clipped && talk.all.includes('共 6 句'), JSON.stringify(talk));
    await shotRow(a, 'open-03-talk', 'talk-1');
    check('the day\'s records were read once for it, a page of 300 from the start', a.s.reads.length === 1 && a.s.reads[0] === '/inherent/memory/day/2026-10-19/records?limit=300&offset=0', a.s.reads.join());
    await tap(a, 'talk-3'); await rest(a, 480);
    check('another talk of the same day does not read them again', a.s.reads.length === 1 && (await said(a, 'talk-3')).join('|') === '你|27 路下一班什么时候|她|还有 6 分钟。|共 1 句|问她', (await said(a, 'talk-3')).join('|'));

    // pictures
    await tap(a, 'picture-1'); await rest(a, 600);
    const small = await orow(a, 'picture-1').evaluate(r => { const b = r.querySelector('.dt-pic'), i = b.querySelector('img'), c = getComputedStyle(i), q = x => x.getBoundingClientRect(), note = r.querySelector('.is-note'); return { w: Math.round(q(i).width), h: Math.round(q(i).height), max: c.maxHeight, radius: c.borderRadius, box: getComputedStyle(b).borderRadius, nat: [i.naturalWidth, i.naturalHeight], cursor: getComputedStyle(b).cursor, note: note.innerText, noteBreaks: getComputedStyle(note).whiteSpace }; });
    check('a picture is read from the daemon by its id and shown as it is (96 x 96 here), with a 240 tall cap and 10 px round, and its note in full under it, line break and all', small.nat.join('x') === '96x96' && small.w === 96 && small.h === 96 && small.max === '240px' && small.radius === '10px' && small.box === '10px' && small.note === '汉堡和薯条\n在 A&W 吃的午饭' && small.cursor === 'zoom-in' && a.s.pictures.some(x => x.endsWith(PIC)), JSON.stringify(small));
    await tap(a, 'picture-2'); await rest(a, 700);
    const big = await orow(a, 'picture-2').evaluate(r => { const i = r.querySelector('.dt-pic img'), q = i.getBoundingClientRect(); return { w: Math.round(q.width), h: Math.round(q.height), nat: [i.naturalWidth, i.naturalHeight] }; });
    check('one taller than 240 is cut to 240 and keeps its proportions (640 x 420 shows at 366 x 240); its note is one short line, which the row already says, so it is not said again', big.nat.join('x') === '640x420' && big.h === 240 && Math.abs(big.w - 366) <= 1 && await orow(a, 'picture-2').locator('.is-note').count() === 0, JSON.stringify(big));
    await shotRow(a, 'open-04-picture', 'picture-2');

    // the bars of a work block: the same columns on every line, a bar as long as its share of the active time
    await tap(a, 'work-2'); await rest(a, 520);
    const bars = await orow(a, 'work-2').evaluate(r => [...r.querySelectorAll('.dt-bar-row')].map(x => { const [name, track, len] = x.children; return { name: name.textContent, track: Math.round(track.getBoundingClientRect().width * 10) / 10, left: Math.round(track.getBoundingClientRect().left), share: Math.round(track.firstElementChild.getBoundingClientRect().width / track.getBoundingClientRect().width * 100), len: len.textContent }; }));
    check('work: Jarvis 87 % of the active time, Ghostty 61 %, Claude 26 %, in tracks of one width that start at one place', bars.map(x => `${x.name} ${x.share}`).join() === 'Jarvis 87,Ghostty 61,Claude 26' && new Set(bars.map(x => x.track)).size === 1 && new Set(bars.map(x => x.left)).size === 1, JSON.stringify(bars));

    // the night
    await tap(a, 'sleep-1'); await rest(a, 520);
    const night = await orow(a, 'sleep-1').evaluate(r => { const plot = r.querySelector('.dt-hyp-plot'), w = plot.getBoundingClientRect().width; return { n: plot.querySelectorAll('i').length, joins: plot.querySelectorAll('u').length, tops: [...new Set([...plot.querySelectorAll('i')].map(x => x.style.top))].sort(), lefts: [...plot.querySelectorAll('i')].map(x => parseFloat(x.style.left)), h: Math.round(plot.getBoundingClientRect().height), w, labels: [...r.querySelectorAll('.dt-hyp-l span')].map(x => x.textContent) }; });
    const slices = openOf(full, 'sleep-1', zh).parts.find(x => x.type === 'night').night.slices.filter(x => x.stage !== 'in_bed');
    check('a night is drawn as a hypnogram 48 tall: four lanes (清醒, REM, 核心, 深睡), a block for each stretch of a stage in the order they happened, and a hairline where a stage gives way to another',
      night.h === 48 && night.labels.join() === '清醒,REM,核心,深睡' && night.n === slices.length && night.tops.join() === '14px,2px,26px,38px'.split(',').sort().join() && night.lefts.every((x, i, l) => i === 0 || x >= l[i - 1]) && night.joins > 5 && night.joins < night.n, JSON.stringify(night));

    // Claude Code: four sessions, each opens to its recap in full, one at a time
    await tap(a, 'code:stay-home-1'); await rest(a, 520);
    await shotRow(a, 'open-10-claude-code', 'code:stay-home-1');
    const sess = () => orow(a, 'code:stay-home-1').evaluate(r => ({ n: r.querySelectorAll('.dt-sess li').length, recaps: [...r.querySelectorAll('.dt-recap')].map(x => x.textContent), open: [...r.querySelectorAll('.dt-sess button[aria-expanded="true"]')].length }));
    let sv = await sess();
    check('a folded row opens to its sessions: four, none opened yet', sv.n === 4 && sv.recaps.length === 0 && sv.open === 0, JSON.stringify(sv));
    await orow(a, 'code:stay-home-1').locator('.dt-sess button').first().click(); await rest(a, 420);
    sv = await sess();
    check('a session opens to its recap in full', sv.recaps.join() === 'Phase A 的 review 修复 的小结' && sv.open === 1, JSON.stringify(sv));
    await shotRow(a, 'open-11-claude-code-recap', 'code:stay-home-1');
    await orow(a, 'code:stay-home-1').locator('.dt-sess button').nth(2).click(); await rest(a, 420);
    sv = await sess();
    check('another session opens its own and closes the first; the row itself stays open', sv.recaps.join() === '给 verify 加 fixtures 的小结' && sv.open === 1 && await expanded(a, 'code:stay-home-1') === 'true', JSON.stringify(sv));
    await orow(a, 'code:stay-home-1').locator('.dt-sess button').nth(2).click(); await rest(a, 420);
    check('and a click on the open session closes it', (await sess()).recaps.length === 0);
    await a.ctx.close();
  }
  // a row with nothing to show and nothing to do (a bus whose id the brain would refuse in a turn, and whose two lines and right column say it all) stays as it is
  if (all || only === 'open') {
    const a = await boot({ served: makeDay({ items: [...makeDay().items, it('bus 27', 'departure', T(22, 50), T(22, 50), { title: '27 · Shelbourne at Pear', route: '27', board_stop: 'Shelbourne at Pear', to: 'UVic', leave_at_ms: T(22, 43) })] }) });
    await openDay(a);
    const still = await a.page.evaluate(() => { const r = document.querySelector('[data-key="bus 27"]'); r.scrollIntoView({ block: 'center' }); return { cursor: getComputedStyle(r).cursor, expanded: r.getAttribute('aria-expanded'), still: r.classList.contains('is-still') }; });
    await tap(a, 'bus 27'); await rest(a, 400);
    check('a row with nothing to show and nothing to do is drawn as it is: no pointer, no expanded state, and a click opens nothing', still.cursor === 'default' && still.expanded === null && still.still && await orow(a, 'bus 27').getAttribute('aria-expanded') === null && await folds(a) === 0 && await orow(a, 'bus 27').locator('.dt-ask').count() === 0, JSON.stringify(still));
    await a.ctx.close();
  }
  // the talk's words, when the day's records cannot be read
  if (all || only === 'open') {
    const a = await boot({ served: bigDay() });
    a.s.refuse.add('records');
    await openDay(a);
    await tap(a, 'talk-1'); await rest(a, 600);
    check('when the records cannot be read the talk says so, in one line, and still has its count and its button', (await said(a, 'talk-1')).join('|') === '这段对话的字还没读到|共 6 句|问她', (await said(a, 'talk-1')).join('|'));
    await a.ctx.close();
  }

  // ---- the name of a place he keeps, changed from the stay's own row ----
  // These run on a day opened by a rest (not a click), so the window has no key focus until a field asks for it.
  const hoverDay = async a => {
    await a.until(() => document.querySelector('.notch')?.dataset.line);
    const seg = await a.lineX();
    await a.glide(a.far, { x: seg.mid, y: 14 }, 300);
    await a.until(() => document.querySelector('.notch-drop.is-open .dt-row'), undefined, 8000);
    await a.page.waitForTimeout(800);
    return seg;
  };
  const focusLog = a => a.page.evaluate(() => (window.__focus ?? []).join());
  const sent = a => a.s.posts.map(p => `${p.path} ${JSON.stringify(p.body)}`);
  if (all || only === 'open') {
    const a = await boot({ served: bigDay() });
    await hoverDay(a);
    const names = () => a.page.evaluate(() => ({ heads: [...document.querySelectorAll('.dt-sh')].map(h => h.querySelector('.dt-tx b')?.textContent ?? h.dataset.key).filter(Boolean), bar: [...document.querySelectorAll('.dt-seg b')].map(b => b.textContent) }));
    await tap(a, 'stay-home-1'); await rest(a, 480);
    check('a hover-opened pane has no key focus, and opening a row asks for none', await focusLog(a) === '' && (await a.read()).tl === 'open', await focusLog(a));
    await orow(a, 'stay-home-1').getByRole('button', { name: '改名字' }).click(); await rest(a, 400);
    const field = await a.page.evaluate(() => { const f = document.querySelector('.dt-rename'), q = f.getBoundingClientRect(), b = f.closest('.dt-sh').querySelector('.dt-dot').getBoundingClientRect(); return { value: f.value, sel: [f.selectionStart, f.selectionEnd], focused: document.activeElement === f, h: Math.round(q.height), font: getComputedStyle(f).fontSize + '/' + getComputedStyle(f).fontWeight, outline: getComputedStyle(f).outlineStyle, hint: document.querySelector('.dt-hint')?.textContent, buttons: [...document.querySelectorAll('[data-key="stay-home-1"] .dt-acts .dt-btn, [data-key="stay-home-1"] .dt-sayhow')].map(x => x.textContent) }; });
    check('改名字 turns the name in the stay\'s header into a field with the cursor in it and the name selected, in the header\'s own type, and says Enter saves and Esc cancels', field.value === '家' && field.sel.join() === '0,1' && field.focused && field.h === 26 && field.font === '15px/600' && field.outline === 'none' && field.hint === '回车保存 · Esc 取消' && field.buttons.join() === '问她,这段在干嘛', JSON.stringify(field));
    check('the field takes the window\'s key focus (it was a pane opened by a rest, with none)', (await focusLog(a)).endsWith('true'), await focusLog(a));
    await shotRow(a, 'open-13-rename-field', 'stay-home-1');
    // the pane is held while the field is there, wherever the pointer goes
    await a.move(a.far.x, a.far.y); await a.page.waitForTimeout(1300);
    check('and the pane stays up while the field has the cursor, though the pointer has gone', (await a.read()).tl === 'open' && (await a.read()).dt, JSON.stringify(await a.read()));
    const back = await orow(a, 'stay-home-1').boundingBox();
    await a.move(back.x + back.width / 2, back.y + 20);
    await a.page.keyboard.type('新家'); await a.page.keyboard.press('Enter'); await a.page.waitForTimeout(500);
    check('Enter saves it: the one route the brain has for it, with exactly the new name', sent(a).join('|') === '/inherent/memory/places/p-home {"name":"新家"}', sent(a).join('|'));
    const renamed = await names();
    check('the day is read again and every home shows it at once: the three stays\' headers and the bar', renamed.heads.filter(x => x === '新家').length === 3 && !renamed.heads.includes('家') && renamed.bar.includes('新家') && !renamed.bar.includes('家'), JSON.stringify(renamed));
    const note = await a.page.locator('.dt-note').innerText();
    check('a quiet line says 改好了 · 撤销, and the field is gone, the row still open', note === '改好了 · 撤销' && await a.page.locator('.dt-rename').count() === 0 && await expanded(a, 'stay-home-1') === 'true', note);
    await a.shot('open-14-rename-note', { x: 40, y: 0, width: 560, height: 760 });
    await a.page.waitForTimeout(900);
    await a.shot('open-14b-wing-after-rename', { x: 200, y: 0, width: 300, height: 60 });
    // the wing's line is the open pane's too: a longer name for the place pushes the wing's edge out; the words do not run past it and lose a digit
    const wing = await a.page.evaluate(() => {
      const n = document.querySelector('.notch'), [x0, x1] = n.dataset.lineAt.split('-').map(Number), words = n.dataset.line, cut = words.lastIndexOf(' · '), ctx = document.createElement('canvas').getContext('2d');
      ctx.font = '600 12px -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif'; const head = ctx.measureText(`${words.slice(0, cut)} · `).width;
      ctx.font = '600 10.5px "JetBrains Mono", Menlo, monospace'; const tail = ctx.measureText(words.slice(cut + 3)).width;
      return { words, x0, x1, drawn: Math.round((head + tail) * 10) / 10, right: Math.round(n.querySelector('.notch-hit').getBoundingClientRect().right * 10) / 10 };
    });
    check('the wing says the new name whole and in its room: the slot holds the words as drawn, and the wing reaches past it', wing.words.startsWith('新家 · ') && wing.x0 + wing.drawn <= wing.x1 + 1 && wing.right - wing.x1 >= 3, JSON.stringify(wing));
    await a.page.getByRole('button', { name: '撤销' }).click(); await a.page.waitForTimeout(500);
    const undone = await names();
    check('撤销 asks the brain to undo that change by its id, and the name comes back everywhere', sent(a).at(-1) === '/inherent/memory/places/undo {"change_id":"chg-1"}' && undone.heads.filter(x => x === '家').length === 3 && undone.bar.includes('家') && await a.page.locator('.dt-note').count() === 0, `${sent(a).at(-1)} ${JSON.stringify(undone)}`);
    // Esc lets go of the field and leaves the row; nothing is sent
    await orow(a, 'stay-home-1').getByRole('button', { name: '改名字' }).click(); await rest(a, 300);
    await a.page.keyboard.type('乱写的'); await a.page.keyboard.press('Escape'); await rest(a, 300);
    check('Esc lets go of the name being changed first: nothing is sent, the name is as it was, and the row stays open', sent(a).length === 2 && (await names()).heads.filter(x => x === '家').length === 3 && await a.page.locator('.dt-rename').count() === 0 && await expanded(a, 'stay-home-1') === 'true', sent(a).join('|'));
    check('and the key focus goes back when the field is gone', (await focusLog(a)).endsWith('false'), await focusLog(a));
    // the same name, or none, sends nothing
    await orow(a, 'stay-home-1').getByRole('button', { name: '改名字' }).click(); await rest(a, 300);
    await a.page.keyboard.press('Enter'); await rest(a, 300);
    check('Enter on the name as it was sends nothing and closes the field', sent(a).length === 2 && await a.page.locator('.dt-rename').count() === 0);
    // a refusal says so and keeps the field
    a.s.refuse.add('rename');
    await orow(a, 'stay-home-1').getByRole('button', { name: '改名字' }).click(); await rest(a, 300);
    await a.page.keyboard.type('新家'); await a.page.keyboard.press('Enter'); await rest(a, 500);
    const refusedView = { field: await a.page.locator('.dt-rename').inputValue(), hint: await a.page.locator('.dt-hint').innerText(), heads: (await names()).heads, notes: await a.page.locator('.dt-note').count() };
    check('a rename the brain refuses says so under the field, keeps the field with what was typed, and changes no name', refusedView.field === '新家' && refusedView.hint === '没改成，再试一次' && refusedView.heads.filter(x => x === '家').length === 2 && refusedView.notes === 0, JSON.stringify(refusedView));
    a.s.refuse.delete('rename');
    await a.page.keyboard.press('Enter'); await rest(a, 500);
    check('and Enter again, when it can, saves it', (await names()).heads.filter(x => x === '新家').length === 3 && await a.page.locator('.dt-note').innerText() === '改好了 · 撤销');
    await a.page.waitForTimeout(5300);
    check('the line goes by itself after 5 s', await a.page.locator('.dt-note').count() === 0);
    await a.ctx.close();
  }

  // ---- 知道了: a reminder that rang is acknowledged on the brain; the badge fades and the count rolls down ----
  if (all || only === 'open') {
    const a = await boot({ served: bigDay() });
    await hoverDay(a);
    const pill = () => a.page.locator('.dt-waits').innerText().catch(() => null);
    check('two things wait on him at first: the reminder that rang and the event that is not his yet', await pill() === '2 件等你' && await orow(a, 'reminder-2').locator('.dt-wait').count() === 1, await pill());
    await tap(a, 'reminder-2'); await rest(a, 480);
    const reads = a.s.hits.length;
    // read while it happens: the old number still in the pill rolling out, the badge going (not cut), then gone
    const watching = a.page.evaluate(async () => { const out = { rolled: false, faded: false }; const end = performance.now() + 900; while (performance.now() < end) { out.rolled ||= !!document.querySelector('.dt-roll .is-old'); out.faded ||= !!document.querySelector('[data-key="reminder-2"] .dt-wait.is-gone'); await new Promise(r => requestAnimationFrame(r)); } out.gone = !document.querySelector('[data-key="reminder-2"] .dt-wait'); return out; });
    await orow(a, 'reminder-2').getByRole('button', { name: '知道了' }).click();
    const seen = await watching;
    check('知道了 sends the one thing the brain takes for it: the reminder\'s id and "dismissed"', sent(a).join('|') === '/inherent/notices/reminder-2 {"action":"dismissed"}', sent(a).join('|'));
    check('the amber badge fades out and goes, and the count in the header rolls down (2 to 1) instead of changing', seen.rolled && seen.faded && seen.gone && await pill() === '1 件等你', JSON.stringify({ seen, pill: await pill() }));
    check('the button is gone from the row, which says it is known (the row\'s own second line: 你知道了); the day was read again, and the brain has it acknowledged', (await said(a, 'reminder-2')).join('|') === '问她' && await orow(a, 'reminder-2').locator('.dt-tx em').innerText() === '你知道了' && a.s.hits.length > reads && a.s.served.items.find(x => x.id === 'reminder-2').acknowledged === true, `${(await said(a, 'reminder-2')).join('|')} ${a.s.hits.length} ${reads}`);
    await a.ctx.close();
  }
  if (all || only === 'open') {
    // when the brain refuses (a 404: it was not a reminder that rang), the button stays and says so; nothing changes
    const a = await boot({ served: bigDay() });
    a.s.refuse.add('notices');
    await hoverDay(a);
    await tap(a, 'reminder-2'); await rest(a, 480);
    await orow(a, 'reminder-2').getByRole('button', { name: '知道了' }).click(); await rest(a, 500);
    check('a refusal leaves the button and the badge and the count as they were, and says so under the button', await orow(a, 'reminder-2').getByRole('button', { name: '知道了' }).count() === 1 && await orow(a, 'reminder-2').locator('.dt-wait').count() === 1 && await a.page.locator('.dt-waits').innerText() === '2 件等你' && await orow(a, 'reminder-2').locator('.dt-hint').innerText() === '没成功，再试一次');
    await a.ctx.close();
  }
  if (all || only === 'open') {
    // the last thing that waited: the pill goes, fading, instead of showing 0
    const a = await boot({ served: small([it('rm', 'reminder', T(9, 30), null, { text: '给银行打电话', fired: true, acknowledged: false }, 'her')]) });
    await hoverDay(a);
    check('with one thing waiting the pill says 1 件等你', await a.page.locator('.dt-waits').innerText() === '1 件等你');
    await tap(a, 'rm'); await rest(a, 480);
    const watching = a.page.evaluate(async () => { let faded = false; const end = performance.now() + 900; while (performance.now() < end) { faded ||= !!document.querySelector('.dt-waits.is-gone'); await new Promise(r => requestAnimationFrame(r)); } return faded; });
    await orow(a, 'rm').getByRole('button', { name: '知道了' }).click();
    check('and when it is known the pill fades away and is gone: no 0', await watching && await a.page.locator('.dt-waits').count() === 0);
    await a.ctx.close();
  }

  // ---- 问她: an input at the pane's foot about a row; Enter hands the turn to the daemon's one submit route, with `about` (ADR 0214) ----
  const dockOf = a => a.page.evaluate(() => {
    const d = document.querySelector('.dt-dock'), c = d?.querySelector('.dt-about'), f = d?.querySelector('.dt-ask-in'), go = d?.querySelector('.dt-send');
    return d && { sent: d.classList.contains('is-sent'), text: d.textContent, chip: c ? { title: c.querySelector('b').textContent, at: c.querySelector('time').textContent, icon: !!c.querySelector('svg'), x: c.querySelector('button').getAttribute('aria-label') } : null,
      draft: f ? f.value : null, focused: !!f && document.activeElement === f, placeholder: f?.placeholder ?? null, canSend: go ? !go.disabled : null };
  });
  const turns = a => a.s.posts.filter(p => p.path === '/inherent/submit');
  const noDock = a => a.page.waitForFunction(() => !document.querySelector('.dt-dock'), null, { timeout: 4000 });
  if (all || only === 'open') {
    const m = buildDay(bigDay(), NOW, zh), about = key => askOf(m, key, zh);
    const a = await boot({ served: bigDay() });
    await hoverDay(a);
    const marks = await a.page.evaluate(() => { const by = {}; for (const el of document.querySelectorAll('.dt-row, .dt-sh')) { const k = el.classList.contains('dt-sh') ? 'stay' : el.dataset.kind; by[k] ??= { rows: 0, asks: 0 }; by[k].rows++; if (el.querySelector(':scope > .dt-quick > .dt-ask')) by[k].asks++; } return by; });
    const yes = ['stay', 'move', 'sleep', 'talk', 'picture', 'reminder', 'event', 'work', 'call', 'ride', 'departure', 'payment', 'claude_chat', 'claude_day', 'workout'], no = ['code'];
    check('only what the brain takes a turn about has 问她 (stays, moves, sleep, talks, pictures, reminders, events, work, calls, rides, buses, payments, Claude chats and days, runs); the folded rows of sessions, no one item, have none', yes.every(k => marks[k]?.rows > 0 && marks[k].asks === marks[k].rows) && no.every(k => marks[k]?.rows > 0 && marks[k].asks === 0), JSON.stringify(marks));
    await orow(a, 'talk-1').scrollIntoViewIfNeeded();
    await orow(a, 'talk-1').hover({ position: { x: 170, y: 14 } }); await rest(a, 300);
    const lit = await a.page.evaluate(() => { const r = document.querySelector('[data-key="talk-1"]'), q = r.querySelector(':scope > .dt-quick'), tm = r.querySelector(':scope > .dt-tm'); return { ask: getComputedStyle(q).opacity, tm: getComputedStyle(tm).opacity, text: q.querySelector('.dt-ask').textContent }; });
    check('under the pointer a row says 问她 (after its star) in the place of its length', lit.ask === '1' && lit.tm === '0' && lit.text === '问她', JSON.stringify(lit));
    await orow(a, 'talk-1').locator(':scope > .dt-quick .dt-ask').click(); await rest(a, 520);
    const d1 = await dockOf(a);
    check('it slides an input up at the pane\'s foot: what it is about as a chip (the row\'s glyph, its title, its time, a ×), the cursor in the field, send greyed while nothing is typed; the row itself did not open', d1 && d1.chip.title === '和她说话' && d1.chip.at === '10:02' && d1.chip.icon && d1.chip.x === '去掉这一条' && d1.focused && d1.placeholder === '问她这一条…' && d1.canSend === false && await expanded(a, 'talk-1') === 'false' && await folds(a) === 0, JSON.stringify(d1));
    check('the field takes the window\'s key focus (the pane was opened by a rest and had none)', (await focusLog(a)).endsWith('true'), await focusLog(a));
    await a.page.keyboard.type('明天下午有空吗'); await rest(a, 150);
    check('typing enables send', (await dockOf(a)).canSend === true && (await dockOf(a)).draft === '明天下午有空吗');
    await a.shot('open-15-ask-input', { x: 40, y: 0, width: 560, height: 760 });
    await a.move(a.far.x, a.far.y); await a.page.waitForTimeout(1300);
    check('the pane stays up while the field has the cursor, though the pointer has gone', (await a.read()).tl === 'open' && (await a.read()).dt && !!await dockOf(a), JSON.stringify(await a.read()));
    const t0 = Date.now();
    await a.page.keyboard.press('Enter'); await rest(a, 350);
    check('Enter hands her the turn: one submit with the text and what it is about (kind, id, title, start) and nothing else', turns(a).length === 1 && JSON.stringify(turns(a)[0].body) === JSON.stringify({ text: '明天下午有空吗', about: about('talk-1').about }) && about('talk-1').about.start_ms === T(10, 2), JSON.stringify(turns(a)));
    const s1 = await dockOf(a);
    check('then 已发给她 stands where the input was (the field and the chip are gone)', s1.sent && s1.text.trim() === '已发给她' && s1.chip === null && s1.draft === null, JSON.stringify(s1));
    check('the pane is still up while it says so', (await a.read()).tl === 'open');
    await noDock(a); const gone = Date.now() - t0;
    check('1.5 s after it was sent the line slides away', gone >= 1450 && gone < 2500, String(gone));
    check('with the pointer away the pane folds once the input is gone', (await seen(a, r => r.tl === '', 2500)).tl === '');
    check('the key focus goes back with it', (await focusLog(a)).endsWith('false'), await focusLog(a));

    // a second look: the button in an open row, asking about another row while one is asked, the ×, and the field being held
    await hoverDay(a);
    await tap(a, 'stay-aw'); await rest(a, 480);
    await orow(a, 'stay-aw').getByRole('button', { name: '问她' }).click(); await rest(a, 520);
    const d2 = await dockOf(a);
    check('the 问她 button of an open row does the same, and the row stays open', d2 && d2.chip.title === about('stay-aw').about.title && d2.chip.at === about('stay-aw').at && d2.focused && await expanded(a, 'stay-aw') === 'true', JSON.stringify(d2));
    await a.page.keyboard.type('abc');
    await orow(a, 'event-1').scrollIntoViewIfNeeded();
    await orow(a, 'event-1').hover({ position: { x: 170, y: 14 } }); await rest(a, 250);
    await orow(a, 'event-1').locator(':scope > .dt-quick .dt-ask').click(); await rest(a, 450);
    const d3 = await dockOf(a);
    check('asking about another row swaps the chip and keeps what was typed, the cursor back in the field', d3 && d3.chip.title === about('event-1').about.title && d3.chip.at === about('event-1').at && d3.draft === 'abc' && d3.focused && turns(a).length === 1, JSON.stringify(d3));
    await a.page.locator('.dt-about button').click(); await rest(a, 250);
    const d4 = await dockOf(a);
    check('the × lets go of the chip: the turn is then asked alone, and the words and the cursor stay', d4.chip === null && d4.draft === 'abc' && d4.focused && d4.placeholder === '问她…', JSON.stringify(d4));
    await a.page.keyboard.press('Enter'); await rest(a, 350);
    check('and it goes with the text alone, no about', turns(a).length === 2 && JSON.stringify(turns(a)[1].body) === '{"text":"abc"}', JSON.stringify(turns(a)));
    await noDock(a);

    // a turn the daemon refuses, and the order of Esc
    await tap(a, 'move-1'); await rest(a, 480);
    await orow(a, 'move-1').getByRole('button', { name: '问她' }).click(); await rest(a, 500);
    a.s.refuse.add('submit');
    await a.page.keyboard.type('xyz'); await a.page.keyboard.press('Enter'); await rest(a, 450);
    const d5 = await dockOf(a), hint = await a.page.locator('.dt-dock + .dt-hint').innerText().catch(() => null);
    check('a turn the daemon refuses says so under the input and keeps the chip and the words', hint === '没发出去，再按一次回车试试' && d5.draft === 'xyz' && !!d5.chip && !d5.sent && d5.focused, JSON.stringify({ hint, d5 }));
    await a.page.keyboard.type('!'); await rest(a, 150);
    check('the next key takes the hint away', await a.page.locator('.dt-dock + .dt-hint').count() === 0);
    a.s.refuse.delete('submit');
    await a.page.keyboard.press('Enter'); await rest(a, 350);
    check('and Enter again, when it can, sends the same words', turns(a).length === 4 && JSON.stringify(turns(a)[3].body) === JSON.stringify({ text: 'xyz!', about: about('move-1').about }) && (await dockOf(a)).sent, JSON.stringify(turns(a).slice(2)));
    await noDock(a);
    await orow(a, 'move-1').getByRole('button', { name: '问她' }).click(); await rest(a, 500);
    check('the words that were sent do not come back into the next input', (await dockOf(a)).draft === '');
    await a.page.keyboard.type('half'); await a.page.keyboard.press('Escape'); await rest(a, 450);
    check('Esc lets go of the input first (and what was typed in it); the open row and the pane stay', await a.page.locator('.dt-dock').count() === 0 && await expanded(a, 'move-1') === 'true' && (await a.read()).tl === 'open' && turns(a).length === 4, JSON.stringify(await a.read()));
    await a.page.keyboard.press('Escape'); await rest(a, 450);
    check('and the next Esc closes the row', await folds(a) === 0 && (await a.read()).tl === 'open');
    await orow(a, 'move-1').getByRole('button', { name: '问她' }).count();
    await tap(a, 'move-1'); await rest(a, 480);
    await orow(a, 'move-1').getByRole('button', { name: '问她' }).click(); await rest(a, 500);
    check('and the words left in a closed input are gone', (await dockOf(a)).draft === '');
    await a.ctx.close();
  }

  // ---- 问她 on a run and on a Claude Code session, kinds the brain takes (main a8ebde17); the folded row of sessions is no one item ----
  if (all || only === 'open') {
    const a = await boot({ served: small([it('w1', 'workout', T(7, 0), T(7, 30), { activity: 'running', distance_m: 3200 }), it('sa', 'stay', T(8, 0), T(9, 30), { place: 'X' }), it('c1', 'code', T(8, 10), T(8, 40), { title: '周报草稿', project: 'notes', account: 'b' }),
      it('sb', 'stay', T(10, 0), T(12, 0), { place: 'Y' }), it('c2', 'code', T(10, 10), T(10, 40), { title: '第二个' }), it('c3', 'code', T(10, 50), T(11, 20), { title: '第三个' })]) });
    await hoverDay(a);
    const btns = key => orow(a, key).locator('.dt-acts .dt-btn, .dt-acts .dt-sent, .dt-sayhow').allInnerTexts();
    for (const [key, kind, title, start] of [['w1', 'workout', '跑步', T(7, 0)], ['c1', 'code', 'Claude Code · 周报草稿', T(8, 10)]]) {
      await tap(a, key); await rest(a, 480);
      check(`an opened ${kind} row offers 问她`, (await btns(key)).includes('问她'), (await btns(key)).join('|'));
      await orow(a, key).getByRole('button', { name: '问她' }).click(); await rest(a, 500);
      const d = await dockOf(a);
      check(`its input holds ${key} as the chip: the row's own title and time`, d && d.chip.title === title && d.chip.at === clock(start, TZ), JSON.stringify(d));
      await a.page.keyboard.type('怎么样'); await a.page.keyboard.press('Enter'); await rest(a, 400);
      check(`and the turn carries {kind: ${kind}, id: ${key}} with its title and start`, JSON.stringify(turns(a).at(-1)?.body) === JSON.stringify({ text: '怎么样', about: { kind, id: key, title, start_ms: start } }), JSON.stringify(turns(a)));
      await noDock(a); await tap(a, key); await rest(a, 480);
    }
    await tap(a, 'code:sb'); await rest(a, 480);
    check('the folded row of two sessions opens to its list and offers no 问她, in the row or on the hover', await a.page.locator('[data-key="code:sb"] .dt-sess li').count() === 2 && !(await btns('code:sb')).includes('问她') && await orow(a, 'code:sb').locator(':scope > .dt-quick > .dt-ask').count() === 0, (await btns('code:sb')).join('|'));
    await a.ctx.close();
  }

  // ---- 让她总结这段: one tap hands her the work block, no input ----
  if (all || only === 'open') {
    const m = buildDay(bigDay(), NOW, zh), want = JSON.stringify({ text: '总结一下这一段', about: askOf(m, 'work-2', zh).about });
    const a = await boot({ served: bigDay() });
    await hoverDay(a);
    await tap(a, 'work-2'); await rest(a, 520);
    const buttons = () => orow(a, 'work-2').locator('.dt-acts, .dt-sayhow').allInnerTexts().then(x => x.join('\n').split('\n').map(y => y.trim()).filter(Boolean).join('|'));
    check('a work block offers 让她总结这段, and 问她 and the quiet 这段在干嘛 after it', await buttons() === '让她总结这段|问她|这段在干嘛', await buttons());
    a.s.refuse.add('submit');
    await orow(a, 'work-2').getByRole('button', { name: '让她总结这段' }).click(); await rest(a, 450);
    check('one tap is one submit; the daemon refusing it leaves the button, which says 没发出去，再试一次', turns(a).length === 1 && await buttons() === '没发出去，再试一次|问她|这段在干嘛', `${turns(a).length} ${await buttons()}`);
    a.s.refuse.delete('submit');
    await orow(a, 'work-2').getByRole('button', { name: '没发出去，再试一次' }).click(); await rest(a, 450);
    check('and the next tap sends the same: the words and the block as `about` (work, its id, its title, its start), without any input opening', turns(a).length === 2 && turns(a).every(x => JSON.stringify(x.body) === want) && (await dockOf(a)) === null, JSON.stringify(turns(a)));
    check('then 已发给她 stands where the button was; 问她 stays', await buttons() === '已发给她|问她|这段在干嘛', await buttons());
    await shotRow(a, 'open-12-work-sent', 'work-2');
    await a.ctx.close();
  }

  // ---- the picture large: a click on it, over the window; Esc or a click closes it ----
  if (all || only === 'open') {
    const a = await boot({ served: bigDay() });
    await hoverDay(a);
    await tap(a, 'picture-2'); await rest(a, 700);
    await a.page.locator('[data-key="picture-2"] .dt-pic').click(); await rest(a, 500);
    const v = await a.page.evaluate(() => { const d = document.querySelector('.dt-viewer'), i = d?.querySelector('img'), r = i?.getBoundingClientRect(); return d && { hit: d.hasAttribute('data-hit'), body: d.parentElement === document.body, src: i.getAttribute('src'), w: Math.round(r.width), h: Math.round(r.height), vw: innerWidth, vh: innerHeight, note: d.querySelector('p')?.textContent, role: d.getAttribute('role'), covers: (b => b.width === innerWidth && b.height === innerHeight)(d.getBoundingClientRect()) }; });
    check('a click on the picture opens it large over the whole window: the daemon\'s picture at 88% of the width and 78% of the height, its note in full under it, on a surface of its own', v && v.hit && v.body && v.src.endsWith(`/inherent/attachments/${BIG}`) && v.w === Math.round(v.vw * .88) && v.h === Math.round(v.vh * .78) && v.note === '傍晚的海边，风很大' && v.role === 'dialog' && v.covers, JSON.stringify(v));
    check('it takes the window\'s key focus, and the row under it stays open', (await focusLog(a)).endsWith('true') && await expanded(a, 'picture-2') === 'true', await focusLog(a));
    await a.shot('open-16-picture-large', { x: 0, y: 0, width: 640, height: 900 });
    await a.page.keyboard.press('Escape'); await rest(a, 350);
    check('Esc closes the picture first: the row stays open and so does the pane, and the key focus goes back', await a.page.locator('.dt-viewer').count() === 0 && await expanded(a, 'picture-2') === 'true' && (await a.read()).tl === 'open' && (await focusLog(a)).endsWith('false'), `${JSON.stringify(await a.read())} ${await focusLog(a)}`);
    await a.page.locator('[data-key="picture-2"] .dt-pic').click(); await rest(a, 400);
    await a.page.mouse.click(320, 450); await rest(a, 350);
    check('a click on it closes it too', await a.page.locator('.dt-viewer').count() === 0 && await expanded(a, 'picture-2') === 'true');
    await a.ctx.close();
  }

  // ---- English ----
  if (all || only === 'open') {
    const m = buildDay(bigDay(), NOW, en);
    const a = await boot({ served: bigDay(), lang: 'en' });
    await hoverDay(a);
    const btns = key => orow(a, key).locator('.dt-acts .dt-btn, .dt-acts .dt-sent, .dt-sayhow').allInnerTexts();
    await tap(a, 'stay-home-1'); await rest(a, 480);
    check('in English a kept place (named as he named it) says Rename and Ask her', (await btns('stay-home-1')).join() === 'Rename,Ask her,What was this' && (await said(a, 'stay-home-1')).includes('A place you keep · 家'), (await said(a, 'stay-home-1')).join('|'));
    await orow(a, 'stay-home-1').getByRole('button', { name: 'Rename' }).click(); await rest(a, 350);
    check('and Rename says how it ends', await orow(a, 'stay-home-1').locator('.dt-hint').innerText() === 'Enter saves · Esc cancels');
    await a.page.keyboard.type('My place'); await a.page.keyboard.press('Enter'); await rest(a, 500);
    check('a name changed says Renamed · Undo', await a.page.locator('.dt-note').innerText() === 'Renamed · Undo', await a.page.locator('.dt-note').innerText());
    await tap(a, 'reminder-2'); await rest(a, 480);
    check('a reminder that rang offers Got it, and Ask her', (await btns('reminder-2')).join() === 'Got it,Ask her' && (await said(a, 'reminder-2')).join('|') === 'Got it|Ask her', (await said(a, 'reminder-2')).join('|'));
    await tap(a, 'talk-1'); await rest(a, 700);
    const talk = await a.page.evaluate(() => { const r = document.querySelector('[data-key="talk-1"]'); return { who: [...r.querySelectorAll('.dt-said small')].map(x => x.textContent), text: r.querySelector('.dt-body').innerText }; });
    check('a talk labels the two lines You and Her and counts its lines', talk.who.join() === 'You,Her' && /6 lines in all/.test(talk.text), JSON.stringify(talk));
    await tap(a, 'work-2'); await rest(a, 520);
    check('a work block offers Have her sum this up', (await btns('work-2')).join() === 'Have her sum this up,Ask her,What was this' && (await said(a, 'work-2')).includes('Active 1 h 55 min · 2 h 5 min in all'), (await said(a, 'work-2')).join('|'));
    await orow(a, 'work-2').getByRole('button', { name: 'Have her sum this up' }).click(); await rest(a, 450);
    check('it hands her the words in English, with the block as about; then Sent to her', turns(a).length === 1 && JSON.stringify(turns(a)[0].body) === JSON.stringify({ text: 'Summarize this stretch', about: askOf(m, 'work-2', en).about }) && (await btns('work-2')).join() === 'Sent to her,Ask her,What was this', `${JSON.stringify(turns(a))} ${(await btns('work-2')).join()}`);
    await orow(a, 'work-2').getByRole('button', { name: 'Ask her' }).click(); await rest(a, 500);
    const d = await dockOf(a);
    check('the input is Ask her about this…, with the chip in English', d && d.placeholder === 'Ask her about this…' && d.chip.title === askOf(m, 'work-2', en).about.title && d.chip.x === 'Take it off', JSON.stringify(d));
    a.s.refuse.add('submit');
    await a.page.keyboard.type('hi'); await a.page.keyboard.press('Enter'); await rest(a, 450);
    check('and a refusal says Press Enter to try again', await a.page.locator('.dt-dock + .dt-hint').innerText() === "Didn't send. Press Enter to try again.");
    await a.ctx.close();
  }

  // ---- 这段在干嘛 (ADR 0302): one quiet entry, in an opened stretch and in the bar's range card; nothing else invites it ----
  const SAY = '这段在干嘛';
  const sayDock = a => a.page.evaluate(() => {
    const d = document.querySelector('.dt-dock'), c = d?.querySelector('.dt-about'), f = d?.querySelector('.dt-ask-in'), go = d?.querySelector('.dt-send');
    return d && { sent: d.classList.contains('is-sent'), text: d.textContent.trim(), chip: c ? c.textContent.trim() : null, drop: !!c?.querySelector('button'), draft: f ? f.value : null, focused: !!f && document.activeElement === f,
      placeholder: f?.placeholder ?? null, max: f?.maxLength ?? null, readOnly: f?.readOnly ?? null, canSend: go ? !go.disabled : null };
  });
  const answers = a => a.s.posts.filter(p => p.path === '/inherent/day/answer');
  const bad = a => a.page.locator('.dt-dock + .dt-hint').innerText().catch(() => null);
  const gone = a => a.page.waitForFunction(() => !document.querySelector('.dt-dock'), null, { timeout: 4000 });
  const savedAs = a => a.page.waitForFunction(() => document.querySelector('.dt-dock.is-sent'), null, { timeout: 4000 });
  const lastOf = async (a, key) => { const acts = orow(a, key).locator('.dt-acts').first(); await acts.waitFor({ state: 'attached', timeout: 1500 }).catch(() => {}); return await acts.count() ? acts.evaluate(el => { const say = el.nextElementSibling?.matches('.dt-sayhow') ? el.nextElementSibling : null, last = say ?? el.lastElementChild; return { text: last?.textContent, cls: last?.className, all: el.innerText + (say ? say.innerText : '') }; }) : { text: null, cls: null, all: '' }; };
  if (all || only === 'say') {
    const sm = buildDay(bigDay(), NOW, zh), sbar = buildBar(sm, zh), t0 = sbar.t0, t1 = T(24);
    const a = await boot({ served: bigDay() });
    await openDay(a);
    const g = await a.page.evaluate(() => { const r = document.querySelector('.dt-track').getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width }; });
    const xAt = (h, m = 0) => g.left + (T(h, m) - t0) / (t1 - t0) * g.width, y = g.top + 12;
    const whole = () => a.page.locator('.notch').innerText();

    // nothing invites: not on the wing, the list, a row under the pointer, the bar or its hover card
    const invited = [];
    for (const key of ['stay-home-2', 'move-1', 'ride-1', 'work-2', 'sleep-1', 'call-1']) {
      await orow(a, key).scrollIntoViewIfNeeded(); await orow(a, key).hover({ position: { x: 170, y: 14 } }); await rest(a, 260);
      if ((await whole()).includes(SAY)) invited.push(key);
    }
    await a.move(xAt(14, 30), y); await rest(a, 300);
    if ((await whole()).includes(SAY) || await a.page.locator('.dt-card.is-on').count() !== 1) invited.push('bar');
    check('nothing invites it: not the list, not a row under the pointer, not the bar and its hover card, and no input is up', invited.length === 0 && !await sayDock(a), invited.join());

    // which rows offer it once opened: the stretches; and where, last among the buttons in the same look
    const offered = {};
    const stretches = ['stay-home-1', 'stay-home-2', 'stay-uvic', 'move-1', 'ride-1', 'sleep-1', 'work-1', 'work-2', 'call-1', 'workout-1', 'chat-1'], others = ['reminder-1', 'reminder-2', 'talk-1', 'talk-3', 'picture-1', 'pay-1', 'departure-1', 'event-1', 'event-2', 'claude-day-1', 'code:stay-home-1'];
    for (const key of [...stretches, ...others]) {
      if (!await orow(a, key).count()) { offered[key] = 'missing'; continue; }
      await tap(a, key); await rest(a, 420);
      const l = await lastOf(a, key);
      offered[key] = l.text === SAY ? (l.cls === 'dt-sayhow' ? 'last' : l.cls) : l.all.includes(SAY) ? 'inside' : 'no';
    }
    check('an opened stay, move, ride, night, Mac block, call, run and Claude chat offer it, a quiet line under their buttons', stretches.every(k => offered[k] === 'last'), JSON.stringify(offered));
    check('her rows (a reminder, a talk), a picture, a payment, a bus to catch, an event, the letter on his chats and the folded row of sessions do not', others.every(k => offered[k] === 'no'), JSON.stringify(offered));

    // the entry, the input and the save
    await tap(a, 'stay-home-2'); await rest(a, 480);
    await shotRow(a, 'say-01-open-row', 'stay-home-2');
    const quiet = async sel => { const e = a.page.locator(sel), read = () => e.evaluate(el => { const c = getComputedStyle(el), r = el.getBoundingClientRect(), p = el.previousElementSibling.getBoundingClientRect(); return { bg: c.backgroundColor, radius: c.borderRadius, color: c.color, svg: !!el.querySelector('svg'), size: c.fontSize, under: r.top >= p.bottom - .5, cls: el.previousElementSibling.className }; }), idle = await read(); await e.hover(); await rest(a, 300); const lit = await read(); await a.page.mouse.move(0, 0); return { idle, lit }; };
    const ql = await quiet('[data-key="stay-home-2"] .dt-sayhow');
    check('in an open row 这段在干嘛 is a faint line under the buttons, not a capsule: a pencil glyph and the words, no fill, no rounding, the muted colour, brighter when the pointer is on it', ql.idle.bg === 'rgba(0, 0, 0, 0)' && ql.idle.radius === '0px' && ql.idle.svg && ql.idle.under && ql.idle.cls === 'dt-acts' && ql.idle.size === '12px' && ql.idle.color !== ql.lit.color, JSON.stringify(ql));
    await orow(a, 'stay-home-2').getByRole('button', { name: SAY }).click(); await rest(a, 520);
    const d1 = await sayDock(a);
    check('it slides an input up at the pane\'s foot: the stretch\'s range as its chip (14:05–14:50, nothing to take off), the cursor in the field, the placeholder, the field held to 200, send greyed; the row stays open',
      d1 && d1.chip === '14:05–14:50' && !d1.drop && d1.focused && d1.placeholder === '这段在干嘛？比如：躺着休息' && d1.max === 200 && d1.canSend === false && await expanded(a, 'stay-home-2') === 'true', JSON.stringify(d1));
    await a.shot('say-02-input', { x: 40, y: 0, width: 560, height: 760 });
    await a.page.keyboard.insertText('字'.repeat(205)); await rest(a, 100);
    check('the field takes 200 characters and no more', (await sayDock(a)).draft === '字'.repeat(200), String((await sayDock(a)).draft?.length));
    await a.page.locator('.dt-ask-in').fill('  躺着休息 '); await rest(a, 100);
    check('typing enables send', (await sayDock(a)).canSend === true && (await sayDock(a)).draft === '  躺着休息 ');
    const h0 = a.s.hits.length;
    a.s.lag = 600;
    await a.page.keyboard.press('Enter'); await rest(a, 250);
    const busy = await sayDock(a);
    check('while it is saving the field is read-only and send greyed; the words and the chip stay', busy && busy.readOnly === true && busy.canSend === false && busy.draft === '  躺着休息 ' && busy.chip === '14:05–14:50' && !busy.sent, JSON.stringify(busy));
    await savedAs(a); const t0s = Date.now();
    const done = await sayDock(a);
    check('on 200 it says 记下了 where the input was (the field and the chip are gone)', done.sent && done.text === '记下了' && done.chip === null && done.draft === null, JSON.stringify(done));
    check('one request, to the one route, with exactly the shown day, the row\'s range in whole ms and his words trimmed', answers(a).length === 1 && JSON.stringify(answers(a)[0].body) === JSON.stringify({ day: DATE, start_ms: T(14, 5), end_ms: T(14, 50), text: '躺着休息' }), JSON.stringify(answers(a)));
    await gone(a); const slid = Date.now() - t0s;
    check('1.5 s after it was saved the line slides away', slid >= 1350 && slid < 2600, String(slid));
    a.s.lag = 0; await rest(a, 300);
    check('the day was read again once, the way every change is', a.s.hits.length - h0 === 1, String(a.s.hits.length - h0));
    await a.move(xAt(14, 30), y); await rest(a, 320);
    const card = () => a.page.evaluate(() => {
      const c = document.querySelector('.dt-card.is-on:not(.is-range)');
      if (!c) return null;
      const cur = c.querySelector('.dt-slot.is-cur'), norm = el => el.innerText.replace(/\s*\n\s*/g, ' ').trim();
      return { head: norm(cur.querySelector('.dt-ch')), place: cur.querySelector('.dt-cnote.is-place')?.innerText ?? null };
    });
    const c1 = await card();
    check('what he wrote shows through the doing words the brain serves: on the bar, 14:30 is 躺着休息 with its own time, and the place 家 under it', c1 && c1.head === '躺着休息 14:05–14:50 · 45 分' && c1.place === '家', JSON.stringify(c1));

    // a save that fails stays under the input until he edits it; the same words go again
    await tap(a, 'move-1'); await rest(a, 480);
    await orow(a, 'move-1').getByRole('button', { name: SAY }).click(); await rest(a, 500);
    a.s.refuse.add('answer');
    const h1 = a.s.hits.length;
    await a.page.keyboard.type('开车去 A&W'); await a.page.keyboard.press('Enter'); await rest(a, 450);
    const d2 = await sayDock(a);
    check('a 502 says 没存上，稍后再试 under the input and keeps the chip (11:25–13:05) and the words; nothing was read again', await bad(a) === '没存上，稍后再试' && d2.chip === '11:25–13:05' && d2.draft === '开车去 A&W' && !d2.sent && d2.readOnly === false && a.s.hits.length === h1 && answers(a).length === 2, JSON.stringify({ d2, line: await bad(a) }));
    await rest(a, 2200);
    check('and it stays (it has no timer)', await bad(a) === '没存上，稍后再试' && !!await sayDock(a));
    await a.page.keyboard.type('!'); await rest(a, 150);
    check('the next key takes it away', await bad(a) === null);
    a.s.refuse.delete('answer');
    await a.page.keyboard.press('Enter'); await rest(a, 450);
    check('and Enter again, when it can, sends the same words and the same range', answers(a).length === 3 && JSON.stringify(answers(a)[2].body) === JSON.stringify({ day: DATE, start_ms: T(11, 25), end_ms: T(13, 5), text: '开车去 A&W!' }) && (await sayDock(a)).sent, JSON.stringify(answers(a).slice(1)));
    await gone(a);

    // the two inputs share the foot: the words of one are not carried into the other; Esc lets go of it first, the row and the pane stay
    await orow(a, 'move-1').getByRole('button', { name: SAY }).click(); await rest(a, 500);
    check('the words that were saved do not come back into the next input', (await sayDock(a)).draft === '');
    await a.page.keyboard.type('abc');
    await orow(a, 'move-1').getByRole('button', { name: '问她' }).click(); await rest(a, 480);
    const d3 = await dockOf(a), about = askOf(sm, 'move-1', zh);
    check('asking her from the same row swaps the input for hers: its chip, its placeholder, and not the words typed for the other', d3 && d3.chip && d3.chip.title === about.about.title && d3.chip.at === '11:25' && d3.draft === '' && d3.placeholder === '问她这一条…', JSON.stringify(d3));
    await a.page.keyboard.type('xyz');
    await orow(a, 'move-1').getByRole('button', { name: SAY }).click(); await rest(a, 480);
    const d4 = await sayDock(a);
    check('and back: the range chip, and not the words typed for her', d4 && d4.chip === '11:25–13:05' && d4.draft === '' && d4.placeholder === '这段在干嘛？比如：躺着休息', JSON.stringify(d4));
    await a.page.keyboard.type('half'); await a.page.keyboard.press('Escape'); await rest(a, 450);
    check('Esc lets go of the input first (and what was typed in it); the open row and the pane stay, and nothing was sent', await a.page.locator('.dt-dock').count() === 0 && await expanded(a, 'move-1') === 'true' && (await a.read()).tl === 'pinned' && answers(a).length === 3 && turns(a).length === 0, JSON.stringify(await a.read()));
    await a.page.keyboard.press('Escape'); await rest(a, 450);
    check('and the next Esc closes the row', await folds(a) === 0);
    await a.ctx.close();
  }
  if (all || only === 'say') {
    // the bar's range card: the quiet button there holds exactly the dragged range, cut to now
    const sm = buildDay(bigDay(), NOW, zh), sbar = buildBar(sm, zh), t0 = sbar.t0, t1 = T(24);
    const a = await boot({ served: bigDay() });
    await openDay(a);
    const g = await a.page.evaluate(() => { const r = document.querySelector('.dt-track').getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width }; });
    const xAt = (h, m = 0) => g.left + (T(h, m) - t0) / (t1 - t0) * g.width, y = g.top + 12;
    const drag = async (x0, x1) => { await a.move(x0, y); await a.page.mouse.down(); await a.move((x0 + x1) / 2, y); await a.move(x1, y); await a.page.mouse.up(); await a.page.waitForTimeout(280); };
    const buttons = () => a.page.locator('.dt-card.is-range button').allInnerTexts();
    await drag(xAt(14) + .1, xAt(18) + .1);
    const look = await a.page.locator('.dt-card.is-range .dt-sayhow').evaluate(el => ({ bg: getComputedStyle(el).backgroundColor, radius: getComputedStyle(el).borderRadius, svg: !!el.querySelector('svg'), size: getComputedStyle(el).fontSize }));
    check('the range card has 只看这段 and, after it, 这段在干嘛 as the same faint text line (glyph and words, no fill, no rounding)', (await buttons()).join() === `只看这段,${SAY}` && look.bg === 'rgba(0, 0, 0, 0)' && look.radius === '0px' && look.svg && look.size === '12px', JSON.stringify(look));
    await a.shot('say-03-range-card', { x: 40, y: 0, width: 560, height: 700 });
    await a.page.locator('.dt-card.is-range .dt-sayhow').click(); await rest(a, 520);
    const r1 = await sayDock(a);
    check('it opens the same input with the dragged range as the chip (14:00–18:00)', r1 && r1.chip === '14:00–18:00' && !r1.drop && r1.focused, JSON.stringify(r1));
    await a.page.keyboard.type('在学校'); await a.page.keyboard.press('Enter'); await rest(a, 450);
    check('and Enter sends exactly that range: the day, 14:00 and 18:00 in ms, his words', answers(a).length === 1 && JSON.stringify(answers(a)[0].body) === JSON.stringify({ day: DATE, start_ms: T(14), end_ms: T(18), text: '在学校' }) && (await sayDock(a)).text === '记下了', JSON.stringify(answers(a)));
    await gone(a);
    await drag(xAt(20) + .1, xAt(23, 30) + .1);
    check('a range dragged past now is cut to now in the chip (20:00–21:50)', (await buttons()).includes(SAY));
    await a.page.locator('.dt-card.is-range .dt-sayhow').click(); await rest(a, 520);
    check('the chip says 20:00–21:50', (await sayDock(a)).chip === '20:00–21:50');
    await a.page.keyboard.type('写作业'); await a.page.keyboard.press('Enter'); await rest(a, 450);
    check('and the range sent ends at now, to the ms', answers(a).length === 2 && JSON.stringify(answers(a)[1].body) === JSON.stringify({ day: DATE, start_ms: T(20), end_ms: NOW, text: '写作业' }), JSON.stringify(answers(a).slice(1)));
    await gone(a);
    await drag(xAt(22, 30) + .1, xAt(23, 30) + .1);
    check('a range that is all still to come has no such button', (await buttons()).join() === '只看这段' && await a.page.locator('.dt-sayhow').count() === 0, (await buttons()).join());
    // a 422 (the day the brain holds has nothing there) says the same line
    await drag(xAt(14) + .1, xAt(18) + .1);
    await a.page.locator('.dt-card.is-range .dt-sayhow').click(); await rest(a, 520);
    a.s.served = { ...a.s.served, items: [] };
    await a.page.keyboard.type('z'); await a.page.keyboard.press('Enter'); await rest(a, 450);
    check('a 422 says 没存上，稍后再试 too, and keeps the words', answers(a).length === 3 && await bad(a) === '没存上，稍后再试' && (await sayDock(a)).draft === 'z', JSON.stringify(answers(a).slice(2)));
    await a.page.getByRole('button', { name: '前一天' }).click(); await rest(a, 400);
    check('the input is the day\'s: another day takes it away, with nothing sent', await a.page.locator('.dt-dock').count() === 0 && answers(a).length === 3);
    await a.ctx.close();
  }
  if (all || only === 'say') {
    const a = await boot({ served: bigDay(), lang: 'en' });
    await openDay(a);
    await tap(a, 'stay-home-2'); await rest(a, 480);
    check('in English the button is What was this', (await lastOf(a, 'stay-home-2')).text === 'What was this');
    await orow(a, 'stay-home-2').getByRole('button', { name: 'What was this' }).click(); await rest(a, 500);
    const e1 = await sayDock(a);
    check('the input says What were you doing? e.g. lying down', e1 && e1.placeholder === 'What were you doing? e.g. lying down' && e1.chip === '14:05–14:50', JSON.stringify(e1));
    a.s.refuse.add('answer');
    await a.page.keyboard.type('lying down'); await a.page.keyboard.press('Enter'); await rest(a, 450);
    check('a failure says Not saved, try again later', await bad(a) === 'Not saved, try again later', await bad(a));
    a.s.refuse.delete('answer');
    await a.page.keyboard.press('Enter'); await rest(a, 450);
    check('and a save says Noted', (await sayDock(a)).text === 'Noted' && JSON.stringify(answers(a).at(-1).body) === JSON.stringify({ day: DATE, start_ms: T(14, 5), end_ms: T(14, 50), text: 'lying down' }));
    await a.ctx.close();
  }

  // ---- the list holds what he is working on (ONLY=steady): the row he presses stays under his hand, an open row keeps 24 px clear of the foot, an input there does not cover it ----
  if (all || only === 'steady') {
    // where a row is in the list's view and how far its foot is above the list's foot, and the room the list was given
    const spot = (a, key) => a.page.evaluate(k => { const l = document.querySelector('.dt-list'), lr = l.getBoundingClientRect(), r = document.querySelector(`[data-key="${k}"]`)?.getBoundingClientRect();
      return { top: r ? r.top - lr.top : null, under: r ? lr.bottom - r.bottom : null, st: l.scrollTop, sh: l.scrollHeight, lh: lr.height, head: parseFloat(l.style.paddingTop) || 0, foot: parseFloat(l.style.paddingBottom) || 0 }; }, key);
    // the frames a change takes, each read as the frame will be painted: a resize observer made in the frame's callback is told after the page's own (resize observers are told in the order they were made;
    // the pane scrolls the list in its own, DayTimeline.tsx), where a frame callback or a task between frames would read the layout before it
    const frames = async (a, key, act, ms = 800) => {
      await a.page.evaluate(k => {
        const l = document.querySelector('.dt-list'), probe = document.createElement('i'), out = window.__frames = [];
        probe.style.cssText = 'position:fixed;left:-9px;top:0;width:1px;height:1px;opacity:0;pointer-events:none';
        document.body.append(probe);
        const grab = () => { const lr = l.getBoundingClientRect(), r = document.querySelector(`[data-key="${k}"]`)?.getBoundingClientRect(); out.push({ top: r ? r.top - lr.top : null, under: r ? lr.bottom - r.bottom : null, st: l.scrollTop, ft: l.firstElementChild.getBoundingClientRect().top - lr.top }); };
        let on = true;
        const tick = () => { if (!on) return; const look = new ResizeObserver(() => { look.disconnect(); if (on) grab(); }); look.observe(probe); requestAnimationFrame(tick); };
        grab(); requestAnimationFrame(tick);
        window.__framesStop = () => { on = false; probe.remove(); };
      }, key);
      await act(); await a.page.waitForTimeout(ms);
      return a.page.evaluate(() => { window.__framesStop(); return window.__frames; });
    };
    const dev = fr => Math.max(...fr.map(f => Math.abs(f.top - fr[0].top)));
    const press = (a, key) => orow(a, key).click({ position: { x: 170, y: 14 } });
    // the list moved (no animation) so that the row's top is `y` into its view
    const place = async (a, key, y) => { await a.page.evaluate(([k, y]) => { const l = document.querySelector('.dt-list'); l.scrollTop += document.querySelector(`[data-key="${k}"]`).getBoundingClientRect().top - l.getBoundingClientRect().top - y; }, [key, y]); await rest(a, 300); };
    const toEnd = async a => { await a.page.evaluate(() => { const l = document.querySelector('.dt-list'); l.scrollTop = l.scrollHeight; }); await rest(a, 300); };
    // the first row after `key` that opens and lies whole in the view with `room` px to spare under it
    const below = (a, key, room) => a.page.evaluate(([k, room]) => { const l = document.querySelector('.dt-list'), lr = l.getBoundingClientRect(), rows = [...l.querySelectorAll('[data-key][aria-expanded]')], i = rows.findIndex(e => e.dataset.key === k);
      return rows.slice(i + 1).find(e => { const r = e.getBoundingClientRect(); return r.top >= document.querySelector(`[data-key="${k}"]`).getBoundingClientRect().bottom - 1 && r.bottom <= lr.bottom - room; })?.dataset.key ?? null; }, [key, room]);
    const lastRow = a => a.page.evaluate(() => [...document.querySelectorAll('.dt-list [data-key][aria-expanded]')].at(-1).dataset.key);
    const dockTop = a => a.page.evaluate(() => document.querySelector('.dt-fold.is-dock .dt-dock')?.getBoundingClientRect().top ?? null);
    const listFoot = a => a.page.evaluate(() => document.querySelector('.dt-list').getBoundingClientRect().bottom);

    const a = await boot({ served: bigDay() });
    await openDay(a);
    // 1 and 2: in the middle of the day, with room above: a stay opens where it is (it fits: nothing scrolls) and a second row pressed under it, the stay closing above it, stays where it was
    await place(a, 'stay-home-2', 24);
    const x0 = await spot(a, 'stay-home-2');
    await press(a, 'stay-home-2'); await rest(a, 700);
    const x1 = await spot(a, 'stay-home-2');
    check('a row that fits under the foot opens without the list moving a pixel, and its top stays', Math.abs(x1.st - x0.st) <= .5 && Math.abs(x1.top - x0.top) <= .5, JSON.stringify({ x0, x1 }));
    const Y = await below(a, 'stay-home-2', 120);
    const y0 = await spot(a, Y), yf = await frames(a, Y, () => press(a, Y)), y1 = await spot(a, Y);
    check('pressing a row while another is open above it: it stays within 1 px of where it was in every frame of the opening and when it is over, though the rows above give back their height',
      Y && yf.length > 8 && dev(yf) <= 1 && Math.abs(y1.top - y0.top) <= 1 && y1.under >= 24 && y0.st - y1.st > 30 && await expanded(a, Y) === 'true' && await expanded(a, 'stay-home-2') === 'false', JSON.stringify({ Y, y0, y1, dev: dev(yf), tops: yf.map(f => +f.top.toFixed(1)), sts: yf.map(f => +f.st.toFixed(1)) }));
    const yc = await frames(a, Y, () => press(a, Y)), y2 = await spot(a, Y);
    check('and pressing it again, it closes in place: its top does not move in any frame', dev(yc) <= 1 && Math.abs(y2.top - y0.top) <= 1 && await expanded(a, Y) === 'false', JSON.stringify({ dev: dev(yc), y0, y2 }));
    // Esc closes the open row in place too
    await press(a, 'stay-home-2'); await rest(a, 600);
    const e0 = await spot(a, 'stay-home-2'), ef = await frames(a, 'stay-home-2', () => a.page.keyboard.press('Escape'));
    check('Esc closes the open row where it is', dev(ef) <= 1 && Math.abs((await spot(a, 'stay-home-2')).top - e0.top) <= 1 && await expanded(a, 'stay-home-2') === 'false', JSON.stringify({ dev: dev(ef) }));

    // at the top of the list there is no scroll to give back and no room is made above the first row (a blank band is worse than rows that move): the rows move with the closing, only by the part that has no scroll
    const firstTop = () => a.page.evaluate(() => { const l = document.querySelector('.dt-list'); return l.firstElementChild.getBoundingClientRect().top - l.getBoundingClientRect().top + l.scrollTop; });
    await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = 0; }); await rest(a, 300);
    const ft0 = await firstTop();
    await press(a, 'stay-home-1'); await rest(a, 700);
    const Z = await below(a, 'stay-home-1', 120), z0 = await spot(a, Z), zf = await frames(a, Z, () => press(a, Z)), z1 = await spot(a, Z);
    check('at the top of the list no room is made above the first row: its top is the list\'s top in every frame, and there is no padding above it; the pressed row moves with the closing above it',
      Z && zf.length > 8 && zf.every(f => f.st === 0 && Math.abs(f.ft - ft0) <= .5) && z1.head === 0 && z1.st === 0 && z0.top - z1.top > 30, JSON.stringify({ Z, z0, z1, ft0, fts: zf.map(f => +f.ft.toFixed(1)) }));
    await press(a, Z); await rest(a, 500);
    // with some scroll to give (here 40 px) the pressed row holds while there is any, and moves only by the rest
    await press(a, 'stay-home-1'); await rest(a, 700);
    await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = 40; }); await rest(a, 300);
    const W = await below(a, 'stay-home-1', 120), w0 = await spot(a, W), wf = await frames(a, W, () => press(a, W)), w1 = await spot(a, W);
    const held = wf.filter(f => f.st > .5);
    check('with only some scroll to give, the pressed row stays within 1 px in every frame that has scroll left, and the list ends at its top with no band above the first row',
      W && held.length >= 2 && held.every(f => Math.abs(f.top - w0.top) <= 1) && wf.every(f => f.st > .5 || Math.abs(f.ft - ft0) <= .5) && w1.st === 0 && w1.head === 0 && w0.top - w1.top > 30, JSON.stringify({ W, w0, w1, held: held.length, tops: wf.map(f => +f.top.toFixed(1)), sts: wf.map(f => +f.st.toFixed(1)) }));
    await press(a, W); await rest(a, 500);

    await a.ctx.close();
    const b = await boot({ served: bigDay() });
    await openDay(b);
    // the last row of the list: 24 px under it when it opens, as under any row, the list scrolling with it
    await toEnd(b);
    const last = await lastRow(b), l0 = await spot(b, last), lf = await frames(b, last, () => press(b, last)), l1 = await spot(b, last);
    check('the last row of the list opens with 24 px under it, not flush: the list scrolls with its opening, smoothly and never back',
      Math.abs(l1.under - 24) <= 1 && l1.foot >= 23 && lf.every((f, i) => i === 0 || f.st >= lf[i - 1].st - .01) && new Set(lf.map(f => f.st)).size >= 5 && l1.st - l0.st > 60, JSON.stringify({ last, l0, l1, steps: new Set(lf.map(f => f.st)).size }));
    // 问她 from it: the input slides up under it and the row is brought up to 24 px clear of the list's new foot
    await orow(b, last).locator('.dt-fold .dt-btn', { hasText: '问她' }).click(); await rest(b, 700);
    const d1 = await spot(b, last), dt1 = await dockTop(b), lb1 = await listFoot(b);
    check('the input at the pane\'s foot does not cover the open row: it ends 24 px above the list\'s foot, which is above the input', dt1 !== null && Math.abs(d1.under - 24) <= 1 && lb1 <= dt1 + .5 && d1.lh < l1.lh - 30, JSON.stringify({ d1, dt1, lb1 }));
    await b.page.keyboard.press('Escape'); await rest(b, 500);
    const lc = await frames(b, last, () => press(b, last)), l2 = await spot(b, last);
    check('closing the last row leaves it where it was: its top does not move in any frame, the list keeping the room', dev(lc) <= 1 && Math.abs(l2.top - (await spot(b, last)).top) <= 1 && l2.under >= 60, JSON.stringify({ dev: dev(lc), l2 }));
    await b.page.evaluate(() => { document.querySelector('.dt-list').scrollTop -= 50; }); await rest(b, 250);
    const l3 = await spot(b, last);
    check('and the room is given back as he scrolls up out of it, without the row moving for it', Math.abs(l3.top - (l2.top + 50)) <= 1 && l3.foot <= l2.foot - 40, JSON.stringify({ l2, l3 }));

    // b stay near the foot that does not fit opens with the list scrolling just enough, 24 px clear; the input that comes up under it moves it again by as much as it takes
    await b.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = 0; }); await rest(b, 300);
    await place(b, 'stay-home-3', (await spot(b, 'stay-home-3')).lh - 60);
    const s0 = await spot(b, 'stay-home-3'), sf = await frames(b, 'stay-home-3', () => press(b, 'stay-home-3')), s1 = await spot(b, 'stay-home-3');
    check('a row near the foot that would not fit opens with the list scrolling just enough to leave 24 px under it, together with its opening', Math.abs(s1.under - 24) <= 1 && s1.st - s0.st > 40 && sf.length > 8 && new Set(sf.map(f => f.st)).size >= 5 && sf.every((f, i) => i === 0 || f.st >= sf[i - 1].st - .01), JSON.stringify({ s0, s1, steps: new Set(sf.map(f => f.st)).size }));
    await orow(b, 'stay-home-3').getByRole('button', { name: SAY }).click(); await rest(b, 700);
    const s2 = await spot(b, 'stay-home-3'), dt2 = await dockTop(b), lb2 = await listFoot(b);
    check('the input of 这段在干嘛 does not cover it either: the open stay stays 24 px above the list\'s foot', dt2 !== null && Math.abs(s2.under - 24) <= 1 && lb2 <= dt2 + .5 && s2.st > s1.st + 30, JSON.stringify({ s1, s2, dt2, lb2 }));
    await b.page.keyboard.press('Escape'); await rest(b, 500); await press(b, 'stay-home-3'); await rest(b, 600);
    // an open row that still fits above the input's top does not move for it
    await place(b, 'stay-home-2', 40);
    await press(b, 'stay-home-2'); await rest(b, 700);
    const f0 = await spot(b, 'stay-home-2');
    await orow(b, 'stay-home-2').getByRole('button', { name: '问她' }).click(); await rest(b, 700);
    const f1 = await spot(b, 'stay-home-2');
    check('an open row that still has 24 px above the new foot does not scroll when the input comes up', Math.abs(f1.st - f0.st) <= .5 && f1.under >= 23 && f1.lh < f0.lh - 30, JSON.stringify({ f0, f1 }));
    await b.ctx.close();

    // reduced motion: nothing animates, and the pressed row is where it was when the frame is drawn
    const r = await boot({ served: bigDay() });
    await openDay(r);
    await r.page.emulateMedia({ reducedMotion: 'reduce' }); await rest(r, 150);
    await place(r, 'stay-home-2', 24);
    await press(r, 'stay-home-2'); await rest(r, 400);
    const Yr = await below(r, 'stay-home-2', 120), r0 = await spot(r, Yr), rf = await frames(r, Yr, () => press(r, Yr), 400), r1 = await spot(r, Yr);
    check('with reduced motion the pressed row is where it was in the first frame after it is pressed, though the row above it closed at once', Yr && dev(rf) <= 1 && Math.abs(r1.top - r0.top) <= 1 && r0.st - r1.st > 30 && await expanded(r, 'stay-home-2') === 'false', JSON.stringify({ Yr, r0, r1, dev: dev(rf) }));
    await r.ctx.close();
  }

  // ---- the rail of an opened row stays inside its card (ONLY=rail) ----
  if (all || only === 'rail') {
    const a = await boot({ served: bigDay() });
    await openDay(a);
    // ::before / ::after are not elements: what a rail is, read from its style against the row's box
    const rail = (key, which) => a.page.evaluate(([k, which]) => { const e = document.querySelector(`[data-key="${k}"]`), c = getComputedStyle(e, which), r = e.getBoundingClientRect(); return { top: r.top + parseFloat(c.top), bottom: r.bottom - parseFloat(c.bottom), row: [r.top, r.bottom], w: c.width, cls: e.className }; }, [key, which]);
    const mids = await a.page.evaluate(() => [...document.querySelectorAll('.dt-list .dt-row.is-rail[aria-expanded]')].filter(e => !e.classList.contains('is-top') && !e.classList.contains('is-end')).map(e => e.dataset.key));
    check('there are rows in the middle of a rail to open', mids.length >= 2, mids.join());
    const shut = await rail(mids[0], '::before');
    check('a row that is shut has its rail from edge to edge', shut.top === shut.row[0] && shut.bottom === shut.row[1], JSON.stringify(shut));
    await tap(a, mids[0]); await rest(a, 600);
    const mid = await rail(mids[0], '::before');
    check('an opened row in the middle of a rail has the rail inside its card: it begins and ends 1 px in, so it never crosses the card\'s hairline',
      mid.top > mid.row[0] && mid.bottom < mid.row[1] && Math.abs(mid.top - mid.row[0] - 1) < .6 && Math.abs(mid.row[1] - mid.bottom - 1) < .6, JSON.stringify(mid));
    await tap(a, mids[0]); await rest(a, 400);
    const ends = await a.page.evaluate(() => ({ top: [...document.querySelectorAll('.dt-list .dt-row.is-rail.is-top[aria-expanded]')].map(e => e.dataset.key), end: [...document.querySelectorAll('.dt-list .dt-row.is-rail.is-end[aria-expanded]')].map(e => e.dataset.key) }));
    if (ends.top[0]) { await tap(a, ends.top[0]); await rest(a, 600); const t = await rail(ends.top[0], '::before'); check('the first row of a rail, opened, still begins at its glyph (inside the card already); its rail ends 1 px in', t.top - t.row[0] > 10 && Math.abs(t.row[1] - t.bottom - 1) < .6, JSON.stringify(t)); await tap(a, ends.top[0]); await rest(a, 400); }
    if (ends.end[0]) { await tap(a, ends.end[0]); await rest(a, 600); const t = await rail(ends.end[0], '::before'); check('the last row of a rail, opened, still ends at its glyph (inside the card already); its rail begins 1 px in', t.row[1] - t.bottom > 10 && Math.abs(t.top - t.row[0] - 1) < .6, JSON.stringify(t)); await tap(a, ends.end[0]); await rest(a, 400); }
    const hk = await a.page.evaluate(() => document.querySelector('.dt-list .dt-sh.has-rows[aria-expanded]')?.dataset.key);
    const head0 = await rail(hk, '::after');
    await tap(a, hk); await rest(a, 600);
    const head1 = await rail(hk, '::after');
    check('a stay\'s rail, from its dot down, stops 1 px inside the bottom of its card when it is opened, and reaches the bottom of its header when it is not', head0.bottom === head0.row[1] && Math.abs(head1.row[1] - head1.bottom - 1) < .6 && head1.top > head1.row[0], JSON.stringify({ head0, head1 }));
    await shotRow(a, 'steady-01-rail-in-card', mids[0]);
    await a.ctx.close();
  }

  // ---- reduced motion: a row does not grow, its words fade in over 120 ms; nothing slides ----
  if (all || only === 'open') {
    const a = await boot({ served: bigDay() });
    await hoverDay(a);
    await a.page.emulateMedia({ reducedMotion: 'reduce' }); await a.page.waitForTimeout(150);
    const run = await a.page.evaluate(async () => {
      const el = document.querySelector('[data-key="reminder-2"]');
      el.scrollIntoView({ block: 'center' });
      el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await new Promise(r => requestAnimationFrame(r));
      const fold = el.querySelector('.dt-fold'), body = fold.querySelector('.dt-body');
      const live = fold.getAnimations({ subtree: true }).map(x => ({ on: x.effect.target === fold ? 'fold' : 'body', duration: x.effect.getTiming().duration, props: [...new Set(x.effect.getKeyframes().flatMap(k => Object.keys(k)).filter(k => !['offset', 'easing', 'composite', 'computedOffset'].includes(k)))].join() }));
      return { live, h: fold.getBoundingClientRect().height, final: body.getBoundingClientRect().height };
    });
    check('with reduced motion an opening row does not grow (its height is its height at once) and its words fade in over 120 ms', run.live.length === 1 && run.live[0].on === 'body' && run.live[0].duration === 120 && run.live[0].props === 'opacity' && Math.abs(run.h - run.final) < 1, JSON.stringify(run));
    await rest(a, 250);
    await orow(a, 'reminder-2').click({ position: { x: 170, y: 14 } }); await a.page.waitForTimeout(80);
    check('and closing it takes it away at once', await folds(a) === 0 && await expanded(a, 'reminder-2') === 'false');
    await orow(a, 'talk-1').hover({ position: { x: 170, y: 14 } }); await rest(a, 200);
    await orow(a, 'talk-1').locator(':scope > .dt-quick .dt-ask').click(); await a.page.waitForTimeout(80);
    const dockRun = await a.page.evaluate(() => { const f = document.querySelector('.dt-fold.is-dock .dt-dock')?.closest('.dt-fold'); return f && { anims: f.getAnimations({ subtree: true }).map(x => x.effect.getTiming().duration), h: f.getBoundingClientRect().height, full: f.firstElementChild.getBoundingClientRect().height }; });
    check('the input comes at once too, with a 120 ms fade of its words', dockRun && dockRun.anims.join() === '120' && Math.abs(dockRun.h - dockRun.full) < 1, JSON.stringify(dockRun));
    await a.ctx.close();
  }

  // ---- the numbers' cards (ONLY=density): a click on a number hangs what is behind it under the numbers ----
  if (all || only === 'density') {
    const fz = buildDay(fullDay(), NOW, zh), fe = buildDay(fullDay(), NOW, en), eq = (x, y) => JSON.stringify(x) === JSON.stringify(y), near = (x, y, d = 1.5) => Math.abs(x - y) <= d;
    const CHIPS = [['睡', 'sleep'], ['路上', 'trip'], ['运动', 'workout'], ['Mac', 'work'], ['通话', 'call'], ['和她', 'talk'], ['花', 'payment'], ['照片', 'picture']];
    const chip = (a, word) => a.page.locator('.dt-num', { hasText: word });
    const settle = a => a.page.waitForTimeout(350);
    // the card on the page, read into the shape numberCard's lines have (cardLines)
    const onPage = a => a.page.evaluate(() => {
      const s = document.querySelector('.dt-card.is-num .dt-slot.is-cur');
      if (!s) return null;
      const txt = e => e.textContent.trim(), head = s.querySelector('.dt-ch');
      return [`${txt(head.querySelector('b'))}|${txt(head.lastElementChild)}`, ...[...s.querySelectorAll('.dt-nb > *')].map(p => {
        if (p.classList.contains('dt-ncs')) return [...p.children].map(c => `${txt(c.querySelector('small'))} ${txt(c.querySelector('b'))}`).join('/');
        if (p.classList.contains('dt-hyp')) return `night ${[...p.querySelectorAll('.dt-hyp-ends em')].map(txt).join('-')}`;
        if (p.classList.contains('dt-nbars')) return [...p.querySelectorAll('.dt-bars')].map(g => `${txt(g.querySelector('h4'))}: ${[...g.querySelectorAll('.dt-bar-row')].map(r => `${txt(r.querySelector('span'))} ${txt(r.querySelector('em'))}`).join(', ')}`).join('//');
        if (p.classList.contains('dt-nes')) { const more = p.querySelector('.dt-nmore'); return [...p.querySelectorAll('li:not(.dt-nmore)')].map(li => `${txt(li.querySelector('b'))}: ${[...li.querySelectorAll('em')].map(txt).join(' · ')}`).join('//') + (more ? ` ${txt(more)}` : ''); }
        const more = p.querySelector('.dt-nmore');
        return `pictures ${[...p.querySelectorAll('.dt-npic')].map(b => `${b.dataset.id[0]}@${txt(b.querySelector('time'))}`).join(',')}${more ? ` ${txt(more)}` : ''}`;
      })];
    });
    const state = a => a.page.evaluate(() => ({ lens: document.querySelector('.dt')?.dataset.lens ?? null, pressed: [...document.querySelectorAll('.dt-num[aria-pressed="true"]')].map(e => e.innerText.split('\n')[0]), nums: document.querySelectorAll('.dt-card.is-num').length, range: document.querySelectorAll('.dt-card.is-range.is-on').length, open: document.querySelector('.notch-drop')?.classList.contains('is-open') ?? false }));
    // a change of number, as a fade through: the card's clocks are held and set, so the same instants are read each time
    const swap = (a, word) => a.page.evaluate(async word => {
      const c = document.querySelector('.dt-card.is-num'); c.dataset.mark = 'kept';
      [...document.querySelectorAll('.dt-num')].find(e => e.textContent.includes(word)).click();
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      const slots = [...c.querySelectorAll('.dt-slot')], cur = slots.find(e => e.classList.contains('is-cur')), old = slots.find(e => e !== cur), anims = c.getAnimations({ subtree: true });
      anims.forEach(x => x.pause());
      const read = t => { anims.forEach(x => { x.currentTime = t; }); return { t, old: +getComputedStyle(old).opacity, now: +getComputedStyle(cur).opacity, oy: new DOMMatrix(getComputedStyle(old).transform).m42, ny: new DOMMatrix(getComputedStyle(cur).transform).m42 }; };
      const out = [0, 30, 60, 75, 90, 120, 150].map(read);
      anims.forEach(x => x.cancel());
      return { out, kept: document.querySelector('.dt-card.is-num') === c && c.dataset.mark === 'kept', head: cur.querySelector('.dt-ch b').textContent, cards: document.querySelectorAll('.dt-card.is-num').length, slots: slots.length };
    }, word);
    const apart = (x, v) => Math.abs(x - v) < .06;

    // the eight cards of a full day, each as numberCard.ts says it, one number after the next (the card is the same one, its words swapped)
    const a = await boot({ served: fullDay(), init: record });
    await openDay(a);
    const g = await a.page.evaluate(() => { const r = document.querySelector('.dt-track').getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width }; });
    const away = { x: g.left + 200, y: g.top - 48 };
    await a.move(away.x, away.y);
    const rest0 = await state(a);
    check('with no number pinned there is no card', rest0.nums === 0 && rest0.lens === null && rest0.pressed.length === 0);
    let p0 = await chip(a, '睡').boundingBox(); await a.move(p0.x + p0.width / 2, p0.y + p0.height / 2); await settle(a);
    const hov = await state(a);
    check('a number under the pointer only previews its lens: the lens is drawn, no card hangs', hov.lens === 'sleep' && hov.nums === 0 && hov.pressed.length === 0, JSON.stringify(hov));
    await a.move(away.x, away.y); await settle(a);

    await a.page.evaluate(() => { window.__anims.length = 0; });
    await chip(a, '睡').click(); await a.page.waitForFunction(() => document.querySelector('.dt-card.is-num .dt-slot.is-cur')); await a.page.waitForTimeout(400);
    const enter = (await anims(a)).filter(x => /dt-card-in/.test(x.cls));
    const first = await state(a);
    check('a click pins the number (aria-pressed, the lens) and hangs its card; the card is not there before it', first.nums === 1 && first.lens === 'sleep' && first.pressed.join() === '睡' && rest0.nums === 0, JSON.stringify(first));
    check('the card comes in over 180 ms on the pane\'s ease, rising 4 px as it fades in', enter.length === 1 && enter[0].opt.duration === 180 && enter[0].opt.easing === 'cubic-bezier(.16, 1, .3, 1)' && enter[0].kf.length === 2 && enter[0].kf[0].opacity === 0 && enter[0].kf[0].transform === 'translateY(4px)' && enter[0].kf[1].opacity === 1 && enter[0].kf[1].transform === 'none', JSON.stringify(enter));
    const geo = await a.page.evaluate(() => {
      const q = s => document.querySelector(s).getBoundingClientRect().toJSON(), c = q('.dt-card.is-num'), n = q('.dt-numbers'), l = q('.dt-list'), p = q('.notch-drop'), top = document.elementFromPoint(c.left + c.width / 2, c.top + 6);
      return { c, n, l, p, over: !!top?.closest('.dt-card.is-num'), rowsUnder: [...document.querySelectorAll('.dt-row')].filter(r => { const b = r.getBoundingClientRect(); return b.bottom > c.top && b.top < c.bottom; }).length };
    });
    check('it hangs under the numbers (6 px below their row), the width of the pane\'s content, inside the pane, over the list and no taller than about 170', near(geo.c.top, geo.n.bottom + 6) && near(geo.c.left, geo.n.left) && near(geo.c.right, geo.n.right) && geo.c.height <= 176 && geo.c.height > 60 && geo.c.bottom <= geo.p.bottom && geo.c.bottom > geo.l.top && geo.over, JSON.stringify(geo));
    await a.shot('density-sleep', { x: 40, y: 0, width: 560, height: 700 });
    const look = await a.page.evaluate(() => { const s = getComputedStyle(document.querySelector('.dt-card.is-num .dt-card-in')); return [s.backgroundColor, s.borderTopColor, s.borderTopWidth, s.borderRadius, s.boxShadow]; });

    // the list keeps scrolling under the card
    const sc0 = await a.page.evaluate(() => { const l = document.querySelector('.dt-list'); return { top: l.scrollTop, room: l.scrollHeight - l.clientHeight, rect: l.getBoundingClientRect().toJSON(), card: document.querySelector('.dt-card.is-num').getBoundingClientRect().toJSON() }; });
    await a.page.mouse.move(sc0.rect.left + sc0.rect.width / 2, sc0.rect.bottom - 30); await a.page.mouse.wheel(0, 240); await a.page.waitForTimeout(500);
    const sc1 = await a.page.evaluate(() => ({ top: document.querySelector('.dt-list').scrollTop, card: document.querySelector('.dt-card.is-num')?.getBoundingClientRect().toJSON() }));
    check('the list keeps scrolling under the card, and the card stays where it was', sc0.room > 100 && sc1.top > sc0.top + 50 && sc1.card && sc1.card.top === sc0.card.top && sc1.card.height === sc0.card.height, JSON.stringify({ sc0: sc0.top, sc1 }));
    await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = 0; });

    // every number's card, as the model says it
    const bad = [];
    for (const [word, kind] of CHIPS) {
      if (kind !== 'sleep') { await chip(a, word).click(); await a.page.waitForTimeout(450); }
      const got = await onPage(a), want = cardLines(cardOf(fz, kind, zh)), s = await state(a);
      if (!eq(got, want) || s.lens !== kind || s.nums !== 1 || s.pressed.join() !== word) bad.push({ kind, got, want, s });
      if (kind !== 'sleep') await a.shot(`density-${kind}`, { x: 40, y: 0, width: 560, height: 700 });
    }
    check('each of the eight numbers hangs the card the model says (numberCard.ts), in the pane\'s own words: the same lines, the same figures', bad.length === 0, JSON.stringify(bad));
    check('the card is the one card throughout: the next number swaps its words and does not bring another', (await state(a)).nums === 1);

    // a change of number is a fade through of 120 ms, in the same card
    await chip(a, '睡').click(); await a.page.waitForTimeout(450);
    const sw = await swap(a, '路上'), [s0, s30, s60, s75, s90, s120, s150] = sw.out;
    check('a change of number is a fade through in the same card: the old words are fading at 30 ms and gone at 60, the new have not begun until 60 and are whole at 120, and never are both seen',
      sw.kept && sw.cards === 1 && sw.head === '路上' && apart(s0.old, 1) && apart(s0.now, 0) && apart(s30.old, .5) && apart(s30.now, 0) && apart(s60.old, 0) && apart(s60.now, 0) && s75.now > 0 && s75.now < .3 && apart(s90.now, .5) && apart(s120.now, 1) && apart(s150.now, 1)
      && sw.out.every(x => Math.min(x.old, x.now) < .02), JSON.stringify(sw));
    check('and the new words come up 2 px as they appear: 2 px down while the old go, nearer at 75 ms, in place at 120', apart(s60.ny, 2) && s75.ny > 0 && s75.ny < 2 && apart(s120.ny, 0) && apart(s150.ny, 0), JSON.stringify(sw.out.map(x => [x.t, x.ny])));

    // hover over another number while one is pinned: the lens previews, the card is still the pinned one's
    p0 = await chip(a, '通话').boundingBox(); await a.move(p0.x + p0.width / 2, p0.y + p0.height / 2); await settle(a);
    const over = await state(a), pinnedHead = (await onPage(a))[0];
    await a.move(away.x, away.y); await settle(a);
    check('another number under the pointer previews its lens and leaves the card to the pinned one', over.lens === 'call' && over.nums === 1 && pinnedHead.startsWith('路上|') && (await state(a)).lens === 'trip', JSON.stringify({ over, pinnedHead }));

    // the range wins the room: while one is drawn or hung its card is the only one; the number's comes back when the range goes
    const rbar = buildBar(fz, zh), t0 = rbar.t0, t1 = T(24), xAt = (h, m = 0) => g.left + (T(h, m) - t0) / (t1 - t0) * g.width, y = g.top + 12;
    const tripCard = await onPage(a);
    await a.move(xAt(14) + .1, y); await a.page.mouse.down(); await a.move(xAt(16), y); await a.move(xAt(18) + .1, y); await a.page.waitForTimeout(150);
    const during = await state(a);
    await a.page.mouse.up(); await a.page.waitForTimeout(450);
    const ranged = await state(a), rlook = await a.page.evaluate(() => { const s = getComputedStyle(document.querySelector('.dt-card.is-range .dt-card-in')); return [s.backgroundColor, s.borderTopColor, s.borderTopWidth, s.borderRadius, s.boxShadow]; });
    await a.shot('density-with-range', { x: 40, y: 0, width: 560, height: 700 });
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(450);
    const back = await state(a), backCard = await onPage(a);
    check('the range card wins: dragging a range takes the number\'s card away, the range\'s card is the only one when it comes, and the number stays pinned', during.nums === 0 && during.range === 0 && ranged.nums === 0 && ranged.range === 1 && ranged.pressed.join() === '路上' && ranged.lens === 'trip', JSON.stringify({ during, ranged }));
    check('and when the range goes the number\'s card is back with the same words (the first Esc is for the range; the number stays pinned)', back.nums === 1 && back.range === 0 && back.pressed.join() === '路上' && eq(backCard, tripCard) && back.open, JSON.stringify({ back }));
    check('the number\'s card is in the look of the range\'s: the same ground, edge, corners and shadow', eq(look, rlook) && look[1] !== 'rgba(0, 0, 0, 0)', JSON.stringify({ look, rlook }));
    // a number clicked while a range is hung lets the range go (it is a click away from its card) and hangs its own card, so never the two
    await a.move(xAt(14) + .1, y); await a.page.mouse.down(); await a.move(xAt(16), y); await a.move(xAt(18) + .1, y); await a.page.mouse.up(); await a.page.waitForTimeout(400);
    const free = await a.page.evaluate(() => { for (const e of document.querySelectorAll('.dt-num')) { const r = e.getBoundingClientRect(), t = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2), name = e.innerText.split('\n')[0]; if (e.contains(t) && name !== '路上') return name; } return null; });
    const hung = await state(a);
    if (free) { await chip(a, free).click(); await a.move(away.x, away.y); await a.page.waitForTimeout(450); }
    const swapped = await state(a);
    check('a number clicked while a range is hung takes the room: the range goes, the number\'s card hangs, and never are the two seen together', hung.range === 1 && hung.nums === 0 && free !== null && swapped.range === 0 && swapped.nums === 1 && swapped.pressed.join() === free, JSON.stringify({ hung, free, swapped }));
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(350);

    // a second click on the pinned number lets both go; so does Esc, and the pane stays
    await chip(a, '路上').click(); await a.page.waitForTimeout(350); await chip(a, '路上').click(); await a.move(away.x, away.y); await a.page.waitForTimeout(400);
    const closedBy = await state(a);
    check('a second click on the pinned number lets go of the lens and the card', closedBy.nums === 0 && closedBy.lens === null && closedBy.pressed.length === 0 && closedBy.open, JSON.stringify(closedBy));
    await chip(a, '通话').click(); await a.move(away.x, away.y); await a.page.waitForTimeout(450);
    const pinnedCall = await state(a);
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(400);
    const escaped = await state(a);
    check('Esc lets go of the lens and the card and leaves the pane open', pinnedCall.nums === 1 && pinnedCall.lens === 'call' && escaped.nums === 0 && escaped.lens === null && escaped.pressed.length === 0 && escaped.open, JSON.stringify({ pinnedCall, escaped }));

    // the card follows the day: pinned on the Sleep, the day before is a day with a sleep and nothing else, and its card is that one's
    const paneBottom = () => a.page.evaluate(() => document.querySelector('.notch-drop').getBoundingClientRect().bottom), cardBottom = () => a.page.evaluate(() => document.querySelector('.dt-card.is-num')?.getBoundingClientRect().bottom ?? null);
    await chip(a, '睡').click(); await a.move(away.x, away.y); await a.page.waitForTimeout(400);
    await a.page.keyboard.press('ArrowLeft');
    await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日' && document.querySelector('.dt-track') && document.querySelector('.dt-card.is-num .dt-slot.is-cur'), undefined, 6000);
    await a.page.waitForTimeout(900);
    const ydayWant = cardLines(cardOf(buildDay(yesterday(), NOW, zh), 'sleep', zh)), ydayGot = await onPage(a), withCard = { card: await cardBottom(), pane: await paneBottom() };
    check('the card follows the day: on the day before, with the Sleep still pinned, it is that day\'s sleep (clocks only, nothing the phone did not say)', eq(ydayGot, ydayWant) && ydayGot.length === 2, JSON.stringify({ ydayGot, ydayWant }));
    await a.shot('density-yesterday', { x: 40, y: 0, width: 560, height: 520 });
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(900);
    const short = await paneBottom(), none = await state(a);
    await chip(a, '睡').click(); await a.move(away.x, away.y); await a.page.waitForTimeout(900);
    const again = { card: await cardBottom(), pane: await paneBottom() };
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(900);
    check('on a short day the card is inside the pane, which makes room for it when it must and gives the room back when the card goes', none.nums === 0 && withCard.card <= withCard.pane - 6 && again.card <= again.pane - 6 && again.pane >= short - 1 && Math.abs(await paneBottom() - short) < 2, JSON.stringify({ withCard, short, again }));
    await a.ctx.close();

    // the same in English (the Sleep and the Mac) and the photos (a thumbnail each; a click on one is the picture row's click)
    {
      const e = await boot({ served: fullDay(), lang: 'en' });
      await openDay(e);
      const names = Object.fromEntries(numbers(fe, en).map(n => [n.kind, n.label])), badEn = [];
      for (const kind of ['sleep', 'work']) {
        await e.page.locator('.dt-num', { hasText: names[kind] }).first().click(); await e.page.waitForTimeout(450);
        const got = await onPage(e), want = cardLines(cardOf(fe, kind, en));
        if (!eq(got, want)) badEn.push({ kind, got, want });
        await e.shot(`density-${kind}-en`, { x: 40, y: 0, width: 560, height: 700 });
      }
      check('in English the Sleep\'s card and the Mac\'s say it in English, the same lines as the model', badEn.length === 0, JSON.stringify(badEn));
      await e.ctx.close();
    }
    {
      const pics = fullDay({ items: [...fullDay().items, it('picture-2', 'picture', T(16, 20), T(16, 20), { attachment: BIG, note: '傍晚的海边，风很大' }), it('picture-3', 'picture', T(17, 5), T(17, 5), { attachment: 'b'.repeat(32), note: '没有了的' })] });
      const p = await boot({ served: pics });
      await openDay(p);
      await chip(p, '照片').click(); await p.page.waitForTimeout(700);
      const got = await onPage(p), want = cardLines(cardOf(buildDay(pics, NOW, zh), 'picture', zh));
      const thumbs = await p.page.evaluate(() => [...document.querySelectorAll('.dt-card.is-num .dt-slot.is-cur .dt-npic')].map(b => { const i = b.querySelector('img'), r = (i ?? b.firstElementChild).getBoundingClientRect(); return { img: !!i, ok: i ? i.complete && i.naturalWidth > 0 : null, w: Math.round(r.width), h: Math.round(r.height), cursor: getComputedStyle(b).cursor, label: b.getAttribute('aria-label') }; }));
      check('the photos card: its head the count, a thumbnail for each with its time (the pictures that the daemon has load; one it no longer has leaves a plain icon, not a broken image)', eq(got, want) && got.length === 2 && thumbs.length === 3 && thumbs.filter(t => t.ok === true).length === 2 && thumbs.filter(t => !t.img).length === 1 && thumbs.every(t => t.w === 56 && t.h === 56 && t.cursor === 'zoom-in'), JSON.stringify({ got, want, thumbs }));
      await p.shot('density-pictures', { x: 40, y: 0, width: 560, height: 700 });
      await p.page.locator('.dt-card.is-num .dt-npic').first().click(); await p.page.waitForTimeout(400);
      const viewer = await p.page.evaluate(() => ({ src: document.querySelector('.dt-viewer img')?.src ?? null, note: document.querySelector('.dt-viewer p')?.textContent ?? null }));
      check('a click on a thumbnail opens the picture large, with its note, as the picture row does', viewer.src?.endsWith(`/inherent/attachments/${PIC}`) && viewer.note === '汉堡和薯条\n在 A&W 吃的午饭', JSON.stringify(viewer));
      await p.page.keyboard.press('Escape'); await p.page.waitForTimeout(350);
      const afterViewer = await state(p);
      check('and Esc closes the picture first and leaves the card', await p.page.locator('.dt-viewer').count() === 0 && afterViewer.nums === 1 && afterViewer.pressed.join() === '照片', JSON.stringify(afterViewer));
      await p.ctx.close();
    }

    // Reduced Motion: the card comes in with a 120 ms fade and does not rise; a change of number is a fade through and nothing moves
    {
      const r = await boot({ served: fullDay(), init: record });
      await r.page.emulateMedia({ reducedMotion: 'reduce' });
      await openDay(r);
      await r.page.evaluate(() => { window.__anims.length = 0; });
      await chip(r, '睡').click(); await r.page.waitForFunction(() => document.querySelector('.dt-card.is-num .dt-slot.is-cur')); await r.page.waitForTimeout(400);
      const enter = (await anims(r)).filter(x => /dt-card-in/.test(x.cls));
      check('with reduced motion the card fades in over 120 ms and does not rise', enter.length === 1 && enter[0].opt.duration === 120 && enter[0].kf.length === 2 && enter[0].kf.every(f => f.transform === undefined) && enter[0].kf[0].opacity === 0 && enter[0].kf[1].opacity === 1, JSON.stringify(enter));
      const sw = await swap(r, '路上'), [s0, , s60, , s90, s120] = sw.out;
      check('and a change of number is a fade through of 120 ms that moves nothing', sw.kept && sw.head === '路上' && apart(s0.old, 1) && apart(s60.old, 0) && apart(s60.now, 0) && apart(s90.now, .5) && apart(s120.now, 1) && sw.out.every(x => Math.min(x.old, x.now) < .02 && x.oy === 0 && x.ny === 0), JSON.stringify(sw));
      await r.ctx.close();
    }
  }

  // ---- phase D: what he can write to the day (ADR 0296): the star, what mail said, a place with no name of his (ONLY=write) ----
  // A sheet of labelled tiles (screenshots, or crops of them) in one picture.
  // (a tile may have its own scale `k`; the label is under it)
  const sheetOf = async (a, name, tiles, cols, k = 1) => {
    const page = await a.ctx.newPage(), px = x => x.w * (x.k ?? k);
    await page.setViewportSize({ width: Math.ceil(cols * (Math.max(...tiles.map(px)) + 8) + 8), height: 100 });
    await page.setContent(`<!doctype html><body style="margin:0;padding:8px;background:#222;display:grid;grid-template-columns:repeat(${cols},max-content);gap:12px 8px;font:11px monospace;color:#ddd;align-items:start">${tiles.map(x => `<figure style="margin:0"><img style="display:block;width:${px(x)}px;border-radius:6px" src="data:image/png;base64,${x.png.toString('base64')}"><figcaption style="padding:4px 2px 0">${x.label}</figcaption></figure>`).join('')}</body>`);
    await page.screenshot({ path: path.join(dir, `${name}.png`), fullPage: true });
    await page.close();
  };
  const crop = async (a, label, box, pad = 0) => ({ png: await a.page.screenshot({ clip: { x: Math.max(0, box.x - pad), y: Math.max(0, box.y - pad), width: box.width + 2 * pad, height: box.height + 2 * pad } }), label, w: box.width + 2 * pad, h: box.height + 2 * pad });
  // A row with what the pane says up top about it beside it, in one sheet: `top` is [selector, label, scale] of the header or the bar, `label` is the row's.
  const sheetRow = async (a, name, key, top, label) => {
    const tiles = [];
    for (const [sel, text, k] of top) tiles.push({ ...await crop(a, text, await a.page.locator(sel).first().boundingBox(), 4), k });
    tiles.push({ ...await crop(a, label, await orow(a, key).boundingBox(), 6), k: 1.2 });
    await sheetOf(a, name, tiles, 1);
  };
  const writes = a => a.s.posts.filter(p => p.path === '/inherent/day/highlight');
  const marksOn = a => a.s.served.highlights.map(h => `${h.id}:${h.by}`).sort().join();
  // the bar's lit stars (each: whose, and whether it is on its way out), and what a row or a stay's header wears
  const lit = a => a.page.evaluate(() => [...document.querySelectorAll('.dt-lit')].map(el => ({ id: el.dataset.star, gone: el.classList.contains('is-gone'), hers: el.classList.contains('is-hers') })));
  const litIds = async a => (await lit(a)).filter(x => !x.gone).map(x => x.id).sort().join();
  const wears = (a, key) => orow(a, key).evaluate(r => { const b = r.querySelector(r.classList.contains('dt-sh') ? '.dt-star' : '.dt-badge'); return b ? { gone: b.classList.contains('is-gone'), hers: b.classList.contains('is-hers') } : null; });
  const quickStar = (a, key) => orow(a, key).locator(':scope > .dt-quick .dt-starbtn');
  const starOf = (a, btn) => btn.evaluate(b => ({ on: b.getAttribute('aria-pressed') === 'true', his: b.classList.contains('is-his'), hers: b.classList.contains('is-hers'), label: b.getAttribute('aria-label') }));
  const hoverOn = async (a, key) => { await orow(a, key).scrollIntoViewIfNeeded(); await orow(a, key).hover({ position: { x: 170, y: 14 } }); await rest(a, 280); };
  const under = (a, key) => orow(a, key).locator(':scope > .dt-fold .dt-hint').allInnerTexts().then(x => x.join('|'));
  // The star's own animations, held at each moment in turn, in one sheet (the click starts them; they are paused at once, so each tile is the star at that moment).
  const frames = async (a, key, name, offsets) => {
    const mine = `[data-key="${key}"] .dt-starbtn`, box = await orow(a, key).locator(':scope > .dt-quick .dt-starbtn').boundingBox(), tiles = [];
    await a.page.evaluate(sel => { for (const x of document.getAnimations()) if (x.effect?.target?.closest?.(sel)) x.pause(); }, mine);
    for (const ms of offsets) {
      await a.page.evaluate(([sel, ms]) => { for (const x of document.getAnimations()) if (x.effect?.target?.closest?.(sel)) x.currentTime = ms; }, [mine, ms]);
      tiles.push(await crop(a, `+${ms} ms`, box, 22));
    }
    await a.page.evaluate(sel => { for (const x of document.getAnimations()) if (x.effect?.target?.closest?.(sel)) x.finish(); }, mine);
    await sheetOf(a, name, tiles, 4, 3);
    return tiles.length;
  };
  if (all || only === 'write') {
    const a = await boot({ served: fullDay(), init: record });
    await openDay(a);
    // ---- the hover's actions: the star, then 问她 ----
    const quick = await a.page.evaluate(() => Object.fromEntries([...document.querySelectorAll('.dt-row, .dt-sh')].map(el => [el.dataset.key, [...(el.querySelector(':scope > .dt-quick')?.children ?? [])].map(c => c.classList.contains('dt-starbtn') ? 'star' : 'ask').join('+')])));
    check('every item has the star in its hover actions, before 问她 where it has that (a talk, a ride, a payment, a bus, a stay, a run); the folded row of sessions, which is no one item, has neither',
      quick['talk-1'] === 'star+ask' && quick['ride-1'] === 'star+ask' && quick['pay-1'] === 'star+ask' && quick['departure-1'] === 'star+ask' && quick['stay-uvic'] === 'star+ask' && quick['workout-1'] === 'star+ask' && quick['code:stay-home-1'] === ''
      && Object.entries(quick).every(([k, v]) => k.startsWith('code:') ? v === '' : v === 'star' || v === 'star+ask'), JSON.stringify(quick));
    await hoverOn(a, 'pay-1'); const his = await starOf(a, quickStar(a, 'pay-1'));
    await hoverOn(a, 'stay-uvic'); const hers = await starOf(a, quickStar(a, 'stay-uvic'));
    await hoverOn(a, 'talk-1'); const none = await starOf(a, quickStar(a, 'talk-1'));
    check('his mark (the payment) is a filled star that says Unmark, her pick (the afternoon at UVic) an outlined one, an item nobody marked a dim outline: the three are told apart, and the two that are not his say Mark as important',
      his.on && his.his && !his.hers && his.label === '取消标记' && !hers.on && hers.hers && !hers.his && hers.label === '标为重要' && !none.on && !none.his && !none.hers && none.label === '标为重要', JSON.stringify({ his, hers, none }));
    // her pick beside his mark: the payment (his) and the afternoon at UVic (hers), each row with the pointer on it, and the bar's two lit stars
    {
      const tiles = [], stars = await lit(a);
      for (const [key, label] of [['pay-1', 'his mark: a filled star in the hover, a filled badge'], ['stay-uvic', 'her pick: an outlined star in the hover, an outlined one by the name']]) {
        await hoverOn(a, key);
        tiles.push({ ...await crop(a, label, await orow(a, key).boundingBox()), k: 1.4 });
      }
      const bar = await a.page.locator('.dt-bar').first().boundingBox();
      tiles.unshift({ ...await crop(a, 'the bar: her pick outlined, his mark filled', { x: bar.x, y: bar.y - 6, width: bar.width, height: bar.height + 12 }), k: 1.6 });
      await sheetOf(a, 'd-02-his-and-hers', tiles, 1);
      check('the sheet of her pick beside his mark is made (the bar and the two rows)', tiles.length === 3 && stars.length === 2);
    }
    check('the bar and the rows wear the same two: both lit stars are on the bar, hers drawn outlined; the payment\'s badge is his, the stay\'s beside its name is hers, and a row nobody marked wears none',
      await litIds(a) === 'pay-1,stay-uvic' && (await lit(a)).find(x => x.id === 'stay-uvic').hers && !(await lit(a)).find(x => x.id === 'pay-1').hers
      && JSON.stringify(await wears(a, 'pay-1')) === '{"gone":false,"hers":false}' && JSON.stringify(await wears(a, 'stay-uvic')) === '{"gone":false,"hers":true}' && await wears(a, 'talk-1') === null);

    // ---- marking: at once, then the brain's word ----
    a.s.lag = 900;
    const hits0 = a.s.hits.length;
    await hoverOn(a, 'talk-1');
    await quickStar(a, 'talk-1').click();
    await a.page.waitForTimeout(90);
    const mid = { star: await starOf(a, quickStar(a, 'talk-1')), bar: await litIds(a), badge: await wears(a, 'talk-1'), posts: writes(a).length };
    check('pressing the star shows it at once, before the brain has answered: the star is filled, the bar has its lit star and the row its badge', mid.star.on && mid.star.his && mid.bar === 'pay-1,stay-uvic,talk-1' && JSON.stringify(mid.badge) === '{"gone":false,"hers":false}' && mid.posts === 0, JSON.stringify(mid));
    check('the frame sheet of the star being set (the star and its four sparks every 40 ms, held from the animations the press started) is made', await frames(a, 'talk-1', 'd-01-star-sparks', [0, 40, 80, 120, 160, 200, 250, 300]) === 8);
    await a.page.waitForTimeout(900);
    const after = { star: await starOf(a, quickStar(a, 'talk-1')), bar: await litIds(a), post: writes(a).map(p => JSON.stringify(p.body)).join(), served: marksOn(a), reads: a.s.hits.length - hits0, title: await orow(a, 'talk-1').locator('.dt-tx b').innerText() };
    check('the brain is told exactly {day, id, on: true}; its answer stands (the day\'s highlights after it); a label that says nothing (talk 10:02) leaves the row\'s title as it was; and the day is read again', after.post === `{"day":"${DATE}","id":"talk-1","on":true}` && after.star.on && after.bar === 'pay-1,stay-uvic,talk-1'
      && after.served === 'pay-1:him,stay-uvic:her,talk-1:him' && after.reads >= 1 && after.title === '和她说话', JSON.stringify(after));
    const log = (await anims(a)).filter(x => x.parent.includes('dt-starbtn') || x.cls.includes('dt-spark'));
    const pop = log.filter(x => x.tag === 'svg' && x.kf.some(k => k.transform === 'scale(.6)')), sparks = log.filter(x => x.cls === 'dt-spark');
    check('the star scales 0.6, 1.15, 1 over 300 ms and four small sparks fly from it over the same 300 ms; the sparks are gone when they have flown', pop.length === 1 && pop[0].opt.duration === 300 && pop[0].kf.map(k => k.transform).join() === 'scale(.6),scale(1.15),scale(1)' && sparks.length === 4 && sparks.every(x => x.opt.duration === 300) && await a.page.locator('.dt-spark').count() === 0, JSON.stringify(log.map(x => [x.tag, x.cls, x.opt.duration])));
    a.s.lag = 0;
    await hoverOn(a, 'talk-1');
    await quickStar(a, 'talk-1').click();
    await a.page.waitForTimeout(90);
    const away = { star: await starOf(a, quickStar(a, 'talk-1')), bar: await lit(a), badge: await wears(a, 'talk-1') };
    check('pressing it again takes the mark off at once: the star is outlined again, and the bar\'s star and the row\'s badge are on their way out (fading), not gone with a jump', !away.star.on && away.bar.find(x => x.id === 'talk-1')?.gone === true && away.badge?.gone === true, JSON.stringify(away));
    await a.page.waitForTimeout(500);
    check('then they are gone; the brain was told {on: false} and has the day it had before', await litIds(a) === 'pay-1,stay-uvic' && (await lit(a)).length === 2 && await wears(a, 'talk-1') === null && writes(a).at(-1).body.on === false && marksOn(a) === 'pay-1:him,stay-uvic:her', marksOn(a));

    // ---- her pick: he can make it his, and take it off (which takes her pick off for him) ----
    await hoverOn(a, 'stay-uvic');
    await quickStar(a, 'stay-uvic').click(); await a.page.waitForTimeout(500);
    const mine = { star: await starOf(a, quickStar(a, 'stay-uvic')), bar: (await lit(a)).find(x => x.id === 'stay-uvic'), badge: await wears(a, 'stay-uvic'), post: JSON.stringify(writes(a).at(-1).body), served: marksOn(a), label: await orow(a, 'stay-uvic').locator('.dt-tx em').innerText() };
    check('marking her pick makes it his: the star fills, the bar\'s star and the stay\'s are filled, she keeps her words for it, and the brain has it as his', mine.star.on && mine.star.his && mine.bar && !mine.bar.hers && mine.badge && !mine.badge.hers && mine.post === `{"day":"${DATE}","id":"stay-uvic","on":true}` && mine.served === 'pay-1:him,stay-uvic:him' && mine.label === '下午在 UVic', JSON.stringify(mine));
    await hoverOn(a, 'stay-uvic');
    await quickStar(a, 'stay-uvic').click(); await a.page.waitForTimeout(700);
    check('and taking it off leaves nothing of hers either: no star on the bar or by the name, the brain told {on: false}, her pick hidden', await litIds(a) === 'pay-1' && await wears(a, 'stay-uvic') === null && writes(a).at(-1).body.on === false && marksOn(a) === 'pay-1:him' && a.s.hidden.has('stay-uvic'), marksOn(a));
    await a.ctx.close();
  }
  if (all || only === 'write') {
    // five of his marks stand: a sixth is a 409. The star does not pretend (nothing lit while it asks), shakes once (120 ms), and a quiet line under the row says why
    const five = ['pay-1', 'move-1', 'call-1', 'work-1', 'sleep-1'].map(id => ({ id, label: '', by: 'him' }));
    const a = await boot({ served: fullDay({ highlights: five }), init: record });
    await openDay(a);
    a.s.lag = 500;
    await hoverOn(a, 'ride-1');
    const rows0 = await orow(a, 'talk-3').boundingBox();
    await quickStar(a, 'ride-1').click();
    await a.page.waitForTimeout(90);
    const asking = { star: await starOf(a, quickStar(a, 'ride-1')), bar: await litIds(a), line: await under(a, 'ride-1') };
    check('with five of his marks standing the star does not pretend: nothing is lit while the brain is asked', !asking.star.on && asking.bar === 'call-1,move-1,pay-1,sleep-1,work-1' && asking.line === '', JSON.stringify(asking));
    await a.page.waitForTimeout(800);
    const said = { star: await starOf(a, quickStar(a, 'ride-1')), bar: await litIds(a), line: await under(a, 'ride-1'), post: JSON.stringify(writes(a).at(-1).body), served: marksOn(a) };
    check('the sixth is a 409: the brain was asked {day, id, on: true}; the star stays off and the day is as it was; the row says 最多标 5 个，先取消一个 under itself', said.post === `{"day":"${DATE}","id":"ride-1","on":true}` && !said.star.on && said.bar === asking.bar && said.line === '最多标 5 个，先取消一个' && said.served === 'call-1:him,move-1:him,pay-1:him,sleep-1:him,work-1:him', JSON.stringify(said));
    const shook = (await anims(a)).filter(x => x.cls.includes('dt-starbtn') && x.kf.some(k => k.transform === 'translateX(-3px)'));
    check('the star shook once, in 120 ms', shook.length === 1 && shook[0].opt.duration === 120, JSON.stringify(shook));
    const grown = await orow(a, 'talk-3').boundingBox();
    check('the line opens under the row in place (the rows under it move down), the row is not open (no open look, no expanded state, the star is still there to press again)', grown.y > rows0.y + 10 && await orow(a, 'ride-1').getAttribute('aria-expanded') === 'false' && await orow(a, 'ride-1').locator(':scope > .dt-fold.is-line').count() === 1 && await orow(a, 'ride-1').locator(':scope > .dt-quick').evaluate(q => getComputedStyle(q).display !== 'none' && getComputedStyle(q).opacity === '1'), JSON.stringify({ rows0, grown }));
    await shotRow(a, 'd-03-cap-line', 'ride-1');
    await a.page.waitForTimeout(4300);
    check('the line goes by itself after four seconds and the rows come back', await under(a, 'ride-1') === '' && await orow(a, 'ride-1').locator(':scope > .dt-fold').count() === 0 && Math.abs((await orow(a, 'talk-3').boundingBox()).y - rows0.y) < 2);
    await a.ctx.close();
  }
  if (all || only === 'write') {
    // any other failure: the star comes back and the line says it did not go through
    const a = await boot({ served: fullDay(), init: record });
    await openDay(a);
    a.s.refuse.add('highlight'); a.s.lag = 500;
    await hoverOn(a, 'move-3');
    await quickStar(a, 'move-3').click();
    await a.page.waitForTimeout(90);
    const trying = { star: await starOf(a, quickStar(a, 'move-3')), bar: await litIds(a) };
    await a.page.waitForTimeout(800);
    const failed = { star: await starOf(a, quickStar(a, 'move-3')), bar: await litIds(a), line: await under(a, 'move-3') };
    check('a mark that fails for any other reason (the brain cannot be reached, a 502): it showed at once, the star comes back off, the bar\'s star goes, and the row says 没标上，稍后再试', trying.star.on && trying.bar.includes('move-3') && !failed.star.on && !failed.bar.includes('move-3') && failed.line === '没标上，稍后再试', JSON.stringify({ trying, failed }));
    await a.ctx.close();
  }
  if (all || only === 'write') {
    // the keys: S marks the row the keys chose (a second S takes it off); not while a field has the cursor; the open row has the star too, before 问她
    const a = await boot({ served: fullDay(), init: record });
    await openDay(a);
    await a.page.keyboard.press('s'); await a.page.waitForTimeout(300);
    check('S with no row chosen does nothing', writes(a).length === 0);
    await a.page.keyboard.press('ArrowDown'); await a.page.waitForTimeout(300);
    const chosen = await a.page.evaluate(() => document.querySelector('.is-pick')?.dataset.key);
    await a.page.keyboard.press('s'); await a.page.waitForTimeout(500);
    check('S marks the row the keys chose', writes(a).length === 1 && writes(a)[0].body.id === chosen && writes(a)[0].body.on === true && (await lit(a)).some(x => x.id === chosen), `${chosen} ${JSON.stringify(writes(a))}`);
    await a.page.keyboard.press('s'); await a.page.waitForTimeout(500);
    check('and S again takes the mark off', writes(a).length === 2 && writes(a)[1].body.on === false && writes(a)[1].body.id === chosen && marksOn(a) === 'pay-1:him,stay-uvic:her');
    await orow(a, 'talk-1').scrollIntoViewIfNeeded(); await orow(a, 'talk-1').hover({ position: { x: 170, y: 14 } }); await rest(a, 250);
    await orow(a, 'talk-1').locator(':scope > .dt-quick .dt-ask').click(); await rest(a, 520);
    await a.page.keyboard.type('sss'); await rest(a, 200);
    check('typed into the input, S is a letter, not a mark', writes(a).length === 2 && await a.page.locator('.dt-ask-in').inputValue() === 'sss');
    await a.page.keyboard.press('Escape'); await rest(a, 500);
    // the open row: the star is among its buttons, before 问她, and does the same
    await tap(a, 'talk-1'); await rest(a, 520);
    const btns = await orow(a, 'talk-1').locator('.dt-acts').first().evaluate(el => [...el.children, ...el.nextElementSibling?.matches('.dt-sayhow') ? [el.nextElementSibling] : []].map(c => c.classList.contains('dt-starbtn') ? 'star' : c.textContent).join('|'));
    check('the open row has the star among its buttons, before 问她', btns === 'star|问她', btns);
    const open = orow(a, 'talk-1').locator('.dt-acts .dt-starbtn');
    await open.click(); await rest(a, 500);
    check('pressed there it marks the same item, and the star in the open row is filled', writes(a).at(-1).body.id === 'talk-1' && writes(a).at(-1).body.on === true && (await starOf(a, open)).on && (await starOf(a, open)).his && await orow(a, 'talk-1').locator(':scope > .dt-quick').isHidden());
    a.s.refuse.add('highlight');
    await open.click(); await rest(a, 600);
    check('a refusal says so under the buttons of the open row, in the row\'s own hint (the star is filled again, as it was)', (await starOf(a, open)).on && await orow(a, 'talk-1').locator('.dt-body .dt-hint').innerText() === '没标上，稍后再试', await orow(a, 'talk-1').locator('.dt-body .dt-hint').innerText());
    await a.ctx.close();
  }
  if (all || only === 'write') {
    // reduced motion: no scale, no sparks, no shake; the star fades in over 120 ms
    const five = ['pay-1', 'move-1', 'call-1', 'work-1', 'sleep-1'].map(id => ({ id, label: '', by: 'him' }));
    const a = await boot({ served: fullDay(), init: record });
    await a.page.emulateMedia({ reducedMotion: 'reduce' });
    await openDay(a);
    await hoverOn(a, 'talk-1');
    await quickStar(a, 'talk-1').click(); await rest(a, 450);
    const log = (await anims(a)).filter(x => x.parent.includes('dt-starbtn') || x.cls.includes('dt-spark'));
    check('with reduced motion the star does not scale and no spark flies: it fades in over 120 ms', log.length === 1 && log[0].tag === 'svg' && log[0].opt.duration === 120 && log[0].kf[0].opacity === 0 && log.every(x => x.kf.every(k => k.transform === undefined)) && await a.page.locator('.dt-spark').count() === 0, JSON.stringify(log));
    await a.ctx.close();
    const b = await boot({ served: fullDay({ highlights: five }), init: record });
    await b.page.emulateMedia({ reducedMotion: 'reduce' });
    await openDay(b);
    await hoverOn(b, 'ride-1');
    await quickStar(b, 'ride-1').click(); await rest(b, 500);
    const shake = (await anims(b)).filter(x => x.cls.includes('dt-starbtn'));
    check('and a refused mark does not shake the star: it fades once over 120 ms, and the line is there all the same', shake.length === 1 && shake[0].opt.duration === 120 && shake[0].kf.every(k => k.transform === undefined) && await under(b, 'ride-1') === '最多标 5 个，先取消一个', JSON.stringify(shake));
    await b.ctx.close();
  }
  if (all || only === 'write') {
    // in English
    const five = ['pay-1', 'move-1', 'call-1', 'work-1', 'sleep-1'].map(id => ({ id, label: '', by: 'him' }));
    const a = await boot({ served: fullDay({ highlights: five }), lang: 'en' });
    await openDay(a);
    await hoverOn(a, 'pay-1'); const his = await starOf(a, quickStar(a, 'pay-1'));
    await hoverOn(a, 'ride-1'); const no = await starOf(a, quickStar(a, 'ride-1'));
    await quickStar(a, 'ride-1').click(); await rest(a, 500);
    check('in English the star says Unmark or Mark as important, and a sixth mark says At most 5 marks. Unmark one first', his.label === 'Unmark' && no.label === 'Mark as important' && await under(a, 'ride-1') === 'At most 5 marks. Unmark one first', `${his.label} ${no.label} ${await under(a, 'ride-1')}`);
    await a.ctx.close();
  }

  // ---- 是我的 / 不是: what mail said he has to go to is confirmed or dropped on the brain (ADR 0274, 0296) ----
  const cm = a => a.s.posts.filter(p => p.path.startsWith('/inherent/commitments/')).map(p => `${p.path} ${JSON.stringify(p.body)}`);
  const pillOf = a => a.page.locator('.dt-waits').innerText().catch(() => null);
  const watchRow = (a, key, ms = 800) => a.page.evaluate(async ([key, ms]) => {
    const out = { rolled: false, faded: false, box: false, heights: [] }, end = performance.now() + ms;
    while (performance.now() < end) {
      out.rolled ||= !!document.querySelector('.dt-roll .is-old');
      out.faded ||= !!document.querySelector(`[data-key="${key}"] .dt-wait.is-gone`);
      const leaving = document.querySelector(`[data-sched="${key}"].is-leaving`);
      out.box ||= !!leaving; if (leaving && out.boxTransition === undefined) out.boxTransition = getComputedStyle(leaving).transitionDuration;
      const r = document.querySelector(`[data-key="${key}"]`); if (r) out.heights.push(Math.round(r.getBoundingClientRect().height));
      await new Promise(r => requestAnimationFrame(r));
    }
    out.gone = !document.querySelector(`[data-key="${key}"]`); out.boxGone = !document.querySelector(`[data-sched="${key}"]`); out.badge = !!document.querySelector(`[data-key="${key}"] .dt-wait`);
    return out;
  }, [key, ms]);
  const btn = (a, key, name) => orow(a, key).locator('.dt-acts').getByRole('button', { name, exact: true });
  if (all || only === 'write') {
    const a = await boot({ served: commitDay(), init: record });
    await openDay(a);
    check('three things wait on him at first: the reminder that rang and the two events mail has not confirmed, each with its badge and a dashed box on the bar', await pillOf(a) === '3 件等你' && await a.page.locator('.dt-wait').count() === 3 && await a.page.locator('[data-sched]').count() === 3, await pillOf(a));
    await tap(a, CM.a); await rest(a, 520);
    const btns = await orow(a, CM.a).locator('.dt-acts').first().evaluate(el => [...el.children, ...el.nextElementSibling?.matches('.dt-sayhow') ? [el.nextElementSibling] : []].map(c => c.classList.contains('dt-starbtn') ? 'star' : c.textContent).join('|'));
    check('an unconfirmed mail event opens to 是我的 and 不是, before the star and 问她, under its time range and state (its source, that it is not confirmed and its place are the row\'s second line already)', btns === '是我的|不是|star|问她' && (await said(a, CM.a))[0] === '22:10–22:50 · 40 分 · 还没开始' && await orow(a, CM.a).locator('.dt-tx em').innerText() === '邮件 · 还没确认 · Library', `${btns} ${(await said(a, CM.a)).join('|')}`);
    await sheetRow(a, 'd-04-commitment-open', CM.a, [['.dt-head', 'the header: three things wait on him (the reminder that rang and the two events mail has not confirmed)', 1.3]], 'the commitment opened: 是我的 and 不是 before the star and 问她, the amber 等你 badge on the row');
    const reads = a.s.hits.length, watching = watchRow(a, CM.a, 900);
    await btn(a, CM.a, '是我的').click();
    const seen = await watching;
    check('是我的 sends the commitment\'s hex in the path and {"action":"confirm"}, nothing else', cm(a).join('|') === `/inherent/commitments/${HEX.a} {"action":"confirm"}`, cm(a).join('|'));
    check('the amber badge fades and goes, the header count rolls down (3 to 2), and the row is not dropped: it is still his, still to come, its dashed box stays', seen.rolled && seen.faded && !seen.badge && !seen.gone && await pillOf(a) === '2 件等你' && await a.page.locator(`[data-sched="${CM.a}"]`).count() === 1, JSON.stringify({ seen: { ...seen, heights: undefined }, pill: await pillOf(a) }));
    await rest(a, 300);
    const lines = await said(a, CM.a);
    check('the row\'s second line says he confirmed it, the open lines say confirmed, the two buttons are gone (the star and 问她 stay), the day was read again and the brain has it confirmed',
      await orow(a, CM.a).locator('.dt-tx em').innerText() === '邮件 · 你确认了 · Library' && lines.join('|').includes('已确认') && await btn(a, CM.a, '是我的').count() === 0 && await btn(a, CM.a, '不是').count() === 0 && await orow(a, CM.a).locator('.dt-acts .dt-starbtn').count() === 1 && a.s.hits.length > reads && a.s.served.items.find(x => x.id === CM.a).confirmed === true, `${lines.join('|')} ${a.s.hits.length} ${reads}`);
    await sheetRow(a, 'd-05-commitment-confirmed', CM.a, [['.dt-head', 'the header after 是我的: two wait now', 1.3]], 'the same row after 是我的: no badge, the second line says 你确认了, the two buttons are gone');
    // he is the one who confirmed it: the words stay when the day is read again (the brain only says it is confirmed)
    await a.page.waitForTimeout(100);
    check('and after another read of the day the row still says he confirmed it (what the brain sends does not say who)', await orow(a, CM.a).locator('.dt-tx em').innerText() === '邮件 · 你确认了 · Library');

    // 不是: the row collapses out (240 ms), the bar's box fades, the count rolls down; the day is read again once the row has gone
    await tap(a, CM.b); await rest(a, 520);
    const reads2 = a.s.hits.length, h0 = (await orow(a, CM.b).boundingBox()).height, ranks = watchRow(a, CM.b, 1800);
    await btn(a, CM.b, '不是').click();
    const went = await ranks;
    check('不是 sends the hex in the path and {"action":"cancel"}', cm(a).at(-1) === `/inherent/commitments/${HEX.b} {"action":"cancel"}` && cm(a).length === 2, cm(a).join('|'));
    const out = (await anims(a)).filter(x => x.key === CM.b && x.kf.some(k => k.height === '0px'));
    check('the row collapses out over 240 ms (its height to nothing, a fade), frame by frame and never back, and is gone; the bar\'s dashed box fades and goes with it; the count rolls down (2 to 1)',
      out.length === 1 && out[0].opt.duration === 240 && out[0].kf.at(-1).opacity === 0 && went.gone && went.boxGone && went.box && went.boxTransition === '0.24s' && went.rolled && went.heights[0] >= h0 - 1 && went.heights.every((x, i, l) => i === 0 || x <= l[i - 1] + 1) && went.heights.at(-1) < 4 && await pillOf(a) === '1 件等你', JSON.stringify({ out, went: { ...went, heights: went.heights.slice(0, 20) }, h0 }));
    check('the day was read again after the row had gone, and the brain no longer has it; the other rows are as they were (the confirmed one, the one he said)', a.s.hits.length > reads2 && !a.s.served.items.some(x => x.id === CM.b) && await orow(a, CM.a).count() === 1 && await orow(a, CM.c).count() === 1 && await a.page.locator(`[data-sched="${CM.b}"]`).count() === 0);

    // 问她 on a commitment event: the id with its @ and the brain's own kind
    await hoverOn(a, CM.c);
    await orow(a, CM.c).locator(':scope > .dt-quick .dt-ask').click(); await rest(a, 520);
    await a.page.keyboard.type('这个几点开始'); await a.page.keyboard.press('Enter'); await rest(a, 350);
    check('问她 is on a commitment event too, and the turn carries exactly its kind (event), its id with the @, its title and its start', turns(a).length === 1 && JSON.stringify(turns(a)[0].body) === JSON.stringify({ text: '这个几点开始', about: { kind: 'event', id: CM.c, title: '跑步', start_ms: T(23, 40) } }), JSON.stringify(turns(a)));
    await a.ctx.close();
  }
  if (all || only === 'write') {
    // the brain did not take it (a 502): the row stays as it was, the buttons come back, and the line under them says so; no day is read again for it
    const a = await boot({ served: commitDay(), init: record });
    await openDay(a);
    a.s.refuse.add('commitments');
    await tap(a, CM.a); await rest(a, 520);
    const reads = a.s.hits.length;
    await btn(a, CM.a, '是我的').click(); await rest(a, 450);
    check('a refusal leaves the row, its badge and the count as they were, the two buttons are there again, and the line says so', await btn(a, CM.a, '是我的').isEnabled() && await btn(a, CM.a, '不是').isEnabled() && await orow(a, CM.a).locator('.dt-wait').count() === 1 && await pillOf(a) === '3 件等你'
      && await orow(a, CM.a).locator('.dt-body .dt-hint').innerText() === '没成功，再试一次' && await orow(a, CM.a).locator('.dt-tx em').innerText() === '邮件 · 还没确认 · Library' && a.s.hits.length === reads, await orow(a, CM.a).locator('.dt-body .dt-hint').innerText().catch(() => '-'));
    await btn(a, CM.a, '不是').click(); await rest(a, 450);
    check('so does a refused 不是: the row is still there', await orow(a, CM.a).count() === 1 && await a.page.locator(`[data-sched="${CM.a}"]`).count() === 1 && cm(a).length === 2);
    a.s.refuse.delete('commitments');
    await btn(a, CM.a, '是我的').click(); await rest(a, 500);
    check('and when the brain takes it the next time the line is gone with the buttons', await orow(a, CM.a).locator('.dt-body .dt-hint').count() === 0 && await btn(a, CM.a, '是我的').count() === 0 && await pillOf(a) === '2 件等你');
    await a.ctx.close();
  }
  if (all || only === 'write') {
    // it was confirmed somewhere else meanwhile (the brain says 409): that is what he asked for, so it is shown as his
    const a = await boot({ served: commitDay(), init: record });
    await openDay(a);
    await tap(a, CM.a); await rest(a, 520);
    a.s.served.items.find(x => x.id === CM.a).confirmed = true;
    await btn(a, CM.a, '是我的').click(); await rest(a, 600);
    check('a confirm of one that is confirmed already (409) is shown as confirmed, with no failure line', await orow(a, CM.a).locator('.dt-tx em').innerText() === '邮件 · 你确认了 · Library' && await orow(a, CM.a).locator('.dt-body .dt-hint').count() === 0 && await pillOf(a) === '2 件等你', await pillOf(a));
    // and one the brain no longer has (a 404) goes the way of one he dropped
    await tap(a, CM.b); await rest(a, 520);
    a.s.served.items = a.s.served.items.filter(x => x.id !== CM.b);
    await btn(a, CM.b, '是我的').click(); await rest(a, 1400);
    check('a commitment the brain no longer has (404) goes out the way a dropped one does: the row, its box and its count (the reminder that rang still waits)', await orow(a, CM.b).count() === 0 && await a.page.locator(`[data-sched="${CM.b}"]`).count() === 0 && await pillOf(a) === '1 件等你' && cm(a).at(-1) === `/inherent/commitments/${HEX.b} {"action":"confirm"}`, await pillOf(a));
    await a.ctx.close();
  }
  if (all || only === 'write') {
    // reduced motion: the row only fades (120 ms), nothing collapses; and in English
    const a = await boot({ served: commitDay(), init: record, lang: 'en' });
    await a.page.emulateMedia({ reducedMotion: 'reduce' });
    await openDay(a);
    await tap(a, CM.a); await rest(a, 400);
    const names = await orow(a, CM.a).locator('.dt-acts').first().evaluate(el => [...el.children, ...el.nextElementSibling?.matches('.dt-sayhow') ? [el.nextElementSibling] : []].map(c => c.classList.contains('dt-starbtn') ? 'star' : c.textContent).join('|'));
    await btn(a, CM.a, 'Mine').click(); await rest(a, 400);
    check('in English the buttons say Mine and Not mine, and the row says you confirmed it', names === 'Mine|Not mine|star|Ask her' && await orow(a, CM.a).locator('.dt-tx em').innerText() === 'Mail · you confirmed it · Library', `${names} ${await orow(a, CM.a).locator('.dt-tx em').innerText()}`);
    await tap(a, CM.b); await rest(a, 400);
    const watching = watchRow(a, CM.b, 1400);
    await btn(a, CM.b, 'Not mine').click();
    const w = await watching, fade = (await anims(a)).filter(x => x.key === CM.b && x.opt.duration === 120 && x.kf.some(k => k.opacity === 0));
    check('with reduced motion the dropped row only fades (120 ms, no height) and the bar\'s box fades over 120 ms too; both are gone', fade.length === 1 && fade[0].kf.every(k => k.height === undefined) && w.boxTransition === '0.12s' && w.gone && w.boxGone, JSON.stringify({ fade, w: { ...w, heights: undefined } }));
    await a.ctx.close();
  }

  // ---- 起个名字: a place that has no name is kept under the one he types, at the stay's own point (ADR 0234, 0296) ----
  const kept = a => a.s.posts.filter(p => p.path === '/inherent/memory/places').map(p => JSON.stringify(p.body));
  const hueOf = (a, key) => orow(a, key).evaluate(el => getComputedStyle(el.closest('.dt-stay')).getPropertyValue('--c').trim());
  const field = a => a.page.evaluate(() => { const i = document.querySelector('.dt-name-in'); return i && { value: i.value, focused: document.activeElement === i, placeholder: i.placeholder, chips: [...document.querySelectorAll('.dt-role')].map(c => `${c.textContent}${c.getAttribute('aria-pressed') === 'true' ? '*' : ''}`).join('|') }; });
  const chip = (a, key, name) => orow(a, key).locator('.dt-role', { hasText: name });
  const barNames = a => a.page.evaluate(() => [...document.querySelectorAll('.dt-seg.is-stay .dt-sl b')].map(b => b.textContent));
  if (all || only === 'write') {
    const a = await boot({ served: nameDay(), init: record });
    await openDay(a);
    const before = { spot: await hueOf(a, 'stay-spot'), school: await hueOf(a, 'stay-uvic'), bar: await barNames(a) };
    await tap(a, 'stay-spot'); await rest(a, 520);
    const btns = await orow(a, 'stay-spot').locator('.dt-acts').first().evaluate(el => [...el.children, ...el.nextElementSibling?.matches('.dt-sayhow') ? [el.nextElementSibling] : []].map(c => c.classList.contains('dt-starbtn') ? 'star' : c.textContent).join('|'));
    check('the open row of a stay that is no place he keeps says 起个名字 among its buttons (before the star, 问她 and 这段在干嘛), and a spot the phone had no words for says nothing else', btns === '起个名字|star|问她|这段在干嘛' && (await said(a, 'stay-spot')).length === 3, `${btns} ${(await said(a, 'stay-spot')).join('|')}`);
    await orow(a, 'stay-spot').getByRole('button', { name: '起个名字' }).click(); await rest(a, 450);
    const f0 = await field(a);
    check('it opens a field with the cursor in it and the kinds of place under it (home, school, other: the brain\'s own three), none picked, and the line says Enter saves and Esc cancels; the button is gone',
      f0 && f0.value === '' && f0.focused && f0.placeholder === '给这个地方起个名字' && f0.chips === '家|学校|其他' && await orow(a, 'stay-spot').locator('.dt-body .dt-hint').innerText() === '回车保存 · Esc 取消' && await orow(a, 'stay-spot').getByRole('button', { name: '起个名字' }).count() === 0, JSON.stringify(f0));
    await a.page.keyboard.type('图书馆');
    await chip(a, 'stay-spot', '学校').click(); await rest(a, 200);
    const f1 = await field(a);
    check('typing fills it and a chip picks a kind (pressed), with the cursor still in the field', f1.value === '图书馆' && f1.focused && f1.chips === '家|学校*|其他', JSON.stringify(f1));
    await shotRow(a, 'd-06-naming-field', 'stay-spot');
    const reads = a.s.hits.length;
    await a.page.keyboard.press('Enter');
    await a.until(() => document.querySelector('[data-key="stay-spot"] .dt-tx b')?.textContent === '图书馆', undefined, 8000); await rest(a, 300);
    check('Enter keeps it: the one route the brain has, with exactly the name, the stay\'s own lat and lng, and the kind he picked', kept(a).join('|') === '{"name":"图书馆","lat":48.4631,"lng":-123.3123,"role":"school"}', kept(a).join('|'));
    const after = { head: await orow(a, 'stay-spot').locator('.dt-tx b').first().innerText(), spot: await hueOf(a, 'stay-spot'), bar: await barNames(a) };
    check('the stay is named in the list and on the bar, and it has the hue of a school (the same as UVic\'s) where it had the neutral one; the day was read again and the brain keeps it',
      after.head === '图书馆' && after.spot === before.school && before.spot !== before.school && after.bar.includes('图书馆') && !before.bar.includes('图书馆') && a.s.hits.length > reads && a.s.served.items.find(x => x.id === 'stay-spot').kept_place.name === '图书馆', JSON.stringify({ before, after }));
    check('the field is gone, and the open row has the rename now (it is a place he keeps)', await a.page.locator('.dt-name').count() === 0 && await orow(a, 'stay-spot').getByRole('button', { name: '改名字' }).count() === 1 && await orow(a, 'stay-spot').getByRole('button', { name: '起个名字' }).count() === 0 && await expanded(a, 'stay-spot') === 'true');
    await sheetRow(a, 'd-07-named', 'stay-spot', [['.dt-bar', 'the bar after the name was kept: 图书馆 in the school\'s blue', 1.4]], 'the row: the name, the hue, and 改名字 where 起个名字 was');
    await a.ctx.close();
  }
  if (all || only === 'write') {
    // no kind picked: the body has no role; a kind picked again is put down; Esc lets go of the field first and then of the row; an empty name asks nothing; the brain refusing keeps the field and what is typed
    const a = await boot({ served: nameDay(), init: record });
    await openDay(a);
    await tap(a, 'stay-aw'); await rest(a, 520);
    await orow(a, 'stay-aw').getByRole('button', { name: '起个名字' }).click(); await rest(a, 450);
    await a.page.keyboard.press('Enter'); await rest(a, 200);
    check('Enter on an empty name asks the brain nothing', kept(a).length === 0 && (await field(a)).focused);
    await a.page.keyboard.type('  快餐  店 ');
    await chip(a, 'stay-aw', '其他').click(); await chip(a, 'stay-aw', '其他').click(); await rest(a, 150);
    check('a kind pressed twice is put down again', (await field(a)).chips === '家|学校|其他');
    a.s.refuse.add('place');
    await a.page.keyboard.press('Enter');
    await a.until(() => document.querySelector('[data-key="stay-aw"] .dt-body .dt-hint')?.textContent === '没记下，再试一次', undefined, 8000);
    const failed = await field(a);
    check('a brain that refuses (a 400) leaves the field with what was typed, the cursor in it, and says it did not keep it; nothing changed on the list', failed.value === '  快餐  店 ' && failed.focused && await orow(a, 'stay-aw').locator('.dt-body .dt-hint').innerText() === '没记下，再试一次' && await orow(a, 'stay-aw').locator('.dt-tx b').first().innerText() === 'A&W' && kept(a).length === 1, JSON.stringify(failed));
    a.s.refuse.delete('place');
    await a.page.keyboard.press('Enter');
    await a.until(() => document.querySelector('[data-key="stay-aw"] .dt-tx b')?.textContent === '快餐 店', undefined, 8000); await rest(a, 200);
    check('and the next time it is kept: the name trimmed (the brain makes the spaces inside one), no role in the body (none was picked), and the row shows the name the brain kept', kept(a).at(-1) === '{"name":"快餐  店","lat":48.4284,"lng":-123.3656}' && await orow(a, 'stay-aw').locator('.dt-tx b').first().innerText() === '快餐 店', kept(a).join('|'));
    // Esc, then Esc
    await tap(a, 'stay-spot'); await rest(a, 520);
    await orow(a, 'stay-spot').getByRole('button', { name: '起个名字' }).click(); await rest(a, 450);
    await a.page.keyboard.type('x');
    await a.page.keyboard.press('Escape'); await rest(a, 350);
    check('Esc lets go of the field (what was typed with it, nothing asked) and the row stays open; the button is back', await a.page.locator('.dt-name').count() === 0 && await expanded(a, 'stay-spot') === 'true' && await orow(a, 'stay-spot').getByRole('button', { name: '起个名字' }).count() === 1 && kept(a).length === 2);
    await a.page.keyboard.press('Escape'); await rest(a, 350);
    check('and the next Esc closes the row', await expanded(a, 'stay-spot') === 'false');
    await a.ctx.close();
  }
  if (all || only === 'write') {
    // what has nothing to name: a stay with no point, a stop on the way, a place he keeps; and in English
    const a = await boot({ served: nameDay(), init: record, lang: 'en' });
    await openDay(a);
    const offered = {};
    for (const key of ['stay-bare', 'stay-uvic', 'stay-aw']) { await tap(a, key); await rest(a, 520); offered[key] = await orow(a, key).locator('.dt-acts').first().evaluate(el => [...el.children, ...el.nextElementSibling?.matches('.dt-sayhow') ? [el.nextElementSibling] : []].map(c => c.classList.contains('dt-starbtn') ? 'star' : c.textContent).join('|')); await tap(a, key); await rest(a, 300); }
    const stop = await a.page.evaluate(() => { const r = document.querySelector('[data-key="stay-stop"]'); return { kind: r.dataset.kind, open: r.getAttribute('aria-expanded') }; });
    check('no offer on a stay with no point (the phone gave none) nor on a place he keeps (it has Rename); in English it says Name this place', offered['stay-bare'] === 'star|Ask her|What was this' && offered['stay-uvic'] === 'Rename|star|Ask her|What was this' && offered['stay-aw'] === 'Name this place|star|Ask her|What was this', JSON.stringify(offered));
    await tap(a, 'stay-aw'); await rest(a, 520);
    await orow(a, 'stay-aw').getByRole('button', { name: 'Name this place' }).click(); await rest(a, 450);
    const f = await a.page.evaluate(() => ({ placeholder: document.querySelector('.dt-name-in').placeholder, chips: [...document.querySelectorAll('.dt-role')].map(c => c.textContent).join('|'), hint: document.querySelector('.dt-body .dt-hint').textContent }));
    check('in English the field and its kinds read A name for this place, Home, School, Other, and Enter saves · Esc cancels', f.placeholder === 'A name for this place' && f.chips === 'Home|School|Other' && f.hint === 'Enter saves · Esc cancels', JSON.stringify(f));
    check('a stop on the way (five minutes between two walks) is no place: it is a row of the trip, which opens to its star alone', stop.kind === 'stay' && await orow(a, 'stay-stop').locator('.dt-acts, .dt-name').count() === 0);
    await a.ctx.close();
  }

  // ---- the review of the notch timeline (50df0775..3662cb6e): each fix has its check here (ONLY=review) ----
  // A day of few items on a pane that is pinned open: the bar runs 06:00 to 24:00 when nothing takes its start back, so `x(ms)` is where a time is on it.
  const barOf = async (served, opts = {}) => {
    const a = await boot({ served, ...opts });
    await openDay(a);
    const g = await a.page.evaluate(() => { const r = document.querySelector('.dt-track').getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width, height: r.height }; });
    return { a, g, y: g.top + g.height / 2, x: ms => g.left + (ms - T(6)) / (T(24) - T(6)) * g.width };
  };

  // a gap lifts nothing on the bar (it used to light the next piece and its row); a tap there still goes to the next row
  if (all || only === 'review') {
    const { a, y, x } = await barOf(small([it('ga', 'stay', T(9, 5), T(11, 0), { place: 'Home', kept_place: home }), it('gt', 'talk', T(10, 0), T(10, 5), { turns: 2 }, 'her'), it('gb', 'stay', T(12, 0), T(13, 0), { place: 'Library', kept_place: uvic })]));
    const lit = () => a.page.evaluate(() => ({ lift: document.querySelectorAll('.dt-seg.is-lift').length, bracket: document.querySelectorAll('.dt-bracket').length, hot: [...document.querySelectorAll('.dt-row.is-hot, .dt-sh.is-hot')].map(e => e.dataset.key),
      head: document.querySelector('.dt-card.is-on .dt-slot.is-cur .dt-ch')?.innerText.replace(/\s*\n\s*/g, ' ') }));
    await a.move(x(T(10, 0)), y); await a.page.waitForTimeout(250);
    let m = await lit();
    check('on a piece the bar lifts it and the list lights its row', m.lift === 1 && m.hot.join() === 'ga', JSON.stringify(m));
    await a.move(x(T(11, 30)), y); await a.page.waitForTimeout(250);
    m = await lit();
    check('in a gap nothing is lifted, bracketed or lit, and the card says nothing was recorded', m.lift === 0 && m.bracket === 0 && m.hot.length === 0 && m.head === '没有记录 11:00–12:00 · 1 时', JSON.stringify(m));
    await a.page.mouse.down(); await a.page.mouse.up(); await a.page.waitForTimeout(150);
    check('a tap in the gap still takes the list to the next row', await orow(a, 'gb').evaluate(el => el.classList.contains('is-flash')));
    await a.ctx.close();
  }

  // a range that ends with the day says 24:00 on the bar's edge, as its card does (the edge said 00:00)
  if (all || only === 'review') {
    const { a, g, y, x } = await barOf(small([it('ha', 'stay', T(9, 5), T(13, 0), { place: 'Home', kept_place: home }), it('ht', 'talk', T(10, 0), T(10, 5), { turns: 2 }, 'her'), it('hb', 'stay', T(13, 0), null, { place: 'Library', kept_place: uvic })]));
    const edges = () => a.page.evaluate(() => [...document.querySelectorAll('.dt-scrub .dt-time')].map(e => e.innerText));
    await a.move(x(T(20, 0)), y); await a.page.mouse.down(); await a.move(x(T(22, 0)), y); await a.move(g.left + g.width + 30, y); await a.page.waitForTimeout(150);
    let labels = await edges();
    check('dragging to the end of the day, the right edge of the band says 24:00', labels.length === 2 && /^(19:5\d|20:0\d)$/.test(labels[0]) && labels[1] === '24:00', labels.join());
    await a.page.mouse.up(); await a.page.waitForTimeout(300);
    labels = await edges();
    const card = await a.page.locator('.dt-card.is-range .dt-ch b').innerText();
    check('and so does the range once it is chosen, as its card does (20:00–24:00)', labels.length === 2 && labels[1] === '24:00' && /–24:00$/.test(card) && card.slice(0, 5) === labels[0], JSON.stringify({ labels, card }));
    await a.ctx.close();
  }

  // a press whose release the bar never heard (the button came up elsewhere; the capture was taken away) lets go: the band does not stay on the bar
  if (all || only === 'review') {
    const day = () => small([it('ha', 'stay', T(9, 5), T(13, 0), { place: 'Home', kept_place: home }), it('ht', 'talk', T(10, 0), T(10, 5), { turns: 2 }, 'her'), it('hb', 'stay', T(13, 0), null, { place: 'Library', kept_place: uvic })]);
    const band = a => a.page.locator('.dt-band').count();
    for (const how of ['a move with no button down', 'the capture lost']) {
      const { a, g, y, x } = await barOf(day());
      await a.move(x(T(15, 0)), y); await a.page.mouse.down(); await a.move(x(T(17, 0)), y); await a.move(x(T(19, 0)), y); await a.page.waitForTimeout(100);
      const before = await band(a);
      // Chrome hands a released capture over with the next pointer input; with none it waits for a hover update, which only a change of the
      // page under the pointer brings (a quiet page does not). The button is still down, so a move of one pixel is that input.
      if (how === 'the capture lost') { await a.page.evaluate(() => document.querySelector('.dt-track').releasePointerCapture(1)); await a.move(x(T(19, 0)) + 1, y); }
      else await a.page.evaluate(([cx, cy]) => document.querySelector('.dt-track').dispatchEvent(new PointerEvent('pointermove', { bubbles: true, clientX: cx, clientY: cy, pointerId: 1, buttons: 0 })), [x(T(20, 0)), y]);
      await a.page.waitForTimeout(100);
      const after = await band(a);
      // the real button is still down here; moving on must not draw the band again
      await a.move(x(T(21, 0)), y); await a.page.waitForTimeout(100);
      const later = await band(a);
      await a.page.mouse.up(); await a.page.waitForTimeout(150);
      check(`${how}: the band on the bar goes, and the pointer is a hover again (no band, no range card after the button comes up)`, before === 1 && after === 0 && later === 0 && await band(a) === 0 && await a.page.locator('.dt-card.is-range').count() === 0, JSON.stringify({ before, after, later }));
      await a.ctx.close();
    }
  }

  // a drag that comes back to where it began is a tap, not a range of no length
  if (all || only === 'review') {
    const { a, y, x } = await barOf(small([it('ha', 'stay', T(9, 5), T(13, 0), { place: 'Home', kept_place: home }), it('ht', 'talk', T(10, 0), T(10, 5), { turns: 2 }, 'her'), it('hb', 'stay', T(13, 0), null, { place: 'Library', kept_place: uvic })]));
    await a.move(x(T(16, 0)), y); await a.page.mouse.down(); await a.move(x(T(18, 0)), y); await a.page.waitForTimeout(100);
    const during = await a.page.locator('.dt-band').count();
    await a.move(x(T(16, 0)), y); await a.page.mouse.up(); await a.page.waitForTimeout(300);
    check('away and back to where it began: no range of no length (no band, no range card), and the tap goes to the row there', during === 1 && await a.page.locator('.dt-band').count() === 0 && await a.page.locator('.dt-card.is-range').count() === 0
      && await orow(a, 'hb').evaluate(el => el.classList.contains('is-flash')));
    await a.move(x(T(14, 0)), y); await a.page.mouse.down(); await a.move(x(T(17, 0)), y); await a.page.mouse.up(); await a.page.waitForTimeout(300);
    check('a drag that ends elsewhere is still a range, with its card', await a.page.locator('.dt-card.is-range').count() === 1 && /^14:0\d|^13:5\d/.test(await a.page.locator('.dt-card.is-range .dt-ch b').innerText()));
    await a.ctx.close();
  }

  // the card's rise (4 px over 120 ms) is a CSS transition of the card's own; the bookkeeping of its height cancelled every animation on it, so it never played
  if (all || only === 'review') {
    const { a, y, x } = await barOf(small([it('ha', 'stay', T(9, 5), T(13, 0), { place: 'Home', kept_place: home }), it('ht', 'talk', T(10, 0), T(10, 5), { turns: 2 }, 'her'), it('hb', 'stay', T(13, 0), null, { place: 'Library', kept_place: uvic })]));
    await a.page.evaluate(() => { window.__tx = []; for (const type of ['transitionend', 'transitioncancel']) document.addEventListener(type, e => { if (e.target.classList?.contains('dt-card-in') && e.propertyName === 'transform') window.__tx.push(type); }, true); });
    await a.move(x(T(10, 30)), y); await a.page.waitForTimeout(450);
    // the pointer goes on along the bar through a stretch of another height: the card's rise is not what it ends
    await a.move(x(T(12, 0)), y); await a.page.waitForTimeout(250); await a.move(x(T(15, 0)), y); await a.page.waitForTimeout(450);
    const tx = await a.page.evaluate(() => window.__tx);
    check('the card rises as it comes: its transform transition runs to its end and nothing cancels it, the pointer moving on', tx[0] === 'transitionend' && !tx.includes('transitioncancel'), tx.join());
    await a.ctx.close();
  }

  // Esc lets go of a lens pinned from a chip the pointer is still on (the chip's own preview held the lens on)
  if (all || only === 'review') {
    const a = await boot({ served: fullDay() });
    await openDay(a);
    const lens = () => a.page.evaluate(() => document.querySelector('.dt').dataset.lens ?? null);
    const chip = a.page.locator('.dt-num', { hasText: 'Mac' });
    await chip.click(); await a.page.waitForTimeout(150);
    check('a click on a chip pins its lens (the pointer is on it)', await lens() === 'work' && await chip.getAttribute('aria-pressed') === 'true');
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(150);
    check('Esc lets the lens go though the pointer is still on the chip, and the pane stays', await lens() === null && await chip.getAttribute('aria-pressed') === 'false' && (await a.read()).tl === 'pinned', String(await lens()));
    await a.ctx.close();
  }

  // a lens previewed by a chip the day then loses (the day was changed with the keys, the pointer resting where the chip was) goes with it
  if (all || only === 'review') {
    const a = await boot({ served: fullDay() });
    await openDay(a);
    const lens = () => a.page.evaluate(() => document.querySelector('.dt').dataset.lens ?? null);
    await a.page.locator('.dt-num', { hasText: 'Mac' }).hover(); await a.page.waitForTimeout(150);
    const rowsDimmed = () => a.page.evaluate(() => [...document.querySelectorAll('.dt-row')].filter(r => getComputedStyle(r).opacity !== '1').length);
    check('the pointer on the Mac chip previews its lens: the rows that are not Mac time step back', await lens() === 'work' && await rowsDimmed() > 5);
    await a.page.keyboard.press('ArrowLeft');
    await a.page.waitForFunction(() => document.querySelector('.dt-date')?.innerText === '10月18日 周日' && document.querySelector('[data-key="y-sleep"]') && !document.querySelector('.dt.is-loading'), null, { timeout: 6000 });
    await a.page.waitForTimeout(300);
    check('the day before has no Mac time, no chip and no lens: the rows are as they are', await lens() === null && await a.page.locator('.dt-num').count() === 1 && await rowsDimmed() === 0, JSON.stringify({ lens: await lens(), chips: await a.page.locator('.dt-num').allInnerTexts(), dimmed: await rowsDimmed() }));
    await a.ctx.close();
  }

  // in English the range of a night from yesterday is long (`yesterday 23:10–07:00 · 7 h 20 min`); the card's name kept no room beside it
  if (all || only === 'review') {
    const read = a => a.page.evaluate(() => {
      const row = document.querySelector('.dt-card.is-on:not(.is-range) .dt-slot.is-cur .dt-ch'), b = row.querySelector('b'), sp = row.querySelector('span');
      return { name: b.innerText, bw: b.clientWidth, cut: b.scrollWidth > b.clientWidth, range: sp.innerText, spw: sp.getBoundingClientRect().width, spCut: sp.scrollWidth > sp.clientWidth, row: row.clientWidth, overflow: getComputedStyle(sp).textOverflow };
    });
    const en = await barOf(fullDay(), { lang: 'en' });
    await en.a.move(en.x(T(6, 30)), en.y); await en.a.page.waitForTimeout(350);
    const ch = await read(en.a);
    check('English, the night from yesterday: the card\'s name keeps its room (Sleep, whole, 39 px or more) and the range gives way with an ellipsis', ch.name === 'Sleep' && ch.bw >= 39 && !ch.cut && ch.range.startsWith('yesterday 23:10–07:00') && ch.overflow === 'ellipsis' && ch.spCut && ch.spw + ch.bw <= ch.row, JSON.stringify(ch));
    await en.a.shot('review-card-english-night', { x: 40, y: 0, width: 560, height: 520 });
    await en.a.ctx.close();
    const zhc = await barOf(fullDay());
    await zhc.a.move(zhc.x(T(6, 30)), zhc.y); await zhc.a.page.waitForTimeout(350);
    const cz = await read(zhc.a);
    check('and in Chinese, where it fits, nothing is cut: 睡觉 and 昨天 23:10–07:00 · 7 时 20 分 whole', cz.name === '睡觉' && !cz.cut && !cz.spCut && cz.range === '昨天 23:10–07:00 · 7 时 20 分', JSON.stringify(cz));
    await zhc.a.ctx.close();
  }

  // a tap on the bar outside the cut the list is in did nothing (its row was not there); it brings the whole day back and goes to the row, as the waiting pill does
  if (all || only === 'review') {
    const { a, g, y, x } = await barOf(small([it('ha', 'stay', T(9, 5), T(13, 0), { place: 'Home', kept_place: home }), it('ht', 'talk', T(10, 0), T(10, 5), { turns: 2 }, 'her'), it('hb', 'stay', T(13, 0), null, { place: 'Library', kept_place: uvic })]));
    await a.move(x(T(14, 0)), y); await a.page.mouse.down(); await a.move(x(T(15, 30)), y); await a.move(x(T(17, 0)), y); await a.page.mouse.up(); await a.page.waitForTimeout(300);
    await a.page.locator('.dt-cbtn').click(); await a.page.waitForTimeout(500);
    const heads = () => a.page.evaluate(() => [...document.querySelectorAll('.dt-list [data-key]')].map(e => e.dataset.key).join());
    check('the list is cut to 14:00–17:00: only the stay that overlaps it, with a chip', await a.page.locator('.dt-filter').count() === 1 && await heads() === 'hb', await heads());
    await a.move(x(T(11, 30)), y); await a.page.mouse.down(); await a.page.mouse.up(); await a.page.waitForTimeout(600);
    check('a tap on the stay at 11:30 (outside the cut) lifts the cut and takes the list to its row, which flashes', await a.page.locator('.dt-filter').count() === 0 && (await heads()).split(',').includes('ha') && await orow(a, 'ha').evaluate(el => el.classList.contains('is-flash')), await heads());
    await a.move(x(T(15, 0)), y); await a.page.waitForTimeout(100);
    await a.ctx.close();
  }

  // a folded pane stays mounted holding what it had (a pinned lens): it must not swallow the next Esc anywhere, and nothing in it keeps moving
  if (all || only === 'review') {
    const a = await boot({ served: fullDay() });
    await openDay(a);
    const running = () => a.page.evaluate(() => document.getAnimations().filter(x => x.playState === 'running' && x.effect?.target?.closest?.('.dt')).length);
    const breath = () => a.page.evaluate(() => getComputedStyle(document.querySelector('.dt-nowtick'), '::after').animationPlayState);
    check('open, the now marker breathes (its animation is running)', await breath() === 'running' && await running() >= 1);
    await a.page.locator('.dt-num', { hasText: 'Mac' }).click(); await a.page.waitForTimeout(100);
    await a.move(a.far.x, a.far.y);
    const folded = await seen(a, r => r.tl === '' && r.dt, 5000);
    await a.page.waitForTimeout(800);
    check('the pane folds with its lens still pinned, and stays mounted', folded.tl === '' && await a.page.evaluate(() => document.querySelector('.dt')?.dataset.lens) === 'work');
    check('folded, nothing in it is running: the breathing of the now marker is paused', await breath() === 'paused' && await running() === 0, JSON.stringify({ breath: await breath(), running: await running() }));
    await a.page.evaluate(() => { window.__esc = 0; document.addEventListener('keydown', e => { if (e.key === 'Escape') window.__esc++; }); });
    await a.page.keyboard.press('Escape'); await a.page.waitForTimeout(100);
    check('an Esc anywhere reaches the rest of the page: the folded pane does not swallow it', await a.page.evaluate(() => window.__esc) === 1);
    await a.ctx.close();
  }

  // a day that cannot be read is said, under its own date, with the day buttons to leave by; it is not today's rows left dimmed, silently, to be tried for ever
  if (all || only === 'review') {
    const a = await boot({ served: fullDay() });
    await openDay(a);
    const state = () => a.page.evaluate(() => ({ title: document.querySelector('.dt-date')?.innerText, say: document.querySelector('.dt > .dt-say')?.innerText ?? null, rows: document.querySelectorAll('.dt-row, .dt-sh').length, loading: !!document.querySelector('.dt.is-loading'),
      prev: !document.querySelector('.dt-nav button:first-child').disabled, next: !document.querySelector('.dt-nav button:last-child').disabled, chips: document.querySelectorAll('.dt-chip, .dt-wx, .dt-wait').length }));
    const settled = async want => { await a.page.waitForFunction(w => document.querySelector('.dt > .dt-say')?.innerText === w, want, { timeout: 8000 }); await a.page.waitForTimeout(150); return state(); };
    a.s.refuse.add('days');
    await a.page.keyboard.press('ArrowLeft');
    let st = await settled('没读到这一天，会再试一次');
    check('the day before cannot be read: its date is the title and the line says so, in place of the rows of today, with no dimming and the day buttons live', st.title === '10月18日 周日' && st.rows === 0 && !st.loading && st.prev && st.next, JSON.stringify(st));
    await a.shot('review-day-unread', { x: 40, y: 0, width: 560, height: 420 });
    await a.page.locator('.dt-nav button:last-child').click(); await a.page.waitForTimeout(700);
    check('the day after takes him back to today: its rows are there', (await state()).title === '10月19日 周一' && (await state()).rows > 10 && (await state()).say === null);
    a.s.refuse.delete('days'); a.s.refuse.add('days-404');
    await a.page.keyboard.press('ArrowLeft');
    st = await settled('这台 Jarvis 还不会给出一天');
    check('a brain that does not serve days says so the same way (and not that it will try again)', st.title === '10月18日 周日' && st.rows === 0 && !st.loading && st.prev && st.next, JSON.stringify(st));
    a.s.refuse.delete('days-404');
    await a.page.locator('.dt-nav button:last-child').click(); await a.page.waitForTimeout(500);
    await a.page.keyboard.press('ArrowLeft');
    await a.page.waitForFunction(() => document.querySelector('[data-key="y-sleep"]') && !document.querySelector('.dt > .dt-say'), null, { timeout: 8000 });
    st = await state();
    check('and a day that can be read again is drawn, with no line left over from the one before', st.title === '10月18日 周日' && st.say === null && st.rows >= 2 && !st.loading, JSON.stringify(st));
    await a.ctx.close();
  }

  // midnight with the pane left open on today: the cut and the range he had belong to the day that ended, and today is read at once, not at the next poll
  if (all || only === 'review') {
    const before = () => small([it('m-home', 'stay', T(9, 5), T(14, 0), { place: '12 Example St, Victoria', kept_place: home }), it('m-talk', 'talk', T(10, 0), T(10, 5), { turns: 2 }, 'her'),
      it('m-lib', 'stay', T(14, 0), T(17, 0), { place: 'Library' }), it('m-end', 'stay', T(17, 0), null, { place: '12 Example St, Victoria', kept_place: home, state: 'now' })], { now_ms: T(23, 58) });
    const nextDay = () => makeDay({ date: '2026-10-20', start_ms: T(0, 0, 20), end_ms: T(24, 0, 20), now_ms: T(0, 1, 20), labels: [], missing: [], line: null, highlights: [],
      items: [it('n-home', 'stay', T(0, 0, 20), null, { place: '12 Example St, Victoria', kept_place: home, state: 'now' }), it('n-talk', 'talk', T(0, 0, 20), T(0, 1, 20), { turns: 2, state: 'done' }, 'her')] });
    const seen = a => a.page.evaluate(() => ({ title: document.querySelector('.dt-date')?.innerText, chip: document.querySelectorAll('.dt-filter').length, band: document.querySelectorAll('.dt-band').length,
      keys: [...document.querySelectorAll('.dt-list [data-key]')].map(e => e.dataset.key).join(), now: document.querySelectorAll('.dt-now').length, say: document.querySelectorAll('.dt-say').length }));
    const drag = async (a, x, y, from, to) => { await a.move(x(from), y); await a.page.mouse.down(); await a.move(x((from + to) / 2), y); await a.move(x(to), y); await a.page.mouse.up(); await a.page.waitForTimeout(300); };
    const { a, y, x } = await barOf(before(), { at: T(23, 58) });
    await drag(a, x, y, T(14, 0), T(17, 0)); await a.page.locator('.dt-cbtn').click(); await a.page.waitForTimeout(400);
    await drag(a, x, y, T(9, 30), T(11, 0));
    const was = await seen(a);
    check('before midnight the list is cut (a chip, the cut\'s bracket and a new range drawn on the bar), on the day that ends at 24:00', was.title === '10月19日 周一' && was.chip === 1 && was.band === 1 && !was.keys.includes('m-talk') && was.keys.includes('m-lib'), JSON.stringify(was));
    const h0 = a.s.hits.length;
    a.s.served = nextDay();
    await a.page.clock.setFixedTime(new Date(T(0, 1, 20)));
    await a.page.waitForFunction(() => document.querySelector('.dt-date')?.innerText === '10月20日 周二', null, { timeout: 4000 });
    await a.page.waitForTimeout(500);
    const after = await seen(a);
    check('at midnight the new day is read at once (within seconds, not at the poll of 30 s): its rows and its now line are there and nothing is left of the cut or the range',
      after.chip === 0 && after.band === 0 && after.keys.includes('n-home') && after.keys.includes('n-talk') && after.now === 1 && after.say === 0 && a.s.hits.length - h0 === 1, JSON.stringify({ ...after, hits: a.s.hits.length - h0 }));
    await a.ctx.close();
    // a brain that still answers with the old day is asked again every 5 s (not every second), and the new day comes with the next answer
    const b = await barOf(before(), { at: T(23, 58) });
    const g0 = b.a.s.hits.length;
    await b.a.page.clock.setFixedTime(new Date(T(0, 1, 20)));
    await b.a.page.waitForTimeout(3500);
    check('a brain that still answers with the old day is asked once in the first seconds, not every second', b.a.s.hits.length - g0 === 1, String(b.a.s.hits.length - g0));
    b.a.s.served = nextDay();
    await b.a.page.waitForFunction(() => document.querySelector('.dt-date')?.innerText === '10月20日 周二', null, { timeout: 8000 });
    check('and the day is drawn with the answer after (the second ask, 5 s on), not 30 s later', b.a.s.hits.length - g0 === 2, String(b.a.s.hits.length - g0));
    await b.a.ctx.close();
  }

  // ---- pane polish B (2026-10-19): the day comes back as he left it, a cut list and a day change are each one move with the pane, the pane opens without
  // passing its height, and a duration with words is set in the pane's face (ONLY=polish) ----
  // Every frame's record of the pane: the list's opacity and rows, the pane, the date over the day and whether it is dimmed (taken in the frame's animation
  // callback, before the page's own callbacks have run); and every change of the pane's or its content's size as it is laid out for painting (a resize
  // observer runs after them), which is where the pane and what is in it are compared.
  const tape = a => a.page.evaluate(() => {
    window.__tape = []; window.__sizes = []; window.__tapeOn = true;
    const inn = document.querySelector('.notch-drop .notch-pane-in'), drop = document.querySelector('.notch-drop');
    window.__tapeRO?.disconnect();
    window.__tapeRO = new ResizeObserver(() => window.__sizes.push({ t: performance.now(), pane: drop.getBoundingClientRect().height, inn: inn.getBoundingClientRect().height }));
    window.__tapeRO.observe(drop); window.__tapeRO.observe(inn);
    const f = now => {
      if (!window.__tapeOn) return;
      const l = document.querySelector('.dt-list'), pr = drop.getBoundingClientRect();
      window.__tape.push({ t: now, op: l ? +getComputedStyle(l).opacity : null, keys: l ? [...l.querySelectorAll('[data-key]')].map(e => e.dataset.key).join() : '', scroll: l?.scrollTop ?? null,
        pane: pr.height, left: pr.left, width: pr.width, inn: inn.getBoundingClientRect().height, date: document.querySelector('.dt-date')?.textContent ?? null, dim: !!document.querySelector('.dt.is-loading') });
      requestAnimationFrame(f);
    };
    requestAnimationFrame(f);
  });
  const untape = a => a.page.evaluate(() => { window.__tapeOn = false; window.__tapeRO.disconnect(); return { frames: window.__tape, sizes: window.__sizes }; });
  // How much of the pane is empty under its content (2 px of it is the pane's own margin).
  const emptiest = frames => Math.max(0, ...frames.filter(f => f.pane > 1 && f.inn > 1).map(f => f.pane - f.inn - 2));
  const emptied = frames => JSON.stringify(frames.filter(f => f.pane > 1 && f.inn > 1 && f.pane - f.inn - 2 > 4).slice(0, 4).map(f => [Math.round(f.t), f.pane, f.inn]));
  if (all || only === 'polish') {
    const state = a => a.page.evaluate(() => {
      const l = document.querySelector('.dt-list'), lr = l?.getBoundingClientRect(), now = l?.querySelector('.dt-now')?.getBoundingClientRect();
      return { date: document.querySelector('.dt-date')?.textContent, scroll: l ? l.scrollTop : null, open: [...document.querySelectorAll('.dt-list [aria-expanded="true"]')].map(e => e.dataset.key).join(),
        nowAt: now && lr ? now.top - lr.top : null, view: lr?.height, yesterday: !!document.querySelector('.dt-list [data-key^="y-"]') };
    });
    // A click on the line: it opens the day pinned, and folds it again.
    const press = async a => { const seg = await a.lineX(); await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up(); };
    const reopened = async a => { await press(a); await settle(a, 'pinned'); await a.until(() => document.querySelector('.notch-drop.is-open .dt-row'), undefined, 8000); await a.page.waitForTimeout(700); };
    const folded = async a => { await press(a); await settle(a, ''); await a.page.waitForTimeout(400); };

    // 1. the day he left, within a minute of folding it
    {
      const a = await boot({ served: fullDay(), init: record });
      await openDay(a);
      const first = await state(a);
      await a.page.evaluate(() => { document.querySelector('.dt-list').scrollTop = 300; });
      await a.page.waitForTimeout(200);
      await tap(a, 'talk-1'); await rest(a, 700);
      const left = await state(a);
      check('before it is folded: the day is on today, scrolled away from its now line, with the talk at 10:02 open', left.date === '10月19日 周一' && left.open === 'talk-1' && Math.abs(left.scroll - first.scroll) > 50, JSON.stringify({ first, left }));
      await folded(a);
      await a.page.evaluate(() => { window.__anims.length = 0; });
      await reopened(a);
      const back = await state(a);
      check('opened again within the minute it is the same day, at the same scroll (within 1 px), with the same row open', back.date === left.date && Math.abs(back.scroll - left.scroll) <= 1 && back.open === 'talk-1', JSON.stringify({ left, back }));
      const rose = (await anims(a)).filter(x => /dt-row|dt-sh|dt-now/.test(x.cls));
      check('and its rows come up again as they do on any open (220 ms each, 25 ms apart)', rose.length >= 3 && rose.every((x, i) => x.opt.duration === 220 && x.opt.delay === i * 25), JSON.stringify(rose.map(x => [x.cls.split(' ')[0], x.opt.duration, x.opt.delay])));
      // the day before, folded and opened again within the minute: the day before
      await a.page.keyboard.press('ArrowLeft');
      await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日' && document.querySelector('.dt-track'), undefined, 6000);
      await a.page.waitForTimeout(500);
      await folded(a); await reopened(a);
      const yd = await state(a);
      check('the day before, folded and opened again within the minute, is the day before (its date and its rows)', yd.date === '10月18日 周日' && yd.yesterday, JSON.stringify(yd));
      // later than a minute: today, at its now line, nothing open (the page's clock is moved on by 61 s while it is folded)
      await a.page.keyboard.press('ArrowRight');
      await a.until(() => document.querySelector('.dt-date')?.textContent === '10月19日 周一' && document.querySelector('.dt-now'), undefined, 6000);
      await a.page.waitForTimeout(500);
      await tap(a, 'talk-1'); await rest(a, 600);
      check('before the minute is up: today again, a row open', (await state(a)).open === 'talk-1');
      await folded(a);
      await a.page.evaluate(() => { const now = performance.now.bind(performance); performance.now = () => now() + 61_000; });
      await reopened(a);
      const late = await state(a);
      check('opened more than a minute after it folded it is made afresh: today, at its now line (60% down the list), with nothing open', late.date === '10月19日 周一' && late.open === '' && Math.abs(late.nowAt - late.view * .6) <= 2, JSON.stringify({ first, late }));
      await a.ctx.close();
    }
    {
      // opened on the day before and folded, a day that was asked for later than a minute ago is not kept either
      const a = await boot({ served: fullDay(), init: record });
      await openDay(a);
      await a.page.keyboard.press('ArrowLeft');
      await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日' && document.querySelector('.dt-track'), undefined, 6000);
      await folded(a);
      await a.page.evaluate(() => { const now = performance.now.bind(performance); performance.now = () => now() + 61_000; });
      await reopened(a);
      check('the day before, opened again more than a minute after it folded, is today again', (await state(a)).date === '10月19日 周一');
      await a.ctx.close();
    }

    // 2. showing only a range, and the whole day again: the list fades out (80 ms), is swapped while it is not there, and comes in (140 ms)
    {
      const day = () => small([it('c-home', 'stay', T(9, 5), T(14, 0), { place: '12 Example St, Victoria', kept_place: home }), it('c-talk', 'talk', T(10, 0), T(10, 5), { turns: 2 }, 'her'),
        it('c-lib', 'stay', T(14, 0), T(17, 0), { place: 'Library' }), it('c-end', 'stay', T(17, 0), null, { place: '12 Example St, Victoria', kept_place: home, state: 'now' })]);
      const { a, y, x } = await barOf(day(), { init: record });
      await a.move(x(T(14, 0)), y); await a.page.mouse.down(); await a.move(x(T(15, 30)), y); await a.move(x(T(17, 0)), y); await a.page.mouse.up(); await a.page.waitForTimeout(400);
      const swapped = frames => { const at = frames.findIndex(f => f.keys !== frames[0].keys); return { at, frame: frames[at], out: frames.slice(0, at).filter(f => f.op < .95 && f.op > .02).length, inn: frames.slice(at).filter(f => f.op < .95 && f.op > .02).length, low: Math.min(...frames.map(f => f.op ?? 1)) }; };
      await tape(a); await a.page.evaluate(() => { window.__anims.length = 0; });
      await a.page.locator('.dt-cbtn').click(); await a.page.waitForTimeout(700);
      let { frames, sizes } = await untape(a), s = swapped(frames);
      const cutKeys = frames.at(-1).keys;
      check('showing only a range: the list fades out, goes through 0, and is swapped for the cut while it is not there (no frame shows both)', s.at > 0 && s.low <= .02 && s.frame.op <= .05 && frames[0].keys.includes('c-talk') && !cutKeys.includes('c-talk') && cutKeys.includes('c-lib'), JSON.stringify({ at: s.at, low: s.low, op: s.frame?.op, first: frames[0].keys, last: cutKeys }));
      check('over 80 ms out and 140 ms in: it is seen going and coming (the in is quick: the day change\'s own easing), the list alone, without a slide', s.out >= 3 && s.inn >= 1, JSON.stringify({ out: s.out, inn: s.inn }));
      let log = (await anims(a)).filter(l => /dt-list/.test(l.cls));
      check('the animations are the day change\'s own: 80 ms out (ease-in, held), 140 ms in (backwards), at the same place (0 px)', log.length === 2 && log[0].opt.duration === 80 && log[0].opt.fill === 'forwards' && log[0].kf[0].transform === 'translateX(0px)' && log[1].opt.duration === 140 && log[1].opt.fill === 'backwards', JSON.stringify(log.map(l => [l.opt, l.kf])));
      check('and the pane takes the height of what is in it at once: never more than 4 px of it empty', emptiest(sizes) <= 4, emptied(sizes));
      await tape(a);
      await a.page.locator('.dt-filter button').click(); await a.page.waitForTimeout(700);
      ({ frames, sizes } = await untape(a)); s = swapped(frames);
      check('and clearing it does the same the other way: out, swapped while the list is not there, in', s.at > 0 && s.low <= .02 && s.frame.op <= .05 && !frames[0].keys.includes('c-talk') && frames.at(-1).keys.includes('c-talk') && s.out >= 3 && s.inn >= 1 && emptiest(sizes) <= 4, JSON.stringify({ at: s.at, low: s.low, op: s.frame?.op, out: s.out, inn: s.inn, empty: emptiest(sizes) }));
      await a.ctx.close();
    }
    {
      // the bar's tap on a row the cut has left out brings the whole day back first (through the fade) and then goes to the row
      const day = () => small([it('c-home', 'stay', T(9, 5), T(14, 0), { place: '12 Example St, Victoria', kept_place: home }), it('c-talk', 'talk', T(10, 0), T(10, 5), { turns: 2 }, 'her'),
        it('c-lib', 'stay', T(14, 0), T(17, 0), { place: 'Library' }), it('c-end', 'stay', T(17, 0), null, { place: '12 Example St, Victoria', kept_place: home, state: 'now' })]);
      const { a, y, x } = await barOf(day());
      await a.move(x(T(14, 0)), y); await a.page.mouse.down(); await a.move(x(T(15, 30)), y); await a.move(x(T(17, 0)), y); await a.page.mouse.up(); await a.page.waitForTimeout(300);
      await a.page.locator('.dt-cbtn').click(); await a.page.waitForTimeout(500);
      await a.move(x(T(10, 2)), y); await a.page.mouse.down(); await a.page.mouse.up();
      await a.page.waitForTimeout(900);
      const got = await a.page.evaluate(() => ({ chip: document.querySelectorAll('.dt-filter').length, keys: [...document.querySelectorAll('.dt-list [data-key]')].map(e => e.dataset.key).join()}));
      check('a tap on the bar outside the cut still brings the whole day back (the chip gone, the talk there again)', got.chip === 0 && got.keys.includes('c-talk'), JSON.stringify(got));
      await a.ctx.close();
    }

    // 3. a day change: the date and the day come together, and the pane has the height of the day that is in it
    for (const [label, lag] of [['a sparse day', 0], ['a slow one', 300]]) {
      const a = await boot({ served: fullDay(), init: record });
      a.s.dayLag = lag;
      await openDay(a);
      await tape(a);
      const t0 = await a.page.evaluate(() => performance.now());
      await a.page.keyboard.press('ArrowLeft');
      await a.until(() => document.querySelector('.dt-date')?.textContent === '10月18日 周日' && document.querySelector('.dt-track'), undefined, 6000);
      await a.page.waitForTimeout(700);
      const taped = await untape(a), frames = taped.frames.filter(f => f.t >= t0), sizes = taped.sizes.filter(f => f.t >= t0);
      const mismatch = frames.filter(f => f.date && f.keys && ((f.date === '10月18日 周日') !== f.keys.split(',').every(k => k.startsWith('y-'))));
      check(`going to the day before (${label}): in no frame is the date of one day over the rows of the other`, mismatch.length === 0, JSON.stringify(mismatch.slice(0, 2)));
      check(`and the pane is never more than 4 px empty under what is in it (${label})`, emptiest(sizes) <= 4, emptied(sizes));
      const flip = frames.find(f => f.date === '10月18日 周日');
      check(`and the new date comes with the new day: ${lag ? `not before the reply (${lag} ms), the old day dimmed under the old date until then` : 'within the fade out (80 ms) and the fade in'}`,
        flip && flip.t - t0 >= lag && flip.t - t0 <= lag + 350 && (!lag || frames.filter(f => f.t - t0 > 120 && f.t < flip.t).every(f => f.dim)), JSON.stringify({ at: flip && flip.t - t0, dim: frames.filter(f => f.dim).length }));
      await tape(a);
      const t1 = await a.page.evaluate(() => performance.now());
      await a.page.keyboard.press('ArrowRight');
      await a.until(() => document.querySelector('.dt-date')?.textContent === '10月19日 周一' && document.querySelector('.dt-now'), undefined, 6000);
      await a.page.waitForTimeout(700);
      const tapedBack = await untape(a), back = tapedBack.frames.filter(f => f.t >= t1), backSizes = tapedBack.sizes.filter(f => f.t >= t1);
      check(`and back to today (${label}): the date and the rows match in every frame, and the pane is not left empty`, back.filter(f => f.date && f.keys && ((f.date === '10月18日 周日') !== f.keys.split(',').every(k => k.startsWith('y-')))).length === 0 && emptiest(backSizes) <= 4, emptied(backSizes));
      await a.ctx.close();
    }

    // 4. the pane opens without passing its height, its width or its left edge
    {
      const a = await boot({ served: fullDay() });
      await a.until(() => document.querySelector('.notch')?.dataset.line);
      await tape(a);
      const seg = await a.lineX();
      await a.move(seg.mid, 14); await a.page.mouse.down(); await a.page.mouse.up();
      await a.until(() => document.querySelector('.notch-drop.is-open .dt-row'), undefined, 8000);
      await a.page.waitForTimeout(1200);
      const frames = (await untape(a)).frames.filter(f => f.pane > 1), end = frames.at(-1);
      check('opening: the pane never goes past the height, the width or the left edge it ends at', Math.max(...frames.map(f => f.pane)) <= end.pane + .5 && Math.max(...frames.map(f => f.width)) <= end.width + .5 && Math.min(...frames.map(f => f.left)) >= end.left - .5 && frames.length > 8,
        JSON.stringify({ end: [end.pane, end.width, end.left], max: Math.max(...frames.map(f => f.pane)), wide: Math.max(...frames.map(f => f.width)), left: Math.min(...frames.map(f => f.left)), n: frames.length }));
      check('and it still takes its time: the height is not the end\'s in the first frames', frames[0].pane < end.pane * .6 && frames.filter(f => f.pane < end.pane - 1).length >= 8);
      await a.ctx.close();
    }
  }

  console.log(`\n${checks.length} checks passed; screenshots in ${dir}`);
} finally {
  await browser.close();
  server.kill();
}
if (errors.length) { console.error('page errors:', errors); process.exit(1); }

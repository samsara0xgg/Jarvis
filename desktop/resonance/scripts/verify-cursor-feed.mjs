// Which pointer samples the main process sends the companion's page (electron/cursorFeed.ts), run against the build (dist-electron).
// The 16 ms tick asks once per tick, as electron/companion.ts does; the key polls of that tick are not part of this.
import assert from 'node:assert/strict';
import { BELOW, FAR_MS, FAR_STEP, MARGIN, reset, shouldSend, start } from '../dist-electron/cursorFeed.js';

const check = (name, pass, detail = '') => { assert.ok(pass, `${name} ${detail}`); console.log(`PASS ${name}`); };
const frame = { x: 1000, y: 0, width: 484, height: 74 }; // not at the origin: samples are judged relative to the window
const TICK = 16;

// `path(t)` is the pointer in screen coordinates; every tick asks, a sample goes to the page when shouldSend says so.
function run(path, { ms = 5000, awake = false, fr = frame } = {}) {
  const feed = start(), sent = [];
  let changes = 0, last = '';
  for (let t = 0; t < ms; t += TICK) {
    const p = path(t), key = `${p.x},${p.y}`;
    if (key !== last) { last = key; changes++; }
    if (shouldSend(feed, p, fr, awake, t)) sent.push({ t, ...p });
  }
  return { sent, changes };
}

// Far away, sweeping: every tick is a different point (the old loop sent all of them, about 60 a second).
const far = t => ({ x: Math.round(2400 + 500 * Math.sin(t / 700)), y: Math.round(1100 + 200 * Math.cos(t / 530)) });
let r = run(far);
check('pointer sweeping far away: the page hears of it at most 8 times a second', r.sent.length / 5 <= 8 && r.changes > 250, `${r.sent.length} of ${r.changes} in 5 s`);
check('pointer sweeping far away: never two samples closer than FAR_MS', r.sent.every((s, i) => i === 0 || s.t - r.sent[i - 1].t >= FAR_MS));

// Near: every change, as ever.
const near = t => ({ x: Math.round(1200 + 200 * Math.sin(t / 400)), y: Math.round(60 + 40 * Math.cos(t / 270)) });
r = run(near);
check('pointer moving in and beside the window: every change is sent', r.sent.length === r.changes && r.changes > 250, `${r.sent.length} of ${r.changes}`);
r = run(t => ({ x: frame.x - MARGIN + Math.round(t / 40), y: 80 }), { ms: 1000 });
check('within the margin on every side of the window it is full rate', r.sent.length === r.changes);
r = run(t => ({ x: frame.x + 100 + Math.round(t / 40), y: frame.height + BELOW - Math.round(t / 40) }), { ms: 1000 });
check('below the window it reaches as far as she follows a pointer at home (260 pt from her)', r.sent.length === r.changes && frame.height + BELOW >= 16 + 260);
for (const [name, p] of [['left', { x: frame.x - MARGIN - 1, y: 30 }], ['right', { x: frame.x + frame.width + MARGIN + 1, y: 30 }], ['below', { x: frame.x + 100, y: frame.height + BELOW + 1 }], ['above', { x: frame.x + 100, y: -MARGIN - 1 }]]) {
  const feed = start(); shouldSend(feed, { x: frame.x + 100, y: 30 }, frame, false, 0);
  shouldSend(feed, { x: p.x, y: p.y }, frame, false, TICK); // first past the margin: sent at once
  check(`past the margin ${name}: the sample after it waits for FAR_MS and FAR_STEP`, !shouldSend(feed, { x: p.x + (p.x > frame.x ? 40 : -40), y: p.y }, frame, false, TICK * 2));
}

// Something on show or under way: every change wherever the pointer is.
r = run(far, { awake: true });
check('awake: every change is sent, wherever the pointer is', r.sent.length === r.changes, `${r.sent.length} of ${r.changes}`);

// What the pointer left must close on time: the first sample past the margin is sent at once, not on the far schedule.
let feed = start();
shouldSend(feed, { x: frame.x + 200, y: 30 }, frame, false, 0);
shouldSend(feed, { x: frame.x + frame.width + MARGIN, y: 30 }, frame, false, TICK); // on the margin: still near
check('leaving: the first sample past the margin is sent at once', shouldSend(feed, { x: frame.x + frame.width + MARGIN + 30, y: 30 }, frame, false, TICK * 2));
check('leaving: the next one 16 ms later is not', !shouldSend(feed, { x: frame.x + frame.width + MARGIN + 60, y: 30 }, frame, false, TICK * 3));

// A pointer that moved and stopped far away: its last position is sent once FAR_MS have passed (each tick asks again), a drift under FAR_STEP is not.
feed = start();
const at = { x: 2400, y: 900 };
shouldSend(feed, at, frame, false, 0);
check('far, moved 40 and stopped: not at once', !shouldSend(feed, { x: at.x + 40, y: at.y }, frame, false, TICK));
check('far, moved 40 and stopped: sent when FAR_MS have passed', shouldSend(feed, { x: at.x + 40, y: at.y }, frame, false, FAR_MS));
check('far, moved 40 and stopped: and only once', !shouldSend(feed, { x: at.x + 40, y: at.y }, frame, false, FAR_MS + TICK));
check('far, a drift under FAR_STEP is never sent', !shouldSend(feed, { x: at.x + 40 + FAR_STEP - 1, y: at.y }, frame, false, 10_000));
check('far, FAR_STEP is', shouldSend(feed, { x: at.x + 40 + FAR_STEP, y: at.y }, frame, false, 10_000));

// Approaching: the pointer crossing the margin runs at full rate over the margin, so the dwell and the speed test see it.
r = run(t => ({ x: frame.x + frame.width + 600 - Math.round(t * .6), y: 30 }), { ms: 1000 }); // 600 pt/s, 10 pt per tick
const inside = r.sent.filter(s => s.x <= frame.x + frame.width + MARGIN);
check('approaching at 600 pt/s: every tick within the margin is sent', inside.length >= MARGIN / 10 - 1, `${inside.length} samples over the last ${MARGIN} pt`);

// A page that has not said (awake until it does) and a reset (the page woke, the pointer was hidden by the tuck): the same point is sent again, once.
feed = start();
check('the first sample is always sent', shouldSend(feed, at, frame, false, 0));
check('the same point again is not', !shouldSend(feed, at, frame, false, 10_000));
reset(feed);
check('after a reset it is, once', shouldSend(feed, at, frame, false, 10_001) && !shouldSend(feed, at, frame, false, 20_000));

// The window moves under a pointer that does not (she follows him to another screen): the point relative to it changed.
feed = start();
shouldSend(feed, { x: 1100, y: 30 }, frame, false, 0);
check('the window moves under a resting pointer: the new relative point is sent', shouldSend(feed, { x: 1100, y: 30 }, { ...frame, x: 1040 }, false, TICK));
console.log('cursor feed: all checks passed');

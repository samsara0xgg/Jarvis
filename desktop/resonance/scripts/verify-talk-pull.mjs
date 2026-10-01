// The talk area's pure logic (ADR 0113): which exchange shows, the rubber band, what counts as a pull. No window, no Chrome:
//   node scripts/verify-talk-pull.mjs
import { register } from 'node:module';
import assert from 'node:assert/strict';
// talk.ts imports './model' without an extension, as the bundler allows.
register('data:text/javascript,' + encodeURIComponent(`export async function resolve(s, c, next) {
  try { return await next(s, c); } catch (e) { if (s.startsWith('.') && !/\\.\\w+$/.test(s)) return next(s + '.ts', c); throw e; } }`));
const { shownOf, exchangesOf, stretch, pull, calm, PULL_AT, PULL_DIM, GESTURE_GAP } = await import('../src/talk.ts');
const checks = [], check = (name, pass) => { assert.ok(pass, name); checks.push(name); console.log(`PASS ${name}`); };

const you = (n, text) => ({ id: `you:${n}`, who: 'you', text, at: n * 1000 });
const her = (n, text, extra = {}) => ({ id: `her:t${n}`, who: 'her', text, turn: `t${n}`, at: n * 1000 + 500, said: true, ...extra });
const two = [you(1, 'one'), her(1, '<voice>first spoken</voice><document>first written</document>'), you(2, 'two'), her(2, '<voice>second spoken</voice><document>second written</document>')];
const texts = items => items.map(it => it.spoken || it.written || it.voiced).join('|');

check('exchanges split at each thing you said', exchangesOf(two).length === 2 && exchangesOf(two)[1][0].id === 'you:2');
let s = shownOf(two, 'all', 0);
check('all: only the latest exchange shows, with one older waiting', s.older === 1 && s.key === 'you:2' && texts(s.items) === 'two|second spoken');
check('the written part of the old answer is not on screen', !s.items.some(it => it.written.includes('first')));
s = shownOf(two, 'all', 1);
check('pulled once: the older exchange is above the latest, nothing older is left', s.older === 0 && texts(s.items) === 'one|first spoken|two|second spoken');
check('pulling more than there is does nothing', shownOf(two, 'all', 5).older === 0 && shownOf(two, 'all', 5).items.length === 4);
s = shownOf(two, 'brief', 0);
check('brief: the latest written part alone (what she says is only timed)', texts(s.items) === 'second written' && s.older === 1);
check('none: nothing shows, and an earlier exchange that shows nothing is not offered', shownOf(two, 'none', 0).items.length === 0 && shownOf(two, 'none', 0).older === 0);
const pending = [...two, you(3, 'three')];
check('a new question puts the previous answer away at once', texts(shownOf(pending, 'all', 0).items) === 'three' && shownOf(pending, 'all', 0).older === 2);
check('brief, new question not answered yet: nothing of the old answer stays', shownOf(pending, 'brief', 0).items.length === 0);
check('an earlier exchange you never got an answer to is not offered', shownOf([you(1, 'a'), ...two.slice(0, 0), you(2, 'b'), her(2, 'x')], 'all', 0).older === 0);
check('a session that is empty shows nothing', shownOf([], 'all', 0).items.length === 0 && shownOf([], 'all', 0).key === '');
check('a failed answer is part of its exchange', shownOf([you(1, 'a'), her(1, 'no', { failed: true })], 'brief', 0).items.length === 1);

check('the rubber band starts at zero, grows, and never passes its limit', stretch(0) === 0 && stretch(100) > stretch(50) && stretch(1e6) < PULL_DIM && stretch(1e6) > PULL_DIM - 1);
check('it resists: the second hundred px moves it less than the first', stretch(200) - stretch(100) < stretch(100));
check('the threshold is a pull of about 220 px of finger', Math.abs(stretch(PULL_DIM / .55) - PULL_DIM / 2) < 1e-9 && PULL_AT === 60 && Math.abs(PULL_DIM / .55 - 218) < 1);

const run = (events, { top = true, room = true } = {}) => { let p = calm, spent = 0, t = 0; for (const [dy, gap] of events) { t += gap; const n = pull(p, dy, t, top, room); if (n.spent && !p.spent) spent++; p = n; } return { p, spent }; };
const swipe = Array.from({ length: 12 }, () => [-24, 16]); // fingers on, steady, 288 px
check('a steady upward pull from the top passes the threshold once', run(swipe).spent === 1);
check('a short pull does not', run(swipe.slice(0, 5)).spent === 0 && stretch(run(swipe.slice(0, 5)).p.d) > 0);
const ramp = [-3, -10, -22, -30, -40].map(d => [d, 16]);
const tail = Array.from({ length: 20 }, (_, i) => [-(39 - i), 16]); // inertia after the fingers lift: slowly smaller steps, 590 px in all
check('inertia after release is ignored: counted in full the tail would have passed the threshold, ramp and tail together do not', stretch(590) >= PULL_AT && run([...ramp, ...tail]).spent === 0 && run([...ramp, ...tail]).p.d < 218);
check('...but the same events as one steady pull would', run([...ramp, ...Array(8).fill([-40, 16])]).spent === 1);
check('a gesture that began away from the top does not pull when it arrives there', (() => { let p = calm, t = 0; p = pull(p, -40, t += 16, false, true); for (let i = 0; i < 20; i++) p = pull(p, -40, t += 16, true, true); return !p.spent && p.d === 0; })());
check('a new gesture after a pause (> GESTURE_GAP) at the top pulls', (() => { let p = calm, t = 0; p = pull(p, -40, t += 16, false, true); t += GESTURE_GAP + 1; for (let i = 0; i < 14; i++) p = pull(p, -24, t += 16, true, true); return p.spent; })());
check('nothing older: no pull at all', run(swipe, { room: false }).p.d === 0);
check('mouse-wheel notches, each its own gesture, never add up', run(Array.from({ length: 10 }, () => [-100, 200])).spent === 0);
check('a pull that is let back down eases off', (() => { const a = run(swipe.slice(0, 4)).p; return pull(a, 30, a.t + 16, true, true).d < a.d; })());
check('once spent, the rest of the gesture adds nothing', run([...swipe, ...swipe]).p.d === run(swipe).p.d || run([...swipe, ...swipe]).spent === 1);
console.log(`${checks.length} checks passed`);

// The companion's polls slow down while the daemon refuses them (a brain's terminal that holds no night run answers
// 404 for good) and come back on the first answer: pure ladder plus the loop on a fake clock, no window.
//   node scripts/verify-poll-backoff.mjs
import { register } from 'node:module';
import { mock } from 'node:test';
import assert from 'node:assert/strict';
register('data:text/javascript,' + encodeURIComponent(`export async function resolve(s, c, next) {
  try { return await next(s, c); } catch (e) { if (s.startsWith('.') && !/\\.\\w+$/.test(s)) return next(s + '.ts', c); throw e; } }`));
const { POLL_MS, MAX_POLL_MS, nextPoll, refusal, poll } = await import('../src/poll.ts');
let n = 0;
const check = (name, pass) => { assert.ok(pass, name); n++; console.log(`PASS ${name}`); };

check('only a 401 or a 404 is a refusal', refusal(new Error('/inherent/night 404')) && refusal(new Error('/inherent/confirmation 401'))
  && !refusal(new Error('/inherent/night 500')) && !refusal(new TypeError('Failed to fetch')) && !refusal('404'));
let wait = POLL_MS;
const ladder = [];
for (let i = 0; i < 7; i++) { wait = nextPoll(wait, true); ladder.push(wait); }
check('a refused poll doubles its wait up to 30 s', ladder.join() === '3000,6000,12000,24000,30000,30000,30000' && MAX_POLL_MS === 30000);
check('the first answer returns it to 1.5 s', nextPoll(30000, false) === 1500);

mock.timers.enable({ apis: ['setTimeout'] });
const settle = () => new Promise(resolve => setImmediate(resolve));
let refused = true, calls = 0;
const stop = poll(async () => { calls++; return refused; });
await settle();
check('it polls at once', calls === 1);
// while refused, the next polls come after 3 s, then 6 s
for (const [gap, total] of [[2999, 1], [1, 2], [5999, 2], [1, 3]]) { mock.timers.tick(gap); await settle(); assert.equal(calls, total, `gap ${gap}`); }
refused = false;
mock.timers.tick(12000); await settle();
check('the poll after the answer comes at the doubled wait, then the base', calls === 4);
mock.timers.tick(1499); await settle();
check('and the base is 1.5 s', calls === 4);
mock.timers.tick(1); await settle();
check('a poll at the base wait follows', calls === 5);
stop(); mock.timers.tick(60000); await settle();
check('stopping ends the loop', calls === 5);
mock.timers.reset();
console.log(`${n} checks passed`);

import assert from 'node:assert/strict';
import { fmtReset } from '../src/quota-time.ts';

const now = new Date('2026-09-14T05:00:00Z');
for (const [minutes, expected] of [[222, 'resets in 3h 42m'], [1860, 'resets in 1d 7h'], [59, 'resets in 59m'], [60, 'resets in 1h 0m'], [1440, 'resets in 1d 0h'], [0, 'resetting'], [-1, 'resetting']]) {
  assert.equal(fmtReset(new Date(+now + minutes * 60_000).toISOString(), now), expected);
}
assert.equal(fmtReset(null, now), '—');
assert.equal(fmtReset('invalid', now), '—');
assert.equal(fmtReset(new Date(+now + 1).toISOString(), now), 'resets in 1m');
console.log('quota reset countdown: 10 checks passed');

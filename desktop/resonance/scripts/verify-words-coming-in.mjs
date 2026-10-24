// His words coming in show under her while he speaks, also when his next words begin before the last ones are accepted
// (Qwen's final words come a second or more after the endpoint): reducer only, no window.
//   node scripts/verify-words-coming-in.mjs
import { register } from 'node:module';
import assert from 'node:assert/strict';
register('data:text/javascript,' + encodeURIComponent(`export async function resolve(s, c, next) {
  try { return await next(s, c); } catch (e) { if (s.startsWith('.') && !/\\.\\w+$/.test(s)) return next(s + '.ts', c); throw e; } }`));
const { reducer, initialState } = await import('../src/model.ts');
let n = 0;
const check = (name, pass) => { assert.ok(pass, name); n++; console.log(`PASS ${name}`); };
const run = (s, ...as) => as.reduce(reducer, s);
// what runtime.ts dispatches for the daemon's voice phases
const listening = (turnId) => ({ type: 'phase', phase: 'hearing', turnId });
const transcribing = (turnId) => ({ type: 'phase', phase: 'processing', turnId });
const accepted = (turnId, text) => [{ type: 'phase', phase: 'processing', turnId }, { type: 'pending', turnId, at: 5 }, { type: 'heard', text, at: 5, turnId }];
const empty = (turnId) => ({ type: 'phase', phase: 'listening', turnId });
const partial = (text) => ({ type: 'partial', text });
const yoursOf = (s) => s.talk.filter(l => l.who === 'you').map(l => l.text);

let s = run(initialState, listening('T1'), partial('现在几'), transcribing('T1'), ...accepted('T1', '现在几点'));
check('one utterance: its words come in, then are heard', s.heard === '现在几点' && !s.inFlight && s.partial === '' && yoursOf(s).join() === '现在几点');

s = run(initialState, listening('T1'), partial('现在几点'), transcribing('T1'), listening('T2'), partial('明天'), ...accepted('T1', '现在几点'));
check('the next words began before the last were accepted: they still show', s.inFlight && s.partial === '明天' && s.phase === 'hearing');
check('the earlier words join the talk all the same', yoursOf(s).join() === '现在几点');
s = run(s, partial('明天呢'));
check('and the next words go on growing', s.partial === '明天呢');
s = run(s, transcribing('T2'), ...accepted('T2', '明天呢'));
check('then they are heard in their turn', !s.inFlight && s.heard === '明天呢' && yoursOf(s).join('|') === '现在几点|明天呢');

s = run(initialState, listening('T1'), transcribing('T1'), listening('T2'), partial('等一下'), empty('T1'));
check('an earlier utterance that came to nothing leaves the words coming in', s.inFlight && s.partial === '等一下');
s = run(s, empty('T2'));
check('their own coming to nothing ends them', !s.inFlight && s.partial === '');

s = run(initialState, listening('T1'), partial('你好'), { type: 'phase', phase: 'listening' });
check('a phase with no turn (the socket opening again) ends the words coming in, as before', !s.inFlight && s.partial === '');
console.log(`${n} checks passed`);

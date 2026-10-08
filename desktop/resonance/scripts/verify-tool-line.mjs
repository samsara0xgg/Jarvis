// The tool status line under her (ADR 0115) never outlives its turn: reducer only, no window.
//   node scripts/verify-tool-line.mjs
import { register } from 'node:module';
import assert from 'node:assert/strict';
register('data:text/javascript,' + encodeURIComponent(`export async function resolve(s, c, next) {
  try { return await next(s, c); } catch (e) { if (s.startsWith('.') && !/\\.\\w+$/.test(s)) return next(s + '.ts', c); throw e; } }`));
const { reducer, initialState, toolLine } = await import('../src/model.ts');
let n = 0;
const check = (name, pass) => { assert.ok(pass, name); n++; console.log(`PASS ${name}`); };
const run = (s, ...as) => as.reduce(reducer, s);
const waiting = (id) => run(initialState, { type: 'pending', turnId: id, at: 1 });
const tool = (turnId, label) => ({ type: 'tool', turnId, label });
const open = (turnId) => ({ type: 'open', turnId, responseId: `r-${turnId}`, at: 2 });

let s = run(waiting('T1'), tool('T1', 'Searching the web...'));
check('a tool of the waiting turn shows', toolLine(s) === 'Searching the web...');
s = run(s, tool('T1', 'Checking your mail...'));
check('the next tool of the same turn takes its place', toolLine(s) === 'Checking your mail...');
check('the turn ends without ever opening an answer: its clear takes the line away', toolLine(run(s, tool('T1', ''))) === '' && run(s, tool('T1', '')).tool === null);
check('its answer opening clears it too', run(s, open('T1')).tool === null);
check('a late label after the answer opened is cleared by the turn end', run(s, open('T1'), tool('T1', 'Searching the web...'), tool('T1', '')).tool === null);
check('a later turn opening before that clear does not strand it', run(waiting('T1'), open('T1'), tool('T1', 'Searching the web...'), open('T2'), tool('T1', '')).tool === null);
check('the clear of another turn leaves this turn line alone', run(s, tool('T0', '')).tool?.turnId === 'T1');
check('a cancelled waiting turn clears its line', run(s, { type: 'failed', turnId: 'T1', cancelled: true, message: '', at: 3 }).tool === null);
check('a failed or cancelled turn clears its line even when another turn is the one waited on', run(waiting('T1'), tool('T1', 'Searching the web...'), { type: 'pending', turnId: 'T2', at: 4 }, { type: 'failed', turnId: 'T1', cancelled: true, message: '', at: 3 }).tool === null);
check('another turn failing leaves this turn line alone', run(s, { type: 'failed', turnId: 'T9', cancelled: false, message: 'x', at: 3 }).tool?.turnId === 'T1');
console.log(`${n} checks passed`);

// Run after npm run build. Pure history checks plus the real host's transcript builder.
import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { timeline } from '../src/agents/exposure/timeline.ts';

process.env.JARVIS_AGENTS_DIR = await mkdtemp(path.join(os.tmpdir(), 'jarvis-exposure-check-'));
const { Session } = await import('../dist-electron/agents/host.js');
const base = { id: 'exposure-check', agent: 'codex', title: 'History check', cwd: '/tmp', project: 'check', branch: '', tree: false,
  st: 'done', pinned: false, parked: false, archived: false, unread: false, updated: 900000, summary: 'Ready', model: '', effort: '', mode: '', ctx: 0 };

const items = [
  { k: 'you', text: 'First', at: 60000 },
  { k: 'steps', steps: [{ k: 'read', t: 'file', at: 90000 }] },
  { k: 'req', req: { id: 'q' }, at: 120000, ended: 180000, done: 'allowed' },
  { k: 'it', text: 'Finished', at: 240000 },
  { k: 'you', text: 'Second', at: 300000 },
];
const trail = timeline({ ...base, st: 'err', summary: 'Stopped', trace: [{ at: 360000, st: 'err' }] }, items);
assert.deepEqual(trail.segs.map(s => [s.a, s.b, s.k]), [[1, 2, 'work'], [2, 3, 'wait'], [3, 4, 'work'], [4, 5, 'idle'], [5, 6, 'work'], [6, null, 'stop']]);
assert.deepEqual(trail.turns.map(t => [t.at, t.reply, t.kind]), [[60000, 'Finished', 'sum'], [300000, 'Stopped', 'err']]);
assert.deepEqual(trail.activity, [1.5]);

const unknown = timeline(base, [{ k: 'you', text: 'Untimed' }, { k: 'it', text: 'Readable' }]);
assert.equal(unknown.turns[0].at, undefined);
assert.deepEqual(unknown.marks, []);
assert.deepEqual(unknown.segs, [{ a: 15, b: null, k: 'idle' }]);

const tied = timeline({ ...base, st: 'work', trace: [{ at: 240000, st: 'work' }] }, items.slice(0, 4));
assert.equal(tied.segs.at(-1).k, 'work');

const historical = new Session({ ...base }, '');
await historical.build(async () => {
  historical.you('A', [], 60000);
  historical.say('Answer A', 120000);
  historical.you('B', [], 180000);
  historical.tool('read', { k: 'read', t: 'file' }, 210000);
  historical.say('Answer B', 240000);
});
assert.deepEqual(historical.items.filter(i => i.k === 'you' || i.k === 'it').map(i => [i.k, i.at]), [['you', 60000], ['it', 120000], ['you', 180000], ['it', 240000]]);
assert.equal(historical.items.find(i => i.k === 'steps').steps[0].at, 210000);
assert.equal(historical.s.created, 60000);
assert.equal(historical.s.trace, undefined, 'replay must not invent observed state transitions');

const legacy = new Session({ ...base }, '');
await legacy.build(async () => { legacy.you('No clock'); legacy.say('Still no clock'); });
assert(legacy.items.every(i => i.at === undefined));
console.log('5 acceptance checks passed: timed transitions, untimed history, transition precedence, transcript replay, legacy timestamps.');

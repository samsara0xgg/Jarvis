// Run after npm run build. Pure history checks plus the real host's transcript builder.
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { timeline } from '../src/agents/exposure/timeline.ts';

process.env.JARVIS_AGENTS_DIR = await mkdtemp(path.join(os.tmpdir(), 'jarvis-exposure-check-'));
const { Session, pic } = await import('../dist-electron/agents/host.js');
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
// A message of pictures alone reads as their names, never as [object Object].
assert.equal(timeline(base, [{ k: 'you', text: '', files: [{ name: 'shot.png', img: 'a' }, { name: '图片 2' }] }]).turns[0].you, 'shot.png、图片 2');

const tied = timeline({ ...base, st: 'work', trace: [{ at: 240000, st: 'work' }] }, items.slice(0, 4));
assert.equal(tied.segs.at(-1).k, 'work');

// B01's words for each turn: its last answer wins; otherwise where the last turn stands; otherwise what it did.
const bash = { id: 'b', tool: 'Bash', why: 'Build it again', cmd: 'npm run build', cwd: '/tmp', always: '' };
const turns = (s, list) => timeline({ ...base, ...s }, list).turns.map(t => [t.kind, t.reply]);
assert.deepEqual(turns({ st: 'wait', summary: 'Wants npm' }, [{ k: 'you', text: 'A', at: 60000 }, { k: 'steps', steps: [{ k: 'read', t: 'f' }] }, { k: 'req', req: bash, at: 90000 }]), [['wait', 'Build it again']]);
assert.deepEqual(turns({ st: 'wait' }, [{ k: 'you', text: 'A' }, { k: 'it', text: 'Answered first' }, { k: 'req', req: bash }]), [['sum', 'Answered first']]);
assert.deepEqual(turns({ st: 'work', now: 'Editing x.ts' }, [{ k: 'you', text: 'A' }, { k: 'steps', steps: [{ k: 'edit', t: 'x.ts', add: 3, del: 1 }], took: '2 分钟' }, { k: 'you', text: 'B' }]),
  [['steps', '干了 2 分钟 · 改了 1 个 +3 −1'], ['live', 'Editing x.ts']]);
assert.deepEqual(turns({}, [{ k: 'you', text: 'A' }, { k: 'you', text: 'B' }]), [['none', '没等它回，你接着又说了一句'], ['none', '还没回']]);
assert.deepEqual(turns({}, [{ k: 'you', text: 'A' }, { k: 'steps', steps: [{ k: 'read', t: 'a' }, { k: 'read', t: 'b' }], took: '1 分钟' }, { k: 'req', req: bash, done: '允许了' },
  { k: 'steps', steps: [{ k: 'edit', t: 'x', add: 3, del: 1 }], took: '20 秒' }]), [['steps', '干完了 · 读了 2 个 · 改了 1 个 +3 −1']]);
assert.deepEqual(turns({ st: 'err', summary: 'Host restarted' }, [{ k: 'you', text: 'A' }, { k: 'it', text: 'Done' }]), [['err', 'Host restarted']]);

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
// A picture sent with a message becomes a copy named by its content; the same bytes find the same copy; no image, no copy.
await mkdir(path.join(process.env.JARVIS_AGENTS_DIR, 'images'));
const png = `data:image/png;base64,${Buffer.from('picture bytes').toString('base64')}`, first = pic('图片 1', png);
assert.match(first.img, /^[0-9a-f]{32}\.png$/);
assert.equal(pic('图片 2', png).img, first.img);
assert.equal(await readFile(path.join(process.env.JARVIS_AGENTS_DIR, 'images', first.img), 'utf8'), 'picture bytes');
assert.deepEqual(pic('图片 3', 'https://example.com/a.png'), { name: '图片 3' });
console.log('8 acceptance checks passed: timed transitions, untimed history, picture-only messages, transition precedence, turn words, transcript replay, legacy timestamps, picture copies.');

// Her captions follow the audio (ADR 0112): the mapping from the daemon's heard text onto the displayed line, and the
// model keeping it per line. Needs no window or built page: node strips the types. Run: node scripts/verify-heard.mjs
import assert from 'node:assert/strict';
import { heardCount } from '../src/heard.ts';
import { initialState, reducer } from '../src/model.ts';

const checks = [];
const check = (name, ok) => { assert.ok(ok, name); checks.push(name); console.log(`PASS ${name}`); };

check('a clean prefix counts its characters', heardCount('你好，我是贾维斯。', '你好，我') === 4);
check('nothing heard yet is zero, all heard is the whole line', heardCount('你好', '') === 0 && heardCount('你好', '你好') === 2);
check('spacing and the joins between <voice> parts do not matter', heardCount('First part.\nSecond part.', 'First part.Second') === 18);
check('markdown and emoji the daemon does not speak are skipped', heardCount('这是 **重点** 内容 🙂 好的', '这是重点内容好') === 16);
check('heard that is not the start of the line is no position (the estimate takes over)', heardCount('你好，我是贾维斯。', '再见') === undefined);
check('heard longer than the line is no position', heardCount('你好', '你好呀') === undefined);
check('a link written as markdown is no position, and never throws', heardCount('see [docs](http://x.y) now', 'see docs now') === undefined);

const her = (s, turn, text) => reducer(s, { type: 'her', turn, text, at: 1 });
let s = her(initialState, 'T1', '<voice>你好，我是贾维斯。</voice>');
const line = st => st.talk.find(l => l.id === 'her:T1');
check('a line has no reported position until the daemon sends one', line(s).heard === undefined);
s = reducer(s, { type: 'playback', turn: 'T1', heard: '你好，我' });
check('the reported position lands on its turn\'s line', line(s).heard === '你好，我');
const same = reducer(s, { type: 'playback', turn: 'T1', heard: '你好，我' });
check('the same report changes nothing', same === s);
check('a report for a turn with no line changes nothing', reducer(s, { type: 'playback', turn: 'T9', heard: 'x' }) === s);
s = reducer(s, { type: 'cut', at: 5 });
check('a cut keeps where the audio had got to', line(s).cutAt === 5 && line(s).heard === '你好，我');
s = reducer(s, { type: 'playback', turn: 'T1', heard: '你好，我是' });
check('the final report after a stop still lands', line(s).heard === '你好，我是');
console.log(`${checks.length} checks passed`);

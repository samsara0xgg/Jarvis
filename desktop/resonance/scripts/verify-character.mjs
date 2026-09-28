// Exercise the real character state machine without requiring a GPU or opening a desktop window.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';

const media = { matches: false };
globalThis.matchMedia = () => media;
let seed = 7;
Math.random = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32);
const source = readFileSync(new URL('../src/starCore.ts', import.meta.url), 'utf8')
  .replace("import nebulaUrl from './assets/skins/icon-sky.jpg?inline';", "const nebulaUrl = '';");
const { Core, MOTION_V2, LAYERS_ON } = await import(`data:text/javascript;base64,${Buffer.from(stripTypeScriptTypes(source)).toString('base64')}`);
const make = () => new Core('glass', MOTION_V2, LAYERS_ON);
const input = (extra = {}) => ({ expr: '02', look: null, still: false, pressed: false, charge: 0, ...extra });
const checks = [];
const check = (label, pass) => { assert.ok(pass, label); checks.push(label); };

const core = make();
core.update(1000, 1 / 60, input({ look: [.8, -.3] }));
check('eyes arrive before the body turns', core.s.ex.value > core.s.gx.value * 2);
check('balanced idle actions are spaced 5–11 seconds apart', core.idleAt - 1000 >= 5000 && core.idleAt - 1000 <= 11000);
for (let t = 1016; t < 5000; t += 16) core.update(t, .016, input({ look: [.8, -.3] }));
check('a stationary cursor loses her attention', !core.attn.on);
for (let t = 5000; t <= 5080; t += 16) core.update(t, .016, input({ look: [-.8, .3] }));
check('cursor movement regains her attention', core.attn.on);

const hop = make();
hop.update(1000, .016, input()); hop.hop(1000, .3);
hop.update(1032, .032, input());
check('a hop begins with a crouch', hop.hopStart > 1032 && hop.s.stretch.value < 1);
hop.update(1180, .032, input());
check('the body rises after the crouch', hop.st.yOff < -.1);

const notice = make();
notice.update(1000, .016, input({ expr: 'ask', poi: { g: [.2, .7], at: 1000, why: 'card' } }));
check('a new notice draws her eyes to its card with a small reaction',
  notice.focus?.g[0] === .2 && notice.focus?.g[1] === .7 && notice.acts.some(a => a.kind === 'flinch'));

const deep = make();
for (let t = 0; t < 3000; t += 16) deep.update(t, .016, input({ expr: 'deep', deep: true }));
check('think mode keeps its established violet light',
  deep.light.glow.every((channel, i) => Math.abs(channel - [154, 134, 255][i] / 255) < .001));

const lively = make(), tired = make();
lively.mood.energy.value = .6; tired.mood.energy.value = .2;
for (const c of [lively, tired]) { c.enter('35', 0); c.blinkAt = 1000; }
for (const t of [1000, 1320]) {
  lively.update(t, .016, input({ expr: '35', mood: { energy: .6, joy: .5 } }));
  tired.update(t, .016, input({ expr: '35', mood: { energy: .2, joy: .5 } }));
}
check('tired blinks last longer than normal energy blinks', lively.blinkStart < 0 && tired.blinkStart === 1000);

media.matches = true;
const quiet = make();
quiet.update(1000, .016, input()); quiet.hop(1000, .3); quiet.act('wiggle', 1000);
check('reduced motion suppresses hops and idle gestures', quiet.hopStart < 0 && quiet.acts.length === 0 && quiet.idleAt === 0);
console.log(`Character acceptance: ${checks.length} checks passed.`);
for (const label of checks) console.log(`  ${label}`);

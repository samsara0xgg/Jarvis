// Acceptance for the mic and speaker cues: they must be synthesized haptic click
// patterns (Hermes' `selection` / `open` / `close` intents), not rendered samples.
// The observable is the `jarvis:feedback` event each toggle emits — its `duration`
// is what separates a 16-70 ms click pattern from the 0.33-0.55 s WAVs it replaced.
import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { existsSync, readdirSync } from 'node:fs';

const checks = [];
const check = (name, ok) => { assert.ok(ok, name); checks.push(name); console.log(`PASS ${name}`); };
const app = await electron.launch({ args: ['.', '--verify'], cwd: process.cwd() });
try {
 const page = await app.firstWindow();
 await page.evaluate(() => localStorage.clear());
 await page.reload();
 const errors = []; page.on('pageerror', error => errors.push(error.message));
 await page.evaluate(() => { window.__cues = []; window.addEventListener('jarvis:feedback', e => window.__cues.push(e.detail)); });
 const cues = () => page.evaluate(() => window.__cues);
 await page.waitForSelector('.presentation-capsule');
 await page.waitForTimeout(400);
 check('launch plays no cue', (await cues()).length === 0);

 // The edge toggles are inert until the capsule opens.
 await page.getByRole('button', { name: '进入 Live' }).click();
 await page.waitForSelector('.presentation-capsule:not(.is-collapsed)');
 await page.waitForTimeout(300);
 await page.evaluate(() => { window.__cues = []; });

 const press = async (label, expected) => {
  await page.getByRole('button', { name: label, exact: true }).click();
  await page.waitForFunction(cue => window.__cues.at(-1)?.cue === cue, expected);
  await page.waitForTimeout(260);
 };
 await press('麦克风静音', 'mic-off');
 await press('麦克风静音', 'mic-on');
 await press('扬声器静音', 'speaker-off');
 await press('扬声器静音', 'speaker-on');

 const fired = await cues();
 check('each toggle direction emits its own cue', fired.map(c => c.cue).join(',') === 'mic-off,mic-on,speaker-off,speaker-on');
 // 16 ms `selection` both ways; 70 ms `close` and 76 ms `open` for the speaker pair.
 check('cue lengths are haptic click patterns, not samples', fired.map(c => c.duration.toFixed(3)).join(',') === '0.016,0.016,0.070,0.076');
 check('mic uses one pattern for both directions, as Hermes does', fired[0].duration === fired[1].duration);
 check('speaker open and close are distinguishable', fired[2].duration !== fired[3].duration);
 check('cues honour the saved volume preference', fired.every(c => c.volume === .35));

 const shipped = readdirSync('dist/audio');
 check('only voice-enter still ships as an asset', shipped.join(',') === 'voice-enter.wav');
 check('replaced samples are gone from source', ['mic-on', 'mic-off', 'speaker-on', 'speaker-off'].every(c => !existsSync(`public/audio/${c}.wav`)));
 check('no renderer errors', errors.length === 0);
 console.log(`\n${checks.length} checks passed`);
} finally {
 await app.close();
}

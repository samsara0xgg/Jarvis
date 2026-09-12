import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';

const app = await electron.launch({ ...(process.argv.includes('--packaged') ? { executablePath: 'build/Jarvis Resonance.app/Contents/MacOS/Electron', args: [] } : { args: ['.', '--verify'] }), cwd: process.cwd() });
try {
  const page = await app.firstWindow();
  await page.emulateMedia({ reducedMotion: 'no-override' });
  // Observe the actual paths sent to Canvas, including the frames immediately
  // around a state change. Endpoint screenshots alone cannot catch a snap.
  await page.addInitScript(() => {
    window.motionFrames = [];
    const proto = CanvasRenderingContext2D.prototype;
    const clear = proto.clearRect, move = proto.moveTo, line = proto.lineTo, stroke = proto.stroke;
    let points = [], strands = 0;
    proto.clearRect = function (...args) { points = []; strands = 0; return clear.apply(this, args); };
    for (const [name, original] of [['moveTo', move], ['lineTo', line]]) {
      proto[name] = function (x, y) { points.push(x, y); return original.call(this, x, y); };
    }
    proto.stroke = function (...args) {
      const result = stroke.apply(this, args);
      if (this.canvas.matches('.voice-presence') && ++strands === 7) {
        window.motionFrames.push({ time: performance.now(), state: this.canvas.dataset.state, points: [...points], alpha: this.globalAlpha });
      }
      return result;
    };
  });
  await page.reload();
  await page.locator('.voice-pill').click({ button: 'right' });
  const select = page.getByLabel('声纹状态', { exact: true });
  await select.selectOption('standby');
  await page.waitForTimeout(1400);
  await page.locator('canvas').evaluate(c => { window.originalPresence = c; });
  const transitions = [];
  for (const state of ['listening', 'thinking', 'speaking', 'muted', 'standby']) {
    await page.evaluate(() => { window.motionFrames = window.motionFrames.slice(-1); });
    await select.selectOption(state);
    await page.waitForTimeout(1600);
    const frames = await page.evaluate(() => window.motionFrames);
    const entering = frames.findIndex((f, i) => i && f.state !== frames[i - 1].state);
    assert.ok(entering > 0, `${state}: observed the transition boundary`);
    const first = frames[entering], before = frames[entering - 1];
    assert.ok(Math.abs(first.points[0] - before.points[0]) < .4, `${state}: width does not jump on entry`);
    assert.ok(Math.abs(first.alpha - before.alpha) < .025, `${state}: luminance does not jump on entry`);
    const steps = frames.slice(1).map((f, i) => ({
      dt: f.time - frames[i].time,
      distance: Math.max(...f.points.map((p, j) => Math.abs(p - frames[i].points[j]))),
    })).filter(f => f.dt > 5 && f.dt < 40);
    assert.ok(steps.length > 15, `${state}: enough rendered frames to assess motion`);
    assert.ok(steps.every(f => f.distance < 3), `${state}: no discontinuity in the drawn strands`);
    assert.ok(Math.max(...first.points.map((p, i) => Math.abs(p - frames.at(-1).points[i]))) > .5, `${state}: visibly evolves after entry`);
    transitions.push({ state, frames: frames.length, maxStep: Math.max(...steps.map(f => f.distance)) });
  }
  for (const state of ['thinking', 'speaking', 'listening', 'muted', 'speaking']) {
    await select.selectOption(state);
    await page.waitForTimeout(120);
  }
  assert.ok(await page.locator('canvas').evaluate(c => c === window.originalPresence), 'rapid interruptions retain the same canvas');
  await page.waitForTimeout(1600);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await select.selectOption('thinking');
  await page.waitForTimeout(100);
  const still = await page.locator('canvas').evaluate(c => c.toDataURL());
  await page.waitForTimeout(200);
  assert.notEqual(await page.locator('canvas').evaluate(c => c.toDataURL()), still, 'explicitly enabled voice animation continues under reduced motion');
  mkdirSync('evidence/presence', { recursive: true });
  writeFileSync('evidence/presence/transitions.json', JSON.stringify(transitions, null, 2));
  console.log(JSON.stringify({ transitions, rapidRetarget: 'passed', reducedMotion: 'passed' }, null, 2));
} finally { await app.close(); }

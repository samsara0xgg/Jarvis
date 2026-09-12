import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
const app = await electron.launch({ ...(process.argv.includes('--packaged')
  ? { executablePath: 'build/Jarvis Motion Preview.app/Contents/MacOS/Electron', args: [] }
  : { args: ['.', '--lab', '--verify'] }), cwd: process.cwd() });
try {
  const page = await app.firstWindow();
  await page.emulateMedia({ reducedMotion: 'no-override' });
  const group = page.locator('.presentation-actual-view .presentation-capsule');
  await group.waitFor();
  const center = group.locator('.presentation-center'), canvas = group.locator('canvas');
  await canvas.evaluate(c => { window.originalCanvas = c; });
  const metrics = () => group.evaluate(el => {
    const r = el.getBoundingClientRect();
    return { width: r.width, center: r.x + r.width / 2, progress: +el.querySelector('canvas').dataset.progress,
      height: el.querySelector('.presentation-core').getBoundingClientRect().height,
      icons: [...el.querySelectorAll('.presentation-edge svg')].map(e => e.getBoundingClientRect().width) };
  });
  const raster = await page.locator('.live-presence').evaluateAll(canvases => canvases.map(c => ({
    backingWidth: c.width, backingHeight: c.height, displayedWidth: c.getBoundingClientRect().width,
    requiredWidth: c.getBoundingClientRect().width * devicePixelRatio,
  })));
  assert.ok(raster.every(c => c.backingWidth >= c.requiredWidth * 1.49), 'both samples are supersampled at their final display size');
  assert.ok(Math.abs(raster[0].backingWidth / raster[1].backingWidth - 2.5) < .01);
  const arm = () => page.evaluate(() => {
    window.morphFrames = [];
    const draw = time => {
      const el = document.querySelector('.presentation-actual-view .presentation-capsule'), r = el.getBoundingClientRect();
      window.morphFrames.push({ time, width: r.width, center: r.x + r.width / 2, progress: +el.querySelector('canvas').dataset.progress, wings: [...el.querySelectorAll('.presentation-wing')].map(w => { const b = w.getBoundingClientRect(); return b.x + b.width / 2 - (r.x + r.width / 2); }) });
      window.morphRecorder = requestAnimationFrame(draw);
    };
    window.morphRecorder = requestAnimationFrame(draw);
  });
  const finish = () => page.evaluate(() => { cancelAnimationFrame(window.morphRecorder); return window.morphFrames; });
  const check = frames => {
    assert.ok(frames.filter(f => f.progress > .05 && f.progress < .95).length >= 10, 'many actual intermediate geometries');
    assert.ok(frames.every(f => Math.abs(f.width - (120 + 150 * f.progress)) < .2), 'shell follows the exact canvas progress');
    assert.ok(frames.every(f => Math.abs(f.wings[0] + 37 + 78 * f.progress) < .1 && Math.abs(f.wings[1] - 37 - 78 * f.progress) < .1), 'message and notification move continuously from inside to outside');
    assert.ok(frames.every(f => Math.abs(f.center - frames[0].center) < .1), 'center never drifts');
    for (let i = 1; i < frames.length; i++) assert.ok(Math.abs(frames[i].width - frames[i-1].width) < (frames[i].time - frames[i-1].time) * .85 + 2, 'no width discontinuity');
  };
  const initial = await metrics(); assert.equal(initial.width, 120); assert.deepEqual(initial.icons, [20, 20]);
  assert.equal(await group.getByRole('button', { name: '麦克风静音', exact: true }).count(), 0);
  assert.equal(await group.getByRole('button', { name: '扬声器静音', exact: true }).count(), 0);
  for (const label of ['发消息', '通知']) {
    await group.getByRole('button', { name: label, exact: true }).click();
    assert.equal(await center.getAttribute('aria-expanded'), 'false');
    assert.equal((await metrics()).width, 120);
    assert.ok((await page.locator('.motion-note').textContent()).startsWith(label + '入口'));
  }
  mkdirSync('evidence/live-morph', { recursive: true });
  await page.screenshot({ path: 'evidence/live-morph/rest.png' });
  await arm(); await center.click();
  await page.waitForTimeout(180); await page.screenshot({ path: 'evidence/live-morph/mid.png' });
  await page.waitForFunction(() => +document.querySelector('.presentation-actual-view canvas').dataset.progress === 1, null, { timeout: 3000 });
  const opening = await finish(); check(opening);
  assert.ok(Math.abs((await metrics()).width - 270) < .1, JSON.stringify({ metrics: await metrics(), last: opening.slice(-3), frameCount: opening.length }));
  await page.screenshot({ path: 'evidence/live-morph/live.png' });
  for (const label of ['麦克风静音', '扬声器静音']) {
    const button = group.getByRole('button', { name: label, exact: true });
    await button.click(); assert.equal(await button.getAttribute('aria-pressed'), 'true');
    assert.equal(await center.getAttribute('aria-expanded'), 'true');
    await button.click();
  }
  for (const label of ['呼吸', '聆听', '思考', '回应', '静音']) {
    await page.getByRole('button', { name: label, exact: true }).click();
    await page.waitForTimeout(160);
    assert.ok(Math.abs((await metrics()).width - 270) < .1);
  }
  await arm(); await center.click(); await page.waitForFunction(() => +document.querySelector('.presentation-actual-view canvas').dataset.progress === 0, null, { timeout: 3000 }); const closing = await finish(); check(closing);
  assert.equal((await metrics()).width, 120);
  for (const label of ['后台处理', '有消息', '暂不可用', '待机']) {
    await page.getByRole('button', { name: label, exact: true }).click();
    await page.waitForTimeout(120); assert.equal((await metrics()).width, 120);
  }
  await arm();
  for (let i = 0; i < 7; i++) { await center.dispatchEvent('click'); await page.waitForTimeout(130); }
  await page.waitForTimeout(1500); const interrupted = await finish(); check(interrupted);
  assert.ok(await canvas.evaluate(c => c === window.originalCanvas));
  const before = await canvas.evaluate(c => c.toDataURL()); await page.waitForTimeout(240);
  assert.notEqual(await canvas.evaluate(c => c.toDataURL()), before, 'actual voice pixels animate under the native motion preference');
  assert.deepEqual((await metrics()).icons, [20, 20]); assert.equal((await metrics()).height, 40);
  await page.getByRole('button', { name: '查看保留的六状态圆环' }).click();
  await page.locator('.orbit-hero canvas').waitFor();
  await page.getByRole('button', { name: '返回胶囊过渡' }).click(); await group.waitFor();
  const report = { systemReducedMotion: await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches),
    openingFrames: opening.length, closingFrames: closing.length, interruptionFrames: interrupted.length,
    raster, correctRestControls: true, continuousControlMovement: true, fixedIcons: true, fixedStateWidths: true, preservedGallery: true };
  writeFileSync('evidence/live-morph/verification.json', JSON.stringify(report, null, 2)); console.log(report);
} finally { await app.close(); }

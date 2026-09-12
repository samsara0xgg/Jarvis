import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';

const app = await electron.launch({ ...(process.argv.includes('--packaged')
  ? { executablePath: 'build/Jarvis Motion Preview.app/Contents/MacOS/Electron', args: [] }
  : { args: ['.', '--lab', '--verify'] }), cwd: process.cwd() });
try {
  const page = await app.firstWindow();
  await page.emulateMedia({ reducedMotion: 'no-override' });
  await page.getByRole('button', { name: '查看保留的六状态圆环' }).click();
  await page.waitForSelector('.orbit-hero canvas');
  const canvas = page.locator('.orbit-hero canvas');
  await canvas.evaluate(c => { window.originalOrbit = c; });
  const report = { systemReducedMotion: await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches), states: [] };
  mkdirSync('evidence/orbit', { recursive: true });
  for (const [state, label] of [['standby', '待机'], ['listening', '聆听'], ['thinking', '思考'], ['speaking', '回应'], ['notification', '有消息'], ['muted', '静音']]) {
    await page.getByRole('button', { name: label, exact: true }).click();
    await page.waitForTimeout(900);
    assert.equal(await canvas.getAttribute('data-state'), state);
    assert.ok(await canvas.evaluate(c => c === window.originalOrbit), 'state switching preserves the canvas');
    const first = await canvas.evaluate(c => c.toDataURL());
    await page.waitForTimeout(300);
    assert.notEqual(await canvas.evaluate(c => c.toDataURL()), first, `${state}: the rendered pixels show motion under the actual OS preference`);
    const actual = await page.locator('.orbit-actual canvas').boundingBox();
    assert.equal(actual.width, 32); assert.equal(actual.height, 32);
    await page.screenshot({ path: `evidence/orbit/${state}.png` });
    report.states.push({ state, animated: true, actualSize: 32 });
  }
  for (const label of ['思考', '回应', '聆听', '有消息', '待机']) {
    await page.getByRole('button', { name: label, exact: true }).click();
    await page.waitForTimeout(90);
  }
  assert.ok(await canvas.evaluate(c => c === window.originalOrbit));
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight), 'preview fits the window');
  report.noNetwork = await page.evaluate(() => performance.getEntriesByType('resource').every(e => !/^https?:/.test(e.name)));
  assert.ok(report.noNetwork);
  await page.waitForTimeout(600);
  await page.screenshot({ path: 'evidence/orbit/overview.png' });
  writeFileSync('evidence/orbit/verification.json', JSON.stringify(report, null, 2));
  console.log(report);
} finally { await app.close(); }

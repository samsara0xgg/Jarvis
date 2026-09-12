import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
mkdirSync('evidence/presence', { recursive: true });
const app = await electron.launch({ args: ['.', '--verify'], cwd: process.cwd() });
const checks = [];
const check = (name, pass) => { assert.ok(pass, name); checks.push(name); console.log(`PASS ${name}`); };
try {
  const page = await app.firstWindow();
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.evaluate(() => localStorage.clear()); await page.reload();
  await page.waitForSelector('.voice-presence');
  const pixels = () => page.locator('canvas').evaluate(c => {
    const { data, width, height } = c.getContext('2d').getImageData(0, 0, c.width, c.height);
    let left = width, right = 0, top = height, bottom = 0, energy = 0;
    for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
      const alpha = data[(y * width + x) * 4 + 3]; energy += alpha;
      if (alpha > 35) { left = Math.min(left, x); right = Math.max(right, x); top = Math.min(top, y); bottom = Math.max(bottom, y); }
    }
    return { width: right - left, height: bottom - top, energy, image: c.toDataURL() };
  });
  const settings = async () => { await page.locator('.voice-pill').click({ button: 'right' }); await page.waitForSelector('.settings'); };
  const pose = async value => { await settings(); await page.getByLabel('声纹状态', { exact: true }).selectOption(value); await page.getByRole('button', { name: '关闭外观设置' }).click(); await page.mouse.move(1, 1); };
  const reports = {};
  for (const state of ['standby', 'listening', 'thinking', 'speaking', 'muted']) {
    await pose(state); await page.waitForTimeout(1200);
    check(`${state} is rendered in the real capsule`, await page.locator('canvas').getAttribute('data-state') === state);
    reports[state] = await pixels();
    await page.locator('.control-row').screenshot({ path: `evidence/presence/${state}.png`, omitBackground: true });
    writeFileSync(`evidence/presence/${state}-wave.png`, Buffer.from(reports[state].image.split(',')[1], 'base64'));
  }
  check('thinking visibly gathers inward', reports.thinking.width < reports.listening.width * .85);
  check('muting settles into a dimmer, flatter trace', reports.muted.height < reports.listening.height && reports.muted.energy < reports.standby.energy);
  await pose('speaking'); await page.waitForTimeout(1100);
  const movingA = await pixels(); await page.waitForTimeout(220); const movingB = await pixels();
  check('response actually animates', movingA.image !== movingB.image);
  await page.emulateMedia({ reducedMotion: 'reduce' }); await page.waitForTimeout(100);
  const quietA = await pixels(); await page.waitForTimeout(250);
  check('reduced motion holds a stable drawing', quietA.image === (await pixels()).image);
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await settings();
  await page.getByLabel('声纹主题色').fill('#a8b5ff');
  check('theme color leaves the glass neutral', await page.locator('main').evaluate(e => e.style.getPropertyValue('--glass-opacity') === '0.4'));
  await page.reload(); await settings();
  check('theme color survives reload', await page.getByLabel('声纹主题色').inputValue() === '#a8b5ff');
  await page.getByRole('button', { name: '恢复默认' }).click();
  await page.getByRole('button', { name: '体验完整动效' }).click();
  await page.mouse.move(1, 1);
  check('full audition closes settings and keeps the small capsule', await page.locator('.settings').count() === 0 && (await page.locator('.voice-pill').boundingBox()).width === 174);
  await page.evaluate(() => {
    window.__presenceStates = ['standby'];
    new MutationObserver(() => window.__presenceStates.push(document.querySelector('canvas').dataset.state))
      .observe(document.querySelector('canvas'), { attributes: true, attributeFilter: ['data-state'] });
  });
  await page.waitForTimeout(20500);
  const sequence = await page.evaluate(() => window.__presenceStates);
  check('audition traverses tension and release then settles', sequence.join(',') === 'standby,listening,thinking,speaking,muted,standby');
  await settings(); check('audition automatically returns to following interactions', await page.getByLabel('声纹状态', { exact: true }).inputValue() === 'auto');
  await page.getByRole('button', { name: '关闭外观设置' }).click();
  await page.getByRole('button', { name: '关闭麦克风（模拟）', exact: true }).click();
  check('microphone mute controls the waveform', await page.locator('canvas').getAttribute('data-state') === 'muted');
  await page.getByRole('button', { name: '开启麦克风（模拟）', exact: true }).click();
  await page.getByRole('button', { name: '打开通知，1 条未读示例' }).click();
  await page.getByRole('button', { name: '标记周末徒步路线已查看' }).click();
  await page.waitForSelector('.empty'); await page.getByRole('button', { name: '收起通知' }).click();
  await page.mouse.move(1, 1); await page.waitForTimeout(250);
  check('empty notifications show the complete bell without a badge', await page.locator('[data-capsule-icon=bell]').count() === 1 && await page.locator('.unread').count() === 0);
  await page.locator('.control-row').screenshot({ path: 'evidence/presence/empty-bell.png', omitBackground: true });
  check('no renderer errors', errors.length === 0);
  writeFileSync('evidence/presence/verification.json', JSON.stringify({ checks, sequence, reports: Object.fromEntries(Object.entries(reports).map(([k, { image, ...v }]) => [k, v])), errors }, null, 2));
} finally { await app.close(); }

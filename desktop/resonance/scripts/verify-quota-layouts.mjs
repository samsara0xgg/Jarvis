import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { fmtReset } from '../src/quota-time.ts';

const dir = 'evidence/quota-layouts';
mkdirSync(dir, { recursive: true });
const now = new Date('2026-09-14T05:00:00Z');
for (const [minutes, expected] of [[222, 'resets in 3h 42m'], [1860, 'resets in 1d 7h'], [59, 'resets in 59m'], [60, 'resets in 1h 0m'], [1440, 'resets in 1d 0h'], [0, 'resetting'], [-1, 'resetting']]) {
  assert.equal(fmtReset(new Date(+now + minutes * 60_000).toISOString(), now), expected);
}
assert.equal(fmtReset(null, now), '—');
assert.equal(fmtReset('invalid', now), '—');
assert.equal(fmtReset(new Date(+now + 1).toISOString(), now), 'resets in 1m');

const app = await electron.launch({ args: ['.', '--verify'] });
const checks = ['countdown: minutes, hours, days, missing, invalid and elapsed timestamps'];
const check = (label, result) => { assert.ok(result, label); checks.push(label); };
try {
  const page = await app.firstWindow();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await app.evaluate(({ ipcMain }) => {
    globalThis.quotaMaterial = [];
    ipcMain.on('material', (_event, payload) => globalThis.quotaMaterial.push(payload));
  });
  const settle = () => page.waitForFunction(() => document.querySelector('.dashboard-stage')?.dataset.motion === 'settled');
  await page.getByRole('button', { name: '外观与窗口选项', exact: true }).click();
  await page.getByLabel('额度页面布局', { exact: true }).selectOption('provider');
  await page.getByRole('button', { name: '打开 Dashboard', exact: true }).click();
  await page.locator('[data-module="2"] .module-summary').click();
  await settle();
  for (const name of ['Claude', 'Codex', 'OpenAI', 'Anthropic', 'DeepSeek', 'MiniMax']) {
    await page.getByRole('tab', { name, exact: true }).click();
    await page.waitForTimeout(180);
    check(`${name}: only selected provider visible`, await page.locator('.quota-content .quota-head').count() === 1);
    check(`${name}: no content clipped`, await page.locator('.quota-content').evaluate(e => e.scrollHeight <= e.clientHeight + 1 && e.scrollWidth <= e.clientWidth));
    await page.locator('.shared-panel').screenshot({ path: `${dir}/${name.toLowerCase()}.png` });
    if (name === 'OpenAI') {
      await page.getByRole('tab', { name: '按 Key', exact: true }).click();
      check('API key breakdown', (await page.locator('.quota-table').textContent()).includes('jarvis'));
    }
    if (name === 'MiniMax') {
      await page.getByRole('button', { name: '估算计算方式', exact: true }).click();
      check('formula collapses', await page.locator('.quota-formula-box').count() === 0);
    }
  }
  await page.getByRole('button', { name: '外观与窗口选项', exact: true }).click();
  await page.getByLabel('额度页面布局', { exact: true }).selectOption('category');
  check('layout updates while dashboard is mounted', await page.locator('.quota-by-provider').count() === 0);
  const opacity = page.getByLabel('玻璃不透明度', { exact: true });
  for (const key of ['Home', 'End']) {
    await opacity.focus(); await opacity.press(key);
    check(`shared material opacity at ${key}`, await page.evaluate(() => {
      const capsule = getComputedStyle(document.querySelector('.presentation-core.glass'));
      const panel = getComputedStyle(document.querySelector('.shared-panel-open'));
      return capsule.backgroundColor === panel.backgroundColor && capsule.getPropertyValue('--glass-opacity') === panel.getPropertyValue('--glass-opacity');
    }));
  }
  await page.getByLabel('声纹主题色', { exact: true }).evaluate(input => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, '#aaa0ff');
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  check('one theme reaches dashboard and quota', await page.evaluate(() => {
    const quota = getComputedStyle(document.querySelector('.quota-detail')).getPropertyValue('--quota-accent').trim();
    const dashboard = getComputedStyle(document.querySelector('.dashboard-preview')).getPropertyValue('--dashboard-accent').trim();
    return quota === '#aaa0ff' && dashboard === quota;
  }));
  const strength = page.getByLabel('毛玻璃强度', { exact: true });
  await strength.focus(); await strength.press('Home');
  await page.waitForTimeout(200);
  check('glass strength reaches native material', await app.evaluate(() => globalThis.quotaMaterial.some(p => p.strength === 0)));
  await page.getByRole('button', { name: '关闭外观设置', exact: true }).click();
  await settle();
  for (const name of ['订阅', '花费', '余额']) {
    await page.getByRole('tab', { name, exact: true }).click();
    check(`${name}: category layout still fits`, await page.locator('.quota-content').evaluate(e => e.scrollHeight <= e.clientHeight + 1 && e.scrollWidth <= e.clientWidth));
  }
  await page.getByRole('tab', { name: '订阅', exact: true }).click();
  await page.locator('.shared-panel').screenshot({ path: `${dir}/category.png` });
  await page.reload();
  await page.getByRole('button', { name: '外观与窗口选项', exact: true }).click();
  check('layout persists after reload', await page.getByLabel('额度页面布局', { exact: true }).inputValue() === 'category');
  check('theme persists after reload', await page.getByLabel('声纹主题色', { exact: true }).inputValue() === '#aaa0ff');
  check('no renderer errors', errors.length === 0);
  writeFileSync(`${dir}/verification.json`, JSON.stringify({ checks, errors }, null, 2));
  console.log(checks);
} finally { await app.close(); }

import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';

const repo = path.resolve('../..'), root = mkdtempSync(path.join(tmpdir(), 'resonance-plugins-'));
const dir = 'evidence/plugins'; mkdirSync(dir, { recursive: true });
const fixture = spawn(path.join(repo, '.venv/bin/python'), ['scripts/plugin-fixture.py', root], {
  env: { ...process.env, PYTHONPATH: repo }, stdio: ['ignore', 'pipe', 'pipe'],
});
let logs = ''; fixture.stderr.on('data', value => logs += value);
const info = await new Promise((resolve, reject) => {
  const lines = createInterface({ input: fixture.stdout });
  const timeout = setTimeout(() => reject(new Error(`fixture timeout: ${logs}`)), 25000);
  lines.on('line', line => { if (line.startsWith('{"port":')) { clearTimeout(timeout); resolve(JSON.parse(line)); } });
  fixture.on('exit', code => { clearTimeout(timeout); reject(new Error(`fixture exited ${code}: ${logs}`)); });
});
let app; const checks = [], errors = [];
const check = (name, pass) => { assert.ok(pass, name); checks.push(name); console.log('PASS', name); };
try {
  app = await electron.launch({ args: ['.', '--verify'], cwd: process.cwd(), env: {
    ...process.env, RESONANCE_PLUGIN_TEST_PORT: String(info.port), RESONANCE_PLUGIN_TEST_ROOT: info.root,
  } });
  const page = await app.firstWindow(); page.on('pageerror', error => errors.push(error.message));
  await page.waitForSelector('.presentation-capsule');
  const click = name => page.getByRole('button', { name, exact: true }).dispatchEvent('click');
  const state = () => page.evaluate(() => window.jarvis.plugins('read'));
  const endpoint = async (name, data = {}) => { const r = await fetch(`http://127.0.0.1:${info.port}/test/${name}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }); assert.ok(r.ok); return r.json(); };
  const menu = () => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.send('command', 'plugins'));
  const settle = () => page.waitForFunction(() => [...document.querySelectorAll('.stack-module')].every(e => e.dataset.motion === 'settled'));
  const shot = async name => { await settle(); await page.screenshot({ path: `${dir}/${name}.png`, omitBackground: true }); };
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].hide());
  await endpoint('request');
  await page.getByRole('button', { name: '连接并继续', exact: true }).waitFor();
  check('conversation opens the connection panel', await page.locator('[data-panel=plugins]').getAttribute('aria-hidden') === 'false');
  check('conversation restores a hidden native window', await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isVisible()));
  check('request opens UI without starting authorization', (await state()).request.state === 'offered');
  await shot('offered');
  await click('连接并继续');
  await page.getByRole('heading', { name: '等待你在浏览器中授权' }).waitFor();
  await shot('authorizing');
  await click('收起'); await settle();
  check('hiding retains authorization and task identity', (await state()).request.state === 'authorizing');
  await menu(); await page.locator('.plugin-row').filter({ hasText: 'Linear' }).click();
  await page.getByRole('button', { name: '取消连接', exact: true }).waitFor(); await click('取消连接');
  check('cancel invalidates continuation', (await state()).request.state === 'cancelled');
  await endpoint('request'); await page.getByRole('button', { name: '连接并继续', exact: true }).waitFor(); await click('连接并继续');
  await page.getByRole('heading', { name: '等待你在浏览器中授权' }).waitFor();
  await click('重新打开授权页面'); await endpoint('authorize');
  await page.getByText('Linear 已连接', { exact: true }).waitFor();
  check('OAuth retry connects and resumes original request once', (await state()).request.resume_status === 'continued');
  await shot('ready');
  await click('管理 Linear');
  await page.getByLabel('插件操作审批').selectOption('prompt');
  check('approval change reaches actual runtime tools', (await state()).plugins.find(p => p.id === 'linear').tools.every(t => t.requires_confirmation));
  await click('停用 Linear');
  check('disable removes active tools', !(await state()).plugins.find(p => p.id === 'linear').tools.length);
  await click('所有插件'); await shot('catalog');
  await page.getByLabel('搜索插件').fill('zz-no-match');
  check('catalog has an explicit no-match state', await page.getByText('没有匹配的插件', { exact: true }).isVisible());
  await click('清空插件搜索'); await page.locator('.plugin-row').filter({ hasText: 'gateway' }).click();
  await page.getByText('此插件依赖尚未接入的连接器网关', { exact: true }).waitFor();
  check('gateway-only packages have no connect action', await page.getByText('此插件依赖尚未接入的连接器网关', { exact: true }).isVisible() && !await page.getByRole('button', { name: '连接', exact: true }).count());
  await click('所有插件'); await page.locator('.plugin-row').filter({ hasText: 'GitHub' }).click();
  await page.getByLabel('GITHUB_TOKEN', { exact: true }).waitFor();
  check('token field masks credentials', await page.getByLabel('GITHUB_TOKEN', { exact: true }).getAttribute('type') === 'password');
  await shot('token'); await click('连接');
  await page.getByRole('button', { name: '重新连接', exact: true }).waitFor(); await shot('error');
  await page.getByLabel('GITHUB_TOKEN', { exact: true }).fill('static-secret'); await click('重新连接');
  await page.getByText('GitHub 已连接', { exact: true }).waitFor();
  check('token connects through real MCP without appearing in snapshot', !JSON.stringify(await state()).includes('static-secret'));
  await page.emulateMedia({ reducedMotion: 'reduce' }); await click('所有插件'); await settle();
  check('reduced-motion catalog renders without horizontal overflow', await page.locator('.plugin-panel').evaluate(e => e.scrollWidth <= e.clientWidth + 1));
  const bounds = await app.evaluate(({ BrowserWindow, screen }) => { const b = BrowserWindow.getAllWindows()[0].getBounds(); return { b, a: screen.getDisplayMatching(b).workArea }; });
  check('native window stays in display work area', bounds.b.y >= bounds.a.y && bounds.b.y + bounds.b.height <= bounds.a.y + bounds.a.height);
  check('no renderer exceptions', errors.length === 0);
  writeFileSync(`${dir}/verification.json`, JSON.stringify({ checks, errors, bounds, fixture: 'isolated real production service + MCP + OAuth; no personal accounts' }, null, 2));
  console.log(`${checks.length} plugin UI checks passed`);
} finally {
  if (app) await app.close();
  fixture.kill('SIGTERM');
  await new Promise(resolve => fixture.once('exit', resolve));
  writeFileSync(`${dir}/fixture.log`, logs);
}

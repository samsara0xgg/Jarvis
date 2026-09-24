import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';

const dir = 'evidence/dashboard-grid';
mkdirSync(dir, { recursive: true });
const app = await electron.launch({ args: ['.', '--verify'] });
const checks = [], errors = [];
const check = (name, value) => { assert.ok(value, name); checks.push(name); console.log(name); };
let previousPreferences;
try {
  const page = await app.firstWindow();
  page.on('pageerror', error => errors.push(error.message));
  previousPreferences = await page.evaluate(() => {
    const previous = localStorage.getItem('resonance-appearance-v1');
    const preferences = JSON.parse(previous ?? '{}');
    delete preferences.dashboardStyle;
    localStorage.setItem('resonance-appearance-v1', JSON.stringify(preferences));
    return previous;
  });
  await page.reload();
  await page.getByRole('button', { name: 'Dashboard', exact: true }).click();
  const settle = async () => {
    await page.waitForFunction(() => document.querySelector('.dashboard-stage')?.dataset.motion === 'settled'
      && document.querySelector('[data-panel="dashboard"]')?.dataset.motion === 'settled');
  };
  const composeSettled = async () => page.waitForFunction(() => {
    const node = document.querySelector('.dashboard-composer-dock');
    return node?.dataset.motion === 'settled' && Number(getComputedStyle(node).getPropertyValue('--compose-progress')) === Number(node.dataset.expanded === 'true');
  });
  const geometry = () => page.locator('.dashboard-board > .dashboard-module').evaluateAll(nodes => nodes.map(node => {
    const r = node.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height };
  }));
  const dock = page.locator('.dashboard-composer-dock');
  const hit = page.locator('.dashboard-composer-hit');
  const field = page.getByRole('textbox', { name: '给 Jarvis 发消息' });
  await settle();
  const style = page.locator('[data-dashboard-style]');
  check('existing appearance preferences adopt the unified dashboard by default', await style.getAttribute('data-dashboard-style') === 'unified');
  const unifiedGeometry = await geometry();
  const visual = () => page.locator('[data-module="0"]').evaluate(node => {
    const css = getComputedStyle(node); return { background: css.backgroundImage, border: css.borderColor, radius: css.borderRadius };
  });
  check('unified overview uses the shared surface instead of gradient cards', (await visual()).background === 'none' && (await visual()).border === 'rgba(0, 0, 0, 0)');
  check('every tile inherits the same glass without its own opaque background', await page.locator('.dashboard-module').evaluateAll(nodes => nodes.every(node => getComputedStyle(node).backgroundImage === 'none' && getComputedStyle(node).backgroundColor === 'rgba(0, 0, 0, 0)')));
  const selectStyle = async value => {
    await page.getByRole('button', { name: '外观与窗口选项', exact: true }).click();
    await page.getByRole('combobox', { name: 'Dashboard 风格', exact: true }).selectOption(value);
    await page.getByRole('button', { name: '关闭外观设置', exact: true }).click();
    await settle();
  };
  await selectStyle('cards');
  const backupVisual = await visual();
  check('settings restores the original gradient, border and corner treatment', await style.getAttribute('data-dashboard-style') === 'cards' && backupVisual.background.startsWith('linear-gradient') && backupVisual.radius === '14px' && backupVisual.border === 'rgba(255, 255, 255, 0.125)');
  check('switching to the backup preserves the fixed layout', JSON.stringify(await geometry()) === JSON.stringify(unifiedGeometry));
  await page.locator('.shell').screenshot({ path: `${dir}/original-cards.png` });
  await page.reload();
  await page.getByRole('button', { name: 'Dashboard', exact: true }).click(); await settle();
  check('the backup choice survives a renderer reload', await style.getAttribute('data-dashboard-style') === 'cards');
  await selectStyle('unified');
  await page.reload();
  await page.getByRole('button', { name: 'Dashboard', exact: true }).click(); await settle();
  check('the unified choice also survives a renderer reload', await style.getAttribute('data-dashboard-style') === 'unified');
  await app.evaluate(({ ipcMain }) => {
    globalThis.gridMaterial = [];
    ipcMain.on('material', (_event, payload) => { globalThis.gridMaterial.push(payload); if (globalThis.gridMaterial.length > 50) globalThis.gridMaterial.shift(); });
  });
  await page.getByRole('button', { name: '外观与窗口选项', exact: true }).click();
  const opacitySlider = page.getByRole('slider', { name: '玻璃不透明度', exact: true });
  const strengthSlider = page.getByRole('slider', { name: '毛玻璃强度', exact: true });
  const initialOpacity = await opacitySlider.inputValue(), initialStrength = await strengthSlider.inputValue();
  for (const key of ['Home', 'End']) {
    await opacitySlider.focus(); await opacitySlider.press(key);
    check(`configured opacity stays identical on capsule and dashboard at ${key}`, await page.evaluate(() => {
      const capsule = getComputedStyle(document.querySelector('.presentation-core.glass'));
      const panel = getComputedStyle(document.querySelector('.panel-stack'));
      const dashboard = getComputedStyle(document.querySelector('.dashboard-surface'));
      return capsule.backgroundColor === panel.backgroundColor && dashboard.backgroundColor === 'rgba(0, 0, 0, 0)';
    }));
  }
  await strengthSlider.focus(); await strengthSlider.press('Home');
  check('configured glass strength reaches the existing native material', await app.evaluate(() => globalThis.gridMaterial.some(payload => payload.strength === 0)));
  for (const [slider, value] of [[opacitySlider, initialOpacity], [strengthSlider, initialStrength]]) {
    await slider.evaluate((input, next) => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, next);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }, value);
  }
  await page.getByRole('button', { name: '关闭外观设置', exact: true }).click(); await settle();
  const before = await geometry(), [chat, codex, quota, work] = before;
  const viewport = await page.locator('.dashboard-viewport').boundingBox();
  const nativeBefore = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getBounds());
  check('conversation and work-state tiles are fixed 134px squares', chat.w === 134 && chat.h === 134 && work.w === 134 && work.h === 134);
  check('quota occupies the right two rows; Codex occupies the full bottom row', quota.w === 134 && quota.h === 276 && quota.x === chat.x + 142 && quota.y === chat.y && work.y === chat.y + 142 && codex.w === 276 && codex.h === 134 && codex.x === chat.x && codex.y === chat.y + 284);
  check('default viewport fits exactly three square rows and two gaps', viewport.height === 418 && codex.y + codex.h === viewport.y + viewport.height);
  check('all four demo quota limits are visible with usage labels', await page.locator('.overview-usage-grid .overview-meter').count() === 4 && await page.locator('.overview-usage-grid').evaluate(node => node.scrollHeight <= node.closest('.module-summary').clientHeight - 20 && node.textContent.includes('已用')));
  await page.locator('.shell').screenshot({ path: `${dir}/overview.png` });

  await hit.hover(); await composeSettled();
  check('hover expands the bar without pinning', await dock.getAttribute('data-expanded') === 'true' && await dock.getAttribute('data-pinned') === 'false');
  check('expanded input also uses the parent glass without a hardcoded fill', await page.locator('.dashboard-composer-surface').evaluate(node => getComputedStyle(node).backgroundColor.endsWith('/ 0)') || getComputedStyle(node).backgroundColor === 'rgba(0, 0, 0, 0)'));
  const inputBox = await page.locator('.dashboard-composer-surface').boundingBox();
  const surfaceBox = await page.locator('.dashboard-surface').boundingBox();
  check('floating input stays entirely inside the dashboard', inputBox.x >= surfaceBox.x && inputBox.y >= surfaceBox.y && inputBox.x + inputBox.width <= surfaceBox.x + surfaceBox.width && inputBox.y + inputBox.height < surfaceBox.y + surfaceBox.height);
  check('input expansion does not move cards or resize the native window', JSON.stringify(await geometry()) === JSON.stringify(before) && JSON.stringify(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getBounds())) === JSON.stringify(nativeBefore));
  for (const target of [field, page.getByRole('button', { name: '固定输入框', exact: true }), page.getByRole('button', { name: '发送消息', exact: true })]) {
    await target.hover(); check(`hover remains open over ${await target.getAttribute('aria-label')}`, await dock.getAttribute('data-expanded') === 'true');
  }
  await page.locator('.shell').screenshot({ path: `${dir}/hover-input.png` });
  await page.mouse.move(3, 3); await composeSettled();
  check('leaving the entire input region collapses unpinned input', await dock.getAttribute('data-expanded') === 'false');

  await hit.hover(); await composeSettled();
  await page.getByRole('button', { name: '固定输入框', exact: true }).click();
  await field.fill('保留这份输入草稿');
  await page.mouse.move(3, 3); await composeSettled();
  check('click pins input and it stays open after leaving', await dock.getAttribute('data-pinned') === 'true' && await field.isVisible());
  await page.getByRole('button', { name: '取消固定输入框', exact: true }).click();
  check('second click unpins but stays open while still hovered', await dock.getAttribute('data-pinned') === 'false' && await dock.getAttribute('data-expanded') === 'true');
  await page.mouse.move(3, 3); await composeSettled();
  await hit.hover(); await composeSettled();
  check('draft survives unpin, collapse and hover reopening', await field.inputValue() === '保留这份输入草稿');
  await field.evaluate(input => input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, isComposing: true })));
  check('IME confirmation does not submit', await field.inputValue() === '保留这份输入草稿');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.dashboard-floating-form input')?.value === '');
  await page.waitForFunction(() => document.querySelector('[data-module="0"] .module-caption')?.textContent === '最近回复');
  check('dashboard composer uses the existing simulated send and reply path', await page.locator('.dashboard-stage').getAttribute('data-selected') === 'overview');

  await page.mouse.move(3, 3); await composeSettled();
  const closed = await hit.boundingBox();
  await page.mouse.move(closed.x + closed.width / 2, closed.y + closed.height / 2);
  await page.waitForTimeout(70);
  const mid = await page.locator('.dashboard-composer-surface').boundingBox();
  check('bar-to-input animation has intermediate geometric frames', mid.width > 36 && mid.width < 276);
  await page.mouse.move(3, 3);
  await page.waitForTimeout(45);
  await page.mouse.move(closed.x + closed.width / 2, closed.y + closed.height / 2);
  await composeSettled();
  check('rapid hover reversal settles open with unchanged grid', await dock.getAttribute('data-expanded') === 'true' && JSON.stringify(await geometry()) === JSON.stringify(before));
  await field.click(); await field.fill('切换面板后保留'); await page.keyboard.press('Escape'); await composeSettled();
  check('Escape collapses only the input and clears its pin', await dock.getAttribute('data-expanded') === 'false' && await dock.getAttribute('data-pinned') === 'false' && await page.locator('.dashboard-surface').isVisible());

  await page.locator('[data-module="2"] .module-summary').click(); await settle();
  check('existing quota detail still opens', await page.locator('.dashboard-stage').getAttribute('data-selected') === '2');
  const bar = await page.getByRole('button', { name: '返回主界面', exact: true }).boundingBox();
  await page.mouse.move(bar.x + bar.width / 2, bar.y + bar.height / 2); await page.mouse.down(); await page.mouse.move(bar.x + bar.width / 2, bar.y + 65); await page.mouse.up();
  check('dragging down does not navigate from detail', await page.locator('.dashboard-stage').getAttribute('data-selected') === '2');
  await page.getByRole('button', { name: '返回主界面', exact: true }).click(); await settle();
  check('clicking the existing bar returns to the same square grid', JSON.stringify(await geometry()) === JSON.stringify(before));
  await page.getByRole('button', { name: '打开插件列表', exact: true }).scrollIntoViewIfNeeded();
  check('additional modules scroll inside the fixed viewport', await page.locator('.dashboard-viewport').evaluate(node => node.scrollTop > 0 && node.clientHeight === 418));
  check('scrolling overflow does not grow the native window', JSON.stringify(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getBounds())) === JSON.stringify(nativeBefore));
  await page.locator('.dashboard-viewport').evaluate(node => { node.scrollTop = 0; });

  await page.getByRole('button', { name: '发消息', exact: true }).click();
  check('floating composer shares the existing text input draft', await page.getByRole('textbox', { name: '文字输入', exact: true }).inputValue() === '切换面板后保留');
  await page.getByRole('button', { name: '关闭文字输入', exact: true }).click(); await settle();
  await page.mouse.move(3, 3); await composeSettled();
  await page.getByRole('button', { name: '固定文字输入框', exact: true }).focus(); await page.keyboard.press('Enter'); await composeSettled();
  check('keyboard activation pins and focuses the input', await dock.getAttribute('data-pinned') === 'true' && await field.evaluate(node => node === document.activeElement));
  await page.keyboard.press('Escape'); await composeSettled();
  await page.emulateMedia({ reducedMotion: 'reduce' }); await hit.hover(); await composeSettled();
  check('reduced-motion mode retains the hover interaction', await dock.getAttribute('data-expanded') === 'true');
  await page.mouse.move(3, 3); await composeSettled();
  check('no renderer exceptions', errors.length === 0);
  writeFileSync(`${dir}/verification.json`, JSON.stringify({ checks, errors, geometry: before, viewport, nativeWindow: nativeBefore }, null, 2));
} finally {
  if (previousPreferences !== undefined) {
    await (await app.firstWindow()).evaluate(previous => {
      if (previous === null) localStorage.removeItem('resonance-appearance-v1');
      else localStorage.setItem('resonance-appearance-v1', previous);
    }, previousPreferences);
  }
  await app.close();
}

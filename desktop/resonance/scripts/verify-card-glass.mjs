import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';

const dir = 'evidence/card-glass';
mkdirSync(dir, { recursive: true });
const checks = [];
const check = (label, pass) => { assert.ok(pass, label); checks.push(label); console.log(`PASS ${label}`); };
const app = await electron.launch({ args: ['.', '--verify'], cwd: process.cwd() });
try {
  const page = await app.firstWindow();
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await app.evaluate(({ ipcMain }) => {
    ipcMain.on('material', (_event, payload) => { globalThis.__cardMaterial = payload; });
  });
  await page.evaluate(() => localStorage.clear()); await page.reload();
  await page.waitForSelector('canvas');
  await page.getByRole('button', { name: '打开通知，2 条未读示例' }).click();
  await page.waitForTimeout(300);
  check('two demo cards are present by default', await page.locator('.result').count() === 2);
  check('opening notifications shows a collapsed stack', await page.locator('.inbox.is-stacked').count() === 1);
  await page.getByRole('button', { name: '展开 2 条通知' }).hover(); await page.waitForTimeout(200);
  check('hover does not expand', await page.locator('.inbox.is-stacked').count() === 1);
  await page.screenshot({ path: `${dir}/stack.png`, omitBackground: true });
  await page.getByRole('button', { name: '展开 2 条通知' }).click();
  await page.waitForTimeout(300);
  const cards = page.locator('.result');
  const states = () => cards.evaluateAll(es => es.map(e => {
    const s = selector => getComputedStyle(e.querySelector(selector));
    return { reply: s('.result-actions').opacity, dismiss: s('.result-dismiss').opacity,
      replyHit: s('.result-actions button').pointerEvents, dismissHit: s('.result-dismiss').pointerEvents,
      complete: s('.result-status').visibility };
  }));
  const expectHover = async (index, label) => {
    await page.waitForTimeout(180);
    const values = await states();
    check(label, values.every((s, i) => s.reply === (i === index ? '1' : '0') && s.dismiss === s.reply));
    check(`${label}: hidden actions do not intercept pointers`, values.every((s, i) => i === index || (s.replyHit === 'none' && s.dismissHit === 'none')));
    check(`${label}: completion stays visible`, values.every(s => s.complete === 'visible'));
    return values;
  };
  await page.mouse.move(1, 1);
  await expectHover(-1, 'leaving after click hides actions on both cards');
  await page.screenshot({ path: `${dir}/none.png`, omitBackground: true });
  await cards.nth(0).hover(); await expectHover(0, 'hover A shows only A');
  await page.screenshot({ path: `${dir}/hover-a.png`, omitBackground: true });
  await cards.nth(1).hover(); await expectHover(1, 'hover B hides A and shows B');
  await page.screenshot({ path: `${dir}/hover-b.png`, omitBackground: true });
  await page.mouse.move(1, 1); await expectHover(-1, 'leaving B hides both');

  const materials = await page.locator('.voice-pill, .result').evaluateAll(es => es.map(e => {
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return { background: s.backgroundColor, border: s.borderColor, shadow: s.boxShadow, opacity: s.opacity, x: r.x, y: r.y, width: r.width, height: r.height };
  }));
  check('cards share capsule neutral background, border and shadow', materials.every(m => m.background === materials[0].background && m.border === materials[0].border && m.shadow === materials[0].shadow));
  const native = await app.evaluate(() => globalThis.__cardMaterial);
  check('all three surfaces reach the native material layer at identical opacity', materials.every(m => native.rects.some(r => Math.abs(r.x - m.x) < .1 && Math.abs(r.y - m.y) < .1 && r.opacity === 1)) && native.strength === 1);
  for (const alpha of [.08, .65]) {
    await page.locator('.shell').evaluate((e, alpha) => e.style.setProperty('--glass-opacity', alpha), String(alpha));
    check(`shared opacity follows ${alpha}`, await page.locator('.voice-pill, .result').evaluateAll(es => es.every(e => getComputedStyle(e).backgroundColor === getComputedStyle(es[0]).backgroundColor)));
  }
  await page.locator('.shell').evaluate(e => e.style.setProperty('--glass-opacity', '.4'));

  await cards.nth(0).hover();
  await page.getByRole('button', { name: '继续讨论周末徒步路线' }).click();
  const input = page.getByRole('textbox', { name: '回复周末徒步路线的内容' });
  await input.fill('保留这段正在输入的回复');
  await cards.nth(1).hover(); await expectHover(1, 'editing A does not pin its actions when hovering B');
  check('inline draft and input focus survive leaving A', await input.inputValue() === '保留这段正在输入的回复' && await input.evaluate(e => e === document.activeElement));
  await input.press('Escape'); await page.mouse.move(1, 1);
  await app.evaluate(({ BrowserWindow }) => { const w = BrowserWindow.getAllWindows()[0]; w.setFocusable(true); w.focus(); w.webContents.focus(); });
  await cards.nth(0).locator('.result-main').focus();
  await page.keyboard.press('Tab');
  await expectHover(0, 'keyboard navigation exposes focused card controls');
  check('keyboard focus has a visible outline', await page.locator(':focus-visible').evaluate(e => getComputedStyle(e).outlineStyle !== 'none'));
  await cards.nth(1).hover();
  // A pointer press changes input modality without opening another card.
  await page.mouse.click(1, 1); await cards.nth(1).hover();
  await page.waitForTimeout(180);
  const x = await cards.nth(1).locator('.result-dismiss').boundingBox();
  const card = await cards.nth(1).boundingBox();
  const point = { x: x.x + x.width / 2, y: card.y - 1 };
  await page.mouse.move(point.x, point.y, { steps: 8 });
  await page.waitForTimeout(180);
  check('protruding X remains visible and hit-testable outside the card', await page.evaluate(p => Boolean(document.elementFromPoint(p.x, p.y)?.closest('.result-dismiss')), point) && (await states())[1].dismiss === '1');
  await page.mouse.click(point.x, point.y); await page.waitForTimeout(250);
  check('clicking protruding X removes only its own card', await cards.count() === 1 && (await cards.first().innerText()).includes('周末徒步路线'));
  check('no renderer errors', errors.length === 0);
  writeFileSync(`${dir}/verification.json`, JSON.stringify({ checks, materials, native, errors }, null, 2));
} finally { await app.close(); }

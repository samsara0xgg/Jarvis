import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';

const dir = 'evidence/codex-board';
mkdirSync(dir, { recursive: true });
const app = await electron.launch({ args: ['.', '--verify'] });
const checks = [];
const check = (label, value) => { assert.ok(value, label); checks.push(label); console.log(label); };
try {
  const page = await app.firstWindow();
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.evaluate(() => localStorage.removeItem('resonance-codex-board-v1-demo'));
  await page.reload();
  const open = async () => {
    await page.getByRole('button', { name: 'Dashboard', exact: true }).click();
    await page.locator('[data-module="1"] .module-summary').click();
    await page.waitForFunction(() => document.querySelector('.dashboard-stage')?.dataset.motion === 'settled');
    await page.mouse.move(0, 0);
  };
  await open();
  const list = page.locator('.codex-sessions');
  const row = id => page.locator(`[data-session="${id}"]`);
  await page.waitForTimeout(350);
  check('all working, waiting and unread-completed tasks stay expanded without hover', await page.locator('.codex-row.is-expanded').count() === 8);
  check('idle task stays compact', !(await row('demo-7').getAttribute('class')).includes('is-expanded'));
  const textWidth = await row('demo-1').locator('.codex-title').evaluate(e => e.getBoundingClientRect().width);
  check('automatically expanded card hides controls until hover', await row('demo-1').evaluate(e => getComputedStyle(e.querySelector('.codex-actions')).opacity === '0' && getComputedStyle(e.querySelector('.codex-archive')).opacity === '0' && getComputedStyle(e.querySelector('.codex-actions')).visibility === 'hidden'));
  await row('demo-1').locator('.codex-open').hover(); await page.waitForTimeout(350);
  check('hover overlays controls without reserving text space', await row('demo-1').evaluate((e, before) => {
    return Math.abs(e.querySelector('.codex-title').getBoundingClientRect().width - before) < 1
      && getComputedStyle(e.querySelector('.codex-actions')).opacity === '1'
      && getComputedStyle(e.querySelector('.codex-archive')).opacity === '1'
      && getComputedStyle(e.querySelector('.codex-open')).getPropertyValue('--codex-action-fade').trim() === '70px';
  }, textWidth));
  await page.mouse.move(0, 0); await page.waitForTimeout(350);
  check('leaving restores full text but keeps active card expanded', await row('demo-1').evaluate(e => e.classList.contains('is-expanded') && getComputedStyle(e.querySelector('.codex-actions')).opacity === '0' && getComputedStyle(e.querySelector('.codex-open')).getPropertyValue('--codex-action-fade').trim() === '0px'));
  await page.evaluate(() => {
    const key = 'resonance-codex-board-v1-demo', saved = JSON.parse(localStorage.getItem(key));
    saved.acknowledged = Object.fromEntries(saved.rows.map(r => [r.session_id, `${r.turn_started_ms ?? r.prompt}:${r.state}${r.state === 'needs_input' ? `:${r.since_ms}` : ''}`]));
    localStorage.setItem(key, JSON.stringify(saved));
  });
  await page.reload(); await open(); await page.waitForTimeout(350);
  check('acknowledged activities stay collapsed across reload', await page.locator('.codex-row.is-expanded').count() === 0);
  const initialCount = await page.locator('.codex-row').count();
  check('fixed viewport with denser rows and overflow scrolling', await list.evaluate(e => e.clientHeight >= 240 && e.clientHeight <= 260 && e.scrollHeight > e.clientHeight));
  check('compact rows are 32px without separators', await page.locator('.codex-row').first().evaluate(e => e.getBoundingClientRect().height === 32 && getComputedStyle(e).borderBottomWidth === '0px'));
  check('no provider icons or instruction header/footer', await page.locator('.codex-open svg').count() === 0 && await page.getByText('最近 24 小时', { exact: true }).count() === 0);
  const height = (await list.boundingBox()).height;
  await list.screenshot({ path: `${dir}/compact.png` });
  await row('demo-2').locator('.codex-open').hover();
  await page.waitForTimeout(350);
  check('hover expands the existing row without enlarging viewport', (await row('demo-2').boundingBox()).height >= 60 && Math.abs((await list.boundingBox()).height - height) < 1);
  check('archive is upper-left, visually 14px with 24px hit area', await row('demo-2').evaluate(e => {
    const r = e.getBoundingClientRect(), x = e.querySelector('.codex-archive').getBoundingClientRect();
    return x.left <= r.left && x.top < r.top && x.width === 24 && getComputedStyle(e.querySelector('.codex-archive'), '::before').width === '14px';
  }));
  await list.screenshot({ path: `${dir}/hover.png` });
  // A locator screenshot can scroll the list away from the desktop pointer.
  await row('demo-2').locator('.codex-open').hover();
  await row('demo-2').getByRole('button', { name: '回复 Fix voice reconnect', exact: true }).click();
  const reply = page.getByRole('textbox', { name: '回复 Fix voice reconnect 的内容' });
  await reply.fill('先检查断网后恢复');
  await page.mouse.move(0, 0); await page.waitForTimeout(300);
  check('reply opens inline and remains expanded when pointer leaves', await reply.isVisible() && await row('demo-2').getAttribute('class').then(c => c.includes('is-replying')));
  await list.screenshot({ path: `${dir}/reply.png` });
  await reply.press('Enter');
  check('unsupported send never reports success', (await page.locator('.codex-toast').innerText()).includes('尚未接通'));
  await reply.press('Escape');
  check('Escape closes only the reply field', await reply.count() === 0 && await list.isVisible());
  await row('demo-2').getByRole('button', { name: '回复 Fix voice reconnect', exact: true }).click();
  check('reply draft survives collapse', await reply.inputValue() === '先检查断网后恢复');
  await reply.press('Escape');
  await row('demo-2').locator('.codex-open').click({ delay: 1100 });
  check('one-second hold pins without opening session', await row('demo-2').getAttribute('class').then(c => c.includes('is-pinned')) && !(await page.locator('.codex-toast').innerText()).includes('演示会话'));
  check('pinned row sorts first', await page.locator('.codex-row').first().getAttribute('data-session') === 'demo-2');
  await page.reload(); await open();
  check('pin survives reload', await row('demo-2').getAttribute('class').then(c => c.includes('is-pinned')));
  await row('demo-2').locator('.codex-open').hover(); await page.waitForTimeout(300);
  await row('demo-2').locator('.codex-archive').click();
  check('archive removes only target row', await row('demo-2').count() === 0 && await page.locator('.codex-row').count() === initialCount - 1);
  await page.getByRole('button', { name: '撤销', exact: true }).click();
  check('undo restores pin and row', await row('demo-2').getAttribute('class').then(c => c.includes('is-pinned')));
  await row('demo-2').locator('.codex-open').focus(); await page.keyboard.press('p');
  check('keyboard can unpin', !(await row('demo-2').getAttribute('class')).includes('is-pinned'));
  await row('demo-2').locator('.codex-open').click();
  check('demo navigation cannot launch real Codex', (await page.locator('.codex-toast').innerText()).includes('演示会话'));
  check('stop honestly exposes unavailable capability', await row('demo-2').getByRole('button', { name: /打断/ }).getAttribute('aria-disabled') === 'true');
  const box = await row('demo-2').locator('.codex-open').boundingBox();
  await page.mouse.move(box.x + 30, box.y + 20); await page.mouse.down();
  await page.mouse.move(box.x + 45, box.y + 20); await page.waitForTimeout(1100); await page.mouse.up();
  check('movement cancels pin', !(await row('demo-2').getAttribute('class')).includes('is-pinned'));
  await page.evaluate(() => {
    const key = 'resonance-codex-board-v1-demo', saved = JSON.parse(localStorage.getItem(key));
    saved.rows.find(r => r.session_id === 'demo-3').since_ms = Date.now() - 86400001;
    saved.rows.find(r => r.session_id === 'demo-4').since_ms = Date.now() - 86400001;
    saved.pins.push('demo-4');
    localStorage.setItem(key, JSON.stringify(saved));
  });
  await page.reload(); await open();
  check('24-hour expiry excludes old unpinned rows but keeps pins', await row('demo-3').count() === 0 && await row('demo-4').count() === 1);
  check('invalid deep links are rejected', await page.evaluate(() => window.jarvis.openCodex('file:///tmp')) === false);
  await page.evaluate(() => {
    const key = 'resonance-codex-board-v1-demo', saved = JSON.parse(localStorage.getItem(key));
    saved.rows.find(r => r.session_id === 'demo-1').detail = '新的普通进度';
    const waiting = saved.rows.find(r => r.session_id === 'demo-2');
    waiting.state = 'needs_input'; waiting.since_ms = Date.now();
    const next = saved.rows.find(r => r.session_id === 'demo-8');
    next.state = 'running'; next.turn_started_ms = Date.now();
    localStorage.setItem(key, JSON.stringify(saved));
  });
  await page.reload(); await open(); await page.waitForTimeout(350);
  check('ordinary progress does not reopen an acknowledged task', !(await row('demo-1').getAttribute('class')).includes('is-expanded'));
  check('new attention and new turns automatically expand', (await row('demo-2').getAttribute('class')).includes('is-expanded') && (await row('demo-8').getAttribute('class')).includes('is-expanded'));
  await row('demo-2').locator('.codex-open').hover();
  await row('demo-2').getByRole('button', { name: '回复 Fix voice reconnect', exact: true }).click();
  await page.getByRole('textbox', { name: '回复 Fix voice reconnect 的内容' }).press('Escape');
  await page.mouse.move(0, 0); await page.waitForTimeout(350);
  check('task collapses after reply interaction ends', !(await row('demo-2').getAttribute('class')).includes('is-expanded'));
  check('no renderer errors', errors.length === 0);
  writeFileSync(`${dir}/verification.json`, JSON.stringify({ checks, errors }, null, 2));
} finally { await app.close(); }

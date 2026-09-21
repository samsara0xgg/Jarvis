// Run against an already-started local Vite preview; all daemon routes are mocked.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
const url = process.env.WORK_STATE_PREVIEW_URL ?? 'http://127.0.0.1:5178';
const dir = process.env.WORK_STATE_EVIDENCE_DIR ?? '/tmp/jarvis-work-state-ui';
mkdirSync(dir, { recursive: true });
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
const checks = [], check = (name, pass) => { assert.ok(pass, name); checks.push(name); };
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.clock.install();
  const at = new Date().toISOString();
  const first = {
    state: { version: 1, analyzed_at: at, observed_until: at, evidence: { coverage: {}, counts: {}, limits: [] },
      now: { text: '核对工作状态数据', basis: 'observed', refs: ['capture:fixture'], as_of: at },
      activities: [{ text: '接通 TimeSink 的活动信息', basis: 'observed', refs: ['capture:fixture'] }],
      links: [], uncertainties: ['窗口活动不能证明待办已完成。'], note: null },
    data: { status: 'ok', capture_latest_seen: at, span_latest_end: at, observed_at_ms: Date.now() },
    freshness: { checked_at_ms: Date.now(), latest_observed_at: at, analyzed_at: at, analysis_observed_until: at },
    refreshing: false, outcome: 'analyzed', error: null,
  };
  const second = structuredClone(first); second.state.version = 2; second.state.now.text = '已经读取最新活动';
  let posts = 0, gets = 0, release;
  const barrier = new Promise(resolve => { release = resolve; });
  await page.route('http://127.0.0.1:19099/**', async route => {
    if (!route.request().url().includes('/inherent/work-state')) return route.abort();
    if (route.request().method() === 'POST') {
      posts++;
      if (posts === 1) await barrier;
      const value = posts === 1 ? second : { ...second, outcome: 'failed', error: '验收模拟模型不可用' };
      return route.fulfill({ json: value });
    }
    gets++;
    return route.fulfill({ json: first }); // deliberately older, even after the POST.
  });
  await page.goto(`${url}/?port=19099`);
  await page.getByRole('button', { name: 'Dashboard', exact: true }).click();
  await page.getByRole('button', { name: '展开当前状态', exact: true }).click();
  const panel = page.locator('.work-detail');
  await panel.getByText('核对工作状态数据', { exact: false }).waitFor();
  check('opening dashboard only reads', posts === 0 && gets > 0);
  const button = panel.getByRole('button', { name: '刷新', exact: true });
  await button.click();
  await panel.getByText('更新中，保留上次结果').waitFor();
  check('refresh keeps previous state visible and prevents repeat clicks', await button.isDisabled() && (await panel.textContent()).includes('核对工作状态数据'));
  release();
  await panel.getByText('已经读取最新活动', { exact: false }).waitFor();
  check('one manual refresh sends one request', posts === 1);
  const readsBefore = gets;
  await page.clock.fastForward(60_000);
  await page.waitForFunction(() => document.querySelector('.work-detail')?.textContent.includes('已经读取最新活动'));
  check('stale GET cannot replace a newer state', gets > readsBefore && (await panel.textContent()).includes('已经读取最新活动'));
  await button.click();
  await panel.getByText('未更新：验收模拟模型不可用').waitFor();
  check('failure preserves the last successful state', (await panel.textContent()).includes('已经读取最新活动'));
  check('three separate freshness clocks shown', (await page.locator('[data-module="3"]').textContent()).includes('最新观察'));
  check('no React runtime errors', errors.length === 0);
  await page.screenshot({ path: `${dir}/current-state.png`, fullPage: true });
  writeFileSync(`${dir}/checks.json`, JSON.stringify({ checks, posts, gets, errors }, null, 2));
  console.log(`${checks.length} UI checks passed; evidence: ${dir}`);
} finally { await browser.close(); }

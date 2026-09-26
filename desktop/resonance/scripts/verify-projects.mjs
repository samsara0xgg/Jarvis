// ADR 0037 项目 module. Run against an already-started local Vite preview; all daemon routes are mocked.
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
const url = process.env.PROJECTS_PREVIEW_URL ?? 'http://127.0.0.1:5178';
const dir = process.env.PROJECTS_EVIDENCE_DIR ?? '/tmp/jarvis-projects-ui';
mkdirSync(dir, { recursive: true });
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
const checks = [], check = (name, pass) => { assert.ok(pass, name); checks.push(name); };
const days = Array.from({ length: 7 }, (_, i) => new Date(Date.now() - (6 - i) * 86_400_000).toISOString().slice(0, 10));
const at = new Date().toISOString();
const row = (id, name, seconds, extra = {}) => ({ id, name, seconds, today_seconds: seconds ? 1_800 : 0, days: seconds ? [0, 600, 0, 1_200, 0, seconds - 3_600, 1_800] : [0, 0, 0, 0, 0, 0, 0], last_seen: seconds ? at : null, commits: { count: 0, items: [] }, recent: [], ...extra });
const before = {
  days, other: { seconds: 5_400, days: [0, 0, 0, 0, 0, 3_600, 1_800], count: 12 }, unsorted: { seconds: 900, days: [0, 0, 0, 0, 0, 0, 900], count: 3 },
  projects: [row('job-search', '求职', 18_000), row('school', '学业', 0)],
  coverage: { timesink: 'available', git: 'available' }, latest_observed_at: at, sorted_at: at, refreshing: false, outcome: null, error: null,
};
const after = structuredClone(before);
after.unsorted = { seconds: 0, days: [0, 0, 0, 0, 0, 0, 0], count: 0 };
after.outcome = 'classified';
after.projects = [
  row('job-search', '求职', 19_800, { recent: [{ app: 'ChatGPT', label: '分析公司背调岗位和JD', seconds: 3_060, last_seen: at }] }),
  row('jarvis', 'Jarvis', 7_200, { commits: { count: 2, items: [{ sha: '355fdb3aaaaa', subject: 'feat: report reads To Do and calendar', committed_at: at, on_main: false, repo: 'jarvis' }] } }),
  row('school', '学业', 0),
];
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let posts = 0, gets = 0, release;
  const barrier = new Promise(resolve => { release = resolve; });
  await page.route('http://127.0.0.1:19099/**', async route => {
    if (!route.request().url().includes('/inherent/projects')) return route.abort();
    if (route.request().method() === 'POST') {
      posts++;
      if (posts === 1) await barrier;
      return route.fulfill({ json: posts < 3 ? after : { ...after, outcome: 'failed', error: '1 batch(es) saved; 验收模拟模型不可用' } });
    }
    gets++;
    return route.fulfill({ json: before });
  });
  await page.goto(`${url}/?port=19099`);
  await page.getByRole('button', { name: 'Dashboard', exact: true }).click();
  const tile = page.locator('[data-module="5"] .module-summary');
  await tile.getByText('求职 · 5.0 h', { exact: false }).waitFor();
  check('tile reads the view and names the week', gets > 0 && (await tile.textContent()).includes('Last 7 days'));
  await tile.getByText('Sorting new activity', { exact: false }).waitFor();
  check('mounting asks for sorting once and keeps the last view visible', posts === 1 && (await tile.textContent()).includes('求职'));
  release();
  await tile.getByText('求职 · 5.5 h', { exact: false }).waitFor();
  check('a finished sort replaces the view', !(await tile.textContent()).includes('unsorted'));
  await page.waitForSelector('.dashboard-stage[data-motion="settled"]');
  await page.screenshot({ path: `${dir}/projects-overview.png` });
  await page.getByRole('button', { name: 'Open Projects', exact: true }).click();
  const panel = page.locator('.projects-detail');
  await panel.getByText('分析公司背调岗位和JD').waitFor();
  check('opening the detail asks for sorting again', posts === 2);
  const firstCard = panel.locator('.project-card').first();
  check('a card per active project, idle ones listed by name', await panel.locator('.project-card').count() === 2 && (await panel.textContent()).includes('近 7 天没碰：学业'));
  check('seven day columns, today marked', await firstCard.locator('.project-day').count() === 7 && await firstCard.locator('.project-day i.is-today').count() === 1);
  check('a column hover names its day and time', (await firstCard.locator('.project-day').last().getAttribute('title')).includes('30 min'));
  check('commits show off-main state', (await panel.textContent()).includes('未进 main'));
  await page.waitForSelector('.dashboard-stage[data-motion="settled"]');
  await page.screenshot({ path: `${dir}/projects-detail.png` });
  await panel.getByRole('button', { name: '归类新活动', exact: true }).click();
  await panel.getByText('归类没做完', { exact: false }).waitFor();
  check('a failed sort keeps the cards and says so', posts === 3 && (await panel.textContent()).includes('分析公司背调岗位和JD'));
  const bare = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  await bare.route('http://127.0.0.1:19099/**', route => route.request().url().includes('/inherent/projects') ? route.fulfill({ status: 404, body: '' }) : route.abort());
  await bare.goto(`${url}/?port=19099`);
  await bare.getByRole('button', { name: 'Dashboard', exact: true }).click();
  await bare.locator('[data-module="5"] .module-summary').getByText('No projects set up').waitFor();
  check('no configured projects says where to add them', true);
  check('no React runtime errors', errors.length === 0);
  writeFileSync(`${dir}/checks.json`, JSON.stringify({ checks, posts, gets, errors }, null, 2));
  console.log(`${checks.length} UI checks passed; evidence: ${dir}`);
} finally { await browser.close(); }

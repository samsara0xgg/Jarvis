// Focused source fixture: real AroundDashboard + SettingsPage with delayed,
// authoritative-history reads. It makes no daemon requests and never saves a key.
import { chromium } from 'playwright';
import { createServer, transformWithOxc } from 'vite';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdirSync, writeFileSync } from 'node:fs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = process.env.DASHBOARD_ASYNC_EVIDENCE_DIR ?? path.join(root, 'evidence/dashboard-async-restore');
const port = Number(process.env.DASHBOARD_ASYNC_PORT ?? 5207);
const routeOrigin = 'http://127.0.0.1:61988';
const checks = [], errors = [], writes = [];
const check = (name, pass) => { assert.ok(pass, name); checks.push(name); console.log(`PASS ${name}`); };
const moduleId = '/@dashboard-restore-fixture.tsx';
const fixture = `
import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { AroundDashboard } from '/src/AroundDashboard.tsx';
import '/src/style.css';
import '/src/companion.css';
const rows = Array.from({ length: 500 }, (_, i) => ({ seq: i + 1, source: i % 2 ? 'jarvis' : 'allen',
  ts: new Date(Date.UTC(2026, 8, 22 + Math.floor(i / 100), 10, i % 60)).toISOString(),
  text: 'History record ' + (i + 1) + '. A readable conversation paragraph retained across a window handoff.' }));
const noop = () => {};
const ctl = { micMuted: false, speechMuted: false, handsFree: false, setMic: noop, setSpeech: noop, setHandsFree: noop,
  look: { skin: 'glass', auto: false, home: 'dark', homeFinish: 'original', marks: 'spark' }, setLook: noop, playFaces: noop,
  cues: { on: false, volume: 0 }, setCues: noop };
window.fixture = { calls: [], lastView: null, generation: 0 };
function Dashboard({ mode, generation }) {
  const ref = useRef(null);
  const [loaded, setLoaded] = useState(mode === 'source' ? rows : []), [floor, setFloor] = useState(mode === 'source');
  const source = mode === 'floor' ? rows.slice(200) : rows;
  const merge = batch => setLoaded(current => [...new Map([...current, ...batch].map(row => [row.seq, row])).values()].sort((a, b) => a.seq - b.seq));
  useEffect(() => {
    if (mode === 'source') return;
    const timer = setTimeout(() => merge(source.slice(-200)), 100);
    return () => clearTimeout(timer);
  }, []);
  const older = async () => {
    const limit = loaded.length + 200, first = loaded[0]?.seq ?? Infinity;
    window.fixture.calls.push({ mode, limit });
    await new Promise(resolve => setTimeout(resolve, 150));
    if (mode === 'failure') throw new Error('Fixture: history temporarily unavailable');
    const batch = source.slice(-limit);
    merge(batch); if (batch.length < limit) setFloor(true);
    return batch.some(row => row.seq < first);
  };
  window.fixture.snapshot = () => ref.current.snapshot();
  window.fixture.restore = value => ref.current.restore(value);
  useEffect(() => { window.fixture.generation = generation; }, [generation]);
  return <AroundDashboard open port="61988" onClose={noop} onMood={noop} onHop={noop} ctl={ctl} viewRef={ref}
    onView={value => { window.fixture.lastView = value; }}
    talk={{ rows: loaded, tail: '', busy: false, offline: mode === 'failure', floor, older, submit: noop,
      think: { on: false, secs: 0, words: [null, null], thoughts: [], exit: noop } }}/>
}
function App() {
  const [state, setState] = useState({ mode: 'source', generation: 1 });
  window.fixture.fresh = mode => { window.fixture.calls = []; window.fixture.lastView = null; setState(s => ({ mode, generation: s.generation + 1 })); };
  return <main className="companion"><div className="companion-dashboard is-open" style={{ visibility: 'visible', left: 140, top: 32 }}>
    <Dashboard key={state.generation} {...state}/></div></main>;
}
createRoot(document.getElementById('root')).render(<App/>);
`;
const server = await createServer({ root, server: { host: '127.0.0.1', port, strictPort: true }, plugins: [{
  name: 'dashboard-restore-acceptance',
  configureServer(server) {
    server.middlewares.use('/__restore', (_req, res) => {
      res.setHeader('Content-Type', 'text/html');
      res.end(`<div id="root"></div><script type="module" src="${moduleId}"></script>`);
    });
  },
  resolveId(id) { if (id === moduleId) return `\0${moduleId}`; },
  load(id) { if (id === `\0${moduleId}`) return transformWithOxc(fixture, path.join(root, 'dashboard-restore-fixture.tsx'), { jsx: { runtime: 'automatic' } }); },
}] });
let browser;
mkdirSync(dir, { recursive: true });
try {
  await server.listen();
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  const page = await browser.newPage({ viewport: { width: 640, height: 760 } });
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => { window.jarvis = { focus: async () => {}, usage: async () => null, material: () => {} }; });
  await page.route(`${routeOrigin}/**`, async route => {
    const request = route.request(), pathname = new URL(request.url()).pathname;
    // The dashboard refreshes its project read model on mount; that existing POST
    // is answered locally too, but is not a key save or a replayed user action.
    if (!['GET', 'OPTIONS'].includes(request.method()) && pathname !== '/inherent/projects/refresh') writes.push(pathname);
    const data = pathname === '/inherent/settings' ? { values: {}, options: {} }
      : pathname === '/inherent/setup' ? { keys: { openai: 'missing', minimax: 'missing', tavily: 'missing' }, voice_models: { state: 'ready' } }
        : pathname.endsWith('sessions') ? { sessions: [] } : {};
    await route.fulfill({ status: 200, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*' }, body: JSON.stringify(data) });
  });
  await page.goto(`http://127.0.0.1:${port}/__restore`);
  await page.waitForFunction(() => window.fixture?.generation === 1);
  const fresh = async mode => {
    const generation = await page.evaluate(mode => { const next = window.fixture.generation + 1; window.fixture.fresh(mode); return next; }, mode);
    await page.waitForFunction(generation => window.fixture.generation === generation, generation);
  };
  const restore = value => page.evaluate(value => window.fixture.restore(value), value);
  const scroll = () => page.locator('.ad .page .pg-body').evaluate(el => el.scrollTop);
  const backHome = async () => {
    await page.locator('.pg-back').click();
    if (await page.locator('.ad .pg-head h3').textContent() === 'Settings') await page.locator('.pg-back').click();
    await page.locator('.ad:not([data-page])').waitFor();
    await page.waitForTimeout(200);
  };

  await page.locator('.corner [data-row="settings"]').click();
  await page.locator('[data-cat="accounts"]').click();
  await page.getByLabel('OpenAI API key', { exact: true }).fill('fixture-openai-unsaved');
  await page.getByLabel('MiniMax API key', { exact: true }).fill('fixture-minimax-unsaved');
  const settings = await page.evaluate(() => window.fixture.snapshot());
  await fresh('source'); await restore(settings);
  await page.getByLabel('OpenAI API key', { exact: true }).waitFor();
  check('account category and both provider key drafts restore in a fresh renderer',
    await page.locator('.pg-head h3').textContent() === 'Accounts'
    && await page.getByLabel('OpenAI API key', { exact: true }).inputValue() === 'fixture-openai-unsaved'
    && await page.getByLabel('MiniMax API key', { exact: true }).inputValue() === 'fixture-minimax-unsaved');
  await page.getByLabel('MiniMax API key', { exact: true }).fill('fixture-minimax-edited');
  const returned = await page.evaluate(() => window.fixture.snapshot());
  await fresh('source'); await restore(returned);
  check('return handoff keeps the edited provider draft', await page.getByLabel('MiniMax API key', { exact: true }).inputValue() === 'fixture-minimax-edited');
  check('key drafts remain in memory without autosave or localStorage', writes.length === 0 && await page.evaluate(() => !JSON.stringify({ ...localStorage }).includes('fixture-')));
  await backHome();
  check('restoring and leaving a settings page reveals the home', await page.locator('.overview').evaluate(el => Number(getComputedStyle(el).opacity) === 1));

  await page.locator('.corner [data-row="conversation"]').click();
  await page.locator('.ad[data-page="conversation"]').waitFor();
  await page.waitForTimeout(300);
  for (let i = 0; i < 4; i++) {
    await page.locator('.pg-body').evaluate(el => { el.scrollTop = 0; el.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -300 })); });
    await page.waitForTimeout(220);
  }
  await page.locator('.pg-body').evaluate(el => { el.scrollTop = 420; });
  const conversation = await page.evaluate(() => window.fixture.snapshot());
  check('source records an expanded five-day range and reading position', conversation.days === 5 && conversation.conversationFirstSeq === 1 && conversation.scroll === 420);
  await fresh('delayed'); await restore(conversation);
  await page.waitForFunction(() => document.querySelectorAll('.tr').length === 250 && Math.abs(document.querySelector('.pg-body').scrollTop - 420) < 1);
  await page.waitForTimeout(350);
  const historyReads = await page.evaluate(() => window.fixture.calls);
  check('delayed initial rows and sequential older reads restore the full range', historyReads.length <= 3 && historyReads.some(call => call.limit === 600));
  check('async history arrival does not pull the reader to the bottom', Math.abs(await scroll() - 420) < 1);

  await fresh('floor'); await restore(conversation);
  await page.waitForFunction(() => document.querySelector('.pg-earlier')?.textContent.includes('Start of the conversation') && Math.abs(document.querySelector('.pg-body').scrollTop - 420) < 1);
  await page.waitForTimeout(350);
  const floorReads = await page.evaluate(() => window.fixture.calls);
  check('an unavailable older range stops at the authoritative floor', floorReads.length === 2 && floorReads.at(-1).limit === 400 && Math.abs(await scroll() - 420) < 1);

  await fresh('failure'); await restore(conversation);
  await page.waitForTimeout(700);
  const pending = await page.evaluate(() => ({ calls: window.fixture.calls, view: window.fixture.snapshot() }));
  check('a failed history read pauses once and retains the requested range and scroll', pending.calls.length === 1 && pending.view.conversationFirstSeq === 1 && pending.view.scroll === 420);
  await page.locator('.pg-body').evaluate(el => { el.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: 100 })); el.scrollTop = 100; });
  await page.waitForTimeout(300);
  check('intentional scrolling takes over after a failed restore', Math.abs(await scroll() - 100) < 1 && (await page.evaluate(() => window.fixture.snapshot())).scroll === 100);
  await fresh('failure'); await restore(conversation); await page.waitForTimeout(300);
  await backHome();
  await page.locator('.corner [data-row="conversation"]').click(); await page.waitForTimeout(300);
  check('leaving the page cancels a paused restore before a later visit', await page.evaluate(() => window.fixture.snapshot().conversationFirstSeq === 301 && window.fixture.calls.length === 1));
  check('restoration never sends requests or replays a save', writes.length === 0);
  check('no unhandled renderer errors', errors.length === 0);
  writeFileSync(path.join(dir, 'checks.json'), JSON.stringify({ checks, errors, writes, historyReads, floorReads }, null, 2));
  console.log(`Dashboard async restore: ${checks.length} checks passed`);
} catch (error) {
  writeFileSync(path.join(dir, 'failure.json'), JSON.stringify({ checks, errors, writes, error: String(error) }, null, 2));
  throw error;
} finally { await browser?.close(); await server.close(); }

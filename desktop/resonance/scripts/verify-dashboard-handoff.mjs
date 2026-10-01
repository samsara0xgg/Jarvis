// Run after the final renderer + Electron build. Every process uses an offline demo profile.
import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const profile = mkdtempSync(path.join(tmpdir(), 'jarvis-dashboard-handoff-'));
const evidence = 'evidence/dashboard-handoff'; mkdirSync(evidence, { recursive: true });
const checks = [], errors = [], requests = [], blocked = [];
let app, parent, child;
const check = (name, pass) => { assert.ok(pass, name); checks.push(name); console.log('PASS', name); };
const homeInput = page => page.locator('.ad .cmp input');
const talkInput = page => page.locator('.ad .page .pg-input input');
const pageIs = (page, name) => page.waitForFunction(name => (document.querySelector('.ad')?.getAttribute('data-page') ?? '') === name, name);
const settle = page => page.waitForTimeout(400);
const childWindow = () => app.evaluate(({ BrowserWindow }) => {
  const w = BrowserWindow.getAllWindows().find(w => w.webContents.getURL().includes('detached=1'));
  return w ? { id: w.id, visible: w.isVisible(), url: w.webContents.getURL() } : null;
});
const detach = async () => {
  await parent.keyboard.press('Meta+Shift+ArrowDown');
  await parent.waitForFunction(async () => (await window.jarvis.dashboard('state')).detached);
  for (let n = 0; n < 100; n++) {
    child = app.windows().find(w => w.url().includes('detached=1'));
    if (child) break;
    await new Promise(resolve => setTimeout(resolve, 30));
  }
  assert.ok(child, 'detached renderer exists');
  child.on('pageerror', error => errors.push(error.message));
  await child.waitForSelector('.companion-detached .companion-dashboard.is-open');
  await settle(child);
};
const attach = async () => {
  await child.keyboard.press('Meta+Shift+ArrowUp');
  await parent.waitForFunction(async () => !(await window.jarvis.dashboard('state')).detached);
  await parent.waitForSelector('.companion-dashboard.is-open');
  await settle(parent);
};
try {
  app = await electron.launch({ args: ['dist-electron/companion.js', '--demo'], cwd: process.cwd(),
    env: { ...process.env, JARVIS_COMPANION_TEST_PROFILE: profile } });
  parent = await app.firstWindow(); parent.on('pageerror', error => errors.push(error.message));
  await parent.waitForSelector('.companion-island-target'); await settle(parent);
  await app.evaluate(({ ipcMain }) => {
    globalThis.handoffMessages = [];
    ipcMain.on('dashboard-message', (_event, target, payload) => globalThis.handoffMessages.push({ target, payload }));
  });
  const island = parent.locator('.companion-island-target');
  const islandPoint = await island.evaluate(node => {
    const box = node.getBoundingClientRect(), y = Math.min(12, box.height / 2);
    for (let x = 8; x < box.width - 8; x += 8) if (document.elementFromPoint(box.x + x, box.y + y) === node) return { x, y };
    throw new Error('No exposed Dashboard click target');
  });
  await island.click({ position: islandPoint });
  await parent.waitForSelector('.companion-dashboard.is-open'); await settle(parent);
  await homeInput(parent).click(); await homeInput(parent).fill('Unsent home draft from the notch');
  await detach();
  check('Cmd+Shift+Down transfers the home page and unsent draft', await homeInput(child).inputValue() === 'Unsent home draft from the notch' && await child.locator('.ad').getAttribute('data-page') === null);
  check('detached presentation contains no character or notch home', await child.locator('.companion-canvas,.companion-hit,.companion-island-target,.notch').count() === 0);
  await homeInput(child).click(); await homeInput(child).fill('Edited in the detached home'); await settle(child);
  await attach();
  check('Cmd+Shift+Up restores the edited home draft', await homeInput(parent).inputValue() === 'Edited in the detached home' && !(await childWindow()).visible);

  await parent.locator('.ad .corner [data-row="conversation"]').click(); await pageIs(parent, 'conversation'); await settle(parent);
  await talkInput(parent).click(); await talkInput(parent).fill('Conversation draft remains unsent');
  const scroll = await parent.locator('.ad .page .pg-body').evaluate(node => { node.scrollTop = 65; return node.scrollTop; });
  await detach(); await pageIs(child, 'conversation');
  check('conversation page, independent draft and home draft survive detach', await talkInput(child).inputValue() === 'Conversation draft remains unsent' && await homeInput(child).inputValue() === 'Edited in the detached home');
  check('conversation scroll position is restored on detach', Math.abs(await child.locator('.ad .page .pg-body').evaluate(node => node.scrollTop) - scroll) <= 2);
  await talkInput(child).click(); await talkInput(child).fill('Conversation draft edited in the window'); await settle(child);
  await attach(); await pageIs(parent, 'conversation');
  check('reattach restores the conversation page and its latest unsent edit', await talkInput(parent).inputValue() === 'Conversation draft edited in the window' && await homeInput(parent).inputValue() === 'Edited in the detached home');
  await parent.screenshot({ path: `${evidence}/conversation-restored.png`, omitBackground: true });

  await talkInput(parent).click(); await detach();
  const id = (await childWindow()).id;
  await parent.locator('.companion-hit').click({ button: 'right', force: true });
  await parent.getByRole('menuitem', { name: 'Settings…', exact: true }).click();
  await pageIs(child, 'settings'); await settle(child);
  check('the character Settings menu targets the existing detached window', (await childWindow()).id === id && (await childWindow()).visible && await parent.locator('.companion-dashboard.is-open').count() === 0);
  await child.locator('.ad .st-cat').filter({ hasText: 'General' }).click(); await settle(child);
  await attach();
  check('the selected settings category survives attach', await parent.locator('.ad .pg-head h3').textContent() === 'General');
  await parent.locator('.ad .pg-back').click(); await settle(parent);
  await parent.locator('.ad .pg-back').click(); await pageIs(parent, ''); await settle(parent);
  await homeInput(parent).click(); await detach();
  check('reused child restores visible home content after leaving a detail page', await child.locator('.ad .overview').evaluate(node => getComputedStyle(node).opacity === '1'
    && node.querySelector('.next-event').checkVisibility({ opacityProperty: true, visibilityProperty: true })
    && node.querySelector('.home-inner > *').checkVisibility({ opacityProperty: true, visibilityProperty: true })));
  await parent.evaluate(() => document.documentElement.style.setProperty('--glow', '51 102 153'));
  await child.waitForFunction(() => getComputedStyle(document.documentElement).getPropertyValue('--glow').trim() === '51 102 153');
  const dim = await child.locator('.dusk-material').evaluate(node => getComputedStyle(node).backgroundImage);
  await parent.evaluate(() => document.documentElement.style.setProperty('--glow', '201 151 101'));
  await child.waitForFunction(() => getComputedStyle(document.documentElement).getPropertyValue('--glow').trim() === '201 151 101');
  check('parent glow updates the child material without creating another character', dim !== await child.locator('.dusk-material').evaluate(node => getComputedStyle(node).backgroundImage) && await child.locator('.companion-canvas').count() === 0);
  check('presentation handoff travels through the real native relay', await app.evaluate(() => globalThis.handoffMessages.some(m => m.target === 'dashboard' && m.payload.type === 'view' && m.payload.value.talkDraft === 'Conversation draft remains unsent')
    && globalThis.handoffMessages.some(m => m.target === 'parent' && m.payload.type === 'view' && m.payload.value.talkDraft === 'Conversation draft edited in the window')));
  await child.screenshot({ path: `${evidence}/detached-home.png`, omitBackground: true });

  // The demo's Agents rows have only simulated Approve/Deny. Exercise the actual Answer UI with
  // a fixed local route fixture, still inside the demo main process. No request reaches a server.
  const fixturePort = '61987', fixtureOrigin = `http://127.0.0.1:${fixturePort}`;
  const session = { session_id: 'handoff-held-request', kind: 'background', phase: 'needs_input', title: 'Isolated Answer handoff', project: 'acceptance', branch: '', where: 'background',
    prompt: 'Review the handoff fixture without executing it', activity: '', last_message: '', updated_ms: Date.now(),
    request: { id: 'handoff-request', tool: 'Bash', input: { command: 'echo acceptance-only', description: 'A local acceptance fixture' }, cwd: '/tmp/acceptance', always: '' } };
  await app.context().route(/^https?:\/\//, async route => {
    const req = route.request(), url = new URL(req.url());
    if (url.origin !== fixtureOrigin) { blocked.push(req.url()); await route.abort(); return; }
    requests.push({ method: req.method(), path: url.pathname, body: req.postData() });
    let data, status = 200;
    if (url.pathname === '/inherent/claude-sessions') data = { sessions: [session] };
    else if (url.pathname === '/inherent/codex-sessions') data = { sessions: [] };
    else if (url.pathname === '/inherent/agent-marks') data = { marks: {} };
    else if (url.pathname.startsWith('/inherent/agent-marks/')) data = {};
    else if (url.pathname === '/inherent/conversation') data = { rows: [] };
    else if (['/inherent/confirmation', '/inherent/clarification'].includes(url.pathname)) data = { card: null };
    else if (url.pathname === '/inherent/controls') data = { mic_muted: false, speech_muted: false, conversation: false };
    else if (url.pathname === '/inherent/language') data = { language: 'en' };
    else if (url.pathname === '/inherent/think') data = { on: false, on_words: 'think deeply' };
    else { data = {}; status = 404; }
    await route.fulfill({ status, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' }, body: JSON.stringify(data) });
  });
  await app.context().routeWebSocket(/.*/, socket => { if (!socket.url().startsWith(`ws://127.0.0.1:${fixturePort}/`)) blocked.push(socket.url()); });
  await app.evaluate(async ({ BrowserWindow }, { page, port }) => {
    for (const w of BrowserWindow.getAllWindows()) {
      const detached = new URL(w.webContents.getURL()).searchParams.has('detached');
      await w.loadFile(page, { query: { companion: '1', port, ...detached ? { detached: '1' } : {} } });
    }
  }, { page: path.resolve('dist/index.html'), port: fixturePort });
  await parent.evaluate(() => window.jarvis.dashboard('open'));
  await parent.waitForFunction(() => !document.querySelector('.notch-note')?.classList.contains('is-open'));
  await child.waitForSelector('.companion-detached .ad [data-row="agents"]');
  await child.locator('.ad [data-row="agents"]').click(); await pageIs(child, 'agents');
  const agent = child.locator('.ad .ag[data-id="handoff-held-request"]');
  await agent.locator('.ag-head').click();
  await agent.getByRole('button', { name: 'Answer', exact: true }).click();
  await parent.locator('.notch-note .nc-head b').filter({ hasText: 'Isolated Answer handoff' }).waitFor({ state: 'visible' });
  check('detached Agent Answer opens the matching parent notice and hides the window', !(await childWindow()).visible && (await parent.locator('.notch-note .nc-cmd').textContent()).includes('tmp/acceptance $ echo acceptance-only'));
  check('Answer routes the notice without resolving or submitting it', !requests.some(req => req.method === 'POST' && (/\/(submit|confirmation|clarification)$/.test(req.path) || req.path.includes('/claude-requests/') || req.path.endsWith('/reply'))));
  check('the fixture scenario reaches no real daemon or external network', blocked.length === 0 && requests.some(req => req.path === '/inherent/claude-sessions'));
  await parent.screenshot({ path: `${evidence}/answer-in-parent.png`, omitBackground: true });
  check('integrated handoff has no uncaught renderer errors', errors.length === 0);
  rmSync(`${evidence}/failure.json`, { force: true });
  writeFileSync(`${evidence}/checks.json`, JSON.stringify({ checks, errors, requests, blocked, profile }, null, 2));
  console.log(`Dashboard handoff: ${checks.length} checks passed`);
} catch (error) {
  writeFileSync(`${evidence}/failure.json`, JSON.stringify({ checks, errors, requests, blocked, error: String(error) }, null, 2));
  await parent?.screenshot({ path: `${evidence}/failure-parent.png`, omitBackground: true }).catch(() => {});
  if ((await childWindow())?.visible) await child?.screenshot({ path: `${evidence}/failure-child.png`, omitBackground: true, timeout: 3000 }).catch(() => {});
  throw error;
} finally { await app?.close(); }

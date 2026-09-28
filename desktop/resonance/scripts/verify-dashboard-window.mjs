import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

// A separate offline Companion profile: never relaunch or touch the user's live instance.
const profile = mkdtempSync(path.join(tmpdir(), 'jarvis-dashboard-window-'));
const evidence = 'evidence/dashboard-window'; mkdirSync(evidence, { recursive: true });
const checks = [], errors = [];
let app;
const check = (name, pass) => { assert.ok(pass, name); checks.push(name); console.log('PASS', name); };
const launch = async () => {
  app = await electron.launch({ args: ['dist-electron/companion.js', '--demo'], cwd: process.cwd(),
    env: { ...process.env, JARVIS_COMPANION_TEST_PROFILE: profile } });
  const parent = await app.firstWindow();
  parent.on('pageerror', error => errors.push(error.message));
  await parent.waitForFunction(() => typeof window.jarvis?.dashboard === 'function');
  return parent;
};
const states = () => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().map(w => ({ id: w.id, url: w.webContents.getURL(), visible: w.isVisible(), bounds: w.getBounds() })));
const childState = async () => (await states()).find(w => new URL(w.url).searchParams.has('detached'));
try {
  let parent = await launch();
  check('demo uses an isolated profile', await app.evaluate(({ app }, expected) => app.getPath('userData') === expected, profile));
  check('initial state is attached and no child is created', !(await parent.evaluate(() => window.jarvis.dashboard('state'))).detached && (await states()).length === 1);
  await parent.evaluate(() => { window.dashboardEvents = []; window.dockEvents = []; window.jarvis.onDashboard(value => window.dashboardEvents.push(value)); window.jarvis.onDashboardDock(value => window.dockEvents.push(value)); });
  await app.evaluate(({ app }) => {
    globalThis.relayDeliveries = [];
    app.on('browser-window-created', (_event, w) => {
      const send = w.webContents.send.bind(w.webContents);
      w.webContents.send = (channel, ...args) => { if (channel === 'dashboard-message') globalThis.relayDeliveries.push({ id: w.id, payload: args[0] }); return send(channel, ...args); };
    });
  });
  await parent.evaluate(() => {
    window.relayReceived = []; window.jarvis.onDashboardMessage(value => window.relayReceived.push(value));
    window.jarvis.dashboardMessage('dashboard', { type: 'acceptance', draft: 'old' });
    window.jarvis.dashboardMessage('dashboard', { type: 'acceptance', draft: 'retained unsent draft' });
  });
  // Fail the native load once, before any child has been shown: renderer must keep its attached UI.
  await app.evaluate(({ BrowserWindow }) => {
    const original = BrowserWindow.prototype.loadFile;
    BrowserWindow.prototype.loadFile = function (...args) { BrowserWindow.prototype.loadFile = original; return Promise.reject(new Error('Acceptance: unavailable local page')); };
  });
  const failed = await parent.evaluate(() => window.jarvis.dashboard('detach').then(() => '', error => error.message));
  check('failed first detach rejects and leaves attached mode intact', failed.includes('unavailable local page') && !(await parent.evaluate(() => window.jarvis.dashboard('state'))).detached && (await states()).length === 1);
  check('detach succeeds only after loading the trusted child', (await parent.evaluate(() => window.jarvis.dashboard('detach', { height: 540 }))).detached);
  const child = app.windows().find(w => new URL(w.url()).searchParams.has('detached'));
  assert.ok(child); child.on('pageerror', error => errors.push(error.message));
  await child.evaluate(() => { window.relayReceived = []; window.jarvis.onDashboardMessage(value => window.relayReceived.push(value)); });
  await child.waitForTimeout(80);
  check('relay queues the latest message of each type until the child subscribes', await app.evaluate(() => {
    const messages = globalThis.relayDeliveries.filter(row => row.payload.type === 'acceptance');
    return messages.length === 1 && messages[0].payload.draft === 'retained unsent draft';
  }));
  await child.evaluate(() => window.jarvis.dashboardMessage('parent', { type: 'acceptance', draft: 'edited detached draft' }));
  await parent.waitForTimeout(80);
  check('child can relay its view back only to the parent', await parent.evaluate(() => window.relayReceived.some(value => value.draft === 'edited detached draft')));
  await parent.evaluate(() => window.jarvis.dashboardMessage('dashboard', { type: 'oversize', text: 'x'.repeat(65536) }));
  await child.waitForTimeout(80);
  check('relay rejects payloads above 64 KiB', await app.evaluate(() => !globalThis.relayDeliveries.some(row => row.payload.type === 'oversize')));
  const first = await childState();
  check('detached window uses the same local page and fixed demo query', first.url.startsWith('file:') && new URL(first.url).searchParams.get('companion') === '1' && !new URL(first.url).searchParams.has('port') && first.bounds.width === 360 && first.visible);
  const placement = await child.evaluate(() => window.jarvis.placement());
  check('child placement has no notch or home inset', placement.topInset === 0 && placement.notchWidth === 0 && placement.surfaceWidth === 360);
  check('child retains sandbox and context isolation', await app.evaluate(({ BrowserWindow }) => {
    const w = BrowserWindow.getAllWindows().find(w => w.webContents.getURL().includes('detached=1')), p = w.webContents.getLastWebPreferences();
    return p.contextIsolation && p.sandbox && !p.nodeIntegration;
  }));
  const bridge = await child.evaluate(async () => ({
    plugin: await window.jarvis.plugins('read').catch(error => error.message),
    usage: await window.jarvis.usageBalance('openai', 1).catch(error => error.message),
    account: await window.jarvis.openAccount('openai'), titles: await window.jarvis.codexTitles([]),
  }));
  check('child reaches shared service handlers while demo remains offline', bridge.plugin.includes('此预览未连接') && bridge.usage.includes('preview is not connected') && bridge.account === false && Object.keys(bridge.titles).length === 0);
  await app.evaluate(({ BrowserWindow }) => {
    const child = BrowserWindow.getAllWindows().find(w => w.webContents.getURL().includes('detached=1'));
    globalThis.passCalls = 0; const original = child.setIgnoreMouseEvents.bind(child);
    child.setIgnoreMouseEvents = (...args) => { globalThis.passCalls++; return original(...args); };
  });
  await child.evaluate(() => window.jarvis.passthrough(true)); await child.waitForTimeout(80);
  check('child cannot enable native click-through', await app.evaluate(() => globalThis.passCalls === 0));
  // A third window may load the very same preload and page, but never joins the explicit allowlist.
  const rogueCreated = app.waitForEvent('window');
  await app.evaluate(async ({ BrowserWindow }, preload) => {
    const rogue = new BrowserWindow({ show: false, webPreferences: { preload, contextIsolation: true, nodeIntegration: false, sandbox: true } });
    globalThis.rogueId = rogue.id; await rogue.loadURL('data:text/html,<title>Untrusted acceptance window</title>');
  }, path.resolve('dist-electron/preload.cjs'));
  const rogue = await rogueCreated;
  await rogue.waitForFunction(() => typeof window.jarvis?.dashboard === 'function');
  const denied = await rogue.evaluate(async () => ({ dashboard: await window.jarvis.dashboard('attach').catch(e => e.message),
    plugin: await window.jarvis.plugins('read').catch(e => e.message), usage: await window.jarvis.usageBalance('openai', 0).catch(e => e.message), placement: await window.jarvis.placement() }));
  check('unlisted window cannot operate dashboard or shared daemon IPC', denied.dashboard.includes('Not this Dashboard window') && denied.plugin.includes('无效的插件窗口') && denied.usage.includes('Not this window') && denied.placement === null && (await parent.evaluate(() => window.jarvis.dashboard('state'))).detached);
  await rogue.evaluate(() => window.jarvis.dashboardMessage('parent', { type: 'rogue', text: 'no' }));
  await parent.waitForTimeout(80);
  check('unlisted windows cannot use the presentation relay', await parent.evaluate(() => !window.relayReceived.some(value => value.type === 'rogue')));
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.fromId(globalThis.rogueId).destroy());
  await child.evaluate(() => { const iframe = document.createElement('iframe'); iframe.srcdoc = '<p>child frame</p>'; document.body.append(iframe); });
  await child.waitForTimeout(80);
  const subframe = await app.evaluate(async ({ BrowserWindow, ipcMain }) => {
    const w = BrowserWindow.getAllWindows().find(w => w.webContents.getURL().includes('detached=1'));
    const event = { sender: w.webContents, senderFrame: w.webContents.mainFrame.frames[0] };
    const denied = [];
    for (const [channel, args] of [['dashboard', ['attach']], ['plugins', ['read']], ['usage-balance', ['openai', 0]]]) {
      try { await ipcMain._invokeHandlers.get(channel)(event, ...args); denied.push(false); } catch { denied.push(true); }
    }
    return denied;
  });
  check('trusted webContents still rejects a real subframe at each privileged guard', subframe.length === 3 && subframe.every(Boolean));
  await child.evaluate(() => document.querySelector('iframe')?.remove());
  await child.evaluate(() => window.jarvis.dashboardSize(99999)); await child.waitForTimeout(100);
  check('requested height clamps to the current display work area', await app.evaluate(({ BrowserWindow, screen }) => {
    const b = BrowserWindow.getAllWindows().find(w => w.webContents.getURL().includes('detached=1')).getBounds(), a = screen.getDisplayMatching(b).workArea;
    return b.height <= a.height && b.y >= a.y && b.y + b.height <= a.y + a.height;
  }));
  const expandedHeight = (await childState()).bounds.height;
  await child.evaluate(() => window.jarvis.dashboardSize(420)); await child.waitForTimeout(80);
  check('content size requests cannot shrink into a viewport feedback loop', (await childState()).bounds.height === expandedHeight);
  await app.evaluate(({ BrowserWindow, screen }) => {
    const w = BrowserWindow.getAllWindows().find(w => w.webContents.getURL().includes('detached=1')), a = screen.getPrimaryDisplay().workArea;
    w.setBounds({ x: a.x + 100, y: a.y + 100, width: 360, height: 420 });
  });
  // Deterministic system cursor/button observations exercise the actual native-drag controller.
  await app.evaluate(async ({ screen, BrowserWindow, ipcMain }, nativePath) => {
    const { createRequire } = process.getBuiltinModule('node:module');
    globalThis.testMaterial = createRequire(process.cwd() + '/package.json')(nativePath);
    globalThis.actualMouseDown = globalThis.testMaterial.leftMouseDown;
    globalThis.testMouseDown = true; globalThis.testMaterial.leftMouseDown = () => globalThis.testMouseDown;
    globalThis.actualCursor = screen.getCursorScreenPoint;
    const a = screen.getPrimaryDisplay().workArea;
    globalThis.testCursor = { x: a.x + a.width / 2, y: a.y + 180 };
    screen.getCursorScreenPoint = () => ({ ...globalThis.testCursor });
  }, path.resolve('dist-native/material.node'));
  await child.evaluate(() => window.jarvis.dashboardDrag('start')); await child.waitForTimeout(60);
  const beforeMove = (await childState()).bounds;
  await app.evaluate(() => { globalThis.testCursor.x += 100; globalThis.testCursor.y += 50; });
  await child.waitForTimeout(100);
  const afterMove = (await childState()).bounds;
  check('native drag follows screen cursor deltas without renderer coordinates', afterMove.x === beforeMove.x + 100 && afterMove.y === beforeMove.y + 50);
  await app.evaluate(() => { globalThis.testMouseDown = false; }); await child.waitForTimeout(80);
  const released = (await childState()).bounds;
  await app.evaluate(() => { globalThis.testCursor.x += 90; }); await child.waitForTimeout(80);
  check('native mouse release ends drag even without renderer pointerup', JSON.stringify((await childState()).bounds) === JSON.stringify(released));
  await app.evaluate(({ BrowserWindow, screen }) => {
    const w = BrowserWindow.getAllWindows().find(w => w.webContents.getURL().includes('detached=1'));
    globalThis.actualMatching = screen.getDisplayMatching;
    const actual = screen.getDisplayMatching(w.getBounds());
    screen.getDisplayMatching = () => ({ ...actual, bounds: { ...actual.bounds, y: w.getBounds().y + 500 } });
    globalThis.testMouseDown = true;
  });
  await child.evaluate(() => { window.jarvis.dashboardDrag('start'); window.jarvis.dashboardDrag('end'); });
  await child.waitForTimeout(80);
  check('a window far above the display is not treated as near its notch', (await parent.evaluate(() => window.jarvis.dashboard('state'))).detached);
  await app.evaluate(({ screen }) => { screen.getDisplayMatching = globalThis.actualMatching; });
  const attachedDisplay = await app.evaluate(({ BrowserWindow, screen }) => {
    const w = BrowserWindow.getAllWindows().find(w => w.webContents.getURL().includes('detached=1')), d = screen.getAllDisplays().at(-1);
    w.setBounds({ x: d.bounds.x + Math.round((d.bounds.width - 360) / 2), y: d.workArea.y, width: 360, height: 420 });
    globalThis.testMouseDown = true; return d.id;
  });
  await parent.evaluate(() => { window.dockEvents = []; });
  await child.evaluate(() => window.jarvis.dashboardDrag('start')); await child.waitForTimeout(80);
  check('dragging near the notch starts receiving feedback on the parent', await parent.evaluate(() => window.dockEvents.length === 1 && window.dockEvents[0] === true));
  await child.evaluate(() => window.jarvis.dashboardDrag('move')); await child.waitForTimeout(50);
  check('unchanged near-notch geometry does not repeat receiving events', await parent.evaluate(() => window.dockEvents.length === 1));
  await app.evaluate(() => { globalThis.testCursor.x += 200; }); await child.waitForTimeout(80);
  check('leaving the notch zone clears receiving feedback', await parent.evaluate(() => window.dockEvents.at(-1) === false));
  await app.evaluate(() => { globalThis.testCursor.x -= 200; }); await child.waitForTimeout(80);
  await child.evaluate(() => window.jarvis.dashboardDrag('end')); await child.waitForTimeout(100);
  check('releasing into the notch clears receiving feedback after attach', await parent.evaluate(() => JSON.stringify(window.dockEvents) === '[true,false,true,false]'));

  check('releasing near the notch attaches and reopens the parent', !(await parent.evaluate(() => window.jarvis.dashboard('state'))).detached && !(await childState()).visible
    && await parent.evaluate(() => window.dashboardEvents.some(v => v.detached === false && v.open === true)));
  check('attaching places the parent on the child display', (await parent.evaluate(() => window.jarvis.placement())).displayId === attachedDisplay);
  await parent.evaluate(async () => { await window.jarvis.focus(false); window.jarvis.dashboardVisible(true); }); await parent.waitForTimeout(100);
  check('visible unfocused attached dashboard registers the temporary detach shortcut', await app.evaluate(({ globalShortcut }) => globalShortcut.isRegistered('CommandOrControl+Shift+Down')));
  await parent.evaluate(() => window.jarvis.focus(true)); await parent.waitForTimeout(100);
  check('focused parent releases the global shortcut to its local key handler', await app.evaluate(({ globalShortcut }) => !globalShortcut.isRegistered('CommandOrControl+Shift+Down')));
  await parent.evaluate(async () => { window.jarvis.dashboardVisible(false); await window.jarvis.focus(false); }); await parent.waitForTimeout(100);
  check('hidden attached dashboard leaves no global detach shortcut', await app.evaluate(({ globalShortcut }) => !globalShortcut.isRegistered('CommandOrControl+Shift+Down')));
  await parent.evaluate(() => window.jarvis.dashboard('detach', { height: 420 }));
  await child.evaluate(() => window.jarvis.dashboard('close'));
  check('close hides the child and preserves detached mode', !(await childState()).visible && (await parent.evaluate(() => window.jarvis.dashboard('state'))).detached);
  await parent.evaluate(() => window.jarvis.dashboard('open'));
  check('opening a detached dashboard reuses the existing native window', (await childState()).visible && (await childState()).id === first.id && (await states()).length === 2);
  await child.screenshot({ path: `${evidence}/detached-demo.png`, omitBackground: true });
  await child.evaluate(() => window.jarvis.dashboard('close'));
  await app.evaluate(({ screen }) => { screen.getCursorScreenPoint = globalThis.actualCursor; globalThis.testMaterial.leftMouseDown = globalThis.actualMouseDown; });
  await app.close(); app = null;
  const persisted = JSON.parse(readFileSync(path.join(profile, 'dashboard.json'), 'utf8'));
  check('mode and native bounds persist in the isolated profile', persisted.detached === true && persisted.bounds.width === 360 && Number.isFinite(persisted.bounds.x));
  // Simulate a removed display from a previous run. Restore mode, then clamp its old position.
  writeFileSync(path.join(profile, 'dashboard.json'), JSON.stringify({ detached: true, bounds: { x: 999999, y: -999999, width: 360, height: 2000 } }));
  parent = await launch();
  check('restart restores detached preference without opening an unsolicited window', (await parent.evaluate(() => window.jarvis.dashboard('state'))).detached && (await states()).length === 1);
  await parent.evaluate(() => window.jarvis.dashboard('open'));
  check('stale off-display bounds clamp to an available work area on restore', await app.evaluate(({ BrowserWindow, screen }) => {
    const b = BrowserWindow.getAllWindows().find(w => w.webContents.getURL().includes('detached=1')).getBounds();
    return screen.getAllDisplays().some(d => b.x >= d.workArea.x && b.y >= d.workArea.y && b.x + b.width <= d.workArea.x + d.workArea.width && b.y + b.height <= d.workArea.y + d.workArea.height);
  }));
  check('renderers have no uncaught runtime errors', errors.length === 0);
  writeFileSync(`${evidence}/checks.json`, JSON.stringify({ checks, errors, profile }, null, 2));
  console.log(`Dashboard native window: ${checks.length} checks passed`);
} finally { await app?.close(); }

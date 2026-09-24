import { app, BrowserWindow, Menu, Tray, nativeImage, ipcMain, screen, globalShortcut, session, clipboard, shell } from 'electron';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { clampBounds } from './geometry.js';
const here = path.dirname(fileURLToPath(import.meta.url));
const dashboard = process.argv.includes('--dashboard');
const lab = process.argv.includes('--lab') || dashboard;
const verification = process.argv.includes('--verify');
const require = createRequire(import.meta.url);
const material = process.platform === 'darwin' && !lab ? require('../dist-native/material.node') : null;
app.setName('Jarvis Resonance');
// Prototype storage stays isolated from the existing Jarvis runtime.
app.setPath('userData', verification ? path.resolve(here, '../.electron-profile/verification') : app.isPackaged ? path.join(app.getPath('appData'), 'Jarvis Resonance Prototype') : path.resolve(here, '../.electron-profile'));
// Let the animation preview coexist with the capsule and other design work.
if (lab) app.setPath('userData', `${app.getPath('userData')}-${dashboard ? 'dashboard' : 'motion'}-lab`);
const locked = app.requestSingleInstanceLock();
if (!locked) app.quit();
let win: BrowserWindow;
let tray: Tray;
let codexTitles: Record<string, string> = {};
let codexTitlesAt = 0;
function openDashboard() {
  restore(true);
  win.webContents.send('command', 'dashboard');
}
let quitting = false;
let dragGesture: { origin: { x: number; y: number }; bounds: Electron.Rectangle; moved: boolean } | null = null;
function moveDrag() {
  if (!dragGesture || !win || win.isDestroyed()) return;
  // Screen coordinates from forwarded DOM events can be stale or out of range.
  // Both ends of the gesture use Electron's native DIP coordinate space.
  const point = screen.getCursorScreenPoint();
  const dx = point.x - dragGesture.origin.x, dy = point.y - dragGesture.origin.y;
  if (Math.hypot(dx, dy) < 4 && !dragGesture.moved) return;
  const x = Math.round(dragGesture.bounds.x + dx), y = Math.round(dragGesture.bounds.y + dy);
  if (![x, y].every(value => Number.isInteger(value) && value >= -2147483648 && value <= 2147483647)) {
    dragGesture = null;
    return;
  }
  dragGesture.moved = true;
  win.setPosition(x, y);
}
function endDrag() {
  dragGesture = null;
  if (win && !win.isDestroyed()) {
    const b = win.getBounds();
    win.setBounds(clampBounds(b, screen.getDisplayMatching(b).workArea));
  }
}
function keepOnTop() {
  if (lab || !win || win.isDestroyed()) return;
  win.setAlwaysOnTop(true, process.platform === 'darwin' ? 'floating' : 'screen-saver');
  // Raise without stealing keyboard focus from the user's active application.
  if (win.isVisible()) win.moveTop();
}
function restore(focus = false) {
  if (!win || win.isDestroyed()) return;
  const bounds = win.getBounds();
  win.setBounds(clampBounds(bounds, screen.getDisplayMatching(bounds).workArea));
  win.setIgnoreMouseEvents(false);
  if (focus) { win.setFocusable(true); win.show(); win.focus(); }
  else win.showInactive();
  keepOnTop();
}
app.on('second-instance', () => restore());
if (locked) app.whenReady().then(() => {
  session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
  const area = screen.getPrimaryDisplay().workArea;
  // Keep enough transparent room for the composer so its material can morph
  // without resizing/clipping the native window. Empty space passes through.
  win = new BrowserWindow({ title: dashboard ? 'Jarvis · Dashboard 设计预览' : lab ? 'Jarvis · 声纹切换预览' : 'Jarvis Resonance', width: lab ? 1040 : 372, height: dashboard ? 840 : lab ? 740 : 100,
    x: Math.round(area.x + (area.width - (lab ? 1040 : 372)) / 2), y: area.y + Math.round(area.height * .32),
    type: !lab && process.platform === 'darwin' ? 'panel' : undefined,
    frame: lab, transparent: !lab, backgroundColor: lab ? '#151c19' : '#00000000', hasShadow: lab,
    resizable: lab, maximizable: lab, fullscreenable: lab, show: false, focusable: lab,
    alwaysOnTop: !lab, skipTaskbar: !lab, roundedCorners: false,
    webPreferences: { preload: path.join(here, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true }
  });
  // Match the installed Codex pet: a nonactivating macOS panel at floating
  // level. Raising an ordinary NSWindow cannot make it join fullscreen Spaces.
  keepOnTop();
  if (!lab && process.platform === 'darwin') {
    app.dock?.hide();
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
    keepOnTop();
  }
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  if (verification) win.webContents.setAudioMuted(true);
  win.webContents.on('will-navigate', event => event.preventDefault());
  // Lab and verification stay simulated; the desktop build talks to the daemon (same port env as the Swift card).
  win.loadFile(path.join(here, '../dist/index.html'), { query: dashboard ? { lab: '1', dashboard: '1' } : lab ? { lab: '1' } : verification ? {} : { port: process.env.JARVIS_INHERENT_BRIDGE_PORT ?? '8006' } });
  win.once('ready-to-show', () => restore(lab));
  win.on('show', keepOnTop);
  win.on('blur', () => { setImmediate(() => { if (win && !win.isDestroyed() && win.isVisible()) keepOnTop(); }); });
  win.on('close', event => { if (!quitting) { event.preventDefault(); win.hide(); } });
  win.on('moved', () => {
    if (dragGesture) return; // Crossing displays must not clamp mid-gesture.
    const b = win.getBounds(); const clamped = clampBounds(b, screen.getDisplayMatching(b).workArea);
    if (b.x !== clamped.x || b.y !== clamped.y) win.setBounds(clamped);
  });
  screen.on('display-removed', () => restore());
  screen.on('display-metrics-changed', () => { if (win.isVisible()) restore(); });
  ipcMain.on('layout', (event, payload) => {
    if (event.sender !== win.webContents || lab) return;
    if (!['voice', 'text', 'idle'].includes(payload?.mode) || !Number.isFinite(payload.height)) return;
    const b = win.getBounds();
    const next = clampBounds({ ...b, height: Math.max(100, Math.min(screen.getDisplayMatching(b).workArea.height - 24, Math.round(payload.height))) }, screen.getDisplayMatching(b).workArea);
    if (b.x !== next.x || b.y !== next.y || b.height !== next.height) win.setBounds(next);
  });
  ipcMain.handle('focus-input', (event, enabled) => {
    if (event.sender !== win.webContents || typeof enabled !== 'boolean') return;
    win.setFocusable(lab || enabled);
    // Match the Legacy Pet handshake: revoke key focus eligibility without
    // blur(), which orders the native macOS window out and then behind others.
    if (enabled) {
      if (!win.isVisible()) win.show();
      if (process.platform !== 'darwin' || lab) app.focus({ steal: true });
      win.focus(); win.webContents.focus();
    }
    if (!lab && process.platform === 'darwin') {
      win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
    }
    keepOnTop();
  });
  ipcMain.handle('copy', (event, text) => {
    if (event.sender !== win.webContents || typeof text !== 'string' || text.length > 100000) return false;
    clipboard.writeText(text);
    return true;
  });
  // The renderer can request plugin operations but never read the daemon's
  // management credential or choose an arbitrary URL/file/process to open.
  ipcMain.handle('plugins', async (event, operation: string, data: Record<string, unknown> = {}) => {
    if (event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame) throw new Error('无效的插件窗口');
    const operations = ['read', 'open', 'connect', 'cancel', 'reopen', 'disable', 'approval'];
    if (!operations.includes(operation) || !data || typeof data !== 'object' || Array.isArray(data)) throw new Error('无效的插件操作');
    const testPort = verification ? process.env.RESONANCE_PLUGIN_TEST_PORT : undefined;
    if (lab || (verification && (!testPort || testPort === '8006'))) throw new Error('此预览未连接插件服务');
    const port = testPort ?? process.env.JARVIS_INHERENT_BRIDGE_PORT ?? '8006';
    if (!/^\d{1,5}$/.test(port) || Number(port) > 65535) throw new Error('插件服务端口无效');
    const root = (verification ? process.env.RESONANCE_PLUGIN_TEST_ROOT : undefined) ?? process.env.JARVIS_RUNTIME_ROOT ?? path.join(homedir(), '.jarvis');
    let token: string;
    try { token = JSON.parse(await readFile(path.join(root, 'plugin-access.json'), 'utf8')).token; }
    catch { throw new Error('插件服务尚未就绪，请确认 Jarvis 后台已更新并启动'); }
    if (typeof token !== 'string' || !token) throw new Error('插件服务凭证无效');
    const body = JSON.stringify({ operation, data });
    if (body.length > 32768) throw new Error('插件请求过长');
    let response: Response;
    try {
      response = await fetch(`http://127.0.0.1:${port}/inherent/plugins${operation === 'read' ? '' : '/action'}`, {
        method: operation === 'read' ? 'GET' : 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: operation === 'read' ? undefined : body,
        signal: AbortSignal.timeout(15000),
      });
    } catch { throw new Error('暂时连不上 Jarvis，请稍后重试'); }
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(response.status === 401 ? '插件服务凭证已更新，请重试' : typeof result.detail === 'string' ? result.detail : '插件操作未完成，请重试');
    return result;
  });
  ipcMain.on('hide', event => { if (event.sender === win.webContents) win.hide(); });
  ipcMain.handle('open-codex', async (event, threadId) => {
    if (event.sender !== win.webContents || typeof threadId !== 'string'
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(threadId)) return false;
    // An OS handoff is not proof that the target conversation was displayed.
    if (verification || lab) return false;
    try { await shell.openExternal(`codex://threads/${threadId}`); return true; }
    catch { return false; }
  });
  ipcMain.handle('codex-titles', async (event, ids) => {
    if (event.sender !== win.webContents || !Array.isArray(ids) || ids.length > 10000 || !ids.every(id => typeof id === 'string')) return {};
    if (verification || lab) return {};
    if (Date.now() - codexTitlesAt > 10000) {
      try {
        const lines = await readFile(path.join(process.env.CODEX_HOME ?? path.join(homedir(), '.codex'), 'session_index.jsonl'), 'utf8');
        const titles: Record<string, string> = {};
        for (const line of lines.split('\n')) {
          try { const row = JSON.parse(line); if (typeof row.id === 'string' && typeof row.thread_name === 'string') titles[row.id] = row.thread_name; } catch { /* Partially appended index line. */ }
        }
        codexTitles = titles;
      } catch { /* Optional name index; the observed prompt is the fallback. */ }
      codexTitlesAt = Date.now();
    }
    return Object.fromEntries(ids.filter(id => Object.hasOwn(codexTitles, id)).map(id => [id, codexTitles[id]]));
  });
  ipcMain.on('drag', (event, payload) => {
    if (event.sender !== win.webContents || lab) return;
    const { phase, point } = payload ?? {};
    const validPoint = point && Number.isFinite(point.x) && Number.isFinite(point.y);
    if (phase === 'end') { if (validPoint) moveDrag(); endDrag(); return; }
    if (!validPoint || !win.isVisible()) return;
    if (phase === 'start' && !dragGesture) dragGesture = { origin: screen.getCursorScreenPoint(), bounds: win.getBounds(), moved: false };
    else if (phase === 'move') moveDrag();
  });
  win.on('hide', endDrag);
  win.webContents.on('render-process-gone', endDrag);
  ipcMain.on('passthrough', (event, enabled) => { if (event.sender === win.webContents && !lab && typeof enabled === 'boolean') win.setIgnoreMouseEvents(enabled, { forward: true }); });
  ipcMain.on('material', (event, payload) => {
    const owner = event.sender === win.webContents ? win : null;
    if (!owner || !material || !Array.isArray(payload?.rects)) return;
    const bounds = owner.getBounds();
    const rects = payload.rects.slice(0, 16).filter((r: Record<string, number>) =>
      r && ['x', 'y', 'width', 'height', 'radius'].every(k => Number.isFinite(r[k])) &&
      r.width > 0 && r.height > 0 && r.width <= bounds.width && r.height <= bounds.height);
    const strength = Number.isFinite(payload.strength) ? Math.min(1, Math.max(0, payload.strength)) : 1;
    material.update(owner.getNativeWindowHandle(), rects, strength);
  });
  tray = new Tray(nativeImage.createEmpty());
  tray.setTitle('J'); tray.setToolTip('Jarvis · 交互原型');
  const command = (value: string) => { restore(value === 'text' || value === 'settings'); win.webContents.send('command', value); };
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Jarvis · 交互原型（无录音）', enabled: false },
    { label: '显示胶囊', click: () => restore() }, { label: '开始语音演示', click: () => command('voice') },
    { label: '文字输入', click: () => command('text') }, { label: '键盘控制胶囊', click: () => restore(true) },
    { label: 'Dashboard…', click: openDashboard },
    { label: '插件…', click: () => command('plugins') },
    { label: '外观与提示音…', click: () => command('settings') },
    { label: '隐藏浮窗', click: () => win.hide() }, { type: 'separator' },
    { label: '退出 Jarvis', click: () => { quitting = true; app.quit(); } }
  ]));
  tray.on('click', () => restore());
  const shortcuts = [
    ['CommandOrControl+Shift+J', () => win.isVisible() ? win.hide() : restore()],
    ['CommandOrControl+Shift+K', () => command('text')],
    ['CommandOrControl+Shift+L', () => { restore(true); win.webContents.send('command', 'keyboard'); }]
  ] as const;
  for (const [key, action] of shortcuts) if (!globalShortcut.register(key, action)) console.warn(`Shortcut unavailable: ${key}`);
});
app.on('before-quit', () => { quitting = true; });
app.on('will-quit', () => { dragGesture = null; globalShortcut.unregisterAll(); });
app.on('window-all-closed', () => {});

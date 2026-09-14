import { app, BrowserWindow, Menu, Tray, nativeImage, ipcMain, screen, globalShortcut, session, clipboard } from 'electron';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
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
function openDashboard() {
  restore(true);
  win.webContents.send('command', 'dashboard');
}
let quitting = false;
let dragGesture: { origin: { x: number; y: number }; bounds: Electron.Rectangle; moved: boolean } | null = null;
function moveDrag(point: { x: number; y: number }) {
  if (!dragGesture || !win || win.isDestroyed()) return;
  const dx = point.x - dragGesture.origin.x, dy = point.y - dragGesture.origin.y;
  if (Math.hypot(dx, dy) < 4 && !dragGesture.moved) return;
  dragGesture.moved = true;
  win.setPosition(Math.round(dragGesture.bounds.x + dx), Math.round(dragGesture.bounds.y + dy));
}
function endDrag() {
  dragGesture = null;
  if (win && !win.isDestroyed()) {
    const b = win.getBounds();
    win.setBounds(clampBounds(b, screen.getDisplayMatching(b).workArea));
  }
}
function restore(focus = false) {
  if (!win || win.isDestroyed()) return;
  const bounds = win.getBounds();
  win.setBounds(clampBounds(bounds, screen.getDisplayMatching(bounds).workArea));
  win.setIgnoreMouseEvents(false);
  if (focus) { win.setFocusable(true); win.show(); win.focus(); }
  else win.showInactive();
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
    frame: lab, transparent: !lab, backgroundColor: lab ? '#151c19' : '#00000000', hasShadow: lab,
    resizable: lab, maximizable: lab, fullscreenable: lab, show: false, focusable: lab,
    alwaysOnTop: !lab, skipTaskbar: !lab, roundedCorners: false,
    webPreferences: { preload: path.join(here, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true }
  });
  // Ordinary floating windows can sit below other apps' panels and overlays.
  if (!lab) win.setAlwaysOnTop(true, 'screen-saver', process.platform === 'darwin' ? 1 : 0);
  if (!lab && process.platform === 'darwin') {
    app.dock?.hide();
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
  }
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  if (verification) win.webContents.setAudioMuted(true);
  win.webContents.on('will-navigate', event => event.preventDefault());
  // Lab and verification stay simulated; the desktop build talks to the daemon (same port env as the Swift card).
  win.loadFile(path.join(here, '../dist/index.html'), { query: dashboard ? { lab: '1', dashboard: '1' } : lab ? { lab: '1' } : verification ? {} : { port: process.env.JARVIS_INHERENT_BRIDGE_PORT ?? '8006' } });
  win.once('ready-to-show', () => restore(lab));
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
    const next = clampBounds({ ...b, height: Math.max(100, Math.min(560, Math.round(payload.height))) }, screen.getDisplayMatching(b).workArea);
    if (b.x !== next.x || b.y !== next.y || b.height !== next.height) win.setBounds(next);
  });
  ipcMain.handle('focus-input', (event, enabled) => {
    if (event.sender !== win.webContents || typeof enabled !== 'boolean') return;
    win.setFocusable(lab || enabled);
    if (enabled) { app.focus({ steal: true }); win.focus(); win.webContents.focus(); } else if (!lab) win.blur();
  });
  ipcMain.handle('copy', (event, text) => {
    if (event.sender !== win.webContents || typeof text !== 'string' || text.length > 100000) return false;
    clipboard.writeText(text);
    return true;
  });
  ipcMain.on('open-dashboard', event => { if (event.sender === win.webContents) openDashboard(); });
  ipcMain.on('hide', event => { if (event.sender === win.webContents) win.hide(); });
  ipcMain.on('drag', (event, payload) => {
    if (event.sender !== win.webContents || lab) return;
    const { phase, point } = payload ?? {};
    const validPoint = point && Number.isFinite(point.x) && Number.isFinite(point.y);
    if (phase === 'end') { if (validPoint) moveDrag(point); endDrag(); return; }
    if (!validPoint || !win.isVisible()) return;
    if (phase === 'start' && !dragGesture) dragGesture = { origin: point, bounds: win.getBounds(), moved: false };
    else if (phase === 'move') moveDrag(point);
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

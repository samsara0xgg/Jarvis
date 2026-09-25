import { app, BrowserWindow, Menu, Tray, nativeImage, ipcMain, screen, session } from 'electron';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
// Companion prototype: the black ball that lives beside the notch. Everything it
// shows is simulated; it never talks to the daemon, records audio, or plays speech.
const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const material = process.platform === 'darwin' ? require('../dist-native/material.node') : null;
type NotchScreen = { id: number; topInset: number; notchWidth: number };
app.setName('Jarvis Companion');
// Its own profile, so it runs beside the live Resonance and its single-instance lock.
app.setPath('userData', path.resolve(here, '../.electron-profile/companion'));
const locked = app.requestSingleInstanceLock();
if (!locked) app.quit();
const WIDTH = 640;
let win: BrowserWindow;
let tray: Tray;
// One companion, on the screen you are using: `current` is the display she lives on now.
let current: Electron.Display | null = null;
function target() {
  return screen.getAllDisplays().find(item => item.id === current?.id) ?? screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
}
function placement(display: Electron.Display) {
  const notches: NotchScreen[] = material?.screens() ?? [];
  const notch = notches.find(item => item.id === display.id && item.topInset > 0);
  // Without a notch she lives in a free-standing pill that hangs just below the menu bar.
  const topInset = notch ? Math.max(24, Math.round(notch.topInset)) : Math.max(32, Math.round(display.workArea.y - display.bounds.y));
  return { docked: false, topInset, notchWidth: notch ? notch.notchWidth : 0, surfaceWidth: WIDTH, compactWidth: 0, displayId: display.id };
}
function frame(): Electron.Rectangle { return material?.getFrame(win.getNativeWindowHandle()) ?? win.getBounds(); }
function place() {
  const display = current = target(), value = placement(display);
  const bounds = { x: Math.round(display.bounds.x + (display.bounds.width - WIDTH) / 2), y: display.bounds.y, width: WIDTH, height: Math.min(display.bounds.height, value.topInset + 560) };
  // A borderless panel may cover the menu bar only through AppKit, like the notch dock.
  if (material) material.setFrame(win.getNativeWindowHandle(), bounds); else win.setBounds(bounds);
  win.webContents.send('placement', value);
}
function keepOnTop() {
  win.setAlwaysOnTop(true, 'status');
  material?.setStationary(win.getNativeWindowHandle(), true);
}
if (locked) app.whenReady().then(() => {
  session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
  win = new BrowserWindow({ width: WIDTH, height: 600, type: process.platform === 'darwin' ? 'panel' : undefined,
    frame: false, transparent: true, backgroundColor: '#00000000', hasShadow: false, resizable: false, maximizable: false,
    fullscreenable: false, show: false, focusable: false, alwaysOnTop: true, skipTaskbar: true, roundedCorners: false,
    // AppKit pushes a window below the menu bar when it is shown on the main screen; this keeps the frame we set.
    enableLargerThanScreen: true,
    webPreferences: { preload: path.join(here, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true } });
  app.dock?.hide();
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
  // Transparent space passes clicks through; the renderer turns input on over its own shapes.
  win.setIgnoreMouseEvents(true, { forward: true });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', event => event.preventDefault());
  win.loadFile(path.join(here, '../dist/index.html'), { query: { companion: '1' } });
  win.webContents.on('did-finish-load', place);
  win.once('ready-to-show', () => { place(); win.showInactive(); keepOnTop(); });
  win.on('blur', () => setImmediate(() => { if (!win.isDestroyed()) keepOnTop(); }));
  screen.on('display-added', place); screen.on('display-removed', place); screen.on('display-metrics-changed', place);
  // The hardware cutout and click-through space get no reliable DOM pointer events,
  // so the renderer reads the cursor from here: approach, peek, gaze and hit testing.
  let last = '', pending: { id: number; since: number } | null = null, moving: ReturnType<typeof setTimeout> | undefined;
  // She follows the cursor to another screen once it has rested there briefly: the renderer
  // sinks her into this island first, answers display-ready, and only then the window moves.
  const move = () => { clearTimeout(moving); moving = undefined; pending = null; current = screen.getDisplayNearestPoint(screen.getCursorScreenPoint()); place(); };
  const cursor = setInterval(() => {
    if (win.isDestroyed() || !win.isVisible()) return;
    const point = screen.getCursorScreenPoint(), bounds = frame();
    const value = { x: point.x - bounds.x, y: point.y - bounds.y }, key = `${value.x},${value.y}`;
    if (key !== last) { last = key; win.webContents.send('cursor', value); }
    const under = screen.getDisplayNearestPoint(point);
    if (moving) return;
    if (!current || under.id === current.id) pending = null;
    else if (pending?.id !== under.id) pending = { id: under.id, since: Date.now() };
    else if (Date.now() - pending.since > 250) { win.webContents.send('display-leave'); moving = setTimeout(move, 900); }
  }, 16);
  win.on('closed', () => { clearInterval(cursor); clearTimeout(moving); });
  ipcMain.on('display-ready', event => { if (event.sender === win.webContents && moving) move(); });
  ipcMain.handle('placement', event => event.sender === win.webContents ? placement(target()) : null);
  ipcMain.on('passthrough', (event, enabled) => { if (event.sender === win.webContents && typeof enabled === 'boolean') win.setIgnoreMouseEvents(enabled, { forward: true }); });
  ipcMain.handle('focus-input', (event, enabled) => {
    if (event.sender !== win.webContents || typeof enabled !== 'boolean') return;
    // Key focus without activating the app, the same handshake as the capsule composer.
    win.setFocusable(enabled);
    if (enabled) { win.focus(); win.webContents.focus(); }
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
    keepOnTop();
  });
  ipcMain.on('material', (event, payload) => {
    if (event.sender !== win.webContents || !material || !Array.isArray(payload?.rects)) return;
    const rects = payload.rects.slice(0, 16).filter((r: Record<string, number>) => r && ['x', 'y', 'width', 'height', 'radius', 'opacity'].every(k => Number.isFinite(r[k])) && r.width > 0 && r.height > 0);
    material.update(win.getNativeWindowHandle(), rects, 1);
  });
  tray = new Tray(nativeImage.createEmpty());
  tray.setTitle('●'); tray.setToolTip('Jarvis 小球 · 交互原型');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Jarvis 小球 · 交互原型（全部模拟）', enabled: false },
    { label: '打开 Dashboard', click: () => win.webContents.send('command', 'dashboard') },
    { type: 'separator' },
    { label: '退出小球原型', click: () => app.quit() },
  ]));
});
app.on('window-all-closed', () => {});

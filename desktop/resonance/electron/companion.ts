import { app, BrowserWindow, Menu, Tray, nativeImage, ipcMain, screen, session } from 'electron';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { registerDaemonBridge } from './bridge.js';
// The companion: 星核, who lives beside the notch, with her Dashboard. She talks to the daemon on
// JARVIS_INHERENT_BRIDGE_PORT like the capsule does; the daemon owns mic and speaker, so she never
// records audio or plays speech herself. `--demo` runs her on the built-in demo data instead.
const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const material = process.platform === 'darwin' ? require('../dist-native/material.node') : null;
type NotchScreen = { id: number; topInset: number; notchWidth: number };
app.setName('Jarvis Companion');
// Its own profile, so it runs beside the live Resonance and its single-instance lock.
app.setPath('userData', path.resolve(here, '../.electron-profile/companion'));
const demo = process.argv.includes('--demo');
const locked = app.requestSingleInstanceLock();
if (!locked) app.quit();
const WIDTH = 640;
let win: BrowserWindow;
let tray: Tray;
// One companion, on the screen you are using: `current` is the display she lives on now. With `follow`
// off (her Settings › General) she stays on the main screen.
let current: Electron.Display | null = null, follow = true;
const home = () => follow ? screen.getDisplayNearestPoint(screen.getCursorScreenPoint()) : screen.getPrimaryDisplay();
function target() {
  return screen.getAllDisplays().find(item => item.id === current?.id) ?? home();
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
  const bounds = { x: Math.round(display.bounds.x + (display.bounds.width - WIDTH) / 2), y: display.bounds.y, width: WIDTH, height: Math.min(display.bounds.height, value.topInset + 690) };
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
    webPreferences: { preload: path.join(here, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true,
      // A notice sounds when it comes, not only after a click.
      autoplayPolicy: 'no-user-gesture-required' } });
  app.dock?.hide();
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
  // Transparent space passes clicks through; the renderer turns input on over its own shapes.
  win.setIgnoreMouseEvents(true, { forward: true });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', event => event.preventDefault());
  registerDaemonBridge(win, { lab: demo });
  win.loadFile(path.join(here, '../dist/index.html'), { query: demo ? { companion: '1' } : { companion: '1', port: process.env.JARVIS_INHERENT_BRIDGE_PORT ?? '8006' } });
  win.webContents.on('did-finish-load', place);
  win.once('ready-to-show', () => { place(); win.showInactive(); keepOnTop(); });
  win.on('blur', () => setImmediate(() => { if (!win.isDestroyed()) keepOnTop(); }));
  screen.on('display-added', place); screen.on('display-removed', place); screen.on('display-metrics-changed', place);
  // The hardware cutout and click-through space get no reliable DOM pointer events,
  // so the renderer reads the cursor from here: approach, peek, gaze and hit testing.
  let last = '', pending: { id: number; since: number } | null = null, moving: ReturnType<typeof setTimeout> | undefined;
  // She follows the cursor to another screen once it has rested there briefly: the renderer
  // sinks her into this island first, answers display-ready, and only then the window moves.
  const move = () => { clearTimeout(moving); moving = undefined; pending = null; current = home(); place(); };
  const leave = () => { win.webContents.send('display-leave'); moving = setTimeout(move, 900); };
  // ⌘ with the cursor in the menu bar row tucks her away on that side of the camera, so the menu bar items
  // and macOS's overflow arrow under her can be clicked; she comes back 3 s after ⌘ is let go.
  // Without a notch she is one pill, and all of her goes.
  let command = false, tucked = { left: false, right: false }, untuck: ReturnType<typeof setTimeout> | undefined;
  const tuck = (next: typeof tucked) => { tucked = next; win.webContents.send('tuck', next); };
  const cursor = setInterval(() => {
    if (win.isDestroyed() || !win.isVisible()) return;
    const point = screen.getCursorScreenPoint(), bounds = frame();
    const value = { x: point.x - bounds.x, y: point.y - bounds.y }, key = `${value.x},${value.y}`;
    if (key !== last) { last = key; win.webContents.send('cursor', value); }
    const down = !!material?.commandDown();
    if (down !== command) {
      command = down;
      const d = current, spot = d && placement(d);
      if (down && d && spot && point.x >= d.bounds.x && point.x < d.bounds.x + d.bounds.width && point.y >= d.bounds.y && point.y <= d.bounds.y + spot.topInset) {
        clearTimeout(untuck);
        const left = !spot.notchWidth || point.x < d.bounds.x + d.bounds.width / 2, right = !spot.notchWidth || !left;
        tuck({ left: tucked.left || left, right: tucked.right || right });
      } else if (!down && (tucked.left || tucked.right)) untuck = setTimeout(() => tuck({ left: false, right: false }), 3000);
    }
    const under = screen.getDisplayNearestPoint(point);
    if (moving || !follow) return;
    if (!current || under.id === current.id) pending = null;
    else if (pending?.id !== under.id) pending = { id: under.id, since: Date.now() };
    else if (Date.now() - pending.since > 250) leave();
  }, 16);
  win.on('closed', () => { clearInterval(cursor); clearTimeout(moving); clearTimeout(untuck); });
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
  tray.setTitle('●'); tray.setToolTip(demo ? 'Jarvis 小球 · 演示数据' : 'Jarvis 小球');
  // The renderer owns her skins and expressions and reports them; every item just sends a command back.
  const send = (command: string) => () => win.webContents.send('command', command);
  type MenuModel = { skins: { key: string; name: string; on: boolean }[]; auto: boolean; layout?: string; homeGlass?: boolean; marks?: string; follow?: boolean; lang?: string; exprs: { id: string; name: string }[] };
  // Her menu speaks the panel's language (Settings › General).
  const menu = (model: MenuModel) => { const t = (en: string, zh: string) => model.lang === 'zh' ? zh : en; tray.setContextMenu(Menu.buildFromTemplate([
    { label: demo ? t('Jarvis companion · demo data', 'Jarvis 小球 · 演示数据') : t('Jarvis companion', 'Jarvis 小球'), enabled: false },
    { label: t('Open Dashboard', '打开 Dashboard'), click: send('dashboard') },
    { label: t('Settings…', '设置…'), click: send('settings') },
    { label: t('Dashboard layout', 'Dashboard 布局'), submenu: [
      { label: t('Around her (one column)', '围着她（一列）'), type: 'radio', checked: model.layout !== 'grid', click: send('layout:around') },
      { label: t('Two columns (the old layout)', '两栏（原来的排法）'), type: 'radio', checked: model.layout === 'grid', click: send('layout:grid') },
    ] },
    { label: t('Agent marks', '状态点'), submenu: [
      { label: t('Spark', '星芒'), type: 'radio', checked: model.marks !== 'pixel', click: send('marks:spark') },
      { label: t('Pixel', '像素'), type: 'radio', checked: model.marks === 'pixel', click: send('marks:pixel') },
    ] },
    { type: 'separator' },
    { label: t('Skin', '皮肤'), enabled: model.skins.length > 0, submenu: [
      ...model.skins.map(skin => ({ label: String(skin.name), type: 'radio' as const, checked: !!skin.on, click: send(`skin:${skin.key}`) })),
      { type: 'separator' },
      { label: t('Change outfit by herself', '自己换装'), type: 'checkbox', checked: !!model.auto, click: send('auto') },
      { label: t('Change now', '现在换一套'), click: send('outing') },
      { type: 'separator' },
      { label: t('Show the glass ball at home', '在家露出玻璃球'), type: 'checkbox', checked: !!model.homeGlass, click: send('homeGlass') },
    ] },
    { label: t('Expressions', '看表情'), enabled: model.exprs.length > 0, submenu: model.exprs.map(x => ({ label: `${x.id} ${x.name}`, click: send(`expr:${x.id}`) })) },
    { type: 'separator' },
    { label: t('Quit the companion', '退出小球'), click: () => app.quit() },
  ])); };
  menu({ skins: [], auto: false, exprs: [] });
  ipcMain.on('companion-menu', (event, model: MenuModel) => {
    if (event.sender !== win.webContents || !Array.isArray(model?.skins) || !Array.isArray(model?.exprs)) return;
    menu(model);
    // Pinned to the main screen while she is on another one: she sinks here and comes up there.
    if (typeof model.follow === 'boolean' && model.follow !== follow) { follow = model.follow; if (!follow && current?.id !== screen.getPrimaryDisplay().id && !moving) leave(); }
  });
});
app.on('window-all-closed', () => {});

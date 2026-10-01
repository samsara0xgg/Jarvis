import { app, BrowserWindow, ipcMain, screen, session, shell, systemPreferences, desktopCapturer, Notification, globalShortcut } from 'electron';
import path from 'node:path';
import { existsSync } from 'node:fs';
import { userInfo } from 'node:os';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { execFile, spawn, type ChildProcess } from 'node:child_process';
import { daemonToken, registerDaemonBridge, sendDaemonKey } from './bridge.js';
import { startDaemon } from './daemon.js';
import { setupDictation } from './dictation.js';
import { AGENTS_PORT, setupAgents } from './agentsWindow.js';
import { setupDashboard } from './dashboardWindow.js';
import { demoBanner } from './demoBanner.js';
// The companion: 星核, who lives beside the notch, with her Dashboard. She talks to the daemon on
// JARVIS_INHERENT_BRIDGE_PORT like the capsule does; the daemon owns mic and speaker, so she never
// records audio or plays speech herself. `--demo` runs her on the built-in demo data instead.
const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const material = process.platform === 'darwin' ? require('../dist-native/material.node') : null;
type NotchScreen = { id: number; topInset: number; notchWidth: number };
app.setName('Jarvis Companion');
// Its own profile, so it runs beside the live Resonance and its single-instance lock. The installed
// app keeps the default one in Application Support: its bundle is read-only.
const demo = process.argv.includes('--demo');
if (!app.isPackaged) app.setPath('userData', demo && process.env.JARVIS_COMPANION_TEST_PROFILE
  ? path.resolve(process.env.JARVIS_COMPANION_TEST_PROFILE) : path.resolve(here, '../.electron-profile/companion'));
const locked = app.requestSingleInstanceLock();
if (!locked) app.quit();
const WIDTH = 640;
let win: BrowserWindow;
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
  // Without a notch she lives in a free-standing pill that hangs just below the menu bar. The notch's height is
  // not rounded: in a scaled mode it can be 28.5 pt, and rounding it leaves her island half a point below the notch.
  const topInset = notch ? Math.max(24, notch.topInset) : Math.max(32, Math.round(display.workArea.y - display.bounds.y));
  return { docked: false, topInset, notchWidth: notch ? notch.notchWidth : 0, surfaceWidth: WIDTH, compactWidth: 0, displayId: display.id };
}
// A line only when it changes: "front<TAB><focused terminal's title>", or "away" (Ghostty not in front, not
// running, or not allowed). The terminal's title, not the tab's: the tab's lags a switch by up to 0.6 s.
const GHOSTTY_WATCH = `set sep to character id 9
set prev to ""
repeat
  set cur to "away"
  if application "Ghostty" is running then
    tell application "Ghostty"
      try
        if frontmost then set cur to "front" & sep & (name of focused terminal of selected tab of front window)
      end try
    end tell
  end if
  if cur is not prev then
    log cur
    set prev to cur
  end if
  delay 0.4
end repeat`;
// Claude Code may put a mark before the name in a terminal's title, so a title ending in " name" matches too.
const GHOSTTY_JUMP = `on run argv
  set want to item 1 of argv
  set job to item 2 of argv
  tell application "Ghostty"
    repeat with w in windows
      repeat with b in tabs of w
        repeat with m in terminals of b
          set t to name of m
          if t is want or t ends with (" " & want) then
            focus m
            activate
            return "focused"
          end if
        end repeat
      end repeat
    end repeat
    if job is "" then return "none"
    set cfg to new surface configuration
    set initial input of cfg to "claude attach " & job & linefeed
    if (count of windows) > 0 then
      new tab in front window with configuration cfg
    else
      new window with configuration cfg
    end if
    activate
    return "attached"
  end tell
end run`;
function frame(): Electron.Rectangle { return material?.getFrame(win.getNativeWindowHandle()) ?? win.getBounds(); }
function place() {
  const display = current = target(), value = placement(display);
  const bounds = { x: Math.round(display.bounds.x + (display.bounds.width - WIDTH) / 2), y: display.bounds.y, width: WIDTH, height: Math.min(display.bounds.height, Math.ceil(value.topInset) + 690) };
  // A borderless panel may cover the menu bar only through AppKit, like the notch dock.
  if (material) material.setFrame(win.getNativeWindowHandle(), bounds); else win.setBounds(bounds);
  win.webContents.send('placement', value);
}
function keepOnTop() {
  win.setAlwaysOnTop(true, 'status');
  material?.setStationary(win.getNativeWindowHandle(), true);
}
let port = process.env.JARVIS_INHERENT_BRIDGE_PORT ?? '8006';
// Her first launch runs until the daemon has marked setup done; `--first-run` shows it anyway.
async function needsSetup() {
  if (process.argv.includes('--first-run')) return true;
  if (demo) return false;
  try {
    const r = await fetch(`http://127.0.0.1:${port}/inherent/setup`, { headers: { Authorization: `Bearer ${await daemonToken()}` }, signal: AbortSignal.timeout(3000) });
    return r.ok && (await r.json()).first_run === true;
  } catch { return false; }
}
const SETTINGS = 'x-apple.systempreferences:com.apple.preference.security?';
// Pages a window may open for Allen, asked for by name, never by URL: where keys are made, and System Settings panes.
const PAGES: Record<string, string> = {
  openai: 'https://platform.openai.com/api-keys',
  minimax: 'https://platform.minimax.io/user-center/basic-information/interface-key',
  tavily: 'https://app.tavily.com/',
  accessibility: `${SETTINGS}Privacy_Accessibility`,
};
const openPage = (page: unknown) => { if (typeof page === 'string' && Object.hasOwn(PAGES, page)) void shell.openExternal(PAGES[page]); };
// What macOS says about one permission; with `ask` it asks first. Screen Recording is turned on in
// System Settings and only counts after a relaunch; notifications answer through the first one: shown is a yes,
// failed a no (or a build macOS will not let notify, as the unsigned dev build), no answer in two minutes is only asked.
async function permission(kind: unknown, ask: unknown, note: unknown): Promise<string> {
  if (kind === 'mic') {
    const status = systemPreferences.getMediaAccessStatus('microphone');
    if (status === 'granted') return 'ok';
    if (!ask) return '';
    if (status === 'not-determined') return await systemPreferences.askForMediaAccess('microphone') ? 'ok' : 'later';
    void shell.openExternal(`${SETTINGS}Privacy_Microphone`);
    return 'later';
  }
  if (kind === 'screen') {
    if (systemPreferences.getMediaAccessStatus('screen') === 'granted') return 'ok';
    if (!ask) return '';
    await desktopCapturer.getSources({ types: ['screen'], thumbnailSize: { width: 0, height: 0 } }).catch(() => []);
    void shell.openExternal(`${SETTINGS}Privacy_ScreenCapture`);
    return 'relaunch';
  }
  if (kind === 'auto' && ask) {
    // Asking to control the terminal is the only way to learn the answer; -1743 is macOS saying no.
    const term = existsSync('/Applications/Ghostty.app') ? 'Ghostty' : 'Terminal';
    return new Promise(done => execFile('osascript', ['-e', `tell application "${term}" to count windows`], { timeout: 120000 },
      (_err, _out, err) => done(/-1743/.test(String(err)) ? 'later' : 'ok')));
  }
  if (kind === 'notify' && ask && Array.isArray(note)) {
    const n = new Notification({ title: String(note[0]), body: String(note[1]) });
    return new Promise(done => {
      // The timer holds the notification, so it is not collected before macOS answers.
      const t = setTimeout(() => { void n; done('asked'); }, 120000);
      n.once('show', () => { clearTimeout(t); done('ok'); });
      n.once('failed', (_e, error) => { clearTimeout(t); console.error(`notify: macOS refused the notification (${error})`); done('later'); });
      n.show();
    });
  }
  return '';
}
// The full-screen first launch on the screen under the cursor: her sky opens where 打开 was clicked,
// she moves in beside the notch and walks through setup. When she has said hello it hands over.
function firstRun() {
  const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint()), at = screen.getCursorScreenPoint();
  const fr = new BrowserWindow({ ...display.bounds, frame: false, transparent: true, backgroundColor: '#00000000', hasShadow: false,
    resizable: false, movable: false, minimizable: false, maximizable: false, fullscreenable: false, show: false, skipTaskbar: true,
    roundedCorners: false, enableLargerThanScreen: true, alwaysOnTop: true,
    webPreferences: { preload: path.join(here, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true, autoplayPolicy: 'no-user-gesture-required' } });
  // Above the menu bar and the Dock, on every Space, like the notch dock. While another app has you (a macOS
  // permission prompt, System Settings, the browser for a sign-in) it comes first; a click back on her sky covers the screen again.
  fr.setAlwaysOnTop(true, 'screen-saver');
  fr.on('blur', () => fr.setAlwaysOnTop(false));
  fr.on('focus', () => fr.setAlwaysOnTop(true, 'screen-saver'));
  fr.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
  if (material) material.setFrame(fr.getNativeWindowHandle(), display.bounds);
  fr.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  fr.webContents.on('will-navigate', event => event.preventDefault());
  sendDaemonKey(fr.webContents.session);
  const mine = (event: Electron.IpcMainEvent | Electron.IpcMainInvokeEvent) => event.sender === fr.webContents;
  ipcMain.handle('first-run-info', async event => {
    if (!mine(event)) return null;
    const spot = placement(display);
    const full = await new Promise<string>(done => execFile('id', ['-F'], (err, out) => done(err ? '' : out.trim())));
    return { top: spot.topInset, notch: spot.notchWidth, cursor: [at.x - display.bounds.x, at.y - display.bounds.y], port,
      name: (full || userInfo().username).split(/\s+/)[0], lang: app.getPreferredSystemLanguages()[0]?.startsWith('zh') ? 'zh' : 'en' };
  });
  ipcMain.handle('first-run-permission', (event, kind, ask, note) => mine(event) ? permission(kind, ask, note) : '');
  ipcMain.on('first-run-open', (event, page) => { if (mine(event)) openPage(page); });
  ipcMain.on('first-run-passthrough', (event, on) => { if (mine(event) && typeof on === 'boolean') fr.setIgnoreMouseEvents(on, { forward: true }); });
  // Setup is not marked done, so the next launch starts the first run again.
  ipcMain.on('first-run-quit', event => { if (mine(event)) app.quit(); });
  // The companion comes up under her last frame, then the first launch closes.
  ipcMain.once('first-run-done', () => companion(() => setTimeout(() => fr.destroy(), 400)));
  fr.loadFile(path.join(here, '../dist/firstrun.html'));
  fr.once('ready-to-show', () => { fr.show(); app.focus({ steal: true }); fr.focus(); });
}
if (locked) app.whenReady().then(async () => {
  session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
  app.dock?.hide();
  if (app.isPackaged && !demo) process.env.JARVIS_INHERENT_BRIDGE_PORT = port = await startDaemon();
  if (await needsSetup()) firstRun(); else companion();
});
function companion(shown?: () => void) {
  win = new BrowserWindow({ width: WIDTH, height: 600, type: process.platform === 'darwin' ? 'panel' : undefined,
    frame: false, transparent: true, backgroundColor: '#00000000', hasShadow: false, resizable: false, maximizable: false,
    fullscreenable: false, show: false, focusable: false, alwaysOnTop: true, skipTaskbar: true, roundedCorners: false,
    // AppKit pushes a window below the menu bar when it is shown on the main screen; this keeps the frame we set.
    enableLargerThanScreen: true,
    webPreferences: { preload: path.join(here, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true,
      // A notice sounds when it comes, not only after a click.
      autoplayPolicy: 'no-user-gesture-required' } });
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
  // Transparent space passes clicks through; the renderer turns input on over its own shapes.
  win.setIgnoreMouseEvents(true, { forward: true });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', event => event.preventDefault());
  const dashboard = setupDashboard({ parent: win, preload: path.join(here, 'preload.cjs'), page: path.join(here, '../dist/index.html'), demo, port,
    mouseDown: material?.leftMouseDown ? () => material.leftMouseDown() : undefined,
    onAttach: display => { clearTimeout(moving); moving = undefined; pending = null; current = display; place(); } });
  registerDaemonBridge(win, { lab: demo, trustedWindows: dashboard.windows });
  const mine = (event: Electron.IpcMainEvent | Electron.IpcMainInvokeEvent) => dashboard.senderWindow(event);
  // ADR 0058: the right ⌥ dictates at the text caret; she goes there from the notch. Live only: it needs the daemon's mic.
  const dictation = demo || !material ? null : setupDictation({ companion: win, native: material, nativePath: path.join(here, '../dist-native/material.node'), preload: path.join(here, 'preload.cjs'),
    page: path.join(here, '../dist/dictation.html'), port, topInset: display => placement(display).topInset, open: openPage });
  // `agents`: the agent host's port where this process runs the host, so the notch follows Startrail's sessions too
  // (src/startrail.ts); the key rides on its requests from sendDaemonKey, never in the page.
  win.loadFile(path.join(here, '../dist/index.html'), { query: demo ? { companion: '1' } : { companion: '1', port: process.env.JARVIS_INHERENT_BRIDGE_PORT ?? '8006', ...app.isPackaged ? { packaged: '1' } : { agents: AGENTS_PORT } } });
  win.webContents.on('did-finish-load', place);
  win.once('ready-to-show', () => { place(); win.showInactive(); keepOnTop(); shown?.(); if (demo) void demoBanner(); });
  win.on('blur', () => setImmediate(() => { if (!win.isDestroyed()) keepOnTop(); }));
  screen.on('display-added', place); screen.on('display-removed', place); screen.on('display-metrics-changed', place);
  // The hardware cutout and click-through space get no reliable DOM pointer events,
  // so the renderer reads the cursor from here: approach, peek, gaze and hit testing.
  let last = '', pending: { id: number; since: number } | null = null, moving: ReturnType<typeof setTimeout> | undefined;
  // She follows the cursor to another screen once it has rested there briefly: the renderer
  // sinks her into this island first, answers display-ready, and only then the window moves.
  const move = () => { clearTimeout(moving); moving = undefined; pending = null; current = home(); place(); };
  const leave = () => { win.webContents.send('display-leave'); moving = setTimeout(move, 900); };
  // ⌘ with the cursor in the menu bar row hides all of her at once: the island, the marks and whatever hangs below
  // them (a notice, a card, the Dashboard), so the menu bar items and macOS's overflow arrow under her can be
  // clicked; she comes back 3 s after ⌘ is let go. Hidden, the window takes no clicks and the page is told the
  // cursor went far away, so what hover opened closes and nothing opens behind the glass.
  let command = false, tucked = false, pass = true, untuck: ReturnType<typeof setTimeout> | undefined;
  const tuck = (on: boolean) => {
    tucked = on; last = '';
    win.setIgnoreMouseEvents(on || pass, { forward: true });
    if (on) win.webContents.send('cursor', { x: -1e4, y: -1e4 });
  };
  const cursor = setInterval(() => {
    const point = screen.getCursorScreenPoint();
    dictation?.tick(point);
    if (win.isDestroyed() || !win.isVisible()) return;
    const bounds = frame();
    const value = { x: point.x - bounds.x, y: point.y - bounds.y }, key = `${value.x},${value.y}`;
    if (key !== last && !tucked) { last = key; win.webContents.send('cursor', value); }
    // She fades out in about 130 ms and back in about 200 ms.
    const alpha = win.getOpacity();
    if (alpha !== (tucked ? 0 : 1)) win.setOpacity(tucked ? Math.max(0, alpha - .12) : Math.min(1, alpha + .08));
    const down = !!material?.commandDown();
    if (down !== command) {
      command = down;
      const d = current, spot = d && placement(d);
      if (down && d && spot && point.x >= d.bounds.x && point.x < d.bounds.x + d.bounds.width && point.y >= d.bounds.y && point.y <= d.bounds.y + spot.topInset) {
        clearTimeout(untuck);
        if (!tucked) tuck(true);
      } else if (!down && tucked) untuck = setTimeout(() => tuck(false), 3000);
    }
    const under = screen.getDisplayNearestPoint(point);
    if (moving || !follow) return;
    if (!current || under.id === current.id) pending = null;
    else if (pending?.id !== under.id) pending = { id: under.id, since: Date.now() };
    else if (Date.now() - pending.since > 250) leave();
  }, 16);
  win.on('closed', () => { clearInterval(cursor); clearTimeout(moving); clearTimeout(untuck); dictation?.close(); });
  ipcMain.on('display-ready', event => { if (mine(event) === win && moving) move(); });
  ipcMain.handle('placement', event => { const sender = mine(event); return sender === win ? placement(target()) : sender ? dashboard.placement() : null; });
  // Settings › Advanced › Quit, the installed app's only way out (no Dock icon); the daemon stops with it (daemon.ts).
  ipcMain.on('quit', event => { if (mine(event) && app.isPackaged) app.quit(); });
  ipcMain.on('passthrough', (event, enabled) => { if (mine(event) === win && typeof enabled === 'boolean') { pass = enabled; win.setIgnoreMouseEvents(enabled || tucked, { forward: true }); } });
  ipcMain.handle('focus-input', (event, enabled) => {
    const sender = mine(event);
    if (!sender || typeof enabled !== 'boolean') return;
    if (sender !== win) { if (enabled) { sender.focus(); sender.webContents.focus(); } return; }
    // Key focus without activating the app, the same handshake as the capsule composer.
    win.setFocusable(enabled);
    if (enabled) { win.focus(); win.webContents.focus(); }
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
    keepOnTop();
  });
  ipcMain.on('material', (event, payload) => {
    const sender = mine(event);
    if (!sender || !material || !Array.isArray(payload?.rects)) return;
    const rects = payload.rects.slice(0, 16).filter((r: Record<string, number>) => r && ['x', 'y', 'width', 'height', 'radius', 'opacity'].every(k => Number.isFinite(r[k])) && r.width > 0 && r.height > 0);
    material.update(sender.getNativeWindowHandle(), rects, 1);
  });
  // ADR 0057: which Claude session Allen is looking at. One long-lived script reads Ghostty's front terminal
  // every 0.4 s while the renderer asks, and reports only changes; Ghostty is never launched for it.
  let watcher: ChildProcess | null = null, watched = '';
  const watching = new Set<number>();
  const watchedContents = new WeakSet<Electron.WebContents>();
  const report = (line: string) => { watched = line; const [state, ...title] = line.split('\t'); for (const w of dashboard.windows()) w.webContents.send('ghostty', { front: state === 'front', title: title.join('\t') }); };
  ipcMain.on('ghostty-watch', (event, on) => {
    if (!mine(event) || demo || typeof on !== 'boolean') return;
    if (!on) { watching.delete(event.sender.id); if (!watching.size) { watcher?.kill(); watcher = null; } return; }
    watching.add(event.sender.id);
    if (!watchedContents.has(event.sender)) {
      const id = event.sender.id;
      watchedContents.add(event.sender);
      event.sender.once('destroyed', () => { watching.delete(id); if (!watching.size) { watcher?.kill(); watcher = null; } });
    }
    if (watcher) { if (watched) report(watched); return; }
    const child = watcher = spawn('/usr/bin/osascript', ['-e', GHOSTTY_WATCH], { stdio: ['ignore', 'ignore', 'pipe'] });
    let buffer = '';
    child.stderr!.setEncoding('utf8').on('data', (chunk: string) => {
      buffer += chunk;
      for (let at = buffer.indexOf('\n'); at >= 0; at = buffer.indexOf('\n')) { report(buffer.slice(0, at)); buffer = buffer.slice(at + 1); }
    });
    child.on('exit', () => { if (watcher === child) { watcher = null; watched = ''; } });
  });
  app.on('will-quit', () => watcher?.kill());
  // Go to a session: its Ghostty terminal if one shows it, else a new tab attaching the background job.
  ipcMain.handle('ghostty-jump', (event, title, job) => new Promise<boolean>(resolve => {
    if (!mine(event) || demo || typeof title !== 'string' || !title.trim() || title.length > 300
      || typeof job !== 'string' || !/^([0-9a-f]{8})?$/.test(job)) { resolve(false); return; }
    execFile('/usr/bin/osascript', ['-e', GHOSTTY_JUMP, title.trim(), job], { timeout: 8000 }, (error, stdout) => resolve(!error && stdout.trim() !== 'none'));
  }));
  // ADR 0073: the Agents window, from the Dashboard's Agents page. Live only: its sessions are real. Not in the
  // installed app yet: it runs on Allen's own subscription.
  const agentsWindow = !demo && !app.isPackaged ? setupAgents({ preload: path.join(here, 'preload.cjs'), page: path.join(here, '../dist/agents.html'), host: path.join(here, 'agents/host.js'), trustedWindows: dashboard.windows }) : null;
  // A landing that restarted this companion comes back to its window (ADR 0085).
  agentsWindow?.reopen();
  // Spec §15.3: ⌥Tab opens the island's list of agent sessions for the keys, and closes it again.
  if (!demo && !globalShortcut.register('Alt+Tab', () => { if (!agentsWindow?.next()) win.webContents.send('command', 'agent-keys'); })) console.warn('Shortcut unavailable: Alt+Tab');
  app.on('will-quit', () => globalShortcut.unregister('Alt+Tab'));
  // Her Settings that act in this process: the right-⌥ dictation, its language, and which screen she lives on.
  ipcMain.on('companion-settings', (event, settings: { follow?: boolean; lang?: string; dictation?: boolean }) => {
    if (!mine(event) || typeof settings !== 'object' || !settings) return;
    dictation?.language(settings.lang);
    dictation?.enabled(settings.dictation !== false);
    // Pinned to the main screen while she is on another one: she sinks here and comes up there.
    if (typeof settings.follow === 'boolean' && settings.follow !== follow) { follow = settings.follow; if (!follow && current?.id !== screen.getPrimaryDisplay().id && !moving) leave(); }
  });
}
app.on('window-all-closed', () => {});

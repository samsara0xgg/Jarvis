import { app, BrowserWindow, clipboard, globalShortcut, ipcMain, screen } from 'electron';
import { execFile } from 'node:child_process';
import path from 'node:path';
// ADR 0058: dictation, Jarvis's small typing tool. A clean tap of the right ⌥ starts it and another finishes it;
// Esc cancels; Return, while it listens, finishes it and sends the message once the words are in (ADR 0175). She leaves the notch for the text caret: a window over the caret's screen draws her there
// (src/dictation.ts), the daemon records, hears and polishes, and the words are pasted where the caret is.
// Typlus keeps F5 and the right ⌘, so both tools can run side by side.
type Box = { x: number; y: number; width: number; height: number };
type Caret = { app: string; trusted: boolean; window?: string; selected?: string; before?: string; caret?: Box; lineRight?: number; element?: Box };
type Native = { rightOption(): { down: boolean; others: boolean; keyIdle: number }; caret(): Caret; accessibility(prompt: boolean): boolean; paste(): boolean; pressReturn(): void;
  pasteTarget(): 'ok' | 'blind' | 'elsewhere' | 'lost';
  setFrame(handle: Buffer, bounds: Box): unknown };
// A tap is shorter than this, alone, and no key goes down while it is held.
const TAP_S = .5;
// 言字 SEND_SETTLE_S: the app reads the pasteboard a moment after the ⌘V; Return waits for it.
const SEND_SETTLE_S = .1;

// Under its LaunchAgent macOS counts Accessibility against the launcher, node: that is the row to turn on.
const AGENT = process.env.XPC_SERVICE_NAME === 'com.allen.jarvis.resonance';
const GRANTEE = AGENT ? 'node' : '';

export function setupDictation({ companion, native, nativePath, preload, page, port, topInset, open }: {
  companion: BrowserWindow; native: Native; nativePath: string; preload: string; page: string; port: string;
  topInset: (display: Electron.Display) => number; open: (page: string) => void;
}) {
  const overlay = new BrowserWindow({ type: 'panel', frame: false, transparent: true, backgroundColor: '#00000000', hasShadow: false,
    resizable: false, movable: false, minimizable: false, maximizable: false, fullscreenable: false, focusable: false, show: false,
    skipTaskbar: true, roundedCorners: false, enableLargerThanScreen: true, alwaysOnTop: true,
    webPreferences: { preload, contextIsolation: true, nodeIntegration: false, sandbox: true } });
  overlay.setAlwaysOnTop(true, 'status');
  overlay.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
  overlay.setIgnoreMouseEvents(true, { forward: true });
  overlay.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  overlay.webContents.on('will-navigate', event => event.preventDefault());
  overlay.loadFile(page);
  // `on`: Settings › General › Dictation; off, a tap starts nothing (one already running still finishes).
  let busy = false, on = true, asked = false, skin = 'glass', lang = 'zh', origin = { x: 0, y: 0 };
  let option = { down: false, at: 0, clean: false };
  const mine = (event: Electron.IpcMainEvent | Electron.IpcMainInvokeEvent) => event.sender === overlay.webContents;
  // Return is the app's again before its own Return is posted, and for her card's box.
  const releaseReturn = () => globalShortcut.unregister('Return');
  const cancel = () => { releaseReturn(); overlay.webContents.send('dictation-cancel'); };

  // A grant made while she runs counts only in a fresh process; this one keeps the answer it started with. Once the
  // Accessibility pane is opened, a child asks every 2 s for five minutes, and when the switch is on she restarts
  // between dictations: launchd brings her back, or she relaunches herself outside it.
  let watching = false, granted = false;
  const restart = () => { if (!AGENT) app.relaunch(); app.exit(0); };
  function watchGrant() {
    if (watching) return;
    watching = true;
    const until = Date.now() + 5 * 60_000;
    const ask = () => execFile(process.execPath, ['-e', `console.log(require(${JSON.stringify(nativePath)}).accessibility(false))`],
      { env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' }, timeout: 5000 }, (error, stdout) => {
        if (!error && stdout.trim() === 'true') { granted = true; if (!busy) restart(); return; }
        if (Date.now() < until) setTimeout(ask, 2000); else watching = false;
      });
    ask();
  }

  function start() {
    const at = native.caret();
    // The first time without Accessibility macOS asks once; until then she copies instead of pasting.
    if (!at.trusted && !asked) { asked = true; native.accessibility(true); }
    const pointer = screen.getCursorScreenPoint(), anchor = at.caret ?? at.element ?? { ...pointer, width: 0, height: 0 };
    const display = screen.getDisplayNearestPoint({ x: Math.round(anchor.x), y: Math.round(anchor.y) }), o = display.bounds;
    origin = { x: o.x, y: o.y };
    const local = (r?: Box) => r ? { l: r.x - o.x, t: r.y - o.y, r: r.x + r.width - o.x, b: r.y + r.height - o.y } : null;
    // Over the menu bar too, like the notch, so a caret near the top still has room above it.
    native.setFrame(overlay.getNativeWindowHandle(), o);
    overlay.showInactive();
    overlay.webContents.send('dictation-start', {
      caret: local(at.caret), lineRight: at.lineRight === undefined ? null : at.lineRight - o.x, element: local(at.element),
      pointer: { x: pointer.x - o.x, y: pointer.y - o.y }, top: topInset(display), skin, lang, port, trusted: at.trusted, grantee: GRANTEE,
      context: { app: at.app, window: at.window ?? '', selected: at.selected ?? '', before: at.before ?? '' },
    });
    companion.webContents.send('dictation', 'out');
    if (!globalShortcut.isRegistered('Escape')) globalShortcut.register('Escape', cancel);
    // ADR 0175, as 言字 0.4.3: Return swallowed while it listens finishes the dictation and sends it.
    if (!globalShortcut.isRegistered('Return')) globalShortcut.register('Return', () => { releaseReturn(); overlay.webContents.send('dictation-finish', true); });
    busy = true;
  }
  const tap = () => { if (busy) { releaseReturn(); overlay.webContents.send('dictation-finish'); } else if (on) start(); };

  ipcMain.on('dictation-paste', (event, text, send) => {
    if (!mine(event) || typeof text !== 'string') return;
    // Left on the pasteboard afterwards, as Typlus does, to paste again elsewhere.
    clipboard.writeText(text);
    // Finished with Return: once the words are in, the same key goes to the app so the message is sent.
    if (native.paste() && send === true) setTimeout(() => native.pressReturn(), SEND_SETTLE_S * 1000);
  });
  // ADR 0110: just before she dives, whether the words can still go where the dictation started.
  ipcMain.handle('dictation-target', event => mine(event) ? native.pasteTarget() : 'lost');
  ipcMain.on('dictation-copy', (event, text) => { if (mine(event) && typeof text === 'string') clipboard.writeText(text); });
  ipcMain.on('dictation-home', (event, happy) => { if (mine(event)) companion.webContents.send('dictation', happy === true ? 'happy' : 'home'); });
  ipcMain.on('dictation-done', event => {
    if (!mine(event)) return;
    globalShortcut.unregister('Escape'); releaseReturn();
    overlay.setIgnoreMouseEvents(true, { forward: true });
    overlay.hide();
    busy = false;
    if (granted) restart();
  });
  ipcMain.on('dictation-open', (event, name) => {
    if (!mine(event) || typeof name !== 'string') return;
    open(name);
    if (name === 'accessibility') watchGrant();
  });
  // A tap while her last card or message is still up: that one goes, a new dictation starts.
  ipcMain.on('dictation-again', event => { if (mine(event)) start(); });
  ipcMain.on('dictation-passthrough', (event, on) => { if (mine(event) && typeof on === 'boolean') overlay.setIgnoreMouseEvents(on, { forward: true }); });
  // Fixing the words: the box takes the keyboard without activating Jarvis (the overlay is a non-activating panel),
  // and Esc is the box's own again. Letting go, a hide and an inactive show hand the keyboard back to the app
  // she pastes into before the ⌘V, which lands after her dive.
  ipcMain.on('dictation-focus', (event, on) => {
    if (!mine(event) || typeof on !== 'boolean') return;
    if (on) { globalShortcut.unregister('Escape'); releaseReturn(); overlay.setFocusable(true); overlay.focus(); overlay.webContents.focus(); return; }
    overlay.setFocusable(false); overlay.hide(); overlay.showInactive();
  });
  ipcMain.on('companion-skin', (event, value) => { if (event.sender === companion.webContents && typeof value === 'string') skin = value; });

  return {
    // Every 16 ms from the companion's cursor loop.
    tick(point: Electron.Point) {
      const key = native.rightOption();
      if (key.down && !option.down) option = { down: true, at: Date.now(), clean: !key.others };
      else if (key.down && key.others) option.clean = false;
      else if (!key.down && option.down) {
        const held = (Date.now() - option.at) / 1000;
        option.down = false;
        if (option.clean && held < TAP_S && key.keyIdle >= held - .02) tap();
      }
      if (busy) overlay.webContents.send('dictation-cursor', { x: point.x - origin.x, y: point.y - origin.y });
    },
    language(value: string | undefined) { if (value === 'zh' || value === 'en') lang = value; },
    enabled(value: boolean) { on = value; },
    close() { globalShortcut.unregister('Escape'); releaseReturn(); if (!overlay.isDestroyed()) overlay.destroy(); },
  };
}

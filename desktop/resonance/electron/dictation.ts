import { BrowserWindow, clipboard, globalShortcut, ipcMain, screen } from 'electron';
import path from 'node:path';
// ADR 0058: dictation, Jarvis's small typing tool. A clean tap of the right ⌥ starts it and another finishes it;
// Esc cancels. She leaves the notch for the text caret: a window over the caret's screen draws her there
// (src/dictation.ts), the daemon records, hears and polishes, and the words are pasted where the caret is.
// Typlus keeps F5 and the right ⌘, so both tools can run side by side.
type Box = { x: number; y: number; width: number; height: number };
type Caret = { app: string; trusted: boolean; window?: string; selected?: string; caret?: Box; lineRight?: number; element?: Box };
type Native = { rightOption(): { down: boolean; others: boolean; keyIdle: number }; caret(): Caret; accessibility(prompt: boolean): boolean; paste(): boolean;
  setFrame(handle: Buffer, bounds: Box): unknown };
// A tap is shorter than this, alone, and no key goes down while it is held.
const TAP_S = .5;

// Under its LaunchAgent macOS counts Accessibility against the launcher, node: that is the row to turn on.
const GRANTEE = process.env.XPC_SERVICE_NAME === 'com.allen.jarvis.resonance' ? 'node' : '';

export function setupDictation({ companion, native, preload, page, port, topInset, open }: {
  companion: BrowserWindow; native: Native; preload: string; page: string; port: string; topInset: (display: Electron.Display) => number;
  open: (page: string) => void;
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
  const mine = (event: Electron.IpcMainEvent) => event.sender === overlay.webContents;
  const cancel = () => overlay.webContents.send('dictation-cancel');

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
      context: { app: at.app, window: at.window ?? '', selected: at.selected ?? '' },
    });
    companion.webContents.send('dictation', 'out');
    if (!globalShortcut.isRegistered('Escape')) globalShortcut.register('Escape', cancel);
    busy = true;
  }
  const tap = () => { if (busy) overlay.webContents.send('dictation-finish'); else if (on) start(); };

  ipcMain.on('dictation-paste', (event, text) => {
    if (!mine(event) || typeof text !== 'string') return;
    // Left on the pasteboard afterwards, as Typlus does, to paste again elsewhere.
    clipboard.writeText(text);
    native.paste();
  });
  ipcMain.on('dictation-copy', (event, text) => { if (mine(event) && typeof text === 'string') clipboard.writeText(text); });
  ipcMain.on('dictation-home', (event, happy) => { if (mine(event)) companion.webContents.send('dictation', happy === true ? 'happy' : 'home'); });
  ipcMain.on('dictation-done', event => {
    if (!mine(event)) return;
    globalShortcut.unregister('Escape');
    overlay.setIgnoreMouseEvents(true, { forward: true });
    overlay.hide();
    busy = false;
  });
  ipcMain.on('dictation-open', (event, name) => { if (mine(event) && typeof name === 'string') open(name); });
  // A tap while her last card or message is still up: that one goes, a new dictation starts.
  ipcMain.on('dictation-again', event => { if (mine(event)) start(); });
  ipcMain.on('dictation-passthrough', (event, on) => { if (mine(event) && typeof on === 'boolean') overlay.setIgnoreMouseEvents(on, { forward: true }); });
  // Fixing the words: the box takes the keyboard without activating Jarvis (the overlay is a non-activating panel),
  // and Esc is the box's own again. Letting go, a hide and an inactive show hand the keyboard back to the app
  // she pastes into before the ⌘V, which lands after her dive.
  ipcMain.on('dictation-focus', (event, on) => {
    if (!mine(event) || typeof on !== 'boolean') return;
    if (on) { globalShortcut.unregister('Escape'); overlay.setFocusable(true); overlay.focus(); overlay.webContents.focus(); return; }
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
    close() { globalShortcut.unregister('Escape'); if (!overlay.isDestroyed()) overlay.destroy(); },
  };
}

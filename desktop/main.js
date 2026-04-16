// main.js — Jarvis desktop shell (Electron main process).
// Wraps the existing ui/web Live2D UI (http://localhost:8006) and adds Pet Mode.
//
// Architecture: single BrowserWindow with in-place mode switching.
//   Window Mode: bordered, opaque, resizable, normal z-order.
//   Pet Mode: transparent, frameless, always-on-top, click-through with
//             main-side cursor polling (macOS-correct — see spec §5.1).
//
// Reference: OLV src/main/window-manager.ts (ported verbatim in spirit).
// On macOS, `transparent` and `frame` cannot be changed at runtime, so the
// window is always created frameless/transparent and Window Mode compensates
// via backgroundColor + renderer CSS.

const { app, BrowserWindow, Tray, Menu, ipcMain, screen, nativeImage } = require('electron');
const path = require('path');
const { MenuManager } = require('./menu');

// ── Config ───────────────────────────────────────────────────────────────────

function sanitizeWebUrl(raw) {
  const DEFAULT = 'http://localhost:8006';
  if (!raw || raw === DEFAULT) return DEFAULT;
  try {
    const u = new URL(raw);
    if (u.protocol !== 'http:') throw new Error('protocol must be http:');
    if (!['localhost', '127.0.0.1'].includes(u.hostname)) throw new Error('hostname must be localhost or 127.0.0.1');
    return raw;
  } catch (e) {
    console.warn(`[main] Invalid JARVIS_WEB_URL (${raw}): ${e.message} — using default`);
    return DEFAULT;
  }
}

const JARVIS_WEB_URL = sanitizeWebUrl(process.env.JARVIS_WEB_URL);
const POLL_INTERVAL_MS = 16; // ~60fps per spec §5.1; drop to 33ms if CPU > 2% idle
const ICON_PATH = path.join(__dirname, 'build', 'icon.png');

function isSafeLocalUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(u.hostname);
  } catch { return false; }
}

// ── Global state ─────────────────────────────────────────────────────────────

/** @type {BrowserWindow | null} */
let win = null;
/** @type {Tray | null} */
let tray = null;
/** @type {MenuManager | null} */
let menuManager = null;
/** @type {NodeJS.Timeout | null} */
let pollId = null;
/** @type {'window' | 'pet'} */
let currentMode = 'window';
/** Latest known hit state from renderer — used to gate setIgnoreMouseEvents. */
let lastHit = false;
/** Saved bounds before entering Pet Mode, restored on exit. */
let windowedBounds = null;
/** Set to true inside app.on('before-quit') so close handler can allow destroy. */
let isQuitting = false;
/** Last cursor relative coords sent to renderer — used for polling dedup. */
let lastRelX = -1;
let lastRelY = -1;

// ── Window creation ──────────────────────────────────────────────────────────

function createWindow() {
  win = new BrowserWindow({
    width: 900,
    height: 670,
    show: false,
    // macOS-critical flags — cannot be changed at runtime, so set once here
    // and let mode switches modify backgroundColor/bounds/alwaysOnTop instead.
    transparent: true,
    frame: false,
    hasShadow: false,
    backgroundColor: '#FFFFFF', // Window Mode default; Pet Mode sets '#00000000'
    autoHideMenuBar: true,
    icon: ICON_PATH,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  win.once('ready-to-show', () => {
    win.show();
  });

  // Load the Jarvis web UI. If it fails (backend not running), log but don't block;
  // user can fix the backend and refresh, or the window simply shows an error page.
  win.loadURL(JARVIS_WEB_URL).catch((err) => {
    console.error(`[jarvis-desktop] Failed to load ${JARVIS_WEB_URL}:`, err.message);
    console.error('[jarvis-desktop] Ensure `python -m ui.web.server` is running on port 8006.');
  });

  win.webContents.on('did-fail-load', (_e, code, desc, url) => {
    console.error(`[jarvis-desktop] did-fail-load ${url}: ${code} ${desc}`);
  });

  // Navigation guards — only allow http://localhost|127.0.0.1. Externals open in
  // the user's default browser rather than hijacking the shell.
  win.webContents.on('will-navigate', (e, url) => {
    if (!isSafeLocalUrl(url)) {
      e.preventDefault();
      console.warn(`[main] Blocked navigation to ${url}`);
    }
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isSafeLocalUrl(url)) return { action: 'allow' };
    const { shell } = require('electron');
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // Close-to-tray: red traffic-light / Cmd-W hides the window instead of
  // destroying it, so the tray "Show" item keeps working. Real quit sets
  // isQuitting inside app.on('before-quit') and we fall through to destroy.
  win.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault();
      win.hide();
    }
  });

  // Track window destruction so IPC handlers don't blow up after close.
  win.on('closed', () => {
    stopCursorPolling();
    win = null;
  });

  return win;
}

// ── Tray ─────────────────────────────────────────────────────────────────────

function ensureTray() {
  if (tray) return tray;
  let icon;
  try {
    icon = nativeImage.createFromPath(ICON_PATH);
    if (icon.isEmpty()) throw new Error('icon empty');
    icon = icon.resize({ width: 18, height: 18 });
  } catch (err) {
    // Fall back to an empty 1x1 image — Electron still creates a clickable tray
    icon = nativeImage.createEmpty();
  }
  tray = new Tray(icon);
  tray.setToolTip('Jarvis (小月)');
  tray.setContextMenu(Menu.buildFromTemplate([
    {
      label: 'Show',
      click: () => {
        if (win === null || win.isDestroyed()) createWindow();
        if (win) { win.show(); win.focus(); }
      },
    },
    {
      label: 'Hide',
      click: () => { if (win && !win.isDestroyed()) win.hide(); },
    },
    { type: 'separator' },
    {
      label: 'Quit',
      click: () => { app.quit(); },
    },
  ]));
  tray.on('click', () => {
    if (win === null || win.isDestroyed()) {
      createWindow();
      if (win) { win.show(); win.focus(); }
      return;
    }
    if (win.isVisible()) win.hide();
    else { win.show(); win.focus(); }
  });
  return tray;
}

// ── Mode switching ───────────────────────────────────────────────────────────

function setMode(mode) {
  if (!win) return;
  if (mode !== 'window' && mode !== 'pet') return;
  if (mode === currentMode) return;

  currentMode = mode;
  if (menuManager) menuManager.setCurrentMode(mode);

  if (mode === 'pet') enterPetMode();
  else enterWindowMode();

  // Notify renderer so it can toggle body.pet-mode and hide chrome.
  win.webContents.send('mode-change', mode);
}

function enterPetMode() {
  if (!win) return;

  // Save current window bounds so we can restore them on exit.
  windowedBounds = win.getBounds();

  if (win.isFullScreen()) win.setFullScreen(false);

  win.setBackgroundColor('#00000000');
  win.setAlwaysOnTop(true, 'screen-saver');
  win.setResizable(false);
  win.setSkipTaskbar(true);

  // Span the virtual desktop across all displays so the model can be dragged
  // freely between monitors (spec §5.3).
  setPetBoundsToVirtualRect();

  if (process.platform === 'darwin') {
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  }

  // Start fully click-through. Cursor polling flips this on/off as mouse
  // enters/leaves the Live2D model hit region.
  lastHit = false;
  lastRelX = -1;
  lastRelY = -1;
  win.setIgnoreMouseEvents(true, { forward: false });

  startCursorPolling();
  registerDisplayListeners();
}

function enterWindowMode() {
  if (!win) return;

  stopCursorPolling();
  unregisterDisplayListeners();

  win.setAlwaysOnTop(false);
  win.setIgnoreMouseEvents(false);
  win.setSkipTaskbar(false);
  win.setResizable(true);
  win.setBackgroundColor('#FFFFFF');

  if (process.platform === 'darwin') {
    win.setVisibleOnAllWorkspaces(false, { visibleOnFullScreen: false });
  }

  // Restore saved bounds or fall back to a sane default centered on primary.
  if (windowedBounds) {
    win.setBounds(windowedBounds);
  } else {
    win.setSize(900, 670);
    win.center();
  }
}

// ── Multi-screen virtual rectangle (spec §5.3) ───────────────────────────────

function computeVirtualRect() {
  const displays = screen.getAllDisplays();
  const left = Math.min(...displays.map((d) => d.bounds.x));
  const top = Math.min(...displays.map((d) => d.bounds.y));
  const right = Math.max(...displays.map((d) => d.bounds.x + d.bounds.width));
  const bottom = Math.max(...displays.map((d) => d.bounds.y + d.bounds.height));
  return { x: left, y: top, width: right - left, height: bottom - top };
}

function setPetBoundsToVirtualRect() {
  if (!win) return;
  const rect = computeVirtualRect();
  win.setBounds(rect);
}

// Recompute bounds when displays change (only while in Pet Mode).
function onDisplayChange() {
  if (currentMode === 'pet') setPetBoundsToVirtualRect();
}

function registerDisplayListeners() {
  screen.on('display-added', onDisplayChange);
  screen.on('display-removed', onDisplayChange);
  screen.on('display-metrics-changed', onDisplayChange);
}

function unregisterDisplayListeners() {
  screen.removeListener('display-added', onDisplayChange);
  screen.removeListener('display-removed', onDisplayChange);
  screen.removeListener('display-metrics-changed', onDisplayChange);
}

// ── Cursor polling loop (CRITICAL — spec §5.1) ───────────────────────────────
//
// On macOS, setIgnoreMouseEvents(true) means the renderer gets zero mouse
// events, so renderer-side mousemove cannot drive hit-testing. We poll from
// main (~60fps), forward screen coords to renderer, get back a hit/miss
// boolean, and flip setIgnoreMouseEvents accordingly.

function startCursorPolling() {
  stopCursorPolling(); // idempotent
  pollId = setInterval(() => {
    if (!win || win.isDestroyed()) return;
    const cursor = screen.getCursorScreenPoint();
    const b = win.getBounds();
    // Cheap bounds check — skip IPC when cursor is off the virtual window
    // entirely.
    if (
      cursor.x < b.x || cursor.x >= b.x + b.width
      || cursor.y < b.y || cursor.y >= b.y + b.height
    ) {
      // If we were hit and now cursor left the window, flip back to ignore.
      if (lastHit) {
        win.setIgnoreMouseEvents(true, { forward: false });
        lastHit = false;
      }
      return;
    }
    const relX = cursor.x - b.x;
    const relY = cursor.y - b.y;
    // Dedup: skip IPC when cursor hasn't moved >= 1px on either axis. Halves
    // IPC traffic when the user is at rest but doesn't regress hit detection.
    if (Math.abs(relX - lastRelX) < 1 && Math.abs(relY - lastRelY) < 1) return;
    win.webContents.send('cursor-update', { x: relX, y: relY });
    lastRelX = relX;
    lastRelY = relY;
  }, POLL_INTERVAL_MS);
}

function stopCursorPolling() {
  if (pollId) {
    clearInterval(pollId);
    pollId = null;
  }
}

// ── IPC handlers ─────────────────────────────────────────────────────────────

ipcMain.on('hover-update', (_event, hit) => {
  if (typeof hit !== 'boolean') return;
  if (!win || win.isDestroyed()) return;
  if (currentMode !== 'pet') return;
  if (hit !== lastHit) {
    win.setIgnoreMouseEvents(!hit, { forward: false });
    lastHit = hit;
  }
});

ipcMain.on('set-mode', (_event, mode) => {
  if (typeof mode !== 'string' || !['window', 'pet'].includes(mode)) return;
  setMode(mode);
});

ipcMain.on('quit', () => {
  app.quit();
});

ipcMain.on('hide-to-tray', () => {
  if (!win) return;
  ensureTray();
  win.hide();
});

// ── App lifecycle ────────────────────────────────────────────────────────────

app.whenReady().then(() => {
  // Defense-in-depth CSP. `unsafe-inline` is required because ui/web uses inline
  // <script>/<style>; backend is local-only so blast radius is limited.
  const { session } = require('electron');
  session.defaultSession.webRequest.onHeadersReceived((details, cb) => {
    cb({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [
          "default-src 'self' http://localhost:8006; "
          + "script-src 'self' 'unsafe-inline' http://localhost:8006; "
          + "style-src 'self' 'unsafe-inline' http://localhost:8006; "
          + "img-src 'self' data: http://localhost:8006; "
          + "media-src 'self' data: http://localhost:8006; "
          + "connect-src 'self' http://localhost:8006 ws://localhost:8006;",
        ],
      },
    });
  });

  createWindow();

  menuManager = new MenuManager({
    onModeChange: (mode) => setMode(mode),
    onHide: () => { if (win) win.hide(); },
    onShow: () => { if (win) { win.show(); win.focus(); } },
    onQuit: () => app.quit(),
  });
  if (win) menuManager.attach(win.webContents);

  ensureTray();

  app.on('activate', () => {
    // macOS: re-open or un-hide on dock click.
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
    else if (win) { win.show(); win.focus(); }
  });
});

app.on('window-all-closed', () => {
  // macOS convention: stay alive in the dock/tray.
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  isQuitting = true;
  stopCursorPolling();
  unregisterDisplayListeners();
});

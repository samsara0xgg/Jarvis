// main.js — Jarvis desktop shell (Electron main process).
// Wraps the existing ui/web Live2D UI (http://localhost:8006) and adds Pet Mode.
//
// Architecture: single BrowserWindow with a WindowManager class that owns the
// two-phase mode switch (setOpacity handshake) ported from OLV's
// src/main/window-manager.ts. Renderer-driven hover reports (not cursor
// polling) drive click-through on macOS.
//
// Reference: Open-LLM-VTuber-Web/src/main/window-manager.ts. Flow:
//   setWindowMode(mode) → setOpacity(0) → partial setup →
//   webContents.send('pre-mode-changed', mode) →
//   renderer responds with 'renderer-ready-for-mode-change' →
//   (500ms setTimeout) continueSetWindowMode{Window|Pet}() → bounds/flags →
//   webContents.send('mode-changed', mode) →
//   renderer resizes PIXI canvas, responds with 'mode-change-rendered' →
//   setOpacity(1).

const { app, BrowserWindow, Tray, Menu, ipcMain, screen, nativeImage, globalShortcut } = require('electron');
const path = require('path');
const { MenuManager } = require('./menu');

const isMac = process.platform === 'darwin';

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
const ICON_PATH = path.join(__dirname, 'build', 'icon.png');

function isSafeLocalUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(u.hostname);
  } catch { return false; }
}

// ── WindowManager ────────────────────────────────────────────────────────────

class WindowManager {
  constructor() {
    /** @type {BrowserWindow | null} */
    this.window = null;
    /** @type {{x:number,y:number,width:number,height:number} | null} */
    this.windowedBounds = null;
    /** @type {Set<string>} */
    this.hoveringComponents = new Set();
    /** @type {'window' | 'pet'} */
    this.currentMode = 'pet';  // 启动即 Pet mode
  }

  createWindow() {
    // 直接以 Pet mode 设置创建窗口 —— 无切换过程、无闪屏
    const target = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());

    this.window = new BrowserWindow({
      x: target.bounds.x,
      y: target.bounds.y,
      width: target.bounds.width,
      height: target.bounds.height,
      show: false,
      transparent: true,
      backgroundColor: '#00000000',  // Pet mode 透明；后续切到 window 会 setBackgroundColor('#ffffff')
      frame: false,
      hasShadow: false,
      autoHideMenuBar: true,
      alwaysOnTop: true,
      skipTaskbar: true,
      focusable: false,
      resizable: false,
      icon: ICON_PATH,
      webPreferences: {
        preload: path.join(__dirname, 'preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    });

    // Pet mode 特化（BrowserWindow options 没覆盖的部分）—— 等同
    // `continueSetWindowModePet` 的效果
    if (isMac) {
      this.window.setAlwaysOnTop(true, 'screen-saver');
      this.window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
      this.window.setIgnoreMouseEvents(true);
    } else {
      this.window.setIgnoreMouseEvents(true, { forward: true });
    }

    this.window.once('ready-to-show', () => {
      if (this.window) this.window.show();
    });

    // Load the Jarvis web UI.
    this.window.loadURL(JARVIS_WEB_URL).catch((err) => {
      console.error(`[jarvis-desktop] Failed to load ${JARVIS_WEB_URL}:`, err.message);
      console.error('[jarvis-desktop] Ensure `python -m ui.web.server` is running on port 8006.');
    });

    this.window.webContents.on('did-fail-load', (_e, code, desc, url) => {
      console.error(`[jarvis-desktop] did-fail-load ${url}: ${code} ${desc}`);
    });

    // Navigation guards — only allow http://localhost|127.0.0.1.
    this.window.webContents.on('will-navigate', (e, url) => {
      if (!isSafeLocalUrl(url)) {
        e.preventDefault();
        console.warn(`[main] Blocked navigation to ${url}`);
      }
    });
    this.window.webContents.setWindowOpenHandler(({ url }) => {
      if (isSafeLocalUrl(url)) return { action: 'allow' };
      const { shell } = require('electron');
      shell.openExternal(url);
      return { action: 'deny' };
    });

    // Close-to-tray.
    this.window.on('close', (e) => {
      if (!isQuitting) {
        e.preventDefault();
        this.window.hide();
      }
    });

    // 隐藏时主动收面板，保证下次 ⌘Space restore 时 toggle 语义干净
    // （否则 panel 的 isOpen=true 会让 toggle 直接走 hide 路径，产生"闪一下就消失"）
    this.window.on('hide', () => {
      if (this.window && !this.window.isDestroyed()) {
        this.window.webContents.send('close-input-panel');
      }
    });

    this.window.on('closed', () => {
      this.window = null;
    });

    return this.window;
  }

  getWindow() {
    return this.window;
  }

  getCurrentMode() {
    return this.currentMode;
  }

  // Phase 1 of mode switch: setOpacity(0), partial setup, notify renderer.
  setWindowMode(mode) {
    if (!this.window) return;
    if (mode !== 'window' && mode !== 'pet') return;
    if (mode === this.currentMode) return;

    this.currentMode = mode;
    this.window.setOpacity(0);

    if (mode === 'window') {
      this.setWindowModeWindow();
    } else {
      this.setWindowModePet();
    }
  }

  setWindowModeWindow() {
    if (!this.window) return;

    this.window.setAlwaysOnTop(false);
    this.window.setIgnoreMouseEvents(false);
    this.window.setSkipTaskbar(false);
    this.window.setResizable(true);
    this.window.setFocusable(true);
    this.window.setBackgroundColor('#ffffff');
    this.window.webContents.send('pre-mode-changed', 'window');
  }

  continueSetWindowModeWindow() {
    if (!this.window) return;

    if (this.windowedBounds) {
      this.window.setBounds(this.windowedBounds);
    } else {
      this.window.setSize(900, 670);
      this.window.center();
    }

    if (isMac) {
      this.window.setVisibleOnAllWorkspaces(false, { visibleOnFullScreen: false });
    }

    if (isMac) {
      this.window.setIgnoreMouseEvents(false);
    } else {
      this.window.setIgnoreMouseEvents(false, { forward: true });
    }

    // Clear hover tracking on exit from pet mode.
    this.hoveringComponents.clear();

    this.window.webContents.send('mode-changed', 'window');
  }

  setWindowModePet() {
    if (!this.window) return;

    this.windowedBounds = this.window.getBounds();

    if (this.window.isFullScreen()) {
      this.window.setFullScreen(false);
    }

    this.window.setBackgroundColor('#00000000');
    this.window.setAlwaysOnTop(true, 'screen-saver');
    this.window.setPosition(0, 0);
    this.window.webContents.send('pre-mode-changed', 'pet');
  }

  continueSetWindowModePet() {
    if (!this.window) return;

    // macOS 透明 NSWindow 无法可靠跨屏渲染（OLV 也栽在这里）：过去这里
    // setBounds 跨虚拟桌面 union，实际只在一块屏上画。改成每次只占当前屏，
    // 用户用 ⌘⇧→/⌘⇧← 在屏之间跳。
    const target = this._getTargetDisplay();
    this.window.setBounds(target.bounds);

    this.window.setResizable(false);
    this.window.setSkipTaskbar(true);
    this.window.setFocusable(false);

    if (isMac) {
      this.window.setIgnoreMouseEvents(true);
      this.window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    } else {
      this.window.setIgnoreMouseEvents(true, { forward: true });
    }

    this.window.webContents.send('mode-changed', 'pet');
  }

  /**
   * 挑一块目标显示器：优先用 setWindowModePet 里保存的 windowedBounds（进 pet
   * 前用户所在的位置），否则用当前光标所在屏。
   */
  _getTargetDisplay() {
    const ref = this.windowedBounds || this.window?.getBounds();
    if (ref) {
      const center = { x: ref.x + ref.width / 2, y: ref.y + ref.height / 2 };
      return screen.getDisplayNearestPoint(center);
    }
    return screen.getPrimaryDisplay();
  }

  /**
   * 把 pet 窗口跳到相邻显示器（direction: +1 右 / -1 左，按 x 坐标排序）。
   * 通知 renderer 居中模型到新窗口。
   */
  jumpToDisplay(direction) {
    if (!this.window || this.currentMode !== 'pet') return;
    const displays = screen.getAllDisplays();
    if (displays.length <= 1) return;
    const sorted = displays.slice().sort((a, b) => a.bounds.x - b.bounds.x);
    const cur = this._getTargetDisplay();
    const curIdx = sorted.findIndex((d) => d.id === cur.id);
    const nextIdx = (curIdx + direction + sorted.length) % sorted.length;
    const target = sorted[nextIdx];
    const oldBounds = this.window.getBounds();
    // 用透明度 handshake 盖住 macOS 原生跨屏转场的闪烁：
    // setOpacity(0) → setBounds → 通知 renderer 重绘 → renderer 发 display-rendered → setOpacity(1)
    this.window.setOpacity(0);
    this.window.setBounds(target.bounds);
    this.windowedBounds = null;
    this.window.webContents.send('display-changed', {
      oldBounds: { x: oldBounds.x, y: oldBounds.y, width: oldBounds.width, height: oldBounds.height },
      newBounds: { x: target.bounds.x, y: target.bounds.y, width: target.bounds.width, height: target.bounds.height },
    });
  }

  // Called on every hover report from the renderer while in Pet Mode.
  // Aggregates hover state across components (currently just 'live2d', but
  // future chat bubbles / message panels can report too).
  updateComponentHover(componentId, isHovering) {
    if (this.currentMode !== 'pet') return;
    if (!this.window) return;

    if (isHovering) {
      this.hoveringComponents.add(componentId);
    } else {
      this.hoveringComponents.delete(componentId);
    }

    const shouldIgnore = this.hoveringComponents.size === 0;
    if (isMac) {
      this.window.setIgnoreMouseEvents(shouldIgnore);
    } else {
      this.window.setIgnoreMouseEvents(shouldIgnore, { forward: true });
    }
    if (!shouldIgnore) {
      this.window.setFocusable(true);
    }
  }
}

// ── Global state ─────────────────────────────────────────────────────────────

const wm = new WindowManager();
/** @type {Tray | null} */
let tray = null;
/** @type {MenuManager | null} */
let menuManager = null;
/** Set to true inside app.on('before-quit') so close handler can allow destroy. */
let isQuitting = false;

// ── Tray ─────────────────────────────────────────────────────────────────────

function ensureTray() {
  if (tray) return tray;
  let icon;
  try {
    icon = nativeImage.createFromPath(ICON_PATH);
    if (icon.isEmpty()) throw new Error('icon empty');
    icon = icon.resize({ width: 18, height: 18 });
  } catch (err) {
    icon = nativeImage.createEmpty();
  }
  tray = new Tray(icon);
  tray.setToolTip('Jarvis (小月)');
  tray.setContextMenu(Menu.buildFromTemplate([
    {
      label: 'Show',
      click: () => {
        let win = wm.getWindow();
        if (win === null || win.isDestroyed()) {
          wm.createWindow();
          win = wm.getWindow();
        }
        if (win) { win.show(); win.focus(); }
      },
    },
    {
      label: 'Hide',
      click: () => {
        const win = wm.getWindow();
        if (win && !win.isDestroyed()) win.hide();
      },
    },
    { type: 'separator' },
    {
      label: 'Quit',
      click: () => { app.quit(); },
    },
  ]));
  tray.on('click', () => {
    let win = wm.getWindow();
    if (win === null || win.isDestroyed()) {
      wm.createWindow();
      win = wm.getWindow();
      if (win) { win.show(); win.focus(); }
      return;
    }
    if (win.isVisible()) win.hide();
    else { win.show(); win.focus(); }
  });
  return tray;
}

// ── IPC handlers ─────────────────────────────────────────────────────────────

// Phase 1 → Phase 2 handshake: renderer signals it has toggled its body class
// and pre-prepared canvas, we wait 500ms (matches OLV) then finalize.
ipcMain.on('renderer-ready-for-mode-change', () => {
  const mode = wm.getCurrentMode();
  setTimeout(() => {
    if (mode === 'pet') {
      wm.continueSetWindowModePet();
    } else {
      wm.continueSetWindowModeWindow();
    }
  }, 500);
});

// Phase 3: renderer finished the final PIXI resize — fade window back in.
ipcMain.on('mode-change-rendered', () => {
  const win = wm.getWindow();
  if (win && !win.isDestroyed()) win.setOpacity(1);
});

// Hover-based click-through (replaces the old cursor-polling loop).
ipcMain.on('update-component-hover', (_event, payload) => {
  if (!payload || typeof payload !== 'object') return;
  const { id, isHovering } = payload;
  if (typeof id !== 'string') return;
  if (typeof isHovering !== 'boolean') return;
  wm.updateComponentHover(id, isHovering);
});

ipcMain.on('set-mode', (_event, mode) => {
  if (typeof mode !== 'string' || !['window', 'pet'].includes(mode)) return;
  wm.setWindowMode(mode);
  if (menuManager) menuManager.setCurrentMode(mode);
});

// Renderer 通过 preload 同步 IPC 拿启动模式，用来立即应用 body.pet-mode CSS，
// 避免 window-mode UI 闪过。
ipcMain.on('get-initial-mode', (e) => {
  e.returnValue = wm ? wm.getCurrentMode() : 'pet';
});

// 跨屏跳转完成 renderer 重绘后通知 main 恢复透明度
ipcMain.on('display-rendered', () => {
  const win = wm.getWindow();
  if (win && !win.isDestroyed()) win.setOpacity(1);
});

ipcMain.on('quit', () => {
  app.quit();
});

ipcMain.on('hide-to-tray', () => {
  const win = wm.getWindow();
  if (!win) return;
  ensureTray();
  win.hide();
});

// ── Pet overlay IPC ──────────────────────────────────────────────────────────
// ⌘Space toggles the Liquid Glass command panel. Global shortcut is registered
// on whenReady below. Overlay-only behaviour — in Window mode we no-op.
//
// Focusable handshake: macOS Pet mode sets setFocusable(false). Without the
// overlay-shown/hidden handshake the input can't capture keystrokes. We only
// flip focusable while overlay is open, to preserve click-through when closed.

ipcMain.on('overlay-shown', () => {
  const win = wm.getWindow();
  if (!win || win.isDestroyed()) return;
  if (isMac && wm.getCurrentMode() === 'pet') {
    win.setFocusable(true);
    win.focus();
  }
});

ipcMain.on('overlay-hidden', () => {
  const win = wm.getWindow();
  if (!win || win.isDestroyed()) return;
  if (isMac && wm.getCurrentMode() === 'pet') {
    win.setFocusable(false);
  }
});

// Whitelisted local commands from the overlay. Action string is validated
// against a Set — anything outside the whitelist is dropped silently.
const VALID_OVERLAY_ACTIONS = new Set(['quit', 'hide', 'toWindow', 'toPet', 'switchModel']);
ipcMain.on('run-local-command', (_event, payload) => {
  if (!payload || typeof payload !== 'object') return;
  const { action, arg } = payload;
  if (typeof action !== 'string' || !VALID_OVERLAY_ACTIONS.has(action)) return;

  switch (action) {
    case 'quit':
      isQuitting = true;
      app.quit();
      break;
    case 'hide': {
      const win = wm.getWindow();
      if (win && !win.isDestroyed()) win.hide();
      break;
    }
    case 'toWindow':
      wm.setWindowMode('window');
      if (menuManager) menuManager.setCurrentMode('window');
      break;
    case 'toPet':
      wm.setWindowMode('pet');
      if (menuManager) menuManager.setCurrentMode('pet');
      break;
    case 'switchModel': {
      // Renderer owns the actual model switch via live2dManager — we relay.
      if (typeof arg !== 'string' || !arg) return;
      const win = wm.getWindow();
      if (win && !win.isDestroyed()) win.webContents.send('switch-model', arg);
      break;
    }
    default:
      break;
  }
});

// ── App lifecycle ────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  const { session } = require('electron');

  // Clear HTTP cache on every launch so edits to ui/web/ JS/CSS are picked up
  // without a hard reload. Cheap; the only cached content is localhost.
  try {
    await session.defaultSession.clearCache();
  } catch (e) {
    console.warn('[main] clearCache failed:', e.message);
  }

  // Defense-in-depth CSP. `unsafe-inline` is required because ui/web uses inline
  // <script>/<style>; `unsafe-eval` is required for PIXI/Live2D.
  session.defaultSession.webRequest.onHeadersReceived((details, cb) => {
    cb({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [
          "default-src 'self' http://localhost:8006; "
          + "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: http://localhost:8006; "
          + "worker-src 'self' blob: http://localhost:8006; "
          + "style-src 'self' 'unsafe-inline' http://localhost:8006; "
          + "img-src 'self' data: blob: http://localhost:8006; "
          + "media-src 'self' data: blob: http://localhost:8006; "
          + "connect-src 'self' http://localhost:8006 ws://localhost:8006;",
        ],
      },
    });
  });

  // Auto-grant mic/camera for the local Jarvis origin only.
  session.defaultSession.setPermissionRequestHandler((webContents, permission, cb) => {
    const url = webContents.getURL();
    const ok = /^https?:\/\/(localhost|127\.0\.0\.1):8006(\/|$)/.test(url)
      && ['media', 'microphone', 'audioCapture', 'clipboard-read', 'clipboard-sanitized-write'].includes(permission);
    cb(ok);
  });
  session.defaultSession.setPermissionCheckHandler((webContents, permission, requestingOrigin) => {
    return /^https?:\/\/(localhost|127\.0\.0\.1):8006/.test(requestingOrigin)
      && ['media', 'microphone', 'audioCapture'].includes(permission);
  });

  wm.createWindow();

  menuManager = new MenuManager({
    onModeChange: (mode) => {
      wm.setWindowMode(mode);
      if (menuManager) menuManager.setCurrentMode(mode);
    },
    onHide: () => {
      const win = wm.getWindow();
      if (win) win.hide();
    },
    onShow: () => {
      const win = wm.getWindow();
      if (win) { win.show(); win.focus(); }
    },
    onQuit: () => app.quit(),
  });
  const initialWin = wm.getWindow();
  if (initialWin) menuManager.attach(initialWin.webContents);

  ensureTray();

  // ⌘Space — 全能召唤键：
  //   1. 强制 Pet mode（如果在 Window）
  //   2. 强制可见（如果被 hide 到菜单栏）
  //   3. 把 Jarvis app 拉到前台（抢焦点）
  //   4. 切换命令面板
  const toggleOk = globalShortcut.register('CommandOrControl+Space', () => {
    const win = wm.getWindow();
    if (!win || win.isDestroyed()) return;
    if (wm.getCurrentMode() !== 'pet') {
      wm.setWindowMode('pet');
      if (menuManager) menuManager.setCurrentMode('pet');
    }
    if (!win.isVisible()) win.show();
    if (isMac && typeof app.focus === 'function') {
      app.focus({ steal: true });
    }
    win.webContents.send('toggle-input-panel');
  });
  if (!toggleOk) {
    console.warn('[main] Failed to register CommandOrControl+Space — another app may own it.');
  }

  // ⌘⇧P global shortcut — toggles between Window and Pet mode from anywhere.
  // Works even when the Jarvis window isn't focused.
  const modeToggleOk = globalShortcut.register('CommandOrControl+Shift+P', () => {
    const next = wm.getCurrentMode() === 'pet' ? 'window' : 'pet';
    wm.setWindowMode(next);
    if (menuManager) menuManager.setCurrentMode(next);
  });
  if (!modeToggleOk) {
    console.warn('[main] Failed to register CommandOrControl+Shift+P — another app may own it.');
  }

  // ⌘⇧→ / ⌘⇧← — 在显示器之间跳（Pet mode only）。macOS 透明窗口不能真跨屏，
  // 这是"跨屏"的替代方案。
  globalShortcut.register('CommandOrControl+Shift+Right', () => wm.jumpToDisplay(+1));
  globalShortcut.register('CommandOrControl+Shift+Left', () => wm.jumpToDisplay(-1));

  app.on('activate', () => {
    // macOS: re-open or un-hide on dock click.
    if (BrowserWindow.getAllWindows().length === 0) {
      wm.createWindow();
    } else {
      const win = wm.getWindow();
      if (win) { win.show(); win.focus(); }
    }
  });
});

app.on('window-all-closed', () => {
  // macOS convention: stay alive in the dock/tray.
  if (!isMac) app.quit();
});

app.on('before-quit', () => {
  isQuitting = true;
});

app.on('will-quit', () => {
  // Release global shortcuts so other apps can own ⌘Space again.
  globalShortcut.unregisterAll();
});

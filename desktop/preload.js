// preload.js — safe bridge between renderer (http://localhost:8006) and Electron main.
// Uses contextBridge with contextIsolation so the renderer never touches Node APIs.
//
// API mirrors OLV's two-phase mode-switch handshake:
//   Main → Renderer: pre-mode-changed, mode-changed
//   Renderer → Main: renderer-ready-for-mode-change, mode-change-rendered,
//                    update-component-hover, set-mode, quit, hide-to-tray

const { contextBridge, ipcRenderer } = require('electron');

// 同步拉启动模式，renderer 可在任何脚本运行时 (包括 index.html 内联) 读取。
// 用来在 DOM 首帧前给 body 加 pet-mode class，避免 window-mode UI 闪过。
const initialMode = (() => {
  try { return ipcRenderer.sendSync('get-initial-mode'); }
  catch { return 'pet'; }
})();

contextBridge.exposeInMainWorld('jarvis', {
  initialMode,

  // Mode change (two-phase)
  onPreModeChanged: (cb) => ipcRenderer.on('pre-mode-changed', (_, mode) => cb(mode)),
  onModeChanged: (cb) => ipcRenderer.on('mode-changed', (_, mode) => cb(mode)),
  sendModeReady: () => ipcRenderer.send('renderer-ready-for-mode-change'),
  sendModeRendered: () => ipcRenderer.send('mode-change-rendered'),

  // Click-through (hover-based)
  updateHover: (id, isHovering) => ipcRenderer.send('update-component-hover', { id, isHovering }),

  // Commands from menu / shortcuts
  setMode: (mode) => ipcRenderer.send('set-mode', mode),
  quit: () => ipcRenderer.send('quit'),
  hideToTray: () => ipcRenderer.send('hide-to-tray'),

  // ── Pet overlay (Liquid Glass command panel) ──────────────────────────────
  // ⌘Space global shortcut relay.
  onToggleInputPanel: (cb) => ipcRenderer.on('toggle-input-panel', () => cb()),
  // win.on('hide') 时 main 通知 renderer 收面板，保证 restore 后 toggle 状态干净
  onCloseInputPanel: (cb) => ipcRenderer.on('close-input-panel', () => cb()),
  // Model-switch command relay from main (panel says "模型 Haru" → main → here).
  onSwitchModel: (cb) => ipcRenderer.on('switch-model', (_, name) => cb(name)),
  // Display-jump relay — main 把 old/new bounds 传给 renderer，用来保持
  // 模型在新屏上的相对比例位置。
  onDisplayChanged: (cb) => ipcRenderer.on('display-changed', (_, payload) => cb(payload)),
  // Renderer 完成重绘后告诉 main，main 把窗口透明度恢复到 1（跨屏不闪）
  sendDisplayRendered: () => ipcRenderer.send('display-rendered'),
  // Focusable handshake — macOS Pet mode starts with setFocusable(false), so
  // the overlay must ask main to flip it back on while open.
  signalOverlayShown: () => ipcRenderer.send('overlay-shown'),
  signalOverlayHidden: () => ipcRenderer.send('overlay-hidden'),
  // Whitelisted local commands (quit / hide / toWindow / toPet / switchModel).
  // Main validates `action` against a Set — no code path here bypasses it.
  runLocalCommand: (action, arg) => ipcRenderer.send('run-local-command', { action, arg }),
});

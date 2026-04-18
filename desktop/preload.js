// preload.js — safe bridge between renderer (http://localhost:8006) and Electron main.
// Uses contextBridge with contextIsolation so the renderer never touches Node APIs.
//
// API mirrors OLV's two-phase mode-switch handshake:
//   Main → Renderer: pre-mode-changed, mode-changed
//   Renderer → Main: renderer-ready-for-mode-change, mode-change-rendered,
//                    update-component-hover, set-mode, quit, hide-to-tray

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('jarvis', {
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
  // Model-switch command relay from main (panel says "模型 Haru" → main → here).
  onSwitchModel: (cb) => ipcRenderer.on('switch-model', (_, name) => cb(name)),
  // Focusable handshake — macOS Pet mode starts with setFocusable(false), so
  // the overlay must ask main to flip it back on while open.
  signalOverlayShown: () => ipcRenderer.send('overlay-shown'),
  signalOverlayHidden: () => ipcRenderer.send('overlay-hidden'),
  // Whitelisted local commands (quit / hide / toWindow / toPet / switchModel).
  // Main validates `action` against a Set — no code path here bypasses it.
  runLocalCommand: (action, arg) => ipcRenderer.send('run-local-command', { action, arg }),
});

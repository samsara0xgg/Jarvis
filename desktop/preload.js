// preload.js — safe bridge between renderer (http://localhost:8006) and Electron main.
// Uses contextBridge with contextIsolation so the renderer never touches Node APIs.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('jarvis', {
  // ── Main → Renderer subscriptions ──────────────────────────────────────────
  onModeChange: (cb) => {
    ipcRenderer.on('mode-change', (_event, mode) => cb(mode));
  },
  onCursorUpdate: (cb) => {
    ipcRenderer.on('cursor-update', (_event, pos) => cb(pos));
  },

  // ── Renderer → Main commands ───────────────────────────────────────────────
  sendHover: (hit) => ipcRenderer.send('hover-update', !!hit),
  setMode: (mode) => ipcRenderer.send('set-mode', mode),
  quit: () => ipcRenderer.send('quit'),
  hideToTray: () => ipcRenderer.send('hide-to-tray'),
});

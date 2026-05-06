// inherent-preload.js — bridge between card.html renderer and inherent main.
//
// Renderer-side API exposed as window.cardAPI:
//   resize(h)        notify main of new content height
//   show()           ask main to make the window visible (post first render)
//   close()          ask main to hide the card (process stays alive)
//   fadeOut(ms)      lerp window opacity 1→0 then hide
//   cancelFade()     ask main to abort an in-flight fade and restore opacity
//   onSiriOpen(cb)   subscribe to main → renderer 'siri:open' events
//   onSiriAppend(cb) subscribe to streaming token append (path 3)
//   onSiriDone(cb)   subscribe to streaming-finished signal
//   onSiriReset(cb)  subscribe to "clear card" signal

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('cardAPI', {
  // Outgoing (renderer → main)
  resize: (h) => ipcRenderer.invoke('card:resize', h),
  show: () => ipcRenderer.invoke('card:show'),
  close: () => ipcRenderer.invoke('card:close'),
  fadeOut: (ms) => ipcRenderer.invoke('card:fadeOut', ms),
  cancelFade: () => ipcRenderer.invoke('card:cancelFade'),

  // Incoming (main → renderer)
  onSiriOpen: (cb) => ipcRenderer.on('siri:open', (_, payload) => cb(payload)),
  onSiriAppend: (cb) => ipcRenderer.on('siri:append', (_, payload) => cb(payload)),
  onSiriDone: (cb) => ipcRenderer.on('siri:done', (_, payload) => cb(payload)),
  onSiriReset: (cb) => ipcRenderer.on('siri:reset', () => cb())
});

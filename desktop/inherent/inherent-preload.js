// inherent-preload.js — bridge between card.html renderer and inherent main.
//
// Renderer-side API exposed as window.cardAPI:
//   resize(h)        notify main of new content height
//   setWidth(w)      widen window leftward for popover (right edge fixed)
//   show()           ask main to make the window visible (post first render)
//   close()          ask main to hide the card (process stays alive)
//   fadeOut(ms)      lerp window opacity 1→0 then hide
//   cancelFade()     ask main to abort an in-flight fade and restore opacity
//   submit(text)     POST typed text to backend (Wave 1 input edge)
//   submitImage(...) POST one staged image + optional text to backend
//   submitVoice(wav) POST recorded WAV bytes to backend voice submit edge
//   duckAudio()      mute system output during microphone capture
//   restoreAudio()   restore system output after microphone capture
//   onSiriOpen(cb)   subscribe to main → renderer 'siri:open' events
//   onSiriAppend(cb) subscribe to streaming token append (path 3)
//   onSiriDone(cb)   subscribe to streaming-finished signal
//   onSiriReset(cb)  subscribe to "clear card" signal
//   onVoiceState(cb) subscribe to wake/listening/transcribing voice states
//   onOpenInput(cb)  subscribe to "hotkey hit, enter input mode" signal

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('cardAPI', {
  // Outgoing (renderer → main)
  resize: (h) => ipcRenderer.invoke('card:resize', h),
  setWidth: (w) => ipcRenderer.invoke('card:setWidth', w),
  show: () => ipcRenderer.invoke('card:show'),
  close: () => ipcRenderer.invoke('card:close'),
  fadeOut: (ms) => ipcRenderer.invoke('card:fadeOut', ms),
  cancelFade: () => ipcRenderer.invoke('card:cancelFade'),
  submit: (text) => ipcRenderer.invoke('card:submit', text),
  submitImage: (payload) => ipcRenderer.invoke('card:submitImage', payload),
  submitVoice: (wavArrayBuffer) => ipcRenderer.invoke('card:submitVoice', wavArrayBuffer),
  duckAudio: () => ipcRenderer.invoke('card:duckAudio'),
  restoreAudio: () => ipcRenderer.invoke('card:restoreAudio'),

  // Incoming (main → renderer)
  onSiriOpen: (cb) => ipcRenderer.on('siri:open', (_, payload) => cb(payload)),
  onSiriAppend: (cb) => ipcRenderer.on('siri:append', (_, payload) => cb(payload)),
  onSiriDone: (cb) => ipcRenderer.on('siri:done', (_, payload) => cb(payload)),
  onSiriReset: (cb) => ipcRenderer.on('siri:reset', () => cb()),
  onVoiceState: (cb) => ipcRenderer.on('card:voice', (_, payload) => cb(payload)),
  onOpenInput: (cb) => ipcRenderer.on('card:openInput', () => cb())
});

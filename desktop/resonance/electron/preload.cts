import { contextBridge, ipcRenderer } from 'electron';
contextBridge.exposeInMainWorld('jarvis', {
  layout: (mode: string, height: number) => ipcRenderer.send('layout', { mode, height }),
  focus: (enabled: boolean) => ipcRenderer.invoke('focus-input', enabled),
  hide: () => ipcRenderer.send('hide'),
  copy: (text: string) => ipcRenderer.invoke('copy', text),
  openCodex: (threadId: string) => ipcRenderer.invoke('open-codex', threadId),
  codexTitles: (ids: string[]) => ipcRenderer.invoke('codex-titles', ids),
  drag: (phase: 'start' | 'move' | 'end', point?: { x: number; y: number }) => ipcRenderer.send('drag', { phase, point }),
  passthrough: (enabled: boolean) => ipcRenderer.send('passthrough', enabled),
  material: (rects: unknown[], strength: number) => ipcRenderer.send('material', { rects, strength }),
  onCommand: (callback: (command: string) => void) => {
    const listener = (_: unknown, command: string) => callback(command);
    ipcRenderer.on('command', listener);
    return () => ipcRenderer.removeListener('command', listener);
  },
});

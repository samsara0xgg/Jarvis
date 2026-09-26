import { contextBridge, ipcRenderer } from 'electron';
contextBridge.exposeInMainWorld('jarvis', {
  placement: () => ipcRenderer.invoke('placement'),
  dock: (enabled: boolean) => ipcRenderer.invoke('dock', enabled),
  onPlacement: (callback: (placement: { docked: boolean; topInset: number; surfaceWidth: number; compactWidth: number; notchWidth: number }) => void) => {
    const listener = (_: unknown, placement: { docked: boolean; topInset: number; surfaceWidth: number; compactWidth: number; notchWidth: number }) => callback(placement);
    ipcRenderer.on('placement', listener);
    return () => ipcRenderer.removeListener('placement', listener);
  },
  onIslandHover: (callback: (inside: boolean) => void) => {
    const listener = (_: unknown, inside: boolean) => callback(inside);
    ipcRenderer.on('island-hover', listener);
    ipcRenderer.send('track-island');
    return () => ipcRenderer.removeListener('island-hover', listener);
  },
  onCursor: (callback: (point: { x: number; y: number }) => void) => {
    const listener = (_: unknown, point: { x: number; y: number }) => callback(point);
    ipcRenderer.on('cursor', listener);
    return () => ipcRenderer.removeListener('cursor', listener);
  },
  onDisplayLeave: (callback: () => void) => {
    const listener = () => callback();
    ipcRenderer.on('display-leave', listener);
    return () => ipcRenderer.removeListener('display-leave', listener);
  },
  displayReady: () => ipcRenderer.send('display-ready'),
  companionMenu: (menu: unknown) => ipcRenderer.send('companion-menu', menu),
  layout: (mode: string, height: number, surface?: { x: number; y: number; width: number; height: number }) => ipcRenderer.send('layout', { mode, height, surface }),
  focus: (enabled: boolean) => ipcRenderer.invoke('focus-input', enabled),
  hide: () => ipcRenderer.send('hide'),
  copy: (text: string) => ipcRenderer.invoke('copy', text),
  openCodex: (threadId: string) => ipcRenderer.invoke('open-codex', threadId),
  codexTitles: (ids: string[]) => ipcRenderer.invoke('codex-titles', ids),
  plugins: (operation: string, data: Record<string, unknown> = {}) => ipcRenderer.invoke('plugins', operation, data),
  drag: (phase: 'start' | 'move' | 'end', point?: { x: number; y: number }) => ipcRenderer.send('drag', { phase, point }),
  passthrough: (enabled: boolean) => ipcRenderer.send('passthrough', enabled),
  material: (rects: unknown[], strength: number) => ipcRenderer.send('material', { rects, strength }),
  onCommand: (callback: (command: string) => void) => {
    const listener = (_: unknown, command: string) => callback(command);
    ipcRenderer.on('command', listener);
    return () => ipcRenderer.removeListener('command', listener);
  },
});

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
  onTuck: (callback: (tucked: { left: boolean; right: boolean }) => void) => {
    const listener = (_: unknown, tucked: { left: boolean; right: boolean }) => callback(tucked);
    ipcRenderer.on('tuck', listener);
    return () => ipcRenderer.removeListener('tuck', listener);
  },
  companionMenu: (menu: unknown) => ipcRenderer.send('companion-menu', menu),
  layout: (mode: string, height: number, surface?: { x: number; y: number; width: number; height: number }) => ipcRenderer.send('layout', { mode, height, surface }),
  focus: (enabled: boolean) => ipcRenderer.invoke('focus-input', enabled),
  hide: () => ipcRenderer.send('hide'),
  copy: (text: string) => ipcRenderer.invoke('copy', text),
  openCodex: (threadId: string) => ipcRenderer.invoke('open-codex', threadId),
  codexTitles: (ids: string[]) => ipcRenderer.invoke('codex-titles', ids),
  openAccount: (service: string) => ipcRenderer.invoke('open-account', service),
  usageReset: (service: string, requestId: string) => ipcRenderer.invoke('usage-reset', service, requestId),
  usageBalance: (service: string, usd: number) => ipcRenderer.invoke('usage-balance', service, usd),
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
// The first launch's window (companion.ts answers only that window).
contextBridge.exposeInMainWorld('firstRun', {
  info: () => ipcRenderer.invoke('first-run-info'),
  permission: (kind: string, ask: boolean, note?: [string, string]) => ipcRenderer.invoke('first-run-permission', kind, ask, note),
  open: (page: string) => ipcRenderer.send('first-run-open', page),
  passthrough: (on: boolean) => ipcRenderer.send('first-run-passthrough', on),
  done: () => ipcRenderer.send('first-run-done'),
});

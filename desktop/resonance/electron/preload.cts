import { contextBridge, ipcRenderer, webUtils } from 'electron';
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
  companionSettings: (settings: unknown) => ipcRenderer.send('companion-settings', settings),
  dashboard: (action: 'state' | 'open' | 'detach' | 'attach' | 'close', options?: { height?: number; dragging?: boolean }) => ipcRenderer.invoke('dashboard', action, options),
  onDashboard: (callback: (state: { detached: boolean; open?: boolean }) => void) => {
    const listener = (_: unknown, state: { detached: boolean; open?: boolean }) => callback(state);
    ipcRenderer.on('dashboard-state', listener);
    return () => ipcRenderer.removeListener('dashboard-state', listener);
  },
  dashboardDrag: (phase: 'start' | 'move' | 'end') => ipcRenderer.send('dashboard-drag', phase),
  dashboardSize: (height: number) => ipcRenderer.send('dashboard-size', height),
  dashboardVisible: (visible: boolean) => ipcRenderer.send('dashboard-visible', visible),
  onDashboardDock: (callback: (near: boolean) => void) => {
    const listener = (_: unknown, near: boolean) => callback(near);
    ipcRenderer.on('dashboard-dock', listener);
    return () => ipcRenderer.removeListener('dashboard-dock', listener);
  },
  dashboardMessage: (target: 'parent' | 'dashboard', payload: Record<string, unknown>) => ipcRenderer.send('dashboard-message', target, payload),
  onDashboardMessage: (callback: (payload: Record<string, unknown>) => void) => {
    const listener = (_: unknown, payload: Record<string, unknown>) => callback(payload);
    ipcRenderer.on('dashboard-message', listener);
    ipcRenderer.send('dashboard-message-ready');
    return () => ipcRenderer.removeListener('dashboard-message', listener);
  },
  layout: (mode: string, height: number, surface?: { x: number; y: number; width: number; height: number }) => ipcRenderer.send('layout', { mode, height, surface }),
  focus: (enabled: boolean) => ipcRenderer.invoke('focus-input', enabled),
  hide: () => ipcRenderer.send('hide'),
  quit: () => ipcRenderer.send('quit'),
  copy: (text: string) => ipcRenderer.invoke('copy', text),
  openCodex: (threadId: string) => ipcRenderer.invoke('open-codex', threadId),
  // ADR 0057: Ghostty's front terminal while asked, and going to a session's terminal.
  watchGhostty: (on: boolean) => ipcRenderer.send('ghostty-watch', on),
  onGhostty: (callback: (seen: { front: boolean; title: string }) => void) => {
    const listener = (_: unknown, seen: { front: boolean; title: string }) => callback(seen);
    ipcRenderer.on('ghostty', listener);
    return () => ipcRenderer.removeListener('ghostty', listener);
  },
  onMouseDown: (callback: () => void) => {
    const listener = () => callback();
    ipcRenderer.on('mouse-down', listener);
    return () => ipcRenderer.removeListener('mouse-down', listener);
  },
  onClaudeFront: (callback: (front: boolean) => void) => {
    const listener = (_: unknown, front: boolean) => callback(front);
    ipcRenderer.on('claude-front', listener);
    return () => ipcRenderer.removeListener('claude-front', listener);
  },
  jumpGhostty: (title: string, job: string) => ipcRenderer.invoke('ghostty-jump', title, job),
  // ADR 0073: the Agents window.
  onAgentsPresence: (callback: (value: { active: boolean; ids: string[] }) => void) => {
    const listener = (_: unknown, value: { active: boolean; ids: string[] }) => callback(value);
    ipcRenderer.on('agents-presence', listener); ipcRenderer.send('agents-presence-ready');
    return () => ipcRenderer.removeListener('agents-presence', listener);
  },
  openAgents: (id?: string) => ipcRenderer.send('agents-open', id),
  codexTitles: (ids: string[]) => ipcRenderer.invoke('codex-titles', ids),
  openAccount: (service: string) => ipcRenderer.invoke('open-account', service),
  openMail: (id: string) => ipcRenderer.invoke('open-mail', id),
  openUrl: (url: string) => ipcRenderer.invoke('open-url', url),
  usageReset: (service: string, requestId: string) => ipcRenderer.invoke('usage-reset', service, requestId),
  usageBalance: (service: string, usd: number) => ipcRenderer.invoke('usage-balance', service, usd),
  tokenUsage: (refresh = false) => ipcRenderer.invoke('token-usage', refresh),
  plugins: (operation: string, data: Record<string, unknown> = {}) => ipcRenderer.invoke('plugins', operation, data),
  drag: (phase: 'start' | 'move' | 'end', point?: { x: number; y: number }) => ipcRenderer.send('drag', { phase, point }),
  passthrough: (enabled: boolean) => ipcRenderer.send('passthrough', enabled),
  material: (rects: unknown[], strength: number) => ipcRenderer.send('material', { rects, strength }),
  // ADR 0058: out at the caret for dictation ('out'), back home ('home', or 'happy' when the words went in); and
  // the skin she wears, for that trip.
  onDictation: (callback: (trip: string) => void) => {
    const listener = (_: unknown, trip: string) => callback(trip);
    ipcRenderer.on('dictation', listener);
    return () => ipcRenderer.removeListener('dictation', listener);
  },
  wearing: (skin: string) => ipcRenderer.send('companion-skin', skin),
  onCommand: (callback: (command: string) => void) => {
    const listener = (_: unknown, command: string) => callback(command);
    ipcRenderer.on('command', listener);
    return () => ipcRenderer.removeListener('command', listener);
  },
});
// ADR 0058: the dictation window beside the text caret (companion.ts answers only that window).
const listen = (channel: string) => (callback: (value: unknown) => void) => { ipcRenderer.on(channel, (_event, value: unknown) => callback(value)); };
contextBridge.exposeInMainWorld('dictation', {
  onStart: listen('dictation-start'),
  onFinish: listen('dictation-finish'),
  onCancel: listen('dictation-cancel'),
  onCursor: listen('dictation-cursor'),
  paste: (text: string, send: boolean) => ipcRenderer.send('dictation-paste', text, send),
  target: (): Promise<string> => ipcRenderer.invoke('dictation-target'),
  copy: (text: string) => ipcRenderer.send('dictation-copy', text),
  home: (happy: boolean) => ipcRenderer.send('dictation-home', happy),
  done: () => ipcRenderer.send('dictation-done'),
  passthrough: (on: boolean) => ipcRenderer.send('dictation-passthrough', on),
  focus: (on: boolean) => ipcRenderer.send('dictation-focus', on),
  open: (page: string) => ipcRenderer.send('dictation-open', page),
  again: () => ipcRenderer.send('dictation-again'),
});
// The Agents window (agentsWindow.ts answers only that window): a folder picker, the owner's terminal, editor, Finder and
// Quick Look, a dropped file's path, and a session a notification opened.
contextBridge.exposeInMainWorld('agents', {
  presence: (enabled: boolean, ids: string[]) => ipcRenderer.send('agents-presence', enabled, ids),
  onNext: (callback: () => void) => { const listener = () => callback(); ipcRenderer.on('agents-next', listener); return () => ipcRenderer.removeListener('agents-next', listener); },
  onOpen: (callback: (id: string) => void) => { const listener = (_: unknown, id: string) => callback(id); ipcRenderer.on('agents-open-session', listener); return () => ipcRenderer.removeListener('agents-open-session', listener); },
  folder: () => ipcRenderer.invoke('agents-folder'),
  terminal: (cwd: string, cmd: string, term?: string) => ipcRenderer.invoke('agents-terminal', cwd, cmd, term),
  terminals: () => ipcRenderer.invoke('agents-terminals'),
  reveal: (cwd: string) => ipcRenderer.invoke('agents-reveal', cwd),
  revealFile: (file: string) => ipcRenderer.invoke('agents-reveal-file', file),
  quickLook: (file: string) => ipcRenderer.invoke('agents-quick-look', file),
  editors: () => ipcRenderer.invoke('agents-editors'),
  openInEditor: (file: string, line?: number, editor?: string) => ipcRenderer.invoke('agents-open-in-editor', file, line, editor),
  pathOf: (file: File) => webUtils.getPathForFile(file),
  openUrl: (url: string) => ipcRenderer.invoke('agents-open-url', url),
  openPath: (file: string) => ipcRenderer.invoke('agents-open-path', file),
  copyFile: (file: string) => ipcRenderer.invoke('agents-copy-file', file),
  cloud: (cwd: string, text: string, term?: string) => ipcRenderer.invoke('agents-cloud', cwd, text, term),
  onSettings: (callback: () => void) => { const listener = () => callback(); ipcRenderer.on('agents-settings', listener); return () => ipcRenderer.removeListener('agents-settings', listener); },
  notifyTest: (title: string, sub: string, body: string, id?: string) => ipcRenderer.invoke('agents-notify-test', title, sub, body, id),
  saveFile: (name: string, text: string) => ipcRenderer.invoke('agents-save-file', name, text),
});
// The first launch's window (companion.ts answers only that window).
contextBridge.exposeInMainWorld('firstRun', {
  info: () => ipcRenderer.invoke('first-run-info'),
  permission: (kind: string, ask: boolean, note?: [string, string]) => ipcRenderer.invoke('first-run-permission', kind, ask, note),
  open: (page: string) => ipcRenderer.send('first-run-open', page),
  passthrough: (on: boolean) => ipcRenderer.send('first-run-passthrough', on),
  done: () => ipcRenderer.send('first-run-done'),
  quit: () => ipcRenderer.send('first-run-quit'),
});

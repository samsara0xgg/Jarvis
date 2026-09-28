import { app, BrowserWindow, globalShortcut, ipcMain, screen } from 'electron';
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import path from 'node:path';

// ADR 0080: a second presentation of the same Dashboard. Main owns its position and
// docking; only these two local main frames can operate it or the shared daemon bridge.
type Sender = Electron.IpcMainEvent | Electron.IpcMainInvokeEvent;
const WIDTH = 360, SHORTCUT = 'CommandOrControl+Shift+Down';
export function setupDashboard({ parent, preload, page, demo, port, mouseDown, onAttach }: {
  parent: BrowserWindow; preload: string; page: string; demo: boolean; port: string; mouseDown?: () => boolean; onAttach: (display: Electron.Display) => void;
}) {
  const file = path.join(app.getPath('userData'), 'dashboard.json');
  let child: BrowserWindow | null = null, loading: Promise<BrowserWindow> | null = null;
  let detached = false, saved: Electron.Rectangle | null = null, attachedVisible = false, registered = false, revision = 0;
  let drag: { point: Electron.Point; bounds: Electron.Rectangle } | null = null;
  let dragTimer: ReturnType<typeof setInterval> | undefined;
  let dockDisplay: number | null = null;
  try {
    const value = JSON.parse(readFileSync(file, 'utf8'));
    detached = value.detached === true;
    const b = value.bounds;
    if (b && ['x', 'y', 'height'].every(k => Number.isFinite(b[k])) && b.height > 0) saved = { x: b.x, y: b.y, width: WIDTH, height: b.height };
  } catch { /* First launch, or an incomplete old preference. */ }
  const windows = () => [parent, child].filter((w): w is BrowserWindow => !!w && !w.isDestroyed());
  const senderWindow = (event: Sender) => windows().find(w => event.sender === w.webContents && event.senderFrame === w.webContents.mainFrame);
  const ready = new Set<number>(), pending = { parent: new Map<string, string>(), dashboard: new Map<string, string>() };
  const watchReady = (w: BrowserWindow) => {
    w.webContents.on('did-start-navigation', (_event, _url, inPlace, mainFrame) => { if (mainFrame && !inPlace) ready.delete(w.id); });
    w.on('closed', () => ready.delete(w.id));
  };
  watchReady(parent);
  // Presentation handoff only: a bounded transient relay, never a second store of daemon state.
  ipcMain.on('dashboard-message', (event, target, payload) => {
    const sender = senderWindow(event);
    if (!sender || !(target === 'dashboard' && sender === parent || target === 'parent' && sender === child)
      || !payload || typeof payload !== 'object' || Array.isArray(payload)) return;
    let text: string;
    try { text = JSON.stringify(payload); } catch { return; }
    if (Buffer.byteLength(text) > 65536) return;
    const recipient = target === 'parent' ? parent : child;
    if (recipient && !recipient.isDestroyed() && ready.has(recipient.id)) { recipient.webContents.send('dashboard-message', JSON.parse(text)); return; }
    const queue = pending[target as 'parent' | 'dashboard'], kind = typeof payload.type === 'string' && payload.type.length < 80 ? payload.type : 'message';
    queue.delete(kind); queue.set(kind, text);
    while (queue.size > 8 || [...queue.values()].reduce((size, value) => size + Buffer.byteLength(value), 0) > 65536) queue.delete(queue.keys().next().value!);
  });
  ipcMain.on('dashboard-message-ready', event => {
    const sender = senderWindow(event);
    if (!sender) return;
    ready.add(sender.id);
    const queue = pending[sender === parent ? 'parent' : 'dashboard'];
    for (const value of queue.values()) sender.webContents.send('dashboard-message', JSON.parse(value));
    queue.clear();
  });
  const persist = () => {
    if (child && !child.isDestroyed()) saved = child.getBounds();
    try {
      mkdirSync(path.dirname(file), { recursive: true });
      writeFileSync(`${file}.tmp`, JSON.stringify({ detached, bounds: saved }), { mode: 0o600 });
      renameSync(`${file}.tmp`, file);
    } catch (error) { console.warn('Dashboard position was not saved', error); }
  };
  const clamp = (bounds: Electron.Rectangle) => {
    const area = screen.getDisplayMatching(bounds).workArea, height = Math.min(Math.max(180, Math.round(bounds.height)), area.height);
    return { x: Math.round(Math.max(area.x, Math.min(bounds.x, area.x + area.width - WIDTH))),
      y: Math.round(Math.max(area.y, Math.min(bounds.y, area.y + area.height - height))), width: WIDTH, height };
  };
  const shortcut = () => {
    const wanted = attachedVisible && !detached && !parent.isDestroyed() && !parent.isFocused();
    if (wanted && !registered) registered = globalShortcut.register(SHORTCUT, () => parent.webContents.send('command', 'dashboard-detach'));
    else if (!wanted && registered) { globalShortcut.unregister(SHORTCUT); registered = false; }
  };
  const notify = (open?: boolean) => {
    for (const w of windows()) w.webContents.send('dashboard-state', { detached, ...(open === undefined ? {} : { open }) });
    shortcut();
  };
  const near = (b: Electron.Rectangle, display: Electron.Display) => Math.abs(b.y - display.bounds.y) <= 90
    && Math.abs(b.x + b.width / 2 - display.bounds.x - display.bounds.width / 2) <= 150;
  const dock = (display: Electron.Display | null) => {
    const was = dockDisplay !== null;
    // The receiving notch must be on the window's target display, including when
    // ordinary cursor-following is disabled. This previews the eventual attach.
    if (display && display.id !== dockDisplay) onAttach(display);
    dockDisplay = display?.id ?? null;
    if (was !== !!display && !parent.isDestroyed()) parent.webContents.send('dashboard-dock', !!display);
  };
  const endTracking = () => { drag = null; clearInterval(dragTimer); dragTimer = undefined; dock(null); };
  const attach = () => {
    if (child && !child.isDestroyed()) onAttach(screen.getDisplayMatching(child.getBounds()));
    endTracking(); detached = false; persist(); child?.hide(); notify(true);
  };
  const move = () => {
    if (!drag || !child || child.isDestroyed()) return;
    const p = screen.getCursorScreenPoint();
    child.setBounds({ ...drag.bounds, x: Math.round(drag.bounds.x + p.x - drag.point.x), y: Math.round(drag.bounds.y + p.y - drag.point.y) });
    const b = child.getBounds(), display = screen.getDisplayMatching(b);
    dock(near(b, display) ? display : null);
  };
  const start = () => {
    if (!detached || !child || child.isDestroyed() || !child.isVisible()) return;
    endTracking(); drag = { point: screen.getCursorScreenPoint(), bounds: child.getBounds() };
    // The original renderer can become inert once the child appears. Native button
    // state completes that same gesture even if its final pointerup is lost.
    dragTimer = setInterval(() => { if (mouseDown && !mouseDown()) end(); else move(); }, 16);
  };
  const end = () => {
    if (!drag || !child || child.isDestroyed()) return;
    move(); endTracking();
    const b = child.getBounds(), display = screen.getDisplayMatching(b);
    if (near(b, display)) attach();
    else { child.setBounds(clamp(b)); persist(); }
  };
  const ensure = (height?: number) => {
    if (child && !child.isDestroyed() && !loading) return Promise.resolve(child);
    if (loading) return loading;
    loading = (async () => {
      const p = parent.getBounds(), initial = saved ?? { x: p.x + (p.width - WIDTH) / 2, y: p.y + 72, width: WIDTH, height: 520 };
      const bounds = clamp({ ...initial, ...(typeof height === 'number' && Number.isFinite(height) ? { height } : {}) });
      const w = child = new BrowserWindow({ ...bounds, title: 'Jarvis Dashboard', frame: false, transparent: true, backgroundColor: '#00000000',
        hasShadow: false, show: false, resizable: false, maximizable: false, fullscreenable: false, skipTaskbar: true, roundedCorners: false,
        webPreferences: { preload, contextIsolation: true, nodeIntegration: false, sandbox: true, autoplayPolicy: 'no-user-gesture-required' } });
      watchReady(w);
      w.setAlwaysOnTop(true, 'floating');
      w.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
      w.setIgnoreMouseEvents(false);
      w.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      w.webContents.on('will-navigate', event => event.preventDefault());
      w.on('close', event => { event.preventDefault(); endTracking(); persist(); w.hide(); notify(false); });
      w.on('closed', () => { endTracking(); if (child === w) child = null; });
      try { await w.loadFile(page, { query: { companion: '1', detached: '1', ...demo ? {} : { port, ...app.isPackaged ? { packaged: '1' } : {} } } }); }
      catch (error) { w.destroy(); throw error; }
      return w;
    })().finally(() => { loading = null; });
    return loading;
  };
  ipcMain.handle('dashboard', async (event, action: unknown, options: { height?: number; dragging?: boolean } | null) => {
    if (!senderWindow(event)) throw new Error('Not this Dashboard window');
    if (action === 'state') return { detached };
    if (!['open', 'detach', 'attach', 'close'].includes(String(action))) throw new Error('Invalid Dashboard action');
    const id = ++revision;
    if (action === 'attach') attach();
    else if (action === 'close') { endTracking(); persist(); child?.hide(); notify(false); }
    else if (action === 'open' && !detached) notify(true);
    else {
      const w = await ensure(options?.height);
      if (id !== revision) throw new Error('Dashboard opening was superseded');
      if (options?.dragging === true) {
        const p = screen.getCursorScreenPoint();
        w.setBounds({ ...w.getBounds(), x: p.x - WIDTH / 2, y: p.y - 18 });
      }
      detached = true; w.show(); w.focus(); persist(); notify(true);
      if (options?.dragging === true) start();
    }
    return { detached };
  });
  ipcMain.on('dashboard-drag', (event, phase) => {
    if (!senderWindow(event)) return;
    if (phase === 'start') start(); else if (phase === 'move') move(); else if (phase === 'end') end();
  });
  ipcMain.on('dashboard-size', (event, height) => {
    if (senderWindow(event) !== child || !child || !Number.isFinite(height)) return;
    const before = child.getBounds(), after = clamp({ ...before, height: Math.max(height, before.height) });
    if (['x', 'y', 'width', 'height'].some(k => before[k as keyof Electron.Rectangle] !== after[k as keyof Electron.Rectangle])) child.setBounds(after);
  });
  ipcMain.on('dashboard-visible', (event, visible) => {
    if (senderWindow(event) !== parent || typeof visible !== 'boolean') return;
    attachedVisible = visible; shortcut();
  });
  const fit = () => { if (child && !child.isDestroyed()) { child.setBounds(clamp(child.getBounds())); persist(); } };
  screen.on('display-added', fit); screen.on('display-removed', fit); screen.on('display-metrics-changed', fit);
  parent.on('focus', shortcut); parent.on('blur', shortcut);
  parent.on('closed', () => { endTracking(); child?.destroy(); });
  app.on('before-quit', () => { endTracking(); persist(); child?.destroy(); });
  app.on('will-quit', () => { if (registered) globalShortcut.unregister(SHORTCUT); });
  return { windows, senderWindow, window: () => child, placement: () => {
    const display = child && !child.isDestroyed() ? screen.getDisplayMatching(child.getBounds()) : screen.getPrimaryDisplay();
    return { docked: false, topInset: 0, notchWidth: 0, surfaceWidth: WIDTH, compactWidth: 0, displayId: display.id };
  } };
}

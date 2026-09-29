import { BrowserWindow, dialog, ipcMain, session, shell } from 'electron';
import { spawn, execFile } from 'node:child_process';
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, rmSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { hostKey } from './agents/key.js';
// ADR 0073: the Agents window. Its sessions run in the agent host (agents/host.ts), which this process starts when
// nothing answers on its port and which keeps running when the companion restarts. The window talks to the host
// itself; from here it only asks for what a page may not do: a folder picker and a terminal tab.
export const AGENTS_PORT = process.env.JARVIS_AGENTS_PORT ?? '8016';
const wait = (ms: number) => new Promise(done => setTimeout(done, ms));
const run = (cmd: string, args: string[]) => new Promise<string>(done => execFile(cmd, args, { timeout: 5000 }, (_, out) => done(String(out ?? ''))));
// 'old': a host from before it had a key of its own (ADR 0095) holds the port, and does not know this one.
async function answers(): Promise<'yes' | 'no' | 'old'> {
  try {
    const r = await fetch(`http://127.0.0.1:${AGENTS_PORT}/health`, { headers: { Authorization: `Bearer ${hostKey()}` }, signal: AbortSignal.timeout(1500) });
    return r.ok ? 'yes' : r.status === 401 ? 'old' : 'no';
  } catch { return 'no'; }
}
// The old host goes the way a restart takes it: its turns carry on in the keeper (ADR 0082) and the new one takes them back.
async function replaceOld() {
  for (const pid of (await run('/usr/sbin/lsof', ['-nP', `-iTCP:${AGENTS_PORT}`, '-sTCP:LISTEN', '-t'])).split('\n').map(Number).filter(Boolean)) {
    if (!/agents\/host\.js/.test(await run('/bin/ps', ['-o', 'command=', '-p', String(pid)]))) continue;
    try { process.kill(pid, 'SIGTERM'); } catch { continue; }
    for (let i = 0; i < 40; i++) { await wait(250); try { process.kill(pid, 0); } catch { break; } }
  }
}
let starting: Promise<void> | null = null;
export function ensureHost(host: string) {
  return starting ??= (async () => {
    const now = await answers();
    if (now === 'yes') return;
    if (now === 'old') await replaceOld();
    const logs = path.join(process.env.JARVIS_RUNTIME_ROOT ?? path.join(homedir(), '.jarvis'), 'logs');
    mkdirSync(logs, { recursive: true, mode: 0o700 });
    const out = openSync(path.join(logs, 'agents-host.out.log'), 'a'), err = openSync(path.join(logs, 'agents-host.err.log'), 'a');
    // Its own process group, so it outlives this one; this Electron runs it as plain Node. git and codex live on these paths.
    const child = spawn(process.execPath, [host], { detached: true, stdio: ['ignore', out, err], env: { ...process.env, ELECTRON_RUN_AS_NODE: '1',
      PATH: [path.join(homedir(), '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin', process.env.PATH || '/usr/bin:/bin:/usr/sbin:/sbin'].join(':') } });
    closeSync(out); closeSync(err);
    child.unref();
    for (let i = 0; i < 60 && await answers() !== 'yes'; i++) await wait(250);
  })().finally(() => { starting = null; });
}
// A new Ghostty tab in the session's folder that continues it.
const GHOSTTY_RUN = `on run argv
  tell application "Ghostty"
    set cfg to new surface configuration
    set initial input of cfg to (item 1 of argv) & linefeed
    if (count of windows) > 0 then
      new tab in front window with configuration cfg
    else
      new window with configuration cfg
    end if
    activate
  end tell
end run`;
const quote = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`;

export function setupAgents({ preload, page, host, trustedWindows }: { preload: string; page: string; host: string; trustedWindows: () => BrowserWindow[] }) {
  let win: BrowserWindow | null = null, exposure = false, ids: string[] = [], presenceKey = '';
  const presence = () => ({ active: !!win && !win.isDestroyed() && win.isFocused() && exposure, ids });
  const publish = () => {
    const value = presence(), key = JSON.stringify(value);
    if (key === presenceKey) return; presenceKey = key;
    for (const target of trustedWindows()) if (!target.isDestroyed()) target.webContents.send('agents-presence', value);
  };
  const mine = (event: Electron.IpcMainInvokeEvent | Electron.IpcMainEvent) => !!win && event.sender === win.webContents && event.senderFrame === win.webContents.mainFrame;
  ipcMain.on('agents-presence', (event, enabled: unknown, values: unknown) => {
    if (!mine(event) || typeof enabled !== 'boolean' || !Array.isArray(values) || values.length > 2000 || !values.every(id => typeof id === 'string' && id.length <= 128)) return;
    exposure = enabled; ids = values; publish();
  });
  ipcMain.on('agents-presence-ready', event => {
    if (trustedWindows().some(w => !w.isDestroyed() && event.sender === w.webContents && event.senderFrame === w.webContents.mainFrame)) event.sender.send('agents-presence', presence());
  });
  ipcMain.handle('agents-folder', async event => {
    if (!mine(event)) return '';
    const r = await dialog.showOpenDialog(win!, { properties: ['openDirectory'], defaultPath: path.join(homedir(), 'Projects') });
    return r.canceled ? '' : r.filePaths[0] ?? '';
  });
  ipcMain.handle('agents-terminal', (event, cwd: unknown, cmd: unknown) => new Promise<boolean>(resolve => {
    if (!mine(event) || typeof cwd !== 'string' || typeof cmd !== 'string' || !path.isAbsolute(cwd) || !existsSync(cwd) || !statSync(cwd).isDirectory()
      || !/^(claude --resume|codex resume) [0-9a-f-]{36}$/i.test(cmd)) { resolve(false); return; }
    execFile('/usr/bin/osascript', ['-e', GHOSTTY_RUN, `cd ${quote(cwd)} && ${cmd}`], { timeout: 8000 }, error => resolve(!error));
  }));
  ipcMain.handle('agents-reveal', (event, cwd: unknown) => { if (mine(event) && typeof cwd === 'string' && path.isAbsolute(cwd)) void shell.openPath(cwd); });
  // The workbench (ADR 0085-0087): a page or file in the real browser or its own app, and a session handed to the cloud.
  ipcMain.handle('agents-open-url', (event, url: unknown) => { if (mine(event) && typeof url === 'string' && /^https?:\/\//i.test(url) && url.length < 4096) void shell.openExternal(url); });
  ipcMain.handle('agents-open-path', (event, file: unknown) => { if (mine(event) && typeof file === 'string' && path.isAbsolute(file) && existsSync(file)) void shell.openPath(file); });
  ipcMain.handle('agents-cloud', (event, cwd: unknown, text: unknown) => new Promise<boolean>(resolve => {
    if (!mine(event) || typeof cwd !== 'string' || typeof text !== 'string' || !text.trim() || text.length > 600 || !path.isAbsolute(cwd) || !existsSync(cwd) || !statSync(cwd).isDirectory()) { resolve(false); return; }
    execFile('/usr/bin/osascript', ['-e', GHOSTTY_RUN, `cd ${quote(cwd)} && claude --cloud ${quote(text.replace(/\s+/g, ' ').trim())}`], { timeout: 8000 }, error => resolve(!error));
  }));
  // Previews run in their own browser: a partition of their own that never gets the daemon key, no preload, no Node,
  // new windows go to the real browser, and every permission is refused.
  const web = session.fromPartition('persist:agents-web');
  web.setPermissionRequestHandler((_, permission, callback) => callback(permission === 'clipboard-sanitized-write' || permission === 'fullscreen'));
  async function open(id = '') {
    if (win && !win.isDestroyed()) { win.show(); win.focus(); return; }
    await ensureHost(host);
    win = new BrowserWindow({ width: 1180, height: 780, minWidth: 720, minHeight: 520, show: false, title: 'Agents',
      titleBarStyle: 'hiddenInset', trafficLightPosition: { x: 14, y: 14 }, backgroundColor: '#0c0d20',
      // Her sounds play before the window is first touched: a session finishing while the window just sits open chimes.
      webPreferences: { preload, contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: true, autoplayPolicy: 'no-user-gesture-required', webviewTag: true } });
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.on('will-navigate', event => event.preventDefault());
    win.webContents.on('will-attach-webview', (event, prefs, params) => {
      delete prefs.preload;
      Object.assign(prefs, { nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true });
      params.partition = 'persist:agents-web';
      if (!/^(https?|file):/i.test(params.src ?? '')) event.preventDefault();
    });
    win.webContents.on('did-attach-webview', (_, guest) => {
      guest.setWindowOpenHandler(({ url }) => { if (/^https?:\/\//i.test(url)) void shell.openExternal(url); return { action: 'deny' }; });
    });
    win.on('focus', publish); win.on('blur', publish);
    win.on('closed', () => { win = null; publish(); });
    win.loadFile(page, { query: { port: AGENTS_PORT, ...(id ? { open: id } : {}) } });
    win.once('ready-to-show', () => { win?.show(); win?.focus(); });
  }
  // A landing (or the 服务 tab) that restarted the companion left a note: open the window again on that session.
  function reopen() {
    const file = path.join(process.env.JARVIS_AGENTS_DIR ?? path.join(process.env.JARVIS_RUNTIME_ROOT ?? path.join(homedir(), '.jarvis'), 'agents'), 'reopen.json');
    try {
      const note = JSON.parse(readFileSync(file, 'utf8')) as { id?: unknown; at?: unknown };
      rmSync(file, { force: true });
      if (typeof note.at === 'number' && Date.now() - note.at < 180e3) void open(typeof note.id === 'string' ? note.id : '');
    } catch { /* no note */ }
  }
  ipcMain.on('agents-open', event => {
    if (trustedWindows().some(w => !w.isDestroyed() && event.sender === w.webContents && event.senderFrame === w.webContents.mainFrame)) void open();
  });
  // ⌥Tab while B01 has the foreground: straight to its next session waiting on Allen.
  return { open: () => open(), reopen, next() { if (!presence().active) return false; win!.webContents.send('agents-next'); return true; } };
}

import { BrowserWindow, dialog, ipcMain, shell } from 'electron';
import { spawn, execFile } from 'node:child_process';
import { closeSync, existsSync, mkdirSync, openSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { daemonToken } from './bridge.js';
// ADR 0073: the Agents window. Its sessions run in the agent host (agents/host.ts), which this process starts when
// nothing answers on its port and which keeps running when the companion restarts. The window talks to the host
// itself; from here it only asks for what a page may not do: a folder picker and a terminal tab.
export const AGENTS_PORT = process.env.JARVIS_AGENTS_PORT ?? '8016';
const wait = (ms: number) => new Promise(done => setTimeout(done, ms));
async function answers() {
  try {
    const r = await fetch(`http://127.0.0.1:${AGENTS_PORT}/health`, { headers: { Authorization: `Bearer ${await daemonToken()}` }, signal: AbortSignal.timeout(1500) });
    return r.ok;
  } catch { return false; }
}
let starting: Promise<void> | null = null;
export function ensureHost(host: string) {
  return starting ??= (async () => {
    if (await answers()) return;
    const logs = path.join(process.env.JARVIS_RUNTIME_ROOT ?? path.join(homedir(), '.jarvis'), 'logs');
    mkdirSync(logs, { recursive: true, mode: 0o700 });
    const out = openSync(path.join(logs, 'agents-host.out.log'), 'a'), err = openSync(path.join(logs, 'agents-host.err.log'), 'a');
    // Its own process group, so it outlives this one; this Electron runs it as plain Node. git and codex live on these paths.
    const child = spawn(process.execPath, [host], { detached: true, stdio: ['ignore', out, err], env: { ...process.env, ELECTRON_RUN_AS_NODE: '1',
      PATH: [path.join(homedir(), '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin', process.env.PATH || '/usr/bin:/bin:/usr/sbin:/sbin'].join(':') } });
    closeSync(out); closeSync(err);
    child.unref();
    for (let i = 0; i < 60 && !await answers(); i++) await wait(250);
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

export function setupAgents({ preload, page, host }: { preload: string; page: string; host: string }) {
  let win: BrowserWindow | null = null;
  const mine = (event: Electron.IpcMainInvokeEvent | Electron.IpcMainEvent) => !!win && event.sender === win.webContents;
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
  async function open() {
    if (win && !win.isDestroyed()) { win.show(); win.focus(); return; }
    await ensureHost(host);
    win = new BrowserWindow({ width: 1180, height: 780, minWidth: 720, minHeight: 520, show: false, title: 'Agents',
      titleBarStyle: 'hiddenInset', trafficLightPosition: { x: 14, y: 14 }, backgroundColor: '#0c0d20',
      webPreferences: { preload, contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: true } });
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    win.webContents.on('will-navigate', event => event.preventDefault());
    win.on('closed', () => { win = null; });
    win.loadFile(page, { query: { port: AGENTS_PORT } });
    win.once('ready-to-show', () => { win?.show(); win?.focus(); });
  }
  ipcMain.on('agents-open', () => { void open(); });
  return { open };
}

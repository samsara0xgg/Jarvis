import { app, BrowserWindow, clipboard, ClipboardItem, dialog, ipcMain, Menu, Notification, session, shell } from 'electron';
import { spawn, execFile } from 'node:child_process';
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { hostKey } from './agents/key.js';
import { en, setLang, tr } from './agents/lang.js';
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
// The old host goes the way a restart takes it: its turns carry on in the keeper (ADR 0098) and the new one takes them back.
async function replaceOld() {
  for (const pid of (await run('/usr/sbin/lsof', ['-nP', `-iTCP:${AGENTS_PORT}`, '-sTCP:LISTEN', '-t'])).split('\n').map(Number).filter(Boolean)) {
    if (!/agents\/host\.js/.test(await run('/bin/ps', ['-o', 'command=', '-p', String(pid)]))) continue;
    try { process.kill(pid, 'SIGTERM'); } catch { continue; }
    for (let i = 0; i < 40; i++) { await wait(250); try { process.kill(pid, 0); } catch { break; } }
  }
}
// ADR 0109: Jarvis's own language, as the agent host holds it (it asks the daemon). The notifications and the menu follow
// it; read when the window opens and once a minute. While the host does not answer, the last answer stands.
let langChanged = () => {};
async function readLang() {
  const was = en;
  try { setLang((await (await fetch(`http://127.0.0.1:${AGENTS_PORT}/lang`, { headers: { Authorization: `Bearer ${hostKey()}` }, signal: AbortSignal.timeout(2500) })).json()).language); } catch { /* host away */ }
  if (en !== was) langChanged();
}
let starting: Promise<void> | null = null;
// `packaged`: the installed app, where Claude signs in with the owner's own key (ADR 0094).
export function ensureHost(host: string, packaged = false) {
  return starting ??= (async () => {
    const now = await answers();
    if (now === 'yes') return;
    if (now === 'old') await replaceOld();
    const logs = path.join(process.env.JARVIS_RUNTIME_ROOT ?? path.join(homedir(), '.jarvis'), 'logs');
    mkdirSync(logs, { recursive: true, mode: 0o700 });
    const out = openSync(path.join(logs, 'agents-host.out.log'), 'a'), err = openSync(path.join(logs, 'agents-host.err.log'), 'a');
    // Its own process group, so it outlives this one; this Electron runs it as plain Node. git and codex live on these paths.
    const child = spawn(process.execPath, [host], { detached: true, stdio: ['ignore', out, err], env: { ...process.env, ELECTRON_RUN_AS_NODE: '1',
      ...packaged ? { JARVIS_AGENTS_PACKAGED: '1' } : {},
      PATH: [path.join(homedir(), '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin', process.env.PATH || '/usr/bin:/bin:/usr/sbin:/sbin'].join(':') } });
    closeSync(out); closeSync(err);
    child.unref();
    for (let i = 0; i < 60 && await answers() !== 'yes'; i++) await wait(250);
  })().finally(() => { starting = null; });
}
// ---------- the owner's own apps (B4, B21): what is installed, and a file or a command in it ----------
const appAt = (name: string) => ['/Applications', '/Applications/Utilities', '/System/Applications/Utilities', path.join(homedir(), 'Applications')]
  .map(dir => path.join(dir, name)).find(p => existsSync(p));
// An editor opens a file at a line through the URL it answers to; one without such a URL gets the file.
const EDITORS: { id: string; name: string; app: string; url?: (file: string, line?: number) => string }[] = [
  { id: 'vscode', name: 'VS Code', app: 'Visual Studio Code.app', url: (f, l) => `vscode://file${encodeURI(f)}${l ? `:${l}` : ''}` },
  { id: 'cursor', name: 'Cursor', app: 'Cursor.app', url: (f, l) => `cursor://file${encodeURI(f)}${l ? `:${l}` : ''}` },
  { id: 'windsurf', name: 'Windsurf', app: 'Windsurf.app', url: (f, l) => `windsurf://file${encodeURI(f)}${l ? `:${l}` : ''}` },
  { id: 'zed', name: 'Zed', app: 'Zed.app', url: (f, l) => `zed://file${encodeURI(f)}${l ? `:${l}` : ''}` },
  { id: 'sublime', name: 'Sublime Text', app: 'Sublime Text.app', url: (f, l) => `subl://open?url=${encodeURIComponent(`file://${f}`)}${l ? `&line=${l}` : ''}` },
  { id: 'xcode', name: 'Xcode', app: 'Xcode.app' }, { id: 'nova', name: 'Nova', app: 'Nova.app' }, { id: 'bbedit', name: 'BBEdit', app: 'BBEdit.app' },
  { id: 'intellij', name: 'IntelliJ IDEA', app: 'IntelliJ IDEA.app' }, { id: 'intellij-ce', name: 'IntelliJ IDEA CE', app: 'IntelliJ IDEA CE.app' },
  { id: 'pycharm', name: 'PyCharm', app: 'PyCharm.app' }, { id: 'pycharm-ce', name: 'PyCharm CE', app: 'PyCharm CE.app' },
  { id: 'webstorm', name: 'WebStorm', app: 'WebStorm.app' }, { id: 'goland', name: 'GoLand', app: 'GoLand.app' },
];
// A command typed into a new tab or window of the owner's terminal, in the order one is picked when none was chosen.
// Warp takes no typed input from outside: it opens in the folder and the command goes to the clipboard.
const TERMINALS: { id: string; name: string; app: string; script?: string }[] = [
  { id: 'ghostty', name: 'Ghostty', app: 'Ghostty.app', script: `on run argv
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
end run` },
  { id: 'iterm', name: 'iTerm2', app: 'iTerm.app', script: `on run argv
  tell application "iTerm"
    activate
    set w to (create window with default profile)
    tell current session of w to write text (item 1 of argv)
  end tell
end run` },
  { id: 'terminal', get name() { return tr('终端', 'Terminal'); }, app: 'Terminal.app', script: `on run argv
  tell application "Terminal"
    activate
    do script (item 1 of argv)
  end tell
end run` },
  { id: 'warp', name: 'Warp', app: 'Warp.app' },
];
const quote = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`;
// The editor and terminal the owner chose in the host's settings, as its event stream last said.
const chosen: { editor?: string; terminal?: string } = {};
// true: typed and running; 'copied': open in the folder with the command on the clipboard; false: nothing to run it in.
// No command: a shell in that folder.
function typeIn(term: unknown, cwd: string, cmd: string) {
  const want = term ?? chosen.terminal;
  const t = TERMINALS.find(x => x.id === want && appAt(x.app)) ?? TERMINALS.find(x => x.script && appAt(x.app));
  const line = cmd ? `cd ${quote(cwd)} && ${cmd}` : `cd ${quote(cwd)}`;
  return new Promise<boolean | 'copied'>(resolve => {
    if (!t) resolve(false);
    else if (t.script) execFile('/usr/bin/osascript', ['-e', t.script, line], { timeout: 8000 }, error => resolve(!error));
    else execFile('/usr/bin/open', ['-a', appAt(t.app)!, cwd], { timeout: 8000 }, error => { if (!error && cmd) clipboard.writeText(cmd); resolve(error ? false : cmd ? 'copied' : true); });
  });
}

// ---------- A6: the Mac says when a session needs the owner, while the window is not in front ----------
// The companion follows the host's event stream itself, so this works with the window closed; a click opens the
// session. Which moments count is the owner's (settings.notify), and none do while Jarvis's notch says them instead
// (`notch`, on unless turned off; src/startrail.ts), which it can only where main hands the notch the host's port
// (companion.ts: not in the installed app). One session says one thing at a time: its newer
// notification replaces the older, and not within 20 seconds of it.
// macOS posts notifications only for a signed app: the dev build's Electron is not, and each one fails with
// UNErrorDomain 1. There the notification goes through osascript instead, which cannot open the session when clicked.
// In the installed app a failure is the owner's no, and nothing goes round it.
type Row = { id: string; title: string; summary: string; st: string; unread: boolean; archived: boolean; parked: boolean; asks?: boolean };
// ADR 0125: a finish the daemon's Jev read as asking you something is told as a wait (src/agents/queue.ts `asksYou`).
const stOf = (s: Row) => s.st === 'done' && s.unread && s.asks ? 'wait' : s.st;
const script = (title: string, sub: string, body: string) => execFile('/usr/bin/osascript',
  ['-e', 'on run a', '-e', 'display notification (item 3 of a) with title (item 1 of a) subtitle (item 2 of a)', '-e', 'end run', title, sub, body], { timeout: 8000 }, () => {});
function watchHost(show: (id: string) => void, front: () => boolean, quiet: () => boolean) {
  const st = new Map<string, string>(), shown = new Map<string, { at: number; n?: Notification }>();
  let notify: { done: boolean; wait: boolean; err: boolean; notch?: boolean } = { done: false, wait: true, err: true }, failed = false;
  const saw = (s: Row) => {
    const was = st.get(s.id), now = stOf(s), last = shown.get(s.id);
    st.set(s.id, now);
    if (was === undefined || was === now || s.archived || front() || (notify.notch !== false && !app.isPackaged) || !Notification.isSupported()) return;
    const kind = now === 'wait' ? 'wait' : now === 'err' ? 'err' : now === 'done' && s.unread && ['work', 'pack', 'wait'].includes(was) ? 'done' : null;
    // A finish that turns out to ask replaces its own "Done" banner at once, not 20 seconds later.
    if (!kind || !notify[kind] || (last && Date.now() - last.at < 20e3 && was !== 'done')) return;
    last?.n?.close();
    const sub = kind === 'wait' ? tr('在等你', 'Waiting on you') : kind === 'err' ? tr('出错了', 'Error') : tr('做完了', 'Done');
    if (failed && !app.isPackaged) { shown.set(s.id, { at: Date.now() }); script(s.title, sub, s.summary); return; }
    const n = new Notification({ title: s.title, subtitle: sub, body: s.summary, silent: quiet() });
    n.on('click', () => show(s.id));
    n.on('failed', (_e, error) => {
      if (!failed) console.error(`agents: macOS refused the notification (${error})${app.isPackaged ? '' : '; the dev build says it through osascript from now on'}`);
      failed = true;
      if (!app.isPackaged) script(s.title, sub, s.summary);
    });
    // Held, or a notification collected before it is clicked opens nothing.
    shown.set(s.id, { at: Date.now(), n });
    n.show();
  };
  const take = (e: { t: string; sessions?: Row[]; s?: Row; id?: string; settings?: { notify?: typeof notify; editor?: string; terminal?: string } }) => {
    if (e.t === 'hello') { st.clear(); for (const s of e.sessions ?? []) st.set(s.id, stOf(s)); }
    if (e.t === 'hello' || e.t === 'settings') {
      notify = e.settings?.notify ?? { done: false, wait: true, err: true };
      Object.assign(chosen, { editor: e.settings?.editor, terminal: e.settings?.terminal });
    }
    else if (e.t === 'sess' && e.s) saw(e.s);
    else if (e.t === 'gone' && e.id) { st.delete(e.id); shown.get(e.id)?.n?.close(); shown.delete(e.id); }
  };
  void (async () => {
    for (;;) {
      try {
        const r = await fetch(`http://127.0.0.1:${AGENTS_PORT}/events`, { headers: { Authorization: `Bearer ${hostKey()}` } });
        const rd = r.ok ? r.body?.getReader() : undefined, dec = new TextDecoder();
        let buf = '';
        for (;;) {
          const { value, done } = rd ? await rd.read() : { value: undefined, done: true };
          if (done) break;
          buf += dec.decode(value, { stream: true });
          for (let i = buf.indexOf('\n\n'); i >= 0; i = buf.indexOf('\n\n')) {
            const data = buf.slice(0, i).split('\n').find(l => l.startsWith('data: '));
            buf = buf.slice(i + 2);
            if (data) take(JSON.parse(data.slice(6)));
          }
        }
      } catch { /* no host yet, or it went: try again */ }
      await wait(10000);
    }
  })();
}

// `packaged`: the installed app (ADR 0094), once it ships Claude Code and offers this window.
export function setupAgents({ preload, page, host, packaged = false, trustedWindows }: { preload: string; page: string; host: string; packaged?: boolean; trustedWindows: () => BrowserWindow[] }) {
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
    const projects = path.join(homedir(), 'Projects');
    const r = await dialog.showOpenDialog(win!, { properties: ['openDirectory', 'createDirectory'], defaultPath: existsSync(projects) ? projects : homedir() });
    return r.canceled ? '' : r.filePaths[0] ?? '';
  });
  // A conversation as Markdown (/export), kept where the owner picks, ~/Downloads to start with: its path, or '' when cancelled.
  ipcMain.handle('agents-save-file', async (event, name: unknown, text: unknown) => {
    if (!mine(event) || typeof name !== 'string' || typeof text !== 'string' || text.length > 64 << 20) return '';
    const r = await dialog.showSaveDialog(win!, { defaultPath: path.join(homedir(), 'Downloads', path.basename(name) || 'session.md'), filters: [{ name: 'Markdown', extensions: ['md'] }] });
    if (r.canceled || !r.filePath) return '';
    writeFileSync(r.filePath, text);
    return r.filePath;
  });
  const isDir = (p: unknown): p is string => typeof p === 'string' && path.isAbsolute(p) && existsSync(p) && statSync(p).isDirectory();
  const isFile = (p: unknown): p is string => typeof p === 'string' && path.isAbsolute(p) && existsSync(p);
  // `term`: a terminal by its id from agents-terminals; without one, the owner's choice in settings, else the first installed.
  // `cmd`: a session to go on with there, or '' for a shell in the folder.
  ipcMain.handle('agents-terminal', async (event, cwd: unknown, cmd: unknown, term: unknown) => {
    if (!mine(event) || !isDir(cwd) || typeof cmd !== 'string' || (cmd !== '' && !/^(claude --resume|codex resume) [0-9a-f-]{36}$/i.test(cmd))) return false;
    return typeIn(term, cwd, cmd);
  });
  ipcMain.handle('agents-terminals', event => mine(event) ? TERMINALS.filter(t => appAt(t.app)).map(t => ({ id: t.id, name: t.name })) : []);
  ipcMain.handle('agents-reveal', (event, cwd: unknown) => { if (mine(event) && typeof cwd === 'string' && path.isAbsolute(cwd)) void shell.openPath(cwd); });
  // A file in Finder (B1), in Quick Look inside the window (B2), or in the owner's editor at a line (B4).
  ipcMain.handle('agents-reveal-file', (event, file: unknown) => { if (mine(event) && isFile(file)) shell.showItemInFolder(file); });
  ipcMain.handle('agents-quick-look', (event, file: unknown) => { if (mine(event) && isFile(file)) win!.previewFile(file); });
  ipcMain.handle('agents-editors', event => mine(event) ? EDITORS.filter(e => appAt(e.app)).map(e => ({ id: e.id, name: e.name })) : []);
  ipcMain.handle('agents-open-in-editor', async (event, file: unknown, line: unknown, editor: unknown) => {
    if (!mine(event) || !isFile(file)) return false;
    const at = typeof line === 'number' && Number.isInteger(line) && line > 0 ? line : undefined;
    const want = editor ?? chosen.editor, e = EDITORS.find(x => x.id === want && appAt(x.app)) ?? EDITORS.find(x => appAt(x.app));
    if (!e) return (await shell.openPath(file)) === '';
    if (e.url) { await shell.openExternal(e.url(file, at)); return true; }
    return new Promise<boolean>(resolve => execFile('/usr/bin/open', ['-a', appAt(e.app)!, file], { timeout: 8000 }, error => resolve(!error)));
  });
  // The workbench (ADR 0085-0087): a page or file in the real browser or its own app, and a session handed to the cloud.
  ipcMain.handle('agents-open-url', (event, url: unknown) => { if (mine(event) && typeof url === 'string' && /^https?:\/\//i.test(url) && url.length < 4096) void shell.openExternal(url); });
  ipcMain.handle('agents-open-path', (event, file: unknown) => { if (mine(event) && typeof file === 'string' && path.isAbsolute(file) && existsSync(file)) void shell.openPath(file); });
  // A file sent with a message onto the clipboard as Finder copies one (its file URL, in macOS's own format) with its path
  // as text, to paste as the file itself: 'file', or 'path' when that format is refused and only the path went.
  ipcMain.handle('agents-copy-file', async (event, file: unknown) => {
    if (!mine(event) || !isFile(file)) return false;
    const item = new ClipboardItem({ 'electron application/osclipboard;format="public.file-url"': pathToFileURL(file).href, 'text/plain': file });
    return clipboard.write([item]).then(() => 'file', () => clipboard.writeText(file).then(() => 'path'));
  });
  ipcMain.handle('agents-cloud', async (event, cwd: unknown, text: unknown, term: unknown) => {
    if (!mine(event) || !isDir(cwd) || typeof text !== 'string' || !text.trim() || text.length > 600) return false;
    return typeIn(term, cwd, `claude --cloud ${quote(text.replace(/\s+/g, ' ').trim())}`);
  });
  // Previews run in their own browser: a partition of their own that never gets the daemon key, no preload, no Node,
  // new windows go to the real browser, and every permission is refused.
  const web = session.fromPartition('persist:agents-web');
  web.setPermissionRequestHandler((_, permission, callback) => callback(permission === 'clipboard-sanitized-write' || permission === 'fullscreen'));
  async function open(id = '') {
    if (win && !win.isDestroyed()) { win.show(); win.focus(); if (id) win.webContents.send('agents-open-session', id); return; }
    await ensureHost(host, packaged);
    await readLang();
    win = new BrowserWindow({ width: 1180, height: 780, minWidth: 720, minHeight: 520, show: false, title: 'Agents',
      titleBarStyle: 'hiddenInset', trafficLightPosition: { x: 14, y: 14 }, backgroundColor: '#0c0d20',
      // Her sounds play before the window is first touched: a session finishing while the window just sits open chimes.
      webPreferences: { preload, contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: true, autoplayPolicy: 'no-user-gesture-required', webviewTag: true, plugins: true } });
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
    // B20: while the window is open the app is in the Dock and ⌘Tab, without a badge: who waits is by her in the window.
    win.on('closed', () => { win = null; publish(); app.dock?.hide(); });
    void app.dock?.show();
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
  // `id`: a session to open it on (Jarvis's notch, for one of Startrail's).
  ipcMain.on('agents-open', (event, id: unknown) => {
    if (trustedWindows().some(w => !w.isDestroyed() && event.sender === w.webContents && event.senderFrame === w.webContents.mainFrame)) void open(typeof id === 'string' && /^[\w-]{1,128}$/.test(id) ? id : '');
  });
  // Settings is a sheet inside the window, opened with ⌘, or the app menu's 设置… (one window: none of its own). The
  // rest of the menu is Electron's default, so the editing keys and ⌘W keep working.
  async function settingsSheet() {
    const fresh = !win || win.isDestroyed();
    await open();
    const w = win;
    if (!w || w.isDestroyed()) return;
    if (fresh || w.webContents.isLoading()) w.webContents.once('did-finish-load', () => w.webContents.send('agents-settings')); else w.webContents.send('agents-settings');
  }
  const buildMenu = () => process.platform === 'darwin' && Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: app.name, submenu: [{ role: 'about' }, { type: 'separator' }, { label: tr('设置…', 'Settings…'), accelerator: 'CommandOrControl+,', click: () => void settingsSheet() }, { type: 'separator' },
      { role: 'services' }, { type: 'separator' }, { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' }, { type: 'separator' }, { role: 'quit' }] },
    { role: 'fileMenu' }, { role: 'editMenu' }, { role: 'viewMenu' }, { role: 'windowMenu' },
  ]));
  buildMenu(); langChanged = buildMenu;
  void readLang(); setInterval(() => void readLang(), 60e3).unref();
  // 发一条试试 in 通知: one notification as a session's would look, shown even with the window in front; the dev build's
  // unsigned Electron says it through osascript when macOS refuses it.
  ipcMain.handle('agents-notify-test', (event, title: unknown, sub: unknown, body: unknown, id: unknown) => {
    if (!mine(event) || ![title, sub, body].every(v => typeof v === 'string' && v.length <= 400) || !Notification.isSupported()) return false;
    const n = new Notification({ title: title as string, subtitle: sub as string, body: body as string });
    if (typeof id === 'string' && id) n.on('click', () => void open(id));
    n.on('failed', () => { if (!app.isPackaged) script(title as string, sub as string, body as string); });
    n.show();
    return true;
  });
  // An open window chimes on its own, so its notifications are silent.
  watchHost(id => void open(id), () => !!win && !win.isDestroyed() && win.isFocused(), () => !!win && !win.isDestroyed());
  // The Dock icon, clicked, brings the window forward.
  app.on('activate', () => { if (win && !win.isDestroyed()) { win.show(); win.focus(); } });
  // ⌥Tab while B01 has the foreground: straight to its next session waiting on Allen.
  return { open: () => open(), reopen, next() { if (!presence().active) return false; win!.webContents.send('agents-next'); return true; } };
}

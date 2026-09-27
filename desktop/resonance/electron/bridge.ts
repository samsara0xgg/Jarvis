import { ipcMain, shell, type BrowserWindow } from 'electron';
import path from 'node:path';
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
// The daemon-facing IPC every Resonance window shares: plugin operations, opening a Codex
// thread, and Codex thread titles. The design lab and verification runs stay offline.
let codexTitles: Record<string, string> = {};
let codexTitlesAt = 0;
// The Usage page names a service; only these pages open. The renderer never passes a URL.
const ACCOUNT_PAGES: Record<string, string> = {
  claude: 'https://claude.ai/settings/usage',
  codex: 'https://chatgpt.com/codex/settings/usage',
  openai: 'https://platform.openai.com/settings/organization/billing/overview',
  deepseek: 'https://platform.deepseek.com/top_up',
  minimax: 'https://platform.minimax.io/user-center/payment/balance',
};
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// The local key every daemon route needs, read fresh so a first boot's key is picked up.
export const daemonToken = () => readFile(path.join(process.env.JARVIS_RUNTIME_ROOT ?? path.join(homedir(), '.jarvis'), 'plugin-access.json'), 'utf8')
  .then(text => JSON.parse(text).token as unknown).catch(() => undefined);
// The renderer never holds the key: its requests and sockets to the daemon get the header here.
export function sendDaemonKey(target: Electron.Session) {
  const daemonPort = process.env.JARVIS_INHERENT_BRIDGE_PORT ?? '8006';
  target.webRequest.onBeforeSendHeaders({ urls: ['http://127.0.0.1/*', 'ws://127.0.0.1/*'] }, (details, callback) => {
    if (new URL(details.url).port !== daemonPort) { callback({}); return; }
    void daemonToken().then(token =>
      callback({ requestHeaders: typeof token === 'string' && token ? { ...details.requestHeaders, Authorization: `Bearer ${token}` } : details.requestHeaders }));
  });
}
export function registerDaemonBridge(win: BrowserWindow, { lab = false, verification = false } = {}) {
  sendDaemonKey(win.webContents.session);
  // The renderer can request plugin operations but never read the daemon's
  // management credential or choose an arbitrary URL/file/process to open.
  ipcMain.handle('plugins', async (event, operation: string, data: Record<string, unknown> = {}) => {
    if (event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame) throw new Error('无效的插件窗口');
    const operations = ['read', 'icon', 'open', 'connect', 'cancel', 'reopen', 'disable', 'approval'];
    if (!operations.includes(operation) || !data || typeof data !== 'object' || Array.isArray(data)) throw new Error('无效的插件操作');
    const testPort = verification ? process.env.RESONANCE_PLUGIN_TEST_PORT : undefined;
    if (lab || (verification && (!testPort || testPort === '8006'))) throw new Error('此预览未连接插件服务');
    const port = testPort ?? process.env.JARVIS_INHERENT_BRIDGE_PORT ?? '8006';
    if (!/^\d{1,5}$/.test(port) || Number(port) > 65535) throw new Error('插件服务端口无效');
    const root = (verification ? process.env.RESONANCE_PLUGIN_TEST_ROOT : undefined) ?? process.env.JARVIS_RUNTIME_ROOT ?? path.join(homedir(), '.jarvis');
    let token: string;
    try { token = JSON.parse(await readFile(path.join(root, 'plugin-access.json'), 'utf8')).token; }
    catch { throw new Error('插件服务尚未就绪，请确认 Jarvis 后台已更新并启动'); }
    if (typeof token !== 'string' || !token) throw new Error('插件服务凭证无效');
    const body = JSON.stringify({ operation, data });
    if (body.length > 32768) throw new Error('插件请求过长');
    const get = operation === 'read' || operation === 'icon';
    const route = operation === 'read' ? '' : operation === 'icon' ? `/${encodeURIComponent(String(data.plugin_id))}/icon` : '/action';
    let response: Response;
    try {
      response = await fetch(`http://127.0.0.1:${port}/inherent/plugins${route}`, {
        method: get ? 'GET' : 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: get ? undefined : body,
        signal: AbortSignal.timeout(15000),
      });
    } catch { throw new Error('暂时连不上 Jarvis，请稍后重试'); }
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(response.status === 401 ? '插件服务凭证已更新，请重试' : typeof result.detail === 'string' ? result.detail : '插件操作未完成，请重试');
    return result;
  });
  ipcMain.handle('open-account', async (event, service) => {
    if (event.sender !== win.webContents || typeof service !== 'string' || !Object.hasOwn(ACCOUNT_PAGES, service)) return false;
    if (verification) return false;
    try { await shell.openExternal(ACCOUNT_PAGES[service]); return true; }
    catch { return false; }
  });
  // A Usage page write (ADR 0048/0050): main reads the desktop credential and posts to the
  // daemon; the renderer only names what to do.
  const usagePost = async (route: string, body: Record<string, unknown>, failed: string) => {
    if (lab || verification) throw new Error('This preview is not connected to Jarvis');
    const port = process.env.JARVIS_INHERENT_BRIDGE_PORT ?? '8006';
    const root = process.env.JARVIS_RUNTIME_ROOT ?? path.join(homedir(), '.jarvis');
    let token: unknown;
    try { token = JSON.parse(await readFile(path.join(root, 'plugin-access.json'), 'utf8')).token; } catch { /* checked below */ }
    if (typeof token !== 'string' || !token) throw new Error('Jarvis is not ready yet. Try again in a moment.');
    let response: Response;
    try {
      response = await fetch(`http://127.0.0.1:${port}/inherent/usage/${route}`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(30000),
      });
    } catch { throw new Error('Could not reach Jarvis. Try again.'); }
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(response.status === 404 ? 'Jarvis needs a restart for this' : typeof result.detail === 'string' ? result.detail : failed);
    return result;
  };
  const fromThisWindow = (event: Electron.IpcMainInvokeEvent) => event.sender === win.webContents && event.senderFrame === win.webContents.mainFrame;
  // ADR 0048: spend one Codex limit reset. The page confirms twice before it calls this; the
  // request id is minted once per confirmation, so a retry cannot spend a second reset.
  ipcMain.handle('usage-reset', async (event, service, requestId) => {
    if (!fromThisWindow(event)) throw new Error('Not this window');
    if (service !== 'codex' || typeof requestId !== 'string' || !UUID.test(requestId)) throw new Error('Invalid reset request');
    return usagePost('codex/reset', { request_id: requestId }, 'The reset did not go through. Try again.');
  });
  // ADR 0050: record a balance OpenAI or MiniMax will not report.
  ipcMain.handle('usage-balance', async (event, service, usd) => {
    if (!fromThisWindow(event)) throw new Error('Not this window');
    if ((service !== 'openai' && service !== 'minimax') || typeof usd !== 'number' || !Number.isFinite(usd) || usd < 0) throw new Error('Invalid balance');
    return usagePost('balance', { service, usd }, 'The balance was not saved. Try again.');
  });
  ipcMain.handle('open-codex', async (event, threadId) => {
    if (event.sender !== win.webContents || typeof threadId !== 'string'
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(threadId)) return false;
    // An OS handoff is not proof that the target conversation was displayed.
    if (verification || lab) return false;
    try { await shell.openExternal(`codex://threads/${threadId}`); return true; }
    catch { return false; }
  });
  ipcMain.handle('codex-titles', async (event, ids) => {
    if (event.sender !== win.webContents || !Array.isArray(ids) || ids.length > 10000 || !ids.every(id => typeof id === 'string')) return {};
    if (verification || lab) return {};
    if (Date.now() - codexTitlesAt > 10000) {
      try {
        const lines = await readFile(path.join(process.env.CODEX_HOME ?? path.join(homedir(), '.codex'), 'session_index.jsonl'), 'utf8');
        const titles: Record<string, string> = {};
        for (const line of lines.split('\n')) {
          try { const row = JSON.parse(line); if (typeof row.id === 'string' && typeof row.thread_name === 'string') titles[row.id] = row.thread_name; } catch { /* Partially appended index line. */ }
        }
        codexTitles = titles;
      } catch { /* Optional name index; the observed prompt is the fallback. */ }
      codexTitlesAt = Date.now();
    }
    return Object.fromEntries(ids.filter(id => Object.hasOwn(codexTitles, id)).map(id => [id, codexTitles[id]]));
  });
}

import { ipcMain, shell, type BrowserWindow } from 'electron';
import path from 'node:path';
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
// The daemon-facing IPC every Resonance window shares: plugin operations, opening a Codex
// thread, and Codex thread titles. The design lab and verification runs stay offline.
let codexTitles: Record<string, string> = {};
let codexTitlesAt = 0;
export function registerDaemonBridge(win: BrowserWindow, { lab = false, verification = false } = {}) {
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

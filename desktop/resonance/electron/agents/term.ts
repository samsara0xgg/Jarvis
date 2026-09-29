// ADR 0086: the workbench's terminal, one real shell per session in the session's folder. It lives here in the host, not
// in the window, so a command keeps running while the window closes or the companion restarts, and the window replays
// what it missed from a rolling buffer when it comes back. node-pty is loaded on first use: without it only the
// terminal is missing.
import { chmod, stat } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import type http from 'node:http';
import { SHELL_ENV } from './land.js';

type Pty = { onData(f: (d: string) => void): void; onExit(f: (e: { exitCode: number }) => void): void; write(d: string): void; resize(c: number, r: number): void; kill(): void; pid: number };
type Term = { pty: Pty; buf: string; subs: Set<http.ServerResponse>; cwd: string };
const terms = new Map<string, Term>();
const KEEP = 256 << 10;
let mod: { spawn(file: string, args: string[], o: Record<string, unknown>): Pty } | null | undefined;
async function load() {
  if (mod !== undefined) return mod;
  try {
    const require = createRequire(import.meta.url);
    const m = require('node-pty');
    // npm leaves the prebuilt spawn helper without its execute bit; without it every spawn fails.
    const helper = path.join(path.dirname(require.resolve('node-pty/package.json')), 'prebuilds', `${process.platform}-${process.arch}`, 'spawn-helper');
    const s = await stat(helper).catch(() => null);
    if (s && !(s.mode & 0o111)) await chmod(helper, 0o755);
    mod = m;
  } catch { mod = null; }
  return mod;
}
const send = (res: http.ServerResponse, data: unknown, event = '') => res.write(`${event ? `event: ${event}\n` : ''}data: ${JSON.stringify(data)}\n\n`);

export async function openTerm(id: string, cwd: string, cols: number, rows: number) {
  const have = terms.get(id);
  if (have) { have.pty.resize(cols, rows); return; }
  const m = await load();
  if (!m) throw new Error('终端用不了：node-pty 没装上');
  const env = { ...SHELL_ENV, TERM: 'xterm-256color', COLORTERM: 'truecolor', TERM_PROGRAM: 'Jarvis', LANG: SHELL_ENV.LANG || 'zh_CN.UTF-8' };
  const pty = m.spawn(SHELL_ENV.SHELL || '/bin/zsh', ['-l'], { name: 'xterm-256color', cols, rows, cwd, env });
  const t: Term = { pty, buf: '', subs: new Set(), cwd };
  terms.set(id, t);
  pty.onData(d => { t.buf = (t.buf + d).slice(-KEEP); for (const r of t.subs) send(r, d); });
  pty.onExit(e => { if (terms.get(id) === t) terms.delete(id); for (const r of t.subs) { send(r, e.exitCode, 'exit'); r.end(); } });
}
// The stream starts with everything still in the buffer, so a window that was away sees what happened meanwhile.
export function streamTerm(id: string, req: http.IncomingMessage, res: http.ServerResponse) {
  const t = terms.get(id);
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*' });
  if (!t) { send(res, 0, 'exit'); res.end(); return; }
  send(res, t.buf, 'replay');
  t.subs.add(res);
  req.on('close', () => t.subs.delete(res));
}
export function inputTerm(id: string, data: string) { terms.get(id)?.pty.write(data); }
export function resizeTerm(id: string, cols: number, rows: number) { terms.get(id)?.pty.resize(cols, rows); }
export function killTerm(id: string) { const t = terms.get(id); if (t) { terms.delete(id); t.pty.kill(); } }
export const hasTerm = (id: string) => terms.has(id);
// A comment every 20 s keeps each terminal stream open through quiet stretches.
setInterval(() => { for (const t of terms.values()) for (const r of t.subs) r.write(': \n\n'); }, 20000).unref();

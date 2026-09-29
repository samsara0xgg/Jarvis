// The keeper (ADR 0082): a small process that owns every Claude Code child the agent host drives, so a session keeps
// running while the host restarts. The host starts it when nothing answers on its socket, and each session is one
// connection: the first line says which session, and after it the connection is that child's stdin and stdout. When
// the host comes back, it opens the same session again and gets what it needs to carry on: the lines of the turn it
// missed, requests still waiting for an answer, and the child's first handshake answered again without asking it.
// Keep this file small and rarely changed: restarting the keeper is the one thing that still ends running turns.
import { spawn, type ChildProcess } from 'node:child_process';
import { createHash } from 'node:crypto';
import net from 'node:net';
import { closeSync, lstatSync, mkdirSync, openSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export type Open = { command: string; args: string[]; cwd?: string; env: Record<string, string | undefined> };
// What each connection sends first; `open` is answered with Head, then the replayed lines, then the live ones.
export type Hello = { op: 'list' } | { op: 'kill'; key: string } | { op: 'open'; key: string; spawn: Open };
export type Head = { fresh: boolean; busy: boolean; replay: number };
export type Kid = { key: string; pid: number; busy: boolean };

const log = (...a: unknown[]) => console.log(new Date().toISOString(), ...a);
export function lines(stream: NodeJS.ReadableStream, on: (l: string) => void) {
  let buf = '';
  stream.setEncoding('utf8');
  stream.on('data', (d: string) => {
    buf += d;
    for (let i; (i = buf.indexOf('\n')) >= 0;) { const l = buf.slice(0, i); buf = buf.slice(i + 1); if (l.trim()) on(l); }
  });
}
export const parse = (l: string): Record<string, any> => { try { return JSON.parse(l); } catch { return {}; } };

// turn: what the child said since its last result, and requests still open; init: its answer to the first handshake
type Child = { key: string; child: ChildProcess; turn: string[]; init?: Record<string, any>; initId?: string; busy: boolean; conn?: net.Socket };
const kids = new Map<string, Child>();

function fromChild(k: Child, line: string) {
  const m = parse(line);
  if (m.type === 'control_response') {
    if (k.initId && m.response?.request_id === k.initId) { k.init = m; k.initId = undefined; }
  } else if (m.type === 'control_cancel_request') {
    k.turn = k.turn.filter(l => parse(l).request_id !== m.request_id);
  } else if (m.type === 'result') {
    k.busy = false;
    k.turn = [...k.turn.filter(l => parse(l).type === 'control_request'), line];
  } else if (m.type !== 'stream_event') k.turn.push(line);
  k.conn?.write(`${line}\n`);
}
function toChild(k: Child, line: string) {
  const m = parse(line);
  if (m.type === 'control_request' && m.request?.subtype === 'initialize') {
    if (k.init) { k.conn?.write(`${JSON.stringify({ ...k.init, response: { ...k.init.response, request_id: m.request_id } })}\n`); return; }
    k.initId = m.request_id;
  } else if (m.type === 'control_response') {
    const id = m.response?.request_id;
    k.turn = k.turn.filter(l => { const x = parse(l); return !(x.type === 'control_request' && x.request_id === id); });
  } else if (m.type === 'user') k.busy = true;
  k.child.stdin?.write(`${line}\n`);
}

function serve(sock: string) {
  mkdirSync(path.dirname(sock), { recursive: true });
  const server = net.createServer(conn => {
    conn.on('error', () => {});
    let head = '';
    const first = (d: Buffer) => {
      head += d.toString('utf8');
      const i = head.indexOf('\n');
      if (i < 0) return;
      conn.off('data', first);
      const h = parse(head.slice(0, i)) as Hello, rest = head.slice(i + 1);
      if (h.op === 'list') { conn.end(`${JSON.stringify([...kids.values()].map(k => ({ key: k.key, pid: k.child.pid, busy: k.busy })))}\n`); return; }
      if (h.op === 'kill') { kids.get(h.key)?.child.kill('SIGTERM'); conn.end('{}\n'); return; }
      if (h.op !== 'open') { conn.destroy(); return; }
      let k = kids.get(h.key);
      const fresh = !k;
      if (!k) {
        const child = spawn(h.spawn.command, h.spawn.args, { cwd: h.spawn.cwd, env: h.spawn.env, stdio: ['pipe', 'pipe', 'pipe'] });
        const kid: Child = k = { key: h.key, child, turn: [], busy: false };
        kids.set(h.key, kid);
        lines(child.stdout!, l => fromChild(kid, l));
        child.stderr!.setEncoding('utf8').on('data', (d: string) => log(h.key.slice(0, 8), d.trim().slice(0, 400)));
        // A line on its way to a child that just died is dropped, not the keeper with every other child in it.
        child.stdin!.on('error', e => log(h.key.slice(0, 8), 'stdin', e.message));
        child.on('error', e => log(h.key.slice(0, 8), 'spawn', e.message));
        child.on('exit', (code, sig) => { log(h.key.slice(0, 8), 'exited', code, sig); if (kids.get(h.key) === kid) kids.delete(h.key); kid.conn?.end(); });
        log(h.key.slice(0, 8), 'started', child.pid);
      } else {
        k.conn?.destroy();
        log(h.key.slice(0, 8), 'reattached', k.turn.length, 'lines', k.busy ? 'busy' : 'idle');
      }
      const kid = k;
      kid.conn = conn;
      conn.write(`${JSON.stringify({ fresh, busy: kid.busy, replay: fresh ? 0 : kid.turn.length } satisfies Head)}\n`);
      if (!fresh) for (const l of kid.turn) conn.write(`${l}\n`);
      lines(conn, l => toChild(kid, l));
      if (rest) conn.emit('data', rest);
      conn.on('close', () => { if (kid.conn === conn) kid.conn = undefined; });
    };
    conn.on('data', first);
  });
  // A socket file nobody answers on is left from a keeper that died; a live one means this keeper is not needed.
  server.on('error', e => { log('listen', e); process.exit(1); });
  net.connect(sock).on('connect', () => { log('a keeper already answers'); process.exit(0); })
    .on('error', () => { rmSync(sock, { force: true }); server.listen(sock, () => log('keeper on', sock)); });
  // Stopping the keeper stops its children: they are in its process group.
  let stopping = false;
  for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP'] as const) process.on(sig, () => {
    if (stopping) return;
    stopping = true;
    try { process.kill(-process.pid, 'SIGTERM'); } catch { /* not a group leader: started by hand */ }
    process.exit(0);
  });
}

// ---------- the host's side ----------
// Where the keeper answers for an agents folder: keeper.sock in it, unless that path is longer than a socket address
// holds (104 bytes on macOS, its end included). Then a socket named for the folder, in a folder under the temp folder
// that only this user can enter; when that folder is not so, the path stays as it was and the keeper says it cannot
// listen there.
export function socketFor(dir: string) {
  const own = path.join(dir, 'keeper.sock');
  if (Buffer.byteLength(own) < 104) return own;
  const uid = process.getuid?.() ?? 0, at = path.join(tmpdir(), `jarvis-agents-${uid}`);
  try {
    mkdirSync(at, { recursive: true, mode: 0o700 });
    const st = lstatSync(at);
    if (st.isDirectory() && st.uid === uid && !(st.mode & 0o077)) return path.join(at, `${createHash('sha256').update(path.resolve(dir)).digest('hex').slice(0, 16)}.sock`);
  } catch { /* the keeper's own error says where it could not listen */ }
  return own;
}
// One question, one line back.
export function ask<T>(sock: string, h: Hello) {
  return new Promise<T>((ok, no) => {
    const c = net.connect(sock);
    let buf = '';
    c.on('error', no).on('data', d => { buf += d; const i = buf.indexOf('\n'); if (i >= 0) { c.destroy(); ok(parse(buf.slice(0, i)) as T); } });
    c.write(`${JSON.stringify(h)}\n`);
  });
}
// Starts the keeper when nothing answers; it outlives the host, so it gets its own process group and log. Its command
// line names the agents folder first, so the keeper of a folder can be found by it wherever its socket is.
export async function startKeeper(dir: string, sock: string, logFile: string) {
  if (await ask(sock, { op: 'list' }).then(() => true, () => false)) return;
  mkdirSync(path.dirname(logFile), { recursive: true });
  const out = openSync(logFile, 'a');
  spawn(process.execPath, [fileURLToPath(new URL('./keeper.js', import.meta.url)), dir, sock], { detached: true, stdio: ['ignore', out, out], env: process.env }).unref();
  closeSync(out);
  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 100));
    if (await ask(sock, { op: 'list' }).then(() => true, () => false)) return;
  }
  throw new Error('the keeper did not start');
}

// `keeper.js <agents folder> <socket>`; a host from before names only the socket.
if (process.argv[1]?.endsWith('keeper.js')) serve(process.argv[3] ?? process.argv[2]);

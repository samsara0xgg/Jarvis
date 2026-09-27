import { app } from 'electron';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { closeSync, mkdirSync, openSync } from 'node:fs';
import { createServer, type AddressInfo } from 'node:net';
import { homedir } from 'node:os';
import path from 'node:path';
import { daemonToken } from './bridge.js';
// The installed app runs its own daemon: the bundled Python in Resources, started again whenever it
// exits (the daemon ends itself to restart after setup or a model download). A daemon that already
// answers on 8006 with this user's key is used as it is, so a developer's own is never doubled;
// another account's daemon may hold 8006, and then this one takes a free port.
const answers = async (port: string) => {
  try {
    const r = await fetch(`http://127.0.0.1:${port}/inherent/setup`, { headers: { Authorization: `Bearer ${await daemonToken()}` }, signal: AbortSignal.timeout(2000) });
    return r.ok;
  } catch { return false; }
};
const free = (port: number) => new Promise<number>(done => {
  const probe = createServer().once('error', () => done(0));
  probe.listen(port, '127.0.0.1', () => { const got = (probe.address() as AddressInfo).port; probe.close(() => done(got)); });
});
const wait = (ms: number) => new Promise(done => setTimeout(done, ms));
export async function startDaemon(): Promise<string> {
  // A daemon left behind by a force-quit app runs with no one to restart it: end it and start over.
  const python = path.join(process.resourcesPath, 'python/bin/python3'), mine = ['-U', String(process.getuid!()), '-f', `${python} -m jarvis serve`];
  if (spawnSync('pkill', mine).status === 0) for (let i = 0; i < 40 && spawnSync('pgrep', mine).status === 0; i++) await wait(250);
  if (await answers('8006')) return '8006';
  const port = String(await free(8006) || await free(0));
  const root = process.env.JARVIS_RUNTIME_ROOT ?? path.join(homedir(), '.jarvis'), logs = path.join(root, 'logs');
  mkdirSync(logs, { recursive: true, mode: 0o700 });
  const runtime = path.join(process.resourcesPath, 'runtime');
  const env = { ...process.env,
    // An app started from Finder gets only the system PATH; git, codex, claude and uvx live in these.
    PATH: [path.join(homedir(), '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin', process.env.PATH || '/usr/bin:/bin:/usr/sbin:/sbin'].join(':'),
    PYTHONPATH: runtime, PYTHONNOUSERSITE: '1',
    // Nothing may write into the signed bundle; its bytecode is compiled at build time.
    PYTHONDONTWRITEBYTECODE: '1',
    // The marker the LaunchAgent sets: something starts this daemon again, so its restarts are on.
    JARVIS_LAUNCHD_AGENT: 'com.allen.jarvis' };
  let child: ChildProcess | undefined, quitting = false, delay = 1000;
  const run = () => {
    const started = Date.now(), out = openSync(path.join(logs, 'daemon.out.log'), 'a'), err = openSync(path.join(logs, 'daemon.err.log'), 'a');
    child = spawn(python, ['-m', 'jarvis', 'serve', '--port', port],{ cwd: runtime, env, stdio: ['ignore', out, err] });
    closeSync(out); closeSync(err);
    child.on('exit', () => {
      if (quitting) return;
      // One that ran a while comes back at once; a crash loop backs off to a minute.
      delay = Date.now() - started > 60_000 ? 1000 : Math.min(delay * 2, 60_000);
      setTimeout(run, delay);
    });
  };
  run();
  app.on('will-quit', () => { quitting = true; child?.kill(); });
  for (const until = Date.now() + 60_000; Date.now() < until && !await answers(port);) await wait(500);
  return port;
}

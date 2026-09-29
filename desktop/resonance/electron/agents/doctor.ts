// The host's check-up (GET /doctor): what it runs Claude and Codex with, whether each can sign in, and the tools it
// found. An app started from Finder gets only the system PATH; the owner's login shell knows where nvm, mise or
// Homebrew put codex and git, so the host asks it once as it starts.
import { execFile } from 'node:child_process';
import { access, constants } from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { log } from './host.js';

const exec = promisify(execFile);
export async function loginPath() {
  const shell = process.env.SHELL || '/bin/zsh', mark = '__JARVIS_PATH__';
  try {
    const { stdout } = await exec(shell, ['-ilc', `printf '%s%s%s' '${mark}' "$PATH" '${mark}'`], { timeout: 8000, env: { ...process.env, TERM: 'dumb' } });
    const found = new RegExp(`${mark}(.*?)${mark}`).exec(stdout)?.[1];
    if (!found) return;
    const have = (process.env.PATH ?? '').split(':').filter(Boolean);
    process.env.PATH = [...new Set([...have, ...found.split(':').filter(p => p.startsWith('/'))])].join(':');
  } catch (e) { log('login shell PATH', String(e).slice(0, 200)); }
}
// The first executable of that name on PATH.
export async function which(name: string) {
  for (const dir of (process.env.PATH ?? '').split(':').filter(Boolean)) {
    const p = path.join(dir, name);
    if (await access(p, constants.X_OK).then(() => true, () => false)) return p;
  }
  return undefined;
}
export async function version(exe: string) {
  try { return (await exec(exe, ['--version'], { timeout: 10000 })).stdout.trim().split('\n')[0]; } catch { return undefined; }
}

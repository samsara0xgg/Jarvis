// What a session changed, file by file, for the window's review (GET /sessions/{id}/changes): against what landing
// compares (ADR 0085: main for its own worktree, origin for main itself), else against the last commit. One file's
// diff, and one file put back the way the base has it, with a copy of what was there kept first.
import { execFile } from 'node:child_process';
import { copyFile, mkdir, rm, stat } from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { diffLines, Refused } from './files.js';
import { changesOf } from './land.js';
import type { Change, Diff } from './types.js';
import type { Session } from './host.js';

const exec = promisify(execFile);
const git = async (cwd: string, ...args: string[]) => (await exec('git', ['-C', cwd, ...args], { maxBuffer: 64 << 20 })).stdout;

export async function baseOf(x: Session) {
  const c = await changesOf(x).catch(() => null);
  if (c) return { top: c.top, base: c.base };
  const top = (await git(x.s.cwd, 'rev-parse', '--show-toplevel').catch(() => '')).trim();
  if (!top) throw new Refused(409, '这个会话的文件夹不在 git 里');
  return { top, base: 'HEAD' };
}
export async function changes(x: Session): Promise<{ base: string; top: string; files: Change[] }> {
  const { top, base } = await baseOf(x), files = new Map<string, Change>();
  const nums = (await git(top, 'diff', '--numstat', '--no-renames', '-z', base)).split('\0');
  const sts = (await git(top, 'diff', '--name-status', '--no-renames', '-z', base)).split('\0');
  for (let i = 0; i + 1 < sts.length; i += 2) if (/^[MAD]$/.test(sts[i][0] ?? '')) files.set(sts[i + 1], { path: sts[i + 1], add: 0, del: 0, st: sts[i][0] as Change['st'] });
  for (const e of nums) {
    const m = /^(\d+|-)\t(\d+|-)\t(.+)$/s.exec(e), f = m && files.get(m[3]);
    if (f) Object.assign(f, { add: m![1] === '-' ? 0 : Number(m![1]), del: m![2] === '-' ? 0 : Number(m![2]) });
  }
  for (const e of (await git(top, 'status', '--porcelain=v1', '-z', '--untracked-files=all')).split('\0')) {
    // A folder here is another repository inside this one (a nested worktree).
    if (!e.startsWith('?? ') || e.endsWith('/')) continue;
    const p = e.slice(3), n = await git(top, 'diff', '--no-index', '--numstat', '--', '/dev/null', p).catch(r => String((r as { stdout?: string }).stdout ?? ''));
    files.set(p, { path: p, add: Number(/^(\d+)/.exec(n)?.[1] ?? 0), del: 0, st: '?' });
  }
  return { base, top, files: [...files.values()].sort((a, b) => a.path.localeCompare(b.path)) };
}
const inside = (top: string, file: string) => {
  const abs = path.resolve(top, file);
  if (abs !== top && !abs.startsWith(`${top}/`)) throw new Refused(400, '这个文件不在会话的仓库里');
  return path.relative(top, abs);
};
export async function fileDiff(x: Session, file: string): Promise<{ path: string; diff: Diff; add: number; del: number }> {
  const { top, base } = await baseOf(x), rel = inside(top, file);
  const tracked = await git(top, 'ls-files', '--error-unmatch', '--', rel).then(() => true, () => false);
  const raw = tracked || (await git(top, 'cat-file', '-e', `${base}:${rel}`).then(() => true, () => false))
    ? await git(top, 'diff', '--no-color', '-U3', base, '--', rel)
    : await git(top, 'diff', '--no-color', '--no-index', '-U3', '--', '/dev/null', rel).catch(r => String((r as { stdout?: string }).stdout ?? ''));
  const diff = diffLines(raw);
  return { path: rel, diff: diff.slice(0, 20000), add: diff.filter(d => d[0] === '+').length, del: diff.filter(d => d[0] === '-').length };
}
// The file goes back to the base's version (or away, when the base has none); what it held is copied to `trash` first.
export async function revert(x: Session, file: string, trash: string) {
  const { top, base } = await baseOf(x), rel = inside(top, file), abs = path.join(top, rel);
  let kept = '';
  if ((await stat(abs).catch(() => null))?.isFile()) {
    kept = path.join(trash, `${Date.now()}`, rel);
    await mkdir(path.dirname(kept), { recursive: true });
    await copyFile(abs, kept);
  }
  if (await git(top, 'cat-file', '-e', `${base}:${rel}`).then(() => true, () => false)) await git(top, 'checkout', base, '--', rel);
  else {
    await git(top, 'rm', '-q', '--cached', '--ignore-unmatch', '--', rel);
    await rm(abs, { force: true });
  }
  return { kept };
}

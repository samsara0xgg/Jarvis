// The files around a session, as the window needs them: finding one to mention with @, opening one in the preview,
// telling which of the paths an answer names are there, and keeping a copy of a file sent with a message.
import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { createReadStream } from 'node:fs';
import { mkdir, open, readdir, readFile, realpath, rmdir, stat, unlink, writeFile } from 'node:fs/promises';
import type { ServerResponse } from 'node:http';
import { homedir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { promisify } from 'node:util';
import { inflateSync } from 'node:zlib';
import type { Diff, Peek } from './types.js';

const exec = promisify(execFile);
// What every git the host runs starts with: reading never takes the index lock. A status, or a diff that meets a file
// only touched, otherwise holds .git/index.lock while it runs, and a commit the agent or the owner makes in that
// checkout at the same moment fails on it. Writes still take the lock they need.
export const GIT = ['--no-optional-locks', '-c', 'diff.autoRefreshIndex=false'];
const git = async (cwd: string, ...args: string[]) => (await exec('git', [...GIT, '-C', cwd, ...args], { maxBuffer: 64 << 20 })).stdout;
export class Refused extends Error { constructor(public status: number, msg: string) { super(msg); } }

// ---------- @: every file and folder, matched by the letters typed in order ----------
// Outside git the folder is walked, past what is never worth mentioning, to a limit.
const HEAVY = new Set(['node_modules', '.git', '.hg', '.svn', 'dist', 'build', 'out', '.next', '.nuxt', 'target', '.venv', 'venv', '__pycache__',
  '.mypy_cache', '.pytest_cache', '.ruff_cache', '.cache', 'Pods', 'DerivedData', '.gradle', '.idea', 'coverage', '.turbo', '.parcel-cache']);
async function walk(root: string, max = 20000) {
  const out: string[] = [], queue = [''];
  while (queue.length && out.length < max) {
    const rel = queue.shift()!;
    const ds = await readdir(path.join(root, rel), { withFileTypes: true }).catch(() => []);
    for (const d of ds) {
      if (HEAVY.has(d.name) || d.name === '.DS_Store') continue;
      const p = rel ? `${rel}/${d.name}` : d.name;
      if (d.isDirectory()) { out.push(`${p}/`); if (p.split('/').length < 12) queue.push(p); }
      else if (d.isFile() || d.isSymbolicLink()) out.push(p);
      if (out.length >= max) break;
    }
  }
  return out;
}
const lists = new Map<string, { at: number; list: string[] }>();
async function listOf(cwd: string) {
  let c = lists.get(cwd);
  if (!c || Date.now() - c.at > 15000) {
    const out = await git(cwd, 'ls-files', '-co', '--exclude-standard').catch(() => null);
    let list: string[];
    if (out === null) list = await walk(cwd);
    else {
      const files = out.split('\n').filter(Boolean), dirs = new Set<string>();
      for (const f of files) for (let i = f.indexOf('/'); i >= 0; i = f.indexOf('/', i + 1)) dirs.add(f.slice(0, i + 1));
      list = [...dirs, ...files];
    }
    c = { at: Date.now(), list };
    lists.set(cwd, c);
  }
  return c.list;
}
// Every letter of `q` in order; runs, the start of a word and the name itself (not its folders) count more.
export function fuzzy(q: string, s: string) {
  const t = s.toLowerCase(), name = t.lastIndexOf('/', t.length - 2) + 1;
  let score = 0, j = 0, run = 0, last = -2;
  for (let i = 0; i < t.length && j < q.length; i++) {
    if (t[i] !== q[j]) continue;
    run = i === last + 1 ? run + 1 : 1;
    score += 1 + run * 2 + (i === 0 || '/-_. '.includes(t[i - 1]) ? 4 : 0) + (i >= name ? 3 : 0);
    last = i; j++;
  }
  return j === q.length ? score - t.length * .05 : -1;
}
// Folders end in `/`.
export async function findFiles(cwd: string, q: string, n = 50) {
  const list = await listOf(cwd), want = q.toLowerCase().replace(/\s+/g, '');
  if (!want) return list.filter(p => !p.endsWith('/')).sort((a, b) => a.length - b.length).slice(0, n);
  return list.map(p => [p, fuzzy(want, p)] as const).filter(x => x[1] >= 0).sort((a, b) => b[1] - a[1] || a[0].length - b[0].length).slice(0, n).map(x => x[0]);
}

// ---------- a path an answer or a step named ----------
// Relative to the session's folder, with `~`, a file:// address, or `:12`, `:12:4`, `#L12` after it.
export function parseRef(cwd: string, ref: string) {
  const raw = decodeURIComponent(ref.replace(/^file:\/\//, '')).trim();
  const m = /(?:#L(\d+)(?:-L?\d+)?|:(\d+)(?::\d+)?(?:-\d+)?)$/.exec(raw);
  const clean = (m ? raw.slice(0, m.index) : raw).replace(/^~(?=\/|$)/, homedir());
  return { abs: clean ? path.resolve(cwd, clean) : '', line: m ? Number(m[1] ?? m[2]) : undefined };
}
// Which of these exist, and whether each is a file or a folder: an answer's paths become links only when they do.
export async function resolveRefs(cwd: string, refs: string[]) {
  const found: Record<string, { abs: string; dir: boolean; line?: number }> = {};
  await Promise.all(refs.slice(0, 200).map(async ref => {
    const { abs, line } = parseRef(cwd, ref), st = abs ? await stat(abs).catch(() => null) : null;
    if (st) found[ref] = { abs, dir: st.isDirectory(), ...line ? { line } : {} };
  }));
  return found;
}

// ---------- the preview ----------
// Pages, PDFs and images open in the preview's own browser, audio and video play there, markdown and text come as text
// (the last megabyte of a big one), with what changed in it against `base`, a folder as its entries, and anything else
// goes to Quick Look. `roots`: the session's folders; a picture, a sound, a video or a PDF inside them can be had as
// bytes (sendFile), and the preview shows it itself.
const WEB = /\.(html?|pdf|png|jpe?g|gif|webp|avif|svg|bmp|ico)$/i, MEDIA = /\.(mp4|m4v|mov|webm|ogv|mp3|m4a|aac|wav|ogg|oga|flac|opus)$/i, MD = /\.(md|markdown|mdx)$/i;
const BIG = 2 << 20, TAIL = 1 << 20;
export async function peek(cwd: string, ref: string, base: () => Promise<string>, roots: string[] = []): Promise<Peek> {
  const { abs, line } = parseRef(cwd, ref);
  if (!abs) throw new Refused(400, 'ref 不对');
  const st = await stat(abs).catch(() => null), at = line ? { line } : {};
  if (!st) throw new Refused(404, `找不到 ${ref}`);
  if (st.isDirectory()) {
    const ds = await readdir(abs, { withFileTypes: true });
    const entries = ds.filter(d => d.name !== '.DS_Store').map(d => ({ name: d.name, dir: d.isDirectory() }))
      .sort((a, b) => Number(b.dir) - Number(a.dir) || a.name.localeCompare(b.name)).slice(0, 500);
    return { kind: 'dir', abs, entries };
  }
  if (!st.isFile()) throw new Refused(415, `${ref} 不是文件`);
  const size = st.size;
  if (WEB.test(abs) || MEDIA.test(abs)) {
    const real = await within(roots, abs), bytes = !!real && !!typeOf(real), pages = bytes && /\.pdf$/i.test(real) ? await pdfPages(real, size) : undefined;
    return { kind: WEB.test(abs) ? 'web' : 'media', abs, url: pathToFileURL(abs).href, size, ...bytes ? { bytes } : {}, ...pages ? { pages } : {} };
  }
  const cut = size > BIG, fh = await open(abs, 'r');
  let buf: Buffer;
  try { buf = Buffer.alloc(Math.min(size, cut ? TAIL : BIG)); await fh.read(buf, 0, buf.length, cut ? size - TAIL : 0); }
  finally { await fh.close(); }
  if (buf.subarray(0, 8192).includes(0)) return { kind: 'quicklook', abs, size };
  let text = buf.toString('utf8');
  if (cut) return { kind: 'text', abs, size, text: text.slice(text.indexOf('\n') + 1), cut: true, ...at };
  if (MD.test(abs)) return { kind: 'md', abs, size, text, ...at };
  const raw = await git(path.dirname(abs), 'diff', '--no-color', '-U3', await base(), '--', abs).catch(() => '');
  const diff = diffLines(raw), add = diff.filter(d => d[0] === '+').length, del = diff.filter(d => d[0] === '-').length;
  return { kind: 'text', abs, size, text, ...at, ...(add + del ? { diff: diff.slice(0, 4000), add, del, hunks: hunksOf(raw) } : {}) };
}
// `git diff` output as the window's lines, a ⋯ between hunks.
export function diffLines(raw: string): Diff {
  const diff: Diff = [];
  for (const l of raw.split('\n')) {
    if (/^(diff |index |--- |\+\+\+ |new file|deleted file|similarity|rename |old mode|new mode|Binary files|\\ )/.test(l)) continue;
    if (l.startsWith('@@')) { if (diff.length) diff.push([' ', '⋯']); continue; }
    if (l[0] === '+' || l[0] === '-' || l[0] === ' ') diff.push([l[0], l.slice(1)]);
  }
  return diff;
}
// Where each hunk starts in the file as it is now, so the window can number the lines of diffLines.
export const hunksOf = (raw: string) => [...raw.matchAll(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/gm)].map(m => Number(m[1]));
// How many pages a PDF says it has: its page tree's root counts them (/Type /Pages … /Count n), written in the file or
// inside one of its compressed object streams. Undefined when it does not say.
async function pdfPages(file: string, size: number) {
  if (size > 64 << 20) return undefined;
  const s = (await readFile(file).catch(() => Buffer.alloc(0))).toString('latin1');
  const count = (t: string) => {
    let n = 0;
    for (const m of t.matchAll(/\/Type\s*\/Pages\b/g)) {
      const from = t.lastIndexOf('<<', m.index), to = t.indexOf('>>', m.index), c = /\/Count\s+(\d+)/.exec(t.slice(from, to < 0 ? undefined : to));
      if (c) n = Math.max(n, Number(c[1]));
    }
    return n;
  };
  let n = count(s);
  for (const m of n ? [] : s.matchAll(/\/Type\s*\/ObjStm\b[^]*?stream\r?\n/g)) {
    const at = m.index + m[0].length, end = s.indexOf('endstream', at);
    if (end < 0) continue;
    try { n = Math.max(n, count(inflateSync(Buffer.from(s.slice(at, end), 'latin1')).toString('latin1'))); } catch { /* not deflated */ }
  }
  return n || undefined;
}

// ---------- a file's own bytes, for the preview's pictures, sound, video and PDFs ----------
// A security boundary: a file's bytes go to the window only when, with every link resolved, it is inside the session's
// folder or a folder the session was given (C5), and only a kind the preview shows itself. A part of it when asked for
// one, so a video can seek.
const TYPE: Record<string, string> = {
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp', avif: 'image/avif', svg: 'image/svg+xml', bmp: 'image/bmp', ico: 'image/x-icon',
  mp4: 'video/mp4', m4v: 'video/mp4', mov: 'video/quicktime', webm: 'video/webm', ogv: 'video/ogg',
  mp3: 'audio/mpeg', m4a: 'audio/mp4', aac: 'audio/aac', wav: 'audio/wav', ogg: 'audio/ogg', oga: 'audio/ogg', flac: 'audio/flac', opus: 'audio/ogg', pdf: 'application/pdf',
};
const typeOf = (p: string) => TYPE[path.extname(p).slice(1).toLowerCase()];
// The file itself (its links resolved) when that is inside one of `roots` (theirs resolved too), else ''.
async function within(roots: string[], abs: string) {
  const real = await realpath(abs).catch(() => '');
  if (!real) return '';
  for (const r of roots) {
    const top = await realpath(r).catch(() => '');
    if (top && (real === top || real.startsWith(top.endsWith('/') ? top : `${top}/`))) return real;
  }
  return '';
}
export async function sendFile(res: ServerResponse, roots: string[], cwd: string, ref: string, range = '') {
  const { abs } = parseRef(cwd, ref);
  if (!abs) throw new Refused(400, 'ref 不对');
  const real = await within(roots, abs);
  if (!real) throw await stat(abs).then(() => new Refused(403, `${ref} 不在这个会话的文件夹里`), () => new Refused(404, `找不到 ${ref}`));
  const type = typeOf(real), st = await stat(real);
  if (!type || !st.isFile()) throw new Refused(415, `${ref} 不在这里打开`);
  const size = st.size, m = /^bytes=(\d*)-(\d*)$/.exec(range.trim()), head = { 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff', 'Access-Control-Allow-Origin': '*' };
  let start = 0, end = size - 1, part = false;
  if (m && (m[1] || m[2])) {
    if (m[1]) { start = Number(m[1]); end = m[2] ? Math.min(Number(m[2]), size - 1) : size - 1; } else start = Math.max(0, size - Number(m[2]));
    if (start >= size || start > end) { res.writeHead(416, { ...head, 'Content-Range': `bytes */${size}` }); res.end(); return; }
    part = true;
  }
  // A picture drawn by its own address never runs what an SVG carries.
  res.writeHead(part ? 206 : 200, { ...head, 'Content-Type': type, 'Content-Length': size ? end - start + 1 : 0, ...part ? { 'Content-Range': `bytes ${start}-${end}/${size}` } : {},
    ...type === 'image/svg+xml' ? { 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; sandbox" } : {} });
  if (!size) { res.end(); return; }
  createReadStream(real, { start, end }).on('error', () => res.destroy()).pipe(res);
}

// ---------- files sent with a message ----------
// Pictures go to the agent as pictures; any other file sent as data is kept here, by its content, and the message
// names where. A copy written more than 30 days ago goes when the host starts (pruneOld).
export async function keepUpload(dir: string, name: string, url: string) {
  const m = /^data:[^;,]*(?:;[^,]*)?,/.exec(url);
  if (!m) throw new Refused(400, `${name} 不对`);
  const buf = m[0].includes(';base64') ? Buffer.from(url.slice(m[0].length), 'base64') : Buffer.from(decodeURIComponent(url.slice(m[0].length)));
  const safe = path.basename(name).replace(/[\0/:]/g, '_').slice(0, 200) || 'file';
  const at = path.join(dir, createHash('sha256').update(buf).digest('hex').slice(0, 16));
  await mkdir(at, { recursive: true });
  const file = path.join(at, safe);
  await writeFile(file, buf, { flag: 'wx' }).catch(e => { if ((e as NodeJS.ErrnoException).code !== 'EEXIST') throw e; });
  return file;
}
// Also what review kept of a file it put back (review.ts): each goes 30 days after it was written, with the folders
// it leaves empty.
export async function pruneOld(dir: string, top = true) {
  for (const d of await readdir(dir, { withFileTypes: true }).catch(() => [])) {
    const p = path.join(dir, d.name);
    if (d.isDirectory()) await pruneOld(p, false);
    else if (Date.now() - ((await stat(p).catch(() => null))?.mtimeMs ?? Date.now()) > 30 * 864e5) await unlink(p).catch(() => {});
  }
  if (!top) await rmdir(dir).catch(() => {});
}
// The line a message carries for files that are not pictures, and reading it back.
export const ATTACHED = '\n\nAttached files:\n';
export function attach(text: string, paths: string[]) { return paths.length ? `${text}${ATTACHED}${paths.map(p => `- ${p}`).join('\n')}` : text; }
export function attached(text: string): { text: string; paths: string[] } {
  const i = text.lastIndexOf(ATTACHED);
  if (i < 0) return { text, paths: [] };
  const rest = text.slice(i + ATTACHED.length).split('\n');
  if (!rest.every(l => l.startsWith('- '))) return { text, paths: [] };
  return { text: text.slice(0, i), paths: rest.map(l => l.slice(2)) };
}

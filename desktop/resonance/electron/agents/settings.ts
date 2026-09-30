// What the owner sets for the agent host (GET /settings, POST /settings), and how Startrail's own Claude sessions sign
// in (ADR 0094). In the packaged app those sessions run on the owner's own account with Anthropic (an API key), Amazon
// Bedrock or Google Vertex, handed only to the claude processes Startrail starts: Claude Code's own login on this Mac is
// left alone, so the owner's claude in a terminal keeps it. The dev build keeps Allen's subscription. The key sits in the
// login Keychain the way the daemon keeps its own (jarvis/deployment: service Jarvis, one item per account path, written
// through `security -i` so it never shows in a process list), in an item of its own so the daemon never loads it.
import { execFile, spawn } from 'node:child_process';
import { realpathSync } from 'node:fs';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { agentsDir } from './key.js';
import type { Auth, Settings } from './types.js';

const exec = promisify(execFile);
export const PACKAGED = process.env.JARVIS_AGENTS_PACKAGED === '1';
const DIR = path.resolve(agentsDir()), FILE = path.join(DIR, 'settings.json');
// The Keychain item's account: this folder with its links resolved, as the daemon names it when it erases everything
// (jarvis/deployment resolves the runtime root). The host has made the folder before anything asks.
const ACCOUNT = () => { try { return realpathSync(DIR); } catch { return DIR; } };
const SECURITY = process.env.JARVIS_AGENTS_SECURITY ?? '/usr/bin/security', SERVICE = 'Jarvis';

// ---------- the settings file ----------
// provider, bedrock, vertex: how Claude signs in (packaged app) · notify: which moments the Mac tells the owner about ·
// editor, terminal: where a file or a session opens outside the window · folders: added to the project list · setup:
// per repository, the script a new worktree runs first · land: per repository, its landing's gates, restart and way.
export let settings: Settings = {};
export async function loadSettings() {
  try { const s = JSON.parse(await readFile(FILE, 'utf8')); settings = s?.settings && typeof s.settings === 'object' ? s.settings : {}; } catch { settings = {}; }
  if (PACKAGED) await loadKey();
}
const text = (v: unknown, n = 300) => typeof v === 'string' && v.trim() && v.length <= n ? v.trim() : undefined;
// Only what the window may set, each checked; anything else is ignored. `null` clears a setting.
export async function patchSettings(p: Record<string, unknown>) {
  const s: Settings = { ...settings }, drop = (k: keyof Settings) => { delete s[k]; };
  if ('provider' in p) { if (p.provider === 'anthropic' || p.provider === 'bedrock' || p.provider === 'vertex') s.provider = p.provider; else drop('provider'); }
  if ('bedrock' in p) { const b = p.bedrock as Record<string, unknown> | null, region = text(b?.region, 40), profile = text(b?.profile, 100); if (region) s.bedrock = { region, ...profile ? { profile } : {} }; else drop('bedrock'); }
  if ('vertex' in p) { const v = p.vertex as Record<string, unknown> | null, region = text(v?.region, 40), project = text(v?.project, 100); if (region && project) s.vertex = { region, project }; else drop('vertex'); }
  if ('notify' in p) { const n = p.notify as Record<string, unknown> | null; if (n && typeof n === 'object') s.notify = { done: n.done === true, wait: n.wait !== false, err: n.err !== false }; else drop('notify'); }
  if ('editor' in p) { const e = text(p.editor, 60); if (e) s.editor = e; else drop('editor'); }
  if ('terminal' in p) { const t = text(p.terminal, 60); if (t) s.terminal = t; else drop('terminal'); }
  if ('folders' in p) { const f = Array.isArray(p.folders) ? [...new Set(p.folders.filter(x => typeof x === 'string' && path.isAbsolute(x)))].slice(0, 200) as string[] : []; if (f.length) s.folders = f; else drop('folders'); }
  if ('setup' in p) {
    const m = p.setup as Record<string, unknown> | null, out: Record<string, string> = {};
    for (const [repo, cmd] of Object.entries(m && typeof m === 'object' ? m : {})) { const c = text(cmd, 2000); if (path.isAbsolute(repo) && c) out[repo] = c; }
    if (Object.keys(out).length) s.setup = out; else drop('setup');
  }
  if ('land' in p) {
    const m = p.land as Record<string, unknown> | null, out: NonNullable<Settings['land']> = {};
    for (const [repo, v] of Object.entries(m && typeof m === 'object' ? m : {})) {
      const o = (v && typeof v === 'object' ? v : {}) as Record<string, unknown>;
      const gates = (Array.isArray(o.gates) ? o.gates : []).map(g => text(g, 2000)).filter((g): g is string => !!g).slice(0, 20);
      const restart = text(o.restart, 2000), via = o.via === 'merge' || o.via === 'pr' ? o.via : undefined;
      if (path.isAbsolute(repo) && (gates.length || restart || via)) out[repo] = { ...gates.length ? { gates } : {}, ...restart ? { restart } : {}, ...via ? { via } : {} };
    }
    if (Object.keys(out).length) s.land = out; else drop('land');
  }
  await mkdir(DIR, { recursive: true, mode: 0o700 });
  await writeFile(`${FILE}.tmp`, JSON.stringify({ v: 1, settings: s }, null, 1));
  await rename(`${FILE}.tmp`, FILE);
  settings = s;
}

// ---------- the key, in the Keychain ----------
let key: string | null = null, keyErr = '';
async function readItem(): Promise<Record<string, string>> {
  try {
    const { stdout } = await exec(SECURITY, ['find-generic-password', '-s', SERVICE, '-a', ACCOUNT(), '-w'], { timeout: 10000 });
    const o = JSON.parse(stdout);
    return o && typeof o === 'object' ? o : {};
  } catch (e) {
    // 44: there is no such item yet. Anything else (a locked Keychain) is said where the key would be.
    if ((e as { code?: unknown }).code === 44) return {};
    throw e;
  }
}
async function writeItem(o: Record<string, string>) {
  const data = Buffer.from(JSON.stringify(o)).toString('hex');
  await new Promise<void>((done, fail) => {
    const c = spawn(SECURITY, ['-i'], { stdio: ['pipe', 'ignore', 'pipe'] });
    let err = '';
    c.stderr.on('data', d => { err += d; });
    c.on('error', fail);
    c.on('close', code => code === 0 ? done() : fail(new Error(`钥匙串没存上：${err.trim().slice(-200) || code}`)));
    c.stdin.end(`add-generic-password -U -s ${SERVICE} -a "${ACCOUNT()}" -X ${data}\n`);
  });
}
async function loadKey() {
  try { key = (await readItem()).ANTHROPIC_API_KEY ?? null; keyErr = ''; } catch (e) { key = null; keyErr = String(e); }
}
// A key is checked against the API before it is kept: listing models costs nothing and says whether the key works.
const devBuild = () => Object.assign(new Error('开发版用这台 Mac 上 Claude Code 自己的登录，不存 key'), { status: 409 });
export async function saveKey(k: string) {
  if (!PACKAGED) throw devBuild();
  const v = k.trim();
  if (!/^\S{20,400}$/.test(v)) throw Object.assign(new Error('这不像一个 API key'), { status: 400 });
  const base = (process.env.ANTHROPIC_BASE_URL ?? 'https://api.anthropic.com').replace(/\/+$/, '');
  let verified = false;
  try {
    const r = await fetch(`${base}/v1/models?limit=1`, { headers: { 'x-api-key': v, 'anthropic-version': '2023-06-01' }, signal: AbortSignal.timeout(10000) });
    if (r.status === 401 || r.status === 403) throw Object.assign(new Error('Anthropic 说这个 key 不对'), { status: 400 });
    verified = r.ok;
  } catch (e) { if ((e as { status?: number }).status) throw e; }
  await writeItem({ ...await readItem(), ANTHROPIC_API_KEY: v });
  key = v; keyErr = '';
  return { verified };
}
export async function forgetKey() {
  if (!PACKAGED) throw devBuild();
  const rest = await readItem();
  delete rest.ANTHROPIC_API_KEY;
  if (Object.keys(rest).length) await writeItem(rest);
  else await exec(SECURITY, ['delete-generic-password', '-s', SERVICE, '-a', ACCOUNT()], { timeout: 10000 }).catch(() => {});
  key = null;
}

// ---------- what a Claude session of Startrail's own signs in with ----------
// Added to that session's environment only (claude.ts), never to this process's: a terminal the window opens copies
// this process's environment, and the owner's claude there keeps its own login.
export function keyEnv(): Record<string, string | undefined> {
  if (!PACKAGED) return {};
  const p = settings.provider ?? 'anthropic';
  if (p === 'bedrock') return { CLAUDE_CODE_USE_BEDROCK: '1', AWS_REGION: settings.bedrock?.region, ...settings.bedrock?.profile ? { AWS_PROFILE: settings.bedrock.profile } : {} };
  if (p === 'vertex') return { CLAUDE_CODE_USE_VERTEX: '1', CLOUD_ML_REGION: settings.vertex?.region, ANTHROPIC_VERTEX_PROJECT_ID: settings.vertex?.project };
  return { ANTHROPIC_API_KEY: key ?? undefined };
}
// How those sessions sign in now; `why` is set when they cannot start.
export function auth(): Auth {
  if (!PACKAGED) return { packaged: false, mode: 'subscription', ready: true };
  const provider = settings.provider ?? 'anthropic';
  const why = provider === 'bedrock' ? settings.bedrock?.region ? '' : '选了 Amazon Bedrock，还没填区域'
    : provider === 'vertex' ? settings.vertex?.region && settings.vertex.project ? '' : '选了 Google Vertex，还没填区域和项目'
    : key ? '' : keyErr ? `读不出钥匙串里的 key：${keyErr.slice(0, 120)}` : '先填一个 Anthropic API key';
  return { packaged: true, mode: 'key', provider, ready: !why, ...why ? { why } : {}, ...key ? { hint: `…${key.slice(-4)}` } : {} };
}

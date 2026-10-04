import { execFile } from 'node:child_process';
import { createReadStream } from 'node:fs';
import { readdir } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';
// The Usage page's Token section: what Claude Code, Codex and Hermes used in the last 30 days, priced at API
// rates by ccusage from their local logs. Everything the page needs is derived here; the renderer reads no log.
export type TokenAgent = 'claude' | 'codex' | 'hermes';
export type TokenDay = { date: string; agent: TokenAgent; cost: number; tokens: number; output: number };
export type TokenSession = { id: string; agent: TokenAgent; title: string; folder: string; date: string; cost: number; tokens: number; output: number; models: { model: string; cost: number; output: number }[] };
export type TokenUsage = { days: TokenDay[]; sessions: TokenSession[]; at: number };
type Model = { modelName: string; cost: number; outputTokens: number };
type Row = { agent: TokenAgent; totalCost: number; totalTokens: number; outputTokens: number; modelBreakdowns?: Model[] };
type Raw = { daily: { period: string; agents?: Row[] }[]; session: (Row & { period: string; metadata?: { lastActivity?: string } })[] };

const here = path.dirname(fileURLToPath(import.meta.url));
const CCUSAGE = path.join(here, '../node_modules/.bin/ccusage');
const KEEP = 10 * 60_000, WINDOW_DAYS = 30, HEAD_LINES = 300, TITLE_CHARS = 70;
const pad = (n: number) => String(n).padStart(2, '0');
const ymd = (d: Date, sep: string) => `${d.getFullYear()}${sep}${pad(d.getMonth() + 1)}${sep}${pad(d.getDate())}`;

// Text that is the harness talking, not the person.
const WRAPPER = /^\s*(<(local-command-caveat|command-name|command-message|local-command-stdout|system-reminder|task-notification|bash-input|bash-stdout|environment_context|codex_internal_context|recommended_plugins|guardian_tool_descriptions|app-context|permissions|user_instructions|INSTRUCTIONS)\b|# AGENTS\.md|Caveat:|\[Request interrupted|\[Image|The previous response failed|Base directory for this skill|>>> )/;
export function titleOf(raw: string): string {
  if (/^\s*The following is the Codex agent/.test(raw)) return 'Codex auto-review';
  let text = raw, command = '';
  const slash = text.match(/<command-name>([^<]*)<\/command-name>[\s\S]*?<command-args>([\s\S]*?)(?:<\/command-args>|$)/); // a slash command with arguments
  if (slash) { command = `${slash[1]} `; text = slash[2]; }
  const note = text.indexOf('<note>');
  if (note >= 0) text = text.slice(note + 6).replace(/<\/note>[\s\S]*/, '');
  const message = text.match(/<message\b[^>]*>([\s\S]*?)(?=<attachment|<\/message>)/); // a mention relayed from the project chat
  if (message) text = message[1];
  const request = text.match(/## My request[^\n]*:/); // Codex puts attached files before the request
  if (request) text = text.slice(request.index! + request[0].length);
  text = text.replace(/^(\s*@"[^"]*")+/, '').replace(/^\s*Reply in \w+[.,]?\s*/i, ''); // relayed notes open with a language directive
  const line = text.split('\n').map(s => s.replace(/\s+/g, ' ').trim()).find(Boolean) ?? '';
  const sentence = line.match(/^.*?([.!?](?=\s)|[。！？]|$)/)?.[0] ?? line;
  const title = command + sentence;
  return title.length > TITLE_CHARS ? `${title.slice(0, TITLE_CHARS - 1).trimEnd()}…` : title;
}
// The first real message of a session log: Claude Code's `user` lines or Codex's `response_item` user messages.
async function firstMessage(file: string): Promise<{ title: string; cwd: string }> {
  const stream = createReadStream(file, { encoding: 'utf8' }), lines = createInterface({ input: stream, crlfDelay: Infinity });
  let n = 0, cwd = '';
  try {
    for await (const line of lines) {
      if (++n > HEAD_LINES) break;
      let entry: any;
      try { entry = JSON.parse(line); } catch { continue; }
      if (entry.type === 'session_meta') cwd = entry.payload?.cwd ?? '';
      const message = entry.type === 'user' ? entry.message : entry.type === 'response_item' && entry.payload?.type === 'message' && entry.payload.role === 'user' ? entry.payload : null;
      if (!message) continue;
      const parts: unknown[] = Array.isArray(message.content) ? message.content : [message.content];
      for (const part of parts) {
        const text = typeof part === 'string' ? part : (part as any)?.type === 'text' || (part as any)?.type === 'input_text' ? (part as any).text : '';
        const title = typeof text === 'string' && (!WRAPPER.test(text) || text.includes('<command-args>')) ? titleOf(text) : '';
        if (title) return { title, cwd };
      }
    }
  } finally { stream.destroy(); }
  return { title: '', cwd };
}

const claudeHome = path.join(homedir(), '.claude/projects'), codexHome = path.join(homedir(), '.codex/sessions');
const HOME_DIR = new RegExp(`^${homedir().replace(/\//g, '-')}-?`); // Claude names a project folder after its path
const named = new Map<string, { title: string; folder: string }>(); // by session id; a log's first message never changes
// uuid -> project folder, one directory listing per run instead of a search per session.
async function claudeIndex() {
  const index = new Map<string, string>();
  for (const dir of await readdir(claudeHome).catch(() => [] as string[]))
    for (const name of await readdir(path.join(claudeHome, dir)).catch(() => [] as string[])) if (name.endsWith('.jsonl')) index.set(name.slice(0, -6), dir);
  return index;
}
async function describe(s: Raw['session'][number], claude: Map<string, string>) {
  const known = named.get(s.period);
  if (known) return known;
  const bare = s.period.split('/').pop()!;
  let out = { title: bare, folder: '' };
  try {
    if (s.agent === 'claude') {
      const dir = claude.get(s.period);
      if (dir) {
        const { title } = await firstMessage(path.join(claudeHome, dir, `${s.period}.jsonl`));
        out = { title: title || bare, folder: dir.replace(HOME_DIR, '') || '~' };
      }
    } else if (s.agent === 'codex') {
      const { title, cwd } = await firstMessage(path.join(codexHome, codexFile(s.period)));
      out = { title: title || bare, folder: path.basename(cwd) };
    } else out = { title: '', folder: '' }; // Hermes jobs are all one kind; the page names them in its language
  } catch { /* the log is gone or unreadable; the id stands in */ }
  named.set(s.period, out);
  return out;
}
// ccusage names most Codex sessions by their path under ~/.codex/sessions, a few by the bare rollout name.
const codexFile = (period: string) => `${period.includes('/') ? period : period.replace(/^rollout-(\d{4})-(\d{2})-(\d{2})T/, '$1/$2/$3/rollout-$1-$2-$3T')}.jsonl`;
const dateOf = (s: Raw['session'][number]) => {
  if (s.metadata?.lastActivity) return ymd(new Date(s.metadata.lastActivity), '-');
  const m = s.period.match(/^(?:rollout-)?(\d{4})[/-](\d{2})[/-](\d{2})/) ?? s.period.match(/^(\d{4})(\d{2})(\d{2})_/);
  return m ? `${m[1]}-${m[2]}-${m[3]}` : '';
};

const runCcusage = () => new Promise<Raw>((resolve, reject) => {
  const since = ymd(new Date(Date.now() - WINDOW_DAYS * 86_400_000), '');
  // Not --offline: the bundled prices miss the newest models. nice keeps the 3 s scan from competing with the UI.
  execFile('nice', ['-n', '19', CCUSAGE, 'daily', '--sections', 'daily,session', '--by-agent', '--since', since, '--json'], { maxBuffer: 256 << 20, timeout: 90_000 }, (error, stdout, stderr) => {
    if (error) return reject(new Error(stderr.trim().split('\n').pop() || error.message));
    try { resolve(JSON.parse(stdout) as Raw); } catch { reject(new Error('ccusage printed something unreadable')); }
  });
});
async function build(): Promise<TokenUsage> {
  const raw = await runCcusage(), claude = await claudeIndex();
  const days = raw.daily.flatMap(d => (d.agents ?? []).map(a => ({ date: d.period, agent: a.agent, cost: a.totalCost, tokens: a.totalTokens, output: a.outputTokens })));
  const rows = raw.session.filter(s => s.totalCost >= .005 && dateOf(s));
  const sessions: TokenSession[] = [];
  for (let i = 0; i < rows.length; i += 16)
    sessions.push(...await Promise.all(rows.slice(i, i + 16).map(async s => ({ id: s.period, agent: s.agent, ...await describe(s, claude), date: dateOf(s),
      cost: s.totalCost, tokens: s.totalTokens, output: s.outputTokens, models: (s.modelBreakdowns ?? []).map(m => ({ model: m.modelName, cost: m.cost, output: m.outputTokens })) }))));
  return { days, sessions, at: Date.now() };
}

let cached: TokenUsage | null = null, running: Promise<TokenUsage> | null = null;
export function tokenUsage(refresh = false): Promise<TokenUsage> {
  if (!refresh && cached && Date.now() - cached.at < KEEP) return Promise.resolve(cached);
  running ??= build().then(r => (cached = r)).finally(() => { running = null; });
  return running;
}

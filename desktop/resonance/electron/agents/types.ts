// What the agent host (ADR 0073) and the Agents window say to each other. Both agents are folded into the same few
// shapes: the window never knows which wire a session came from.
export type Agent = 'claude' | 'codex';
// work: a turn is running · pack: compacting · wait: it asked you something · done: idle · err: the last turn failed
export type St = 'work' | 'pack' | 'wait' | 'done' | 'err';
export type Diff = [' ' | '+' | '-', string][];
// `say` is something it wrote on the way, before its next step; the last thing it writes in a turn is the answer.
// `think` is a summary of its thinking (Claude's summarized thinking, Codex's reasoning summary): `t` its first line,
// `out` the whole. `sub`: a sub-agent's own steps, under the `agent` step that started it. `pics`: pictures a step gave
// back (a screenshot a tool took, an image Codex viewed or made).
export type Step = { at?: number; k: 'read' | 'edit' | 'bash' | 'search' | 'agent' | 'web' | 'tool' | 'say' | 'think'; t: string; add?: number; del?: number; diff?: Diff; out?: string; ok?: boolean;
  sub?: Step[]; pics?: Pic[] };
export type Question = { q: string; head?: string; multi?: boolean; opts: [string, string][] };
export type Req =
  | { id: string; tool: 'Bash'; why: string; cmd: string; cwd: string; always: string }
  | { id: string; tool: 'Edit'; why: string; file: string; diff: Diff; always: string }
  | { id: string; tool: 'Tool'; why: string; name: string; detail: string; always: string }
  | { id: string; tool: 'Ask'; qs: Question[] }
  | { id: string; tool: 'Plan'; plan: string };
// Epoch milliseconds when known. Missing transcript times stay missing.
// A picture sent with a message: `img` names the host's copy of it (GET /images/{img}); without one only its name is known.
export type Pic = { name: string; img?: string };
// `id` on what you said and on an answer names that point of the conversation for fork and rewind (POST
// /sessions/{id}/fork): Claude's message uuid, Codex's turn id.
export type Item = ({ at?: number; ended?: number } & (
  | { k: 'you'; text: string; files?: Pic[]; queued?: boolean; id?: string }
  | { k: 'it'; text: string; id?: string }
  | { k: 'steps'; steps: Step[]; took?: string; live?: boolean }
  | { k: 'plan'; todos: [string, 0 | 1 | 2][] }
  | { k: 'req'; req: Req; done?: string }
  | { k: 'note'; text: string }));
// One row of the list. `updated` is ms since epoch; `ctx` is the share of the context window used, 0–100.
export type Sess = {
  id: string; agent: Agent; title: string; cwd: string; project: string; branch: string; tree: boolean;
  // unread, parked and archived are the daemon's marks (ADR 0069), shared with the notch
  st: St; pinned: boolean; parked: boolean; archived: boolean; unread: boolean; updated: number; summary: string;
  model: string; effort: string; mode: string; ctx: number;
  // now: what it is doing this moment · bg: its background tasks · term: handed to a terminal · since: this turn's start
  // queue: what you sent while it worked, not yet taken
  created?: number; trace?: { at: number; st: St }[];
  // The conversations each reset (/clear, a plan run with a clean context) started, oldest first: `id` is the first and
  // the key, the last is the one it goes on in.
  resets?: string[];
  now?: string; bg?: string; term?: boolean; stopped?: boolean; since?: number; queue?: string[];
  // tasks: its background work (shells, sub-agents, monitors) while it runs, with the ones that ended this turn ·
  // dirs: folders it may work in besides its own · named: the title is yours, so it is never replaced by a generated one
  // · base: what its worktree started from
  tasks?: Task[]; dirs?: string[]; named?: boolean; base?: string;
  // ADR 0085 · dirty: what landing would take (files, lines, commits main does not have yet); absent outside git or with
  // nothing to land · land: the landing under way, absent when none is · gone: its worktree was cleaned away after landing
  dirty?: { n: number; add: number; del: number; ahead: number }; land?: Land; gone?: boolean;
};
// ADR 0085: landing, the host's line from a session's changes to main. `s` is where the line stands and `i` the step it
// is on; `steps` follows the line's order (changes, gates, commit, into main, restart, push, clean), each with its
// state, how long it took, the line under its name and the commands it runs. `why` says what stopped it or what it waits
// for; `acts` are the ways on, the first one lit.
export type LandSt = 'todo' | 'run' | 'ok' | 'skip' | 'wait' | 'paused' | 'fail';
export type Land = {
  s: 'run' | 'stopping' | 'wait' | 'paused' | 'fail' | 'fixing' | 'done'; i: number;
  steps: { st: LandSt; ms?: number; d?: string; cmd?: string[] }[];
  files: [string, number, number][]; gates: { n: string; st: 'todo' | 'run' | 'ok' | 'er'; say?: string }[];
  msg: string; drafting?: boolean; why?: string; acts?: ('fix' | 'stay' | 'resume' | 'allow' | 'deny')[];
  branch: string; into: string; restart?: string[];
};
// A background task: `kind` as the agent names it (local_bash, local_agent, monitor, …; Codex: terminal), `what` its
// description or command, `out` where its output is kept (GET /sessions/{id}/tasks/{task}).
export type Task = { id: string; kind: string; what: string; st: 'run' | 'done' | 'fail' | 'stop'; since?: number; ended?: number; out?: string };
// A file or page a session pointed at, as the preview shows it: markdown and text come as text (with what changed, when
// something did), pages, PDFs, images, audio and video as a file:// address for the preview's own browser, a folder as
// its entries; anything else is for Quick Look. `line`: the line the reference named · `cut`: text past 2 MB, only the
// last part is here.
export type Peek = { kind: 'md' | 'text' | 'web' | 'media' | 'dir' | 'quicklook'; abs: string; url?: string; text?: string; diff?: Diff; add?: number; del?: number;
  line?: number; cut?: boolean; entries?: { name: string; dir: boolean }[] };
// What a session changed against what landing would compare (GET /sessions/{id}/changes): per file its lines added and
// removed and how it changed (M changed, A new, D deleted, R renamed, ? not tracked yet).
export type Change = { path: string; add: number; del: number; st: 'M' | 'A' | 'D' | 'R' | '?'; from?: string };
// A session started outside the window (a terminal, Codex's app) that it can take in (GET /import).
export type Outside = { agent: Agent; id: string; title: string; cwd: string; updated: number; branch?: string; recent?: boolean };
// A project in the list (GET /projects): recent session folders first, then folders you added, then ~/Projects.
export type Project = { path: string; name: string; git: boolean; used?: number; added?: boolean };
// What the host runs with (GET /doctor), for the window's own check-up: the PATH it searches, which Claude Code it runs
// (`own`: this Mac's install, not the SDK's copy), how Startrail's Claude sessions sign in and as whom, Codex and git if
// found, and the daemon.
export type Doctor = {
  packaged: boolean; path: string[];
  claude: { exe: string; version?: string; own: boolean; auth: Auth; account?: Record<string, string> };
  codex: { found: boolean; path?: string; version?: string; account?: Record<string, string> | null; error?: string };
  git: { found: boolean; version?: string }; daemon: { up: boolean };
};
// The workbench's other tabs: Jarvis's two services as launchd sees them, and plan usage as the daemon last read it.
// `loaded`: launchd knows it (the installed app's daemon is the app's own child, not a LaunchAgent).
export type Service = { name: 'daemon' | 'companion'; label: string; loaded: boolean; running: boolean; pid?: number; since?: number };
export type UsageWindow = { key: string; label: string; percent: number; resets_at: string | null };
export type Usage = Partial<Record<Agent, { plan: string; windows: UsageWindow[] }>>;
export type Choice = { models: [string, string][]; efforts: string[]; modes: [string, string][]; always: string };
export type Catalog = Record<Agent, Choice>;
// A file sent with a message: its name and a data: URL, or where it is on this Mac (a file dropped on the window).
// Pictures go to the agent as pictures, a PDF to Claude as a document; any other file is named in the message.
export type File = { name: string; url?: string; path?: string };
export type Answer = { req: string; decision: 'allow' | 'always' | 'deny'; answers?: string[][]; text?: string };
// What fills a session's context window, for the popover on its ring: rows in the order the bar draws them, each with
// what it holds (a string is a heading); `say` is one line on what it means, its first part in bold.
export type CtxRow = { n: string; t: number; kind?: 'buf' | 'free'; sub?: (string | [string, number])[] };
export type Ctx = { used: number; max: number; model: string; rows: CtxRow[]; say: [string, string]; foot: string[] };
// What the owner sets for the host (GET /settings, POST /settings). provider, bedrock and vertex: how Startrail's Claude
// sessions sign in in the packaged app (ADR 0094) · notify: which moments the Mac tells you about while the window is
// not in front · editor, terminal: where a file or a session opens outside the window (ids from the window's lists) ·
// folders: folders you added to the project list · setup: per repository, the script a new worktree runs before its
// first turn.
export type Settings = {
  provider?: 'anthropic' | 'bedrock' | 'vertex'; bedrock?: { region: string; profile?: string }; vertex?: { region: string; project: string };
  notify?: { done: boolean; wait: boolean; err: boolean }; editor?: string; terminal?: string;
  folders?: string[]; setup?: Record<string, string>;
};
// How Startrail's Claude sessions sign in: the dev build with Allen's subscription; the packaged app with the owner's
// own key or cloud account, ready once it has what it needs (`why` says what is missing) · hint: the key's last four
// characters.
export type Auth = { packaged: boolean; mode: 'subscription' | 'key'; provider?: 'anthropic' | 'bedrock' | 'vertex'; ready: boolean; why?: string; hint?: string };
export type Event =
  | { t: 'hello'; sessions: Sess[]; catalog: Catalog; settings?: Settings; auth?: Auth }
  | { t: 'settings'; settings: Settings; auth: Auth }
  // A sign-in the check-up started (Codex's ChatGPT login) finished.
  | { t: 'signin'; agent: Agent; ok: boolean; why?: string }
  | { t: 'sess'; s: Sess }
  | { t: 'gone'; id: string }
  | { t: 'catalog'; catalog: Catalog }
  // Items from `from` on replace what the window has there; a window missing items before `from` reloads them.
  | { t: 'items'; id: string; from: number; items: Item[] }
  | { t: 'live'; id: string; text: string | null };

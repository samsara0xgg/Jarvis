// What the agent host (ADR 0073) and the Agents window say to each other. Both agents are folded into the same few
// shapes: the window never knows which wire a session came from.
export type Agent = 'claude' | 'codex';
// work: a turn is running · pack: compacting · wait: it asked you something · done: idle · err: the last turn failed
export type St = 'work' | 'pack' | 'wait' | 'done' | 'err';
export type Diff = [' ' | '+' | '-', string][];
// `say` is something it wrote on the way, before its next step; the last thing it writes in a turn is the answer.
// `think` is a summary of its thinking (Claude's summarized thinking, Codex's reasoning summary): `t` its first line,
// `out` the whole, `ms` how long it thought (Claude's is a step from the moment it starts thinking, with no `t` yet; read
// back from a transcript, `ms` is the time since the entry before it). `sub`: a sub-agent's own steps, under the `agent`
// step that started it. `pics`: pictures a step gave back (a screenshot a tool took, an image Codex viewed or made).
// `task`: the task the call started (a sub-agent, a shell in the background), which stops on its own through
// POST /sessions/{id}/tasks/{task}/stop while it runs.
export type Step = { at?: number; k: 'read' | 'edit' | 'bash' | 'search' | 'agent' | 'web' | 'tool' | 'say' | 'think'; t: string; add?: number; del?: number; diff?: Diff; out?: string; ok?: boolean;
  sub?: Step[]; pics?: Pic[]; ms?: number; task?: string };
export type Question = { q: string; head?: string; multi?: boolean; opts: [string, string][] };
// One field of an MCP server's form (C3): the answer it takes (text, a number, a whole number, yes or no, one or several
// of `opts`, each [value, label]), whether it must be filled, what it holds to start with; `min` and `max` bound a
// number's value, a text's length, or how many are picked.
export type Field = { key: string; title: string; about?: string; kind: 'text' | 'number' | 'int' | 'bool' | 'one' | 'many'; opts?: [string, string][];
  need?: boolean; def?: string | number | boolean | string[]; format?: string; min?: number; max?: number };
export type Req =
  | { id: string; tool: 'Bash'; why: string; cmd: string; cwd: string; always: string }
  | { id: string; tool: 'Edit'; why: string; file: string; diff: Diff; always: string }
  | { id: string; tool: 'Tool'; why: string; name: string; detail: string; always: string }
  | { id: string; tool: 'Ask'; qs: Question[] }
  | { id: string; tool: 'Plan'; plan: string }
  // An MCP server asks for its form to be filled, or (`url`) for a page to be opened, a sign-in most often: the window
  // opens it when the owner says yes.
  | { id: string; tool: 'Form'; server: string; why: string; fields: Field[]; url?: string };
// Epoch milliseconds when known. Missing transcript times stay missing.
// A picture sent with a message: `img` names the host's copy of it (GET /images/{img}); without one only its name is known.
export type Pic = { name: string; img?: string };
// `id` on what you said and on an answer names that point of the conversation for fork and rewind (POST
// /sessions/{id}/fork): Claude's message uuid, Codex's turn id. `ride`: your reactions that went to the agent with what
// you said (m-rx).
export type Item = ({ at?: number; ended?: number } & (
  | { k: 'you'; text: string; files?: Pic[]; queued?: boolean; id?: string; ride?: string[] }
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
  // ADR 0097 · dirty: what landing would take (files, lines, commits the default branch does not have yet), the default
  // branch it lands into and the ways it can, the default first; absent outside git, with nothing to land or no way to.
  // `ask`: its repository has no way of its own yet and could go either, so the first landing asks which (and keeps the
  // answer) · `gates`: the gates this change would run · `restart`: what merging it would restart · `local`: its
  // repository has no origin, so nothing is pushed ·
  // land: the landing under way, absent when none is · gone: its worktree was cleaned away after landing · pr: the pull
  // request a landing opened for its branch
  dirty?: { n: number; add: number; del: number; ahead: number; into: string; ways: LandVia[]; ask?: boolean; gates?: string[]; restart?: string[]; local?: boolean }; land?: Land; gone?: boolean; pr?: string;
  // rx: reactions on its messages, by the message (`you:<id>`, `it:<id>`) · vers: this session as one version of a
  // conversation you edited (m-edit): `root` is shared by every version of it, `at` names the messages here that have
  // versions, each with its family and this version's number
  rx?: Record<string, Rx>; vers?: { root: string; at: Record<string, [string, number]> };
};
// Reactions on one message (m-rx): yours in the order you added them, those already carried to the agent with a message
// you sent after them (`sent`), and the agent's own (`by`: 👀 when it took a message you sent while it worked).
export type Rx = { mine?: string[]; sent?: string[]; by?: string };
// ADR 0097: how a landing ends. merge: fast-forward into the default branch, restart, push it, clean the worktree away ·
// pr: push the session's branch and open a pull request against the default branch, keeping the worktree.
export type LandVia = 'merge' | 'pr';
// ADR 0097: landing, the host's line from a session's changes to its repository's default branch. `s` is where the
// line stands and `i` the step it is on; `steps` follows the line's order (changes, gates, commit, into the default
// branch, restart, push, clean), each with its state, how long it took, the line under its name and the commands it
// runs. `why` says what stopped it or what it waits for; `acts` are the ways on, the first one lit. `into`: the default
// branch · `restart`: what it restarts (the owner's command, cut short) · `pr`: the pull request's address, or where to
// open one when gh is not installed.
export type LandSt = 'todo' | 'run' | 'ok' | 'skip' | 'wait' | 'paused' | 'fail';
export type Land = {
  s: 'run' | 'stopping' | 'wait' | 'paused' | 'fail' | 'fixing' | 'done'; i: number;
  steps: { st: LandSt; ms?: number; d?: string; cmd?: string[] }[];
  files: [string, number, number][]; gates: { n: string; st: 'todo' | 'run' | 'ok' | 'er'; say?: string }[];
  msg: string; drafting?: boolean; why?: string; acts?: ('fix' | 'stay' | 'resume' | 'allow' | 'deny')[];
  branch: string; into: string; via: LandVia; restart?: string[]; pr?: string;
};
// A background task: `kind` as the agent names it (local_bash, local_agent, monitor, …; Codex: terminal), `what` its
// description or command, `out` where its output is kept (GET /sessions/{id}/tasks/{task}), known while it runs for a
// shell Claude sent to the background. `fg`: started in the foreground, the call that started it waiting on it (a
// sub-agent working for the turn), so not background work.
export type Task = { id: string; kind: string; what: string; st: 'run' | 'done' | 'fail' | 'stop'; since?: number; ended?: number; out?: string; fg?: boolean };
// A file or page a session pointed at, as the preview shows it: markdown and text come as text (with what changed, when
// something did), pages, PDFs, images, audio and video as a file:// address for the preview's own browser, a folder as
// its entries; anything else is for Quick Look. `line`: the line the reference named · `cut`: text past 2 MB, only the
// last part is here · `size`: in bytes · `bytes`: GET /sessions/{id}/file gives its bytes (a picture, sound, video or
// PDF inside the session's folders), so the preview shows it itself · `pages`: a PDF's, when it says · `hunks`: where
// each hunk of `diff` starts in the file.
export type Peek = { kind: 'md' | 'text' | 'web' | 'media' | 'dir' | 'quicklook'; abs: string; url?: string; text?: string; diff?: Diff; add?: number; del?: number;
  line?: number; cut?: boolean; entries?: { name: string; dir: boolean }[]; size?: number; bytes?: boolean; pages?: number; hunks?: number[] };
// What a session changed against what landing would compare (GET /sessions/{id}/changes): per file its lines added and
// removed and how it changed (M changed, A new, D deleted, R renamed, ? not tracked yet); `bin`: git counts no lines.
export type Change = { path: string; add: number; del: number; st: 'M' | 'A' | 'D' | 'R' | '?'; from?: string; bin?: boolean };
// A session started outside the window (a terminal, Codex's app) that it can take in (GET /import).
export type Outside = { agent: Agent; id: string; title: string; cwd: string; updated: number; branch?: string; recent?: boolean };
// One of them running now, as the daemon's board of Claude Code sessions sees it (GET /import/live, ADR 0049): what it
// is doing, and the request it stopped on when the daemon holds it, answered from here with POST /import/answer.
export type Live = { id: string; st: 'work' | 'wait' | 'done'; req?: Req };
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
// A form (C3) is answered with `values` by its fields' keys (allow: 提供), `deny` (不提供，继续) or `cancel` (取消); any
// other request takes cancel as deny.
export type Answer = { req: string; decision: 'allow' | 'always' | 'deny' | 'cancel'; answers?: string[][]; text?: string; values?: Record<string, unknown> };
// An MCP server as a session sees it now (C3): connected, starting, waiting for a sign-in, failed (`why`) or switched
// off; how many tools it gives, where it is configured, and what the window can do with it here (switch it on or off,
// connect again, sign in: POST /sessions/{id}/mcp).
export type Mcp = { name: string; st: 'on' | 'wait' | 'auth' | 'fail' | 'off'; tools?: number; why?: string; scope?: string; can: McpAct[] };
export type McpAct = 'on' | 'off' | 'reconnect' | 'login';
// What fills a session's context window, for the popover on its ring: rows in the order the bar draws them, each with
// what it holds (a string is a heading); `say` is one line on what it means, its first part in bold.
export type CtxRow = { n: string; t: number; kind?: 'buf' | 'free'; sub?: (string | [string, number])[] };
export type Ctx = { used: number; max: number; model: string; rows: CtxRow[]; say: [string, string]; foot: string[] };
// What the owner sets for the host (GET /settings, POST /settings). provider, bedrock and vertex: how Startrail's Claude
// sessions sign in in the packaged app (ADR 0094) · notify: which moments the Mac tells you about while the window is
// not in front, and whether Jarvis's notch says them instead (`notch`, on unless false) · editor, terminal: where a
// file or a session opens outside the window (ids from the window's lists) ·
// folders: folders you added to the project list · setup: per repository, the script a new worktree runs before its
// first turn · land: per repository, how its landing goes (ADR 0097): the gates, each a shell command run in the
// session's folder, the restart command run in the main checkout once the default branch has the change, and the way
// it lands unless the owner picks another.
export type Settings = {
  provider?: 'anthropic' | 'bedrock' | 'vertex'; bedrock?: { region: string; profile?: string }; vertex?: { region: string; project: string };
  notify?: { done: boolean; wait: boolean; err: boolean; notch?: boolean }; editor?: string; terminal?: string;
  folders?: string[]; setup?: Record<string, string>; land?: Record<string, { gates?: string[]; restart?: string; via?: LandVia }>;
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
  // A sign-in to an MCP server (C3) finished; `id`: the session it was started from, when the agent says.
  | { t: 'mcp'; agent: Agent; name: string; ok: boolean; why?: string; id?: string }
  | { t: 'sess'; s: Sess }
  | { t: 'gone'; id: string }
  | { t: 'catalog'; catalog: Catalog }
  // Items from `from` on replace what the window has there; a window missing items before `from` reloads them.
  | { t: 'items'; id: string; from: number; items: Item[] }
  | { t: 'live'; id: string; text: string | null };

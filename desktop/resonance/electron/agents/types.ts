// What the agent host (ADR 0067) and the Agents window say to each other. Both agents are folded into the same few
// shapes: the window never knows which wire a session came from.
export type Agent = 'claude' | 'codex';
// work: a turn is running · pack: compacting · wait: it asked you something · done: idle · err: the last turn failed
export type St = 'work' | 'pack' | 'wait' | 'done' | 'err';
export type Diff = [' ' | '+' | '-', string][];
// `say` is something it wrote on the way, before its next step; the last thing it writes in a turn is the answer.
export type Step = { k: 'read' | 'edit' | 'bash' | 'search' | 'agent' | 'web' | 'tool' | 'say'; t: string; add?: number; del?: number; diff?: Diff; out?: string; ok?: boolean };
export type Question = { q: string; head?: string; multi?: boolean; opts: [string, string][] };
export type Req =
  | { id: string; tool: 'Bash'; why: string; cmd: string; cwd: string; always: string }
  | { id: string; tool: 'Edit'; why: string; file: string; diff: Diff; always: string }
  | { id: string; tool: 'Tool'; why: string; name: string; detail: string; always: string }
  | { id: string; tool: 'Ask'; qs: Question[] }
  | { id: string; tool: 'Plan'; plan: string };
export type Item =
  | { k: 'you'; text: string; files?: string[]; queued?: boolean }
  | { k: 'it'; text: string }
  | { k: 'steps'; steps: Step[]; took?: string; live?: boolean }
  | { k: 'plan'; todos: [string, 0 | 1 | 2][] }
  | { k: 'req'; req: Req; done?: string }
  | { k: 'note'; text: string };
// One row of the list. `updated` is ms since epoch; `ctx` is the share of the context window used, 0–100.
export type Sess = {
  id: string; agent: Agent; title: string; cwd: string; project: string; branch: string; tree: boolean;
  // unread, parked and archived are the daemon's marks (ADR 0069), shared with the notch
  st: St; pinned: boolean; parked: boolean; archived: boolean; unread: boolean; updated: number; summary: string;
  model: string; effort: string; mode: string; ctx: number;
  // now: what it is doing this moment · bg: its background tasks · term: handed to a terminal · since: this turn's start
  // queue: what you sent while it worked, not yet taken
  now?: string; bg?: string; term?: boolean; stopped?: boolean; since?: number; queue?: string[];
};
export type Choice = { models: [string, string][]; efforts: string[]; modes: [string, string][]; always: string };
export type Catalog = Record<Agent, Choice>;
// A file sent with a message: its name and a data: URL.
export type File = { name: string; url: string };
export type Answer = { req: string; decision: 'allow' | 'always' | 'deny'; answers?: string[][]; text?: string };
export type Event =
  | { t: 'hello'; sessions: Sess[]; catalog: Catalog }
  | { t: 'sess'; s: Sess }
  | { t: 'gone'; id: string }
  | { t: 'catalog'; catalog: Catalog }
  // Items from `from` on replace what the window has there; a window missing items before `from` reloads them.
  | { t: 'items'; id: string; from: number; items: Item[] }
  | { t: 'live'; id: string; text: string | null };

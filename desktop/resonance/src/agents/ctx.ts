// What a feature of the Agents page works with. page.ts builds one context and mounts every feature on it (messages,
// the queue by her, what it is doing, going back, opening what it names, the composer, settings…); each feature keeps
// its own state and answers the clicks, keys, commands and title-menu lines that are its own. Nothing here draws.
import type { Catalog, Item, Sess, Step } from '../../electron/agents/types';
import type { mountWorkbench } from './workbench';

export type Part = 'side' | 'head' | 'main' | 'live' | 'comp';
export type Feature = {
  // A click on a [data-act] in the window: true when the act was this feature's.
  act?(a: string, el: HTMLElement): boolean | void;
  // Esc, before the page's own: true when it closed something of this feature's.
  esc?(): boolean;
  // A key anywhere, in the capture phase before the page's own keys: true when it took the key (and consumed it).
  key?(e: KeyboardEvent): boolean;
  // Its lines in the title's ··· menu: buttons with data-act, or ''.
  more?(s: Sess): string;
  // An answer's HTML (its markdown, drawn) on its way into the conversation: what it adds or changes. A block it inserts
  // carries data-x, so it is never taken for part of what the agent wrote.
  answer?(s: Sess, it: Item & { k: 'it' }, i: number, html: string): string;
  // A line under one step of a turn (item i, step j), inside the step list, or ''.
  under?(s: Sess, st: Step, i: number, j: number): string;
  // Rows right above the composer while a session is open, or ''.
  rows?(s: Sess): string;
};
// A window command the host marks with a place (claude.ts OWN_UI): typed or picked, it runs here and never reaches the
// agent. `arg` is what follows the command.
export type Own = (s: Sess | undefined, arg: string) => void;

export type PageCtx = {
  win: HTMLElement; ta: HTMLTextAreaElement; api: string;
  // The page's state, read fresh each time.
  sessions(): Sess[]; current(): Sess | undefined; byId(id: string): Sess | undefined; items(id: string): Item[] | undefined;
  chat(): boolean;
  // What each agent offers (models, efforts, modes) as the host last said, or null before it has.
  catalog(): Catalog | null;
  // The host: `call` throws its error; `tryCall` says it in a toast and gives null.
  call<T = Record<string, unknown>>(route: string, body?: unknown, method?: string): Promise<T>;
  tryCall(route: string, body?: unknown, method?: string): Promise<Record<string, unknown> | null>;
  load(id: string): Promise<void>;
  // Drawing is batched into one frame; `open` goes to a session.
  draw(...parts: Part[]): void; open(id: string): void;
  toast(text: string): void; cue(name: string, gain?: number): void; tick(): void;
  md(text: string): string; diff(d: [string, string][]): string;
  // The page's one popover with any lines, under an element (right-aligned when `right`) or at a point in the window.
  menu(html: string, at: HTMLElement | { x: number; y: number }, o?: { right?: boolean; cls?: string }): void;
  closeMenu(): void;
  wb: ReturnType<typeof mountWorkbench>;
  own: Map<string, Own>;
};
export const features: Feature[] = [];

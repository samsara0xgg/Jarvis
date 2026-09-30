// Who waits on you (ADR 0102): one rule for her queue, the list, the next key and the notch. A session waits when it asks (a
// request to allow, a question, a plan), when it stopped on an error you have not read, or when it finished and you have
// not read it. Parked, archived and terminal-held sessions never wait. Asks come first, then errors, then finished
// ones; within each, the one that has waited longest.
import type { Sess } from '../../electron/agents/types';

export type Wait = 'ask' | 'err' | 'done';
const RANK: Record<Wait, number> = { ask: 0, err: 1, done: 2 };

export function waitOf(s: Sess): Wait | null {
  if (s.archived || s.parked || s.term) return null;
  if (s.st === 'wait') return 'ask';
  if (s.unread && s.st === 'err') return 'err';
  if (s.unread && s.st === 'done') return 'done';
  return null;
}
// When it began to wait: its last change of state.
export const waitingSince = (s: Sess) => s.trace?.at(-1)?.at ?? s.updated;
export function queue(ss: Sess[]): Sess[] {
  return ss.filter(s => waitOf(s)).sort((a, b) => RANK[waitOf(a)!] - RANK[waitOf(b)!] || waitingSince(a) - waitingSince(b));
}

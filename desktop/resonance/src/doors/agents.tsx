import type { DoorCtx, DoorPage } from '../doors';

// Agents: a stub until its page is built, a valid empty page that says so.
export function useAgentsDoor({ t }: DoorCtx): DoorPage | null {
  return { count: null, line: t(['Agents are not here yet.', 'Agents 还没接进来。']), chips: [], marks: [], rows: [], nowAt: null, empty: t(['Nothing here yet', '这里还没有东西']) };
}

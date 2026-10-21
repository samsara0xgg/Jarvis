import type { DoorCtx, DoorPage } from '../doors';

// 更多: a stub until its page is built, a valid empty page that says so.
export function useMoreDoor({ t }: DoorCtx): DoorPage | null {
  return { count: null, line: t(['There is nothing more here yet.', '这里还没有更多。']), chips: [], marks: [], rows: [], nowAt: null, empty: t(['Nothing here yet', '这里还没有东西']) };
}

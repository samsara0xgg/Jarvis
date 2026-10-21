import type { DoorCtx, DoorPage } from '../doors';

// 信: a stub until its page is built, a valid empty page that says so.
export function useMailDoor({ t }: DoorCtx): DoorPage | null {
  return { count: null, line: t(['Mail is not here yet.', '信还没接进来。']), chips: [], marks: [], rows: [], nowAt: null, empty: t(['Nothing here yet', '这里还没有东西']) };
}

import type { DoorCtx, DoorPage } from '../doors';

// 待办: a stub until its page is built, a valid empty page that says so.
export function useTodoDoor({ t }: DoorCtx): DoorPage | null {
  return { count: null, line: t(['To-dos are not here yet.', '待办还没接进来。']), chips: [], marks: [], rows: [], nowAt: null, empty: t(['Nothing here yet', '这里还没有东西']) };
}

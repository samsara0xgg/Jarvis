import type { DoorCtx, DoorPage } from '../doors';

// 额度: a stub until its page is built, a valid empty page that says so.
export function useQuotaDoor({ t }: DoorCtx): DoorPage | null {
  return { count: null, line: t(['Usage is not here yet.', '额度还没接进来。']), chips: [], marks: [], rows: [], nowAt: null, empty: t(['Nothing here yet', '这里还没有东西']) };
}

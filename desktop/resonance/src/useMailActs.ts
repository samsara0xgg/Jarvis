import { useState } from 'react';
import type { L } from './companionSettings';
import { postRoute, type Mail } from './homeData';
import type { MailAct } from './MailPage';

// What is done to letters, the same on the Dashboard's Mail page and the day pane's 信 page: a letter leaves the list at once, whichever way it goes (`gone`; the
// daemon's list catches up on its next poll); the daemon is told; one that did not go through comes back and says so; one that did says what was done and gives
// the way back. `tell` is how each surface says it: a toast there, a row that stays a moment here (`ids`: the letters it is about).
const DONE: Record<MailAct, [L, string]> = { trash: [['Moved to Trash', '已移到废纸篓'], 'untrash'], archive: [['Archived · still in All Mail', '已归档 · 仍在“所有邮件”里'], 'unarchive'], read: [['Marked as read', '已标为已读'], 'unread'] };
export type Tell = (text: string, undo?: () => void, ids?: string[]) => void;

export function useMailActs(port: string | null, t: (l: L) => string, reload: () => void, tell: Tell) {
  const [gone, setGone] = useState<string[]>([]);
  const move = async (ids: string[], route: string, undo: string, words: { done: string; failed: string; back: string }) => {
    setGone(g => [...g, ...ids]);
    const back = () => setGone(g => g.filter(id => !ids.includes(id)));
    try { if (port) await postRoute(port, `/inherent/mail/${route}`, { ids }); }
    catch { back(); tell(words.failed, undefined, ids); return; }
    tell(words.done, async () => {
      try { if (port) await postRoute(port, `/inherent/mail/${undo}`, { ids }); back(); reload(); }
      catch { tell(words.back, undefined, ids); }
    }, ids);
  };
  return {
    gone,
    // The letters the last answer called junk, out of the inbox (the route takes no others).
    archiveJunk: (ids: string[]) => ids.length ? move(ids, 'archive', 'unarchive', { done: t([`Archived ${ids.length} · still in All Mail`, `已归档 ${ids.length} 封 · 仍在“所有邮件”里`]),
      failed: t(['Couldn’t archive. Try again.', '归档没成功，请再试一次。']), back: t(['Couldn’t put them back.', '放不回收件箱。']) }) : Promise.resolve(),
    // One letter opened on its page: trashed, archived or marked as read.
    act: (kind: MailAct, m: Mail) => move([m.id], kind, DONE[kind][1], { done: t(DONE[kind][0]), failed: t(['That didn’t go through. Try again.', '没成功，请再试一次。']), back: t(['Couldn’t put it back.', '放不回去。']) }),
  };
}

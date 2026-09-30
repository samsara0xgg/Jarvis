// 回到上一个 (design point hist): ⌘[ goes back to the session you were in before, ⌘] forward again, the way a browser's
// history does. The page keeps each session's scroll and draft, so going back lands where you left it with what you
// were writing still there. Every way of switching counts (a row, her, the long exposure, a throw you followed).
import type { Feature, PageCtx } from './ctx';

export function mountHist(ctx: PageCtx): Feature {
  const H = { list: [] as string[], i: -1 };
  let walking = '';
  // A different session on screen is a step in the history, unless ⌘[ / ⌘] went there.
  function saw() {
    const s = ctx.current(), w = walking;
    walking = '';
    if (!s || s.id === w || H.list[H.i] === s.id) return;
    H.list = [...H.list.slice(0, H.i + 1).filter(x => x !== s.id), s.id];
    H.i = H.list.length - 1;
  }
  // The page swaps the conversation in .host whenever another one comes on screen.
  const host = ctx.win.querySelector('.host');
  if (host) new MutationObserver(saw).observe(host, { childList: true });
  const usable = (id: string) => { const s = ctx.byId(id); return !!s && !s.archived; };
  function go(d: -1 | 1) {
    // Off every session (the archive, an empty window), back is the one you were last in.
    let j = !ctx.current() && d < 0 ? H.i : H.i + d;
    while (j >= 0 && j < H.list.length && !usable(H.list[j])) j += d;
    if (j < 0 || j >= H.list.length) { ctx.toast(d < 0 ? '再往前没有了' : '已经是最近的了'); return; }
    H.i = j;
    const id = H.list[j];
    if (ctx.current()?.id !== id) { walking = id; ctx.open(id); }
    ctx.tick();
  }
  return {
    key(e) {
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey || e.isComposing) return false;
      const d = e.code === 'BracketLeft' || e.key === '[' ? -1 : e.code === 'BracketRight' || e.key === ']' ? 1 : 0;
      if (!d) return false;
      e.preventDefault(); e.stopImmediatePropagation();
      if (!e.repeat) go(d);
      return true;
    },
  };
}

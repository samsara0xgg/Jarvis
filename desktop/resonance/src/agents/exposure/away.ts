// The away line's times: each time the window went to the back for a while, when you left and when you came back.
// The last three are kept in this window's storage, so a reload keeps them; the sky draws them on its time axis.
export type Away = { a: number; b: number | null };
const KEY = 'agents.away', KEEP = 3;
// A glance at another app is not being away.
const WHILE = 5 * 60000;

export function mountAway() {
  let spans: Away[] = [];
  try { spans = (JSON.parse(localStorage.getItem(KEY) ?? '[]') as Away[]).filter(s => typeof s?.a === 'number' && (s.b === null || typeof s.b === 'number')); } catch { /* none kept */ }
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(spans)); } catch { /* not kept, that is all */ } };
  // Back: a span long enough stays, a short one goes.
  function back() {
    const last = spans.at(-1);
    if (!last || last.b !== null) return;
    if (Date.now() - last.a >= WHILE) last.b = Date.now(); else spans.pop();
    spans = spans.slice(-KEEP); save();
  }
  addEventListener('blur', () => {
    // Focus moving into a page shown in the window (a <webview>) is still being here.
    if (document.activeElement?.matches('webview,iframe') || spans.at(-1)?.b === null) return;
    spans.push({ a: Date.now(), b: null }); save();
  });
  addEventListener('focus', back);
  // Opened again while away (a reload behind the window): the span goes on until the window is in front.
  if (document.hasFocus()) back();
  return { spans: () => spans };
}

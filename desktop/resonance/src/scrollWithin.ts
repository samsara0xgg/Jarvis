// scrollIntoView also scrolls every ancestor up to the page itself, and the companion's page must never move (fitWindow).
// This scrolls only the nearest box around `el` that scrolls.
export function scrollWithin(el: HTMLElement, block: 'center' | 'nearest', smooth = false) {
  let box = el.parentElement;
  while (box && !(box.scrollHeight > box.clientHeight && /auto|scroll/.test(getComputedStyle(box).overflowY))) box = box.parentElement;
  if (!box) return;
  const b = box.getBoundingClientRect(), r = el.getBoundingClientRect();
  const delta = block === 'center' ? r.top + r.height / 2 - (b.top + b.height / 2)
    : r.top < b.top ? r.top - b.top : r.bottom > b.bottom ? Math.min(r.bottom - b.bottom, r.top - b.top) : 0;
  if (delta) box.scrollBy({ top: delta, behavior: smooth ? 'smooth' : 'auto' });
}

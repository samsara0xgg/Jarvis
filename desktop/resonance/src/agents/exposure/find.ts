// ⌘K in the long exposure: searching what was said. The sky keeps the sessions it is in, each standing on the newest
// sentence it is in; ⏎ goes in and lands on that sentence. The host searches every session, archived ones and ones this
// window has not read yet (GET /search); what each one said is then read here, to find the sentences.
import type { Item } from '../../../electron/agents/types';
import { esc } from './motion';
import './find.css';

const ICON = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="7" cy="7" r="4.3"/><path d="m10.3 10.3 3.2 3.2"/></svg>';
// The field in the horizon, where 收起 stands while the sky is open.
export function findField() {
  const el = document.createElement('label'); el.className = 'bw-find'; el.hidden = true;
  el.innerHTML = `${ICON}<input type="text" placeholder="搜标题，也搜说过的话" aria-label="搜说过的话" autocomplete="off" spellcheck="false"><kbd>esc</kbd>`;
  return { el, input: el.querySelector('input')! };
}
// The text with what you typed lit, wherever it is.
export function hl(text: string, q: string) {
  q = q.trim();
  const low = text.toLowerCase(), ql = q.toLowerCase();
  // Lower case that changes a string's length would shift every match: those look for the exact case.
  const [hay, needle] = low.length === text.length ? [low, ql] : [text, q];
  if (!q) return esc(text);
  let out = '', i = 0, j: number;
  while ((j = hay.indexOf(needle, i)) >= 0) { out += `${esc(text.slice(i, j))}<mark>${esc(text.slice(j, j + q.length))}</mark>`; i = j + q.length; }
  return out + esc(text.slice(i));
}
// The turns (counted by what you said, as the sky counts them) whose words, yours or its answers, hold q.
export function turnHits(items: Item[], q: string) {
  const out: number[] = [];
  let turn = -1;
  for (const it of items) {
    if (it.k === 'you') turn++;
    if (turn >= 0 && (it.k === 'you' || it.k === 'it') && it.text.toLowerCase().includes(q) && out[out.length - 1] !== turn) out.push(turn);
  }
  return out;
}
// Which sessions the host finds q in, titles and summaries included.
export async function search(call: (route: string) => Promise<unknown>, q: string) {
  const r = await call(`/search?q=${encodeURIComponent(q)}&all=1`) as { hits?: { id: string }[] };
  return new Set((r.hits ?? []).map(h => h.id));
}

// ---------- landing: the session is on screen, its conversation stands on the sentence ----------
let lit: { els: HTMLElement[]; marks: HTMLElement[]; timer: number } | null = null;
function unlight() {
  if (!lit) return;
  clearTimeout(lit.timer); for (const el of lit.els) el.classList.remove('hit');
  for (const m of lit.marks) if (m.isConnected) { const parent = m.parentNode!; parent.replaceChild(document.createTextNode(m.textContent ?? ''), m); parent.normalize(); }
  lit = null;
}
// Where q is in a text, as hl finds it.
const at = (s: string, q: string) => s.toLowerCase().length === s.length ? s.toLowerCase().indexOf(q.toLowerCase()) : s.indexOf(q);
// Wraps q where it is in el's text, and gives the marks.
function mark(el: Element, q: string) {
  const found: Text[] = [], walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) if (!n.parentElement?.closest('button,time,kbd') && at(n.textContent ?? '', q) >= 0) found.push(n as Text);
  const marks: HTMLElement[] = [];
  for (let n of found) {
    for (let j = at(n.data, q); j >= 0; j = at(n.data, q)) {
      const hit = n.splitText(j), rest = hit.splitText(q.length), m = document.createElement('mark');
      hit.replaceWith(m); m.append(hit); marks.push(m); n = rest;
    }
  }
  return marks;
}
// `item` is where you said it (its index in the conversation) and `upto` the first item of the next turn, once the
// session is the one on screen (`here`). What you typed is lit where it is, yours first, else in its answer; the view
// stands on it, and what you said and its answer glow for a moment.
export function land(win: HTMLElement, here: () => boolean, item: number, upto: number, q: string, tries = 60) {
  if (!here()) return;
  const box = win.querySelector<HTMLElement>('.host .conv'), list = box?.querySelector('.c-items'), el = list?.children[item] as HTMLElement | undefined;
  // Not drawn yet (still being read): the next frames.
  if (!box || !list || !el?.classList.contains('item')) { if (tries) requestAnimationFrame(() => land(win, here, item, upto, q, tries - 1)); return; }
  unlight();
  // The narrow column folds the earlier turns away; this one has to show.
  if (!el.getClientRects().length) win.querySelector('.chat')?.classList.add('all');
  let marks = q ? mark(el.querySelector('.you') ?? el, q) : [], ans: HTMLElement | null = null, first: HTMLElement | null = null;
  for (let k = item + 1; k < Math.min(upto, list.children.length) && !ans; k++) {
    const row = list.children[k] as HTMLElement, md = row.querySelector('.it .md');
    if (!md) continue;
    first ??= row;
    if (q && !marks.length) { marks = mark(md, q); if (marks.length) ans = row; } else break;
  }
  // Found only in an answer, the view stands on the words found.
  const spot = ans ? marks[0] : el, els = [el, ans ?? first].filter((x): x is HTMLElement => !!x);
  for (const x of els) x.classList.add('hit');
  // Rows above that come on screen trade their guessed height for their real one: the next frame stands again.
  const stand = () => { const d = spot.getBoundingClientRect().top - box.getBoundingClientRect().top - 56; if (Math.abs(d) > 2) box.scrollTop += d; };
  stand(); requestAnimationFrame(stand);
  lit = { els, marks, timer: window.setTimeout(unlight, 2600) };
}

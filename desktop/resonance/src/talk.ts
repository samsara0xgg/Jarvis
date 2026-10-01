import { plain, type Line, type Mark } from './model';

// The talk area under her: what it shows at each caption level, and how far along her words are.
// Captions (Settings › Voice, her right-click menu): everything, only what is worth reading, or nothing.
export type Captions = 'all' | 'brief' | 'none';
export type Voice = 'off' | 'listening' | 'thinking' | 'speaking';
// What it looks like: a capsule of state alone (full captions, nothing said yet), the smaller pill (the other two levels), or the area.
export type Kind = 'capsule' | 'pill' | 'area';
export const WIDTH = 360, HEIGHT = 360;
// What you just said stays in the pill this long, so you see what she heard.
export const HEARD_MS = 3000;
// After a turn is over and she is not listening, the area folds away; reopening within the window continues it.
export const LINGER_MS = 8000, MEMORY_MS = 10 * 60_000;

// With her voice turned off nothing else would reach you, so every level shows everything.
export const level = (captions: Captions, voiceOff: boolean): Captions => voiceOff ? 'all' : captions;

// Her answer is `<voice>` (what she says) and `<document>` (what is written); an answer with neither tag is both at once (ADR 0040).
// The same words twice are shown once.
const words = (text: string) => text.replace(/[\s*`#|:\-–—，。！？、,.!?]/g, '');
export function split(raw: string): { spoken: string; written: string } {
  const voice = /<voice>([\s\S]*?)(?:<\/voice>|$)/.exec(raw), doc = /<document>([\s\S]*?)(?:<\/document>|$)/.exec(raw);
  const spoken = voice ? plain(voice[1]) : doc ? '' : plain(raw);
  const written = doc ? doc[1].replace(/\r/g, '').replace(/<\/?[a-z]*$/, '').trim() : '';
  return { spoken, written: words(written) === words(spoken) ? '' : written };
}

// One thing on screen: what you said, or her answer split into the part she says and the part that is written.
export type Item = { id: string; who: 'you' | 'her'; spoken: string; written: string; failed: boolean; at: number; said: boolean; cutAt?: number; turn?: string; queued: boolean; from: number; mark?: Mark;
  // She says something besides what is written, though it is not shown (the middle level shows the written part alone): the written part writes itself in.
  voiced?: boolean };
// Full: everything. The middle level: only what is written (lists, times, places, links), and what she says right after it.
// Hidden: nothing. An error always shows.
export function itemsOf(lines: Line[], captions: Captions): Item[] {
  const out: Item[] = [];
  for (const l of lines) {
    const base = { id: l.id, at: l.at, said: !!l.said, cutAt: l.cutAt, turn: l.turn, queued: !!l.queued, from: l.from ?? l.at, mark: l.mark };
    if (l.failed) out.push({ ...base, who: 'her', spoken: l.text, written: '', failed: true, said: true });
    else if (l.who === 'you') { if (captions === 'all') out.push({ ...base, who: 'you', spoken: l.text, written: '', failed: false }); }
    else {
      const { spoken, written } = split(l.text);
      if (captions === 'all' && (spoken || written)) out.push({ ...base, who: 'her', spoken, written, failed: false });
      else if (captions === 'brief' && written) out.push({ ...base, who: 'her', spoken: '', voiced: !!spoken, written, failed: false });
      // A spoken-only answer that follows an answer on screen (a shorter version, a correction, then another) shows too,
      // so the area never reads one answer while she says another.
      else if (captions === 'brief' && spoken && out.at(-1)?.who === 'her' && !out.at(-1)?.failed) out.push({ ...base, who: 'her', spoken, written: '', failed: false });
    }
  }
  return out;
}

export function kindOf(captions: Captions, items: Item[], field: boolean, expanded: boolean): Kind {
  if (field) return 'area';
  if (captions === 'all') return items.length ? 'area' : 'capsule';
  return expanded && items.length ? 'area' : 'pill';
}

// How far she has got: the daemon says where her voice is (ADR 0112) and the pace below carries it on until the next report;
// until the first report, and for a daemon that sends none, it is all estimated from when she began. About 4.5 characters a
// second for Chinese, counting its punctuation; Latin letters are spoken about three times as fast.
export const SPEED = 4.5;
const WIDE = /[⺀-鿿豈-﫿＀-￯　-〿]/;
const cost = (ch: string) => WIDE.test(ch) ? 1 : /[A-Za-z0-9]/.test(ch) ? .32 : /\s/.test(ch) ? .25 : .8;
// Seconds, from the start, at which each character of the text has been said.
export function pace(text: string): number[] {
  let t = 0;
  return [...text].map(ch => (t += cost(ch) / SPEED));
}
// How many characters have been said after this many seconds.
export function said(clock: number[], seconds: number): number {
  let lo = 0, hi = clock.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (clock[mid] <= seconds) lo = mid + 1; else hi = mid; }
  return lo;
}
// The daemon counts letters and digits of what was played (speech and caption differ in marks, spaces and markdown): this many
// characters of the text have been said, the marks right after the last of them included.
const LETTER = /[\p{L}\p{N}]/u;
export function reach(chars: string[], n: number): number {
  let seen = 0, i = 0;
  if (n <= 0) return 0;
  for (; i < chars.length; i++) if (LETTER.test(chars[i]) && ++seen === n) { i++; break; }
  while (seen >= n && i < chars.length && !/\s/.test(chars[i]) && !LETTER.test(chars[i])) i++;
  return i;
}
// Characters said at `now` by the last report: from where it put her, at pace, no further than the end of the segment it says she is in;
// held (a barge-in being judged) or stopped, the report's clock stops.
export function placed(text: string, clock: number[], m: Mark, now: number): number {
  const chars = [...text], at = reach(chars, m.n), from = at ? clock[at - 1] : 0;
  return Math.max(at, Math.min(said(clock, from + (Math.min(now, m.hold ?? now) - m.at) / 1000), reach(chars, m.ahead)));
}
// The written part writes itself in at a pace of its own, not her voice's: INK units a second (a Chinese character is 1, other characters .4),
// a paragraph in at most INK_CAP s, paragraphs one after another. Seconds, from the block's first showing, at which each character is reached.
export const INK = 25, INK_CAP = 2, INK_LEAD = .3, INK_GAP = .15;
export function ink(paras: string[][]): number[] {
  let t = INK_LEAD;
  const out: number[] = [];
  for (const chars of paras) {
    const w = chars.map(ch => WIDE.test(ch) ? 1 : .4), total = w.reduce((a, b) => a + b, 0), secs = Math.min(total / INK, INK_CAP);
    let acc = 0;
    for (const x of w) out.push(t + (acc += x) / total * secs);
    t += secs + INK_GAP;
  }
  return out;
}
// The text cut into sentences: each ends at its terminal mark, or at a line break.
export function sentences(text: string): string[][] {
  const out: string[][] = [];
  let sentence: string[] = [];
  const chars = [...text];
  chars.forEach((ch, i) => {
    sentence.push(ch);
    const next = chars[i + 1];
    if (/[。！？!?]/.test(ch) ? !(next && /[。！？!?”’"')）]/.test(next)) : ch === '\n' || (ch === '.' && (!next || /\s/.test(next)))) { out.push(sentence); sentence = []; }
  });
  if (sentence.length) out.push(sentence);
  return out;
}

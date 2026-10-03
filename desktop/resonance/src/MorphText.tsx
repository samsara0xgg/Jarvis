import { Fragment, useLayoutEffect, useMemo, useRef } from 'react';

// Words morph from one text to another: what stays stays still, new words fade and blur in one after another,
// removed words fade out and close up. Meant for any text Jarvis rewrites for you; the caller swaps back to its own field in onDone.
type Seg = { s: string; k: 'same' | 'add' | 'del' };
const IN_MS = 320, OUT_MS = 240, STAGGER = 25, SPREAD = 1500;
const words = (text: string) => [...new Intl.Segmenter('zh', { granularity: 'word' }).segment(text)].map(x => x.segment);
// A longest-common-subsequence diff over words; a replaced word shows the old one leaving before the new one arrives.
export function diffWords(a: string, b: string): Seg[] | null {
  const x = words(a), y = words(b), n = x.length, m = y.length;
  if (n * m > 4_000_000) return null; // ponytail: the LCS table is O(n*m); past this the caller just swaps the text
  const l = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) l[i][j] = x[i] === y[j] ? l[i + 1][j + 1] + 1 : Math.max(l[i + 1][j], l[i][j + 1]);
  const out: Seg[] = [];
  const put = (s: string, k: Seg['k']) => { const last = out.at(-1); if (k === 'same' && last?.k === 'same') last.s += s; else out.push({ s, k }); };
  for (let i = 0, j = 0; i < n || j < m;) {
    if (i < n && j < m && x[i] === y[j]) { put(x[i++], 'same'); j++; }
    else if (i < n && (j === m || l[i + 1][j] >= l[i][j + 1])) put(x[i++], 'del');
    else put(y[j++], 'add');
  }
  return out;
}

export function MorphText({ from, to, onDone, className }: { from: string; to: string; onDone: () => void; className?: string }) {
  const segs = useMemo(() => diffWords(from, to), [from, to]), box = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const el = box.current;
    if (!segs || !el || matchMedia('(prefers-reduced-motion: reduce)').matches) { onDone(); return; }
    const adds = [...el.querySelectorAll<HTMLElement>('.mt-add')], dels = [...el.querySelectorAll<HTMLElement>('.mt-del')];
    const gap = Math.min(STAGGER, SPREAD / Math.max(adds.length, 1));
    const runs = [
      ...adds.map((e, i) => e.animate([{ opacity: 0, filter: 'blur(4px)' }, { opacity: 1, filter: 'blur(0)' }], { duration: IN_MS, delay: i * gap, easing: 'ease-out', fill: 'backwards' })),
      ...dels.map(e => { const w = `${e.offsetWidth}px`; return e.animate([{ opacity: 1, maxWidth: w }, { opacity: 0, maxWidth: w, offset: .5 }, { opacity: 0, maxWidth: '0px' }], { duration: OUT_MS * 2, easing: 'ease', fill: 'forwards' }); }),
    ];
    let live = true;
    void Promise.allSettled(runs.map(a => a.finished)).then(() => { if (live) onDone(); });
    return () => { live = false; runs.forEach(a => a.cancel()); };
  }, [segs]);
  return <span ref={box} className={`mt ${className ?? ''}`}>{segs?.map((g, i) => g.k === 'same' ? <Fragment key={i}>{g.s}</Fragment> : <span key={i} className={`mt-${g.k}`}>{g.s}</span>)}</span>;
}

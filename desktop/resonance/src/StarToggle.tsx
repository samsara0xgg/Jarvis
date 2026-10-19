import { useEffect, useRef } from 'react';
import { Star } from '@phosphor-icons/react';
import type { L } from './companionSettings';
import { EASE, reduced } from './RowOpen';

// The star that marks what mattered (ADR 0296), in a row's hover actions and in its open body. His mark is a filled warm-white star, her pick an
// outlined one, and an item nobody marked a dim outline. Marking it scales the star 0.6, 1.15, 1 with four small sparks (300 ms); a mark the brain
// refused (five of his are standing) shakes it once (120 ms). With reduced motion nothing scales, flies or shakes: the star fades in over 120 ms.
export type StarMark = 'him' | 'her' | null;
const SPARKS: readonly (readonly [number, number])[] = [[-14, -14], [14, -14], [-14, 14], [14, 14]];
function pop(button: HTMLElement) {
  const glyph = button.querySelector<SVGElement>('svg');
  if (reduced.matches) { glyph?.animate([{ opacity: 0 }, {}], { duration: 120 }); return; }
  glyph?.animate([{ transform: 'scale(.6)' }, { transform: 'scale(1.15)', offset: .55 }, { transform: 'scale(1)' }], { duration: 300, easing: EASE });
  for (const [x, y] of SPARKS) {
    const spark = document.createElement('i');
    spark.className = 'dt-spark';
    button.append(spark);
    spark.animate([{ transform: 'translate(0, 0) scale(.5)', opacity: 1 }, { transform: `translate(${x}px, ${y}px) scale(1)`, opacity: 0 }], { duration: 300, easing: 'ease-out', fill: 'forwards' }).onfinish = () => spark.remove();
  }
}
function shake(button: HTMLElement) {
  button.animate(reduced.matches ? [{ opacity: .35 }, {}] : [{ transform: 'translateX(0)' }, { transform: 'translateX(-3px)' }, { transform: 'translateX(3px)' }, { transform: 'translateX(0)' }], { duration: 120, easing: 'ease-in-out' });
}

// `nth` counts the refusals of this item: when it goes up the star shakes.
export function StarToggle({ mark, nth, press, t }: { mark: StarMark; nth: number; press: () => void; t: (l: L) => string }) {
  const box = useRef<HTMLButtonElement>(null), was = useRef(mark), shook = useRef(nth), his = mark === 'him';
  useEffect(() => { if (his && was.current !== 'him' && box.current) pop(box.current); was.current = mark; }, [mark]);
  useEffect(() => { if (nth !== shook.current && box.current) shake(box.current); shook.current = nth; }, [nth]);
  const label = his ? t(['Unmark', '取消标记']) : t(['Mark as important', '标为重要']);
  return <button ref={box} type="button" className={`dt-starbtn${his ? ' is-his' : mark ? ' is-hers' : ''}`} tabIndex={-1} aria-pressed={his} aria-label={label} title={label}
    onClick={e => { e.stopPropagation(); press(); }}><Star size={14} weight={his ? 'fill' : 'bold'}/></button>;
}

import { useRef, useState, type ReactNode } from 'react';
import type { Icon as PhosphorIcon } from '@phosphor-icons/react';
import type { L } from './companionSettings';
import type { Card, CardPart, Pic } from './numberCard';
import { Ic } from './dayIcons';
import { Bars, Hypnogram } from './RowOpen';

// What hangs under the numbers when one is pinned (numberCard.ts made the lines; DayBar.tsx owns when it is there). In the look of the range's
// card: a head (the number's glyph, its name, its value) and under it the parts, each only if it has something to say.
type T = (l: L) => string;

function Photo({ p, port, onPicture, t }: { p: Pic; port: string | null; onPicture: (id: string, note: string) => void; t: T }) {
  const [broken, setBroken] = useState(false);
  return <button type="button" className="dt-npic" data-id={p.id} aria-label={`${t(['Open the picture', '看大图'])} ${p.at}`} onClick={() => onPicture(p.id, p.note)}>
    {port && !broken ? <img alt="" decoding="async" width={56} height={56} src={`http://127.0.0.1:${port}/inherent/attachments/${p.id}`} onError={() => setBroken(true)}/> : <span className="dt-npic-no"><Ic icon="picture" size={16}/></span>}
    <time>{p.at}</time>
  </button>;
}

function Part({ part, port, onPicture, t }: { part: CardPart; port: string | null; onPicture: (id: string, note: string) => void; t: T }) {
  switch (part.type) {
    case 'cells': return <div className="dt-ncs">{part.cells.map((c, i) => <span key={i} className="dt-nc"><small>{c.label}</small><b>{c.value}</b></span>)}</div>;
    case 'night': return <Hypnogram night={part.night} t={t}/>;
    case 'bars': return <div className="dt-nbars">{part.groups.map(g => <Bars key={g.heading} heading={g.heading} bars={g.bars}/>)}</div>;
    case 'entries': return <ul className="dt-nes">
      {part.entries.map((e, i) => <li key={i}><span className="dt-nic">{e.icon && <Ic icon={e.icon} size={13}/>}</span><b>{e.lead}</b>{e.facts.map((f, j) => <em key={j}>{f}</em>)}</li>)}
      {part.more > 0 && <li className="dt-nmore">+{part.more}</li>}
    </ul>;
    case 'pictures': return <div className="dt-npics">{part.list.map(p => <Photo key={p.id} p={p} port={port} onPicture={onPicture} t={t}/>)}{part.more > 0 && <em className="dt-nmore">+{part.more}</em>}</div>;
  }
}

export function CardBody({ card, Glyph, color, port, onPicture, t }: { card: Card; Glyph: PhosphorIcon; color: string; port: string | null; onPicture: (id: string, note: string) => void; t: T }) {
  return <>
    <div className="dt-ch"><span className="dt-cic" style={{ color }}><Glyph size={14} aria-hidden="true"/></span><b>{card.label}</b><span>{card.value}</span></div>
    {card.parts.length > 0 && <div className="dt-nb">{card.parts.map((p, i) => <Part key={`${p.type}${i}`} part={p} port={port} onPicture={onPicture} t={t}/>)}</div>}
  </>;
}

// Two layers that swap when `id` changes: the words that were fade out, then the new ones fade in (the style does that), never both at once.
export function Swap({ id, children }: { id: string; children: ReactNode }) {
  const kept = useRef<{ cur: 0 | 1; at: [{ id: string; node: ReactNode } | null, { id: string; node: ReactNode } | null] }>({ cur: 0, at: [null, null] }), k = kept.current;
  if (k.at[k.cur]?.id !== id) k.cur = k.cur ? 0 : 1;
  k.at[k.cur] = { id, node: children };
  return <>{k.at.map((p, i) => <div key={i} className={`dt-slot${i === k.cur ? ' is-cur' : ''}`}>{p?.node}</div>)}</>;
}

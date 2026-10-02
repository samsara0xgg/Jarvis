import { useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { CaretDown } from '@phosphor-icons/react';
import { useT } from './companionSettings';
import type { Brief, BriefRow, BriefSection } from './homeData';

// A section lists this many rows before "N more"; what Allen decided and what Jarvis suggests wait behind one fold.
const SHOWN = 5;
const LATER: readonly string[] = ['decisions', 'suggestions'];

// A work item is its title and a word for how it ended; opening it shows its note. A bullet is its text, opened when it was cut short.
function Row({ row }: { row: BriefRow }) {
  const [open, setOpen] = useState(false), [cut, setCut] = useState(false), title = useRef<HTMLSpanElement>(null);
  const more = [row.note, row.status && row.status !== row.label ? row.status : ''].filter(Boolean);
  useLayoutEffect(() => { const el = title.current; if (el && !open) setCut(el.scrollHeight > el.clientHeight + 1); }, [row.text, open]);
  const line = <span className="bf-line">
    <i className={`bf-dot${row.tag ? '' : ' is-quiet'}`}/>
    <span ref={title} className="bf-title">{row.text}</span>
    {row.label && <span className="bf-tag">{row.label}</span>}
    {more.length > 0 && <CaretDown size={11} className="bf-caret"/>}
  </span>;
  const style = { '--c': `var(--bf-${row.tag ?? 'quiet'})` } as CSSProperties;
  if (!more.length && !cut && !open) return <div className="bf-row" style={style} data-tag={row.tag}>{line}</div>;
  return <>
    <button className="bf-row" style={style} data-tag={row.tag} aria-expanded={open} onClick={() => setOpen(value => !value)}>{line}</button>
    {more.length > 0 && <div className="bf-more-body" data-open={open} inert={!open}><div>{more.map((text, i) => <p key={i}>{text}</p>)}</div></div>}
  </>;
}

function Section({ section }: { section: BriefSection }) {
  const t = useT(), [all, setAll] = useState(false);
  const rows = all ? section.rows : section.rows.slice(0, SHOWN), rest = section.rows.length - rows.length;
  return <section className="bf-sec" data-section={section.key}>
    <h4>{section.title}<b>{section.rows.length}</b></h4>
    <ul>{rows.map((row, i) => <li key={i}><Row row={row}/></li>)}</ul>
    {rest > 0 && <button className="bf-rest" onClick={() => setAll(true)}>{t([`${rest} more`, `还有 ${rest} 项`])}</button>}
  </section>;
}

export function BriefPage({ brief }: { brief: Brief }) {
  const t = useT(), [later, setLater] = useState(false);
  const now = brief.sections.filter(section => !LATER.includes(section.key)), rest = brief.sections.filter(section => LATER.includes(section.key));
  return <div className="bf">
    <p className="bf-lead">{brief.lead || brief.summary}</p>
    {brief.sections.length === 0 && <p className="muted">{t(['Nothing was recorded for yesterday.', '昨天没有记录到工作。'])}</p>}
    {now.map(section => <Section key={section.key} section={section}/>)}
    {rest.length > 0 && <div className="bf-later">
      <button className="fold" aria-expanded={later} onClick={() => setLater(value => !value)}>{rest.map(section => `${section.title} ${section.rows.length}`).join(' · ')}<CaretDown size={11}/></button>
      <div className="fold-body" inert={!later}><div>{rest.map(section => <Section key={section.key} section={section}/>)}</div></div>
    </div>}
  </div>;
}

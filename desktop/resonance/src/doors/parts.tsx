import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ArrowUp } from '@phosphor-icons/react';
import type { L } from '../companionSettings';
import type { About } from '../dayOpen';
import type { DoorCtx } from '../doors';

// What a door's page in the V2 right column is made of (styles: day-doors.css): a head (the circle, the title and one line, then a time or 问她 at the right), a section heading, a meter with a mark, and the
// buttons. The hooks in this folder put these together; the frame (DayDoors.tsx) only gives the column.
type T = (l: L) => string;

// 问她 on a page: a pill in the head that opens one line to type into (the day's 问她 dock belongs to the day's rows). What he types goes to the same route as the dock's (`ctx.ask`), with `lead` before it
// to say what it is about: the brain has no kind for a letter, a to-do or a session, so the words carry it (as a stretch of the day's do, dayOpen.ts).
export type DoorAsk = { send: NonNullable<DoorCtx['ask']>; lead: string };
export function useAsk(ctx: Pick<DoorCtx, 'ask' | 't' | 'lang'>): (about: string) => DoorAsk | null {
  // `ctx` is made anew at every render of the pane: what is read when he types is the latest, and what a page is made of changes only with the language or with there being a route to ask at all.
  const now = useRef(ctx); now.current = ctx;
  const has = !!ctx.ask;
  return useMemo(() => (about: string) => has ? { send: (text: string, about?: About) => now.current.ask!(text, about), lead: now.current.t([`About "${about}": `, `关于「${about}」：`]) } : null, [has, ctx.lang]);
}

export function DoorHead({ icon, who, logo, title, sub, time, ask, t }: { icon: ReactNode; who?: boolean; logo?: boolean; title: ReactNode; sub?: ReactNode; time?: string; ask?: DoorAsk | null; t: T }) {
  const [open, setOpen] = useState(false), [draft, setDraft] = useState(''), [state, setState] = useState<'idle' | 'busy' | 'sent' | 'failed'>('idle'), box = useRef<HTMLInputElement>(null);
  useEffect(() => { if (open) box.current?.focus(); }, [open]);
  useEffect(() => { if (state !== 'sent') return; const id = window.setTimeout(() => { setState('idle'); setOpen(false); }, 1500); return () => clearTimeout(id); }, [state]);
  const send = async () => {
    const text = draft.trim();
    if (!ask || !text || state === 'busy') return;
    setState('busy');
    try { await ask.send(`${ask.lead}${text}`); setDraft(''); setState('sent'); } catch { setState('failed'); }
  };
  return <div className="dd-head">
    <i className={`dt-ic${who ? ' is-who' : ''}${logo ? ' is-logo' : ''}`}>{icon}</i>
    <span className="dd-ht"><b>{title}</b>{sub && <em>{sub}</em>}</span>
    {time ? <time>{time}</time> : ask && <button type="button" className="dt-ask" aria-expanded={open} onClick={() => setOpen(o => !o)}>{t(['Ask', '问她'])}</button>}
    {ask && open && <form className="dd-ask" onSubmit={e => { e.preventDefault(); void send(); }}>
      <input ref={box} value={draft} disabled={state === 'busy'} aria-label={t(['Ask her', '问她'])} placeholder={state === 'sent' ? t(['Sent', '发给她了']) : state === 'failed' ? t(["Didn't go through", '没发出去']) : t(['Ask her…', '问她…'])}
        onChange={e => { setDraft(e.target.value); if (state === 'failed') setState('idle'); }} onKeyDown={e => { if (e.key === 'Escape') { e.stopPropagation(); setOpen(false); } }}/>
      <button type="submit" aria-label={t(['Send', '发送'])} disabled={!draft.trim() || state === 'busy'}><ArrowUp size={12} weight="bold"/></button>
    </form>}
  </div>;
}

// A heading inside a page: a small label at the left, a clock or a date at the right.
export const DoorSec = ({ children, em }: { children: ReactNode; em?: ReactNode }) => <div className="dd-sec"><b>{children}</b>{em && <em>{em}</em>}</div>;

// A bar of 0..1 with a mark (the time that has gone): the fill is the page's own colour.
const pct = (v: number) => `${Math.max(0, Math.min(1, v)) * 100}%`;
export const DoorMeter = ({ fill, mark }: { fill: number; mark?: number | null }) => <div className="dd-meter" role="img"><i style={{ width: pct(fill) }}/>{mark != null && <u style={{ left: pct(mark) }}/>}</div>;

// The buttons of a page, and one button: `yes` is the amber answer to what waits on him, `quiet` an outline.
export const DoorActs = ({ children }: { children: ReactNode }) => <div className="dd-acts">{children}</div>;
export const DoorBtn = ({ children, onClick, tone }: { children: ReactNode; onClick: () => void; tone?: 'yes' | 'quiet' | 'done' }) => <button type="button" className={`dd-btn${tone ? ` is-${tone}` : ''}`} onClick={onClick}>{children}</button>;

// A button that asks her something at once (the words are fixed), and says it was sent.
export function AskBtn({ ask, text, label, t }: { ask: DoorAsk; text: string; label: string; t: T }) {
  const [state, setState] = useState<'idle' | 'busy' | 'sent' | 'failed'>('idle');
  const go = async () => { if (state === 'busy') return; setState('busy'); try { await ask.send(`${ask.lead}${text}`); setState('sent'); } catch { setState('failed'); } };
  return <DoorBtn onClick={() => void go()}>{state === 'sent' ? t(['Sent', '发给她了']) : state === 'failed' ? t(["Didn't go through", '没发出去']) : label}</DoorBtn>;
}

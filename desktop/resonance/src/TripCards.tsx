import { useEffect, useRef, useState } from 'react';
import { Check, X } from '@phosphor-icons/react';
import { postRoute } from './homeData';
import { tr, type L, type Lang } from './companionSettings';
import { useEscape } from './Notices';
import { isDeparture, modesLine, offerLine, offerRoute, pinStatus, pinTrip, tripTid, type Departure, type TripCardData } from './pin';
import type { NotchNote } from './Notch';

// ADR 0203: the notch's card for the pinned bus trip: the pill opens a card with the trips, what the live refresh knows, and Next
// bus / Cancel; Cancel leaves a 5 s "Unpinned · Undo" line. ADR 0205: the bus lookup's rows are not here but in the conversation
// (TripCard below). All of it is one daemon route, POST /inherent/departure.
const UNDO_MS = 5000, JUST_OPENED_MS = 800;
type View = 'pin' | 'undo' | null;

type Reply = { departure?: unknown; reason?: string | null };
export async function departureAct(port: string | null, body: Record<string, unknown>) {
  const r = await postRoute(port ?? '', '/inherent/departure', body) as Reply;
  return { d: isDeparture(r.departure) ? r.departure : null, reason: r.reason ?? null };
}
// What the conversation's bus card needs: the pin now served (to mark its rows) and the route (which also updates it).
export type TripLink = { departure: Departure | null; act: (body: Record<string, unknown>) => Promise<string | null> };
export const tripLink = (port: string | null, departure: Departure | null, setDeparture: (d: Departure | null) => void): TripLink => ({
  departure,
  act: async body => { const r = await departureAct(port, body); setDeparture(r.d); return r.reason; },
});

// ADR 0205: the bus card in the conversation, drawn from the ask card's `trip`. A row's button adds that bus to the pin (up to three; it
// shows ✓ while the pin holds it, and a second press takes it off); the closing × and the card's lifetime are the ask card's.
export function TripCard({ trip, link, lang, onClose }: { trip: TripCardData; link: TripLink; lang: Lang; onClose: () => void }) {
  const t = (l: L) => tr(lang, l), [msg, setMsg] = useState(''), held = new Set(link.departure?.trips?.map(x => x.id));
  const press = (o: TripCardData['options'][number]) => {
    const tid = tripTid(trip, o);
    setMsg('');
    link.act(held.has(tid) ? { action: 'remove', trip_id: tid } : { action: 'add', offer_id: trip.offer_id, index: o.index })
      .then(reason => { if (reason === 'full') setMsg(t(['Three are pinned already', '已经挂满三班了'])); })
      .catch(() => setMsg(t(['That bus is no longer on offer', '这班车已经过期了'])));
  };
  return <div className="ac tc" data-question={trip.offer_id}>
    <div className="ac-bar"><span className="ac-label"><i/>{t(['Bus', '公交'])}</span>
      <button type="button" className="ac-x" aria-label={t(['Dismiss', '关掉'])} onClick={onClose}><X size={10} weight="bold"/></button></div>
    {modesLine(trip, t) && <p className="tc-modes">{modesLine(trip, t)}</p>}
    <ul className="tc-rows">{trip.options.map(o => <li key={o.index} className="tc-row">
      <span className="tc-route">🚌 {offerRoute(o)}</span>
      <span className="tc-line">{offerLine(o, t)}</span>
      <button type="button" className={`qc-choice${held.has(tripTid(trip, o)) ? ' is-on' : ''}`} aria-pressed={held.has(tripTid(trip, o))} onClick={() => press(o)}>
        {held.has(tripTid(trip, o)) ? t(['✓ Pinned', '✓ 已挂']) : t(['Pin', '挂上'])}</button>
    </li>)}</ul>
    {msg && <p className="ac-more">{msg}</p>}
  </div>;
}

function PinCard({ d, lang, msg, onNext, onCancel, onDrop, onClose }: { d: Departure; lang: Lang; msg: string; onNext: () => void; onCancel: () => void; onDrop: (tid: string) => void; onClose: () => void }) {
  const t = (l: L) => tr(lang, l), root = useRef<HTMLDivElement>(null);
  useEscape(root, true, onClose, d.id);
  return <div ref={root} className="nc nc-mail">
    <div className="nc-bar"><span className="nc-label is-wait"><i/>{t(['Pinned bus', '挂着的车'])}</span>
      <button type="button" className="nc-x nc-dismiss" aria-label={t(['Close', '关闭'])} title={t(['Close', '关闭'])} onClick={onClose}><X size={14}/></button></div>
    <ul className="nc-away nc-jobrows">{(d.trips ?? [d]).map(x => <li key={x.id} className="nc-jobrow">
      <span className="nc-jr-who">{pinTrip(x, t)}{pinStatus(x, t) && <span className="tagc">{pinStatus(x, t)}</span>}</span>
      <button type="button" className="nc-x nc-dismiss" aria-label={t(['Remove this trip', '去掉这班'])} title={t(['Remove this trip', '去掉这班'])} onClick={() => onDrop(x.id)}><X size={12}/></button>
    </li>)}</ul>
    {msg && <div className="nc-tags"><span className="tagc">{msg}</span></div>}
    <div className="nc-choice">
      <button type="button" className="btn btn-warm" onClick={onNext}>{t(['Next bus', '换下一班'])}</button>
      <button type="button" className="btn btn-ghost" onClick={onCancel}>{t(['Cancel', '取消'])}</button>
    </div>
  </div>;
}

function UndoCard({ lang, onUndo }: { lang: Lang; onUndo: () => void }) {
  const t = (l: L) => tr(lang, l);
  return <div className="nc"><p className="nc-ok"><Check size={14} weight="bold"/><span>{t(['Unpinned', '已取消'])} ·</span>
    <button type="button" className="btn btn-ghost" onClick={onUndo}>{t(['Undo', '撤销'])}</button></p></div>;
}

export function useTripNote({ port, lang, departure, setDeparture, leaveDeparture }: {
  port: string | null; lang: Lang; departure: Departure | null; setDeparture: (d: Departure | null) => void; leaveDeparture: (d: Departure) => void;
}): { note: NotchNote | null; open: () => void; away: () => void } {
  const [view, setView] = useState<View>(null), [msg, setMsg] = useState(''), undo = useRef<Departure | null>(null), openedAt = useRef(0);
  const t = (l: L) => tr(lang, l), close = () => setView(null);
  const send = (body: Record<string, unknown>) => departureAct(port, body);
  useEffect(() => { if (view !== 'undo') return; const timer = setTimeout(close, UNDO_MS); return () => clearTimeout(timer); }, [view]);
  useEffect(() => { if (view === 'pin' && !departure) close(); }, [departure?.id]);

  const next = () => {
    void send({ action: 'next' }).then(r => {
      if (r.d && !r.reason) { setDeparture(r.d); setMsg(''); } else setMsg(t(['No later bus', '没有更晚的车了']));
    }).catch(() => close());
  };
  const cancel = () => { if (!departure) return; undo.current = departure; leaveDeparture(departure); setView('undo'); };
  // One trip off the set: the daemon takes the whole pin off with the last one. Either way the undo line shows, and undo brings back just what this took.
  const drop = (tid: string) => {
    if (!departure) return;
    const was = departure;
    undo.current = was;
    void send({ action: 'remove', trip_id: tid }).then(r => {
      if (r.d) setDeparture(r.d); else leaveDeparture(was);
      setView('undo');
    }).catch(() => undefined);
  };
  const bringBack = () => {
    const gone = undo.current;
    close();
    if (gone) void send({ action: 'undo', pin_id: gone.id }).then(r => { if (r.d) setDeparture(r.d); }).catch(() => undefined);
  };

  const note: NotchNote | null = view === 'pin' && departure
    ? { key: 'trip:pin', onClose: close, card: <PinCard d={departure} lang={lang} msg={msg} onNext={next} onCancel={cancel} onDrop={drop} onClose={close}/> }
    : view === 'undo' ? { key: 'trip:undo', onClose: close, card: <UndoCard lang={lang} onUndo={bringBack}/> }
    : null;
  return {
    note,
    open: () => { openedAt.current = performance.now(); setMsg(''); setView('pin'); },
    // A press anywhere else puts the pin's card away, not the press that opened it.
    away: () => { if (view === 'pin' && performance.now() - openedAt.current > JUST_OPENED_MS) close(); },
  };
}

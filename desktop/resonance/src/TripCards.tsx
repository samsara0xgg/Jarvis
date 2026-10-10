import { useEffect, useRef, useState } from 'react';
import { Check, X } from '@phosphor-icons/react';
import { postRoute } from './homeData';
import { tr, type L, type Lang } from './companionSettings';
import { useEscape } from './Notices';
import { isDeparture, offerLine, offerRoute, pinStatus, pinTrip, type Departure, type TransitOffer } from './pin';
import type { NotchNote } from './Notch';

// ADR 0203: the cards under the notch for the bus trip. After a bus lookup one row per option with a Pin button (it closes itself
// after 60 s, or when a newer lookup replaces it); the pinned pill opens a card with the trip, what the live refresh knows, and Next
// bus / Cancel; Cancel leaves a 5 s "Unpinned · Undo" line. All of it is one daemon route, POST /inherent/departure.
const OFFER_MS = 60_000, UNDO_MS = 5000, JUST_OPENED_MS = 800;
type View = 'pin' | 'undo' | null;

function OfferCard({ offer, lang, onPin, onClose }: { offer: TransitOffer; lang: Lang; onPin: (index: number) => void; onClose: () => void }) {
  const t = (l: L) => tr(lang, l), root = useRef<HTMLDivElement>(null);
  useEscape(root, true, onClose, offer.id);
  return <div ref={root} className="nc nc-jobs">
    <div className="nc-bar"><span className="nc-label is-other"><i/>{t(['Pin the bus', '挂上这趟车'])}</span>
      <button type="button" className="nc-x nc-dismiss" aria-label={t(['Dismiss', '关掉'])} title={t(['Dismiss', '关掉'])} onClick={onClose}><X size={14}/></button></div>
    <ul className="nc-away nc-jobrows">{offer.options.map(o => <li key={o.index} className="nc-jobrow">
      <span className="tagc">🚌 {offerRoute(o)}</span>
      <span className="nc-jr-who">{offerLine(o, t)}</span>
      <button type="button" className="btn btn-warm" onClick={() => onPin(o.index)}>{t(['Pin', '挂上'])}</button>
    </li>)}</ul>
  </div>;
}

function PinCard({ d, lang, msg, onNext, onCancel, onClose }: { d: Departure; lang: Lang; msg: string; onNext: () => void; onCancel: () => void; onClose: () => void }) {
  const t = (l: L) => tr(lang, l), root = useRef<HTMLDivElement>(null), status = pinStatus(d, t);
  useEscape(root, true, onClose, d.id);
  return <div ref={root} className="nc nc-mail">
    <div className="nc-bar"><span className="nc-label is-wait"><i/>{t(['Pinned bus', '挂着的车'])}</span>
      <button type="button" className="nc-x nc-dismiss" aria-label={t(['Close', '关闭'])} title={t(['Close', '关闭'])} onClick={onClose}><X size={14}/></button></div>
    <p className="nc-what">{pinTrip(d, t)}</p>
    {(status || msg) && <div className="nc-tags">{status && <span className="tagc">{status}</span>}{msg && <span className="tagc">{msg}</span>}</div>}
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

type Reply = { departure?: unknown; reason?: string | null };
export function useTripNote({ port, lang, offer, departure, blocked, closeOffer, setDeparture, leaveDeparture }: {
  port: string | null; lang: Lang; offer: TransitOffer | null; departure: Departure | null; blocked: boolean;
  closeOffer: (id: string) => void; setDeparture: (d: Departure | null) => void; leaveDeparture: (d: Departure) => void;
}): { note: NotchNote | null; open: () => void; away: () => void } {
  const [view, setView] = useState<View>(null), [msg, setMsg] = useState(''), undo = useRef<Departure | null>(null), openedAt = useRef(0);
  const t = (l: L) => tr(lang, l), close = () => setView(null);
  const send = async (body: Record<string, unknown>) => {
    const r = await postRoute(port ?? '', '/inherent/departure', body) as Reply;
    return { d: isDeparture(r.departure) ? r.departure : null, reason: r.reason ?? null };
  };
  // An offer closes itself a minute after it first showed; the daemon stops serving it then too.
  useEffect(() => { if (!offer) return; const id = offer.id, timer = setTimeout(() => closeOffer(id), OFFER_MS); return () => clearTimeout(timer); }, [offer?.id]);
  useEffect(() => { if (view !== 'undo') return; const timer = setTimeout(close, UNDO_MS); return () => clearTimeout(timer); }, [view]);
  useEffect(() => { if (view === 'pin' && !departure) close(); }, [departure?.id]);

  const pin = (index: number) => {
    if (!offer) return;
    const id = offer.id;
    closeOffer(id);
    void send({ action: 'pin', offer_id: id, index }).then(r => { if (r.d) setDeparture(r.d); }).catch(() => undefined);
  };
  const next = () => {
    void send({ action: 'next' }).then(r => {
      if (r.d && !r.reason) { setDeparture(r.d); setMsg(''); } else setMsg(t(['No later bus', '没有更晚的车了']));
    }).catch(() => close());
  };
  const cancel = () => { if (!departure) return; undo.current = departure; leaveDeparture(departure); setView('undo'); };
  const bringBack = () => {
    const gone = undo.current;
    close();
    if (gone) void send({ action: 'undo', pin_id: gone.id }).then(r => { if (r.d) setDeparture(r.d); }).catch(() => undefined);
  };

  const note: NotchNote | null = view === 'pin' && departure
    ? { key: 'trip:pin', onClose: close, card: <PinCard d={departure} lang={lang} msg={msg} onNext={next} onCancel={cancel} onClose={close}/> }
    : view === 'undo' ? { key: 'trip:undo', onClose: close, card: <UndoCard lang={lang} onUndo={bringBack}/> }
    : !view && offer && !blocked ? { key: `trip:offer:${offer.id}`, onClose: () => closeOffer(offer.id), card: <OfferCard offer={offer} lang={lang} onPin={pin} onClose={() => closeOffer(offer.id)}/> }
    : null;
  return {
    note,
    open: () => { openedAt.current = performance.now(); setMsg(''); setView('pin'); },
    // A press anywhere else puts the pin's card away, not the press that opened it.
    away: () => { if (view === 'pin' && performance.now() - openedAt.current > JUST_OPENED_MS) close(); },
  };
}

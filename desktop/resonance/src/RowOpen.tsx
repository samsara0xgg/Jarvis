import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { CaretRight, Check } from '@phosphor-icons/react';
import type { L } from './companionSettings';
import { stageLook } from './dayView';
import { talkLines, type Bar, type Night, type Open, type Part, type Rec, type Session, type Words } from './dayOpen';

// What an open row holds (ADR 0296, spec-r3 section 8): the parts dayOpen.ts made of it, drawn, and the buttons that work today. The row's
// own two lines stay as they were; this hangs under them from the title's column.
export const EASE = 'cubic-bezier(.16, 1, .3, 1)';
export const reduced = matchMedia('(prefers-reduced-motion: reduce)');

// What opens in place (a row's content, the input and the note at the pane's foot): its height grows from nothing in 260 ms with its words
// fading in after 60 ms; closing is the same backwards in 200 ms; and while it is open a change in what it holds (the talk's lines coming, a
// recap unfolding, a picture loading) is a 180 ms glide of the height, not a jump. Reduced motion: nothing moves, what appears fades in
// over 120 ms. The content is drawn only while it is open or closing (`render`), so a closed row costs nothing.
export function Unfold({ open, render, tick, onOpen, onSettle, className = 'dt-fold' }: { open: boolean; render: () => ReactNode; tick?: () => void; onOpen?: (grow: number) => void; onSettle?: () => void; className?: string }) {
  const [here, setHere] = useState(open);
  if (open && !here) setHere(true);
  const fold = useRef<HTMLDivElement>(null), body = useRef<HTMLDivElement>(null), run = useRef<Animation | null>(null), fade = useRef<Animation | null>(null), rest = useRef(0), raf = useRef(0);
  const live = useRef({ open, tick, onOpen, onSettle });
  live.current = { open, tick, onOpen, onSettle };
  // `quick`: the content changed while it was open (not an opening); the height glides to it and nothing else moves.
  const go = (quick: boolean) => {
    const f = fold.current, b = body.current;
    if (!f || !b) return;
    const up = live.current.open, running = !!run.current;
    const from = running ? f.offsetHeight : rest.current, alpha = running && fade.current ? Number(getComputedStyle(b).opacity) || 0 : up ? 0 : 1;
    run.current?.cancel(); fade.current?.cancel(); run.current = fade.current = null;
    const to = up ? b.offsetHeight : 0;
    rest.current = to;
    if (reduced.matches) { if (!up) setHere(false); else { if (!quick) b.animate([{ opacity: 0 }, {}], { duration: 120 }); live.current.onSettle?.(); } return; }
    if (Math.abs(from - to) < 1) { if (!up) setHere(false); return; }
    const a = f.animate([{ height: `${from}px` }, { height: `${to}px` }], { duration: up ? quick ? 180 : 260 : 200, easing: EASE, fill: up ? 'none' : 'forwards' });
    run.current = a;
    if (up && !quick) fade.current = b.animate([{ opacity: alpha }, { opacity: 1 }], { duration: 200, delay: from === 0 ? 60 : 0, easing: 'ease-out', fill: 'backwards' });
    else if (!up) fade.current = b.animate([{ opacity: alpha }, { opacity: 0 }], { duration: 120, easing: 'ease-in', fill: 'forwards' });
    a.onfinish = () => { if (run.current !== a) return; run.current = null; if (!live.current.open) setHere(false); else live.current.onSettle?.(); live.current.tick?.(); };
    const loop = () => { live.current.tick?.(); if (run.current) raf.current = requestAnimationFrame(loop); };
    cancelAnimationFrame(raf.current); raf.current = requestAnimationFrame(loop);
    if (up && !quick) live.current.onOpen?.(to - from);
  };
  useLayoutEffect(() => go(false), [open, here]);
  useEffect(() => {
    const b = body.current;
    if (!b || !here) return;
    const watch = new ResizeObserver(() => { if (live.current.open && Math.abs(b.offsetHeight - rest.current) >= 1) go(true); });
    watch.observe(b);
    return () => watch.disconnect();
  }, [here]);
  useEffect(() => () => { run.current?.cancel(); fade.current?.cancel(); cancelAnimationFrame(raf.current); }, []);
  return here ? <div ref={fold} className={className}><div ref={body} className="dt-body">{render()}</div></div> : null;
}

// What the buttons of an open row do, and what they are doing: DayTimeline.tsx owns the work (the routes, the input) and fills this in.
// `null` where the row has no such button; a state where it has.
export type Acts = {
  port: string | null; t: (l: L) => string;
  records: (day: string) => Promise<Rec[]>;
  picture: (id: string, note: string) => void;
  ask: (() => void) | null;
  rename: { state: 'idle' | 'editing' | 'failed'; start: () => void } | null;
  // Naming a place that has no name: the field (what is typed, the kind of place picked) is there from `start` until the name is kept or `cancel`.
  name: { state: 'idle' | 'editing' | 'busy' | 'failed'; draft: string; role: string | null; start: () => void; set: (text: string) => void; pick: (role: string | null) => void; save: () => void; field: (on: boolean) => void } | null;
  ack: { state: 'idle' | 'busy' | 'failed'; press: () => void } | null;
  commit: { state: 'idle' | 'busy' | 'failed'; confirm: () => void; cancel: () => void } | null;
  summarize: { state: 'idle' | 'busy' | 'sent' | 'failed'; press: () => void } | null;
  star: ReactNode;
  line: string;
};

function Line({ w }: { w: Words }) { return <p className={w.tone === 'ink' ? 'is-ink' : undefined}>{w.text}</p>; }

// His first line in the talk and her first reply to it, one line each (clipped); read from the day's records when the row opens. While they
// come two bars of the same height are there, so what arrives does not push the rows under it.
function Talk({ part, acts }: { part: Extract<Part, { type: 'talk' }>; acts: Acts }) {
  const { t } = acts, [got, setGot] = useState<{ you: string; her: string } | null | 'failed' | undefined>(undefined);
  useEffect(() => {
    let live = true;
    setGot(undefined);
    acts.records(part.day).then(list => { if (live) setGot(talkLines(list, part.from, part.to)); }, () => { if (live) setGot('failed'); });
    return () => { live = false; };
  }, [part.day, part.from, part.to]);
  if (got === undefined) return <div className="dt-said is-wait" aria-hidden="true"><i/><i/></div>;
  if (!got || got === 'failed') return <p>{t(["The words of this talk haven't been read", '这段对话的字还没读到'])}</p>;
  return <div className="dt-said"><p><small>{t(['You', '你'])}</small><span>{got.you}</span></p>{got.her && <p><small>{t(['Her', '她'])}</small><span>{got.her}</span></p>}</div>;
}

// The picture at 240 tall at most, with the note in full under it; a click opens it large. One the daemon no longer has says so.
function Picture({ part, acts }: { part: Extract<Part, { type: 'picture' }>; acts: Acts }) {
  const { t } = acts, [state, setState] = useState<'wait' | 'ready' | 'gone'>('wait');
  return <>
    {part.id && acts.port && (state === 'gone'
      ? <p>{t(["The picture isn't there any more", '这张图已经没有了'])}</p>
      : <button type="button" className={`dt-pic${state === 'wait' ? ' is-wait' : ''}`} aria-label={t(['Open the picture', '看大图'])} onClick={() => acts.picture(part.id, part.note)}>
        <img alt="" decoding="async" src={`http://127.0.0.1:${acts.port}/inherent/attachments/${part.id}`} onLoad={() => setState('ready')} onError={() => setState('gone')}/>
      </button>)}
    {part.show && <p className="is-note">{part.note}</p>}
  </>;
}

// The biggest of what the block was spent on: a name, a thin bar for its share of the active time, its length.
export function Bars({ heading, bars }: { heading: string; bars: Bar[] }) {
  return <div className="dt-bars" role="list" aria-label={heading}>
    <h4>{heading}</h4>
    {bars.map(b => <div key={b.name} className="dt-bar-row" role="listitem"><span>{b.name}</span><i><u style={{ width: `${Math.max(2, b.share * 100)}%` }}/></i><em>{b.text}</em></div>)}
  </div>;
}

// The night as the watch told it: four lanes (awake on top, then REM, core, deep at the foot), a block where each stage was on the row's own
// time scale, and where one stage gives way to another a hairline between the lanes. What the phone only said "in bed" for is not drawn;
// a stretch no stage was reported for sits in the core lane, paler.
const LANE: Record<string, number> = { awake: 0, rem: 1, core: 2, unspecified: 2, deep: 3 };
export function Hypnogram({ night, t }: { night: Night; t: (l: L) => string }) {
  const span = Math.max(1, night.to - night.from), drawn = night.slices.filter(x => x.stage in LANE), at = (ms: number) => `${((ms - night.from) / span) * 100}%`;
  const names = [t(['Awake', '清醒']), 'REM', t(['Core', '核心']), t(['Deep', '深睡'])];
  return <div className="dt-hyp" role="img" aria-label={t([`Sleep stages, ${night.at[0]} to ${night.at[1]}`, `睡眠分期，${night.at[0]} 到 ${night.at[1]}`])}>
    <div className="dt-hyp-l" aria-hidden="true">{names.map(n => <span key={n}>{n}</span>)}</div>
    <div className="dt-hyp-plot" aria-hidden="true">
      {drawn.slice(1).map((x, i) => { const a = LANE[drawn[i].stage], b = LANE[x.stage]; return a === b ? null : <u key={`j${i}`} style={{ left: at(x.s), top: Math.min(a, b) * 12 + 6, height: Math.abs(a - b) * 12 }}/>; })}
      {drawn.map((x, i) => { const look = stageLook(x.stage); return <i key={i} style={{ left: at(x.s), width: `max(2px, ${((x.e - x.s) / span) * 100}%)`, top: LANE[x.stage] * 12 + 2, '--c': look.rgb.join(' '), '--a': Math.max(.55, look.a) } as CSSProperties}/>; })}
    </div>
    <div className="dt-hyp-ends" aria-hidden="true"><em>{night.at[0]}</em><em>{night.at[1]}</em></div>
  </div>;
}

// The Claude Code sessions of a row: when each began, its title, project · account and how long; a session with a recap opens to it, in full
// (one alone is already open).
function Sessions({ list, t }: { list: Session[]; t: (l: L) => string }) {
  const [one, setOne] = useState<string | null>(list.length === 1 ? list[0].id : null);
  return <ul className="dt-sess">{list.map(s => {
    const shown = one === s.id, fold = !!s.recap && list.length > 1, head = <><time>{s.at}</time><span className="dt-sess-t"><b>{s.title}</b>{s.meta && <em>{s.meta}</em>}</span><span className="dt-sess-l">{s.length}</span></>;
    return <li key={s.id} className={shown ? 'is-open' : undefined}>
      {fold ? <button type="button" aria-expanded={shown} onClick={() => setOne(shown ? null : s.id)}>{head}<CaretRight size={10} weight="bold" aria-hidden="true"/></button> : <div className="dt-sess-h">{head}</div>}
      {shown && s.recap ? <p className="dt-recap">{s.recap}</p> : null}
      {list.length === 1 && !s.recap && <p>{t(['No recap was written', '没有写小结'])}</p>}
    </li>;
  })}</ul>;
}

function PartView({ part, acts }: { part: Part; acts: Acts }) {
  switch (part.type) {
    case 'lines': return <div className="dt-lines">{part.lines.map((w, i) => <Line key={i} w={w}/>)}</div>;
    case 'talk': return <Talk part={part} acts={acts}/>;
    case 'picture': return <Picture part={part} acts={acts}/>;
    case 'bars': return <Bars heading={part.heading} bars={part.bars}/>;
    case 'night': return <Hypnogram night={part.night} t={acts.t}/>;
    case 'sessions': return <Sessions list={part.list} t={acts.t}/>;
    case 'titles': return <ul className="dt-titles">{part.list.map((x, i) => <li key={i}>{x}</li>)}</ul>;
  }
}

// The kinds of place the brain keeps (jarvis/state/places.py ROLES); none picked is `other` there.
const ROLES: [string, L][] = [['home', ['Home', '家']], ['school', ['School', '学校']], ['other', ['Other', '其他']]];
// A name for a place that has none, in the open row: the cursor is in the field, the kind of place is picked from the chips under it (picking one again
// puts it down), Enter keeps it and Esc lets go (DayTimeline.tsx hears the key). The chips take no focus, so the cursor stays in the field.
function NameField({ name, t }: { name: NonNullable<Acts['name']>; t: (l: L) => string }) {
  const box = useRef<HTMLInputElement>(null);
  useEffect(() => { box.current?.focus({ preventScroll: true }); }, []);
  return <div className="dt-name">
    <input ref={box} className="dt-name-in" type="text" autoComplete="off" spellCheck={false} maxLength={40} aria-label={t(['A name for this place', '给这个地方起个名字'])} placeholder={t(['A name for this place', '给这个地方起个名字'])}
      value={name.draft} readOnly={name.state === 'busy'} onChange={e => name.set(e.target.value)} onFocus={() => name.field(true)} onBlur={() => name.field(false)}
      onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); name.save(); } }}/>
    <div className="dt-roles" role="group" aria-label={t(['What kind of place', '是什么地方'])}>
      {ROLES.map(([role, label]) => <button key={role} type="button" className={`dt-role${name.role === role ? ' is-on' : ''}`} aria-pressed={name.role === role} onMouseDown={e => e.preventDefault()} onClick={() => name.pick(name.role === role ? null : role)}>{t(label)}</button>)}
    </div>
  </div>;
}

// The buttons: what can be done from the row, only where the route to do it works today; under them a line when there is something to say.
function Buttons({ acts, say }: { acts: Acts; say: string }) {
  const { t, rename, name, ack, commit, summarize, star } = acts, any = !!(acts.ask || rename?.state === 'idle' || name?.state === 'idle' || ack || commit || summarize || star);
  return <>
    {any && <div className="dt-acts">
      {rename?.state === 'idle' && <button type="button" className="dt-btn" onClick={rename.start}>{t(['Rename', '改名字'])}</button>}
      {name?.state === 'idle' && <button type="button" className="dt-btn" onClick={name.start}>{t(['Name this place', '起个名字'])}</button>}
      {ack && <button type="button" className="dt-btn is-amber" disabled={ack.state === 'busy'} onClick={ack.press}>{t(['Got it', '知道了'])}</button>}
      {commit && <>
        <button type="button" className="dt-btn is-amber" disabled={commit.state === 'busy'} onClick={commit.confirm}>{t(['Mine', '是我的'])}</button>
        <button type="button" className="dt-btn" disabled={commit.state === 'busy'} onClick={commit.cancel}>{t(['Not mine', '不是'])}</button>
      </>}
      {summarize && (summarize.state === 'sent'
        ? <span className="dt-sent"><Check size={12} weight="bold" aria-hidden="true"/>{t(['Sent to her', '已发给她'])}</span>
        : <button type="button" className="dt-btn" disabled={summarize.state === 'busy'} onClick={summarize.press}>{summarize.state === 'failed' ? t(["Didn't send. Try again", '没发出去，再试一次']) : t(['Have her sum this up', '让她总结这段'])}</button>)}
      {star}
      {acts.ask && <button type="button" className="dt-btn" onClick={acts.ask}>{t(['Ask her', '问她'])}</button>}
    </div>}
    {name && name.state !== 'idle' && <NameField name={name} t={t}/>}
    {say && <p className="dt-hint" role="status">{say}</p>}
  </>;
}

export function OpenBody({ open, acts }: { open: Open; acts: Acts }) {
  const { t } = acts;
  const say = acts.rename?.state === 'editing' || acts.name?.state === 'editing' ? t(['Enter saves · Esc cancels', '回车保存 · Esc 取消']) : acts.name?.state === 'failed' ? t(["Couldn't keep it. Try again.", '没记下，再试一次']) : acts.rename?.state === 'failed' ? t(["Couldn't rename it. Try again.", '没改成，再试一次']) : acts.ack?.state === 'failed' || acts.commit?.state === 'failed' ? t(["That didn't go through. Try again.", '没成功，再试一次']) : acts.line;
  return <>
    {open.parts.map((p, i) => <PartView key={`${p.type}${i}`} part={p} acts={acts}/>)}
    <Buttons acts={acts} say={say}/>
  </>;
}

// The picture large over the window: a click anywhere or Esc closes it (DayTimeline.tsx hears the key). It hangs from the body, outside
// the notch, so it is its own surface and takes the pointer where it is (`data-hit`).
export function Viewer({ port, id, note, t, onClose }: { port: string; id: string; note: string; t: (l: L) => string; onClose: () => void }) {
  return createPortal(<div className="dt-viewer" data-hit role="dialog" aria-modal="true" aria-label={t(['The picture', '图片'])} onClick={onClose}>
    <img alt="" src={`http://127.0.0.1:${port}/inherent/attachments/${id}`}/>
    {note && <p>{note}</p>}
  </div>, document.body);
}

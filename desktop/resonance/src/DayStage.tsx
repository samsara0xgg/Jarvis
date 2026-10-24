import { useLayoutEffect, useRef, type CSSProperties, type KeyboardEvent, type MouseEvent } from 'react';
import { requestWords, type Agent } from './agents';
import { Ic } from './dayIcons';
import { openOf } from './dayOpen';
import { clock, dur, type Block, type DayModel, type DayItem, type Icon, type Rgb, type Row, type Say, type Segment } from './dayView';
import './day-stage.css';

// The stage of the V2 pane (the right column, ~340 px) while nothing in the list is selected: what is going on now, what waits on him, what comes next.
// Everything here is read from the day model as it is: the stretch he is in (or the thing, or the place), the rows that wait on him with the buttons
// dayOpen.ts already offers for them (the handlers are DayTimeline's: they are passed in `acts`), and the next few rows that are still to come.
// A card is a click away from its row in the list (`onPick`). Motion: each card fades in over --motion-fast when it first shows, nothing moves.
const rgb = (c: readonly number[]) => c.join(' ');
// What can come and go inside a stretch, by the kind of item: the colour of its part of the strip (a part of any other kind takes its stretch's own).
const PART: Record<string, Rgb> = { work: [91, 140, 255], code: [91, 140, 255], call: [111, 168, 200], phone_call: [111, 168, 200], workout: [76, 195, 138], event: [155, 123, 255], media: [233, 121, 166], messages: [233, 121, 166] };
// A moment has no length to show in a strip.
const MOMENT = new Set(['reminder', 'picture', 'departure', 'payment', 'claude_day']);
const MINUTE = 60_000, GAP = 5 * MINUTE, LABELS = 3, WIDE = .12, NEXT = 3;

// What the stage can do for a card that waits on him: the handlers are DayTimeline's own (`acknowledge`, `settle`), `state` is its `work` table by the same
// keys (`ack:<id>`, `commit:<row key>`), so a button that is busy is shut and a failure is said.
export type StageActs = {
  ack?: (id: string) => void;
  commit?: (key: string, hex: string, action: 'confirm' | 'cancel') => void;
  state?: (work: string) => 'busy' | 'failed' | '' | undefined;
};
// What the brain has said of how he spent the current stretch. Nothing serves it yet (docs/Gaps): the line shows only when it is given.
export type Drift = { minutes?: number; switches?: number };
export type DayStageProps = {
  model: DayModel;
  now: number;
  t: Say;
  onPick: (key: string) => void;
  sessions?: { agents: Agent[] };
  acts?: StageActs;
  onAgent?: (id: string) => void;
  drift?: Drift | null;
};

type Look = { key: string; icon: Icon; rgb: Rgb | null; unknown: boolean; title: string; gist: string; length: string; seg: Segment | null };
// What he is doing now: the stretch that has him in it; else the lasting thing that has begun and is not over (a way he is going, a call, an event); else the place he is at.
function nowOf(m: DayModel, now: number): Look | null {
  if (!m.today) return null;
  let seg: Segment | undefined;
  for (const s of m.segs.values()) if (s.start <= now && (s.span.end_ms == null || s.span.end_ms > now) && (!seg || s.start >= seg.start)) seg = s;
  const srow = seg && m.rows.get(seg.span.id);
  if (seg && srow) return { key: srow.key, icon: seg.icon, rgb: seg.rgb, unknown: seg.unknown, title: srow.title, gist: seg.detail || srow.detail, length: srow.right, seg };
  let going: DayItem | undefined;
  for (const it of m.items) {
    if (it.kind === 'stay' || MOMENT.has(it.kind) || it.start_ms > now || (it.end_ms == null ? it.state === 'next' : it.end_ms <= now) || !m.rows.has(it.id)) continue;
    if (!going || it.start_ms >= going.start_ms) going = it;
  }
  const grow = going && m.rows.get(going.id);
  if (going && grow) return { key: grow.key, icon: grow.icon === 'dot' ? 'pin' : grow.icon, rgb: null, unknown: false, title: grow.title, gist: grow.detail, length: grow.right, seg: null };
  let stay: DayItem | undefined;
  for (const it of m.items) if (it.kind === 'stay' && m.heads.has(it.id) && it.start_ms <= now && (it.end_ms == null || it.end_ms > now) && (!stay || it.start_ms >= stay.start_ms)) stay = it;
  const head = stay && m.heads.get(stay.id);
  return head ? { key: head.key, icon: head.icon, rgb: head.rgb, unknown: false, title: head.name, gist: head.when, length: head.dur, seg: null } : null;
}

// The stretch cut into the parts that were in it, in time order, each as long as it lasted; what no part covers (five minutes or more) is a hollow one.
type Part = { key: string; ms: number; rgb: Rgb | null; label: string };
export function partsOf(m: DayModel, seg: Segment, now: number): Part[] {
  const byId = new Map(m.items.map(it => [it.id, it])), found: { key: string; s: number; e: number; kind: string; label: string }[] = [];
  for (const x of seg.inside) {
    const it = byId.get(x.key), all = (x.ids ?? [x.key]).flatMap(id => byId.get(id) ?? []);
    if (!it || MOMENT.has(it.kind)) continue;
    // A line of several sessions or bursts is one part, from the first to the last.
    const s = Math.max(Math.min(...all.map(y => y.start_ms)), seg.start), e = Math.min(Math.max(...all.map(y => y.end_ms ?? now)), seg.end);
    if (e - s >= MINUTE) found.push({ key: x.key, s, e, kind: it.kind, label: it.kind === 'work' ? x.detail.split(' · ')[0] || x.title : x.title });
  }
  found.sort((a, b) => a.s - b.s || a.e - b.e);
  const out: Part[] = [], hole = (a: number, b: number) => { if (b - a >= GAP) out.push({ key: `gap${a}`, ms: b - a, rgb: null, label: '' }); };
  let at = seg.start;
  for (const f of found) {
    const s = Math.max(f.s, at);
    if (f.e - s < MINUTE) continue;
    hole(at, s);
    out.push({ key: f.key, ms: f.e - s, rgb: PART[f.kind] ?? seg.rgb ?? [196, 204, 238], label: f.label });
    at = f.e;
  }
  hole(at, seg.end);
  return out.length > 1 ? out : [];
}

// A card that goes somewhere: a button in all but the tag, since it holds buttons of its own.
const press = (go?: () => void) => go ? {
  role: 'button' as const, tabIndex: 0, onClick: go,
  onKeyDown: (e: KeyboardEvent) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); go(); } },
} : {};
const quiet = (e: MouseEvent) => e.stopPropagation();
// How long until it, as the list says it (a departure keeps its own words).
function until(row: Row, m: DayModel, now: number, t: Say) {
  const at = m.span.get(row.key);
  if (row.value || !at) return row.right;
  const left = dur(Math.max(1, Math.ceil((at.t0 - now) / MINUTE)), t);
  return t([`in ${left}`, `还有 ${left}`]);
}
const glyph = (icon: Icon | 'dot', size = 12) => icon === 'dot' ? <span className="ds-pip"/> : <Ic icon={icon} size={size}/>;
const futures = (blocks: Block[], out: Row[] = []): Row[] => { for (const b of blocks) { if (out.length >= NEXT) break; if (b.type === 'row') { if (b.row.future && !b.row.wait && !b.row.leaving) out.push(b.row); } else if (b.type === 'stay') futures(b.items, out); } return out; };

function Mini({ at, icon, title, gist, right }: { at: string; icon: Icon | 'dot'; title: string; gist: string; right: string }) {
  return <div className="ds-mini"><time>{at}</time><i>{glyph(icon)}</i><span><b>{title}</b>{gist && <small>{gist}</small>}</span>{right && <em>{right}</em>}</div>;
}

function NowCard({ look, m, now, t, drift, onPick }: { look: Look; m: DayModel; now: number; t: Say; drift?: Drift | null; onPick: () => void }) {
  const parts = look.seg ? partsOf(m, look.seg, now) : [], total = parts.reduce((n, p) => n + p.ms, 0);
  // The three longest parts that are wide enough to hold a word say what they are.
  const named = new Set([...parts].filter(p => p.label && p.ms / total > WIDE).sort((a, b) => b.ms - a.ms).slice(0, LABELS).map(p => p.key));
  const minutes = (p: Part) => Math.round(p.ms / MINUTE);
  const flex = (p: Part) => ({ flex: `${p.ms} 1 0`, '--k': p.rgb ? rgb(p.rgb) : undefined }) as CSSProperties;
  const lost = drift ? [drift.minutes ? t([`Distracted ${dur(drift.minutes, t)}`, `分心 ${dur(drift.minutes, t)}`]) : '', drift.switches ? t([`Switched away ${drift.switches}×`, `切走 ${drift.switches} 次`]) : ''].filter(Boolean).join(' · ') : '';
  return <div className="ds-card is-tap" data-stage="now" data-key={look.key} {...press(onPick)}>
    <div className={`ds-now${look.unknown ? ' is-unknown' : ''}`} style={{ '--c': rgb(look.rgb ?? [196, 204, 238]) } as CSSProperties}>
      <i className="ds-ic">{glyph(look.icon, 13)}</i>
      <span className="ds-tx"><b>{look.title}</b>{look.gist && <em>{look.gist}</em>}</span>
      {look.length && <span className="ds-len">{look.length}</span>}
    </div>
    {parts.length > 0 && <>
      <div className="ds-strip" role="img" aria-label={parts.filter(p => named.has(p.key)).map(p => `${p.label} ${minutes(p)}`).join(', ')}>{parts.map(p => <i key={p.key} className={p.rgb ? undefined : 'is-away'} style={flex(p)}/>)}</div>
      <div className="ds-strip-l" aria-hidden="true">{parts.map(p => <span key={p.key} style={{ flex: `${p.ms} 1 0` }}>{named.has(p.key) ? `${p.label} ${minutes(p)}` : ''}</span>)}</div>
    </>}
    {lost && <p className="ds-note">{lost}</p>}
  </div>;
}

function WaitCard({ row, m, now, t, acts, onPick }: { row: Row; m: DayModel; now: number; t: Say; acts?: StageActs; onPick: () => void }) {
  const open = openOf(m, row.key, t), ack = open?.ack && acts?.ack ? open.ack : null, commit = open?.commit && acts?.commit ? open.commit : null;
  const as = ack ? acts!.state?.(`ack:${ack}`) : commit ? acts!.state?.(`commit:${row.key}`) : '';
  const go = (fn: () => void) => (e: MouseEvent) => { quiet(e); fn(); };
  return <div className="ds-card is-wait" data-stage="wait" data-key={row.key} {...press(onPick)}>
    <Mini at={row.at} icon={row.icon} title={row.title} gist={row.detail} right={row.future ? until(row, m, now, t) : row.right}/>
    {(ack || commit) && <div className="ds-acts">
      {ack && <button type="button" className="ds-btn is-yes" disabled={as === 'busy'} onClick={go(() => acts!.ack!(ack))}>{t(['Got it', '知道了'])}</button>}
      {commit && <>
        <button type="button" className="ds-btn is-yes" disabled={as === 'busy'} onClick={go(() => acts!.commit!(row.key, commit, 'confirm'))}>{t(['Mine', '是我的'])}</button>
        <button type="button" className="ds-btn" disabled={as === 'busy'} onClick={go(() => acts!.commit!(row.key, commit, 'cancel'))}>{t(['Not mine', '不是'])}</button>
      </>}
    </div>}
    {as === 'failed' && <p className="ds-hint" role="status">{t(["That didn't go through. Try again.", '没成功，再试一次'])}</p>}
  </div>;
}

// A session that has stopped to ask him: what it asks (in words, when it holds a request), where, and how long ago.
function AgentCard({ agent, t, onAgent }: { agent: Agent; t: Say; onAgent?: (id: string) => void }) {
  const ask = agent.request ? t(requestWords(agent.request)) : agent.last;
  return <div className="ds-card is-wait" data-stage="agent" data-key={agent.id} {...press(onAgent && (() => onAgent(agent.id)))}>
    <Mini at={agent.age} icon={agent.agent === 'claude' ? 'claude' : 'code'} title={agent.title} gist={[agent.project, ask].filter(Boolean).join(' · ')} right=""/>
  </div>;
}

export function DayStage({ model: m, now, t, onPick, sessions, acts, onAgent, drift }: DayStageProps) {
  const here = nowOf(m, now), waits = (m.today ? m.waiting : []).flatMap(k => m.rows.get(k) ?? []);
  const asking = (sessions?.agents ?? []).filter(a => a.state === 'wait' && !a.fromProject), next = m.today ? futures(m.blocks) : [], count = waits.length + asking.length;
  const box = useRef<HTMLDivElement>(null);
  // The edges fade over 16 px where there is more past them, as the list's do.
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const edges = () => { el.classList.toggle('more-above', el.scrollTop > 1); el.classList.toggle('more-below', el.scrollHeight - el.scrollTop - el.clientHeight > 1); };
    edges();
    el.addEventListener('scroll', edges, { passive: true });
    const ro = new ResizeObserver(edges);
    ro.observe(el);
    for (const c of el.children) ro.observe(c);
    return () => { el.removeEventListener('scroll', edges); ro.disconnect(); };
  });
  return <div ref={box} className="ds" data-stage="today">
    {!m.today
      ? <p className="ds-empty">{t(['This day is over', '这一天已经过去了'])}</p>
      : <>
        <div className="ds-sec"><b>{t(['Now', '现在'])}</b><em>{clock(now, m.tz)}</em></div>
        {here ? <NowCard key={here.key} look={here} m={m} now={now} t={t} drift={drift} onPick={() => onPick(here.key)}/> : <p className="ds-empty">{t(['Nothing going on', '现在没什么事'])}</p>}
        {count > 0 && <>
          <div className="ds-sec is-amber"><b>{t([`Waiting · ${count}`, `等你 · ${count}`])}</b></div>
          {waits.map(r => <WaitCard key={r.key} row={r} m={m} now={now} t={t} acts={acts} onPick={() => onPick(r.key)}/>)}
          {asking.map(a => <AgentCard key={a.id} agent={a} t={t} onAgent={onAgent}/>)}
        </>}
        {next.length > 0 && <>
          <div className="ds-sec"><b>{t(['Next', '接下来'])}</b></div>
          {next.map(r => <div key={r.key} className="ds-next" data-stage="next" data-key={r.key} {...press(() => onPick(r.key))}><Mini at={r.at} icon={r.icon} title={r.title} gist={r.detail} right={until(r, m, now, t)}/></div>)}
        </>}
      </>}
  </div>;
}

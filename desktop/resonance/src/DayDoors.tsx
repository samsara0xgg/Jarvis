import type { CSSProperties, MouseEvent, ReactNode } from 'react';
import { ArrowUpRight, CaretDown, CaretLeft, CaretUp } from '@phosphor-icons/react';
import type { L } from './companionSettings';
import { fOf } from './barView';
import { clock } from './dayView';
import { DOORS, type BarMark, type DoorKey, type DoorPage, type DoorRow } from './doors';
import './day-doors.css';

// What the day's pane draws for a door (DayTimeline.tsx owns the state): the row of doors, a page's numbers row, a row of a page, a row's detail page, and a page's marks on
// the day's bar (DayBar.tsx puts them over the dimmed day). A page's hook (src/doors/<key>.tsx) says what; none of it draws anything itself.
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
const AMBER = '255 201 143', PALE = '214 222 250';

// The doors at the pane's foot, one pill each, the door he is on lit; a count after the name where the door has one (amber when it waits on him). The last has no name: it is its dots.
export function Doors({ on, pages, pick, t }: { on: DoorKey; pages: Partial<Record<DoorKey, DoorPage | null>>; pick: (door: DoorKey) => void; t: (l: L) => string }) {
  return <nav className="dt-doors" aria-label={t(['Pages', '页面'])}>
    {DOORS.map(({ key, label, icon: Glyph, bare }) => {
      const page = pages[key], count = page?.count;
      return <button key={key} type="button" className={`dt-door${key === on ? ' is-on' : ''}${bare ? ' is-bare' : ''}`} data-door={key} aria-current={key === on ? 'page' : undefined} aria-label={bare ? t(label) : undefined} title={bare ? t(label) : undefined} onClick={() => pick(key)}>
        <Glyph size={12} weight={bare ? 'bold' : 'regular'} aria-hidden="true"/>{!bare && <span>{t(label)}</span>}{count && <em className={page?.countAmber ? 'is-amber' : undefined}>{count}</em>}</button>;
    })}
  </nav>;
}

// A page's numbers row, in the look of the day's chips: a chip that has `only` is a lens on the page (previewed under the pointer, pinned by a click), the others only say a number;
// the page's link, if it has one, is at the right end.
export function DoorNumbers({ page, pinned, preview, pin, t }: { page: DoorPage; pinned: string | null; preview: (id: string | null) => void; pin: (id: string | null) => void; t: (l: L) => string }) {
  return <div className="dt-numbers is-page" role="group" aria-label={t(['This page in numbers', '这一页的数字'])}>
    {page.chips.map(c => {
      const face = <>{c.icon}<span>{c.label}</span>{c.value && <em>{c.value}</em>}</>, style = { '--k': c.amber ? AMBER : PALE } as CSSProperties, cls = `dt-num${c.amber ? ' is-amber' : ''}`;
      return c.only
        ? <button key={c.id} type="button" className={`${cls}${pinned === c.id ? ' is-pinned' : ''}`} data-chip={c.id} aria-pressed={pinned === c.id} style={style} onPointerEnter={() => preview(c.id)} onPointerLeave={() => preview(null)} onClick={() => pin(pinned === c.id ? null : c.id)}>{face}</button>
        : <span key={c.id} className={`${cls} is-info`} data-chip={c.id} style={style}>{face}</span>;
    })}
    {page.link && <button type="button" className="dt-go" onClick={page.link.open}>{page.link.label}<ArrowUpRight size={11} aria-hidden="true"/></button>}
  </div>;
}

// A row of a page, with the day's row classes: the time, the circle, the words (a tag after the title, a second line), the buttons a rest on it offers, and the right column. `off`: a chip
// is lit that this row is not one of. A button marked `data-opens` in `quick` or `side` opens the row like a click on the row does; any other button is its own.
export function DoorRowView({ row, tz, hot, pick, off, onHot, onClick }: { row: DoorRow; tz: string; hot: boolean; pick: boolean; off: boolean; onHot: (key: string | null) => void; onClick: (e: MouseEvent, key: string) => void }) {
  const time = row.time ?? (row.at === null ? '' : clock(row.at, tz));
  return <div className={`dt-row has-detail${row.tone === 'read' || row.tone === 'future' ? ` is-${row.tone}` : ''}${off ? ' is-off' : ''}${hot ? ' is-hot' : ''}${pick ? ' is-pick' : ''}${row.opens ? '' : ' is-still'}`} data-key={row.key} aria-current={pick ? 'true' : undefined}
    onPointerMove={() => onHot(row.key)} onPointerLeave={() => onHot(null)} onClick={e => onClick(e, row.key)}>
    <time className="dt-at">{time}</time>
    <i className={`dt-ic${row.who ? ' is-who' : ''}${row.tone === 'live' ? ' is-on' : ''}`}>{row.icon}</i>
    <span className="dt-tx"><span className="dt-ttl"><b>{row.title}</b>{row.tag && <i className={row.tag.amber ? 'dt-wait' : 'dt-tag'}>{row.tag.text}</i>}</span>{row.sub && <em>{row.sub}</em>}</span>
    {row.quick && <span className="dt-quick">{row.quick}</span>}
    <span className="dt-tm">{row.side}</span>
  </div>;
}

// A row's detail page: the same head as a day row's (a back control, the title, the row before and after), then what the page's hook made of the row.
export function DoorDetail({ title, body, back, prev, next, t }: { title: string; body: ReactNode; back: () => void; prev: (() => void) | null; next: (() => void) | null; t: (l: L) => string }) {
  return <div className="dt-pin is-door">
    <header className="dt-ph">
      <button type="button" className="dt-pb" aria-label={t(['Back', '返回'])} onClick={back}><CaretLeft size={14} weight="bold" aria-hidden="true"/></button>
      <b>{title}</b>
      <span className="dt-pn">
        <button type="button" aria-label={t(['Previous', '上一个'])} disabled={!prev} onClick={prev ?? undefined}><CaretUp size={14} weight="bold" aria-hidden="true"/></button>
        <button type="button" aria-label={t(['Next', '下一个'])} disabled={!next} onClick={next ?? undefined}><CaretDown size={14} weight="bold" aria-hidden="true"/></button>
      </span>
    </header>
    <div className="dt-body">{body}</div>
  </div>;
}

// ---- the marks, on the bar ----
type Axis = { t0: number; t1: number };
// A point is 3 px wide over the track's whole height; a span is a 6 px bar in one of three lanes; a level is the area under a line over the track, and the last value carried on dashed to `until`.
const LANE_TOP = [2, 9, 16];
function Level({ m, bar, className }: { m: Extract<BarMark, { kind: 'level' }>; bar: Axis; className: string }) {
  const pts = [...m.points].sort((a, b) => a.at - b.at);
  if (!pts.length) return null;
  const W = 1000, H = 24, x = (ms: number) => (clamp(fOf(bar, ms), 0, 1) * W).toFixed(1), y = (v: number) => (1 + (H - 1) * (1 - clamp(v, 0, 1))).toFixed(1), last = pts[pts.length - 1];
  const line = pts.map(p => `${x(p.at)},${y(p.value)}`).join(' ');
  return <svg className={`dt-mk-lv is-${m.tone}${className}`} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
    <polygon className="dt-lv-fill" points={`${x(pts[0].at)},${H} ${line} ${x(last.at)},${H}`}/>
    <polyline className="dt-lv-top" points={line} vectorEffect="non-scaling-stroke"/>
    {m.until !== undefined && m.until > last.at && <line className="dt-lv-run" x1={x(last.at)} y1={y(last.value)} x2={x(m.until)} y2={y(last.value)} vectorEffect="non-scaling-stroke"/>}
  </svg>;
}
// `mute`: the rows a lit chip keeps; the marks of the others (a mark with no row is never muted) are drawn at .25.
export function DoorMarks({ marks, bar, mute }: { marks: BarMark[]; bar: Axis; mute: ReadonlySet<string> | null }) {
  return <>{marks.map((m, i) => {
    const k = `${m.kind}${i}`, off = mute && m.key !== undefined && !mute.has(m.key) ? ' is-muted' : '';
    if (m.kind === 'point') { const f = fOf(bar, m.at); return f < 0 || f > 1 ? null : <i key={k} className={`dt-mk is-point is-${m.tone}${off}`} data-mark={m.key} style={{ left: `${f * 100}%` }}/>; }
    if (m.kind === 'span') {
      if (m.to < bar.t0 || m.from > bar.t1) return null;
      const f0 = clamp(fOf(bar, m.from), 0, 1), f1 = clamp(fOf(bar, m.to), 0, 1);
      return <i key={k} className={`dt-mk is-span is-${m.tone}${off}`} data-mark={m.key} style={{ left: `${f0 * 100}%`, width: `max(2px, ${(f1 - f0) * 100}%)`, top: LANE_TOP[m.lane] }}/>;
    }
    return <Level key={k} m={m} bar={bar} className={off}/>;
  })}</>;
}

// Where the marks of one row are on the day: from the start of its first to the end of its last (a `point` when it is one moment). A row with no mark has none.
export type Extent = { t0: number; t1: number; point: boolean };
export function extentOf(marks: BarMark[], key: string): Extent | null {
  let t0 = Infinity, t1 = -Infinity, span = false;
  for (const m of marks) {
    if (m.key !== key) continue;
    if (m.kind === 'point') { t0 = Math.min(t0, m.at); t1 = Math.max(t1, m.at); }
    else if (m.kind === 'span') { t0 = Math.min(t0, m.from); t1 = Math.max(t1, m.to); span = true; }
    else if (m.points.length) { const at = m.points.map(p => p.at); t0 = Math.min(t0, ...at); t1 = Math.max(t1, m.until ?? -Infinity, ...at); span = true; }
  }
  return t0 <= t1 ? { t0, t1, point: !span && t0 === t1 } : null;
}
// Whether a row is in a range of the day (the cut): by where its marks are (a span by its overlap, as the day's rows are), else by its time; a row with neither is not.
export function inRange(marks: BarMark[], row: DoorRow, a: number, b: number): boolean {
  const e = extentOf(marks, row.key) ?? (row.at === null ? null : { t0: row.at, t1: row.at, point: true });
  return !!e && (e.t0 === e.t1 ? e.t0 >= a && e.t0 <= b : e.t0 < b && e.t1 > a);
}
// The row whose mark is nearest the pointer, if one is within 8 px of it; between marks that are as near, the one nearer the pointer's height (the lanes).
export const NEAR_PX = 8;
export function nearestMark(marks: BarMark[], bar: Axis, width: number, x: number, y: number): string | null {
  let best: string | null = null, score = Infinity;
  for (const m of marks) {
    if (m.key === undefined) continue;
    const e = extentOf([m], m.key);
    if (!e) continue;
    const a = fOf(bar, e.t0) * width, b = m.kind === 'span' ? Math.max(fOf(bar, e.t1) * width, a + 2) : fOf(bar, e.t1) * width, dx = x < a ? a - x : x > b ? x - b : 0;
    if (dx > NEAR_PX) continue;
    const s = dx + Math.abs(y - (m.kind === 'span' ? LANE_TOP[m.lane] + 3 : 12)) / 24;
    if (s < score) { score = s; best = m.key; }
  }
  return best;
}

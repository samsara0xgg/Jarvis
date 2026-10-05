import { X } from '@phosphor-icons/react';
import type { ReactNode } from 'react';
import { tr, type L, type Lang } from './companionSettings';
import { useNow } from './homeData';
import { AgentMark, type MarkLook, type MarkState } from './AgentMarks';
import './action-card.css';
import './night-card.css';

// ADR 0093: the night run as GET /inherent/night draws it. `night` is the run now; `last` the one that ended last.
// Its `watch` is what the run saw of the agent sessions: each one's state now (`st` as the Agents window has it;
// `busy` also for a background task still running), when that last changed, and its `trail` of working stretches.
export type NightPhase = 'starting' | 'dark' | 'glance';
export type NightSt = 'work' | 'pack' | 'wait' | 'done' | 'err';
export interface NightSession { id: string; agent: string; title: string; st: NightSt; busy: boolean; since_ms: number | null; what: string; changed_ms: number; trail: [number, number | null][] }
export interface NightWatch {
  seen: boolean; blind: boolean; blind_since_ms: number | null; lists: Record<string, boolean>; busy: number; quiet_ms: number | null; sessions: NightSession[];
  release_ms?: number | null; monitor_ms?: number | null; extra_ms?: number; busy_at_deadline?: number | null; busy_at_release?: number;
}
export interface NightRun {
  id: string; phase: NightPhase; started_ms: number; until_ms: number; cap_ms: number; wake_at_ms: number | null; dark_at_ms: number | null; stay: boolean;
  released_ms: number | null; guarded: boolean; watch: NightWatch;
}
export interface NightLast {
  id: string; started_ms: number; until_ms: number; released_ms: number | null; release_reason: string | null; ended_ms: number; reason: string; slept_ms: number | null;
  restored: { brightness: boolean; volume: boolean }; watch: NightWatch; totals: { nights: number; extra_ms: number; blind: number };
}
export interface NightState { night: NightRun | null; last: NightLast | null; hours: number; laptop: boolean }
export type NightAction = 'start' | 'dark' | 'stay' | 'end';
// The two looks Allen kept (2026-09-30): the list, and the star trail on a twelve-hour dial.
export type NightLook = 'list' | 'trail';
export const isNightLook = (value: unknown): value is NightLook => value === 'list' || value === 'trail';

// The last run's card stays up to half a day after it ended, until its × is pressed.
export const MORNING_MS = 12 * 3_600_000;
const SEEN = 'companion-night-seen-v1';
export const seenNight = () => { try { return localStorage.getItem(SEEN) ?? ''; } catch { return ''; } };
export const markNightSeen = (id: string) => { try { localStorage.setItem(SEEN, id); } catch { /* shown again after a reload */ } };
// A run that ended because the owner came back or said so; a cancelled one changed nothing and has nothing to tell.
export const morningOf = (s: NightState | null, now: number, seen: string) =>
  s?.last && !s.night && (s.last.reason === 'returned' || s.last.reason === 'ended') && now - s.last.ended_ms < MORNING_MS && s.last.id !== seen ? s.last : null;

const SETTLE_MIN = 3, MIN = 60_000;
const hhmm = (ms: number) => { const d = new Date(ms); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
const span = (ms: number, lang: Lang) => {
  const m = Math.max(1, Math.round(ms / MIN)), h = Math.floor(m / 60), r = m % 60;
  return lang === 'zh' ? h ? `${h} 小时${r ? ` ${r} 分` : ''}` : `${r} 分` : h ? `${h} h${r ? ` ${r} min` : ''}` : `${r} min`;
};
const clock = (ms: number) => { const m = Math.round(ms / MIN); return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}`; };

type Ctx = { lang: Lang; t: (l: L) => string; now: number; marks: MarkLook };
// What a row's mark shows: a session with a background task still running works whatever its turn does.
const markOf = (s: NightSession, gone = false): MarkState => gone ? 'seen' : s.busy ? s.st === 'pack' ? 'pack' : 'work' : s.st === 'pack' || s.st === 'work' ? 'done' : s.st;
function status(s: NightSession, { t, lang, now }: Ctx, bedtime: boolean): string {
  if (s.busy) return s.st === 'pack' ? t(['Compacting', '在压缩']) : s.st !== 'work' ? t(['Background task running', '后台任务在跑'])
    : s.since_ms ? t([`Working ${span(now - s.since_ms, lang)}`, `在干活 ${span(now - s.since_ms, lang)}`]) : t(['Working', '在干活']);
  if (s.st === 'wait') return bedtime ? t([`Needs you${s.what ? ` · ${s.what}` : ''}`, `等你${s.what ? ` · ${s.what}` : ''}`])
    : t([`Needs you since ${hhmm(s.since_ms ?? s.changed_ms)}`, `等你 · ${hhmm(s.since_ms ?? s.changed_ms)} 起`]);
  if (s.st === 'err') return t([`Error at ${hhmm(s.changed_ms)}`, `出错了 ${hhmm(s.changed_ms)}`]);
  return t([`Done at ${hhmm(s.changed_ms)}`, `做完了 ${hhmm(s.changed_ms)}`]);
}

function Row({ s, c, bedtime, go, gone, said }: { s: NightSession; c: Ctx; bedtime: boolean; go?: (s: NightSession) => void; gone?: boolean; said?: string }) {
  const st = markOf(s, gone);
  return <div className={`nc-row is-${st}`} data-session={s.id}>
    <AgentMark look={c.marks} state={st} id={s.id} size={12} still/>
    <b title={s.title}>{s.title || c.t(['Untitled', '未命名'])}</b>
    <span>{said ?? status(s, c, bedtime)}</span>
    {go && <button type="button" className="nc-go" onClick={() => go(s)}>{c.t(['Answer', '去回答'])}</button>}
  </div>;
}
function Section({ label, tone, hint, children, n }: { label: string; tone?: 'warm'; hint?: string; n: number; children: ReactNode }) {
  return <div className="nc-sec">
    <div className={`nc-head${tone ? ` is-${tone}` : ''}`}><span>{label}<em>{n}</em></span>{hint && <span className="nc-hint">{hint}</span>}</div>
    {children}
  </div>;
}
// The sessions in three groups: working, needing you (waiting or stopped on an error), done.
const groups = (w: NightWatch) => ({
  busy: w.sessions.filter(s => s.busy),
  due: w.sessions.filter(s => !s.busy && (s.st === 'wait' || s.st === 'err')),
  done: w.sessions.filter(s => !s.busy && s.st !== 'wait' && s.st !== 'err').sort((a, b) => b.changed_ms - a.changed_ms),
});
function Listing({ w, c, bedtime, go, hints }: { w: NightWatch; c: Ctx; bedtime: boolean; go?: (s: NightSession) => void; hints: { busy?: string; due?: string } }) {
  const { busy, due, done } = groups(w), t = c.t;
  if (!busy.length && !due.length && !done.length) return null;
  return <div className="nc-list">
    {busy.length > 0 && <Section label={bedtime ? t(['Watching', '盯着']) : t(['Working', '在干活'])} n={busy.length} hint={hints.busy}>{busy.map(s => <Row key={s.id} s={s} c={c} bedtime={bedtime}/>)}</Section>}
    {due.length > 0 && <Section label={t(['Your turn', '轮到你'])} tone="warm" n={due.length} hint={hints.due}>{due.map(s => <Row key={s.id} s={s} c={c} bedtime={bedtime} go={go && s.st === 'wait' ? go : undefined}/>)}</Section>}
    {done.length > 0 && !bedtime && <Section label={t(['Done', '做完了'])} n={done.length}>{done.map(s => <Row key={s.id} s={s} c={c} bedtime={bedtime}/>)}</Section>}
  </div>;
}
// One row per session, no groups: beside the dial, whose rings are these rows in order.
function Legend({ rows, c, bedtime, go }: { rows: NightSession[]; c: Ctx; bedtime: boolean; go?: (s: NightSession) => void }) {
  return rows.length ? <div className="nc-list">{rows.map(s => <Row key={s.id} s={s} c={c} bedtime={bedtime} go={go && s.st === 'wait' ? go : undefined}/>)}</div> : null;
}
const ringed = (w: NightWatch) => { const { busy, due, done } = groups(w); return [...busy, ...done, ...due].slice(0, 4); };

// What Jarvis cannot see: Claude Code sessions in a terminal, while reading them is off or its list does not answer.
function Unseen({ w, c }: { w: NightWatch; c: Ctx }) {
  const t = c.t;
  return <>
    {!w.lists.claude && w.seen && <p className="nc-line">{t(['Claude Code sessions in a terminal are not watched; only the deadline covers them', '终端里开的 Claude Code 会话看不到，只有兜底护着'])}</p>}
  </>;
}
const Kv = ({ rows, dt }: { rows: [string, ReactNode][]; dt?: string }) =>
  <dl className="nc-kv" style={dt ? { ['--nc-dt' as string]: dt } : undefined}>{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>;
const Tag = ({ children }: { children: ReactNode }) => <span className="nc-tag">{children}</span>;
const Quiet = ({ children }: { children: ReactNode }) => <span className="nc-quiet">{children}</span>;
const Warm = ({ children }: { children: ReactNode }) => <span className="nc-warm">{children}</span>;

// ---------- the twelve-hour dial of the star-trail look ----------
// The outer ring is the keep-awake: held, and by which rule; each inner ring one session, its working stretches in
// blue and its mark where it is now or where it stopped. Twelve on the clock at the top, as on a watch.
const S = 192, C = S / 2, R = 62, STEP = 10;
const minutes = (ms: number) => { const d = new Date(ms); return d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60; };
const at = (r: number, ms: number): [number, number] => { const a = (minutes(ms) % 720) / 720 * Math.PI * 2; return [C + r * Math.sin(a), C - r * Math.cos(a)]; };
function arc(r: number, from: number, to: number) {
  const [x1, y1] = at(r, from);
  if (to - from >= 12 * 60 * MIN - MIN) { const [x2, y2] = at(r, from + 6 * 60 * MIN); return `M${x1} ${y1} A${r} ${r} 0 1 1 ${x2} ${y2} A${r} ${r} 0 1 1 ${x1} ${y1}`; }
  const [x2, y2] = at(r, to);
  return `M${x1} ${y1} A${r} ${r} 0 ${to - from > 6 * 60 * MIN ? 1 : 0} 1 ${x2} ${y2}`;
}
type Hold = [number, number, 'both' | 'mon' | 'extra' | 'plan' | 'far'];
function Dial({ w, start, end, holds, ticks, labels, big, small, title }: {
  w: NightWatch; start: number; end: number; holds: Hold[]; ticks: [number, string][]; labels: [number, string, string][]; big: string; small: string; title: string;
}) {
  const rows = ringed(w);
  const label = (ms: number, text: string, cls: string, r = R + 13) => {
    const a = (minutes(ms) % 720) / 720 * Math.PI * 2, [x, y] = at(r, ms), s = Math.sin(a), co = Math.cos(a);
    return <text key={`${ms}${text}`} x={x} y={y + (co > .5 ? -2 : co < -.5 ? 8 : 3.5)} textAnchor={s > .35 ? 'start' : s < -.35 ? 'end' : 'middle'} className={cls}>{text}</text>;
  };
  const tick = (ms: number, r0: number, r1: number, cls: string, key: string) => { const [x1, y1] = at(r0, ms), [x2, y2] = at(r1, ms); return <line key={key} x1={x1} y1={y1} x2={x2} y2={y2} className={cls}/>; };
  const noon = new Date(start); noon.setHours(0, 0, 0, 0);
  return <svg className="nc-dial" width={S} height={S} viewBox={`0 0 ${S} ${S}`} role="img" aria-label={title}>
    {rows.map((s, i) => <circle key={`g${s.id}`} cx={C} cy={C} r={R - STEP * (i + 1)} className="nc-groove"/>)}
    <circle cx={C} cy={C} r={R} className="nc-rim"/>
    {Array.from({ length: 12 }, (_, h) => tick(noon.getTime() + h * 60 * MIN, R + 5, R + (h % 3 ? 7 : 9), 'nc-hour', `h${h}`))}
    {[0, 3, 6, 9].map(h => label(noon.getTime() + h * 60 * MIN, String(h), 'nc-num', R + 17))}
    {holds.filter(([a, b]) => b > a).map(([a, b, kind]) => <path key={`${kind}${a}`} d={arc(R, a, b)} className={`nc-hold is-${kind}`}/>)}
    {rows.map((s, i) => {
      const r = R - STEP * (i + 1), before = s.busy && s.since_ms && s.since_ms < start ? [<path key="pre" d={arc(r, Math.max(s.since_ms, end - 12 * 60 * MIN), start)} className="nc-trail is-before"/>] : [];
      return <g key={`t${s.id}`}>{before}{s.trail.map(([a, b]) => <path key={a} d={arc(r, a, Math.min(b ?? end, end))} className="nc-trail"/>)}</g>;
    })}
    {ticks.map(([ms, cls], i) => tick(ms, R - 5, R + 5, `nc-mark ${cls}`, `k${i}`))}
    {rows.map((s, i) => {
      const [x, y] = at(R - STEP * (i + 1), s.busy ? end : s.st === 'wait' ? Math.max(start, s.changed_ms) : s.changed_ms), st = markOf(s);
      return <foreignObject key={`s${s.id}`} x={x - 6} y={y - 6} width={12} height={12} className={`nc-star is-${st}`}><AgentMark look="spark" state={st} id={s.id} size={12} still/></foreignObject>;
    })}
    {labels.map(([ms, text, cls]) => label(ms, text, cls))}
    <text x={C} y={C + 3} textAnchor="middle" className="nc-big">{big}</text>
    <text x={C} y={C + 16} textAnchor="middle" className="nc-small">{small}</text>
  </svg>;
}

// ---------- before the screen goes ----------
function Bedtime({ run, laptop, look, c, act, go }: { run: NightRun; laptop: boolean; look: NightLook; c: Ctx; act: (action: NightAction) => void; go: (s: NightSession) => void }) {
  const { t, lang, now } = c, w = run.watch, until = hhmm(run.until_ms), cap = hhmm(run.cap_ms), hours = span(run.until_ms - run.started_ms, lang);
  const secs = run.dark_at_ms === null ? 0 : Math.ceil((run.dark_at_ms - now) / 1000);
  const count = run.stay ? <p className="nc-count">{t(['The screen goes off after a quiet minute once you have answered', '你回完、一分钟不动就熄屏'])}</p>
    : <p className="nc-count" aria-live="polite">{secs > 0 ? <><b>{secs}</b>{t([' s until the screen goes off', ' 秒后熄屏'])}</> : t(['The screen is going off', '马上熄屏'])}</p>;
  const watching: ReactNode = !w.seen ? <Warm>{t(['No session list can be read; this time it is the deadline alone', '看不到会话列表，这次只按时间'])}</Warm>
    : w.busy ? t([`Once all that work has stopped ${SETTLE_MIN} min, the Mac may sleep`, `在干活的都停下 ${SETTLE_MIN} 分钟，就放开防睡`])
    : t(['Nothing is working now; work that starts in the night is watched too', '现在没有在干活的；夜里开始干活的也会盯']);
  const floor = w.seen ? t([`At least until ${until} (${hours})`, `至少挂到 ${until}（${hours}）`]) : t([`Until ${until}, then let go`, `挂到 ${until} 就放开`]);
  const rows: [string, ReactNode][] = [[t(['Floor', '兜底']), floor], [t(['Watching', '盯着']), watching], ...w.seen ? [[t(['Longest', '最长']), `${cap}（${t(['12 h', '12 小时'])}）`] as [string, ReactNode]] : []];
  const v1 = <ul className="nc-lines">
    <li>{t(['Brightness and sound are noted first and come back when you are up', '熄屏前记下亮度和声音，起来时调回'])}</li>
    <li>{run.wake_at_ms ? t([`Up before ${hhmm(run.wake_at_ms)} is only a look`, `${hhmm(run.wake_at_ms)} 前醒来只看一眼，不算起床`]) : t(['Coming back ends it', '回来就结束挂机'])}</li>
    {laptop && <li>{t(['Closing the lid still sleeps the Mac', '合上盖子 Mac 仍会睡着'])}</li>}
    {!run.guarded && <li className="is-warn">{t(['The Mac could not be kept awake; it may sleep as usual', '没拿到防睡，Mac 可能照常睡着'])}</li>}
  </ul>;
  const bar = <div className="ac-bar">
    <span className="ac-label"><i/>{t(['Night run', '夜间挂机'])}</span>
    <button type="button" className="ac-x" aria-label={t(['Cancel', '不挂了'])} onClick={() => act('end')}><X size={10} weight="bold"/></button>
  </div>;
  const foot = <div className="ac-foot"><span/><button type="button" className="ac-go" onClick={() => act('dark')}>{t(['Screen off now', '现在熄屏'])}</button></div>;
  const due = t(['It waits until you are up if not answered', '不回它会一直等到你起来']);
  if (look === 'trail' && w.seen) return <div className="ac nc-night is-trail" data-night={run.id} data-look="trail">
    {bar}
    <div className="nc-dialrow">
      <Dial w={w} start={run.started_ms} end={now} holds={[[run.started_ms, run.until_ms, 'plan'], [run.until_ms, run.cap_ms, 'far']]} ticks={[[run.started_ms, 'is-ink'], [run.until_ms, 'is-night']]}
        labels={[[run.until_ms, t(['floor', '兜底']), 'nc-lab is-night']]} big={String(w.busy)} small={t(['at work', '个在干活'])}
        title={t([`Night run: ${w.busy} at work, floor until ${until}`, `挂机开始：${w.busy} 个在干活，兜底到 ${until}，最长一圈`])}/>
      <div className="nc-side">{count}<Kv dt="3.2em" rows={[[t(['Floor', '兜底']), t([`until ${until}`, `至少到 ${until}`])], [t(['Watch', '盯着']), t([`${SETTLE_MIN} min quiet`, `停下 ${SETTLE_MIN} 分钟放开`])], [t(['Longest', '最长']), t([`one turn, ${cap}`, `转满一圈 ${cap}`])]]}/></div>
    </div>
    <Legend rows={[...groups(w).busy, ...groups(w).due]} c={c} bedtime go={go}/>
    <Unseen w={w} c={c}/>
    <p className="nc-line">{t([`Brightness and sound noted first · up before ${run.wake_at_ms ? hhmm(run.wake_at_ms) : '—'} is a look`, `熄屏前记下亮度和声音 · ${run.wake_at_ms ? hhmm(run.wake_at_ms) : '—'} 前醒来只算看一眼`])}</p>
    {foot}
  </div>;
  return <div className="ac nc-night" data-night={run.id} data-look="list">
    {bar}{count}<Kv rows={rows}/>
    <Listing w={w} c={c} bedtime go={go} hints={{ due }}/>
    <Unseen w={w} c={c}/>
    {v1}{foot}
  </div>;
}

// ---------- the screen woke in the night ----------
function Night({ run, look, c, act, go, rate }: { run: NightRun; look: NightLook; c: Ctx; act: (action: NightAction) => void; go: (s: NightSession) => void; rate?: ReactNode }) {
  const { t, lang, now } = c, w = run.watch, until = hhmm(run.until_ms), cap = hhmm(run.cap_ms);
  const released = run.released_ms !== null, worked = w.quiet_ms !== null, lookLine = run.wake_at_ms
    ? <p className="nc-line">{t([`Before ${hhmm(run.wake_at_ms)} the screen goes off again after a quiet minute`, `${hhmm(run.wake_at_ms)} 前看一眼，一分钟不动就再熄屏`])}</p> : null;
  let label: string, meta: string, line: ReactNode = null;
  if (released) {
    label = t(['Let go', '已放开防睡']); meta = hhmm(run.released_ms!);
    line = t(['The Mac may sleep as usual; the screen stays dark, brightness and sound come back when you are up', 'Mac 可以照常睡；屏幕继续黑着，起来时调回亮度和声音']);
  } else if (!w.seen) {
    label = t(['Still running', '还在挂着']); meta = t([`floor until ${until}`, `兜底到 ${until}`]);
    line = <Warm>{w.blind_since_ms ? t([`No session list since ${hhmm(w.blind_since_ms)}; let go at ${until}`, `${hhmm(w.blind_since_ms)} 起看不到会话，到 ${until} 就放开`]) : t([`No session list; let go at ${until}`, `看不到会话，到 ${until} 就放开`])}</Warm>;
  } else if (w.busy) {
    label = t(['Still running', '还在挂着']); meta = now < run.until_ms ? t([`at least until ${until}`, `至少到 ${until}`]) : t([`${w.busy} at work`, `${w.busy} 个在干活`]);
    line = t([`Let go ${SETTLE_MIN} min after ${w.busy > 1 ? 'they all stop' : 'it stops'}; ${cap} at the latest`, `${w.busy > 1 ? '都' : '它'}停下 ${SETTLE_MIN} 分钟后放开防睡，最长到 ${cap}`]);
  } else if (worked && run.watch.release_ms && run.watch.release_ms > run.until_ms) {
    label = t(['All stopped', '都停了']); meta = t([`let go at ${hhmm(run.watch.release_ms)}`, `${hhmm(run.watch.release_ms)} 放开防睡`]);
    line = t([`The last stopped at ${hhmm(w.quiet_ms!)}; nothing by ${hhmm(run.watch.release_ms)} and the Mac may sleep`, `最后一个 ${hhmm(w.quiet_ms!)} 停下；到 ${hhmm(run.watch.release_ms)} 还没动静就放开`]);
  } else {
    label = worked ? t(['All stopped', '都停了']) : t(['Still running', '还在挂着']); meta = t([`floor until ${until}`, `兜底挂到 ${until}`]);
    if (worked) line = t([`All stopped at ${hhmm(w.quiet_ms!)}; the floor holds ${span(run.until_ms - now, lang)} more`, `${hhmm(w.quiet_ms!)} 就都停了，兜底还要挂 ${span(run.until_ms - now, lang)}，到点放开`]);
  }
  const bar = <div className="ac-bar"><span className="ac-label"><i/>{label}</span><span className="ac-meta">{meta}</span></div>;
  const foot = <div className="ac-foot"><span/><button type="button" className="ac-go" onClick={() => act('end')}>{t(["I'm up", '我起来了'])}</button></div>;
  const due = released ? undefined : t(['Answer it and it goes on; the run waits for it', '回了它就接着干，挂机会等它']);
  if (!w.seen && w.sessions.length) return <div className="ac nc-night is-dim" data-night={run.id}>
    {bar}<p className="nc-line">{line}</p>
    <div className="nc-list"><Section label={t([`Last seen ${w.blind_since_ms ? hhmm(w.blind_since_ms) : ''}`, `${w.blind_since_ms ? hhmm(w.blind_since_ms) : ''} 最后看到`])} n={w.sessions.length}>
      {w.sessions.map(s => <Row key={s.id} s={s} c={c} bedtime={false} gone said={s.busy ? t(['working then', '那时在干活']) : s.st === 'wait' ? t(['waiting then', '那时在等你']) : status(s, c, false)}/>)}
    </Section></div>
    {lookLine}{rate}{foot}
  </div>;
  if (look === 'trail' && w.seen) {
    const held = run.released_ms ?? now, plan = run.watch.release_ms ?? run.cap_ms;
    return <div className="ac nc-night is-dim is-trail" data-night={run.id} data-look="trail">
      <div className="ac-bar"><span className="ac-label"><i/>{label}</span><span className="ac-meta">{hhmm(now)}</span></div>
      <div className="nc-dialrow">
        <Dial w={w} start={run.started_ms} end={held} holds={[[run.started_ms, held, 'both'], ...released ? [] : [[held, Math.max(held, plan), 'plan'], [Math.max(held, plan), run.cap_ms, 'far']] as Hold[]]}
          ticks={[[held, 'is-ink'], [run.until_ms, 'is-night']]} labels={[[run.until_ms, t(['floor', '兜底']), 'nc-lab is-night']]} big={String(w.busy)} small={t(['at work', '个在干活'])}
          title={t([`${hhmm(now)}: ${w.busy} at work, floor until ${until}`, `${hhmm(now)}：${w.busy} 个还在干活，兜底到 ${until}`])}/>
        <div className="nc-side"><Kv dt="3.2em" rows={[[t(['Floor', '兜底']), t([`until ${until}`, `至少到 ${until}`])], [t(['Working', '在干活']), w.busy ? t([`${w.busy}, let go ${SETTLE_MIN} min after`, `${w.busy} 个，停下 ${SETTLE_MIN} 分钟放开`]) : meta], [t(['Longest', '最长']), cap]]}/></div>
      </div>
      <Legend rows={ringed(w).concat(w.sessions.filter(s => !ringed(w).includes(s)))} c={c} bedtime={false}/>
      {lookLine}{rate}{foot}
    </div>;
  }
  // While work goes on the rule comes after the sessions; once all stopped, what happens next comes first.
  const said = line && <p className="nc-line">{line}</p>, working = w.busy > 0 && !released;
  return <div className="ac nc-night is-dim" data-night={run.id} data-look="list">
    {bar}{!working && said}
    <Listing w={w} c={c} bedtime={false} go={go} hints={{ due }}/>
    {working && said}{lookLine}{rate}{foot}
  </div>;
}

// ---------- back in the morning ----------
function Morning({ last, look, unread, c, onClose, rate }: { last: NightLast; look: NightLook; unread: number; c: Ctx; onClose: () => void; rate?: ReactNode }) {
  const { t, lang } = c, w = last.watch, why = last.release_reason, until = hhmm(last.until_ms);
  const overnight = new Date(last.started_ms).toDateString() !== new Date(last.ended_ms).toDateString();
  const { brightness, volume } = last.restored;
  const back: L = brightness && volume ? ['Brightness and sound', '亮度和声音'] : brightness ? ['Brightness', '亮度'] : volume ? ['Sound', '声音'] : ['Nothing to put back', '没有要还原的'];
  const held = last.released_ms ?? last.ended_ms, monitor = w.monitor_ms ?? null, extra = w.extra_ms ?? 0;
  const awake: ReactNode = last.released_ms === null ? t(['until you were up', '到你起来'])
    : <>{`${hhmm(last.started_ms)} – ${hhmm(last.released_ms)} · `}{why === 'battery' ? <Warm>{t(['battery at 10%, let go early', '电量到 10%，提前放开'])}</Warm>
      : why === 'cap' ? t([`${span(held - last.started_ms, lang)}, the limit`, `${span(held - last.started_ms, lang)}，到上限`]) : span(held - last.started_ms, lang)}</>;
  const byTime: ReactNode = why === 'deadline' || why === 'blind' ? <>{until}<Tag>{t(['this one', '按这个'])}</Tag></>
    : <Quiet>{until}{w.busy_at_deadline ? t([` · ${w.busy_at_deadline} still at work then`, ` · 那时还有 ${w.busy_at_deadline} 个在干活`]) : ''}</Quiet>;
  const byWatch: ReactNode = !w.seen ? <Quiet>{t(['no session list could be read', '看不到会话'])}</Quiet>
    : monitor === null ? <Warm>{why === 'cap' ? t([`never came: ${w.busy_at_release ?? 1} still at work at the limit`, `没等到：到上限时还有 ${w.busy_at_release ?? 1} 个在干活`]) : t([`never came: ${w.busy_at_release ?? 1} still at work then`, `没等到：那时还有 ${w.busy_at_release ?? 1} 个在干活`])}</Warm>
    : why === 'settled' ? <>{hhmm(monitor)}{w.quiet_ms ? t([` · the last stopped at ${hhmm(w.quiet_ms)}`, ` · 最后一个 ${hhmm(w.quiet_ms)} 停下`]) : ''}<Tag>{t(['this one', '按这个'])}</Tag></>
    : <Quiet>{hhmm(monitor)}{' · '}{w.quiet_ms ? t([`all stopped at ${hhmm(w.quiet_ms)}`, `${hhmm(w.quiet_ms)} 就都停了`]) : t(['nothing was working', '没有在干活的'])}{extra > 0 ? t([`, the floor held ${span(extra, lang)} more`, `，兜底多挂 ${span(extra, lang)}`]) : ''}</Quiet>;
  const slept: ReactNode = last.slept_ms === null ? t(['awake the whole time', '一直醒着'])
    : last.released_ms === null || last.slept_ms < last.released_ms ? <Warm>{t([`slept at ${hhmm(last.slept_ms)}, while held`, `${hhmm(last.slept_ms)} 睡着了（还在防睡时）`])}</Warm>
    : t([`slept at ${hhmm(last.slept_ms)}`, `${hhmm(last.slept_ms)} 睡着了`]);
  const agents: [string, ReactNode][] = unread > 0 ? [[t(['Agents', 'Agent']), t([`${unread} to read`, `${unread} 个待看`])]] : [];
  const x = last.totals, data = x.nights > 1 ? <p className="nc-data">{t([`Last ${x.nights} nights · the floor held ${span(x.extra_ms, lang)} more in all`, `近 ${x.nights} 晚 · 兜底共多挂 ${span(x.extra_ms, lang)}`])}
    {x.blind ? t([` · ${x.blind} with no session list, the floor alone`, ` · 有 ${x.blind} 晚看不到会话，只靠兜底护着`]) : ''}</p> : null;
  const bar = (title: string) => <div className="ac-bar">
    <span className="ac-label"><i/>{title}</span>
    <span className="ac-meta">{`${hhmm(last.started_ms)} – ${hhmm(last.ended_ms)}`}</span>
    <button type="button" className="ac-x" aria-label={t(['Dismiss', '知道了'])} onClick={onClose}><X size={10} weight="bold"/></button>
  </div>;
  if (look === 'trail' && w.seen) {
    const kept = Math.min(last.until_ms, held), holds: Hold[] = monitor !== null && monitor < kept
      ? [[last.started_ms, monitor, 'both'], [monitor, kept, 'extra']] : [[last.started_ms, kept, 'both']];
    if (held > last.until_ms) holds.push([last.until_ms, held, 'mon']);
    const labels: [number, string, string][] = held > last.until_ms ? [[(last.until_ms + held) / 2, `+${span(held - last.until_ms, lang)}`, 'nc-lab is-blue']]
      : extra > 0 && monitor !== null ? [[(monitor + kept) / 2, t([`+${span(extra, lang)} floor`, `多挂 ${span(extra, lang)}`]), 'nc-lab is-night']] : [];
    return <div className="ac nc-night is-morning is-trail" data-night={last.id} data-look="trail">
      {bar(overnight ? t(["Last night's trail", '昨晚的星轨']) : t(['Night run', '挂机']))}
      <div className="nc-dialrow">
        <Dial w={w} start={last.started_ms} end={held} holds={holds} ticks={[[last.until_ms, 'is-night']]} labels={labels} big={clock(held - last.started_ms)} small={t(['awake', '防睡'])}
          title={t([`Kept awake ${span(held - last.started_ms, lang)}`, `昨晚：防睡 ${span(held - last.started_ms, lang)}`])}/>
        <div className="nc-side"><Kv dt="3.2em" rows={[[t(['Time', '按时间']), byTime], [t(['Watch', '按监控']), byWatch], [t(['Mac', 'Mac']), slept], [t(['Put back', '还原']), t(back)], ...agents]}/></div>
      </div>
      <Legend rows={ringed(w).concat(w.sessions.filter(s => !ringed(w).includes(s)))} c={c} bedtime={false}/>
      {data}{rate}
    </div>;
  }
  const { busy } = groups(w);
  return <div className="ac nc-night is-morning" data-night={last.id} data-look="list">
    {bar(overnight ? t(['Last night', '昨晚']) : t(['Night run', '挂机']))}
    <Kv rows={[[t(['Kept awake', '防睡']), awake], ...w.seen || w.blind ? [[t(['By time', '按时间']), byTime], [t(['By watch', '按监控']), byWatch]] as [string, ReactNode][] : [],
      [t(['Mac', 'Mac']), slept], [t(['Put back', '还原']), t(back)], ...agents]}/>
    <Listing w={w} c={c} bedtime={false} hints={{ busy: why === 'cap' && busy.length ? t(['Check it is not stuck', '看看是不是卡住了']) : undefined }}/>
    {data}{rate}
  </div>;
}

// `rate` is the 合适吗 row (ADR 0160), for the card when the screen wakes and for the morning one.
export function NightCard({ state, morning, unread, lang, marks, look, act, onGo, onClose, rate }: {
  state: NightState; morning: NightLast | null; unread: number; lang: Lang; marks: MarkLook; look: NightLook;
  act: (action: NightAction) => void; onGo: (session: NightSession) => void; onClose: () => void; rate?: ReactNode;
}) {
  const now = useNow(state.night?.phase === 'starting' ? 250 : 15_000), run = state.night;
  const c: Ctx = { lang, t: (l: L) => tr(lang, l), now, marks };
  if (run) return run.phase === 'starting' ? <Bedtime run={run} laptop={state.laptop} look={look} c={c} act={act} go={onGo}/> : <Night run={run} look={look} c={c} act={act} go={onGo} rate={rate}/>;
  return morning ? <Morning last={morning} look={look} unread={unread} c={c} onClose={onClose} rate={rate}/> : null;
}

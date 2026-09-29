import { X } from '@phosphor-icons/react';
import { tr, type L, type Lang } from './companionSettings';
import { useNow } from './homeData';
import './action-card.css';
import './night-card.css';

// ADR 0093: the night run as GET /inherent/night draws it. `night` is the run now; `last` the one that ended last.
export type NightPhase = 'starting' | 'dark' | 'glance';
export interface NightRun { id: string; phase: NightPhase; started_ms: number; until_ms: number; wake_at_ms: number | null; dark_at_ms: number; released_ms: number | null; guarded: boolean }
export interface NightLast { id: string; started_ms: number; until_ms: number; released_ms: number | null; ended_ms: number; reason: string; slept_ms: number | null; restored: { brightness: boolean; volume: boolean } }
export interface NightState { night: NightRun | null; last: NightLast | null; hours: number; laptop: boolean }
export type NightAction = 'start' | 'dark' | 'end';

// The last run's card stays up to half a day after it ended, until its × is pressed.
export const MORNING_MS = 12 * 3_600_000;
const SEEN = 'companion-night-seen-v1';
export const seenNight = () => { try { return localStorage.getItem(SEEN) ?? ''; } catch { return ''; } };
export const markNightSeen = (id: string) => { try { localStorage.setItem(SEEN, id); } catch { /* shown again after a reload */ } };
// A run that ended because the owner came back or said so; a cancelled one changed nothing and has nothing to tell.
export const morningOf = (s: NightState | null, now: number, seen: string) =>
  s?.last && !s.night && (s.last.reason === 'returned' || s.last.reason === 'ended') && now - s.last.ended_ms < MORNING_MS && s.last.id !== seen ? s.last : null;

const hhmm = (ms: number) => { const d = new Date(ms); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };

// Before the screen goes: where it runs to, the seconds left, and the way out.
function Bedtime({ run, laptop, lang, act }: { run: NightRun; laptop: boolean; lang: Lang; act: (action: NightAction) => void }) {
  const t = (l: L) => tr(lang, l);
  const now = useNow(250), secs = Math.ceil((run.dark_at_ms - now) / 1000), until = hhmm(run.until_ms);
  return <div className="ac nc-night" data-night={run.id}>
    <div className="ac-bar">
      <span className="ac-label"><i/>{t(['Night run', '夜间挂机'])}</span>
      <span className="ac-meta">{t([`until ${until}`, `挂到 ${until}`])}</span>
      <button type="button" className="ac-x" aria-label={t(['Cancel', '不挂了'])} onClick={() => act('end')}><X size={10} weight="bold"/></button>
    </div>
    <p className="nc-count" aria-live="polite">{secs > 0 ? <><b>{secs}</b>{t([' s until the screen goes off', ' 秒后熄屏'])}</> : t(['The screen is going off', '马上熄屏'])}</p>
    <ul className="nc-lines">
      <li>{t(['Brightness and sound are noted first and come back when you are up', '熄屏前记下亮度和声音，起来时调回'])}</li>
      <li>{run.wake_at_ms ? t([`Up before ${hhmm(run.wake_at_ms)} is only a look`, `${hhmm(run.wake_at_ms)} 前醒来只看一眼，不算起床`]) : t(['Coming back ends it', '回来就结束挂机'])}</li>
      {laptop && <li>{t(['Closing the lid still sleeps the Mac', '合上盖子 Mac 仍会睡着'])}</li>}
      {!run.guarded && <li className="is-warn">{t(['The Mac could not be kept awake; it may sleep as usual', '没拿到防睡，Mac 可能照常睡着'])}</li>}
    </ul>
    <div className="ac-foot"><span/><button type="button" className="ac-go" onClick={() => act('dark')}>{t(['Screen off now', '现在熄屏'])}</button></div>
  </div>;
}

// The screen woke in the night: a dim card that the run goes on, and the way to end it.
function Night({ run, lang, act }: { run: NightRun; lang: Lang; act: (action: NightAction) => void }) {
  const t = (l: L) => tr(lang, l);
  return <div className="ac nc-night is-dim" data-night={run.id}>
    <div className="ac-bar">
      <span className="ac-label"><i/>{t(['Still running', '还在挂着'])}</span>
      <span className="ac-meta">{run.released_ms ? t([`let go at ${hhmm(run.released_ms)}`, `${hhmm(run.released_ms)} 已放开防睡`]) : t([`awake until ${hhmm(run.until_ms)}`, `醒到 ${hhmm(run.until_ms)}`])}</span>
    </div>
    {run.wake_at_ms && <p className="nc-line">{t([`Before ${hhmm(run.wake_at_ms)} the screen goes off again after a quiet minute`, `${hhmm(run.wake_at_ms)} 前看一眼，一分钟不动就再熄屏`])}</p>}
    <div className="ac-foot"><span/><button type="button" className="ac-go" onClick={() => act('end')}>{t(["I'm up", '我起来了'])}</button></div>
  </div>;
}

// Back in the morning: how the night went, then the agents' own notices.
function Morning({ last, unread, lang, onClose }: { last: NightLast; unread: number; lang: Lang; onClose: () => void }) {
  const t = (l: L) => tr(lang, l);
  const overnight = new Date(last.started_ms).toDateString() !== new Date(last.ended_ms).toDateString();
  const { brightness, volume } = last.restored;
  const back: L = brightness && volume ? ['Brightness and sound', '亮度和声音'] : brightness ? ['Brightness', '亮度'] : volume ? ['Sound', '声音'] : ['Nothing to put back', '没有要还原的'];
  const rows: [L, string][] = [
    [['Kept awake', '防睡'], last.released_ms ? `${hhmm(last.started_ms)} – ${hhmm(last.released_ms)}` : t(['until you were up', '到你起来'])],
    [['Mac', 'Mac'], last.slept_ms ? t([`slept at ${hhmm(last.slept_ms)}`, `${hhmm(last.slept_ms)} 睡着了`]) : t(['awake the whole time', '一直醒着'])],
    [['Put back', '还原'], t(back)],
    ...unread > 0 ? [[['Agents', 'Agent'], t([`${unread} to read`, `${unread} 个待看`])] as [L, string]] : [],
  ];
  return <div className="ac nc-night is-morning" data-night={last.id}>
    <div className="ac-bar">
      <span className="ac-label"><i/>{overnight ? t(['Last night', '昨晚']) : t(['Night run', '挂机'])}</span>
      <span className="ac-meta">{`${hhmm(last.started_ms)} – ${hhmm(last.ended_ms)}`}</span>
      <button type="button" className="ac-x" aria-label={t(['Dismiss', '知道了'])} onClick={onClose}><X size={10} weight="bold"/></button>
    </div>
    <dl className="ac-kv">{rows.map(([name, value]) => <div key={name[0]}><dt>{t(name)}</dt><dd>{value}</dd></div>)}</dl>
  </div>;
}

export function NightCard({ state, morning, unread, lang, act, onClose }: {
  state: NightState; morning: NightLast | null; unread: number; lang: Lang; act: (action: NightAction) => void; onClose: () => void;
}) {
  const run = state.night;
  if (run) return run.phase === 'starting' ? <Bedtime run={run} laptop={state.laptop} lang={lang} act={act}/> : <Night run={run} lang={lang} act={act}/>;
  return morning ? <Morning last={morning} unread={unread} lang={lang} onClose={onClose}/> : null;
}

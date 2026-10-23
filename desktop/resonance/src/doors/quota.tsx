import { useMemo, useRef, type CSSProperties } from 'react';
import { ArrowCounterClockwise, CalendarBlank, Coins, Gauge, TerminalWindow, Wallet } from '@phosphor-icons/react';
import type { L } from '../companionSettings';
import { clock, dur } from '../dayView';
import { useRoute } from '../homeData';
import { balanceTotal, type Usage, type UsageWindow } from '../QuotaModule';
import type { BarMark, DoorChip, DoorCtx, DoorPage, DoorRow } from '../doors';
import { AskBtn, DoorActs, DoorMeter, DoorSec, useAsk } from './parts';

// 额度: how full Claude's 5-hour limit got, as a page of the day under the bar: a level on the bar for each window (past ones dim, the one that is running lit and carried on dashed), a row each,
// and after the now line the next reset. What each window did comes from the daemon's history of the poll; the numbers of now (the week, Codex, API spend, balances) from its usage route.
// The poll writes a reading only when a number changes, so between two readings the level stood where the first left it (a stair, not a slope), and a failed poll is no reading, not 0%.
// A daemon without the history draws the running window flat at its percent now. A row opens its page in the right column: the window's percent, how much of its time has gone and, at this pace, when it runs out; the week's the same.
// Not here yet (Gaps.png): the token column of each quarter hour by project on the bar, and which project took most of a window: the daemon keeps no tokens by time (the local logs, ccusage, are by day).
const FIVE_H = 5 * 3_600_000, MIN = 60_000;
const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const usd = (n: number) => `${n <= -.005 ? '-' : ''}$${Math.abs(n).toFixed(2)}`;
// The daemon drops sub-seconds, so a reset the API puts at 04:59:59 is the 05:00 one: windows are told apart, and clocked, by the minute.
const minute = (ms: number) => Math.round(ms / MIN) * MIN;
const WEEK = 7 * 24 * 3_600_000;
const hourOf = (ms: number, tz: string) => Number(clock(ms, tz).slice(0, 2));
// The weekday of a moment in the day's zone, as the language writes it (周二, Tue).
const weekday = (ms: number, tz: string, lang: 'en' | 'zh') => { try { return new Intl.DateTimeFormat(lang === 'zh' ? 'zh-CN' : 'en-US', { timeZone: tz || undefined, weekday: 'short' }).format(ms); } catch { return ''; } };

// One reading: the 5-hour window's percent and its reset, each null when the reading has none (an idle window is a percent and no reset).
type Reading = { at: number; pct: number | null; reset: number | null };
const readingOf = (at: number, windows: UsageWindow[]): Reading | null => {
  if (!windows.length) return null;
  const five = windows.find(w => w?.key === 'five_hour'), pct = five?.percent, reset = five?.resets_at ? Date.parse(five.resets_at) : NaN;
  return { at, pct: num(pct) ? pct : null, reset: Number.isFinite(reset) ? minute(reset) : null };
};
// Claude's readings in the history's answer, oldest first.
const readingsOf = (data: unknown): Reading[] => {
  const list = (data as { services?: { claude?: unknown } } | null)?.services?.claude;
  return (Array.isArray(list) ? list as { at_ms?: unknown; status?: unknown; windows?: unknown }[] : [])
    .flatMap(r => { const x = r?.status === 'ok' && num(r.at_ms) && Array.isArray(r.windows) ? readingOf(r.at_ms, r.windows as UsageWindow[]) : null; return x ? [x] : []; })
    .sort((a, b) => a.at - b.at);
};

// A window is what one reset names: it starts 5 h before it and ends at it, or at the first reading that no longer has it (a reset he used). `from` is its level at its start: 0, a window starts empty,
// unless the readings begin after it did (then the first one is all there is to go by).
type Win = { reset: number; start: number; end: number; from: number; steps: { at: number; v: number }[] };
function windowsOf(rs: Reading[]): Win[] {
  const wins: Win[] = [], known = rs[0]?.at ?? Infinity;
  let cur: Win | null = null;
  for (const r of rs) {
    const { pct, reset } = r, live = pct !== null && reset !== null && r.at <= reset + MIN;
    if (cur && !(live && reset === cur.reset)) { cur.end = Math.min(cur.reset, r.at); cur = null; }
    if (!live) continue;
    const v = pct / 100;
    if (!cur) { const start: number = reset - FIVE_H; cur = { reset, start, end: reset, from: start < known ? v : 0, steps: [] }; wins.push(cur); }
    cur.steps.push({ at: Math.max(r.at, cur.start), v });
  }
  return wins;
}
// A window's level as a stair: it goes up at the time of the reading that says so. `tip` is how far it is drawn: now for the window that runs, its end for one that is over.
function levelOf(w: Win, tip: number) {
  const pts: { at: number; value: number }[] = [], put = (at: number, value: number) => { const p = pts[pts.length - 1]; if (!p || p.at !== at || p.value !== value) pts.push({ at, value }); };
  let v = w.from;
  put(w.start, v);
  for (const s of w.steps) { if (s.v !== v) put(s.at, v); put(s.at, s.v); v = s.v; }
  put(Math.max(tip, w.steps[w.steps.length - 1].at), v);
  return pts;
}
const percent = (v: number) => Math.round(v * 100);

export function useQuotaDoor(ctx: DoorCtx): DoorPage | null {
  const { port, open, t, lang, day } = ctx, askAbout = useAsk(ctx);
  const usage = useRoute<Usage>(port, '/inherent/usage', open, 60_000);
  const path = `/inherent/usage/history?from_ms=${day.start}&to_ms=${day.end}`, hist = useRoute<unknown>(port, path, open && day.end > day.start, 60_000);
  // useRoute keeps its last answer while the path changes (another day): an answer is this day's only if it came for this path.
  const got = useRef({ data: hist.data, path });
  if (hist.data !== got.current.data) got.current = { data: hist.data, path };
  const answer = got.current.path === path ? hist.data : null;
  return useMemo((): DoorPage => {
    const { claude, codex, openai, anthropic, deepseek, minimax } = usage.data?.services ?? {};
    const cw = claude?.status === 'ok' ? claude.data.windows ?? [] : [], five = cw.find(w => w.key === 'five_hour'), week = cw.find(w => w.key === 'seven_day');
    const pct = (w?: UsageWindow) => w && num(w.percent) ? Math.round(w.percent) : null, f = pct(five), wk = pct(week);

    // The windows of the shown day. On today the reading the usage route has now is one more reading (it may be newer than the history's last), so the page agrees with its own numbers.
    const past = readingsOf(answer), seen = claude?.status === 'ok' && day.today ? readingOf(num(claude.observed_at_ms) ? claude.observed_at_ms : day.now, cw) : null;
    const rs = seen && !past.some(r => r.at === seen.at) ? [...past, seen].sort((a, b) => a.at - b.at) : past;
    const wins = windowsOf(rs).filter(w => w.end > day.start && w.start < day.end), live = wins.find(w => day.today && w.end > day.now);
    const marks: BarMark[] = wins.map(w => ({ kind: 'level', points: levelOf(w, w === live ? day.now : w.end), tone: w === live ? 'on' : 'dim', ...w === live && { until: Math.min(w.end, day.end) }, key: `w${w.reset}` }));
    const rows: DoorRow[] = wins.map(w => {
      const top = percent(Math.max(...w.steps.map(s => s.v))), now = percent(w.steps[w.steps.length - 1].v);
      return { key: `w${w.reset}`, at: w.start, icon: <Gauge size={13} aria-hidden="true"/>, title: w === live ? t(['This window', '现在这段']) : hourOf(w.start, day.tz) < 12 ? t(['Morning window', '早上这段']) : hourOf(w.start, day.tz) < 18 ? t(['Afternoon window', '下午这段']) : t(['Evening window', '晚上这段']), sub: `${clock(w.start, day.tz)}–${clock(w.end, day.tz)}`,
        // the frame's side has no `is-value`, so the running window's number carries the day's own
        side: w === live ? <span className="dt-tm is-value">{t([`Used ${now}%`, `已用 ${now}%`])}</span> : t([`Up to ${top}%`, `用到 ${top}%`]), tone: w === live ? 'live' : undefined, opens: true };
    });
    if (live) {
      const left = dur(Math.ceil((live.end - day.now) / MIN), t);
      rows.push({ key: 'reset', at: live.end, icon: <ArrowCounterClockwise size={13} aria-hidden="true"/>, title: t(['Resets', '重置']), sub: t(['A new window starts', '这 5 小时重新算']), side: t([`in ${left}`, `还有 ${left}`]), tone: 'future' });
    }
    // After the now line, what is counted by the week: Claude's week and Codex's, from the usage route (today only; the route knows no other day).
    const cxw = codex?.status === 'ok' ? codex.data.windows?.find(w => w.key === 'primary_window') : undefined, cxp = pct(cxw), wkReset = week?.resets_at ? Date.parse(week.resets_at) : NaN;
    if (day.today && wk !== null) rows.push({ key: 'week', at: null, icon: <CalendarBlank size={13} aria-hidden="true"/>, title: t(['This week', '本周额度']), sub: Number.isFinite(wkReset) ? t([`Resets ${weekday(wkReset, day.tz, 'en')} ${clock(wkReset, day.tz)}`, `${weekday(wkReset, day.tz, 'zh')} ${clock(wkReset, day.tz)} 重置`]) : undefined, side: `${wk}%`, opens: true });
    if (day.today && cxp !== null) rows.push({ key: 'codex', at: null, icon: <TerminalWindow size={13} aria-hidden="true"/>, title: 'Codex', sub: cxw?.resets_at ? t([`Resets ${weekday(Date.parse(cxw.resets_at), day.tz, 'en')} ${clock(Date.parse(cxw.resets_at), day.tz)}`, `${weekday(Date.parse(cxw.resets_at), day.tz, 'zh')} ${clock(Date.parse(cxw.resets_at), day.tz)} 重置`]) : undefined, side: `${cxp}%`, opens: true });

    // A row's page. A window: how much of the 5 hours has been used against how much of the time has gone, and at this pace when it runs out (a window that is over says only how high it got). The week: the same against 7 days.
    const meterOf = (used: number, from: number, to: number, ongoing: boolean) => { const gone = ongoing ? (day.now - from) / (to - from) : null; return { used, gone }; };
    const pace = (w: Win) => {
      const v = w.steps[w.steps.length - 1].v, gone = day.now - w.start, out = v > 0 && gone >= 5 * MIN ? w.start + gone / v : Infinity;
      return out < w.end ? t([`At this pace it runs out around ${clock(out, day.tz)}; it resets at ${clock(w.end, day.tz)}.`, `照现在的速度，${clock(out, day.tz)} 左右用完，${clock(w.end, day.tz)} 才重置。`])
        : t([`At this pace it won't run out; it resets at ${clock(w.end, day.tz)}.`, `照现在的速度，这一段用不完，${clock(w.end, day.tz)} 重置。`]);
    };
    const weekPage = (title: string, used: number, reset: number, label: string) => {
      const gone = Number.isFinite(reset) ? 1 - (reset - day.now) / WEEK : null;
      return <>
        <DoorSec em={Number.isFinite(reset) ? t([`resets ${weekday(reset, day.tz, 'en')} ${clock(reset, day.tz)}`, `${weekday(reset, day.tz, 'zh')} ${clock(reset, day.tz)} 重置`]) : undefined}>{title}</DoorSec>
        <div className="dd-big"><b>{used}%</b><em>{t(['used', '用掉了'])}</em></div>
        <DoorMeter fill={used / 100} mark={gone}/>
        {gone !== null && <p className="dd-note">{t([`${Math.round(gone * 100)}% of ${label} has gone; ${used}% is used.`, `这${label}过了 ${Math.round(gone * 100)}% 的时间，用了 ${used}%。`])}</p>}
      </>;
    };
    const detail: DoorPage['detail'] = key => {
      const w = wins.find(x => `w${x.reset}` === key);
      if (w) {
        const used = percent(w.steps[w.steps.length - 1].v), top = percent(Math.max(...w.steps.map(s => s.v))), on = w === live, { gone } = meterOf(used, w.start, w.end, on), range = `${clock(w.start, day.tz)}–${clock(w.end, day.tz)}`;
        return { title: t(['Usage', '额度']), body: <>
          <DoorSec em={range}>{on ? t(['This window', '现在这段']) : t(['5-hour window', '5 小时额度'])}</DoorSec>
          <div className="dd-big"><b>{on ? used : top}%</b><em>{on ? t(['used', '用掉了']) : t(['at most', '最高用到'])}</em></div>
          <DoorMeter fill={(on ? used : top) / 100} mark={gone}/>
          {on && gone !== null && <>
            <div className="dd-legend"><span style={{ '--k': '157 180 255' } as CSSProperties}>{t(['Used ', '用掉的 '])}<em>{used}%</em></span><span style={{ '--k': '255 240 220' } as CSSProperties}>{t(['Time gone ', '过去的时间 '])}<em>{Math.round(gone * 100)}%</em></span></div>
            <p className="dd-note">{pace(w)}</p>
            {askAbout(t(['the 5-hour usage window', '这 5 小时的额度'])) && <DoorActs><AskBtn ask={askAbout(t(['the 5-hour usage window', '这 5 小时的额度']))!} text={t(['How can I use less?', '怎么省着用？'])} label={t(['Ask how to save', '问她怎么省'])} t={t}/></DoorActs>}</>}
          {on && Number.isFinite(wkReset) && wk !== null && weekPage(t(['This week', '本周']), wk, wkReset, t(['of the week', '周']))}</> };
      }
      if (key === 'week' && wk !== null) return { title: t(['Usage', '额度']), body: weekPage(t(['This week', '本周']), wk, wkReset, t(['of the week', '周'])) };
      if (key === 'codex' && cxp !== null) {
        const reset = cxw?.resets_at ? Date.parse(cxw.resets_at) : NaN;
        return { title: 'Codex', body: weekPage('Codex', cxp, reset, t(['of the period', '一段'])) };
      }
      return null;
    };

    // What the page says in numbers. On today: the 5-hour window and the week, Codex, what the API cost today, and what is left in each account that says. On another day: only what that day's windows reached.
    const chips: DoorChip[] = [], icon = (Glyph: typeof Gauge) => <Glyph size={12} aria-hidden="true"/>;
    const parts: L[] = [];
    let line: string | null;
    if (day.today) {
      const reset = live ? clock(live.end, day.tz) : null;
      if (f !== null) { parts.push([`${f}% of this 5-hour window used${reset ? `, resets at ${reset}` : ''}`, `这 5 小时用了 ${f}%${reset ? `，${reset} 重置` : ''}`]); chips.push({ id: 'five', icon: icon(Gauge), label: t(['5-hour', '5 小时']), value: `${f}%` }); }
      if (wk !== null) { parts.push([`${wk}% this week`, `这周用了 ${wk}%`]); chips.push({ id: 'week', icon: icon(CalendarBlank), label: t(['Week', '本周']), value: `${wk}%` }); }
      const cx = codex?.status === 'ok' ? pct(codex.data.windows?.find(w => w.key === 'primary_window')) : null;
      if (cx !== null) chips.push({ id: 'codex', icon: icon(TerminalWindow), label: 'Codex', value: `${cx}%` });
      const spent = [openai?.status === 'ok' ? openai.data.today_usd : undefined, anthropic?.status === 'ok' ? (anthropic.data.official ?? anthropic.data).today_usd : undefined].filter(num);
      if (spent.length) chips.push({ id: 'api', icon: icon(Coins), label: t(['API today', 'API 今天']), value: usd(spent.reduce((a, b) => a + b, 0)) });
      const dsk = deepseek?.status === 'ok' ? deepseek.data.balances : undefined, oab = openai?.status === 'ok' ? openai.data.balance_usd : undefined, mmb = minimax?.status === 'ok' ? minimax.data.balance : undefined, anb = anthropic?.status === 'ok' ? anthropic.data.official?.balance_usd : undefined;
      for (const [name, value] of [['DeepSeek', dsk && Object.keys(dsk).length ? balanceTotal(dsk) : null], ['OpenAI', num(oab) ? usd(oab) : null], ['MiniMax', num(mmb) ? usd(mmb) : null], ['Anthropic', num(anb) ? usd(anb) : null]] as const)
        if (value) chips.push({ id: `balance-${name}`, icon: icon(Wallet), label: name, value });
      line = usage.data || usage.missing ? parts.length ? t([`${parts.map(p => p[0]).join('; ')}.`, `${parts.map(p => p[1]).join('；')}。`]) : t(["Haven't read your usage yet.", '还没读到额度。']) : null;
    } else {
      const top = wins.length ? percent(Math.max(...wins.flatMap(w => w.steps.map(s => s.v)))) : null;
      if (top !== null) chips.push({ id: 'peak', icon: icon(Gauge), label: t(['Highest', '最高']), value: `${top}%` });
      line = top === null ? null : t([`${wins.length} five-hour window${wins.length === 1 ? '' : 's'} that day, up to ${top}%.`, `这天开了 ${wins.length} 段 5 小时，最高用到 ${top}%。`]);
    }

    // A page waits for both answers it draws from (the usage route only on today), so it comes in whole and not as a flat line that turns into a stair.
    const usageIn = !!usage.data || usage.missing || usage.failed, histIn = !!answer || hist.missing || hist.failed, loading = !!port && (!histIn || day.today && !usageIn);
    const why = !claude || usage.missing ? t(["Usage isn't set up on this Jarvis", '这台 Jarvis 还没设置额度']) : claude.status === 'unconfigured' ? t(["Claude isn't signed in", '没登录 Claude'])
      : claude.status === 'error' ? t(["Couldn't read Claude's usage", '没读到 Claude 的额度']) : t(['No usage recorded today yet', '今天还没有额度记录']);
    return {
      count: f !== null ? `${f}%` : null, line, chips, marks: loading ? [] : marks, rows: loading ? [] : rows, nowAt: day.today ? day.now : null,
      empty: !port ? t(['Not connected to Jarvis yet', '还没连上 Jarvis']) : !day.today ? hist.missing ? t(["This Jarvis can't give usage history yet", '这台 Jarvis 还不会给额度的历史']) : t(['No usage recorded that day', '这天没有额度记录']) : why,
      loading, failed: day.today ? !usage.data && usage.failed : !answer && hist.failed,
      detail, untimed: rows.some(r => r.at === null) ? t(['This week', '本周']) : null, rest: live ? `w${live.reset}` : null,
    };
  }, [usage.data, usage.missing, usage.failed, answer, hist.missing, hist.failed, port, day.start, day.end, day.today, day.now, day.tz, lang, askAbout]);
}

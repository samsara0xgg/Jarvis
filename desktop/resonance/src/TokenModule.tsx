import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import { tr, useCompanionSettings, type L } from './companionSettings';

// The Usage page's Token section: what Claude Code, Codex and Hermes used, priced at API rates. Main reads the
// local logs (electron/tokenUsage.ts); this file only sums and draws what it is given.
export type TokenAgent = 'claude' | 'codex' | 'hermes';
export type TokenDay = { date: string; agent: TokenAgent; cost: number; tokens: number; output: number };
export type TokenSession = { id: string; agent: TokenAgent; title: string; folder: string; date: string; cost: number; tokens: number; output: number; models: { model: string; cost: number; output: number }[] };
export type TokenUsage = { days: TokenDay[]; sessions: TokenSession[]; at: number };

// The one failure the renderer words itself; TokenSection says it in the interface language.
const NO_APP = 'no-desktop-app';
export function useTokenUsage(active: boolean) {
  const [data, setData] = useState<TokenUsage | null>(null), [error, setError] = useState(''), [loading, setLoading] = useState(false);
  const busy = useRef(false);
  const load = useCallback(async (refresh: boolean) => {
    if (busy.current) return;
    busy.current = true; setLoading(true);
    try {
      if (!window.jarvis?.tokenUsage) throw new Error(NO_APP);
      setData(await window.jarvis.tokenUsage(refresh)); setError('');
    } catch (e) { setError(String((e as Error).message ?? e).replace(/^Error invoking remote method '[^']*': (Error: )?/, '')); }
    finally { busy.current = false; setLoading(false); }
  }, []);
  // Opening the page reads main's 10-minute cache; the refresh button forces a rerun.
  useEffect(() => { if (active) void load(false); }, [active, load]);
  return { data, error, loading, refresh: () => void load(true) };
}

const AGENTS: { id: TokenAgent; name: string; color: string }[] = [
  { id: 'claude', name: 'Claude Code', color: 'var(--status-ask)' },
  { id: 'codex', name: 'Codex', color: 'var(--data)' },
  { id: 'hermes', name: 'Hermes', color: 'var(--status-done)' },
];
const RANGES = [{ days: 1, name: ['Today', '今天'] }, { days: 7, name: ['7 days', '7 天'] }, { days: 30, name: ['30 days', '30 天'] }] as const;
const TOP = 8;
const pad = (n: number) => String(n).padStart(2, '0');
const ymd = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const usd = (n: number) => `$${n.toFixed(2)}`;
const compact = (n: number, zh: boolean) => zh
  ? n >= 1e8 ? `${(n / 1e8).toFixed(1)} 亿` : n >= 1e4 ? `${Math.round(n / 1e4)} 万` : String(Math.round(n))
  : n >= 1e9 ? `${(n / 1e9).toFixed(1)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}K` : String(Math.round(n));
const sum = <T,>(rows: T[], pick: (row: T) => number) => rows.reduce((total, row) => total + pick(row), 0);

export function TokenSection({ state }: { state: ReturnType<typeof useTokenUsage> }) {
  const [{ lang }] = useCompanionSettings(), zh = lang === 'zh', t = (l: L) => tr(lang, l);
  const [span, setSpan] = useState<number>(7), [all, setAll] = useState(false), [open, setOpen] = useState<string | null>(null);
  const { data, loading } = state, error = state.error === NO_APP ? t(['Token stats need the desktop app', 'Token 统计需要桌面版']) : state.error;
  const dates = Array.from({ length: span }, (_, i) => ymd(new Date(Date.now() - (span - 1 - i) * 86_400_000)));
  const days = data?.days.filter(d => dates.includes(d.date)) ?? [], sessions = (data?.sessions.filter(s => dates.includes(s.date)) ?? []).sort((a, b) => b.cost - a.cost);
  const cost = (agent: TokenAgent, rows = days) => sum(rows.filter(d => d.agent === agent), d => d.cost);
  const total = sum(days, d => d.cost), peak = Math.max(...dates.map(date => sum(days.filter(d => d.date === date), d => d.cost)), .01);
  const shown = all ? sessions : sessions.slice(0, TOP);
  const dayLabel = (date: string) => `${Number(date.slice(5, 7))}/${Number(date.slice(8))}`;
  return <div className="pg-sec tku">
    <div className="tku-head"><h4>Token<span className="tku-note">{t(['at API rates', '按 API 价折算'])}</span></h4>
      <div className="tku-seg" role="group" aria-label={t(['Range', '范围'])}>{RANGES.map(r =>
        <button key={r.days} className="mp-chip" aria-pressed={span === r.days} onClick={() => { setSpan(r.days); setAll(false); }}>{t([...r.name])}</button>)}</div></div>
    {!data ? <p className="muted">{error || (loading ? t(['Counting…', '正在统计…']) : '')}</p> : <>
      <div className="tku-total"><b>{usd(total)}</b>
        <span className="muted">{compact(sum(days, d => d.tokens), zh)} tokens · {t(['output', '输出'])} {compact(sum(days, d => d.output), zh)}{error && ` · ${error}`}</span></div>
      {span > 1 && <>
        <div className="tku-bars" role="img" aria-label={t(['Cost per day', '每日花费'])}>{dates.map(date => {
          const rows = days.filter(d => d.date === date);
          return <span key={date} title={`${dayLabel(date)} · ${AGENTS.map(a => `${a.name} ${usd(cost(a.id, rows))}`).join(' · ')}`}>
            {AGENTS.map(a => cost(a.id, rows) > 0 && <i key={a.id} style={{ height: `${cost(a.id, rows) / peak * 100}%`, background: a.color }}/>)}</span>;
        })}</div>
        <div className="tku-legend">{AGENTS.map(a => <span key={a.id}><i style={{ background: a.color }}/>{a.name}</span>)}<em>{dayLabel(dates[0])} – {dayLabel(dates[span - 1])}</em></div></>}
      <div className="tku-by">{AGENTS.map(a => {
        const rows = days.filter(d => d.agent === a.id);
        return <div key={a.id} className="tku-row"><span>{a.name}</span>
          <span className="tku-share"><i style={{ width: `${total ? cost(a.id) / total * 100 : 0}%`, background: a.color }}/></span>
          <b>{usd(cost(a.id))}</b><small>{compact(sum(rows, d => d.tokens), zh)}</small></div>;
      })}</div>
      <h4 className="tku-sub">{t(['Sessions', '会话'])}</h4>
      {!sessions.length && <p className="muted">{t(['No sessions in this range.', '这个范围里没有会话。'])}</p>}
      <div className="tku-list">{shown.map(s => {
        const agent = AGENTS.find(a => a.id === s.agent)!, expanded = open === s.id;
        return <div key={s.id} className="tku-ses" data-open={expanded}>
          <button aria-expanded={expanded} onClick={() => setOpen(expanded ? null : s.id)}>
            <span className="tku-title">{s.title || (s.agent === 'hermes' ? t(['Hermes scheduled job', 'Hermes 定时任务']) : s.id)}</span>
            <span className="tku-tag" style={{ '--c': agent.color } as CSSProperties}>{agent.name}</span>
            <small>{dayLabel(s.date)}</small><b>{usd(s.cost)}</b></button>
          {expanded && <div className="tku-det">
            {s.folder && <small>{s.folder}</small>}
            {s.models.map(m => <span key={m.model}><em>{m.model}</em>{usd(m.cost)}<small>{compact(m.output, zh)} {t(['out', '输出'])}</small></span>)}
            <small>{compact(s.tokens, zh)} tokens · {t(['output', '输出'])} {compact(s.output, zh)}</small></div>}
        </div>;
      })}</div>
      {sessions.length > TOP && <button className="tku-more" onClick={() => setAll(v => !v)}>{all ? t(['Show less', '收起']) : t([`Show all ${sessions.length}`, `显示全部 ${sessions.length} 个`])}</button>}
    </>}
  </div>;
}

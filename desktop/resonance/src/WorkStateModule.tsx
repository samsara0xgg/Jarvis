import { useCallback, useEffect, useRef, useState } from 'react';
import { tr, useT, type L, type Lang } from './companionSettings';

// ADR 0023: the 当前状态 module. The daemon owns the record (`GET /inherent/work-state`,
// `POST /inherent/work-state/refresh` runs the same analysis the conversation uses);
// this file only fetches and reads it. Without a `port` (the --demo companion) it shows demo data.

export type Basis = 'stated' | 'observed' | 'inferred';
export type Claim = { text: string; basis: Basis; refs: string[]; progress?: string | null; as_of?: string | null };
export type Link = { kind: 'todo' | 'discussion'; ref: string; title: string; note: string; basis: Basis };
export type WorkStateRecord = {
  version: number; analyzed_at: string; observed_until: string | null;
  evidence: { coverage: Record<string, string>; counts: Record<string, number>; limits: string[] };
  now: Claim | null; activities: Claim[]; links: Link[]; uncertainties: string[]; note: string | null;
};
export type DataHead = { status: string; capture_latest_seen: string | null; span_latest_end: string | null; observed_at_ms: number; reason?: string | null };
export type Freshness = { checked_at_ms: number | null; latest_observed_at: string | null; analyzed_at: string | null; analysis_observed_until: string | null };
export type Outcome = 'analyzed' | 'reused' | 'no_evidence' | 'failed' | null;
export type WorkState = { state: WorkStateRecord | null; data: DataHead | null; freshness: Freshness; refreshing: boolean; outcome: Outcome; error: string | null };

const demoAt = Date.now();
const iso = (ms: number) => new Date(ms).toISOString();
export const demoWorkState: WorkState = {
  state: {
    version: 3, analyzed_at: iso(demoAt - 4 * 60_000), observed_until: iso(demoAt - 6 * 60_000),
    evidence: { coverage: { app: 'partial', screen: 'partial', records: 'available', todos: 'available', git: 'unavailable' }, counts: { recent: 12, records: 5, todos: 2 }, limits: ['没有配置被观察的 Git 仓库：没有 Git 活动数据'] },
    now: { text: 'Adding a “Now” card to the Resonance dashboard.', basis: 'observed', refs: ['s1'], as_of: iso(demoAt - 6 * 60_000) },
    activities: [
      { text: 'Morning: TimeSink screen-capture code, splitting lock and sleep segments.', basis: 'observed', progress: 'Fix committed to the screen-capture branch.', refs: ['s2'] },
      { text: 'Noon: talked with Jarvis about the work-state record format.', basis: 'stated', progress: null, refs: ['r1'] },
      { text: 'Maybe getting tonight’s Typlus release ready.', basis: 'inferred', progress: null, refs: [] },
    ],
    links: [{ kind: 'todo', ref: 'todo_1', title: 'Merge the TimeSink fix into main', note: 'Today’s changes all lead here; not merged yet.', basis: 'inferred' }],
    uncertainties: ['13:00–14:10 the screen was locked, so there is no data for it.'],
    note: null,
  },
  data: { status: 'ok', capture_latest_seen: iso(demoAt - 90_000), span_latest_end: iso(demoAt - 30_000), observed_at_ms: demoAt - 60_000 },
  freshness: { checked_at_ms: demoAt - 60_000, latest_observed_at: iso(demoAt - 30_000), analyzed_at: iso(demoAt - 4 * 60_000), analysis_observed_until: iso(demoAt - 6 * 60_000) },
  refreshing: false, outcome: 'analyzed', error: null,
};

export function useWorkState(port: string | null) {
  const [view, setView] = useState<WorkState | null>(port ? null : demoWorkState);
  const [refreshing, setRefreshing] = useState(false);
  const [notice, setNotice] = useState<L | null>(null), t = useT();
  const inFlight = useRef<Promise<void> | null>(null);
  const load = useCallback(async () => {
    if (!port) return;
    try { const r = await fetch(`http://127.0.0.1:${port}/inherent/work-state`); if (r.ok) { const next = await r.json() as WorkState; setView(prev => prev?.state && (!next.state || next.state.version < prev.state.version) ? prev : next); } } catch { /* daemon away; keep the last answer */ }
  }, [port]);
  useEffect(() => {
    void load();
    if (!port) return;
    const id = setInterval(() => void load(), 60_000);
    return () => clearInterval(id);
  }, [load, port]);
  const refresh = useCallback(() => {
    if (!port) return;
    // A second click while one refresh runs joins it: the daemon is single-flight per input and so are we.
    if (inFlight.current) return;
    setRefreshing(true); setNotice(null);
    inFlight.current = (async () => {
      try {
        const r = await fetch(`http://127.0.0.1:${port}/inherent/work-state/refresh`, { method: 'POST' });
        if (!r.ok) { setNotice([`Couldn’t update: Jarvis returned ${r.status}`, `未更新：服务返回 ${r.status}`]); return; }
        const next = await r.json() as WorkState;
        // A reply that carries an older record than the one on screen never overwrites it.
        setView(prev => (prev?.state && next.state && next.state.version < prev.state.version ? { ...next, state: prev.state } : next));
        setNotice(next.outcome === 'failed' ? [`Couldn’t update: ${next.error ?? 'the analysis failed'}`, `未更新：${next.error ?? '分析失败'}`]
          : next.outcome === 'reused' ? ['Nothing new, so the last analysis still stands', '没有新证据，沿用上次分析']
          : next.outcome === 'no_evidence' ? ['Nothing to analyze yet', '没有可分析的数据，未知'] : null);
      } catch { setNotice(['Couldn’t update: can’t reach Jarvis', '未更新：连不上 Jarvis']); } finally { setRefreshing(false); inFlight.current = null; }
    })();
  }, [port]);
  return { view, refresh, refreshing, notice: notice && t(notice) };
}

const pad = (n: number) => String(n).padStart(2, '0');
const hm = (value: string | number | null | undefined) => { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.getTime()) ? '—' : `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const ageMinutes = (value: string | number | null | undefined, now = Date.now()) => { if (!value) return null; const t = new Date(value).getTime(); return Number.isNaN(t) ? null : Math.round((now - t) / 60_000); };
const STALE_MINUTES = 30;
const RECENT_MINUTES = 120;

// Three clocks, kept apart on purpose: when the daemon last checked TimeSink, the newest
// observation it found, and when the analysis ran. A successful check is not fresh data.
export function freshnessLine(view: WorkState | null, lang: Lang = 'en'): { text: string; stale: boolean } {
  const t = (l: L) => tr(lang, l);
  const f = view?.freshness;
  const unavailable = view?.data?.status === 'unavailable';
  const age = ageMinutes(f?.latest_observed_at);
  const stale = unavailable || age === null || age > STALE_MINUTES;
  const data = unavailable ? t(['TimeSink unavailable', 'TimeSink 不可用']) : f?.latest_observed_at ? t([`Last activity ${hm(f.latest_observed_at)}`, `最近动静 ${hm(f.latest_observed_at)}`]) : t(['Nothing observed yet', '还没观察到动静']);
  const checked = f?.checked_at_ms ? t([`checked ${hm(f.checked_at_ms)}`, `${hm(f.checked_at_ms)} 检查`]) : t(['not checked', '没检查过']);
  const analysed = f?.analyzed_at ? t([`analyzed ${hm(f.analyzed_at)}`, `${hm(f.analyzed_at)} 分析`]) : t(['not analyzed', '没分析过']);
  return { text: `${data} · ${checked} · ${analysed}`, stale };
}

// "最近在做" is only ever shown with the instant it is true for; past the recent window it is
// history, not the present.
export function nowLine(now: Claim | null | undefined, analyzedAt: string | null | undefined, at = Date.now(), lang: Lang = 'en'): { text: string; current: boolean; at: string; claim: string } | null {
  if (!now) return null;
  const asOf = now.as_of ?? analyzedAt ?? null;
  const age = ageMinutes(asOf, at);
  const current = age !== null && age <= RECENT_MINUTES;
  return { text: tr(lang, current ? [`As of ${hm(asOf)}: ${now.text}`, `截至 ${hm(asOf)}：${now.text}`] : [`At ${hm(asOf)}: ${now.text}`, `${hm(asOf)}：${now.text}`]), current, at: hm(asOf), claim: now.text };
}

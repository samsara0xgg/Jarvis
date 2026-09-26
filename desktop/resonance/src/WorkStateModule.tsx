import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowsClockwise } from '@phosphor-icons/react';
import './work-state-module.css';

// ADR 0023: the 当前状态 module. The daemon owns the record (`GET /inherent/work-state`,
// `POST /inherent/work-state/refresh` runs the same analysis the conversation uses);
// this file owns presentation only. Without a `port` (design lab) it shows demo data.

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
  const [notice, setNotice] = useState<string | null>(null);
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
        if (!r.ok) { setNotice(`未更新：服务返回 ${r.status}`); return; }
        const next = await r.json() as WorkState;
        // A reply that carries an older record than the one on screen never overwrites it.
        setView(prev => (prev?.state && next.state && next.state.version < prev.state.version ? { ...next, state: prev.state } : next));
        setNotice(next.outcome === 'failed' ? `未更新：${next.error ?? '分析失败'}` : next.outcome === 'reused' ? '没有新证据，沿用上次分析' : next.outcome === 'no_evidence' ? '没有可分析的数据，未知' : null);
      } catch { setNotice('未更新：连不上 Jarvis'); } finally { setRefreshing(false); inFlight.current = null; }
    })();
  }, [port]);
  return { view, refresh, refreshing, notice };
}

const pad = (n: number) => String(n).padStart(2, '0');
const hm = (value: string | number | null | undefined) => { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.getTime()) ? '—' : `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const ageMinutes = (value: string | number | null | undefined, now = Date.now()) => { if (!value) return null; const t = new Date(value).getTime(); return Number.isNaN(t) ? null : Math.round((now - t) / 60_000); };
const basisText: Record<Basis, string> = { stated: '你说的', observed: '观察到', inferred: '推断' };
const STALE_MINUTES = 30;
const RECENT_MINUTES = 120;

// Three clocks, kept apart on purpose: when the daemon last checked TimeSink, the newest
// observation it found, and when the analysis ran. A successful check is not fresh data.
export function freshnessLine(view: WorkState | null): { text: string; stale: boolean } {
  const f = view?.freshness;
  const unavailable = view?.data?.status === 'unavailable';
  const age = ageMinutes(f?.latest_observed_at);
  const stale = unavailable || age === null || age > STALE_MINUTES;
  const data = unavailable ? 'TimeSink unavailable' : f?.latest_observed_at ? `Seen ${hm(f.latest_observed_at)}` : 'Nothing observed yet';
  const checked = f?.checked_at_ms ? `checked ${hm(f.checked_at_ms)}` : 'not checked';
  const analysed = f?.analyzed_at ? `analyzed ${hm(f.analyzed_at)}` : 'not analyzed';
  return { text: `${data} · ${checked} · ${analysed}`, stale };
}

// "最近在做" is only ever shown with the instant it is true for; past the recent window it is
// history, not the present.
export function nowLine(now: Claim | null | undefined, analyzedAt: string | null | undefined, at = Date.now()): { text: string; current: boolean; at: string; claim: string } | null {
  if (!now) return null;
  const asOf = now.as_of ?? analyzedAt ?? null;
  const age = ageMinutes(asOf, at);
  const current = age !== null && age <= RECENT_MINUTES;
  return { text: current ? `As of ${hm(asOf)}: ${now.text}` : `At ${hm(asOf)}: ${now.text}`, current, at: hm(asOf), claim: now.text };
}

export function WorkStateSummary({ view }: { view: WorkState | null }) {
  const fresh = freshnessLine(view);
  const now = nowLine(view?.state?.now, view?.state?.analyzed_at);
  const idle = view === null ? 'Syncing…' : view.state ? 'No recent activity observed' : 'No status yet';
  return <>
    <span className="module-primary">{now ? now.text : idle}</span>
    <span className={`module-caption ${fresh.stale ? 'needs-attention' : ''}`}>{fresh.text}</span>
  </>;
}

function ClaimRow({ claim }: { claim: Claim }) {
  return <div className="work-claim">
    <span className={`work-basis is-${claim.basis}`}>{basisText[claim.basis]}</span>
    <div>{claim.text}{claim.progress ? <small>进展：{claim.progress}</small> : null}</div>
  </div>;
}

export function WorkStateDetail({ view, onRefresh, refreshing, notice }: { view: WorkState | null; onRefresh: () => void; refreshing: boolean; notice: string | null }) {
  const state = view?.state ?? null;
  const fresh = freshnessLine(view);
  const now = nowLine(state?.now, state?.analyzed_at);
  const busy = refreshing || view?.refreshing === true;
  return <div className="work-detail">
    {view === null ? <p className="quota-note">正在同步…</p> : state === null ? <p className="quota-note">还没有整理过状态<small>点右下角刷新，Jarvis 会读取最新的活动数据并分析。</small></p> : <>
      <section className="work-section"><h4>{now?.current ? '最近在做' : '最后一次观察到'}</h4>{state.now ? <ClaimRow claim={{ ...state.now, text: now?.text ?? state.now.text }}/> : <p className="work-empty">最近窗口内没有观察到活动，未知。</p>}</section>
      <section className="work-section"><h4>今天的主要活动（{state.observed_until ? `到 ${hm(state.observed_until)}` : '当天'}）</h4>{state.activities.length ? state.activities.map((claim, i) => <ClaimRow key={i} claim={claim}/>) : <p className="work-empty">没有足够的数据。</p>}</section>
      {state.links.length > 0 && <section className="work-section"><h4>相关的待办与讨论</h4>{state.links.map((link, i) => <div className="work-claim" key={i}><span className={`work-basis is-${link.basis}`}>{link.kind === 'todo' ? '待办' : '讨论'}</span><div>{link.title || link.note}{link.title ? <small>{link.note}</small> : null}</div></div>)}</section>}
      {state.uncertainties.length > 0 && <section className="work-section"><h4>不确定 / 材料范围</h4><ul className="work-uncertain">{state.uncertainties.map((u, i) => <li key={i}>{u}</li>)}</ul></section>}
    </>}
    <div className="quota-foot">
      <span className={`quota-status ${busy ? 'is-muted' : notice?.startsWith('未更新') || fresh.stale ? 'is-warn' : 'is-ok'}`}><i/>{busy ? '更新中，保留上次结果' : notice ?? (view?.outcome === 'failed' ? `未更新：${view.error ?? '分析失败'}` : view?.outcome === 'no_evidence' ? '没有可分析的新数据，保留上次结果' : fresh.text)}</span>
      <button className="quota-refresh" aria-label="刷新" title="刷新" disabled={busy} onClick={onRefresh}><ArrowsClockwise size={16} className={busy ? 'is-spinning' : ''}/></button>
    </div>
  </div>;
}

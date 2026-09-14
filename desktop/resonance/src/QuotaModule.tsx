import { useCallback, useEffect, useState } from 'react';
import { ArrowsClockwise, CaretDown, Info } from '@phosphor-icons/react';
import './quota-module.css';

// ADR-0018 D6: the 模型额度 module. The daemon owns every number here
// (`GET /inherent/usage`, `POST /inherent/usage/refresh`); this file owns
// presentation only. Without a `port` (design lab) it shows demo data.

export type UsageWindow = { key: string; label: string; percent: number; resets_at: string | null };
export type ServiceStatus = 'ok' | 'error' | 'unconfigured';
export type UsageService<T> = { status: ServiceStatus; error?: string | null; observed_at_ms?: number | null; data: Partial<T> };
export type ClaudeData = { plan: string; windows: UsageWindow[] };
export type CodexData = { plan: string; windows: UsageWindow[]; reset_credits: number };
export type SpendRow = { model: string; today_usd: number; month_usd: number };
export type KeyRow = { key_id: string; name: string; today_tokens: number; month_tokens: number };
export type OpenAIData = { today_usd: number; month_usd: number; by_model: SpendRow[]; by_key: KeyRow[] };
export type DeepSeekData = { balance: number; currency: string };
export type MiniMaxData = { anchor_usd: number; anchor_at: string | null; characters_since_anchor: number; usd_per_million_chars: number; estimate_usd: number };
export type Usage = { services: {
  claude?: UsageService<ClaudeData>; codex?: UsageService<CodexData>; openai?: UsageService<OpenAIData>;
  anthropic?: UsageService<Record<string, never>>; deepseek?: UsageService<DeepSeekData>; minimax?: UsageService<MiniMaxData>;
} };

const demoAt = Date.now();
export const demoUsage: Usage = { services: {
  claude: { status: 'ok', observed_at_ms: demoAt, data: { plan: '20X', windows: [
    { key: 'five_hour', label: '5 小时', percent: 38, resets_at: new Date(new Date().setHours(21, 0, 0, 0)).toISOString() },
    { key: 'seven_day', label: '7 天 · 总', percent: 62, resets_at: '2026-09-18T10:00:00' },
    { key: 'seven_day_fable', label: '7 天 · Fable', percent: 24, resets_at: '2026-09-18T10:00:00' },
  ] } },
  codex: { status: 'ok', observed_at_ms: demoAt - 60_000, data: { plan: 'Pro 5X', reset_credits: 0, windows: [
    { key: 'primary_window', label: '7 天', percent: 46, resets_at: '2026-09-19T08:00:00' },
  ] } },
  openai: { status: 'ok', observed_at_ms: demoAt, data: { today_usd: 0.84, month_usd: 12.6,
    by_model: [{ model: 'gpt-live-1', today_usd: 0.61, month_usd: 9.2 }, { model: 'gpt-5.4-mini', today_usd: 0.23, month_usd: 3.4 }],
    by_key: [{ key_id: 'k1', name: 'jarvis', today_tokens: 98_000, month_tokens: 1_240_000 }, { key_id: 'k2', name: 'typeless', today_tokens: 12_000, month_tokens: 310_000 }] } },
  deepseek: { status: 'ok', observed_at_ms: demoAt, data: { balance: 8.46, currency: 'USD' } },
  minimax: { status: 'ok', observed_at_ms: demoAt - 120_000, data: { anchor_usd: 17.82, anchor_at: '2026-09-13T20:24:00-07:00', characters_since_anchor: 41_000, usd_per_million_chars: 60, estimate_usd: 15.36 } },
} };

export function useUsage(port: string | null) {
  const [usage, setUsage] = useState<Usage | null>(port ? null : demoUsage);
  const [refreshing, setRefreshing] = useState(false);
  const load = useCallback(async () => {
    if (!port) return;
    try { const r = await fetch(`http://127.0.0.1:${port}/inherent/usage`); if (r.ok) setUsage(await r.json() as Usage); } catch { /* daemon away; keep the last answer */ }
  }, [port]);
  useEffect(() => {
    void load();
    if (!port) return;
    const id = setInterval(() => void load(), 60_000);
    return () => clearInterval(id);
  }, [load, port]);
  const refresh = useCallback(async () => {
    if (!port) return;
    setRefreshing(true);
    try { const r = await fetch(`http://127.0.0.1:${port}/inherent/usage/refresh`, { method: 'POST' }); if (r.ok) setUsage(await r.json() as Usage); } catch { /* same as load */ } finally { setRefreshing(false); }
  }, [port]);
  return { usage, refresh, refreshing };
}

const pad = (n: number) => String(n).padStart(2, '0');
const hm = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
export function fmtReset(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toDateString() === now.toDateString() ? `今日 ${hm(d)} 重置` : `${d.getMonth() + 1}月${d.getDate()}日 ${hm(d)} 重置`;
}
const fmtTime = (ms?: number | null) => (ms ? hm(new Date(ms)) : '—');
const fmtUsd = (n?: number) => (n === undefined ? '—' : `$${n.toFixed(2)}`);
const fmtTokens = (n?: number) => (n === undefined ? '—' : n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(0)}K` : String(n));
const tone = (s?: UsageService<unknown>) => (!s || s.status === 'unconfigured' ? 'muted' : s.status === 'ok' ? 'ok' : 'warn');
const statusText = (s?: UsageService<unknown>) => (!s || s.status === 'unconfigured' ? '未配置' : s.status === 'ok' ? `正常 · ${fmtTime(s.observed_at_ms)} 同步` : `异常 · ${fmtTime(s.observed_at_ms)}`);

const glyphs = {
  claude: <svg viewBox="0 0 24 24" aria-hidden="true"><g stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" fill="none">{[0, 45, 90, 135].map(a => <line key={a} x1="12" y1="3.5" x2="12" y2="20.5" transform={`rotate(${a} 12 12)`}/>)}</g></svg>,
  codex: <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20C4 11 10 5 20 4c-1 10-7 16-16 16zm0 0 9-9" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" fill="none"/></svg>,
  openai: <svg viewBox="0 0 24 24" aria-hidden="true"><g stroke="currentColor" strokeWidth="1.8" fill="none"><circle cx="12" cy="12" r="9"/><path d="M12 6.5l4.8 2.75v5.5L12 17.5l-4.8-2.75v-5.5z"/></g></svg>,
  anthropic: <svg viewBox="0 0 24 24" aria-hidden="true"><g stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"><path d="M3.5 19 9.5 5l6 14M6 14h7"/><path d="M15.5 5l5 14"/></g></svg>,
  deepseek: <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 13c3-6 9-8 18-6-2 2-3 4-3 7-3 1-6 1-9-1-2 1-4 1-6 0zm12-6 3-3" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" fill="none"/></svg>,
  minimax: <svg viewBox="0 0 24 24" aria-hidden="true"><g fill="currentColor">{[6, 12, 18, 12, 6].map((h, i) => <rect key={i} x={3 + i * 4} y={12 - h / 2} width="2.4" height={h} rx="1.2"/>)}</g></svg>,
};

function Head({ glyph, name, chip, service }: { glyph: keyof typeof glyphs; name: string; chip?: string; service?: UsageService<unknown> }) {
  return <div className="quota-head"><span className="quota-glyph">{glyphs[glyph]}</span><span className="quota-name">{name}</span>{chip && <span className="quota-chip">{chip}</span>}<span className={`quota-status is-${tone(service)}`} title={service?.error ?? undefined}><i/>{statusText(service)}</span></div>;
}

function Meter({ w }: { w: UsageWindow }) {
  const pct = Math.max(0, Math.min(100, w.percent));
  const severity = pct >= 90 ? 'is-critical' : pct >= 75 ? 'is-warning' : '';
  return <div className={`quota-meter ${severity}`}>
    <span className="quota-meter-label">{w.label}</span>
    <span className="quota-track" role="meter" aria-label={`${w.label}已用`} aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}><i style={{ width: `${pct}%` }}/></span>
    <span className="quota-meter-value">已用 {Math.round(pct)}%</span>
    <span className="quota-meter-reset">{fmtReset(w.resets_at)}</span>
  </div>;
}

function Subscriptions({ usage }: { usage: Usage }) {
  const { claude, codex } = usage.services;
  return <>
    <Head glyph="claude" name="Claude Max" chip={claude?.data.plan} service={claude}/>
    {claude?.status === 'ok' ? (claude.data.windows ?? []).map(w => <Meter key={w.key} w={w}/>) : <p className="quota-note">{claude?.error ?? '没有 Claude Code 登录态'}</p>}
    <div className="quota-divider"/>
    <Head glyph="codex" name="Codex" chip={codex?.data.plan} service={codex}/>
    {codex?.status === 'ok' ? (codex.data.windows ?? []).map(w => <Meter key={w.key} w={w}/>) : <p className="quota-note">{codex?.error ?? '没有 Codex 登录态'}</p>}
    {codex?.status === 'ok' && <div className="quota-row"><span>Reset credits<Info size={14} aria-label="OpenAI 临时发放的额度重置次数"/></span><strong>{codex.data.reset_credits ?? 0}</strong></div>}
  </>;
}

function Spend({ usage }: { usage: Usage }) {
  const { openai, anthropic } = usage.services;
  const [by, setBy] = useState<'model' | 'key'>('model');
  const d = openai?.data ?? {};
  return <>
    <Head glyph="openai" name="OpenAI (API)" service={openai}/>
    {openai?.status === 'ok' ? <>
      <div className="quota-stats"><div><small>今日</small><strong>{fmtUsd(d.today_usd)}</strong></div><div><small>本月至今</small><strong>{fmtUsd(d.month_usd)}</strong></div></div>
      <div className="quota-tabs is-sub" role="tablist"><button role="tab" aria-selected={by === 'model'} onClick={() => setBy('model')}>按模型</button><button role="tab" aria-selected={by === 'key'} onClick={() => setBy('key')}>按 Key</button></div>
      {by === 'model'
        ? <div className="quota-table"><span>模型</span><span>今日</span><span>本月</span>{(d.by_model ?? []).map(r => <div key={r.model}><span>{r.model}</span><span>{fmtUsd(r.today_usd)}</span><span>{fmtUsd(r.month_usd)}</span></div>)}</div>
        : <div className="quota-table"><span>Key</span><span>今日 tokens</span><span>本月</span>{(d.by_key ?? []).map(r => <div key={r.key_id}><span>{r.name}</span><span>{fmtTokens(r.today_tokens)}</span><span>{fmtTokens(r.month_tokens)}</span></div>)}</div>}
    </> : <p className="quota-note">{openai?.status === 'unconfigured' || !openai ? '未配置' : '同步异常'}<small>{openai?.error ?? '需要 Admin key'}</small></p>}
    <div className="quota-divider"/>
    <Head glyph="anthropic" name="Anthropic (API)" service={anthropic}/>
    <p className="quota-note">{anthropic?.status === 'ok' ? '已配置' : '未配置'}<small>{anthropic?.error ?? '需要 Admin key'}</small></p>
  </>;
}

function Balances({ usage }: { usage: Usage }) {
  const { deepseek, minimax } = usage.services;
  const [formula, setFormula] = useState(false);
  const m = minimax?.data ?? {};
  return <>
    <Head glyph="deepseek" name="DeepSeek" service={deepseek}/>
    {deepseek?.status === 'ok' ? <div className="quota-big"><strong>{fmtUsd(deepseek.data.balance)}</strong><small>官方余额</small></div> : <p className="quota-note">{deepseek?.error ?? '未配置'}</p>}
    <div className="quota-divider"/>
    <Head glyph="minimax" name="MiniMax" service={minimax}/>
    {minimax?.status === 'ok' ? <>
      <div className="quota-big"><strong>{fmtUsd(m.estimate_usd)}<span className="quota-chip">估算</span></strong><small>估算余额</small></div>
      <div className="quota-formula">
        <button aria-expanded={formula} onClick={() => setFormula(v => !v)}><CaretDown className={formula ? 'rotated' : ''}/>估算计算方式</button>
        {formula && <div className="quota-formula-box"><span>{fmtUsd(m.anchor_usd)} − 累计字符 × 单价</span><Info size={16} aria-label={`锚点之后 ${m.characters_since_anchor ?? 0} 字符，单价 $${m.usd_per_million_chars ?? 0} / 百万字符`}/></div>}
      </div>
    </> : <p className="quota-note">{minimax?.error ?? '未配置'}<small>在 config 里填写充值锚点</small></p>}
  </>;
}

export function QuotaDetail({ usage, onRefresh, refreshing }: { usage: Usage | null; onRefresh: () => void; refreshing: boolean }) {
  const [tab, setTab] = useState<'sub' | 'spend' | 'balance'>('sub');
  const tabs = [['sub', '订阅'], ['spend', '花费'], ['balance', '余额']] as const;
  const syncing = refreshing || usage === null;
  return <div className="quota-detail">
    <div className="quota-tabs" role="tablist">{tabs.map(([id, label]) => <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>{label}</button>)}</div>
    {usage === null ? <p className="quota-note">正在同步…</p> : tab === 'sub' ? <Subscriptions usage={usage}/> : tab === 'spend' ? <Spend usage={usage}/> : <Balances usage={usage}/>}
    <div className="quota-foot"><span className={`quota-status ${syncing ? 'is-muted' : 'is-ok'}`}><i/>{syncing ? '同步中' : '同步完成'}</span><button className="quota-refresh" aria-label="刷新" title="刷新" disabled={refreshing} onClick={onRefresh}><ArrowsClockwise size={16} className={refreshing ? 'is-spinning' : ''}/></button></div>
  </div>;
}

export function QuotaSummary({ usage }: { usage: Usage | null }) {
  const windows = [
    ...(usage?.services.claude?.data.windows ?? []).map(w => ({ ...w, service: 'Claude' })),
    ...(usage?.services.codex?.data.windows ?? []).map(w => ({ ...w, service: 'Codex' })),
  ].filter(w => w.key !== 'five_hour');
  const worst = windows.sort((a, b) => b.percent - a.percent)[0];
  const remaining = worst ? Math.max(0, Math.min(12, Math.round((100 - worst.percent) / 100 * 12))) : 0;
  const today = usage?.services.openai?.data.today_usd;
  return <>
    <span className="module-primary">{worst ? `${worst.service} ${worst.label} 已用 ${Math.round(worst.percent)}%` : usage ? '暂无订阅数据' : '正在同步…'}</span>
    <span className="quota-segments" aria-label={worst ? `${worst.service}剩余 ${100 - Math.round(worst.percent)}%` : undefined}>{Array.from({ length: 12 }, (_, i) => <i key={i} className={i < remaining ? 'available' : ''}/>)}</span>
    <span className="module-caption">{worst ? fmtReset(worst.resets_at) : '—'}{today !== undefined ? ` · 今日 API ${fmtUsd(today)}` : ''}</span>
  </>;
}

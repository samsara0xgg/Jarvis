import { useCallback, useEffect, useState } from 'react';

// ADR-0018 D6: the 模型额度 module. The daemon owns every number here
// (`GET /inherent/usage`, `POST /inherent/usage/refresh`); this file only
// fetches them. Without a `port` (the --demo companion) it shows demo data.

export type UsageWindow = { key: string; label: string; percent: number; resets_at: string | null };
export type ServiceStatus = 'ok' | 'error' | 'unconfigured';
export type UsageService<T> = { status: ServiceStatus; error?: string | null; observed_at_ms?: number | null; data: Partial<T> };
// Limit resets Claude grants (claude.ai's cedar_ember block): how many are left and the last day to use them.
export type ClaudeData = { plan: string; windows: UsageWindow[]; reset_credits?: number; reset_ends_at?: string | null };
export type CodexData = { plan: string; windows: UsageWindow[]; reset_credits: number };
export type SpendRow = { model: string; today_usd: number; month_usd: number };
export type KeyRow = { key_id: string; name: string; today_tokens: number; month_tokens: number };
// ADR 0050: OpenAI reports no balance; with one recorded on the Usage page, balance_usd = it minus the spend since.
export type OpenAIData = { today_usd: number; month_usd: number; by_model: SpendRow[]; by_key: KeyRow[];
  balance_usd?: number; balance_recorded_usd?: number; balance_recorded_at?: string | null };
// Anthropic's spend (ADR 0239): the brain's own recorded calls, and `official` for the whole account; unpriced_calls = calls whose model has no price.
export type AnthropicData = { today_usd: number; month_usd: number; by_model: SpendRow[]; unpriced_calls?: number;
  // Anthropic's own Cost API, when the key is allowed to read it; otherwise official_error says why.
  official?: { today_usd: number; month_usd: number; by_model: SpendRow[]; balance_usd?: number }; official_error?: string;
  balance_recorded_usd?: number; balance_recorded_at?: string | null };
// One balance per currency DeepSeek holds (a CNY top-up sits beside the USD one).
export type DeepSeekData = { balances: Record<string, number> };
export type MiniMaxData = { balance: number };
export type Usage = { services: {
  claude?: UsageService<ClaudeData>; codex?: UsageService<CodexData>; openai?: UsageService<OpenAIData>;
  anthropic?: UsageService<AnthropicData>; deepseek?: UsageService<DeepSeekData>; minimax?: UsageService<MiniMaxData>;
} };

const demoAt = Date.now();
export const demoUsage: Usage = { services: {
  claude: { status: 'ok', observed_at_ms: demoAt, data: { plan: '20X', reset_credits: 1, reset_ends_at: new Date(demoAt + 8 * 86_400_000).toISOString(), windows: [
    { key: 'five_hour', label: '5 hours', percent: 38, resets_at: new Date(demoAt + (3 * 60 + 42) * 60_000).toISOString() },
    { key: 'seven_day', label: '7 days', percent: 62, resets_at: new Date(demoAt + 31 * 3_600_000).toISOString() },
    { key: 'seven_day_fable', label: '7 days · Fable', percent: 24, resets_at: new Date(demoAt + 31 * 3_600_000).toISOString() },
  ] } },
  codex: { status: 'ok', observed_at_ms: demoAt - 60_000, data: { plan: 'Pro 5X', reset_credits: 0, windows: [
    { key: 'primary_window', label: '7 days', percent: 46, resets_at: new Date(demoAt + 47 * 3_600_000).toISOString() },
  ] } },
  openai: { status: 'ok', observed_at_ms: demoAt, data: { today_usd: 0.84, month_usd: 12.6,
    by_model: [{ model: 'gpt-live-1', today_usd: 0.61, month_usd: 9.2 }, { model: 'gpt-5.4-mini', today_usd: 0.23, month_usd: 3.4 }],
    by_key: [{ key_id: 'k1', name: 'jarvis', today_tokens: 98_000, month_tokens: 1_240_000 }, { key_id: 'k2', name: 'typeless', today_tokens: 12_000, month_tokens: 310_000 }] } },
  anthropic: { status: 'ok', observed_at_ms: demoAt, data: { today_usd: 0.31, month_usd: 4.2,
    by_model: [{ model: 'claude-haiku-5-5', today_usd: 0.31, month_usd: 4.2 }],
    official: { today_usd: 1.12, month_usd: 25.4, balance_usd: 179.4, by_model: [{ model: 'claude-sonnet-5-5', today_usd: 0.81, month_usd: 21.2 }, { model: 'claude-haiku-5-5', today_usd: 0.31, month_usd: 4.2 }] },
    balance_recorded_usd: 179.63, balance_recorded_at: new Date(demoAt - 86_400_000).toISOString() } },
  deepseek: { status: 'ok', observed_at_ms: demoAt, data: { balances: { USD: 8.46 } } },
  minimax: { status: 'ok', observed_at_ms: demoAt - 120_000, data: { balance: 15.36 } },
} };

export function useUsage(port: string | null) {
  const [usage, setUsage] = useState<Usage | null>(port ? null : demoUsage);
  const [refreshing, setRefreshing] = useState(false);
  const load = useCallback(async () => {
    if (!port) return;
    // 404: the daemon has no usage observer (observer.usage is off), so no service is set up; that is an answer, not a wait.
    try { const r = await fetch(`http://127.0.0.1:${port}/inherent/usage`); if (r.ok) setUsage(await r.json() as Usage); else if (r.status === 404) setUsage({ services: {} }); } catch { /* daemon away; keep the last answer */ }
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

// ponytail: a fixed rate (DeepSeek's own CNY/USD price ratio); fetch a live one if the gap ever matters.
const CNY_PER_USD = 7.1;
// One account spends both rows: their sum in dollars, "≈" once a yuan row was converted, debt signed.
export const balanceTotal = (b: Record<string, number> = {}) => {
  const rows = Object.entries(b);
  if (!rows.length) return '—';
  const total = rows.reduce((sum, [cur, n]) => sum + (cur === 'CNY' ? n / CNY_PER_USD : n), 0);
  return `${rows.some(([cur]) => cur === 'CNY') ? '≈ ' : ''}${total <= -.005 ? '-' : ''}$${Math.abs(total).toFixed(2)}`;
};

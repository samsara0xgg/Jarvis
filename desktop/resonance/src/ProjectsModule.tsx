import { useCallback, useEffect, useRef, useState } from 'react';
import { useT, type L, type Lang } from './companionSettings';

// ADR 0037: the 项目 module. The daemon owns the view (`GET /inherent/projects`, never a model
// call) and sorts only new activities on `POST /inherent/projects/refresh`, which this module
// asks for each time the dashboard shows it. Without a `port` (the --demo companion) it shows demo data.

export type Commit = { sha: string; subject: string; committed_at: string | null; on_main: boolean | null; repo: string };
export type Recent = { app: string; label: string; seconds: number; last_seen: string | null };
export type ProjectRow = { id: string; name: string; seconds: number; today_seconds: number; days: number[]; last_seen: string | null; commits: { count: number; items: Commit[] }; recent: Recent[] };
export type Totals = { seconds: number; days: number[]; count: number };
export type ProjectsView = {
  days: string[]; projects: ProjectRow[]; other: Totals; unsorted: Totals;
  coverage: { timesink: string; git: string }; latest_observed_at: string | null; sorted_at: string | null;
  refreshing: boolean; outcome: 'classified' | 'reused' | 'no_evidence' | 'failed' | null; error: string | null;
};

const demoNow = Date.now();
const demoIso = (hoursAgo: number) => new Date(demoNow - hoursAgo * 3_600_000).toISOString();
const demoDays = Array.from({ length: 7 }, (_, i) => new Date(demoNow - (6 - i) * 86_400_000).toISOString().slice(0, 10));
export const demoProjects: ProjectsView = {
  days: demoDays,
  projects: [
    { id: 'typlus', name: 'Typlus', seconds: 64_800, today_seconds: 7_200, days: [3_600, 10_800, 14_400, 9_000, 12_600, 7_200, 7_200], last_seen: demoIso(0.5), commits: { count: 0, items: [] }, recent: [
      { app: 'Xcode', label: 'Typlus overlay polish', seconds: 3_060, last_seen: demoIso(0.5) },
      { app: 'Google Chrome', label: 'Typlus release notes', seconds: 2_090, last_seen: demoIso(3) },
    ] },
    { id: 'jarvis', name: 'Jarvis', seconds: 41_400, today_seconds: 5_400, days: [7_200, 3_600, 9_000, 5_400, 7_200, 3_600, 5_400], last_seen: demoIso(1), commits: { count: 12, items: [
      { sha: '355fdb3', subject: 'feat(execution,decision,state,runtime): report reads To Do and calendar', committed_at: demoIso(2), on_main: true, repo: 'jarvis' },
      { sha: '188d4ea', subject: 'feat(execution,decision,state,runtime): Codex plugins, search, approval', committed_at: demoIso(26), on_main: true, repo: 'jarvis' },
    ] }, recent: [{ app: 'Ghostty', label: 'cc | jarvis full audit', seconds: 1_260, last_seen: demoIso(1) }] },
    { id: 'school', name: 'School', seconds: 0, today_seconds: 0, days: [0, 0, 0, 0, 0, 0, 0], last_seen: null, commits: { count: 0, items: [] }, recent: [] },
  ],
  other: { seconds: 21_600, days: [3_000, 3_000, 3_000, 3_000, 3_000, 3_000, 3_600], count: 140 },
  unsorted: { seconds: 900, days: [0, 0, 0, 0, 0, 0, 900], count: 6 },
  coverage: { timesink: 'available', git: 'available' }, latest_observed_at: demoIso(0.2), sorted_at: demoIso(0.3),
  refreshing: false, outcome: 'classified', error: null,
};

export function useProjects(port: string | null, active: boolean) {
  const [view, setView] = useState<ProjectsView | null>(port ? null : demoProjects);
  const [missing, setMissing] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [notice, setNotice] = useState<L | null>(null), t = useT();
  const inFlight = useRef(false);
  const load = useCallback(async () => {
    if (!port) return;
    try {
      const r = await fetch(`http://127.0.0.1:${port}/inherent/projects`);
      setMissing(r.status === 404);
      if (r.ok) setView(await r.json() as ProjectsView);
    } catch { /* daemon away; keep the last view */ }
  }, [port]);
  const refresh = useCallback(async () => {
    // A second ask while one runs is dropped here; the daemon joins concurrent asks anyway.
    if (!port || inFlight.current) return;
    inFlight.current = true; setRefreshing(true); setNotice(null);
    try {
      const r = await fetch(`http://127.0.0.1:${port}/inherent/projects/refresh`, { method: 'POST' });
      setMissing(r.status === 404);
      if (r.status === 404) return;
      if (!r.ok) { setNotice([`Couldn’t update: Jarvis returned ${r.status}`, `未更新：服务返回 ${r.status}`]); return; }
      const next = await r.json() as ProjectsView;
      setView(next);
      setNotice(next.outcome === 'failed' ? [`Sorting didn’t finish: ${next.error ?? 'the model call failed'}`, `归类没做完：${next.error ?? '模型调用失败'}`] : null);
    } catch { setNotice(['Couldn’t update: can’t reach Jarvis', '未更新：连不上 Jarvis']); } finally { setRefreshing(false); inFlight.current = false; }
  }, [port]);
  // Each time the dashboard shows it sorts what is new (ADR 0037); hidden, it stops polling.
  useEffect(() => {
    if (!active) return;
    void load(); void refresh();
    if (!port) return;
    const id = setInterval(() => void load(), 60_000);
    return () => clearInterval(id);
  }, [load, refresh, port, active]);
  return { view, missing, refresh, refreshing, notice: notice && t(notice) };
}

export const duration = (seconds: number, lang: Lang = 'en') => seconds < 3_600
  ? `${Math.max(1, Math.round(seconds / 60))} ${lang === 'zh' ? '分钟' : 'min'}` : `${(seconds / 3_600).toFixed(1)} ${lang === 'zh' ? '小时' : 'h'}`;

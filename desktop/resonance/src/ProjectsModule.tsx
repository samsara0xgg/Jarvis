import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowsClockwise } from '@phosphor-icons/react';
import './projects-module.css';

// ADR 0037: the 项目 module. The daemon owns the view (`GET /inherent/projects`, never a model
// call) and sorts only new activities on `POST /inherent/projects/refresh`, which this module
// asks for when it mounts and when its detail opens. Without a `port` (design lab) it shows demo data.

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
    { id: 'job-search', name: '求职', seconds: 64_800, today_seconds: 7_200, days: [3_600, 10_800, 14_400, 9_000, 12_600, 7_200, 7_200], last_seen: demoIso(0.5), commits: { count: 0, items: [] }, recent: [
      { app: 'ChatGPT', label: '分析公司背调岗位和JD', seconds: 3_060, last_seen: demoIso(0.5) },
      { app: 'Google Chrome', label: 'Yilun Shi Resume sku.docx', seconds: 2_090, last_seen: demoIso(3) },
    ] },
    { id: 'jarvis', name: 'Jarvis', seconds: 41_400, today_seconds: 5_400, days: [7_200, 3_600, 9_000, 5_400, 7_200, 3_600, 5_400], last_seen: demoIso(1), commits: { count: 12, items: [
      { sha: '355fdb3', subject: 'feat(execution,decision,state,runtime): report reads To Do and calendar', committed_at: demoIso(2), on_main: true, repo: 'jarvis' },
      { sha: '188d4ea', subject: 'feat(execution,decision,state,runtime): Codex plugins, search, approval', committed_at: demoIso(26), on_main: true, repo: 'jarvis' },
    ] }, recent: [{ app: 'Ghostty', label: 'cc | jarvis full audit', seconds: 1_260, last_seen: demoIso(1) }] },
    { id: 'school', name: '学业', seconds: 0, today_seconds: 0, days: [0, 0, 0, 0, 0, 0, 0], last_seen: null, commits: { count: 0, items: [] }, recent: [] },
  ],
  other: { seconds: 21_600, days: [3_000, 3_000, 3_000, 3_000, 3_000, 3_000, 3_600], count: 140 },
  unsorted: { seconds: 900, days: [0, 0, 0, 0, 0, 0, 900], count: 6 },
  coverage: { timesink: 'available', git: 'available' }, latest_observed_at: demoIso(0.2), sorted_at: demoIso(0.3),
  refreshing: false, outcome: 'classified', error: null,
};

export function useProjects(port: string | null) {
  const [view, setView] = useState<ProjectsView | null>(port ? null : demoProjects);
  const [missing, setMissing] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
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
      if (!r.ok) { setNotice(`未更新：服务返回 ${r.status}`); return; }
      const next = await r.json() as ProjectsView;
      setView(next);
      setNotice(next.outcome === 'failed' ? `归类没做完：${next.error ?? '模型调用失败'}` : null);
    } catch { setNotice('未更新：连不上 Jarvis'); } finally { setRefreshing(false); inFlight.current = false; }
  }, [port]);
  useEffect(() => {
    void load(); void refresh();
    if (!port) return;
    const id = setInterval(() => void load(), 60_000);
    return () => clearInterval(id);
  }, [load, refresh, port]);
  return { view, missing, refresh, refreshing, notice };
}

const pad = (n: number) => String(n).padStart(2, '0');
export const duration = (seconds: number) => seconds < 3_600 ? `${Math.max(1, Math.round(seconds / 60))} 分钟` : `${(seconds / 3_600).toFixed(1)} 小时`;
const clock = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
export function when(value: string | null, now = Date.now()): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '—';
  const days = Math.round((new Date(new Date(now).toDateString()).getTime() - new Date(d.toDateString()).getTime()) / 86_400_000);
  return days <= 0 ? `今天 ${clock(d)}` : days === 1 ? `昨天 ${clock(d)}` : `${days} 天前`;
}
const weekday = (iso: string) => '日一二三四五六'[new Date(`${iso}T12:00:00`).getDay()];

function statusLine(view: ProjectsView | null, refreshing: boolean, notice: string | null): { text: string; warn: boolean } {
  if (refreshing || view?.refreshing) return { text: '正在归类新活动，先显示上次结果', warn: false };
  if (notice) return { text: notice, warn: true };
  if (view?.coverage.timesink === 'unavailable') return { text: 'TimeSink 读不到，时间未知', warn: true };
  if (view && view.unsorted.seconds > 0) return { text: `还有 ${duration(view.unsorted.seconds)}没归类`, warn: true };
  return { text: view?.sorted_at ? `归类于 ${when(view.sorted_at)}` : '还没有归类过', warn: false };
}

export function ProjectsSummary({ view, missing, refreshing, notice }: { view: ProjectsView | null; missing: boolean; refreshing: boolean; notice: string | null }) {
  if (missing) return <><span className="module-primary">还没有配置项目</span><span className="module-caption">config/jarvis.yaml → projects</span></>;
  // The tile fits one line: the week's leading project. Every project is in the detail.
  const top = (view?.projects ?? []).find(p => p.seconds > 0);
  const status = statusLine(view, refreshing, notice);
  return <>
    <span className="module-primary projects-primary">{view === null ? '正在同步…' : top ? `${top.name} ${duration(top.seconds)}` : '还没有项目时间'}</span>
    <span className={`module-caption ${status.warn ? 'needs-attention' : ''}`}>近 7 天 · {status.text}</span>
  </>;
}

// One series per project: a column per local day, today in the accent, earlier days one step
// quieter; no value labels on the columns, the numbers are the text beside and each column's hover.
function DayColumns({ project, days }: { project: ProjectRow; days: string[] }) {
  const peak = Math.max(...project.days, 1);
  const spoken = days.map((d, i) => `${d.slice(5)} ${duration(project.days[i] ?? 0)}`).join('，');
  return <div className="project-days" role="img" aria-label={`${project.name}每天的时间：${spoken}`}>
    {days.map((d, i) => {
      const seconds = project.days[i] ?? 0;
      return <span key={d} className="project-day" title={`${d.slice(5)} 周${weekday(d)} · ${seconds ? duration(seconds) : '没有活动'}`}>
        <i className={i === days.length - 1 ? 'is-today' : ''} style={{ height: seconds ? `${Math.max(2, (seconds / peak) * 100)}%` : 0 }}/>
        <small>{weekday(d)}</small>
      </span>;
    })}
  </div>;
}

function ProjectCard({ project, days }: { project: ProjectRow; days: string[] }) {
  return <section className="project-card">
    <header className="project-head">
      <h4>{project.name}</h4>
      <span>{duration(project.seconds)}<small>今天 {project.today_seconds ? duration(project.today_seconds) : '没碰'} · 最近 {when(project.last_seen)}</small></span>
    </header>
    <DayColumns project={project} days={days}/>
    {project.recent.length > 0 && <ul className="project-list">{project.recent.slice(0, 4).map((r, i) => <li key={i}><span>{r.label}</span><small>{r.app} · {duration(r.seconds)} · {when(r.last_seen)}</small></li>)}</ul>}
    {project.commits.count > 0 && <ul className="project-list is-commits">
      <li className="project-list-title">{project.commits.count} 个提交</li>
      {project.commits.items.slice(0, 3).map(c => <li key={c.sha}><span>{c.subject}</span><small>{c.repo} · {when(c.committed_at)}{c.on_main === false ? ' · 未进 main' : ''}</small></li>)}
    </ul>}
  </section>;
}

export function ProjectsDetail({ view, missing, onRefresh, refreshing, notice }: { view: ProjectsView | null; missing: boolean; onRefresh: () => void; refreshing: boolean; notice: string | null }) {
  const status = statusLine(view, refreshing, notice);
  const busy = refreshing || view?.refreshing === true;
  const active = (view?.projects ?? []).filter(p => p.seconds > 0 || p.commits.count > 0);
  const idle = (view?.projects ?? []).filter(p => p.seconds === 0 && p.commits.count === 0);
  return <div className="projects-detail">
    {missing ? <p className="quota-note">还没有配置项目<small>在 config/jarvis.yaml 的 projects 里列出项目，Jarvis 只会把活动归到这些项目。</small></p>
      : view === null ? <p className="quota-note">正在同步…</p> : <>
        {active.map(p => <ProjectCard key={p.id} project={p} days={view.days}/>)}
        {idle.length > 0 && <p className="projects-idle">近 7 天没碰：{idle.map(p => p.name).join('、')}</p>}
        <p className="projects-idle">不属于任何项目 {duration(view.other.seconds)}{view.unsorted.seconds > 0 ? ` · 还没归类 ${duration(view.unsorted.seconds)}` : ''}</p>
      </>}
    <div className="quota-foot">
      <span className={`quota-status ${busy ? 'is-muted' : status.warn ? 'is-warn' : 'is-ok'}`}><i/>{status.text}</span>
      <button className="quota-refresh" aria-label="归类新活动" title="归类新活动" disabled={busy || missing} onClick={onRefresh}><ArrowsClockwise size={16} className={busy ? 'is-spinning' : ''}/></button>
    </div>
  </div>;
}

import { useEffect, useState } from 'react';

export type CodexState = 'idle' | 'running' | 'needs_input' | 'finished';
export type CodexSession = { session_id: string; state: CodexState; cwd: string; model: string; prompt: string; detail: string; last_message: string; since_ms: number; turn_started_ms?: number; title?: string };
type Saved = { rows: CodexSession[]; pins: string[]; archived: Record<string, string>; acknowledged: Record<string, string> };
const day = 86_400_000;
const demoAt = Date.now();
export const demoSessions: CodexSession[] = [
  ['Design the Resonance monitor module', 'running', 'Checking session state…'],
  ['Fix voice reconnect', 'running', 'Reading the reconnect logic…'],
  ['Evaluate the Jarvis minimal loop', 'finished', 'Summarized the loop and what is left.'],
  ['Clean up the install guide', 'finished', 'Install guide updated.'],
  ['Adjust the usage page', 'needs_input', 'Waiting for approval to run the build'],
  ['Investigate the signing failure', 'finished', 'Found the signing config problem.'],
  ['Check microphone settings', 'idle', ''],
  ['Check connection logs', 'finished', 'Log check complete.'],
  ['Update the shortcut guide', 'finished', 'Shortcut guide updated.'],
].map(([prompt, state, detail], index) => ({ session_id: `demo-${index + 1}`, state: state as CodexState, cwd: '/Projects/jarvis', model: '', prompt, detail, last_message: state === 'finished' ? detail : '', since_ms: demoAt - index * 60_000, turn_started_ms: demoAt - index * 60_000 }));
const empty: Saved = { rows: [], pins: [], archived: {}, acknowledged: {} };
const token = (row: CodexSession) => String(row.turn_started_ms ?? row.prompt);
const activityToken = (row: CodexSession) => `${token(row)}:${row.state}${row.state === 'needs_input' ? `:${row.since_ms}` : ''}`;
const active = (row: CodexSession) => row.state === 'running' || row.state === 'needs_input';
const stateText: Record<CodexState, string> = { idle: '空闲', running: 'Thinking…', needs_input: '等你处理', finished: '已完成' };
const valid = (row: unknown): row is CodexSession => {
  if (!row || typeof row !== 'object') return false;
  const r = row as CodexSession;
  return typeof r.session_id === 'string' && Object.hasOwn(stateText, r.state) && Number.isFinite(r.since_ms)
    && ['cwd', 'model', 'prompt', 'detail', 'last_message'].every(k => typeof (r as unknown as Record<string, unknown>)[k] === 'string');
};

export function useCodexSessions(port: string | null) {
  const key = `resonance-codex-board-v1-${port ?? 'demo'}`;
  const [saved, setSaved] = useState<Saved>(() => {
    try {
      const value = JSON.parse(localStorage.getItem(key) ?? 'null');
      if (value && Array.isArray(value.rows) && Array.isArray(value.pins) && value.archived && typeof value.archived === 'object')
        return { rows: value.rows.filter(valid), pins: value.pins.filter((x: unknown) => typeof x === 'string'), archived: value.archived, acknowledged: value.acknowledged && typeof value.acknowledged === 'object' ? value.acknowledged : {} };
    } catch { /* Damaged preferences must not prevent opening the dashboard. */ }
    return port ? empty : { ...empty, rows: demoSessions };
  });
  const [fresh, setFresh] = useState<Set<string>>(new Set(port ? [] : saved.rows.map(r => r.session_id)));
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const [undo, setUndo] = useState<{ id: string; value: string; pinned: boolean } | null>(null);
  useEffect(() => { try { localStorage.setItem(key, JSON.stringify(saved)); } catch { setError('本地存储不可用，固定与移除无法在重启后保留。'); } }, [key, saved]);
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 30_000); return () => clearInterval(id); }, []);
  useEffect(() => {
    if (!port) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const load = async () => {
      try {
        const response = await fetch(`http://127.0.0.1:${port}/inherent/codex-sessions`, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]) });
        if (!response.ok) throw new Error(response.status === 404 ? '当前 daemon 尚未提供 Codex 会话接口' : 'Codex 会话同步失败');
        const data = await response.json();
        if (!Array.isArray(data.sessions) || !data.sessions.every(valid)) throw new Error('Codex 会话数据格式异常');
        if (stopped) return;
        const incoming = data.sessions as CodexSession[];
        const titles = await window.jarvis?.codexTitles(incoming.map(r => r.session_id)).catch(() => ({} as Record<string, string>));
        if (stopped) return;
        incoming.forEach(r => { if (titles?.[r.session_id]) r.title = titles[r.session_id]; });
        setFresh(new Set(incoming.map(r => r.session_id))); setError(null);
        setSaved(previous => {
          const rows = new Map(previous.rows.map(r => [r.session_id, r]));
          incoming.forEach(r => rows.set(r.session_id, r));
          const live = new Set(incoming.filter(active).map(r => r.session_id));
          const kept = [...rows.values()].filter(r => previous.pins.includes(r.session_id) || live.has(r.session_id) || Date.now() - r.since_ms < day);
          const ids = new Set(kept.map(r => r.session_id));
          return { ...previous, rows: kept, archived: Object.fromEntries(Object.entries(previous.archived).filter(([id]) => ids.has(id))), acknowledged: Object.fromEntries(Object.entries(previous.acknowledged).filter(([id]) => ids.has(id))) };
        });
      } catch (e) {
        if (!stopped) { setFresh(new Set()); setError(e instanceof Error ? e.message : 'Codex 会话暂时无法同步'); }
      } finally { if (!stopped) timer = setTimeout(load, 2000); }
    };
    void load();
    return () => { stopped = true; clearTimeout(timer); controller.abort(); };
  }, [port]);
  const rows = saved.rows.filter(r => saved.archived[r.session_id] !== token(r)
    && (saved.pins.includes(r.session_id) || (fresh.has(r.session_id) && active(r)) || now - r.since_ms < day))
    .sort((a, b) => Number(saved.pins.includes(b.session_id)) - Number(saved.pins.includes(a.session_id))
      || (b.turn_started_ms ?? b.since_ms) - (a.turn_started_ms ?? a.since_ms));
  const pin = (id: string) => setSaved(p => ({ ...p, pins: p.pins.includes(id) ? p.pins.filter(x => x !== id) : [...p.pins, id] }));
  const acknowledge = (row: CodexSession) => setSaved(p => ({ ...p, acknowledged: { ...p.acknowledged, [row.session_id]: activityToken(row) } }));
  const attention = (row: CodexSession) => row.state !== 'idle'
    && (row.state === 'finished' || fresh.has(row.session_id))
    && saved.acknowledged[row.session_id] !== activityToken(row);
  const archive = (row: CodexSession) => {
    setUndo({ id: row.session_id, value: token(row), pinned: saved.pins.includes(row.session_id) });
    setSaved(p => ({ ...p, pins: p.pins.filter(id => id !== row.session_id), archived: { ...p.archived, [row.session_id]: token(row) } }));
  };
  const restore = () => {
    if (!undo) return;
    setSaved(p => {
      const archived = { ...p.archived }; if (archived[undo.id] === undo.value) delete archived[undo.id];
      return { ...p, archived, pins: undo.pinned && !p.pins.includes(undo.id) ? [...p.pins, undo.id] : p.pins };
    }); setUndo(null);
  };
  useEffect(() => { if (!undo) return; const timer = setTimeout(() => setUndo(null), 6000); return () => clearTimeout(timer); }, [undo]);
  return { rows, pins: saved.pins, fresh, error, pin, archive, undo, restore, acknowledge, attention, demo: port === null };
}

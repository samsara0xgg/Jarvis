import { useEffect, useState } from 'react';

// ADR 0019 step 4: the Codex 会话 module. The daemon owns every row
// (`GET /inherent/codex-sessions`, fed by the Codex hooks); this file owns
// presentation only. Without a `port` (design lab) it shows demo rows.

export type CodexState = 'idle' | 'running' | 'needs_input' | 'finished';
export type CodexSession = { session_id: string; state: CodexState; cwd: string; model: string; prompt: string; detail: string; last_message: string; since_ms: number };

const demoAt = Date.now();
export const demoSessions: CodexSession[] = [
  { session_id: 'demo-1', state: 'needs_input', cwd: '/Users/allen/Projects/jarvis', model: 'gpt-6-astra', prompt: '把 README 的安装步骤改成 uv', detail: 'shell rm -rf node_modules', last_message: '', since_ms: demoAt - 20_000 },
  { session_id: 'demo-2', state: 'running', cwd: '/Users/allen/Documents/Codex/paper', model: 'gpt-6-astra', prompt: '帮我润色第三节', detail: 'read_file', last_message: '', since_ms: demoAt - 90_000 },
  { session_id: 'demo-3', state: 'finished', cwd: '/Users/allen/Projects/typlus', model: 'gpt-6-astra', prompt: '为什么 notarize 失败', detail: '', last_message: '签名用的是过期的 Developer ID，换成新证书后重跑即可。', since_ms: demoAt - 600_000 },
];

// ponytail: 2 s polling, same shape as useUsage; move to a `codex` op on the ws if the lag shows.
export function useCodexSessions(port: string | null): CodexSession[] {
  const [sessions, setSessions] = useState<CodexSession[]>(port ? [] : demoSessions);
  useEffect(() => {
    if (!port) return;
    let stop = false;
    const load = async () => {
      try { const r = await fetch(`http://127.0.0.1:${port}/inherent/codex-sessions`); if (r.ok && !stop) setSessions(((await r.json()) as { sessions: CodexSession[] }).sessions); } catch { /* daemon away; keep the last answer */ }
    };
    void load();
    const id = setInterval(() => void load(), 2000);
    return () => { stop = true; clearInterval(id); };
  }, [port]);
  return sessions;
}

const stateText: Record<CodexState, string> = { idle: '空闲', running: '运行中', needs_input: '等你输入', finished: '已完成' };
const folder = (cwd: string) => cwd.split('/').filter(Boolean).pop() ?? cwd;
const ago = (ms: number, now = Date.now()) => { const s = Math.max(0, Math.round((now - ms) / 1000)); return s < 60 ? `${s} 秒前` : s < 3600 ? `${Math.round(s / 60)} 分钟前` : `${Math.round(s / 3600)} 小时前`; };

export function CodexSummary({ sessions }: { sessions: CodexSession[] }) {
  const waiting = sessions.filter(s => s.state === 'needs_input').length;
  const running = sessions.filter(s => s.state === 'running').length;
  const primary = waiting ? `${waiting} 个等你输入` : running ? `${running} 个运行中` : sessions.length ? '都已完成' : '没有会话';
  return <><span className={`module-primary ${waiting ? 'needs-attention' : ''}`}>{primary}</span><span className="module-caption">{sessions.length ? `${sessions.length} 个 Codex 会话` : 'Codex 空闲'}</span></>;
}

export function CodexDetail({ sessions }: { sessions: CodexSession[] }) {
  if (!sessions.length) return <p className="quota-note">没有活动的 Codex 会话。</p>;
  return <>{sessions.map(s => <div key={s.session_id} className="dashboard-list-row">
    <div>{s.prompt || folder(s.cwd)}<small>{folder(s.cwd)} · {ago(s.since_ms)}{s.state === 'needs_input' && s.detail ? ` · ${s.detail}` : ''}{s.state === 'finished' && s.last_message ? ` · ${s.last_message}` : ''}</small></div>
    <span className={`row-status ${s.state === 'needs_input' ? 'needs-attention' : ''}`}>{stateText[s.state]}</span>
  </div>)}</>;
}

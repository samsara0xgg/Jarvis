import { useEffect, useRef, useState, type PointerEvent } from 'react';
import { ArrowBendUpLeft, PushPin, Square, X } from '@phosphor-icons/react';
import './codex-module.css';

export type CodexState = 'idle' | 'running' | 'needs_input' | 'finished';
export type CodexSession = { session_id: string; state: CodexState; cwd: string; model: string; prompt: string; detail: string; last_message: string; since_ms: number; turn_started_ms?: number; title?: string };
type Saved = { rows: CodexSession[]; pins: string[]; archived: Record<string, string>; acknowledged: Record<string, string> };
const day = 86_400_000;
const demoAt = Date.now();
export const demoSessions: CodexSession[] = [
  ['设计 Resonance 监控模块', 'running', '正在检查会话状态…'],
  ['修复语音重连', 'running', '正在检查连接恢复逻辑…'],
  ['评估 Jarvis 最小闭环', 'finished', '已整理当前闭环与剩余事项。'],
  ['整理安装文档', 'finished', '安装说明已更新。'],
  ['调整额度页面', 'needs_input', '等待批准执行构建'],
  ['排查签名失败', 'finished', '已找到签名配置问题。'],
  ['检查麦克风设置', 'idle', ''],
  ['检查连接日志', 'finished', '日志检查完成。'],
  ['更新快捷键说明', 'finished', '快捷键说明已更新。'],
].map(([prompt, state, detail], index) => ({ session_id: `demo-${index + 1}`, state: state as CodexState, cwd: '/Projects/jarvis', model: '', prompt, detail, last_message: state === 'finished' ? detail : '', since_ms: demoAt - index * 60_000, turn_started_ms: demoAt - index * 60_000 }));
const empty: Saved = { rows: [], pins: [], archived: {}, acknowledged: {} };
const token = (row: CodexSession) => String(row.turn_started_ms ?? row.prompt);
const activityToken = (row: CodexSession) => `${token(row)}:${row.state}${row.state === 'needs_input' ? `:${row.since_ms}` : ''}`;
const active = (row: CodexSession) => row.state === 'running' || row.state === 'needs_input';
const title = (row: CodexSession) => row.title || row.prompt || row.cwd.split('/').filter(Boolean).pop() || 'Codex 会话';
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
type Board = ReturnType<typeof useCodexSessions>;

export function CodexSummary({ board }: { board: Board }) {
  const waiting = board.rows.filter(r => board.fresh.has(r.session_id) && r.state === 'needs_input').length;
  const running = board.rows.filter(r => board.fresh.has(r.session_id) && r.state === 'running').length;
  return <><span className={`module-primary ${waiting ? 'needs-attention' : ''}`}>{board.error ? '暂时无法同步' : waiting ? `${waiting} 个等你处理` : running ? `${running} 个运行中` : board.rows.length ? `${board.rows.length} 个最近会话` : '没有最近会话'}</span><span className="module-caption">{board.rows[0] ? title(board.rows[0]) : '开始工作后自动出现'}</span></>;
}

export function CodexDetail({ board }: { board: Board }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const [holding, setHolding] = useState<string | null>(null);
  const [replying, setReplying] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const replyInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!replying) return;
    let stopped = false;
    void (async () => {
      await window.jarvis?.focus(true);
      if (!stopped) replyInput.current?.focus({ preventScroll: true });
    })();
    return () => { stopped = true; };
  }, [replying]);
  const gesture = useRef<{ id: string; x: number; y: number; timer: ReturnType<typeof setTimeout>; cancelled: boolean; fired: boolean } | null>(null);
  const cancel = () => { if (gesture.current) { clearTimeout(gesture.current.timer); gesture.current.cancelled = true; } setHolding(null); };
  useEffect(() => { window.addEventListener('blur', cancel); return () => { window.removeEventListener('blur', cancel); if (gesture.current) clearTimeout(gesture.current.timer); }; }, []);
  useEffect(() => { if (!notice) return; const timer = setTimeout(() => setNotice(''), 4500); return () => clearTimeout(timer); }, [notice]);
  const down = (event: PointerEvent<HTMLButtonElement>, id: string) => {
    if (event.button !== 0) return;
    cancel(); setHolding(id);
    const g = { id, x: event.clientX, y: event.clientY, cancelled: false, fired: false, timer: setTimeout(() => {
      g.fired = true; setHolding(null); board.pin(id);
      setNotice(board.pins.includes(id) ? '已取消固定' : '已固定');
    }, 1000) };
    gesture.current = g; event.currentTarget.setPointerCapture(event.pointerId);
  };
  const open = async (row: CodexSession) => {
    if (board.demo) { board.acknowledge(row); setNotice('演示会话，未打开 Codex。'); return; }
    try {
      if (!window.jarvis?.openCodex || !await window.jarvis.openCodex(row.session_id)) throw new Error();
      board.acknowledge(row);
      setNotice('已请求在 Codex 中打开');
    } catch { setNotice('无法打开 Codex，请确认客户端已安装。'); }
  };
  return <div className="codex-panel">
    {board.error && <div className="codex-sync-error" role="status">{board.error}；缓存状态仅供参考。</div>}
    <div className="codex-sessions" role="list" aria-label="最近 Codex 会话" onWheel={cancel} onScroll={cancel} onMouseLeave={() => setExpanded(null)}>
      {!board.rows.length && <p className="codex-empty">没有最近会话</p>}
      {board.rows.map(row => {
        const id = row.session_id, pinned = board.pins.includes(id), isReplying = replying === id, isOpen = expanded === id || isReplying || board.attention(row);
        const stale = !board.fresh.has(id) && active(row);
        const progress = row.state === 'running' && /^[\w.:-]+$/.test(row.detail) ? `Thinking · 刚使用 ${row.detail}` : row.detail;
        const detail = stale ? '状态待同步' : row.state === 'finished' ? row.last_message || stateText.finished : progress || stateText[row.state];
        return <div role="listitem" className={`codex-row ${isOpen ? 'is-expanded' : ''} ${isReplying ? 'is-replying' : ''} ${pinned ? 'is-pinned' : ''} ${holding === id ? 'is-holding' : ''}`} key={id} data-session={id}
          onMouseEnter={() => setExpanded(id)} onPointerMove={e => { if (e.pointerType === 'mouse') setExpanded(id); }} onFocus={() => setExpanded(id)} onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget)) setExpanded(current => current === id ? null : current); }}>
          <div className="codex-capsule">
            <button className="codex-open" aria-label={`打开 ${title(row)}`} title="点击打开 Codex；长按一秒固定；P 键固定" onPointerDown={e => down(e, id)}
              onPointerMove={e => { const g = gesture.current; if (g && Math.hypot(e.clientX - g.x, e.clientY - g.y) > 5) cancel(); }}
              onPointerUp={() => { if (gesture.current) clearTimeout(gesture.current.timer); setHolding(null); }} onPointerCancel={cancel}
              onLostPointerCapture={() => { if (gesture.current) clearTimeout(gesture.current.timer); setHolding(null); }}
              onKeyDown={e => { if (e.key.toLowerCase() === 'p' && !e.repeat) { e.preventDefault(); board.pin(id); } }}
              onClick={e => { const g = gesture.current; gesture.current = null; if (e.detail && g && (g.fired || g.cancelled)) return; void open(row); }}>
              <span className="codex-title">{pinned && <PushPin weight="fill" aria-label="已固定"/>}{title(row)}</span>
              <span className="codex-detail">{detail}</span>
            </button>
            <span className={`codex-status ${stale ? 'unknown' : row.state}`} aria-label={stale ? '状态待同步' : stateText[row.state]}/>
            <div className="codex-actions" inert={!isOpen}>
              <button aria-label={`回复 ${title(row)}`} aria-pressed={isReplying} title="展开回复框" onClick={() => { cancel(); board.acknowledge(row); setReplying(isReplying ? null : id); }}><ArrowBendUpLeft/></button>
              {active(row) && <button aria-label={`打断 ${title(row)}（暂不可用）`} aria-disabled="true" title="桌面 Codex 尚无可用的外部打断接口，请回到会话停止" onClick={() => setNotice('请点击任务回到 Codex 停止本轮；Jarvis 尚不能直接打断桌面会话。')}><Square weight="fill"/></button>}
            </div>
            {isReplying && <form className="codex-reply" onSubmit={e => {
              e.preventDefault();
              if (drafts[id]?.trim()) setNotice('草稿已保留；桌面 Codex 的直接发送通道尚未接通。');
            }}>
              <input ref={replyInput} aria-label={`回复 ${title(row)} 的内容`} placeholder="Follow up" value={drafts[id] ?? ''}
                title="草稿；直接发送暂不可用" onChange={e => setDrafts(p => ({ ...p, [id]: e.target.value }))}
                onKeyDown={e => {
                  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); setReplying(null); e.currentTarget.closest('.codex-row')?.querySelector<HTMLButtonElement>('.codex-open')?.focus(); }
                  if (e.key === 'Enter' && e.nativeEvent.isComposing) e.preventDefault();
                }}/>
              <span className="codex-reply-limit">暂未接通发送</span>
            </form>}
          </div>
          <button className="codex-archive" aria-label={`移除 ${title(row)}`} title="从 Jarvis 移除，不删除 Codex 对话" tabIndex={isOpen ? 0 : -1} onClick={() => { cancel(); if (isReplying) setReplying(null); board.archive(row); }}><X/></button>
        </div>;
      })}
    </div>
    {board.undo ? <div className="codex-toast" role="status">已移除 <button onClick={board.restore}>撤销</button></div> : notice && <div className="codex-toast" role="status" onClick={() => setNotice('')}>{notice}</div>}
  </div>;
}

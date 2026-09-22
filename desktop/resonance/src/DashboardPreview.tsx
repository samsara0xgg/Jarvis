import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { Database, Compass, ArrowDownLeft, ArrowUp, ArrowUpRight, CaretDown, ChatCircle, GitBranch, IconContext } from '@phosphor-icons/react';
import { PresentationCapsule } from './PresentationCapsule';
import { MotionPreview } from './MotionPreview';
import type { Presence } from './VoicePresence';
import { useDashboardMotion } from './useDashboardMotion';
import { playFeedback, stopFeedback, warmFeedback, type FeedbackCue } from './feedback';
import { usePreferences } from './preferences';
import { QuotaDetail, QuotaSummary, useUsage } from './QuotaModule';
import { CodexDetail, CodexSummary, useCodexSessions } from './CodexModule';
import { WorkStateDetail, WorkStateSummary, useWorkState } from './WorkStateModule';
import './dashboard-preview.css';

const modules = [
  { name: '对话', Icon: ChatCircle }, { name: 'Codex', Icon: GitBranch },
  { name: '模型额度', Icon: Database }, { name: '当前状态', Icon: Compass },
];
const accent = '#abbce6';

export function PreviewLab({ initialDashboard = false }: { initialDashboard?: boolean }) {
  const [dashboard, setDashboard] = useState(initialDashboard);
  return <div className="resonance-design-lab">
    <nav className="design-pages" aria-label="设计预览页面">
      <button aria-pressed={!dashboard} onClick={() => setDashboard(false)}>胶囊动效</button>
      <button aria-pressed={dashboard} onClick={() => setDashboard(true)}>Dashboard</button>
    </nav>
    {dashboard ? <DashboardPreview/> : <MotionPreview/>}
  </div>;
}

export function DashboardPreview({ standalone = false, embedded = false, port = null, onClose, notifications, visible = true }: { visible?: boolean; notifications?: ReactNode; standalone?: boolean; embedded?: boolean; port?: string | null; onClose?: () => void }) {
  const [preferences] = usePreferences();
  // ADR-0018: live quotas when Electron passes the daemon port; demo data in the lab.
  const quota = useUsage(port);
  const codex = useCodexSessions(port);
  // ADR 0023: the persisted work state; the same refresh the conversation tool runs.
  const work = useWorkState(port);
  const feedback = (cue: FeedbackCue) => { if (preferences.feedbackEnabled) void playFeedback(cue, preferences.feedbackVolume); };
  useEffect(() => { if (embedded) return; warmFeedback(); return stopFeedback; }, [embedded]);
  const [open, setOpen] = useState(true);
  const [selected, setSelected] = useState<number | null>(null);
  const wide = false;
  const [slow, setSlow] = useState(false);
  const [live, setLive] = useState(false);
  const [presence, setPresence] = useState<Presence>('standby');
  const [microphoneMuted, setMicrophoneMuted] = useState(false);
  const [speakerMuted, setSpeakerMuted] = useState(false);
  const [raw, setRaw] = useState(false);
  const [draft, setDraft] = useState('');
  const [message, setMessage] = useState('帮我看看今天还有什么需要处理。');
  const [answered, setAnswered] = useState(true);
  const [cancelled, setCancelled] = useState(false);
  const [demoPhase, setDemoPhase] = useState('等你开口');
  const [completed, setCompleted] = useState(false);
  const stage = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const returnFocus = useRef<number | null>(null);
  const returnGesture = useRef<{ id: number; y: number; distance: number } | null>(null);
  const suppressReturnClick = useRef(false);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useDashboardMotion(stage, { open: open && visible, selected, wide, slow, providerLayout: preferences.quotaLayout === 'provider' });
  useLayoutEffect(() => {
    if (!standalone || !stage.current) return;
    const surface = stage.current.querySelector<HTMLElement>('.dashboard-surface')!;
    const update = () => { const r = surface.getBoundingClientRect(); window.jarvis?.material([{ x:r.x, y:r.y, width:r.width, height:r.height, radius:26, opacity:1 }], preferences.glassStrength); };
    const observer = new ResizeObserver(update); observer.observe(surface); update();
    return () => observer.disconnect();
  }, [standalone, preferences.glassStrength]);
  const cancelDemo = () => { timers.current.forEach(clearTimeout); timers.current = []; setCancelled(!answered); };
  useEffect(() => () => timers.current.forEach(clearTimeout), []);
  const expand = (index: number) => { setSelected(index); setOpen(true); };
  const back = () => { returnFocus.current = selected; setSelected(null); };
  useEffect(() => {
    if (selected !== null && open) stage.current?.querySelector<HTMLButtonElement>('.dashboard-home-bar')?.focus({ preventScroll: true });
    else if (returnFocus.current !== null) {
      stage.current?.querySelector<HTMLButtonElement>(`[data-module="${returnFocus.current}"] .module-summary`)?.focus({ preventScroll: true });
      returnFocus.current = null;
    }
  }, [selected, open]);
  const hide = () => {
    if (embedded) { onClose?.(); return; }
    if (standalone) { window.close(); return; }
    setOpen(false);
    stage.current?.querySelector<HTMLButtonElement>('.presentation-wing button')?.focus({ preventScroll: true });
  };
  const simulate = (text = '帮我看看今天还有什么需要处理。') => {
    cancelDemo(); setCancelled(false); setMessage(text); setAnswered(false); setOpen(true); setLive(true);
    setPresence('listening'); setDemoPhase('正在听');
    timers.current.push(setTimeout(() => { setPresence('thinking'); setDemoPhase('正在整理'); }, 900));
    timers.current.push(setTimeout(() => { setAnswered(true); setPresence('speaking'); setDemoPhase('已回复'); }, 2300));
    timers.current.push(setTimeout(() => { setPresence('standby'); setDemoPhase('等你开口'); setLive(false); }, 5600));
  };
  const send = () => { if (!draft.trim()) return; simulate(draft.trim()); setDraft(''); setSelected(0); };
  const answer = completed ? '设计方向已经确认。下午的语音测试提醒，也已经排好了。' : '今天还有两件事，下午的提醒已经排好了。';
  return <IconContext.Provider value={{ size: 16, weight: 'regular' }}>
    <main style={{ '--theme-color': preferences.themeColor, '--glass-opacity': preferences.opacity, '--glass-strength': preferences.glassStrength } as React.CSSProperties} className={`dashboard-preview ${standalone || embedded ? 'compact-dashboard' : ''} ${embedded ? 'embedded-dashboard' : ''} ${selected === 2 ? 'quota-dashboard' : ''} ${selected === null ? 'dashboard-overview' : ''}`}>
      <header className="dashboard-intro"><span>RESONANCE / DASHBOARD</span><h1>需要时，靠近一点。</h1><p>四个模块，一个随对话展开的空间。</p></header>
      <div className="dashboard-stage" ref={stage} data-selected={selected ?? 'overview'} data-open={open} onKeyDown={event => {
        if (event.key === 'Escape') { event.stopPropagation(); if (selected !== null) back(); else hide(); }
      }}>
        <div className="dashboard-capsule">
          <PresentationCapsule presentation={live ? 'expanded' : 'collapsed'} presence={presence} color={accent}
            microphoneMuted={microphoneMuted} speakerMuted={speakerMuted}
            onMicrophoneToggle={() => { feedback(microphoneMuted ? 'mic-on' : 'mic-off'); setMicrophoneMuted(value => !value); }} onSpeakerToggle={() => { feedback(speakerMuted ? 'speaker-on' : 'speaker-off'); setSpeakerMuted(value => !value); }}
            onActivate={() => { feedback('voice-enter'); setLive(true); setOpen(true); }} onCollapse={() => { feedback('voice-exit'); cancelDemo(); setLive(false); setPresence('standby'); setDemoPhase('等你开口'); }}
            onCompose={() => { setOpen(value => !value); }} onNotifications={() => { setOpen(true); setSelected(1); }}/>
        </div>
        <section data-interactive className="dashboard-surface" inert={!open || !visible} aria-label="Resonance dashboard" aria-hidden={!open || !visible}>
          <div className="dashboard-board">
            {modules.map(({ name, Icon }, index) => <article key={name} className={`dashboard-module ${selected === index ? 'is-selected' : ''}`} data-module={index} inert={selected !== null && selected !== index} aria-hidden={selected !== null && selected !== index}>
              <button className="module-summary" inert={selected === index} aria-hidden={selected === index} aria-label={`展开${name}`} onClick={() => expand(index)}>
                {index !== 2 && <span className="module-label"><Icon/>{name}<ArrowUpRight className="module-expand"/></span>}
                {index === 0 && <><span className={`module-conversation ${answered ? '' : 'is-processing'}`}>{cancelled ? '对话已暂停，随时可以继续。' : answered ? answer : '正在整理今天的安排…'}</span><span className="module-caption">{cancelled ? '已停止 · 可重新开始' : answered ? '刚刚 · 1 次委派' : '模型 A · 处理中'}</span></>}
                {index === 1 && <CodexSummary board={codex}/>}
                {index === 2 && <QuotaSummary usage={quota.usage}/>}
                {index === 3 && <WorkStateSummary view={work.view}/>}
              </button>
              <div className="module-detail" inert={selected !== index} aria-hidden={selected !== index}>
                <div className="module-scroll">
                  {index === 0 && <><div className="transcript-label">你<span>14:32</span></div><p className="transcript-user">{message}</p><div className="transcript-label jarvis-label"><span className="jarvis-dot"/>Jarvis<span>刚刚</span></div><p className="transcript-answer" aria-live="polite">{cancelled ? '这次演示已停止，没有生成新的回复。' : answered ? `${answer} ${completed ? '' : '你可以先确认 Resonance dashboard 的方向，下午 4 点再查看语音连接测试结果。'}` : '正在整理今天的安排…'}</p><div className="delegation"><button className="delegation-toggle" aria-expanded={raw} aria-controls="delegate-original" onClick={() => setRaw(value => !value)}><GitBranch/><span>模型 A <small>· {cancelled ? '已停止' : answered ? '已完成' : '处理中'}</small></span><CaretDown className={raw ? 'rotated' : ''}/></button><div className={`delegation-reveal ${raw ? 'is-open' : ''}`} id="delegate-original" inert={!raw}><div><div className="delegate-output"><span>原始输出 · 示例</span><p>{cancelled ? '本次演示已停止，没有模型结果。' : answered ? '已检查当前待办：\n1. 确认 Resonance dashboard 设计方向，等待 Allen 选择。\n2. 今天 16:00 查看语音连接测试结果，提醒已安排。\n\n后台任务：整理两种 dashboard 布局。' : '等待模型返回…'}</p></div></div></div></div></>}
                  {index === 1 && <CodexDetail board={codex}/>}
                  {index === 2 && <QuotaDetail layout={preferences.quotaLayout} usage={quota.usage} onRefresh={() => void quota.refresh()} refreshing={quota.refreshing}/>}
                  {index === 3 && <WorkStateDetail view={work.view} onRefresh={work.refresh} refreshing={work.refreshing} notice={work.notice}/>}
                </div>
              </div>
            </article>)}
          </div>
          <button className="dashboard-home-bar" aria-label="返回主界面" title="点击或向下拖动返回主界面" inert={selected === null} aria-hidden={selected === null}
            onClick={() => { if (!suppressReturnClick.current) back(); suppressReturnClick.current = false; }}
            onPointerDown={event => {
              if (event.button !== 0) return;
              suppressReturnClick.current = false;
              returnGesture.current = { id: event.pointerId, y: event.clientY, distance: 0 };
              event.currentTarget.setPointerCapture(event.pointerId);
            }}
            onPointerMove={event => {
              const gesture = returnGesture.current;
              if (!gesture || gesture.id !== event.pointerId) return;
              gesture.distance = Math.max(0, event.clientY - gesture.y);
              event.currentTarget.style.setProperty('--return-drag', `${Math.min(8, gesture.distance / 5)}px`);
            }}
            onPointerUp={event => {
              const gesture = returnGesture.current;
              if (!gesture || gesture.id !== event.pointerId) return;
              suppressReturnClick.current = Math.abs(event.clientY - gesture.y) > 6;
              if (gesture.distance >= 36) back();
              returnGesture.current = null;
              event.currentTarget.style.setProperty('--return-drag', '0px');
              event.currentTarget.releasePointerCapture(event.pointerId);
            }}
            onPointerCancel={event => { returnGesture.current = null; suppressReturnClick.current = true; event.currentTarget.style.setProperty('--return-drag', '0px'); }}
            onLostPointerCapture={event => { returnGesture.current = null; event.currentTarget.style.setProperty('--return-drag', '0px'); }}><span/></button>
          <div className="dashboard-notification-reveal" inert={selected !== null || !notifications} aria-hidden={selected !== null || !notifications}><div className="dashboard-notifications">{notifications}</div></div>
          <div className="dashboard-status"><span>最近同步 · 刚刚</span><span aria-live="polite">{demoPhase}</span></div>
          <form className="dashboard-composer" onSubmit={event => { event.preventDefault(); send(); }}><input ref={input} aria-label="给 Jarvis 发消息" value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && event.nativeEvent.isComposing) event.preventDefault(); }} placeholder="和 Jarvis 说点什么…"/><button className="dashboard-send" type="submit" disabled={!draft.trim()} aria-label="发送演示消息"><ArrowUp/></button></form>
        </section>
      </div>
      <footer className="dashboard-lab-controls"><div><button onClick={() => simulate()}>模拟一轮对话<ArrowUpRight size={13}/></button><button onClick={() => { cancelDemo(); setLive(false); setPresence('standby'); setDemoPhase('等你开口'); hide(); }}>待机<ArrowDownLeft size={13}/></button><button aria-pressed={slow} onClick={() => setSlow(value => !value)}>慢放</button></div><p>设计预览 · 演示数据与本地交互<span>主色 #ABBCE6</span></p></footer>
    </main>
  </IconContext.Provider>;
}

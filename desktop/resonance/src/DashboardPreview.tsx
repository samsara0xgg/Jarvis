import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Database, Compass, ArrowDownLeft, ArrowLeft, ArrowUp, ArrowUpRight, ArrowsInSimple, ArrowsOutSimple, CaretDown, ChatCircle, GitBranch, IconContext, Minus } from '@phosphor-icons/react';
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

export function DashboardPreview({ standalone = false, embedded = false, port = null, onClose }: { standalone?: boolean; embedded?: boolean; port?: string | null; onClose?: () => void }) {
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
  const [wide, setWide] = useState(false);
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
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useDashboardMotion(stage, { open, selected, wide, slow });
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
    if (selected !== null && open) stage.current?.querySelector<HTMLButtonElement>('.dashboard-back')?.focus({ preventScroll: true });
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
    <main className={`dashboard-preview ${standalone || embedded ? 'compact-dashboard' : ''} ${embedded ? 'embedded-dashboard' : ''}`}>
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
        <section data-interactive className="dashboard-surface" inert={!open} aria-label="Resonance dashboard" aria-hidden={!open}>
          <div className="dashboard-heading">
            <div className="dashboard-wordmark">resonance<span>{selected === null ? '此刻' : modules[selected].name}</span></div>
            <div className="dashboard-window-actions">
              {selected !== null && <button className="dashboard-icon dashboard-back" aria-label="返回四个模块" onClick={back}><ArrowLeft/></button>}
              <button className="dashboard-icon" aria-label={wide ? '缩小面板' : '放大面板'} aria-pressed={wide} onClick={() => setWide(value => !value)}>{wide ? <ArrowsInSimple/> : <ArrowsOutSimple/>}</button>
              <button className="dashboard-icon" aria-label="收回胶囊" onClick={hide}><Minus/></button>
            </div>
          </div>
          <div className="dashboard-board">
            {modules.map(({ name, Icon }, index) => <article key={name} className={`dashboard-module ${selected === index ? 'is-selected' : ''}`} data-module={index} inert={selected !== null && selected !== index} aria-hidden={selected !== null && selected !== index}>
              <button className="module-summary" inert={selected === index} aria-hidden={selected === index} aria-label={`展开${name}`} onClick={() => expand(index)}>
                <span className="module-label"><Icon/>{name}<ArrowUpRight className="module-expand"/></span>
                {index === 0 && <><span className={`module-conversation ${answered ? '' : 'is-processing'}`}>{cancelled ? '对话已暂停，随时可以继续。' : answered ? answer : '正在整理今天的安排…'}</span><span className="module-caption">{cancelled ? '已停止 · 可重新开始' : answered ? '刚刚 · 1 次委派' : '模型 A · 处理中'}</span></>}
                {index === 1 && <CodexSummary sessions={codex}/>}
                {index === 2 && <QuotaSummary usage={quota.usage}/>}
                {index === 3 && <WorkStateSummary view={work.view}/>}
              </button>
              <div className="module-detail" inert={selected !== index} aria-hidden={selected !== index}>
                <div className="detail-label"><Icon/>{name}<span>{index === 0 ? '当前对话' : '此刻'}</span></div>
                <div className="module-scroll">
                  {index === 0 && <><div className="transcript-label">你<span>14:32</span></div><p className="transcript-user">{message}</p><div className="transcript-label jarvis-label"><span className="jarvis-dot"/>Jarvis<span>刚刚</span></div><p className="transcript-answer" aria-live="polite">{cancelled ? '这次演示已停止，没有生成新的回复。' : answered ? `${answer} ${completed ? '' : '你可以先确认 Resonance dashboard 的方向，下午 4 点再查看语音连接测试结果。'}` : '正在整理今天的安排…'}</p><div className="delegation"><button className="delegation-toggle" aria-expanded={raw} aria-controls="delegate-original" onClick={() => setRaw(value => !value)}><GitBranch/><span>模型 A <small>· {cancelled ? '已停止' : answered ? '已完成' : '处理中'}</small></span><CaretDown className={raw ? 'rotated' : ''}/></button><div className={`delegation-reveal ${raw ? 'is-open' : ''}`} id="delegate-original" inert={!raw}><div><div className="delegate-output"><span>原始输出 · 示例</span><p>{cancelled ? '本次演示已停止，没有模型结果。' : answered ? '已检查当前待办：\n1. 确认 Resonance dashboard 设计方向，等待 Allen 选择。\n2. 今天 16:00 查看语音连接测试结果，提醒已安排。\n\n后台任务：整理两种 dashboard 布局。' : '等待模型返回…'}</p></div></div></div></div></>}
                  {index === 1 && <CodexDetail sessions={codex}/>}
                  {index === 2 && <QuotaDetail usage={quota.usage} onRefresh={() => void quota.refresh()} refreshing={quota.refreshing}/>}
                  {index === 3 && <WorkStateDetail view={work.view} onRefresh={work.refresh} refreshing={work.refreshing} notice={work.notice}/>}
                </div>
              </div>
            </article>)}
          </div>
          <div className="dashboard-status"><span>最近同步 · 刚刚</span><span aria-live="polite">{demoPhase}</span></div>
          <form className="dashboard-composer" onSubmit={event => { event.preventDefault(); send(); }}><input ref={input} aria-label="给 Jarvis 发消息" value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && event.nativeEvent.isComposing) event.preventDefault(); }} placeholder="和 Jarvis 说点什么…"/><button className="dashboard-send" type="submit" disabled={!draft.trim()} aria-label="发送演示消息"><ArrowUp/></button></form>
        </section>
      </div>
      <footer className="dashboard-lab-controls"><div><button onClick={() => simulate()}>模拟一轮对话<ArrowUpRight size={13}/></button><button onClick={() => { cancelDemo(); setLive(false); setPresence('standby'); setDemoPhase('等你开口'); hide(); }}>待机<ArrowDownLeft size={13}/></button><button aria-pressed={slow} onClick={() => setSlow(value => !value)}>慢放</button></div><p>设计预览 · 演示数据与本地交互<span>主色 #ABBCE6</span></p></footer>
    </main>
  </IconContext.Provider>;
}

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { Database, Compass, ArrowDownLeft, ArrowUpRight, CaretDown, ChatCircle, FolderSimple, GitBranch, Plugs, IconContext } from '@phosphor-icons/react';
import { PresentationCapsule } from './PresentationCapsule';
import { MotionPreview } from './MotionPreview';
import type { Presence } from './VoicePresence';
import { useDashboardMotion } from './useDashboardMotion';
import { playFeedback, stopFeedback, warmFeedback, type FeedbackCue } from './feedback';
import { usePreferences } from './preferences';
import { QuotaDetail, QuotaSummary, useUsage } from './QuotaModule';
import { CodexDetail, CodexSummary, useCodexSessions } from './CodexModule';
import { WorkStateDetail, WorkStateSummary, useWorkState } from './WorkStateModule';
import { DashboardComposer, type DashboardInput } from './DashboardComposer';
import { PluginPanel, type usePlugins } from './PluginPanel';
import { ProjectsDetail, ProjectsSummary, useProjects } from './ProjectsModule';
import './dashboard-preview.css';
import './dashboard-unified.css';

const modules = [
  { name: '对话', Icon: ChatCircle }, { name: 'Codex', Icon: GitBranch },
  { name: '模型额度', Icon: Database }, { name: '当前状态', Icon: Compass },
  { name: '插件', Icon: Plugs }, { name: '项目', Icon: FolderSimple },
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

export function DashboardPreview({ standalone = false, embedded = false, port = null, onClose, notifications, visible = true, composer, conversation, plugins }: {
  visible?: boolean; notifications?: ReactNode; standalone?: boolean; embedded?: boolean; port?: string | null; onClose?: () => void;
  composer?: DashboardInput; conversation?: { text: string; caption: string; pending: boolean };
  plugins?: { controller: ReturnType<typeof usePlugins>; presentation: string; open: number; onCatalog: () => void; onConversation: () => void };
}) {
  const [preferences] = usePreferences();
  // ADR-0018: live quotas when Electron passes the daemon port; demo data in the lab.
  const quota = useUsage(port);
  const codex = useCodexSessions(port);
  // ADR 0023: the persisted work state; the same refresh the conversation tool runs.
  const work = useWorkState(port);
  // ADR 0037: the project view; opening its detail sorts whatever activity is new.
  const projects = useProjects(port, visible);
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
  const returnFocus = useRef<number | null>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const scrollTop = useRef(0);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useDashboardMotion(stage, { open: open && visible, selected, wide, slow, providerLayout: preferences.quotaLayout === 'provider', scrollOffset: scrollTop.current });
  useLayoutEffect(() => {
    if (!standalone || !stage.current) return;
    const surface = stage.current.querySelector<HTMLElement>('.dashboard-surface')!;
    const update = () => { const r = surface.getBoundingClientRect(); window.jarvis?.material([{ x:r.x, y:r.y, width:r.width, height:r.height, radius:26, opacity:1 }], preferences.glassStrength); };
    const observer = new ResizeObserver(update); observer.observe(surface); update();
    return () => observer.disconnect();
  }, [standalone, preferences.glassStrength]);
  const cancelDemo = () => { timers.current.forEach(clearTimeout); timers.current = []; setCancelled(!answered); };
  useEffect(() => () => timers.current.forEach(clearTimeout), []);
  const expand = (index: number) => { if (selected === null) scrollTop.current = viewport.current?.scrollTop ?? 0; setSelected(index); setOpen(true); };
  const back = () => { returnFocus.current = selected; setSelected(null); };
  useEffect(() => { if (plugins?.open) expand(4); }, [plugins?.open]);
  useLayoutEffect(() => { if (viewport.current) viewport.current.scrollTop = scrollTop.current; }, [selected]);
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
  const send = () => { if (!draft.trim()) return; simulate(draft.trim()); setDraft(''); };
  useEffect(() => { if (selected === 5) void projects.refresh(); }, [selected, projects.refresh]);
  const answer = completed ? '设计方向已经确认。下午的语音测试提醒，也已经排好了。' : '今天还有两件事，下午的提醒已经排好了。';
  return <IconContext.Provider value={{ size: 16, weight: 'regular' }}>
    <main data-dashboard-style={preferences.dashboardStyle} style={{ '--theme-color': preferences.themeColor, '--glass-opacity': preferences.opacity, '--glass-strength': preferences.glassStrength } as React.CSSProperties} className={`dashboard-preview ${standalone || embedded ? 'compact-dashboard' : ''} ${embedded ? 'embedded-dashboard' : ''} ${selected === 2 ? 'quota-dashboard' : ''} ${selected === 4 ? 'plugins-dashboard' : ''} ${selected === null ? 'dashboard-overview' : ''}`}>
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
          <div className="dashboard-viewport" ref={viewport}>
          <div className="dashboard-board">
            {modules.map(({ name, Icon }, index) => index === 4 && !plugins ? null : <article key={name} className={`dashboard-module ${selected === index ? 'is-selected' : ''}`} data-module={index} inert={selected !== null && selected !== index} aria-hidden={selected !== null && selected !== index}>
              <button className="module-summary" inert={selected === index} aria-hidden={selected === index} aria-label={index === 4 ? '打开插件列表' : `展开${name}`} onClick={() => index === 4 ? plugins?.onCatalog() : expand(index)}>
                {index !== 2 && <span className="module-label"><Icon/>{name}<ArrowUpRight className="module-expand"/></span>}
                {index === 0 && <><span className={`module-conversation ${(conversation?.pending ?? !answered) ? 'is-processing' : ''}`}>{conversation ? conversation.text : cancelled ? '对话已暂停，随时可以继续。' : answered ? answer : '正在整理今天的安排…'}</span><span className="module-caption">{conversation ? conversation.caption : cancelled ? '已停止 · 可重新开始' : answered ? '刚刚 · 示例' : '模型 A · 处理中'}</span></>}
                {index === 1 && <CodexSummary board={codex}/>}
                {index === 2 && <QuotaSummary usage={quota.usage} grid/>}
                {index === 3 && <WorkStateSummary view={work.view}/>}
                {index === 5 && <ProjectsSummary view={projects.view} missing={projects.missing} refreshing={projects.refreshing} notice={projects.notice}/>}
                {index === 4 && <><span className="module-primary">{plugins!.controller.snapshot?.plugins.filter(plugin => plugin.status === 'ready').length ?? 0} 个已连接</span><span className="module-caption">查看与管理插件</span></>}
              </button>
              <div className="module-detail" inert={selected !== index} aria-hidden={selected !== index}>
                <div className="module-scroll">
                  {index === 0 && <><div className="transcript-label">你<span>14:32</span></div><p className="transcript-user">{message}</p><div className="transcript-label jarvis-label"><span className="jarvis-dot"/>Jarvis<span>刚刚</span></div><p className="transcript-answer" aria-live="polite">{cancelled ? '这次演示已停止，没有生成新的回复。' : answered ? `${answer} ${completed ? '' : '你可以先确认 Resonance dashboard 的方向，下午 4 点再查看语音连接测试结果。'}` : '正在整理今天的安排…'}</p><div className="delegation"><button className="delegation-toggle" aria-expanded={raw} aria-controls="delegate-original" onClick={() => setRaw(value => !value)}><GitBranch/><span>模型 A <small>· {cancelled ? '已停止' : answered ? '已完成' : '处理中'}</small></span><CaretDown className={raw ? 'rotated' : ''}/></button><div className={`delegation-reveal ${raw ? 'is-open' : ''}`} id="delegate-original" inert={!raw}><div><div className="delegate-output"><span>原始输出 · 示例</span><p>{cancelled ? '本次演示已停止，没有模型结果。' : answered ? '已检查当前待办：\n1. 确认 Resonance dashboard 设计方向，等待 Allen 选择。\n2. 今天 16:00 查看语音连接测试结果，提醒已安排。\n\n后台任务：整理两种 dashboard 布局。' : '等待模型返回…'}</p></div></div></div></div></>}
                  {index === 1 && <CodexDetail board={codex}/>}
                  {index === 2 && <QuotaDetail layout={preferences.quotaLayout} usage={quota.usage} onRefresh={() => void quota.refresh()} refreshing={quota.refreshing}/>}
                  {index === 3 && <WorkStateDetail view={work.view} onRefresh={work.refresh} refreshing={work.refreshing} notice={work.notice}/>}
                  {index === 5 && <ProjectsDetail view={projects.view} missing={projects.missing} onRefresh={() => void projects.refresh()} refreshing={projects.refreshing} notice={projects.notice}/>}
                  {index === 4 && <PluginPanel controller={plugins!.controller} presentation={plugins!.presentation} active={selected === 4 && visible}
                    onCatalog={plugins!.onCatalog} onHide={back} onConversation={plugins!.onConversation}/>}
                </div>
              </div>
            </article>)}
          </div>
          <div className="dashboard-notification-reveal" inert={selected !== null || !notifications} aria-hidden={selected !== null || !notifications}><div className="dashboard-notifications">{notifications}</div></div>
          </div>
          <button className="dashboard-home-bar" aria-label="返回主界面" title="点击返回主界面" inert={selected === null} aria-hidden={selected === null} onClick={back}><span/></button>
          <DashboardComposer active={selected === null && open && visible} {...(composer ?? { value: draft, onChange: setDraft, onSend: send, busy: !answered })}/>
        </section>
      </div>
      <footer className="dashboard-lab-controls"><div><button onClick={() => simulate()}>模拟一轮对话<ArrowUpRight size={13}/></button><button onClick={() => { cancelDemo(); setLive(false); setPresence('standby'); setDemoPhase('等你开口'); hide(); }}>待机<ArrowDownLeft size={13}/></button><button aria-pressed={slow} onClick={() => setSlow(value => !value)}>慢放</button></div><p>设计预览 · 演示数据与本地交互<span>主色 #ABBCE6</span></p></footer>
    </main>
  </IconContext.Provider>;
}

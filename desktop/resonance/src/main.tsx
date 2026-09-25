import React, { useEffect, useLayoutEffect, useReducer, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { IconContext, SquaresFour, Bell, X, ArrowUpRight, Copy, Check, ArrowCounterClockwise, Pause, DotsThree, GearSix, Phone, PhoneDisconnect } from '@phosphor-icons/react';
import { initialState, reducer, examples, type Phase } from './model';
import './style.css';
import { activeSurfaceTheme, surfaceThemes } from './surface-themes';
import { PanelStack, type PanelId } from './PanelStack';
import './panel-stack.css';
import './notch.css';
import { useCapsuleDrag } from './useCapsuleDrag';
import { useIslandHover } from './useIslandHover';
import { presenceLabels, type Presence } from './VoicePresence';
import { PreviewLab, DashboardPreview } from './DashboardPreview';
import { LiveTranscript, TranscriptTrigger } from './LiveTranscript';
import { PresentationCapsule } from './PresentationCapsule';
import { CapsuleIcon } from './CapsuleIcon';
import { playFeedback, stopFeedback, warmFeedback, type FeedbackCue } from './feedback';
import { defaultPreferences, usePreferences } from './preferences';
import { clipStackGlass, type GlassOcclusion } from './stackGlass';
import { connect, type Runtime } from './runtime';
import { usePlugins, type PluginSnapshot } from './PluginPanel';
import { WorkspacePreview } from './WorkspacePreview';
import { Companion } from './Companion';
type WindowPlacement = { docked: boolean; topInset: number; surfaceWidth: number; compactWidth: number; notchWidth: number; displayId?: number };
declare global { interface Window { jarvis?: {
  placement: () => Promise<WindowPlacement>;
  dock: (enabled: boolean) => Promise<WindowPlacement>;
  onPlacement: (cb: (value: WindowPlacement) => void) => () => void;
  onIslandHover: (cb: (inside: boolean) => void) => () => void;
  onCursor: (cb: (point: { x: number; y: number }) => void) => () => void;
  onDisplayLeave: (cb: () => void) => () => void;
  displayReady: () => void;
  drag: (phase: 'start' | 'move' | 'end', point?: { x: number; y: number }) => void;
  copy: (text: string) => Promise<boolean>;
  openCodex: (threadId: string) => Promise<boolean>;
  codexTitles: (ids: string[]) => Promise<Record<string, string>>;
  plugins: (operation: string, data?: Record<string, unknown>) => Promise<PluginSnapshot>;
  layout: (mode: string, height: number, surface?: { x: number; y: number; width: number; height: number }) => void; focus: (enabled: boolean) => Promise<void>; hide: () => void; passthrough: (enabled: boolean) => void;
  material: (rects: {x:number;y:number;width:number;height:number;radius:number;opacity:number;occlusion?:GlassOcclusion}[], strength: number) => void;
  onCommand: (cb: (value: string) => void) => () => void;
} } }
const lab = new URLSearchParams(location.search).has('lab');
// A `port` query means Electron wants the live daemon link; without it every timer below is the simulation.
const runtimePort = new URLSearchParams(location.search).get('port');
const live = runtimePort !== null;
// The render layer wraps speech in <voice> and card text in <document> (voice_tts.py:99). A document is the whole answer and the voice only its spoken form (ADR 0040), so once one arrives show it alone; drop the markup and any half-streamed tag.
const visible = (reply: string) => reply.slice(Math.max(0, reply.indexOf('<document>'))).replace(/<\/voice>/g, '\n').replace(/<\/?(voice|document)>/g, '').replace(/<\/?[a-z]*$/, '').trim();
const labels: Record<Phase, string> = { listening: '正在听取', hearing: '正在听', processing: '正在处理', speaking: '正在播报', error: '连接失败' };
const mmss = (sec: number) => `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
function Button({ label, children, className = '', ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return <button {...props} className={`icon-button ${className}`} aria-label={label} title={label}><span className="button-glyph" key={label}>{children}</span></button>;
}
function App() {
  const [placement, setPlacement] = useState<WindowPlacement>({ docked: false, topInset: 32, surfaceWidth: 540, compactWidth: 268, notchWidth: 180 });
  useEffect(() => {
    if (!window.jarvis || lab) return;
    let active = true;
    const receive = (value: WindowPlacement) => { if (active) setPlacement(value); };
    const unsubscribe = window.jarvis.onPlacement(receive);
    void window.jarvis.placement().then(receive);
    return () => { active = false; unsubscribe(); };
  }, []);
  const [s, dispatch] = useReducer(reducer, live ? { ...initialState, mode: 'idle', results: [] } : { ...initialState, mode: 'idle' });
  const [preferences, updatePreferences] = usePreferences();
  const { opacity, glassStrength, feedbackEnabled, feedbackVolume, themeColor } = preferences;
  const setOpacity = (value: number) => updatePreferences({ opacity: value });
  const feedback = (cue: FeedbackCue) => { if (feedbackEnabled) void playFeedback(cue, feedbackVolume); };
  useEffect(() => { warmFeedback(); return stopFeedback; }, []);
  const runtime = useRef<Runtime | null>(null);
  useEffect(() => {
    if (runtimePort === null) return;
    runtime.current = connect(runtimePort, dispatch);
    return () => { runtime.current?.close(); runtime.current = null; };
  }, []);
  const [background, setBackground] = useState('forest');
  const [scale, setScale] = useState(1.6);
  const [settings, setSettings] = useState(false);
  const [panels, setPanels] = useState<Record<PanelId, boolean>>({ composer: false, transcript: false, dashboard: false });
  const [collapsed, setCollapsed] = useState<Record<PanelId, boolean>>({ composer: false, transcript: false, dashboard: false });
  const panel = Object.values(panels).some(Boolean);
  const island = useIslandHover(placement.docked, () => {
    if (!panel && !settings) {
      setPanels(previous => ({ ...previous, dashboard: true }));
      if (!s.inbox) dispatch({ type: 'inbox' });
    }
  });
  const transcriptOpen = panels.transcript;
  const plugins = usePlugins();
  const [pluginPresentation, setPluginPresentation] = useState('catalog');
  const [pluginOpen, setPluginOpen] = useState(0);
  const lastPluginPresentation = useRef('');
  const pluginRequest = plugins.snapshot?.request;
  const setPanelOpen = (id: PanelId, open: boolean) => {
    setPanels(previous => ({ ...previous, [id]: open }));
    if (open) { if (placement.docked) island.reveal(); setCollapsed(previous => ({ ...previous, [id]: false })); void window.jarvis?.focus(true); }
  };
  const openDashboard = () => { setPanelOpen('dashboard', true); setSettings(false); if (!s.inbox) dispatch({ type: 'inbox' }); };
  const closePanel = () => { setPanelOpen('dashboard', false); if (s.inbox) dispatch({ type: 'inbox' }); };
  const openTranscript = () => { setPanelOpen('transcript', true); setSettings(false); };
  const closeTranscript = () => setPanelOpen('transcript', false);
  const openPlugins = () => { setPluginPresentation('catalog'); setPluginOpen(value => value + 1); openDashboard(); void plugins.refresh(); };
  useEffect(() => {
    if (!pluginRequest) return;
    const key = `${pluginRequest.id}:${pluginRequest.presentation}`;
    if (key !== lastPluginPresentation.current) {
      lastPluginPresentation.current = key; setPluginPresentation(key);
      setHidden(false); setPluginOpen(value => value + 1); openDashboard();
    }
  }, [pluginRequest?.id, pluginRequest?.presentation]);
  useEffect(() => { if (pluginRequest?.state === 'ready' && pluginRequest.resume_status === 'continued') setPanelOpen('transcript', true); }, [pluginRequest?.id, pluginRequest?.state, pluginRequest?.resume_status]);
  useEffect(() => { if (!panel && !settings) void window.jarvis?.focus(false); }, [panel, settings]);
  const [presencePreview, setPresencePreview] = useState<Presence | 'auto' | 'cycle'>('auto');
  const [cyclePhase, setCyclePhase] = useState<Presence>('standby');
  useEffect(() => {
    if (presencePreview !== 'cycle') return;
    setCyclePhase('standby');
    const steps: [number, Presence][] = [[2800, 'listening'], [6500, 'thinking'], [9700, 'speaking'], [14500, 'muted'], [17000, 'standby']];
    const timers = steps.map(([ms, phase]) => setTimeout(() => setCyclePhase(phase), ms));
    timers.push(setTimeout(() => setPresencePreview('auto'), 20000));
    return () => timers.forEach(clearTimeout);
  }, [presencePreview]);
  // Under GPT-Live, speaking is the local player draining and hearing is recent user transcript; both come from the daemon, never from subtitle timing.
  const presence: Presence = s.micMuted ? 'muted' : presencePreview === 'cycle' ? cyclePhase
    : presencePreview !== 'auto' ? presencePreview : s.live.state === 'active' ? (s.live.speaking ? 'speaking' : s.live.hearing ? 'listening' : 'standby')
    : s.phase === 'hearing' ? 'listening' : s.phase === 'processing' ? 'thinking'
    : s.phase === 'speaking' ? 'speaking' : s.phase === 'error' ? 'muted' : 'standby';
  // The session is billed per second, so the clock stays visible the whole time it is open.
  const [, tick] = useReducer((n: number) => n + 1, 0);
  useEffect(() => { if (s.live.state !== 'active') return; const t = setInterval(tick, 1000); return () => clearInterval(t); }, [s.live.state]);
  const liveClock = s.live.since !== null ? mmss(Math.max(0, Math.floor((Date.now() - s.live.since) / 1000))) : '';
  const liveBusy = s.live.state === 'connecting' || s.live.state === 'closing';
  const [copied, setCopied] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [retainInbox, setRetainInbox] = useState(false);
  const [inboxExpanded, setInboxExpanded] = useState(true);
  const [notificationScroll, setNotificationScroll] = useState(0);
  const remainingNotifications = Math.max(0, s.results.length - 2 - Math.floor(notificationScroll / 63));
  const [replying, setReplying] = useState<string | null>(null);
  const [followup, setFollowup] = useState('');
  const [followupMessage, setFollowupMessage] = useState('');
  const followupInput = useRef<HTMLTextAreaElement>(null);
  const followupTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stacked = false;
  const openFollowup = (id: string) => {
    if (followupTimer.current) clearTimeout(followupTimer.current);
    setReplying(id); setFollowup(''); setFollowupMessage(''); setInboxExpanded(true);
    requestAnimationFrame(() => followupInput.current?.focus());
    void window.jarvis?.focus(true);
  };
  useEffect(() => { if (replying) followupInput.current?.focus(); }, [replying]);
  useEffect(() => () => { if (followupTimer.current) clearTimeout(followupTimer.current); }, []);
  const sendFollowup = () => {
    if (!followup.trim()) return;
    const message = followup.trim(); setFollowup(''); setFollowupMessage('模拟处理中…');
    if (followupTimer.current) clearTimeout(followupTimer.current);
    followupTimer.current = setTimeout(() => { setFollowupMessage(`演示回复：已收到“${message}”。未发送或执行真实任务。`); followupTimer.current = null; }, 1200);
  };
  const [dismissing, setDismissing] = useState<string[]>([]);
  const dismissTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const showInbox = s.inbox || retainInbox;
  useEffect(() => {
    if (s.inbox) { setRetainInbox(true); return; }
    const t = setTimeout(() => setRetainInbox(false), 260);
    return () => clearTimeout(t);
  }, [s.inbox]);
  const input = useRef<HTMLTextAreaElement>(null);
  const shell = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const count = s.results.filter(r => !r.read).length;
  const detail = s.results.find(r => r.id === s.detail);
  const focusInput = async () => { await window.jarvis?.focus(true); input.current?.focus(); };
  const mode = (value: 'voice' | 'text' | 'idle') => {
    if (placement.docked) island.reveal();
    // Text mode keeps the conversation in view: the log opens with the composer when no panel is up.
    if (value === 'text') { if (!panel) setPanelOpen('transcript', true); setPanelOpen('composer', true); return; }
    setPresencePreview('auto');
    if (value === 'voice' && s.mode !== 'voice') feedback('voice-enter');
    if (value !== 'voice' && s.mode === 'voice') feedback('voice-exit');
    dispatch({ type: 'mode', mode: value }); setSettings(false);
  };
  // ADR 0041: wave mode is conversation mode, listening without a wake word. Resent after
  // every reconnect, because a restarted daemon comes back out of it.
  const connected = s.phase !== 'error';
  useEffect(() => { if (live && connected) void runtime.current?.controls({ conversation: s.mode === 'voice' }).catch(() => undefined); }, [s.mode, connected]);
  // The wake word opens wave mode too, so the talk goes on without saying it again.
  useEffect(() => { if (live && s.phase === 'hearing' && s.mode === 'idle') mode('voice'); }, [s.phase]);
  // The log polls memory.db while it is open: the first load takes the newest page, every later tick only the rows past the last one held.
  const lastSeq = useRef(0);
  lastSeq.current = s.rows.length ? s.rows[s.rows.length - 1].seq : 0;
  useEffect(() => {
    if (!live || (!transcriptOpen && !panels.dashboard)) return;
    let stop = false;
    const load = async () => { try { const rows = await runtime.current?.conversation(lastSeq.current); if (rows && !stop) dispatch({ type: 'rows', rows }); } catch { /* daemon away; the next tick retries */ } };
    void load();
    const id = setInterval(() => void load(), 2000);
    return () => { stop = true; clearInterval(id); };
  }, [transcriptOpen, panels.dashboard]);
  const tail = s.reply && !s.rows.some(row => row.seq > s.openSeq && row.source !== 'allen') ? visible(s.reply) : '';
  // Starting a GPT-Live session opens the capsule too, so the clock and subtitles have somewhere to live.
  const toggleLive = () => { if (!live || liveBusy) return; if (s.live.state !== 'active' && s.mode === 'idle') mode('voice'); void runtime.current?.controls({ live: s.live.state === 'active' ? 'stop' : 'start' }); };
  useEffect(() => { if (!panels.composer || collapsed.composer) return; const t = setTimeout(() => void focusInput(), 80); return () => clearTimeout(t); }, [panels.composer, collapsed.composer]);
  useEffect(() => window.jarvis?.onCommand(command => {
    setHidden(false);
    if (placement.docked) island.reveal();
    if (command === 'keyboard') document.querySelector<HTMLButtonElement>('.control-row button:not([inert])')?.focus();
    else if (command === 'dashboard') openDashboard();
    else if (command === 'plugins') openPlugins();
    else if (command === 'settings') { setSettings(true); void window.jarvis?.focus(true); }
    else if (command === 'voice' || command === 'text') mode(command);
  }), [s.mode, s.inbox, panels, feedbackEnabled, feedbackVolume, placement.docked]);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); for (const t of dismissTimers.current.values()) clearTimeout(t); }, []);
  const dismissResult = (id: string) => {
    if (replying === id) { setReplying(null); if (followupTimer.current) clearTimeout(followupTimer.current); }
    if (dismissTimers.current.has(id)) return;
    setDismissing(ids => [...ids, id]);
    dismissTimers.current.set(id, setTimeout(() => {
      dispatch({ type: 'dismiss', id });
      setDismissing(ids => ids.filter(value => value !== id));
      dismissTimers.current.delete(id);
    }, 340));
  };
  const closeDetail = () => { dispatch({ type: 'detail', id: null }); if (panels.composer) void focusInput(); else if (s.inbox) void focusInbox(); else if (!settings) void window.jarvis?.focus(false); };
  const closeSettings = () => { setSettings(false); if (panels.composer) void focusInput(); else if (s.inbox) void focusInbox(); else if (!s.detail) void window.jarvis?.focus(false); };
  const focusInbox = async () => {
    await window.jarvis?.focus(true);
    requestAnimationFrame(() => shell.current?.querySelector<HTMLButtonElement>('.inbox .result-main')?.focus({ preventScroll: true }));
  };
  const expandInbox = () => { setInboxExpanded(true); void focusInbox(); };
  const collapseInbox = () => {
    if (followupTimer.current) clearTimeout(followupTimer.current);
    setReplying(null); setInboxExpanded(false);
    shell.current?.querySelector('.inbox')?.scrollTo({ top: 0, behavior: 'smooth' });
    void focusInbox();
  };
  const toggleInbox = () => {
    if (followupTimer.current) clearTimeout(followupTimer.current);
    if (!s.inbox && !retainInbox) setInboxExpanded(false);
    setReplying(null); dispatch({ type: 'inbox' });
    if (!s.inbox) void focusInbox();
    else if (panels.composer) void focusInput();
    else if (!settings) void window.jarvis?.focus(false);
  };
  useEffect(() => { if (!live && s.phase === 'speaking') { const t = setTimeout(() => dispatch({ type: 'interrupt' }), 6500); return () => clearTimeout(t); } }, [s.phase]);
  useLayoutEffect(() => {
    const el = shell.current;
    if (!el) return;
    const update = () => {
      const bounds = el.getBoundingClientRect();
      if (placement.docked) {
        // Grow from the closed pill into concave shoulders at the screen edge.
        const progress = Math.max(0, Math.min(1, (bounds.width - placement.compactWidth) / (placement.surfaceWidth - placement.compactWidth)));
        const w = bounds.width, h = bounds.height, top = Math.min(22 * progress, h / 4), bottom = Math.min(placement.topInset / 2 + (22 - placement.topInset / 2) * progress, h / 2);
        el.style.clipPath = `path('M 0 0 Q ${top} 0 ${top} ${top} L ${top} ${h - bottom} Q ${top} ${h} ${top + bottom} ${h} L ${w - top - bottom} ${h} Q ${w - top} ${h} ${w - top} ${h - bottom} L ${w - top} ${top} Q ${w - top} 0 ${w} 0 Z')`;
      } else el.style.clipPath = '';
      if (!lab) window.jarvis?.layout(s.mode, Math.ceil(bounds.height + (placement.docked ? 16 : 32)), { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height });
      const rects = [...el.querySelectorAll<HTMLElement>('[data-glass]')].filter(e => !e.parentElement?.closest('.panel-stack')).map(e => {
        const r = e.getBoundingClientRect();
        let visibleOpacity = 1;
        for (let node: HTMLElement | null = e; node && node !== el; node = node.parentElement) { const style = getComputedStyle(node); visibleOpacity *= style.visibility === 'hidden' || style.display === 'none' ? 0 : Number(style.opacity); }
        if (e.dataset.glassFade) visibleOpacity *= Number(getComputedStyle(e).getPropertyValue(e.dataset.glassFade));
        return { x: r.x, y: r.y, width: r.width, height: r.height, radius: Number(e.dataset.glass), opacity: visibleOpacity, occlusion: clipStackGlass(e) };
      });
      if (!lab) window.jarvis?.material(placement.docked ? [] : rects.filter(r => r.width > 0 && r.height > 0), placement.docked ? 0 : glassStrength);
    };
    update(); const observer = new ResizeObserver(update); observer.observe(el);
    el.querySelectorAll('[data-glass]').forEach(e => observer.observe(e));
    let frame = 0; let deadline = performance.now() + 650;
    const animate = () => { update(); if (performance.now() < deadline) frame = requestAnimationFrame(animate); };
    frame = requestAnimationFrame(animate);
    const transition = () => { cancelAnimationFrame(frame); deadline = performance.now() + 650; frame = requestAnimationFrame(animate); };
    el.addEventListener('transitionrun', transition);
    el.addEventListener('animationstart', transition);
    el.addEventListener('scroll', update, true);
    el.addEventListener('capsule-motion', update);
    window.addEventListener('resize', update);
    return () => { el.removeEventListener('animationstart', transition); el.removeEventListener('scroll', update, true); el.removeEventListener('capsule-motion', update); el.removeEventListener('transitionrun', transition); cancelAnimationFrame(frame); observer.disconnect(); window.removeEventListener('resize', update); };
  }, [s.mode, s.inbox, showInbox, s.detail, s.reply, s.phase, settings, opacity, glassStrength, s.results.length, s.attachment, dismissing.length, inboxExpanded, replying, followupMessage, s.subtitles.length, s.rows.length, s.live.state, s.live.reason, s.live.notice, panel, s.soundMuted, placement, island.open]);
  useCapsuleDrag(!lab);
  const send = () => {
    if (!s.draft.trim() || s.phase === 'processing') return;
    const text = s.draft.trim();
    dispatch({ type: 'send' });
    if (live) { runtime.current?.submit(text).catch(() => dispatch({ type: 'phase', phase: 'error' })); return; }
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => { dispatch({ type: 'answer' }); timer.current = null; }, 1400);
  };
  const interrupt = () => { if (live) void runtime.current?.cancel(s.responseId); dispatch({ type: 'interrupt' }); };
  const retry = () => {
    if (live) { runtime.current?.reconnect(); return; }
    dispatch({ type: 'phase', phase: 'processing' }); if (timer.current) clearTimeout(timer.current); timer.current = setTimeout(() => dispatch({ type: 'phase', phase: 'listening' }), 1200);
  };
  const hide = () => { setPresencePreview('auto'); stopFeedback(); window.jarvis?.hide(); if (lab) setHidden(true); };
  const end = () => { if (timer.current) clearTimeout(timer.current); dispatch({ type: 'end' }); hide(); };
  const copy = async (text: string) => { try { if (window.jarvis) setCopied(await window.jarvis.copy(text)); else { await navigator.clipboard.writeText(text); setCopied(true); } } catch { setCopied(false); } };
  const escape = () => {
    if (settings) closeSettings();
    else if (s.detail) closeDetail();
    else if (s.inbox && inboxExpanded && s.results.length > 1) collapseInbox();
    else if (replying) { setReplying(null); if (s.inbox) void focusInbox(); }
    else if (s.inbox) toggleInbox();
    else if (panels.composer) setPanelOpen('composer', false);
    else if (panels.transcript) closeTranscript();
    else if (panels.dashboard) closePanel();
    else if (s.mode === 'voice') mode('idle');
    else hide();
  };
  const status = s.phase === 'error' ? '连接失败' : s.live.state === 'active' ? (s.live.speaking ? 'Live · 正在播报' : s.live.hearing ? 'Live · 正在听' : 'Live · 通话中') : s.micMuted && s.phase === 'listening' ? '麦克风已关闭' : labels[s.phase];
  const surfaceStyle = { ...surfaceThemes[activeSurfaceTheme], '--theme-color': themeColor, '--glass-opacity': placement.docked ? 1 : opacity, '--glass-strength': placement.docked ? 0 : glassStrength, '--notch-top': `${placement.topInset}px`, '--notch-width': `${placement.surfaceWidth}px`, '--notch-content-height': `${Math.min(472, screen.availHeight - placement.topInset - 32)}px`, '--notch-compact-width': `${placement.compactWidth}px`, '--notch-cutout': `${placement.notchWidth}px`, '--preview-scale': lab ? scale : 1 } as React.CSSProperties & Record<`--${string}`, string | number>;
  const notifications = <div className="dashboard-notification-content" onKeyDown={event => { if (event.key !== 'Escape') return; event.stopPropagation(); if (s.detail) closeDetail(); else if (replying) setReplying(null); else closePanel(); }}>
        {<section className={`inbox is-open ${stacked ? 'is-stacked' : 'is-expanded'} ${s.detail ? 'has-detail' : ''}`} inert={!s.inbox} aria-hidden={!s.inbox} aria-label="示例通知" data-interactive onScroll={event => setNotificationScroll(event.currentTarget.scrollTop)}>
          {s.results.length === 0 && <div className="empty glass"><Bell size={20}/><p>暂时没有待查看的事项</p><small>任务结果、待回应事项和你设定的提醒会出现在这里。</small></div>}
          {s.results.map((r, index) => <article key={r.id} style={{ '--card-index': index } as React.CSSProperties} className={`result glass ${s.detail && s.detail !== r.id ? 'is-detail-hidden' : ''} ${dismissing.includes(r.id) ? 'is-dismissing' : ''} ${replying === r.id ? 'is-replying' : ''}`} inert={dismissing.includes(r.id) || (stacked && index > 0) || (!!s.detail && s.detail !== r.id)} aria-hidden={(stacked && index > 0) || (!!s.detail && s.detail !== r.id)} >
            <button className="result-main" aria-label={stacked && index === 0 ? `展开 ${s.results.length} 条通知` : undefined} onClick={() => { if (stacked) { expandInbox(); return; } dispatch({ type: 'detail', id: r.id }); setCopied(false); void window.jarvis?.focus(true); }}><span className="result-copy"><strong>{r.title}</strong><small> · 示例 · </small><span className="summary">{r.summary}</span></span></button>
            <Button label={r.kind === 'question' ? `回复${r.title}` : `标记${r.title}已查看`} className={`result-status kind-${r.kind}`} onClick={() => { if (r.kind === 'question') { openFollowup(r.id); } else dismissResult(r.id); }}>{r.kind === 'failure' ? <X size={18}/> : r.kind === 'question' ? <CapsuleIcon name="reply" width={16} height={16}/> : <Check size={19}/>}</Button>
            <Button label={`移除${r.title}`} className="result-dismiss" onClick={() => dismissResult(r.id)}><X size={12}/></Button>
            <div className="result-actions"><Button label={`继续讨论${r.title}`} onClick={() => openFollowup(r.id)}><CapsuleIcon name="reply" width={16} height={16}/></Button></div>
            {replying === r.id && <>{followupMessage ? <p className="inline-reply-message" role="status">{followupMessage}</p> : <form className="inline-reply" onSubmit={e => { e.preventDefault(); sendFollowup(); }}><textarea ref={followupInput} rows={1} aria-label={`回复${r.title}的内容`} placeholder="继续回复…" value={followup} onChange={e => setFollowup(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); sendFollowup(); } }}/><Button label="发送通知回复（模拟）" type="submit" disabled={!followup.trim()}><CapsuleIcon name="send" width={15} height={15}/></Button></form>}</>}
          </article>)}
        </section>}
        {remainingNotifications > 0 && !detail && <button className="notification-more" onClick={() => shell.current?.querySelector('.inbox')?.scrollBy({ top:63, behavior:'smooth' })}>还有 {remainingNotifications} 条 ↓</button>}
        {detail && <section className="detail glass" data-interactive aria-label="结果详情"><div className="section-heading"><span>{detail.title}</span><Button label="关闭结果详情" onClick={closeDetail}><X size={17}/></Button></div><div className="detail-body">{detail.body}</div><div className="detail-actions"><button onClick={() => void copy(detail.body)}>{copied ? <Check size={14}/> : <Copy size={14}/>} {copied ? '已复制' : '复制'}</button>{detail.kind === 'question' && <button onClick={() => { dispatch({ type: 'detail', id: null }); mode('text'); dispatch({ type: 'draft', value: '明天上午十点' }); }}>回复 <ArrowUpRight size={14}/></button>}</div></section>}
  </div>;
  return <IconContext.Provider value={{ size: 20, weight: 'regular' }}>
    <main className={lab ? `lab ${background}` : `desktop ${placement.docked ? 'notch-docked' : ''}`} style={surfaceStyle} onKeyDown={e => {
      if (e.key === 'Escape') { e.preventDefault(); if (!e.repeat) escape(); }
      if (e.key === '.' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); end(); }
    }}>
      {lab && <header className="lab-header"><div><span>JARVIS</span><h1>Resonance</h1><p>交互原型 · 所有语音、回复与结果均为模拟</p></div><p className="lab-note">无色毛玻璃<br/>背景赋予玻璃颜色，声纹随状态舒展。</p></header>}
      <div className="stage">
      {(!hidden || !lab) && <div ref={shell} onMouseEnter={island.enter} onMouseLeave={island.leave} data-glass={placement.docked ? '22' : undefined} data-notch-surface={placement.docked || undefined} className={`shell mode-${s.mode} ${island.open ? 'island-open' : ''} ${panel || (s.soundMuted && s.live.state === 'active') ? 'has-shared-panel' : ''}`}>
        <div className="control-row" role="toolbar" aria-label={live ? 'Jarvis 语音控制' : 'Jarvis 语音控制 · 演示，无真实录音'} onContextMenu={e => {
          if ((e.target as Element).closest('textarea')) return;
          e.preventDefault(); if (placement.docked) island.reveal(); setSettings(true); void window.jarvis?.focus(true);
        }}>
          <div className="capsule-main-view">
            <PresentationCapsule presentation={s.mode === 'voice' ? 'expanded' : 'collapsed'} active nativeSurface
              transcriptEntry={placement.docked ? undefined : <TranscriptTrigger open={transcriptOpen} onOpen={() => transcriptOpen ? closeTranscript() : openTranscript()}/>}
              presence={presence} restState={s.phase === 'error' ? 'unavailable' : s.phase === 'processing' ? 'thinking' : count > 0 ? 'notification' : 'standby'}
              color={themeColor} onActivate={() => mode('voice')} onCollapse={() => { if (s.live.state === 'active') void runtime.current?.controls({ live: 'stop' }); mode('idle'); }}
              microphoneMuted={s.micMuted} speakerMuted={s.soundMuted}
              onMicrophoneToggle={() => { feedback(s.micMuted ? 'mic-on' : 'mic-off'); if (live) void runtime.current?.controls({ mic_muted: !s.micMuted }); else dispatch({ type: 'mic' }); }}
              onSpeakerToggle={() => { feedback(s.soundMuted ? 'speaker-on' : 'speaker-off'); if (live) void runtime.current?.controls({ speech_muted: !s.soundMuted }); else dispatch({ type: 'sound' }); }}
              onCompose={() => panels.composer ? setPanelOpen('composer', false) : mode('text')} rightControl={<button className="presentation-wing-face dashboard-toggle" data-glass="20" data-glass-fade="--wing-reveal" data-interactive aria-label="Dashboard" aria-expanded={panels.dashboard} onClick={() => panels.dashboard ? closePanel() : openDashboard()}><SquaresFour size={20}/>{count > 0 && <span className="dashboard-unread"/>}</button>}/>
          </div>
        </div>
        <div className="notch-reveal" inert={placement.docked && !island.open} aria-hidden={placement.docked && !island.open}>
        <div className="notch-content"><div className="notch-content-inner">
        <div className="status-line" data-interactive><span role="status" className="sr-only">{s.mode === 'idle' ? '待机' : status}{!live && <span className="demo-label"> · 演示</span>}</span>
          {s.phase === 'speaking' && <button className="text-action" onClick={interrupt}><Pause size={12}/>停止播报</button>}
          {live && s.live.state !== 'unavailable' && <button className={`text-action live-toggle ${s.live.state === 'active' ? 'is-active' : ''}`} disabled={liveBusy} onClick={toggleLive}>{s.live.state === 'active' ? <><PhoneDisconnect size={12}/>挂断 {liveClock}</> : s.live.state === 'connecting' ? '连接中…' : s.live.state === 'closing' ? '挂断中…' : <><Phone size={12}/>开始 Live</>}</button>}
          <Button label="外观与窗口选项" className="options" aria-expanded={settings} onClick={() => { if (settings) closeSettings(); else { setSettings(true); void window.jarvis?.focus(true); } }}><DotsThree size={19}/></Button>
        </div>
        {settings && <section className="settings glass" data-glass="18" data-interactive aria-label="外观与窗口选项">
          <div className="section-heading"><span><GearSix size={16}/>外观与提示音</span><Button label="关闭外观设置" onClick={closeSettings}><X size={16}/></Button></div>
          {!lab && <div className="settings-actions"><button onClick={() => void window.jarvis?.dock(!placement.docked)}>{placement.docked ? '自由悬浮' : '贴到刘海'}</button>{placement.docked && <span className="notch-material-note">刘海模式使用纯黑背景</span>}</div>}
          <div className="presence-settings">
            <label>声纹状态<select aria-label="声纹状态" value={presencePreview} onChange={e => setPresencePreview(e.target.value as typeof presencePreview)}><option value="auto">跟随交互</option><option value="cycle">完整体验中</option>{Object.entries(presenceLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
            <button onClick={() => { mode('voice'); setPresencePreview('cycle'); closeSettings(); }}>体验完整动效</button>
            <label>主题色<input aria-label="声纹主题色" type="color" value={themeColor} onChange={e => updatePreferences({ themeColor: e.target.value })}/></label>
          </div>
          <label>Dashboard 风格<select aria-label="Dashboard 风格" value={preferences.dashboardStyle} onChange={e => updatePreferences({ dashboardStyle: e.target.value as 'unified' | 'cards' })}><option value="unified">统一风格</option><option value="cards">原卡片风格（备份）</option></select></label>
          <label>额度页面布局<select aria-label="额度页面布局" value={preferences.quotaLayout} onChange={e => updatePreferences({ quotaLayout: e.target.value as 'category' | 'provider' | 'accordion' })}><option value="category">轻量标签 · 按类别</option><option value="provider">服务商切换 · 按服务商</option><option value="accordion">B2 · 紧凑折叠</option></select></label>
          <label className="range-label">玻璃不透明度 <output>{placement.docked ? '100%' : `${Math.round(opacity * 100)}%`}</output><input aria-label="玻璃不透明度" aria-valuetext={`${Math.round(opacity * 100)}%`} disabled={placement.docked} type="range" min=".08" max=".65" step=".01" value={opacity} onChange={e => setOpacity(Number(e.target.value))}/></label>
          <label className="range-label">毛玻璃强度 <output>{placement.docked ? '0%' : `${Math.round(glassStrength * 100)}%`}</output><input aria-label="毛玻璃强度" aria-valuetext={`${Math.round(glassStrength * 100)}%`} disabled={placement.docked} type="range" min="0" max="1" step=".01" value={glassStrength} onChange={e => updatePreferences({ glassStrength: Number(e.target.value) })}/></label>
          <div className="feedback-setting"><span>操作提示音</span><button role="switch" aria-label="操作提示音" aria-checked={feedbackEnabled} onClick={() => { stopFeedback(); updatePreferences({ feedbackEnabled: !feedbackEnabled }); }}><span/></button></div>
          <label className="range-label">提示音音量 <output>{Math.round(feedbackVolume * 100)}%</output><input aria-label="提示音音量" aria-valuetext={`${Math.round(feedbackVolume * 100)}%`} type="range" min="0" max="1" step=".01" value={feedbackVolume} disabled={!feedbackEnabled} onChange={e => updatePreferences({ feedbackVolume: Number(e.target.value) })}/></label>
          <div className="settings-actions"><button onClick={openDashboard}>打开 Dashboard</button><button onClick={openPlugins}>插件</button><button onClick={() => { closeSettings(); openTranscript(); }}>完整对话记录</button><button onClick={() => updatePreferences(defaultPreferences)}>恢复默认</button><button disabled={!feedbackEnabled} onClick={() => feedback('voice-enter')}>试听提示音</button><button onClick={() => { dispatch({ type: 'example', id: 'result' }); dispatch({ type: 'example', id: 'reminder' }); dispatch({ type: 'example', id: 'question' }); setInboxExpanded(true); openDashboard(); }}>体验三条通知</button><button onClick={hide}>隐藏浮窗</button><button onClick={end}>结束语音</button></div>
          <p>声纹使用模拟节奏，未接入录音。主题色统一用于声纹和 Dashboard，外观、额度布局与提示音设置自动保存。</p>
        </section>}
        {s.phase === 'error' && <section className="error-panel glass" data-glass="18" data-interactive><div><strong>暂时没有连上</strong><p>{live ? 'Jarvis 服务没有响应，正在重连。' : '演示连接失败。你可以重试或继续打字。'}</p></div><Button label={live ? '立即重连' : '重试模拟连接'} onClick={retry}><ArrowCounterClockwise/></Button></section>}
        <PanelStack open={panels} collapsed={collapsed} onCollapse={id => setCollapsed(previous => ({ ...previous, [id]: !previous[id] }))} onClose={id => id === 'dashboard' ? closePanel() : setPanelOpen(id, false)}>
          {{ composer: <div className="text-composer">
            <textarea ref={input} aria-label="文字输入" rows={3} placeholder="说不方便说的话…" value={s.draft} onChange={e => dispatch({ type: 'draft', value: e.target.value })} onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); }
            }}/>
            <div className="composer-actions"><Button label="添加附件（即将支持）" disabled><CapsuleIcon name="plus"/></Button>
              <Button label={s.phase === 'processing' ? '正在处理' : live ? '发送' : '发送示例输入'} className="composer-send" disabled={!s.draft.trim() || s.phase === 'processing'} onClick={send}><CapsuleIcon name="send"/></Button></div>
            {s.reply && <section className="reply glass"  data-interactive><div className="section-heading"><span>{live ? '回复' : '示例回复'}</span><Button label="复制回复" onClick={() => void copy(visible(s.reply))}>{copied ? <Check size={16}/> : <Copy size={16}/>}</Button></div><p>{visible(s.reply)}</p></section>}
          </div>,
          transcript: <LiveTranscript embedded rows={s.rows} tail={tail} sessionId={s.live.sessionId} lines={s.subtitles} open={true} muted={s.soundMuted} active={s.live.state === 'active'} clock={liveClock} onClose={closeTranscript}/>,
          dashboard: <DashboardPreview embedded port={runtimePort} onClose={closePanel} visible={panels.dashboard && !collapsed.dashboard}
            shown={panels.dashboard && !collapsed.dashboard && (!placement.docked || island.open)}
            composer={{ value: s.draft, onChange: value => dispatch({ type: 'draft', value }), onSend: send, busy: s.phase === 'processing' }}
            conversation={{ text: tail || visible(s.rows.filter(row => row.source !== 'allen').at(-1)?.text ?? '') || (s.phase === 'processing' ? '正在处理你的消息…' : '和 Jarvis 说点什么，最近的回复会出现在这里。'), caption: s.phase === 'processing' ? '处理中' : s.phase === 'error' ? '连接失败' : tail || s.rows.length ? '最近回复' : '暂无对话', pending: s.phase === 'processing' }}
            plugins={{ controller: plugins, presentation: pluginPresentation, open: pluginOpen, onCatalog: openPlugins, onConversation: () => { closePanel(); openTranscript(); } }}
            notifications={s.results.length || detail ? notifications : null}/>
          }}
        </PanelStack>
        {!transcriptOpen && <LiveTranscript embedded rows={s.rows} tail={tail} sessionId={s.live.sessionId} lines={s.subtitles} open={false} muted={s.soundMuted} active={s.live.state === 'active'} clock={liveClock} onClose={closeTranscript}/>}
        </div></div></div>
      </div>}
      {hidden && lab && <button className="restore" onClick={() => setHidden(false)}>恢复胶囊</button>}
      </div>
      {lab && <aside className="lab-controls" aria-label="独立开发测试台"><div><label>模拟阶段<select aria-label="模拟阶段" value={s.phase} onChange={e => dispatch({ type: 'phase', phase: e.target.value as Phase })}>{Object.entries(labels).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select></label><label>桌面背景<select aria-label="桌面背景" value={background} onChange={e => setBackground(e.target.value)}><option value="forest">森林</option><option value="light">浅灰</option><option value="dark">深灰</option></select></label><label>预览倍率<select aria-label="预览倍率" value={scale} onChange={e => setScale(Number(e.target.value))}><option value="1">实际尺寸</option><option value="1.6">1.6 倍</option></select></label></div><div><button onClick={() => mode('idle')}>待机</button><button onClick={() => mode('voice')}>语音</button>{examples.map(r => <button key={r.id} onClick={() => dispatch({ type: 'example', id: r.id })}>{({ result: '结果', question: '待回应', failure: '失败', reminder: '提醒' })[r.kind]}示例</button>)}<button onClick={() => { if (timer.current) clearTimeout(timer.current); dispatch({ type: 'reset' }); setHidden(false); setSettings(false); }}>重置演示</button></div></aside>}
    </main>
  </IconContext.Provider>;
}
createRoot(document.getElementById('root')!).render(new URLSearchParams(location.search).has('companion') ? <Companion/> : new URLSearchParams(location.search).has('workspace-preview') ? <WorkspacePreview/> : new URLSearchParams(location.search).has('dashboard-window') ? <DashboardPreview standalone/> : lab ? <PreviewLab initialDashboard={new URLSearchParams(location.search).has('dashboard')}/> : <App/>);

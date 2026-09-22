import React, { useEffect, useLayoutEffect, useReducer, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { IconContext, SquaresFour, Bell, X, ArrowUpRight, Copy, Check, ArrowCounterClockwise, Pause, Paperclip, DotsThree, GearSix, Phone, PhoneDisconnect } from '@phosphor-icons/react';
import { initialState, reducer, examples, type Phase } from './model';
import './style.css';
import { useCapsuleDrag } from './useCapsuleDrag';
import { presenceLabels, type Presence } from './VoicePresence';
import { PreviewLab, DashboardPreview } from './DashboardPreview';
import { LiveTranscript, TranscriptTrigger } from './LiveTranscript';
import { PresentationCapsule } from './PresentationCapsule';
import { CapsuleIcon } from './CapsuleIcon';
import { playFeedback, stopFeedback, warmFeedback, type FeedbackCue } from './feedback';
import { defaultPreferences, usePreferences } from './preferences';
import { clipStackGlass, type GlassOcclusion } from './stackGlass';
import { connect, type Runtime } from './runtime';
declare global { interface Window { jarvis?: {
  drag: (phase: 'start' | 'move' | 'end', point?: { x: number; y: number }) => void;
  copy: (text: string) => Promise<boolean>;
  layout: (mode: string, height: number) => void; focus: (enabled: boolean) => Promise<void>; hide: () => void; passthrough: (enabled: boolean) => void;
  material: (rects: {x:number;y:number;width:number;height:number;radius:number;opacity:number;occlusion?:GlassOcclusion}[], strength: number) => void;
  onCommand: (cb: (value: string) => void) => () => void;
} } }
const lab = new URLSearchParams(location.search).has('lab');
// A `port` query means Electron wants the live daemon link; without it every timer below is the simulation.
const runtimePort = new URLSearchParams(location.search).get('port');
const live = runtimePort !== null;
// The render layer wraps speech in <voice> and card text in <document> (voice_tts.py:99); show both, drop the markup and any half-streamed tag.
const visible = (reply: string) => reply.replace(/<\/voice>/g, '\n').replace(/<\/?(voice|document)>/g, '').replace(/<\/?[a-z]*$/, '').trim();
const labels: Record<Phase, string> = { listening: '正在听取', hearing: '正在听', processing: '正在处理', speaking: '正在播报', error: '连接失败' };
const mmss = (sec: number) => `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
function Button({ label, children, className = '', ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return <button {...props} className={`icon-button ${className}`} aria-label={label} title={label}><span className="button-glyph" key={label}>{children}</span></button>;
}
function App() {
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
  const [panel, setPanel] = useState<'transcript' | 'dashboard' | null>(null);
  const transcriptOpen = panel === 'transcript';
  const setTranscriptOpen = (open: boolean) => setPanel(open ? 'transcript' : null);
  const openDashboard = () => { setPanel('dashboard'); setSettings(false); if (s.inbox) dispatch({ type: 'inbox' }); void window.jarvis?.focus(true); };
  const closePanel = () => { setPanel(null); void window.jarvis?.focus(false); };
  const openTranscript = () => { setTranscriptOpen(true); setSettings(false); if (s.inbox) dispatch({ type: 'inbox' }); void window.jarvis?.focus(true); };
  const closeTranscript = () => { setTranscriptOpen(false); void window.jarvis?.focus(false); };
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
  const [added, setAdded] = useState(false);
  const [copied, setCopied] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [retainInbox, setRetainInbox] = useState(false);
  const [inboxExpanded, setInboxExpanded] = useState(false);
  const [replying, setReplying] = useState<string | null>(null);
  const [followup, setFollowup] = useState('');
  const [followupMessage, setFollowupMessage] = useState('');
  const followupInput = useRef<HTMLTextAreaElement>(null);
  const followupTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stacked = s.results.length > 1 && !inboxExpanded && !s.detail;
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
  const composerReturn = useRef<'voice' | 'idle'>('idle');
  const mode = (value: 'voice' | 'text' | 'idle') => { if (value === 'text' && s.mode !== 'text') composerReturn.current = s.mode; setPresencePreview('auto'); if (value === 'voice' && s.mode !== 'voice') feedback('voice-enter'); if (value !== 'voice' && s.mode === 'voice') feedback('voice-exit'); dispatch({ type: 'mode', mode: value }); setAdded(false); setSettings(false); if (value !== 'text') void window.jarvis?.focus(false);
    // Text mode keeps the conversation in view: the log opens with the composer unless another panel is up, and leaves with it.
    if (value === 'text' && s.mode !== 'text') setPanel(p => p ?? 'transcript'); else if (value !== 'text' && s.mode === 'text' && panel === 'transcript') setPanel(null); };
  // The log polls memory.db while it is open: the first load takes the newest page, every later tick only the rows past the last one held.
  const lastSeq = useRef(0);
  lastSeq.current = s.rows.length ? s.rows[s.rows.length - 1].seq : 0;
  useEffect(() => {
    if (!live || !transcriptOpen) return;
    let stop = false;
    const load = async () => { try { const rows = await runtime.current?.conversation(lastSeq.current); if (rows && !stop) dispatch({ type: 'rows', rows }); } catch { /* daemon away; the next tick retries */ } };
    void load();
    const id = setInterval(() => void load(), 2000);
    return () => { stop = true; clearInterval(id); };
  }, [transcriptOpen]);
  const tail = s.reply && !s.rows.some(row => row.seq > s.openSeq && row.source !== 'allen') ? visible(s.reply) : '';
  // Starting a GPT-Live session opens the capsule too, so the clock and subtitles have somewhere to live.
  const toggleLive = () => { if (!live || liveBusy) return; if (s.live.state !== 'active' && s.mode === 'idle') mode('voice'); void runtime.current?.controls({ live: s.live.state === 'active' ? 'stop' : 'start' }); };
  useEffect(() => { if (s.mode !== 'text') return; const t = setTimeout(() => void focusInput(), 80); return () => clearTimeout(t); }, [s.mode]);
  useEffect(() => window.jarvis?.onCommand(command => {
    setHidden(false);
    if (command === 'keyboard') document.querySelector<HTMLButtonElement>('.control-row button:not([inert])')?.focus();
    else if (command === 'dashboard') openDashboard();
    else if (command === 'settings') { setSettings(true); void window.jarvis?.focus(true); }
    else if (command === 'voice' || command === 'text') mode(command);
  }), [s.mode, feedbackEnabled, feedbackVolume]);
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
  const closeDetail = () => { dispatch({ type: 'detail', id: null }); if (s.mode === 'text') void focusInput(); else if (s.inbox) void focusInbox(); else if (!settings) void window.jarvis?.focus(false); };
  const closeSettings = () => { setSettings(false); if (s.mode === 'text') void focusInput(); else if (s.inbox) void focusInbox(); else if (!s.detail) void window.jarvis?.focus(false); };
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
    else if (s.mode === 'text') void focusInput();
    else if (!settings) void window.jarvis?.focus(false);
  };
  useEffect(() => { if (!live && s.phase === 'speaking') { const t = setTimeout(() => dispatch({ type: 'interrupt' }), 6500); return () => clearTimeout(t); } }, [s.phase]);
  useLayoutEffect(() => {
    const el = shell.current;
    if (!el) return;
    const update = () => {
      if (!lab) window.jarvis?.layout(s.mode, Math.ceil(el.getBoundingClientRect().height + 32));
      const rects = [...el.querySelectorAll<HTMLElement>('[data-glass]')].map(e => {
        const r = e.getBoundingClientRect();
        let visibleOpacity = 1;
        for (let node: HTMLElement | null = e; node && node !== el; node = node.parentElement) { const style = getComputedStyle(node); visibleOpacity *= style.visibility === 'hidden' || style.display === 'none' ? 0 : Number(style.opacity); }
        if (e.dataset.glassFade) visibleOpacity *= Number(getComputedStyle(e).getPropertyValue(e.dataset.glassFade));
        return { x: r.x, y: r.y, width: r.width, height: r.height, radius: Number(e.dataset.glass), opacity: visibleOpacity, occlusion: clipStackGlass(e) };
      });
      if (!lab) window.jarvis?.material(rects.filter(r => r.width > 0 && r.height > 0), glassStrength);
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
  }, [s.mode, s.inbox, showInbox, s.detail, s.reply, s.phase, settings, added, opacity, glassStrength, s.results.length, s.attachment, dismissing.length, inboxExpanded, replying, followupMessage, s.subtitles.length, s.rows.length, s.live.state, s.live.reason, s.live.notice, panel, s.soundMuted]);
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
    if (panel) closePanel();
    else if (settings) closeSettings();
    else if (added) setAdded(false);
    else if (s.detail) closeDetail();
    else if (s.inbox && inboxExpanded && s.results.length > 1) collapseInbox();
    else if (replying) { setReplying(null); if (s.inbox) void focusInbox(); }
    else if (s.inbox) toggleInbox();
    else if (s.mode === 'text') mode(composerReturn.current);
    else if (s.mode === 'voice') mode('idle');
    else hide();
  };
  const status = s.phase === 'error' ? '连接失败' : s.live.state === 'active' ? (s.live.speaking ? 'Live · 正在播报' : s.live.hearing ? 'Live · 正在听' : 'Live · 通话中') : s.micMuted && s.phase === 'listening' ? '麦克风已关闭' : labels[s.phase];
  const surfaceStyle = { '--glass-opacity': opacity, '--glass-strength': glassStrength, '--preview-scale': lab ? scale : 1 } as React.CSSProperties;
  return <IconContext.Provider value={{ size: 20, weight: 'regular' }}>
    <main className={lab ? `lab ${background}` : 'desktop'} style={surfaceStyle} onKeyDown={e => {
      if (e.key === 'Escape') { e.preventDefault(); if (!e.repeat) escape(); }
      if (e.key === '.' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); end(); }
    }}>
      {lab && <header className="lab-header"><div><span>JARVIS</span><h1>Resonance</h1><p>交互原型 · 所有语音、回复与结果均为模拟</p></div><p className="lab-note">无色毛玻璃<br/>背景赋予玻璃颜色，声纹随状态舒展。</p></header>}
      <div className="stage">
      {(!hidden || !lab) && <div ref={shell} className={`shell mode-${s.mode} ${panel || (s.soundMuted && s.live.state === 'active') ? 'has-shared-panel' : ''}`}>
        <div className="control-row" role="toolbar" aria-label={live ? 'Jarvis 语音控制' : 'Jarvis 语音控制 · 演示，无真实录音'} onContextMenu={e => {
          if ((e.target as Element).closest('textarea')) return;
          e.preventDefault(); setSettings(true); void window.jarvis?.focus(true);
        }}>
          <div className="capsule-main-view" hidden={s.mode === 'text'}>
            <PresentationCapsule presentation={s.mode === 'voice' ? 'expanded' : 'collapsed'} active={s.mode !== 'text'} nativeSurface
              transcriptEntry={s.mode === 'voice' ? <TranscriptTrigger open={transcriptOpen} onOpen={() => transcriptOpen ? closeTranscript() : openTranscript()}/> : undefined}
              presence={presence} restState={s.phase === 'error' ? 'unavailable' : s.phase === 'processing' ? 'thinking' : count > 0 ? 'notification' : 'standby'}
              color={themeColor} onActivate={() => mode('voice')} onCollapse={() => { if (s.live.state === 'active') void runtime.current?.controls({ live: 'stop' }); setTranscriptOpen(false); mode('idle'); }}
              microphoneMuted={s.micMuted} speakerMuted={s.soundMuted}
              onMicrophoneToggle={() => { feedback(s.micMuted ? 'mic-on' : 'mic-off'); if (live) void runtime.current?.controls({ mic_muted: !s.micMuted }); else dispatch({ type: 'mic' }); }}
              onSpeakerToggle={() => { feedback(s.soundMuted ? 'speaker-on' : 'speaker-off'); if (live) void runtime.current?.controls({ speech_muted: !s.soundMuted }); else dispatch({ type: 'sound' }); }}
              onCompose={() => mode('text')} onNotifications={toggleInbox} unreadCount={count} inboxOpen={s.inbox}/>
          </div>
          {s.mode === 'text' && <>
            <div className="entry-surface glass" data-glass="20" data-interactive>
              <Button label="添加内容" className="entry-button" aria-expanded={added} onClick={() => setAdded(!added)}><CapsuleIcon name="plus"/></Button>
              <div className="composer-fields">
                <textarea ref={input} aria-label="文字输入" rows={1} placeholder="说不方便说的话…" value={s.draft} onChange={e => dispatch({ type: 'draft', value: e.target.value })} onKeyDown={e => {
                  if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); }
                }}/>
                <Button label={s.phase === 'processing' ? (live ? '正在处理' : '正在处理示例输入') : (live ? '发送' : '发送示例输入')} className="send" disabled={!s.draft.trim() || s.phase === 'processing'} onClick={send}><CapsuleIcon name="send"/></Button>
              </div>
            </div>
            <Button label="收起文字输入" className="glass detached" data-glass="20" data-interactive onClick={() => mode(composerReturn.current)}><CapsuleIcon name="collapse"/></Button>
            <Button label={s.inbox ? '收起通知' : '通知'} aria-expanded={s.inbox} className="glass detached notification" data-glass="20" data-interactive onClick={toggleInbox}><CapsuleIcon name={s.inbox ? 'collapse' : 'bell'}/>{count > 0 && !s.inbox && <span className="unread">{count > 9 ? '9+' : count}</span>}</Button>
          </>}
          <Button label="Dashboard" aria-expanded={panel === 'dashboard'} className="glass detached dashboard-toggle" data-glass="20" data-interactive onClick={() => panel === 'dashboard' ? closePanel() : openDashboard()}><SquaresFour size={19}/></Button>
        </div>
        <div className="status-line" data-interactive><span role="status" className="sr-only">{s.mode === 'idle' ? '待机' : status}{!live && <span className="demo-label"> · 演示</span>}</span>
          {s.phase === 'speaking' && <button className="text-action" onClick={interrupt}><Pause size={12}/>停止播报</button>}
          {live && s.live.state !== 'unavailable' && <button className={`text-action live-toggle ${s.live.state === 'active' ? 'is-active' : ''}`} disabled={liveBusy} onClick={toggleLive}>{s.live.state === 'active' ? <><PhoneDisconnect size={12}/>挂断 {liveClock}</> : s.live.state === 'connecting' ? '连接中…' : s.live.state === 'closing' ? '挂断中…' : <><Phone size={12}/>开始 Live</>}</button>}
          <Button label="外观与窗口选项" className="options" aria-expanded={settings} onClick={() => { if (settings) closeSettings(); else { setSettings(true); void window.jarvis?.focus(true); } }}><DotsThree size={19}/></Button>
        </div>
        {settings && <section className="settings glass" data-glass="18" data-interactive aria-label="外观与窗口选项">
          <div className="section-heading"><span><GearSix size={16}/>外观与提示音</span><Button label="关闭外观设置" onClick={closeSettings}><X size={16}/></Button></div>
          <div className="presence-settings">
            <label>声纹状态<select aria-label="声纹状态" value={presencePreview} onChange={e => setPresencePreview(e.target.value as typeof presencePreview)}><option value="auto">跟随交互</option><option value="cycle">完整体验中</option>{Object.entries(presenceLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
            <button onClick={() => { mode('voice'); setPresencePreview('cycle'); closeSettings(); }}>体验完整动效</button>
            <label>主题色<input aria-label="声纹主题色" type="color" value={themeColor} onChange={e => updatePreferences({ themeColor: e.target.value })}/></label>
          </div>
          <label className="range-label">玻璃不透明度 <output>{Math.round(opacity * 100)}%</output><input aria-label="玻璃不透明度" aria-valuetext={`${Math.round(opacity * 100)}%`} type="range" min=".08" max=".65" step=".01" value={opacity} onChange={e => setOpacity(Number(e.target.value))}/></label>
          <label className="range-label">毛玻璃强度 <output>{Math.round(glassStrength * 100)}%</output><input aria-label="毛玻璃强度" aria-valuetext={`${Math.round(glassStrength * 100)}%`} type="range" min="0" max="1" step=".01" value={glassStrength} onChange={e => updatePreferences({ glassStrength: Number(e.target.value) })}/></label>
          <div className="feedback-setting"><span>操作提示音</span><button role="switch" aria-label="操作提示音" aria-checked={feedbackEnabled} onClick={() => { stopFeedback(); updatePreferences({ feedbackEnabled: !feedbackEnabled }); }}><span/></button></div>
          <label className="range-label">提示音音量 <output>{Math.round(feedbackVolume * 100)}%</output><input aria-label="提示音音量" aria-valuetext={`${Math.round(feedbackVolume * 100)}%`} type="range" min="0" max="1" step=".01" value={feedbackVolume} disabled={!feedbackEnabled} onChange={e => updatePreferences({ feedbackVolume: Number(e.target.value) })}/></label>
          <div className="settings-actions"><button onClick={openDashboard}>打开 Dashboard</button><button onClick={() => { closeSettings(); openTranscript(); }}>完整对话记录</button><button onClick={() => updatePreferences(defaultPreferences)}>恢复默认</button><button disabled={!feedbackEnabled} onClick={() => feedback('voice-enter')}>试听提示音</button><button onClick={() => { dispatch({ type: 'example', id: 'reminder' }); if (!s.inbox) dispatch({ type: 'inbox' }); setInboxExpanded(false); closeSettings(); }}>体验通知叠层</button><button onClick={hide}>隐藏浮窗</button><button onClick={end}>结束语音</button></div>
          <p>声纹使用模拟节奏，未接入录音。主题色只影响声纹，外观与提示音设置自动保存。</p>
        </section>}
        {added && <section className="addition glass" data-glass="18" data-interactive><button onClick={() => { dispatch({ type: 'attachment' }); setAdded(false); }}><Paperclip size={18}/>{s.attachment ? '移除示例附件' : '附加示例便笺'}</button><p>仅使用预置示例，不读取本地文件。</p></section>}
        {s.attachment && <div className="attachment" data-interactive><Paperclip size={13}/>示例便笺.txt<Button label="移除示例附件" onClick={() => dispatch({ type: 'attachment' })}><X size={12}/></Button></div>}
        {s.phase === 'error' && <section className="error-panel glass" data-glass="18" data-interactive><div><strong>暂时没有连上</strong><p>{live ? 'Jarvis 服务没有响应，正在重连。' : '演示连接失败。你可以重试或继续打字。'}</p></div><Button label={live ? '立即重连' : '重试模拟连接'} onClick={retry}><ArrowCounterClockwise/></Button></section>}
        {!s.inbox && <div className={`shared-panel ${panel ? 'shared-panel-open glass' : ''}`} data-glass={panel ? "20" : undefined} data-interactive={panel ? true : undefined}>{panel === 'dashboard' ? <DashboardPreview embedded port={runtimePort} onClose={closePanel}/> :<LiveTranscript embedded={!!panel} rows={s.rows} tail={tail} sessionId={s.live.sessionId} lines={s.subtitles} open={transcriptOpen} muted={s.soundMuted} active={s.live.state === 'active'} clock={liveClock} onClose={closeTranscript}/>}</div>}
        {s.reply && !s.inbox && <section className="reply glass" data-glass="18" data-interactive><div className="section-heading"><span>{live ? '回复' : '示例回复'}</span><Button label="复制回复" onClick={() => void copy(visible(s.reply))}>{copied ? <Check size={16}/> : <Copy size={16}/>}</Button></div><p>{visible(s.reply)}</p></section>}
        {showInbox && <section className={`inbox ${s.inbox ? 'is-open' : 'is-closing'} ${stacked ? 'is-stacked' : 'is-expanded'} ${s.detail ? 'has-detail' : ''}`} inert={!s.inbox} aria-hidden={!s.inbox} aria-label="示例通知" data-interactive>
          {s.results.length === 0 && <div className="empty glass" data-glass="18"><Bell size={20}/><p>暂时没有待查看的事项</p><small>任务结果、待回应事项和你设定的提醒会出现在这里。</small></div>}
          {s.results.map((r, index) => <article key={r.id} style={{ '--card-index': index } as React.CSSProperties} className={`result glass ${s.detail && s.detail !== r.id ? 'is-detail-hidden' : ''} ${dismissing.includes(r.id) ? 'is-dismissing' : ''} ${replying === r.id ? 'is-replying' : ''}`} inert={dismissing.includes(r.id) || (stacked && index > 0) || (!!s.detail && s.detail !== r.id)} aria-hidden={(stacked && index > 0) || (!!s.detail && s.detail !== r.id)} data-glass="28">
            <button className="result-main" aria-label={stacked && index === 0 ? `展开 ${s.results.length} 条通知` : undefined} onClick={() => { if (stacked) { expandInbox(); return; } dispatch({ type: 'detail', id: r.id }); setCopied(false); void window.jarvis?.focus(true); }}><span className="result-copy"><strong>{r.title}</strong><small> · 示例 · </small><span className="summary">{r.summary}</span></span></button>
            <Button label={r.kind === 'question' ? `回复${r.title}` : `标记${r.title}已查看`} className={`result-status kind-${r.kind}`} onClick={() => { if (r.kind === 'question') { openFollowup(r.id); } else dismissResult(r.id); }}>{r.kind === 'failure' ? <X size={18}/> : r.kind === 'question' ? <CapsuleIcon name="reply" width={16} height={16}/> : <Check size={19}/>}</Button>
            <Button label={`移除${r.title}`} className="result-dismiss" data-glass="10" onClick={() => dismissResult(r.id)}><X size={12}/></Button>
            <div className="result-actions"><Button label={`继续讨论${r.title}`} onClick={() => openFollowup(r.id)}><CapsuleIcon name="reply" width={16} height={16}/></Button></div>
            {replying === r.id && <>{followupMessage ? <p className="inline-reply-message" role="status">{followupMessage}</p> : <form className="inline-reply" onSubmit={e => { e.preventDefault(); sendFollowup(); }}><textarea ref={followupInput} rows={1} aria-label={`回复${r.title}的内容`} placeholder="继续回复…" value={followup} onChange={e => setFollowup(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); sendFollowup(); } }}/><Button label="发送通知回复（模拟）" type="submit" disabled={!followup.trim()}><CapsuleIcon name="send" width={15} height={15}/></Button></form>}</>}
          </article>)}
          {s.results.length > 1 && <button className="stack-collapse" inert={!inboxExpanded || !!s.detail} aria-hidden={!inboxExpanded || !!s.detail} onClick={collapseInbox}>收起为叠层 · Esc</button>}
        </section>}
        {detail && <section className="detail glass" data-glass="18" data-interactive aria-label="结果详情"><div className="section-heading"><span>{detail.title}</span><Button label="关闭结果详情" onClick={closeDetail}><X size={17}/></Button></div><div className="detail-body">{detail.body}</div><div className="detail-actions"><button onClick={() => void copy(detail.body)}>{copied ? <Check size={14}/> : <Copy size={14}/>} {copied ? '已复制' : '复制'}</button>{detail.kind === 'question' && <button onClick={() => { dispatch({ type: 'detail', id: null }); mode('text'); dispatch({ type: 'draft', value: '明天上午十点' }); }}>回复 <ArrowUpRight size={14}/></button>}</div></section>}
      </div>}
      {hidden && lab && <button className="restore" onClick={() => setHidden(false)}>恢复胶囊</button>}
      </div>
      {lab && <aside className="lab-controls" aria-label="独立开发测试台"><div><label>模拟阶段<select aria-label="模拟阶段" value={s.phase} onChange={e => dispatch({ type: 'phase', phase: e.target.value as Phase })}>{Object.entries(labels).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select></label><label>桌面背景<select aria-label="桌面背景" value={background} onChange={e => setBackground(e.target.value)}><option value="forest">森林</option><option value="light">浅灰</option><option value="dark">深灰</option></select></label><label>预览倍率<select aria-label="预览倍率" value={scale} onChange={e => setScale(Number(e.target.value))}><option value="1">实际尺寸</option><option value="1.6">1.6 倍</option></select></label></div><div><button onClick={() => mode('idle')}>待机</button><button onClick={() => mode('voice')}>语音</button>{examples.map(r => <button key={r.id} onClick={() => dispatch({ type: 'example', id: r.id })}>{({ result: '结果', question: '待回应', failure: '失败', reminder: '提醒' })[r.kind]}示例</button>)}<button onClick={() => { if (timer.current) clearTimeout(timer.current); dispatch({ type: 'reset' }); setHidden(false); setSettings(false); setAdded(false); }}>重置演示</button></div></aside>}
    </main>
  </IconContext.Provider>;
}
createRoot(document.getElementById('root')!).render(new URLSearchParams(location.search).has('dashboard-window') ? <DashboardPreview standalone/> : lab ? <PreviewLab initialDashboard={new URLSearchParams(location.search).has('dashboard')}/> : <App/>);

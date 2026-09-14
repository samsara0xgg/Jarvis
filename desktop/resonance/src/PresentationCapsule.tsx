import { useCallback, useRef, type ReactNode } from 'react';
import { CapsuleIcon } from './CapsuleIcon';
import { LivePresence, type RestState } from './LivePresence';
import { VoicePresence, type Presence } from './VoicePresence';
import './presentation-capsule.css';

export type CapsulePresentation = 'collapsed' | 'expanded';
// All event handlers express UI intent only. The runtime owns actual mode selection.
export function PresentationCapsule({ presentation, presence, restState = 'standby', onCollapse, onActivate,
  microphoneMuted = false, speakerMuted = false, onMicrophoneToggle, onSpeakerToggle, onCompose, onNotifications,
  color = '#a8b5ff', playbackRate = 1, renderScale = 1, active = true, nativeSurface = false, unreadCount = 0, inboxOpen = false, waveOnly = false, transcriptEntry }: {
  transcriptEntry?: ReactNode; presentation: CapsulePresentation; presence: Presence; restState?: RestState;
  onCollapse: () => void; onActivate: () => void;
  microphoneMuted?: boolean; speakerMuted?: boolean;
  onMicrophoneToggle?: () => void; onSpeakerToggle?: () => void;
  onCompose?: () => void; onNotifications?: () => void;
  color?: string; playbackRate?: number; renderScale?: number; active?: boolean; nativeSurface?: boolean; unreadCount?: number; inboxOpen?: boolean; waveOnly?: boolean;
}) {
  const collapsed = !waveOnly && presentation === 'collapsed';
  const root = useRef<HTMLDivElement>(null);
  const lastProgress = useRef<number | null>(null);
  const onProgress = useCallback((p: number) => {
    if (!root.current || p === lastProgress.current) return;
    lastProgress.current = p;
    root.current.style.setProperty('--mode-progress', String(p));
    const reveal = Math.max(0, Math.min(1, (p - .28) / .72));
    root.current.style.setProperty('--wing-reveal', String(reveal * reveal * (3 - 2 * reveal)));
    const controls = Math.max(0, Math.min(1, (p - .3) / .55));
    root.current.style.setProperty('--control-reveal', String(controls * controls * (3 - 2 * controls)));
    if (nativeSurface) root.current.dispatchEvent(new Event('capsule-motion', { bubbles: true }));
  }, [nativeSurface]);
  return <div ref={root} className={`presentation-capsule ${waveOnly ? 'is-wave-only' : ''} ${collapsed ? 'is-collapsed' : 'is-expanded'}`} data-presentation={waveOnly ? "expanded" : presentation}>
    <span className="presentation-wing"><button className="presentation-wing-face" data-glass={nativeSurface ? 20 : undefined} data-glass-fade={nativeSurface ? "--wing-reveal" : undefined} data-interactive aria-label="发消息" onClick={onCompose}><CapsuleIcon name="compose"/></button></span>
    <div className={`presentation-core ${transcriptEntry ? "has-transcript-entry" : ""} ${nativeSurface ? "glass" : ""}`} data-glass={nativeSurface ? 20 : undefined} data-interactive>
      <button className="presentation-edge" inert={collapsed} aria-hidden={collapsed} aria-label="麦克风静音" aria-pressed={microphoneMuted} onClick={onMicrophoneToggle}><CapsuleIcon name={microphoneMuted ? 'microphone-off' : 'microphone'}/></button>
      <span className="presentation-divider"/>
      <button className="presentation-center" aria-label={waveOnly ? '隐藏悬浮窗' : collapsed ? '进入 Live' : '退出 Live'} aria-expanded={!collapsed} onClick={collapsed ? onActivate : onCollapse}>
        <>{waveOnly ? <VoicePresence state={presence} color={color} variant="refined" motionPolicy="animate" active={active} playbackRate={playbackRate}/> : <LivePresence live={!collapsed} restState={restState} presence={presence} color={color} playbackRate={playbackRate} renderScale={renderScale} active={active} onProgress={onProgress}/>}</>
      </button>
      <span className="presentation-divider"/>
      <button className="presentation-edge" inert={collapsed} aria-hidden={collapsed} aria-label="扬声器静音" aria-pressed={speakerMuted} onClick={onSpeakerToggle}><CapsuleIcon name={speakerMuted ? 'speaker-off' : 'speaker'}/></button>
      {transcriptEntry}
    </div>
    <span className="presentation-wing"><button className="presentation-wing-face notification" data-glass={nativeSurface ? 20 : undefined} data-glass-fade={nativeSurface ? "--wing-reveal" : undefined} data-interactive aria-label={inboxOpen ? "收起通知" : "通知"} aria-expanded={inboxOpen} onClick={onNotifications}><CapsuleIcon name={inboxOpen ? "collapse" : "bell"}/>{unreadCount > 0 && !inboxOpen && <span className="unread">{unreadCount > 9 ? "9+" : unreadCount}</span>}</button></span>
  </div>;
}

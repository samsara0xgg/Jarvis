import { useState } from 'react';
import { PresentationCapsule } from './PresentationCapsule';
import { OrbitPreview } from './OrbitPreview';
import type { RestState } from './LivePresence';
import { presenceLabels, type Presence } from './VoicePresence';
import './motion-preview.css';

const restStates: { value: RestState; label: string }[] = [
  { value: 'standby', label: '待机' }, { value: 'thinking', label: '后台处理' },
  { value: 'notification', label: '有消息' }, { value: 'unavailable', label: '暂不可用' },
];
export function MotionPreview() {
  const [gallery, setGallery] = useState(false);
  const [live, setLive] = useState(false);
  const [restState, setRestState] = useState<RestState>('standby');
  const [presence, setPresence] = useState<Presence>('listening');
  const [slow, setSlow] = useState(false);
  const [microphoneMuted, setMicrophoneMuted] = useState(false);
  const [speakerMuted, setSpeakerMuted] = useState(false);
  const [notice, setNotice] = useState('');
  const props = { presentation: live ? 'expanded' as const : 'collapsed' as const, presence, restState,
    onActivate: () => setLive(true), onCollapse: () => setLive(false), playbackRate: slow ? 1 / 3 : 1,
    microphoneMuted, speakerMuted, onMicrophoneToggle: () => setMicrophoneMuted(v => !v), onSpeakerToggle: () => setSpeakerMuted(v => !v),
    onCompose: () => setNotice('发消息入口 · 本次只预览按钮'), onNotifications: () => setNotice('通知入口 · 本次只预览按钮') };
  if (gallery) return <><button className="preview-back" onClick={() => setGallery(false)}>返回胶囊过渡</button><OrbitPreview/></>;
  return <main className="motion-preview live-preview">
    <header><span>JARVIS / ORBIT TO LIVE</span><h1>从安静，到对话。</h1><p>点击中间光点进入 Live，再次点击中间声纹退出。收起时是发消息与通知；展开后，两者移向外侧，露出语音控制。</p></header>
    <section className="live-stage" aria-label="胶囊切换预览">
      <div className="orbit-stage-caption"><span>光点快速预览 · 放大 2.5 倍</span><span aria-live="polite">{live ? 'LIVE / 声纹' : '非 LIVE / 光点'}</span></div>
      <div className="presentation-large-view"><PresentationCapsule {...props} renderScale={2.5}/></div>
      <p className="live-hint">{live ? '点击声纹，收回光点' : '点击光点，展开对话'}</p>
      <div className="presentation-actual-view"><PresentationCapsule {...props}/><span>实际尺寸 · 可直接点击</span></div>
    </section>
    <div className="live-state-label">{live ? 'Live 声纹状态' : '非 Live 光点状态'}<span>切换这些状态，胶囊宽度不变</span></div>
    <nav className="live-states" aria-label="当前模式状态">{live ? Object.entries(presenceLabels).map(([value, label]) => <button key={value} aria-pressed={presence === value} onClick={() => setPresence(value as Presence)}>{label}</button>) : restStates.map(item => <button key={item.value} aria-pressed={restState === item.value} onClick={() => setRestState(item.value)}>{item.label}</button>)}</nav>
    <footer><button aria-pressed={slow} onClick={() => setSlow(!slow)}>三分之一慢放</button><button onClick={() => setGallery(true)}>查看保留的六状态圆环</button><span>可以连续点击中间，观察动画中途反向。</span></footer>
    <p className="motion-note" aria-live="polite">{notice || '纯 UI 预览 · 模式与按钮均为本地演示，未连接麦克风、唤醒词或业务逻辑。'}</p>
  </main>;
}

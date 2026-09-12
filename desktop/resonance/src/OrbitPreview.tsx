import { useEffect, useState } from 'react';
import { OrbitPresence, orbitStates, type OrbitState } from './OrbitPresence';
import './motion-preview.css';

export function OrbitPreview() {
  const [state, setState] = useState<OrbitState>('standby');
  const [slow, setSlow] = useState(false);
  const [cycle, setCycle] = useState(false);
  useEffect(() => {
    if (!cycle) return;
    const timer = setTimeout(() => setState(value => orbitStates[(orbitStates.findIndex(s => s.value === value) + 1) % orbitStates.length].value), slow ? 7000 : 4000);
    return () => clearTimeout(timer);
  }, [cycle, state, slow]);
  const selected = orbitStates.find(s => s.value === state)!;
  return <main className="motion-preview">
    <header><span>JARVIS / ORBIT STUDY</span><h1>安静，也有状态。</h1><p>同一种声纹颜色，用圆环的运动与明暗表达状态。点击下方切换，观察圆环如何连续过渡。</p></header>
    <section className="orbit-stage" aria-label="圆环动态预览">
      <div className="orbit-stage-caption"><span>圆环形态 · 放大观察</span><span>01 / ORBIT</span></div>
      <div className="orbit-hero"><OrbitPresence state={state} size={144} playbackRate={slow ? 1 / 3 : 1}/></div>
      <div className="orbit-description" aria-live="polite"><h2>{selected.label}</h2><p>{selected.detail}</p></div>
      <div className="orbit-actual"><OrbitPresence state={state} size={32} playbackRate={slow ? 1 / 3 : 1}/><span>实际尺寸 · 32 px</span></div>
    </section>
    <nav aria-label="圆环状态">{orbitStates.map(item => <button key={item.value} aria-label={item.label} aria-pressed={state === item.value} onClick={() => { setCycle(false); setState(item.value); }}><OrbitPresence state={item.value} size={36}/><span>{item.label}</span></button>)}</nav>
    <footer><button aria-pressed={cycle} onClick={() => setCycle(!cycle)}>{cycle ? '停止轮播' : '自动轮播'}</button><button aria-pressed={slow} onClick={() => setSlow(!slow)}>三分之一慢放</button><span>状态切换保留光点位置，逐渐改变速度、亮度与呼吸幅度。</span></footer>
    <p className="motion-note">纯视觉样稿 · 聆听和回应使用示意节奏，未连接麦克风或业务逻辑。</p>
  </main>;
}

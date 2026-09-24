import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowUp, PushPin } from '@phosphor-icons/react';

export type DashboardInput = { value: string; onChange: (value: string) => void; onSend: () => void; busy?: boolean };

// One retained surface: the spring changes its geometry, never scales the text.
export function DashboardComposer({ value, onChange, onSend, busy = false, active }: DashboardInput & { active: boolean }) {
  const [hovered, setHovered] = useState(false);
  const [pinned, setPinned] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const expanded = active && (hovered || pinned);
  const target = useRef(false), wake = useRef(() => {});
  useLayoutEffect(() => { target.current = expanded; wake.current(); }, [expanded]);
  useLayoutEffect(() => {
    const el = root.current!;
    const reduce = matchMedia('(prefers-reduced-motion: reduce)');
    let value = 0, velocity = 0, frame = 0, last = 0;
    const draw = (now: number) => {
      frame = 0;
      const goal = Number(target.current);
      const dt = Math.min(last ? (now - last) / 1000 : 1 / 60, .04);
      last = now;
      if (reduce.matches) { value = goal; velocity = 0; }
      else {
        const tension = 23, offset = value - goal, momentum = velocity + tension * offset, decay = Math.exp(-tension * dt);
        value = goal + (offset + momentum * dt) * decay;
        velocity = (velocity - tension * momentum * dt) * decay;
      }
      const settled = Math.abs(value - goal) < .001 && Math.abs(velocity) < .01;
      if (settled) { value = goal; velocity = 0; last = 0; }
      el.style.setProperty('--compose-progress', String(value));
      el.dataset.motion = settled ? 'settled' : 'moving';
      if (!settled) frame = requestAnimationFrame(draw);
    };
    const resume = () => { if (!frame) frame = requestAnimationFrame(draw); };
    wake.current = resume; reduce.addEventListener('change', resume); resume();
    return () => { cancelAnimationFrame(frame); reduce.removeEventListener('change', resume); wake.current = () => {}; };
  }, []);
  useEffect(() => { if (!active) { setHovered(false); input.current?.blur(); } }, [active]);
  const focusInput = async () => { await window.jarvis?.focus(true); if (target.current) input.current?.focus({ preventScroll: true }); };
  const togglePin = () => {
    setPinned(value => !value);
    if (!pinned) requestAnimationFrame(() => void focusInput());
  };
  const collapse = () => { setPinned(false); setHovered(false); input.current?.blur(); trigger.current?.focus({ preventScroll: true }); };
  return <div className="dashboard-composer-dock" ref={root} data-expanded={expanded} data-pinned={pinned} hidden={!active} inert={!active}
    onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); collapse(); } }}>
    <div className="dashboard-composer-hit" onPointerEnter={() => setHovered(true)} onPointerLeave={() => { setHovered(false); if (!pinned) input.current?.blur(); }}
      onClick={event => { if (!(event.target as Element).closest('input,button')) togglePin(); }}>
      <div className="dashboard-composer-surface"/>
      <button ref={trigger} className="dashboard-composer-trigger" aria-label="固定文字输入框" aria-expanded={expanded} aria-pressed={pinned}
        tabIndex={expanded ? -1 : 0} aria-hidden={expanded} onClick={togglePin}><span className="sr-only">悬停展开，点击固定文字输入框</span></button>
      <form className="dashboard-floating-form" inert={!expanded} aria-hidden={!expanded} onSubmit={event => { event.preventDefault(); if (value.trim() && !busy) onSend(); }}>
        <input ref={input} aria-label="给 Jarvis 发消息" placeholder="和 Jarvis 说点什么…" value={value} onChange={event => onChange(event.target.value)}
          onClick={() => { setPinned(true); void focusInput(); }} onKeyDown={event => { if (event.key === 'Enter' && event.nativeEvent.isComposing) event.preventDefault(); }}/>
        <button className="dashboard-input-pin" type="button" aria-label={pinned ? '取消固定输入框' : '固定输入框'} aria-pressed={pinned} title={pinned ? '取消固定 · 移开鼠标收起' : '固定输入框'} onClick={togglePin}><PushPin size={14} weight={pinned ? 'fill' : 'regular'}/></button>
        <button className="dashboard-input-send" aria-label={busy ? '正在处理' : '发送消息'} disabled={!value.trim() || busy}><ArrowUp size={16}/></button>
      </form>
    </div>
  </div>;
}

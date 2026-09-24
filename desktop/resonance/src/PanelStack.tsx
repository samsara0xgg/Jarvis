import { useLayoutEffect, useRef, useState, type ReactNode, type PointerEvent as ReactPointerEvent } from 'react';
import { CaretDown, DotsSixVertical, X } from '@phosphor-icons/react';
import { usePanelMotion } from './usePanelMotion';

export type PanelId = 'composer' | 'transcript' | 'dashboard' | 'plugins';
const titles: Record<PanelId, string> = { composer: '文字输入', transcript: '对话记录', dashboard: 'Dashboard', plugins: '连接插件' };
const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const movement = { duration: 240, easing: 'cubic-bezier(.22,1,.36,1)' };

function Panel({ id, open, collapsed, dragging, children, onCollapse, onClose, onDrag, onMove }: {
  id: PanelId; open: boolean; collapsed: boolean; dragging: boolean; children: ReactNode;
  onCollapse: () => void; onClose: () => void; onDrag: (event: ReactPointerEvent) => void; onMove: (direction: number) => void;
}) {
  const root = useRef<HTMLElement>(null), content = useRef<HTMLDivElement>(null);
  const seen = useRef(false);
  if (open) seen.current = true;
  usePanelMotion(root, content, open, collapsed);
  return <section className={`stack-module ${dragging ? 'is-dragging' : ''}`} ref={root} data-panel={id} inert={!open} aria-hidden={!open} onKeyDown={event => {
    if (event.key === 'Escape' && !dragging) { event.preventDefault(); event.stopPropagation(); if (!event.repeat) onClose(); }
  }}>
    <div className="stack-drag-layer"><div className="stack-module-inner" ref={content}>
      <header className="stack-heading" onPointerDown={event => { if (!(event.target as Element).closest('.stack-collapse-toggle,.stack-close')) onDrag(event); }}>
        <button className="stack-collapse-toggle" aria-label={`${collapsed ? '展开' : '折叠'}${titles[id]}`} aria-expanded={!collapsed} onClick={onCollapse}><CaretDown size={13} style={{ transform: collapsed ? 'rotate(-90deg)' : undefined }}/></button>
        <button className="stack-title" aria-label={`拖动排序${titles[id]}`} aria-describedby="panel-drag-help" title="拖动标题排序 · Alt + ↑/↓" onKeyDown={event => {
          if (event.altKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) { event.preventDefault(); onMove(event.key === 'ArrowUp' ? -1 : 1); }
        }}>{titles[id]}<DotsSixVertical size={13}/></button>
        <button className="stack-close" aria-label={`关闭${titles[id]}`} title="关闭" onClick={onClose}><X size={13}/></button>
      </header>
      <div className="stack-body" inert={collapsed} aria-hidden={collapsed}>{seen.current && children}</div>
    </div></div>
  </section>;
}

type Gesture = { id: PanelId; pointer: number; startY: number; y: number; offset: number; moved: boolean; initialOrder: PanelId[] };
export function PanelStack({ open, collapsed, onCollapse, onClose, children }: {
  open: Record<PanelId, boolean>; collapsed: Record<PanelId, boolean>;
  onCollapse: (id: PanelId) => void; onClose: (id: PanelId) => void; children: Record<PanelId, ReactNode>;
}) {
  const [order, setOrder] = useState<PanelId[]>(['composer', 'plugins', 'transcript', 'dashboard']);
  const [dragging, setDragging] = useState<PanelId | null>(null);
  const root = useRef<HTMLDivElement>(null), gesture = useRef<Gesture | null>(null);
  const keyboardFocus = useRef<PanelId | null>(null);
  const positions = useRef(new Map<string, number>()), animations = useRef(new Map<HTMLElement, Animation>());
  const frame = useRef(0), current = useRef({ order, open }); current.current = { order, open };
  const naturalTop = (el: HTMLElement) => root.current!.getBoundingClientRect().top + el.offsetTop - root.current!.scrollTop;
  const reorder = (next: PanelId[]) => {
    root.current?.querySelectorAll<HTMLElement>('.stack-module').forEach(el => positions.current.set(el.dataset.panel!, el.getBoundingClientRect().top));
    current.current.order = next; setOrder(next);
  };
  const move = (id: PanelId, direction: number) => {
    const visible = current.current.order.filter(key => current.current.open[key]), other = visible[visible.indexOf(id) + direction];
    if (!other) return;
    const next = [...current.current.order], a = next.indexOf(id), b = next.indexOf(other);
    next.splice(a, 1); next.splice(b, 0, id); reorder(next);
  };
  const positionDrag = () => {
    const g = gesture.current, el = g && root.current?.querySelector<HTMLElement>(`[data-panel="${g.id}"]`);
    if (g?.moved && el) el.querySelector<HTMLElement>('.stack-drag-layer')!.style.transform = `translateY(${g.y - g.offset - naturalTop(el)}px)`;
  };
  const tick = () => {
    const g = gesture.current, stack = root.current;
    if (!g?.moved || !stack) return;
    const bounds = stack.getBoundingClientRect();
    if (g.y < bounds.top + 28) stack.scrollTop -= 8;
    else if (g.y > bounds.bottom - 28) stack.scrollTop += 8;
    const visible = current.current.order.filter(id => current.current.open[id]), index = visible.indexOf(g.id);
    for (const direction of [-1, 1]) {
      const other = visible[index + direction], el = other && stack.querySelector<HTMLElement>(`[data-panel="${other}"]`);
      if (!el) continue;
      const middle = naturalTop(el) + el.offsetHeight / 2;
      if (direction < 0 ? g.y < middle : g.y > middle) { move(g.id, direction); break; }
    }
    positionDrag(); frame.current = requestAnimationFrame(tick);
  };
  const begin = (id: PanelId, event: ReactPointerEvent) => {
    if (event.button !== 0 || !event.isPrimary || gesture.current) return;
    const el = root.current!.querySelector<HTMLElement>(`[data-panel="${id}"]`)!;
    gesture.current = { id, pointer: event.pointerId, startY: event.clientY, y: event.clientY, offset: event.clientY - naturalTop(el), moved: false, initialOrder: [...order] };
    root.current!.setPointerCapture(event.pointerId);
  };
  const finish = (cancel = false) => {
    const g = gesture.current; if (!g) return;
    cancelAnimationFrame(frame.current);
    gesture.current = null;
    if (root.current?.hasPointerCapture(g.pointer)) root.current.releasePointerCapture(g.pointer);
    if (cancel && g.moved) reorder(g.initialOrder);
    const el = root.current?.querySelector<HTMLElement>(`[data-panel="${g.id}"] .stack-drag-layer`);
    if (!el || !g.moved) { setDragging(null); return; }
    requestAnimationFrame(() => {
      // Recompute after restoring the original slot on Escape / pointer cancel.
      const module = el.parentElement!;
      const offset = g.y - g.offset - naturalTop(module);
      el.style.transform = '';
      const animation = el.animate([{ transform: `translateY(${offset}px)` }, { transform: 'translateY(0)' }], reducedMotion() ? { duration: 0 } : movement);
      animation.finished.then(() => { if (!gesture.current) setDragging(null); }).catch(() => {});
    });
  };
  useLayoutEffect(() => {
    root.current?.querySelectorAll<HTMLElement>('.stack-module').forEach(el => {
      const before = positions.current.get(el.dataset.panel!);
      animations.current.get(el)?.cancel();
      if (before !== undefined && el.dataset.panel !== gesture.current?.id && !reducedMotion()) animations.current.set(el, el.animate([{ transform: `translateY(${before - naturalTop(el)}px)` }, { transform: 'translateY(0)' }], movement));
    });
    positions.current.clear(); positionDrag();
    if (keyboardFocus.current) { root.current?.querySelector<HTMLButtonElement>(`[data-panel="${keyboardFocus.current}"] .stack-title`)?.focus({ preventScroll: true }); keyboardFocus.current = null; }
  }, [order]);
  useLayoutEffect(() => {
    // Reordering a focused DOM node may temporarily move focus to the document.
    const cancel = (event: KeyboardEvent) => { if (event.key === 'Escape' && gesture.current) { event.preventDefault(); event.stopPropagation(); finish(true); } };
    window.addEventListener('keydown', cancel, true);
    return () => { window.removeEventListener('keydown', cancel, true); cancelAnimationFrame(frame.current); animations.current.forEach(animation => animation.cancel()); };
  }, []);
  return <div ref={root} className="shared-panel panel-stack glass" data-glass="20" data-glass-fade="--stack-presence" data-interactive style={{ '--panel-limit': `${Math.max(180, screen.availHeight - 120)}px` } as React.CSSProperties}
    onPointerMove={event => {
      const g = gesture.current; if (!g || event.pointerId !== g.pointer) return;
      g.y = event.clientY;
      if (!g.moved && Math.abs(g.y - g.startY) > 5) { g.moved = true; setDragging(g.id); frame.current = requestAnimationFrame(tick); }
      if (g.moved) event.preventDefault();
    }} onPointerUp={() => finish()} onPointerCancel={() => finish(true)} onLostPointerCapture={() => { if (gesture.current) finish(true); }}
    onKeyDownCapture={event => { if (event.key === 'Escape' && gesture.current) { event.preventDefault(); event.stopPropagation(); finish(true); } }}>
    <span id="panel-drag-help" className="sr-only">拖动标题调整位置；也可按 Alt 和上下方向键。拖动时按 Escape 取消。</span>
    {order.map(id => <Panel key={id} id={id} open={open[id]} collapsed={collapsed[id]} dragging={dragging === id} onCollapse={() => onCollapse(id)} onClose={() => onClose(id)} onDrag={event => begin(id, event)} onMove={direction => { keyboardFocus.current = id; move(id, direction); }}>{children[id]}</Panel>)}
  </div>;
}

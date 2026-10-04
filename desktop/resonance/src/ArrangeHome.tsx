import { useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react';
import { Bell, CalendarBlank, ChartDonut, ChatCircle, Clock, DotsSixVertical, EnvelopeSimple, Minus, Notebook, Plus, Robot, SquaresFour, SunHorizon } from '@phosphor-icons/react';
import { HOME_DEFAULTS, isPop, tr, useCompanionSettings, type BlockId, type L, type Lang } from './companionSettings';

// The home's blocks, as the arrange page and her panel name them.
export const BLOCK: Record<BlockId, { icon: ReactNode; name: L; when?: L }> = {
  talk: { icon: <ChatCircle/>, name: ['Conversation', '对话'], when: ['After you talk, for 10 min', '你开口后出现，10 分钟后收起'] },
  foryou: { icon: <Bell/>, name: ['For you', '找你的事'], when: ['When Jarvis needs you', 'Jarvis 有事找你时'] },
  brief: { icon: <SunHorizon/>, name: ['Morning brief', '早报'], when: ['Once each morning, the first time you open', '每天早上第一次打开时出现'] },
  today: { icon: <CalendarBlank/>, name: ['Today', '今天'] },
  mail: { icon: <EnvelopeSimple/>, name: ['Mail', '邮件'], when: ['Unread mail from people', '有人发来的未读邮件'] },
  memory: { icon: <Notebook/>, name: ['Memory', '记忆'], when: ['When Jarvis is connected', '连上 Jarvis 时出现'] },
  agents: { icon: <Robot/>, name: ['Agents', 'Agents'] },
  now: { icon: <Clock/>, name: ['Now', '现在'] },
  usage: { icon: <ChartDonut/>, name: ['Usage', '用量'] },
  tiles: { icon: <SquaresFour/>, name: ['Plugins · Projects', '插件 · 项目'] },
};

// Arrange the home: drag the dots (or Alt + ↑/↓) to move a block, − hides one that is always there, + brings it
// back, and the pop-ups have a switch instead. Escape during a drag puts the block back.
export function ArrangeHome({ lang }: { lang: Lang }) {
  const t = (l: L) => tr(lang, l);
  const [s, update] = useCompanionSettings();
  const shown = s.order.filter(id => !s.hidden.includes(id));
  const list = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: BlockId; from: number; to: number; y0: number; step: number; rows: HTMLElement[] } | null>(null);
  const [dragging, setDragging] = useState<BlockId | null>(null);
  const place = (next: BlockId[]) => update({ order: [...next, ...s.order.filter(id => s.hidden.includes(id))] });
  const move = (id: BlockId, to: number) => {
    const next = shown.filter(x => x !== id);
    next.splice(Math.max(0, Math.min(next.length, to)), 0, id);
    place(next);
  };
  const on = (id: BlockId) => id === 'talk' ? s.talk !== 'never' : !!s[id as 'foryou' | 'brief' | 'mail'];
  const flip = (id: BlockId) => id === 'talk' ? update({ talk: s.talk === 'never' ? 'after' : 'never' }) : update({ [id]: !on(id) });

  // The dragged row follows the pointer; the rows it passes step aside. It is never re-inserted mid-drag,
  // so the pointer capture stays on its grip; the new order lands on release.
  const settle = () => { drag.current?.rows.forEach(r => { r.style.transform = ''; }); drag.current = null; setDragging(null); };
  const start = (id: BlockId, e: PointerEvent<HTMLButtonElement>) => {
    const rows = [...list.current!.querySelectorAll<HTMLElement>('.ar-row')], from = shown.indexOf(id);
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { id, from, to: from, y0: e.clientY, step: rows[from].offsetHeight, rows };
    setDragging(id);
  };
  const follow = (e: PointerEvent) => {
    const d = drag.current; if (!d) return;
    const dy = e.clientY - d.y0, to = Math.max(0, Math.min(d.rows.length - 1, d.from + Math.round(dy / d.step)));
    d.to = to;
    d.rows.forEach((r, i) => {
      r.style.transform = i === d.from ? `translateY(${dy}px)` : i > d.from && i <= to ? `translateY(${-d.step}px)` : i < d.from && i >= to ? `translateY(${d.step}px)` : '';
    });
  };
  const drop = () => { const d = drag.current; if (!d) return; const { id, to, from } = d; settle(); if (to !== from) move(id, to); };
  const keys = (id: BlockId, e: KeyboardEvent) => {
    if (e.key === 'Escape' && drag.current) { e.stopPropagation(); settle(); return; }
    if (!e.altKey || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
    e.preventDefault();
    move(id, shown.indexOf(id) + (e.key === 'ArrowUp' ? -1 : 1));
    requestAnimationFrame(() => list.current?.querySelector<HTMLElement>(`[data-grip="${id}"]`)?.focus());
  };

  return <div className="pg-body ar-body">
    <div className="pg-sec ar-list" ref={list}>{shown.map(id => {
      const b = BLOCK[id], pop = isPop(id);
      return <div key={id} className={`ar-row ${pop && !on(id) ? 'is-off' : ''} ${dragging === id ? 'is-drag' : ''}`} data-block={id}>
        <button className="ar-grip" data-grip={id} aria-label={`${t(['Move', '移动'])} ${t(b.name)}`} title={t(['Drag · Alt + ↑/↓', '拖动 · Alt + ↑/↓'])}
          onPointerDown={e => start(id, e)} onPointerMove={follow} onPointerUp={drop} onPointerCancel={settle} onKeyDown={e => keys(id, e)}><DotsSixVertical size={14} weight="bold"/></button>
        <span className="ar-ic">{b.icon}</span>
        <span className="ar-tx"><b>{t(b.name)}</b><small>{t(b.when ?? ['Always on the home', '一直在首页'])}</small></span>
        {pop ? <button className="sw" role="switch" aria-checked={on(id)} aria-label={t(b.name)} onClick={() => flip(id)}/>
          : <button className="ar-b" aria-label={`${t(['Hide', '隐藏'])} ${t(b.name)}`} title={t(['Hide', '隐藏'])} onClick={() => update({ hidden: [...s.hidden, id] })}><Minus size={12} weight="bold"/></button>}
      </div>;
    })}</div>
    {s.hidden.length > 0 && <div className="pg-sec"><h4>{t(['Hidden', '隐藏的'])}</h4><div className="ar-list">{s.hidden.map(id =>
      <div key={id} className="ar-row is-off" data-block={id}><span className="ar-ic">{BLOCK[id].icon}</span><span className="ar-tx"><b>{t(BLOCK[id].name)}</b><small>{t(['Hidden', '已隐藏'])}</small></span>
        <button className="ar-b is-add" aria-label={`${t(['Add back', '加回来'])} ${t(BLOCK[id].name)}`} title={t(['Add back', '加回来'])} onClick={() => update({ hidden: s.hidden.filter(x => x !== id) })}><Plus size={12} weight="bold"/></button></div>)}</div></div>}
    <p className="pg-sec muted">{t(['Drag the dots to move a block. The ones with a switch only show up when there is something.', '拖左边的点换位置。带开关的几块只在有事时出现，关掉就不再出现。'])}</p>
    <div className="pg-sec"><button className="btn btn-ghost" onClick={() => update(HOME_DEFAULTS)}>{t(['Reset the home', '恢复默认首页'])}</button></div>
  </div>;
}

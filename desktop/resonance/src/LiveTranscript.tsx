import { useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown, Minus } from '@phosphor-icons/react';
import type { Subtitle } from './model';
import './live-transcript.css';

export function TranscriptTrigger({ open, onOpen }: { open: boolean; onOpen: () => void }) {
  return <button className="transcript-trigger" aria-label={open ? "收起完整对话记录" : "打开完整对话记录"} aria-expanded={open} onClick={onOpen} data-interactive><span/></button>;
}

export function LiveTranscript({ lines, open, muted, active, clock, onClose, embedded = false }: {
  embedded?: boolean; lines: Subtitle[]; open: boolean; muted: boolean; active: boolean; clock: string; onClose: () => void;
}) {
  const scroll = useRef<HTMLDivElement>(null);
  const mini = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [away, setAway] = useState(false);
  const latest = [...lines].reverse().find(line => line.role === 'assistant');
  const signature = lines.map(line => line.text).join('\n');
  useLayoutEffect(() => {
    if (following.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
    if (mini.current) mini.current.scrollTo({ top: mini.current.scrollHeight, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
  }, [signature, open, muted]);
  const tail = () => { following.current = true; setAway(false); scroll.current?.scrollTo({ top: scroll.current.scrollHeight, behavior: 'smooth' }); };
  return <>
    <div className={`transcript-reveal ${open ? 'is-open' : ''}`} inert={!open} aria-hidden={!open}>
      <section className="live-transcript glass" data-glass={embedded ? undefined : "20"} data-interactive aria-label="完整 Live 对话记录">
        <header><span className="transcript-session"><i/>{active ? 'LIVE' : '对话记录'}<small>{clock}</small></span><button aria-label="收起完整对话记录" onClick={onClose}><Minus size={16}/></button></header>
        <div className="transcript-history detail-body" ref={scroll} onScroll={() => { const el = scroll.current!; const atEnd = el.scrollHeight - el.clientHeight - el.scrollTop < 24; following.current = atEnd; setAway(!atEnd); }}>
          {lines.length ? lines.map((line, i) => <div className={`transcript-turn ${line.role}`} key={`${line.role}-${line.startMs}-${i}`}><span>{line.role === 'user' ? '你' : 'Jarvis'}</span><p>{line.text}</p></div>) : <p className="transcript-empty">{active ? '正在听，直接开口即可。' : '当前还没有对话记录。'}</p>}
        </div>
        {away && <button className="transcript-latest" onClick={tail}><ArrowDown size={13}/>回到最新</button>}
        <footer>{active ? '正在对话' : '对话已结束'}</footer>
      </section>
    </div>
    <div className={`mini-caption-reveal ${!open && muted && active && latest?.text ? 'is-open' : ''}`} aria-hidden={open || !muted || !active}>
      <section className="mini-caption glass" data-glass="16" data-interactive aria-label="Jarvis 当前回复"><div ref={mini}><p>{latest?.text}</p></div></section>
    </div>
  </>;
}

import { useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown, Minus } from '@phosphor-icons/react';
import type { Row, Subtitle } from './model';
import './live-transcript.css';

export function TranscriptTrigger({ open, onOpen }: { open: boolean; onOpen: () => void }) {
  return <button className="transcript-trigger" aria-label={open ? "收起完整对话记录" : "打开完整对话记录"} aria-expanded={open} onClick={onOpen} data-interactive><span/></button>;
}

const who = (source: string) => source === 'allen' ? '你' : source === 'jarvis' ? 'Jarvis' : source === 'jarvis_live' ? 'Jarvis · Live' : source;
// Same-day rows show the clock only; older rows carry the date so the log reads across days.
const when = (ts: string, now = new Date()) => {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  const clock = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return d.toDateString() === now.toDateString() ? clock : `${d.getMonth() + 1}/${d.getDate()} ${clock}`;
};

// The conversation of record (memory.db rows, the same history the backend sees) first; the in-flight
// backend answer as a tail row until its row lands; then the current Live session's subtitles. Rows the
// current Live session already wrote stay hidden while its subtitles are on screen, so nothing shows twice.
export function LiveTranscript({ rows, tail, sessionId, lines, open, muted, active, clock, onClose, embedded = false }: {
  embedded?: boolean; rows: Row[]; tail: string; sessionId: string | null; lines: Subtitle[]; open: boolean; muted: boolean; active: boolean; clock: string; onClose: () => void;
}) {
  const scroll = useRef<HTMLDivElement>(null);
  const mini = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [away, setAway] = useState(false);
  const latest = [...lines].reverse().find(line => line.role === 'assistant');
  const livePrefix = lines.length && sessionId ? `live:${sessionId}:` : null;
  const shown = livePrefix ? rows.filter(row => !row.id.startsWith(livePrefix)) : rows;
  const signature = `${shown.length ? shown[shown.length - 1].seq : 0}|${tail.length}|${lines.map(line => line.text).join('\n')}`;
  useLayoutEffect(() => {
    if (following.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
    if (mini.current) mini.current.scrollTo({ top: mini.current.scrollHeight, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
  }, [signature, open, muted]);
  const tailScroll = () => { following.current = true; setAway(false); scroll.current?.scrollTo({ top: scroll.current.scrollHeight, behavior: 'smooth' }); };
  const empty = !shown.length && !tail && !lines.length;
  return <>
    <div className={`transcript-reveal ${open ? 'is-open' : ''}`} inert={!open} aria-hidden={!open}>
      <section className="live-transcript glass" data-glass={embedded ? undefined : "20"} data-interactive aria-label="完整对话记录">
        <header><span className="transcript-session"><i/>{active ? 'LIVE' : '对话记录'}<small>{clock}</small></span><button aria-label="收起完整对话记录" onClick={onClose}><Minus size={16}/></button></header>
        <div className="transcript-history detail-body" ref={scroll} onScroll={() => { const el = scroll.current!; const atEnd = el.scrollHeight - el.clientHeight - el.scrollTop < 24; following.current = atEnd; setAway(!atEnd); }}>
          {shown.map(row => <div className={`transcript-turn ${row.source === 'allen' ? 'user' : 'assistant'}`} key={row.seq}><span>{who(row.source)} · {when(row.ts)}</span><p>{row.text}</p></div>)}
          {tail && <div className="transcript-turn assistant"><span>Jarvis · 正在回答</span><p>{tail}</p></div>}
          {lines.map((line, i) => <div className={`transcript-turn ${line.role}`} key={`${line.role}-${line.startMs}-${i}`}><span>{line.role === 'user' ? '你' : 'Jarvis · Live'}</span><p>{line.text}</p></div>)}
          {empty && <p className="transcript-empty">{active ? '正在听，直接开口即可。' : '当前还没有对话记录。'}</p>}
        </div>
        {away && <button className="transcript-latest" onClick={tailScroll}><ArrowDown size={13}/>回到最新</button>}
        <footer>{active ? '正在对话' : '完整记录 · 与 Jarvis 看到的历史相同'}</footer>
      </section>
    </div>
    <div className={`mini-caption-reveal ${!open && muted && active && latest?.text ? 'is-open' : ''}`} aria-hidden={open || !muted || !active}>
      <section className="mini-caption glass" data-glass="16" data-interactive aria-label="Jarvis 当前回复"><div ref={mini}><p>{latest?.text}</p></div></section>
    </div>
  </>;
}

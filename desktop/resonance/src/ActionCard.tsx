import { useState } from 'react';
import { ArrowUp, X } from '@phosphor-icons/react';
import { tr, type L, type Lang } from './companionSettings';
import './action-card.css';

// ADR 0061: the card Jarvis puts up before it does something with consequences. It shows exactly what will run;
// a letter can be edited in place, and the button sends what the card holds at that moment. Dismissing is the ×.
export interface Card { id: string; tool: string; action: string; source: string; letter: boolean; args: Record<string, unknown> }
export type Decide = (decision: 'accept' | 'reject', edits?: Record<string, string>) => void;

const MAX_ROWS = 5;
const SOURCE: Record<string, string> = { gmail: 'Gmail', notion: 'Notion', linear: 'Linear', github: 'GitHub', microsoft: 'Microsoft', hue: 'Hue' };
const FIELD: Record<string, L> = { message: ['Task', '任务'], cwd: ['Folder', '目录'], title: ['Title', '标题'], name: ['Name', '名称'], query: ['Search', '搜索'],
  description: ['Details', '说明'], content: ['Content', '内容'], path: ['Path', '路径'], messageIds: ['Emails', '邮件'], messageId: ['Email', '邮件'],
  addLabelIds: ['Add labels', '加标签'], removeLabelIds: ['Remove labels', '去标签'], draftId: ['Draft', '草稿'], threadId: ['Thread', '对话'] };
const shown = (value: unknown) => typeof value === 'string' ? value : Array.isArray(value) && value.every(v => typeof v === 'string') ? value.join(', ') : JSON.stringify(value);
const focusWindow = () => void window.jarvis?.focus(true);

export function ActionCard({ card, lang, onDecide }: { card: Card; lang: Lang; onDecide: Decide }) {
  const t = (l: L) => tr(lang, l);
  const args = card.args;
  const [subject, setSubject] = useState(String(args.subject ?? '')), [body, setBody] = useState(String(args.body ?? ''));
  const decide: Decide = (decision, edits) => { onDecide(decision, edits); void window.jarvis?.focus(false); };
  const bar = <div className="ac-bar">
    <span className="ac-label"><i/>{card.action}</span>
    {card.source && <span className="ac-meta">{SOURCE[card.source] ?? card.source}</span>}
    <button type="button" className="ac-x" aria-label={t(['Dismiss', '不要了'])} onClick={() => decide('reject')}><X size={10} weight="bold"/></button>
  </div>;
  if (card.letter) {
    const to = Array.isArray(args.to) ? args.to.join(', ') : String(args.to ?? '');
    const edits = { ...(subject !== args.subject && { subject }), ...(body !== args.body && { body }) };
    return <div className="ac" data-card={card.id}>
      {bar}
      <div className="ac-to">{t(['To', '发给'])}<span className="ac-chip" title={to}>{to}</span>{typeof args.threadId === 'string' && <em>{t(['reply', '回复'])}</em>}</div>
      <input className="ac-edit ac-subject" aria-label={t(['Subject', '主题'])} value={subject} onPointerDown={focusWindow} onChange={e => setSubject(e.target.value)}/>
      <textarea className="ac-edit ac-body" aria-label={t(['Body', '正文'])} value={body} rows={3} onPointerDown={focusWindow} onChange={e => setBody(e.target.value)}/>
      <div className="ac-foot"><span/>
        <button type="button" className="ac-go" onClick={() => decide('accept', edits)}>{t(['Send', '发送'])}<ArrowUp size={12} weight="bold"/></button></div>
    </div>;
  }
  const rows = Object.entries(args);
  return <div className="ac" data-card={card.id}>
    {bar}
    <dl className="ac-kv">{rows.slice(0, MAX_ROWS).map(([key, value]) =>
      <div key={key}><dt>{FIELD[key] ? t(FIELD[key]) : key}</dt><dd>{shown(value)}</dd></div>)}</dl>
    {rows.length > MAX_ROWS && <p className="ac-more">{t([`${rows.length - MAX_ROWS} more`, `还有 ${rows.length - MAX_ROWS} 项`])}</p>}
    <div className="ac-foot"><span/><button type="button" className="ac-go" onClick={() => decide('accept')}>{t(['Go ahead', '执行'])}</button></div>
  </div>;
}

// ADR 0062: an email Jarvis read whole, shown above its answer. The record is headers, a blank line, the body.
export function MailCard({ text, lang }: { text: string; lang: Lang }) {
  const [open, setOpen] = useState(false);
  const cut = text.indexOf('\n\n'), head = cut < 0 ? text : text.slice(0, cut), body = cut < 0 ? '' : text.slice(cut + 2);
  const field = (name: string) => head.split('\n').find(line => line.startsWith(`${name}: `))?.slice(name.length + 2) ?? '';
  const from = field('From'), who = from.replace(/\s*<[^>]+>\s*$/, '').replace(/^"|"$/g, '') || from;
  const date = new Date(field('Date')), at = Number.isNaN(date.getTime()) ? '' : date.toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  return <div className="mc">
    <div className="mc-top"><b title={from}>{who}</b><time>{at}</time></div>
    {field('Subject') && <p className="mc-subject">{field('Subject')}</p>}
    <p className={`mc-body${open ? ' is-open' : ''}`}>{body}</p>
    {body.split('\n').length > 6 || body.length > 360 ? <button type="button" className="mc-more" onClick={() => setOpen(v => !v)}>{open ? tr(lang, ['Less', '收起']) : tr(lang, ['Show all', '展开'])}</button> : null}
  </div>;
}

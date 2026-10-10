import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowUp, X } from '@phosphor-icons/react';
import { tr, type L, type Lang } from './companionSettings';
import type { TripCardData } from './pin';
import './action-card.css';

// ADR 0062: the card Jarvis puts up before it does something with consequences. It shows exactly what will run;
// a letter can be edited in place, and the button sends what the card holds at that moment. Dismissing is the ×.
export interface Card { id: string; tool: string; action: string; source: string; letter: boolean; args: Record<string, unknown> }
export type Decide = (decision: 'accept' | 'reject', edits?: Record<string, string>) => void;
export type ActionDraft = { subject: string; body: string };

const MAX_ROWS = 5;
const SOURCE: Record<string, string> = { gmail: 'Gmail', notion: 'Notion', linear: 'Linear', github: 'GitHub', microsoft: 'Microsoft', hue: 'Hue' };
const FIELD: Record<string, L> = { message: ['Task', '任务'], cwd: ['Folder', '目录'], title: ['Title', '标题'], name: ['Name', '名称'], query: ['Search', '搜索'],
  description: ['Details', '说明'], content: ['Content', '内容'], path: ['Path', '路径'], messageIds: ['Emails', '邮件'], messageId: ['Email', '邮件'],
  addLabelIds: ['Add labels', '加标签'], removeLabelIds: ['Remove labels', '去标签'], draftId: ['Draft', '草稿'], threadId: ['Thread', '对话'] };
const shown = (value: unknown) => typeof value === 'string' ? value : Array.isArray(value) && value.every(v => typeof v === 'string') ? value.join(', ') : JSON.stringify(value);
const focusWindow = () => void window.jarvis?.focus(true);

export function ActionCard({ card, lang, onDecide, draft, onDraft }: {
  card: Card; lang: Lang; onDecide: Decide; draft?: ActionDraft; onDraft?: (draft: ActionDraft) => void;
}) {
  const t = (l: L) => tr(lang, l);
  const args = card.args;
  const [localDraft, setLocalDraft] = useState<ActionDraft>(() => ({ subject: String(args.subject ?? ''), body: String(args.body ?? '') }));
  const { subject, body } = draft ?? localDraft;
  const edit = (change: Partial<ActionDraft>) => {
    const next = { subject, body, ...change };
    setLocalDraft(next); onDraft?.(next);
  };
  const acceptButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key !== 'Enter' || e.isComposing) return;
      const button = acceptButton.current, pane = button?.closest('.notch-pane');
      if (!button?.checkVisibility({ opacityProperty: true, visibilityProperty: true }) || pane && !pane.classList.contains('is-open')) return;
      const target = e.target instanceof HTMLElement ? e.target : null;
      if (target?.closest('button,input,textarea,[contenteditable]') && !button.closest('.ac')?.contains(target)) return;
      if (e.target instanceof HTMLTextAreaElement && !e.metaKey) return;
      e.preventDefault(); e.stopImmediatePropagation();
      if (e.metaKey && !e.repeat) button.click();
    };
    window.addEventListener('keydown', key, true);
    return () => window.removeEventListener('keydown', key, true);
  }, [card.id]);
  // The letter's box grows to its text (CSS caps it): `field-sizing: content` left it one line tall in Chrome 153.
  // It refits when its width settles, since the notch grows the card from narrower than its final width.
  const bodyBox = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = bodyBox.current;
    if (!el) return;
    const fit = () => { el.style.height = 'auto'; el.style.height = `${el.scrollHeight}px`; };
    fit();
    let width = el.clientWidth;
    const watch = new ResizeObserver(() => { if (el.clientWidth !== width) { width = el.clientWidth; fit(); } });
    watch.observe(el);
    return () => watch.disconnect();
  }, [body]);
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
      <input className="ac-edit ac-subject" aria-label={t(['Subject', '主题'])} value={subject} onPointerDown={focusWindow} onChange={e => edit({ subject: e.target.value })}/>
      <textarea ref={bodyBox} className="ac-edit ac-body" aria-label={t(['Body', '正文'])} value={body} rows={3} onPointerDown={focusWindow} onChange={e => edit({ body: e.target.value })}/>
      <div className="ac-foot"><span/>
        <button ref={acceptButton} type="button" className="ac-go" onClick={() => decide('accept', edits)}>{t(['Send', '发送'])}<kbd>⌘⏎</kbd><ArrowUp size={12} weight="bold"/></button></div>
    </div>;
  }
  const rows = Object.entries(args);
  return <div className="ac" data-card={card.id}>
    {bar}
    <dl className="ac-kv">{rows.slice(0, MAX_ROWS).map(([key, value]) =>
      <div key={key}><dt>{FIELD[key] ? t(FIELD[key]) : key}</dt><dd>{shown(value)}</dd></div>)}</dl>
    {rows.length > MAX_ROWS && <p className="ac-more">{t([`${rows.length - MAX_ROWS} more`, `还有 ${rows.length - MAX_ROWS} 项`])}</p>}
    <div className="ac-foot"><span/><button ref={acceptButton} type="button" className="ac-go" onClick={() => decide('accept')}>{t(['Go ahead', '执行'])}<kbd>⌘⏎</kbd></button></div>
  </div>;
}

// ADR 0063: an email Jarvis read whole, shown above its answer. The record is headers, a blank line, the body.
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
    {body.split('\n').length > 6 || body.length > 360 ? <button type="button" className="mc-more" onClick={() => setOpen(v => !v)}>{open ? tr(lang, ['Show less', '收起']) : tr(lang, ['Show all', '展开'])}</button> : null}
  </div>;
}

// ADR 0066: Jarvis asks for details it cannot go on without. One input per detail, or a row of choices; "Done" sends
// what is filled in (and the daemon remembers it), the × closes the card and nothing runs.
export interface QuestionField { label: string; choices?: string[]; value?: string }
// ADR 0205: a `trip` makes it the bus card (TripCards.tsx) instead of a question.
export interface Question { id: string; question: string; fields: QuestionField[]; trip?: TripCardData }
export type Answer = (answers: Record<string, string> | null) => void;

export function QuestionCard({ question, lang, onAnswer, draft, onDraft }: {
  question: Question; lang: Lang; onAnswer: Answer; draft?: Record<string, string>; onDraft?: (draft: Record<string, string>) => void;
}) {
  const t = (l: L) => tr(lang, l);
  const [localValues, setLocalValues] = useState<Record<string, string>>(() => Object.fromEntries(question.fields.map(f => [f.label, f.value ?? ''])));
  const values = draft ?? localValues;
  const set = (label: string, value: string) => {
    const next = { ...values, [label]: value };
    setLocalValues(next); onDraft?.(next);
  };
  const filled = Object.values(values).some(v => v.trim());
  const answer: Answer = answers => { onAnswer(answers); void window.jarvis?.focus(false); };
  const send = () => { if (filled) answer(values); };
  return <div className="ac" data-question={question.id}>
    <div className="ac-bar">
      <span className="ac-label"><i/>{t(['Jarvis needs more info', 'Jarvis 需要补充信息'])}</span>
      <button type="button" className="ac-x" aria-label={t(['Dismiss', '不填了'])} onClick={() => answer(null)}><X size={10} weight="bold"/></button>
    </div>
    <p className="qc-q">{question.question}</p>
    {question.fields.map(f => <div className="qc-field" key={f.label}>
      <span>{f.label}</span>
      {f.choices
        ? <span className="qc-choices" role="radiogroup" aria-label={f.label}>{f.choices.map(c =>
          <button type="button" role="radio" aria-checked={values[f.label] === c} className={`qc-choice${values[f.label] === c ? ' is-on' : ''}`} key={c}
            onClick={() => set(f.label, c)}>{c}</button>)}</span>
        : <input className="qc-input" aria-label={f.label} value={values[f.label] ?? ''} onPointerDown={focusWindow} onChange={e => set(f.label, e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) send(); }}/>}
    </div>)}
    <div className="ac-foot"><span/>
      <button type="button" className="ac-go" disabled={!filled} onClick={send}>{t(['Done', '好了'])}<ArrowUp size={12} weight="bold"/></button></div>
  </div>;
}

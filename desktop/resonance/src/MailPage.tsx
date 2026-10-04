import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Archive, ArrowSquareOut, ArrowUpRight, EnvelopeOpen, Trash } from '@phosphor-icons/react';
import { useT, type L } from './companionSettings';
import { demoMailText, postRoute, type Mail } from './homeData';
import { Lk, short } from './Markdown';
import { MorphText } from './MorphText';
import { useMailDraft } from './useMailDraft';

// The Dashboard's Mail page: every unread letter, and one letter opened in place with its body, its three actions and Jarvis's reply draft.
// The list arrives ranked (the home's order); the page only filters it. A letter that opens tells the daemon, so "reply to this" means this one.
export type MailFilter = 'all' | 'yes' | 'job';
export const MAIL_FILTERS: [MailFilter, L, (m: Mail) => boolean][] = [
  ['all', ['All', '全部'], () => true], ['yes', ['Reply', '要回'], m => m.reply === 'yes'], ['job', ['Job search', '找工作'], m => m.category === 'job_search'],
];
export type MailAct = 'trash' | 'archive' | 'read';

const pad = (n: number) => String(n).padStart(2, '0');
const hhmm = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
// Today: the time; any other day: month/day.
const stamp = (iso: string) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? '' : d.toDateString() === new Date().toDateString() ? hhmm(d) : `${d.getMonth() + 1}/${d.getDate()}`; };
const fullStamp = (iso: string) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? '' : `${d.getMonth() + 1}/${d.getDate()} ${hhmm(d)}`; };
const MARK: Record<string, L> = { yes: ['Reply', '要回'], fyi: ['FYI', '知会'], junk: ['Junk', '垃圾'] };
// Jev's 0-3 score in words; the two plain levels are drawn faint, an unrated letter has no level.
const level = (x?: number): [string, L] | null => x == null ? null : x >= 2.5 ? ['is-hot', ['Urgent', '紧急']] : x >= 1.5 ? ['is-mid', ['Important', '重要']] : x >= .5 ? ['', ['Normal', '普通']] : ['', ['Low', '不重要']];
const focusWindow = (event: { currentTarget: HTMLElement }) => { const el = event.currentTarget; void window.jarvis?.focus(true).then(() => el.focus({ preventScroll: true })); };

export function MailList({ mail, filter, onFilter, onOpen }: { mail: Mail[]; filter: MailFilter; onFilter: (f: MailFilter) => void; onOpen: (m: Mail) => void }) {
  const t = useT(), shown = mail.filter(MAIL_FILTERS.find(f => f[0] === filter)![2]);
  return <div className="mp-list">
    <div className="pg-sec mp-chips" role="group" aria-label={t(['Filter', '筛选'])}>{MAIL_FILTERS.map(([id, name, keep]) =>
      <button key={id} className="mp-chip" data-filter={id} aria-pressed={filter === id} onClick={() => onFilter(id)}>{t(name)}<em>{mail.filter(keep).length}</em></button>)}</div>
    <div className="pg-sec mp-rows">{shown.map(m => { const mark = m.junk ? 'junk' : m.reply, lv = level(m.importance);
      return <button key={m.id} className="mp-row" data-id={m.id} onClick={() => onOpen(m)}>
        <span className="mp-top"><b>{m.from}</b><time>{stamp(m.received)}</time></span>
        <span className="mp-line"><span className="mp-sub">{m.subject}</span>
          {mark && <em className={`mp-tag mp-mark is-${mark}`}>{t(MARK[mark])}</em>}{lv && <em className={`mp-tag ${lv[0]}`}>{t(lv[1])}</em>}</span></button>; })}</div>
    {!shown.length && <p className="pg-sec muted">{t(['Nothing here.', '这里没有邮件。'])}</p>}
  </div>;
}

type Detail = { thread_id?: string; address?: string; text: string };
// A bare address shows short; the daemon writes a link with words as `words (url)`, and that becomes a small arrow after the words.
const URL_AT = /( ?\(https?:\/\/[^\s()]+\)|https?:\/\/[^\s<>()"]*[^\s<>()".,;:!?])/;
const linked = (text: string) => text.split(URL_AT).map((part, i) => {
  if (i % 2 === 0) return part;
  const url = part.trim().replace(/^\((.*)\)$/, '$1');
  return url === part ? <Lk key={i} url={url}>{short(url)}</Lk> : <Lk key={i} url={url}><ArrowUpRight className="mp-go" size={12} weight="bold"/></Lk>;
});
// Mail as it comes, made readable: the invisible padding of HTML mail goes, a link in angle brackets (`click here <url>`, often broken
// over lines) sits in the sentence, a line that is only a link (an image or a picture button, no words) is counted instead of shown,
// lines lose their indent and trailing spaces, runs of blank lines close up to one, and an earlier letter quoted under it folds away.
const INVISIBLE = /[\u00a0\u00ad\u034f\u200b-\u200d\u2060\ufeff]/g;
const LONE_LINK = /^\(?https?:\/\/\S+?\)?$/;
const tidy = (text: string) => {
  let pictures = 0;
  const lines = text.replace(INVISIBLE, ' ').replace(/\s*<(https?:\/\/[^\s>]+)\s*>[ \t]*/g, ' $1 ').split('\n').map(line => line.trim())
    .filter(line => !(LONE_LINK.test(line) && ++pictures) && !/^[.·•|]$/.test(line));
  return { text: lines.join('\n').replace(/\n{3,}/g, '\n\n').trim(), pictures };
};
const QUOTED = /^(?:On .{4,200} wrote:|在.{2,200}写道[:：]|-{2,} ?Original Message ?-{2,}|>)/m;
function Body({ text, onGmail }: { text: string; onGmail: () => void }) {
  const t = useT(), [open, setOpen] = useState(false);
  const { text: all, pictures } = tidy(text), at = all.search(QUOTED), main = at > 0 ? all.slice(0, at).trim() : all, quoted = at > 0 ? all.slice(at) : '';
  return <>
    {main && <p className="mp-text">{linked(main)}</p>}
    {pictures > 0 && <button className="btn-text mp-pics" onClick={onGmail}>{t([`${pictures} picture${pictures > 1 ? 's' : ''} or picture links aren’t shown here; open it in Gmail to see them`, `还有 ${pictures} 张图片或图片链接没显示，在 Gmail 里能看到`])}</button>}
    {quoted && <button className="btn-text mp-quoted" aria-expanded={open} onClick={() => setOpen(v => !v)}>{open ? t(['Hide the quoted letter', '收起引用的信']) : t(['Show the quoted letter', '显示引用的信'])}</button>}
    {quoted && open && <p className="mp-text mp-quote">{linked(quoted)}</p>}
  </>;
}

export function MailLetter({ port, letter, onAct }: { port: string | null; letter: Mail; onAct: (kind: MailAct) => void }) {
  const t = useT();
  const [detail, setDetail] = useState<Detail | 'failed' | null>(null);
  useEffect(() => {
    if (!port) { setDetail({ text: demoMailText[letter.id] ?? '' }); return; }
    let stop = false;
    setDetail(null);
    fetch(`http://127.0.0.1:${port}/inherent/mail/${encodeURIComponent(letter.id)}`, { signal: AbortSignal.timeout(8000) })
      .then(r => r.ok ? r.json() as Promise<Detail> : Promise.reject(new Error(String(r.status)))).then(d => { if (!stop) setDetail(d); }, () => { if (!stop) setDetail('failed'); });
    return () => { stop = true; };
  }, [port, letter.id]);
  const body = detail && detail !== 'failed' ? detail : null, thread = body?.thread_id ?? letter.thread_id, address = body?.address ?? letter.address;

  // The daemon learns which letter is open (it forgets after 60 s, so it is said again every 20 s) and when none is. Failures change nothing here.
  const said = useRef({ id: letter.id, thread, sender: letter.from, subject: letter.subject });
  said.current = { id: letter.id, thread, sender: letter.from, subject: letter.subject };
  useEffect(() => {
    if (!port) return;
    const tell = () => { const s = said.current; void postRoute(port, '/inherent/focus', { kind: 'mail', id: s.id, thread_id: s.thread, sender: s.sender, subject: s.subject }).catch(() => {}); };
    tell();
    const every = setInterval(tell, 20_000);
    return () => clearInterval(every);
  }, [port, letter.id, thread]);
  useEffect(() => () => { if (port) void postRoute(port, '/inherent/focus', { kind: null }).catch(() => {}); }, [port, letter.id]);

  const d = useMailDraft(port, letter, thread);
  return <div className="mp-det">
    <header className="pg-sec mp-head">
      <span className="mp-who"><b>{letter.from}</b>{address && <small>&lt;{address}&gt;</small>}<time>{fullStamp(letter.received)}</time></span>
      <p className="mp-subject">{letter.subject}</p>
    </header>
    <div className="pg-sec mp-bar">
      <button className="btn btn-ghost" data-act="draft" onClick={() => void d.ask()}>{t(['Draft a reply with Jarvis', '让 Jarvis 起草回复'])}</button>
      <span className="mp-end">
        <button className="icon-btn" data-act="gmail" aria-label={t(['Open in Gmail', '在 Gmail 打开'])} title={t(['Open in Gmail', '在 Gmail 打开'])} onClick={() => void window.jarvis?.openMail?.(letter.id)}><ArrowSquareOut size={14}/></button>
        <button className="icon-btn" data-act="read" aria-label={t(['Mark as read', '标为已读'])} title={t(['Mark as read', '标为已读'])} onClick={() => onAct('read')}><EnvelopeOpen size={14}/></button>
        <button className="icon-btn" data-act="trash" aria-label={t(['Trash', '删除'])} title={t(['Trash', '删除'])} onClick={() => onAct('trash')}><Trash size={14}/></button>
        <button className="icon-btn" data-act="archive" aria-label={t(['Archive', '归档'])} title={t(['Archive', '归档'])} onClick={() => onAct('archive')}><Archive size={14}/></button></span>
    </div>
    <div className="pg-sec mp-body">{detail === 'failed' ? <p className="muted">{t(['Can’t read this one yet. You can open it in Gmail.', '正文还读不到，可以在 Gmail 里看'])}</p>
      : body ? <Body text={body.text} onGmail={() => void window.jarvis?.openMail?.(letter.id)}/> : <p className="muted">{t(['Loading…', '正在读…'])}</p>}</div>
    <DraftArea d={d}/>
  </div>;
}

// A text box that grows to its text.
function Grow({ value, label, onChange, className }: { value: string; label: string; onChange: (value: string) => void; className?: string }) {
  const box = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => { const el = box.current!; el.style.height = 'auto'; el.style.height = `${el.scrollHeight}px`; }, [value]);
  return <textarea ref={box} className={`mp-edit ${className ?? ''}`} aria-label={label} value={value} rows={3} onPointerDown={focusWindow} onChange={e => onChange(e.target.value)}/>;
}

// Jarvis's draft: his rewrites morph in over the old words, your edits are kept and saved as you type. Sending asks the daemon to
// raise the usual confirmation, which becomes the last step here: the letter as it will go, still editable, then 确认发送.
type MailDraft = ReturnType<typeof useMailDraft>;
function DraftArea({ d }: { d: MailDraft }) {
  const t = useT();
  if (d.done) return <p className="pg-sec muted mp-sent">{t(['Sent.', '已发送'])}</p>;
  if (d.card) return <Confirm d={d} card={d.card}/>;
  if (d.off || !d.draft && !d.writing && !d.failed) return null; // the daemon has no drafts: only the button is left
  return <section className="pg-sec mp-draft" aria-label={t(['Reply draft', '回复草稿'])}>
    {d.draft && <>
      <div className="mp-to"><span>{t(['To', '发给'])}</span><em>{d.draft!.to}</em></div>
      <input className="mp-edit mp-subj" aria-label={t(['Subject', '主题'])} value={d.fields.subject} onPointerDown={focusWindow} onChange={e => d.edit({ subject: e.target.value })}/>
      {d.morph ? <MorphText className="mp-edit mp-bodytext" from={d.morph.from} to={d.morph.to} onDone={d.endMorph}/>
        : <Grow className="mp-bodytext" label={t(['Body', '正文'])} value={d.fields.body} onChange={body => d.edit({ body })}/>}
      <div className="mp-foot"><button className="btn btn-text" data-act="discard" onClick={() => void d.discard()}>{t(['Discard', '不要了'])}</button>
        <button className="btn btn-glow" data-act="send" disabled={d.writing || d.sending} onClick={() => void d.send()}>{t(['Send', '发送'])}</button></div></>}
    {d.writing && <p className="muted mp-quiet">{t(['Jarvis is writing…', 'Jarvis 在写…'])}</p>}
    {d.sending && <p className="muted mp-quiet">{t(['Jarvis is getting it ready to send…', 'Jarvis 在准备发送…'])}</p>}
    {d.failed && <p className="muted is-warm">{t(['That didn’t go through. Try again.', '没成功，请再试一次。'])}</p>}
  </section>;
}
function Confirm({ d, card }: { d: MailDraft; card: NonNullable<MailDraft['card']> }) {
  const t = useT(), a = card.args;
  const [subject, setSubject] = useState(String(a.subject ?? '')), [body, setBody] = useState(String(a.body ?? ''));
  return <section className="pg-sec mp-draft mp-confirm" aria-label={t(['Confirm sending', '确认发送'])}>
    <div className="mp-to"><span>{t(['To', '发给'])}</span><em>{Array.isArray(a.to) ? a.to.join(', ') : String(a.to ?? '')}</em></div>
    <input className="mp-edit mp-subj" aria-label={t(['Subject', '主题'])} value={subject} onPointerDown={focusWindow} onChange={e => setSubject(e.target.value)}/>
    <Grow className="mp-bodytext" label={t(['Body', '正文'])} value={body} onChange={setBody}/>
    <div className="mp-foot"><button className="btn btn-text" data-act="cancel" onClick={() => void d.decide('reject')}>{t(['Cancel', '取消'])}</button>
      <button className="btn btn-glow" data-act="confirm" onClick={() => void d.decide('accept', { subject, body })}>{t(['Confirm and send', '确认发送'])}</button></div>
    {d.failed && <p className="muted is-warm">{t(['That didn’t go through. Try again.', '没成功，请再试一次。'])}</p>}
  </section>;
}

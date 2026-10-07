import { useState } from 'react';
import { CaretRight, Trash } from '@phosphor-icons/react';
import { useT, type L } from './companionSettings';
import { postRoute } from './homeData';

// The Dashboard's job ledger: GET /inherent/jobs, one row per company, its mails opened in place. The daemon (job_mail) keeps
// the facts, never the bodies; a deleted mail is hidden there (POST /inherent/jobs/{message_id}/delete). The kind chips
// are shared with the notch's mail card and digest.
export type JobMailRow = { message_id: string; kind: string; received_at: string; subject: string; event_at?: string | null; event_text?: string | null };
// `skipped` (GET /inherent/jobs, up to 50, newest first, absent on older daemons): mail the triage held back as not job, whatever its job-likelihood.
export type Skipped = { message_id: string; received_at: string; sender_name?: string; sender_domain?: string; subject: string; p_job?: number };
// `rules` (GET /inherent/jobs, absent on older daemons): the standing alert rules the daemon is running, shown as one line each; `linkedin_alerts` is `ledger_only` or `card_sound`; `exclude_domains` is the sender domains kept out of job mail, comma-separated.
export type JobRule = { id: string; value: string };
// `time_spent` / `time_total_s` (GET /inherent/jobs, ADR 0161, absent on older daemons): seconds per local day on that company's job pages over the last 14 days, from TimeSink; `job_site_other_s` (top level) is job-site time that names no company.
export type JobGroup = { company: string; role?: string; kind: string; last_at: string; next_event_at?: string | null; count: number; mails: JobMailRow[]; time_spent?: { day: string; seconds: number }[]; time_total_s?: number };

const KINDS: Record<string, [string, L]> = {
  offer: ['is-offer', ['Offer', 'Offer']], interview: ['is-interview', ['Interview', '面试']], rejection: ['is-rejection', ['Rejection', '拒信']],
  receipt: ['is-receipt', ['Received', '已收到']], job_other: ['is-other', ['Other', '其他']], other: ['is-other', ['Account', '账号通知']],
};
export const jobKind = (kind?: string) => KINDS[kind ?? ''] ?? KINDS.job_other;
const pad = (n: number) => String(n).padStart(2, '0');
// Today: the time; any other day: month/day. `withTime`: month/day and the time.
export function jobStamp(iso?: string | null, withTime = false) {
  const d = new Date(iso ?? ''); if (Number.isNaN(d.getTime())) return '';
  const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return withTime ? `${d.getMonth() + 1}/${d.getDate()} ${hhmm}` : d.toDateString() === new Date().toDateString() ? hhmm : `${d.getMonth() + 1}/${d.getDate()}`;
}

// 「1 小时 20 分」: whole minutes, rounded; under a minute is said as such.
export const spentText = (seconds: number): L => {
  const m = Math.round(seconds / 60), h = Math.floor(m / 60), r = m % 60;
  if (m < 1) return ['under 1 min', '不到 1 分'];
  return [`${h ? `${h} h ` : ''}${r || !h ? `${r} min` : ''}`.trim(), `${h ? `${h} 小时 ` : ''}${r || !h ? `${r} 分` : ''}`.trim()];
};
const dayLabel = (day: string) => { const [, m, d] = day.split('-'); return `${Number(m)}/${Number(d)}`; };

export function JobsPage({ port, ledger, skipped, rules = [], otherS = 0, onChanged }: { port: string; ledger: JobGroup[]; skipped: Skipped[]; rules?: JobRule[]; otherS?: number; onChanged: () => void }) {
  const t = useT(), [open, setOpen] = useState(''), [confirm, setConfirm] = useState(''), [gone, setGone] = useState<string[]>([]), [failed, setFailed] = useState(false);
  // POST /inherent/jobs/{id}/flag { reaction: 'should_alert' } says a held-back mail was job mail after all; a 404 means the daemon has no such route, and the buttons go.
  const [skipOpen, setSkipOpen] = useState(false), [flagged, setFlagged] = useState<string[]>([]), [noFlag, setNoFlag] = useState(false);
  const held = skipped.filter(m => !flagged.includes(m.message_id));
  const flag = async (id: string) => {
    setFailed(false); setFlagged(v => [...v, id]);
    try { await postRoute(port, `/inherent/jobs/${encodeURIComponent(id)}/flag`, { reaction: 'should_alert' }); onChanged(); }
    catch (e) { setFlagged(v => v.filter(x => x !== id)); if (/ 404$/.test(String(e))) setNoFlag(true); else setFailed(true); }
  };
  const groups = ledger.map(g => ({ ...g, mails: g.mails.filter(m => !gone.includes(m.message_id)) })).filter(g => g.mails.length);
  const remove = async (id: string) => {
    setConfirm(''); setFailed(false); setGone(v => [...v, id]);
    try { await postRoute(port, `/inherent/jobs/${encodeURIComponent(id)}/delete`, {}); onChanged(); }
    catch { setGone(v => v.filter(x => x !== id)); setFailed(true); }
  };
  const linkedin = rules.find(r => r.id === 'linkedin_alerts')?.value, excluded = rules.find(r => r.id === 'exclude_domains')?.value;
  return <div className="jp">
    {linkedin && <p className="pg-sec muted jp-rule" data-rule="linkedin_alerts">{linkedin === 'ledger_only' ? t(['LinkedIn job alerts: ledger only, no alert', 'LinkedIn 职位提醒：只进账本，不提醒']) : t(['LinkedIn job alerts: a card with sound', 'LinkedIn 职位提醒：出卡片带提示音'])}</p>}
    {excluded && <p className="pg-sec muted jp-rule" data-rule="exclude_domains">{t([`Not counted: mail from ${excluded}`, `不计入：来自 ${excluded} 的邮件`])}</p>}
    {otherS > 0 && <p className="pg-sec muted jp-other" data-other>{t(['Other job sites', '其他求职网站'])} {t(['spent', '花了'])} {t(spentText(otherS))}</p>}
    {failed && <p className="pg-sec muted is-warm" role="alert">{t(['That didn’t go through. Try again.', '没成功，请再试一次。'])}</p>}
    {!groups.length && <p className="pg-sec muted">{t(['No job mail yet. Jarvis adds it here as it comes in.', '还没有求职邮件，收到了会记在这里。'])}</p>}
    {groups.map((g, i) => { const key = `${g.company}|${g.role ?? ''}|${i}`, [cls, name] = jobKind(g.kind), on = open === key;
      return <section className="pg-sec jp-g" key={key} data-company={g.company}>
        <button className="jp-top" aria-expanded={on} onClick={() => setOpen(on ? '' : key)}>
          <CaretRight size={10} weight="bold" className="jp-caret"/>
          <span className="jp-name"><b>{g.company}</b>{g.role && <small>{g.role}</small>}</span>
          <em className={`jk ${cls}`}>{t(name)}</em></button>
        <span className="jp-meta"><span>{t(['Last mail', '最近来信'])} {jobStamp(g.last_at, true)}</span>
          {g.next_event_at && <span className="jp-next">{t(['Next', '下一个'])} {jobStamp(g.next_event_at, true)}</span>}
          <span>{t([`${g.mails.length} mail${g.mails.length > 1 ? 's' : ''}`, `${g.mails.length} 封`])}</span>
          {(g.time_total_s ?? 0) > 0 && <span className="jp-time" data-time>{t(['Spent', '花了'])} {t(spentText(g.time_total_s!))}</span>}</span>
        {on && (g.time_spent?.length ?? 0) > 0 && <p className="jp-days" data-days>{[...g.time_spent!].reverse().map(d => `${dayLabel(d.day)} ${t(spentText(d.seconds))}`).join(' · ')}</p>}
        {on && <ul className="jp-mails">{g.mails.map(m => { const [mc, mn] = jobKind(m.kind);
          return <li key={m.message_id} data-id={m.message_id}>
            <span className="jp-sub"><em className={`jk ${mc}`}>{t(mn)}</em><span title={m.subject}>{m.subject}</span></span>
            <time>{jobStamp(m.received_at, true)}</time>
            <button className="icon-btn jp-del" data-act="delete" aria-label={t(['Delete from the ledger', '从记录里删除'])} title={t(['Delete from the ledger', '从记录里删除'])} onClick={() => setConfirm(m.message_id)}><Trash size={13}/></button>
            {confirm === m.message_id && <span className="jp-sure">{t(['Delete this mail from the ledger?', '从记录里删掉这封？'])}
              <button className="btn-text" data-act="no" onClick={() => setConfirm('')}>{t(['Keep', '留着'])}</button>
              <button className="btn-text is-danger" data-act="yes" onClick={() => void remove(m.message_id)}>{t(['Delete', '删除'])}</button></span>}
            {m.event_text && <small className="jp-event">{m.event_text}</small>}
          </li>; })}</ul>}
      </section>; })}
    {held.length > 0 && <section className="pg-sec jp-skip">
      <button className="jp-top" aria-expanded={skipOpen} onClick={() => setSkipOpen(v => !v)}><CaretRight size={10} weight="bold" className="jp-caret"/>
        <span className="jp-name"><b>{t(['Held back as not job', '被判成不相关的可疑邮件'])}</b></span><em className="jp-count">{held.length}</em></button>
      {skipOpen && <ul className="jp-mails">{held.map(m => <li key={m.message_id} data-id={m.message_id}>
        <span className="jp-sk-sub" title={m.subject}>{m.subject}</span>
        <span className="jp-sk-meta"><b>{m.sender_name || m.sender_domain}</b>
          <small title={jobStamp(m.received_at, true)}>{m.sender_domain}</small>
          <time title={t(['Job likelihood', '像求职邮件的程度'])}>{m.p_job != null ? `${Math.round(m.p_job * 100)}%` : ''}</time>
          {!noFlag && <button className="btn-text" data-act="flag" onClick={() => void flag(m.message_id)}>{t(['Actually job mail', '这其实相关'])}</button>}</span>
      </li>)}</ul>}
    </section>}
  </div>;
}

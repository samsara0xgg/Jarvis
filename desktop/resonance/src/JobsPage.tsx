import { useEffect, useState } from 'react';
import { ArrowSquareOut, CaretDown, CaretRight, PencilSimple, Trash } from '@phosphor-icons/react';
import { useT, type L } from './companionSettings';
import { postRoute } from './homeData';
import { openLink } from './Markdown';

// The Dashboard's job ledger: GET /inherent/jobs. With `applications` (ADR 0177) it is a tracker, one card per job applied to, its timeline, interview, links, mails and note opened in place (ADR 0182); without it (an older daemon) one section per company. The daemon (job_mail) keeps
// the facts, never the bodies; a deleted mail is hidden there (POST /inherent/jobs/{message_id}/delete). The kind chips
// are shared with the notch's mail card and digest.
export type JobMailRow = { message_id: string; thread_id?: string | null; kind: string; received_at: string; subject: string; event_at?: string | null; event_text?: string | null };
// `skipped` (GET /inherent/jobs, up to 50, newest first, absent on older daemons): mail the triage held back as not job, whatever its job-likelihood.
export type Skipped = { message_id: string; received_at: string; sender_name?: string; sender_domain?: string; subject: string; p_job?: number };
// `rules` (GET /inherent/jobs, absent on older daemons): the standing alert rules the daemon is running, shown as one line each; `linkedin_alerts` is `ledger_only` or `card_sound`; `exclude_domains` is the sender domains kept out of job mail, comma-separated.
export type JobRule = { id: string; value: string };
// `time_spent` / `time_total_s` (GET /inherent/jobs, ADR 0161, absent on older daemons): seconds per local day on that company's job pages over the last 14 days, from TimeSink; `job_site_other_s` (top level) is job-site time that names no company.
export type JobGroup = { company: string; role?: string; kind: string; last_at: string; next_event_at?: string | null; count: number; mails: JobMailRow[]; time_spent?: { day: string; seconds: number }[]; time_total_s?: number };

// `applications` (GET /inherent/jobs, ADR 0177, absent on older daemons): one per job applied to, from the mail or added by hand. `status_auto` is false once Allen set the status himself; `source` is `mail` or `manual`. Edits: POST /inherent/jobs/applications/{id} { status?, applied_at?, note?, hidden? }; a new row: POST /inherent/jobs/applications { company, role?, applied_at?, status? }.
// ADR 0182 (absent on a daemon before it): `timeline` is the steps its mails show, oldest first (`future` is an interview still ahead; `message_id` the mail the step came from, null for a manual applied date); `interview` is read from the interview mails' body starts, every field null or empty when the mail does not say; `links` are https addresses from the mails.
export type JobStep = { kind: 'applied' | 'interview_invite' | 'interview' | 'offer' | 'rejection'; at?: string | null; future: boolean; message_id?: string | null };
export type JobInterview = { at?: string | null; mode: 'online' | 'onsite' | null; platform: string | null; join_url: string | null; location: string | null; interviewers: string[] };
// ADR 0186 (absent on a daemon before it, null when nothing is armed): what Jarvis set for the interview ahead; `at` is that time, `cancelled` is Allen's undo of it.
export type JobReminders = { at: string; evening: boolean; before: boolean; evening_at: string; before_min: number; outlook: boolean; cancelled: boolean };
export type JobApplication = { id: string; company: string; role: string; status: string; status_auto: boolean; applied_at?: string | null; last_at?: string | null; next_event_at?: string | null; count: number; mails: JobMailRow[]; note: string; source: 'mail' | 'manual'; timeline?: JobStep[]; interview?: JobInterview | null; links?: { portal_url: string | null; posting_url: string | null }; reminders?: JobReminders | null };

const STATUSES: Record<string, L> = { applied: ['Applied', '已投'], interviewing: ['Interviewing', '面试中'], offer: ['Offer', 'Offer'], rejected: ['Rejected', '拒了'], no_reply: ['No reply', '没回音'] };
// "2026-09-12" is a day, not a moment: read it as written, so a western time zone does not step it back.
const dayStamp = (v?: string | null) => {
  const day = /^\d{4}-(\d\d)-(\d\d)$/.exec(v ?? ''); if (day) return `${Number(day[1])}/${Number(day[2])}`;
  const d = new Date(v ?? ''); return Number.isNaN(d.getTime()) ? '' : `${d.getMonth() + 1}/${d.getDate()}`;
};

const STEPS: Record<JobStep['kind'], L> = { applied: ['applied', '投递'], interview_invite: ['invited', '约面试'], interview: ['interview', '面试'], offer: ['offer', 'Offer'], rejection: ['rejected', '拒信'] };
// Whole calendar days from today to a moment (0 today, negative once past).
const daysTo = (iso: string) => { const d = new Date(iso), n = new Date(); d.setHours(0, 0, 0, 0); n.setHours(0, 0, 0, 0); return Math.round((d.getTime() - n.getTime()) / 86_400_000); };
const inDays = (n: number): L => n <= 0 ? ['today', '今天'] : [`in ${n} day${n > 1 ? 's' : ''}`, `还有 ${n} 天`];

const KINDS: Record<string, [string, L]> = {
  offer: ['is-offer', ['Offer', 'Offer']], interview: ['is-interview', ['Interview', '面试']], rejection: ['is-rejection', ['Rejection', '拒信']],
  receipt: ['is-receipt', ['Received', '已收到']], job_other: ['is-other', ['Other', '其他']], other: ['is-other', ['Account', '账号通知']],
  reminder: ['is-interview', ['Reminder', '提醒']],
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

// A company's row has no id of its own; the view report and her page-turning name it by company and role.
export const jobKey = (g: { company: string; role?: string }) => `${g.company}|${g.role ?? ''}`;

export function JobsPage({ port, ledger, applications, skipped, rules = [], otherS = 0, onChanged, open, onOpen }: { port: string; ledger: JobGroup[]; applications?: JobApplication[]; skipped: Skipped[]; rules?: JobRule[]; otherS?: number; onChanged: () => void; open: string; onOpen: (id: string) => void }) {
  const t = useT(), setOpen = onOpen, [confirm, setConfirm] = useState(''), [gone, setGone] = useState<string[]>([]), [failed, setFailed] = useState(false);
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
  // The tracker: a status shown at once while its post is in flight, until the next answer from the daemon replaces it.
  const [pending, setPending] = useState<Record<string, string>>({}), [adding, setAdding] = useState(false), [form, setForm] = useState({ company: '', role: '', applied_at: '', status: 'applied' });
  useEffect(() => setPending({}), [applications]);
  const edit = async (id: string, body: Record<string, unknown>) => {
    setFailed(false);
    try { await postRoute(port, `/inherent/jobs/applications/${encodeURIComponent(id)}`, body); onChanged(); }
    catch { setPending(v => Object.fromEntries(Object.entries(v).filter(([k]) => k !== id))); setFailed(true); }
  };
  const add = async () => {
    setFailed(false);
    try { await postRoute(port, '/inherent/jobs/applications', { company: form.company.trim(), role: form.role.trim(), ...(form.applied_at ? { applied_at: form.applied_at } : {}), status: form.status }); setAdding(false); setForm({ company: '', role: '', applied_at: '', status: 'applied' }); onChanged(); }
    catch { setFailed(true); }
  };
  // POST /inherent/jobs/applications/{id}/cancel-reminders: both reminders cancelled, the Outlook event deleted, not armed again for that time.
  const cancelReminders = async (id: string) => {
    setFailed(false);
    try { await postRoute(port, `/inherent/jobs/applications/${encodeURIComponent(id)}/cancel-reminders`, {}); onChanged(); }
    catch { setFailed(true); }
  };
  const remindersOf = (r: JobReminders) => {
    if (r.cancelled) return t(['Reminders cancelled for this time', '这次面试的提醒已取消']);
    const set = [r.evening && t([`evening before ${r.evening_at}`, `前一晚 ${r.evening_at}`]), r.before && t([`${r.before_min} min before`, `开始前 ${r.before_min} 分钟`])].filter(Boolean).join(t([', ', '、']));
    return [set && t([`Reminders set: ${set}`, `已设提醒：${set}`]), r.outlook && t(['written to Outlook calendar', '已写入 Outlook 日历'])].filter(Boolean).join(' · ');
  };
  const rows = (applications ?? []).map(a => ({ ...a, mails: a.mails.filter(m => !gone.includes(m.message_id)) })).filter(a => a.source === 'manual' || a.mails.length);
  const gmail = (m: JobMailRow) => void window.jarvis?.openMail?.(m.thread_id || m.message_id);
  const mailItem = (m: JobMailRow) => { const [mc, mn] = jobKind(m.kind);
    return <li key={m.message_id} data-id={m.message_id}>
      <span className="jp-sub"><em className={`jk ${mc}`}>{t(mn)}</em><span title={m.subject}>{m.subject}</span></span>
      <time>{jobStamp(m.received_at, true)}</time>
      <button className="icon-btn" data-act="gmail" aria-label={t(['Open in Gmail', '在 Gmail 打开'])} title={t(['Open in Gmail', '在 Gmail 打开'])} onClick={() => gmail(m)}><ArrowSquareOut size={13}/></button>
      <button className="icon-btn jp-del" data-act="delete" aria-label={t(['Delete from the ledger', '从记录里删除'])} title={t(['Delete from the ledger', '从记录里删除'])} onClick={() => setConfirm(m.message_id)}><Trash size={13}/></button>
      {confirm === m.message_id && <span className="jp-sure">{t(['Delete this mail from the ledger?', '从记录里删掉这封？'])}
        <button className="btn-text" data-act="no" onClick={() => setConfirm('')}>{t(['Keep', '留着'])}</button>
        <button className="btn-text is-danger" data-act="yes" onClick={() => void remove(m.message_id)}>{t(['Delete', '删除'])}</button></span>}
      {m.event_text && <small className="jp-event">{m.event_text}</small>}
    </li>; };
  // Collapsed, a card's second line: the role, the day applied, then what matters for its status.
  const lineOf = (a: JobApplication, status: string) => {
    const applied = dayStamp(a.applied_at), last = a.last_at ? new Date(a.last_at).getTime() : NaN, ended = a.timeline?.find(x => x.kind === 'rejection')?.at;
    const quiet = Math.max(1, Math.floor((Date.now() - last) / 86_400_000));
    return [a.role, applied && t([`Applied ${applied}`, `${applied} 投递`]),
      (status === 'interviewing' || status === 'offer') && a.next_event_at && t([`Interview ${jobStamp(a.next_event_at, true)}`, `面试 ${jobStamp(a.next_event_at, true)}`]),
      status === 'no_reply' && !Number.isNaN(last) && t([`No reply for ${quiet} day${quiet > 1 ? 's' : ''}`, `已 ${quiet} 天没回音`]),
      status === 'rejected' && ended && t([`Rejected ${dayStamp(ended)}`, `${dayStamp(ended)} 拒信`])].filter(Boolean).join(' · ');
  };
  const whenOf = (iv: JobInterview) => {
    const d = new Date(iv.at ?? ''), set = !Number.isNaN(d.getTime());
    return [set && `${d.getMonth() + 1}/${d.getDate()} ${t([d.toLocaleDateString('en-US', { weekday: 'short' }), d.toLocaleDateString('zh-CN', { weekday: 'short' })])} ${pad(d.getHours())}:${pad(d.getMinutes())}`,
      iv.mode && t(iv.mode === 'online' ? ['Online', '线上'] : ['On site', '线下']), iv.mode === 'onsite' ? iv.location : iv.platform].filter(Boolean).join(' · ');
  };
  const linkedin = rules.find(r => r.id === 'linkedin_alerts')?.value, excluded = rules.find(r => r.id === 'exclude_domains')?.value;
  return <div className="jp">
    {linkedin && <p className="pg-sec muted jp-rule" data-rule="linkedin_alerts">{linkedin === 'ledger_only' ? t(['LinkedIn job alerts: ledger only, no alert', 'LinkedIn 职位提醒：只进账本，不提醒']) : t(['LinkedIn job alerts: a card with sound', 'LinkedIn 职位提醒：出卡片带提示音'])}</p>}
    {excluded && <p className="pg-sec muted jp-rule" data-rule="exclude_domains">{t([`Not counted: mail from ${excluded}`, `不计入：来自 ${excluded} 的邮件`])}</p>}
    {otherS > 0 && <p className="pg-sec muted jp-other" data-other>{t(['Other job sites', '其他求职网站'])} {t(['spent', '花了'])} {t(spentText(otherS))}</p>}
    {failed && <p className="pg-sec muted is-warm" role="alert">{t(['That didn’t go through. Try again.', '没成功，请再试一次。'])}</p>}
    {applications && <>
      <div className="pg-sec jp-bar"><button className="btn-text" data-act="add" aria-expanded={adding} onClick={() => setAdding(v => !v)}>{t(['Add', '添加'])}</button></div>
      {adding && <form className="pg-sec jp-form" onSubmit={e => { e.preventDefault(); if (form.company.trim()) void add(); }}>
        <input data-f="company" required value={form.company} placeholder={t(['Company', '公司'])} aria-label={t(['Company', '公司'])} onChange={e => setForm(f => ({ ...f, company: e.target.value }))}/>
        <input data-f="role" value={form.role} placeholder={t(['Role', '职位'])} aria-label={t(['Role', '职位'])} onChange={e => setForm(f => ({ ...f, role: e.target.value }))}/>
        <input data-f="applied_at" type="date" value={form.applied_at} aria-label={t(['Applied', '投递日期'])} onChange={e => setForm(f => ({ ...f, applied_at: e.target.value }))}/>
        <select data-f="status" value={form.status} aria-label={t(['Status', '状态'])} onChange={e => setForm(f => ({ ...f, status: e.target.value }))}>{Object.entries(STATUSES).map(([k, name]) => <option key={k} value={k}>{t(name)}</option>)}</select>
        <button className="btn-text" type="button" data-act="cancel" onClick={() => setAdding(false)}>{t(['Cancel', '取消'])}</button>
        <button className="btn-text" type="submit" data-act="save" disabled={!form.company.trim()}>{t(['Add', '添加'])}</button>
      </form>}
      {!rows.length && <p className="pg-sec muted">{t(['No applications yet. Jarvis adds them from your mail, or add one here.', '还没有投递记录。Jarvis 会从邮件里记下，你也可以在这里添加。'])}</p>}
      {rows.length > 0 && <div className="jc-list">{rows.map(a => { const on = open === a.id, status = pending[a.id] ?? a.status, spent = ledger.find(g => g.company.toLowerCase() === a.company.toLowerCase())?.time_total_s ?? 0,
          iv = a.interview, steps = a.timeline ?? [], links = a.links, shortcuts = !!(links?.portal_url || links?.posting_url);
        return <article key={a.id} className="jc" data-id={a.id} data-company={a.company} data-vid={jobKey(a)}>
          <div className="jc-head">
            <button className="jp-top" aria-expanded={on} onClick={() => setOpen(on ? '' : a.id)}><CaretRight size={10} weight="bold" className="jp-caret"/><b title={a.company}>{a.company}</b></button>
            <span className="jc-st"><label className={`jc-pill is-${status}`}><span>{t(STATUSES[status] ?? STATUSES.applied)}</span><CaretDown size={8} weight="bold"/>
              <select data-act="status" value={status} aria-label={t(['Status', '状态'])} onChange={e => { const v = e.target.value; setPending(p => ({ ...p, [a.id]: v })); void edit(a.id, { status: v }); }}>{Object.entries(STATUSES).map(([k, name]) => <option key={k} value={k}>{t(name)}</option>)}</select></label>
              {!a.status_auto && <PencilSimple size={10} className="jp-hand" data-hand aria-label={t(['Set by you', '你手动设的'])}/>}</span>
          </div>
          <p className="jc-line" data-line>{lineOf(a, status)}</p>
          {on && <div className="jc-x">
            {steps.length > 0 && <div className="jc-row" data-row="timeline"><b>{t(['Progress', '进度'])}</b><ol className="jc-tl">{steps.map((st, i) => { const [en, zh] = st.at ? inDays(daysTo(st.at)) : ['', ''];
              return <li key={i} className={st.future ? 'is-future' : ''} data-step={st.kind}><i/><span>{dayStamp(st.at)} {t(STEPS[st.kind])}{st.future && st.at && <small> {t([`(${en})`, `（${zh}）`])}</small>}</span></li>; })}</ol></div>}
            {iv && <div className="jc-row" data-row="interview"><b>{t(['Interview', '面试'])}</b><div>
              <p>{whenOf(iv)}{iv.join_url && <button className="jc-join" data-act="join" onClick={() => openLink(iv.join_url!)}>{t(['Join meeting', '加入会议'])}</button>}</p>
              {iv.interviewers.length > 0 && <p className="jc-people" data-interviewers>{t(['Interviewer', '面试官'])} {iv.interviewers.join(', ')}</p>}
              {a.reminders && remindersOf(a.reminders) && <p className="jc-people" data-reminders>{remindersOf(a.reminders)}{!a.reminders.cancelled && <button className="btn-text" data-act="cancel-reminders" onClick={() => void cancelReminders(a.id)}>{t(['Cancel reminders', '取消提醒'])}</button>}</p>}</div></div>}
            {shortcuts && <div className="jc-row" data-row="links"><b>{t(['Links', '链接'])}</b><div>
              {links?.portal_url && <button className="btn-text" data-act="portal" onClick={() => openLink(links.portal_url!)}>{t(['Application status', '查申请进度'])}</button>}
              {links?.posting_url && <button className="btn-text" data-act="posting" onClick={() => openLink(links.posting_url!)}>{t(['Job posting', '职位原帖'])}</button>}</div></div>}
            {spent > 0 && <p className="jp-days" data-time>{t(['Spent', '花了'])} {t(spentText(spent))}</p>}
            {a.mails.length > 0 && <div data-row="mails"><p className="jc-cap">{t([`${a.mails.length} mail${a.mails.length > 1 ? 's' : ''}`, `邮件 ${a.mails.length} 封`])}</p><ul className="jp-mails">{a.mails.map(mailItem)}</ul></div>}
            <textarea className="jp-note" data-act="note" rows={2} defaultValue={a.note} placeholder={t(['Note', '备注'])} aria-label={t(['Note', '备注'])} onBlur={e => { if (e.target.value !== a.note) void edit(a.id, { note: e.target.value }); }}/>
            <button className="btn-text jp-hide" data-act="hide" onClick={() => void edit(a.id, { hidden: true })}>{t(['Remove from the list', '从列表里移除'])}</button>
          </div>}
        </article>; })}</div>}
    </>}
    {!applications && !groups.length && <p className="pg-sec muted">{t(['No job mail yet. Jarvis adds it here as it comes in.', '还没有求职邮件，收到了会记在这里。'])}</p>}
    {!applications && groups.map((g, i) => { const key = `${g.company}|${g.role ?? ''}|${i}`, [cls, name] = jobKind(g.kind), on = open === key;
      return <section className="pg-sec jp-g" key={key} data-company={g.company} data-vid={jobKey(g)}>
        <button className="jp-top" aria-expanded={on} onClick={() => setOpen(on ? '' : key)}>
          <CaretRight size={10} weight="bold" className="jp-caret"/>
          <span className="jp-name"><b>{g.company}</b>{g.role && <small>{g.role}</small>}</span>
          <em className={`jk ${cls}`}>{t(name)}</em></button>
        <span className="jp-meta"><span>{t(['Last mail', '最近来信'])} {jobStamp(g.last_at, true)}</span>
          {g.next_event_at && <span className="jp-next">{t(['Next', '下一个'])} {jobStamp(g.next_event_at, true)}</span>}
          <span>{t([`${g.mails.length} mail${g.mails.length > 1 ? 's' : ''}`, `${g.mails.length} 封`])}</span>
          {(g.time_total_s ?? 0) > 0 && <span className="jp-time" data-time>{t(['Spent', '花了'])} {t(spentText(g.time_total_s!))}</span>}</span>
        {on && (g.time_spent?.length ?? 0) > 0 && <p className="jp-days" data-days>{[...g.time_spent!].reverse().map(d => `${dayLabel(d.day)} ${t(spentText(d.seconds))}`).join(' · ')}</p>}
        {on && <ul className="jp-mails">{g.mails.map(mailItem)}</ul>}
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

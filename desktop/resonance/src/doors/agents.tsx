import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { ArrowUp, CheckCircle, CircleNotch, HourglassMedium } from '@phosphor-icons/react';
import claudeLogo from '../assets/brands/claude.svg';
import openaiLogo from '../assets/brands/openai.svg';
import { AGENT_NAME, answerRequest, readConversation, requestWords, sendReply, type Agent, type Said } from '../agents';
import type { L } from '../companionSettings';
import { accountOf, clock, dur } from '../dayView';
import { Markdown } from '../Markdown';
import { openTip } from '../Notices';
import { useAsk, DoorHead } from './parts';
import { answerStartrail, readStartrail } from '../startrail';
import type { BarMark, DoorChip, DoorCtx, DoorPage, DoorRow, Tone } from '../doors';
import '../day-doors-agents.css';

// Agents: the sessions of the day under the bar, from the list the notch already has (Claude Code, Codex and Startrail's: Notch.tsx hands it down, nothing here polls). Today: the live ones and
// the ones that finished on it, oldest start first, each a span on the bar over the time it ran; a session that waits on him says so, and he can answer a plain permission or park it from its
// row (the notch's own acts, so his marks stay one truth: ADR 0069). Another day: no live data, only that day's Claude Code sessions from the day itself, as finished ones. A row opens as its
// conversation, with the way to the place it runs.
// Lanes: the bar's three lanes are filled greedily by start (`lanesOf`); the board's lane per project (named, in the project's colour) needs the session's working directory mapped to a project (ADR 0315, Gaps.png),
// which the notch's list does not have: `BarMark` has a lane number and a tone only. Until then a lane is just where there was room.
const NOTICE_MS = 4000;
type T = (l: L) => string;
// What a session is to him now: waiting on him, put aside by him (the moon), running (or just allowed, until the board says so), finished, stopped on an error.
type Kind = 'asking' | 'parked' | 'running' | 'done' | 'stopped';
type Run = { a: Agent; kind: Kind; from: number | null; to: number };
type Seg = { from: number; to: number; tone: Tone };
type Acts = { jump: (a: Agent) => void; park: (id: string) => void; unpark: (id: string) => void; allow: (a: Agent) => void };

// The brand's mark in the row's circle: the logo is a mask, so it takes the circle's ink.
const logo = (agent: Agent['agent']) => <span className="dt-logo" style={{ maskImage: `url(${JSON.stringify(agent === 'claude' ? claudeLogo : openaiLogo)})` }} aria-hidden="true"/>;
const dateOf = (ms: number, tz: string) => { try { return new Intl.DateTimeFormat('en-US', { timeZone: tz || undefined, month: 'numeric', day: 'numeric' }).format(ms); } catch { return ''; } };
const live = (k: Kind) => k === 'asking' || k === 'parked' || k === 'running';
// A permission that one tap can answer; a question or a plan has to be read, so it goes to where it runs.
const plain = (a: Agent) => !!a.request && a.request.tool !== 'AskUserQuestion' && a.request.tool !== 'ExitPlanMode';
const waitingIn = (a: Agent, t: T) => a.where === 'Background' ? t(['Waiting for you in the background', '在后台等你']) : a.where ? t([`Waiting for you in ${a.where}`, `在 ${a.where} 里等你`]) : t(['Waiting for you', '在等你']);
// The row's second line: who, then what it asks, is doing, or how it ended.
const subOf = (r: Run, t: T, allowed: boolean) => {
  const { a } = r;
  const say = allowed ? t(['Allowed', '已同意']) : a.state === 'wait' ? a.request ? t(requestWords(a.request)) : waitingIn(a, t)
    : a.state === 'err' ? [t(['Stopped', '出错停了']), a.error].filter(Boolean).join(' · ') : a.state === 'done' ? [t(['Done', '做完了']), a.last].filter(Boolean).join(' · ')
    : a.state === 'pack' ? t(['Compacting', '在压缩']) : a.last || t(['Working', '在干活']);
  return `${AGENT_NAME[a.agent]} · ${say}`;
};
// What a session did on the bar: running is lit up to now; one waiting on him was running until its last change and is amber since (put aside: dim); a finished one is dim. One that does not say
// when it began (a Codex session only says when it last changed) starts at that change: a finished one is a point there, a running one is lit from there to now.
const segsOf = (r: Run): Seg[] => {
  const at = r.a.at!, tone: Tone = r.kind === 'asking' ? 'amber' : 'dim';
  if (r.kind === 'asking' || r.kind === 'parked') { const tail = { from: at, to: Math.max(r.to, at), tone }; return r.from === null ? [tail] : [{ from: r.from, to: at, tone: 'on' }, tail]; }
  return [{ from: r.from ?? at, to: r.kind === 'running' ? r.to : at, tone: r.kind === 'running' ? 'on' : 'dim' }];
};
// Spans go into the three lanes greedily by start (the first lane that is free by then); with more than three at once the rest share the last.
function lanesOf(spans: { key: string; from: number; to: number }[]) {
  const ends = [-Infinity, -Infinity, -Infinity], lane = new Map<string, 0 | 1 | 2>();
  for (const s of [...spans].sort((a, b) => a.from - b.from)) { const free = ends.findIndex(e => e <= s.from), at = (free < 0 ? 2 : free) as 0 | 1 | 2; ends[at] = Math.max(ends[at], s.to); lane.set(s.key, at); }
  return lane;
}

export function useAgentsDoor(ctx: DoorCtx): DoorPage | null {
  const { port, t, lang, day, items, sessions } = ctx, askAbout = useAsk(ctx);
  // The notch's acts, as they are at this render (they are made anew every render by the companion).
  const act = useRef(sessions?.act);
  act.current = sessions?.act;
  // The requests he has just allowed from here, until the board shows the session going on; what could not be sent is said in the page's line for 4 s, or until his next act.
  const [sent, setSent] = useState<ReadonlySet<string>>(() => new Set()), [notice, setNotice] = useState<string | null>(null), timer = useRef(0);
  useEffect(() => () => clearTimeout(timer.current), []);
  const say = (text: string | null) => { clearTimeout(timer.current); setNotice(text); if (text) timer.current = window.setTimeout(() => setNotice(null), NOTICE_MS); };
  const allow = async (a: Agent) => {
    const req = a.request;
    if (!req) return;
    const keep = (on: boolean) => setSent(s => { const next = new Set(s); if (on) next.add(req.id); else next.delete(req.id); return next; });
    say(null);
    keep(true);
    let ok = false;
    try { ok = a.host ? await answerStartrail(a.id, req, { decision: 'allow' }) : port ? await answerRequest(port, req.id, { decision: 'allow' }) : false; } catch { /* the host was not there */ }
    if (ok) return;
    keep(false);
    say(t(["Couldn't send that; it may already be answered.", '没送出去，可能已经有人回答了。']));
  };
  // The companion keeps his marks in sets it changes in place, so what is in them is read at each render and the page is made anew when it differs.
  const parkedKey = sessions ? [...sessions.parked.keys()].sort().join('|') : '', archivedKey = sessions ? [...sessions.archived].sort().join('|') : '';
  return useMemo((): DoorPage => {
    const acts: Acts = { jump: a => act.current?.jump(a), park: id => { say(null); act.current?.park([id]); }, unpark: id => { say(null); act.current?.unpark([id]); }, allow: a => void allow(a) };
    const link = window.jarvis?.openAgents ? { label: t(['Agents window', 'Agents 窗口']), open: () => window.jarvis?.openAgents?.() } : null;
    const btn = (label: string, onClick: () => void, cls = '') => <button type="button" className={`dt-ask${cls}`} tabIndex={-1} onClick={onClick}>{label}</button>;
    const inDay = (ms: number) => ms >= day.start && ms < day.end;

    // The sessions of the list that are shown (what he archived is gone from the notch, so from here), each with what it is to him now.
    const shown = (sessions?.agents ?? []).filter(a => !sessions?.archived.has(a.id) && a.at !== undefined);
    const runOf = (a: Agent): Run => {
      const allowed = !!a.request && sent.has(a.request.id), at = a.at!;
      const kind: Kind = a.state === 'wait' && !allowed ? sessions?.parked.has(a.id) ? 'parked' : 'asking' : a.state === 'done' ? 'done' : a.state === 'err' ? 'stopped' : 'running';
      return { a, kind, from: a.started !== undefined && a.started <= at ? a.started : null, to: live(kind) ? Math.max(day.now, at) : at };
    };
    const all = shown.map(runOf), liveAll = all.filter(r => live(r.kind));
    const count = liveAll.length ? String(liveAll.length) : null, countAmber = liveAll.some(r => r.kind === 'asking');
    const base = { count, countAmber, link };

    // Another day has no live data: that day's Claude Code sessions from the day itself, finished ones.
    if (!day.today) {
      const codes = items.filter(it => it.kind === 'code').sort((a, b) => a.start_ms - b.start_ms || (a.id < b.id ? -1 : 1));
      const lane = lanesOf(codes.filter(it => (it.end_ms ?? it.start_ms) > it.start_ms).map(it => ({ key: it.id, from: it.start_ms, to: it.end_ms! })));
      const marks: BarMark[] = codes.map(it => lane.has(it.id) ? { kind: 'span', from: it.start_ms, to: it.end_ms!, lane: lane.get(it.id)!, tone: 'dim', key: it.id } : { kind: 'point', at: it.start_ms, tone: 'dim', key: it.id });
      const rows: DoorRow[] = codes.map(it => ({
        key: it.id, at: it.start_ms, icon: logo('claude'), title: it.title?.trim() || it.project?.trim() || t(['Claude Code session', 'Claude Code 会话']),
        sub: ['Claude', it.project?.trim(), accountOf(it)].filter(Boolean).join(' · '), side: lane.has(it.id) ? dur(Math.max(1, (it.end_ms! - it.start_ms) / 60_000), t) : undefined,
      }));
      return { ...base, line: codes.length ? t([`${codes.length} ${codes.length === 1 ? 'session' : 'sessions'} that day.`, `这天开了 ${codes.length} 个会话。`]) : t(['No Agents that day.', '这天没开 Agents。']),
        chips: codes.length ? [{ id: 'done', icon: <CheckCircle size={12} aria-hidden="true"/>, label: t(['Done', '做完']), value: String(codes.length) }] : [], marks, rows, nowAt: null, empty: t(['No sessions that day', '这天没有会话']) };
    }

    // Today: what is live, and what last changed inside the day; oldest start first.
    const runs = all.filter(r => live(r.kind) || inDay(r.a.at!)).sort((x, y) => (x.from ?? x.a.at!) - (y.from ?? y.a.at!) || (x.a.id < y.a.id ? -1 : 1));
    const spans = runs.flatMap(r => { const segs = segsOf(r).filter(s => s.to > s.from); return segs.length ? [{ key: r.a.id, from: Math.max(segs[0].from, day.start), to: Math.min(segs[segs.length - 1].to, day.end) }] : []; });
    const lane = lanesOf(spans);
    const marks: BarMark[] = runs.flatMap(r => segsOf(r).filter(s => s.to >= day.start && s.from <= day.end).map((s): BarMark => s.to > s.from
      ? { kind: 'span', from: Math.max(s.from, day.start), to: Math.min(s.to, day.end), lane: lane.get(r.a.id) ?? 2, tone: s.tone, key: r.a.id } : { kind: 'point', at: s.from, tone: s.tone, key: r.a.id }));

    const rowOf = (r: Run): DoorRow => {
      const { a } = r, at = r.from ?? a.at!, allowed = !!a.request && sent.has(a.request.id), can = !!openTip(a, t);
      const go = can && btn(t(['Open', '打开']), () => acts.jump(a));
      const quick = r.kind === 'parked' ? <>{btn(t(['Take back', '拿回来']), () => acts.unpark(a.id))}{go}</>
        : r.kind === 'asking' ? <>{btn(t(['Park', '先放着']), () => acts.park(a.id))}{plain(a) ? btn(t(['Allow', '同意']), () => acts.allow(a), ' is-yes') : go}</> : go || undefined;
      // How long it ran, to its last change (a live one: to now, with a dot while it works); one that does not say when it began does not say how long.
      const len = r.from === null ? '' : dur(Math.max(1, (r.to - r.from) / 60_000), t), works = a.state === 'work' || a.state === 'pack';
      const side: ReactNode = works ? <span className="dt-live"><i/>{len}</span> : len || undefined;
      return {
        key: a.id, at, time: at < day.start ? dateOf(at, day.tz) : undefined, icon: logo(a.agent), title: a.title, tag: r.kind === 'asking' ? { text: t(['Waiting', '等你']), amber: true } : r.kind === 'parked' ? { text: t(['Parked', '先放着']) } : null,
        sub: subOf(r, t, allowed), side, quick, tone: r.kind === 'parked' ? 'read' : live(r.kind) ? 'live' : undefined, opens: true,
      };
    };
    const asking = runs.filter(r => r.kind === 'asking'), running = runs.filter(r => r.kind === 'running'), parked = runs.filter(r => r.kind === 'parked'), done = runs.filter(r => r.kind === 'done'), stopped = runs.filter(r => r.kind === 'stopped');
    const chips: DoorChip[] = [];
    if (asking.length) chips.push({ id: 'wait', icon: <HourglassMedium size={12} aria-hidden="true"/>, label: t(['Waiting', '等你']), value: String(asking.length), amber: true, only: new Set(asking.map(r => r.a.id)) });
    chips.push({ id: 'run', icon: <CircleNotch size={12} aria-hidden="true"/>, label: t(['Running', '在跑']), value: String(running.length) }, { id: 'done', icon: <CheckCircle size={12} aria-hidden="true"/>, label: t(['Done today', '今天做完']), value: String(done.length) });

    // The line, written from the facts: who waits on him (the one by name), how many more run, how many he has put aside; else that all have stopped, or that none was opened.
    const en: string[] = [], zh: string[] = [], k = asking.length, n = running.length, p = parked.length;
    if (k) { en.push(k === 1 ? `${asking[0].a.title} is waiting for you` : `${k} are waiting for you`); zh.push(k === 1 ? `${asking[0].a.title} 在等你点头` : `${k} 个在等你点头`); }
    if (n) { en.push(k ? `${n} more running` : `${n} running`); zh.push(k ? `另外 ${n} 个在跑` : `${n} 个在跑`); }
    if (p) { en.push(`${p} parked`); zh.push(`${p} 个先放着`); }
    const line = en.length ? t([`${en.join('; ')}.`, `${zh.join('；')}。`]) : done.length ? t([`All stopped; ${done.length} finished today.`, `都停了，今天做完 ${done.length} 个。`]) : stopped.length ? t(['All stopped.', '都停了。']) : t(['No Agents today.', '今天没开 Agents。']);

    return {
      ...base, line: notice ?? line, chips, marks, rows: runs.map(rowOf), nowAt: day.now, empty: t(['No sessions today', '今天没有会话']),
      // The page opens on the session that waits on him (the oldest of them), else the one that runs (the newest), else the last that finished.
      rest: (asking[0] ?? running[running.length - 1] ?? runs[runs.length - 1])?.a.id ?? null,
      detail: key => {
        const r = runs.find(x => x.a.id === key);
        // "19:40–21:50" for one that ended, "19:40 起" for one that runs; a session that does not say when it began says only the hour of its last change.
        const range = !r ? '' : r.from === null ? clock(r.a.at!, day.tz) : live(r.kind) ? t([`since ${clock(r.from, day.tz)}`, `${clock(r.from, day.tz)} 起`]) : `${clock(r.from, day.tz)}–${clock(r.to, day.tz)}`;
        return r ? { title: r.a.title, body: <>
          <DoorHead icon={logo(r.a.agent)} logo title={r.a.title} sub={[r.a.project, AGENT_NAME[r.a.agent], range].filter(Boolean).join(' · ')} ask={askAbout(r.a.title)} t={t}/>
          <Talk key={r.a.id} a={r.a} kind={r.kind} port={port} t={t} acts={acts}/></> } : null;
      },
    };
  }, [sessions?.agents, askAbout, parkedKey, archivedKey, items, sent, notice, port, day.start, day.end, day.today, day.now, day.tz, lang]);
}

// A session's page: where it runs and the way there, the conversation (his words on the right, its answers as it wrote them: the notch's page reads the same), what it asks if it waits on him,
// and a box for a line to type into it where that can be done (an idle background session).
function Talk({ a, kind, port, t, acts }: { a: Agent; kind: Kind; port: string | null; t: T; acts: Acts }) {
  const [said, setSaid] = useState<Said[] | null>(null), [mine, setMine] = useState<string[]>([]), [draft, setDraft] = useState(''), [sending, setSending] = useState(false), [why, setWhy] = useState(''), [again, setAgain] = useState(0);
  const reads = !!a.host || a.agent === 'claude' && !!port;
  useEffect(() => {
    if (!reads) return;
    let stop = false;
    void (a.host ? readStartrail(a.id) : readConversation(port!, a.id)).then(m => { if (!stop && m) setSaid(m); });
    return () => { stop = true; };
  }, [a.id, a.at, a.state, again]);
  // Before the record comes (and for a Codex session, which has none to read) what its row knows: his last words and, once it ended, its last.
  const base: Said[] = said ?? [...a.you ? [{ who: 'you' as const, text: a.you }] : [], ...a.last && (a.state === 'done' || a.state === 'err') ? [{ who: 'it' as const, text: a.last }] : []];
  // A line he has just sent shows at once, until the record has it.
  const recorded = base.filter(m => m.who === 'you').slice(-3).map(m => m.text.replace(/\s+/g, ' ').trim()), pending = mine.filter(x => !recorded.includes(x));
  const working = a.state === 'work' || a.state === 'pack', busy = working || sending || pending.length > 0;
  const send = async (e: FormEvent) => {
    e.preventDefault();
    const text = draft.replace(/\s+/g, ' ').trim().slice(0, 4000);
    if (!text || busy || !a.replyable || !port) return;
    setWhy(''); setSending(true); setMine(m => [...m, text]); setDraft('');
    const fail = await sendReply(port, a.id, text);
    setSending(false);
    if (fail) { setWhy(t(['That reply did not go through', '这条回复没发出去'])); setMine(m => m.filter(x => x !== text)); setDraft(text); } else setAgain(n => n + 1);
  };
  const where = a.where === 'Background' ? t(['Background', '后台']) : a.where, can = !!openTip(a, t);
  return <div className="dt-ag">
    <div className="dt-ag-top"><p>{a.project && <b>{a.project}</b>}{[AGENT_NAME[a.agent], where, a.branch].filter(Boolean).join(' · ')}</p>
      {can && <button type="button" className="dt-ask" onClick={() => acts.jump(a)}>{t(['Open where it runs', '在它那里打开'])}</button>}</div>
    <div className="m-list">
      {!base.length && !pending.length && <p className="r-none">{t(['No messages yet', '还没有对话'])}</p>}
      {base.map((m, i) => m.who === 'you' ? <div key={i} className="m-you">{m.text}</div> : <div key={i} className="m-it md"><Markdown text={m.text}/></div>)}
      {pending.map(x => <div key={`p:${x}`} className="m-you is-pending">{x}</div>)}
      {working && <div className="m-now"><span>{(a.last || t(['Working', '在干活'])).replace(/[.…]+$/, '')}…</span></div>}
    </div>
    {kind === 'asking' && a.request && <div className="m-req"><p className="dt-ag-ask">{t(requestWords(a.request))}</p>
      <div className="dt-ag-acts"><button type="button" className="dt-ask" onClick={() => acts.park(a.id)}>{t(['Park', '先放着'])}</button>{plain(a) && <button type="button" className="dt-ask is-yes" onClick={() => acts.allow(a)}>{t(['Allow', '同意'])}</button>}</div></div>}
    {why && <p className="r-why">{why}</p>}
    {a.replyable && port && <form className="pg-input c-reply" onSubmit={send}>
      <input value={draft} onChange={e => setDraft(e.target.value)} disabled={busy} aria-label={t(['Reply', '回复'])} placeholder={sending ? t(['Sending…', '发送中…']) : t(['Reply to it…', '回它一句…'])}/>
      <button className="send" aria-label={t(['Send', '发送'])} disabled={!draft.trim() || busy}><ArrowUp size={13} weight="bold"/></button></form>}
  </div>;
}

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode, type WheelEvent } from 'react';
import { ArrowSquareOut, ArrowUp, ArrowsClockwise, CaretDown, CaretLeft, CaretRight, CaretUp, GitBranch, MagnifyingGlass, ShieldCheck, X } from '@phosphor-icons/react';
import { TAKES, pick, type ExprId } from './starCore';
import { useUsage, type UsageWindow } from './QuotaModule';
import { useCodexSessions } from './CodexModule';
import { AGENT_NAME, DEMO_AGENTS, fromClaude, fromCodex, useClaudeSessions, type Agent, type AgentState, type ShownAgent } from './agents';
import { freshnessLine, nowLine, useWorkState, type Basis } from './WorkStateModule';
import { duration, useProjects } from './ProjectsModule';
import { fmtReset } from './quota-time';
import { plain, visible, type Row } from './model';
import { Markdown } from './Markdown';
import { AgentMark, type MarkLook, type MarkState } from './AgentMarks';
import { cleanError, usePluginIcon, type Plugin, type PluginRequest, type usePlugins } from './PluginPanel';
import './dashboard-around.css';

// The Dashboard around her: one column under the companion, her words first. A row grows into its
// page in place; the panel never changes height. Every colour comes from her light (--glow).
type Page = 'conversation' | 'now' | 'agents' | 'usage' | 'plugins' | 'projects';
const TITLES: Record<Page, string> = { conversation: 'Conversation', now: 'Right now', agents: 'Agents', usage: 'Usage', plugins: 'Plugins', projects: 'Projects' };
const SPRING = 'linear(0,.054,.178,.329,.481,.617,.731,.82,.888,.936,.969,.99,1.003,1.01,1.014,1.015,1.014,1.012,1.01,1.008,1)';
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const dur = (ms: number) => reduced.matches ? 0 : ms;
const usd = (n?: number) => n === undefined ? '—' : `$${n.toFixed(2)}`;
const pad = (n: number) => String(n).padStart(2, '0');
const hm = (ms: number) => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
// Claude and Codex both hand out limit resets; one wording for both.
const resetsLeft = (n: number, until?: string | null) =>
  `${n} reset${n === 1 ? '' : 's'} left${n && until ? ` · until ${new Date(until).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}` : ''}`;
// "Yes" wakes ARM_MS after the question, so a double click on "Use reset" cannot land on it;
// an unanswered question folds away after ASK_MS. The answers are Codex's own words.
const ARM_MS = 600, ASK_MS = 10_000;
const RESET_ANSWERS: Record<string, string> = {
  reset: 'Codex limits reset', nothing_to_reset: 'Your usage does not need a reset right now',
  no_credit: 'No resets left', already_redeemed: 'That reset already went through',
};

// Hidden rows stay hidden until the session is given a new prompt.
const HIDDEN = 'companion-hidden-agents-v1';

// Plugins: the daemon's catalog through the window's plugin bridge when live, a demo catalog otherwise.
// Both are drawn from one shape; a live plugin's request adds what Jarvis asked and how far sign-in got.
type PluginState = 'on' | 'off' | 'token' | 'signin' | 'connecting';
type DemoPlugin = { name: string; mark: string; kind: 'oauth' | 'token' | 'none'; about: string; state: PluginState; was?: PluginState; ask?: string; resumed?: string; toolCount: number; tools: string[];
  error?: string; unsupported?: string; approval?: string; can?: string[]; saved?: boolean };
type PluginController = ReturnType<typeof usePlugins>;
const fromPlugin = (p: Plugin, r: PluginRequest | null): DemoPlugin => {
  const mine = r?.plugin_id === p.id ? r : null;
  const kind = p.auth === 'oauth' || p.auth === 'mixed' ? 'oauth' : p.credential_fields.length ? 'token' : 'none';
  const state: PluginState = p.status === 'ready' ? 'on'
    : mine?.state === 'connecting' || mine?.state === 'authorizing' || p.status === 'connecting' || p.status === 'authorizing' ? 'connecting'
    : kind === 'oauth' ? 'signin' : kind === 'token' ? 'token' : 'off';
  return { name: p.name, mark: p.name.slice(0, 1).toUpperCase(), kind, about: p.description, state, ask: mine?.purpose || undefined,
    resumed: mine?.state === 'ready' && mine.resume_status === 'continued' ? 'Picking up the task you asked for.' : undefined,
    toolCount: p.tools.length, tools: p.tools.map(t => t.name.replace(/^mcp__[^_]+__/, '')),
    error: mine?.state === 'error' ? mine.error ?? 'The connection didn’t go through.' : p.status === 'error' ? p.error ?? 'Connection problem.' : undefined,
    unsupported: p.supported ? undefined : p.unavailable_reason ?? 'Not supported on this Mac.', approval: p.approval_mode, can: p.capabilities, saved: p.credentials_saved };
};
const PLUGIN_ORDER = ['notion', 'microsoft', 'github', 'linear'];
const DEMO_PLUGINS: Record<string, DemoPlugin> = {
  notion: { name: 'Notion', mark: 'N', kind: 'oauth', about: 'Pages and databases in your workspace.', state: 'signin', ask: 'Find last week’s meeting notes and sum them up.', toolCount: 9, tools: ['search', 'fetch_page', 'create_page', 'update_page', 'query_database'] },
  microsoft: { name: 'Microsoft 365', mark: 'M', kind: 'oauth', about: 'Your To Do lists and Outlook calendar.', state: 'on', toolCount: 14, tools: ['list_todo_tasks', 'create_todo_task', 'list_calendar_events', 'create_calendar_event', '…10 more'] },
  github: { name: 'GitHub', mark: 'G', kind: 'token', about: 'Repositories, issues and pull requests.', state: 'token', toolCount: 21, tools: ['search_issues', 'get_pull_request', 'create_issue', 'list_commits', '…17 more'] },
  linear: { name: 'Linear', mark: 'L', kind: 'none', about: 'Issues and projects.', state: 'off', toolCount: 8, tools: ['list_issues', 'create_issue', 'update_issue', '…5 more'] },
};
function Mark({ id, mark }: { id: string; mark: string }) {
  const src = usePluginIcon(id);
  return src ? <img src={src} alt=""/> : <>{mark}</>;
}
const pluginStatus = (p: DemoPlugin): [string, string] => p.unsupported ? ['', 'Not supported'] : p.state === 'on' ? ['is-on', 'Connected']
  : p.error && p.state !== 'connecting' ? ['is-need', 'Connection problem']
  : p.state === 'connecting' ? ['is-need', p.kind === 'oauth' ? 'Waiting for sign-in…' : 'Connecting…']
  : p.state === 'off' ? ['', 'Off'] : p.state === 'token' ? ['is-need', 'Needs an access token']
  : ['is-need', p.ask ? 'Jarvis asked · needs sign-in' : 'Needs sign-in'];

type Turn = { you: string; at: string; jarvis?: string; jarvisAt?: string; work?: [string, string]; day?: string };
const DEMO_TURNS: Turn[] = [
  { you: 'Remind me to test the mic at four.', at: '11:05', jarvis: 'Done. I’ll remind you at 4 PM.', jarvisAt: '11:05' },
  { you: 'What’s left on my plate today?', at: '14:32', jarvisAt: 'just now',
    jarvis: 'Two things left today. Your 4 PM reminder is set. You can confirm the dashboard direction first, then check the voice test at 4 PM.',
    work: ['Worked it out with gpt-5.6-luna · 2.1 s', 'Checked today’s to-dos:\n1. Confirm the Resonance dashboard direction.\n2. 16:00 voice test reminder, scheduled.'] },
];
const ANSWER = 'Got it. I’ll take care of it and tell you when it’s done.';
const BASIS: Record<Basis, string> = { observed: 'Observed', stated: 'You said', inferred: 'A guess' };
// Live conversation: the memory.db rows and the answer still streaming, from the companion's daemon link.
// `older` fetches a longer page and says whether it brought earlier rows; `floor` means the history's start is on hand.
type Talk = { rows: Row[]; tail: string; busy: boolean; offline: boolean; floor: boolean; submit: (text: string) => void; older: () => Promise<boolean> };
const when = (ts: string) => { const d = new Date(ts); return Number.isNaN(d.getTime()) ? '' : d.toDateString() === new Date().toDateString() ? hm(d.getTime()) : `${d.getMonth() + 1}/${d.getDate()} ${hm(d.getTime())}`; };
const dayLabel = (day: string) => { const d = new Date(day); return d.toDateString() === new Date(Date.now() - 86_400_000).toDateString() ? 'yesterday' : `${d.toLocaleDateString('en-US', { weekday: 'short' })} ${d.getMonth() + 1}/${d.getDate()}`; };
// The conversation of record as turns, each dated by the row that opens it: your rows open one, and Jarvis's rows after it answer it.
const toTurns = (rows: Row[]): Turn[] => {
  const turns: Turn[] = [];
  for (const row of rows) {
    const t = turns.at(-1), text = visible(row.text), at = when(row.ts), day = new Date(row.ts).toDateString();
    if (row.source === 'allen') turns.push({ you: row.text, at, day });
    else if (!t) turns.push({ you: '', at: '', jarvis: text, jarvisAt: at, day });
    else Object.assign(t, { jarvis: t.jarvis ? `${t.jarvis}\n\n${text}` : text, jarvisAt: at });
  }
  return turns;
};
const PULL = 240; // px of fresh upward scroll at the top that adds the day before

export function AroundDashboard({ open, port = null, onClose, onMood, onHop, talk, plugins: live, pluginFocus = null, marks = 'spark', onAgents, agentsFocus = 0, onAnswer, seen }: {
  open: boolean; port?: string | null; onClose: () => void; onMood: (expr: ExprId | null) => void; onHop: (height: number) => void;
  talk?: Talk; plugins?: PluginController; pluginFocus?: { plugin: string; key: string } | null;
  marks?: MarkLook; onAgents?: (agents: ShownAgent[]) => void; agentsFocus?: number; onAnswer?: (id: string) => void;
  seen?: { ids: string[]; at: number };
}) {
  const quota = useUsage(port), codex = useCodexSessions(port), work = useWorkState(port), projects = useProjects(port, open), claudeRows = useClaudeSessions(port);
  const [page, setPage] = useState<Page | null>(null);
  const [plugin, setPlugin] = useState<string | null>(null);
  const [demoPlugins, setPlugins] = useState(DEMO_PLUGINS);
  const snapshot = live?.snapshot;
  const plugins: Record<string, DemoPlugin> = !live ? demoPlugins : Object.fromEntries((snapshot?.plugins ?? []).map(p => [p.id, fromPlugin(p, snapshot!.request)]));
  const pluginIds = !live ? PLUGIN_ORDER : [...snapshot?.plugins ?? []].sort((a, b) => Number(b.status === 'ready') - Number(a.status === 'ready')
    || Number(b.supported) - Number(a.supported) || a.name.localeCompare(b.name)).map(p => p.id);
  const [query, setQuery] = useState('');
  const [token, setToken] = useState('');
  const [said, setSaid] = useState({ text: 'Two things left today. Your 4 PM reminder is set.', caption: 'Jarvis · just now', busy: false });
  const [turns, setTurns] = useState(DEMO_TURNS);
  // The Conversation page opens on the newest day; at the top, a fresh scroll up past PULL adds the day before.
  const [days, setDays] = useState(1), [pull, setPull] = useState(0);
  const wheel = useRef({ acc: 0, last: 0, armed: false, busy: false, timer: undefined as ReturnType<typeof setTimeout> | undefined });
  const anchor = useRef<number | null>(null);
  const [moved, setMoved] = useState<Record<string, Partial<Agent> & { at: number }>>({});
  // Agent cards show only name, state and tags; one card at a time opens to show the rest.
  const [unfolded, setUnfolded] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Record<string, string>>(() => { if (!port) return {}; try { return JSON.parse(localStorage.getItem(HIDDEN) ?? '{}') ?? {}; } catch { return {}; } });
  useEffect(() => { if (port) try { localStorage.setItem(HIDDEN, JSON.stringify(hidden)); } catch { /* not remembered across restarts */ } }, [hidden]);
  const [toast, setToast] = useState<{ text: string; undo?: () => void } | null>(null);
  // ADR 0048: a Codex reset takes two clicks on two buttons in different places, the second one
  // live only after ARM_MS. The id is minted with the question, so "Try again" cannot spend twice.
  const [reset, setReset] = useState<{ id: string; state: 'ask' | 'using' | 'error'; armed: boolean; error?: string } | null>(null);
  const [mood, setMood] = useState<ExprId>('02');
  const view = useRef<HTMLDivElement>(null), home = useRef<HTMLDivElement>(null), pageEl = useRef<HTMLElement>(null);
  const closing = useRef(false), flip = useRef<{ id: string; top: number } | null>(null), shownPlugin = useRef<string | null>(null);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]), moodTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const pluginTimer = useRef<ReturnType<typeof setTimeout>>(undefined), toastTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const later = (ms: number, run: () => void) => { timers.current.push(setTimeout(run, ms)); };
  useEffect(() => () => { timers.current.forEach(clearTimeout); [moodTimer, pluginTimer, toastTimer].forEach(t => clearTimeout(t.current)); }, []);

  // Her face follows the page; '02' is her resting face, so it hands control back to the companion.
  const react = (expr: ExprId, ms: number, after: ExprId = '02') => {
    clearTimeout(moodTimer.current); setMood(expr);
    if (ms) moodTimer.current = setTimeout(() => setMood(after), ms);
  };
  useEffect(() => onMood(open && mood !== '02' ? mood : null), [open, mood, onMood]);
  const notify = (text: string, undo?: () => void) => {
    setToast({ text, undo }); clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), undo ? 5000 : 1800);
  };

  // Closing the panel puts everything back on the home page, without animation.
  useEffect(() => {
    if (open) return;
    closing.current = false; setPage(null); setPlugin(null); setUnfolded(null); setReset(null); react('02', 0);
    if (home.current) stopMotion(home.current);
    if (view.current?.contains(document.activeElement)) { (document.activeElement as HTMLElement).blur(); void window.jarvis?.focus(false); }
  }, [open]);

  const row = (name: Page) => home.current!.querySelector<HTMLElement>(`[data-row="${name}"]`)!;
  const insetOf = (el: HTMLElement) => {
    const v = view.current!.getBoundingClientRect(), r = el.getBoundingClientRect();
    return `inset(${r.top - v.top}px ${v.right - r.right}px ${v.bottom - r.bottom}px ${r.left - v.left}px round 12px)`;
  };
  // Only animations started here; her CSS loops (orbs, pills, rings) keep running.
  const stopMotion = (el: HTMLElement) => el.getAnimations({ subtree: true }).forEach(a => { if (!(a instanceof CSSAnimation || a instanceof CSSTransition)) a.cancel(); });
  const openPage = (name: Page) => {
    if (page || closing.current) return;
    setPage(name); setPlugin(null);
    if (name === 'conversation') { setDays(1); react(pick(TAKES.reply), 2600); }
    else if (name === 'now') react('37', 2400);
    else if (name === 'projects') { react('40', 1500); void projects.refresh(); }
    else { react('02', 0); if (name === 'agents') onHop(.2); }
  };
  // The row grows into the page: its outline opens to the whole panel and its title slides up to the top.
  useLayoutEffect(() => {
    const el = pageEl.current;
    if (!page || !el) return;
    const from = row(page), dy = from.getBoundingClientRect().top - view.current!.getBoundingClientRect().top;
    if (page === 'conversation') { const b = el.querySelector('.pg-body')!; b.scrollTop = b.scrollHeight; } // it opens on the newest turn
    el.animate([{ clipPath: insetOf(from) }, { clipPath: 'inset(0 0 0 0 round 14px)' }], { duration: dur(560), easing: SPRING });
    el.querySelector('.pg-head')?.animate([{ transform: `translateY(${dy}px)`, opacity: .3 }, { transform: 'none', opacity: 1 }], { duration: dur(560), easing: SPRING });
    // Only what is on screen fades in, in order; a long page would otherwise hold its newest words back.
    const box = el.getBoundingClientRect();
    [...el.querySelectorAll('.pg-sec, .pg-foot, .pg-input')].filter(s => { const r = s.getBoundingClientRect(); return r.bottom > box.top && r.top < box.bottom; }).forEach((s, i) => s.animate([{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }],
      { duration: dur(260), delay: dur(170 + i * 45), easing: 'cubic-bezier(.2,.7,.2,1)', fill: 'backwards' }));
    stopMotion(home.current!);
    home.current!.animate([{ opacity: 1 }, { opacity: 0 }], { duration: dur(150), fill: 'forwards' });
    el.querySelector<HTMLElement>('.pg-back')?.focus({ preventScroll: true });
  }, [page]);
  const closePage = () => {
    const el = pageEl.current;
    if (!page || !el || closing.current) return;
    const name = page, from = row(name);
    closing.current = true;
    // One thing at a time: the page's words leave, the empty page folds back into its row as a faint card,
    // and only then do the home rows return in order, the row it came from last, as the card lands on it.
    el.classList.add('is-closing');
    el.querySelectorAll('.pg-head, .pg-body').forEach(part => part.animate([{ opacity: 1 }, { opacity: 0, transform: 'translateY(-4px)' }], { duration: dur(120), easing: 'ease-in', fill: 'forwards' }));
    const shrink = el.animate([{ clipPath: 'inset(0 0 0 0 round 14px)' }, { clipPath: insetOf(from) }], { duration: dur(320), delay: dur(40), easing: 'cubic-bezier(.3,0,.2,1)', fill: 'forwards' });
    el.animate([{ opacity: 1 }, { opacity: 0 }], { duration: dur(90), delay: dur(290), fill: 'forwards' });
    const homeEl = home.current!;
    stopMotion(homeEl);
    [...homeEl.children].forEach((unit, i) => { if (unit !== from) unit.animate([{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: dur(220), delay: dur(130 + 30 * i), easing: 'cubic-bezier(.2,.7,.2,1)', fill: 'backwards' }); });
    from.animate([{ opacity: 0 }, { opacity: 1 }], { duration: dur(200), delay: dur(290), fill: 'backwards' });
    shrink.onfinish = () => {
      if (!closing.current) return;
      closing.current = false; setPage(null); setPlugin(null); react('02', 0);
      (from.matches('button') ? from : from.querySelector('button'))?.focus({ preventScroll: true });
    };
  };
  const goUp = () => { if (page === 'plugins' && plugin) setPlugin(null); else if (page) closePage(); else onClose(); };
  const keys = (event: KeyboardEvent) => {
    if (event.key !== 'Escape') return;
    event.stopPropagation();
    if ((event.target as Element).matches('input,select')) (event.target as HTMLElement).blur(); else goUp();
  };

  const ask = (text: string) => {
    if (talk) { talk.submit(text); return; }
    setTurns(value => [...value, { you: text, at: 'now' }]);
    const reply = pick(TAKES.reply);
    setSaid({ text: 'Thinking…', caption: 'Thinking', busy: true }); react('30', 1300, reply);
    later(1300, () => {
      setSaid({ text: ANSWER, caption: 'Jarvis · just now', busy: false }); react(reply, 2400);
      setTurns(value => value.map((t, i) => i === value.length - 1 ? { ...t, jarvis: ANSWER, jarvisAt: 'just now' } : t));
    });
  };
  const body = () => pageEl.current?.querySelector('.pg-body');
  const allTurns = talk ? toTurns(talk.rows) : turns, dayList = [...new Set(allTurns.map(t => t.day))];
  const shownTurns = talk ? allTurns.filter(t => dayList.slice(-days).includes(t.day)) : allTurns;
  const before = dayList.length > days ? dayList[dayList.length - days - 1]! : null, more = !!talk && (before !== null || !talk.floor);
  const loadEarlier = async () => {
    const b = body();
    anchor.current = b ? b.scrollHeight - b.scrollTop : null;
    // The oldest day on hand may be cut off by the page the daemon sent, so fetch before showing it.
    if (dayList.length <= days + 1 && !talk!.floor && !(await talk!.older()) && before === null) { anchor.current = null; return; }
    setDays(d => d + 1);
  };
  const onWheel = (e: WheelEvent<HTMLDivElement>) => {
    const w = wheel.current, fresh = e.timeStamp - w.last > 180;
    w.last = e.timeStamp;
    if (!more || w.busy || e.deltaY >= 0 || e.currentTarget.scrollTop > 0) { w.armed = false; if (w.acc) { w.acc = 0; setPull(0); } return; }
    if (fresh) w.armed = true; // momentum that carried the page to the top does not count
    if (!w.armed) return;
    w.acc -= e.deltaY;
    clearTimeout(w.timer);
    if (w.acc < PULL) { setPull(w.acc / PULL); w.timer = setTimeout(() => { Object.assign(w, { acc: 0, armed: false }); setPull(0); }, 400); return; }
    Object.assign(w, { acc: 0, armed: false, busy: true });
    setPull(0);
    void loadEarlier().finally(() => { w.busy = false; });
  };
  // The day added above keeps the words you were reading where they were.
  useLayoutEffect(() => { const b = body(); if (anchor.current !== null && b) b.scrollTop = b.scrollHeight - anchor.current; anchor.current = null; }, [days]);
  const lastAnswer = talk && [...talk.rows].reverse().find(row => row.source !== 'allen');
  const saying = !talk ? said : { busy: talk.busy,
    text: plain(talk.tail) || (lastAnswer ? plain(lastAnswer.text) : 'Say something and Jarvis answers here.'),
    caption: talk.offline ? 'Offline · reconnecting' : talk.busy ? 'Thinking' : talk.tail ? 'Jarvis · now' : lastAnswer ? `Jarvis · ${when(lastAnswer.ts)}` : 'Jarvis' };
  const newest = shownTurns.at(-1);
  useEffect(() => { if (page === 'conversation') body()?.scrollTo({ top: body()!.scrollHeight, behavior: reduced.matches ? 'auto' : 'smooth' }); }, [newest?.at, newest?.you, newest?.jarvis]);

  // Agents, grouped the way you act on them. A row you moved goes to the top of its new group.
  const agents = (port ? [...claudeRows.map(fromClaude), ...codex.rows.map(fromCodex)] : DEMO_AGENTS).filter(s => hidden[s.id] !== s.you).map(s => ({ ...s, ...moved[s.id] }));
  const group = (state: AgentState) => agents.filter(s => s.state === state).sort((a, b) => (moved[b.id]?.at ?? 0) - (moved[a.id]?.at ?? 0));
  const waiting = group('wait'), stopped = group('err'), working = [...group('work'), ...group('pack')], earlier = group('done');
  // A session that finishes while she watches stays "finished" until you have been on the Agents page;
  // what was already done when she started, or has been looked at since, is quiet.
  const [unseen, setUnseen] = useState<ReadonlySet<string>>(new Set());
  const seenStates = useRef<Record<string, AgentState>>({}), onAgentsPage = open && page === 'agents', wasOnPage = useRef(false);
  const statesKey = agents.map(s => `${s.id}:${s.state}`).join('|');
  useEffect(() => {
    const was = seenStates.current, now = seenStates.current = Object.fromEntries(agents.map(s => [s.id, s.state]));
    setUnseen(current => {
      const next = new Set([...current].filter(id => now[id] === 'done'));
      agents.forEach(s => { if (s.state === 'done' && ['work', 'pack', 'wait'].includes(was[s.id])) next.add(s.id); });
      return next.size === current.size && [...next].every(id => current.has(id)) ? current : next;
    });
  }, [statesKey]);
  useEffect(() => { if (wasOnPage.current && !onAgentsPage) setUnseen(new Set()); wasOnPage.current = onAgentsPage; }, [onAgentsPage]);
  // A finished card closed by hand counts as looked at.
  useEffect(() => { if (seen?.ids.length) setUnseen(current => new Set([...current].filter(id => !seen.ids.includes(id)))); }, [seen?.at]);
  const markOf = (s: Agent): MarkState => s.state === 'done' ? unseen.has(s.id) ? 'done' : 'seen' : s.state;
  const finished = earlier.filter(s => unseen.has(s.id));
  // Every row goes up to the companion: the wing draws the live ones, the notices watch them all change.
  const shown: ShownAgent[] = agents.map(s => ({ ...s, mark: markOf(s),
    line: s.state === 'done' ? unseen.has(s.id) ? 'Finished · not opened yet' : s.last : s.state === 'err' ? `Stopped · ${s.error}` : s.last || (s.state === 'wait' ? 'Needs you' : 'Working') }));
  const shownKey = JSON.stringify(shown);
  useEffect(() => onAgents?.(shown), [shownKey]);
  // The marks beside the notch were clicked: the companion opened the panel, and it lands on Agents.
  useEffect(() => {
    if (!agentsFocus || !open) return;
    if (!page) openPage('agents'); else if (page !== 'agents') setPage('agents');
  }, [agentsFocus]);
  const move = (s: Agent, change: Partial<Agent>) => {
    const el = pageEl.current?.querySelector<HTMLElement>(`[data-id="${s.id}"]`);
    if (el) flip.current = { id: s.id, top: el.getBoundingClientRect().top };
    setMoved(value => ({ ...value, [s.id]: { ...change, at: Date.now() } }));
  };
  useLayoutEffect(() => {
    const f = flip.current, el = f && pageEl.current?.querySelector<HTMLElement>(`[data-id="${f.id}"]`);
    flip.current = null;
    if (f && el) el.animate([{ transform: `translateY(${f.top - el.getBoundingClientRect().top}px)` }, { transform: 'none' }], { duration: dur(460), easing: SPRING });
  }, [moved]);
  const hide = (s: Agent) => {
    setHidden(value => ({ ...value, [s.id]: s.you }));
    notify('Hidden from this list.', () => setHidden(({ [s.id]: _, ...rest }) => rest));
  };
  // Claude sessions have no jump yet; their cards leave the button out rather than offer one that cannot work.
  const agentRow = (s: Agent, actions?: ReactNode) => <AgentRow key={s.id} s={s} look={marks} mark={markOf(s)} open={unfolded === s.id} onToggle={() => setUnfolded(v => v === s.id ? null : s.id)}
    onOpen={!port || s.agent === 'codex' ? () => void openAgent(s) : undefined} onHide={() => hide(s)} actions={actions}/>;
  const openAgent = async (s: Agent) => {
    if (port && s.agent === 'codex' && await window.jarvis?.openCodex?.(s.id).catch(() => false)) notify('Opening in Codex…');
    else notify(port ? `Can’t open ${s.where} from here yet.` : 'Demo session, nothing to open.');
  };

  const setPluginState = (id: string, change: Partial<DemoPlugin>) => setPlugins(value => ({ ...value, [id]: { ...value[id], ...change } }));
  // Opening a plugin pushes its page in from the right; going back slides the list in from the left.
  useLayoutEffect(() => {
    const prev = shownPlugin.current; shownPlugin.current = plugin;
    if (page !== 'plugins' || prev === plugin) return;
    const el = pageEl.current?.querySelector(plugin ? '.pl-det' : '.pl-cat');
    el?.animate([{ transform: `translateX(${plugin ? 36 : -36}px)`, opacity: 0 }, { transform: 'none', opacity: 1 }], { duration: dur(380), easing: SPRING });
    if (plugin) body()?.scrollTo({ top: 0 });
    else pageEl.current?.querySelector<HTMLElement>(`[data-plugin="${prev}"]`)?.focus({ preventScroll: true });
  }, [plugin, page]);
  const openPlugin = async (id: string) => { if (!live || await live.action('open', { plugin_id: id })) setPlugin(id); };
  // Live, every act is an operation on this plugin's request; the next snapshot redraws the page.
  const liveAct = async (id: string, act: string, value?: string) => {
    const request = snapshot?.request, mine = request?.plugin_id === id ? request : null;
    const run = (operation: string, data: Record<string, unknown> = {}) => live!.action(operation, { request_id: mine?.id, ...data });
    if (act === 'later') { if (mine?.state === 'offered' || mine?.state === 'error') void run('cancel'); setPlugin(null); }
    else if (act === 'reopen' || act === 'cancel') void run(act);
    else if (act === 'off') { if (await run('disable')) notify(`${plugins[id].name} is off.`); }
    else if (act === 'approval') { if (await run('approval', { mode: value })) notify('Saved.'); }
    else if (act === 'connect') {
      // ponytail: one token box; a plugin with several credential fields needs one box per field.
      const field = snapshot?.plugins.find(p => p.id === id)?.credential_fields[0];
      if (field && !token.trim() && !plugins[id].saved) { notify('Paste an access token first.'); return; }
      const credentials = field && token.trim() ? { [field]: token.trim() } : {};
      setToken('');
      if (await run('connect', { credentials })) react('36', 60_000);
    }
  };
  // Her waiting face ends when the sign-in does.
  const shownState = plugin ? plugins[plugin]?.state : undefined;
  useEffect(() => { if (live && mood === '36' && shownState !== 'connecting') react(shownState === 'on' ? '10' : '02', shownState === 'on' ? 1800 : 0); }, [shownState]);
  // Jarvis asked for a plugin: the companion opened the panel, and it lands on that plugin.
  useEffect(() => {
    if (!pluginFocus || !open) return;
    if (!page) openPage('plugins'); else if (page !== 'plugins') setPage('plugins');
    setPlugin(pluginFocus.plugin);
  }, [pluginFocus?.key]);
  const pluginAct = (act: string, value?: string) => {
    const id = plugin!, p = plugins[id];
    if (live) { void liveAct(id, act, value); return; }
    if (act === 'later') setPlugin(null);
    else if (act === 'reopen') notify('Opened the sign-in page again.');
    else if (act === 'approval') { setPluginState(id, { approval: value }); notify('Saved.'); }
    else if (act === 'cancel') { clearTimeout(pluginTimer.current); setPluginState(id, { state: p.was }); react('02', 0); }
    else if (act === 'off') { setPluginState(id, { state: p.kind === 'oauth' ? 'signin' : p.kind === 'token' ? 'token' : 'off', resumed: undefined }); notify(`${p.name} is off.`); }
    else if (act === 'connect') {
      if (p.state === 'token' && !token.trim()) { notify('Paste an access token first.'); return; }
      setToken(''); setPluginState(id, { was: p.state, state: 'connecting' }); react('36', 60_000);
      pluginTimer.current = setTimeout(() => {
        setPluginState(id, { state: 'on', ask: undefined, resumed: p.ask && `Picking up: “${p.ask}”` });
        if (p.ask) setSaid({ text: `Signed in to ${p.name}. Looking for last week’s notes now.`, caption: 'Jarvis · just now', busy: false });
        react('10', 1800);
      }, p.kind === 'oauth' ? 2600 : 1100);
    }
  };
  const pluginsOn = Object.values(plugins).filter(p => p.state === 'on').length;

  const { claude, codex: codexUsage, openai, deepseek, minimax } = quota.usage?.services ?? {};
  const synced = Math.max(0, ...[claude, codexUsage, openai, deepseek, minimax].map(s => s?.observed_at_ms ?? 0));
  const codexResets = codexUsage?.status === 'ok' ? codexUsage.data.reset_credits ?? 0 : 0;
  const balanceSaved = () => { notify('Balance saved'); void quota.refresh(); };
  const askReset = () => {
    const id = crypto.randomUUID();
    setReset({ id, state: 'ask', armed: false });
    later(ARM_MS, () => setReset(r => r?.id === id ? { ...r, armed: true } : r));
    later(ASK_MS, () => setReset(r => r?.id === id && r.state === 'ask' ? null : r));
  };
  const spendReset = async () => {
    const r = reset;
    if (!r?.armed || r.state === 'using') return;
    setReset({ ...r, state: 'using' });
    try {
      const answer = await window.jarvis!.usageReset('codex', r.id);
      setReset(v => v?.id === r.id ? null : v);
      notify(RESET_ANSWERS[answer.code] ?? `Codex answered ${answer.code}`);
      if (answer.code === 'reset') react('33', 1900);
      void quota.refresh();
    } catch (error) {
      setReset(v => v?.id === r.id ? { ...v, state: 'error', error: cleanError(error) } : v);
    }
  };
  const workView = work.view, state = workView?.state ?? null, fresh = freshnessLine(workView), now = nowLine(state?.now, state?.analyzed_at);
  const projectsView = projects.view, activeProjects = (projectsView?.projects ?? []).filter(p => p.seconds > 0 || p.commits.count > 0);
  const topProject = (projectsView?.projects ?? []).find(p => p.seconds > 0);
  const lead = waiting[0] ?? working[0];

  const back = (title: string, meta?: ReactNode) => <header className="pg-head">
    <button className="pg-back" aria-label="Back" onClick={goUp}><CaretLeft size={14} weight="bold"/></button>
    <h3 key={title}>{title}</h3>{meta && <span className="meta">{meta}</span>}
  </header>;
  const pages: Record<Page, () => ReactNode> = {
    conversation: () => <>
      {back('Conversation', talk ? undefined : 'today')}
      <div className="pg-body" onWheel={talk ? onWheel : undefined}>
      {talk && <div className={`pg-earlier${pull ? ' is-pulling' : ''}`} style={{ '--pull': pull } as CSSProperties}>
        {more ? <><CaretUp size={10} weight="bold"/>Scroll up for {before ? dayLabel(before) : 'earlier'}</> : 'Start of the conversation'}</div>}
      {shownTurns.map((t, i) => <div className="pg-sec tr" key={i} data-day={t.day}>
        {t.you && <div className="tr-you"><span className="who">You · {t.at}</span><p>{t.you}</p></div>}
        {t.jarvis && <div className="tr-jarvis"><span className="who"><span className="dot"/>Jarvis · {t.jarvisAt}</span><Markdown text={t.jarvis}/>
          {t.work && <Fold label={t.work[0]}><pre>{t.work[1]}</pre></Fold>}</div>}
      </div>)}
      <Ask className="pg-input" onAsk={ask}/></div>
    </>,
    now: () => <>
      {back('Right now', now && `${now.current ? 'as of' : 'at'} ${now.at}`)}
      <div className="pg-body">{workView === null ? <p className="pg-sec muted">Syncing…</p> : !state ? <p className="pg-sec muted">No status yet. Refresh and Jarvis reads the latest activity.</p> : <>
        {state.now && now && <div className="pg-sec now-card"><span className="who"><span className="dot"/>{BASIS[state.now.basis]} · {now.current ? 'as of' : 'at'} {now.at}</span><p>{now.claim}</p></div>}
        <div className="pg-sec"><h4>Today{state.observed_until ? `, until ${hm(Date.parse(state.observed_until))}` : ''}</h4>
          {state.activities.length ? <ol className="tl">{state.activities.map((c, i) => <li key={i} className={`is-${c.basis}`}>{c.text}{c.progress && <small>{c.progress}</small>}</li>)}</ol> : <p className="muted">Not enough data yet.</p>}
          <div className="legend"><span><i className="o"/>seen</span><span><i className="s"/>you said</span><span><i className="g"/>a guess</span></div>
        </div>
        {state.links.length > 0 && <div className="pg-sec"><h4>Linked</h4>{state.links.map((l, i) => <div className="link" key={i}><span className="chip">{l.kind === 'todo' ? 'To-do' : 'Discussion'}</span>{l.title || l.note}{l.title && <small>{l.note}</small>}</div>)}</div>}
        {state.uncertainties.length > 0 && <div className="pg-sec"><h4>Unknown</h4>{state.uncertainties.map((u, i) => <p className="muted" key={i}>{u}</p>)}</div>}
      </>}
      <footer className="pg-foot"><span className={fresh.stale ? 'is-warm' : ''}>{work.notice ?? fresh.text}</span>
        <button className="icon-btn" aria-label="Refresh" disabled={work.refreshing} onClick={work.refresh}><ArrowsClockwise size={13} className={work.refreshing ? 'is-spinning' : ''}/></button></footer></div>
    </>,
    agents: () => <>
      {back('Agents', `${waiting.length + working.length} live`)}
      <div className="pg-body">{!agents.length && <p className="pg-sec muted">Sessions show up once you start one.</p>}
      {waiting.length > 0 && <div className="pg-sec"><h4 className="is-warm"><span className="dot"/>Needs you</h4>{waiting.map(s => agentRow(s,
        !port ? <><button className="btn btn-glow" onClick={() => { move(s, { state: 'work', last: 'Approved · running it now…' }); react('33', 1900); }}>Approve</button>
          <button className="btn btn-ghost" onClick={() => move(s, { state: 'done', last: 'You denied it. It stopped there.', age: 'now' })}>Deny</button></>
          : s.request && <button className="btn btn-glow" onClick={() => onAnswer?.(s.id)}>Answer</button>))}</div>}
      {stopped.length > 0 && <div className="pg-sec"><h4 className="is-alert">Stopped · {stopped.length}</h4>{stopped.map(s => agentRow(s))}</div>}
      {working.length > 0 && <div className="pg-sec"><h4>Working · {working.length}</h4>{working.map(s => agentRow(s))}</div>}
      {earlier.length > 0 && <div className="pg-sec"><h4>{port ? 'Last 24 hours' : 'Earlier today'} · {earlier.length}</h4>{earlier.map(s => agentRow(s))}</div>}
      {agents.length > 0 && <p className="pg-sec muted">Click a session to see what it’s doing.</p>}</div>
    </>,
    usage: () => <>
      {back('Usage', <button className="us-sync" aria-label="Refresh" disabled={quota.refreshing} onClick={() => void quota.refresh()}>
        {!quota.refreshing && synced ? `synced ${hm(synced)}` : 'syncing…'}<ArrowsClockwise size={11} className={quota.refreshing ? 'is-spinning' : ''}/></button>)}
      <div className="pg-body"><div className="pg-sec"><div className="us-plan"><Account id="claude">Claude Max <em>{claude?.data.plan}</em></Account>{claude?.status === 'ok' && claude.data.reset_credits !== undefined && <span className="meta">{resetsLeft(claude.data.reset_credits, claude.data.reset_ends_at)}</span>}</div>
        {claude?.status === 'ok' ? <div className="bigrings">{(claude.data.windows ?? []).map(w => <Ring key={w.key} w={w} name={w.label} sub={fmtReset(w.resets_at)}/>)}</div> : <p className="muted">{claude?.error ?? 'Not signed in to Claude Code'}</p>}</div>
      <div className="pg-sec"><div className="us-plan"><Account id="codex">Codex <em>{codexUsage?.data.plan}</em></Account>{codexUsage?.status === 'ok' && <span className="meta">{resetsLeft(codexResets)}</span>}
          {port && codexResets > 0 && !reset && <button className="us-use" onClick={askReset}>Use reset</button>}</div>
        {reset && <div className="us-confirm" role="alertdialog" aria-label="Use this reset?">
          <b>{reset.state === 'using' ? 'Using a reset…' : 'Use this reset?'}</b>
          {reset.state === 'error' ? <p className="is-alert">{reset.error}</p>
            : <p>Clears your Codex limits now. {codexResets === 1 ? 'It is your only reset.' : `Uses 1 of your ${codexResets}.`}</p>}
          <div><button className="btn btn-text" disabled={reset.state === 'using'} onClick={() => setReset(null)}>No, go back</button>
            <button className="btn btn-glow" disabled={!reset.armed || reset.state === 'using'} onClick={() => void spendReset()}>{reset.state === 'error' ? 'Try again' : 'Yes, use reset'}</button></div>
        </div>}
        {codexUsage?.status === 'ok' ? <div className="bigrings">{(codexUsage.data.windows ?? []).map(w => <Ring key={w.key} w={w} name={w.label} sub={fmtReset(w.resets_at)}/>)}</div> : <p className="muted">{codexUsage?.error ?? 'Not signed in to Codex'}</p>}</div>
      <div className="pg-sec"><div className="us-plan"><Account id="openai">OpenAI <em>API</em></Account>{openai?.status === 'ok' && <span className="meta">this month {usd(openai.data.month_usd)}</span>}</div>
        {openai?.status === 'ok' ? <Spend total={openai.data.today_usd ?? 0} models={openai.data.by_model ?? []}/> : <p className="muted">{openai?.error ?? 'Needs an admin key'}</p>}</div>
      <div className="pg-sec"><h4>Balances</h4><div className="bal">
        <div className="bal-card"><Account id="deepseek">DeepSeek</Account><b>{deepseek?.status === 'ok' ? usd(deepseek.data.balance) : '—'}</b></div>
        <Balance id="openai" name="OpenAI" left={openai?.status === 'ok' ? openai.data.balance_usd : undefined} since={openai?.data.balance_recorded_at} live={!!port} onSaved={balanceSaved}/>
        <Balance id="minimax" name="MiniMax" left={minimax?.status === 'ok' ? minimax.data.estimate_usd : undefined} since={minimax?.data.anchor_at} live={!!port} onSaved={balanceSaved}/>
      </div></div></div>
    </>,
    plugins: () => {
      const p = plugin ? plugins[plugin] : null;
      const shown = pluginIds.filter(id => plugins[id].name.toLowerCase().includes(query.trim().toLowerCase()));
      return <>
        {back(p ? p.name : 'Plugins', !p && `${pluginsOn} connected`)}
        <div className="pg-body">{p ? <div className="pl-det" key={plugin}>{live?.error && <p className="pg-sec is-warm">{live.error}</p>}<PluginDetail id={plugin!} p={p} token={token} onToken={setToken} onAct={pluginAct}/></div>
          : <div className="pl-cat">
            <label className="pg-sec search"><MagnifyingGlass size={13}/><input type="search" aria-label="Search plugins" placeholder="Search plugins" autoComplete="off" value={query} onChange={e => setQuery(e.target.value)} onPointerDown={focusWindow}/></label>
            {live?.error && <p className="pg-sec is-warm">{live.error}</p>}
            {live && !snapshot && !live.error && <p className="pg-sec muted">Loading plugins…</p>}
            <div className="pg-sec pl-list">{shown.map(id => { const [cls, text] = pluginStatus(plugins[id]);
              return <button key={id} className="pl-row" data-plugin={id} disabled={live?.busy} onClick={() => void openPlugin(id)}><span className={`pl-ic mk-${id}`}><Mark id={id} mark={plugins[id].mark}/></span><span className="pl-name">{plugins[id].name}<small className={cls}>{text}</small></span><CaretRight size={12}/></button>; })}</div>
            {!shown.length && (!live || snapshot) && <p className="muted">No plugins match.</p>}
            <p className="pg-sec muted">Plugins let Jarvis read and act in your apps. It asks before it writes, unless you change that.</p>
          </div>}</div>
      </>;
    },
    projects: () => <>
      {back('Projects', 'last 7 days')}
      <div className="pg-body">{projects.missing ? <p className="pg-sec muted">No projects set up. List them under projects in config/jarvis.yaml.</p> : !projectsView ? <p className="pg-sec muted">Syncing…</p> : <>
        {activeProjects.map(p => <article className="pg-sec pj" key={p.id}>
          <div className="pj-top"><b>{p.name}</b><span>{duration(p.seconds)}{p.commits.count ? ` · ${p.commits.count} commits` : ''}</span></div>
          <Cols days={p.days} dates={projectsView.days}/>
          <small>Today {p.today_seconds ? duration(p.today_seconds) : 'not touched'}{p.recent[0] ? ` · ${p.recent[0].app}, ${p.recent[0].label}` : ''}</small>
        </article>)}
        {projectsView.projects.some(p => !activeProjects.includes(p)) && <p className="pg-sec muted">Not touched this week: {projectsView.projects.filter(p => !activeProjects.includes(p)).map(p => p.name).join(', ')}</p>}
        <footer className="pg-foot"><span>{projects.notice ?? `Other ${duration(projectsView.other.seconds)}${projectsView.unsorted.seconds ? ` · ${duration(projectsView.unsorted.seconds)} not sorted` : ''}`}</span>
          {projectsView.unsorted.seconds > 0 && <button className="btn btn-ghost" disabled={projects.refreshing} onClick={() => void projects.refresh()}>{projects.refreshing ? 'Sorting…' : 'Sort now'}</button>}</footer>
      </>}</div>
    </>,
  };

  return <div className="ad" data-page={page ?? undefined} onKeyDown={keys}>
    <div className="view" ref={view}>
      <div className="overview" ref={home} inert={!!page}>
        <div className="row r-voice" data-row="conversation">
          <button className="voice-open" aria-label="Open Conversation" onClick={() => openPage('conversation')}>
            <span className="say" key={saying.text}>{saying.text}</span>
            <span className={`cap ${saying.busy ? 'is-busy' : ''}`}><i/><span>{saying.caption}</span></span>
          </button>
        </div>
        <button className="row r-agents" data-row="agents" aria-label="Open Agents" onClick={() => openPage('agents')}>
          <span className="head"><span className="label">Agents</span><span className="head-r">
            <span className="orbs">{[...waiting, ...stopped, ...finished, ...working].slice(0, 5).map(s => <AgentMark key={s.id} id={s.id} look={marks} state={markOf(s)} size={12}/>)}</span>
            {(waiting.length > 0 || working.length > 0) && <span className={`pill ${waiting.length ? 'is-waiting' : ''}`}>{waiting.length ? `${waiting.length} ${waiting.length > 1 ? 'need' : 'needs'} you` : `${working.length} working`}</span>}
          </span></span>
          <span className="text one">{lead ? <><span className={`tagc ${lead.agent}`}>{AGENT_NAME[lead.agent]}</span>{lead.title}{lead.state === 'wait' && lead.last ? ` · ${lead.last.replace(/^Wants/, 'wants')}` : ''}</> : 'Sessions show up once you start one.'}</span>
        </button>
        <button className="row r-now" data-row="now" aria-label="Open Right now" onClick={() => openPage('now')}>
          <span className="head"><span className="label">Now</span><span className={`meta ${fresh.stale ? 'is-warm' : ''}`} title={fresh.text}>{now ? `${now.current ? 'as of' : 'at'} ${now.at}` : ''}</span></span>
          <span className="text">{now ? now.claim : workView === null ? 'Syncing…' : 'No recent activity observed'}</span>
        </button>
        <button className="row r-usage" data-row="usage" aria-label="Open Usage" onClick={() => openPage('usage')}>
          <span className="head"><span className="label">Usage</span><span className="meta">OpenAI today <b>{usd(openai?.data.today_usd)}</b></span></span>
          <span className="rings">
            <UsageGroup name="Claude Max" plan={claude?.data.plan} ok={claude?.status === 'ok'} windows={(claude?.data.windows ?? []).slice(0, 3)} synced={!!quota.usage}/>
            <span className="split"/>
            <UsageGroup name="Codex" plan={codexUsage?.data.plan?.split(' ')[0]} ok={codexUsage?.status === 'ok'} windows={(codexUsage?.data.windows ?? []).slice(0, 1)} synced={!!quota.usage}/>
          </span>
        </button>
        <div className="tiles">
          <button className="row tile" data-row="plugins" aria-label="Open Plugins" onClick={() => openPage('plugins')}>
            <span className="head"><span className="label">Plugins</span><span className="meta">{pluginsOn} on</span></span>
            <span className="pl-mini">{pluginIds.slice(0, 4).map(id => <i key={id} className={`mk-${id} ${plugins[id].state === 'on' ? 'on' : plugins[id].state === 'off' ? 'off' : 'need'}`} title={`${plugins[id].name}: ${pluginStatus(plugins[id])[1]}`}><Mark id={id} mark={plugins[id].mark}/></i>)}</span>
          </button>
          <button className="row tile" data-row="projects" aria-label="Open Projects" onClick={() => openPage('projects')}>
            <span className="head"><span className="label">Projects</span><span className="meta">7 d</span></span>
            <span className="pj-mini">{topProject ? <><span><b>{topProject.name}</b> {duration(topProject.seconds)}</span><Cols days={topProject.days}/></> : <span>{projects.missing ? 'Not set up' : projectsView ? 'No time yet' : 'Syncing…'}</span>}</span>
          </button>
        </div>
      </div>
      {page && <section className="page" ref={pageEl} aria-label={TITLES[page]}>{pages[page]()}</section>}
    </div>
    <div className="cmp-hit" onClick={e => { const input = e.currentTarget.querySelector('input'); if (input && !(e.target as Element).closest('button,input')) focusWindow({ currentTarget: input }); }}><Ask className="cmp" onAsk={ask}/></div>
    <div className={`toast ${toast ? 'is-on' : ''}`} role="status">{toast?.text}{toast?.undo && <button onClick={() => { toast.undo!(); setToast(null); }}>Undo</button>}</div>
  </div>;
}

// The companion window takes no key focus until you reach for a text box.
const focusWindow = (event: { currentTarget: HTMLElement }) => { const el = event.currentTarget; void window.jarvis?.focus(true).then(() => el.focus({ preventScroll: true })); };

function Ask({ className, onAsk }: { className: string; onAsk: (text: string) => void }) {
  const [text, setText] = useState('');
  return <form className={className} onSubmit={event => {
    event.preventDefault();
    if (!text.trim()) return;
    onAsk(text.trim()); setText(''); event.currentTarget.querySelector('input')?.blur();
  }}>
    <input aria-label="Message Jarvis" placeholder="Message Jarvis…" autoComplete="off" value={text} onChange={event => setText(event.target.value)}
      onPointerDown={focusWindow} onKeyDown={event => { if (event.key === 'Enter' && event.nativeEvent.isComposing) event.preventDefault(); }}/>
    <button className="send" aria-label="Send" disabled={!text.trim()}><ArrowUp size={13} weight="bold"/></button>
  </form>;
}

function Fold({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return <><button className="fold" aria-expanded={open} onClick={() => setOpen(value => !value)}>{label}<CaretDown size={11}/></button>
    <div className="fold-body" inert={!open}><div>{children}</div></div></>;
}

// The service's own usage or billing page, in the browser; main keeps the list of pages.
function Account({ id, children }: { id: string; children: ReactNode }) {
  return <button className="us-link" title="Open in the browser" onClick={() => void window.jarvis?.openAccount?.(id)}>{children}<ArrowSquareOut size={11} className="us-out"/></button>;
}
// Short names fit under a small ring on the home page; the Usage page spells them out.
const RING_NAME: Record<string, string> = { five_hour: '5 h', seven_day: '7 d', seven_day_fable: 'Fable', primary_window: '7 d' };
function Ring({ w, name, sub }: { w: UsageWindow; name: string; sub: string }) {
  const pct = Math.max(0, Math.min(100, w.percent));
  return <span className={`ring ${pct >= 90 ? 'is-critical' : pct >= 75 ? 'is-warning' : ''}`} title={`${w.label} · ${fmtReset(w.resets_at)}`}>
    <span className="dial" style={{ '--fill': pct } as CSSProperties} role="meter" aria-label={`${w.label} used`} aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}><b>{Math.round(pct)}<small>%</small></b></span>
    {name}<em>{sub}</em>
  </span>;
}
function UsageGroup({ name, plan, ok, windows, synced }: { name: string; plan?: string; ok: boolean; windows: UsageWindow[]; synced: boolean }) {
  return <span className="group"><span className="plan">{name} <em>{plan}</em></span>
    {ok && windows.length ? <span className="ringset">{windows.map(w => <Ring key={w.key} w={w} name={RING_NAME[w.key] ?? w.label} sub={fmtReset(w.resets_at).replace('resets in ', '')}/>)}</span>
      : <span className="plan">{synced ? 'Not set up' : 'Syncing…'}</span>}
  </span>;
}
// Today's spend as one ring cut by model, biggest first; the list beside it names each cut.
// The cuts sit on their own element so the fill animation reaches the gradient.
// Only models that cost a cent today are listed; the rest fold into one line that opens them.
function Spend({ total, models }: { total: number; models: { model: string; today_usd: number }[] }) {
  const [all, setAll] = useState(false);
  const sorted = [...models].sort((a, b) => b.today_usd - a.today_usd), tint = (i: number) => `rgb(var(--glow) / ${Math.max(.2, 1 - i * .55)})`;
  const paid = sorted.filter(m => m.today_usd >= .005), free = sorted.length - paid.length;
  let edge = 0;
  const stops = sorted.map((m, i) => { const from = edge; edge += total ? m.today_usd / total : 0; return `${tint(i)} calc(var(--fill) * ${from}%) calc(var(--fill) * ${edge}%)`; });
  return <div className="spend">
    <span className="ring"><span className="dial donut"><i style={{ background: `conic-gradient(${[...stops, 'rgb(var(--glow) / .12) 0'].join(',')})` }}/><b>{usd(total)}<small>today</small></b></span></span>
    <ul>{(all ? sorted : paid).map((m, i) => <li key={m.model}><i style={{ background: tint(i) }}/>{m.model}<span>{usd(m.today_usd)}</span></li>)}
      {free > 0 && <li><button className="more" aria-expanded={all} onClick={() => setAll(v => !v)}>{all ? 'Show less' : `${free} more at $0.00`}</button></li>}</ul>
  </div>;
}
// OpenAI and MiniMax report no balance (ADR 0050): Allen types the one on their billing page and
// the daemon subtracts what is spent after it, so the number shown is an estimate since then.
function Balance({ id, name, left, since, live, onSaved }: { id: 'openai' | 'minimax'; name: string; left?: number; since?: string | null; live: boolean; onSaved: () => void }) {
  const [draft, setDraft] = useState<string | null>(null), [saving, setSaving] = useState(false), [error, setError] = useState('');
  const amount = Number(draft), valid = !!draft?.trim() && Number.isFinite(amount) && amount >= 0;
  const input = useCallback((el: HTMLInputElement | null) => {
    if (!el) return;
    el.closest('form')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    void window.jarvis?.focus(true).then(() => el.focus({ preventScroll: true }));
  }, []);
  const save = async () => {
    if (!valid || saving) return;
    setSaving(true); setError('');
    try { await window.jarvis!.usageBalance(id, amount); setDraft(null); onSaved(); }
    catch (e) { setError(cleanError(e)); }
    finally { setSaving(false); }
  };
  if (draft !== null) return <form className="bal-card is-editing" onSubmit={e => { e.preventDefault(); void save(); }}>
    <span className="bal-name">{name} balance now</span>
    <label className="bal-input">$<input ref={input} aria-label={`${name} balance`} inputMode="decimal" autoComplete="off" placeholder="0.00" value={draft}
      onChange={e => setDraft(e.target.value.replace(/[^\d.]/g, ''))} onPointerDown={focusWindow} onKeyDown={e => { if (e.key === 'Escape') setDraft(null); }}/></label>
    {error && <p className="is-alert">{error}</p>}
    <div><button type="button" className="btn btn-text" onClick={() => setDraft(null)}>Cancel</button><button className="btn btn-glow" disabled={!valid || saving}>{saving ? 'Saving…' : 'Save'}</button></div>
  </form>;
  return <div className="bal-card">
    <Account id={id}>{name}</Account>
    <b>{left === undefined ? '—' : `≈ ${usd(left)}`}</b>
    <small>{since ? `since ${new Date(since).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}` : 'not set'}</small>
    {live && <button className="bal-set" onClick={() => setDraft('')}>{since ? 'Update' : 'Set'}</button>}
  </div>;
}
// One column per day, today last. With dates, hovering a column tells its day and hours.
function Cols({ days, dates }: { days: number[]; dates?: string[] }) {
  const peak = Math.max(...days, 1);
  const day = (iso: string, i: number) => i === days.length - 1 ? 'Today'
    : `${new Date(`${iso}T12:00:00`).toLocaleDateString('en-US', { weekday: 'short' })} ${Number(iso.slice(5, 7))}/${Number(iso.slice(8, 10))}`;
  return <span className="cols" role={dates ? 'img' : undefined} aria-hidden={!dates} aria-label={dates ? `Hours per day, today last: ${days.map(duration).join(', ')}` : undefined}>
    {days.map((s, i) => <span key={i} style={{ '--h': s / peak * 100 } as CSSProperties}><i/>{dates?.[i] && <b className="tip">{day(dates[i], i)} · {s ? duration(s) : 'nothing'}</b>}</span>)}
  </span>;
}

// Folded: the state mark, the session's name and its tags. A click opens it to what you said, what it is
// doing, and the actions; the jump to its terminal or Codex thread lives there too.
function AgentRow({ s, look, mark, open, onToggle, onOpen, onHide, actions }: { s: Agent; look: MarkLook; mark: MarkState; open: boolean; onToggle: () => void; onOpen?: () => void; onHide: () => void; actions?: ReactNode }) {
  return <article className={`ag is-${s.state} ${open ? 'is-open' : ''}`} data-id={s.id}>
    <AgentMark look={look} state={mark} id={s.id} size={14}/>
    <div className="ag-body">
      <button className="ag-head" aria-expanded={open} onClick={onToggle}>
        <span className="ag-top"><span className="ag-title">{s.title}</span><span className="age">{s.age}</span></span>
        <span className="ag-tags"><span className={`tagc ${s.agent}`}>{AGENT_NAME[s.agent]}</span><span className="tagc">{s.project}</span>
          {s.where !== 'Codex' && <span className="tagc">{s.where}</span>}{s.sub && <span className="tagc sub">Subagent</span>}</span>
      </button>
      <div className="ag-more" inert={!open}><div>
        {s.branch && <span className="ag-meta"><GitBranch size={10}/>{s.branch}</span>}
        {s.you && <span className="ag-you"><b>You</b>{s.you}</span>}
        {s.last && <span className="ag-last">{s.last}</span>}
        <span className="ag-actions">{s.state === 'wait' && actions}{onOpen && <button className="ag-go" onClick={onOpen}>Open in {s.where}<ArrowSquareOut size={11}/></button>}</span>
      </div></div>
    </div>
    <button className="ag-x" aria-label={`Hide ${s.title}`} onClick={onHide}><X size={11}/></button>
    {s.state === 'work' && <i className="shimmer"/>}
  </article>;
}

function PluginDetail({ id, p, token, onToken, onAct }: { id: string; p: DemoPlugin; token: string; onToken: (value: string) => void; onAct: (act: string, value?: string) => void }) {
  const top = <><div className="pg-sec pl-id"><span className={`pl-ic lg mk-${id}`}><Mark id={id} mark={p.mark}/></span><h5>{p.name}</h5><p>{p.about}</p>{p.state === 'on' && <span className="pill is-new">Connected</span>}</div>
    {p.error && p.state !== 'on' && p.state !== 'connecting' && <p className="pg-sec is-warm" role="alert">{p.error}</p>}</>;
  if (p.unsupported) return <>{top}<p className="pg-sec muted">{p.unsupported}</p></>;
  const act = (name: string, label: string, className = 'btn-text') => <button className={className} onClick={() => onAct(name)}>{label}</button>;
  if (p.state === 'connecting') return <>{top}
    <div className="pg-sec waiting" role="status"><span className="spin-ring"/><p>{p.kind === 'oauth' ? 'Waiting for you in the browser…' : 'Connecting…'}</p><p className="muted">{p.ask ? 'Jarvis picks the task back up once you’re in.' : 'You can close the panel. It keeps waiting.'}</p></div>
    <div className="pg-sec acts row-btns">{p.kind === 'oauth' && act('reopen', 'Open sign-in page again', 'btn btn-ghost')}{act('cancel', 'Cancel')}</div>
  </>;
  if (p.state === 'on') return <>{top}
    {p.resumed && <div className="pg-sec ask-card is-ok"><span>Back to your task</span>{p.resumed}</div>}
    <div className="pg-sec"><div><div className="kv"><span>Tools</span><span>{p.toolCount}</span></div><div className="kv"><span>Can</span><span>{p.can?.length ? p.can.join(' · ') : 'Read · Write'}</span></div></div></div>
    <label className="pg-sec field">Ask before acting<select aria-label="Ask before acting" value={p.approval ?? 'auto'} onChange={e => onAct('approval', e.target.value)}>
      {p.approval === 'configured' && <option value="configured" disabled>Each service’s own setting</option>}
      <option value="auto">Let Jarvis decide (default)</option><option value="prompt">Ask every time</option><option value="writes">Ask before it writes</option><option value="approve">Don’t ask</option></select><small>A tool’s own setting wins over this.</small></label>
    {p.tools.length > 0 && <div className="pg-sec"><Fold label="Show tools"><pre>{p.tools.join('\n')}</pre></Fold></div>}
    <footer className="pg-foot"><span>Turning it off removes its tools.</span>{act('off', 'Turn off', 'btn-text is-alert')}</footer>
  </>;
  if (p.state === 'token') return <>{top}
    <label className="pg-sec field">Access token<input type="password" placeholder={p.saved ? 'Saved · leave empty to keep it' : 'ghp_…'} autoComplete="off" aria-label={`${p.name} access token`} value={token} onChange={e => onToken(e.target.value)} onPointerDown={focusWindow}/><small>Stays on this Mac. It never goes into the conversation.</small></label>
    <div className="pg-sec acts">{act('connect', 'Connect', 'btn btn-glow wide')}{act('later', 'Not now')}</div>
  </>;
  if (p.state === 'off') return <>{top}
    <ul className="pg-sec facts"><li><ArrowSquareOut size={13}/><span>Adds its tools to Jarvis on this Mac.</span></li><li><ShieldCheck size={13}/><span>Asks before it writes, unless you change that.</span></li></ul>
    <div className="pg-sec acts">{act('connect', 'Turn on', 'btn btn-glow wide')}{act('later', 'Not now')}</div>
  </>;
  return <>{top}
    {p.ask && <div className="pg-sec ask-card"><span>Jarvis asked for this</span>“{p.ask}”</div>}
    <ul className="pg-sec facts"><li><ArrowSquareOut size={13}/><span>Opens {p.name} in your browser to sign in.</span></li><li><ShieldCheck size={13}/><span>You choose what it can see there.</span></li></ul>
    <div className="pg-sec acts">{act('connect', p.ask ? 'Sign in and continue' : 'Sign in', 'btn btn-glow wide')}{act('later', 'Not now')}</div>
  </>;
}

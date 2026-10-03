import { useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type ReactNode, type Ref, type WheelEvent } from 'react';
import { ArrowSquareOut, ArrowUp, ArrowsClockwise, CaretDown, CaretLeft, CaretRight, CaretUp, ChatCircle, Check, Cloud, CloudFog, CloudLightning, CloudRain, CloudSnow, EnvelopeSimple, GearSix, GitBranch, MagnifyingGlass, ShieldCheck, SpeakerHigh, SpeakerSlash, Sun, X } from '@phosphor-icons/react';
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
import { HOME_DEFAULTS, isPop, tr, useCompanionSettings, useT, type BlockId, type L, type Lang } from './companionSettings';
import { demoBrief, demoMail, demoNotices, demoToday, postRoute, useNow, useRoute, type Brief, type Mail, type Notice, type Today, type WxKind } from './homeData';
import { ArrangeHome, BLOCK } from './ArrangeHome';
import { BriefPage } from './BriefPage';
import { SettingsPage, type Account, type AccountKeyDrafts, type Controls } from './SettingsPage';
import { ActionCard, MailCard, QuestionCard, type Answer, type Card, type Decide, type Question } from './ActionCard';
import { MOTION } from './motion';
import './dashboard-around.css';
import './dashboard-home.css';

// The Dashboard around her: one column under the companion. A corner strip beside her, then the home's
// blocks: the ones you keep, in your order, and the ones that show up when there is something. A block grows
// into its page in place; the panel follows the blocks up to VIEW_MAX. Her light accents the surface;
// measurements and agent states keep their own stable colours.
type Page = 'conversation' | 'now' | 'agents' | 'usage' | 'plugins' | 'projects' | 'settings' | 'arrange' | 'brief';
export type DashboardView = {
  page: Page | null; plugin: string | null; settingsCat: string | null; unfolded: string | null;
  query: string; token: string; homeDraft: string; talkDraft: string; days: number; scroll: number;
  accountKeyDrafts: AccountKeyDrafts; conversationFirstSeq: number | null;
  briefRead: string; dismissed: Partial<Record<BlockId, string>>; hidden: Record<string, string>;
  actionDraft: { id: string; value: { subject: string; body: string } } | null;
  questionDraft: { id: string; value: Record<string, string> } | null;
};
export type DashboardViewHandle = { snapshot: () => DashboardView; restore: (value: DashboardView) => void };
const TITLES: Record<Page, L> = { conversation: ['Conversation', '对话'], now: ['Right now', '现在'], agents: ['Agents', 'Agents'], usage: ['Usage', '用量'], plugins: ['Plugins', '插件'], projects: ['Projects', '项目'], settings: ['Settings', '设置'], arrange: ['Arrange the home', '编辑首页'], brief: ['Morning brief', '早报'] };
// The home follows its blocks from the old fixed height up to this, then scrolls inside the panel.
const VIEW_MIN = 466, VIEW_MAX = 600, CORNER = 28, HOME_GAP = 8, HOLD = 560, TALK_STAYS = 10 * 60_000;
// The room the resting input keeps under the home (the panel's bottom padding, 58 against a page's 12): a page, where the input is gone, takes it.
const DOCK = 46;
const WX: Record<WxKind, ReactNode> = { sun: <Sun/>, cloud: <Cloud/>, rain: <CloudRain/>, snow: <CloudSnow/>, fog: <CloudFog/>, storm: <CloudLightning/> };
const PAGE_MS = MOTION.medium, EXIT_MS = PAGE_MS * MOTION.exit;
const PAGE_EASE = 'cubic-bezier(.16,1,.3,1)', EXIT_EASE = 'cubic-bezier(.7,0,.84,0)';
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const dur = (ms: number) => reduced.matches ? 0 : ms;
const usd = (n?: number) => n === undefined ? '—' : `${n <= -.005 ? '-' : ''}$${Math.abs(n).toFixed(2)}`;
const pad = (n: number) => String(n).padStart(2, '0');
const hm = (ms: number) => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
// Claude and Codex both hand out limit resets; one wording for both.
const resetsLeft = (lang: Lang, n: number, until?: string | null) => {
  const date = n && until ? new Date(until).toLocaleDateString(lang === 'zh' ? 'zh-CN' : 'en-US', { month: 'short', day: 'numeric' }) : '';
  return lang === 'zh' ? `还剩 ${n} 次重置${date ? ` · ${date}前` : ''}` : `${n} reset${n === 1 ? '' : 's'} left${date ? ` · until ${date}` : ''}`;
};
// "Yes" wakes ARM_MS after the question, so a double click on "Use reset" cannot land on it;
// an unanswered question folds away after ASK_MS. The answers are Codex's own words.
const ARM_MS = 600, ASK_MS = 10_000;
const RESET_ANSWERS: Record<string, L> = {
  reset: ['Codex limits reset', 'Codex 额度已重置'], nothing_to_reset: ['Your usage does not need a reset right now', '现在的用量不需要重置'],
  no_credit: ['No resets left', '没有重置次数了'], already_redeemed: ['That reset already went through', '这次重置已经生效了'],
};

const STALE_MS = { hour: 3_600_000, day: 86_400_000, never: Infinity };
// Hidden rows stay hidden until the session is given a new prompt.
const HIDDEN = 'companion-hidden-agents-v1';
// The day whose brief you have seen: it shows the first time the home opens that morning, then not again that day.
const BRIEF_READ = 'companion-brief-read-v1';

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
const pluginStatus = (p: DemoPlugin): [string, L] => p.unsupported ? ['', ['Not supported', '不支持']] : p.state === 'on' ? ['is-on', ['Connected', '已连接']]
  : p.error && p.state !== 'connecting' ? ['is-need', ['Connection problem', '连接有问题']]
  : p.state === 'connecting' ? ['is-need', p.kind === 'oauth' ? ['Waiting for sign-in…', '等你登录…'] : ['Connecting…', '连接中…']]
  : p.state === 'off' ? ['', ['Off', '关']] : p.state === 'token' ? ['is-need', ['Needs an access token', '要一个访问令牌']]
  : ['is-need', p.ask ? ['Jarvis asked · needs sign-in', 'Jarvis 要用 · 要登录'] : ['Needs sign-in', '要登录']];

type Turn = { you: string; at: string; jarvis?: string; jarvisAt?: string; work?: [string, string]; day?: string; mail?: string[]; thought?: number };
const DEMO_TURNS: Turn[] = [
  { you: 'Remind me to test the mic at four.', at: '11:05', jarvis: 'Done. I’ll remind you at 4 PM.', jarvisAt: '11:05' },
  { you: 'What’s left on my plate today?', at: '14:32', jarvisAt: 'just now',
    jarvis: 'Two things left today. Your 4 PM reminder is set. You can confirm the dashboard direction first, then check the voice test at 4 PM.',
    work: ['Worked it out with gpt-5.6-luna · 2.1 s', 'Checked today’s to-dos:\n1. Confirm the Resonance dashboard direction.\n2. 16:00 voice test reminder, scheduled.'] },
];
const ANSWER = 'Got it. I’ll take care of it and tell you when it’s done.';
const BASIS: Record<Basis, L> = { observed: ['Observed', '看到的'], stated: ['You said', '你说的'], inferred: ['A guess', '猜的'] };
// Live conversation: the memory.db rows and the answer still streaming, from the companion's daemon link.
// `older` fetches a longer page and says whether it brought earlier rows; `floor` means the history's start is on hand.
// `card` is the one waiting for a button (ADR 0062); it sits above the input until it is sent or dismissed.
// `question` is the ask card (ADR 0066), in the same place, until it is filled in, dismissed or talked over.
type Talk = { rows: Row[]; tail: string; busy: boolean; offline: boolean; floor: boolean; submit: (text: string) => void; older: () => Promise<boolean>; card?: Card | null; decide?: Decide;
  question?: Question | null; answer?: Answer; think: Think };
// Think mode (ADR 0064): whether this turn is deep, the seconds of the deep answer still coming, the words that make a turn deep,
// and each deep answer's wait by the log position its row lands after.
export type Think = { on: boolean; secs: number; words: RegExp | null; thoughts: { after: number; secs: number }[] };
// A sentence with an on-word in it is a deep turn.
const deepAfter = (think: Think, text: string) => !!think.words?.test(text);
const when = (ts: string) => { const d = new Date(ts); return Number.isNaN(d.getTime()) ? '' : d.toDateString() === new Date().toDateString() ? hm(d.getTime()) : `${d.getMonth() + 1}/${d.getDate()} ${hm(d.getTime())}`; };
const dayLabel = (lang: Lang, day: string) => { const d = new Date(day); return d.toDateString() === new Date(Date.now() - 86_400_000).toDateString() ? tr(lang, ['yesterday', '昨天']) : `${d.toLocaleDateString(lang === 'zh' ? 'zh-CN' : 'en-US', { weekday: 'short' })} ${d.getMonth() + 1}/${d.getDate()}`; };
// The conversation of record as turns, each dated by the row that opens it: your rows open one, and Jarvis's rows after it answer it.
// `thought` holds a deep answer's wait, keyed by its row's seq.
const toTurns = (rows: Row[], thought = new Map<number, number>()): Turn[] => {
  const turns: Turn[] = [];
  for (const row of rows) {
    const t = turns.at(-1), text = visible(row.text), at = when(row.ts), day = new Date(row.ts).toDateString();
    if (row.source === 'allen') turns.push({ you: row.text, at, day });
    // ADR 0063: an email Jarvis read whole shows above its answer.
    else if (row.source === 'mail') { if (t) t.mail = [...t.mail ?? [], row.text]; else turns.push({ you: '', at: '', day, mail: [row.text] }); }
    else if (!t) turns.push({ you: '', at: '', jarvis: text, jarvisAt: at, day, thought: thought.get(row.seq) });
    else Object.assign(t, { jarvis: t.jarvis ? `${t.jarvis}\n\n${text}` : text, jarvisAt: at, thought: t.thought ?? thought.get(row.seq) });
  }
  return turns;
};
// A deep answer's wait goes on the first answer row past where the log stood when it opened.
const thoughtRows = (rows: Row[], thoughts: Think['thoughts']) => new Map(thoughts.flatMap(({ after, secs }) => {
  const row = rows.find(r => r.seq > after && r.source !== 'allen');
  return row ? [[row.seq, secs] as const] : [];
}));
const PULL = 240; // px of fresh upward scroll at the top that adds the day before

export function AroundDashboard({ open, port = null, onClose, onMood, onHop, talk, plugins: live, pluginFocus = null, marks = 'spark', onAgents, agentsFocus = 0, settingFocus = 0, onAnswer, unread, ctl, viewRef, onView }: {
  open: boolean; port?: string | null; onClose: () => void; onMood: (expr: ExprId | null) => void; onHop: (height: number) => void;
  talk?: Talk; plugins?: PluginController; pluginFocus?: { plugin: string; key: string } | null;
  marks?: MarkLook; onAgents?: (agents: ShownAgent[]) => void; agentsFocus?: number; settingFocus?: number; onAnswer?: (id: string) => void;
  unread?: ReadonlySet<string>; ctl: Controls; viewRef?: Ref<DashboardViewHandle>; onView?: (value: DashboardView) => void;
}) {
  const [settings, updateSettings] = useCompanionSettings(), lang = settings.lang;
  const t = (l: L) => tr(lang, l);
  const tick = useNow(20_000);
  const quota = useUsage(port), codex = useCodexSessions(port), work = useWorkState(port), projects = useProjects(port, open), claudeRows = useClaudeSessions(port);
  const [page, setPage] = useState<Page | null>(null);
  const [plugin, setPlugin] = useState<string | null>(null);
  const [settingsCat, setSettingsCat] = useState<string | null>(null);
  const [demoPlugins, setPlugins] = useState(DEMO_PLUGINS);
  const snapshot = live?.snapshot;
  const plugins: Record<string, DemoPlugin> = !live ? demoPlugins : Object.fromEntries((snapshot?.plugins ?? []).map(p => [p.id, fromPlugin(p, snapshot!.request)]));
  const pluginIds = !live ? PLUGIN_ORDER : [...snapshot?.plugins ?? []].sort((a, b) => Number(b.status === 'ready') - Number(a.status === 'ready')
    || Number(b.supported) - Number(a.supported) || a.name.localeCompare(b.name)).map(p => p.id);
  const [query, setQuery] = useState('');
  const [token, setToken] = useState('');
  const [briefRead, setBriefRead] = useState(() => { if (!port) return ''; try { return localStorage.getItem(BRIEF_READ) ?? ''; } catch { return ''; } });
  useEffect(() => { if (port) try { localStorage.setItem(BRIEF_READ, briefRead); } catch { /* shows again after a restart */ } }, [briefRead]);
  const briefShown = useRef('');
  const [dismissed, setDismissed] = useState<Partial<Record<BlockId, string>>>({});
  const [homeDraft, setHomeDraft] = useState(''), [talkDraft, setTalkDraft] = useState('');
  const [accountKeyDrafts, setAccountKeyDrafts] = useState<AccountKeyDrafts>({});
  const [actionDraft, setActionDraft] = useState<DashboardView['actionDraft']>(null);
  const [questionDraft, setQuestionDraft] = useState<DashboardView['questionDraft']>(null);
  const [said, setSaid] = useState({ text: 'Two things left today. Your 4 PM reminder is set.', caption: 'Jarvis · just now', busy: false });
  const [turns, setTurns] = useState(DEMO_TURNS), [demoTalkAt, setDemoTalkAt] = useState(0);
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

  // Only presentation and unsent edits cross between the two trusted windows. A
  // pending action or an armed spending confirmation is never replayed by a move.
  const restoredScroll = useRef<number | null>(null);
  const conversationRestore = useRef<{ firstSeq: number | null; scroll: number; loading: boolean; paused: boolean } | null>(null);
  const [restoringConversation, setRestoringConversation] = useState(false), [restoreRevision, setRestoreRevision] = useState(0);
  const snapshotView = (): DashboardView => ({ page, plugin, settingsCat, unfolded, query, token, homeDraft, talkDraft, days,
    accountKeyDrafts, conversationFirstSeq: conversationRestore.current?.firstSeq ?? (page === 'conversation' ? talk?.rows[0]?.seq ?? null : null),
    scroll: conversationRestore.current?.scroll ?? pageEl.current?.querySelector('.pg-body')?.scrollTop ?? 0, actionDraft, questionDraft, briefRead, dismissed, hidden });
  useImperativeHandle(viewRef, () => ({ snapshot: snapshotView, restore: value => {
    closing.current = false;
    if (view.current) stopMotion(view.current);
    pageEl.current?.classList.remove('is-closing');
    // A reused window can still hold the previous page's filled opacity animation.
    // Keep the overview hidden only when the restored view is another page.
    if (value.page && home.current) home.current.animate([{ opacity: 0 }, { opacity: 0 }], { duration: 0, fill: 'forwards' });
    setReset(null); setPage(value.page); setPlugin(value.plugin); setSettingsCat(value.settingsCat); setUnfolded(value.unfolded);
    setQuery(value.query); setToken(value.token); setHomeDraft(value.homeDraft); setTalkDraft(value.talkDraft); setDays(value.days);
    setAccountKeyDrafts(value.accountKeyDrafts); setActionDraft(value.actionDraft); setQuestionDraft(value.questionDraft); setBriefRead(value.briefRead); setDismissed(value.dismissed); setHidden(value.hidden);
    const history = value.page === 'conversation' && !!talk;
    conversationRestore.current = history ? { firstSeq: value.conversationFirstSeq, scroll: value.scroll, loading: false, paused: false } : null;
    setRestoringConversation(history); setRestoreRevision(n => n + 1); restoredScroll.current = history ? null : value.scroll;
  } }));
  const publishView = useRef(onView); publishView.current = onView;
  useEffect(() => {
    if (restoredScroll.current !== null) {
      const el = pageEl.current?.querySelector('.pg-body'); if (el) el.scrollTop = restoredScroll.current;
      restoredScroll.current = null;
    }
    publishView.current?.(snapshotView());
  }, [page, plugin, settingsCat, unfolded, query, token, homeDraft, talkDraft, days, accountKeyDrafts, actionDraft, questionDraft, briefRead, dismissed, hidden, restoringConversation]);

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
    conversationRestore.current = null; restoredScroll.current = null; setRestoringConversation(false);
    closing.current = false; setPage(null); setPlugin(null); setSettingsCat(null); setUnfolded(null); setReset(null); react('02', 0);
    // Having been on the home once is having seen the brief; the next opening that day leaves it out.
    if (briefShown.current) { setBriefRead(briefShown.current); briefShown.current = ''; }
    if (home.current) stopMotion(home.current);
    if (view.current?.contains(document.activeElement)) { (document.activeElement as HTMLElement).blur(); void window.jarvis?.focus(false); }
  }, [open]);

  // The page grows out of what opened it: its row, the corner button, or the block held to arrange the home.
  const origin = useRef<HTMLElement | null>(null);
  const from = () => origin.current?.isConnected ? origin.current : null;
  const insetOf = (el: HTMLElement) => {
    const v = view.current!.getBoundingClientRect(), r = el.getBoundingClientRect();
    return `inset(${r.top - v.top}px ${v.right - r.right}px ${v.bottom - r.bottom}px ${r.left - v.left}px round 14px)`;
  };
  // Only animations started here; her CSS loops (orbs, pills, rings) keep running.
  const stopMotion = (el: HTMLElement) => el.getAnimations({ subtree: true }).forEach(a => { if (!(a instanceof CSSAnimation || a instanceof CSSTransition)) a.cancel(); });
  const openPage = (name: Page, el?: HTMLElement | null) => {
    if (page || closing.current) return;
    origin.current = el ?? home.current?.querySelector<HTMLElement>(`[data-row="${name}"]`) ?? null;
    setPage(name); setPlugin(null); setSettingsCat(null);
    if (name === 'conversation') { setDays(1); react(pick(TAKES.reply), 2600); }
    else if (name === 'now') react('37', 2400);
    else if (name === 'projects') { react('40', 1500); void projects.refresh(); }
    else if (name === 'settings') react('30', 1400);
    else if (name === 'arrange') react('14', 1200);
    else if (name === 'brief') react('10', 1600);
    else { react('02', 0); if (name === 'agents') onHop(.2); }
  };
  // The row grows into the page: its outline opens to the whole panel and its title slides up to the top.
  useLayoutEffect(() => {
    const el = pageEl.current;
    if (!page || !el) return;
    const start = from(), dy = start ? start.getBoundingClientRect().top - view.current!.getBoundingClientRect().top : 0;
    if (page === 'conversation') { const b = el.querySelector('.pg-body')!; b.scrollTop = b.scrollHeight; } // it opens on the newest turn
    if (start) el.animate([{ clipPath: insetOf(start) }, { clipPath: 'inset(0 0 0 0 round 14px)' }], { duration: dur(PAGE_MS), easing: PAGE_EASE });
    else el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: dur(PAGE_MS), easing: PAGE_EASE });
    el.querySelector('.pg-head')?.animate([{ transform: `translateY(${dy}px)`, opacity: .3 }, { transform: 'none', opacity: 1 }], { duration: dur(PAGE_MS), easing: PAGE_EASE });
    // Only what is on screen fades in, in order; a long page would otherwise hold its newest words back.
    const box = el.getBoundingClientRect();
    [...el.querySelectorAll('.pg-sec, .pg-foot, .pg-input')].filter(s => { const r = s.getBoundingClientRect(); return r.bottom > box.top && r.top < box.bottom; }).forEach((s, i) => s.animate([{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }],
      { duration: dur(MOTION.fast), delay: dur(Math.min(i, 4) * MOTION.stagger), easing: PAGE_EASE, fill: 'backwards' }));
    stopMotion(home.current!);
    home.current!.animate([{ opacity: 1 }, { opacity: 0 }], { duration: dur(MOTION.fast), fill: 'forwards' });
    el.querySelector<HTMLElement>('.pg-back')?.focus({ preventScroll: true });
  }, [page]);
  const closePage = () => {
    const el = pageEl.current;
    if (!page || !el || closing.current) return;
    conversationRestore.current = null; setRestoringConversation(false);
    const back = from();
    closing.current = true;
    // Reverse the same geometry into the opening row, with a shorter exit.
    el.classList.add('is-closing');
    el.querySelectorAll('.pg-head, .pg-body').forEach(part => part.animate([{ opacity: 1 }, { opacity: 0, transform: 'translateY(-4px)' }], { duration: dur(MOTION.fast * MOTION.exit), easing: EXIT_EASE, fill: 'forwards' }));
    const shrink = back ? el.animate([{ clipPath: 'inset(0 0 0 0 round 14px)' }, { clipPath: insetOf(back) }], { duration: dur(EXIT_MS), easing: EXIT_EASE, fill: 'forwards' })
      : el.animate([{ opacity: 1 }, { opacity: 0 }], { duration: dur(EXIT_MS), easing: EXIT_EASE, fill: 'forwards' });
    if (back) el.animate([{ opacity: 1 }, { opacity: 0 }], { duration: dur(MOTION.fast * MOTION.exit), delay: dur(EXIT_MS - MOTION.fast * MOTION.exit), fill: 'forwards' });
    const homeEl = home.current!;
    stopMotion(homeEl);
    [...homeEl.querySelectorAll(':scope > .corner, .home-inner > *')].forEach((unit, i) => { if (unit !== back) unit.animate([{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: dur(MOTION.fast), delay: dur(MOTION.fast * MOTION.exit + Math.min(i, 4) * MOTION.stagger), easing: PAGE_EASE, fill: 'backwards' }); });
    back?.animate([{ opacity: 0 }, { opacity: 1 }], { duration: dur(MOTION.fast), delay: dur(MOTION.fast * MOTION.exit), fill: 'backwards' });
    shrink.onfinish = () => {
      if (!closing.current) return;
      closing.current = false; setPage(null); setPlugin(null); setSettingsCat(null); react('02', 0);
      back?.classList.remove('is-holding');
      (back?.matches('button') ? back : back?.querySelector('button'))?.focus({ preventScroll: true });
    };
  };
  const goUp = () => { if (page === 'plugins' && plugin) setPlugin(null); else if (page === 'settings' && settingsCat) setSettingsCat(null); else if (page) closePage(); else onClose(); };
  const keys = (event: KeyboardEvent) => {
    if (event.key !== 'Escape') return;
    event.stopPropagation();
    if ((event.target as Element).matches('input,select')) (event.target as HTMLElement).blur(); else goUp();
  };

  const ask = (text: string) => {
    if (talk) { talk.submit(text); return; }
    setTurns(value => [...value, { you: text, at: 'now' }]); setDemoTalkAt(Date.now());
    const reply = pick(TAKES.reply);
    setSaid({ text: 'Thinking…', caption: 'Thinking', busy: true }); react('30', 1300, reply);
    later(1300, () => {
      setSaid({ text: ANSWER, caption: 'Jarvis · just now', busy: false }); react(reply, 2400);
      setTurns(value => value.map((t, i) => i === value.length - 1 ? { ...t, jarvis: ANSWER, jarvisAt: 'just now' } : t));
    });
  };
  const body = () => pageEl.current?.querySelector('.pg-body');
  const allTurns = talk ? toTurns(talk.rows, thoughtRows(talk.rows, talk.think.thoughts)) : turns, dayList = [...new Set(allTurns.map(t => t.day))];
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
    // An intentional scroll takes over from a pending history handoff, including
    // one paused after a failed read. It must not jump back when that read ends.
    if (conversationRestore.current) { conversationRestore.current = null; setRestoringConversation(false); }
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
  const newest = shownTurns.at(-1);
  useEffect(() => {
    const target = conversationRestore.current;
    if (!open || !restoringConversation || !target || page !== 'conversation' || !talk) return;
    if (target.firstSeq === null || (talk.rows[0]?.seq ?? Infinity) <= target.firstSeq || talk.floor) {
      const el = body(); if (el) el.scrollTop = target.scroll;
      conversationRestore.current = null; setRestoringConversation(false); return;
    }
    if (target.loading || target.paused) return;
    target.loading = true;
    // Re-read through the authoritative route; the handoff carries only a range,
    // not another copy of conversation rows. A failed read pauses without polling.
    void talk.older().then(changed => {
      if (conversationRestore.current !== target) return;
      target.loading = false; target.paused = !changed; setRestoreRevision(n => n + 1);
    }, () => {
      if (conversationRestore.current !== target) return;
      target.loading = false; target.paused = true; setRestoreRevision(n => n + 1);
    });
  }, [open, page, talk?.rows, talk?.floor, restoringConversation, restoreRevision]);
  useEffect(() => { if (page === 'conversation' && !restoringConversation) body()?.scrollTo({ top: body()!.scrollHeight, behavior: reduced.matches ? 'auto' : 'smooth' }); }, [newest?.at, newest?.you, newest?.jarvis]);

  // Agents, grouped the way you act on them. A row you moved goes to the top of its new group.
  // Settings › Agents picks which sessions show; one left waiting past the stale limit counts as earlier.
  const agents = (port ? [...claudeRows.map(fromClaude), ...codex.rows.map(fromCodex)] : DEMO_AGENTS).filter(s => hidden[s.id] !== s.you && settings[s.agent])
    .map(s => ({ ...s, ...moved[s.id] })).map(s => s.state === 'wait' && !s.request && s.at && tick - s.at > STALE_MS[settings.stale] ? { ...s, state: 'done' as const } : s);
  const group = (state: AgentState) => agents.filter(s => s.state === state).sort((a, b) => (moved[b.id]?.at ?? 0) - (moved[a.id]?.at ?? 0));
  const waiting = group('wait'), stopped = group('err'), working = [...group('work'), ...group('pack')], earlier = group('done');
  // Finished and not looked at yet: the companion keeps that list (ADR 0057); what is done and seen is quiet.
  const markOf = (s: Agent): MarkState => s.state === 'done' && !unread?.has(s.id) ? 'seen' : s.state;
  const finished = earlier.filter(s => unread?.has(s.id));
  // Every row goes up to the companion: the wing draws the live ones, the notices watch them all change.
  const shown: ShownAgent[] = agents.map(s => ({ ...s, mark: markOf(s),
    line: s.state === 'done' ? unread?.has(s.id) ? 'Finished · not opened yet' : s.last : s.state === 'err' ? `Stopped · ${s.error}` : s.last || (s.state === 'wait' ? 'Needs you' : 'Working') }));
  const shownKey = JSON.stringify(shown);
  useEffect(() => onAgents?.(shown), [shownKey]);
  // The marks beside the notch were clicked: the companion opened the panel, and it lands on Agents.
  useEffect(() => {
    if (!agentsFocus || !open) return;
    if (!page) openPage('agents'); else if (page !== 'agents') setPage('agents');
  }, [agentsFocus]);
  useEffect(() => {
    if (!settingFocus || !open) return;
    if (!page) openPage('settings'); else if (page !== 'settings') setPage('settings');
    setSettingsCat(null);
  }, [settingFocus]);
  const move = (s: Agent, change: Partial<Agent>) => {
    const el = pageEl.current?.querySelector<HTMLElement>(`[data-id="${s.id}"]`);
    if (el) flip.current = { id: s.id, top: el.getBoundingClientRect().top };
    setMoved(value => ({ ...value, [s.id]: { ...change, at: Date.now() } }));
  };
  useLayoutEffect(() => {
    const f = flip.current, el = f && pageEl.current?.querySelector<HTMLElement>(`[data-id="${f.id}"]`);
    flip.current = null;
    if (f && el) el.animate([{ transform: `translateY(${f.top - el.getBoundingClientRect().top}px)` }, { transform: 'none' }], { duration: dur(PAGE_MS), easing: PAGE_EASE });
  }, [moved]);
  const hide = (s: Agent) => {
    setHidden(value => ({ ...value, [s.id]: s.you }));
    notify(t(['Hidden from this list.', '已从列表隐藏。']), () => setHidden(({ [s.id]: _, ...rest }) => rest));
  };
  // Claude sessions have no jump yet; their cards leave the button out rather than offer one that cannot work.
  const agentRow = (s: Agent, actions?: ReactNode) => <AgentRow key={s.id} s={s} look={marks} mark={markOf(s)} open={unfolded === s.id} onToggle={() => setUnfolded(v => v === s.id ? null : s.id)}
    onOpen={!port || s.agent === 'codex' ? () => void openAgent(s) : undefined} onHide={() => hide(s)} actions={actions}/>;
  const openAgent = async (s: Agent) => {
    if (port && s.agent === 'codex' && await window.jarvis?.openCodex?.(s.id).catch(() => false)) notify(t(['Opening in Codex…', '正在用 Codex 打开…']));
    else notify(port ? t([`Can’t open ${s.where} from here yet.`, `还不能从这里打开 ${s.where}。`]) : t(['Demo session, nothing to open.', '演示会话，没有东西可打开。']));
  };

  const setPluginState = (id: string, change: Partial<DemoPlugin>) => setPlugins(value => ({ ...value, [id]: { ...value[id], ...change } }));
  // Opening a plugin pushes its page in from the right; going back slides the list in from the left.
  useLayoutEffect(() => {
    const prev = shownPlugin.current; shownPlugin.current = plugin;
    if (page !== 'plugins' || prev === plugin) return;
    const el = pageEl.current?.querySelector(plugin ? '.pl-det' : '.pl-cat');
    el?.animate([{ transform: `translateX(${plugin ? 36 : -36}px)`, opacity: 0 }, { transform: 'none', opacity: 1 }], { duration: dur(plugin ? PAGE_MS : EXIT_MS), easing: PAGE_EASE });
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
    else if (act === 'off') { if (await run('disable')) notify(t([`${plugins[id].name} is off.`, `${plugins[id].name} 已关掉。`])); }
    else if (act === 'approval') { if (await run('approval', { mode: value })) notify(t(['Saved.', '已保存。'])); }
    else if (act === 'connect') {
      // ponytail: one token box; a plugin with several credential fields needs one box per field.
      const field = snapshot?.plugins.find(p => p.id === id)?.credential_fields[0];
      if (field && !token.trim() && !plugins[id].saved) { notify(t(['Paste an access token first.', '先粘贴一个访问令牌。'])); return; }
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
    else if (act === 'reopen') notify(t(['Opened the sign-in page again.', '又打开了一次登录页。']));
    else if (act === 'approval') { setPluginState(id, { approval: value }); notify(t(['Saved.', '已保存。'])); }
    else if (act === 'cancel') { clearTimeout(pluginTimer.current); setPluginState(id, { state: p.was }); react('02', 0); }
    else if (act === 'off') { setPluginState(id, { state: p.kind === 'oauth' ? 'signin' : p.kind === 'token' ? 'token' : 'off', resumed: undefined }); notify(t([`${p.name} is off.`, `${p.name} 已关掉。`])); }
    else if (act === 'connect') {
      if (p.state === 'token' && !token.trim()) { notify(t(['Paste an access token first.', '先粘贴一个访问令牌。'])); return; }
      setToken(''); setPluginState(id, { was: p.state, state: 'connecting' }); react('36', 60_000);
      pluginTimer.current = setTimeout(() => {
        setPluginState(id, { state: 'on', ask: undefined, resumed: p.ask && `Picking up: “${p.ask}”` });
        if (p.ask) { setSaid({ text: `Signed in to ${p.name}. Looking for last week’s notes now.`, caption: 'Jarvis · just now', busy: false }); setDemoTalkAt(Date.now()); }
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
      notify(RESET_ANSWERS[answer.code] ? t(RESET_ANSWERS[answer.code]) : t([`Codex answered ${answer.code}`, `Codex 回复：${answer.code}`]));
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

  // ---------- the home's blocks ----------
  const zh = lang === 'zh', d = new Date(tick);
  const timeOf = (ms: number) => new Date(ms).toLocaleTimeString(zh ? 'zh-CN' : 'en-US', zh ? { hour: '2-digit', minute: '2-digit', hour12: false } : { hour: 'numeric', minute: '2-digit' });
  const inAbout = (ms: number) => { const m = Math.max(0, Math.round((ms - tick) / 60_000)), h = Math.floor(m / 60);
    return m < 1 ? t(['now', '现在']) : zh ? `${h ? `${h} 小时 ` : ''}${m % 60} 分后` : `in ${h ? `${h} h ` : ''}${m % 60} m`; };
  const sameDay = (ms: number) => new Date(ms).toDateString() === d.toDateString();
  const localDate = d.toLocaleDateString('en-CA');

  // Today: the weather, what is left on the calendar, and the to-dos, all from /inherent/today.
  const todayRoute = useRoute<Today>(port, '/inherent/today', open, 5 * 60_000);
  const demoDay = useMemo(() => demoToday(), [open]);
  const today = port ? todayRoute.data : demoDay;
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const checkTodo = async (id: string) => {
    const done = !checked[id];
    setChecked(c => ({ ...c, [id]: done }));
    if (done) react('33', 1500);
    if (!port) return;
    try { await postRoute(port, '/inherent/today/todo', { id, done }); }
    catch { setChecked(c => ({ ...c, [id]: !done })); notify(t(['Couldn’t reach To Do.', '连不上 To Do。'])); }
  };
  const remainingEvents = (today?.events ?? []).filter(e => { const start = Date.parse(e.start), end = e.end ? Date.parse(e.end) : start + 30 * 60_000; return sameDay(start) && end > tick; }).sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
  const events = remainingEvents.slice(0, 2), nextEvent = remainingEvents.find(e => !e.all_day) ?? remainingEvents[0];
  const due = (x: { due?: string }) => x.due ? Date.parse(x.due) : Infinity;
  const todos = [...today?.todos ?? []].sort((a, b) => due(a) - due(b)).slice(0, 3);
  const dueLabel = (ms: number) => ms < tick ? t(['overdue', '已过期']) : sameDay(ms) ? zh ? `今天 ${timeOf(ms)}` : `due ${timeOf(ms)}` : new Date(ms).toLocaleDateString(zh ? 'zh-CN' : 'en-US', { month: 'short', day: 'numeric' });
  const wx = today?.weather, forecast = settings.forecast && d.getHours() < 11 && wx?.hours?.length ? wx.hours.slice(0, 4) : null;

  // The pop-ups, each keyed by what it shows: closing one hides that; something newer brings the block back.
  const briefRoute = useRoute<Brief>(port, '/inherent/brief', open, 10 * 60_000);
  const mailRoute = useRoute<{ unread: Mail[] }>(port, '/inherent/mail', open, 5 * 60_000);
  const noticeRoute = useRoute<{ notices: Notice[] }>(port, '/inherent/notices', open, 60_000);
  // A first boot fetches the speech models (~240 MB) before she can hear or speak; the corner shows how far, polled until they are in.
  const [voiceIn, setVoiceIn] = useState(false);
  const models = useRoute<{ voice_models?: { state: 'ready' | 'downloading' | 'failed'; done: number; total: number } }>(port, '/inherent/setup', open && !voiceIn, 3000).data?.voice_models;
  useEffect(() => { if (models?.state === 'ready') setVoiceIn(true); }, [models?.state]);
  const demoPops = useMemo(() => ({ brief: demoBrief(), mail: demoMail(), notices: demoNotices() }), []);
  // Letters archived from the home's junk line leave the list at once; the daemon stops serving them from the next poll.
  const [archived, setArchived] = useState<string[]>([]);
  const brief = port ? briefRoute.data : demoPops.brief, mail = (port ? mailRoute.data?.unread ?? [] : demoPops.mail).filter(m => !archived.includes(m.id));
  const notices = port ? noticeRoute.data?.notices ?? [] : demoPops.notices;
  // Letters Jev rated come first, most important first (ADR 0141); then the unrated ones that need a reply, the unmarked, the FYI ones;
  // junk always last; newest first inside each (the sort is stable).
  const rank = (m: Mail) => m.junk ? 3 : m.reply === 'yes' ? 0 : m.reply === 'fyi' ? 2 : 1;
  const mailRanked = [...mail].sort((a, b) => Number(!!a.junk) - Number(!!b.junk) || (b.importance ?? -1) - (a.importance ?? -1) || rank(a) - rank(b));
  const mailYes = mail.filter(m => m.reply === 'yes'), mailJunk = mail.filter(m => m.junk), marked = mail.some(m => m.reply != null || m.junk);
  const archiveJunk = async () => {
    const ids = mailJunk.map(m => m.id);
    if (!ids.length) return;
    setArchived(a => [...a, ...ids]);
    const back = () => setArchived(a => a.filter(id => !ids.includes(id)));
    try { if (port) await postRoute(port, '/inherent/mail/archive', { ids }); }
    catch { back(); notify(t(['Couldn’t archive. Try again.', '归档没成功，请再试一次。'])); return; }
    notify(t([`Archived ${ids.length} · still in All Mail`, `已归档 ${ids.length} 封 · 仍在“所有邮件”里`]), async () => {
      try { if (port) await postRoute(port, '/inherent/mail/unarchive', { ids }); back(); mailRoute.reload(); }
      catch { notify(t(['Couldn’t put them back.', '放不回收件箱。'])); }
    });
  };
  // For you: what Jarvis itself wants from you. Agents keep their own row.
  type ForYou = { id: string; text: string; ask?: boolean; act?: [L, () => void] };
  const signIn = (id: string): [L, () => void] => [['Sign in', '登录'], () => { openPage('plugins', home.current?.querySelector<HTMLElement>('[data-block="foryou"]')); setPlugin(id); }];
  const request = snapshot?.request;
  const forYou: ForYou[] = [
    ...(live ? request?.purpose && (request.state === 'offered' || request.state === 'error') ? [{ id: request.id, ask: true, text: `${plugins[request.plugin_id]?.name ?? request.plugin_id} · ${request.purpose}`, act: signIn(request.plugin_id) }] : []
      : pluginIds.filter(id => plugins[id].ask && plugins[id].state === 'signin').map(id => ({ id, ask: true, text: `${plugins[id].name} · ${plugins[id].ask}`, act: signIn(id) }))),
    ...notices.map(n => ({ id: n.id, text: n.text })),
  ];
  // The conversation sits on top while you talk and for TALK_STAYS after your last turn (Settings › Home).
  const lastYou = talk ? [...talk.rows].reverse().find(row => row.source === 'allen') : null;
  const talkAt = talk ? lastYou ? Date.parse(lastYou.ts) : 0 : demoTalkAt;
  const talking = talk ? talk.busy || !!talk.tail || talk.think.secs > 0 : said.busy;
  const youSaid = talk ? lastYou ? plain(lastYou.text) : '' : demoTalkAt ? turns.at(-1)?.you ?? '' : '';
  const answer = !talk ? said.text : talk.tail ? plain(talk.tail) : talk.think.secs ? t([`Thinking deeply · ${talk.think.secs} s`, `深想中 · ${talk.think.secs} 秒`]) : talk.busy ? t(['Thinking…', '在想…'])
    : lastAnswer && (!lastYou || lastAnswer.seq > lastYou.seq) ? plain(lastAnswer.text) : '';
  const popKey: Partial<Record<BlockId, string>> = {
    talk: settings.talk === 'always' ? (talk ? talk.rows.length > 0 : true) ? `t${talkAt}` : undefined
      : settings.talk === 'after' && (talking || (talkAt > 0 && tick - talkAt < TALK_STAYS)) ? `t${talkAt}` : undefined,
    foryou: settings.foryou && forYou.length ? forYou.map(f => f.id).join('|') : undefined,
    brief: settings.brief && brief?.date === localDate && briefRead !== brief.date ? brief.date : undefined,
    // With Jev's marks the pop-up is for the newest letter that needs a reply or is junk to clear; without any mark it is the newest letter.
    mail: !settings.mail ? undefined : marked ? [mailYes[0], mailJunk[0]].filter(m => m).sort((a, b) => Date.parse(b.received) - Date.parse(a.received))[0]?.id : mail[0]?.id,
  };
  const shows = (id: BlockId) => isPop(id) ? popKey[id] !== undefined && dismissed[id] !== popKey[id] : !settings.hidden.includes(id);
  if (open && shows('brief') && popKey.brief) briefShown.current = popKey.brief;
  const blocks = settings.order.filter(shows);
  const dismiss = (id: BlockId) => {
    const key = popKey[id], el = home.current?.querySelector<HTMLElement>(`[data-block="${id}"]`);
    if (key === undefined) return;
    const apply = () => {
      const was = briefRead;
      if (id === 'brief') setBriefRead(key);
      setDismissed(v => ({ ...v, [id]: key }));
      notify(t(['Closed · comes back with the next one', '关掉了 · 有新的会再出现']), () => { setDismissed(({ [id]: _, ...rest }) => rest); if (id === 'brief') setBriefRead(was); });
    };
    if (!el || reduced.matches) { apply(); return; }
    el.style.height = `${el.offsetHeight}px`; void el.offsetHeight; el.classList.add('is-leaving');
    later(EXIT_MS, apply);
  };

  // Holding a block arranges the home. The click that ends the hold is swallowed, wherever it lands.
  const hold = useRef<{ el: HTMLElement; x: number; y: number; timer: ReturnType<typeof setTimeout> } | null>(null);
  const cancelHold = () => { const h = hold.current; if (!h) return; clearTimeout(h.timer); h.el.classList.remove('is-holding'); hold.current = null; };
  const holdDown = (e: PointerEvent<HTMLDivElement>) => {
    const el = (e.target as Element).closest<HTMLElement>('[data-block]');
    if (e.button !== 0 || page || !el || (e.target as Element).closest('.mx')) return;
    cancelHold();
    el.classList.add('is-holding');
    hold.current = { el, x: e.clientX, y: e.clientY, timer: setTimeout(() => {
      hold.current = null;
      const stop = (event: Event) => { event.stopPropagation(); event.preventDefault(); off(); };
      const off = () => { window.removeEventListener('click', stop, true); window.removeEventListener('pointerdown', off, true); };
      window.addEventListener('click', stop, true); window.addEventListener('pointerdown', off, true); later(1500, off);
      openPage('arrange', el);
    }, HOLD) };
  };
  const holdMove = (e: PointerEvent) => { const h = hold.current; if (h && Math.hypot(e.clientX - h.x, e.clientY - h.y) > 6) cancelHold(); };

  // The home follows its blocks, so the input sits right under the last one; a page needs room to read, so it keeps at least VIEW_MIN.
  // Either way the resting input keeps its space even on a short display.
  const inner = useRef<HTMLDivElement>(null), [viewH, setViewH] = useState(VIEW_MIN);
  useLayoutEffect(() => {
    const el = inner.current;
    if (!el || !view.current) return;
    const fit = () => {
      const viewport = view.current!, panel = viewport.parentElement!;
      // Layout offsets exclude the entrance transform, so opening cannot briefly oversize the panel.
      let top = 0;
      for (let node: HTMLElement | null = viewport; node; node = node.offsetParent as HTMLElement | null) top += node.offsetTop;
      const available = Math.max(0, window.innerHeight - top - parseFloat(getComputedStyle(panel).paddingBottom) - 12);
      const homeH = CORNER + HOME_GAP + el.offsetHeight;
      setViewH(Math.round(Math.min(available, page ? Math.min(VIEW_MAX, Math.max(VIEW_MIN, homeH)) + DOCK : Math.min(VIEW_MAX, homeH))));
    };
    fit();
    let frame = 0;
    const resize = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(fit); };
    const observer = new ResizeObserver(fit);
    observer.observe(el);
    window.addEventListener('resize', resize);
    return () => { observer.disconnect(); cancelAnimationFrame(frame); window.removeEventListener('resize', resize); };
  }, [open, page]);
  const mute = () => {
    const muted = !ctl.speechMuted;
    ctl.setSpeech(muted);
    notify(t(muted ? ['Jarvis is muted: no voice, no sounds', 'Jarvis 已静音：不说话，也没有提示音'] : ['Jarvis can talk again', 'Jarvis 可以说话了']));
  };
  const svc = (name: string, x: { status: string; error?: string | null } | undefined, ok: string): Account =>
    ({ name, ok: !x || x.status === 'ok', text: !x ? t(['Syncing…', '同步中…']) : x.status === 'ok' ? ok : x.error ?? t(['Not connected', '没连上']) });
  const signedIn = t(['Signed in', '已登录']), connected = t(['Connected', '已连接']);
  const accounts: Account[] = [svc('Claude', claude, signedIn), svc('Codex', codexUsage, signedIn), svc('OpenAI', openai, connected), svc('DeepSeek', deepseek, connected), svc('MiniMax', minimax, connected)];

  const back = (title: string, meta?: ReactNode) => <header className="pg-head">
    <button className="pg-back" aria-label={t(['Back', '返回'])} onClick={goUp}><CaretLeft size={14} weight="bold"/></button>
    <h3 key={title}>{title}</h3>{meta && <span className="meta">{meta}</span>}
  </header>;
  const pages: Record<Page, () => ReactNode> = {
    conversation: () => <>
      {back(t(TITLES.conversation), talk ? undefined : t(['today', '今天']))}
      <div className="pg-body" onWheel={talk ? onWheel : undefined}>
      {talk && <div className={`pg-earlier${pull ? ' is-pulling' : ''}`} style={{ '--pull': pull } as CSSProperties}>
        {more ? <><CaretUp size={10} weight="bold"/>{t(['Scroll up for', '往上滚看'])} {before ? dayLabel(lang, before) : t(['earlier', '更早的'])}</> : t(['Start of the conversation', '对话从这里开始'])}</div>}
      {shownTurns.map((turn, i) => <div className="pg-sec tr" key={i} data-day={turn.day}>
        {turn.you && <div className="tr-you"><span className="who">{t(['You', '你'])} · {turn.at}</span><p>{turn.you}</p></div>}
        {(turn.jarvis || turn.mail) && <div className="tr-jarvis"><span className="who"><span className="dot"/><span>Jarvis · {turn.jarvisAt}{turn.thought ? <em className="is-deep">{t([` · thought for ${turn.thought.toFixed(1)} s`, ` · 想了 ${turn.thought.toFixed(1)} 秒`])}</em> : null}</span></span>
          {turn.mail?.map((text, k) => <MailCard key={k} text={text} lang={lang}/>)}
          {turn.jarvis && <Markdown text={turn.jarvis}/>}
          {turn.work && <Fold label={turn.work[0]}><pre>{turn.work[1]}</pre></Fold>}</div>}
      </div>)}
      {talk?.card && talk.decide && <ActionCard key={talk.card.id} card={talk.card} lang={lang} onDecide={talk.decide} draft={actionDraft?.id === talk.card.id ? actionDraft.value : undefined} onDraft={value => setActionDraft({ id: talk.card!.id, value })}/>}
      {talk?.question && talk.answer && <QuestionCard key={talk.question.id} question={talk.question} lang={lang} onAnswer={talk.answer} draft={questionDraft?.id === talk.question.id ? questionDraft.value : undefined} onDraft={value => setQuestionDraft({ id: talk.question!.id, value })}/>}
      {talk && talk.think.secs > 0 && <div className="pg-sec tr-think">{t([`Thinking deeply · ${talk.think.secs} s`, `深想中 · ${talk.think.secs} 秒`])}</div>}
      <Ask className="pg-input" text={talkDraft} setText={setTalkDraft} onAsk={ask} think={talk?.think}/></div>
    </>,
    now: () => <>
      {back(t(TITLES.now), now && `${now.current ? t(['as of', '截至']) : t(['at', '于'])} ${now.at}`)}
      <div className="pg-body">{workView === null ? <p className="pg-sec muted">{t(['Syncing…', '同步中…'])}</p> : !state ? <p className="pg-sec muted">{t(['No status yet. Refresh and Jarvis reads the latest activity.', '还没有状态。刷新一下，Jarvis 会读最新的动静。'])}</p> : <>
        {state.now && now && <div className="pg-sec now-card"><span className="who"><span className="dot"/>{t(BASIS[state.now.basis])} · {now.current ? t(['as of', '截至']) : t(['at', '于'])} {now.at}</span><p>{now.claim}</p></div>}
        <div className="pg-sec"><h4>{t(['Today', '今天'])}{state.observed_until ? t([`, until ${hm(Date.parse(state.observed_until))}`, `，到 ${hm(Date.parse(state.observed_until))}`]) : ''}</h4>
          {state.activities.length ? <ol className="tl">{state.activities.map((c, i) => <li key={i} className={`is-${c.basis}`}>{c.text}{c.progress && <small>{c.progress}</small>}</li>)}</ol> : <p className="muted">{t(['Not enough data yet.', '数据还不够。'])}</p>}
          <div className="legend"><span><i className="o"/>{t(['seen', '看到的'])}</span><span><i className="s"/>{t(['you said', '你说的'])}</span><span><i className="g"/>{t(['a guess', '猜的'])}</span></div>
        </div>
        {state.links.length > 0 && <div className="pg-sec"><h4>{t(['Linked', '相关'])}</h4>{state.links.map((l, i) => <div className="link" key={i}><span className="chip">{l.kind === 'todo' ? t(['To-do', '待办']) : t(['Discussion', '讨论'])}</span>{l.title || l.note}{l.title && <small>{l.note}</small>}</div>)}</div>}
        {state.uncertainties.length > 0 && <div className="pg-sec"><h4>{t(['Unknown', '不确定'])}</h4>{state.uncertainties.map((u, i) => <p className="muted" key={i}>{u}</p>)}</div>}
      </>}
      <footer className="pg-foot"><span className={fresh.stale ? 'is-warm' : ''}>{work.notice ?? fresh.text}</span>
        <button className="icon-btn" aria-label={t(['Refresh', '刷新'])} disabled={work.refreshing} onClick={work.refresh}><ArrowsClockwise size={13} className={work.refreshing ? 'is-spinning' : ''}/></button></footer></div>
    </>,
    agents: () => <>
      {back(t(TITLES.agents), t([`${waiting.length + working.length} live`, `${waiting.length + working.length} 个在跑`]))}
      <div className="pg-body">{!agents.length && <p className="pg-sec muted">{t(['Sessions show up once you start one.', '开一个会话，它就会出现在这里。'])}</p>}
      {waiting.length > 0 && <div className="pg-sec"><h4 className="is-warm"><span className="dot"/>{t(['Needs you', '等你'])}</h4>{waiting.map(s => agentRow(s,
        !port ? <><button className="btn btn-glow" onClick={() => { move(s, { state: 'work', last: 'Approved · running it now…' }); react('33', 1900); }}>{t(['Approve', '批准'])}</button>
          <button className="btn btn-ghost" onClick={() => move(s, { state: 'done', last: 'You denied it. It stopped there.', age: 'now' })}>{t(['Deny', '拒绝'])}</button></>
          : s.request && <button className="btn btn-glow" onClick={() => onAnswer?.(s.id)}>{t(['Answer', '回答'])}</button>))}</div>}
      {stopped.length > 0 && <div className="pg-sec"><h4 className="is-alert">{t(['Stopped', '停了'])} · {stopped.length}</h4>{stopped.map(s => agentRow(s))}</div>}
      {working.length > 0 && <div className="pg-sec"><h4>{t(['Working', '在做'])} · {working.length}</h4>{working.map(s => agentRow(s))}</div>}
      {earlier.length > 0 && <div className="pg-sec"><h4>{port ? t(['Last 24 hours', '过去 24 小时']) : t(['Earlier today', '今天早些时候'])} · {earlier.length}</h4>{earlier.map(s => agentRow(s))}</div>}
      {agents.length > 0 && <p className="pg-sec muted">{t(['Click a session to see what it’s doing.', '点一个会话，看它在做什么。'])}</p>}
      {port && window.jarvis?.openAgents && <div className="pg-sec"><button className="btn btn-ghost" onClick={() => window.jarvis?.openAgents?.()}>{t(['Open the Agents window', '打开 Agents 窗口'])}</button></div>}</div>
    </>,
    usage: () => <>
      {back(t(TITLES.usage), <button className="us-sync" aria-label={t(['Refresh', '刷新'])} disabled={quota.refreshing} onClick={() => void quota.refresh()}>
        {!quota.refreshing && synced ? t([`synced ${hm(synced)}`, `${hm(synced)} 同步`]) : t(['syncing…', '同步中…'])}<ArrowsClockwise size={11} className={quota.refreshing ? 'is-spinning' : ''}/></button>)}
      <div className="pg-body"><div className="pg-sec"><h4>{t(['Balances', '余额'])}</h4><div className="bal">
        <div className="bal-card"><Account id="deepseek">DeepSeek</Account><b>{deepseek?.status === 'ok' ? usd(deepseek.data.balance) : '—'}</b></div>
        <Balance id="openai" name="OpenAI" left={openai?.status === 'ok' ? openai.data.balance_usd : undefined} since={openai?.data.balance_recorded_at} live={!!port} onSaved={balanceSaved}/>
        <div className="bal-card"><Account id="minimax">MiniMax</Account><b>{minimax?.status === 'ok' ? usd(minimax.data.balance) : '—'}</b></div>
      </div></div>
      <div className="pg-sec"><div className="us-plan"><Account id="claude">Claude Max <em>{claude?.data.plan}</em></Account>{claude?.status === 'ok' && claude.data.reset_credits !== undefined && <span className="meta">{resetsLeft(lang, claude.data.reset_credits, claude.data.reset_ends_at)}</span>}</div>
        {claude?.status === 'ok' ? <div className="bigrings">{(claude.data.windows ?? []).map(w => <Ring key={w.key} w={w} name={w.label} sub={fmtReset(w.resets_at)}/>)}</div> : <p className="muted">{claude?.error ?? t(['Not signed in to Claude Code', '没登录 Claude Code'])}</p>}</div>
      <div className="pg-sec"><div className="us-plan"><Account id="codex">Codex <em>{codexUsage?.data.plan}</em></Account>{codexUsage?.status === 'ok' && <span className="meta">{resetsLeft(lang, codexResets)}</span>}
          {port && codexResets > 0 && !reset && <button className="us-use" onClick={askReset}>{t(['Use reset', '用一次重置'])}</button>}</div>
        {reset && <div className="us-confirm" role="alertdialog" aria-label={t(['Use this reset?', '用掉这次重置？'])}>
          <b>{reset.state === 'using' ? t(['Using a reset…', '正在重置…']) : t(['Use this reset?', '用掉这次重置？'])}</b>
          {reset.state === 'error' ? <p className="is-alert">{reset.error}</p>
            : <p>{t(['Clears your Codex limits now.', '马上清空 Codex 的额度。'])} {codexResets === 1 ? t(['It is your only reset.', '这是你唯一一次重置。']) : t([`Uses 1 of your ${codexResets}.`, `用掉 ${codexResets} 次中的 1 次。`])}</p>}
          <div><button className="btn btn-text" disabled={reset.state === 'using'} onClick={() => setReset(null)}>{t(['No, go back', '不用了'])}</button>
            <button className="btn btn-glow" disabled={!reset.armed || reset.state === 'using'} onClick={() => void spendReset()}>{reset.state === 'error' ? t(['Try again', '再试一次']) : t(['Yes, use reset', '确定重置'])}</button></div>
        </div>}
        {codexUsage?.status === 'ok' ? <div className="bigrings">{(codexUsage.data.windows ?? []).map(w => <Ring key={w.key} w={w} name={w.label} sub={fmtReset(w.resets_at)}/>)}</div> : <p className="muted">{codexUsage?.error ?? t(['Not signed in to Codex', '没登录 Codex'])}</p>}</div>
      <div className="pg-sec"><div className="us-plan"><Account id="openai">OpenAI <em>API</em></Account>{openai?.status === 'ok' && <span className="meta">{t(['this month', '本月'])} {usd(openai.data.month_usd)}</span>}</div>
        {openai?.status === 'ok' ? <Spend total={openai.data.today_usd ?? 0} models={openai.data.by_model ?? []}/> : <p className="muted">{openai?.error ?? t(['Needs an admin key', '缺管理密钥'])}</p>}</div></div>
    </>,
    plugins: () => {
      const p = plugin ? plugins[plugin] : null;
      const shown = pluginIds.filter(id => plugins[id].name.toLowerCase().includes(query.trim().toLowerCase()));
      return <>
        {back(p ? p.name : t(TITLES.plugins), !p && t([`${pluginsOn} connected`, `${pluginsOn} 个已连接`]))}
        <div className="pg-body">{p ? <div className="pl-det" key={plugin}>{live?.error && <p className="pg-sec is-warm">{live.error}</p>}<PluginDetail id={plugin!} p={p} token={token} onToken={setToken} onAct={pluginAct}/></div>
          : <div className="pl-cat">
            <label className="pg-sec search"><MagnifyingGlass size={13}/><input type="search" aria-label={t(['Search plugins', '搜索插件'])} placeholder={t(['Search plugins', '搜索插件'])} autoComplete="off" value={query} onChange={e => setQuery(e.target.value)} onPointerDown={focusWindow}/></label>
            {live?.error && <p className="pg-sec is-warm">{live.error}</p>}
            {live && !snapshot && !live.error && <p className="pg-sec muted">{t(['Loading plugins…', '正在加载插件…'])}</p>}
            <div className="pg-sec pl-list">{shown.map(id => { const [cls, text] = pluginStatus(plugins[id]);
              return <button key={id} className="pl-row" data-plugin={id} disabled={live?.busy} onClick={() => void openPlugin(id)}><span className={`pl-ic mk-${id}`}><Mark id={id} mark={plugins[id].mark}/></span><span className="pl-name">{plugins[id].name}<small className={cls}>{t(text)}</small></span><CaretRight size={12}/></button>; })}</div>
            {!shown.length && (!live || snapshot) && <p className="muted">{t(['No plugins match.', '没有匹配的插件。'])}</p>}
            <p className="pg-sec muted">{t(['Plugins let Jarvis read and act in your apps. It asks before it writes, unless you change that.', '插件让 Jarvis 读取并操作你的应用。写入前会先问你，除非你改了设置。'])}</p>
          </div>}</div>
      </>;
    },
    settings: () => <SettingsPage lang={lang} port={port} open={open} cat={settingsCat} onCat={setSettingsCat} ctl={ctl} accounts={accounts}
      keyDrafts={accountKeyDrafts} onKeyDraft={(provider, value) => setAccountKeyDrafts(drafts => ({ ...drafts, [provider]: value }))}
      hiddenAgents={Object.keys(hidden).length} onUnhideAgents={() => { setHidden({}); notify(t(['Hidden sessions are back.', '隐藏的会话回来了。'])); }}
      onArrange={() => { setSettingsCat(null); setPage('arrange'); react('14', 1200); }} onPlugins={() => { setSettingsCat(null); setPage('plugins'); }}
      onResetHome={() => { updateSettings(HOME_DEFAULTS); setDismissed({}); setBriefRead(''); react('10', 1400); notify(t(['The home is back to how it started.', '首页恢复默认了。'])); }}
      notify={text => notify(text)} head={(title, meta) => back(title, meta)}/>,
    arrange: () => <>{back(t(['Arrange the home', '编辑首页']))}<ArrangeHome lang={lang}/></>,
    brief: () => <>
      {back(t(['Morning brief', '早报']), brief?.date)}
      <div className="pg-body">{brief ? <BriefPage brief={brief}/> : <p className="pg-sec muted">{t(['No brief today yet.', '今天的早报还没写好。'])}</p>}</div>
    </>,
    projects: () => <>
      {back(t(TITLES.projects), t(['last 7 days', '最近 7 天']))}
      <div className="pg-body">{projects.missing ? <p className="pg-sec muted">{t(['No projects set up. List them under projects in ~/.jarvis/settings.yaml.', '还没设置项目。在 ~/.jarvis/settings.yaml 的 projects 下列出来。'])}</p> : !projectsView ? <p className="pg-sec muted">{t(['Syncing…', '同步中…'])}</p> : <>
        {activeProjects.map(p => <article className="pg-sec pj" key={p.id}>
          <div className="pj-top"><b>{p.name}</b><span>{duration(p.seconds)}{p.commits.count ? t([` · ${p.commits.count} commits`, ` · ${p.commits.count} 次提交`]) : ''}</span></div>
          <Cols days={p.days} dates={projectsView.days}/>
          <small>{t(['Today', '今天'])} {p.today_seconds ? duration(p.today_seconds) : t(['not touched', '没碰'])}{p.recent[0] ? ` · ${p.recent[0].app}, ${p.recent[0].label}` : ''}</small>
        </article>)}
        {projectsView.projects.some(p => !activeProjects.includes(p)) && <p className="pg-sec muted">{t(['Not touched this week:', '这周没碰：'])} {projectsView.projects.filter(p => !activeProjects.includes(p)).map(p => p.name).join(', ')}</p>}
        <footer className="pg-foot"><span>{projects.notice ?? `${t(['Other', '其他'])} ${duration(projectsView.other.seconds)}${projectsView.unsorted.seconds ? ` · ${duration(projectsView.unsorted.seconds)} ${t(['not sorted', '未归类'])}` : ''}`}</span>
          {projectsView.unsorted.seconds > 0 && <button className="btn btn-ghost" disabled={projects.refreshing} onClick={() => void projects.refresh()}>{projects.refreshing ? t(['Sorting…', '归类中…']) : t(['Sort now', '现在归类'])}</button>}</footer>
      </>}</div>
    </>,
  };

  return <div className="ad" data-page={page ?? undefined} data-deep={page === 'conversation' && talk?.think.on ? '' : undefined} onKeyDown={keys} onScrollCapture={() => publishView.current?.(snapshotView())}>
    <div className="view" ref={view} style={{ height: viewH }}>
      <div className="overview" ref={home} inert={!!page}>
        <div className="corner">
          <span className="next-event" title={nextEvent?.title}>{talk?.offline ? <b className="is-warm">{t(['Offline · reconnecting', '离线 · 重连中'])}</b>
            : models?.state === 'failed' ? <b className="is-warm" title={t(['Her voice didn’t download. Restart Jarvis in Settings › Advanced to try again.', '她的声音没下载下来。到 设置 › 高级 里重启 Jarvis 再试。'])}>{t(['Voice failed', '声音下载失败'])}</b>
            : models?.state === 'downloading' ? <b className="is-warm" title={t(['Downloading her voice (about 240 MB). Type to her meanwhile.', '正在下载她的声音（约 240 MB），这期间可以先打字。'])}>
              {t([`Voice · ${Math.floor(models.done * 100 / models.total)}%`, `声音准备中 ${Math.floor(models.done * 100 / models.total)}%`])}</b>
            : nextEvent ? <><b>{nextEvent.all_day ? t(['Today', '今天']) : timeOf(Date.parse(nextEvent.start))}</b><span>{nextEvent.title}</span></>
            : <span>{today ? t(['No more events today', '今天没有后续日程']) : todayRoute.missing ? t(['Calendar not connected', '日历未连接']) : t(['Syncing calendar…', '正在同步日历…'])}</span>}</span>
          <span className="corner-b">
            <button className="cb" data-row="conversation" aria-label={t(['Conversation', '对话'])} title={t(['Conversation', '对话'])} onClick={e => openPage('conversation', e.currentTarget)}><ChatCircle size={15}/></button>
            <button className={`cb ${ctl.speechMuted ? 'is-muted' : ''}`} aria-pressed={ctl.speechMuted} onClick={mute}
              aria-label={t(ctl.speechMuted ? ['Unmute Jarvis', '取消静音'] : ['Mute Jarvis', '让 Jarvis 静音'])} title={t(ctl.speechMuted ? ['Unmute Jarvis', '取消静音'] : ['Mute Jarvis: voice and sounds', '让 Jarvis 静音：声音和提示音'])}>
              {ctl.speechMuted ? <SpeakerSlash size={15}/> : <SpeakerHigh size={15}/>}</button>
            <button className="cb" data-row="settings" aria-label={t(['Settings', '设置'])} title={t(['Settings', '设置'])} onClick={e => openPage('settings', e.currentTarget)}><GearSix size={15}/></button>
          </span>
        </div>
        <div className="home-list" onPointerDown={holdDown} onPointerMove={holdMove} onPointerUp={cancelHold} onPointerLeave={cancelHold} onPointerCancel={cancelHold}>
          <div className="home-inner" ref={inner}>{blocks.map(id => <HomeBlock key={id} id={id} pop={isPop(id)} lang={lang} onClose={() => dismiss(id)}>{{
            talk: () => <button className="talk-open" aria-label={t(['Open Conversation', '打开对话'])} onClick={e => openPage('conversation', e.currentTarget.parentElement)}>
              {youSaid && <span className="you"><b>{t(['You', '你'])}</b>{youSaid}</span>}
              <span className={`say ${talking ? 'is-busy' : ''}`} key={answer}>{answer || '…'}</span>
            </button>,
            foryou: () => <>
              <span className="head"><span className="label is-warm">{t(['For you', '找你的事'])}</span><span className="meta">{forYou.length}</span></span>
              {forYou.slice(0, 3).map(f => <div className="fy" key={f.id}><span className={`nd ${f.ask ? '' : 'is-note'}`}/><span className="fy-t" title={f.text}>{f.text}</span>
                {f.act && <button className="fy-b" onClick={f.act[1]}>{t(f.act[0])}</button>}</div>)}
            </>,
            brief: () => brief && <button className="fill" data-row="brief" aria-label={t(['Read the morning brief', '看早报'])}
              onClick={e => { setBriefRead(brief.date); openPage('brief', e.currentTarget.parentElement); }}>
              <span className="head"><span className="label">{t(['Morning brief', '早报'])}</span>
                <span className="meta">{brief.items ? t([`${brief.items} items`, `${brief.items} 条`]) : t(['Read', '看全文'])}<CaretRight size={10}/></span></span>
              <span className="text">{brief.summary}</span>
            </button>,
            today: () => <>
              <span className="head"><span className="label">{t(['Today', '今天'])}</span>{wx && <span className="meta wx">{WX[wx.hours?.[0]?.kind ?? 'cloud']}{Math.round(wx.now_c)}°{wx.summary ? ` · ${wx.summary}` : ''}</span>}</span>
              {forecast && <span className="fc">{forecast.map(h => <span key={h.at} className={h.kind === 'rain' || h.kind === 'storm' ? 'is-rain' : ''}><em>{timeOf(Date.parse(h.at)).replace(':00', '')}</em>{WX[h.kind]}<b>{Math.round(h.temp_c)}°</b></span>)}</span>}
              <span className="td-list">
                {events.map((e, i) => <span className="ev" key={e.id}><b>{e.all_day ? t(['All day', '全天']) : timeOf(Date.parse(e.start))}</b><span>{e.title}</span>{i === 0 && !e.all_day && Date.parse(e.start) > tick && <em>{inAbout(Date.parse(e.start))}</em>}</span>)}
                {todos.map(x => <button className="td" key={x.id} aria-pressed={!!checked[x.id]} onClick={() => void checkTodo(x.id)}><i>{checked[x.id] && <Check size={10} weight="bold"/>}</i><span>{x.title}</span>
                  {x.due && <em className={due(x) < tick || sameDay(due(x)) ? 'is-warm' : ''}>{dueLabel(due(x))}</em>}</button>)}
                {port && !today && <span className="muted">{todayRoute.missing ? t(['Calendar and to-dos aren’t connected yet.', '日程和待办还没接上。']) : t(['Syncing…', '同步中…'])}</span>}
                {today && !events.length && !todos.length && <span className="muted">{t(['Nothing left today.', '今天没有别的事了。'])}</span>}
              </span>
            </>,
            mail: () => <>
              <span className="head"><span className="label">{t(['Mail', '邮件'])}</span><span className="meta">{t([`${mail.length} unread`, `${mail.length} 封未读`])}{mailYes.length > 0 && t([` · ${mailYes.length} need a reply`, ` · ${mailYes.length} 封要回`])}</span></span>
              {mailRanked.slice(0, 2).map(m => <button className="ml" key={m.id} title={t(['Open in Gmail', '在 Gmail 里打开'])} onClick={() => void window.jarvis?.openMail?.(m.id)}><EnvelopeSimple size={13}/><b>{m.from}</b><span>{m.subject}</span>{m.reply === 'yes' && <em>{t(['Reply', '要回'])}</em>}</button>)}
              {mailJunk.length > 0 && <span className="mj"><span>{t([`${mailJunk.length} look like junk`, `${mailJunk.length} 封像垃圾邮件`])}</span><button onClick={() => void archiveJunk()}>{t(['Archive', '一键归档'])}</button></span>}
            </>,
            agents: () => <button className="fill" data-row="agents" aria-label={t(['Open Agents', '打开 Agents'])} onClick={e => openPage('agents', e.currentTarget.parentElement)}>
              <span className="head"><span className="label">Agents</span><span className="head-r">
                <span className="orbs">{[...waiting, ...stopped, ...finished, ...working].slice(0, 5).map(s => <AgentMark key={s.id} id={s.id} look={marks} state={markOf(s)} size={12}/>)}</span>
                {(waiting.length > 0 || working.length > 0) && <span className={`pill ${waiting.length ? 'is-waiting' : ''}`}>{waiting.length ? zh ? `${waiting.length} 个等你` : `${waiting.length} ${waiting.length > 1 ? 'need' : 'needs'} you` : zh ? `${working.length} 个在做` : `${working.length} working`}</span>}
              </span></span>
              <span className="text one">{lead ? <><span className={`tagc ${lead.agent}`}>{AGENT_NAME[lead.agent]}</span>{lead.title}{lead.state === 'wait' && lead.last ? ` · ${lead.last.replace(/^Wants/, 'wants')}` : ''}</> : t(['Sessions show up once you start one.', '开一个会话，它就会出现在这里。'])}</span>
            </button>,
            now: () => <button className="fill" data-row="now" aria-label={t(['Open Right now', '打开“现在”'])} onClick={e => openPage('now', e.currentTarget.parentElement)}>
              <span className="head"><span className="label">{t(['Now', '现在'])}</span><span className={`meta ${fresh.stale ? 'is-warm' : ''}`} title={fresh.text}>{now ? `${now.current ? t(['as of', '截至']) : t(['at', '于'])} ${now.at}` : ''}</span></span>
              <span className="text">{now ? now.claim : workView === null ? t(['Syncing…', '同步中…']) : t(['No recent activity observed', '最近没看到动静'])}</span>
            </button>,
            usage: () => <button className="fill" data-row="usage" aria-label={t(['Open Usage', '打开用量'])} onClick={e => openPage('usage', e.currentTarget.parentElement)}>
              <span className="head"><span className="label">{t(['Usage', '用量'])}</span><span className="meta">{t(['OpenAI today', 'OpenAI 今天'])} <b>{usd(openai?.data.today_usd)}</b></span></span>
              <span className="rings">
                <UsageGroup name="Claude Max" plan={claude?.data.plan} ok={claude?.status === 'ok'} windows={(claude?.data.windows ?? []).slice(0, 3)} synced={!!quota.usage}/>
                <span className="split"/>
                <UsageGroup name="Codex" plan={codexUsage?.data.plan?.split(' ')[0]} ok={codexUsage?.status === 'ok'} windows={(codexUsage?.data.windows ?? []).slice(0, 1)} synced={!!quota.usage}/>
              </span>
            </button>,
            tiles: () => <>
              <button className="row tile" data-row="plugins" aria-label={t(['Open Plugins', '打开插件'])} onClick={e => openPage('plugins', e.currentTarget)}>
                <span className="head"><span className="label">{t(['Plugins', '插件'])}</span><span className="meta">{t([`${pluginsOn} on`, `${pluginsOn} 个开`])}</span></span>
                <span className="pl-mini">{pluginIds.slice(0, 4).map(id => <i key={id} className={`mk-${id} ${plugins[id].state === 'on' ? 'on' : plugins[id].state === 'off' ? 'off' : 'need'}`} title={`${plugins[id].name}: ${t(pluginStatus(plugins[id])[1])}`}><Mark id={id} mark={plugins[id].mark}/></i>)}</span>
              </button>
              <button className="row tile" data-row="projects" aria-label={t(['Open Projects', '打开项目'])} onClick={e => openPage('projects', e.currentTarget)}>
                <span className="head"><span className="label">{t(['Projects', '项目'])}</span><span className="meta">{t(['7 d', '7 天'])}</span></span>
                <span className="pj-mini">{topProject ? <><span><b>{topProject.name}</b> {duration(topProject.seconds)}</span><Cols days={topProject.days}/></> : <span>{projects.missing ? t(['Not set up', '还没设置']) : projectsView ? t(['No time yet', '还没有时间']) : t(['Syncing…', '同步中…'])}</span>}</span>
              </button>
            </>,
          }[id]()}</HomeBlock>)}
          {!blocks.length && <p className="muted home-empty">{t(['Everything is hidden. Hold here or open Settings › Home.', '全部隐藏了。去 设置 › 首页 恢复。'])}</p>}</div>
        </div>
      </div>
      {page && <section className="page" ref={pageEl} aria-label={t(TITLES[page])}>{pages[page]()}</section>}
    </div>
    <div className="cmp-hit" onClick={e => { const input = e.currentTarget.querySelector('input'); if (input && !(e.target as Element).closest('button,input')) focusWindow({ currentTarget: input }); }}><Ask className="cmp" text={homeDraft} setText={setHomeDraft} onAsk={ask}/></div>
    <div className={`toast ${toast ? 'is-on' : ''}`} role="status">{toast?.text}{toast?.undo && <button onClick={() => { toast.undo!(); setToast(null); }}>{t(['Undo', '撤销'])}</button>}</div>
  </div>;
}

// One block on the home. The pop-ups carry a × that closes what they show now; the tiles keep their own grid.
function HomeBlock({ id, pop, lang, onClose, children }: { id: BlockId; pop: boolean; lang: Lang; onClose: () => void; children: ReactNode }) {
  return <div className={id === 'tiles' ? 'tiles' : `row r-${id}${pop ? ' is-pop' : ''}`} data-block={id}>
    {children}
    {pop && <button className="mx" aria-label={`${tr(lang, ['Close', '关掉'])} ${tr(lang, BLOCK[id].name)}`} title={tr(lang, ['Close', '关掉'])} onClick={onClose}><X size={11} weight="bold"/></button>}
  </div>;
}

// The companion window takes no key focus until you reach for a text box.
const focusWindow = (event: { currentTarget: HTMLElement }) => { const el = event.currentTarget; void window.jarvis?.focus(true).then(() => el.focus({ preventScroll: true })); };

// Typing an on-word deepens the box before you send.
function Ask({ className, onAsk, think, text, setText }: { className: string; onAsk: (text: string) => void; think?: Think; text: string; setText: (text: string) => void }) {
  const t = useT();
  const deep = !!think && deepAfter(think, text);
  return <form className={`${className}${deep ? ' is-deep' : ''}`} onSubmit={event => {
    event.preventDefault();
    if (!text.trim()) return;
    onAsk(text.trim()); setText(''); event.currentTarget.querySelector('input')?.blur();
  }}>
    <input aria-label={t(['Message Jarvis', '给 Jarvis 发消息'])} placeholder={t(['Ask Jarvis…', '问问 Jarvis…'])} autoComplete="off" value={text} onChange={event => setText(event.target.value)}
      onPointerDown={focusWindow} onKeyDown={event => { if (event.key === 'Enter' && event.nativeEvent.isComposing) event.preventDefault(); }}/>
    <button className="send" aria-label={t(['Send', '发送'])} disabled={!text.trim()}><ArrowUp size={13} weight="bold"/></button>
  </form>;
}

function Fold({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return <><button className="fold" aria-expanded={open} onClick={() => setOpen(value => !value)}>{label}<CaretDown size={11}/></button>
    <div className="fold-body" inert={!open}><div>{children}</div></div></>;
}

// The service's own usage or billing page, in the browser; main keeps the list of pages.
function Account({ id, children }: { id: string; children: ReactNode }) {
  const t = useT();
  return <button className="us-link" title={t(['Open in the browser', '在浏览器里打开'])} onClick={() => void window.jarvis?.openAccount?.(id)}>{children}<ArrowSquareOut size={11} className="us-out"/></button>;
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
  const t = useT();
  return <span className="group"><span className="plan">{name} <em>{plan}</em></span>
    {ok && windows.length ? <span className="ringset">{windows.map(w => <Ring key={w.key} w={w} name={RING_NAME[w.key] ?? w.label} sub={fmtReset(w.resets_at).replace('resets in ', '')}/>)}</span>
      : <span className="plan">{synced ? t(['Not set up', '还没设置']) : t(['Syncing…', '同步中…'])}</span>}
  </span>;
}
// Today's spend as one ring cut by model, biggest first; the list beside it names each cut.
// The cuts retain their values when the page opens; no decorative fill replay.
// Only models that cost a cent today are listed; the rest fold into one line that opens them.
function Spend({ total, models }: { total: number; models: { model: string; today_usd: number }[] }) {
  const t = useT();
  const [all, setAll] = useState(false);
  const sorted = [...models].sort((a, b) => b.today_usd - a.today_usd), tint = (i: number) => `color-mix(in srgb,var(--data) ${Math.max(20, 100 - i * 55)}%,transparent)`;
  const paid = sorted.filter(m => m.today_usd >= .005), free = sorted.length - paid.length;
  let edge = 0;
  const stops = sorted.map((m, i) => { const from = edge; edge += total ? m.today_usd / total : 0; return `${tint(i)} calc(var(--fill) * ${from}%) calc(var(--fill) * ${edge}%)`; });
  return <div className="spend">
    <span className="ring"><span className="dial donut"><i style={{ background: `conic-gradient(${[...stops, 'color-mix(in srgb,var(--data) 12%,transparent) 0'].join(',')})` }}/><b>{usd(total)}<small>{t(['today', '今天'])}</small></b></span></span>
    <ul>{(all ? sorted : paid).map((m, i) => <li key={m.model}><i style={{ background: tint(i) }}/>{m.model}<span>{usd(m.today_usd)}</span></li>)}
      {free > 0 && <li><button className="more" aria-expanded={all} onClick={() => setAll(v => !v)}>{all ? 'Show less' : `${free} more at $0.00`}</button></li>}</ul>
  </div>;
}
// OpenAI reports no balance (ADR 0065): Allen types the one on its billing page and
// the daemon subtracts what is spent after it, so the number shown is an estimate since then.
function Balance({ id, name, left, since, live, onSaved }: { id: 'openai'; name: string; left?: number; since?: string | null; live: boolean; onSaved: () => void }) {
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
  const peak = Math.max(...days, 1), t = useT();
  const day = (iso: string, i: number) => i === days.length - 1 ? t(['Today', '今天'])
    : `${new Date(`${iso}T12:00:00`).toLocaleDateString(t(['en-US', 'zh-CN']), { weekday: 'short' })} ${Number(iso.slice(5, 7))}/${Number(iso.slice(8, 10))}`;
  return <span className="cols" role={dates ? 'img' : undefined} aria-hidden={!dates} aria-label={dates ? `Hours per day, today last: ${days.map(duration).join(', ')}` : undefined}>
    {days.map((s, i) => <span key={i} style={{ '--h': s / peak * 100 } as CSSProperties}><i/>{dates?.[i] && <b className="tip">{day(dates[i], i)} · {s ? duration(s) : t(['nothing', '没有'])}</b>}</span>)}
  </span>;
}

// Folded: the state mark, the session's name and its tags. A click opens it to what you said, what it is
// doing, and the actions; the jump to its terminal or Codex thread lives there too.
function AgentRow({ s, look, mark, open, onToggle, onOpen, onHide, actions }: { s: Agent; look: MarkLook; mark: MarkState; open: boolean; onToggle: () => void; onOpen?: () => void; onHide: () => void; actions?: ReactNode }) {
  const t = useT();
  return <article className={`ag is-${s.state} ${open ? 'is-open' : ''}`} data-id={s.id}>
    <AgentMark look={look} state={mark} id={s.id} size={14}/>
    <div className="ag-body">
      <button className="ag-head" aria-expanded={open} onClick={onToggle}>
        <span className="ag-top"><span className="ag-title">{s.title}</span><span className="age">{s.age}</span></span>
        <span className="ag-tags"><span className={`tagc ${s.agent}`}>{AGENT_NAME[s.agent]}</span><span className="tagc">{s.project}</span>
          {s.where !== 'Codex' && <span className="tagc">{s.where}</span>}{s.sub && <span className="tagc sub">{t(['Subagent', '子代理'])}</span>}</span>
      </button>
      <div className="ag-more" inert={!open}><div>
        {s.branch && <span className="ag-meta"><GitBranch size={10}/>{s.branch}</span>}
        {s.you && <span className="ag-you"><b>{t(['You', '你'])}</b>{s.you}</span>}
        {s.last && <span className="ag-last">{s.last}</span>}
        <span className="ag-actions">{s.state === 'wait' && actions}{onOpen && <button className="ag-go" onClick={onOpen}>{t([`Open in ${s.where}`, `在 ${s.where} 打开`])}<ArrowSquareOut size={11}/></button>}</span>
      </div></div>
    </div>
    <button className="ag-x" aria-label={`${t(['Hide', '隐藏'])} ${s.title}`} onClick={onHide}><X size={11}/></button>
    {s.state === 'work' && <i className="shimmer"/>}
  </article>;
}

function PluginDetail({ id, p, token, onToken, onAct }: { id: string; p: DemoPlugin; token: string; onToken: (value: string) => void; onAct: (act: string, value?: string) => void }) {
  const t = useT();
  const top = <><div className="pg-sec pl-id"><span className={`pl-ic lg mk-${id}`}><Mark id={id} mark={p.mark}/></span><h5>{p.name}</h5><p>{p.about}</p>{p.state === 'on' && <span className="pill is-new">{t(['Connected', '已连接'])}</span>}</div>
    {p.error && p.state !== 'on' && p.state !== 'connecting' && <p className="pg-sec is-warm" role="alert">{p.error}</p>}</>;
  if (p.unsupported) return <>{top}<p className="pg-sec muted">{p.unsupported}</p></>;
  const act = (name: string, label: L, className = 'btn-text') => <button className={className} onClick={() => onAct(name)}>{t(label)}</button>;
  if (p.state === 'connecting') return <>{top}
    <div className="pg-sec waiting" role="status"><span className="spin-ring"/><p>{p.kind === 'oauth' ? t(['Waiting for you in the browser…', '在浏览器里等你…']) : t(['Connecting…', '连接中…'])}</p><p className="muted">{p.ask ? t(['Jarvis picks the task back up once you’re in.', '你登录后 Jarvis 会接着做那件事。']) : t(['You can close the panel. It keeps waiting.', '可以关掉面板，它会继续等。'])}</p></div>
    <div className="pg-sec acts row-btns">{p.kind === 'oauth' && act('reopen', ['Open sign-in page again', '再打开登录页'], 'btn btn-ghost')}{act('cancel', ['Cancel', '取消'])}</div>
  </>;
  if (p.state === 'on') return <>{top}
    {p.resumed && <div className="pg-sec ask-card is-ok"><span>{t(['Back to your task', '接着做你的事'])}</span>{p.resumed}</div>}
    <div className="pg-sec"><div><div className="kv"><span>{t(['Tools', '工具'])}</span><span>{p.toolCount}</span></div><div className="kv"><span>{t(['Can', '能做'])}</span><span>{p.can?.length ? p.can.join(' · ') : t(['Read · Write', '读 · 写'])}</span></div></div></div>
    <label className="pg-sec field">{t(['Ask before acting', '动手前先问'])}<select aria-label={t(['Ask before acting', '动手前先问'])} value={p.approval ?? 'auto'} onChange={e => onAct('approval', e.target.value)}>
      {p.approval === 'configured' && <option value="configured" disabled>{t(['Each service’s own setting', '按各服务自己的设置'])}</option>}
      <option value="auto">{t(['Let Jarvis decide (default)', '让 Jarvis 决定（默认）'])}</option><option value="prompt">{t(['Ask every time', '每次都问'])}</option><option value="writes">{t(['Ask before it writes', '写入前问'])}</option><option value="approve">{t(['Don’t ask', '不用问'])}</option></select><small>{t(['A tool’s own setting wins over this.', '工具自己的设置优先。'])}</small></label>
    {p.tools.length > 0 && <div className="pg-sec"><Fold label={t(['Show tools', '看看有哪些工具'])}><pre>{p.tools.join('\n')}</pre></Fold></div>}
    <footer className="pg-foot"><span>{t(['Turning it off removes its tools.', '关掉后它的工具就没了。'])}</span>{act('off', ['Turn off', '关掉'], 'btn-text is-alert')}</footer>
  </>;
  if (p.state === 'token') return <>{top}
    <label className="pg-sec field">{t(['Access token', '访问令牌'])}<input type="password" placeholder={p.saved ? t(['Saved · leave empty to keep it', '已保存 · 留空就不变']) : 'ghp_…'} autoComplete="off" aria-label={`${p.name} ${t(['access token', '访问令牌'])}`} value={token} onChange={e => onToken(e.target.value)} onPointerDown={focusWindow}/><small>{t(['Stays on this Mac. It never goes into the conversation.', '只存在这台 Mac 上，不会进对话。'])}</small></label>
    <div className="pg-sec acts">{act('connect', ['Connect', '连接'], 'btn btn-glow wide')}{act('later', ['Not now', '先不'])}</div>
  </>;
  if (p.state === 'off') return <>{top}
    <ul className="pg-sec facts"><li><ArrowSquareOut size={13}/><span>{t(['Adds its tools to Jarvis on this Mac.', '把它的工具加给这台 Mac 上的 Jarvis。'])}</span></li><li><ShieldCheck size={13}/><span>{t(['Asks before it writes, unless you change that.', '写入前会先问你，除非你改了设置。'])}</span></li></ul>
    <div className="pg-sec acts">{act('connect', ['Turn on', '打开'], 'btn btn-glow wide')}{act('later', ['Not now', '先不'])}</div>
  </>;
  return <>{top}
    {p.ask && <div className="pg-sec ask-card"><span>{t(['Jarvis asked for this', 'Jarvis 要用它'])}</span>“{p.ask}”</div>}
    <ul className="pg-sec facts"><li><ArrowSquareOut size={13}/><span>{t([`Opens ${p.name} in your browser to sign in.`, `在浏览器里打开 ${p.name} 登录。`])}</span></li><li><ShieldCheck size={13}/><span>{t(['You choose what it can see there.', '在那里由你决定它能看到什么。'])}</span></li></ul>
    <div className="pg-sec acts">{act('connect', p.ask ? ['Sign in and continue', '登录并继续'] : ['Sign in', '登录'], 'btn btn-glow wide')}{act('later', ['Not now', '先不'])}</div>
  </>;
}

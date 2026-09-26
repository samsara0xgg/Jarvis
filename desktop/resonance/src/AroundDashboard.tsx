import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from 'react';
import { ArrowSquareOut, ArrowUp, ArrowsClockwise, CaretDown, CaretLeft, CaretRight, GitBranch, MagnifyingGlass, ShieldCheck, X } from '@phosphor-icons/react';
import { TAKES, pick, type ExprId } from './starCore';
import { useUsage, type UsageWindow } from './QuotaModule';
import { useCodexSessions, type CodexSession } from './CodexModule';
import { freshnessLine, nowLine, useWorkState, type Basis } from './WorkStateModule';
import { duration, useProjects } from './ProjectsModule';
import { fmtReset } from './quota-time';
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

// Agents: Claude Code and Codex sessions together. Codex rows are live once the companion has a
// daemon port; Claude rows wait for the Claude Code bridge. Without a port every row is a demo.
type AgentState = 'wait' | 'work' | 'done';
type Agent = { id: string; agent: 'claude' | 'codex'; state: AgentState; title: string; project: string; branch?: string; where: string; age: string; you: string; last: string; sub?: boolean };
const AGENT_NAME = { claude: 'Claude', codex: 'Codex' };
const DEMO_AGENTS: Agent[] = [
  { id: 'usage', state: 'wait', agent: 'codex', project: 'jarvis', title: 'Adjust the usage page', where: 'Codex', age: '2m', you: 'make the usage rings match', last: 'Wants to run npm run build' },
  { id: 'inner', state: 'work', agent: 'claude', project: 'jarvis', branch: 'companion-ball', title: 'Dashboard inner pages', where: 'Ghostty', age: '4m', you: 'add the plugins page and fix the bottom bar', last: 'Editing the design page…' },
  { id: 'review', state: 'work', agent: 'claude', sub: true, project: 'jarvis', branch: 'companion-ball', title: 'Check the panel pages', where: 'Ghostty', age: '1m', you: 'check every page against the design', last: 'Comparing the Usage page…' },
  { id: 'voice', state: 'work', agent: 'codex', project: 'jarvis', title: 'Fix voice reconnect', where: 'Codex', age: '9m', you: 'the voice drops after the Mac sleeps', last: 'Reading the reconnect logic…' },
  { id: 'aec', state: 'done', agent: 'claude', project: 'jarvis', title: 'Mac echo cancel', where: 'zellij', age: '1h', you: 'why does it keep saying “mm”?', last: 'Found it: a search result was read aloud.' },
  { id: 'loop', state: 'done', agent: 'codex', project: 'jarvis', title: 'Evaluate the minimal loop', where: 'Codex', age: '2h', you: 'what’s the smallest loop that works?', last: 'Summarized the loop and what’s left.' },
  { id: 'cap', state: 'done', agent: 'claude', project: 'typlus', title: 'Long dictation cap', where: 'Ghostty', age: '3h', you: 'long notes get cut off', last: 'Raised the cap and installed the build.' },
];
const ago = (ms: number) => { const m = Math.round((Date.now() - ms) / 60_000); return m < 1 ? 'now' : m < 60 ? `${m}m` : m < 1440 ? `${Math.floor(m / 60)}h` : `${Math.floor(m / 1440)}d`; };
const fromCodex = (r: CodexSession): Agent => ({
  id: r.session_id, agent: 'codex', state: r.state === 'needs_input' ? 'wait' : r.state === 'running' ? 'work' : 'done',
  title: r.title || r.prompt || 'Codex session', project: r.cwd.split('/').filter(Boolean).pop() ?? '', where: 'Codex',
  age: ago(r.since_ms), you: r.prompt, last: r.state === 'finished' ? r.last_message : r.detail,
});

// Plugins: a demo catalog until the companion talks to the daemon's plugin bridge.
type PluginState = 'on' | 'off' | 'token' | 'signin' | 'connecting';
type DemoPlugin = { name: string; mark: string; kind: 'oauth' | 'token' | 'none'; about: string; state: PluginState; was?: PluginState; ask?: string; resumed?: string; toolCount: number; tools: string[] };
const PLUGIN_ORDER = ['notion', 'microsoft', 'github', 'linear'];
const DEMO_PLUGINS: Record<string, DemoPlugin> = {
  notion: { name: 'Notion', mark: 'N', kind: 'oauth', about: 'Pages and databases in your workspace.', state: 'signin', ask: 'Find last week’s meeting notes and sum them up.', toolCount: 9, tools: ['search', 'fetch_page', 'create_page', 'update_page', 'query_database'] },
  microsoft: { name: 'Microsoft 365', mark: 'M', kind: 'oauth', about: 'Your To Do lists and Outlook calendar.', state: 'on', toolCount: 14, tools: ['list_todo_tasks', 'create_todo_task', 'list_calendar_events', 'create_calendar_event', '…10 more'] },
  github: { name: 'GitHub', mark: 'G', kind: 'token', about: 'Repositories, issues and pull requests.', state: 'token', toolCount: 21, tools: ['search_issues', 'get_pull_request', 'create_issue', 'list_commits', '…17 more'] },
  linear: { name: 'Linear', mark: 'L', kind: 'none', about: 'Issues and projects.', state: 'off', toolCount: 8, tools: ['list_issues', 'create_issue', 'update_issue', '…5 more'] },
};
const pluginStatus = (p: DemoPlugin): [string, string] => p.state === 'on' ? ['is-on', 'Connected']
  : p.state === 'connecting' ? ['is-need', p.kind === 'oauth' ? 'Waiting for sign-in…' : 'Connecting…']
  : p.state === 'off' ? ['', 'Off'] : p.state === 'token' ? ['is-need', 'Needs an access token']
  : ['is-need', p.ask ? 'Jarvis asked · needs sign-in' : 'Needs sign-in'];

type Turn = { you: string; at: string; jarvis?: string; jarvisAt?: string; work?: [string, string] };
const DEMO_TURNS: Turn[] = [
  { you: 'Remind me to test the mic at four.', at: '11:05', jarvis: 'Done. I’ll remind you at 4 PM.', jarvisAt: '11:05' },
  { you: 'What’s left on my plate today?', at: '14:32', jarvisAt: 'just now',
    jarvis: 'Two things left today. Your 4 PM reminder is set. You can confirm the dashboard direction first, then check the voice test at 4 PM.',
    work: ['Worked it out with gpt-5.6-luna · 2.1 s', 'Checked today’s to-dos:\n1. Confirm the Resonance dashboard direction.\n2. 16:00 voice test reminder, scheduled.'] },
];
const ANSWER = 'Got it. I’ll take care of it and tell you when it’s done.';
const BASIS: Record<Basis, string> = { observed: 'Observed', stated: 'You said', inferred: 'A guess' };

export function AroundDashboard({ open, port = null, onClose, onMood, onHop }: {
  open: boolean; port?: string | null; onClose: () => void; onMood: (expr: ExprId | null) => void; onHop: (height: number) => void;
}) {
  const quota = useUsage(port), codex = useCodexSessions(port), work = useWorkState(port), projects = useProjects(port, open);
  const [page, setPage] = useState<Page | null>(null);
  const [plugin, setPlugin] = useState<string | null>(null);
  const [plugins, setPlugins] = useState(DEMO_PLUGINS);
  const [query, setQuery] = useState('');
  const [token, setToken] = useState('');
  const [said, setSaid] = useState({ text: 'Two things left today. Your 4 PM reminder is set.', caption: 'Jarvis · just now', busy: false });
  const [turns, setTurns] = useState(DEMO_TURNS);
  const [moved, setMoved] = useState<Record<string, Partial<Agent> & { at: number }>>({});
  // Agent cards show only name, state and tags; one card at a time opens to show the rest.
  const [unfolded, setUnfolded] = useState<string | null>(null);
  const [hidden, setHidden] = useState<string[]>([]);
  const [toast, setToast] = useState<{ text: string; undo?: () => void } | null>(null);
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
    closing.current = false; setPage(null); setPlugin(null); setUnfolded(null); react('02', 0);
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
    if (name === 'conversation') react(pick(TAKES.reply), 2600);
    else if (name === 'now') react('37', 2400);
    else if (name === 'projects') { react('40', 1500); void projects.refresh(); }
    else { react('02', 0); if (name === 'agents') onHop(.2); }
  };
  // The row grows into the page: its outline opens to the whole panel and its title slides up to the top.
  useLayoutEffect(() => {
    const el = pageEl.current;
    if (!page || !el) return;
    const from = row(page), dy = from.getBoundingClientRect().top - view.current!.getBoundingClientRect().top;
    el.animate([{ clipPath: insetOf(from) }, { clipPath: 'inset(0 0 0 0 round 14px)' }], { duration: dur(560), easing: SPRING });
    el.querySelector('.pg-head')?.animate([{ transform: `translateY(${dy}px)`, opacity: .3 }, { transform: 'none', opacity: 1 }], { duration: dur(560), easing: SPRING });
    el.querySelectorAll('.pg-sec, .pg-foot, .pg-input').forEach((s, i) => s.animate([{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }],
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
    setTurns(value => [...value, { you: text, at: 'now' }]);
    const reply = pick(TAKES.reply);
    setSaid({ text: 'Thinking…', caption: 'Thinking', busy: true }); react('30', 1300, reply);
    later(1300, () => {
      setSaid({ text: ANSWER, caption: 'Jarvis · just now', busy: false }); react(reply, 2400);
      setTurns(value => value.map((t, i) => i === value.length - 1 ? { ...t, jarvis: ANSWER, jarvisAt: 'just now' } : t));
    });
  };
  const body = () => pageEl.current?.querySelector('.pg-body');
  useEffect(() => { if (page === 'conversation') body()?.scrollTo({ top: body()!.scrollHeight, behavior: reduced.matches ? 'auto' : 'smooth' }); }, [turns]);

  // Agents, grouped the way you act on them. A row you moved goes to the top of its new group.
  const agents = (port ? codex.rows.map(fromCodex) : DEMO_AGENTS).filter(s => !hidden.includes(s.id)).map(s => ({ ...s, ...moved[s.id] }));
  const group = (state: AgentState) => agents.filter(s => s.state === state).sort((a, b) => (moved[b.id]?.at ?? 0) - (moved[a.id]?.at ?? 0));
  const waiting = group('wait'), working = group('work'), earlier = group('done');
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
    setHidden(value => [...value, s.id]);
    notify('Hidden from this list.', () => setHidden(value => value.filter(id => id !== s.id)));
  };
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
  const pluginAct = (act: string) => {
    const id = plugin!, p = plugins[id];
    if (act === 'later') setPlugin(null);
    else if (act === 'reopen') notify('Opened the sign-in page again.');
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
      {back('Conversation', 'today')}
      <div className="pg-body">{turns.map((t, i) => <div className="pg-sec tr" key={i}>
        <div className="tr-you"><span className="who">You · {t.at}</span><p>{t.you}</p></div>
        {t.jarvis && <div className="tr-jarvis"><span className="who"><span className="dot"/>Jarvis · {t.jarvisAt}</span><p>{t.jarvis}</p>
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
      {waiting.length > 0 && <div className="pg-sec"><h4 className="is-warm"><span className="dot"/>Needs you</h4>{waiting.map(s => <AgentRow key={s.id} s={s} open={unfolded === s.id} onToggle={() => setUnfolded(v => v === s.id ? null : s.id)} onOpen={() => void openAgent(s)} onHide={() => hide(s)}
        actions={!port && <><button className="btn btn-glow" onClick={() => { move(s, { state: 'work', last: 'Approved · running it now…' }); react('33', 1900); }}>Approve</button>
          <button className="btn btn-ghost" onClick={() => move(s, { state: 'done', last: 'You denied it. It stopped there.', age: 'now' })}>Deny</button></>}/>)}</div>}
      {working.length > 0 && <div className="pg-sec"><h4>Working · {working.length}</h4>{working.map(s => <AgentRow key={s.id} s={s} open={unfolded === s.id} onToggle={() => setUnfolded(v => v === s.id ? null : s.id)} onOpen={() => void openAgent(s)} onHide={() => hide(s)}/>)}</div>}
      {earlier.length > 0 && <div className="pg-sec"><h4>Earlier today · {earlier.length}</h4>{earlier.map(s => <AgentRow key={s.id} s={s} open={unfolded === s.id} onToggle={() => setUnfolded(v => v === s.id ? null : s.id)} onOpen={() => void openAgent(s)} onHide={() => hide(s)}/>)}</div>}
      {agents.length > 0 && <p className="pg-sec muted">Click a session to jump to it.</p>}</div>
    </>,
    usage: () => <>
      {back('Usage', synced ? `synced ${hm(synced)}` : 'syncing…')}
      <div className="pg-body"><div className="pg-sec"><div className="us-plan">Claude Max <em>{claude?.data.plan}</em>{claude?.status === 'ok' && claude.data.reset_credits !== undefined && <span className="meta">{resetsLeft(claude.data.reset_credits, claude.data.reset_ends_at)}</span>}</div>
        {claude?.status === 'ok' ? <div className="bigrings">{(claude.data.windows ?? []).map(w => <Ring key={w.key} w={w} name={w.label} sub={fmtReset(w.resets_at)}/>)}</div> : <p className="muted">{claude?.error ?? 'Not signed in to Claude Code'}</p>}</div>
      <div className="pg-sec"><div className="us-plan">Codex <em>{codexUsage?.data.plan}</em>{codexUsage?.status === 'ok' && <span className="meta">{resetsLeft(codexUsage.data.reset_credits ?? 0)}</span>}</div>
        {codexUsage?.status === 'ok' ? <div className="bigrings">{(codexUsage.data.windows ?? []).map(w => <Ring key={w.key} w={w} name={w.label} sub={fmtReset(w.resets_at)}/>)}</div> : <p className="muted">{codexUsage?.error ?? 'Not signed in to Codex'}</p>}</div>
      <div className="pg-sec"><div className="us-plan">OpenAI <em>API</em>{openai?.status === 'ok' && <span className="meta">this month {usd(openai.data.month_usd)}</span>}</div>
        {openai?.status === 'ok' ? <Spend total={openai.data.today_usd ?? 0} models={openai.data.by_model ?? []}/> : <p className="muted">{openai?.error ?? 'Needs an admin key'}</p>}</div>
      <div className="pg-sec"><h4>Balances</h4><div className="bal">
        <div><span>DeepSeek</span><b>{deepseek?.status === 'ok' ? usd(deepseek.data.balance) : '—'}</b></div>
        <div><span>MiniMax</span><b>{minimax?.status === 'ok' ? `≈ ${usd(minimax.data.estimate_usd)}` : '—'}</b><small>estimated from use</small></div>
      </div></div></div>
    </>,
    plugins: () => {
      const p = plugin ? plugins[plugin] : null;
      const shown = PLUGIN_ORDER.filter(id => plugins[id].name.toLowerCase().includes(query.trim().toLowerCase()));
      return <>
        {back(p ? p.name : 'Plugins', !p && `${pluginsOn} connected`)}
        <div className="pg-body">{p ? <div className="pl-det" key={plugin}><PluginDetail id={plugin!} p={p} token={token} onToken={setToken} onAct={pluginAct} onSaved={() => notify('Saved.')}/></div>
          : <div className="pl-cat">
            <label className="pg-sec search"><MagnifyingGlass size={13}/><input type="search" aria-label="Search plugins" placeholder="Search plugins" autoComplete="off" value={query} onChange={e => setQuery(e.target.value)} onPointerDown={focusWindow}/></label>
            <div className="pg-sec pl-list">{shown.map(id => { const [cls, text] = pluginStatus(plugins[id]);
              return <button key={id} className="pl-row" data-plugin={id} onClick={() => setPlugin(id)}><span className={`pl-ic mk-${id}`}>{plugins[id].mark}</span><span className="pl-name">{plugins[id].name}<small className={cls}>{text}</small></span><CaretRight size={12}/></button>; })}</div>
            {!shown.length && <p className="muted">No plugins match.</p>}
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
            <span className="say" key={said.text}>{said.text}</span>
            <span className={`cap ${said.busy ? 'is-busy' : ''}`}><i/><span>{said.caption}</span></span>
          </button>
        </div>
        <button className="row r-now" data-row="now" aria-label="Open Right now" onClick={() => openPage('now')}>
          <span className="head"><span className="label">Now</span><span className={`meta ${fresh.stale ? 'is-warm' : ''}`} title={fresh.text}>{now ? `${now.current ? 'as of' : 'at'} ${now.at}` : ''}</span></span>
          <span className="text">{now ? now.claim : workView === null ? 'Syncing…' : 'No recent activity observed'}</span>
        </button>
        <button className="row r-agents" data-row="agents" aria-label="Open Agents" onClick={() => openPage('agents')}>
          <span className="head"><span className="label">Agents</span><span className="head-r">
            <span className="orbs">{waiting.map(s => <i key={s.id} className="orb sm is-wait"/>)}{working.slice(0, 4).map(s => <i key={s.id} className="orb sm is-work"/>)}</span>
            {(waiting.length > 0 || working.length > 0) && <span className={`pill ${waiting.length ? 'is-waiting' : ''}`}>{waiting.length ? `${waiting.length} ${waiting.length > 1 ? 'need' : 'needs'} you` : `${working.length} working`}</span>}
          </span></span>
          <span className="text one">{lead ? <><span className={`tagc ${lead.agent}`}>{AGENT_NAME[lead.agent]}</span>{lead.title}{lead.state === 'wait' && lead.last ? ` · ${lead.last.replace(/^Wants/, 'wants')}` : ''}</> : 'Sessions show up once you start one.'}</span>
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
            <span className="pl-mini">{PLUGIN_ORDER.map(id => <i key={id} className={`mk-${id} ${plugins[id].state === 'on' ? 'on' : plugins[id].state === 'off' ? 'off' : 'need'}`} title={`${plugins[id].name}: ${pluginStatus(plugins[id])[1]}`}>{plugins[id].mark}</i>)}</span>
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
function Spend({ total, models }: { total: number; models: { model: string; today_usd: number }[] }) {
  const sorted = [...models].sort((a, b) => b.today_usd - a.today_usd), tint = (i: number) => `rgb(var(--glow) / ${Math.max(.2, 1 - i * .55)})`;
  let edge = 0;
  const stops = sorted.map((m, i) => { const from = edge; edge += total ? m.today_usd / total : 0; return `${tint(i)} calc(var(--fill) * ${from}%) calc(var(--fill) * ${edge}%)`; });
  return <div className="spend">
    <span className="ring"><span className="dial donut"><i style={{ background: `conic-gradient(${[...stops, 'rgb(var(--glow) / .12) 0'].join(',')})` }}/><b>{usd(total)}<small>today</small></b></span></span>
    <ul>{sorted.map((m, i) => <li key={m.model}><i style={{ background: tint(i) }}/>{m.model}<span>{usd(m.today_usd)}</span></li>)}</ul>
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

// Folded: the state orb, the session's name and its tags. A click opens it to what you said, what it is
// doing, and the actions; the jump to its terminal or Codex thread lives there too.
function AgentRow({ s, open, onToggle, onOpen, onHide, actions }: { s: Agent; open: boolean; onToggle: () => void; onOpen: () => void; onHide: () => void; actions?: ReactNode }) {
  return <article className={`ag is-${s.state} ${open ? 'is-open' : ''}`} data-id={s.id}>
    <span className={`orb is-${s.state}`}/>
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
        <span className="ag-actions">{s.state === 'wait' && actions}<button className="ag-go" onClick={onOpen}>Open in {s.where}<ArrowSquareOut size={11}/></button></span>
      </div></div>
    </div>
    <button className="ag-x" aria-label={`Hide ${s.title}`} onClick={onHide}><X size={11}/></button>
    {s.state === 'work' && <i className="shimmer"/>}
  </article>;
}

function PluginDetail({ id, p, token, onToken, onAct, onSaved }: { id: string; p: DemoPlugin; token: string; onToken: (value: string) => void; onAct: (act: string) => void; onSaved: () => void }) {
  const top = <div className="pg-sec pl-id"><span className={`pl-ic lg mk-${id}`}>{p.mark}</span><h5>{p.name}</h5><p>{p.about}</p>{p.state === 'on' && <span className="pill is-new">Connected</span>}</div>;
  const act = (name: string, label: string, className = 'btn-text') => <button className={className} onClick={() => onAct(name)}>{label}</button>;
  if (p.state === 'connecting') return <>{top}
    <div className="pg-sec waiting" role="status"><span className="spin-ring"/><p>{p.kind === 'oauth' ? 'Waiting for you in the browser…' : 'Connecting…'}</p><p className="muted">{p.ask ? 'Jarvis picks the task back up once you’re in.' : 'You can close the panel. It keeps waiting.'}</p></div>
    <div className="pg-sec acts row-btns">{p.kind === 'oauth' && act('reopen', 'Open sign-in page again', 'btn btn-ghost')}{act('cancel', 'Cancel')}</div>
  </>;
  if (p.state === 'on') return <>{top}
    {p.resumed && <div className="pg-sec ask-card is-ok"><span>Back to your task</span>{p.resumed}</div>}
    <div className="pg-sec"><div><div className="kv"><span>Tools</span><span>{p.toolCount}</span></div><div className="kv"><span>Can</span><span>Read · Write</span></div></div></div>
    <label className="pg-sec field">Ask before acting<select aria-label="Ask before acting" onChange={onSaved}><option>Let Jarvis decide (default)</option><option>Ask every time</option><option>Ask before it writes</option><option>Don’t ask</option></select><small>A tool’s own setting wins over this.</small></label>
    <div className="pg-sec"><Fold label="Show tools"><pre>{p.tools.join('\n')}</pre></Fold></div>
    <footer className="pg-foot"><span>Turning it off removes its tools.</span>{act('off', 'Turn off', 'btn-text is-alert')}</footer>
  </>;
  if (p.state === 'token') return <>{top}
    <label className="pg-sec field">Access token<input type="password" placeholder="ghp_…" autoComplete="off" aria-label={`${p.name} access token`} value={token} onChange={e => onToken(e.target.value)} onPointerDown={focusWindow}/><small>Stays on this Mac. It never goes into the conversation.</small></label>
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

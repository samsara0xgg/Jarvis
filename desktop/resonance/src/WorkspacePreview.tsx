import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { ArrowLeft, ArrowUp, ArrowRight, ArrowsClockwise, CaretRight, ChatCircle, Check, CircleNotch, Compass, FileText, GearSix, GitBranch, GithubLogo, IconContext, MagnifyingGlass, Microphone, Minus, Plugs, SquaresFour, Stop, User, X, ChartPie } from '@phosphor-icons/react';
import { PresentationCapsule } from './PresentationCapsule';
import { PluginPanel, type Plugin as CatalogPlugin } from './PluginPanel';
import { CodexDetail, useCodexSessions } from './CodexModule';
import { QuotaDetail, demoUsage } from './QuotaModule';
import { surfaceThemes, activeSurfaceTheme } from './surface-themes';
import './workspace-preview.css';

type Page = 'dashboard' | 'conversation' | 'plugins' | 'plugin' | 'codex' | 'quota' | 'status' | 'settings';
type Connection = 'disconnected' | 'authorizing' | 'connected' | 'error';
type Plugin = { id: string; name: string; description: string; tools: number; auth?: 'token' | 'skill'; unavailable?: boolean };
type Message = { id: number; who: 'you' | 'jarvis'; text: string; plugin?: string };
type Pending = { plugin: string; origin: number | null };
const plugins: Plugin[] = [
  { id: 'linear', name: 'Linear', description: '产品规划与任务管理', tools: 68 },
  { id: 'notion', name: 'Notion', description: '笔记与知识库', tools: 12 },
  { id: 'figma', name: 'Figma', description: '设计与协作', tools: 8 },
  { id: 'github', name: 'GitHub', description: '代码与项目', tools: 24, auth: 'token' },
  { id: 'slack', name: 'Slack', description: '团队消息', tools: 16 },
  { id: 'airtable', name: 'Airtable', description: '表格与结构化数据', tools: 9 },
  { id: 'zoom', name: 'Zoom', description: '会议记录', tools: 7 },
  { id: 'pdf', name: 'PDF', description: '阅读与制作 PDF', tools: 0, auth: 'skill' },
  { id: 'documents', name: 'Documents', description: '创建与编辑文档', tools: 0, auth: 'skill' },
  { id: 'gmail', name: 'Gmail', description: '尚未接入连接器', tools: 0, unavailable: true },
];
const pages: { page: Page; label: string; Icon: typeof ChatCircle }[] = [
  { page: 'conversation', label: '对话', Icon: ChatCircle },
  { page: 'plugins', label: '插件', Icon: Plugs },
  { page: 'codex', label: 'Codex', Icon: GitBranch },
  { page: 'quota', label: '模型额度', Icon: ChartPie },
  { page: 'status', label: '当前状态', Icon: Compass },
  { page: 'settings', label: '设置', Icon: GearSix },
];
const titles: Record<Page, string> = { dashboard: 'Dashboard', conversation: '对话', plugins: '插件', plugin: '连接插件', codex: 'Codex', quota: '模型额度', status: '当前状态', settings: '设置' };
const initialMessages: Message[] = [
  { id: 1, who: 'you', text: '连接 Linear，帮我看看待办。' },
  { id: 2, who: 'jarvis', text: 'Linear 已连接，可以继续刚才的任务。', plugin: 'linear' },
];

function PluginMark({ id }: { id: string }) {
  return <span className={`wp-plugin-mark wp-mark-${id}`} aria-hidden="true">
    {id === 'linear' ? <svg viewBox="0 0 32 32"><circle cx="16" cy="16" r="14" fill="currentColor"/><path d="M3 13 19 29M3 20l8 8M5 7l22 22" stroke="#24272c" strokeWidth="2.6"/></svg>
      : id === 'github' ? <GithubLogo weight="fill"/>
      : id === 'notion' ? <b>N</b>
      : id === 'figma' ? <svg viewBox="0 0 24 36"><path d="M6 0h6v12H6A6 6 0 0 1 6 0" fill="#f24e1e"/><path d="M12 0h6a6 6 0 0 1 0 12h-6" fill="#ff7262"/><path d="M6 12h6v12H6a6 6 0 0 1 0-12" fill="#a259ff"/><circle cx="18" cy="18" r="6" fill="#1abcfe"/><path d="M6 24h6v6a6 6 0 1 1-6-6" fill="#0acf83"/></svg>
      : id === 'slack' ? <svg viewBox="0 0 32 32"><path d="M12 3v12M3 20h12M20 29V17M29 12H17" strokeWidth="6" strokeLinecap="round" stroke="#7dd3b0"/><path d="M3 11h1M11 29v-1M29 21h-1M21 3v1" strokeWidth="6" strokeLinecap="round" stroke="#d89cae"/></svg>
      : id === 'pdf' || id === 'documents' ? <FileText/> : <Plugs/>}
  </span>;
}

export function WorkspacePreview() {
  useEffect(() => { document.title = 'Jarvis · 工作区交互预览'; }, []);
  const [page, setPage] = useState<Page>('dashboard');
  const [open, setOpen] = useState(true);
  const [selected, setSelected] = useState('linear');
  const [connections, setConnections] = useState<Record<string, Connection>>({ linear: 'connected' });
  const [pending, setPending] = useState<Pending | null>(null);
  const [messages, setMessages] = useState(initialMessages);
  const [draft, setDraft] = useState('');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<'all' | 'connected'>('all');
  const [processing, setProcessing] = useState(false);
  const [notice, setNotice] = useState('');
  const [voice, setVoice] = useState(false);
  const [muted, setMuted] = useState(false);
  const [speakerMuted, setSpeakerMuted] = useState(false);
  const [caption, setCaption] = useState<{ who: 'you' | 'jarvis'; text: string } | null>(null);
  const [opacity, setOpacity] = useState(82);
  const [reduced, setReduced] = useState(false);
  const [approvals, setApprovals] = useState<Record<string, string>>({});
  const [connectionError, setConnectionError] = useState('');
  const [quotaLayout, setQuotaLayout] = useState<'category' | 'provider' | 'accordion'>('accordion');
  const board = useCodexSessions(null);
  const history = useRef<Page[]>([]);
  const scrolls = useRef<Record<string, number>>({});
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const nextId = useRef(3);
  const turn = useRef(0);
  const connectionOrigins = useRef<Record<string, number>>({});
  const capsuleRoot = useRef<HTMLDivElement>(null);
  const sequence = useRef(0);
  const replyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const captionTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stickToBottom = useRef(true);
  const focusComposer = useRef(false);
  const plugin = plugins.find(item => item.id === selected)!;
  const connection = connections[selected] ?? 'disconnected';
  const connected = plugins.filter(item => connections[item.id] === 'connected');
  const scrollKey = page === 'plugin' ? `plugin:${selected}` : page;
  const showCaption = voice && caption !== null && (!open || page !== 'conversation');
  const status = (id: string) => connections[id] ?? 'disconnected';
  const statusLabel = (id: string) => ({ disconnected: '未连接', authorizing: '等待授权', connected: '已连接', error: '连接失败' })[status(id)];

  const navigate = (next: Page, options: { compose?: boolean; replace?: boolean } = {}) => {
    if (next !== page && !options.replace) history.current.push(page);
    focusComposer.current = !!options.compose;
    setPage(next); setOpen(true);
    if (options.compose && next === page) requestAnimationFrame(() => input.current?.focus());
  };
  const home = () => { history.current = []; navigate('dashboard', { replace: true }); };
  const back = () => {
    if (history.current.length) navigate(history.current.pop()!, { replace: true });
    else if (page !== 'dashboard') home();
    else setOpen(false);
  };
  const showPlugin = (id: string) => { setSelected(id); navigate('plugin'); };
  const append = (who: Message['who'], text: string, pluginId?: string) => {
    const message: Message = { id: nextId.current++, who, text, plugin: pluginId };
    setMessages(previous => [...previous, message]);
  };
  const announce = (text: string) => setNotice(text);
  const startConnection = (demoToken = '') => {
    if (pending || plugin.unavailable || connection === 'connected') return;
    if (plugin.auth === 'token' && !demoToken.trim()) { setConnectionError('请先填写任意演示令牌，再重新连接。'); setConnections(previous => ({ ...previous, [selected]: 'error' })); return; }
    setConnectionError('');
    setConnections(previous => ({ ...previous, [selected]: 'authorizing' }));
    setPending({ plugin: selected, origin: connectionOrigins.current[selected] === turn.current ? turn.current : null });
    announce(`${plugin.name} ${plugin.auth === 'skill' ? '正在启用' : '等待模拟授权'}，可以切换到其他页面`);
  };
  const completeConnection = (success: boolean) => {
    if (!pending) return;
    const target = plugins.find(item => item.id === pending.plugin)!;
    setConnections(previous => ({ ...previous, [target.id]: success ? 'connected' : 'error' }));
    setPending(null);
    if (!success) { setConnectionError('模拟授权未完成，请重新连接。'); announce(`${target.name} 连接失败，可从插件详情重试`); return; }
    const continues = pending.origin !== null && pending.origin === turn.current;
    delete connectionOrigins.current[target.id];
    append('jarvis', `${target.name} 已连接。${continues ? '现在继续刚才的任务。' : '可以在对话中使用这个插件了。'}`, target.id);
    announce(`${target.name} 已连接${continues ? '，已继续对话' : ''}`);
    if (page === 'plugin' && selected === target.id) navigate('conversation');
  };
  const cancelConnection = () => {
    if (!pending) return;
    setConnections(previous => ({ ...previous, [pending.plugin]: 'disconnected' }));
    setPending(null); announce('连接已取消，可以重新开始');
  };
  const stopReply = () => {
    sequence.current++;
    if (replyTimer.current) clearTimeout(replyTimer.current);
    setProcessing(false); append('jarvis', '这次回复已停止。你可以继续输入。');
  };
  const send = () => {
    if (!draft.trim() || processing) return;
    const text = draft.trim(); turn.current++; const current = ++sequence.current;
    append('you', text); setDraft(''); setProcessing(true); stickToBottom.current = true;
    const target = plugins.find(item => text.toLowerCase().includes(item.name.toLowerCase()) && /连接|接入|启用/.test(text));
    replyTimer.current = setTimeout(() => {
      if (sequence.current !== current) return;
      setProcessing(false);
      if (target && status(target.id) !== 'connected') {
        connectionOrigins.current[target.id] = turn.current;
        append('jarvis', `可以，${target.name} 的连接入口在这里。`, target.id);
        // A delayed reply must not move someone out of the page they chose.
        if (currentPage.current === 'conversation' && panelOpen.current) showPlugin(target.id);
      } else if (/长|很多|详细/.test(text)) {
        append('jarvis', '这是长回复演示，用来检查滚动与输入框的位置。\n\n' + Array.from({ length: 10 }, (_, index) => `${index + 1}. 查看当前任务的进度，确认下一步安排。你可以滚动这段内容，底部输入框会一直留在原位。`).join('\n\n'));
      } else {
        append('jarvis', '收到。这是本地交互预览，我会保留这段对话。你可以继续输入、切换 Dashboard，或试试「连接 Notion」。');
      }
    }, 850);
  };
  const currentPage = useRef(page); currentPage.current = page;
  const panelOpen = useRef(open); panelOpen.current = open;
  const showVoice = (who: 'you' | 'jarvis') => {
    setVoice(true); setMuted(false);
    const text = who === 'you' ? '我想再连接一个插件，帮我打开 Notion。' : 'Linear 已连接，我来看看分配给你的任务。';
    setCaption({ who, text }); append(who, text);
    if (captionTimer.current) clearTimeout(captionTimer.current);
    captionTimer.current = setTimeout(() => setCaption(null), 7000);
  };
  const endVoice = () => { setVoice(false); setCaption(null); if (captionTimer.current) clearTimeout(captionTimer.current); };
  const reset = () => {
    sequence.current++; if (replyTimer.current) clearTimeout(replyTimer.current);
    endVoice(); setProcessing(false); setPending(null); setConnections({ linear: 'connected' });
    setMessages(initialMessages); setDraft(''); setQuery(''); setFilter('all'); setConnectionError('');
    setApprovals({}); setSelected('linear'); setQuotaLayout('accordion'); setOpacity(82); setReduced(false); setMuted(false); setSpeakerMuted(false); turn.current = 0; connectionOrigins.current = {}; scrolls.current = {}; stickToBottom.current = true; home(); announce('已恢复预览初始状态');
  };
  useEffect(() => () => { if (replyTimer.current) clearTimeout(replyTimer.current); if (captionTimer.current) clearTimeout(captionTimer.current); }, []);
  useEffect(() => { if (!notice) return; const timer = setTimeout(() => setNotice(''), 4500); return () => clearTimeout(timer); }, [notice]);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = scrolls.current[scrollKey] ?? (page === 'conversation' ? el.scrollHeight : 0);
    if (open) {
      if (focusComposer.current && page === 'conversation') input.current?.focus();
      else heading.current?.focus({ preventScroll: true });
    } else capsuleRoot.current?.querySelector<HTMLButtonElement>('button')?.focus();
  }, [scrollKey, open]);
  useLayoutEffect(() => {
    if (page === 'conversation' && stickToBottom.current && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [messages, processing, page, caption]);
  useLayoutEffect(() => {
    if (!input.current) return;
    input.current.style.height = '24px'; input.current.style.height = `${Math.max(24, Math.min(96, input.current.scrollHeight - 14))}px`;
  }, [draft, page, open]);
  useEffect(() => window.jarvis?.onCommand(command => {
    if (command === 'text') navigate('conversation', { compose: true });
    else if (command === 'dashboard') home();
    else if (command === 'plugins') navigate('plugins');
    else if (command === 'settings') navigate('settings');
  }), [page]);

  const catalogPlugins: CatalogPlugin[] = plugins.map(item => ({
    id: item.id, name: item.name, description: item.description, capabilities: ['Read', 'Write'],
    skill_count: item.auth === 'skill' ? 1 : 0, supported: !item.unavailable, unavailable_reason: item.unavailable ? '此插件需要尚未接入的连接器。' : null,
    enabled: status(item.id) === 'connected', status: status(item.id) === 'connected' ? 'ready' : status(item.id), error: null,
    auth: item.auth === 'token' ? 'token' : item.auth === 'skill' ? 'none' : 'oauth', credential_fields: item.auth === 'token' ? ['演示令牌'] : [],
    credentials_saved: false, approval_mode: approvals[item.id] ?? 'auto',
    tools: Array.from({ length: item.tools }, (_, index) => ({ name: `demo_tool_${index + 1}`, description: '本地预览的演示工具，不会执行真实操作。', read_only: index % 2 === 0, requires_confirmation: index % 2 !== 0 })),
  }));
  const pluginController = {
    snapshot: { plugins: catalogPlugins, request: {
      id: `preview-${selected}`, plugin_id: selected, presentation: 1, purpose: '在对话中使用这个插件', continue_task: connectionOrigins.current[selected] === turn.current,
      state: connection === 'connected' ? 'ready' as const : connection === 'authorizing' ? 'authorizing' as const : connection === 'error' ? 'error' as const : 'offered' as const,
      error: connectionError || '连接没有完成，请重新连接。', resume_status: '',
    } },
    error: null, busy: !!pending && pending.plugin !== selected,
    refresh: async () => { announce('演示插件状态已刷新'); },
    action: async (operation: string, data: Record<string, unknown> = {}) => {
      if (operation === 'connect') startConnection(String((data.credentials as Record<string, string> | undefined)?.['演示令牌'] ?? ''));
      else if (operation === 'cancel') { if (pending?.plugin === selected) cancelConnection(); }
      else if (operation === 'disable') { setConnections(previous => ({ ...previous, [selected]: 'disconnected' })); announce(`${plugin.name} 已在预览中停用`); }
      else if (operation === 'approval') setApprovals(previous => ({ ...previous, [selected]: String(data.mode) }));
      else if (operation === 'reopen') announce('请在左侧选择模拟授权结果');
      return true;
    },
  };

  const matches = plugins.filter(item => `${item.name} ${item.description}`.toLowerCase().includes(query.toLowerCase()) && (filter === 'all' || status(item.id) === 'connected'));
  const row = (item: Plugin) => <button key={item.id} className="wp-plugin-row" onClick={() => showPlugin(item.id)} aria-label={`查看 ${item.name}`}>
    <PluginMark id={item.id}/><span className="wp-row-copy"><strong>{item.name}</strong><small>{status(item.id) === 'connected' ? item.auth === 'skill' ? '技能已启用' : `${item.tools} 个工具` : item.description}</small></span>
    <span className={`wp-row-status ${status(item.id)}`}>{item.unavailable ? '暂不支持' : status(item.id) === 'disconnected' ? '连接' : statusLabel(item.id)}</span><CaretRight size={14}/>
  </button>;
  const connectedMatches = matches.filter(item => status(item.id) === 'connected');
  const availableMatches = matches.filter(item => status(item.id) !== 'connected');
  return <IconContext.Provider value={{ size: 20, weight: 'regular' }}>
    <main className={`workspace-preview ${reduced ? 'wp-reduced' : ''}`} style={{ ...surfaceThemes[activeSurfaceTheme], '--wp-opacity': opacity / 100, '--surface-accent': '#8be4bc', '--dashboard-accent': '#8be4bc', '--theme-color': '#8be4bc' } as CSSProperties & Record<string, string | number>} onKeyDown={event => {
      if (event.key !== 'Escape' || event.repeat) return;
      event.preventDefault(); if (open) back(); else if (voice) endVoice(); else home();
    }}>
      <aside className="wp-lab">
        <h1>Resonance</h1><p className="wp-lab-description">一个空间，按需切换。</p>
        <span className="wp-preview-tag">交互预览 · 模拟数据</span>
        <p className="wp-lab-note">在右侧直接操作。对话、授权与语音均为本地演示。</p>
        <section aria-label="预览情景"><h2>试试这些交互</h2>
          <button onClick={() => { navigate('conversation', { compose: true }); setDraft('连接 Notion，帮我整理项目笔记。'); }}><ChatCircle/>通过对话连接插件<ArrowRight size={14}/></button>
          <button onClick={() => { navigate('conversation', { compose: true }); setDraft('给我一段详细的长回复。'); }}><FileText/>长回复与输入框<ArrowRight size={14}/></button>
          <button onClick={() => { setOpen(false); showVoice('jarvis'); }}><Microphone/>只显示实时字幕<ArrowRight size={14}/></button>
          <button onClick={() => { home(); showVoice('you'); }}><SquaresFour/>语音中查看 Dashboard<ArrowRight size={14}/></button>
        </section>
        <section aria-label="模拟授权结果"><h2>授权结果</h2><p>{pending ? `${plugins.find(item => item.id === pending.plugin)?.name} 正在等待授权` : '开始连接插件后，可以在这里模拟结果。'}</p>
          <div className="wp-outcomes"><button disabled={!pending} onClick={() => completeConnection(true)}><Check size={16}/>成功</button><button disabled={!pending} onClick={() => completeConnection(false)}><X size={16}/>失败</button></div>
        </section>
        <button className="wp-reset" onClick={reset}><ArrowsClockwise size={16}/>重置预览</button>
        <p className="wp-lab-footnote">Esc 返回上一级，再按收起。<br/>切换页面后，草稿与连接进度保留。</p>
      </aside>

      <div className={`wp-stage ${showCaption ? 'wp-has-caption' : ''}`}>
        <div className="wp-capsule-wrap" ref={capsuleRoot}>
          <PresentationCapsule presentation={voice ? 'expanded' : 'collapsed'} presence={muted ? 'muted' : caption?.who === 'you' ? 'listening' : caption ? 'speaking' : 'standby'} color="#8be4bc"
            onActivate={() => showVoice('jarvis')} onCollapse={endVoice} microphoneMuted={muted} speakerMuted={speakerMuted}
            onMicrophoneToggle={() => { setMuted(value => !value); if (!muted) setCaption(null); }} onSpeakerToggle={() => setSpeakerMuted(value => !value)}
            onCompose={() => navigate('conversation', { compose: true })}
            rightControl={<button className="presentation-wing-face" aria-label="打开 Dashboard" onClick={home}><SquaresFour/></button>}/>
        </div>
        {showCaption && <section className="wp-caption" aria-label="实时字幕">
          <header><span>{caption.who === 'you' ? <Microphone size={15}/> : <span className="wp-presence-dot"/>}{caption.who === 'you' ? '你正在说' : 'Jarvis 正在说'}</span>
            <button aria-label={caption.who === 'you' ? '暂停字幕演示' : '停止播报演示'} onClick={() => setCaption(null)}><Stop size={12} weight="fill"/>停止</button></header>
          <button className="wp-caption-text" onClick={() => navigate('conversation')} aria-label="查看完整字幕对话"><span>{caption.text}</span><CaretRight size={18}/></button>
        </section>}
        {open && <section className="wp-surface" aria-label="Resonance 工作区" data-page={page}>
          <header className={`wp-header ${page === 'dashboard' ? 'wp-home-header' : ''}`}>
            {page !== 'dashboard' && <button className="wp-back" onClick={back} aria-label="返回上一级"><ArrowLeft size={16}/><span>{titles[history.current.at(-1) ?? 'dashboard']}</span></button>}
            <h2 ref={heading} tabIndex={-1}>{titles[page]}</h2><button className="wp-icon-button" aria-label="收起工作区" onClick={() => setOpen(false)}><Minus size={18}/></button>
          </header>
          {page === 'plugins' && <div className="wp-plugin-controls"><label className="wp-search"><MagnifyingGlass size={18}/><input aria-label="搜索插件" placeholder="搜索插件" value={query} onChange={event => setQuery(event.target.value)}/>{query && <button aria-label="清空搜索" onClick={() => setQuery('')}><X size={15}/></button>}</label>
            <div className="wp-filters" role="group" aria-label="筛选插件"><button aria-pressed={filter === 'connected'} onClick={() => setFilter('connected')}>已连接 <span>{connected.length}</span></button><button aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>全部</button></div></div>}
          <div ref={scroller} className={`wp-scroll wp-page-${page}`} onScroll={event => {
            const el = event.currentTarget; scrolls.current[scrollKey] = el.scrollTop;
            if (page === 'conversation') stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
          }}>
            {page === 'dashboard' && <>
              <nav className="wp-launchers" aria-label="常用功能">{pages.map(({ page: target, label, Icon }) => <button key={target} className={target === 'plugins' ? 'wp-launcher-featured' : ''} onClick={() => navigate(target, { compose: target === 'conversation' })}><Icon size={25}/><span>{label}</span>{target === 'plugins' && pending && <i className="wp-notification-dot"/>}</button>)}</nav>
              <section className="wp-home-section"><h3>正在进行</h3><button className="wp-task-row" onClick={() => navigate('codex')}><GitBranch size={23}/><span className="wp-row-copy"><strong>Resonance 交互优化</strong><small>Codex · 处理中</small></span><span className="wp-presence-dot"/><CaretRight size={16}/></button></section>
              <div className="wp-summaries"><button onClick={() => navigate('quota')}><span>模型额度</span><strong>Claude <b>62%</b></strong><span className="wp-meter"><i style={{ width: '62%' }}/></span></button><button onClick={() => navigate('plugins')}><span>插件</span><strong><PluginMark id="linear"/>{connected.length} 个已连接<CaretRight size={14}/></strong></button></div>
              <section className="wp-home-section"><h3>需要你</h3><button className="wp-task-row" onClick={() => pending ? showPlugin(pending.plugin) : navigate('conversation', { compose: true })}><FileText size={21}/><span className="wp-row-copy"><strong>{pending ? `${plugins.find(item => item.id === pending.plugin)?.name} 等待授权` : '确认新的交互方式'}</strong><small>{pending ? '连接仍在进行' : '设计讨论'}</small></span><CaretRight size={16}/></button></section>
            </>}
            {page === 'conversation' && <div className="wp-messages" aria-label="对话记录">{messages.map(message => <article key={message.id} className={`wp-message wp-${message.who}`}><header>{message.who === 'you' ? <User size={13} weight="fill"/> : <span className="wp-presence-dot"/>}<span>{message.who === 'you' ? '你' : 'Jarvis'}</span></header><p>{message.text}</p>{message.plugin && <button className="wp-connection-card" onClick={() => showPlugin(message.plugin!)}><PluginMark id={message.plugin}/><span>{plugins.find(item => item.id === message.plugin)?.name} {statusLabel(message.plugin)}</span>{status(message.plugin) === 'connected' && <Check className="wp-mint" size={18}/>}<small>管理</small><CaretRight size={15}/></button>}</article>)}
              {processing && <p className="wp-processing" role="status"><CircleNotch className="wp-spin" size={16}/>正在准备回复…</p>}
              {voice && caption && <div className="wp-inline-caption" role="status">{caption.who === 'you' ? '正在听…' : '正在播报…'}</div>}
            </div>}
            {page === 'plugins' && <>
              {connectedMatches.length > 0 && <section className="wp-plugin-group"><h3>已连接</h3>{connectedMatches.map(row)}</section>}
              {availableMatches.length > 0 && <section className="wp-plugin-group"><h3>可以连接</h3>{availableMatches.map(row)}</section>}
              {!matches.length && <div className="wp-empty"><MagnifyingGlass size={28}/><h3>没有匹配的插件</h3><p>换个关键词，或查看全部插件。</p><button onClick={() => { setQuery(''); setFilter('all'); }}>查看全部</button></div>}
            </>}
            {page === 'plugin' && <PluginPanel controller={pluginController} presentation={`preview-${selected}`} onHide={back} onConversation={() => navigate('conversation', { compose: true })} onCatalog={() => navigate('plugins')} simulation/>}
            <div className="wp-retained-page" hidden={page !== 'codex'}><CodexDetail board={board}/></div>
            <div className="wp-retained-page" hidden={page !== 'quota'}><QuotaDetail usage={demoUsage} onRefresh={() => announce('演示额度已刷新')} refreshing={false} layout={quotaLayout}/></div>
            {page === 'status' && <><p className="wp-page-intro">当前关注的工作，以及下一步。</p><section className="wp-status-content"><h3>Jarvis 交互优化</h3><p>正在确认统一工作区的使用方式。</p><h4>下一步</h4><ul><li>试试在多个页面之间切换</li><li>授权等待中返回对话</li><li>检查长内容和字幕的位置</li></ul><button className="wp-secondary" onClick={() => { navigate('conversation', { compose: true }); setDraft('我们继续讨论 Jarvis 的交互方式。'); }}>继续讨论<ArrowRight size={16}/></button></section></>}
            {page === 'settings' && <><p className="wp-page-intro">只影响这个交互预览。</p><label className="wp-setting"><span>额度页面布局</span><select aria-label="额度页面布局" value={quotaLayout} onChange={event => setQuotaLayout(event.target.value as typeof quotaLayout)}><option value="category">轻量标签 · 按类别</option><option value="provider">服务商切换 · 按服务商</option><option value="accordion">B2 · 紧凑折叠</option></select></label><label className="wp-setting"><span>玻璃不透明度<output>{opacity}%</output></span><input aria-label="玻璃不透明度" type="range" min="65" max="100" value={opacity} onChange={event => setOpacity(Number(event.target.value))}/></label><label className="wp-switch-row"><span>减少动态效果</span><input type="checkbox" checked={reduced} onChange={event => setReduced(event.target.checked)}/></label><button className="wp-secondary" onClick={() => { setOpacity(82); setReduced(false); announce('已恢复预览外观'); }}>恢复默认外观</button></>}
          </div>
          {page === 'dashboard' && <footer className="wp-footer"><button className="wp-continue" onClick={() => navigate('conversation', { compose: true })}><ChatCircle size={20}/><span>继续对话</span><ArrowRight size={17}/></button></footer>}
          {page === 'conversation' && <form className="wp-composer" onSubmit={event => { event.preventDefault(); send(); }}><div className="wp-composer-field"><textarea ref={input} aria-label="给 Jarvis 发消息" placeholder="和 Jarvis 说点什么…" rows={1} value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(); } }}/>{processing ? <button className="wp-send" type="button" aria-label="停止回复" onClick={stopReply}><Stop size={17} weight="fill"/></button> : <button className="wp-send" type="submit" aria-label="发送消息" disabled={!draft.trim()}><ArrowUp size={23}/></button>}</div></form>}
          {page === 'plugins' && <footer className="wp-list-footer">{pending ? '连接仍在进行，可以切换页面' : '连接状态实时更新'}</footer>}
        </section>}
        {!open && !showCaption && <button className="wp-reopen" onClick={() => setOpen(true)}>展开{titles[page]}<CaretRight size={14}/></button>}
        <div className="wp-notice" role="status" aria-live="polite">{notice}</div>
      </div>
    </main>
  </IconContext.Provider>;
}

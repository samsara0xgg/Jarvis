import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowSquareOut, ArrowsClockwise, CaretLeft, CaretRight, Check, GithubLogo, Key, MagnifyingGlass, Plugs, ShieldCheck, X } from '@phosphor-icons/react';
import './plugin-panel.css';

export type Plugin = {
  id: string; name: string; description: string; capabilities: string[]; skill_count: number;
  supported: boolean; unavailable_reason: string | null; enabled: boolean; status: string; error: string | null;
  auth: string; credential_fields: string[]; credentials_saved: boolean; approval_mode: string;
  tools: { name: string; description: string; read_only: boolean; requires_confirmation: boolean }[];
};
export type PluginRequest = {
  id: string; plugin_id: string; presentation: number; purpose: string; continue_task: boolean;
  state: 'offered' | 'connecting' | 'authorizing' | 'ready' | 'error' | 'cancelled';
  error: string | null; resume_status: string;
};
export type PluginSnapshot = { plugins: Plugin[]; request: PluginRequest | null };
const cleanError = (error: unknown) => String(error instanceof Error ? error.message : error).replace(/^Error invoking remote method '[^']+': (?:Error: )?/, '');

export function usePlugins() {
  const [snapshot, setSnapshot] = useState<PluginSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const sequence = useRef(0), applied = useRef(0), mutation = useRef(false);
  const alive = useRef(true);
  const load = useCallback(async () => {
    if (!window.jarvis?.plugins || mutation.current) return;
    const seq = ++sequence.current;
    try {
      const next = await window.jarvis.plugins('read') as PluginSnapshot;
      if (alive.current && seq > applied.current) { applied.current = seq; setSnapshot(next); setError(null); }
    } catch (e) { if (alive.current && seq > applied.current) { applied.current = seq; setError(cleanError(e)); } }
  }, []);
  useEffect(() => {
    alive.current = true; let stopped = false; let timer: ReturnType<typeof setTimeout>;
    const poll = async () => { await load(); if (!stopped) timer = setTimeout(poll, 1500); };
    void poll();
    return () => { stopped = true; alive.current = false; clearTimeout(timer); };
  }, [load]);
  const action = useCallback(async (operation: string, data: Record<string, unknown> = {}) => {
    if (!window.jarvis?.plugins || mutation.current) return false;
    mutation.current = true; setBusy(true); setError(null);
    const seq = ++sequence.current; applied.current = seq;
    try {
      const next = await window.jarvis.plugins(operation, data) as PluginSnapshot;
      if (alive.current) setSnapshot(next);
      return true;
    } catch (e) { if (alive.current) setError(cleanError(e)); return false; }
    finally { mutation.current = false; if (alive.current) setBusy(false); }
  }, []);
  return { snapshot, error, busy, action, refresh: load };
}

function PluginIcon({ plugin, small = false }: { plugin: Plugin; small?: boolean }) {
  return <span className={`plugin-icon ${small ? 'is-small' : ''} plugin-icon-${plugin.id}`} aria-hidden="true">
    {plugin.id === 'github' ? <GithubLogo weight="fill"/> : plugin.id === 'linear' ? <svg viewBox="0 0 32 32"><defs><clipPath id={`linear-${small}`}><circle cx="16" cy="16" r="14"/></clipPath></defs><circle cx="16" cy="16" r="14" fill="currentColor"/><g clipPath={`url(#linear-${small})`} stroke="var(--surface-drag)" strokeWidth="2.3"><path d="M-2 7 25 34M-5 12 20 37M-8 17 15 40M-11 22 10 43"/></g></svg> : plugin.id === 'notion' ? <span className="notion-letter">N</span> : <Plugs/>}
  </span>;
}
const statusText = (p: Plugin) => !p.supported ? '暂不支持' : p.status === 'ready' ? '已连接' : p.status === 'authorizing' ? '等待授权' : p.status === 'connecting' ? '正在连接' : p.status === 'needs_auth' ? '待授权' : p.status === 'error' ? '连接异常' : p.status === 'disabled' ? '已停用' : '未连接';

export function PluginPanel({ controller, onHide, onConversation, presentation }: {
  controller: ReturnType<typeof usePlugins>; onHide: () => void; onConversation: () => void; presentation: string;
}) {
  const { snapshot, error, busy, action, refresh } = controller;
  const [catalog, setCatalog] = useState(true), [query, setQuery] = useState('');
  const [details, setDetails] = useState(false), [credentials, setCredentials] = useState<Record<string, string>>({});
  const request = snapshot?.request;
  const plugin = snapshot?.plugins.find(p => p.id === request?.plugin_id);
  const titleRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    setCatalog(presentation === 'catalog'); setDetails(false); setCredentials({});
    if (presentation !== 'catalog') requestAnimationFrame(() => titleRef.current?.focus({ preventScroll: true }));
  }, [presentation]);
  useEffect(() => { setCredentials({}); }, [request?.id]);
  const command = (operation: string, data: Record<string, unknown> = {}) => action(operation, { request_id: request?.id, ...data });
  const cancel = async () => { if (await command('cancel')) { setCredentials({}); onHide(); } };
  const connect = async () => {
    const values = Object.fromEntries(Object.entries(credentials).filter(([, v]) => v.trim()));
    setCredentials({});
    await command('connect', { credentials: values });
  };
  const connecting = request?.state === 'connecting' || request?.state === 'authorizing';
  const ready = request?.state === 'ready';
  const showCatalog = catalog || !plugin || !request;
  const matches = snapshot?.plugins.filter(p => `${p.name} ${p.description}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())) ?? [];
  const sorted = [...matches].sort((a, b) => Number(b.status === 'ready') - Number(a.status === 'ready') || Number(b.supported) - Number(a.supported) || a.name.localeCompare(b.name));
  return <div className="plugin-panel" aria-busy={busy}>
    {error && <div className="plugin-notice" role="alert"><span>{error}</span><button aria-label="重试插件服务" onClick={() => void refresh()}><ArrowsClockwise/></button></div>}
    {showCatalog ? <>
      <h2 className="plugin-catalog-title">插件</h2>
      <p className="plugin-intro">连接你的应用，继续手上的事。</p>
      <label className="plugin-search"><MagnifyingGlass/><input aria-label="搜索插件" placeholder="搜索插件" value={query} onChange={e => setQuery(e.target.value)}/>{query && <button aria-label="清空插件搜索" onClick={() => setQuery('')}><X/></button>}</label>
      {!snapshot && !error && <p className="plugin-empty" role="status">正在读取插件…</p>}
      {snapshot && !sorted.length && <p className="plugin-empty">{query ? '没有匹配的插件' : '暂无可用插件'}</p>}
      <div className="plugin-catalog" aria-label="插件列表">{sorted.map(p => <button className="plugin-row" key={p.id} disabled={busy} onClick={async () => { if (await action('open', { plugin_id: p.id })) setCatalog(false); }}>
        <PluginIcon plugin={p} small/><span><strong>{p.name}</strong><small>{statusText(p)}</small></span><CaretRight/>
      </button>)}</div>
    </> : <>
      <button className="plugin-back" onClick={() => { setCatalog(true); setCredentials({}); }}><CaretLeft/>所有插件</button>
      {!ready && <div className="plugin-identity"><PluginIcon plugin={plugin}/><h2 ref={titleRef} tabIndex={-1}>{plugin.name}</h2><p>{plugin.description}</p></div>}
      {!plugin.supported ? <p className="plugin-empty">{plugin.unavailable_reason}</p> : ready ? <>
        <div className="plugin-success" role="status"><PluginIcon plugin={plugin} small/><h2 ref={titleRef} tabIndex={-1}>{plugin.name} 已连接</h2><Check weight="bold"/></div>
        <p className="plugin-result-note">{request.resume_status === 'continued' ? '正在继续刚才的任务。' : request.resume_status === 'superseded' ? '对话已更新，没有继续之前的任务。' : request.resume_status === 'failed' ? '连接成功，任务未自动继续。你可以回到对话继续。' : '现在可以在对话中使用这个插件。'}</p>
        <button className="plugin-primary" onClick={onConversation}>回到对话</button>
        <button className="plugin-disclosure" aria-expanded={details} onClick={() => setDetails(v => !v)}>管理 {plugin.name}<CaretRight className={details ? 'is-open' : ''}/></button>
      </> : connecting ? <div className="plugin-progress" role="status" aria-live="polite">
        <span className="plugin-spinner" aria-hidden="true"/>
        <h3>{request.state === 'authorizing' ? '等待你在浏览器中授权' : '正在接入插件'}</h3>
        <p>{request.continue_task ? '完成后会自动继续刚才的任务' : '完成后，这个插件即可在对话中使用'}</p>
        {request.state === 'authorizing' && <button className="plugin-secondary" disabled={busy} onClick={() => void command('reopen')}><ArrowSquareOut/>重新打开授权页面</button>}
        <div className="plugin-progress-actions"><button onClick={onHide}>收起</button><button disabled={busy} onClick={() => void cancel()}>取消连接</button></div>
      </div> : <>
        {request.purpose && <div className="plugin-purpose"><span>本次用途</span><p>{request.purpose}</p></div>}
        {request.state === 'error' && <p className="plugin-error" role="alert">{request.error}</p>}
        {request.state === 'cancelled' && <p className="plugin-result-note">连接已取消。你可以重新开始。</p>}
        <div className="plugin-explanation"><ArrowSquareOut/><span>{plugin.auth === 'oauth' || plugin.auth === 'mixed' ? `前往 ${plugin.name} 完成账号授权` : plugin.credential_fields.length ? '填写此插件使用的访问凭证' : plugin.auth === 'local' ? '启动此插件提供的本地工具' : '启用此插件提供的工具与技能'}</span></div>
        <div className="plugin-explanation"><ShieldCheck/><span>{plugin.auth === 'oauth' || plugin.auth === 'mixed' ? '访问范围在授权页面确认' : '后续操作仍遵循你的审批设置'}</span></div>
        {plugin.credential_fields.length > 0 && <form className="plugin-credentials" onSubmit={e => { e.preventDefault(); void connect(); }}>
          {plugin.credential_fields.map(field => <label key={field}><span><Key/>{plugin.credential_fields.length === 1 ? '访问令牌' : field}</span><input type="password" autoComplete="off" aria-label={field} value={credentials[field] ?? ''} placeholder={plugin.credentials_saved ? '已有凭证；留空沿用' : '输入访问令牌'} onChange={e => setCredentials(v => ({ ...v, [field]: e.target.value }))}/></label>)}
          <p>凭证仅保存在本机，不会发送到对话。</p>
        </form>}
        <button className="plugin-disclosure" aria-expanded={details} onClick={() => setDetails(v => !v)}>查看插件能力<CaretRight className={details ? 'is-open' : ''}/></button>
        <button className="plugin-primary" disabled={busy || !!error} onClick={() => void connect()}>{request.state === 'error' ? '重新连接' : request.continue_task ? '连接并继续' : plugin.auth === 'none' ? '启用' : '连接'}</button>
        <button className="plugin-defer" disabled={busy} onClick={() => void cancel()}>暂时不用</button>
      </>}
      {details && <div className="plugin-details">
        <p>{plugin.tools.length ? `${plugin.tools.length} 个工具` : '连接后可查看实际可用工具'}{plugin.skill_count > 0 ? ` · ${plugin.skill_count} 项技能` : ''}</p>
        {plugin.capabilities.length > 0 && <p>{plugin.capabilities.map(c => ({ Read: '读取', Write: '写入', Interactive: '交互' })[c] ?? c).join(' · ')}</p>}
        {ready && <><label className="plugin-approval">默认操作审批<select aria-label="插件操作审批" disabled={busy} value={plugin.approval_mode} onChange={e => void command('approval', { mode: e.target.value })}>{plugin.approval_mode === 'configured' && <option value="configured" disabled>沿用各服务配置</option>}<option value="auto">自动判断（默认）</option><option value="prompt">每次询问</option><option value="writes">写入前询问</option><option value="approve">默认不再询问</option></select></label><p className="plugin-detail-note">单个工具的独立审批设置优先。</p><button className="plugin-disable" disabled={busy} onClick={() => void command('disable')}>停用 {plugin.name}</button><p className="plugin-detail-note">停用会移除工具；已授予的账号权限可在对应服务中撤销。</p></>}
        <div className="plugin-tool-list">{plugin.tools.map(t => <details key={t.name}><summary>{t.name.replace(/^mcp__[^_]+__/, '')}<small>{t.requires_confirmation ? '需确认' : t.read_only ? '读取' : '按设置执行'}</small></summary><p>{t.description}</p></details>)}</div>
      </div>}
    </>}
  </div>;
}

// Settings, a sheet inside the window (one window: none of its own for settings): 钥匙, 体检, 通知, 项目, then the pages
// other features add. ⌘, and the app menu's 设置… open it. When the host is not ready yet (Claude has no way to sign
// in, or there is no project), the first run walks 钥匙 → 体检 → 项目 one page at a time, each skippable, and ends in the
// window on a new session in the folder picked.
import type { Auth, Doctor, Project, Settings } from '../../electron/agents/types';
import type { Feature, PageCtx } from './ctx';
import './settings.css';
import { plural, tr } from './lang';

// A page another feature adds to the sheet (the MCP list joins here): drawn into the page's body when it is opened.
export type SettingsPage = { id: string; label: string; draw(el: HTMLElement): void };
export const settingsPages: SettingsPage[] = [];
// Pages the review holds do not appear, whoever adds them: 打开方式, 仓库, 语言, 手机, 定时.
const HELD = /^(open|opener|repo|repos|lang|language|phone|mobile|timer|schedule)$/;
const HELD_LABEL = /^(打开方式|仓库|语言|手机|定时|open with|repos?|repositories|language|phone|schedule|timer)/i;
// The sheet opened from elsewhere (接手 needs a key first): a page, and a card on it to light up.
let opener: ((page?: string, flash?: string) => void) | null = null;
export const showSettings = (page?: string, flash?: string) => opener?.(page, flash);

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const anim = (el: Element, kf: Keyframe[], ms: number, easing = 'cubic-bezier(.23,1,.32,1)') => reduced.matches ? null : el.animate(kf, { duration: ms, easing });
const H = new WeakMap<Element, string>();
const patch = (el: Element, html: string) => { if (H.get(el) === html) return false; el.innerHTML = html; H.set(el, html); return true; };
const home = (p: string) => p.replace(/^\/Users\/[^/]+/, '~');
const svg = (d: string, w = 13) => `<svg viewBox="0 0 16 16" width="${w}" height="${w}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const I = {
  x: svg('<path d="m4 4 8 8M12 4l-8 8"/>', 12),
  plus: svg('<path d="M8 3.5v9M3.5 8h9"/>', 12),
  dir: svg('<path d="M2 4.5c0-.6.4-1 1-1h3.2l1.3 1.4H13c.6 0 1 .4 1 1v6.1c0 .6-.4 1-1 1H3c-.6 0-1-.4-1-1z"/>', 15),
};
const SPIN = '<span class="fr-spin" aria-hidden="true"></span>';
const store = {
  get(k: string) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k: string, v: string) { try { localStorage.setItem(k, v); } catch { /* not remembered, that is all */ } },
};
const PAGES: [string, string][] = [['key', tr('钥匙', 'Keys')], ['doc', tr('体检', 'Health check')], ['notify', tr('通知', 'Notifications')], ['proj', tr('项目', 'Projects')]];
const STEPS: [string, string, string][] = [[tr('钥匙', 'Keys'), tr('钥匙', 'Keys'), tr('填好一个就能开始，以后在设置里还能改。', 'Fill in one to get started; you can change it later in Settings.')], [tr('体检', 'Health check'), tr('体检', 'Health check'), tr('看看这台 Mac 上还缺什么。', 'See what this Mac is still missing.')], [tr('项目', 'Project'), tr('选一个项目文件夹', 'Choose a project folder'), tr('第一个会话在这个文件夹里开。', 'Your first session opens in this folder.')]];
const NOTIFY0: NonNullable<Settings['notify']> = { done: false, wait: true, err: true };
type Row = { k: string; name: string; v: 'ok' | 'bad' | 'may' | 'wait'; say: string; at?: string; plain?: boolean; fix?: [string, string] };

export function mountSettings(ctx: PageCtx): Feature {
  const { win } = ctx;
  const S = {
    open: false, page: 'key', settings: {} as Settings, auth: null as Auth | null, doc: null as Doctor | null, checking: false, list: [] as Project[],
    key: '', keySt: '', keyErr: '', verified: true, signing: false, howed: new Set<string>(), paths: false,
    fl: '', flAt: 0, fk: '', fkAt: 0, first: null as null | { step: number; proj: string },
  };
  const ext = () => settingsPages.filter(p => p?.id && !HELD.test(p.id) && !HELD_LABEL.test(p.label ?? '') && !PAGES.some(([k]) => k === p.id));
  const labelOf = (id: string) => PAGES.find(([k]) => k === id)?.[1] ?? ext().find(p => p.id === id)?.label ?? '';

  // ---------- what the host says ----------
  const took = (r: { settings?: Settings; auth?: Auth } | null) => { if (r?.settings) S.settings = r.settings; if (r?.auth) S.auth = r.auth; };
  async function loadSettings() { took(await ctx.call<{ settings: Settings; auth: Auth }>('/settings').catch(() => null)); }
  async function loadProjects() { const r = await ctx.call<{ list: Project[] }>('/projects').catch(() => null); if (r) S.list = r.list; }
  let docTok = 0;
  async function recheck() {
    const tok = ++docTok;
    S.checking = true; redraw();
    const d = await ctx.call<Doctor>('/doctor').catch(() => null);
    if (tok !== docTok) return;
    S.checking = false;
    if (d) { S.doc = d; S.auth = d.claude.auth; if (d.codex.account) S.signing = false; }
    redraw();
    if (d && S.page === 'doc' && !rows().some(r => r.v === 'bad')) ctx.cue('done', .6);
  }
  // What was changed is saved at once; the header says so for a moment.
  let savedT = 0;
  function saved() {
    const el = $('.fr-sv'); if (!el) return;
    el.classList.add('on'); clearTimeout(savedT); savedT = window.setTimeout(() => el.classList.remove('on'), 1500);
  }
  async function save(p: Partial<Settings>) {
    const r = await ctx.tryCall('/settings', p) as { settings?: Settings; auth?: Auth } | null;
    if (!r) return false;
    took(r); saved(); return true;
  }

  // ---------- 体检: one row each for Claude Code, Codex, git and the daemon ----------
  function rows(): Row[] {
    const d = S.doc; if (!d) return [];
    const a = d.claude.auth, how = !a.packaged ? tr('用这台 Mac 上的登录', 'Uses the login on this Mac') : a.provider === 'bedrock' ? `Amazon Bedrock · ${S.settings.bedrock?.region ?? ''}`
      : a.provider === 'vertex' ? `Google Vertex · ${S.settings.vertex?.region ?? ''}` : tr(`钥匙 ${a.hint ?? ''}`, `Key ${a.hint ?? ''}`);
    const cv = (d.claude.version ?? '').replace(/\s*\(Claude Code\)/, ''), cx = d.codex;
    const claude: Row = !d.claude.exe ? { k: 'claude', name: 'Claude Code', v: 'bad', say: tr('没找到 claude', 'claude not found'), fix: ['how-claude', tr('怎么装', 'How to install')] }
      : !a.ready ? { k: 'claude', name: 'Claude Code', v: 'bad', say: a.why ?? tr('还不能开会话', 'Cannot start sessions yet'), at: home(d.claude.exe), fix: ['key', tr('填 key', 'Add key')] }
      : { k: 'claude', name: 'Claude Code', v: 'ok', say: [cv, d.claude.own ? tr('你装的', 'Installed by you') : tr('Startrail 带的', 'Bundled with Startrail'), how].filter(Boolean).join(' · '), at: home(d.claude.exe) };
    const codex: Row = !cx.found ? { k: 'codex', name: 'Codex', v: 'may', say: tr('没找到 codex · 不用 Codex 可以不管', 'codex not found · ignore if you do not use Codex'), at: tr(`PATH 里 ${d.path.length} 个地方都没有`, `Not in ${plural(d.path.length, 'PATH entry', 'PATH entries')}`), plain: true, fix: S.howed.has('codex') ? ['refind', tr('再找一次', 'Search again')] : ['how-codex', tr('怎么装', 'How to install')] }
      : S.signing && !cx.account ? { k: 'codex', name: 'Codex', v: 'wait', say: tr('在浏览器里登录…', 'Waiting for sign-in in the browser…'), at: home(cx.path ?? '') }
      : cx.error ? { k: 'codex', name: 'Codex', v: 'bad', say: cx.error, at: home(cx.path ?? ''), fix: ['refind', tr('再查一次', 'Check again')] }
      : !cx.account ? { k: 'codex', name: 'Codex', v: 'bad', say: tr('没登录', 'Not signed in'), at: [home(cx.path ?? ''), (cx.version ?? '').replace(/^codex-cli\s*/, '')].filter(Boolean).join(' · '), fix: ['login', tr('登录', 'Sign in')] }
      : { k: 'codex', name: 'Codex', v: 'ok', say: [(cx.version ?? '').replace(/^codex-cli\s*/, ''), 'ChatGPT', cx.account.email].filter(Boolean).join(' · '), at: home(cx.path ?? '') };
    const git: Row = d.git.found ? { k: 'git', name: 'git', v: 'ok', say: (d.git.version ?? '').replace(/^git version\s*/, '') || tr('有', 'Found') }
      : { k: 'git', name: 'git', v: 'bad', say: tr('没找到 git', 'git not found'), fix: ['how-git', tr('怎么装', 'How to install')] };
    const daemon: Row = d.daemon.up ? { k: 'daemon', name: tr('Jarvis 后台', 'Jarvis daemon'), v: 'ok', say: tr('开着', 'Running') } : { k: 'daemon', name: tr('Jarvis 后台', 'Jarvis daemon'), v: 'may', say: tr('没开 · 没开也能用', 'Not running · works without it'), at: tr('用量环和终端里的要批，等它开了才有', 'The usage ring and Terminal approvals appear once it is running'), plain: true };
    return [claude, codex, git, daemon];
  }
  // A path and what follows it: the path is what gets cut, never the separator.
  const atHTML = (at: string) => { const [p0, ...rest] = at.split(' · '); return `<span>${esc(p0)}</span>${rest.length ? `<i>· ${esc(rest.join(' · '))}</i>` : ''}`; };
  function docHTML() {
    if (!S.doc) return `<div class="fr-dl"><p class="fr-sl fr-dl-wait">${SPIN}${tr('在查', 'Checking')}…</p></div>`;
    return `<div class="fr-dl">${rows().map(r => {
      const q = S.checking, v = q ? 'q' : r.v;
      const mk = q || r.v === 'wait' ? SPIN : r.v === 'ok' ? '✓' : r.v === 'bad' ? '✕' : '○';
      const fix = !q && r.fix ? `<button type="button" class="btn sm" data-act="fr-fix" data-x="${r.fix[0]}">${r.fix[1]}</button>` : '<span></span>';
      return `<div class="fr-dr ${v}" data-k="${r.k}"><span class="fr-mk" aria-label="${q ? tr('在查', 'Checking') : { ok: tr('好的', 'OK'), bad: tr('不行', 'Problem'), wait: tr('在登', 'Signing in'), may: tr('可以不管', 'Optional') }[r.v]}">${mk}</span><b>${esc(r.name)}</b>`
        + `<span class="fr-ds"><span>${q ? tr('在查…', 'Checking…') : esc(r.say)}</span>${!q && r.at ? `<small${r.plain ? ' class="np"' : ''}>${atHTML(r.at)}</small>` : ''}</span>${fix}</div>`;
    }).join('')}</div>`;
  }
  function pathHTML() {
    const ps = S.doc?.path ?? []; if (!ps.length) return '';
    const hit = S.doc?.codex.path ? S.doc.codex.path.replace(/\/[^/]+$/, '') : '';
    return `<div class="fr-pa"><span>${tr('找的是你登录 shell 的 PATH：', 'Searched your login shell PATH:')}</span>${(S.paths ? ps : ps.slice(0, 3)).map(p => `<code${p === hit ? ' class="hit"' : ''}>${esc(home(p))}</code>`).join('')}`
      + `${ps.length > 3 ? `<button type="button" class="fr-lk" data-act="fr-paths">${S.paths ? tr('收起', 'Show fewer') : tr(`全部 ${ps.length} 个`, `All ${ps.length}`)}</button>` : ''}</div>`;
  }
  const docFoot = () => `<div class="fr-df"><button type="button" class="btn sm" data-act="fr-recheck"${S.checking ? ' disabled' : ''}>${tr('重新检查', 'Check again')}</button><span>${S.checking || !S.doc ? tr('在查…', 'Checking…') : tr('刚查过', 'Just checked')}</span></div>`;

  // ---------- 钥匙: how Startrail's own Claude sessions sign in (ADR 0094), and Codex's login ----------
  const lit = (on: boolean, at: number, ms: number) => { const t = Math.round(performance.now() - at); return on && t < ms ? ` fl" style="animation-delay:-${t}ms` : ''; };
  const seg = (list: [string, string][], cur: string) => `<span class="fr-sg" role="radiogroup" aria-label="${tr('Claude 用哪家的账户', 'Which account Claude uses')}">${list.map(([k, l]) => `<button type="button" role="radio" data-act="fr-prov" data-v="${k}" aria-checked="${cur === k}">${l}</button>`).join('')}</span>`;
  const provider = () => S.auth?.provider ?? S.settings.provider ?? 'anthropic';
  function claudeTag() { const a = S.auth; return !a ? '' : a.ready ? tr('<span class="fr-tag mint">能开会话</span>', '<span class="fr-tag mint">Ready</span>') : tr('<span class="fr-tag warm">还开不了</span>', '<span class="fr-tag warm">Not ready</span>'); }
  function claudeLine() {
    const a = S.auth; if (!a) return `<p class="fr-sl">${SPIN}${tr('在查', 'Checking')}…</p>`;
    if (!a.packaged) return `<p class="fr-sl mint">✓ ${tr('开发版：用这台 Mac 上 Claude Code 自己的登录', 'Dev build: uses the login Claude Code already has on this Mac')}</p>`;
    const p = provider();
    if (p === 'bedrock') return a.ready ? `<p class="fr-sl mint">✓ ${tr('好了 · 用这台 Mac 上 AWS 的登录', 'Ready · uses the AWS login on this Mac')}${S.settings.bedrock?.profile ? tr(`（${esc(S.settings.bedrock.profile)}）`, ` (${esc(S.settings.bedrock.profile)})`) : ''}</p>` : `<p class="fr-sl warm">${esc(a.why ?? '')}</p>`;
    if (p === 'vertex') return a.ready ? `<p class="fr-sl mint">✓ ${tr('好了 · 用这台 Mac 上 gcloud 的登录', 'Ready · uses the gcloud login on this Mac')}</p>` : `<p class="fr-sl warm">${esc(a.why ?? '')}</p>`;
    if (S.keySt === 'checking') return `<p class="fr-sl">${SPIN}${tr('在问', 'Asking')} Anthropic…<small>${tr('列一次模型，不用 token', 'Lists models once, no tokens')}</small></p>`;
    if (S.keyErr) return `<p class="fr-sl red">${esc(S.keyErr)}${a.hint ? tr(` · 原来的 ${esc(a.hint)} 还在用`, ` · your existing ${esc(a.hint)} is still in use`) : ''}</p>`;
    if (a.hint) return `<p class="fr-sl mint">✓ ${tr('存好了', 'Saved')} · ${esc(a.hint)}${S.verified ? '' : tr('<small>没连上 Anthropic，先存下了</small>', '<small>Could not reach Anthropic, saved anyway</small>')} · <button type="button" class="fr-lk" data-act="fr-forget">${tr('删掉', 'Remove')}</button></p>`;
    return `<p class="fr-sl warm">${esc(a.why ?? tr('先填一个 Anthropic API key', 'Add an Anthropic API key first'))}</p>`;
  }
  const verifyOff = () => !S.key.trim() || S.keySt === 'checking';
  function claudeCard() {
    const a = S.auth, p = provider(), packaged = !!a?.packaged;
    const body = !packaged ? ''
      : seg([['anthropic', 'Anthropic API key'], ['bedrock', 'Amazon Bedrock'], ['vertex', 'Google Vertex']], p) + (p === 'anthropic'
        ? `<div class="fr-kr"><input class="fr-in" type="password" data-f="key" value="${esc(S.key)}" placeholder="${a?.hint ? tr(`已存 ${esc(a.hint)} · 贴一个新的会换掉它`, `Saved ${esc(a.hint)} · paste a new one to replace it`) : 'sk-ant-…'}" aria-label="Anthropic API key" autocomplete="off" spellcheck="false"><button type="button" class="btn sm${a?.hint ? '' : ' warm'}" data-act="fr-verify"${verifyOff() ? ' disabled' : ''}>${tr('验证并存进钥匙串', 'Verify and save to Keychain')}</button></div>`
        : p === 'bedrock'
          ? `<div class="fr-fg"><label class="fr-fr"><span>${tr('区域', 'Region')}</span><input class="fr-in" data-f="bd-region" value="${esc(S.settings.bedrock?.region ?? '')}" placeholder="us-east-1" spellcheck="false"></label><label class="fr-fr"><span>profile</span><input class="fr-in" data-f="bd-profile" value="${esc(S.settings.bedrock?.profile ?? '')}" placeholder="${tr('可以不填', 'Optional')}" spellcheck="false"></label></div>`
          : `<div class="fr-fg"><label class="fr-fr"><span>${tr('区域', 'Region')}</span><input class="fr-in" data-f="vx-region" value="${esc(S.settings.vertex?.region ?? '')}" placeholder="us-east5" spellcheck="false"></label><label class="fr-fr"><span>${tr('项目', 'Project')}</span><input class="fr-in" data-f="vx-project" value="${esc(S.settings.vertex?.project ?? '')}" placeholder="GCP ${tr('项目', 'project')} ID" spellcheck="false"></label></div>`);
    return `<div class="fr-cd${lit(S.fl === 'claude', S.flAt, 1800)}" data-card="claude"><div class="fr-ch"><b>Claude Code</b><span data-slot="tag">${claudeTag()}</span></div>${body}`
      + `<div data-slot="line">${claudeLine()}</div><p class="fr-fn">${packaged ? tr('只给 Startrail 自己开的 Claude 会话用；你在终端里跑的 claude 还是它自己的登录。', 'Only for Claude sessions Startrail starts itself; claude in your terminal keeps its own login.') : tr('装好的 Startrail 在这里填 key；开发版不存 key。', 'The installed Startrail takes a key here; the dev build does not store keys.')}</p></div>`;
  }
  function codexCard() {
    const cx = S.doc?.codex;
    const tag = !cx ? tr('<span class="fr-tag">在查</span>', '<span class="fr-tag">Checking</span>') : !cx.found ? tr('<span class="fr-tag">没装</span>', '<span class="fr-tag">Not installed</span>') : cx.account ? tr('<span class="fr-tag mint">已登录</span>', '<span class="fr-tag mint">Signed in</span>') : tr('<span class="fr-tag warm">没登录</span>', '<span class="fr-tag warm">Not signed in</span>');
    const body = !cx ? `<p class="fr-sl">${SPIN}${tr('在查', 'Checking')}…</p>`
      : !cx.found ? `<div class="fr-kr"><span class="fr-sl">${tr('没找到 codex · 不用 Codex 可以不管', 'codex not found · ignore if you do not use Codex')}</span><span class="fr-gap"></span><button type="button" class="btn sm" data-act="fr-fix" data-x="how-codex">${tr('怎么装', 'How to install')}</button><button type="button" class="btn sm" data-act="fr-fix" data-x="refind">${tr('再找一次', 'Search again')}</button></div>`
      : cx.account ? `<p class="fr-sl mint">✓ ${tr('已登录', 'Signed in')} · ChatGPT${cx.account.email ? ` · ${esc(cx.account.email)}` : ''}</p>`
      : S.signing ? `<p class="fr-sl">${SPIN}${tr('在浏览器里登录', 'Waiting for sign-in in the browser')}…<small>${tr('登好了这里自己会变', 'This updates by itself once you are done')}</small></p>`
      : cx.error ? `<div class="fr-kr"><span class="fr-sl red">${esc(cx.error)}</span><span class="fr-gap"></span><button type="button" class="btn sm" data-act="fr-fix" data-x="refind">${tr('再查一次', 'Check again')}</button></div>`
      : `<div class="fr-kr"><button type="button" class="btn sm warm" data-act="fr-fix" data-x="login">${tr('用 ChatGPT 账号登录', 'Sign in with ChatGPT')}</button></div>`;
    return `<div class="fr-cd${lit(S.fl === 'codex', S.flAt, 1800)}" data-card="codex"><div class="fr-ch"><b>Codex</b>${tag}</div>${body}<p class="fr-fn">${tr('和你终端里的 codex 是同一个登录。', 'The same login as codex in your terminal.')}</p></div>`;
  }

  // ---------- 通知 and 项目 ----------
  const notify = () => S.settings.notify ?? NOTIFY0;
  function notifyHTML() {
    const n = notify(), sw = (k: 'done' | 'wait' | 'err', l: string, d: string) => `<div class="fr-tr"><span class="fr-tl"><b>${l}</b><small>${d}</small></span>`
      + `<button type="button" class="fr-sw" role="switch" data-act="fr-sw" data-k="${k}" aria-checked="${n[k]}" aria-label="${l}"><i></i></button></div>`;
    const notch = n.notch !== false;
    return `<div class="fr-cd fr-tg0">${sw('done', tr('做完了', 'Done'), n.done ? tr('做完了也弹一条', 'Also notify when a session finishes') : tr('不弹：星星落到她旁边，等你回来看', 'Off: a star lands beside her, waiting for you to come back'))}${sw('wait', tr('等你批准或回答', 'Needs your approval or answer'), tr('点它直接到那个会话', 'Click it to go straight to that session'))}${sw('err', tr('出错了', 'Error'), tr('点它直接到那个会话', 'Click it to go straight to that session'))}</div>`
      + `<p class="fr-fn">${tr('窗口在前台时不弹；在后面时弹出来不出声。同一个会话 20 秒内只弹一条。', 'Nothing shows while the window is in front; behind it, notifications arrive silently. One per session every 20 seconds.')}</p>`
      // The notch follows the host only in the dev build (companion.ts), so only there is it a choice.
      + (S.auth?.packaged ? '' : `<div class="fr-cd fr-tg0"><div class="fr-tr"><span class="fr-tl"><b>${tr('Jarvis 开着时用刘海说', 'Use the notch when Jarvis is running')}</b><small>${notch ? tr('要批的、要回答的从刘海垂下来就地回答，不弹系统通知', 'Approvals and questions drop from the notch to answer in place, with no system notification') : tr('不用刘海，照上面弹系统通知', 'Notch off: system notifications as above')}</small></span>`
      + `<button type="button" class="fr-sw" role="switch" data-act="fr-sw" data-k="notch" aria-checked="${notch}" aria-label="${tr('Jarvis 开着时用刘海说', 'Use the notch when Jarvis is running')}"><i></i></button></div></div>`)
      + `<div class="fr-kr"><button type="button" class="btn sm" data-act="fr-test">${tr('发一条试试', 'Send a test')}</button></div>`;
  }
  function whenUsed(at: number) {
    const d = new Date(at), today = new Date(); today.setHours(0, 0, 0, 0);
    if (Date.now() - at < 3600e3) return tr('刚用过', 'Just used');
    if (at >= today.getTime()) return tr('今天', 'Today');
    const days = Math.ceil((today.getTime() - d.getTime()) / 864e5);
    return days <= 1 ? tr('昨天', 'Yesterday') : tr(`${days} 天前`, `${days} d ago`);
  }
  function projHTML() {
    const used = S.list.filter(p => p.used), added = S.list.filter(p => !p.used && p.added), scan = S.list.filter(p => !p.used && !p.added);
    const row = (p: Project, x: boolean) => `<div class="fr-pj${lit(S.fk === p.path, S.fkAt, 1600)}" data-path="${esc(p.path)}"><span class="fr-pi">${I.dir}</span><b>${esc(p.name)}</b><code>${esc(home(p.path))}</code>`
      + `<span class="fr-pm">${p.used ? `<small>${whenUsed(p.used)}</small>` : ''}${p.git ? '' : tr('<span class="fr-tag">不是 git</span>', '<span class="fr-tag">Not a git repo</span>')}</span>`
      + (x ? `<button type="button" class="ib fr-px" data-act="fr-unadd" data-path="${esc(p.path)}" aria-label="${tr('从列表里拿掉', 'Remove')} ${esc(p.name)}${tr('', ' from the list')}" data-tip="${tr('拿掉', 'Remove')}">${I.x}</button>` : '<span class="fr-px"></span>') + '</div>';
    const sec = (t: string, ps: Project[], x = false) => ps.length ? `<p class="fr-h4">${t}</p>${ps.map(p => row(p, x)).join('')}` : '';
    return `<div class="fr-pl0">${sec(tr('最近用过', 'Recently used'), used)}${sec(tr('你加的', 'Added by you'), added, true)}${sec(tr('~/Projects 里的', 'In ~/Projects'), scan)}${S.list.length ? '' : tr('<p class="fr-fn">还没有项目：加一个文件夹。</p>', '<p class="fr-fn">No projects yet: add a folder.</p>')}</div>`
      + `<div class="fr-kr"><button type="button" class="btn sm" data-act="fr-add">${I.plus}${tr('添加文件夹', 'Add folder')}</button><span class="fr-fn">${tr('拿掉只是不列在这里，文件夹本身不动。', 'Removing only unlists it here; the folder itself is untouched.')}</span></div>`;
  }
  const PAGE: Record<string, () => string> = { key: () => claudeCard() + codexCard(), doc: () => docHTML() + pathHTML() + docFoot(), notify: notifyHTML, proj: projHTML };

  // ---------- the sheet ----------
  win.insertAdjacentHTML('beforeend', `<div class="fr-veil" hidden></div><section class="sheet fr-set" hidden role="dialog" aria-modal="true" aria-label="${tr('设置', 'Settings')}" tabindex="-1">`
    + `<nav class="fr-nav" aria-label="${tr('设置的几页', 'Settings pages')}"></nav><div class="fr-pg"><header class="fr-ph"></header><div class="fr-pb"></div></div></section>`
    + `<section class="fr-first" hidden role="dialog" aria-modal="true" aria-label="${tr('第一次打开', 'First run')}" tabindex="-1"><div class="fr-fl"><div class="fr-fh"><div class="fr-ft">${STEPS.map(([l], j) =>
      `<button type="button" class="fr-fd" data-act="fr-fdot" data-k="${j}" style="left:${j * 50}%"><i></i><small>${l}</small></button>`).join('')}<span class="fr-fx"></span></div></div><div class="fr-fb"></div><div class="fr-ff"></div></div></section>`);
  const veil = win.querySelector<HTMLElement>('.fr-veil')!, sheet = win.querySelector<HTMLElement>('.fr-set')!, nav = sheet.querySelector<HTMLElement>('.fr-nav')!;
  const head = sheet.querySelector<HTMLElement>('.fr-ph')!, body = sheet.querySelector<HTMLElement>('.fr-pb')!;
  const first = win.querySelector<HTMLElement>('.fr-first')!, fBody = first.querySelector<HTMLElement>('.fr-fb')!, fFoot = first.querySelector<HTMLElement>('.fr-ff')!;
  function $(sel: string) { return (S.first ? first : sheet).querySelector<HTMLElement>(sel); }
  function navHTML() {
    const a = S.auth, bad = S.checking ? 0 : rows().filter(r => r.v === 'bad').length;
    const mark: Record<string, string> = { key: (a?.packaged && !a.ready) || (S.doc?.codex.found && !S.doc.codex.account && !S.doc.codex.error) ? tr('<em class="fr-nd" aria-label="有要填的"></em>', '<em class="fr-nd" aria-label="Needs setup"></em>') : '',
      doc: bad ? tr(`<em class="fr-nc" aria-label="${bad} 样不行">${bad}</em>`, `<em class="fr-nc" aria-label="${plural(bad, 'problem')}">${bad}</em>`) : '' };
    const item = (id: string, l: string) => `<button type="button" class="fr-ni" data-act="fr-pg" data-p="${esc(id)}"${S.page === id ? ' aria-current="page"' : ''}>${esc(l)}${mark[id] ?? ''}</button>`;
    const more = ext();
    return `<b class="fr-nh">${tr('设置', 'Settings')}</b>${PAGES.map(([id, l]) => item(id, l)).join('')}${more.length ? `<i class="fr-nsep"></i>${more.map(p => item(p.id, p.label)).join('')}` : ''}`;
  }
  // A redraw keeps the field or button you were on, and the caret in it.
  function focusKey(root: HTMLElement) {
    const ae = document.activeElement as HTMLInputElement | null;
    if (!ae || !root.contains(ae) || ae === root) return null;
    const d = ae.dataset, k = d.f ? `[data-f="${d.f}"]` : d.act ? `[data-act="${d.act}"]${['v', 'k', 'p', 'x', 'path'].map(n => d[n] ? `[data-${n}="${CSS.escape(d[n]!)}"]` : '').join('')}` : null;
    return k ? { k, s: ae.selectionStart, e: ae.selectionEnd, card: ae.closest<HTMLElement>('[data-card]')?.dataset.card } : null;
  }
  function refocus(root: HTMLElement, o: ReturnType<typeof focusKey>) {
    if (!o) return;
    const el = root.querySelector<HTMLInputElement>(o.k);
    if (el && !el.disabled) {
      if (el !== document.activeElement) el.focus({ preventScroll: true });
      if (o.s != null && el.setSelectionRange) try { el.setSelectionRange(o.s, o.e); } catch { /* not a text field */ }
      return;
    }
    if (root.contains(document.activeElement) && document.activeElement !== root) return;
    const card = o.card ? root.querySelector(`[data-card="${o.card}"]`) : null;
    (card?.querySelector<HTMLElement>('input:not([disabled]),button:not([disabled])') ?? root.querySelector<HTMLElement>('.fr-ni[aria-current="page"],.fr-ff .btn.warm:not([disabled])') ?? root).focus({ preventScroll: true });
  }
  let drawn = '';
  function renderSheet() {
    if (!S.open) return;
    const keep = focusKey(sheet), page = ext().find(p => p.id === S.page), turned = drawn !== S.page;
    patch(nav, navHTML());
    patch(head, `<h3>${esc(labelOf(S.page))}</h3><span class="fr-sv" aria-live="polite">${tr('已存', 'Saved')}</span><button type="button" class="ib" data-act="fr-close" aria-label="${tr('关掉设置', 'Close settings')}" data-tip="${tr('关掉', 'Close')}" data-key="esc">${I.x}</button>`);
    if (page) {
      if (turned) { H.delete(body); body.replaceChildren(); try { page.draw(body); } catch (e) { console.warn('settings page', page.id, e); body.innerHTML = tr('<p class="fr-fn">这一页没画出来。</p>', '<p class="fr-fn">This page failed to load.</p>'); } }
    } else patch(body, PAGE[S.page]());
    if (turned) { body.scrollTop = 0; drawn = S.page; }
    refocus(sheet, keep);
  }
  function renderFirst() {
    const F = S.first; if (!F) return;
    const keep = focusKey(first), i = F.step;
    first.querySelectorAll<HTMLButtonElement>('.fr-fd').forEach((d, j) => {
      d.className = `fr-fd${j < i ? ' done' : j === i ? ' cur' : ''}`; d.disabled = j >= i;
      d.setAttribute('aria-label', tr(`第 ${j + 1} 步 · ${STEPS[j][0]}${j < i ? ' · 做过了，点回去' : j === i ? ' · 现在' : ''}`, `Step ${j + 1} · ${STEPS[j][0]}${j < i ? ' · done, click to go back' : j === i ? ' · current' : ''}`));
    });
    first.querySelector<HTMLElement>('.fr-fx')!.style.width = `${i * 50}%`;
    const list = `<div class="fr-rl" role="radiogroup" aria-label="${tr('项目文件夹', 'Project folder')}">${S.list.map(p => `<button type="button" role="radio" class="fr-rad" data-act="fr-fproj" data-path="${esc(p.path)}" aria-checked="${F.proj === p.path}"><i class="fr-rd"></i><b>${esc(p.name)}</b><small>${esc(home(p.path))}${p.git ? '' : tr(' · 不是 git', ' · not git')}</small></button>`).join('')}</div>`
      + `<button type="button" class="btn sm fr-add" data-act="fr-fadd">${I.plus}${tr('选别的文件夹', 'Choose another folder')}…</button>`;
    patch(fBody, `<h3><small>${tr('第', 'Step')} ${i + 1}${tr(' 步', '')}</small>${STEPS[i][1]}</h3><p class="fr-fs">${STEPS[i][2]}</p><div class="fr-fc">${i === 0 ? claudeCard() + codexCard() : i === 1 ? docHTML() : list}</div>`);
    patch(fFoot, `<button type="button" class="fr-lk" data-act="fr-fskip">${tr('跳过，以后在设置里填', 'Skip, fill in later in Settings')}</button><span class="fr-gap"></span>${i ? `<button type="button" class="btn sm" data-act="fr-fback">${tr('上一步', 'Back')}</button>` : ''}`
      + (i < 2 ? `<button type="button" class="btn sm warm" data-act="fr-fnext">${tr('下一步', 'Next')}</button>` : `<button type="button" class="btn sm warm" data-act="fr-fgo"${F.proj ? '' : ' disabled'}>${tr('开始', 'Start')}</button>`));
    refocus(first, keep);
  }
  const redraw = () => { if (S.first) renderFirst(); else renderSheet(); };
  // While you type: only the lines that follow what you typed, never the field you are in.
  function soft() {
    const root = S.first ? first : sheet;
    if (S.open) patch(nav, navHTML());
    root.querySelectorAll<HTMLElement>('[data-slot]').forEach(el => patch(el, el.dataset.slot === 'tag' ? claudeTag() : claudeLine()));
    const v = root.querySelector<HTMLButtonElement>('[data-act="fr-verify"]'); if (v) v.disabled = verifyOff();
  }

  function openSheet(page?: string, flash?: string) {
    if (S.first) return;
    ctx.closeMenu();
    if (win.classList.contains('sky-on')) win.querySelector<HTMLElement>('.bw-pull')?.click();
    const was = S.open;
    S.open = true;
    if (page) S.page = page;
    if (!labelOf(S.page)) S.page = 'key';
    if (flash) { S.fl = flash; S.flAt = performance.now(); }
    veil.hidden = sheet.hidden = false; win.classList.add('fr-modal');
    drawn = ''; renderSheet();
    if (!was) {
      // The veil is down before the sheet is solid, and the sheet is solid early in its move: nothing reads through it.
      anim(veil, [{ opacity: 0 }, { opacity: 1 }], 140, 'linear');
      anim(sheet, [{ opacity: 0, transform: 'translateY(-10px) scale(.99)' }, { opacity: 1, offset: .4 }, { opacity: 1, transform: 'none' }], 260);
      ctx.cue('open', .6);
      void loadSettings().then(redraw); void loadProjects().then(redraw); void recheck();
    }
    ((flash === 'claude' ? sheet.querySelector<HTMLElement>('[data-f="key"]') : null) ?? sheet.querySelector<HTMLElement>('.fr-ni[aria-current="page"]') ?? sheet).focus({ preventScroll: true });
  }
  opener = openSheet;
  function closeSheet() {
    if (!S.open) return;
    S.open = false; S.key = ''; S.keyErr = ''; win.classList.remove('fr-modal');
    const done = () => { if (!S.open) veil.hidden = sheet.hidden = true; };
    const a = anim(sheet, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-8px) scale(.99)' }], 160, 'cubic-bezier(.4,0,1,1)');
    anim(veil, [{ opacity: 1 }, { opacity: 0 }], 160);
    if (a) a.onfinish = done; else done();
    ctx.cue('close', .6);
    settle();
  }
  // Back to where you were: the composer when it takes text, else the window.
  function settle() { if (!ctx.ta.disabled && ctx.ta.offsetParent) ctx.ta.focus({ preventScroll: true }); else { win.tabIndex = -1; win.focus({ preventScroll: true }); } }
  function goPage(id: string) {
    if (!labelOf(id) || S.page === id) return;
    S.page = id; S.fl = ''; renderSheet(); ctx.tick();
    if (id === 'doc' || id === 'key') void recheck();
    if (id === 'proj') void loadProjects().then(redraw);
  }
  veil.addEventListener('click', () => closeSheet());

  // ---------- doing things ----------
  // POST /settings/key: the host checks the key with Anthropic (a model list, no tokens) before the Keychain keeps it.
  async function verify() {
    const v = S.key.trim(); if (!v || S.keySt === 'checking') return;
    S.keySt = 'checking'; S.keyErr = ''; soft();
    const r = await ctx.call<{ verified?: boolean; settings: Settings; auth: Auth }>('/settings/key', { key: v }).catch((e: unknown) => e instanceof Error ? e.message : String(e));
    S.keySt = '';
    if (typeof r === 'string') { S.keyErr = r; ctx.cue('error', .5); }
    else { took(r); S.verified = r.verified !== false; S.key = ''; ctx.cue('done', .7); const inp = $('[data-f="key"]') as HTMLInputElement | null; if (inp) inp.value = ''; }
    redraw(); if (typeof r !== 'string') saved();
  }
  let signTimer = 0;
  async function fix(x: string) {
    if (x === 'key') {
      if (S.first) { S.first.step = 0; renderFirst(); }
      else { S.page = 'key'; S.fl = 'claude'; S.flAt = performance.now(); renderSheet(); }
      $('[data-f="key"]')?.focus({ preventScroll: true });
      return;
    }
    if (x === 'login') {
      const r = await ctx.tryCall('/doctor/login', { agent: 'codex' });
      if (!r) return;
      if (typeof r.url === 'string' && r.url) void window.agents?.openUrl?.(r.url);
      S.signing = true; redraw(); ctx.toast(tr('在浏览器里打开了 ChatGPT 的登录页', 'Opened the ChatGPT sign-in page in your browser'));
      // Codex hears back from the browser itself; the check-up is read again until it says so, for three minutes.
      const t0 = Date.now();
      clearInterval(signTimer);
      signTimer = window.setInterval(async () => {
        const d = await ctx.call<Doctor>('/doctor').catch(() => null);
        if (d) { S.doc = d; S.auth = d.claude.auth; }
        if (d?.codex.account || Date.now() - t0 > 180e3) { clearInterval(signTimer); S.signing = false; if (d?.codex.account) { ctx.cue('done', .7); ctx.toast(tr(`Codex 登好了${d.codex.account.email ? ` · ${d.codex.account.email}` : ''}`, `Codex is signed in${d.codex.account.email ? ` · ${d.codex.account.email}` : ''}`)); } }
        redraw();
      }, 2000);
      return;
    }
    const how: Record<string, string> = { 'how-codex': 'https://developers.openai.com/codex/cli', 'how-git': 'https://git-scm.com/download/mac', 'how-claude': 'https://docs.anthropic.com/en/docs/claude-code/setup' };
    if (how[x]) { void window.agents?.openUrl?.(how[x]); S.howed.add(x.slice(4)); redraw(); ctx.toast(tr('在浏览器里打开了安装说明', 'Opened the install instructions in your browser')); return; }
    if (x === 'refind') { await recheck(); if (!S.doc?.codex.found) ctx.toast(tr('还是没找到 codex', 'Still cannot find codex'), true); }
  }
  // 发一条试试: the notification a session would send, from one that fits the first kind switched on.
  async function testNote() {
    const n = notify(), k = (['wait', 'err', 'done'] as const).find(x => n[x]);
    if (!k) { ctx.toast(tr('三种都关了，不会弹', 'All three are off, so nothing will show')); return; }
    if (!window.agents?.notifyTest) { ctx.toast(tr('这里弹不了通知', 'Notifications cannot be shown here'), true); return; }
    const ss = ctx.sessions().filter(s => !s.archived), s = ss.find(x => k === 'wait' ? x.st === 'wait' : k === 'err' ? x.st === 'err' : x.st === 'done') ?? ss[0];
    const sub = k === 'wait' ? tr('在等你', 'Waiting on you') : k === 'err' ? tr('出错了', 'Error') : tr('做完了', 'Done');
    const ok = await window.agents?.notifyTest?.(s?.title ?? 'Startrail', sub, s?.summary ?? tr('通知会这样弹出来', 'Notifications will look like this'), s?.id);
    ctx.cue('mark', .6);
    ctx.toast(ok === false ? tr('这台 Mac 不让它弹通知：去系统设置的通知里打开', 'This Mac is blocking its notifications: turn them on in System Settings > Notifications') : tr('发了一条：看屏幕右上角', 'Sent one: look at the top right of the screen'), ok === false);
  }
  async function addFolder(then?: (p: string) => void) {
    const p = await window.agents?.folder();
    if (!p) return;
    const r = await ctx.tryCall('/projects', { path: p }) as { list?: Project[] } | null;
    if (!r) return;
    if (r.list) S.list = r.list;
    const got = S.list.find(x => x.path === p) ?? S.list.find(x => home(x.path) === home(p));
    S.fk = got?.path ?? p; S.fkAt = performance.now();
    ctx.cue('mark', .6); ctx.toast(tr(`加进来了：${home(p)}`, `Added: ${home(p)}`));
    then?.(got?.path ?? p);
    redraw(); saved();
  }
  async function unadd(p: string, row: HTMLElement | null) {
    const r = await ctx.tryCall(`/projects?path=${encodeURIComponent(p)}`, undefined, 'DELETE') as { list?: Project[] } | null;
    if (!r) return;
    ctx.cue('close', .6);
    const done = () => { if (r.list) S.list = r.list; ctx.toast(tr(`从列表里拿掉了 ${home(p)} · 文件夹还在`, `Removed ${home(p)} from the list · the folder is still there`)); redraw(); saved(); };
    const a = row ? anim(row, [{ opacity: 1 }, { opacity: 0, transform: 'translateX(12px)' }], 200) : null;
    if (a) a.onfinish = done; else done();
  }
  function onInput(e: Event) {
    const t = e.target as HTMLInputElement, f = t.dataset?.f;
    if (f === 'key') { S.key = t.value; S.keyErr = ''; soft(); }
  }
  // Bedrock and Vertex fields are kept once you leave them.
  function onChange(e: Event) {
    const t = e.target as HTMLInputElement, f = t.dataset?.f;
    if (!f || f === 'key') return;
    const root = S.first ? first : sheet, val = (k: string) => root.querySelector<HTMLInputElement>(`[data-f="${k}"]`)?.value.trim() ?? '';
    if (f.startsWith('bd-')) void save({ bedrock: { region: val('bd-region'), profile: val('bd-profile') } }).then(soft);
    else void save({ vertex: { region: val('vx-region'), project: val('vx-project') } }).then(soft);
  }
  for (const el of [sheet, first]) { el.addEventListener('input', onInput); el.addEventListener('change', onChange); }

  // ---------- the first run: 钥匙 → 体检 → 项目 ----------
  function openFirst() {
    if (S.open) { S.open = false; veil.hidden = sheet.hidden = true; }
    ctx.closeMenu();
    S.first = { step: 0, proj: '' };
    first.hidden = false; win.classList.add('fr-modal');
    renderFirst();
    anim(first.querySelector('.fr-fl')!, [{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'none' }], 320);
    ctx.cue('open', .7);
    void recheck();
    (first.querySelector<HTMLElement>('[data-f="key"]') ?? first.querySelector<HTMLElement>('.fr-ff .btn.warm') ?? first).focus({ preventScroll: true });
  }
  function firstTo(i: number) {
    const F = S.first; if (!F || i === F.step || i < 0 || i > 2) return;
    F.step = i; renderFirst(); ctx.tick();
    if (i === 1) void recheck();
    if (i === 2) void loadProjects().then(renderFirst);
    anim(fBody, [{ opacity: 0, transform: 'translateX(10px)' }, { opacity: 1, transform: 'none' }], 220);
    first.querySelector<HTMLElement>('.fr-ff [data-act="fr-fnext"], .fr-ff [data-act="fr-fgo"]')?.focus({ preventScroll: true });
  }
  function closeFirst() {
    if (!S.first) return;
    S.first = null; S.key = ''; store.set('agents.first', 'done'); win.classList.remove('fr-modal');
    const done = () => { if (!S.first) first.hidden = true; };
    const a = anim(first, [{ opacity: 1 }, { opacity: 0 }], 260, 'cubic-bezier(.4,0,1,1)');
    if (a) a.onfinish = done; else done();
  }
  // 开始: the slip drops on the folder picked, ready for the first sentence.
  function start() {
    const dir = S.first?.proj; if (!dir) return;
    closeFirst();
    store.set('agents.project', dir);
    const go = Object.assign(document.createElement('button'), { type: 'button', hidden: true });
    go.dataset.act = 'new'; go.dataset.dir = dir; win.append(go); go.click(); go.remove();
  }
  // A Mac the host is not ready on yet opens on the first run, once.
  void (async () => {
    await Promise.all([loadSettings(), loadProjects()]);
    if (store.get('agents.first') !== 'done' && S.auth && (!S.auth.ready || !S.list.length)) openFirst();
  })();
  window.agents?.onSettings?.(() => openSheet());
  // /doctor, /status and /login: the check-up, here.
  ctx.own.set('doctor', () => openSheet('doc'));

  // ---------- keys ----------
  function trap(e: KeyboardEvent, root: HTMLElement) {
    const f = [...root.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),[tabindex]:not([tabindex="-1"])')].filter(el => el.offsetParent !== null);
    if (!f.length) { e.preventDefault(); return; }
    const i = f.indexOf(document.activeElement as HTMLElement);
    if (i < 0 || (e.shiftKey && i === 0) || (!e.shiftKey && i === f.length - 1)) { e.preventDefault(); f[i < 0 ? 0 : e.shiftKey ? f.length - 1 : 0].focus({ preventScroll: true }); }
  }
  return {
    key(e) {
      const k = e.key, mod = (e.metaKey || e.ctrlKey) && !e.altKey, t = e.target as HTMLElement;
      const root = S.first ? first : S.open ? sheet : null;
      if (!root) {
        if (mod && k === ',' && !e.isComposing) { e.preventDefault(); e.stopImmediatePropagation(); openSheet(); return true; }
        return false;
      }
      // Modal: nothing behind it takes a key; what is typed inside goes where it goes.
      e.stopImmediatePropagation();
      if (!root.contains(t) || (mod && k === ',')) e.preventDefault();
      if (k === 'Tab') trap(e, root);
      else if (k === 'Escape' && S.open) { e.preventDefault(); closeSheet(); }
      else if (k === 'Enter' && t.dataset?.f === 'key' && !e.isComposing) { e.preventDefault(); void verify(); }
      else if (S.open && (k === 'ArrowDown' || k === 'ArrowUp') && t.classList.contains('fr-ni')) {
        e.preventDefault();
        const ids = [...PAGES.map(([id]) => id), ...ext().map(p => p.id)], j = ids.indexOf(S.page);
        goPage(ids[Math.max(0, Math.min(ids.length - 1, j + (k === 'ArrowDown' ? 1 : -1)))]);
        sheet.querySelector<HTMLElement>('.fr-ni[aria-current="page"]')?.focus({ preventScroll: true });
      }
      return true;
    },
    act(a, el) {
      if (!a.startsWith('fr-') || !(S.open || S.first)) return false;
      if (a === 'fr-close') closeSheet();
      else if (a === 'fr-pg') goPage(el.dataset.p!);
      else if (a === 'fr-prov') { if (provider() !== el.dataset.v) { ctx.tick(); void save({ provider: el.dataset.v as Settings['provider'] }).then(() => { S.keyErr = ''; redraw(); }); } }
      else if (a === 'fr-verify') void verify();
      else if (a === 'fr-forget') void ctx.tryCall('/settings/key', undefined, 'DELETE').then(r => { if (!r) return; took(r as { settings: Settings; auth: Auth }); ctx.cue('close', .6); ctx.toast(tr('从钥匙串里删掉了', 'Removed from Keychain')); redraw(); saved(); });
      else if (a === 'fr-fix') void fix(el.dataset.x!);
      else if (a === 'fr-paths') { S.paths = !S.paths; redraw(); }
      else if (a === 'fr-recheck') void recheck();
      else if (a === 'fr-sw') {
        // The notch is on unless switched off; the whole notify goes back each time.
        const k = el.dataset.k as 'done' | 'wait' | 'err' | 'notch', on = k === 'notch' ? notify().notch === false : !notify()[k], n = { ...notify(), [k]: on };
        ctx.cue(on ? 'on' : 'off', .6); void save({ notify: n }).then(redraw);
      }
      else if (a === 'fr-test') void testNote();
      else if (a === 'fr-add') void addFolder();
      else if (a === 'fr-unadd') void unadd(el.dataset.path!, el.closest('.fr-pj'));
      else if (a === 'fr-fnext') firstTo((S.first?.step ?? 0) + 1);
      else if (a === 'fr-fback') firstTo((S.first?.step ?? 0) - 1);
      else if (a === 'fr-fdot') { if (Number(el.dataset.k) < (S.first?.step ?? 0)) firstTo(Number(el.dataset.k)); }
      else if (a === 'fr-fskip') { closeFirst(); settle(); ctx.toast(tr('跳过了 · 钥匙和体检在设置里（⌘,）', 'Skipped · Keys and Health check are in Settings (⌘,)')); }
      else if (a === 'fr-fgo') start();
      else if (a === 'fr-fproj') { if (S.first) { S.first.proj = el.dataset.path!; ctx.tick(); renderFirst(); } }
      else if (a === 'fr-fadd') void addFolder(p => { if (S.first) S.first.proj = p; });
      else return false;
      return true;
    },
  };
}

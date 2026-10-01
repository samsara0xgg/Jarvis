// The composer beyond typing: any file, dropped anywhere on the window or picked, goes with a message; a right click on
// one copies it, its path, or opens it. The / menu shows the window's own commands above the agent's, and /add-dir
// gives a session one more folder. The permission modes are each agent's own, and 完全放开 is asked once. An MCP server
// that failed or wants a sign-in gets one line under the step that ran into it; /mcp lists them all.
import type { Agent, Mcp, File as Upload, Pic, Sess, Step } from '../../electron/agents/types';
import type { Feature, PageCtx } from './ctx';
import { settingsPages } from './settings';
import './input.css';
import { plural, tr } from './lang';

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const anim = (el: Element, kf: Keyframe[], ms: number) => reduced.matches ? null : el.animate(kf, { duration: ms, easing: 'cubic-bezier(.23,1,.32,1)' });
const IC = {
  tray: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v11M7.5 9.5L12 14l4.5-4.5"/><path d="M4 14v4.5A1.5 1.5 0 005.5 20h13a1.5 1.5 0 001.5-1.5V14"/></svg>',
  dir: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" aria-hidden="true"><path d="M1.8 4.2c0-.6.4-1 1-1h3.1l1.4 1.5h5.9c.6 0 1 .4 1 1v6.6c0 .6-.4 1-1 1H2.8c-.6 0-1-.4-1-1z"/></svg>',
  x: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="m4.5 4.5 7 7m0-7-7 7"/></svg>',
  copy: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.6"/><path d="M10.5 5.5v-2a1 1 0 00-1-1h-6a1 1 0 00-1 1v6a1 1 0 001 1h2"/></svg>',
  path: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" aria-hidden="true"><path d="M6.8 3.2 4.2 12.8M8.8 12.8h3.6"/></svg>',
  side: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="3" width="12" height="10" rx="1.6"/><path d="M9.5 3v10"/></svg>',
  out: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 2.5h4.5V7M13.5 2.5 7.5 8.5M11.5 9.5v3a1 1 0 01-1 1h-7a1 1 0 01-1-1v-7a1 1 0 011-1h3"/></svg>',
  plus: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true"><path d="M8 3v10M3 8h10"/></svg>',
  finder: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="2.5" width="11" height="11" rx="2"/><path d="M8 2.5c-1 2-1.3 4-1.1 6H8.6M6 10.8c1.3.8 2.7.8 4 0" stroke-linecap="round"/></svg>',
};

// ---------- files: what a message carries, and how the composer and the conversation show them ----------
type Kind = 'img' | 'pdf' | 'code' | 'md' | 'text' | 'media' | 'dir' | 'file';
// `view` shows a picture in the composer without carrying its data: URL through every redraw; `size` in bytes.
export type Attached = Upload & { view: string; kind: Kind; size: number };
const MB = 1 << 20, CAP = 30 * MB;
const extOf = (name: string) => /\.([a-z0-9]+)$/i.exec(name)?.[1] ?? '';
const CODE = /^(ts|tsx|js|jsx|mjs|cjs|py|rb|go|rs|swift|kt|java|c|h|cc|cpp|hpp|m|mm|cs|php|sh|zsh|bash|lua|sql|html|css|scss|vue|svelte|json|yaml|yml|toml|xml|ini|zig|ex|exs|hs|scala|dart)$/i;
function kindOf(name: string, type: string, dir: boolean): Kind {
  const x = extOf(name).toLowerCase();
  if (dir) return 'dir';
  if (/^(png|jpe?g|gif|webp)$/.test(x) || /^image\/(png|jpeg|gif|webp)$/.test(type)) return 'img';
  if (x === 'pdf' || type === 'application/pdf') return 'pdf';
  if (x === 'md' || x === 'markdown') return 'md';
  if (CODE.test(x)) return 'code';
  if (/^(mov|mp4|m4v|webm|avi|mkv|mp3|m4a|wav|aac|flac)$/.test(x) || /^(video|audio)\//.test(type)) return 'media';
  return /^(txt|log|csv|tsv)$/.test(x) || type.startsWith('text/') ? 'text' : 'file';
}
const sizeOf = (n: number) => n < 1024 ? `${n} B` : n < MB ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / MB).toFixed(n < 10 * MB ? 1 : 0)} MB`;
const dataURL = (f: Blob) => new Promise<string>((done, fail) => { const r = new FileReader(); r.onload = () => done(String(r.result)); r.onerror = () => fail(r.error); r.readAsDataURL(f); });
// Files from the system (a drop, a paste, the picker): each goes by where it is on this Mac when the window can tell,
// otherwise as its content. A folder has to go by its path; one over 30 MB goes by its path only.
export async function attachAll(list: { f: File; dir?: boolean }[], say: (text: string) => void): Promise<Attached[]> {
  const out: Attached[] = [];
  for (const [k, { f, dir = false }] of list.entries()) {
    const name = f.name || tr(`图片 ${k + 1}.png`, `Image ${k + 1}.png`), kind = kindOf(name, f.type, dir), p = window.agents?.pathOf?.(f) ?? '', path = p.startsWith('/') ? p : '';
    const view = kind === 'img' && f.size <= CAP ? URL.createObjectURL(f) : '';
    if (path) { out.push({ name, path, view, kind, size: f.size }); continue; }
    if (dir || f.size > CAP) { if (view) URL.revokeObjectURL(view); say(dir ? tr(`找不到「${name}」在哪，文件夹要从访达拖进来`, `Can't find "${name}", drag folders in from Finder`) : tr(`「${name}」超过 30 MB，从访达拖进来就只给它路径`, `"${name}" is over 30 MB, drag it in from Finder to give just its path`)); continue; }
    try { out.push({ name, url: await dataURL(f), view, kind, size: f.size }); }
    catch { if (view) URL.revokeObjectURL(view); say(tr(`读不了「${name}」`, `Can't read "${name}"`)); }
  }
  return out;
}
// The type as a small tile: a folder, or the extension.
const tile = (kind: Kind, name: string) => kind === 'dir' ? `<i class="in-fi in-k-dir">${IC.dir}</i>`
  : `<i class="in-fi in-k-${kind}">${esc((extOf(name) || 'FILE').slice(0, 4).toUpperCase())}</i>`;
// The composer's files: pictures as small pictures, anything else a chip with its type, name and size.
export const chipsHTML = (fs: Attached[]) => fs.map((f, k) => {
  const at = `data-in-k="${k}" data-kind="${f.kind}"${f.path ? ` data-ref="${esc(f.path)}"` : ''} data-label="${esc(f.name)}"`;
  if (f.kind === 'img' && f.view) return `<span class="c-pic" ${at}><button type="button" class="pic" data-act="view" data-tip="${tr('看大图', 'View larger')}"><img src="${f.view}" alt="${esc(f.name)}"></button><i data-act="unfile" data-k="${k}" aria-label="${tr('去掉', 'Remove')}">✕</i></span>`;
  const big = f.kind !== 'dir' && f.size > CAP, sub = f.kind === 'dir' ? tr('文件夹', 'Folder') : big ? tr(`${sizeOf(f.size)} · 只给路径`, `${sizeOf(f.size)} · path only`) : sizeOf(f.size);
  return `<span class="in-fc${big ? ' in-big' : ''}" role="button" tabindex="0" data-act="in-open" ${at} data-tip="${esc(f.path || f.name)}" aria-label="${tr('打开', 'Open')} ${esc(f.name)}">`
    + `${tile(f.kind, f.name)}<span class="in-fn"><b>${esc(f.name)}</b><small>${esc(sub)}</small></span><button type="button" class="in-x" data-act="unfile" data-k="${k}" aria-label="${tr('去掉', 'Remove')} ${esc(f.name)}">${IC.x}</button></span>`;
}).join('');
// A file a message went with, by its path: a smaller chip that opens it.
export function fileTag(f: Pic & { path: string }) {
  const dir = f.path.endsWith('/'), kind = kindOf(f.name, '', dir);
  return `<button type="button" class="in-mfc" data-act="in-open" data-kind="${kind}" data-ref="${esc(f.path)}" data-label="${esc(f.name)}" data-tip="${esc(f.path)}" aria-label="${tr('打开', 'Open')} ${esc(f.name)}">`
    + `${tile(kind, f.name)}<span>${esc(f.name)}</span>${dir ? tr('<small>文件夹</small>', '<small>Folder</small>') : ''}</button>`;
}

// ---------- dropping: anywhere on the window, a calm veil while files are held over it ----------
let veil: HTMLElement | null = null, over = 0, inner = false;
const hasFiles = (e: DragEvent) => !inner && !!e.dataTransfer && [...e.dataTransfer.types].includes('Files');
const composerOn = (win: HTMLElement) => !(win.querySelector('.composer') as HTMLElement | null)?.hidden;
function hideVeil() { over = 0; if (veil && !veil.hidden) { veil.hidden = true; veil.replaceChildren(); } }
// What a drop brings, read while the drop lasts (a folder is known only then): null when it is not files from outside.
export function dropped(e: DragEvent, say: (text: string) => void): Promise<Attached[]> | null {
  if (!hasFiles(e)) return null;
  e.preventDefault(); hideVeil();
  const win = e.currentTarget as HTMLElement, dt = e.dataTransfer!;
  if (!composerOn(win)) return null;
  const list = [...dt.items].filter(it => it.kind === 'file').map(it => ({ f: it.getAsFile(), dir: !!it.webkitGetAsEntry?.()?.isDirectory }))
    .filter((x): x is { f: File; dir: boolean } => !!x.f);
  return attachAll(list.length ? list : [...dt.files].map(f => ({ f })), say);
}

// ---------- the / menu: the window's own commands (a feature here runs them) above the agent's, one filter over both ----------
export type Pick = [string, string, string?];
let own: PageCtx['own'] | null = null;
// What starts with what was typed, then what holds it; the third entry marks the window's own.
export function slashPicks(list: [string, string, string?][], q: string): Pick[] {
  const rest = q.slice(1).toLowerCase(), mine = (c: Pick) => !!c[2] && !!own?.has(c[2]);
  const found = [...list.filter(c => c[0].startsWith(q)), ...rest ? list.filter(c => c[0][0] === q[0] && !c[0].startsWith(q) && c[0].slice(1).toLowerCase().includes(rest)) : []];
  return [...found.filter(mine).map(c => [c[0], c[1], 'own'] as Pick), ...found.filter(c => !mine(c)).map(c => [c[0], c[1]] as Pick)].slice(0, 60);
}
export function slashHTML(picks: Pick[], at: number, agent: string) {
  const rows = picks.map(([v, d, g], k) => (k && !!picks[k - 1][2] === !!g ? '' : `<p class="in-h">${g ? tr('窗口里的', 'Window commands') : tr(`${esc(agent)} 的`, `${esc(agent)} commands`)}</p>`)
    + `<button type="button" data-act="pickcmd" data-v="${esc(v)}"${k === at ? ' class="on"' : ''}><code>${esc(v)}</code><span>${esc(d)}</span></button>`).join('');
  return rows && `${rows}<p class="in-f"><kbd>↑</kbd><kbd>↓</kbd> ${tr('选', 'select')} · <kbd>⏎</kbd> ${tr('或', 'or')} <kbd>Tab</kbd> ${tr('填进去', 'insert')} · <kbd>esc</kbd> ${tr('收起', 'close')}</p>`;
}
const home = (p: string) => p.replace(/^\/(Users|home)\/[^/]+(?=\/|$)/, '~');
const day = (ms: number) => {
  const d = Math.floor((new Date().setHours(24, 0, 0, 0) - ms) / 864e5);
  return d <= 0 ? tr('今天', 'Today') : d === 1 ? tr('昨天', 'Yesterday') : d < 7 ? tr(`${d} 天前`, `${d} d ago`) : tr(`${new Date(ms).getMonth() + 1} 月 ${new Date(ms).getDate()} 日`, new Date(ms).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }));
};

// ---------- permission modes: each agent's own (the host's catalog names them), each with what it means ----------
// 完全放开 is Claude Code's bypassPermissions and Codex's full access: chosen, it is asked once in the window first.
const FULL = new Set(['bypassPermissions', 'full']);
const SUB: Record<Agent, Record<string, string>> = {
  claude: { auto: tr('能做的都做，要紧的才问你', 'Acts, asks only if it matters'), default: tr('改文件、跑命令前都问', 'Asks before edits and commands'), acceptEdits: tr('改文件不问，跑命令还问', 'Edits freely, asks for commands'), plan: tr('先出计划，你点头才动手', 'Plans first, acts on approval'), bypassPermissions: tr('什么都不问', 'Never asks') },
  codex: { auto: tr('在这个文件夹里改，出去要问', 'Edits in this folder, asks outside'), read: tr('只看不改', 'Read only'), full: tr('不问你，也不限文件夹', 'Never asks, any folder'), plan: tr('先出计划', 'Plans first') },
};
const ASK: Record<Agent, string> = { claude: tr('它不再问你就改文件、跑命令。只在这个会话里。', 'It will edit files and run commands without asking. This session only.'), codex: tr('它不再问你，改文件、跑命令也不限在这个文件夹里。只在这个会话里。', 'It will stop asking, and can edit files and run commands outside this folder. This session only.') };
export const modesHTML = (modes: [string, string][], v: string, agent: Agent) => modes.map(([x, l]) => `<button type="button" data-act="set" data-k="mode" data-v="${esc(x)}" data-agent="${agent}"`
  + `${x === v || FULL.has(x) ? ` class="${[x === v ? 'on' : '', FULL.has(x) ? 'in-warm' : ''].filter(Boolean).join(' ')}"` : ''}>${esc(l)}${SUB[agent][x] ? `<small>${SUB[agent][x]}</small>` : ''}</button>`).join('');
const askHTML = (agent: Agent, v: string, label: string) => `<p><b>${esc(label)}${tr('？', '?')}</b>${ASK[agent]}</p><span class="in-askb"><button type="button" class="btn" data-act="in-full-no">${tr('算了', 'Cancel')} <kbd>esc</kbd></button>`
  + `<button type="button" class="btn warm" data-act="set" data-k="mode" data-v="${esc(v)}" data-ok="1">${tr('放开', 'Turn on')} <kbd>⏎</kbd></button></span>`;

// ---------- MCP: nothing while all is well; one line under the step that ran into a server that failed or wants a
// sign-in; /mcp (and the settings sheet, through renderMcp) the whole list ----------
// What the host last said of a session's servers; `busy`: a sign-in or a connect this window is waiting on; `fixed`: one
// it signed in to or connected again, whose line turns mint; `skip`: 这次不用, no line for it in this session again.
type Srv = { list?: Mcp[]; err?: string; loading?: Promise<void>; again?: number };
const mcp = new Map<string, Srv>(), busy = new Map<string, 'login' | 'wait'>(), fixed = new Set<string>(), skip = new Set<string>(), known = new Set<string>();
const views = new Map<HTMLElement, { sid: string; page: boolean }>();
const SCOPE: Record<string, string> = { user: tr('用户', 'User'), project: tr('项目', 'Project'), local: tr('本地', 'Local'), plugin: tr('插件', 'Plugin'), claudeai: 'claude.ai', managed: tr('管理员', 'Admin'), enterprise: tr('管理员', 'Admin'), dynamic: tr('这次加的', 'Session') };
let C: PageCtx | undefined;
const kOf = (sid: string, n: string) => `${sid}\n${n}`;
const norm = (n: string) => n.replace(/[^\w-]/g, '_');
function loadMcp(sid: string): Promise<void> {
  const m = mcp.get(sid) ?? {};
  mcp.set(sid, m);
  if (!C) return Promise.resolve();
  return m.loading ??= C.call<{ servers: Mcp[] }>(`/sessions/${sid}/mcp`).then(r => { m.list = r.servers; m.err = undefined; }, e => { m.err = e instanceof Error ? e.message : String(e); })
    .finally(() => {
      m.loading = undefined;
      // One still connecting is read again while a list shows it.
      if (m.list?.some(x => x.st === 'wait') && [...views.values()].some(v => v.sid === sid) && (m.again = (m.again ?? 0) + 1) <= 8) setTimeout(() => void loadMcp(sid), 2000);
      mcpChanged();
    });
}
function mcpChanged() {
  C?.draw('main');
  for (const [el, v] of views) { if (el.isConnected) drawList(el, v.sid, v.page); else views.delete(el); }
}
// The server a step called that failed, or that was Claude Code's own authenticate call for a server that wants a sign-in
// (it succeeds, telling the model to hand the owner a page), once the list is read; undefined until then. A step not
// seen before reads the list again.
function serverOf(s: Sess, st: Step, i: number, j: number): Mcp | null | undefined {
  if (st.k !== 'tool' || !st.t.includes(' · ') || st.t.startsWith('Skill · ') || (st.ok !== false && !st.t.endsWith(' · authenticate'))) return null;
  const m = mcp.get(s.id), key = `${s.id}:${i}:${j}`, fresh = !known.has(key);
  known.add(key);
  if (!m?.list) { if (!m?.loading && !m?.err) void loadMcp(s.id); return undefined; }
  if (fresh && !m.loading) void loadMcp(s.id);
  const n = norm(st.t.split(' · ')[0]);
  return m.list.find(x => norm(x.name) === n) ?? null;
}
function lineHTML(s: Sess, m: Mcp): string {
  const k = kOf(s.id, m.name), b = busy.get(k);
  if (skip.has(k)) return '';
  const act = (v: string, l: string) => `<b>·</b><button type="button" data-act="in-mq" data-v="${v}" data-sid="${esc(s.id)}" data-n="${esc(m.name)}">${l}</button>`, no = act('skip', tr('这次不用', 'Not now'));
  const [tone, text, acts] = b === 'login' ? ['wait', tr(`在浏览器里登录 ${m.name}…`, `Signing in to ${m.name} via browser…`), ''] : b || m.st === 'wait' ? ['wait', tr(`${m.name} 在连…`, `${m.name} connecting…`), '']
    : m.st === 'auth' ? ['warm', tr(`${m.name} 要登录才能用`, `${m.name} needs sign-in`), (m.can.includes('login') ? act('login', tr('登录', 'Sign in')) : '') + no]
    : m.st === 'fail' ? ['red', tr(`${m.name} ${m.why ?? '连不上'}`, `${m.name} ${m.why ?? "can't connect"}`), (m.can.includes('reconnect') ? act('retry', tr('重试', 'Retry')) : '') + no]
    : m.st === 'off' ? ['plain', tr(`${m.name} 关着`, `${m.name} is off`), (m.can.includes('on') ? act('on', tr('打开', 'Turn on')) : '') + no]
    : fixed.has(k) ? ['mint', tr(`${m.name} 连上了${m.tools ? ` · ${m.tools} 个工具` : ''}`, `${m.name} connected${m.tools ? ` · ${plural(m.tools, 'tool')}` : ''}`), ''] : ['', '', ''];
  return text ? `<div class="in-mq in-t-${tone}" data-x><i></i><span title="${esc(text)}">${esc(text)}</span>${acts}</div>` : '';
}
// Sign in, connect again, switch on or off: the page a sign-in opens is watched until the server is connected (three
// minutes at most).
async function mcpDo(s: Sess, n: string, act: 'login' | 'reconnect' | 'on' | 'off' | 'skip') {
  const k = kOf(s.id, n), c = C;
  if (!c || busy.has(k)) return;
  if (act === 'skip') { skip.add(k); c.cue('close'); mcpChanged(); return; }
  if (act !== 'off') { busy.set(k, act === 'login' ? 'login' : 'wait'); mcpChanged(); }
  const r = await c.tryCall(`/sessions/${s.id}/mcp`, { name: n, action: act }) as { servers?: Mcp[]; url?: string } | null, m = mcp.get(s.id) ?? {};
  mcp.set(s.id, m);
  if (r?.servers) m.list = r.servers;
  if (r?.url) {
    void window.agents?.openUrl?.(r.url);
    for (let t = Date.now(); Date.now() - t < 180_000; ) {
      await new Promise(ok => setTimeout(ok, 2000));
      const l = await c.call<{ servers: Mcp[] }>(`/sessions/${s.id}/mcp`).catch(() => null);
      if (l) m.list = l.servers;
      if (l?.servers.find(x => x.name === n)?.st === 'on') break;
    }
  }
  busy.delete(k);
  const now = m.list?.find(x => x.name === n);
  if ((act === 'login' || act === 'reconnect') && now?.st === 'on') { fixed.add(k); skip.delete(k); c.cue('done'); }
  else if (act === 'login' && r?.url) c.toast(tr(`还没等到 ${n} 登好`, `${n} sign-in didn't finish`), true);
  else if (act === 'reconnect' && r) c.toast(tr(`${n} 还是连不上`, `${n} still can't connect`), true);
  else if ((act === 'on' || act === 'off') && r) c.cue(act);
  mcpChanged();
}
function rowHTML(s: Sess, m: Mcp) {
  const b = busy.get(kOf(s.id, m.name)), on = m.st !== 'off', n = esc(m.name), can = m.can.includes(on ? 'off' : 'on');
  const text = b === 'login' ? tr('在浏览器里登录…', 'Signing in via browser…') : b || m.st === 'wait' ? tr('在连…', 'Connecting…') : m.st === 'on' ? tr(`连上了${m.tools ? ` · ${m.tools} 个工具` : ''}`, `Connected${m.tools ? ` · ${plural(m.tools, 'tool')}` : ''}`)
    : m.st === 'auth' ? m.why ?? tr('要登录', 'Needs sign-in') : m.st === 'fail' ? m.why ?? tr('连不上', "Can't connect") : tr('关着', 'Off');
  const act = b || m.st === 'wait' ? `<span class="in-spin" role="img" aria-label="${tr('在连', 'Connecting')}"></span>`
    : m.st === 'auth' && m.can.includes('login') ? `<button type="button" class="in-ma in-warm" data-in-m="login" data-n="${n}">${tr('登录', 'Sign in')}</button>`
    : m.st === 'fail' && m.can.includes('reconnect') ? `<button type="button" class="in-ma" data-in-m="reconnect" data-n="${n}">${tr('重连', 'Reconnect')}</button>` : '';
  const sw = `<button type="button" class="in-sw" role="switch" aria-checked="${on}" aria-label="${n}"${can ? '' : ' aria-disabled="true"'} data-in-m="sw" data-n="${n}"><i></i></button>`;
  return `<div class="in-mr in-s-${m.st}"><i class="in-dot in-s-${b ? 'wait' : m.st}"></i><span class="in-mt"><b>${n}</b>${m.scope ? `<small>${esc(SCOPE[m.scope] ?? m.scope)}</small>` : ''}`
    + `<span title="${esc(text)}">${esc(text)}</span></span><span class="in-mx">${act}${sw}</span></div>`;
}
function drawList(el: HTMLElement, sid: string, page: boolean) {
  const s = C?.byId(sid), m = mcp.get(sid);
  if (!s) { el.innerHTML = tr('<p class="in-mf">没有这个会话</p>', '<p class="in-mf">No such session</p>'); return; }
  const f = document.activeElement instanceof HTMLElement && el.contains(document.activeElement) ? [document.activeElement.dataset.n, document.activeElement.dataset.inM] : null;
  const head = page ? `<p class="in-ms">${esc(s.title)} · ${s.agent === 'codex' ? 'Codex' : 'Claude Code'}</p>`
    : `<p class="in-mh"><b>${tr('这个会话的', "This session's")} MCP</b><button type="button" class="in-mclose" data-in-m="x" aria-label="${tr('关闭', 'Close')}">${IC.x}</button></p>`;
  el.innerHTML = head + (!m?.list ? `<p class="in-mf in-mload">${m?.err ? esc(m.err) : tr('<span class="in-spin"></span>在读它的 MCP…', '<span class="in-spin"></span>Reading MCP…')}</p>`
    : !m.list.length ? tr('<p class="in-mf">这个会话没有 MCP</p>', '<p class="in-mf">No MCP in this session</p>')
    : m.list.map(x => rowHTML(s, x)).join('') + `<p class="in-mf">${s.agent === 'codex' ? tr('Codex 的 MCP 在它的 config.toml 里开关', 'Switch Codex MCP in its config.toml') : tr('关掉的在这个文件夹里都关着，终端里的 Claude Code 也是', 'Turned off here stays off in this folder, in Claude Code in Terminal too')}</p>`);
  // Focus stays on the same row: its button, or its switch once the button is gone.
  if (f?.[0]) { const q = (a?: string) => a ? [...el.querySelectorAll<HTMLElement>(`[data-in-m="${a}"]`)].find(x => x.dataset.n === f[0]) : undefined; (q(f[1]) ?? q('sw'))?.focus({ preventScroll: true }); }
  el.dispatchEvent(new Event('in-mcp-drawn'));
}
function onList(e: MouseEvent) {
  const el = e.currentTarget as HTMLElement, b = (e.target as Element).closest<HTMLElement>('[data-in-m]'), v = views.get(el), s = v && C?.byId(v.sid);
  if (!b || !s || !C) return;
  const a = b.dataset.inM!, n = b.dataset.n ?? '';
  if (a === 'x') { el.dispatchEvent(new Event('in-mcp-close', { bubbles: true })); return; }
  if (a === 'sw') {
    if (b.getAttribute('aria-disabled') === 'true') { C.toast(s.agent === 'codex' ? tr('Codex 的 MCP 要在它的 config.toml 里开关', 'Switch Codex MCP in its config.toml') : tr('它现在开关不了', "Can't switch it right now")); return; }
    void mcpDo(s, n, b.getAttribute('aria-checked') === 'true' ? 'off' : 'on');
  } else if (a === 'login' || a === 'reconnect') void mcpDo(s, n, a);
}
// A session's whole list, drawn into `el` and kept current as it changes: /mcp shows it over the composer, and the
// settings sheet can show it as a page of its own (`page`: a line naming the session in place of the title and ✕).
// It is read from the host each time it is drawn afresh.
export function renderMcp(el: HTMLElement, s: Sess, page = true) {
  if (!views.has(el)) el.addEventListener('click', onList);
  views.set(el, { sid: s.id, page });
  el.classList.add('in-mcpl'); el.classList.toggle('in-page', page);
  drawList(el, s.id, page);
  void loadMcp(s.id);
}

export function mountInput(ctx: PageCtx): Feature {
  C = ctx;
  const { win } = ctx;
  own = ctx.own;
  // The settings sheet's MCP page: the session in front's list, the same one /mcp shows.
  settingsPages.push({ id: 'mcp', label: 'MCP', draw(el) {
    const s = ctx.current();
    if (s) renderMcp(el, s, true); else el.innerHTML = `<p class="fr-fn">${tr('开了会话再看它的 MCP：每个会话连着的服务器不一样。', 'Open a session to see its MCP: each session connects different servers.')}</p>`;
  } });
  veil = document.createElement('div'); veil.className = 'in-drop'; veil.hidden = true; win.append(veil);
  function showVeil(n: number) {
    if (!veil!.hidden) return;
    ctx.closeMenu();
    const s = ctx.current(), head = win.querySelector('.m-head')?.getBoundingClientRect(), w = win.getBoundingClientRect();
    veil!.style.top = `${head && head.height ? head.bottom - w.top : 0}px`;
    veil!.innerHTML = `<div class="in-dt">${IC.tray}<b>${tr('放下就行', 'Drop to add')}</b><small>${n ? tr(`${n} 个文件，`, `${plural(n, 'file')} into `) : tr('', 'Into ')}${tr('放进', '')}${s ? tr(`「${esc(s.title)}」`, `"${esc(s.title)}"`) : tr('新会话', 'a new session')}</small></div>`;
    veil!.hidden = false; anim(veil!, [{ opacity: 0 }, { opacity: 1 }], 160);
  }
  // A picture dragged within the page says Files too; only a drag from outside counts. Enter and leave come in pairs as
  // the pointer crosses elements, so the count reaches 0 when it leaves the window.
  document.addEventListener('dragstart', () => { inner = true; });
  document.addEventListener('dragend', () => { inner = false; });
  win.addEventListener('dragenter', e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    if (composerOn(win)) { over++; showVeil([...e.dataTransfer!.items].filter(it => it.kind === 'file').length); }
  });
  win.addEventListener('dragover', e => { if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer!.dropEffect = composerOn(win) ? 'copy' : 'none'; });
  win.addEventListener('dragleave', () => { if (over && --over <= 0) hideVeil(); });

  // ---------- a right click on a file: in the composer, or on a message it went with ----------
  type Target = { name: string; path?: string; dir: boolean; src?: string; el: HTMLElement };
  let target: Target | null = null;
  function targetOf(el: Element): Target | null {
    const chip = el.closest<HTMLElement>('.c-files > [data-in-k]');
    if (chip) return { name: chip.dataset.label ?? '', path: chip.dataset.ref, dir: chip.dataset.kind === 'dir', src: chip.querySelector('img')?.src, el: chip };
    const one = el.closest<HTMLElement>('.you .att > *'), item = one?.closest('.item'), s = ctx.current();
    if (!one || !item || !s) return null;
    const it = ctx.items(s.id)?.[[...item.parentElement!.children].indexOf(item)], f = it?.k === 'you' ? it.files?.[[...one.parentElement!.children].indexOf(one)] : undefined;
    return f ? { name: f.name, path: f.path, dir: !!f.path?.endsWith('/'), src: one.querySelector('img')?.src, el: one } : null;
  }
  win.addEventListener('contextmenu', e => {
    const t = targetOf(e.target as Element);
    if (!t) return;
    e.preventDefault(); target = t;
    const b = (v: string, label: string, ic: string) => `<button type="button" data-act="in-a" data-v="${v}">${ic}${label}</button>`, w = win.getBoundingClientRect();
    const opens = [...t.path && !t.dir && !t.src && ctx.chat() ? [b('side', tr('在右边打开', 'Open on the right'), IC.side)] : [], ...t.path && !t.dir ? [b('app', tr('用默认的 app 打开', 'Open with default app'), IC.out)] : [],
      ...t.path ? [b('finder', tr('在访达里显示', 'Show in Finder'), IC.finder)] : []];
    ctx.menu(b('copy', tr('复制', 'Copy'), IC.copy) + (t.path ? b('path', tr('复制路径', 'Copy path'), IC.path) : '') + (opens.length ? `<span class="sep"></span>${opens.join('')}` : ''),
      { x: e.clientX - w.left, y: e.clientY - w.top }, { cls: 'in-att' });
  });
  // A picture is copied as a picture (drawn again, since the page may not read its bytes back); any other file as the
  // file itself, the way Finder copies it.
  async function copy(t: Target) {
    try {
      if (t.src) {
        const img = new Image(); img.crossOrigin = 'anonymous'; img.src = t.src; await img.decode();
        const c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight; c.getContext('2d')!.drawImage(img, 0, 0);
        const blob = await new Promise<Blob | null>(r => c.toBlob(r, 'image/png'));
        if (!blob) throw new Error('no picture');
        await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
      } else {
        const how = t.path ? await window.agents?.copyFile?.(t.path) : false;
        if (!how) throw new Error('not copied');
        if (how === 'path') { ctx.toast(tr('只复制了它的路径', 'Copied its path only')); return; }
      }
      ctx.toast(tr(`复制了「${t.name}」`, `Copied "${t.name}"`));
    } catch { ctx.toast(tr(`复制不了「${t.name}」`, `Can't copy "${t.name}"`), true); }
  }
  // A file opens on the right in a session, with Quick Look before there is one; a folder opens in Finder.
  function open(el: HTMLElement) {
    const p = el.dataset.ref;
    if (!p) { ctx.toast(tr('发出去以后才能打开', 'Send it first to open it')); return; }
    if (el.dataset.kind === 'dir') void window.agents?.reveal(p);
    else if (ctx.chat()) ctx.wb.act('peek', el);
    else void window.agents?.quickLook?.(p);
  }

  // ---------- /add-dir: a sheet under the title with the folders you work in, or the Mac's folder picker ----------
  type Folder = { path: string; name: string; used?: number };
  const sheet = document.createElement('div');
  sheet.className = 'in-sheet'; sheet.hidden = true; win.append(sheet);
  let dirs: { id: string; rows: Folder[]; at: number } | null = null;
  async function addDir(s: Sess, p: string) {
    if (p.replace(/\/+$/, '') === s.cwd) { ctx.toast(tr('这就是它自己的文件夹', "That's already this session's folder")); return; }
    if (s.dirs?.includes(p)) { ctx.toast(tr('这个文件夹已经加过了', 'Folder already added')); return; }
    if (await ctx.tryCall(`/sessions/${s.id}/dirs`, { dirs: [...s.dirs ?? [], p] })) ctx.cue('on');
  }
  function drawDirs() {
    if (!dirs) return;
    const s = ctx.byId(dirs.id), have = s?.dirs ?? [], at = dirs.at;
    sheet.innerHTML = `<div class="in-veil" data-act="in-dir-x"></div><div class="in-sh" role="dialog" aria-label="${tr('再加一个文件夹', 'Add another folder')}"><p class="in-shh"><b>${tr('再加一个文件夹', 'Add another folder')}</b><span>${tr('这个会话里它也能动', 'It can work there in this session')}</span></p>`
      + `<div class="in-drs" role="listbox">${dirs.rows.map((f, j) => `<button type="button" class="in-dr${j === at ? ' in-sel' : ''}${have.includes(f.path) ? ' in-on' : ''}" data-act="in-dir" data-v="${esc(f.path)}" role="option" aria-selected="${j === at}">`
        + `${IC.dir}<b>${esc(f.name)}</b><span>${esc(home(f.path))}</span><em>${have.includes(f.path) ? tr('加过了', 'Added') : f.used ? day(f.used) : ''}</em></button>`).join('')}`
      + `${dirs.rows.length ? '<i class="in-sep"></i>' : ''}<button type="button" class="in-dr in-other${at === dirs.rows.length ? ' in-sel' : ''}" data-act="in-dir-other" role="option" aria-selected="${at === dirs.rows.length}">${IC.plus}<b>${tr('选别的文件夹', 'Choose another folder')}…</b></button></div>`
      + `<div class="in-shf"><span><kbd>↑</kbd><kbd>↓</kbd> ${tr('选', 'select')} · <kbd>⏎</kbd> ${tr('加上', 'add')}</span><button type="button" class="btn" data-act="in-dir-x">${tr('取消', 'Cancel')} <kbd>esc</kbd></button></div></div>`;
  }
  async function openDirs(s: Sess) {
    ctx.closeMenu();
    const r = await ctx.call<{ list: Folder[] }>('/projects').catch(() => ({ list: [] as Folder[] }));
    dirs = { id: s.id, rows: r.list.filter(f => f.path !== s.cwd).slice(0, 6), at: 0 };
    drawDirs();
    const head = win.querySelector('.m-head')?.getBoundingClientRect(), w = win.getBoundingClientRect();
    sheet.style.top = `${head && head.height ? head.bottom - w.top : 0}px`;
    sheet.hidden = false;
    anim(sheet.querySelector('.in-sh')!, [{ opacity: 0, transform: 'translate(-50%, -10px)' }, { opacity: 1, transform: 'translateX(-50%)' }], 220);
  }
  function closeDirs() { if (!dirs) return false; dirs = null; sheet.hidden = true; sheet.replaceChildren(); ctx.ta.focus(); return true; }
  async function pickDir(p: string) {
    const s = dirs && ctx.byId(dirs.id);
    closeDirs();
    if (s && p) await addDir(s, p);
  }
  // ---------- 完全放开, asked once: above the composer in a session, in the menu's place before there is one ----------
  let ask: { id: string; v: string; label: string } | null = null;
  const modeChip = () => win.querySelector<HTMLElement>('.tb.mode');
  function askFull(el: HTMLElement) {
    const s = ctx.current(), v = el.dataset.v!, label = el.firstChild?.textContent ?? tr('完全放开', 'Full access');
    ctx.closeMenu();
    ask = { id: s?.id ?? '', v, label };
    if (s) { ctx.draw('comp'); requestAnimationFrame(() => { const a = win.querySelector('.in-ask'); if (a) anim(a, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], 180); }); }
    else { const chip = modeChip(); if (chip) ctx.menu(`<div class="in-ask">${askHTML(el.dataset.agent === 'codex' ? 'codex' : 'claude', v, label)}</div>`, chip, { cls: 'in-askpop' }); }
  }
  function noFull() { if (!ask) return false; const here = !ask.id; ask = null; if (here) ctx.closeMenu(); else ctx.draw('comp'); ctx.ta.focus(); return true; }
  // /permissions opens the same menu as the mode under the composer.
  ctx.own.set('mode', () => { requestAnimationFrame(() => modeChip()?.click()); });

  // ---------- /mcp: the list over the composer, closed by esc, ✕ or a click anywhere else ----------
  const panel = document.createElement('div'), list = document.createElement('div');
  panel.className = 'in-mcp'; panel.hidden = true; panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', 'MCP'); panel.append(list); win.append(panel);
  let shown = '';
  function place() {
    const box = win.querySelector('.c-box')?.getBoundingClientRect(), w = win.getBoundingClientRect();
    if (!box) return;
    panel.style.left = `${Math.max(8, Math.min(box.left - w.left + 8, w.width - panel.offsetWidth - 8))}px`;
    panel.style.top = `${Math.max(64, box.top - w.top - panel.offsetHeight - 8)}px`;
  }
  function openMcp(s: Sess) {
    ctx.closeMenu(); closeDirs();
    shown = s.id; panel.hidden = false;
    renderMcp(list, s, false); place();
    anim(panel, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], 160);
  }
  function closeMcp(focus = true) { if (!shown) return false; shown = ''; panel.hidden = true; views.delete(list); list.replaceChildren(); if (focus) ctx.ta.focus(); return true; }
  list.addEventListener('in-mcp-drawn', () => { if (shown) place(); });
  panel.addEventListener('in-mcp-close', () => closeMcp());
  win.addEventListener('pointerdown', e => { if (shown && !panel.contains(e.target as Node)) closeMcp(false); }, true);
  ctx.own.set('mcp', s => { if (s) openMcp(s); else ctx.toast(tr('开了会话再看它的 MCP', 'Open a session to see its MCP')); });

  // Typed with a path it adds that folder; without one it asks which.
  ctx.own.set('dirs', (s, arg) => {
    if (!s) { ctx.toast(tr('开了会话再给它加文件夹', 'Open a session to add a folder')); return; }
    if (arg.startsWith('/')) void addDir(s, arg);
    else void openDirs(s);
  });

  return {
    act(a, el) {
      if (a === 'in-open') { open(el); return true; }
      if (a === 'in-dir') { void pickDir(el.dataset.v!); return true; }
      if (a === 'in-dir-other') { const id = dirs?.id; closeDirs(); void window.agents?.folder().then(p => { const s = id ? ctx.byId(id) : undefined; if (p && s) void addDir(s, p); }); return true; }
      if (a === 'in-dir-x') { closeDirs(); return true; }
      if (a === 'set' && el.dataset.k === 'mode' && FULL.has(el.dataset.v!)) {
        const s = ctx.current();
        if (el.dataset.ok) { ask = null; ctx.cue('on'); ctx.draw('comp'); return false; }
        if (s?.mode === el.dataset.v) return false;
        askFull(el); return true;
      }
      if (a === 'set' && el.dataset.k === 'mode') { ask = null; return false; }
      if (a === 'in-full-no') { noFull(); return true; }
      if (a === 'in-mq') { const s = ctx.byId(el.dataset.sid!), v = el.dataset.v!; if (s) void mcpDo(s, el.dataset.n!, v === 'retry' ? 'reconnect' : v as 'login' | 'on' | 'skip'); return true; }
      if (a === 'in-a' && target) {
        const t = target, v = el.dataset.v;
        ctx.closeMenu();
        if (v === 'copy') void copy(t);
        else if (v === 'path' && t.path) void navigator.clipboard.writeText(t.path).then(() => ctx.toast(tr('复制了路径', 'Copied path')), () => ctx.toast(tr('复制不了路径', "Can't copy path"), true));
        else if (v === 'side') open(t.el);
        else if (v === 'app' && t.path) void window.agents?.openPath?.(t.path);
        else if (v === 'finder' && t.path) void window.agents?.revealFile?.(t.path.replace(/\/$/, ''));
        return true;
      }
      return false;
    },
    esc() { if (!veil?.hidden) { hideVeil(); return true; } return closeDirs() || noFull() || closeMcp(); },
    // The line for an MCP server under the last step of the turn that ran into it, while the steps show; folded, the same
    // lines stand right under them, above the answer.
    under(s, st, i, j) {
      const m = serverOf(s, st, i, j), it = ctx.items(s.id)?.[i];
      if (!m || it?.k !== 'steps' || it.steps.slice(j + 1).some((x, d) => serverOf(s, x, i, j + 1 + d)?.name === m.name)) return '';
      return lineHTML(s, m);
    },
    answer(s, it, i, html) {
      const prev = i > 0 ? ctx.items(s.id)?.[i - 1] : undefined, seen = new Set<string>();
      if (prev?.k !== 'steps') return html;
      const lines = prev.steps.map((st, j) => { const m = serverOf(s, st, i - 1, j); if (!m || seen.has(m.name)) return ''; seen.add(m.name); return lineHTML(s, m); }).join('');
      return lines ? `<div class="in-mqs" data-x>${lines}</div>${html}` : html;
    },
    rows(s) {
      if (shown && shown !== s.id) closeMcp(false);
      return ask?.id === s.id ? `<div class="in-ask" role="alertdialog" aria-label="${esc(ask.label)}">${askHTML(s.agent, ask.v, ask.label)}</div>` : ''; },
    key(e) {
      const take = () => { e.preventDefault(); e.stopImmediatePropagation(); return true; };
      // The question takes esc, and ⏎ when nothing is being written.
      if (ask && !dirs && !e.isComposing && (ask.id ? ask.id === ctx.current()?.id : !!win.querySelector('.pop.on.in-askpop'))) {
        const t = e.target as HTMLElement, empty = t === ctx.ta ? !ctx.ta.value.trim() && !win.querySelector('.c-files > *') : !t.closest('input,textarea,button,[role="button"]');
        if (e.key === 'Escape') { noFull(); return take(); }
        if (e.key === 'Enter' && !e.shiftKey && empty) { if (!e.repeat) win.querySelector<HTMLElement>('.in-ask [data-ok]')?.click(); return take(); }
      }
      if (!dirs || e.isComposing) return false;
      const n = dirs.rows.length + 1;
      if (e.key === 'Escape') { closeDirs(); return take(); }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { dirs.at = (dirs.at + (e.key === 'ArrowDown' ? 1 : -1) + n) % n; drawDirs(); return take(); }
      if (e.key === 'Enter') { if (!e.repeat) sheet.querySelector<HTMLElement>('.in-sel')?.click(); return take(); }
      return false;
    },
  };
}

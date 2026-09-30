// The composer beyond typing: any file, dropped anywhere on the window or picked, goes with a message; a right click on
// one copies it, its path, or opens it. The / menu shows the window's own commands above the agent's, and /add-dir
// gives a session one more folder.
import type { File as Upload, Pic, Sess } from '../../electron/agents/types';
import type { Feature, PageCtx } from './ctx';
import './input.css';

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
    const name = f.name || `图片 ${k + 1}.png`, kind = kindOf(name, f.type, dir), p = window.agents?.pathOf?.(f) ?? '', path = p.startsWith('/') ? p : '';
    const view = kind === 'img' && f.size <= CAP ? URL.createObjectURL(f) : '';
    if (path) { out.push({ name, path, view, kind, size: f.size }); continue; }
    if (dir || f.size > CAP) { if (view) URL.revokeObjectURL(view); say(dir ? `找不到「${name}」在哪，文件夹要从访达拖进来` : `「${name}」超过 30 MB，从访达拖进来就只给它路径`); continue; }
    try { out.push({ name, url: await dataURL(f), view, kind, size: f.size }); }
    catch { if (view) URL.revokeObjectURL(view); say(`读不了「${name}」`); }
  }
  return out;
}
// The type as a small tile: a folder, or the extension.
const tile = (kind: Kind, name: string) => kind === 'dir' ? `<i class="in-fi in-k-dir">${IC.dir}</i>`
  : `<i class="in-fi in-k-${kind}">${esc((extOf(name) || 'FILE').slice(0, 4).toUpperCase())}</i>`;
// The composer's files: pictures as small pictures, anything else a chip with its type, name and size.
export const chipsHTML = (fs: Attached[]) => fs.map((f, k) => {
  const at = `data-in-k="${k}" data-kind="${f.kind}"${f.path ? ` data-ref="${esc(f.path)}"` : ''} data-label="${esc(f.name)}"`;
  if (f.kind === 'img' && f.view) return `<span class="c-pic" ${at}><button type="button" class="pic" data-act="view" data-tip="看大图"><img src="${f.view}" alt="${esc(f.name)}"></button><i data-act="unfile" data-k="${k}" aria-label="去掉">✕</i></span>`;
  const big = f.kind !== 'dir' && f.size > CAP, sub = f.kind === 'dir' ? '文件夹' : big ? `${sizeOf(f.size)} · 只给路径` : sizeOf(f.size);
  return `<span class="in-fc${big ? ' in-big' : ''}" role="button" tabindex="0" data-act="in-open" ${at} data-tip="${esc(f.path || f.name)}" aria-label="打开 ${esc(f.name)}">`
    + `${tile(f.kind, f.name)}<span class="in-fn"><b>${esc(f.name)}</b><small>${esc(sub)}</small></span><button type="button" class="in-x" data-act="unfile" data-k="${k}" aria-label="去掉 ${esc(f.name)}">${IC.x}</button></span>`;
}).join('');
// A file a message went with, by its path: a smaller chip that opens it.
export function fileTag(f: Pic & { path: string }) {
  const dir = f.path.endsWith('/'), kind = kindOf(f.name, '', dir);
  return `<button type="button" class="in-mfc" data-act="in-open" data-kind="${kind}" data-ref="${esc(f.path)}" data-label="${esc(f.name)}" data-tip="${esc(f.path)}" aria-label="打开 ${esc(f.name)}">`
    + `${tile(kind, f.name)}<span>${esc(f.name)}</span>${dir ? '<small>文件夹</small>' : ''}</button>`;
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
  const rows = picks.map(([v, d, g], k) => (k && !!picks[k - 1][2] === !!g ? '' : `<p class="in-h">${g ? '窗口里的' : `${esc(agent)} 的`}</p>`)
    + `<button type="button" data-act="pickcmd" data-v="${esc(v)}"${k === at ? ' class="on"' : ''}><code>${esc(v)}</code><span>${esc(d)}</span></button>`).join('');
  return rows && `${rows}<p class="in-f"><kbd>↑</kbd><kbd>↓</kbd> 选 · <kbd>⏎</kbd> 或 <kbd>Tab</kbd> 填进去 · <kbd>esc</kbd> 收起</p>`;
}
const home = (p: string) => p.replace(/^\/(Users|home)\/[^/]+(?=\/|$)/, '~');
const day = (ms: number) => {
  const d = Math.floor((new Date().setHours(24, 0, 0, 0) - ms) / 864e5);
  return d <= 0 ? '今天' : d === 1 ? '昨天' : d < 7 ? `${d} 天前` : `${new Date(ms).getMonth() + 1} 月 ${new Date(ms).getDate()} 日`;
};

export function mountInput(ctx: PageCtx): Feature {
  const { win } = ctx;
  own = ctx.own;
  veil = document.createElement('div'); veil.className = 'in-drop'; veil.hidden = true; win.append(veil);
  function showVeil(n: number) {
    if (!veil!.hidden) return;
    ctx.closeMenu();
    const s = ctx.current(), head = win.querySelector('.m-head')?.getBoundingClientRect(), w = win.getBoundingClientRect();
    veil!.style.top = `${head && head.height ? head.bottom - w.top : 0}px`;
    veil!.innerHTML = `<div class="in-dt">${IC.tray}<b>放下就行</b><small>${n ? `${n} 个文件，` : ''}放进${s ? `「${esc(s.title)}」` : '新会话'}</small></div>`;
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
    const opens = [...t.path && !t.dir && !t.src && ctx.chat() ? [b('side', '在右边打开', IC.side)] : [], ...t.path && !t.dir ? [b('app', '用默认的 app 打开', IC.out)] : [],
      ...t.path ? [b('finder', '在访达里显示', IC.finder)] : []];
    ctx.menu(b('copy', '复制', IC.copy) + (t.path ? b('path', '复制路径', IC.path) : '') + (opens.length ? `<span class="sep"></span>${opens.join('')}` : ''),
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
        if (how === 'path') { ctx.toast('只复制了它的路径'); return; }
      }
      ctx.toast(`复制了「${t.name}」`);
    } catch { ctx.toast(`复制不了「${t.name}」`); }
  }
  // A file opens on the right in a session, with Quick Look before there is one; a folder opens in Finder.
  function open(el: HTMLElement) {
    const p = el.dataset.ref;
    if (!p) { ctx.toast('发出去以后才能打开'); return; }
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
    if (p.replace(/\/+$/, '') === s.cwd) { ctx.toast('这就是它自己的文件夹'); return; }
    if (s.dirs?.includes(p)) { ctx.toast('这个文件夹已经加过了'); return; }
    if (await ctx.tryCall(`/sessions/${s.id}/dirs`, { dirs: [...s.dirs ?? [], p] })) ctx.cue('mark');
  }
  function drawDirs() {
    if (!dirs) return;
    const s = ctx.byId(dirs.id), have = s?.dirs ?? [], at = dirs.at;
    sheet.innerHTML = `<div class="in-veil" data-act="in-dir-x"></div><div class="in-sh" role="dialog" aria-label="再加一个文件夹"><p class="in-shh"><b>再加一个文件夹</b><span>这个会话里它也能动</span></p>`
      + `<div class="in-drs" role="listbox">${dirs.rows.map((f, j) => `<button type="button" class="in-dr${j === at ? ' in-sel' : ''}${have.includes(f.path) ? ' in-on' : ''}" data-act="in-dir" data-v="${esc(f.path)}" role="option" aria-selected="${j === at}">`
        + `${IC.dir}<b>${esc(f.name)}</b><span>${esc(home(f.path))}</span><em>${have.includes(f.path) ? '加过了' : f.used ? day(f.used) : ''}</em></button>`).join('')}`
      + `${dirs.rows.length ? '<i class="in-sep"></i>' : ''}<button type="button" class="in-dr in-other${at === dirs.rows.length ? ' in-sel' : ''}" data-act="in-dir-other" role="option" aria-selected="${at === dirs.rows.length}">${IC.plus}<b>选别的文件夹…</b></button></div>`
      + `<div class="in-shf"><span><kbd>↑</kbd><kbd>↓</kbd> 选 · <kbd>⏎</kbd> 加上</span><button type="button" class="btn" data-act="in-dir-x">取消 <kbd>esc</kbd></button></div></div>`;
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
  // Typed with a path it adds that folder; without one it asks which.
  ctx.own.set('dirs', (s, arg) => {
    if (!s) { ctx.toast('开了会话再给它加文件夹'); return; }
    if (arg.startsWith('/')) void addDir(s, arg);
    else void openDirs(s);
  });

  return {
    act(a, el) {
      if (a === 'in-open') { open(el); return true; }
      if (a === 'in-dir') { void pickDir(el.dataset.v!); return true; }
      if (a === 'in-dir-other') { const id = dirs?.id; closeDirs(); void window.agents?.folder().then(p => { const s = id ? ctx.byId(id) : undefined; if (p && s) void addDir(s, p); }); return true; }
      if (a === 'in-dir-x') { closeDirs(); return true; }
      if (a === 'in-a' && target) {
        const t = target, v = el.dataset.v;
        ctx.closeMenu();
        if (v === 'copy') void copy(t);
        else if (v === 'path' && t.path) void navigator.clipboard.writeText(t.path).then(() => ctx.toast('复制了路径'), () => ctx.toast('复制不了路径'));
        else if (v === 'side') open(t.el);
        else if (v === 'app' && t.path) void window.agents?.openPath?.(t.path);
        else if (v === 'finder' && t.path) void window.agents?.revealFile?.(t.path.replace(/\/$/, ''));
        return true;
      }
      return false;
    },
    esc() { if (!veil?.hidden) { hideVeil(); return true; } return closeDirs(); },
    key(e) {
      if (!dirs || e.isComposing) return false;
      const take = () => { e.preventDefault(); e.stopImmediatePropagation(); return true; }, n = dirs.rows.length + 1;
      if (e.key === 'Escape') { closeDirs(); return take(); }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { dirs.at = (dirs.at + (e.key === 'ArrowDown' ? 1 : -1) + n) % n; drawDirs(); return take(); }
      if (e.key === 'Enter') { if (!e.repeat) sheet.querySelector<HTMLElement>('.in-sel')?.click(); return take(); }
      return false;
    },
  };
}

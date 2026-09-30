// 出手 (design points mt-n, mt-where, mt-who, mt-fuse): ⌘N drops a slip from the top of the window, over whatever you
// are doing. Its first line says what to do; the line under it says where that goes (a folder for a new session, a
// session already there, or 先存着 to throw later) and who takes it. ⏎ throws it and you stay where you are; ⌘⏎ throws it
// and follows it. A throw burns a three-second fuse first: nothing reaches the host until it has burnt, and ⌘Z takes it
// back into the slip. The slip is how a session starts: it replaces the new-session page, and an empty window opens it.
import type { Agent, Project, Sess } from '../../electron/agents/types';
import type { Feature, PageCtx } from './ctx';
import './slip.css';

type Dest = 'new' | 'sess' | 'keep';
type Tab = '' | 'where' | 'who';
// One line of the list: a folder, 别的文件夹…, a session, 先存着, a kept note, an agent, or a branch choice.
type Row = { kind: 'dir' | 'other' | 'sess' | 'keep' | 'note' | 'agent' | 'tree'; v: string; label: string; sub: string; when?: string; st?: string; on?: boolean; at?: number };
type Note = { id: string; text: string; at: number };
type Fuse = { id: number; text: string; dest: 'new' | 'sess'; target: string; agent: Agent; dir: string; tree: boolean; at: number; timer: number };

const FUSE = 3000;
const NAME: Record<Agent, string> = { claude: 'Claude Code', codex: 'Codex' };
// What each agent runs on when the host has not listed its models yet.
const PLAN: Record<Agent, string> = { claude: '用你的 Claude 订阅', codex: '用你的 ChatGPT 登录' };
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const home = (p: string) => p.replace(/^\/Users\/[^/]+/, '~');
const oneLine = (s: string, n = 80) => { const t = s.replace(/\s+/g, ' ').trim(); return t.length > n ? `${t.slice(0, n - 1)}…` : t; };
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const SPRING = 'linear(0,.054,.178,.329,.481,.617,.731,.82,.888,.936,.969,.99,1.003,1.01,1.014,1.015,1.014,1.012,1.01,1.008,1)';
const anim = (el: Element, kf: Keyframe[], ms: number, easing = 'cubic-bezier(.23,1,.32,1)') => reduced.matches ? null : el.animate(kf, { duration: ms, easing });
const store = {
  get(k: string) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k: string, v: string) { try { localStorage.setItem(k, v); } catch { /* not remembered, that is all */ } },
};
const TREE = '<svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="4.5" cy="3.5" r="1.5"/><circle cx="4.5" cy="12.5" r="1.5"/><circle cx="11.5" cy="6.5" r="1.5"/><path d="M4.5 5v6M11.5 8c0 2.5-4 2-7 3.2"/></svg>';
const FOLDER = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 4.6c0-.6.4-1.1 1-1.1h2.9l1.5 1.6h4.6c.6 0 1 .4 1 1v5.4c0 .6-.4 1-1 1h-9c-.6 0-1-.4-1-1z"/></svg>';

// When a folder was last used, the way the list says it.
function when(at?: number) {
  if (!at) return '';
  const d = new Date(at), now = new Date(), days = Math.round((new Date(now.toDateString()).getTime() - new Date(d.toDateString()).getTime()) / 864e5);
  if (Date.now() - at < 3600e3) return '刚用过';
  if (days <= 0) return '今天';
  if (days === 1) return '昨天';
  if (days < 7) return `${days} 天前`;
  if (days < 14) return '上周';
  return d.getFullYear() === now.getFullYear() ? `${d.getMonth() + 1} 月 ${d.getDate()} 日` : `${d.getFullYear()} 年 ${d.getMonth() + 1} 月`;
}
function ago(at: number) {
  const m = Math.floor((Date.now() - at) / 60000);
  return m < 1 ? '刚' : m < 60 ? `${m} 分钟前` : m < 1440 ? `${Math.floor(m / 60)} 小时前` : `${Math.floor(m / 1440)} 天前`;
}
// What a message thrown to a session will do there.
const say = (s: Sess) => s.st === 'work' || s.st === 'pack' ? '在跑，这一轮完了接着' : s.st === 'wait' ? '在等你，接着干完再说' : s.st === 'err' || s.stopped ? '停了，发过去接着做' : '做完了，接着往下做';

export function mountSlip(ctx: PageCtx): Feature {
  const { win } = ctx;
  const el = document.createElement('div');
  el.className = 'slip'; el.hidden = true; el.setAttribute('role', 'dialog'); el.setAttribute('aria-label', '纸条：写一句，抛给一个会话');
  el.innerHTML = '<div class="mt-box"><span class="mt-dot" aria-hidden="true"></span><textarea class="mt-ta" rows="1" placeholder="冒出来的念头，写一句" aria-label="要它做什么" spellcheck="false"></textarea></div><div class="mt-dest"></div>'
    + '<div class="mt-pick" hidden><div class="mp-h"><span class="mp-tabs" role="tablist" aria-label="挑什么"><button type="button" role="tab" data-act="slip-tab" data-t="where">去处</button><button type="button" role="tab" data-act="slip-tab" data-t="who">用谁</button></span>'
    + '<input class="mp-q" type="text" autocomplete="off" spellcheck="false" aria-label="筛选"><span class="mp-k"><span><kbd>↑</kbd><kbd>↓</kbd></span><span><kbd>⏎</kbd> 选</span><span><kbd>⇥</kbd> 换</span><span><kbd>esc</kbd> 回去</span></span></div>'
    + '<div class="mp-list" role="listbox" aria-label="去处"></div></div>';
  // The fuse: one quiet line above the composer while a throw burns, and a moment after it lands.
  const bar = document.createElement('div');
  bar.className = 'mt-fuse'; bar.hidden = true; bar.setAttribute('role', 'status');
  const scrim = document.createElement('div'); scrim.className = 'mt-scrim'; scrim.setAttribute('aria-hidden', 'true');
  win.append(scrim, el, bar);
  const ta = el.querySelector<HTMLTextAreaElement>('.mt-ta')!, dot = el.querySelector<HTMLElement>('.mt-dot')!, dest = el.querySelector<HTMLElement>('.mt-dest')!;
  const pick = el.querySelector<HTMLElement>('.mt-pick')!, q = el.querySelector<HTMLInputElement>('.mp-q')!, list = el.querySelector<HTMLElement>('.mp-list')!;

  const S = {
    open: false, dest: 'new' as Dest, target: '', agent: (store.get('agents.agent') === 'codex' ? 'codex' : 'claude') as Agent,
    dir: store.get('agents.project') ?? '', tree: store.get('agents.tree') !== 'off', tab: '' as Tab, i: 0, rows: [] as Row[],
    projects: [] as Project[], from: null as HTMLElement | null,
  };
  // 先存着: notes kept in this window, thrown later from the same list.
  let notes: Note[] = [];
  try { notes = (JSON.parse(store.get('agents.kept') ?? '[]') as Note[]).filter(n => typeof n?.text === 'string'); } catch { notes = []; }
  const saveNotes = () => store.set('agents.kept', JSON.stringify(notes));
  const fuses: Fuse[] = [];
  let seq = 0;
  ta.value = store.get('agents.slip') ?? '';
  const saveDraft = () => store.set('agents.slip', ta.value);
  // A throw's words stay on the slip while it folds away; they leave it once it is gone, or as soon as it is wanted again.
  let spent = false;
  const clearSpent = () => { if (!spent) return; spent = false; ta.value = ''; charge(); };

  async function loadProjects() {
    const r = await ctx.call<{ list: Project[] }>('/projects').catch(() => null);
    if (!r) return;
    S.projects = r.list;
    if (!S.dir) S.dir = r.list[0]?.path ?? '';
    if (S.open) { renderDest(); if (S.tab) renderPick(true); }
  }
  const project = () => S.projects.find(p => p.path === S.dir);
  const dirName = () => project()?.name ?? S.dir.split('/').filter(Boolean).pop() ?? '';
  const queueable = () => ctx.sessions().filter(s => !s.archived && !s.term && !s.gone).sort((a, b) => b.updated - a.updated);
  const agents = () => Object.keys(ctx.catalog() ?? NAME) as Agent[];
  const modelsOf = (a: Agent) => ctx.catalog()?.[a]?.models.map(m => m[1]).slice(0, 3).join(' · ') || PLAN[a];

  // ---------- drawing ----------
  function charge() {
    dot.style.setProperty('--charge', String(Math.min(1, Math.max(.3, .3 + ta.value.trim().length / 30))));
    ta.style.height = 'auto'; ta.style.height = `${Math.min(112, ta.scrollHeight)}px`;
  }
  // The destination reads as one sentence: 去「新会话 · app」用「Claude Code」新 worktree.
  function renderDest() {
    const t = S.dest === 'sess' ? ctx.byId(S.target) : undefined, p = project(), expanded = (m: Tab) => String(S.tab === m);
    const tok = S.dest === 'new' ? `<span class="mt-ic ic-new" aria-hidden="true"></span>新会话<i class="sep">·</i>${S.dir ? esc(dirName()) : '挑一个文件夹'}`
      : S.dest === 'keep' ? '<span class="mt-ic ic-keep" aria-hidden="true"></span>先存着'
      : t ? `<b class="st-${t.st}" aria-hidden="true">✦</b><span class="mt-tt">${esc(t.title)}</span>` : '没有能排进的会话';
    const who = `<span class="mt-w">用</span><button type="button" class="mt-tok" data-act="slip-who" aria-haspopup="listbox" aria-expanded="${expanded('who')}">${NAME[S.agent]}<i aria-hidden="true">▾</i></button>`
      + (p && !p.git ? '' : `<button type="button" class="mt-sw" data-act="slip-tree" aria-pressed="${S.tree}" data-tip="${S.tree ? '单独一个 worktree，不碰你现在的分支' : '就在这个文件夹现在的分支上改'}">${TREE}${S.tree ? '新 worktree' : '当前分支'}</button>`);
    const keys = `<span class="mt-k"><span><kbd>⇥</kbd> 去处</span><span><kbd>⏎</kbd> ${S.dest === 'keep' ? '存下' : '抛出去'}</span>${S.dest === 'keep' ? '' : '<span><kbd>⌘</kbd><kbd>⏎</kbd> 跟过去</span>'}</span>`;
    dest.innerHTML = `<span class="mt-w">${S.dest === 'sess' ? '排进' : '去'}</span><button type="button" class="mt-tok" data-act="slip-where" aria-haspopup="listbox" aria-expanded="${expanded('where')}">${tok}<i aria-hidden="true">▾</i></button>`
      + (S.dest === 'new' ? who : `<span class="mt-say">${S.dest === 'keep' ? '不开跑，不花 token' : t ? say(t) : ''}</span>`) + keys;
  }
  // The list: folders, sessions and 先存着 under 去处; the agents (and the branch) under 用谁. Typing narrows it.
  function groups(): [string, Row[]][] {
    const f = q.value.trim().toLowerCase(), fits = (x: Row) => !f || `${x.label} ${x.sub}`.toLowerCase().includes(f);
    if (S.tab === 'who') {
      const p = project(), who: [string, Row[]][] = [['新会话用', agents().map(a => ({ kind: 'agent' as const, v: a, label: NAME[a], sub: modelsOf(a), on: S.agent === a })).filter(fits)]];
      if (!p || p.git) who.push(['在哪条分支上', [{ kind: 'tree' as const, v: 'on', label: '新 worktree', sub: '单独一个 worktree，不碰你现在的分支', on: S.tree }, { kind: 'tree' as const, v: 'off', label: '当前分支', sub: '就在这个文件夹现在的分支上改', on: !S.tree }].filter(fits)]);
      return who.filter(g => g[1].length);
    }
    const ps = S.dir && !S.projects.some(p => p.path === S.dir) ? [{ path: S.dir, name: dirName(), git: true }, ...S.projects] : S.projects;
    const dirs: Row[] = ps.map(p => ({ kind: 'dir' as const, v: p.path, label: p.name, sub: home(p.path), when: when(p.used), on: S.dest === 'new' && S.dir === p.path })).filter(fits);
    if (!f) dirs.push({ kind: 'other', v: '', label: '别的文件夹…', sub: '弹出选文件夹的窗口' });
    const sess: Row[] = queueable().map(s => ({ kind: 'sess' as const, v: s.id, label: s.title, sub: say(s), st: s.st, on: S.dest === 'sess' && S.target === s.id })).filter(fits);
    const keep: Row[] = [{ kind: 'keep' as const, v: '', label: '先存着', sub: '不开跑，不花 token', on: S.dest === 'keep' }, ...notes.map(n => ({ kind: 'note' as const, v: n.id, label: oneLine(n.text), sub: `${ago(n.at)}存的`, at: n.at }))].filter(fits);
    return ([['新会话，在', dirs], ['排进', sess], ['先存着', keep]] as [string, Row[]][]).filter(g => g[1].length);
  }
  const lit = (label: string) => {
    const f = q.value.trim(), at = f ? label.toLowerCase().indexOf(f.toLowerCase()) : -1;
    return at < 0 ? esc(label) : `${esc(label.slice(0, at))}<u>${esc(label.slice(at, at + f.length))}</u>${esc(label.slice(at + f.length))}`;
  };
  function renderPick(keepI = false) {
    const gs = groups();
    S.rows = gs.flatMap(g => g[1]);
    if (!keepI) S.i = q.value.trim() ? 0 : Math.max(0, S.rows.findIndex(x => x.on));
    S.i = Math.max(0, Math.min(S.i, S.rows.length - 1));
    let n = 0;
    const ic = (x: Row) => x.kind === 'agent' ? `<span class="mp-ic ag">${esc(x.label[0])}</span>` : x.kind === 'sess' ? `<span class="mp-ic"><b class="st-${x.st}">✦</b></span>`
      : x.kind === 'keep' || x.kind === 'note' ? `<span class="mp-ic"><i class="mt-ic ${x.kind === 'keep' ? 'ic-keep' : 'ic-note'}"></i></span>` : x.kind === 'tree' ? `<span class="mp-ic">${TREE}</span>` : `<span class="mp-ic">${FOLDER}</span>`;
    list.innerHTML = gs.map(([h, rows]) => `<div class="mp-g" role="group" aria-label="${esc(h)}"><p class="mp-gh">${esc(h)}</p>${rows.map(x => {
      const j = n++;
      return `<div class="mp-it${j === S.i ? ' on' : ''}${x.kind === 'other' ? ' other' : ''}" role="option" aria-selected="${j === S.i}" data-act="slip-row" data-j="${j}">${ic(x)}<span class="mp-l">${lit(x.label)}</span>`
        + `<span class="mp-s${x.kind === 'dir' ? '' : ' say'}">${esc(x.sub)}</span>${x.when ? `<em>${esc(x.when)}</em>` : ''}${x.on ? '<i class="mp-ck" aria-label="现在的去处">✓</i>' : ''}`
        + `${x.kind === 'note' ? `<i class="mp-x" data-act="slip-drop" data-id="${esc(x.v)}" aria-label="扔掉这条">✕</i>` : ''}</div>`;
    }).join('')}</div>`).join('') || `<p class="mp-none">没有对得上「${esc(q.value.trim())}」的。换几个字，或者 esc 回到纸条。</p>`;
    el.querySelectorAll<HTMLElement>('.mp-tabs button').forEach(b => b.setAttribute('aria-selected', String(b.dataset.t === S.tab)));
    list.setAttribute('aria-label', S.tab === 'who' ? '用谁' : '去处');
    q.placeholder = S.tab === 'who' ? '筛 agent 或模型' : '打几个字筛文件夹和会话';
    list.querySelector('.mp-it.on')?.scrollIntoView({ block: 'nearest' });
  }

  // ---------- open and close ----------
  // The slip sits over the conversation's column, under the window's top line.
  function place() {
    const w = win.getBoundingClientRect(), bd = (win.querySelector('.bd') ?? win).getBoundingClientRect(), width = Math.min(680, bd.width - 32);
    Object.assign(el.style, { left: `${bd.left - w.left + (bd.width - width) / 2}px`, top: `${bd.top - w.top + 10}px`, width: `${width}px` });
  }
  // The list stays inside the window, however small: it scrolls in what room is left under it.
  function fit() {
    if (pick.hidden) return;
    list.style.maxHeight = '';
    const room = win.getBoundingClientRect().bottom - list.getBoundingClientRect().top - 22;
    list.style.maxHeight = `${Math.round(Math.max(96, Math.min(258, room)))}px`;
  }
  addEventListener('resize', () => { if (S.open) { place(); fit(); } });
  function open(o: { dest?: Dest; target?: string } = {}) {
    if (S.open) { if (o.dest) { S.dest = o.dest; S.target = o.target ?? S.target; renderDest(); } closePick(); return; }
    ctx.closeMenu(); clearSpent();
    S.open = true; S.dest = o.dest ?? 'new'; S.target = o.target ?? S.target; S.from = document.activeElement as HTMLElement | null;
    place(); renderDest(); el.hidden = false; win.classList.add('slip-on'); charge();
    // One motion: it drops a little and is solid early in the drop, so nothing behind reads through it.
    el.style.transformOrigin = '';
    anim(el, [{ opacity: 0, transform: 'translateY(-8px)' }, { opacity: 1, offset: .35 }, { opacity: 1, transform: 'none' }], 260, SPRING);
    ta.focus({ preventScroll: true }); ta.setSelectionRange(ta.value.length, ta.value.length);
    ctx.cue('open', .5);
    void loadProjects();
  }
  // Back to what you were doing: the element you left, or the composer.
  function back() {
    const f = S.from; S.from = null;
    const typed = !!f?.matches('textarea,input,[contenteditable="true"]');
    if (typed && f?.isConnected && !el.contains(f) && !f.closest('[hidden]') && !(f as HTMLInputElement).disabled) f.focus({ preventScroll: true });
    else if (ctx.current() && !ctx.ta.disabled) ctx.ta.focus({ preventScroll: true });
    // Nowhere to go back to (an empty window): the window's keys, not a hidden field's.
    else if (el.contains(document.activeElement)) (document.activeElement as HTMLElement).blur();
  }
  // Put away: its words stay for next time.
  function close(focus = true) {
    if (!S.open) return;
    closePick(false); S.open = false; saveDraft(); win.classList.remove('slip-on');
    const a = anim(el, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-8px)' }], 180, 'cubic-bezier(.4,0,1,1)');
    if (a) a.onfinish = () => { if (!S.open) el.hidden = true; }; else el.hidden = true;
    if (focus) back();
  }
  function openPick(tab: 'where' | 'who') {
    const was = S.tab; S.tab = tab; q.value = '';
    pick.hidden = false; renderPick(); renderDest(); fit();
    if (!was) { anim(pick, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], 200); ctx.tick(); }
    q.focus({ preventScroll: true });
  }
  function closePick(focus = true) {
    if (!S.tab) return;
    S.tab = ''; pick.hidden = true; renderDest();
    if (focus) ta.focus({ preventScroll: true });
  }
  async function pickAt(j: number) {
    const x = S.rows[j];
    if (!x) return;
    ctx.tick();
    if (x.kind === 'other') {
      const p = await window.agents?.folder();
      if (!p) { q.focus({ preventScroll: true }); return; }
      S.dir = p; S.dest = 'new';
    } else if (x.kind === 'dir') { S.dir = x.v; S.dest = 'new'; }
    else if (x.kind === 'sess') { S.dest = 'sess'; S.target = x.v; }
    else if (x.kind === 'keep') S.dest = 'keep';
    else if (x.kind === 'agent') { S.agent = x.v as Agent; store.set('agents.agent', S.agent); if (S.dest !== 'new') S.dest = 'new'; }
    else if (x.kind === 'tree') { S.tree = x.v === 'on'; store.set('agents.tree', x.v); }
    else if (x.kind === 'note') {
      // A kept note comes back into the slip; what the slip held takes its place in the list.
      const n = notes.find(m => m.id === x.v), had = ta.value.trim();
      if (!n) return;
      notes = notes.filter(m => m !== n);
      if (had && had !== n.text) notes.unshift({ id: `n${Date.now().toString(36)}`, text: had, at: Date.now() });
      saveNotes(); ta.value = n.text; charge(); saveDraft();
      if (S.dest === 'keep') S.dest = 'new';
    }
    closePick();
  }

  // ---------- throwing ----------
  function throwIt(follow: boolean) {
    const text = ta.value.trim();
    if (!text) { anim(el, [{ transform: 'none' }, { transform: 'translateX(-6px)' }, { transform: 'translateX(5px)' }, { transform: 'translateX(-3px)' }, { transform: 'none' }], 260); return; }
    if (S.dest === 'new' && !S.dir) { ctx.toast('先挑一个文件夹'); openPick('where'); return; }
    if (S.dest === 'sess' && !ctx.byId(S.target)) { ctx.toast('那个会话不在了，换一个去处'); openPick('where'); return; }
    fold();
    spent = true; store.set('agents.slip', '');
    if (S.dest === 'keep') {
      notes.unshift({ id: `n${Date.now().toString(36)}`, text, at: Date.now() }); saveNotes();
      ctx.cue('on', .6); tell(`存下了：${oneLine(text, 40)} · 在「先存着」里`);
      back(); return;
    }
    const f: Fuse = { id: ++seq, text, dest: S.dest, target: S.target, agent: S.agent, dir: S.dir, tree: S.tree, at: performance.now(), timer: 0 };
    ctx.cue('send', .6);
    // ⌘⏎ goes at once and you go with it: you watch it start, and esc there stops it.
    if (follow) { S.from = null; void go(f, true); return; }
    back();
    f.timer = window.setTimeout(() => ignite(f), FUSE);
    fuses.push(f); burn();
  }
  // The slip folds back into its star and is gone.
  function fold() {
    closePick(false); S.open = false; win.classList.remove('slip-on');
    const r = dot.getBoundingClientRect(), m = el.getBoundingClientRect(), dx = r.left + r.width / 2 - m.left, dy = r.top + r.height / 2 - m.top;
    el.style.transformOrigin = `${dx}px ${dy}px`;
    const a = anim(el, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.94)' }], 200, 'cubic-bezier(.4,0,1,1)');
    const gone = () => { if (!S.open) { el.hidden = true; clearSpent(); } };
    if (a) a.onfinish = gone; else gone();
  }
  function ignite(f: Fuse) {
    const i = fuses.indexOf(f);
    if (i < 0) return;
    // The burnt line stays up until the word of how it landed takes its place.
    fuses.splice(i, 1); clearTimeout(f.timer); burn(true);
    void go(f, false);
  }
  // Only here does anything reach the host.
  async function go(f: Fuse, follow: boolean) {
    try {
      let id = f.target;
      if (f.dest === 'sess') await ctx.call(`/sessions/${f.target}/send`, { text: f.text, files: [] });
      else {
        const r = await ctx.call<{ id: string }>('/sessions', { agent: f.agent, cwd: f.dir, tree: f.tree, text: f.text, files: [], ...settings(f.agent) });
        id = String(r.id);
        store.set('agents.project', f.dir);
      }
      // With nothing on screen there is nowhere to stay: a new session opens.
      if (follow || (f.dest === 'new' && !ctx.current())) {
        for (let k = 0; k < 60 && !ctx.byId(id); k++) await new Promise(r => setTimeout(r, 50));
        if (ctx.byId(id)) { ctx.open(id); requestAnimationFrame(() => ctx.ta.focus({ preventScroll: true })); }
        if (!fuses.length) bar.hidden = true;
        return;
      }
      const s = ctx.byId(id);
      if (f.dest === 'sess') tell(s && (s.st === 'work' || s.st === 'pack' || s.st === 'wait') ? `排进了「${oneLine(s.title, 24)}」：这一轮完了接着` : `发给「${oneLine(s?.title ?? '', 24)}」了，直接开始`);
      else tell(`开跑了：${oneLine(f.text, 40)}`, id);
    } catch (e) {
      // What did not reach the host is not lost: it goes back into the slip.
      clearSpent(); if (!fuses.length) bar.hidden = true;
      ta.value = ta.value.trim() ? `${f.text}\n${ta.value}` : f.text; saveDraft();
      ctx.toast(`${e instanceof Error ? e.message : String(e)} · 字回到纸条里了，⌘N 打开`, true);
    }
  }
  // The model, effort and mode last picked for this agent, when the host still offers them; otherwise its defaults.
  // 完全放开 is asked for one session at a time and never carried to a new one.
  function settings(a: Agent) {
    const c = ctx.catalog()?.[a], out: Record<string, string> = {};
    if (!c) return out;
    const m = store.get(`agents.${a}.model`), e = store.get(`agents.${a}.effort`), md = store.get(`agents.${a}.mode`);
    if (m && c.models.some(x => x[0] === m)) out.model = m;
    if (e && c.efforts.includes(e)) out.effort = e;
    if (md && md !== 'bypassPermissions' && md !== 'full' && c.modes.some(x => x[0] === md)) out.mode = md;
    return out;
  }
  // ⌘Z before it has burnt: the newest throw comes back into the slip, and not a token was spent.
  function unthrow() {
    const f = fuses.pop();
    if (!f) return false;
    clearTimeout(f.timer); burn(); clearSpent();
    const had = ta.value.trim();
    ta.value = had ? `${f.text}\n${had}` : f.text;
    S.agent = f.agent; S.dir = f.dir; S.tree = f.tree;
    open({ dest: f.dest, target: f.target });
    ctx.cue('back', .7);
    return true;
  }

  // ---------- the fuse line ----------
  let tellTimer = 0, raf = 0, shown = '';
  function placeBar() {
    const w = win.getBoundingClientRect(), box = win.querySelector<HTMLElement>('.composer:not([hidden]) .c-box'), r = box?.offsetParent ? box.getBoundingClientRect() : null;
    const bd = (win.querySelector('.bd') ?? win).getBoundingClientRect();
    bar.style.left = `${r ? r.left - w.left + r.width / 2 : bd.left - w.left + bd.width / 2}px`;
    bar.style.bottom = `${r ? w.bottom - r.top + 10 : 28}px`;
  }
  function show(html: string) {
    const was = !bar.hidden;
    bar.innerHTML = html; bar.hidden = false; placeBar();
    if (was) { const t = bar.querySelector('.mt-ft'); if (t && t.textContent !== shown) anim(t, [{ opacity: 0 }, { opacity: 1 }], 180); }
    shown = bar.querySelector('.mt-ft')?.textContent ?? '';
    if (!was) anim(bar, [{ opacity: 0, transform: 'translate(-50%, 8px)' }, { opacity: 1, transform: 'translate(-50%, 0)' }], 320, SPRING);
  }
  // While throws burn, the newest shows with its ring; ⌘Z or 撤回 takes it back.
  function burn(keep = false) {
    clearTimeout(tellTimer); cancelAnimationFrame(raf);
    const f = fuses.at(-1);
    if (!f) { if (!keep) bar.hidden = true; return; }
    show(`<svg class="mt-ring" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6"/><circle class="fg" cx="8" cy="8" r="6" pathLength="100" stroke-dasharray="0 100" transform="rotate(-90 8 8)"/></svg>`
      + `<span class="mt-ft">抛出去了：${esc(oneLine(f.text, 36))}${fuses.length > 1 ? `<em> · 还有 ${fuses.length - 1} 个在烧</em>` : ''}</span><button type="button" data-act="slip-back">撤回 <kbd>⌘Z</kbd></button>`);
    const fg = bar.querySelector('.fg')!;
    const frame = () => {
      const t = performance.now() - f.at, k = Math.min(1, reduced.matches ? Math.floor(t / 1000) / 3 : t / FUSE);
      fg.setAttribute('stroke-dasharray', `${(k * 100).toFixed(1)} 100`);
      if (k < 1 && fuses.at(-1) === f) raf = requestAnimationFrame(frame);
    };
    frame();
  }
  // A word once it has landed, with the way there when it started a session.
  function tell(text: string, id = '') {
    if (fuses.length) return;
    show(`<span class="mt-ft">${esc(text)}</span>${id ? `<button type="button" data-act="slip-go" data-id="${esc(id)}">过去</button>` : ''}`);
    clearTimeout(tellTimer); tellTimer = window.setTimeout(() => { if (!fuses.length) bar.hidden = true; }, 4200);
  }

  ta.addEventListener('input', charge);
  q.addEventListener('input', () => renderPick());
  // A click anywhere else puts the slip away, words kept.
  document.addEventListener('pointerdown', e => {
    const t = e.target as Element;
    if (S.open && !el.contains(t) && !t.closest?.('[data-act="new"],.mt-fuse,.tip')) close(false);
  }, true);
  // A window that closes while a throw burns sends nothing: the words wait in the slip.
  addEventListener('pagehide', () => {
    if (!fuses.length) return;
    clearSpent();
    ta.value = [...fuses.map(f => f.text), ta.value.trim()].filter(Boolean).join('\n');
    fuses.splice(0).forEach(f => clearTimeout(f.timer)); saveDraft();
  });
  void loadProjects();

  return {
    act(a, target) {
      // A folder named on the act (the first run's 开始) is where the slip starts.
      if (a === 'new') { if (target.dataset.dir) { S.dir = target.dataset.dir; S.dest = 'new'; } open(); return true; }
      if (!a.startsWith('slip-')) return false;
      if (a === 'slip-where' || a === 'slip-who') { const m = a === 'slip-where' ? 'where' : 'who'; if (S.tab === m) closePick(); else openPick(m); }
      else if (a === 'slip-tab') openPick(target.dataset.t === 'who' ? 'who' : 'where');
      else if (a === 'slip-tree') { S.tree = !S.tree; store.set('agents.tree', S.tree ? 'on' : 'off'); ctx.tick(); renderDest(); ta.focus({ preventScroll: true }); }
      else if (a === 'slip-row') void pickAt(Number(target.dataset.j));
      else if (a === 'slip-drop') { notes = notes.filter(n => n.id !== target.dataset.id); saveNotes(); ctx.tick(); renderPick(true); q.focus({ preventScroll: true }); }
      else if (a === 'slip-back') unthrow();
      else if (a === 'slip-go') { const id = target.dataset.id ?? ''; bar.hidden = true; if (ctx.byId(id)) ctx.open(id); }
      return true;
    },
    esc() { if (!S.open) return false; close(); return true; },
    key(e) {
      const t = e.target as HTMLElement, mod = e.metaKey || e.ctrlKey;
      const consume = () => { e.preventDefault(); e.stopImmediatePropagation(); return true; };
      // Keys typed on the slip are the slip's: nothing behind it hears them.
      if (S.open && el.contains(t)) {
        if (e.isComposing || e.keyCode === 229) { e.stopImmediatePropagation(); return true; }
        if (t === q) {
          if (e.key === 'Escape') { closePick(); return consume(); }
          if (e.key === 'Tab') { openPick(S.tab === 'where' ? 'who' : 'where'); return consume(); }
          if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            const n = S.rows.length;
            if (n) { S.i = (S.i + (e.key === 'ArrowDown' ? 1 : -1) + n) % n; renderPick(true); }
            return consume();
          }
          if (e.key === 'Enter') { if (!e.repeat) void pickAt(S.i); return consume(); }
        } else {
          if (e.key === 'Escape') { if (S.tab) closePick(); else close(); return consume(); }
          if (e.key === 'Tab') { openPick(e.shiftKey ? 'who' : 'where'); return consume(); }
          if (e.key === 'Enter' && !e.shiftKey && !e.altKey && t === ta) { if (!e.repeat) throwIt(mod); return consume(); }
          if (mod && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'z' && !ta.value && fuses.length) { unthrow(); return consume(); }
        }
        if (mod && !e.shiftKey && e.key.toLowerCase() === 'n') { ta.focus({ preventScroll: true }); return consume(); }
        e.stopImmediatePropagation(); return true;
      }
      if (fuses.length && mod && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'z') {
        const editable = t.matches('input,textarea,select,[contenteditable="true"]');
        if (!editable || (t === ctx.ta && !ctx.ta.value)) { unthrow(); return consume(); }
      }
      return false;
    },
  };
}

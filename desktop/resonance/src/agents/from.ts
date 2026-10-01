// Where sessions come from, and where they go (ADR 0096). The Claude Code and Codex sessions on this Mac the window does
// not hold are listed quietly at the bottom of the long exposure's sky (「终端里还有 N 个」), or beside 已归档 in the
// classic list; one opens read-only, marked 终端, and a request it stopped on in the terminal can be answered here
// through the daemon that holds it (ADR 0049). 接手 asks once, inside the window, then takes it in.
// Deleting a session asks first when its worktree still holds work git would lose (the host answers 409, need
// 'force'); a clean one goes at once and is really deleted only after a few seconds, so 撤销 in the toast is real.
import type { Auth, Item, Live, Outside, Req, Sess, Step } from '../../electron/agents/types';
import type { Feature, PageCtx } from './ctx';
import { showSettings } from './settings';
import './from.css';

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const anim = (el: Element, kf: Keyframe[], ms: number, easing = 'cubic-bezier(.23,1,.32,1)') => reduced.matches ? null : el.animate(kf, { duration: ms, easing });
const H = new WeakMap<Element, string>();
const patch = (el: Element, html: string) => { if (H.get(el) === html) return false; el.innerHTML = html; H.set(el, html); return true; };
const home = (p: string) => p.replace(/^\/Users\/[^/]+/, '~');
const svg = (d: string, w = 13) => `<svg viewBox="0 0 16 16" width="${w}" height="${w}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const I = {
  term: svg('<rect x="1.8" y="2.8" width="12.4" height="10.4" rx="2"/><path d="M4.5 6.5 6.5 8l-2 1.5M8 10h3.5"/>'),
  take: svg('<path d="M8 2.5v7.2M5 6.8l3 3 3-3M3 10.8V12c0 .8.7 1.5 1.5 1.5h7c.8 0 1.5-.7 1.5-1.5v-1.2"/>'),
  chev: svg('<path d="M6 3.5 10.5 8 6 12.5"/>', 11),
  more: '<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor" aria-hidden="true"><circle cx="3.5" cy="8" r="1.3"/><circle cx="8" cy="8" r="1.3"/><circle cx="12.5" cy="8" r="1.3"/></svg>',
};
const K: Record<Step['k'], string> = { read: '读', edit: '改', bash: '跑', search: '搜', agent: '子任务', web: '网页', tool: '工具', say: '', think: '想' };
const kbd = (k: string) => `<kbd>${k}</kbd>`;
function age(ms: number) {
  const m = Math.floor((Date.now() - ms) / 60000);
  return m < 1 ? '刚刚' : m < 60 ? `${m} 分钟前` : m < 1440 ? `${Math.floor(m / 60)} 小时前` : `${Math.floor(m / 1440)} 天前`;
}
const resumeOf = (o: Outside) => `cd ${home(o.cwd)} && ${o.agent === 'codex' ? `codex resume ${o.id}` : `claude --resume ${o.id}`}`;
// How long a clean delete waits for 撤销 before the host really deletes it.
const HOLD = 5000;

export function mountFrom(ctx: PageCtx): Feature {
  const { win } = ctx;
  const T = {
    list: [] as Outside[], live: new Map<string, Live>(), group: false, cur: null as Outside | null, items: null as Item[] | null, why: '',
    take: false, auth: null as Auth | null, opened: new Set<number>(), busy: '', asked: [] as string[][], said: '', nudged: -1e9,
  };
  const liveOf = (id: string) => T.live.get(id);
  const reqOf = (id: string) => liveOf(id)?.req;

  // ---------- what the host says ----------
  async function refreshList() {
    const r = await ctx.call<{ sessions: Outside[] }>('/import').catch(() => null);
    if (!r) return;
    T.list = r.sessions;
    if (T.cur) T.cur = T.list.find(o => o.id === T.cur!.id) ?? T.cur;
    drawSky(); drawSide();
  }
  let liveKey = '';
  async function refreshLive() {
    const r = await ctx.call<{ live: Live[] }>('/import/live').catch(() => null);
    if (!r) return;
    const was = T.cur ? JSON.stringify(liveOf(T.cur.id) ?? null) : '';
    T.live = new Map(r.live.map(l => [l.id, l]));
    const key = JSON.stringify(r.live);
    if (key === liveKey) return;
    liveKey = key;
    // One that started asking and is not listed yet: the list is read again.
    if (r.live.some(l => l.req && !T.list.some(o => o.id === l.id))) void refreshList();
    drawSky();
    if (T.cur && JSON.stringify(liveOf(T.cur.id) ?? null) !== was) {
      // A request answered in the terminal, or the turn went on: what the transcript has now.
      const req = reqOf(T.cur.id);
      if (req?.id !== T.busy) T.busy = '';
      if (req) { ctx.cue('ask', .7); }
      void readItems();
      drawRO();
    }
  }
  async function readItems() {
    const o = T.cur; if (!o) return;
    const r = await ctx.call<{ items: Item[] }>(`/import/read?agent=${o.agent}&id=${encodeURIComponent(o.id)}&cwd=${encodeURIComponent(o.cwd)}`).catch((e: unknown) => e instanceof Error ? e.message : String(e));
    if (T.cur !== o) return;
    if (typeof r === 'string') { T.why = r; T.items = T.items ?? []; } else { T.items = r.items; T.why = ''; }
    drawRO(true);
  }

  // ---------- the sky's last group, and the classic list's line beside 已归档 ----------
  const sky = win.querySelector<HTMLElement>('.bw-more'), rowsEl = win.querySelector<HTMLElement>('.bw-rows');
  const stateOf = (o: Outside) => {
    const l = liveOf(o.id);
    return l?.st === 'wait' ? ['wait', l.req ? '等你批' : '等你'] : l?.st === 'work' ? ['work', '在干活'] : ['read', age(o.updated)];
  };
  function drawSky() {
    if (!sky) return;
    const must = (o: Outside) => o.id === T.cur?.id || !!reqOf(o.id);
    const shown = T.list.filter(o => T.group || must(o)), n = T.group ? T.list.length : T.list.length - shown.length;
    const rows = shown.map((o, k) => {
      const [st, word] = stateOf(o);
      return `<button type="button" class="bw-row fr-trow${o.id === T.cur?.id ? ' on' : ''}" data-act="fr-topen" data-fr="${esc(o.id)}" style="top:${2 + k * 27}px" aria-label="${esc(o.title)}，在终端里，${word}"><b>${esc(o.title)}</b><em class="fr-tg">终端</em><em class="st-${st}">${word}</em></button>`;
    }).join('');
    const line = n ? `<button type="button" class="fr-tcount" data-act="fr-tgroup" style="top:${2 + shown.length * 27}px" aria-expanded="${T.group}">${T.group ? `收起终端里的 ${n} 个` : `终端里还有 ${n} 个`}</button>` : '';
    patch(sky, rows + line);
    sky.dataset.h = String(shown.length * 27 + (n ? 30 : 0));
  }
  // The names in the sky stand where its own rows stand, which moves with its width.
  const follow = () => {
    if (sky && win.classList.contains('sky-on')) { const l = (rowsEl?.firstElementChild as HTMLElement | null)?.style.left; if (l && sky.style.left !== l) sky.style.left = l; requestAnimationFrame(follow); }
  };
  new MutationObserver(() => {
    const on = win.classList.contains('sky-on');
    if (on && !skyWas) { void refreshList(); requestAnimationFrame(follow); }
    skyWas = on;
  }).observe(win, { attributes: true, attributeFilter: ['class'] });
  let skyWas = false;
  const side = win.querySelector<HTMLElement>('.arch-link');
  side?.insertAdjacentHTML('beforebegin', `<button type="button" class="fr-tlink" data-act="fr-tlist" hidden>${I.term}终端里的会话<em></em></button>`);
  const sideLink = win.querySelector<HTMLElement>('.fr-tlink');
  function drawSide() { if (!sideLink) return; sideLink.hidden = !T.list.length; patch(sideLink.querySelector('em')!, String(T.list.length)); }
  // /resume, and the classic list's line: the newest of them in the page's one popover.
  function listMenu(at: HTMLElement) {
    if (!T.list.length) { ctx.toast('这台 Mac 上没有别处开的会话'); return; }
    ctx.menu(`<span class="ph">终端里开的会话</span>${T.list.slice(0, 30).map(o => `<button type="button" data-act="fr-topen" data-fr="${esc(o.id)}"><span class="fr-tg">终端</span><span class="fr-mt">${esc(o.title)}</span><span class="fr-k">${stateOf(o)[1]}</span></button>`).join('')}`, at, { cls: 'fr-tm' });
  }
  async function showTerminals() {
    await refreshList();
    const pull = win.querySelector<HTMLElement>('.bw-pull');
    if (win.classList.contains('bw') && ctx.sessions().some(s => !s.archived) && pull) {
      T.group = true; drawSky();
      if (!win.classList.contains('sky-on')) pull.click();
    } else listMenu(ctx.chat() ? ctx.ta : sideLink ?? ctx.ta);
  }
  ctx.own.set('import', () => void showTerminals());

  // ---------- the read-only view ----------
  const bd = win.querySelector<HTMLElement>('.bd')!;
  const rh = document.createElement('header'); rh.className = 'm-head fr-rh';
  const rv = document.createElement('section'); rv.className = 'fr-rov'; rv.setAttribute('aria-label', '在终端里开的会话');
  rv.innerHTML = '<div class="conv"><div class="c-in"><div class="c-items"></div><div class="fr-rq"></div></div></div><div class="fr-rbar"></div>';
  bd.append(rv);
  const conv = rv.querySelector<HTMLElement>('.conv')!, itemsEl = rv.querySelector<HTMLElement>('.c-items')!, rqEl = rv.querySelector<HTMLElement>('.fr-rq')!, bar = rv.querySelector<HTMLElement>('.fr-rbar')!;
  function openRO(o: Outside) {
    if (win.classList.contains('sky-on')) win.querySelector<HTMLElement>('.bw-pull')?.click();
    const same = T.cur?.id === o.id;
    T.cur = o; T.take = false;
    if (!same) { T.items = null; T.why = ''; T.opened.clear(); T.asked = []; T.busy = ''; T.said = ''; }
    // Its header stands where the window's own stands, in either layout.
    win.querySelector('.m-head:not(.fr-rh)')?.after(rh);
    win.classList.add('fr-ro-on');
    ctx.cue('open', .6);
    drawRO(); drawSky();
    anim(rv, [{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'none' }], 300);
    win.tabIndex = -1; win.focus({ preventScroll: true });
    void readItems(); void refreshLive();
  }
  function closeRO() {
    if (!T.cur) return;
    T.cur = null; T.take = false;
    win.classList.remove('fr-ro-on');
    drawSky();
  }
  const words = (o: Outside) => { const l = liveOf(o.id); return l?.st === 'wait' ? (l.req ? '在终端里等你批' : '在终端里等你') : l?.st === 'work' ? '在终端里跑着' : '在终端里开的'; };
  function stepsLine(steps: Step[]) {
    const n = (k: Step['k']) => steps.filter(s => s.k === k).length, add = steps.reduce((a, s) => a + (s.add ?? 0), 0), del = steps.reduce((a, s) => a + (s.del ?? 0), 0);
    return [n('read') + n('search') ? `读了 ${n('read') + n('search')} 个` : '', n('edit') ? `改了 ${n('edit')} 个 <span class="p">+${add}</span> <span class="m">−${del}</span>` : '',
      n('bash') ? `跑了 ${n('bash')} 条` : '', n('agent') ? `${n('agent')} 个子任务` : '', n('web') ? `查了 ${n('web')} 次网页` : '', n('tool') ? `用了 ${n('tool')} 个工具` : ''].filter(Boolean).join(' · ');
  }
  const clock = (at?: number) => at ? new Date(at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }) : '';
  function itemHTML(it: Item, i: number) {
    if (it.k === 'you') return `<div class="you">${it.files?.length ? `<span class="att">${it.files.map(f => `<span class="thumb">${esc(f.name)}</span>`).join('')}</span>` : ''}${esc(it.text)}</div>`;
    if (it.k === 'it') return `<div class="it">${ctx.md(it.text)}${it.at ? `<div class="it-acts"><time>${clock(it.at)}</time></div>` : ''}</div>`;
    if (it.k === 'note') return `<p class="note">${esc(it.text)}</p>`;
    if (it.k === 'plan') return `<div class="plan"><span class="p-h">计划</span>${it.todos.map(([t, d]) => `<span class="todo d${d}"><i></i>${esc(t)}</span>`).join('')}</div>`;
    if (it.k === 'req') return it.done ? `<p class="note done"><span class="ok">${/^(拒绝|没回答)/.test(it.done) ? '✕' : '✓'}</span>${esc(recordOf(it.req))}<span class="how">${esc(it.done)}</span></p>` : '';
    const open = T.opened.has(i), sum = stepsLine(it.steps);
    return `<div class="steps ${open ? 'open' : 'tail'}"${it.steps.length ? '' : ' hidden'}><button type="button" class="s-sum" data-act="fr-rsteps" data-i="${i}" aria-expanded="${open}"><span class="chev">${I.chev}</span><span class="s-t">${it.took ? `干了 ${esc(it.took)}` : '干完了'}${sum ? ` · ${sum}` : ''}</span></button>`
      + `<div class="s-wrap"><div class="s-clip"><div class="s-in">${open ? it.steps.map(st => st.k === 'say' ? `<div class="step say">${esc(st.t)}</div>`
        : `<div class="step"><span class="k">${K[st.k]}</span><span class="a" title="${esc(st.t)}">${esc(st.t)}</span><span class="r">${st.add !== undefined ? `<span class="p">+${st.add}</span> <span class="m">−${st.del ?? 0}</span>` : st.ok === true ? '<span class="p">✓</span>' : st.ok === false ? '<span class="m">✕</span>' : ''}</span></div>`).join('') : ''}</div></div></div></div>`;
  }
  function recordOf(r: Req) {
    return r.tool === 'Ask' ? r.qs.map(q => q.q).join(' · ') : r.tool === 'Plan' ? '计划' : r.tool === 'Bash' ? r.cmd : r.tool === 'Edit' ? `改 ${r.file}` : r.tool === 'Form' ? r.server : r.name;
  }
  // The request it stopped on in the terminal, the window's own card; answering it here lets the terminal go on.
  function reqHTML(r: Req) {
    const on = (k: string) => T.busy === r.id && T.said === k ? ' is-busy' : '', off = T.busy === r.id ? ' disabled' : '', busy = T.busy === r.id ? ' busy' : '';
    const who = T.cur?.agent === 'codex' ? 'Codex' : 'Claude Code';
    if (r.tool === 'Ask') {
      const simple = r.qs.length === 1 && !r.qs[0].multi;
      return `<div class="req ask${busy}"><span class="r-h">${who} 问你</span>${r.qs.map((q, qi) => `<div class="q-block"><p class="q">${esc(q.q)}</p><div class="opts">${q.opts.map(([l, d], k) =>
        `<button type="button" class="opt${T.asked[qi]?.includes(l) ? ' on' : ''}${on(`opt:${l}`)}" data-act="${simple ? 'fr-ropt' : 'fr-rpick'}" data-q="${qi}" data-v="${esc(l)}"${off}><i>${k + 1}</i><span><b>${esc(l)}</b>${d ? `<small>${esc(d)}</small>` : ''}</span></button>`).join('')}</div></div>`).join('')}`
        + (simple ? '' : `<div class="choice"><button type="button" class="btn warm${on('all')}" data-act="fr-rall"${off || !r.qs.every((_, qi) => T.asked[qi]?.length) ? ' disabled' : ''}>好了</button></div>`)
        + `<p class="hint">${simple ? '按数字键选，' : ''}${kbd('esc')} 不回答</p></div>`;
    }
    const head = r.tool === 'Bash' ? '要你批准 · 跑一条命令' : r.tool === 'Edit' ? '要你批准 · 改一个文件' : r.tool === 'Plan' ? '计划写好了' : `要你批准 · ${esc(r.tool === 'Tool' ? r.name : r.tool === 'Form' ? r.server : '')}`;
    const what = r.tool === 'Plan' ? `<div class="plan-text">${ctx.md(r.plan)}</div>` : r.tool === 'Bash' ? `<pre class="cmd"><span>${esc(home(r.cwd))} $</span> ${esc(r.cmd)}</pre>`
      : r.tool === 'Edit' ? `<div class="file">${esc(r.file)}</div>${r.diff.length ? ctx.diff(r.diff) : ''}` : r.tool === 'Tool' ? `<pre class="cmd">${esc(r.detail)}</pre>` : '';
    const why = r.tool !== 'Plan' && r.tool !== 'Form' && r.why ? `<p class="why">${esc(r.why)}</p>` : '';
    const always = r.tool !== 'Plan' && r.tool !== 'Form' && r.always ? `<button type="button" class="btn${on('always')}" data-act="fr-ralways"${off}>${esc(r.always)}</button>` : '';
    return `<div class="req${busy}"><span class="r-h">${head}</span>${why}${what}<div class="choice"><button type="button" class="btn${on('deny')}" data-act="fr-rdeny"${off}>${r.tool === 'Plan' ? '再想想' : '拒绝'}${kbd('esc')}</button>${always}`
      + `<button type="button" class="btn warm${on('allow')}" data-act="fr-rallow"${off}>${r.tool === 'Plan' ? '就这么做' : '允许'}${kbd('↵')}</button></div></div>`;
  }
  // What 接手 changes, said once before it happens.
  function takeHTML(o: Outside) {
    const a = T.auth, claude = o.agent === 'claude', need = claude && a && !a.ready, l = liveOf(o.id);
    const how = !claude ? '之后用同一个 ChatGPT 登录。' : !a ? '' : !a.packaged ? '之后用这台 Mac 上 Claude Code 自己的登录。'
      : a.provider === 'bedrock' ? '之后用你的 Amazon Bedrock 账户。' : a.provider === 'vertex' ? '之后用你的 Google Vertex 账户。' : `之后用你的 API key（${a.hint ?? ''}），不再走订阅。`;
    const now = l?.st === 'work' ? '它这会儿正在终端里跑：先在那边停下，再接过来。' : l?.st === 'wait' ? '它在终端里等你批：先在这里批了，或者在那边关掉。'
      : o.recent ? '它两分钟内还在终端里动过，可能还开着：先在那边关掉。' : '';
    const lines = [['', '接过来以后在这里接着聊，终端里那个就别再用了。'], ['', need ? '' : how], ['warm', need ? `还开不了：${a!.why ?? '先填一个 Anthropic API key'}。` : ''], ['warm', now]]
      .filter(x => x[1]).map(([c, t]) => `<li${c ? ` class="${c}"` : ''}>${esc(t)}</li>`).join('');
    const go = need ? `<button type="button" class="btn sm warm" data-act="fr-take-key">先填 key ${kbd('⏎')}</button>` : `<button type="button" class="btn sm warm" data-act="fr-take-yes">接手 ${kbd('⏎')}</button>`;
    return `<div class="fr-tkc" role="group" aria-label="接手"><p class="fr-tkc-h">接手「${esc(o.title)}」？</p><ul>${lines}</ul><div class="fr-tkc-r"><button type="button" class="btn sm" data-act="fr-take-no">算了 ${kbd('esc')}</button>${go}</div></div>`;
  }
  function drawRO(fresh = false) {
    const o = T.cur; if (!o) return;
    const l = liveOf(o.id), st = l?.st === 'wait' ? 'wait' : l?.st === 'work' ? 'work' : 'done';
    patch(rh, `<span class="h-mk fr-rmk">${I.term}</span><div class="h-main"><div class="h-t"><b>${esc(o.title)}</b></div><div class="h-meta"><span class="who ${o.agent}">${o.agent === 'claude' ? 'Claude' : 'Codex'}</span><span class="dot">·</span>`
      + `<span title="${esc(o.cwd)}">${esc(o.cwd.split('/').pop() || o.cwd)}</span>${o.branch ? `<span class="dot">·</span><span class="br">⎇ ${esc(o.branch)}</span>` : ''}<span class="dot">·</span><span class="st-${st}">${st === 'wait' ? '等你' : st === 'work' ? '在干活' : age(o.updated)}</span>`
      + '<span class="dot">·</span><i class="fr-ttag">在终端里 · 只能看</i></div></div>'
      + `<button type="button" class="h-btn" data-act="fr-take">${I.take}<span>接手</span></button><button type="button" class="h-btn icon" data-act="fr-rmore" aria-label="更多" data-tip="接手、复制 resume 命令">${I.more}</button>`);
    const stick = fresh || conv.scrollTop >= conv.scrollHeight - conv.clientHeight - 40;
    patch(itemsEl, T.items === null ? '<p class="loading">在读这个会话…</p>'
      : (T.items.map((it, i) => { const h = itemHTML(it, i); return h ? `<div class="item">${h}</div>` : ''; }).join('') || '<p class="loading">它的记录里还没有话。</p>')
      + (T.why ? `<p class="note">读不出来：${esc(T.why)}</p>` : ''));
    const r = reqOf(o.id);
    patch(rqEl, r ? `<div class="item">${reqHTML(r)}</div><p class="fr-also">终端里也在等，哪边先批都算</p>` : '');
    patch(bar, T.take ? takeHTML(o)
      : `<div class="fr-rob"><span class="fr-rob-i">${I.term}</span><span class="fr-rob-t"><b>${words(o)}</b><span>这里只能看</span></span><button type="button" class="btn sm" data-act="fr-take">${I.take}接手</button></div>`);
    if (stick) conv.scrollTop = conv.scrollHeight;
  }
  // A session the window opens is where you went: the read-only view steps aside.
  new MutationObserver(() => closeRO()).observe(win.querySelector('.host')!, { childList: true });
  win.addEventListener('click', e => {
    const t = e.target as Element;
    if (T.cur && t.closest('[data-act="open"],[data-act="new"],[data-act="archview"],[data-act="next"],.her-c,.ex-mode,.bw-rows [data-session],.bw-stars [data-session]')) closeRO();
  }, true);
  // In the sky, a click or a key that went into a session closed it on the way.
  const skyEl = win.querySelector<HTMLElement>('.bw-sky');
  skyEl?.addEventListener('click', e => { if (T.cur && !(e.target as Element).closest('.bw-more')) setTimeout(() => { if (!win.classList.contains('sky-on')) closeRO(); }); }, true);

  // ---------- answering, and 接手 ----------
  async function answer(decision: 'allow' | 'always' | 'deny', key: string, answers?: string[][]) {
    const o = T.cur, r = o && reqOf(o.id);
    if (!o || !r || T.busy === r.id) return;
    T.busy = r.id; T.said = key; drawRO();
    ctx.cue(decision === 'deny' ? 'close' : 'send');
    // A question's answers go back as {question: label}, the way Claude Code takes them.
    const by = r.tool === 'Ask' && answers ? Object.fromEntries(r.qs.map((q, qi) => [q.q, (answers[qi] ?? []).join(', ')]).filter(x => x[1])) : undefined;
    const ok = await ctx.tryCall('/import/answer', { req: r.id, decision, ...by ? { answers: by } : {} });
    if (T.cur !== o) return;
    if (!ok) { T.busy = ''; drawRO(); void refreshLive(); return; }
    // Gone from here at once; the terminal goes on, and its transcript shows what came of it.
    T.live.set(o.id, { id: o.id, st: decision === 'deny' ? 'done' : 'work' }); T.asked = []; T.busy = '';
    ctx.toast(decision === 'deny' ? '拒绝了 · 终端那边接着往下走' : '批了 · 终端那边接着跑');
    drawRO(); drawSky();
    setTimeout(() => { if (T.cur === o) void readItems(); }, 1200);
  }
  async function askTake() {
    const o = T.cur; if (!o) return;
    T.take = true; drawRO();
    const c = bar.querySelector('.fr-tkc'); if (c) anim(c, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], 200);
    ctx.tick();
    T.auth = (await ctx.call<{ auth: Auth }>('/settings').catch(() => null))?.auth ?? T.auth;
    if (T.cur === o && T.take) drawRO();
  }
  async function takeOver() {
    const o = T.cur; if (!o || !T.take) return;
    if (o.agent === 'claude' && T.auth && !T.auth.ready) { T.take = false; drawRO(); showSettings('key', 'claude'); return; }
    // The confirmation said when it may still be open there: this press is the second, sure one (ADR 0096).
    const force = !!o.recent || !!liveOf(o.id);
    const r = await ctx.call<{ id: string }>('/import', { agent: o.agent, id: o.id, cwd: o.cwd, force }).catch((e: unknown) => e as Error & { need?: string });
    if (T.cur !== o) return;
    if (r instanceof Error) {
      if (r.need === 'force') { o.recent = true; drawRO(); ctx.cue('ask', .6); return; }
      ctx.toast(r.message, true); return;
    }
    ctx.cue('open');
    closeRO();
    T.list = T.list.filter(x => x.id !== o.id); drawSky(); drawSide();
    await ctx.load(r.id).catch(() => {});
    ctx.open(r.id);
  }

  // ---------- delete ----------
  const moreBtn = () => win.querySelector<HTMLElement>('.m-head:not(.fr-rh) [data-act="menu"][data-v="more"]');
  function askHTML(s: Sess, how: 'ahead' | 'dirty') {
    return `<p class="fr-ask-h">删掉「${esc(s.title)}」？</p><p class="fr-ask-b">${how === 'ahead' ? `<code>${esc(s.branch)}</code> 上还有没合进去的提交。` : 'worktree 里还有没提交的改动。'}删之前会先备份。</p>`
      + `<div class="fr-ask-r"><button type="button" class="btn sm fr-bad" data-act="fr-del-yes" data-id="${esc(s.id)}">删掉</button><button type="button" class="btn sm" data-act="fr-del-no" data-id="${esc(s.id)}">先留着</button></div>`;
  }
  function ask(s: Sess, how: 'ahead' | 'dirty', at: HTMLElement | { x: number; y: number }) {
    ctx.menu(askHTML(s, how), at, { right: at instanceof HTMLElement, cls: 'fr-ask' });
    ctx.cue('ask', .5);
    win.querySelector<HTMLElement>('.pop [data-act="fr-del-no"]')?.focus({ preventScroll: true });
  }
  // Where the last right click was, for the question to stand there.
  let point: { x: number; y: number } | null = null;
  function del(s: Sess) {
    const at = point ?? moreBtn() ?? { x: 24, y: 24 };
    point = null;
    // The session's own record of its worktree says whether git would lose something; the host checks again.
    if (s.tree && !s.gone && (s.dirty?.ahead || s.dirty?.n)) { ask(s, s.dirty.ahead ? 'ahead' : 'dirty', at); return; }
    hold(s);
  }
  const holds = new Map<string, { t: number; pinned: boolean; parked: boolean; was: boolean }>();
  function away(s: Sess) {
    if (ctx.current()?.id !== s.id) return;
    const next = ctx.sessions().filter(x => x.id !== s.id && !x.archived).sort((a, b) => b.updated - a.updated)[0];
    if (next) ctx.open(next.id); else win.querySelector<HTMLElement>('[data-act="new"]')?.click();
  }
  // A clean one leaves the window at once (archived meanwhile) and is deleted when the toast's 撤销 has had its time.
  function hold(s: Sess) {
    const was = ctx.current()?.id === s.id;
    holds.set(s.id, { t: window.setTimeout(() => void finish(s.id), HOLD), pinned: s.pinned, parked: s.parked, was });
    away(s);
    s.archived = true; s.pinned = false; ctx.draw();
    void ctx.call(`/sessions/${s.id}/meta`, { archived: true }).catch(() => {});
    ctx.cue('close', .8);
    ctx.toast(`删了「${s.title}」`);
    const t = win.querySelector<HTMLElement>('.toast');
    t?.insertAdjacentHTML('beforeend', ` <button type="button" class="fr-undo" data-act="fr-undel" data-id="${esc(s.id)}">撤销</button>`);
  }
  function unhold(id: string) {
    const h = holds.get(id), s = ctx.byId(id);
    if (!h || !s) return;
    clearTimeout(h.t); holds.delete(id);
    s.archived = false; s.pinned = h.pinned; s.parked = h.parked; ctx.draw();
    void ctx.tryCall(`/sessions/${id}/meta`, { archived: false, pinned: h.pinned, parked: h.parked });
    ctx.toast(`撤销了，「${s.title}」还在`);
    if (h.was) ctx.open(id);
    ctx.cue('open', .7);
  }
  async function finish(id: string) {
    holds.delete(id);
    const s = ctx.byId(id); if (!s) return;
    win.querySelector(`.toast .fr-undo[data-id="${CSS.escape(id)}"]`)?.remove();
    const r = await ctx.call(`/sessions/${id}`, undefined, 'DELETE').catch((e: unknown) => e as Error & { need?: string });
    if (!(r instanceof Error)) return;
    // Git would lose something after all: the question, and the row is back until it is answered.
    const back = () => { s.archived = false; ctx.draw(); void ctx.tryCall(`/sessions/${id}/meta`, { archived: false }); };
    if (r.need === 'force') { back(); ask(s, /没提交/.test(r.message) ? 'dirty' : 'ahead', { x: Math.max(8, win.clientWidth / 2 - 150), y: 70 }); return; }
    back(); ctx.toast(r.message, true);
  }
  async function force(id: string) {
    const s = ctx.byId(id); if (!s) return;
    ctx.closeMenu();
    const r = await ctx.tryCall(`/sessions/${id}?force=1`, undefined, 'DELETE');
    if (!r) return;
    ctx.cue('close');
    ctx.toast(typeof r.kept === 'string' && r.kept ? `删了 · ${s.dirty?.ahead || !s.dirty ? '提交' : '改动'}备份在 ${r.kept}` : `删了「${s.title}」`);
  }
  // A row's right click, in the sky and in the classic list: the row's own lines, and 删除.
  win.addEventListener('contextmenu', e => {
    const t = e.target as Element, row = t.closest<HTMLElement>('.bw-rows [data-session], .s-list .row[data-id], .bw-more [data-fr]');
    if (!row) return;
    e.preventDefault(); e.stopPropagation();
    const r = win.getBoundingClientRect(), at = { x: e.clientX - r.left, y: e.clientY - r.top };
    if (row.dataset.fr) {
      const o = T.list.find(x => x.id === row.dataset.fr); if (!o) return;
      ctx.menu(`<button type="button" data-act="fr-topen" data-fr="${esc(o.id)}" data-take="1">接手</button><button type="button" data-act="fr-resume" data-fr="${esc(o.id)}">复制 resume 命令</button>`, at);
      return;
    }
    const s = ctx.byId(row.dataset.session ?? row.dataset.id ?? ''); if (!s) return;
    point = at;
    ctx.menu(`<button type="button" data-act="pin" data-id="${esc(s.id)}">${s.pinned ? '取消置顶' : '置顶'}</button><button type="button" data-act="park" data-id="${esc(s.id)}">${s.parked ? '不放着了' : '先放着'}</button>`
      + `<button type="button" data-act="archive" data-id="${esc(s.id)}">归档</button><span class="sep"></span><button type="button" class="bad" data-act="fr-del" data-id="${esc(s.id)}">删除</button>`, at);
  }, true);

  // ---------- polling: the list now and then, what runs in a terminal every few seconds ----------
  setTimeout(() => void refreshList(), 1200);
  setInterval(() => { if (!document.hidden) void refreshList(); }, 60000);
  setInterval(() => { if (!document.hidden && (T.list.length || T.cur)) void refreshLive(); }, 2500);

  const nudge = () => { if (performance.now() - T.nudged < 3000) return; T.nudged = performance.now(); ctx.toast('它在终端里：接手以后才能在这里说'); };
  return {
    more: s => `<button type="button" class="bad" data-act="fr-del" data-id="${esc(s.id)}">删除</button>`,
    key(e) {
      const o = T.cur, k = e.key, t = e.target as HTMLElement;
      if (!o || e.isComposing || win.classList.contains('fr-modal') || win.querySelector('.pop.on')) return false;
      if (win.classList.contains('sky-on')) {
        // Going into a session from the sky leaves this view.
        if (k === 'Enter' || k === 'ArrowRight') setTimeout(() => { if (!win.classList.contains('sky-on')) closeRO(); });
        return false;
      }
      const onBtn = !!t.closest?.('button,input,textarea,select'), plain = !e.metaKey && !e.ctrlKey && !e.altKey;
      const take = (fn: () => void) => { e.preventDefault(); e.stopImmediatePropagation(); if (!e.repeat) fn(); return true; };
      const r = reqOf(o.id), off = T.busy === r?.id;
      if (T.take) {
        if (k === 'Escape') return take(() => { T.take = false; drawRO(); ctx.cue('close', .5); });
        if (k === 'Enter' && plain && !onBtn) return take(() => void (T.auth && o.agent === 'claude' && !T.auth.ready ? (T.take = false, drawRO(), showSettings('key', 'claude')) : takeOver()));
      }
      if (r && !off && plain) {
        const opt = r.tool === 'Ask' && r.qs.length === 1 && !r.qs[0].multi && /^[1-9]$/.test(k) ? r.qs[0].opts[Number(k) - 1] : undefined;
        if (opt) return take(() => void answer('allow', `opt:${opt[0]}`, [[opt[0]]]));
        if (k === 'Enter' && !onBtn && r.tool !== 'Ask') return take(() => void answer('allow', 'allow'));
        if (k === 'Escape') return take(() => void answer('deny', 'deny'));
      }
      // The window's own session is behind this view: its keys (esc, ⏎, digits) must not reach it.
      if (k === 'Escape') return take(() => { if (liveOf(o.id)?.st === 'work') ctx.toast('它在终端里跑：要停去那边停，或者先接手'); });
      if (k === 'Enter' && !onBtn) return take(() => {});
      if (k === 'Enter' || (plain && /^[1-9]$/.test(k))) { e.stopImmediatePropagation(); return true; }
      if (plain && k.length === 1 && k !== ' ' && k !== '?' && !onBtn) return take(nudge);
      return false;
    },
    act(a, el) {
      if (a === 'fr-del') {
        const s = ctx.byId(el.dataset.id ?? ''), menu = el.closest('.pop')?.getBoundingClientRect(), w = win.getBoundingClientRect();
        // The question stands where the menu stood, its right edge on the menu's (under the ··· it came from); a right
        // click already said where. 300 is .pop.fr-ask's width.
        if (menu && !point) point = { x: menu.right - w.left - 300, y: menu.top - w.top };
        if (s) { ctx.closeMenu(); del(s); }
        return true;
      }
      if (a === 'fr-del-yes') { void force(el.dataset.id!); return true; }
      if (a === 'fr-del-no') { ctx.closeMenu(); ctx.tick(); return true; }
      if (a === 'fr-undel') { unhold(el.dataset.id!); return true; }
      if (a === 'fr-tgroup') { T.group = !T.group; ctx.tick(); drawSky(); return true; }
      if (a === 'fr-tlist') { void refreshList().then(() => listMenu(el)); return true; }
      if (a === 'fr-topen') {
        const o = T.list.find(x => x.id === el.dataset.fr); ctx.closeMenu();
        if (o) { openRO(o); if (el.dataset.take) void askTake(); }
        return true;
      }
      if (a === 'fr-resume') {
        const o = T.list.find(x => x.id === el.dataset.fr) ?? T.cur; ctx.closeMenu();
        if (o) { const c = resumeOf(o); void navigator.clipboard.writeText(c).then(() => ctx.toast(`复制了：${c}`), () => ctx.toast(`在终端里跑：${c}`)); ctx.tick(); }
        return true;
      }
      if (!T.cur) return false;
      if (a === 'fr-take') { void askTake(); return true; }
      if (a === 'fr-take-no') { T.take = false; drawRO(); ctx.tick(); return true; }
      if (a === 'fr-take-yes') { void takeOver(); return true; }
      if (a === 'fr-take-key') { T.take = false; drawRO(); showSettings('key', 'claude'); return true; }
      if (a === 'fr-rmore') { ctx.menu(`<button type="button" data-act="fr-take">接手</button><button type="button" data-act="fr-resume" data-fr="${esc(T.cur.id)}">复制 resume 命令</button>`, el, { right: true }); return true; }
      if (a === 'fr-rsteps') { const i = Number(el.dataset.i); if (T.opened.has(i)) T.opened.delete(i); else T.opened.add(i); drawRO(); return true; }
      if (a === 'fr-rallow' || a === 'fr-ralways' || a === 'fr-rdeny') { void answer(a === 'fr-rallow' ? 'allow' : a === 'fr-ralways' ? 'always' : 'deny', a.slice(4)); return true; }
      if (a === 'fr-ropt') { void answer('allow', `opt:${el.dataset.v}`, [[el.dataset.v!]]); return true; }
      if (a === 'fr-rpick') {
        const r = reqOf(T.cur.id), qi = Number(el.dataset.q), v = el.dataset.v!, multi = r?.tool === 'Ask' && r.qs[qi]?.multi, now = T.asked[qi] ?? [];
        T.asked[qi] = multi ? (now.includes(v) ? now.filter(x => x !== v) : [...now, v]) : [v];
        ctx.tick(); drawRO(); return true;
      }
      if (a === 'fr-rall') { void answer('allow', 'all', T.asked); return true; }
      return false;
    },
  };
}

// 回头: going back to before something you said (/rewind), with its files or without; a new session from before it
// (/fork, 分叉…); a question on the side, answered under the paragraph it is about and never in the conversation (/btw,
// /side, 问一句 on selected words); and the whole conversation as Markdown, looked at before it is kept (/export).
// Going back in the conversation is a fork before that point with the old session archived and its name kept (host.ts
// fork, `back`); the files go back from Claude's checkpoints. Codex keeps none, so it can only take the conversation back.
import type { File as Upload, Item, Sess } from '../../electron/agents/types';
import type { Feature, PageCtx } from './ctx';
import './back.css';

type You = Item & { k: 'you' };
type Answer = Item & { k: 'it' };
// A row of the list: one thing you said (item i), or with none the whole conversation as it is now (/fork only).
type Row = { it?: You; i: number };
type Files = { list: { p: string; add?: number; del?: number }[]; wrote: string[]; add: number; del: number };
type Opt = { v: 0 | 1 | 2 | 3; l: string; sub?: string; off?: boolean; files?: Files };
// A fork or a going-back that waits for what you send: from before `at`, or (without it) the whole conversation.
type Later = { kind: 'fork' | 'back'; at?: string; code?: boolean };
type QA = { q: string; a?: string; err?: boolean };
// A side question: under paragraph `para` of one answer (`key`), open there or folded to a mark; `pin` keeps its list
// scrolled to the latest, `top` is where you left it otherwise.
type Side = { id: string; sid: string; key: string; para: number; words: string; qa: QA[]; st: 'open' | 'min'; draft: string; asking: boolean; top: number; pin: boolean; fresh: boolean };

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const OUT = 'cubic-bezier(.23,1,.32,1)';
const SPRING = 'linear(0,.054,.178,.329,.481,.617,.731,.82,.888,.936,.969,.99,1.003,1.01,1.014,1.015,1.014,1.012,1.01,1.008,1)';
const anim = (el: Element | null | undefined, kf: Keyframe[], ms: number, easing = OUT) => el && !reduced.matches ? el.animate(kf, { duration: ms, easing }) : null;
const H = new WeakMap<Element, string>();
const patch = (el: Element, html: string) => { if (H.get(el) === html) return false; el.innerHTML = html; H.set(el, html); return true; };
const kbd = (k: string) => `<kbd>${k}</kbd>`;
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));
const frame = () => new Promise(r => requestAnimationFrame(r));
// What you said, short enough to quote: one line, no end punctuation.
const said = (t: string, n: number) => { const x = t.replace(/\s+/g, ' ').replace(/[。！？!?.，,；;：:\s]+$/, ''); return x.length > n ? `${x.slice(0, n - 1)}…` : x; };
const clock = (at?: number) => at ? new Date(at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }) : '';
const nums = (a: number, d: number) => `<span class="p">+${a}</span> <span class="m">−${d}</span>`;
const busy = (s: Sess) => s.st === 'work' || s.st === 'pack' || s.st === 'wait';
const svg = (d: string, w = 13) => `<svg viewBox="0 0 16 16" width="${w}" height="${w}" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const I = {
  btw: svg('<path d="M3.4 3h9.2c.8 0 1.4.6 1.4 1.4v5.4c0 .8-.6 1.4-1.4 1.4H8.2l-3.1 2.4v-2.4H3.4c-.8 0-1.4-.6-1.4-1.4V4.4C2 3.6 2.6 3 3.4 3z"/><path d="M5.2 7.1h.1M8 7.1h.1M10.8 7.1h.1" stroke-width="1.7"/>'),
  quote: svg('<path d="M6.5 3.5 2.5 7.5l4 4"/><path d="M2.8 7.5H9c2.5 0 4.5 2 4.5 4.5v.5"/>'),
  copy: svg('<rect x="5.5" y="5.5" width="8" height="8" rx="2"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/>', 12),
  x: svg('<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>', 15),
};

export function mountBack(ctx: PageCtx): Feature {
  const { win, ta } = ctx;
  const comp = win.querySelector<HTMLElement>('.composer')!, box = comp.querySelector<HTMLElement>('.c-box')!, host = win.querySelector<HTMLElement>('.host')!;
  const pk = document.createElement('div'), bar = document.createElement('div'), ex = document.createElement('div');
  pk.className = 'bk-pk'; pk.hidden = true; pk.setAttribute('role', 'dialog');
  bar.className = 'bk-bar'; bar.hidden = true; bar.setAttribute('role', 'toolbar'); bar.setAttribute('aria-label', '选中的字');
  ex.className = 'bk-ex'; ex.hidden = true;
  comp.append(pk); win.append(bar, ex);
  // The composer keeps the keys while a row is clicked, and selected words stay selected while the bar is.
  pk.addEventListener('pointerdown', e => e.preventDefault());
  bar.addEventListener('mousedown', e => e.preventDefault());
  const later = new Map<string, Later>();

  // ---------- /rewind and /fork: one of your messages, picked with the keys, above the composer like the / menu ----------
  const PK = { open: false, kind: 'rewind' as 'rewind' | 'fork', stage: 'list' as 'list' | 'opts' | 'ask', sid: '', rows: [] as Row[], sel: 0, opt: 0, moved: false, going: false,
    dry: null as null | { at: string; can: boolean; why?: string; files: string[]; add: number; del: number } };
  const firstYou = (id: string) => (ctx.items(id) ?? []).findIndex(x => x.k === 'you');
  const lastYou = (id: string) => (ctx.items(id) ?? []).map(x => x.k).lastIndexOf('you');
  // What putting the files back to before item i would undo, as the conversation shows it: its own edits file by file;
  // what a shell command wrote is not in the checkpoints, so it is named apart.
  function edits(s: Sess, i: number): Files {
    const m = new Map<string, { p: string; add: number; del: number }>(), wrote = new Set<string>();
    for (const x of (ctx.items(s.id) ?? []).slice(i + 1)) if (x.k === 'steps') for (const st of x.steps) {
      if (st.k === 'bash') { const f = /(?:>>?|\btee\s+)\s*([^\s|;&>]+)/.exec(st.t)?.[1]; if (f) wrote.add(f); continue; }
      if (st.k !== 'edit') continue;
      const o = m.get(st.t) ?? { p: st.t, add: 0, del: 0 }; o.add += st.add ?? 0; o.del += st.del ?? 0; m.set(st.t, o);
    }
    const list = [...m.values()];
    return { list, wrote: [...wrote], add: list.reduce((a, f) => a + f.add, 0), del: list.reduce((a, f) => a + f.del, 0) };
  }
  // Once the host has looked (a dry run of Claude's checkpoints), its files are the ones that go back.
  function filesOf(s: Sess, r: Row): Files {
    const F = edits(s, r.i), d = PK.dry;
    if (!d || d.at !== r.it?.id) return F;
    const rel = (f: string) => f.startsWith(`${s.cwd}/`) ? f.slice(s.cwd.length + 1) : f;
    // Its lines are the conversation's own count of each file's edits, the sense the rows use (the host counts the undoing).
    const list = d.files.map(f => { const p = rel(f); return F.list.find(x => x.p === p) ?? { p }; });
    return { list, wrote: F.wrote, add: list.reduce((a, f) => a + (f.add ?? 0), 0), del: list.reduce((a, f) => a + (f.del ?? 0), 0) };
  }
  function optsOf(s: Sess, r: Row): Opt[] {
    if (s.agent === 'codex') return [{ v: 1, l: '对话和文件都退', off: true }, { v: 2, l: '只退对话' }, { v: 3, l: '只退文件', off: true }, { v: 0, l: '算了' }];
    const F = filesOf(s, r), n = F.list.length, cannot = !!PK.dry && PK.dry.at === r.it?.id && !PK.dry.can;
    const fs = cannot ? '文件回不去' : n ? `${n} 个文件${F.add || F.del ? ` ${nums(F.add, F.del)}` : ''}` : PK.dry?.at === r.it?.id ? '文件没有要退的' : '这之后没改文件';
    return [{ v: 1, l: '对话和文件都退', sub: fs, off: !n || cannot, files: F }, { v: 2, l: '只退对话', sub: n ? '文件留着' : '' },
      { v: 3, l: '只退文件', sub: cannot ? '文件回不去' : n ? '对话留着' : '', off: !n || cannot, files: F }, { v: 0, l: '算了' }];
  }
  const firstOn = (opts: Opt[]) => Math.max(0, opts.findIndex(o => !o.off));
  const liveRow = (s: Sess, r: Row) => busy(s) && r.i === lastYou(s.id);
  function listHTML(s: Sess) {
    const fork = PK.kind === 'fork', cx = s.agent === 'codex';
    const rows = PK.rows.map((r, j) => {
      const on = j === PK.sel, a = `type="button" class="bk-r${r.it ? '' : ' bk-now'}${on ? ' bk-sel' : ''}" data-act="bk-row" data-j="${j}" role="option" aria-selected="${on}"`;
      if (!r.it) return `<button ${a}><span class="bk-r-t">整段对话</span><span class="bk-r-m">现在</span></button>`;
      let meta = '';
      if (liveRow(s, r)) meta = '<span class="bk-r-live">在干活</span>';
      else if (!fork && !cx) { const F = edits(s, r.i); meta = `<span class="bk-r-f">${F.list.length ? `${F.list.length} 个文件 ${nums(F.add, F.del)}` : '没改文件'}</span>`; }
      return `<button ${a}><span class="bk-r-t">${esc(r.it.text.replace(/\s+/g, ' ') || '（图片）')}</span>${meta}<span class="bk-r-m">${clock(r.it.at)}</span></button>`;
    }).join('');
    return `<p class="bk-pk-h">${fork ? '分叉 · 从哪一句之前分出去' : '退回 · 退到哪一句之前'}</p><div class="bk-pk-l" role="listbox" aria-label="你说过的话">${rows}</div>`
      + `<p class="bk-pk-f">${kbd('↑')}${kbd('↓')} 选 · ${kbd('⏎')} ${fork ? '分叉' : '下一步'} · ${kbd('esc')} 收起</p>`;
  }
  function flHTML(F: Files) {
    const show = F.list.slice(0, 4), more = F.list.length - show.length;
    return `<div class="bk-fl">${show.map(f => `<div><code>${esc(f.p)}</code><span>${f.add !== undefined ? nums(f.add, f.del ?? 0) : ''}</span></div>`).join('')}${more > 0 ? `<div><span>另外 ${more} 个</span></div>` : ''}`
      + `${F.wrote.map(f => `<p class="bk-warn">命令写的 <code>${esc(f)}</code> 不在这里，要自己看</p>`).join('')}</div>`;
  }
  function optsHTML(s: Sess) {
    const r = PK.rows[PK.sel], opts = optsOf(s, r);
    const rows = opts.map((o, j) => {
      const on = j === PK.opt, F = on && !o.off && o.files?.list.length ? o.files : null;
      return `<button type="button" class="bk-r bk-o${on ? ' bk-sel' : ''}${o.off ? ' bk-off' : ''}" data-act="bk-opt" data-j="${j}" role="option" aria-selected="${on}"${o.off ? ' aria-disabled="true"' : ''}>`
        + `${kbd(String(j + 1))}<span class="bk-r-t">${o.l}</span>${o.sub ? `<span class="bk-r-f">${o.sub}</span>` : ''}</button>${F ? flHTML(F) : ''}`;
    }).join('');
    const d = PK.dry, why = s.agent === 'codex' ? 'Codex 不记文件的检查点，只能退对话' : d && d.at === r.it?.id && !d.can ? `文件回不去：${d.why ?? '没有那时的检查点'}` : '';
    return `<p class="bk-pk-h">退回到你说「${esc(said(r.it!.text, 18))}」之前</p>${why ? `<p class="bk-pk-w">${esc(why)}</p>` : ''}<div class="bk-pk-l" role="listbox" aria-label="退什么">${rows}</div>`
      + `<p class="bk-pk-f">${PK.going ? '在退回…' : `${kbd('↑')}${kbd('↓')} 选 · ${kbd('⏎')} 确定 · ${kbd('esc')} 换一句`}</p>`;
  }
  function askHTML(s: Sess) {
    const doing = s.st === 'wait' ? '在等你回答' : s.st === 'pack' ? '在压缩上下文' : s.now ?? '在想', m = /^(在\S+) (.+)$/.exec(doing);
    const opts = [['先打断它', '再选退什么'], ['算了', '']];
    return `<p class="bk-pk-h bk-warm">它还在干活，先打断它？</p><p class="bk-pk-p">退回之前要先停下这一轮：它${m ? `${esc(m[1])} <code>${esc(m[2])}</code>` : esc(doing)}，会停在半路。</p>`
      + `<div class="bk-pk-l" role="listbox" aria-label="先打断它吗">${opts.map(([l, sub], j) => `<button type="button" class="bk-r bk-o${j === PK.opt ? ' bk-sel' : ''}" data-act="bk-opt" data-j="${j}" role="option" aria-selected="${j === PK.opt}">${kbd(String(j + 1))}<span class="bk-r-t">${l}</span>${sub ? `<span class="bk-r-f">${sub}</span>` : ''}</button>`).join('')}</div>`
      + `<p class="bk-pk-f">${kbd('⏎')} 确定 · ${kbd('esc')} 换一句</p>`;
  }
  // Edge to edge with the composer box, just above it. The conversation makes room under its end for it, so the last
  // turn is read above the list rather than under it.
  let padded: HTMLElement | null = null;
  function place() {
    const c = comp.getBoundingClientRect(), b = box.getBoundingClientRect();
    Object.assign(pk.style, { left: `${Math.round(b.left - c.left)}px`, bottom: `${Math.round(c.bottom - b.top + 8)}px`, width: `${Math.round(b.width)}px` });
    const cv = host.querySelector<HTMLElement>('.conv');
    if (padded && padded !== cv) unpad();
    if (!cv) return;
    const end = cv.scrollTop >= cv.scrollHeight - cv.clientHeight - 40, over = Math.max(0, Math.round(cv.getBoundingClientRect().bottom - pk.getBoundingClientRect().top));
    cv.style.paddingBottom = over ? `${over + 10}px` : ''; padded = cv;
    if (end) cv.scrollTop = cv.scrollHeight;
  }
  function unpad() { if (padded) padded.style.paddingBottom = ''; padded = null; }
  function drawPick(first = false) {
    const s = ctx.byId(PK.sid);
    if (!PK.open || !s || ctx.current()?.id !== s.id) { closePick(); return; }
    // Until you move, the first choice that can be made is picked, as what the files would do becomes known.
    if (PK.stage === 'opts' && !PK.moved && !PK.going) PK.opt = firstOn(optsOf(s, PK.rows[PK.sel]));
    patch(pk, PK.stage === 'list' ? listHTML(s) : PK.stage === 'ask' ? askHTML(s) : optsHTML(s));
    pk.setAttribute('aria-label', PK.kind === 'fork' ? '分叉' : '退回');
    pk.hidden = false; place();
    const l = pk.querySelector<HTMLElement>('.bk-pk-l'), r = pk.querySelector<HTMLElement>('.bk-r.bk-sel');
    if (l && r) { if (r.offsetTop < l.scrollTop) l.scrollTop = r.offsetTop - 2; else if (r.offsetTop + r.offsetHeight > l.scrollTop + l.clientHeight) l.scrollTop = r.offsetTop + r.offsetHeight - l.clientHeight + 2; }
    if (first) anim(pk, [{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], 150);
  }
  function closePick() { if (!PK.open && pk.hidden) return; PK.open = false; PK.rows = []; PK.going = false; pk.hidden = true; patch(pk, ''); unpad(); }
  function openPick(kind: 'rewind' | 'fork', s: Sess | undefined) {
    if (!s || !ctx.chat() || ctx.current()?.id !== s.id) return;
    if (s.term) { ctx.toast('在终端里，先拿回来'); return; }
    const items = ctx.items(s.id) ?? [];
    const rows: Row[] = items.flatMap((it, i) => it.k === 'you' && !it.queued && it.id && (it.text || it.files?.length) ? [{ it, i }] : []);
    if (!rows.length) { ctx.toast('这个会话里你还没说过话'); return; }
    if (kind === 'fork') rows.push({ i: items.length });
    ctx.closeMenu(); closeBar(); closeEx(true);
    Object.assign(PK, { open: true, kind, stage: 'list', sid: s.id, rows, sel: rows.length - 1, opt: 0, going: false, dry: null });
    drawPick(true);
    ta.focus({ preventScroll: true });
  }
  function move(d: number) {
    const s = ctx.byId(PK.sid);
    if (!s || PK.going) return;
    if (PK.stage === 'list') PK.sel = Math.max(0, Math.min(PK.rows.length - 1, PK.sel + d));
    else {
      const opts: { off?: boolean }[] = PK.stage === 'ask' ? [{}, {}] : optsOf(s, PK.rows[PK.sel]);
      let j = PK.opt;
      for (let k = 0; k < opts.length; k++) { j = (j + d + opts.length) % opts.length; if (!opts[j].off) break; }
      PK.opt = j; PK.moved = true;
    }
    drawPick();
  }
  function back() { if (PK.stage === 'list' || PK.going) closePick(); else { PK.stage = 'list'; PK.opt = 0; PK.moved = false; drawPick(); } }
  function num(j: number) {
    const s = ctx.byId(PK.sid);
    if (!s || PK.stage === 'list' || PK.going) return;
    const opts: { off?: boolean }[] = PK.stage === 'ask' ? [{}, {}] : optsOf(s, PK.rows[PK.sel]);
    if (!opts[j] || opts[j].off) return;
    PK.opt = j; PK.moved = true; void go();
  }
  // What the files would do, asked of the host once per sentence you look at (Claude only).
  function dryRun(s: Sess, r: Row) {
    const at = r.it?.id;
    if (s.agent !== 'claude' || !at || PK.dry?.at === at) return;
    void ctx.call<{ can: boolean; why?: string; files: string[]; add: number; del: number }>(`/sessions/${s.id}/rewind?at=${encodeURIComponent(at)}`).then(d => {
      PK.dry = { at, ...d };
      if (PK.open && PK.stage === 'opts' && PK.rows[PK.sel]?.it?.id === at) { const o = optsOf(s, r); if (o[PK.opt]?.off) { PK.opt = firstOn(o); PK.moved = false; } drawPick(); }
    }, () => {});
  }
  async function go() {
    const s = ctx.byId(PK.sid), r = PK.rows[PK.sel];
    if (!PK.open || PK.going) return;
    if (!s || !r) { closePick(); return; }
    if (PK.stage === 'list') {
      ctx.tick();
      if (PK.kind === 'fork') { forkFrom(s, r); return; }
      PK.opt = 0; PK.moved = false;
      if (busy(s)) PK.stage = 'ask'; else { PK.stage = 'opts'; dryRun(s, r); }
      drawPick(); return;
    }
    if (PK.stage === 'ask') {
      if (PK.opt === 1) { closePick(); return; }
      stop(s);
      PK.stage = 'opts'; PK.moved = false; dryRun(s, r); drawPick(); return;
    }
    const o = optsOf(s, r)[PK.opt];
    if (!o || o.off) return;
    if (!o.v) { closePick(); return; }
    await rewind(s, r, o.v);
  }
  // Stopped the way the stop button stops it (quietly, her and all); a turn waiting on an answer, through the host.
  function stop(s: Sess) {
    const b = comp.querySelector<HTMLElement>('[data-act="interrupt"]');
    if (b && s.st !== 'wait') b.click(); else { ctx.cue('interrupt'); void ctx.tryCall(`/sessions/${s.id}/interrupt`, {}); }
  }
  async function idle(id: string, ms = 8000) {
    for (const t0 = performance.now(); performance.now() - t0 < ms; await sleep(80)) { const s = ctx.byId(id); if (!s || !busy(s)) return true; }
    return false;
  }

  // ---------- going back: the conversation to before what you said, with its files or without; the sentence comes back ----------
  async function rewind(s: Sess, r: Row, v: 1 | 2 | 3) {
    const it = r.it!, id = s.id;
    PK.going = true; drawPick();
    const fail = () => { PK.going = false; if (PK.open) drawPick(); };
    if (!await idle(id)) { fail(); ctx.toast('它还没停下来，等一下再退'); return; }
    if (v === 3) {
      if (!await ctx.tryCall(`/sessions/${id}/rewind`, { at: it.id })) { fail(); return; }
      closePick(); ctx.cue('back');
      return;
    }
    // Before the first thing you said nothing is left to keep, and a session cannot start empty: it goes back when you send.
    if (r.i === firstYou(id)) { closePick(); later.set(id, { kind: 'back', at: it.id, code: v === 1 }); put(it.text); ctx.cue('back', .6); return; }
    const res = await ctx.tryCall(`/sessions/${id}/fork`, { at: it.id, before: true, code: v === 1, archive: true, back: true });
    if (!res) { fail(); return; }
    closePick(); ctx.cue('back');
    await arrive(String(res.id), it.text);
  }
  // The new session is on screen once the page has heard of it; what you said goes back into its composer.
  async function arrive(id: string, text = '') {
    for (let k = 0; k < 100 && !ctx.byId(id); k++) await sleep(50);
    if (!ctx.byId(id)) return;
    ctx.open(id);
    if (!text) return;
    await frame(); await frame();
    put(text);
  }
  function put(text: string) {
    ta.value = text; ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.focus({ preventScroll: true }); ta.setSelectionRange(text.length, text.length);
    anim(box, [{ boxShadow: 'inset 0 0 0 1px rgb(255 201 143 / .8),0 0 0 4px rgb(255 201 143 / .1)' }, { boxShadow: 'inset 0 0 0 1px rgb(255 201 143 / .8),0 0 0 4px rgb(255 201 143 / .1)', offset: .4 },
      { boxShadow: 'inset 0 0 0 .5px rgb(157 180 255 / .6),0 0 0 3px rgb(157 180 255 / .08)' }], 900, 'cubic-bezier(.22,.8,.24,1)');
  }

  // ---------- forking: from before one thing you said, or the whole conversation; the new one starts when you send ----------
  function forkFrom(s: Sess, r: Row) {
    closePick();
    later.set(s.id, r.it ? { kind: 'fork', at: r.it.id } : { kind: 'fork' });
    if (r.it) put(r.it.text);
    else { ta.focus({ preventScroll: true }); ta.setSelectionRange(ta.value.length, ta.value.length); }
    ctx.draw('comp');
  }
  function unfork(s: Sess) {
    const p = later.get(s.id);
    if (!p) return false;
    later.delete(s.id);
    if (p.at) { ta.value = ''; ta.dispatchEvent(new Event('input', { bubbles: true })); }
    ctx.draw('comp');
    return true;
  }
  async function forkSend(s: Sess, p: Later, text: string, files: Upload[]) {
    later.delete(s.id); ctx.draw('comp');
    const body = p.kind === 'back' ? { at: p.at, before: true, code: p.code, archive: true, back: true, text, files } : { ...p.at ? { at: p.at, before: true } : {}, text, files };
    const r = await ctx.tryCall(`/sessions/${s.id}/fork`, body);
    if (!r) { later.set(s.id, p); if (ctx.current()?.id === s.id && !ta.value) put(text); ctx.draw('comp'); return; }
    ctx.cue('send', .8);
    await arrive(String(r.id));
  }

  // ---------- selected words in an answer: a small bar over them ----------
  const SEL = { text: '', sid: '', i: -1, para: -1, inQ: '' };
  const closeBar = () => { bar.hidden = true; };
  function onSelect() {
    const g = getSelection(), s = ctx.current();
    if (!g || g.isCollapsed || !g.rangeCount || !s || !ctx.chat()) { closeBar(); return; }
    const r = g.getRangeAt(0), n = r.commonAncestorContainer, el = n.nodeType === 1 ? n as Element : n.parentElement;
    const md = el?.closest('.c-items .it .md'), text = g.toString().trim();
    if (!md || text.length < 2) { closeBar(); return; }
    const end = r.endContainer.nodeType === 1 ? r.endContainer as Element : r.endContainer.parentElement, blk = end?.closest('.md > *'), q = blk?.closest<HTMLElement>('.bk-q');
    const item = md.closest('.item'), i = item?.parentElement ? [...item.parentElement.children].indexOf(item) : -1;
    if (ctx.items(s.id)?.[i]?.k !== 'it') { closeBar(); return; }
    Object.assign(SEL, { text, sid: s.id, i, inQ: q?.dataset.q ?? '', para: blk && !q ? [...md.children].filter(x => !x.hasAttribute('data-x')).indexOf(blk) : -1 });
    patch(bar, `<button type="button" data-act="bk-quote">${I.quote}<span>引用</span></button><button type="button" data-act="bk-ask">${I.btw}<span>问一句</span></button>`);
    bar.hidden = false;
    // Under the words, from where they start and inside the answer's column, so it covers neither the line above nor
    // the margin; over them only when the composer is too close below.
    const rr = r.getBoundingClientRect(), w = win.getBoundingClientRect(), col = md.getBoundingClientRect(), pw = bar.offsetWidth, ph = bar.offsetHeight;
    const lim = host.getBoundingClientRect().bottom, under = rr.bottom + 6 + ph <= lim - 4;
    bar.style.left = `${Math.max(col.left, Math.min(col.right - pw, rr.left)) - w.left}px`;
    bar.style.top = `${(under ? rr.bottom + 6 : Math.max(host.getBoundingClientRect().top + 4, rr.top - ph - 6)) - w.top}px`;
  }
  host.addEventListener('mouseup', () => setTimeout(onSelect, 0));
  host.addEventListener('keyup', e => { if (e.shiftKey) onSelect(); });
  document.addEventListener('selectionchange', () => { if (bar.hidden) return; const g = getSelection(); if (!g || g.isCollapsed) closeBar(); });
  host.addEventListener('scroll', e => { if ((e.target as Element).classList?.contains('conv')) closeBar(); }, true);
  function quote(text: string) {
    const t = text.replace(/\s+/g, ' ').trim(), cut = t.length > 160 ? `${t.slice(0, 160)}…` : t;
    ta.value = `> ${cut}\n\n${ta.value}`; ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.focus({ preventScroll: true }); ta.setSelectionRange(ta.value.length, ta.value.length);
  }

  // ---------- 侧问: a question on the side, answered under the paragraph it is about; never in the conversation ----------
  const Q = new Map<string, Side>(), byKey = new Map<string, Map<number, string>>();
  let qn = 0, focusQ = '', caret = 0, shownWas = '';
  const keyOf = (sid: string, it: Answer, i: number) => `${sid}:${it.id ?? i}`;
  const right = (q: Side) => ctx.wb.shown() === `btw:${q.id}`;
  function sideAt(s: Sess, it: Answer, i: number, para: number, words: string) {
    const k = keyOf(s.id, it, i);
    let m = byKey.get(k);
    if (!m) byKey.set(k, m = new Map());
    let q = Q.get(m.get(para) ?? '');
    if (!q) { q = { id: `q${++qn}`, sid: s.id, key: k, para, words: '', qa: [], st: 'open', draft: '', asking: false, top: 0, pin: true, fresh: false }; Q.set(q.id, q); m.set(para, q.id); }
    if (words) q.words = said(words, 60);
    return q;
  }
  // An answer's own markdown, drawn the way the page draws answers, without a second .md inside the first.
  const mdIn = (t: string) => { const h = ctx.md(t); return h.startsWith('<div class="md">') ? h.slice(16, -6) : h; };
  const qaHTML = (q: Side) => q.qa.map((x, j) => `<div class="bk-qa"><p class="bk-qq">${esc(x.q)}</p>${x.a === undefined ? '<div class="bk-sk" role="img" aria-label="在想"><i></i><i></i></div>'
    : x.err ? `<p class="bk-qa-a bk-err">${esc(x.a)}</p>`
    : `<div class="bk-qa-a">${mdIn(x.a)}</div><div class="bk-qb"><button type="button" data-act="bk-q-copy" data-q="${q.id}" data-j="${j}">${I.copy}复制</button><button type="button" data-act="bk-q-put" data-q="${q.id}" data-j="${j}">放进输入框</button></div>`}</div>`).join('');
  // The box keeps no value in its markup: what you type is put back after a redraw, so typing never redraws it.
  const inHTML = (q: Side) => `<label class="bk-q-in"><input type="text" class="bk-qi" data-q="${q.id}" placeholder="${q.qa.length ? '接着问' : '问这一段'}" aria-label="侧问" autocomplete="off" spellcheck="false">${kbd('⏎')}</label>`;
  const blockHTML = (q: Side) => `<div class="bk-q" data-x data-q="${q.id}"><div class="bk-q-h"><span class="bk-q-k">${I.btw}侧问</span><span class="bk-q-w">${q.words ? `「${esc(q.words)}」` : ''}</span>`
    + `<button type="button" class="bk-q-b" data-act="bk-q-right" data-q="${q.id}">拖到右边</button><button type="button" class="bk-q-b" data-act="bk-q-min" data-q="${q.id}">收起</button></div>`
    + `<div class="bk-q-s">${qaHTML(q)}</div>${inHTML(q)}</div>`;
  const panelHTML = (q: Side) => `<div class="bk-sp" data-q="${q.id}">${q.words ? `<p class="bk-sp-w">「${esc(q.words)}」</p>` : ''}<div class="bk-sp-s">${qaHTML(q)}</div><div class="bk-sp-in">${inHTML(q)}</div></div>`;
  function markIn(blk: Element, q: Side) {
    const on = right(q), n = q.qa.length;
    const b = `<button type="button" class="bk-mk${on ? ' bk-on' : ''}" data-x data-act="bk-mk" data-q="${q.id}" data-n="${n > 1 ? n : ''}" data-tip="${on ? '侧问 · 在右边' : '侧问 · 点开'}" aria-label="侧问${q.words ? `：${esc(q.words)}` : ''}">${I.btw}</button>`;
    const at = /^(UL|OL)$/.test(blk.tagName) ? blk.lastElementChild : /^(P|LI|H[1-6]|BLOCKQUOTE)$/.test(blk.tagName) ? blk : null;
    if (at) at.insertAdjacentHTML('beforeend', b); else blk.insertAdjacentHTML('afterend', `<div class="bk-mkr" data-x>${b}</div>`);
  }
  function focusIn(q: Side) {
    const i = win.querySelector<HTMLInputElement>(`.bk-q .bk-qi[data-q="${q.id}"]`) ?? (right(q) ? win.querySelector<HTMLInputElement>(`.bk-sp .bk-qi[data-q="${q.id}"]`) : null);
    if (i) { i.focus({ preventScroll: true }); i.setSelectionRange(i.value.length, i.value.length); }
  }
  // A block or panel drawn anew gets back what you were typing, where its list was scrolled, and the keys if it had them.
  function settle() {
    for (const el of win.querySelectorAll<HTMLElement>('.bk-q:not([data-on]),.bk-sp:not([data-on])')) {
      el.dataset.on = '';
      const q = Q.get(el.dataset.q ?? '');
      if (!q) continue;
      const sc = el.querySelector<HTMLElement>('.bk-q-s');
      if (sc) { sc.scrollTop = q.pin ? sc.scrollHeight : q.top; sc.classList.toggle('bk-top', sc.scrollTop > 2); }
      const inp = el.querySelector<HTMLInputElement>('.bk-qi');
      if (inp) {
        inp.value = q.draft;
        const lost = !document.activeElement || document.activeElement === document.body;
        if (focusQ === q.id && lost && el.classList.contains('bk-q')) { inp.focus({ preventScroll: true }); const c = Math.min(caret, inp.value.length); inp.setSelectionRange(c, c); }
      }
      if (q.fresh && el.classList.contains('bk-q')) { q.fresh = false; anim(el.querySelector('.bk-qa:last-child .bk-qa-a'), [{ opacity: 0 }, { opacity: 1 }], 220); }
    }
    // The mark by a paragraph lights while its question is on the right, and goes out when the sheet is put away.
    const sh = ctx.wb.shown();
    if (sh !== shownWas) { if (sh.startsWith('btw:') || shownWas.startsWith('btw:')) ctx.draw('main'); shownWas = sh; }
  }
  new MutationObserver(settle).observe(win.querySelector('.bd')!, { childList: true, subtree: true });
  win.addEventListener('focusin', e => { const t = e.target as HTMLElement; if (t.matches?.('.bk-qi')) focusQ = t.dataset.q ?? ''; });
  win.addEventListener('focusout', e => {
    if (!(e.target as HTMLElement).matches?.('.bk-qi')) return;
    setTimeout(() => { const a = document.activeElement as HTMLElement | null; if (a && a !== document.body && !a.matches('.bk-qi')) focusQ = ''; }, 0);
  });
  win.addEventListener('input', e => {
    const t = e.target as HTMLInputElement;
    if (e.target === ta) { if (PK.open) closePick(); return; }
    if (!t.matches?.('.bk-qi')) return;
    const q = Q.get(t.dataset.q ?? '');
    if (q) { q.draft = t.value; caret = t.selectionStart ?? t.value.length; }
  });
  win.addEventListener('keyup', e => { const t = e.target as HTMLInputElement; if (t.matches?.('.bk-qi')) caret = t.selectionStart ?? t.value.length; });
  // A list scrolled up fades at its top edge, and stays where you left it.
  win.addEventListener('scroll', e => {
    const t = e.target as HTMLElement;
    if (!t.classList?.contains('bk-q-s')) return;
    t.classList.toggle('bk-top', t.scrollTop > 2);
    const q = Q.get(t.closest<HTMLElement>('.bk-q')?.dataset.q ?? '');
    if (q) { q.top = t.scrollTop; q.pin = t.scrollTop >= t.scrollHeight - t.clientHeight - 4; }
  }, true);
  // New questions and answers go wherever the question shows: the block (a redraw of its answer) and the panel in place.
  function sync(q: Side, fresh = false) {
    q.fresh = fresh;
    if (ctx.current()?.id === q.sid) ctx.draw('main');
    const sp = right(q) ? win.querySelector<HTMLElement>(`.pv-view .bk-sp[data-q="${q.id}"]`) : null;
    if (!sp) return;
    patch(sp.querySelector('.bk-sp-s')!, qaHTML(q));
    const v = sp.closest<HTMLElement>('.pv-view');
    if (v) v.scrollTop = v.scrollHeight;
    if (fresh) anim(sp.querySelector('.bk-qa:last-child .bk-qa-a'), [{ opacity: 0 }, { opacity: 1 }], 220);
    const i = sp.querySelector<HTMLInputElement>('.bk-qi');
    if (i) i.placeholder = q.qa.length ? '接着问' : '问这一段';
  }
  // One question at a time: it goes with this side talk so far, and the session's own turn goes on.
  async function ask(q: Side, text: string) {
    text = text.trim();
    if (!text || q.asking) return;
    const history = q.qa.filter(x => x.a !== undefined && !x.err).map(x => [x.q, x.a!] as [string, string]);
    const x: QA = { q: text };
    q.qa.push(x); q.draft = ''; q.asking = true; q.pin = true; caret = 0;
    for (const i of win.querySelectorAll<HTMLInputElement>(`.bk-qi[data-q="${q.id}"]`)) i.value = '';
    sync(q); ctx.cue('send', .5);
    try { x.a = (await ctx.call<{ text: string }>(`/sessions/${q.sid}/side`, { text, history })).text || '（没有回答）'; }
    catch (e) { x.a = `没问成：${e instanceof Error ? e.message : String(e)}`; x.err = true; }
    q.asking = false;
    sync(q, true);
  }
  function openSide(q: Side, quiet = false) {
    q.st = 'open'; q.pin = true; focusQ = q.id;
    if (right(q)) void ctx.wb.close();
    ctx.draw('main');
    void frame().then(frame).then(() => {
      const el = host.querySelector<HTMLElement>(`.bk-q[data-q="${q.id}"]`);
      if (!el) return;
      if (!quiet) anim(el, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], 200);
      el.scrollIntoView({ block: 'nearest', behavior: reduced.matches ? 'auto' : 'smooth' });
      focusIn(q);
    });
  }
  function fold(q: Side) {
    const el = host.querySelector<HTMLElement>(`.bk-q[data-q="${q.id}"]`);
    if (focusQ === q.id) { focusQ = ''; ta.focus({ preventScroll: true }); }
    ctx.cue('close', .6);
    const done = () => { q.st = 'min'; ctx.draw('main'); void frame().then(frame).then(() => anim(host.querySelector(`.bk-mk[data-q="${q.id}"]`), [{ transform: 'scale(1.6)', opacity: 0 }, { transform: 'none', opacity: 1 }], 260, SPRING)); };
    if (!el || reduced.matches) { done(); return; }
    el.style.overflow = 'hidden';
    el.animate([{ height: `${el.offsetHeight}px`, opacity: 1 }, { height: '0px', opacity: 0, marginTop: '0px', marginBottom: '0px' }], { duration: 180, easing: 'cubic-bezier(.4,0,.2,1)', fill: 'forwards' }).onfinish = done;
  }
  // On the right-hand sheet the same questions go on, the box at the bottom; the paragraph keeps a lit mark.
  function toRight(q: Side, from: HTMLElement | null) {
    if (ctx.current()?.id !== q.sid) return;
    q.st = 'min';
    if (focusQ === q.id) focusQ = '';
    void ctx.wb.show({ key: `btw:${q.id}`, ic: '问', b: '侧问', small: '不打断它 · 不进对话', fill: v => { v.innerHTML = panelHTML(q); v.scrollTop = v.scrollHeight; } }, from);
    ctx.draw('main');
    setTimeout(() => { if (right(q)) focusIn(q); }, 520);
  }
  // /btw (Claude) and /side (Codex): under the latest answer, asked at once when a question follows the command.
  function btw(s: Sess | undefined, text: string) {
    if (!s || !ctx.chat()) return;
    const items = ctx.items(s.id) ?? [];
    let i = items.length - 1;
    while (i >= 0 && items[i].k !== 'it') i--;
    if (i < 0) { ctx.toast('还没有回答可以问'); return; }
    const it = items[i] as Answer, t = document.createElement('template');
    t.innerHTML = ctx.md(it.text);
    const q = sideAt(s, it, i, Math.max(0, (t.content.firstElementChild?.childElementCount ?? 1) - 1), '');
    openSide(q);
    if (text) void ask(q, text);
  }
  function askSel() {
    const s = ctx.byId(SEL.sid), it = s ? ctx.items(s.id)?.[SEL.i] : undefined;
    getSelection()?.removeAllRanges(); closeBar();
    if (SEL.inQ) { const q = Q.get(SEL.inQ); if (q) focusIn(q); return; }
    if (!s || it?.k !== 'it') return;
    openSide(sideAt(s, it, SEL.i, Math.max(0, SEL.para), SEL.text));
  }
  // 拖到右边, literally: hold the block's top row and pull it right.
  let drag: { el: HTMLElement; id: string; x0: number; y0: number; dx: number; on: boolean } | null = null, dragAt = -1e9;
  host.addEventListener('pointerdown', e => {
    const t = e.target as Element, h = t.closest?.('.bk-q-h');
    if (!h || e.button !== 0 || t.closest('button')) return;
    const el = h.closest<HTMLElement>('.bk-q')!;
    drag = { el, id: el.dataset.q ?? '', x0: e.clientX, y0: e.clientY, dx: 0, on: false };
  });
  addEventListener('pointermove', e => {
    const d = drag;
    if (!d) return;
    const dx = e.clientX - d.x0, dy = e.clientY - d.y0;
    if (!d.on && dx > 6 && dx > Math.abs(dy)) { d.on = true; d.el.classList.add('bk-drag'); getSelection()?.removeAllRanges(); }
    if (d.on) { d.dx = Math.max(0, dx); d.el.style.transform = `translateX(${d.dx}px)`; d.el.style.opacity = String(1 - Math.min(.55, d.dx / 360)); }
  });
  addEventListener('pointerup', () => {
    const d = drag, q = d ? Q.get(d.id) : undefined;
    drag = null;
    if (!d?.on) return;
    dragAt = performance.now();
    if (d.dx > 90 && q && d.el.isConnected) { toRight(q, d.el); return; }
    d.el.classList.remove('bk-drag');
    anim(d.el, [{ transform: `translateX(${d.dx}px)`, opacity: d.el.style.opacity }, { transform: 'none', opacity: 1 }], 180);
    d.el.style.transform = ''; d.el.style.opacity = '';
  });

  // ---------- 导出: the whole conversation as one Markdown file, looked at first ----------
  const X = { name: '', text: '' };
  async function openEx(s: Sess | undefined) {
    if (!s || !ctx.chat()) return;
    closePick(); closeBar(); ctx.closeMenu();
    const r = await ctx.tryCall(`/sessions/${s.id}/export`);
    if (!r) return;
    X.name = String(r.name); X.text = String(r.text);
    const lines = X.text.trimEnd().split('\n').length, turns = (X.text.match(/^## 你/gm) ?? []).length;
    ex.innerHTML = `<div class="bk-ex-scrim" data-act="bk-ex-x"></div><section class="sheet bk-ex-s" role="dialog" aria-modal="true" aria-label="导出成 Markdown">`
      + `<div class="sh"><span class="ic fi">MD</span><b>${esc(X.name)}</b><small>整段对话 · ${turns} 轮 · ${lines} 行</small><button type="button" class="bk-x" data-act="bk-ex-x" aria-label="关掉" data-tip="关掉" data-key="esc">${I.x}</button></div>`
      + `<div class="pv-body"><div class="pv-view"><div class="bk-md">${ctx.md(X.text)}</div></div></div>`
      + `<div class="bk-ex-f"><span>你说的、它答的原样留着，每一步一行</span><button type="button" class="btn" data-act="bk-ex-copy">${I.copy}复制</button><button type="button" class="btn warm" data-act="bk-ex-save">存成文件… ${kbd('⏎')}</button></div></section>`;
    ex.hidden = false;
    anim(ex.querySelector('.bk-ex-s'), [{ opacity: 0, transform: 'translateY(-10px)' }, { opacity: 1, transform: 'none' }], 220);
    anim(ex.querySelector('.bk-ex-scrim'), [{ opacity: 0 }, { opacity: 1 }], 200);
    ex.querySelector<HTMLElement>('[data-act="bk-ex-save"]')!.focus({ preventScroll: true });
    ctx.cue('open', .6);
  }
  function closeEx(quiet = false) {
    if (ex.hidden) return;
    ex.hidden = true; ex.innerHTML = '';
    if (!quiet) { ctx.cue('close', .6); ta.focus({ preventScroll: true }); }
  }
  async function copyEx() {
    try { await navigator.clipboard.writeText(X.text); ctx.tick(); ctx.toast(`复制了 Markdown · ${X.text.trimEnd().split('\n').length} 行`); }
    catch { ctx.toast('剪贴板用不了，存成文件吧'); }
    // ⏎ still saves after a copy.
    ex.querySelector<HTMLElement>('[data-act="bk-ex-save"]')?.focus({ preventScroll: true });
  }
  // Kept where you pick in the Mac's own save dialog (agentsWindow.ts), ~/Downloads to start with.
  async function saveEx() {
    if (!window.agents?.saveFile) { ctx.toast('这里存不了文件，先复制吧'); return; }
    const p = await window.agents.saveFile(X.name, X.text).catch((e: unknown) => { ctx.toast(`没存成：${e instanceof Error ? e.message : String(e)}`); return ''; });
    if (!p) return;
    closeEx(true); ta.focus({ preventScroll: true });
    ctx.cue('done', .6); ctx.toast(`存到了 ${p.replace(/^\/Users\/[^/]+/, '~')}`);
  }

  // ---------- where they are reached: / commands, the title menu, what you send, clicks, keys ----------
  ctx.own.set('rewind', s => openPick('rewind', s));
  ctx.own.set('fork', s => openPick('fork', s));
  ctx.own.set('export', s => void openEx(s));
  ctx.own.set('btw', (s, arg) => btw(s, arg));
  win.addEventListener('pointerdown', e => { if (PK.open && !(e.target as Element).closest?.('.bk-pk')) closePick(); }, true);
  new ResizeObserver(() => { if (PK.open) place(); }).observe(comp);
  return {
    act(a, el) {
      if (!a.startsWith('bk-')) return false;
      const s = ctx.current(), q = Q.get(el.dataset.q ?? ''), j = Number(el.dataset.j);
      if (a === 'bk-fork') openPick('fork', s);
      else if (a === 'bk-export') void openEx(s);
      else if (a === 'bk-row') { if (!PK.going) { PK.sel = j; void go(); } }
      else if (a === 'bk-opt') num(j);
      else if (a === 'bk-unfork') { if (s) unfork(s); }
      else if (a === 'bk-quote') { quote(SEL.text); getSelection()?.removeAllRanges(); closeBar(); }
      else if (a === 'bk-ask') askSel();
      else if (a === 'bk-mk' && q) openSide(q);
      else if (a === 'bk-q-min' && q) fold(q);
      else if (a === 'bk-q-right' && q) { if (performance.now() - dragAt > 300) toRight(q, el.closest<HTMLElement>('.bk-q')); }
      else if (a === 'bk-q-copy' && q) { const x = q.qa[j]; if (x?.a) navigator.clipboard.writeText(x.a).then(() => { ctx.tick(); ctx.toast('复制了这个回答'); }, () => ctx.toast('剪贴板用不了')); }
      else if (a === 'bk-q-put' && q) {
        const x = q.qa[j];
        if (!x?.a) return true;
        ta.value = `${x.a.split('\n').map(l => `> ${l}`).join('\n')}\n\n${ta.value}`; ta.dispatchEvent(new Event('input', { bubbles: true }));
        ta.focus({ preventScroll: true }); ta.setSelectionRange(ta.value.length, ta.value.length);
        ctx.toast('放进输入框了：发出去，它才进对话');
      }
      else if (a === 'bk-ex-x') closeEx();
      else if (a === 'bk-ex-copy') void copyEx();
      else if (a === 'bk-ex-save') void saveEx();
      else return false;
      return true;
    },
    esc() {
      if (!bar.hidden) { closeBar(); return true; }
      const s = ctx.current();
      return !!s && unfork(s);
    },
    key(e) {
      const k = e.key, t = e.target as HTMLElement, plain = !e.metaKey && !e.ctrlKey && !e.altKey, consume = () => { e.preventDefault(); e.stopImmediatePropagation(); };
      if (e.isComposing) return false;
      // The export sheet is modal: esc closes, ⏎ saves, the arrows scroll what it will save.
      if (!ex.hidden) {
        if (k === 'Escape') { consume(); closeEx(); return true; }
        if ((k === 'Enter' || k === ' ') && t.closest?.('.bk-ex button')) { e.stopImmediatePropagation(); return true; }
        if (k === 'Enter' && plain) { consume(); if (!e.repeat) void saveEx(); return true; }
        if (k === 'Tab') { consume(); const bs = [...ex.querySelectorAll<HTMLElement>('button')], i = bs.indexOf(t); bs[(i + (e.shiftKey ? -1 : 1) + bs.length) % bs.length]?.focus(); return true; }
        const by = ({ ArrowDown: 48, ArrowUp: -48, PageDown: 320, PageUp: -320, ' ': 320 } as Record<string, number>)[k];
        if (by && plain) { consume(); const v = ex.querySelector<HTMLElement>('.pv-view'); if (v) v.scrollTop += by; return true; }
        if ((e.metaKey || e.ctrlKey) && /^[ac]$/i.test(k)) { e.stopImmediatePropagation(); return true; }
        consume(); return true;
      }
      if (PK.open) {
        if (!plain) { closePick(); return false; }
        if (k === 'ArrowDown' || k === 'ArrowUp') { consume(); move(k === 'ArrowDown' ? 1 : -1); return true; }
        if ((k === 'Enter' && !e.shiftKey) || k === 'ArrowRight' || k === 'Tab') { consume(); if (!e.repeat) void go(); return true; }
        if (k === 'Escape' || k === 'ArrowLeft') { consume(); back(); return true; }
        if (/^[1-9]$/.test(k) && PK.stage !== 'list') { consume(); num(Number(k) - 1); return true; }
        return false;
      }
      // The box a side question is typed in: ⏎ asks, esc folds it away (or, on the right, puts the sheet away).
      if (t.matches?.('input.bk-qi')) {
        const q = Q.get(t.dataset.q ?? '');
        if (!q) return false;
        if (k === 'Enter' && !e.shiftKey) { consume(); if (!e.repeat) void ask(q, (t as HTMLInputElement).value); return true; }
        if (k === 'Escape') { consume(); if (t.closest('.pv')) { focusQ = ''; void ctx.wb.close(); ta.focus({ preventScroll: true }); } else fold(q); return true; }
      }
      return false;
    },
    more: () => '<button type="button" data-act="bk-fork">分叉…<span class="k">/fork</span></button><button type="button" data-act="bk-export">导出成 Markdown…<span class="k">/export</span></button>',
    answer(s, it, i, html) {
      const m = byKey.get(keyOf(s.id, it, i));
      if (!m?.size || (!it.id && i < 0)) return html;
      const t = document.createElement('template');
      t.innerHTML = html;
      const md = t.content.querySelector('.md'), blocks = md ? [...md.children].filter(x => !x.hasAttribute('data-x')) : [];
      if (!blocks.length) return html;
      for (const [para, id] of m) {
        const q = Q.get(id);
        if (!q) continue;
        const blk = blocks[Math.min(Math.max(para, 0), blocks.length - 1)];
        if (q.st === 'open') blk.insertAdjacentHTML('afterend', blockHTML(q));
        else if (q.qa.length || right(q)) markIn(blk, q);
      }
      return t.innerHTML;
    },
    rows(s) {
      if (PK.open && PK.sid !== s.id) closePick();
      else if (PK.open) drawPick();
      const p = later.get(s.id);
      if (!p) return '';
      const [b, t] = p.kind === 'back' ? ['退回到最开头', `发出去就从这一句重新开始${p.code ? '，文件也退回去' : ''}，原来的归档`]
        : p.at ? ['从这一句分叉', '改完发出去是一个新会话，原来的不动'] : ['从现在分叉', '发出去是一个新会话，原来的不动'];
      return `<div class="bk-bn"><i></i><span><b>${b}</b> · ${t}</span><button type="button" data-act="bk-unfork">取消</button></div>`;
    },
    send(s, text, files) {
      const p = later.get(s.id);
      if (!p) return false;
      void forkSend(s, p, text, files);
      return true;
    },
  };
}

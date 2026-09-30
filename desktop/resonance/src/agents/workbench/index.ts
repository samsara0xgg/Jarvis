// The workbench (design pinned in docs/design/agents-workbench.html): what is at hand once you are in a session. The
// window has three postures and nothing to drag: 对话 most of the time; 并排 when a document, diff or the terminal
// opens beside it; 舞台 when a page, artifact, PDF or image wants the width and the conversation folds into a narrow
// column on the right. On the left, 落地 lays out the line from these changes to main. Every posture is a set of
// rectangles and one curve moves between them; the one heavy move is the card you clicked growing into the stage.
import type { Peek, Sess } from '../../../electron/agents/types';
import { classify, stripLine, type Ref } from './refs';
import { CLOSE_ICON, active, chipOf, panelHTML, railHTML } from './landing';
import { mountTerminal, type TPos } from './terminal';
import './workbench.css';

type Hooks = {
  api: string; call<T = unknown>(route: string, body?: unknown, method?: string): Promise<T>; toast(t: string): void; cue(name: string, gain?: number): void;
  current(): Sess | undefined; chat(): boolean; busy(): boolean; b01(): boolean;
  md(text: string): string; diff(d: NonNullable<Peek['diff']>): string; redraw(): void;
};
type Q = { x: number; y: number; w: number; h: number };
type Pos = 'chat' | 'side' | 'stage';
type Custom = { key: string; ic: string; b: string; small?: string; target?: 'side' | 'stage'; fill(view: HTMLElement): void };
const $ = <T extends Element = HTMLElement>(s: string, root: ParentNode) => root.querySelector(s) as T;
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const wait = (ms: number) => new Promise(r => setTimeout(r, ms));
const RM = matchMedia('(prefers-reduced-motion: reduce)');
const EASE = 'cubic-bezier(.22,.8,.24,1)';
const icon = {
  stage: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M9.5 2.5h4v4M6.5 13.5h-4v-4M13.5 2.5 9 7M2.5 13.5 7 9"/></svg>',
  side: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="2" y="3" width="12" height="10" rx="2"/><path d="M9.5 3v10"/></svg>',
  out: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M9 3h4v4M13 3 7.5 8.5M11 9.5V13H3V5h3.5"/></svg>',
};
// 记住习惯: a kind of page you leave the stage from at once three times running opens beside the conversation next.
const QUICK_MS = 3000;
type Habit = Record<string, { quick: number; side: boolean }>;
const loadHabit = (): Habit => { try { return JSON.parse(localStorage.getItem('agents.habit') ?? '{}') as Habit; } catch { return {}; } };

export function mountWorkbench(win: HTMLElement, ta: HTMLTextAreaElement, hooks: Hooks) {
  const bd = $('.bd', win), lp = $('.lp', bd), chat = $('.chat', bd), pv = $('.pv', bd), tm = $('.tm', bd), conv = () => $<HTMLElement>('.host .conv', chat);
  const pl = $('.pl', lp), foot = $('.lp-f', lp), br = $('.br', lp), rail = $('.rail', lp), chip = $('.land', win), hint = $('.hint', win), view = $('.pv-view', pv);
  $('.lp-h .ib', lp).innerHTML = CLOSE_ICON; $('[data-act="pvclose"]', pv).innerHTML = CLOSE_ICON; $('[data-act="pvext"]', pv).innerHTML = icon.out;
  // titles: a commit title written before the line starts, per session
  const S = { titles: new Map<string, string>(), pos: 'chat' as Pos, ref: null as (Ref & { src: HTMLElement | null; abs?: string; href?: string; custom?: boolean }) | null, left: false, wide: false, term: false, busy: false, openedAt: 0, doneAt: new Map<string, number>(), was: new Map<string, string>() };
  const habit = loadHabit();
  const terminal = mountTerminal(tm, { api: hooks.api, call: hooks.call, current: () => hooks.current()?.id ?? '', toast: hooks.toast, changed: () => {} });
  terminal.onPos = p => { terminal.setPos(p); layout(); };
  terminal.onClose = () => toggleTerm(false);

  // ---------- layout: every posture is a set of rectangles ----------
  const sess = () => hooks.chat() ? hooks.current() : undefined;
  function rects() {
    const W = bd.clientWidth, BH = bd.clientHeight, stage = S.pos === 'stage', I = 10, s = sess();
    const left = S.left && !!s, railed = left && !S.wide && (stage || (S.pos === 'side' && W < 1000));
    const L = left ? (railed ? 52 : 336) : 0;
    const a: Q = { x: L, y: 0, w: W - L, h: BH };
    const r: { lp: Q; chat: Q; pv: Q | null; tm: Q | null; railed: boolean } = { lp: { x: 0, y: 0, w: L, h: BH }, chat: a, pv: null, tm: null, railed };
    const T = S.term && !!s, tp = terminal.pos;
    if (T && tp === 'drawer') { r.tm = { x: L + I, y: BH - 240 - I, w: W - L - 2 * I, h: 240 }; a.h = BH - 250; }
    if (S.pos === 'chat' || !S.ref) {
      r.chat = { ...a };
      if (T && tp === 'side') { const pw = Math.round(a.w * .44); r.tm = { x: a.x + a.w - pw, y: I, w: pw - I, h: a.h - 2 * I }; r.chat.w -= pw; }
    } else if (S.pos === 'side') {
      const pw = Math.round(a.w * .46);
      r.pv = { x: a.x + a.w - pw, y: I, w: pw - I, h: a.h - 2 * I };
      r.chat = { x: a.x, y: 0, w: a.w - pw, h: a.h };
      if (T && tp === 'side') { const th = Math.round((a.h - 2 * I) * .42); r.pv.h -= th + 8; r.tm = { x: r.pv.x, y: r.pv.y + r.pv.h + 8, w: r.pv.w, h: th }; }
    } else {
      const cw = 372;
      r.chat = { x: a.x + a.w - cw, y: 0, w: cw, h: a.h };
      r.pv = { x: a.x + I, y: I, w: a.w - cw - I, h: a.h - 2 * I };
      if (T && tp === 'side') { r.tm = { x: r.chat.x + 6, y: a.h - 262 - I, w: cw - 6 - I, h: 262 }; r.chat.h = a.h - 272; }
    }
    if (T && tp === 'island') r.tm = stage && r.pv ? { x: r.pv.x + 16, y: BH - 236 - 26, w: 440, h: 236 } : { x: W - 440 - 22, y: BH - 236 - 132, w: 440, h: 236 };
    return r;
  }
  const put = (el: HTMLElement, q: Q) => Object.assign(el.style, { left: `${q.x}px`, top: `${q.y}px`, width: `${q.w}px`, height: `${q.h}px` });
  function layout(instant = false) {
    if (instant) win.classList.add('wb-nt');
    const r = rects(), s = sess();
    put(lp, r.lp); put(chat, r.chat);
    pv.classList.toggle('off', !r.pv);
    put(pv, r.pv ?? { x: r.chat.x + r.chat.w - 20, y: 10, w: 20, h: Math.max(0, r.chat.h - 20) });
    tm.classList.toggle('off', !r.tm); tm.classList.toggle('island', terminal.pos === 'island');
    if (r.tm) put(tm, r.tm);
    lp.classList.toggle('rl', r.railed);
    lp.classList.toggle('off', !(S.left && s));
    const nar = r.chat.w < 480;
    chat.classList.toggle('nar', nar);
    if (!nar) chat.classList.remove('all');
    win.classList.toggle('wb-right', !!r.pv || (!!r.tm && terminal.pos !== 'drawer' && terminal.pos !== 'island'));
    win.classList.toggle('wb-cover', (S.left && !!s) || S.pos === 'stage');
    terminal.show(!!r.tm, s?.id ?? '');
    hooks.redraw();
    if (instant) { void win.offsetWidth; win.classList.remove('wb-nt'); }
  }
  new ResizeObserver(() => layout(true)).observe(bd);

  // ---------- the preview sheet ----------
  const rel = (el: Element): Q => { const a = el.getBoundingClientRect(), b = bd.getBoundingClientRect(); return { x: a.left - b.left, y: a.top - b.top, w: a.width, h: a.height }; };
  function ghostFly(from: Q, to: Q, ms = 460) {
    if (RM.matches) return Promise.resolve();
    const g = document.createElement('div'); g.className = 'wb-ghost';
    Object.assign(g.style, { left: `${from.x}px`, top: `${from.y}px`, width: `${from.w}px`, height: `${from.h}px` });
    bd.append(g);
    const an = g.animate([
      { left: `${from.x}px`, top: `${from.y}px`, width: `${from.w}px`, height: `${from.h}px`, borderRadius: '12px' },
      { left: `${to.x}px`, top: `${to.y}px`, width: `${to.w}px`, height: `${to.h}px`, borderRadius: '14px' },
    ], { duration: ms, easing: EASE, fill: 'forwards' });
    return an.finished.then(() => { void g.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 160, fill: 'forwards' }).finished.then(() => g.remove()); }, () => g.remove());
  }
  const fadeConv = (to: number) => { const c = conv(); if (c) c.style.opacity = String(to); };
  function cards() { for (const c of win.querySelectorAll<HTMLElement>('.lnk')) c.classList.toggle('on', !!S.ref && classify(c.dataset.ref!).key === S.ref.key); }
  function flipIcon() {
    const f = $<HTMLElement>('[data-act="pvflip"]', pv), st = S.pos === 'stage';
    f.innerHTML = st ? icon.side : icon.stage; f.setAttribute('aria-label', st ? '放回旁边' : '推上舞台'); f.dataset.tip = st ? '放回旁边' : '推上舞台';
  }
  function head(ic: string, fi: boolean, b: string, small: string) {
    const i = $('.sh .ic', pv); i.textContent = ic; i.className = `ic${fi ? ' fi' : ''}`;
    $('.sh b', pv).textContent = b; $('.sh small', pv).textContent = small;
  }
  let loadTok = 0;
  async function fill(r: Ref, s: Sess) {
    const tok = ++loadTok;
    view.classList.remove('web'); view.scrollTop = 0;
    if (r.url) {
      let host = r.ref;
      try { host = new URL(r.ref).host.replace(/^www\./, ''); } catch { /* as written */ }
      const b = r.label || (r.kind === 'art' ? 'Artifact' : host);
      head([...b.trim()][0]?.toUpperCase() ?? '·', false, b, r.ref);
      web(r.ref, r.kind === 'pdf');
      S.ref!.href = r.ref;
      return;
    }
    const p = stripLine(r.ref), ext = /\.[a-z0-9]+$/i.exec(p)?.[0].toLowerCase() ?? '';
    head(ext.length <= 6 ? ext : '.txt', true, r.label || p, '在读…');
    view.innerHTML = '<p class="pv-wait">在读…</p>';
    const k = await hooks.call<Peek>(`/sessions/${s.id}/peek?ref=${encodeURIComponent(r.ref)}`).catch((e: unknown) => e instanceof Error ? e.message : String(e));
    if (tok !== loadTok || !S.ref) return;
    if (typeof k === 'string') { view.innerHTML = `<p class="pv-err">${esc(k)}</p>`; $('.sh small', pv).textContent = '打不开'; return; }
    S.ref.abs = k.abs;
    const dir = k.abs.replace(/\/[^/]+$/, '').replace(/^\/Users\/[^/]+/, '~');
    $('.sh small', pv).textContent = k.add || k.del ? `${dir} · 改了 ${(k.add ?? 0) + (k.del ?? 0)} 行` : dir;
    if (k.kind === 'web') { web(k.url!, /\.pdf$/i.test(k.abs)); return; }
    view.innerHTML = k.kind === 'md' ? `<div class="doc">${hooks.md(k.text ?? '')}</div>` : k.diff?.length ? `<div class="dv">${hooks.diff(k.diff)}</div>` : `<pre class="code-v">${esc(k.text ?? '')}</pre>`;
  }
  // Pages run in their own browser: their own session without the daemon's key, new windows go to the real browser.
  function web(url: string, pdf: boolean) {
    view.classList.add('web');
    view.innerHTML = '<p class="pv-wait">在打开…</p>';
    const w = document.createElement('webview');
    w.setAttribute('partition', 'persist:agents-web');
    w.setAttribute('allowpopups', '');
    if (pdf) w.setAttribute('plugins', '');
    w.setAttribute('src', url);
    w.addEventListener('dom-ready', () => view.querySelector('.pv-wait')?.remove(), { once: true });
    view.append(w);
  }
  async function open(el: HTMLElement) {
    const s = sess();
    if (S.busy || !s || !el.dataset.ref) return;
    const r = classify(el.dataset.ref, el.dataset.label ?? '');
    let target: Pos = r.target, note = false;
    if (target === 'stage' && habit[r.kind]?.side) { target = 'side'; note = true; }
    if (S.ref?.key === r.key && S.pos === target) return;
    S.busy = true;
    const from = rel(el), was = S.pos;
    S.ref = { ...r, src: el.closest('.lnk') ?? el };
    cards(); void fill(r, s);
    S.pos = target;
    pv.style.opacity = '0';
    if (target === 'stage' && was !== 'stage') { fadeConv(0); await wait(120); }
    layout(); flipIcon();
    await ghostFly(from, rects().pv!);
    pv.style.opacity = ''; fadeConv(1);
    if (target === 'stage') S.ref?.src?.scrollIntoView({ block: 'nearest' });
    S.openedAt = performance.now();
    showNote(note);
    S.busy = false;
  }
  // Anything else a feature shows beside the conversation (a subagent, a task's output, a side question): its own head
  // and body on the same sheet, one thing at a time, in and out the way a file comes and goes. `fill` draws into the
  // sheet and may keep drawing there while `shown()` is still its key.
  async function show(o: Custom, from: HTMLElement | null) {
    const s = sess();
    if (S.busy || !s) return;
    const target: Pos = o.target ?? 'side';
    if (S.ref?.key === o.key && S.pos === target) return;
    S.busy = true;
    const was = S.pos, start = from ? rel(from) : { x: bd.clientWidth - 40, y: bd.clientHeight / 2, w: 20, h: 20 };
    S.ref = { key: o.key, ref: o.key, kind: 'file', target, label: o.b, url: false, src: from, custom: true };
    cards(); ++loadTok; view.classList.remove('web'); view.scrollTop = 0;
    head(o.ic, false, o.b, o.small ?? ''); view.replaceChildren(); o.fill(view);
    S.pos = target;
    pv.style.opacity = '0';
    if (target === 'stage' && was !== 'stage') { fadeConv(0); await wait(120); }
    layout(); flipIcon();
    await ghostFly(start, rects().pv!);
    pv.style.opacity = ''; fadeConv(1);
    S.openedAt = performance.now();
    S.busy = false;
  }
  function showNote(on: boolean) {
    pv.querySelector('.pv-note')?.remove();
    if (!on) return;
    const n = document.createElement('p'); n.className = 'pv-note'; n.innerHTML = `上次你放在旁边 · 点 ${icon.stage} 推上舞台`;
    pv.append(n);
    setTimeout(() => n.animate({ opacity: [1, 0] }, { duration: 400, fill: 'forwards' }).finished.then(() => n.remove(), () => n.remove()), 4000);
  }
  // Back to the card it came from: the chat takes its width back, the sheet shrinks into that card, you are on the same line.
  async function close(quick = false) {
    if (S.busy || !S.ref) return;
    S.busy = true;
    const r = S.ref, from = rects().pv!, wasStage = S.pos === 'stage', card = r.src?.isConnected ? r.src : null;
    if (wasStage && !r.custom) {
      const h = habit[r.kind] ??= { quick: 0, side: false };
      h.quick = performance.now() - S.openedAt < QUICK_MS ? h.quick + 1 : 0;
      if (h.quick >= 3) { h.side = true; h.quick = 0; }
      localStorage.setItem('agents.habit', JSON.stringify(habit));
    }
    S.pos = 'chat';
    if (wasStage) fadeConv(0);
    // Where the card will sit once the chat has its width back.
    win.classList.add('wb-nt'); const fin = rects().chat; put(chat, fin); chat.classList.toggle('nar', fin.w < 480);
    void chat.offsetWidth; const to = card ? rel(card) : { x: fin.x + fin.w / 2 - 60, y: fin.h / 2, w: 120, h: 40 };
    S.pos = wasStage ? 'stage' : 'side'; layout(true); S.pos = 'chat';
    pv.style.opacity = '0';
    requestAnimationFrame(() => layout());
    await (quick ? Promise.resolve() : ghostFly(from, to, 420));
    S.ref = null; cards(); ++loadTok; view.replaceChildren(); showNote(false);
    pv.style.opacity = ''; fadeConv(1);
    if (card && !RM.matches) card.animate([{ boxShadow: 'inset 0 0 0 1px rgb(157 180 255 / .8)' }, { boxShadow: 'inset 0 0 0 .5px rgb(157 180 255 / .22)' }], { duration: 900, easing: 'ease-out' });
    S.busy = false;
  }
  async function flip() {
    if (S.busy || !S.ref) return;
    const toStage = S.pos !== 'stage';
    if (toStage) { const h = habit[S.ref.kind]; if (h) { h.side = false; h.quick = 0; localStorage.setItem('agents.habit', JSON.stringify(habit)); } fadeConv(0); await wait(110); }
    S.pos = toStage ? 'stage' : 'side'; S.openedAt = performance.now(); showNote(false);
    layout(); flipIcon();
    await wait(260); fadeConv(1);
  }

  // ---------- landing ----------
  const drawn = new WeakMap<Element, string>();
  function renderLand() {
    const s = sess();
    if (!s) { chip.hidden = true; return; }
    const p = panelHTML(s, S.titles.get(s.id) ?? '');
    // Step by step, so the title being typed is never redrawn under the caret.
    let moved = false;
    p.steps.forEach((h, i) => {
      const li = pl.children[i] as HTMLElement | undefined;
      if (li && drawn.get(li) === h) return;
      if (li && li.contains(document.activeElement) && document.activeElement?.id === 'landmsg') return;
      const t = document.createElement('template'); t.innerHTML = h;
      const nu = t.content.firstElementChild as HTMLElement;
      drawn.set(nu, h); moved = true;
      if (li) li.replaceWith(nu); else pl.append(nu);
    });
    if (foot.innerHTML !== p.foot) foot.innerHTML = p.foot;
    if (br.innerHTML !== p.branch) br.innerHTML = p.branch;
    const rl = railHTML(s); if (rail.innerHTML !== rl) rail.innerHTML = rl;
    if (moved) reveal();
    // The chip: where the line is while the panel is shut; green when done, then gone a few seconds later.
    const c = chipOf(s), doneAt = S.doneAt.get(s.id) ?? 0, gone = c?.k === 'done' && performance.now() - doneAt > 4800;
    chip.hidden = !c || gone;
    if (c && !gone) { chip.dataset.s = c.k; if (chip.innerHTML !== c.html) chip.innerHTML = c.html; chip.style.opacity = ''; }
  }
  // Keep the step that is moving in view, gently: as it moves, and when the panel opens on it.
  function reveal() {
    const cur = pl.querySelector<HTMLElement>('.st[data-s="run"],.st[data-s="wait"],.st[data-s="paused"],.st[data-s="fail"]');
    if (!cur || !S.left) return;
    const top = cur.offsetTop - pl.offsetTop, bot = top + cur.offsetHeight;
    if (top < pl.scrollTop || bot > pl.scrollTop + pl.clientHeight) pl.scrollTo({ top: Math.max(0, Math.min(top - 8, bot - pl.clientHeight + 12)), behavior: RM.matches ? 'auto' : 'smooth' });
  }
  const openPanel = () => { S.left = true; S.wide = false; layout(); requestAnimationFrame(reveal); };
  // A session's row changed: its landing moved on, sounds for what needs Allen, and the chip's fade after it is done.
  function saw(s: Sess) {
    const was = S.was.get(s.id), now = s.land?.s ?? '';
    S.was.set(s.id, now);
    if (s.id !== hooks.current()?.id || was === now) { if (s.id === hooks.current()?.id) renderLand(); return; }
    if (now === 'wait') hooks.cue('ask');
    else if (now === 'fail') hooks.cue('error');
    else if (now === 'done' && was) {
      hooks.cue('done', .6); S.doneAt.set(s.id, performance.now());
      setTimeout(() => { if (!RM.matches && !chip.hidden) chip.animate({ opacity: [1, 0] }, { duration: 600, fill: 'forwards' }).finished.then(() => { chip.getAnimations().forEach(a => a.cancel()); renderLand(); }, () => {}); else renderLand(); }, 4200);
    }
    if (active(s.land) && !was && !S.left) openPanel();
    renderLand();
  }
  async function land(action: string, extra: Record<string, unknown> = {}) {
    const s = sess();
    if (!s) return;
    await hooks.call(`/sessions/${s.id}/land`, { action, ...extra }).catch((e: unknown) => hooks.toast(e instanceof Error ? e.message : String(e)));
  }
  function startLanding() {
    const s = sess();
    if (!s) return;
    const st = s.land?.s;
    if (st === 'run' || st === 'stopping' || st === 'wait' || st === 'fixing') { if (!S.left) openPanel(); return; }
    if (!s.dirty && !s.land) { hooks.toast('没有要落地的改动'); return; }
    hooks.cue('send', .7);
    if (!S.left) openPanel();
    void land(st === 'paused' || st === 'fail' ? 'resume' : 'start');
  }
  let msgTimer = 0;
  win.addEventListener('focusout', e => { if ((e.target as HTMLElement).id === 'landmsg') requestAnimationFrame(renderLand); });
  win.addEventListener('input', e => {
    if ((e.target as HTMLElement).id !== 'landmsg') return;
    const v = (e.target as HTMLInputElement).value, s = sess();
    if (s && !s.land) S.titles.set(s.id, v);
    clearTimeout(msgTimer); msgTimer = window.setTimeout(() => void land('msg', { msg: v }), 250);
  });

  // ---------- the terminal ----------
  function toggleTerm(on = !S.term) {
    if (on && !sess()) return;
    S.term = on; layout();
    if (on) requestAnimationFrame(() => terminal.focus()); else if (tm.contains(document.activeElement)) ta.focus({ preventScroll: true });
  }

  // ---------- keys ----------
  // Registered before the Long Exposure's own, so ← leaves a preview before it can open the sky, and ⌃` reaches the
  // pane even from inside the shell.
  addEventListener('keydown', e => {
    if (e.isComposing) return;
    const t = e.target as HTMLElement, consume = () => { e.preventDefault(); e.stopImmediatePropagation(); };
    if (e.ctrlKey && !e.metaKey && !e.altKey && (e.code === 'Backquote' || e.key === '`')) { consume(); if (!e.repeat) toggleTerm(); return; }
    if (hooks.busy() || !S.ref || e.key !== 'ArrowLeft' || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
    const free = t === ta ? ta.disabled || !ta.value.trim() : !t.matches('input,textarea,select,[contenteditable="true"],webview') && !t.closest('.tm');
    if (free) { consume(); void close(); }
  }, true);

  const api = {
    layout,
    // A different session, or no session: postures start over at once; the panel stays only for a line under way.
    switched() {
      if (S.ref) { S.ref = null; ++loadTok; view.replaceChildren(); showNote(false); fadeConv(1); }
      S.pos = 'chat'; S.busy = false;
      const s = sess(); S.left = !!s && active(s.land); S.wide = false;
      if (s) S.was.set(s.id, s.land?.s ?? '');
      renderLand(); layout(true); requestAnimationFrame(reveal);
    },
    saw,
    // The hint in an empty composer says what ← does now.
    hint() {
      const empty = !ta.value, back = hooks.busy() ? '' : S.pos === 'stage' ? '退出舞台' : S.pos === 'side' && S.ref ? '关掉旁边' : hooks.b01() ? '长曝光' : '';
      return empty ? `${back ? `<span><kbd>←</kbd>${back}</span>` : ''}${chat.classList.contains('nar') || !sess() ? '' : '<span class="hk2"><kbd>⌃</kbd><kbd>`</kbd>终端</span>'}` : '';
    },
    // Esc: out of the stage first, then stop a landing that is running.
    esc() {
      if (S.ref && S.pos === 'stage') { void close(); return true; }
      if (sess()?.land?.s === 'run') { void land('stop'); return true; }
      return false;
    },
    back() { if (!S.ref) return false; void close(); return true; },
    land: startLanding,
    get narrow() { return chat.classList.contains('nar'); },
    // The page's click handler hands over what is the workbench's.
    act(a: string, el: HTMLElement) {
      if (a === 'peek') void open(el);
      else if (a === 'pvclose') void close();
      else if (a === 'pvflip') void flip();
      else if (a === 'pvext') { const r = S.ref; if (r?.href) void window.agents?.openUrl?.(r.href); else if (r?.abs) void window.agents?.openPath?.(r.abs); }
      else if (a === 'land') startLanding();
      else if (a === 'landpanel') openPanel();
      else if (a === 'lpclose') { S.left = false; layout(); }
      else if (a === 'lpwide') { S.wide = !S.wide; layout(); }
      else if (a === 'lpstop') void land('stop');
      else if (a === 'lpresume') void land('resume');
      else if (a === 'lpallow') { hooks.cue('send', .8); void land('allow'); }
      else if (a === 'lpdeny') { hooks.cue('close', .7); void land('deny'); }
      else if (a === 'lpfix') { hooks.cue('send', .7); void land('fix'); }
      else if (a === 'lpstay') { hooks.cue('close', .6); void land('stay'); }
      else if (a === 'lpsvc') { terminal.tab('svc'); toggleTerm(true); }
      else if (a === 'older') { chat.classList.add('all'); const c = conv(); if (c) c.scrollTop = 0; }
      else return false;
      return true;
    },
    terminal: (on?: boolean) => toggleTerm(on),
    show,
    // What the sheet shows now (a file's path, a page's address or a feature's key), or ''.
    shown: () => S.ref?.key ?? '',
    close: () => close(),
    get pos() { return S.pos; },
  };
  // Scrolling up past the top of the narrow column brings the earlier turns back.
  chat.addEventListener('wheel', e => {
    const c = conv();
    if (!c || !chat.classList.contains('nar') || chat.classList.contains('all') || e.deltaY >= 0 || c.scrollTop > 0) return;
    const h = c.scrollHeight; chat.classList.add('all'); c.scrollTop = c.scrollHeight - h;
  }, { passive: true });
  layout(true); flipIcon();
  return api;
}

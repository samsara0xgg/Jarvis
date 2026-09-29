import type { Item, Req, Sess } from '../../../electron/agents/types';
import { COL, glyph, rgba, type St } from './glyph';
import { clamp, dpr, easeInOut, esc, hash, lerp, reduced, smooth, spring, step } from './motion';
import { Her } from './her';
import { ago, drawSky, geometry } from './sky';
import { started, timeline, type Trail, type Turn } from './timeline';

type Hooks = {
  sessions(): Sess[]; items(id: string): Item[] | undefined; current(): string; chat(): boolean;
  load(id: string): Promise<void>; open(id: string): void;
  call(route: string, body?: unknown): Promise<unknown>;
  md(text: string): string; toast(text: string): void; cue(name: string, gain?: number): void;
  // blip: the short tick of a step along the sky, pitched by p · changed: when a session last changed state (performance.now)
  blip(p: number): void; changed(id: string): number;
  refresh(): void;
};
type Decision = { id: string; key: string; kind: 'err' | 'ask' | 'allow' | 'land'; at: number; req?: Req };
const kbd = (key: string) => `<kbd>${key}</kbd>`;
const words: Record<St, string> = { work: '在干活', pack: '在整理', wait: '等你', done: '做完了', read: '看过了', err: '停了' };
const status = (s: Sess): St => s.st === 'done' && !s.unread ? 'read' : s.st;
const $ = <T extends HTMLElement = HTMLElement>(s: string, root: ParentNode) => root.querySelector(s) as T;
const animate = (el: Element, frames: Keyframe[], duration: number, easing = 'cubic-bezier(.2,.8,.2,1)', more: KeyframeAnimationOptions = {}) => reduced.matches ? null : el.animate(frames, { duration, easing, ...more });
const LIVELY = 'linear(0,.045,.153,.29,.433,.568,.687,.786,.864,.924,.967,.996,1.014,1.024,1.028,1.028,1.026,1.022,1.018,1.014,1.011,1.008,1.005,1.003,1)';
const rank = { err: 0, ask: 1, allow: 1, land: 2 };
// The label in front of a reply that is not its last words: still working, waiting on you, stopped.
const LEAD: Partial<Record<Turn['kind'], string>> = { live: '还在做', wait: '等你', err: '停了' };

export function mountExposure(win: HTMLElement, ta: HTMLTextAreaElement, hooks: Hooks) {
  const chrome = document.createElement('div'); chrome.className = 'exposure';
  chrome.innerHTML = `<div class="bw-hz"></div><canvas class="bw-cv" aria-hidden="true"></canvas>
    <div class="bw-stars" aria-label="会话地平线"></div>
    <button type="button" class="bw-her" aria-label="Jarvis：把手里的事过一遍"><canvas></canvas></button>
    <button type="button" class="bw-pull" aria-expanded="false"></button>
    <div class="bw-sky" role="region" aria-label="长曝光时间线" inert>
      <div class="bw-rows"></div><div class="bw-axis"></div><div class="bw-gap" hidden></div><div class="bw-open" hidden><div class="pp-in"></div></div>
      <button type="button" class="bw-rest" hidden></button>
    </div>
    <div class="bw-offer" hidden role="status"></div>
    <div class="bw-deck" hidden><section class="dk-card" role="dialog" aria-modal="true" aria-label="过一遍" tabindex="-1"></section></div>
    <div class="bw-peek" hidden></div>`;
  win.append(chrome);
  const cv = $<HTMLCanvasElement>('.bw-cv', chrome), c = cv.getContext('2d')!;
  const skyEl = $('.bw-sky', chrome), rowsEl = $('.bw-rows', chrome), starsEl = $('.bw-stars', chrome), axis = $('.bw-axis', chrome);
  const pop = $('.bw-open', chrome), inner = $('.pp-in', pop), gap = $('.bw-gap', chrome), pull = $('.bw-pull', chrome);
  const offerEl = $('.bw-offer', chrome), deckEl = $('.bw-deck', chrome), card = $('.dk-card', chrome), peek = $('.bw-peek', chrome), rest = $('.bw-rest', chrome);
  const head = $('.m-head', win), main = $('.main', win);
  const her = new Her($<HTMLCanvasElement>('.bw-her canvas', chrome), 56, 15.6, true);
  const modeButton = document.createElement('button'); modeButton.type = 'button'; modeButton.className = 'ex-mode';
  $('.w-bar', win).append(modeButton);
  const held = document.createElement('button'); held.type = 'button'; held.className = 'ex-held'; held.setAttribute('aria-label', '过一遍待处理事项');
  $('.c-tools', win).append(held);
  let enabled = localStorage.getItem('agents.layout') !== 'classic', skyOn = false, nameStop = false, selected = '', qi = -1, time: number | null = null;
  let width = win.clientWidth, height = win.clientHeight, now = Date.now() / 60000, expandRest = false, rowKey = '', popKey = '', deckKey = '', busy = false;
  let openAt = performance.now(), typedAt = -Infinity, scrolledAt = -Infinity, lastCurrent = hooks.current(), lastRefresh = 0;
  let deck: Decision[] = [], deckOn = false, bookmark: { el: HTMLElement; scroll: number; focus: HTMLElement | null; row: HTMLElement | null } | null = null;
  const trails: Record<string, Trail> = {}, sources = new Map<string, { s: string; items?: Item[] }>();
  let all: Sess[] = [], rows: Sess[] = [], resting: Sess[] = [];
  const seenErrors = new Set<string>(), loading = new Set<string>();
  let presenceKey = '', axisKey = '', revealUntil = 0;
  // hoverQi: the point of the selected row under the pointer, read in place of the one you stand on · hoverStar: the
  // horizon star under it · glance: the star she turns to when a session changes, until when
  let hoverQi: number | null = null, hoverStar = '', glance = { id: '', until: 0 };
  const sky = spring(0), opening = spring(0), left = spring(0), top = spring(0), needle = spring(0), needleY = spring(0), focus = spring(0);
  const offsets = new Map<string, ReturnType<typeof spring>>();
  let wordWidth = 0, wordHeight = 0, targetLeft = 0, targetTop = 0, nearNow = false, snap = true;
  const get = (id: string) => hooks.sessions().find(s => s.id === id);
  const point = () => trails[selected]?.turns[qi];
  const index = () => Math.max(0, rows.findIndex(s => s.id === selected));
  const since = (s: Sess) => s.trace?.at(-1)?.at ?? hooks.items(s.id)?.find(it => it.k === 'req' && !it.done)?.at ?? s.updated;
  const waitMin = (s: Sess) => Math.max(0, Math.round((Date.now() - since(s)) / 60000));
  const stateText = (s: Sess) => `${words[status(s)]}${s.st === 'wait' ? ` · ${waitMin(s) || '刚刚'}${waitMin(s) ? ' 分' : ''}` : ''}`;
  const stateHTML = (s: Sess, tag = 'em') => `<${tag} class="st-${status(s)}">${stateText(s)}</${tag}>`;
  const skyHeight = () => 26 + rows.length * 27 + opening.value + 34 + (resting.length ? 28 : 0);
  const geo = () => geometry(width, now, Math.max(60, ...rows.map(s => now - (trails[s.id]?.segs[0]?.a ?? now))) * 1.04, skyHeight(),
    (i, x) => (offsets.get(rows[i]?.id)?.value ?? 0) * smooth(left.value - 62, left.value - 18, x) * (1 - smooth(left.value + wordWidth + 14, left.value + wordWidth + 58, x)));
  const hz = (i: number, n: number) => width - 84 - (n - 1 - i) * Math.min(24, Math.max(8, (width - 580) / Math.max(1, n - 1)));
  const starPosition = (i: number): [number, number] => {
    const p = easeInOut(clamp(sky.value * 1.3 - i * .03));
    return [lerp(hz(i, rows.length), width - 262, p), lerp(27, 82 + i * 27, p)];
  };
  async function ensure(id: string) {
    if (hooks.items(id) || loading.has(id)) return;
    loading.add(id); try { await hooks.load(id); } finally {
      loading.delete(id); refresh();
      if (skyOn && selected === id && qi < 0) { latest(true); renderWords(); revealUntil = performance.now() + 800; }
    }
  }
  function holding(): Decision[] {
    return all.flatMap((s): Decision[] => {
      if (s.term || s.parked) return [];
      const it = hooks.items(s.id)?.find(it => it.k === 'req' && !it.done);
      const key = `${s.id}:${s.st}:${since(s)}`;
      if (s.st === 'err' && !seenErrors.has(key)) return [{ id: s.id, key, kind: 'err', at: since(s) }];
      if (it?.k === 'req') return [{ id: s.id, key: it.req.id, kind: it.req.tool === 'Ask' ? 'ask' : 'allow', at: it.at ?? since(s), req: it.req }];
      if (s.st === 'done' && s.unread) return [{ id: s.id, key, kind: 'land', at: s.updated }];
      return [];
    }).sort((a, b) => rank[a.kind] - rank[b.kind] || a.at - b.at);
  }
  function setMode(on: boolean) {
    closeSky(); closeDeck('go'); enabled = on;
    localStorage.setItem('agents.layout', on ? 'exposure' : 'classic');
    if (on) win.append(head); else main.prepend(head);
    win.classList.toggle('bw', on); win.classList.toggle('m-top', on); win.classList.toggle('oc-over', on);
    chrome.hidden = held.hidden = !on;
    modeButton.textContent = on ? '原模式' : '长曝光';
    modeButton.setAttribute('aria-label', on ? '切换到原模式' : '切换到长曝光');
    if (on) { for (const s of all) void ensure(s.id); }
    hooks.refresh(); refresh();
  }
  function refresh() {
    all = hooks.sessions().filter(s => !s.archived);
    for (const s of all) {
      const items = hooks.items(s.id), key = `${s.st}|${s.updated}|${s.summary}|${s.now}|${s.trace?.length}`;
      if (sources.get(s.id)?.items !== items || sources.get(s.id)?.s !== key) {
        trails[s.id] = timeline(s, items ?? []); sources.set(s.id, { s: key, items });
      }
    }
    // Pinned first, then by when each began: a row never moves because its state changed.
    all.sort((a, b) => Number(b.pinned) - Number(a.pinned) || started(a, trails[a.id]) - started(b, trails[b.id]) || a.id.localeCompare(b.id));
    const fold = all.length > 14 && !expandRest;
    resting = fold ? all.filter(s => s.st === 'done' && !s.unread && Date.now() - s.updated > 3600000 && s.id !== hooks.current() && s.id !== selected) : [];
    rows = all.filter(s => !resting.includes(s));
    if (!rows.some(s => s.id === selected)) { selected = rows.find(s => s.id === hooks.current())?.id ?? rows[0]?.id ?? ''; latest(); }
    if (lastCurrent !== hooks.current()) { lastCurrent = hooks.current(); openAt = performance.now(); hideOffer(); }
    const h = holding();
    held.innerHTML = h.length ? `<i></i>她手里 ${h.length} 件 ${kbd('空格')}` : '';
    held.hidden = !enabled || !h.length;
    if (!h.length) hideOffer();
    if (enabled) for (const s of all) if (s.st === 'wait') void ensure(s.id);
    if (deckOn && !busy) {
      const available = new Set(h.map(d => d.key));
      deck = deck.filter(d => available.has(d.key));
      if (!deck.length) closeDeck('gone'); else renderDeck();
    }
    const ids = all.map(s => s.id), presence = JSON.stringify([enabled, ids]);
    if (presence !== presenceKey) { presenceKey = presence; window.agents?.presence?.(enabled, ids); }
    renderRows(); if (skyOn) renderWords();
  }
  function renderRows() {
    const key = rows.map(s => `${s.id}|${s.title}|${status(s)}|${waitMin(s)}`).join(';') + `|${selected}|${skyOn}|${nameStop}|${hooks.current()}`;
    if (rowKey === key) return; rowKey = key;
    rowsEl.innerHTML = rows.map((s, i) => `<button type="button" class="bw-row${selected === s.id ? ' sel' : ''}${hooks.current() === s.id ? ' on' : ''}${nameStop && selected === s.id ? ' nm' : ''}" data-session="${esc(s.id)}" style="top:${15 + i * 27}px" aria-label="${esc(s.title)}，${words[status(s)]}"><b>${esc(s.title)}</b>${stateHTML(s)}</button>`).join('');
    starsEl.innerHTML = rows.map((s, i) => `<button type="button" data-session="${esc(s.id)}" aria-label="${esc(s.title)}，${words[status(s)]}" style="left:${hz(i, rows.length) - 10}px" title="${esc(s.title)} · ${esc(s.summary)}"></button>`).join('');
    rest.hidden = !resting.length; rest.textContent = `还有 ${resting.length} 个在歇着`; rest.style.top = `${26 + rows.length * 27}px`;
  }
  function latest(keep = false) {
    const turns = trails[selected]?.turns ?? [];
    qi = turns.length - 1;
    if (keep && time !== null && turns.some(t => t.at !== undefined)) {
      qi = turns.reduce((best, t, i) => t.at !== undefined && Math.abs(t.at - time!) < Math.abs((turns[best]?.at ?? Infinity) - time!) ? i : best, turns.findIndex(t => t.at !== undefined));
    } else time = turns[qi]?.at ?? null;
  }
  function lastParagraph(text: string) {
    const template = document.createElement('template'); template.innerHTML = hooks.md(text);
    const ps = template.content.querySelectorAll('p');
    return ps.length ? ps[ps.length - 1].textContent ?? '' : template.content.textContent ?? text;
  }
  const keys = (...ks: string[]) => `<p class="pp-k">${ks.filter(Boolean).map(k => `<span>${k}</span>`).join('')}</p>`;
  // A point you said something at: when, your words, the last thing it said in that turn, the next key.
  function said(s: Sess, t: Turn, at: number, n: number) {
    const clock = t.at === undefined ? '时间未记录' : new Date(t.at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
    return `<p class="pp-h"><span class="pp-t"><b>${clock}</b><small>第 ${at + 1} / ${n} 句</small></span></p><p class="pp-q">${esc(t.you)}</p>`
      + `<p class="pp-a${t.kind === 'sum' ? '' : ` r-${t.kind}`}"><i>${s.agent === 'claude' ? 'Claude' : 'Codex'}</i>${LEAD[t.kind] ? `<em>${LEAD[t.kind]}</em>` : ''}${esc(t.kind === 'sum' ? lastParagraph(t.reply) : t.reply)}</p>`
      + keys(at > 0 ? `${kbd('←')} 上一句` : '', `${kbd('→')} ${at < n - 1 ? '下一句' : '到名字'}`);
  }
  // The name stop, or a session you have not said anything to: where it stands now.
  function standing(s: Sess, n: number) {
    return `<p class="pp-h"><span class="pp-t"><b>现在</b>${stateHTML(s, 'small')}</span></p><p class="pp-her"><i>她</i>${nameStop || n ? '' : '你还没跟它说过话。'}${esc(s.now || s.summary || '')}</p>`
      + (nameStop ? keys(n ? `${kbd('←')} 回到你说的` : '', `${kbd('→')} 进去`) : keys(`${kbd('→')} 到名字`));
  }
  function renderWords() {
    const s = get(selected); if (!s || !skyOn) return;
    // A point under the pointer is read in place of the one you stand on, without moving the needle.
    const turns = trails[selected]?.turns ?? [], at = hoverQi ?? (nameStop ? -1 : qi), t = turns[at];
    const key = `${selected}|${at}|${nameStop}|${t?.kind}|${t?.reply}|${t?.you}|${stateText(s)}|${s.now}|${s.summary}|${turns.length}`;
    const fresh = pop.hidden; pop.hidden = gap.hidden = false;
    if (key !== popKey) {
      popKey = key;
      inner.innerHTML = t ? said(s, t, at, turns.length) : standing(s, turns.length);
      if (fresh) animate(inner, [{ opacity: 0 }, { opacity: 1 }], 260, undefined, { delay: 90, fill: 'backwards' });
      else animate(inner, [{ opacity: .35 }, { opacity: 1 }], 180);
    }
    // The last 110 px before now stay straight; the words never invade the names.
    pop.style.maxWidth = `${Math.min(480, Math.max(210, width - 420))}px`;
    wordWidth = pop.offsetWidth; wordHeight = pop.offsetHeight + 18;
    const x = t?.at === undefined ? width - 262 : geo().xOf(t.at / 60000);
    targetLeft = clamp(x - 10, 24, width - 372 - wordWidth); targetTop = 48 + index() * 27;
    nearNow = t?.at === undefined || x > targetLeft + wordWidth - 12;
    pop.classList.toggle('sc', nearNow);
    if (fresh || reduced.matches) { left.value = targetLeft; left.velocity = 0; top.value = targetTop; top.velocity = 0; }
  }
  function openSky() {
    if (!enabled || !rows.length || deckOn) return;
    peek.hidden = true;
    // It opens on the names: ↑↓ pick a session, → goes in; ← steps back into what you said.
    selected = rows.find(s => s.id === hooks.current())?.id ?? rows[0].id; nameStop = true; latest();
    skyOn = true; snap = true; hoverQi = null; revealUntil = performance.now() + 800; hideOffer();
    win.classList.add('sky-on'); skyEl.inert = false; pullLabel();
    win.tabIndex = -1; win.focus({ preventScroll: true }); renderRows(); renderWords();
    for (const s of all) void ensure(s.id);
  }
  function closeSky() {
    skyOn = false; pop.hidden = gap.hidden = true; wordHeight = 0; popKey = ''; nameStop = false; hoverQi = null;
    win.classList.remove('sky-on'); skyEl.inert = true; skyEl.style.cursor = ''; pullLabel(); renderRows();
  }
  // The horizon is the window's drag strip, so the way in stays on it as the way out: esc, 收起.
  function pullLabel() {
    pull.innerHTML = skyOn ? `${kbd('esc')}<span>收起</span>` : `${kbd('←')}<span>长曝光</span>`;
    pull.setAttribute('aria-expanded', String(skyOn)); pull.setAttribute('aria-label', skyOn ? '收起长曝光' : '全部会话');
  }
  // Back to where you were writing; while a request holds the composer shut, the window keeps the keys.
  function settle() {
    if (ta.disabled) { win.tabIndex = -1; win.focus({ preventScroll: true }); } else ta.focus({ preventScroll: true });
  }
  function revealSelection() {
    const y = 15 + index() * 27, bottom = top.value + wordHeight - 18, viewport = skyEl.clientHeight;
    if (!viewport) return;
    const scroll = skyEl.scrollTop;
    if (bottom - y > viewport - 24 || y < scroll + 12) skyEl.scrollTop = Math.max(0, y - 12);
    else if (bottom > scroll + viewport - 12) skyEl.scrollTop = Math.max(0, bottom - viewport + 12);
  }
  function hideOffer() { offerEl.hidden = true; if (!deckOn) her.surface(false); }
  function offer(reason: string) {
    const h = holding(), conv = $('.host .conv', win);
    if (!enabled || !h.length || deckOn || skyOn || !document.hasFocus() || ta.value.trim() || performance.now() - typedAt < 1000 || performance.now() - openAt < 5000 || performance.now() - scrolledAt < 1500 || (conv && conv.scrollTop < conv.scrollHeight - conv.clientHeight - 40)) return;
    offerEl.innerHTML = `<p><b>${h.length} 件事等你</b><span>${reason}</span></p><button type="button" class="btn warm">过一遍 ${kbd('空格')}</button>`;
    // She surfaces first; her words follow a beat later.
    if (offerEl.hidden) animate(offerEl, [{ opacity: 0, transform: 'translateY(-6px) scale(.96)' }, { opacity: 1, transform: 'none' }], 380, LIVELY, { delay: 160, fill: 'backwards' });
    offerEl.hidden = false; her.surface(true); her.say('ask', 2400);
  }
  function remember() {
    const el = $('.host .conv', win); if (!el) return;
    const bounds = el.getBoundingClientRect();
    const row = [...el.querySelectorAll<HTMLElement>('.c-items > *')].find(r => r.getBoundingClientRect().bottom >= bounds.top + 28) ?? null;
    bookmark = { el, scroll: el.scrollTop, focus: document.activeElement as HTMLElement | null, row };
  }
  function openDeck() {
    if (!enabled || busy || deckOn) return;
    deck = holding();
    if (!deck.length) { hooks.toast('她手里没有事'); return; }
    closeSky(); hideOffer(); remember(); deckOn = true; deckEl.hidden = false; peek.hidden = true;
    main.inert = head.inert = true; her.surface(true); her.say('35', 60000); hooks.cue('open', .7);
    animate(deckEl, [{ opacity: 0 }, { opacity: 1 }], 240);
    renderDeck(true); card.focus({ preventScroll: true });
  }
  // done: every card answered · put: the rest put down, still in her hands · go: left to read one in full · gone: the
  // cards settled elsewhere, nothing to celebrate
  function closeDeck(how: 'done' | 'put' | 'go' | 'gone') {
    if (!deckOn) return;
    deckOn = false; main.inert = head.inert = false; her.surface(false); her.faceUntil = 0; deckKey = ''; deck = [];
    const fade = animate(deckEl, [{ opacity: 1 }, { opacity: 0 }], 200);
    if (fade) fade.onfinish = () => { if (!deckOn) deckEl.hidden = true; }; else deckEl.hidden = true;
    if (how === 'done') { her.say('fin', 1800); her.hop(.12); hooks.cue('done', .6); hooks.toast('都处理完了 · 回到你刚才那一行'); }
    else if (how === 'put') hooks.toast('先放下了 · 都还在她手里');
    if (bookmark) {
      bookmark.el.scrollTop = bookmark.scroll;
      if (how !== 'go' && bookmark.row?.isConnected) animate(bookmark.row, [
        { boxShadow: '0 0 0 1px rgba(255,201,143,.6), 0 0 24px -4px rgba(255,201,143,.5)', background: 'rgba(255,201,143,.07)' },
        { boxShadow: '0 0 0 1px rgba(255,201,143,0), 0 0 24px -4px rgba(255,201,143,0)', background: 'rgba(255,201,143,0)' }], 1600, 'ease-out');
      if (bookmark.focus?.isConnected && !bookmark.focus.matches(':disabled') && bookmark.focus !== document.body) bookmark.focus.focus({ preventScroll: true }); else settle();
    }
    bookmark = null;
  }
  // What its last turn changed, from the edit steps since the last thing you said.
  function changes(id: string) {
    const items = hooks.items(id) ?? [], from = items.map(it => it.k).lastIndexOf('you');
    const edits = items.slice(from + 1).flatMap(it => it.k === 'steps' ? it.steps : []).filter(step => step.k === 'edit');
    const add = edits.reduce((n, step) => n + (step.add ?? 0), 0), del = edits.reduce((n, step) => n + (step.del ?? 0), 0);
    return edits.length ? ` · 改了 ${edits.length} 个 <i class="p">+${add}</i> <i class="m">−${del}</i>` : '';
  }
  function renderDeck(deal = false) {
    const d = deck[0], s = d && get(d.id); if (!d || !s) return;
    if (deckKey === d.key) return; deckKey = d.key;
    const ownedFocus = card.contains(document.activeElement);
    const footer = `<div class="dk-k"><button type="button" data-deck="later">${kbd('S')} 稍后</button><button type="button" data-deck="open">${kbd('⌘')}${kbd('⏎')} 去看全文</button><button type="button" data-deck="close">${kbd('esc')} 先放下</button></div>`;
    let body = '';
    const actions = `<div class="dk-row"><button type="button" class="btn" data-deck="deny">拒绝 ${kbd('N')}</button><button type="button" class="btn warm" data-deck="allow">允许 ${kbd('⏎')}</button></div>`;
    if (d.kind === 'err') body = `<p class="dk-t">停了 · ${esc(s.summary)}</p><div class="dk-row"><button type="button" class="btn" data-deck="open">去看看</button><button type="button" class="btn warm" data-deck="resume">让它接着来 ${kbd('⏎')}</button></div>`;
    else if (d.kind === 'land') body = `<p class="dk-t">做完了${changes(s.id)}</p><div class="dk-summary">${hooks.md(s.summary)}</div><p class="dk-her">收尾照你的顺序：提交 → 合进 main → 重启用到的 → 推送。推送那步再等你点头。</p><div class="dk-row"><button type="button" class="btn" data-deck="open">先看看</button><button type="button" class="btn warm" data-deck="land">收尾 ${kbd('⏎')}</button></div>`;
    else if (d.req) {
      const r = d.req;
      if (r.tool === 'Ask') body = r.qs.map((q, i) => `<fieldset><legend class="dk-t">${esc(q.q)}</legend><div class="dk-opts">${q.opts.map(([label, desc], j) => `<label><input type="${q.multi ? 'checkbox' : 'radio'}" name="question-${i}" value="${esc(label)}"><span><b>${kbd(String(j + 1))} ${esc(label)}</b><small>${esc(desc)}</small></span></label>`).join('')}</div></fieldset>`).join('') + `<textarea class="dk-text" aria-label="补充回答" placeholder="也可以直接写你的回答"></textarea><div class="dk-row"><button type="button" class="btn" data-deck="deny">不回答 ${kbd('N')}</button><button type="button" class="btn warm" data-deck="allow">回答 ${kbd('⏎')}</button></div>`;
      else if (r.tool === 'Plan') body = `<p class="dk-t">计划写好了</p><div class="dk-summary">${hooks.md(r.plan)}</div><textarea class="dk-text" aria-label="修改意见" placeholder="需要调整的地方"></textarea>${actions}`;
      else {
        const detail = r.tool === 'Bash' ? `<span>${esc(r.cwd)} $</span> ${esc(r.cmd)}` : r.tool === 'Edit' ? `${esc(r.file)}\n${r.diff.map(([sign, text]) => esc(sign + text)).join('\n')}` : `${esc(r.name)}\n${esc(r.detail)}`;
        body = `<p class="dk-t">${esc(r.why || '这一步需要你批准')}</p><pre class="dk-cmd">${detail}</pre>${r.always ? `<label class="dk-always"><input type="checkbox" name="always">${esc(r.always)}</label>` : ''}${actions}`;
      }
    }
    // Who, how long it has waited, and one dot per card still in the stack, this one lit.
    const m = Math.max(0, Math.round((Date.now() - d.at) / 60000)), waited = d.kind === 'land' ? m ? `${m} 分前做完` : '刚做完' : m ? `等了 ${m} 分` : '刚刚';
    card.innerHTML = `<p class="dk-h"><span class="dk-who st-${status(s)}"><i></i>${esc(s.title)}<em>${waited}</em></span><span class="dk-dots" aria-label="还有 ${deck.length} 件">${deck.map((_, i) => `<i${i ? '' : ' class="on"'}></i>`).join('')}</span></p>${body}${footer}<p class="dk-error" role="alert" hidden></p>`;
    if (ownedFocus) card.focus({ preventScroll: true });
    if (deal) {
      const bounds = card.getBoundingClientRect(), frame = win.getBoundingClientRect();
      animate(card, [{ opacity: 0, transform: `translate(${width - 34 - (bounds.left - frame.left + bounds.width / 2)}px,${47 - (bounds.top - frame.top + bounds.height / 2)}px) scale(.12)` }, { opacity: 1, transform: 'none' }], 460, LIVELY);
    }
  }
  async function decide(action: string) {
    if (busy || !deckOn) return;
    const d = deck[0], s = d && get(d.id); if (!d || !s) return;
    if (action === 'close') { closeDeck('put'); return; }
    if (action === 'open') { closeDeck('go'); hooks.open(s.id); return; }
    if (action === 'later') {
      if (deck.length === 1) { closeDeck('put'); return; }
      hooks.cue('mic', .45);
      deck.push(deck.shift()!); deckKey = ''; renderDeck(true); card.focus({ preventScroll: true });
      return;
    }
    const error = $('.dk-error', card);
    let body: Record<string, unknown>, route: string;
    if (d.req) {
      const text = $<HTMLTextAreaElement>('.dk-text', card)?.value.trim() || '';
      const answers = d.req.tool === 'Ask' ? d.req.qs.map((_, i) => [...card.querySelectorAll<HTMLInputElement>(`input[name="question-${i}"]:checked`)].map(el => el.value)) : undefined;
      if (action === 'allow' && answers && !text && answers.some(a => !a.length)) { error.textContent = '把每个问题选好，或写下你的回答'; error.hidden = false; return; }
      body = { req: d.req.id, decision: action === 'deny' ? 'deny' : $<HTMLInputElement>('input[name="always"]', card)?.checked ? 'always' : 'allow', answers, ...(text ? { text } : {}) };
      route = `/sessions/${s.id}/answer`;
    } else {
      body = { text: action === 'resume' ? '请从刚才出错的地方接着来，先确认当前状态。' : '请按项目约定收尾：完成验证、提交、合入 main、重启用到的服务；推送前等我确认。', files: [] };
      route = `/sessions/${s.id}/send`;
    }
    busy = true; card.setAttribute('aria-busy', 'true');
    card.querySelectorAll<HTMLButtonElement | HTMLInputElement>('button,input').forEach(b => b.disabled = true);
    try {
      // Do not remove a card or change its star until the real host accepts the answer.
      await hooks.call(route, body);
      if (d.kind === 'err') seenErrors.add(d.key);
      hooks.cue(action === 'deny' ? 'close' : 'send', .85);
      const i = rows.findIndex(r => r.id === s.id), target = i < 0 ? [width - 34, 27] : starPosition(i);
      const bounds = card.getBoundingClientRect(), frame = win.getBoundingClientRect();
      const animation = animate(card, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: `translate(${target[0] - (bounds.left - frame.left + bounds.width / 2)}px,${target[1] - (bounds.top - frame.top + bounds.height / 2)}px) scale(.1)` }], 340, 'cubic-bezier(.4,0,.6,1)');
      if (animation) await animation.finished.catch(() => {});
      deck.shift(); deckKey = ''; busy = false;
      if (!deck.length) closeDeck('done'); else { renderDeck(true); card.focus({ preventScroll: true }); }
    } catch (e) {
      error.textContent = e instanceof Error ? e.message : String(e); error.hidden = false; busy = false;
      card.querySelectorAll<HTMLButtonElement | HTMLInputElement>('button,input').forEach(b => b.disabled = false);
    } finally { card.removeAttribute('aria-busy'); }
  }
  // ⌥⇥: straight to the next session waiting on you, without the stack.
  function next() {
    if (!enabled || deckOn) return;
    const d = holding().find(d => d.id !== hooks.current());
    if (d) hooks.open(d.id); else hooks.toast('没有别的等你的了');
  }
  function keyboard(e: KeyboardEvent) {
    if (!enabled || e.isComposing) return;
    const target = e.target as HTMLElement, editable = target.matches('input,textarea,select,[contenteditable="true"]');
    const consume = () => { e.preventDefault(); e.stopImmediatePropagation(); };
    if (deckOn) {
      if (e.key === 'Escape') { consume(); if (!busy) closeDeck('put'); return; }
      // The stack is modal: the desk's session keys wait until it is put down.
      if (e.altKey && e.key.startsWith('Arrow')) { consume(); return; }
      if (e.key === 'Tab') {
        const buttons = [...card.querySelectorAll<HTMLElement>('button:not(:disabled),input,textarea')];
        if (buttons.length && (e.shiftKey ? target === buttons[0] || target === card : target === buttons.at(-1))) { consume(); (e.shiftKey ? buttons.at(-1)! : buttons[0]).focus(); }
        return;
      }
      if (editable && !(e.key === 'Enter' && (e.metaKey || e.ctrlKey))) return;
      const d = deck[0];
      if (e.key === 'Enter' || /^[1-9nNsS]$/.test(e.key)) {
        consume(); if (e.repeat || busy) return;
        if (e.key === 'Enter') void decide(e.metaKey || e.ctrlKey ? 'open' : (target.closest<HTMLElement>('[data-deck]')?.dataset.deck ?? (d.kind === 'err' ? 'resume' : d.kind === 'land' ? 'land' : 'allow')));
        else if (/^[1-9]$/.test(e.key) && d.req?.tool === 'Ask' && d.req.qs.length === 1) {
          const opt = card.querySelectorAll<HTMLInputElement>('input[name="question-0"]')[Number(e.key) - 1];
          if (opt) { opt.checked = opt.type === 'radio' || !opt.checked; if (!d.req.qs[0].multi) void decide('allow'); }
        } else if (e.key.toLowerCase() === 'n') void decide(d.req ? 'deny' : 'later');
        else if (e.key.toLowerCase() === 's') void decide('later');
      }
      return;
    }
    if (e.altKey && e.key === 'Tab') { consume(); if (!e.repeat) next(); return; }
    if (skyOn) {
      if (editable && target !== ta) { closeSky(); return; }
      if (e.altKey && e.key === 'ArrowDown') { consume(); closeSky(); settle(); return; }
      if (e.key.startsWith('Arrow') || e.key === 'Escape' || e.key === 'Enter') {
        consume(); hoverQi = null;
        const turns = trails[selected]?.turns ?? [], was = qi, row = index();
        if (e.key === 'Escape' || (e.key === 'ArrowDown' && row === rows.length - 1)) { closeSky(); settle(); return; }
        if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
          // A new row keeps the moment: it lands on its own sentence nearest to where the needle stood.
          selected = rows[clamp(row + (e.key === 'ArrowDown' ? 1 : -1), 0, rows.length - 1)].id; latest(true);
          if (index() !== row) hooks.blip(.82);
        } else if (e.key === 'ArrowLeft') {
          if (!turns.length) { hooks.toast('你还没跟它说过话'); return; }
          if (nameStop) { nameStop = false; qi = turns.length - 1; } else qi = qi < 0 ? turns.length - 1 : Math.max(0, qi - 1);
          time = point()?.at ?? time;
          if (qi !== was) hooks.blip(1);
        } else if (e.key === 'ArrowRight' || e.key === 'Enter') {
          if (nameStop || e.key === 'Enter') { const id = selected; closeSky(); hooks.open(id); settle(); return; }
          // Past the last thing you said, the name is one stop before going in.
          if (qi >= turns.length - 1) { nameStop = true; hooks.cue('mic', .35); } else { qi++; time = point()?.at ?? time; hooks.blip(1); }
        }
        renderRows(); renderWords(); revealUntil = performance.now() + 800; return;
      }
      if (!e.metaKey && !e.ctrlKey && !e.altKey && (e.key.length === 1 || e.key === 'Process')) {
        closeSky(); settle();
        if (e.key.length === 1) { consume(); if (!ta.disabled) { ta.setRangeText(e.key, ta.selectionStart, ta.selectionEnd, 'end'); ta.dispatchEvent(new Event('input', { bubbles: true })); } }
      }
      return;
    }
    if (editable && target !== ta) return;
    // An empty composer, or none to write in while a request holds it, is a pause: the keys are hers.
    const free = (target === ta || target === win || target === document.body) && (ta.disabled || !ta.value.trim());
    const plain = !e.metaKey && !e.ctrlKey && !e.altKey;
    if (e.key === 'Escape' && !offerEl.hidden && !win.querySelector('.pop.on')) { consume(); hideOffer(); return; }
    if ((free && plain && !e.shiftKey && e.key === 'ArrowLeft') || (e.altKey && e.key === 'ArrowUp')) { consume(); openSky(); return; }
    if (free && plain && e.key === ' ') { consume(); if (!e.repeat) openDeck(); }
  }
  function frame(t: number, dt: number) {
    if (!enabled) return;
    now = Date.now() / 60000;
    if (t - lastRefresh > 1000) { lastRefresh = t; refresh(); }
    step(sky, skyOn ? 1 : 0, 2.2, .92, dt); const selectedIndex = index();
    step(opening, skyOn ? wordHeight : 0, 2.6, .9, dt); step(left, targetLeft, 2.8, .86, dt); step(top, targetTop, 2.8, .86, dt); step(focus, selectedIndex, 2.4, 1, dt);
    for (const [i, s] of rows.entries()) {
      const value = offsets.get(s.id) ?? spring(0); offsets.set(s.id, value);
      step(value, skyOn && i > selectedIndex ? wordHeight : 0, 2.6, .9, dt);
    }
    // At the name stop the needle's point slides on to now and waits by the name; the needle itself steps aside.
    const g = geo(), turn = point(), at = turn?.at !== undefined ? turn.at / 60000 : undefined, stand = nameStop || at === undefined;
    const nx = stand ? g.x1 : g.xOf(at!), ny = g.y(selectedIndex);
    if (snap || sky.value < .3) { needle.value = nx; needle.velocity = 0; needleY.value = ny; needleY.velocity = 0; snap = false; }
    else { step(needle, nx, 2.8, .82, dt); step(needleY, ny, 2.8, .86, dt); }
    win.style.setProperty('--sky', `${sky.value * skyHeight()}px`); win.style.setProperty('--skp', String(sky.value));
    skyEl.style.height = `${Math.min(height - 56, sky.value * skyHeight())}px`;
    skyEl.style.opacity = String(clamp((sky.value - .35) / .5)); skyEl.style.pointerEvents = skyOn ? 'auto' : 'none';
    const ratio = dpr(), canvasHeight = Math.max(height, skyHeight() + 56);
    if (cv.width !== Math.round(width * ratio) || cv.height !== Math.round(canvasHeight * ratio)) { cv.width = Math.round(width * ratio); cv.height = Math.round(canvasHeight * ratio); cv.style.width = `${width}px`; cv.style.height = `${canvasHeight}px`; }
    c.setTransform(ratio, 0, 0, ratio, 0, 0); c.clearRect(0, 0, width, canvasHeight);
    // Scrolling a tall sky moves its trails and text together; the desk below never scrolls with it.
    c.save();
    if (skyEl.scrollTop > 0) { c.beginPath(); c.rect(0, 56, width, height - 56); c.clip(); }
    c.translate(0, -skyEl.scrollTop);
    drawSky(c, rows, trails, { now, p: sky.value, dev: clamp((sky.value - .18) / .82), geo: g, sel: skyOn ? selectedIndex : undefined, pt: skyOn ? at : undefined,
      span: stand ? undefined : [at!, (trails[selected]?.turns[qi + 1]?.at ?? Date.now()) / 60000], ndx: at === undefined ? undefined : needle.value, nm: nameStop,
      cy: needleY.value, focus: skyOn ? focus.value : undefined,
      conn: skyOn && nearNow && !pop.hidden ? { x: stand ? g.x1 : needle.value, y: needleY.value, x2: left.value + wordWidth - 16, y2: top.value + 58 } : null });
    c.restore();
    // A star pops when its session changes state and swells under the pointer.
    rows.forEach((s, i) => { const [x, y] = starPosition(i), sy = y - (skyOn ? skyEl.scrollTop : 0); if (skyOn && skyEl.scrollTop && sy < 65) return; c.save(); c.translate(x, sy); glyph(c, status(s), { lit: s.id === hooks.current(), t: t / 1000 + hash(s.id) * 9, since: (t - hooks.changed(s.id)) / 1000, hover: !skyOn && hoverStar === s.id, calm: reduced.matches, lift: 1.3 }); c.restore(); });
    const h = holding(), count = Math.min(3, h.length), beadY = 49 + (deckOn ? 2 : 0);
    for (let i = 0; i < count; i++) { const col = COL[h[i].kind === 'err' ? 'err' : h[i].kind === 'land' ? 'done' : 'wait']; c.save(); c.shadowColor = rgba(col, .9); c.shadowBlur = 5; c.fillStyle = rgba(col, 1, .25); c.beginPath(); c.arc(width - 34 + (i - (count - 1) / 2) * 8.5, beadY, 2.4, 0, Math.PI * 2); c.fill(); c.restore(); }
    if (h.length > 3) { c.fillStyle = 'rgba(255,201,143,.9)'; c.font = '600 9px "IBM Plex Mono", ui-monospace, monospace'; c.textBaseline = 'middle'; c.fillText(`+${h.length - 3}`, width - 19, 49.5); }
    // For a moment after a session changes, she looks toward its star.
    const glancing = t < glance.until ? rows.findIndex(r => r.id === glance.id) : -1;
    if (glancing >= 0) { const [x, y] = starPosition(glancing); her.look = [clamp((x - width + 34) / 160, -1, 1), clamp((y - 27) / 160, -1, 1)]; }
    else if (glance.id) { glance.id = ''; her.look = null; }
    if (skyOn) {
      pop.style.transform = `translate(${left.value}px,${top.value}px)`; pop.style.setProperty('--sx', `${needle.value - left.value}px`);
      Object.assign(gap.style, { left: `${left.value - 18}px`, width: `${wordWidth + 32}px`, top: `${top.value - 8.5}px`, height: `${Math.max(0, opening.value - 4)}px` });
      axis.style.top = `${skyHeight() - 26}px`;
      const labels = g.guides.map(ago), guidesKey = labels.join(',');
      if (axisKey !== guidesKey) { axisKey = guidesKey; axis.innerHTML = labels.map(label => `<span>${label}</span>`).join('') + '<span class="nowl">现在</span>'; }
      // The axis steps aside where the needle writes its own time.
      const writes = stand ? -1e3 : needle.value;
      // Where the cells are too narrow for every label, every other one (counting back from now) keeps its words.
      const every = Math.ceil(64 / g.cell);
      [...axis.children].forEach((el, i) => { const tick = el as HTMLElement, x = g.guides[i] === undefined ? g.x1 : g.xOf(now - g.guides[i]); tick.style.left = `${x - 30}px`; tick.style.opacity = String(g.guides[i] === undefined ? 1 : (i + 1) % every ? 0 : clamp((Math.abs(x - writes) - 34) / 26)); });
      rowsEl.querySelectorAll<HTMLElement>('.bw-row').forEach((el, i) => { el.style.left = `${g.x1}px`; el.style.opacity = String(1 - Math.min(.4, Math.abs(i - focus.value) * .1)); });
      renderWords();
      if (t < revealUntil) revealSelection();
    }
    starsEl.inert = skyOn; starsEl.style.pointerEvents = skyOn ? 'none' : '';
    her.frame(t, dt);
  }
  modeButton.addEventListener('click', () => setMode(!enabled));
  pull.addEventListener('click', () => { if (!skyOn) openSky(); else { closeSky(); settle(); } });
  $('.bw-her', chrome).addEventListener('click', openDeck); held.addEventListener('click', openDeck); offerEl.addEventListener('click', openDeck);
  $('.bw-her', chrome).addEventListener('pointerenter', () => her.hover = true); $('.bw-her', chrome).addEventListener('pointerleave', () => her.hover = false);
  $('.bw-her', chrome).addEventListener('pointerdown', () => her.pressed = true);
  addEventListener('pointerup', () => her.pressed = false);
  win.addEventListener('pointermove', e => { const r = win.getBoundingClientRect(); her.look = [(e.clientX - r.left - width + 34) / 140, (e.clientY - r.top - 27) / 140]; });
  win.addEventListener('pointerleave', () => { her.look = null; her.pressed = false; });
  rest.addEventListener('click', () => { expandRest = true; refresh(); });
  skyEl.addEventListener('wheel', () => revealUntil = 0, { passive: true });
  for (const el of [rowsEl, starsEl]) el.addEventListener('click', e => { const id = (e.target as HTMLElement).closest<HTMLElement>('[data-session]')?.dataset.session; if (id) { peek.hidden = true; closeSky(); hooks.open(id); } });
  starsEl.addEventListener('pointerover', e => { const target = (e.target as HTMLElement).closest<HTMLElement>('[data-session]'), s = target && get(target.dataset.session!); if (!s) return; hoverStar = s.id; peek.innerHTML = `<b>${esc(s.title)}</b>${stateHTML(s, 'span')}<small>${esc(s.summary)}</small>`; peek.style.left = `${clamp(target.offsetLeft + 10 - 110, 12, width - 250)}px`; peek.hidden = false; });
  starsEl.addEventListener('pointerout', e => { if (!(e.relatedTarget as Element | null)?.closest?.('.bw-stars button')) { hoverStar = ''; peek.hidden = true; } });
  // What the pointer is on in the sky, with the prototype's reach: a row within half its height of its (bent) line,
  // one of your points within 7 px across and 13 px of its tick.
  function hit(e: MouseEvent) {
    if ((e.target as HTMLElement).closest('button,.bw-open')) return null;
    const rect = win.getBoundingClientRect(), x = e.clientX - rect.left, y = e.clientY - rect.top + skyEl.scrollTop, g = geo();
    if (y <= 56 || y >= 56 + skyHeight() - 30 || x < 20 || x > g.x1) return null;
    const row = rows.findIndex((_, i) => Math.abs(g.yx(i, x) - y) <= 27 / 2);
    if (row < 0) return null;
    const turn = (trails[rows[row].id]?.turns ?? []).findIndex(t => t.at !== undefined && Math.abs(g.xOf(t.at / 60000) - x) < 7);
    return { row, turn: turn >= 0 && Math.abs(y - (g.yx(row, x) - 8)) < 13 ? turn : -1 };
  }
  // Pointing at another of your points on the row you stand on reads it in place; leaving puts back where you stand.
  skyEl.addEventListener('pointermove', e => {
    const over = hit(e), q = over && over.turn >= 0 && rows[over.row]?.id === selected ? over.turn : null;
    skyEl.style.cursor = over ? 'pointer' : '';
    if (q !== hoverQi) { hoverQi = q; renderWords(); }
  });
  skyEl.addEventListener('pointerleave', () => { skyEl.style.cursor = ''; if (hoverQi !== null) { hoverQi = null; renderWords(); } });
  // A point is a place to stand; the rest of a row is its session, and clicking it goes in.
  skyEl.addEventListener('click', e => {
    const over = hit(e); if (!over) return;
    if (over.turn < 0) { const id = rows[over.row].id; closeSky(); hooks.open(id); settle(); return; }
    const moved = rows[over.row].id !== selected || over.turn !== qi || nameStop;
    selected = rows[over.row].id; qi = over.turn; nameStop = false; hoverQi = null; time = point()?.at ?? null;
    if (moved) hooks.blip(1);
    renderRows(); renderWords(); revealUntil = performance.now() + 800;
  });
  card.addEventListener('click', e => { const action = (e.target as HTMLElement).closest<HTMLElement>('[data-deck]')?.dataset.deck; if (action) void decide(action); });
  // One question, one answer: choosing it is answering.
  card.addEventListener('change', e => { const el = e.target as HTMLInputElement, r = deck[0]?.req; if (el.type === 'radio' && r?.tool === 'Ask' && r.qs.length === 1 && !r.qs[0].multi) void decide('allow'); });
  win.addEventListener('click', e => { if ((e.target as HTMLElement).closest('.c-held')) openDeck(); });
  ta.addEventListener('input', () => { typedAt = performance.now(); hideOffer(); });
  // Reading back is scrolling away from the latest; the conversation following its own end is not.
  win.addEventListener('scroll', e => { const el = e.target as HTMLElement; if (el.matches('.conv') && el.scrollTop < el.scrollHeight - el.clientHeight - 40) { scrolledAt = performance.now(); hideOffer(); } }, true);
  // On the window, so the keys still arrive while a request holds the composer shut and nothing inside has focus.
  addEventListener('keydown', keyboard, true);
  win.addEventListener('compositionstart', () => { if (skyOn) { closeSky(); settle(); } }, true);
  addEventListener('focus', () => offer('你回来了'));
  window.agents?.onNext?.(next);
  new ResizeObserver(() => { width = win.clientWidth; height = win.clientHeight; rowKey = ''; renderRows(); if (skyOn) renderWords(); }).observe(win);
  pullLabel(); refresh(); setMode(enabled);
  return { invalidate(id: string) { sources.delete(id); refresh(); }, get enabled() { return enabled; }, get busy() { return deckOn || skyOn; }, refresh, frame,
    sent() { her.say('31', 1100); const sentAt = performance.now(); window.setTimeout(() => { if (typedAt <= sentAt) offer('你刚发出去一条，正好过一遍'); }, 1050); },
    notify(s: Sess) {
      if (!enabled) return;
      // Another session came to need you, stopped or finished: she only glances at its star.
      if (s.id !== hooks.current() && (s.st === 'wait' || s.st === 'err' || s.st === 'done')) glance = { id: s.id, until: performance.now() + 1600 };
      if (s.st === 'err' || waitMin(s) >= 10) her.say(s.st === 'err' ? '34' : 'ask', 1800);
      refresh();
    },
  };
}

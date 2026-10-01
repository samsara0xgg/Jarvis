import type { Item, Sess } from '../../../electron/agents/types';
import { glyph, type St } from './glyph';
import { clamp, dpr, easeInOut, esc, hash, lerp, reduced, smooth, spring, step } from './motion';
import { Her } from './her';
import { ago, drawSky, geometry } from './sky';
import { started, timeline, type Trail, type Turn } from './timeline';
import { mountAway } from './away';
import { findField, hl, land, search, turnHits } from './find';
import { mountWaiting } from './waiting';

type Hooks = {
  sessions(): Sess[]; items(id: string): Item[] | undefined; current(): string; chat(): boolean;
  load(id: string): Promise<void>; open(id: string): void;
  call(route: string, body?: unknown): Promise<unknown>;
  md(text: string): string; toast(text: string): void; cue(name: string, gain?: number): void;
  // blip: the short tick of a step along the sky, pitched by p · changed: when a session last changed state (performance.now)
  blip(p: number): void; changed(id: string): number;
  refresh(): void;
  // back: the workbench steps back a layer first (out of a preview) and says whether it did
  back?(): boolean;
  // the page's one popover, at a point in the window, for her menus
  menu(html: string, at: HTMLElement | { x: number; y: number }): void; closeMenu(): void;
};
const kbd = (key: string) => `<kbd>${key}</kbd>`;
const words: Record<St, string> = { work: '在干活', pack: '在整理', wait: '等你', done: '做完了', read: '看过了', err: '停了' };
const status = (s: Sess): St => s.st === 'done' && !s.unread ? 'read' : s.st;
const $ = <T extends HTMLElement = HTMLElement>(s: string, root: ParentNode) => root.querySelector(s) as T;
const animate = (el: Element, frames: Keyframe[], duration: number, easing = 'cubic-bezier(.2,.8,.2,1)', more: KeyframeAnimationOptions = {}) => reduced.matches ? null : el.animate(frames, { duration, easing, ...more });
// The label in front of a reply that is not its last words: still working, waiting on you, stopped.
const LEAD: Partial<Record<Turn['kind'], string>> = { live: '还在做', wait: '等你', err: '停了' };
// A line under the sessions in the sky, stood on like a row: 新会话, the resting and the archived folds, an archived
// session (back: its id, to take it back). Its key is where you stand while on it.
type X = { key: string; label: string; arch?: boolean; back?: string; line: number; end?: boolean };

export function mountExposure(win: HTMLElement, ta: HTMLTextAreaElement, hooks: Hooks) {
  const chrome = document.createElement('div'); chrome.className = 'exposure';
  chrome.innerHTML = `<div class="bw-hz"></div><canvas class="bw-cv" aria-hidden="true"></canvas>
    <div class="bw-stars" aria-label="会话地平线"></div>
    <button type="button" class="bw-her" aria-label="Jarvis：下一个等你的"><canvas></canvas></button>
    <button type="button" class="bw-pull" aria-expanded="false"></button>
    <div class="bw-sky" role="region" aria-label="长曝光时间线" inert>
      <div class="bw-rows"></div><div class="bw-axis"></div><div class="bw-gap" hidden></div><div class="bw-open" hidden><div class="pp-in"></div></div>
      <div class="bw-more"></div>
    </div>
    <div class="bw-peek" hidden></div>`;
  win.append(chrome);
  const cv = $<HTMLCanvasElement>('.bw-cv', chrome), c = cv.getContext('2d')!;
  const skyEl = $('.bw-sky', chrome), rowsEl = $('.bw-rows', chrome), starsEl = $('.bw-stars', chrome), axis = $('.bw-axis', chrome);
  const pop = $('.bw-open', chrome), inner = $('.pp-in', pop), gap = $('.bw-gap', chrome), pull = $('.bw-pull', chrome);
  const peek = $('.bw-peek', chrome);
  // ⌘K's field stands where 收起 does while it searches.
  const { el: findEl, input: findInput } = findField(); pull.after(findEl);
  const away = mountAway();
  // Under the rows, a place for what a feature lists in the sky (the sessions in a terminal); its data-h is its height.
  const more = $('.bw-more', chrome);
  const head = $('.m-head', win), main = $('.main', win);
  const her = new Her($<HTMLCanvasElement>('.bw-her canvas', chrome), 56, 15.6, true);
  // The window is always the long exposure; the classic layout stays in the code, unreachable, until it is removed.
  const enabled = true;
  let skyOn = false, nameStop = false, selected = '', qi = -1, time: number | null = null;
  let width = win.clientWidth, height = win.clientHeight, now = Date.now() / 60000, openMore = false, back = Infinity, rowKey = '', popKey = '';
  let lastRefresh = 0, pointerAt = -Infinity;
  const trails: Record<string, Trail> = {}, sources = new Map<string, { s: string; items?: Item[] }>();
  let all: Sess[] = [], rows: Sess[] = [], resting: Sess[] = [], folds: Sess[] = [], xrows: X[] = [];
  // ⌘K: searching, what is typed, the sessions the host finds it in, and per session the turns that say it.
  let finding = false, query = '', findSeq = 0, found = new Set<string>();
  const hits = new Map<string, { items?: Item[]; q: string; turns: number[] }>();
  const searching = () => finding && !!query.trim();
  const saying = (s: Sess, q = query.trim().toLowerCase()) => {
    const items = hooks.items(s.id), was = hits.get(s.id);
    if (was && was.items === items && was.q === q) return was.turns;
    const turns = turnHits(items ?? [], q); hits.set(s.id, { items, q, turns }); return turns;
  };
  const loading = new Set<string>();
  let presenceKey = '', axisKey = '', axisW: number[] = [], revealUntil = 0;
  // hoverQi: the point of the selected row under the pointer, read in place of the one you stand on · hoverStar: the
  // horizon star under it · glance: the star she turns to when a session changes, until when
  let hoverQi: number | null = null, hoverStar = '', glance = { id: '', until: 0 };
  // pan: how far the sky is slid right to show older time; panTo, where it is going; follow, what it last followed.
  let panTo = 0, follow = '', spansKey = '', spans: [number, number] = [60, 60], trailsSeen = 0;
  const pan = spring(0);
  const sky = spring(0), opening = spring(0), left = spring(0), top = spring(0), needle = spring(0), needleY = spring(0), focus = spring(0);
  const offsets = new Map<string, ReturnType<typeof spring>>();
  let wordWidth = 0, wordHeight = 0, targetLeft = 0, targetTop = 0, nearNow = false, snap = true;
  const get = (id: string) => hooks.sessions().find(s => s.id === id);
  const point = () => trails[selected]?.turns[qi];
  // Standing on a line under the sessions: no words, no needle, nothing bends.
  const onX = () => selected.startsWith('x:') || selected.startsWith('m:');
  // The rows another feature puts at the sky's foot (the terminal's sessions) are stops too, after its own lines.
  const moreRows = () => more.hidden ? [] : [...more.querySelectorAll<HTMLElement>('button')];
  const index = () => Math.max(0, rows.findIndex(s => s.id === selected));
  const stops = () => [...rows.map(s => s.id), ...xrows.filter(x => x.key !== 'x:none').map(x => x.key), ...moreRows().map((_, i) => `m:${i}`)];
  const xLines = () => xrows.reduce((n, x) => Math.max(n, x.line + 1), 0);
  const place = () => selected.startsWith('m:') ? rows.length + xLines() + Number(selected.slice(2)) : onX() ? rows.length + (xrows.find(x => x.key === selected)?.line ?? 0) : index();
  // Where the sky opens, and where a search starts: the session on screen, else the first.
  const first = () => (searching() ? undefined : rows.find(s => s.id === hooks.current())?.id) ?? stops()[0] ?? '';
  const since = (s: Sess) => s.trace?.at(-1)?.at ?? hooks.items(s.id)?.find(it => it.k === 'req' && !it.done)?.at ?? s.updated;
  const waitMin = (s: Sess) => Math.max(0, Math.round((Date.now() - since(s)) / 60000));
  const stateText = (s: Sess) => `${words[status(s)]}${s.st === 'wait' ? ` · ${waitMin(s) || '刚刚'}${waitMin(s) ? ' 分' : ''}` : ''}`;
  const stateHTML = (s: Sess, tag = 'em') => `<${tag} class="st-${status(s)}">${stateText(s)}</${tag}>`;
  const skyHeight = () => 26 + (rows.length + xLines()) * 27 + opening.value + 34 + (more.hidden ? 0 : Number(more.dataset.h) || 0);
  // Under the words the trails bend down once, past their right edge, and stay down all the way back.
  const geo = () => geometry(width, now, ...reach(), skyHeight(),
    (i, x) => (offsets.get(rows[i]?.id)?.value ?? 0) * (1 - smooth(left.value + wordWidth + 14, left.value + wordWidth + 58, x)), pan.value);
  // How far back the sky opens, and how far back it goes. It opens on the stretch you have been working in: back from
  // the newest moment in any row until three quiet hours; an older session's line comes in from the left edge instead
  // of squeezing that stretch, and what is older is a slide away.
  // Every moment a session marks on its trail: what you said, and where each stretch began and ended; one still going
  // reaches now, except resting, which only says when it began.
  const moments = (s: Sess) => { const t = trails[s.id]; return t ? [...t.marks.map(m => m.t), ...t.segs.flatMap(g => g.b === null ? g.k === 'idle' ? [g.a] : [g.a, now] : [g.a, g.b])].filter(m => m <= now) : []; };
  let endsAt = -1; const ends = new Map<string, number>();
  const ended = (s: Sess) => {
    if (endsAt !== trailsSeen) { endsAt = trailsSeen; ends.clear(); }
    // A session taken in from a terminal is dated by when it was taken in, so its trail says when it last did anything.
    let e = ends.get(s.id); if (e === undefined) { const m = moments(s); ends.set(s.id, e = m.length ? Math.max(...m) : s.updated / 60000); }
    return e;
  };
  // The stretch you have been working in: back from the newest moment until three quiet hours.
  function stretch(ss: Sess[]) {
    const at = ss.flatMap(moments).sort((a, b) => b - a);
    let from = at[0] ?? now;
    for (const t of at) { if (from - t > 180) break; from = t; }
    return from;
  }
  function reach(): [number, number] {
    const key = `${trailsSeen}|${rows.map(s => s.id).join(',')}|${all.length}|${Math.floor(now)}`;
    if (key === spansKey) return spans;
    spansKey = key;
    const recent = Math.max(60, now - stretch(rows)) * 1.04;
    spans = [recent, Math.max(recent, ...all.map(s => (now - (trails[s.id]?.segs[0]?.a ?? now)) * 1.04))];
    return spans;
  }
  // A star rises from where it rests (on the horizon, or in the queue by her) to its row's head as the sky opens.
  const starPosition = (i: number): [number, number] => {
    // Staggered by row, but the last one still lands by the time the sky is open.
    const p = easeInOut(clamp(sky.value * 1.3 - i * Math.min(.03, .3 / Math.max(1, rows.length - 1)))), [x, y] = waiting.at(rows[i]?.id ?? '');
    return [lerp(x, width - 262, p), lerp(y, 82 + i * 27, p)];
  };
  async function ensure(id: string) {
    if (hooks.items(id) || loading.has(id)) return;
    loading.add(id); try { await hooks.load(id); } finally {
      loading.delete(id); refresh();
      if (skyOn && selected === id && (qi < 0 || searching())) { latest(true); if (qi >= 0 && !searching()) nameStop = false; findStop(); renderWords(); revealUntil = performance.now() + 800; }
    }
  }
  function setMode(on: boolean) {
    closeSky();
    if (on) win.append(head); else main.prepend(head);
    win.classList.toggle('bw', on); win.classList.toggle('m-top', on); win.classList.toggle('oc-over', on);
    chrome.hidden = !on;
    if (on) { for (const s of all) void ensure(s.id); }
    hooks.refresh(); refresh();
  }
  function refresh() {
    const every = hooks.sessions(), archived = every.filter(s => s.archived);
    all = every.filter(s => !s.archived);
    // A search reaches the archived too, so they get trails of their own while it lasts.
    for (const s of searching() ? every : all) {
      const items = hooks.items(s.id), key = `${s.st}|${s.updated}|${s.summary}|${s.now}|${s.trace?.length}`;
      if (sources.get(s.id)?.items !== items || sources.get(s.id)?.s !== key) {
        trails[s.id] = timeline(s, items ?? []); sources.set(s.id, { s: key, items }); trailsSeen++;
      }
    }
    // Pinned first, then by when each began: a row never moves because its state changed.
    const order = (a: Sess, b: Sess) => Number(b.pinned) - Number(a.pinned) || started(a, trails[a.id]) - started(b, trails[b.id]) || a.id.localeCompare(b.id);
    all.sort(order); archived.sort(order);
    if (searching()) {
      // ⌘K keeps the sessions that say it (or are named by it, or the host finds it in), the archived after the rest.
      const q = query.trim().toLowerCase();
      rows = [...all, ...archived].filter(s => saying(s, q).length || s.title.toLowerCase().includes(q) || found.has(s.id)); resting = folds = [];
      xrows = rows.length ? [] : [{ key: 'x:none', label: `没找到「${query.trim()}」`, line: 0 }];
    } else {
      // Read sessions whose last moment is before the stretch you have been working in rest behind one line, and past
      // fourteen sessions so do the ones read and idle an hour; a slide back to their time brings them in under the rest.
      const from = stretch(all), cur = hooks.current();
      // The one you stand on stays among them, so a row never moves under you as you walk.
      const rests = (s: Sess) => status(s) === 'read' && !s.pinned && s.id !== cur
        && (ended(s) < from || (all.length > 14 && Date.now() - s.updated > 3600000));
      const folded = folds = all.filter(rests), shown = openMore ? folded : folded.filter(s => ended(s) >= back || s.id === selected);
      resting = folded.filter(s => !shown.includes(s));
      rows = [...all.filter(s => !folded.includes(s)), ...shown];
      const n = resting.length + archived.length;
      xrows = [{ key: 'x:new', label: '＋ 新会话', line: 0 }];
      if (openMore || n) xrows.push({ key: 'x:more', label: openMore ? '收起' : `还有 ${n} 个 ›`, line: 0, end: true });
      if (openMore) archived.forEach((s, j) => xrows.push({ key: `x:a:${s.id}`, label: s.title, arch: true, back: s.id, line: j + 1 }));
    }
    if (!stops().includes(selected)) { selected = first(); latest(); findStop(); }
    if (enabled) for (const s of all) if (s.st === 'wait') void ensure(s.id);
    const ids = all.map(s => s.id), presence = JSON.stringify([enabled, ids]);
    if (presence !== presenceKey) { presenceKey = presence; window.agents?.presence?.(enabled, ids); }
    renderRows(); if (skyOn) renderWords();
  }
  function renderRows() {
    // While searching, a name shows what matched and how many of its sentences say it.
    const q = searching() ? query.trim() : '', count = (s: Sess) => q ? saying(s).length : 0;
    const key = rows.map(s => `${s.id}|${s.title}|${status(s)}|${waitMin(s)}|${s.archived}|${count(s)}`).join(';') + `|${selected}|${skyOn}|${nameStop}|${hooks.current()}|${q}|${xrows.map(x => x.key + x.label).join(',')}|${waiting.key}`;
    // A search looks through the window's own sessions: the terminal's group steps out while it runs.
    more.hidden = searching();
    moreRows().forEach((b, i) => b.classList.toggle('sel', skyOn && selected === `m:${i}`));
    if (rowKey === key) return; rowKey = key;
    rowsEl.innerHTML = rows.map((s, i) => `<button type="button" class="bw-row${selected === s.id ? ' sel' : ''}${hooks.current() === s.id ? ' on' : ''}${nameStop && selected === s.id ? ' nm' : ''}" data-session="${esc(s.id)}" style="top:${15 + i * 27}px" aria-label="${esc(s.title)}，${s.archived ? '已归档' : words[status(s)]}"><b>${hl(s.title, q)}</b>${count(s) ? `<em class="st-find">${count(s)} 处说过</em>` : ''}${s.archived ? '<em class="st-arch">已归档</em>' : stateHTML(s)}</button>`).join('')
      // The lines under the sessions: 新会话 and taking an archived one back are the page's own acts.
      + xrows.map(x => `<button type="button" class="bw-row bw-x${skyOn && selected === x.key ? ' sel' : ''}${x.arch ? ' arch' : ''}${x.end ? ' end' : x.line === 0 && xrows.some(y => y.end) ? ' start' : ''}" data-x="${esc(x.key)}"${x.key === 'x:new' ? ' data-act="new" title="⌘N"' : x.back ? ` data-act="unarchive" data-id="${esc(x.back)}"` : ''}${x.key === 'x:none' ? ' disabled' : ''} style="top:${15 + (rows.length + x.line) * 27}px"><span>${esc(x.label)}</span>${x.back ? '<span class="back">拿回来</span>' : ''}</button>`).join('');
    starsEl.innerHTML = rows.map(s => { const at = waiting.place(s.id); return at === null ? '' : `<button type="button" data-session="${esc(s.id)}" aria-label="${esc(s.title)}，${words[status(s)]}" style="${at}" title="${esc(s.title)} · ${esc(s.summary)}"></button>`; }).join('');
    more.style.top = `${15 + (rows.length + xLines()) * 27}px`;
  }
  // A session found only in what was said stands on the newest sentence that says it; one its name matches, on its name.
  function findStop() {
    const s = get(selected), q = query.trim().toLowerCase(); if (!searching() || !s) return;
    const turns = saying(s);
    if (turns.length && !s.title.toLowerCase().includes(q)) { nameStop = false; qi = turns[turns.length - 1]; time = point()?.at ?? time; } else nameStop = true;
  }
  function latest(keep = false) {
    if (onX()) return;
    const turns = trails[selected]?.turns ?? [];
    qi = turns.length - 1;
    if (keep && time !== null && turns.some(t => t.at !== undefined)) {
      qi = turns.reduce((best, t, i) => t.at !== undefined && Math.abs(t.at - time!) < Math.abs((turns[best]?.at ?? Infinity) - time!) ? i : best, turns.findIndex(t => t.at !== undefined));
    } else time = turns[qi]?.at ?? null;
  }
  // One paragraph of what it said: the one that says q, else the last.
  function paragraph(text: string, q = '') {
    const template = document.createElement('template'); template.innerHTML = hooks.md(text);
    const ql = q.toLowerCase(), ps = template.content.querySelectorAll('p');
    const p = (q ? [...template.content.querySelectorAll('p,li,h1,h2,h3,h4,pre')].find(p => p.textContent?.toLowerCase().includes(ql)) : undefined) ?? ps[ps.length - 1];
    return p ? p.textContent ?? '' : template.content.textContent ?? text;
  }
  // Its answer in a turn; while searching, the answer that says it, when your words do not.
  function reply(s: Sess, t: Turn, q: string) {
    if (t.kind !== 'sum') return t.reply;
    const items = hooks.items(s.id) ?? [], ql = q.toLowerCase();
    if (q && !t.you.toLowerCase().includes(ql)) for (let k = t.item + 1; k < items.length && items[k].k !== 'you'; k++) {
      const it = items[k]; if (it.k === 'it' && it.text.toLowerCase().includes(ql)) return paragraph(it.text, q);
    }
    return paragraph(t.reply);
  }
  const keys = (...ks: string[]) => `<p class="pp-k">${ks.filter(Boolean).map(k => `<span>${k}</span>`).join('')}</p>`;
  // A point you said something at: when, your words, the last thing it said in that turn, the next key.
  function said(s: Sess, t: Turn, at: number, n: number) {
    const clock = t.at === undefined ? '时间未记录' : new Date(t.at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
    const q = searching() ? query.trim() : '';
    return `<p class="pp-h"><span class="pp-t"><b>${clock}</b><small>第 ${at + 1} / ${n} 句</small></span></p><p class="pp-q">${hl(t.you, q)}</p>`
      + `<p class="pp-a${t.kind === 'sum' ? '' : ` r-${t.kind}`}"><i>${s.agent === 'claude' ? 'Claude' : 'Codex'}</i>${LEAD[t.kind] ? `<em>${LEAD[t.kind]}</em>` : ''}${hl(reply(s, t, q), q)}</p>`
      + keys(at > 0 ? `${kbd('←')} 上一句` : '', `${kbd('→')} ${at < n - 1 ? '下一句' : '到名字'}`, `${kbd('⏎')} 进去`);
  }
  // The name stop, or a session you have not said anything to: where it stands now.
  function standing(s: Sess, n: number) {
    return `<p class="pp-h"><span class="pp-t"><b>现在</b>${stateHTML(s, 'small')}</span></p><p class="pp-her"><i>她</i>${nameStop || n ? '' : '你还没跟它说过话。'}${hl(s.now || s.summary || '', searching() ? query.trim() : '')}</p>`
      + (nameStop ? keys(n ? `${kbd('←')} 回到你说的` : '', `${kbd('→')} 进去`) : keys(`${kbd('→')} 到名字`));
  }
  function renderWords() {
    if (!skyOn) return;
    // On a line under the sessions there is nothing to read.
    const s = get(selected); if (!s) { pop.hidden = gap.hidden = true; wordHeight = 0; popKey = ''; return; }
    // A point under the pointer is read in place of the one you stand on, without moving the needle.
    const turns = trails[selected]?.turns ?? [], at = hoverQi ?? (nameStop ? -1 : qi), t = turns[at];
    const key = `${selected}|${at}|${nameStop}|${t?.kind}|${t?.reply}|${t?.you}|${stateText(s)}|${s.now}|${s.summary}|${turns.length}|${searching() ? query : ''}`;
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
  // find: opened by ⌘K, searching what was said.
  function openSky(find = false) {
    // With none of its own it still opens: 新会话, and the sessions held in a terminal under it.
    if (!enabled) return;
    // ⌘K in an open sky turns it into the search.
    if (skyOn) { if (find) { finding = true; pullLabel(); findInput.focus({ preventScroll: true }); findInput.select(); } return; }
    peek.hidden = true;
    finding = find; query = findInput.value = ''; found = new Set(); openMore = false; back = Infinity; refresh();
    // It opens on the last thing you said to the session on screen, with its words out; ↑↓ keep the moment, ⏎ goes in
    // from any sentence. A session you have said nothing to opens on its name.
    selected = first(); nameStop = false; latest(); nameStop = !trails[selected]?.turns.length;
    pan.value = panTo = 0; pan.velocity = 0; follow = '';
    skyOn = true; snap = true; hoverQi = null; revealUntil = performance.now() + 800; waiting.hide();
    pop.hidden = gap.hidden = true; popKey = '';
    win.classList.add('sky-on'); skyEl.inert = false; pullLabel();
    if (find) findInput.focus({ preventScroll: true }); else { win.tabIndex = -1; win.focus({ preventScroll: true }); }
    renderRows(); renderWords();
    for (const s of all) void ensure(s.id);
  }
  function closeSky() {
    const typing = findEl.contains(document.activeElement);
    // The words and where you stood stay as they are and fade with the sky; frame puts them away once it has folded.
    skyOn = false; hoverQi = null;
    // A search ends with the sky: the sessions come back as they were.
    finding = false; query = findInput.value = ''; found = new Set(); findSeq++;
    win.classList.remove('sky-on'); skyEl.inert = true; skyEl.style.cursor = ''; pullLabel(); refresh();
    if (typing) settle();
  }
  // What is typed in ⌘K's field: the rows narrow at once, and widen when the host finds it where this window has not read.
  function onFind() {
    query = findInput.value; found = new Set(); hoverQi = null;
    refresh(); selected = first(); nameStop = true; latest(); findStop();
    renderRows(); renderWords(); revealUntil = performance.now() + 800;
    const seq = ++findSeq, q = query.trim(), picked = selected;
    if (q) window.setTimeout(() => {
      if (seq === findSeq) void search(hooks.call, q).then(ids => {
        if (seq !== findSeq) return;
        found = ids; for (const id of ids) void ensure(id);
        refresh();
        if (selected === picked) { selected = first(); nameStop = true; latest(); findStop(); }
        renderRows(); renderWords();
      }, () => { /* the host could not search: what this window has read still counts */ });
    }, 120);
  }
  // ↑↓ over the sessions and the lines under them. A new row keeps the moment: it lands on its own sentence nearest to
  // where the needle stood, or, while searching, where it was found.
  function move(d: number, keep: boolean) {
    const list = stops(), to = list[clamp(list.indexOf(selected) + d, 0, list.length - 1)];
    if (!to || to === selected) return;
    selected = to; hoverQi = null; latest(keep); findStop();
    hooks.blip(.82); renderRows(); renderWords(); revealUntil = performance.now() + 800;
  }
  // ⏎ into the session you stand on; from a sentence, its conversation lands on that sentence.
  function enter() {
    const id = selected, t = nameStop ? undefined : point(), q = searching() ? query.trim() : '', items = hooks.items(id) ?? [];
    if (!get(id)) return;
    closeSky(); hooks.open(id); settle();
    if (!t) return;
    const upto = items.findIndex((it, i) => i > t.item && it.k === 'you');
    requestAnimationFrame(() => land(win, () => hooks.current() === id, t.item, upto < 0 ? items.length : upto, q));
  }
  // ⏎ or → where you stand: a session goes in; a line under the sessions does its one thing.
  function activate() {
    if (selected.startsWith('m:')) moreRows()[Number(selected.slice(2))]?.click();
    else if (onX()) rowsEl.querySelector<HTMLElement>(`[data-x="${CSS.escape(selected)}"]`)?.click(); else if (selected) enter();
  }
  // The horizon is the window's drag strip, so the way in stays on it as the way out: esc, 收起. While searching, the
  // field stands there instead.
  function pullLabel() {
    pull.hidden = skyOn && finding; findEl.hidden = !(skyOn && finding);
    if (!pull.firstElementChild) pull.innerHTML = `<span class="pl-in">${kbd('←')}<span>长曝光</span></span><span class="pl-out" aria-hidden="true">${kbd('esc')}<span>收起</span></span>`;
    pull.querySelector('.pl-in')!.setAttribute('aria-hidden', String(skyOn)); pull.querySelector('.pl-out')!.setAttribute('aria-hidden', String(!skyOn));
    pull.setAttribute('aria-expanded', String(skyOn)); pull.setAttribute('aria-label', skyOn ? '收起长曝光' : '全部会话');
  }
  // Back to where you were writing; while a request holds the composer shut, the window keeps the keys.
  function settle() {
    if (ta.disabled) { win.tabIndex = -1; win.focus({ preventScroll: true }); } else ta.focus({ preventScroll: true });
  }
  function revealSelection() {
    // Against the height the sky is opening to: mid-way it is short, and scrolling then would hide the rows at its top.
    const y = 15 + place() * 27, bottom = Math.max(y + 22, top.value + wordHeight - 18), viewport = Math.min(height - 56, skyHeight() - opening.value + wordHeight);
    if (!viewport || !skyEl.clientHeight) return;
    // A sky that scrolls keeps its axis at the foot: what you stand on stays clear of it.
    const scroll = skyEl.scrollTop, foot = skyHeight() > height - 56 ? 40 : 12;
    if (bottom - y > viewport - 12 - foot || y < scroll + 12) skyEl.scrollTop = Math.max(0, y - 12);
    else if (bottom > scroll + viewport - foot) skyEl.scrollTop = Math.max(0, bottom - viewport + foot);
  }
  function keyboard(e: KeyboardEvent) {
    if (!enabled || e.isComposing) return;
    const target = e.target as HTMLElement, editable = target.matches('input,textarea,select,[contenteditable="true"]');
    const consume = () => { e.preventDefault(); e.stopImmediatePropagation(); };
    // ⌥⇥ and ⌥↓: straight to the next session waiting on you, from anywhere but another field.
    if (e.altKey && e.key === 'Tab') { consume(); if (!e.repeat) waiting.next(); return; }
    if (e.altKey && !e.metaKey && !e.ctrlKey && e.key === 'ArrowDown' && (!editable || target === ta)) { consume(); if (!e.repeat) waiting.next(); return; }
    // ⌘K, from anywhere: the sky, searching what was said. The side list keeps its own field for the pointer.
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'k') { consume(); openSky(true); return; }
    if (skyOn) {
      // In ⌘K's field ↑↓ walk the rows, ⏎ goes in, esc empties it and then closes; the rest is typing.
      if (target === findInput) {
        if (e.key === 'ArrowUp' || e.key === 'ArrowDown') { consume(); move(e.key === 'ArrowDown' ? 1 : -1, false); }
        else if (e.key === 'Enter') { consume(); if (!e.repeat) activate(); }
        else if (e.key === 'Escape') { consume(); if (findInput.value) { findInput.value = ''; onFind(); } else { closeSky(); settle(); } }
        return;
      }
      if (editable && target !== ta) { closeSky(); return; }
      if (e.key.startsWith('Arrow') || e.key === 'Escape' || e.key === 'Enter') {
        consume(); hoverQi = null;
        const turns = trails[selected]?.turns ?? [], was = qi, list = stops();
        if (e.key === 'Escape' || (e.key === 'ArrowDown' && list.indexOf(selected) === list.length - 1)) { closeSky(); settle(); return; }
        if (e.key === 'ArrowUp' || e.key === 'ArrowDown') { move(e.key === 'ArrowDown' ? 1 : -1, true); return; }
        // A line under the sessions has no sentences: → and ⏎ do its one thing.
        if (onX()) { if (e.key !== 'ArrowLeft' && !e.repeat) activate(); return; }
        if (e.key === 'ArrowLeft') {
          if (!turns.length) { hooks.toast('你还没跟它说过话'); return; }
          if (nameStop) { nameStop = false; qi = turns.length - 1; } else qi = qi < 0 ? turns.length - 1 : Math.max(0, qi - 1);
          time = point()?.at ?? time;
          if (qi !== was) hooks.blip(1);
        } else if (e.key === 'ArrowRight' || e.key === 'Enter') {
          if (nameStop || e.key === 'Enter') { if (!e.repeat) enter(); return; }
          // Past the last thing you said, the name is one stop before going in.
          if (qi >= turns.length - 1) { nameStop = true; hooks.cue('mic', .35); } else { qi++; time = point()?.at ?? time; hooks.blip(1); }
        }
        renderRows(); renderWords(); revealUntil = performance.now() + 800; return;
      }
      if (!e.metaKey && !e.ctrlKey && !e.altKey && (e.key.length === 1 || e.key === 'Process')) {
        // While searching, what you type goes to the field; otherwise a space is a pause here, and a word goes to the composer.
        if (finding) {
          findInput.focus({ preventScroll: true });
          if (e.key.length === 1) { consume(); findInput.setRangeText(e.key, findInput.selectionStart ?? 0, findInput.selectionEnd ?? 0, 'end'); onFind(); }
          return;
        }
        if (e.key === ' ') { consume(); return; }
        closeSky(); settle();
        if (e.key.length === 1) { consume(); if (!ta.disabled) { ta.setRangeText(e.key, ta.selectionStart, ta.selectionEnd, 'end'); ta.dispatchEvent(new Event('input', { bubbles: true })); } }
      }
      return;
    }
    if (editable && target !== ta) return;
    // An empty composer, or none to write in while a request holds it, is a pause: the keys are hers.
    const free = (target === ta || target === win || target === document.body) && (ta.disabled || !ta.value.trim());
    const plain = !e.metaKey && !e.ctrlKey && !e.altKey;
    if (e.key === 'Escape' && !win.querySelector('.pop.on') && waiting.esc()) { consume(); return; }
    if ((free && plain && !e.shiftKey && e.key === 'ArrowLeft') || (e.altKey && e.key === 'ArrowUp')) { consume(); openSky(); return; }
  }
  function frame(t: number, dt: number) {
    if (!enabled) return;
    now = Date.now() / 60000;
    if (t - lastRefresh > 1000) { lastRefresh = t; refresh(); }
    step(sky, skyOn ? 1 : 0, 2.2, .92, dt); const selectedIndex = index();
    // Every star toward its resting place; the buttons follow when the places change.
    if (waiting.move(dt)) renderRows();
    // Folding, the sky keeps its shape: the words stay where they stood and the rows under them stay parted.
    if (!skyOn && !pop.hidden && sky.value < .02) { pop.hidden = gap.hidden = true; wordHeight = 0; popKey = ''; }
    const shown = skyOn || sky.value > .02, parted = skyOn || !pop.hidden;
    step(opening, parted ? wordHeight : 0, 2.6, .9, dt); step(left, targetLeft, 2.8, .86, dt); step(top, targetTop, 2.8, .86, dt); step(focus, place(), 2.4, 1, dt);
    for (const [i, s] of rows.entries()) {
      const value = offsets.get(s.id) ?? spring(0); offsets.set(s.id, value);
      step(value, parted && i > selectedIndex ? wordHeight : 0, 2.6, .9, dt);
    }
    // Stepping to a sentence out of view slides the sky just far enough to bring it in; the name stop slides back to now.
    const standing = `${selected}|${nameStop ? -1 : qi}`;
    if (skyOn && standing !== follow) {
      follow = standing;
      const g = geo(), turn = point();
      if (nameStop || turn?.at === undefined) panTo = 0;
      else { const x = g.xOf(turn.at / 60000), room = Math.min(120, (g.x1 - g.x0) / 3); panTo = g.pan + Math.max(0, g.x0 + room - x) - Math.max(0, x - (g.x1 - room)); }
    }
    const was = pan.value;
    step(pan, panTo = clamp(panTo, 0, geo().panMax), 2.6, 1, dt);
    // What is drawn moves with the sky: the needle keeps its point instead of trailing behind.
    needle.value += pan.value - was;
    // Slid back, the folded sessions whose time comes into view join the rows under the rest, and stay until the sky
    // closes, so walking the rows never makes one vanish.
    const g1 = geo(), seen = pan.value > 1 ? g1.tOf(g1.x0) : Infinity;
    if (seen < back && folds.some(s => ended(s) >= seen && ended(s) < back)) { back = seen; refresh(); }
    // At the name stop the needle's point slides on to now and waits by the name; the needle itself steps aside.
    const g = geo(), turn = point(), at = turn?.at !== undefined ? turn.at / 60000 : undefined, stand = nameStop || at === undefined;
    const nx = stand ? g.x1 : g.xOf(at!), ny = g.y(selectedIndex);
    if (snap || sky.value < .3) { needle.value = nx; needle.velocity = 0; needleY.value = ny; needleY.velocity = 0; snap = false; }
    else if (skyOn) { step(needle, nx, 2.8, .82, dt); step(needleY, ny, 2.8, .86, dt); }
    const skyPx = Math.min(height - 56, sky.value * skyHeight());
    // A sky taller than the window scrolls; its time axis stays at the foot of what shows, over the rows going under it.
    const full = skyHeight(), foot = skyOn && full > height - 56 ? Math.min(full, skyEl.scrollTop + height - 56) : full;
    win.style.setProperty('--sky', `${sky.value * skyHeight()}px`); win.style.setProperty('--skp', String(sky.value));
    skyEl.style.height = `${skyPx}px`;
    skyEl.style.opacity = String(clamp((sky.value - .35) / .5)); skyEl.style.pointerEvents = skyOn ? 'auto' : 'none';
    // While it opens or folds, what is in the sky fades out toward its moving edge instead of being cut by it.
    const fade = (1 - sky.value) * 72, soft = sky.value > .001 && fade > .5;
    for (const [el, mask] of [[skyEl, `linear-gradient(#000 calc(100% - ${fade}px), transparent)`], [cv, `linear-gradient(#000 ${56 + skyPx - fade}px, transparent ${56 + skyPx}px)`]] as const) {
      el.style.setProperty('mask-image', soft ? mask : ''); el.style.setProperty('-webkit-mask-image', soft ? mask : '');
    }
    const ratio = dpr(), canvasHeight = Math.max(height, skyHeight() + 56);
    if (cv.width !== Math.round(width * ratio) || cv.height !== Math.round(canvasHeight * ratio)) { cv.width = Math.round(width * ratio); cv.height = Math.round(canvasHeight * ratio); cv.style.width = `${width}px`; cv.style.height = `${canvasHeight}px`; }
    c.setTransform(ratio, 0, 0, ratio, 0, 0); c.clearRect(0, 0, width, canvasHeight);
    // Scrolling a tall sky moves its trails and text together; the desk below never scrolls with it.
    c.save();
    if (skyEl.scrollTop > 0) { c.beginPath(); c.rect(0, 56, width, height - 56); c.clip(); }
    c.translate(0, -skyEl.scrollTop);
    // Slid back in time, what is newer than the sky's right edge goes under it rather than under the names.
    if (g.pan > .5) { c.beginPath(); c.rect(0, 0, g.x1 + 1, canvasHeight + skyEl.scrollTop); c.clip(); }
    drawSky(c, rows, trails, { now, p: sky.value, dev: clamp((sky.value - .18) / .82), geo: foot < full ? { ...g, bottom: 56 + foot - 22 } : g, foot: foot < full, sel: shown && !onX() ? selectedIndex : undefined, pt: shown ? at : undefined,
      aways: away.spans().map(s => ({ a: s.a / 60000, b: s.b === null ? null : s.b / 60000 })),
      span: stand ? undefined : [at!, (trails[selected]?.turns[qi + 1]?.at ?? Date.now()) / 60000], ndx: at === undefined ? undefined : needle.value, nm: nameStop,
      cy: needleY.value, focus: shown ? focus.value : undefined,
      card: pop.hidden ? null : { x: left.value - 18, y: 56 + top.value - 8.5, w: wordWidth + 32, h: Math.max(0, opening.value - 4) },
      conn: nearNow && !pop.hidden ? { x: stand ? g.x1 : needle.value, y: needleY.value, x2: left.value + wordWidth - 16, y2: top.value + 58, a: clamp((sky.value - .35) / .5) } : null });
    // Where there is more time past an edge, the sky fades out toward it.
    c.globalCompositeOperation = 'destination-out';
    for (const [more, from, to] of [[g.panMax - g.pan, g.x0, g.x0 + 56], [g.pan, g.x1, g.x1 - 40]] as const) {
      if (more < .5) continue;
      const fade = c.createLinearGradient(from, 0, to, 0), a = clamp(more / 40);
      fade.addColorStop(0, `rgba(0,0,0,${a})`); fade.addColorStop(1, 'rgba(0,0,0,0)');
      c.fillStyle = fade; c.fillRect(Math.min(from, to) - (from < to ? 30 : 0), g.top, Math.abs(to - from) + 30, g.bottom - g.top + 30);
    }
    c.globalCompositeOperation = 'source-over';
    c.restore();
    // A star pops when its session changes state and swells under the pointer; one waiting on you is the queue's to
    // draw while the sky is closed.
    rows.forEach((s, i) => {
      const [x, y] = starPosition(i), sy = y - (skyOn ? skyEl.scrollTop : 0); if (skyOn && skyEl.scrollTop && sy < 65) return;
      if (waiting.queued(s.id) && sky.value < .02) return;
      if (sky.value < .05) waiting.streak(c, s.id, status(s), x, sy);
      c.save(); c.translate(x, sy); glyph(c, status(s), { lit: s.id === hooks.current(), t: t / 1000 + hash(s.id) * 9, since: (t - hooks.changed(s.id)) / 1000, hover: !skyOn && hoverStar === s.id, calm: reduced.matches, lift: 1.3 }); c.restore();
    });
    // Who waits: A lines them up by her, B circles her; the sky takes them back as it opens.
    waiting.draw(c, t, 1 - clamp(sky.value * 4), skyOn ? '' : hoverStar);
    // For a moment after a session changes, she looks toward its star.
    const glancing = t < glance.until ? rows.findIndex(r => r.id === glance.id) : -1;
    if (glancing >= 0) { const [x, y] = starPosition(glancing); her.look = [clamp((x - width + 34) / 160, -.8, .8), clamp((y - 28) / 160, -.8, .8)]; }
    else if (glance.id) { glance.id = ''; her.look = null; }
    // At rest, a while after the pointer last moved, her eyes are on the first one waiting.
    else if (t - pointerAt > 1800) her.look = waiting.lookAt();
    if (shown) {
      pop.style.transform = `translate(${left.value}px,${top.value}px)`; pop.style.setProperty('--sx', `${needle.value - left.value}px`);
      Object.assign(gap.style, { left: `${left.value - 18}px`, width: `${wordWidth + 32}px`, top: `${top.value - 8.5}px`, height: `${Math.max(0, opening.value - 4)}px` });
      axis.style.top = `${foot - 26}px`;
      const labels = g.guides.map(ago), guidesKey = labels.join(',');
      if (axisKey !== guidesKey) {
        axisKey = guidesKey; axis.innerHTML = labels.map(label => `<span>${label}</span>`).join('') + '<span class="nowl">现在</span>';
        axisW = [...axis.children].map(el => { const r = document.createRange(); r.selectNodeContents(el); return r.getBoundingClientRect().width; });
      }
      // The axis steps aside where the needle writes its own time, in a pill as wide as its words (sky.ts draws it).
      // Off the sky's edge (slid back past it), the time sits at that edge.
      c.font = '500 10px "IBM Plex Mono", ui-monospace, monospace';
      const pill = stand ? 0 : c.measureText(ago(Math.max(0, now - g.tOf(needle.value)))).width + 12;
      const writes = stand ? -1e3 : clamp(needle.value, g.x0 + pill / 2, g.x1 - pill / 2);
      // Where the cells are too narrow for every label, every other one (counting back from now) keeps its words.
      const every = Math.ceil(64 / g.cell);
      // Slid back in time, the words fade out toward an edge with more time past it.
      [...axis.children].forEach((el, i) => {
        const tick = el as HTMLElement, x = g.guides[i] === undefined ? g.xOf(now) : g.xOf(now - g.guides[i]);
        const edge = Math.min(clamp((x - g.x0 - (g.panMax - g.pan > 8 ? 30 : -30)) / 24), clamp((g.x1 + 30 - x) / 24));
        tick.style.left = `${x - 30}px`; tick.style.opacity = String(Math.min(edge, g.guides[i] !== undefined && (i + 1) % every ? 0 : clamp((Math.abs(x - writes) - pill / 2 - (axisW[i] || 40) / 2 - 4) / 14)));
      });
      // The names start a little right of the heads, so a row's star stands before its name, not on its edge.
      rowsEl.querySelectorAll<HTMLElement>('.bw-row').forEach((el, i) => { if (!el.classList.contains('end')) el.style.left = `${g.x1 + 12}px`; el.style.opacity = String(1 - Math.min(.4, Math.abs(i - focus.value) * .1)); });
      renderWords();
      if (skyOn && t < revealUntil) revealSelection();
    }
    starsEl.inert = skyOn; starsEl.style.pointerEvents = skyOn ? 'none' : '';
    her.frame(t, dt);
  }
  pull.addEventListener('click', () => { if (!skyOn) { if (!hooks.back?.()) openSky(); } else { closeSky(); settle(); } });
  $('.bw-her', chrome).addEventListener('pointerenter', () => her.hover = true); $('.bw-her', chrome).addEventListener('pointerleave', () => her.hover = false);
  $('.bw-her', chrome).addEventListener('pointerdown', () => her.pressed = true);
  addEventListener('pointerup', () => her.pressed = false);
  // Her eyes follow the pointer, but only so far round: past that they would turn to the back of her glass and she would
  // show no face.
  win.addEventListener('pointermove', e => { const r = win.getBoundingClientRect(); her.look = [clamp((e.clientX - r.left - width + 34) / 140, -.8, .8), clamp((e.clientY - r.top - 28) / 140, -.8, .8)]; pointerAt = performance.now(); });
  win.addEventListener('pointerleave', () => { her.look = null; her.pressed = false; });
  // The lines under the sessions: the folds open and close here; 新会话 and 拿回来 are the page's acts (data-act).
  rowsEl.addEventListener('click', e => {
    const x = (e.target as HTMLElement).closest<HTMLElement>('[data-x]')?.dataset.x;
    if (!x) return;
    if (x === 'x:new') { closeSky(); return; }
    if (x === 'x:more') openMore = !openMore;
    selected = x; refresh();
    // Taken back, it is a session again: stand on its name once the page has it.
    if (x.startsWith('x:a:')) window.setTimeout(() => { if (!skyOn) return; selected = x.slice(4); nameStop = true; latest(); refresh(); renderRows(); renderWords(); });
  });
  skyEl.addEventListener('wheel', () => revealUntil = 0, { passive: true });
  // Sideways (or with ⇧) the sky slides through time: toward what is older, and back to now.
  win.addEventListener('wheel', e => {
    if (!skyOn || e.clientY - win.getBoundingClientRect().top > 56 + skyEl.clientHeight) return;
    const dx = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.shiftKey ? e.deltaY : 0, g = geo();
    if (!dx || !g.panMax) return;
    e.preventDefault();
    const to = clamp(pan.value - dx, 0, g.panMax); needle.value += to - pan.value; panTo = pan.value = to; pan.velocity = 0;
    if (hoverQi !== null) { hoverQi = null; renderWords(); }
  }, { passive: false });
  for (const el of [rowsEl, starsEl]) el.addEventListener('click', e => {
    const id = (e.target as HTMLElement).closest<HTMLElement>('[data-session]')?.dataset.session; if (!id) return;
    // A found session opens where it was found.
    if (el === rowsEl && searching()) { selected = id; latest(); findStop(); enter(); return; }
    peek.hidden = true; closeSky(); hooks.open(id);
  });
  starsEl.addEventListener('pointerover', e => { const target = (e.target as HTMLElement).closest<HTMLElement>('[data-session]'), s = target && get(target.dataset.session!); if (!s) return; hoverStar = s.id; peek.innerHTML = `<b>${esc(s.title)}</b>${stateHTML(s, 'span')}<small>${esc(s.summary)}</small>`; peek.style.left = `${clamp(target.offsetLeft + 10 - 110, 12, width - 250)}px`; peek.hidden = false; });
  starsEl.addEventListener('pointerout', e => { if (!(e.relatedTarget as Element | null)?.closest?.('.bw-stars button')) { hoverStar = ''; peek.hidden = true; } });
  // What the pointer is on in the sky, with the prototype's reach: a row within half its height of its (bent) line,
  // one of your points within 7 px across and 13 px of its tick.
  function hit(e: MouseEvent) {
    if ((e.target as HTMLElement).closest('button,.bw-open')) return null;
    const rect = win.getBoundingClientRect(), x = e.clientX - rect.left, y = e.clientY - rect.top + skyEl.scrollTop, g = geo();
    if (y <= 56 || y >= 56 + skyHeight() - 30 || x < g.x0 - 10 || x > g.x1) return null;
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
  // On the window, so the keys still arrive while a request holds the composer shut and nothing inside has focus.
  addEventListener('keydown', keyboard, true);
  win.addEventListener('compositionstart', e => { if (skyOn && e.target !== findInput) { closeSky(); settle(); } }, true);
  findInput.addEventListener('input', e => { if (!(e as InputEvent).isComposing) onFind(); });
  findInput.addEventListener('compositionend', onFind);
  // The top right: the queue by her, her list and her one line (waiting.ts).
  const waiting = mountWaiting({
    win, chrome, ta, her, sessions: () => hooks.sessions(), current: () => hooks.current(), rows: () => rows, width: () => width,
    open: id => hooks.open(id), settle, call: (route, body) => hooks.call(route, body), toast: text => hooks.toast(text), cue: (name, gain) => hooks.cue(name, gain),
    refresh: () => { hooks.refresh(); refresh(); }, menu: (html, at) => hooks.menu(html, at), closeMenu: () => hooks.closeMenu(),
    changed: id => hooks.changed(id), sky: () => skyOn, closeSky,
  });
  window.agents?.onNext?.(waiting.next);
  new ResizeObserver(() => { width = win.clientWidth; height = win.clientHeight; rowKey = ''; renderRows(); if (skyOn) renderWords(); }).observe(win);
  pullLabel(); refresh(); setMode(enabled);
  return { invalidate(id: string) { sources.delete(id); refresh(); }, get enabled() { return enabled; }, get busy() { return skyOn; }, refresh, frame,
    sent() { her.say('31', 1100); waiting.sent(); },
    notify(s: Sess) {
      if (!enabled) return;
      // Another session came to need you, stopped or finished: she only glances at its star; one that blocks falls into
      // the queue by her (waiting.ts).
      if (s.id !== hooks.current() && (s.st === 'wait' || s.st === 'err' || s.st === 'done')) glance = { id: s.id, until: performance.now() + 1600 };
      if (s.st === 'err' || waitMin(s) >= 10) her.say(s.st === 'err' ? '34' : 'ask', 1800);
      waiting.notify(s);
      refresh();
    },
    // The composer's hint for ⌥↓, with nothing written.
    hint: () => ta.value ? '' : '<span><kbd>⌥</kbd><kbd>↓</kbd>下一个等你的</span>',
  };
}

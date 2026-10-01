// The top right: who waits on you, in queue.ts's one order (asks, then errors, then finished ones you have not read;
// the longest waiting first within each), gathered by her. A · 落星排队: each waiting star falls from its place on the
// horizon into a line left of her, the next one nearest her. B · 北极星: the same queue as rings round her, a turn an
// hour. Clicking her (or ⌥↓) opens the next one; pointing at her lists them all; a right click puts one aside
// (先放着); when you pause she says how many wait. The horizon ends where the queue begins, so the resting place of
// every star on it is laid out here too, as springs, so a star slides or falls to a new place instead of jumping.
import type { Sess } from '../../../electron/agents/types';
import type { Feature, PageCtx } from '../ctx';
import { queue, waitingSince, waitOf, type Wait } from '../queue';
import { COL, glyph, rgba, type C3, type St } from './glyph';
import type { Her } from './her';
import { clamp, dpr, esc, hash, lerp, reduced, spring, step, type Spring } from './motion';
import './waiting.css';

type Env = {
  win: HTMLElement; chrome: HTMLElement; ta: HTMLTextAreaElement; her: Her;
  sessions(): Sess[]; current(): string; rows(): Sess[]; width(): number;
  open(id: string): void; settle(): void; call(route: string, body?: unknown): Promise<unknown>;
  toast(text: string): void; cue(name: string, gain?: number): void; refresh(): void;
  menu(html: string, at: HTMLElement | { x: number; y: number }): void; closeMenu(): void;
  // when a session last changed state (performance.now) · the long exposure open, and closing it
  changed(id: string): number; sky(): boolean; closeSky(): void;
};
export type Look = 'A' | 'B';
const COLOR: Record<Wait, C3> = { ask: COL.wait, err: COL.err, done: COL.done };
const STAR: Record<Wait, St> = { ask: 'wait', err: 'err', done: 'done' };
// A new one falls in with its own kit sound, softly: the notch's ask, error or done.
const CUE: Record<Wait, string> = { ask: 'ask', err: 'error', done: 'done' };
// One line for every star up here, level with her centre: the horizon's, then the queue's. A: the queue on it, evenly
// spaced, the next one 76 px in from the edge (clear of her glow) · B: the rings round her, the innermost just outside
// her glass. A star coming to wait (or going back once read) rises off the line as it travels (LIFT) and settles into
// its place, so it never crosses the others; they ease aside along the line.
const BASE = 28, LIFT = 15;
const QA = { max: 6, dx: 26, right: 76 }, QB = { max: 4, r0: 19, dr: 3, right: 34, y: BASE };
// The rings' canvas, in css px from the window's top right corner (its size is in waiting.css too).
const RING = { w: 100, h: 72 };
const LIVELY = 'linear(0,.045,.153,.29,.433,.568,.687,.786,.864,.924,.967,.996,1.014,1.024,1.028,1.028,1.026,1.022,1.018,1.014,1.011,1.008,1.005,1.003,1)';
// the page's own 先放着 mark: a clock
const PARK_ICON = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="5.5"/><path d="M8 5v3.2l2 1.3"/></svg>';
const X_ICON = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/></svg>';
const waited = (s: Sess) => Math.max(0, (Date.now() - waitingSince(s)) / 60000);
const mins = (m: number) => m < 1 ? '刚刚' : m < 60 ? `${Math.round(m)} 分` : `${+(m / 60).toFixed(1)} 小时`;
// What each waits with, in one line: the host already words a request (reqLine) and an error in the summary.
const lineOf = (s: Sess) => waitOf(s) === 'done' ? `做完了：${s.summary}` : s.summary;
// Its tail: longer the longer it has waited, full length at an hour, always short of the star behind it.
const tail = (s: Sess) => 5 + 10 * clamp(Math.log2(1 + waited(s)) / Math.log2(61));
const anim = (el: Element, frames: Keyframe[], ms: number, easing = 'cubic-bezier(.2,.8,.2,1)') => reduced.matches ? null : el.animate(frames, { duration: ms, easing });

export function mountWaiting(env: Env) {
  const listEl = document.createElement('div'), sayEl = document.createElement('div'), ringCv = document.createElement('canvas');
  listEl.className = 'bw-hq'; listEl.hidden = true; listEl.setAttribute('aria-label', '等你的');
  sayEl.className = 'bw-say'; sayEl.hidden = true; sayEl.setAttribute('role', 'status');
  // B's rings pass over her own glow, so they have a small canvas of their own above her.
  ringCv.className = 'bw-qb'; ringCv.setAttribute('aria-hidden', 'true');
  env.chrome.append(ringCv, listEl, sayEl);
  const ring = ringCv.getContext('2d')!;
  const herEl = env.chrome.querySelector<HTMLElement>('.bw-her')!, starsEl = env.chrome.querySelector<HTMLElement>('.bw-stars')!;
  let look: Look = localStorage.getItem('agents.queue') === 'B' ? 'B' : 'A';
  const X = new Map<string, Spring>(), Y = new Map<string, Spring>(), trails = new Map<string, [number, number, number][]>();
  // coming: the ones travelling between the horizon and the line, lifted off it until nearly there · moreN, moreAt: +N,
  // and when it appeared
  const coming = new Set<string>();
  let moreN = 0, moreAt = -Infinity;
  // slots: where each row's star rests while the sky is closed · line: the queue as drawn (the rows' waiting ones)
  let slots = new Map<string, [number, number]>(), slotKey = '', line: Sess[] = [], place = new Map<string, number>(), seen: Set<string> | null = null;
  // over: the list row under the pointer, whose star lifts · listKey: what the open list shows
  let over = '', listKey = '', listT = 0, sayT = 0, saidAt = -Infinity, typedAt = -Infinity, blurAt = 0;
  // What changed while the window was away, by session: its last state then.
  const away = new Map<string, Sess['st']>();
  const others = () => queue(env.sessions()).filter(s => s.id !== env.current());
  const menuOpen = () => !!env.win.querySelector('.pop.on');
  const at = (id: string): [number, number] => { const x = X.get(id), y = Y.get(id); return x && y ? [x.value, y.value] : slots.get(id) ?? [env.width() - 84, BASE]; };
  // The one she opens next: the first waiting that is not the one on screen.
  const nextId = () => line.find(s => s.id !== env.current())?.id ?? '';

  // ---------- where the stars rest ----------
  // Past the line the rest wait where +N stands, one place further out.
  const lineX = (j: number, w: number) => w - QA.right - Math.min(j, QA.max) * QA.dx;
  // Each ring's head: its arc starts at twelve o'clock when the wait began and runs clockwise, a full turn an hour.
  function ringHead(s: Sess, j: number, w: number): [number, number] {
    const cx = w - QB.right;
    if (j >= QB.max) return [cx - QB.r0, QB.y];
    const r = QB.r0 + j * QB.dr, a = -Math.PI / 2 + Math.PI * 2 * Math.max(.02, clamp(waited(s) / 60));
    return [cx + Math.cos(a) * r, QB.y + Math.sin(a) * r];
  }
  // Whether the resting places changed.
  function layout() {
    // A finished one you open is being read: it goes back to the horizon at once, not when the page marks it read.
    // The rows as the page has them now: the long exposure refreshes its own copy only once a second.
    const fresh = new Map(env.sessions().map(s => [s.id, s])), rows = env.rows().map(s => fresh.get(s.id) ?? s);
    const w = env.width(), cur = env.current(), q = queue(rows).filter(s => s.id !== cur || waitOf(s) !== 'done');
    const key = `${look}|${w}|${cur}|${look === 'B' ? Math.floor(Date.now() / 60000) : ''}|${rows.map(s => s.id + (q.includes(s) ? `.${q.indexOf(s)}${waitOf(s)}` : '')).join(',')}`;
    if (key === slotKey) return false;
    const was = place;
    slotKey = key; line = q; slots = new Map(); place = new Map(q.map((s, j) => [s.id, j]));
    // one that leaves the line goes back up over the others too
    if (!reduced.matches) for (const id of was.keys()) if (!place.has(id) && X.has(id)) coming.add(id);
    q.forEach((s, j) => slots.set(s.id, look === 'B' ? ringHead(s, j, w) : [lineX(j, w), BASE]));
    const calm = rows.filter(s => !place.has(s.id)), n = calm.length;
    // the horizon ends a clear gap before the line, or before its +N
    const right = look === 'B' ? w - QB.right - QB.r0 - QB.dr * (QB.max - 1) - 30 : q.length ? lineX(q.length - 1, w) - 44 : w - 84;
    // 24 px apart, closer when there are many, never further in than the title leaves room for
    const gap = Math.min(24, Math.max(8, (right - 496) / Math.max(1, n - 1)));
    calm.forEach((s, i) => slots.set(s.id, [right - (n - 1 - i) * gap, BASE]));
    arrivals();
    if (!listEl.hidden) renderList();
    return true;
  }
  // A star that just came to wait lets go of the horizon: it rises off the line, travels, and settles into its place,
  // with its sound when you are here to hear it (away, the notice already chimed). One new to the page drops into its
  // place from just above. Nothing moves when the page first reads the list.
  function arrivals() {
    if (seen) for (const s of line) {
      if (seen.has(s.id)) continue;
      if (!reduced.matches) {
        coming.add(s.id);
        const [tx, ty] = slots.get(s.id)!;
        if (!Y.has(s.id)) { X.set(s.id, spring(tx)); Y.set(s.id, spring(ty - LIFT)); }
      }
      if (s.id !== env.current() && document.hasFocus() && performance.now() - env.changed(s.id) < 3000) env.cue(CUE[waitOf(s)!], .5);
    }
    seen = new Set(line.map(s => s.id));
  }

  // ---------- drawing ----------
  // A star that moves leaves a light trail that fades in half a second: the long exposure, in small.
  function trailPoint(id: string, x: number, y: number, t: number) {
    let p = trails.get(id); if (!p) trails.set(id, p = []);
    const l = p.at(-1); if (!l || Math.hypot(l[0] - x, l[1] - y) > .6) p.push([x, y, t]);
    while (p.length && t - p[0][2] > 480) p.shift();
    return p;
  }
  function drawTrail(c: CanvasRenderingContext2D, p: [number, number, number][], col: C3, t: number) {
    if (reduced.matches || p.length < 2) return;
    c.save(); c.lineCap = 'round';
    for (let k = 1; k < p.length; k++) {
      const age = clamp((t - p[k][2]) / 480);
      c.strokeStyle = rgba(col, .55 * (1 - age) * (k / p.length), .35); c.lineWidth = 1.6 * (1 - age * .6);
      c.beginPath(); c.moveTo(p[k - 1][0], p[k - 1][1]); c.lineTo(p[k][0], p[k][1]); c.stroke();
    }
    c.restore();
  }
  function number(c: CanvasRenderingContext2D, j: number, x: number, y: number) {
    c.save(); c.fillStyle = 'rgba(214,224,255,.78)'; c.font = '600 8.5px "IBM Plex Mono", ui-monospace, monospace'; c.textAlign = 'center'; c.textBaseline = 'middle';
    c.fillText(String(j + 1), x, y); c.restore();
  }
  // +N: the ones past the line, where the next of them would stand; it fades in when it first appears.
  function more(c: CanvasRenderingContext2D, n: number, x: number, y: number, t: number, align: CanvasTextAlign = 'center') {
    if (!moreN) moreAt = t;
    moreN = n;
    const k = reduced.matches ? 1 : clamp((t - moreAt) / 320);
    c.save(); c.globalAlpha *= k; c.fillStyle = 'rgba(214,224,255,.7)'; c.font = '600 9px "IBM Plex Mono", ui-monospace, monospace'; c.textAlign = align; c.textBaseline = 'middle';
    c.fillText(`+${n}`, x, y + .5); c.restore();
  }
  // A soft glow under a star: the one she will open next breathes in it; the one you point at glows brighter.
  function glow(c: CanvasRenderingContext2D, x: number, y: number, col: C3, t: number, a: number, breathe: boolean) {
    const k = reduced.matches || !breathe ? .5 : .5 + .5 * Math.sin(t / 1000 * 1.9), r = 10.5 + 1.2 * k, A = a * (.75 + .25 * k);
    const g = c.createRadialGradient(x, y, 0, x, y, r);
    g.addColorStop(0, rgba(col, A, .2)); g.addColorStop(.45, rgba(col, A * .4, .2)); g.addColorStop(1, rgba(col, 0, .2));
    c.save(); c.fillStyle = g; c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.fill(); c.restore();
  }
  function star(c: CanvasRenderingContext2D, s: Sess, x: number, y: number, t: number, lift: number, scale: number, on: boolean) {
    c.save(); c.translate(x, y);
    glyph(c, STAR[waitOf(s)!], { lit: s.id === env.current(), t: t / 1000 + hash(s.id) * 9, since: (t - env.changed(s.id)) / 1000, hover: on, calm: reduced.matches, lift, scale });
    c.restore();
  }
  function drawLine(c: CanvasRenderingContext2D, t: number, hover: string) {
    // the numbers, while her list is open, are the list's
    const q = line, w = env.width(), nums = !listEl.hidden, next = nextId(), order = nums ? queue(env.sessions()).map(s => s.id) : [];
    for (let j = q.length - 1; j >= 0; j--) {
      const s = q[j], [x, y] = at(s.id), col = COLOR[waitOf(s)!], L = tail(s), tx = lineX(j, w);
      // past the line: one on its way out to +N fades as it gets there (one coming back fades in); the rest are +N
      const fade = j < QA.max ? 1 : clamp((Math.abs(x - tx) + Math.abs(y - BASE)) / QA.dx);
      if (fade < .02) continue;
      c.save(); c.globalAlpha *= fade;
      const settled = Math.abs(y - BASE) < 1.5 && Math.abs(x - tx) < 1.5, on = hover === s.id || over === s.id, first = s.id === next;
      // only one on its way in leaves a trail; the others just slide along the line
      if (coming.has(s.id)) drawTrail(c, trailPoint(s.id, x, y, t), col, t);
      else { const p = trails.get(s.id); if (p) { while (p.length && t - p[0][2] > 480) p.shift(); if (p.length > 1) drawTrail(c, p, col, t); else trails.delete(s.id); } }
      // its tail: a thin fading line back along the line, longer the longer it has waited
      const x0 = x - 3, g = c.createLinearGradient(x0, y, x0 - L, y);
      g.addColorStop(0, rgba(col, on ? .6 : .45, .25)); g.addColorStop(1, rgba(col, 0, .25));
      c.save(); c.strokeStyle = g; c.lineWidth = .9; c.lineCap = 'round'; c.beginPath(); c.moveTo(x0, y); c.lineTo(x0 - L, y); c.stroke(); c.restore();
      if (first || on) glow(c, x, y, col, t, on ? .45 : .38, !on);
      star(c, s, x, y, t, on ? 1.7 : 1.35, (first ? 1.18 : .95) * (on ? 1.12 : 1), on);
      if (nums && settled && j < QA.max) number(c, order.indexOf(s.id), x, y + 13);
      c.restore();
    }
    if (q.length > QA.max) more(c, q.length - QA.max, lineX(QA.max, w), BASE, t); else moreN = 0;
  }
  // o: the rings' own canvas, over her · c: the window's, for a star still on its way in from the horizon
  function drawRings(o: CanvasRenderingContext2D, c: CanvasRenderingContext2D, t: number, hover: string) {
    const q = line, n = Math.min(q.length, QB.max), w = env.width(), cx = w - QB.right, cy = QB.y, nums = !listEl.hidden, order = nums ? queue(env.sessions()).map(s => s.id) : [];
    // the orbits in use, faint, and a tick at twelve: where every wait begins
    o.save(); o.lineWidth = .6;
    for (let j = 0; j < n; j++) { o.strokeStyle = 'rgba(157,180,255,.1)'; o.beginPath(); o.arc(cx, cy, QB.r0 + j * QB.dr, 0, Math.PI * 2); o.stroke(); }
    const rt = QB.r0 + (n - 1) * QB.dr; o.strokeStyle = 'rgba(214,224,255,.3)'; o.lineWidth = 1;
    o.beginPath(); o.moveTo(cx, cy - QB.r0 + 1.5); o.lineTo(cx, cy - rt - 2.5); o.stroke();
    o.restore();
    for (let j = n - 1; j >= 0; j--) {
      const s = q[j], r = QB.r0 + j * QB.dr, col = COLOR[waitOf(s)!], k = Math.max(.02, clamp(waited(s) / 60)), on = hover === s.id || over === s.id;
      const a0 = -Math.PI / 2, a1 = a0 + Math.PI * 2 * k, hx = cx + Math.cos(a1) * r, hy = cy + Math.sin(a1) * r;
      // a star still on its way in draws where it is, with its light trail; its ring fades in as it arrives
      const [sx, sy] = at(s.id), far = Math.hypot(sx - hx, sy - hy) > 3;
      drawTrail(far ? c : o, trailPoint(s.id, far ? sx : hx, far ? sy : hy, t), col, t);
      const N = Math.max(6, Math.round(28 * k));
      o.save(); o.lineCap = 'round'; o.lineWidth = (j === 0 ? 1.7 : 1.25) * (on ? 1.4 : 1);
      for (let m = 0; m < N; m++) {
        const u1 = (m + 1) / N;
        o.strokeStyle = rgba(col, (.1 + .72 * u1 * u1) * (j === 0 || on ? 1 : .78) * (far ? .25 : 1), .2);
        o.beginPath(); o.arc(cx, cy, r, lerp(a0, a1, m / N), lerp(a0, a1, u1) + .002); o.stroke();
      }
      o.restore();
      star(far ? c : o, s, far ? sx : hx, far ? sy : hy, t, on ? 1.8 : 1.45, far ? .9 : (j === 0 ? .66 : .52) * (on ? 1.3 : 1), on);
      if (nums && !far) number(o, order.indexOf(s.id), hx + Math.cos(a1) * 7.5, hy + Math.sin(a1) * 7.5);
    }
    // +N: just outside the outermost ring, on the line
    if (q.length > QB.max) more(o, q.length - QB.max, cx - QB.r0 - QB.dr * (QB.max - 1) - 5, cy, t, 'right'); else moreN = 0;
  }

  // ---------- going there, and putting one aside ----------
  function go(id: string) {
    hideList(); hideSay();
    if (env.sky()) env.closeSky();
    env.open(id); env.settle();
  }
  // The first in the queue that is not the one on screen.
  function next() {
    const d = others()[0];
    hideList(); hideSay();
    if (!d) { env.toast('没有别的等你的了'); env.her.say('fin', 1100); return; }
    env.her.hop(.16); go(d.id);
  }
  function park(id: string) {
    const s = env.sessions().find(x => x.id === id); if (!s) return;
    hideList(); s.parked = true; env.cue('close', .6); env.toast(`先放着：${s.title}`); env.refresh();
    env.call(`/sessions/${id}/meta`, { parked: true }).catch(e => { s.parked = false; env.toast(e instanceof Error ? e.message : String(e)); env.refresh(); });
  }
  function setLook(k: Look) {
    if (look === k) return;
    look = k; slotKey = ''; localStorage.setItem('agents.queue', k); env.cue('mic', .45);
  }
  const point = (e: MouseEvent) => { const r = env.win.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  // row: a row of her list, which the menu stands beside (left of the list, level with the row) instead of over.
  function aside(id: string, e: MouseEvent, row?: HTMLElement) {
    e.preventDefault(); clearTimeout(listT);
    env.menu(`<button type="button" data-q="open" data-id="${esc(id)}">打开</button><button type="button" data-q="park" data-id="${esc(id)}">${PARK_ICON}先放着</button>`, point(e));
    const pop = env.win.querySelector<HTMLElement>('.pop.on');
    if (!row || !pop) return;
    const w = env.win.getBoundingClientRect(), l = listEl.getBoundingClientRect(), r = row.getBoundingClientRect();
    Object.assign(pop.style, { left: `${Math.max(8, l.left - w.left - pop.offsetWidth - 6)}px`, top: `${clamp(r.top - w.top, 8, w.height - pop.offsetHeight - 8)}px`, transformOrigin: '100% 0' });
  }

  // ---------- the list, while you point at her ----------
  function renderList() {
    const q = queue(env.sessions());
    const key = q.map(s => `${s.id}|${s.title}|${waitOf(s)}|${lineOf(s)}|${mins(waited(s))}`).join(';') + `|${env.current()}`;
    if (key === listKey) return; listKey = key;
    listEl.innerHTML = q.length ? q.map((s, j) => `<button type="button" class="hq-r${s.id === env.current() ? ' on' : ''}" data-id="${esc(s.id)}"><i class="hq-n">${j + 1}</i><b class="hq-g q-${waitOf(s)}" aria-hidden="true">✦</b>`
      + `<span class="hq-t"><b>${esc(s.title)}</b><small>${esc(lineOf(s))}</small></span><em>${mins(waited(s))}</em></button>`).join('') : '<p class="hq-none">没有等你的</p>';
  }
  function showList() {
    listKey = ''; renderList(); hideSay();
    if (!listEl.hidden) return;
    listEl.hidden = false;
    anim(listEl, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], 140);
  }
  function hideList() { clearTimeout(listT); listEl.hidden = true; over = ''; }
  // A menu opened from the list keeps it while it is up.
  function hideSoon() { if (menuOpen()) { listT = window.setTimeout(hideSoon, 300); return; } hideList(); }

  // ---------- her one line, at a pause ----------
  function hideSay() { clearTimeout(sayT); if (sayEl.hidden) return; sayEl.hidden = true; env.her.surface(false); }
  // Under her, or, with a sheet open on the right, under the sheet's header and the view switch at the top of its body
  // (open.css .op-seg), so their buttons stay in reach.
  function placeSay() {
    const sh = env.win.querySelector<HTMLElement>('.pv:not(.off) .sh'), w = env.win.getBoundingClientRect(), r = sh?.getBoundingClientRect();
    const top = r && r.height && r.right > w.right - 330 ? Math.max(58, Math.round(r.bottom - w.top + 42)) : 58;
    if (sayEl.style.top !== `${top}px`) sayEl.style.top = `${top}px`;
  }
  function say(lead: string, first: Sess, ms: number) {
    sayEl.innerHTML = `<button type="button" class="say-go" data-id="${esc(first.id)}"><span class="say-l">${esc(lead)}</span><span class="say-f"><b class="q-${waitOf(first)}" aria-hidden="true">✦</b>`
      + `<em>${esc(first.title)}</em><i>${mins(waited(first))}</i><small>${esc(lineOf(first))}</small></span></button><button type="button" class="say-x" aria-label="收起">${X_ICON}</button>`;
    hideList(); sayEl.hidden = false; placeSay();
    // She surfaces from her glass; her words follow.
    env.her.surface(true); env.her.say('ask', 1100); env.her.hop(.08); env.cue('msg', .5);
    anim(sayEl, [{ opacity: 0, transform: 'translateY(-6px) scale(.96)' }, { opacity: 1, transform: 'none' }], 240, LIVELY);
    clearTimeout(sayT); sayT = window.setTimeout(hideSay, ms); saidAt = Date.now();
  }
  // A pause: you just sent, a turn here just ended, or you came back. At most one line a minute, never over your
  // typing (none since `quiet`: a second and a half ago, or the moment you sent), never over the sky, a menu or the
  // list; `lead` words it from how many wait.
  function paused(lead = (n: number) => `${n} 个等你`, ms = 7000, quiet = performance.now() - 1500) {
    const q = others();
    if (!q.length || env.sky() || menuOpen() || !listEl.hidden) return false;
    if (Date.now() - saidAt < 60000 || typedAt > quiet) return false;
    say(lead(q.length), q[0], ms);
    return true;
  }
  // Back after a minute or more: what happened meanwhile, among the ones still waiting.
  function back() {
    const n = { done: 0, err: 0, wait: 0 };
    for (const [id, st] of away) {
      const s = env.sessions().find(x => x.id === id);
      if (s && waitOf(s) && (st === 'done' || st === 'err' || st === 'wait') && s.st === st) n[st]++;
    }
    away.clear();
    const bits = [n.done && `${n.done} 个做完`, n.err && `${n.err} 个出错`, n.wait && `${n.wait} 个要你批`].filter(Boolean);
    return paused(q => bits.length ? `你不在时 ${bits.join(' · ')}` : `你不在时没有新的 · ${q} 个等你`, 9000);
  }

  // ---------- wiring ----------
  herEl.setAttribute('aria-label', 'Jarvis：下一个等你的');
  herEl.addEventListener('click', next);
  // A click on her keeps focus where it was, so nothing later hands focus back to her (a slip thrown after it would,
  // and her focus ring would show); the keyboard still reaches her with Tab.
  herEl.addEventListener('mousedown', e => e.preventDefault());
  // A short intent delay: pointing at her lists them at once, passing over her does not.
  herEl.addEventListener('pointerenter', () => { clearTimeout(listT); listT = window.setTimeout(() => { if (!env.sky() && !menuOpen()) showList(); }, 150); });
  herEl.addEventListener('pointerleave', e => { clearTimeout(listT); if (!listEl.contains(e.relatedTarget as Node | null)) listT = window.setTimeout(hideSoon, 260); });
  // Right-click her: how the queue is drawn.
  herEl.addEventListener('contextmenu', e => {
    e.preventDefault(); hideList();
    env.menu(`<span class="ph">等你的</span><button type="button" data-q="look" data-v="A"${look === 'A' ? ' class="on"' : ''}>落星排队</button><button type="button" data-q="look" data-v="B"${look === 'B' ? ' class="on"' : ''}>北极星</button>`, point(e));
  });
  listEl.addEventListener('pointerenter', () => clearTimeout(listT));
  listEl.addEventListener('pointerleave', e => { over = ''; if (!herEl.contains(e.relatedTarget as Node | null)) listT = window.setTimeout(hideSoon, 260); });
  // a row you point at lifts its star, so the list and the line read as one
  listEl.addEventListener('pointerover', e => { over = (e.target as HTMLElement).closest<HTMLElement>('.hq-r')?.dataset.id ?? ''; });
  listEl.addEventListener('click', e => { const id = (e.target as HTMLElement).closest<HTMLElement>('.hq-r')?.dataset.id; if (id) go(id); });
  listEl.addEventListener('contextmenu', e => { const row = (e.target as HTMLElement).closest<HTMLElement>('.hq-r'), id = row?.dataset.id; if (id) aside(id, e, row!); });
  starsEl.addEventListener('contextmenu', e => { const id = (e.target as HTMLElement).closest<HTMLElement>('[data-session]')?.dataset.session; if (id && place.has(id)) aside(id, e); });
  sayEl.addEventListener('click', e => {
    const el = e.target as HTMLElement, id = el.closest<HTMLElement>('.say-go')?.dataset.id;
    if (el.closest('.say-x') || !id) hideSay(); else go(id);
  });
  // The menus' lines are the page's one popover; theirs are the ones marked data-q.
  env.win.addEventListener('click', e => {
    const b = (e.target as HTMLElement).closest<HTMLElement>('.pop [data-q]'); if (!b) return;
    env.closeMenu();
    if (b.dataset.q === 'look') setLook(b.dataset.v === 'B' ? 'B' : 'A');
    else if (b.dataset.q === 'open') go(b.dataset.id!);
    else if (b.dataset.q === 'park') park(b.dataset.id!);
  });
  env.ta.addEventListener('input', () => { typedAt = performance.now(); hideSay(); });
  addEventListener('blur', e => { if (e.target !== window) return; blurAt = Date.now(); away.clear(); hideSay(); hideList(); });
  addEventListener('focus', e => {
    if (e.target !== window || !blurAt) return;
    const gone = Date.now() - blurAt; blurAt = 0;
    window.setTimeout(() => { if (gone >= 60000) back(); else paused(); }, 600);
  });

  return {
    get look() { return look; },
    get key() { return slotKey; },
    next, esc() { if (!sayEl.hidden) { hideSay(); return true; } if (!listEl.hidden) { hideList(); return true; } return false; },
    hide() { hideSay(); hideList(); },
    // Each frame: the resting places, and every star's spring toward its own; true when the places changed.
    move(dt: number) {
      const changed = layout(), rows = env.rows(), ids = new Set(rows.map(s => s.id));
      for (const id of [...X.keys()]) if (!ids.has(id)) { X.delete(id); Y.delete(id); trails.delete(id); }
      for (const id of coming) if (!ids.has(id)) coming.delete(id);
      for (const s of rows) {
        const [tx, ty] = slots.get(s.id) ?? [env.width() - 84, BASE], q = place.has(s.id);
        let x = X.get(s.id), y = Y.get(s.id);
        if (!x || !y) { X.set(s.id, x = spring(tx)); Y.set(s.id, y = spring(ty)); }
        // on its way in, it keeps above the line until it is nearly over its place, then settles into it
        const up = look === 'A' && coming.has(s.id) && Math.abs(x.value - tx) > 14;
        step(x, tx, q ? 2 : 2.4, .9, dt); step(y, up ? ty - LIFT : ty, q ? 2.4 : 2.6, .82, dt);
        if (coming.has(s.id) && !up && Math.abs(y.value - ty) < .5 && Math.abs(x.value - tx) < .5) coming.delete(s.id);
      }
      if (!sayEl.hidden) placeSay();
      return changed;
    },
    at,
    // Queued stars are drawn by the queue while the sky is closed.
    queued: (id: string) => place.has(id),
    draw(c: CanvasRenderingContext2D, t: number, alpha: number, hover: string) {
      for (const id of [...trails.keys()]) if (!place.has(id)) trails.delete(id);
      const d = dpr(), W = Math.round(RING.w * d), H = Math.round(RING.h * d), rings = look === 'B' && alpha > .01 && line.length > 0;
      if (ringCv.width !== W || ringCv.height !== H) { ringCv.width = W; ringCv.height = H; }
      ring.setTransform(1, 0, 0, 1, 0, 0); ring.clearRect(0, 0, W, H);
      if (alpha <= .01 || !line.length) return;
      c.save(); c.globalAlpha = alpha;
      if (rings) {
        // the rings' canvas sits at the window's top right corner; it draws in the window's coordinates
        ring.setTransform(d, 0, 0, d, -(env.width() - RING.w) * d, 0); ring.globalAlpha = alpha;
        drawRings(ring, c, t, hover);
      } else drawLine(c, t, hover);
      c.restore();
    },
    // On its way down to her, or back up once read, a horizon star draws a short tail.
    streak(c: CanvasRenderingContext2D, id: string, st: St, x: number, y: number) {
      const vx = X.get(id)?.velocity ?? 0, vy = Y.get(id)?.velocity ?? 0;
      if (reduced.matches || Math.abs(vy) <= 25 || Math.hypot(vx, vy) <= 60) return;
      const g = c.createLinearGradient(x, y, x - vx * .09, y - vy * .09);
      g.addColorStop(0, rgba(COL[st], .6, .3)); g.addColorStop(1, rgba(COL[st], 0, .3));
      c.save(); c.strokeStyle = g; c.lineWidth = 1.7; c.lineCap = 'round'; c.beginPath(); c.moveTo(x, y); c.lineTo(x - vx * .09, y - vy * .09); c.stroke(); c.restore();
    },
    // Where a horizon star's button sits: in the line for a queued one in A; none for one on a ring or past the line.
    place(id: string) {
      const j = place.get(id), [x, y] = slots.get(id) ?? [env.width() - 84, BASE];
      return j !== undefined && (look === 'B' || j >= QA.max) ? null : `left:${x - 10}px;top:${y - 15}px`;
    },
    // At rest her eyes are on the first one waiting.
    lookAt(): [number, number] | null {
      const f = line.find(s => s.id !== env.current()); if (!f) return null;
      const [x, y] = look === 'B' ? ringHead(f, 0, env.width()) : at(f.id);
      return [clamp((x - env.width() + 34) / 50, -.8, .8), clamp((y - 28) / 36, -.8, .8)];
    },
    sent() { const t = performance.now(); window.setTimeout(() => paused(undefined, undefined, t), 1050); },
    notify(s: Sess) {
      if (blurAt) away.set(s.id, s.st);
      // The turn on screen just ended: a pause.
      else if (s.id === env.current() && s.st === 'done' && document.hasFocus()) window.setTimeout(() => paused(), 1200);
    },
  };
}

// The line of a stopped session, right above the composer while it stays stopped: why, 让它接着来 (a message asking it
// to pick up where it stopped) and 在终端里看 (the workbench terminal on the host's log, where the error is in full).
// Requests need nothing here: their cards stand in the conversation with their keys.
const RESUME = '请从刚才出错的地方接着来，先确认当前状态。';
export function mountStopped(ctx: PageCtx): Feature {
  return {
    rows(s) {
      if (s.st !== 'err' || s.term) return '';
      return `<div class="c-stop" role="alert"><p><b>出错了</b>${esc(s.summary || '这一轮没做完')}</p><div class="c-stop-k">`
        + '<button type="button" class="btn warm" data-act="stopgo">让它接着来</button><button type="button" class="btn" data-act="stoplog">在终端里看</button></div></div>';
    },
    act(a) {
      const s = ctx.current();
      if (!s || (a !== 'stopgo' && a !== 'stoplog')) return false;
      if (a === 'stopgo') { ctx.cue('send', .8); void ctx.tryCall(`/sessions/${s.id}/send`, { text: RESUME, files: [] }); }
      else ctx.wb.terminal(true, 'log', '后台');
      return true;
    },
  };
}

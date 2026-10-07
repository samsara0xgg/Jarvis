// ADR 0058: Jarvis's dictation, beside the text caret. The companion slips her into the notch while this page opens
// a hole next to where the words will land; she comes up through it and listens, head tilted, while the daemon
// records; when the polished words are back she dives in and they appear as the hole shuts; then she is home.
// Motion from the 钻过去 lab (artifact GzXa22kH, v3), drawn by the companion's own star core.
import { Core, EXPRESSIONS, isSkin, type ExprId } from './starCore';
import './dictation.css';

type Rect = { l: number; t: number; r: number; b: number };
type Pt = { x: number; y: number };
type Lang = 'zh' | 'en';
// Everything in this window's own points; the window covers the caret's screen.
export type DictationStart = {
  caret: Rect | null; lineRight: number | null; element: Rect | null; pointer: Pt; top: number;
  skin: string; lang: Lang; port: string; trusted: boolean; grantee: string; context: { app: string; window: string; selected: string; before: string };
};
declare global { interface Window { dictation: {
  onStart: (cb: (start: DictationStart) => void) => void;
  onFinish: (cb: (send?: boolean) => void) => void;
  onCancel: (cb: () => void) => void;
  onCursor: (cb: (point: Pt) => void) => void;
  paste: (text: string, send: boolean) => void;
  target: () => Promise<string>;
  copy: (text: string) => void;
  home: (happy: boolean) => void;
  done: () => void;
  passthrough: (on: boolean) => void;
  focus: (on: boolean) => void;
  open: (page: string) => void;
  again: () => void;
} } }

const T = {
  noBox: ['No text box here. I will copy what you say.', '没找到输入框，说完我先帮你复制'],
  noAccess: ['No Accessibility access yet. I will copy what you say.', '还没有辅助功能权限，说完我先帮你复制'],
  miss: ['Didn’t catch that. Again?', '没听清，再说一次？'],
  polish: ['Couldn’t polish it: ', '润色没成功：'],
  rawCopied: ['The raw words are copied.', '原话已经帮你复制'],
  busy: ['Still finishing the last one.', '上一段还没写完'],
  off: ['Jarvis’s voice is off, so dictation can’t listen.', 'Jarvis 的语音没开，听写用不了'],
  offline: ['Can’t reach Jarvis.', '连不上 Jarvis'],
  cardTitle: ['No text box', '没找到输入框'],
  goneTitle: ['The text box is gone', '原来的输入框不在了'],
  noAccessTitle: ['No Accessibility access yet', '还没有辅助功能权限'],
  copied: ['Copied', '已复制'],
  pasteIt: ['Paste it anywhere', '直接粘贴就行'],
  close: ['Close', '关闭'],
  openAccess: ['Open Accessibility settings', '打开辅助功能设置'],
  grantee: ['Turn on “%” in the list.', '在列表里打开“%”'],
  editArmed: ['I’ll let you edit it first.', '写好先给你改'],
  editHint: ['Enter or tap her to paste · Shift+Enter for a new line · Esc to cancel', '回车或点她贴上 · Shift+回车换行 · Esc 取消'],
} satisfies Record<string, [string, string]>;

const TAU = Math.PI * 2, R = 15, M = 6;
const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const smooth = (a: number, b: number, x: number) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
const rgba = (c: number[], a = 1) => `rgba(${Math.round(c[0] * 255)},${Math.round(c[1] * 255)},${Math.round(c[2] * 255)},${a})`;
// Listening: the tilted head (Allen's pick), looking where the words will land.
const X = EXPRESSIONS as Record<string, unknown>;
X.dictate = { ...EXPRESSIONS['35'], gaze: 'free' };

const cv = document.getElementById('fx') as HTMLCanvasElement, ctx = cv.getContext('2d')!;
const hit = document.getElementById('hit')!, bubble = document.getElementById('bubble')!;
let W = innerWidth, H = innerHeight, k = 1;
function fit() {
  W = innerWidth; H = innerHeight; k = Math.min(2, devicePixelRatio || 1);
  cv.width = Math.round(W * k); cv.height = Math.round(H * k);
}
addEventListener('resize', fit); fit();

// ---------- where she comes up: right of the line's text, else right of the text box, else above the caret ----------
type Info = { kind: 'caret' | 'element' | 'none'; caret: Rect | null; element: Rect | null };
let top = 0;
function place(info: Info, lineRight: number | null, pointer: Pt): Pt {
  const fits = (x: number, y: number) => x - R >= M && x + R <= W - M && y - R >= top + M && y + R <= H - M;
  const onScreen = (x: number, y: number): Pt => ({ x: clamp(x, R + M, W - R - M), y: clamp(y, top + R + M, H - R - M) });
  const { caret: c, element: e } = info;
  if (c) {
    const mid = (c.t + c.b) / 2, lx = Math.max(c.l, lineRight ?? c.l) + 10 + R;
    if (fits(lx, mid) && (!e || lx + R <= e.r - 6)) return { x: lx, y: mid };
    if (e && fits(e.r + 10 + R, mid)) return { x: e.r + 10 + R, y: mid };
    const cx = c.l + 8 + R;
    return fits(cx, c.t - 8 - R) ? onScreen(cx, c.t - 8 - R) : onScreen(cx, c.b + 8 + R);
  }
  // A text box that shows Accessibility no caret (a terminal): its bottom-right corner, where nothing is written.
  if (e) return onScreen(e.r - R - 14, e.b - R - 14);
  // Nowhere to write: bottom-right of the pointer, flipped at the edges, like a tooltip.
  let x = pointer.x + 16 + R, y = pointer.y + 20 + R;
  if (x + R > W - M) x = pointer.x - 16 - R;
  if (y + R > H - M) y = pointer.y - 16 - R;
  return onScreen(x, y);
}

// ---------- the hole she goes through: it opens, she passes, it snaps shut with a puff of her light ----------
type Hole = { x: number; y: number; open: number; close: number };
let holes: Hole[] = [];
const FLAT = .3; // a round hole seen from the side
const shut = (h: Hole | null, now: number) => { if (h) h.close = Math.min(h.close, now); };
function dig(x: number, y: number, open: number, close = 1e12) { // 1e12: open until shut
  shut(P.hole, performance.now());
  const h = { x, y, open, close }; holes.push(h);
  return (P.hole = h);
}
const holeR = (h: Hole, now: number) => R * smooth(h.open, h.open + 90, now) * (1 - smooth(h.close, h.close + 110, now));

// ---------- her ----------
type State = 'off' | 'appear' | 'listen' | 'think' | 'edit' | 'leave' | 'miss' | 'error' | 'card' | 'dissolve' | 'cancel' | 'gone';
let core = new Core('glass'), lang: Lang = 'zh';
const t = (pair: [string, string]) => pair[lang === 'zh' ? 1 : 0];
const P = {
  state: 'off' as State, t0: 0, x: 0, y: 0, info: { kind: 'none', caret: null, element: null } as Info,
  lvl: 0, target: 0, hole: null as Hole | null, sinkAt: 0, trusted: true, grantee: '', port: '', session: 0,
  vis: { x: 0, y: 0 }, cursor: { x: -1e4, y: -1e4 }, over: false, downAt: -1,
  // A tap on her once she is listening: the words come back in a box to fix before they go in.
  edit: false,
  // Return finished it (ADR 0175): once the words are pasted, Return goes to the app too.
  send: false,
};
// Beats, in ms.
const APPEAR = 340, PLUNGE = 250, SHAKE = 260;
const active = () => P.state === 'appear' || P.state === 'listen';
const canPaste = () => P.trusted && P.info.kind !== 'none';

// ---------- the daemon: one streamed request per dictation ----------
let abort: AbortController | null = null, opened = false, stopWanted = false, text = '';
const sendStop = () => { void fetch(`http://127.0.0.1:${P.port}/inherent/dictation/stop`, { method: 'POST' }).catch(() => undefined); };
async function record(start: DictationStart, session: number) {
  const ctrl = abort = new AbortController();
  opened = false; stopWanted = false;
  const mine = () => session === P.session;
  try {
    const res = await fetch(`http://127.0.0.1:${start.port}/inherent/dictation`, {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(start.context), signal: ctrl.signal,
    });
    if (!mine()) return;
    if (!res.ok || !res.body) { fail(t(res.status === 409 ? T.busy : res.status === 404 ? T.off : T.offline)); return; }
    opened = true;
    if (stopWanted) sendStop();
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done || !mine()) break;
      buffer += value;
      for (let at = buffer.indexOf('\n'); at >= 0; at = buffer.indexOf('\n')) {
        received(JSON.parse(buffer.slice(0, at)));
        buffer = buffer.slice(at + 1);
      }
    }
    if (mine() && (active() || P.state === 'think')) fail(t(T.offline));
  } catch {
    if (mine() && !ctrl.signal.aborted && (active() || P.state === 'think')) fail(t(T.offline));
  }
}
function received(line: { level?: number; state?: string; text?: string; raw?: string; error?: string }) {
  const now = performance.now();
  if (typeof line.level === 'number') P.target = line.level;
  // The daemon stops by itself after fifteen minutes.
  if (line.state === 'thinking' && active()) { P.state = 'think'; P.t0 = now; }
  if (P.state !== 'think' && !active()) return;
  if (typeof line.error === 'string') {
    if (line.raw) window.dictation.copy(line.raw);
    error(`${t(T.polish)}${line.error}`, line.raw ? t(T.rawCopied) : '');
  } else if (typeof line.text === 'string') outcome(now, line.text);
}

// ---------- the story ----------
function start(s: DictationStart) {
  const now = performance.now();
  hideBubble();
  if (isSkin(s.skin) && s.skin !== core.skin) core = new Core(s.skin);
  lang = s.lang; top = s.top; P.trusted = s.trusted; P.grantee = s.grantee; P.port = s.port; P.session++;
  P.info = { kind: s.caret ? 'caret' : s.element ? 'element' : 'none', caret: s.caret, element: s.element };
  const at = place(P.info, s.lineRight, s.pointer);
  Object.assign(P, { x: at.x, y: at.y, vis: at, state: 'appear', t0: now, lvl: 0, target: 0, hole: null, downAt: -1, edit: false, send: false });
  holes = [];
  // the hole at her feet opens while she is still slipping into the notch
  dig(at.x, at.y + R, now + 90, now + 260);
  if (P.info.kind === 'none') setTimeout(() => {
    if (!active()) return;
    if (P.trusted) showBubble(t(T.noBox), '', 'hint', 2200);
    else showBubble(t(T.noAccess), grantNote(), 'hint', 6000, accessButton());
  }, 450);
  void record(s, P.session);
  loop();
}
function finish(now: number) {
  if (!active()) return;
  P.state = 'think'; P.t0 = now;
  stopWanted = true;
  if (opened) sendStop();
}
function outcome(now: number, words: string) {
  if (!words) { P.state = 'miss'; P.t0 = now; showBubble(t(T.miss), '', 'hint', 1900); return; }
  text = words;
  if (P.edit) { P.state = 'edit'; P.t0 = now; showEditor(words); return; }
  void deliver(now);
}
// ADR 0110, as 言字 0.4.0: the words go in only where the dictation started; that text box gone or another app in
// front, they are copied and shown instead.
async function deliver(now: number) {
  const session = P.session, where = canPaste() ? await window.dictation.target() : 'none';
  if (session !== P.session) return;
  if (where !== 'ok' && where !== 'blind') {
    window.dictation.copy(text); P.state = 'card'; P.t0 = performance.now();
    showCard(text, where === 'none' ? undefined : T.goneTitle);
    return;
  }
  now = performance.now();
  // no pause for a victory lap: she dives at once, and the words come out as the hole shuts
  const c = P.info.caret;
  P.state = 'leave'; P.t0 = now;
  dig(c ? c.l : P.x, c ? Math.max(c.b, P.y + R) : P.y + R, now + 70);
}
// The fixed words go in the way the spoken ones would; nothing left in the box sends nothing.
function submit(now: number) {
  const box = bubble.querySelector('textarea');
  if (P.state !== 'edit' || !box) return;
  text = box.value.trim();
  window.dictation.focus(false); hideBubble();
  if (text) void deliver(now); else dissolve(now);
}
function error(message: string, note: string) { const now = performance.now(); P.state = 'error'; P.t0 = now; showBubble(message, note, 'err', 3000); }
function fail(message: string) { abort?.abort(); error(message, ''); }
// She sinks where she stands: a hole opens at her feet as she crouches.
function sink(at: number) { P.sinkAt = at; dig(P.x, P.y + R, at + 40); }
function cancel(now: number) {
  if (!['appear', 'listen', 'think', 'edit', 'card', 'miss', 'error'].includes(P.state)) return;
  if (P.state === 'edit') window.dictation.focus(false);
  abort?.abort(); P.session++;
  if (P.state !== 'card') core.effect('shake', now);
  const card = P.state === 'card';
  P.state = 'cancel'; P.t0 = now; hideBubble();
  sink(card ? now : now + SHAKE);
}
function dissolve(now: number) { P.state = 'dissolve'; P.t0 = now; sink(now); hideBubble(); }
function goHome(now: number, happy = false) {
  shut(P.hole, now); P.state = 'gone'; P.t0 = now; hideBubble();
  window.dictation.home(happy);
}
// Not when the words were fixed in the box first: that is a message he looked at, not one he sent by voice.
function land(now: number) { window.dictation.paste(text, P.send && !P.edit); goHome(now, true); }

// ---------- bubbles ----------
let bubbleTimer: ReturnType<typeof setTimeout> | undefined;
// Without Accessibility she can only copy: one click takes Allen to the switch in System Settings.
const grantNote = () => P.grantee ? t(T.grantee).replace('%', P.grantee) : '';
function accessButton() {
  const go = document.createElement('button');
  go.type = 'button'; go.className = 'go'; go.textContent = t(T.openAccess);
  go.addEventListener('click', () => window.dictation.open('accessibility'));
  return go;
}
function placeBubble() {
  const b = bubble.getBoundingClientRect();
  let x = P.x + R + 10;
  if (x + b.width > W - 8) x = P.x - R - 10 - b.width;
  bubble.style.left = `${Math.max(8, x)}px`; bubble.style.top = `${clamp(P.y - b.height / 2, top + 8, H - b.height - 8)}px`;
}
function showBubble(message: string, note: string, kind: string, ms: number, action?: HTMLElement) {
  clearTimeout(bubbleTimer);
  bubble.className = `bubble ${kind}`; bubble.replaceChildren(message);
  if (note) { const small = document.createElement('span'); small.textContent = note; bubble.append(document.createElement('br'), small); }
  if (action) bubble.append(document.createElement('br'), action);
  bubble.hidden = false; placeBubble();
  bubbleTimer = setTimeout(hideBubble, ms);
}
function showCard(words: string, title?: [string, string]) {
  clearTimeout(bubbleTimer);
  bubble.className = 'bubble card';
  bubble.innerHTML = '<div class="card-top"><b></b><button type="button" class="x">×</button></div><div class="card-text"></div><div class="card-foot"><span class="copied"></span><span></span></div>';
  bubble.querySelector('b')!.textContent = t(title ?? (P.trusted ? T.cardTitle : T.noAccessTitle));
  bubble.querySelector('.x')!.setAttribute('aria-label', t(T.close));
  bubble.querySelector('.card-text')!.textContent = words;
  const [copied, paste] = bubble.querySelectorAll('.card-foot span');
  copied.textContent = t(T.copied); paste.textContent = t(T.pasteIt);
  bubble.querySelector('.x')!.addEventListener('click', () => dissolve(performance.now()));
  if (!P.trusted) {
    const row = document.createElement('div'); row.className = 'card-foot';
    const note = document.createElement('span'); note.textContent = grantNote();
    row.append(accessButton(), note); bubble.append(row);
  }
  bubble.hidden = false; placeBubble();
  bubbleTimer = setTimeout(() => { if (P.state === 'card') dissolve(performance.now()); }, 8000);
}
// The polished words in a box that has the keyboard; Enter (not while an input method is composing) pastes them.
function showEditor(words: string) {
  clearTimeout(bubbleTimer);
  bubble.className = 'bubble card edit';
  bubble.innerHTML = '<textarea rows="1" spellcheck="false"></textarea><div class="card-foot"><span></span></div>';
  const box = bubble.querySelector('textarea')!;
  bubble.querySelector('.card-foot span')!.textContent = t(T.editHint);
  box.value = words;
  const grow = () => { box.style.height = 'auto'; box.style.height = `${Math.min(box.scrollHeight, 220)}px`; placeBubble(); };
  box.addEventListener('input', grow);
  box.addEventListener('keydown', event => {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submit(performance.now()); }
    else if (event.key === 'Escape') { event.preventDefault(); cancel(performance.now()); }
  });
  bubble.hidden = false; grow();
  window.dictation.focus(true);
  box.focus(); box.setSelectionRange(box.value.length, box.value.length);
}
function hideBubble() { clearTimeout(bubbleTimer); bubble.hidden = true; }

// ---------- clicks: she and her card take the mouse; everything else passes through to the app below ----------
function refreshHit() {
  const p = P.cursor, b = bubble.hidden ? null : bubble.getBoundingClientRect();
  const onHer = ['appear', 'listen', 'think', 'edit', 'card'].includes(P.state) && Math.hypot(p.x - P.vis.x, p.y - P.vis.y) < R + 8;
  const onCard = !!b && (P.state === 'card' || P.state === 'edit' || !!bubble.querySelector('button')) && p.x >= b.left && p.x <= b.right && p.y >= b.top && p.y <= b.bottom;
  const over = onHer || onCard;
  if (over !== P.over) { P.over = over; window.dictation.passthrough(!over); }
}
hit.addEventListener('pointerdown', event => { event.preventDefault(); P.downAt = performance.now(); });
hit.addEventListener('pointerup', () => {
  const now = performance.now();
  if (P.downAt >= 0 && now - P.downAt < 350) {
    if (P.state === 'card') dissolve(now);
    else if (P.state === 'edit') submit(now);
    // Finishing by tapping her, or tapping her while she thinks: the words come back to fix first.
    else if (active() || P.state === 'think') { if (!P.edit) { P.edit = true; showBubble(t(T.editArmed), '', 'hint', 1400); } finish(now); }
  }
  P.downAt = -1;
});

// ---------- drawing ----------
const eyesCv = document.createElement('canvas'), ectx = eyesCv.getContext('2d')!;
// Her eyes at (x, y), drawn apart so one blur gives them their glow.
function drawEyes(x: number, y: number, r: number, sx = 1, sy = 1) {
  const pose = core.pose(1), E = Math.ceil(3.8 * r * k * Math.max(1, sx, sy));
  if (eyesCv.width < E) eyesCv.width = eyesCv.height = E;
  ectx.setTransform(1, 0, 0, 1, 0, 0); ectx.clearRect(0, 0, eyesCv.width, eyesCv.height);
  ectx.setTransform(k, 0, 0, k, E / 2, E / 2); ectx.scale(sx, sy);
  ectx.translate(pose[0] * r, pose[1] * r); ectx.scale(pose[2], pose[3]);
  core.eyes(ectx, r);
  ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0);
  const [glow, blur] = core.glow(); ctx.shadowColor = glow; ctx.shadowBlur = blur * r * k;
  ctx.drawImage(eyesCv, 0, 0, E, E, x * k - E / 2, y * k - E / 2, E, E); ctx.restore();
}
const bodyCv = document.createElement('canvas'), bctx = bodyCv.getContext('2d')!;
// Her glass and inside, drawn off screen at radius r; returns the canvas size, 0 without WebGL.
function paintGlass(r: number) {
  const S = Math.round(2 * 1.3 * r * k), N = S + 4;
  if (bodyCv.width !== N) bodyCv.width = bodyCv.height = N;
  bctx.setTransform(1, 0, 0, 1, 0, 0); bctx.clearRect(0, 0, N, N);
  if (!core.render(S, r * k < 20 ? 2 : 3)) return 0;
  const [jx, jy, bx, by] = core.pose(1);
  bctx.setTransform(k, 0, 0, k, N / 2, N / 2); bctx.translate(jx * r, jy * r); bctx.scale(bx, by);
  core.inside(bctx, r, S); core.glass(bctx, r, S);
  return N;
}
// The solid glass ball, stretched by (sx, sy) around its centre, with a soft shadow under her.
function drawSolid(x: number, y: number, r: number, sx: number, sy: number) {
  const N = paintGlass(r);
  if (N) {
    ctx.save(); ctx.setTransform(k, 0, 0, k, 0, 0);
    const g = ctx.createRadialGradient(x, y + .28 * r, 0, x, y + .28 * r, 1.15 * r);
    g.addColorStop(0, 'rgba(0,0,0,.32)'); g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y + .28 * r, 1.15 * r, 0, TAU); ctx.fill();
    ctx.setTransform(1, 0, 0, 1, x * k, y * k); ctx.scale(sx, sy);
    ctx.drawImage(bodyCv, -N / 2, -N / 2); ctx.restore();
  }
  drawEyes(x, y, r, sx, sy);
}
function spark(x: number, y: number, a: number, color: number[], r: number) {
  ctx.save(); ctx.setTransform(k, 0, 0, k, 0, 0); ctx.shadowColor = rgba(color, 1); ctx.shadowBlur = 9 * k;
  ctx.fillStyle = `rgba(255,255,255,${a})`; ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.fill(); ctx.restore();
}
// The hole's dark mouth and far rim, drawn under her.
function holeBack(h: Hole, now: number, glow: number[]) {
  const r = holeR(h, now);
  if (r < .3) return;
  ctx.save(); ctx.translate(h.x, h.y); ctx.scale(1, FLAT);
  const g = ctx.createRadialGradient(0, 0, 0, 0, 0, r);
  g.addColorStop(0, 'rgba(2,3,8,.96)'); g.addColorStop(.72, 'rgba(6,8,18,.92)'); g.addColorStop(1, rgba(glow, .55));
  ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, r, 0, TAU); ctx.fill(); ctx.restore();
  ctx.save(); ctx.strokeStyle = rgba(glow, .5); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.ellipse(h.x, h.y, r, r * FLAT, 0, Math.PI, TAU); ctx.stroke(); ctx.restore();
}
// As it snaps shut, a puff of her light thrown out from its rim (behind her, so it never lands on her face).
function puff(h: Hole, now: number, glow: number[]) {
  const e = (now - h.close) / 300;
  if (e > 0 && e < 1) for (const a of [-1, -.75, .75, 1]) {
    spark(h.x + a * R * (1.05 + .8 * e), h.y - R * (1.1 * e - .8 * e * e) * (Math.abs(a) < 1 ? 1.4 : .8), 1 - e, glow, 1.3);
  }
}
// The near lip, drawn over her.
function holeFront(h: Hole, now: number, glow: number[]) {
  const r = holeR(h, now);
  if (r < .3) return;
  ctx.save(); ctx.shadowColor = rgba(glow, .9); ctx.shadowBlur = 6 * k; ctx.strokeStyle = rgba(glow, .95); ctx.lineWidth = 1.4;
  ctx.beginPath(); ctx.ellipse(h.x, h.y, r, r * FLAT, 0, 0, Math.PI); ctx.stroke(); ctx.restore();
}
// Everything but the shaft under the hole: below its rim she is hidden, through its mouth she shows.
function clipFloor(h: Hole, now: number) {
  const r = holeR(h, now), w = r > .3 ? 1.15 * R : 0;
  ctx.beginPath(); ctx.rect(0, 0, W, h.y); ctx.rect(0, h.y, h.x - w, H); ctx.rect(h.x + w, h.y, W, H);
  if (r > .3) ctx.ellipse(h.x, h.y, r, r * FLAT, 0, 0, TAU);
  ctx.clip();
}
// A hop and a dive into hole h: crouch, a small arc over to it, then straight down through it.
function plunge(el: number, x0: number, y0: number, h: Hole) {
  if (el < 50) { const c = smooth(0, 50, el); return { x: x0, y: y0 + .06 * R * c, sy: 1 - .16 * c }; }
  const u = clamp((el - 50) / 200, 0, 1), d = h.y + 2.2 * R - y0, hop = 1.6 * R;
  const v = (d - 4 * hop * (1 - 2 * u)) / (Math.abs(d) + 4 * hop);
  return { x: lerp(x0, h.x, smooth(0, .6, u)), y: y0 + d * u - 4 * hop * u * (1 - u), sy: 1 + .32 * Math.abs(v) };
}
// Where she is, how she stretches, and the hole she is passing through.
function body(now: number, el: number) {
  let x = P.x, y = P.y, sy = 1, vis = true, clip: Hole | null = null;
  if (P.state === 'appear') {
    // out of the hole at her feet: a fast stretched rise, a little too high, then down onto her spot
    clip = P.hole; vis = el > 150;
    const u = clamp((el - 150) / 140, 0, 1), v = clamp((el - 290) / 50, 0, 1);
    y += el < 290 ? lerp(2 * R, -.2 * R, 1 - (1 - u) ** 3) : lerp(-.2 * R, 0, v * v);
    sy = el < 290 ? lerp(1.3, 1.04, u) : 1 + .04 * (1 - v);
  } else if (P.state === 'leave' && P.hole) {
    clip = P.hole; ({ x, y, sy } = plunge(el, x, y, P.hole));
  } else if ((P.state === 'cancel' || P.state === 'dissolve') && now >= P.sinkAt && P.hole) {
    clip = P.hole; ({ x, y, sy } = plunge(now - P.sinkAt, x, y, P.hole));
  } else if (P.state === 'gone' || P.state === 'off') vis = false;
  const scale = active() ? 1 + .05 * P.lvl : 1;
  return { x, y, sx: 1 / Math.sqrt(sy), sy, vis, clip, scale };
}
function faceFor(): ExprId {
  switch (P.state) {
    case 'think': return '30';
    case 'leave': return '33';
    case 'miss': return '14';
    case 'error': return '34';
    case 'card': case 'edit': return 'ask';
    default: return 'dictate' as ExprId;
  }
}

// ---------- frame ----------
let frameId = 0, last = 0;
function loop() { if (!frameId) { last = 0; frameId = requestAnimationFrame(frame); } }
function frame() {
  frameId = 0;
  const now = performance.now(), dt = Math.min(last ? (now - last) / 1000 : 1 / 60, 1 / 20), el = now - P.t0;
  last = now;
  // timeline: at most one step per frame
  if (P.state === 'appear' && el > APPEAR) { P.state = 'listen'; core.effect('squash', now); core.blinkStart = now; }
  else if (P.state === 'leave' && el > PLUNGE) land(now);
  else if ((P.state === 'miss' && el > 1900) || (P.state === 'error' && el > 3000)) dissolve(now);
  else if ((P.state === 'dissolve' || P.state === 'cancel') && now > P.sinkAt + PLUNGE) goHome(now);
  else if (P.state === 'gone' && el > 500) { P.state = 'off'; P.over = false; window.dictation.done(); }
  holes = holes.filter(h => now < h.close + 500);
  // her glow follows your voice
  P.lvl += ((active() ? P.target : 0) - P.lvl) * (1 - Math.exp(-dt * 12));
  refreshHit();

  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, cv.width, cv.height);
  if (P.state === 'off') return;
  ctx.setTransform(k, 0, 0, k, 0, 0);
  const c = P.info.caret, cx = c ? c.l : P.vis.x, cy = c ? (c.t + c.b) / 2 : P.vis.y;
  let look: [number, number] | null = null;
  if (c) { const dx = cx - P.vis.x, dy = cy - P.vis.y, d = Math.hypot(dx, dy) || 1; look = [dx / d * .7, dy / d * .7]; }
  core.update(now, dt, { expr: faceFor(), look, still: false, pressed: false });
  const b = body(now, el), glow = core.light.glow, r = R * b.scale;
  P.vis = { x: b.x, y: b.y };
  // where the words will land: a small light at the caret, and a thread to it when she is further off
  if (c && (active() || P.state === 'think' || P.state === 'edit' || P.state === 'leave')) {
    const pulse = .55 + .35 * Math.sin(now / 160);
    ctx.save(); ctx.shadowColor = rgba(glow, .9); ctx.shadowBlur = 8 * k;
    ctx.fillStyle = rgba(glow, pulse); ctx.fillRect(c.l - 1, c.t, 2, c.b - c.t); ctx.restore();
    const d = Math.hypot(cx - b.x, cy - b.y);
    if (d > r * 2.6 && P.state === 'listen') {
      const ux = (cx - b.x) / d, uy = (cy - b.y) / d, g = ctx.createLinearGradient(b.x, b.y, cx, cy);
      g.addColorStop(0, rgba(glow, 0)); g.addColorStop(.35, rgba(glow, .3)); g.addColorStop(1, rgba(glow, .45));
      ctx.strokeStyle = g; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(b.x + ux * (r + 3), b.y + uy * (r + 3)); ctx.lineTo(cx - ux * 3, cy - uy * 3); ctx.stroke();
    }
  }
  for (const h of holes) { holeBack(h, now, glow); puff(h, now, glow); }
  if (b.vis) {
    ctx.save(); if (b.clip) clipFloor(b.clip, now);
    ctx.save(); ctx.translate(b.x, b.y); ctx.scale(b.scale, b.scale); core.orbit(ctx, R, -1); ctx.restore();
    drawSolid(b.x, b.y, r, b.sx, b.sy);
    ctx.save(); ctx.translate(b.x, b.y); ctx.scale(b.scale, b.scale); core.orbit(ctx, R, 1); core.particles(ctx, R, k); ctx.restore();
    ctx.restore();
  }
  for (const h of holes) holeFront(h, now, glow);
  hit.style.transform = `translate(${b.x - R - 6}px, ${b.y - R - 6}px)`;
  frameId = requestAnimationFrame(frame);
}

window.dictation.onStart(start);
// The right ⌥ again: it finishes a listening dictation, pastes the box being fixed, and while her last card or
// message is still up it clears that and starts the next one. While she thinks or dives it waits.
window.dictation.onFinish(send => {
  const now = performance.now();
  if (active()) { P.send = send === true; finish(now); }
  else if (P.state === 'edit') submit(now);
  else if (['card', 'miss', 'error', 'dissolve', 'cancel', 'gone'].includes(P.state)) { hideBubble(); window.dictation.again(); }
});
window.dictation.onCancel(() => cancel(performance.now()));
window.dictation.onCursor(point => { P.cursor = point; });

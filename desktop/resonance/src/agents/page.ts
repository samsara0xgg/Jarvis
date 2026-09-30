// The Agents window (ADR 0073), built from design lab FHrstDSC v2 and v3 (the context popover, and Hermes' button details). It is cut from her glass like the Dashboard, she sits
// at the top of the list and answers what the sessions do, and the sounds are her kit exactly as the notch plays them.
// The sessions themselves run in the agent host; this page draws what the host's event stream says and sends back
// what Allen does. Drawing is batched into one frame, rows and messages are keyed so only what changed is touched,
// each session keeps its own conversation so switching is instant, and only marks that move repaint.
import type { Agent, Catalog, Ctx, Event, File as Upload, Item, Pic, Req, Sess, St, Step, Usage } from '../../electron/agents/types';
import { drawMark } from '../AgentMarks';
import { features, type Own, type PageCtx } from './ctx';
import { waitOf } from './queue';
import { mountSee } from './see';
import { palette, play, scoreOf } from '../soundKit';
import { Core, TAKES, pick, type ExprId } from '../starCore';
import { mountExposure } from './exposure';
import { mountStopped } from './exposure/waiting';
import { mountWorkbench } from './workbench';
import { cardsHTML, editsBefore, inline } from './workbench/refs';
import * as usage from './workbench/usage';
import { mountViewer } from './viewer';
import { mountKeys } from './keys';
import { mountSlip } from './slip';
import { mountHist } from './hist';
import { mountBang } from './bang';
import './agents.css';
import './exposure/exposure.css';

declare global { interface Window { agents?: {
  presence?(enabled: boolean, ids: string[]): void; onNext?(callback: () => void): () => void; onOpen?(callback: (id: string) => void): () => void;
  folder(): Promise<string>; terminal(cwd: string, cmd: string, term?: string): Promise<boolean>; reveal(cwd: string): Promise<void>;
  openUrl?(url: string): Promise<void>; openPath?(file: string): Promise<void>; cloud?(cwd: string, text: string, term?: string): Promise<boolean>;
  terminals?(): Promise<{ id: string; name: string }[]>; revealFile?(file: string): Promise<void>; quickLook?(file: string): Promise<void>;
  editors?(): Promise<{ id: string; name: string }[]>; openInEditor?(file: string, line?: number, editor?: string): Promise<boolean>; pathOf?(file: File): string;
} } }

const $ = <T extends Element = HTMLElement>(s: string, root: ParentNode = document) => root.querySelector(s) as T;
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const DPR = () => Math.min(2, devicePixelRatio || 1);
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
// The app's own curves: the shared spring from companion.css, and a quick ease-out for fades.
const SPRING = 'linear(0,.054,.178,.329,.481,.617,.731,.82,.888,.936,.969,.99,1.003,1.01,1.014,1.015,1.014,1.012,1.01,1.008,1)';
const OUT = 'cubic-bezier(.23,1,.32,1)';
const anim = (el: Element, kf: Keyframe[], ms: number, easing = OUT, fill: FillMode = 'none') => reduced.matches ? null : el.animate(kf, { duration: ms, easing, fill });
// Only what changed is touched: an element remembers the markup it was given and skips an identical one.
const H = new WeakMap<Element, string>();
const patch = (el: Element, html: string) => { if (H.get(el) === html) return false; el.innerHTML = html; H.set(el, html); return true; };
const cls = (el: Element, c: string) => { if (el.className !== c) el.className = c; };
const store = {
  get(k: string) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k: string, v: string) { try { localStorage.setItem(k, v); } catch { /* not remembered, that is all */ } },
};

// ---------- the host ----------
const API = `http://127.0.0.1:${new URLSearchParams(location.search).get('port') ?? '8016'}`;
async function call<T = Record<string, unknown>>(route: string, body?: unknown, method = body === undefined ? 'GET' : 'POST'): Promise<T> {
  const r = await fetch(API + route, { method, headers: body === undefined ? undefined : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof j.error === 'string' ? j.error : `后台答不上来（${r.status}）`);
  return j as T;
}
const toastEl = $('.toast');
let toastTimer = 0;
function toast(text: string) {
  toastEl.textContent = text; toastEl.hidden = false;
  anim(toastEl, [{ opacity: 0, transform: 'translate(-50%, 8px)' }, { opacity: 1, transform: 'translate(-50%, 0)' }], 320, SPRING);
  clearTimeout(toastTimer); toastTimer = window.setTimeout(() => { toastEl.hidden = true; }, 5200);
}
const tryCall = (route: string, body?: unknown, method?: string) => call(route, body, method).catch(e => { toast(e instanceof Error ? e.message : String(e)); return null; });

// ---------- words ----------
const STATE: Record<St, string> = { work: '在干活', pack: '在压缩', wait: '等你', done: '做完了', err: '出错了' };
const NAME: Record<Agent, string> = { claude: 'Claude Code', codex: 'Codex' };
const home = (p: string) => p.replace(/^\/Users\/[^/]+/, '~');
function age(ms: number) {
  const m = Math.floor((Date.now() - ms) / 60000);
  return m < 1 ? '刚刚' : m < 60 ? `${m} 分钟` : m < 1440 ? `${Math.floor(m / 60)} 小时` : `${Math.floor(m / 1440)} 天`;
}
// When an answer came: the clock today, the date in front on other days.
function clock(at: number) {
  const d = new Date(at), t = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  return d.toDateString() === new Date().toDateString() ? t : `${d.getMonth() + 1}月${d.getDate()}日 ${t}`;
}
function ago(since?: number) { const t = (Date.now() - (since ?? Date.now())) / 1000; return t < 60 ? `${Math.max(1, Math.round(t))} 秒` : `${Math.round(t / 60)} 分钟`; }

// ---------- markdown, the part agents use, block by block so a stream only redraws its last block ----------
// Links, addresses and paths in the text open in the workbench (workbench/refs.ts).
const inl = inline;
function mdBlocks(src: string) {
  const out: string[] = [], lines = src.split('\n');
  let list = '', lis: string[] = [], para: string[] = [];
  const endP = () => { if (para.length) out.push(`<p>${para.map(inl).join('<br>')}</p>`); para = []; };
  const endL = () => { if (list) out.push(`<${list}>${lis.join('')}</${list}>`); list = ''; lis = []; };
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i], li = /^\s*(?:[-*+]|(\d+)[.)])\s+(.*)/.exec(l), h = /^#{1,6}\s+(.*)/.exec(l);
    if (l.trimStart().startsWith('```')) {
      endP(); endL();
      const code: string[] = [];
      while (++i < lines.length && !lines[i].trimStart().startsWith('```')) code.push(lines[i]);
      out.push(`<pre>${esc(code.join('\n'))}</pre>`);
    } else if (/^\s*\|/.test(l)) {
      // A table reads fine in monospace, and costs nothing to lay out.
      endP(); endL();
      const rows = [l];
      while (i + 1 < lines.length && /^\s*\|/.test(lines[i + 1])) rows.push(lines[++i]);
      out.push(`<pre>${esc(rows.filter(r => !/^\s*\|[\s:|-]+\|\s*$/.test(r)).join('\n'))}</pre>`);
    } else if (h) { endP(); endL(); out.push(`<h4>${inl(h[1])}</h4>`); }
    else if (/^\s*>\s?/.test(l)) { endP(); endL(); out.push(`<blockquote>${inl(l.replace(/^\s*>\s?/, ''))}</blockquote>`); }
    else if (li) { endP(); const k = li[1] ? 'ol' : 'ul'; if (list !== k) { endL(); list = k; } lis.push(`<li>${inl(li[2])}</li>`); }
    else if (!l.trim()) { endP(); endL(); }
    else { endL(); para.push(l); }
  }
  endP(); endL();
  return out;
}
const md = (src: string) => `<div class="md">${mdBlocks(src).join('')}</div>`;
// The caret is one of her stars, at the end of the last line written.
const CARET = '<i class="caret" aria-hidden="true"></i>';
const withCaret = (b: string) => { const m = /<\/(?:p|li|pre|h4|blockquote)>(?:<\/[uo]l>)?$/.exec(b); return m ? b.slice(0, m.index) + CARET + b.slice(m.index) : b + CARET; };

// ---------- small pieces ----------
const star = (id: string, size = 12) => `<span class="mk" style="--s:${size}px"><canvas data-mk="${esc(id)}" data-size="${size}"></canvas></span>`;
const svg = (d: string, w = 13, extra = '') => `<svg viewBox="0 0 16 16" width="${w}" height="${w}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"${extra}>${d}</svg>`;
const I = {
  pin: svg('<path d="M6 2.5h4l-.6 4 2.3 2.2H4.3L6.6 6.5z"/><path d="M8 8.7V13.5"/>'),
  park: svg('<circle cx="8" cy="8" r="5.5"/><path d="M8 5v3.2l2 1.3"/>'),
  box: svg('<path d="M2.5 3.5h11v3h-11zM3.5 6.5v6h9v-6M6.5 9h3"/>'),
  term: svg('<rect x="1.8" y="2.8" width="12.4" height="10.4" rx="2"/><path d="M4.5 6.5 6.5 8l-2 1.5M8 10h3.5"/>'),
  chev: svg('<path d="M6 3.5 10.5 8 6 12.5"/>', 11, ' stroke-width="1.8"'),
  tree: svg('<circle cx="4.5" cy="3.5" r="1.5"/><circle cx="4.5" cy="12.5" r="1.5"/><circle cx="11.5" cy="6.5" r="1.5"/><path d="M4.5 5v6M11.5 8c0 2.5-4 2-7 3.2"/>', 11),
  img: svg('<rect x="2" y="3" width="12" height="10" rx="2"/><circle cx="6" cy="6.8" r="1.2"/><path d="m3 12 3.5-3.5 2.5 2.5 1.8-1.8L14 12"/>', 14),
  stop: '<svg viewBox="0 0 16 16" width="11" height="11" fill="currentColor" aria-hidden="true"><rect x="3.5" y="3.5" width="9" height="9" rx="2"/></svg>',
  up: svg('<path d="M8 13V3.5M4 7.5l4-4 4 4"/>', 14, ' stroke-width="2"'),
  doc: svg('<path d="M4 1.8h5.2L12.5 5v9.2H4z"/><path d="M9 1.8V5h3.5"/>', 12),
  sound: svg('<path d="M2.5 6h2.2L8 3.2v9.6L4.7 10H2.5z"/><path d="M10.6 5.6a3.4 3.4 0 0 1 0 4.8M12.4 3.9a5.8 5.8 0 0 1 0 8.2"/>', 15),
  mute: svg('<path d="M2.5 6h2.2L8 3.2v9.6L4.7 10H2.5z"/><path d="m10.8 6.2 3.4 3.6M14.2 6.2l-3.4 3.6"/>', 15),
  copy: svg('<rect x="5.5" y="5.5" width="8" height="8" rx="2"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/>', 12),
  check: svg('<path d="m3.5 8.5 3 3 6-7"/>', 12, ' stroke-width="1.8"'),
};
const STEP_K: Record<Step['k'], string> = { read: '读', edit: '改', bash: '跑', search: '搜', agent: '子任务', web: '网页', tool: '工具', say: '', think: '想' };
const who = (a: Agent) => `<span class="who ${a}">${a === 'claude' ? 'Claude' : 'Codex'}</span>`;
const diffHTML = (d: [string, string][]) => `<div class="diff">${d.map(([s, t]) => `<code class="${s === '+' ? 'add' : s === '-' ? 'del' : ''}"><b>${s === ' ' ? '' : s === '-' ? '−' : '+'}</b><span>${esc(t)}</span></code>`).join('')}</div>`;
function stepsSummary(steps: Step[]) {
  const n = (k: Step['k']) => steps.filter(s => s.k === k).length;
  const add = steps.reduce((a, s) => a + (s.add ?? 0), 0), del = steps.reduce((a, s) => a + (s.del ?? 0), 0);
  return [n('read') + n('search') ? `读了 ${n('read') + n('search')} 个` : '', n('edit') ? `改了 ${n('edit')} 个 <span class="p">+${add}</span> <span class="m">−${del}</span>` : '',
    n('bash') ? `跑了 ${n('bash')} 条` : '', n('agent') ? `${n('agent')} 个子任务` : '', n('web') ? `查了 ${n('web')} 次网页` : '', n('tool') ? `用了 ${n('tool')} 个工具` : '']
    .filter(Boolean).join(' · ');
}

// ---------- sound: her kit, exactly as the notch plays it (melody "fifths", palette "dropCrisp") ----------
const snd = { on: store.get('agents.sound') !== 'off', ctx: null as AudioContext | null, out: null as GainNode | null, last: new Map<string, number>() };
// The same cue twice in quick succession is one cue: a burst of finishes is one chime, not a chord.
const NOTICE = new Set(['done', 'ask', 'error']);
// `notice`: the cue announces a session; B01 in front keeps those quiet, not the ones answering what Allen did there.
function cue(name: string, gain = 1, force = false, notice = NOTICE.has(name)) {
  if (!snd.on && !force) return;
  if (notice && win?.classList.contains('bw') && document.hasFocus()) return;
  const now = performance.now();
  if (now - (snd.last.get(name) ?? -1e9) < (notice ? 450 : 120)) return;
  snd.last.set(name, now);
  // Made at the first cue, not the first touch: the window may play before it is ever clicked (its autoplay is allowed),
  // so a session finishing while the window just sits open still chimes.
  if (!snd.ctx) { snd.ctx = new AudioContext(); snd.out = snd.ctx.createGain(); snd.out.gain.value = .8; snd.out.connect(snd.ctx.destination); }
  const c = scoreOf('fifths')[name];
  if (c) { void snd.ctx.resume(); play(snd.ctx, snd.out!, c, palette('dropCrisp'), gain); }
}
// A pick (a model, a filter, a menu line, a copy) is one quiet click, Hermes' selection haptic, as her kit has it for the mic.
const tick = () => cue('mic', .45);
// A step along B01's sky: the prototype's short falling blip, pitched by p.
function blip(p: number) {
  if (!snd.on) return;
  if (!snd.ctx) { snd.ctx = new AudioContext(); snd.out = snd.ctx.createGain(); snd.out.gain.value = .8; snd.out.connect(snd.ctx.destination); }
  const a = snd.ctx, t = a.currentTime + .004, o = a.createOscillator(), g = a.createGain(), f = a.createBiquadFilter();
  void a.resume();
  o.type = 'sine'; o.frequency.setValueAtTime(1850 * p, t); o.frequency.exponentialRampToValueAtTime(1200 * p, t + .035);
  f.type = 'lowpass'; f.frequency.value = 3200;
  g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(.05, t + .003); g.gain.exponentialRampToValueAtTime(1e-4, t + .05);
  o.connect(f).connect(g).connect(snd.out!); o.start(t); o.stop(t + .07);
}

// ---------- her: the ball at the top of the list, the desktop's own starCore ----------
const win = $('#win');
const core = new Core('glass');
const herCv = $<HTMLCanvasElement>('.her-c', win), hctx = herCv.getContext('2d')!;
const eyeCv = document.createElement('canvas'), ectx = eyeCv.getContext('2d')!;
const HW = 60, HR = 15;
const her = { face: 'rest' as ExprId, faceUntil: 0, lookId: '', lookUntil: 0, ptr: null as [number, number] | null, pressed: false, glow: '' };
// She turns to the row that changed and makes the face for it, then settles back.
function herSay(face: ExprId, ms: number, id = '') {
  const now = performance.now();
  her.face = face; her.faceUntil = now + ms;
  if (id) { her.lookId = id; her.lookUntil = now + Math.min(ms, 1800); }
}
function herLook(): [number, number] | null {
  const now = performance.now(), row = now < her.lookUntil ? rowEls.get(her.lookId) : undefined;
  if (!row?.isConnected && !her.ptr) return null;
  const r = herCv.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  let x: number, y: number;
  if (row?.isConnected) { const q = row.getBoundingClientRect(); x = q.left + 60 - cx; y = q.top + q.height / 2 - cy; }
  else { x = her.ptr![0] - cx; y = her.ptr![1] - cy; if (Math.hypot(x, y) > 340) return null; }
  const dist = Math.hypot(x, y) || 1, k = dist / (dist + 90);
  return [x / dist * k, y / dist * k];
}
function herFrame(now: number, dt: number) {
  core.update(now, dt, { expr: now < her.faceUntil ? her.face : 'rest', look: herLook(), still: false, pressed: her.pressed, charge: 0 });
  const d = DPR(), N = Math.round(HW * d), c = hctx, m = HW / 2;
  if (herCv.width !== N) herCv.width = herCv.height = N;
  c.setTransform(d, 0, 0, d, 0, 0); c.clearRect(0, 0, HW, HW);
  const S = Math.round(2 * 1.3 * HR * d), [jx, jy, bx, by] = core.pose(1);
  if (core.render(S, HR * d < 20 ? 2 : 3)) {
    c.save(); c.translate(m, m); core.orbit(c, HR, -1); c.restore();
    c.save(); c.translate(m + jx * HR, m + jy * HR); c.scale(bx, by); core.inside(c, HR, S); core.glass(c, HR, S); c.restore();
  }
  const E = Math.ceil(3.8 * HR * d);
  if (eyeCv.width !== E) eyeCv.width = eyeCv.height = E;
  ectx.setTransform(1, 0, 0, 1, 0, 0); ectx.clearRect(0, 0, E, E);
  ectx.setTransform(d, 0, 0, d, E / 2, E / 2); ectx.translate(jx * HR, jy * HR); ectx.scale(bx, by); core.eyes(ectx, HR);
  const [glow, blur] = core.glow();
  c.save(); c.setTransform(1, 0, 0, 1, 0, 0); c.shadowColor = glow; c.shadowBlur = blur * HR * d; c.drawImage(eyeCv, m * d - E / 2, m * d - E / 2); c.restore();
  c.save(); c.translate(m, m); core.orbit(c, HR, 1); core.particles(c, HR, d); c.restore();
  // Her light is the window's accent, as it is the Dashboard's.
  const g = core.light.glow.map(v => Math.round(v * 255)).join(' ');
  if (g !== her.glow) { her.glow = g; win.style.setProperty('--glow', g); }
}

// ---------- marks: the island's stars; a mark repaints only while it moves ----------
const painted = new WeakMap<HTMLCanvasElement, string>();
const canvases = win.getElementsByTagName('canvas');
const stAt = new Map<string, number>();
const seed = (id: string) => [...id].reduce((a, ch) => a + ch.charCodeAt(0), 0) % 7;
function paintMarks(now: number) {
  for (const cv of canvases) {
    const s = cv.dataset.mk ? byId(cv.dataset.mk) : undefined;
    if (!s) continue;
    const since = (now - (stAt.get(s.id) ?? -1e9)) / 1000, moving = !reduced.matches && (s.st !== 'done' || since < .5), key = moving ? '' : s.st;
    if (key && painted.get(cv) === key) continue;
    painted.set(cv, key);
    const size = Number(cv.dataset.size), d = DPR(), w = Math.round(size * 1.6 * d), k = size / 16;
    if (cv.width !== w) cv.width = cv.height = w;
    const c = cv.getContext('2d')!;
    c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, w, w); c.setTransform(d * k, 0, 0, d * k, w / 2, w / 2);
    drawMark(c, 'spark', s.st, moving ? now / 1000 + seed(s.id) : 0, reduced.matches ? 99 : since, d * k);
  }
}

// ---------- the app ----------
type View = 'chat' | 'new' | 'archive';
type Open = { open?: boolean; step?: number };
const app = {
  ss: [] as Sess[], catalog: null as Catalog | null, items: new Map<string, Item[]>(), live: new Map<string, string>(),
  // Which folded steps Allen opened, per session and item: that is this window's business, not the host's.
  opened: new Map<string, Map<number, Open>>(),
  cur: '', view: 'chat' as View, filter: 'all' as 'all' | Agent, by: 'state' as 'state' | 'project', q: '',
  files: [] as Attached[], menu: '' as '' | 'slash' | 'at', pick: 0, picks: [] as [string, string][], renaming: false, del: '',
  newAgent: (store.get('agents.agent') === 'codex' ? 'codex' : 'claude') as Agent, newProject: store.get('agents.project') ?? '', newTree: store.get('agents.tree') !== 'off',
  newSet: { model: '', effort: '', mode: '' }, projects: [] as string[],
  sideOpen: false, openAt: performance.now(), how: 'click' as 'click' | 'key', sending: false,
  // Answers picked on a question card with more than one question or more than one choice.
  asked: new Map<string, string[][]>(),
  // busy: per session, the answer in flight (its request and what was pressed) · cx: each session's last measured context,
  // or why it could not be read · cxOpen: rows opened in the context popover
  busy: new Map<string, { req: string; key: string }>(), cx: new Map<string, Ctx | string>(), cxOpen: new Set<string>(),
  // usage: the plans as the daemon last read them, for the composer's ring · cxMore: the usage card's context detail open
  usage: null as Usage | null, cxMore: false,
};
const byId = (id: string) => app.ss.find(s => s.id === id);
const cur = () => byId(app.cur);
const side = $('.side', win), list = $('.s-list', side), herT = $('.her-t', side), find = $<HTMLInputElement>('#find'), archLink = $('.arch-link', side);
const head = $('.m-head', win), hMk = $('.h-mk', head), hT = $('.h-t', head), hMeta = $('.h-meta', head);
const hostEl = $('.host', win), comp = $('.composer', win), ta = $<HTMLTextAreaElement>('#msg'), cMenu = $('.c-menu', comp), cFiles = $('.c-files', comp), tl = $('.t-l', comp), tr = $('.t-r', comp);
const bnEl = $('.bn', comp), hintEl = $('.hint', comp), cRows = $('.c-rows', comp);
const pop = $('.pop', win), sndBtn = $('.snd', win), offEl = $('.w-off', win);

// Every change asks for a frame; one frame draws whatever was asked for since the last.
type Part = 'side' | 'head' | 'main' | 'live' | 'comp';
const dirty = new Set<Part>();
let raf = 0;
function draw(...parts: Part[]) {
  for (const p of parts.length ? parts : ['side', 'head', 'main', 'comp'] as Part[]) dirty.add(p);
  if (!raf) raf = requestAnimationFrame(flush);
}
// Each session keeps its own draft, and the new-session page one of its own: what you typed or attached for one never
// goes to another when you switch.
const drafts = new Map<string, { text: string; files: Attached[] }>();
let draftAt = '';
function swapDraft() {
  const key = app.view === 'new' ? '__new' : app.view === 'chat' ? app.cur : draftAt;
  if (key === draftAt) return;
  if (ta.value || app.files.length) drafts.set(draftAt, { text: ta.value, files: app.files }); else drafts.delete(draftAt);
  const d = drafts.get(key);
  drafts.delete(key);
  ta.value = d?.text ?? ''; app.files = d?.files ?? []; app.menu = ''; app.picks = [];
  ta.style.height = 'auto'; ta.style.height = ta.value ? `${Math.min(180, ta.scrollHeight)}px` : '';
  draftAt = key;
  dirty.add('comp');
}
function flush() {
  raf = 0;
  swapDraft();
  const d = new Set(dirty); dirty.clear();
  if (d.has('side')) renderSide();
  if (d.has('head')) renderHead();
  const s = cur();
  if (d.has('main')) renderMain();
  else if (d.has('live') && app.view === 'chat' && s) { const c = convs.get(s.id); if (c) { const b = bottom(c.root); renderLive(s, c); if (b) c.root.scrollTop = c.root.scrollHeight; } }
  if (d.has('comp')) renderComp();
  if (tipFor && !tipFor.isConnected) tipHide();
  attention.refresh();
}
// A session's change redraws the list; the rest only when it is the one on screen.
const touch = (id: string) => { if (app.view === 'chat' && app.cur === id) draw(); else draw('side'); };

// ---------- the side: her, then every session, keyed so rows glide when they change group ----------
function visible() {
  const q = app.q.trim().toLowerCase();
  return app.ss.filter(s => (app.filter === 'all' || s.agent === app.filter) && (!q || `${s.title} ${s.summary} ${s.project} ${s.branch}`.toLowerCase().includes(q)));
}
// Parked (ADR 0069, shared with the notch) is out of his turn and quiet until he takes it back (queue.ts).
const yourTurn = (s: Sess) => !!waitOf(s);
function groups(): [string, Sess[]][] {
  const vs = visible().filter(s => !s.archived).sort((a, b) => b.updated - a.updated);
  if (app.by === 'project') return [...new Set(vs.map(s => s.project))].map(p => [p, vs.filter(s => s.project === p)]);
  const rest = vs.filter(s => !s.pinned && !s.parked);
  return ([
    ['置顶', vs.filter(s => s.pinned && !s.parked)],
    ['轮到你', rest.filter(yourTurn)],
    ['在干活', rest.filter(s => !yourTurn(s) && (s.st === 'work' || s.st === 'pack'))],
    ['做完了', rest.filter(s => !yourTurn(s) && (s.st === 'done' || s.st === 'err' || s.st === 'wait'))],
    ['先放着', vs.filter(s => s.parked)],
  ] as [string, Sess[]][]).filter(g => g[1].length);
}
const order = () => groups().flatMap(g => g[1].map(s => s.id));
const label = (s: Sess) => s.term ? '在终端里' : s.stopped ? '停了' : STATE[s.st];

const rowEls = new Map<string, HTMLElement>(), grpEls = new Map<string, HTMLElement>();
let quiet = true; // the first draw, filtering and search do not animate the list
function rowEl(s: Sess) {
  let el = rowEls.get(s.id);
  if (!el) {
    el = document.createElement('div');
    el.dataset.act = 'open'; el.dataset.id = s.id; el.tabIndex = 0; el.setAttribute('role', 'button');
    el.innerHTML = `${star(s.id, 12)}<span class="r-b"></span><span class="r-acts"></span>`;
    rowEls.set(s.id, el);
  }
  return el;
}
function renderSide() {
  const live = app.ss.filter(s => !s.archived && !s.parked), nWait = live.filter(yourTurn).length, nWork = live.filter(s => s.st === 'work' || s.st === 'pack').length;
  patch(herT, nWait
    ? `<b class="warm">${nWait} 个轮到你</b><span>${nWork ? `${nWork} 个在干活 · ` : ''}点我去下一个</span>`
    : nWork ? `<b>${nWork} 个在干活</b><span>没有要你管的</span>` : live.length ? '<b>都做完了</b><span>想到什么就开一个新的</span>' : '<b>还没有会话</b><span>点「新会话」开一个</span>');
  side.querySelectorAll<HTMLElement>('.seg button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === app.filter)));
  patch($('.by', side), app.by === 'state' ? '按状态' : '按项目');
  $('.new', side).classList.toggle('is-on', app.view === 'new');
  archLink.classList.toggle('is-on', app.view === 'archive');
  patch($('em', archLink), String(visible().filter(s => s.archived).length));

  const glide = !quiet && !reduced.matches, before = new Map<Element, number>();
  if (glide) for (const k of list.children) before.set(k, k.getBoundingClientRect().top);
  const want: HTMLElement[] = [];
  for (const [g, ss] of groups()) {
    let h = grpEls.get(g);
    if (!h) { h = document.createElement('div'); h.className = 'g-h'; grpEls.set(g, h); }
    patch(h, `${esc(g)}<em>${ss.length}</em>`); want.push(h);
    for (const s of ss) {
      const el = rowEl(s);
      cls(el, `row${s.id === app.cur && app.view === 'chat' ? ' is-on' : ''}${s.st === 'wait' && !s.term ? ' is-ask' : ''}${s.unread ? ' is-new' : ''}${s.st === 'err' ? ' is-err' : ''}`);
      patch(el.children[1], `<b>${esc(s.title)}</b><span class="age">${s.unread ? '<i class="nd"></i>' : ''}${age(s.updated)}</span>`
        + `<span class="sum">${who(s.agent)}<span class="dot">·</span><span class="t">${esc(s.term ? '在终端里' : s.summary)}</span></span>`);
      patch(el.children[2], `<i data-act="pin" data-id="${s.id}" data-tip="${s.pinned ? '取消置顶' : '置顶'}"${s.pinned ? ' class="on"' : ''}>${I.pin}</i>`
        + `<i data-act="park" data-id="${s.id}" data-tip="${s.parked ? '拿回来' : '先放着'}"${s.parked ? ' class="on"' : ''}>${I.park}</i><i data-act="archive" data-id="${s.id}" data-tip="归档">${I.box}</i>`);
      want.push(el);
    }
  }
  want.forEach((el, i) => { if (list.children[i] !== el) list.insertBefore(el, list.children[i] ?? null); });
  while (list.children.length > want.length) { const x = list.lastElementChild as HTMLElement; for (const a of x.getAnimations()) a.cancel(); x.style.pointerEvents = ''; x.remove(); }
  const empty = $('.empty', side);
  empty.hidden = want.length > 0 || !app.ss.some(s => !s.archived);
  patch(empty, '没有对得上的会话。');
  // FLIP: every row that moved starts where it was and springs to where it is; a new one drops in.
  if (glide) for (const el of want) {
    const b = before.get(el), t = el.getBoundingClientRect().top;
    if (b === undefined) anim(el, [{ opacity: 0, transform: 'translateY(-8px) scale(.97)' }, { opacity: 1, transform: 'none' }], 460, SPRING);
    else if (Math.abs(b - t) > 1) anim(el, [{ transform: `translateY(${b - t}px)` }, { transform: 'none' }], 520, SPRING);
  }
  quiet = false;
}

// ---------- the header ----------
function renderHead() {
  const s = cur(), chat = app.view === 'chat' && !!s;
  head.classList.toggle('plain', !chat);
  if (!chat) {
    patch(hMk, ''); patch(hT, `<b>${app.view === 'archive' ? '已归档' : '没有开着的会话'}</b>`);
    patch(hMeta, app.view === 'archive' ? '<span>还能搜到，随时能拿回来</span>' : '<span>⌘N 落下一张纸条，写一句要它做什么</span>');
    return;
  }
  patch(hMk, star(s.id, 13));
  if (app.renaming) { if (patch(hT, `<input id="rename" class="rename" value="${esc(s.title)}" aria-label="会话名字" autocomplete="off">`)) { const r = $<HTMLInputElement>('#rename', hT); r.focus(); r.select(); } }
  else patch(hT, `<b data-act="rename" data-tip="点一下改名">${esc(s.title)}</b>`);
  patch(hMeta, `${who(s.agent)}<span class="dot">·</span><span title="${esc(s.cwd)}">${esc(s.project)}</span><span class="dot">·</span><span class="br">⎇ ${esc(s.branch || '—')}</span><span class="dot">·</span><span class="st st-${s.st}">${label(s)}</span>`);
  const tb = $('[data-act="terminal"]', head);
  patch($('span', tb), s.term ? '拿回来' : '在终端打开');
  tb.dataset.tip = s.term ? '在这里接着聊' : `在 Ghostty 里接着聊 · ${s.agent === 'codex' ? 'codex resume' : 'claude --resume'}`;
}

// ---------- the conversation: one kept per session, so switching is instant and each keeps its place ----------
type Conv = { root: HTMLElement; items: HTMLElement; live: HTMLElement; queue: HTMLElement; now: HTMLElement; bg: HTMLElement; term: HTMLElement; older: HTMLElement; ask: HTMLElement; built: boolean; scroll: number };
const convs = new Map<string, Conv>();
function convOf(id: string): Conv {
  let c = convs.get(id);
  if (!c) {
    const root = document.createElement('div');
    root.className = 'conv';
    root.innerHTML = '<div class="c-in"><button type="button" class="older" data-act="older" hidden></button><div class="c-items"></div><div class="it md live" hidden></div><div class="c-queue"></div><div class="ask" hidden><button type="button" data-act="land">一键落地 <kbd>⌘⏎</kbd></button><span>推送那一步会等你点头</span></div><div class="now" hidden><span class="nm"></span><span class="shine"></span><span class="el"></span><kbd>esc</kbd><span class="k">打断</span></div><p class="bg" hidden></p><div class="banner" hidden></div></div>';
    c = { root, items: $('.c-items', root), live: $('.live', root), queue: $('.c-queue', root), now: $('.now', root), bg: $('.bg', root), term: $('.banner', root), older: $('.older', root), ask: $('.ask', root), built: false, scroll: -1 };
    convs.set(id, c);
  }
  return c;
}
const viewNew = document.createElement('div'), viewArch = document.createElement('div');
viewNew.className = viewArch.className = 'conv';
viewNew.innerHTML = '<div class="c-in wide"></div>'; viewArch.innerHTML = '<div class="c-in wide"></div>';
const bottom = (r: HTMLElement) => !r.isConnected || r.scrollTop >= r.scrollHeight - r.clientHeight - 40;
// To the latest answer. Rows that come on screen trade their 60 px guess for their real height and push the end away, so
// it follows the end frame by frame until the height holds, or until Allen scrolls.
function toEnd(root: HTMLElement, frames = 30) {
  const h = root.scrollHeight;
  root.scrollTop = h;
  const at = root.scrollTop;
  requestAnimationFrame(() => { if (frames && root.isConnected && root.scrollTop === at && root.scrollHeight !== h) toEnd(root, frames - 1); });
}

let shownId = '';
function show(root: HTMLElement, id: string) {
  if (shownId === id) return;
  const prev = convs.get(shownId);
  if (prev) prev.scroll = prev.root.scrollTop;
  hostEl.replaceChildren(root); shownId = id;
  const c = convs.get(id);
  if (c && c.scroll < 0) toEnd(root); else root.scrollTop = c ? c.scroll : 0;
  // Switching by keyboard is a quick fade; by click it also rises a little.
  const k = root.firstElementChild!;
  if (app.how === 'key') anim(k, [{ opacity: .35 }, { opacity: 1 }], 120);
  else anim(k, [{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'none' }], 300, OUT);
}
function renderMain() {
  if (app.view === 'new') { renderNew(); show(viewNew, '__new'); return; }
  if (app.view === 'archive') { renderArchive(); show(viewArch, '__arch'); return; }
  const s = cur();
  if (!s) { app.view = 'new'; renderNew(); show(viewNew, '__new'); return; }
  const c = convOf(s.id);
  renderConv(s, c);
  show(c.root, s.id);
}

function reqHead(r: Req) {
  if (r.tool === 'Bash') return '要你批准 · 跑一条命令';
  if (r.tool === 'Edit') return '要你批准 · 改一个文件';
  if (r.tool === 'Plan') return '计划写好了';
  return `要你批准 · ${r.tool === 'Tool' ? esc(r.name) : r.tool === 'Form' ? esc(r.server) : ''}`;
}
function reqRecord(r: Req) {
  if (r.tool === 'Ask') return r.qs.map(q => q.q).join(' · ');
  if (r.tool === 'Plan') return '计划';
  if (r.tool === 'Bash') return r.cmd;
  if (r.tool === 'Edit') return `改 ${r.file}`;
  return r.tool === 'Form' ? r.server : r.name;
}
function itemHTML(s: Sess, it: Exclude<Item, { k: 'steps' }>, i = -1) {
  if (it.k === 'you') return `<div class="you">${it.files?.length ? `<span class="att">${it.files.map(picHTML).join('')}</span>` : ''}${esc(it.text)}</div>`;
  if (it.k === 'it') { const cards = i >= 0 ? cardsHTML(it.text, editsBefore(app.items.get(s.id) ?? [], i)) : ''; return `<div class="it">${features.reduce((h, f) => f.answer?.(s, it, i, h) ?? h, withCopy(md(it.text)))}${cards ? `<div class="lnks">${cards}</div>` : ''}<div class="it-acts"><button type="button" class="ia" data-act="copy" data-tip="复制" aria-label="复制">${I.copy}</button>${it.at ? `<time>${clock(it.at)}</time>` : ''}</div></div>`; }
  if (it.k === 'note') return `<p class="note">${esc(it.text)}</p>`;
  if (it.k === 'plan') return `<div class="plan"><span class="p-h">计划</span>${it.todos.map(([t, d]) => `<span class="todo d${d}"><i></i>${esc(t)}</span>`).join('')}</div>`;
  const r = it.req, a = NAME[s.agent], b = app.busy.get(s.id), busy = b?.req === r.id ? b.key : '';
  const on = (k: string) => busy === k ? ' is-busy' : '', off = busy ? ' disabled' : '';
  if (it.done) return `<p class="note done"><span class="ok">${/^(拒绝|没回答)/.test(it.done) ? '✕' : '✓'}</span>${esc(reqRecord(r))}<span class="how">${esc(it.done)}</span></p>`;
  if (r.tool === 'Ask') {
    const picked = app.asked.get(r.id) ?? [], simple = r.qs.length === 1 && !r.qs[0].multi;
    return `<div class="req ask${busy ? ' busy' : ''}"><span class="r-h">${esc(a)} 问你</span>${r.qs.map((q, qi) => `<div class="q-block"><p class="q">${esc(q.q)}</p><div class="opts">${q.opts.map(([l, d], k) =>
      `<button type="button" class="opt${picked[qi]?.includes(l) ? ' on' : ''}${on(`opt:${l}`)}" data-act="${simple ? 'answer' : 'pickopt'}" data-q="${qi}" data-v="${esc(l)}" data-req="${esc(r.id)}"${off}><i>${k + 1}</i><span><b>${esc(l)}</b>${d ? `<small>${esc(d)}</small>` : ''}</span></button>`).join('')}</div></div>`).join('')}`
      + (simple ? '' : `<div class="choice"><button type="button" class="btn warm${on('all')}" data-act="answerall" data-req="${esc(r.id)}"${busy || !r.qs.every((_, qi) => picked[qi]?.length) ? ' disabled' : ''}>好了</button></div>`)
      + `<p class="hint">${simple ? '按数字键选，' : ''}<kbd>esc</kbd> 不回答，也可以直接在下面打字回答。</p></div>`;
  }
  if (r.tool === 'Plan') return `<div class="req${busy ? ' busy' : ''}"><span class="r-h">${reqHead(r)}</span><div class="plan-text">${md(r.plan)}</div><div class="choice">`
    + `<button type="button" class="btn${on('deny')}" data-act="deny" data-req="${esc(r.id)}"${off}>再想想<kbd>esc</kbd></button><button type="button" class="btn warm${on('allow')}" data-act="allow" data-req="${esc(r.id)}"${off}>就这么做<kbd>↵</kbd></button></div>`
    + '<p class="hint">点「再想想」前可以在下面写哪里要改。</p></div>';
  const what = r.tool === 'Bash' ? `<pre class="cmd"><span>${esc(home(r.cwd))} $</span> ${esc(r.cmd)}</pre>`
    : r.tool === 'Edit' ? `<div class="file">${I.doc}${esc(r.file)}</div>${r.diff.length ? diffHTML(r.diff) : ''}`
    : `<pre class="cmd">${esc(r.tool === 'Form' ? [r.url ?? '', ...r.fields.map(f => `· ${f.title}`)].filter(Boolean).join('\n') : r.detail)}</pre>`;
  return `<div class="req${busy ? ' busy' : ''}"><span class="r-h">${reqHead(r)}</span>${r.why ? `<p class="why">${esc(r.why)}</p>` : ''}${what}<div class="choice">`
    + `<button type="button" class="btn${on('deny')}" data-act="deny" data-req="${esc(r.id)}"${off}>拒绝<kbd>esc</kbd></button>${r.tool !== 'Form' && r.always ? `<button type="button" class="btn${on('always')}" data-act="always" data-req="${esc(r.id)}"${off}>${esc(r.always)}</button>` : ''}`
    + `<button type="button" class="btn warm${on('allow')}" data-act="allow" data-req="${esc(r.id)}"${off}>允许<kbd>↵</kbd></button></div></div>`;
}
// A picture shows itself and opens large; one the host kept no copy of stays a named chip.
const picHTML = (f: Pic) => f.img ? `<button type="button" class="pic" data-act="view" data-tip="看大图"><img src="${API}/images/${esc(f.img)}" alt="${esc(f.name)}" loading="lazy" decoding="async"></button>`
  : `<span class="thumb">${I.img}${esc(f.name)}</span>`;
// Code blocks in a finished answer get their own copy button.
const withCopy = (html: string) => html.replace(/<pre>/g, `<div class="code"><button type="button" class="cp" data-act="copy" data-what="code">${I.copy}<span>复制</span></button><pre>`).replace(/<\/pre>/g, '</pre></div>');
// A new item arrives the way it happened: yours rises from the composer, a request drops in, the rest fade.
function enter(el: HTMLElement, it: Item) {
  if (it.k === 'you') { el.style.transformOrigin = '100% 100%'; anim(el, [{ opacity: 0, transform: 'translateY(14px) scale(.97)' }, { opacity: 1, transform: 'none' }], 460, SPRING); }
  else if (it.k === 'req') anim(el, [{ opacity: 0, transform: 'translateY(-6px) scale(.98)' }, { opacity: 1, transform: 'none' }], 480, SPRING);
  else if (it.k !== 'it') anim(el, [{ opacity: 0 }, { opacity: 1 }], 240);
}
// An answered request folds into its one-line record: the height eases from the card to the line.
function morph(el: HTMLElement, html: string) {
  const h0 = el.offsetHeight;
  patch(el, html);
  const h1 = el.offsetHeight;
  if (h0 === h1 || reduced.matches) return;
  el.style.overflow = 'hidden';
  el.animate([{ height: `${h0}px`, opacity: .2 }, { height: `${h1}px`, opacity: 1 }], { duration: 300, easing: OUT }).onfinish = () => { el.style.overflow = ''; };
}
const SKEL = '<div class="skel" aria-hidden="true"><div class="y"><i style="width:44%"></i></div><div><i class="s" style="width:34%"></i></div><div><i style="width:86%"></i><i style="width:71%"></i><i style="width:52%"></i></div>'
  + '<div class="y"><i style="width:31%"></i></div><div><i class="s" style="width:28%"></i></div><div><i style="width:78%"></i><i style="width:60%"></i></div></div>';
function renderConv(s: Sess, c: Conv) {
  const stick = bottom(c.root), items = app.items.get(s.id), built = c.built;
  // Not read yet: the shape of a conversation for a moment, then the conversation fades in over it.
  if (!items) { patch(c.items, SKEL); c.built = false; return; }
  if (!c.built) { if (c.items.firstElementChild?.classList.contains('skel')) anim(c.items, [{ opacity: 0 }, { opacity: 1 }], 200); c.items.replaceChildren(); H.delete(c.items); }
  items.forEach((it, i) => {
    let el = c.items.children[i] as HTMLElement | undefined;
    const isNew = !el;
    if (!el) { el = document.createElement('div'); el.className = 'item'; c.items.appendChild(el); }
    if (it.k === 'steps') renderSteps(s.id, el, it, i, c.built);
    else if (!isNew && it.k === 'req' && it.done && H.get(el)?.includes('class="req')) morph(el, itemHTML(s, it, i));
    else { if (el.firstElementChild?.classList.contains('steps')) { el.replaceChildren(); H.delete(el); } patch(el, itemHTML(s, it, i)); if (isNew && c.built) enter(el, it); }
  });
  while (c.items.children.length > items.length) c.items.lastElementChild!.remove();
  // In the stage's narrow column only the last turn stays: everything up to the last thing Allen said folds into one line.
  const lastYou = items.map(it => it.k).lastIndexOf('you'), older = items.slice(0, lastYou + 1).filter(it => it.k === 'you').length;
  [...c.items.children].forEach((el, i) => el.classList.toggle('old', i <= lastYou));
  c.older.hidden = !older; patch(c.older, `更早 ${older} 轮`);
  // The way to land what this session changed, once it is done and nothing is under way.
  c.ask.hidden = !(s.dirty && s.st === 'done' && !s.term && !s.gone && (!s.land || s.land.s === 'done'));
  renderLive(s, c);
  patch(c.queue, (s.queue ?? []).map(q => `<div class="item"><div class="you queued">${esc(q)}<em>排队中 · 这一步做完它就会看到</em></div></div>`).join(''));
  const working = s.st === 'work' || s.st === 'pack';
  if (working && c.now.hidden && c.built) anim(c.now, [{ opacity: 0 }, { opacity: 1 }], 240);
  c.now.hidden = !working;
  if (working) {
    patch($('.nm', c.now), star(s.id, 10));
    patch($('.shine', c.now), `${esc(s.st === 'pack' ? '在压缩上下文' : s.now ?? '在想')}…`);
    patch($('.el', c.now), s.since ? `· ${ago(s.since)}` : '');
  }
  if (s.term && c.term.hidden && c.built) anim(c.term, [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], 420, SPRING);
  c.term.hidden = !s.term;
  if (s.term) patch(c.term, `${I.term}<span><b>在终端里打开着。</b>Jarvis 先放手，一次只有一边能写。</span><button type="button" class="btn" data-act="takeback">拿回来</button>`);
  c.built = true;
  if (stick && !built) toEnd(c.root); else if (stick) c.root.scrollTop = c.root.scrollHeight;
}
// Steps keep their elements: new ones slide in while it works, and the list folds shut when the turn ends.
function renderSteps(id: string, el: HTMLElement, it: Item & { k: 'steps' }, i: number, animate: boolean) {
  const first = !el.firstElementChild?.classList.contains('steps'), s = byId(id)!;
  if (first) {
    H.delete(el);
    el.innerHTML = `<div class="steps"><button type="button" class="s-sum" data-act="steps" data-i="${i}"><span class="chev">${I.chev}</span><span class="s-t"></span></button><div class="s-wrap"><div class="s-clip"><div class="s-in"></div></div></div></div>`;
    if (animate) anim(el, [{ opacity: 0 }, { opacity: 1 }], 240);
  }
  const o = app.opened.get(id)?.get(i) ?? {};
  const box = el.firstElementChild as HTMLElement, live = !!it.live, btn = box.firstElementChild as HTMLButtonElement, rows = $('.s-in', box);
  box.hidden = !it.steps.length;
  // While it works only its newest step shows, and the list folds from that one; the line opens all of them.
  box.classList.toggle('live', live); box.classList.toggle('open', live || !!o.open); box.classList.toggle('tail', !o.open);
  btn.setAttribute('aria-expanded', String(!!o.open));
  const sum = stepsSummary(it.steps);
  patch(btn.lastElementChild!, `${live ? '正在干' : it.took ? `干了 ${esc(it.took)}` : '干完了'}${sum ? ` · ${sum}` : ''}`);
  // A folded list draws its rows only once it is opened.
  if (!live && !o.open) { if (rows.childElementCount && first) rows.replaceChildren(); if (!rows.childElementCount) return; }
  it.steps.forEach((st, j) => {
    let r = rows.children[j] as HTMLElement | undefined;
    if (!r) { r = document.createElement('div'); rows.appendChild(r); if (!first && live) anim(r, [{ opacity: 0, transform: 'translateX(-6px)' }, { opacity: 1, transform: 'none' }], 320, OUT); }
    if (st.k === 'say') { cls(r, 'step say'); patch(r, esc(st.t)); return; }
    const more = !!(st.diff?.length || st.out), open = o.step === j;
    cls(r, `step${more ? ' more' : ''}${open ? ' open' : ''}`);
    if (more) { r.dataset.act = 'step'; r.dataset.i = String(i); r.dataset.j = String(j); r.setAttribute('role', 'button'); r.tabIndex = 0; }
    const was = r.dataset.shown === '1';
    patch(r, `<span class="k">${STEP_K[st.k]}</span><span class="a" title="${esc(st.t)}">${features.reduce((h, f) => f.arg?.(s, st, i, j, h) ?? h, esc(st.t))}</span>`
      + `<span class="r">${st.add !== undefined ? `<span class="p">+${st.add}</span> <span class="m">−${st.del ?? 0}</span>` : st.ok === true ? '<span class="p">✓</span>' : st.ok === false ? '<span class="m">✕</span>' : ''}</span>`
      + (open ? `<div class="x">${st.diff?.length ? diffHTML(st.diff) : `<pre class="out">${esc(st.out ?? '')}</pre>`}</div>` : '')
      + features.map(f => f.under?.(s, st, i, j) ?? '').join(''));
    if (open && !was) { const x = $('.x', r); if (x) anim(x, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], 260); }
    r.dataset.shown = open ? '1' : '';
  });
  while (rows.children.length > it.steps.length) rows.lastElementChild!.remove();
}
// The answer as it is written: only its last block changes, so a long answer costs no more than a short one.
function renderLive(s: Sess, c: Conv) {
  const el = c.live, text = app.live.get(s.id);
  if (text === undefined) { if (!el.hidden) { el.hidden = true; el.textContent = ''; } return; }
  el.hidden = false;
  const bs = mdBlocks(text);
  if (!bs.length) bs.push('<p></p>');
  bs[bs.length - 1] = withCaret(bs[bs.length - 1]);
  bs.forEach((b, i) => { let x = el.children[i]; if (!x) { x = document.createElement('div'); el.appendChild(x); } patch(x, b); });
  while (el.children.length > bs.length) el.lastElementChild!.remove();
}
function renderArchive() {
  const as = visible().filter(s => s.archived).sort((a, b) => b.updated - a.updated);
  patch(viewArch.firstElementChild!, '<p class="lead">归档的会话还能搜到，随时能拿回来。它们的 worktree 留着，删掉时才一起删。</p>'
    + (as.length ? `<div class="a-list">${as.map(s => `<div class="a-row">${star(s.id)}<span class="a-t"><b>${esc(s.title)}</b><span>${who(s.agent)}<span class="dot">·</span>${esc(s.project)}<span class="dot">·</span>${esc(s.summary)}<span class="dot">·</span>${age(s.updated)}</span></span>`
      + `<button type="button" class="btn" data-act="unarchive" data-id="${s.id}">拿回来</button><button type="button" class="btn${app.del === s.id ? ' bad-on' : ' bad'}" data-act="delete" data-id="${s.id}">${app.del === s.id ? s.tree ? '连 worktree 一起删' : '真的删掉' : '删除'}</button></div>`).join('')}</div>`
      : '<p class="empty">没有归档的会话。</p>'));
}
// Nothing open: a session starts from the slip (slip.ts), which an empty window drops by itself; this is what is under it.
function renderNew() {
  patch(viewNew.firstElementChild!, '<div class="n-empty"><p>没有开着的会话。写一句要它做什么，抛出去就开跑。</p><button type="button" class="btn" data-act="new">写一句 <kbd>⌘N</kbd></button></div>');
}

// ---------- the composer: stays in the page so what you type survives every redraw; only its parts change ----------
function choice(agent: Agent) { return app.catalog?.[agent] ?? { models: [], efforts: [], modes: [], always: '' }; }
const labelOf = (xs: [string, string][], v: string) => xs.find(x => x[0] === v)?.[1] ?? v;
function newDefaults() {
  const c = choice(app.newAgent);
  const pickOr = (v: string, xs: string[], d: string) => xs.includes(v) ? v : d;
  app.newSet.model = pickOr(store.get(`agents.${app.newAgent}.model`) ?? app.newSet.model, c.models.map(m => m[0]), c.models[0]?.[0] ?? '');
  app.newSet.effort = pickOr(store.get(`agents.${app.newAgent}.effort`) ?? app.newSet.effort, c.efforts, c.efforts.includes('high') ? 'high' : c.efforts[0] ?? '');
  app.newSet.mode = pickOr(store.get(`agents.${app.newAgent}.mode`) ?? app.newSet.mode, c.modes.map(m => m[0]), c.modes[0]?.[0] ?? '');
}
function renderComp() {
  const newV = app.view === 'new', s = newV ? undefined : cur(), agent = newV ? app.newAgent : s?.agent ?? 'claude', c = choice(agent);
  comp.hidden = app.view !== 'chat' || !s;
  if (comp.hidden) return;
  const busy = !!s && (s.st === 'work' || s.st === 'pack'), pend = s ? pendingReq(s.id) : undefined;
  const blocked = !!s && (!!s.term || (!!pend && pend.tool !== 'Ask' && pend.tool !== 'Plan'));
  ta.disabled = blocked || app.sending;
  const short = agent === 'codex' ? 'Codex' : 'Claude';
  ta.placeholder = newV ? `要 ${NAME[agent]} 做什么？` : s!.term ? '在终端里 · 拿回来才能在这里写' : blocked ? '先回答上面的请求'
    : pend?.tool === 'Ask' ? '打字回答它的问题' : pend?.tool === 'Plan' ? '哪里要改？写了再点「再想想」' : busy ? `给 ${short} 发消息 · 这一步做完它就会看到` : `给 ${short} 发消息`;
  const model = newV ? app.newSet.model : s!.model, effort = newV ? app.newSet.effort : s!.effort, mode = newV ? app.newSet.mode : s!.mode;
  // One row of quiet tools (the workbench composer): ＋ for pictures, files and commands, the mode; on the right the model
  // and its effort as one, the ring, and send.
  patch(tl, '<button type="button" class="tb plus" data-act="menu" data-v="plus" aria-label="添加" data-tip="图片、文件、命令">＋</button>'
    + (c.modes.length ? `<button type="button" class="tb mode" data-act="menu" data-v="mode" data-tip="它能自己做到哪一步"><i></i><span class="lbl">${esc(labelOf(c.modes, mode) || '模式')}</span></button>` : ''));
  const r = usage.ring(s, agent, app.usage, s ? app.cx.get(s.id) : undefined), eff = effort ? effort === 'xhigh' ? 'XHigh' : effort[0].toUpperCase() + effort.slice(1) : '';
  patch(tr, (c.models.length ? `<button type="button" class="tb model" data-act="menu" data-v="me" data-tip="模型和力度">${esc(labelOf(c.models, model) || '模型')}${eff ? ` <em>· ${esc(eff)}</em>` : ''}</button>` : '')
    + `<button type="button" class="ring" data-act="menu" data-v="usage" aria-label="用量" aria-haspopup="dialog" aria-expanded="${popFor === 'usage'}" data-tip="${esc(r.tip)}">${r.svg}</button>`
    + `${busy ? `<button type="button" class="t-stop" data-act="interrupt" data-tip="打断" data-key="esc">${I.stop}</button>` : ''}`
    + `<button type="button" class="send" data-act="send" aria-label="${newV ? '开始' : '发送'}" data-tip="${newV ? '开始' : '发送'}" data-key="↵"${blocked || app.sending || !ta.value.trim() && !app.files.length ? ' disabled' : ''}>${I.up}</button>`);
  // A plan window used up: one line in the box with the way on.
  const out = usage.banner(agent, app.usage);
  bnEl.hidden = !out;
  if (out) patch(bnEl, `<i></i><span>${out}</span>${agent === 'claude' && s ? '<button type="button" data-act="cloud">挪到云端继续</button>' : ''}`);
  patch(hintEl, wb.hint() + attention.hint());
  patch(cRows, s ? features.map(f => f.rows?.(s) ?? '').join('') : '');
  patch(cFiles, app.files.map((f, k) => `<span class="c-pic"><button type="button" class="pic" data-act="view" data-tip="看大图"><img src="${f.view}" alt="${esc(f.name)}"></button><i data-act="unfile" data-k="${k}" aria-label="去掉">✕</i></span>`).join(''));
  patch(cMenu, app.picks.map(([v, d], k) => app.menu === 'at'
    ? `<button type="button" data-act="pickfile" data-v="${esc(v)}"${k === app.pick ? ' class="on"' : ''}><code>@${esc(v)}</code></button>`
    : `<button type="button" data-act="pickcmd" data-v="${esc(v)}"${k === app.pick ? ' class="on"' : ''}><code>${esc(v)}</code><span>${esc(d)}</span></button>`).join(''));
  cMenu.classList.toggle('on', !!app.menu && app.picks.length > 0);
}
const pendingReq = (id: string) => (app.items.get(id)?.find(it => it.k === 'req' && !it.done) as (Item & { k: 'req' }) | undefined)?.req;

// ---------- one popover for every menu, kept in the page so it can ease in and out ----------
let popFor = '';
function openPop(kind: string, anchor: HTMLElement) {
  if (popFor === kind) { closePop(); return; }
  const s = app.view === 'chat' ? cur() : undefined, c = choice(s ? s.agent : app.newAgent);
  // The ring's card: the context window and the plan's windows, above the ring.
  if (kind === 'usage') {
    pop.className = 'pop us'; pop.setAttribute('role', 'dialog'); pop.setAttribute('aria-label', '用量');
    popFor = kind; H.delete(pop); fillUsage();
    const w = win.getBoundingClientRect(), r = anchor.getBoundingClientRect();
    Object.assign(pop.style, { left: `${Math.max(8, Math.min(w.width - 350, r.right - w.left - 340))}px`, right: 'auto', top: 'auto', bottom: `${w.bottom - r.top + 8}px`, transformOrigin: '100% 100%' });
    anchor.setAttribute('aria-expanded', 'true');
    pop.classList.add('on');
    if (s) void loadCtx(s.id);
    void loadUsage();
    return;
  }
  pop.className = 'pop'; pop.setAttribute('role', 'menu'); pop.removeAttribute('aria-label');
  const opts = (k: 'model' | 'effort' | 'mode', vs: [string, string][], v: string) => vs.map(([x, l]) => `<button type="button" data-act="set" data-k="${k}" data-v="${esc(x)}"${x === v ? ' class="on"' : ''}>${esc(l)}</button>`).join('');
  const cap = (e: string) => e === 'xhigh' ? 'XHigh' : e[0].toUpperCase() + e.slice(1);
  const html = kind === 'plus' ? `<button type="button" data-act="attach">加图片<span class="k">也可以直接粘贴</span></button><button type="button" data-act="insert" data-v="@">提到一个文件<span class="k">@</span></button><button type="button" data-act="insert" data-v="/">命令和 skill<span class="k">/</span></button>`
    : kind === 'me' ? `<span class="ph">模型</span>${opts('model', c.models, s ? s.model : app.newSet.model)}${c.efforts.length ? `<span class="sep"></span><span class="ph">力度</span>${opts('effort', c.efforts.map(e => [e, cap(e)]), s ? s.effort : app.newSet.effort)}` : ''}`
    : kind === 'more' && s
    ? `<button type="button" data-act="pin">${s.pinned ? '取消置顶' : '置顶'}</button><button type="button" data-act="park">${s.parked ? '不放着了' : '先放着'}</button><button type="button" data-act="rename">改名</button><button type="button" data-act="fork">从这里分叉</button>${features.map(f => f.more?.(s) ?? '').join('')}<button type="button" data-act="archive" data-id="${s.id}">归档</button><span class="sep"></span><button type="button" data-act="stop" class="bad">停掉</button>`
    : kind === 'model' ? opts('model', c.models, s ? s.model : app.newSet.model)
    : kind === 'effort' ? opts('effort', c.efforts.map(e => [e, e]), s ? s.effort : app.newSet.effort)
    : opts('mode', c.modes, s ? s.mode : app.newSet.mode);
  pop.innerHTML = html; popFor = kind;
  const w = win.getBoundingClientRect(), r = anchor.getBoundingClientRect(), down = kind === 'more';
  Object.assign(pop.style, down
    ? { left: 'auto', right: `${w.right - r.right}px`, top: `${r.bottom - w.top + 6}px`, bottom: 'auto', transformOrigin: '100% 0' }
    : { left: `${Math.min(r.left - w.left, w.width - 200)}px`, right: 'auto', top: 'auto', bottom: `${w.bottom - r.top + 6}px`, transformOrigin: '0 100%' });
  pop.classList.add('on');
}
function closePop() { if (!popFor) return; popFor = ''; pop.classList.remove('on'); win.querySelector('.ring')?.setAttribute('aria-expanded', 'false'); }
// A feature's lines in the same popover: under an element (to its right edge with `right`), or at a point in the window
// (a right click), kept inside the window.
function menu(html: string, at: HTMLElement | { x: number; y: number }, o: { right?: boolean; cls?: string } = {}) {
  closePop();
  pop.className = `pop${o.cls ? ` ${o.cls}` : ''}`; pop.setAttribute('role', 'menu'); pop.removeAttribute('aria-label');
  pop.innerHTML = html; H.delete(pop); popFor = 'feature';
  const w = win.getBoundingClientRect(), pw = pop.offsetWidth, ph = pop.offsetHeight;
  let x: number, y: number, origin: string;
  if (at instanceof HTMLElement) {
    const r = at.getBoundingClientRect(), below = r.bottom - w.top + 6 + ph < w.height - 8;
    x = o.right ? r.right - w.left - pw : r.left - w.left; y = below ? r.bottom - w.top + 6 : r.top - w.top - 6 - ph;
    origin = `${o.right ? '100%' : '0'} ${below ? '0' : '100%'}`;
  } else { x = at.x; y = at.y + ph > w.height - 8 ? at.y - ph : at.y; origin = '0 0'; }
  Object.assign(pop.style, { left: `${Math.max(8, Math.min(w.width - pw - 8, x))}px`, right: 'auto', top: `${Math.max(8, Math.min(w.height - ph - 8, y))}px`, bottom: 'auto', transformOrigin: origin });
  pop.classList.add('on');
}

// ---------- the context ring's popover: what fills the window, as the host measures it ----------
// Claude's numbers are /context's own token counts through the Agent SDK; Codex gives only totals, so its view is plainer.
const kt = (n: number) => n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${n >= 1e5 ? Math.round(n / 1000) : +(n / 1000).toFixed(1)}k` : String(Math.round(n));
const CX_COLOR: Record<string, string> = { 系统提示词: '#8fb1ff', 内置工具: '#c7a8ff', 'MCP 说明': '#7fd4e8', 'MCP 工具': '#7fd4e8', '自定义 agent': '#f2b596',
  记忆文件: '#ffc98f', Skills: '#6fe0b4', 对话: '#e8ebff', 发过去的: '#a9bfff', 它上一次写的: '#6fe0b4' };
async function loadCtx(id: string) {
  const r = await call<Ctx>(`/sessions/${id}/context`).catch((e: unknown) => e instanceof Error ? e.message : String(e));
  // A reading that fails after one that worked keeps the one that worked.
  if (typeof r !== 'string' || typeof app.cx.get(id) !== 'object') app.cx.set(id, r);
  if (popFor === 'usage' && app.cur === id) fillUsage();
  draw('comp');
}
// The plans as the daemon last read them; the daemon polls them every few minutes.
async function loadUsage() {
  app.usage = await call<Usage>('/usage').catch(() => app.usage);
  if (popFor === 'usage') fillUsage();
  draw('comp');
}
function fillUsage() {
  const s = app.view === 'chat' ? cur() : undefined;
  patch(pop, usage.card(s, s?.agent ?? app.newAgent, app.usage, s ? app.cx.get(s.id) : undefined, { open: app.cxMore, html: s ? ctxDetail(s) : '' }, CX_COLOR));
}
// What fills the context window, row by row, for the card's 都占了什么.
function ctxDetail(s: Sess) {
  const x = app.cx.get(s.id), m = labelOf(choice(s.agent).models, s.model) || (typeof x === 'object' ? x.model : '');
  if (typeof x !== 'object' || !x.max) return `<p class="cx-sub">${esc(m)}</p><p class="cx-say">${x === undefined ? '在量…' : typeof x === 'string' ? esc(x) : `<b>${esc(x.say[0])}</b>${esc(x.say[1])}`}</p>`;
  return `<p class="cx-sub">${esc(m)}</p><p class="cx-say"><b>${esc(x.say[0])}</b>${esc(x.say[1])}</p><div class="cx-rows">${x.rows.map(r => {
      const has = !!r.sub?.length, o = has && app.cxOpen.has(r.n), tag = has ? 'button' : 'div';
      return `<${tag}${has ? ` type="button" data-act="cxrow" aria-expanded="${o}"` : ''} class="cx-r${o ? ' open' : ''}" data-n="${esc(r.n)}"><i class="sw${r.kind ? ` ${r.kind}` : ''}"${r.kind ? '' : ` style="background:${CX_COLOR[r.n] ?? '#9aa3c7'}"`}></i><span>${esc(r.n)}</span><span class="n">${kt(r.t)}</span><span class="cv">${has ? I.chev : ''}</span></${tag}>`
        + (has ? `<div class="cx-sub-w${o ? ' open' : ''}"><div class="cx-sub-c"><div class="cx-sub-l">${r.sub!.map(e => typeof e === 'string' ? `<h5>${esc(e)}</h5>` : `<p><span title="${esc(e[0])}">${esc(e[0])}</span><em>${kt(e[1])}</em></p>`).join('')}</div></div></div>` : '');
    }).join('')}</div>`
    + (x.foot.length ? `<div class="cx-foot">${x.foot.map(l => `<span>${esc(l)}</span>`).join('')}</div>` : '');
}
// Pointing at a row lights its part of the bar.
pop.addEventListener('pointerover', e => {
  const r = (e.target as Element).closest<HTMLElement>('.cx-r'), bar = pop.querySelector<HTMLElement>('.cx-bar');
  if (!bar) return;
  bar.classList.toggle('hi', !!r);
  for (const i of bar.children) i.classList.toggle('on', !!r && (i as HTMLElement).dataset.n === r.dataset.n);
});
pop.addEventListener('pointerleave', () => pop.querySelector('.cx-bar')?.classList.remove('hi'));

// ---------- tooltips: 200 ms to appear, at once while another has just shown, a keycap for the shortcut (Hermes' timing) ----------
const tipEl = $('.tip', win);
let tipFor: HTMLElement | null = null, tipTimer = 0, tipGone = -1e9;
function tipShow(el: HTMLElement) {
  if (!el.isConnected || !el.dataset.tip) return;
  patch(tipEl, `<span>${esc(el.dataset.tip)}</span>${el.dataset.key ? `<kbd>${esc(el.dataset.key)}</kbd>` : ''}`);
  const w = win.getBoundingClientRect(), r = el.getBoundingClientRect(), t = tipEl.getBoundingClientRect(), below = r.top - w.top < t.height + 14;
  tipEl.style.left = `${Math.max(8, Math.min(w.width - t.width - 8, r.left - w.left + r.width / 2 - t.width / 2))}px`;
  tipEl.style.top = `${below ? r.bottom - w.top + 7 : r.top - w.top - t.height - 7}px`;
  tipEl.style.setProperty('--dy', below ? '-3px' : '3px');
  tipEl.classList.add('on');
}
function tipHide() {
  clearTimeout(tipTimer);
  if (tipEl.classList.contains('on')) tipGone = performance.now();
  tipFor = null; tipEl.classList.remove('on');
}
win.addEventListener('pointerover', e => {
  if (e.pointerType !== 'mouse') return;
  const el = (e.target as Element).closest<HTMLElement>('[data-tip]');
  if (el === tipFor) return;
  tipHide();
  if (!el) return;
  tipFor = el;
  tipTimer = window.setTimeout(() => tipShow(el), performance.now() - tipGone < 300 ? 0 : 200);
});
win.addEventListener('pointerdown', tipHide, true);
win.addEventListener('pointerleave', tipHide);
win.addEventListener('focusin', e => { const el = e.target as HTMLElement; if (el.dataset?.tip && el.matches(':focus-visible')) { tipFor = el; tipShow(el); } });
win.addEventListener('focusout', tipHide);

// ---------- what the host says ----------
const here = (id: string) => app.view === 'chat' && app.cur === id && document.hasFocus();
// A state change is where she and the sound answer: a finish chimes (softly if you are watching), a question asks.
// A change Allen caused himself (an interrupt, a stop) stays quiet.
const hush = new Map<string, number>();
function react(s: Sess, was: St) {
  if (s.st === was) return;
  attention.notify(s);
  stAt.set(s.id, performance.now());
  if ((hush.get(s.id) ?? 0) > performance.now() || s.parked) return;
  if (s.st === 'done' && was !== 'done') { cue('done', here(s.id) ? .45 : 1); herSay('fin', 2400, s.id); core.hop(performance.now(), .14); }
  if (s.st === 'wait') { cue('ask'); herSay('ask', 2800, s.id); }
  if (s.st === 'err') { cue('error'); herSay('34', 2600, s.id); core.effect('shake', performance.now()); }
}
async function loadItems(id: string) {
  const r = await call<{ items: Item[]; live: string | null }>(`/sessions/${id}`).catch(e => { toast(String(e instanceof Error ? e.message : e)); return null; });
  if (!r) return;
  app.items.set(id, r.items);
  if (r.live) app.live.set(id, r.live); else app.live.delete(id);
  const c = convs.get(id);
  if (c) c.built = false;
  touch(id);
}
let connected = false;
function apply(e: Event) {
  if (e.t === 'hello') {
    const first = !connected;
    connected = true; offEl.hidden = true; win.classList.remove('booting');
    app.ss = e.sessions; app.catalog = e.catalog;
    for (const s of app.ss) if (!stAt.has(s.id)) stAt.set(s.id, -1e9);
    // After a reconnect the host may have restarted: what this window holds is read again.
    for (const id of [...app.items.keys()]) { if (byId(id)) void loadItems(id); else app.items.delete(id); }
    // A companion that a landing restarted opens the window again on that session (ADR 0085).
    const back = new URLSearchParams(location.search).get('open');
    if (first) { newDefaults(); const o = order(); app.cur = back && byId(back) ? back : o[0] ?? ''; if (!app.cur) { app.view = 'new'; void act('new', win); } else { app.view = 'chat'; void loadItems(app.cur); } wb.switched(); }
    quiet = true; draw(); return;
  }
  if (e.t === 'catalog') { app.catalog = e.catalog; newDefaults(); draw('comp'); return; }
  if (e.t === 'gone') {
    app.ss = app.ss.filter(s => s.id !== e.id); app.items.delete(e.id); app.live.delete(e.id); convs.delete(e.id); rowEls.delete(e.id);
    if (app.cur === e.id) { const next = order()[0]; if (next) open(next); else { app.cur = ''; app.view = 'new'; wb.switched(); void act('new', win); } }
    draw(); return;
  }
  if (e.t === 'sess') {
    const i = app.ss.findIndex(s => s.id === e.s.id), was = i >= 0 ? app.ss[i].st : e.s.st;
    // Looking at it when it finishes is having seen it: it never goes to 「轮到你」.
    if (!attention.busy && e.s.unread && here(e.s.id) && e.s.st !== 'wait') { e.s.unread = false; void call(`/sessions/${e.s.id}/meta`, { seen: true }).catch(() => {}); }
    if (i >= 0) app.ss[i] = e.s; else app.ss.push(e.s);
    react(e.s, was);
    touch(e.s.id); wb.saw(e.s); return;
  }
  if (e.t === 'items') {
    const have = app.items.get(e.id);
    if (!have) return;
    if (e.from > have.length) { void loadItems(e.id); return; }
    have.splice(e.from, have.length - e.from, ...e.items);
    attention.invalidate(e.id);
    if (app.view === 'chat' && app.cur === e.id) draw('main', 'comp');
    return;
  }
  if (e.t === 'live') {
    if (e.text === null) app.live.delete(e.id); else app.live.set(e.id, e.text);
    if (app.view === 'chat' && app.cur === e.id) draw('live');
  }
}
function connect() {
  const es = new EventSource(`${API}/events`);
  es.onmessage = m => apply(JSON.parse(m.data) as Event);
  // The stream comes back by itself; until then the title bar says so.
  es.onerror = () => { offEl.hidden = false; };
}

// ---------- doing things ----------
// `end`: open at its latest answer, wherever it was last read (B01's way in).
function open(id: string, how: 'click' | 'key' = 'click', end = false) {
  const c = end ? convs.get(id) : undefined;
  if (c) c.scroll = -1;
  if (app.view === 'chat' && app.cur === id) { app.sideOpen = false; win.classList.remove('side-open'); if (c) toEnd(c.root); return; }
  app.cur = id; app.view = 'chat'; app.renaming = false; app.sideOpen = false; app.openAt = performance.now(); app.how = how;
  app.menu = ''; app.picks = [];
  closePop(); win.classList.remove('side-open');
  if (!app.items.has(id)) void loadItems(id);
  wb.switched();
  draw();
}
// `view` shows the picture in the composer without carrying its data: URL through every redraw.
type Attached = Upload & { view: string };
const unattach = (fs: Attached[]) => { for (const f of fs) URL.revokeObjectURL(f.view); };
const readFile = (f: Blob & { name?: string }, k: number) => new Promise<Attached>((done, fail) => {
  const r = new FileReader();
  r.onload = () => done({ name: f.name || `图片 ${k + 1}.png`, url: String(r.result), view: URL.createObjectURL(f) });
  r.onerror = () => fail(r.error);
  r.readAsDataURL(f);
});
// A window command (the host names its place) runs here with what follows it, when a feature answers that place.
function ownOf(text: string): [Own, string] | null {
  const m = /^([/$][^\s]+)(?:\s+([\s\S]*))?$/.exec(text), place = m && cmds?.list.find(c => c[0] === m[1])?.[2], run = place ? ctx.own.get(place) : undefined;
  return m && run ? [run, (m[2] ?? '').trim()] : null;
}
async function send() {
  const text = ta.value.trim();
  if ((!text && !app.files.length) || app.sending) return;
  const own = text && !app.files.length ? ownOf(text) : null;
  if (own) { clearTa(); app.menu = ''; app.picks = []; own[0](app.view === 'chat' ? cur() : undefined, own[1]); return; }
  if (app.view === 'new') {
    if (!app.newProject) { toast('先选一个文件夹'); return; }
    app.sending = true; draw('comp');
    const r = await tryCall('/sessions', { agent: app.newAgent, cwd: app.newProject, tree: app.newTree, text, files: app.files, ...app.newSet });
    app.sending = false;
    if (!r) { draw('comp'); return; }
    unattach(app.files); app.files = []; clearTa();
    cue('send'); herSay(pick(TAKES.receive), 1300, String(r.id)); core.hop(performance.now(), .12);
    store.set('agents.project', app.newProject);
    app.items.set(String(r.id), app.items.get(String(r.id)) ?? []);
    open(String(r.id));
    void loadItems(String(r.id));
    return;
  }
  const s = cur();
  if (!s || s.term) return;
  const pend = pendingReq(s.id);
  if (pend?.tool === 'Ask') { clearTa(); cue('send'); await tryCall(`/sessions/${s.id}/answer`, { req: pend.id, decision: 'allow', text }); return; }
  if (pend?.tool === 'Plan') { clearTa(); cue('close'); await tryCall(`/sessions/${s.id}/answer`, { req: pend.id, decision: 'deny', text }); return; }
  if (pend) return;
  const files = app.files; unattach(files); app.files = []; app.menu = ''; app.picks = []; clearTa();
  cue('send', s.st === 'work' ? .55 : .8);
  if (s.st !== 'work') herSay(pick(TAKES.receive), 1100);
  if (await tryCall(`/sessions/${s.id}/send`, { text, files })) attention.sent();
}
const clearTa = () => { ta.value = ''; ta.style.height = ''; draw('comp'); };
// Pressing an answer sounds at once and locks its card, a spinner on what was pressed, until the host has it; a second
// press or a held key does nothing. `key` is what was pressed: allow, always, deny, all, or opt:<label>.
async function answer(s: Sess, req: string, decision: 'allow' | 'always' | 'deny', key: string, answers?: string[][]) {
  if (app.busy.get(s.id)?.req === req) return;
  app.busy.set(s.id, { req, key });
  cue(decision === 'deny' ? 'close' : 'send');
  const text = decision === 'deny' ? ta.value.trim() : '';
  if (text) clearTa();
  draw('main');
  if (!await tryCall(`/sessions/${s.id}/answer`, { req, decision, answers, ...(text ? { text } : {}) })) { app.busy.delete(s.id); draw('main'); }
}
// Copying: the button says so for 1.5 s; where the clipboard is refused, the text is selected for ⌘C instead.
// A code block's button says it in words; an answer's is an icon that speaks only when the copy was refused.
function copy(el: HTMLElement, text: string, target: Element) {
  const words = el.classList.contains('cp'), say = (t: string) => words ? `<span>${t}</span>` : '';
  const done = (ok: boolean) => {
    el.classList.remove('ok', 'no'); el.classList.add(ok ? 'ok' : 'no');
    patch(el, ok ? `${I.check}${say('复制好了')}` : `${I.copy}<span>选好了，按 ⌘C</span>`);
    clearTimeout(Number(el.dataset.t));
    el.dataset.t = String(setTimeout(() => { el.classList.remove('ok', 'no'); patch(el, `${I.copy}${say('复制')}`); }, 1500));
  };
  tick();
  navigator.clipboard.writeText(text).then(() => done(true), () => {
    const r = document.createRange(); r.selectNodeContents(target);
    const sel = getSelection(); sel?.removeAllRanges(); sel?.addRange(r);
    done(false);
  });
}
// Esc while you are writing: the turn goes on, and the now-line says why.
function escHint(s: Sess) {
  const c = convs.get(s.id);
  if (!c) return;
  const k = $('.k', c.now), kb = $('kbd', c.now);
  k.textContent = '输入框里有字，没打断 · 清空再按'; k.classList.add('warn');
  anim(kb, [{ transform: 'none' }, { transform: 'translateX(-3px)' }, { transform: 'translateX(3px)' }, { transform: 'translateX(-2px)' }, { transform: 'none' }], 320);
  clearTimeout(Number(k.dataset.t));
  k.dataset.t = String(setTimeout(() => { k.textContent = '打断'; k.classList.remove('warn'); }, 2200));
}
function interrupt(s: Sess) {
  hush.set(s.id, performance.now() + 2500);
  cue('interrupt'); core.effect('jolt', performance.now());
  void tryCall(`/sessions/${s.id}/interrupt`, {});
}
// Archiving folds the row away first, then the list closes over it.
function archive(s: Sess) {
  const el = rowEls.get(s.id);
  hush.set(s.id, performance.now() + 2500);
  const commit = () => {
    s.archived = true; s.pinned = false;
    if (app.cur === s.id && app.view === 'chat') { const next = order().find(id => id !== s.id); if (next) open(next); else app.view = 'archive'; }
    draw();
    void tryCall(`/sessions/${s.id}/meta`, { archived: true });
  };
  cue('close', .8);
  if (el?.isConnected && !reduced.matches) {
    el.style.pointerEvents = 'none';
    el.animate([{ height: `${el.offsetHeight}px`, opacity: 1, transform: 'none' }, { height: '0px', opacity: 0, paddingTop: '0px', paddingBottom: '0px', transform: 'translateX(-10px)' }],
      { duration: 260, easing: 'cubic-bezier(.4,0,.2,1)', fill: 'forwards' }).onfinish = commit;
  } else commit();
}
async function act(a: string, el: HTMLElement) {
  const id = el.dataset.id ?? app.cur, s = byId(id);
  if (wb.act(a, el)) return;
  for (const f of features) if (f.act?.(a, el)) return;
  if (a === 'open') open(id);
  else if (a === 'cxmore') { app.cxMore = !app.cxMore; fillUsage(); }
  else if (a === 'usagepage') { closePop(); void window.agents?.openUrl?.((s?.agent ?? app.newAgent) === 'codex' ? 'https://chatgpt.com/codex/settings/usage' : 'https://claude.ai/settings/usage'); }
  // 挪到云端继续: the session's work goes on in a Claude Code cloud session, started from its folder in Ghostty.
  else if (a === 'cloud' && s) {
    closePop();
    const text = `接着 Jarvis 里的会话「${s.title}」做下去。它停在：${s.summary}。仓库 ${s.project}，分支 ${s.branch}。`.slice(0, 590);
    toast(await window.agents?.cloud?.(s.cwd, text) ? '在 Ghostty 里开了一个云端会话' : `没能打开 Ghostty。在终端里跑：cd ${home(s.cwd)} && claude --cloud "…"`);
  }
  else if (a === 'next') {
    const o = order().filter(x => yourTurn(byId(x)!));
    core.hop(performance.now(), .16);
    if (o.length) open(o[(o.indexOf(app.cur) + 1) % o.length]);
  }
  else if (a === 'archview') { app.view = 'archive'; app.sideOpen = false; closePop(); win.classList.remove('side-open'); wb.switched(); draw(); }
  else if (a === 'filter') { if (app.filter !== el.dataset.v) tick(); app.filter = el.dataset.v as typeof app.filter; quiet = true; draw('side'); }
  else if (a === 'by') { tick(); app.by = app.by === 'state' ? 'project' : 'state'; quiet = true; draw('side'); }
  else if (a === 'pin' && s) { s.pinned = !s.pinned; closePop(); cue(s.pinned ? 'on' : 'off', .7); draw(); void tryCall(`/sessions/${s.id}/meta`, { pinned: s.pinned }); }
  else if (a === 'archive' && s) { closePop(); archive(s); }
  else if (a === 'park' && s) { s.parked = !s.parked; closePop(); cue(s.parked ? 'close' : 'open', .6); draw(); void tryCall(`/sessions/${s.id}/meta`, { parked: s.parked }); }
  else if (a === 'unarchive' && s) { s.archived = false; cue('open', .8); draw(); void tryCall(`/sessions/${s.id}/meta`, { archived: false }); }
  else if (a === 'delete' && s) {
    if (app.del !== id) { app.del = id; draw('main'); return; }
    app.del = '';
    const ok = await tryCall(`/sessions/${id}`, undefined, 'DELETE');
    if (ok) cue('close');
    draw();
  }
  else if (a === 'rename') { app.renaming = true; closePop(); draw('head'); }
  else if (a === 'fork' && s) { closePop(); const r = await tryCall(`/sessions/${s.id}/fork`, {}); if (r) { cue('open', .7); open(String(r.id)); } }
  else if (a === 'reveal' && s) { closePop(); void window.agents?.reveal(s.cwd); }
  else if (a === 'stop' && s) { closePop(); hush.set(s.id, performance.now() + 2500); cue('interrupt'); void tryCall(`/sessions/${s.id}/stop`, {}); }
  else if (a === 'terminal' && s) {
    if (s.term) { void act('takeback', el); return; }
    const r = await tryCall(`/sessions/${s.id}/release`, {});
    if (!r) return;
    cue('close', .6);
    if (!await window.agents?.terminal(String(r.cwd), String(r.cmd))) toast(`没能打开 Ghostty。在终端里跑：cd ${home(String(r.cwd))} && ${r.cmd}`);
  }
  else if (a === 'takeback' && s) { cue('open', .8); await tryCall(`/sessions/${s.id}/takeback`, {}); }
  else if (a === 'steps' || a === 'step') {
    const m = app.opened.get(app.cur) ?? new Map<number, Open>(), i = Number(el.dataset.i), o = m.get(i) ?? {};
    if (a === 'steps') { o.open = !o.open; o.step = undefined; } else { const j = Number(el.dataset.j); o.step = o.step === j ? undefined : j; }
    m.set(i, o); app.opened.set(app.cur, m); draw('main');
  }
  else if ((a === 'allow' || a === 'always' || a === 'deny') && s) void answer(s, el.dataset.req!, a, a);
  else if (a === 'answer' && s) void answer(s, el.dataset.req!, 'allow', `opt:${el.dataset.v}`, [[el.dataset.v!]]);
  else if (a === 'pickopt' && s) {
    tick();
    const req = el.dataset.req!, qi = Number(el.dataset.q), v = el.dataset.v!, r = pendingReq(s.id);
    const picked = app.asked.get(req) ?? [], multi = r?.tool === 'Ask' && r.qs[qi]?.multi;
    const now = picked[qi] ?? [];
    picked[qi] = multi ? (now.includes(v) ? now.filter(x => x !== v) : [...now, v]) : [v];
    app.asked.set(req, picked); draw('main');
  }
  else if (a === 'answerall' && s) { const req = el.dataset.req!; void answer(s, req, 'allow', 'all', app.asked.get(req)); app.asked.delete(req); }
  else if (a === 'copy' && s) {
    const code = el.dataset.what === 'code', item = el.closest('.item')!, it = app.items.get(s.id)?.[[...item.parentElement!.children].indexOf(item)];
    const target = code ? el.nextElementSibling! : item.querySelector('.md')!;
    copy(el, code || it?.k !== 'it' ? target.textContent ?? '' : it.text, target);
  }
  else if (a === 'cxrow') {
    const n = el.dataset.n!, o = !app.cxOpen.has(n);
    if (o) app.cxOpen.add(n); else app.cxOpen.delete(n);
    el.classList.toggle('open', o); el.setAttribute('aria-expanded', String(o)); el.nextElementSibling?.classList.toggle('open', o);
  }
  else if (a === 'interrupt' && s) interrupt(s);
  else if (a === 'send') void send();
  else if (a === 'menu') openPop(el.dataset.v!, el);
  else if (a === 'set') {
    const k = el.dataset.k as 'model' | 'effort' | 'mode', v = el.dataset.v!;
    closePop(); tick();
    if (app.view === 'new') { app.newSet[k] = v; store.set(`agents.${app.newAgent}.${k}`, v); draw('comp'); }
    else if (s && s[k] !== v) { await tryCall(`/sessions/${s.id}/set`, { key: k, value: v }); }
  }
  else if (a === 'insert') { ta.value += (ta.value && !/\s$/.test(ta.value) && el.dataset.v === '@' ? ' ' : '') + el.dataset.v; ta.focus(); typed(); }
  else if (a === 'pickcmd' || a === 'pickfile') { tick(); pickIt(el.dataset.v!, a === 'pickcmd'); }
  else if (a === 'attach') $<HTMLInputElement>('#file').click();
  else if (a === 'unfile') { unattach(app.files.splice(Number(el.dataset.k), 1)); draw('comp'); }
  else if (a === 'side') { app.sideOpen = !app.sideOpen; win.classList.toggle('side-open', app.sideOpen); }
  else if (a === 'sound') setSound(!snd.on);
}
function setSound(on: boolean) {
  if (!on) cue('speakerOff', 1, true);
  snd.on = on; store.set('agents.sound', on ? 'on' : 'off');
  if (on) cue('speakerOn', 1, true);
  sndBtn.innerHTML = on ? I.sound : I.mute;
  sndBtn.setAttribute('aria-pressed', String(on)); sndBtn.dataset.tip = on ? '声音开着 · 点一下关' : '声音关了 · 点一下开';
}
function pickIt(v: string, isCmd: boolean) {
  if (isCmd) ta.value = `${v} `;
  else ta.value = ta.value.slice(0, ta.value.lastIndexOf('@')) + `@${v} `;
  app.menu = ''; app.pick = 0; app.picks = []; ta.focus(); draw('comp');
}
// Typing "/" at the start opens commands and skills; "@" opens files. Arrows and Enter pick from the list.
let lookup = 0, cmds: { key: string; list: [string, string, string?][] } | null = null;
async function typed() {
  const v = ta.value, n = ++lookup;
  app.menu = /^[/$]\S*$/.test(v) ? 'slash' : /(^|\s)@[^\s]*$/.test(v) ? 'at' : '';
  app.pick = 0;
  ta.style.height = 'auto'; ta.style.height = `${Math.min(180, ta.scrollHeight)}px`;
  const s = app.view === 'chat' ? cur() : undefined, where = s ? `id=${encodeURIComponent(s.id)}` : `agent=${app.newAgent}&cwd=${encodeURIComponent(app.newProject)}`;
  if (app.menu === 'slash') {
    if (cmds?.key !== where) cmds = { key: where, list: (await call<{ commands: [string, string, string?][] }>(`/commands?${where}`).catch(() => ({ commands: [] }))).commands };
    if (n !== lookup) return;
    const q = v.split(/\s/)[0];
    app.picks = cmds.list.filter(c => c[0].startsWith(q)).slice(0, 60).map(c => [c[0], c[1]]);
  } else if (app.menu === 'at') {
    const q = v.slice(v.lastIndexOf('@') + 1);
    const r = await call<{ files: string[] }>(`/files?${where}&q=${encodeURIComponent(q)}`).catch(() => ({ files: [] }));
    if (n !== lookup) return;
    app.picks = r.files.map(f => [f, '']);
  } else app.picks = [];
  draw('comp');
}
async function refreshProjects() {
  const r = await call<{ projects: string[] }>('/projects').catch(() => null);
  if (!r) return;
  app.projects = r.projects;
  if (!app.newProject) app.newProject = app.projects[0] ?? '';
  if (app.view === 'new') draw('main');
}

// ---------- wiring ----------
win.addEventListener('click', e => {
  const t = e.target as Element, el = t.closest<HTMLElement>('[data-act]');
  // The session list laid over the conversation folds away once you click past it.
  if (app.sideOpen && !t.closest('.side,.pop,[data-act="side"]')) { app.sideOpen = false; win.classList.remove('side-open'); }
  if (popFor && !t.closest('.pop') && el?.dataset.act !== 'menu') closePop();
  if (el && !(el as HTMLButtonElement).disabled) void act(el.dataset.act!, el);
});
win.addEventListener('keydown', e => {
  const t = e.target as HTMLElement;
  if (t.id === 'rename') {
    const s = cur();
    if (e.key === 'Enter' && !e.isComposing) { const v = (t as HTMLInputElement).value.trim(); if (v && s) { s.title = v; void tryCall(`/sessions/${s.id}/meta`, { title: v }); } app.renaming = false; draw(); }
    if (e.key === 'Escape') { app.renaming = false; draw('head'); }
    return;
  }
  if (t === ta) {
    const menuOpen = !!app.menu, n = cMenu.children.length;
    if (menuOpen && n && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) { e.preventDefault(); app.pick = (app.pick + (e.key === 'ArrowDown' ? 1 : -1) + n) % n; draw('comp'); return; }
    if (menuOpen && n && (e.key === 'Enter' || e.key === 'Tab') && !e.isComposing) { e.preventDefault(); const b = cMenu.children[app.pick] as HTMLElement; tick(); pickIt(b.dataset.v!, b.dataset.act === 'pickcmd'); return; }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); void send(); return; }
  }
  if ((e.key === 'Enter' || e.key === ' ') && t.matches('[role="button"]')) { e.preventDefault(); void act(t.dataset.act!, t); }
});
addEventListener('keydown', e => {
  const t = e.target as HTMLElement, s = app.view === 'chat' ? cur() : undefined;
  if (t.id !== 'rename') {
    // A card waiting on you takes the keys unless you are typing: Enter allows, Esc says no, digits pick an answer. Enter
    // on a focused button is that button's (the open session's own row aside), and a held key does nothing more.
    const r = s && !s.term && !popFor && !app.menu ? pendingReq(s.id) : undefined;
    const typing = (t === ta && !!ta.value.trim()) || t.tagName === 'INPUT' || t.tagName === 'SELECT';
    if (s && r && !typing && !e.metaKey && !e.ctrlKey && !e.altKey && !e.isComposing) {
      const opt = r.tool === 'Ask' && r.qs.length === 1 && !r.qs[0].multi && /^[1-9]$/.test(e.key) ? r.qs[0].opts[Number(e.key) - 1] : undefined;
      const enter = e.key === 'Enter' && !e.shiftKey && r.tool !== 'Ask' && !t.closest('button,[role="button"]:not(.row.is-on)');
      if (opt || enter || e.key === 'Escape') {
        e.preventDefault();
        if (!e.repeat) void answer(s, r.id, opt || enter ? 'allow' : 'deny', opt ? `opt:${opt[0]}` : enter ? 'allow' : 'deny', opt ? [[opt[0]]] : undefined);
        return;
      }
    }
    // Esc: close a menu or the session list first, otherwise interrupt the turn on screen; a message you are writing is never lost to it.
    if (e.key === 'Escape') {
      if (popFor) { closePop(); return; }
      if (app.sideOpen) { app.sideOpen = false; win.classList.remove('side-open'); return; }
      if (app.menu) { app.menu = ''; app.picks = []; draw('comp'); return; }
      if (features.some(f => f.esc?.()) || wb.esc()) { e.preventDefault(); return; }
      if (s && (s.st === 'work' || s.st === 'pack')) { e.preventDefault(); if (t === ta && ta.value.trim()) escHint(s); else interrupt(s); }
      return;
    }
  }
  const mod = e.metaKey || e.ctrlKey;
  // ⌘⏎ with nothing being written: land this session.
  if (mod && e.key === 'Enter' && !e.shiftKey && !e.isComposing && app.view === 'chat' && (t !== ta || !ta.value.trim())) { e.preventDefault(); wb.land(); return; }
  if (mod && e.key.toLowerCase() === 'k') { e.preventDefault(); if (attention.enabled) { app.sideOpen = true; win.classList.add('side-open'); } find.focus(); find.select(); }
  if (mod && e.key.toLowerCase() === 'n') { e.preventDefault(); void act('new', $('.new', side)); }
  if ((mod || e.altKey) && (e.key === 'ArrowDown' || e.key === 'ArrowUp') && document.activeElement !== ta) {
    const o = order(), i = o.indexOf(app.cur);
    if (o.length) { e.preventDefault(); open(o[Math.max(0, Math.min(o.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)))], 'key'); }
  }
});
find.addEventListener('input', () => { app.q = find.value; quiet = true; draw('side'); if (app.view === 'archive') draw('main'); });
ta.addEventListener('input', () => { void typed(); });
win.addEventListener('change', async e => {
  const t = e.target as HTMLInputElement;
  if (t.id === 'file' && t.files) { const fs = [...t.files]; t.value = ''; app.files.push(...await Promise.all(fs.map(readFile))); draw('comp'); }
});
ta.addEventListener('paste', async e => {
  const imgs = [...(e.clipboardData?.files ?? [])].filter(f => f.type.startsWith('image/'));
  if (!imgs.length) return;
  e.preventDefault();
  app.files.push(...await Promise.all(imgs.map(readFile)));
  draw('comp');
});
// Her eyes follow the pointer when it is near her; pressing her squashes her a little.
win.addEventListener('pointermove', e => { her.ptr = [e.clientX, e.clientY]; });
win.addEventListener('pointerleave', () => { her.ptr = null; her.pressed = false; });
herCv.addEventListener('pointerdown', () => { her.pressed = true; });
addEventListener('pointerup', () => { her.pressed = false; });
herCv.addEventListener('click', () => { void act('next', herCv); });

// Features take keys before the workbench and the Long Exposure, so what one of them has open keeps them.
addEventListener('keydown', e => { for (const f of features) if (f.key?.(e)) return; }, true);
const wb = mountWorkbench(win, ta, {
  api: API, call, toast, cue: (name, gain) => cue(name, gain, false, false),
  current: () => app.view === 'chat' ? cur() : undefined, chat: () => app.view === 'chat' && !!cur(),
  busy: () => attention.busy, b01: () => attention.enabled, md, diff: diffHTML, redraw: () => draw('comp'),
});
const attention = mountExposure(win, ta, {
  sessions: () => app.ss, items: id => app.items.get(id), current: () => app.cur, chat: () => app.view === 'chat',
  load: loadItems, open: id => open(id, 'key', true), call, md, toast, cue: (name, gain) => cue(name, gain, false, false), blip, changed: id => stAt.get(id) ?? -1e9,
  refresh: () => draw(), back: () => wb.back(), menu, closeMenu: closePop,
});
const ctx: PageCtx = {
  win, ta, api: API, sessions: () => app.ss, current: () => app.view === 'chat' ? cur() : undefined, byId, items: id => app.items.get(id), chat: () => app.view === 'chat' && !!cur(),
  call, tryCall, load: loadItems, draw, open: id => open(id), toast, cue: (name, gain) => cue(name, gain, false, false), tick, md, diff: diffHTML,
  menu, closeMenu: closePop, wb, own: new Map(),
  catalog: () => app.catalog,
};
// Each feature is mounted on the one context below; its clicks, keys, commands and menu lines are its own.
// The picture viewer and the key sheet hold every key while they are open, so they come first.
features.push(mountViewer(ctx));
features.push(mountKeys(ctx));
features.push(mountStopped(ctx));
features.push(mountSlip(ctx));
features.push(mountHist(ctx));
features.push(mountBang(ctx));
features.push(mountSee(ctx));

// ---------- one loop: her every frame, moving marks at 30 fps, nothing while the window is out of sight ----------
let lastT = performance.now(), lastMk = 0, lastAge = 0;
function loop(now: number) {
  const dt = Math.min(.05, (now - lastT) / 1000);
  lastT = now;
  if (!document.hidden) {
    if (attention.enabled) attention.frame(now, dt); else herFrame(now, dt);
    if (now - lastMk > 33) {
      lastMk = now; paintMarks(now);
      // Looking at a finished one for 1.5 s reads it, as in the island.
      const s = app.view === 'chat' ? cur() : undefined;
      if (!attention.busy && s?.unread && now - app.openAt > 1500 && document.hasFocus()) { s.unread = false; draw('side'); void call(`/sessions/${s.id}/meta`, { seen: true }).catch(() => {}); }
      const w = s && (s.st === 'work' || s.st === 'pack') ? convs.get(s.id) : undefined;
      if (w && s?.since) patch($('.el', w.now), `· ${ago(s.since)}`);
    }
    // Ages in the list move on by themselves.
    if (now - lastAge > 30000) { lastAge = now; draw('side'); }
  }
  requestAnimationFrame(loop);
}
setSound(snd.on);
connect(); void refreshProjects(); void loadUsage();
// A notification the companion showed opens its session here (A6).
window.agents?.onOpen?.(id => { if (byId(id)) open(id); });
setInterval(() => { if (!document.hidden) void loadUsage(); }, 60000);
draw(); requestAnimationFrame(loop);

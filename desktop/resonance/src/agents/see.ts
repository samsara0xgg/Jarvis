// What it is doing (看它在干什么): its thinking as a line of its own that opens on the whole thought, a sub-agent as one
// row that opens on the right with its own 停, its background work in a row above the composer, the files in its steps
// as buttons, a file menu on a right click of any file in the window, paths in answers that link only when the host
// finds them, a name that fades in when Claude Code gives the session one, and 打开 in the title's ··· menu.
import type { Sess, Step, Task } from '../../electron/agents/types';
import type { Feature, PageCtx } from './ctx';
import { classify, stripLine } from './workbench/refs';
import './see.css';

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const anim = (el: Element, kf: Keyframe[], ms: number) => reduced.matches ? null : el.animate(kf, { duration: ms, easing: 'cubic-bezier(.23,1,.32,1)' });
const STOP = '<svg viewBox="0 0 16 16" width="11" height="11" fill="currentColor" aria-hidden="true"><rect x="3.5" y="3.5" width="9" height="9" rx="2"/></svg>';
const CHEV = '<svg class="see-chev" viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6l4 4 4-4"/></svg>';
const K: Record<Step['k'], string> = { read: '读', edit: '改', bash: '跑', search: '搜', agent: '子任务', web: '网页', tool: '工具', say: '', think: '想' };
const VERB: Partial<Record<Step['k'], string>> = { read: '在读', edit: '在改', bash: '在跑', search: '在搜', web: '在查网页', tool: '在用', agent: '在派', think: '在想', say: '在写' };

// ---------- time: running clocks tick in place ----------
const mmss = (ms: number) => {
  const t = Math.max(0, Math.floor(ms / 1000)), h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = String(t % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
};
const secs = (ms: number) => Math.max(1, Math.round(ms / 1000));
const took = (ms: number) => ms < 60000 ? `${secs(ms)} 秒` : ms < 3600000 ? `${Math.round(ms / 60000)} 分钟` : `${(ms / 3600000).toFixed(1)} 小时`;
// `s`: seconds (想 · 12 秒); otherwise m:ss.
const clock = (t0: number, f = '') => `<em data-see-t0="${t0}"${f ? ` data-see-f="${f}"` : ''}>${f === 's' ? `${secs(Date.now() - t0)} 秒` : mmss(Date.now() - t0)}</em>`;
// A panel is redrawn only when more than its clocks changed.
const still = (html: string) => html.replace(/(<em data-see-t0="\d+"[^>]*>)[^<]*/g, '$1');
// Bring `el` to `html` changing only what differs, so the button under the pointer stays the same element.
function morph(el: Node, to: Node) {
  const a = [...el.childNodes], b = [...to.childNodes];
  b.forEach((n, k) => {
    const o = a[k];
    if (o && o.nodeType === n.nodeType && o.nodeName === n.nodeName) {
      if (o.nodeType !== 1) { if (o.nodeValue !== n.nodeValue) o.nodeValue = n.nodeValue; return; }
      const oe = o as Element, ne = n as Element;
      for (const at of [...oe.attributes]) if (!ne.hasAttribute(at.name)) oe.removeAttribute(at.name);
      for (const at of [...ne.attributes]) if (oe.getAttribute(at.name) !== at.value) oe.setAttribute(at.name, at.value);
      morph(o, n);
    } else if (o) el.replaceChild(n.cloneNode(true), o);
    else el.appendChild(n.cloneNode(true));
  });
  for (const o of a.slice(b.length)) o.remove();
}

// ---------- paths the host finds (POST /resolve), per folder; one it does not find is asked again after a minute ----------
type Hit = { abs: string; dir: boolean; line?: number };
const EXT = /\.[A-Za-z][A-Za-z0-9]{0,7}(?::\d+(?::\d+)?(?:-\d+)?|#L\d+)?(?![A-Za-z0-9])/g;
const ENDS = /\.[A-Za-z][A-Za-z0-9]{0,7}(?::\d+(?::\d+)?(?:-\d+)?|#L\d+)?$/;
const STOPS = '，。、；：！？（）()[]{}「」『』《》<>"“”‘’\'`,;!?|';
const cjk = (ch?: string) => !!ch && /[⺀-鿿豈-﫿＀-￯　-〿]/.test(ch);
const isUrl = (r: string) => /^[a-z][a-z0-9+.-]*:\/\//i.test(r) && !/^file:/i.test(r);

export function mountSee(ctx: PageCtx): Feature {
  const { win } = ctx;
  const known = new Map<string, Map<string, { hit: Hit | null; at: number }>>();
  const want = new Map<string, Set<string>>(), asking = new Set<string>();
  let ver = 0, askTimer = 0;
  const made = new Map<string, string>();
  function look(s: Sess, ref: string): Hit | null | undefined {
    const k = known.get(s.cwd)?.get(ref);
    if (k && (k.hit || Date.now() - k.at < 60000)) return k.hit;
    if (!asking.has(`${s.cwd}\u0000${ref}`)) {
      let w = want.get(s.id); if (!w) want.set(s.id, w = new Set());
      w.add(ref);
      if (!askTimer) askTimer = window.setTimeout(ask, 30);
    }
    return k?.hit;
  }
  // Every path waiting to be looked up, in one request per session (at most 200 each); drawing never waits for it.
  function ask() {
    askTimer = 0;
    for (const [id, refs] of want) {
      want.delete(id);
      const s = ctx.byId(id);
      if (!s) continue;
      const list = [...refs].slice(0, 200), cwd = s.cwd;
      list.forEach(r => asking.add(`${cwd}\u0000${r}`));
      void ctx.call<{ found: Record<string, Hit> }>(`/resolve?id=${encodeURIComponent(id)}`, { refs: list }).then(r => {
        let m = known.get(cwd), moved = false;
        if (!m) known.set(cwd, m = new Map());
        for (const ref of list) {
          const hit = r.found[ref] ?? null, was = m.get(ref)?.hit;
          if (!!hit !== !!was || (hit && was && hit.abs !== was.abs)) moved = true;
          m.set(ref, { hit, at: Date.now() });
        }
        if (moved) { ver++; ctx.draw('main'); paintShown(); }
      }, () => {}).finally(() => list.forEach(r => asking.delete(`${cwd}\u0000${r}`)));
    }
  }
  const absOf = (s: Sess, ref: string) => {
    const h = look(s, ref);
    if (h) return h.abs;
    const p = stripLine(ref.replace(/^file:\/\//, ''));
    return p.startsWith('/') ? p : `${s.cwd.replace(/\/$/, '')}/${p.replace(/^\.\//, '')}`;
  };

  // An answer's paths: a path in code, a link to a file, or a path in the text is a link only when the host found it;
  // one it did not find (or has not yet) stays as written.
  function candidates(text: string) {
    const out: { from: number; to: number; list: string[] }[] = [];
    for (const m of text.matchAll(EXT)) {
      const end = m.index! + m[0].length;
      let lo = m.index!;
      while (lo > 0 && !STOPS.includes(text[lo - 1]) && text[lo - 1] !== '\n') lo--;
      const starts: number[] = [];
      for (let st = lo; st < m.index!; st++) if (!/\s/.test(text[st]) && (st === lo || /\s/.test(text[st - 1]) || cjk(text[st - 1]))) starts.push(st);
      const list = starts.slice(-4).map(st => text.slice(st, end)).filter(c => c.length <= 240 && !c.includes('://'));
      if (list.length) out.push({ from: end - list[0].length, to: end, list });
    }
    return out;
  }
  function linkText(node: Text, s: Sess) {
    const text = node.nodeValue ?? '', parts: (string | { text: string; hit: Hit })[] = [];
    let done = 0;
    for (const c of candidates(text)) {
      if (c.from < done) continue;
      const pick = c.list.map(t => ({ t, hit: look(s, t) })).find(x => x.hit);
      if (!pick) continue;
      const at = c.to - pick.t.length;
      parts.push(text.slice(done, at), { text: pick.t, hit: pick.hit! }); done = c.to;
    }
    if (!done) return;
    const frag = document.createDocumentFragment();
    for (const p of [...parts, text.slice(done)]) {
      if (typeof p === 'string') { if (p) frag.append(p); continue; }
      const a = document.createElement('a');
      a.className = 'ref see-p'; a.dataset.act = 'peek'; a.dataset.ref = p.text; a.title = p.hit.abs; a.textContent = p.text;
      frag.append(a);
    }
    node.replaceWith(frag);
  }
  function walk(node: Node, s: Sess) {
    for (const n of [...node.childNodes]) {
      if (n.nodeType === 3) { linkText(n as Text, s); continue; }
      if (n.nodeType !== 1) continue;
      const el = n as HTMLElement;
      if (el.matches('pre,.code,button,svg,[data-x]')) continue;
      if (el.tagName === 'A') {
        const ref = el.dataset.ref;
        if (!el.classList.contains('ref') || !ref || isUrl(ref)) continue;
        const hit = look(s, ref);
        if (hit) { el.classList.add('see-p'); el.title = hit.abs; } else el.replaceWith(...el.childNodes);
        continue;
      }
      if (el.tagName === 'CODE') {
        const ref = el.dataset.ref ?? (ENDS.test(el.textContent ?? '') && !/\n/.test(el.textContent ?? '') ? (el.textContent ?? '').trim() : '');
        const hit = ref ? look(s, ref) : undefined;
        if (hit) { el.className = 'ref see-p'; el.dataset.act = 'peek'; el.dataset.ref = ref; el.title = hit.abs; }
        else if (el.dataset.ref) { el.removeAttribute('class'); el.removeAttribute('data-act'); el.removeAttribute('data-ref'); }
        continue;
      }
      walk(el, s);
    }
  }
  function linkPaths(s: Sess, html: string) {
    const key = `${s.id}\u0000${ver}\u0000${html}`, had = made.get(key);
    if (had !== undefined) return had;
    const t = document.createElement('template');
    t.innerHTML = html; walk(t.content, s);
    const out = t.innerHTML;
    if (made.size > 500) made.clear();
    made.set(key, out);
    return out;
  }

  // ---------- the steps: 想, a sub-agent, and the files they name ----------
  const itemOf = (s: Sess, i: number) => ctx.items(s.id)?.[i];
  const liveAt = (s: Sess, i: number) => { const it = itemOf(s, i); return !!it && it.k === 'steps' && !!it.live; };
  const thinking = (s: Sess, st: Step, i: number) => st.k === 'think' && st.ms === undefined && !st.out && liveAt(s, i);
  const thinkOpen = new Set<string>();
  const fileBtn = (ref: string) => `<button type="button" class="see-f${onFile(ref) ? ' on' : ''}" data-act="peek" data-ref="${esc(ref)}">${esc(ref)}</button>`;
  const onFile = (ref: string) => { const k = ctx.wb.shown(); return !!k && classify(ref).key === k; };
  // A step's argument with the file it names as a button: Read and Edit name a file, Grep what it looked for and where.
  function fileArg(st: Step) {
    if ((st.k === 'read' || st.k === 'edit') && st.t) return fileBtn(st.t);
    const m = st.k === 'search' ? /^(".*") in (.+)$/.exec(st.t) : null;
    return m ? `${esc(m[1])} in ${fileBtn(m[2])}` : undefined;
  }
  function thinkArg(s: Sess, st: Step, i: number) {
    if (thinking(s, st, i)) return `<span class="see-th run">在想 · ${clock(st.at ?? Date.now(), 's')}</span>`;
    return `<span class="see-th">${st.ms && st.ms >= 1000 ? `想了 ${secs(st.ms)} 秒` : '想了一下'}</span>`;
  }
  const thoughtBox = (text: string) => `<div class="see-tb" data-act="seetb">${ctx.md(text)}</div>`;

  // A sub-agent: how far it is, as its task and its step say.
  type SubSt = 'run' | 'done' | 'stop' | 'fail';
  function subState(s: Sess, st: Step, i: number): SubSt {
    const t = st.task ? s.tasks?.find(x => x.id === st.task) : undefined;
    if (t?.st === 'run' || (!t && st.ok === undefined && liveAt(s, i))) return 'run';
    if (t && t.st !== 'done') return t.st;
    if (st.ok === false) return /stop|停/i.test(st.out ?? '') ? 'stop' : 'fail';
    return 'done';
  }
  const subSteps = (st: Step) => (st.sub ?? []).filter(x => x.k !== 'say').length;
  const subKey = (id: string, i: number, j: number) => `see-sub:${id}:${i}:${j}`;
  function subLine(s: Sess, st: Step, i: number) {
    const state = subState(s, st, i), n = subSteps(st), t = st.task ? s.tasks?.find(x => x.id === st.task) : undefined;
    const long = t?.ended && t.since ? ` · ${took(t.ended - t.since)}` : '';
    return state === 'run' ? `子任务 · 在跑${n ? ` · 第 ${n} 步` : ''}` : state === 'stop' ? `子任务 · 停了 · 做到第 ${n} 步` : state === 'fail' ? `子任务 · 出错了 · ${n} 步` : `子任务 · 做完了 · ${n} 步${long}`;
  }
  function subArg(s: Sess, st: Step, i: number, j: number) {
    const state = subState(s, st, i), n = subSteps(st), key = subKey(s.id, i, j);
    const tail = state === 'run' ? (n ? `第 ${n} 步` : '在起') : state === 'stop' ? '停了' : '';
    if (ctx.wb.shown() === key) requestAnimationFrame(paintShown);
    return `<button type="button" class="see-sa${ctx.wb.shown() === key ? ' on' : ''}" data-act="seesub" data-i="${i}" data-j="${j}">${esc(st.t || '子任务')}</button>${tail ? `<span class="see-sn">${tail}</span>` : ''}`;
  }

  // ---------- the right side: a sub-agent's steps, or a background task's output ----------
  let panel: { key: string; sid: string; i?: number; j?: number; task?: string; out: string; fetched: number; loading: boolean; open: Set<number>; stopping: boolean } | null = null;
  const view = () => win.querySelector<HTMLElement>('.pv .pv-view');
  const subNow = (st: Step) => {
    const last = st.sub?.at(-1);
    if (!last) return '在起…';
    if (last.k === 'think') return '在想';
    if (last.k === 'say' || last.ok !== undefined) return '在想下一步';
    return `${VERB[last.k] ?? '在做'} ${last.t}`;
  };
  function subRow(st: Step, j: number, open: boolean, running: boolean) {
    if (st.k === 'say') return `<p class="see-say">${esc(st.t)}</p>`;
    const more = !!(st.out || st.diff?.length);
    const a = st.k === 'think' ? `<span class="see-th">${running ? '在想' : st.ms && st.ms >= 1000 ? `想了 ${secs(st.ms)} 秒` : '想了一下'}</span>` : fileArg(st) ?? esc(st.t);
    const r = running ? '<span class="spin" role="img" aria-label="在做"></span>' : st.add !== undefined ? `<span class="p">+${st.add}</span> <span class="m">−${st.del ?? 0}</span>` : st.ok === true ? '<span class="p">✓</span>' : st.ok === false ? '<span class="m">✕</span>' : '';
    const x = open ? st.k === 'think' ? thoughtBox(st.out ?? '') : `<div class="x">${st.diff?.length ? ctx.diff(st.diff) : `<pre class="out">${esc(st.out ?? '')}</pre>`}</div>` : '';
    return `<div class="step${more ? ' more' : ''}${open ? ' open' : ''}"${more ? ` data-act="seesx" data-j="${j}" role="button" tabindex="0"` : ''}><span class="k">${K[st.k]}</span><span class="a" title="${esc(st.t)}">${a}</span><span class="r">${r}</span>${x}</div>`;
  }
  // A panel is three parts, each brought up to date only where it changed, so the 停 under the pointer stays the same
  // button: what it is doing now (while it runs), the body, and what it ends with.
  type Parts = [string, string, string];
  function subParts(s: Sess, st: Step, i: number): Parts {
    const state = subState(s, st, i), subs = st.sub ?? [], p = panel!;
    const bar = state === 'run' ? `<div class="see-bar"><span class="spin" role="img" aria-label="在跑"></span><span class="see-now">${esc(subNow(st))}</span>${clock(st.at ?? Date.now())}`
      + `${st.task ? `<button type="button" class="btn see-sm" data-act="seesubstop"${p.stopping ? ' disabled' : ''}>${STOP}${p.stopping ? '在停' : '停'}</button>` : ''}</div>` : '';
    const rows = subs.map((x, k) => subRow(x, k, p.open.has(k), state === 'run' && k === subs.length - 1 && x.k !== 'say' && x.ok === undefined && !(x.k === 'think' && x.out))).join('');
    const res = state === 'done' && st.out ? `<div class="see-res"><b>结果</b>${linkPaths(s, ctx.md(st.out))}</div>` : '';
    return [bar, `<div class="see-steps">${rows || '<p class="see-say">还没有动作。</p>'}</div>`, res];
  }
  const taskLine = (t: Task) => t.st === 'run' ? '后台 · 在跑' : t.st === 'done' ? `后台 · 做完了${t.ended && t.since ? ` · ${took(t.ended - t.since)}` : ''}` : t.st === 'stop' ? '后台 · 停了' : '后台 · 出错了';
  const noAnsi = (t: string) => t.replace(/\u001b\[[0-9;?]*[ -/]*[@-~]/g, '').replace(/\u001b\][^\u0007]*(\u0007|\u001b\\)/g, '').replace(/\r(?!\n)/g, '\n');
  function outParts(t: Task | undefined): Parts {
    const p = panel!;
    const bar = t?.st === 'run' ? `<div class="see-bar"><span class="spin" role="img" aria-label="在跑"></span><span class="see-now">在跑</span>${clock(t.since ?? Date.now())}<button type="button" class="btn see-sm" data-act="seebgstop" data-t="${esc(t.id)}">${STOP}停</button></div>` : '';
    const body = p.out ? `<pre class="see-out">${esc(noAnsi(p.out))}</pre>` : `<p class="see-none">${p.loading ? '在读输出…' : '没有可看的输出'}</p>`;
    return [bar, body, ''];
  }
  // Redraw what is open on the right, keeping its end in view while it grows.
  function paintShown() {
    const p = panel, v = view();
    if (!p || !v || ctx.wb.shown() !== p.key) { if (p && ctx.wb.shown() !== p.key) panel = null; return; }
    const s = ctx.byId(p.sid);
    if (!s) return;
    let parts: Parts, line: string;
    if (p.task) { const t = s.tasks?.find(x => x.id === p.task); parts = outParts(t); line = t ? taskLine(t) : '后台 · 结束了'; }
    else {
      const st = (itemOf(s, p.i!) as { steps?: Step[] } | undefined)?.steps?.[p.j!];
      if (!st || st.k !== 'agent') return;
      parts = subParts(s, st, p.i!); line = subLine(s, st, p.i!);
      if (subState(s, st, p.i!) !== 'run') p.stopping = false;
    }
    const small = win.querySelector('.pv .sh small');
    if (small && small.textContent !== line) small.textContent = line;
    const end = v.scrollTop >= v.scrollHeight - v.clientHeight - 24;
    let sp = v.firstElementChild as HTMLElement | null;
    if (v.dataset.seeKey !== p.key || !sp?.classList.contains('see-sp')) {
      v.innerHTML = `<div class="see-sp">${parts.map(h => `<div class="see-w">${h}</div>`).join('')}</div>`;
      sp = v.firstElementChild as HTMLElement;
      parts.forEach((h, k) => { (sp!.children[k] as HTMLElement).dataset.h = h; });
      v.dataset.seeKey = p.key;
    } else parts.forEach((h, k) => {
      const w = sp!.children[k] as HTMLElement;
      if (still(w.dataset.h ?? '') !== still(h)) { const t = document.createElement('template'); t.innerHTML = h; morph(w, t.content); }
      w.dataset.h = h;
    });
    if (end) v.scrollTop = v.scrollHeight;
  }
  function openSub(el: HTMLElement, i: number, j: number) {
    const s = ctx.current(), st = s && (itemOf(s, i) as { steps?: Step[] } | undefined)?.steps?.[j];
    if (!s || !st || st.k !== 'agent') return;
    const key = subKey(s.id, i, j);
    if (ctx.wb.shown() === key) return;
    panel = { key, sid: s.id, i, j, out: '', fetched: 0, loading: false, open: new Set(), stopping: false };
    void ctx.wb.show({ key, ic: '子', b: st.t || '子任务', small: subLine(s, st, i), fill: v => { v.dataset.seeKey = ''; paintShown(); requestAnimationFrame(() => { const w = view(); if (w) w.scrollTop = w.scrollHeight; }); } }, el.closest<HTMLElement>('.step') ?? el);
  }
  function openOut(el: HTMLElement, tid: string) {
    const s = ctx.current(), t = s?.tasks?.find(x => x.id === tid);
    if (!s || !t) return;
    const key = `see-bg:${s.id}:${tid}`;
    if (ctx.wb.shown() === key) return;
    panel = { key, sid: s.id, task: tid, out: '', fetched: 0, loading: true, open: new Set(), stopping: false };
    void ctx.wb.show({ key, ic: '后', b: t.what || t.kind, small: taskLine(t), fill: v => { v.dataset.seeKey = ''; paintShown(); } }, el);
    void fetchOut();
  }
  // A task's output as the host tails it; again every two seconds or so while it runs.
  async function fetchOut() {
    const p = panel;
    if (!p?.task || p.loading && p.fetched) return;
    p.loading = true; p.fetched = Date.now();
    const r = await ctx.call<{ task: Task; out: string }>(`/sessions/${p.sid}/tasks/${encodeURIComponent(p.task)}`).catch(() => null);
    p.loading = false;
    if (r) { p.out = r.out; if (r.task.st !== 'run') p.fetched = Infinity; }
    if (panel === p) paintShown();
  }
  async function stopTask(s: Sess, tid: string) {
    ctx.cue('close');
    await ctx.tryCall(`/sessions/${s.id}/tasks/${encodeURIComponent(tid)}/stop`, {});
  }

  // ---------- background work: a row above the composer ----------
  let bgOpen = '';
  const bgOf = (s: Sess) => (s.tasks ?? []).filter(t => !t.fg);
  const bgState = (t: Task) => t.st === 'run' ? `在跑 ${clock(t.since ?? Date.now())}` : t.st === 'done' ? `做完了${t.ended && t.since ? ` · ${took(t.ended - t.since)}` : ''}` : t.st === 'stop' ? '停了' : '出错了';
  function rows(s: Sess) {
    const all = bgOf(s);
    if (!all.length) { if (bgOpen === s.id) bgOpen = ''; return ''; }
    const run = all.filter(t => t.st === 'run').length, done = all.filter(t => t.st === 'done').length, other = all.length - run - done, open = bgOpen === s.id;
    const what = [run && `${run} 个在跑`, done && `${done} 个做完了`, other && `${other} 个停了`].filter(Boolean).join(' · '), shown = ctx.wb.shown();
    const list = open ? `<div class="see-bgl">${all.map(t => `<div class="see-bgr ${t.st}"><i class="see-dot ${t.st}"></i><code title="${esc(t.what)}">${esc(t.what || t.kind)}</code><span class="see-bgt">${bgState(t)}</span>`
      + `<button type="button" class="btn see-sm see-bgo${shown === `see-bg:${s.id}:${t.id}` ? ' on' : ''}" data-act="seebgout" data-t="${esc(t.id)}">看输出</button>`
      + `${t.st === 'run' ? `<button type="button" class="btn see-sm" data-act="seebgstop" data-t="${esc(t.id)}" aria-label="停">${STOP}停</button>` : ''}</div>`).join('')}</div>` : '';
    const chips = open ? '' : `<span class="see-bgs">${all.map(t => `<span class="${t.st}">${esc(t.what || t.kind)}${t.st === 'run' ? ` ${clock(t.since ?? Date.now())}` : t.st === 'done' ? ' <i>✓</i>' : ' <i>■</i>'}</span>`).join('')}</span>`;
    return `<div class="see-bg${open ? ' open' : ''}">${list}<button type="button" class="see-bgh" data-act="seebg" aria-expanded="${open}"><i class="see-dot${run ? ' run' : ''}"></i><b>后台</b><span class="see-bgn">${what}</span>${chips}${CHEV}</button></div>`;
  }
  // The rows above the composer change height: a conversation read to its end stays at its end.
  let rowsH = 0;
  const cRows = win.querySelector<HTMLElement>('.c-rows');
  if (cRows) new ResizeObserver(() => {
    const h = cRows.offsetHeight, d = h - rowsH, c = win.querySelector<HTMLElement>('.host .conv');
    rowsH = h;
    if (c && d > 0 && c.scrollTop + c.clientHeight + d >= c.scrollHeight - 40) c.scrollTop = c.scrollHeight;
  }).observe(cRows);

  // ---------- the file menu: a right click on any file in the window ----------
  let menuFor: { el: HTMLElement; ref: string; sid: string } | null = null, editor = '';
  // The editor a file opens in: the one chosen in settings if installed, else the first installed.
  async function readEditor() {
    const [list, set] = await Promise.all([window.agents?.editors?.().catch(() => []) ?? [], ctx.call<{ settings?: { editor?: string } }>('/settings').catch(() => null)]);
    editor = (list.find(e => e.id === set?.settings?.editor) ?? list[0])?.name ?? '';
  }
  void readEditor();
  win.addEventListener('contextmenu', e => {
    const el = (e.target as Element).closest<HTMLElement>('[data-ref]'), s = ctx.current(), ref = el?.dataset.ref;
    if (e.defaultPrevented || !el || !s || !ref || isUrl(ref) || el.closest('.pop')) return;
    e.preventDefault();
    look(s, ref);
    menuFor = { el, ref, sid: s.id };
    const w = win.getBoundingClientRect();
    ctx.menu(`<button type="button" data-act="seefside">在右边打开</button><button type="button" data-act="seefapp">用默认的 app 打开</button><button type="button" data-act="seefed">在编辑器里打开${editor ? `<span class="k">${esc(editor)}</span>` : ''}</button>`
      + '<button type="button" data-act="seeffind">在访达里显示</button><span class="sep"></span><button type="button" data-act="seefcopy">复制路径</button>', { x: e.clientX - w.left, y: e.clientY - w.top }, { cls: 'see-fm' });
    void readEditor();
  });
  async function fileAct(a: string) {
    const m = menuFor, s = m && ctx.byId(m.sid);
    ctx.closeMenu();
    if (!m || !s) return;
    const abs = absOf(s, m.ref), hit = look(s, m.ref), line = hit?.line ?? (Number(/:(\d+)(?::\d+)?(?:-\d+)?$|#L(\d+)/.exec(m.ref)?.slice(1).find(Boolean)) || undefined);
    if (a === 'seefside') {
      const src = m.el.isConnected ? m.el : [...win.querySelectorAll<HTMLElement>('[data-ref]')].find(x => x.dataset.ref === m.ref);
      if (src) ctx.wb.act('peek', src);
    } else if (a === 'seefapp') void window.agents?.openPath?.(abs);
    else if (a === 'seefed') { if (await window.agents?.openInEditor?.(abs, line) === false) ctx.toast('没能在编辑器里打开'); }
    else if (a === 'seeffind') void window.agents?.revealFile?.(abs);
    else if (a === 'seefcopy') await navigator.clipboard.writeText(abs).then(() => ctx.tick(), () => ctx.toast(`没能复制。路径是 ${abs}`));
  }

  // ---------- what is open on the right is marked where it was opened from ----------
  let markRaf = 0;
  function marks() {
    markRaf = 0;
    const k = ctx.wb.shown();
    for (const b of win.querySelectorAll<HTMLElement>('.see-f,.see-p')) b.classList.toggle('on', !!k && classify(b.dataset.ref ?? '').key === k);
    const s = ctx.current();
    for (const b of win.querySelectorAll<HTMLElement>('.see-sa')) b.classList.toggle('on', !!s && k === subKey(s.id, Number(b.dataset.i), Number(b.dataset.j)));
    for (const b of win.querySelectorAll<HTMLElement>('.see-bgo')) b.classList.toggle('on', !!s && k === `see-bg:${s.id}:${b.dataset.t}`);
  }
  const mark = () => { if (!markRaf) markRaf = requestAnimationFrame(marks); };
  const pv = win.querySelector('.pv'), sh = pv?.querySelector('.sh');
  if (pv) new MutationObserver(mark).observe(pv, { attributes: true, attributeFilter: ['class', 'style'] });
  if (sh) new MutationObserver(mark).observe(sh, { childList: true, subtree: true, characterData: true });

  // ---------- the title: the name Claude Code gives the session fades in; a name you type does not ----------
  const hT = win.querySelector('.m-head .h-t');
  let lastTitle = ['', ''];
  if (hT) new MutationObserver(() => {
    const b = hT.querySelector('b[data-act="rename"]'), s = ctx.current();
    if (!b || !s) { lastTitle = ['', '']; return; }
    const t = b.textContent ?? '';
    if (lastTitle[0] === s.id && lastTitle[1] && lastTitle[1] !== t) anim(b, [{ opacity: 0, filter: 'blur(4px)' }, { opacity: 1, filter: 'none' }], 300);
    lastTitle = [s.id, t];
  }).observe(hT, { childList: true, subtree: true, characterData: true });

  // ---------- one clock for the whole window: running times four times a second, what is open on the right each second ----------
  let beat = 0;
  setInterval(() => {
    if (document.hidden) return;
    for (const el of win.querySelectorAll<HTMLElement>('[data-see-t0]')) {
      const ms = Date.now() - Number(el.dataset.seeT0), t = el.dataset.seeF === 's' ? `${secs(ms)} 秒` : mmss(ms);
      if (el.textContent !== t) el.textContent = t;
    }
    if (++beat % 4) return;
    if (panel && ctx.wb.shown() !== panel.key) panel = null;
    if (!panel) return;
    if (panel.task && Date.now() - panel.fetched >= 1400) void fetchOut();
    paintShown();
  }, 250);

  // ---------- commands the window answers ----------
  ctx.own.set('tasks', s => {
    if (!s || !bgOf(s).length) { ctx.toast('没有后台任务'); return; }
    bgOpen = s.id; ctx.draw('comp');
  });
  ctx.own.set('editor', s => { if (s) void openFolder(s); });
  async function openFolder(s: Sess) { if (await window.agents?.openInEditor?.(s.cwd) === false) ctx.toast('没能在编辑器里打开'); }

  return {
    arg(s, st, i, j, html) {
      if (st.k === 'think') return thinkArg(s, st, i);
      if (st.k === 'agent') return subArg(s, st, i, j);
      return fileArg(st) ?? html;
    },
    under(s, st, i, j) {
      return st.k === 'think' && st.out && thinkOpen.has(`${s.id}:${i}:${j}`) ? thoughtBox(st.out) : '';
    },
    answer(s, _it, _i, html) { return linkPaths(s, html); },
    rows,
    more(s) {
      const name = s.cwd.split('/').filter(Boolean).pop() ?? s.cwd;
      return `<span class="sep"></span><span class="ph">${esc(name)}</span><button type="button" data-act="seeed">在编辑器里打开${editor ? `<span class="k">${esc(editor)}</span>` : ''}</button>`
        + '<button type="button" data-act="reveal">在访达里显示</button><button type="button" data-act="seeterm">在终端里打开</button><span class="sep"></span>';
    },
    act(a, el) {
      const s = ctx.current();
      // A thought opens under its line and a sub-agent opens on the right; a click inside the thought does not fold it.
      if (a === 'step' && s) {
        const i = Number(el.dataset.i), j = Number(el.dataset.j), st = (itemOf(s, i) as { steps?: Step[] } | undefined)?.steps?.[j];
        if (st?.k === 'think') {
          const k = `${s.id}:${i}:${j}`, open = !thinkOpen.has(k);
          if (open) thinkOpen.add(k); else thinkOpen.delete(k);
          ctx.draw('main');
          if (open) requestAnimationFrame(() => requestAnimationFrame(() => { const b = el.querySelector('.see-tb'); if (b) anim(b, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], 260); }));
          return true;
        }
        if (st?.k === 'agent') { openSub(el, i, j); return true; }
        return false;
      }
      if (a === 'seetb') return true;
      if (a === 'seesub') { openSub(el, Number(el.dataset.i), Number(el.dataset.j)); return true; }
      if (a === 'seesx' && panel) {
        const j = Number(el.dataset.j);
        if (panel.open.has(j)) panel.open.delete(j); else panel.open.add(j);
        paintShown(); return true;
      }
      if (a === 'seesubstop' && panel && !panel.task) {
        const p = panel, sess = ctx.byId(p.sid), st = sess && (itemOf(sess, p.i!) as { steps?: Step[] } | undefined)?.steps?.[p.j!];
        if (!sess || !st?.task || p.stopping) return true;
        p.stopping = true; paintShown();
        void stopTask(sess, st.task).then(() => { p.stopping = false; paintShown(); });
        return true;
      }
      if (a === 'seebg' && s) { bgOpen = bgOpen === s.id ? '' : s.id; ctx.tick(); ctx.draw('comp'); return true; }
      if (a === 'seebgout') { openOut(el, el.dataset.t ?? ''); return true; }
      if (a === 'seebgstop' && s) { void stopTask(s, el.dataset.t ?? ''); return true; }
      if (a.startsWith('seef')) { void fileAct(a); return true; }
      if (a === 'seeed' && s) { ctx.closeMenu(); void openFolder(s); return true; }
      if (a === 'seeterm' && s) {
        ctx.closeMenu();
        void window.agents?.terminal(s.cwd, '').then(ok => { if (!ok) ctx.toast(`没能打开终端。在终端里跑：cd ${s.cwd.replace(/^\/Users\/[^/]+/, '~')}`); });
        return true;
      }
      return false;
    },
    esc() {
      if (!bgOpen) return false;
      bgOpen = ''; ctx.draw('comp'); return true;
    },
  };
}

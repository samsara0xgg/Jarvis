// 大图 (design point ql): a picture opens over the window, grown from the one you pressed, and ← → step through every
// picture of the session in the order they came: what you sent, what its tools gave back, and what is waiting in the
// composer. Space or esc puts it away, shrinking back into the picture it came from. 复制图片 copies it; 放上舞台 moves it
// onto the workbench's stage. It replaces the page's one-picture viewer.
import type { Pic, Step } from '../../electron/agents/types';
import type { Feature, PageCtx } from './ctx';
import './viewer.css';

type Shot = { url: string; name: string };
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const svg = (d: string) => `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const I = {
  copy: svg('<rect x="5.5" y="5.5" width="8" height="8" rx="2"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/>'),
  stage: svg('<path d="M9.5 2.5h4v4M6.5 13.5h-4v-4M13.5 2.5 9 7M2.5 13.5 7 9"/>'),
  x: svg('<path d="M4 4l8 8M12 4l-8 8"/>'),
  prev: svg('<path d="M10 3.5 5.5 8l4.5 4.5"/>'),
  next: svg('<path d="M6 3.5 10.5 8 6 12.5"/>'),
};

export function mountViewer(ctx: PageCtx): Feature {
  const dlg = document.createElement('dialog');
  dlg.className = 'ql'; dlg.setAttribute('aria-label', '大图');
  ctx.win.append(dlg);
  const V = { open: false, list: [] as Shot[], i: 0, zoom: false, size: '' };

  // Every picture of the session on screen, oldest first, then the ones about to be sent; each once.
  function gallery(): Shot[] {
    const s = ctx.current(), out: Shot[] = [];
    const add = (p: Pic) => { if (p.img) out.push({ url: `${ctx.api}/images/${p.img}`, name: p.name }); };
    const steps = (ss: Step[]) => { for (const st of ss) { st.pics?.forEach(add); if (st.sub) steps(st.sub); } };
    for (const it of (s && ctx.items(s.id)) || []) { if (it.k === 'you') it.files?.forEach(add); else if (it.k === 'steps') steps(it.steps); }
    for (const img of ctx.win.querySelectorAll<HTMLImageElement>('.c-files img')) out.push({ url: img.src, name: img.alt });
    const seen = new Set<string>();
    return out.filter(x => !seen.has(x.url) && !!seen.add(x.url));
  }
  // The picture on the page it came from, when it is in sight.
  const thumbOf = (url: string) => [...ctx.win.querySelectorAll<HTMLImageElement>('[data-act="view"] img')].find(t => {
    if (t.src !== url || dlg.contains(t) || !t.offsetParent) return false;
    const r = t.getBoundingClientRect();
    return r.width > 0 && r.bottom > 0 && r.top < innerHeight;
  });
  const big = () => dlg.querySelector<HTMLImageElement>('.ql-b img');

  function render() {
    const x = V.list[V.i], many = V.list.length > 1;
    dlg.innerHTML = `<div class="ql-h"><b>${esc(x.name)}</b><small class="ql-n"></small><span class="sp"></span>`
      + `<button type="button" class="ql-l" data-act="ql-copy">${I.copy}<span>复制图片</span></button>`
      + (ctx.current() ? `<button type="button" class="ql-l" data-act="ql-stage">${I.stage}<span>放上舞台</span></button>` : '')
      + `<button type="button" class="ql-x" data-act="ql-close" aria-label="关闭">${I.x}</button></div>`
      + `<div class="ql-b${V.zoom ? ' z' : ''}" tabindex="-1"><img src="${esc(x.url)}" alt="${esc(x.name)}" crossorigin="anonymous" data-act="ql-zoom">`
      + (many ? `<button type="button" class="ql-nav prev" data-act="ql-prev" aria-label="上一张">${I.prev}</button><button type="button" class="ql-nav next" data-act="ql-next" aria-label="下一张">${I.next}</button>` : '') + '</div>'
      + (many ? `<div class="ql-f">${V.list.map((y, j) => `<button type="button" data-act="ql-go" data-j="${j}" class="${j === V.i ? 'on' : ''}" aria-label="${esc(y.name)}"><img src="${esc(y.url)}" alt=""></button>`).join('')}</div>` : '');
    const img = big()!, n = dlg.querySelector<HTMLElement>('.ql-n')!;
    const count = many ? `${V.i + 1} / ${V.list.length}` : '';
    n.textContent = count;
    void img.decode().then(() => { V.size = `${img.naturalWidth} × ${img.naturalHeight}`; n.textContent = [V.size, count].filter(Boolean).join(' · '); }, () => {});
    return img;
  }
  function open(from: HTMLElement) {
    const t = from.querySelector('img');
    if (!t) return;
    V.list = gallery();
    let i = V.list.findIndex(x => x.url === t.src);
    if (i < 0) { V.list.push({ url: t.src, name: t.alt }); i = V.list.length - 1; }
    V.i = i; V.zoom = false; V.open = true;
    ctx.closeMenu();
    const img = render();
    dlg.showModal();
    dlg.querySelector<HTMLElement>('.ql-b')?.focus({ preventScroll: true });
    if (reduced.matches) return;
    // It grows out of the picture you pressed.
    const a = t.getBoundingClientRect();
    dlg.animate([{ backgroundColor: 'rgb(3 4 10 / 0)', backdropFilter: 'blur(0px)' }, { backgroundColor: 'rgb(3 4 10 / .82)', backdropFilter: 'blur(10px)' }], { duration: 240 });
    dlg.querySelectorAll('.ql-h,.ql-f,.ql-nav').forEach(e => e.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 220, delay: 80, fill: 'backwards' }));
    img.style.opacity = '0';
    void img.decode().catch(() => {}).then(() => {
      img.style.opacity = '';
      const b = img.getBoundingClientRect();
      if (!b.width || !b.height || !V.open) return;
      img.style.transformOrigin = '0 0';
      img.animate([{ transform: `translate(${a.left - b.left}px,${a.top - b.top}px) scale(${a.width / b.width},${a.height / b.height})`, opacity: .5 }, { transform: 'none', opacity: 1 }], { duration: 300, easing: 'cubic-bezier(.2,.8,.2,1)' });
    });
  }
  function step(j: number) {
    if (V.list.length < 2) return;
    V.i = (j + V.list.length) % V.list.length; V.zoom = false;
    const img = render();
    if (!reduced.matches) img.animate([{ opacity: .4, transform: 'scale(.985)' }, { opacity: 1, transform: 'none' }], { duration: 180 });
    ctx.tick();
  }
  // Away: back into the picture it came from when that is in sight, otherwise it fades.
  function close(now = false) {
    if (!V.open) return;
    V.open = false;
    const img = big(), thumb = thumbOf(V.list[V.i].url);
    const done = () => { if (!V.open) { dlg.close(); dlg.replaceChildren(); } };
    if (now || reduced.matches || !img) { done(); return; }
    if (thumb && !V.zoom) {
      const a = img.getBoundingClientRect(), b = thumb.getBoundingClientRect();
      img.style.transformOrigin = '0 0';
      img.animate([{ transform: 'none', opacity: 1 }, { transform: `translate(${b.left - a.left}px,${b.top - a.top}px) scale(${b.width / a.width},${b.height / a.height})`, opacity: .6 }],
        { duration: 240, easing: 'cubic-bezier(.4,0,.2,1)', fill: 'forwards' }).onfinish = done;
      dlg.animate([{ backgroundColor: 'rgb(3 4 10 / .82)', backdropFilter: 'blur(10px)' }, { backgroundColor: 'rgb(3 4 10 / 0)', backdropFilter: 'blur(0px)' }], { duration: 240, fill: 'forwards' });
      dlg.querySelectorAll('.ql-h,.ql-f,.ql-nav').forEach(e => e.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 140, fill: 'forwards' }));
    } else dlg.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 160, fill: 'forwards' }).onfinish = done;
  }
  // The picture itself goes to the clipboard, as a PNG: what the clipboard takes from a page.
  async function copy(btn: HTMLElement | null) {
    const img = big(), say = (ok: boolean) => {
      if (!btn) return;
      const l = btn.querySelector('span')!;
      l.textContent = ok ? '复制好了' : '没能复制'; btn.classList.toggle('ok', ok);
      clearTimeout(Number(btn.dataset.t)); btn.dataset.t = String(setTimeout(() => { l.textContent = '复制图片'; btn.classList.remove('ok'); }, 1500));
    };
    ctx.tick();
    try {
      if (!img) throw new Error('no picture');
      await img.decode();
      const c = document.createElement('canvas');
      c.width = img.naturalWidth; c.height = img.naturalHeight; c.getContext('2d')!.drawImage(img, 0, 0);
      const png = await new Promise<Blob>((ok, no) => c.toBlob(b => b ? ok(b) : no(new Error('png')), 'image/png'));
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })]);
      say(true);
    } catch { say(false); }
  }
  function toStage() {
    const x = V.list[V.i], from = thumbOf(x.url)?.closest<HTMLElement>('[data-act="view"]') ?? null, size = V.size;
    close(true);
    // The sheet's badge is a word, as a file's is its extension.
    void ctx.wb.show({ key: `pic:${x.url}`, ic: '图', b: x.name, small: size, target: 'stage',
      fill(view) { const im = document.createElement('img'); im.className = 'ql-stage'; im.src = x.url; im.alt = x.name; view.append(im); } }, from);
  }
  // esc on a modal dialog cancels it: the same way out as everything else.
  dlg.addEventListener('cancel', e => { e.preventDefault(); close(); });

  return {
    act(a, el) {
      if (a === 'view') { open(el); return true; }
      if (!a.startsWith('ql-')) return false;
      if (a === 'ql-close') close();
      else if (a === 'ql-prev' || a === 'ql-next') step(V.i + (a === 'ql-next' ? 1 : -1));
      else if (a === 'ql-go') step(Number(el.dataset.j));
      else if (a === 'ql-zoom') { V.zoom = !V.zoom; dlg.querySelector('.ql-b')?.classList.toggle('z', V.zoom); }
      else if (a === 'ql-copy') void copy(el);
      else if (a === 'ql-stage') toStage();
      return true;
    },
    esc() { if (!V.open) return false; close(); return true; },
    // While it is open every key is its own: ← → step, space and esc put it away, ⌘C copies.
    key(e) {
      if (!V.open) return false;
      const t = e.target as HTMLElement, plain = !e.metaKey && !e.ctrlKey && !e.altKey;
      const consume = () => { e.preventDefault(); e.stopImmediatePropagation(); return true; };
      if (e.key === 'Escape' || (e.key === ' ' && plain)) { if (!e.repeat) close(); return consume(); }
      if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && plain) { step(V.i + (e.key === 'ArrowRight' ? 1 : -1)); return consume(); }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'c') { void copy(dlg.querySelector('[data-act="ql-copy"]')); return consume(); }
      if (e.key === 'Tab' || (e.key === 'Enter' && t.closest('button'))) { e.stopImmediatePropagation(); return true; }
      return consume();
    },
  };
}

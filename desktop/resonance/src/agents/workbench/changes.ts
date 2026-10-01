// 改动 (review 17): what this session changed against what landing compares it with, on the stage: the files on the
// left with how each changed and its lines added and removed, the chosen file's hunks on the right, numbered as the
// file is now. From the title's ··· menu, from /diff, and from the landing's 改动 step (a file there opens on it).
import type { Change, Diff, Sess } from '../../../electron/agents/types';
import type { Feature, PageCtx } from '../ctx';
import { diffRows, langOf, rowsHTML } from './open';
import { plural, tr } from '../lang';

type List = { base: string; top: string; files: Change[]; into?: string; branch?: string };
type One = { path: string; diff: Diff; add: number; del: number; hunks: number[]; bin?: boolean };
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const pm = (a: number, d: number) => `<span class="p">+${a}</span> <span class="m">−${d}</span>`;
const st = (c: Change) => { const k = c.st === '?' ? 'A' : c.st; return `<i class="op-st s${k}">${k}</i>`; };
// What it is compared with, in words: the default branch where the branch left it, origin's copy, or the last commit.
const against = (l: List) => l.base === 'HEAD' ? tr('对比上一次提交', 'Compared with the last commit')
  : l.into && l.branch && l.branch !== l.into ? tr(`对比 ${l.into}（开分支时的 ${l.base.slice(0, 7)}）`, `Compared with ${l.into} (branch point ${l.base.slice(0, 7)})`) : tr(`对比 origin/${l.into ?? 'main'}（${l.base.slice(0, 7)}）`, `Compared with origin/${l.into ?? 'main'} (${l.base.slice(0, 7)})`);

export function mountChanges(ctx: PageCtx): Feature {
  // The file each session's changes were last looking at, and the drawn panel's way to look at another.
  const picked = new Map<string, string>();
  let pick: ((path: string) => void) | null = null;

  async function show(s: Sess, from: HTMLElement | null, path?: string) {
    if (path) picked.set(s.id, path);
    const key = `changes:${s.id}`;
    if (ctx.wb.shown() === key && pick) { if (path) pick(path); return; }
    let l: List;
    try { l = await ctx.call<List>(`/sessions/${s.id}/changes`); } catch (e) { ctx.toast(e instanceof Error ? e.message : String(e), true); return; }
    if (!l.files.length) { ctx.toast(tr('它还没改文件', 'It hasn\'t changed any files yet')); return; }
    await ctx.wb.show({ key, ic: '±', b: tr('改动', 'Changes'), small: against(l), target: 'stage', fill: view => draw(s, l, view) }, from);
  }

  function draw(s: Sess, l: List, view: HTMLElement) {
    const add = l.files.reduce((a, f) => a + f.add, 0), del = l.files.reduce((a, f) => a + f.del, 0);
    view.innerHTML = `<div class="op-dw"><div class="op-dv"><div class="op-dl"><p class="op-dsum">${tr(`${l.files.length} 个文件`, plural(l.files.length, 'file'))} · ${pm(add, del)}</p>${l.files.map(f => {
      const i = f.path.lastIndexOf('/');
      return `<button type="button" class="op-df" data-path="${esc(f.path)}" title="${esc(f.path)}">${st(f)}<b>${esc(f.path.slice(i + 1))}</b>${f.bin ? tr('<span class="op-pm op-pb">二进制</span>', '<span class="op-pm op-pb">Binary</span>') : `<span class="op-pm">${pm(f.add, f.del)}</span>`}<small>${esc(i < 0 ? '.' : f.path.slice(0, i))}</small></button>`;
    }).join('')}</div><div class="op-dd"></div></div></div>`;
    const dd = view.querySelector<HTMLElement>('.op-dd')!;
    let tok = 0;
    const one = async (path: string) => {
      const f = l.files.find(x => x.path === path) ?? l.files[0], t = ++tok;
      picked.set(s.id, f.path);
      for (const b of view.querySelectorAll<HTMLElement>('.op-df')) { const on = b.dataset.path === f.path; b.classList.toggle('on', on); b.setAttribute('aria-current', String(on)); }
      view.querySelector<HTMLElement>('.op-df.on')?.scrollIntoView({ block: 'nearest' });
      // The file's name opens it in the sheet (‹ comes back here); a deleted one has nothing to open.
      const name = f.st === 'D' ? `<span class="op-dp">${esc(f.path)}</span>` : `<button type="button" class="op-dp" data-act="peek" data-ref="${esc(`${l.top}/${f.path}`)}" title="${tr('打开这个文件', 'Open this file')}">${esc(f.path)}</button>`;
      const head = `<div class="op-dh">${st(f)}${name}${f.bin ? tr('<span class="op-pm op-pb">二进制</span>', '<span class="op-pm op-pb">Binary</span>') : `<span class="op-pm">${pm(f.add, f.del)}</span>`}</div>`;
      dd.innerHTML = `${head}<p class="op-bin">${tr('在读', 'Reading')}…</p>`;
      const d = await ctx.call<One>(`/sessions/${s.id}/changes/diff?path=${encodeURIComponent(f.path)}`).catch((e: unknown) => e instanceof Error ? e.message : String(e));
      if (t !== tok || !dd.isConnected) return;
      dd.innerHTML = head + (typeof d === 'string' ? `<p class="op-bin">${esc(d)}</p>` : d.bin || f.bin ? tr('<p class="op-bin">二进制文件，这里不比</p>', '<p class="op-bin">Binary file, not compared here</p>')
        : d.diff.length ? rowsHTML(diffRows(d.diff, d.hunks), langOf(f.path), { diff: true }) : tr('<p class="op-bin">没有要比的行</p>', '<p class="op-bin">No lines to compare</p>'));
      dd.scrollTop = 0;
    };
    view.querySelector('.op-dl')!.addEventListener('click', e => { const b = (e.target as HTMLElement).closest<HTMLElement>('.op-df'); if (b) void one(b.dataset.path!); });
    pick = p => { if (view.isConnected) void one(p); };
    void one(picked.get(s.id) ?? l.files[0].path);
  }

  // /diff, typed or picked, opens it too.
  ctx.own.set('changes', s => { if (s) void show(s, null); });
  return {
    act(a, el) {
      if (a !== 'changes') return false;
      const s = ctx.current(), menu = !!el.closest('.pop');
      if (menu) ctx.closeMenu();
      if (s) void show(s, menu ? null : el, el.dataset.path);
      return true;
    },
    more: s => s.dirty || (s.tree && !s.gone) ? `<button type="button" data-act="changes">${tr('改动', 'Changes')}${s.dirty ? `<span class="op-mk">${pm(s.dirty.add, s.dirty.del)}</span>` : ''}</button>` : '',
  };
}

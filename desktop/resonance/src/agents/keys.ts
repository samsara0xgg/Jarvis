// 键位表 (design point keys): ? with nothing being written shows every key of the window on one sheet; esc or ? puts it
// away. The sheet is the design's (keys.png) as the window has it: the keys of the features built beside this one are
// listed the way the design names them, and what the window does not do (⌘Space from another app, dictation) is left out.
import type { Feature, PageCtx } from './ctx';
import './keys.css';

// [keys, where they work, what they do]; ' / ' separates alternatives, a space separates keys pressed in turn.
const KEYS: [string, [string, string, string][]][] = [
  ['出手', [
    ['⌘N', '任何时候', '落下一张纸条：写一句，抛给一个会话，人不走'],
    ['⏎ / ⌘⏎', '纸条上', '抛出去，留在原地 / 抛出去并跟过去'],
    ['⇥ / ⇧⇥', '纸条上', '打开列表：去处 / 用谁'],
    ['↑ ↓ / ⏎ / ⇥', '列表里', '挑 / 选中 / 换到另一张；打几个字就筛'],
    ['⌘Z', '保险丝烧完前', '撤回刚抛出去的，一个 token 没花'],
    ['esc', '纸条上', '收起纸条，字留着下次接着写'],
  ]],
  ['等你的', [
    ['⌥↓', '任何时候', '去下一个等你的：先问你的，再出错的，再做完的'],
    ['点她', '任何时候', '同 ⌥↓'],
    ['指着她', '任何时候', '按顺序列出所有等你的；右键一行，先放着'],
  ]],
  ['去哪', [
    ['←', '输入框空着', '退一层：舞台 → 对话 → 长曝光'],
    ['⌥↑', '任何时候', '盖下长曝光'],
    ['↑ ↓', '长曝光里', '换会话，停在名字上'],
    ['← →', '长曝光里', '一句一句读你说过的话，针指到那一刻'],
    ['→ / ⏎', '长曝光里', '进去；在某一句上按 ⏎，落在那一句'],
    ['⌘K', '任何时候', '搜标题，也搜说过的话'],
    ['⌘[ / ⌘]', '任何时候', '回到上一个会话 / 再回去'],
    ['⌘↑ / ⌘↓', '对话里', '上一个 / 下一个会话'],
  ]],
  ['眼前这个会话', [
    ['⏎ / esc / 1–9', '有批准卡、没在打字', '允许 / 拒绝 / 选第几个'],
    ['⌘⏎', '有「要落地吗」', '一键落地'],
    ['/ @', '输入框', '命令 / 文件和文件夹'],
    ['↑', '输入框空着', '拿回排着的最后一句来改'],
    ['⌃`', '任何时候', '终端'],
    ['!命令', '输入框', '在终端里跑一次，不进对话'],
    ['/rewind / /fork', '输入框', '挑你说过的一句：退回到它之前，或者从它之前分叉'],
    ['/btw / /side', '输入框', '侧问：后面接一句话，答在最新那段回答下面；不进对话，也不打断它'],
    ['/export', '输入框', '整段对话导出成 Markdown'],
    ['← → / 空格', '大图里', '上一张 · 下一张 / 收起'],
    ['?', '输入框空着', '这张表'],
  ]],
  ['消息', [
    ['指着一条消息', '对话里', '复制 · 修改 · 表情'],
    ['⌘⏎ / esc', '修改时', '重发 / 不改了'],
    ['选中几个字', '回答里', '引用 · 问一句'],
  ]],
  ['esc 的顺序', [
    ['esc', '有东西开着', '先关最上面一层：修改 → 表情 → 大图 → 弹层 → 补全 → 纸条 → 长曝光 → 她说的那句 → 舞台或并排 → 落地面板'],
    ['esc', '什么都没开', '有批准卡就拒绝；落地在跑就这一步跑完停；这一轮在跑就打断'],
  ]],
];
const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
// A modifier and its key are two caps; a word (点她, !命令, /rewind) stays one.
const caps = (k: string) => k.split(' / ').map(alt => alt.split(' ').map(part => (/^[⌘⌥⌃⇧].+/.test(part) ? [part[0], part.slice(1)] : [part])
  .map(x => `<kbd>${esc(x)}</kbd>`).join('')).join(' ')).join('<i>/</i>');
const reduced = matchMedia('(prefers-reduced-motion: reduce)');

export function mountKeys(ctx: PageCtx): Feature {
  const el = document.createElement('div');
  el.className = 'keys'; el.hidden = true;
  el.innerHTML = `<div class="keys-in" role="dialog" aria-label="键位" tabindex="-1"><h4>键位<small><kbd>esc</kbd> 或 <kbd>?</kbd> 关</small></h4><div class="keys-g">${KEYS.map(([g, rows]) => `<h5>${esc(g)}</h5>`
    + rows.map(([k, where, what]) => `<p><span>${esc(what)}<small>${esc(where)}</small></span><span class="kc">${caps(k)}</span></p>`).join('')).join('')}</div></div>`;
  ctx.win.append(el);
  const inner = el.querySelector<HTMLElement>('.keys-in')!;
  let from: HTMLElement | null = null;

  function open() {
    ctx.closeMenu();
    from = document.activeElement as HTMLElement | null;
    el.hidden = false; inner.scrollTop = 0; inner.focus({ preventScroll: true });
    if (!reduced.matches) el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 160 });
    ctx.tick();
  }
  function close() {
    if (el.hidden) return;
    el.hidden = true;
    if (from?.isConnected && !from.closest('[hidden]')) from.focus({ preventScroll: true });
    from = null;
  }
  // A click beside the sheet puts it away.
  el.addEventListener('click', e => { if (!inner.contains(e.target as Node)) close(); });

  return {
    esc() { if (el.hidden) return false; close(); return true; },
    key(e) {
      const q = e.key === '?' || e.key === '？', consume = () => { e.preventDefault(); e.stopImmediatePropagation(); return true; };
      if (!el.hidden) {
        if (e.key === 'Escape' || q) { close(); return consume(); }
        // The sheet scrolls with the keys that scroll; nothing behind it hears any key.
        if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' ', 'Tab'].includes(e.key)) { e.stopImmediatePropagation(); return true; }
        return consume();
      }
      if (!q || e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return false;
      const t = e.target as HTMLElement, editable = t.matches('input,textarea,select,[contenteditable="true"]');
      if (editable && !(t === ctx.ta && !ctx.ta.value.trim())) return false;
      open(); return consume();
    },
  };
}

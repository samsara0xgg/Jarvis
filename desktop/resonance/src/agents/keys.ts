// 键位表 (design point keys): ? with nothing being written shows every key of the window on one sheet; esc or ? puts it
// away. The sheet is the design's (keys.png) as the window has it: the keys of the features built beside this one are
// listed the way the design names them, and what the window does not do (⌘Space from another app, dictation) is left out.
import type { Feature, PageCtx } from './ctx';
import './keys.css';
import { tr } from './lang';

// [keys, where they work, what they do]; ' / ' separates alternatives, a space separates keys pressed in turn.
const KEYS: [string, [string, string, string][]][] = [
  [tr('出手', 'Notes'), [
    ['⌘N', tr('任何时候', 'Anytime'), tr('落下一张纸条：写一句，抛给一个会话，人不走', 'Drop a note: write a line, send it to a session, stay where you are')],
    ['⏎ / ⌘⏎', tr('纸条上', 'On a note'), tr('抛出去，留在原地 / 抛出去并跟过去', 'Send it and stay / send it and follow')],
    ['⇥ / ⇧⇥', tr('纸条上', 'On a note'), tr('打开列表：去处 / 用谁', 'Open the list: where to / which agent')],
    ['↑ ↓ / ⏎ / ⇥', tr('列表里', 'In the list'), tr('挑 / 选中 / 换到另一张；打几个字就筛', 'Move / select / switch lists; type to filter')],
    ['⌘Z', tr('保险丝烧完前', 'Before the fuse runs out'), tr('撤回刚抛出去的，一个 token 没花', 'Take back what you just sent, not a token spent')],
    ['esc', tr('纸条上', 'On a note'), tr('收起纸条，字留着下次接着写', 'Put the note away; the text stays for next time')],
  ]],
  [tr('等你的', 'Waiting on you'), [
    ['⌥↓', tr('任何时候', 'Anytime'), tr('去下一个等你的：先问你的，再出错的，再做完的', 'Go to the next one waiting on you: questions first, then errors, then done')],
    [tr('点她', 'Click\u00a0her'), tr('任何时候', 'Anytime'), tr('同 ⌥↓', 'Same as ⌥↓')],
    [tr('指着她', 'Hover\u00a0her'), tr('任何时候', 'Anytime'), tr('按顺序列出所有等你的；右键一行，先放着', 'List everything waiting on you in order; right-click a row to park it')],
  ]],
  [tr('去哪', 'Go to'), [
    ['←', tr('输入框空着', 'Input empty'), tr('退一层：舞台 → 对话 → 长曝光', 'Step back a level: stage → conversation → Long Exposure')],
    ['⌥↑', tr('任何时候', 'Anytime'), tr('盖下长曝光', 'Pull down Long Exposure')],
    ['↑ ↓', tr('长曝光里', 'In Long Exposure'), tr('换会话，停在名字上', 'Switch session, resting on the name')],
    ['← →', tr('长曝光里', 'In Long Exposure'), tr('一句一句读你说过的话，针指到那一刻', 'Read what you said line by line; the needle points to that moment')],
    ['→ / ⏎', tr('长曝光里', 'In Long Exposure'), tr('进去；在某一句上按 ⏎，落在那一句', 'Go in; press ⏎ on a line to land on it')],
    ['⌘K', tr('任何时候', 'Anytime'), tr('搜标题，也搜说过的话', 'Search titles and what you said')],
    ['⌘[ / ⌘]', tr('任何时候', 'Anytime'), tr('回到上一个会话 / 再回去', 'Back to the previous session / forward again')],
    ['⌘↑ / ⌘↓', tr('对话里', 'In a conversation'), tr('上一个 / 下一个会话', 'Previous / next session')],
  ]],
  [tr('眼前这个会话', 'This session'), [
    ['⏎ / esc / 1–9', tr('有批准卡、没在打字', 'Approval card up, not typing'), tr('允许 / 拒绝 / 选第几个', 'Allow / deny / pick by number')],
    ['⌘⏎', tr('有「要落地吗」', 'When "Land it?" shows'), tr('一键落地', 'Land it')],
    ['/ @', tr('输入框', 'Input'), tr('命令 / 文件和文件夹', 'Commands / files and folders')],
    ['↑', tr('输入框空着', 'Input empty'), tr('拿回排着的最后一句来改', 'Take back the last queued line to edit it')],
    ['⌃`', tr('任何时候', 'Anytime'), tr('终端', 'Terminal')],
    [tr('!命令', '!command'), tr('输入框', 'Input'), tr('在终端里跑一次，不进对话', 'Run once in Terminal, not added to the conversation')],
    ['/rewind / /fork', tr('输入框', 'Input'), tr('挑你说过的一句：退回到它之前，或者从它之前分叉', 'Pick a line you said: rewind to before it, or fork from before it')],
    ['/btw / /side', tr('输入框', 'Input'), tr('侧问：后面接一句话，答在最新那段回答下面；不进对话，也不打断它', 'Side question: add a line after it; the answer shows under the latest reply, stays out of the conversation and does not interrupt')],
    ['/export', tr('输入框', 'Input'), tr('整段对话导出成 Markdown', 'Export the whole conversation as Markdown')],
    [tr('← → / 空格', '← → / Space'), tr('大图里', 'In a large image'), tr('上一张 · 下一张 / 收起', 'Previous · next / close')],
    ['?', tr('输入框空着', 'Input empty'), tr('这张表', 'This sheet')],
  ]],
  [tr('消息', 'Messages'), [
    [tr('指着一条消息', 'Hover\u00a0a\u00a0message'), tr('对话里', 'In a conversation'), tr('复制 · 修改 · 表情', 'Copy · edit · react')],
    ['⌘⏎ / esc', tr('修改时', 'While editing'), tr('重发 / 不改了', 'Resend / discard')],
    [tr('选中几个字', 'Select\u00a0text'), tr('回答里', 'In an answer'), tr('引用 · 问一句', 'Quote · ask about it')],
  ]],
  [tr('esc 的顺序', 'What esc does'), [
    ['esc', tr('有东西开着', 'Something is open'), tr('先关最上面一层：修改 → 表情 → 大图 → 弹层 → 补全 → 纸条 → 长曝光 → 她说的那句 → 舞台或并排 → 落地面板', 'Closes the top layer first: edit → reactions → large image → popover → autocomplete → note → Long Exposure → her line → stage or side by side → Land panel')],
    ['esc', tr('什么都没开', 'Nothing open'), tr('有批准卡就拒绝；落地在跑就这一步跑完停；这一轮在跑就打断', 'Denies an approval card; if Land is running, stops after this step; if a turn is running, interrupts it')],
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
  el.innerHTML = `<div class="keys-in" role="dialog" aria-label="${tr('键位', 'Keyboard shortcuts')}" tabindex="-1"><h4>${tr('键位', 'Shortcuts')}<small><kbd>esc</kbd> ${tr('或', 'or')} <kbd>?</kbd> ${tr('关', 'to close')}</small></h4><div class="keys-g">${KEYS.map(([g, rows]) => `<h5>${esc(g)}</h5>`
    + rows.map(([k, where, what]) => `<p><span>${esc(what)}<small>${esc(where)}</small></span><span class="kc">${caps(k)}</span></p>`).join('')).join('')}</div></div>`;
  ctx.win.append(el);
  const inner = el.querySelector<HTMLElement>('.keys-in')!;
  let from: HTMLElement | null = null;
  // A small window cuts the sheet short: its foot says there is more below until the end is in view.
  const more = () => inner.classList.toggle('more', inner.scrollTop + inner.clientHeight < inner.scrollHeight - 4);
  inner.addEventListener('scroll', more, { passive: true });
  new ResizeObserver(() => { if (!el.hidden) more(); }).observe(inner);

  function open() {
    ctx.closeMenu();
    from = document.activeElement as HTMLElement | null;
    el.hidden = false; inner.scrollTop = 0; inner.focus({ preventScroll: true }); more();
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

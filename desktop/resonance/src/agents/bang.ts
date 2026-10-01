// !命令 (design point term): a message that starts with ! is a shell command, not a message. It runs once in the session's
// own terminal (the workbench's pane opens on 终端 and shows it running) and never reaches the agent; the composer keeps
// the keys, as it does for a message sent.
import type { Feature, PageCtx } from './ctx';
import { tr } from './lang';

export function mountBang(ctx: PageCtx): Feature {
  const { ta } = ctx;
  // What is in the composer, when it is a command: its text after the !, or null. Pictures make it a message.
  const cmdOf = () => /^!/.test(ta.value.trimStart()) && !ctx.win.querySelector('.c-files img') ? ta.value.trimStart().slice(1).trim() : null;
  function run() {
    const cmd = cmdOf();
    if (cmd === null) return false;
    if (!cmd) { ctx.toast(tr('! 后面写一条命令', 'Write a command after !')); return true; }
    if (!ctx.wb.run(cmd)) { ctx.toast(tr('先打开一个会话，命令在它的文件夹里跑', 'Open a session first: the command runs in its folder')); return true; }
    ta.value = ''; ta.dispatchEvent(new Event('input')); ctx.draw('comp');
    ctx.cue('send', .5);
    return true;
  }
  return {
    act(a) { return a === 'send' && run(); },
    key(e) {
      if (e.target !== ta || e.key !== 'Enter' || e.shiftKey || e.altKey || e.metaKey || e.ctrlKey || e.isComposing || ta.disabled) return false;
      if (cmdOf() === null) return false;
      e.preventDefault(); e.stopImmediatePropagation();
      if (!e.repeat) run();
      return true;
    },
  };
}

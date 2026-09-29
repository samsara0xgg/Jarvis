// The landing panel on the left (ADR 0085): the host's line from this session's changes to main, one step under the
// other, lit as it goes; the same line folded to a rail of seven dots on the stage, and a small chip in the composer's
// tools so it can be watched with the panel shut. It only draws what the host says and sends back what Allen presses.
import type { Land, LandSt, Sess } from '../../../electron/agents/types';

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
export const STEP_T = ['改动', '门禁', '提交', '合入 main', '重启', '推送', '清理'];
const I = {
  x: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
  ok: '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M2.5 6.2 5 8.5 9.5 3.5"/></svg>',
  bang: '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M6 2.5v4.2M6 9.2v.3"/></svg>',
  pause: '<svg viewBox="0 0 12 12" fill="currentColor"><rect x="3" y="2.5" width="2" height="7" rx=".8"/><rect x="7" y="2.5" width="2" height="7" rx=".8"/></svg>',
};
// A session with nothing under way draws the line as it would start.
const idle = (s: Sess): Land => ({ s: 'run', i: -1, steps: STEP_T.map(() => ({ st: 'todo' as LandSt })), files: [], gates: [], msg: '', branch: s.branch, into: 'main' });
export const active = (l?: Land) => !!l && l.s !== 'done';

export function panelHTML(s: Sess, pre = '') {
  const l = s.land ?? { ...idle(s), msg: pre }, on = !!s.land, who = s.agent === 'codex' ? 'Codex' : 'Claude';
  const merged = on && l.i > 3 && l.steps[3].st !== 'todo';
  const btn = (a: string) => ({
    fix: `<button type="button" class="pri" data-act="lpfix">让 ${who} 修</button>`,
    stay: `<button type="button" data-act="lpstay">${merged ? '就停在这里' : '留在分支上'}</button>`,
    resume: `<button type="button"${l.acts?.[0] === 'resume' ? ' class="pri"' : ''} data-act="lpresume">${l.s === 'fail' && l.i === 5 ? '再试一次' : '继续'}</button>`,
    allow: '<button type="button" class="pri" data-act="lpallow">允许</button>',
    deny: '<button type="button" data-act="lpdeny">拒绝</button>',
  } as Record<string, string>)[a] ?? '';
  const row2 = () => l.acts?.length ? `<div class="row2">${l.acts.map(btn).join('')}</div>` : '';
  const steps = STEP_T.map((t, j) => {
    const st = on ? l.steps[j].st : 'todo', d = l.steps[j].d, cmd = l.steps[j].cmd ?? [];
    const n = st === 'ok' ? I.ok : st === 'run' ? '<span class="spin"></span>' : st === 'fail' ? I.bang : st === 'paused' ? I.pause : String(j + 1);
    const here = on && j === l.i;
    let body = '', extra = '';
    if (j === 0) {
      const add = l.files.reduce((a, f) => a + f[1], 0), del = l.files.reduce((a, f) => a + f[2], 0);
      const n0 = on && l.files.length ? l.files.length : s.dirty?.n ?? 0, a0 = on && l.files.length ? add : s.dirty?.add ?? 0, d0 = on && l.files.length ? del : s.dirty?.del ?? 0;
      body = n0 ? `${n0} 个文件 · <span style="color:var(--mint)">+${a0}</span> <span style="color:#ff9f8f">−${d0}</span> · 只加明确的路径` : '还没看';
      if (l.files.length) {
        const shown = l.files.slice(0, 5), rest = l.files.slice(5);
        const li = (f: string, p: number, m: number) => `<li><span title="${esc(f)}">${esc(f)}</span><i><span class="p">+${p}</span> <span class="m">−${m}</span></i></li>`;
        extra = `<ul class="files">${shown.map(f => li(...f)).join('')}${rest.length ? li(`… 另外 ${rest.length} 个`, rest.reduce((a, f) => a + f[1], 0), rest.reduce((a, f) => a + f[2], 0)) : ''}</ul>`;
      }
    } else if (j === 1) {
      body = esc(d ?? (on ? '' : '按改到的地方挑：Python 跑 Tier 1，desktop 跑类型检查、构建和验收'));
      if (l.gates.length) extra = `<div class="gates">${l.gates.map(g => `<span class="${g.st === 'todo' ? '' : g.st}">${g.st === 'ok' ? '✓ ' : g.st === 'er' ? '✕ ' : ''}${esc(g.say ?? g.n)}</span>`).join('')}</div>`;
    } else if (j === 2) {
      body = esc(d ?? 'commit skill 起草，跑之前可以改');
      if (st !== 'skip') extra = `<input class="msg" id="landmsg" value="${esc(l.msg)}" placeholder="${on && l.drafting ? '在起草…' : '提交标题'}" aria-label="提交标题" autocomplete="off" spellcheck="false"${on && (l.i > 2 || (l.i === 2 && st === 'run') || l.s === 'done') ? ' disabled' : ''}>`;
    } else if (j === 3) body = cmd.length ? cmd.map(c => `<code>${esc(c)}</code>`).join(' · ') : esc(d ?? `把 ${l.branch} 快进到 main`);
    else if (j === 4) { body = esc(d ?? '看改到哪里：daemon、companion，或者都不用'); if (cmd.length && (st === 'run' || st === 'ok')) extra = `<p class="d" style="margin-top:4px">${cmd.map(c => `<code>${esc(c)}</code>`).join('<br>')}</p>`; }
    else if (j === 5) body = `<code>git push origin main</code> · ${esc(d ?? '等你点头')}`;
    else body = cmd.length ? cmd.map(c => `<code>${esc(c)}</code>`).join(' · ') : d ? esc(d) : '<code>git worktree remove</code> · <code>git branch -d</code>';
    // What stopped the line, or what it waits for, sits under the step it stopped at, with the ways on.
    if (here && l.why && (l.s === 'fail' || l.s === 'wait' || l.s === 'paused' || l.s === 'fixing'))
      extra += `<p class="why ${l.s === 'fail' ? 'er' : 'wr'}">${esc(l.why)}</p>${l.s === 'fixing' ? '' : row2()}`;
    const time = st === 'ok' && l.steps[j].ms !== undefined ? `${(l.steps[j].ms! / 1000).toFixed(1)}s` : '';
    return `<li class="st" data-s="${st}"><span class="n">${n}</span><div><h5>${t}<em>${time}</em></h5><div class="d">${body}</div>${extra}</div></li>`;
  });
  const restarted = l.restart?.length ? ` · ${l.restart.join(' 和 ')} 刚重启` : '';
  const foot = !on ? (s.dirty ? '<button type="button" class="pri" data-act="land">一键落地 <kbd>⌘⏎</kbd></button>' : '<small>没有要落地的改动</small>')
    : l.s === 'run' || l.s === 'stopping' ? `<small>${l.s === 'stopping' ? '这一步跑完就停…' : '一步一步在走，你可以继续干别的'}</small><button type="button" data-act="lpstop"${l.s === 'stopping' ? ' disabled' : ''}>打断 <kbd>esc</kbd></button>`
    : l.s === 'done' ? `<small style="color:var(--mint)">落地完成${esc(restarted)}</small>${l.restart?.length ? '<button type="button" data-act="lpsvc">看服务</button>' : ''}`
    : l.s === 'fixing' ? `<small>${esc(l.why ?? '')}</small>`
    : `<small>${l.s === 'wait' ? '等你点头' : l.s === 'fail' && l.i === 1 ? '门禁没过，停在这里' : '停在这里'}</small>`;
  return { steps, foot, branch: `${esc(l.branch || s.branch || '—')} <i>→</i> main` };
}
export function railHTML(s: Sess) {
  const l = s.land;
  return `<button type="button" data-act="lpwide" aria-label="展开落地面板"><b>落地</b>${STEP_T.map((_, j) => `<i data-s="${l ? l.steps[j].st : 'todo'}"></i>`).join('')}</button>`;
}
// The chip: where the line is, as a little ring filling step by step.
export function chipOf(s: Sess) {
  const l = s.land;
  if (!l) return null;
  const k = ({ run: 'run', stopping: 'run', fixing: 'run', wait: 'wait', paused: 'paused', fail: 'fail', done: 'done' } as const)[l.s];
  const lab = l.s === 'done' ? '落地完成' : l.s === 'wait' ? '落地 · 等你点头' : l.s === 'fail' ? l.i === 1 ? '落地 · 门禁没过' : `落地 · ${STEP_T[l.i]}没成` : l.s === 'paused' ? `落地 · 停在${STEP_T[l.i]}前`
    : l.s === 'fixing' ? `落地 · 在修` : `落地 · ${STEP_T[l.i]} ${l.i + 1}/7`;
  const pr = l.s === 'done' ? 1 : (l.i + (l.s === 'run' ? .5 : 0)) / 7;
  const col = { run: '#6C9CFF', wait: '#FFC98F', paused: '#FFC98F', fail: '#FF7A66', done: '#6FE0B4' }[k];
  return { k, html: `<svg viewBox="0 0 14 14" style="transform:rotate(-90deg)"><circle cx="7" cy="7" r="5.5" fill="none" stroke="rgba(255,255,255,.12)" stroke-width="1.6"/><circle cx="7" cy="7" r="5.5" fill="none" stroke="${col}" stroke-width="1.6" stroke-linecap="round" stroke-dasharray="${(pr * 34.56).toFixed(2)} 40"/></svg><span class="lbl">${esc(lab)}</span>` };
}
export const CLOSE_ICON = I.x;

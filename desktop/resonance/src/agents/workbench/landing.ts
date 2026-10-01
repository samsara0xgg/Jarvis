// The landing panel on the left (ADR 0085, cut down in review 18): the host's line from this session's changes to the
// default branch, one step under the other, lit as it goes; only the steps this landing has (门禁 where the repository
// has gates, 重启 where merging restarts something, 推送 where there is an origin); the same line folded to a rail of
// dots on the stage, and a small chip in the composer's tools so it can be watched with the panel shut. The first
// landing in a repository that could go either way asks which, once. Done, it is one line. It only draws what the host
// says and sends back what Allen presses.
import type { Land, LandSt, LandVia, Sess } from '../../../electron/agents/types';
import { plural, tr } from '../lang';

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
// The host's steps, in its order (land.ts STEPS): every landing has all seven, and the ones that do nothing are skipped.
const IDS = ['diff', 'gate', 'commit', 'merge', 'restart', 'push', 'clean'] as const;
const I = {
  x: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
  ok: '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M2.5 6.2 5 8.5 9.5 3.5"/></svg>',
  bang: '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M6 2.5v4.2M6 9.2v.3"/></svg>',
  pause: '<svg viewBox="0 0 12 12" fill="currentColor"><rect x="3" y="2.5" width="2" height="7" rx=".8"/><rect x="7" y="2.5" width="2" height="7" rx=".8"/></svg>',
};
// A session with nothing under way draws the line as it would start.
const idle = (s: Sess): Land => ({ s: 'run', i: -1, steps: IDS.map(() => ({ st: 'todo' as LandSt })), files: [], gates: (s.dirty?.gates ?? []).map(n => ({ n, st: 'todo' as const })),
  msg: '', branch: s.branch, into: s.dirty?.into ?? 'main', via: s.dirty?.ways[0] ?? 'merge', restart: s.dirty?.restart });
export const active = (l?: Land) => !!l && l.s !== 'done';
// It restarted Jarvis's own services (not a command the owner gave), so there are services to look at once it is done.
export const svcOf = (l?: Land) => !!l?.restart?.length && l.restart.every(r => r === 'daemon' || r === 'companion');
// The way this landing goes; undefined while the repository's first landing has not been told.
export const wayOf = (s: Sess): LandVia | undefined => s.land ? s.land.via : s.dirty?.ask ? undefined : s.dirty?.ways[0];
// A pull request's number, for 「PR #128」.
export const prNo = (url?: string) => /\/pull\/(\d+)/.exec(url ?? '')?.[1];

// The host's step indices this landing shows, in order. A step that did something always shows.
function stepsOf(s: Sess, l: Land) {
  const via = l.via, into = l.into, branch = l.branch || s.branch, did = (j: number) => ['ok', 'fail', 'wait'].includes(l.steps[j].st);
  const started = !!s.land && l.i > 0;
  const local = started ? !l.steps[5].cmd?.length : !!s.dirty?.local;
  const show: Record<typeof IDS[number], boolean> = {
    diff: true,
    gate: l.gates.length > 0,
    commit: true,
    merge: via === 'merge' && branch !== into,
    restart: via === 'merge' && !!l.restart?.length,
    push: via === 'pr' || !local,
    clean: (s.tree || !!s.gone) && branch !== into,
  };
  return IDS.map((id, j) => show[id] || did(j) ? j : -1).filter(j => j >= 0);
}
// Where the line is among the steps it shows: the step under way, or the next one shown after a skipped one.
const posOf = (vis: number[], i: number) => { const k = vis.findIndex(j => j >= i); return k < 0 ? vis.length - 1 : k; };
const title = (l: Land, j: number) => IDS[j] === 'merge' ? tr(`合进 ${l.into}`, `Merge into ${l.into}`) : IDS[j] === 'push' && l.via === 'pr' ? tr('推分支、开 PR', 'Push branch, open PR')
  : ({ diff: tr('改动', 'Changes'), gate: tr('门禁', 'Checks'), commit: tr('提交', 'Commit'), restart: tr('重启', 'Restart'), push: tr('推送', 'Push'), clean: tr('清理 worktree', 'Clean up worktree') } as Record<string, string>)[IDS[j]];
const plusMinus = (a: number, d: number) => `<span class="p">+${a}</span> <span class="m">−${d}</span>`;
const choose = (s: Sess, cls: string) => `<span class="${cls}"><button type="button" data-act="landvia" data-v="merge">${tr('合进', 'Merge into')} ${esc(s.dirty?.into ?? 'main')}</button><button type="button" data-act="landvia" data-v="pr">${tr('推分支开', 'Push branch, open')} PR</button></span>`;

export function panelHTML(s: Sess, pre = '') {
  const l = s.land ?? { ...idle(s), msg: pre }, on = !!s.land, who = s.agent === 'codex' ? 'Codex' : 'Claude', via = wayOf(s);
  const branch = esc(l.branch || s.branch || '—');
  // Not told yet: the one question, in the panel as in the conversation.
  if (!on && !via) {
    return { steps: [`<li class="st op-l1" data-s="wait"><span class="n">?</span><div><h5>${tr('走哪条', 'Merge or open a PR?')}</h5><div class="d">${tr('', 'First landing in ')}${esc(s.project)}${tr(' 第一次落地。选一次，这个仓库以后都这样。', '. Choose once; it applies to this repository from now on.')}</div>${choose(s, 'row2 op-choose')}</div></li>`],
      foot: '', branch: `${branch} <i>→</i> ?` };
  }
  // Done: one line.
  if (on && l.s === 'done') {
    const pr = prNo(l.pr), pushed = l.steps[5].st === 'ok';
    const head = l.via === 'pr' ? pr ? `${tr('落地了', 'Landed')} · <button type="button" class="op-pr" data-act="landpr">PR #${pr}</button>` : l.pr ? `${tr('落地了', 'Landed')} · <button type="button" class="op-pr" data-act="landpr">${tr('开', 'Open')} PR</button>` : tr('落地了 · 分支推上去了', 'Landed · branch pushed')
      : tr(`落地了 · 合进 ${esc(l.into)}`, `Landed · merged into ${esc(l.into)}`);
    const d = l.via === 'pr' ? pr ? tr('PR 开好了，worktree 留着接着改', 'PR opened, worktree kept for more edits') : l.pr ? tr('分支推上去了，点链接开 PR', 'Branch pushed, open the PR from the link') : tr('分支推上去了，PR 要你自己开', 'Branch pushed, the PR is yours to open')
      : tr(`${esc(l.into)} ${pushed ? '推上去了' : '在本地'}${s.gone ? '，worktree 清掉了' : ''}`, `${esc(l.into)} ${pushed ? 'pushed' : 'is local only'}${s.gone ? ', worktree removed' : ''}`);
    return { steps: [`<li class="st op-l1" data-s="ok"><span class="n">${I.ok}</span><div><h5><span>${head}</span></h5><div class="d">${d}</div></div></li>`],
      foot: svcOf(l) ? `<small style="color:var(--mint)">${tr('落地完成', 'Landed')} · ${esc(l.restart!.join(tr(' 和 ', ' and ')))} ${tr('刚重启', 'just restarted')}</small><button type="button" data-act="lpsvc">${tr('看服务', 'View services')}</button>` : '',
      branch: `${branch} <i>→</i> ${l.via === 'pr' ? 'PR' : esc(l.into)}` };
  }
  const merged = on && l.steps[3].st === 'ok';
  const btn = (a: string) => ({
    fix: `<button type="button" class="pri" data-act="lpfix">${tr('让', 'Let')} ${who} ${tr('修', 'fix it')}</button>`,
    stay: `<button type="button" data-act="lpstay">${merged ? tr('就停在这里', 'Stop here') : tr('留在分支上', 'Stay on the branch')}</button>`,
    resume: `<button type="button"${l.acts?.[0] === 'resume' ? ' class="pri"' : ''} data-act="lpresume">${l.s === 'fail' && IDS[l.i] === 'push' ? tr('再试一次', 'Try again') : tr('继续', 'Continue')}</button>`,
    allow: `<button type="button" class="pri" data-act="lpallow">${tr('允许', 'Allow')}</button>`,
    deny: tr('<button type="button" data-act="lpdeny">拒绝</button>', '<button type="button" data-act="lpdeny">Deny</button>'),
  } as Record<string, string>)[a] ?? '';
  const row2 = () => l.acts?.length ? `<div class="row2">${l.acts.map(btn).join('')}</div>` : '';
  const vis = stepsOf(s, l), here = on ? posOf(vis, l.i) : -1;
  const steps = vis.map((j, k) => {
    // A skipped step's run or stop shows on the next step shown.
    const st0 = on ? l.steps[j].st : 'todo', st = k === here && vis[k] !== l.i && (l.steps[l.i]?.st === 'run' || l.steps[l.i]?.st === 'paused') ? l.steps[l.i].st : st0;
    const d = l.steps[j].d, cmd = l.steps[j].cmd ?? [], id = IDS[j];
    const n = st === 'ok' ? I.ok : st === 'run' ? '<span class="spin"></span>' : st === 'fail' ? I.bang : st === 'paused' ? I.pause : String(k + 1);
    let body = '', extra = '';
    if (id === 'diff') {
      const files = on && l.files.length ? l.files : [], nf = files.length || s.dirty?.n || 0;
      const add = files.length ? files.reduce((a, f) => a + f[1], 0) : s.dirty?.add ?? 0, del = files.length ? files.reduce((a, f) => a + f[2], 0) : s.dirty?.del ?? 0;
      body = nf ? tr(`<button type="button" class="op-lsum" data-act="changes">${nf} 个文件 · ${plusMinus(add, del)}</button>`, `<button type="button" class="op-lsum" data-act="changes">${plural(nf, 'file')} · ${plusMinus(add, del)}</button>`) : tr('还没看', 'Not reviewed yet');
      if (files.length) {
        const shown = files.slice(0, files.length > 6 ? 5 : 6), rest = files.slice(shown.length);
        const li = (label: string, p: string, a: number, m: number) => `<li><button type="button" data-act="changes" data-path="${esc(p)}"><span title="${esc(label)}">${esc(label)}</span><i>${plusMinus(a, m)}</i></button></li>`;
        extra = `<ul class="files">${shown.map(f => li(f[0], ...f)).join('')}${rest.length ? li(tr(`另外 ${rest.length} 个`, `${rest.length} more`), rest[0][0], rest.reduce((a, f) => a + f[1], 0), rest.reduce((a, f) => a + f[2], 0)) : ''}</ul>`;
      }
    } else if (id === 'gate') {
      body = esc(d ?? tr(`要过 ${l.gates.length} 项`, `${plural(l.gates.length, 'check')} to pass`));
      extra = `<div class="gates">${l.gates.map(g => `<span class="${g.st === 'todo' ? '' : g.st}">${g.st === 'ok' ? '✓ ' : g.st === 'er' ? '✕ ' : ''}${esc(g.say ?? g.n)}${g.st === 'er' ? tr(' · 没过', ' · failed') : ''}</span>`).join('')}</div>`;
    } else if (id === 'commit') {
      body = esc(d ?? tr('commit skill 起草，跑之前可以改', 'Drafted by the commit skill, editable before it runs'));
      if (st !== 'skip') extra = `<input class="msg" id="landmsg" value="${esc(l.msg)}" placeholder="${on && l.drafting ? tr('在起草…', 'Drafting…') : tr('提交标题', 'Commit title')}" aria-label="${tr('提交标题', 'Commit title')}" autocomplete="off" spellcheck="false"${on && (l.i > 2 || (l.i === 2 && st === 'run')) ? ' disabled' : ''}>`;
    } else if (id === 'merge') body = cmd.length ? cmd.map(c => `<code>${esc(c)}</code>`).join(' · ') : esc(d ?? tr(`把 ${l.branch || s.branch} 快进到 ${l.into}，不留合并提交`, `Fast-forward ${l.into} to ${l.branch || s.branch}, no merge commit`));
    else if (id === 'restart') { body = esc(d ?? (svcOf(l) ? tr(`重启 ${l.restart!.join(' 和 ')}`, `Restart ${l.restart!.join(' and ')}`) : tr('跑你给这个仓库配的重启命令', 'Run the restart command set for this repository'))); if (cmd.length && (st === 'run' || st === 'ok')) extra = `<p class="d" style="margin-top:4px">${cmd.map(c => `<code>${esc(c)}</code>`).join('<br>')}</p>`; }
    else if (id === 'push') {
      const c = cmd.length ? cmd : l.via === 'pr' ? [`git push -u origin ${l.branch || s.branch}`] : [`git push origin ${l.into}`];
      body = tr(`${c.map(x => `<code>${esc(x)}</code>`).join(' · ')} · ${esc(d ?? '等你点头')}`, `${c.map(x => `<code>${esc(x)}</code>`).join(' · ')} · ${esc(d ?? 'Waiting for your go-ahead')}`);
    } else body = cmd.length ? cmd.map(c => `<code>${esc(c)}</code>`).join(' · ') : d ? esc(d) : l.via === 'pr' ? tr('PR 开着的时候，worktree 留着接着改', 'The worktree stays while the PR is open') : '<code>git worktree remove</code> · <code>git branch -d</code>';
    // What stopped the line, or what it waits for, sits under the step it stopped at, with the ways on.
    if (k === here && l.why && (l.s === 'fail' || l.s === 'wait' || l.s === 'paused' || l.s === 'fixing'))
      extra += `<p class="why ${l.s === 'fail' ? 'er' : 'wr'}">${esc(l.why)}</p>${l.s === 'fixing' ? '' : row2()}`;
    const time = st === 'ok' && l.steps[j].ms !== undefined ? `${(l.steps[j].ms! / 1000).toFixed(1)}s` : '';
    return `<li class="st" data-s="${st}" data-id="${id}"><span class="n">${n}</span><div><h5>${esc(title(l, j))}<em>${time}</em></h5><div class="d">${body}</div>${extra}</div></li>`;
  });
  const foot = !on ? (s.dirty ? `<button type="button" class="pri" data-act="land">${tr('一键落地', 'Land it')} <kbd>⌘⏎</kbd></button>` : tr('<small>没有要落地的改动</small>', '<small>Nothing to land</small>'))
    : l.s === 'run' || l.s === 'stopping' ? `<small>${l.s === 'stopping' ? tr('这一步跑完就停…', 'Stopping after this step…') : tr('一步一步在走，你可以接着干别的', 'Running step by step, you can keep working')}</small><button type="button" data-act="lpstop"${l.s === 'stopping' ? ' disabled' : ''}>${tr('打断', 'Interrupt')} <kbd>esc</kbd></button>`
    : l.s === 'fixing' ? `<small>${esc(l.why ?? '')}</small>`
    : `<small>${l.s === 'wait' ? tr('等你点头', 'Waiting for your go-ahead') : l.s === 'fail' && IDS[l.i] === 'gate' ? tr('门禁没过，停在这里', 'Checks failed, stopped here') : tr('停在这里', 'Stopped here')}</small>`;
  return { steps, foot, branch: `${branch} <i>→</i> ${l.via === 'pr' ? 'PR' : esc(l.into)}` };
}
export function railHTML(s: Sess) {
  const l = s.land ?? idle(s), vis = s.land?.s === 'done' ? [-1] : stepsOf(s, l);
  return `<button type="button" data-act="lpwide" aria-label="${tr('展开落地面板', 'Expand landing panel')}"><b>${tr('落地', 'Land')}</b>${vis.map(j => `<i data-s="${j < 0 ? 'ok' : s.land ? l.steps[j].st : 'todo'}"></i>`).join('')}</button>`;
}
// Where the line is, in words: the chip's and the conversation's.
export function labelOf(s: Sess) {
  const l = s.land;
  if (!l) return '';
  const vis = stepsOf(s, l), k = posOf(vis, l.i), t = title(l, vis[k] ?? l.i);
  const low = (x: string) => x.charAt(0).toLowerCase() + x.slice(1);
  return l.s === 'done' ? tr('落地了', 'Landed') : l.s === 'wait' ? tr('落地 · 等你点头', 'Land · waiting for go-ahead') : l.s === 'fail' ? IDS[l.i] === 'gate' ? tr('落地 · 门禁没过', 'Land · checks failed') : tr(`落地 · ${t}没成`, `Land · ${low(t)} failed`) : l.s === 'paused' ? tr(`落地 · 停在${t}前`, `Land · paused before ${low(t)}`)
    : l.s === 'fixing' ? tr('落地 · 在修', 'Land · fixing') : `${tr('落地', 'Land')} · ${t} ${k + 1}/${vis.length}`;
}
// The chip: where the line is, as a little ring filling step by step.
export function chipOf(s: Sess) {
  const l = s.land;
  if (!l) return null;
  const k = ({ run: 'run', stopping: 'run', fixing: 'run', wait: 'wait', paused: 'paused', fail: 'fail', done: 'done' } as const)[l.s];
  const vis = stepsOf(s, l), pr = l.s === 'done' ? 1 : (posOf(vis, l.i) + (l.s === 'run' ? .5 : 0)) / Math.max(1, vis.length);
  const col = { run: '#6C9CFF', wait: '#FFC98F', paused: '#FFC98F', fail: '#FF7A66', done: '#6FE0B4' }[k];
  return { k, html: `<svg viewBox="0 0 14 14" style="transform:rotate(-90deg)"><circle cx="7" cy="7" r="5.5" fill="none" stroke="rgba(255,255,255,.12)" stroke-width="1.6"/><circle cx="7" cy="7" r="5.5" fill="none" stroke="${col}" stroke-width="1.6" stroke-linecap="round" stroke-dasharray="${(pr * 34.56).toFixed(2)} 40"/></svg><span class="lbl">${esc(labelOf(s))}</span>` };
}
// Under the last answer: the question the first time, what landing will do and the key for it, where it is while it
// runs, and one line once it is done. '' when there is nothing to say.
export function askHTML(s: Sess) {
  const l = s.land, via = wayOf(s);
  if (s.term || (s.st !== 'done' && !active(l))) return '';
  if (active(l)) return `<p class="ask-q">${tr('要落地吗？', 'Land it? ')}${sumOf(s, l!.via)}</p><button type="button" data-act="landpanel"><span class="when-shut">${tr('看落地进度', 'View progress')}</span><span class="when-open">${tr('落地面板开着', 'Landing panel open')}</span></button><span>${esc(labelOf(s))}</span>`;
  if (!s.dirty || s.gone) {
    if (l?.s !== 'done') return '';
    const pr = prNo(l.pr);
    return `<p class="op-landed"><b>✓</b><span>${tr('落地了', 'Landed')} · ${l.via === 'pr' ? pr ? `<button type="button" data-act="landpr">PR #${pr}</button>` : l.pr ? tr('<button type="button" data-act="landpr">开 PR</button>', '<button type="button" data-act="landpr">Open PR</button>') : tr('分支推上去了', 'branch pushed') : tr(`合进 ${esc(l.into)}`, `merged into ${esc(l.into)}`)}</span></p>`;
  }
  if (!via) return `<p class="ask-q">${tr('要落地吗？', 'Land it? First landing in ')}${esc(s.project)}${tr(' 第一次落地，走哪条？', ', which way?')}</p>${choose(s, 'op-choose')}<span>${tr('选一次，这个仓库以后都这样', 'Choose once; it applies to this repository from now on')}</span>`;
  return `<p class="ask-q">${tr('要落地吗？', 'Land it? ')}${sumOf(s, via)}</p><button type="button" data-act="land">${tr('一键落地', 'Land it')} <kbd>⌘⏎</kbd></button><span>${via === 'pr' ? tr('推分支之前会等你点头', 'It waits for your go-ahead before pushing the branch') : s.dirty.local ? tr('这个仓库没有 origin，不推', 'This repository has no origin, so nothing is pushed') : tr('推送那一步会等你点头', 'The push step waits for your go-ahead')}</span>`;
}
function sumOf(s: Sess, via: LandVia) {
  if (via === 'pr') return tr('提交，推分支开 PR。', 'Commit, push the branch, open a PR.');
  const l = s.land ?? idle(s), vis = stepsOf(s, { ...l, via });
  const en = [vis.includes(2) && 'commit', vis.includes(3) && `merge into ${esc(l.into)}`, vis.includes(4) && (svcOf(l) ? `restart ${l.restart!.join(' and ')}` : 'run the restart command'), vis.includes(5) && 'push'].filter(Boolean).join(', ');
  return tr([vis.includes(2) && '提交', vis.includes(3) && `合进 ${esc(l.into)}`, vis.includes(4) && (svcOf(l) ? `重启 ${l.restart!.join(' 和 ')}` : '跑重启命令'), vis.includes(5) && '推送'].filter(Boolean).join('、') + '。', en.charAt(0).toUpperCase() + en.slice(1) + '.');
}
export const CLOSE_ICON = I.x;

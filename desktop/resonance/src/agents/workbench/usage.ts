// The composer's one ring (workbench, after Claude's own): it draws the tightest of the context window and the plan's
// windows, stays quiet until one gets close, and opens a card with all of them. A plan window used up becomes one line
// in the composer with the way on, not a red box.
import type { Agent, Ctx, Sess, Usage, UsageWindow } from '../../../electron/agents/types';
import { hhmm, tr } from '../lang';

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
export const kt = (n: number) => n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${n >= 1e5 ? Math.round(n / 1000) : +(n / 1000).toFixed(1)}k` : String(Math.round(n));
const level = (v: number) => v >= 100 ? '#FF7A66' : v >= 80 ? '#FFC98F' : '';
const WEEK = [tr('周日', 'Sun'), tr('周一', 'Mon'), tr('周二', 'Tue'), tr('周三', 'Wed'), tr('周四', 'Thu'), tr('周五', 'Fri'), tr('周六', 'Sat')];
// When a window starts over, as a person says it: 17:00, 明天 1:00, 周二 5:00.
export function resetAt(iso: string | null) {
  if (!iso) return '';
  const d = new Date(iso), now = new Date(), t = tr(`${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`, hhmm(d.getTime()));
  const days = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date(now).setHours(0, 0, 0, 0)) / 864e5);
  return tr(`${days <= 0 ? '' : days === 1 ? '明天 ' : `${WEEK[d.getDay()]} `}${t} 重置`, `Resets ${days <= 0 ? '' : days === 1 ? 'tomorrow ' : `${WEEK[d.getDay()]} `}${t}`);
}
const plan = (p: string) => /^(\d+)X$/i.test(p) ? `Max (${p.slice(0, -1)}x)` : p;
function nameOf(a: Agent, w: UsageWindow) {
  if (w.key === 'five_hour') return tr('5 小时', '5 hours');
  if (w.key === 'seven_day') return tr('每周 · 全部模型', 'Weekly · all models');
  if (w.key.startsWith('seven_day_')) return tr(`每周 · ${w.label.split('·').pop()!.trim()}`, `Weekly · ${w.label.split('·').pop()!.trim()}`);
  return a === 'codex' && /7/.test(w.label) ? tr('每周', 'Weekly') : w.label;
}
const ctxPct = (s: Sess, cx: Ctx | string | undefined) => typeof cx === 'object' && cx.max ? cx.used / cx.max * 100 : s.ctx;

// The ring: the tightest share, its colour, and what it says when pointed at.
export function ring(s: Sess | undefined, agent: Agent, u: Usage | null, cx: Ctx | string | undefined) {
  const ws = u?.[agent]?.windows ?? [], c = s ? ctxPct(s, cx) : 0;
  const top = ws.reduce((m, w) => w.percent > m.p ? { p: w.percent, n: nameOf(agent, w), w } : m, { p: c, n: tr('上下文', 'Context'), w: null as UsageWindow | null });
  const col = level(top.p) || 'rgba(157,180,255,.85)';
  const tip = top.w && top.p >= 100 ? tr(`${top.n} 用完了 · ${resetAt(top.w.resets_at)}`, `${top.n} used up · ${resetAt(top.w.resets_at)}`) : tr(`最紧的一项：${top.n} ${Math.round(top.p)}%`, `Tightest: ${top.n} ${Math.round(top.p)}%`);
  // A filled wedge in a thin ring, a gauge that stands still: an open arc on a faint track reads as a spinner.
  const svg = `<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.6" fill="none" stroke="rgba(255,255,255,.3)" stroke-width="1.2"/><circle cx="8" cy="8" r="2.75" fill="none" stroke="${col}" stroke-width="5.5" stroke-dasharray="${(Math.min(100, top.p) / 100 * 17.28).toFixed(2)} 18"/></svg>`;
  return { svg, tip };
}
// A plan window used up: which one, and when it starts over.
export function banner(agent: Agent, u: Usage | null) {
  const out = (u?.[agent]?.windows ?? []).filter(w => w.percent >= 100).sort((a, b) => Date.parse(b.resets_at ?? '') - Date.parse(a.resets_at ?? ''))[0];
  if (!out) return null;
  const what = out.key === 'five_hour' ? tr('5 小时额度用完了', '5-hour limit reached') : out.key.startsWith('seven_day_') ? tr(`每周 ${out.label.split('·').pop()!.trim()} 额度用完了`, `Weekly ${out.label.split('·').pop()!.trim()} limit reached`) : tr('每周额度用完了', 'Weekly limit reached');
  return `<b>${esc(what)}</b>${out.resets_at ? ` · ${esc(resetAt(out.resets_at))}` : ''}`;
}
// The card: the context window, then each plan window with when it starts over, then the ways on.
export function card(s: Sess | undefined, agent: Agent, u: Usage | null, cx: Ctx | string | undefined, detail: { open: boolean; html: string }, colors: Record<string, string>) {
  const bar = (v: number, c: string) => `<span class="bar"><i style="width:${Math.min(100, v)}%;background:${c}"></i></span>`;
  let ctx = '';
  if (s) {
    const x = typeof cx === 'object' && cx.max ? cx : null, pct = Math.round(ctxPct(s, cx));
    const segs = x ? x.rows.filter(r => r.kind !== 'free').map(r => `<i style="width:${(r.t / x.max * 100).toFixed(2)}%;background:${r.kind ? 'rgba(196,204,238,.35)' : colors[r.n] ?? '#9aa3c7'}"></i>`).join('') : `<i style="width:${pct}%;background:#6C9CFF"></i>`;
    ctx = `<div class="sec" style="padding-top:0"><h5>${tr('上下文', 'Context')}<b>${x ? `${kt(x.used)} / ${kt(x.max)} · ` : ''}${pct}%</b></h5><div class="row" style="margin-top:6px"><span class="bar">${segs}</span></div>`
      + `<button type="button" class="more" data-act="cxmore" aria-expanded="${detail.open}">${detail.open ? tr('收起', 'Hide details') : typeof cx === 'string' ? esc(cx) : cx === undefined ? tr('在量…', 'Measuring…') : tr('都占了什么', 'What is using it')}</button>${detail.open ? `<div class="cx-detail">${detail.html}</div>` : ''}</div>`;
  }
  const p = u?.[agent];
  const rows = p ? `<div class="sec"><p class="sub">${tr('套餐', 'Plan')} · ${esc(plan(p.plan))}</p>${p.windows.map(w => `<div class="row"><span>${esc(nameOf(agent, w))}</span><em>${w.resets_at && w.key !== 'five_hour' || w.percent >= 100 ? `<small>${esc(resetAt(w.resets_at))}</small>` : ''}${Math.round(w.percent)}%</em>${bar(w.percent, level(w.percent) || '#6C9CFF')}</div>`).join('')}</div>`
    : `<div class="sec"><p class="sub">${tr('套餐', 'Plan')}</p><p class="note">${tr('还没读到额度：daemon 每几分钟读一次', 'No usage yet: the daemon reads it every few minutes')}</p></div>`;
  const outNow = (p?.windows ?? []).some(w => w.percent >= 100);
  return `${ctx}${rows}<div class="act">${outNow && agent === 'claude' && s ? `<button type="button" class="pri" data-act="cloud">${tr('挪到云端继续', 'Continue in cloud')}</button>` : ''}<button type="button" data-act="usagepage">${tr('看明细', 'Usage details')}</button></div>`;
}

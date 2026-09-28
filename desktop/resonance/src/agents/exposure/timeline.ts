import type { Item, Req, Sess, St, Step } from '../../../electron/agents/types';

export type Turn = { item: number; at?: number; you: string; reply: string; kind: 'sum' | 'live' | 'wait' | 'err' | 'steps' | 'none' };
export type Trail = {
  segs: { a: number; b: number | null; k: 'work' | 'wait' | 'idle' | 'stop' }[];
  marks: { t: number; k: 'you' | 'done' | 'err' | 'her'; turn?: number }[];
  activity: number[];
  turns: Turn[];
};
const minute = (at: number) => at / 60000;
const kind = (st: St): Trail['segs'][number]['k'] => st === 'wait' ? 'wait' : st === 'err' ? 'stop' : st === 'done' ? 'idle' : 'work';
// The folded steps line, as the conversation writes it, in plain words.
function stepsLine(steps: Step[], took?: string) {
  const n = (k: Step['k']) => steps.filter(s => s.k === k).length;
  const add = steps.reduce((a, s) => a + (s.add ?? 0), 0), del = steps.reduce((a, s) => a + (s.del ?? 0), 0);
  return [took ? `干了 ${took}` : '干完了', n('read') + n('search') ? `读了 ${n('read') + n('search')} 个` : '', n('edit') ? `改了 ${n('edit')} 个 +${add} −${del}` : '',
    n('bash') ? `跑了 ${n('bash')} 条` : '', n('agent') ? `${n('agent')} 个子任务` : '', n('web') ? `查了 ${n('web')} 次网页` : '', n('tool') ? `用了 ${n('tool')} 个工具` : '']
    .filter(Boolean).join(' · ');
}
const lastOf = (xs: Item[], k: Item['k'], open = false) => { for (let i = xs.length - 1; i >= 0; i--) if (xs[i].k === k && !(open && (xs[i] as Item & { k: 'req' }).done)) return xs[i]; };
const asks = (r: Req) => r.tool === 'Ask' ? r.qs.map(q => q.q).join(' · ') : r.tool === 'Plan' ? '计划写好了，等你看' : r.why;
// Transcript times and host-observed transitions only; an untimed sentence remains readable, never a made-up tick.
export function timeline(s: Sess, items: Item[]): Trail {
  const out: Trail = { segs: [], marks: [], activity: [], turns: [] };
  const events: { at: number; k: Trail['segs'][number]['k'] }[] = [];
  // What followed each thing you said, up to the next one.
  const blocks: Item[][] = [];
  for (const [i, it] of items.entries()) {
    if (it.k === 'you') {
      out.turns.push({ item: i, at: it.at, you: it.text || (it.files ?? []).join('、'), reply: '', kind: 'none' });
      blocks.push([]);
      if (it.at !== undefined) {
        out.marks.push({ t: minute(it.at), k: 'you', turn: out.turns.length - 1 });
        events.push({ at: it.at, k: 'work' });
      }
      continue;
    }
    blocks.at(-1)?.push(it);
    if (it.k === 'it') {
      if (it.at !== undefined) { events.push({ at: it.at, k: 'idle' }); out.marks.push({ t: minute(it.at), k: 'done' }); }
    } else if (it.k === 'steps') {
      for (const step of it.steps) if (step.at !== undefined) out.activity.push(minute(step.at));
    } else if (it.k === 'req' && it.at !== undefined) {
      events.push({ at: it.at, k: 'wait' });
      if (it.ended !== undefined) events.push({ at: it.ended, k: 'work' });
    }
  }
  // Its reply in that turn: the last thing it said, or where the turn stands, as the B01 words read it.
  out.turns.forEach((turn, i) => {
    const after = blocks[i], last = i === out.turns.length - 1;
    const said = lastOf(after, 'it'), steps = lastOf(after, 'steps'), pending = lastOf(after, 'req', true);
    if (last && s.st === 'err') Object.assign(turn, { kind: 'err', reply: s.summary || '出错了' });
    else if (said?.k === 'it') Object.assign(turn, { kind: 'sum', reply: said.text });
    else if (last && s.st === 'wait') Object.assign(turn, { kind: 'wait', reply: pending?.k === 'req' ? asks(pending.req) || s.summary : s.summary });
    else if (last && (s.st === 'work' || s.st === 'pack')) Object.assign(turn, { kind: 'live', reply: s.now || '在干活…' });
    else if (steps?.k === 'steps' && steps.steps.length) Object.assign(turn, { kind: 'steps', reply: stepsLine(steps.steps, steps.took) });
    else turn.reply = last ? '还没回' : '没等它回，你接着又说了一句';
  });
  // Observed transitions win when the agent records a last text before it actually finishes.
  for (const event of s.trace ?? []) {
    events.push({ at: event.at, k: kind(event.st) });
    if (event.st === 'err') out.marks.push({ t: minute(event.at), k: 'err' });
  }
  events.sort((a, b) => a.at - b.at);
  for (const e of events) {
    const last = out.segs.at(-1), at = minute(e.at);
    if (last?.k === e.k) continue;
    if (last) last.b = at;
    out.segs.push({ a: at, b: null, k: e.k });
  }
  // A pre-existing session without a timed transcript can only expose its known present state.
  if (!out.segs.length) out.segs.push({ a: minute(s.trace?.at(-1)?.at ?? s.updated), b: null, k: kind(s.st) });
  return out;
}
export const activityAt = (trail: Trail, time: number) => Math.min(1, .25 + trail.activity.reduce((n, at) => n + (Math.abs(at - time) < 1 ? .18 : 0), 0));
export const started = (s: Sess, trail?: Trail) => s.created ?? ((trail?.segs[0]?.a ?? s.updated / 60000) * 60000);

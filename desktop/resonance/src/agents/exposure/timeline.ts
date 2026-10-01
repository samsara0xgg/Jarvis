import type { Item, Req, Sess, St, Step } from '../../../electron/agents/types';
import { plural, tr } from '../lang.ts'; // with its extension: a check runs this module in Node

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
  return [took ? tr(`干了 ${took}`, `Worked ${took}`) : tr('干完了', 'Done'), n('read') + n('search') ? tr(`读了 ${n('read') + n('search')} 个`, `Read ${plural(n('read') + n('search'), 'file')}`) : '', n('edit') ? tr(`改了 ${n('edit')} 个 +${add} −${del}`, `Edited ${plural(n('edit'), 'file')} +${add} −${del}`) : '',
    n('bash') ? tr(`跑了 ${n('bash')} 条`, `Ran ${plural(n('bash'), 'command')}`) : '', n('agent') ? tr(`${n('agent')} 个子任务`, plural(n('agent'), 'subtask')) : '', n('web') ? tr(`查了 ${n('web')} 次网页`, plural(n('web'), 'web lookup')) : '', n('tool') ? tr(`用了 ${n('tool')} 个工具`, `Used ${plural(n('tool'), 'tool')}`) : '']
    .filter(Boolean).join(' · ');
}
const lastOf = (xs: Item[], k: Item['k'], open = false) => { for (let i = xs.length - 1; i >= 0; i--) if (xs[i].k === k && !(open && (xs[i] as Item & { k: 'req' }).done)) return xs[i]; };
const asks = (r: Req) => r.tool === 'Ask' ? r.qs.map(q => q.q).join(' · ') : r.tool === 'Plan' ? tr('计划写好了，等你看', 'Plan ready for your review') : r.why;
// Transcript times and host-observed transitions only; an untimed sentence remains readable, never a made-up tick.
export function timeline(s: Sess, items: Item[]): Trail {
  const out: Trail = { segs: [], marks: [], activity: [], turns: [] };
  const events: { at: number; k: Trail['segs'][number]['k'] }[] = [];
  // What followed each thing you said, up to the next one.
  const blocks: Item[][] = [];
  for (const [i, it] of items.entries()) {
    if (it.k === 'you') {
      out.turns.push({ item: i, at: it.at, you: it.text || (it.files ?? []).map(f => f.name).join(tr('、', ', ')), reply: '', kind: 'none' });
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
    const said = lastOf(after, 'it'), pending = lastOf(after, 'req', true);
    // A request splits a turn's steps into groups; the line counts them all, and its time only when there is one.
    const groups = after.filter(it => it.k === 'steps'), steps = groups.flatMap(it => it.k === 'steps' ? it.steps : []);
    if (last && s.st === 'err') Object.assign(turn, { kind: 'err', reply: s.summary || tr('出错了', 'Something went wrong') });
    else if (said?.k === 'it') Object.assign(turn, { kind: 'sum', reply: said.text });
    else if (last && s.st === 'wait') Object.assign(turn, { kind: 'wait', reply: pending?.k === 'req' ? asks(pending.req) || s.summary : s.summary });
    else if (last && (s.st === 'work' || s.st === 'pack')) Object.assign(turn, { kind: 'live', reply: s.now || tr('在干活…', 'Working…') });
    else if (steps.length) Object.assign(turn, { kind: 'steps', reply: stepsLine(steps, groups.length === 1 && groups[0].k === 'steps' ? groups[0].took : undefined) });
    else turn.reply = last ? tr('还没回', 'No reply yet') : tr('没等它回，你接着又说了一句', 'You wrote again before it replied');
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

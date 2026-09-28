import type { Item, Sess, St } from '../../../electron/agents/types';

export type Turn = { item: number; at?: number; you: string; reply: string; kind: 'sum' | 'live' | 'wait' | 'err' | 'none' };
export type Trail = {
  segs: { a: number; b: number | null; k: 'work' | 'wait' | 'idle' | 'stop' }[];
  marks: { t: number; k: 'you' | 'done' | 'err' | 'her'; turn?: number }[];
  activity: number[];
  turns: Turn[];
};
const minute = (at: number) => at / 60000;
const kind = (st: St): Trail['segs'][number]['k'] => st === 'wait' ? 'wait' : st === 'err' ? 'stop' : st === 'done' ? 'idle' : 'work';
// Transcript times and host-observed transitions only; an untimed sentence remains readable, never a made-up tick.
export function timeline(s: Sess, items: Item[]): Trail {
  const out: Trail = { segs: [], marks: [], activity: [], turns: [] };
  const events: { at: number; k: Trail['segs'][number]['k'] }[] = [];
  let turn: Turn | undefined;
  for (const [i, it] of items.entries()) {
    if (it.k === 'you') {
      turn = { item: i, at: it.at, you: it.text || (it.files ?? []).join('、'), reply: '还没有回复', kind: 'none' };
      out.turns.push(turn);
      if (it.at !== undefined) {
        out.marks.push({ t: minute(it.at), k: 'you', turn: out.turns.length - 1 });
        events.push({ at: it.at, k: 'work' });
      }
    } else if (it.k === 'it') {
      if (turn) { turn.reply = it.text; turn.kind = 'sum'; }
      if (it.at !== undefined) { events.push({ at: it.at, k: 'idle' }); out.marks.push({ t: minute(it.at), k: 'done' }); }
    } else if (it.k === 'steps') {
      for (const step of it.steps) if (step.at !== undefined) out.activity.push(minute(step.at));
    } else if (it.k === 'req' && it.at !== undefined) {
      events.push({ at: it.at, k: 'wait' });
      if (it.ended !== undefined) events.push({ at: it.ended, k: 'work' });
    }
  }
  // Observed transitions win when the agent records a last text before it actually finishes.
  for (const event of s.trace ?? []) {
    events.push({ at: event.at, k: kind(event.st) });
    if (event.st === 'err') out.marks.push({ t: minute(event.at), k: 'err' });
  }
  if (turn && s.st !== 'done') {
    turn.kind = s.st === 'err' ? 'err' : s.st === 'wait' ? 'wait' : 'live';
    turn.reply = s.now || s.summary || (s.st === 'wait' ? '等你拍板' : '在干活');
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

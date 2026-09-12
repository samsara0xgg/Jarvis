// UI state. Without a runtime port this module is the entire simulated backend; with one, runtime.ts drives it.
export type Phase = 'listening' | 'hearing' | 'processing' | 'speaking' | 'error';
export type Mode = 'voice' | 'text' | 'idle';
export interface Result { id: string; kind: 'result' | 'question' | 'failure' | 'reminder'; title: string; summary: string; body: string; read: boolean }
export const examples: Result[] = [
  { id: 'result', kind: 'result', title: '周末徒步路线', summary: '三条路线的比较已备好。', body: '演示结果 · 未进行真实查询\n\nLynn Loop：林间环线，适合轻松走走。\nQuarry Rock：海湾视野，可作为另一种选择。\nPacific Spirit：城市内的森林步道。\n\n这些是用来检查长文字和结果呈现的示例，不代表当天开放状况或实时路线建议。', read: false },
  { id: 'question', kind: 'question', title: '需要你补充时间', summary: '“明天提醒我”具体是几点？', body: '演示待回应事项\n\n这条示例展示缺少必要信息时的交互。点击“回复”可回到文字胶囊，原型不会创建真实提醒。', read: false },
  { id: 'failure', kind: 'failure', title: '示例任务未完成', summary: '执行器暂时无法连接。', body: '演示失败\n\n原型没有连接执行器。重试只播放本地状态变化，不会提交或执行任何任务。', read: false },
  { id: 'reminder', kind: 'reminder', title: '你设定的提醒', summary: '起来走一走，休息一下。', body: '演示提醒\n\n这是预置示例，没有创建定时任务。只有用户明确设置的提醒才进入此类通知。', read: false },
];
export interface State { mode: Mode; phase: Phase; micMuted: boolean; soundMuted: boolean; inbox: boolean; detail: string | null; results: Result[]; reply: string; draft: string; attachment: boolean; turnId: string | null; responseId: string | null }
export const initialState: State = { mode: 'voice', phase: 'listening', micMuted: false, soundMuted: false, inbox: false, detail: null, results: [examples[0]], reply: '', draft: '', attachment: false, turnId: null, responseId: null };
export type Action = { type: 'mode'; mode: Mode } | { type: 'phase'; phase: Phase } | { type: 'mic' | 'sound' | 'inbox' | 'interrupt' | 'end' | 'attachment' | 'reset' } | { type: 'draft'; value: string } | { type: 'send' } | { type: 'answer' } | { type: 'detail'; id: string | null } | { type: 'dismiss'; id: string } | { type: 'example'; id: string }
  | { type: 'open'; turnId: string; responseId: string | null } | { type: 'append'; token: string } | { type: 'settle'; turnId: string };
export function reducer(s: State, a: Action): State {
  switch (a.type) {
    case 'reset': return { ...initialState, results: examples.slice(0, 1) };
    case 'mode': return { ...s, mode: a.mode };
    case 'phase': return { ...s, phase: a.phase, reply: a.phase === 'error' ? '' : s.reply };
    case 'mic': return { ...s, micMuted: !s.micMuted };
    case 'sound': return { ...s, soundMuted: !s.soundMuted };
    case 'interrupt': return { ...s, phase: 'listening' };
    case 'end': return { ...s, mode: 'idle', phase: 'listening', reply: '', inbox: false, detail: null };
    case 'inbox': return { ...s, inbox: !s.inbox, detail: null };
    case 'draft': return { ...s, draft: a.value };
    case 'attachment': return { ...s, attachment: !s.attachment };
    case 'send': return s.draft.trim() && s.phase !== 'processing' ? { ...s, draft: '', attachment: false, phase: 'processing', reply: '' } : s;
    case 'answer': return { ...s, phase: 'speaking', reply: '演示回复：我接住了这段表达。正式连接后，可以从这里继续交流、保存和找回上下文。此处没有保存或执行真实任务。' };
    case 'open': return { ...s, phase: 'processing', reply: '', turnId: a.turnId, responseId: a.responseId };
    case 'append': return { ...s, reply: s.reply + a.token, phase: 'speaking' };
    // The daemon's `done` carries fadeMs; runtime.ts turns it into this delayed settle for the same turn only.
    case 'settle': return s.turnId === a.turnId ? { ...s, reply: '', phase: s.phase === 'speaking' ? 'listening' : s.phase } : s;
    case 'detail': return { ...s, detail: a.id, results: s.results.map(r => r.id === a.id ? { ...r, read: true } : r) };
    case 'dismiss': return { ...s, detail: null, results: s.results.filter(r => r.id !== a.id) };
    case 'example': { const r = examples.find(r => r.id === a.id); return r ? { ...s, results: [...s.results.filter(i => i.id !== r.id), { ...r, read: false }] } : s; }
  }
}

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
// GPT-Live phase A. The daemon owns the session; every `live` op or controls answer replaces this whole record.
export type LiveState = 'idle' | 'connecting' | 'active' | 'closing' | 'unavailable';
export interface Live { state: LiveState; sessionId: string | null; since: number | null; usageS: number | null; usageFinal: boolean; reason: string | null; hushed: boolean; speaking: boolean; hearing: boolean; error: string | null; notice: string | null }
export interface Subtitle { role: 'user' | 'assistant'; text: string; startMs: number; endMs: number }
// A same-speaker pause longer than this starts a new caption row (docs/gpt-live/live-conversations.md, Display captions): an assistant resuming after an interruption must not extend the cut-off line. Application choice; tune against recordings.
const SUBTITLE_GAP_MS = 1500;
export const idleLive: Live = { state: 'idle', sessionId: null, since: null, usageS: null, usageFinal: false, reason: null, hushed: false, speaking: false, hearing: false, error: null, notice: null };
export interface State { mode: Mode; phase: Phase; micMuted: boolean; soundMuted: boolean; inbox: boolean; detail: string | null; results: Result[]; reply: string; draft: string; attachment: boolean; turnId: string | null; responseId: string | null; live: Live; subtitles: Subtitle[] }
export const initialState: State = { mode: 'voice', phase: 'listening', micMuted: false, soundMuted: false, inbox: false, detail: null, results: [examples[0], examples[3]], reply: '', draft: '', attachment: false, turnId: null, responseId: null, live: idleLive, subtitles: [] };
export type Action = { type: 'mode'; mode: Mode } | { type: 'phase'; phase: Phase } | { type: 'mic' | 'sound' | 'inbox' | 'interrupt' | 'end' | 'attachment' | 'reset' } | { type: 'draft'; value: string } | { type: 'send' } | { type: 'answer' } | { type: 'detail'; id: string | null } | { type: 'dismiss'; id: string } | { type: 'example'; id: string }
  | { type: 'open'; turnId: string; responseId: string | null } | { type: 'append'; token: string } | { type: 'settle'; turnId: string } | { type: 'controls'; micMuted: boolean; soundMuted: boolean }
  | { type: 'live'; live: Live } | { type: 'subtitle'; sessionId: string; role: 'user' | 'assistant'; delta: string; startMs: number; endMs: number } | { type: 'live_dismiss' };
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
    case 'controls': return { ...s, micMuted: a.micMuted, soundMuted: a.soundMuted };
    // A new session id starts a fresh transcript; a closed session keeps its lines on screen until dismissed.
    case 'live': return { ...s, live: a.live, subtitles: a.live.sessionId && a.live.sessionId !== s.live.sessionId ? [] : s.subtitles };
    case 'subtitle': {
      if (a.sessionId !== s.live.sessionId) return s; // late delta from an earlier session
      // Merge into the latest row of the SAME speaker (other speaker's rows may sit in between: full duplex overlaps and user fragments arrive late) when this fragment starts within SUBTITLE_GAP_MS of that row's end; the wire has no turn boundaries (transcripts carry start_ms/end_ms, not turns).
      const i = s.subtitles.map(t => t.role).lastIndexOf(a.role);
      const row = i >= 0 ? s.subtitles[i] : undefined;
      const merged = row && a.startMs - row.endMs <= SUBTITLE_GAP_MS ? s.subtitles.map((t, j) => j === i ? { ...t, text: t.text + a.delta, endMs: Math.max(t.endMs, a.endMs) } : t) : [...s.subtitles, { role: a.role, text: a.delta, startMs: a.startMs, endMs: a.endMs }];
      return { ...s, subtitles: merged.slice(-40) };
    }
    case 'live_dismiss': return { ...s, subtitles: [], live: { ...s.live, reason: null, error: null, notice: null, usageS: null, usageFinal: false } };
    case 'detail': return { ...s, detail: a.id, results: s.results.map(r => r.id === a.id ? { ...r, read: true } : r) };
    case 'dismiss': return { ...s, detail: null, results: s.results.filter(r => r.id !== a.id) };
    case 'example': { const r = examples.find(r => r.id === a.id); return r ? { ...s, results: [...s.results.filter(i => i.id !== r.id), { ...r, read: false }] } : s; }
  }
}

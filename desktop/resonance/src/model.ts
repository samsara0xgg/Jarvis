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
export interface Live { state: LiveState; sessionId: string | null; since: number | null; usageS: number | null; usageFinal: boolean; reason: string | null; speaking: boolean; hearing: boolean; error: string | null; notice: string | null }
export interface Subtitle { role: 'user' | 'assistant'; text: string; startMs: number; endMs: number }
// One memory.db record: the conversation of record, the same rows the backend's history is built from. `seq` is the poll cursor.
export interface Row { seq: number; id: string; ts: string; source: string; text: string }
// A same-speaker pause longer than this starts a new caption row (https://developers.openai.com/api/docs/guides/live-conversations, Display captions): an assistant resuming after an interruption must not extend the cut-off line. Application choice; tune against recordings.
const SUBTITLE_GAP_MS = 1500;
export const idleLive: Live = { state: 'idle', sessionId: null, since: null, usageS: null, usageFinal: false, reason: null, speaking: false, hearing: false, error: null, notice: null };
// `conversation` is the daemon's wave mode (ADR 0041), from every controls answer; `heard` is the last accepted transcript.
// `askedAt`: when this surface's turn went in, until its answer opens; `thoughtS`: how long that answer took to come (ADR 0064).
export interface State { mode: Mode; phase: Phase; micMuted: boolean; soundMuted: boolean; conversation: boolean; heard: string; inbox: boolean; detail: string | null; results: Result[]; reply: string; draft: string; attachment: boolean; turnId: string | null; responseId: string | null; failed: boolean; waiting: string | null; askedAt: number | null; thoughtS: number; faded: boolean; played: boolean; inFlight: boolean; live: Live; subtitles: Subtitle[]; rows: Row[]; openSeq: number }
export const initialState: State = { mode: 'voice', phase: 'listening', micMuted: false, soundMuted: false, conversation: false, heard: '', inbox: false, detail: null, results: [examples[0], examples[3], examples[1]], reply: '', draft: '', attachment: false, turnId: null, responseId: null, failed: false, waiting: null, askedAt: null, thoughtS: 0, faded: false, played: false, inFlight: false, live: idleLive, subtitles: [], rows: [], openSeq: 0 };
export type Action = { type: 'mode'; mode: Mode } | { type: 'phase'; phase: Phase } | { type: 'mic' | 'sound' | 'inbox' | 'interrupt' | 'end' | 'attachment' | 'reset' } | { type: 'draft'; value: string } | { type: 'send' } | { type: 'answer' } | { type: 'detail'; id: string | null } | { type: 'dismiss'; id: string } | { type: 'example'; id: string }
  | { type: 'open'; turnId: string; responseId: string | null; at: number } | { type: 'append'; token: string } | { type: 'settle'; turnId: string } | { type: 'pending'; turnId: string; at: number } | { type: 'failed'; turnId: string; cancelled: boolean; message: string | null } | { type: 'controls'; micMuted: boolean; soundMuted: boolean; conversation: boolean } | { type: 'heard'; text: string } | { type: 'spoken'; turnId: string }
  | { type: 'live'; live: Live } | { type: 'subtitle'; sessionId: string; role: 'user' | 'assistant'; delta: string; startMs: number; endMs: number } | { type: 'live_dismiss' }
  | { type: 'rows'; rows: Row[] } | { type: 'older'; rows: Row[] };
export function reducer(s: State, a: Action): State {
  switch (a.type) {
    case 'reset': return { ...initialState, results: examples.slice(0, 1) };
    case 'mode': return { ...s, mode: a.mode };
    // `inFlight`: Allen's words are coming in, from speech onset until they are accepted or come to nothing.
    case 'phase': return { ...s, phase: a.phase, reply: a.phase === 'error' ? '' : s.reply, heard: a.phase === 'hearing' ? '' : s.heard, askedAt: a.phase === 'error' ? null : s.askedAt,
      inFlight: a.phase === 'hearing' || (s.inFlight && a.phase === 'processing') };
    case 'mic': return { ...s, micMuted: !s.micMuted };
    case 'sound': return { ...s, soundMuted: !s.soundMuted };
    case 'interrupt': return { ...s, phase: 'listening' };
    case 'end': return { ...s, mode: 'idle', phase: 'listening', reply: '', inbox: false, detail: null };
    case 'inbox': return { ...s, inbox: !s.inbox, detail: null };
    case 'draft': return { ...s, draft: a.value };
    case 'attachment': return { ...s, attachment: !s.attachment };
    case 'send': return s.draft.trim() && s.phase !== 'processing' ? { ...s, draft: '', attachment: false, phase: 'processing', reply: '', waiting: null } : s;
    case 'answer': return { ...s, phase: 'speaking', reply: '演示回复：我接住了这段表达。正式连接后，可以从这里继续交流、保存和找回上下文。此处没有保存或执行真实任务。' };
    // `openSeq` remembers where the log stood when this turn opened: the streaming reply shows as a tail row until an answer row lands past it.
    // The daemon opens an answer once it is whole (ADR 0064), so the wait from `askedAt` is how long it took.
    case 'open': { const mine = a.turnId === s.waiting && s.askedAt !== null;
      return { ...s, phase: 'processing', reply: '', turnId: a.turnId, responseId: a.responseId, failed: false, faded: false, played: false, openSeq: s.rows.length ? s.rows[s.rows.length - 1].seq : 0,
        thoughtS: mine ? (a.at - s.askedAt!) / 1000 : 0, askedAt: mine ? null : s.askedAt }; }
    case 'append': return { ...s, reply: s.reply + a.token, phase: 'speaking' };
    // An answer leaves once its fade is over (`done` + fadeMs, which runtime.ts turns into a delayed settle) and she has
    // stopped saying it (`spoken`, also when cut off), whichever comes last. Until `spoken` she is still speaking.
    case 'settle': return s.turnId !== a.turnId ? s : s.played ? { ...s, reply: '' } : { ...s, faded: true };
    case 'spoken': return s.turnId !== a.turnId ? s : { ...s, played: true, reply: s.faded ? '' : s.reply, phase: s.phase === 'speaking' || s.phase === 'processing' ? 'listening' : s.phase };
    // `waiting` is the turn this surface started (submit answer or voice `accepted`); only its end without an answer
    // (daemon `failed` / `cancelled`) releases "processing", so a background turn failing meanwhile changes nothing.
    // A failure says why in the daemon's words (a bad key, no credit, no network…) in place of the answer.
    case 'pending': return { ...s, waiting: a.turnId, askedAt: a.at };
    // Nothing is spoken for it, so it leaves at its settle (runtime.ts) like an answer whose speech is over.
    case 'failed': { if (a.turnId !== s.waiting) return s;
      const t = { ...s, askedAt: null };
      return t.phase === 'processing' ? { ...t, phase: 'listening', reply: a.cancelled ? '' : a.message ?? '这一轮出错了，没有完成。可以再说一次。', turnId: a.turnId, responseId: null, failed: !a.cancelled, faded: false, played: true,
        openSeq: t.rows.length ? t.rows[t.rows.length - 1].seq : 0 } : t; }
    case 'controls': return { ...s, micMuted: a.micMuted, soundMuted: a.soundMuted, conversation: a.conversation };
    case 'heard': return { ...s, heard: a.text, inFlight: false };
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
    case 'rows': {
      const last = s.rows.length ? s.rows[s.rows.length - 1].seq : 0;
      const fresh = a.rows.filter(row => row.seq > last);
      return fresh.length ? { ...s, rows: [...s.rows, ...fresh].slice(-400) } : s; // ponytail: the newest 400 held (the main app's transcript shows every row); the companion fetches older days again through 'older'
    }
    case 'older': { const first = s.rows[0]?.seq ?? Infinity, old = a.rows.filter(row => row.seq < first); return old.length ? { ...s, rows: [...old, ...s.rows] } : s; }
    case 'detail': return { ...s, detail: a.id, results: s.results.map(r => r.id === a.id ? { ...r, read: true } : r) };
    case 'dismiss': return { ...s, detail: null, results: s.results.filter(r => r.id !== a.id) };
    case 'example': { const r = examples.find(r => r.id === a.id); return r ? { ...s, results: [...s.results.filter(i => i.id !== r.id), { ...r, read: false }] } : s; }
  }
}
// The render layer wraps speech in <voice> and card text in <document> (voice_tts.py:99). A document is the whole answer and the voice only its spoken form (ADR 0040), so once one arrives show it alone; drop the markup and any half-streamed tag.
export const visible = (reply: string) => reply.slice(Math.max(0, reply.indexOf('<document>'))).replace(/<\/voice>/g, '\n').replace(/<\/?(voice|document)>/g, '').replace(/<\/?[a-z]*$/, '').trim();
// Plain words for her bubble and the Dashboard: the answer's markdown emphasis and code ticks dropped.
export const plain = (text: string) => visible(text).replace(/\*\*|`/g, '');

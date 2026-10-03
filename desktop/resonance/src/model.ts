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
// One line of what is on screen under her: yours as you said it, hers as the daemon wrote it (its <voice>/<document> tags kept).
// `at`: when it landed (hers: when she began saying it). `said`: she has finished saying it; `cutAt`: she was stopped there.
// `queued`: another answer of hers is still being said, so this one has not begun; `from`: when she began saying it, if that was later than `at`.
// `mark`: where her voice was when the daemon last said (ADR 0112): `n` letters and digits played, `ahead` through the segment playing,
// as of `at`; `hold`: when she was held or stopped there, so the lit words stop with her.
export interface Mark { n: number; ahead: number; at: number; hold?: number }
// `written`: the details her spoken `text` leaves out, shown under it and not the whole answer (ADR 0114); a `<document>` in `text` is the whole answer.
// `late`: it came while Allen's next words were coming in, so it shows with them, above them.
export interface Line { id: string; who: 'you' | 'her'; text: string; turn?: string; failed?: boolean; at: number; said?: boolean; cutAt?: number; queued?: boolean; from?: number; mark?: Mark; written?: string; late?: boolean }
const MAX_LINES = 40;
// A same-speaker pause longer than this starts a new caption row (https://developers.openai.com/api/docs/guides/live-conversations, Display captions): an assistant resuming after an interruption must not extend the cut-off line. Application choice; tune against recordings.
const SUBTITLE_GAP_MS = 1500;
export const idleLive: Live = { state: 'idle', sessionId: null, since: null, usageS: null, usageFinal: false, reason: null, speaking: false, hearing: false, error: null, notice: null };
// `conversation` is the daemon's wave mode (ADR 0041), from every controls answer; `heard` is the last accepted transcript.
// `askedAt`: when this surface's turn went in, until its answer opens; `thoughtS`: how long that answer took to come (ADR 0064).
export interface State { mode: Mode; phase: Phase; micMuted: boolean; soundMuted: boolean; conversation: boolean; heard: string; partial: string; settled: string | null; tool: { turnId: string; label: string } | null; inbox: boolean; detail: string | null; results: Result[]; reply: string; draft: string; attachment: boolean; turnId: string | null; responseId: string | null; failed: boolean; waiting: string | null; askedAt: number | null; thoughtS: number; faded: boolean; played: boolean; inFlight: boolean; live: Live; subtitles: Subtitle[]; rows: Row[]; openSeq: number; talk: Line[]; talkN: number; replyAt: number; apart: string }
export const initialState: State = { mode: 'voice', phase: 'listening', micMuted: false, soundMuted: false, conversation: false, heard: '', partial: '', settled: null, tool: null, inbox: false, detail: null, results: [examples[0], examples[3], examples[1]], reply: '', draft: '', attachment: false, turnId: null, responseId: null, failed: false, waiting: null, askedAt: null, thoughtS: 0, faded: false, played: false, inFlight: false, live: idleLive, subtitles: [], rows: [], openSeq: 0, talk: [], talkN: 0, replyAt: 0, apart: '' };
export type Action = { type: 'mode'; mode: Mode } | { type: 'phase'; phase: Phase } | { type: 'mic' | 'sound' | 'inbox' | 'interrupt' | 'end' | 'attachment' | 'reset' } | { type: 'draft'; value: string } | { type: 'send' } | { type: 'answer' } | { type: 'detail'; id: string | null } | { type: 'dismiss'; id: string } | { type: 'example'; id: string }
  | { type: 'open'; turnId: string; responseId: string | null; at: number } | { type: 'append'; turnId: string; token: string; at: number } | { type: 'written'; turnId: string; text: string } | { type: 'whole'; turnId: string; text: string; at: number } | { type: 'settle'; turnId: string } | { type: 'pending'; turnId: string; at: number } | { type: 'failed'; turnId: string; cancelled: boolean; message: string | null; at: number } | { type: 'controls'; micMuted: boolean; soundMuted: boolean; conversation: boolean } | { type: 'heard'; text: string; at: number } | { type: 'partial'; text: string; settled?: string } | { type: 'spoken'; turnId: string; at: number; outcome?: string } | { type: 'playing'; turnId: string; played: number; ahead: number; held: boolean; at: number }
  | { type: 'tool'; turnId: string; label: string }
  | { type: 'live'; live: Live } | { type: 'subtitle'; sessionId: string; role: 'user' | 'assistant'; delta: string; startMs: number; endMs: number } | { type: 'live_dismiss' }
  | { type: 'rows'; rows: Row[] } | { type: 'older'; rows: Row[] }
  | { type: 'you'; text: string; at: number } | { type: 'her'; turn: string; text: string; at: number } | { type: 'said'; turn: string; at: number } | { type: 'cut'; at: number } | { type: 'talk-clear' };

// The conversation under her. Her line is keyed by its turn and rewritten as the answer grows; a turn that is dropped takes its line away.
const keep = (lines: Line[]) => lines.length > MAX_LINES ? lines.slice(-MAX_LINES) : lines;
const hers = (lines: Line[], turn: string | null, text: string, at: number, extra: Partial<Line> = {}): Line[] => {
  if (!turn || !text) return lines;
  const id = `her:${turn}`, i = lines.findIndex(l => l.id === id);
  // An answer that arrives while an earlier one is still being said waits its turn: its clock starts when that one is over.
  if (i < 0) return keep([...lines, { id, who: 'her', text, turn, at, ...(lines.some(l => l.who === 'her' && !l.said) ? { queued: true } : {}), ...extra }]);
  return lines[i].text === text && !Object.keys(extra).length ? lines : lines.map((l, j) => j === i ? { ...l, text, ...extra } : l);
};
const yours = (s: State, text: string, at: number): Pick<State, 'talk' | 'talkN'> => ({ talk: keep([...s.talk, { id: `you:${s.talkN}`, who: 'you', text, at }]), talkN: s.talkN + 1 });
// Her words on screen stay as they were while yours are coming in; once they are in, what was written meanwhile shows (until it is dropped).
const unheld = (s: State, reply: string, late = false): Line[] => s.turnId && !s.played ? hers(s.talk, s.turnId, reply, s.replyAt, { ...(s.apart ? { written: s.apart } : {}), ...(late && !s.talk.some(l => l.id === `her:${s.turnId}`) ? { late } : {}) }) : s.talk;
// She has finished saying a turn's line (`cutAt`: she was stopped, or it was never said); the next answer that was waiting begins.
const finished = (lines: Line[], turn: string, at: number, cutAt?: number): Line[] => {
  if (!lines.some(l => l.id === `her:${turn}` && !l.said)) return lines;
  const out = lines.map(l => l.id === `her:${turn}` ? { ...l, said: true, queued: false, ...(cutAt === undefined ? {} : { cutAt: l.cutAt ?? cutAt }) } : l);
  const next = out.findIndex(l => l.who === 'her' && !l.said && l.queued);
  return next >= 0 && !out.some(l => l.who === 'her' && !l.said && !l.queued) ? out.map((l, j) => j === next ? { ...l, queued: false, from: at } : l) : out;
};
// A line she was still saying when something else began (your words, a stop, a lost link) stops where it was.
const ended = (lines: Line[], at: number): Line[] => lines.some(l => l.who === 'her' && !l.said) ? lines.map(l => l.who === 'her' && !l.said ? { ...l, said: true, queued: false, cutAt: l.cutAt ?? (l.queued ? l.at : at) } : l) : lines;
// Where her voice is, from the daemon (ADR 0112). The same place again only holds or lets go the clock of the lit words:
// held it stops, let go it goes on from there.
const marked = (lines: Line[], a: { turnId: string; played: number; ahead: number; held: boolean; at: number }): Line[] => {
  const l = lines.find(l => l.id === `her:${a.turnId}`), m = l?.mark;
  if (!l || l.said || m && m.n === a.played && m.ahead === a.ahead && !m.hold === !a.held) return lines;
  const mark: Mark = m && m.n === a.played && m.ahead === a.ahead ? a.held ? { ...m, hold: a.at } : { n: m.n, ahead: m.ahead, at: m.at + a.at - m.hold! } : { n: a.played, ahead: a.ahead, at: a.at, ...(a.held ? { hold: a.at } : {}) };
  return lines.map(x => x === l ? { ...x, mark } : x);
};
export function reducer(s: State, a: Action): State {
  switch (a.type) {
    case 'reset': return { ...initialState, results: examples.slice(0, 1) };
    case 'mode': return { ...s, mode: a.mode };
    // `inFlight`: Allen's words are coming in, from speech onset until they are accepted or come to nothing.
    // A lost link also ends the answer on screen: no `spoken` will come for it.
    case 'phase': { const inFlight = a.phase === 'hearing' || (s.inFlight && a.phase === 'processing');
      const talk = a.phase === 'error' ? ended(s.talk, s.replyAt) : s.inFlight && !inFlight ? unheld(s, s.reply) : s.talk;
      return { ...s, phase: a.phase, reply: a.phase === 'error' ? '' : s.reply, heard: a.phase === 'hearing' ? '' : s.heard, partial: a.phase === 'processing' && s.inFlight ? s.partial : '', askedAt: a.phase === 'error' ? null : s.askedAt,
        played: s.played || a.phase === 'error', inFlight, talk, tool: a.phase === 'error' ? null : s.tool }; }
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
      return { ...s, tool: s.tool?.turnId === a.turnId ? null : s.tool, phase: 'processing', reply: '', replyAt: 0, apart: '', turnId: a.turnId, responseId: a.responseId, failed: false, faded: false, played: false, openSeq: s.rows.length ? s.rows[s.rows.length - 1].seq : 0,
        thoughtS: mine ? (a.at - s.askedAt!) / 1000 : 0, askedAt: mine ? null : s.askedAt }; }
    // A chunk belongs to the turn it carries: a late one of an earlier turn (a slow tool turn's answer, ADR 0107) grows that turn's own line, never the newest turn's reply.
    case 'append': { if (a.turnId && a.turnId !== s.turnId) { const old = s.talk.find(l => l.id === `her:${a.turnId}`); return old ? { ...s, talk: hers(s.talk, a.turnId, old.text + a.token, old.at) } : s; }
      const reply = s.reply + a.token, replyAt = s.replyAt || a.at;
      return { ...s, reply, replyAt, phase: 'speaking', talk: s.inFlight || !s.turnId ? s.talk : hers(s.talk, s.turnId, reply, replyAt) }; }
    // The written part that goes with the spoken line, in `done` (ADR 0114). Her line may still be held back for your words coming in: `apart` waits for it.
    case 'written': return a.turnId !== s.turnId ? s : { ...s, apart: a.text, talk: s.talk.map(l => l.id === `her:${a.turnId}` ? { ...l, written: a.text } : l) };
    // The whole spoken answer, in `done`: the streamed chunks can stop at its first sentence while the voice says all of it. A turn's own line only grows to it.
    case 'whole': { if (a.turnId !== s.turnId) { const old = s.talk.find(l => l.id === `her:${a.turnId}`); return old && a.text.length > old.text.length ? { ...s, talk: hers(s.talk, a.turnId, a.text, old.at) } : s; }
      if (a.text.length <= s.reply.length) return s;
      const replyAt = s.replyAt || a.at;
      return { ...s, reply: a.text, replyAt, talk: s.inFlight || !s.turnId ? s.talk : hers(s.talk, s.turnId, a.text, replyAt) }; }
    // An answer leaves once its fade is over (`done` + fadeMs, which runtime.ts turns into a delayed settle) and she has
    // stopped saying it (`spoken`, also when cut off), whichever comes last. Until `spoken` she is still speaking.
    case 'settle': return s.turnId !== a.turnId ? s : s.played ? { ...s, reply: '' } : { ...s, faded: true };
    // A waiting turn answered where no words show (a silent channel) ends its wait here too.
    case 'spoken': { const t = a.turnId === s.waiting ? { ...s, askedAt: null } : s;
      // An answer stopped or dropped before the end was not all said: where she got to stays lit (nothing, if she never began).
      const cut = a.outcome === 'interrupted' || a.outcome === 'failed' ? a.at : a.outcome === 'dropped' ? 0 : undefined;
      const talk = finished(s.talk, a.turnId, a.at, cut);
      return s.turnId !== a.turnId ? { ...t, talk } : { ...t, talk, played: true, responseId: null, reply: s.faded ? '' : s.reply, phase: s.phase === 'speaking' || s.phase === 'processing' ? 'listening' : s.phase }; }
    // `waiting` is the turn this surface started (submit, card or voice `accepted`), thought about while `askedAt` is
    // set. Only its end without an answer (daemon `failed` / `cancelled`) shows, whatever else is going on, so a
    // background turn failing meanwhile changes nothing. A failure says why in the daemon's words (a bad key, no
    // credit, no network…) in place of the answer. A typed turn's id comes back on the HTTP answer, which a quick
    // answer's `open` can beat on the socket: then it has nothing left to wait for.
    case 'pending': return { ...s, waiting: a.turnId, askedAt: a.turnId === s.turnId ? null : a.at };
    // Nothing is spoken for it, so it leaves at its settle (runtime.ts) like an answer whose speech is over. A cancelled
    // answer on screen goes whoever asked for it: it was stopped, or dropped for the words after it (ADR 0074).
    case 'failed': { const shown = a.turnId === s.turnId, gone = { reply: '', played: true, responseId: null }, dropped = s.talk.filter(l => l.id !== `her:${a.turnId}`);
      if (a.turnId !== s.waiting) return shown && a.cancelled ? { ...s, ...gone, talk: dropped } : s;
      const t = { ...s, askedAt: null, tool: null, phase: s.phase === 'processing' ? 'listening' as const : s.phase };
      if (a.cancelled) return shown ? { ...t, ...gone, talk: dropped } : t;
      const reply = a.message ?? '这一轮出错了，没有完成。可以再说一次。';
      return { ...t, reply, turnId: a.turnId, responseId: null, failed: true, faded: false, played: true, talk: hers(ended(s.talk, a.at), a.turnId, reply, a.at, { failed: true, said: true }),
        openSeq: t.rows.length ? t.rows[t.rows.length - 1].seq : 0 }; }
    // The daemon leaving conversation mode (idle, the end button) ends the words still coming in: no `accepted` or `empty` is owed for them.
    case 'controls': { const next = { ...s, micMuted: a.micMuted, soundMuted: a.soundMuted, conversation: a.conversation };
      return !a.conversation && s.inFlight ? reducer(next, { type: 'phase', phase: 'listening' }) : next; }
    // What has been heard so far of the words still coming in (ADR 0111); one that arrives after they were accepted is late.
    // The tool this turn is waiting on, as the daemon's fixed line. Until the turn's answer opens, a tool's line stays until the next one takes
    // its place (the daemon's clearing between two tools is not shown); once it has opened, an empty label clears it. It also goes when the
    // answer opens (above), or when the turn ends without one.
    case 'tool': return { ...s, tool: a.label ? { turnId: a.turnId, label: a.label } : s.tool?.turnId === a.turnId && s.turnId !== a.turnId ? s.tool : null };
    case 'partial': return s.inFlight ? { ...s, partial: a.text, settled: a.settled ?? null } : s;
    // What was written while your words came in answers the words before them: it goes above yours, and shows with them (talk.ts shownOf).
    case 'heard': { const before = a.text.trim() ? ended(s.talk, a.at) : s.talk, talk = s.inFlight ? unheld({ ...s, talk: before }, s.reply, !!a.text.trim()) : before;
      return { ...s, heard: a.text, partial: '', inFlight: false, ...(a.text.trim() ? yours({ ...s, talk }, a.text, a.at) : { talk }) }; }
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
    // The conversation under her, written by the surface itself: typed words and the demo's answers.
    case 'you': return { ...s, ...yours(s, a.text, a.at) };
    case 'her': return { ...s, talk: hers(s.talk, a.turn, a.text, a.at) };
    case 'said': return { ...s, talk: finished(s.talk, a.turn, a.at) };
    case 'playing': { const talk = marked(s.talk, a); return talk === s.talk ? s : { ...s, talk }; }
    case 'cut': return { ...s, talk: ended(s.talk, a.at) };
    case 'talk-clear': return s.talk.length ? { ...s, talk: [] } : s;
    case 'detail': return { ...s, detail: a.id, results: s.results.map(r => r.id === a.id ? { ...r, read: true } : r) };
    case 'dismiss': return { ...s, detail: null, results: s.results.filter(r => r.id !== a.id) };
    case 'example': { const r = examples.find(r => r.id === a.id); return r ? { ...s, results: [...s.results.filter(i => i.id !== r.id), { ...r, read: false }] } : s; }
  }
}
// The render layer wraps speech in <voice> and card text in <document> (voice_tts.py:99). A document is the whole answer and the voice only its spoken form (ADR 0040), so once one arrives show it alone; drop the markup and any half-streamed tag.
export const visible = (reply: string) => reply.slice(Math.max(0, reply.indexOf('<document>'))).replace(/<\/voice>/g, '\n').replace(/<\/?(voice|document)>/g, '').replace(/<\/?[a-z]*$/, '').trim();
// Plain words for her bubble and the Dashboard: the answer's markdown emphasis and code ticks dropped.
export const plain = (text: string) => visible(text).replace(/\*\*|`/g, '');
// The line of the tool this surface's turn is waiting on ("Searching the web..."), or ''. What her voice is doing does not decide it (a wait line
// being said, an earlier answer still going): every place that shows it reads this one rule.
export const toolLine = (s: State): string => s.tool && s.tool.turnId === s.waiting ? s.tool.label : '';

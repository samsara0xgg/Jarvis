// Live link to the Jarvis daemon over the Inherent v1 wire (ADR-0003/0005):
// outbound-only WebSocket envelopes `{op, payload}` in, HTTP POSTs out. Audio never crosses this link; the daemon owns mic and speaker.
import type { Action, Live, LiveState, Row } from './model';
import type { Card, Question } from './ActionCard';
import type { NightAction, NightState } from './NightCard';

export interface Controls { mic_muted?: boolean; speech_muted?: boolean; conversation?: boolean; live?: 'start' | 'stop' }
const liveStates: LiveState[] = ['idle', 'connecting', 'active', 'closing', 'unavailable'];
// `LiveVoice.status()` as the daemon sends it, on the `live` op and inside every controls answer.
const liveFrom = (p: Record<string, unknown>): Live => ({
  state: liveStates.find(v => v === p.state) ?? 'idle',
  sessionId: typeof p.session_id === 'string' ? p.session_id : null,
  since: p.state === 'active' || p.state === 'closing' ? Date.now() - Number(p.elapsed_s ?? 0) * 1000 : null,
  usageS: typeof p.usage_s === 'number' ? p.usage_s : null,
  usageFinal: p.usage_final === true,
  reason: typeof p.reason === 'string' ? p.reason : null,
  speaking: p.speaking === true,
  hearing: p.hearing === true,
  error: typeof p.error === 'string' ? p.error : null,
  notice: typeof p.notice === 'string' ? p.notice : null,
});
export interface Runtime { submit: (text: string) => Promise<void>; cancel: (responseId: string | null) => Promise<void>; stopTurn: (turnId: string) => Promise<void>; controls: (patch: Controls) => Promise<void>; conversation: (after: number, limit?: number) => Promise<Row[]>; card: () => Promise<Card | null>; decide: (id: string, decision: 'accept' | 'reject', edits?: Record<string, string>) => Promise<void>; question: () => Promise<Question | null>; answer: (id: string, answers: Record<string, string> | null) => Promise<void>; night: () => Promise<NightState>; nightAct: (action: NightAction, hours?: number) => Promise<NightState>; reconnect: () => void; close: () => void }

// Daemon `voice` phases → UI phases. Anything unlisted leaves the phase alone.
const voicePhase: Record<string, Action> = {
  listening: { type: 'phase', phase: 'hearing' },
  transcribing: { type: 'phase', phase: 'processing' },
  accepted: { type: 'phase', phase: 'processing' },
  empty: { type: 'phase', phase: 'listening' },
  error: { type: 'phase', phase: 'listening' },
};

export function connect(port: string, dispatch: (a: Action) => void): Runtime {
  const http = `http://127.0.0.1:${port}`;
  const post = async (path: string, body: unknown): Promise<Record<string, unknown>> => {
    const r = await fetch(`${http}${path}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`${path} ${r.status}`);
    return r.json();
  };
  // The daemon owns mute state; every answer (including the `{}` sync on connect) is authoritative.
  // Live state has exactly one writer, the `live` op below: the daemon pushes one on
  // registration and one per transition, all ordered on this socket. The controls answer
  // carries `live` too, but it is computed before it travels, so dispatching it here would
  // let a slow HTTP response overwrite a newer push.
  const controls = async (patch: Controls) => {
    const c = await post('/inherent/controls', patch);
    dispatch({ type: 'controls', micMuted: c.mic_muted === true, soundMuted: c.speech_muted === true, conversation: c.conversation === true });
  };
  let ws: WebSocket | null = null;
  let attempt = 0;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let closed = false;
  const open = () => {
    if (closed) return;
    if (retry) { clearTimeout(retry); retry = null; }
    ws = new WebSocket(`ws://127.0.0.1:${port}/inherent/ws`);
    ws.onopen = () => { attempt = 0; dispatch({ type: 'phase', phase: 'listening' }); controls({}).catch(() => undefined); };
    ws.onmessage = e => {
      let msg: { op?: string; payload?: Record<string, unknown> };
      try { msg = JSON.parse(String(e.data)); } catch { return; }
      const p = msg.payload ?? {};
      const turnId = String(p.turn_id ?? '');
      if (msg.op === 'open') dispatch({ type: 'open', turnId, responseId: typeof p.response_id === 'string' ? p.response_id : null, at: Date.now() });
      else if (msg.op === 'append') dispatch({ type: 'append', token: String(p.token ?? ''), at: Date.now() });
      else if (msg.op === 'done') setTimeout(() => dispatch({ type: 'settle', turnId }), Number(p.fadeMs ?? 5000));
      else if (msg.op === 'failed' || msg.op === 'cancelled') {
        dispatch({ type: 'failed', turnId, cancelled: msg.op === 'cancelled', message: typeof p.message === 'string' ? p.message : null, at: Date.now() });
        if (msg.op === 'failed') setTimeout(() => dispatch({ type: 'settle', turnId }), 8000); // long enough to read why
      }
      else if (msg.op === 'voice') { const a = voicePhase[String(p.phase)]; if (a) dispatch(a); if (p.phase === 'spoken') dispatch({ type: 'spoken', turnId }); if (p.phase === 'accepted' && turnId) dispatch({ type: 'pending', turnId, at: Date.now() }); if (p.phase === 'accepted' && typeof p.text === 'string') dispatch({ type: 'heard', text: p.text, at: Date.now() }); }
      else if (msg.op === 'live') dispatch({ type: 'live', live: liveFrom(p) });
      // ADR 0102: the wake word, a dismissal or quiet flips conversation mode from the daemon's side.
      else if (msg.op === 'controls') dispatch({ type: 'controls', micMuted: p.mic_muted === true, soundMuted: p.speech_muted === true, conversation: p.conversation === true });
      else if (msg.op === 'subtitle') dispatch({ type: 'subtitle', sessionId: String(p.session_id ?? ''), role: p.role === 'user' ? 'user' : 'assistant', delta: String(p.delta ?? ''), startMs: Number(p.start_ms ?? 0), endMs: Number(p.end_ms ?? 0) });
    };
    ws.onclose = () => {
      ws = null;
      if (closed) return;
      dispatch({ type: 'phase', phase: 'error' });
      retry = setTimeout(open, Math.min(16000, 1000 * 2 ** attempt++)); // same 1/2/4/8/16 s ladder as the Swift card
    };
  };
  open();
  return {
    // The answer names the turn this text started; it is the one whose failure releases "processing".
    submit: async text => { const r = await post('/inherent/submit', { text }); if (typeof r.turn_id === 'string' && r.turn_id) dispatch({ type: 'pending', turnId: r.turn_id, at: Date.now() }); },
    // foreground_output stops what is audible now and lets the run finish (ADR-0008 D10).
    cancel: async responseId => { if (responseId) await post('/inherent/cancel-response', { response_id: responseId, scope: 'foreground_output' }); },
    // A turn still being thought about has no answer to name yet: the daemon stops it by its turn, and it is never said.
    stopTurn: async turnId => { await post('/inherent/cancel-response', { turn_id: turnId, reason: 'user_stop' }); },
    controls,
    // Rows past `after` (0 = the newest `limit` rows, the daemon's default 200); the log is memory.db, so it survives every reload.
    conversation: async (after, limit) => { const r = await fetch(`${http}/inherent/conversation?after=${after}${limit ? `&limit=${limit}` : ''}`); if (!r.ok) throw new Error(`/inherent/conversation ${r.status}`); return ((await r.json()) as { rows: Row[] }).rows; },
    // ADR 0062: the card waiting for a button, and the button. A 409 means it is no longer the pending card.
    card: async () => { const r = await fetch(`${http}/inherent/confirmation`); if (!r.ok) throw new Error(`/inherent/confirmation ${r.status}`); return ((await r.json()) as { card: Card | null }).card; },
    decide: async (id, decision, edits = {}) => { const r = await post('/inherent/confirmation', { confirmation_id: id, decision, edits }); if (typeof r.turn_id === 'string' && r.turn_id) dispatch({ type: 'pending', turnId: r.turn_id, at: Date.now() }); },
    // ADR 0066: the ask card waiting to be filled in, and its answers (null = the ×). A 409 means it is no longer waiting.
    question: async () => { const r = await fetch(`${http}/inherent/clarification`); if (!r.ok) throw new Error(`/inherent/clarification ${r.status}`); return ((await r.json()) as { card: Question | null }).card; },
    answer: async (id, answers) => { const r = await post('/inherent/clarification', answers ? { clarification_id: id, answers } : { clarification_id: id, dismiss: true }); if (typeof r.turn_id === 'string' && r.turn_id) dispatch({ type: 'pending', turnId: r.turn_id, at: Date.now() }); },
    // ADR 0093: the night run as the daemon holds it, and the bedtime and night cards' buttons (the answer is the new state).
    night: async () => { const r = await fetch(`${http}/inherent/night`); if (!r.ok) throw new Error(`/inherent/night ${r.status}`); return await r.json() as NightState; },
    nightAct: async (action, hours) => await post('/inherent/night', hours === undefined ? { action } : { action, hours }) as unknown as NightState,
    reconnect: () => { if (ws) ws.close(); else open(); },
    close: () => { closed = true; if (retry) clearTimeout(retry); ws?.close(); },
  };
}

// Live link to the Jarvis daemon over the Inherent v1 wire (ADR-0003/0005):
// outbound-only WebSocket envelopes `{op, payload}` in, HTTP POSTs out. Audio never crosses this link; the daemon owns mic and speaker.
import type { Action } from './model';

export interface Controls { mic_muted?: boolean; speech_muted?: boolean }
export interface Runtime { submit: (text: string) => Promise<void>; cancel: (responseId: string | null) => Promise<void>; controls: (patch: Controls) => Promise<void>; reconnect: () => void; close: () => void }

// Daemon `voice` phases → UI phases. Anything unlisted leaves the phase alone.
const voicePhase: Record<string, Action> = {
  listening: { type: 'phase', phase: 'hearing' },
  transcribing: { type: 'phase', phase: 'processing' },
  accepted: { type: 'phase', phase: 'processing' },
  empty: { type: 'phase', phase: 'listening' },
  error: { type: 'phase', phase: 'listening' },
  spoken: { type: 'phase', phase: 'listening' },
};

export function connect(port: string, dispatch: (a: Action) => void): Runtime {
  const http = `http://127.0.0.1:${port}`;
  const post = async (path: string, body: unknown): Promise<Record<string, unknown>> => {
    const r = await fetch(`${http}${path}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`${path} ${r.status}`);
    return r.json();
  };
  // The daemon owns mute state; every answer (including the `{}` sync on connect) is authoritative.
  const controls = async (patch: Controls) => {
    const c = await post('/inherent/controls', patch);
    dispatch({ type: 'controls', micMuted: c.mic_muted === true, soundMuted: c.speech_muted === true });
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
      if (msg.op === 'open') dispatch({ type: 'open', turnId, responseId: typeof p.response_id === 'string' ? p.response_id : null });
      else if (msg.op === 'append') dispatch({ type: 'append', token: String(p.token ?? '') });
      // ponytail: text fades fadeMs after `done`; a long TTS tail can outlive it. Key the fade on `spoken` if that shows.
      else if (msg.op === 'done') setTimeout(() => dispatch({ type: 'settle', turnId }), Number(p.fadeMs ?? 5000));
      else if (msg.op === 'voice') { const a = voicePhase[String(p.phase)]; if (a) dispatch(a); }
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
    submit: async text => { await post('/inherent/submit', { text }); },
    // foreground_output stops what is audible now and lets the run finish (ADR-0008 D10).
    cancel: async responseId => { if (responseId) await post('/inherent/cancel-response', { response_id: responseId, scope: 'foreground_output' }); },
    controls,
    reconnect: () => { if (ws) ws.close(); else open(); },
    close: () => { closed = true; if (retry) clearTimeout(retry); ws?.close(); },
  };
}

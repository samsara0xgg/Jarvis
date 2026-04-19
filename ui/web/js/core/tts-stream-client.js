// ui/web/js/core/tts-stream-client.js
// Owns the /api/tts/stream WebSocket. Receives:
//   - text frames (JSON) — routed to onTurnStart / onSentenceStart /
//     onSentenceEnd / onTurnEnd / onCancel callbacks.
//   - binary frames (ArrayBuffer) — delivered to onAudioChunk.
// Reconnects with exponential backoff on close.

import { log } from '../utils/logger.js';

export class TTSStreamClient {
    constructor() {
        this.ws = null;
        this.serverUrl = '';
        this.sessionId = null;
        this.connected = false;

        this.onTurnStart = null;
        this.onSentenceStart = null;
        this.onAudioChunk = null;      // (ArrayBuffer) => void
        this.onSentenceEnd = null;
        this.onTurnEnd = null;
        this.onCancel = null;

        this._reconnectMs = 1000;
        this._maxReconnectMs = 5000;
        this._shouldReconnect = true;
    }

    setServerUrl(url) {
        this.serverUrl = url.replace(/\/+$/, '');
    }

    connect(sessionId) {
        this.sessionId = sessionId;
        this._shouldReconnect = true;
        this._open();
    }

    disconnect() {
        this._shouldReconnect = false;
        if (this.ws) { try { this.ws.close(); } catch {} this.ws = null; }
        this.connected = false;
    }

    _wsUrl() {
        const httpUrl = this.serverUrl || window.location.origin;
        const wsUrl = httpUrl.replace(/^http/, 'ws');
        return `${wsUrl}/api/tts/stream?session_id=${encodeURIComponent(this.sessionId)}`;
    }

    _open() {
        const url = this._wsUrl();
        log(`[TTSStreamClient] opening ${url}`, 'debug');
        this.ws = new WebSocket(url);
        this.ws.binaryType = 'arraybuffer';

        this.ws.onopen = () => {
            this.connected = true;
            this._reconnectMs = 1000;
            log('TTS stream WS connected', 'success');
        };
        this.ws.onmessage = (event) => {
            if (typeof event.data === 'string') {
                let payload;
                try { payload = JSON.parse(event.data); }
                catch (e) {
                    log(`[TTSStreamClient] bad JSON text frame: ${event.data.slice(0, 80)}`, 'warning');
                    return;
                }
                const t = payload.type;
                if (t === 'turn_start' && this.onTurnStart) this.onTurnStart(payload);
                else if (t === 'sentence_start' && this.onSentenceStart) this.onSentenceStart(payload);
                else if (t === 'sentence_end' && this.onSentenceEnd) this.onSentenceEnd(payload);
                else if (t === 'turn_end' && this.onTurnEnd) this.onTurnEnd(payload);
                else if (t === 'cancel' && this.onCancel) this.onCancel(payload);
                else if (t === 'ping') {
                    try { this.ws.send(JSON.stringify({ type: 'pong' })); } catch {}
                }
                else log(`[TTSStreamClient] unknown text frame type: ${t}`, 'warning');
            } else if (event.data instanceof ArrayBuffer) {
                if (this.onAudioChunk) this.onAudioChunk(event.data);
            } else {
                log(`[TTSStreamClient] unexpected message type: ${typeof event.data}`, 'warning');
            }
        };
        this.ws.onclose = (ev) => {
            log(`[TTSStreamClient] WS closed code=${ev.code} reason=${ev.reason || '(none)'}`, 'warning');
            this.connected = false;
            this.ws = null;
            if (this._shouldReconnect && this.sessionId) {
                setTimeout(() => this._open(), this._reconnectMs);
                this._reconnectMs = Math.min(this._reconnectMs * 2, this._maxReconnectMs);
            }
        };
        this.ws.onerror = () => {
            log('[TTSStreamClient] WS error event', 'warning');
        };
    }

    isConnected() { return this.connected; }

    sendCursor(turnId, samples) {
        if (!this.ws || !this.connected) return;
        try {
            this.ws.send(JSON.stringify({
                type: 'playback_cursor',
                turn_id: turnId || null,
                samples,
            }));
        } catch {}
    }
}

let instance = null;
export function getTTSStreamClient() {
    if (!instance) instance = new TTSStreamClient();
    return instance;
}

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
        this.ws = new WebSocket(this._wsUrl());
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
                catch { return; }
                const t = payload.type;
                if (t === 'turn_start' && this.onTurnStart) this.onTurnStart(payload);
                else if (t === 'sentence_start' && this.onSentenceStart) this.onSentenceStart(payload);
                else if (t === 'sentence_end' && this.onSentenceEnd) this.onSentenceEnd(payload);
                else if (t === 'turn_end' && this.onTurnEnd) this.onTurnEnd(payload);
                else if (t === 'cancel' && this.onCancel) this.onCancel(payload);
            } else if (event.data instanceof ArrayBuffer) {
                if (this.onAudioChunk) this.onAudioChunk(event.data);
            }
        };
        this.ws.onclose = () => {
            this.connected = false;
            this.ws = null;
            if (this._shouldReconnect && this.sessionId) {
                setTimeout(() => this._open(), this._reconnectMs);
                this._reconnectMs = Math.min(this._reconnectMs * 2, this._maxReconnectMs);
            }
        };
        this.ws.onerror = () => { /* onclose will follow */ };
    }

    isConnected() { return this.connected; }
}

let instance = null;
export function getTTSStreamClient() {
    if (!instance) instance = new TTSStreamClient();
    return instance;
}

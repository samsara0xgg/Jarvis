// 小月 HTTP + SSE API client (replaces WebSocket handler)
import { log } from '../utils/logger.js';

class ApiClient {
    constructor() {
        this.serverUrl = '';
        this.sessionId = null;
        this.connected = false;
        this.onConnectionStateChange = null;
        this.onChatMessage = null;
        this.onSentence = null;
        this.onSessionStateChange = null;
        this.onTurnDone = null;  // (traceId: number|null) => void
        // Client-side chat pipeline. New messages preempt the in-flight
        // fetch via AbortController; server handles concurrent POSTs.
        this._chatQueue = [];
        this._chatProcessing = false;
        this._chatAbortController = null;
    }

    setServerUrl(url) {
        this.serverUrl = url.replace(/\/+$/, '');
    }

    async checkHealth() {
        try {
            const resp = await fetch(`${this.serverUrl}/api/health`);
            return resp.ok;
        } catch {
            return false;
        }
    }

    async connect() {
        const healthy = await this.checkHealth();
        if (!healthy) {
            log('服务器不可达', 'error');
            return false;
        }
        try {
            const resp = await fetch(`${this.serverUrl}/api/session`, { method: 'POST' });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            this.sessionId = data.session_id;
            this.connected = true;
            log(`会话已建立: ${this.sessionId}`, 'success');
            if (this.onConnectionStateChange) this.onConnectionStateChange(true);
            return true;
        } catch (err) {
            log(`连接失败: ${err.message}`, 'error');
            if (this.onConnectionStateChange) this.onConnectionStateChange(false);
            return false;
        }
    }

    async disconnect() {
        if (!this.sessionId) return;
        // Local-first cleanup: flush audio immediately so the user hears
        // silence before the server round-trip returns. Even if the DELETE
        // fails, the ring is already drained.
        try {
            if (typeof window !== 'undefined' && window.chatApp && window.chatApp.audioPlayer) {
                const p = window.chatApp.audioPlayer;
                if (typeof p.clearAll === 'function') p.clearAll();
            }
        } catch { /* ignore */ }
        try {
            await fetch(`${this.serverUrl}/api/session/${this.sessionId}`, { method: 'DELETE' });
        } catch { /* ignore */ }
        this.sessionId = null;
        this.connected = false;
        log('会话已断开', 'info');
        if (this.onConnectionStateChange) this.onConnectionStateChange(false);
    }

    async cancelChat() {
        if (!this.sessionId) return false;
        try {
            const resp = await fetch(`${this.serverUrl}/api/chat/cancel`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: this.sessionId, text: '' }),
            });
            return resp.ok;
        } catch (err) {
            log(`cancelChat failed: ${err.message}`, 'warning');
            return false;
        }
    }

    async setHiddenMode(enabled) {
        try {
            await fetch(`${this.serverUrl}/api/hidden-mode`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: this.sessionId || '', enabled }),
            });
        } catch { /* ignore */ }
    }

    // Cached copy of `/api/llm/presets` so slash-command pickers can render
    // synchronously. getLLMPresets() refreshes it; getCachedLLMPresets() reads.
    async getLLMPresets() {
        try {
            const resp = await fetch(`${this.serverUrl}/api/llm/presets`);
            if (!resp.ok) return this._llmPresetsCache || { presets: [], active: null };
            const data = await resp.json();
            this._llmPresetsCache = data;
            return data;
        } catch {
            return this._llmPresetsCache || { presets: [], active: null };
        }
    }

    getCachedLLMPresets() {
        return this._llmPresetsCache || { presets: [], active: null };
    }

    async switchLLM(preset) {
        const resp = await fetch(`${this.serverUrl}/api/llm/switch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preset }),
        });
        if (!resp.ok) {
            const msg = await resp.text();
            throw new Error(msg || `switch failed (${resp.status})`);
        }
        // Refresh cache so `/llm` picker reflects new active preset next open
        this.getLLMPresets().catch(() => {});
        return resp.json();
    }

    async sendTextMessage(text) {
        if (!this.connected || !this.sessionId) return false;
        if (!text.trim()) return false;

        // Preempt any in-flight chat: abort its fetch so its SSE reader
        // unblocks immediately. The server's /api/chat already does an
        // unconditional cancel-flush on every new POST, so concurrent
        // handle_text calls are safe. Serializing on the client would
        // make new utterances wait for the full previous turn to drain —
        // that's the "new chat doesn't interrupt" bug.
        if (this._chatAbortController) {
            try { this._chatAbortController.abort(); } catch {}
        }
        // Resolve any stale queued items so their callers don't hang.
        while (this._chatQueue.length) {
            try { this._chatQueue.shift().resolve(false); } catch {}
        }

        return new Promise((resolve) => {
            this._chatQueue.push({ text, resolve });
            if (!this._chatProcessing) this._processNextChat();
        });
    }

    async _processNextChat() {
        if (this._chatQueue.length === 0) {
            this._chatProcessing = false;
            return;
        }
        this._chatProcessing = true;
        const { text, resolve } = this._chatQueue.shift();
        this._chatAbortController = new AbortController();

        if (this.onSessionStateChange) this.onSessionStateChange(true);

        try {
            const resp = await fetch(`${this.serverUrl}/api/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, session_id: this.sessionId }),
                signal: this._chatAbortController.signal,
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });

                const lines = buffer.split('\n');
                buffer = lines.pop();

                let eventType = '';
                let eventData = '';
                for (const line of lines) {
                    if (line.startsWith('event: ')) {
                        eventType = line.slice(7).trim();
                    } else if (line.startsWith('data: ')) {
                        eventData = line.slice(6);
                    } else if (line === '' && eventType) {
                        if (eventType === 'sentence') {
                            try {
                                const parsed = JSON.parse(eventData);
                                if (this.onSentence) this.onSentence(parsed);
                                if (this.onChatMessage) this.onChatMessage(parsed.text, false);
                            } catch (e) {
                                log(`SSE parse error: ${e.message}`, 'error');
                            }
                        } else if (eventType === 'done') {
                            try {
                                const parsed = JSON.parse(eventData || '{}');
                                if (this.onTurnDone) this.onTurnDone(parsed.trace_id ?? null);
                            } catch { /* ignore */ }
                        } else if (eventType === 'log') {
                            try {
                                const parsed = JSON.parse(eventData);
                                const level = (parsed.level || 'INFO').toLowerCase();
                                const logType = level === 'error' ? 'error'
                                    : level === 'warning' ? 'warning'
                                    : level === 'info' ? 'info' : 'debug';
                                log(`[Server] ${parsed.msg}`, logType);
                            } catch { /* ignore */ }
                        }
                        eventType = '';
                        eventData = '';
                    }
                }
            }
        } catch (err) {
            // AbortError means a new message preempted this one — expected.
            if (err.name !== 'AbortError') {
                log(`聊天请求失败: ${err.message}`, 'error');
                if (this.onChatMessage) this.onChatMessage(`请求失败: ${err.message}`, false);
            }
        } finally {
            this._chatAbortController = null;
            if (this.onSessionStateChange) this.onSessionStateChange(false);
            resolve(true);
            this._processNextChat();
        }
    }

    async sendAudio(wavBlob) {
        if (!this.connected || !this.sessionId) return null;
        try {
            const formData = new FormData();
            formData.append('audio', wavBlob, 'recording.wav');
            formData.append('session_id', this.sessionId);
            const resp = await fetch(`${this.serverUrl}/api/asr`, {
                method: 'POST',
                body: formData,
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            return await resp.json();
        } catch (err) {
            log(`ASR 请求失败: ${err.message}`, 'error');
            return null;
        }
    }

    isConnected() {
        return this.connected && this.sessionId !== null;
    }
}

let instance = null;
export function getApiClient() {
    if (!instance) instance = new ApiClient();
    return instance;
}

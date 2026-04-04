// Jarvis HTTP + SSE API client (replaces WebSocket handler)
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
        try {
            await fetch(`${this.serverUrl}/api/session/${this.sessionId}`, { method: 'DELETE' });
        } catch { /* ignore */ }
        this.sessionId = null;
        this.connected = false;
        log('会话已断开', 'info');
        if (this.onConnectionStateChange) this.onConnectionStateChange(false);
    }

    async sendTextMessage(text) {
        if (!this.connected || !this.sessionId) return false;
        if (!text.trim()) return false;

        if (this.onSessionStateChange) this.onSessionStateChange(true);

        try {
            const resp = await fetch(`${this.serverUrl}/api/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, session_id: this.sessionId }),
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
                        }
                        eventType = '';
                        eventData = '';
                    }
                }
            }
        } catch (err) {
            log(`聊天请求失败: ${err.message}`, 'error');
            if (this.onChatMessage) this.onChatMessage(`请求失败: ${err.message}`, false);
        } finally {
            if (this.onSessionStateChange) this.onSessionStateChange(false);
        }
        return true;
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

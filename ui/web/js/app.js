// 主应用入口
import { getAudioPlayer } from './core/audio/player.js';
import { getPCMStreamPlayer } from './core/audio/pcm-stream-player.js';
import { getTTSStreamClient } from './core/tts-stream-client.js';
import { getApiClient } from './core/api-client.js';
import { checkMicrophoneAvailability, isHttpNonLocalhost } from './core/audio/recorder.js';
import { uiController } from './ui/controller.js';
import { petOverlay } from './ui/pet-overlay.js';
import { log } from './utils/logger.js';

class App {
    constructor() {
        this.uiController = null;
        this.audioPlayer = null;
        this.live2dManager = null;
    }

    async init() {
        log('正在初始化应用...', 'info');
        this.uiController = uiController;
        this.uiController.init();
        petOverlay.init(); // idempotent; controller also calls this

        // Prefer streaming PCM player; fall back to legacy AudioPlayer if the
        // AudioWorklet or 32kHz AudioContext is unavailable.
        try {
            const streamPlayer = getPCMStreamPlayer();
            await streamPlayer.start();
            this.audioPlayer = streamPlayer;
            this._wireTTSStream();
            log('使用 PCMStreamPlayer (WS streaming)', 'info');
        } catch (err) {
            log(`PCMStreamPlayer 初始化失败，回退 AudioPlayer: ${err.message}`, 'warning');
            this.audioPlayer = getAudioPlayer();
            await this.audioPlayer.start();
        }

        // First-gesture hook: user must tap before AudioContext resumes
        // (browser autoplay policy). Once only, then remove.
        const resume = () => {
            if (this.audioPlayer && typeof this.audioPlayer.resumeOnGesture === 'function') {
                this.audioPlayer.resumeOnGesture().catch(() => {});
            }
        };
        document.addEventListener('click', resume, { once: true });
        document.addEventListener('touchstart', resume, { once: true });

        // Esc / Cmd+. → stop current TTS turn. Always-on, idempotent.
        document.addEventListener('keydown', (ev) => {
            const isStop = ev.key === 'Escape' || (ev.metaKey && ev.key === '.');
            if (!isStop) return;
            ev.preventDefault();
            const apiClient = getApiClient();
            if (apiClient && typeof apiClient.cancelChat === 'function') {
                apiClient.cancelChat().then((ok) => {
                    log(`cancelChat ${ok ? 'ok' : 'failed'}`, ok ? 'info' : 'warning');
                });
            }
        });

        await this.checkMicrophoneAvailability();
        await this.initLive2D();
        this.setModelLoadingStatus(false);
        log('应用初始化完成', 'success');
    }

    _wireTTSStream() {
        // Open the /api/tts/stream WebSocket after the session dials.
        // Monkey-patch apiClient.connect to chain the TTSStreamClient
        // connect on successful session establishment. This avoids touching
        // controller.js (which has unrelated WIP).
        const apiClient = getApiClient();
        const tts = getTTSStreamClient();
        tts.setServerUrl(window.location.origin);

        // DEBUG: track every WS event to diagnose stream issues.
        this._dbgStats = { turns: 0, sentences: 0, chunks: 0, bytes: 0, cancels: 0 };

        tts.onTurnStart = (p) => {
            this._dbgStats.turns++;
            log(`[ws] turn_start turn_id=${p.turn_id}`, 'debug');
        };
        tts.onAudioChunk = (buf) => {
            this._dbgStats.chunks++;
            this._dbgStats.bytes += buf.byteLength;
            if (this._dbgStats.chunks % 10 === 0) {
                log(`[ws] audio: ${this._dbgStats.chunks} chunks, ${(this._dbgStats.bytes / 1024).toFixed(1)} KB`, 'debug');
            }
            if (this.audioPlayer && typeof this.audioPlayer.writeChunk === 'function') {
                this.audioPlayer.writeChunk(buf);
            } else {
                log('[ws] audio_chunk received but audioPlayer has no writeChunk', 'warning');
            }
        };
        tts.onSentenceStart = (p) => {
            this._dbgStats.sentences++;
            log(`[ws] sentence_start idx=${p.sentence_index} emotion=${p.emotion} text=${(p.text || '').slice(0, 30)}`, 'debug');
            if (this.live2dManager) {
                this.live2dManager.triggerEmotionAction(p.emotion || 'neutral');
                this.live2dManager.startTalking();
            }
        };
        tts.onSentenceEnd = (p) => {
            log(`[ws] sentence_end idx=${p.sentence_index}`, 'debug');
        };
        tts.onTurnEnd = (p) => {
            log(`[ws] turn_end turn_id=${p && p.turn_id}. Stats: ${this._dbgStats.sentences} sentences, ${this._dbgStats.chunks} chunks, ${(this._dbgStats.bytes/1024).toFixed(1)} KB`, 'info');
            if (this.live2dManager) this.live2dManager.stopTalking();
        };
        tts.onCancel = (p) => {
            this._dbgStats.cancels++;
            log(`[ws] cancel reason=${p && p.reason} turn_id=${p && p.turn_id}`, 'warning');
            if (this.audioPlayer && typeof this.audioPlayer.clearAll === 'function') {
                this.audioPlayer.clearAll();
            }
            if (this.live2dManager) this.live2dManager.stopTalking();
            const r = p && p.reason;
            if (r === 'new_chat' || r === 'user_stop') {
                if (this.live2dManager) this.live2dManager.triggerEmotionAction('neutral');
            } else if (r === 'pipeline_error') {
                log('TTS error, retrying…', 'warning');
            }
        };

        this._ttsStream = tts;

        // Connect the TTS stream. Safe to call multiple times.
        const connectTTS = () => {
            if (apiClient.sessionId && !tts.connected && !tts.ws) {
                try { tts.connect(apiClient.sessionId); }
                catch (err) { log(`TTS stream connect failed: ${err.message}`, 'warning'); }
            }
        };

        // Immediate: if dial already completed before _wireTTSStream ran,
        // connect now. (Race between apiClient.connect click and
        // addModule's await.)
        connectTTS();

        // Future dials: wrap apiClient.connect so the TTS stream opens on
        // subsequent session dial (e.g. after disconnect/re-dial).
        const origConnect = apiClient.connect.bind(apiClient);
        apiClient.connect = async () => {
            const ok = await origConnect();
            if (ok) connectTTS();
            return ok;
        };

        // Wrap disconnect to also close the TTS stream.
        const origDisconnect = apiClient.disconnect.bind(apiClient);
        apiClient.disconnect = async () => {
            try { tts.disconnect(); } catch {}
            return origDisconnect();
        };
    }

    async initLive2D() {
        try {
            if (typeof window.Live2DManager === 'undefined') {
                throw new Error('Live2DManager未加载，请检查脚本引入顺序');
            }
            this.live2dManager = new window.Live2DManager();
            await this.live2dManager.initializeLive2D();
            log('Live2D初始化完成', 'success');
        } catch (error) {
            log(`Live2D初始化失败: ${error.message}`, 'error');
        }
    }

    setModelLoadingStatus(isLoading) {
        const modelLoading = document.getElementById('modelLoading');
        if (modelLoading) {
            modelLoading.style.display = isLoading ? 'flex' : 'none';
        }
    }

    async checkMicrophoneAvailability() {
        try {
            const isAvailable = await checkMicrophoneAvailability();
            const isHttp = isHttpNonLocalhost();
            window.microphoneAvailable = isAvailable;
            window.isHttpNonLocalhost = isHttp;
            if (this.uiController) {
                this.uiController.updateMicrophoneAvailability(isAvailable, isHttp);
            }
            log(`麦克风可用性: ${isAvailable ? '可用' : '不可用'}`, isAvailable ? 'success' : 'warning');
        } catch (error) {
            log(`检查麦克风可用性失败: ${error.message}`, 'error');
            window.microphoneAvailable = false;
            window.isHttpNonLocalhost = isHttpNonLocalhost();
            if (this.uiController) {
                this.uiController.updateMicrophoneAvailability(false, window.isHttpNonLocalhost);
            }
        }
    }
}

const app = new App();
window.chatApp = app;
document.addEventListener('DOMContentLoaded', () => {
    app.init();
});
export default app;

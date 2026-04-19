// ui/web/js/core/audio/pcm-stream-player.js
// Public API parity with ui/web/js/core/audio/player.js (AudioPlayer):
//   - start()
//   - getAnalyser()
//   - getAudioContext()
//   - clearAll()
// Plus streaming-specific writeChunk(ArrayBuffer) and resumeOnGesture().
//
// sampleRate is pinned to 32000 to match MiniMax PCM output — the
// AudioWorklet then reads 1:1 from the ring buffer with no interpolation.
// If the target browser rejects that rate the constructor throws; caller
// falls back to legacy AudioPlayer.

import { log } from '../../utils/logger.js';

export class PCMStreamPlayer {
    constructor() {
        this.ctx = null;
        this.node = null;
        this.gainNode = null;
        this.analyser = null;
        this._ready = false;
        this._preCtxQueue = [];   // Float32Array chunks, bounded ~2s
        this._preCtxSamples = 0;
        // Called on every cursor report from the worklet. Main loop wires
        // this to tts-stream-client to relay the cursor to the server.
        this.onCursor = null;
    }

    async start() {
        this.ctx = new (window.AudioContext || window.webkitAudioContext)({
            sampleRate: 32000,
        });
        const basePath = (() => {
            const p = window.location.pathname;
            return p.substring(0, p.lastIndexOf('/') + 1);
        })();
        await this.ctx.audioWorklet.addModule(
            basePath + 'js/core/audio/pcm-player-processor.js',
        );
        this.node = new AudioWorkletNode(this.ctx, 'pcm-player');
        // Surface worklet overflow notifications to the main-thread log so
        // pacing bugs are visible. Worklet only posts once per overflow run.
        this.node.port.onmessage = (e) => {
            if (!e.data) return;
            if (typeof e.data.overflow === 'number') {
                log(`PCMStreamPlayer ring overflow, dropped ${e.data.overflow} samples`, 'warning');
            }
            if (typeof e.data.cursor === 'number' && this.onCursor) {
                this.onCursor(e.data.cursor);
            }
        };
        this.gainNode = this.ctx.createGain();
        this.analyser = this.ctx.createAnalyser();
        this.analyser.fftSize = 256;
        this.node.connect(this.gainNode);
        this.gainNode.connect(this.analyser);
        this.analyser.connect(this.ctx.destination);
        this._ready = true;
        log('PCMStreamPlayer 初始化完成 (32kHz)', 'success');
    }

    getAnalyser() { return this.analyser; }
    getAudioContext() { return this.ctx; }

    async resumeOnGesture() {
        if (!this.ctx) return;
        if (this.ctx.state === 'suspended') await this.ctx.resume();
        // Drain pre-gesture queue.
        while (this._preCtxQueue.length) {
            this._pushToWorklet(this._preCtxQueue.shift());
        }
        this._preCtxSamples = 0;
    }

    writeChunk(arrayBuf) {
        // arrayBuf: raw binary WS frame. First 2 bytes = uint16 LE
        // sentence_index (we don't use it client-side for now — WS frame
        // order is preserved). Remaining bytes = int16 LE @ 32 kHz mono.
        if (!this._ready) return;
        if (arrayBuf.byteLength < 2) return;
        const i16 = new Int16Array(arrayBuf, 2);
        const f32 = new Float32Array(i16.length);
        for (let j = 0; j < i16.length; j++) f32[j] = i16[j] / 32768;

        if (this.ctx.state === 'suspended') {
            // Bound at ~2s of audio to prevent runaway buffering if the
            // user never clicks.
            if (this._preCtxSamples + f32.length < 64000) {
                this._preCtxQueue.push(f32);
                this._preCtxSamples += f32.length;
            }
            return;
        }
        this._pushToWorklet(f32);
    }

    _pushToWorklet(f32) {
        this.node.port.postMessage({ pcm: f32 }, [f32.buffer]);
    }

    clearAll() {
        this._preCtxQueue = [];
        this._preCtxSamples = 0;
        if (this.node) this.node.port.postMessage({ clear: true });
    }
}

let instance = null;
export function getPCMStreamPlayer() {
    if (!instance) instance = new PCMStreamPlayer();
    return instance;
}

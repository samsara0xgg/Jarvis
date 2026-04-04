// Audio player — fetches MP3 URLs, decodes, plays in sequence via Web Audio API.
// Exposes AnalyserNode for Live2D lip-sync.
import { log } from '../../utils/logger.js';

export class AudioPlayer {
    constructor() {
        this.audioContext = null;
        this.analyser = null;
        this.gainNode = null;
        this._queue = [];
        this._playing = false;
    }

    getAudioContext() {
        if (!this.audioContext) {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
        }
        return this.audioContext;
    }

    getAnalyser() {
        if (!this.analyser) {
            const ctx = this.getAudioContext();
            this.analyser = ctx.createAnalyser();
            this.analyser.fftSize = 256;
            this.gainNode = ctx.createGain();
            this.gainNode.connect(this.analyser);
            this.analyser.connect(ctx.destination);
        }
        return this.analyser;
    }

    enqueue(url) {
        return new Promise((resolve) => {
            this._queue.push({ url, resolve });
            if (!this._playing) this._playNext();
        });
    }

    async _playNext() {
        if (this._queue.length === 0) {
            this._playing = false;
            return;
        }
        this._playing = true;
        const { url, resolve } = this._queue.shift();

        try {
            const ctx = this.getAudioContext();
            if (ctx.state === 'suspended') await ctx.resume();

            const resp = await fetch(url);
            if (!resp.ok) throw new Error(`Fetch ${url} failed: ${resp.status}`);
            const arrayBuf = await resp.arrayBuffer();
            const audioBuf = await ctx.decodeAudioData(arrayBuf);

            const source = ctx.createBufferSource();
            source.buffer = audioBuf;
            this.getAnalyser();
            source.connect(this.gainNode);

            source.onended = () => {
                resolve();
                this._playNext();
            };
            source.start(0);
        } catch (err) {
            log(`音频播放失败: ${err.message}`, 'error');
            resolve();
            this._playNext();
        }
    }

    clearAll() {
        this._queue = [];
    }

    async start() {
        this.getAudioContext();
        this.getAnalyser();
        log('AudioPlayer 初始化完成', 'success');
    }
}

let instance = null;
export function getAudioPlayer() {
    if (!instance) instance = new AudioPlayer();
    return instance;
}

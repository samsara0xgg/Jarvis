// ui/web/js/core/audio/pcm-player-processor.js
// AudioWorklet that plays PCM pushed via port.postMessage({pcm: Float32Array}).
// Ring buffer sized for 10 seconds of 32 kHz audio. `{clear: true}` resets.

class PCMPlayerProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this._ring = new Float32Array(32000 * 10);  // 10s @ 32kHz
        this._read = 0;
        this._write = 0;
        this.port.onmessage = (e) => {
            if (e.data && e.data.clear) {
                this._read = this._write = 0;
                return;
            }
            const chunk = e.data && e.data.pcm;
            if (!chunk) return;
            for (let i = 0; i < chunk.length; i++) {
                this._ring[this._write] = chunk[i];
                this._write = (this._write + 1) % this._ring.length;
                // Overflow: advance read pointer (drop oldest).
                if (this._write === this._read) {
                    this._read = (this._read + 1) % this._ring.length;
                }
            }
        };
    }
    process(_inputs, outputs) {
        const out = outputs[0][0];  // mono
        for (let i = 0; i < out.length; i++) {
            if (this._read === this._write) {
                out[i] = 0;
            } else {
                out[i] = this._ring[this._read];
                this._read = (this._read + 1) % this._ring.length;
            }
        }
        return true;
    }
}
registerProcessor('pcm-player', PCMPlayerProcessor);

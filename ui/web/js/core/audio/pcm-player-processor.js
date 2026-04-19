// ui/web/js/core/audio/pcm-player-processor.js
// AudioWorklet that plays PCM pushed via port.postMessage({pcm: Float32Array}).
// Ring buffer sized for 30 seconds of 32 kHz audio (~3.84 MB). Server paces
// writes to near real-time, so overflow here should never happen in practice;
// if it does, we drop NEW samples (preserve the earlier, unplayed audio)
// instead of evicting in-flight playback data. Evicting old samples while the
// reader is mid-ring produces audible overlap between sentences — worse than
// cleanly clipping the tail.
// `{clear: true}` resets (used by cancel frames).

const RING_SECONDS = 30;
const SAMPLE_RATE = 32000;

class PCMPlayerProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this._ring = new Float32Array(SAMPLE_RATE * RING_SECONDS);
        this._read = 0;
        this._write = 0;
        this._count = 0;  // live samples in ring; avoids read===write ambiguity
        this._overflowNotified = false;
        this.port.onmessage = (e) => {
            if (e.data && e.data.clear) {
                this._read = this._write = 0;
                this._count = 0;
                this._overflowNotified = false;
                return;
            }
            const chunk = e.data && e.data.pcm;
            if (!chunk) return;
            const cap = this._ring.length;
            let dropped = 0;
            for (let i = 0; i < chunk.length; i++) {
                if (this._count >= cap) {
                    dropped++;
                    continue;  // ring full → discard incoming sample
                }
                this._ring[this._write] = chunk[i];
                this._write = (this._write + 1) % cap;
                this._count++;
            }
            if (dropped > 0 && !this._overflowNotified) {
                this._overflowNotified = true;
                // Main thread can log; worklet context has no console.
                this.port.postMessage({ overflow: dropped });
            }
        };
    }
    process(_inputs, outputs) {
        const out = outputs[0][0];  // mono
        const cap = this._ring.length;
        for (let i = 0; i < out.length; i++) {
            if (this._count === 0) {
                out[i] = 0;
            } else {
                out[i] = this._ring[this._read];
                this._read = (this._read + 1) % cap;
                this._count--;
            }
        }
        return true;
    }
}
registerProcessor('pcm-player', PCMPlayerProcessor);

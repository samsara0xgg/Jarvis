// Audio recorder — AudioWorklet captures 16kHz mono PCM, builds WAV blob.
import { log } from '../../utils/logger.js';
import { getAudioPlayer } from './player.js';
import { getApiClient } from '../api-client.js';

class AudioRecorder {
    constructor() {
        this.isRecording = false;
        this.audioContext = null;
        this.workletNode = null;
        this.sourceNode = null;
        this.stream = null;
        this.pcmChunks = [];
        this.recordingTimer = null;
        this.onRecordingStart = null;
        this.onRecordingStop = null;
    }

    getAudioContext() {
        return getAudioPlayer().getAudioContext();
    }

    _workletCode() {
        return `
            class RecorderProcessor extends AudioWorkletProcessor {
                constructor() {
                    super();
                    this.recording = false;
                    this.port.onmessage = (e) => {
                        if (e.data.command === 'start') this.recording = true;
                        if (e.data.command === 'stop') this.recording = false;
                    };
                }
                process(inputs) {
                    if (!this.recording || !inputs[0][0]) return true;
                    const float32 = inputs[0][0];
                    const int16 = new Int16Array(float32.length);
                    for (let i = 0; i < float32.length; i++) {
                        int16[i] = Math.max(-32768, Math.min(32767, Math.floor(float32[i] * 32767)));
                    }
                    this.port.postMessage({ pcm: int16 }, [int16.buffer]);
                    return true;
                }
            }
            registerProcessor('jarvis-recorder', RecorderProcessor);
        `;
    }

    async start() {
        if (this.isRecording) return false;
        try {
            this.stream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000, channelCount: 1 }
            });
            this.audioContext = this.getAudioContext();
            if (this.audioContext.state === 'suspended') await this.audioContext.resume();

            const blob = new Blob([this._workletCode()], { type: 'application/javascript' });
            const url = URL.createObjectURL(blob);
            await this.audioContext.audioWorklet.addModule(url);
            URL.revokeObjectURL(url);

            this.workletNode = new AudioWorkletNode(this.audioContext, 'jarvis-recorder');
            this.sourceNode = this.audioContext.createMediaStreamSource(this.stream);
            this.sourceNode.connect(this.workletNode);
            const silent = this.audioContext.createGain();
            silent.gain.value = 0;
            this.workletNode.connect(silent);
            silent.connect(this.audioContext.destination);

            this.pcmChunks = [];
            this.workletNode.port.onmessage = (e) => {
                if (e.data.pcm) this.pcmChunks.push(e.data.pcm);
            };
            this.workletNode.port.postMessage({ command: 'start' });
            this.isRecording = true;

            let seconds = 0;
            if (this.onRecordingStart) this.onRecordingStart(0);
            this.recordingTimer = setInterval(() => {
                seconds += 0.1;
                if (this.onRecordingStart) this.onRecordingStart(seconds);
            }, 100);

            log('录音已开始', 'success');
            return true;
        } catch (err) {
            log(`录音启动失败: ${err.message}`, 'error');
            this.isRecording = false;
            return false;
        }
    }

    stop() {
        if (!this.isRecording) return null;
        this.isRecording = false;
        if (this.workletNode) {
            this.workletNode.port.postMessage({ command: 'stop' });
            this.workletNode.disconnect();
            this.workletNode = null;
        }
        if (this.sourceNode) {
            this.sourceNode.disconnect();
            this.sourceNode = null;
        }
        if (this.stream) {
            this.stream.getTracks().forEach(t => t.stop());
            this.stream = null;
        }
        if (this.recordingTimer) {
            clearInterval(this.recordingTimer);
            this.recordingTimer = null;
        }
        if (this.onRecordingStop) this.onRecordingStop();

        const wavBlob = this._buildWav();
        log(`录音已停止，WAV 大小: ${wavBlob.size} bytes`, 'success');
        this._sendToASR(wavBlob);
        return wavBlob;
    }

    _buildWav() {
        let totalLen = 0;
        for (const chunk of this.pcmChunks) totalLen += chunk.length;
        const merged = new Int16Array(totalLen);
        let offset = 0;
        for (const chunk of this.pcmChunks) {
            merged.set(chunk, offset);
            offset += chunk.length;
        }
        this.pcmChunks = [];

        const sr = 16000;
        const dataBytes = merged.length * 2;
        const buffer = new ArrayBuffer(44 + dataBytes);
        const view = new DataView(buffer);
        const writeStr = (off, str) => { for (let i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i)); };
        writeStr(0, 'RIFF');
        view.setUint32(4, 36 + dataBytes, true);
        writeStr(8, 'WAVE');
        writeStr(12, 'fmt ');
        view.setUint32(16, 16, true);
        view.setUint16(20, 1, true);
        view.setUint16(22, 1, true);
        view.setUint32(24, sr, true);
        view.setUint32(28, sr * 2, true);
        view.setUint16(32, 2, true);
        view.setUint16(34, 16, true);
        writeStr(36, 'data');
        view.setUint32(40, dataBytes, true);
        const pcmView = new Int16Array(buffer, 44);
        pcmView.set(merged);

        return new Blob([buffer], { type: 'audio/wav' });
    }

    async _sendToASR(wavBlob) {
        const apiClient = getApiClient();
        const result = await apiClient.sendAudio(wavBlob);
        if (result && result.text) {
            log(`ASR 识别: ${result.text}`, 'info');
            if (apiClient.onChatMessage) apiClient.onChatMessage(result.text, true);
            await apiClient.sendTextMessage(result.text);
        } else {
            log('ASR 未识别到文字', 'warning');
        }
    }
}

let instance = null;
export function getAudioRecorder() {
    if (!instance) instance = new AudioRecorder();
    return instance;
}

export async function checkMicrophoneAvailability() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return false;
    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000, channelCount: 1 }
        });
        stream.getTracks().forEach(t => t.stop());
        return true;
    } catch { return false; }
}

export function isHttpNonLocalhost() {
    if (window.location.protocol !== 'http:') return false;
    const h = window.location.hostname;
    if (h === 'localhost' || h === '127.0.0.1') return false;
    if (h.startsWith('192.168.') || h.startsWith('10.') || h.startsWith('172.')) return false;
    return true;
}

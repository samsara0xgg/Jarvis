import AVFoundation
import Foundation
import os

// One talk from the wrist, on the phone: the watch sends its microphone as 16 kHz mono Int16 data messages (`WatchWire.audio`), and this feeds them to the phone's own
// recognizer (Voice/VoiceRecognizer.swift `DualRecognizer`, the two locales on the same audio). It ends the talk as the voice session does: 0.8 s with no new words and the
// level under `quiet` (Voice/VoiceSession.swift). It starts nothing of the phone's voice (no microphone, no player). Called from WatchConnectivity's threads.

final class WatchTalk: @unchecked Sendable {
    let id: UUID
    private let send: @Sendable (WatchTell) -> Void
    private let paused: @Sendable () -> Void
    private var rec: AnyObject?                     // a DualRecognizer (iOS 26), made in init and not changed
    private var ticker: Task<Void, Never>?

    private struct State {
        var text = ""
        var startedAt = WatchTalk.now, lastWordAt = WatchTalk.now, sentAt = 0.0
        var raw = 0.0, level = 0.0
        var chunks = 0, samples = 0
        var ended = false
    }
    private let state = OSAllocatedUnfairLock(initialState: State())

    private let silenceEnd = 0.8, quiet = 0.3       // the voice session's own
    private let noSpeech = 10.0, cap = 30.0

    private static let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: WatchWire.sampleRate, channels: 1, interleaved: false)!
    private static var now: Double { ProcessInfo.processInfo.systemUptime }

    /// the phone can recognize what the wrist sends
    static var canListen: Bool { if #available(iOS 26, *) { return true } else { return false } }

    /// `send` tells the wrist; `paused` says the talk reached its end by itself (a pause, or too long a silence)
    init(id: UUID, send: @escaping @Sendable (WatchTell) -> Void, paused: @escaping @Sendable () -> Void) {
        self.id = id; self.send = send; self.paused = paused
        if #available(iOS 26, *) { rec = DualRecognizer(onPartial: { [weak self] p in self?.heard(p.text) }) }
        ticker = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 100_000_000)
                self?.tick()
            }
        }
    }

    /// what the wrist's microphone sent next (the samples of an audio data message)
    func feed(_ samples: Data) {
        let n = samples.count / 2
        guard n > 0, let buf = AVAudioPCMBuffer(pcmFormat: Self.format, frameCapacity: AVAudioFrameCount(n)), let out = buf.floatChannelData?[0] else { return }
        buf.frameLength = AVAudioFrameCount(n)
        var sum: Float = 0
        samples.withUnsafeBytes { raw in
            for i in 0..<n {
                let v = Float(Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))) / 32768
                out[i] = v; sum += v * v
            }
        }
        // RMS in dB mapped from -55...-10 to 0...1, as the phone's microphone level is (Voice/VoiceAudio.swift)
        let db = 20 * log10(max((sum / Float(n)).squareRoot(), 1e-5))
        let level = min(1, max(0, (Double(db) + 55) / 45))
        state.withLock { $0.raw = max($0.raw, level); $0.chunks += 1; $0.samples += n }
        if #available(iOS 26, *) { (rec as? DualRecognizer)?.feed(buf) }
    }

    /// how many data messages and samples came, for the log
    var counts: (chunks: Int, samples: Int) { state.withLock { ($0.chunks, $0.samples) } }

    private func heard(_ text: String) {
        let now = Self.now
        let emit: String? = state.withLock { s in
            guard text != s.text else { return nil }
            s.text = text; s.lastWordAt = now
            guard now - s.sentAt >= 0.2 else { return nil }          // about five a second
            s.sentAt = now
            return text
        }
        if let emit { send(.heard(talk: id, text: emit, final: false)) }
    }

    private func tick() {
        let now = Self.now
        let over = state.withLock { s -> Bool in
            guard !s.ended else { return false }
            s.level = max(s.raw, s.level * 0.85); s.raw = 0
            let done = s.text.isEmpty ? now - s.startedAt > noSpeech : now - s.lastWordAt > silenceEnd && s.level < quiet
            if done || now - s.startedAt > cap { s.ended = true; return true }
            return false
        }
        if over { paused() }
    }

    /// The talk is over: what was said, once the recognizer has delivered its final text (within about three seconds).
    func finish() async -> String {
        ticker?.cancel()
        state.withLock { $0.ended = true }
        var said = state.withLock { $0.text }
        if #available(iOS 26, *), let r = rec as? DualRecognizer {
            let t = await r.finish().picked.text.trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty { said = t }
        }
        return said
    }

    func cancel() {
        ticker?.cancel()
        if #available(iOS 26, *), let r = rec as? DualRecognizer { Task { await r.cancel() } }
    }
}

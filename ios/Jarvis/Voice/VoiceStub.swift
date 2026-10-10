import AVFoundation

/// A host that is only this phone: it echoes what he said, in text and in AVSpeechSynthesizer's voice, through the same player frames the
/// real host sends, so listening, picking, saying, the row, the audio and the reports all run without a pairing.
@MainActor
final class LocalStubLink: VoiceLink {
    let events: AsyncStream<LinkEvent>
    private let out: AsyncStream<LinkEvent>.Continuation
    private var turns = 0
    private var generation: Int64 = 0
    private var discardSeq: UInt64 = 0
    private var speaking: Task<Void, Never>?

    init() { (events, out) = AsyncStream.makeStream(of: LinkEvent.self) }

    func connect() { out.yield(.ready(device: "phone", voice: "AVSpeechSynthesizer", sampleRate: kWireRate)) }
    func disconnect() { stopSpeaking() }
    func barge() { stopSpeaking() }
    func cancel() { stopSpeaking() }
    func send(_ frames: [PlayerOut]) {}

    func say(utteranceId: String, text: String, language: VoiceLang?, spoken: Bool) async throws -> String {
        turns += 1
        let turn = "stub-\(turns)"
        let reply = "还没连上大脑。你刚才说的是：\(text)"
        out.yield(.row(RowFrame(json: ["turn_id": turn, "role": "assistant", "text": reply])))
        if spoken {
            let zh = language.map { $0 == .zh } ?? text.unicodeScalars.contains { $0.value >= 0x2E80 }
            speaking = Task { await speak(zh ? reply : "Not connected to the brain yet. You said: \(text)", zh: zh) }
        }
        return turn
    }

    private func stopSpeaking() {
        guard let t = speaking else { return }
        speaking = nil
        t.cancel()
        discardSeq += 1
        out.yield(.audio(.active(-1)))                  // what the host sends on a barge-in
        out.yield(.audio(.discard(discardSeq)))
    }

    /// 20 ms frames, handed over 2x faster than they play so the player's ring never overflows on a long answer.
    private func speak(_ text: String, zh: Bool) async {
        let x = await Synth.render(text, zh: zh)
        generation += 1
        let gen = generation
        out.yield(.audio(.active(gen)))
        var at = 0
        while at < x.count, !Task.isCancelled {
            for _ in 0..<10 where at < x.count {
                let n = min(640, x.count - at)
                out.yield(.audio(.pcm(generation: gen, start: Int64(at), samples: Array(x[at..<at + n]))))
                at += n
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
    }
}

/// AVSpeechSynthesizer.write, converted to int16 mono at the wire rate.
private final class Synth: @unchecked Sendable {
    let synth = AVSpeechSynthesizer()
    var samples: [Int16] = []
    var conv: AVAudioConverter?
    var done = false

    static func render(_ text: String, zh: Bool) async -> [Int16] {
        await withCheckedContinuation { c in
            let s = Synth()
            let u = AVSpeechUtterance(string: text)
            u.voice = AVSpeechSynthesisVoice(language: zh ? "zh-CN" : "en-US")
            let wire = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Double(kWireRate), channels: 1, interleaved: true)!
            s.synth.write(u) { buf in
                guard !s.done, let pcm = buf as? AVAudioPCMBuffer else { return }
                if pcm.frameLength == 0 { s.done = true; c.resume(returning: s.samples); return }   // the empty buffer ends the speech
                if s.conv == nil { s.conv = AVAudioConverter(from: pcm.format, to: wire) }
                if let conv = s.conv, let o = convert(pcm, using: conv), let p = o.int16ChannelData?[0] {
                    s.samples += UnsafeBufferPointer(start: p, count: Int(o.frameLength))
                }
            }
        }
    }
}

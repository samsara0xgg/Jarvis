// words: legacy
import AVFoundation

/// A host that is only this phone: it echoes what he said, in text rows and (for spoken says) in AVSpeechSynthesizer's voice through the same player
/// frames the real host sends, so listening, picking, saying, the rows, the audio and the reports all run without a pairing. The rows have the real
/// host's shapes: response_open, response_chunk*, response_emitted (response.cancelled when he cuts in), all carrying payload.turn_id.
@MainActor
final class LocalStubLink: VoiceLink {
    let events: AsyncStream<LinkEvent>
    private let out: AsyncStream<LinkEvent>.Continuation
    private var turns = 0
    private var openTurn: String?
    private var generation: Int64 = 0
    private var discardSeq: UInt64 = 0
    private var speaking: Task<Void, Never>?
    private var sentEnd: (gen: Int64, end: Int64)?          // the last sample of the generation being spoken, once all of it is sent

    init() { (events, out) = AsyncStream.makeStream(of: LinkEvent.self) }

    func connect() { out.yield(.ready(device: "phone", voice: true, sampleRate: kWireRate, judges: false)) }
    func disconnect() { stopSpeaking() }
    func barge() { stopSpeaking(); cancelOpen("barge_in") }
    func cancel() { stopSpeaking(); cancelOpen("user_stop") }
    /// Like the host: ACTIVE(-1) once the phone's reports show the whole generation played.
    func send(_ frames: [PlayerOut]) {
        guard let e = sentEnd else { return }
        for case .report(let r) in frames where r.generation == e.gen && r.end >= e.end {
            sentEnd = nil
            out.yield(.audio(.active(-1)))
            return
        }
    }

    func say(utteranceId: String, text: String, language: VoiceLang?, confidence: Double?, spoken: Bool, overHer: Bool, conversation: Bool,
             about: [String: Any]?, attachments: [String], continues: String?, timeout: Double) async throws -> SayResult {
        turns += 1
        let turn = "stub-\(turns)"
        let zh = language.map { $0 == .zh } ?? text.unicodeScalars.contains { $0.value >= 0x2E80 }
        let lead = zh ? "还没连上大脑。" : "Not connected to the brain yet. "
        let body = zh ? "你刚才说的是：\(text)" : "You said: \(text)"
        openTurn = turn
        // like the host: `said` first, the rows after it
        Task {
            try? await Task.sleep(nanoseconds: 50_000_000)
            guard openTurn == turn else { return }
            row("surface.response_open", ["turn_id": turn, "query": text, "kind": "answer"])
            row("surface.response_chunk", ["turn_id": turn, "text": lead, "sequence": 0, "phase": "final"])
            row("surface.response_chunk", ["turn_id": turn, "text": body, "sequence": 1, "phase": "final"])
            emitted(turn, lead + body)
            if spoken { speaking = Task { await speak(lead + body, zh: zh) } }
        }
        return (turn, "turn", nil)
    }

    private func row(_ type: String, _ payload: [String: Any]) {
        out.yield(.row(RowFrame(eventType: type, eventUid: UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(),
                                payload: payload, tsEpochMs: Int64(Date().timeIntervalSince1970 * 1000))))
    }

    /// The real host writes `surface.response_emitted` when the answer's text is done, which for speech is before the audio has played.
    private func emitted(_ turn: String, _ text: String) {
        guard openTurn == turn else { return }
        openTurn = nil
        row("surface.response_emitted", ["turn_id": turn, "text": text, "phase": "final"])
    }

    private func cancelOpen(_ reason: String) {
        guard let turn = openTurn else { return }
        openTurn = nil
        row("response.cancelled", ["turn_id": turn, "response_id": "stub", "response_group_id": "stub", "reason": reason])
    }

    private func stopSpeaking() {
        guard let t = speaking else { return }
        speaking = nil
        sentEnd = nil
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
        if !Task.isCancelled, generation == gen { sentEnd = (gen, Int64(x.count)) }
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

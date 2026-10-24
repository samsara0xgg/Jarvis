import AVFoundation
import Foundation

// ADR 0404: his words heard by Qwen in the cloud, through the brain, over the phone's line (/phone/ws). The frames are the brain's `hear` frames
// (jarvis/surface/terminal_hearing.py); the brain holds the key. The phone's own recognizer still runs for the wake word, the end of an utterance
// and her echo, and its words stand whenever Qwen's are missing. No UIKit, so the Mac harness compiles this file as it is.

/// The 16 kHz mono Int16 audio a `hear` stream carries.
let kHearFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: false)!

/// How long the commit waits for Qwen's final words before the phone's own stand: the brain's FINISH_TIMEOUT_S.
let kHearFinishWait = 4.0

enum HearFailure: Error { case timeout, failed, gone }

/// The brain's Qwen over the line: there while a connected line's host said `hear` in its `ready`.
@MainActor
final class CloudHearing {
    static let shared = CloudHearing()

    private weak var link: VoiceLink?
    private var streams: [String: Stream] = [:]
    private struct Stream {
        let onText: (String) -> Void
        var done: CheckedContinuation<String, Error>?
        var failed = false
    }

    var available: Bool { link != nil }

    /// A line's host answered `ready`: it hears, or it does not.
    func up(_ link: VoiceLink, hears: Bool) {
        if hears { self.link = link } else { down(link) }
    }

    /// The line `link` closed: every stream on it fails.
    func down(_ link: VoiceLink) {
        guard self.link === link else { return }
        self.link = nil
        let open = streams
        streams = [:]
        for s in open.values { s.done?.resume(throwing: HearFailure.gone) }
    }

    /// A new stream with these hot words; nil when no host hears. `onText` gets the words so far each time they change.
    func open(terms: [String], onText: @escaping (String) -> Void) -> String? {
        guard let link else { return nil }
        let sid = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        streams[sid] = Stream(onText: onText)
        link.sendFrame(.hear(sid: sid, op: "open", terms: terms))
        return sid
    }

    func send(_ sid: String, _ pcm: Data) {
        guard streams[sid] != nil else { return }
        link?.sendFrame(.hear(sid: sid, op: "audio", pcm: pcm))
    }

    /// The end of the stream's audio; its final words, within `timeout` seconds.
    func finish(_ sid: String, timeout: Double) async throws -> String {
        guard let s = streams[sid], !s.failed, let link else { streams[sid] = nil; throw HearFailure.failed }
        link.sendFrame(.hear(sid: sid, op: "finish"))
        return try await withCheckedThrowingContinuation { c in
            streams[sid]?.done = c
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1e9))
                self?.streams.removeValue(forKey: sid)?.done?.resume(throwing: HearFailure.timeout)
            }
        }
    }

    func abort(_ sid: String) {
        guard streams.removeValue(forKey: sid) != nil else { return }
        link?.sendFrame(.hear(sid: sid, op: "abort"))
    }

    /// One `hear` frame from the host.
    func receive(sid: String, op: String, text: String) {
        guard let s = streams[sid] else { return }
        switch op {
        case "text": s.onText(text)
        case "done": streams[sid] = nil; s.done?.resume(returning: text)
        case "failed":
            VoiceTrace.note("hear failed: \(text)")
            if let done = s.done { streams[sid] = nil; done.resume(throwing: HearFailure.failed) } else { streams[sid]?.failed = true }
        default: break
        }
    }
}

/// One utterance's audio on its way to Qwen. It is kept (the newest `keep` seconds) until `open`, which sends what was kept and streams the rest as it comes;
/// a stream opened while no host hears opens as soon as one does. `push` from any thread; `onWords` gets Qwen's words so far.
@MainActor
final class CloudStream {
    nonisolated private let input: AsyncStream<Data>.Continuation
    private var pump: Task<Void, Never>?
    private let keep: Int
    private var kept = Data()
    private var pending = Data()
    private var sid: String?
    private var wanted = false
    var onWords: ((String) -> Void)?

    private static let frameBytes = 3200          // 100 ms: what one `audio` frame carries, as the Mac's do

    init(keepSeconds: Double) {
        keep = Int(keepSeconds * 16_000) * 2
        let (audio, input) = AsyncStream.makeStream(of: Data.self)
        self.input = input
        pump = Task { [weak self] in
            for await chunk in audio { self?.take(chunk) }
            self?.flush()
        }
    }

    /// More of the utterance: 16 kHz mono Int16, little-endian.
    nonisolated func push(_ pcm: Data) { input.yield(pcm) }

    /// `last`: only that many seconds of what was kept go first (his words over her, not her voice before them, ADR 0408).
    func open(last seconds: Double? = nil) {
        if let seconds { kept = Data(kept.suffix(max(0, Int(seconds * 16_000)) * 2)) }
        wanted = true
        tryOpen()
    }

    private func take(_ chunk: Data) {
        guard sid != nil else {
            kept.append(chunk)
            if kept.count > keep { kept = Data(kept.suffix(keep)) }
            if wanted { tryOpen() }
            return
        }
        pending.append(chunk)
        if pending.count >= Self.frameBytes { flush() }
    }

    private func flush() {
        guard let sid, !pending.isEmpty else { return }
        CloudHearing.shared.send(sid, pending)
        pending = Data()
    }

    private func tryOpen() {
        guard sid == nil, let s = CloudHearing.shared.open(terms: kContextualNames, onText: { [weak self] w in self?.onWords?(w) }) else { return }
        sid = s
        for at in stride(from: kept.startIndex, to: kept.endIndex, by: Self.frameBytes) {
            CloudHearing.shared.send(s, Data(kept[at..<min(at + Self.frameBytes, kept.endIndex)]))
        }
        kept = Data()
    }

    /// Qwen's words for the whole utterance, or nil and why not, in the names the brain's `asr_engine` uses.
    func finish() async -> (words: String?, engine: String) {
        input.finish()
        await pump?.value                               // what was pushed has been sent
        guard let sid else { return (nil, "qwen_unavailable") }
        do {
            let words = try await CloudHearing.shared.finish(sid, timeout: kHearFinishWait).trimmingCharacters(in: .whitespacesAndNewlines)
            return words.isEmpty ? (nil, "qwen_empty") : (words, "qwen")
        } catch HearFailure.timeout {
            return (nil, "qwen_timeout")
        } catch {
            return (nil, "qwen_error")
        }
    }

    func cancel() {
        input.finish()
        pump?.cancel()
        if let sid { CloudHearing.shared.abort(sid) }
        sid = nil
    }
}

/// The microphone's buffers as `hear` audio: one converter, kept while the input format stays the same. The audio thread's only.
final class HearDownsampler {
    private var conv: AVAudioConverter?

    func pcm(_ b: AVAudioPCMBuffer) -> Data? {
        if conv?.inputFormat != b.format { conv = AVAudioConverter(from: b.format, to: kHearFormat) }
        guard let conv, let out = convert(b, using: conv), let samples = out.int16ChannelData else { return nil }
        return Data(bytes: samples[0], count: Int(out.frameLength) * 2)
    }
}

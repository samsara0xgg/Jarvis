import AVFoundation
import Foundation
import os
import Speech

// Two SpeechTranscribers (zh-CN and en-US) on the same audio; the more confident locale's text is the utterance.
// No UIKit, so the Mac harness compiles this file as it is.

let kContextualNames = ["Jarvis", "Claude Code", "Opus", "TimeSink", "co-op", "Pola", "Eva", "美光"]

/// One mono Float32 channel at the buffer's own rate, whatever the microphone hands over (voice processing may give several channels).
func monoFloat(_ b: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
    guard b.format.channelCount > 1 || b.format.commonFormat != .pcmFormatFloat32 || b.format.isInterleaved else { return b }
    guard let fmt = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: b.format.sampleRate, channels: 1, interleaved: false),
          let out = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: b.frameLength) else { return nil }
    out.frameLength = b.frameLength
    if let src = b.floatChannelData { out.floatChannelData![0].update(from: src[0], count: Int(b.frameLength)); return out }
    if let src = b.int16ChannelData {
        for i in 0..<Int(b.frameLength) { out.floatChannelData![0][i] = Float(src[0][i]) / 32768 }
        return out
    }
    return nil
}

func convert(_ buf: AVAudioPCMBuffer, using conv: AVAudioConverter) -> AVAudioPCMBuffer? {
    let fmt = conv.outputFormat
    let cap = AVAudioFrameCount(Double(buf.frameLength) * fmt.sampleRate / buf.format.sampleRate) + 64
    guard let out = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: cap) else { return nil }
    var fed = false
    var err: NSError?
    conv.convert(to: out, error: &err) { _, status in
        if fed { status.pointee = .noDataNow; return nil }
        fed = true
        status.pointee = .haveData
        return buf
    }
    return err == nil && out.frameLength > 0 ? out : nil
}

/// What a lane thinks so far, for the live caption: the same pick rule as the final.
/// `heard` is every lane's text, for spotting the wake word in whichever locale caught it.
struct Partial: Sendable { var lang: VoiceLang; var text: String; var confidence: Double; var heard = "" }

@available(iOS 26, macOS 26, *)
final class VoiceLane: @unchecked Sendable {
    let lang: VoiceLang
    private let transcriber: SpeechTranscriber
    private let analyzer: SpeechAnalyzer
    private let stream: AsyncStream<AnalyzerInput>
    private let cont: AsyncStream<AnalyzerInput>.Continuation
    // buffers that arrive before the analyzer knows its format wait here, in order
    private struct Gate { var format: AVAudioFormat?; var conv: AVAudioConverter?; var waiting: [AVAudioPCMBuffer] = [] }
    private let gate = OSAllocatedUnfairLock(initialState: Gate())
    private var collector: Task<LaneResult, Never>?
    private let onPartial: @Sendable (Partial) -> Void

    init(_ lang: VoiceLang, onPartial: @escaping @Sendable (Partial) -> Void) {
        self.lang = lang
        self.onPartial = onPartial
        transcriber = SpeechTranscriber(locale: lang.locale, transcriptionOptions: [], reportingOptions: [.volatileResults],
                                        attributeOptions: [.transcriptionConfidence])
        analyzer = SpeechAnalyzer(modules: [transcriber], options: .init(priority: .userInitiated, modelRetention: .processLifetime))
        (stream, cont) = AsyncStream.makeStream(of: AnalyzerInput.self)
    }

    func start() async throws {
        let ctx = AnalysisContext()
        ctx.contextualStrings[.general] = kContextualNames
        try await analyzer.setContext(ctx)
        let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber])
        let (t, lang, onPartial) = (transcriber, self.lang, self.onPartial)
        collector = Task {
            var text = "", conf: [Double] = []
            do {
                for try await r in t.results {
                    let runs = r.text.runs.compactMap { $0.transcriptionConfidence }
                    if r.isFinal {
                        text += String(r.text.characters); conf += runs
                        onPartial(Partial(lang: lang, text: text, confidence: meanConfidence(conf)))
                    } else {
                        onPartial(Partial(lang: lang, text: text + String(r.text.characters), confidence: meanConfidence(conf + runs)))
                    }
                }
            } catch {}
            return LaneResult(lang: lang, text: text, confidence: text.isEmpty ? 0 : meanConfidence(conf))
        }
        try await analyzer.start(inputSequence: stream)
        let waiting: [AVAudioPCMBuffer] = gate.withLock { g in
            g.format = format
            defer { g.waiting = [] }
            return g.waiting
        }
        for b in waiting { feed(b) }
    }

    private enum Route { case wait, direct, convert(AVAudioConverter) }

    /// Any thread. Buffers are the microphone's (mono Float32).
    func feed(_ b: AVAudioPCMBuffer) {
        let route: Route = gate.withLock { g in
            guard let format = g.format else { g.waiting.append(b); return .wait }
            if format == b.format { return .direct }
            if g.conv?.inputFormat != b.format { g.conv = AVAudioConverter(from: b.format, to: format) }
            return g.conv.map { .convert($0) } ?? .wait
        }
        switch route {
        case .wait: break
        case .direct: cont.yield(AnalyzerInput(buffer: b))
        case .convert(let c): if let o = convert(b, using: c) { cont.yield(AnalyzerInput(buffer: o)) }
        }
    }

    func finish() async -> LaneResult {
        cont.finish()
        try? await analyzer.finalizeAndFinishThroughEndOfInput()
        return await collector?.value ?? LaneResult(lang: lang)
    }

    func cancel() async {
        cont.finish()
        await analyzer.cancelAndFinishNow()
        collector?.cancel()
    }
}

/// One utterance on both locales. `feed` from any thread right away (audio before the analyzers are ready is kept), then `finish`.
@available(iOS 26, macOS 26, *)
final class DualRecognizer: @unchecked Sendable {
    private let lanes: [VoiceLane]
    private let startup: Task<Void, Error>
    /// Latest partial of each lane, most confident first; called off the main thread.
    private let partials = OSAllocatedUnfairLock(initialState: [VoiceLang: Partial]())

    init(onPartial: @escaping @Sendable (Partial) -> Void) {
        let store = partials
        lanes = VoiceLang.allCases.map { lang in
            VoiceLane(lang) { p in
                let best = store.withLock { s -> Partial in
                    s[p.lang] = p
                    var best = VoiceLang.allCases.compactMap { s[$0] }.reduce(p) { $1.confidence > $0.confidence ? $1 : $0 }
                    best.heard = s.values.map(\.text).joined(separator: " ")
                    return best
                }
                onPartial(best)
            }
        }
        let lanes = lanes
        startup = Task {
            try await withThrowingTaskGroup(of: Void.self) { g in
                for l in lanes { g.addTask { try await l.start() } }
                try await g.waitForAll()
            }
        }
    }

    func feed(_ b: AVAudioPCMBuffer) { for l in lanes { l.feed(b) } }

    /// The more confident lane's result, and both.
    func finish() async -> (picked: LaneResult, all: [LaneResult]) {
        _ = try? await startup.value
        var byLang: [VoiceLang: LaneResult] = [:]
        await withTaskGroup(of: LaneResult.self) { g in
            for l in lanes { g.addTask { await l.finish() } }
            for await r in g { byLang[r.lang] = r }
        }
        let all = VoiceLang.allCases.map { byLang[$0] ?? LaneResult(lang: $0) }       // zh first, so a tie picks zh
        return (all.dropFirst().reduce(all[0], { pickLane($0, $1) }), all)
    }

    func cancel() async {
        startup.cancel()
        for l in lanes { await l.cancel() }
    }

    /// Install the models for both locales (first run downloads them) and reserve them. `progress` is 0...1.
    static func ensureAssets(progress: @escaping @Sendable (Double) -> Void) async throws {
        for lang in VoiceLang.allCases {
            let t = SpeechTranscriber(locale: lang.locale, transcriptionOptions: [], reportingOptions: [], attributeOptions: [])
            if await AssetInventory.status(forModules: [t]) < .installed, let req = try await AssetInventory.assetInstallationRequest(supporting: [t]) {
                let poll = Task {
                    while !Task.isCancelled { progress(req.progress.fractionCompleted); try? await Task.sleep(nanoseconds: 250_000_000) }
                }
                defer { poll.cancel() }
                try await req.downloadAndInstall()
            }
            _ = try await AssetInventory.reserve(locale: lang.locale)
        }
        progress(1)
    }
}

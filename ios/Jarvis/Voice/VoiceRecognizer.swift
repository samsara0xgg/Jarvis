import AVFoundation
import Foundation
import os
import Speech

// Two SpeechTranscribers (zh-CN and en-US) on the same audio; the more confident locale's text is the utterance.
// No UIKit, so the Mac harness compiles this file as it is.

private let log = Logger(subsystem: "com.allen.jarvis", category: "voice")

/// After the end of the audio each lane gets this long to deliver its final text; a lane that misses it is cut off.
let kLaneDeadline = 1.5

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
    private struct Gate { var format: AVAudioFormat?; var conv: AVAudioConverter?; var waiting: [AVAudioPCMBuffer] = []; var dead = false }
    private let gate = OSAllocatedUnfairLock(initialState: Gate())
    private var collector: Task<LaneResult, Never>?
    /// What the collector has finalized so far, for a lane that is cut off at the deadline.
    private let got = OSAllocatedUnfairLock(initialState: (text: "", conf: [Double]()))
    private let onPartial: @Sendable (Partial) -> Void
    /// Test seam for the harness: make this lane fail to start, or never finish.
    enum Fault { case failStart, hangFinish }
    private let fault: Fault?

    init(_ lang: VoiceLang, locale: Locale, fault: Fault? = nil, onPartial: @escaping @Sendable (Partial) -> Void) {
        self.lang = lang
        self.fault = fault
        self.onPartial = onPartial
        transcriber = SpeechTranscriber(locale: locale, transcriptionOptions: [], reportingOptions: [.volatileResults],
                                        attributeOptions: [.transcriptionConfidence])
        analyzer = SpeechAnalyzer(modules: [transcriber], options: .init(priority: .userInitiated, modelRetention: .processLifetime))
        (stream, cont) = AsyncStream.makeStream(of: AnalyzerInput.self)
    }

    struct StartFault: Error {}

    func start() async throws {
        do {
            try await startAnalyzer()
        } catch {
            collector?.cancel()         // the results of an analyzer that never started would never end
            throw error
        }
    }

    private func startAnalyzer() async throws {
        let ctx = AnalysisContext()
        ctx.contextualStrings[.general] = kContextualNames
        try await analyzer.setContext(ctx)
        let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber])
        let (t, lang, onPartial, got) = (transcriber, self.lang, self.onPartial, self.got)
        collector = Task {
            var text = "", conf: [Double] = []
            do {
                for try await r in t.results {
                    let runs = r.text.runs.compactMap { $0.transcriptionConfidence }
                    if r.isFinal {
                        text += String(r.text.characters); conf += runs
                        let (t0, c0) = (text, conf); got.withLock { $0 = (t0, c0) }
                        onPartial(Partial(lang: lang, text: text, confidence: meanConfidence(conf)))
                    } else {
                        onPartial(Partial(lang: lang, text: text + String(r.text.characters), confidence: meanConfidence(conf + runs)))
                    }
                }
            } catch {}
            return LaneResult(lang: lang, text: text, confidence: text.isEmpty ? 0 : meanConfidence(conf))
        }
        if fault == .failStart { throw StartFault() }
        try await analyzer.start(inputSequence: stream)
        let waiting: [AVAudioPCMBuffer] = gate.withLock { g in
            g.format = format
            defer { g.waiting = [] }
            return g.waiting
        }
        for b in waiting { feed(b) }
    }

    /// The lane could not start: it drops audio from here on.
    func markDead() { gate.withLock { $0.dead = true; $0.waiting = [] } }
    var isDead: Bool { gate.withLock { $0.dead } }

    private enum Route { case wait, direct, convert(AVAudioConverter) }

    /// Any thread. Buffers are the microphone's (mono Float32).
    func feed(_ b: AVAudioPCMBuffer) {
        let route: Route = gate.withLock { g in
            if g.dead { return .wait }
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

    /// The lane's final text, within `deadline` seconds of now: a dead lane is empty at once, a late one gives what it had finalized.
    func finish(after start: Task<Void, Never>, deadline: Double) async -> LaneResult {
        let work = Task<LaneResult, Never> {
            await start.value                   // audio kept while starting is fed by now; only then is the input over
            self.cont.finish()
            if self.isDead { return LaneResult(lang: self.lang) }
            if self.fault == .hangFinish { try? await Task.sleep(nanoseconds: 3_600_000_000_000) }
            else { try? await self.analyzer.finalizeAndFinishThroughEndOfInput() }
            return await self.collector?.value ?? LaneResult(lang: self.lang)
        }
        let done: LaneResult? = await withCheckedContinuation { c in
            let once = OSAllocatedUnfairLock(initialState: false)
            let resume: @Sendable (LaneResult?) -> Void = { v in
                if once.withLock({ let was = $0; $0 = true; return !was }) { c.resume(returning: v) }
            }
            let timer = Task { try? await Task.sleep(nanoseconds: UInt64(deadline * 1e9)); resume(nil) }
            Task { resume(await work.value); timer.cancel() }
        }
        if let done { VoiceTrace.note("lane \(lang.rawValue) finished: \(done.text.count) chars, confidence \(done.confidence)"); return done }
        log.error("voice lane \(self.lang.rawValue) missed the \(deadline)s deadline; using what it had")
        VoiceTrace.note("lane \(lang.rawValue) missed the \(deadline)s deadline")
        work.cancel()
        Task { await self.cancel() }
        return got.withLock { LaneResult(lang: lang, text: $0.text, confidence: $0.text.isEmpty ? 0 : meanConfidence($0.conf)) }
    }

    func cancel() async {
        cont.finish()
        await analyzer.cancelAndFinishNow()
        collector?.cancel()
    }
}

/// One utterance on both locales. `feed` from any thread right away (audio before the analyzers are ready is kept), then `finish`.
/// A lane that cannot start is dropped (`onDead`, once per lane); the other keeps working. `both` is true when no lane is left.
@available(iOS 26, macOS 26, *)
final class DualRecognizer: @unchecked Sendable {
    private let lanes: [VoiceLane]
    private let starts: [Task<Void, Never>]
    /// Latest partial of each lane, most confident first; called off the main thread.
    private let partials = OSAllocatedUnfairLock(initialState: [VoiceLang: Partial]())
    /// The SpeechTranscriber locale each language resolved to in ensureAssets (the literal one until then).
    private static let locales = OSAllocatedUnfairLock(initialState: [VoiceLang: Locale]())

    init(faults: [VoiceLang: VoiceLane.Fault] = [:], onDead: @escaping @Sendable (_ lang: VoiceLang, _ both: Bool) -> Void = { _, _ in },
         onPartial: @escaping @Sendable (Partial) -> Void) {
        let store = partials
        let resolved = Self.locales.withLock { $0 }
        let lanes: [VoiceLane] = VoiceLang.allCases.map { lang in
            VoiceLane(lang, locale: resolved[lang] ?? lang.locale, fault: faults[lang]) { p in
                let best = store.withLock { s -> Partial in
                    s[p.lang] = p
                    // volatile results often carry no confidence: with nothing to compare, the lane with more text leads
                    var best = VoiceLang.allCases.compactMap { s[$0] }.reduce(p) {
                        $1.confidence > $0.confidence || ($1.confidence == 0 && $0.confidence == 0 && $1.text.count > $0.text.count) ? $1 : $0
                    }
                    best.heard = s.values.map(\.text).joined(separator: " ")
                    return best
                }
                onPartial(best)
            }
        }
        self.lanes = lanes
        starts = lanes.map { l in
            Task {
                do { try await l.start(); VoiceTrace.note("lane \(l.lang.rawValue) started") } catch {
                    l.markDead()
                    log.error("voice lane \(l.lang.rawValue) did not start: \(String(describing: error))")
                    VoiceTrace.note("lane \(l.lang.rawValue) did not start: \(String(describing: error))")
                    onDead(l.lang, lanes.allSatisfy(\.isDead))
                }
            }
        }
    }

    func feed(_ b: AVAudioPCMBuffer) { for l in lanes { l.feed(b) } }

    /// The more confident lane's result, and both. Returns within about `kLaneDeadline` of the call, whatever the lanes do.
    func finish() async -> (picked: LaneResult, all: [LaneResult]) {
        var byLang: [VoiceLang: LaneResult] = [:]
        await withTaskGroup(of: LaneResult.self) { g in
            for (l, s) in zip(lanes, starts) { g.addTask { await l.finish(after: s, deadline: kLaneDeadline) } }
            for await r in g { byLang[r.lang] = r }
        }
        let all = VoiceLang.allCases.map { byLang[$0] ?? LaneResult(lang: $0) }       // zh first, so a tie picks zh
        return (all.dropFirst().reduce(all[0], { pickLane($0, $1) }), all)
    }

    func cancel() async {
        for s in starts { s.cancel() }
        for l in lanes { await l.cancel() }
    }

    /// Install the models for both locales (first run downloads them) and reserve them. `progress` is 0...1.
    /// Returns the languages whose model is still not installed afterwards; throws if that is both.
    @discardableResult
    static func ensureAssets(progress: @escaping @Sendable (Double) -> Void) async throws -> [VoiceLang] {
        var missing: [VoiceLang] = []
        for lang in VoiceLang.allCases {
            let locale = await SpeechTranscriber.supportedLocale(equivalentTo: lang.locale) ?? lang.locale
            locales.withLock { $0[lang] = locale }
            let t = SpeechTranscriber(locale: locale, transcriptionOptions: [], reportingOptions: [], attributeOptions: [])
            do {
                if await AssetInventory.status(forModules: [t]) < .installed, let req = try await AssetInventory.assetInstallationRequest(supporting: [t]) {
                    let poll = Task {
                        while !Task.isCancelled { progress(req.progress.fractionCompleted); try? await Task.sleep(nanoseconds: 250_000_000) }
                    }
                    defer { poll.cancel() }
                    try await req.downloadAndInstall()
                }
                _ = try await AssetInventory.reserve(locale: locale)
            } catch {
                try Task.checkCancellation()
                log.error("voice assets for \(locale.identifier) failed: \(String(describing: error))")
            }
            let installed = await AssetInventory.status(forModules: [t]) == .installed
            VoiceTrace.note("assets \(locale.identifier): \(installed ? "installed" : "missing")")
            if !installed { missing.append(lang) }
        }
        progress(1)
        if missing.count == VoiceLang.allCases.count { throw NSError(domain: "Jarvis.voice", code: 1, userInfo: [NSLocalizedDescriptionKey: "中文和英文语音模型都没装好"]) }
        return missing
    }
}

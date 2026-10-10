import AVFoundation
import Observation
import os
import Speech
import UIKit

/// Talking to Jarvis: listen, pick the language, say it to the host, play her answer, listen again for a moment. The UI binds to this.
///
///     VoiceSession.shared.start()   // tap to talk (while she talks: interrupt her and listen)
///     VoiceSession.shared.stop()    // tap again: the utterance ends now (while she talks: cancel)
///     VoiceSession.shared.cancel()  // drop everything
///
/// Read `state`, `partial` (live words), `transcript` (what was said), `reply` (her words so far), `inputLevel` / `outputLevel` (0...1),
/// `language`, `errorText`, `modelProgress` (0...1 while the language models download on first use). Set `endpoint` once the pairing exists;
/// until then a local stub answers.
@MainActor @Observable
final class VoiceSession {
    enum State: Equatable { case idle, listening, thinking, speaking }

    static let shared = VoiceSession()

    private(set) var state = State.idle
    private(set) var errorText: String?
    private(set) var partial = ""
    private(set) var transcript = ""
    private(set) var reply = ""
    private(set) var inputLevel = 0.0
    private(set) var outputLevel = 0.0
    private(set) var language = VoiceLang.zh
    private(set) var modelProgress: Double?

    /// Listen for "Jarvis" / "贾维斯" while the app is open and nothing else is going on.
    var wakeWordInApp: Bool {
        get { wake }
        set { wake = newValue; refreshArming() }
    }
    private var wake = false

    /// Where the host is and the token that proves this phone; the pairing owns both.
    @ObservationIgnored var endpoint: () -> (base: URL, token: String)? = { nil }

    // MARK: timings

    private let silenceEnd = 0.8       // no new words and a quiet mic after speech began: the utterance is over
    private let noSpeech = 6.0         // nothing said this long after listening began: back to idle (also the follow-up window)
    private let utteranceCap = 30.0
    private let quiet = 0.3            // input level below this is quiet
    private let bargeLevel = 0.12      // words heard while she talks only count as him when the mic is at least this loud
    private let replyDry = 1.5         // her ring empty and nothing new this long: she is done
    private let noReply = 30.0
    private let wakeReset = 120.0      // an armed recognizer with no words is restarted this often

    // MARK: internals

    @ObservationIgnored private let audio = VoiceAudio()
    @ObservationIgnored private let feed = Feed()
    @ObservationIgnored private var rec: AnyObject?                 // a DualRecognizer (iOS 26)
    @ObservationIgnored private var segment = 0
    @ObservationIgnored private var segmentText = ""
    @ObservationIgnored private var segmentAt = 0.0
    @ObservationIgnored private var link: VoiceLink?
    @ObservationIgnored private var linkIsReal = false
    @ObservationIgnored private var linkTask: Task<Void, Never>?
    @ObservationIgnored private var tickTask: Task<Void, Never>?
    @ObservationIgnored private var assetsReady = false
    @ObservationIgnored private var preparing = false
    @ObservationIgnored private var foreground = true
    @ObservationIgnored private var rawLevel = 0.0
    @ObservationIgnored private var startedAt = 0.0
    @ObservationIgnored private var lastWordAt = 0.0
    @ObservationIgnored private var wakeTurn = false
    @ObservationIgnored private var sayAt = 0.0
    @ObservationIgnored private var lastRowAt = 0.0
    @ObservationIgnored private var playedAtSay: UInt64 = 0
    @ObservationIgnored private var replyStarted = false
    @ObservationIgnored private var currentTurn: String?
    @ObservationIgnored private var commandSaid: VoiceCommand?
    @ObservationIgnored private let log = Logger(subsystem: "com.allen.jarvis", category: "voice")

    private var now: Double { Double(uptimeNs()) / 1e9 }
    private var armed: Bool { wake && foreground && state == .idle }

    private init() {
        foreground = UIApplication.shared.applicationState != .background
        hop(UIApplication.didEnterBackgroundNotification) { $0.foreground = false; $0.refreshArming() }
        hop(UIApplication.willEnterForegroundNotification) { $0.foreground = true; $0.refreshArming() }
        hop(UIApplication.didBecomeActiveNotification) { if TalkRequest.pending { $0.requestTalk() } }
        hop(TalkRequest.notification) { $0.requestTalk() }
        audio.onMic = { [weak self, feed] buf, level in
            feed.feed(buf)
            Task { @MainActor in self?.rawLevel = level }
        }
        audio.onOut = { [weak self] frames in self?.link?.send(frames) }
        audio.onReset = { [weak self] interrupted in
            guard let self else { return }
            self.goIdle(releasing: true)
            if !interrupted { self.refreshArming() }
        }
        if TalkRequest.pending { requestTalk() }
    }

    private func hop(_ name: Notification.Name, _ body: @escaping @MainActor (VoiceSession) -> Void) {
        _ = NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { if let self { body(self) } }
        }
    }

    // MARK: the calls

    /// Start listening. Only once the app is on screen, so an intent that fires during launch waits for it.
    func requestTalk() {
        if UIApplication.shared.applicationState == .active { TalkRequest.pending = false; start() } else { TalkRequest.pending = true }
    }

    func start() {
        guard #available(iOS 26, *) else { errorText = "语音需要 iOS 26 或更新的系统"; return }
        errorText = nil
        switch state {
        case .listening: break
        case .thinking, .speaking: bargeIn()
        case .idle: Task { if await prepare(), state == .idle { enterListening(); startSegment() } }
        }
    }

    /// While he talks: the utterance ends now. While she talks or thinks: cancel.
    func stop() {
        switch state {
        case .listening: endUtterance()
        case .thinking, .speaking: cancel()
        case .idle: break
        }
    }

    func cancel() {
        if state == .thinking || state == .speaking { audio.core?.stopNow(); link?.cancel() }
        goIdle(releasing: false)
    }

    // MARK: getting ready

    /// Microphone, models, audio engine. False (with `errorText`) if any of them is not there.
    private func prepare() async -> Bool {
        guard #available(iOS 26, *) else { return false }
        if preparing { return false }
        preparing = true
        defer { preparing = false }
        if !(await AVAudioApplication.requestRecordPermission()) { errorText = "没有麦克风权限"; return false }
        if !assetsReady {
            guard SpeechTranscriber.isAvailable else { errorText = "这台设备不能在本地识别语音"; return false }
            modelProgress = 0
            defer { modelProgress = nil }
            do {
                try await DualRecognizer.ensureAssets { p in Task { @MainActor in VoiceSession.shared.modelProgress = p } }
                assetsReady = true
            } catch { errorText = "语言模型没装好：\(error.localizedDescription)"; return false }
        }
        do {
            try audio.start()
            if tickTask == nil { tickTask = Task { while !Task.isCancelled { tick(); try? await Task.sleep(nanoseconds: 33_000_000) } } }
        } catch { errorText = "打不开麦克风：\(error.localizedDescription)"; return false }
        return true
    }

    private func ensureLink() {
        let real = endpoint() != nil
        if let link, real == linkIsReal { link.connect(); return }
        linkTask?.cancel()
        link?.disconnect()
        let l: VoiceLink = real ? WebSocketVoiceLink(endpoint: endpoint) : LocalStubLink()
        link = l
        linkIsReal = real
        l.connect()
        linkTask = Task { for await e in l.events { handle(e) } }
    }

    /// Wake-word listening runs while the app is open and idle; otherwise an idle session lets go of the microphone.
    private func refreshArming() {
        guard #available(iOS 26, *) else { return }
        if armed {
            Task { if await prepare(), armed, rec == nil { startSegment() } }
        } else if state == .idle {
            release()
        }
    }

    private func release() {
        dropSegment()
        audio.stop()
        tickTask?.cancel(); tickTask = nil
        linkTask?.cancel(); linkTask = nil
        link?.disconnect(); link = nil
        inputLevel = 0; outputLevel = 0
    }

    // MARK: segments: one recognizer per stretch of audio

    @available(iOS 26, *)
    private func startSegment() {
        guard audio.running else { return }
        segment += 1
        let id = segment
        let old = rec as? DualRecognizer
        let r = DualRecognizer { [weak self] p in Task { @MainActor in self?.heard(p, id) } }
        rec = r
        feed.set(r)
        if let old { Task { await old.cancel() } }
        segmentText = ""
        segmentAt = now
        lastWordAt = now
        partial = ""
    }

    private func dropSegment() {
        segment += 1
        feed.set(nil)
        if #available(iOS 26, *), let old = rec as? DualRecognizer { Task { await old.cancel() } }
        rec = nil
        segmentText = ""
        partial = ""
    }

    /// Words from the recognizer, live.
    private func heard(_ p: Partial, _ id: Int) {
        guard id == segment, !p.text.isEmpty else { return }
        if p.text != segmentText { lastWordAt = now }
        segmentText = p.text
        switch state {
        case .listening: partial = p.text; language = p.lang
        case .idle: if armed, heardWake(p.heard) { wakeTurn = true; language = p.lang; enterListening(); partial = p.text }
        case .thinking, .speaking: if rawLevel >= bargeLevel { bargeIn() }
        }
    }

    // MARK: turns

    private func enterListening() {
        set(.listening)
        startedAt = now
        lastWordAt = now
        errorText = nil
        transcript = ""
    }

    /// He spoke over her (or tapped): stop her audio here at once, tell the host, listen.
    private func bargeIn() {
        audio.core?.stopNow()
        link?.barge()
        currentTurn = nil
        wakeTurn = false
        enterListening()
        if #available(iOS 26, *), rec == nil { startSegment() }
        partial = segmentText
    }

    private func endUtterance() {
        guard #available(iOS 26, *), state == .listening, let r = rec as? DualRecognizer else { return }
        let wakeTurn = wakeTurn
        self.wakeTurn = false
        set(.thinking)
        sayAt = now
        rec = nil
        feed.set(nil)
        Task {
            let (picked, _) = await r.finish()
            gotFinal(picked, wakeTurn: wakeTurn)
        }
    }

    @available(iOS 26, *)
    private func gotFinal(_ picked: LaneResult, wakeTurn: Bool) {
        guard state == .thinking else { return }            // cancelled meanwhile
        let text = (wakeTurn ? stripWake(picked.text) : picked.text).trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty {
            if wakeTurn { enterListening(); startSegment() }  // only the wake word: now the request
            else { goIdle(releasing: false) }
            return
        }
        language = picked.lang
        transcript = text
        partial = ""
        reply = ""
        replyStarted = false
        lastRowAt = 0
        commandSaid = shortCommand(text)
        if commandSaid == .stop || commandSaid == .wait { audio.core?.stopNow(); link?.cancel() }
        playedAtSay = audio.core?.playedSamples ?? 0
        sayAt = now
        ensureLink()
        startSegment()                                      // listening for him to interrupt
        let lang = picked.lang
        Task {
            do {
                currentTurn = try await link?.say(utteranceId: UUID().uuidString, text: text, language: lang, spoken: true)
            } catch { fail(error.localizedDescription) }
        }
    }

    private func replyFinished() {
        if commandSaid == .stop || commandSaid == .wait || commandSaid == .goodnight { goIdle(releasing: false); return }
        // a short window to answer without tapping; its own silence timer ends it
        currentTurn = nil
        enterListening()
        if #available(iOS 26, *) { startSegment() }
    }

    private func fail(_ message: String) {
        errorText = message
        goIdle(releasing: false)
    }

    private func goIdle(releasing: Bool) {
        audio.core?.stopNow()
        set(.idle)
        wakeTurn = false
        currentTurn = nil
        dropSegment()
        if releasing { release() } else { refreshArming() }
    }

    private func set(_ s: State) {
        guard s != state else { return }
        log.debug("voice \(String(describing: self.state)) -> \(String(describing: s))")
        state = s
    }

    // MARK: the host

    private func handle(_ e: LinkEvent) {
        switch e {
        case .ready:
            if let core = audio.core { link?.send([core.readyFrame(bufferFrames: audio.bufferFrames)]) }
        case .row(let r):
            guard r.role == nil || r.role == "assistant", let t = r.text,
                  currentTurn == nil || r.turnId == nil || r.turnId == currentTurn, state == .thinking || state == .speaking else { return }
            reply += t
            lastRowAt = now
            replyStarted = true
        case .audio(let f):
            audio.core?.ingest(f)
            if case .pcm = f { replyStarted = true }
        case .error(let m): errorText = m
        case .unpaired: fail("还没有和 Jarvis 配对")
        case .pong, .closed: break
        }
    }

    // MARK: the clock

    private func tick() {
        let t = now
        inputLevel = max(rawLevel, inputLevel * 0.85)
        outputLevel = Double(min(1, (audio.core?.level ?? 0) * 4))
        switch state {
        case .listening:
            let age = t - startedAt
            if !segmentText.isEmpty, t - lastWordAt > silenceEnd, inputLevel < quiet { endUtterance() }
            else if segmentText.isEmpty, age > noSpeech { goIdle(releasing: false) }
            else if age > utteranceCap { endUtterance() }
        case .thinking, .speaking:
            guard let core = audio.core else { return }
            if state == .thinking, core.playedSamples > playedAtSay + 480 { set(.speaking) }
            let quietAfter = commandSaid == .stop || commandSaid == .wait || commandSaid == .goodnight
            if replyStarted {
                if core.ringEmpty, t - max(Double(core.lastAudioNs) / 1e9, lastRowAt) > replyDry { replyFinished() }
            } else if t - sayAt > (quietAfter ? 3 : noReply) {
                if quietAfter { goIdle(releasing: false) } else { fail("Jarvis 没有回应") }
            }
        case .idle:
            // armed: a phrase that is not the wake word is forgotten, and a long silence restarts the recognizer
            if #available(iOS 26, *), armed, rec != nil {
                if !segmentText.isEmpty, t - lastWordAt > silenceEnd, inputLevel < quiet { startSegment() }
                else if segmentText.isEmpty, t - segmentAt > wakeReset { startSegment() }
            }
        }
    }
}

/// Hands the microphone's buffers to the current recognizer from the audio thread.
private final class Feed: @unchecked Sendable {
    private let lock = OSAllocatedUnfairLock<AnyObject?>(initialState: nil)
    func set(_ r: AnyObject?) { lock.withLock { $0 = r } }
    func feed(_ b: AVAudioPCMBuffer) {
        if #available(iOS 26, *) { (lock.withLock { $0 } as? DualRecognizer)?.feed(b) }
    }
}

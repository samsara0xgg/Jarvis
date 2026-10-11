import AVFoundation

/// The one AVAudioEngine: microphone in and her voice out. Both go through the voice-processing unit so its echo canceller hears
/// what she plays and he can talk over her. Speaker by default.
final class VoiceAudio {
    private(set) var core: PlayerCore?
    private var source: AVAudioSourceNode?
    private var tapFormat: AVAudioFormat?
    private var engine = AVAudioEngine()
    private var timer: DispatchSourceTimer?
    private var observers: [NSObjectProtocol] = []
    private(set) var running = false
    /// stopping the engine and handing the session back can block for a second (AirPods leaving the call route), so it runs here, off the main thread
    private let sessionQueue = DispatchQueue(label: "jarvis.voice.session")
    /// mono Float32 microphone buffers and their level 0...1; off the main thread
    var onMic: (@Sendable (AVAudioPCMBuffer, Double) -> Void)?
    /// what the player owes the host, in order; on the main queue
    var onOut: (@MainActor ([PlayerOut]) -> Void)?
    /// the engine was torn down by the system (an interruption began or ended, or the hardware changed); `interrupted` while it lasts
    var onReset: (@MainActor (_ interrupted: Bool) -> Void)?

    var bufferFrames: Int { Int(AVAudioSession.sharedInstance().ioBufferDuration * Double(core?.sampleRate ?? 48_000)) }

    /// The microphone had no usable format within 3 s (a headset switching over, another app holding it).
    struct NotReady: Error {}

    init() {
        // what the session went through while the voice was not using it, for when the microphone comes up dead
        for n in [AVAudioSession.mediaServicesWereResetNotification, AVAudioSession.interruptionNotification] {
            NotificationCenter.default.addObserver(forName: n, object: nil, queue: nil) { VoiceTrace.note("session: \($0.name.rawValue) \($0.userInfo ?? [:])") }
        }
    }

    func start() async throws {
        guard !running else { return }
        await withCheckedContinuation { c in sessionQueue.async { c.resume() } }   // a stop still letting go of the session finishes first
        Sound.shared.suspend()                             // one owner of the session: the UI cues' engine is stopped first
        var started = false
        defer { if !started { Sound.shared.resume() } }
        let s = AVAudioSession.sharedInstance()
        try s.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker, .allowBluetoothHFP])
        try s.setActive(true)
        if s.category != .playAndRecord {
            VoiceTrace.note("category is \(s.category.rawValue) after setting playAndRecord; setting it again")
            try s.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker, .allowBluetoothHFP])
        }
        // A fresh engine each time, built on the session as it is now: one built while the route was still settling (just after
        // launch, with navigation holding the audio, and the first one after the session goes active) kept a 0 Hz input that never
        // recovered, while one built a moment later had 48 kHz at once (phone, 12:29 PT: 1 s lost on the dead one). So a dead one is
        // built again after 0.3 s, for about 3 s in all. installTap with a 0 Hz or 0-channel format raises an exception Swift cannot
        // catch, hence the wait.
        var inFmt = AVAudioFormat()
        ready: for build in 0..<8 {
            engine = AVAudioEngine()
            try engine.inputNode.setVoiceProcessingEnabled(true)   // changes the input format: read it only after this
            for attempt in 0...3 {
                inFmt = engine.inputNode.outputFormat(forBus: 0)
                let ok = s.isInputAvailable && inFmt.sampleRate > 0 && inFmt.channelCount > 0
                if ok || (attempt == 0 && build == 0) || (attempt == 3 && build == 7) {
                    let r = s.currentRoute
                    VoiceTrace.note("mic engine \(build) attempt \(attempt): input available \(s.isInputAvailable), \(Int(inFmt.sampleRate)) Hz \(inFmt.channelCount) ch, "
                        + "in \(r.inputs.map { "\($0.portType.rawValue) \($0.portName)" }), out \(r.outputs.map(\.portType.rawValue)), "
                        + "\(s.category.rawValue)/\(s.mode.rawValue)")
                }
                if ok { break ready }
                if attempt < 3 { try await Task.sleep(nanoseconds: 100_000_000) }
            }
            if build == 7 { throw NotReady() }
        }
        let input = engine.inputNode
        let rate = engine.outputNode.outputFormat(forBus: 0).sampleRate
        let fmt = AVAudioFormat(standardFormatWithSampleRate: rate > 0 ? rate : 48_000, channels: 1)!
        let core = PlayerCore(sampleRate: Int(fmt.sampleRate))
        core.deviceLatencyNs = Self.latencyNs(s)
        let src = AVAudioSourceNode(format: fmt) { _, ts, frames, abl in
            guard let p = UnsafeMutableAudioBufferListPointer(abl)[0].mData?.assumingMemoryBound(to: Float.self) else { return noErr }
            // time until this block's first sample leaves the speaker
            core.render(p, frames: Int(frames), delayNs: max(0, hostTimeToNs(ts.pointee.mHostTime) - uptimeNs()) + core.deviceLatencyNs)
            return noErr
        }
        engine.attach(src)
        source = src
        engine.connect(src, to: engine.mainMixerNode, format: fmt)
        tapFormat = inFmt
        input.installTap(onBus: 0, bufferSize: 2048, format: inFmt) { [weak self] buf, _ in
            guard let mono = monoFloat(buf) else { return }
            self?.onMic?(mono, Self.level(mono))
        }
        engine.prepare()
        try engine.start()
        self.core = core
        running = true
        started = true

        let t = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "jarvis.voice.drain", qos: .userInteractive))
        t.schedule(deadline: .now(), repeating: .milliseconds(5), leeway: .milliseconds(2))
        t.setEventHandler { [weak self] in
            guard let f = self?.core?.drain(), !f.isEmpty else { return }
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.onOut?(f) } }
        }
        t.resume()
        timer = t
        observe()
    }

    func stop() {
        guard running else { return }
        running = false
        timer?.cancel(); timer = nil
        observers.forEach(NotificationCenter.default.removeObserver); observers = []
        let engine = engine, source = source
        self.source = nil
        core = nil
        sessionQueue.async {                                // taps froze for a moment after each talk ended with AirPods on (live 2026-10-10)
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
            if let source { engine.detach(source) }
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            Sound.shared.resume()
        }
    }

    private func observe() {
        let nc = NotificationCenter.default
        observers = [
            nc.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] n in
                let began = (n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt) == AVAudioSession.InterruptionType.began.rawValue
                MainActor.assumeIsolated { self?.teardown(interrupted: began) }
            },
            // the hardware changed and the engine stopped itself. One comes right after every start, as voice processing settles:
            // with the microphone's format unchanged the graph is still good and is started again; otherwise it is stale and goes.
            // Restarting can block like stopping does, so it runs on the session queue, after any stop already queued there.
            nc.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { [weak self, engine, tapFormat, sessionQueue] _ in
                sessionQueue.async {
                    let f = engine.inputNode.outputFormat(forBus: 0)
                    if f.sampleRate == tapFormat?.sampleRate, f.channelCount == tapFormat?.channelCount, (try? engine.start()) != nil {
                        VoiceTrace.note("engine configuration changed: started again at \(Int(f.sampleRate)) Hz")
                        return
                    }
                    VoiceTrace.note("engine configuration changed: microphone now \(Int(f.sampleRate)) Hz \(f.channelCount) ch, stopping")
                    DispatchQueue.main.async { MainActor.assumeIsolated { if let self, self.engine === engine { self.teardown(interrupted: false) } } }
                }
            },
            nc.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] n in
                guard let self, let core else { return }
                core.deviceLatencyNs = Self.latencyNs(.sharedInstance())
                // headphones pulled out: stop talking, as every audio app does
                let why = (n.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt).flatMap(AVAudioSession.RouteChangeReason.init)
                if why == .oldDeviceUnavailable { core.stopNow() }
            },
        ]
    }

    @MainActor private func teardown(interrupted: Bool) {
        stop()
        onReset?(interrupted)
    }

    private static func latencyNs(_ s: AVAudioSession) -> Int64 { Int64((s.outputLatency + s.ioBufferDuration) * 1e9) }

    /// RMS in dB mapped from -55...-10 to 0...1
    private static func level(_ b: AVAudioPCMBuffer) -> Double {
        guard let x = b.floatChannelData?[0], b.frameLength > 0 else { return 0 }
        var sum: Float = 0
        for i in 0..<Int(b.frameLength) { sum += x[i] * x[i] }
        let db = 20 * log10(max((sum / Float(b.frameLength)).squareRoot(), 1e-5))
        return min(1, max(0, (Double(db) + 55) / 45))
    }
}

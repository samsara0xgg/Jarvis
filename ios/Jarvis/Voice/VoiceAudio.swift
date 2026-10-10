import AVFoundation

/// The one AVAudioEngine: microphone in and her voice out. Both go through the voice-processing unit so its echo canceller hears
/// what she plays and he can talk over her. Speaker by default.
final class VoiceAudio {
    private(set) var core: PlayerCore?
    private var source: AVAudioSourceNode?
    private let engine = AVAudioEngine()
    private var timer: DispatchSourceTimer?
    private var observers: [NSObjectProtocol] = []
    private(set) var running = false
    /// mono Float32 microphone buffers and their level 0...1; off the main thread
    var onMic: (@Sendable (AVAudioPCMBuffer, Double) -> Void)?
    /// what the player owes the host, in order; on the main queue
    var onOut: (@MainActor ([PlayerOut]) -> Void)?
    /// the engine was torn down by the system (an interruption began or ended, or the hardware changed); `interrupted` while it lasts
    var onReset: (@MainActor (_ interrupted: Bool) -> Void)?

    var bufferFrames: Int { Int(AVAudioSession.sharedInstance().ioBufferDuration * Double(core?.sampleRate ?? 48_000)) }

    /// The microphone had no usable format within a second (a headset switching over, another app holding it).
    struct NotReady: Error {}

    func start() async throws {
        guard !running else { return }
        let s = AVAudioSession.sharedInstance()
        try s.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker, .allowBluetoothHFP])
        try s.setActive(true)
        let input = engine.inputNode
        try input.setVoiceProcessingEnabled(true)          // changes the input format: read it only after this
        // installTap with a 0 Hz or 0-channel format raises an exception Swift cannot catch: wait up to 1 s for the route instead
        var inFmt = input.outputFormat(forBus: 0)
        for attempt in 0...10 {
            let r = s.currentRoute
            VoiceTrace.note("mic attempt \(attempt): input available \(s.isInputAvailable), \(Int(inFmt.sampleRate)) Hz \(inFmt.channelCount) ch, "
                + "in \(r.inputs.map { "\($0.portType.rawValue) \($0.portName)" }), out \(r.outputs.map(\.portType.rawValue)), "
                + "\(s.category.rawValue)/\(s.mode.rawValue)")
            if s.isInputAvailable && inFmt.sampleRate > 0 && inFmt.channelCount > 0 { break }
            if attempt == 10 { throw NotReady() }
            try await Task.sleep(nanoseconds: 100_000_000)
            inFmt = input.outputFormat(forBus: 0)
        }
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
        input.installTap(onBus: 0, bufferSize: 2048, format: inFmt) { [weak self] buf, _ in
            guard let mono = monoFloat(buf) else { return }
            self?.onMic?(mono, Self.level(mono))
        }
        engine.prepare()
        try engine.start()
        self.core = core
        running = true

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
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        if let source { engine.detach(source) }
        source = nil
        core = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func observe() {
        let nc = NotificationCenter.default
        observers = [
            nc.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] n in
                let began = (n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt) == AVAudioSession.InterruptionType.began.rawValue
                MainActor.assumeIsolated { self?.teardown(interrupted: began) }
            },
            // the hardware changed (route, sample rate): the graph is stale, rebuild it
            nc.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.teardown(interrupted: false) }
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

import AVFoundation
import os

enum SoundCue: String, CaseIterable {
    case listen, heard, think, turn, interrupt, end, done, ask, remind, msg, hello, bye, happy
    case open, close, module, back, send, on, off, tick, tock
    case voiceEnter, voiceExit, mic, speakerOn          // the Mac's (SoundFeedback)
}

/// UI sounds: the desktop's FM "glass" cues, rendered offline once (see `SoundSynth`) and played through a
/// small pool of player nodes. The engine only runs while something is sounding (plus 3 s).
/// `play` is cheap and safe from any thread; all audio work happens on a private serial queue.
final class Sound {
    static let shared = Sound()

    private static let key = "jv-snd"
    private static let voiceCount = 4
    private static let idleSeconds = 3.0
    private let on = OSAllocatedUnfairLock(initialState: UserDefaults.standard.object(forKey: Sound.key) as? Bool ?? true)

    /// Master switch, persisted under the prototype's key `jv-snd`; default on.
    var isOn: Bool {
        get { on.withLock { $0 } }
        set {
            on.withLock { $0 = newValue }
            UserDefaults.standard.set(newValue, forKey: Sound.key)
        }
    }

    /// The cues' format, also the one a node elsewhere that plays them (`route`) is connected with.
    static let cueFormat = AVAudioFormat(standardFormatWithSampleRate: SoundSynth.sampleRate, channels: 2)!

    private let format = Sound.cueFormat
    // Rendered buffers are immutable once stored, so the pre-render thread and the audio queue can share them.
    private let buffers = OSAllocatedUnfairLock(initialState: [SoundCue: AVAudioPCMBuffer]())
    private let warmUp = DispatchQueue(label: "jarvis.sound.render", qos: .utility)

    // Everything below is touched on `queue` only.
    private let queue = DispatchQueue(label: "jarvis.sound", qos: .userInitiated)
    private var warmedUp = false
    private var engine: AVAudioEngine?
    private var voices: [AVAudioPlayerNode] = []
    private var busyUntil: [TimeInterval] = []
    private var idleStop: DispatchWorkItem?
    private var configObserver: NSObjectProtocol?
    private var suspended = false
    private var routed: AVAudioPlayerNode?
    private var held: HeldCue?

    private init() {
        // An interruption or media-services reset invalidates the engine; dropping it is enough,
        // the next `play` builds a fresh one.
        let nc = NotificationCenter.default
        nc.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: nil) { [weak self] _ in
            self?.queue.async { self?.teardown() }
        }
        nc.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: nil) { [weak self] note in
            guard note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt == AVAudioSession.InterruptionType.began.rawValue else { return }
            self?.queue.async { self?.teardown() }
        }
    }

    /// Plays a cue. `gain` is a linear volume, clamped to 0...1.
    func play(_ cue: SoundCue, gain: Float = 1) {
        guard isOn else { return }
        queue.async {
            if let node = self.routed { self.start(cue, gain: gain, on: node) }
            else if !self.suspended { self.start(cue, gain: gain) }
            else { self.held = HeldCue(cue: cue, gain: gain, at: ProcessInfo.processInfo.systemUptime) }
        }
    }

    /// The voice owns the audio session while it listens or speaks: no cue may start a second engine or touch the
    /// category meanwhile (a second running engine left the voice's microphone with no usable format).
    /// Blocks until this engine is gone. `resume` when the voice has let the session go: a cue asked for in its last 0.3 s plays then (`HeldCue`).
    func suspend() { queue.sync { suspended = true; teardown() } }
    func resume() {
        queue.async {
            self.suspended = false
            if let h = self.held, h.due(ProcessInfo.processInfo.systemUptime) { self.start(h.cue, gain: h.gain) }
            self.held = nil
        }
    }

    /// While the voice's engine runs (VoiceAudio), cues play through its node, connected with `cueFormat`: its echo canceller hears them
    /// and no second engine starts. Nil gives them back to this engine. Blocks, so no cue is scheduled on a node being let go.
    func route(through node: AVAudioPlayerNode?) { queue.sync { routed = node } }

    /// a cue through the voice's node; dropped when that engine is not running (a node plays only in a running engine)
    private func start(_ cue: SoundCue, gain: Float, on node: AVAudioPlayerNode) {
        guard node.engine?.isRunning == true else { return }
        node.volume = min(max(gain, 0), 1)
        node.scheduleBuffer(buffer(for: cue), at: nil, options: .interrupts)
        if !node.isPlaying { node.play() }
    }

    private func start(_ cue: SoundCue, gain: Float) {
        let buffer = self.buffer(for: cue)
        if !warmedUp {
            // Render the rest on their own queue, so plays never wait behind the pre-rendering.
            warmedUp = true
            warmUp.async { for other in SoundCue.allCases { _ = self.buffer(for: other) } }
        }
        if LifelogMic.play(buffer, gain: gain) { return }   // the day's recording holds the session: the cue goes through its engine
        guard prepareEngine() else { return }
        let now = ProcessInfo.processInfo.systemUptime
        // A free voice, else the one closest to finishing its tail (a voice is cut, never stacked beyond four).
        let i = busyUntil.firstIndex { $0 <= now } ?? busyUntil.indices.min { busyUntil[$0] < busyUntil[$1] }!
        voices[i].volume = min(max(gain, 0), 1)
        voices[i].scheduleBuffer(buffer, at: nil, options: .interrupts)
        voices[i].play()
        busyUntil[i] = now + Double(buffer.frameLength) / format.sampleRate
        armIdleStop(after: busyUntil.max()! - now + Sound.idleSeconds)
    }

    private func buffer(for cue: SoundCue) -> AVAudioPCMBuffer {
        let fresh = cue == .mic || cue == .speakerOn        // the Mac's clicks are new noise at a new pitch each time
        if !fresh, let b = buffers.withLock({ $0[cue] }) { return b }
        let samples = SoundSynth.render(cue)
        let b = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples[0].count))!
        b.frameLength = b.frameCapacity
        for c in 0..<2 { samples[c].withUnsafeBufferPointer { b.floatChannelData![c].update(from: $0.baseAddress!, count: samples[c].count) } }
        if !fresh { buffers.withLock { $0[cue] = b } }
        return b
    }

    private func prepareEngine() -> Bool {
        if engine?.isRunning == true { return true }
        if engine == nil {
            let session = AVAudioSession.sharedInstance()
            // .ambient follows the silent switch and mixes with other audio. The voice leaves its playAndRecord/voiceChat category behind
            // when it lets the session go; a cue activating that again took the audio from a phone call he was on. The voice suspends
            // the cues while it owns the session, so here the category is always the cues' own.
            if session.category != .ambient { try? session.setCategory(.ambient) }
            try? session.setActive(true)
            let e = AVAudioEngine()
            // The engine stops itself on a route or format change; rebuild on the next play.
            configObserver = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: e, queue: nil) { [weak self] _ in
                self?.queue.async { self?.teardown() }
            }
            for _ in 0..<Sound.voiceCount {
                let p = AVAudioPlayerNode()
                e.attach(p)
                e.connect(p, to: e.mainMixerNode, format: format)
                voices.append(p)
            }
            busyUntil = Array(repeating: 0, count: Sound.voiceCount)
            engine = e
        }
        do {
            try engine!.start()
            return true
        } catch {
            teardown()
            return false
        }
    }

    private func armIdleStop(after seconds: TimeInterval) {
        idleStop?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.engine?.stop() }
        idleStop = work
        queue.asyncAfter(deadline: .now() + seconds, execute: work)
    }

    private func teardown() {
        idleStop?.cancel()
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        configObserver = nil
        engine?.stop()
        engine = nil
        voices = []
        busyUntil = []
    }
}

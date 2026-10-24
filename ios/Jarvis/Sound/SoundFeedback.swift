import AVFoundation

/// The Mac's voice and toggle cues (desktop/resonance/src/feedback.ts), as mono samples at `SoundSynth.sampleRate` for `SoundSynth.render`:
/// voice-enter is its rendered wav, voice-exit that wav backwards, the toggles Hermes' noise-burst click patterns.
enum SoundFeedback {
    /// what the Mac plays them at (its feedbackVolume default)
    static let gain: Float = 0.35

    /// voice-enter.wav (48 kHz on the Mac), resampled to the cues' rate
    static let enter: [Float] = load()

    /// Leaving voice is the enter cue backwards, as on the Mac: its swell is the enter's decay, its release the enter's attack. The first
    /// 0.12 s of the reversed wav is the enter's own faded-out tail, so it is dropped.
    static let exitSkip = 0.12
    static let exit: [Float] = reversed(enter)
    static func reversed(_ x: [Float]) -> [Float] {
        let n = x.count - Int((SoundSynth.sampleRate * exitSkip).rounded())
        return Array(x[0..<n].reversed())
    }

    private static func load() -> [Float] {
        let rate = SoundSynth.sampleRate, mono = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1)!
        guard let url = Bundle.main.url(forResource: "voice-enter", withExtension: "wav"),
              let file = try? AVAudioFile(forReading: url),
              let input = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length)),
              (try? file.read(into: input)) != nil,
              let converter = AVAudioConverter(from: file.processingFormat, to: mono),
              let out = AVAudioPCMBuffer(pcmFormat: mono, frameCapacity: AVAudioFrameCount(Double(input.frameLength) * rate / file.processingFormat.sampleRate) + 1024)
        else { return [0] }
        var fed = false
        converter.convert(to: out, error: nil) { _, status in
            if fed { status.pointee = .endOfStream; return nil }
            fed = true; status.pointee = .haveData; return input
        }
        return Array(UnsafeBufferPointer(start: out.floatChannelData![0], count: Int(out.frameLength)))
    }

    // MARK: the toggles' clicks

    /// (ms after the previous pulse, ms long, intensity): mic-on and mic-off are Hermes' `selection`, one click; speaker-on its `open`,
    /// a pair that rises into the second click
    private typealias Pulse = (delay: Double, duration: Double, intensity: Double)
    private static func pattern(_ cue: SoundCue) -> [Pulse] {
        cue == .speakerOn ? [(0, 18, 0.42), (36, 22, 0.66)] : [(0, 16, 0.52)]
    }

    /// Every click of a pattern (web-haptics 0.0.6's engine, as the Mac ports it), new noise at a new pitch each time: an 8 ms noise burst
    /// decaying over 1.5 ms, through its own bandpass (Q 8) at (2000 + 2000 x intensity) Hz +-15 %, at 3 x intensity. Inside a pulse a click
    /// comes every 16 + (1 - intensity) x 184 ms, which at these lengths is one per pulse.
    static func clicks(_ cue: SoundCue) -> [Float] {
        let sr = SoundSynth.sampleRate
        var hits: [(ms: Double, intensity: Double)] = []
        var at = 0.0
        for p in pattern(cue) {
            at += p.delay
            let step = 16 + (1 - p.intensity) * 184
            for offset in stride(from: 0, through: p.duration, by: step) { hits.append((at + offset, p.intensity)) }
            at += p.duration
        }
        // the filter rings on after its 8 ms of noise (Web Audio runs it until it is silent): 20 ms more, a few dozen of its time constants
        let burst = max(1, Int((sr * 0.008).rounded())), ring = burst + Int(sr * 0.02), decay = sr * 0.0015
        var out = [Float](repeating: 0, count: Int((hits.last!.ms / 1000 * sr).rounded()) + ring)
        for h in hits {
            let f0 = (2000 + h.intensity * 2000) * (1 + (Double.random(in: 0..<1) - 0.5) * 0.3)
            // Web Audio's bandpass biquad (0 dB at the centre): b = (alpha, 0, -alpha), a = (1 + alpha, -2 cos w0, 1 - alpha), alpha = sin w0 / 2Q
            let w0 = 2 * Double.pi * f0 / sr, alpha = sin(w0) / 16, a0 = 1 + alpha
            let b0 = alpha / a0, a1 = -2 * cos(w0) / a0, a2 = (1 - alpha) / a0
            let g = 3 * h.intensity, start = Int((h.ms / 1000 * sr).rounded())
            var x1 = 0.0, x2 = 0.0, y1 = 0.0, y2 = 0.0
            for i in 0..<ring {
                let x = i < burst ? (Double.random(in: 0..<1) * 2 - 1) * exp(-Double(i) / decay) : 0
                let y = b0 * x - b0 * x2 - a1 * y1 - a2 * y2
                x2 = x1; x1 = x; y2 = y1; y1 = y
                out[start + i] += Float(g * y)
            }
        }
        return out
    }
}

/// A cue asked for while the voice held the session and had no node for it (its engine letting go): `Sound.resume` plays it if it is
/// this recent, so voice-exit, asked for as the voice ends, is not lost.
struct HeldCue {
    static let window = 0.3
    let cue: SoundCue, gain: Float, at: TimeInterval
    func due(_ now: TimeInterval) -> Bool { now - at <= Self.window }
}

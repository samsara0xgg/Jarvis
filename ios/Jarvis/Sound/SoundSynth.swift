import Accelerate
import Foundation

/// Offline port of the prototype's FM "glass" synth (`glass()` and `CUES`).
/// Each cue renders once to a stereo Float buffer: dry FM hits plus the 0.22 "room" send,
/// whose convolver is replaced by an offline FFT convolution with the same decaying-noise impulse response.
enum SoundSynth {
    static let sampleRate = 44_100.0

    private enum N {
        static let A4 = 440.0, A5 = 880.0, B5 = 987.77, Cs6 = 1108.73, E6 = 1318.51, Fs6 = 1479.98
        static let A6 = 1760.0, B6 = 1975.53, Cs7 = 2217.46
    }

    /// (offset in ms, frequency in Hz, velocity, short)
    private typealias Hit = (ms: Double, f: Double, v: Double, short: Bool)

    private static func score(_ cue: SoundCue) -> (hits: [Hit], len: Double, gain: Double) {
        switch cue {
        case .listen: return ([(0, N.A5, 0.75, false), (75, N.E6, 0.9, false)], 0.6, 1)
        case .heard: return ([(0, N.Cs6, 0.5, false)], 0.4, 1)
        case .think: return ([(0, N.Fs6, 0.24, false), (150, N.E6, 0.2, false)], 0.5, 0.8)
        case .turn: return ([(0, N.E6, 0.42, false)], 0.5, 1)
        case .interrupt: return ([(0, N.E6, 0.6, true), (45, N.A5, 0.65, true)], 1, 1)
        case .end: return ([(0, N.E6, 0.75, false), (95, N.A5, 0.85, false)], 1, 1)
        case .done: return ([(0, N.Cs6, 0.6, false), (85, N.E6, 0.7, false), (170, N.A6, 0.85, false)], 1, 1)
        case .ask: return ([(0, N.B5, 0.6, false), (95, N.E6, 0.7, false), (190, N.B6, 0.85, false)], 1, 1)
        case .remind:
            return ([(0, N.A5, 0.55, false), (0, N.E6, 0.45, false), (420, N.A5, 0.55, false), (420, N.E6, 0.45, false)], 1.2, 1)
        case .msg: return ([(0, N.E6, 0.5, false), (60, N.A6, 0.35, false)], 0.8, 1)
        case .hello:
            return ([(0, N.A4, 0.35, false), (0, N.A5, 0.6, false), (120, N.E6, 0.65, false), (240, N.A6, 0.7, false),
                     (360, N.Cs7, 0.6, false)], 1.3, 1)
        case .bye:
            return ([(0, N.Cs7, 0.45, false), (120, N.A6, 0.5, false), (240, N.E6, 0.55, false), (360, N.A5, 0.65, false),
                     (360, N.A4, 0.3, false)], 1.3, 1)
        case .happy: return ([(0, N.E6, 0.45, false), (70, N.A6, 0.55, false)], 0.5, 1)
        case .open: return ([(0, N.E6, 0.25, true), (40, N.A6, 0.3, true)], 1, 1)
        case .close: return ([(0, N.A6, 0.25, true), (40, N.E6, 0.22, true)], 1, 1)
        case .module: return ([(0, N.A6, 0.34, true)], 1, 1)
        case .back: return ([(0, N.E6, 0.32, true)], 1, 1)
        case .send: return ([(0, N.A5, 0.35, true), (45, N.E6, 0.45, true), (90, N.A6, 0.55, false)], 0.5, 1)
        case .on: return ([(0, N.Cs6, 0.32, true), (45, N.A6, 0.36, true)], 1, 1)
        case .off: return ([(0, N.A6, 0.32, true), (45, N.Cs6, 0.28, true)], 1, 1)
        case .tick: return ([(0, N.Cs7, 0.16, true)], 1, 1)
        case .tock: return ([(0, N.A6, 0.12, true)], 1, 1)
        case .voiceEnter, .voiceExit, .mic, .speakerOn: return ([], 0, 1)     // SoundFeedback's, not FM hits
        }
    }

    private static let outGain = 0.55, sendGain = 0.22, floor = 0.0001

    private static func decay(_ f: Double, _ len: Double, _ short: Bool) -> Double {
        (short ? 0.075 : 0.85 * len) * min(max(1100 / f, 0.55), 1.25)
    }

    private static let attackFrames = Int(0.002 * sampleRate)

    private static func frames(_ h: Hit, _ len: Double) -> Int {
        attackFrames + Int(decay(h.f, len, h.short) * sampleRate)
    }

    /// One glass hit: carrier at f, frequency-modulated at 3.5 f with an exponentially collapsing depth,
    /// plus an inharmonic partial at 2.76 f; every envelope is an exponential ramp, as in Web Audio.
    private static func addGlass(_ dry: inout [Float], at start: Int, f: Double, v: Double, len: Double, short: Bool) {
        let sr = sampleRate, d = decay(f, len, short)
        let nA = attackFrames, nD = Int(d * sr), nP = max(Int(d * 0.3 * sr), 1)
        let peak = max(0.3 * v, 0.0002), pPeak = max(0.05 * v, 0.0002)
        let rA = pow(peak / floor, 1 / Double(nA)), rD = pow(floor / peak, 1 / Double(nD))
        let pA = pow(pPeak / floor, 1 / Double(nA)), pD = pow(floor / pPeak, 1 / Double(nP))
        let depth0 = f * (short ? 0.9 : 1.5) * v, nDep = Int((short ? 0.025 : 0.06) * sr)
        let rDep = pow(f * 0.01 / depth0, 1 / Double(nDep))
        let w = 2 * Double.pi * f / sr
        var amp = floor, par = floor, depth = depth0, cp = 0.0, mp = 0.0, pp = 0.0
        for i in 0..<(nA + nD) {
            var x = amp * sin(cp)
            if i < nA + nP { x += par * sin(pp) }
            dry[start + i] += Float(x)
            cp += w + 2 * Double.pi / sr * depth * sin(mp)
            mp += w * 3.5
            pp += w * 2.76
            amp *= i < nA ? rA : rD
            if i < nA { par *= pA } else if i < nA + nP { par *= pD }
            if i < nDep { depth *= rDep }
        }
    }

    /// Stereo samples `[left, right]` for a cue at gain 1, trimmed once the tail is below -74 dBFS.
    static func render(_ cue: SoundCue) -> [[Float]] {
        switch cue {
        case .voiceEnter: return [SoundFeedback.enter, SoundFeedback.enter]
        case .voiceExit: return [SoundFeedback.exit, SoundFeedback.exit]
        case .mic, .speakerOn: let x = SoundFeedback.clicks(cue); return [x, x]
        default: break
        }
        let s = score(cue), sr = sampleRate
        let starts = s.hits.map { Int($0.ms / 1000 * sr) }
        var dry = [Float](repeating: 0, count: zip(starts, s.hits.map { frames($0, s.len) }).map(+).max() ?? 0)
        for (i, h) in s.hits.enumerated() {
            addGlass(&dry, at: starts[i], f: h.f, v: h.v, len: s.len, short: h.short)
        }
        let wet = Reverb.wet(dry)
        let g = Float(outGain * s.gain), send = Float(sendGain)
        var out = wet
        for c in 0..<2 {
            for i in 0..<out[c].count { out[c][i] = g * ((i < dry.count ? dry[i] : 0) + send * wet[c][i]) }
        }
        var n = out[0].count
        while n > 1, max(abs(out[0][n - 1]), abs(out[1][n - 1])) < 0.0002 { n -= 1 }
        return out.map { Array($0[0..<n]) }
    }

    /// The prototype's room: 1.4 s of noise under a (1 - x)^3.2 envelope, power-normalised the way Web Audio's
    /// ConvolverNode does, convolved through vDSP FFTs.
    private enum Reverb {
        static let lock = NSLock()
        static var setup: FFTSetup?
        static var setupLog2n: vDSP_Length = 0
        static var irSpectra: [vDSP_Length: [(re: [Float], im: [Float])]] = [:]

        static let ir: [[Float]] = {
            var rng = SplitMix64(seed: 0x4A41_5256_4953)
            let n = Int((1.4 * sampleRate).rounded())
            var ch = (0..<2).map { _ in
                (0..<n).map { i in Float(rng.unit() * 2 - 1) * Float(pow(1 - Double(i) / Double(n), 3.2)) }
            }
            let power = sqrt(ch.reduce(0.0) { $0 + $1.reduce(0.0) { $0 + Double($1 * $1) } } / Double(2 * n))
            let scale = Float(0.00125 / max(power, 0.000125) * 44_100 / sampleRate)
            ch = ch.map { $0.map { $0 * scale } }
            return ch
        }()

        /// Left and right wet signal for a mono dry signal, `dry.count + ir.count - 1` samples each.
        static func wet(_ dry: [Float]) -> [[Float]] {
            lock.lock(); defer { lock.unlock() }
            let total = dry.count + ir[0].count - 1
            var log2n: vDSP_Length = 1
            while (1 << Int(log2n)) < total { log2n += 1 }
            if setupLog2n < log2n {
                if let setup { vDSP_destroy_fftsetup(setup) }
                setup = vDSP_create_fftsetup(log2n, FFTRadix(kFFTRadix2))
                setupLog2n = log2n
            }
            let n = 1 << Int(log2n)
            let x = spectrum(dry, n: n, log2n: log2n)
            if irSpectra[log2n] == nil { irSpectra[log2n] = ir.map { spectrum($0, n: n, log2n: log2n) } }
            return irSpectra[log2n]!.map { h in Array(multiplyAndInvert(x, h, n: n, log2n: log2n)[0..<total]) }
        }

        // vDSP's packed real FFT: forward is 2x a DFT, so product + inverse comes out 4n too big.
        private static func spectrum(_ x: [Float], n: Int, log2n: vDSP_Length) -> (re: [Float], im: [Float]) {
            var padded = x + [Float](repeating: 0, count: n - x.count)
            var re = [Float](repeating: 0, count: n / 2), im = re
            re.withUnsafeMutableBufferPointer { rp in
                im.withUnsafeMutableBufferPointer { ip in
                    var split = DSPSplitComplex(realp: rp.baseAddress!, imagp: ip.baseAddress!)
                    padded.withUnsafeMutableBufferPointer {
                        $0.baseAddress!.withMemoryRebound(to: DSPComplex.self, capacity: n / 2) {
                            vDSP_ctoz($0, 2, &split, 1, vDSP_Length(n / 2))
                        }
                    }
                    vDSP_fft_zrip(setup!, &split, 1, log2n, FFTDirection(kFFTDirection_Forward))
                }
            }
            return (re, im)
        }

        private static func multiplyAndInvert(_ a: (re: [Float], im: [Float]), _ b: (re: [Float], im: [Float]), n: Int,
                                              log2n: vDSP_Length) -> [Float] {
            var a = a, b = b
            var re = [Float](repeating: 0, count: n / 2), im = re, out = [Float](repeating: 0, count: n)
            let dc = a.re[0] * b.re[0], nyquist = a.im[0] * b.im[0]  // packed into bin 0, multiplied separately
            a.re.withUnsafeMutableBufferPointer { ar in a.im.withUnsafeMutableBufferPointer { ai in
            b.re.withUnsafeMutableBufferPointer { br in b.im.withUnsafeMutableBufferPointer { bi in
            re.withUnsafeMutableBufferPointer { cr in im.withUnsafeMutableBufferPointer { ci in
                var A = DSPSplitComplex(realp: ar.baseAddress!, imagp: ai.baseAddress!)
                var B = DSPSplitComplex(realp: br.baseAddress!, imagp: bi.baseAddress!)
                var C = DSPSplitComplex(realp: cr.baseAddress!, imagp: ci.baseAddress!)
                vDSP_zvmul(&A, 1, &B, 1, &C, 1, vDSP_Length(n / 2), 1)
                cr[0] = dc
                ci[0] = nyquist
                vDSP_fft_zrip(setup!, &C, 1, log2n, FFTDirection(kFFTDirection_Inverse))
                out.withUnsafeMutableBufferPointer {
                    $0.baseAddress!.withMemoryRebound(to: DSPComplex.self, capacity: n / 2) {
                        vDSP_ztoc(&C, 1, $0, 2, vDSP_Length(n / 2))
                    }
                }
            }}}}}}
            var scale = 1 / Float(4 * n)
            vDSP_vsmul(out, 1, &scale, &out, 1, vDSP_Length(n))
            return out
        }
    }

    /// Deterministic noise, so the room sounds the same on every launch.
    private struct SplitMix64 {
        var state: UInt64
        init(seed: UInt64) { state = seed }
        mutating func unit() -> Double {
            state &+= 0x9E37_79B9_7F4A_7C15
            var z = state
            z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
            z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
            return Double((z ^ (z >> 31)) >> 11) / Double(1 << 53)
        }
    }

    #if DEBUG
    /// Peak, duration and sanity of every cue, plus FFT-vs-direct convolution agreement; for a debug print.
    static func report() -> String {
        var lines: [String] = []
        let t0 = Date()
        for cue in SoundCue.allCases {
            let ch = render(cue)
            let flat = ch[0] + ch[1]
            let peak = flat.map { abs($0) }.max() ?? 0
            let bad = flat.contains { !$0.isFinite }
            lines.append(String(format: "%@ dur=%.3fs peak=%.3f (%.1f dBFS) nan=%@ clip=%@", cue.rawValue,
                                Double(ch[0].count) / sampleRate, peak, 20 * log10(Double(peak)), bad ? "YES" : "no",
                                peak >= 0.98 ? "YES" : "no"))
        }
        lines.append(String(format: "rendered %d cues in %.0f ms", SoundCue.allCases.count, Date().timeIntervalSince(t0) * 1000))
        let x = (0..<3000).map { i in Float(sin(Double(i) * 0.05)) * exp(-Float(i) / 800) }
        let fast = Reverb.wet(x)[0]
        var worst: Float = 0
        for i in [0, 1, 17, 500, 1500, 2999, 4000, 20000, 60000] {
            var s: Float = 0
            for j in max(0, i - 3000 + 1)...min(i, Reverb.ir[0].count - 1) where i - j < x.count { s += x[i - j] * Reverb.ir[0][j] }
            worst = max(worst, abs(s - fast[i]))
        }
        lines.append("fft vs direct convolution, worst abs error: \(worst)")
        return lines.joined(separator: "\n")
    }
    #endif
}

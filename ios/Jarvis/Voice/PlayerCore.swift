import Foundation
import Synchronization

// The realtime half of her voice on the phone: a port of native/voice_out/main.swift's Engine (ADR 0129) with the same
// ring, discard boundary, generation checks, gain ramps, declicks, starvation clause and reports. Differences:
//  - input is int16 at 32 kHz; it is resampled (linear) to the hardware rate when it is written, so every ring slot
//    still carries its generation and 32 kHz source cursor,
//  - reports are stamped on this phone's CLOCK_UPTIME_RAW,
//  - there is no stdin/stdout: `ingest` is the writer (one thread), `render` the callback, `drain` the reader,
//  - `stopNow()` is the local, immediate barge-in stop.
// No AVFoundation here, so the Mac harness can drive `render` directly.

let kDeclickSamples = 128
let kHeardGainFloor = 0.15

@inline(__always)
func uptimeNs() -> Int64 { Int64(truncatingIfNeeded: clock_gettime_nsec_np(CLOCK_UPTIME_RAW)) }

private let timebase: mach_timebase_info_data_t = {
    var t = mach_timebase_info_data_t()
    mach_timebase_info(&t)
    return t
}()

/// mHostTime (mach absolute ticks) to CLOCK_UPTIME_RAW nanoseconds
func hostTimeToNs(_ ticks: UInt64) -> Int64 { Int64(truncatingIfNeeded: ticks &* UInt64(timebase.numer) / UInt64(timebase.denom)) }

private struct OutRecord {
    var kind: Int64 = 0  // 1 report, 2 discard ack
    var a: Int64 = 0, b: Int64 = 0, c: Int64 = 0, d: Int64 = 0, e: Int64 = 0, f: Int64 = 0, g: Int64 = 0
}

// Render-thread-owned state. Behind a raw pointer so the callback pays for no exclusivity checks and no allocation.
private struct RenderState {
    var callbacks: UInt64 = 0
    var readIdx = 0
    var appliedDiscardSeq: UInt64 = 0
    var ackedDiscardSeq: UInt64 = 0
    var gainCurrent = 1.0
    var gainTarget = 1.0
    var gainRemaining = 0
    var gainConsumed: UInt64
    var declickLast: Float = 0
    var declickAmp: Float = 0
    var declickRemaining = 0
    var tailRampSamples = 0
    var firstGeneration: Int64 = -1
    var reportedGen: Int64 = -1         // the last report's generation and end cursor: the next one starts there
    var reportedEnd: Int64 = 0
    var starvationDry: Int64 = -1
    var starvationGaps: UInt64 = 0
    var fadeIn: Int64 = -1
    var played: UInt64 = 0
}

private func packGain(target: Float, ramp: UInt32, seq: UInt8) -> UInt64 {
    UInt64(target.bitPattern) | (UInt64(min(ramp, 0xff_ffff)) << 32) | (UInt64(seq) << 56)
}

private let kOutRingCapacity = 4096

final class PlayerCore: @unchecked Sendable {
    let sampleRate: Int
    let ringSize: Int
    private let mask: Int
    let maxFrames: Int
    private let deviceLatencyPub = Atomic<Int64>(0)
    var deviceLatencyNs: Int64 {
        get { deviceLatencyPub.load(ordering: .relaxed) }
        set { deviceLatencyPub.store(newValue, ordering: .relaxed) }
    }

    // Sample ring: `ingest` produces, `render` consumes.
    private let pcm: UnsafeMutablePointer<Float>
    private let gens: UnsafeMutablePointer<Int64>
    private let curs: UnsafeMutablePointer<Int64>
    // 32 kHz samples handed over up to and including this slot, counted since this player began: what STATUS reports as read,
    // because the host mirrors the ring in its own (wire) samples and paces against that, not against hardware-rate slots
    private let srcs: UnsafeMutablePointer<Int64>
    private let scratchGen: UnsafeMutablePointer<Int64>
    private let scratchCur: UnsafeMutablePointer<Int64>
    private let rs: UnsafeMutablePointer<RenderState>

    private let writeIdx = Atomic<Int>(0)
    private let readIdxPub = Atomic<Int>(0)
    private let srcReadPub = Atomic<Int64>(0)
    private let discardBefore = Atomic<Int>(0)
    private let discardSeq = Atomic<UInt64>(0)
    private let activeGen = Atomic<Int64>(-1)
    private let heldGen = Atomic<Int64>(-1)
    private let gainWord = Atomic<UInt64>(packGain(target: 1.0, ramp: 0, seq: 0))

    // Status mirrors, written by the render thread, read by `drain`.
    private let callbacksPub = Atomic<UInt64>(0)
    private let gainPub = Atomic<UInt64>(1.0.bitPattern)
    private let overloads = Atomic<UInt64>(0)
    private let gapsPub = Atomic<UInt64>(0)
    private let tailPub = Atomic<Int>(0)
    private let playedPub = Atomic<UInt64>(0)
    // What the session and the orb read.
    private let levelPub = Atomic<UInt32>(0)
    private let lastAudioPub = Atomic<Int64>(0)
    private let cursorPub = Atomic<Int64>(0)

    // Render -> drain record ring.
    private let outRing: UnsafeMutablePointer<OutRecord>
    private let outW = Atomic<Int>(0)
    private let outR = Atomic<Int>(0)
    private let outDrops = Atomic<UInt64>(0)

    // Writer-side state (single producer thread).
    private var gainSeq: UInt8 = 0
    private var rsGen: Int64 = -2, rsNext: Int64 = 0, rsT = 0.0, rsPrev: Float = 0
    private var srcCum: Int64 = 0
    // Drain-side state (single consumer thread).
    private var lastStatus: Int64 = 0

    /// ringSize must be a power of two (at the hardware rate: 1 << 18 is 5 s at 48 kHz).
    init(sampleRate: Int, ringSize: Int = 1 << 18, maxFrames: Int = 8192) {
        precondition(ringSize & (ringSize - 1) == 0)
        self.sampleRate = sampleRate
        self.ringSize = ringSize
        self.maxFrames = maxFrames
        mask = ringSize - 1
        pcm = .allocate(capacity: ringSize); pcm.initialize(repeating: 0, count: ringSize)
        gens = .allocate(capacity: ringSize); gens.initialize(repeating: -1, count: ringSize)
        curs = .allocate(capacity: ringSize); curs.initialize(repeating: 0, count: ringSize)
        srcs = .allocate(capacity: ringSize); srcs.initialize(repeating: 0, count: ringSize)
        scratchGen = .allocate(capacity: maxFrames); scratchGen.initialize(repeating: -1, count: maxFrames)
        scratchCur = .allocate(capacity: maxFrames); scratchCur.initialize(repeating: 0, count: maxFrames)
        rs = .allocate(capacity: 1)
        rs.initialize(to: RenderState(gainConsumed: packGain(target: 1.0, ramp: 0, seq: 0)))
        outRing = .allocate(capacity: kOutRingCapacity); outRing.initialize(repeating: OutRecord(), count: kOutRingCapacity)
    }

    deinit {
        pcm.deallocate(); gens.deallocate(); curs.deallocate(); srcs.deallocate(); scratchGen.deallocate(); scratchCur.deallocate()
        rs.deallocate(); outRing.deallocate()
    }

    // MARK: what the rest of the app reads

    /// RMS of the last block handed to the device, 0...1 (before the orb's own scaling)
    var level: Float { Float(bitPattern: levelPub.load(ordering: .relaxed)) }
    /// when a block of hers last played, on the uptime clock (0: never)
    var lastAudioNs: Int64 { lastAudioPub.load(ordering: .relaxed) }
    /// frames cut short because the host sent past the ring, and callbacks that ran dry mid-stream
    var trouble: (overloads: UInt64, gaps: UInt64) { (overloads.load(ordering: .relaxed), gapsPub.load(ordering: .relaxed)) }
    /// 32 kHz cursor just past the last sample played
    var playedCursor: Int64 { cursorPub.load(ordering: .relaxed) }
    var playedSamples: UInt64 { playedPub.load(ordering: .relaxed) }
    var activeGeneration: Int64 { activeGen.load(ordering: .relaxed) }
    /// her audio queued here and not yet played, in seconds
    var bufferedSeconds: Double { Double(writeIdx.load(ordering: .acquiring) - readIdxPub.load(ordering: .acquiring)) / Double(sampleRate) }
    var ringEmpty: Bool { writeIdx.load(ordering: .acquiring) - readIdxPub.load(ordering: .acquiring) <= 0 }

    // MARK: writer (one thread)

    func ingest(_ f: PlayerIn) {
        switch f {
        case .active(let g): activeGen.store(g, ordering: .releasing)
        case .discard(let seq):
            // Stream order makes the boundary exact: everything before this frame is dropped.
            discardBefore.store(writeIdx.load(ordering: .relaxed), ordering: .releasing)
            discardSeq.store(seq, ordering: .releasing)
            rsGen = -2                                   // what follows does not continue what was dropped
        case .gain(let target, let ramp):
            gainSeq &+= 1
            gainWord.store(packGain(target: target, ramp: ramp, seq: gainSeq), ordering: .releasing)
        case .hold(let g): heldGen.store(g, ordering: .releasing)
        case .pcm(let g, let start, let x): write(generation: g, start: start, x)
        }
    }

    /// Barge-in: silence from the next callback (a declick, then zeros) and everything queued is dropped. No ack: the host's
    /// own ACTIVE(-1) and DISCARD still follow and are acked as usual.
    func stopNow() {
        activeGen.store(-1, ordering: .releasing)
        discardBefore.store(writeIdx.load(ordering: .relaxed), ordering: .releasing)
        heldGen.store(-1, ordering: .releasing)
        rsGen = -2
    }

    private func write(generation: Int64, start: Int64, _ x: [Int16]) {
        let n = x.count
        guard n > 0 else { return }
        let scale: Float = 1 / 32768
        if generation != rsGen || start != rsNext {      // a new stream: begin on its first sample
            rsGen = generation; rsT = 0; rsPrev = Float(x[0]) * scale
        }
        rsNext = start + Int64(n)
        let step = Double(kWireRate) / Double(sampleRate)
        var w = writeIdx.load(ordering: .relaxed)
        let r = readIdxPub.load(ordering: .acquiring)
        var t = rsT
        var wrote = 0
        while t <= Double(n - 1) {
            if w + wrote - r >= ringSize {               // the host sent past the ring: drop the rest of this frame
                overloads.wrappingAdd(1, ordering: .relaxed)
                rsGen = -2
                break
            }
            let i = Int(t.rounded(.down))
            let a = i >= 0 ? Float(x[i]) * scale : rsPrev
            let b = i + 1 < n ? Float(x[i + 1]) * scale : a   // past the end only when t is exactly the last sample
            let slot = (w + wrote) & mask
            pcm[slot] = a + (b - a) * Float(t - Double(i))
            gens[slot] = generation
            curs[slot] = start + Int64(i)
            srcs[slot] = srcCum + Int64(i)
            wrote += 1
            t += step
        }
        // The last sample out covers the frame's end, so the ledger's end cursor lands on the true total.
        if wrote > 0 { curs[(w + wrote - 1) & mask] = start + Int64(n - 1) }
        // the frame's last slot accounts for all of it (a tail dropped on overload too: the host counted it as sent)
        srcCum += Int64(n)
        if wrote > 0 { srcs[(w + wrote - 1) & mask] = srcCum }
        rsT = t - Double(n)
        rsPrev = Float(x[n - 1]) * scale
        w += wrote
        writeIdx.store(w, ordering: .releasing)
    }

    // MARK: render (realtime: no allocation, locks, syscalls or I/O)

    private func push(_ record: OutRecord) -> Bool {
        let w = outW.load(ordering: .relaxed)
        if w - outR.load(ordering: .acquiring) >= kOutRingCapacity {
            outDrops.wrappingAdd(1, ordering: .relaxed)
            return false
        }
        outRing[w & (kOutRingCapacity - 1)] = record
        outW.store(w + 1, ordering: .releasing)
        return true
    }

    @inline(__always)
    private func gainSetTarget(_ target: Double, _ ramp: Int) {
        rs.pointee.gainTarget = target
        rs.pointee.gainRemaining = max(0, ramp)
        if rs.pointee.gainRemaining == 0 { rs.pointee.gainCurrent = target }
    }

    @inline(__always)
    private func consumeGainCommand(rampOverride: Int?) {
        let word = gainWord.load(ordering: .acquiring)
        if word == rs.pointee.gainConsumed { return }
        let target = Double(Float(bitPattern: UInt32(truncatingIfNeeded: word)))
        let ramp = Int((word >> 32) & 0xff_ffff)
        gainSetTarget(target, rampOverride ?? ramp)
        rs.pointee.gainConsumed = word
    }

    /// 0 normal, 1 attenuated, 2 muted. Classifies the next block before advancing the ramp.
    @inline(__always)
    private func gainApply(_ out: UnsafeMutablePointer<Float>, _ n: Int) -> UInt8 {
        let s = rs
        let isNormal = s.pointee.gainCurrent == 1.0 && s.pointee.gainTarget == 1.0 && s.pointee.gainRemaining == 0
        var cls: UInt8 = 1
        if isNormal || min(s.pointee.gainCurrent, s.pointee.gainTarget) >= kHeardGainFloor {
            cls = 0
        } else if s.pointee.gainCurrent == 0.0 && s.pointee.gainTarget == 0.0 && s.pointee.gainRemaining == 0 {
            cls = 2
        }
        if s.pointee.gainRemaining == 0 {
            if s.pointee.gainCurrent != 1.0 {
                let g = Float(s.pointee.gainCurrent)
                for i in 0..<n { out[i] *= g }
            }
            return cls
        }
        let step = min(n, s.pointee.gainRemaining)
        let fracEnd = Double(step) / Double(s.pointee.gainRemaining)
        let current = s.pointee.gainCurrent
        let next = current + (s.pointee.gainTarget - current) * fracEnd
        if step == 1 {
            out[0] *= Float(next)
        } else {
            let slope = (next - current) / Double(step - 1)
            for i in 0..<step { out[i] *= Float(current + slope * Double(i)) }
        }
        if step < n {
            let g = Float(next)
            for i in step..<n { out[i] *= g }
        }
        s.pointee.gainCurrent = next
        s.pointee.gainRemaining -= step
        if s.pointee.gainRemaining == 0 { s.pointee.gainCurrent = s.pointee.gainTarget }
        return cls
    }

    /// Fill out[0..<frames] with a decay to silence, then exact zeros (ADR-0006 D11).
    @inline(__always)
    private func emitDeclick(_ out: UnsafeMutablePointer<Float>, _ frames: Int) {
        let s = rs
        var remaining = s.pointee.declickRemaining
        if remaining == 0 && s.pointee.declickLast != 0.0 {
            s.pointee.declickAmp = s.pointee.declickLast
            remaining = kDeclickSamples
            s.pointee.declickLast = 0.0
        }
        let tail = min(remaining, frames)
        if tail > 0 {
            let done = kDeclickSamples - remaining
            let amp = s.pointee.declickAmp
            for i in 0..<tail {
                out[i] = Float(kDeclickSamples - (done + i + 1)) / Float(kDeclickSamples) * amp
            }
        }
        s.pointee.declickRemaining = remaining - tail
        if tail < frames { for i in tail..<frames { out[i] = 0 } }
    }

    /// ``delayNs``: from now until the block's first sample leaves the speaker.
    func render(_ out: UnsafeMutablePointer<Float>, frames: Int, delayNs: Int64) {
        renderBody(out, frames, delayNs)
        let s = rs
        if s.pointee.appliedDiscardSeq != s.pointee.ackedDiscardSeq {
            var ack = OutRecord()
            ack.kind = 2
            ack.a = Int64(bitPattern: s.pointee.appliedDiscardSeq)
            if push(ack) { s.pointee.ackedDiscardSeq = s.pointee.appliedDiscardSeq }
        }
        var sum: Float = 0
        for i in 0..<frames { sum += out[i] * out[i] }
        levelPub.store((sum / Float(max(frames, 1))).squareRoot().bitPattern, ordering: .relaxed)
        callbacksPub.store(s.pointee.callbacks, ordering: .relaxed)
        gainPub.store(s.pointee.gainCurrent.bitPattern, ordering: .relaxed)
        gapsPub.store(s.pointee.starvationGaps, ordering: .relaxed)
        tailPub.store(s.pointee.tailRampSamples, ordering: .relaxed)
        playedPub.store(s.pointee.played, ordering: .relaxed)
        readIdxPub.store(s.pointee.readIdx, ordering: .releasing)
    }

    // swiftlint:disable:next function_body_length
    private func renderBody(_ out: UnsafeMutablePointer<Float>, _ frames: Int, _ delayNs: Int64) {
        let s = rs
        s.pointee.callbacks &+= 1
        if frames > maxFrames {
            // An unexpected host block fails silent rather than overrun scratch.
            for i in 0..<frames { out[i] = 0 }
            overloads.wrappingAdd(1, ordering: .relaxed)
            return
        }
        let activeBefore = activeGen.load(ordering: .acquiring)
        let held = heldGen.load(ordering: .acquiring)
        if activeBefore >= 0 && activeBefore == held {
            // Held while a barge-in is judged: silence, and her place kept. A DISCARD is still applied and acked now: the host judges
            // his words a turn, discards her and waits half a second for the ack; one held back until the hold ended came too late over
            // Tailscale, and the host shut the answer lane for good (12:29 PT, no answer after he spoke over her).
            let seq = discardSeq.load(ordering: .acquiring)
            if seq != s.pointee.appliedDiscardSeq {
                s.pointee.readIdx = max(s.pointee.readIdx, discardBefore.load(ordering: .acquiring))
                s.pointee.appliedDiscardSeq = seq
            }
            emitDeclick(out, frames)
            s.pointee.fadeIn = held
            return
        }

        // Ring read: apply the discard boundary first, then copy and zero-pad.
        let seq = discardSeq.load(ordering: .acquiring)
        let boundary = discardBefore.load(ordering: .acquiring)
        var ri = s.pointee.readIdx
        if boundary > ri { ri = boundary }
        s.pointee.appliedDiscardSeq = seq
        let available = writeIdx.load(ordering: .acquiring) - ri
        let actual = max(0, min(frames, available))
        for i in 0..<actual {
            let slot = (ri + i) & mask
            out[i] = pcm[slot]
            scratchGen[i] = gens[slot]
            scratchCur[i] = curs[slot]
        }
        if actual < frames { for i in actual..<frames { out[i] = 0 } }
        s.pointee.readIdx = ri + actual
        if ri + actual > 0 { srcReadPub.store(srcs[(ri + actual - 1) & mask], ordering: .relaxed) }

        // ADR-0006:347 starvation clause.
        var resumed = false
        if activeBefore >= 0 {
            if actual > 0 && activeBefore == s.pointee.starvationDry {
                s.pointee.starvationGaps &+= 1
                s.pointee.starvationDry = -1
                resumed = true
            }
            if actual < frames && activeBefore == s.pointee.firstGeneration {
                s.pointee.starvationDry = activeBefore
            }
        }
        if actual <= 0 {
            // Nothing of hers is playing, so a gain set meanwhile lands whole.
            consumeGainCommand(rampOverride: 0)
            emitDeclick(out, frames)
            return
        }
        let activeAfter = activeGen.load(ordering: .acquiring)
        if activeBefore < 0 || activeAfter < 0 || activeBefore != activeAfter {
            emitDeclick(out, actual)
            return
        }
        let generation = activeAfter
        for i in 0..<actual where scratchGen[i] != generation {
            emitDeclick(out, actual)
            return
        }
        if s.pointee.fadeIn == generation {
            // Back from a hold: ramp in rather than step onto the waveform.
            s.pointee.fadeIn = -1
            let gain = s.pointee.gainCurrent
            gainSetTarget(0.0, 0)
            gainSetTarget(gain, kDeclickSamples)
        }
        consumeGainCommand(rampOverride: nil)
        let audibility = gainApply(out, frames)

        // Revalidate immediately before returning the block to the host.
        if activeGen.load(ordering: .acquiring) != generation {
            emitDeclick(out, actual)
            return
        }
        // At 48 kHz two slots can share a 32 kHz cursor, so a block's first slot may repeat the last one's: start where the
        // last report ended, or the host's ledger reads a gap and never trusts the heard prefix again (live 2026-10-10: stuck at 735).
        var startCursor = scratchCur[0]
        if generation == s.pointee.reportedGen, startCursor <= s.pointee.reportedEnd { startCursor = s.pointee.reportedEnd }
        let endCursor = max(startCursor, scratchCur[actual - 1] + 1)
        s.pointee.reportedGen = generation
        s.pointee.reportedEnd = endCursor
        let first = s.pointee.firstGeneration != generation
        if first {
            s.pointee.firstGeneration = generation
            s.pointee.tailRampSamples = 0
        }
        if resumed {
            // Back from a dry ring: rise out of the silence rather than step onto the waveform.
            let ramp = min(actual, kDeclickSamples)
            let div = ramp > 1 ? Float(ramp - 1) : 1.0
            for j in 0..<ramp { out[j] *= Float(j) / div }
        }
        if actual < frames {
            // ADR-0006 D12: decay this block's own last real samples to exactly 0.0.
            let ramp = min(actual, kDeclickSamples)
            let div = ramp > 1 ? Float(ramp - 1) : 1.0
            for j in 0..<ramp { out[actual - ramp + j] *= Float(ramp - 1 - j) / div }
            s.pointee.tailRampSamples = ramp
        }
        s.pointee.played &+= UInt64(actual)
        // The ledger compares the END cursor against this horizon, so the delay
        // runs to the block's last sample: its duration is added to the first's.
        var report = OutRecord()
        report.kind = 1
        report.a = generation
        report.b = startCursor
        report.c = endCursor
        report.d = Int64(audibility)
        report.e = uptimeNs()
        report.f = delayNs + Int64(frames) * 1_000_000_000 / Int64(sampleRate)
        report.g = first ? 1 : 0
        _ = push(report)
        lastAudioPub.store(report.e, ordering: .relaxed)
        cursorPub.store(endCursor, ordering: .relaxed)
        s.pointee.declickRemaining = 0
        s.pointee.declickLast = out[frames - 1]
    }

    // MARK: drain (one thread, a few milliseconds apart)

    /// Everything the callback has to tell the host since the last call: reports and acks in order, then a STATUS (every 20 ms
    /// while a generation is active, every 500 ms otherwise, and with any report).
    func drain() -> [PlayerOut] {
        var frames: [PlayerOut] = []
        var r = outR.load(ordering: .relaxed)
        let w = outW.load(ordering: .acquiring)
        while r < w {
            let rec = outRing[r & (kOutRingCapacity - 1)]
            if rec.kind == 1 {
                frames.append(.report(PlayerReport(generation: rec.a, start: rec.b, end: rec.c, audibility: UInt8(truncatingIfNeeded: rec.d),
                                                   callbackNs: rec.e, presentationDelayNs: rec.f, first: rec.g != 0)))
            } else {
                frames.append(.discardAck(UInt64(bitPattern: rec.a)))
            }
            r += 1
        }
        outR.store(r, ordering: .releasing)
        let now = uptimeNs()
        let every: Int64 = activeGen.load(ordering: .relaxed) >= 0 ? 20_000_000 : 500_000_000
        if !frames.isEmpty || now - lastStatus >= every {
            frames.append(.status(PlayerStatus(
                readIdx: srcReadPub.load(ordering: .relaxed), gain: Double(bitPattern: gainPub.load(ordering: .relaxed)),
                callbacks: callbacksPub.load(ordering: .relaxed), overloads: overloads.load(ordering: .relaxed),
                starvationGaps: gapsPub.load(ordering: .relaxed), tailRampSamples: UInt32(tailPub.load(ordering: .relaxed)),
                playedSamples: playedPub.load(ordering: .relaxed))))
            lastStatus = now
        }
        return frames
    }

    /// READY names the rate the host sends at (32 kHz); resampling to the hardware rate is this side's own business.
    func readyFrame(bufferFrames: Int) -> PlayerOut {
        .ready(sampleRate: UInt32(kWireRate), bufferFrames: UInt32(bufferFrames), deviceLatencyNs: deviceLatencyNs, clockNs: uptimeNs())
    }
}

#if DEBUG || VOICE_HARNESS
/// Drive the callback by hand: one generation plays through with true cursors, a local stop silences it, and a host discard is acked.
func demoPlayerCore() {
    let p = PlayerCore(sampleRate: 48_000, ringSize: 1 << 14)
    var block = [Float](repeating: 0, count: 480)
    func render() { block.withUnsafeMutableBufferPointer { p.render($0.baseAddress!, frames: 480, delayNs: 10_000_000) } }
    let tone = (0..<3200).map { Int16(8000 * sin(Double($0) * 0.2)) }          // 0.1 s at 32 kHz
    p.ingest(.active(3)); p.ingest(.pcm(generation: 3, start: 1000, samples: tone))
    var reports: [PlayerReport] = []
    for _ in 0..<12 { render(); for case .report(let r) in p.drain() { reports.append(r) } }
    assert(reports.first?.first == true && reports.first?.start == 1000, "\(reports)")
    assert(reports.last?.end == 1000 + 3200, "all 3200 source samples are played: \(String(describing: reports.last))")
    assert(zip(reports, reports.dropFirst()).allSatisfy { $0.end <= $1.end && $1.first == false })
    assert(reports.allSatisfy { $0.generation == 3 && $0.audibility == 0 && $0.presentationDelayNs >= 20_000_000 }, "\(reports)")
    assert(abs(Int(p.playedSamples) - 4800) <= 1 && p.playedCursor == 4200, "\(p.playedSamples) \(p.playedCursor)")
    // 1024-frame blocks: a block's first slot can share the last one's 32 kHz cursor; the reports must still join end to start
    let q = PlayerCore(sampleRate: 48_000, ringSize: 1 << 14)
    var big = [Float](repeating: 0, count: 1024)
    q.ingest(.active(1))
    for f in stride(from: 0, to: 3200, by: 735) { q.ingest(.pcm(generation: 1, start: Int64(f), samples: Array(tone[f..<min(f + 735, 3200)]))) }   // the host's frames
    var joined: [PlayerReport] = []
    for _ in 0..<6 { big.withUnsafeMutableBufferPointer { q.render($0.baseAddress!, frames: 1024, delayNs: 0) }; for case .report(let r) in q.drain() { joined.append(r) } }
    assert(zip(joined, joined.dropFirst()).allSatisfy { $0.end == $1.start } && joined.last?.end == 3200, "\(joined.map { ($0.start, $0.end) })")
    // a local stop silences the next block and drops what is queued
    p.ingest(.pcm(generation: 3, start: 4200, samples: tone))
    render(); assert(block.contains { $0 != 0 })
    p.stopNow(); render()
    assert(block.prefix(kDeclickSamples).last == 0 && block.dropFirst(kDeclickSamples).allSatisfy { $0 == 0 }, "declick then zeros")
    render(); assert(p.ringEmpty && block.allSatisfy { $0 == 0 })
    // the host's discard boundary: samples sent before it never play, and the ack follows after the callback applies it
    p.ingest(.active(4)); p.ingest(.pcm(generation: 4, start: 0, samples: tone)); p.ingest(.discard(9))
    p.ingest(.pcm(generation: 4, start: 3200, samples: tone))
    var out: [PlayerOut] = []
    render(); out += p.drain()
    let starts = out.compactMap { o -> Int64? in if case .report(let r) = o, r.generation == 4 { return r.start } else { return nil } }
    assert(starts.first == 3200, "the discarded 0..<3200 is skipped: \(starts)")
    assert(out.contains(.discardAck(9)))
    // held while his words over her are judged: the host's discard is still acked at once, and what it drops never plays
    p.ingest(.active(6)); p.ingest(.pcm(generation: 6, start: 0, samples: tone)); render()
    p.ingest(.hold(6)); p.ingest(.discard(10))
    render(); assert(p.drain().contains(.discardAck(10)), "a held player acks the discard")
    assert(p.ringEmpty, "the discarded audio is gone")
    p.ingest(.hold(-1))
    // samples of another generation than the active one play as a declick, then silence
    p.ingest(.active(5)); render(); assert(block.dropFirst(kDeclickSamples).allSatisfy { $0 == 0 })
}
#endif

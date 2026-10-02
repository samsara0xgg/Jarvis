// jarvis-voice-out: the realtime half of Jarvis's streaming player (ADR 0129).
//
// Python keeps leases, the PlaybackLedger, heard-prefix accounting and the media
// actor. This process owns only what must meet CoreAudio's deadline: the sample
// ring, gain ramps, declicks and the render callback, ported from
// AudioStreamPlayer._callback in jarvis/surface/voice_tts.py. It talks to the
// daemon over stdin/stdout and exits when stdin closes, so it cannot outlive it.
//
// Wire protocol, little-endian. Every frame is: u32 length (type byte plus
// payload), u8 type, payload. Mirrored in jarvis/surface/voice_native_out.py.
//
//   Python -> helper
//     1 PCM      i64 generation, i64 start_cursor, f32[] samples
//     2 ACTIVE   i64 generation (-1: none)
//     3 DISCARD  u64 seq: drop every sample sent before this frame
//     4 GAIN     f32 target, u32 ramp_samples (latest wins)
//     5 HOLD     i64 generation (-1: none)
//   helper -> Python
//     0x81 READY        u32 sample_rate, u32 buffer_frames, i64 device_latency_ns, i64 clock_ns
//     0x82 REPORT       i64 generation, i64 start, i64 end, u8 audibility (0 normal, 1 attenuated,
//                       2 muted, 3 unknown), i64 callback_ns, i64 presentation_delay_ns, u8 first
//     0x83 STATUS       i64 read_idx, f64 gain, u64 callbacks, u64 overloads, u64 starvation_gaps,
//                       u32 tail_ramp_samples, u64 played_samples
//     0x84 DISCARD_ACK  u64 seq (first callback after that boundary was applied; every report for
//                       the discarded samples precedes it)
//
// Options: --rate N --ring-samples N (a power of two) --buffer-frames N
//          --device NAME
//          --null-device (a plain thread renders at real-time pace; for tests)
//          --null-capture PATH (with --null-device: append every rendered block to PATH)

import AudioToolbox
import CoreAudio
import Foundation
import Synchronization

let kDeclickSamples = 128
let kHeardGainFloor = 0.15
let kMaxFrames = 4096
let kOutRingCapacity = 4096
let kMaxPcmBytes = 17 + 4 * 65536

func fail(_ message: String, code: Int32) -> Never {
    FileHandle.standardError.write(Data("jarvis-voice-out: \(message)\n".utf8))
    exit(code)
}

@inline(__always)
func uptimeNs() -> Int64 {
    Int64(truncatingIfNeeded: clock_gettime_nsec_np(CLOCK_UPTIME_RAW))
}

struct OutRecord {
    var kind: Int64 = 0  // 1 report, 2 discard ack
    var a: Int64 = 0
    var b: Int64 = 0
    var c: Int64 = 0
    var d: Int64 = 0
    var e: Int64 = 0
    var f: Int64 = 0
    var g: Int64 = 0
}

// Render-thread-owned state. Behind a raw pointer so the callback pays for no
// exclusivity checks and no allocation.
struct RenderState {
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
    var starvationDry: Int64 = -1
    var starvationGaps: UInt64 = 0
    var fadeIn: Int64 = -1
    var played: UInt64 = 0
}

func packGain(target: Float, ramp: UInt32, seq: UInt8) -> UInt64 {
    UInt64(target.bitPattern) | (UInt64(min(ramp, 0xff_ffff)) << 32) | (UInt64(seq) << 56)
}

final class Engine: @unchecked Sendable {
    let sampleRate: Int
    let ringSize: Int
    let mask: Int
    var bufferFrames: Int
    // Read by the render callback: an atomic, not a var, so the callback pays for no
    // exclusivity check.
    private let deviceLatencyPub = Atomic<Int64>(0)
    var deviceLatencyNs: Int64 {
        get { deviceLatencyPub.load(ordering: .relaxed) }
        set { deviceLatencyPub.store(newValue, ordering: .relaxed) }
    }

    // Sample ring: the stdin thread produces, the render thread consumes.
    let pcm: UnsafeMutablePointer<Float>
    let gens: UnsafeMutablePointer<Int64>
    let curs: UnsafeMutablePointer<Int64>
    let scratchGen: UnsafeMutablePointer<Int64>
    let scratchCur: UnsafeMutablePointer<Int64>
    let rs: UnsafeMutablePointer<RenderState>

    let writeIdx = Atomic<Int>(0)
    let readIdxPub = Atomic<Int>(0)
    let discardBefore = Atomic<Int>(0)
    let discardSeq = Atomic<UInt64>(0)
    let activeGen = Atomic<Int64>(-1)
    let heldGen = Atomic<Int64>(-1)
    let gainWord = Atomic<UInt64>(packGain(target: 1.0, ramp: 0, seq: 0))

    // Status mirrors, written by the render thread, read by the stdout thread.
    let callbacksPub = Atomic<UInt64>(0)
    let gainPub = Atomic<UInt64>(1.0.bitPattern)
    let overloads = Atomic<UInt64>(0)
    let gapsPub = Atomic<UInt64>(0)
    let tailPub = Atomic<Int>(0)
    let playedPub = Atomic<UInt64>(0)

    // Render -> stdout record ring.
    let outRing: UnsafeMutablePointer<OutRecord>
    let outW = Atomic<Int>(0)
    let outR = Atomic<Int>(0)
    let outDrops = Atomic<UInt64>(0)

    let shuttingDown = Atomic<Bool>(false)

    init(sampleRate: Int, ringSize: Int, bufferFrames: Int) {
        self.sampleRate = sampleRate
        self.ringSize = ringSize
        self.mask = ringSize - 1
        self.bufferFrames = bufferFrames
        pcm = .allocate(capacity: ringSize)
        pcm.initialize(repeating: 0, count: ringSize)
        gens = .allocate(capacity: ringSize)
        gens.initialize(repeating: -1, count: ringSize)
        curs = .allocate(capacity: ringSize)
        curs.initialize(repeating: 0, count: ringSize)
        scratchGen = .allocate(capacity: kMaxFrames)
        scratchGen.initialize(repeating: -1, count: kMaxFrames)
        scratchCur = .allocate(capacity: kMaxFrames)
        scratchCur.initialize(repeating: 0, count: kMaxFrames)
        rs = .allocate(capacity: 1)
        rs.initialize(to: RenderState(gainConsumed: packGain(target: 1.0, ramp: 0, seq: 0)))
        outRing = .allocate(capacity: kOutRingCapacity)
        outRing.initialize(repeating: OutRecord(), count: kOutRingCapacity)
    }

    // MARK: render (realtime: no allocation, locks, syscalls or I/O)

    func push(_ record: OutRecord) -> Bool {
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
    func gainSetTarget(_ target: Double, _ ramp: Int) {
        rs.pointee.gainTarget = target
        rs.pointee.gainRemaining = max(0, ramp)
        if rs.pointee.gainRemaining == 0 { rs.pointee.gainCurrent = target }
    }

    @inline(__always)
    func consumeGainCommand(rampOverride: Int?) {
        let word = gainWord.load(ordering: .acquiring)
        if word == rs.pointee.gainConsumed { return }
        let target = Double(Float(bitPattern: UInt32(truncatingIfNeeded: word)))
        let ramp = Int((word >> 32) & 0xff_ffff)
        gainSetTarget(target, rampOverride ?? ramp)
        rs.pointee.gainConsumed = word
    }

    /// 0 normal, 1 attenuated, 2 muted. Classifies the next block before advancing the ramp.
    @inline(__always)
    func gainApply(_ out: UnsafeMutablePointer<Float>, _ n: Int) -> UInt8 {
        let s = rs
        let isNormal = s.pointee.gainCurrent == 1.0 && s.pointee.gainTarget == 1.0
            && s.pointee.gainRemaining == 0
        var cls: UInt8 = 1
        if isNormal || min(s.pointee.gainCurrent, s.pointee.gainTarget) >= kHeardGainFloor {
            cls = 0
        } else if s.pointee.gainCurrent == 0.0 && s.pointee.gainTarget == 0.0
            && s.pointee.gainRemaining == 0 {
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
    func emitDeclick(_ out: UnsafeMutablePointer<Float>, _ frames: Int) {
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

    func render(_ out: UnsafeMutablePointer<Float>, frames: Int, delayNs: Int64) {
        renderBody(out, frames, delayNs)
        let s = rs
        if s.pointee.appliedDiscardSeq != s.pointee.ackedDiscardSeq {
            var ack = OutRecord()
            ack.kind = 2
            ack.a = Int64(bitPattern: s.pointee.appliedDiscardSeq)
            if push(ack) { s.pointee.ackedDiscardSeq = s.pointee.appliedDiscardSeq }
        }
        callbacksPub.store(s.pointee.callbacks, ordering: .relaxed)
        gainPub.store(s.pointee.gainCurrent.bitPattern, ordering: .relaxed)
        gapsPub.store(s.pointee.starvationGaps, ordering: .relaxed)
        tailPub.store(s.pointee.tailRampSamples, ordering: .relaxed)
        playedPub.store(s.pointee.played, ordering: .relaxed)
        readIdxPub.store(s.pointee.readIdx, ordering: .releasing)
    }

    // swiftlint:disable:next function_body_length
    func renderBody(_ out: UnsafeMutablePointer<Float>, _ frames: Int, _ delayNs: Int64) {
        let s = rs
        s.pointee.callbacks &+= 1
        if frames > kMaxFrames {
            // An unexpected host block fails silent rather than overrun scratch.
            for i in 0..<frames { out[i] = 0 }
            overloads.wrappingAdd(1, ordering: .relaxed)
            return
        }
        let activeBefore = activeGen.load(ordering: .acquiring)
        let held = heldGen.load(ordering: .acquiring)
        if activeBefore >= 0 && activeBefore == held {
            // Held while a barge-in is judged: silence, and her place kept.
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
        let startCursor = scratchCur[0]
        let endCursor = scratchCur[actual - 1] + 1
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
        s.pointee.declickRemaining = 0
        s.pointee.declickLast = out[frames - 1]
    }
}

// MARK: byte helpers

struct ByteBuffer {
    var bytes: [UInt8] = []

    mutating func put<T: FixedWidthInteger>(_ value: T) {
        withUnsafeBytes(of: value.littleEndian) { bytes.append(contentsOf: $0) }
    }

    mutating func put(_ value: Double) { put(value.bitPattern) }

    mutating func frame(type: UInt8, _ body: (inout ByteBuffer) -> Void) {
        let lengthAt = bytes.count
        put(UInt32(0))
        put(type)
        body(&self)
        let length = UInt32(bytes.count - lengthAt - 4).littleEndian
        withUnsafeBytes(of: length) { raw in
            for i in 0..<4 { bytes[lengthAt + i] = raw[i] }
        }
    }
}

func writeAll(_ fd: Int32, _ bytes: [UInt8]) {
    bytes.withUnsafeBytes { raw in
        var offset = 0
        while offset < raw.count {
            let n = write(fd, raw.baseAddress! + offset, raw.count - offset)
            if n < 0 {
                if errno == EINTR { continue }
                exit(0)  // the daemon is gone
            }
            offset += n
        }
    }
}

func readExact(_ fd: Int32, _ dst: UnsafeMutableRawPointer, _ count: Int) -> Bool {
    var offset = 0
    while offset < count {
        let n = read(fd, dst + offset, count - offset)
        if n == 0 { return false }
        if n < 0 {
            if errno == EINTR { continue }
            return false
        }
        offset += n
    }
    return true
}

// MARK: threads

func runStdinReader(_ engine: Engine, finish: @escaping () -> Never) {
    let payload = UnsafeMutableRawPointer.allocate(byteCount: kMaxPcmBytes, alignment: 16)
    var gainSeq: UInt8 = 0
    var header: UInt32 = 0
    while readExact(0, &header, 4) {
        let length = Int(UInt32(littleEndian: header))
        if length < 1 || length > kMaxPcmBytes + 1 { fail("bad frame length \(length)", code: 3) }
        if !readExact(0, payload, length) { break }
        let type = payload.load(as: UInt8.self)
        let body = payload + 1
        switch type {
        case 1:
            let samples = (length - 1 - 16) / 4
            let generation = body.loadUnaligned(as: Int64.self)
            let startCursor = body.loadUnaligned(fromByteOffset: 8, as: Int64.self)
            let w = engine.writeIdx.load(ordering: .relaxed)
            if w + samples - engine.readIdxPub.load(ordering: .acquiring) > engine.ringSize {
                fail("sample ring overflow: the daemon sent past the mirrored capacity", code: 3)
            }
            let src = body + 16
            for i in 0..<samples {
                let slot = (w + i) & engine.mask
                engine.pcm[slot] = src.loadUnaligned(fromByteOffset: 4 * i, as: Float.self)
                engine.gens[slot] = generation
                engine.curs[slot] = startCursor + Int64(i)
            }
            engine.writeIdx.store(w + samples, ordering: .releasing)
        case 2:
            engine.activeGen.store(body.loadUnaligned(as: Int64.self), ordering: .releasing)
        case 3:
            // Stream order makes the boundary exact: everything before this frame is dropped.
            let seq = body.loadUnaligned(as: UInt64.self)
            engine.discardBefore.store(engine.writeIdx.load(ordering: .relaxed), ordering: .releasing)
            engine.discardSeq.store(seq, ordering: .releasing)
        case 4:
            let target = Float(bitPattern: body.loadUnaligned(as: UInt32.self))
            let ramp = body.loadUnaligned(fromByteOffset: 4, as: UInt32.self)
            gainSeq &+= 1
            engine.gainWord.store(packGain(target: target, ramp: ramp, seq: gainSeq), ordering: .releasing)
        case 5:
            engine.heldGen.store(body.loadUnaligned(as: Int64.self), ordering: .releasing)
        default:
            fail("unknown frame type \(type)", code: 3)
        }
    }
    finish()
}

func runStdoutWriter(_ engine: Engine) {
    var buffer = ByteBuffer()
    buffer.bytes.reserveCapacity(1 << 16)
    var lastStatus: Int64 = 0
    func status() {
        buffer.frame(type: 0x83) { b in
            b.put(Int64(engine.readIdxPub.load(ordering: .acquiring)))
            b.put(Double(bitPattern: engine.gainPub.load(ordering: .relaxed)))
            b.put(engine.callbacksPub.load(ordering: .relaxed))
            b.put(engine.overloads.load(ordering: .relaxed))
            b.put(engine.gapsPub.load(ordering: .relaxed))
            b.put(UInt32(engine.tailPub.load(ordering: .relaxed)))
            b.put(engine.playedPub.load(ordering: .relaxed))
        }
        lastStatus = uptimeNs()
    }
    while true {
        var reported = false
        var r = engine.outR.load(ordering: .relaxed)
        let w = engine.outW.load(ordering: .acquiring)
        while r < w {
            let rec = engine.outRing[r & (kOutRingCapacity - 1)]
            if rec.kind == 1 {
                buffer.frame(type: 0x82) { b in
                    b.put(rec.a)
                    b.put(rec.b)
                    b.put(rec.c)
                    b.put(UInt8(truncatingIfNeeded: rec.d))
                    b.put(rec.e)
                    b.put(rec.f)
                    b.put(UInt8(truncatingIfNeeded: rec.g))
                }
            } else {
                buffer.frame(type: 0x84) { b in b.put(UInt64(bitPattern: rec.a)) }
            }
            reported = true
            r += 1
        }
        engine.outR.store(r, ordering: .releasing)
        if reported || uptimeNs() - lastStatus >= 20_000_000 { status() }
        if !buffer.bytes.isEmpty {
            writeAll(1, buffer.bytes)
            buffer.bytes.removeAll(keepingCapacity: true)
        }
        usleep(1000)
    }
}

func runNullDevice(_ engine: Engine, capture: String?) {
    // The capture file is a test aid: every rendered block, float32 mono, in order.
    let captureFd = capture.map { open($0, O_WRONLY | O_CREAT | O_TRUNC, 0o644) } ?? -1
    let frames = engine.bufferFrames
    let out = UnsafeMutablePointer<Float>.allocate(capacity: frames)
    let period = Int64(frames) * 1_000_000_000 / Int64(engine.sampleRate)
    var next = uptimeNs()
    while true {
        engine.render(out, frames: frames, delayNs: engine.deviceLatencyNs)
        if captureFd >= 0 { _ = write(captureFd, out, frames * 4) }
        next += period
        let wait = next - uptimeNs()
        if wait > 0 {
            var ts = timespec(tv_sec: Int(wait / 1_000_000_000), tv_nsec: Int(wait % 1_000_000_000))
            nanosleep(&ts, nil)
        }
    }
}

// MARK: CoreAudio device

func address(_ selector: AudioObjectPropertySelector,
             scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func getValue<T>(_ id: AudioObjectID, _ addr: AudioObjectPropertyAddress, as: T.Type, zero: T) -> T? {
    var addr = addr
    var value = zero
    var size = UInt32(MemoryLayout<T>.size)
    let status = withUnsafeMutableBytes(of: &value) {
        AudioObjectGetPropertyData(id, &addr, 0, nil, &size, $0.baseAddress!)
    }
    return status == noErr ? value : nil
}

func deviceName(_ id: AudioObjectID) -> String? {
    var addr = address(kAudioObjectPropertyName)
    var name: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &name) == noErr, let name else { return nil }
    return name.takeRetainedValue() as String
}

func outputStreams(_ id: AudioObjectID) -> [AudioObjectID] {
    var addr = address(kAudioDevicePropertyStreams, scope: kAudioObjectPropertyScopeOutput)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
    var streams = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &streams) == noErr else { return [] }
    return streams
}

func findDevice(named wanted: String?) -> AudioObjectID? {
    guard let wanted else {
        return getValue(AudioObjectID(kAudioObjectSystemObject),
                        address(kAudioHardwarePropertyDefaultOutputDevice),
                        as: AudioObjectID.self, zero: 0)
    }
    var addr = address(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    let system = AudioObjectID(kAudioObjectSystemObject)
    guard AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size) == noErr else { return nil }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids) == noErr else { return nil }
    return ids.first { deviceName($0) == wanted && !outputStreams($0).isEmpty }
}

func outputChannels(_ id: AudioObjectID) -> Int {
    var addr = address(kAudioDevicePropertyStreamConfiguration, scope: kAudioObjectPropertyScopeOutput)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: 16)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, raw) == noErr else { return 0 }
    let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return list.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func setOSStatus(_ status: OSStatus, _ what: String) {
    if status != noErr { fail("\(what) failed (OSStatus \(status))", code: 4) }
}

nonisolated(unsafe) var stopUnit: () -> Void = {}

/// Everything after the device is chosen: AUHAL on it, 512-frame IO, the render callback.
func startAudioUnit(_ engine: Engine, device: AudioObjectID) {
    var desc = AudioComponentDescription(
        componentType: kAudioUnitType_Output, componentSubType: kAudioUnitSubType_HALOutput,
        componentManufacturer: kAudioUnitManufacturer_Apple, componentFlags: 0, componentFlagsMask: 0)
    guard let component = AudioComponentFindNext(nil, &desc) else { fail("no HAL output component", code: 4) }
    var maybeUnit: AudioUnit?
    setOSStatus(AudioComponentInstanceNew(component, &maybeUnit), "AudioComponentInstanceNew")
    guard let unit = maybeUnit else { fail("no audio unit", code: 4) }

    var on: UInt32 = 1
    var off: UInt32 = 0
    setOSStatus(AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &on, 4), "enable output")
    setOSStatus(AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &off, 4), "disable input")
    var dev = device
    setOSStatus(AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &dev, 4), "set device")

    var frames = UInt32(engine.bufferFrames)
    var bufferAddr = address(kAudioDevicePropertyBufferFrameSize)
    setOSStatus(AudioObjectSetPropertyData(device, &bufferAddr, 0, nil, 4, &frames), "set IO buffer size")
    engine.bufferFrames = Int(getValue(device, bufferAddr, as: UInt32.self, zero: frames) ?? frames)

    // Mono is copied to the first two channels so one speaker is not left silent.
    let channels = max(1, min(2, outputChannels(device)))
    var format = AudioStreamBasicDescription(
        mSampleRate: Float64(engine.sampleRate), mFormatID: kAudioFormatLinearPCM,
        mFormatFlags: kAudioFormatFlagsNativeFloatPacked | kAudioFormatFlagIsNonInterleaved,
        mBytesPerPacket: 4, mFramesPerPacket: 1, mBytesPerFrame: 4,
        mChannelsPerFrame: UInt32(channels), mBitsPerChannel: 32, mReserved: 0)
    setOSStatus(AudioUnitSetProperty(unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0,
                                     &format, UInt32(MemoryLayout<AudioStreamBasicDescription>.size)), "set client format")

    // Latency in frames at the device rate: device + safety offset + stream.
    let outScope = kAudioObjectPropertyScopeOutput
    let deviceFrames = Int(getValue(device, address(kAudioDevicePropertyLatency, scope: outScope), as: UInt32.self, zero: 0) ?? 0)
        + Int(getValue(device, address(kAudioDevicePropertySafetyOffset, scope: outScope), as: UInt32.self, zero: 0) ?? 0)
        + (outputStreams(device).first.flatMap {
            getValue($0, address(kAudioStreamPropertyLatency), as: UInt32.self, zero: 0)
        }.map(Int.init) ?? 0)
    let deviceRate = getValue(device, address(kAudioDevicePropertyNominalSampleRate), as: Float64.self, zero: 0) ?? 0
    engine.deviceLatencyNs = deviceRate > 0 ? Int64(Double(deviceFrames) / deviceRate * 1e9) : 0

    var overload = address(kAudioDeviceProcessorOverload)
    let selfPtr = Unmanaged.passUnretained(engine).toOpaque()
    setOSStatus(AudioObjectAddPropertyListener(device, &overload, { _, _, _, client in
        Unmanaged<Engine>.fromOpaque(client!).takeUnretainedValue().overloads.wrappingAdd(1, ordering: .relaxed)
        return noErr
    }, selfPtr), "overload listener")

    var callback = AURenderCallbackStruct(inputProc: { client, _, timestamp, _, inFrames, ioData in
        let engine = Unmanaged<Engine>.fromOpaque(client).takeUnretainedValue()
        guard let ioData else { return noErr }
        let list = UnsafeMutableAudioBufferListPointer(ioData)
        guard let first = list[0].mData?.assumingMemoryBound(to: Float.self) else { return noErr }
        let now = uptimeNs()
        let hostNs = Int64(truncatingIfNeeded: AudioConvertHostTimeToNanos(timestamp.pointee.mHostTime))
        engine.render(first, frames: Int(inFrames), delayNs: max(0, hostNs - now) + engine.deviceLatencyNs)
        if list.count > 1 {
            for c in 1..<min(list.count, 8) {
                if let dst = list[c].mData { memcpy(dst, first, Int(inFrames) * 4) }
            }
        }
        return noErr
    }, inputProcRefCon: selfPtr)
    setOSStatus(AudioUnitSetProperty(unit, kAudioUnitProperty_SetRenderCallback, kAudioUnitScope_Input, 0,
                                     &callback, UInt32(MemoryLayout<AURenderCallbackStruct>.size)), "set render callback")
    setOSStatus(AudioUnitInitialize(unit), "AudioUnitInitialize")
    setOSStatus(AudioOutputUnitStart(unit), "AudioOutputUnitStart")
    stopUnit = { AudioOutputUnitStop(unit) }
}

// MARK: main

var rate = 48000
var ringSamples = 131_072
var bufferFrames = 512
var deviceArg: String?
var nullDevice = false
var nullCapture: String?
var args = CommandLine.arguments.dropFirst().makeIterator()
while let arg = args.next() {
    switch arg {
    case "--rate": rate = Int(args.next() ?? "") ?? rate
    case "--ring-samples": ringSamples = Int(args.next() ?? "") ?? ringSamples
    case "--buffer-frames": bufferFrames = Int(args.next() ?? "") ?? bufferFrames
    case "--device": deviceArg = args.next()
    case "--null-device": nullDevice = true
    case "--null-capture": nullCapture = args.next()
    default: fail("unknown option \(arg)", code: 1)
    }
}
if ringSamples <= 0 || ringSamples & (ringSamples - 1) != 0 { fail("--ring-samples must be a power of two", code: 1) }
if bufferFrames <= 0 || bufferFrames > kMaxFrames { fail("--buffer-frames must be 1...\(kMaxFrames)", code: 1) }

signal(SIGPIPE, SIG_IGN)
let engine = Engine(sampleRate: rate, ringSize: ringSamples, bufferFrames: bufferFrames)

func finish() -> Never {
    // stdin closed: let the last declick play out, then stop.
    usleep(40_000)
    stopUnit()
    exit(0)
}

if nullDevice {
    Thread.detachNewThread { runNullDevice(engine, capture: nullCapture) }
} else {
    guard let device = findDevice(named: deviceArg) else {
        fail("output device not found: \(deviceArg ?? "<default>")", code: 2)
    }
    startAudioUnit(engine, device: device)
}

var ready = ByteBuffer()
ready.frame(type: 0x81) { b in
    b.put(UInt32(engine.sampleRate))
    b.put(UInt32(engine.bufferFrames))
    b.put(engine.deviceLatencyNs)
    b.put(uptimeNs())
}
writeAll(1, ready.bytes)
Thread.detachNewThread { runStdoutWriter(engine) }
Thread.detachNewThread { runStdinReader(engine, finish: finish) }
CFRunLoopRun()

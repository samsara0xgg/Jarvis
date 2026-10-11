import AVFoundation
import Foundation

/// Where a piece's audio goes while it is open.
protocol LifelogSink: AnyObject {
    func write(_ b: AVAudioPCMBuffer) throws
    /// Ends the file properly (a killed one is left as it is).
    func finish()
}

/// Ogg Opus (RFC 7845 on RFC 3533), written page by page: a killed app leaves a file that plays up to its last complete page.
enum OggOpus {
    /// Ogg's CRC-32: polynomial 0x04C11DB7, not reflected, initial value 0, no final xor.
    static func crc(_ bytes: [UInt8]) -> UInt32 {
        var c: UInt32 = 0
        for b in bytes { c = (c << 8) ^ table[Int((c >> 24) ^ UInt32(b))] }
        return c
    }
    private static let table: [UInt32] = (0..<256).map { i in
        var r = UInt32(i) << 24
        for _ in 0..<8 { r = r & 0x8000_0000 != 0 ? (r << 1) ^ 0x04C1_1DB7 : r << 1 }
        return r
    }

    static let bos: UInt8 = 2, eos: UInt8 = 4

    /// Lacing values of one packet: runs of 255, then the remainder (0 when the size is a multiple of 255).
    static func lacing(_ size: Int) -> [UInt8] { Array(repeating: 255, count: size / 255) + [UInt8(size % 255)] }

    /// One page. At most 255 lacing values in all (the caller keeps to that); no packet continues over a page.
    static func page(serial: UInt32, seq: UInt32, granule: Int64, flags: UInt8, packets: [[UInt8]]) -> [UInt8] {
        let laces = packets.flatMap { lacing($0.count) }
        precondition(laces.count <= 255)
        var p: [UInt8] = Array("OggS".utf8) + [0, flags]
        p += le(UInt64(bitPattern: granule), 8) + le(UInt64(serial), 4) + le(UInt64(seq), 4) + [0, 0, 0, 0]
        p += [UInt8(laces.count)] + laces + packets.flatMap { $0 }
        p.replaceSubrange(22..<26, with: le(UInt64(crc(p)), 4))
        return p
    }

    private static func le(_ v: UInt64, _ n: Int) -> [UInt8] { (0..<n).map { UInt8(truncatingIfNeeded: v >> (8 * UInt64($0))) } }

    /// The identification header: version 1, 2 channels, pre-skip, 48000 Hz, gain 0, mapping family 0.
    static func head(preSkip: Int) -> [UInt8] {
        Array("OpusHead".utf8) + [1, 2] + le(UInt64(preSkip), 2) + le(48_000, 4) + le(0, 2) + [0]
    }
    /// The comment header: vendor "Jarvis", no comments.
    static let tags: [UInt8] = Array("OpusTags".utf8) + le(6, 4) + Array("Jarvis".utf8) + le(0, 4)

    /// What a file holds up to its last complete page (a truncated or damaged page ends it).
    struct Scan: Equatable {
        var validLength = 0
        var preSkip = 0
        /// granule position of the last audio page: 48 kHz samples through its end, pre-skip included
        var granule: Int64 = 0
        var durationMs: Int64 { max(0, (granule - Int64(preSkip)) * 1000 / 48_000) }
    }

    static func scan(_ d: [UInt8]) -> Scan {
        var s = Scan(), i = 0, pageNo = 0
        while i + 27 <= d.count, d[i..<i + 4].elementsEqual("OggS".utf8), d[i + 4] == 0 {
            let n = Int(d[i + 26])
            guard i + 27 + n <= d.count else { break }
            let end = i + 27 + n + d[(i + 27)..<(i + 27 + n)].reduce(0) { $0 + Int($1) }
            guard end <= d.count else { break }
            var page = Array(d[i..<end])
            let stored = page[22..<26].enumerated().reduce(UInt32(0)) { $0 | UInt32($1.element) << (8 * UInt32($1.offset)) }
            page.replaceSubrange(22..<26, with: [0, 0, 0, 0])
            guard crc(page) == stored else { break }
            if pageNo == 0 {
                guard page.count >= 27 + n + 12, page[(27 + n)..<(27 + n + 8)].elementsEqual("OpusHead".utf8) else { break }
                s.preSkip = Int(page[27 + n + 10]) | Int(page[27 + n + 11]) << 8
            } else if pageNo >= 2 {
                let g = (0..<8).reduce(UInt64(0)) { $0 | UInt64(page[6 + $1]) << (8 * UInt64($1)) }
                if Int64(bitPattern: g) >= 0 { s.granule = Int64(bitPattern: g) }
            }
            s.validLength = end
            i = end
            pageNo += 1
        }
        return s
    }
}

/// Stereo 48 kHz Float32 in, Opus (about 48 kbps) out, muxed into an Ogg file as it comes: a page for about every second of audio.
final class OggOpusSink: LifelogSink {
    struct NoEncoder: Error {}
    static let inFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 2, interleaved: false)!
    private static let packetFrames: Int64 = 960
    private static let packetsPerPage = 50

    private let handle: FileHandle
    private let conv: AVAudioConverter
    private let outFormat: AVAudioFormat
    private let serial = UInt32.random(in: 1...UInt32.max)
    private var seq: UInt32 = 0
    private let preSkip: Int
    private var pending: [[UInt8]] = []
    private var pendingLaces = 0
    private var encodedFrames: Int64 = 0
    private var realFrames: Int64 = 0

    init(url: URL) throws {
        var d = AudioStreamBasicDescription(mSampleRate: 48_000, mFormatID: kAudioFormatOpus, mFormatFlags: 0, mBytesPerPacket: 0,
                                            mFramesPerPacket: UInt32(Self.packetFrames), mBytesPerFrame: 0, mChannelsPerFrame: 2,
                                            mBitsPerChannel: 0, mReserved: 0)
        guard let out = AVAudioFormat(streamDescription: &d), let c = AVAudioConverter(from: Self.inFormat, to: out) else { throw NoEncoder() }
        c.bitRate = 48_000
        outFormat = out
        conv = c
        preSkip = c.primeInfo.leadingFrames > 0 ? Int(c.primeInfo.leadingFrames) : 312
        guard FileManager.default.createFile(atPath: url.path, contents: nil) else { throw CocoaError(.fileWriteUnknown) }
        handle = try FileHandle(forWritingTo: url)
        try emit(OggOpus.page(serial: serial, seq: next(), granule: 0, flags: OggOpus.bos, packets: [OggOpus.head(preSkip: preSkip)]))
        try emit(OggOpus.page(serial: serial, seq: next(), granule: 0, flags: 0, packets: [OggOpus.tags]))
    }

    func write(_ b: AVAudioPCMBuffer) throws {
        realFrames += Int64(b.frameLength)
        try encode(b, last: false)
    }

    func finish() {
        try? encode(nil, last: true)
        try? flush(eos: true)
        try? handle.close()
    }

    private func next() -> UInt32 { defer { seq += 1 }; return seq }
    private func emit(_ page: [UInt8]) throws { try handle.write(contentsOf: Data(page)) }

    private func encode(_ b: AVAudioPCMBuffer?, last: Bool) throws {
        var given = false
        while true {
            let out = AVAudioCompressedBuffer(format: outFormat, packetCapacity: 16, maximumPacketSize: conv.maximumOutputPacketSize)
            var err: NSError?
            let status = conv.convert(to: out, error: &err) { _, st in
                if given || b == nil { st.pointee = last ? .endOfStream : .noDataNow; return nil }
                given = true
                st.pointee = .haveData
                return b
            }
            if let err { throw err }
            if let descs = out.packetDescriptions {
                for i in 0..<Int(out.packetCount) {
                    let d = descs[i]
                    let p = Array(UnsafeRawBufferPointer(start: out.data + Int(d.mStartOffset), count: Int(d.mDataByteSize)))
                    let laces = OggOpus.lacing(p.count).count
                    if pendingLaces + laces > 255 { try flush(eos: false) }
                    pending.append(p)
                    pendingLaces += laces
                    encodedFrames += Self.packetFrames
                    if pending.count >= Self.packetsPerPage { try flush(eos: false) }
                }
            }
            if status == .error || out.packetCount == 0 || status != .haveData { break }
        }
    }

    /// The pending packets as a page. Its granule position is the 48 kHz samples decoded through its end; on the last page, the real audio
    /// only (the encoder pads its last packet), so a player drops the padding.
    private func flush(eos: Bool) throws {
        guard !pending.isEmpty || eos else { return }
        let granule = eos ? min(encodedFrames, Int64(preSkip) + realFrames) : encodedFrames
        try emit(OggOpus.page(serial: serial, seq: next(), granule: granule, flags: eos ? OggOpus.eos : 0, packets: pending))
        pending = []
        pendingLaces = 0
    }
}

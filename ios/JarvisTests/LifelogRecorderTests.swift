import AVFoundation
import XCTest
@testable import Jarvis

/// The recorder's pure parts: where a 10-minute piece ends, what the writer makes of a microphone's buffers, and that both file kinds can be made.
final class LifelogRecorderTests: XCTestCase {
    private func utc(_ h: Int, _ m: Int, _ s: Int) -> Int64 { Int64(((h * 60 + m) * 60 + s) * 1000) }

    func testAPieceEndsWhenTheLocalClockCrossesAMultipleOfTenMinutes() {
        XCTAssertEqual(LifelogClock.nextBoundary(after: utc(12, 3, 20), offsetSec: 0), utc(12, 10, 0))
        XCTAssertEqual(LifelogClock.nextBoundary(after: utc(12, 10, 0), offsetSec: 0), utc(12, 20, 0), "on the mark, the next one")
        XCTAssertEqual(LifelogClock.nextBoundary(after: utc(23, 59, 59), offsetSec: 0), utc(24, 0, 0))
        XCTAssertEqual(LifelogClock.nextBoundary(after: utc(6, 37, 0), offsetSec: 19_800), utc(6, 40, 0), "+05:30: 12:07 local ends at 12:10 local")
        XCTAssertEqual(LifelogClock.nextBoundary(after: utc(1, 7, 0) + 86_400_000, offsetSec: -25_200), utc(1, 10, 0) + 86_400_000, "-07:00")
    }

    private func sine(rate: Double, channels: AVAudioChannelCount, frames: Int) -> AVAudioPCMBuffer {
        let f = AVAudioFormat(standardFormatWithSampleRate: rate, channels: channels)!
        let b = AVAudioPCMBuffer(pcmFormat: f, frameCapacity: AVAudioFrameCount(frames))!
        b.frameLength = AVAudioFrameCount(frames)
        for c in 0..<Int(channels) { for i in 0..<frames { b.floatChannelData![c][i] = 0.5 * sinf(Float(i) * 2 * .pi * 440 / Float(rate)) } }
        return b
    }

    func testMonoAtAnyRateBecomesStereo48kWithBothChannelsTheSame() throws {
        for rate in [16_000.0, 44_100.0, 48_000.0] {
            let r = LifelogResampler()
            var out = 0
            for _ in 0..<10 {                                      // chunks, as a tap delivers them, through one converter
                let o = try XCTUnwrap(r.convert(sine(rate: rate, channels: 1, frames: Int(rate / 10))))
                XCTAssertEqual(o.format, LifelogWriter.format)
                let l = o.floatChannelData![0], rr = o.floatChannelData![1]
                var peak: Float = 0
                for i in 0..<Int(o.frameLength) { XCTAssertEqual(l[i], rr[i]); peak = max(peak, abs(l[i])) }
                XCTAssertGreaterThan(peak, 0.2, "not silence at \(rate)")
                out += Int(o.frameLength)
            }
            XCTAssertEqual(Double(out), 48_000, accuracy: 600, "one second in, one second out at \(rate)")
        }
    }

    func testStereo48kPassesThroughUntouched() throws {
        let b = sine(rate: 48_000, channels: 2, frames: 4800)
        XCTAssertTrue(try XCTUnwrap(LifelogResampler().convert(b)) === b)
    }

    // MARK: Ogg Opus

    func testOggCRCHasTheOggParameters() {
        // polynomial 0x04C11DB7, not reflected, initial value 0, no final xor (the value was computed independently, in Python)
        XCTAssertEqual(OggOpus.crc(Array("123456789".utf8)), 0x89A1_897F)
        XCTAssertEqual(OggOpus.crc([UInt8](repeating: 0, count: 27)), 0)
    }

    func testLacingOfPacketsAtTheEdgesOf255() {
        XCTAssertEqual(OggOpus.lacing(0), [0])
        XCTAssertEqual(OggOpus.lacing(254), [254])
        XCTAssertEqual(OggOpus.lacing(255), [255, 0], "a packet of exactly 255 ends with a 0")
        XCTAssertEqual(OggOpus.lacing(256), [255, 1])
        XCTAssertEqual(OggOpus.lacing(510), [255, 255, 0])
        XCTAssertEqual(OggOpus.lacing(1275), [255, 255, 255, 255, 255, 0])
    }

    func testAPageCarriesItsLacingGranuleSerialAndCRC() {
        let pk: [[UInt8]] = [[UInt8](repeating: 7, count: 255), [UInt8](repeating: 8, count: 256), [UInt8](repeating: 9, count: 510)]
        let p = OggOpus.page(serial: 0xAABBCCDD, seq: 5, granule: 96_312, flags: OggOpus.eos, packets: pk)
        XCTAssertEqual(Array(p[0..<4]), Array("OggS".utf8))
        XCTAssertEqual(p[5], OggOpus.eos)
        XCTAssertEqual(Array(p[6..<14]), [0x38, 0x78, 0x01, 0, 0, 0, 0, 0], "granule 96312, little-endian")
        XCTAssertEqual(Array(p[14..<18]), [0xDD, 0xCC, 0xBB, 0xAA])
        XCTAssertEqual(p[18], 5)
        XCTAssertEqual(Array(p[26..<(27 + 7)]), [7, 255, 0, 255, 1, 255, 255, 0], "7 lacing values for the three packets")
        XCTAssertEqual(p.count, 27 + 7 + 255 + 256 + 510)
        var z = p
        z.replaceSubrange(22..<26, with: [0, 0, 0, 0])
        XCTAssertEqual(Array(p[22..<26]), (0..<4).map { UInt8(truncatingIfNeeded: OggOpus.crc(z) >> (8 * UInt32($0))) })
    }

    private func ogg(seconds: Double, to url: URL) throws {
        try? FileManager.default.removeItem(at: url)
        let sink = try OggOpusSink(url: url)
        let total = Int(seconds * 48_000)
        var t = 0
        while t < total {
            let n = min(4096, total - t)
            let b = AVAudioPCMBuffer(pcmFormat: OggOpusSink.inFormat, frameCapacity: AVAudioFrameCount(n))!
            b.frameLength = AVAudioFrameCount(n)
            for i in 0..<n {
                b.floatChannelData![0][i] = 0.4 * sinf(Float(t + i) * 2 * Float.pi * 440 / 48_000)
                b.floatChannelData![1][i] = 0.4 * sinf(Float(t + i) * 2 * Float.pi * 660 / 48_000)
            }
            try sink.write(b)
            t += n
        }
        sink.finish()
    }

    func testThreeSecondsOfToneBecomeAnOggOfThreeSecondsWithOnePagePerSecond() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("lifelog-3s.ogg")
        try ogg(seconds: 3, to: url)
        let d = [UInt8](try Data(contentsOf: url))
        let s = OggOpus.scan(d)
        XCTAssertEqual(s.validLength, d.count, "every page complete, every CRC right")
        XCTAssertEqual(s.preSkip, 312)
        XCTAssertEqual(s.granule, 144_312, "3 s of 48 kHz samples plus the pre-skip, on the last page")
        XCTAssertEqual(s.durationMs, 3_000)
        var granules: [Int64] = [], flags: [UInt8] = [], i = 0
        while i < d.count {
            let n = Int(d[i + 26])
            granules.append(Int64(bitPattern: (0..<8).reduce(UInt64(0)) { $0 | UInt64(d[i + 6 + $1]) << (8 * UInt64($1)) }))
            flags.append(d[i + 5])
            i += 27 + n + d[(i + 27)..<(i + 27 + n)].reduce(0) { $0 + Int($1) }
        }
        XCTAssertEqual(granules, [0, 0, 48_000, 96_000, 144_000, 144_312], "head, tags, three seconds of 50 packets, the rest")
        XCTAssertEqual(flags, [OggOpus.bos, 0, 0, 0, 0, OggOpus.eos])
        print("lifelog-ogg: \(url.path)")
    }

    func testAnOggCutInsideAPageCountsOnlyTheCompletePages() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("lifelog-cut.ogg")
        try ogg(seconds: 3, to: url)
        let d = [UInt8](try Data(contentsOf: url))
        let cut = Array(d[0..<(d.count - 3_000)])          // inside the page that ends at 144000
        let s = OggOpus.scan(cut)
        XCTAssertEqual(s.granule, 96_000)
        XCTAssertEqual(s.durationMs, (96_000 - 312) * 1000 / 48_000)
        XCTAssertLessThan(s.validLength, cut.count)
        var bad = d
        bad[bad.count - 100] ^= 0xFF                       // one wrong byte in the last page
        XCTAssertEqual(OggOpus.scan(bad).granule, 144_000)
    }

    /// Writes one second through a sink of the given kind; nil if this device cannot make that kind.
    private func writeOneSecond(_ format: AudioFormatID, in dir: URL, id: String) throws -> String? {
        guard let made = try? LifelogWriter.create(in: dir, id: id, format: format) else { return nil }
        let r = LifelogResampler()
        for _ in 0..<10 { try made.0.write(XCTUnwrap(r.convert(sine(rate: 44_100, channels: 1, frames: 4410)))) }
        made.0.finish()
        return made.ext
    }

    func testTheOpusSinkIsMadeAndAnAACFallbackIsToo() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("lifelog-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        var kinds = 0
        for format in LifelogWriter.formats {
            let id = "t\(format)"
            guard let ext = try writeOneSecond(format, in: dir, id: id) else {
                XCTAssertNotEqual(format, kAudioFormatOpus, "Opus must work")
                continue
            }
            XCTAssertEqual(ext, format == kAudioFormatOpus ? "ogg" : "m4a")
            let url = dir.appendingPathComponent("\(id).\(ext)")
            if ext == "ogg" {
                XCTAssertEqual(Double(OggOpus.scan([UInt8](try Data(contentsOf: url))).durationMs), 1_000, accuracy: 20)
            } else {
                let f = try AVAudioFile(forReading: url)
                XCTAssertEqual(Double(f.length), 48_000, accuracy: 2_000)
            }
            kinds += 1
        }
        XCTAssertGreaterThanOrEqual(kinds, 2, "Opus and at least one AAC kind")
    }
}

import AVFoundation
import UIKit
import Network
import VideoToolbox

/// One connection to Jarvis Remote on his Mac: the screen comes in as HEVC and goes straight to a display layer,
/// his touches and keys go out as small JSON events. The wire is desktop/remote's `Wire`, mirrored here.
final class RemoteSession {
    enum State: Equatable { case connecting, live(locked: Bool), refused, closed }

    private enum Wire {
        static let hello: UInt8 = 0x01, input: UInt8 = 0x02, keyframe: UInt8 = 0x03, got: UInt8 = 0x04, settings: UInt8 = 0x05
        static let info: UInt8 = 0x10, params: UInt8 = 0x11, frame: UInt8 = 0x12, refused: UInt8 = 0x13, stats: UInt8 = 0x14
    }

    let layer = AVSampleBufferDisplayLayer()
    /// Main queue: the state, and the stream's pixel size once known.
    var onState: (State) -> Void = { _ in }
    var onSize: (CGSize) -> Void = { _ in }
    /// Main queue, once a second: what the stream is really doing, for the 画面 panel.
    var onStats: (StreamStats) -> Void = { _ in }
    private var history: [Int] = []

    private let q = DispatchQueue(label: "jarvis.remote")
    private var conn: NWConnection?
    private var inbox = Data()
    private var format: CMVideoFormatDescription?
    private var gone = false

    init() {
        layer.videoGravity = .resizeAspect
        layer.backgroundColor = UIColor.black.cgColor
    }

    /// `width` pins the stream's width, 0 lets the Mac choose; `prefer` ("fps" | "quality") says what gives when the link is short.
    func start(host: String, port: UInt16, token: String, curtain: Bool, width: Int, prefer: String) {
        guard let p = NWEndpoint.Port(rawValue: port) else { return }
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        let c = NWConnection(host: NWEndpoint.Host(host), port: p, using: NWParameters(tls: nil, tcp: tcp))
        conn = c
        state(.connecting)
        c.stateUpdateHandler = { [weak self] s in
            guard let self else { return }
            switch s {
            case .ready:
                let hello: [String: Any] = ["token": token, "curtain": curtain, "acks": true, "width": width, "prefer": prefer]
                self.send(Wire.hello, try! JSONSerialization.data(withJSONObject: hello))
                self.receive()
            case .failed, .waiting: self.close()
            default: break
            }
        }
        c.start(queue: q)
    }

    func stop() {
        q.async { self.gone = true; self.conn?.cancel() }
    }

    /// One input event, see desktop/remote Input.swift for the shapes.
    func input(_ event: [String: Any]) {
        guard let d = try? JSONSerialization.data(withJSONObject: event) else { return }
        q.async { self.send(Wire.input, d) }
    }

    /// The same two choices as in `start`, applied live.
    func settings(width: Int, prefer: String) {
        let d = try! JSONSerialization.data(withJSONObject: ["width": width, "prefer": prefer])
        q.async { self.send(Wire.settings, d) }
    }

    private func send(_ type: UInt8, _ payload: Data) {
        var d = Data([type])
        withUnsafeBytes(of: UInt32(payload.count).bigEndian) { d.append(contentsOf: $0) }
        d.append(payload)
        conn?.send(content: d, completion: .idempotent)
    }

    private func receive() {
        conn?.receive(minimumIncompleteLength: 1, maximumLength: 1 << 20) { [weak self] data, _, done, error in
            guard let self else { return }
            if let data { self.inbox.append(data); self.drain() }
            if done || error != nil { self.close() } else { self.receive() }
        }
    }

    private func drain() {
        while inbox.count >= 5 {
            let s = inbox.startIndex
            let len = Int(inbox[s + 1]) << 24 | Int(inbox[s + 2]) << 16 | Int(inbox[s + 3]) << 8 | Int(inbox[s + 4])
            guard inbox.count >= 5 + len else { return }
            let type = inbox[s]
            let body = inbox.subdata(in: s + 5 ..< s + 5 + len)
            inbox.removeSubrange(s ..< s + 5 + len)
            handle(type, body)
        }
    }

    private func handle(_ type: UInt8, _ body: Data) {
        switch type {
        case Wire.info:
            guard let j = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] else { return }
            let size = CGSize(width: j["w"] as? Double ?? 0, height: j["h"] as? Double ?? 0)
            let locked = j["locked"] as? Bool ?? false
            DispatchQueue.main.async { self.onSize(size) }
            state(.live(locked: locked))
        case Wire.params:
            var sets: [Data] = [], i = body.startIndex
            while i + 2 <= body.endIndex {
                let n = Int(body[i]) << 8 | Int(body[i + 1])
                guard i + 2 + n <= body.endIndex else { break }
                sets.append(body.subdata(in: i + 2 ..< i + 2 + n))
                i += 2 + n
            }
            format = Self.format(sets)
        case Wire.frame:
            send(Wire.got, Data())   // every frame, shown or not: the Mac paces itself by these
            guard let format, body.count > 1 else { return }
            enqueue(body.subdata(in: body.startIndex + 1 ..< body.endIndex), format)
        case Wire.stats:
            guard let j = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] else { return }
            let fps = j["fps"] as? Int ?? 0
            history = Array((history + [fps]).suffix(10))
            let est = (j["est"] as? [String: Int] ?? [:]).reduce(into: [Int: Int]()) { if let w = Int($1.key) { $0[w] = $1.value } }
            let s = StreamStats(fps: fps, latencyMs: j["ms"] as? Int ?? 0, mbps: j["mbps"] as? Int ?? 0, width: j["w"] as? Int ?? 0,
                                estimates: est, history: history)
            DispatchQueue.main.async { self.onStats(s) }
        case Wire.refused:
            gone = true
            state(.refused)
            conn?.cancel()
        default:
            break
        }
    }

    private static func format(_ sets: [Data]) -> CMVideoFormatDescription? {
        guard sets.count >= 3 else { return nil }
        var out: CMVideoFormatDescription?
        let ptrs = sets.map { d -> UnsafeMutablePointer<UInt8> in
            let p = UnsafeMutablePointer<UInt8>.allocate(capacity: d.count)
            d.copyBytes(to: p, count: d.count)
            return p
        }
        defer { ptrs.forEach { $0.deallocate() } }
        CMVideoFormatDescriptionCreateFromHEVCParameterSets(allocator: nil, parameterSetCount: ptrs.count, parameterSetPointers: ptrs.map { UnsafePointer($0) },
                                                            parameterSetSizes: sets.map(\.count), nalUnitHeaderLength: 4, extensions: nil, formatDescriptionOut: &out)
        return out
    }

    private func enqueue(_ data: Data, _ format: CMVideoFormatDescription) {
        var block: CMBlockBuffer?
        guard CMBlockBufferCreateWithMemoryBlock(allocator: nil, memoryBlock: nil, blockLength: data.count, blockAllocator: nil, customBlockSource: nil,
                                                 offsetToData: 0, dataLength: data.count, flags: kCMBlockBufferAssureMemoryNowFlag, blockBufferOut: &block) == noErr,
              let block else { return }
        data.withUnsafeBytes { _ = CMBlockBufferReplaceDataBytes(with: $0.baseAddress!, blockBuffer: block, offsetIntoDestination: 0, dataLength: data.count) }
        var sb: CMSampleBuffer?
        var size = data.count
        guard CMSampleBufferCreateReady(allocator: nil, dataBuffer: block, formatDescription: format, sampleCount: 1, sampleTimingEntryCount: 0,
                                        sampleTimingArray: nil, sampleSizeEntryCount: 1, sampleSizeArray: &size, sampleBufferOut: &sb) == noErr,
              let sb else { return }
        if let att = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: true), CFArrayGetCount(att) > 0 {
            let d = unsafeBitCast(CFArrayGetValueAtIndex(att, 0), to: CFMutableDictionary.self)
            CFDictionarySetValue(d, Unmanaged.passUnretained(kCMSampleAttachmentKey_DisplayImmediately).toOpaque(), Unmanaged.passUnretained(kCFBooleanTrue).toOpaque())
        }
        let r = layer.sampleBufferRenderer
        if r.status == .failed {
            r.flush()
            send(Wire.keyframe, Data())
        }
        r.enqueue(sb)
    }

    private func close() {
        guard !gone else { return }
        gone = true
        conn?.cancel()
        state(.closed)
    }

    private func state(_ s: State) {
        DispatchQueue.main.async { self.onState(s) }
    }
}

/// One second of the stream, as the Mac measured it: frames a second, how long a frame took to arrive and be acked, the bitrate,
/// the width it streams at, the fps the Mac's encoder would reach at each width it offers, and the last ten seconds of fps.
struct StreamStats: Equatable {
    var fps = 0, latencyMs = 0, mbps = 0, width = 0
    var estimates: [Int: Int] = [:]
    var history: [Int] = []
}

import AVFoundation
import Foundation

// The wrist's microphone for a talk: 16 kHz mono Int16 in chunks of about 150 ms, each sent to the phone as a data message tagged with the talk (`WatchWire.audio`).
// The watch recognizes nothing itself; the phone's recognizer hears these.

final class WatchMic: @unchecked Sendable {
    private let engine = AVAudioEngine()
    private let send: @Sendable (Data) -> Void
    private let lock = NSLock()
    private var talk: UUID?                         // the talk the microphone is on for, none when it is off
    private var pending = Data()                    // converted samples not sent yet
    private var fake: Task<Void, Never>?
    private let chunk = 2400 * 2                    // 150 ms

    /// `send` takes one finished audio data message
    init(send: @escaping @Sendable (Data) -> Void) { self.send = send }

    /// Switch the microphone on for `talk`. `failed` is called when it cannot be (denied, or no input).
    func start(talk id: UUID, failed: @escaping @Sendable () -> Void) {
        stop()
        lock.withLock { talk = id; pending = Data() }
        #if DEBUG
        if UserDefaults.standard.bool(forKey: "watchFakeMic") { fakeTone(id); return }         // the simulator's wrist has no microphone
        #endif
        Task {
            guard await AVAudioApplication.requestRecordPermission() else { return failed() }
            do { try begin(id) } catch { stop(); failed() }
        }
    }

    private func begin(_ id: UUID) throws {
        guard lock.withLock({ talk == id }) else { return }               // he has stopped while the permission was asked
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.record, mode: .default)
        try session.setActive(true)
        let input = engine.inputNode
        let have = input.outputFormat(forBus: 0)
        guard have.sampleRate > 0, let want = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: WatchWire.sampleRate, channels: 1, interleaved: true),
              let converter = AVAudioConverter(from: have, to: want) else { throw CocoaError(.featureUnsupported) }
        input.installTap(onBus: 0, bufferSize: 2048, format: have) { [weak self] buffer, _ in self?.take(buffer, converter, want, id) }
        engine.prepare()
        try engine.start()
    }

    private func take(_ buffer: AVAudioPCMBuffer, _ converter: AVAudioConverter, _ want: AVAudioFormat, _ id: UUID) {
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * want.sampleRate / buffer.format.sampleRate) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: want, frameCapacity: capacity), let samples = out.int16ChannelData?[0] else { return }
        var fed = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true; status.pointee = .haveData
            return buffer
        }
        guard error == nil, out.frameLength > 0 else { return }
        append(Data(bytes: samples, count: Int(out.frameLength) * 2), id)
    }

    /// whole chunks go out as they fill
    private func append(_ samples: Data, _ id: UUID) {
        let ready: [Data] = lock.withLock {
            guard talk == id else { return [] }
            pending.append(samples)
            var out: [Data] = []
            while pending.count >= chunk { out.append(pending.prefix(chunk)); pending = Data(pending.dropFirst(chunk)) }
            return out
        }
        for c in ready { send(WatchWire.audio(talk: id, samples: c)) }
    }

    /// Switch it off. `flush`: what is left of the last chunk is sent first.
    func stop(flush: Bool = false) {
        let (id, rest): (UUID?, Data) = lock.withLock { defer { talk = nil; pending = Data() }; return (talk, pending) }
        fake?.cancel(); fake = nil
        if engine.isRunning { engine.inputNode.removeTap(onBus: 0); engine.stop(); try? AVAudioSession.sharedInstance().setActive(false) }
        if flush, let id, !rest.isEmpty { send(WatchWire.audio(talk: id, samples: rest)) }
    }

    #if DEBUG
    /// `-watchFakeMic 1`: a quiet tone instead of the microphone, in the same chunks, so that the link and the phone's side can be seen working without one
    private func fakeTone(_ id: UUID) {
        fake = Task { [send] in
            var phase = 0.0
            while !Task.isCancelled {
                var samples = Data()
                for _ in 0..<2400 {
                    var v = Int16(6000 * sin(phase)).littleEndian
                    withUnsafeBytes(of: &v) { samples.append(contentsOf: $0) }
                    phase += 2 * .pi * 440 / WatchWire.sampleRate
                }
                send(WatchWire.audio(talk: id, samples: samples))
                try? await Task.sleep(nanoseconds: 150_000_000)
            }
        }
    }
    #endif
}

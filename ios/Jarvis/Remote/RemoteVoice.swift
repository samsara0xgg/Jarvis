import AVFoundation

/// Hold to talk on the remote view: the iPad's microphone, heard by Qwen through the brain like every other utterance of his (ADR 0404),
/// and the words typed into the Mac when he lets go.
@MainActor final class RemoteVoice {
    private let engine = AVAudioEngine()
    private var stream: CloudStream?

    var canHear: Bool { CloudHearing.shared.available }

    func start(onWords: @escaping (String) -> Void) throws {
        let s = CloudStream(keepSeconds: 60)
        s.onWords = onWords
        s.open()
        stream = s
        let session = AVAudioSession.sharedInstance()
        if session.category != .playAndRecord && session.category != .record {
            try session.setCategory(.playAndRecord, options: [.defaultToSpeaker, .mixWithOthers])
        }
        try session.setActive(true)
        let input = engine.inputNode
        let down = HearDownsampler()
        input.installTap(onBus: 0, bufferSize: 1600, format: input.outputFormat(forBus: 0)) { buffer, _ in
            if let pcm = down.pcm(buffer) { s.push(pcm) }
        }
        try engine.start()
    }

    /// Qwen's words for what he said, or nil.
    func finish() async -> String? {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        guard let s = stream else { return nil }
        stream = nil
        return await s.finish().words
    }

    func cancel() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        stream?.cancel()
        stream = nil
    }
}

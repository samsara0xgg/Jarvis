import Foundation
import os

/// One line of the voice's story, to the unified log, to stderr (a console attached by devicectl shows stderr, not the log) and to Library/Caches/voice.log.
/// Never the token and never what was said, only counts.
enum VoiceTrace {
    private static let log = Logger(subsystem: "com.yilunshi.jarvis", category: "voice")
    static func note(_ s: String) {
        log.notice("\(s, privacy: .public)")
        fputs("voice: \(s)\n", stderr)
        #if !VOICE_HARNESS
        StallLog.append(s, to: "voice.log")             // read back from the phone after a live talk (devicectl copy from)
        #endif
    }
}

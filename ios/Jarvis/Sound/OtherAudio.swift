import AVFoundation
import CallKit

/// Another app has the sound: a call (the phone's, or an app's through CallKit) or another app's audio on (a Discord voice channel, music).
/// Jarvis takes the audio session from another app only when he talks to her; what would start on its own (the wake word, the day's
/// recording) waits while this says why. Reading it never touches the session.
enum OtherAudio {
    private static let calls = CXCallObserver()

    /// why the sound is another app's now, or nil
    static var why: String? {
        if calls.calls.contains(where: { !$0.hasEnded }) { return "a call is on" }
        let s = AVAudioSession.sharedInstance()
        if s.isOtherAudioPlaying { return "another app's audio is on" }
        if s.secondaryAudioShouldBeSilencedHint { return "another app holds the audio" }
        return nil
    }
}

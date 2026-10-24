import XCTest
@testable import Jarvis

/// The Mac's voice cues on the phone (Sound/SoundFeedback): voice-exit is voice-enter backwards less its first 0.12 s, and a cue asked
/// for while the voice lets the session go plays on resume only if it is that recent.
final class SoundFeedbackTests: XCTestCase {
    func testVoiceExitIsTheEnterWavBackwardsLessItsFirstPointTwelveSeconds() {
        let enter = SoundFeedback.enter, exit = SoundFeedback.exit
        // the wav is 0.78 s at 48 kHz: about 34 400 samples at the cues' 44.1 kHz
        XCTAssertEqual(Double(enter.count) / SoundSynth.sampleRate, 0.78, accuracy: 0.01)
        XCTAssertEqual(enter.count - exit.count, 5292)          // 0.12 s at 44.1 kHz
        for i in 0..<64 { XCTAssertEqual(exit[i], enter[exit.count - 1 - i]) }
        XCTAssertEqual(exit.last, enter.first)
        // the render Sound plays from is the same samples, in both channels
        let rendered = SoundSynth.render(.voiceExit)
        XCTAssertEqual(rendered[0], exit)
        XCTAssertEqual(rendered[1], exit)
    }

    func testAHeldCuePlaysOnResumeOnlyWithinPointThreeSeconds() {
        let asked = HeldCue(cue: .voiceExit, gain: SoundFeedback.gain, at: 100)
        XCTAssertTrue(asked.due(100.25))
        XCTAssertTrue(asked.due(100.3))
        XCTAssertFalse(asked.due(100.4))
    }
}

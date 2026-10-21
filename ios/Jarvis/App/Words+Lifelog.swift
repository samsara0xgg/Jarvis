import Foundation

extension Words {
    /// The fixed words of the day's recording (Lifelog/): the notification that tells him it stopped while the switch is on (LifelogAlarm).
    @MainActor enum Lifelog {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        static var stoppedTitle: String { pick(zh: "随身录音停了", en: "Recording stopped") }
        static func stoppedBody(_ why: String) -> String { pick(zh: why + "点一下接着录。", en: why + " Tap to keep recording.") }
        /// why, as far as the phone knows
        static var whySilent: String { pick(zh: "麦克风有一会儿没有声音进来了，可能被系统停了。", en: "No sound has come from the microphone for a while. iOS may have stopped it.") }
        static var whyBackground: String { pick(zh: "Jarvis 在后台，系统不让它重新打开麦克风。", en: "Jarvis is in the background and iOS won't let it reopen the microphone.") }
        static var whyInterrupted: String { pick(zh: "被系统打断了，没有自动接上。", en: "Something interrupted it and it didn't resume.") }
    }
}

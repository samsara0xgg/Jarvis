import Foundation

extension Words {
    /// Every fixed string of the watch app and its complications. The phone tells the watch the language (`WatchDay.language`).
    @MainActor enum Watch {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        static var name: String { pick(zh: "Jarvis", en: "Jarvis") }
    }
}

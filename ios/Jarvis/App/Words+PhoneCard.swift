import Foundation

extension Words {
    /// The fixed words of her card under her (Sky/DayTimelineTalk.swift).
    @MainActor enum PhoneCard {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        /// under an answer longer than the card shows: 聊天, at it
        static var goOn: String { pick(zh: "去聊天接着说 ›", en: "Continue in Chat ›") }
    }
}

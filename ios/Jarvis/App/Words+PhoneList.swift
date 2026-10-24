import Foundation

extension Words {
    /// The strings of the 今天 page's list (Sky/DayTimelineRowViews.swift).
    @MainActor enum PhoneList {
        /// what VoiceOver says of the warm dot on the rail where the list stands at the present
        static func now(_ time: String) -> String { Words.pick(zh: "现在 \(time)", en: "Now \(time)") }
    }
}

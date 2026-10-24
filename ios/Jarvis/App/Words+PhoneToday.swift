import Foundation

extension Words {
    /// The strings of the 今天 page's pinned top (Sky/DayTimelineTop.swift, Sky/DayTimelineStrip.swift).
    @MainActor enum PhoneToday {
        /// the button at the right of the strip: the brain tidies today
        static var polish: String { Words.pick(zh: "润色", en: "Tidy up") }
        /// the day's numbers row, said as one
        static var chipsRow: String { Words.pick(zh: "一天的数字", en: "The day's numbers") }
    }
}

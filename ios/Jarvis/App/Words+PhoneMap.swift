import Foundation

extension Words {
    /// The strings of the map page's top strip (Sky/FootprintDayParts.swift).
    @MainActor enum PhoneMap {
        /// the button at the right of the strip: the map goes back to where he is now
        static var locate: String { Words.pick(zh: "回到我在哪", en: "Back to where I am") }
        /// the line under her: the day, then the page's name
        static func title(date: String) -> String { date + " · " + Words.Footprint.name }
    }
}

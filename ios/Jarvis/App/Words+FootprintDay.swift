import Foundation

extension Words {
    /// The strings of the footprint's one-day screen that `Words.Footprint` does not hold.
    @MainActor enum FootprintDay {
        /// how he moved, as the word before a distance (`开车 21.7 km`); nil for a way the page has no word for
        static func moving(_ activity: String?) -> String? {
            switch activity {
            case "automotive": Words.pick(zh: "开车", en: "Driving")
            case "walking": Words.pick(zh: "步行", en: "Walking")
            case "running": Words.pick(zh: "跑步", en: "Running")
            case "cycling": Words.pick(zh: "骑车", en: "Cycling")
            default: nil
            }
        }
        static var unnamedPlace: String { Words.pick(zh: "没名字的地方", en: "Unnamed place") }
        /// a day's one outing when the brain could not work out how fast he went
        static func summaryOneBare(title: String, distance: String) -> String {
            Words.pick(zh: "\(title)，\(distance)。", en: "\(title), \(distance).")
        }
        /// two sentences one after the other: nothing between them in Chinese, a space in English
        static func then(_ first: String, _ second: String) -> String { Words.pick(zh: first + second, en: first + " " + second) }
    }
}

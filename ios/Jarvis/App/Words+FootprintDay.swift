import Foundation

extension Words {
    /// The strings of the footprint's one-day screen that `Words.Footprint` does not hold.
    @MainActor enum FootprintDay {
        static var unnamedPlace: String { Words.pick(zh: "没名字的地方", en: "Unnamed place") }
        /// a day's one outing when the brain could not work out how fast he went
        static func summaryOneBare(title: String, distance: String) -> String {
            Words.pick(zh: "\(title)，\(distance)。", en: "\(title), \(distance).")
        }
        /// two sentences one after the other: nothing between them in Chinese, a space in English
        static func then(_ first: String, _ second: String) -> String { Words.pick(zh: first + second, en: first + " " + second) }
    }
}

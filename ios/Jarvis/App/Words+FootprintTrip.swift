import Foundation

extension Words.Footprint {
    /// the unit under a heart rate that is the trip's average
    static var average: String { Words.pick(zh: "平均", en: "avg") }

    /// the day of an outing in the zone the brain keeps it in: "10月20日 周二" in Chinese, "Tue, Oct 20" in English
    static func dateLine(_ date: Date, in cal: Calendar) -> String {
        let f = DateFormatter()
        f.calendar = cal; f.timeZone = cal.timeZone
        f.locale = Locale(identifier: Words.shared.language == .zh ? "zh_CN" : "en_US")
        f.setLocalizedDateFormatFromTemplate("MMMdEEE")
        return f.string(from: date)
    }
}

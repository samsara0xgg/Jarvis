import Foundation

extension Words {
    /// The fixed words of a row of the day opened, by what it is (Sky/RowStage.swift, Sky/DayTimelineOpen.swift).
    @MainActor enum PhoneRow {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        static var onTheWay: String { pick(zh: "路上", en: "On the way") }
        static var seeOnMap: String { pick(zh: "在地图里看 ›", en: "See on the map ›") }
        static var theNight: String { pick(zh: "这一晚", en: "The night") }
        static var fellAsleep: String { pick(zh: "睡着", en: "Fell asleep") }
        static var wokeOnce: String { pick(zh: "醒了一次", en: "Woke up") }
        static var gotUp: String { pick(zh: "起床", en: "Got up") }
        static var body: String { pick(zh: "身体", en: "Body") }
        // a stretch she is not sure of
        static var unsure: String { pick(zh: "她拿不准", en: "She isn't sure") }
        static func guess(_ title: String) -> String { pick(zh: "我猜是\(title)。", en: "My guess: \(title).") }
        static var sawWhat: String { pick(zh: "她看到的", en: "What she saw") }
        static var rightOrNot: String { pick(zh: "对不对？", en: "Is that right?") }
        static var right: String { pick(zh: "对的", en: "Right") }
        static var notRight: String { pick(zh: "不对，跟她说", en: "No, tell her") }
        // something not yet happened
        static var fromWhere: String { pick(zh: "从哪来的", en: "Where it came from") }
        static var howToGo: String { pick(zh: "怎么去", en: "Getting there") }
        static var going: String { pick(zh: "确认去", en: "I'll go") }
        static var notGoing: String { pick(zh: "这次不去", en: "Not this time") }
    }
}

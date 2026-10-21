import Foundation

extension Words {
    /// The fixed words of the home's top, where she sits (Sky/DayTimelineSeat.swift): her cards, the way back to now, the weather and the count of what waits beside the date, and what is
    /// said of her to VoiceOver.
    @MainActor enum Home {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        // her cards
        static var type: String { pick(zh: "打字", en: "Type") }
        static var tidy: String { pick(zh: "理一下", en: "Tidy up") }
        static var tidying: String { pick(zh: "在理…", en: "Tidying…") }
        static var tidyNothing: String { pick(zh: "没有要改的", en: "Nothing to change") }
        static var tidyFailed: String { pick(zh: "没理成，等会儿再试", en: "Couldn't tidy it. Try again later") }
        static var history: String { pick(zh: "聊天记录", en: "History") }
        static var backToToday: String { pick(zh: "回到今天", en: "Back to today") }
        static var backToNow: String { pick(zh: "回到现在", en: "Back to now") }

        // her, to VoiceOver: she is drawn under the screen's controls, so an element stands in for her
        static var her: String { "Jarvis" }
        static var herHint: String { pick(zh: "点一下和她说话", en: "Tap to talk to her") }

        // beside the date
        static func weather(_ text: String) -> String { pick(zh: "天气 \(text)", en: "Weather \(text)") }
        static var sunHint: String { pick(zh: "显示日出和日落", en: "Shows sunrise and sunset") }
        /// what the weather shows when it is tapped, as VoiceOver says it (clock times; either may be missing)
        static func sun(rise: String?, set: String?) -> String {
            [rise.map { pick(zh: "日出 \($0)", en: "Sunrise \($0)") }, set.map { pick(zh: "日落 \($0)", en: "Sunset \($0)") }].compactMap { $0 }.joined(separator: pick(zh: "，", en: ", "))
        }
        /// what waits on him, on the pill over the list (`count` as it rolls down) and as VoiceOver says it
        static func waiting(_ count: Int) -> String { pick(zh: "\(count) 件等你", en: "\(count) waiting") }
        static func waitingSpoken(_ count: Int) -> String { pick(zh: "\(count) 件事等你", en: count == 1 ? "1 thing waits on you" : "\(count) things wait on you") }
    }
}

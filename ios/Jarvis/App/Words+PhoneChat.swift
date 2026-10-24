import Foundation

extension Words {
    /// The strings of the chat page's own parts: the strip at its top, the list of past chats, the cards that jump from her answer (Sky/StarPanel.swift, Sky/PastChats.swift, Sky/ChatLinks.swift).
    @MainActor enum PhoneChat {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        // the strip
        static var history: String { pick(zh: "以前的聊天", en: "Past chats") }
        static var spokenOn: String { pick(zh: "她出声，点一下关掉", en: "She speaks aloud. Tap to turn it off") }
        static var spokenOff: String { pick(zh: "她不出声，点一下打开", en: "She is silent. Tap to turn her voice on") }
        /// the chip under her while she is doing something
        static func working(_ count: Int) -> String { pick(zh: "她在做 \(count)", en: "She's on \(count)") }

        // the list of past chats
        static var searchPrompt: String { pick(zh: "搜聊过的话", en: "Search what was said") }
        static var searchClear: String { pick(zh: "清除", en: "Clear") }
        static var empty: String { pick(zh: "还没有聊过", en: "Nothing said yet") }
        static var olderInSearch: String { pick(zh: "更早的在上面搜", en: "Older chats are found by searching above") }
        static var searching: String { pick(zh: "在找…", en: "Searching…") }
        static var noHits: String { pick(zh: "没有找到", en: "Nothing found") }
        static var searchFailed: String { pick(zh: "没连上，等会儿再搜", en: "Couldn't reach her. Try again later") }
        static func sentences(_ count: Int) -> String { pick(zh: "\(count) 句", en: count == 1 ? "1 message" : "\(count) messages") }
        static var his: String { pick(zh: "我", en: "Me") }
        static var hers: String { pick(zh: "她", en: "Her") }
        static var back: String { pick(zh: "返回", en: "Back") }
        static var dayLoading: String { pick(zh: "在翻这一天…", en: "Opening that day…") }
        static var dayFailed: String { pick(zh: "这一天没打开，等会儿再试", en: "Couldn't open that day. Try again later") }
        static var dayEmpty: String { pick(zh: "这一天没有聊天", en: "Nothing said that day") }

        // the cards under her answer that jump to a row of the day or a trip of the map
        static var jumpRow: String { pick(zh: "今天页", en: "Today") }
        static var jumpOuting: String { pick(zh: "地图上的一趟", en: "A trip on the map") }
    }
}

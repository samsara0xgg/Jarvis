import Foundation

extension Words {
    /// The strings of the chat page's own parts: the strip at its top, the card of search and topics, the cards that jump from her answer (Sky/StarPanel.swift, Sky/ChatSearch.swift, Sky/ChatLinks.swift).
    @MainActor enum PhoneChat {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }
        /// what he searched for, as the bar over the field names the hits he steps through
        static func searched(_ s: String) -> String { pick(zh: "「\(s)」", en: "“\(s)”") }

        // the strip
        static var history: String { pick(zh: "搜我们说过的和她整理的话题", en: "Search and topics") }
        static var spokenOn: String { pick(zh: "她出声，点一下关掉", en: "She speaks aloud. Tap to turn it off") }
        static var spokenOff: String { pick(zh: "她不出声，点一下打开", en: "She is silent. Tap to turn her voice on") }
        /// the chip under her while she is doing something
        static func working(_ count: Int) -> String { pick(zh: "她在做 \(count)", en: "She's on \(count)") }

        // the card of search and topics, and the bar that steps through a topic's places
        static var searchPrompt: String { pick(zh: "搜我们说过的…", en: "Search what we said…") }
        static var searchClear: String { pick(zh: "清除", en: "Clear") }
        static var searching: String { pick(zh: "在找…", en: "Searching…") }
        static var noHits: String { pick(zh: "没有找到", en: "Nothing found") }
        static var searchFailed: String { pick(zh: "没连上，等会儿再搜", en: "Couldn't reach her. Try again later") }
        static var his: String { pick(zh: "我", en: "Me") }
        static var hers: String { pick(zh: "她", en: "Her") }
        static func days(_ n: Int) -> String { pick(zh: "\(n) 天", en: n == 1 ? "1 day" : "\(n) days") }
        static var topics: String { pick(zh: "她整理的话题", en: "Topics she keeps") }
        static func place(_ i: Int, of n: Int) -> String { pick(zh: "第 \(i) 处，共 \(n) 处", en: "\(i) of \(n)") }
        static var earlierPlace: String { pick(zh: "前一处", en: "Earlier") }
        static var laterPlace: String { pick(zh: "后一处", en: "Later") }
        static var back: String { pick(zh: "返回", en: "Back") }
        static var goOnTopic: String { pick(zh: "接着聊", en: "Continue") }
        static var seeWords: String { pick(zh: "看原话", en: "See what was said") }
        static func goingOn(_ name: String) -> String { pick(zh: "接着\(name)", en: "On: \(name)") }
        static var stopTopic: String { pick(zh: "不接着了", en: "Stop") }
        static var endTrail: String { pick(zh: "不看了", en: "Done") }

        // the cards under her answer that jump to a row of the day or a trip of the map
        static var jumpRow: String { pick(zh: "今天页", en: "Today") }
        static var jumpOuting: String { pick(zh: "地图上的一趟", en: "A trip on the map") }
    }
}

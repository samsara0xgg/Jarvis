import Foundation

extension Words {
    /// Every fixed string of the watch app and its complications. The phone tells the watch the language (`WatchDay.language`).
    @MainActor enum Watch {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        static var name: String { pick(zh: "Jarvis", en: "Jarvis") }

        // the list's three sections
        static var now: String { pick(zh: "现在", en: "Now") }
        static var waiting: String { pick(zh: "等你", en: "Waiting") }
        static var next: String { pick(zh: "接下来", en: "Next") }

        /// the top-left of a waiting item's screen: which of how many
        static func waitingOf(_ i: Int, _ n: Int) -> String { pick(zh: "等你 \(i)/\(n)", en: "Waiting \(i)/\(n)") }
        /// the inline complication and the corner's label
        static func waitingCount(_ n: Int) -> String { pick(zh: "等你 \(n)", en: "\(n) waiting") }

        static var doubleTap: String { pick(zh: "双击", en: "Double tap") }
        static var done: String { pick(zh: "好了", en: "Done") }
        static var listening: String { pick(zh: "在听", en: "Listening") }
        static var thinking: String { pick(zh: "在想", en: "Thinking") }
        static var noDay: String { pick(zh: "等手机发来今天", en: "Waiting for the phone") }
        static var nothingMore: String { pick(zh: "今天没有别的了", en: "Nothing more today") }

        static var complicationName: String { pick(zh: "她和今天一圈", en: "Her and today's ring") }
        static var complicationDetail: String { pick(zh: "一圈是今天，有事等你时亮一个小点。点一下直接和她说话。", en: "The ring is today, with a dot when something waits. Tap to talk to her.") }
    }
}

import Foundation

extension Words {
    /// Every fixed string of the iPad's own home (Pad/). What it shares with the phone (the rows, the bar, the chips, an opened row) words itself where the phone does.
    @MainActor enum Pad {
        private static func pick(zh: String, en: String) -> String { Words.pick(zh: zh, en: en) }

        // the stage
        static var now: String { pick(zh: "现在", en: "Now") }
        static func waiting(_ n: Int) -> String { pick(zh: "等你 \(n)", en: "Waiting on you · \(n)") }
        static var backToNow: String { pick(zh: "今天", en: "Today") }

        // the day
        static var unpaired: String { pick(zh: "还没连上大脑，连上以后这里会排出你的一天。", en: "Not connected to your brain yet. Once it is, your day lines up here.") }
        static var nothing: String { pick(zh: "这一天没有记下什么。", en: "Nothing was recorded that day.") }
        static var bar: String { pick(zh: "一天的概览", en: "The day at a glance") }
    }
}
